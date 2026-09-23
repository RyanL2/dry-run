from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from dryrun import paths


def test_env_overrides(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DRYRUN_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("DRYRUN_SOCKET", str(tmp_path / "d.sock"))
    monkeypatch.setenv("DRYRUN_BWRAP", "/opt/bwrap")
    assert paths.state_dir() == tmp_path / "s"
    assert paths.socket_path() == tmp_path / "d.sock"
    assert paths.bwrap_path() == Path("/opt/bwrap")


def test_ensure_private_dir_sets_0700(tmp_path: Path):
    d = paths.ensure_private_dir(tmp_path / "a" / "b")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_ensure_private_dir_rejects_symlink(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(PermissionError):
        paths.ensure_private_dir(tmp_path / "link")
