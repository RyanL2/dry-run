"""Leakage audit to run before scoring a judge on generated items.

Reports how many evaluation model prompts occur verbatim in training, and the
accuracy of the best one-word request rule ("destructive iff the request
contains / lacks word w"). The 2026-09-26 audit found 160/160 prompt overlap and
a one-line text rule at 160/160; a usable split needs zero overlap and a
request-only rule near chance. Exits 1 on any prompt overlap.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from dryrun.judge.features import prompt_for_effect


def _items(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def best_word_rule(items: list[dict]) -> tuple[str, float]:
    words = [set(re.findall(r"[a-z]+", (i["effect"]["request"]["text"] or "").lower())) for i in items]
    harmful = [i["label"] == "destructive" for i in items]
    best = ("", max(sum(harmful), len(items) - sum(harmful)) / len(items))  # constant rule
    for word in set().union(*words):
        hits = sum((word in w) == h for w, h in zip(words, harmful))
        acc = max(hits, len(items) - hits) / len(items)
        if acc > best[1]:
            best = (word, acc)
    return best


def audit(train: list[dict], evaluation: list[dict]) -> dict:
    seen = {prompt_for_effect(i["effect"]) for i in train}
    overlap = sum(prompt_for_effect(i["effect"]) in seen for i in evaluation)
    word, acc = best_word_rule(evaluation)
    return {"eval_items": len(evaluation), "prompt_overlap": overlap,
            "distinct_eval_prompts": len({prompt_for_effect(i["effect"]) for i in evaluation}),
            "best_word_rule": word, "best_word_rule_accuracy": round(acc, 4)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("train", type=Path)
    parser.add_argument("evaluation", type=Path, nargs="+")
    args = parser.parse_args(argv)
    train = _items(args.train)
    report = {str(p): audit(train, _items(p)) for p in args.evaluation}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if any(r["prompt_overlap"] for r in report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
