"""Process one snapshot of pending Lark mail in received-time order."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from run_onboarding import PROJECT, ROOT, main as run_batch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompt_onboarding.txt")
    parser.add_argument("--model", help="Override the configured model for this run")
    args = parser.parse_args(argv)

    command = ["--backend", "lark", "--resume", "--env", str(args.env),
               "--config", str(args.config), "--prompt", str(args.prompt)]
    if args.model:
        command += ["--model", args.model]
    return run_batch(command, lock_lark=False)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print("Mail processing failed: " + str(error), file=sys.stderr)
        sys.exit(1)
