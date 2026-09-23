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
