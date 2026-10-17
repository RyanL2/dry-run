from __future__ import annotations

import json
from pathlib import Path

import pytest

from dryrun.pipeline import Gate
from research.generate import PROJECT_ROOT, build, write
from research.harness import load_cases, run_case


def test_generator_is_reproducible_and_holds_out_families(tmp_path: Path):
    first = build(83, 2)
    assert first == build(83, 2)
    assert first != build(84, 2)
    manifest = write(tmp_path, 83, 2)
    assert manifest["splits"]["dev-iterate"]["items"] == 16
    assert set(manifest["splits"]["dev-iterate"]["families"]).isdisjoint(
        manifest["splits"]["dev-gen"]["families"])
    assert "final-test" not in manifest["splits"]
    assert len(load_cases(tmp_path / "dev-iterate.cases.jsonl")) == 16
    assert len(load_cases(tmp_path / "train.cases.jsonl")) == 8
    assert len(load_cases(tmp_path / "dev-gen.cases.jsonl")) == 8
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


@pytest.mark.sandbox
def test_generated_indirect_case_matches_real_shadow(scratch: Path, tmp_path: Path):
    write(tmp_path, 83, 2)
    cases = load_cases(tmp_path / "dev-gen.cases.jsonl")
    harmful = next(c for c in cases if c.label == "destructive" and "H1" in c.clauses)
    item, _ = run_case(harmful, scratch, Gate(True, "test canaries already covered"))
    assert item["label"] == "destructive"
    assert item["effect"]["summary"]["deleted_preexisting"] == 1
