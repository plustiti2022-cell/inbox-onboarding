"""End-to-end CLI test with a loopback mock API; never substitutes for live AI."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import MAIL_FIELDS, MAIL_TABLE, PROGRESS_TABLE, read_xlsx
from test_workflow import result

ROOT = Path(__file__).resolve().parents[1]


def verify(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    calls = []
    failures_left = [0]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer synthetic-test-key"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert data["model"] == "synthetic-test-model"
            assert data["response_format"] == {"type": "json_object"}
            content = json.loads(data["messages"][1]["content"])
            for customer in content["公司名称|客户名称|邮件列表"]:
                assert set(customer) == {"公司名称", "客户名称", "邮件"}
            subject = content["邮件主题"]
            calls.append(subject)
            if failures_left[0]:
                failures_left[0] -= 1
                body = b'{"error":{"message":"Synthetic temporary failure"}}'
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if subject == "1":
                answer = result(卡点="缺地址证明", 球在谁方="球在客户")
            elif subject == "2":
                answer = result(公司名称="", 客户名称="", 简要描述="客户已提交附件，等待核对。")
            else:
                answer = result(is_onboarding=False, 公司名称=None, 客户名称=None, 邮件=None, 球在谁方=None, 简要描述=None)
            body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer, ensure_ascii=False)}}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_path = output / "synthetic.env"
    env_path.write_text(f"LLM_API_HOST=http://127.0.0.1:{server.server_port}/v1\nLLM_API_KEY=synthetic-test-key\nLLM_MODEL=synthetic-test-model\nLLM_PROVIDER=openai_compatible\n", encoding="utf-8")
    mail_path = output / "synthetic_mail.json"
    rows = []
    for hour in (3, 2, 1):
        fields = dict.fromkeys(MAIL_FIELDS[:17], "")
        fields.update({"receivedDateTime": f"2026-11-03T0{hour}:00:00Z", "subject": str(hour),
                       "AI处理": "AI未完成", "internetMessageId": f"<{hour}@example.com>", "hasAttachments": False})
        rows.append([fields[h] for h in MAIL_FIELDS[:17]])
    mail_path.write_text(json.dumps({"headers": MAIL_FIELDS[:17], "rows": rows}, ensure_ascii=False), encoding="utf-8")
    workbook = output / "cli_simulation.xlsx"
    base = [sys.executable, str(ROOT / "run_onboarding.py"), "--env", str(env_path), "--workbook", str(workbook), "--no-preview"]
    commands = [["--input", str(mail_path), "--limit", "1"], ["--resume"], ["--resume"]]
    process_env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost"}
    try:
        for index, extra in enumerate(commands, 1):
            process = subprocess.run(base + extra, capture_output=True, text=True, encoding="utf-8", timeout=120, env=process_env)
            (output / f"run_{index}.log").write_text(process.stdout + process.stderr, encoding="utf-8")
            assert process.returncode == 0, f"CLI failed; inspect run_{index}.log"
        assert calls == ["1", "2", "3"], "Chronology/resume unexpectedly recalled the model"
        sheets = read_xlsx(workbook)
        headers, row = sheets[PROGRESS_TABLE]
        progress = dict(zip(headers, row))
        assert progress["邮件"] == "alice@northstar.example"
        assert progress["卡点"] == "缺地址证明" and not progress["经办人"]
        assert progress["internetMessageId"] == "人工修改"
        assert progress["球在谁方"] == "球在我方"
        assert progress["简要描述"] == "客户已提交附件，等待核对。"
        mail_rows = sheets[MAIL_TABLE]
        statuses = [dict(zip(mail_rows[0], r))["AI处理"] for r in mail_rows[1:]]
        assert statuses == ["AI已处理"] * 3
        # A real HTTP failure must persist only the earliest mail as failed;
        # after restarting the CLI, it must finish before any later mail.
        failure_workbook = output / "failure_resume.xlsx"
        failure_base = base.copy()
        failure_base[failure_base.index(str(workbook))] = str(failure_workbook)
        failures_left[0] = 1
        process = subprocess.run(failure_base + ["--input", str(mail_path)], capture_output=True,
                                 text=True, encoding="utf-8", timeout=120, env=process_env)
        (output / "failure.log").write_text(process.stdout + process.stderr, encoding="utf-8")
        assert process.returncode == 1
        failed_sheets = read_xlsx(failure_workbook)
        failed_rows = failed_sheets[MAIL_TABLE]
        failed_statuses = [dict(zip(failed_rows[0], r))["AI处理"] for r in failed_rows[1:]]
        assert failed_statuses == ["AI失败", "AI未完成", "AI未完成"]
        assert len(failed_sheets[PROGRESS_TABLE]) == 1
        assert calls == ["1", "2", "3", "1"]
        process = subprocess.run(failure_base + ["--resume"], capture_output=True,
                                 text=True, encoding="utf-8", timeout=120, env=process_env)
        (output / "failure_resume.log").write_text(process.stdout + process.stderr, encoding="utf-8")
        assert process.returncode == 0
        assert calls == ["1", "2", "3", "1", "1", "2", "3"]
        resumed = read_xlsx(failure_workbook)[PROGRESS_TABLE]
        assert dict(zip(resumed[0], resumed[1]))["简要描述"] == "客户已提交附件，等待核对。"
        report = {"passed": True, "api": "loopback mock, not live AI", "calls": calls,
                  "verified": ["CLI JSON import", "actual HTTP request/response", "seven-key JSON mode", "identity-only context", "limit", "resume", "no repeat calls", "two-table XLSX saved/reopened", "preserve blocker and handler", "manual source marker", "email-only update", "negative classification", "HTTP failure stops later mail", "failed mail retries before later mail"]}
        (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    verify(sys.argv[1])
