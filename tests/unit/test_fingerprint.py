from __future__ import annotations

import os
import time
from pathlib import Path

from dryrun import fingerprint
from dryrun.fingerprint import digest, fingerprint_tree, fp_of, submounts, subtree


def make_tree(root: Path) -> None:
    (root / "a").mkdir()
    (root / "a" / "f.txt").write_text("x")
    (root / "g.txt").write_text("y")
    (root / "link").symlink_to("/etc/passwd")


def test_fingerprint_tree_lists_all_paths_without_following_symlinks(tmp_path: Path):
    make_tree(tmp_path)
    fps = fingerprint_tree(tmp_path)
    assert set(fps) == {"a", "a/f.txt", "g.txt", "link"}
    assert fps["link"] == fp_of(os.lstat(tmp_path / "link"))


def test_digest_is_stable_and_sensitive(tmp_path: Path):
    make_tree(tmp_path)
    before = fingerprint_tree(tmp_path)
    assert digest(before) == digest(fingerprint_tree(tmp_path))
    time.sleep(0.01)
    (tmp_path / "g.txt").write_text("changed")
    after = fingerprint_tree(tmp_path)
    assert digest(before) != digest(after)


def test_subtree_selects_descendants_only(tmp_path: Path):
    make_tree(tmp_path)
    (tmp_path / "ab").write_text("not a child of a")
    fps = fingerprint_tree(tmp_path)
    assert set(subtree(fps, "a")) == {"a", "a/f.txt"}


def test_submounts_parses_mountinfo():
    info = (
        "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "40 22 0:5 / /home/u/ws/data rw - tmpfs tmpfs rw\n"
        "41 22 0:6 / /home/u/ws2 rw - tmpfs tmpfs rw\n"
        "42 22 0:7 / /home/u/ws/with\\040space rw - tmpfs tmpfs rw\n"
    )
    assert submounts(Path("/home/u/ws"), info) == ["/home/u/ws/data", "/home/u/ws/with space"]
    assert submounts(Path("/home/u/other"), info) == []


# C-0009: lower_changed and the commit conflict check both compare these fingerprints, so every way a
# concurrent writer can change a file must change its fingerprint.

def _rewrite_keeping_mtime(path: Path, data: bytes) -> None:
    st = os.lstat(path)
    with open(path, "r+b") as f:
        f.write(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_same_size_in_place_edit_with_restored_mtime_is_seen(tmp_path: Path):
    make_tree(tmp_path)
    before = fingerprint_tree(tmp_path)
    _rewrite_keeping_mtime(tmp_path / "a" / "f.txt", b"z")
    after = fingerprint_tree(tmp_path)
    assert after["a/f.txt"][2] == before["a/f.txt"][2]  # mtime forged back
    assert after != before


def test_rename_over_with_same_size_and_mtime_is_seen(tmp_path: Path):
    make_tree(tmp_path)
    before = fingerprint_tree(tmp_path)
    st = os.lstat(tmp_path / "g.txt")
    (tmp_path / "new").write_text("z")
    os.utime(tmp_path / "new", ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp_path / "new", tmp_path / "g.txt")
    assert fingerprint_tree(tmp_path) != before


def test_racy_entries_never_compare_equal_without_multigrain_ctime(tmp_path: Path, monkeypatch):
    # Coarse timestamps: a change in the same clock tick as our stat can leave (ino, size, mtime, ctime)
    # equal. Without multigrain ctime, entries changed that recently must count as changed.
    monkeypatch.setattr(fingerprint, "multigrain_ctime", lambda root: False)
    make_tree(tmp_path)
    first, second = fingerprint_tree(tmp_path), fingerprint_tree(tmp_path)
    assert first["g.txt"][3] < 0 and first != second
    monkeypatch.setattr(fingerprint, "RACY_NS", 1_000_000)
    time.sleep(0.01)
    assert fingerprint_tree(tmp_path) == fingerprint_tree(tmp_path)  # old enough: real ctimes again


def test_multigrain_ctime_needs_linux_6_13_and_a_local_fs():
    info = ("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "40 22 0:5 / /tmp rw - tmpfs tmpfs rw\n"
            "41 22 0:6 / /mnt/c rw - 9p drvfs rw\n")
    assert fingerprint.fstype(Path("/home/u/ws"), info) == "ext4"
    assert fingerprint.fstype(Path("/tmp"), info) == "tmpfs"
    assert fingerprint.fstype(Path("/tmpx"), info) == "ext4"
    assert fingerprint.fstype(Path("/mnt/c/Users"), info) == "9p"
    assert fingerprint.multigrain_ctime(Path("/home/u/ws"), "6.18.33.1-microsoft-standard-WSL2", info)
    assert fingerprint.multigrain_ctime(Path("/tmp"), "6.13.0-rc1", info)
    assert not fingerprint.multigrain_ctime(Path("/home/u/ws"), "6.12.9", info)
    assert not fingerprint.multigrain_ctime(Path("/home/u/ws"), "5.15.167.4-microsoft-standard-WSL2", info)
    assert not fingerprint.multigrain_ctime(Path("/mnt/c/Users"), "6.18.0", info)
