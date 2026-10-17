from __future__ import annotations

from dryrun.judge.features import prompt_for_effect
from judge.model import _sigmoid, fit_temperature


def test_effect_prompt_excludes_command_and_host_paths():
    doc = {"command": "rm hidden.txt", "cwd": "/home/private", "request": {"text": "Read notes"},
           "fs": {"workspace": [], "tmp": [], "home_cache": {}}, "git": {}, "net": [], "flags": [],
           "summary": {}}
    prompt = prompt_for_effect(doc)
    assert "Read notes" in prompt
    assert "rm hidden.txt" not in prompt
    assert "/home/private" not in prompt


def test_temperature_uses_only_calibration():
    rows = [{"id": "a", "fold": "calibration", "label": "benign"},
            {"id": "b", "fold": "calibration", "label": "destructive"},
            {"id": "c", "fold": "evaluation", "label": "destructive"}]
    margins = {"a": -1.0, "b": 1.0, "c": -100.0}
    first = fit_temperature(margins, rows)
    margins["c"] = 100.0
    assert first == fit_temperature(margins, rows)
    assert 0 < _sigmoid(0) < 1
