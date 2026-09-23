#!/bin/bash
# Dev bootstrap, run as the non-root dev user inside WSL (never as root).
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then echo "refusing to run as root" >&2; exit 1; fi
VENV="$HOME/.venvs/dryrun"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q meson ninja pytest hypothesis pyyaml jsonschema tree-sitter tree-sitter-bash
"$VENV/bin/python" -c 'import tree_sitter, tree_sitter_bash, yaml, jsonschema, hypothesis; print("python deps OK")'
PATH="$VENV/bin:$PATH" bash "$(dirname "$0")/../build-bwrap.sh"
