"""Native Bitable HTTPS adapter using user tokens; no CLI dependency."""
from __future__ import annotations
import copy
from disk_snapshot import DiskSnapshot, DiskRecords
from datetime import datetime, timezone
from lark_support import LarkAPI, LarkError
from storage import LATEST_RECEIVED_TIME, MAIL_TABLE, PROGRESS_TABLE, MAIL_FIELDS, PROGRESS_FIELDS, SHANGHAI

def field_schema(table):
    names = MAIL_FIELDS if table == MAIL_TABLE else PROGRESS_FIELDS
    result = []
    for name in names:
        field = {"field_name": name, "type": 5 if name in ("receivedDateTime", LATEST_RECEIVED_TIME)
                 else 7 if name == "hasAttachments" else 1}
        if field["type"] == 5:
            field["property"] = {"date_formatter": "yyyy/MM/dd HH:mm"}
        result.append(field)
    return result

class LarkRecordStore:
    def __init__(self, env, *, api=None, tables=(MAIL_TABLE, PROGRESS_TABLE), disk_backed=False):
        self.base_token = env.get("LARK_BASE_TOKEN", "")
        keys = {MAIL_TABLE: "LARK_MAIL_TABLE_ID", PROGRESS_TABLE: "LARK_PROGRESS_TABLE_ID"}
        self.table_ids = {table: env.get(keys[table], "") for table in tables}
        if not self.base_token or not all(self.table_ids.values()):
            raise ValueError("Configure the Lark Base and required table IDs in .env")
        self.api = api or LarkAPI(env)
        self.snapshot = None
        self.fields, self.tables = {}, {}
        try:
            if disk_backed:
                self.snapshot = DiskSnapshot()
            self._load_tables()
        except BaseException:
            self.close()
            raise

    def _load_tables(self):
        for table in self.table_ids:
            fields = self.api.paged(self.path(table) + "/fields", query={"page_size": 100})
            self.fields[table] = {f["field_name"]: f for f in fields}
            required = set(MAIL_FIELDS if table == MAIL_TABLE else PROGRESS_FIELDS)
            if not required.issubset(self.fields[table]):
                raise ValueError(f"Missing fields in {table}: " + ", ".join(sorted(required - self.fields[table].keys())))
            if self.snapshot is None:
                self.tables[table] = [self.decode_record(table, r) for r in self.api.paged(self.path(table) + "/records")]
            else:
                records = self.tables[table] = self.snapshot.table(table)
                for page in self.api.iter_pages(self.path(table) + "/records", query={"page_size": 50}):
                    for raw in page:
                        records.append(self.decode_record(table, raw))
                    self.snapshot.db.commit()
                    del page

    def path(self, table):
        return f"/open-apis/bitable/v1/apps/{self.base_token}/tables/{self.table_ids[table]}"

    def decode_record(self, table, record):
        raw, result = record.get("fields", {}), {}
        for name, meta in self.fields[table].items():
            value = raw.get(name, "")
            if meta["type"] == 5 and value not in ("", None):
                zone = SHANGHAI if name == LATEST_RECEIVED_TIME else timezone.utc
                value = datetime.fromtimestamp(float(value) / 1000, tz=zone).isoformat()
            elif meta["type"] == 1 and isinstance(value, list):
                value = "".join(x.get("text", "") for x in value)
            result[name] = "" if value is None else value
        return {"record_id": record["record_id"], "fields": result}

    def encode_fields(self, table, fields):
        output = {}
        for name, value in fields.items():
            if name not in self.fields[table]:
                raise ValueError(f"Unknown Lark field: {name}")
            kind = self.fields[table][name]["type"]
            if value is None or value == "":
                output[name] = None
                continue
            if kind == 5:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Cloud dates must include a timezone")
                value = int(parsed.timestamp() * 1000)
            elif kind == 7:
                if type(value) is not bool:
                    raise ValueError(f"Checkbox must be a boolean: {name}")
            elif kind in (1, 3):
                if not isinstance(value, str):
                    raise ValueError(f"Text/select field must be a string: {name}")
            else:
                raise ValueError(f"Unsupported writable field type for {name}: {kind}")
            output[name] = value
        return output

    def list_records(self, table):
        return copy.deepcopy(list(self.tables[table]))

    def pending_snapshot(self, limit=None):
        from workflow import mail_time
        records = self.tables[MAIL_TABLE]
        if isinstance(records, DiskRecords):
            return records.pending_snapshot(mail_time, limit)
        pending = sorted((r for r in self.list_records(MAIL_TABLE)
                          if r["fields"].get("AI处理") != "AI已处理"), key=mail_time)
        if limit is not None:
            pending = pending[:limit]
        return len(pending), iter(pending)

    def customer_identities(self):
        from workflow import IDENTITY_FIELDS
        records = self.tables[PROGRESS_TABLE]
        if isinstance(records, DiskRecords):
            return records.identities(IDENTITY_FIELDS)
        return [{"record_id": r["record_id"],
                 "fields": {field: r["fields"].get(field, "") for field in IDENTITY_FIELDS}}
                for r in records]

    def record_counts(self):
        mail = self.tables[MAIL_TABLE]
        return {"mail_records": len(mail), "progress_records": len(self.tables[PROGRESS_TABLE]),
                "remaining": mail.pending_count() if isinstance(mail, DiskRecords) else
                sum(r["fields"].get("AI处理") != "AI已处理" for r in mail)}

    def create_record(self, table, fields):
        data = self.api.request("POST", self.path(table) + "/records", body={"fields": self.encode_fields(table, fields)})
        record = data.get("record", {})
        if not record.get("record_id"):
            raise LarkError("Cloud create did not return a record ID; inspect cloud state before retrying")
        local = {"record_id": record["record_id"], "fields": copy.deepcopy(fields)}
        self.tables[table].append(local)
        return copy.deepcopy(local)

    def update_record(self, table, record_id, fields):
        records = self.tables[table]
        target = records.get(record_id) if isinstance(records, DiskRecords) else next(
            (r for r in records if r["record_id"] == record_id), None)
        if target is None:
            raise ValueError("Unknown cloud record ID")
        data = self.api.request("PUT", self.path(table) + "/records/" + record_id,
                                body={"fields": self.encode_fields(table, fields)})
        if data.get("record", {}).get("record_id") != record_id:
            raise LarkError("Cloud update did not confirm the target record ID")
        target["fields"].update(copy.deepcopy(fields))
        if isinstance(records, DiskRecords):
            records.replace(target)

    def flush(self, preview_dir=None):
        if self.snapshot is not None:
            self.snapshot.db.commit()

    def close(self):
        try:
            if self.snapshot is not None:
                self.snapshot.close()
        finally:
            self.api.http.session.close()
