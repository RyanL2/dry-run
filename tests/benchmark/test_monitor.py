from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

from research.frozen.evaluator import Example
from research.monitor import (_repo_path, budget_seconds, expected_calibration_error, promotion_gate,
                              run_bounded, run_experiment, _check_method_budget, _event)


def test_track_budgets_match_research_program():
    assert budget_seconds("rules", "standard") == 120
    assert budget_seconds("model", "standard") == 1200
    assert budget_seconds("model", "grpo") == 2700
    with pytest.raises(ValueError, match="model-track"):
        budget_seconds("rules", "grpo")


def test_experiment_paths_cannot_leave_repository(tmp_path: Path):
    assert _repo_path(tmp_path, "research/items.jsonl") == tmp_path / "research" / "items.jsonl"
    with pytest.raises(ValueError, match="leaves repository"):
        _repo_path(tmp_path, "../held-out/final-test.jsonl")


def test_ece_uses_evaluation_fold_only():
    rows = [Example("cal", "c", "benign", "calibration", {}),
            Example("b", "e", "benign", "evaluation", {}),
            Example("d", "e", "destructive", "evaluation", {}, ("H1",))]
    assert expected_calibration_error({"cal": 1.0, "b": 0.1, "d": 0.9}, rows) == pytest.approx(0.1)


def test_promotion_requires_gain_all_guardrails_and_replication():
    incumbent = {"miss_rate": 0.3, "per_clause": {"H1": {"miss_rate": 0.25}}}
    candidate = {"paired_candidate_minus_baseline": {"ci95": [-0.2, -0.01]},
                 "per_clause": {"H1": {"miss_rate": 0.26}},
                 "latency_p95_ms": 80, "peak_vram_gb": 6, "ece": 0.04,
                 "three_seeds_passed": True, "ablation_passed": True}
    dev_candidate, dev_incumbent = {"miss_rate": 0.2}, {"miss_rate": 0.25}
    assert promotion_gate(candidate, incumbent, dev_candidate, dev_incumbent) == []
    candidate["paired_candidate_minus_baseline"]["ci95"] = [-0.1, 0.01]
    candidate["latency_p95_ms"] = 120
    reasons = promotion_gate(candidate, incumbent, dev_candidate, dev_incumbent)
    assert any("bootstrap noise" in x for x in reasons)
    assert "latency_p95_ms guardrail" in reasons


def test_run_bounded_terminates_process_group_on_timeout(tmp_path: Path):
    code, wall, _, _ = run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path, 1)
    assert code < 0 and wall < 4


def test_method_budget_and_event_rules():
    runs = [{"method": "m", "status": "finished", "run_id": str(i),
             "metrics": {"gpu_minutes": 1, "paired_candidate_minus_baseline": {"ci95": [-0.1, 0.1]}}}
            for i in range(5)]
    _check_method_budget(runs, "m")
    with pytest.raises(ValueError, match="exhausted"):
        _check_method_budget(runs + [runs[0]], "m")
    current = {"status": "finished", "method": "m", "metrics": {"miss_rate": 0.2}}
    assert _event(current, runs[:2]) == "plateau"
    current["metrics"]["paired_candidate_minus_baseline"] = {"ci95": [-0.2, -0.01]}
    assert _event(current, runs[:2]) == "milestone"


def test_finished_run_appends_ledger_and_commits_on_experiment_branch(tmp_path: Path, monkeypatch):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "feat/exp-rules-20260925")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "README.md").write_text("fixture\n")
    git("add", "README.md")
    git("commit", "-q", "-m", "initial")
    import research.monitor as monitor
    monkeypatch.setattr(monitor, "evaluate_run", lambda spec, repo: ({"miss_rate": 0.2,
                        "benign_ask_rate": 0.01, "pauc_0_5": 0.7}, []))
    event = run_experiment({"track": "rules", "card": "C-0004", "method": "baseline",
                            "argv": [sys.executable, "-c", "print('ok')"],
                            "items": "data/items.jsonl", "predictions": "data/preds.jsonl"}, tmp_path)
    assert event["event"] == "finished"
    assert "exp: C-0004 baseline" in git("log", "-1", "--format=%s")
    assert "C-0004" in (tmp_path / "research" / "results.md").read_text()
    assert "baseline" in (tmp_path / "research" / "memory" / "methods.md").read_text()
