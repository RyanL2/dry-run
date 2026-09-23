### Task 12: Rules, cascade and model slot (F7, F8)

**Files:**
- Create: `src/dryrun/judge/__init__.py` (empty), `src/dryrun/judge/model.py`, `src/dryrun/judge/rules.py`, `src/dryrun/judge/cascade.py`, `tests/unit/test_rules.py`, `tests/unit/test_cascade.py`

**Interfaces:**
- Consumes: `EffectRecord`, `FsEntry`, `RefChange`, `Decision`, `RERUN_FLAGS` (Task 2); `Policy` (Task 1)
- Produces:
  - `Hit(rule_id: str, clause: str, verdict: str, tier: str, evidence: str)`
    - `verdict` ∈ `deny|ask`
    - `tier` ∈ `hard|soft`
  - `evaluate(rec: EffectRecord, policy: Policy, read: Callable[[str], bytes | None] = lambda p: None) -> list[Hit]`
    - clauses H1–H10
    - `read(rel)` returns the post-run content of a workspace file (used for `.gitattributes` in H9)
  - `text_hits(argvs: list[list[str]]) -> list[Hit]`: clauses T1–T6 on parsed simple commands
  - `EffectJudge` (Protocol: `score(rec) -> dict[str, float]`), `NullJudge`
  - `decide(triage_class: str, *, triage_reason: str = "", text: list[Hit] = (), rec: EffectRecord | None = None, hits: list[Hit] = (), judge: EffectJudge = NullJudge(), dev_server_allowed: bool = False) -> Decision`
    - Follows the ARCHITECTURE §4 cascade: hard deny → flags (ask + rerun) → hard ask (ask + commit) → soft (ask + commit; the model may lower it in sub-project 3) → allow + commit.
    - The returned `Decision` has no `run_id` or `token`; the pipeline adds them.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_rules.py`:
```python
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
```

`tests/unit/test_cascade.py`:
```python
from __future__ import annotations

from dryrun.judge.cascade import decide
from dryrun.judge.rules import Hit
from dryrun.types import EffectRecord


def rec(**kw) -> EffectRecord:
    base = dict(run_id="r", session_id="s", command="c", cwd="/w", workspace_root="/w", request_text=None,
                request_source=None, triage_class="shadow", triage_reason="", exit_code=0, wall_ms=1,
                timed_out=False, stdout_tail="", stderr_tail="")
    base.update(kw)
    return EffectRecord(**base)


H = lambda rid, v, t: Hit(rid, rid.split(".")[0], v, t, f"evidence for {rid}")  # noqa: E731


def test_non_shadow_classes():
    assert (decide("read_only").decision, decide("read_only").mode) == ("allow", "passthrough")
    assert decide("apply").decision == "deny"
    d = decide("non_shadowable", text=[H("T1.git_push", "ask", "hard")])
    assert (d.decision, d.mode, d.rule_ids) == ("ask", "passthrough", ["T1.git_push"])
    assert (decide("long_running", dev_server_allowed=True).decision,
            decide("long_running", dev_server_allowed=True).mode) == ("allow", "rerun")
    assert decide("long_running").decision == "ask"


def test_shadow_order_hard_deny_beats_flags():
    d = decide("shadow", rec=rec(flags=["timeout"]), hits=[H("H5.decoy", "deny", "hard"), H("H6.resources", "ask", "hard")])
    assert (d.decision, d.rule_ids[0]) == ("deny", "H5.decoy")


def test_shadow_flags_give_ask_rerun():
    d = decide("shadow", rec=rec(flags=["incomplete_network"]), hits=[H("H7.network", "ask", "hard")])
    assert (d.decision, d.mode) == ("ask", "rerun")


def test_tmp_partial_alone_does_not_force_rerun():
    d = decide("shadow", rec=rec(flags=["tmp_partial"]), hits=[])
    assert (d.decision, d.mode) == ("allow", "commit")


def test_hard_and_soft_ask_commit_and_clean_allow():
    assert (decide("shadow", rec=rec(), hits=[H("H1.unrecoverable", "ask", "hard")]).mode) == "commit"
    d = decide("shadow", rec=rec(), hits=[H("H1.recoverable", "ask", "soft")])
    assert (d.decision, d.mode) == ("ask", "commit")
    d = decide("shadow", rec=rec(), hits=[])
    assert (d.decision, d.mode, d.rule_ids) == ("allow", "commit", [])


def test_reason_is_bounded_and_prefixed_by_top_rule():
    d = decide("shadow", rec=rec(), hits=[Hit("H1.unrecoverable", "H1", "ask", "hard", "x" * 500)])
    assert len(d.reason) <= 200 and d.reason.startswith("H1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... bash scripts/dev/test.sh tests/unit/test_rules.py tests/unit/test_cascade.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.judge'`)

- [ ] **Step 3: Implement**

`src/dryrun/judge/__init__.py`: empty file.

`src/dryrun/judge/model.py`:
```python
"""Model slot (sub-project 3). v1 ships NullJudge: rules only, which is also the rules-only ablation."""
from __future__ import annotations

from typing import Protocol

from dryrun.types import EffectRecord


class EffectJudge(Protocol):
    def score(self, rec: EffectRecord) -> dict[str, float]:
        """Calibrated P(harm) per harm-policy clause, e.g. {"H1": 0.03, "H8": 0.4}."""


class NullJudge:
    def score(self, rec: EffectRecord) -> dict[str, float]:
        return {}
```

`src/dryrun/judge/rules.py`:
```python
"""Harm-policy clauses (docs/harm-policy.md) as predicates over the EffectRecord."""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Callable

from dryrun.config import Policy
from dryrun.types import EffectRecord, FsEntry

PERSISTENCE = (".git/hooks", ".git/hooks/*", ".git/config", ".git/info/attributes", ".claude", ".claude/*",
               ".mcp.json", ".envrc", ".husky/*", ".vscode/tasks.json", ".devcontainer/*")
GUARD = (".claude/settings.json", ".claude/settings.local.json")
UNRECOVERABLE = {"untracked", "tracked_dirty", None}
_DRIVER = re.compile(rb"\b(filter|diff|merge)=")


@dataclass
class Hit:
    rule_id: str
    clause: str
    verdict: str
    tier: str
    evidence: str


def _names(entries: list[FsEntry], limit: int = 3) -> str:
    shown = ", ".join(e.path for e in entries[:limit])
    return shown + (f" (+{len(entries) - limit} more)" if len(entries) > limit else "")


def _destroyed(e: FsEntry, policy: Policy) -> bool:
    if e.build_output or e.in_ledger or not e.preexisting or e.kind == "dir":
        return False
    if e.op == "delete":
        return True
    if e.op != "modify" or e.kind != "file":
        return False
    if e.discards_work:
        return True
    if e.bytes_after == 0 and (e.bytes_before or 0) > 0:
        return True
    if e.git == "untracked" and e.bytes_before:
        return (e.bytes_after or 0) < (1 - policy.h1_overwrite_fraction) * e.bytes_before
    return False


def _h1(rec: EffectRecord, policy: Policy) -> list[Hit]:
    gone = [e for e in rec.workspace if _destroyed(e, policy)]
    if not gone:
        return []
    stash_saved = any(r.ref == "refs/stash" and r.after for r in rec.refs_changed)

    def unrecoverable(e: FsEntry) -> bool:
        if e.discards_work and stash_saved:
            return False
        return e.git in UNRECOVERABLE or e.discards_work

    lost = [e for e in gone if unrecoverable(e)]
    if len(gone) > policy.h1_mass_threshold:
        if lost:
            return [Hit("H1.mass", "H1", "deny", "hard",
                        f"would destroy {len(gone)} pre-existing files, {len(lost)} not recoverable from git: {_names(lost)}")]
        return [Hit("H1.mass_clean", "H1", "ask", "hard",
                    f"would delete {len(gone)} pre-existing tracked files (recoverable from git): {_names(gone)}")]
    if lost:
        return [Hit("H1.unrecoverable", "H1", "ask", "hard",
                    f"would destroy {len(lost)} pre-existing file(s) not recoverable from git: {_names(lost)}")]
    return [Hit("H1.recoverable", "H1", "ask", "soft",
                f"would delete or truncate {len(gone)} tracked file(s), recoverable from git: {_names(gone)}")]


def _h3(rec: EffectRecord) -> list[Hit]:
    hits = []
    if rec.internals_touched:
        hits.append(Hit("H3.internals", "H3", "deny", "hard",
                        f"writes git internals directly: {', '.join(rec.internals_touched[:3])}"))
    if rec.objects_deleted:
        hits.append(Hit("H3.objects_deleted", "H3", "ask", "hard",
                        f"deletes {rec.objects_deleted} git object file(s) (prune/gc)"))
    deleted = [r.ref for r in rec.refs_changed if r.change == "deleted" and not r.ref.startswith("refs/remotes/")]
    if deleted:
        hits.append(Hit("H3.ref_deleted", "H3", "ask", "hard", f"deletes git ref(s): {', '.join(deleted[:3])}"))
    rewound = [r.ref for r in rec.refs_changed
               if r.change in ("non_fast_forward", "unknown") and (r.ref.startswith("refs/heads/") or r.ref == "refs/stash")]
    if rewound:
        hits.append(Hit("H3.ref_rewound", "H3", "ask", "hard",
                        f"moves branch(es) to a non-descendant commit: {', '.join(rewound[:3])}"))
    return hits


def _h4(rec: EffectRecord, policy: Policy) -> list[Hit]:
    modes = [e for e in rec.workspace if e.op == "mode"]
    special = [e for e in rec.workspace + rec.tmp if (e.mode_after or 0) & 0o6000]
    hits = []
    if special:
        hits.append(Hit("H4.setuid", "H4", "deny", "hard", f"sets setuid/setgid on {_names(special)}"))
    if len(modes) > policy.h4_mass_threshold:
        hits.append(Hit("H4.mass", "H4", "ask", "hard", f"changes permissions on {len(modes)} pre-existing paths"))
    unx = [e for e in modes if e.git not in (None, "untracked", "ignored") and (e.mode_before or 0) & 0o111
           and not (e.mode_after or 0) & 0o111]
    if unx and len(modes) <= policy.h4_mass_threshold:
        hits.append(Hit("H4.exec_removed", "H4", "ask", "soft", f"removes the execute bit from {_names(unx)}"))
    return hits


def _h9(rec: EffectRecord, policy: Policy, read: Callable[[str], bytes | None]) -> list[Hit]:
    hits = []
    touched = [e for e in rec.workspace if e.op in ("create", "modify", "delete", "mode")]
    guard = [e for e in touched if e.path in GUARD]
    if guard:
        hits.append(Hit("H9.guard_tampering", "H9", "deny", "hard",
                        f"changes Claude Code settings: {_names(guard)}"))
    prot = [e for e in touched if any(fnmatch.fnmatchcase(e.path, p) for p in policy.protected_paths)]
    if prot:
        hits.append(Hit("H9.protected", "H9", "deny", "hard", f"changes protected paths: {_names(prot)}"))
    persist = [e for e in touched if e not in guard and any(fnmatch.fnmatchcase(e.path, p) for p in PERSISTENCE)]
    for e in touched:
        if os.path.basename(e.path) == ".gitattributes" and e.op in ("create", "modify"):
            content = read(e.path) or b""
            if _DRIVER.search(content):
                persist.append(e)
    if persist:
        hits.append(Hit("H9.persistence", "H9", "ask", "hard",
                        f"changes files that run code later: {_names(persist)}"))
    return hits


def evaluate(rec: EffectRecord, policy: Policy, read: Callable[[str], bytes | None] = lambda p: None) -> list[Hit]:
    hits = _h1(rec, policy)
    if "ro_write_blocked" in rec.flags:
        hits.append(Hit("H2.outside_workspace", "H2", "ask", "hard",
                        "tried to write outside the workspace and /tmp (blocked in the shadow)"))
    hits += _h3(rec)
    hits += _h4(rec, policy)
    if rec.decoy_hits:
        hits.append(Hit("H5.decoy", "H5", "deny", "hard",
                        f"exposed a credential decoy via {', '.join(h['where'] for h in rec.decoy_hits[:3])}"))
    if {"timeout", "resource_limit"} & set(rec.flags):
        hits.append(Hit("H6.resources", "H6", "ask", "hard", "hit a time, memory, process or disk limit"))
    if "incomplete_network" in rec.flags:
        targets = ", ".join(n["target"] for n in rec.net[:3])
        hits.append(Hit("H7.network", "H7", "ask", "hard", f"tried to use the network ({targets}); shadow is incomplete"))
    hits += _h9(rec, policy, read)
    if "unsupported_entry" in rec.flags:
        hits.append(Hit("H10.unsupported", "H10", "ask", "hard",
                        "produced effects the shadow cannot commit faithfully (hard links, special files)"))
    return hits


def _strip_git_globals(argv: list[str]) -> list[str]:
    out, i = [], 1
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if a.startswith("--git-dir=") or a.startswith("--work-tree=") or a.startswith("-c"):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


_PUBLISH = {("npm", "publish"), ("yarn", "publish"), ("pnpm", "publish"), ("twine", "upload"),
            ("cargo", "publish"), ("gem", "push"), ("poetry", "publish"), ("docker", "push"),
            ("helm", "push"), ("gh", "release")}
_CLOUD = {"aws", "gcloud", "az", "kubectl", "heroku", "fly", "flyctl", "vercel", "netlify", "firebase", "doctl"}
_CLOUD_SUB = {("terraform", "apply"), ("terraform", "destroy"), ("terraform", "import"), ("pulumi", "up"),
              ("pulumi", "destroy"), ("gh", "pr"), ("gh", "issue"), ("gh", "repo"), ("gh", "api")}
_PRIV = {"sudo", "su", "doas", "pkexec"}
_REMOTE = {"ssh", "scp", "sftp", "mosh"}
_DB = {"psql", "mysql", "mongo", "mongosh", "redis-cli"}


def text_hits(argvs: list[list[str]]) -> list[Hit]:
    hits: list[Hit] = []
    for argv in argvs:
        if not argv:
            continue
        name = os.path.basename(argv[0])
        rest = argv[1:]
        first = rest[0] if rest else ""
        if name == "git":
            sub = _strip_git_globals(argv)
            if sub and sub[0] == "push":
                force = any(a in ("-f", "--force", "--force-with-lease", "--delete", "-d", "--mirror")
                            or a.startswith("+") or a.startswith("--force") or a.startswith(":") for a in sub[1:])
                hits.append(Hit("T1.git_push", "T1", "ask", "hard",
                                "pushes to a remote" + (" with force/delete" if force else "") + ": " + " ".join(argv)[:120]))
        elif (name, first) in _PUBLISH:
            hits.append(Hit("T2.publish", "T2", "ask", "hard", f"publishes a package: {' '.join(argv)[:120]}"))
        elif name in _PRIV:
            hits.append(Hit("T3.privilege", "T3", "ask", "hard", f"runs with elevated privileges: {' '.join(argv)[:120]}"))
        elif name in _REMOTE or (name == "rsync" and any(":" in a and not a.startswith(("/", ".", "-")) for a in rest)):
            hits.append(Hit("T4.remote", "T4", "ask", "hard", f"acts on a remote host: {' '.join(argv)[:120]}"))
        elif name in _CLOUD or (name, first) in _CLOUD_SUB or (
                name in _DB and any(a in ("-h", "--host") or a.startswith("--host=") for a in rest)):
            hits.append(Hit("T5.cloud", "T5", "ask", "hard", f"acts on cloud or remote services: {' '.join(argv)[:120]}"))
        elif name in ("curl", "wget", "http", "https", "xh"):
            write = False
            for i, a in enumerate(rest):
                if a in ("-X", "--request") and i + 1 < len(rest) and rest[i + 1].upper() in ("POST", "PUT", "DELETE", "PATCH"):
                    write = True
                if a.startswith(("-d", "--data", "-F", "--form", "-T", "--upload-file", "--json", "--post-data",
                                 "--post-file", "--method=POST", "--method=PUT", "--method=DELETE")):
                    write = True
                if name in ("http", "https", "xh") and a.upper() in ("POST", "PUT", "DELETE", "PATCH"):
                    write = True
            if write:
                hits.append(Hit("T6.http_write", "T6", "ask", "hard", f"sends data over HTTP: {' '.join(argv)[:120]}"))
    return hits
```

`src/dryrun/judge/cascade.py`:
```python
"""The decision cascade (ARCHITECTURE §4)."""
from __future__ import annotations

from dryrun.judge.model import EffectJudge, NullJudge
from dryrun.judge.rules import Hit
from dryrun.types import RERUN_FLAGS, Decision, EffectRecord

MAX_REASON = 200


def _reason(hits: list[Hit]) -> str:
    top = hits[0]
    text = f"{top.rule_id}: {top.evidence}"
    if len(hits) > 1:
        text += f" (+{len(hits) - 1} more)"
    return text if len(text) <= MAX_REASON else text[:MAX_REASON - 1] + "…"


def decide(triage_class: str, *, triage_reason: str = "", text: list[Hit] = (), rec: EffectRecord | None = None,
           hits: list[Hit] = (), judge: EffectJudge = NullJudge(), dev_server_allowed: bool = False) -> Decision:
    text, hits = list(text), list(hits)
    if triage_class == "read_only":
        return Decision("allow", "passthrough", "read-only command")
    if triage_class == "apply":
        return Decision("deny", "passthrough", "only Dry Run may issue `dryrun apply`", ["F11.apply"])
    if triage_class == "non_shadowable":
        if text:
            return Decision("ask", "passthrough", _reason(text), [h.rule_id for h in text])
        return Decision("ask", "passthrough", f"T0: cannot be shadowed ({triage_reason})", ["T0.unshadowable"])
    if triage_class == "long_running":
        if dev_server_allowed:
            return Decision("allow", "rerun", "allow-listed long-running command (runs for real, logged)")
        return Decision("ask", "passthrough", f"long-running command cannot be shadowed ({triage_reason})",
                        ["T0.long_running"])
    assert rec is not None, "shadow decisions need an EffectRecord"
    hard_deny = [h for h in hits if h.tier == "hard" and h.verdict == "deny"]
    if hard_deny:
        return Decision("deny", "passthrough", _reason(hard_deny), [h.rule_id for h in hard_deny])
    if RERUN_FLAGS & set(rec.flags):
        asks = [h for h in hits if h.verdict == "ask"] or [
            Hit("F.flags", "flags", "ask", "hard", "shadow result incomplete: " + ", ".join(rec.flags))]
        return Decision("ask", "rerun", _reason(asks), [h.rule_id for h in asks])
    hard_ask = [h for h in hits if h.tier == "hard" and h.verdict == "ask"]
    if hard_ask:
        return Decision("ask", "commit", _reason(hard_ask), [h.rule_id for h in hard_ask + [h for h in hits if h not in hard_ask]])
    soft = [h for h in hits if h.tier == "soft"]
    if soft:
        judge.score(rec)  # sub-project 3: calibrated thresholds may lower soft asks; v1 keeps them
        return Decision("ask", "commit", _reason(soft), [h.rule_id for h in soft])
    return Decision("allow", "commit", "observed effect is within policy")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... bash scripts/dev/test.sh tests/unit/test_rules.py tests/unit/test_cascade.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/judge tests/unit/test_rules.py tests/unit/test_cascade.py
git commit -m "feat: harm-policy rules H1-H10/T1-T6 and the decision cascade"
```
