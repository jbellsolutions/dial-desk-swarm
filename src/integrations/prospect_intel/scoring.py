"""ICP scoring — 100-point rubric mirrored from autonomous-sdr-agent.

Each signal contributes 0..weight points; total caps at 100. Tier is derived
from the total: A/Tier1 (>=80), B/Tier2 (50..79), C/Tier3 (<50).

Channel allocation per tier (enforced by the SDR runtime, not here):
  Tier 1: call + email + SMS same day, sequenced ~30min apart
  Tier 2: email day 0, call day 1, SMS day 3
  Tier 3: email queue days 2-3, no calls, SMS only on engagement
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Tier = Literal["A", "B", "C"]

# Default weights — overridable per campaign via campaigns/{name}.yaml
DEFAULT_WEIGHTS = {
    "industry": 20,    # exact ICP industry vs adjacent vs out-of-ICP
    "revenue": 15,     # in revenue band vs above/below
    "headcount": 15,   # in headcount band
    "tech_stack": 15,  # uses signal tech (e.g. specific stack)
    "title": 20,       # decision-maker title match
    "funding": 15,     # recent funding round / stage match
}


@dataclass
class ProspectSignals:
    industry_match: float = 0.0      # 0..1
    revenue_match: float = 0.0       # 0..1
    headcount_match: float = 0.0     # 0..1
    tech_stack_match: float = 0.0    # 0..1
    title_match: float = 0.0         # 0..1
    funding_match: float = 0.0       # 0..1
    raw: dict = field(default_factory=dict)


@dataclass
class ScoreResult:
    score: int
    tier: Tier
    breakdown: dict[str, int]


def score(signals: ProspectSignals, weights: dict[str, int] | None = None) -> ScoreResult:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    breakdown = {
        "industry": round(signals.industry_match * w["industry"]),
        "revenue": round(signals.revenue_match * w["revenue"]),
        "headcount": round(signals.headcount_match * w["headcount"]),
        "tech_stack": round(signals.tech_stack_match * w["tech_stack"]),
        "title": round(signals.title_match * w["title"]),
        "funding": round(signals.funding_match * w["funding"]),
    }
    total = min(sum(breakdown.values()), 100)
    return ScoreResult(score=total, tier=_tier(total), breakdown=breakdown)


def _tier(total: int) -> Tier:
    if total >= 80:
        return "A"
    if total >= 50:
        return "B"
    return "C"
