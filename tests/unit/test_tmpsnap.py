from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

from dryrun.sandbox.tmpsnap import snapshot_tmp

LIMITS = dict(max_entries=100, max_total=10_000, max_file=1_000)


def test_copies_own_files_dirs_symlinks_and_preserves_metadata(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "d").mkdir(parents=True)
    (src / "d" / "x.sh").write_text("echo hi\n")
    os.chmod(src / "d" / "x.sh", 0o755)
    os.utime(src / "d" / "x.sh", ns=(1_000_000_000, 2_000_000_000))
    (src / "ln").symlink_to("d/x.sh")
    dst.mkdir()
    snap = snapshot_tmp(dst, src, **LIMITS)
    assert (dst / "d" / "x.sh").read_text() == "echo hi\n"
    assert os.stat(dst / "d" / "x.sh").st_mode & 0o777 == 0o755
    assert os.stat(dst / "d" / "x.sh").st_mtime_ns == 2_000_000_000
    assert os.readlink(dst / "ln") == "d/x.sh"
    assert set(snap.base_fps) == {"d", "d/x.sh", "ln"}
    assert snap.base_fps["d/x.sh"][0] == os.lstat(src / "d" / "x.sh").st_ino  # real inode
    assert not snap.partial


def test_skips_sockets_and_respects_caps(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(src / "sock"))
    (src / "big").write_bytes(b"x" * 2_000)
    (src / "small").write_bytes(b"y" * 10)
    snap = snapshot_tmp(dst, src, **LIMITS)
    s.close()
    assert not (dst / "sock").exists()
    assert not (dst / "big").exists()
    assert (dst / "small").exists()
    assert snap.partial


def test_exclude_creates_empty_mountpoint_without_copying(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "p" / "ws" / "src").mkdir(parents=True)
    (src / "p" / "ws" / "src" / "a.py").write_text("x")
    (src / "p" / "other.txt").write_text("o")
    dst.mkdir()
    snap = snapshot_tmp(dst, src, exclude=src / "p" / "ws", **LIMITS)
    assert (dst / "p" / "ws").is_dir()
    assert list((dst / "p" / "ws").iterdir()) == []
    assert (dst / "p" / "other.txt").exists()
    assert "p/ws/src/a.py" not in snap.base_fps


def test_restrictive_directory_mode_is_preserved(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "private").mkdir(parents=True)
    dst.mkdir()
    os.chmod(src / "private", 0o500)
    try:
        snapshot_tmp(dst, src, **LIMITS)
        assert stat.S_IMODE(os.lstat(dst / "private").st_mode) == 0o500
    finally:
        os.chmod(src / "private", 0o700)
