from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import pytest

from dryrun.pipeline import Gate
from research.generate import FAMILIES, PROJECT_ROOT, SCENARIOS, SPLIT_INDEX, Repo, build, case_row, write
from research.harness import load_cases, run_case


def test_generator_is_reproducible_and_holds_out_families(tmp_path: Path):
    first = build(83, 2)
    assert first == build(83, 2)
    assert first != build(84, 2)
    manifest = write(tmp_path, 83, 2)
    assert manifest["splits"]["dev-iterate"]["items"] == 32
    assert set(manifest["splits"]["dev-iterate"]["families"]).isdisjoint(
        manifest["splits"]["dev-gen"]["families"])
    assert "final-test" not in manifest["splits"]
    assert len(load_cases(tmp_path / "dev-iterate.cases.jsonl")) == 32
    assert len(load_cases(tmp_path / "train.cases.jsonl")) == 16
    assert len(load_cases(tmp_path / "dev-gen.cases.jsonl")) == 16
    assert json.loads((tmp_path / "manifest.json").read_text()) == manifest
    private = tmp_path / "private"
    release = write(private, 83, 2, final_test_seed=111)
    assert set(release["splits"]) == {"final-test"}
    assert release["final_test_seed"] == 111
    assert not (private / "dev-iterate.cases.jsonl").exists()
    assert set(manifest["splits"]["dev-gen"]["families"]).isdisjoint(
        release["splits"]["final-test"]["families"])
    with pytest.raises(ValueError, match="protected"):
        load_cases(private / "final-test.cases.jsonl")
    with pytest.raises(ValueError, match="cannot be opened"):
        load_cases(tmp_path / "final_test.cases.jsonl")
    with pytest.raises(ValueError, match="empty"):
        write(private, 83, 2, final_test_seed=112)
    with pytest.raises(ValueError, match="outside the repository"):
        write(PROJECT_ROOT / "research" / "generated" / "final-test", 83, 2,
              final_test_seed=112)


def test_requests_do_not_repeat_across_splits_or_give_away_the_label():
    splits = build(5, 20, final_test_seed=6)
    requests = {split: {r["request"] for r in rows} for split, rows in splits.items()}
    names = sorted(requests)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert requests[a].isdisjoint(requests[b]), (a, b)
    for split, rows in splits.items():
        by_kind = Counter((r["kind"], r["label"]) for r in rows)
        for kind in SCENARIOS:
            assert by_kind[(kind, "benign")] and by_kind[(kind, "destructive")], (split, kind)
        # The same request kind maps to both labels, and no single template decides the label.
        by_template = {}
        for r in rows:
            words = r["request"].split()
            by_template.setdefault((r["kind"], words[0]), set()).add(r["label"])
        assert sum(len(v) == 2 for v in by_template.values()) >= len(by_template) // 2, split


def _all_scenario_rows():
    rows, families = [], sorted(FAMILIES)
    for split in SPLIT_INDEX:
        for kind in sorted(SCENARIOS):
            for label in ("benign", "destructive"):
                for n, scenario in enumerate(SCENARIOS[kind][label]):
                    rng = random.Random(f"{split}/{kind}/{label}/{n}")
                    family = families[len(rows) % len(families)]
                    row = case_row(split, "evaluation", len(rows), 0, family, Repo(rng, split), rng,
                                   kind, label, scenario)
                    rows.append({**row, "split": "dev-gen", "id": f"x{len(rows):03d}-{split}"})
    return rows


@pytest.mark.sandbox
def test_every_scenario_matches_the_real_shadow_in_every_split_vocabulary(scratch: Path, tmp_path: Path):
    rows = _all_scenario_rows()
    path = tmp_path / "all.cases.jsonl"
    path.write_text("".join(json.dumps({k: v for k, v in r.items() if k not in {"family", "kind"}}) + "\n"
                            for r in rows))
    for case in load_cases(path):
        item, _ = run_case(case, scratch, Gate(True, "test canaries already covered"))
        assert item["label"] == case.label
