"""Fetch one Microsoft Graph mail window and append new messages to Lark."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lark_store import LarkRecordStore
from lark_support import PROJECT, atomic_write
from model import read_env
from storage import MAIL_TABLE, canonical_header

sys.path.insert(0, str(PROJECT / "mail_export_source"))
from export_mail_last24h import GraphClient, HEADERS, iso, map_message, parse_time, read_config


def checkpoint_path(config, env):
    # Separate watermarks when using another mailbox or destination table.
    identity = [config["MICROSOFT_GRAPH_TENANT_ID"], config["MICROSOFT_GRAPH_USER_ID"],
                env.get("LARK_BRAND", "lark"), env["LARK_BASE_TOKEN"], env["LARK_MAIL_TABLE_ID"]]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return PROJECT / ".runtime" / "graph" / f"{key}.json"


def save_checkpoint(path, state):
    atomic_write(path, (json.dumps(state, indent=2) + "\n").encode())


def fetch_once(env_path, started_at):
    env = read_env(env_path)
    graph_config = read_config(env_path)
    if not env.get("LARK_BASE_TOKEN") or not env.get("LARK_MAIL_TABLE_ID"):
        raise ValueError("Configure the Lark Base and mail table ID in .env")
    checkpoint = checkpoint_path(graph_config, env)
    end = started_at.astimezone(timezone.utc)
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("first_start_utc"), str) or "last_success_end_utc" not in state:
            raise ValueError("Invalid Graph checkpoint; restore it before retrying")
    else:
        state = {"first_start_utc": iso(end - timedelta(hours=24)), "last_success_end_utc": None}
        save_checkpoint(checkpoint, state)

    previous = state["last_success_end_utc"]
    if previous is None:
        start = parse_time(state["first_start_utc"])
    else:
        if not isinstance(previous, str) or parse_time(previous) > end:
            raise ValueError("Invalid Graph checkpoint or system clock")
        previous_hour = end.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = min(previous_hour, parse_time(previous))
    if start >= end:
        raise ValueError("Graph window start must precede its end")

    messages, pages = GraphClient(graph_config).fetch(start, end)
    headers = [canonical_header(name) for name in HEADERS]
    rows = [dict(zip(headers, map_message(message))) for message in messages]
    if any(not isinstance(row["internetMessageId"], str) or not row["internetMessageId"].strip() for row in rows):
        raise ValueError("Graph returned mail without an internetMessageId; cannot safely deduplicate the window")

    imported = 0
    if rows:
        store = LarkRecordStore(env, tables=(MAIL_TABLE,))
        try:
            existing = {record["fields"]["internetMessageId"] for record in store.list_records(MAIL_TABLE)}
            for row in rows:
                message_id = row["internetMessageId"]
                if message_id not in existing:
                    store.create_record(MAIL_TABLE, row)
                    existing.add(message_id)
                    imported += 1
        finally:
            store.close()

    state["last_success_end_utc"] = iso(end)
    save_checkpoint(checkpoint, state)
    return {"window_start_utc_inclusive": iso(start), "window_end_utc_exclusive": iso(end),
            "pages": pages, "fetched": len(rows), "imported": imported,
            "skipped_existing_or_duplicate": len(rows) - imported, "checkpoint": str(checkpoint)}


def main(argv=None):
    started_at = datetime.now(timezone.utc)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    args = parser.parse_args(argv)
    print(json.dumps(fetch_once(args.env, started_at), ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print("Mail fetch failed: " + str(error), file=sys.stderr)
        sys.exit(1)
