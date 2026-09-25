from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from dryrun.types import FLAGS, ChangeOp, ChangeSet, Decision, EffectRecord, FsEntry, RefChange

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


def test_schema_flags_match_the_flags_the_core_sets():
    """A sandbox error never produces a record (the pipeline answers S.sandbox_error), so it is not a flag."""
    enum = load_schema("effect.schema.json")["properties"]["flags"]["items"]["enum"]
    assert set(enum) == set(FLAGS)
    assert "sandbox_error" not in FLAGS


def test_effect_schema_accepts_git_read_triage():
    rec = sample_record()
    rec.triage_class = "git_read"
    jsonschema.validate(rec.to_json(), load_schema("effect.schema.json"))
