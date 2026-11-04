"""Read Microsoft Graph using .env and export the last 24 hours to Excel.

Run: python export_mail_last24h.py
Requires Node.js, certifi and @oai/artifact-tool. Use --node for an explicit executable.
No LLM is used. Credentials are never written to the output or console.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import certifi

ROOT = Path(__file__).resolve().parent
SHANGHAI = timezone(timedelta(hours=8))
REQUIRED = tuple("MICROSOFT_GRAPH_" + key for key in
                 ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "USER_ID"))
# Preserve the user's literal headings, including the two incomplete n8n snippets.
HEADERS = [
    "internetMessageId", "conversationId", "receivedDateTime", "webLink",
    "hasAttachments", "from.emailAddress.name", "from.emailAddress.address",
    ".item.json.value[0].toRecipients ?? []).map(r => r.emailAddress?.name).filter(Boolean).join(', ') }}",
    ".item.json.value[0].toRecipients ?? []).map(r => r.emailAddress?.address).filter(Boolean).join(', ') }}",
    "{{ (.item.json.value[0].ccRecipients ?? []).map(r => r.emailAddress?.name).filter(Boolean).join(', ') }}",
    "{{ (.item.json.value[0].ccRecipients ?? []).map(r => r.emailAddress?.address).filter(Boolean).join(', ') }}",
    "{{ (.item.json.value[0].bccRecipients ?? []).map(r => r.emailAddress?.name).filter(Boolean).join(', ') }}",
    "{{ (.item.json.value[0].bccRecipients ?? []).map(r => r.emailAddress?.address).filter(Boolean).join(', ') }}",
    "subject", "body.content（去除html）", "AI处理", "是否为开户邮件",
]
SELECT = ("id", "internetMessageId", "conversationId", "receivedDateTime",
          "webLink", "hasAttachments", "from", "toRecipients", "ccRecipients",
          "bccRecipients", "subject", "body")


def read_config(path: Path) -> dict[str, str]:
    """Read only Graph keys; support ordinary quoted/unquoted .env assignments.

    Does not expand variables, execute shell commands, or interpret backslashes.
    Read this file only. No environment override or alternate configuration.
    """
    config = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in REQUIRED:
                continue
            value = value.strip()
            if value[:1] in ("'", '"'):
                delimiter = value[0]
                closing = value.rfind(delimiter)
                if closing < 1 or (value[closing + 1:].strip() and
                                   not value[closing + 1:].strip().startswith("#")):
                    raise ValueError(f"Invalid quoted .env value: {key}")
                value = value[1:closing]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            config[key] = value
    missing = [key for key in REQUIRED if not config.get(key)]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    return config


class PlainTextParser(HTMLParser):
    """Hard rules: skip scripts/styles/head, break blocks, decode entities."""
    BLOCKS = {"address", "article", "aside", "blockquote", "div", "dl", "dt", "dd",
              "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
              "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
              "p", "pre", "section", "table", "tr", "ul"}
    SKIP = {"script", "style", "head", "noscript", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skipped = []

    def handle_starttag(self, tag, attrs):
        if self.skipped:
            if tag in self.SKIP:
                self.skipped.append(tag)
            return
        if tag in self.SKIP:
            self.skipped.append(tag)
        elif tag == "br" or tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(alt)

    def handle_startendtag(self, tag, attrs):
        if tag not in self.SKIP:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.skipped:
            if tag == self.skipped[-1]:
                self.skipped.pop()
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(data)


def body_text(body) -> str:
    body = body or {}
    content = body.get("content") or ""
    if str(body.get("contentType", "html")).lower() == "text":
        return content.replace("\r\n", "\n").replace("\r", "\n")
    parser = PlainTextParser()
    parser.feed(content)
    parser.close()
    value = "".join(parser.parts).replace("\xa0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def recipients(message: dict, field: str, part: str) -> str:
    # Same semantics as map(emailAddress?.part).filter(Boolean).join(', ').
    return ", ".join(str(value) for recipient in message.get(field) or []
                     if (value := ((recipient or {}).get("emailAddress") or {}).get(part)))


def map_message(message: dict) -> list:
    sender = (message.get("from") or {}).get("emailAddress") or {}
    return [message.get("internetMessageId") or "", message.get("conversationId") or "",
            message.get("receivedDateTime") or "", message.get("webLink") or "",
            message.get("hasAttachments") if message.get("hasAttachments") is not None else "",
            sender.get("name") or "", sender.get("address") or "",
            recipients(message, "toRecipients", "name"),
            recipients(message, "toRecipients", "address"),
            recipients(message, "ccRecipients", "name"),
            recipients(message, "ccRecipients", "address"),
            recipients(message, "bccRecipients", "name"),
            recipients(message, "bccRecipients", "address"),
            message.get("subject") or "", body_text(message.get("body")), "AI未完成", ""]


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed


class GraphClient:
    def __init__(self, config: dict):
        self.context = ssl.create_default_context(cafile=certifi.where())
        self.config = config
        self.token = ""

    def request(self, request: Request) -> dict:
        # Retry only transient HTTP/network errors; maximum three total attempts.
        # No alternate endpoint, account, configuration or substituted result.
        for attempt in range(3):
            try:
                with urlopen(request, timeout=60, context=self.context) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                error.close()
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(2 ** attempt)

    def authenticate(self):
        config = self.config
        url = ("https://login.microsoftonline.com/" +
               quote(config["MICROSOFT_GRAPH_TENANT_ID"], safe="") + "/oauth2/v2.0/token")
        data = urlencode({"client_id": config["MICROSOFT_GRAPH_CLIENT_ID"],
                          "client_secret": config["MICROSOFT_GRAPH_CLIENT_SECRET"],
                          "scope": "https://graph.microsoft.com/.default",
                          "grant_type": "client_credentials"}).encode()
        payload = self.request(Request(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded"}, method="POST"))
        self.token = payload["access_token"]

    def fetch(self, start: datetime, end: datetime):
        query = {"$select": ",".join(SELECT), "$top": "100",
                 "$filter": f"receivedDateTime ge {iso(start)} and receivedDateTime lt {iso(end)}",
                 "$orderby": "receivedDateTime asc"}
        user = quote(self.config["MICROSOFT_GRAPH_USER_ID"], safe="")
        url = f"https://graph.microsoft.com/v1.0/users/{user}/messages?" + urlencode(query)
        messages = []
        pages = 0
        self.authenticate()
        while url:
            headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                       "Prefer": 'IdType="ImmutableId", outlook.body-content-type="html"'}
            payload = self.request(Request(url, headers=headers))
            messages.extend(payload["value"])
            pages += 1
            print(f"Fetched page {pages}: {len(payload['value'])} messages", flush=True)
            url = payload.get("@odata.nextLink")
        messages.sort(key=lambda message: parse_time(message["receivedDateTime"]))
        return messages, pages


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def export_excel(mapped_path: Path, node: str, modules: str | None):
    args = [node, str(ROOT / "write_mail_workbook.mjs"), str(mapped_path)]
    if modules:
        args.append(modules)
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT.parent / ".env")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--node", default="node", help="Node.js executable")
    parser.add_argument("--node-modules", help="Explicit node_modules directory for @oai/artifact-tool")
    args = parser.parse_args()
    # Freeze the cutoff before authentication/pagination so all pages share one window.
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=24)
    config = read_config(args.env)
    messages, pages = GraphClient(config).fetch(start, end)
    output_dir = (args.output_dir or ROOT / "outputs" /
                  ("mail_last24h_" + end.astimezone(SHANGHAI).strftime("%Y%m%d_%H%M%S"))).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    rows = [map_message(message) for message in messages]
    manifest = {"complete": False, "scope": "all mailbox folders",
                "window_start_utc_inclusive": iso(start), "window_end_utc_exclusive": iso(end),
                "window_start_shanghai_inclusive": start.astimezone(SHANGHAI).isoformat(),
                "window_end_shanghai_exclusive": end.astimezone(SHANGHAI).isoformat(),
                "message_count": len(messages), "page_count": pages,
                "column_count": len(HEADERS),
                "date_column_timezone": "UTC", "sort_order": "receivedDateTime asc", "llm_used": False,
                "source_documentation": "https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0"}
    write_json(output_dir / "run_manifest.json", manifest)
    mapped_path = output_dir / "mapped_rows.json"
    write_json(mapped_path, {"headers": HEADERS, "rows": rows})
    export_excel(mapped_path, args.node, args.node_modules)
    manifest["complete"] = True
    manifest["excel"] = str(output_dir / "mail_last24h.xlsx")
    manifest["validation"] = json.loads((output_dir / "validation.json").read_text(encoding="utf-8"))
    write_json(output_dir / "run_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        sys.exit(1)
