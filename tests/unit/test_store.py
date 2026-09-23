from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from dryrun.store import Store, TokenError, safe_rmtree


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state")


def test_new_run_is_private(store: Store):
    run = store.new_run()
    assert stat.S_IMODE(os.stat(run.root).st_mode) == 0o700
    assert run.ws_up.is_dir() and run.tmp_lower.is_dir()
    assert store.run(run.run_id) == run


def test_run_id_validation(store: Store):
    for bad in ["../x", "abc", "12-zz", "12-abcd1234/.."]:
        with pytest.raises(TokenError):
            store.run(bad)


def test_redeem_is_single_use(store: Store):
    run = store.new_run()
    token = store.authorize(run, session_id="s", decision="allow")
    assert store.load_meta(run)["status"] == "authorized"
    assert store.redeem(run.run_id, token, ttl_s=60) == run
    with pytest.raises(TokenError, match="applying"):
        store.redeem(run.run_id, token, ttl_s=60)


def test_redeem_rejects_bad_token_and_expired(store: Store):
    run = store.new_run()
    token = store.authorize(run, session_id="s", decision="ask")
    assert store.load_meta(run)["status"] == "pending"
    with pytest.raises(TokenError, match="bad token"):
        store.redeem(run.run_id, "nope", ttl_s=60)
    store.save_meta(run, created=time.time() - 3600)
    with pytest.raises(TokenError, match="expired"):
        store.redeem(run.run_id, token, ttl_s=60)


def test_sessions_request_and_ledger(store: Store):
    store.set_request("abc-123", "clean the build dir")
    assert store.get_request("abc-123") == "clean the build dir"
    assert store.get_request("other") is None
    store.set_request("../../weird id", "x")
    assert store.get_request("../../weird id") == "x"
    assert not any(p.name.startswith("..") for p in store.sessions_dir.iterdir())
    store.ledger_add("abc-123", "/w", ["gen/a.txt", "gen"])
    store.ledger_add("abc-123", "/w", ["b.txt"])
    assert store.ledger("abc-123", "/w") == {"gen/a.txt", "gen", "b.txt"}
    assert store.ledger("abc-123", "/other") == set()


def test_log_decision_appends_jsonl(store: Store):
    store.log_decision({"run_id": "r1", "decision": "allow"})
    store.log_decision({"run_id": "r2", "decision": "ask"})
    lines = store.log_path.read_text().splitlines()
    assert [json.loads(x)["run_id"] for x in lines] == ["r1", "r2"]


def test_cleanup_removes_expired_keeps_fresh(store: Store):
    old = store.new_run()
    store.authorize(old, session_id="s", decision="ask")
    store.save_meta(old, created=time.time() - 3600)
    fresh = store.new_run()
    store.authorize(fresh, session_id="s", decision="ask")
    removed = store.cleanup(ttl_s=900)
    assert removed == [old.run_id]
    assert fresh.root.exists() and not old.root.exists()


def test_safe_rmtree_handles_mode0_and_never_follows_symlinks(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    os.chmod(outside, 0o555)
    runs = tmp_path / "runs"
    victim = runs / "r"
    (victim / "locked" / "inner").mkdir(parents=True)
    (victim / "locked" / "inner" / "f").write_text("f")
    (victim / "link").symlink_to(outside)
    os.chmod(victim / "locked" / "inner", 0)
    os.chmod(victim / "locked", 0)
    safe_rmtree(victim, within=runs)
    assert not victim.exists()
    assert (outside / "keep").exists() and stat.S_IMODE(os.stat(outside).st_mode) == 0o555
    os.chmod(outside, 0o755)
    with pytest.raises(PermissionError):
        safe_rmtree(outside, within=runs)
