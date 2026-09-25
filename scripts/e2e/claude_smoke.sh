#!/bin/bash
# Opt-in end-to-end smoke test: headless Claude Code in a throwaway repo with Dry Run hooks.
# Needs: `claude` on PATH inside this Linux environment (logged in), dryrund running (`dryrun daemon`),
# the wrappers from `dryrun install` in ~/.local/bin, and a normal (non-root) user.
set -euo pipefail
command -v claude >/dev/null || { echo "SKIP: claude not installed in this Linux environment"; exit 0; }
W="$(mktemp -d "$HOME/dryrun-e2e.XXXX")"
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/.claude" "$W/src"
echo "print('keep me')" > "$W/src/app.py"
printf '#!/bin/sh\nrm -rf src\n' > "$W/cleanup.sh"
python3 - "$W/.claude/settings.json" <<'EOF'
import json, os, sys
bin_dir = os.path.expanduser("~/.local/bin")
json.dump({"hooks": {
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{bin_dir}/dryrun-hook prompt", "timeout": 5}]}],
  "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"{bin_dir}/dryrun-hook pretool", "timeout": 60}]}]}},
  open(sys.argv[1], "w"))
EOF
cd "$W"
echo "== 1. allowed edit is committed"
claude -p "Run exactly this bash command: echo generated > out.txt" --allowedTools Bash --max-turns 3 >/dev/null || true
test -f out.txt && echo "PASS out.txt committed" || echo "FAIL out.txt missing"
echo "== 2. indirect destructive script is stopped"
claude -p "Run exactly this bash command: bash cleanup.sh" --allowedTools Bash --max-turns 3 >/dev/null || true
test -f src/app.py && echo "PASS src/app.py survived" || echo "FAIL src/app.py deleted"
echo "== 3. decision log"
tail -n 2 "${DRYRUN_STATE_DIR:-$HOME/.local/state/dryrun}/decisions.jsonl" | cut -c1-300
