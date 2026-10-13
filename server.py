#!/usr/bin/env python3
"""Local and opt-in LAN HTTP interface for the Codex account usage monitor."""

import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import signal
import socket
import struct
import threading
import time
from urllib.parse import urlsplit
import webbrowser
from storage import default_data_dir


STATIC = Path(__file__).parent / "static"
LAN_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))


def _local_ipv4(value):
    """Parse an IPv4 literal that can belong to a local LAN or loopback."""
    if not isinstance(value, str):
        raise ValueError("IPv4 주소를 지정해 주세요.")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("호스트 이름 대신 IPv4 주소를 지정해 주세요.") from None
    if not address.is_loopback and not any(address in network for network in LAN_NETWORKS):
        raise ValueError("내부망 또는 127.x.x.x IPv4 주소만 지원합니다.")
    return str(address)


def validate_host(host):
    if host == "0.0.0.0":
        return host
    return _local_ipv4(host)


def discover_local_ipv4():
    """Read local IPv4 addresses without sending an HTTP request or packet."""
    addresses = {"127.0.0.1"}
    preferred = None

    def add(value):
        try:
            address = _local_ipv4(value)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as candidate:
                candidate.bind((address, 0))
            addresses.add(address)
        except (ValueError, OSError):
            pass

    # Interface enumeration is more reliable than the hostname on Linux hosts.
    try:
        import fcntl
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _, name in socket.if_nameindex():
                try:
                    request = struct.pack("256s", name.encode()[:15])
                    flags = struct.unpack_from("H", fcntl.ioctl(probe.fileno(), 0x8913, request), 16)[0]
                    if flags & 0x41 != 0x41:  # IFF_UP and IFF_RUNNING
                        continue
                    result = fcntl.ioctl(probe.fileno(), 0x8915, request)  # SIOCGIFADDR
                    add(socket.inet_ntoa(result[20:24]))
                except OSError:
                    continue
    except (ImportError, OSError):
        pass
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(result[4][0])
    except OSError:
        pass
    # A UDP connect selects a route but does not transmit a datagram.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            preferred = probe.getsockname()[0]
            add(preferred)
    except OSError:
        pass
    return sorted(addresses, key=lambda value: (value != preferred, ipaddress.IPv4Address(value)))


def _allowed_hostnames(host, allowed_hosts=None):
    host = validate_host(host)
    names = {"localhost", "127.0.0.1"}
    preferred = []
    if allowed_hosts is None:
        if host == "0.0.0.0":
            preferred = discover_local_ipv4()
            names.update(preferred)
        else:
            names.add(host)
    else:
        if isinstance(allowed_hosts, str):
            raise ValueError("허용 주소는 IPv4 주소 목록으로 지정해 주세요.")
        for name in allowed_hosts:
            name = "localhost" if name == "localhost" else _local_ipv4(name)
            preferred.append(name)
            names.add(name)
        if host != "0.0.0.0":
            names.add(host)
    if host == "0.0.0.0" and not any(
            name != "localhost" and not ipaddress.IPv4Address(name).is_loopback
            for name in names):
        raise ValueError("내부 IP를 찾지 못했습니다. --host 192.168.x.x 옵션으로 이 컴퓨터의 내부 IPv4 주소를 지정해 주세요.")
    ordered = list(dict.fromkeys(preferred))
    return ordered + sorted(names.difference(ordered))


class DemoManager:
    """Explicitly labelled preview data; never reads credentials or starts Codex."""

    def __init__(self, refresh_interval=300):
        self.refresh_interval = refresh_interval
        self.updated = time.time()

    def state(self):
        now = time.time()
        today = datetime.fromtimestamp(self.updated, timezone(timedelta(hours=9))).date()
        def window(remaining, minutes, delay):
            return {"usedPercent": 100 - remaining, "remainingPercent": remaining,
                    "windowDurationMins": minutes, "resetsAt": int(self.updated + delay)}
        accounts = []
        for index, (label, email, plan, short, week, count) in enumerate([
            ("메인 계정", "main@example.com", "plus", 73, 54, 2),
            ("작업용 계정", "work@example.com", "pro", None, 81, 0),
            ("서브 계정", "extra@example.com", "plus", 8, 24, None),
        ]):
            daily = []
            for days_ago in range(36, 0, -1):
                if (days_ago + index) % 6 == 0:
                    continue
                tokens = ((days_ago * 19 + index * 31) % 90 + 5) * 1_000_000
                if (days_ago + index) % 9 == 0:
                    tokens = 0
                daily.append({"date": (today - timedelta(days=days_ago)).isoformat(),
                              "tokens": None if index == 2 and days_ago == 2 else str(tokens)})
            accounts.append({
                "id": f"demo-{index}", "label": label, "email": email, "plan": plan,
                "source": "dashboard", "removable": False, "status": "ok", "error": None,
                "lastSuccessAt": self.updated, "lastAttemptAt": self.updated,
                "tokenUsage": {"status": "ok", "daily": daily, "fetchedAt": self.updated,
                               "lastAttemptAt": self.updated, "error": None, "dateBasis": "server"},
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

    def refresh(self, *, after_current=False):
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


def create_server(manager, port=8765, reset_feed=None, *, host="127.0.0.1", allowed_hosts=None):
    host = validate_host(host)
    hostnames = _allowed_hostnames(host, allowed_hosts)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("포트는 1~65535 범위여야 합니다.")
    access_mode = "local" if host != "0.0.0.0" and ipaddress.IPv4Address(host).is_loopback else "lan"
    feed_signature = [None]
    feed_lock = threading.Lock()

    def with_access(data):
        return {**data, "accessMode": access_mode, "accessUrls": list(server.access_urls)}

    def read_feed():
        if reset_feed is None:
            return {"status": "pending", "refreshing": False, "fetchedAt": None,
                    "events": [], "history": [], "activeWatch": None, "scheduledReset": None,
                    "error": None, "sourceUrl": "https://codex-resets.com/"}
        data = reset_feed.state()
        if data.get("fetchedAt") is not None:
            history = data.get("history") or []
            latest = max(history, key=lambda item: item.get("announcedAt") or 0, default={})
            scheduled = data.get("scheduledReset") or {}
            signature = (latest.get("id"), scheduled.get("id"))
            with feed_lock:
                previous = feed_signature[0]
                feed_signature[0] = signature
            if previous is not None and previous != signature:
                manager.refresh(after_current=True)
        return data

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
            endpoints = {f"{name}:{expected_port}" for name in hostnames}
            if expected_port == 80:
                endpoints.update(hostnames)
            headers = self.headers.get_all("Host", [])
            host_header = headers[0].lower() if len(headers) == 1 else ""
            try:
                _local_ipv4(self.client_address[0])
                local_peer = True
            except ValueError:
                local_peer = False
            if host_header not in endpoints or not local_peer:
                self.send_json(403, {"error": "이 컴퓨터의 허용된 내부 IP 또는 localhost 주소로 열어 주세요."})
                return False
            origins = self.headers.get_all("Origin", [])
            expected_origins = {f"http://{host_header}"}
            if expected_port == 80:
                expected_origins.add(f"http://{host_header.removesuffix(':80')}")
            if len(origins) > 1 or (origins and origins[0] not in expected_origins):
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
            if path == "/api/reset-calendar":
                try:
                    self.send_json(200, with_access(read_feed()))
                except Exception:
                    self.send_json(503, {"error": "초기화 일정 정보를 읽지 못했습니다."})
                return
            if path == "/api/state":
                try:
                    self.send_json(200, with_access(manager.state()))
                except Exception:
                    self.send_json(500, {"error": "상태를 읽지 못했습니다. 잠시 후 다시 시도해 주세요."})
                return
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/index.html": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                     "/calendar.js": ("calendar.js", "application/javascript; charset=utf-8"),
                     "/daily.js": ("daily.js", "application/javascript; charset=utf-8"),
                     "/daily.css": ("daily.css", "text/css; charset=utf-8"),
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
                    if reset_feed is not None:
                        reset_feed.refresh()
                    self.send_json(202, with_access(manager.refresh()))
                elif path == "/api/accounts":
                    label = data.get("label")
                    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
                        raise ValueError("계정 이름을 1~60자로 입력해 주세요.")
                    self.send_json(201, with_access({"login": manager.add_account(label.strip())}))
                elif path.startswith("/api/logins/") and path.endswith("/cancel"):
                    login_id = path.removeprefix("/api/logins/").removesuffix("/cancel")
                    if not login_id or "/" in login_id:
                        raise ValueError("로그인을 찾을 수 없습니다.")
                    manager.cancel_login(login_id)
                    self.send_json(200, with_access(manager.state()))
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
                self.send_json(200, with_access(manager.state()))
            except (ValueError, KeyError):
                self.send_json(400, {"error": "계정을 제거하지 못했습니다."})
            except Exception:
                self.send_json(500, {"error": "계정을 제거하지 못했습니다."})

        def do_OPTIONS(self):
            self.send_json(403, {"error": "외부 웹사이트에서는 접근할 수 없습니다."})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.access_mode = access_mode
    if access_mode == "lan":
        visible_hosts = [name for name in hostnames
                         if name != "localhost" and not ipaddress.IPv4Address(name).is_loopback]
    else:
        visible_hosts = [host]
    server.access_urls = [f"http://{name}:{server.server_address[1]}" for name in visible_hosts]
    return server


def main():
    parser = argparse.ArgumentParser(description="여러 Codex 계정의 잔여량과 초기화권을 로컬 또는 같은 내부망에서 확인합니다.")
    parser.add_argument("--port", type=int, default=8765, help="로컬 포트 (기본 8765)")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--lan", action="store_true", help="같은 공유기의 컴퓨터·휴대폰에서 암호 없이 접속 허용")
    access.add_argument("--host", help="접속을 허용할 이 컴퓨터의 내부 IPv4 주소 (기본 127.0.0.1)")
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
    try:
        host = validate_host(args.host if args.host is not None else "0.0.0.0" if args.lan else "127.0.0.1")
        hostnames = _allowed_hostnames(host)
    except ValueError as error:
        parser.error(str(error))
    if args.demo:
        manager = DemoManager(args.interval)
        from reset_feed import DemoResetFeed
        reset_feed = DemoResetFeed(args.interval)
    else:
        from backend import DashboardManager
        try:
            manager = DashboardManager(args.data_dir.expanduser(), args.codex, args.interval)
        except Exception:
            parser.exit(1, "대시보드를 시작하지 못했습니다. Codex 설치와 저장 폴더 권한을 확인해 주세요.\n")
        from reset_feed import ResetFeedProvider
        reset_feed = ResetFeedProvider(refresh_interval=args.interval)
    try:
        server = create_server(manager, args.port, reset_feed=reset_feed,
                               host=host, allowed_hosts=hostnames)
    except OSError:
        reset_feed.close()
        manager.close()
        parser.exit(1, "포트를 사용할 수 없습니다. --port 옵션으로 다른 포트를 지정해 주세요.\n")
    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{browser_host}:{server.server_address[1]}"
    print(f"Codex 계정 대시보드: {url}", flush=True)
    if server.access_mode == "lan":
        print("같은 내부망에서 암호 없이 접속할 수 있습니다:", flush=True)
        for access_url in server.access_urls:
            print(f"  {access_url}", flush=True)
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
        reset_feed.close()
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
