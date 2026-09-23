"""lstat fingerprints of directory trees (racy-git style, ctime included) and mount checks."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

Fp = tuple[int, int, int, int, int]


def fp_of(st: os.stat_result) -> Fp:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)


def lstat_fp(path: str | os.PathLike) -> Fp | None:
    try:
        return fp_of(os.lstat(path))
    except FileNotFoundError:
        return None


def fingerprint_tree(root: Path) -> dict[str, Fp]:
    """Every path under root mapped to its lstat fingerprint. Never follows symlinks and does not
    descend into mount points (the mount point itself is recorded)."""
    root = Path(root)
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
                out[child] = fp_of(st)
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


def changed_paths(before: dict[str, Fp], after: dict[str, Fp]) -> set[str]:
    keys = set(before) | set(after)
    return {k for k in keys if before.get(k) != after.get(k)}


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
