"""Read the progress table and send morning-report cards through the Lark bot."""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "onboarding_source"))
from lark_support import LarkAPI, LarkHTTP, PROJECT
from model import read_env

HK = ZoneInfo("Asia/Hong_Kong")
BALLS = ("球在我方", "球在客户", "球在合规", "球权待确认")
LABELS = ("球在我方", "球在客户", "球在合规", "球权待确认")
COLORS = ("orange", "blue", "green", "purple")
TEXT_FIELDS = ("公司名称", "客户名称", "邮件", "球在谁方", "卡点", "简要描述", "经办人")


def text(value):
    if isinstance(value, list):
        return "".join(item["text"] for item in value).strip()
    return (value or "").strip()


def read_progress(env):
    api = LarkAPI(env)
    path = f"/open-apis/bitable/v1/apps/{env['LARK_BASE_TOKEN']}/tables/{env['LARK_PROGRESS_TABLE_ID']}/records"
    records = api.paged(path, query={"page_size": 50})
    api.http.session.close()
    return [{**{name: text(record["fields"].get(name)) for name in TEXT_FIELDS},
             "最新收件时间": record["fields"].get("最新收件时间")} for record in records]


def ball(row):
    return row["球在谁方"] if row["球在谁方"] in BALLS[:3] else BALLS[3]


def counts(rows):
    return [sum(ball(row) == key for row in rows) for key in BALLS]


def count_line(rows, personal=False):
    own, customer, compliance, unknown = counts(rows)
    return (f"{'需你跟进' if personal else '需我方跟进'}：{own}｜等客户：{customer}｜"
            f"等合规：{compliance}｜球权待确认：{unknown}")


def entry(row, index, now):
    identity = " ｜ ".join(row[name] for name in TEXT_FIELDS[:3] if row[name])
    lines = [f"**{index}. {identity}**"]
    if row["最新收件时间"]:
        received = datetime.fromtimestamp(row["最新收件时间"] / 1000, tz=HK)
        days = (now - received).days
        age = f"距今 {days} 天" if days else "距今不足 1 天"
        if days > 7:
            age = f"<font color='red'>{age}</font>"
        lines.append(f"最新邮件：{received:%Y-%m-%d %H:%M}｜{age}")
    lines.append(f"摘要：{row['简要描述']}")
    if row["卡点"]:
        lines.append(row["卡点"])
    return "\n".join(lines)


def div(content):
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def card(title, color, elements):
    return {"config": {"wide_screen_mode": True},
            "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def progress_url(env):
    parsed = urlsplit(env["LARK_BASE_URL"])
    query = dict(parse_qsl(parsed.query))
    query.pop("view", None)
    query.pop("record", None)
    query["table"] = env["LARK_PROGRESS_TABLE_ID"]
    return urlunsplit(parsed._replace(query=urlencode(query), fragment=""))


def build_cards(rows, now, table_url):
    overview = [div(f"表格读取时间：{now:%Y-%m-%d %H:%M:%S}（香港时间）"),
                div(f"**共 {len(rows)} 笔进度记录**\n{count_line(rows)}"),
                {"tag": "action", "actions": [{"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "查看开户进度表"}, "url": table_url}]}]
    cards = [card(f"开户跟进晨报｜{now:%Y-%m-%d}", "blue", overview)]
    groups = {}
    for row in rows:
        groups.setdefault(row["经办人"] or "未分配", []).append(row)
    for handler in sorted(groups, key=lambda name: (name == "未分配", name.casefold(), name)):
        group = groups[handler]
        elements = [div(count_line(group, personal=True))]
        for key, label in zip(BALLS, LABELS):
            items = sorted((row for row in group if ball(row) == key),
                           key=lambda row: row["最新收件时间"] or 0, reverse=True)
            if items:
                elements.extend([{"tag": "hr"}, div(f"**{label}（{len(items)}）**")])
                elements.extend(div(entry(row, index, now)) for index, row in enumerate(items, 1))
        color = COLORS[int.from_bytes(hashlib.sha256(handler.encode()).digest()[:2], "big") % len(COLORS)]
        cards.append(card(f"{handler}｜{len(group)} 笔进度记录", color, elements))
    return cards


def send_cards(env, recipients, cards):
    http = LarkHTTP(env)
    token = http.send("POST", http.domain + "/open-apis/auth/v3/tenant_access_token/internal",
                      payload={"app_id": env["LARK_APP_ID"], "app_secret": env["LARK_APP_SECRET"]})["tenant_access_token"]
    for recipient in recipients:
        for item in cards:
            result = http.send("POST", http.domain + "/open-apis/im/v1/messages?receive_id_type=email",
                               payload={"receive_id": recipient, "msg_type": "interactive",
                                        "content": json.dumps(item, ensure_ascii=False)},
                               headers={"Authorization": "Bearer " + token})
            print(json.dumps({"recipient": recipient, "message_id": result["data"]["message_id"]}, ensure_ascii=False))
    http.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    parser.add_argument("--config", type=Path, default=ROOT / "morning_report_config.json")
    args = parser.parse_args()
    env = read_env(args.env)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    rows = read_progress(env)
    cards = build_cards(rows, datetime.now(HK), progress_url(env))
    send_cards(env, config["recipients"], cards)


if __name__ == "__main__":
    main()
