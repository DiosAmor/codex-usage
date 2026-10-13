#!/usr/bin/env python3
"""Install a private checkout without copying credentials or downloading packages."""

import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


RUNTIME_FILES = (
    "codex-usage", "cli.py", "backend.py", "protocol.py", "server.py", "storage.py", "reset_feed.py", "daily_usage.py",
    "static/index.html", "static/app.css", "static/app.js", "static/calendar.js", "static/daily.js", "static/daily.css",
)
MARKER = "# codex-usage managed launcher"


def write_atomic(path, content, mode):
    if path.is_symlink():
        raise ValueError(f"심볼릭 링크에 덮어쓰지 않습니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(prefix):
    source = Path(__file__).resolve().parent
    prefix = Path(prefix).expanduser().absolute()
    runtime = prefix / "share" / "codex-usage"
    launcher = prefix / "bin" / "codex-usage"
    if runtime.is_symlink() or launcher.is_symlink():
        raise ValueError("기존 심볼릭 링크는 변경하지 않습니다. 다른 --prefix를 사용하세요.")
    if launcher.exists() and MARKER not in launcher.read_text():
        raise ValueError("같은 이름의 다른 명령이 있습니다. 다른 --prefix를 사용하세요.")
    # Read the exact allowlist before replacing anything; credentials are never globbed.
    contents = {}
    for name in RUNTIME_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"필요한 소스 파일을 찾을 수 없습니다: {name}")
        contents[name] = path.read_bytes()
    for name, content in contents.items():
        target = runtime / name
        if target.parent.is_symlink():
            raise ValueError(f"설치 경로의 심볼릭 링크를 변경하지 않습니다: {target.parent}")
        write_atomic(target, content, 0o755 if name == "codex-usage" else 0o644)
    script = f"#!/bin/sh\n{MARKER}\nexec {shlex.quote(sys.executable)} {shlex.quote(str(runtime / 'codex-usage'))} \"$@\"\n"
    write_atomic(launcher, script.encode(), 0o755)
    return launcher


def main(argv=None):
    parser = argparse.ArgumentParser(description="이 checkout의 codex-usage를 사용자 명령으로 설치합니다. sudo나 추가 패키지가 필요 없습니다.")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local",
                        help="설치 위치 (기본 ~/.local)")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.exit(1, "Python 3.11 이상이 필요합니다.\n")
    if sys.platform not in ("linux", "darwin"):
        parser.exit(1, "Linux/macOS에서 설치하세요. Windows에서는 WSL을 사용하세요.\n")
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        ZoneInfo("Asia/Seoul")
    except (ImportError, ZoneInfoNotFoundError):
        parser.exit(1, "한국 시간대 정보를 찾지 못했습니다. 시스템 tzdata를 설치한 후 다시 실행하세요.\n")
    try:
        launcher = install(args.prefix)
    except (OSError, ValueError) as error:
        parser.exit(1, f"설치 실패: {error}\n")
    print(f"설치 완료: {launcher}")
    print("빠른 확인: " + shlex.quote(str(launcher)) + " --demo")
    search_paths = [Path(part).expanduser().resolve() for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    if launcher.parent.resolve() not in search_paths:
        print("명령을 어디서든 사용하려면 ~/.bashrc 또는 ~/.zshrc에 다음 줄을 추가하세요:")
        print("export PATH=" + shlex.quote(str(launcher.parent)) + ':"$PATH"')
    if not shutil.which("codex"):
        print("실제 계정 조회에는 Codex CLI가 필요합니다: https://learn.chatgpt.com/docs/codex/cli")
    if (Path(__file__).parent / ".private" / "accounts.json").is_file():
        print("기존 .private 로그인은 복사하지 않았습니다. --data-dir로 기존 위치를 지정하거나 계정을 다시 연결하세요.")
    print("로그인 정보와 Git 파일은 설치에 포함되지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
