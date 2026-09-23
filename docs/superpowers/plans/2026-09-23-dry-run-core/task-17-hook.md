### Task 17: Hook client (F1, F2, F9, N4)

**Files:**
- Create: `src/dryrun/hook.py`, `tests/hook/__init__.py` (empty), `tests/hook/test_hook.py`

**Interfaces:**
- Consumes:
  - `dryrun.rpc.call` (Task 16) and `dryrun.paths.socket_path` (Task 1), both stdlib-only modules.
  - The Claude Code hook stdin JSON: `session_id`, `cwd`, `transcript_path`, `tool_name`, `tool_input{command, description, timeout, run_in_background}`, and `prompt` for UserPromptSubmit.
- Produces:
  - `dryrun-hook pretool` / `dryrun-hook prompt` (the `main(argv)` entry point)
  - `map_response(resp: dict, tool_input: dict) -> dict`: a Claude Code `hookSpecificOutput` document

  | Daemon response | Hook output |
  |---|---|
  | allow/ask + `commit` | `updatedInput` = tool_input with `command` replaced by `<DRYRUN_BIN> apply <run_id> --token <token>` |
  | `rerun` | original command; the reason says approval runs it for real |
  | `deny` | deny |
  | anything malformed | ask |

- Env: `DRYRUN_SOCKET` (socket), `DRYRUN_HOOK_TIMEOUT_S` (default 60; the deadline is this minus 2 s), `DRYRUN_BIN` (default `~/.local/bin/dryrun`).

- [ ] **Step 1: Write the failing test**

`tests/hook/test_hook.py`:
```python
from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
BASH_INPUT = {"session_id": "s1", "cwd": "/w", "transcript_path": "/t.jsonl", "tool_name": "Bash",
              "hook_event_name": "PreToolUse",
              "tool_input": {"command": "rm -rf build", "description": "clean", "timeout": 120000}}


def run_hook(mode: str, payload, sock: Path, timeout_s: str = "60", raw: bytes | None = None):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(SRC), "DRYRUN_SOCKET": str(sock),
           "DRYRUN_HOOK_TIMEOUT_S": timeout_s, "DRYRUN_BIN": "/opt/dryrun"}
    data = raw if raw is not None else json.dumps(payload).encode()
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "dryrun.hook", mode], input=data, capture_output=True, env=env,
                       timeout=30)
    return p, time.monotonic() - t0


def decision(p) -> dict:
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["hookSpecificOutput"]


def serve(sock: Path, reply):
    """One-shot fake dryrund: reply(request_dict) -> bytes to send (or None to hang)."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(sock))
    srv.listen(1)

    def loop():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                buf += conn.recv(65536)
            out = reply(json.loads(buf))
            if out is None:
                time.sleep(10)
            else:
                conn.sendall(out)
        srv.close()

    threading.Thread(target=loop, daemon=True).start()


def test_missing_daemon_asks(tmp_path: Path):
    p, _ = run_hook("pretool", BASH_INPUT, tmp_path / "none.sock")
    d = decision(p)
    assert d["permissionDecision"] == "ask" and "not running" in d["permissionDecisionReason"]


def test_garbage_reply_asks(tmp_path: Path):
    sock = tmp_path / "d.sock"
    serve(sock, lambda req: b"this is not json\n")
    assert decision(run_hook("pretool", BASH_INPUT, sock)[0])["permissionDecision"] == "ask"


def test_malformed_decision_asks(tmp_path: Path):
    sock = tmp_path / "d.sock"
    serve(sock, lambda req: json.dumps({"decision": "allow", "mode": "sideways"}).encode() + b"\n")
    assert decision(run_hook("pretool", BASH_INPUT, sock)[0])["permissionDecision"] == "ask"


def test_hung_daemon_asks_before_deadline(tmp_path: Path):
    sock = tmp_path / "d.sock"
    serve(sock, lambda req: None)
    p, elapsed = run_hook("pretool", BASH_INPUT, sock, timeout_s="3")
    assert decision(p)["permissionDecision"] == "ask" and elapsed < 3


def test_commit_rewrites_command_and_keeps_other_fields(tmp_path: Path):
    sock = tmp_path / "d.sock"
    seen = {}

    def reply(req):
        seen.update(req)
        return json.dumps({"decision": "allow", "mode": "commit", "reason": "within policy", "rule_ids": [],
                           "run_id": "68d2f1a3-0badc0de", "token": "tok en"}).encode() + b"\n"

    serve(sock, reply)
    d = decision(run_hook("pretool", BASH_INPUT, sock)[0])
    assert d["permissionDecision"] == "allow"
    assert d["updatedInput"]["command"] == "/opt/dryrun apply 68d2f1a3-0badc0de --token 'tok en'"
    assert d["updatedInput"]["description"] == "clean" and d["updatedInput"]["timeout"] == 120000
    assert seen["command"] == "rm -rf build" and seen["op"] == "pretool" and seen["deadline_ms"] == 58000


def test_rerun_and_deny_pass_through(tmp_path: Path):
    sock = tmp_path / "d.sock"
    serve(sock, lambda req: json.dumps({"decision": "ask", "mode": "rerun", "reason": "H7: network",
                                        "rule_ids": ["H7.network"], "run_id": None, "token": None}).encode() + b"\n")
    d = decision(run_hook("pretool", BASH_INPUT, sock)[0])
    assert d["permissionDecision"] == "ask" and "updatedInput" not in d and "for real" in d["permissionDecisionReason"]


@pytest.mark.parametrize("raw", [b"{not json", b"[]", b"x" * (1024 * 1024 + 10)])
def test_bad_stdin_asks(tmp_path: Path, raw):
    p, _ = run_hook("pretool", None, tmp_path / "none.sock", raw=raw)
    assert decision(p)["permissionDecision"] == "ask"


def test_non_bash_tool_has_no_opinion(tmp_path: Path):
    p, _ = run_hook("pretool", {**BASH_INPUT, "tool_name": "Write"}, tmp_path / "none.sock")
    assert p.returncode == 0 and p.stdout == b""


def test_prompt_mode_never_blocks(tmp_path: Path):
    p, _ = run_hook("prompt", {"session_id": "s", "prompt": "hi", "hook_event_name": "UserPromptSubmit"},
                    tmp_path / "none.sock")
    assert p.returncode == 0 and p.stdout == b""


def test_hook_imports_stdlib_only():
    p = subprocess.run([sys.executable, "-S", "-c", "import dryrun.hook"], env={"PYTHONPATH": str(SRC)},
                       capture_output=True)
    assert p.returncode == 0, p.stderr
    tree = ast.parse((SRC / "dryrun" / "hook.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert {m for m in mods if m.startswith("dryrun")} <= {"dryrun", "dryrun.rpc", "dryrun.paths"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/hook -q`
Expected: FAIL (`No module named dryrun.hook`)

- [ ] **Step 3: Implement**

`src/dryrun/hook.py`:
```python
"""Claude Code hook client. Stdlib only. Every failure path prints an `ask` decision and exits 0:
a crashed or timed-out PreToolUse hook would otherwise let the tool call through."""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from dryrun import rpc
from dryrun.paths import socket_path

MAX_STDIN = 1024 * 1024
PREFIX = "Dry Run: "


def _deadline_ms() -> int:
    try:
        timeout_s = float(os.environ.get("DRYRUN_HOOK_TIMEOUT_S", "60"))
    except ValueError:
        timeout_s = 60.0
    return int(max(0.5, timeout_s - 2) * 1000)


def _out(decision: str, reason: str, updated: dict | None = None) -> dict:
    spec = {"hookEventName": "PreToolUse", "permissionDecision": decision,
            "permissionDecisionReason": PREFIX + reason}
    if updated is not None:
        spec["updatedInput"] = updated
    return {"hookSpecificOutput": spec}


def _apply_command(run_id: str, token: str) -> str:
    exe = os.environ.get("DRYRUN_BIN") or str(Path.home() / ".local" / "bin" / "dryrun")
    return f"{shlex.quote(exe)} apply {shlex.quote(run_id)} --token {shlex.quote(token)}"


def map_response(resp: dict, tool_input: dict) -> dict:
    decision, mode = resp.get("decision"), resp.get("mode")
    reason = str(resp.get("reason", ""))[:1000]
    if decision not in ("allow", "ask", "deny") or mode not in ("commit", "rerun", "passthrough"):
        return _out("ask", "malformed response from dryrund; review manually")
    if mode == "commit" and decision in ("allow", "ask"):
        run_id, token = resp.get("run_id"), resp.get("token")
        if not isinstance(run_id, str) or not isinstance(token, str):
            return _out("ask", "commit decision without a token; review manually")
        updated = dict(tool_input)
        updated["command"] = _apply_command(run_id, token)
        return _out(decision, reason, updated)
    if mode == "rerun" and decision == "ask":
        reason += " (approving runs the original command for real)"
    return _out(decision, reason)


def pretool(data: dict) -> dict:
    if data.get("tool_name") != "Bash":
        return {}
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return _out("ask", "no command in hook input")
    deadline = _deadline_ms()
    req = {"op": "pretool", "session_id": str(data.get("session_id", "")),
           "cwd": str(data.get("cwd") or os.getcwd()), "command": command,
           "description": str(tool_input.get("description") or ""),
           "transcript_path": str(data.get("transcript_path") or ""),
           "env": {k: v for k, v in os.environ.items()}, "deadline_ms": deadline}
    try:
        resp = rpc.call(socket_path(), req, timeout=deadline / 1000)
    except (FileNotFoundError, ConnectionRefusedError):
        return _out("ask", "dryrund is not running; review this command manually")
    except (TimeoutError, OSError) as exc:
        if isinstance(exc, TimeoutError) or "timed out" in str(exc):
            return _out("ask", "could not finish in time; review this command manually")
        return _out("ask", f"cannot reach dryrund ({type(exc).__name__}); review manually")
    except Exception as exc:
        return _out("ask", f"bad reply from dryrund ({type(exc).__name__}); review manually")
    return map_response(resp, tool_input)


def prompt(data: dict) -> None:
    try:
        rpc.call(socket_path(), {"op": "prompt", "session_id": str(data.get("session_id", "")),
                                 "text": str(data.get("prompt", ""))}, timeout=1.0)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else ""
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN + 1)
        if len(raw) > MAX_STDIN:
            raise ValueError("hook input too large")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("hook input is not an object")
    except Exception:
        if mode == "pretool":
            print(json.dumps(_out("ask", "could not read hook input; review manually")))
        return 0
    try:
        if mode == "pretool":
            out = pretool(data)
            if out:
                print(json.dumps(out))
        elif mode == "prompt":
            prompt(data)
    except BaseException as exc:  # noqa: BLE001 - fail to ask on anything, including KeyboardInterrupt
        if mode == "pretool":
            print(json.dumps(_out("ask", f"hook error ({type(exc).__name__}); review manually")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/hook -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/hook.py tests/hook
git commit -m "feat: stdlib-only hook client that fails to ask"
```
