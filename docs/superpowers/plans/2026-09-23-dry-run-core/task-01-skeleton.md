### Task 1: Package skeleton, config, paths, test runner

**Files:**
- Create: `pyproject.toml`, `scripts/dev/test.sh`, `src/dryrun/__init__.py`, `src/dryrun/data/policy.yaml`, `src/dryrun/config.py`, `src/dryrun/paths.py`, `tests/conftest.py`, `tests/unit/test_config.py`, `tests/unit/test_paths.py`

**Interfaces:**
- Produces:
  - `dryrun.config.load_config(path: Path | None = None, *, use_user_file: bool = True) -> Config`
  - `Config(shadow: ShadowConfig, policy: Policy)` (frozen dataclasses; field names as below)
  - `parse_size(v) -> int`, `ConfigError`
  - `dryrun.paths.state_dir() -> Path`, `runtime_dir() -> Path`, `socket_path() -> Path`, `bwrap_path() -> Path`, `ensure_private_dir(p: Path) -> Path`, `BWRAP_VERSION = "0.11.0"`
  - fixtures `scratch` (fresh dir under `~/dryrun-tests`) and `state_env` (sets `DRYRUN_STATE_DIR`)

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_config.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from dryrun.config import Config, ConfigError, load_config, parse_size


def test_defaults_file_matches_dataclass_defaults():
    assert load_config(use_user_file=False) == Config()


def test_default_values_match_spec():
    cfg = load_config(use_user_file=False)
    assert cfg.shadow.wall_clock_s == 30
    assert cfg.shadow.memory_max == 2 * 1024**3
    assert cfg.shadow.tasks_max == 512
    assert cfg.shadow.disk_budget == 2 * 1024**3
    assert cfg.shadow.free_space_floor_abs == 5 * 1024**3
    assert cfg.shadow.free_space_floor_frac == 0.10
    assert cfg.shadow.tmp_max_entries == 2000
    assert cfg.policy.h1_mass_threshold == 20
    assert ".ssh" in cfg.policy.secret_paths
    assert "node_modules" in cfg.policy.build_output_dirs


def test_user_file_overrides_nested_keys(tmp_path: Path):
    user = tmp_path / "policy.yaml"
    user.write_text("shadow:\n  wall_clock_s: 5\nh1:\n  mass_threshold: 3\n")
    cfg = load_config(user)
    assert cfg.shadow.wall_clock_s == 5
    assert cfg.shadow.tasks_max == 512
    assert cfg.policy.h1_mass_threshold == 3
    assert cfg.policy.h1_overwrite_fraction == 0.9


def test_unknown_key_is_rejected(tmp_path: Path):
    user = tmp_path / "policy.yaml"
    user.write_text("shadow:\n  wal_clock_s: 5\n")
    with pytest.raises(ConfigError, match="shadow.wal_clock_s"):
        load_config(user)


@pytest.mark.parametrize("raw,expected", [(1024, 1024), ("2G", 2 * 1024**3), ("64M", 64 * 1024**2), ("1.5K", 1536)])
def test_parse_size(raw, expected):
    assert parse_size(raw) == expected


def test_parse_size_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_size("lots")
```

`tests/unit/test_paths.py`:
```python
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from dryrun import paths


def test_env_overrides(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DRYRUN_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("DRYRUN_SOCKET", str(tmp_path / "d.sock"))
    monkeypatch.setenv("DRYRUN_BWRAP", "/opt/bwrap")
    assert paths.state_dir() == tmp_path / "s"
    assert paths.socket_path() == tmp_path / "d.sock"
    assert paths.bwrap_path() == Path("/opt/bwrap")


def test_ensure_private_dir_sets_0700(tmp_path: Path):
    d = paths.ensure_private_dir(tmp_path / "a" / "b")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_ensure_private_dir_rejects_symlink(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(PermissionError):
        paths.ensure_private_dir(tmp_path / "link")
```

- [ ] **Step 2: Create the runner and skeleton, then run the tests to verify they fail**

`scripts/dev/test.sh`:
```bash
#!/bin/bash
# Run pytest inside WSL as the non-root dev user.
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then echo "refusing to run tests as root" >&2; exit 1; fi
cd "$(dirname "$0")/../.."
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
exec "$HOME/.venvs/dryrun/bin/python" -m pytest -p no:cacheprovider "$@"
```

`src/dryrun/__init__.py`: empty file.

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "dryrun"
version = "0.1.0"
description = "Decide from observed effects before a coding agent's shell command touches real files"
requires-python = ">=3.10"
license = { text = "Apache-2.0" }
dependencies = ["pyyaml>=6", "tree-sitter>=0.23", "tree-sitter-bash>=0.23"]

[project.optional-dependencies]
dev = ["pytest>=8", "hypothesis>=6", "jsonschema>=4"]

[project.scripts]
dryrun = "dryrun.cli:main"
dryrun-hook = "dryrun.hook:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
dryrun = ["data/*.yaml"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
  "sandbox: launches real bwrap sandboxes (Linux/WSL, non-root, user namespaces)",
  "slow: takes more than a few seconds",
]
```

`tests/conftest.py`:
```python
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

TEST_ROOT = Path.home() / "dryrun-tests"


def force_rmtree(path: Path) -> None:
    """Remove a tree even if a test left read-only directories behind."""
    if not path.exists():
        return
    for dirpath, dirnames, _ in os.walk(path):
        for name in dirnames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o700, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def scratch() -> Path:
    """Fresh directory under ~/dryrun-tests: same filesystem as the state dir, never a real project."""
    d = TEST_ROOT / uuid.uuid4().hex[:12]
    d.mkdir(parents=True)
    yield d
    force_rmtree(d)


@pytest.fixture
def state_env(scratch: Path, monkeypatch) -> Path:
    """Point Dry Run's state dir at a private per-test directory."""
    state = scratch / "state"
    monkeypatch.setenv("DRYRUN_STATE_DIR", str(state))
    return state
```

Run: `wsl.exe -d Ubuntu-22.04 -u dryrundev --cd /mnt/c/Users/rylei/github/dry-run/.claude/worktrees/core-spec -- bash scripts/dev/test.sh tests/unit -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.config'`).

- [ ] **Step 3: Implement config, defaults and paths**

`src/dryrun/data/policy.yaml`:
```yaml
# Dry Run default policy (spec §7). ~/.config/dryrun/policy.yaml overrides individual keys.
shadow:
  wall_clock_s: 30
  memory_max: 2G
  tasks_max: 512
  nice: 19
  ionice_class: idle          # WSL delegates only memory+pids cgroup controllers (spike 0)
  file_size_max: 1G
  disk_budget: 2G
  free_space_floor: {abs: 5G, frac: 0.10}
  require_cgroup: true
  canary_interval_h: 6
  tmp_snapshot: {max_entries: 2000, max_total: 64M, max_file: 16M}
  output_max: 16M
  max_concurrent: 2
h1: {mass_threshold: 20, overwrite_fraction: 0.9}
h4: {mass_threshold: 20}
build_output_dirs: [build, dist, target, out, node_modules, __pycache__, .venv, .pytest_cache, .mypy_cache, .next, coverage]
home_cache_dirs: [.cache, .npm, .cargo/registry, .local/share/pnpm, go/pkg/mod]
secret_paths: [.ssh, .aws, .config/gh, .netrc, .docker, .kube, .gnupg, .password-store, .config/gcloud, .azure, .claude/.credentials.json]
env_denylist: ["*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*", "AWS_*", "GH_*", "GITHUB_*", "ANTHROPIC_*", "OPENAI_*"]
dev_server_allowlist: ["npm run dev", "npm start", "vite", "next dev", "uvicorn *", "python -m http.server *"]
protected_paths: []
pending_ttl_min: 15
```

`src/dryrun/config.py`:
```python
"""Configuration: defaults from data/policy.yaml, overridden by an optional user file."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_FILE = Path(__file__).parent / "data" / "policy.yaml"
USER_POLICY_FILE = Path.home() / ".config" / "dryrun" / "policy.yaml"
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3}
_G = 1024**3
_M = 1024**2


class ConfigError(ValueError):
    pass


def parse_size(value: int | float | str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"not a size: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().upper()
    try:
        if text and text[-1] in _UNITS:
            return int(float(text[:-1]) * _UNITS[text[-1]])
        return int(text)
    except ValueError as exc:
        raise ConfigError(f"not a size: {value!r}") from exc


@dataclass(frozen=True)
class ShadowConfig:
    wall_clock_s: float = 30.0
    memory_max: int = 2 * _G
    tasks_max: int = 512
    nice: int = 19
    ionice_class: str = "idle"
    file_size_max: int = 1 * _G
    disk_budget: int = 2 * _G
    free_space_floor_abs: int = 5 * _G
    free_space_floor_frac: float = 0.10
    require_cgroup: bool = True
    canary_interval_h: float = 6.0
    tmp_max_entries: int = 2000
    tmp_max_total: int = 64 * _M
    tmp_max_file: int = 16 * _M
    output_max: int = 16 * _M
    max_concurrent: int = 2


@dataclass(frozen=True)
class Policy:
    h1_mass_threshold: int = 20
    h1_overwrite_fraction: float = 0.9
    h4_mass_threshold: int = 20
    build_output_dirs: tuple[str, ...] = (
        "build", "dist", "target", "out", "node_modules", "__pycache__", ".venv",
        ".pytest_cache", ".mypy_cache", ".next", "coverage",
    )
    home_cache_dirs: tuple[str, ...] = (".cache", ".npm", ".cargo/registry", ".local/share/pnpm", "go/pkg/mod")
    secret_paths: tuple[str, ...] = (
        ".ssh", ".aws", ".config/gh", ".netrc", ".docker", ".kube", ".gnupg", ".password-store",
        ".config/gcloud", ".azure", ".claude/.credentials.json",
    )
    env_denylist: tuple[str, ...] = (
        "*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*", "AWS_*", "GH_*", "GITHUB_*", "ANTHROPIC_*", "OPENAI_*",
    )
    dev_server_allowlist: tuple[str, ...] = (
        "npm run dev", "npm start", "vite", "next dev", "uvicorn *", "python -m http.server *",
    )
    protected_paths: tuple[str, ...] = ()
    pending_ttl_min: float = 15.0


@dataclass(frozen=True)
class Config:
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    policy: Policy = field(default_factory=Policy)


_SHADOW_KEYS = {
    "wall_clock_s", "memory_max", "tasks_max", "nice", "ionice_class", "file_size_max", "disk_budget",
    "free_space_floor", "require_cgroup", "canary_interval_h", "tmp_snapshot", "output_max", "max_concurrent",
}
_TOP_KEYS = {
    "shadow", "h1", "h4", "build_output_dirs", "home_cache_dirs", "secret_paths", "env_denylist",
    "dev_server_allowlist", "protected_paths", "pending_ttl_min",
}


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _check_keys(raw: dict[str, Any]) -> None:
    unknown = set(raw) - _TOP_KEYS
    unknown |= {f"shadow.{k}" for k in set(raw.get("shadow") or {}) - _SHADOW_KEYS}
    if unknown:
        raise ConfigError("unknown config keys: " + ", ".join(sorted(unknown)))


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in (value or ()))


def _build(raw: dict[str, Any]) -> Config:
    s = raw["shadow"]
    floor = s["free_space_floor"]
    tmp = s["tmp_snapshot"]
    shadow = ShadowConfig(
        wall_clock_s=float(s["wall_clock_s"]),
        memory_max=parse_size(s["memory_max"]),
        tasks_max=int(s["tasks_max"]),
        nice=int(s["nice"]),
        ionice_class=str(s["ionice_class"]),
        file_size_max=parse_size(s["file_size_max"]),
        disk_budget=parse_size(s["disk_budget"]),
        free_space_floor_abs=parse_size(floor["abs"]),
        free_space_floor_frac=float(floor["frac"]),
        require_cgroup=bool(s["require_cgroup"]),
        canary_interval_h=float(s["canary_interval_h"]),
        tmp_max_entries=int(tmp["max_entries"]),
        tmp_max_total=parse_size(tmp["max_total"]),
        tmp_max_file=parse_size(tmp["max_file"]),
        output_max=parse_size(s["output_max"]),
        max_concurrent=int(s["max_concurrent"]),
    )
    policy = Policy(
        h1_mass_threshold=int(raw["h1"]["mass_threshold"]),
        h1_overwrite_fraction=float(raw["h1"]["overwrite_fraction"]),
        h4_mass_threshold=int(raw["h4"]["mass_threshold"]),
        build_output_dirs=_strings(raw["build_output_dirs"]),
        home_cache_dirs=_strings(raw["home_cache_dirs"]),
        secret_paths=_strings(raw["secret_paths"]),
        env_denylist=_strings(raw["env_denylist"]),
        dev_server_allowlist=_strings(raw["dev_server_allowlist"]),
        protected_paths=_strings(raw["protected_paths"]),
        pending_ttl_min=float(raw["pending_ttl_min"]),
    )
    return Config(shadow=shadow, policy=policy)


def load_config(path: Path | None = None, *, use_user_file: bool = True) -> Config:
    raw = _read_yaml(DEFAULT_POLICY_FILE)
    user = path
    if user is None and use_user_file and USER_POLICY_FILE.exists():
        user = USER_POLICY_FILE
    if user is not None:
        override = _read_yaml(Path(user))
        _check_keys(override)
        raw = _merge(raw, override)
    _check_keys(raw)
    try:
        return _build(raw)
    except KeyError as exc:
        raise ConfigError(f"missing config key: {exc}") from exc


def with_shadow(cfg: Config, **changes: Any) -> Config:
    """Copy of cfg with some ShadowConfig fields replaced (tests and canaries)."""
    return replace(cfg, shadow=replace(cfg.shadow, **changes))
```

`src/dryrun/paths.py`:
```python
"""Where Dry Run keeps things. Environment variables override every location (tests use them)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

BWRAP_VERSION = "0.11.0"


def state_dir() -> Path:
    return Path(os.environ.get("DRYRUN_STATE_DIR") or Path.home() / ".local" / "state" / "dryrun")


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def socket_path() -> Path:
    return Path(os.environ.get("DRYRUN_SOCKET") or runtime_dir() / "dryrun.sock")


def bwrap_path() -> Path:
    default = Path.home() / ".local" / "share" / "dryrun" / "bwrap" / BWRAP_VERSION / "bwrap"
    return Path(os.environ.get("DRYRUN_BWRAP") or default)


def ensure_private_dir(path: Path) -> Path:
    """Create path (and parents) owned by us with mode 0700; refuse symlinks and foreign owners."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"{path} is not a real directory")
    if st.st_uid != os.getuid():
        raise PermissionError(f"{path} is owned by uid {st.st_uid}")
    os.chmod(path, 0o700)
    return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit -q`
Expected: PASS (all tests in `test_config.py`, `test_paths.py`).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml scripts/dev/test.sh src tests
git commit -m "feat: package skeleton, config loader, state paths, WSL test runner"
```
