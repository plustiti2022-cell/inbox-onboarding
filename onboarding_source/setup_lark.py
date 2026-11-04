"""Direct application/user authorization, Base creation, and event subscription."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from model import read_env
from lark_support import LarkAPI, LarkAuth, LarkHTTP, LarkError, PROJECT, update_env_file
from lark_store import field_schema
from storage import MAIL_TABLE, PROGRESS_TABLE


def create_tables(env_path, name):
    env = read_env(env_path)
    api = LarkAPI(env)
    base = env.get("LARK_BASE_TOKEN")
    created_now = not base
    if not base:
        data = api.request("POST", "/open-apis/bitable/v1/apps", body={"name": name, "time_zone": "Asia/Shanghai"})
        app = data["app"]
        base = app["app_token"]
        update_env_file(env_path, {"LARK_BASE_TOKEN": base, "LARK_BASE_URL": app["url"],
                                  "LARK_SETUP_DEFAULT_TABLE_ID": app.get("default_table_id", "")})
        env = read_env(env_path)
    tables = api.paged(f"/open-apis/bitable/v1/apps/{base}/tables", query={"page_size": 100})
    # The v1 create response may omit default_table_id. Discover the sole empty
    # default table immediately after creating our own Base, before adding data.
    if created_now and not env.get("LARK_SETUP_DEFAULT_TABLE_ID") and len(tables) == 1:
        default_id = tables[0]["table_id"]
        default_rows = api.paged(f"/open-apis/bitable/v1/apps/{base}/tables/{default_id}/records")
        if all(not row.get("fields") for row in default_rows):
            update_env_file(env_path, {"LARK_SETUP_DEFAULT_TABLE_ID": default_id})
            env = read_env(env_path)
    for table, key in ((MAIL_TABLE, "LARK_MAIL_TABLE_ID"), (PROGRESS_TABLE, "LARK_PROGRESS_TABLE_ID")):
        candidates = [t for t in tables if t["name"] == table]
        if len(candidates) > 1:
            raise LarkError("Multiple tables have the same target name")
        if candidates:
            table_id = candidates[0]["table_id"]
        else:
            data = api.request("POST", f"/open-apis/bitable/v1/apps/{base}/tables",
                               body={"table": {"name": table, "default_view_name": "全部记录", "fields": field_schema(table)}})
            table_id = data["table_id"]
        update_env_file(env_path, {key: table_id})
    # Only remove the automatically-created empty table of this newly-created Base.
    original = env.get("LARK_SETUP_DEFAULT_TABLE_ID")
    current = read_env(env_path)
    if original and original not in (current["LARK_MAIL_TABLE_ID"], current["LARK_PROGRESS_TABLE_ID"]):
        if any(t["table_id"] == original for t in tables):
            records = api.paged(f"/open-apis/bitable/v1/apps/{base}/tables/{original}/records")
            if all(not row.get("fields") for row in records):
                api.request("DELETE", f"/open-apis/bitable/v1/apps/{base}/tables/{original}")
    print(json.dumps({"created": True, "identity": "user", "base_url": current.get("LARK_BASE_URL"),
                      "mail_table": current["LARK_MAIL_TABLE_ID"], "progress_table": current["LARK_PROGRESS_TABLE_ID"]}, ensure_ascii=False))


def ensure_progress_message_id(env):
    base, table = env.get("LARK_BASE_TOKEN"), env.get("LARK_PROGRESS_TABLE_ID")
    if not base or not table:
        raise ValueError("Configure the Lark Base and progress table ID in .env")
    api = LarkAPI(env)
    path = f"/open-apis/bitable/v1/apps/{base}/tables/{table}/fields"
    try:
        fields = api.paged(path, query={"page_size": 100})
        field = next((f for f in fields if f["field_name"] == "internetMessageId"), None)
        created = field is None
        if created:
            field = api.request("POST", path, body={"field_name": "internetMessageId", "type": 1}).get("field", {})
        if not field.get("field_id") or field.get("type") != 1:
            raise LarkError("The progress table's internetMessageId must be a text field")
        return {"field": field["field_name"], "field_id": field["field_id"], "created": created}
    finally:
        api.http.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=PROJECT / ".env")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify-app", action="store_true")
    action.add_argument("--authorize", action="store_true")
    action.add_argument("--finish-authorization", action="store_true")
    action.add_argument("--create", action="store_true")
    action.add_argument("--add-progress-message-id", action="store_true", help="Add the source mail ID field to an existing progress table")
    action.add_argument("--subscribe", action="store_true")
    parser.add_argument("--name", default="开户邮件自动跟进")
    args = parser.parse_args(argv)
    env = read_env(args.env)
    if args.verify_app:
        http = LarkHTTP(env)
        data = http.send("POST", http.domain + "/open-apis/auth/v3/tenant_access_token/internal",
                         payload={"app_id": env["LARK_APP_ID"], "app_secret": env["LARK_APP_SECRET"]})
        print(json.dumps({"app_credentials_verified": bool(data.get("tenant_access_token")), "app_id": env["LARK_APP_ID"]}))
    elif args.authorize:
        data = LarkAuth(env).start_authorization()
        import qrcode
        directory = PROJECT / "outputs" / "lark" / "auth"
        directory.mkdir(parents=True, exist_ok=True)
        url = data.get("verification_uri_complete") or data["verification_uri"]
        image = directory / "user_authorization.png"
        qrcode.make(url).save(image)
        print(json.dumps({**data, "qr_image": str(image)}, ensure_ascii=False))
    elif args.finish_authorization:
        print(json.dumps(LarkAuth(env).finish_authorization(), ensure_ascii=False))
    elif args.create:
        create_tables(args.env, args.name)
    elif args.add_progress_message_id:
        print(json.dumps(ensure_progress_message_id(env), ensure_ascii=False))
    elif args.subscribe:
        base = env.get("LARK_BASE_TOKEN")
        if not base:
            raise ValueError("Create the Base first")
        data = LarkAPI(env).request("POST", f"/open-apis/drive/v1/files/{base}/subscribe", query={"file_type": "bitable"})
        print(json.dumps({"subscribed": True, "identity": "user", "result": data}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
