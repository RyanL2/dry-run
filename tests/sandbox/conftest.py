from __future__ import annotations

from pathlib import Path

import pytest

from dryrun.config import load_config, with_shadow
from dryrun.paths import bwrap_path
from dryrun.sandbox.layout import OverlaySpec, SandboxSpec, existing


@pytest.fixture
def shadow_cfg():
    return with_shadow(load_config(use_user_file=False), wall_clock_s=10, memory_max=512 * 1024**2,
                       tasks_max=128, disk_budget=256 * 1024**2).shadow


@pytest.fixture
def ws(scratch: Path) -> Path:
    w = scratch / "ws"
    w.mkdir()
    (w / "keep.txt").write_text("keep\n")
    return w


@pytest.fixture
def run_dir(scratch: Path) -> Path:
    r = scratch / "state" / "runs" / "r1"
    for sub in ("ws.up", "ws.wk"):
        (r / sub).mkdir(parents=True)
    return r


def simple_spec(ws: Path, run_dir: Path, command: str) -> SandboxSpec:
    return SandboxSpec(
        bwrap=str(bwrap_path()),
        overlays=(OverlaySpec(str(ws), str(run_dir / "ws.up"), str(run_dir / "ws.wk"), str(ws)),),
        hide_early=existing(["/run", "/mnt/wsl", "/mnt/wslg"]),
        hide_late=(str(run_dir.parent.parent),),
        ro_binds=(),
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},
        cwd=str(ws),
        argv=("bash", "-c", command),
    )
