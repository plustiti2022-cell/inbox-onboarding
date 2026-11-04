"""Official SDK long connection; record events wake the sequential user-API workflow."""
from __future__ import annotations
import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from model import read_env
from lark_support import PROJECT, LarkError

EVENT_TYPE = "drive.file.bitable_record_changed_v1"


def event_text(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return "".join(event_text(x) for x in value)
    if isinstance(value, dict):
        return str(value.get("text", value.get("value", "")))
    return str(value or "")


def should_wake(event, base, table, status_field_id, raw_field_ids):
    if event.get("file_token") != base or event.get("table_id") != table:
        return False
    for action in event.get("action_list") or []:
        if action.get("action") in ("record_added", "record_created"):
            return True
        if action.get("action") != "record_edited":
            continue
        changed = action.get("after_value") or []
        if any(x.get("field_id") in raw_field_ids for x in changed):
            return True
        if any(x.get("field_id") == status_field_id and event_text(x.get("field_value")) == "AI未完成" for x in changed):
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    parser.add_argument("--model")
    parser.add_argument("--check-connection", action="store_true")
    parser.add_argument("--receive-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=0)
    args = parser.parse_args(argv)
    env = read_env(args.env)
    import lark_oapi as lark
    from lark_oapi.ws.client import loop
    # SDK 1.7.5 explicitly disables proxy discovery. Match this machine's HTTPS
    # transport while retaining the SDK's protocol, heartbeat and reconnect logic.
    import importlib
    import ssl
    import certifi
    ws_transport = importlib.import_module("lark_oapi.ws.client")
    original_kwargs = ws_transport._ws_connect_kwargs
    proxy = env.get("LARK_WS_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    def connection_kwargs(headers=None):
        options = original_kwargs(headers)
        if proxy:
            options["proxy"] = proxy
        options["ssl"] = ssl.create_default_context(cafile=certifi.where())
        return options

    ws_transport._ws_connect_kwargs = connection_kwargs
    directory = PROJECT / "outputs" / "lark"
    directory.mkdir(parents=True, exist_ok=True)
    status_file = directory / "connection_status.json"
    status = {"pid": os.getpid(), "app_id": env["LARK_APP_ID"], "connected": False,
              "event_count": 0, "identity_for_table_operations": "user", "transport": "websocket"}
    wake, stop = queue.Queue(maxsize=1), threading.Event()
    seen, status_lock = deque(maxlen=1000), threading.Lock()
    fields, status_id = set(), ""
    if not (args.check_connection or args.receive_only):
        from lark_store import LarkRecordStore
        from storage import MAIL_TABLE, MAIL_FIELDS
        store = LarkRecordStore(env)
        status_id = store.fields[MAIL_TABLE]["AI处理"]["field_id"]
        fields = {store.fields[MAIL_TABLE][name]["field_id"] for name in MAIL_FIELDS[:15]}
        store.close()

    def save(**changes):
        with status_lock:
            status.update(changes, updated_at=time.time())
            temporary = status_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, status_file)

    def enqueue():
        if args.check_connection or args.receive_only:
            return
        try:
            wake.put_nowait(True)
        except queue.Full:
            pass

    class SafeSDKLog(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if "connected to " in message and "disconnected to " not in message:
                save(connected=True, ready_at=time.time())
                print(json.dumps({"websocket_ready": True, "app_id": env["LARK_APP_ID"]}), flush=True)
                enqueue()
            elif record.levelno >= logging.WARNING:
                safe = re.sub(r"(?:https?|wss?)://\S+", "[endpoint]", message)
                safe = safe.replace(env["LARK_APP_SECRET"], "[REDACTED]")
                save(last_error=safe[:500])
                print(json.dumps({"sdk_warning": safe[:500]}), flush=True)

    sdk_logger = logging.getLogger("Lark")
    sdk_logger.handlers = [SafeSDKLog()]
    sdk_logger.propagate = False

    def on_event(data):
        payload = json.loads(lark.JSON.marshal(data))
        event_id = payload.get("header", {}).get("event_id")
        if event_id and event_id in seen:
            return
        if event_id:
            seen.append(event_id)
        event = payload.get("event", {})
        if event.get("file_token") != env.get("LARK_BASE_TOKEN"):
            return
        save(event_count=status["event_count"] + 1, last_event_id=event_id,
             last_table_id=event.get("table_id"), last_event_at=time.time())
        if should_wake(event, env.get("LARK_BASE_TOKEN"), env.get("LARK_MAIL_TABLE_ID"), status_id, fields):
            enqueue()

    def worker():
        from run_onboarding import main as run_batch
        while not stop.is_set():
            try:
                wake.get(timeout=1)
            except queue.Empty:
                continue
            command = ["--backend", "lark", "--resume", "--env", str(args.env), "--no-preview"]
            if args.model:
                command += ["--model", args.model]
            try:
                code = run_batch(command)
                save(last_batch_exit_code=code)
            except LarkError as error:
                save(last_batch_error=str(error)[:500])
                if error.code == "local_worker_busy" and not stop.wait(1):
                    enqueue()
            except Exception as error:
                save(last_batch_error=str(error)[:500])

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_drive_file_bitable_record_changed_v1(on_event).build()
    domain = "https://open.larksuite.com" if env.get("LARK_BRAND", "lark") == "lark" else "https://open.feishu.cn"
    client = lark.ws.Client(env["LARK_APP_ID"], env["LARK_APP_SECRET"], event_handler=handler,
                            domain=domain, log_level=lark.LogLevel.INFO, auto_reconnect=not args.check_connection)
    client.on_reconnecting = lambda: save(connected=False)
    client.on_reconnected = lambda: (save(connected=True), enqueue())
    thread = None
    if not (args.check_connection or args.receive_only):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
    timer = None
    if args.timeout:
        timer = threading.Timer(args.timeout, lambda: loop.call_soon_threadsafe(loop.stop))
        timer.daemon = True
        timer.start()
    save()
    connected = False
    try:
        client.start()
    except KeyboardInterrupt:
        pass
    except RuntimeError as error:
        if not args.timeout or "Event loop stopped" not in str(error):
            raise
    finally:
        connected = bool(status.get("ready_at"))
        stop.set()
        if timer:
            timer.cancel()
        import asyncio
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        # Stop receive/heartbeat tasks before a planned socket close so the SDK
        # does not interpret clean shutdown as an unexpected reconnect failure.
        client._auto_reconnect = False
        loop.run_until_complete(client._disconnect())
        save(connected=False, stopped_at=time.time())
        ws_transport._ws_connect_kwargs = original_kwargs
    return 0 if connected else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as error:
        message = re.sub(r"(?:https?|wss?)://\S+", "[endpoint]", str(error))
        print("Lark listener failed: " + message, file=sys.stderr)
        sys.exit(1)
