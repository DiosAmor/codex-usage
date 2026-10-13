"""Shared storage defaults, independent of where application code is installed."""

import os
from pathlib import Path
import sys


def default_data_dir():
    configured = os.environ.get("CODEX_USAGE_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    legacy = Path(__file__).resolve().parent / ".private"
    if (legacy / "accounts.json").is_file():
        # Preserve accounts registered before the installable distribution existed.
        return legacy
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "codex-usage"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex-usage"
    return Path.home() / ".local" / "state" / "codex-usage"
