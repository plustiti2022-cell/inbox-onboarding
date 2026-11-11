"""Opt-in live acceptance: verify migration, exercise AI CRUD, remove own test rows."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import MailAnalyzer, read_env
from lark_support import LarkAPI, LarkRunLock
from lark_store import LarkRecordStore
from storage import ExcelRecordStore, MAIL_TABLE, PROGRESS_TABLE
from workflow import process_pending


def audit_migration(env, workbook):
    source = ExcelRecordStore(workbook, "unused", "unused")
    cloud = LarkRecordStore(env)
    receipt_path = ROOT.parent / ".runtime/lark" / env["LARK_APP_ID"] / ("migration_" + env["LARK_BASE_TOKEN"] + ".json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    counts, errors, checked = {}, [], 0
    try:
        for table in (MAIL_TABLE, PROGRESS_TABLE):
            local, remote = source.list_records(table), cloud.list_records(table)
            counts[table] = {"local": len(local), "cloud": len(remote), "fields": len(cloud.fields[table])}
            if len(local) != len(remote):
                errors.append({"table": table, "count_mismatch": True})
            for row in local:
                rid = receipt[table + ":" + row["record_id"]]
                target = next((r for r in remote if r["record_id"] == rid), None)
                if target is None:
                    errors.append({"table": table, "missing_record": True})
                    continue
                for name, value in row["fields"].items():
                    other = target["fields"].get(name, "")
                    if cloud.fields[table][name]["type"] == 5 and value not in ("", None):
                        equal = abs(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
                                    - datetime.fromisoformat(other).timestamp()) < .001
                    else:
                        equal = value == other
                    checked += 1
                    if not equal:
                        errors.append({"table": table, "field": name})
        tables = cloud.api.paged(f"/open-apis/bitable/v1/apps/{env['LARK_BASE_TOKEN']}/tables", query={"page_size": 100})
        if len(tables) != 2:
            errors.append({"table_count": len(tables)})
        return {"passed": not errors, "counts": counts, "verified_cells": checked, "differences": errors}
    finally:
        source.close()
        cloud.close()


def exercise(env):
    suffix = uuid.uuid4().hex[:10]
    marker = "Mariner Capital " + suffix
    address = "evelyn." + suffix + "@mariner.example"
    mail_ids, results = [], []
    analyzer = MailAnalyzer(env,
                            json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig")), ROOT / "prompt_onboarding.txt")
    api = LarkAPI(env)
    before = LarkRecordStore(env)
    try:
        assert before.record_counts()["remaining"] == 0, "Resolve real pending mail before acceptance"
        progress_rows = before.list_records(PROGRESS_TABLE)
        assert not any(r["fields"].get("客户名称", "").strip().casefold() == "evelyn hart" for r in progress_rows), "Choose a distinct fixture contact before acceptance"
        before_ids = {r["record_id"] for r in progress_rows}
    finally:
        before.close()
    try:
        cases = [
            ("created", address, "Evelyn Hart", "Adrian Wu", "adrian.wu@hashkey.com", "Account opening application - " + marker,
             f"I am Evelyn Hart from {marker}. My email is {address}. We are applying to open a HashKey OTC corporate account. "
             "Our certificate of incorporation is still missing and we will send it tomorrow. Please wait for our document. Our HashKey account manager is Adrian Wu."),
            ("updated", "adrian.wu@hashkey.com", "Adrian Wu", "KYC Team", "kyc@hashkey.com", "Account opening documents complete - " + marker,
             f"Update for {marker}, customer Evelyn Hart, email {address}: The previously missing certificate has been verified and accepted. "
             "All requested documents passed completeness checks and no further supplementary documents are required. The previous missing-document blocker is fully resolved. "
             "KYC team, please perform the final account opening review. Internal account manager: Adrian Wu."),
            ("non_onboarding", "newsletter@example.invalid", "Weather Newsletter", "Adrian Wu", "adrian.wu@hashkey.com", "Weekly weather newsletter",
             "This is a weather newsletter. Tomorrow will be sunny. No account applications or customer onboarding are discussed."),
        ]
        for expected, sender, sender_name, to_name, to_address, subject, body in cases:
            store = LarkRecordStore(env)
            try:
                row = store.create_record(MAIL_TABLE, {
                    "internetMessageId": f"<nativeacceptance.{suffix}.{len(mail_ids)}@example.invalid>",
                    "receivedDateTime": datetime.now(timezone.utc).isoformat(), "hasAttachments": False,
                    "from.emailAddress.name": sender_name, "from.emailAddress.address": sender,
                    "toRecipients.names": to_name, "toRecipients.addresses": to_address,
                    "subject": subject, "body.content（去除html）": body, "AI处理": "AI未完成"})
                mail_ids.append(row["record_id"])
                report = process_pending(store, analyzer)
                if report["failed"]:
                    print(json.dumps({"live_case": expected, "errors": [x.get("error") for x in report["items"]]}, ensure_ascii=False), flush=True)
                assert report["failed"] == 0 and len(report["items"]) == 1, "Live processing failed"
                item = report["items"][0]
                assert item["action"] == expected, "Unexpected live action: " + item["action"] + "; " + json.dumps(item["result"], ensure_ascii=False)
                if expected == "created":
                    progress_id = item["progress_record_id"]
                if expected == "updated":
                    assert item["progress_record_id"] == progress_id, "Update created a duplicate customer"
                    reread = LarkRecordStore(env)
                    try:
                        progress = next(r for r in reread.list_records(PROGRESS_TABLE) if r["record_id"] == progress_id)
                        assert not progress["fields"].get("卡点"), "Resolved blocker was not cleared"
                        assert progress["fields"].get("球在谁方") == "球在合规", "Wrong responsible party"
                    finally:
                        reread.close()
                results.append({"expected": expected, "actual": item["action"], "passed": True})
                print(json.dumps({"live_case": expected, "passed": True}), flush=True)
            finally:
                store.close()
    finally:
        # Delete only acknowledged test mail IDs and new rows with our unique identity.
        for rid in mail_ids:
            api.request("DELETE", f"/open-apis/bitable/v1/apps/{env['LARK_BASE_TOKEN']}/tables/{env['LARK_MAIL_TABLE_ID']}/records/{rid}")
        cleanup = LarkRecordStore(env)
        try:
            for row in cleanup.list_records(PROGRESS_TABLE):
                fields = row["fields"]
                if row["record_id"] not in before_ids and (fields.get("邮件", "").casefold() == address or fields.get("公司名称") == marker):
                    api.request("DELETE", cleanup.path(PROGRESS_TABLE) + "/records/" + row["record_id"])
        finally:
            cleanup.close()
            api.http.session.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise", action="store_true", help="Create temporary rows and call the configured live Qwen model")
    args = parser.parse_args()
    env = read_env(ROOT.parent / ".env")
    guard = LarkRunLock(env["LARK_APP_ID"])
    report = {"identity": "user", "transport": "native HTTPS", "model": env.get("LLM_MODEL"), "verified_at": time.time(),
              "prompt_sha256": hashlib.sha256((ROOT / "prompt_onboarding.txt").read_bytes()).hexdigest()}
    try:
        workbook = ROOT.parent / "outputs/onboarding_01a19f8a/开户自动跟进.xlsx"
        report["before"] = audit_migration(env, workbook)
        assert report["before"]["passed"], "Migration audit failed"
        if args.exercise:
            report["live_ai_cases"] = exercise(env)
        report["after"] = audit_migration(env, workbook)
        assert report["after"]["passed"], "Final audit failed"
        report["passed"] = True
    finally:
        guard.close()
        output = ROOT.parent / "outputs/lark/native_verification.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
