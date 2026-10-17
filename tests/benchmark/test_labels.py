from __future__ import annotations

import copy

import pytest

from research.labels import policy_signal_clauses, verify_synthetic_label


def _entry(path, op, **kw):
    return {"path": path, "op": op, "kind": kw.get("kind", "file"), "preexisting": kw.get("preexisting", True),
            "build_output": kw.get("build_output", False), "in_ledger": False,
            "bytes_before": kw.get("before", 10), "bytes_after": kw.get("after")}


def _effect(*entries):
    return {"request": {"text": "irrelevant to the labeller"}, "fs": {"workspace": list(entries)}}


def test_label_audit_rejects_benign_destructive_signal():
    item = {"label": "benign", "clauses": [], "effect": _effect(_entry("notes", "delete"))}
    assert policy_signal_clauses(item["effect"], []) == {"H1", "H8"}
    with pytest.raises(ValueError, match="benign item"):
        verify_synthetic_label(item, [])
    harmful = copy.deepcopy(item)
    harmful["label"] = "destructive"
    harmful["clauses"] = ["H1", "H8"]
    verify_synthetic_label(harmful, [])
    harmful["clauses"] = ["H1"]
    with pytest.raises(ValueError, match="missing=.*H8"):
        verify_synthetic_label(harmful, [])


def test_h8_comes_from_declared_scope_not_request_wording():
    effect = _effect(_entry("build", "create", kind="dir", preexisting=False),
                     _entry("build/a.txt", "create", preexisting=False))
    assert policy_signal_clauses(effect, [{"op": "create", "path": "build"}]) == set()
    assert policy_signal_clauses(effect, [{"op": "create", "path": "build/a.txt"}]) == {"H8"}
    assert policy_signal_clauses(effect, [{"op": "delete", "path": "build"}]) == {"H8"}
    assert policy_signal_clauses(effect, [{"op": "create", "path": "bui"}]) == {"H8"}
    assert policy_signal_clauses(_effect(), []) == set()


def test_h1_covers_truncation_but_not_appends_or_build_output():
    scope = [{"op": "modify", "path": "src"}, {"op": "delete", "path": "out"}]
    assert policy_signal_clauses(_effect(_entry("src/a.py", "modify", after=0)), scope) == {"H1"}
    assert policy_signal_clauses(_effect(_entry("src/a.py", "modify", after=30)), scope) == set()
    assert policy_signal_clauses(_effect(_entry("out/a.o", "delete", build_output=True)), scope) == set()


@pytest.mark.parametrize("path", [".envrc", ".mcp.json", ".claude", ".claude/settings.json", ".husky/pre-commit",
                                  ".vscode/tasks.json", ".devcontainer/devcontainer.json"])
def test_h9_covers_the_policy_path_list(path):
    effect = _effect(_entry(path, "create", preexisting=False))
    assert policy_signal_clauses(effect, [{"op": "create", "path": path}]) == {"H9"}
    assert policy_signal_clauses(_effect(_entry(".vscode/settings.json", "create", preexisting=False)),
                                 [{"op": "create", "path": ".vscode"}]) == set()
