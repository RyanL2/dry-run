from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from dataclasses import replace

import pytest

from dryrun.sandbox.layout import OverlaySpec
from dryrun.sandbox.spawn import SandboxError, preflight, run_readonly, run_shadow
from tests.sandbox.conftest import simple_spec

pytestmark = pytest.mark.sandbox


def shadow(ws, run_dir, cfg, cmd, **kw):
    return run_shadow(simple_spec(ws, run_dir, cmd), run_id="t" + os.urandom(3).hex(), out_dir=run_dir,
                      cfg=cfg, watch_fs=run_dir, **kw)


def test_preflight_is_clean_in_dev_env(shadow_cfg):
    assert preflight(shadow_cfg) == []


def test_runs_command_and_captures_output_exit_and_trace(ws, run_dir, shadow_cfg):
    res = shadow(ws, run_dir, shadow_cfg, "echo hello; echo oops >&2; exit 7")
    assert res.exit_code == 7 and not res.timed_out and res.killed_reason is None
    assert res.stdout_path.read_text() == "hello\n"
    assert "oops" in res.stderr_path.read_text()
    assert "execve(" in res.trace_path.read_text()


def test_writes_land_in_upper_not_in_real_workspace(ws, run_dir, shadow_cfg):
    res = shadow(ws, run_dir, shadow_cfg, "echo new > new.txt && rm keep.txt")
    assert res.exit_code == 0
    assert (ws / "keep.txt").read_text() == "keep\n"
    assert not (ws / "new.txt").exists()
    assert (run_dir / "ws.up" / "new.txt").read_text() == "new\n"
    st = os.lstat(run_dir / "ws.up" / "keep.txt")
    assert stat.S_ISCHR(st.st_mode) and st.st_rdev == 0  # whiteout


def test_timeout_kills_whole_tree(ws, run_dir, shadow_cfg):
    cfg = replace(shadow_cfg, wall_clock_s=1)
    t0 = time.monotonic()
    res = shadow(ws, run_dir, cfg, "setsid sleep 31.25 & sleep 31.25")
    assert res.timed_out and res.killed_reason == "timeout" and res.exit_code is None
    assert time.monotonic() - t0 < 8
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep 31.25"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


def test_cancel_event_stops_run(ws, run_dir, shadow_cfg):
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    res = shadow(ws, run_dir, shadow_cfg, "sleep 20", cancel=ev)
    assert res.killed_reason == "cancelled"


def test_setup_failure_raises_sandbox_error(ws, run_dir, shadow_cfg):
    spec = simple_spec(ws, run_dir, "true")
    bad = replace(spec, overlays=(OverlaySpec("/nonexistent-lower", spec.overlays[0].upper,
                                              spec.overlays[0].work, str(ws)),))
    with pytest.raises(SandboxError):
        run_shadow(bad, run_id="bad1", out_dir=run_dir, cfg=shadow_cfg, watch_fs=run_dir)


def test_run_readonly_hides_secrets_and_state_and_limits_memory(scratch, monkeypatch):
    from pathlib import Path
    state = scratch / "state"
    state.mkdir()
    (state / "tokens").write_text("TOKEN-IN-STATE")
    monkeypatch.setenv("DRYRUN_STATE_DIR", str(state))
    secret_dir = Path.home() / ".aws"
    created = not secret_dir.exists()
    secret_dir.mkdir(exist_ok=True)
    secret = secret_dir / "dryrun_test_secret"
    secret.write_text("REAL-AWS-SECRET")
    try:
        r = run_readonly(["sh", "-c", f"cat {secret} {state}/tokens 2>/dev/null; ls {state} | wc -l"], cwd=scratch)
        assert b"REAL-AWS-SECRET" not in r.stdout and b"TOKEN-IN-STATE" not in r.stdout
        assert r.stdout.strip().endswith(b"0")
        big = run_readonly(["python3", "-c", "b = bytearray(3 * 1024**3); print('LEAK')"], cwd=scratch)
        assert b"LEAK" not in big.stdout and big.returncode != 0
    finally:
        secret.unlink()
        if created:
            secret_dir.rmdir()


def test_run_readonly_works_and_hides_secrets_behind_symlinks(scratch):
    """Common setups: ~/.ssh -> /mnt/c/Users/X/.ssh, dotfile managers symlinking ~/.npmrc."""
    from pathlib import Path
    home = Path.home()
    links = {".docker": scratch / "real-docker", ".npmrc": scratch / "real-npmrc", ".pypirc": scratch / "gone"}
    if any(os.path.lexists(home / name) for name in links):
        pytest.skip("test user already has one of these secret paths")
    (scratch / "real-docker").mkdir()
    (scratch / "real-docker" / "config.json").write_text("DOCKER-SECRET")
    (scratch / "real-npmrc").write_text("NPM-SECRET")
    try:
        for name, target in links.items():
            (home / name).symlink_to(target)
        r = run_readonly(["sh", "-c", "cat ~/.docker/config.json ~/.npmrc 2>/dev/null; echo done"], cwd=scratch)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == b"done"
    finally:
        for name in links:
            if (home / name).is_symlink():
                (home / name).unlink()


def test_run_readonly_cannot_write_and_returns_output(ws):
    ok = run_readonly(["bash", "-c", "echo ok; touch x"], cwd=ws)
    assert ok.stdout == b"ok\n"
    assert ok.returncode != 0
    assert not (ws / "x").exists()


def test_run_readonly_command_reports_a_sandbox_that_did_not_start(scratch):
    """bwrap's own error must never be replayed to the agent as if the command had printed it."""
    from dryrun.sandbox.spawn import run_readonly_command
    out = scratch / "out"
    out.mkdir()
    with pytest.raises(SandboxError):
        run_readonly_command("git status", cwd=scratch / "missing", env={}, home=scratch, out_dir=out,
                             timeout=10, output_max=1 << 20)


def test_run_readonly_command_caps_output_and_keeps_exit_code(scratch):
    from dryrun.sandbox.spawn import run_readonly_command
    out = scratch / "out"
    out.mkdir()
    r = run_readonly_command("echo hi; echo err >&2; exit 3", cwd=scratch, env={}, home=scratch, out_dir=out,
                             timeout=10, output_max=1 << 20)
    assert (r.exit_code, r.truncated, r.killed_reason) == (3, False, None)
    assert r.stdout_path.read_bytes() == b"hi\n" and r.stderr_path.read_bytes() == b"err\n"
    big = run_readonly_command("head -c 100000 /dev/zero", cwd=scratch, env={}, home=scratch, out_dir=out,
                               timeout=10, output_max=1000)
    assert big.truncated and big.stdout_path.stat().st_size <= 1001
