"""Authorized chat instruction -> seven-key JSON -> confirmed Bitable update."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from model import validate_result
from storage import LATEST_RECEIVED_TIME, PROGRESS_TABLE, SHANGHAI
from workflow import IDENTITY_FIELDS, ManualReviewRequired, find_customer, progress_patch


class ManualUpdater:
    def __init__(self, client, tools):
        self.client, self.tools = client, tools
        self.prompt = Path(__file__).with_name("update_prompt.txt").read_text(encoding="utf-8")

    def run(self, instruction, *, on_event=None):
        def trace(event, **details):
            if on_event:
                on_event(event, **details)

        # Keep IDs locally; the model receives only the three customer identities.
        query = {"page_size": 50, "field_names": json.dumps(list(IDENTITY_FIELDS), ensure_ascii=False)}
        records = [self.tools.record(raw) for page in self.tools.api.iter_pages(
            self.tools.path(PROGRESS_TABLE) + "/records", query=query) for raw in page]
        known = [{field: row["fields"].get(field, "") for field in IDENTITY_FIELDS} for row in records]
        message = self.client.complete([
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": json.dumps({
                "修改指令": instruction, "公司名称|客户名称|邮件列表": known}, ensure_ascii=False)},
        ], json_mode=True)
        try:
            result = validate_result(json.loads(message["content"]))
        except (KeyError, TypeError, ValueError):
            trace("ai_output_invalid")
            return "未更新：AI 回傳格式不正確，請重新描述修改要求。"
        trace("ai_result", result=result)
        if not result["is_onboarding"]:
            return "未更新：請提供要修改的開戶客戶、欄位及新內容。"
        try:
            target = find_customer(records, result)
        except ManualReviewRequired:
            return "未更新：客戶身份不明或命中多筆記錄，請補充公司名稱、姓名或郵箱。"
        if target is None:
            return "未更新：找不到對應的開戶進度記錄，請核對客戶身份。"
        trace("record_matched", record_id=target["record_id"])
        fields = progress_patch(result, target["fields"])
        if not fields:
            return "未更新：沒有新的修改內容，原資料已保留。"
        updates = dict(fields)
        fields[LATEST_RECEIVED_TIME] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        fields["internetMessageId"] = "人工修改"
        trace("update_prepared", record_id=target["record_id"], fields=fields)
        try:
            self.tools.update_record(PROGRESS_TABLE, target["record_id"], fields)
        except RuntimeError:
            # An HTTP timeout may follow a committed write: never claim success or
            # automatically replay the instruction when confirmation is missing.
            return "更新未獲確認：請先核對表格結果，再決定是否重試。"
        trace("update_confirmed", record_id=target["record_id"])
        label = (updates.get("公司名称") or target["fields"].get("公司名称")
                 or updates.get("客户名称") or target["fields"].get("客户名称") or target["record_id"])
        changes = "；".join(f"{key} → {value if value else '已清空'}" for key, value in updates.items())
        return f"已更新 {label}：{changes}\n記錄：{target['record_id']}"
