"""Assemble the EffectRecord from the raw run artifacts."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from dryrun.config import Policy
from dryrun.effects.gitstate import GitSnapshot, blob_sha1, is_ancestor, read_refs, recoverability
from dryrun.effects.upper import AreaEffect
from dryrun.runpaths import RunPaths
from dryrun.sandbox.spawn import SpawnResult
from dryrun.sandbox.trace import TraceSummary
from dryrun.types import EffectRecord, FsEntry, RefChange

EROFS_MARKER = b"Read-only file system"
GIT_ALLOWED = ("index", "ORIG_HEAD", "FETCH_HEAD", "HEAD", "COMMIT_EDITMSG", "MERGE_HEAD", "MERGE_MSG",
               "MERGE_MODE", "AUTO_MERGE", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_*", "packed-refs",
               "logs", "logs/*", "objects", "objects/*", "refs", "refs/*", "rebase-merge", "rebase-merge/*",
               "rebase-apply", "rebase-apply/*", "sequencer", "sequencer/*", "info", "info/exclude",
               "description", "gc.log", "worktrees", "worktrees/*", "index.lock", "*.lock")
GIT_H9 = ("hooks", "hooks/*", "config", "info/attributes")  # judged by harm-policy H9, not H3
MAX_ANCESTRY_CHECKS = 20


def annotate(entries: list[FsEntry], snap: GitSnapshot, policy: Policy, ledger: set[str],
             contents: dict[str, Path], blob_exists=lambda shas: set()) -> None:
    """blob_exists(shas) -> subset already in the repo's object store (gitstate.objects_exist).
    A tracked_dirty file rewritten to ANY known blob (HEAD, index or an older commit, as with
    `git reset --hard HEAD~1` or `git checkout -- f`) means its uncommitted work was discarded."""
    build = set(policy.build_output_dirs)
    restored: dict[str, str] = {}
    for e in entries:
        e.git = recoverability(snap, e.path)
        parts = e.path.split("/")
        dirs = parts if e.kind == "dir" else parts[:-1]
        e.build_output = e.git == "ignored" or any(p in build for p in dirs)
        e.in_ledger = any(e.path == p or e.path.startswith(p + "/") for p in ledger)
        if e.op == "modify" and e.git == "tracked_dirty" and e.path in contents:
            restored[e.path] = blob_sha1(contents[e.path])
    if not restored:
        return
    known = {v for p in restored for v in (snap.head_blobs.get(p), snap.index_blobs.get(p)) if v}
    unknown = set(restored.values()) - known
    known |= blob_exists(unknown) if unknown else set()
    for e in entries:
        if e.path in restored:
            e.discards_work = restored[e.path] in known


def git_effects(ws_root: Path, run: RunPaths, ws_entries: list[FsEntry]) -> dict:
    internals, objects_deleted, index_changed = [], 0, False
    for e in ws_entries:
        if not e.path.startswith(".git/"):
            continue
        inner = e.path[len(".git/"):]
        if inner == "index":
            index_changed = True
        if inner.startswith("objects/") and e.op == "delete" and e.kind == "file":
            objects_deleted += 1
        if any(fnmatch.fnmatchcase(inner, p) for p in GIT_ALLOWED + GIT_H9):
            continue
        internals.append(e.path)
    git_dir = Path(ws_root) / ".git"
    before = read_refs(git_dir)
    up_git = run.ws_up / ".git"
    after = read_refs(git_dir, up_git if up_git.is_dir() else None)
    objects = up_git / "objects" if (up_git / "objects").is_dir() else None
    changes: list[RefChange] = []
    checks = 0
    for ref in sorted(set(before) | set(after)):
        b, a = before.get(ref), after.get(ref)
        if b == a:
            continue
        if b is None:
            kind = "created"
        elif a is None:
            kind = "deleted"
        elif checks < MAX_ANCESTRY_CHECKS:
            checks += 1
            anc = is_ancestor(Path(ws_root), b, a, objects)
            kind = "fast_forward" if anc else ("non_fast_forward" if anc is False else "unknown")
        else:
            kind = "unknown"
        changes.append(RefChange(ref=ref, before=b, after=a, change=kind))
    return {"refs_changed": changes, "index_changed": index_changed,
            "internals_touched": internals, "objects_deleted": objects_deleted}


def compute_flags(*, res: SpawnResult | None, trace: TraceSummary, ws_eff: AreaEffect, tmp_eff: AreaEffect,
                  lower_changed: bool, tmp_partial: bool, output: bytes) -> list[str]:
    flags = []
    if res is not None and res.timed_out:
        flags.append("timeout")
    if res is not None and res.killed_reason in ("disk", "killed"):
        flags.append("resource_limit")
    if trace.net:
        flags.append("incomplete_network")
    if ws_eff.refused or tmp_eff.refused:
        flags.append("unsupported_entry")
    if lower_changed:
        flags.append("lower_changed")
    if EROFS_MARKER in output:
        flags.append("ro_write_blocked")
    if tmp_partial:
        flags.append("tmp_partial")
    return flags


def tail(path: Path, n: int = 4096) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def cache_counts(caches: list[tuple[Path, Path]]) -> tuple[int, int]:
    files = size = 0
    for _, up in caches:
        for dirpath, _, names in os.walk(up):
            for name in names:
                try:
                    size += os.lstat(os.path.join(dirpath, name)).st_size
                    files += 1
                except OSError:
                    pass
    return files, size


def build_record(*, run_id: str, session_id: str, command: str, cwd: str, ws_root: str,
                 request_text: str | None, request_source: str | None, triage_class: str, triage_reason: str,
                 res: SpawnResult | None, ws_eff: AreaEffect, tmp_eff: AreaEffect, cache_files: int,
                 cache_bytes: int, git_is_repo: bool, git: dict, trace: TraceSummary, decoy_hits: list[dict],
                 flags: list[str]) -> EffectRecord:
    return EffectRecord(
        run_id=run_id, session_id=session_id, command=command, cwd=cwd, workspace_root=ws_root,
        request_text=request_text, request_source=request_source,
        triage_class=triage_class, triage_reason=triage_reason,
        exit_code=res.exit_code if res else None, wall_ms=res.wall_ms if res else 0,
        timed_out=res.timed_out if res else False,
        stdout_tail=tail(res.stdout_path) if res else "", stderr_tail=tail(res.stderr_path) if res else "",
        workspace=ws_eff.entries, tmp=tmp_eff.entries, home_cache_files=cache_files,
        home_cache_bytes=cache_bytes, git_is_repo=git_is_repo,
        refs_changed=git.get("refs_changed", []), index_changed=git.get("index_changed", False),
        internals_touched=git.get("internals_touched", []), objects_deleted=git.get("objects_deleted", 0),
        net=list(trace.net), proc_count=trace.pids, execs=list(trace.execs),
        decoy_hits=decoy_hits, flags=flags,
    )
