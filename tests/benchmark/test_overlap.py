from __future__ import annotations

from research.overlap import audit


def _item(request: str, label: str, path: str = "a") -> dict:
    return {"label": label, "effect": {"request": {"text": request}, "fs": {"workspace": [{"path": path}]},
                                       "git": {}, "net": {}, "flags": [], "summary": {}}}


def test_audit_flags_verbatim_prompts_and_a_leaking_word():
    train = [_item("Read a without changing it", "destructive")]
    leaky = [_item("Read a without changing it", "destructive"), _item("Clean b", "benign", "b"),
             _item("Read c without changing it", "destructive", "c"), _item("Clean d", "benign", "d")]
    report = audit(train, leaky)
    assert report["prompt_overlap"] == 1
    assert report["best_word_rule_accuracy"] == 1.0
    balanced = [_item("Clean e", "benign", "e"), _item("Clean e", "destructive", "f")]
    report = audit(train, balanced)
    assert (report["prompt_overlap"], report["best_word_rule_accuracy"]) == (0, 0.5)
