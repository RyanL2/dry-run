from __future__ import annotations

from pathlib import Path

import pytest

from dryrun.config import load_config
from dryrun.paths import bwrap_path
from dryrun.runpaths import RunPaths
from dryrun.sandbox.assemble import prepare

pytestmark = pytest.mark.sandbox


def test_prepare_builds_overlays_hides_and_decoys(scratch: Path):
    home = scratch / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("REAL")
    (home / ".cache").mkdir()
    ws = scratch / "ws"
    ws.mkdir()
    state = scratch / "state"
    run = RunPaths(state / "runs" / "r1")
    p = prepare(run, ws_root=ws, cwd=ws, command="true", env={"PATH": "/bin", "GH_TOKEN": "x"},
                cfg=load_config(use_user_file=False), home=home, state=state, bwrap=str(bwrap_path()))
    targets = [o.target for o in p.spec.overlays]
    assert targets[:2] == ["/tmp", str(ws)]
    assert str(home / ".cache") in targets
    assert str(state) in p.spec.hide_late
    assert [dst for _, dst in p.spec.ro_binds] == [str(home / ".ssh")]
    assert "GH_TOKEN" not in p.spec.env
    assert p.spec.argv == ("bash", "-c", "true")
    assert p.token.startswith("DRT")
