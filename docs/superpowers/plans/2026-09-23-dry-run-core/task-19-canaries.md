### Task 19: Isolation canaries I1–I15 (S1, F13): the CRITICAL requirement, executable

**Files:**
- Create: `src/dryrun/canary.py`, `tests/isolation/__init__.py` (empty), `tests/isolation/test_canaries.py`

**Interfaces:**
- Consumes: `Config`, `with_shadow` (Task 1); `prepare` (Task 11, the **production layout**); `run_shadow`, `SandboxError` (Task 7); `scan` (Task 6); `Store` (Task 14); `runtime_dir`, `bwrap_path` (Task 1)
- Produces:
  - `CanaryResult(id: str, name: str, passed: bool, detail: str)`
  - `run_all(cfg: Config, store: Store) -> list[CanaryResult]`: one result per I1–I15. I10 is split into I10a (priority) and I10b (wall clock).
  - `run_gate(cfg: Config, store: Store) -> tuple[bool, str]`: used by the daemon

**Method:**
- One main probe run in the production layout checks I1–I7, I10a and I12–I15. The probe (Python) tries each escape from inside. The host then checks from outside that nothing happened.
- Four resource runs with tight limits cover I8 (memory), I9 (tasks), I10b (wall clock) and I11 (disk).
- Host-side targets:
  - write targets under `/`, the real `$HOME`, `/etc`, `/mnt/c` and `/usr/lib/wsl`
  - listening unix sockets in the real `/tmp` and `$XDG_RUNTIME_DIR`
  - a host `sleep` process
  - a `/dev/shm` file
  - a synthetic home holding a secret file
- Everything is cleaned up afterwards.

- [ ] **Step 1: Write the failing test**

`tests/isolation/test_canaries.py`:
```python
from __future__ import annotations

import pytest

from dryrun.canary import run_all, run_gate
from dryrun.config import load_config
from dryrun.store import Store

pytestmark = [pytest.mark.sandbox, pytest.mark.slow]
EXPECTED = ["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9", "I10a", "I10b", "I11", "I12", "I13", "I14", "I15"]


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    import os
    state = tmp_path_factory.mktemp("canary-state")
    return {r.id: r for r in run_all(load_config(use_user_file=False), Store(state))}


def test_every_channel_has_a_canary(results):
    assert sorted(results) == sorted(EXPECTED)


@pytest.mark.parametrize("cid", EXPECTED)
def test_canary_blocks_channel(results, cid):
    r = results[cid]
    assert r.passed, f"{cid} {r.name}: {r.detail}"


def test_gate_summary(tmp_path):
    ok, detail = run_gate(load_config(use_user_file=False), Store(tmp_path / "state"))
    assert ok, detail
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/isolation -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.canary'`)

- [ ] **Step 3: Implement**

`src/dryrun/canary.py`:
```python
"""Isolation canaries I1-I15 (ARCHITECTURE §7.1). Each probe runs INSIDE a shadow built by the same
`prepare()` the pipeline uses, then the host checks from the outside that nothing was affected.
If any canary fails, the daemon disables shadowing and every Bash call gets `ask` (spec F13)."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dryrun.config import Config, with_shadow
from dryrun.paths import bwrap_path, runtime_dir
from dryrun.runpaths import RunPaths
from dryrun.sandbox.assemble import prepare
from dryrun.sandbox.decoys import scan
from dryrun.sandbox.spawn import SandboxError, SpawnResult, run_shadow
from dryrun.store import Store

CANARY_ROOT = Path.home() / ".local" / "share" / "dryrun" / "canary"
CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"

PROBE = r'''
import ctypes, errno, json, os, socket, subprocess, sys
cfg = json.load(open(sys.argv[1]))
res = {}
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
    except Exception as e:
        return type(e).__name__
def write(path):
    with open(path, "w") as f:
        f.write("canary")
res["writes"] = {label: attempt(lambda p=path: write(p)) for label, path in cfg["write_targets"].items()}
res["write_tmp"] = attempt(lambda: write(cfg["tmp_target"]))
def unix_connect(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(1)
    s.connect(path)
res["unix_create"] = attempt(lambda: socket.socket(socket.AF_UNIX).close())
res["vsock_create"] = attempt(lambda: socket.socket(40, socket.SOCK_STREAM).close())
res["unix_connect"] = {p: attempt(lambda p=p: unix_connect(p)) for p in cfg["unix_paths"]}
res["tcp"] = attempt(lambda: socket.create_connection(("1.1.1.1", 443), 2).close())
res["dns"] = attempt(lambda: socket.getaddrinfo("example.com", 443))
res["kill"] = attempt(lambda: os.kill(cfg["host_pid"], 9))
res["shm_visible"] = os.path.exists("/dev/shm/" + cfg["shm_name"])
res["blockdev"] = attempt(lambda: open(cfg["blockdev"], "rb").read(1)) if cfg["blockdev"] else "skip"
def tiocsti():
    import fcntl
    fd = os.open("/dev/tty", os.O_RDWR)
    fcntl.ioctl(fd, 0x5412, b"x")
res["tiocsti"] = attempt(tiocsti)
libc = ctypes.CDLL(None, use_errno=True)
def sysc(nr, *args):
    r = libc.syscall(nr, *args)
    return "ok" if r >= 0 else errno.errorcode.get(ctypes.get_errno(), "?")
res["kernel"] = {"keyctl": sysc(250, 0, 0, 0, 0, 0), "io_uring_setup": sysc(425, 1, None),
                 "bpf": sysc(321, 0, None, 0), "ptrace": sysc(101, 0, 0, 0, 0),
                 "unshare_newuser": sysc(272, 0x10000000), "mount": sysc(165, None, None, None, 0, None)}
res["nice"] = os.nice(0)
res["ioprio_class"] = libc.syscall(252, 1, 0) >> 13
res["wsl_env"] = "WSL_INTEROP" in os.environ
res["interop"] = (attempt(lambda: subprocess.run([cfg["cmd_exe"], "/c", "echo"], timeout=5,
                                                 capture_output=True, check=True))
                  if cfg["cmd_exe"] else "skip")
res["state_listing"] = sorted(os.listdir(cfg["state_dir"])) if os.path.isdir(cfg["state_dir"]) else "absent"
res["secret_env"] = sorted(k for k in os.environ if "DRYRUN_CANARY" in k)
res["secret_file"] = open(cfg["secret_file"]).read()[:200] if os.path.exists(cfg["secret_file"]) else "absent"
subprocess.Popen(["setsid", "sleep", cfg["sleeper_marker"]], start_new_session=True,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps(res))
'''

FORK_PROBE = r'''
import os, time
n = 0
try:
    while n < 1000:
        pid = os.fork()
        if pid == 0:
            time.sleep(5)
            os._exit(0)
        n += 1
except OSError:
    pass
print(n)
'''


@dataclass
class CanaryResult:
    id: str
    name: str
    passed: bool
    detail: str


def _first_blockdev() -> str | None:
    for name in sorted(os.listdir("/dev")):
        path = f"/dev/{name}"
        try:
            if os.path.exists(path) and os.stat(path).st_mode & 0o170000 == 0o060000:
                return path
        except OSError:
            continue
    return None


def _procs_with(marker: str) -> list[int]:
    found = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if marker in cmd:
                found.append(int(pid))
    return found


class _Host:
    """Host-side canary targets."""

    def __init__(self) -> None:
        self.tag = secrets.token_hex(4)
        self.root = CANARY_ROOT / self.tag
        self.ws = self.root / "ws"
        self.home = self.root / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_canary").write_text("REAL-CANARY-SECRET\n")
        self.ws.mkdir(parents=True)
        (self.ws / "probe.py").write_text(PROBE)
        (self.ws / "fork_probe.py").write_text(FORK_PROBE)
        name = f"dryrun_canary_{self.tag}"
        self.write_targets = {"root": f"/{name}", "etc": f"/etc/{name}", "home": str(Path.home() / name)}
        if os.path.isdir("/mnt/c/Users/Public"):
            self.write_targets["mnt_c"] = f"/mnt/c/Users/Public/{name}"
        if os.path.isdir("/usr/lib/wsl"):
            self.write_targets["usr_lib_wsl"] = f"/usr/lib/wsl/{name}"
        self.tmp_target = f"/tmp/{name}"
        self.listeners = []
        self.unix_paths = []
        for base in (Path("/tmp"), runtime_dir()):
            path = base / f"dryrun-canary-{self.tag}.sock"
            try:
                s = socket.socket(socket.AF_UNIX)
                s.bind(str(path))
                s.listen(1)
                s.setblocking(False)
                self.listeners.append(s)
                self.unix_paths.append(str(path))
            except OSError:
                pass
        self.sleeper = subprocess.Popen(["sleep", "600"])
        self.shm_name = f"dryrun-canary-{self.tag}"
        Path("/dev/shm", self.shm_name).write_text("host shm")
        self.sleeper_marker = f"601.{int(self.tag, 16) % 1000000}"
        self.cfg = {"write_targets": self.write_targets, "tmp_target": self.tmp_target,
                    "unix_paths": self.unix_paths, "host_pid": self.sleeper.pid, "shm_name": self.shm_name,
                    "blockdev": _first_blockdev(), "cmd_exe": CMD_EXE if os.path.exists(CMD_EXE) else None,
                    "secret_file": str(self.home / ".ssh" / "id_canary"),
                    "sleeper_marker": self.sleeper_marker}

    def accepted_any(self) -> bool:
        for s in self.listeners:
            try:
                conn, _ = s.accept()
                conn.close()
                return True
            except BlockingIOError:
                pass
        return False

    def close(self) -> None:
        self.sleeper.kill()
        self.sleeper.wait()
        for s in self.listeners:
            s.close()
        for path in self.unix_paths + [f"/dev/shm/{self.shm_name}"]:
            try:
                os.unlink(path)
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)


def _run(host: _Host, store: Store, cfg: Config, command: str) -> tuple[SpawnResult, RunPaths, object]:
    run = store.new_run()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(host.home), "LANG": "C.UTF-8",
           "DRYRUN_CANARY_SECRET_TOKEN": "leak-me", "WSL_INTEROP": "/run/WSL/1_interop"}
    prep = prepare(run, ws_root=host.ws, cwd=host.ws, command=command, env=env, cfg=cfg, home=host.home,
                   state=store.root, bwrap=str(bwrap_path()))
    res = run_shadow(prep.spec, run_id=run.run_id, out_dir=run.root, cfg=cfg.shadow, watch_fs=run.root)
    return res, run, prep


def _main_probe(host: _Host, store: Store, cfg: Config) -> list[CanaryResult]:
    cfg_file = host.ws / "probe_cfg.json"
    host.cfg["state_dir"] = str(store.root)
    cfg_file.write_text(json.dumps(host.cfg))
    res, run, prep = _run(host, store, cfg, "python3 probe.py probe_cfg.json")
    try:
        out = res.stdout_path.read_bytes()
        try:
            p = json.loads(out.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            err = res.stderr_path.read_text(errors="replace")[-300:]
            return [CanaryResult(i, "main probe", False, f"probe did not report: {err}")
                    for i in ("I1", "I2", "I3", "I4", "I5", "I6", "I7", "I10a", "I12", "I13", "I14", "I15")]
        leaked = [label for label, path in host.write_targets.items() if os.path.exists(path)]
        leaked += ["tmp"] if os.path.exists(host.tmp_target) else []
        results = [
            CanaryResult("I1", "filesystem writes outside the overlays",
                         not leaked and all(v != "ok" for v in p["writes"].values()),
                         f"inside={p['writes']} write_tmp={p['write_tmp']} host_leaks={leaked}"),
            CanaryResult("I2", "unix-socket host services (incl. dryrund)",
                         p["unix_create"] == "EAFNOSUPPORT" and all(v != "ok" for v in p["unix_connect"].values())
                         and not host.accepted_any(),
                         f"create={p['unix_create']} connect={p['unix_connect']}"),
            CanaryResult("I3", "network", p["tcp"] != "ok" and p["dns"] != "ok", f"tcp={p['tcp']} dns={p['dns']}"),
            CanaryResult("I4", "host processes", p["kill"] != "ok" and host.sleeper.poll() is None,
                         f"kill={p['kill']} host_alive={host.sleeper.poll() is None}"),
            CanaryResult("I5", "IPC / shared memory", not p["shm_visible"], f"shm_visible={p['shm_visible']}"),
            CanaryResult("I6", "devices and terminal injection", p["blockdev"] != "ok" and p["tiocsti"] != "ok",
                         f"blockdev={p['blockdev']} tiocsti={p['tiocsti']}"),
            CanaryResult("I7", "kernel interfaces", all(v == "EPERM" for v in p["kernel"].values()), str(p["kernel"])),
            CanaryResult("I10a", "CPU/IO priority", p["nice"] == cfg.shadow.nice and p["ioprio_class"] == 3,
                         f"nice={p['nice']} ioprio_class={p['ioprio_class']}"),
            CanaryResult("I12", "WSL interop and Hyper-V sockets",
                         not p["wsl_env"] and p["interop"] != "ok" and p["vsock_create"] != "ok",
                         f"WSL_INTEROP_set={p['wsl_env']} cmd.exe={p['interop']} vsock={p['vsock_create']}"),
            CanaryResult("I13", "Dry Run's own state", p["state_listing"] in ([], "absent"),
                         f"state_listing={p['state_listing']}"),
        ]
        secret_ok = (p["secret_env"] == [] and "REAL-CANARY-SECRET" not in p["secret_file"]
                     and bool(scan(prep.token, {"f": p["secret_file"].encode()})))
        results.append(CanaryResult("I14", "secrets (env + decoys)", secret_ok,
                                    f"env={p['secret_env']} file={p['secret_file'][:60]!r}"))
        survivors = _procs_with(f"sleep {host.sleeper_marker}")
        for pid in survivors:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        results.append(CanaryResult("I15", "processes surviving the shadow", not survivors, f"survivors={survivors}"))
        return results
    finally:
        store.remove_run(run)


def _resource_runs(host: _Host, store: Store, cfg: Config) -> list[CanaryResult]:
    out = []
    mem_cfg = with_shadow(cfg, memory_max=128 * 1024**2, wall_clock_s=20)
    res, run, _ = _run(host, store, mem_cfg, "python3 -c \"b = bytearray(512 * 1024 * 1024); print('LEAK')\"")
    leaked = b"LEAK" in res.stdout_path.read_bytes()
    store.remove_run(run)
    out.append(CanaryResult("I8", "memory limit", not leaked and host.sleeper.poll() is None,
                            f"exit={res.exit_code} killed={res.killed_reason} printed_leak={leaked}"))
    task_cfg = with_shadow(cfg, tasks_max=64, wall_clock_s=20)
    res, run, _ = _run(host, store, task_cfg, "python3 fork_probe.py")
    text = res.stdout_path.read_text().strip()
    store.remove_run(run)
    n = int(text) if text.isdigit() else -1
    host_fork = subprocess.run(["true"]).returncode == 0
    out.append(CanaryResult("I9", "process-count limit", 0 <= n < 64 and host_fork, f"forked={n} host_fork={host_fork}"))
    clock_cfg = with_shadow(cfg, wall_clock_s=2)
    res, run, _ = _run(host, store, clock_cfg, "sleep 60")
    store.remove_run(run)
    out.append(CanaryResult("I10b", "wall-clock limit", res.timed_out and res.wall_ms < 8000,
                            f"timed_out={res.timed_out} wall_ms={res.wall_ms}"))
    disk_cfg = with_shadow(cfg, disk_budget=64 * 1024**2, wall_clock_s=30)
    res, run, _ = _run(host, store, disk_cfg, "dd if=/dev/zero of=big bs=1M count=600 status=none")
    store.remove_run(run)
    out.append(CanaryResult("I11", "disk budget", res.killed_reason == "disk",
                            f"killed={res.killed_reason} exit={res.exit_code}"))
    return out


def run_all(cfg: Config, store: Store) -> list[CanaryResult]:
    host = _Host()
    try:
        return _main_probe(host, store, cfg) + _resource_runs(host, store, cfg)
    except SandboxError as exc:
        return [CanaryResult("I0", "sandbox setup", False, str(exc))]
    finally:
        host.close()


def run_gate(cfg: Config, store: Store) -> tuple[bool, str]:
    results = run_all(cfg, store)
    failed = [f"{r.id} {r.name}: {r.detail}" for r in results if not r.passed]
    if failed:
        return False, "; ".join(failed)[:900]
    return True, f"all {len(results)} isolation canaries passed"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/isolation -q`
Expected: PASS (18 tests). **Every canary must pass. If one fails, treat it as a CRITICAL isolation bug.** Fix the mechanism (layout, seccomp or spawn) until it passes. Never weaken the canary.

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/canary.py tests/isolation
git commit -m "feat: isolation canaries I1-I15 against the production sandbox layout"
```
