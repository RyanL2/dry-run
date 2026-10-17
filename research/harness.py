"""Run labelled benchmark fixtures through the production Dry Run shadow pipeline.

The fixture setup only creates regular files in a temporary workspace. Commands
run in the same bwrap sandbox as production. The CLI requires the isolation
canaries to pass before it accepts any fixture commands.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dryrun.canary import run_gate
from dryrun.config import load_config
from dryrun.fingerprint import digest, fingerprint_tree
from dryrun.pipeline import Gate, Pipeline
from dryrun.store import Store
from research.labels import verify_synthetic_label

MAX_CASES = 5000
MAX_FILES = 100
MAX_FILE_BYTES = 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class Case:
    item_id: str
    repo_id: str
    split: str
    fold: str
    label: str
    clauses: tuple[str, ...]
    request: str
    command: str
    files: dict[str, str]
    expected: tuple[tuple[str, str, str], ...]  # area, op, path
    scope: tuple[tuple[str, str], ...] = ()  # (op, path prefix) the request consents to; empty = read-only


def _relpath(raw: str) -> Path:
    path = Path(raw)
    if (not raw or path.is_absolute() or raw in {".", ".."} or
            any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError(f"unsafe fixture path: {raw!r}")
    return path


def load_cases(path: Path) -> list[Case]:
    path = Path(path).resolve()
    if any(part.lower().replace("_", "-") in {"final-test", "final-test.cases.jsonl"} or
           part.lower().replace("_", "-").startswith("final-test.") for part in path.parts):
        raise ValueError("protected final-test fixture path cannot be opened by the ordinary harness")
    cases: list[Case] = []
    seen: set[str] = set()
    split_seen: str | None = None
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            doc = json.loads(line)
            want = {"id", "repo_id", "split", "fold", "label", "clauses", "request", "command", "files", "expected",
                    "scope"}
            if not isinstance(doc, dict) or set(doc) != want:
                raise ValueError(f"line {line_no}: wrong fixture fields")
            split, fold = doc["split"], doc["fold"]
            if split not in {"train", "dev-iterate", "dev-gen"} or (
                    split == "train" and fold != "train") or (
                    split != "train" and fold not in {"calibration", "evaluation"}):
                raise ValueError(f"line {line_no}: invalid or protected benchmark split/fold")
            if split_seen is not None and split_seen != split:
                raise ValueError("fixture file mixes benchmark splits")
            split_seen = split
            if doc["label"] not in {"benign", "destructive"}:
                raise ValueError(f"line {line_no}: invalid label")
            clauses = doc["clauses"]
            if (not isinstance(clauses, list) or any(not isinstance(c, str) or c not in {f"H{i}" for i in range(1, 11)} for c in clauses)
                    or len(clauses) != len(set(clauses)) or bool(clauses) != (doc["label"] == "destructive")):
                raise ValueError(f"line {line_no}: invalid clauses")
            item_id, repo_id = doc["id"], doc["repo_id"]
            if (not isinstance(item_id, str) or not SAFE_ID.fullmatch(item_id) or item_id in seen or
                    not isinstance(repo_id, str) or not repo_id):
                raise ValueError(f"line {line_no}: duplicate/empty id or repo_id")
            if not isinstance(doc["request"], str) or not isinstance(doc["command"], str) or not doc["command"]:
                raise ValueError(f"line {line_no}: invalid request or command")
            files = doc["files"]
            if not isinstance(files, dict) or len(files) > MAX_FILES:
                raise ValueError(f"line {line_no}: too many fixture files")
            for name, content in files.items():
                if not isinstance(name, str) or not isinstance(content, str):
                    raise ValueError(f"line {line_no}: files must map paths to text")
                _relpath(name)
                if len(content.encode()) > MAX_FILE_BYTES:
                    raise ValueError(f"line {line_no}: fixture file too large")
            expected = doc["expected"]
            if not isinstance(expected, list) or not expected:
                raise ValueError(f"line {line_no}: expected effects required")
            effects = []
            for item in expected:
                if (not isinstance(item, dict) or set(item) != {"area", "op", "path"} or
                        item["area"] not in {"workspace", "tmp"} or
                        item["op"] not in {"create", "modify", "delete", "mode"} or
                        not isinstance(item["path"], str)):
                    raise ValueError(f"line {line_no}: invalid expected effect")
                _relpath(item["path"])
                effects.append((item["area"], item["op"], item["path"]))
            scope = doc["scope"]
            if not isinstance(scope, list) or any(
                    not isinstance(s, dict) or set(s) != {"op", "path"} or
                    s["op"] not in {"create", "modify", "delete", "mode"} or not isinstance(s["path"], str)
                    for s in scope):
                raise ValueError(f"line {line_no}: invalid scope")
            for s in scope:
                _relpath(s["path"])
            seen.add(item_id)
            cases.append(Case(item_id, repo_id, split, fold, doc["label"], tuple(clauses),
                              doc["request"], doc["command"], files, tuple(effects),
                              tuple((s["op"], s["path"]) for s in scope)))
            if len(cases) > MAX_CASES:
                raise ValueError("too many cases")
    cal = {c.repo_id for c in cases if c.fold == "calibration"}
    ev = {c.repo_id for c in cases if c.fold == "evaluation"}
    if not cases:
        raise ValueError("empty fixture file")
    if split_seen == "dev-iterate" and (not cal or not ev or cal & ev):
        raise ValueError("fixture folds need disjoint, nonempty repo sets")
    return cases


def run_case(case: Case, parent: Path, gate: Gate) -> tuple[dict, dict]:
    """Return benchmark item and rules-only prediction; never apply the shadow ChangeSet."""
    if not SAFE_ID.fullmatch(case.item_id):
        raise ValueError("unsafe case id")
    root = Path(parent) / case.item_id
    root.mkdir()
    workspace, home, state = root / "workspace", root / "home", root / "state"
    workspace.mkdir()
    home.mkdir()
    for rel, content in case.files.items():
        target = workspace / _relpath(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    before = digest(fingerprint_tree(workspace))
    store = Store(state)
    pipeline = Pipeline(load_config(use_user_file=False), store, gate=gate, home=home)
    session_id = f"bench-{case.item_id}"
    pipeline.handle_prompt(session_id, case.request)
    decision = pipeline.handle_pretool({
        "op": "pretool", "session_id": session_id, "cwd": str(workspace), "command": case.command,
        "description": "", "transcript_path": "",
        "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home)}, "deadline_ms": 30000,
    })
    if digest(fingerprint_tree(workspace)) != before:
        raise RuntimeError(f"{case.item_id}: real fixture workspace changed during shadow execution")
    entry = json.loads(store.log_path.read_text(encoding="utf-8").splitlines()[-1])
    record = entry.get("record")
    if not isinstance(record, dict):
        raise RuntimeError(f"{case.item_id}: missing EffectRecord ({decision.reason})")
    if any(flag in {"timeout", "resource_limit", "incomplete_network", "unsupported_entry",
                    "lower_changed", "ro_write_blocked"} for flag in record["flags"]):
        raise RuntimeError(f"{case.item_id}: incomplete shadow effect")
    actual = {(area, e["op"], e["path"]) for area in ("workspace", "tmp") for e in record["fs"][area]}
    if actual != set(case.expected):
        raise RuntimeError(f"{case.item_id}: effect mismatch; missing={set(case.expected) - actual}, "
                           f"unexpected={actual - set(case.expected)}")
    item = {"id": case.item_id, "repo_id": case.repo_id, "split": case.split, "fold": case.fold,
            "label": case.label, "clauses": list(case.clauses), "effect": record}
    verify_synthetic_label(item, [{"op": op, "path": path} for op, path in case.scope])
    prediction = {"id": case.item_id, "decision": decision.decision}
    return item, prediction


def generate(cases_path: Path, items_path: Path, predictions_path: Path) -> int:
    cases = load_cases(cases_path)
    if len({Path(p).resolve() for p in (cases_path, items_path, predictions_path)}) != 3:
        raise ValueError("cases, items and prediction paths must be distinct")
    if os.geteuid() == 0:
        raise RuntimeError("benchmark harness must run as a non-root Linux user")
    if str(Path.home()).startswith("/mnt/"):
        raise RuntimeError("benchmark harness requires a native Linux filesystem")
    cfg = load_config(use_user_file=False)
    with tempfile.TemporaryDirectory(prefix="dryrun-benchmark-", dir=Path.home()) as temp:
        root = Path(temp)
        ok, detail = run_gate(cfg, Store(root / "gate-state"))
        if not ok:
            raise RuntimeError(f"isolation canaries failed: {detail}")
        items, predictions = [], []
        for case in cases:
            item, prediction = run_case(case, root, Gate(True, detail))
            items.append(item)
            predictions.append(prediction)
    for path, rows in ((Path(items_path), items), (Path(predictions_path), predictions)):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        os.replace(temp, path)
    return len(cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("items", type=Path)
    parser.add_argument("predictions", type=Path)
    args = parser.parse_args(argv)
    print(f"generated {generate(args.cases, args.items, args.predictions)} verified EffectRecords")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
