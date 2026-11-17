import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from http.client import RemoteDisconnected
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model import MailAnalyzer, ONBOARDING_STATUSES, read_env, validate_result
from storage import MAIL_TABLE, PROGRESS_TABLE, canonical_header, load_mail_input
from workflow import find_customer, process_pending, progress_patch, select_handler


def result(**updates):
    value = dict(is_onboarding=True, 公司名称="North Star Limited", 客户名称="Alice Chen",
                 邮件="alice@northstar.example", 球在谁方="球在我方", 状态=None,
                 简要描述="客户回复资料，等待我方核对。")
    value.update(updates)
    return value


class MemoryStore:
    def __init__(self):
        self.tables = {MAIL_TABLE: [], PROGRESS_TABLE: []}
        self.saves = 0

    def list_records(self, table):
        return copy.deepcopy(self.tables[table])

    def create_record(self, table, fields):
        rec = {"record_id": f"rec{len(self.tables[table]) + 1}", "fields": copy.deepcopy(fields)}
        self.tables[table].append(rec)
        return copy.deepcopy(rec)

    def update_record(self, table, record_id, fields):
        next(r for r in self.tables[table] if r["record_id"] == record_id)["fields"].update(fields)

    def flush(self, preview_dir=None):
        self.saves += 1


class WorkflowTests(unittest.TestCase):
    def test_each_identity_field_matches_independently(self):
        records = [{"record_id": "a", "fields": {"公司名称": "Ｎorth Star Limited", "客户名称": "Alice Chen", "邮件": "ALICE@northstar.example"}}]
        for field, value in (("公司名称", "North Star Limited"), ("客户名称", " alice chen "), ("邮件", "alice@northstar.example")):
            with self.subTest(field=field):
                identity = dict(公司名称="", 客户名称="", 邮件="")
                identity[field] = value
                self.assertEqual(find_customer(records, result(**identity))["record_id"], "a")

    def test_blank_identity_never_matches_blank_rows(self):
        records = [{"record_id": "empty", "fields": {"公司名称": "", "客户名称": "", "邮件": ""}}]
        self.assertIsNone(find_customer(records, result()))
        with self.assertRaisesRegex(ValueError, "no identifiable"):
            find_customer(records, result(公司名称="", 客户名称="", 邮件=""))

    def test_conflicting_matches_leave_progress_intact_and_continue(self):
        store = MemoryStore()
        company = store.create_record(PROGRESS_TABLE, {"公司名称": "North Star Limited"})
        email = store.create_record(PROGRESS_TABLE, {"邮件": "alice@northstar.example"})
        for hour in (1, 2):
            store.create_record(MAIL_TABLE, {"receivedDateTime": f"2026-11-03T0{hour}:00:00Z",
                                             "internetMessageId": f"<{hour}@example.invalid>"})
        analyzer = MailAnalyzer({"LLM_API_KEY": "synthetic-secret", "LLM_API_HOST": "https://example.com/v1",
                                 "LLM_MODEL": "synthetic-model"}, {},
                                Path(__file__).resolve().parents[1] / "prompt_onboarding.txt")
        with patch.object(analyzer, "analyze", side_effect=[result(), result(is_onboarding=False)]) as analyze:
            report = process_pending(store, analyzer)
            self.assertEqual((report["manual_review"], report["non_onboarding"], report["failed"]), (1, 1, 0))
            self.assertEqual(process_pending(store, analyzer)["processed"], 0)
            self.assertEqual(analyze.call_count, 2)
        self.assertEqual(store.list_records(PROGRESS_TABLE), [company, email])
        first, second = [row["fields"] for row in store.list_records(MAIL_TABLE)]
        self.assertEqual((first["AI处理"], second["AI处理"]), ("人工核查", "AI已处理"))
        for match in (company, email):
            self.assertIn(match["record_id"], first["处理说明"])

    def test_or_matches_conflicting_rows_without_picking_first(self):
        records = [{"record_id": "a", "fields": {"公司名称": "North Star Limited"}},
                   {"record_id": "b", "fields": {"邮件": "alice@northstar.example"}}]
        with self.assertRaisesRegex(ValueError, "conflicts"):
            find_customer(records, result())

    def test_absent_fields_are_not_cleared(self):
        update = progress_patch(result())
        self.assertNotIn("状态", update)
        self.assertNotIn("卡点", update)
        self.assertNotIn("经办人", update)
        self.assertEqual(progress_patch(result(状态="已通知客户补件"))["状态"], "已通知客户补件")

    def test_identity_patch_only_writes_new_information(self):
        current = {"公司名称": "Ｎorth Star Limited", "客户名称": "Alice Chen", "邮件": "ALICE@northstar.example"}
        unchanged = progress_patch(result(), current)
        self.assertTrue(set(current).isdisjoint(unchanged))
        missing = progress_patch(result(公司名称=None, 客户名称="", 邮件=None), current)
        self.assertTrue(set(current).isdisjoint(missing))
        changed = progress_patch(result(客户名称="Alice Zhang", 邮件="alice.zhang@northstar.example"), current)
        self.assertNotIn("公司名称", changed)
        self.assertEqual(changed["客户名称"], "Alice Zhang")
        self.assertEqual(changed["邮件"], "alice.zhang@northstar.example")

    def test_handler_priority_uses_first_matching_header_and_exact_email(self):
        cases = (
            ({"from.emailAddress.address": " JAMES.YANG@HASHKEY.COM ",
              "toRecipients.addresses": "prance.wang@hashkey.com"}, "James.Yang"),
            ({"toRecipients.addresses": "james.yang@hashkey.com, KATHY.SUN@hashkey.com, bruce.li@hashkey.com",
              "ccRecipients.addresses": "fery.li@hashkey.com"}, "Kathy.Sun"),
            ({"from.emailAddress.address": "fery.li@hashkey.com.example",
              "ccRecipients.addresses": "meilin.wu@hashkey.com"}, "Meilin.Wu"),
            ({"toRecipients.addresses": None, "bccRecipients.addresses": "bruce.li@hashkey.com"}, "James.Yang"),
        )
        for mail, expected in cases:
            with self.subTest(mail=mail):
                self.assertEqual(select_handler(mail), expected)

    def test_chronological_create_update_negative_and_identity_only_context(self):
        store = MemoryStore()
        participants = {
            1: {"from.emailAddress.address": "fery.li@hashkey.com"},
            2: {"ccRecipients.addresses": "james.yang@hashkey.com, prance.wang@hashkey.com"},
            3: {"from.emailAddress.address": "bruce.li@hashkey.com"},
        }
        for hour in (3, 2, 1):
            store.create_record(MAIL_TABLE, {"receivedDateTime": f"2026-11-03T0{hour}:00:00Z", "AI处理": "AI未完成",
                                             "subject": str(hour), "internetMessageId": f"<{hour}@example.invalid>",
                                             **participants[hour]})
        seen = []
        now = datetime.fromisoformat("2026-11-10T15:12:34+08:00")

        class Analyzer:
            def analyze(self, mail, known):
                seen.append(mail["subject"])
                for customer in known:
                    self_keys = set(customer)
                    if self_keys != {"公司名称", "客户名称", "邮件"}:
                        raise AssertionError("Historical progress was sent to the model")
                if mail["subject"] == "1":
                    return result(状态="已通知客户补件")
                if mail["subject"] == "2":
                    created = store.list_records(PROGRESS_TABLE)[0]["fields"]
                    self_test.assertEqual(created["internetMessageId"], "<1@example.invalid>")
                    self_test.assertEqual(created["最新收件时间"], now.isoformat())
                    self_test.assertEqual(created["经办人"], "Fery.Li")
                    return result(简要描述="请查收附件。")
                return result(is_onboarding=False, 公司名称="", 客户名称="", 邮件="", 球在谁方="", 简要描述=None)

        self_test = self
        with patch("workflow.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            report = process_pending(store, Analyzer())
        self.assertEqual(seen, ["1", "2", "3"])
        self.assertEqual((report["created"], report["updated"], report["non_onboarding"]), (1, 1, 1))
        self.assertEqual(set(report["items"][0]["result"]), {
            "is_onboarding", "公司名称", "客户名称", "邮件", "球在谁方", "状态", "简要描述"})
        progress = store.list_records(PROGRESS_TABLE)[0]["fields"]
        self.assertEqual(progress["状态"], "已通知客户补件")
        self.assertEqual(progress["经办人"], "Fery.Li")
        self.assertEqual(progress["简要描述"], "请查收附件。")
        self.assertEqual(progress["internetMessageId"], "<2@example.invalid>")
        self.assertEqual(progress["最新收件时间"], now.isoformat())
        self.assertTrue(all(r["fields"]["AI处理"] == "AI已处理" for r in store.list_records(MAIL_TABLE)))
        self.assertEqual(process_pending(store, Analyzer())["processed"], 0)

    def test_failure_is_retryable_not_marked_complete(self):
        store = MemoryStore()
        store.create_record(MAIL_TABLE, {"receivedDateTime": "2026-11-03T00:00:00Z", "AI处理": "AI未完成"})

        class Broken:
            def analyze(self, mail, known):
                raise RuntimeError("LLM network error or timeout")

        self.assertEqual(process_pending(store, Broken())["failed"], 1)
        self.assertEqual(store.list_records(MAIL_TABLE)[0]["fields"]["AI处理"], "AI失败")
        self.assertEqual(store.list_records(PROGRESS_TABLE), [])

    def test_mail_update_replaces_agent_marker_and_keeps_unspecified_fields(self):
        store = MemoryStore()
        store.create_record(MAIL_TABLE, {"receivedDateTime": "2026-11-03T00:00:00Z", "AI处理": "AI未完成",
                                         "internetMessageId": "<source@example.invalid>"})
        original = store.create_record(PROGRESS_TABLE, {
            "公司名称": "North Star Limited", "客户名称": "Alice Chen", "邮件": "alice@northstar.example",
            "状态": "已通知客户补件", "卡点": "缺地址证明", "经办人": "Jx.Yao", "internetMessageId": "人工修改"})

        class Analyzer:
            def analyze(self, mail, known):
                return result(公司名称=None, 客户名称=None, 简要描述=None)

        before = datetime.now().astimezone()
        with patch.object(store, "update_record", wraps=store.update_record) as write:
            report = process_pending(store, Analyzer())
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["updated"], 1)
        table, record_id, fields = write.call_args_list[0].args
        self.assertEqual((table, record_id), (PROGRESS_TABLE, original["record_id"]))
        self.assertTrue({"公司名称", "客户名称", "邮件", "简要描述", "状态", "卡点", "经办人"}.isdisjoint(fields))
        progress = store.list_records(PROGRESS_TABLE)[0]["fields"]
        self.assertEqual(progress["状态"], "已通知客户补件")
        self.assertEqual(progress["卡点"], "缺地址证明")
        self.assertEqual(progress["经办人"], "Jx.Yao")
        self.assertEqual(progress["internetMessageId"], "<source@example.invalid>")
        updated = datetime.fromisoformat(progress["最新收件时间"])
        self.assertEqual(updated.utcoffset(), timedelta(hours=8))
        self.assertLess(abs((updated - before).total_seconds()), 5)

    def test_missing_source_id_fails_without_writing_progress(self):
        store = MemoryStore()
        store.create_record(MAIL_TABLE, {"receivedDateTime": "2026-11-03T00:00:00Z", "AI处理": "AI未完成"})
        original = store.create_record(PROGRESS_TABLE, {
            "公司名称": "North Star Limited", "internetMessageId": "<prior@example.invalid>"})

        class Analyzer:
            def analyze(self, mail, known):
                return result()

        self.assertEqual(process_pending(store, Analyzer())["failed"], 1)
        self.assertEqual(store.list_records(PROGRESS_TABLE), [original])
        mail = store.list_records(MAIL_TABLE)[0]["fields"]
        self.assertEqual(mail["AI处理"], "AI失败")
        self.assertIn("missing internetMessageId", mail["处理说明"])

    def test_failure_stops_later_mail_and_resume_preserves_chronology(self):
        store = MemoryStore()
        for hour in (2, 1):
            store.create_record(MAIL_TABLE, {"receivedDateTime": f"2026-11-03T0{hour}:00:00Z",
                                           "AI处理": "AI未完成", "subject": str(hour),
                                           "internetMessageId": f"<{hour}@example.invalid>"})
        attempted = []

        class Broken:
            def analyze(self, mail, known):
                attempted.append(mail["subject"])
                raise RuntimeError("LLM network error or timeout")

        first = process_pending(store, Broken())
        self.assertEqual(first["failed"], 1)
        self.assertEqual(attempted, ["1"])
        records = {r["fields"]["subject"]: r["fields"] for r in store.list_records(MAIL_TABLE)}
        self.assertEqual(records["1"]["AI处理"], "AI失败")
        self.assertEqual(records["2"]["AI处理"], "AI未完成")

        class Working:
            def analyze(self, mail, known):
                attempted.append(mail["subject"])
                return result(简要描述="进展" + mail["subject"])

        retried = process_pending(store, Working())
        self.assertEqual(attempted, ["1", "1", "2"])
        self.assertEqual((retried["created"], retried["updated"]), (1, 1))
        self.assertEqual(store.list_records(PROGRESS_TABLE)[0]["fields"]["简要描述"], "进展2")

    def test_model_schema_rejects_bad_types_extra_fields_and_invalid_values(self):
        for bad in (result(is_onboarding="true"), result(blocker_resolved=True), result(经办人="someone"),
                    result(邮件="a@b.example,c@d.example"), result(卡点=None), result(状态=""),
                    result(状态="待人工确认"), result(球在谁方="未知"),
                    {key: value for key, value in result().items() if key != "公司名称"}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                validate_result(bad)

    def test_model_schema_accepts_the_prompt_labels_and_null(self):
        prompt_path = Path(__file__).resolve().parents[1] / "prompt_onboarding.txt"
        prompt = prompt_path.read_text(encoding="utf-8")
        for status in ONBOARDING_STATUSES:
            self.assertIn(f"- {status}：", prompt)
            self.assertEqual(validate_result(result(状态=status))["状态"], status)
        self.assertIsNone(validate_result(result(状态=None))["状态"])

    def test_model_redacts_access_codes_in_its_request(self):
        analyzer = MailAnalyzer({"LLM_API_KEY": "synthetic-secret", "LLM_API_HOST": "https://example.com/v1",
                                 "LLM_MODEL": "synthetic-model"}, {},
                                Path(__file__).resolve().parents[1] / "prompt_onboarding.txt")
        reply = {"choices": [{"message": {"content": json.dumps(result())}}]}
        code = "2F94AB170000ABCD1234567890ABCDEF"
        mail = {"subject": "查看通知，验证码：529184",
                "body.content（去除html）": f"Enter the security code:\n{code}\nDocument viewed."}
        with patch("model.urlopen", return_value=io.BytesIO(json.dumps(reply).encode())) as send:
            analyzer.analyze(mail, [])
        payload = json.loads(send.call_args.args[0].data)
        content = json.loads(payload["messages"][1]["content"])
        self.assertEqual(content["邮件主题"], "查看通知，验证码：[访问码已移除]")
        self.assertEqual(content["邮件正文"], "Enter the security code:\n[访问码已移除]\nDocument viewed.")
        self.assertIn(code, mail["body.content（去除html）"])

    def test_model_distinguishes_gateway_auth_from_upstream_key_errors(self):
        analyzer = MailAnalyzer({"LLM_API_KEY": "synthetic-secret", "LLM_API_HOST": "https://example.com/v1",
                                 "LLM_MODEL": "synthetic-model"}, {},
                                Path(__file__).resolve().parents[1] / "prompt_onboarding.txt")
        cases = [(401, "invalid_api_key", "Invalid API key", "configured API key was rejected"),
                 (400, "bad_response_status_code", "Incorrect API key provided", "provider model route")]
        for status, code, message, expected in cases:
            with self.subTest(code=code):
                body = json.dumps({"error": {"code": code, "message": message}}).encode()
                error = HTTPError(analyzer.url, status, "synthetic", {}, io.BytesIO(body))
                with patch("model.urlopen", side_effect=error):
                    with self.assertRaisesRegex(RuntimeError, expected) as raised:
                        analyzer.analyze({}, [])
                self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_model_sends_thinking_option_only_when_configured(self):
        env = {"LLM_API_KEY": "synthetic-secret", "LLM_API_HOST": "https://example.com/v1",
               "LLM_MODEL": "qwen3.8-max"}
        prompt = Path(__file__).resolve().parents[1] / "prompt_onboarding.txt"
        for setting, expected in (("", None), ("false", False), ("true", True)):
            with self.subTest(setting=setting):
                analyzer = MailAnalyzer({**env, "LLM_ENABLE_THINKING": setting}, {}, prompt)
                response = {"choices": [{"message": {"content": json.dumps(result())}}]}
                with patch("model.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as request:
                    analyzer.analyze({}, [])
                payload = json.loads(request.call_args.args[0].data)
                if expected is None:
                    self.assertNotIn("enable_thinking", payload)
                else:
                    self.assertIs(payload["enable_thinking"], expected)

    def test_original_recipient_expression_headers(self):
        self.assertEqual(canonical_header("{{ (.item.json.value[0].ccRecipients ?? []).map(r => r.emailAddress?.address).filter(Boolean).join(', ') }}"), "ccRecipients.addresses")
        self.assertEqual(canonical_header("{{ (.item.json.value[0].bccRecipients ?? []).map(r => r.emailAddress?.address).filter(Boolean).join(', ') }}"), "bccRecipients.addresses")

    def test_model_remote_disconnect_marks_failed_and_stops_later_mail(self):
        analyzer = MailAnalyzer({"LLM_API_KEY": "synthetic-secret", "LLM_API_HOST": "https://example.com/v1",
                                 "LLM_MODEL": "synthetic-model"}, {},
                                Path(__file__).resolve().parents[1] / "prompt_onboarding.txt")
        store = MemoryStore()
        for hour in (1, 2):
            store.create_record(MAIL_TABLE, {"receivedDateTime": f"2026-11-03T0{hour}:00:00Z", "AI处理": "AI未完成"})
        with patch("model.urlopen", side_effect=RemoteDisconnected("synthetic disconnect")) as request:
            report = process_pending(store, analyzer)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual([r["fields"]["AI处理"] for r in store.list_records(MAIL_TABLE)], ["AI失败", "AI未完成"])

    def test_export_input_loads_all_seventeen_fields(self):
        from fetch_mail import HEADERS

        values = [f"synthetic-{index}" for index in range(len(HEADERS))]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mapped_rows.json"
            source.write_text(json.dumps({"headers": HEADERS, "rows": [values]}), encoding="utf-8")
            rows = load_mail_input(source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bccRecipients.addresses"], values[12])
        self.assertTrue(all(rows[0][canonical_header(header)] == value for header, value in zip(HEADERS, values)))

    def test_env_quoted_values_and_no_shell_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('LLM_API_KEY="literal$(`value)#with-hash" # comment\nLLM_MODEL=test # comment\n', encoding="utf-8")
            self.assertEqual(read_env(path), {"LLM_API_KEY": "literal$(`value)#with-hash", "LLM_MODEL": "test"})

    def test_graph_sort_across_multiple_pages(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mail_export_source"))
        from export_mail_last24h import GraphClient, parse_time
        from urllib.parse import parse_qs, urlsplit
        client = GraphClient({"MICROSOFT_GRAPH_USER_ID": "test@example.com"})
        pages = [{"value": [{"receivedDateTime": "2026-11-03T02:00:00Z"}], "@odata.nextLink": "https://graph.microsoft.com/next"},
                 {"value": [{"receivedDateTime": "2026-11-03T01:00:00Z"}]}]
        requests = []

        def request(req):
            requests.append(req.full_url)
            return pages.pop(0)

        with patch.object(client, "authenticate"), patch.object(client, "request", side_effect=request):
            messages, count = client.fetch(parse_time("2026-11-02T00:00:00Z"), parse_time("2026-11-04T00:00:00Z"))
        self.assertEqual(parse_qs(urlsplit(requests[0]).query)["$orderby"], ["receivedDateTime asc"])
        self.assertEqual([m["receivedDateTime"] for m in messages], ["2026-11-03T01:00:00Z", "2026-11-03T02:00:00Z"])
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
