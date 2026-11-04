"""Copy the two existing Excel tables to the native Base, preserving AI statuses."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from model import read_env
from storage import MAIL_TABLE, PROGRESS_TABLE, ExcelRecordStore
from workflow import find_customer
from lark_store import LarkRecordStore
from lark_support import PROJECT, LarkError, LarkRunLock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    args = parser.parse_args(argv)
    env = read_env(args.env)
    guard = LarkRunLock(env["LARK_APP_ID"])
    # This store is read-only here; no Node writer is started.
    source = ExcelRecordStore(args.workbook, "unused", "unused")
    try:
        target = LarkRecordStore(env)
    except BaseException:
        source.close()
        guard.close()
        raise
    receipt_path = PROJECT / ".runtime" / "lark" / env["LARK_APP_ID"] / ("migration_" + target.base_token + ".json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
    created, reused = 0, 0
    try:
        for table in (PROGRESS_TABLE, MAIL_TABLE):
            cloud = target.list_records(table)
            for row in source.list_records(table):
                key = table + ":" + row["record_id"]
                if key in receipt and any(r["record_id"] == receipt[key] for r in cloud):
                    reused += 1
                    continue
                fields = row["fields"]
                if table == PROGRESS_TABLE:
                    match = find_customer(cloud, {"company_name": fields.get("公司名称", ""),
                                                  "customer_name": fields.get("客户名称", ""), "email": fields.get("邮件", "")})
                    matches = [match] if match else []
                else:
                    message_id = fields.get("internetMessageId")
                    if not message_id:
                        raise ValueError("Migration requires internetMessageId to recover safely after an interrupted create")
                    matches = [r for r in cloud if r["fields"].get("internetMessageId") == message_id]
                if len(matches) > 1:
                    raise LarkError("Migration found ambiguous existing cloud records")
                if matches:
                    mapped = matches[0]
                    reused += 1
                else:
                    mapped = target.create_record(table, fields)
                    cloud.append(mapped)
                    created += 1
                receipt[key] = mapped["record_id"]
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"migrated": True, "identity": "user", "created": created, "reused": reused,
                          "base_url": env.get("LARK_BASE_URL"), "mapping_file": str(receipt_path)}, ensure_ascii=False))
    finally:
        source.close()
        target.close()
        guard.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
