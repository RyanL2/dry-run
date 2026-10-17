"""Copied lower layer for the shadow's /tmp.

The real /tmp cannot be an unprivileged overlay lower layer when something is mounted inside it
(WSLg's /tmp/.X11-unix -> "failed to clone lowerpath", spike 0). We copy (never hard-link: that would
change nlink/ctime of real files) the caller's own small files instead. Sockets are never copied, so
host services behind /tmp sockets are unreachable (I2).

Copying is slow when /tmp is busy (on WSL2 ext4 each mkdir costs about 0.35 ms), so the pipeline lends each
run the lower of a finished run to refresh (C-0011). A lent copy is kept only if it was verified when made
(its source kept the same fingerprint from our stat until the read ended) and the source still has that
fingerprint now; everything else is recopied, and entries no longer selected are removed, so the refreshed
lower has the same contents as a fresh copy.
"""
from __future__ import annotations

import os
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dryrun.fingerprint import Fp, fp_of, observe, racy_cutoff
from dryrun.store import safe_rmtree


@dataclass
class TmpSnapshot:
    base_fps: dict[str, Fp] = field(default_factory=dict)
    partial: bool = False
    entries: int = 0
    bytes: int = 0
    reusable: dict[str, Fp] = field(default_factory=dict)  # verified copies: may be kept while the fp holds


class TmpLowerCache:
    """Holds the /tmp lower of one finished run (its sandbox has exited) until the next run takes it.
    Each lower goes to exactly one run; a run that finds the slot empty copies /tmp from scratch."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._slot: tuple[Path, dict[str, Fp]] | None = None
        if self.root.is_dir():  # left by an earlier daemon, whose fingerprints are gone
            for entry in self.root.iterdir():
                _remove(entry)

    def take(self, dst: Path) -> dict[str, Fp] | None:
        """Move the held lower onto dst (an empty directory); return its verified fingerprints."""
        with self._lock:
            slot, self._slot = self._slot, None
            if slot is None:
                return None
            try:
                os.rename(slot[0], dst)
            except OSError:
                _remove(slot[0])
                return None
            return slot[1]

    def give(self, lower: Path, reusable: dict[str, Fp]) -> None:
        with self._lock:
            if self._slot is not None:
                return
            held = self.root / uuid.uuid4().hex
            try:
                self.root.mkdir(mode=0o700, exist_ok=True)
                os.rename(lower, held)
            except OSError:
                return
            self._slot = (held, reusable)


def _remove(path: str | Path) -> None:
    """Delete whatever is at path (a lent lower's stale entry); never follows symlinks."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        safe_rmtree(Path(path), Path(path).parent)
    else:
        os.unlink(path)


def _copy_file(src: str, dst: str, st: os.stat_result) -> tuple[bool, bool]:
    """(copied, verified): verified means the source still had fingerprint st after the whole read."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False, False
    try:
        cur = os.fstat(fd)
        if not stat.S_ISREG(cur.st_mode) or cur.st_ino != st.st_ino:
            return False, False
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                os.write(out, chunk)
        finally:
            os.close(out)
        verified = fp_of(os.fstat(fd)) == fp_of(st)
    finally:
        os.close(fd)
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    return True, verified


def snapshot_tmp(dst: Path, src: Path = Path("/tmp"), *, max_entries: int, max_total: int, max_file: int,
                 exclude: Path | None = None, uid: int | None = None,
                 reuse: dict[str, Fp] | None = None) -> TmpSnapshot:
    """Copy src into dst. With reuse, dst already holds a lent lower whose verified copies reuse maps to
    their source fingerprints: those still matching are kept, the rest of dst is made to match src."""
    uid = os.getuid() if uid is None else uid
    src, dst = Path(src), Path(dst)
    snap = TmpSnapshot()
    src_dev = os.lstat(src).st_dev
    cutoff = racy_cutoff(src)
    excl_rel = None
    if exclude is not None:
        try:
            excl_rel = str(Path(exclude).relative_to(src))
        except ValueError:
            excl_rel = None
    seen: set[str] = set()  # every path the result holds
    if excl_rel:
        # Mount point for a workspace that lives under /tmp: ancestors + empty dir, nothing inside.
        parts = excl_rel.split("/")
        for i in range(1, len(parts) + 1):
            seen.add("/".join(parts[:i]))
            if reuse is not None:
                _open_dir(str(dst / "/".join(parts[:i])))
        (dst / excl_rel).mkdir(parents=True, exist_ok=True)
    dirs: list[tuple[str, os.stat_result]] = []

    def done() -> TmpSnapshot:
        if reuse is not None:
            _prune(dst, seen)
        return _finish(dst, dirs, snap)

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
                return done()
            target = os.path.join(dst, child)
            fp = observe(st, cutoff)
            if stat.S_ISDIR(st.st_mode):
                if reuse is not None:
                    _open_dir(target)
                os.makedirs(target, mode=0o700, exist_ok=True)
                if st.st_uid == uid:
                    snap.base_fps[child] = fp
                    dirs.append((child, st))
                snap.entries += 1
                seen.add(child)
                stack.append(child)
                continue
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                continue  # sockets, fifos, devices: never copied
            if stat.S_ISREG(st.st_mode) and (st.st_size > max_file or snap.bytes + st.st_size > max_total):
                snap.partial = True
                continue
            if reuse is not None and reuse.get(child) == fp:
                verified = True  # a verified copy of this exact inode state (racy fps never compare equal)
            else:
                if reuse is not None:
                    _remove(target)
                if stat.S_ISREG(st.st_mode):
                    copied, verified = _copy_file(entry.path, target, st)
                    if not copied:
                        continue
                else:
                    os.symlink(os.readlink(entry.path), target)
                    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
                    try:
                        verified = fp_of(os.lstat(entry.path)) == fp_of(st)
                    except OSError:
                        verified = False
            snap.base_fps[child] = fp
            if verified:
                snap.reusable[child] = fp
            snap.entries += 1
            snap.bytes += st.st_size if stat.S_ISREG(st.st_mode) else 0
            seen.add(child)
    return done()


def _open_dir(path: str) -> None:
    """A lent lower keeps its directories' final modes: give the owner rwx back so entries can be added and
    removed (_finish sets the modes again). A non-directory at path is removed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        if st.st_mode & 0o700 != 0o700:
            os.chmod(path, stat.S_IMODE(st.st_mode) | 0o700)
    else:
        os.unlink(path)


def _prune(dst: Path, seen: set[str]) -> None:
    """Remove what a lent lower holds beyond this snapshot."""
    stack = [""]
    while stack:
        rel = stack.pop()
        with os.scandir(dst / rel if rel else dst) as it:
            entries = list(it)
        for entry in entries:
            child = f"{rel}/{entry.name}" if rel else entry.name
            if child not in seen:
                _remove(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                stack.append(child)


def _finish(dst: Path, dirs: list[tuple[str, os.stat_result]], snap: TmpSnapshot) -> TmpSnapshot:
    for rel, st in sorted(dirs, key=lambda d: d[0].count("/"), reverse=True):
        path = str(dst / rel)
        os.chmod(path, stat.S_IMODE(st.st_mode))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return snap
