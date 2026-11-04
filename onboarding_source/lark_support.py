"""Direct Lark HTTPS/OAuth. No CLI or global CLI credentials are used."""
from __future__ import annotations
import base64
import ctypes
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

PROJECT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)
LARK_MAX_RETRIES = 5
LARK_RETRY_SECONDS = 10
LARK_TRANSIENT_CODES = {1254290, 1254291, 1254607, 1255001, 1255002, 1255040}


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def update_env_file(path: Path, updates: dict) -> None:
    remaining, output = dict(updates), []
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match[1] in updates:
            if match[1] in remaining:
                value = remaining.pop(match[1])
                if value is not None:
                    output.append(match[1] + "=" + str(value))
        else:
            output.append(line)
    output.extend(k + "=" + str(v) for k, v in remaining.items() if v is not None)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")

class LarkError(RuntimeError):
    def __init__(self, message, code=None, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LarkRunLock:
    """Prevent two local workers from writing the same Base out of order."""
    def __init__(self, app_id):
        import msvcrt
        path = PROJECT / ".runtime" / "lark" / app_id / "workflow.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        if path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise LarkError("Another local worker is already processing this application's Base", "local_worker_busy") from None

    def close(self):
        import msvcrt
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()

class SecureJSON:
    """Windows DPAPI protects project-scoped user/device tokens at rest."""
    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def crypt(data: bytes, decrypt=False):
        if os.name != "nt":
            raise LarkError("This credential store requires Windows DPAPI")
        class Blob(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_byte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        output = Blob()
        function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
        if not ok:
            raise LarkError("Windows credential protection failed")
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            ctypes.windll.kernel32.LocalFree(output.data)

    def load(self):
        return json.loads(self.crypt(self.path.read_bytes(), True)) if self.path.exists() else None

    def save(self, data):
        atomic_write(self.path, self.crypt(json.dumps(data, ensure_ascii=False).encode()))

class LarkHTTP:
    def __init__(self, env, session=None):
        import requests
        self.env, self.session = env, session or requests.Session()
        brand = env.get("LARK_BRAND", "lark")
        if brand not in ("lark", "feishu"):
            raise ValueError("LARK_BRAND must be lark or feishu")
        self.domain = "https://open.larksuite.com" if brand == "lark" else "https://open.feishu.cn"
        self.accounts = "https://accounts.larksuite.com" if brand == "lark" else "https://accounts.feishu.cn"

    def _request_error(self, method, url, detail, *, response=None, code=None, retryable=False):
        status = response.status_code if response is not None else None
        code_display = code if code is not None else "unavailable"
        message = (f"Lark request failed: {method} {urlsplit(url).path} "
                   f"http_status={status or 'unavailable'} lark_code={code_display}; {detail}")
        # Log immediately so a later failure while marking the mail does not hide this one.
        logger.error("%s", message)
        transient_status = status is not None and (status in (408, 429) or 500 <= status < 600)
        transient_code = isinstance(code, int) and code in LARK_TRANSIENT_CODES
        return LarkError(message, code, retryable=retryable or transient_status or transient_code)

    def send(self, method, url, *, payload=None, form=None, headers=None, allow_error=False):
        import requests
        try:
            response = self.session.request(method, url, json=payload, data=form, headers=headers or {}, timeout=(10, 60))
        except requests.RequestException as error:
            exception_type = type(error).__name__
            cause = error.__cause__ or error.__context__
            if cause is not None:
                exception_type += f" ({type(cause).__name__})"
            detail = f"network_error={exception_type}"
            transient = isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                                           requests.exceptions.ChunkedEncodingError)) and not isinstance(error, requests.exceptions.SSLError)
            raise self._request_error(method, url, detail, response=error.response, retryable=transient) from None
        try:
            data = response.json()
        except ValueError:
            raise self._request_error(method, url, "non-JSON response; inspect cloud state before retrying a write",
                                      response=response) from None
        if not isinstance(data, dict):
            raise self._request_error(method, url, "invalid response body", response=response)
        failed = response.status_code >= 400 or data.get("code", 0) != 0 or bool(data.get("error"))
        if failed and not allow_error:
            message = str(data.get("msg") or data.get("error_description") or data.get("error") or "request failed")
            for key in ("LARK_APP_SECRET", "LARK_CALLBACK_VERIFICATION_TOKEN"):
                if self.env.get(key):
                    message = message.replace(self.env[key], "[REDACTED]")
            raise self._request_error(method, url, message[:500], response=response, code=data.get("code"))
        return data

class LarkAuth:
    def __init__(self, env, *, http=None, vault=None, pending=None):
        self.env, self.http = env, http or LarkHTTP(env)
        self.app_id, self.secret = env.get("LARK_APP_ID"), env.get("LARK_APP_SECRET")
        if not self.app_id or not self.secret:
            raise ValueError("Missing LARK_APP_ID / LARK_APP_SECRET")
        root = PROJECT / ".runtime" / "lark" / self.app_id
        self.vault = vault or SecureJSON(root / "user_token.dpapi")
        self.pending = pending or SecureJSON(root / "pending_auth.dpapi")
        self.lock = threading.RLock()

    def start_authorization(self):
        basic = base64.b64encode((self.app_id + ":" + self.secret).encode()).decode()
        data = self.http.send("POST", self.http.accounts + "/oauth/v1/device_authorization",
            form={"client_id": self.app_id, "scope": self.env.get("LARK_USER_SCOPES", "bitable:app offline_access")},
            headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
        payload = data.get("data", data)
        if not payload.get("device_code") or not payload.get("verification_uri_complete", payload.get("verification_uri")):
            raise LarkError("Lark did not return a complete device authorization")
        self.pending.save({**payload, "app_id": self.app_id, "created_at": time.time()})
        return {k: payload.get(k) for k in ("verification_uri", "verification_uri_complete", "user_code", "expires_in", "interval")}

    def _oauth(self, fields, allow_error=False):
        # Lark international accepts device-code and refresh grants on v2.
        return self.http.send("POST", self.http.domain + "/open-apis/authen/v2/oauth/token",
            form={"client_id": self.app_id, "client_secret": self.secret, **fields},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, allow_error=allow_error)

    def _save_token(self, data, previous=None):
        payload = data.get("data", data)
        if not payload.get("access_token"):
            raise LarkError("Lark did not return a user access token")
        self.vault.save({**payload, "app_id": self.app_id,
            "expires_at": time.time() + int(payload.get("expires_in", 7200)),
            "refresh_token": payload.get("refresh_token") or (previous or {}).get("refresh_token")})

    def finish_authorization(self, timeout=60):
        pending = self.pending.load()
        if not pending or pending.get("app_id") != self.app_id:
            raise LarkError("Start a new user authorization first")
        end = min(time.time() + timeout, pending["created_at"] + int(pending.get("expires_in", 240)))
        while time.time() < end:
            data = self._oauth({"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": pending["device_code"]}, True)
            if (data.get("data") or data).get("access_token"):
                self._save_token(data)
                token = self.vault.load()
                return {"authorized": True, "identity": "user", "app_id": self.app_id,
                        "refresh_token_issued": bool(token.get("refresh_token"))}
            error = data.get("error") or data.get("msg")
            if error not in ("authorization_pending", "slow_down") and data.get("code") != 20094:
                raise LarkError("User authorization failed: " + str(error)[:250], data.get("code"))
            time.sleep(min(10, max(1, int(pending.get("interval", 5)))))
        raise LarkError("User authorization is pending or expired")

    def access_token(self):
        with self.lock:
            data = self.vault.load()
            if not data or data.get("app_id") != self.app_id:
                raise LarkError("User authorization required: run setup_lark.py --authorize")
            if data.get("expires_at", 0) <= time.time() + 120:
                if not data.get("refresh_token"):
                    raise LarkError("User token expired without refresh token; authorize this application again")
                try:
                    refreshed = self._oauth({"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
                except LarkError:
                    # The other batch may have rotated this refresh token first.
                    latest = self.vault.load() or {}
                    if (latest.get("app_id") == self.app_id and latest.get("access_token")
                            and latest["access_token"] != data["access_token"]
                            and latest.get("expires_at", 0) > time.time()):
                        return latest["access_token"]
                    raise
                self._save_token(refreshed, data)
                data = self.vault.load()
            return data["access_token"]

class LarkAPI:
    def __init__(self, env, *, auth=None, http=None):
        self.http = http or LarkHTTP(env)
        self.auth = auth or LarkAuth(env, http=self.http)

    def request(self, method, path, *, body=None, query=None):
        method = method.upper()
        record_path = re.fullmatch(r"/open-apis/bitable/v1/apps/[^/]+/tables/[^/]+/records(?:/([^/]+))?", path)
        retry_safe = method == "GET"
        if record_path:
            if method == "POST" and record_path.group(1) is None:
                query = dict(query or {})
                # Reuse the same key after an ambiguous write result.
                query.setdefault("client_token", str(uuid.uuid4()))
                retry_safe = True
            elif method == "PUT" and record_path.group(1) is not None:
                retry_safe = True
        url = self.http.domain + path
        if query:
            url += "?" + urlencode(query)
        for attempt in range(LARK_MAX_RETRIES + 1):
            headers = {"Authorization": "Bearer " + self.auth.access_token()}
            try:
                data = self.http.send(method, url, payload=body, headers=headers)
                return data.get("data", {})
            except LarkError as error:
                if not retry_safe or not error.retryable or attempt == LARK_MAX_RETRIES:
                    raise
                logger.warning("Retrying Lark %s %s in %s seconds (%s/%s)",
                               method, path, LARK_RETRY_SECONDS, attempt + 1, LARK_MAX_RETRIES)
                time.sleep(LARK_RETRY_SECONDS)

    def paged(self, path, *, query=None):
        return [item for page in self.iter_pages(path, query=query) for item in page]

    def iter_pages(self, path, *, query=None):
        page_token, seen = None, set()
        while True:
            args = {"page_size": 500, **(query or {})}
            if page_token:
                args["page_token"] = page_token
            data = self.request("GET", path, query=args)
            yield data.get("items", [])
            if not data.get("has_more"):
                return
            page_token = data.get("page_token")
            if not page_token or page_token in seen:
                raise LarkError("Invalid pagination token; complete cloud read could not be verified")
            seen.add(page_token)
            del data
