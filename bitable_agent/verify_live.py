"""Live DeepSeek + Lark acceptance, restricted to this run's own synthetic rows."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import PROJECT
from .agent import BitableAgent, DeepSeekClient
from .tools import BitableTools
from model import read_env


TABLE = "开户进度表"
MUTATIONS = {"create_record", "update_record", "delete_record"}


class ScopedTestTools(BitableTools):
    """Model can mutate only rows acknowledged as created by this test run."""
    def __init__(self, env, marker, report, save, auth_project=None):
        super().__init__(env, auth_project=auth_project)
        self.tables = {TABLE: self.tables[TABLE]}
        self.marker, self.owned, self.report, self.save = marker, set(), report, save

    def execute(self, name, arguments):
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "Expected object"}
        if name == "create_record" and arguments.get("fields", {}).get("公司名称") != self.marker:
            return {"ok": False, "error": "Live test may create only its unique company marker"}
        if name in ("get_record", "update_record", "delete_record") and arguments.get("record_id") not in self.owned:
            return {"ok": False, "error": "Live test may access only its acknowledged test rows"}
        if name == "update_record" and "公司名称" in arguments.get("fields", {}):
            return {"ok": False, "error": "Keep the test marker unchanged"}
        if name == "search_records" and (arguments.get("field") != "公司名称" or
                                         arguments.get("value") not in (self.marker, self.marker + "_missing")):
            return {"ok": False, "error": "Live search must use the exact test company marker"}
        result = super().execute(name, arguments)
        if name == "create_record" and result["ok"]:
            self.owned.add(result["record"]["record_id"])
            self.report["created_ids"] = sorted(self.owned)
            self.save()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    parser.add_argument("--auth-project", type=Path, help="Reuse Lark authorization from an existing deployment")
    parser.add_argument("--output", type=Path, default=PROJECT / "outputs/bitable_agent")
    args = parser.parse_args()
    marker = "AGENT_TEST_" + uuid.uuid4().hex[:16]
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / (marker + ".json")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "marker": marker,
              "passed": False, "cases": [], "created_ids": [], "cleanup": {"passed": False}}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    env = read_env(args.env)
    report["model"] = env.get("LLM_MODEL")
    client = DeepSeekClient(env)
    tools = ScopedTestTools(env, marker, report, save, auth_project=args.auth_project)
    agent = BitableAgent(client, tools)
    save()

    def run_case(name, instruction, required, check=None):
        entry = {"name": name, "instruction": instruction, "passed": False}
        report["cases"].append(entry)
        try:
            result = agent.run(instruction, on_tool=lambda e: print(
                f"  {e['name']}: {'ok' if e['result']['ok'] else 'error'}", flush=True))
            entry.update(result)
            successful = {e["name"] for e in agent.trace if e["result"]["ok"]}
            if not required.issubset(successful):
                raise AssertionError("Missing successful tools: " + str(required - successful))
            if any(not e["result"]["ok"] for e in agent.trace):
                raise AssertionError("Tool returned an error")
            if check:
                check()
            entry["passed"] = True
            print(f"PASS {name}", flush=True)
        except Exception as error:
            entry.update(error=str(error), tool_calls=agent.trace)
            raise
        finally:
            save()

    try:
        run_case("discover_schema", "列出可用的多維表格，並查詢开户进度表的欄位。", {"list_tables", "list_fields"})
        run_case("create", f"在开户进度表新增一筆測試記錄：公司名称={marker}，客户名称=工具測試，"
                 "卡点=缺地址证明，简要描述=初次测试，球在谁方=球在客户。", {"create_record"})
        if len(tools.owned) != 1:
            raise AssertionError("Expected exactly one created row")
        rid = next(iter(tools.owned))

        def verify_values(expected):
            fields = tools.get_record(TABLE, rid)["record"]["fields"]
            for key, value in expected.items():
                if (fields.get(key) or "") != value:
                    raise AssertionError(f"Cloud value mismatch: {key}")

        verify_values({"公司名称": marker, "卡点": "缺地址证明", "简要描述": "初次测试"})
        run_case("search_and_update", f"請先搜尋开户进度表公司名称精確等於 {marker} 的記錄，"
                 "只把简要描述改為已收到地址证明，其他欄位保留。", {"search_records", "update_record"},
                 lambda: verify_values({"简要描述": "已收到地址证明", "卡点": "缺地址证明", "公司名称": marker}))
        run_case("clear_field", f"將开户进度表記錄 {rid} 的卡点清空，其他欄位保留。", {"update_record"},
                 lambda: verify_values({"卡点": "", "简要描述": "已收到地址证明"}))

        def no_mutation():
            if any(e["name"] in MUTATIONS for e in agent.trace):
                raise AssertionError("Missing record must not trigger any mutation")

        run_case("missing_record", f"請查找开户进度表公司名称精確等於 {marker}_missing 的記錄，"
                 "將其简要描述改為不存在的测试。如果找不到請告訴我，不要新增。", {"search_records"}, no_mutation)
        run_case("delete", f"請刪除开户进度表記錄 {rid}，這是剛才建立的測試記錄。", {"delete_record"})
        if tools.search_records(TABLE, field="公司名称", value=marker)["records"]:
            raise AssertionError("Deleted row still present")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        print("FAIL " + str(error), flush=True)
    finally:
        # Recover even an acknowledged creation followed by a model/network failure.
        # Search only this run's marker, also covering a lost create acknowledgement.
        try:
            page_token = None
            found = []
            while True:
                page = tools.search_records(TABLE, field="公司名称", value=marker, page_token=page_token)
                found.extend(page["records"])
                if not page["has_more"]:
                    break
                page_token = page["page_token"]
                if not page_token:
                    raise RuntimeError("Cleanup pagination failed")
            for row in found:
                if row["fields"].get("公司名称") == marker:
                    tools.delete_record(TABLE, row["record_id"])
            remaining = tools.search_records(TABLE, field="公司名称", value=marker)
            if remaining["records"] or remaining["has_more"]:
                raise AssertionError("Test cleanup incomplete")
            report["cleanup"] = {"passed": True, "remaining_test_rows": 0}
        except Exception as error:
            report["cleanup"] = {"passed": False, "error": str(error)}
            report["passed"] = False
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        tools.close()
        client.close()
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]),
                      "cleanup": report["cleanup"], "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
