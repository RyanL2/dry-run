### Task 10: Git state (F6, S10)

**Files:**
- Create: `src/dryrun/effects/gitstate.py`, `tests/unit/test_gitstate_refs.py`, `tests/sandbox/test_gitstate.py`

**Interfaces:**
- Consumes: `run_readonly` (Task 7)
- Produces:
  - `find_workspace_root(cwd: Path) -> Path`: nearest ancestor containing `.git` (dir or file), else `cwd`
  - `GitSnapshot(is_repo: bool, status: dict[str, str], ignored_dirs: tuple[str, ...], tracked: frozenset[str], head_blobs: dict[str, str], index_blobs: dict[str, str])`
    - `status` maps a path to `tracked_dirty|untracked|ignored`
  - `snapshot(ws_root: Path) -> GitSnapshot`: one sandboxed invocation running `status`, `ls-files -s` and `ls-tree -r HEAD`
  - `recoverability(snap, rel) -> str | None`: None if not a repo, else `tracked_clean|tracked_dirty|untracked|ignored`
  - `blob_sha1(path: Path) -> str` (git blob id for SHA-1 repos)
  - `read_refs(git_dir: Path, upper_git_dir: Path | None = None) -> dict[str, str]`
    - Keys are `HEAD` plus `refs/...`; values are object ids.
    - It merges lower and upper (whiteouts delete; upper loose refs and upper `packed-refs` win).
    - It is pure Python and runs no git.
  - `is_ancestor(ws_root: Path, old: str, new: str, extra_objects: Path | None) -> bool | None` (sandboxed `git merge-base --is-ancestor`)
  - `objects_exist(ws_root: Path, shas: set[str]) -> set[str]`: which blob ids already exist in the repo's object store (sandboxed `git cat-file --batch-check`). If a tracked file's uncommitted edits are replaced by *any* already-known blob (HEAD, the index, or an older commit, as with `reset --hard HEAD~1`), the work was discarded.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_gitstate_refs.py`:
```python
from __future__ import annotations

import os
from pathlib import Path

from dryrun.effects.gitstate import find_workspace_root, read_refs

A, B, C = "a" * 40, "b" * 40, "c" * 40


def make_git(root: Path) -> Path:
    g = root / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text(A + "\n")
    (g / "packed-refs").write_text(f"# pack-refs with: peeled\n{B} refs/heads/old\n{C} refs/tags/v1\n")
    return g


def test_read_refs_lower_only(tmp_path: Path):
    g = make_git(tmp_path)
    assert read_refs(g) == {"HEAD": A, "refs/heads/main": A, "refs/heads/old": B, "refs/tags/v1": C}


def test_read_refs_merges_upper_with_whiteouts(tmp_path: Path):
    g = make_git(tmp_path / "lower")
    up = tmp_path / "upper" / ".git"
    (up / "refs" / "heads").mkdir(parents=True)
    (up / "refs" / "heads" / "main").write_text(C + "\n")        # moved
    (up / "refs" / "heads" / "feature").write_text(B + "\n")     # created
    (up / "packed-refs").write_text(f"{C} refs/tags/v1\n")        # repacked without refs/heads/old
    refs = read_refs(g, up)
    assert refs == {"HEAD": C, "refs/heads/main": C, "refs/heads/feature": B, "refs/tags/v1": C}
    wo = up / "refs" / "heads" / "main"
    wo.unlink()
    wo.write_bytes(b"")
    os.setxattr(wo, "user.overlay.whiteout", b"y")
    assert "refs/heads/main" not in read_refs(g, up)


def test_find_workspace_root(tmp_path: Path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "a" / "b").mkdir(parents=True)
    assert find_workspace_root(tmp_path / "repo" / "a" / "b") == tmp_path / "repo"
    (tmp_path / "plain").mkdir()
    assert find_workspace_root(tmp_path / "plain") == tmp_path / "plain"
```

`tests/sandbox/test_gitstate.py`:
```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dryrun.effects.gitstate import blob_sha1, is_ancestor, recoverability, snapshot

pytestmark = pytest.mark.sandbox


def git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(ws), "GIT_AUTHOR_NAME": "t",
                               "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


@pytest.fixture
def repo(scratch: Path) -> Path:
    ws = scratch / "repo"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    (ws / ".gitignore").write_text("build/\n")
    (ws / "clean.py").write_text("clean\n")
    (ws / "dirty.py").write_text("v1\n")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "init")
    (ws / "dirty.py").write_text("v2\n")
    (ws / "new.py").write_text("new\n")
    (ws / "build").mkdir()
    (ws / "build" / "out.o").write_text("o")
    return ws


def test_recoverability_classes(repo: Path):
    snap = snapshot(repo)
    assert snap.is_repo
    assert recoverability(snap, "clean.py") == "tracked_clean"
    assert recoverability(snap, "dirty.py") == "tracked_dirty"
    assert recoverability(snap, "new.py") == "untracked"
    assert recoverability(snap, "build/out.o") == "ignored"


def test_blob_shas_match_git(repo: Path):
    snap = snapshot(repo)
    assert snap.head_blobs["dirty.py"] == blob_sha1_of_text("v1\n")
    assert blob_sha1(repo / "clean.py") == git(repo, "hash-object", "clean.py")


def blob_sha1_of_text(text: str) -> str:
    import hashlib
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_snapshot_does_not_run_fsmonitor_outside_sandbox(repo: Path, scratch: Path):
    marker = scratch / "fsmonitor-ran"
    hook = scratch / "fsmon.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))
    snapshot(repo)
    assert not marker.exists()


def test_is_ancestor(repo: Path):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "-am", "second")
    second = git(repo, "rev-parse", "HEAD")
    assert is_ancestor(repo, first, second, None) is True
    assert is_ancestor(repo, second, first, None) is False


def test_objects_exist(repo: Path):
    from dryrun.effects.gitstate import objects_exist
    old = blob_sha1_of_text("v1\n")
    missing = blob_sha1_of_text("never committed\n")
    assert objects_exist(repo, {old, missing}) == {old}
    assert objects_exist(repo, set()) == set()


def test_non_repo(scratch: Path):
    d = scratch / "plain"
    d.mkdir()
    snap = snapshot(d)
    assert not snap.is_repo and recoverability(snap, "x") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_gitstate_refs.py tests/sandbox/test_gitstate.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

`src/dryrun/effects/gitstate.py`:
```python
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


def _loose(git_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    base = git_dir / "refs"
    if not base.is_dir():
        return out
    for dirpath, _, files in os.walk(base):
        for name in files:
            p = Path(dirpath) / name
            out[p.relative_to(git_dir).as_posix()] = p
    return out


def _packed(path: Path) -> dict[str, str]:
    refs: dict[str, str] = {}
    try:
        text = path.read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError):
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
    up = Path(upper_git_dir) if upper_git_dir is not None else None
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
        value = p.read_text(errors="replace").strip()
        if len(value) in (40, 64):
            refs[name] = value
    head_file = up / "HEAD" if up is not None and os.path.lexists(up / "HEAD") else git_dir / "HEAD"
    try:
        head = head_file.read_text().strip()
    except FileNotFoundError:
        head = ""
    if head.startswith("ref: "):
        target = head[5:].strip()
        if target in refs:
            refs["HEAD"] = refs[target]
    elif len(head) in (40, 64):
        refs["HEAD"] = head
    return refs


_HEX = set("0123456789abcdef")


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


def is_ancestor(ws_root: Path, old: str, new: str, extra_objects: Path | None) -> bool | None:
    env = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(extra_objects)} if extra_objects else {}
    res = run_readonly(["git", *_SAFE, "merge-base", "--is-ancestor", old, new], cwd=Path(ws_root), env=env)
    if res.returncode == 0:
        return True
    if res.returncode == 1:
        return False
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_gitstate_refs.py tests/sandbox/test_gitstate.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/effects/gitstate.py tests/unit/test_gitstate_refs.py tests/sandbox/test_gitstate.py
git commit -m "feat: sandboxed git snapshot, recoverability, blob ids and merged ref view"
```
