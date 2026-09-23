### Task 21: Benchmarks and cards C-0001..C-0003 (N1, N2; research loop used by hand)

**Files:**
- Create: `bench/__init__.py` (empty), `bench/common.py`, `bench/overlay_factorial.py`, `bench/stages.py`, `bench/latency.py`, `research/memory/ledger.jsonl`
- Modify: `research/cards/C-0001.md`, `research/cards/C-0002.md` and `research/cards/C-0003.md` (Verdict lines)

**Interfaces:**
- Consumes: `prepare`, `run_shadow`, `Pipeline`, `Gate`, `Store`, `Daemon`, `load_config`
- Produces:
  - `bench.common.ledger_append(card: str, experiment: str, metrics: dict, status: str, note: str) -> None`: appends to `research/memory/ledger.jsonl`, including git HEAD, host and kernel
  - `bench.common.timeit(fn, reps) -> list[float]`

**Experiments** (the cards' Change lines):

| Card(s) | Benchmark | What it measures |
|---|---|---|
| C-0001 / C-0002 | `overlay_factorial.py` | 2×2 over {200, 20000} files × {10 MB, 512 MB} total. Workload: append 1 byte to every file (forces copy-up). Native time on a copy vs `run_shadow` time, 3 reps, median ratio |
| C-0003 | `stages.py` | a 20k-file synthetic repo and 30 realistic short commands. Pipeline stage timings (triage/prepare/run/judge from the decision log) and native runtime |
| N1 | `latency.py` | an in-process daemon (gate open) and the real hook subprocess, 200 read-only and 50 non-shadowable commands. p50/p95 of hook wall time |

- [ ] **Step 1: Implement `bench/common.py`**

```python
from __future__ import annotations

import json
import os
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
                              text=True).stdout.strip()
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
```

- [ ] **Step 2: Implement `bench/overlay_factorial.py` (C-0001/C-0002)**

```python
"""C-0001/C-0002: does overlay overhead scale with file count or with bytes copied up?"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from bench.common import ledger_append, median, timeit
from dryrun.config import load_config
from dryrun.paths import bwrap_path
from dryrun.sandbox.assemble import prepare
from dryrun.sandbox.spawn import run_shadow
from dryrun.store import Store

WORKLOAD = "find . -type f -print0 | xargs -0 -n 500 sh -c 'for f; do printf x >> \"$f\"; done' sh"


def make_tree(root: Path, files: int, total: int) -> None:
    size = max(1, total // files)
    blob = b"a" * size
    for i in range(files):
        d = root / f"d{i // 500}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i}").write_bytes(blob)


def one(files: int, total: int, reps: int = 3) -> dict:
    base = Path.home() / "dryrun-bench" / uuid.uuid4().hex[:8]
    src = base / "src"
    make_tree(src, files, total)
    cfg = load_config(use_user_file=False)
    store = Store(base / "state")

    def native():
        work = base / "native"
        shutil.copytree(src, work)
        subprocess.run(["bash", "-c", WORKLOAD], cwd=work, check=True)
        shutil.rmtree(work)

    def copy_only():
        work = base / "native"
        shutil.copytree(src, work)
        shutil.rmtree(work)

    def shadow():
        run = store.new_run()
        prep = prepare(run, ws_root=src, cwd=src, command=WORKLOAD, env={"PATH": "/usr/bin:/bin"}, cfg=cfg,
                       home=Path.home(), state=store.root, bwrap=str(bwrap_path()))
        res = run_shadow(prep.spec, run_id=run.run_id, out_dir=run.root, cfg=cfg.shadow, watch_fs=run.root)
        assert res.exit_code == 0, res.stderr_path.read_text()
        store.remove_run(run)

    n = median(timeit(native, reps)) - median(timeit(copy_only, reps))
    s = median(timeit(shadow, reps))
    shutil.rmtree(base)
    return {"files": files, "total_bytes": total, "native_s": round(n, 3), "shadow_s": round(s, 3),
            "ratio": round(s / n, 3) if n > 0 else None}


def main() -> None:
    rows = [one(f, t) for f in (200, 20_000) for t in (10 * 1024**2, 512 * 1024**2)]
    for r in rows:
        print(r)
    ledger_append("C-0001,C-0002", "overlay_factorial", {"rows": rows}, "finished",
                  "append 1 byte to every file; native excludes copy time")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Implement `bench/stages.py` (C-0003) and `bench/latency.py` (N1)**

`bench/stages.py`:
```python
"""C-0003: for short commands, do fixed pipeline stages dominate the added latency?"""
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


def make_repo(root: Path, files: int = 20_000) -> None:
    for i in range(files):
        d = root / "src" / f"m{i // 200}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i % 200}.py").write_text(f"a = {i}\n")
    (root / "run.sh").write_text("#!/bin/sh\n")
    subprocess.run(["git", "init", "-q"], cwd=root)
    subprocess.run(["git", "add", "-A"], cwd=root)
    subprocess.run(["git", "-c", "user.name=b", "-c", "user.email=b@b", "commit", "-qm", "init"], cwd=root)


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
        rows.append({"cmd": cmd, "decision": d.decision, "total_ms": round(total * 1000), "native_ms": round(native * 1000),
                     **{f"{k}_ms": v for k, v in entry["timings_ms"].items()}})
        if d.run_id:
            try:
                p.store.remove_run(p.store.run(d.run_id))
            except Exception:
                pass
    fixed = [r["prepare_ms"] + r.get("judge_ms", 0) + r.get("triage_ms", 0) for r in rows if "prepare_ms" in r]
    added = [r["total_ms"] - r["native_ms"] for r in rows if "prepare_ms" in r]
    share = median([f / a for f, a in zip(fixed, added) if a > 0])
    shadowed = [r for r in rows if "run_ms" in r]
    summary = {"n2_median_ratio_shadow_vs_native": round(median([r["total_ms"] / max(r["native_ms"], 1)
                                                                 for r in shadowed]), 2),
               "median_total_ms": median([r["total_ms"] for r in rows]),
               "median_native_ms": median([r["native_ms"] for r in rows]),
               "median_fixed_share_of_added": round(share, 3),
               "median_prepare_ms": median([r["prepare_ms"] for r in rows if "prepare_ms" in r]),
               "median_run_ms": median([r["run_ms"] for r in rows if "run_ms" in r])}
    print(json.dumps(summary, indent=2))
    ledger_append("C-0003", "stages", {"summary": summary, "rows": rows}, "finished", "20k-file git repo")
    shutil.rmtree(base)


if __name__ == "__main__":
    main()
```

`bench/latency.py`:
```python
"""N1: added latency of the hook on non-shadowed commands (p50 < 300 ms, p95 < 500 ms)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from bench.common import ledger_append, pct
from dryrun.config import load_config
from dryrun.daemon import Daemon
from dryrun.pipeline import Gate, Pipeline
from dryrun.store import Store

SRC = Path(__file__).resolve().parents[1] / "src"


def main() -> None:
    base = Path.home() / "dryrun-bench" / uuid.uuid4().hex[:8]
    sock = base / "d.sock"
    cfg = load_config(use_user_file=False)
    store = Store(base / "state")
    d = Daemon(cfg, store, Pipeline(cfg, store, gate=Gate(True, "bench")), sock, canary_fn=lambda: (True, "bench"))
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(d.serve()), daemon=True).start()
    while not sock.exists():
        time.sleep(0.02)
    env = {**os.environ, "PYTHONPATH": str(SRC), "DRYRUN_SOCKET": str(sock)}
    cmds = ["ls -la"] * 100 + ["git status"] * 100 + ["git push origin main"] * 50
    times = []
    for c in cmds:
        payload = json.dumps({"session_id": "b", "cwd": str(base), "tool_name": "Bash",
                              "tool_input": {"command": c}}).encode()
        t = time.perf_counter()
        subprocess.run([sys.executable, "-m", "dryrun.hook", "pretool"], input=payload, env=env, capture_output=True)
        times.append((time.perf_counter() - t) * 1000)
    summary = {"n": len(times), "p50_ms": round(pct(times, 0.5)), "p95_ms": round(pct(times, 0.95))}
    print(summary)
    ledger_append("N1", "latency", summary, "pass" if summary["p50_ms"] < 300 and summary["p95_ms"] < 500 else "fail",
                  "hook subprocess incl. python startup")
    loop.call_soon_threadsafe(d.stop)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the experiments (as dryrundev in WSL) and record verdicts**

```bash
cd /mnt/c/Users/rylei/github/dry-run/.claude/worktrees/core-spec
PYTHONPATH=src:. ~/.venvs/dryrun/bin/python -m bench.latency
PYTHONPATH=src:. ~/.venvs/dryrun/bin/python -m bench.overlay_factorial
PYTHONPATH=src:. ~/.venvs/dryrun/bin/python -m bench.stages
```
For each of C-0001..C-0003, fill in the `Verdict:` line using the card's own Prediction and Disproof. Say which hypotheses the factorial supports or refutes, with ledger timestamps. If a Disproof outcome occurred, write `refuted` and do not re-tune. Do the same for N1 (spec §3.3). Report the numbers honestly even when they miss the targets.

- [ ] **Step 5: Commit**

```bash
git add bench research
git commit -m "bench: latency, overlay factorial and stage timing; record C-0001..C-0003 verdicts"
```
