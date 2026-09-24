"""C-0001/C-0002: does overlay overhead scale with file count or with bytes copied up?"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from bench.common import ledger_append, median, timeit
from dryrun.config import load_config, with_shadow
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
    cfg = with_shadow(load_config(use_user_file=False), wall_clock_s=600, disk_budget=4 * 1024**3)
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
                  "append 1 byte to every file; native excludes copy time; shadow includes prepare+run")


if __name__ == "__main__":
    main()
