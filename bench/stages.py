"""C-0003 (and N2): for short commands, do fixed pipeline stages dominate the added latency?"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from bench.common import ledger_append, median
from dryrun.config import load_config
from dryrun.pipeline import Gate, Pipeline
from dryrun.store import Store

COMMANDS = [
    "python3 -c 'print(1)'", "sed -i 's/a/b/' src/m0/f0.py", "echo x > out.txt", "rm -rf build",
    "mkdir -p build && touch build/x", "cat src/m0/f0.py | wc -l > count.txt", "python3 -m compileall -q src/m0",
    "cp src/m0/f0.py src/m0/f0_copy.py", "mv src/m0/f1.py src/m0/f1_renamed.py", "chmod +x run.sh",
] * 3
GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_AUTHOR_NAME": "b", "GIT_AUTHOR_EMAIL": "b@b",
           "GIT_COMMITTER_NAME": "b", "GIT_COMMITTER_EMAIL": "b@b"}


def make_repo(root: Path, files: int = 20_000) -> None:
    for i in range(files):
        d = root / "src" / f"m{i // 200}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i % 200}.py").write_text(f"a = {i}\n")
    (root / "run.sh").write_text("#!/bin/sh\n")
    # gc.auto=0: otherwise `git commit` of 20k files starts a background auto-gc that repacks objects
    # while the benchmark copies the repo (observed on the first run; it also makes shadows see
    # lower_changed, correctly).
    for args in (["init", "-q"], ["config", "gc.auto", "0"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=root, env=GIT_ENV, check=True)


def main() -> None:
    base = Path.home() / "dryrun-bench" / uuid.uuid4().hex[:8]
    ws = base / "repo"
    make_repo(ws)
    p = Pipeline(load_config(use_user_file=False), Store(base / "state"), gate=Gate(True, "bench"))
    rows = []
    for cmd in COMMANDS:
        t = time.perf_counter()
        d = p.handle_pretool({"op": "pretool", "session_id": "b", "cwd": str(ws), "command": cmd, "description": "",
                              "transcript_path": "", "env": {"PATH": "/usr/bin:/bin"}, "deadline_ms": 60000})
        total = time.perf_counter() - t
        entry = json.loads(p.store.log_path.read_text().splitlines()[-1])
        native_dir = base / "native"
        shutil.copytree(ws, native_dir, symlinks=True)
        t = time.perf_counter()
        subprocess.run(["bash", "-c", cmd], cwd=native_dir, capture_output=True)
        native = time.perf_counter() - t
        shutil.rmtree(native_dir)
        rows.append({"cmd": cmd, "decision": d.decision, "total_ms": round(total * 1000),
                     "native_ms": round(native * 1000), **{f"{k}_ms": v for k, v in entry["timings_ms"].items()}})
        if d.token:
            try:
                p.store.remove_run(p.store.run(d.run_id))
            except Exception:
                pass
    shadowed = [r for r in rows if "run_ms" in r]
    fixed = [r["prepare_ms"] + r.get("judge_ms", 0) + r.get("triage_ms", 0) for r in shadowed]
    added = [r["total_ms"] - r["native_ms"] for r in shadowed]
    summary = {
        "n_shadowed": len(shadowed),
        "n2_median_ratio_shadow_vs_native": round(median([r["total_ms"] / max(r["native_ms"], 1) for r in shadowed]), 2),
        "median_added_ms": median(added),
        "median_fixed_share_of_added": round(median([f / a for f, a in zip(fixed, added) if a > 0]), 3),
        "median_prepare_ms": median([r["prepare_ms"] for r in shadowed]),
        "median_run_ms": median([r["run_ms"] for r in shadowed]),
        "median_judge_ms": median([r.get("judge_ms", 0) for r in shadowed]),
        "median_native_ms": median([r["native_ms"] for r in shadowed]),
    }
    print(json.dumps(summary, indent=2))
    ledger_append("C-0003", "stages", {"summary": summary, "rows": rows}, "finished", "20k-file git repo")
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
