#!/bin/sh
set -eu

installer_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$installer_root/install.py" "$@"
