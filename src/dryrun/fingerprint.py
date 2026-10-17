"""lstat fingerprints of directory trees (ctime included, racy entries marked) and mount checks."""
from __future__ import annotations

import hashlib
import itertools
import os
import re
import stat
import time
from pathlib import Path

Fp = tuple[int, int, int, int, int]
# Filesystems that get multigrain timestamps in Linux 6.13+: once a stat has seen an inode's ctime, the
# next change to that inode gets a strictly later ctime. Probed on WSL2 6.18 ext4 and tmpfs: 0 of 20,000
# same-size rewrites right after a stat kept the fingerprint (research/cards/C-0009.md).
MULTIGRAIN_FS = frozenset({"ext4", "xfs", "btrfs", "tmpfs"})
# Elsewhere ctime comes from a clock-tick coarse clock (<= 10 ms at HZ=100), and a change in the same tick as
# our stat can leave the whole fingerprint equal (git's "racy" entries). Such recent entries are marked.
RACY_NS = 50_000_000
_racy = itertools.count(1)


def fp_of(st: os.stat_result) -> Fp:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)


def fstype(path: Path, mountinfo: str | None = None) -> str:
    """Filesystem type of the mount that holds path (longest mount point that contains it)."""
    if mountinfo is None:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as f:
            mountinfo = f.read()
    path, best, kind = str(path), "", ""
    for line in mountinfo.splitlines():
        left, _, right = line.partition(" - ")
        parts = left.split(" ")
        if len(parts) > 4 and right:
            point = _unescape_mount(parts[4])
            if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
                best, kind = point, right.split(" ")[0]
    return kind


def multigrain_ctime(root: Path, release: str | None = None, mountinfo: str | None = None) -> bool:
    m = re.match(r"(\d+)\.(\d+)", release if release is not None else os.uname().release)
    return bool(m) and (int(m[1]), int(m[2])) >= (6, 13) and fstype(root, mountinfo) in MULTIGRAIN_FS


def racy_cutoff(root: Path) -> int | None:
    """ctime_ns from which an entry under root is racy, or None when the filesystem makes that impossible."""
    return None if multigrain_ctime(root) else time.time_ns() - RACY_NS


def observe(st: os.stat_result, cutoff: int | None) -> Fp:
    """fp_of, except that a racy entry gets a unique negative ctime: it then never matches any other
    observation, so lower_changed and the commit conflict check treat it as changed (safe, may re-ask)."""
    if cutoff is not None and st.st_ctime_ns >= cutoff:
        return (st.st_ino, st.st_size, st.st_mtime_ns, -next(_racy), st.st_mode)
    return fp_of(st)


def fingerprint_tree(root: Path) -> dict[str, Fp]:
    """Every path under root mapped to its lstat fingerprint. Never follows symlinks and does not
    descend into mount points (the mount point itself is recorded). Racy entries never compare equal."""
    root = Path(root)
    cutoff = racy_cutoff(root)
    root_dev = os.lstat(root).st_dev
    out: dict[str, Fp] = {}
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            it = os.scandir(os.path.join(root, rel) if rel else root)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        with it:
            for entry in it:
                child = f"{rel}/{entry.name}" if rel else entry.name
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                out[child] = observe(st, cutoff)
                if stat.S_ISDIR(st.st_mode) and st.st_dev == root_dev:
                    stack.append(child)
    return out


def digest(fps: dict[str, Fp]) -> str:
    h = hashlib.sha256()
    for key in sorted(fps):
        h.update(key.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(repr(fps[key]).encode())
        h.update(b"\n")
    return h.hexdigest()


def subtree(fps: dict[str, Fp], rel: str) -> dict[str, Fp]:
    prefix = rel + "/"
    return {k: v for k, v in fps.items() if k == rel or k.startswith(prefix)}


def _unescape_mount(field: str) -> str:
    # /proc/self/mountinfo escapes space, tab, newline and backslash as \ooo octal.
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 4 <= len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def submounts(root: Path, mountinfo: str | None = None) -> list[str]:
    """Mount points strictly inside root. Unprivileged overlayfs cannot use such a tree as a lower
    layer (spike 0), so the pipeline refuses these workspaces with `ask`."""
    if mountinfo is None:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as f:
            mountinfo = f.read()
    prefix = str(root).rstrip("/") + "/"
    found = []
    for line in mountinfo.splitlines():
        parts = line.split(" ")
        if len(parts) > 4:
            point = _unescape_mount(parts[4])
            if point.startswith(prefix):
                found.append(point)
    return sorted(found)
