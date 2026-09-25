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
