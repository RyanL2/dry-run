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
        if run.root.exists():
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
