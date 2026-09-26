"""Evaluate dev-iterate predictions against the frozen metric implementation.

Usage: python -m research.evaluate items.jsonl predictions.jsonl [--kind score|decision]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from research.frozen.evaluator import (evaluate_decisions, evaluate_scores, load_dev_iterate,
                                       load_predictions, paired_miss_delta_ci, threshold_at_ask5)

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "research" / "frozen.lock.json"


def verify_frozen() -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    for relative, expected in lock["sha256"].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f"invalid frozen path: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"frozen evaluator changed: {relative}")


def _escalations(rows, predictions, kind):
    if kind == "decision":
        return {r.item_id: predictions[r.item_id] != "allow" for r in rows if r.fold == "evaluation"}
    threshold = threshold_at_ask5([r for r in rows if r.fold == "calibration"], predictions)
    return {r.item_id: predictions[r.item_id] >= threshold for r in rows if r.fold == "evaluation"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", type=Path, help="dev-iterate JSONL items")
    parser.add_argument("predictions", type=Path, help="JSONL predictions, one per item")
    parser.add_argument("--kind", choices=("score", "decision"), default="score")
    parser.add_argument("--baseline", type=Path, help="paired baseline predictions for a repo-clustered CI")
    parser.add_argument("--baseline-kind", choices=("score", "decision"), default="score")
    args = parser.parse_args(argv)
    verify_frozen()
    rows = load_dev_iterate(args.items)
    predictions = load_predictions(args.predictions, rows, kind=args.kind)
    result = (evaluate_scores(rows, predictions) if args.kind == "score"
              else evaluate_decisions(rows, predictions))
    if args.baseline:
        baseline = load_predictions(args.baseline, rows, kind=args.baseline_kind)
        result["paired_candidate_minus_baseline"] = paired_miss_delta_ci(
            rows, _escalations(rows, baseline, args.baseline_kind), _escalations(rows, predictions, args.kind))
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
