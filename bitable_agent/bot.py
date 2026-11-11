"""Receive the authorized user's Lark DMs and reply after a confirmed update."""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

import certifi

from . import PROJECT
from .agent import DeepSeekClient
from .tools import BitableTools
from .update import ManualUpdater
from lark_support import LarkError, LarkHTTP, LarkRunLock
from model import read_env
from storage import SHANGHAI


def emit(event, **fields):
    print(json.dumps({"time": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                      "pid": os.getpid(), "event": event, **fields}, ensure_ascii=False), flush=True)


class BotClient:
    def __init__(self, env):
        self.env, self.http = env, LarkHTTP(env)
        self.token, self.expires_at = "", 0

    def request(self, method, path, *, body=None):
        if time.time() + 120 >= self.expires_at:
            token = self.http.send("POST", self.http.domain + "/open-apis/auth/v3/tenant_access_token/internal",
                payload={"app_id": self.env["LARK_APP_ID"], "app_secret": self.env["LARK_APP_SECRET"]})
            self.token = token["tenant_access_token"]
            self.expires_at = time.time() + int(token["expire"])
        return self.http.send(method, self.http.domain + path, payload=body,
                              headers={"Authorization": "Bearer " + self.token})

    def check(self):
        info = self.request("GET", "/open-apis/bot/v3/info")
        scopes = self.request("GET", "/open-apis/application/v6/scopes")["data"]["scopes"]
        granted = {item["scope_name"] for item in scopes
                   if item["scope_type"] == "tenant" and item["grant_status"] == 1}
        missing = []
        if not granted.intersection({"im:message.p2p_msg", "im:message.p2p_msg:readonly"}):
            missing.append("im:message.p2p_msg:readonly")
        if not granted.intersection({"im:message", "im:message:send_as_bot"}):
            missing.append("im:message:send_as_bot")
        return {"bot_name": info["bot"]["app_name"], "missing_scopes": missing,
                "required_event": "im.message.receive_v1", "transport": "websocket"}

    def reply(self, message_id, text):
        result = self.request("POST", f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reply", body={
            "msg_type": "text", "content": json.dumps({"text": text[:20000]}, ensure_ascii=False),
            "uuid": str(uuid5(NAMESPACE_URL, "inbox-onboarding:reply:" + message_id))})
        if not result.get("data", {}).get("message_id"):
            raise LarkError("Lark did not confirm the reply message ID")

    def close(self):
        self.http.session.close()


class ChatHandler:
    def __init__(self, app_id, owner_id, updater, bot, database):
        self.app_id, self.owner_id, self.updater, self.bot = app_id, owner_id, updater, bot
        self.lock = threading.Lock()
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY, state TEXT, reply TEXT)")
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.recover()

    def recover(self):
        # Recover acknowledgements without replaying a possibly committed write.
        with self.lock:
            self.db.execute("UPDATE messages SET state='reply_pending', reply=? WHERE state='processing'",
                            ("上次處理曾中斷，請先核對表格結果，再決定是否重試。",))
            pending = self.db.execute("SELECT message_id FROM messages WHERE state='reply_pending'").fetchall()
            self.db.commit()
        for (message_id,) in pending:
            self.pool.submit(self.deliver, message_id)

    def receive(self, payload):
        event = payload.get("event", {})
        sender, message = event.get("sender", {}), event.get("message", {})
        owner_match = sender.get("sender_id", {}).get("open_id") == self.owner_id
        emit("message_received", message_id=message.get("message_id"), chat_type=message.get("chat_type"),
             message_type=message.get("message_type"), sender_type=sender.get("sender_type"), owner_match=owner_match)
        if payload.get("header", {}).get("app_id") != self.app_id:
            emit("message_ignored", message_id=message.get("message_id"), reason="different_app")
            return
        if (sender.get("sender_type") != "user" or not owner_match
                or message.get("chat_type") != "p2p" or message.get("message_type") != "text"):
            emit("message_ignored", message_id=message.get("message_id"), reason="requires_owner_private_text")
            return
        try:
            text = json.loads(message.get("content", ""))["text"].strip()
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        message_id = message.get("message_id")
        if not message_id or not text:
            return
        with self.lock:
            inserted = self.db.execute("INSERT OR IGNORE INTO messages VALUES (?, 'processing', NULL)", (message_id,)).rowcount
            state = self.db.execute("SELECT state FROM messages WHERE message_id=?", (message_id,)).fetchone()[0]
            self.db.commit()
        if inserted:
            emit("message_queued", message_id=message_id)
            self.pool.submit(self.process, message_id, text)
        elif state == "reply_pending":
            self.pool.submit(self.deliver, message_id)
        else:
            emit("message_duplicate", message_id=message_id, state=state)

    def process(self, message_id, text):
        emit("processing_started", message_id=message_id)
        try:
            answer = self.updater.run(text, on_event=lambda event, **details: emit(event, message_id=message_id, **details))
        except Exception as error:
            # Avoid exposing credentials, model payloads or raw transport errors.
            logging.getLogger(__name__).error("Instruction failed (%s)", type(error).__name__)
            answer = "處理中斷，未取得更新確認。請先核對表格後再重試。"
        with self.lock:
            self.db.execute("UPDATE messages SET state='reply_pending', reply=? WHERE message_id=?", (answer, message_id))
            self.db.commit()
        emit("processing_finished", message_id=message_id, updated=answer.startswith("已更新"))
        self.deliver(message_id)

    def deliver(self, message_id):
        with self.lock:
            row = self.db.execute("SELECT state, reply FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if not row or row[0] != "reply_pending":
            return
        try:
            self.bot.reply(message_id, row[1])
        except Exception as error:
            logging.getLogger(__name__).error("Reply failed (%s); saved reply for recovery", type(error).__name__)
            emit("reply_failed", message_id=message_id, error_type=type(error).__name__)
            return
        with self.lock:
            self.db.execute("UPDATE messages SET state='done' WHERE message_id=?", (message_id,))
            self.db.commit()
        emit("reply_sent", message_id=message_id)

    def close(self):
        self.pool.shutdown(wait=True)
        self.db.close()


def serve(env, handler, *, timeout=0, check_connection=False):
    import lark_oapi as lark
    transport = importlib.import_module("lark_oapi.ws.client")
    original_kwargs = transport._ws_connect_kwargs
    proxy = env.get("LARK_WS_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    def connection_kwargs(headers=None):
        options = original_kwargs(headers)
        if proxy:
            options["proxy"] = proxy
        options["ssl"] = ssl.create_default_context(cafile=certifi.where())
        return options

    transport._ws_connect_kwargs = connection_kwargs
    connected = threading.Event()

    class SafeLog(logging.Handler):
        def emit(self, record):
            text = record.getMessage()
            if "connected to " in text and "disconnected to " not in text:
                connected.set()
                emit("connected", connected=True, subscription="im.message.receive_v1")
            elif "disconnected to " in text:
                emit("disconnected", connected=False)
            elif record.levelno >= logging.WARNING:
                safe = re.sub(r"(?:https?|wss?)://\S+", "[endpoint]", text).replace(env["LARK_APP_SECRET"], "[REDACTED]")
                emit("connection_warning", detail=safe[:300])

    logger = logging.getLogger("Lark")
    previous_handlers, previous_propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [SafeLog()], False
    dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        lambda event: handler.receive(json.loads(lark.JSON.marshal(event)))).build()
    client = lark.ws.Client(env["LARK_APP_ID"], env["LARK_APP_SECRET"], event_handler=dispatcher,
        domain="https://open.larksuite.com" if env.get("LARK_BRAND", "lark") == "lark" else "https://open.feishu.cn",
        log_level=lark.LogLevel.INFO, auto_reconnect=not check_connection)
    timer = threading.Timer(timeout, lambda: transport.loop.call_soon_threadsafe(transport.loop.stop)) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        client.start()
    except KeyboardInterrupt:
        pass
    except RuntimeError as error:
        if not timeout or "Event loop stopped" not in str(error):
            raise
    finally:
        import asyncio
        if timer:
            timer.cancel()
        client._auto_reconnect = False
        pending = asyncio.all_tasks(transport.loop)
        for task in pending:
            task.cancel()
        if pending:
            transport.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        transport.loop.run_until_complete(client._disconnect())
        transport._ws_connect_kwargs = original_kwargs
        logger.handlers, logger.propagate = previous_handlers, previous_propagate
    return connected.is_set()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    parser.add_argument("--auth-project", type=Path)
    parser.add_argument("--check", action="store_true", help="Read existing bot permissions without sending messages")
    parser.add_argument("--check-connection", action="store_true", help="Connect without processing messages")
    parser.add_argument("--timeout", type=int, default=0)
    args = parser.parse_args(argv)
    if args.timeout < 0 or (args.check_connection and not args.timeout):
        parser.error("Use a positive --timeout with --check-connection")
    env = read_env(args.env)
    bot = BotClient(env)
    tools = client = handler = guard = None
    try:
        status = bot.check()
        print(json.dumps(status, ensure_ascii=False), flush=True)
        if args.check:
            return 0 if not status["missing_scopes"] else 1
        if args.check_connection:
            class IgnoreMessages:
                def receive(self, payload):
                    pass
            return 0 if serve(env, IgnoreMessages(), timeout=args.timeout, check_connection=True) else 1
        if status["missing_scopes"]:
            # Scope discovery is advisory. Let Lark enforce the live event/API
            # permissions so an incomplete scope listing cannot block a connection.
            emit("scope_warning", reported_missing=status["missing_scopes"], action="starting_listener")
        guard = LarkRunLock(env["LARK_APP_ID"] + "_chat")
        tools = BitableTools(env, auth_project=args.auth_project)
        owner = tools.api.request("GET", "/open-apis/authen/v1/user_info")["open_id"]
        client = DeepSeekClient(env)
        runtime = PROJECT / ".runtime/bitable_bot" / env["LARK_APP_ID"]
        runtime.mkdir(parents=True, exist_ok=True)
        handler = ChatHandler(env["LARK_APP_ID"], owner, ManualUpdater(client, tools), bot, runtime / "messages.sqlite3")
        emit("listener_starting", bot_name=status["bot_name"], identity="existing_user_oauth")
        return 0 if serve(env, handler, timeout=args.timeout) else 1
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if handler:
            handler.close()
        if client:
            client.close()
        if tools:
            tools.close()
        if guard:
            guard.close()
        bot.close()


if __name__ == "__main__":
    raise SystemExit(main())
