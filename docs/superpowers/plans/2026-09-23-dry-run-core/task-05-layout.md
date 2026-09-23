### Task 5: bwrap layout builder (pure)

**Files:**
- Create: `src/dryrun/sandbox/layout.py`, `tests/unit/test_layout.py`

**Interfaces:**
- Produces:
  - `OverlaySpec(lower: str, upper: str, work: str, target: str)`
  - `SandboxSpec(bwrap: str, overlays: tuple[OverlaySpec, ...], hide_early: tuple[str, ...], hide_late: tuple[str, ...], ro_binds: tuple[tuple[str, str], ...], env: dict[str, str], cwd: str, argv: tuple[str, ...], seccomp_fd: int | None = None)`
  - `bwrap_argv(spec) -> list[str]`
  - `existing(paths) -> tuple[str, ...]`: keeps only paths that exist (lexists), because bwrap cannot create mount points on the read-only root (spike 0)

**Argument order** (spike 0 verified this sequence):
1. namespaces
2. `--ro-bind / /`, `--dev`, `--proc`
3. early tmpfs hides (`/run`, `/mnt/wsl`, `/mnt/wslg`)
4. overlays in the order given (`/tmp` before the workspace, so a workspace under `/tmp` mounts inside the new `/tmp`)
5. late tmpfs hides (the state dir, `~/.claude`)
6. decoy ro-binds
7. env
8. chdir
9. seccomp
10. `--` and the argv

- [ ] **Step 1: Write the failing test**

`tests/unit/test_layout.py`:
```python
from __future__ import annotations

from pathlib import Path

from dryrun.sandbox.layout import OverlaySpec, SandboxSpec, bwrap_argv, existing


def spec(**kw) -> SandboxSpec:
    base = dict(
        bwrap="/opt/bwrap",
        overlays=(OverlaySpec("/s/tmp.lower", "/s/tmp.up", "/s/tmp.wk", "/tmp"),
                  OverlaySpec("/home/u/ws", "/s/ws.up", "/s/ws.wk", "/home/u/ws")),
        hide_early=("/run",), hide_late=("/home/u/.local/state/dryrun",),
        ro_binds=(("/s/decoys/0", "/home/u/.ssh"),),
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/u"}, cwd="/home/u/ws/sub",
        argv=("bash", "-c", "rm -rf build"), seccomp_fd=7,
    )
    base.update(kw)
    return SandboxSpec(**base)


def test_argv_order_and_isolation_flags():
    a = bwrap_argv(spec())
    assert a[0] == "/opt/bwrap"
    for flag in ["--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]:
        assert flag in a
    assert a[a.index("--cap-drop") + 1] == "ALL"
    i_root = a.index("--ro-bind")
    i_run = a.index("/run")
    i_tmp = a.index("/s/tmp.lower")
    i_ws = a.index("/s/ws.up")
    i_state = a.index("/home/u/.local/state/dryrun")
    i_decoy = a.index("/s/decoys/0")
    assert a[i_root:i_root + 3] == ["--ro-bind", "/", "/"]
    assert i_root < i_run < i_tmp < i_ws < i_state < i_decoy
    assert a[a.index("--seccomp") + 1] == "7"
    assert a[a.index("--chdir") + 1] == "/home/u/ws/sub"
    assert a[-4:] == ["--", "bash", "-c", "rm -rf build"]


def test_overlay_triplet():
    a = bwrap_argv(spec())
    i = a.index("--overlay-src")
    assert a[i:i + 6] == ["--overlay-src", "/s/tmp.lower", "--overlay", "/s/tmp.up", "/s/tmp.wk", "/tmp"]


def test_env_is_cleared_then_set_sorted():
    a = bwrap_argv(spec())
    i = a.index("--clearenv")
    assert a[i + 1:i + 7] == ["--setenv", "HOME", "/home/u", "--setenv", "PATH", "/usr/bin:/bin"]


def test_no_seccomp_flag_when_fd_missing():
    assert "--seccomp" not in bwrap_argv(spec(seccomp_fd=None))


def test_existing_filters_missing_paths(tmp_path: Path):
    (tmp_path / "a").mkdir()
    assert existing([str(tmp_path / "a"), str(tmp_path / "missing")]) == (str(tmp_path / "a"),)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/unit/test_layout.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.sandbox.layout'`)

- [ ] **Step 3: Implement**

`src/dryrun/sandbox/layout.py`:
```python
"""Pure translation of a SandboxSpec into a bwrap argv (ARCHITECTURE §7)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class OverlaySpec:
    lower: str
    upper: str
    work: str
    target: str


@dataclass(frozen=True)
class SandboxSpec:
    bwrap: str
    overlays: tuple[OverlaySpec, ...]
    hide_early: tuple[str, ...]
    hide_late: tuple[str, ...]
    ro_binds: tuple[tuple[str, str], ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = "/"
    argv: tuple[str, ...] = ()
    seccomp_fd: int | None = None


def existing(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(p for p in paths if os.path.lexists(p))


def bwrap_argv(spec: SandboxSpec) -> list[str]:
    a = [spec.bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
         "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for path in spec.hide_early:
        a += ["--tmpfs", path]
    for o in spec.overlays:
        a += ["--overlay-src", o.lower, "--overlay", o.upper, o.work, o.target]
    for path in spec.hide_late:
        a += ["--tmpfs", path]
    for src, dst in spec.ro_binds:
        a += ["--ro-bind", src, dst]
    a.append("--clearenv")
    for key in sorted(spec.env):
        a += ["--setenv", key, spec.env[key]]
    a += ["--chdir", spec.cwd]
    if spec.seccomp_fd is not None:
        a += ["--seccomp", str(spec.seccomp_fd)]
    a.append("--")
    a.extend(spec.argv)
    return a
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/unit/test_layout.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/sandbox/layout.py tests/unit/test_layout.py
git commit -m "feat: bwrap argv builder with verified mount ordering"
```
