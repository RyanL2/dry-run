from __future__ import annotations

import os
import shutil
import socket
import stat
from pathlib import Path

from dryrun.sandbox import tmpsnap
from dryrun.sandbox.tmpsnap import TmpLowerCache, snapshot_tmp

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


def test_racy_base_fingerprints_without_multigrain_ctime(tmp_path: Path, monkeypatch):
    # The commit checks tmp targets against these fingerprints; see test_fingerprint racy test.
    from dryrun import fingerprint
    monkeypatch.setattr(fingerprint, "multigrain_ctime", lambda root: False)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "f").write_text("x")
    snap = snapshot_tmp(dst, src, **LIMITS)
    assert snap.base_fps["f"][3] < 0


# C-0011: a lent lower is refreshed, never trusted. The refreshed tree must equal a fresh copy.

def _tree(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = os.lstat(p)
            rel = p.relative_to(root).as_posix()
            body = os.readlink(p) if stat.S_ISLNK(st.st_mode) else (p.read_bytes() if p.is_file() else None)
            out[rel] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_mtime_ns, body)
    return out


def _rewrite_keeping_mtime(path: Path, data: bytes) -> None:
    st = os.lstat(path)
    with open(path, "r+b") as f:
        f.write(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def _count_copies(monkeypatch) -> list[str]:
    copied, real = [], tmpsnap._copy_file

    def spy(src, dst, st):
        copied.append(Path(src).name)
        return real(src, dst, st)

    monkeypatch.setattr(tmpsnap, "_copy_file", spy)
    return copied


def test_refresh_keeps_unchanged_copies_and_matches_a_fresh_copy(tmp_path: Path, monkeypatch):
    src, lent, fresh = tmp_path / "src", tmp_path / "lent", tmp_path / "fresh"
    (src / "d" / "gone").mkdir(parents=True)
    (src / "d" / "gone" / "x").write_text("x")
    (src / "same.txt").write_text("same")
    (src / "edited.txt").write_text("aaaa")
    (src / "becomes_dir").write_text("f")
    (src / "ln").symlink_to("same.txt")
    os.chmod(src / "d", 0o500)  # the lent lower keeps this mode: refreshing must still edit inside
    lent.mkdir()
    first = snapshot_tmp(lent, src, **LIMITS)
    assert set(first.reusable) == {"same.txt", "edited.txt", "becomes_dir", "ln", "d/gone/x"}
    os.chmod(src / "d", 0o700)
    shutil.rmtree(src / "d" / "gone")
    (src / "d" / "new").write_text("new")
    os.chmod(src / "d", 0o500)
    _rewrite_keeping_mtime(src / "edited.txt", b"bbbb")  # same size and mtime: only ctime moves
    (src / "becomes_dir").unlink()
    (src / "becomes_dir").mkdir()
    try:
        copied = _count_copies(monkeypatch)
        snap = snapshot_tmp(lent, src, reuse=first.reusable, **LIMITS)
        assert sorted(copied) == ["edited.txt", "new"]  # same.txt and the symlink were kept
        fresh.mkdir()
        want = snapshot_tmp(fresh, src, **LIMITS)
        assert _tree(lent) == _tree(fresh)
        assert snap.base_fps == want.base_fps and snap.entries == want.entries and snap.bytes == want.bytes
        assert (lent / "edited.txt").read_bytes() == b"bbbb"
    finally:
        for d in (src, lent, fresh):
            if (d / "d").exists():
                os.chmod(d / "d", 0o700)


def test_racy_copies_are_never_reused(tmp_path: Path, monkeypatch):
    from dryrun import fingerprint
    monkeypatch.setattr(fingerprint, "multigrain_ctime", lambda root: False)
    src, lent = tmp_path / "src", tmp_path / "lent"
    src.mkdir()
    lent.mkdir()
    (src / "f").write_text("x")
    first = snapshot_tmp(lent, src, **LIMITS)
    copied = _count_copies(monkeypatch)
    snapshot_tmp(lent, src, reuse=first.reusable, **LIMITS)
    assert copied == ["f"]


def test_copy_of_a_file_changing_during_the_read_is_not_reusable(tmp_path: Path, monkeypatch):
    src, lent = tmp_path / "src", tmp_path / "lent"
    src.mkdir()
    lent.mkdir()
    (src / "f").write_text("aaaa")
    real_read = os.read

    def read_while_writer_runs(fd, n):
        data = real_read(fd, n)
        if data:
            _rewrite_keeping_mtime(src / "f", b"bbbb")
        return data

    monkeypatch.setattr(os, "read", read_while_writer_runs)
    snap = snapshot_tmp(lent, src, **LIMITS)
    monkeypatch.undo()
    assert "f" in snap.base_fps and "f" not in snap.reusable


def test_tmp_lower_cache_lends_each_lower_once(tmp_path: Path):
    leftover = tmp_path / "cache" / "old"
    leftover.mkdir(parents=True)
    cache = TmpLowerCache(tmp_path / "cache")
    assert not leftover.exists()
    a, b, c = (tmp_path / n for n in ("a", "b", "c"))
    for d in (a, b, c):
        d.mkdir()
    (a / "f").write_text("a")
    assert cache.take(b) is None
    cache.give(a, {"f": (1, 2, 3, 4, 5)})
    (c / "g").write_text("c")
    cache.give(c, {})  # slot taken: c stays where it is (its run deletes it)
    assert (c / "g").exists()
    assert cache.take(b) == {"f": (1, 2, 3, 4, 5)} and (b / "f").read_text() == "a" and not a.exists()
    assert cache.take(c) is None
