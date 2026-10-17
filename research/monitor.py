"""Bounded experiment runner and event-driven research ledger.

Run specs are JSON files with an argv array, never shell text. Each run is
committed on a feat/exp-<track>-* branch together with its ledger entry. The
branch prefix follows the feat/ branch naming convention.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from research.evaluate import verify_frozen
from research.frozen.evaluator import (evaluate_decisions, evaluate_scores, load_dev_iterate,
                                       load_predictions, paired_miss_delta_ci, threshold_at_ask5)

TRACKS = {"rules": 120, "model": 1200, "sandbox-perf": 1200}
EDITABLE = {
    "rules": ("src/dryrun/judge/rules.py", "src/dryrun/data/policy.yaml"),
    "model": ("judge/train_config.yaml", "judge/model.py", "judge/data_mix.yaml"),
    "sandbox-perf": ("src/dryrun/sandbox/", "src/dryrun/effects/"),
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _repo_path(repo: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("experiment path must be a nonempty string")
    path = (repo / raw).resolve()
    if not path.is_relative_to(repo):
        raise ValueError(f"experiment path leaves repository: {raw}")
    return path


def _allowed(path: str, track: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in EDITABLE[track]) or path in {
        "research/memory/ledger.jsonl", "research/memory/methods.md", "research/results.md"} or path.startswith(
        "research/runs/")


def check_experiment_branch(repo: Path, track: str) -> str:
    if track not in TRACKS:
        raise ValueError(f"unknown track: {track}")
    branch = _git(repo, "branch", "--show-current")
    if not branch.startswith(f"feat/exp-{track}-"):
        raise ValueError(f"experiments must run on feat/exp-{track}-* (current: {branch})")
    changed = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    bad = [line[3:] for line in changed if not _allowed(line[3:], track)]
    if bad:
        raise ValueError(f"files outside {track}'s editable surface changed: {bad[:5]}")
    return branch


def budget_seconds(track: str, objective: str) -> int:
    if track not in TRACKS or objective not in {"standard", "grpo"}:
        raise ValueError("unknown track or objective")
    if objective == "grpo" and track != "model":
        raise ValueError("GRPO is a model-track objective")
    return 2700 if objective == "grpo" else TRACKS[track]


def run_bounded(argv: list[str], cwd: Path, timeout_s: int) -> tuple[int, float, str, str]:
    """Kill the whole process group on timeout and cap retained output."""
    if not argv or any(not isinstance(a, str) or not a for a in argv):
        raise ValueError("argv must be a nonempty list of strings")
    start = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=err, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            code = -signal.SIGKILL
        wall = time.monotonic() - start
        out.seek(0)
        err.seek(0)
        return code, wall, out.read(2048).decode(errors="replace"), err.read(2048).decode(errors="replace")


def expected_calibration_error(scores: dict[str, float], rows) -> float:
    """Ten equal-width bins over evaluation items; scores are P(destructive)."""
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    evaluation = [r for r in rows if r.fold == "evaluation"]
    for row in evaluation:
        score = scores[row.item_id]
        bins[min(int(score * 10), 9)].append((score, int(row.label == "destructive")))
    n = len(evaluation)
    return sum(len(group) / n * abs(sum(s for s, _ in group) / len(group) -
                                    sum(y for _, y in group) / len(group))
               for group in bins if group)


def _escalations(rows, predictions, kind):
    if kind == "decision":
        return {r.item_id: predictions[r.item_id] != "allow" for r in rows if r.fold == "evaluation"}
    threshold = threshold_at_ask5([r for r in rows if r.fold == "calibration"], predictions)
    return {r.item_id: predictions[r.item_id] >= threshold for r in rows if r.fold == "evaluation"}


def evaluate_run(spec: dict, repo: Path) -> tuple[dict, list[dict]]:
    rows = load_dev_iterate(_repo_path(repo, spec["items"]))
    kind = spec.get("kind", "score")
    candidate = load_predictions(_repo_path(repo, spec["predictions"]), rows, kind=kind)
    metrics = evaluate_scores(rows, candidate) if kind == "score" else evaluate_decisions(rows, candidate)
    if kind == "score":
        metrics["ece"] = expected_calibration_error(candidate, rows)
    clips: list[dict] = []
    if "baseline" in spec:
        base_kind = spec.get("baseline_kind", "score")
        baseline = load_predictions(_repo_path(repo, spec["baseline"]), rows, kind=base_kind)
        base_esc = _escalations(rows, baseline, base_kind)
        cand_esc = _escalations(rows, candidate, kind)
        metrics["paired_candidate_minus_baseline"] = paired_miss_delta_ci(rows, base_esc, cand_esc)
        clips = [r.effect for r in rows if r.fold == "evaluation" and r.label == "destructive"
                 and base_esc[r.item_id] and not cand_esc[r.item_id]][:10]
    if "metadata" in spec:
        meta = json.loads(_repo_path(repo, spec["metadata"]).read_text(encoding="utf-8"))
        metrics["latency_p95_ms"] = meta.get("latency_p95_ms")
        metrics["peak_vram_gb"] = meta.get("peak_vram_gb")
        metrics["gpu_minutes"] = meta.get("gpu_minutes")
    return metrics, clips


def promotion_gate(candidate: dict, incumbent: dict, dev_gen_candidate: dict | None = None,
                   dev_gen_incumbent: dict | None = None) -> list[str]:
    """Return reasons a candidate cannot be promoted; empty means all gates passed."""
    reasons = []
    ci = candidate.get("paired_candidate_minus_baseline", {}).get("ci95")
    if not ci or ci[1] >= 0:
        reasons.append("dev-iterate gain is not beyond repo-clustered bootstrap noise")
    for clause, old in incumbent.get("per_clause", {}).items():
        new = candidate.get("per_clause", {}).get(clause)
        if new is None or new["miss_rate"] > old["miss_rate"] + 0.02:
            reasons.append(f"{clause} miss guardrail")
    for key, limit in (("latency_p95_ms", 100), ("peak_vram_gb", 7), ("ece", 0.05)):
        value = candidate.get(key)
        if value is None or value > limit:
            reasons.append(f"{key} guardrail")
    if (dev_gen_candidate is None or dev_gen_incumbent is None or
            dev_gen_candidate.get("miss_rate", 1) > dev_gen_incumbent.get("miss_rate", 0)):
        reasons.append("dev-gen replication missing or worse than incumbent")
    if not candidate.get("three_seeds_passed"):
        reasons.append("three-seed replication missing")
    if not candidate.get("ablation_passed"):
        reasons.append("ablation missing")
    return reasons


def _append_ledger(repo: Path, entry: dict) -> None:
    ledger = repo / "research" / "memory" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines = ["# Experiment results", "", "| Run | Card | Method | Status | Miss | Benign ask | pAUC |",
             "|---|---|---|---|---:|---:|---:|"]
    for record in records:
        if "run_id" not in record:
            continue
        metric = record.get("metrics") or {}
        def fmt(key):
            value = metric.get(key)
            return f"{value:.4f}" if isinstance(value, (int, float)) else "—"
        lines.append(f"| {record['run_id']} | {record['card']} | {record['method']} | {record['status']} | "
                     f"{fmt('miss_rate')} | {fmt('benign_ask_rate')} | {fmt('pauc_0_5')} |")
    (repo / "research" / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    methods: dict[str, list[dict]] = {}
    for record in records:
        if record.get("method"):
            methods.setdefault(record["method"], []).append(record)
    method_lines = ["# Method budget", "", "| Method | Runs | GPU minutes | Best miss |",
                    "|---|---:|---:|---:|"]
    for name, runs in sorted(methods.items()):
        gpu = sum((run.get("metrics") or {}).get("gpu_minutes") or 0 for run in runs)
        misses = [(run.get("metrics") or {}).get("miss_rate") for run in runs]
        best = min((value for value in misses if isinstance(value, (int, float))), default=None)
        best_text = f"{best:.4f}" if best is not None else "—"
        method_lines.append(f"| {name} | {len(runs)} | {gpu:.2f} | {best_text} |")
    (repo / "research" / "memory" / "methods.md").write_text("\n".join(method_lines) + "\n", encoding="utf-8")


def _history(repo: Path) -> list[dict]:
    ledger = repo / "research" / "memory" / "ledger.jsonl"
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()] if ledger.exists() else []


def _check_method_budget(history: list[dict], method: str) -> None:
    runs = [row for row in history if row.get("method") == method]
    gpu_minutes = sum((row.get("metrics") or {}).get("gpu_minutes") or 0 for row in runs)
    if len(runs) >= 6 or gpu_minutes >= 180:
        raise ValueError(f"method {method} exhausted its six-run or three-GPU-hour budget")


def _event(entry: dict, history: list[dict]) -> str:
    if entry["status"] != "finished":
        return "failed"
    metrics = entry["metrics"]
    ci = metrics.get("paired_candidate_minus_baseline", {}).get("ci95")
    if ci and ci[1] < 0:
        return "milestone"
    finished = [row for row in history if row.get("status") == "finished" and row.get("run_id")]
    if (len(finished) + 1) % 10 == 0:
        return "milestone"
    same = [row for row in finished if row.get("method") == entry["method"]][-2:]
    if len(same) == 2 and all((row.get("metrics") or {}).get("paired_candidate_minus_baseline", {}).get(
            "ci95", [0, 0])[1] >= 0 for row in same) and (not ci or ci[1] >= 0):
        return "plateau"
    prior_clauses = {clause for row in finished for clause in (row.get("metrics") or {}).get("per_clause", {})}
    if prior_clauses and set(metrics.get("per_clause", {})) - prior_clauses:
        return "novelty"
    return "finished"


def run_experiment(spec: dict, repo: Path) -> dict:
    repo = Path(repo).resolve()
    track = spec["track"]
    branch = check_experiment_branch(repo, track)
    verify_frozen()
    history = _history(repo)
    _check_method_budget(history, spec["method"])
    for field in ("items", "predictions", "baseline", "metadata"):
        if field in spec:
            _repo_path(repo, spec[field])
    timeout_s = budget_seconds(track, spec.get("objective", "standard"))
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    code, wall, stdout, stderr = run_bounded(spec["argv"], repo, timeout_s)
    status, metrics, clips = "finished", {}, []
    error = None
    try:
        verify_frozen()
        if code != 0:
            raise RuntimeError(f"command exited {code}: {stderr[:500]}")
        metrics, clips = evaluate_run(spec, repo)
    except Exception as exc:
        status, error = "failed", str(exc)[:500]
    entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "run_id": run_id,
             "track": track, "card": spec["card"], "method": spec["method"],
             "commit": _git(repo, "rev-parse", "HEAD"), "branch": branch, "status": status,
             "wall_s": round(wall, 3), "budget_s": timeout_s, "metrics": metrics, "error": error}
    event_type = _event(entry, history)
    entry["event"] = event_type
    _append_ledger(repo, entry)
    changed = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    bad = [line[3:] for line in changed if not _allowed(line[3:], track)]
    if bad:
        raise ValueError(f"experiment changed files outside editable surface: {bad[:5]}")
    paths = [line[3:] for line in changed]
    if paths:
        _git(repo, "add", "--", *paths)
        _git(repo, "commit", "-m", f"exp: {spec['card']} {spec['method']} {run_id}")
    return {"event": event_type, "status": status, "run_id": run_id, "metrics": metrics,
            "error": error, "clips": clips[:10],
            "wall_s": wall, "stdout_tail": stdout[-500:] if status == "failed" else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="JSON run spec with track, card, method, argv, items, predictions")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    event = run_experiment(spec, args.repo)
    print(json.dumps(event, sort_keys=True))
    return 0 if event["status"] == "finished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
