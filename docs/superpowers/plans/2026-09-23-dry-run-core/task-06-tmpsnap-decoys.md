### Task 6: /tmp snapshot and decoys (S9, I14, H5)

**Files:**
- Create: `src/dryrun/sandbox/tmpsnap.py`, `src/dryrun/sandbox/decoys.py`, `tests/unit/test_tmpsnap.py`, `tests/unit/test_decoys.py`

**Interfaces:**
- Consumes: `dryrun.fingerprint.Fp`, `fp_of`
- Produces:
  - `TmpSnapshot(base_fps: dict[str, Fp], partial: bool, entries: int, bytes: int)`
  - `snapshot_tmp(dst: Path, src: Path = Path("/tmp"), *, max_entries: int, max_total: int, max_file: int, exclude: Path | None = None, uid: int | None = None) -> TmpSnapshot`
    - Copies only entries owned by `uid`: regular files, dirs and symlinks. Skips sockets, fifos, devices and mount points.
    - Preserves mode and mtime.
    - `exclude` (a workspace under /tmp) is created as an empty dir along with its ancestors, and never descended into.
    - `base_fps` holds the *real* source fingerprints of every copied path.
  - `make_decoys(decoy_root: Path, home: Path, secret_paths, token: str) -> list[tuple[str, str]]`: (decoy source, real destination), only for secret paths that exist
  - `new_token() -> str`
  - `needles(token) -> list[bytes]`
  - `scan(token, blobs: dict[str, bytes]) -> list[dict]`: returns `{"path": <decoy name or "">, "where": <blob key>}` for every blob containing the token (plain, hex or base64 at any alignment)

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_tmpsnap.py`:
```python
from __future__ import annotations

import os
import socket
from pathlib import Path

from dryrun.sandbox.tmpsnap import snapshot_tmp

LIMITS = dict(max_entries=100, max_total=10_000, max_file=1_000)


def test_copies_own_files_dirs_symlinks_and_preserves_metadata(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "d").mkdir(parents=True)
    (src / "d" / "x.sh").write_text("echo hi\n")
    os.chmod(src / "d" / "x.sh", 0o755)
    os.utime(src / "d" / "x.sh", ns=(1_000_000_000, 2_000_000_000))
    (src / "ln").symlink_to("d/x.sh")
    dst.mkdir()
    snap = snapshot_tmp(dst, src, **LIMITS)
    assert (dst / "d" / "x.sh").read_text() == "echo hi\n"
    assert os.stat(dst / "d" / "x.sh").st_mode & 0o777 == 0o755
    assert os.stat(dst / "d" / "x.sh").st_mtime_ns == 2_000_000_000
    assert os.readlink(dst / "ln") == "d/x.sh"
    assert set(snap.base_fps) == {"d", "d/x.sh", "ln"}
    assert snap.base_fps["d/x.sh"][0] == os.lstat(src / "d" / "x.sh").st_ino  # real inode
    assert not snap.partial


def test_skips_sockets_and_respects_caps(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(src / "sock"))
    (src / "big").write_bytes(b"x" * 2_000)
    (src / "small").write_bytes(b"y" * 10)
    snap = snapshot_tmp(dst, src, **LIMITS)
    s.close()
    assert not (dst / "sock").exists()
    assert not (dst / "big").exists()
    assert (dst / "small").exists()
    assert snap.partial


def test_exclude_creates_empty_mountpoint_without_copying(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "p" / "ws" / "src").mkdir(parents=True)
    (src / "p" / "ws" / "src" / "a.py").write_text("x")
    (src / "p" / "other.txt").write_text("o")
    dst.mkdir()
    snap = snapshot_tmp(dst, src, exclude=src / "p" / "ws", **LIMITS)
    assert (dst / "p" / "ws").is_dir()
    assert list((dst / "p" / "ws").iterdir()) == []
    assert (dst / "p" / "other.txt").exists()
    assert "p/ws/src/a.py" not in snap.base_fps
```

`tests/unit/test_decoys.py`:
```python
from __future__ import annotations

import base64
from pathlib import Path

from dryrun.sandbox.decoys import make_decoys, new_token, scan


def test_decoys_only_for_existing_paths_and_mirror_names(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("REAL KEY")
    (home / ".netrc").write_text("machine x password REAL")
    token = new_token()
    mounts = make_decoys(tmp_path / "decoys", home, [".ssh", ".netrc", ".aws"], token)
    dests = {dst for _, dst in mounts}
    assert dests == {str(home / ".ssh"), str(home / ".netrc")}
    ssh_src = next(src for src, dst in mounts if dst.endswith(".ssh"))
    content = (Path(ssh_src) / "id_ed25519").read_text()
    assert token in content and "REAL" not in content


def test_scan_finds_plain_hex_and_base64_at_every_alignment(tmp_path: Path):
    token = new_token()
    secret = f"DRYRUN-DECOY {token} .ssh/id_ed25519\n".encode()
    blobs = {
        "stdout": b"leak: " + secret,
        "stderr": b"nothing here",
        "workspace/out.hex": secret.hex().encode(),
        "workspace/out.b64a": base64.b64encode(secret),
        "workspace/out.b64b": base64.b64encode(b"x" + secret),
        "workspace/out.b64c": base64.b64encode(b"xy" + secret),
    }
    hits = {h["where"] for h in scan(token, blobs)}
    assert hits == {"stdout", "workspace/out.hex", "workspace/out.b64a", "workspace/out.b64b",
                    "workspace/out.b64c"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_tmpsnap.py tests/unit/test_decoys.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

`src/dryrun/sandbox/tmpsnap.py`:
```python
"""Copied lower layer for the shadow's /tmp.

The real /tmp cannot be an unprivileged overlay lower layer when something is mounted inside it
(WSLg's /tmp/.X11-unix → "failed to clone lowerpath", spike 0). We copy (never hard-link: that would
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
            if excl_rel and (child == excl_rel):
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
```

`src/dryrun/sandbox/decoys.py`:
```python
"""Decoy credentials with per-run canary tokens (I14, harm-policy H5).

Without network, a credential read only matters if it reaches the agent (stdout/stderr) or the
real disk (a committed file). The token scan detects exactly that, in plain, hex or base64 form.
"""
from __future__ import annotations

import base64
import os
import secrets
import stat
from pathlib import Path
from typing import Iterable

MAX_DIR_ENTRIES = 50


def new_token() -> str:
    return "DRT" + secrets.token_hex(16)


def decoy_text(token: str, name: str) -> str:
    return f"DRYRUN-DECOY {token} {name}\n"


def make_decoys(decoy_root: Path, home: Path, secret_paths: Iterable[str], token: str) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    decoy_root.mkdir(parents=True, exist_ok=True)
    for i, rel in enumerate(secret_paths):
        real = Path(home) / rel
        try:
            st = os.lstat(real)
        except (FileNotFoundError, NotADirectoryError):
            continue
        src = decoy_root / str(i)
        if stat.S_ISDIR(st.st_mode) or (stat.S_ISLNK(st.st_mode) and real.is_dir()):
            src.mkdir()
            try:
                names = sorted(os.listdir(real))[:MAX_DIR_ENTRIES]
            except OSError:
                names = []
            for name in names:
                (src / name).write_text(decoy_text(token, f"{rel}/{name}"))
        elif stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            src.write_text(decoy_text(token, rel))
        else:
            continue
        mounts.append((str(src), str(real)))
    return mounts


def needles(token: str) -> list[bytes]:
    raw = token.encode()
    found = [raw, raw.hex().encode()]
    for pad in range(3):
        enc = base64.b64encode(b"\0" * pad + raw)
        # Drop the first block (affected by the padding bytes) and the last one (by what follows).
        found.append(enc[4:-4] if pad else enc[:-4])
    return [n for n in found if len(n) >= 16]


def scan(token: str, blobs: dict[str, bytes]) -> list[dict]:
    keys = needles(token)
    hits = []
    for where, blob in blobs.items():
        if any(k in blob for k in keys):
            hits.append({"path": "", "where": where})
    return hits
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_tmpsnap.py tests/unit/test_decoys.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/sandbox/tmpsnap.py src/dryrun/sandbox/decoys.py tests/unit/test_tmpsnap.py tests/unit/test_decoys.py
git commit -m "feat: copied /tmp lower snapshot and canary-token decoys"
```
