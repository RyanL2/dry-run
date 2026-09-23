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
