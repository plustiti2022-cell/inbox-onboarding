"""Mail -> classification -> three-field OR match -> create/update -> mail status."""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

from model import validate_result
from storage import LATEST_RECEIVED_TIME, MAIL_TABLE, MANUAL_REVIEW, PROGRESS_TABLE, TERMINAL_MAIL_STATUSES, RecordStore

IDENTITY_FIELDS = {"公司名称": "company_name", "客户名称": "customer_name", "邮件": "email"}
DEFAULT_HANDLER = "james.yang@hashkey.com"
HANDLER_EMAILS = {
    DEFAULT_HANDLER,
    "prance.wang@hashkey.com",
    "zishan.feng@hashkey.com",
    "jx.yao@hashkey.com",
    "ingrid.lin@hashkey.com",
    "kathy.sun@hashkey.com",
    "bruce.li@hashkey.com",
    "fery.li@hashkey.com",
    "meilin.wu@hashkey.com",
}


def select_handler(mail: dict) -> str:
    for field in ("from.emailAddress.address", "toRecipients.addresses", "ccRecipients.addresses"):
        addresses = (mail.get(field) or "").casefold().split(",")
        matches = [address.strip() for address in addresses if address.strip() in HANDLER_EMAILS]
        if matches:
            selected = next((address for address in matches if address != DEFAULT_HANDLER), matches[0])
            return selected.split("@", 1)[0]
    return DEFAULT_HANDLER.split("@", 1)[0]


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def mail_time(record: dict) -> datetime:
    value = record["fields"].get("receivedDateTime")
    if not isinstance(value, str) or not value:
        raise ValueError("Mail is missing receivedDateTime")
    time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if time.tzinfo is None:
        raise ValueError("receivedDateTime must include a timezone")
    return time.astimezone(timezone.utc)


class ManualReviewRequired(ValueError):
    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def find_customer(records: list[dict], result: dict) -> dict | None:
    """Any nonblank same-field exact match counts. Never choose a conflicting row."""
    wanted = {field: normalized(result[key]) for field, key in IDENTITY_FIELDS.items() if normalized(result[key])}
    if not wanted:
        raise ManualReviewRequired("Onboarding mail has no identifiable company, customer name or email",
                                   "无法识别开户公司、客户姓名或客户邮箱。")
    matches = [record for record in records if any(
        normalized(record["fields"].get(field)) == value for field, value in wanted.items())]
    if len(matches) > 1:
        record_ids = ", ".join(r["record_id"] for r in matches)
        raise ManualReviewRequired("Customer match conflicts across records: " + record_ids,
                                   f"命中多笔开户进度记录（{record_ids}）。")
    return matches[0] if matches else None


def progress_patch(result: dict) -> dict:
    patch = {field: result[key] for field, key in IDENTITY_FIELDS.items() if result[key]}
    if result["ball_in_court"]:
        patch["球在谁方"] = result["ball_in_court"]
    if result["summary"] is not None:
        patch["简要描述"] = result["summary"]
    if result["handler"] is not None:
        patch["经办人"] = result["handler"]
    if result["blocker_resolved"]:
        patch["卡点"] = ""
    elif result["blocker"] is not None:
        patch["卡点"] = result["blocker"]
    return patch


def process_pending(store: RecordStore, analyzer, limit: int | None = None, on_event=None,
                    *, retain_items=True, report=None) -> dict:
    if hasattr(store, "pending_snapshot"):
        total, pending = store.pending_snapshot(limit)
    else:
        pending = [r for r in store.list_records(MAIL_TABLE) if r["fields"].get("AI处理") not in TERMINAL_MAIL_STATUSES]
        # Validate/sort before making updates; even old descending exports work.
        pending.sort(key=mail_time)
        if limit is not None:
            pending = pending[:limit]
        total = len(pending)
    if report is None:
        report = {}
    report.update(processed=0, onboarding=0, non_onboarding=0, created=0, updated=0, manual_review=0, failed=0)
    if retain_items:
        report["items"] = []
    for index, record in enumerate(pending, 1):
        mail_id = record["record_id"]
        event = {"index": index, "total": total, "mail_record_id": mail_id,
                 "internetMessageId": record["fields"].get("internetMessageId", "")}
        if on_event:
            on_event({**event, "state": "analyzing"})
        try:
            progress = (store.customer_identities() if hasattr(store, "customer_identities")
                        else store.list_records(PROGRESS_TABLE))
            # Pass identities only; no prior progress, blocker, handler or mail history.
            known = [{field: r["fields"].get(field, "") for field in IDENTITY_FIELDS} for r in progress]
            result = validate_result(analyzer.analyze(record["fields"], known))
            event["result"] = result
            status, reason = "AI已处理", result["reason"]
            if result["is_onboarding"]:
                try:
                    target = find_customer(progress, result)
                except ManualReviewRequired as review:
                    status = MANUAL_REVIEW
                    reason = f"{review.reason} {reason}"
                    event["action"] = "manual_review"
                else:
                    message_id = event["internetMessageId"]
                    if not isinstance(message_id, str) or not message_id.strip():
                        raise ValueError("Onboarding mail is missing internetMessageId")
                    result["handler"] = select_handler(record["fields"])
                    patch = progress_patch(result)
                    patch["internetMessageId"] = message_id
                    patch[LATEST_RECEIVED_TIME] = record["fields"]["receivedDateTime"]
                    if target is None:
                        target = store.create_record(PROGRESS_TABLE, patch)
                        event["action"] = "created"
                    else:
                        store.update_record(PROGRESS_TABLE, target["record_id"], patch)
                        event["action"] = "updated"
                    event["progress_record_id"] = target["record_id"]
            else:
                event["action"] = "non_onboarding"
            store.update_record(MAIL_TABLE, mail_id, {
                "AI处理": status, "是否为开户邮件": "是" if result["is_onboarding"] else "否", "处理说明": reason,
            })
        except (ValueError, RuntimeError) as error:
            report["failed"] += 1
            event["error"] = str(error)
            store.update_record(MAIL_TABLE, mail_id, {"AI处理": "AI失败", "处理说明": str(error)})
            event["action"] = "failed"
        # One checkpoint writes both local tables. Save errors stop the run.
        store.flush()
        if event["action"] != "failed":
            report["processed"] += 1
            if result["is_onboarding"]:
                report["onboarding"] += 1
                report[event["action"]] += 1
            else:
                report["non_onboarding"] += 1
        if retain_items:
            report["items"].append(event)
        if on_event:
            on_event({**event, "state": "finished"})
        # Do not let a later mail overtake a failed earlier mail. Otherwise a
        # subsequent resume would apply the older update after the newer one.
        if event["action"] == "failed":
            break
    return report
