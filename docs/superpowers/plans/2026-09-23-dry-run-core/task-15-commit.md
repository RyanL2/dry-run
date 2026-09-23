### Task 15: Confined ops and commit engine (F10, F12, S4, S5, N3)

**Files:**
- Create: `src/dryrun/confined.py`, `src/dryrun/commit.py`, `tests/unit/test_confined.py`, `tests/unit/test_commit.py`

**Interfaces:**
- Consumes: `Fp`, `fp_of`, `digest` (Task 3); `ChangeSet`, `ChangeOp` (Task 2); `extract` (Task 9, used in tests); `Store`, `TokenError` (Task 14); `Config` (Task 1)
- Produces:
  - `ConfinementError(OSError)`
  - `Root(path)` (context manager) with methods:

    | Method | Notes |
    |---|---|
    | `lstat(rel) -> os.stat_result \| None` | |
    | `readlink(rel) -> str` | |
    | `unlink(rel)` | |
    | `rmtree(rel)` | |
    | `mkdir(rel, mode=0o700)` | |
    | `rename_in(src_abs, rel, *, noreplace: bool)` | falls back to copy on EXDEV |
    | `symlink(rel, target)` | |
    | `chmod(rel, mode)` | |
    | `fsync_parent(rel)` | |
    | `fingerprint_subtree(rel) -> dict[str, Fp]` | |

    Every method resolves the path with O_NOFOLLOW dir-fd walks and rejects `..`, absolute paths and empty parts.
  - `CommitError(code: int, message: str)`; `EXIT_REFUSED = 3`, `EXIT_CONFLICT = 4`, `EXIT_FIDELITY = 5`
  - `ApplyReport(applied: int, created: list[str])`
  - `apply_changeset(cs: ChangeSet, *, journal_path: Path) -> ApplyReport`
  - `apply_run(store: Store, run_id: str, token: str, *, cfg: Config, out: BinaryIO, err: BinaryIO) -> int`: returns the shadow's exit code, or 3/4/5
  - `recover_all(store: Store) -> list[str]`: run ids it completed

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_confined.py`:
```python
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
```

`tests/unit/test_commit.py`:
```python
from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest

import dryrun.confined as confined
from dryrun.commit import CommitError, apply_changeset, apply_run, recover_all
from dryrun.config import load_config
from dryrun.effects.upper import extract
from dryrun.fingerprint import fingerprint_tree
from dryrun.store import Store
from dryrun.types import ChangeSet


def whiteout(p: Path) -> None:
    p.write_bytes(b"")
    os.setxattr(p, "user.overlay.whiteout", b"y")


def build(tmp_path: Path):
    ws, up = tmp_path / "ws", tmp_path / "up"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("a1\n")
    (ws / "old").mkdir()
    (ws / "old" / "x").write_text("x")
    (ws / "keep.txt").write_text("keep\n")
    base = fingerprint_tree(ws)
    (up / "src").mkdir(parents=True)
    (up / "src" / "a.py").write_text("a2\n")
    (up / "new" / "deep").mkdir(parents=True)
    (up / "new" / "deep" / "n.txt").write_text("n\n")
    os.chmod(up / "new" / "deep", 0o555)
    (up / "ln").symlink_to("keep.txt")
    whiteout(up / "old")
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id="r1", roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    return ws, up, cs


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        st = os.lstat(p)
        out[rel] = (st.st_mode, os.readlink(p) if p.is_symlink() else (p.read_bytes() if p.is_file() else None))
    return out


def test_apply_produces_reviewed_tree(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    mtime = os.stat(up / "src" / "a.py").st_mtime_ns
    report = apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert (ws / "src" / "a.py").read_text() == "a2\n"
    assert os.stat(ws / "src" / "a.py").st_mtime_ns == mtime
    assert (ws / "new" / "deep" / "n.txt").read_text() == "n\n"
    assert os.stat(ws / "new" / "deep").st_mode & 0o777 == 0o555
    assert os.readlink(ws / "ln") == "keep.txt"
    assert not (ws / "old").exists()
    assert set(report.created) == {"new", "new/deep", "new/deep/n.txt", "ln"}
    assert not (tmp_path / "j.json").exists()
    os.chmod(ws / "new" / "deep", 0o755)


def test_apply_conflict_writes_nothing(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    (ws / "src" / "a.py").write_text("user edit\n")
    before = snapshot(ws)
    with pytest.raises(CommitError) as exc:
        apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert exc.value.code == 4
    assert snapshot(ws) == before
    os.chmod(up / "new" / "deep", 0o755)


def test_symlink_swap_between_shadow_and_commit(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(ws / "src")
    (ws / "src").symlink_to(outside)
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert list(outside.iterdir()) == []
    os.chmod(up / "new" / "deep", 0o755)


def test_refused_changeset_is_not_applied(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    cs.refused.append({"path": "h", "reason": "hardlink_in_shadow"})
    with pytest.raises(CommitError) as exc:
        apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert exc.value.code == 3
    os.chmod(up / "new" / "deep", 0o755)


def test_recover_after_crash_mid_commit(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up, cs = build(tmp_path)
    cs.run_id = run.run_id
    run.changeset.write_text(json.dumps(cs.to_json()))
    store.save_meta(run, status="applying", session_id="s", exit_code=0)
    real = confined.Root.rename_in
    calls = {"n": 0}

    def flaky(self, src, rel, *, noreplace):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated crash")
        return real(self, src, rel, noreplace=noreplace)

    monkeypatch.setattr(confined.Root, "rename_in", flaky)
    with pytest.raises(OSError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    assert (store.journal_dir / f"{run.run_id}.json").exists()
    monkeypatch.setattr(confined.Root, "rename_in", real)
    assert recover_all(store) == [run.run_id]
    assert (ws / "src" / "a.py").read_text() == "a2\n" and (ws / "new" / "deep" / "n.txt").exists()
    assert not (store.journal_dir / f"{run.run_id}.json").exists()
    os.chmod(ws / "new" / "deep", 0o755)


def test_apply_run_replays_output_and_is_single_use(tmp_path: Path):
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up, cs = build(tmp_path)
    run.changeset.write_text(json.dumps(cs.to_json()))
    run.stdout.write_bytes(b"built ok\n")
    run.stderr.write_bytes(b"warning\n")
    token = store.authorize(run, session_id="s1", decision="allow")
    store.save_meta(run, exit_code=0)
    out, err = io.BytesIO(), io.BytesIO()
    cfg = load_config(use_user_file=False)
    assert apply_run(store, run.run_id, token, cfg=cfg, out=out, err=err) == 0
    assert out.getvalue() == b"built ok\n" and err.getvalue() == b"warning\n"
    assert "new/deep/n.txt" in store.ledger("s1", str(ws))
    assert apply_run(store, run.run_id, token, cfg=cfg, out=io.BytesIO(), err=io.BytesIO()) == 3
    os.chmod(ws / "new" / "deep", 0o755)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_confined.py tests/unit/test_commit.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

`src/dryrun/confined.py`:
```python
"""Path operations confined beneath a root directory (spec S4): every component is opened with
O_NOFOLLOW|O_DIRECTORY relative to its parent fd and the final step uses *at() syscalls, so a directory
swapped for a symlink between shadow and commit cannot redirect a write outside the root."""
from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat

from dryrun.fingerprint import Fp, fp_of

_libc = ctypes.CDLL(None, use_errno=True)
_libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
AT_FDCWD = -100
RENAME_NOREPLACE = 1
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PATH = getattr(os, "O_PATH", 0o10000000) | os.O_NOFOLLOW | os.O_CLOEXEC


class ConfinementError(OSError):
    pass


def _parts(rel: str) -> list[str]:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\0" in rel:
        raise ConfinementError(errno.EINVAL, f"bad path {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ConfinementError(errno.EINVAL, f"bad path {rel!r}")
    return parts


def _renameat2(src_dir: int, src: str, dst_dir: int, dst: str, flags: int) -> None:
    if _libc.renameat2(src_dir, os.fsencode(src), dst_dir, os.fsencode(dst), flags) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), dst)


def _chmod_fd_path(dir_fd: int, name: str, mode: int) -> None:
    pfd = os.open(name, _PATH, dir_fd=dir_fd)
    try:
        if stat.S_ISLNK(os.fstat(pfd).st_mode):
            return
        os.chmod(f"/proc/self/fd/{pfd}", mode)
    finally:
        os.close(pfd)


class Root:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = os.fspath(path)
        self.fd = os.open(self.path, _DIR)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "Root":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _parent(self, rel: str) -> tuple[int, str]:
        parts = _parts(rel)
        fd = os.dup(self.fd)
        try:
            for comp in parts[:-1]:
                try:
                    nfd = os.open(comp, _DIR, dir_fd=fd)
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ConfinementError(e.errno, f"{rel}: component {comp!r} is not a real directory")
                    raise
                os.close(fd)
                fd = nfd
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def lstat(self, rel: str) -> os.stat_result | None:
        try:
            fd, name = self._parent(rel)
        except FileNotFoundError:
            return None
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        finally:
            os.close(fd)

    def readlink(self, rel: str) -> str:
        fd, name = self._parent(rel)
        try:
            return os.readlink(name, dir_fd=fd)
        finally:
            os.close(fd)

    def unlink(self, rel: str) -> None:
        fd, name = self._parent(rel)
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)

    def rmtree(self, rel: str) -> None:
        fd, name = self._parent(rel)
        try:
            _rmtree_at(fd, name)
        finally:
            os.close(fd)

    def mkdir(self, rel: str, mode: int = 0o700) -> None:
        fd, name = self._parent(rel)
        try:
            os.mkdir(name, mode, dir_fd=fd)
        except FileExistsError:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                raise
        finally:
            os.close(fd)

    def rename_in(self, src_abs: str, rel: str, *, noreplace: bool) -> None:
        fd, name = self._parent(rel)
        flags = RENAME_NOREPLACE if noreplace else 0
        try:
            try:
                _renameat2(AT_FDCWD, src_abs, fd, name, flags)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    if e.errno == errno.EEXIST:
                        raise FileExistsError(e.errno, e.strerror, rel) from None
                    raise
                self._copy_then_rename(src_abs, fd, name, flags)
        finally:
            os.close(fd)

    @staticmethod
    def _copy_then_rename(src_abs: str, dir_fd: int, name: str, flags: int) -> None:
        tmp = f".{name}.dryrun-{secrets.token_hex(4)}"
        st = os.lstat(src_abs)
        with open(src_abs, "rb") as src:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                          dir_fd=dir_fd)
            try:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    os.write(out, chunk)
                os.fchmod(out, stat.S_IMODE(st.st_mode))
                os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
                os.fsync(out)
            finally:
                os.close(out)
        _renameat2(dir_fd, tmp, dir_fd, name, flags)
        os.unlink(src_abs)

    def symlink(self, rel: str, target: str) -> None:
        fd, name = self._parent(rel)
        try:
            os.symlink(target, name, dir_fd=fd)
        finally:
            os.close(fd)

    def chmod(self, rel: str, mode: int) -> None:
        fd, name = self._parent(rel)
        try:
            _chmod_fd_path(fd, name, mode)
        finally:
            os.close(fd)

    def fsync_parent(self, rel: str) -> None:
        try:
            fd, name = self._parent(rel)
        except (FileNotFoundError, ConfinementError):
            return
        try:
            os.fsync(fd)
            try:
                ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
            except OSError:
                return
            try:
                os.fsync(ffd)
            finally:
                os.close(ffd)
        finally:
            os.close(fd)

    def fingerprint_subtree(self, rel: str) -> dict[str, Fp]:
        fd, name = self._parent(rel)
        out: dict[str, Fp] = {}
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            out[rel] = fp_of(st)
            if stat.S_ISDIR(st.st_mode):
                _walk_at(fd, name, rel, out)
        finally:
            os.close(fd)
        return out


def _open_dir_at(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIR, dir_fd=parent_fd)
    except PermissionError:
        _chmod_fd_path(parent_fd, name, 0o700)
        return os.open(name, _DIR, dir_fd=parent_fd)


def _walk_at(parent_fd: int, name: str, rel: str, out: dict[str, Fp]) -> None:
    dfd = _open_dir_at(parent_fd, name)
    try:
        for entry in os.scandir(dfd):
            st = entry.stat(follow_symlinks=False)
            child = f"{rel}/{entry.name}"
            out[child] = fp_of(st)
            if stat.S_ISDIR(st.st_mode):
                _walk_at(dfd, entry.name, child, out)
    finally:
        os.close(dfd)


def _rmtree_at(parent_fd: int, name: str) -> None:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    dfd = _open_dir_at(parent_fd, name)
    try:
        for entry in list(os.scandir(dfd)):
            _rmtree_at(dfd, entry.name)
    finally:
        os.close(dfd)
    os.rmdir(name, dir_fd=parent_fd)
```

Note: `_walk_at` may chmod an unreadable directory (0o000 → 0o700) to fingerprint it. That changes the dir's ctime, which only affects directories we are about to delete (rmtree subtree checks), so it is acceptable.

`src/dryrun/commit.py`:
```python
"""Apply a reviewed ChangeSet to the real filesystem (F10, F12). Never runs code (S5): only
unlink, rmtree, mkdir, rename, symlink and chmod, all through confined.Root (S4)."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from dryrun.config import Config
from dryrun.confined import ConfinementError, Root
from dryrun.fingerprint import digest, fp_of
from dryrun.store import Store, TokenError
from dryrun.types import ChangeOp, ChangeSet

EXIT_REFUSED, EXIT_CONFLICT, EXIT_FIDELITY = 3, 4, 5
HASH_LIMIT = 64 * 1024**2


class CommitError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ApplyReport:
    applied: int
    created: list[str] = field(default_factory=list)


def _open_roots(cs: ChangeSet) -> dict[str, Root]:
    roots: dict[str, Root] = {}
    try:
        for area in {op.area for op in cs.ops}:
            roots[area] = Root(cs.roots[area])
    except BaseException:
        for r in roots.values():
            r.close()
        raise
    return roots


def _check_conflicts(roots: dict[str, Root], cs: ChangeSet) -> None:
    for op in cs.ops:
        root = roots[op.area]
        try:
            cur = root.lstat(op.target)
        except ConfinementError as e:
            raise CommitError(EXIT_CONFLICT, f"{op.area}:{op.target}: {e}") from None
        cur_fp = list(fp_of(cur)) if cur is not None else None
        if cur_fp != op.base_fp:
            raise CommitError(EXIT_CONFLICT, f"{op.area}:{op.target} changed since the shadow run")
        if op.op == "rmtree" and digest(root.fingerprint_subtree(op.target)) != op.subtree_digest:
            raise CommitError(EXIT_CONFLICT, f"{op.area}:{op.target} contents changed since the shadow run")


def _writable_parent(source_upper: str) -> None:
    """The shadowed command may have made its own directories read-only (e.g. chmod 555). The upper
    dir is private to Dry Run and every sandbox process is dead, so let us move files out of it."""
    parent = os.path.dirname(source_upper)
    st = os.lstat(parent)
    if stat.S_ISDIR(st.st_mode) and (st.st_mode & 0o300) != 0o300:
        os.chmod(parent, stat.S_IMODE(st.st_mode) | 0o700)


def _run_ops(roots: dict[str, Root], cs: ChangeSet) -> None:
    dir_modes: list[ChangeOp] = []
    for op in cs.ops:
        root = roots[op.area]
        if op.op == "unlink":
            root.unlink(op.target)
        elif op.op == "rmtree":
            root.rmtree(op.target)
        elif op.op == "mkdir":
            root.mkdir(op.target, 0o700)
            dir_modes.append(op)
        elif op.op == "rename_in":
            if op.source_upper and os.path.lexists(op.source_upper):
                _writable_parent(op.source_upper)
                root.rename_in(op.source_upper, op.target, noreplace=op.base_fp is None)
        elif op.op == "symlink":
            cur = root.lstat(op.target)
            if not (cur is not None and stat.S_ISLNK(cur.st_mode) and root.readlink(op.target) == op.link_target):
                root.symlink(op.target, op.link_target or "")
        elif op.op == "chmod":
            cur = root.lstat(op.target)
            if cur is not None and stat.S_ISDIR(cur.st_mode):
                dir_modes.append(op)
            else:
                root.chmod(op.target, op.mode)
        else:
            raise CommitError(EXIT_REFUSED, f"unknown op {op.op}")
    for op in sorted(dir_modes, key=lambda o: o.target.count("/"), reverse=True):
        if op.mode is not None:
            roots[op.area].chmod(op.target, op.mode)
    for op in cs.ops:
        roots[op.area].fsync_parent(op.target)


def _sha256_at(root: Root, rel: str) -> str | None:
    path = os.path.join(root.path, rel)
    try:
        h = hashlib.sha256()
        with open(path, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _verify(roots: dict[str, Root], cs: ChangeSet) -> list[str]:
    last: dict[tuple[str, str], ChangeOp] = {}
    for op in cs.ops:
        last[(op.area, op.target)] = op
    problems = []
    for (area, target), op in last.items():
        root = roots[area]
        st = root.lstat(target)
        where = f"{area}:{target}"
        if op.op in ("unlink", "rmtree"):
            if st is not None:
                problems.append(f"{where} still exists")
        elif st is None:
            problems.append(f"{where} missing")
        elif op.op == "mkdir":
            if not stat.S_ISDIR(st.st_mode) or (op.mode is not None and stat.S_IMODE(st.st_mode) != op.mode):
                problems.append(f"{where} is not the expected directory")
        elif op.op == "symlink":
            if not stat.S_ISLNK(st.st_mode) or root.readlink(target) != op.link_target:
                problems.append(f"{where} is not the expected symlink")
        elif op.op == "chmod":
            if stat.S_IMODE(st.st_mode) != op.mode:
                problems.append(f"{where} has mode {oct(stat.S_IMODE(st.st_mode))}")
        elif op.op == "rename_in":
            if not stat.S_ISREG(st.st_mode) or st.st_size != op.size:
                problems.append(f"{where} has wrong type or size")
            elif op.mode is not None and stat.S_IMODE(st.st_mode) != op.mode:
                problems.append(f"{where} has wrong mode")
            elif op.mtime_ns is not None and st.st_mtime_ns != op.mtime_ns:
                problems.append(f"{where} has wrong mtime")
            elif op.sha256 and (op.size or 0) <= HASH_LIMIT and _sha256_at(root, target) != op.sha256:
                problems.append(f"{where} content differs from the reviewed version")
    return problems


def _write_journal(path: Path, cs: ChangeSet) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"run_id": cs.run_id, "state": "applying", "started": time.time(), "changeset": cs.to_json()}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _created(cs: ChangeSet) -> list[str]:
    return [op.target for op in cs.ops
            if op.area == "workspace" and op.op in ("mkdir", "rename_in", "symlink") and op.base_fp is None]


def apply_changeset(cs: ChangeSet, *, journal_path: Path) -> ApplyReport:
    if not cs.committable:
        raise CommitError(EXIT_REFUSED, "changeset contains effects that cannot be committed faithfully")
    roots = _open_roots(cs)
    try:
        _check_conflicts(roots, cs)
        _write_journal(Path(journal_path), cs)
        try:
            _run_ops(roots, cs)
        except ConfinementError as e:
            raise CommitError(EXIT_CONFLICT, f"path changed during commit: {e}") from None
        problems = _verify(roots, cs)
        if problems:
            raise CommitError(EXIT_FIDELITY, "FIDELITY_ERROR: " + "; ".join(problems[:5]))
        os.unlink(journal_path)
        return ApplyReport(applied=len(cs.ops), created=_created(cs))
    finally:
        for r in roots.values():
            r.close()


def _replay(path: Path, stream: BinaryIO) -> None:
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                stream.write(chunk)
    except FileNotFoundError:
        pass
    stream.flush()


def apply_run(store: Store, run_id: str, token: str, *, cfg: Config, out: BinaryIO, err: BinaryIO) -> int:
    try:
        run = store.redeem(run_id, token, ttl_s=cfg.policy.pending_ttl_min * 60)
    except TokenError as e:
        err.write(f"dryrun: refused: {e}\n".encode())
        return EXIT_REFUSED
    meta = store.load_meta(run)
    cs = ChangeSet.from_json(json.loads(run.changeset.read_text()))
    try:
        report = apply_changeset(cs, journal_path=store.journal_dir / f"{run_id}.json")
    except CommitError as e:
        store.finish(run, "conflict" if e.code == EXIT_CONFLICT else "failed")
        err.write(f"dryrun: {e}\n".encode())
        if e.code == EXIT_CONFLICT:
            err.write(b"dryrun: nothing was written; re-run the command to shadow it again\n")
        return e.code
    if "workspace" in cs.roots:
        store.ledger_add(str(meta.get("session_id", "")), cs.roots["workspace"], report.created)
    _replay(run.stdout, out)
    _replay(run.stderr, err)
    store.finish(run, "committed")
    store.remove_run(run)
    code = meta.get("exit_code")
    return int(code) if isinstance(code, int) else 0


def recover_all(store: Store) -> list[str]:
    done = []
    for journal in sorted(store.journal_dir.glob("*.json")):
        try:
            data = json.loads(journal.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("state") != "applying":
            continue
        cs = ChangeSet.from_json(data["changeset"])
        roots = _open_roots(cs)
        try:
            _run_ops(roots, cs)
            problems = _verify(roots, cs)
        finally:
            for r in roots.values():
                r.close()
        run = store.run(cs.run_id)
        if problems:
            store.finish(run, "failed")
            continue
        journal.unlink()
        if run.root.exists():
            meta = store.load_meta(run)
            if "workspace" in cs.roots:
                store.ledger_add(str(meta.get("session_id", "")), cs.roots["workspace"], _created(cs))
            store.finish(run, "committed")
        done.append(cs.run_id)
    return done
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_confined.py tests/unit/test_commit.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/confined.py src/dryrun/commit.py tests/unit/test_confined.py tests/unit/test_commit.py
git commit -m "feat: confined journaled commit with conflict detection, verification and recovery"
```
