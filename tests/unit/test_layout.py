from __future__ import annotations

from pathlib import Path

from dryrun.sandbox.layout import OverlaySpec, SandboxSpec, bwrap_argv, existing


def spec(**kw) -> SandboxSpec:
    base = dict(
        bwrap="/opt/bwrap",
        overlays=(OverlaySpec("/s/tmp.lower", "/s/tmp.up", "/s/tmp.wk", "/tmp"),
                  OverlaySpec("/home/u/ws", "/s/ws.up", "/s/ws.wk", "/home/u/ws")),
        hide_early=("/run",), hide_late=("/home/u/.local/state/dryrun",),
        ro_binds=(("/s/decoys/0", "/home/u/.ssh"),),
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/u"}, cwd="/home/u/ws/sub",
        argv=("bash", "-c", "rm -rf build"), seccomp_fd=7,
    )
    base.update(kw)
    return SandboxSpec(**base)


def test_argv_order_and_isolation_flags():
    a = bwrap_argv(spec())
    assert a[0] == "/opt/bwrap"
    for flag in ["--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]:
        assert flag in a
    assert a[a.index("--cap-drop") + 1] == "ALL"
    i_root = a.index("--ro-bind")
    i_run = a.index("/run")
    i_tmp = a.index("/s/tmp.lower")
    i_ws = a.index("/s/ws.up")
    i_state = a.index("/home/u/.local/state/dryrun")
    i_decoy = a.index("/s/decoys/0")
    assert a[i_root:i_root + 3] == ["--ro-bind", "/", "/"]
    assert i_root < i_run < i_tmp < i_ws < i_state < i_decoy
    assert a[a.index("--seccomp") + 1] == "7"
    assert a[a.index("--chdir") + 1] == "/home/u/ws/sub"
    assert a[-4:] == ["--", "bash", "-c", "rm -rf build"]


def test_overlay_triplet():
    a = bwrap_argv(spec())
    i = a.index("--overlay-src")
    assert a[i:i + 6] == ["--overlay-src", "/s/tmp.lower", "--overlay", "/s/tmp.up", "/s/tmp.wk", "/tmp"]


def test_env_is_cleared_then_set_sorted():
    a = bwrap_argv(spec())
    i = a.index("--clearenv")
    assert a[i + 1:i + 7] == ["--setenv", "HOME", "/home/u", "--setenv", "PATH", "/usr/bin:/bin"]


def test_no_seccomp_flag_when_fd_missing():
    assert "--seccomp" not in bwrap_argv(spec(seccomp_fd=None))


def test_existing_filters_missing_paths(tmp_path: Path):
    (tmp_path / "a").mkdir()
    assert existing([str(tmp_path / "a"), str(tmp_path / "missing")]) == (str(tmp_path / "a"),)
