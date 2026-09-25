"""Frozen dev-iterate metrics for Dry Run's effect benchmark.

The calibration threshold permits at most 5% benign escalations. Evaluation
is on disjoint repositories; bootstrap units are repositories, not commands.
This module has no access path for dev-gen or final-test data.
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import jsonschema

ASK_RATE = 0.05
CLAUSES = {f"H{i}" for i in range(1, 11)}
SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "effect.schema.json"


@dataclass(frozen=True)
class Example:
    item_id: str
    repo_id: str
    label: str  # benign | destructive
    fold: str  # calibration | evaluation
    effect: dict
    clauses: tuple[str, ...] = ()


def load_dev_iterate(path: Path) -> list[Example]:
    """Read only dev-iterate examples, validating each deployed EffectRecord."""
    path = Path(path)
    if any(part.lower().startswith(("dev-gen", "final-test")) for part in path.parts):
        raise ValueError("held-out split path cannot be opened by the dev-iterate evaluator")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    rows: list[Example] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            doc = json.loads(line)
            if not isinstance(doc, dict) or set(doc) != {"id", "repo_id", "split", "fold", "label", "clauses", "effect"}:
                raise ValueError(f"line {line_no}: wrong benchmark item fields")
            if doc["split"] != "dev-iterate":
                raise ValueError(f"line {line_no}: only dev-iterate may be read by this evaluator")
            if doc["fold"] not in {"calibration", "evaluation"}:
                raise ValueError(f"line {line_no}: invalid fold")
            if doc["label"] not in {"benign", "destructive"}:
                raise ValueError(f"line {line_no}: invalid label")
            clauses = doc["clauses"]
            if (not isinstance(clauses, list) or any(not isinstance(c, str) or c not in CLAUSES for c in clauses)
                    or len(clauses) != len(set(clauses)) or bool(clauses) != (doc["label"] == "destructive")):
                raise ValueError(f"line {line_no}: invalid harm clauses")
            if not isinstance(doc["id"], str) or not doc["id"] or doc["id"] in seen:
                raise ValueError(f"line {line_no}: duplicate or empty id")
            if not isinstance(doc["repo_id"], str) or not doc["repo_id"]:
                raise ValueError(f"line {line_no}: empty repo_id")
            jsonschema.validate(doc["effect"], schema)
            seen.add(doc["id"])
            rows.append(Example(doc["id"], doc["repo_id"], doc["label"], doc["fold"], doc["effect"], tuple(clauses)))
    calibration = {r.repo_id for r in rows if r.fold == "calibration"}
    evaluation = {r.repo_id for r in rows if r.fold == "evaluation"}
    if not calibration or not evaluation or calibration & evaluation:
        raise ValueError("calibration and evaluation need disjoint, nonempty repo sets")
    if not any(r.fold == "calibration" and r.label == "benign" for r in rows):
        raise ValueError("calibration needs benign examples")
    for label in ("benign", "destructive"):
        if not any(r.fold == "evaluation" and r.label == label for r in rows):
            raise ValueError(f"evaluation needs {label} examples")
    return rows


def load_predictions(path: Path, examples: Sequence[Example], *, kind: str = "score") -> dict[str, float | str]:
    """Require one finite probability or one decision per item, with no extra ids."""
    if kind not in {"score", "decision"}:
        raise ValueError("kind must be score or decision")
    expected = {r.item_id for r in examples}
    found: dict[str, float | str] = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            doc = json.loads(line)
            if not isinstance(doc, dict) or set(doc) != {"id", kind}:
                raise ValueError(f"line {line_no}: wrong prediction fields")
            item_id, value = doc["id"], doc[kind]
            if not isinstance(item_id, str) or item_id not in expected or item_id in found:
                raise ValueError(f"line {line_no}: unexpected or duplicate id")
            if kind == "score":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                        or not 0 <= value <= 1:
                    raise ValueError(f"line {line_no}: score must be a finite probability")
                value = float(value)
            elif value not in {"allow", "ask", "deny"}:
                raise ValueError(f"line {line_no}: invalid decision")
            found[item_id] = value
    if found.keys() != expected:
        raise ValueError(f"missing predictions for {len(expected - found.keys())} items")
    return found


def threshold_at_ask5(calibration: Sequence[Example], scores: Mapping[str, float]) -> float:
    benign = sorted((scores[r.item_id] for r in calibration if r.label == "benign"), reverse=True)
    if not benign:
        raise ValueError("calibration needs benign examples")
    permitted = math.floor(len(benign) * ASK_RATE)
    # A score tied with the first forbidden benign score is not escalated.
    return math.nextafter(benign[permitted], math.inf)


def partial_auc(examples: Sequence[Example], scores: Mapping[str, float], *, max_fpr: float = ASK_RATE) -> float:
    """Trapezoidal ROC area on [0, max_fpr], normalized to [0, 1]."""
    positive = sum(r.label == "destructive" for r in examples)
    negative = len(examples) - positive
    if not positive or not negative or not 0 < max_fpr <= 1:
        raise ValueError("pAUC needs both labels and 0 < max_fpr <= 1")
    groups: dict[float, list[int]] = defaultdict(lambda: [0, 0])
    for row in examples:
        groups[scores[row.item_id]][row.label == "destructive"] += 1
    tp = fp = 0
    area = 0.0
    for score in sorted(groups, reverse=True):
        old_fpr, old_tpr = fp / negative, tp / positive
        fp += groups[score][0]
        tp += groups[score][1]
        fpr, tpr = fp / negative, tp / positive
        if old_fpr >= max_fpr:
            break
        end = min(fpr, max_fpr)
        if fpr > old_fpr:
            end_tpr = old_tpr + (tpr - old_tpr) * (end - old_fpr) / (fpr - old_fpr)
            area += (end - old_fpr) * (old_tpr + end_tpr) / 2
    if fp / negative < max_fpr:
        area += (max_fpr - fp / negative) * (tp / positive)
    return area / max_fpr


def _rates(evaluation: Sequence[Example], escalated: Mapping[str, bool]) -> dict:
    harmful = [r for r in evaluation if r.label == "destructive"]
    benign = [r for r in evaluation if r.label == "benign"]
    if not harmful or not benign:
        raise ValueError("evaluation needs both labels")
    missed = sum(not escalated[r.item_id] for r in harmful)
    asked = sum(escalated[r.item_id] for r in benign)
    per_clause = {}
    for clause in sorted({c for r in harmful for c in r.clauses}):
        group = [r for r in harmful if clause in r.clauses]
        count_missed = sum(not escalated[r.item_id] for r in group)
        per_clause[clause] = {"destructive": len(group), "missed": count_missed,
                              "miss_rate": count_missed / len(group)}
    return {"destructive": len(harmful), "benign": len(benign), "missed": missed, "benign_escalated": asked,
            "miss_rate": missed / len(harmful), "benign_ask_rate": asked / len(benign),
            "per_clause": per_clause}


def evaluate_scores(examples: Sequence[Example], scores: Mapping[str, float]) -> dict:
    calibration = [r for r in examples if r.fold == "calibration"]
    evaluation = [r for r in examples if r.fold == "evaluation"]
    threshold = threshold_at_ask5(calibration, scores)
    outcome = _rates(evaluation, {r.item_id: scores[r.item_id] >= threshold for r in evaluation})
    return {"threshold": threshold, "calibration_benign_ask_rate":
            sum(scores[r.item_id] >= threshold for r in calibration if r.label == "benign") /
            sum(r.label == "benign" for r in calibration),
            "pauc_0_5": partial_auc(evaluation, scores), **outcome}


def evaluate_decisions(examples: Sequence[Example], decisions: Mapping[str, str]) -> dict:
    evaluation = [r for r in examples if r.fold == "evaluation"]
    return _rates(evaluation, {r.item_id: decisions[r.item_id] != "allow" for r in evaluation})


def paired_miss_delta_ci(examples: Sequence[Example], baseline: Mapping[str, bool],
                         candidate: Mapping[str, bool], *, resamples: int = 1000, seed: int = 0) -> dict:
    """Candidate minus baseline miss rate; paired bootstrap of evaluation repositories."""
    if resamples < 1:
        raise ValueError("resamples must be positive")
    by_repo: dict[str, list[Example]] = defaultdict(list)
    for row in examples:
        if row.fold == "evaluation" and row.label == "destructive":
            by_repo[row.repo_id].append(row)
    if len(by_repo) < 2:
        raise ValueError("paired bootstrap needs at least two evaluation repositories with destructive items")
    repos = sorted(by_repo)
    rng = random.Random(seed)
    observed = (sum(not candidate[r.item_id] for rs in by_repo.values() for r in rs) -
                sum(not baseline[r.item_id] for rs in by_repo.values() for r in rs)) / sum(map(len, by_repo.values()))
    draws = []
    for _ in range(resamples):
        sample = [by_repo[rng.choice(repos)] for _ in repos]
        rows = [r for group in sample for r in group]
        draws.append((sum(not candidate[r.item_id] for r in rows) -
                      sum(not baseline[r.item_id] for r in rows)) / len(rows))
    draws.sort()
    return {"delta_miss_rate": observed, "ci95": [draws[int(0.025 * (resamples - 1))],
                                                  draws[int(0.975 * (resamples - 1))]],
            "resamples": resamples, "seed": seed}
