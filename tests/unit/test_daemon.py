from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from dryrun import rpc
from dryrun.config import load_config
from dryrun.daemon import Daemon
from dryrun.store import Store
from dryrun.types import Decision


class StubPipeline:
    def __init__(self):
        self.prompts = []

    def handle_prompt(self, session_id, text):
        self.prompts.append((session_id, text))

    def handle_pretool(self, req, cancel=None):
        if req["command"] == "slow":
            cancel.wait(10)
            return Decision("allow", "commit", "too late")
        return Decision("allow", "passthrough", "ok")


def start(tmp_path: Path):
    sock = tmp_path / "d.sock"
    stub = StubPipeline()
    d = Daemon(load_config(use_user_file=False), Store(tmp_path / "state"), stub, sock,
               canary_fn=lambda: (True, "stub"))
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=lambda: loop.run_until_complete(d.serve()), daemon=True)
    t.start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.02)
    return d, stub, sock, loop


def pre(cmd, deadline_ms=5000):
    return {"op": "pretool", "session_id": "s", "cwd": "/", "command": cmd, "description": "",
            "transcript_path": "", "env": {}, "deadline_ms": deadline_ms}


def test_round_trip_status_prompt_and_pretool(tmp_path: Path):
    d, stub, sock, loop = start(tmp_path)
    try:
        assert rpc.call(sock, {"op": "status"}, 2)["ok"] is True
        assert rpc.call(sock, {"op": "prompt", "session_id": "s", "text": "hi"}, 2) == {"ok": True}
        assert stub.prompts == [("s", "hi")]
        r = rpc.call(sock, pre("ls"), 5)
        assert (r["decision"], r["mode"]) == ("allow", "passthrough")
    finally:
        loop.call_soon_threadsafe(d.stop)


def test_deadline_turns_into_ask(tmp_path: Path):
    d, stub, sock, loop = start(tmp_path)
    try:
        t0 = time.monotonic()
        r = rpc.call(sock, pre("slow", deadline_ms=1500), 5)
        assert r["decision"] == "ask" and "time" in r["reason"]
        assert time.monotonic() - t0 < 3
    finally:
        loop.call_soon_threadsafe(d.stop)


def test_unknown_op(tmp_path: Path):
    d, stub, sock, loop = start(tmp_path)
    try:
        assert rpc.call(sock, {"op": "nope"}, 2)["ok"] is False
    finally:
        loop.call_soon_threadsafe(d.stop)
