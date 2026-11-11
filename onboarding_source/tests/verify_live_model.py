"""Verify six ball rules and two negative cases against the configured real model."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model import MailAnalyzer, read_env

ROOT = Path(__file__).resolve().parents[1]


def verify(output: Path, model: str | None = None):
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    config["compliance_addresses"] = ["kyc@hashkey.com"]
    config["own_team_addresses"] = ["james.yang@hashkey.com", "sales@hashkey.com"]
    model_env = read_env(ROOT.parent / ".env")
    if model:
        model_env["LLM_MODEL"] = model
    analyzer = MailAnalyzer(model_env, config, ROOT / "prompt_onboarding.txt")
    client, own, kyc = "alice@northstar.example", "james.yang@hashkey.com", "kyc@hashkey.com"
    cases = [
        ("客户发来", client, own, "", "球在我方"),
        ("我方发客户，合规抄送", own, client, kyc, "球在客户"),
        ("我方发合规，客户仅抄送", own, kyc, client, "球在合规"),
        ("合规发我方，客户在抄送", kyc, own, client, "球在客户"),
        ("合规仅回复我方", kyc, own, "", "球在我方"),
        ("内部同事讨论", own, "sales@hashkey.com", "", "球在我方"),
    ]
    report = {"model": analyzer.model, "cases": [], "passed": True}
    for name, sender, to, cc, expected in cases:
        mail = {"from.emailAddress.address": sender, "toRecipients.addresses": to,
                "ccRecipients.addresses": cc, "subject": "North Star Limited 开户补件跟进",
                "body.content（去除html）": "跟进 North Star Limited 的开户地址证明补件。客户联系人 Alice Chen <alice@northstar.example>，我方对接人为 James Yang。请收件人安排下一步处理。"}
        result = analyzer.analyze(mail, [])
        passed = result["is_onboarding"] and result["球在谁方"] == expected and result["邮件"] == client
        report["cases"].append({"case": name, "expected": expected, "result": result, "passed": passed})
        report["passed"] &= passed
        print(json.dumps({"case": name, "passed": passed, "ball": result["球在谁方"]}, ensure_ascii=False), flush=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for subject, body in [("季度活动邀请", "邀请参加下周行业交流酒会。"), ("开通EDDA白名单权限", "客户已完成开户，现申请开通 EDDA 白名单充值权限。")]:
        result = analyzer.analyze({"subject": subject, "body.content（去除html）": body, "from.emailAddress.address": own}, [])
        passed = not result["is_onboarding"]
        report["cases"].append({"case": subject, "result": result, "passed": passed})
        report["passed"] &= passed
        print(json.dumps({"case": subject, "passed": passed}, ensure_ascii=False), flush=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert report["passed"], "Real model did not pass all cases; inspect report"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", help="Explicit model override; does not modify .env")
    args = parser.parse_args()
    verify(args.output, model=args.model)
