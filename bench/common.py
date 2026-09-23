from __future__ import annotations

import json
import platform
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "research" / "memory" / "ledger.jsonl"


def _head() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True).stdout.strip() or "?"
    except OSError:
        return "?"


def ledger_append(card: str, experiment: str, metrics: dict, status: str, note: str) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "card": card, "experiment": experiment, "commit": _head(),
             "host": platform.node(), "kernel": platform.release(), "metrics": metrics, "status": status,
             "note": note}
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")


def timeit(fn, reps: int) -> list[float]:
    out = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return out


def median(xs: list[float]) -> float:
    return statistics.median(xs)


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]
