from __future__ import annotations

from pathlib import Path

from dryrun.config import load_config
from dryrun.pipeline import Gate, Pipeline
from dryrun.store import Store


def pipe(tmp_path: Path, gate_ok=True) -> Pipeline:
    return Pipeline(load_config(use_user_file=False), Store(tmp_path / "state"), gate=Gate(gate_ok, "stub"),
                    home=tmp_path / "home")


def req(cmd: str, cwd: Path) -> dict:
    return {"op": "pretool", "session_id": "s", "cwd": str(cwd), "command": cmd, "description": "",
            "transcript_path": "", "env": {}, "deadline_ms": 5000}


def test_read_only_passthrough(tmp_path: Path):
    d = pipe(tmp_path).handle_pretool(req("ls -la", tmp_path))
    assert (d.decision, d.mode) == ("allow", "passthrough")


def test_non_shadowable_asks(tmp_path: Path):
    d = pipe(tmp_path).handle_pretool(req("git push --force", tmp_path))
    assert (d.decision, d.rule_ids) == ("ask", ["T1.git_push"])


def test_agent_typed_apply_denied_but_valid_token_allowed(tmp_path: Path):
    p = pipe(tmp_path)
    assert p.handle_pretool(req("dryrun apply 68d2f1a3-0badc0de --token x", tmp_path)).decision == "deny"
    run = p.store.new_run()
    token = p.store.authorize(run, session_id="s", decision="allow")
    d = p.handle_pretool(req(f"dryrun apply {run.run_id} --token {token}", tmp_path))
    assert d.decision == "allow"


def test_closed_gate_asks_for_shadow_class(tmp_path: Path):
    (tmp_path / "ws").mkdir()
    d = pipe(tmp_path, gate_ok=False).handle_pretool(req("rm -rf build", tmp_path / "ws"))
    assert d.decision == "ask" and "self-test" in d.reason


def test_decisions_are_logged(tmp_path: Path):
    p = pipe(tmp_path)
    p.handle_pretool(req("ls", tmp_path))
    assert '"decision": "allow"' in p.store.log_path.read_text()
