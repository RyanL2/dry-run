from __future__ import annotations

import io
import subprocess
import threading
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
    assert (d.decision, d.mode) == ("allow", "commit") and d.token, d.reason
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
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {r.decision for r in results.values()} == {"allow"}
    assert results["a"].run_id != results["b"].run_id
    for name in ("a", "b"):
        assert apply(p, results[name])[0] == 0
    assert (ws / "a.txt").read_text() == "a\n" and (ws / "b.txt").read_text() == "b\n"


def test_git_reset_hard_is_caught(env):
    p, ws, home = env

    def g(*a):
        subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(home), "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    g("init", "-q", "-b", "main")
    g("add", ".")
    g("commit", "-q", "-m", "one")
    (ws / "src" / "main.py").write_text("print('two')\n")
    g("commit", "-q", "-am", "two")
    (ws / "src" / "main.py").write_text("uncommitted work\n")
    d = ask(p, ws, home, "git reset -q --hard HEAD~1")
    assert d.decision == "ask", d.reason
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
