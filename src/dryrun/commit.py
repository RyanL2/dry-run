"""Apply a reviewed ChangeSet to the real filesystem (F10, F12). Never runs code (S5): only
unlink, rmtree, mkdir, rename, symlink and chmod, all through confined.Root (S4).

Crash safety: a journal holds the ChangeSet and a `.done` marker file records each completed op.
Recovery never replays an op blindly: an unfinished op is applied only if its target is still in the
exact pre-shadow state (or the state the already-executed ops leave). Every remaining op is validated before
any is run; if one fails, the path changed after the crash, so recovery refuses, changes nothing,
and retires the journal instead of replaying it forever."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

from dryrun.config import Config
from dryrun.confined import ConfinementError, Root
from dryrun.fingerprint import digest, fp_of
from dryrun.store import Store, TokenError
from dryrun.types import ChangeOp, ChangeSet

log = logging.getLogger("dryrun")
EXIT_REFUSED, EXIT_CONFLICT, EXIT_FIDELITY = 3, 4, 5
HASH_LIMIT = 64 * 1024**2


class CommitError(Exception):
    def __init__(self, code: int, message: str, *, partial: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.partial = partial


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


def _close(roots: dict[str, Root]) -> None:
    for r in roots.values():
        r.close()


def _at_base(root: Root, op: ChangeOp) -> bool:
    """True if the target is exactly as it was when the shadow started."""
    try:
        cur = root.lstat(op.target)
    except ConfinementError:
        return False
    cur_fp = list(fp_of(cur)) if cur is not None else None
    if cur_fp != op.base_fp:
        return False
    if op.op == "rmtree":
        return digest(root.fingerprint_subtree(op.target)) == op.subtree_digest
    return True


def _check_conflicts(roots: dict[str, Root], cs: ChangeSet) -> None:
    for op in cs.ops:
        if not _at_base(roots[op.area], op):
            raise CommitError(EXIT_CONFLICT, f"{op.area}:{op.target} changed since the shadow run")
    _check_writable_parents(roots, cs.ops, cs.ops)


def _check_writable_parents(roots: dict[str, Root], ops: list[ChangeOp], all_ops: list[ChangeOp]) -> None:
    """Refuse before writing anything if an op must add or remove an entry in a real directory we cannot
    write to: the commit never loosens a real directory's mode, so it would fail part-way."""
    created = {(o.area, o.target) for o in all_ops if o.op == "mkdir"}
    for op in ops:
        parent = op.target.rpartition("/")[0]
        if op.op == "chmod" or (op.area, parent) in created:
            continue
        path = os.path.join(roots[op.area].path, parent)
        if os.path.isdir(path) and not os.access(path, os.W_OK | os.X_OK):
            raise CommitError(EXIT_REFUSED, f"{op.area}:{parent or '.'} is not writable, so "
                              f"{op.target} cannot be committed; nothing was written")


def _writable_parent(source_upper: str) -> None:
    """The shadowed command may have made its own directories read-only (e.g. chmod 555). The upper
    dir is private to Dry Run and every sandbox process is dead, so let us move files out of it."""
    parent = os.path.dirname(source_upper)
    st = os.lstat(parent)
    if stat.S_ISDIR(st.st_mode) and (st.st_mode & 0o300) != 0o300:
        os.chmod(parent, stat.S_IMODE(st.st_mode) | 0o700)


def _apply_one(root: Root, op: ChangeOp, dir_modes: list[ChangeOp]) -> None:
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


def _finish_dirs(roots: dict[str, Root], cs: ChangeSet, dir_modes: list[ChangeOp]) -> list[str]:
    """Flush, then apply directory modes deepest first and check each one right away: once a directory loses
    u+x (chmod 600 d), nothing below it can be examined any more. Returns the mismatches."""
    for op in cs.ops:
        roots[op.area].fsync_parent(op.target)
    problems = []
    for op in sorted(dir_modes, key=lambda o: o.target.count("/"), reverse=True):
        if op.mode is None:
            continue
        root = roots[op.area]
        root.chmod(op.target, op.mode)
        st = root.lstat(op.target)
        if st is None or not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != op.mode:
            problems.append(f"{op.area}:{op.target} is not a directory with mode {oct(op.mode)}")
        root.fsync_parent(op.target)
    return problems


def _run_ops(roots: dict[str, Root], cs: ChangeSet, done: Callable[[int], None] | None = None) -> list[ChangeOp]:
    """Apply every op; directory modes are returned for _finish_dirs, after verification."""
    dir_modes: list[ChangeOp] = []
    for op in cs.ops:
        _apply_one(roots[op.area], op, dir_modes)
        if done is not None:
            done(op.seq)
    return dir_modes


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


def _satisfied(root: Root, op: ChangeOp) -> str | None:
    """None if the target shows this op's reviewed post-state, else a description of the difference.
    Directory modes are not checked here: they are applied (and checked) last, by _finish_dirs."""
    try:
        st = root.lstat(op.target)
    except ConfinementError as e:
        return str(e)
    where = f"{op.area}:{op.target}"
    if op.op in ("unlink", "rmtree"):
        return f"{where} still exists" if st is not None else None
    if st is None:
        return f"{where} missing"
    if op.op == "mkdir":
        if not stat.S_ISDIR(st.st_mode):
            return f"{where} is not a directory"
    elif op.op == "symlink":
        if not stat.S_ISLNK(st.st_mode) or root.readlink(op.target) != op.link_target:
            return f"{where} is not the expected symlink"
    elif op.op == "chmod":
        if not stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) != op.mode:
            return f"{where} has mode {oct(stat.S_IMODE(st.st_mode))}"
    elif op.op == "rename_in":
        if not stat.S_ISREG(st.st_mode) or st.st_size != op.size:
            return f"{where} has wrong type or size"
        if op.mode is not None and stat.S_IMODE(st.st_mode) != op.mode:
            return f"{where} has wrong mode"
        if op.mtime_ns is not None and st.st_mtime_ns != op.mtime_ns:
            return f"{where} has wrong mtime"
        if op.sha256 and (op.size or 0) <= HASH_LIMIT and _sha256_at(root, op.target) != op.sha256:
            return f"{where} content differs from the reviewed version"
    return None


def _verify(roots: dict[str, Root], cs: ChangeSet) -> list[str]:
    last: dict[tuple[str, str], ChangeOp] = {}
    for op in cs.ops:
        last[(op.area, op.target)] = op
    problems = []
    for (area, _target), op in last.items():
        diff = _satisfied(roots[area], op)
        if diff:
            problems.append(diff)
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


def _marker(journal_path: Path) -> Path:
    return journal_path.with_suffix(".done")


def _marker_writer(journal_path: Path) -> Callable[[int], None]:
    path = _marker(journal_path)

    def done(seq: int) -> None:
        with open(path, "a") as f:
            f.write(f"{seq}\n")

    return done


def _retire(journal_path: Path) -> None:
    """Keep a failed journal for inspection but never replay it again."""
    for p in (journal_path, _marker(journal_path)):
        if p.exists():
            os.replace(p, p.with_name(p.name + ".failed"))


def _drop(journal_path: Path) -> None:
    for p in (journal_path, _marker(journal_path)):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def _created(cs: ChangeSet) -> list[str]:
    return [op.target for op in cs.ops
            if op.area == "workspace" and op.op in ("mkdir", "rename_in", "symlink") and op.base_fp is None]


def apply_changeset(cs: ChangeSet, *, journal_path: Path) -> ApplyReport:
    if not cs.committable:
        raise CommitError(EXIT_REFUSED, "changeset contains effects that cannot be committed faithfully")
    journal_path = Path(journal_path)
    roots = _open_roots(cs)
    try:
        _check_conflicts(roots, cs)
        _write_journal(journal_path, cs)
        try:
            dir_modes = _run_ops(roots, cs, _marker_writer(journal_path))
            problems = _verify(roots, cs) or _finish_dirs(roots, cs, dir_modes)
        except CommitError:
            raise
        except Exception as exc:  # journal kept: recovery finishes the remaining ops or refuses safely
            raise CommitError(EXIT_FIDELITY, f"partially applied ({type(exc).__name__}: {exc}); the journal was "
                              "kept and `dryrun recover` will finish it or refuse safely", partial=True) from exc
        if problems:
            _retire(journal_path)
            raise CommitError(EXIT_FIDELITY, "FIDELITY_ERROR: " + "; ".join(problems[:5]), partial=True)
        _drop(journal_path)
        return ApplyReport(applied=len(cs.ops), created=_created(cs))
    finally:
        _close(roots)


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
    try:
        cs = ChangeSet.from_json(json.loads(run.changeset.read_text()))
        report = apply_changeset(cs, journal_path=store.journal_dir / f"{run_id}.json")
    except CommitError as e:
        store.finish(run, "conflict" if e.code == EXIT_CONFLICT and not e.partial else "failed")
        err.write(f"dryrun: {e}\n".encode())
        if e.code == EXIT_CONFLICT and not e.partial:
            err.write(b"dryrun: nothing was written; re-run the command to shadow it again\n")
        return e.code
    except Exception as e:
        store.finish(run, "failed")
        err.write(f"dryrun: commit failed ({type(e).__name__}: {e}); state may be partial, run `dryrun recover`\n"
                  .encode())
        return EXIT_FIDELITY
    if "workspace" in cs.roots:
        store.ledger_add(str(meta.get("session_id", "")), cs.roots["workspace"], report.created)
    _replay(run.stdout, out)
    _replay(run.stderr, err)
    store.finish(run, "committed")
    store.remove_run(run)
    code = meta.get("exit_code")
    return int(code) if isinstance(code, int) else 0


def _left_by(root: Root, op: ChangeOp) -> bool:
    """True if the target is in the state `op` leaves behind (directory modes are only applied at the end)."""
    try:
        st = root.lstat(op.target)
    except ConfinementError:
        return False
    if op.op == "chmod" and st is not None and stat.S_ISDIR(st.st_mode):
        return False  # deferred to _finish_dirs, so it never completes on its own
    if op.op != "mkdir":
        return _satisfied(root, op) is None
    return st is not None and stat.S_ISDIR(st.st_mode)


def _empty_private_dir(root: Root, rel: str) -> bool:
    try:
        st = root.lstat(rel)
        return (st is not None and stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700
                and not os.listdir(os.path.join(root.path, rel)))
    except (ConfinementError, OSError):
        return False


def _in_expected_state(root: Root, op: ChangeOp, executed: list[ChangeOp]) -> bool:
    """Is op's target as the already-executed ops left it? Base fingerprints describe the tree before the
    commit, so after an executed op on the same target the expected state is what that op left, and after an
    executed delete of an ancestor (rm -rf D before D/x is recreated) the target must be absent."""
    last = None
    for e in executed:
        if e.area == op.area and (e.target == op.target
                                  or (e.op in ("rmtree", "unlink") and op.target.startswith(e.target + "/"))):
            last = e
    if last is None:
        if op.op == "chmod" and any(e.area == op.area and e.target.startswith(op.target + "/") for e in executed):
            # Executed ops inside this directory changed its size, mtime and ctime; its identity and mode must
            # still be the base ones (directory modes are only applied at the end).
            try:
                cur = root.lstat(op.target)
            except ConfinementError:
                return False
            return (cur is not None and op.base_fp is not None and stat.S_ISDIR(cur.st_mode)
                    and [cur.st_ino, cur.st_mode] == [op.base_fp[0], op.base_fp[4]])
        return _at_base(root, op)
    if last.target == op.target:
        return _left_by(root, last)
    try:
        return root.lstat(op.target) is None
    except ConfinementError:
        return False


def _recover_one(store: Store, journal: Path) -> str | None:
    """Validate every remaining op against the current tree first and change nothing unless all pass; then
    run them. Markers are written in op order, so the executed ops are a prefix, possibly plus one op that
    completed before its marker was written."""
    data = json.loads(journal.read_text())
    if data.get("state") != "applying":
        raise ValueError("journal is not in the applying state")
    cs = ChangeSet.from_json(data["changeset"])
    marker = _marker(journal)
    done = {int(x) for x in marker.read_text().split()} if marker.exists() else set()
    r = 0
    while r < len(cs.ops) and cs.ops[r].seq in done:
        r += 1
    if any(op.seq in done for op in cs.ops[r:]):
        raise CommitError(EXIT_CONFLICT, "journal markers are out of order")
    executed = list(cs.ops[:r])
    roots = _open_roots(cs)
    try:
        if r < len(cs.ops):
            op = cs.ops[r]
            root = roots[op.area]
            if not _in_expected_state(root, op, executed):
                # Only exactly what this op leaves counts: a mkdir leaves an empty 0700 directory, so a
                # directory someone else created there is not taken for ours.
                ours = op.op != "mkdir" or _empty_private_dir(root, op.target)
                if not (_left_by(root, op) and ours):
                    raise CommitError(EXIT_CONFLICT, f"{op.area}:{op.target} changed after the interrupted commit")
                executed.append(op)  # it completed before its marker was written
        remaining = cs.ops[len(executed):]
        for op in remaining:
            if not _in_expected_state(roots[op.area], op, executed):
                raise CommitError(EXIT_CONFLICT, f"{op.area}:{op.target} changed after the interrupted commit")
            if op.op == "rename_in" and not (op.source_upper and os.path.lexists(op.source_upper)):
                raise CommitError(EXIT_CONFLICT, f"{op.area}:{op.target}: the reviewed content is gone")
        _check_writable_parents(roots, remaining, cs.ops)
        dir_modes: list[ChangeOp] = []
        mark = _marker_writer(journal)
        try:
            # Directory modes are deferred to the end, so executed ones still need them. A crash inside
            # _finish_dirs may have applied some already (chmod 600 d): reopen those shallowest first so the
            # paths below stay reachable; _finish_dirs applies the reviewed modes again.
            for op in sorted(executed, key=lambda o: o.target.count("/")):
                if op.op not in ("mkdir", "chmod"):
                    continue
                root = roots[op.area]
                st = root.lstat(op.target)
                if st is None or not stat.S_ISDIR(st.st_mode):
                    continue
                if stat.S_IMODE(st.st_mode) & 0o700 != 0o700:
                    root.chmod(op.target, stat.S_IMODE(st.st_mode) | 0o700)
                dir_modes.append(op)
            for op in remaining:
                _apply_one(roots[op.area], op, dir_modes)
                mark(op.seq)
            problems = _verify(roots, cs) or _finish_dirs(roots, cs, dir_modes)
        except CommitError:
            raise
        except Exception as exc:
            raise CommitError(EXIT_FIDELITY, f"recovery stopped part-way ({type(exc).__name__}: {exc})",
                              partial=True) from exc
        if problems:
            raise CommitError(EXIT_FIDELITY, "; ".join(problems[:5]), partial=True)
    finally:
        _close(roots)
    return cs.run_id


def recover_all(store: Store) -> list[str]:
    """Finish interrupted commits where that is provably safe; retire every journal after one attempt."""
    done = []
    for journal in sorted(store.journal_dir.glob("*.json")):
        run_id = journal.stem
        try:
            finished = _recover_one(store, journal)
        except Exception as exc:
            partial = isinstance(exc, CommitError) and exc.partial
            log.error("could not recover commit %s (%s); journal retired; %s", run_id, exc,
                      "the tree may be partially updated" if partial else "nothing further was changed")
            _retire(journal)
            try:
                store.finish(store.run(run_id), "failed")
            except Exception:
                pass
            continue
        _drop(journal)
        try:
            run = store.run(finished)
            if run.root.exists():
                meta = store.load_meta(run)
                cs = ChangeSet.from_json(json.loads(run.changeset.read_text())) if run.changeset.exists() else None
                if cs is not None and "workspace" in cs.roots:
                    store.ledger_add(str(meta.get("session_id", "")), cs.roots["workspace"], _created(cs))
                store.finish(run, "committed")
        except Exception as exc:
            log.error("recovered %s but could not update its run record: %s", finished, exc)
        done.append(finished)
    return done
