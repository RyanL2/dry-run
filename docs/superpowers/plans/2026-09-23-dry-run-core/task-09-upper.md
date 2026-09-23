### Task 9: Upper-dir extraction (F5)

**Files:**
- Create: `src/dryrun/effects/__init__.py` (empty), `src/dryrun/effects/upper.py`, `tests/unit/test_upper.py`, `tests/sandbox/test_upper_real.py`

**Interfaces:**
- Consumes: `Fp`, `digest`, `subtree` (Task 3); `ChangeOp`, `FsEntry` (Task 2); `run_shadow` + the `simple_spec` test helper (Task 7)
- Produces:
  - `AreaEffect(entries: list[FsEntry], ops: list[ChangeOp], refused: list[dict], contents: dict[str, Path], bytes_written: int, truncated: bool)`
  - `extract(area: str, lower: Path, upper: Path, base_fps: dict[str, Fp], *, skip: frozenset[str] = frozenset(), expand_limit: int = 10_000, seq_start: int = 0) -> AreaEffect`
    - The ops come back ordered and numbered from `seq_start`: deletes (deepest first), mkdirs (shallowest first), `rename_in` and `symlink` (shallowest first), file chmods, then dir chmods (deepest first).
    - `mkdir` ops carry the **final** mode. The commit engine creates dirs 0700 and applies final modes at the end.
    - Every op's `base_fp` is `base_fps.get(target)`: the target's fingerprint at shadow start, or None if it did not exist.

**Upper entry → result:**

| Upper entry | Result |
|---|---|
| whiteout (char 0:0, or empty file with `user.overlay.whiteout`) | delete; a deleted dir is expanded into one entry per lower descendant |
| dir with `user.overlay.opaque=y`, or replacing a non-dir | delete lower + `mkdir`; children treated as new |
| dir not in lower | `mkdir` (create) |
| dir in lower, mode differs | `chmod` |
| regular file, lower absent | `rename_in` (create) |
| regular file identical to lower (content, mode, mtime) | dropped (no-op copy-up) |
| identical content and mtime, different mode | `chmod` |
| otherwise | `rename_in` (modify) |
| symlink | `symlink` (dropped if the lower has the same target) |
| hard link (nlink > 1 in upper or lower), setuid/setgid, fifo, socket, device, non-UTF-8 name | refused |

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_upper.py`:
```python
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from dryrun.effects.upper import extract
from dryrun.fingerprint import fingerprint_tree


def whiteout(path: Path) -> None:
    path.write_bytes(b"")
    os.setxattr(path, "user.overlay.whiteout", b"y")


def setup(tmp_path: Path) -> tuple[Path, Path]:
    lower, upper = tmp_path / "lower", tmp_path / "upper"
    (lower / "src").mkdir(parents=True)
    (lower / "src" / "a.py").write_text("print(1)\n")
    (lower / "src" / "b.py").write_text("print(2)\n")
    (lower / "old").mkdir()
    (lower / "old" / "x.txt").write_text("x")
    (lower / "old" / "y.txt").write_text("y")
    (lower / "same.txt").write_text("same")
    (lower / "mode.sh").write_text("#!/bin/sh\n")
    upper.mkdir()
    return lower, upper


def by_path(entries):
    return {e.path: e for e in entries}


def test_create_modify_delete_and_noise(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    (upper / "src").mkdir()
    (upper / "src" / "a.py").write_text("print(10)\n")          # modify
    whiteout(upper / "src" / "b.py")                               # delete
    (upper / "new.txt").write_text("n")                            # create
    same = upper / "same.txt"                                       # no-op copy-up
    same.write_text("same")
    st = os.stat(lower / "same.txt")
    os.utime(same, ns=(st.st_atime_ns, st.st_mtime_ns))
    eff = extract("workspace", lower, upper, base)
    e = by_path(eff.entries)
    assert e["src/a.py"].op == "modify" and e["src/a.py"].preexisting
    assert e["src/a.py"].bytes_before == 9 and e["src/a.py"].bytes_after == 10
    assert e["src/b.py"].op == "delete" and e["src/b.py"].preexisting
    assert e["new.txt"].op == "create" and not e["new.txt"].preexisting
    assert "same.txt" not in e
    ops = {(o.op, o.target) for o in eff.ops}
    assert ops == {("rename_in", "src/a.py"), ("unlink", "src/b.py"), ("rename_in", "new.txt")}
    a = next(o for o in eff.ops if o.target == "src/a.py")
    assert a.base_fp == list(base["src/a.py"]) and a.sha256 and a.size == 10
    n = next(o for o in eff.ops if o.target == "new.txt")
    assert n.base_fp is None
    assert set(eff.contents) == {"src/a.py", "new.txt"}


def test_deleted_dir_is_expanded_and_uses_rmtree(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    whiteout(upper / "old")
    eff = extract("workspace", lower, upper, base)
    assert {e.path for e in eff.entries if e.op == "delete"} == {"old", "old/x.txt", "old/y.txt"}
    (op,) = eff.ops
    assert op.op == "rmtree" and op.target == "old" and op.subtree_digest


def test_opaque_dir_replaces_lower_contents(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    (upper / "old").mkdir()
    os.setxattr(upper / "old", "user.overlay.opaque", b"y")
    (upper / "old" / "z.txt").write_text("z")
    eff = extract("workspace", lower, upper, base)
    kinds = [(o.op, o.target) for o in eff.ops]
    assert kinds == [("rmtree", "old"), ("mkdir", "old"), ("rename_in", "old/z.txt")]
    assert by_path(eff.entries)["old/z.txt"].op == "create"


def test_chmod_only_becomes_chmod(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    up = upper / "mode.sh"
    up.write_text("#!/bin/sh\n")
    st = os.stat(lower / "mode.sh")
    os.chmod(up, 0o755)
    os.utime(up, ns=(st.st_atime_ns, st.st_mtime_ns))
    eff = extract("workspace", lower, upper, base)
    (op,) = eff.ops
    assert (op.op, op.mode) == ("chmod", 0o755)
    assert by_path(eff.entries)["mode.sh"].op == "mode"


def test_symlink_create_and_refusals(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    (upper / "link").symlink_to("/etc/passwd")
    (upper / "h1").write_text("h")
    os.link(upper / "h1", upper / "h2")
    (upper / "suid").write_text("s")
    os.chmod(upper / "suid", 0o4755)
    os.mkfifo(upper / "fifo")
    eff = extract("workspace", lower, upper, base)
    link = next(o for o in eff.ops if o.target == "link")
    assert link.op == "symlink" and link.link_target == "/etc/passwd"
    reasons = {r["path"]: r["reason"] for r in eff.refused}
    assert reasons == {"h1": "hardlink_in_shadow", "h2": "hardlink_in_shadow",
                       "suid": "setuid_or_setgid", "fifo": "special_file"}


def test_op_order_and_sequence(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    whiteout(upper / "old")
    (upper / "n1" / "n2").mkdir(parents=True)
    (upper / "n1" / "n2" / "f").write_text("f")
    eff = extract("workspace", lower, upper, base, seq_start=5)
    assert [o.seq for o in eff.ops] == list(range(5, 5 + len(eff.ops)))
    assert [o.op for o in eff.ops] == ["rmtree", "mkdir", "mkdir", "rename_in"]
    assert [o.target for o in eff.ops][1:3] == ["n1", "n1/n2"]


def test_extract_odd_names(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    for name in ["with space.txt", "-leading", "unicodé.txt", "new\nline"]:
        (upper / name).write_text("x")
    fd = os.open(os.path.join(os.fsencode(upper), b"bad\xff"), os.O_CREAT | os.O_WRONLY, 0o644)
    os.close(fd)
    eff = extract("workspace", lower, upper, base)
    assert {o.target for o in eff.ops} == {"with space.txt", "-leading", "unicodé.txt", "new\nline"}
    assert [r["reason"] for r in eff.refused] == ["non_utf8_name"]


def test_skip_ignores_subtree(tmp_path: Path):
    lower, upper = setup(tmp_path)
    (upper / "mnt" / "ws").mkdir(parents=True)
    (upper / "mnt" / "ws" / "f").write_text("f")
    eff = extract("tmp", lower, upper, fingerprint_tree(lower), skip=frozenset({"mnt/ws"}))
    assert {o.target for o in eff.ops} == {"mnt"}
```

`tests/sandbox/test_upper_real.py`:
```python
from __future__ import annotations

import os
from pathlib import Path

import pytest

from dryrun.effects.upper import extract
from dryrun.fingerprint import fingerprint_tree
from dryrun.sandbox.spawn import run_shadow
from tests.sandbox.conftest import simple_spec

pytestmark = pytest.mark.sandbox


def shadow_extract(ws: Path, run_dir: Path, cfg, cmd: str):
    base = fingerprint_tree(ws)
    res = run_shadow(simple_spec(ws, run_dir, cmd), run_id="u" + os.urandom(3).hex(), out_dir=run_dir,
                     cfg=cfg, watch_fs=run_dir)
    assert res.exit_code == 0, res.stderr_path.read_text()
    return extract("workspace", ws, run_dir / "ws.up", base)


def test_real_kernel_whiteouts_and_dir_rename(ws, run_dir, shadow_cfg):
    (ws / "d").mkdir()
    (ws / "d" / "f.txt").write_text("f")
    eff = shadow_extract(ws, run_dir, shadow_cfg, "rm keep.txt && mv d e && : > e/f.txt")
    e = {x.path: x for x in eff.entries}
    assert e["keep.txt"].op == "delete"
    assert e["d/f.txt"].op == "delete"
    assert e["e/f.txt"].op == "create" and e["e/f.txt"].bytes_after == 0
    assert eff.refused == []


def test_real_touch_and_chmod(ws, run_dir, shadow_cfg):
    eff = shadow_extract(ws, run_dir, shadow_cfg, "chmod +x keep.txt")
    (op,) = eff.ops
    assert op.op == "chmod" and op.mode & 0o111
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_upper.py tests/sandbox/test_upper_real.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.effects'`)

- [ ] **Step 3: Implement**

`src/dryrun/effects/__init__.py`: empty file.

`src/dryrun/effects/upper.py`:
```python
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
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            try:
                names = sorted(os.listdir(os.path.join(self.upper, rel_dir) if rel_dir else self.upper))
            except OSError:
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
        depth = lambda o: o.target.count("/")  # noqa: E731
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_upper.py tests/sandbox/test_upper_real.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/effects tests/unit/test_upper.py tests/sandbox/test_upper_real.py
git commit -m "feat: overlay upper-dir extraction into effect entries and ordered change ops"
```
