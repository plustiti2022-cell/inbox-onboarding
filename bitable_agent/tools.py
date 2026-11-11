"""Function schemas and a direct, record-oriented Lark HTTP adapter."""
from __future__ import annotations

import inspect
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from lark_support import LarkAPI, LarkAuth, LarkError, LarkHTTP, KeychainJSON, SecureJSON


def function(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


TEXT = {"type": "string", "minLength": 1}
TABLE = {"table": {**TEXT, "description": "list_tables 回傳的表名或 table_id"}}
RECORD = {**TABLE, "record_id": TEXT}
FIELDS = {"fields": {"type": "object", "minProperties": 1,
                     "description": "欄位名稱對應新值；null 或空字串表示清空；日期用帶時區 ISO 8601 字串"}}
TOOL_SCHEMAS = [
    function("list_tables", "列出目前配置可操作的資料表。", {}),
    function("list_fields", "查詢一張表的真實欄位名稱、類型和選項。", TABLE, ["table"]),
    function("search_records", "查詢記錄；field 和 value 同時提供時作文字/單選欄位的精確匹配。支援分頁。", {
        **TABLE, "field": TEXT, "value": {"type": "string"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
        "page_token": TEXT}, ["table"]),
    function("get_record", "依 record_id 讀取單筆記錄。", RECORD, ["table", "record_id"]),
    function("create_record", "新增一筆記錄，回傳新 record_id。", {**TABLE, **FIELDS}, ["table", "fields"]),
    function("update_record", "修改已有記錄的指定欄位，其餘欄位保留。", {**RECORD, **FIELDS},
             ["table", "record_id", "fields"]),
    function("delete_record", "刪除指定記錄。僅在使用者明確要求刪除時使用。", RECORD, ["table", "record_id"]),
]


class BitableTools:
    def __init__(self, env, *, api=None, auth_project=None):
        self.base = env.get("LARK_BASE_TOKEN", "")
        self.tables = {name: env[key] for name, key in (
            ("邮件信息表", "LARK_MAIL_TABLE_ID"), ("开户进度表", "LARK_PROGRESS_TABLE_ID")) if env.get(key)}
        if not self.base or not self.tables:
            raise ValueError("Missing LARK_BASE_TOKEN or configured table IDs")
        if api is not None:
            self.api = api
        elif auth_project is None:
            self.api = LarkAPI(env)
        else:
            # Point to the established authorization; never copy user tokens.
            root = Path(auth_project).expanduser().resolve() / ".runtime/lark" / env["LARK_APP_ID"]
            http = LarkHTTP(env)
            storage = KeychainJSON if sys.platform == "darwin" else SecureJSON
            auth = LarkAuth(env, http=http, vault=storage(root / "user_token.dpapi"))
            if sys.platform == "darwin":
                auth.refresh_lock_path = root / "refresh.lock"
            self.api = LarkAPI(env, http=http, auth=auth)
        self.metadata = {}

    def path(self, table):
        if not isinstance(table, str):
            raise ValueError("table must be a configured name or ID")
        table_id = self.tables.get(table, table)
        if table_id not in self.tables.values():
            raise ValueError("Table is not configured for this agent; use list_tables")
        return f"/open-apis/bitable/v1/apps/{quote(self.base, safe='')}/tables/{quote(table_id, safe='')}"

    def record_path(self, table, record_id):
        if not isinstance(record_id, str) or not re.fullmatch(r"rec[A-Za-z0-9_]+", record_id):
            raise ValueError("Invalid record_id")
        return self.path(table) + "/records/" + record_id

    def list_tables(self):
        return {"tables": [{"name": name, "table_id": value} for name, value in self.tables.items()]}

    def list_fields(self, table):
        path = self.path(table)
        fields = self.api.paged(path + "/fields", query={"page_size": 100})
        self.metadata[path] = {f["field_name"]: f for f in fields}
        return {"fields": [{k: v for k, v in f.items() if k in (
            "field_name", "field_id", "type", "property", "is_primary")} for f in fields],
            "writable_types": {"1": "text", "2": "number", "3": "single select",
                               "4": "multi select", "5": "ISO date with timezone", "7": "boolean"}}

    def field_map(self, table):
        path = self.path(table)
        if path not in self.metadata:
            self.list_fields(table)
        return self.metadata[path]

    @staticmethod
    def record(raw):
        fields = dict(raw.get("fields", {}))
        for name, value in fields.items():
            if isinstance(value, list) and value and all(isinstance(x, dict) and "text" in x for x in value):
                fields[name] = "".join(x["text"] for x in value)
        return {"record_id": raw["record_id"], "fields": fields}

    def search_records(self, table, field=None, value=None, page_size=20, page_token=None):
        path = self.path(table)
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("page_size must be an integer from 1 to 100")
        if (field is None) != (value is None):
            raise ValueError("Supply field and value together")
        body = {}
        if field is not None:
            meta = self.field_map(table).get(field)
            if not meta or meta["type"] not in (1, 3) or not isinstance(value, str):
                raise ValueError("Exact search requires an existing text or single-select field and a string value")
            body["filter"] = {"conjunction": "and", "conditions": [
                {"field_name": field, "operator": "is", "value": [value]}]}
        query = {"page_size": page_size}
        if page_token is not None:
            if not isinstance(page_token, str) or not page_token:
                raise ValueError("Invalid page_token")
            query["page_token"] = page_token
        data = self.api.request("POST", path + "/records/search", body=body, query=query)
        return {"records": [self.record(r) for r in data.get("items", [])],
                "has_more": bool(data.get("has_more")), "page_token": data.get("page_token")}

    def get_record(self, table, record_id):
        return {"record": self.record(self.api.request("GET", self.record_path(table, record_id))["record"])}

    def encode_fields(self, table, fields):
        if not isinstance(fields, dict) or not fields:
            raise ValueError("fields must be a nonempty object")
        metadata, output = self.field_map(table), {}
        for name, value in fields.items():
            if name not in metadata:
                raise ValueError("Unknown field: " + name)
            kind = metadata[name]["type"]
            if kind not in (1, 2, 3, 4, 5, 7):
                raise ValueError(f"Unsupported writable field type: {name} ({kind})")
            if value is None or value == "":
                output[name] = None
                continue
            if kind in (1, 3) and not isinstance(value, str):
                raise ValueError("Text/select requires a string: " + name)
            if kind == 2 and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("Number requires a finite number: " + name)
            if kind == 4 and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
                raise ValueError("Multi select requires a string list: " + name)
            if kind == 7 and type(value) is not bool:
                raise ValueError("Checkbox requires a boolean: " + name)
            if kind == 5:
                if not isinstance(value, str):
                    raise ValueError("Date requires an ISO string with timezone: " + name)
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Date must include a timezone: " + name)
                value = int(parsed.timestamp() * 1000)
            output[name] = value
        return output

    def create_record(self, table, fields):
        data = self.api.request("POST", self.path(table) + "/records", body={"fields": self.encode_fields(table, fields)})
        if not data.get("record", {}).get("record_id"):
            raise LarkError("Create did not confirm record_id; inspect cloud state before retrying")
        return {"record": self.record(data["record"])}

    def update_record(self, table, record_id, fields):
        data = self.api.request("PUT", self.record_path(table, record_id), body={"fields": self.encode_fields(table, fields)})
        if data.get("record", {}).get("record_id") != record_id:
            raise LarkError("Update did not confirm target record_id")
        return {"record": self.record(data["record"])}

    def delete_record(self, table, record_id):
        data = self.api.request("DELETE", self.record_path(table, record_id))
        if data.get("deleted") is not True or data.get("record_id") != record_id:
            raise LarkError("Delete did not confirm target record_id; inspect cloud state")
        return {"record_id": record_id, "deleted": True}

    def execute(self, name, arguments):
        allowed = {s["function"]["name"] for s in TOOL_SCHEMAS}
        try:
            if name not in allowed:
                raise ValueError("Unknown tool: " + str(name))
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            method = getattr(self, name)
            inspect.signature(method).bind(**arguments)
            return {"ok": True, **method(**arguments)}
        except (ValueError, TypeError, LarkError) as error:
            return {"ok": False, "error": str(error)}

    def close(self):
        self.api.http.session.close()
