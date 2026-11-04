"""Record-oriented local Excel store. XLSX itself is the persistent source of truth.

The native Lark adapter shares list_records/create_record/update_record/flush;
business logic never uses Excel row numbers or cell addresses.
"""
from __future__ import annotations

import copy
import json
import posixpath
import subprocess
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

MAIL_TABLE = "邮件信息表"
PROGRESS_TABLE = "开户进度表"
MAIL_FIELDS = [
    "internetMessageId", "conversationId", "receivedDateTime", "webLink", "hasAttachments",
    "from.emailAddress.name", "from.emailAddress.address", "toRecipients.names", "toRecipients.addresses",
    "ccRecipients.names", "ccRecipients.addresses", "bccRecipients.names", "bccRecipients.addresses",
    "subject", "body.content（去除html）", "AI处理", "是否为开户邮件", "处理说明",
]
LATEST_RECEIVED_TIME = "最新收件时间"
PROGRESS_FIELDS = ["公司名称", "客户名称", "邮件", "球在谁方", "卡点", "简要描述", "经办人", LATEST_RECEIVED_TIME, "internetMessageId"]
SHANGHAI = timezone(timedelta(hours=8))


class RecordStore(Protocol):
    def list_records(self, table: str) -> list[dict]: ...
    def create_record(self, table: str, fields: dict) -> dict: ...
    def update_record(self, table: str, record_id: str, fields: dict) -> None: ...
    def flush(self, preview_dir: Path | None = None) -> None: ...


def read_xlsx(path: Path) -> dict[str, list[list]]:
    """Read saved cell values independently of the writer, including shared strings."""
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): posixpath.normpath("xl/" + r.get("Target"))
                   if not r.get("Target", "").startswith("/") else r.get("Target").lstrip("/") for r in rels}
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(t.text or "" for t in si.findall(".//{*}t"))
                      for si in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("{*}si")]
        result = {}
        for sheet in workbook.findall(".//{*}sheets/{*}sheet"):
            rel_id = next(v for k, v in sheet.attrib.items() if k.endswith("}id"))
            root = ET.fromstring(archive.read(targets[rel_id]))
            rows = []
            for row in root.findall(".//{*}sheetData/{*}row"):
                index = int(row.get("r")) - 1
                while len(rows) <= index:
                    rows.append([])
                for cell in row.findall("{*}c"):
                    letters = "".join(c for c in cell.get("r") if c.isalpha())
                    column = 0
                    for char in letters:
                        column = column * 26 + ord(char.upper()) - 64
                    while len(rows[index]) < column:
                        rows[index].append("")
                    node = cell.find("{*}v")
                    raw = node.text if node is not None and node.text is not None else ""
                    kind = cell.get("t")
                    if kind == "s":
                        value = shared[int(raw)]
                    elif kind == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//{*}t"))
                    elif kind == "b":
                        value = raw == "1"
                    elif kind == "e":
                        raise ValueError(f"Excel error cell in {sheet.get('name')}: {cell.get('r')}")
                    elif kind in ("str", "d") or not raw:
                        value = raw
                    else:
                        value = float(raw)
                        if value.is_integer():
                            value = int(value)
                    rows[index][column - 1] = value
            result[sheet.get("name")] = rows
        return result


def excel_date(value, field: str):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if field == "receivedDateTime":
        return (datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(days=value)).isoformat().replace("+00:00", "Z")
    if field == LATEST_RECEIVED_TIME:
        return (datetime(1899, 12, 30, tzinfo=SHANGHAI) + timedelta(days=value)).isoformat()
    return value


def canonical_header(header: str) -> str:
    # Accept the literal n8n expression headers in the existing exporter and workflow.
    # bccRecipients contains ccRecipients as a substring; match the longer name first.
    for recipient in ("bccRecipients", "ccRecipients", "toRecipients"):
        if recipient in header and "emailAddress?." in header:
            return recipient + (".addresses" if "emailAddress?.address" in header else ".names")
    return header


def load_mail_input(path: Path) -> list[dict]:
    if path.suffix.lower() == ".xlsx":
        sheets = read_xlsx(path)
        if not sheets:
            raise ValueError("Input workbook has no sheets")
        matrix = sheets.get(MAIL_TABLE, next(iter(sheets.values())))
        if not matrix:
            return []
        headers, rows = matrix[0], matrix[1:]
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or "headers" not in payload or "rows" not in payload:
            raise ValueError("Input JSON must contain headers and rows (mapped_rows.json)")
        headers, rows = payload["headers"], payload["rows"]
    else:
        raise ValueError("Input must be mapped_rows.json or a mail .xlsx export")
    headers = [canonical_header(str(h)) for h in headers]
    required = set(MAIL_FIELDS[:17])
    if len(headers) != len(set(headers)) or not required.issubset(headers):
        raise ValueError("Mail input must contain the original 17 mail fields (or canonical headers)")
    result = []
    for row in rows:
        if not any(v not in (None, "") for v in row):
            continue
        if len(row) > len(headers):
            raise ValueError("Mail input row has more cells than headers")
        fields = {key: excel_date(row[i], key) if i < len(row) else "" for i, key in enumerate(headers)}
        fields.pop("record_id", None)
        fields["AI处理"] = fields.get("AI处理") or "AI未完成"
        result.append(fields)
    return result


class ExcelRecordStore:
    def __init__(self, path: Path, node: str, modules: str):
        self.path = path.resolve()
        self.node, self.modules = node, modules
        self.headers = {PROGRESS_TABLE: PROGRESS_FIELDS.copy(), MAIL_TABLE: MAIL_FIELDS.copy()}
        self.tables = {PROGRESS_TABLE: [], MAIL_TABLE: []}
        self.writer = None
        self.dirty = False
        if self.path.exists():
            data = read_xlsx(self.path)
            if set(data) != set(self.tables):
                raise ValueError("The simulation workbook must contain exactly 开户进度表 and 邮件信息表")
            for table, matrix in data.items():
                if not matrix:
                    raise ValueError(f"Missing headers in {table}")
                headers = [str(h) for h in matrix[0]]
                required = set(self.headers[table] + ["record_id"])
                if table == PROGRESS_TABLE:
                    required.discard("internetMessageId")  # Older simulation workbooks lack this column.
                    headers = [LATEST_RECEIVED_TIME if h == "最新ai更新时间" else h for h in headers]
                if len(headers) != len(set(headers)) or not required.issubset(headers):
                    raise ValueError(f"Invalid or missing headers in {table}")
                self.headers[table] = [h for h in headers if h != "record_id"]
                seen = set()
                for row in matrix[1:]:
                    if not any(v not in (None, "") for v in row):
                        continue
                    fields = {h: excel_date(row[i], h) if i < len(row) else "" for i, h in enumerate(headers)}
                    record_id = str(fields.pop("record_id") or "")
                    if not record_id or record_id in seen:
                        raise ValueError(f"Missing or duplicate record_id in {table}")
                    seen.add(record_id)
                    self.tables[table].append({"record_id": record_id, "fields": fields})

    def list_records(self, table: str) -> list[dict]:
        return copy.deepcopy(self.tables[table])

    def create_record(self, table: str, fields: dict) -> dict:
        self._extend_headers(table, fields)
        record = {"record_id": "rec_" + uuid.uuid4().hex, "fields": copy.deepcopy(fields)}
        self.tables[table].append(record)
        self.dirty = True
        return copy.deepcopy(record)

    def update_record(self, table: str, record_id: str, fields: dict) -> None:
        self._extend_headers(table, fields)
        for record in self.tables[table]:
            if record["record_id"] == record_id:
                record["fields"].update(copy.deepcopy(fields))
                self.dirty = True
                return
        raise KeyError(f"Unknown record_id in {table}")

    def _extend_headers(self, table: str, fields: dict):
        for field in fields:
            if field == "record_id":
                raise ValueError("record_id is metadata, not a writable field")
            if field not in self.headers[table]:
                self.headers[table].append(field)

    def flush(self, preview_dir: Path | None = None) -> None:
        if not self.dirty and self.path.exists() and preview_dir is None:
            return
        if self.writer is None:
            self.writer = subprocess.Popen(
                [self.node, str(Path(__file__).with_name("excel_bridge.mjs")), self.modules],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", text=True,
            )
        request = {"path": str(self.path), "tables": [
            {"name": table, "headers": self.headers[table], "records": records}
            for table, records in self.tables.items()
        ], "preview_dir": str(preview_dir) if preview_dir else None}
        self.writer.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.writer.stdin.flush()
        while True:
            line = self.writer.stdout.readline()
            if not line:
                raise RuntimeError("Excel writer stopped unexpectedly")
            if line.startswith("@@ONBOARDING@@"):
                response = json.loads(line[len("@@ONBOARDING@@"):])
                break
        if not response.get("ok"):
            raise RuntimeError("Excel save failed: " + response.get("error", "unknown error"))
        self.dirty = False

    def close(self):
        if self.writer is not None:
            self.writer.stdin.close()
            self.writer.wait(timeout=30)
            self.writer.stdout.close()
            self.writer = None
