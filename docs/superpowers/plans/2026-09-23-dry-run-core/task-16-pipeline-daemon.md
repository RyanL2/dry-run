### Task 16: Pipeline, RPC framing, daemon (F2–F9, F13 gate, F16, F17)

**Files:**
- Create: `src/dryrun/pipeline.py`, `src/dryrun/rpc.py`, `src/dryrun/daemon.py`, `tests/unit/test_pipeline_fast.py`, `tests/unit/test_daemon.py`, `tests/sandbox/test_pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 1–15.
- Produces:
  - `Gate(ok: bool | None, detail: str)`: the isolation self-test state
  - `Pipeline(cfg: Config, store: Store, *, judge: EffectJudge | None = None, gate: Gate | None = None, home: Path | None = None, bwrap: str | None = None)` with:
    - `.handle_prompt(session_id: str, text: str) -> None`
    - `.handle_pretool(req: dict, cancel: threading.Event | None = None) -> Decision`
      - `req` follows `dryrun.rpc/1` `pretool_request`
      - A `commit` decision carries `run_id` and `token`, and its run dir is kept.
      - Any other decision removes the run dir.
  - `rpc.call(sock_path: Path, request: dict, timeout: float) -> dict` (stdlib only; the hook uses it); `rpc.read_request(reader)`, `rpc.write_response(writer, obj)`
  - `Daemon(cfg, store, pipeline, sock_path: Path, *, canary_fn: Callable[[], tuple[bool, str]] | None)` with `async serve()` and `stop()`
  - `daemon.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_pipeline_fast.py` (no sandbox; triage paths only):
```python
from __future__ import annotations

from pathlib import Path

from dryrun.config import load_config
from dryrun.pipeline import Gate, Pipeline
from dryrun.store import Store


def pipe(tmp_path: Path, gate_ok=True) -> Pipeline:
    return Pipeline(load_config(use_user_file=False), Store(tmp_path / "state"), gate=Gate(gate_ok, "stub"),
                    home=tmp_path / "home")


def req(cmd: str, cwd: Path) -> dict:
    return {"op": "pretool", "session_id": "s", "cwd": str(cwd), "command": cmd, "description": "",
            "transcript_path": "", "env": {}, "deadline_ms": 5000}


def test_read_only_passthrough(tmp_path: Path):
    d = pipe(tmp_path).handle_pretool(req("ls -la", tmp_path))
    assert (d.decision, d.mode) == ("allow", "passthrough")


def test_non_shadowable_asks(tmp_path: Path):
    d = pipe(tmp_path).handle_pretool(req("git push --force", tmp_path))
    assert (d.decision, d.rule_ids) == ("ask", ["T1.git_push"])


def test_agent_typed_apply_denied_but_valid_token_allowed(tmp_path: Path):
    p = pipe(tmp_path)
    assert p.handle_pretool(req("dryrun apply 68d2f1a3-0badc0de --token x", tmp_path)).decision == "deny"
    run = p.store.new_run()
    token = p.store.authorize(run, session_id="s", decision="allow")
    d = p.handle_pretool(req(f"dryrun apply {run.run_id} --token {token}", tmp_path))
    assert d.decision == "allow"


def test_closed_gate_asks_for_shadow_class(tmp_path: Path):
    (tmp_path / "ws").mkdir()
    d = pipe(tmp_path, gate_ok=False).handle_pretool(req("rm -rf build", tmp_path / "ws"))
    assert d.decision == "ask" and "self-test" in d.reason


def test_decisions_are_logged(tmp_path: Path):
    p = pipe(tmp_path)
    p.handle_pretool(req("ls", tmp_path))
    assert '"decision": "allow"' in p.store.log_path.read_text()
```

`tests/unit/test_daemon.py`:
```python
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


def test_unknown_op_and_garbage(tmp_path: Path):
    d, stub, sock, loop = start(tmp_path)
    try:
        assert rpc.call(sock, {"op": "nope"}, 2)["ok"] is False
    finally:
        loop.call_soon_threadsafe(d.stop)
```

`tests/sandbox/test_pipeline.py`:
```python
from __future__ import annotations

import io
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

import dryrun.pipeline as pipeline_mod
from dryrun.commit import apply_run
from dryrun.config import load_config, with_shadow
from dryrun.pipeline import Gate, Pipeline
from dryrun.store import Store

pytestmark = pytest.mark.sandbox


@pytest.fixture
def env(scratch: Path):
    home = scratch / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("REAL-PRIVATE-KEY\n")
    ws = scratch / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("print('hi')\n")
    (ws / "keep.txt").write_text("keep\n")
    cfg = with_shadow(load_config(use_user_file=False), wall_clock_s=15)
    p = Pipeline(cfg, Store(scratch / "state"), gate=Gate(True, "stub"), home=home)
    return p, ws, home


def ask(p, ws, home, cmd, cwd=None):
    return p.handle_pretool({"op": "pretool", "session_id": "sess1", "cwd": str(cwd or ws), "command": cmd,
                             "description": "", "transcript_path": "",
                             "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home)},
                             "deadline_ms": 30000})


def apply(p, d):
    out, err = io.BytesIO(), io.BytesIO()
    code = apply_run(p.store, d.run_id, d.token, cfg=p.cfg, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_allow_commit_round_trip(env):
    p, ws, home = env
    d = ask(p, ws, home, "echo generated > out.txt && echo done")
    assert (d.decision, d.mode) == ("allow", "commit") and d.token
    assert not (ws / "out.txt").exists()
    code, out, _ = apply(p, d)
    assert code == 0 and out == b"done\n"
    assert (ws / "out.txt").read_text() == "generated\n"


def test_untracked_delete_asks_and_real_file_survives(env):
    p, ws, home = env
    d = ask(p, ws, home, "rm keep.txt")
    assert (d.decision, d.mode) == ("ask", "commit") and "H1.unrecoverable" in d.rule_ids
    assert (ws / "keep.txt").exists()


def test_indirect_script_delete_is_caught(env):
    p, ws, home = env
    (ws / "clean.sh").write_text("#!/bin/sh\nV=-rf\nrm $V src\n")
    d = ask(p, ws, home, "bash clean.sh")
    assert d.decision == "ask" and "H1.unrecoverable" in d.rule_ids
    assert (ws / "src" / "main.py").exists()


def test_decoy_exposure_is_denied(env):
    p, ws, home = env
    d = ask(p, ws, home, "cat ~/.ssh/id_ed25519")
    assert d.decision == "deny" and d.rule_ids[0] == "H5.decoy"


def test_network_attempt_asks_rerun(env):
    p, ws, home = env
    d = ask(p, ws, home, "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 2)\" || true")
    assert (d.decision, d.mode) == ("ask", "rerun") and "H7.network" in d.rule_ids


def test_write_outside_workspace_asks_rerun(env):
    p, ws, home = env
    d = ask(p, ws, home, "touch ~/should_not_exist; true")
    assert (d.decision, d.mode) == ("ask", "rerun") and "H2.outside_workspace" in d.rule_ids
    assert not (home / "should_not_exist").exists()


def test_pipeline_refuses_broad_or_mounted_workspace(env, monkeypatch):
    p, ws, home = env
    d = ask(p, ws, home, "rm -rf x", cwd=home)
    assert d.decision == "ask" and "S.workspace" in d.rule_ids
    monkeypatch.setattr(pipeline_mod, "submounts", lambda root: [str(root) + "/mnt"])
    d = ask(p, ws, home, "rm -rf x")
    assert d.decision == "ask" and "mount" in d.reason


def test_pipeline_lower_changed(env):
    p, ws, home = env
    threading.Timer(1.0, lambda: (ws / "keep.txt").write_text("edited by user\n")).start()
    d = ask(p, ws, home, "sleep 2; echo x > y.txt")
    assert (d.decision, d.mode) == ("ask", "rerun")


def test_pipeline_parallel_runs_are_independent(env):
    p, ws, home = env
    results = {}

    def go(name):
        results[name] = ask(p, ws, home, f"echo {name} > {name}.txt")

    threads = [threading.Thread(target=go, args=(n,)) for n in ("a", "b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert {r.decision for r in results.values()} == {"allow"}
    assert results["a"].run_id != results["b"].run_id
    for name in ("a", "b"):
        assert apply(p, results[name])[0] == 0
    assert (ws / "a.txt").read_text() == "a\n" and (ws / "b.txt").read_text() == "b\n"


def test_git_reset_hard_is_caught(env):
    p, ws, home = env
    g = lambda *a: subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True,  # noqa: E731
                                  env={"PATH": "/usr/bin:/bin", "HOME": str(home), "GIT_AUTHOR_NAME": "t",
                                       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                       "GIT_COMMITTER_EMAIL": "t@t"})
    g("init", "-q", "-b", "main")
    g("add", ".")
    g("commit", "-q", "-m", "one")
    (ws / "src" / "main.py").write_text("print('two')\n")
    g("commit", "-q", "-am", "two")
    (ws / "src" / "main.py").write_text("uncommitted work\n")
    d = ask(p, ws, home, "git reset -q --hard HEAD~1")
    assert d.decision == "ask"
    assert "H3.ref_rewound" in d.rule_ids and "H1.unrecoverable" in d.rule_ids
    assert (ws / "src" / "main.py").read_text() == "uncommitted work\n"


def test_workspace_under_tmp(scratch: Path):
    ws = Path("/tmp") / f"dryrun-test-{uuid.uuid4().hex[:8]}" / "ws"
    ws.mkdir(parents=True)
    try:
        cfg = load_config(use_user_file=False)
        p = Pipeline(cfg, Store(scratch / "state"), gate=Gate(True, "stub"), home=scratch)
        d = ask(p, ws, scratch, "echo t > made.txt")
        assert (d.decision, d.mode) == ("allow", "commit"), d.reason
        assert apply(p, d)[0] == 0 and (ws / "made.txt").exists()
    finally:
        subprocess.run(["rm", "-rf", str(ws.parent)])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_pipeline_fast.py tests/unit/test_daemon.py tests/sandbox/test_pipeline.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.pipeline'`)

- [ ] **Step 3: Implement**

`src/dryrun/rpc.py`:
```python
"""dryrun.rpc/1: one newline-terminated JSON request and one JSON response per unix-socket connection.
Stdlib only (the hook imports this)."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

MAX_LINE = 4 * 1024**2


def call(sock_path: Path, request: dict, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(max(0.05, timeout))
        s.connect(str(sock_path))
        s.sendall(json.dumps(request).encode() + b"\n")
        buf = bytearray()
        while not buf.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("dryrund did not answer in time")
            s.settimeout(remaining)
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise ValueError("response too large")
    obj = json.loads(bytes(buf).decode())
    if not isinstance(obj, dict):
        raise ValueError("response is not an object")
    return obj


async def read_request(reader) -> dict:
    line = await reader.readuntil(b"\n")
    if len(line) > MAX_LINE:
        raise ValueError("request too large")
    obj = json.loads(line.decode())
    if not isinstance(obj, dict):
        raise ValueError("request is not an object")
    return obj


async def write_response(writer, obj: dict) -> None:
    writer.write(json.dumps(obj).encode() + b"\n")
    await writer.drain()
```

`src/dryrun/pipeline.py`:
```python
"""One pretool request end to end: triage -> shadow -> effects -> judge -> decision (spec F2-F9)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from dryrun.config import Config
from dryrun.effects import gitstate
from dryrun.effects.record import annotate, build_record, cache_counts, compute_flags, git_effects
from dryrun.effects.upper import extract
from dryrun.fingerprint import digest, fingerprint_tree, submounts
from dryrun.judge.cascade import decide
from dryrun.judge.model import EffectJudge, NullJudge
from dryrun.judge.rules import evaluate
from dryrun.paths import bwrap_path
from dryrun.runpaths import RunPaths
from dryrun.sandbox.assemble import prepare
from dryrun.sandbox.decoys import scan
from dryrun.sandbox.spawn import SandboxError, run_shadow
from dryrun.sandbox.trace import parse_trace_file
from dryrun.store import Store, TokenError
from dryrun.triage import classify
from dryrun.types import ChangeSet, Decision

log = logging.getLogger("dryrun")
SCAN_LIMIT = 256 * 1024**2


@dataclass
class Gate:
    ok: bool | None = None
    detail: str = "isolation self-test has not finished"


def _read_capped(path: Path, cap: int) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(cap)
    except FileNotFoundError:
        return b""


class Pipeline:
    def __init__(self, cfg: Config, store: Store, *, judge: EffectJudge | None = None, gate: Gate | None = None,
                 home: Path | None = None, bwrap: str | None = None) -> None:
        self.cfg, self.store = cfg, store
        self.judge = judge or NullJudge()
        self.gate = gate or Gate()
        self.home = Path(home) if home is not None else Path.home()
        self.bwrap = bwrap or str(bwrap_path())

    # --- public ----------------------------------------------------------------------------------
    def handle_prompt(self, session_id: str, text: str) -> None:
        self.store.set_request(session_id, text)

    def handle_pretool(self, req: dict, cancel: threading.Event | None = None) -> Decision:
        t0 = time.monotonic()
        timings: dict[str, int] = {}
        record_json = None
        try:
            decision, record_json = self._decide(req, cancel, timings)
        except Exception as exc:  # fail to ask, never allow
            log.error("pipeline error: %s\n%s", exc, traceback.format_exc())
            decision = Decision("ask", "passthrough", f"Dry Run internal error: {str(exc)[:120]}", ["S.error"])
        self.store.log_decision({
            "ts": time.time(), "session_id": req.get("session_id"), "run_id": decision.run_id,
            "command": str(req.get("command", ""))[:500], "decision": decision.decision, "mode": decision.mode,
            "rule_ids": decision.rule_ids, "reason": decision.reason,
            "latency_ms": int((time.monotonic() - t0) * 1000), "timings_ms": timings, "record": record_json,
        })
        return decision

    # --- internals -------------------------------------------------------------------------------
    def _token_valid(self, run_id: str, token: str) -> bool:
        try:
            run = self.store.run(run_id)
        except TokenError:
            return False
        meta = self.store.load_meta(run)
        want = meta.get("token_sha256", "")
        got = hashlib.sha256(token.encode()).hexdigest()
        return meta.get("status") in ("authorized", "pending") and hmac.compare_digest(want, got)

    def _workspace_problem(self, ws_root: Path) -> str | None:
        if not ws_root.is_dir():
            return f"working directory {ws_root} does not exist"
        if ws_root in (Path("/"), self.home) or ws_root == Path.home():
            return f"workspace {ws_root} is too broad (home or /); open a project directory"
        state = self.store.root
        for a, b in ((state, ws_root), (ws_root, state)):
            try:
                a.relative_to(b)
                return "workspace overlaps the Dry Run state directory"
            except ValueError:
                pass
        mounts = submounts(ws_root)
        if mounts:
            return f"workspace contains mount points ({', '.join(mounts[:3])})"
        return None

    def _decide(self, req: dict, cancel: threading.Event | None, timings: dict) -> tuple[Decision, dict | None]:
        session_id = str(req.get("session_id", ""))
        command = str(req.get("command", ""))
        cwd = Path(str(req.get("cwd") or "/"))
        ws_root = gitstate.find_workspace_root(cwd)
        t = time.monotonic()
        tri = classify(command, ws_root, self.cfg.policy, home=self.home)
        timings["triage"] = int((time.monotonic() - t) * 1000)
        if tri.cls == "apply":
            if tri.apply_args and self._token_valid(*tri.apply_args):
                return Decision("allow", "passthrough", "commit of a reviewed Dry Run result"), None
            return decide("apply"), None
        if tri.cls != "shadow":
            return decide(tri.cls, triage_reason=tri.reason, text=tri.text, dev_server_allowed=tri.dev_server), None
        problem = self._workspace_problem(ws_root)
        if problem:
            return Decision("ask", "passthrough", f"Dry Run cannot shadow here: {problem}", ["S.workspace"]), None
        if self.gate.ok is not True:
            return Decision("ask", "passthrough", f"Dry Run isolation self-test failed or pending: {self.gate.detail}",
                            ["S.canary"]), None
        run = self.store.new_run()
        try:
            return self._shadow(run, req, tri, ws_root, cwd, session_id, command, cancel, timings)
        except SandboxError as exc:
            self.store.finish(run, "failed")
            self.store.remove_run(run)
            return Decision("ask", "passthrough", f"Dry Run sandbox error: {str(exc)[:150]}", ["S.sandbox_error"],
                            run_id=run.run_id), None
        except BaseException:
            self.store.finish(run, "failed")
            self.store.remove_run(run)
            raise

    def _shadow(self, run: RunPaths, req: dict, tri, ws_root: Path, cwd: Path, session_id: str, command: str,
                cancel: threading.Event | None, timings: dict) -> tuple[Decision, dict]:
        cfg, policy = self.cfg, self.cfg.policy
        t = time.monotonic()
        prep = prepare(run, ws_root=ws_root, cwd=cwd, command=command, env=dict(req.get("env") or {}), cfg=cfg,
                       home=self.home, state=self.store.root, bwrap=self.bwrap)
        snap = gitstate.snapshot(ws_root)
        timings["prepare"] = int((time.monotonic() - t) * 1000)
        t = time.monotonic()
        res = run_shadow(prep.spec, run_id=run.run_id, out_dir=run.root, cfg=cfg.shadow, watch_fs=run.root,
                         cancel=cancel)
        timings["run"] = int((time.monotonic() - t) * 1000)
        t = time.monotonic()
        lower_changed = digest(fingerprint_tree(ws_root)) != digest(prep.ws_base)
        ws_eff = extract("workspace", ws_root, run.ws_up, prep.ws_base)
        tmp_eff = extract("tmp", run.tmp_lower, run.tmp_up, prep.tmp.base_fps, skip=prep.tmp_skip,
                          seq_start=len(ws_eff.ops))
        annotate(ws_eff.entries, snap, policy, self.store.ledger(session_id, str(ws_root)), ws_eff.contents,
                 blob_exists=lambda shas: gitstate.objects_exist(ws_root, shas))
        git = git_effects(ws_root, run, ws_eff.entries) if snap.is_repo else {}
        trace = parse_trace_file(res.trace_path)
        stdout = _read_capped(res.stdout_path, cfg.shadow.output_max)
        stderr = _read_capped(res.stderr_path, cfg.shadow.output_max)
        blobs, budget = {"stdout": stdout, "stderr": stderr}, SCAN_LIMIT
        for area, eff in (("workspace", ws_eff), ("tmp", tmp_eff)):
            for rel, path in eff.contents.items():
                data = _read_capped(path, min(budget, 16 * 1024**2))
                budget -= len(data)
                blobs[f"{area}/{rel}"] = data
                if budget <= 0:
                    break
        decoy_hits = scan(prep.token, blobs)
        flags = compute_flags(res=res, trace=trace, ws_eff=ws_eff, tmp_eff=tmp_eff, lower_changed=lower_changed,
                              tmp_partial=prep.tmp.partial, output=stdout + stderr)
        files, size = cache_counts(prep.caches)
        request = self.store.get_request(session_id)
        record = build_record(
            run_id=run.run_id, session_id=session_id, command=command, cwd=str(cwd), ws_root=str(ws_root),
            request_text=request, request_source="UserPromptSubmit" if request else None,
            triage_class=tri.cls, triage_reason=tri.reason, res=res, ws_eff=ws_eff, tmp_eff=tmp_eff,
            cache_files=files, cache_bytes=size, git_is_repo=snap.is_repo, git=git, trace=trace,
            decoy_hits=decoy_hits, flags=flags)
        cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws_root), "tmp": "/tmp"},
                       base_digest=digest(prep.ws_base), ops=ws_eff.ops + tmp_eff.ops,
                       refused=ws_eff.refused + tmp_eff.refused)
        contents = ws_eff.contents
        hits = evaluate(record, policy, read=lambda rel: _read_capped(contents[rel], 1 << 20) if rel in contents else None)
        decision = decide("shadow", rec=record, hits=hits, judge=self.judge)
        timings["judge"] = int((time.monotonic() - t) * 1000)
        record_json = record.to_json()
        decision.run_id = run.run_id
        if decision.mode == "commit" and decision.decision in ("allow", "ask"):
            run.record.write_text(json.dumps(record_json))
            run.changeset.write_text(json.dumps(cs.to_json()))
            decision.token = self.store.authorize(run, session_id=session_id, decision=decision.decision)
            self.store.save_meta(run, exit_code=res.exit_code)
        else:
            self.store.finish(run, {"deny": "denied"}.get(decision.decision, decision.mode))
            self.store.remove_run(run)
        return decision, record_json
```

`src/dryrun/daemon.py`:
```python
"""dryrund: long-lived asyncio server behind the hook (spec F2, F13, F16, F17)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from dryrun import rpc
from dryrun.commit import recover_all
from dryrun.config import Config, load_config
from dryrun.paths import socket_path, state_dir
from dryrun.pipeline import Gate, Pipeline
from dryrun.sandbox.spawn import preflight
from dryrun.store import Store
from dryrun.types import Decision

log = logging.getLogger("dryrun")


def _ask(reason: str, rule: str) -> dict:
    return Decision("ask", "passthrough", reason, [rule]).to_json()


class Daemon:
    def __init__(self, cfg: Config, store: Store, pipeline, sock_path: Path, *,
                 canary_fn: Callable[[], tuple[bool, str]] | None = None) -> None:
        self.cfg, self.store, self.pipeline = cfg, store, pipeline
        self.sock_path = Path(sock_path)
        self.canary_fn = canary_fn
        self.executor = ThreadPoolExecutor(max_workers=max(1, cfg.shadow.max_concurrent))
        self._server: asyncio.base_events.Server | None = None
        self._stopping = threading.Event()

    def _run_canaries(self) -> None:
        gate: Gate = getattr(self.pipeline, "gate", Gate())
        while not self._stopping.is_set():
            try:
                ok, detail = self.canary_fn() if self.canary_fn else (False, "no self-test configured")
            except Exception as exc:
                ok, detail = False, f"self-test crashed: {exc}"
            gate.ok, gate.detail = ok, detail
            (log.info if ok else log.error)("isolation self-test: %s (%s)", "PASS" if ok else "FAIL", detail)
            if self._stopping.wait(self.cfg.shadow.canary_interval_h * 3600):
                return

    def _peer_ok(self, writer) -> bool:
        sock = writer.get_extra_info("socket")
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            return uid == os.getuid()
        except OSError:
            return False

    async def _handle(self, reader, writer) -> None:
        resp: dict
        try:
            if not self._peer_ok(writer):
                writer.close()
                return
            req = await asyncio.wait_for(rpc.read_request(reader), 5)
            op = req.get("op")
            if op == "prompt":
                await asyncio.get_running_loop().run_in_executor(
                    None, self.pipeline.handle_prompt, str(req.get("session_id", "")), str(req.get("text", "")))
                resp = {"ok": True}
            elif op == "status":
                gate = getattr(self.pipeline, "gate", Gate())
                resp = {"ok": True, "gate_ok": gate.ok, "gate_detail": gate.detail}
            elif op == "pretool":
                resp = await self._pretool(req)
            else:
                resp = {"ok": False, "error": f"unknown op {op!r}"}
        except Exception as exc:
            resp = _ask(f"Dry Run error: {str(exc)[:120]}", "S.error")
        try:
            await rpc.write_response(writer, resp)
        finally:
            writer.close()

    async def _pretool(self, req: dict) -> dict:
        deadline_s = float(req.get("deadline_ms", 58_000)) / 1000 - 0.5
        deadline_s = max(0.5, min(deadline_s, self.cfg.shadow.wall_clock_s + 25))
        cancel = threading.Event()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.executor, self.pipeline.handle_pretool, req, cancel)
        try:
            decision = await asyncio.wait_for(asyncio.shield(fut), deadline_s)
        except asyncio.TimeoutError:
            cancel.set()
            return _ask("Dry Run could not finish in time; review manually", "S.deadline")
        return decision.to_json()

    async def serve(self) -> None:
        if self.sock_path.exists():
            try:
                rpc.call(self.sock_path, {"op": "status"}, 1)
                raise RuntimeError(f"dryrund already running on {self.sock_path}")
            except (OSError, ValueError, TimeoutError):
                self.sock_path.unlink()
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.sock_path))
        finally:
            os.umask(old)
        threading.Thread(target=self._run_canaries, daemon=True).start()
        loop = asyncio.get_running_loop()
        cleanup = loop.create_task(self._cleanup_loop())
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            cleanup.cancel()
            self._stopping.set()
            self.executor.shutdown(wait=False, cancel_futures=True)
            try:
                self.sock_path.unlink()
            except FileNotFoundError:
                pass

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                self.store.cleanup(ttl_s=self.cfg.policy.pending_ttl_min * 60)
            except Exception as exc:
                log.error("cleanup failed: %s", exc)
            await asyncio.sleep(60)

    def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()
            for task in asyncio.all_tasks():
                task.cancel()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dryrund")
    ap.add_argument("--allow-root", action="store_true")
    ap.add_argument("--config", type=Path)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s dryrund %(levelname)s %(message)s")
    if os.geteuid() == 0 and not args.allow_root:
        print("dryrund: refusing to run as root (use a normal user or --allow-root)", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    store = Store(state_dir())
    for run_id in recover_all(store):
        log.warning("completed interrupted commit %s", run_id)
    problems = preflight(cfg.shadow, allow_root=args.allow_root)
    gate = Gate(ok=None if not problems else False, detail="; ".join(problems) or "pending")
    pipeline = Pipeline(cfg, store, gate=gate)
    if problems:
        canary_fn = lambda: (False, "; ".join(problems))  # noqa: E731
    else:
        from dryrun.canary import run_gate
        canary_fn = lambda: run_gate(cfg, store)  # noqa: E731
    daemon = Daemon(cfg, store, pipeline, socket_path(), canary_fn=canary_fn)
    try:
        asyncio.run(daemon.serve())
    except KeyboardInterrupt:
        pass
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_pipeline_fast.py tests/unit/test_daemon.py tests/sandbox/test_pipeline.py -q`
Expected: PASS. `dryrun.canary` is imported lazily, only in `main`, so the tests don't need Task 19.

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/pipeline.py src/dryrun/rpc.py src/dryrun/daemon.py tests/unit/test_pipeline_fast.py tests/unit/test_daemon.py tests/sandbox/test_pipeline.py
git commit -m "feat: pretool pipeline, rpc framing and dryrund with deadlines and self-test gate"
```
