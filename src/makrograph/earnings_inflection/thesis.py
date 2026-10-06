"""Forward-looking inflection thesis, per mechanism.

The evidence-status ladder looks backwards: it needs reported results, and two
periods of them to confirm.  This layer keeps that ladder unchanged and adds the
forward-looking view next to it:

* **Leading candidate** - a source-backed change in economics that is not yet in
  reported results: verified binding external orders material to revenue, a rising
  company-stated order book, commissioned capacity, issuer-stated realised price /
  input-cost / mix / volume changes.  Bare forward-looking statements are not leading
  signals (they stay ASSERTION_ONLY).
* **First-results validation** - the first results published *after* the signal are a
  separate, dated milestone, measured on the mechanism's own metric (revenue growth for
  orders / capacity / volume, gross margin for pricing / mix).  VALIDATED, NOT_VALIDATED
  or ADVERSE; PENDING before the deadline, OVERDUE when results should be public but did
  not parse (a data gap, not a verdict).
* **Confirmation** - a second period raises confidence (low -> medium -> high).  It is
  never an admission gate.
* Every mechanism keeps its own dated positive and negative evidence.
* When the financial series is stale the last known thesis is kept and labelled stale.

Earliest defensible time is always the latest availability of the inputs a signal
needs.  Nothing here reads prices or later outcomes; states are research
descriptions, never actions.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from .contracts import (
    IST, EconomicEvent, Evidence, InflectionThesis, Mechanism, MechanismResult, MechanismState, MechanismThesis,
    Metric, MilestoneOutcome, Modality, ThesisMilestone, ThesisStage, Unit,
)
from .demand import first_binding_at, summarise_demand
from .financial_series import FinancialSeries, _year_ago, prev_period_end
from .mechanisms import DEFAULT_MECHANISM_THRESHOLDS, _usual_cost_parts

DEFAULT_THESIS_THRESHOLDS = {
    "validation_revenue_growth_pct": 15.0,   # first results after an order / capacity / volume signal
    "validation_margin_bps": 150.0,          # first results after a pricing / mix signal
    "order_inflow_to_ttm_revenue": 0.25,     # verified binding external inflow (12 months) / TTM revenue
    "order_book_growth_pct": 30.0,           # company-stated order book vs a snapshot 5-15 months earlier
    "capacity_expansion_pct": 20.0,          # same facility, commissioned capacity
    "leading_max_age_days": 400,             # older signals no longer lead anything
    "results_deadline_days": 45,             # SEBI: 45 days after a quarter / half-year ...
    "results_deadline_days_year_end": 60,    # ... 60 days after the financial year
    "history_periods": 8,
}

# mechanism -> how the first results after its signal are judged
_REVENUE_TESTED = (Mechanism.ORDER_QUALITY, Mechanism.UTILIZATION, Mechanism.ORGANIC_VOLUME)
_MARGIN_TESTED = (Mechanism.PRICING_INPUT, Mechanism.PRODUCT_MIX)

_STAGE_RANK = {ThesisStage.CONFIRMED: 5, ThesisStage.FIRST_RESULTS_VALIDATED: 4, ThesisStage.REALIZED_EMERGING: 3,
               ThesisStage.LEADING: 2, ThesisStage.FIRST_RESULTS_NOT_VALIDATED: 1}
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _d(x) -> Optional[date]:
    if x is None:
        return None
    return x.date() if isinstance(x, datetime) else x


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=IST)


# --- reported-period measurements --------------------------------------------------------

def _avail(points) -> Optional[datetime]:
    """When a measurement first became knowable: the latest FIRST publication of its inputs
    (a later filing repeating a figure as a comparative does not move it)."""
    ts = [p.first_public_at or p.available_at for p in points if p is not None
          and (p.first_public_at or p.available_at) is not None]
    return max(ts) if ts else None


def _revenue_growth(series: FinancialSeries, end: date, p: str):
    now, ya = series.get(Metric.REVENUE, end, p), series.get(Metric.REVENUE, _year_ago(end), p)
    if not now or not ya or not ya.value:
        return None, None
    return (now.value / ya.value - 1) * 100, _avail([now, ya])


def _margin_change(series: FinancialSeries, end: date, p: str):
    """(bps change vs year-ago, basis, availability): gross margin on the company's usual
    cost definition, else EBITDA margin."""
    parts = _usual_cost_parts(series, p)
    if parts:
        now, _ = series.gross_margin(end, p, parts)
        ya, _ = series.gross_margin(_year_ago(end), p, parts)
        if now is not None and ya is not None:
            keys = [series.get(m, d, p) for m in (Metric.REVENUE, *parts) for d in (end, _year_ago(end))]
            return (now - ya) * 100, f"gross margin {ya:.1f}% -> {now:.1f}%", _avail(keys)
    now, ya = series.margin(end, p), series.margin(_year_ago(end), p)
    if now is not None and ya is not None:
        keys = [series.get(m, d, p) for m in (Metric.REVENUE, Metric.EBITDA) for d in (end, _year_ago(end))]
        return (now - ya) * 100, f"EBITDA margin {ya:.1f}% -> {now:.1f}%", _avail(keys)
    return None, "", None


def _measure(mech: Mechanism, series, end, p, th):
    """(value, unit text, availability, sign) for one reported period; sign +1 / -1 / 0."""
    if mech in _MARGIN_TESTED:
        bps, basis, at = _margin_change(series, end, p)
        if bps is None:
            return None
        sign = 1 if bps >= th["validation_margin_bps"] else -1 if bps <= -th["validation_margin_bps"] else 0
        return bps, f"{basis} ({bps:+.0f} bps YoY)", at, sign
    g, at = _revenue_growth(series, end, p)
    if g is None:
        return None
    sign = 1 if g >= th["validation_revenue_growth_pct"] else -1 if g < 0 else 0
    return g, f"revenue {g:+.1f}% YoY", at, sign


def _results_published(series: FinancialSeries, p: str) -> list[tuple[date, datetime]]:
    """(period end, when its revenue figure first became public), oldest first."""
    out = []
    for e in series.period_ends(Metric.REVENUE, p):
        pt = series.get(Metric.REVENUE, e, p)
        at = pt and (pt.first_public_at or pt.available_at)
        if at:
            out.append((e, at))
    return sorted(out)


def _deadline(signal_at: datetime, p: str, th) -> date:
    """Results deadline for the first reporting period that ends after the signal."""
    end = next_period_end_on_or_after(_d(signal_at), p)
    days = th["results_deadline_days_year_end"] if end.month == 3 else th["results_deadline_days"]
    return end + timedelta(days=days)


def next_period_end_on_or_after(d: date, p: str) -> date:
    """Quarter ends Jun/Sep/Dec/Mar; half-year ends Sep/Mar (Indian financial year)."""
    ends = (3, 6, 9, 12) if p == "Q" else (3, 9)
    for y in (d.year, d.year + 1):
        for m in ends:
            e = date(y, m, 30 if m in (6, 9) else 31)
            if e >= d:
                return e
    raise ValueError(d)


# --- leading signals ----------------------------------------------------------------------

def _ttm_known_at(series: FinancialSeries, p: str, t: datetime) -> Optional[float]:
    known = [e for e, at in _results_published(series, p) if at <= t]
    for e in reversed(known):
        v, _ = series.ttm(Metric.REVENUE, e, p)
        fy = series.get(Metric.REVENUE, e, "FY")
        if v is None and fy is not None and (fy.first_public_at or fy.available_at) <= t:
            v = fy.value                      # the reported financial year covers the same 12 months
        if v:
            return v
    return None


def _leading_orders(series, evidence, events, p, as_of, th) -> list[tuple[datetime, str, list[str], list[str]]]:
    """Order-based leading signals: (time, description, doc_ids, evidence_ids)."""
    out = []
    if p is None:
        return out
    dem = summarise_demand(events, evidence, as_of)
    # an order counts from the day it became BINDING (an earlier L1 / LoI is not yet an order)
    bound = lambda e: first_binding_at(e) or e.first_public_at  # noqa: E731
    verified = sorted((e for e in events if e.event_id in dem.verified_events and bound(e)), key=bound)
    window: list[EconomicEvent] = []
    for e in verified:
        window = [x for x in window if (bound(e) - bound(x)).days <= 365] + [e]
        total = sum(x.amount.value for x in window if x.amount)
        ttm = _ttm_known_at(series, p, bound(e))
        if ttm and total >= th["order_inflow_to_ttm_revenue"] * ttm:
            out.append((bound(e),
                        f"verified binding external orders {total:,.1f} cr in 12 months = {total / ttm:.2f}x TTM "
                        f"revenue known then ({ttm:,.1f} cr)",
                        sorted({d for x in window for d in x.doc_ids}),
                        [i for x in window for i in x.evidence_ids]))
            break
    # a rising order book is a change in economics; a high but flat book (cover) is context only
    books = order_book_snapshots(evidence)
    for b in books:
        earlier = [a for a in books if 150 <= (b.available_at - a.available_at).days <= 460]
        if not earlier:
            continue
        a = earlier[-1]
        g = (b.quantity.value / a.quantity.value - 1) * 100 if a.quantity.value else None
        if g is not None and g >= th["order_book_growth_pct"]:
            ttm = _ttm_known_at(series, p, b.available_at)
            cover = f"; {b.quantity.value / ttm:.2f}x TTM revenue known then" if ttm else ""
            out.append((b.available_at, f"stated order book {a.quantity.value:,.1f} -> {b.quantity.value:,.1f} cr "
                        f"({g:+.0f}%; disclosed {_d(a.available_at)} -> {_d(b.available_at)}{cover})",
                        sorted({a.doc_id, b.doc_id}), [a.evidence_id, b.evidence_id]))
    return sorted(out, key=lambda x: x[0])


_BOOK_AMOUNT = re.compile(r"order\s*book\b[^.;:\u2013\u2014]{0,40}?(?:rs\.?|inr|\u20b9)\s*~?\s*([\d,]+(?:\.\d+)?)\s*"
                          r"(crores?|cr\b|lakhs?|lacs?)", re.I)


def order_book_snapshots(evidence: list[Evidence]) -> list[Evidence]:
    """Stated outstanding order books, one per filing, whose amount is the one written right
    after "order book" (a sentence can also carry revenue / operating income)."""
    out, seen = [], set()
    for x in sorted((x for x in evidence if x.usable and x.metric == Metric.ORDER_BOOK and x.quantity
                     and x.quantity.unit == Unit.INR_CRORE and x.modality == Modality.REALIZED and x.available_at),
                    key=lambda x: (x.available_at, x.evidence_id)):
        m = _BOOK_AMOUNT.search(x.quote)
        if not m or x.doc_id in seen:
            continue
        v = float(m.group(1).replace(",", "")) * (0.01 if m.group(2).lower().startswith(("l", "la")) else 1.0)
        if abs(v - x.quantity.value) > 0.01 * max(v, 1e-9):
            continue
        seen.add(x.doc_id)
        out.append(x)
    return out


def _realized(evidence: list[Evidence], metric: Metric) -> list[Evidence]:
    return sorted((e for e in evidence if e.usable and e.metric == metric and e.modality == Modality.REALIZED
                   and e.available_at), key=lambda e: (e.available_at, e.evidence_id))


_FROM_TO = re.compile(r"(?:increas|enhanc|expand|augment|rais)\w*\s+(?:the\s+)?(?:installed\s+|production\s+|"
                      r"manufacturing\s+)?capacity\s+(?:of\s+\w+\s+)?(?:from\s+([\d,]+(?:\.\d+)?)\s*([A-Za-z]{2,6})"
                      r"\s+to\s+([\d,]+(?:\.\d+)?)\s*([A-Za-z]{2,6})|to\s+([\d,]+(?:\.\d+)?)\s*([A-Za-z]{2,6})\s+"
                      r"from\s+([\d,]+(?:\.\d+)?)\s*([A-Za-z]{2,6}))", re.I)


def _leading_capacity(evidence, th):
    out = []
    # one sentence stating the old and the new capacity of a completed expansion
    for e in _realized(evidence, Metric.CAPACITY):
        m = _FROM_TO.search(e.quote)
        if not m or re.search(r"\b(?:plan|propos|will|shall|authoris|authoriz|approv|phased)\w*", e.quote, re.I):
            continue
        if m.group(1):
            a, ua, b, ub = m.group(1), m.group(2), m.group(3), m.group(4)
        else:
            b, ub, a, ua = m.group(5), m.group(6), m.group(7), m.group(8)
        if ua.lower() != ub.lower():
            continue
        va, vb = float(a.replace(",", "")), float(b.replace(",", ""))
        g = (vb / va - 1) * 100 if va else 0
        if g >= th["capacity_expansion_pct"]:
            out.append((e.available_at, f"stated capacity {va:g} -> {vb:g} {ub} ({g:+.0f}%), completed "
                        f"(disclosed {_d(e.available_at)})", [e.doc_id], [e.evidence_id]))
    caps = [e for e in _realized(evidence, Metric.CAPACITY) if e.quantity and e.quantity.value > 0]
    by_fac: dict[str, list[Evidence]] = {}
    for e in caps:
        by_fac.setdefault(e.facility or "company-level", []).append(e)
    for fac, seq in by_fac.items():
        for i, b in enumerate(seq):
            prior = [a for a in seq[:i] if a.quantity.unit == b.quantity.unit
                     and (b.available_at - a.available_at).days >= 60]
            if prior:
                a = prior[-1]
                g = (b.quantity.value / a.quantity.value - 1) * 100
                if g >= th["capacity_expansion_pct"] and re.search(
                        r"commission|commenc|operational|started production|enhanced|increased|expanded", b.quote, re.I):
                    out.append((b.available_at, f"{fac}: stated capacity {a.quantity.value:g} -> "
                                f"{b.quantity.value:g} {b.quantity.unit.value} ({g:+.0f}%), in operation",
                                sorted({a.doc_id, b.doc_id}), [a.evidence_id, b.evidence_id]))
    return sorted(out, key=lambda x: x[0])


def _leading_statements(evidence, metric, sign):
    """Issuer statements of a realised change that carry their size ("prices up 6%"): a
    direction read from wording alone is too unreliable to admit a candidate."""
    out = []
    for e in _realized(evidence, metric):
        if e.direction == sign and e.quantity is not None and e.quantity.unit == Unit.PERCENT:
            out.append((e.available_at, f"issuer states ({_d(e.available_at)}): \"{e.quote[:140]}\"",
                        [e.doc_id], [e.evidence_id]))
    return out


_MIX_FROM_TO = re.compile(r"from\s+(\d+(?:\.\d+)?)\s*%\s+to\s+(\d+(?:\.\d+)?)\s*%|"
                          r"to\s+(\d+(?:\.\d+)?)\s*%\s+from\s+(\d+(?:\.\d+)?)\s*%", re.I)
_DECLINE = re.compile(r"\b(?:declin|decreas|fell|fall|drop|reduc|lower|shrank|contract)\w*", re.I)


def _mix_shift(quote: str) -> Optional[tuple[float, float]]:
    """(old %, new %) for a stated mix change; None when the direction is not explicit."""
    m = _MIX_FROM_TO.search(quote)
    if m:
        return (float(m.group(1)), float(m.group(2))) if m.group(1) else (float(m.group(4)), float(m.group(3)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*%[^%]{0,80}?(?:compared to|against|vs\.?|versus)\s+(\d+(?:\.\d+)?)\s*%",
                  quote, re.I)
    if m:
        return float(m.group(2)), float(m.group(1))
    return None


def _leading_mix(evidence):
    out = []
    for e in _realized(evidence, Metric.MIX_SHARE):
        shift = _mix_shift(e.quote)
        if shift and shift[1] > shift[0] and e.direction >= 0 and not (
                _DECLINE.search(e.quote) and not _MIX_FROM_TO.search(e.quote)):
            out.append((e.available_at, f"issuer-stated mix shift ({_d(e.available_at)}): \"{e.quote[:140]}\"",
                        [e.doc_id], [e.evidence_id]))
    return out


def _leading_volume(evidence, th):
    out = []
    for e in _realized(evidence, Metric.VOLUME):
        if e.quantity and e.quantity.unit == Unit.PERCENT and e.direction > 0 \
                and e.quantity.value >= DEFAULT_MECHANISM_THRESHOLDS["volume_growth_pct"]:
            out.append((e.available_at, f"issuer-stated volume growth ({_d(e.available_at)}): \"{e.quote[:140]}\"",
                        [e.doc_id], [e.evidence_id]))
    return out


def leading_signals(mech: Mechanism, series, evidence, events, p, as_of: date, th) -> list:
    if mech == Mechanism.ORDER_QUALITY:
        sig = _leading_orders(series, evidence, events, p, as_of, th)
    elif mech == Mechanism.UTILIZATION:
        sig = _leading_capacity(evidence, th)
    elif mech == Mechanism.PRICING_INPUT:
        sig = sorted(_leading_statements(evidence, Metric.PRICING, +1)
                     + _leading_statements(evidence, Metric.INPUT_COST, -1), key=lambda x: x[0])
    elif mech == Mechanism.PRODUCT_MIX:
        sig = _leading_mix(evidence)
    elif mech == Mechanism.ORGANIC_VOLUME:
        sig = _leading_volume(evidence, th)
    else:
        sig = []                       # debt reduction and segment turnaround are seen in results
    return [s for s in sig if s[0] and (as_of - _d(s[0])).days <= th["leading_max_age_days"]]


def _negative_statements(mech, evidence):
    if mech == Mechanism.PRICING_INPUT:
        return (_leading_statements(evidence, Metric.PRICING, -1)
                + _leading_statements(evidence, Metric.INPUT_COST, +1))
    if mech == Mechanism.ORGANIC_VOLUME:
        return [x for x in _leading_statements(evidence, Metric.VOLUME, -1)]
    return []


# --- per-mechanism thesis ------------------------------------------------------------------

def mechanism_thesis(mr: MechanismResult, series: FinancialSeries, evidence: list[Evidence],
                     events: list[EconomicEvent], as_of: date, th: dict, stale_note: str = "") -> MechanismThesis:
    mech = mr.mechanism
    p = series.cadence(Metric.REVENUE)
    t = MechanismThesis(mechanism=mech, stage=ThesisStage.NONE, stale=mr.stale, stale_note=stale_note if mr.stale else "",
                        research_question=mr.hypothesis, source_doc_ids=list(mr.source_doc_ids))
    published = [(e, at) for e, at in _results_published(series, p)] if p else []

    if mr.state in (MechanismState.EMERGING, MechanismState.CONFIRMED, MechanismState.COMMITMENT) \
            and mr.direction == "positive" and mr.magnitude_basis:
        t.positive_evidence.append(f"{mr.state.value}: {mr.magnitude_basis}")
    if mr.state in (MechanismState.ADVERSE, MechanismState.CONTRADICTED):
        t.negative_evidence.append(f"{mr.state.value}: {mr.magnitude_basis or mr.attribution}")
    t.negative_evidence += [f"invalidator: {x}" for x in mr.invalidators]
    t.negative_evidence += [s[1] for s in _negative_statements(mech, evidence)
                            if (as_of - _d(s[0])).days <= th["leading_max_age_days"]][-3:]

    signals = leading_signals(mech, series, evidence, events, p, as_of, th) if p else []
    if signals:
        at, text, docs, _ = signals[0]
        t.leading_signal, t.leading_signal_at = text, at
        t.positive_evidence.insert(0, f"leading: {text}")
        t.source_doc_ids = sorted(set(t.source_doc_ids) | set(docs))
        t.stage, t.confidence, t.earliest_defensible_at = ThesisStage.LEADING, "low", at
        t.positive_evidence += [f"renewed ({_d(x[0])}): {x[1]}" for x in signals[1:]]
        t.validation = _validate(mech, series, p, at, as_of, th, published)
        # the validation metric after the signal, period by period (shared with other mechanisms
        # that move the same line: an outcome check, not proof that this mechanism caused it)
        for e, pub in published:
            if pub > at and _d(pub) <= as_of:
                m = _measure(mech, series, e, p, th)
                if m and m[3] > 0:
                    t.positive_evidence.append(f"after the signal, {p} {e} (public {_d(m[2])}): {m[1]}")
                elif m and m[3] < 0:
                    t.negative_evidence.append(f"after the signal, {p} {e} (public {_d(m[2])}): {m[1]}")
        v = t.validation
        if v.outcome == MilestoneOutcome.VALIDATED:
            t.stage, t.confidence = ThesisStage.FIRST_RESULTS_VALIDATED, "medium"
            nxt = [e for e, a in published if e > v.period_end and _d(a) <= as_of]
            if nxt:
                m2 = _measure(mech, series, nxt[0], p, th)
                if m2 and m2[3] > 0:
                    t.stage, t.confidence = ThesisStage.CONFIRMED, "high"
                    t.confirmation = f"second period {p} {nxt[0]}: {m2[1]}"
                elif m2:
                    t.confirmation = f"second period {p} {nxt[0]} did not repeat it: {m2[1]}"
        elif v.outcome == MilestoneOutcome.NOT_VALIDATED:
            t.stage, t.confidence = ThesisStage.FIRST_RESULTS_NOT_VALIDATED, "low"
        elif v.outcome == MilestoneOutcome.ADVERSE:
            t.stage, t.confidence = ThesisStage.ADVERSE, "low"
        elif v.outcome == MilestoneOutcome.OVERDUE:
            t.stale = True
            t.stale_note = t.stale_note or f"first results after the signal were due by {v.expected_by} and are not parsed"
    # a mechanism seen first in reported results (no earlier leading signal)
    if not mr.stale and t.stage in (ThesisStage.NONE, ThesisStage.LEADING, ThesisStage.FIRST_RESULTS_NOT_VALIDATED):
        if mr.direction == "positive" and mr.state == MechanismState.CONFIRMED:
            t.stage, t.confidence = ThesisStage.CONFIRMED, "high"
            t.confirmation = mr.durability
            t.earliest_defensible_at = t.earliest_defensible_at or mr.first_signal_at
        elif mr.direction == "positive" and mr.state == MechanismState.EMERGING and t.stage == ThesisStage.NONE:
            t.stage, t.confidence = ThesisStage.REALIZED_EMERGING, "medium"
            t.earliest_defensible_at = mr.first_signal_at
    if t.stage == ThesisStage.NONE and mr.state == MechanismState.ADVERSE:
        t.stage = ThesisStage.ADVERSE
    if mr.stale and t.stage != ThesisStage.NONE:
        t.stale_note = t.stale_note or stale_note
    if not t.research_question and t.stage == ThesisStage.LEADING:
        t.research_question = (f"Do the first results after {_d(t.leading_signal_at)} show "
                               f"{t.validation.metric_basis if t.validation else 'the change'}?")
    return t


def _validate(mech, series, p, signal_at: datetime, as_of: date, th, published) -> ThesisMilestone:
    basis = (f"gross (else EBITDA) margin change >= {th['validation_margin_bps']:.0f} bps YoY"
             if mech in _MARGIN_TESTED else
             f"revenue growth >= {th['validation_revenue_growth_pct']:.0f}% YoY")
    v = ThesisMilestone(name="first results after the leading signal", metric_basis=basis,
                        expected_by=_deadline(signal_at, p, th))
    # only a period that ENDS after the signal can show it (an earlier quarter predates it)
    after = [(e, at) for e, at in published if at > signal_at and _d(at) <= as_of and e >= _d(signal_at)]
    if not after:
        v.outcome = MilestoneOutcome.OVERDUE if as_of > v.expected_by else MilestoneOutcome.PENDING
        return v
    e, at = after[0]
    v.period_end, v.observed_at = e, at
    m = _measure(mech, series, e, p, th)
    if m is None:
        v.outcome = MilestoneOutcome.OVERDUE
        v.observed = "results published but the year-ago comparison is not available"
        return v
    v.observed = m[1]
    v.outcome = {1: MilestoneOutcome.VALIDATED, -1: MilestoneOutcome.ADVERSE, 0: MilestoneOutcome.NOT_VALIDATED}[m[3]]
    return v


def outcome_history(series: FinancialSeries, as_of: date, th: dict) -> list[str]:
    """Reported revenue growth and margin change per period, dated by first publication.
    Outcomes, not mechanisms: shown once, attributed to no mechanism."""
    p = series.cadence(Metric.REVENUE)
    if p is None:
        return []
    out = []
    for e, pub in _results_published(series, p)[-th["history_periods"]:]:
        if _d(pub) > as_of:
            continue
        g = _measure(Mechanism.ORGANIC_VOLUME, series, e, p, th)
        mg = _measure(Mechanism.PRICING_INPUT, series, e, p, th)
        parts = [x[1] for x in (g, mg) if x]
        if parts:
            out.append(f"{p} {e} (public {_d(pub)}): " + "; ".join(parts))
    return out


def build_thesis(mechanisms: list[MechanismResult], series: FinancialSeries, evidence: list[Evidence],
                 events: list[EconomicEvent], as_of: date, thresholds: Optional[dict] = None,
                 stale_note: str = "") -> InflectionThesis:
    th = {**DEFAULT_THESIS_THRESHOLDS, **(thresholds or {})}
    out = InflectionThesis()
    out.outcome_history = outcome_history(series, as_of, th)
    for mr in mechanisms:
        out.mechanisms.append(mechanism_thesis(mr, series, evidence, events, as_of, th, stale_note))
    positive = [t for t in out.mechanisms if t.stage in _STAGE_RANK]
    if positive:
        best = max(positive, key=lambda t: (_STAGE_RANK[t.stage], _CONF_RANK[t.confidence]))
        out.stage, out.confidence = best.stage, best.confidence
        standing = [t.earliest_defensible_at for t in positive
                    if t.earliest_defensible_at and t.stage != ThesisStage.FIRST_RESULTS_NOT_VALIDATED]
        out.earliest_defensible_at = min(standing) if standing else None
        out.stale = all(t.stale for t in positive)
    elif any(t.stage == ThesisStage.ADVERSE for t in out.mechanisms):
        out.stage = ThesisStage.ADVERSE
    if stale_note:
        out.stale, out.stale_note = True, stale_note
        out.notes.append("financial series is stale: the thesis shown is the last known reading, not current")
    pending = sorted((t.validation.expected_by, t.mechanism.value) for t in out.mechanisms
                     if t.validation and t.validation.outcome in (MilestoneOutcome.PENDING, MilestoneOutcome.OVERDUE))
    if pending:
        out.next_milestone = (f"first results after the {pending[0][1]} signal (due by {pending[0][0]}): "
                              "validate on the mechanism's own metric")
    elif out.stage in (ThesisStage.FIRST_RESULTS_VALIDATED, ThesisStage.REALIZED_EMERGING):
        out.next_milestone = "next results: does the change repeat for a second period (confidence upgrade)?"
    return out
