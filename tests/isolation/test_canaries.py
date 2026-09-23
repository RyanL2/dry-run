from __future__ import annotations

import uuid

import pytest

from dryrun.canary import run_all, run_gate
from dryrun.config import load_config
from dryrun.store import Store
from tests.conftest import TEST_ROOT, force_rmtree

pytestmark = [pytest.mark.sandbox, pytest.mark.slow]
EXPECTED = ["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9", "I10a", "I10b", "I11", "I12", "I13", "I14", "I15"]


@pytest.fixture(scope="module")
def results():
    # State under ~/dryrun-tests (like production's ~/.local/state), never under /tmp: the /tmp snapshot
    # would otherwise copy the state dir into itself.
    state = TEST_ROOT / f"canary-{uuid.uuid4().hex[:8]}"
    try:
        yield {r.id: r for r in run_all(load_config(use_user_file=False), Store(state))}
    finally:
        force_rmtree(state)


def test_every_channel_has_a_canary(results):
    assert sorted(results) == sorted(EXPECTED)


@pytest.mark.parametrize("cid", EXPECTED)
def test_canary_blocks_channel(results, cid):
    r = results[cid]
    assert r.passed, f"{cid} {r.name}: {r.detail}"


def test_canaries_detect_a_broken_sandbox(scratch, monkeypatch):
    """Mutation check: with an allow-everything seccomp filter the socket and kernel canaries must fail."""
    import struct

    import dryrun.sandbox.spawn as spawn
    monkeypatch.setattr(spawn, "build_filter", lambda: struct.pack("<HBBI", 0x06, 0, 0, 0x7FFF0000))
    monkeypatch.setenv("DRYRUN_STATE_DIR", str(scratch / "mutant-state"))
    got = {r.id: r.passed for r in run_all(load_config(use_user_file=False), Store(scratch / "mutant-state"))}
    assert got["I2"] is False and got["I7"] is False and got["I12"] is False


def test_gate_summary(scratch):
    ok, detail = run_gate(load_config(use_user_file=False), Store(scratch / "state"))
    assert ok, detail
