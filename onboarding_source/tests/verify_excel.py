"""Real writer/save/reopen verification using synthetic data, no model calls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_onboarding import runtime_paths
from storage import ExcelRecordStore, MAIL_FIELDS, MAIL_TABLE, PROGRESS_TABLE, load_mail_input, read_xlsx
from workflow import process_pending
from test_workflow import result


def verify(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    workbook = directory / "synthetic_workflow.xlsx"
    if workbook.exists():
        raise ValueError("Choose a new output directory for verification")
    node, modules = runtime_paths(None, None)
    store = ExcelRecordStore(workbook, node, modules)
    for hour in (3, 1, 2):
        fields = dict.fromkeys(MAIL_FIELDS, "")
        fields.update({"internetMessageId": f"<synthetic-{hour}@example.com>",
                       "receivedDateTime": f"2026-11-03T0{hour}:00:00Z", "AI处理": "AI未完成", "subject": str(hour),
                       "hasAttachments": True, "body.content（去除html）": "=本次邮件包含中文、换行\n及 literal text"})
        store.create_record(MAIL_TABLE, fields)

    class Analyzer:
        def analyze(self, mail, known):
            if mail["subject"] == "1":
                return result(卡点="缺地址证明")
            if mail["subject"] == "2":
                # Only name matches: company/email were absent, never clear them.
                return result(公司名称="", 邮件="", 简要描述="附件已提交，待审核。")
            return result(is_onboarding=False, 公司名称=None, 客户名称=None, 邮件=None, 球在谁方=None, 简要描述=None)

    try:
        report = process_pending(store, Analyzer())
        store.flush(directory / "previews")
        before = store.list_records(PROGRESS_TABLE)
    finally:
        store.close()
    reopened = ExcelRecordStore(workbook, node, modules)
    try:
        after = reopened.list_records(PROGRESS_TABLE)
        assert before[0]["record_id"] == after[0]["record_id"]
        assert len(after) == 1
        fields = after[0]["fields"]
        assert fields["公司名称"] == "North Star Limited" and fields["邮件"] == "alice@northstar.example"
        assert fields["卡点"] == "缺地址证明" and not fields["经办人"]
        assert fields["internetMessageId"] == "人工修改"
        assert fields["简要描述"] == "附件已提交，待审核。"
        mails = reopened.list_records(MAIL_TABLE)
        assert all(r["fields"]["AI处理"] == "AI已处理" for r in mails)
        assert all(r["fields"]["hasAttachments"] is True for r in mails)
        assert all(r["fields"]["body.content（去除html）"].startswith("=本次邮件") for r in mails)
        assert all(r["fields"]["receivedDateTime"].endswith("Z") for r in mails)
        assert process_pending(reopened, Analyzer())["processed"] == 0
        # Persist an explicit resolution and verify it clears, not just null/absent.
        reopened.create_record(MAIL_TABLE, {"receivedDateTime": "2026-11-03T04:00:00Z", "AI处理": "AI未完成",
                                           "internetMessageId": "<synthetic-4@example.com>"})

        class Resolved:
            def analyze(self, mail, known):
                return result(卡点="", 简要描述="审核明确确认地址证明通过。")

        process_pending(reopened, Resolved())
    finally:
        reopened.close()
    saved = ExcelRecordStore(workbook, node, modules)
    try:
        assert saved.list_records(PROGRESS_TABLE)[0]["fields"]["卡点"] == ""
        assert saved.list_records(PROGRESS_TABLE)[0]["record_id"] == before[0]["record_id"]
    finally:
        saved.close()
    source = load_mail_input(workbook)
    assert len(source) == 4
    outcome = {"passed": True, "first_run": {k: v for k, v in report.items() if k != "items"},
               "verified": ["two tables", "create/update/negative", "name-only OR matching", "stable record IDs", "preserve old blocker and handler", "manual source marker", "explicit resolution", "literal equals text", "booleans", "UTC dates", "resume", "XLSX mail input"]}
    (directory / "verification.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == "__main__":
    verify(sys.argv[1])
