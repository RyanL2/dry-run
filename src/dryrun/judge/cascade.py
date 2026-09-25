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
    if rec is None:
        raise ValueError("shadow decisions need an EffectRecord")
    hard_deny = [h for h in hits if h.tier == "hard" and h.verdict == "deny"]
    if hard_deny:
        return Decision("deny", "passthrough", _reason(hard_deny), [h.rule_id for h in hard_deny])
    if RERUN_FLAGS & set(rec.flags):
        asks = [h for h in hits if h.verdict == "ask"] or [
            Hit("F.flags", "flags", "ask", "hard", "shadow result incomplete: " + ", ".join(rec.flags))]
        return Decision("ask", "rerun", _reason(asks), [h.rule_id for h in asks])
    hard_ask = [h for h in hits if h.tier == "hard" and h.verdict == "ask"]
    if hard_ask:
        rest = [h for h in hits if h not in hard_ask]
        return Decision("ask", "commit", _reason(hard_ask), [h.rule_id for h in hard_ask + rest])
    soft = [h for h in hits if h.tier == "soft"]
    if soft:
        judge.score(rec)  # sub-project 3: calibrated thresholds may lower soft asks; v1 keeps them
        return Decision("ask", "commit", _reason(soft), [h.rule_id for h in soft])
    return Decision("allow", "commit", "observed effect is within policy")
