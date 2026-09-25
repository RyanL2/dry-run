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


def test_read_refs_ignores_fifos_symlinks_and_huge_files_from_the_shadow(tmp_path: Path):
    import threading
    g = make_git(tmp_path / "lower")
    up = tmp_path / "upper" / ".git"
    (up / "refs" / "heads").mkdir(parents=True)
    os.mkfifo(up / "refs" / "heads" / "fifo")                         # would block a naive read forever
    (up / "refs" / "heads" / "zero").symlink_to("/dev/zero")          # would read forever
    (up / "packed-refs").symlink_to("/dev/zero")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("refs", read_refs(g, up)), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "read_refs blocked on shadow-controlled content"
    assert "refs/heads/fifo" not in out["refs"] and "refs/heads/zero" not in out["refs"]


def test_read_refs_never_follows_symlinked_directories_from_the_shadow(tmp_path: Path):
    g = make_git(tmp_path / "lower")
    host = tmp_path / "host"                                           # stands in for / or /usr
    (host / "heads").mkdir(parents=True)
    (host / "heads" / "planted").write_text(C + "\n")
    up = tmp_path / "upper" / ".git"
    up.mkdir(parents=True)
    (up / "refs").symlink_to(host)                                    # rm -rf .git/refs; ln -s / .git/refs
    assert "refs/heads/planted" not in read_refs(g, up)
    up2 = tmp_path / "upper2"
    up2.mkdir()
    (up2 / ".git").symlink_to(host.parent)
    assert "refs/heads/planted" not in read_refs(g, up2 / ".git")
    up3 = tmp_path / "upper3" / ".git"
    (up3 / "refs" / "heads").mkdir(parents=True)
    with open(up3 / "refs" / "heads" / "huge", "wb") as f:
        f.truncate(1024**3)                                           # sparse 1 GiB regular file
    assert "refs/heads/huge" not in read_refs(g, up3)
