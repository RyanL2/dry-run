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
