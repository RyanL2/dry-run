from __future__ import annotations

import os
import time
from pathlib import Path

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
