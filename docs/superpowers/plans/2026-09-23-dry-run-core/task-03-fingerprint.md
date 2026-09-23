### Task 3: Fingerprints and submount detection

**Files:**
- Create: `src/dryrun/fingerprint.py`, `tests/unit/test_fingerprint.py`

**Interfaces:**
- Produces:
  - `Fp = tuple[int, int, int, int, int]` (ino, size, mtime_ns, ctime_ns, mode)
  - `fp_of(st) -> Fp`
  - `lstat_fp(path) -> Fp | None`
  - `fingerprint_tree(root: Path) -> dict[str, Fp]`: relative POSIX paths, root excluded, never follows symlinks, does not descend into other filesystems
  - `digest(fps: dict[str, Fp]) -> str` (sha256 hex)
  - `subtree(fps, rel) -> dict[str, Fp]`: `rel` and its descendants
  - `changed_paths(before, after) -> set[str]`
  - `submounts(root: Path, mountinfo: str | None = None) -> list[str]`: mount points strictly inside root

- [ ] **Step 1: Write the failing test**

`tests/unit/test_fingerprint.py`:
```python
from __future__ import annotations

import os
import time
from pathlib import Path

from dryrun.fingerprint import changed_paths, digest, fingerprint_tree, lstat_fp, submounts, subtree


def make_tree(root: Path) -> None:
    (root / "a").mkdir()
    (root / "a" / "f.txt").write_text("x")
    (root / "g.txt").write_text("y")
    (root / "link").symlink_to("/etc/passwd")


def test_fingerprint_tree_lists_all_paths_without_following_symlinks(tmp_path: Path):
    make_tree(tmp_path)
    fps = fingerprint_tree(tmp_path)
    assert set(fps) == {"a", "a/f.txt", "g.txt", "link"}
    assert fps["link"] == lstat_fp(tmp_path / "link")


def test_digest_is_stable_and_sensitive(tmp_path: Path):
    make_tree(tmp_path)
    before = fingerprint_tree(tmp_path)
    assert digest(before) == digest(fingerprint_tree(tmp_path))
    time.sleep(0.01)
    (tmp_path / "g.txt").write_text("changed")
    after = fingerprint_tree(tmp_path)
    assert digest(before) != digest(after)
    assert changed_paths(before, after) == {"g.txt"}


def test_changed_paths_sees_add_and_remove(tmp_path: Path):
    make_tree(tmp_path)
    before = fingerprint_tree(tmp_path)
    (tmp_path / "new").write_text("n")
    os.unlink(tmp_path / "g.txt")
    assert changed_paths(before, fingerprint_tree(tmp_path)) == {"new", "g.txt"}


def test_subtree_selects_descendants_only(tmp_path: Path):
    make_tree(tmp_path)
    (tmp_path / "ab").write_text("not a child of a")
    fps = fingerprint_tree(tmp_path)
    assert set(subtree(fps, "a")) == {"a", "a/f.txt"}


def test_lstat_fp_missing_is_none(tmp_path: Path):
    assert lstat_fp(tmp_path / "nope") is None


def test_submounts_parses_mountinfo():
    info = (
        "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "40 22 0:5 / /home/u/ws/data rw - tmpfs tmpfs rw\n"
        "41 22 0:6 / /home/u/ws2 rw - tmpfs tmpfs rw\n"
        "42 22 0:7 / /home/u/ws/with\\040space rw - tmpfs tmpfs rw\n"
    )
    assert submounts(Path("/home/u/ws"), info) == ["/home/u/ws/data", "/home/u/ws/with space"]
    assert submounts(Path("/home/u/other"), info) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/unit/test_fingerprint.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.fingerprint'`)

- [ ] **Step 3: Implement**

`src/dryrun/fingerprint.py`:
```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/unit/test_fingerprint.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/fingerprint.py tests/unit/test_fingerprint.py
git commit -m "feat: tree fingerprints, digests and submount detection"
```
