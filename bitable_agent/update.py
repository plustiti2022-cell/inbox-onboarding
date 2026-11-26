"""Authorized chat instruction -> structured JSON -> confirmed Bitable update."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from model import validate_result
from storage import LATEST_RECEIVED_TIME, PROGRESS_TABLE, SHANGHAI
from workflow import IDENTITY_FIELDS, ManualReviewRequired, find_customer, progress_patch

UPDATE_FIELDS = (*IDENTITY_FIELDS, "球在谁方", "状态", "卡点", "简要描述")
MULTI_RECORD_FLAG = "命中多笔交易记录"
UNCONFIRMED_UPDATE = "更新未獲確認：請先核對表格結果，再決定是否重試。"
SPLIT_PROMPT = """按原文顺序将开户进度更新拆分成独立指令，每项只涉及一笔记录。
支持编号、换行和自然语言。保留原文中的公司、姓名、邮箱、修改内容和明确限制，将共同要求带入每个相关项。
不要补写或推测身份、修改值或操作。无法确定修改分别属于哪笔记录时，返回空列表。
只返回 JSON 对象，唯一键为“修改指令列表”，值为字符串列表。"""


class ManualUpdater:
    def __init__(self, client, tools):
        self.client, self.tools = client, tools
        self.prompt = Path(__file__).with_name("update_prompt.txt").read_text(encoding="utf-8")

    def run(self, instruction, *, editor_name="", on_event=None):
        def trace(event, **details):
            if on_event:
                on_event(event, **details)

        return self._run_instruction(instruction, editor_name, trace, allow_split=True)

    def _run_instruction(self, instruction, editor_name, trace, *, allow_split):
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
            result = json.loads(message["content"])
            multiple = result.pop(MULTI_RECORD_FLAG) if isinstance(result, dict) else None
            if type(multiple) is not int or multiple not in (0, 1):
                raise ValueError("Expected a 0/1 multiple-record flag")
            result = validate_result(result, fields=UPDATE_FIELDS)
        except (KeyError, TypeError, ValueError):
            trace("ai_output_invalid")
            return "未更新：AI 回傳格式不正確，請重新描述修改要求。"
        trace("ai_result", result={**result, MULTI_RECORD_FLAG: multiple})
        if multiple == 1:
            if allow_split:
                return self._run_batch(instruction, editor_name, trace)
            return f"未更新：{MULTI_RECORD_FLAG}，請分開描述要更新的客戶。"
        if not result["is_onboarding"]:
            return "未更新：請提供要修改的開戶客戶、欄位及新內容。"
        try:
            target = find_customer(records, result)
        except ManualReviewRequired:
            return f"未更新：客戶身份不明或{MULTI_RECORD_FLAG}，請補充公司名稱、姓名或郵箱。"
        if target is None:
            return "未更新：找不到對應的開戶進度記錄，請核對客戶身份。"
        trace("record_matched", record_id=target["record_id"])
        fields = progress_patch(result, target["fields"], fields=UPDATE_FIELDS, clearable=("卡点",))
        if not fields:
            return "未更新：沒有新的修改內容，原資料已保留。"
        updates = dict(fields)
        fields[LATEST_RECEIVED_TIME] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        fields["internetMessageId"] = f"人工修改（{editor_name}）" if editor_name else "人工修改"
        trace("update_prepared", record_id=target["record_id"], fields=fields)
        try:
            self.tools.update_record(PROGRESS_TABLE, target["record_id"], fields)
        except ValueError:
            return "未更新：欄位或選項與 Lark 表格設定不符，請核對表格設定。"
        except RuntimeError:
            # An HTTP timeout may follow a committed write: never claim success or
            # automatically replay the instruction when confirmation is missing.
            return UNCONFIRMED_UPDATE
        trace("update_confirmed", record_id=target["record_id"])
        label = (updates.get("公司名称") or target["fields"].get("公司名称")
                 or updates.get("客户名称") or target["fields"].get("客户名称") or target["record_id"])
        changes = "；".join(f"{key} → {value if value else '已清空'}" for key, value in updates.items())
        return f"已更新 {label}：{changes}\n記錄：{target['record_id']}"

    def _run_batch(self, instruction, editor_name, trace):
        try:
            message = self.client.complete([
                {"role": "system", "content": SPLIT_PROMPT},
                {"role": "user", "content": instruction},
            ], json_mode=True)
            result = json.loads(message["content"])
            if not isinstance(result, dict) or set(result) != {"修改指令列表"}:
                raise ValueError("Invalid batch instructions object")
            items = result["修改指令列表"]
            if not isinstance(items, list) or len(items) < 2 or any(
                not isinstance(item, str) or not item.strip() for item in items
            ):
                raise ValueError("Expected at least two nonempty instructions")
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            trace("batch_split_failed", error_type=type(error).__name__)
            return f"未更新：{MULTI_RECORD_FLAG}，無法拆分，請分開描述各筆修改要求。"

        trace("batch_split", item_count=len(items))
        replies = []
        updated = 0
        for index, item in enumerate(items, 1):
            trace("batch_item_started", item_index=index)
            try:
                answer = self._run_instruction(item.strip(), editor_name, trace, allow_split=False)
            except Exception as error:
                trace("batch_item_failed", item_index=index, error_type=type(error).__name__)
                answer = UNCONFIRMED_UPDATE
            replies.append(f"{index}. {answer}")
            if answer == UNCONFIRMED_UPDATE:
                if index < len(items):
                    replies.append(f"後續 {len(items) - index} 項尚未處理。")
                break
            updated += answer.startswith("已更新")
        summary = f"{MULTI_RECORD_FLAG}，共拆分 {len(items)} 筆。"
        if updated:
            summary = f"已更新 {updated} 筆。{summary}"
        return "\n".join([summary, *replies])
