"""One pretool request end to end: triage -> shadow -> effects -> judge -> decision (spec F2-F9)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
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
    def _token_valid(self, run_id: str, token: str, session_id: str) -> bool:
        """Only an `allow` result of this same session. A `pending` token belongs to an ask the user may
        have declined; Claude Code does not re-run hooks on updatedInput (spike 0), so the legitimate
        approved-ask path never reaches this check."""
        try:
            run = self.store.run(run_id)
        except TokenError:
            return False
        meta = self.store.load_meta(run)
        want = str(meta.get("token_sha256", ""))
        got = hashlib.sha256(token.encode()).hexdigest()
        return (meta.get("status") == "authorized" and meta.get("session_id") == session_id
                and hmac.compare_digest(want, got))

    def _workspace_problem(self, ws_root: Path) -> str | None:
        if not ws_root.is_dir():
            return f"working directory {ws_root} does not exist"
        broad = {Path("/"), Path(os.path.realpath(self.home)), Path(os.path.realpath(Path.home()))}
        if ws_root in broad:
            return f"workspace {ws_root} is too broad (home or /); open a project directory"
        state = Path(os.path.realpath(self.store.root))
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
        # Resolve symlinks first: every workspace check and the bwrap spec must see the real path.
        cwd = Path(os.path.realpath(str(req.get("cwd") or "/")))
        ws_root = gitstate.find_workspace_root(cwd)
        env = {str(k): str(v) for k, v in dict(req.get("env") or {}).items()}
        t = time.monotonic()
        tri = classify(command, ws_root, self.cfg.policy, home=self.home, env=env)
        timings["triage"] = int((time.monotonic() - t) * 1000)
        if tri.cls == "apply":
            if tri.apply_args and self._token_valid(*tri.apply_args, session_id):
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
                if budget <= 0:
                    break
                data = _read_capped(path, min(budget, 16 * 1024**2))
                budget -= len(data)
                blobs[f"{area}/{rel}"] = data
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
        hits = evaluate(record, policy,
                        read=lambda rel: _read_capped(contents[rel], 1 << 20) if rel in contents else None)
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
