#!/usr/bin/env python3
"""Loopback-only HTTP interface for the Codex account usage monitor."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import urlsplit
import webbrowser
from storage import default_data_dir


STATIC = Path(__file__).parent / "static"


class DemoManager:
    """Explicitly labelled preview data; never reads credentials or starts Codex."""

    def __init__(self, refresh_interval=300):
        self.refresh_interval = refresh_interval
        self.updated = time.time()

    def state(self):
        now = time.time()
        def window(remaining, minutes, delay):
            return {"usedPercent": 100 - remaining, "remainingPercent": remaining,
                    "windowDurationMins": minutes, "resetsAt": int(self.updated + delay)}
        accounts = []
        for index, (label, email, plan, short, week, count) in enumerate([
            ("메인 계정", "main@example.com", "plus", 73, 54, 2),
            ("작업용 계정", "work@example.com", "pro", None, 81, 0),
            ("서브 계정", "extra@example.com", "plus", 8, 24, None),
        ]):
            accounts.append({
                "id": f"demo-{index}", "label": label, "email": email, "plan": plan,
                "source": "dashboard", "removable": False, "status": "ok", "error": None,
                "lastSuccessAt": self.updated, "lastAttemptAt": self.updated,
                "buckets": [{"id": "codex", "name": "Codex", "plan": plan,
                             "primary": window(short, 300, 6300) if short is not None else None,
                             "secondary": window(week, 10080, 238000), "credits": None}],
                "resetCredits": {"availableCount": count,
                                 "credits": [{"title": "사용량 초기화권", "status": "available",
                                              "expiresAt": int(self.updated + 86400 * 6)}]
                                 if count else ([] if count == 0 else None)},
            })
        return {"accounts": accounts, "logins": [], "refreshing": False,
                "refreshInterval": self.refresh_interval, "now": now,
                "nextRefreshAt": self.updated + self.refresh_interval,
                "demo": True}

    def refresh(self):
        self.updated = time.time()
        return self.state()

    def add_account(self, label):
        raise ValueError("미리보기에서는 계정을 등록할 수 없습니다. 실제 대시보드를 열어 주세요.")

    def cancel_login(self, login_id):
        raise ValueError("미리보기에서는 로그인할 수 없습니다.")

    def remove_account(self, account_id):
        raise ValueError("미리보기에서는 계정을 제거할 수 없습니다.")

    def close(self):
        pass


def create_server(manager, port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexMonitor/1.0"

        def log_message(self, *args):
            # Requests, login codes, account labels and remote errors stay out of logs.
            pass

        def send_bytes(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_bytes(status, body)

        def check_request(self, mutation=False):
            expected_port = self.server.server_address[1]
            allowed_hosts = {f"127.0.0.1:{expected_port}", f"localhost:{expected_port}"}
            host = self.headers.get("Host", "")
            if host not in allowed_hosts:
                self.send_json(403, {"error": "이 대시보드는 localhost 주소로만 열 수 있습니다."})
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                self.send_json(403, {"error": "다른 웹사이트에서 보낸 요청은 허용하지 않습니다."})
                return False
            if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                self.send_json(403, {"error": "대시보드 화면에서 다시 시도해 주세요."})
                return False
            if mutation and self.headers.get("X-Dashboard-Request") != "1":
                self.send_json(403, {"error": "대시보드 화면에서 다시 시도해 주세요."})
                return False
            return True

        def do_GET(self):
            if not self.check_request():
                return
            path = urlsplit(self.path).path
            if path == "/api/state":
                try:
                    self.send_json(200, manager.state())
                except Exception:
                    self.send_json(500, {"error": "상태를 읽지 못했습니다. 잠시 후 다시 시도해 주세요."})
                return
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/index.html": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                     "/app.css": ("app.css", "text/css; charset=utf-8")}
            if path not in files:
                self.send_json(404, {"error": "페이지를 찾을 수 없습니다."})
                return
            name, content_type = files[path]
            try:
                body = (STATIC / name).read_bytes()
                self.send_bytes(200, body, content_type)
            except OSError:
                self.send_json(500, {"error": "대시보드 파일을 읽지 못했습니다."})

        def read_json(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("지원하지 않는 요청 형식입니다.")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("JSON 요청만 지원합니다.")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 8192:
                raise ValueError("요청이 너무 큽니다.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("요청 형식이 올바르지 않습니다.")
            return data

        def do_POST(self):
            if not self.check_request(mutation=True):
                return
            path = urlsplit(self.path).path
            try:
                data = self.read_json()
                if path == "/api/refresh":
                    self.send_json(202, manager.refresh())
                elif path == "/api/accounts":
                    label = data.get("label")
                    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
                        raise ValueError("계정 이름을 1~60자로 입력해 주세요.")
                    self.send_json(201, {"login": manager.add_account(label.strip())})
                elif path.startswith("/api/logins/") and path.endswith("/cancel"):
                    login_id = path.removeprefix("/api/logins/").removesuffix("/cancel")
                    if not login_id or "/" in login_id:
                        raise ValueError("로그인을 찾을 수 없습니다.")
                    manager.cancel_login(login_id)
                    self.send_json(200, manager.state())
                else:
                    self.send_json(404, {"error": "지원하지 않는 요청입니다."})
            except (ValueError, KeyError):
                self.send_json(400, {"error": "요청을 처리하지 못했습니다. 입력과 계정 상태를 확인해 주세요."})
            except Exception:
                self.send_json(503, {"error": "계정 연결을 완료하지 못했습니다. Codex 설치와 네트워크를 확인해 주세요."})

        def do_DELETE(self):
            if not self.check_request(mutation=True):
                return
            path = urlsplit(self.path).path
            account_id = path.removeprefix("/api/accounts/")
            if not path.startswith("/api/accounts/") or not account_id or "/" in account_id:
                self.send_json(404, {"error": "계정을 찾을 수 없습니다."})
                return
            try:
                manager.remove_account(account_id)
                self.send_json(200, manager.state())
            except (ValueError, KeyError):
                self.send_json(400, {"error": "계정을 제거하지 못했습니다."})
            except Exception:
                self.send_json(500, {"error": "계정을 제거하지 못했습니다."})

        def do_OPTIONS(self):
            self.send_json(403, {"error": "외부 웹사이트에서는 접근할 수 없습니다."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="여러 Codex 계정의 잔여량과 초기화권을 localhost에서 확인합니다.")
    parser.add_argument("--port", type=int, default=8765, help="로컬 포트 (기본 8765)")
    parser.add_argument("--interval", type=int, default=300, help="자동 조회 간격, 초 (최소 60)")
    parser.add_argument("--data-dir", type=Path,
                        default=default_data_dir(),
                        help="대시보드 전용 로그인 저장 폴더")
    parser.add_argument("--codex", default="codex", help="Codex CLI 실행 파일")
    parser.add_argument("--demo", action="store_true", help="실제 계정을 읽지 않는 예시 화면")
    parser.add_argument("--open-browser", action="store_true", help="시작할 때 브라우저 열기")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.interval < 60:
        parser.error("포트는 1~65535, 조회 간격은 60초 이상이어야 합니다.")
    if args.demo:
        manager = DemoManager(args.interval)
    else:
        from backend import DashboardManager
        try:
            manager = DashboardManager(args.data_dir.expanduser(), args.codex, args.interval)
        except Exception:
            parser.exit(1, "대시보드를 시작하지 못했습니다. Codex 설치와 저장 폴더 권한을 확인해 주세요.\n")
    try:
        server = create_server(manager, args.port)
    except OSError:
        manager.close()
        parser.exit(1, "포트를 사용할 수 없습니다. --port 옵션으로 다른 포트를 지정해 주세요.\n")
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Codex 계정 대시보드: {url}", flush=True)
    if args.demo:
        print("미리보기 모드: 모든 수치는 예시이며 실제 계정을 조회하지 않습니다.", flush=True)
    print("종료하려면 Ctrl+C를 누르세요.", flush=True)
    if args.open_browser:
        webbrowser.open(url)
    def stop(*unused):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
