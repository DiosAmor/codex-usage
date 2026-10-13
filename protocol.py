"""Small, bounded JSONL client. Credentials never leave its private pipes."""

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


class ProtocolError(RuntimeError):
    def __init__(self, message, *, method=None, code=None):
        super().__init__(message)
        self.method = method
        self.code = code


class CodexSession:
    ALLOWED = {"initialize", "account/login/start", "account/login/cancel",
               "account/read", "account/rateLimits/read"}

    def __init__(self, home, binary="codex", refresh_tokens=None):
        self.home = Path(home)
        self.refresh_tokens = refresh_tokens
        self.messages = queue.Queue()
        self.notifications = queue.Queue()
        self.write_lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.counter = 0
        self.closed = False
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        binary = os.fspath(binary)
        if os.path.dirname(binary):
            binary = str(Path(binary).expanduser().resolve())
        try:
            self.proc = subprocess.Popen(
                [binary, "-c", 'cli_auth_credentials_store="file"',
                 "-c", "analytics.enabled=false", "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", env=env, cwd=self.home)
        except OSError:
            raise ProtocolError("Codex CLI를 실행할 수 없습니다.") from None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "codex_usage_monitor",
                         "title": "Codex Monitor", "version": "1.0.0"},
                         "capabilities": {"experimentalApi": True}}, timeout=15)
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _send(self, value):
        with self.write_lock:
            if self.closed:
                raise ProtocolError("Codex 연결이 종료되었습니다.")
            try:
                self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                raise ProtocolError("Codex 연결이 종료되었습니다.") from None

    def _read(self):
        try:
            for line in self.proc.stdout:
                if len(line) > 2_000_000:
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "account/chatgptAuthTokens/refresh" and "id" in message:
                    try:
                        fresh = self.refresh_tokens() if self.refresh_tokens else None
                        if fresh:
                            self._send({"id": message["id"], "result": fresh})
                        else:
                            self._send({"id": message["id"], "error": {
                                "code": -32000, "message": "Reconnect account in dashboard"}})
                    except Exception:
                        try:
                            self._send({"id": message["id"], "error": {
                                "code": -32000, "message": "Reconnect account in dashboard"}})
                        except Exception:
                            pass
                elif "method" in message:
                    self.notifications.put(message)
                else:
                    self.messages.put(message)
        finally:
            self.messages.put(None)
            self.notifications.put(None)

    def request(self, method, params=None, timeout=30):
        if method not in self.ALLOWED:
            raise ProtocolError("지원하지 않는 Codex 요청입니다.")
        with self.request_lock:
            self.counter += 1
            request_id = self.counter
            self._send({"id": request_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if message is None:
                    raise ProtocolError("Codex 연결이 종료되었습니다.")
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    # Server errors may contain secrets or URLs. Never pass them to UI/logs.
                    error = message.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    raise ProtocolError("Codex 요청에 실패했습니다. 로그인 상태와 네트워크를 확인해 주세요.",
                                        method=method, code=code if isinstance(code, int) else None)
                return message.get("result", {})
            raise ProtocolError("Codex 응답 시간이 초과되었습니다.")

    def wait_login(self, login_id, timeout=900):
        deadline = time.monotonic() + timeout
        while not self.closed and time.monotonic() < deadline:
            try:
                message = self.notifications.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if message is None:
                raise ProtocolError("로그인 연결이 종료되었습니다.")
            params = message.get("params") or {}
            if message.get("method") == "account/login/completed" and params.get("loginId") == login_id:
                if params.get("success") is True:
                    return
                raise ProtocolError("로그인을 완료하지 못했습니다. 새 코드로 다시 시도해 주세요.")
        raise ProtocolError("로그인 시간이 만료되었습니다. 다시 시작해 주세요.")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        if self.reader is not threading.current_thread():
            self.reader.join(timeout=1)
        self.proc.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
