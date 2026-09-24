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


def test_symlinked_secrets_still_shadow_and_deny(scratch: Path):
    """~/.ssh as a symlink (WSL's /mnt/c, dotfile managers) must not stop the sandbox from starting."""
    home, keys = scratch / "home", scratch / "dotfiles" / "ssh"
    keys.mkdir(parents=True)
    (keys / "id_ed25519").write_text("REAL-PRIVATE-KEY\n")
    home.mkdir()
    (home / ".ssh").symlink_to(keys)
    ws = scratch / "ws"
    ws.mkdir()
    cfg = with_shadow(load_config(use_user_file=False), wall_clock_s=15)
    p = Pipeline(cfg, Store(scratch / "state"), gate=Gate(True, "stub"), home=home)
    d = ask(p, ws, home, "echo hi > x.txt")
    assert d.decision == "allow", d.reason
    d = ask(p, ws, home, "cat ~/.ssh/id_ed25519")
    assert d.decision == "deny" and d.rule_ids[0] == "H5.decoy", d.reason


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


def test_symlinked_cwd_is_resolved_before_workspace_checks(env, scratch):
    p, ws, home = env
    link = scratch / "link-to-home"
    link.symlink_to(home)
    d = ask(p, ws, home, "rm -rf x", cwd=link)
    assert d.decision == "ask" and "S.workspace" in d.rule_ids and "too broad" in d.reason


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


def test_plain_git_commit_is_fast_forward_not_rewound(env):
    p, ws, home = env

    def g(*a):
        subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(home), "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    g("init", "-q", "-b", "main")
    g("add", ".")
    g("commit", "-q", "-m", "one")
    (ws / "src" / "main.py").write_text("print('two')\n")
    d = ask(p, ws, home, "git -c user.name=t -c user.email=t@t commit -qam two")
    assert "H3.ref_rewound" not in d.rule_ids, d.reason
    assert d.decision == "allow", d.reason


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


# --- git reads run in the read-only sandbox, never natively -------------------------------------------
GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _repo(ws: Path, home: Path) -> None:
    for a in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first commit"]):
        subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, env={**GIT_ENV, "HOME": str(home)})


def _plant_fsmonitor(repo: Path, markers: list[Path]) -> None:
    """core.fsmonitor names a program that `git status` runs; it stands in for every config-driven program
    (textconv, credential helpers, uploadpack in partial clones, ...)."""
    hook = repo / ".git" / "fsmon.sh"
    hook.write_text("#!/bin/sh\ntouch " + " ".join(str(m) for m in markers) + "\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)


def test_git_read_runs_in_the_readonly_sandbox_and_replays_output(env):
    p, ws, home = env
    _repo(ws, home)
    d = ask(p, ws, home, "git log --oneline")
    assert (d.decision, d.mode) == ("allow", "commit") and d.token, d.reason
    code, out, _ = apply(p, d)
    assert code == 0 and b"first commit" in out


def test_git_config_programs_cannot_touch_the_real_system(env):
    p, ws, home = env
    _repo(ws, home)
    markers = [home / "pwned", ws / "pwned"]
    _plant_fsmonitor(ws, markers)
    native = subprocess.run(["git", "status"], cwd=ws, capture_output=True, env={**GIT_ENV, "HOME": str(home)})
    assert native.returncode == 0 and all(m.exists() for m in markers), "the planted program must really run"
    for m in markers:
        m.unlink()
    d = ask(p, ws, home, "git status")
    assert (d.decision, d.mode) == ("allow", "commit"), d.reason
    code, out, _ = apply(p, d)
    assert code == 0 and b"branch main" in out
    assert not any(m.exists() for m in markers)


def test_git_read_from_a_subdirectory_with_an_invalid_dotgit(env):
    """Round 7: git skips an invalid pkg/.git and uses the repo above, whose config starts a program."""
    p, ws, home = env
    _repo(ws, home)
    markers = [home / "pwned", ws / "pwned"]
    _plant_fsmonitor(ws, markers)
    (ws / "pkg" / ".git").mkdir(parents=True)
    d = ask(p, ws, home, "git status", cwd=ws / "pkg")
    assert d.decision == "allow", d.reason
    assert apply(p, d)[0] == 0
    assert not any(m.exists() for m in markers)


def test_git_status_outside_a_repo_replays_the_git_error(env):
    p, ws, home = env
    d = ask(p, ws, home, "git status")
    assert (d.decision, d.mode) == ("allow", "commit"), d.reason
    code, _, err = apply(p, d)
    assert code == 128 and b"not a git repository" in err


def test_git_read_with_oversized_output_asks(env):
    p, ws, home = env
    (ws / "big.txt").write_text("x" * 4096 + "\n")
    _repo(ws, home)
    p.cfg = with_shadow(p.cfg, output_max=1024)
    d = ask(p, ws, home, "git show HEAD")
    assert (d.decision, d.mode) == ("ask", "passthrough") and "S.git_read_incomplete" in d.rule_ids, d.reason


# --- the brief's text-guard bypasses (anthropics/claude-code#85274, GuardFall) end to end ---------------
BYPASSES = {
    "85274 bash script": "bash clean.sh",
    "85274 python -c rmtree": "python3 -c 'import shutil; shutil.rmtree(\"src\")'",
    "85274 find -delete": "find src -name '*.py' -delete",
    "85274 xargs rm": "ls src/*.py | xargs rm",
    "85274 variable flags": "V=-rf; rm $V src",
    "GuardFall quote removal": "r''m -rf src",
    "GuardFall IFS": "rm${IFS}-rf${IFS}src",
    "GuardFall command substitution": "$(echo rm) -rf src",
    "GuardFall encoded pipeline": "echo cm0gLXJmIHNyYw== | base64 -d | sh",
    "GuardFall flag variant": "rm --recursive --force src",
    "GuardFall other binary": "perl -e 'use File::Path; rmtree(\"src\")'",
    "truncation": ": > src/main.py",
}


@pytest.mark.parametrize("name", list(BYPASSES))
def test_text_guard_bypasses_are_caught_by_their_effect(env, name):
    p, ws, home = env
    (ws / "clean.sh").write_text("#!/bin/sh\nrm -rf src\n")
    d = ask(p, ws, home, BYPASSES[name])
    assert d.decision == "ask" and "H1.unrecoverable" in d.rule_ids, (d.decision, d.reason)
    assert (ws / "src" / "main.py").read_text() == "print('hi')\n"
