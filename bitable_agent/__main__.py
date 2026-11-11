"""Run from project root: python -m bitable_agent 'your instruction'."""
import argparse
import json
import sys
from pathlib import Path

from . import PROJECT
from .agent import BitableAgent, DeepSeekClient
from .tools import BitableTools
from model import read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instruction")
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    parser.add_argument("--auth-project", type=Path, help="Project/deployment directory containing the existing Lark authorization")
    parser.add_argument("--trace", type=Path, help="Save tool arguments/results; may contain table data")
    args = parser.parse_args()
    client = tools = agent = None
    try:
        env = read_env(args.env)
        client = DeepSeekClient(env)
        tools = BitableTools(env, auth_project=args.auth_project)
        agent = BitableAgent(client, tools)
        result = agent.run(args.instruction, on_tool=lambda e: print(
            f"[tool] {e['name']}: {'ok' if e['result']['ok'] else 'error'}", file=sys.stderr, flush=True))
        print(result["answer"])
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if args.trace and agent:
            args.trace.parent.mkdir(parents=True, exist_ok=True)
            args.trace.write_text(json.dumps(agent.trace, ensure_ascii=False, indent=2), encoding="utf-8")
        if tools:
            tools.close()
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
