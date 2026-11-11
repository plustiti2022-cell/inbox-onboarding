"""Offline checks for seven-key chat updates, confirmed replies and event deduplication."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from .bot import BotClient, ChatHandler, main
from .tools import BitableTools
from .update import ManualUpdater


def answer(**changes):
    result = {"is_onboarding": True, "公司名称": "ABC公司", "客户名称": "陳先生", "邮件": "chan@example.com",
              "球在谁方": None, "卡点": None, "简要描述": "等待合规审核"}
    result.update(changes)
    return result


def event():
    return {"header": {"app_id": "app", "event_id": "event1"}, "event": {
        "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
        "message": {"chat_type": "p2p", "message_type": "text", "message_id": "om1",
                    "content": json.dumps({"text": "修改ABC公司的摘要"})}}}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tools, self.client = Mock(), Mock()
        self.tools.record = BitableTools.record
        self.tools.path.return_value = "/table"
        self.records = [{"record_id": "rec1", "fields": {
            "公司名称": "ABC公司", "客户名称": "陳先生", "邮件": "chan@example.com"}}]
        self.tools.api.iter_pages.side_effect = lambda *a, **kw: iter([self.records])
        self.client.complete.return_value = {"content": json.dumps(answer())}
        self.updater = ManualUpdater(self.client, self.tools)

    def test_confirmed_update_uses_seven_keys_and_returns_actual_fields(self):
        trace = Mock()
        result = self.updater.run("將ABC公司的摘要改為等待合规审核", on_event=trace)
        self.assertTrue(result.startswith("已更新 ABC公司"))
        self.assertIn("简要描述 → 等待合规审核", result)
        self.assertIn("rec1", result)
        table, record_id, fields = self.tools.update_record.call_args.args
        self.assertEqual((table, record_id), ("开户进度表", "rec1"))
        self.assertEqual(set(fields), {"简要描述", "最新收件时间", "internetMessageId"})
        self.assertEqual(fields["internetMessageId"], "人工修改")
        self.assertEqual(datetime.fromisoformat(fields["最新收件时间"]).utcoffset(), timedelta(hours=8))
        messages = self.client.complete.call_args.args[0]
        content = json.loads(messages[1]["content"])
        self.assertEqual(set(content["公司名称|客户名称|邮件列表"][0]), {"公司名称", "客户名称", "邮件"})
        self.assertTrue(self.client.complete.call_args.kwargs["json_mode"])
        self.assertEqual([item.args[0] for item in trace.call_args_list], [
            "ai_result", "record_matched", "update_prepared", "update_confirmed"])
        self.assertEqual(trace.call_args_list[2].kwargs["fields"], fields)

    def test_explicit_clear_keeps_unspecified_fields(self):
        self.client.complete.return_value = {"content": json.dumps(answer(卡点="", 简要描述=None))}
        self.assertIn("卡点 → 已清空", self.updater.run("清空卡点"))
        fields = self.tools.update_record.call_args.args[2]
        self.assertEqual(fields["卡点"], "")
        self.assertNotIn("简要描述", fields)

    def test_ambiguous_missing_non_onboarding_and_no_changes_never_write(self):
        for model_result, records in (
            (answer(), self.records + [{"record_id": "rec2", "fields": {"公司名称": "ABC公司"}}]),
            (answer(公司名称="other", 客户名称=None, 邮件=None), self.records),
            (answer(is_onboarding=False), self.records),
            (answer(简要描述=None), self.records),
            (answer(公司名称=None, 客户名称=None, 邮件=None), self.records),
        ):
            with self.subTest(result=model_result):
                self.tools.api.iter_pages.side_effect = lambda *a, **kw: iter([records])
                self.client.complete.return_value = {"content": json.dumps(model_result)}
                self.assertTrue(self.updater.run("修改").startswith("未更新"))
                self.tools.update_record.assert_not_called()

    def test_invalid_output_and_unconfirmed_write_never_claim_success(self):
        for output in ("not json", json.dumps({**answer(), "record_id": "rec1"})):
            self.client.complete.return_value = {"content": output}
            self.assertTrue(self.updater.run("修改").startswith("未更新"))
            self.tools.update_record.assert_not_called()
        self.client.complete.return_value = {"content": json.dumps(answer())}
        self.tools.update_record.side_effect = RuntimeError("lost acknowledgement")
        self.assertTrue(self.updater.run("修改").startswith("更新未獲確認"))


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("bitable_agent.bot.emit"))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bot, self.updater = Mock(), Mock()
        self.updater.run.return_value = "已更新 ABC公司：简要描述 → 等待合规审核"
        self.database = Path(self.directory.name) / "messages.sqlite3"
        self.handler = ChatHandler("app", "owner", self.updater, self.bot, self.database)
        self.addCleanup(lambda: self.handler.close())
        self.flush()

    def flush(self):
        self.handler.pool.submit(lambda: None).result(timeout=3)

    def test_receive_reply_and_message_id_deduplication(self):
        payload = event()
        self.handler.receive(payload)
        self.flush()
        payload["header"]["event_id"] = "different-redelivery-id"
        self.handler.receive(payload)
        self.flush()
        self.assertEqual(self.updater.run.call_count, 1)
        self.assertEqual(self.updater.run.call_args.args, ("修改ABC公司的摘要",))
        self.assertTrue(callable(self.updater.run.call_args.kwargs["on_event"]))
        self.bot.reply.assert_called_once_with("om1", self.updater.run.return_value)

    def test_other_users_bots_apps_groups_and_bad_text_are_ignored(self):
        for part, key, value in (("sender", "sender_type", "bot"), ("message", "chat_type", "group"),
                                 ("message", "message_type", "image"), ("message", "content", "invalid")):
            payload = event()
            payload["event"][part][key] = value
            self.handler.receive(payload)
        payload = event()
        payload["event"]["sender"]["sender_id"]["open_id"] = "someone_else"
        self.handler.receive(payload)
        payload = event()
        payload["header"]["app_id"] = "other"
        self.handler.receive(payload)
        self.flush()
        self.updater.run.assert_not_called()
        self.bot.reply.assert_not_called()

    def test_reply_failure_retries_reply_without_replaying_update(self):
        self.bot.reply.side_effect = RuntimeError("network")
        with self.assertLogs("bitable_agent.bot", level="ERROR"):
            self.handler.receive(event())
            self.flush()
        self.bot.reply.side_effect = None
        self.handler.receive(event())
        self.flush()
        self.updater.run.assert_called_once()
        self.assertEqual(self.bot.reply.call_count, 2)
        self.assertEqual(self.handler.db.execute("SELECT state FROM messages").fetchone()[0], "done")

    def test_restart_recovers_only_reply_after_committed_update(self):
        self.handler.db.execute("INSERT INTO messages VALUES ('old', 'reply_pending', '已更新舊記錄')")
        self.handler.db.execute("INSERT INTO messages VALUES ('uncertain', 'processing', NULL)")
        self.handler.db.commit()
        self.handler.close()
        self.handler = ChatHandler("app", "owner", self.updater, self.bot, self.database)
        self.flush()
        self.updater.run.assert_not_called()
        self.assertEqual(self.bot.reply.call_count, 2)
        self.assertTrue(all(row[0] == "done" for row in self.handler.db.execute("SELECT state FROM messages")))

    def test_bot_reply_uses_stable_uuid_and_requires_confirmation(self):
        client = BotClient({})
        self.addCleanup(client.close)
        client.request = Mock(return_value={"data": {"message_id": "reply1"}})
        client.reply("om1", "已更新ABC公司")
        body = copy.deepcopy(client.request.call_args.kwargs["body"])
        client.reply("om1", "已更新ABC公司")
        self.assertEqual(body, client.request.call_args.kwargs["body"])
        self.assertEqual(json.loads(body["content"])["text"], "已更新ABC公司")
        client.request.return_value = {"data": {}}
        with self.assertRaisesRegex(RuntimeError, "confirm"):
            client.reply("om1", "已更新ABC公司")

    def test_advisory_scope_listing_does_not_block_live_listener(self):
        with patch("bitable_agent.bot.PROJECT", Path(self.directory.name)), \
                patch("bitable_agent.bot.read_env", return_value={"LARK_APP_ID": "app"}), \
                patch("bitable_agent.bot.BotClient") as bot, \
                patch("bitable_agent.bot.LarkRunLock"), \
                patch("bitable_agent.bot.BitableTools") as tools, \
                patch("bitable_agent.bot.DeepSeekClient"), \
                patch("bitable_agent.bot.serve", return_value=True) as serve:
            bot.return_value.check.return_value = {"bot_name": "bot", "missing_scopes": ["im:message.p2p_msg:readonly"]}
            tools.return_value.api.request.return_value = {"open_id": "owner"}
            self.assertEqual(main([]), 0)
            serve.assert_called_once()
