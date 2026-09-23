### Task 2: Types and JSON schemas

**Files:**
- Create: `src/dryrun/types.py`, `schemas/effect.schema.json`, `schemas/changeset.schema.json`, `schemas/rpc.schema.json`, `tests/unit/test_types.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FsEntry(op, path, kind, preexisting, in_ledger=False, git=None, build_output=False, discards_work=False, bytes_before=None, bytes_after=None, mode_before=None, mode_after=None)`
    - `op` ∈ `create|modify|delete|mode`
    - `kind` ∈ `file|dir|symlink|other`
    - `git` ∈ `tracked_clean|tracked_dirty|untracked|ignored|None`
  - `RefChange(ref, before, after, change)`, where `change` ∈ `created|deleted|fast_forward|non_fast_forward|unknown`
  - `EffectRecord` (fields below). `.to_json() -> dict` matches `dryrun.effect/1`; `EffectRecord.from_json(d)`; `.summary() -> dict`
  - `ChangeOp(seq, op, area, target, source_upper=None, kind="file", mode=None, base_fp=None, subtree_digest=None, link_target=None, sha256=None, size=None, mtime_ns=None)`
    - `op` ∈ `unlink|rmtree|mkdir|rename_in|symlink|chmod`
    - `area` ∈ `workspace|tmp`
  - `ChangeSet(run_id, roots: dict[str,str], base_digest, ops: list[ChangeOp], refused: list[dict])`, with `.committable`, `.to_json()`, `.from_json()`
  - `Decision(decision, mode, reason, rule_ids=[], run_id=None, token=None)`, with `.to_json()`
    - `decision` ∈ `allow|ask|deny`
    - `mode` ∈ `commit|rerun|passthrough`
  - constants `FLAGS`, `RERUN_FLAGS`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_types.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from dryrun.types import ChangeOp, ChangeSet, Decision, EffectRecord, FsEntry, RefChange

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def sample_record() -> EffectRecord:
    return EffectRecord(
        run_id="r1", session_id="s1", command="bash clean.sh", cwd="/w/app", workspace_root="/w/app",
        request_text="clean the build directory", request_source="UserPromptSubmit",
        triage_class="shadow", triage_reason="script execution",
        exit_code=0, wall_ms=412, timed_out=False, stdout_tail="ok\n", stderr_tail="",
        workspace=[
            FsEntry(op="delete", path="src/main.py", kind="file", preexisting=True, git="tracked_clean", bytes_before=2048),
            FsEntry(op="create", path="build/out.o", kind="file", preexisting=False, build_output=True, bytes_after=10),
        ],
        tmp=[], home_cache_files=2, home_cache_bytes=100,
        git_is_repo=True,
        refs_changed=[RefChange(ref="refs/heads/main", before="a" * 40, after="b" * 40, change="non_fast_forward")],
        index_changed=False, internals_touched=[], objects_deleted=0,
        net=[{"kind": "dns", "target": "127.0.0.53:53"}], proc_count=3, execs=["bash", "rm"],
        decoy_hits=[], flags=["incomplete_network"],
    )


def test_effect_record_matches_schema_and_round_trips():
    rec = sample_record()
    doc = rec.to_json()
    jsonschema.validate(doc, load_schema("effect.schema.json"))
    assert doc["schema"] == "dryrun.effect/1"
    assert EffectRecord.from_json(json.loads(json.dumps(doc))) == rec


def test_summary_counts():
    s = sample_record().summary()
    assert s == {"created": 1, "modified": 0, "deleted_preexisting": 1, "deleted_new": 0,
                 "bytes_written": 10, "mode_changes": 0}


def test_deleting_ledger_path_counts_as_new():
    rec = sample_record()
    rec.workspace = [FsEntry(op="delete", path="gen.txt", kind="file", preexisting=True, in_ledger=True)]
    assert rec.summary()["deleted_new"] == 1


def test_changeset_schema_round_trip_and_committable():
    cs = ChangeSet(
        run_id="r1", roots={"workspace": "/w/app", "tmp": "/tmp"}, base_digest="d" * 64,
        ops=[ChangeOp(seq=0, op="rename_in", area="workspace", target="a.txt", source_upper="/s/up/a.txt",
                      kind="file", mode=0o644, base_fp=[1, 2, 3, 4, 33188], sha256="e" * 64, size=2, mtime_ns=5)],
        refused=[],
    )
    doc = cs.to_json()
    jsonschema.validate(doc, load_schema("changeset.schema.json"))
    assert ChangeSet.from_json(json.loads(json.dumps(doc))) == cs
    assert cs.committable
    cs.refused.append({"path": "x", "reason": "hardlink_in_shadow"})
    assert not cs.committable


def test_decision_matches_rpc_schema():
    d = Decision(decision="ask", mode="commit", reason="would delete 1 file", rule_ids=["H1.unrecoverable"],
                 run_id="r1", token="t")
    schema = load_schema("rpc.schema.json")
    jsonschema.validate(d.to_json(), {"$ref": "#/$defs/decision_response", "$defs": schema["$defs"]})


@pytest.mark.parametrize("req", [
    {"op": "prompt", "session_id": "s", "text": "hi"},
    {"op": "pretool", "session_id": "s", "cwd": "/w", "command": "ls", "description": "",
     "transcript_path": "", "env": {"PATH": "/bin"}, "deadline_ms": 5000},
    {"op": "status"},
])
def test_rpc_requests_match_schema(req):
    schema = load_schema("rpc.schema.json")
    jsonschema.validate(req, {"$ref": "#/$defs/request", "$defs": schema["$defs"]})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/unit/test_types.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'dryrun.types'`

- [ ] **Step 3: Implement types and schemas**

`src/dryrun/types.py`:
```python
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
```

`schemas/effect.schema.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "dryrun.effect/1",
  "title": "Dry Run EffectRecord",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema", "run_id", "session_id", "command", "cwd", "workspace_root", "request", "triage",
               "exec", "fs", "git", "summary", "net", "procs", "decoy_hits", "flags"],
  "properties": {
    "schema": {"const": "dryrun.effect/1"},
    "run_id": {"type": "string"},
    "session_id": {"type": "string"},
    "command": {"type": "string"},
    "cwd": {"type": "string"},
    "workspace_root": {"type": "string"},
    "request": {
      "type": "object", "additionalProperties": false, "required": ["text", "source"],
      "properties": {"text": {"type": ["string", "null"]}, "source": {"type": ["string", "null"]}}
    },
    "triage": {
      "type": "object", "additionalProperties": false, "required": ["class", "reason"],
      "properties": {
        "class": {"enum": ["read_only", "apply", "non_shadowable", "long_running", "shadow"]},
        "reason": {"type": "string"}
      }
    },
    "exec": {
      "type": "object", "additionalProperties": false,
      "required": ["exit_code", "wall_ms", "timed_out", "stdout_tail", "stderr_tail"],
      "properties": {
        "exit_code": {"type": ["integer", "null"]},
        "wall_ms": {"type": "integer", "minimum": 0},
        "timed_out": {"type": "boolean"},
        "stdout_tail": {"type": "string"},
        "stderr_tail": {"type": "string"}
      }
    },
    "fs": {
      "type": "object", "additionalProperties": false, "required": ["workspace", "tmp", "home_cache"],
      "properties": {
        "workspace": {"type": "array", "items": {"$ref": "#/$defs/entry"}},
        "tmp": {"type": "array", "items": {"$ref": "#/$defs/entry"}},
        "home_cache": {
          "type": "object", "additionalProperties": false, "required": ["files", "bytes"],
          "properties": {"files": {"type": "integer"}, "bytes": {"type": "integer"}}
        }
      }
    },
    "git": {
      "type": "object", "additionalProperties": false,
      "required": ["is_repo", "refs_changed", "index_changed", "internals_touched", "objects_deleted"],
      "properties": {
        "is_repo": {"type": "boolean"},
        "refs_changed": {"type": "array", "items": {
          "type": "object", "additionalProperties": false, "required": ["ref", "before", "after", "change"],
          "properties": {
            "ref": {"type": "string"},
            "before": {"type": ["string", "null"]},
            "after": {"type": ["string", "null"]},
            "change": {"enum": ["created", "deleted", "fast_forward", "non_fast_forward", "unknown"]}
          }
        }},
        "index_changed": {"type": "boolean"},
        "internals_touched": {"type": "array", "items": {"type": "string"}},
        "objects_deleted": {"type": "integer"}
      }
    },
    "summary": {
      "type": "object", "additionalProperties": false,
      "required": ["created", "modified", "deleted_preexisting", "deleted_new", "bytes_written", "mode_changes"],
      "properties": {
        "created": {"type": "integer"}, "modified": {"type": "integer"},
        "deleted_preexisting": {"type": "integer"}, "deleted_new": {"type": "integer"},
        "bytes_written": {"type": "integer"}, "mode_changes": {"type": "integer"}
      }
    },
    "net": {"type": "array", "items": {
      "type": "object", "additionalProperties": false, "required": ["kind", "target"],
      "properties": {"kind": {"enum": ["connect", "dns", "send"]}, "target": {"type": "string"}}
    }},
    "procs": {
      "type": "object", "additionalProperties": false, "required": ["count", "exec"],
      "properties": {"count": {"type": "integer"}, "exec": {"type": "array", "items": {"type": "string"}}}
    },
    "decoy_hits": {"type": "array", "items": {
      "type": "object", "additionalProperties": false, "required": ["path", "where"],
      "properties": {"path": {"type": "string"}, "where": {"type": "string"}}
    }},
    "flags": {"type": "array", "items": {"enum": ["timeout", "resource_limit", "incomplete_network",
      "unsupported_entry", "lower_changed", "sandbox_error", "ro_write_blocked", "tmp_partial"]}}
  },
  "$defs": {
    "entry": {
      "type": "object", "additionalProperties": false,
      "required": ["op", "path", "kind", "preexisting", "in_ledger", "git", "build_output", "discards_work",
                   "bytes_before", "bytes_after", "mode_before", "mode_after"],
      "properties": {
        "op": {"enum": ["create", "modify", "delete", "mode"]},
        "path": {"type": "string"},
        "kind": {"enum": ["file", "dir", "symlink", "other"]},
        "preexisting": {"type": "boolean"},
        "in_ledger": {"type": "boolean"},
        "git": {"enum": ["tracked_clean", "tracked_dirty", "untracked", "ignored", null]},
        "build_output": {"type": "boolean"},
        "discards_work": {"type": "boolean"},
        "bytes_before": {"type": ["integer", "null"]},
        "bytes_after": {"type": ["integer", "null"]},
        "mode_before": {"type": ["integer", "null"]},
        "mode_after": {"type": ["integer", "null"]}
      }
    }
  }
}
```

`schemas/changeset.schema.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "dryrun.changeset/1",
  "title": "Dry Run ChangeSet",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema", "run_id", "roots", "base_digest", "ops", "refused", "committable"],
  "properties": {
    "schema": {"const": "dryrun.changeset/1"},
    "run_id": {"type": "string"},
    "roots": {"type": "object", "additionalProperties": {"type": "string"}},
    "base_digest": {"type": "string"},
    "ops": {"type": "array", "items": {
      "type": "object", "additionalProperties": false,
      "required": ["seq", "op", "area", "target", "source_upper", "kind", "mode", "base_fp", "subtree_digest",
                   "link_target", "sha256", "size", "mtime_ns"],
      "properties": {
        "seq": {"type": "integer"},
        "op": {"enum": ["unlink", "rmtree", "mkdir", "rename_in", "symlink", "chmod"]},
        "area": {"enum": ["workspace", "tmp"]},
        "target": {"type": "string", "minLength": 1},
        "source_upper": {"type": ["string", "null"]},
        "kind": {"enum": ["file", "dir", "symlink", "other"]},
        "mode": {"type": ["integer", "null"]},
        "base_fp": {"type": ["array", "null"], "items": {"type": "integer"}, "minItems": 5, "maxItems": 5},
        "subtree_digest": {"type": ["string", "null"]},
        "link_target": {"type": ["string", "null"]},
        "sha256": {"type": ["string", "null"]},
        "size": {"type": ["integer", "null"]},
        "mtime_ns": {"type": ["integer", "null"]}
      }
    }},
    "refused": {"type": "array", "items": {
      "type": "object", "required": ["path", "reason"],
      "properties": {"path": {"type": "string"}, "reason": {"type": "string"}}
    }},
    "committable": {"type": "boolean"}
  }
}
```

`schemas/rpc.schema.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "dryrun.rpc/1",
  "title": "Dry Run hook <-> daemon protocol (one NDJSON request and one response per connection)",
  "$defs": {
    "request": {"oneOf": [
      {"$ref": "#/$defs/prompt_request"}, {"$ref": "#/$defs/pretool_request"}, {"$ref": "#/$defs/status_request"}
    ]},
    "prompt_request": {
      "type": "object", "additionalProperties": false, "required": ["op", "session_id", "text"],
      "properties": {"op": {"const": "prompt"}, "session_id": {"type": "string"}, "text": {"type": "string"}}
    },
    "pretool_request": {
      "type": "object", "additionalProperties": false,
      "required": ["op", "session_id", "cwd", "command", "description", "transcript_path", "env", "deadline_ms"],
      "properties": {
        "op": {"const": "pretool"},
        "session_id": {"type": "string"},
        "cwd": {"type": "string"},
        "command": {"type": "string"},
        "description": {"type": "string"},
        "transcript_path": {"type": "string"},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "deadline_ms": {"type": "integer", "minimum": 0}
      }
    },
    "status_request": {
      "type": "object", "additionalProperties": false, "required": ["op"],
      "properties": {"op": {"const": "status"}}
    },
    "decision_response": {
      "type": "object", "additionalProperties": false,
      "required": ["decision", "mode", "reason", "rule_ids", "run_id", "token"],
      "properties": {
        "decision": {"enum": ["allow", "ask", "deny"]},
        "mode": {"enum": ["commit", "rerun", "passthrough"]},
        "reason": {"type": "string", "maxLength": 1000},
        "rule_ids": {"type": "array", "items": {"type": "string"}},
        "run_id": {"type": ["string", "null"]},
        "token": {"type": ["string", "null"]}
      }
    },
    "ok_response": {
      "type": "object", "required": ["ok"],
      "properties": {"ok": {"type": "boolean"}, "error": {"type": "string"}}
    }
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/unit/test_types.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/types.py schemas tests/unit/test_types.py
git commit -m "feat: effect record, changeset and rpc contracts with JSON schemas"
```
