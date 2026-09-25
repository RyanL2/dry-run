"""Translate an overlay upper directory into FsEntry records and an ordered ChangeSet (ARCHITECTURE §7)."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dryrun.fingerprint import Fp, digest, subtree
from dryrun.types import ChangeOp, FsEntry

HASH_LIMIT = 64 * 1024**2
CONTENT_LIMIT = 16 * 1024**2
SPECIAL_BITS = stat.S_ISUID | stat.S_ISGID


@dataclass
class AreaEffect:
    entries: list[FsEntry] = field(default_factory=list)
    ops: list[ChangeOp] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    contents: dict[str, Path] = field(default_factory=dict)
    bytes_written: int = 0
    truncated: bool = False


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_whiteout(path: str, st: os.stat_result) -> bool:
    if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
        return True
    if stat.S_ISREG(st.st_mode) and st.st_size == 0:
        try:
            os.getxattr(path, "user.overlay.whiteout", follow_symlinks=False)
            return True
        except OSError:
            return False
    return False


def _is_opaque(path: str) -> bool:
    try:
        return os.getxattr(path, "user.overlay.opaque", follow_symlinks=False) == b"y"
    except OSError:
        return False


def _files_equal(a: str, b: str) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca, cb = fa.read(1 << 20), fb.read(1 << 20)
            if ca != cb:
                return False
            if not ca:
                return True


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _Extractor:
    def __init__(self, area: str, lower: Path, upper: Path, base_fps: dict[str, Fp],
                 skip: frozenset[str], expand_limit: int) -> None:
        self.area, self.lower, self.upper = area, str(lower), str(upper)
        self.base, self.skip, self.expand_limit = base_fps, skip, expand_limit
        self.eff = AreaEffect()
        self.fresh: set[str] = set()  # dirs whose lower content is hidden (created or opaque)
        self.deletes: list[ChangeOp] = []
        self.mkdirs: list[ChangeOp] = []
        self.puts: list[ChangeOp] = []
        self.chmod_files: list[ChangeOp] = []
        self.chmod_dirs: list[ChangeOp] = []

    # --- helpers -------------------------------------------------------------------------------
    def _op(self, op: str, rel: str, **kw) -> ChangeOp:
        # Adding or removing an entry needs a writable parent; the commit never loosens a real directory's
        # mode to get one, so such an effect cannot be committed faithfully.
        refusal = {"path": rel, "reason": "read_only_parent"}
        if op != "chmod" and not self._parent_fresh(rel) and refusal not in self.eff.refused:
            parent = os.path.join(self.lower, rel.rpartition("/")[0])
            if os.path.isdir(parent) and not os.access(parent, os.W_OK | os.X_OK):
                self.eff.refused.append(refusal)
        return ChangeOp(seq=-1, op=op, area=self.area, target=rel, base_fp=self._base(rel), **kw)

    def _base(self, rel: str) -> list[int] | None:
        fp = self.base.get(rel)
        return list(fp) if fp is not None else None

    def _refuse(self, rel: str, reason: str) -> None:
        self.eff.refused.append({"path": rel, "reason": reason})

    def _skipped(self, rel: str) -> bool:
        return any(rel == s or rel.startswith(s + "/") for s in self.skip)

    def _parent_fresh(self, rel: str) -> bool:
        parent = rel.rpartition("/")[0]
        return bool(parent) and parent in self.fresh

    # --- effects -------------------------------------------------------------------------------
    def _delete(self, rel: str, lst: os.stat_result) -> None:
        if stat.S_ISDIR(lst.st_mode):
            self.deletes.append(self._op("rmtree", rel, kind="dir",
                                         subtree_digest=digest(subtree(self.base, rel))))
            self._expand_deleted_dir(rel, lst)
        else:
            self.deletes.append(self._op("unlink", rel, kind=_kind(lst.st_mode)))
            self.eff.entries.append(FsEntry(op="delete", path=rel, kind=_kind(lst.st_mode), preexisting=True,
                                            bytes_before=lst.st_size, mode_before=stat.S_IMODE(lst.st_mode)))

    def _expand_deleted_dir(self, rel: str, lst: os.stat_result) -> None:
        self.eff.entries.append(FsEntry(op="delete", path=rel, kind="dir", preexisting=True,
                                        mode_before=stat.S_IMODE(lst.st_mode)))
        count, stack = 0, [rel]
        while stack:
            d = stack.pop()
            try:
                names = sorted(os.listdir(os.path.join(self.lower, d)))
            except OSError:
                continue
            for name in names:
                child = f"{d}/{name}"
                st = _lstat(os.path.join(self.lower, child))
                if st is None:
                    continue
                count += 1
                if count > self.expand_limit:
                    self.eff.truncated = True
                    return
                self.eff.entries.append(FsEntry(op="delete", path=child, kind=_kind(st.st_mode), preexisting=True,
                                                bytes_before=st.st_size if stat.S_ISREG(st.st_mode) else None,
                                                mode_before=stat.S_IMODE(st.st_mode)))
                if stat.S_ISDIR(st.st_mode):
                    stack.append(child)

    def _mkdir(self, rel: str, st: os.stat_result) -> None:
        self.mkdirs.append(self._op("mkdir", rel, kind="dir", mode=stat.S_IMODE(st.st_mode)))
        self.eff.entries.append(FsEntry(op="create", path=rel, kind="dir", preexisting=False,
                                        mode_after=stat.S_IMODE(st.st_mode)))

    def _put(self, rel: str, up: str, st: os.stat_result, lst: os.stat_result | None) -> None:
        size = st.st_size
        self.puts.append(self._op("rename_in", rel, source_upper=up, kind="file", mode=stat.S_IMODE(st.st_mode),
                                  sha256=_sha256(up) if size <= HASH_LIMIT else None, size=size,
                                  mtime_ns=st.st_mtime_ns))
        modify = lst is not None and stat.S_ISREG(lst.st_mode)
        self.eff.entries.append(FsEntry(
            op="modify" if modify else "create", path=rel, kind="file", preexisting=modify,
            bytes_before=lst.st_size if modify else None, bytes_after=size,
            mode_before=stat.S_IMODE(lst.st_mode) if modify else None, mode_after=stat.S_IMODE(st.st_mode)))
        if size <= CONTENT_LIMIT:
            self.eff.contents[rel] = Path(up)
        self.eff.bytes_written += size

    def _chmod(self, rel: str, st: os.stat_result, lst: os.stat_result) -> None:
        op = self._op("chmod", rel, kind=_kind(st.st_mode), mode=stat.S_IMODE(st.st_mode))
        (self.chmod_dirs if stat.S_ISDIR(st.st_mode) else self.chmod_files).append(op)
        self.eff.entries.append(FsEntry(op="mode", path=rel, kind=_kind(st.st_mode), preexisting=True,
                                        mode_before=stat.S_IMODE(lst.st_mode), mode_after=stat.S_IMODE(st.st_mode)))

    def _symlink(self, rel: str, target: str, lst: os.stat_result | None) -> None:
        self.puts.append(self._op("symlink", rel, kind="symlink", link_target=target))
        replaced = lst is not None and stat.S_ISLNK(lst.st_mode)
        self.eff.entries.append(FsEntry(op="modify" if replaced else "create", path=rel, kind="symlink",
                                        preexisting=replaced))

    # --- walk ----------------------------------------------------------------------------------
    def visit(self, rel: str, stack: list[str]) -> None:
        up = os.path.join(self.upper, rel)
        st = os.lstat(up)
        low = os.path.join(self.lower, rel)
        lst = None if self._parent_fresh(rel) else _lstat(low)
        if _is_whiteout(up, st):
            if lst is not None:
                self._delete(rel, lst)
            return
        if stat.S_ISDIR(st.st_mode):
            if lst is None:
                self._mkdir(rel, st)
                self.fresh.add(rel)
            elif not stat.S_ISDIR(lst.st_mode) or _is_opaque(up):
                self._delete(rel, lst)
                self._mkdir(rel, st)
                self.fresh.add(rel)
            elif stat.S_IMODE(st.st_mode) != stat.S_IMODE(lst.st_mode):
                self._chmod(rel, st, lst)
            # The mode is recorded above; the upper copy needs u+rwx so its children can be listed and moved.
            if stat.S_IMODE(st.st_mode) & 0o700 != 0o700:
                os.chmod(up, stat.S_IMODE(st.st_mode) | 0o700)
            stack.append(rel)
            return
        if stat.S_ISREG(st.st_mode):
            if st.st_mode & SPECIAL_BITS:
                return self._refuse(rel, "setuid_or_setgid")
            if st.st_nlink > 1:
                return self._refuse(rel, "hardlink_in_shadow")
            if lst is None:
                return self._put(rel, up, st, None)
            if stat.S_ISREG(lst.st_mode):
                if lst.st_nlink > 1:
                    return self._refuse(rel, "hardlink_modified")
                same = lst.st_size == st.st_size and _files_equal(up, low)
                if same and lst.st_mtime_ns == st.st_mtime_ns:
                    if stat.S_IMODE(lst.st_mode) != stat.S_IMODE(st.st_mode):
                        self._chmod(rel, st, lst)
                    return
                return self._put(rel, up, st, lst)
            self._delete(rel, lst)
            return self._put(rel, up, st, None)
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(up)
            if lst is not None and stat.S_ISLNK(lst.st_mode) and os.readlink(low) == target:
                return
            if lst is not None:
                self._delete(rel, lst)
            return self._symlink(rel, target, lst)
        self._refuse(rel, "special_file")

    def run(self, seq_start: int) -> AreaEffect:
        # The upper root is never visited. Its initial mode matches the lower
        # root, so any difference is a root mode change we cannot commit.
        root_mode = stat.S_IMODE(os.lstat(self.upper).st_mode)
        low = _lstat(self.lower)
        if low is not None and root_mode != stat.S_IMODE(low.st_mode):
            self._refuse(".", "workspace_root_mode")
            os.chmod(self.upper, root_mode | 0o700)
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            try:
                names = sorted(os.listdir(os.path.join(self.upper, rel_dir) if rel_dir else self.upper))
            except OSError:
                self._refuse(rel_dir or ".", "unreadable_dir")  # never silently hide a directory's effects
                continue
            for name in names:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if self._skipped(rel):
                    continue
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError:
                    self._refuse(rel.encode("utf-8", "surrogateescape").decode("utf-8", "replace"), "non_utf8_name")
                    continue
                self.visit(rel, stack)

        def depth(o: ChangeOp) -> int:
            return o.target.count("/")

        ordered = (sorted(self.deletes, key=depth, reverse=True) + sorted(self.mkdirs, key=depth)
                   + sorted(self.puts, key=depth) + self.chmod_files
                   + sorted(self.chmod_dirs, key=depth, reverse=True))
        for i, op in enumerate(ordered):
            op.seq = seq_start + i
        self.eff.ops = ordered
        return self.eff


def extract(area: str, lower: Path, upper: Path, base_fps: dict[str, Fp], *, skip: frozenset[str] = frozenset(),
            expand_limit: int = 10_000, seq_start: int = 0) -> AreaEffect:
    return _Extractor(area, Path(lower), Path(upper), base_fps, skip, expand_limit).run(seq_start)
