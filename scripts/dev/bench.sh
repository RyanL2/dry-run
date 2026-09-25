#!/bin/bash
# Run one benchmark module inside WSL as the non-root dev user: scripts/dev/bench.sh latency|overlay_factorial|stages
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then echo "refusing to run as root" >&2; exit 1; fi
cd "$(dirname "$0")/../.."
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
exec "$HOME/.venvs/dryrun/bin/python" -m "bench.$1"
