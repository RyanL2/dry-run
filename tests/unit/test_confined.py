from __future__ import annotations

import os
from pathlib import Path

import pytest

from dryrun.confined import ConfinementError, Root


def test_rejects_traversal_and_symlinked_components(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "link").symlink_to(outside)
    with Root(ws) as r:
        for bad in ["../x", "/etc/passwd", "a//b", "", "real/../../x"]:
            with pytest.raises(ConfinementError):
                r.mkdir(bad)
        with pytest.raises(ConfinementError):
            r.mkdir("link/evil")
    assert list(outside.iterdir()) == []


def test_basic_ops(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src.txt"
    src.write_text("hello")
    with Root(ws) as r:
        r.mkdir("d")
        r.rename_in(str(src), "d/f.txt", noreplace=True)
        r.symlink("d/l", "f.txt")
        r.chmod("d/f.txt", 0o600)
        assert r.readlink("d/l") == "f.txt"
        assert r.lstat("d/f.txt").st_mode & 0o777 == 0o600
        os.chmod(ws / "d", 0)
        r.rmtree("d")
    assert list(ws.iterdir()) == []


def test_rename_noreplace_refuses_existing(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f").write_text("user")
    src = tmp_path / "s"
    src.write_text("shadow")
    with Root(ws) as r, pytest.raises(FileExistsError):
        r.rename_in(str(src), "f", noreplace=True)
    assert (ws / "f").read_text() == "user"


def test_chmod_never_follows_symlink(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "secret"
    target.write_text("s")
    os.chmod(target, 0o600)
    (ws / "l").symlink_to(target)
    with Root(ws) as r:
        r.chmod("l", 0o777)
    assert os.stat(target).st_mode & 0o777 == 0o600
