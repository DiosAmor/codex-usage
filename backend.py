"""Private account storage and read-only, isolated Codex usage polling."""

import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit
import uuid

from protocol import CodexSession, ProtocolError
from daily_usage import DAILY_USAGE_ERROR, normalize_daily_usage, pending_token_usage


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def window(value):
    if not isinstance(value, dict):
        return None
    used = number(value.get("usedPercent"))
    duration = number(value.get("windowDurationMins"))
    reset = number(value.get("resetsAt"))
    return {"usedPercent": used,
            "remainingPercent": max(0, min(100, 100 - used)) if used is not None else None,
            "windowDurationMins": duration if duration is not None and duration > 0 else None,
            "resetsAt": reset if reset is not None and reset > 0 else None}


def normalize_usage(data):
    multi = data.get("rateLimitsByLimitId")
    if isinstance(multi, dict) and multi:
        entries = multi.items()
    else:
        legacy = data.get("rateLimits")
        entries = [(legacy.get("limitId") or "codex", legacy)] if isinstance(legacy, dict) else []
    buckets = []
    for key, value in entries:
        if not isinstance(value, dict):
            continue
        credits = value.get("credits")
        safe_credits = None
        if isinstance(credits, dict):
            safe_credits = {k: credits.get(k) for k in ("hasCredits", "unlimited", "balance")}
        buckets.append({"id": str(key), "name": value.get("limitName") or str(key),
                        "plan": value.get("planType"), "primary": window(value.get("primary")),
                        "secondary": window(value.get("secondary")), "credits": safe_credits,
                        "rateLimitReachedType": value.get("rateLimitReachedType"),
                        "spendControlReached": value.get("spendControlReached")})
    summary = data.get("rateLimitResetCredits")
    reset_credits = None
    if isinstance(summary, dict):
        count = number(summary.get("availableCount"))
        count = int(count) if count is not None and count >= 0 and count == int(count) else None
        details = summary.get("credits")
        reset_credits = {"availableCount": count, "credits": [
            {"title": item.get("title"), "status": item.get("status"),
             "grantedAt": number(item.get("grantedAt")), "expiresAt": number(item.get("expiresAt"))}
            for item in details if isinstance(item, dict)] if isinstance(details, list) else None}
    return {"buckets": buckets, "resetCredits": reset_credits,
            "ordinaryUsageAllowed": data.get("ordinaryUsageAllowed") if isinstance(data.get("ordinaryUsageAllowed"), bool) else None}


def claims(token):
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, IndexError, AttributeError):
        return {}


def read_auth(path):
    """Local identity hints only; JWT signatures are validated by Codex services."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("인증 심볼릭 링크는 지원하지 않습니다.")
    try:
        data = json.loads(path.read_bytes())
        tokens = data["tokens"]
        if data.get("OPENAI_API_KEY") or data.get("auth_mode") not in (None, "chatgpt"):
            raise ValueError
        access = tokens["access_token"]
        workspace = tokens["account_id"]
        if not isinstance(access, str) or not access or not isinstance(workspace, str) or not workspace:
            raise ValueError
        identity_claims = claims(tokens.get("id_token"))
        access_claims = claims(access)
        subject = identity_claims.get("sub") or access_claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError
        metadata = identity_claims.get("https://api.openai.com/auth", {})
        if metadata.get("chatgpt_account_id", workspace) != workspace:
            raise ValueError
        email = identity_claims.get("email") or access_claims.get("https://api.openai.com/profile", {}).get("email")
        return {"identity": (workspace, subject), "email": email,
                "plan": metadata.get("chatgpt_plan_type", "unknown"),
                "expiry": number(access_claims.get("exp")) or 0,
                "accessToken": access, "chatgptAccountId": workspace,
                "chatgptPlanType": metadata.get("chatgpt_plan_type")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("지원하는 ChatGPT 로그인 파일을 읽지 못했습니다.") from None


def private_dir(path):
    if path.is_symlink():
        raise ValueError("로그인 저장소의 심볼릭 링크는 지원하지 않습니다.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("로그인 저장소의 소유자가 다릅니다.")
    path.chmod(0o700)


def private_json(path, value):
    if path.is_symlink():
        raise ValueError("저장 파일의 심볼릭 링크는 지원하지 않습니다.")
    fd, name = tempfile.mkstemp(prefix=".save-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class DashboardManager:
    def __init__(self, data_dir, codex_binary="codex", refresh_interval=300,
                 *, source_home=None, auto_start=True, session_factory=CodexSession):
        self.root = Path(data_dir).absolute()
        self.source_home = Path(source_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
        if self.root.resolve() == self.source_home.resolve():
            raise ValueError("대시보드 로그인 폴더는 Codex 폴더와 달라야 합니다.")
        private_dir(self.root)
        private_dir(self.root / "accounts")
        self.binary = codex_binary
        self.interval = refresh_interval
        self.session_factory = session_factory
        self.lock = threading.RLock()
        self.account_locks = {}
        self.accounts = {}
        self.logins = {}
        self.sessions = set()
        self.worker = None
        self.ignored = set()
        self.refreshing = False
        self._refresh_pending = False
        self.closed = threading.Event()
        self.next_refresh = time.time()
        self._load()
        self._discover()
        self.scheduler = None
        if auto_start:
            self.scheduler = threading.Thread(target=self._schedule, daemon=True)
            self.scheduler.start()

    @staticmethod
    def _id(identity):
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]

    def _account(self, auth, label, source, home=None, sources=None):
        account_id = self._id(auth["identity"])
        return {"id": account_id, "label": label, "email": auth.get("email"), "plan": auth.get("plan"),
                "source": source, "removable": True, "status": "pending", "lastSuccessAt": None,
                "lastAttemptAt": None, "error": None, "buckets": [], "resetCredits": None,
                "ordinaryUsageAllowed": None, "tokenUsage": pending_token_usage(),
                "_identity": auth["identity"], "_home": home,
                "_sources": sources or []}

    def _load(self):
        manifest = self.root / "accounts.json"
        if not manifest.exists():
            return
        if manifest.is_symlink():
            raise ValueError("저장 파일의 심볼릭 링크는 지원하지 않습니다.")
        data = json.loads(manifest.read_text())
        self.ignored = set(data.get("ignored", []))
        for item in data.get("accounts", []):
            directory = item.get("directory", "")
            if not isinstance(directory, str) or len(directory) != 32 or any(c not in "0123456789abcdef" for c in directory):
                continue
            home = self.root / "accounts" / directory
            if home.is_symlink():
                continue
            try:
                auth = read_auth(home / "auth.json")
            except ValueError:
                continue
            account = self._account(auth, str(item.get("label") or "계정")[:60], "dashboard", home)
            self.accounts[account["id"]] = account

    def _save(self):
        data = {"version": 1, "ignored": sorted(self.ignored), "accounts": [
            {"label": account["label"], "directory": account["_home"].name}
            for account in self.accounts.values() if account["source"] == "dashboard"]}
        private_json(self.root / "accounts.json", data)

    def _discover(self):
        forced_workspace = None
        config_path = self.source_home / "config.toml"
        try:
            config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
            if config.get("forced_login_method") == "api" or config.get("chatgpt_base_url"):
                return
            forced_workspace = config.get("forced_chatgpt_workspace_id")
        except (OSError, tomllib.TOMLDecodeError):
            return
        sources = [self.source_home / "auth.json"]
        store = self.source_home / "workspace-logins"
        if store.is_dir() and not store.is_symlink():
            sources.extend(sorted(store.glob("*.json")))
        grouped = {}
        for source in sources:
            try:
                auth = read_auth(source)
            except ValueError:
                continue
            if forced_workspace and auth["chatgptAccountId"] != forced_workspace:
                continue
            key = self._id(auth["identity"])
            grouped.setdefault(key, []).append((source, auth))
        with self.lock:
            for key, candidates in grouped.items():
                if key in self.ignored:
                    continue
                ordered = sorted(candidates, key=lambda pair: pair[1]["expiry"], reverse=True)
                source, auth = ordered[0]
                if key not in self.accounts:
                    label = "현재 Codex 계정" if source == sources[0] else f"저장된 {source.stem} 계정"
                    self.accounts[key] = self._account(auth, label, "existing", sources=[pair[0] for pair in ordered])
                elif self.accounts[key]["source"] == "existing":
                    self.accounts[key]["_sources"] = [pair[0] for pair in ordered]

    def _external_tokens(self, account):
        candidates = []
        for path in account["_sources"]:
            try:
                auth = read_auth(path)
            except ValueError:
                continue
            if auth["identity"] == account["_identity"]:
                candidates.append(auth)
        if not candidates:
            raise ValueError("기존 로그인을 읽을 수 없습니다. 계정 추가로 다시 연결해 주세요.")
        auth = max(candidates, key=lambda item: item["expiry"])
        if auth["expiry"] and auth["expiry"] <= time.time() + 30:
            raise ValueError("저장된 로그인이 만료되었습니다. 계정 추가로 다시 로그인해 주세요.")
        return {k: auth[k] for k in ("accessToken", "chatgptAccountId", "chatgptPlanType")}

    def _new_session(self, home, refresh_tokens=None):
        session = self.session_factory(home, self.binary, refresh_tokens=refresh_tokens)
        with self.lock:
            if self.closed.is_set():
                session.close()
                raise ProtocolError("대시보드가 종료되었습니다.")
            self.sessions.add(session)
        return session

    @contextlib.contextmanager
    def _managed_lock(self, account):
        if account["source"] != "dashboard":
            yield
            return
        # Separate status/watch/browser processes must not rotate the same login concurrently.
        path = account["_home"] / ".usage.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self.closed.wait(0.1):
                        raise ProtocolError("대시보드가 종료되었습니다.")
            yield

    def _close_session(self, session):
        session.close()
        with self.lock:
            self.sessions.discard(session)

    def _poll_one(self, account_id):
        with self.lock:
            account = self.accounts.get(account_id)
            if not account:
                return
            guard = self.account_locks.setdefault(account_id, threading.Lock())
        with guard:
            with self.lock:
                account = self.accounts.get(account_id)
                if not account:
                    return
                self.accounts[account_id]["lastAttemptAt"] = time.time()
            session = None
            temporary = None
            lock_stack = contextlib.ExitStack()
            try:
                # Keep the credential file locked until the session has been stopped.
                lock_stack.enter_context(self._managed_lock(account))
                if account["source"] == "existing":
                    tokens = self._external_tokens(account)
                    temporary = tempfile.TemporaryDirectory(prefix=".probe-", dir=self.root)
                    home = Path(temporary.name)
                    session = self._new_session(home, lambda: self._external_tokens(account))
                    session.request("account/login/start", {"type": "chatgptAuthTokens", **tokens})
                else:
                    session = self._new_session(account["_home"])
                info = session.request("account/read", {"refreshToken": False}).get("account")
                if not isinstance(info, dict) or info.get("type") != "chatgpt":
                    raise ProtocolError("로그인을 확인할 수 없습니다. 계정을 다시 연결해 주세요.")
                usage = session.request("account/rateLimits/read", {}, timeout=40)
                if usage.get("accountId") and usage["accountId"] != account["_identity"][0]:
                    raise ProtocolError("계정과 사용량 응답이 일치하지 않습니다.")
                normalized = normalize_usage(usage)
                with self.lock:
                    if self.accounts.get(account_id) is account:
                        account.update(normalized)
                        account.update(status="ok", error=None, lastSuccessAt=time.time())
                        account["email"] = info.get("email") or account["email"]
                        account["plan"] = info.get("planType") or account["plan"]
                # Daily history is optional. Its failure must not make successful
                # remaining-quota polling look like an account login failure.
                self._poll_daily_usage(account_id, account, session)
            except Exception as error:
                safe = str(error) if isinstance(error, (ProtocolError, ValueError)) else "조회를 완료하지 못했습니다. 로그인과 네트워크를 확인해 주세요."
                with self.lock:
                    if self.accounts.get(account_id) is account:
                        account.update(status="error", error=safe)
                        account["tokenUsage"].update(status="error", error=DAILY_USAGE_ERROR,
                                                     lastAttemptAt=account["lastAttemptAt"])
            finally:
                if session:
                    self._close_session(session)
                if temporary:
                    temporary.cleanup()
                lock_stack.close()

    def _poll_daily_usage(self, account_id, account, session):
        attempted = time.time()
        with self.lock:
            if self.accounts.get(account_id) is not account or self.closed.is_set():
                return
            account["tokenUsage"]["lastAttemptAt"] = attempted
        try:
            daily = normalize_daily_usage(session.request("account/usage/read", {}, timeout=40))
            with self.lock:
                if self.accounts.get(account_id) is account:
                    account["tokenUsage"].update(daily)
                    account["tokenUsage"].update(fetchedAt=time.time(), error=None)
        except Exception:
            # Do not forward remote errors, URLs, credentials, or unsupported
            # method details to the browser. Existing successful rows remain.
            with self.lock:
                if self.accounts.get(account_id) is account:
                    account["tokenUsage"].update(status="error", error=DAILY_USAGE_ERROR)

    def refresh(self, *, after_current=False):
        with self.lock:
            if self.closed.is_set():
                return self.state()
            if self.refreshing:
                if after_current:
                    self._refresh_pending = True
                return self.state()
            self.refreshing = True
            self.worker = threading.Thread(target=self._refresh, daemon=True)
            self.worker.start()
        return self.state()

    def _refresh(self):
        try:
            while True:
                if not self.closed.is_set():
                    self._discover()
                    with self.lock:
                        ids = list(self.accounts)
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        list(pool.map(self._poll_one, ids))
                with self.lock:
                    if self._refresh_pending and not self.closed.is_set():
                        # Keep refreshing true across both passes so callers
                        # cannot mistake the earlier account snapshot for final.
                        self._refresh_pending = False
                        continue
                    self.refreshing = False
                    self._refresh_pending = False
                    self.next_refresh = time.time() + self.interval
                    return
        except BaseException:
            with self.lock:
                self.refreshing = False
                self._refresh_pending = False
                self.next_refresh = time.time() + self.interval
            raise

    def _schedule(self):
        while not self.closed.is_set():
            if time.time() >= self.next_refresh:
                self.refresh()
            self.closed.wait(1)

    def state(self):
        with self.lock:
            return copy.deepcopy({"accounts": [
                {key: value for key, value in account.items() if not key.startswith("_")}
                for account in self.accounts.values()],
                "logins": [{k: v for k, v in item.items() if not k.startswith("_")}
                           for item in self.logins.values()],
                "refreshing": self.refreshing, "refreshInterval": self.interval,
                "nextRefreshAt": self.next_refresh, "now": time.time(), "demo": False})

    def add_account(self, label):
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
            raise ValueError("계정 이름을 1~60자로 입력해 주세요.")
        with self.lock:
            if any(item["status"] == "pending" for item in self.logins.values()):
                raise ValueError("진행 중인 로그인을 먼저 완료하거나 취소해 주세요.")
            directory = uuid.uuid4().hex
            home = self.root / "accounts" / directory
            private_dir(home)
            login_id = uuid.uuid4().hex
            item = {"id": login_id, "label": label.strip(), "status": "pending",
                    "verificationUrl": None, "userCode": None, "createdAt": time.time(),
                    "expiresAt": None, "error": None, "accountId": None,
                    "_home": home, "_session": None, "_server_login_id": None}
            self.logins[login_id] = item
        try:
            session = self._new_session(home)
            item["_session"] = session
            response = session.request("account/login/start", {"type": "chatgptDeviceCode"}, timeout=30)
            url = response.get("verificationUrl", "")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "auth.openai.com":
                raise ProtocolError("공식 로그인 주소를 확인하지 못했습니다.")
            with self.lock:
                item.update(verificationUrl=url, userCode=response.get("userCode"),
                            _server_login_id=response.get("loginId"))
                if not item["_server_login_id"] or not item["userCode"]:
                    raise ProtocolError("로그인 코드를 받지 못했습니다.")
            threading.Thread(target=self._finish_login, args=(item,), daemon=True).start()
        except Exception:
            with self.lock:
                item.update(status="failed", error="로그인을 시작하지 못했습니다. 네트워크와 Codex 설치를 확인해 주세요.")
            if item["_session"]:
                self._close_session(item["_session"])
            shutil.rmtree(home, ignore_errors=True)
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _finish_login(self, item):
        session = item["_session"]
        retained = False
        try:
            session.wait_login(item["_server_login_id"])
            auth_file = item["_home"] / "auth.json"
            auth = read_auth(auth_file)
            auth_file.chmod(0o600)
            account = self._account(auth, item["label"], "dashboard", item["_home"])
            with self.lock:
                if item["status"] != "pending" or self.closed.is_set():
                    return
                previous = self.accounts.get(account["id"])
                if previous and previous["source"] == "dashboard":
                    # Existing owned login is preserved until a new one succeeds.
                    old_home = previous["_home"]
                else:
                    old_home = None
                was_ignored = account["id"] in self.ignored
                self.accounts[account["id"]] = account
                self.ignored.discard(account["id"])
                try:
                    self._save()
                except Exception:
                    if previous:
                        self.accounts[account["id"]] = previous
                    else:
                        self.accounts.pop(account["id"], None)
                    if was_ignored:
                        self.ignored.add(account["id"])
                    raise
                retained = True
                item.update(status="complete", accountId=account["id"], userCode=None)
            if old_home:
                guard = self.account_locks.setdefault(account["id"], threading.Lock())
                with guard:
                    shutil.rmtree(old_home, ignore_errors=True)
        except Exception:
            with self.lock:
                if item["status"] == "pending":
                    item.update(status="failed", error="로그인을 완료하지 못했습니다. 새 코드로 다시 시도해 주세요.", userCode=None)
        finally:
            self._close_session(session)
            if not retained:
                shutil.rmtree(item["_home"], ignore_errors=True)
            else:
                self.refresh()

    def cancel_login(self, login_id):
        with self.lock:
            item = self.logins.get(login_id)
            if not item:
                raise ValueError("로그인을 찾을 수 없습니다.")
            if item["status"] != "pending":
                self.logins.pop(login_id)
                return
            item.update(status="cancelled", userCode=None)
        # Closing this isolated server cancels its login without touching main Codex auth.
        session = item["_session"]
        if session:
            self._close_session(session)

    def remove_account(self, account_id):
        with self.lock:
            guard = self.account_locks.setdefault(account_id, threading.Lock())
        with guard, self.lock:
            account = self.accounts.pop(account_id, None)
            if not account:
                raise ValueError("계정을 찾을 수 없습니다.")
            self.ignored.add(account_id)
            self._save()
            if account["source"] == "dashboard":
                shutil.rmtree(account["_home"], ignore_errors=True)

    def close(self):
        self.closed.set()
        with self.lock:
            self._refresh_pending = False
            sessions = list(self.sessions)
        for session in sessions:
            self._close_session(session)
        if self.scheduler:
            self.scheduler.join(timeout=2)
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=4)
