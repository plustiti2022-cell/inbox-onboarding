"""DeepSeek Chat Completions -> Python tool -> tool result -> final answer."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .tools import TOOL_SCHEMAS


class DeepSeekClient:
    def __init__(self, env):
        for name in ("LLM_API_HOST", "LLM_API_KEY", "LLM_MODEL"):
            if not env.get(name):
                raise ValueError("Missing " + name)
        host = env["LLM_API_HOST"].rstrip("/")
        parsed = urlsplit(host)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LLM_API_HOST must be an HTTPS endpoint")
        self.url = host if host.endswith("/chat/completions") else host + "/chat/completions"
        self.model, self.key = env["LLM_MODEL"], env["LLM_API_KEY"]
        self.timeout = float(env.get("LLM_TIMEOUT_SECONDS", "120"))
        if self.timeout <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be positive")
        thinking = env.get("LLM_ENABLE_THINKING", "false").lower()
        if thinking not in ("true", "false"):
            raise ValueError("LLM_ENABLE_THINKING must be true or false")
        self.thinking = "enabled" if thinking == "true" else "disabled"
        self.session = requests.Session()

    def complete(self, messages, *, json_mode=False):
        # No automatic retry: previous tool calls may already have written data.
        payload = {"model": self.model, "messages": messages,
                   "thinking": {"type": self.thinking}, "stream": False, "max_tokens": 4096}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload.update(tools=TOOL_SCHEMAS, tool_choice="auto")
        try:
            response = self.session.post(self.url, headers={"Authorization": "Bearer " + self.key},
                json=payload, timeout=(10, self.timeout))
            if response.status_code >= 400:
                raise RuntimeError(f"DeepSeek HTTP {response.status_code}; check endpoint, model, credentials and quota")
            data = response.json()
        except requests.RequestException:
            raise RuntimeError("DeepSeek network error or timeout") from None
        except ValueError:
            raise RuntimeError("DeepSeek returned a non-JSON response") from None
        try:
            choice = data["choices"][0]
            if choice["finish_reason"] not in ("stop", "tool_calls"):
                raise RuntimeError("DeepSeek response incomplete; no tools from this response were executed")
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError()
            return message
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("DeepSeek returned an invalid chat completion") from None

    def close(self):
        self.session.close()


class BitableAgent:
    def __init__(self, client, tools, *, prompt=None, max_rounds=12, max_tool_calls=30):
        if max_rounds < 1 or max_tool_calls < 1:
            raise ValueError("Agent limits must be positive")
        self.client, self.tools = client, tools
        self.prompt = prompt if prompt is not None else Path(__file__).with_name("prompt.txt").read_text(encoding="utf-8")
        self.max_rounds, self.max_tool_calls = max_rounds, max_tool_calls
        self.trace = []

    def run(self, instruction, *, on_tool=None):
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("Instruction cannot be empty")
        self.trace = []
        messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": instruction}]
        for _ in range(self.max_rounds):
            message = self.client.complete(messages)
            calls = message.get("tool_calls") or []
            if not calls:
                if not isinstance(message.get("content"), str) or not message["content"].strip():
                    raise RuntimeError("Model returned neither tools nor an answer")
                return {"answer": message["content"], "tool_calls": self.trace}
            if not isinstance(calls, list) or len(self.trace) + len(calls) > self.max_tool_calls:
                raise RuntimeError("Tool call limit exceeded; inspect completed calls before retrying")
            # Preserve reasoning_content for DeepSeek's thinking-mode continuation.
            assistant = {k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "reasoning_content")}
            messages.append(assistant)
            for call in calls:
                if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                        or call.get("type") != "function" or not isinstance(call.get("function"), dict)):
                    raise RuntimeError("Malformed tool call")
                name = call["function"].get("name", "")
                try:
                    arguments = json.loads(call["function"]["arguments"])
                except (KeyError, TypeError, ValueError):
                    arguments = None
                    result = {"ok": False, "error": "Tool arguments must be valid JSON"}
                else:
                    result = self.tools.execute(name, arguments)
                entry = {"name": name, "arguments": arguments, "result": result}
                self.trace.append(entry)
                if on_tool:
                    on_tool(entry)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False, allow_nan=False)})
        raise RuntimeError("Agent round limit exceeded; inspect completed calls before retrying")
