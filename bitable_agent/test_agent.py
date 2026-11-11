"""Offline checks for tool dispatch, validation and the actual tool-result loop."""
import json
import unittest
from unittest.mock import Mock

from .agent import BitableAgent, DeepSeekClient
from .tools import BitableTools


ENV = {"LARK_BASE_TOKEN": "base", "LARK_PROGRESS_TABLE_ID": "tblProgress"}
META = [{"field_name": name, "type": kind} for name, kind in (
    ("公司名称", 1), ("卡点", 1), ("最新收件时间", 5), ("附件", 7), ("公式", 20))]


def call(name, args, identifier="call1"):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.paged.return_value = META
        self.tools = BitableTools(ENV, api=self.api)

    def test_real_dispatch_and_result_continuation(self):
        self.api.request.return_value = {"record": {"record_id": "rec123", "fields": {"卡点": "已补交"}}}
        client = Mock()
        client.complete.side_effect = [
            {"role": "assistant", "content": None, "reasoning_content": "internal",
             "tool_calls": [call("update_record", {"table": "开户进度表", "record_id": "rec123", "fields": {"卡点": "已补交"}})]},
            {"role": "assistant", "content": "已更新 rec123"}]
        result = BitableAgent(client, self.tools).run("修改卡点")
        self.assertTrue(result["tool_calls"][0]["result"]["ok"])
        self.api.request.assert_called_once_with("PUT", "/open-apis/bitable/v1/apps/base/tables/tblProgress/records/rec123",
                                                 body={"fields": {"卡点": "已补交"}})
        messages = client.complete.call_args.args[0]
        self.assertEqual(messages[-1]["tool_call_id"], "call1")
        self.assertTrue(json.loads(messages[-1]["content"])["ok"])
        self.assertEqual(messages[-2]["reasoning_content"], "internal")

    def test_unknown_table_and_path_injection_rejected(self):
        for args in ({"table": "other", "record_id": "rec123"},
                     {"table": "开户进度表", "record_id": "rec123/../../fields"}):
            self.assertFalse(self.tools.execute("delete_record", args)["ok"])
        self.api.request.assert_not_called()

    def test_clear_and_timezone_encoding(self):
        fields = self.tools.encode_fields("开户进度表", {"卡点": "", "最新收件时间": "2026-11-10T09:00:00+08:00", "附件": False})
        self.assertIsNone(fields["卡点"])
        self.assertEqual(fields["最新收件时间"], 1794272400000)
        self.assertIs(fields["附件"], False)

    def test_bad_fields_rejected_before_write(self):
        for fields in ({"不存在": "x"}, {"最新收件时间": "2026-11-10T09:00:00"},
                       {"附件": "false"}, {"公式": None}, {}):
            self.assertFalse(self.tools.execute("update_record", {
                "table": "开户进度表", "record_id": "rec123", "fields": fields})["ok"])
        self.api.request.assert_not_called()

    def test_search_exact_and_pagination(self):
        self.api.request.return_value = {"items": [{"record_id": "rec123", "fields": {
            "公司名称": [{"type": "text", "text": "測試公司"}]}}], "has_more": True, "page_token": "next"}
        result = self.tools.search_records("开户进度表", field="公司名称", value="測試公司", page_token="previous")
        self.assertEqual(result["records"][0]["fields"]["公司名称"], "測試公司")
        self.assertTrue(result["has_more"])
        self.assertEqual(self.api.request.call_args.kwargs["query"]["page_token"], "previous")
        self.assertEqual(self.api.request.call_args.kwargs["body"]["filter"]["conditions"][0]["value"], ["測試公司"])

    def test_create_and_delete_confirmation(self):
        self.api.request.return_value = {"record": {"record_id": "rec123", "fields": {"公司名称": "測試"}}}
        result = self.tools.execute("create_record", {"table": "开户进度表", "fields": {"公司名称": "測試"}})
        self.assertEqual(result["record"]["record_id"], "rec123")
        self.api.request.return_value = {"record_id": "rec123", "deleted": True}
        self.assertTrue(self.tools.execute("delete_record", {"table": "开户进度表", "record_id": "rec123"})["deleted"])
        self.api.request.return_value = {"record_id": "recOther", "deleted": True}
        self.assertFalse(self.tools.execute("delete_record", {"table": "开户进度表", "record_id": "rec123"})["ok"])

    def test_invalid_arguments_return_to_model(self):
        bad_call = call("create_record", {})
        bad_call["function"]["arguments"] = "{bad json"
        client = Mock()
        client.complete.side_effect = [{"role": "assistant", "tool_calls": [bad_call]},
                                       {"role": "assistant", "content": "參數有誤"}]
        result = BitableAgent(client, self.tools).run("新增")
        self.assertFalse(result["tool_calls"][0]["result"]["ok"])
        self.api.request.assert_not_called()

    def test_unknown_tool_and_extra_parameters(self):
        self.assertFalse(self.tools.execute("shell", {"cmd": "anything"})["ok"])
        self.assertFalse(self.tools.execute("list_tables", {"extra": "x"})["ok"])

    def test_round_and_call_limits(self):
        client = Mock()
        client.complete.return_value = {"role": "assistant", "tool_calls": [call("list_tables", {})]}
        with self.assertRaisesRegex(RuntimeError, "round limit"):
            BitableAgent(client, self.tools, max_rounds=1).run("列出表")
        client.complete.return_value["tool_calls"].append(call("list_tables", {}, "call2"))
        with self.assertRaisesRegex(RuntimeError, "call limit"):
            BitableAgent(client, self.tools, max_tool_calls=1).run("列出表")

    def test_deepseek_http_payload_and_incomplete_response(self):
        client = DeepSeekClient({"LLM_API_HOST": "https://api.deepseek.com", "LLM_API_KEY": "dummy",
                                 "LLM_MODEL": "deepseek-flash", "LLM_ENABLE_THINKING": "false"})
        self.addCleanup(client.close)
        client.session = Mock()
        response = client.session.post.return_value
        response.status_code = 200
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}
        self.assertEqual(client.complete([])["content"], "ok")
        payload = client.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["tool_choice"], "auto")
        response.json.return_value["choices"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            client.complete([])


if __name__ == "__main__":
    unittest.main()
