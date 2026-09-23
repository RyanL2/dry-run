from __future__ import annotations

import os
from pathlib import Path

from dryrun.effects.upper import extract
from dryrun.fingerprint import fingerprint_tree


def whiteout(path: Path) -> None:
    path.write_bytes(b"")
    os.setxattr(path, "user.overlay.whiteout", b"y")


def setup(tmp_path: Path) -> tuple[Path, Path]:
    lower, upper = tmp_path / "lower", tmp_path / "upper"
    (lower / "src").mkdir(parents=True)
    (lower / "src" / "a.py").write_text("print(1)\n")
    (lower / "src" / "b.py").write_text("print(2)\n")
    (lower / "old").mkdir()
    (lower / "old" / "x.txt").write_text("x")
    (lower / "old" / "y.txt").write_text("y")
    (lower / "same.txt").write_text("same")
    (lower / "mode.sh").write_text("#!/bin/sh\n")
    upper.mkdir()
    return lower, upper


def by_path(entries):
    return {e.path: e for e in entries}


def test_create_modify_delete_and_noise(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    (upper / "src").mkdir()
    (upper / "src" / "a.py").write_text("print(10)\n")
    whiteout(upper / "src" / "b.py")
    (upper / "new.txt").write_text("n")
    same = upper / "same.txt"
    same.write_text("same")
    st = os.stat(lower / "same.txt")
    os.utime(same, ns=(st.st_atime_ns, st.st_mtime_ns))
    eff = extract("workspace", lower, upper, base)
    e = by_path(eff.entries)
    assert e["src/a.py"].op == "modify" and e["src/a.py"].preexisting
    assert e["src/a.py"].bytes_before == 9 and e["src/a.py"].bytes_after == 10
    assert e["src/b.py"].op == "delete" and e["src/b.py"].preexisting
    assert e["new.txt"].op == "create" and not e["new.txt"].preexisting
    assert "same.txt" not in e
    ops = {(o.op, o.target) for o in eff.ops}
    assert ops == {("rename_in", "src/a.py"), ("unlink", "src/b.py"), ("rename_in", "new.txt")}
    a = next(o for o in eff.ops if o.target == "src/a.py")
    assert a.base_fp == list(base["src/a.py"]) and a.sha256 and a.size == 10
    n = next(o for o in eff.ops if o.target == "new.txt")
    assert n.base_fp is None
    assert set(eff.contents) == {"src/a.py", "new.txt"}


def test_deleted_dir_is_expanded_and_uses_rmtree(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    whiteout(upper / "old")
    eff = extract("workspace", lower, upper, base)
    assert {e.path for e in eff.entries if e.op == "delete"} == {"old", "old/x.txt", "old/y.txt"}
    (op,) = eff.ops
    assert op.op == "rmtree" and op.target == "old" and op.subtree_digest


def test_opaque_dir_replaces_lower_contents(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    (upper / "old").mkdir()
    os.setxattr(upper / "old", "user.overlay.opaque", b"y")
    (upper / "old" / "z.txt").write_text("z")
    eff = extract("workspace", lower, upper, base)
    kinds = [(o.op, o.target) for o in eff.ops]
    assert kinds == [("rmtree", "old"), ("mkdir", "old"), ("rename_in", "old/z.txt")]
    assert by_path(eff.entries)["old/z.txt"].op == "create"


def test_chmod_only_becomes_chmod(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    up = upper / "mode.sh"
    up.write_text("#!/bin/sh\n")
    st = os.stat(lower / "mode.sh")
    os.chmod(up, 0o755)
    os.utime(up, ns=(st.st_atime_ns, st.st_mtime_ns))
    eff = extract("workspace", lower, upper, base)
    (op,) = eff.ops
    assert (op.op, op.mode) == ("chmod", 0o755)
    assert by_path(eff.entries)["mode.sh"].op == "mode"


def test_symlink_create_and_refusals(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    (upper / "link").symlink_to("/etc/passwd")
    (upper / "h1").write_text("h")
    os.link(upper / "h1", upper / "h2")
    (upper / "suid").write_text("s")
    os.chmod(upper / "suid", 0o4755)
    os.mkfifo(upper / "fifo")
    eff = extract("workspace", lower, upper, base)
    link = next(o for o in eff.ops if o.target == "link")
    assert link.op == "symlink" and link.link_target == "/etc/passwd"
    reasons = {r["path"]: r["reason"] for r in eff.refused}
    assert reasons == {"h1": "hardlink_in_shadow", "h2": "hardlink_in_shadow",
                       "suid": "setuid_or_setgid", "fifo": "special_file"}


def test_op_order_and_sequence(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    whiteout(upper / "old")
    (upper / "n1" / "n2").mkdir(parents=True)
    (upper / "n1" / "n2" / "f").write_text("f")
    eff = extract("workspace", lower, upper, base, seq_start=5)
    assert [o.seq for o in eff.ops] == list(range(5, 5 + len(eff.ops)))
    assert [o.op for o in eff.ops] == ["rmtree", "mkdir", "mkdir", "rename_in"]
    assert [o.target for o in eff.ops][1:3] == ["n1", "n1/n2"]


def test_extract_odd_names(tmp_path: Path):
    lower, upper = setup(tmp_path)
    base = fingerprint_tree(lower)
    for name in ["with space.txt", "-leading", "unicodé.txt", "new\nline"]:
        (upper / name).write_text("x")
    fd = os.open(os.path.join(os.fsencode(upper), b"bad\xff"), os.O_CREAT | os.O_WRONLY, 0o644)
    os.close(fd)
    eff = extract("workspace", lower, upper, base)
    assert {o.target for o in eff.ops} == {"with space.txt", "-leading", "unicodé.txt", "new\nline"}
    assert [r["reason"] for r in eff.refused] == ["non_utf8_name"]


def test_skip_ignores_subtree(tmp_path: Path):
    lower, upper = setup(tmp_path)
    (upper / "mnt" / "ws").mkdir(parents=True)
    (upper / "mnt" / "ws" / "f").write_text("f")
    eff = extract("tmp", lower, upper, fingerprint_tree(lower), skip=frozenset({"mnt/ws"}))
    assert {o.target for o in eff.ops} == {"mnt"}
