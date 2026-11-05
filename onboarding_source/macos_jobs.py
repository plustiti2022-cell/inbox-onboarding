"""Check the production connections or render the two macOS LaunchAgents."""
from __future__ import annotations

import argparse
import json
import plistlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fetch_mail import GraphClient, read_config
from lark_support import LarkAPI, PROJECT, atomic_write
from model import MailAnalyzer, read_env
from storage import MAIL_FIELDS, MAIL_TABLE, PROGRESS_FIELDS, PROGRESS_TABLE

ROOT = Path(__file__).resolve().parent
ENV = PROJECT / ".env"


def check_connections():
    env = read_env(ENV)
    api = LarkAPI(env)
    try:
        base = env["LARK_BASE_TOKEN"]
        for key, table, required in (
            ("LARK_MAIL_TABLE_ID", MAIL_TABLE, MAIL_FIELDS),
            ("LARK_PROGRESS_TABLE_ID", PROGRESS_TABLE, PROGRESS_FIELDS),
        ):
            fields = api.paged(f"/open-apis/bitable/v1/apps/{base}/tables/{env[key]}/fields")
            missing = set(required) - {field["field_name"] for field in fields}
            if missing:
                raise ValueError(f"Missing fields in {table}: " + ", ".join(sorted(missing)))
        print("Lark user access and both table schemas verified", flush=True)
    finally:
        api.http.session.close()

    end = datetime.now(timezone.utc)
    GraphClient(read_config(ENV)).fetch(end - timedelta(minutes=1), end)
    print("Graph mailbox read verified", flush=True)

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    analyzer = MailAnalyzer(env, config, ROOT / "prompt_onboarding.txt")
    analyzer.analyze({
        "subject": "Synthetic test: office lunch",
        "from.emailAddress.address": "test@example.invalid",
        "body.content（去除html）": "This is a synthetic connection check about office lunch.",
    }, [])
    print("Model response verified using synthetic mail", flush=True)


def render_plists(output_dir):
    if not ENV.is_file():
        raise ValueError(f"Missing deployment configuration: {ENV}")
    logs = PROJECT / "outputs" / "lark" / "launchd"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, script, minute in (("fetch", "fetch_mail.py", 3), ("process", "process_mail.py", 30)):
        label = f"local.inbox-onboarding.{name}"
        plist = {
            "Label": label,
            "ProgramArguments": [sys.executable, "-B", str(ROOT / script), "--env", str(ENV)],
            "WorkingDirectory": str(PROJECT),
            "StartCalendarInterval": {"Minute": minute},
            "StandardOutPath": str(logs / f"{name}.stdout.log"),
            "StandardErrorPath": str(logs / f"{name}.stderr.log"),
            "Umask": 0o077,
        }
        atomic_write(output_dir / f"{label}.plist", plistlib.dumps(plist))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true", help="Verify Lark, Graph and model access")
    action.add_argument("--render-plists", type=Path, metavar="DIR", help="Write the launchd plists for this deployment")
    args = parser.parse_args()
    if args.preflight:
        check_connections()
    else:
        render_plists(args.render_plists)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, OSError) as error:
        print(f"macOS deployment check failed: {error}", file=sys.stderr)
        sys.exit(1)
