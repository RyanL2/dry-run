### Task 11: Run paths, run assembly, and EffectRecord builder

**Files:**
- Create: `src/dryrun/runpaths.py`, `src/dryrun/sandbox/assemble.py`, `src/dryrun/effects/record.py`, `tests/unit/test_assemble.py`, `tests/unit/test_record.py`, `tests/sandbox/test_assemble_real.py`

**Interfaces:**
- Consumes:
  - `Config`/`Policy` (Task 1); `Fp`, `fingerprint_tree` (Task 3); `SandboxSpec`, `OverlaySpec`, `existing` (Task 5)
  - `snapshot_tmp`, `TmpSnapshot`, `make_decoys`, `new_token`, `scan` (Task 6)
  - `SpawnResult` (Task 7); `TraceSummary` (Task 8); `AreaEffect` (Task 9)
  - `GitSnapshot`, `recoverability`, `blob_sha1`, `read_refs`, `is_ancestor` (Task 10)
  - `EffectRecord`, `FsEntry`, `RefChange` (Task 2)
- Produces:
  - `RunPaths(root: Path)`
    - properties: `ws_up`, `ws_wk`, `tmp_lower`, `tmp_up`, `tmp_wk`, `cache`, `decoys`, `stdout`, `stderr`, `trace`, `record`, `changeset`, `meta`
    - `.create()` makes the dirs with mode 0700
    - `cache_up(i)`, `cache_wk(i)`
  - `Prepared(spec: SandboxSpec, ws_base: dict[str, Fp], tmp: TmpSnapshot, token: str, caches: list[tuple[Path, Path]], tmp_skip: frozenset[str])`
  - `filter_env(env: dict[str, str], denylist) -> dict[str, str]`
  - `prepare(run: RunPaths, *, ws_root: Path, cwd: Path, command: str, env: dict[str, str], cfg: Config, home: Path, state: Path, bwrap: str) -> Prepared`
  - `annotate(entries, snap, policy, ledger: set[str], contents: dict[str, Path], blob_exists=lambda shas: set()) -> None`: fills `git`, `build_output`, `in_ledger` and `discards_work` in place
  - `git_effects(ws_root: Path, run: RunPaths, ws_entries: list[FsEntry]) -> dict` with keys `refs_changed: list[RefChange]`, `index_changed: bool`, `internals_touched: list[str]` and `objects_deleted: int`
  - `compute_flags(*, res, trace, ws_eff, tmp_eff, lower_changed: bool, tmp_partial: bool, output: bytes) -> list[str]`
  - `tail(path: Path, n: int = 4096) -> str`
  - `cache_counts(caches) -> tuple[int, int]`
  - `build_record(**fields) -> EffectRecord` (keyword-only; see the code)

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_assemble.py`:
```python
from __future__ import annotations

from dryrun.sandbox.assemble import filter_env

DENY = ("*TOKEN*", "*SECRET*", "*KEY*", "AWS_*")


def test_filter_env_drops_secrets_and_host_sockets():
    env = {"PATH": "/bin", "HOME": "/h", "GITHUB_TOKEN": "x", "AWS_REGION": "r", "MY_API_KEY": "k",
           "SSH_AUTH_SOCK": "/s", "DBUS_SESSION_BUS_ADDRESS": "d", "WSL_INTEROP": "w", "DISPLAY": ":0",
           "XDG_RUNTIME_DIR": "/run/user/1", "EDITOR": "vim"}
    out = filter_env(env, DENY)
    assert out["PATH"] == "/bin" and out["HOME"] == "/h" and out["EDITOR"] == "vim"
    for gone in ["GITHUB_TOKEN", "AWS_REGION", "MY_API_KEY", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS",
                 "WSL_INTEROP", "DISPLAY", "XDG_RUNTIME_DIR"]:
        assert gone not in out
    assert out["LC_MESSAGES"] == "C"


def test_filter_env_supplies_defaults():
    out = filter_env({}, DENY)
    assert out["PATH"].startswith("/usr/local/bin")
```

`tests/unit/test_record.py`:
```python
from __future__ import annotations

import hashlib
from pathlib import Path

from dryrun.config import Policy
from dryrun.effects.gitstate import GitSnapshot
from dryrun.effects.record import annotate, compute_flags, git_effects
from dryrun.effects.upper import AreaEffect
from dryrun.runpaths import RunPaths
from dryrun.sandbox.spawn import SpawnResult
from dryrun.sandbox.trace import TraceSummary
from dryrun.types import FsEntry


def sha1_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_annotate_recoverability_build_output_ledger_and_discard(tmp_path: Path):
    restored = tmp_path / "restored.py"
    restored.write_bytes(b"v1\n")
    snap = GitSnapshot(is_repo=True, status={"dirty.py": "tracked_dirty", "new.py": "untracked"},
                       ignored_dirs=("dist",), tracked=frozenset({"clean.py", "dirty.py"}),
                       head_blobs={"dirty.py": sha1_blob(b"v1\n")}, index_blobs={})
    entries = [
        FsEntry(op="delete", path="clean.py", kind="file", preexisting=True),
        FsEntry(op="modify", path="dirty.py", kind="file", preexisting=True),
        FsEntry(op="delete", path="new.py", kind="file", preexisting=True),
        FsEntry(op="delete", path="build/x.o", kind="file", preexisting=True),
        FsEntry(op="delete", path="dist/app.js", kind="file", preexisting=True),
        FsEntry(op="delete", path="gen/out.txt", kind="file", preexisting=True),
    ]
    annotate(entries, snap, Policy(), ledger={"gen"}, contents={"dirty.py": restored})
    e = {x.path: x for x in entries}
    assert e["clean.py"].git == "tracked_clean" and not e["clean.py"].build_output
    assert e["dirty.py"].discards_work
    assert e["new.py"].git == "untracked"
    assert e["build/x.o"].build_output
    assert e["dist/app.js"].git == "ignored" and e["dist/app.js"].build_output
    assert e["gen/out.txt"].in_ledger


def test_compute_flags(tmp_path: Path):
    res = SpawnResult(exit_code=None, wall_ms=10, timed_out=True, killed_reason="timeout",
                      stdout_path=tmp_path / "o", stderr_path=tmp_path / "e", trace_path=tmp_path / "t")
    flags = compute_flags(res=res, trace=TraceSummary(net=[{"kind": "dns", "target": "x:53"}]),
                          ws_eff=AreaEffect(refused=[{"path": "h", "reason": "hardlink_in_shadow"}]),
                          tmp_eff=AreaEffect(), lower_changed=True, tmp_partial=True,
                          output=b"touch: cannot touch '/home/u/x': Read-only file system\n")
    assert set(flags) == {"timeout", "incomplete_network", "unsupported_entry", "lower_changed",
                          "tmp_partial", "ro_write_blocked"}


def test_git_effects_classifies_internals_and_refs(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".git" / "refs" / "heads").mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (ws / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    run = RunPaths(tmp_path / "run")
    run.create()
    (run.ws_up / ".git" / "refs" / "heads").mkdir(parents=True)
    (run.ws_up / ".git" / "refs" / "heads" / "topic").write_text("b" * 40 + "\n")
    entries = [
        FsEntry(op="modify", path=".git/index", kind="file", preexisting=True),
        FsEntry(op="create", path=".git/objects/ab/cdef", kind="file", preexisting=False),
        FsEntry(op="delete", path=".git/objects/pack/p.pack", kind="file", preexisting=True),
        FsEntry(op="create", path=".git/refs/heads/topic", kind="file", preexisting=False),
        FsEntry(op="modify", path=".git/config", kind="file", preexisting=True),
        FsEntry(op="create", path=".git/modules/x", kind="file", preexisting=False),
    ]
    g = git_effects(ws, run, entries)
    assert g["index_changed"]
    assert g["objects_deleted"] == 1
    assert g["internals_touched"] == [".git/modules/x"]
    assert [(r.ref, r.change) for r in g["refs_changed"]] == [("refs/heads/topic", "created")]
```

`tests/sandbox/test_assemble_real.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from dryrun.config import load_config
from dryrun.paths import bwrap_path
from dryrun.runpaths import RunPaths
from dryrun.sandbox.assemble import prepare

pytestmark = pytest.mark.sandbox


def test_prepare_builds_overlays_hides_and_decoys(scratch: Path):
    home = scratch / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("REAL")
    (home / ".cache").mkdir()
    ws = scratch / "ws"
    ws.mkdir()
    state = scratch / "state"
    run = RunPaths(state / "runs" / "r1")
    p = prepare(run, ws_root=ws, cwd=ws, command="true", env={"PATH": "/bin", "GH_TOKEN": "x"},
                cfg=load_config(use_user_file=False), home=home, state=state, bwrap=str(bwrap_path()))
    targets = [o.target for o in p.spec.overlays]
    assert targets[:2] == ["/tmp", str(ws)]
    assert str(home / ".cache") in targets
    assert str(state) in p.spec.hide_late
    assert [dst for _, dst in p.spec.ro_binds] == [str(home / ".ssh")]
    assert "GH_TOKEN" not in p.spec.env
    assert p.spec.argv == ("bash", "-c", "true")
    assert p.token.startswith("DRT")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_assemble.py tests/unit/test_record.py tests/sandbox/test_assemble_real.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

`src/dryrun/runpaths.py`:
```python
"""Layout of one run directory under <state>/runs/<run_id> (always on the workspace's filesystem)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def run_id(self) -> str:
        return self.root.name

    ws_up = property(lambda self: self.root / "ws.up")
    ws_wk = property(lambda self: self.root / "ws.wk")
    tmp_lower = property(lambda self: self.root / "tmp.lower")
    tmp_up = property(lambda self: self.root / "tmp.up")
    tmp_wk = property(lambda self: self.root / "tmp.wk")
    cache = property(lambda self: self.root / "cache")
    decoys = property(lambda self: self.root / "decoys")
    stdout = property(lambda self: self.root / "stdout")
    stderr = property(lambda self: self.root / "stderr")
    trace = property(lambda self: self.root / "trace")
    record = property(lambda self: self.root / "record.json")
    changeset = property(lambda self: self.root / "changeset.json")
    meta = property(lambda self: self.root / "meta.json")

    def cache_up(self, i: int) -> Path:
        return self.cache / f"{i}.up"

    def cache_wk(self, i: int) -> Path:
        return self.cache / f"{i}.wk"

    def create(self) -> "RunPaths":
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for d in (self.ws_up, self.ws_wk, self.tmp_lower, self.tmp_up, self.tmp_wk, self.cache, self.decoys):
            d.mkdir(exist_ok=True, mode=0o700)
        return self
```

`src/dryrun/sandbox/assemble.py`:
```python
"""Per-run sandbox assembly shared by the pipeline and the isolation canaries (so canaries test the
exact production layout)."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from dryrun.config import Config
from dryrun.fingerprint import Fp, fingerprint_tree
from dryrun.runpaths import RunPaths
from dryrun.sandbox.decoys import make_decoys, new_token
from dryrun.sandbox.layout import OverlaySpec, SandboxSpec, existing
from dryrun.sandbox.tmpsnap import TmpSnapshot, snapshot_tmp

HOST_ENV_DROP = {"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "WSL_INTEROP", "DISPLAY", "WAYLAND_DISPLAY",
                 "XDG_RUNTIME_DIR", "GPG_AGENT_INFO", "SSH_AGENT_PID", "PULSE_SERVER"}
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass
class Prepared:
    spec: SandboxSpec
    ws_base: dict[str, Fp]
    tmp: TmpSnapshot
    token: str
    caches: list[tuple[Path, Path]] = field(default_factory=list)  # (real dir, upper dir)
    tmp_skip: frozenset[str] = frozenset()


def filter_env(env: dict[str, str], denylist) -> dict[str, str]:
    out = {}
    for key, value in env.items():
        if key in HOST_ENV_DROP or any(fnmatch.fnmatchcase(key, pat) for pat in denylist):
            continue
        out[key] = value
    out.setdefault("PATH", DEFAULT_PATH)
    out["LC_MESSAGES"] = "C"  # English errors so EROFS can be detected (ro_write_blocked)
    out.pop("LANGUAGE", None)
    return out


def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).relative_to(parent)
        return True
    except ValueError:
        return False


def prepare(run: RunPaths, *, ws_root: Path, cwd: Path, command: str, env: dict[str, str], cfg: Config,
            home: Path, state: Path, bwrap: str) -> Prepared:
    run.create()
    ws_root, home = Path(ws_root), Path(home)
    ws_base = fingerprint_tree(ws_root)
    s = cfg.shadow
    tmp_root = Path("/tmp")
    exclude = ws_root if _inside(ws_root, tmp_root) else None
    tmp = snapshot_tmp(run.tmp_lower, tmp_root, max_entries=s.tmp_max_entries, max_total=s.tmp_max_total,
                       max_file=s.tmp_max_file, exclude=exclude)
    tmp_skip = frozenset({str(ws_root.relative_to(tmp_root))}) if exclude else frozenset()
    token = new_token()
    decoys = make_decoys(run.decoys, home, cfg.policy.secret_paths, token)
    overlays = [OverlaySpec(str(run.tmp_lower), str(run.tmp_up), str(run.tmp_wk), "/tmp"),
                OverlaySpec(str(ws_root), str(run.ws_up), str(run.ws_wk), str(ws_root))]
    caches: list[tuple[Path, Path]] = []
    for i, rel in enumerate(cfg.policy.home_cache_dirs):
        real = home / rel
        if (not real.is_dir() or real.is_symlink() or _inside(real, ws_root) or _inside(ws_root, real)
                or _inside(Path(state), real)):
            continue
        up, wk = run.cache_up(i), run.cache_wk(i)
        up.mkdir(mode=0o700)
        wk.mkdir(mode=0o700)
        overlays.append(OverlaySpec(str(real), str(up), str(wk), str(real)))
        caches.append((real, up))
    spec = SandboxSpec(
        bwrap=bwrap,
        overlays=tuple(overlays),
        hide_early=existing(["/run", "/mnt/wsl", "/mnt/wslg"]),
        hide_late=existing([str(state), str(home / ".claude")]),
        ro_binds=tuple(decoys),
        env=filter_env(env, cfg.policy.env_denylist),
        cwd=str(cwd),
        argv=("bash", "-c", command),
    )
    return Prepared(spec=spec, ws_base=ws_base, tmp=tmp, token=token, caches=caches, tmp_skip=tmp_skip)
```

`src/dryrun/effects/record.py`:
```python
"""Assemble the EffectRecord from the raw run artifacts."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from dryrun.config import Policy
from dryrun.effects.gitstate import GitSnapshot, blob_sha1, is_ancestor, read_refs, recoverability
from dryrun.effects.upper import AreaEffect
from dryrun.runpaths import RunPaths
from dryrun.sandbox.spawn import SpawnResult
from dryrun.sandbox.trace import TraceSummary
from dryrun.types import EffectRecord, FsEntry, RefChange

EROFS_MARKER = b"Read-only file system"
GIT_ALLOWED = ("index", "ORIG_HEAD", "FETCH_HEAD", "HEAD", "COMMIT_EDITMSG", "MERGE_HEAD", "MERGE_MSG",
               "MERGE_MODE", "AUTO_MERGE", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_*", "packed-refs",
               "logs", "logs/*", "objects", "objects/*", "refs", "refs/*", "rebase-merge", "rebase-merge/*",
               "rebase-apply", "rebase-apply/*", "sequencer", "sequencer/*", "info", "info/exclude",
               "description", "gc.log", "worktrees", "worktrees/*", "index.lock", "*.lock")
GIT_H9 = ("hooks", "hooks/*", "config", "info/attributes")  # judged by harm-policy H9, not H3
MAX_ANCESTRY_CHECKS = 20


def annotate(entries: list[FsEntry], snap: GitSnapshot, policy: Policy, ledger: set[str],
             contents: dict[str, Path], blob_exists=lambda shas: set()) -> None:
    """blob_exists(shas) -> subset already in the repo's object store (gitstate.objects_exist).
    A tracked_dirty file rewritten to ANY known blob (HEAD, index or an older commit, as with
    `git reset --hard HEAD~1` or `git checkout -- f`) means its uncommitted work was discarded."""
    build = set(policy.build_output_dirs)
    restored: dict[str, str] = {}
    for e in entries:
        e.git = recoverability(snap, e.path)
        parts = e.path.split("/")
        dirs = parts if e.kind == "dir" else parts[:-1]
        e.build_output = e.git == "ignored" or any(p in build for p in dirs)
        e.in_ledger = any(e.path == p or e.path.startswith(p + "/") for p in ledger)
        if e.op == "modify" and e.git == "tracked_dirty" and e.path in contents:
            restored[e.path] = blob_sha1(contents[e.path])
    if not restored:
        return
    known = {v for p in restored for v in (snap.head_blobs.get(p), snap.index_blobs.get(p)) if v}
    unknown = set(restored.values()) - known
    known |= blob_exists(unknown) if unknown else set()
    for e in entries:
        if e.path in restored:
            e.discards_work = restored[e.path] in known


def git_effects(ws_root: Path, run: RunPaths, ws_entries: list[FsEntry]) -> dict:
    internals, objects_deleted, index_changed = [], 0, False
    for e in ws_entries:
        if not e.path.startswith(".git/"):
            continue
        inner = e.path[len(".git/"):]
        if inner == "index":
            index_changed = True
        if inner.startswith("objects/") and e.op == "delete" and e.kind == "file":
            objects_deleted += 1
        if any(fnmatch.fnmatchcase(inner, p) for p in GIT_ALLOWED + GIT_H9):
            continue
        internals.append(e.path)
    git_dir = Path(ws_root) / ".git"
    before = read_refs(git_dir)
    up_git = run.ws_up / ".git"
    after = read_refs(git_dir, up_git if up_git.is_dir() else None)
    objects = up_git / "objects" if (up_git / "objects").is_dir() else None
    changes: list[RefChange] = []
    checks = 0
    for ref in sorted(set(before) | set(after)):
        b, a = before.get(ref), after.get(ref)
        if b == a:
            continue
        if b is None:
            kind = "created"
        elif a is None:
            kind = "deleted"
        elif checks < MAX_ANCESTRY_CHECKS:
            checks += 1
            anc = is_ancestor(Path(ws_root), b, a, objects)
            kind = "fast_forward" if anc else ("non_fast_forward" if anc is False else "unknown")
        else:
            kind = "unknown"
        changes.append(RefChange(ref=ref, before=b, after=a, change=kind))
    return {"refs_changed": changes, "index_changed": index_changed,
            "internals_touched": internals, "objects_deleted": objects_deleted}


def compute_flags(*, res: SpawnResult | None, trace: TraceSummary, ws_eff: AreaEffect, tmp_eff: AreaEffect,
                  lower_changed: bool, tmp_partial: bool, output: bytes) -> list[str]:
    flags = []
    if res is not None and res.timed_out:
        flags.append("timeout")
    if res is not None and res.killed_reason in ("disk", "killed"):
        flags.append("resource_limit")
    if trace.net:
        flags.append("incomplete_network")
    if ws_eff.refused or tmp_eff.refused:
        flags.append("unsupported_entry")
    if lower_changed:
        flags.append("lower_changed")
    if EROFS_MARKER in output:
        flags.append("ro_write_blocked")
    if tmp_partial:
        flags.append("tmp_partial")
    return flags


def tail(path: Path, n: int = 4096) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def cache_counts(caches: list[tuple[Path, Path]]) -> tuple[int, int]:
    files = size = 0
    for _, up in caches:
        for dirpath, _, names in os.walk(up):
            for name in names:
                try:
                    size += os.lstat(os.path.join(dirpath, name)).st_size
                    files += 1
                except OSError:
                    pass
    return files, size


def build_record(*, run_id: str, session_id: str, command: str, cwd: str, ws_root: str,
                 request_text: str | None, request_source: str | None, triage_class: str, triage_reason: str,
                 res: SpawnResult | None, ws_eff: AreaEffect, tmp_eff: AreaEffect, cache_files: int,
                 cache_bytes: int, git_is_repo: bool, git: dict, trace: TraceSummary, decoy_hits: list[dict],
                 flags: list[str]) -> EffectRecord:
    return EffectRecord(
        run_id=run_id, session_id=session_id, command=command, cwd=cwd, workspace_root=ws_root,
        request_text=request_text, request_source=request_source,
        triage_class=triage_class, triage_reason=triage_reason,
        exit_code=res.exit_code if res else None, wall_ms=res.wall_ms if res else 0,
        timed_out=res.timed_out if res else False,
        stdout_tail=tail(res.stdout_path) if res else "", stderr_tail=tail(res.stderr_path) if res else "",
        workspace=ws_eff.entries, tmp=tmp_eff.entries, home_cache_files=cache_files,
        home_cache_bytes=cache_bytes, git_is_repo=git_is_repo,
        refs_changed=git.get("refs_changed", []), index_changed=git.get("index_changed", False),
        internals_touched=git.get("internals_touched", []), objects_deleted=git.get("objects_deleted", 0),
        net=list(trace.net), proc_count=trace.pids, execs=list(trace.execs),
        decoy_hits=decoy_hits, flags=flags,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_assemble.py tests/unit/test_record.py tests/sandbox/test_assemble_real.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/runpaths.py src/dryrun/sandbox/assemble.py src/dryrun/effects/record.py tests/unit/test_assemble.py tests/unit/test_record.py tests/sandbox/test_assemble_real.py
git commit -m "feat: run layout assembly and effect record builder"
```
