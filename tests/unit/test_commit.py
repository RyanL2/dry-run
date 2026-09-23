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
    with pytest.raises(OSError):
        apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    assert (store.journal_dir / f"{run.run_id}.json").exists()
    monkeypatch.setattr(confined.Root, "rename_in", real)
    assert recover_all(store) == [run.run_id]
    assert (ws / "src" / "a.py").read_text() == "a2\n" and (ws / "new" / "deep" / "n.txt").exists()
    assert not (store.journal_dir / f"{run.run_id}.json").exists()
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
