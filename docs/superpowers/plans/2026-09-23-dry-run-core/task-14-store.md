### Task 14: Store (F1, F10 tokens, F15, F16, S6)

**Files:**
- Create: `src/dryrun/store.py`, `tests/unit/test_store.py`

**Interfaces:**
- Consumes: `ensure_private_dir` (Task 1); `RunPaths` (Task 11)
- Produces:
  - `TokenError(Exception)`
  - `Store(root: Path)` with attributes `runs_dir`, `sessions_dir`, `journal_dir`, `log_path`, and these methods:

    | Method | Behaviour |
    |---|---|
    | `new_run() -> RunPaths` | creates the run dirs; id is `<hex time>-<8 hex>` |
    | `run(run_id) -> RunPaths` | validates the id format, else `TokenError` |
    | `save_meta(run, **fields)` / `load_meta(run) -> dict` | read and update `meta.json` |
    | `authorize(run, *, session_id: str, decision: str) -> str` | returns a fresh token; status `authorized` for allow, `pending` for ask |
    | `redeem(run_id, token, *, ttl_s: float) -> RunPaths` | single use; status becomes `applying`; `TokenError` otherwise |
    | `finish(run, status)` | records the terminal status |
    | `set_request(session_id, text)` / `get_request(session_id) -> str \| None` | latest user request per session |
    | `ledger(session_id, root: str) -> set[str]` / `ledger_add(session_id, root: str, paths)` | paths created this session |
    | `log_decision(entry: dict)` | append-only JSONL, rotated at 50 MB |
    | `remove_run(run)` | deletes one run dir |
    | `cleanup(*, ttl_s: float, now: float \| None = None) -> list[str]` | deletes expired runs |

  - `safe_rmtree(path: Path, within: Path)`: never follows symlinks, and makes mode-0 dirs removable first

- [ ] **Step 1: Write the failing test**

`tests/unit/test_store.py`:
```python
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from dryrun.store import Store, TokenError, safe_rmtree


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state")


def test_new_run_is_private(store: Store):
    run = store.new_run()
    assert stat.S_IMODE(os.stat(run.root).st_mode) == 0o700
    assert run.ws_up.is_dir() and run.tmp_lower.is_dir()
    assert store.run(run.run_id) == run


def test_run_id_validation(store: Store):
    for bad in ["../x", "abc", "12-zz", "12-abcd1234/.."]:
        with pytest.raises(TokenError):
            store.run(bad)


def test_redeem_is_single_use(store: Store):
    run = store.new_run()
    token = store.authorize(run, session_id="s", decision="allow")
    assert store.load_meta(run)["status"] == "authorized"
    assert store.redeem(run.run_id, token, ttl_s=60) == run
    with pytest.raises(TokenError, match="applying"):
        store.redeem(run.run_id, token, ttl_s=60)


def test_redeem_rejects_bad_token_and_expired(store: Store):
    run = store.new_run()
    token = store.authorize(run, session_id="s", decision="ask")
    assert store.load_meta(run)["status"] == "pending"
    with pytest.raises(TokenError, match="bad token"):
        store.redeem(run.run_id, "nope", ttl_s=60)
    store.save_meta(run, created=time.time() - 3600)
    with pytest.raises(TokenError, match="expired"):
        store.redeem(run.run_id, token, ttl_s=60)


def test_sessions_request_and_ledger(store: Store):
    store.set_request("abc-123", "clean the build dir")
    assert store.get_request("abc-123") == "clean the build dir"
    assert store.get_request("other") is None
    store.set_request("../../weird id", "x")
    assert store.get_request("../../weird id") == "x"
    assert not any(p.name.startswith("..") for p in store.sessions_dir.iterdir())
    store.ledger_add("abc-123", "/w", ["gen/a.txt", "gen"])
    store.ledger_add("abc-123", "/w", ["b.txt"])
    assert store.ledger("abc-123", "/w") == {"gen/a.txt", "gen", "b.txt"}
    assert store.ledger("abc-123", "/other") == set()


def test_log_decision_appends_jsonl(store: Store):
    store.log_decision({"run_id": "r1", "decision": "allow"})
    store.log_decision({"run_id": "r2", "decision": "ask"})
    lines = store.log_path.read_text().splitlines()
    assert [json.loads(x)["run_id"] for x in lines] == ["r1", "r2"]


def test_cleanup_removes_expired_keeps_fresh(store: Store):
    old = store.new_run()
    store.authorize(old, session_id="s", decision="ask")
    store.save_meta(old, created=time.time() - 3600)
    fresh = store.new_run()
    store.authorize(fresh, session_id="s", decision="ask")
    removed = store.cleanup(ttl_s=900)
    assert removed == [old.run_id]
    assert fresh.root.exists() and not old.root.exists()


def test_safe_rmtree_handles_mode0_and_never_follows_symlinks(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    os.chmod(outside, 0o555)
    runs = tmp_path / "runs"
    victim = runs / "r"
    (victim / "locked" / "inner").mkdir(parents=True)
    (victim / "locked" / "inner" / "f").write_text("f")
    (victim / "link").symlink_to(outside)
    os.chmod(victim / "locked" / "inner", 0)
    os.chmod(victim / "locked", 0)
    safe_rmtree(victim, within=runs)
    assert not victim.exists()
    assert (outside / "keep").exists() and stat.S_IMODE(os.stat(outside).st_mode) == 0o555
    os.chmod(outside, 0o755)
    with pytest.raises(PermissionError):
        safe_rmtree(outside, within=runs)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/unit/test_store.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.store'`)

- [ ] **Step 3: Implement**

`src/dryrun/store.py`:
```python
"""Dry Run's private state: runs, sessions (latest request + ledger), single-use tokens, decision log.
Everything lives under a 0700 directory owned by the user; nothing is ever sent anywhere (F15)."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from dryrun.paths import ensure_private_dir
from dryrun.runpaths import RunPaths

LOG_ROTATE_BYTES = 50 * 1024**2
LEDGER_CAP = 20_000
_RUN_ID = re.compile(r"^[0-9a-f]{6,16}-[0-9a-f]{8}$")
_SESSION = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
TERMINAL = {"committed", "denied", "discarded", "expired", "rerun", "failed", "conflict", "passthrough"}


class TokenError(Exception):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def safe_rmtree(path: Path, within: Path) -> None:
    path, within = Path(path), Path(within)
    if path.parent != within:
        raise PermissionError(f"refusing to delete {path}: not directly inside {within}")
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"refusing to delete {path}: not a real directory")
    os.chmod(path, 0o700)
    for dirpath, dirnames, _ in os.walk(path, topdown=True, followlinks=False):
        for name in list(dirnames):
            child = os.path.join(dirpath, name)
            cst = os.lstat(child)
            if stat.S_ISDIR(cst.st_mode):
                os.chmod(child, 0o700)
            else:
                dirnames.remove(name)  # symlink to a dir: never descend or chmod through it
    shutil.rmtree(path)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = ensure_private_dir(Path(root))
        self.runs_dir = ensure_private_dir(self.root / "runs")
        self.sessions_dir = ensure_private_dir(self.root / "sessions")
        self.journal_dir = ensure_private_dir(self.root / "journal")
        self.log_path = self.root / "decisions.jsonl"

    # --- runs ------------------------------------------------------------------------------------
    def new_run(self) -> RunPaths:
        run_id = f"{int(time.time()):x}-{secrets.token_hex(4)}"
        return RunPaths(self.runs_dir / run_id).create()

    def run(self, run_id: str) -> RunPaths:
        if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
            raise TokenError(f"bad run id: {run_id!r}")
        return RunPaths(self.runs_dir / run_id)

    def save_meta(self, run: RunPaths, **fields) -> None:
        meta = _read_json(run.meta) or {}
        meta.update(fields)
        _write_json(run.meta, meta)

    def load_meta(self, run: RunPaths) -> dict:
        return _read_json(run.meta) or {}

    @contextmanager
    def _locked(self, path: Path):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def authorize(self, run: RunPaths, *, session_id: str, decision: str) -> str:
        token = secrets.token_urlsafe(24)
        self.save_meta(run, token_sha256=_sha(token), session_id=session_id, decision=decision,
                       status="authorized" if decision == "allow" else "pending", created=time.time())
        return token

    def redeem(self, run_id: str, token: str, *, ttl_s: float) -> RunPaths:
        run = self.run(run_id)
        if not run.root.is_dir():
            raise TokenError("unknown run")
        with self._locked(run.root / "lock"):
            meta = self.load_meta(run)
            status = meta.get("status")
            if status not in ("authorized", "pending"):
                raise TokenError(f"run is {status}")
            if not hmac.compare_digest(str(meta.get("token_sha256", "")), _sha(str(token))):
                raise TokenError("bad token")
            if time.time() - float(meta.get("created", 0)) > ttl_s:
                self.save_meta(run, status="expired")
                raise TokenError("expired")
            self.save_meta(run, status="applying", redeemed=time.time())
        return run

    def finish(self, run: RunPaths, status: str) -> None:
        self.save_meta(run, status=status, finished=time.time())

    def remove_run(self, run: RunPaths) -> None:
        if run.root.exists():
            safe_rmtree(run.root, within=self.runs_dir)

    def cleanup(self, *, ttl_s: float, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        removed = []
        for entry in sorted(self.runs_dir.iterdir()):
            if not _RUN_ID.match(entry.name):
                continue
            run = RunPaths(entry)
            meta = self.load_meta(run)
            created = float(meta.get("created") or os.lstat(entry).st_mtime)
            status = meta.get("status")
            age = now - created
            if status == "applying" and age < 3600:
                continue
            if status in TERMINAL or age > ttl_s:
                self.remove_run(run)
                removed.append(entry.name)
        return removed

    # --- sessions --------------------------------------------------------------------------------
    def _session_file(self, session_id: str) -> Path:
        name = session_id if _SESSION.match(session_id) and not session_id.startswith(".") else _sha(session_id)[:32]
        return self.sessions_dir / f"{name}.json"

    def set_request(self, session_id: str, text: str) -> None:
        path = self._session_file(session_id)
        with self._locked(path.with_suffix(".lock")):
            data = _read_json(path) or {}
            data.update(request=text[:8000], updated=time.time())
            _write_json(path, data)

    def get_request(self, session_id: str) -> str | None:
        data = _read_json(self._session_file(session_id))
        return data.get("request") if data else None

    def ledger(self, session_id: str, root: str) -> set[str]:
        data = _read_json(self._session_file(session_id)) or {}
        return set(data.get("ledger", {}).get(root, []))

    def ledger_add(self, session_id: str, root: str, paths: Iterable[str]) -> None:
        path = self._session_file(session_id)
        with self._locked(path.with_suffix(".lock")):
            data = _read_json(path) or {}
            ledger = data.setdefault("ledger", {})
            merged = sorted(set(ledger.get(root, [])) | set(paths))
            ledger[root] = merged[-LEDGER_CAP:]
            _write_json(path, data)

    # --- decision log ----------------------------------------------------------------------------
    def log_decision(self, entry: dict) -> None:
        try:
            if self.log_path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(self.log_path, self.log_path.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/unit/test_store.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/store.py tests/unit/test_store.py
git commit -m "feat: private state store with single-use tokens, sessions, ledger and decision log"
```
