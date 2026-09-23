"""Copied lower layer for the shadow's /tmp.

The real /tmp cannot be an unprivileged overlay lower layer when something is mounted inside it
(WSLg's /tmp/.X11-unix -> "failed to clone lowerpath", spike 0). We copy (never hard-link: that would
change nlink/ctime of real files) the caller's own small files instead. Sockets are never copied, so
host services behind /tmp sockets are unreachable (I2).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dryrun.fingerprint import Fp, fp_of


@dataclass
class TmpSnapshot:
    base_fps: dict[str, Fp] = field(default_factory=dict)
    partial: bool = False
    entries: int = 0
    bytes: int = 0


def _copy_file(src: str, dst: str, st: os.stat_result) -> bool:
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        cur = os.fstat(fd)
        if not stat.S_ISREG(cur.st_mode) or cur.st_ino != st.st_ino:
            return False
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                os.write(out, chunk)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    return True


def snapshot_tmp(dst: Path, src: Path = Path("/tmp"), *, max_entries: int, max_total: int, max_file: int,
                 exclude: Path | None = None, uid: int | None = None) -> TmpSnapshot:
    uid = os.getuid() if uid is None else uid
    src, dst = Path(src), Path(dst)
    snap = TmpSnapshot()
    src_dev = os.lstat(src).st_dev
    excl_rel = None
    if exclude is not None:
        try:
            excl_rel = str(Path(exclude).relative_to(src))
        except ValueError:
            excl_rel = None
    if excl_rel:
        # Mount point for a workspace that lives under /tmp: ancestors + empty dir, nothing inside.
        (dst / excl_rel).mkdir(parents=True, exist_ok=True)
    dirs: list[tuple[str, os.stat_result]] = []
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            entries = sorted(os.scandir(src / rel if rel else src), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            child = f"{rel}/{entry.name}" if rel else entry.name
            if excl_rel and child == excl_rel:
                continue
            is_ancestor = bool(excl_rel) and excl_rel.startswith(child + "/")
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_dev != src_dev or (st.st_uid != uid and not is_ancestor):
                continue
            if snap.entries >= max_entries:
                snap.partial = True
                return _finish(dst, dirs, snap)
            target = str(dst / child)
            if stat.S_ISDIR(st.st_mode):
                os.makedirs(target, mode=0o700, exist_ok=True)
                if st.st_uid == uid:
                    snap.base_fps[child] = fp_of(st)
                    dirs.append((child, st))
                snap.entries += 1
                stack.append(child)
            elif stat.S_ISREG(st.st_mode):
                if st.st_size > max_file or snap.bytes + st.st_size > max_total:
                    snap.partial = True
                    continue
                if _copy_file(str(src / child), target, st):
                    snap.base_fps[child] = fp_of(st)
                    snap.entries += 1
                    snap.bytes += st.st_size
            elif stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(src / child), target)
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
                snap.base_fps[child] = fp_of(st)
                snap.entries += 1
            # sockets, fifos, devices: never copied
    return _finish(dst, dirs, snap)


def _finish(dst: Path, dirs: list[tuple[str, os.stat_result]], snap: TmpSnapshot) -> TmpSnapshot:
    for rel, st in sorted(dirs, key=lambda d: d[0].count("/"), reverse=True):
        path = str(dst / rel)
        os.chmod(path, stat.S_IMODE(st.st_mode) | 0o700)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return snap
