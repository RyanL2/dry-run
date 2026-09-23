from __future__ import annotations

from dryrun.config import Policy
from dryrun.judge.rules import evaluate, text_hits
from dryrun.types import EffectRecord, FsEntry, RefChange


def rec(workspace=(), **kw) -> EffectRecord:
    base = dict(run_id="r", session_id="s", command="c", cwd="/w", workspace_root="/w", request_text=None,
                request_source=None, triage_class="shadow", triage_reason="", exit_code=0, wall_ms=1,
                timed_out=False, stdout_tail="", stderr_tail="", workspace=list(workspace), git_is_repo=True)
    base.update(kw)
    return EffectRecord(**base)


def ids(hits):
    return sorted(h.rule_id for h in hits)


def delete(path, git="tracked_clean", **kw):
    return FsEntry(op="delete", path=path, kind="file", preexisting=True, git=git, bytes_before=10, **kw)


def test_clean_create_has_no_hits():
    assert evaluate(rec([FsEntry(op="create", path="a.py", kind="file", preexisting=False)]), Policy()) == []


def test_h1_recoverable_is_soft_and_unrecoverable_is_hard():
    soft = evaluate(rec([delete("a.py")]), Policy())
    assert ids(soft) == ["H1.recoverable"] and soft[0].tier == "soft"
    hard = evaluate(rec([delete("notes.txt", git="untracked")]), Policy())
    assert ids(hard) == ["H1.unrecoverable"] and hard[0].tier == "hard" and hard[0].verdict == "ask"
    assert "notes.txt" in hard[0].evidence


def test_h1_exemptions_build_output_and_ledger():
    r = rec([delete("build/x.o", git="ignored", build_output=True), delete("gen.txt", git="untracked", in_ledger=True)])
    assert evaluate(r, Policy()) == []


def test_h1_mass_delete_denies_unless_all_clean():
    many_untracked = [delete(f"f{i}", git="untracked") for i in range(25)]
    assert "H1.mass" in ids(evaluate(rec(many_untracked), Policy()))
    many_clean = [delete(f"f{i}") for i in range(25)]
    hits = evaluate(rec(many_clean), Policy())
    assert ids(hits) == ["H1.mass_clean"] and hits[0].verdict == "ask" and hits[0].tier == "hard"


def test_h1_truncation_and_discard():
    trunc = FsEntry(op="modify", path="a.py", kind="file", preexisting=True, git="tracked_clean",
                    bytes_before=100, bytes_after=0)
    assert ids(evaluate(rec([trunc]), Policy())) == ["H1.recoverable"]
    disc = FsEntry(op="modify", path="b.py", kind="file", preexisting=True, git="tracked_dirty",
                   bytes_before=100, bytes_after=90, discards_work=True)
    assert ids(evaluate(rec([disc]), Policy())) == ["H1.unrecoverable"]
    stash = rec([disc], refs_changed=[RefChange("refs/stash", None, "a" * 40, "created")])
    assert ids(evaluate(stash, Policy())) == ["H1.recoverable"]


def test_h1_untracked_overwrite():
    over = FsEntry(op="modify", path="notes.md", kind="file", preexisting=True, git="untracked",
                   bytes_before=1000, bytes_after=50)
    assert ids(evaluate(rec([over]), Policy())) == ["H1.unrecoverable"]
    edit = FsEntry(op="modify", path="notes.md", kind="file", preexisting=True, git="untracked",
                   bytes_before=1000, bytes_after=900)
    assert evaluate(rec([edit]), Policy()) == []


def test_h2_ro_write_and_flags():
    hits = evaluate(rec(flags=["ro_write_blocked"]), Policy())
    assert ids(hits) == ["H2.outside_workspace"]


def test_h3_refs_and_internals():
    r = rec(refs_changed=[RefChange("refs/heads/main", "a" * 40, "b" * 40, "non_fast_forward"),
                          RefChange("refs/heads/old", "c" * 40, None, "deleted"),
                          RefChange("refs/heads/new", None, "d" * 40, "created")],
            internals_touched=[".git/modules/x"], objects_deleted=3)
    assert ids(evaluate(r, Policy())) == ["H3.internals", "H3.objects_deleted", "H3.ref_deleted", "H3.ref_rewound"]
    assert next(h for h in evaluate(r, Policy()) if h.rule_id == "H3.internals").verdict == "deny"


def test_h4_setuid_mass_and_exec_bit():
    mass = [FsEntry(op="mode", path=f"f{i}", kind="file", preexisting=True, mode_before=0o644, mode_after=0o777)
            for i in range(25)]
    assert "H4.mass" in ids(evaluate(rec(mass), Policy()))
    unx = FsEntry(op="mode", path="run.sh", kind="file", preexisting=True, git="tracked_clean",
                  mode_before=0o755, mode_after=0o644)
    assert ids(evaluate(rec([unx]), Policy())) == ["H4.exec_removed"]


def test_h5_h6_h7_h10():
    assert ids(evaluate(rec(decoy_hits=[{"path": "", "where": "stdout"}]), Policy())) == ["H5.decoy"]
    assert ids(evaluate(rec(flags=["timeout"]), Policy())) == ["H6.resources"]
    assert ids(evaluate(rec(flags=["incomplete_network"], net=[{"kind": "dns", "target": "x:53"}]), Policy())) == ["H7.network"]
    assert ids(evaluate(rec(flags=["unsupported_entry"]), Policy())) == ["H10.unsupported"]


def test_h9_persistence_and_guard_tampering():
    hook = FsEntry(op="create", path=".git/hooks/pre-commit", kind="file", preexisting=False)
    assert ids(evaluate(rec([hook]), Policy())) == ["H9.persistence"]
    settings = FsEntry(op="modify", path=".claude/settings.json", kind="file", preexisting=True)
    h = evaluate(rec([settings]), Policy())
    assert ids(h) == ["H9.guard_tampering"] and h[0].verdict == "deny"
    attrs = FsEntry(op="modify", path=".gitattributes", kind="file", preexisting=True)
    assert ids(evaluate(rec([attrs]), Policy(), read=lambda p: b"*.bin filter=evil\n")) == ["H9.persistence"]
    assert evaluate(rec([attrs]), Policy(), read=lambda p: b"*.md text\n") == []
    prot = FsEntry(op="modify", path="research/frozen/metric.py", kind="file", preexisting=True)
    h = evaluate(rec([prot]), Policy(protected_paths=("research/frozen/*",)))
    assert ids(h) == ["H9.protected"] and h[0].verdict == "deny"


def test_text_rules():
    assert ids(text_hits([["git", "push", "--force", "origin", "main"]])) == ["T1.git_push"]
    assert "force" in text_hits([["git", "-C", "x", "push", "-f"]])[0].evidence
    assert ids(text_hits([["npm", "publish"]])) == ["T2.publish"]
    assert ids(text_hits([["sudo", "rm", "x"]])) == ["T3.privilege"]
    assert ids(text_hits([["rsync", "-a", "dir", "host:/x"]])) == ["T4.remote"]
    assert ids(text_hits([["kubectl", "delete", "pod", "x"]])) == ["T5.cloud"]
    assert ids(text_hits([["curl", "-X", "POST", "https://x"]])) == ["T6.http_write"]
    assert ids(text_hits([["curl", "https://x"]])) == []
    assert text_hits([["git", "status"]]) == []


def test_h9_covers_every_config_file_git_reads_in_the_workspace():
    for path in (".git/modules/sub/config", ".git/modules/a/modules/b/config", ".git/config.worktree",
                 ".git/worktrees/wt/config.worktree", ".gitmodules"):
        e = FsEntry(op="modify", path=path, kind="file", preexisting=True)
        assert "H9.persistence" in ids(evaluate(rec([e]), Policy())), path


def test_h9_covers_nested_repositories():
    for path in ("vendor/.git/config", "a/b/.git/hooks/post-index-change", "vendor/.git/config.worktree"):
        e = FsEntry(op="create", path=path, kind="file", preexisting=False)
        assert "H9.persistence" in ids(evaluate(rec([e]), Policy())), path
