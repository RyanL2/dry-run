from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest

import dryrun.confined as confined
from dryrun.commit import CommitError, apply_changeset, apply_run, recover_all
from dryrun.config import load_config
from dryrun.effects.upper import extract
from dryrun.fingerprint import fingerprint_tree
from dryrun.store import Store
from dryrun.types import ChangeSet


def whiteout(p: Path) -> None:
    p.write_bytes(b"")
    os.setxattr(p, "user.overlay.whiteout", b"y")


def build(tmp_path: Path):
    ws, up = tmp_path / "ws", tmp_path / "up"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("a1\n")
    (ws / "old").mkdir()
    (ws / "old" / "x").write_text("x")
    (ws / "keep.txt").write_text("keep\n")
    base = fingerprint_tree(ws)
    (up / "src").mkdir(parents=True)
    (up / "src" / "a.py").write_text("a2\n")
    (up / "new" / "deep").mkdir(parents=True)
    (up / "new" / "deep" / "n.txt").write_text("n\n")
    os.chmod(up / "new" / "deep", 0o555)
    (up / "ln").symlink_to("keep.txt")
    whiteout(up / "old")
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id="r1", roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    return ws, up, cs


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        st = os.lstat(p)
        out[rel] = (st.st_mode, os.readlink(p) if p.is_symlink() else (p.read_bytes() if p.is_file() else None))
    return out


def unlock(*dirs: Path) -> None:
    for d in dirs:
        if d.exists():
            os.chmod(d, 0o755)


def test_apply_produces_reviewed_tree(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    mtime = os.stat(up / "src" / "a.py").st_mtime_ns
    report = apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert (ws / "src" / "a.py").read_text() == "a2\n"
    assert os.stat(ws / "src" / "a.py").st_mtime_ns == mtime
    assert (ws / "new" / "deep" / "n.txt").read_text() == "n\n"
    assert os.stat(ws / "new" / "deep").st_mode & 0o777 == 0o555
    assert os.readlink(ws / "ln") == "keep.txt"
    assert not (ws / "old").exists()
    assert set(report.created) == {"new", "new/deep", "new/deep/n.txt", "ln"}
    assert not (tmp_path / "j.json").exists()
    unlock(ws / "new" / "deep", up / "new" / "deep")


def test_apply_conflict_writes_nothing(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    (ws / "src" / "a.py").write_text("user edit\n")
    before = snapshot(ws)
    with pytest.raises(CommitError) as exc:
        apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert exc.value.code == 4
    assert snapshot(ws) == before
    unlock(up / "new" / "deep")


def test_symlink_swap_between_shadow_and_commit(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(ws / "src")
    (ws / "src").symlink_to(outside)
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert list(outside.iterdir()) == []
    unlock(up / "new" / "deep")


def test_file_replaced_by_directory_is_not_a_conflict(tmp_path: Path):
    ws, up = tmp_path / "ws", tmp_path / "up"
    ws.mkdir()
    (ws / "other.txt").write_text("was a file\n")
    base = fingerprint_tree(ws)
    (up / "other.txt").mkdir(parents=True)
    os.setxattr(up / "other.txt", "user.overlay.opaque", b"y")
    (up / "other.txt" / "x").write_text("in\n")
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id="r1", roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert (ws / "other.txt" / "x").read_text() == "in\n"


def test_refused_changeset_is_not_applied(tmp_path: Path):
    ws, up, cs = build(tmp_path)
    cs.refused.append({"path": "h", "reason": "hardlink_in_shadow"})
    with pytest.raises(CommitError) as exc:
        apply_changeset(cs, journal_path=tmp_path / "j.json")
    assert exc.value.code == 3
    unlock(up / "new" / "deep")


def test_recover_after_crash_mid_commit(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up, cs = build(tmp_path)
    cs.run_id = run.run_id
    run.changeset.write_text(json.dumps(cs.to_json()))
    store.save_meta(run, status="applying", session_id="s", exit_code=0)
    real = confined.Root.rename_in
    calls = {"n": 0}

    def flaky(self, src, rel, *, noreplace):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated crash")
        return real(self, src, rel, noreplace=noreplace)

    monkeypatch.setattr(confined.Root, "rename_in", flaky)
    with pytest.raises(CommitError, match="partially applied") as exc:
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    assert exc.value.code == 5
    assert (store.journal_dir / f"{run.run_id}.json").exists()
    monkeypatch.setattr(confined.Root, "rename_in", real)
    assert recover_all(store) == [run.run_id]
    assert (ws / "src" / "a.py").read_text() == "a2\n" and (ws / "new" / "deep" / "n.txt").exists()
    assert not (store.journal_dir / f"{run.run_id}.json").exists()
    unlock(ws / "new" / "deep", up / "new" / "deep")


def _crash_before_any_op(monkeypatch):
    import dryrun.commit as commit_mod

    def boom(roots, cs, done=None):
        raise OSError("simulated crash right after the journal was written")

    monkeypatch.setattr(commit_mod, "_run_ops", boom)
    return commit_mod


def test_recovery_refuses_to_clobber_changes_made_after_the_crash(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up, cs = build(tmp_path)
    cs.run_id = run.run_id
    store.save_meta(run, status="applying", session_id="s")
    real_run_ops = __import__("dryrun.commit", fromlist=["_run_ops"])._run_ops
    commit_mod = _crash_before_any_op(monkeypatch)
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    monkeypatch.setattr(commit_mod, "_run_ops", real_run_ops)
    (ws / "src" / "a.py").write_text("the user's newer edit\n")        # target of a rename_in op
    (ws / "old" / "user_new.txt").write_text("created after the crash\n")  # inside a dir the journal would rmtree
    assert recover_all(store) == []
    assert (ws / "src" / "a.py").read_text() == "the user's newer edit\n"
    assert (ws / "old" / "user_new.txt").exists()
    assert not (store.journal_dir / f"{run.run_id}.json").exists()      # retired, not replayed forever
    assert list(store.journal_dir.glob(f"{run.run_id}.json.failed"))
    assert recover_all(store) == []
    unlock(up / "new" / "deep")


def test_recovery_finishes_a_crash_between_two_ops_on_the_same_path(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up = tmp_path / "ws", tmp_path / "up"
    ws.mkdir()
    (ws / "other.txt").write_text("was a file\n")
    base = fingerprint_tree(ws)
    (up / "other.txt").mkdir(parents=True)
    os.setxattr(up / "other.txt", "user.overlay.opaque", b"y")
    (up / "other.txt" / "x").write_text("in\n")
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    assert [o.op for o in cs.ops if o.target == "other.txt"][:2] == ["unlink", "mkdir"]
    run.changeset.write_text(json.dumps(cs.to_json()))
    store.save_meta(run, status="applying", session_id="s")
    real = confined.Root.mkdir

    def crash(self, rel, mode):
        raise OSError("simulated crash after the unlink")

    monkeypatch.setattr(confined.Root, "mkdir", crash)
    with pytest.raises(CommitError, match="partially applied"):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    assert not (ws / "other.txt").exists()
    monkeypatch.setattr(confined.Root, "mkdir", real)
    assert recover_all(store) == [run.run_id]
    assert (ws / "other.txt" / "x").read_text() == "in\n"


def test_recover_all_survives_a_corrupt_journal(tmp_path: Path):
    store = Store(tmp_path / "state")
    (store.journal_dir / "68d2f1a3-0badc0de.json").write_text("{not json")
    (store.journal_dir / "68d2f1a3-0badc0df.json").write_text(json.dumps({"state": "applying", "changeset": {}}))
    assert recover_all(store) == []


def test_apply_run_reports_partial_failure_honestly(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up, cs = build(tmp_path)
    run.changeset.write_text(json.dumps(cs.to_json()))
    token = store.authorize(run, session_id="s1", decision="allow")
    real = confined.Root.rename_in
    calls = {"n": 0}

    def flaky(self, src, rel, *, noreplace):
        calls["n"] += 1
        if calls["n"] == 2:
            raise FileExistsError(17, "File exists", rel)
        return real(self, src, rel, noreplace=noreplace)

    monkeypatch.setattr(confined.Root, "rename_in", flaky)
    err = io.BytesIO()
    code = apply_run(store, run.run_id, token, cfg=load_config(use_user_file=False), out=io.BytesIO(), err=err)
    assert code == 5
    assert b"nothing was written" not in err.getvalue() and b"partially" in err.getvalue()
    assert store.load_meta(run)["status"] == "failed"
    unlock(ws / "new" / "deep", up / "new" / "deep")


def test_apply_run_replays_output_and_is_single_use(tmp_path: Path):
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up, cs = build(tmp_path)
    run.changeset.write_text(json.dumps(cs.to_json()))
    run.stdout.write_bytes(b"built ok\n")
    run.stderr.write_bytes(b"warning\n")
    token = store.authorize(run, session_id="s1", decision="allow")
    store.save_meta(run, exit_code=0)
    out, err = io.BytesIO(), io.BytesIO()
    cfg = load_config(use_user_file=False)
    assert apply_run(store, run.run_id, token, cfg=cfg, out=out, err=err) == 0
    assert out.getvalue() == b"built ok\n" and err.getvalue() == b"warning\n"
    assert "new/deep/n.txt" in store.ledger("s1", str(ws))
    assert apply_run(store, run.run_id, token, cfg=cfg, out=io.BytesIO(), err=io.BytesIO()) == 3
    unlock(ws / "new" / "deep", up / "new" / "deep")


def _dir_replacement(tmp_path: Path):
    """`rm -rf D && mkdir D && touch D/new.txt`: an opaque dir, recorded as rmtree D, mkdir D, rename_in."""
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up = tmp_path / "ws", tmp_path / "up"
    (ws / "D").mkdir(parents=True)
    (ws / "D" / "old1").write_text("1")
    (ws / "D" / "old2").write_text("2")
    base = fingerprint_tree(ws)
    (up / "D").mkdir(parents=True)
    os.setxattr(up / "D", "user.overlay.opaque", b"y")
    (up / "D" / "new.txt").write_text("n")
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    assert [o.op for o in cs.ops if o.target == "D"][:2] == ["rmtree", "mkdir"]
    run.changeset.write_text(json.dumps(cs.to_json()))
    store.save_meta(run, status="applying", session_id="s")
    return store, run, ws, cs


@pytest.mark.parametrize("after_crash,expected", [
    ("untouched", ["new.txt"]),                                  # nothing happened: recovery finishes the commit
    ("partial_rmtree", ["old2"]),                                # crash half-way through deleting D: refuse
    ("user_added_file", ["old1", "old2", "user.txt"]),           # user changed D after the crash: refuse
])
def test_recovery_of_a_directory_replacement(tmp_path: Path, monkeypatch, after_crash, expected):
    store, run, ws, cs = _dir_replacement(tmp_path)
    real_run_ops = __import__("dryrun.commit", fromlist=["_run_ops"])._run_ops
    commit_mod = _crash_before_any_op(monkeypatch)
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    monkeypatch.setattr(commit_mod, "_run_ops", real_run_ops)
    if after_crash == "partial_rmtree":
        (ws / "D" / "old1").unlink()
    elif after_crash == "user_added_file":
        (ws / "D" / "user.txt").write_text("mine")
    recovered = recover_all(store)
    assert sorted(os.listdir(ws / "D")) == expected
    assert recovered == ([run.run_id] if after_crash == "untouched" else [])


def _replacement_reusing_a_name(tmp_path: Path):
    """`rm -rf D && mkdir D && echo NEW > D/old1`: the new file reuses a name from the deleted directory."""
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up = tmp_path / "ws", tmp_path / "up"
    (ws / "D").mkdir(parents=True)
    (ws / "D" / "old1").write_text("1")
    (ws / "D" / "old2").write_text("2")
    base = fingerprint_tree(ws)
    (up / "D").mkdir(parents=True)
    os.setxattr(up / "D", "user.overlay.opaque", b"y")
    (up / "D" / "old1").write_text("NEW")
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    run.changeset.write_text(json.dumps(cs.to_json()))
    store.save_meta(run, status="applying", session_id="s")
    return store, run, ws, cs


@pytest.mark.parametrize("crash", ["before_any_op", "after_rmtree", "after_mkdir_before_its_marker"])
def test_recovery_finishes_a_replacement_that_reuses_a_name(tmp_path: Path, monkeypatch, crash):
    import dryrun.commit as commit_mod
    store, run, ws, cs = _replacement_reusing_a_name(tmp_path)
    journal = store.journal_dir / f"{run.run_id}.json"
    mkdir_seq = next(o.seq for o in cs.ops if o.op == "mkdir")
    if crash == "before_any_op":
        real_run_ops = commit_mod._run_ops
        _crash_before_any_op(monkeypatch)
        with pytest.raises(CommitError):
            apply_changeset(cs, journal_path=journal)
        monkeypatch.setattr(commit_mod, "_run_ops", real_run_ops)
    elif crash == "after_rmtree":
        real = confined.Root.mkdir
        monkeypatch.setattr(confined.Root, "mkdir", lambda self, rel, mode: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(CommitError):
            apply_changeset(cs, journal_path=journal)
        monkeypatch.setattr(confined.Root, "mkdir", real)
    else:
        real_writer = commit_mod._marker_writer

        def writer(path):
            done = real_writer(path)

            def maybe(seq):
                if seq == mkdir_seq:
                    raise OSError("crash after mkdir, before its marker")
                done(seq)
            return maybe

        monkeypatch.setattr(commit_mod, "_marker_writer", writer)
        with pytest.raises(CommitError):
            apply_changeset(cs, journal_path=journal)
        monkeypatch.setattr(commit_mod, "_marker_writer", real_writer)
    assert recover_all(store) == [run.run_id]
    assert sorted(os.listdir(ws / "D")) == ["old1"]
    assert (ws / "D" / "old1").read_text() == "NEW"


def test_recovery_refusal_changes_nothing(tmp_path: Path, monkeypatch):
    import dryrun.commit as commit_mod
    store, run, ws, cs = _replacement_reusing_a_name(tmp_path)
    real_run_ops = commit_mod._run_ops
    _crash_before_any_op(monkeypatch)
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    monkeypatch.setattr(commit_mod, "_run_ops", real_run_ops)
    (ws / "D" / "old2").write_text("edited after the crash")        # a path no op targets directly
    (ws / "D" / "old1").write_text("also edited")                    # the target of the last op
    assert recover_all(store) == []
    assert sorted(os.listdir(ws / "D")) == ["old1", "old2"]           # the rmtree did not run first
    assert (ws / "D" / "old1").read_text() == "also edited"


def _chmod_dir_and_new_file(tmp_path: Path):
    """`chmod 700 P && echo n > P/new`: ops rename_in P/new, then chmod P (directory modes come last)."""
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up = tmp_path / "ws", tmp_path / "up"
    (ws / "P").mkdir(parents=True)
    (ws / "P" / "old").write_text("o")
    os.chmod(ws / "P", 0o755)
    base = fingerprint_tree(ws)
    (up / "P").mkdir(parents=True)
    (up / "P" / "new").write_text("n")
    os.chmod(up / "P", 0o700)
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    assert [(o.op, o.target) for o in cs.ops] == [("rename_in", "P/new"), ("chmod", "P")]
    run.changeset.write_text(json.dumps(cs.to_json()))
    store.save_meta(run, status="applying", session_id="s")
    return store, run, ws, cs


@pytest.mark.parametrize("point", ["before:0", "marker:0", "before:1", "marker:1"])
def test_recovery_finishes_a_directory_chmod_at_every_crash_point(tmp_path: Path, monkeypatch, point):
    import dryrun.commit as commit_mod
    store, run, ws, cs = _chmod_dir_and_new_file(tmp_path)
    kind, k = point.split(":")
    seq = cs.ops[int(k)].seq
    real_apply, real_writer = commit_mod._apply_one, commit_mod._marker_writer

    def apply(root, op, dir_modes):
        if kind == "before" and op.seq == seq:
            raise OSError("crash before the op")
        real_apply(root, op, dir_modes)

    def writer(path):
        done = real_writer(path)

        def mark(s):
            if kind == "marker" and s == seq:
                raise OSError("crash before the marker")
            done(s)
        return mark

    monkeypatch.setattr(commit_mod, "_apply_one", apply)
    monkeypatch.setattr(commit_mod, "_marker_writer", writer)
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    monkeypatch.setattr(commit_mod, "_apply_one", real_apply)
    monkeypatch.setattr(commit_mod, "_marker_writer", real_writer)
    assert recover_all(store) == [run.run_id]
    assert (ws / "P" / "new").read_text() == "n"
    assert os.stat(ws / "P").st_mode & 0o777 == 0o700


def _unsearchable_new_dir(tmp_path: Path):
    """`mkdir d && echo a > d/c && chmod 600 d`: once d has its mode, nothing below it can be examined."""
    store = Store(tmp_path / "state")
    run = store.new_run()
    ws, up = tmp_path / "ws", tmp_path / "up"
    ws.mkdir()
    base = fingerprint_tree(ws)
    (up / "d").mkdir(parents=True)
    (up / "d" / "c").write_text("a")
    os.chmod(up / "d", 0o600)
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=eff.refused)
    run.changeset.write_text(json.dumps(cs.to_json()))
    store.save_meta(run, status="applying", session_id="s")
    return store, run, ws, cs


def test_commit_applies_and_verifies_an_unsearchable_directory(tmp_path: Path):
    store, run, ws, cs = _unsearchable_new_dir(tmp_path)
    apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    assert os.stat(ws / "d").st_mode & 0o777 == 0o600
    os.chmod(ws / "d", 0o700)
    assert (ws / "d" / "c").read_text() == "a"


def test_recovery_after_a_crash_while_applying_directory_modes(tmp_path: Path, monkeypatch):
    store, run, ws, cs = _unsearchable_new_dir(tmp_path)
    real = confined.Root.chmod

    def chmod_then_crash(self, rel, mode):
        real(self, rel, mode)
        if rel == "d" and mode == 0o600:
            raise OSError("crash right after d got its final mode")

    monkeypatch.setattr(confined.Root, "chmod", chmod_then_crash)
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    monkeypatch.setattr(confined.Root, "chmod", real)
    assert recover_all(store) == [run.run_id]
    assert os.stat(ws / "d").st_mode & 0o777 == 0o600
    os.chmod(ws / "d", 0o700)
    assert (ws / "d" / "c").read_text() == "a"


def test_commit_refuses_before_writing_into_a_directory_it_cannot_write(tmp_path: Path):
    ws, up = tmp_path / "ws", tmp_path / "up"
    (ws / "RO").mkdir(parents=True)
    (ws / "x").write_text("x")
    base = fingerprint_tree(ws)
    (up / "RO").mkdir(parents=True)
    (up / "RO" / "new").write_text("n")
    whiteout(up / "x")
    eff = extract("workspace", ws, up, base)
    cs = ChangeSet(run_id="r1", roots={"workspace": str(ws)}, base_digest="x", ops=eff.ops, refused=[])
    os.chmod(ws / "RO", 0o555)                                       # became read-only after the shadow run
    try:
        with pytest.raises(CommitError) as exc:
            apply_changeset(cs, journal_path=tmp_path / "j.json")
        assert exc.value.code == 3 and not exc.value.partial
        assert (ws / "x").exists() and not (ws / "RO" / "new").exists()
    finally:
        os.chmod(ws / "RO", 0o755)


def test_recovery_does_not_take_a_foreign_directory_for_its_mkdir(tmp_path: Path, monkeypatch):
    store, run, ws, cs = _dir_replacement(tmp_path)                  # rmtree D, mkdir D, rename_in D/new.txt
    real = confined.Root.mkdir
    monkeypatch.setattr(confined.Root, "mkdir", lambda self, rel, mode: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(CommitError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    monkeypatch.setattr(confined.Root, "mkdir", real)
    (ws / "D").mkdir()
    (ws / "D" / "mine").write_text("the user's")                    # recreated by the user after the crash
    assert recover_all(store) == []
    assert sorted(os.listdir(ws / "D")) == ["mine"]
