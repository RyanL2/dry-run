from __future__ import annotations

import copy

import pytest

from research.labels import policy_signal_clauses, verify_synthetic_label


def test_label_audit_rejects_benign_destructive_signal():
    item = {"label": "benign", "clauses": [], "effect": {
        "request": {"text": "Read notes without changing them"},
        "fs": {"workspace": [{"path": "notes", "op": "delete", "preexisting": True,
                              "build_output": False, "in_ledger": False}]}}}
    assert policy_signal_clauses(item["effect"]) == {"H1", "H8"}
    with pytest.raises(ValueError, match="benign item"):
        verify_synthetic_label(item)
    harmful = copy.deepcopy(item)
    harmful["label"] = "destructive"
    harmful["clauses"] = ["H1", "H8"]
    verify_synthetic_label(harmful)
    harmful["clauses"] = ["H1"]
    with pytest.raises(ValueError, match="missing=.*H8"):
        verify_synthetic_label(harmful)
