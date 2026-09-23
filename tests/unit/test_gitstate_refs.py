from __future__ import annotations

import os
from pathlib import Path

from dryrun.effects.gitstate import find_workspace_root, read_refs

A, B, C = "a" * 40, "b" * 40, "c" * 40


def make_git(root: Path) -> Path:
    g = root / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text(A + "\n")
    (g / "packed-refs").write_text(f"# pack-refs with: peeled\n{B} refs/heads/old\n{C} refs/tags/v1\n")
    return g


def test_read_refs_lower_only(tmp_path: Path):
    g = make_git(tmp_path)
    assert read_refs(g) == {"HEAD": A, "refs/heads/main": A, "refs/heads/old": B, "refs/tags/v1": C}


def test_read_refs_merges_upper_with_whiteouts(tmp_path: Path):
    g = make_git(tmp_path / "lower")
    up = tmp_path / "upper" / ".git"
    (up / "refs" / "heads").mkdir(parents=True)
    (up / "refs" / "heads" / "main").write_text(C + "\n")
    (up / "refs" / "heads" / "feature").write_text(B + "\n")
    (up / "packed-refs").write_text(f"{C} refs/tags/v1\n")
    refs = read_refs(g, up)
    assert refs == {"HEAD": C, "refs/heads/main": C, "refs/heads/feature": B, "refs/tags/v1": C}
    wo = up / "refs" / "heads" / "main"
    wo.unlink()
    wo.write_bytes(b"")
    os.setxattr(wo, "user.overlay.whiteout", b"y")
    assert "refs/heads/main" not in read_refs(g, up)


def test_find_workspace_root(tmp_path: Path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "a" / "b").mkdir(parents=True)
    assert find_workspace_root(tmp_path / "repo" / "a" / "b") == tmp_path / "repo"
    (tmp_path / "plain").mkdir()
    assert find_workspace_root(tmp_path / "plain") == tmp_path / "plain"
