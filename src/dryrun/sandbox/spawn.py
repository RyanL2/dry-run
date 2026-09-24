"""The one place Dry Run starts processes for user commands (spec S2).

run_shadow: systemd user scope (MemoryMax, TasksMax) -> prlimit (fsize, core) -> nice/ionice ->
oom_score_adj=1000 -> strace (outside bwrap, so the in-sandbox seccomp may deny ptrace) -> bwrap.
run_readonly: bwrap + seccomp over a fully read-only root, for Dry Run's own git queries (S10).
run_readonly_command: the same sandbox for the agent's read-only git commands, output replayed later.
"""
from __future__ import annotations

import fcntl
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

from dryrun.config import Policy, ShadowConfig
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
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
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


READONLY_MEMORY_MAX = 1024**3
READONLY_TASKS_MAX = 128


def _secret_hides(home: Path, secret_paths=None) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """tmpfs over secret directories, /dev/null over secret files. Symlinked secrets (~/.ssh -> /mnt/c/...,
    dotfile managers) are hidden at their resolved target, since bwrap cannot mount over a symlink."""
    dirs, files = [], []
    for rel in Policy().secret_paths if secret_paths is None else secret_paths:
        real = Path(os.path.realpath(home / rel))
        if real.is_dir():
            dirs.append(str(real))
        elif real.exists():
            files.append(("/dev/null", str(real)))
    return tuple(dirs), tuple(files)


def _readonly_launch(argv, *, cwd: Path, env: dict[str, str] | None, homes=(), secret_paths=None,
                     hide: tuple[str, ...] = (), extra_ro_binds: tuple[tuple[str, str], ...] = (),
                     fsize: int | None = None) -> list[str]:
    base = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    base.update(env or {})
    secret_dirs, secret_files = [], []
    for home in dict.fromkeys([Path.home(), *map(Path, homes)]):
        d, f = _secret_hides(home, secret_paths)
        secret_dirs += d
        secret_files += f
    spec = SandboxSpec(bwrap=str(bwrap_path()), overlays=(),
                       hide_early=existing(["/run", "/mnt/wsl", "/mnt/wslg"]),
                       hide_late=existing(list(dict.fromkeys([str(state_dir()), *hide, *secret_dirs]))),
                       ro_binds=tuple(dict.fromkeys(secret_files)) + tuple(extra_ro_binds),
                       env=base, cwd=str(cwd), argv=tuple(argv), seccomp_fd=SECCOMP_FD)
    limits = ["--core=0"] + ([f"--fsize={fsize}"] if fsize is not None else [])
    inner = ["prlimit", *limits, "--", "sh", "-c", 'exec 9<"$0" && exec "$@"', str(_filter_file()),
             *bwrap_argv(spec)]
    if _cgroup_scope_works():
        inner = ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={READONLY_MEMORY_MAX}",
                 "-p", "MemorySwapMax=0", "-p", f"TasksMax={READONLY_TASKS_MAX}", "--", *inner]
    return inner


def run_readonly(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 20.0,
                 extra_ro_binds: tuple[tuple[str, str], ...] = ()) -> subprocess.CompletedProcess:
    """Run a trusted argv (Dry Run's own git queries) in a read-only, network-less sandbox so that
    repository-controlled programs (core.fsmonitor, clean filters) can never touch the real system:
    same seccomp filter, Dry Run's state and the user's secrets hidden, and (when available) the same
    kind of cgroup scope as shadow runs so they cannot exhaust host memory or PIDs."""
    inner = _readonly_launch(argv, cwd=cwd, env=env, extra_ro_binds=extra_ro_binds)
    return subprocess.run(inner, capture_output=True, timeout=timeout, env=launcher_env(),
                          stdin=subprocess.DEVNULL, start_new_session=True)


@dataclass
class ReadonlyResult:
    exit_code: int | None
    wall_ms: int
    killed_reason: str | None
    truncated: bool
    stdout_path: Path
    stderr_path: Path


def run_readonly_command(command: str, *, cwd: Path, env: dict[str, str], home: Path, out_dir: Path,
                         timeout: float, output_max: int, secret_paths=None, hide: tuple[str, ...] = (),
                         cancel: threading.Event | None = None) -> ReadonlyResult:
    """Run an agent's read-only command (git reads) in the run_readonly sandbox instead of natively, with
    stdout/stderr going to files for replay by `dryrun apply`. Whatever programs the command starts (git
    config can name many) find a read-only root, no network and hidden secrets. Output above output_max is
    cut off (truncated=True). Raises SandboxError if the sandbox never started the command, so a bwrap error
    is never replayed as if the command had printed it."""
    out_dir = Path(out_dir)
    stdout_p, stderr_p = out_dir / "stdout", out_dir / "stderr"
    r, w0 = os.pipe()
    w = fcntl.fcntl(w0, fcntl.F_DUPFD_CLOEXEC, 10)  # above the seccomp fd (9) the launcher opens
    os.close(w0)
    started_marker = f'printf 1 >&{w} && exec {w}>&- && exec /bin/bash -c "$0"'
    try:
        inner = _readonly_launch(["/bin/bash", "-c", started_marker, command], cwd=cwd, env=env, homes=(home,),
                                 secret_paths=secret_paths, hide=hide, fsize=output_max + 1)
        start = time.monotonic()
        with open(stdout_p, "wb") as out, open(stderr_p, "wb") as err:
            proc = subprocess.Popen(inner, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=launcher_env(),
                                    start_new_session=True, pass_fds=(w,))
        os.close(w)
        w = -1
        killed: str | None = None
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - start > timeout:
                killed = "timeout"
            elif cancel is not None and cancel.is_set():
                killed = "cancelled"
            if killed:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=10)
                break
        wall_ms = int((time.monotonic() - start) * 1000)
        os.set_blocking(r, False)
        try:
            started = os.read(r, 1) == b"1"
        except BlockingIOError:
            started = False
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)
    if killed is None and not started:
        detail = stderr_p.read_text(errors="replace").strip().splitlines()
        raise SandboxError(detail[0] if detail else f"read-only sandbox did not start (exit {proc.returncode})")
    truncated = any(pth.stat().st_size > output_max for pth in (stdout_p, stderr_p))
    return ReadonlyResult(exit_code=None if killed else proc.returncode, wall_ms=wall_ms, killed_reason=killed,
                          truncated=truncated, stdout_path=stdout_p, stderr_path=stderr_p)
