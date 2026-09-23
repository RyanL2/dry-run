"""Git facts for the EffectRecord: recoverability of paths and ref changes (harm policy H1, H3).

All git executions go through the read-only sandbox (S10): `git status` can otherwise run
core.fsmonitor or clean filters from repository config on the real system.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dryrun.sandbox.spawn import run_readonly

SEP = "\x1e--dryrun-section--\x1e"
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
_HEX = set("0123456789abcdef")


@dataclass
class GitSnapshot:
    is_repo: bool
    status: dict[str, str] = field(default_factory=dict)
    ignored_dirs: tuple[str, ...] = ()
    tracked: frozenset[str] = frozenset()
    head_blobs: dict[str, str] = field(default_factory=dict)
    index_blobs: dict[str, str] = field(default_factory=dict)


def find_workspace_root(cwd: Path) -> Path:
    cwd = Path(cwd)
    for d in [cwd, *cwd.parents]:
        if os.path.lexists(d / ".git"):
            return d
    return cwd


def snapshot(ws_root: Path) -> GitSnapshot:
    if not os.path.lexists(Path(ws_root) / ".git"):
        return GitSnapshot(is_repo=False)
    script = (
        f"git {' '.join(_SAFE)} status --porcelain=v1 -z --ignored=matching --untracked-files=all; "
        f"printf '{SEP}'; git ls-files -z -s; printf '{SEP}'; git ls-tree -r -z HEAD 2>/dev/null; true"
    )
    res = run_readonly(["sh", "-c", script], cwd=Path(ws_root))
    parts = res.stdout.decode("utf-8", "surrogateescape").split(SEP)
    if len(parts) != 3:
        return GitSnapshot(is_repo=False)
    status: dict[str, str] = {}
    ignored_dirs: list[str] = []
    records = parts[0].split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:
            continue
        code, path = rec[:2], rec[3:]
        if code[0] in "RC":
            i += 1  # the rename source follows as its own record
        if code == "!!":
            if path.endswith("/"):
                ignored_dirs.append(path.rstrip("/"))
            else:
                status[path] = "ignored"
        elif code == "??":
            status[path] = "untracked"
        else:
            status[path] = "tracked_dirty"
    index: dict[str, str] = {}
    for rec in parts[1].split("\0"):
        if "\t" in rec:
            meta, path = rec.split("\t", 1)
            index[path] = meta.split()[1]
    head: dict[str, str] = {}
    for rec in parts[2].split("\0"):
        if "\t" in rec:
            meta, path = rec.split("\t", 1)
            fields_ = meta.split()
            if len(fields_) == 3 and fields_[1] == "blob":
                head[path] = fields_[2]
    return GitSnapshot(is_repo=True, status=status, ignored_dirs=tuple(ignored_dirs),
                       tracked=frozenset(index), head_blobs=head, index_blobs=index)


def recoverability(snap: GitSnapshot, rel: str) -> str | None:
    if not snap.is_repo:
        return None
    if rel in snap.status:
        return snap.status[rel]
    if any(rel == d or rel.startswith(d + "/") for d in snap.ignored_dirs):
        return "ignored"
    if rel in snap.tracked:
        return "tracked_clean"
    if any(t.startswith(rel + "/") for t in snap.tracked):
        return "tracked_clean"  # a directory containing tracked files
    return "untracked"


def blob_sha1(path: Path) -> str:
    data = Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _is_whiteout(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
        return True
    if stat.S_ISREG(st.st_mode) and st.st_size == 0:
        try:
            os.getxattr(path, "user.overlay.whiteout", follow_symlinks=False)
            return True
        except OSError:
            return False
    return False


REF_CAP = 4096
PACKED_REFS_CAP = 64 * 1024**2


def _safe_read(path: Path, cap: int) -> str | None:
    """Read a regular file without following symlinks, blocking on FIFOs, or reading without bound.
    Upper-dir content is produced by the shadowed command, and this runs in the daemon (outside every
    cgroup), so a planted FIFO or a symlink to /dev/zero must not hang or exhaust it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            return None
        return os.read(fd, cap).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _loose(git_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    base = git_dir / "refs"
    # The upper dir is shadow-controlled: a symlinked .git or refs must not make the daemon walk the host.
    if not (_real_dir(git_dir) and _real_dir(base)):
        return out
    for dirpath, _, files in os.walk(base):  # os.walk never descends into symlinked subdirectories
        for name in files:
            p = Path(dirpath) / name
            out[p.relative_to(git_dir).as_posix()] = p
    return out


def _packed(path: Path) -> dict[str, str]:
    refs: dict[str, str] = {}
    text = _safe_read(path, PACKED_REFS_CAP)
    if text is None:
        return refs
    for line in text.splitlines():
        if line and line[0] not in "#^" and " " in line:
            oid, name = line.split(" ", 1)
            refs[name.strip()] = oid
    return refs


def read_refs(git_dir: Path, upper_git_dir: Path | None = None) -> dict[str, str]:
    """Refs as the merged overlay view would show them, parsed from files (no git executed)."""
    git_dir = Path(git_dir)
    if not git_dir.is_dir():
        return {}
    up = Path(upper_git_dir) if upper_git_dir is not None and _real_dir(Path(upper_git_dir)) else None
    packed_file = git_dir / "packed-refs"
    if up is not None and os.path.lexists(up / "packed-refs"):
        packed_file = up / "packed-refs"
    refs = {} if (up is not None and _is_whiteout(up / "packed-refs")) else _packed(packed_file)
    loose = _loose(git_dir)
    if up is not None:
        for name, p in _loose(up).items():
            loose[name] = p
    for name, p in loose.items():
        if _is_whiteout(p):
            refs.pop(name, None)
            continue
        value = (_safe_read(p, REF_CAP) or "").strip()
        if len(value) in (40, 64):
            refs[name] = value
    head_file = up / "HEAD" if up is not None and os.path.lexists(up / "HEAD") else git_dir / "HEAD"
    head = (_safe_read(head_file, REF_CAP) or "").strip()
    if head.startswith("ref: "):
        target = head[5:].strip()
        if target in refs:
            refs["HEAD"] = refs[target]
    elif len(head) in (40, 64):
        refs["HEAD"] = head
    return refs


def objects_exist(ws_root: Path, shas: set[str]) -> set[str]:
    wanted = sorted(s for s in shas if len(s) in (40, 64) and set(s) <= _HEX)
    if not wanted:
        return set()
    script = "printf '%s\\n' " + " ".join(wanted) + " | git cat-file --batch-check"
    res = run_readonly(["sh", "-c", script], cwd=Path(ws_root))
    found = set()
    for line in res.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "missing":
            found.add(parts[0])
    return found


ALT_OBJECTS_MOUNT = "/run/dryrun-shadow-objects"  # inside the /run tmpfs of the read-only sandbox


def is_ancestor(ws_root: Path, old: str, new: str, extra_objects: Path | None) -> bool | None:
    # The shadow's new objects live in the run's upper dir, inside Dry Run's (hidden) state dir: bind them
    # read-only at a neutral path so commits made in the shadow can be checked for fast-forward.
    env, binds = {}, ()
    if extra_objects:
        env = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": ALT_OBJECTS_MOUNT}
        binds = ((str(extra_objects), ALT_OBJECTS_MOUNT),)
    res = run_readonly(["git", *_SAFE, "merge-base", "--is-ancestor", old, new], cwd=Path(ws_root), env=env,
                       extra_ro_binds=binds)
    if res.returncode == 0:
        return True
    if res.returncode == 1:
        return False
    return None
