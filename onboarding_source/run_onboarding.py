"""Run the mail/onboarding workflow using native Lark Base or local Excel."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model import MailAnalyzer, read_env
from storage import ExcelRecordStore, MAIL_TABLE, PROGRESS_TABLE, SHANGHAI, load_mail_input
from workflow import mail_time, process_pending

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DEFAULT_WORKBOOK = PROJECT / "outputs" / "onboarding_01a19f8a" / "开户自动跟进.xlsx"


def runtime_paths(node_arg: str | None, modules_arg: str | None) -> tuple[str, str]:
    bundle = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
    bundled_node = bundle / "node/bin/node.exe"
    bundled_modules = bundle / "node/node_modules"
    node = node_arg or (str(bundled_node) if bundled_node.exists() else shutil.which("node"))
    modules = modules_arg or (str(bundled_modules) if bundled_modules.exists() else None)
    if not node or not modules:
        raise ValueError("Node.js/artifact-tool runtime not found; pass --node and --node-modules")
    return node, modules


def fetch_mail(env_path: Path) -> tuple[list[dict], dict]:
    # Reuse the source project's actual Graph client, HTML cleaner and field mapper.
    sys.path.insert(0, str(PROJECT / "mail_export_source"))
    from export_mail_last24h import GraphClient, read_config, map_message, iso
    from storage import canonical_header
    from export_mail_last24h import HEADERS
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=24)
    messages, pages = GraphClient(read_config(env_path)).fetch(start, end)
    headers = [canonical_header(h) for h in HEADERS]
    rows = [dict(zip(headers, map_message(message))) for message in messages]
    return rows, {"source": "Microsoft Graph", "pages": pages, "messages": len(rows),
                  "window_start_utc_inclusive": iso(start), "window_end_utc_exclusive": iso(end),
                  "sort_order": "receivedDateTime asc"}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="Existing mapped_rows.json or mail export .xlsx")
    source.add_argument("--fetch", action="store_true", help="Fetch the last 24 hours (default when no source is selected)")
    source.add_argument("--resume", action="store_true", help="Process unfinished records already in the selected backend")
    source.add_argument("--init", action="store_true", help="Create/validate the selected tables without fetching mail or calling AI")
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompt_onboarding.txt")
    parser.add_argument("--model", help="Explicit model override for this run; does not modify .env")
    parser.add_argument("--backend", choices=("excel", "lark"), help="Defaults to ONBOARDING_BACKEND, otherwise excel")
    parser.add_argument("--watch", action="store_true", help="Listen for native Base events over the official SDK WebSocket")
    parser.add_argument("--node")
    parser.add_argument("--node-modules")
    parser.add_argument("--limit", type=int, help="Maximum unfinished mails to analyze this run")
    parser.add_argument("--import-only", action="store_true", help="Import/fetch mails without calling the model")
    parser.add_argument("--no-preview", action="store_true", help="Skip final preview PNGs")
    return parser


def main(argv=None, *, lock_lark=True) -> int:
    args = build_parser().parse_args(argv)
    base_env = read_env(args.env) if args.env.exists() else {}
    backend = args.backend or base_env.get("ONBOARDING_BACKEND", "excel")
    if backend not in ("excel", "lark"):
        raise ValueError("ONBOARDING_BACKEND must be excel or lark")
    if args.watch:
        if backend != "lark" or args.input or args.fetch or args.init or args.import_only or args.limit:
            raise ValueError("Use --watch with the lark backend to process the cloud's pending records; run a fetch/import separately")
        from run_lark_listener import main as listen
        options = ["--env", str(args.env)]
        if args.model:
            options += ["--model", args.model]
        return listen(options)
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive")
    if backend == "excel" and args.resume and not args.workbook.exists():
        raise ValueError("--resume requires an existing simulation workbook")
    if backend == "excel" and args.input and args.input.resolve() == args.workbook.resolve():
        raise ValueError("Use --resume to process the existing workbook; --input is for a separate mail export")
    # Validate model configuration before importing anything in an actual processing run.
    analyzer = None
    if not (args.init or args.import_only):
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        model_env = dict(base_env)
        if args.model:
            model_env["LLM_MODEL"] = args.model
        analyzer = MailAnalyzer(model_env, config, args.prompt)
    run_id = datetime.now(SHANGHAI).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    run_dir = (args.workbook.resolve().parent if backend == "excel" else PROJECT / "outputs" / "lark") / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    guard = None
    if backend == "excel":
        node, modules = runtime_paths(args.node, args.node_modules)
        store = ExcelRecordStore(args.workbook, node, modules)
    else:
        from lark_store import LarkRecordStore
        from lark_support import LarkRunLock
        if lock_lark:
            guard = LarkRunLock(base_env["LARK_APP_ID"])
        try:
            print("Reading Lark snapshot (50 records per page, temporary disk cache)", flush=True)
            store = LarkRecordStore(base_env, disk_backed=True)
        except BaseException:
            if guard:
                guard.close()
            raise
    report = {"complete": False, "run_id": run_id, "backend": backend,
              "llm_model": analyzer.model if analyzer else None, "imported": 0}
    if backend == "excel":
        report["workbook"] = str(store.path)
    else:
        report.update(identity="user", base_url=base_env.get("LARK_BASE_URL"), table_ids=store.table_ids)

    def event_log(event):
        if event["state"] == "analyzing":
            print(f"[{event['index']}/{event['total']}] Analyzing {event['mail_record_id']}", flush=True)
        else:
            print(f"[{event['index']}/{event['total']}] {event['action']}", flush=True)
            with (run_dir / "results.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    try:
        if not (args.init or args.resume):
            if args.input:
                fields = load_mail_input(args.input)
                report["source"] = str(args.input.resolve())
            else:
                fields, source = fetch_mail(args.env)
                report.update(source)
            fields.sort(key=lambda row: mail_time({"fields": row}))
            for row in fields:
                store.create_record(MAIL_TABLE, row)
            report["imported"] = len(fields)
            print(f"Imported {len(fields)} mails, oldest first", flush=True)
        store.flush()
        if analyzer:
            process_pending(store, analyzer, limit=args.limit, on_event=event_log,
                            retain_items=backend != "lark", report=report)
        preview_dir = None if args.no_preview or backend == "lark" else run_dir / "previews"
        store.flush(preview_dir=preview_dir)
        if backend == "lark":
            report.update(store.record_counts())
        else:
            report["mail_records"] = len(store.list_records(MAIL_TABLE))
            report["progress_records"] = len(store.list_records(PROGRESS_TABLE))
            report["remaining"] = sum(r["fields"].get("AI处理") != "AI已处理" for r in store.list_records(MAIL_TABLE))
        report["complete"] = report.get("failed", 0) == 0 and (analyzer is None or report["remaining"] == 0)
        report["run_finished"] = True
        print(json.dumps({key: value for key, value in report.items() if key != "items"}, ensure_ascii=False, indent=2), flush=True)
        return 1 if report.get("failed", 0) else 0
    except BaseException as error:
        report["error"] = str(error)
        raise
    finally:
        try:
            (run_dir / "run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            try:
                store.close()
            finally:
                if guard:
                    guard.close()


if __name__ == "__main__":
    # UTF-8 works from Windows PowerShell and native process pipes alike.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print("Workflow failed: " + str(error), file=sys.stderr)
        sys.exit(1)
