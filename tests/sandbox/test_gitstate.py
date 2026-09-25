from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from dryrun.effects.gitstate import blob_sha1, is_ancestor, objects_exist, recoverability, snapshot

pytestmark = pytest.mark.sandbox


def git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(ws), "GIT_AUTHOR_NAME": "t",
                               "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def blob_sha1_of_text(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture
def repo(scratch: Path) -> Path:
    ws = scratch / "repo"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    (ws / ".gitignore").write_text("build/\n")
    (ws / "clean.py").write_text("clean\n")
    (ws / "dirty.py").write_text("v1\n")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "init")
    (ws / "dirty.py").write_text("v2\n")
    (ws / "new.py").write_text("new\n")
    (ws / "build").mkdir()
    (ws / "build" / "out.o").write_text("o")
    return ws


def test_recoverability_classes(repo: Path):
    snap = snapshot(repo)
    assert snap.is_repo
    assert recoverability(snap, "clean.py") == "tracked_clean"
    assert recoverability(snap, "dirty.py") == "tracked_dirty"
    assert recoverability(snap, "new.py") == "untracked"
    assert recoverability(snap, "build/out.o") == "ignored"


def test_blob_shas_match_git(repo: Path):
    snap = snapshot(repo)
    assert snap.head_blobs["dirty.py"] == blob_sha1_of_text("v1\n")
    assert blob_sha1(repo / "clean.py") == git(repo, "hash-object", "clean.py")


def test_snapshot_does_not_run_fsmonitor_outside_sandbox(repo: Path, scratch: Path):
    marker = scratch / "fsmonitor-ran"
    hook = scratch / "fsmon.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))
    snapshot(repo)
    assert not marker.exists()


def test_is_ancestor(repo: Path):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "-am", "second")
    second = git(repo, "rev-parse", "HEAD")
    assert is_ancestor(repo, first, second, None) is True
    assert is_ancestor(repo, second, first, None) is False


def test_objects_exist(repo: Path):
    old = blob_sha1_of_text("v1\n")
    missing = blob_sha1_of_text("never committed\n")
    assert objects_exist(repo, {old, missing}) == {old}
    assert objects_exist(repo, set()) == set()


def test_non_repo(scratch: Path):
    d = scratch / "plain"
    d.mkdir()
    snap = snapshot(d)
    assert not snap.is_repo and recoverability(snap, "x") is None
