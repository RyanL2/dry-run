from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from dryrun.pipeline import Gate
from research.harness import load_cases, run_case

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "research" / "fixtures" / "smoke_cases.jsonl"


def test_fixture_loader_rejects_escaping_paths(tmp_path: Path):
    rows = [json.loads(line) for line in SMOKE.read_text(encoding="utf-8").splitlines()]
    rows[0]["files"] = {"../outside": "bad"}
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe fixture path"):
        load_cases(path)
    rows[0]["files"] = {}
    rows[0]["id"] = "../outside"
    path.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate/empty"):
        load_cases(path)


@pytest.mark.sandbox
def test_smoke_cases_produce_observed_records_without_real_changes(scratch: Path):
    schema = json.loads((ROOT / "schemas" / "effect.schema.json").read_text(encoding="utf-8"))
    cases = load_cases(SMOKE)
    assert len(cases) == 4
    for case in cases:
        item, prediction = run_case(case, scratch, Gate(True, "test canaries already covered"))
        jsonschema.validate(item["effect"], schema)
        assert item["label"] == case.label
        assert prediction["decision"] in {"allow", "ask", "deny"}
        assert item["effect"]["request"]["text"] == case.request
