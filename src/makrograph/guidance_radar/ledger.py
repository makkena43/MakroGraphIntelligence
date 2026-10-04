"""Chronological promise-ledger calculations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from .models import CompanyPacket, GuidanceClaim


@dataclass(frozen=True)
class LedgerHealth:
    consistency_score: float
    repeated_metrics: int
    cuts: int
    misses: int
    narrative_drift: bool
    explanations: tuple[str, ...]


def assess_ledger(packet: CompanyPacket) -> LedgerHealth:
    """Assess promise stability without rewarding repeated promotional copies."""
    by_metric: dict[str, list[GuidanceClaim]] = defaultdict(list)
    for claim in packet.claims:
        if claim.certainty == "guidance" and claim.confidence >= 0.60:
            by_metric[claim.metric].append(claim)

    score = 4.0 if by_metric else 0.0
    repeated = 0
    cuts = 0
    notes: list[str] = []
    for metric, claims in by_metric.items():
        claims.sort(key=lambda c: c.source_date)
        independent_dates = {c.source_date for c in claims}
        if len(independent_dates) >= 2:
            repeated += 1
            score += 2.0
        values = [_midpoint(c) for c in claims if _midpoint(c) is not None]
        if len(values) >= 2:
            for previous, current in zip(values, values[1:]):
                if previous and current < previous * 0.85:
                    cuts += 1
                    score -= 3.0
                    notes.append(f"{metric} guidance was cut materially")

    risk_kinds = {risk.kind for risk in packet.risks}
    misses = sum(risk.kind == "target_miss" for risk in packet.risks)
    score -= min(5.0, misses * 3.0)
    if "guidance_cut" in risk_kinds:
        cuts += 1
        score -= 3.0

    drift = "narrative_drift" in risk_kinds or _tag_drift(packet)
    if drift:
        score -= 2.0
        notes.append("the disclosed earnings driver changed materially")

    if repeated:
        notes.append(f"formal guidance repeated across {repeated} metric(s)")
    elif by_metric:
        notes.append("only one dated formal-guidance observation is available")
    else:
        notes.append("no formal numeric guidance was recovered")

    return LedgerHealth(
        consistency_score=max(0.0, min(10.0, score)),
        repeated_metrics=repeated,
        cuts=cuts,
        misses=misses,
        narrative_drift=drift,
        explanations=tuple(notes),
    )


def _midpoint(claim: GuidanceClaim):
    if claim.target_low is None and claim.target_high is None:
        return None
    if claim.target_low is None:
        return claim.target_high
    if claim.target_high is None:
        return claim.target_low
    return median((claim.target_low, claim.target_high))


def _tag_drift(packet: CompanyPacket) -> bool:
    observations = [tags for _, tags in sorted(packet.narrative_tags_by_date.items()) if tags]
    if len(observations) < 3:
        return False
    first = set.union(*observations[:2])
    last = observations[-1]
    union = first | last
    if not union:
        return False
    return len(first & last) / len(union) < 0.15
