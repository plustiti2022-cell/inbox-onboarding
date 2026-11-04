import gc
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_lark import Cloud, ENV
from test_workflow import result
from lark_store import LarkRecordStore
from lark_support import LarkAPI, LarkError
from storage import MAIL_TABLE, PROGRESS_TABLE
from workflow import mail_time, process_pending


class MemoryTests(unittest.TestCase):
    def cloud(self):
        cloud = Cloud()
        cloud.http = SimpleNamespace(session=SimpleNamespace(close=Mock()))
        return cloud

    def test_disk_queue_orders_pages_skips_done_and_defers_new_mail(self):
        cloud = self.cloud()
        original = LarkRecordStore(ENV, api=cloud)
        for number in reversed(range(105)):
            original.create_record(MAIL_TABLE, {
                "receivedDateTime": (datetime(2026, 11, 1, tzinfo=timezone.utc)
                                     + timedelta(minutes=number)).isoformat(),
                "internetMessageId": f"<{number}@test.invalid>", "subject": str(number),
                "AI处理": "AI已处理" if number == 0 else ("AI失败" if number == 1 else ""),
                "body.content（去除html）": "synthetic body" * 1000,
            })
        with patch.object(cloud, "iter_pages", wraps=cloud.iter_pages) as pages:
            store = LarkRecordStore(ENV, api=cloud, disk_backed=True)
            self.assertTrue(all(c.kwargs["query"]["page_size"] == 50 for c in pages.call_args_list))
        path = store.snapshot.path
        self.addCleanup(store.close)
        seen, events, summary = [], [], {}

        def analyze(mail, known):
            seen.append(int(mail["subject"]))
            if len(seen) == 1:
                original.create_record(MAIL_TABLE, {
                    "receivedDateTime": "2026-11-01T00:00:00Z", "subject": "late",
                    "internetMessageId": "<late@test.invalid>"})
                self.assertEqual(known, [])
            else:
                self.assertEqual(len(known), 1)
                self.assertEqual(set(known[0]), {"公司名称", "客户名称", "邮件"})
            return result(summary=mail["subject"])

        # Production must not secretly reconstruct either full table.
        with patch.object(store, "list_records", side_effect=AssertionError("full-table copy")):
            report = process_pending(store, SimpleNamespace(analyze=analyze), on_event=events.append,
                                     retain_items=False, report=summary)
        self.assertIs(report, summary)
        self.assertNotIn("items", report)
        self.assertEqual(seen, list(range(1, 105)))
        self.assertEqual((report["processed"], report["created"], report["updated"]), (104, 1, 103))
        self.assertEqual(store.record_counts(), {"mail_records": 105, "progress_records": 1, "remaining": 0})
        self.assertEqual(len([e for e in events if e["state"] == "finished"]), 104)
        self.assertEqual(cloud.rows["progress"][0]["fields"]["简要描述"], "104")
        self.assertNotEqual(cloud.rows["mail"][-1]["fields"].get("AI处理"), "AI已处理")
        store.close()
        self.assertFalse(path.exists())

    def test_failure_stops_stream_and_summary_keeps_prior_counts(self):
        cloud = self.cloud()
        original = LarkRecordStore(ENV, api=cloud)
        for number in (1, 2, 3):
            original.create_record(MAIL_TABLE, {"receivedDateTime": f"2026-11-01T00:0{number}:00Z"})
        store = LarkRecordStore(ENV, api=cloud, disk_backed=True)
        self.addCleanup(store.close)
        analyzer = Mock()
        analyzer.analyze.side_effect = [result(is_onboarding=False), RuntimeError("synthetic failure")]
        events = []
        report = process_pending(store, analyzer, on_event=events.append, retain_items=False)
        self.assertEqual((report["processed"], report["failed"]), (1, 1))
        self.assertEqual(analyzer.analyze.call_count, 2)
        self.assertEqual(events[-1]["error"], "synthetic failure")
        self.assertEqual(store.record_counts()["remaining"], 2)
        self.assertEqual(store.tables[MAIL_TABLE].pending_snapshot(mail_time, 1)[0], 1)

    def test_invalid_pending_time_prevents_all_processing(self):
        cloud = self.cloud()
        original = LarkRecordStore(ENV, api=cloud)
        original.create_record(MAIL_TABLE, {"receivedDateTime": "2026-11-01T00:00:00Z"})
        original.create_record(MAIL_TABLE, {})
        store = LarkRecordStore(ENV, api=cloud, disk_backed=True)
        path = store.snapshot.path
        try:
            analyzer = Mock()
            with self.assertRaisesRegex(ValueError, "missing receivedDateTime"):
                process_pending(store, analyzer, retain_items=False)
            analyzer.analyze.assert_not_called()
        finally:
            store.close()
        self.assertFalse(path.exists())

    def test_pagination_failure_happens_before_ai_and_cleans_snapshot(self):
        cloud = self.cloud()

        def fail(path, query=None):
            yield [{"record_id": "r1", "fields": {}}]
            raise LarkError("page failed")

        from disk_snapshot import DiskSnapshot
        snapshots = []
        def create():
            snapshot = DiskSnapshot()
            snapshots.append(snapshot.path)
            return snapshot

        with patch.object(cloud, "iter_pages", side_effect=fail), patch("lark_store.DiskSnapshot", side_effect=create):
            with self.assertRaisesRegex(LarkError, "page failed"):
                LarkRecordStore(ENV, api=cloud, disk_backed=True)
        self.assertTrue(snapshots)
        self.assertFalse(snapshots[0].exists())
        cloud.http.session.close.assert_called_once()

    def test_stream_pagination_keeps_retry_query_and_rejects_cycle(self):
        api = LarkAPI(ENV, auth=Mock(), http=Mock())
        api.request = Mock(side_effect=[
            {"items": [1], "has_more": True, "page_token": "next"},
            {"items": [2], "has_more": True, "page_token": "next"},
        ])
        pages = api.iter_pages("/records", query={"page_size": 50})
        self.assertEqual(next(pages), [1])
        self.assertEqual(api.request.call_count, 1)
        self.assertEqual(next(pages), [2])
        self.assertEqual(api.request.call_args.kwargs["query"], {"page_size": 50, "page_token": "next"})
        with self.assertRaisesRegex(LarkError, "pagination token"):
            next(pages)

    def test_large_body_snapshot_memory_does_not_grow_with_mail_count(self):
        cloud = self.cloud()

        def measure(count):
            def pages(path, query=None):
                if "/tables/progress/" in path:
                    return
                size = query["page_size"]
                for offset in range(0, count, size):
                    yield [{"record_id": f"r{i}", "fields": {
                        "receivedDateTime": 1793491200000 + i * 1000,
                        "subject": str(i), "body.content（去除html）": str(i) + "x" * 16384}}
                        for i in range(offset, min(offset + size, count))]
            gc.collect()
            tracemalloc.start()
            try:
                with patch.object(cloud, "iter_pages", side_effect=pages):
                    store = LarkRecordStore(ENV, api=cloud, disk_backed=True)
                try:
                    total, records = store.pending_snapshot()
                    self.assertEqual(total, count)
                    self.assertEqual(sum(1 for _ in records), count)
                    return tracemalloc.get_traced_memory()[1]
                finally:
                    store.close()
            finally:
                tracemalloc.stop()

        small, large = measure(100), measure(1500)
        # 25 MB of source bodies must not cause a comparable Python heap increase.
        self.assertLess(large, small + 2 * 1024 * 1024)
        print(f"Snapshot Python heap peak: 100 mails={small / 1048576:.2f} MiB; "
              f"1500 mails={large / 1048576:.2f} MiB")


if __name__ == "__main__":
    unittest.main()
