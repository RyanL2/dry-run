from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from dryrun.types import EffectRecord
from research import evaluate as cli
from research.frozen.evaluator import (Example, evaluate_decisions, evaluate_scores, load_dev_iterate,
                                       load_predictions, paired_miss_delta_ci, partial_auc, threshold_at_ask5)


def effect() -> dict:
    return EffectRecord(
        run_id="r", session_id="s", command="git status", cwd="/w", workspace_root="/w",
        request_text="check status", request_source="UserPromptSubmit", triage_class="git_read",
        triage_reason="git read", exit_code=0, wall_ms=1, timed_out=False,
        stdout_tail="", stderr_tail="",
    ).to_json()


def rows() -> list[Example]:
    record = effect()
    return [Example("c1", "repo-cal", "benign", "calibration", record),
            Example("c2", "repo-cal", "benign", "calibration", record),
            Example("d1", "repo-one", "destructive", "evaluation", record, ("H1",)),
            Example("b1", "repo-one", "benign", "evaluation", record),
            Example("d2", "repo-two", "destructive", "evaluation", record, ("H1", "H3")),
            Example("b2", "repo-two", "benign", "evaluation", record)]


def write_items(path: Path, examples: list[Example], *, split: str = "dev-iterate") -> None:
    path.write_text("".join(json.dumps({"id": r.item_id, "repo_id": r.repo_id, "split": split,
                                        "fold": r.fold, "label": r.label, "clauses": r.clauses,
                                        "effect": r.effect}) + "\n"
                            for r in examples), encoding="utf-8")


def write_predictions(path: Path, values: dict, kind: str) -> None:
    path.write_text("".join(json.dumps({"id": k, kind: v}) + "\n" for k, v in values.items()), encoding="utf-8")


def test_loader_validates_effect_schema_and_split_isolation(tmp_path: Path):
    path = tmp_path / "items.jsonl"
    write_items(path, rows())
    assert len(load_dev_iterate(path)) == 6
    write_items(path, rows(), split="dev-gen")
    with pytest.raises(ValueError, match="only dev-iterate"):
        load_dev_iterate(path)
    bad = rows()
    bad[-1] = Example("b2", "repo-cal", "benign", "evaluation", effect())
    write_items(path, bad)
    with pytest.raises(ValueError, match="disjoint"):
        load_dev_iterate(path)
    forbidden = tmp_path / "final-test.jsonl"
    with pytest.raises(ValueError, match="cannot be opened"):
        load_dev_iterate(forbidden)


def test_score_loader_requires_complete_finite_probabilities(tmp_path: Path):
    path = tmp_path / "pred.jsonl"
    predictions = {r.item_id: 0.5 for r in rows()}
    write_predictions(path, predictions, "score")
    assert len(load_predictions(path, rows())) == len(rows())
    predictions["b2"] = math.nan
    write_predictions(path, predictions, "score")
    with pytest.raises(ValueError, match="finite"):
        load_predictions(path, rows())


def test_threshold_treats_benign_ties_conservatively():
    record = effect()
    calibration = [Example(str(i), "repo-cal", "benign", "calibration", record) for i in range(20)]
    scores = {str(i): (0.9 if i == 0 else 0.8) for i in range(20)}
    threshold = threshold_at_ask5(calibration, scores)
    assert sum(v >= threshold for v in scores.values()) == 1


def test_perfect_scoring_and_rules_point():
    sample = rows()
    scores = {"c1": 0.2, "c2": 0.3, "d1": 0.9, "b1": 0.1, "d2": 0.8, "b2": 0.2}
    result = evaluate_scores(sample, scores)
    assert (result["miss_rate"], result["benign_ask_rate"], result["pauc_0_5"]) == (0, 0, 1)
    decisions = {"d1": "ask", "b1": "allow", "d2": "allow", "b2": "deny"}
    point = evaluate_decisions(sample, decisions)
    assert (point["miss_rate"], point["benign_ask_rate"]) == (0.5, 0.5)
    assert point["per_clause"]["H3"]["miss_rate"] == 1
    assert partial_auc([r for r in sample if r.fold == "evaluation"], scores) == 1


def test_partial_auc_treats_tied_scores_as_one_roc_step():
    record = effect()
    sample = [Example("d", "r1", "destructive", "evaluation", record, ("H1",)),
              Example("b", "r2", "benign", "evaluation", record)]
    assert partial_auc(sample, {"d": 0.5, "b": 0.5}) == pytest.approx(0.025)


def test_paired_bootstrap_resamples_repositories_together():
    sample = rows()
    baseline = {"d1": False, "d2": False}
    candidate = {"d1": True, "d2": True}
    ci = paired_miss_delta_ci(sample, baseline, candidate, resamples=1000, seed=7)
    assert ci["delta_miss_rate"] == -1
    assert ci["ci95"] == [-1, -1]


def test_cli_rejects_a_changed_frozen_evaluator(tmp_path: Path, monkeypatch):
    frozen = tmp_path / "frozen.py"
    frozen.write_text("baseline", encoding="utf-8")
    lock = tmp_path / "frozen.lock.json"
    lock.write_text(json.dumps({"sha256": {"frozen.py": hashlib.sha256(b"baseline").hexdigest()}}))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "LOCK", lock)
    cli.verify_frozen()
    frozen.write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        cli.verify_frozen()


def test_cli_evaluates_dev_iterate_with_real_frozen_lock(tmp_path: Path, capsys):
    items, predictions = tmp_path / "items.jsonl", tmp_path / "scores.jsonl"
    write_items(items, rows())
    write_predictions(predictions, {"c1": 0.2, "c2": 0.3, "d1": 0.9,
                                    "b1": 0.1, "d2": 0.8, "b2": 0.2}, "score")
    assert cli.main([str(items), str(predictions)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["miss_rate"] == 0
    assert result["per_clause"]["H3"]["missed"] == 0
