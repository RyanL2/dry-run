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
    except TimeoutError:
        return _out("ask", "could not finish in time; review this command manually")
    except OSError as exc:
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
