from __future__ import annotations

import hashlib
from pathlib import Path

from dryrun.config import Policy
from dryrun.effects.gitstate import GitSnapshot
from dryrun.effects.record import annotate, compute_flags, git_effects
from dryrun.effects.upper import AreaEffect
from dryrun.runpaths import RunPaths
from dryrun.sandbox.spawn import SpawnResult
from dryrun.sandbox.trace import TraceSummary
from dryrun.types import FsEntry


def sha1_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_annotate_recoverability_build_output_ledger_and_discard(tmp_path: Path):
    restored = tmp_path / "restored.py"
    restored.write_bytes(b"v1\n")
    snap = GitSnapshot(is_repo=True, status={"dirty.py": "tracked_dirty", "new.py": "untracked"},
                       ignored_dirs=("dist",), tracked=frozenset({"clean.py", "dirty.py"}),
                       head_blobs={"dirty.py": sha1_blob(b"v1\n")}, index_blobs={})
    entries = [
        FsEntry(op="delete", path="clean.py", kind="file", preexisting=True),
        FsEntry(op="modify", path="dirty.py", kind="file", preexisting=True),
        FsEntry(op="delete", path="new.py", kind="file", preexisting=True),
        FsEntry(op="delete", path="build/x.o", kind="file", preexisting=True),
        FsEntry(op="delete", path="dist/app.js", kind="file", preexisting=True),
        FsEntry(op="delete", path="gen/out.txt", kind="file", preexisting=True),
    ]
    annotate(entries, snap, Policy(), ledger={"gen"}, contents={"dirty.py": restored})
    e = {x.path: x for x in entries}
    assert e["clean.py"].git == "tracked_clean" and not e["clean.py"].build_output
    assert e["dirty.py"].discards_work
    assert e["new.py"].git == "untracked"
    assert e["build/x.o"].build_output
    assert e["dist/app.js"].git == "ignored" and e["dist/app.js"].build_output
    assert e["gen/out.txt"].in_ledger


def test_annotate_detects_restore_to_older_commit_via_blob_exists(tmp_path: Path):
    older = tmp_path / "older.py"
    older.write_bytes(b"v0\n")
    snap = GitSnapshot(is_repo=True, status={"dirty.py": "tracked_dirty"}, tracked=frozenset({"dirty.py"}),
                       head_blobs={"dirty.py": sha1_blob(b"v1\n")}, index_blobs={"dirty.py": sha1_blob(b"v1\n")})
    entries = [FsEntry(op="modify", path="dirty.py", kind="file", preexisting=True)]
    annotate(entries, snap, Policy(), ledger=set(), contents={"dirty.py": older},
             blob_exists=lambda shas: {sha1_blob(b"v0\n")} & shas)
    assert entries[0].discards_work
    fresh = [FsEntry(op="modify", path="dirty.py", kind="file", preexisting=True)]
    annotate(fresh, snap, Policy(), ledger=set(), contents={"dirty.py": older}, blob_exists=lambda shas: set())
    assert not fresh[0].discards_work


def test_compute_flags(tmp_path: Path):
    res = SpawnResult(exit_code=None, wall_ms=10, timed_out=True, killed_reason="timeout",
                      stdout_path=tmp_path / "o", stderr_path=tmp_path / "e", trace_path=tmp_path / "t")
    flags = compute_flags(res=res, trace=TraceSummary(net=[{"kind": "dns", "target": "x:53"}]),
                          ws_eff=AreaEffect(refused=[{"path": "h", "reason": "hardlink_in_shadow"}]),
                          tmp_eff=AreaEffect(), lower_changed=True, tmp_partial=True,
                          output=b"touch: cannot touch '/home/u/x': Read-only file system\n")
    assert set(flags) == {"timeout", "incomplete_network", "unsupported_entry", "lower_changed",
                          "tmp_partial", "ro_write_blocked"}


def test_truncated_trace_flags_resource_limit():
    """A cut-short trace may hide execs and network attempts, so the record cannot be trusted as complete."""
    flags = compute_flags(res=None, trace=TraceSummary(truncated=True), ws_eff=AreaEffect(), tmp_eff=AreaEffect(),
                          lower_changed=False, tmp_partial=False, output=b"")
    assert flags == ["resource_limit"]


def test_git_effects_classifies_internals_and_refs(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".git" / "refs" / "heads").mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (ws / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    run = RunPaths(tmp_path / "run")
    run.create()
    (run.ws_up / ".git" / "refs" / "heads").mkdir(parents=True)
    (run.ws_up / ".git" / "refs" / "heads" / "topic").write_text("b" * 40 + "\n")
    entries = [
        FsEntry(op="modify", path=".git/index", kind="file", preexisting=True),
        FsEntry(op="create", path=".git/objects/ab/cdef", kind="file", preexisting=False),
        FsEntry(op="delete", path=".git/objects/pack/p.pack", kind="file", preexisting=True),
        FsEntry(op="create", path=".git/refs/heads/topic", kind="file", preexisting=False),
        FsEntry(op="modify", path=".git/config", kind="file", preexisting=True),
        FsEntry(op="create", path=".git/modules/x", kind="file", preexisting=False),
    ]
    g = git_effects(ws, run, entries)
    assert g["index_changed"]
    assert g["objects_deleted"] == 1
    assert g["internals_touched"] == [".git/modules/x"]
    assert [(r.ref, r.change) for r in g["refs_changed"]] == [("refs/heads/topic", "created")]


def test_git_effects_never_hands_a_symlinked_shadow_objects_dir_to_git(tmp_path: Path, monkeypatch):
    import dryrun.effects.record as record
    ws = tmp_path / "ws"
    (ws / ".git" / "refs" / "heads").mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (ws / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    run = RunPaths(tmp_path / "run")
    run.create()
    (run.ws_up / ".git" / "refs" / "heads").mkdir(parents=True)
    (run.ws_up / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    (tmp_path / "host").mkdir()
    (run.ws_up / ".git" / "objects").symlink_to(tmp_path / "host")              # ln -s / .git/objects
    seen = []
    monkeypatch.setattr(record, "is_ancestor", lambda ws_root, old, new, extra: seen.append(extra) or None)
    record.git_effects(ws, run, [FsEntry(op="modify", path=".git/refs/heads/main", kind="file", preexisting=True)])
    assert seen and all(extra is None for extra in seen)          # main and HEAD both moved
