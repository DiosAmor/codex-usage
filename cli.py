"""Terminal-first interface; the optional browser UI uses the same provider."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

from backend import DashboardManager
from storage import default_data_dir


DEFAULT_DATA = default_data_dir()
KST = ZoneInfo("Asia/Seoul")


def plain(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value if value is not None else "확인 불가"))[:240]


def timestamp(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return "확인 불가"
    try:
        return datetime.fromtimestamp(value, KST).strftime("%m/%d %H:%M KST")
    except (ValueError, OSError, OverflowError):
        return "확인 불가"


def duration(minutes):
    if minutes == 10080:
        return "주간"
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        return "기간 확인 불가"
    if minutes % 1440 == 0:
        return f"{minutes / 1440:g}일"
    if minutes % 60 == 0:
        return f"{minutes / 60:g}시간"
    return f"{minutes:g}분"


def render(state, *, offline=False):
    lines = ["Codex 계정 사용량" + (" · 예시 데이터" if state.get("demo") else ""),
             f"조회 시각: {timestamp(state.get('now'))}", ""]
    if not state.get("accounts"):
        lines += ["등록된 계정이 없습니다.", '계정 등록: codex-usage login "계정 이름"']
    for account in state.get("accounts", []):
        identity = " · ".join(plain(item) for item in (account.get("label"), account.get("email"), account.get("plan")) if item)
        lines.append(identity)
        if offline:
            lines.append(f"  ID: {plain(account.get('id'))} · {'기존 Codex 로그인' if account.get('source') == 'existing' else '대시보드 전용 로그인'}")
            lines.append("")
            continue
        if account.get("error"):
            lines.append(f"  조회 실패: {plain(account['error'])}")
        if account.get("status") == "pending":
            lines.append("  아직 조회하지 못했습니다.")
        elif account.get("status") != "ok" and account.get("lastSuccessAt"):
            lines.append(f"  이전 조회값: {timestamp(account['lastSuccessAt'])}")
        for bucket in account.get("buckets", []):
            lines.append(f"  [{plain(bucket.get('name') or bucket.get('id'))}]")
            windows = [bucket.get("primary"), bucket.get("secondary")]
            if not any(windows):
                lines.append("    사용 한도 정보: 제공되지 않음")
            for quota in windows:
                if not isinstance(quota, dict):
                    continue
                remaining = quota.get("remainingPercent")
                value = f"{remaining:g}% 남음" if isinstance(remaining, (int, float)) else "잔여량 확인 불가"
                lines.append(f"    {duration(quota.get('windowDurationMins'))}: {value} · 갱신 {timestamp(quota.get('resetsAt'))}")
            credits = bucket.get("credits")
            if isinstance(credits, dict) and credits.get("balance") is not None:
                lines.append(f"    추가 크레딧: {plain(credits['balance'])}")
        reset = account.get("resetCredits")
        count = reset.get("availableCount") if isinstance(reset, dict) else None
        lines.append(f"  초기화권: {count}개" if count is not None else "  초기화권: 확인 불가")
        details = reset.get("credits") if isinstance(reset, dict) else None
        if count and isinstance(details, list):
            for item in details:
                lines.append(f"    {plain(item.get('title') or '초기화권')} · 만료 {timestamp(item.get('expiresAt'))}")
            if len(details) < count:
                lines.append("    일부 초기화권의 만료 정보는 제공되지 않았습니다.")
        elif count and details is None:
            lines.append("    만료 정보는 제공되지 않았습니다.")
        lines.append("")
    if not offline:
        lines.append("초기화권은 조회만 합니다. 확인 불가는 0개 또는 무제한을 의미하지 않습니다.")
    return "\n".join(lines)


def fetch_once(manager, timeout=75):
    manager.refresh()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.state()
        if not state.get("refreshing"):
            return state
        time.sleep(0.1)
    state = manager.state()
    for account in state.get("accounts", []):
        if account.get("status") == "pending":
            account.update(status="error", error="조회 시간이 초과되었습니다.")
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="여러 Codex 계정의 잔여량과 초기화권을 터미널에서 확인합니다.")
    parser.add_argument("--version", action="version", version="codex-usage 0.1.0")
    parser.add_argument("command", nargs="?", default="status", choices=["status", "watch", "login", "list", "serve"])
    parser.add_argument("name", nargs="?", help="login에서 사용할 계정 이름")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--codex", default="codex", help="Codex CLI 실행 파일")
    parser.add_argument("--interval", type=int, default=300, help="자동 조회 주기, 초 (최소60)")
    parser.add_argument("--json", action="store_true", help="인증 토큰을 제외한 JSON 결과")
    parser.add_argument("--demo", action="store_true", help="실제 계정을 조회하지 않는 예시")
    parser.add_argument("--port", type=int, default=8765, help="serve의 로컬 포트")
    parser.add_argument("--open-browser", action="store_true", help="serve에서 브라우저 열기")
    args = parser.parse_args(argv)
    if args.interval < 60:
        parser.error("조회 주기는 60초 이상이어야 합니다.")
    if (args.command == "login") != (args.name is not None):
        parser.error('login에는 계정 이름을 지정하세요. 예: login "개인 계정"')
    if args.command == "serve":
        from server import main as serve
        sys.argv = [sys.argv[0], "--port", str(args.port), "--interval", str(args.interval),
                    "--data-dir", str(args.data_dir), "--codex", args.codex]
        if args.demo:
            sys.argv.append("--demo")
        if args.open_browser:
            sys.argv.append("--open-browser")
        return serve()
    if args.demo:
        from server import DemoManager
        manager = DemoManager(args.interval)
    else:
        try:
            manager = DashboardManager(args.data_dir.expanduser(), args.codex, args.interval, auto_start=False)
        except Exception:
            print("저장소를 열지 못했습니다. Codex 설치와 폴더 권한을 확인해 주세요.", file=sys.stderr)
            return 1
    try:
        if args.command == "login":
            result = manager.add_account(args.name)
            if result.get("status") != "pending":
                print(plain(result.get("error")), file=sys.stderr)
                return 1
            print(f"{plain(args.name)} 계정을 연결합니다.")
            print(f"로그인 주소: {result['verificationUrl']}")
            print(f"입력할 일회용 코드: {plain(result['userCode'])}")
            print("로그인이 끝날 때까지 이 터미널을 열어 두세요. 취소: Ctrl+C", flush=True)
            while True:
                login = next((item for item in manager.state().get("logins", []) if item["id"] == result["id"]), None)
                if not login or login["status"] != "pending":
                    if login and login["status"] == "complete":
                        print("계정을 등록했습니다. codex-usage로 전체 사용량을 확인하세요.")
                        return 0
                    print(plain((login or {}).get("error") or "로그인을 완료하지 못했습니다."), file=sys.stderr)
                    return 1
                time.sleep(0.5)
        elif args.command == "list":
            state = manager.state()
            print(json.dumps(state, ensure_ascii=False, indent=2) if args.json else render(state, offline=True))
        else:
            while True:
                state = manager.state() if args.demo else fetch_once(manager)
                if args.command == "watch" and sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                print(json.dumps(state, ensure_ascii=False, indent=2) if args.json else render(state), flush=True)
                if args.command != "watch":
                    return 1 if any(account.get("status") == "error" for account in state.get("accounts", [])) else 0
                print(f"다음 조회: {args.interval}초 후 · 종료: Ctrl+C", flush=True)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n종료했습니다.")
        return 130
    finally:
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
