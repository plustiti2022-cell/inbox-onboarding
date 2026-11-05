import copy
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lark_support import LarkAPI, LarkAuth, LarkError, LarkHTTP, LarkRunLock, SecureJSON
from lark_store import LarkRecordStore, field_schema
from run_lark_listener import should_wake
from storage import MAIL_TABLE, PROGRESS_TABLE
from workflow import process_pending
from test_workflow import result

ENV = {"LARK_APP_ID": "synthetic-app", "LARK_APP_SECRET": "synthetic-secret", "LARK_BASE_TOKEN": "base",
       "LARK_MAIL_TABLE_ID": "mail", "LARK_PROGRESS_TABLE_ID": "progress"}


class Cloud:
    def __init__(self):
        self.rows = {"mail": [], "progress": []}
        self.created = 0
        self.fail_status = False
        self.unknown_create = False

    def paged(self, path, query=None):
        table = path.split("/tables/")[1].split("/")[0]
        if path.endswith("/fields"):
            name = MAIL_TABLE if table == "mail" else PROGRESS_TABLE
            return [{**f, "field_id": "fld" + str(i)} for i, f in enumerate(field_schema(name))]
        return copy.deepcopy(self.rows[table])

    def iter_pages(self, path, query=None):
        rows = self.paged(path, query)
        size = (query or {}).get("page_size", 500)
        for offset in range(0, len(rows), size):
            yield rows[offset:offset + size]

    def request(self, method, path, body=None, query=None):
        table = path.split("/tables/")[1].split("/")[0]
        fields = body["fields"]
        if method == "POST":
            self.created += 1
            record = {"record_id": f"cloud{self.created}", "fields": copy.deepcopy(fields)}
            self.rows[table].append(record)
            if table == "progress" and self.unknown_create:
                self.unknown_create = False
                raise LarkError("Write acknowledgement was lost")
        else:
            record = next(r for r in self.rows[table] if r["record_id"] == path.rsplit("/", 1)[1])
            if table == "mail" and fields.get("AI处理") == "AI已处理" and self.fail_status:
                self.fail_status = False
                raise LarkError("Status write failed after progress was committed")
            record["fields"].update(copy.deepcopy(fields))
        return {"record": copy.deepcopy(record)}


class Vault:
    def __init__(self, value=None):
        self.value = value
    def load(self):
        return copy.deepcopy(self.value)
    def save(self, value):
        self.value = copy.deepcopy(value)


class LarkTests(unittest.TestCase):
    def test_lark_create_recovers_with_same_client_token(self):
        session = Mock()
        http = LarkHTTP(ENV, session=session)
        api = LarkAPI(ENV, auth=Mock(access_token=Mock(return_value="test-token")), http=http)
        unavailable = requests.Response()
        unavailable.status_code = 200
        unavailable._content = b'{"code":1255001,"msg":"InternalError"}'
        success = requests.Response()
        success.status_code = 200
        success._content = b'{"code":0,"data":{"record":{"record_id":"created-once"}}}'
        session.request.side_effect = [requests.ReadTimeout(), unavailable, success]

        path = "/open-apis/bitable/v1/apps/base/tables/progress/records"
        fields = {"fields": {"internetMessageId": "<source@example.invalid>"}}
        with patch("lark_support.time.sleep") as sleep, self.assertLogs("lark_support", level="WARNING") as log:
            result = api.request("POST", path, body=fields)

        self.assertEqual(result["record"]["record_id"], "created-once")
        self.assertEqual(sleep.call_args_list, [call(10), call(10)])
        self.assertIn("(2/5)", log.output[-1])
        requests_sent = session.request.call_args_list
        self.assertEqual(len(requests_sent), 3)
        urls = {request.args[1] for request in requests_sent}
        self.assertEqual(len(urls), 1)
        token = parse_qs(urlsplit(urls.pop()).query)["client_token"][0]
        self.assertEqual(UUID(token).version, 4)
        self.assertTrue(all(request.kwargs["json"] == fields for request in requests_sent))

    def test_lark_retry_limit_and_non_retryable_permission_error(self):
        session = Mock()
        http = LarkHTTP(ENV, session=session)
        api = LarkAPI(ENV, auth=Mock(access_token=Mock(return_value="test-token")), http=http)
        path = "/open-apis/bitable/v1/apps/base/tables/progress/records/rec1"
        session.request.side_effect = requests.ConnectionError()
        with patch("lark_support.time.sleep") as sleep, self.assertLogs("lark_support", level="WARNING"):
            with self.assertRaises(LarkError):
                api.request("PUT", path, body={"fields": {}})
        self.assertEqual(session.request.call_count, 6)
        self.assertEqual(sleep.call_args_list, [call(10)] * 5)

        session.reset_mock(side_effect=True)
        forbidden = requests.Response()
        forbidden.status_code = 403
        forbidden._content = b'{"code":99991679,"msg":"Permission denied"}'
        session.request.return_value = forbidden
        with patch("lark_support.time.sleep") as sleep, self.assertLogs("lark_support", level="ERROR"):
            with self.assertRaises(LarkError):
                api.request("PUT", path, body={"fields": {}})
        self.assertEqual(session.request.call_count, 1)
        sleep.assert_not_called()

    def test_request_logs_codes_and_distinguishes_missing_response(self):
        session = Mock()
        http = LarkHTTP(ENV, session=session)
        url = http.domain + "/open-apis/bitable/v1/apps/base/tables/mail/records?secret=hidden-query"

        response = requests.Response()
        response.status_code = 200
        response._content = b'{"code":1255001,"msg":"InternalError"}'
        session.request.return_value = response
        with self.assertLogs("lark_support", level="ERROR") as log:
            with self.assertRaises(LarkError) as error:
                http.send("PUT", url)
        self.assertEqual(error.exception.code, 1255001)
        self.assertIn("PUT /open-apis/bitable/v1/apps/base/tables/mail/records", log.output[0])
        self.assertIn("http_status=200 lark_code=1255001", log.output[0])
        self.assertNotIn("hidden-query", log.output[0])

        response.status_code = 502
        response._content = b"Bad Gateway"
        with self.assertLogs("lark_support", level="ERROR") as log:
            with self.assertRaises(LarkError):
                http.send("PUT", url)
        self.assertIn("http_status=502 lark_code=unavailable", log.output[0])

        session.request.side_effect = requests.ReadTimeout("private exception detail")
        with self.assertLogs("lark_support", level="ERROR") as log:
            with self.assertRaises(LarkError):
                http.send("PUT", url)
        self.assertIn("http_status=unavailable lark_code=unavailable", log.output[0])
        self.assertIn("network_error=ReadTimeout", log.output[0])
        self.assertNotIn("private exception detail", log.output[0])

    def test_cloud_dates_boolean_and_null_roundtrip(self):
        cloud = Cloud()
        store = LarkRecordStore(ENV, api=cloud)
        fields = {"receivedDateTime": "2026-11-03T03:00:00Z", "hasAttachments": False, "subject": "测试"}
        saved = store.create_record(MAIL_TABLE, fields)
        raw = cloud.rows["mail"][0]
        self.assertIsInstance(raw["fields"]["receivedDateTime"], int)
        self.assertIs(raw["fields"]["hasAttachments"], False)
        reread = LarkRecordStore(ENV, api=cloud).list_records(MAIL_TABLE)[0]
        self.assertEqual(reread["fields"]["receivedDateTime"], "2026-11-03T03:00:00+00:00")
        self.assertEqual(saved["record_id"], reread["record_id"])
        store.create_record(PROGRESS_TABLE, {"邮件": "alice@example.com", "卡点": "缺文件"})
        rid = store.list_records(PROGRESS_TABLE)[0]["record_id"]
        store.update_record(PROGRESS_TABLE, rid, {"卡点": ""})
        self.assertIsNone(cloud.rows["progress"][0]["fields"]["卡点"])

    def test_status_failure_and_uncertain_create_resume_without_duplicate_progress(self):
        for fault in ("fail_status", "unknown_create"):
            with self.subTest(fault=fault):
                cloud = Cloud()
                store = LarkRecordStore(ENV, api=cloud)
                store.create_record(MAIL_TABLE, {"receivedDateTime": "2026-11-03T03:00:00Z", "AI处理": "AI未完成",
                                                 "internetMessageId": "<source@example.invalid>"})
                setattr(cloud, fault, True)
                class Analyzer:
                    def analyze(self, mail, known):
                        return result()
                first = process_pending(store, Analyzer())
                self.assertEqual(first["failed"], 1)
                self.assertEqual(len(cloud.rows["progress"]), 1)
                self.assertEqual(cloud.rows["progress"][0]["fields"]["internetMessageId"], "<source@example.invalid>")
                second = process_pending(LarkRecordStore(ENV, api=cloud), Analyzer())
                self.assertEqual(second["updated"], 1)
                self.assertEqual(len(cloud.rows["progress"]), 1)
                self.assertEqual(cloud.rows["mail"][0]["fields"]["AI处理"], "AI已处理")

    def test_events_ignore_own_status_writes_and_other_tables(self):
        base = {"file_token": "base", "table_id": "mail", "action_list": [{"action": "record_edited", "after_value": [{"field_id": "status", "field_value": "AI已处理"}]}]}
        self.assertFalse(should_wake(base, "base", "mail", "status", {"body"}))
        base["action_list"][0]["after_value"][0]["field_value"] = "AI失败"
        self.assertFalse(should_wake(base, "base", "mail", "status", {"body"}))
        base["action_list"][0]["after_value"][0]["field_value"] = json.dumps([{"text": "AI未完成"}])
        self.assertTrue(should_wake(base, "base", "mail", "status", {"body"}))
        self.assertFalse(should_wake(base, "base", "progress", "status", {"body"}))
        base["action_list"] = [{"action": "record_added"}]
        self.assertTrue(should_wake(base, "base", "mail", "status", {"body"}))

    def test_user_token_required_without_app_identity_fallback(self):
        class HTTP:
            pass
        auth = LarkAuth(ENV, http=HTTP(), vault=Vault(), pending=Vault())
        with self.assertRaisesRegex(LarkError, "User authorization required"):
            auth.access_token()

    def test_user_token_refresh_rotates_and_persists(self):
        class HTTP:
            domain = "https://open.larksuite.com"
            def send(self, method, url, **kwargs):
                self.body = kwargs["form"]
                self.url = url
                return {"access_token": "new-user", "refresh_token": "new-refresh", "expires_in": 7200}
        http = HTTP()
        vault = Vault({"app_id": ENV["LARK_APP_ID"], "access_token": "old", "refresh_token": "old-refresh", "expires_at": 0})
        auth = LarkAuth(ENV, http=http, vault=vault, pending=Vault())
        self.assertEqual(auth.access_token(), "new-user")
        self.assertEqual(vault.load()["refresh_token"], "new-refresh")
        self.assertEqual(http.body["grant_type"], "refresh_token")
        self.assertEqual(http.url, "https://open.larksuite.com/open-apis/authen/v2/oauth/token")

    @unittest.skipUnless(sys.platform == "darwin", "macOS refresh lock")
    def test_concurrent_workers_reuse_rotated_mac_token(self):
        vault = Vault({"app_id": ENV["LARK_APP_ID"], "access_token": "old",
                       "refresh_token": "old-refresh", "expires_at": 0})
        refresh_started, second_read = Event(), Event()

        def oauth(*args, **kwargs):
            refresh_started.set()
            if not second_read.wait(5):
                raise AssertionError("Second worker did not see the expired token")
            return {"access_token": "new", "refresh_token": "new-refresh"}

        def load_for_second():
            data = vault.load()
            second_read.set()
            return data

        http = Mock(domain="https://open.larksuite.com", send=Mock(side_effect=oauth))
        second_vault = Mock(load=Mock(side_effect=load_for_second), save=vault.save)
        first = LarkAuth(ENV, http=http, vault=vault, pending=Vault())
        second = LarkAuth(ENV, http=http, vault=second_vault, pending=Vault())
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as pool:
            first.refresh_lock_path = second.refresh_lock_path = Path(directory) / "refresh.lock"
            first_run = pool.submit(first.access_token)
            self.assertTrue(refresh_started.wait(5))
            second_run = pool.submit(second.access_token)
            self.assertEqual(first_run.result(timeout=5), "new")
            self.assertEqual(second_run.result(timeout=5), "new")
        http.send.assert_called_once()

    def test_native_device_grant_saves_user_and_refresh_tokens(self):
        class HTTP:
            domain = "https://open.larksuite.com"
            def send(self, method, url, **kwargs):
                self.url, self.fields = url, kwargs["form"]
                return {"access_token": "synthetic-user", "refresh_token": "synthetic-refresh", "expires_in": 7200}
        http, vault = HTTP(), Vault()
        pending = Vault({"app_id": ENV["LARK_APP_ID"], "device_code": "synthetic-device", "created_at": time.time(), "expires_in": 600})
        auth = LarkAuth(ENV, http=http, vault=vault, pending=pending)
        self.assertTrue(auth.finish_authorization()["refresh_token_issued"])
        self.assertEqual(vault.load()["access_token"], "synthetic-user")
        self.assertEqual(http.url, "https://open.larksuite.com/open-apis/authen/v2/oauth/token")
        self.assertEqual(http.fields["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")

    def test_busy_worker_lock_can_be_acquired_after_release(self):
        with tempfile.TemporaryDirectory() as directory, patch("lark_support.PROJECT", Path(directory)):
            first = LarkRunLock(ENV["LARK_APP_ID"])
            try:
                with self.assertRaises(LarkError) as raised:
                    LarkRunLock(ENV["LARK_APP_ID"])
                self.assertEqual(raised.exception.code, "local_worker_busy")
            finally:
                first.close()
            next_worker = LarkRunLock(ENV["LARK_APP_ID"])
            next_worker.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_credentials_are_encrypted_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = SecureJSON(Path(directory) / "token.dpapi")
            vault.save({"access_token": "synthetic-private-token"})
            self.assertNotIn(b"synthetic-private-token", vault.path.read_bytes())
            self.assertEqual(vault.load()["access_token"], "synthetic-private-token")


if __name__ == "__main__":
    unittest.main()
