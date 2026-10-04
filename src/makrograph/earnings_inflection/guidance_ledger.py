"""Guidance ledger: original commitments, revisions and realized outcomes.

The ORIGINAL statement is always retained and outcomes are judged against it;
later revisions are recorded (raised / lowered / reiterated / withdrawn) but
never overwrite the original.  Silent management is not penalised: absence of
guidance simply yields an empty ledger.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Optional

from .contracts import (
    Evidence, GuidanceOutcome, GuidanceRecord, GuidanceRevision, Metric, Modality, RevisionDirection,
)
from .extraction import fy_label_end
from .financial_series import FinancialSeries

GUIDANCE_METRICS = (Metric.REVENUE_GUIDANCE, Metric.REVENUE_GROWTH_GUIDANCE, Metric.MARGIN_GUIDANCE)
RESULTS_LAG_DAYS = 75   # SEBI LODR: quarterly 45 days, annual 60 days (+ buffer)


def _direction(prev: GuidanceRevision, ev: Evidence) -> RevisionDirection:
    if ev.modality == Modality.NEGATED:
        return RevisionDirection.WITHDRAWN
    if ev.quantity is None or prev.quantity is None:
        return RevisionDirection.REITERATED
    p, q = prev.quantity, ev.quantity
    p_lo, p_hi = (p.low if p.low is not None else p.value), (p.high if p.high is not None else p.value)
    q_lo, q_hi = (q.low if q.low is not None else q.value), (q.high if q.high is not None else q.value)
    if q_lo > p_hi:
        return RevisionDirection.RAISED
    if q_hi < p_lo:
        return RevisionDirection.LOWERED
    if abs(q.value - p.value) / max(abs(p.value), 1e-9) <= 0.02 or (q_lo <= p.value <= q_hi):
        return RevisionDirection.REITERATED
    return RevisionDirection.RAISED if q.value > p.value else RevisionDirection.LOWERED


def _period_type(label: str) -> str:
    return "FY" if label.startswith("FY") else "H" if label.startswith("H") else "Q"


def _realized(series: FinancialSeries, metric: Metric, label: str) -> Optional[float]:
    end, ptype = fy_label_end(label), _period_type(label)
    if end is None or series is None:
        return None
    if metric == Metric.REVENUE_GROWTH_GUIDANCE:
        return series.yoy(Metric.REVENUE, end, ptype)
    if metric == Metric.REVENUE_GUIDANCE:
        p = series.get(Metric.REVENUE, end, ptype)
        return p.value if p else None
    if metric == Metric.MARGIN_GUIDANCE:
        return series.margin(end, ptype)
    return None


def build_ledger(evidence: list[Evidence], series: Optional[FinancialSeries], as_of: datetime) -> list[GuidanceRecord]:
    items = sorted((e for e in evidence if e.usable and e.metric in GUIDANCE_METRICS
                    and e.modality in (Modality.FORWARD, Modality.CONDITIONAL, Modality.NEGATED)),
                   key=lambda e: (e.available_at, e.doc_id))
    ledger: dict[tuple[Metric, str], GuidanceRecord] = {}
    for e in items:
        key = (e.metric, e.target_period_label or "unspecified")
        rev = GuidanceRevision(e.available_at, e.doc_id, e.evidence_id, e.quantity, RevisionDirection.ORIGINAL, e.quote)
        rec = ledger.get(key)
        if rec is None:
            if e.modality == Modality.NEGATED or e.quantity is None:
                continue   # a negated/unquantified first mention is not a commitment
            gid = hashlib.sha1(f"{e.ticker}|{key}".encode()).hexdigest()[:12]
            ledger[key] = GuidanceRecord(f"g-{gid}", e.ticker, e.metric, key[1], rev)
            continue
        last = rec.revisions[-1] if rec.revisions else rec.original
        rev.direction = _direction(last, e)
        if rev.evidence_id not in {r.evidence_id for r in rec.revisions}:
            rec.revisions.append(rev)

    for rec in ledger.values():
        _judge(rec, series, as_of)
    return list(ledger.values())


def _judge(rec: GuidanceRecord, series: Optional[FinancialSeries], as_of: datetime) -> None:
    if rec.target_period_label == "unspecified":
        rec.outcome, rec.outcome_note = GuidanceOutcome.PENDING, "no target period stated; cannot be judged"
        return
    end = fy_label_end(rec.target_period_label)
    if end is None:
        rec.outcome_note = "unparseable target period"
        return
    if end >= as_of.date():
        rec.outcome, rec.outcome_note = GuidanceOutcome.PENDING, f"target period ends {end}"
        return
    realized = _realized(series, rec.metric, rec.target_period_label)
    if realized is None:
        if (as_of.date() - end).days <= RESULTS_LAG_DAYS:
            rec.outcome, rec.outcome_note = GuidanceOutcome.PENDING, "results for target period not yet due/public"
        else:
            rec.outcome, rec.outcome_note = GuidanceOutcome.UNVERIFIABLE, "no comparable realized data in sources"
        return
    rec.realized_value = round(realized, 2)
    q = rec.original.quantity
    target_lo = q.low if q.low is not None else q.value
    if realized >= target_lo:
        rec.outcome = GuidanceOutcome.MET
    elif target_lo > 0 and realized >= 0.8 * target_lo:
        rec.outcome = GuidanceOutcome.PARTIALLY_MET
    else:
        rec.outcome = GuidanceOutcome.MISSED
    last = rec.revisions[-1] if rec.revisions else None
    rec.outcome_note = (f"realized {realized:.2f} vs original {q.raw or q.value}"
                        + (f"; last revision {last.direction.value}" if last else ""))
