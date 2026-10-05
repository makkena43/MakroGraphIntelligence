"""Guidance ledger: original commitments, revisions and realized outcomes.

The ORIGINAL statement is always retained; later revisions are recorded
(raised / lowered / reiterated / withdrawn) but never overwrite it.  Each
target is judged twice: against the original and against the latest stated
guidance.

Revision policy (evidence-only, no reputation scoring):
* Conservative guidance and a revision with a stated reason are NOT treated
  as contradictions.  The reason is quoted for a human to judge credibility.
* Contradiction evidence is: missing even the latest stated guidance, or
  repeated downward revisions with no stated reason (counted in assessments).
* A target that is not mentioned again in later management commentary before
  its deadline is flagged as possibly "disappearing".
* Silent management is not penalised: no guidance simply means an empty ledger.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from typing import Iterable, Optional

from .contracts import (
    IST, Evidence, GuidanceOutcome, GuidanceRecord, GuidanceRevision, Metric, Modality, RevisionDirection,
    SourceDocument,
)
from .extraction import fy_label_end
from .financial_series import FinancialSeries

GUIDANCE_METRICS = (Metric.REVENUE_GUIDANCE, Metric.REVENUE_GROWTH_GUIDANCE, Metric.MARGIN_GUIDANCE)
RESULTS_LAG_DAYS = 75   # SEBI LODR: quarterly 45 days, annual 60 days (+ buffer)
DISAPPEARING_AFTER_DOCS = 2   # later commentary documents without a mention

_REASON = re.compile(
    r"\b(?:due to|because|on account of|owing to|as a result of|driven by|impacted by|affected by|"
    r"on the back of|attributable to|reflecting|in view of|given the|following the|"
    r"delay(?:s|ed)? in|deferr\w+|postpone\w+|push(?:ed)? out|slowdown|weak(?:er)? demand|"
    r"raw material|input cost|commodity|monsoon|elections?|supply chain|customer (?:delay|deferral)|"
    r"regulatory|approval)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
DOWNWARD = (RevisionDirection.LOWERED, RevisionDirection.WITHDRAWN)


def _find_explanation(rev: GuidanceRevision, doc: Optional[SourceDocument]) -> tuple[Optional[bool], str]:
    """Look for a stated reason in the revision sentence and its neighbours.

    Returns (explained, verbatim sentence).  (None, "") when text is unavailable.
    """
    if doc is None or not doc.full_text():
        return None, ""
    text = doc.full_text()
    norm = re.sub(r"\s+", " ", text)
    q = re.sub(r"\s+", " ", rev.quote).strip()
    i = norm.find(q)
    if i < 0:
        return None, ""
    sents = [x.strip() for x in _SENT.split(norm[max(0, i - 300):i + len(q) + 600]) if x.strip()]
    # the revision sentence itself, then up to two following and one preceding sentence
    try:
        k = next(j for j, x in enumerate(sents) if q[:40] in x)
    except StopIteration:
        k = 0
    for j in [k, k + 1, k + 2, k - 1]:
        if 0 <= j < len(sents) and _REASON.search(sents[j]):
            return True, sents[j]
    return False, ""


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


def build_ledger(evidence: list[Evidence], series: Optional[FinancialSeries], as_of: datetime,
                 docs_by_id: Optional[dict[str, SourceDocument]] = None,
                 commentary_times: Iterable[datetime] = ()) -> list[GuidanceRecord]:
    """Build the ledger.

    ``docs_by_id`` lets downward revisions be checked for a stated reason;
    ``commentary_times`` (publication times of later calls / presentations)
    lets unmentioned targets be flagged as possibly disappearing.
    """
    items = sorted((e for e in evidence if e.usable and e.metric in GUIDANCE_METRICS
                    and e.modality in (Modality.FORWARD, Modality.CONDITIONAL, Modality.NEGATED)),
                   key=lambda e: (e.available_at, e.doc_id))
    ledger: dict[tuple[Metric, str], GuidanceRecord] = {}
    for e in items:
        end = fy_label_end(e.target_period_label) if e.target_period_label else None
        if end is not None and e.available_at is not None and e.available_at.date() > end:
            continue   # said after the period had ended: a report of results, not a target
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
        if rev.direction in DOWNWARD:
            rev.explained, rev.explanation = _find_explanation(rev, (docs_by_id or {}).get(e.doc_id))
        if rev.evidence_id not in {r.evidence_id for r in rec.revisions}:
            rec.revisions.append(rev)

    commentary = sorted(t for t in commentary_times if t is not None)
    for rec in ledger.values():
        _judge(rec, series, as_of)
        _flag(rec, commentary, as_of)
    return list(ledger.values())


def _latest_target(rec: GuidanceRecord) -> Optional[GuidanceRevision]:
    """Latest stated guidance; None if the last statement withdrew it."""
    for r in reversed(rec.revisions):
        if r.direction == RevisionDirection.WITHDRAWN:
            return None
        if r.quantity is not None:
            return r
    return rec.original


def _grade(realized: float, rev: GuidanceRevision) -> GuidanceOutcome:
    q = rev.quantity
    lo = q.low if q.low is not None else q.value
    if realized >= lo:
        return GuidanceOutcome.MET
    if lo > 0 and realized >= 0.8 * lo:
        return GuidanceOutcome.PARTIALLY_MET
    return GuidanceOutcome.MISSED


def _flag(rec: GuidanceRecord, commentary: list[datetime], as_of: datetime) -> None:
    for r in rec.revisions:
        if r.direction not in DOWNWARD:
            continue
        verb = "withdrawn" if r.direction == RevisionDirection.WITHDRAWN else "revised down"
        when = r.stated_at.date() if r.stated_at else "?"
        if r.explained:
            rec.flags.append(f"{verb} on {when} with a stated reason (credibility needs human review): "
                             f"\"{r.explanation[:200]}\"")
        elif r.explained is False:
            rec.flags.append(f"{verb} on {when} with no stated reason found near the statement")
        else:
            rec.flags.append(f"{verb} on {when}; source text unavailable to check for a reason")
    if rec.outcome == GuidanceOutcome.MISSED and rec.latest_outcome in (GuidanceOutcome.MET,
                                                                       GuidanceOutcome.PARTIALLY_MET):
        rec.flags.append("missed the original target but delivered the revised guidance")
    if rec.latest_outcome == GuidanceOutcome.MISSED:
        rec.flags.append("missed even the latest stated guidance" if rec.revisions
                         else "missed its guidance (never revised)")
    # disappearing target: not repeated in later commentary before its deadline
    if rec.target_period_label == "unspecified" or _latest_target(rec) is None:
        return
    end = fy_label_end(rec.target_period_label)
    last_mention = (rec.revisions[-1] if rec.revisions else rec.original).stated_at
    if end is None or last_mention is None:
        return
    horizon = min(as_of, datetime.combine(end, time(23, 59, 59), tzinfo=IST))
    silent = [t for t in commentary if last_mention < t <= horizon]
    if len(silent) >= DISAPPEARING_AFTER_DOCS:
        rec.flags.append(f"not mentioned again in {len(silent)} later call/presentation document(s) "
                         f"before the deadline (possible disappearing target)")


def _judge(rec: GuidanceRecord, series: Optional[FinancialSeries], as_of: datetime) -> None:
    if rec.target_period_label == "unspecified":
        rec.outcome = rec.latest_outcome = GuidanceOutcome.PENDING
        rec.outcome_note = "no target period stated; cannot be judged"
        return
    end = fy_label_end(rec.target_period_label)
    if end is None:
        rec.outcome_note = "unparseable target period"
        return
    if end >= as_of.date():
        rec.outcome = rec.latest_outcome = GuidanceOutcome.PENDING
        rec.outcome_note = f"target period ends {end}"
        return
    realized = _realized(series, rec.metric, rec.target_period_label)
    if realized is None:
        if (as_of.date() - end).days <= RESULTS_LAG_DAYS:
            rec.outcome = rec.latest_outcome = GuidanceOutcome.PENDING
            rec.outcome_note = "results for target period not yet due/public"
        else:
            rec.outcome = rec.latest_outcome = GuidanceOutcome.UNVERIFIABLE
            rec.outcome_note = "no comparable realized data in sources"
        return
    rec.realized_value = round(realized, 2)
    q = rec.original.quantity
    rec.outcome = _grade(realized, rec.original)
    latest = _latest_target(rec)
    if latest is None:
        rec.latest_outcome = GuidanceOutcome.UNVERIFIABLE
        latest_note = "guidance withdrawn before the deadline"
    else:
        rec.latest_outcome = _grade(realized, latest)
        latest_note = (f"latest {latest.quantity.raw or latest.quantity.value} -> {rec.latest_outcome.value}"
                       if latest is not rec.original else "no revision")
    rec.outcome_note = f"realized {realized:.2f} vs original {q.raw or q.value} -> {rec.outcome.value}; {latest_note}"
