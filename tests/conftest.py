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
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                try:
                    os.chmod(full, 0o700)
                except OSError:
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
