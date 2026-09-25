from __future__ import annotations

import os
from pathlib import Path

import pytest

from dryrun.effects.upper import extract
from dryrun.fingerprint import fingerprint_tree
from dryrun.sandbox.spawn import run_shadow
from tests.sandbox.conftest import simple_spec

pytestmark = pytest.mark.sandbox


def shadow_extract(ws: Path, run_dir: Path, cfg, cmd: str):
    base = fingerprint_tree(ws)
    res = run_shadow(simple_spec(ws, run_dir, cmd), run_id="u" + os.urandom(3).hex(), out_dir=run_dir,
                     cfg=cfg, watch_fs=run_dir)
    assert res.exit_code == 0, res.stderr_path.read_text()
    return extract("workspace", ws, run_dir / "ws.up", base)


def test_real_kernel_whiteouts_and_dir_rename(ws, run_dir, shadow_cfg):
    (ws / "d").mkdir()
    (ws / "d" / "f.txt").write_text("f")
    eff = shadow_extract(ws, run_dir, shadow_cfg, "rm keep.txt && mv d e && : > e/f.txt")
    e = {x.path: x for x in eff.entries}
    assert e["keep.txt"].op == "delete"
    assert e["d/f.txt"].op == "delete"
    assert e["e/f.txt"].op == "create" and e["e/f.txt"].bytes_after == 0
    assert eff.refused == []


def test_real_touch_and_chmod(ws, run_dir, shadow_cfg):
    eff = shadow_extract(ws, run_dir, shadow_cfg, "chmod +x keep.txt")
    (op,) = eff.ops
    assert op.op == "chmod" and op.mode & 0o111
