"""Data contracts shared by the core, the benchmark and the judge model (JSON Schemas in /schemas)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

EFFECT_SCHEMA = "dryrun.effect/1"
CHANGESET_SCHEMA = "dryrun.changeset/1"

FLAGS = (
    "timeout", "resource_limit", "incomplete_network", "unsupported_entry",
    "lower_changed", "sandbox_error", "ro_write_blocked", "tmp_partial",
)
# Flags that mean the shadow result cannot be committed faithfully: ask, and re-run for real if approved.
RERUN_FLAGS = frozenset(FLAGS) - {"tmp_partial"}


@dataclass
class FsEntry:
    op: str
    path: str
    kind: str
    preexisting: bool
    in_ledger: bool = False
    git: str | None = None
    build_output: bool = False
    discards_work: bool = False
    bytes_before: int | None = None
    bytes_after: int | None = None
    mode_before: int | None = None
    mode_after: int | None = None


@dataclass
class RefChange:
    ref: str
    before: str | None
    after: str | None
    change: str


@dataclass
class EffectRecord:
    run_id: str
    session_id: str
    command: str
    cwd: str
    workspace_root: str
    request_text: str | None
    request_source: str | None
    triage_class: str
    triage_reason: str
    exit_code: int | None
    wall_ms: int
    timed_out: bool
    stdout_tail: str
    stderr_tail: str
    workspace: list[FsEntry] = field(default_factory=list)
    tmp: list[FsEntry] = field(default_factory=list)
    home_cache_files: int = 0
    home_cache_bytes: int = 0
    git_is_repo: bool = False
    refs_changed: list[RefChange] = field(default_factory=list)
    index_changed: bool = False
    internals_touched: list[str] = field(default_factory=list)
    objects_deleted: int = 0
    net: list[dict] = field(default_factory=list)
    proc_count: int = 0
    execs: list[str] = field(default_factory=list)
    decoy_hits: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        entries = self.workspace + self.tmp
        return {
            "created": sum(e.op == "create" for e in entries),
            "modified": sum(e.op == "modify" for e in entries),
            "deleted_preexisting": sum(e.op == "delete" and e.preexisting and not e.in_ledger for e in entries),
            "deleted_new": sum(e.op == "delete" and (not e.preexisting or e.in_ledger) for e in entries),
            "bytes_written": sum(e.bytes_after or 0 for e in entries if e.op in ("create", "modify")),
            "mode_changes": sum(e.op == "mode" for e in entries),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": EFFECT_SCHEMA,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "command": self.command,
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "request": {"text": self.request_text, "source": self.request_source},
            "triage": {"class": self.triage_class, "reason": self.triage_reason},
            "exec": {"exit_code": self.exit_code, "wall_ms": self.wall_ms, "timed_out": self.timed_out,
                     "stdout_tail": self.stdout_tail, "stderr_tail": self.stderr_tail},
            "fs": {"workspace": [asdict(e) for e in self.workspace], "tmp": [asdict(e) for e in self.tmp],
                   "home_cache": {"files": self.home_cache_files, "bytes": self.home_cache_bytes}},
            "git": {"is_repo": self.git_is_repo, "refs_changed": [asdict(r) for r in self.refs_changed],
                    "index_changed": self.index_changed, "internals_touched": list(self.internals_touched),
                    "objects_deleted": self.objects_deleted},
            "summary": self.summary(),
            "net": list(self.net),
            "procs": {"count": self.proc_count, "exec": list(self.execs)},
            "decoy_hits": list(self.decoy_hits),
            "flags": list(self.flags),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "EffectRecord":
        return cls(
            run_id=d["run_id"], session_id=d["session_id"], command=d["command"], cwd=d["cwd"],
            workspace_root=d["workspace_root"],
            request_text=d["request"]["text"], request_source=d["request"]["source"],
            triage_class=d["triage"]["class"], triage_reason=d["triage"]["reason"],
            exit_code=d["exec"]["exit_code"], wall_ms=d["exec"]["wall_ms"], timed_out=d["exec"]["timed_out"],
            stdout_tail=d["exec"]["stdout_tail"], stderr_tail=d["exec"]["stderr_tail"],
            workspace=[FsEntry(**e) for e in d["fs"]["workspace"]],
            tmp=[FsEntry(**e) for e in d["fs"]["tmp"]],
            home_cache_files=d["fs"]["home_cache"]["files"], home_cache_bytes=d["fs"]["home_cache"]["bytes"],
            git_is_repo=d["git"]["is_repo"],
            refs_changed=[RefChange(**r) for r in d["git"]["refs_changed"]],
            index_changed=d["git"]["index_changed"], internals_touched=list(d["git"]["internals_touched"]),
            objects_deleted=d["git"]["objects_deleted"],
            net=list(d["net"]), proc_count=d["procs"]["count"], execs=list(d["procs"]["exec"]),
            decoy_hits=list(d["decoy_hits"]), flags=list(d["flags"]),
        )


@dataclass
class ChangeOp:
    seq: int
    op: str
    area: str
    target: str
    source_upper: str | None = None
    kind: str = "file"
    mode: int | None = None
    base_fp: list[int] | None = None
    subtree_digest: str | None = None
    link_target: str | None = None
    sha256: str | None = None
    size: int | None = None
    mtime_ns: int | None = None


@dataclass
class ChangeSet:
    run_id: str
    roots: dict[str, str]
    base_digest: str
    ops: list[ChangeOp] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)

    @property
    def committable(self) -> bool:
        return not self.refused

    def to_json(self) -> dict[str, Any]:
        return {"schema": CHANGESET_SCHEMA, "run_id": self.run_id, "roots": dict(self.roots),
                "base_digest": self.base_digest, "ops": [asdict(o) for o in self.ops],
                "refused": list(self.refused), "committable": self.committable}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ChangeSet":
        names = {f.name for f in fields(ChangeOp)}
        return cls(run_id=d["run_id"], roots=dict(d["roots"]), base_digest=d["base_digest"],
                   ops=[ChangeOp(**{k: v for k, v in o.items() if k in names}) for o in d["ops"]],
                   refused=list(d["refused"]))


@dataclass
class Decision:
    decision: str
    mode: str
    reason: str
    rule_ids: list[str] = field(default_factory=list)
    run_id: str | None = None
    token: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
