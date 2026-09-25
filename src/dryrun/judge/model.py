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
