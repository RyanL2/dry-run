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
        ".config/gcloud", ".azure", ".claude/.credentials.json", ".git-credentials", ".npmrc", ".pypirc",
    )
    env_denylist: tuple[str, ...] = (
        "*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*", "AWS_*", "GH_*", "GITHUB_*", "ANTHROPIC_*", "OPENAI_*",
    )
    dev_server_allowlist: tuple[str, ...] = (
        "npm run dev", "npm start", "vite", "next dev", "uvicorn *", "python -m http.server *",
    )
    protected_paths: tuple[str, ...] = ("research/frozen", "research/frozen/*", "research/frozen.lock.json",
                                      "research/evaluate.py")
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
