import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_lark import Cloud, ENV
from test_workflow import result

import fetch_mail
import process_mail
from lark_store import LarkRecordStore
from lark_support import LarkError
from storage import MAIL_TABLE


GRAPH_CONFIG = {
    "MICROSOFT_GRAPH_TENANT_ID": "test-tenant", "MICROSOFT_GRAPH_USER_ID": "test-mailbox",
    "MICROSOFT_GRAPH_CLIENT_ID": "test-client", "MICROSOFT_GRAPH_CLIENT_SECRET": "test-secret",
}
STARTED = datetime.fromisoformat("2026-11-04T07:05:23.123456+08:00")


def message(name, received=STARTED - timedelta(minutes=10)):
    return {"internetMessageId": f"<{name}@example.invalid>", "receivedDateTime": received.isoformat(),
            "subject": name, "hasAttachments": False}


class WindowsJobTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_path = self.root / ".env"
        self.env_path.write_text("\n".join(f"{k}={v}" for k, v in {**ENV, **GRAPH_CONFIG}.items()), encoding="utf-8")
        self.cloud = Cloud()
        self.cloud.http = SimpleNamespace(session=SimpleNamespace(close=lambda: None))
        self.graph = self.enterContext(patch("fetch_mail.GraphClient")).return_value
        self.enterContext(patch("fetch_mail.PROJECT", self.root))
        self.enterContext(patch("run_onboarding.PROJECT", self.root))
        self.enterContext(patch("lark_store.LarkAPI", return_value=self.cloud))
        self.enterContext(patch("lark_support.LarkRunLock", side_effect=AssertionError("Jobs must not take the workflow lock")))
        self.checkpoint = fetch_mail.checkpoint_path(GRAPH_CONFIG, ENV)

    def test_first_window_delayed_start_and_missed_hours(self):
        self.graph.fetch.return_value = ([], 1)
        fetch_mail.fetch_once(self.env_path, STARTED)
        self.graph.fetch.assert_called_with(STARTED - timedelta(hours=24), STARTED)
        delayed = STARTED + timedelta(hours=1)
        fetch_mail.fetch_once(self.env_path, delayed)
        self.graph.fetch.assert_called_with(STARTED.replace(minute=0, second=0, microsecond=0), delayed)
        after_missed_runs = STARTED + timedelta(hours=5)
        fetch_mail.fetch_once(self.env_path, after_missed_runs)
        self.graph.fetch.assert_called_with(delayed, after_missed_runs)

    def test_graph_error_keeps_first_window_anchor(self):
        self.graph.fetch.side_effect = RuntimeError("Graph pagination failed")
        with self.assertRaisesRegex(RuntimeError, "pagination failed"):
            fetch_mail.fetch_once(self.env_path, STARTED)
        self.assertIsNone(json.loads(self.checkpoint.read_text())["last_success_end_utc"])
        self.assertFalse(self.cloud.rows["mail"])
        self.graph.fetch.side_effect = None
        self.graph.fetch.return_value = ([], 2)
        retry = STARTED + timedelta(hours=1)
        fetch_mail.fetch_once(self.env_path, retry)
        self.graph.fetch.assert_called_with(STARTED - timedelta(hours=24), retry)

    def test_dedup_and_partial_write_retry_preserve_status_and_checkpoint(self):
        self.graph.fetch.return_value = ([message("existing")], 1)
        fetch_mail.fetch_once(self.env_path, STARTED)
        fields = self.cloud.rows["mail"][0]["fields"]
        fields.update({"AI处理": "AI已处理", "处理说明": "keep"})
        original_checkpoint = self.checkpoint.read_bytes()

        request = self.cloud.request

        def lose_second_ack(method, path, body=None, query=None):
            response = request(method, path, body, query)
            if method == "POST" and body["fields"]["subject"] == "second":
                raise LarkError("Write acknowledgement was lost")
            return response

        self.graph.fetch.return_value = ([message("existing"), message("first"), message("first"), message("second")], 2)
        retry = STARTED + timedelta(hours=1)
        with patch.object(self.cloud, "request", side_effect=lose_second_ack):
            with self.assertRaisesRegex(LarkError, "acknowledgement"):
                fetch_mail.fetch_once(self.env_path, retry)
        self.assertEqual(self.checkpoint.read_bytes(), original_checkpoint)
        self.assertEqual(len(self.cloud.rows["mail"]), 3)

        self.graph.fetch.return_value[0].append(message("third"))
        summary = fetch_mail.fetch_once(self.env_path, retry + timedelta(minutes=1))
        self.assertEqual((summary["imported"], summary["skipped_existing_or_duplicate"]), (1, 4))
        self.assertEqual((fields["AI处理"], fields["处理说明"]), ("AI已处理", "keep"))
        self.assertEqual(len(self.cloud.rows["mail"]), 4)

    def test_two_entrypoints_overlap_and_ai_keeps_its_snapshot(self):
        with patch.object(self.cloud, "paged", wraps=self.cloud.paged) as read:
            self.graph.fetch.return_value = ([message("existing")], 1)
            self.assertEqual(fetch_mail.main(["--env", str(self.env_path)]), 0)
            self.assertTrue(all("/tables/mail/" in call.args[0] for call in read.call_args_list))

        graph_started, finish_graph = threading.Event(), threading.Event()

        def slow_graph(start, end):
            graph_started.set()
            if not finish_graph.wait(5):
                raise RuntimeError("Graph was not released by the independent AI run")
            return [message("later")], 1

        self.graph.fetch.side_effect = slow_graph
        with ThreadPoolExecutor(max_workers=1) as pool:
            graph_run = pool.submit(fetch_mail.main, ["--env", str(self.env_path)])
            try:
                self.assertTrue(graph_started.wait(5))
                with patch("run_onboarding.MailAnalyzer") as analyzer:
                    instance = analyzer.return_value
                    instance.model = "test-model"

                    def analyze(mail, known):
                        self.assertEqual(mail["subject"], "existing")
                        self.assertFalse(graph_run.done())
                        finish_graph.set()
                        self.assertEqual(graph_run.result(timeout=5), 0)
                        return result()

                    instance.analyze.side_effect = analyze
                    self.assertEqual(process_mail.main(["--env", str(self.env_path)]), 0)
                    instance.analyze.assert_called_once()
                    reports = list((self.root / "outputs/lark/runs").glob("*/run_report.json"))
                    self.assertEqual(len(reports), 1)
                    report = json.loads(reports[0].read_text(encoding="utf-8"))
                    self.assertNotIn("items", report)
                    self.assertEqual(report["processed"], 1)
                    details = reports[0].with_name("results.jsonl").read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(details), 1)
                    self.assertEqual(json.loads(details[0])["action"], "created")
            finally:
                finish_graph.set()

        fresh = LarkRecordStore(ENV, api=self.cloud)
        self.assertEqual({row["fields"]["subject"]: row["fields"]["AI处理"] for row in fresh.list_records(MAIL_TABLE)},
                         {"existing": "AI已处理", "later": "AI未完成"})
        self.assertEqual(len(self.cloud.rows["progress"]), 1)
