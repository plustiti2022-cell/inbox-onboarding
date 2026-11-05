"""Configured OpenAI-compatible chat endpoint; no backup model or endpoint."""
from __future__ import annotations

import json
import re
import ssl
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import certifi


def read_env(path: Path) -> dict[str, str]:
    """Read this file only, without shell expansion or printing credentials."""
    result = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.rfind(value[0])
            if end < 1 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                raise ValueError(f"Invalid quoted .env setting: {key.strip()}")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        result[key.strip()] = value
    return result


def validate_result(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("AI output must be a JSON object")
    string_fields = ("company_name", "customer_name", "email", "ball_in_court", "reason")
    for key in string_fields:
        if not isinstance(value.get(key), str):
            raise ValueError(f"Invalid AI string field: {key}")
        value[key] = value[key].strip()
    for key in ("is_onboarding", "blocker_resolved"):
        if type(value.get(key)) is not bool:
            raise ValueError(f"Invalid AI boolean field: {key}")
    for key in ("blocker", "summary", "handler"):
        if key not in value or (value[key] is not None and not isinstance(value[key], str)):
            raise ValueError(f"Invalid AI nullable text field: {key}")
        if isinstance(value[key], str):
            value[key] = value[key].strip() or None
    if value["ball_in_court"] not in ("", "球在我方", "球在客户", "球在合规"):
        raise ValueError("Invalid ball_in_court")
    if value["email"] and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value["email"]):
        raise ValueError("AI email must contain one actual email address")
    if value["blocker_resolved"] and value["blocker"]:
        raise ValueError("blocker_resolved conflicts with an outstanding blocker")
    if value["is_onboarding"] and not value["summary"]:
        raise ValueError("Onboarding output must include a summary")
    return value


class MailAnalyzer:
    def __init__(self, env: dict, config: dict, prompt_path: Path):
        missing = [k for k in ("LLM_API_KEY", "LLM_API_HOST", "LLM_MODEL") if not env.get(k)]
        if missing:
            raise ValueError("Missing settings: " + ", ".join(missing))
        if env.get("LLM_PROVIDER", "openai_compatible") not in ("openai_compatible", "openai"):
            raise ValueError("This script requires LLM_PROVIDER=openai_compatible (or openai)")
        host = env["LLM_API_HOST"].rstrip("/")
        parsed = urlsplit(host)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Invalid LLM_API_HOST")
        self.url = host if host.endswith("/chat/completions") else host + "/chat/completions"
        self.key, self.model = env["LLM_API_KEY"], env["LLM_MODEL"]
        self.timeout = float(env.get("LLM_TIMEOUT_SECONDS", "300"))
        if self.timeout <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be positive")
        thinking = env.get("LLM_ENABLE_THINKING", "").lower()
        if thinking not in ("", "true", "false"):
            raise ValueError("LLM_ENABLE_THINKING must be true or false")
        self.enable_thinking = None if not thinking else thinking == "true"
        self.config = config
        self.prompt = prompt_path.read_text(encoding="utf-8-sig")
        self.context = ssl.create_default_context(cafile=certifi.where())

    def analyze(self, mail: dict, known_customers: list[dict]) -> dict:
        content = {
            "身份配置": {key: self.config.get(key, []) for key in (
                "internal_domains", "compliance_addresses", "compliance_name_hints",
                "own_team_addresses", "own_team_name_hints")},
            "邮件发件人名和邮箱": {"name": mail.get("from.emailAddress.name", ""), "email": mail.get("from.emailAddress.address", "")},
            "邮件收件人名和邮箱": {"names": mail.get("toRecipients.names", ""), "emails": mail.get("toRecipients.addresses", "")},
            "邮件抄送人名和邮箱": {"names": mail.get("ccRecipients.names", ""), "emails": mail.get("ccRecipients.addresses", "")},
            "邮件密送人名和邮箱": {"names": mail.get("bccRecipients.names", ""), "emails": mail.get("bccRecipients.addresses", "")},
            "邮件主题": mail.get("subject", ""),
            "邮件正文": mail.get("body.content（去除html）", ""),
            "has_attachments": mail.get("hasAttachments", False),
            "公司名称|客户名称|邮件列表": known_customers,
        }
        payload = {"model": self.model, "stream": False, "messages": [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": json.dumps(content, ensure_ascii=False)},
        ]}
        if self.config.get("json_mode", True):
            payload["response_format"] = {"type": "json_object"}
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking
        request = Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout, context=self.context) as response:
                result = json.load(response)
        except HTTPError as error:
            code = error.code
            detail = error.read().decode("utf-8", errors="replace").lower()
            error.close()
            if any(term in detail for term in ("incorrect api key", "invalid api key", "invalid_api_key", "unauthorized")):
                try:
                    provider_code = json.loads(detail).get("error", {}).get("code", "")
                except (ValueError, AttributeError):
                    provider_code = ""
                if provider_code == "bad_response_status_code":
                    raise RuntimeError(
                        f"LLM HTTP {code}: provider model route reported an invalid API key; "
                        "verify this model's upstream credentials/routing with the provider"
                    ) from None
                raise RuntimeError(f"LLM HTTP {code}: configured API key was rejected; update LLM_API_KEY for this host") from None
            raise RuntimeError(f"LLM HTTP {code}; check configured endpoint, model and quota") from None
        except (URLError, TimeoutError, ConnectionError, HTTPException):
            raise RuntimeError("LLM network error or timeout") from None
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"):
                raise ValueError("LLM did not finish a complete answer")
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("LLM returned no text content")
            # Accept a single JSON code fence, never extract an arbitrary fragment.
            text = re.sub(r"^```(?:json)?\s*\n(.*?)\n```$", r"\1", text.strip(), flags=re.S)
            return validate_result(json.loads(text))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ValueError("LLM returned an invalid chat completion or JSON object") from None
