#!/bin/bash
# Run `dryrun doctor` from the source tree inside WSL as the non-root dev user.
set -euo pipefail
cd "$(dirname "$0")/../.."
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
exec "$HOME/.venvs/dryrun/bin/python" -m dryrun.cli doctor "$@"
