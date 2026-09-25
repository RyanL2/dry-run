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
    assert "research/frozen/*" in cfg.policy.protected_paths


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
