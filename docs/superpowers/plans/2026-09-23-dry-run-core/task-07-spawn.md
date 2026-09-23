### Task 7: The launcher, `sandbox.spawn` (S2, S3, S7, S10; I8–I11, I15)

**Files:**
- Create: `src/dryrun/sandbox/spawn.py`, `tests/sandbox/__init__.py` (empty), `tests/sandbox/conftest.py`, `tests/sandbox/test_spawn.py`, `tests/unit/test_static_isolation.py`

**Interfaces:**
- Consumes:
  - `ShadowConfig` (Task 1), `bwrap_path()`, `state_dir()`, `ensure_private_dir()` (Task 1)
  - `SandboxSpec`, `OverlaySpec`, `bwrap_argv`, `existing` (Task 5)
  - `build_filter` (Task 4)
- Produces:
  - `SandboxError(RuntimeError)`
  - `SpawnResult(exit_code: int | None, wall_ms: int, timed_out: bool, killed_reason: str | None, stdout_path: Path, stderr_path: Path, trace_path: Path)`
    - `killed_reason` ∈ `None|"timeout"|"disk"|"cancelled"|"killed"`
  - `preflight(cfg: ShadowConfig, *, allow_root=False, bwrap: Path | None = None) -> list[str]`: an empty list means OK
  - `run_shadow(spec: SandboxSpec, *, run_id: str, out_dir: Path, cfg: ShadowConfig, watch_fs: Path, cancel: threading.Event | None = None) -> SpawnResult`
    - Raises `SandboxError` if the sandbox never started the command.
    - The spec's `seccomp_fd` is overridden to 9 (a wrapper opens the filter file on fd 9).
  - `run_readonly(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 20.0) -> subprocess.CompletedProcess` (bytes stdout/stderr)
  - `launcher_env() -> dict[str, str]`

**Launch chain** for `run_shadow`:

| Layer | Purpose |
|---|---|
| `systemd-run --user --scope --unit dryrun-<id> -p MemoryMax -p MemorySwapMax=0 -p TasksMax` | hard memory and process limits |
| `prlimit --fsize --core=0` | per-file size cap, no core dumps |
| `nice -n 19 ionice -c 3` | CPU/IO de-prioritisation |
| `sh -c 'echo 1000 > /proc/self/oom_score_adj && exec 9<"$0" && exec "$@"' <filter>` | OOM killer picks the shadow first; filter opened on fd 9 |
| `strace -f -qq --seccomp-bpf -e trace=execve,execveat,connect,sendto,sendmsg -o <trace>` | runs outside bwrap |
| `bwrap … --seccomp 9 -- <argv>` | the sandbox itself |

**Watchdog** (polls every 50 ms):
- kills on the wall clock;
- kills when disk growth exceeds `disk_budget`, or free space drops below the floor;
- kills on cancel.

A kill is `systemctl --user kill --signal=SIGKILL dryrun-<id>.scope` plus `killpg`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_static_isolation.py`:
```python
"""S2: only the launcher may start processes for user commands. cli.py (installer: systemctl) and
canary.py (isolation harness: fixed argv, host-side target processes) start trusted fixed commands."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "dryrun"
ALLOWED = {"sandbox/spawn.py", "cli.py", "canary.py"}
PATTERN = re.compile(r"\bsubprocess\b|os\.exec\w*\(|os\.spawn\w*\(|os\.posix_spawn|os\.system\(|os\.popen\(")


def test_only_the_launcher_starts_processes():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel not in ALLOWED and PATTERN.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == []
```

`tests/sandbox/conftest.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from dryrun.config import load_config, with_shadow
from dryrun.paths import bwrap_path
from dryrun.sandbox.layout import OverlaySpec, SandboxSpec, existing


@pytest.fixture
def shadow_cfg():
    return with_shadow(load_config(use_user_file=False), wall_clock_s=10, memory_max=512 * 1024**2,
                       tasks_max=128, disk_budget=256 * 1024**2).shadow


@pytest.fixture
def ws(scratch: Path) -> Path:
    w = scratch / "ws"
    w.mkdir()
    (w / "keep.txt").write_text("keep\n")
    return w


@pytest.fixture
def run_dir(scratch: Path) -> Path:
    r = scratch / "state" / "runs" / "r1"
    for sub in ("ws.up", "ws.wk"):
        (r / sub).mkdir(parents=True)
    return r


def simple_spec(ws: Path, run_dir: Path, command: str) -> SandboxSpec:
    return SandboxSpec(
        bwrap=str(bwrap_path()),
        overlays=(OverlaySpec(str(ws), str(run_dir / "ws.up"), str(run_dir / "ws.wk"), str(ws)),),
        hide_early=existing(["/run", "/mnt/wsl", "/mnt/wslg"]),
        hide_late=(str(run_dir.parent.parent),),
        ro_binds=(),
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},
        cwd=str(ws),
        argv=("bash", "-c", command),
    )
```

`tests/sandbox/test_spawn.py`:
```python
from __future__ import annotations

import os
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from dryrun.sandbox.layout import OverlaySpec
from dryrun.sandbox.spawn import SandboxError, preflight, run_readonly, run_shadow
from tests.sandbox.conftest import simple_spec

pytestmark = pytest.mark.sandbox


def shadow(ws, run_dir, cfg, cmd, **kw):
    return run_shadow(simple_spec(ws, run_dir, cmd), run_id="t" + os.urandom(3).hex(), out_dir=run_dir,
                      cfg=cfg, watch_fs=run_dir, **kw)


def test_preflight_is_clean_in_dev_env(shadow_cfg):
    assert preflight(shadow_cfg) == []


def test_runs_command_and_captures_output_exit_and_trace(ws, run_dir, shadow_cfg):
    res = shadow(ws, run_dir, shadow_cfg, "echo hello; echo oops >&2; exit 7")
    assert res.exit_code == 7 and not res.timed_out and res.killed_reason is None
    assert res.stdout_path.read_text() == "hello\n"
    assert "oops" in res.stderr_path.read_text()
    assert "execve(" in res.trace_path.read_text()


def test_writes_land_in_upper_not_in_real_workspace(ws, run_dir, shadow_cfg):
    res = shadow(ws, run_dir, shadow_cfg, "echo new > new.txt && rm keep.txt")
    assert res.exit_code == 0
    assert (ws / "keep.txt").read_text() == "keep\n"
    assert not (ws / "new.txt").exists()
    assert (run_dir / "ws.up" / "new.txt").read_text() == "new\n"
    st = os.lstat(run_dir / "ws.up" / "keep.txt")
    assert stat.S_ISCHR(st.st_mode) and st.st_rdev == 0  # whiteout


def test_timeout_kills_whole_tree(ws, run_dir, shadow_cfg):
    cfg = replace(shadow_cfg, wall_clock_s=1)
    t0 = time.monotonic()
    res = shadow(ws, run_dir, cfg, "setsid sleep 31.25 & sleep 31.25")
    assert res.timed_out and res.killed_reason == "timeout" and res.exit_code is None
    assert time.monotonic() - t0 < 8
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep 31.25"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


def test_cancel_event_stops_run(ws, run_dir, shadow_cfg):
    import threading
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    res = shadow(ws, run_dir, shadow_cfg, "sleep 20", cancel=ev)
    assert res.killed_reason == "cancelled"


def test_setup_failure_raises_sandbox_error(ws, run_dir, shadow_cfg):
    spec = simple_spec(ws, run_dir, "true")
    bad = replace(spec, overlays=(OverlaySpec("/nonexistent-lower", spec.overlays[0].upper,
                                              spec.overlays[0].work, str(ws)),))
    with pytest.raises(SandboxError):
        run_shadow(bad, run_id="bad1", out_dir=run_dir, cfg=shadow_cfg, watch_fs=run_dir)


def test_run_readonly_cannot_write_and_returns_output(ws):
    ok = run_readonly(["bash", "-c", "echo ok; touch x"], cwd=ws)
    assert ok.stdout == b"ok\n"
    assert ok.returncode != 0
    assert not (ws / "x").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_static_isolation.py tests/sandbox/test_spawn.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.sandbox.spawn'`). The static test passes vacuously until spawn exists; that's fine.

- [ ] **Step 3: Implement**

`tests/sandbox/__init__.py` and `tests/__init__.py`: empty files (so `from tests.sandbox.conftest import simple_spec` works).

`src/dryrun/sandbox/spawn.py`:
```python
"""The one place Dry Run starts processes for user commands (spec S2).

run_shadow: systemd user scope (MemoryMax, TasksMax) -> prlimit (fsize, core) -> nice/ionice ->
oom_score_adj=1000 -> strace (outside bwrap, so the in-sandbox seccomp may deny ptrace) -> bwrap.
run_readonly: bwrap + seccomp over a fully read-only root, for Dry Run's own git queries (S10).
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from dryrun.config import ShadowConfig
from dryrun.paths import bwrap_path, ensure_private_dir, state_dir
from dryrun.sandbox.layout import SandboxSpec, bwrap_argv, existing
from dryrun.sandbox.seccomp import build_filter

TRACE_CALLS = "trace=execve,execveat,connect,sendto,sendmsg"
REQUIRED_TOOLS = ("strace", "systemd-run", "systemctl", "prlimit", "nice", "ionice")
IONICE_CLASS = {"idle": "3", "best-effort": "2"}
SECCOMP_FD = 9
_WRAPPER = 'echo 1000 > /proc/self/oom_score_adj && exec 9<"$0" && exec "$@"'


class SandboxError(RuntimeError):
    pass


@dataclass
class SpawnResult:
    exit_code: int | None
    wall_ms: int
    timed_out: bool
    killed_reason: str | None
    stdout_path: Path
    stderr_path: Path
    trace_path: Path


_cgroup_ok: bool | None = None


def launcher_env() -> dict[str, str]:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus"),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cgroup_scope_works() -> bool:
    global _cgroup_ok
    if _cgroup_ok is None:
        try:
            r = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "TasksMax=16",
                                "-p", "MemoryMax=64M", "--", "true"],
                               env=launcher_env(), capture_output=True, timeout=15)
            _cgroup_ok = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _cgroup_ok = False
    return _cgroup_ok


def preflight(cfg: ShadowConfig, *, allow_root: bool = False, bwrap: Path | None = None) -> list[str]:
    problems: list[str] = []
    if os.geteuid() == 0 and not allow_root:
        problems.append("running as root: run Dry Run as a normal user (or pass --allow-root)")
    if platform.machine() != "x86_64":
        problems.append(f"unsupported architecture {platform.machine()} (seccomp filter is x86_64 only)")
    bw = Path(bwrap or bwrap_path())
    if not os.access(bw, os.X_OK):
        problems.append(f"bwrap not found at {bw} (run scripts/build-bwrap.sh)")
    else:
        pin = bw.with_name("bwrap.sha256")
        if not pin.exists():
            problems.append(f"missing pinned hash {pin}")
        elif pin.read_text().strip() != _sha256(bw):
            problems.append("bwrap binary does not match its pinned sha256")
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            problems.append(f"missing tool: {tool}")
    try:
        if int(Path("/proc/sys/user/max_user_namespaces").read_text()) <= 0:
            problems.append("user namespaces are disabled")
    except (OSError, ValueError):
        problems.append("cannot read /proc/sys/user/max_user_namespaces")
    if cfg.require_cgroup and not problems and not _cgroup_scope_works():
        problems.append("systemd --user scopes unavailable: cgroup limits cannot be enforced")
    return problems


def _filter_file() -> Path:
    path = ensure_private_dir(state_dir()) / "seccomp.bpf"
    data = build_filter()
    if not path.exists() or path.read_bytes() != data:
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return path


def _kill(proc: subprocess.Popen, unit: str, cfg: ShadowConfig) -> None:
    if cfg.require_cgroup:
        try:
            subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", f"{unit}.scope"],
                           env=launcher_env(), capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _started(trace_path: Path) -> bool:
    """True once the sandboxed command itself was exec'd (the first exec is bwrap)."""
    try:
        text = trace_path.read_text(errors="replace")
    except FileNotFoundError:
        return False
    execs = [ln for ln in text.splitlines() if "execve(" in ln and ln.rstrip().endswith("= 0")]
    return len(execs) >= 2


def run_shadow(spec: SandboxSpec, *, run_id: str, out_dir: Path, cfg: ShadowConfig, watch_fs: Path,
               cancel: threading.Event | None = None) -> SpawnResult:
    out_dir = Path(out_dir)
    spec = replace(spec, seccomp_fd=SECCOMP_FD)
    stdout_p, stderr_p, trace_p = out_dir / "stdout", out_dir / "stderr", out_dir / "trace"
    unit = f"dryrun-{run_id}"
    inner = ["prlimit", f"--fsize={cfg.file_size_max}", "--core=0", "--",
             "nice", "-n", str(cfg.nice), "ionice", "-c", IONICE_CLASS.get(cfg.ionice_class, "3"),
             "sh", "-c", _WRAPPER, str(_filter_file()),
             "strace", "-f", "-qq", "--seccomp-bpf", "-e", TRACE_CALLS, "-o", str(trace_p),
             *bwrap_argv(spec)]
    if cfg.require_cgroup:
        argv = ["systemd-run", "--user", "--scope", "-q", "--unit", unit,
                "-p", f"MemoryMax={cfg.memory_max}", "-p", "MemorySwapMax=0",
                "-p", f"TasksMax={cfg.tasks_max}", "--", *inner]
    else:
        argv = inner
    vfs = os.statvfs(watch_fs)
    free0 = vfs.f_bavail * vfs.f_frsize
    floor = max(cfg.free_space_floor_abs, int(cfg.free_space_floor_frac * vfs.f_blocks * vfs.f_frsize))
    start = time.monotonic()
    with open(stdout_p, "wb") as out, open(stderr_p, "wb") as err:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                env=launcher_env(), start_new_session=True, close_fds=True)
    killed: str | None = None
    while True:
        try:
            proc.wait(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            pass
        v = os.statvfs(watch_fs)
        free = v.f_bavail * v.f_frsize
        if time.monotonic() - start > cfg.wall_clock_s:
            killed = "timeout"
        elif free0 - free > cfg.disk_budget or free < floor:
            killed = "disk"
        elif cancel is not None and cancel.is_set():
            killed = "cancelled"
        if killed:
            _kill(proc, unit, cfg)
            break
    wall_ms = int((time.monotonic() - start) * 1000)
    code = proc.returncode
    if killed is None and code in (-signal.SIGKILL, 128 + signal.SIGKILL):
        killed = "killed"
    if killed is None and not _started(trace_p):
        detail = stderr_p.read_text(errors="replace").strip().splitlines()
        raise SandboxError(detail[0] if detail else f"sandbox did not start (exit {code})")
    return SpawnResult(exit_code=None if killed else code, wall_ms=wall_ms, timed_out=killed == "timeout",
                       killed_reason=killed, stdout_path=stdout_p, stderr_path=stderr_p, trace_path=trace_p)


def run_readonly(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                 timeout: float = 20.0) -> subprocess.CompletedProcess:
    """Run a trusted argv (Dry Run's own git queries) in a read-only, network-less sandbox so that
    repository-controlled programs (core.fsmonitor, clean filters) can never touch the real system."""
    base = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    base.update(env or {})
    fd = os.open(_filter_file(), os.O_RDONLY)
    try:
        spec = SandboxSpec(bwrap=str(bwrap_path()), overlays=(),
                           hide_early=existing(["/run", "/mnt/wsl", "/mnt/wslg"]), hide_late=(), ro_binds=(),
                           env=base, cwd=str(cwd), argv=tuple(argv), seccomp_fd=fd)
        return subprocess.run(bwrap_argv(spec), capture_output=True, timeout=timeout, pass_fds=(fd,),
                              env=launcher_env(), stdin=subprocess.DEVNULL)
    finally:
        os.close(fd)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_static_isolation.py tests/sandbox/test_spawn.py -q`
Expected: PASS (8 tests). If `test_setup_failure_raises_sandbox_error` fails because bwrap exits before strace records an exec, check `_started`. It must return False when only the bwrap exec is present.

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/sandbox/spawn.py tests/__init__.py tests/sandbox tests/unit/test_static_isolation.py
git commit -m "feat: sandbox launcher with cgroup scope, rlimits, strace, watchdogs and read-only mode"
```
