"""N1: added latency of the hook on non-shadowed commands (p50 < 300 ms, p95 < 500 ms)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
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
    base.mkdir(parents=True)
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
    ok = summary["p50_ms"] < 300 and summary["p95_ms"] < 500
    ledger_append("N1", "latency", summary, "pass" if ok else "fail", "hook subprocess incl. python startup")
    loop.call_soon_threadsafe(d.stop)
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
