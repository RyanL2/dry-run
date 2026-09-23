from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from dryrun.commit import apply_changeset
from dryrun.config import load_config
from dryrun.effects.upper import extract
from dryrun.paths import bwrap_path
from dryrun.sandbox.assemble import prepare
from dryrun.sandbox.spawn import run_shadow
from dryrun.store import Store
from dryrun.types import ChangeSet

ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def standard_tree(ws: Path) -> None:
    (ws / "dir" / "sub").mkdir(parents=True)
    (ws / "keep.txt").write_text("keep\n")
    (ws / "other.txt").write_text("other\n")
    (ws / "script.sh").write_text("#!/bin/sh\necho hi\n")
    (ws / "dir" / "f1").write_text("one\n")
    (ws / "dir" / "f2").write_text("two\n")
    (ws / "dir" / "sub" / "f3").write_text("three\n")
    (ws / "a.log").write_text("log\n")
    (ws / "dir" / "b.log").write_text("log\n")
    (ws / "existing_link").symlink_to("keep.txt")


def twin(scratch: Path, setup=standard_tree) -> tuple[Path, Path]:
    a = scratch / "A" / "ws"
    a.mkdir(parents=True)
    setup(a)
    b = scratch / "B" / "ws"
    shutil.copytree(a, b, symlinks=True, copy_function=shutil.copy2)
    for src in sorted([a, *a.rglob("*")], key=lambda p: len(p.parts), reverse=True):
        dst = b / src.relative_to(a)
        if src.is_dir() and not src.is_symlink():
            shutil.copystat(src, dst)
    return a, b


def real(ws: Path, cmd: str) -> None:
    subprocess.run(["bash", "-c", cmd], cwd=ws, env=ENV, capture_output=True, timeout=60)


def shadow_commit(ws: Path, cmd: str, state: Path) -> tuple[ChangeSet, dict[str, int]]:
    cfg = load_config(use_user_file=False)
    store = Store(state)
    run = store.new_run()
    prep = prepare(run, ws_root=ws, cwd=ws, command=cmd, env=dict(ENV), cfg=cfg, home=Path.home(),
                   state=store.root, bwrap=str(bwrap_path()))
    run_shadow(prep.spec, run_id=run.run_id, out_dir=run.root, cfg=cfg.shadow, watch_fs=run.root)
    eff = extract("workspace", ws, run.ws_up, prep.ws_base)
    cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws)}, base_digest="-", ops=eff.ops, refused=eff.refused)
    mtimes = {op.target: op.mtime_ns for op in eff.ops if op.op == "rename_in"}
    try:
        if cs.committable:
            apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    finally:
        store.remove_run(run)
    return cs, mtimes


def tree(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p), None)
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode), None)
            else:
                with open(p, "rb") as f:
                    out[rel] = ("file", stat.S_IMODE(st.st_mode), f.read(), st.st_mtime_ns)
    return out


def _index_entries(ws: Path) -> str:
    return subprocess.run(["git", "-C", str(ws), "ls-files", "-s"], capture_output=True, text=True, env=ENV).stdout


def assert_same(a: Path, b: Path, touched: dict[str, int]) -> None:
    ta, tb = tree(a), tree(b)
    assert set(ta) == set(tb), f"paths differ: only real={set(ta) - set(tb)} only shadow={set(tb) - set(ta)}"
    for rel in ta:
        ea, eb = ta[rel], tb[rel]
        if rel == ".git/index":
            # The index caches each file's device/inode; two real runs in two directories differ too.
            # Compare what it means: the staged entries and their blob ids.
            assert _index_entries(a) == _index_entries(b), ".git/index entries differ"
            continue
        assert ea[:3] == eb[:3], f"{rel}: real={ea[:3]!r:.120} shadow={eb[:3]!r:.120}"
        if ea[0] == "file":
            if rel in touched:
                assert eb[3] == touched[rel], f"{rel}: commit did not preserve the reviewed mtime"
            else:
                assert ea[3] == eb[3], f"{rel}: untouched file mtime changed"
