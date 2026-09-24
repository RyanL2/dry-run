"""Harm-policy clauses (docs/harm-policy.md) as predicates over the EffectRecord."""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Callable

from dryrun.config import Policy
from dryrun.types import EffectRecord, FsEntry

PERSISTENCE = (".git/hooks", ".git/hooks/*", ".git/config", ".git/config.worktree", ".git/modules/*",
               "*/.git/hooks/*", "*/.git/config", "*/.git/config.worktree", "*/.git/modules/*",
               ".git/worktrees/*", ".gitmodules", ".git/info/attributes", ".claude", ".claude/*",
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
        hits.append(Hit("H6.resources", "H6", "ask", "hard", "hit a time, memory, process, disk or trace-size limit"))
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
