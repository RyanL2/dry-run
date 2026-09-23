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


def H(rid: str, verdict: str, tier: str) -> Hit:
    return Hit(rid, rid.split(".")[0], verdict, tier, f"evidence for {rid}")


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
    assert decide("shadow", rec=rec(), hits=[H("H1.unrecoverable", "ask", "hard")]).mode == "commit"
    d = decide("shadow", rec=rec(), hits=[H("H1.recoverable", "ask", "soft")])
    assert (d.decision, d.mode) == ("ask", "commit")
    d = decide("shadow", rec=rec(), hits=[])
    assert (d.decision, d.mode, d.rule_ids) == ("allow", "commit", [])


def test_reason_is_bounded_and_prefixed_by_top_rule():
    d = decide("shadow", rec=rec(), hits=[Hit("H1.unrecoverable", "H1", "ask", "hard", "x" * 500)])
    assert len(d.reason) <= 200 and d.reason.startswith("H1")
