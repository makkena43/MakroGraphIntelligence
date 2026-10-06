"""Forward catalysts: detect a credible future earnings change, then verify it.

Discovery is triggered by new disclosures that establish a potentially material change in
orders / customer approvals, capacity or a bottleneck, utilisation, product mix, contract
pricing, financing cost or a loss-making segment - not by current-quarter growth, which is
recorded separately (the evidence status).

Each catalyst is a separate, persistent record keyed by its first public disclosure.  It is
rebuilt from every document public by the as-of date, so a new quarter adds evidence to it
but never overwrites an older catalyst.

For every catalyst the detector connects
    demand -> deliverable capacity -> revenue conversion -> recurring profit -> cash needs
and states which links are supported.  Capacity without demand is not an earnings change
(it may only add depreciation).  Downside / base / upside contributions are produced only
when the inputs exist; otherwise "potentially material; magnitude unresolved".

Confirmation questions are fixed at detection and judged only on reporting periods that
end after the catalyst could contribute (first two relevant periods, singly and together).
A muted quarter can stay consistent with the thesis; a missed commissioning date or a
cancelled order can contradict it even when reported growth looks strong.

Research stages are not instructions to invest.  "Confirmed for investment review" opens a
separate review gate (valuation, cash, financing, governance, liquidity, downside).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .contracts import (
    IST, Catalyst, CatalystKind, CatalystMilestone, ChainLink, DriverChange, EarningsContribution, EconomicEvent,
    EventStage, Evidence, InvestmentReview, Mechanism, MechanismResult, MechanismState, Metric, MilestoneStatus,
    Modality, RatingRationale, ResearchStage, Unit,
)
from .demand import first_binding_at, summarise_demand
from .financial_series import FinancialSeries, PERIODS_PER_YEAR, _year_ago, prev_period_end
from .thesis import (
    _leading_capacity, _leading_mix, _leading_statements, _results_published, _ttm_known_at,
    next_period_end_on_or_after, order_book_snapshots,
)

RULES_VERSION = "catalyst-rules-1"

DEFAULT_CATALYST_THRESHOLDS = {
    "order_inflow_to_ttm_revenue": 0.25,     # verified binding external orders in 12 months / TTM revenue
    "order_book_growth_pct": 30.0,           # stated order book vs a snapshot 5-15 months earlier
    "capacity_expansion_pct": 20.0,
    "materiality_share_of_ttm_ebitda": 0.15, # base incremental EBITDA / TTM EBITDA for "material"
    "default_order_horizon_months": 12,      # used only when no execution period is disclosed (labelled)
    "conversion_downside": 0.8,              # share of the book executed in the downside case
    "deliverable_run_rate_multiple": 1.3,    # execution needing more than this x current revenue needs capacity
    "demand_cover_min": 1.0,                 # order book / TTM revenue that supports new capacity
    "utilization_tight_pct": 75.0,
    "revenue_test_growth_pct": 15.0,         # relevant-period revenue test (orders, capacity, volume)
    "margin_tolerance_bps": 150.0,           # orders: margin may fall this much vs detection without failing
    "margin_test_bps": 150.0,                # pricing / mix: margin gain needed
    "finance_cost_drop_pct": 10.0,
    "dilution_max_pct": 5.0,
    "financing_min_share_of_ebitda": 0.10,   # finance cost / EBITDA below this: financing is not a material lever
    "confirm_periods": 2,                    # relevant periods that must support the mechanism
    "commissioning_grace_days": 90,
    "window_grace_days": 120,                # after the execution window: delayed if still unconfirmed
    "current_after_window_months": 12,       # a catalyst older than its window + this is history, not news
    "results_deadline_days": 45,
    "results_deadline_days_year_end": 60,
}

_COMMISSIONED = re.compile(r"\b(?:commissioned|commercial (?:production|operations?)|started (?:commercial )?production|"
                           r"commenced (?:commercial )?(?:production|operations)|became operational|is operational|"
                           r"capitali[sz]ed)\b", re.I)
_DELAYED = re.compile(r"\b(?:delay\w*|deferr\w*|postpon\w*|slipp\w*|reschedul\w*|pushed (?:out|back))\b", re.I)
_ABANDONED = re.compile(r"\b(?:cancel\w*|abandon\w*|shelved|put on hold|dropped|scrapped|withdrawn)\b", re.I)
_APPROVAL = re.compile(r"\b(?:received|obtained|secured|got|cleared|successfully completed)\b[^.]{0,40}\b(?:approval|"
                       r"qualification|vendor registration|type[- ]test\w*|homologation|PPAP|empanel\w*|"
                       r"registration as (?:an? )?(?:approved )?(?:vendor|supplier))\b", re.I)
# the company's own prices / realisations (or input costs) moved by a stated percentage
_PRICE_CHANGE = re.compile(r"(?:(?:price|prices|pricing|realis\w*|realiz\w*|ASPs?|NSRs?|NSPs?|tariffs?|"
                           r"raw material (?:cost|price)s?|input costs?)\b[^.%]{0,60}?(?:increas|ris|rose|up|hike|"
                           r"improv|grew|higher|declin|fell|lower|soften|reduc|eas)\w*[^.%]{0,30}?\d+(?:\.\d+)?\s*%"
                           r"|(?:increas|hike|rais|cut|reduc)\w*\s+(?:in\s+|of\s+)?(?:\w+\s+){0,2}(?:price|prices|"
                           r"realis\w*|realiz\w*)\s+(?:by\s+)?(?:about\s+|around\s+|~)?\d+(?:\.\d+)?\s*%)", re.I)
_BOTTLENECK = re.compile(r"\b(?:testing (?:facility|capacity|bay|lab\w*)|test bay|bottleneck|debottleneck\w*)\b", re.I)


def _d(x) -> Optional[date]:
    if x is None:
        return None
    return x.date() if isinstance(x, datetime) else x


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=IST)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    import calendar
    yy, mm = d.year + y, m + 1
    return date(yy, mm, min(d.day, calendar.monthrange(yy, mm)[1]))


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}


def parse_when(text: str, ref: date) -> Optional[date]:
    """'March 2025', 'March 2025-end', 'FY2026', 'Q2FY26', 'next 15 months' -> a date (end of that period)."""
    t = text.lower()
    m = re.search(r"next\s+(\d{1,2})\s+months?", t)
    if m:
        return _add_months(ref, int(m.group(1)))
    m = re.search(r"next\s+(\d)\s+years?", t)
    if m:
        return _add_months(ref, 12 * int(m.group(1)))
    m = re.search(r"([a-z]+)\s+(?:\d{1,2},?\s+)?(\d{4})", t)
    if m and m.group(1) in _MONTHS:
        y, mo = int(m.group(2)), _MONTHS[m.group(1)]
        return _add_months(date(y, mo, 1), 1) - timedelta(days=1)
    m = re.search(r"q([1-4])\s*fy\s?'?(\d{2,4})", t)
    if m:
        fy = int(m.group(2)) % 100 + 2000
        q_end = {1: date(fy - 1, 6, 30), 2: date(fy - 1, 9, 30), 3: date(fy - 1, 12, 31), 4: date(fy, 3, 31)}
        return q_end[int(m.group(1))]
    m = re.search(r"h([12])\s*fy\s?'?(\d{2,4})", t)
    if m:
        fy = int(m.group(2)) % 100 + 2000
        return date(fy - 1, 9, 30) if m.group(1) == "1" else date(fy, 3, 31)
    m = re.search(r"fy\s?'?(\d{2,4})", t)
    if m:
        return date(int(m.group(1)) % 100 + 2000, 3, 31)
    return None


# --- seeds: what a disclosure establishes ----------------------------------------------------------

@dataclass
class Seed:
    kind: CatalystKind
    at: datetime
    change: str
    key: str                                  # identity of the change (for de-duplication)
    doc_ids: list[str]
    evidence_ids: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    expectations: list[str] = field(default_factory=list)
    q: dict = field(default_factory=dict)     # quantities used later
    completed: bool = True                    # capacity: commissioned vs planned


def _horizon_months(rationales: list[RatingRationale], near: datetime, book_date: date) -> tuple[Optional[int], str]:
    """Execution horizon stated next to the order book (same rationale, or one within 30 days)."""
    for r in sorted(rationales, key=lambda x: abs(((x.published_at or near) - near).days)):
        if r.published_at and abs((r.published_at - near).days) <= 30:
            for f in r.get("execution_horizon"):
                end = parse_when(f.text, book_date)
                if end and end > book_date:
                    months = max(1, round((end - book_date).days / 30.44))
                    return months, f"{f.text} ({r.agency} rationale {r.rationale_date})"
    return None, ""


def _order_seeds(series, evidence, events, rationales, p, as_of, th) -> list[Seed]:
    out = []
    if p is None:
        return out
    books = order_book_snapshots(evidence)
    for b in books:
        earlier = [a for a in books if 150 <= (b.available_at - a.available_at).days <= 460]
        if not earlier:
            continue
        a = earlier[-1]
        if not a.quantity.value:
            continue
        g = (b.quantity.value / a.quantity.value - 1) * 100
        if g < th["order_book_growth_pct"]:
            continue
        months, basis = _horizon_months(rationales, b.available_at, _d(b.available_at))
        role = " (rating-agency rationale)" if b.source_role == "rating_agency" else ""
        out.append(Seed(
            CatalystKind.ORDERS, b.available_at,
            f"stated order book {a.quantity.value:,.1f} -> {b.quantity.value:,.1f} cr ({g:+.0f}%; disclosed "
            f"{_d(a.available_at)} -> {_d(b.available_at)})", f"book:{b.quantity.value:.0f}",
            sorted({a.doc_id, b.doc_id}), [a.evidence_id, b.evidence_id],
            facts=[f"{_d(b.available_at)}{role}: \"{b.quote[:220]}\""],
            expectations=[f"execution horizon: {basis}"] if basis else [],
            q={"book": b.quantity.value, "book_prev": a.quantity.value, "horizon_months": months,
               "horizon_basis": basis}))
    dem = summarise_demand(events, evidence, as_of)
    bound = lambda e: first_binding_at(e) or e.first_public_at  # noqa: E731
    verified = sorted((e for e in events if e.event_id in dem.verified_events and bound(e)), key=bound)
    window: list[EconomicEvent] = []
    for e in verified:
        window = [x for x in window if (bound(e) - bound(x)).days <= 365] + [e]
        total = sum(x.amount.value for x in window if x.amount)
        ttm = _ttm_known_at(series, p, bound(e))
        if ttm and total >= th["order_inflow_to_ttm_revenue"] * ttm:
            durations = [x.duration_months for x in window if x.duration_months]
            out.append(Seed(
                CatalystKind.ORDERS, bound(e),
                f"verified binding external orders {total:,.1f} cr in 12 months = {total / ttm:.2f}x TTM revenue "
                f"known then", f"inflow:{bound(e).date()}", sorted({d for x in window for d in x.doc_ids}),
                [i for x in window for i in x.evidence_ids],
                facts=[f"{_d(bound(x))}: {x.counterparty or 'customer'} {x.amount.value:,.1f} cr "
                       f"({x.current_stage.value if x.current_stage else ''})" for x in window if x.amount],
                q={"inflow": total, "horizon_months": max(durations) if durations else None,
                   "horizon_basis": "longest disclosed execution period" if durations else ""}))
            window = []                     # the next crossing is a separate catalyst
    return out


def _capacity_seeds(evidence, rationales, th) -> list[Seed]:
    out = []
    for at, text, docs, ev in _leading_capacity(evidence, th):
        m = re.search(r"([\d.]+)\s*->\s*([\d.]+)\s*(\S+)", text)
        q = {"from": float(m.group(1)), "to": float(m.group(2)), "unit": m.group(3)} if m else {}
        out.append(Seed(CatalystKind.CAPACITY, at, text, f"cap:{q.get('to')}{q.get('unit')}", docs, ev,
                        facts=[f"{_d(at)}: {text}"], q=q, completed=True))
    for r in rationales:
        for f in r.get("capacity_change"):
            m = re.search(r"([\d.]+)\s*->\s*([\d.]+)\s*(\S+)", f.text)
            if not m or not r.published_at:
                continue
            a, b = float(m.group(1)), float(m.group(2))
            if not a or (b / a - 1) * 100 < th["capacity_expansion_pct"]:
                continue
            q = {"from": a, "to": b, "unit": m.group(3)}
            if f.expected:
                when = next((parse_when(c.text, _d(r.published_at)) for c in r.get("completion") if c.expected), None)
                q["expected_completion"] = when.isoformat() if when else None
            out.append(Seed(CatalystKind.CAPACITY, r.published_at,
                            f"capacity {a:g} -> {b:g} {m.group(3)} ({(b / a - 1) * 100:+.0f}%"
                            f"{', planned' if f.expected else ', completed'}; {r.agency} rationale {r.rationale_date})",
                            f"cap:{b}{m.group(3)}", [r.doc_id],
                            facts=[] if f.expected else [f"{r.rationale_date} {r.agency}: \"{f.quote[:200]}\""],
                            expectations=[f"{r.rationale_date} {r.agency} (plan reported, not proof of execution): "
                                          f"\"{f.quote[:200]}\""] if f.expected else [],
                            q=q, completed=not f.expected))
    # planned expansions stated by the issuer ("increase capacity from 9,500 MVA to 16,000 MVA ... next 2 years")
    for e in evidence:
        if not (e.usable and e.metric == Metric.CAPACITY and e.modality in (Modality.FORWARD, Modality.CONDITIONAL)
                and e.available_at):
            continue
        m = re.search(r"from\s+([\d,]+(?:\.\d+)?)\s*([A-Za-z]{2,6})\s+to\s+([\d,]+(?:\.\d+)?)\s*([A-Za-z]{2,6})",
                      e.quote)
        if not m or m.group(2).lower() != m.group(4).lower():
            continue
        a, b = float(m.group(1).replace(",", "")), float(m.group(3).replace(",", ""))
        if not a or (b / a - 1) * 100 < th["capacity_expansion_pct"]:
            continue
        when = parse_when(e.quote, _d(e.available_at))
        out.append(Seed(CatalystKind.CAPACITY, e.available_at,
                        f"planned capacity {a:g} -> {b:g} {m.group(4)} ({(b / a - 1) * 100:+.0f}%), stated by the issuer",
                        f"cap:{b}{m.group(4)}", [e.doc_id], [e.evidence_id],
                        expectations=[f"{_d(e.available_at)} issuer plan: \"{e.quote[:220]}\""],
                        q={"from": a, "to": b, "unit": m.group(4),
                           "expected_completion": when.isoformat() if when else None}, completed=False))
    for e in evidence:
        if e.usable and e.available_at and _BOTTLENECK.search(e.quote) and re.search(
                r"capacity|expan|commission|augment|new", e.quote, re.I) and e.metric in (
                Metric.CAPACITY, Metric.CAPEX):
            done = e.modality == Modality.REALIZED and bool(_COMMISSIONED.search(e.quote))
            out.append(Seed(CatalystKind.CAPACITY, e.available_at,
                            f"testing / bottleneck capacity {'added' if done else 'planned'} "
                            f"({_d(e.available_at)})", f"bottleneck:{_d(e.available_at)}", [e.doc_id], [e.evidence_id],
                            facts=[f"\"{e.quote[:220]}\""] if done else [],
                            expectations=[] if done else [f"\"{e.quote[:220]}\""],
                            q={"bottleneck": True}, completed=done))
    return out


def _other_seeds(series, evidence, mechanisms, rationales, as_of, th) -> list[Seed]:
    out = []
    for at, text, docs, ev in _leading_mix(evidence):
        out.append(Seed(CatalystKind.PRODUCT_MIX, at, text, f"mix:{_d(at)}", docs, ev, facts=[text]))
    for at, text, docs, ev in (_leading_statements(evidence, Metric.PRICING, +1)
                               + _leading_statements(evidence, Metric.INPUT_COST, -1)):
        if not _PRICE_CHANGE.search(text):
            continue            # a percentage near a price word is not yet a stated price / cost change
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        out.append(Seed(CatalystKind.CONTRACT_PRICING, at, text, f"price:{_d(at)}", docs, ev, facts=[text],
                        q={"pct": float(pct.group(1)) if pct else None}))
    for e in evidence:
        if e.usable and e.modality == Modality.REALIZED and e.available_at and _APPROVAL.search(e.quote):
            out.append(Seed(CatalystKind.CUSTOMER_APPROVAL, e.available_at,
                            f"customer approval / qualification ({_d(e.available_at)})", f"appr:{e.doc_id}",
                            [e.doc_id], [e.evidence_id], facts=[f"\"{e.quote[:220]}\""],
                            q={"counterparty": e.counterparty}))
    for m in mechanisms:
        if m.stale or m.direction != "positive" or m.state not in (MechanismState.EMERGING, MechanismState.CONFIRMED) \
                or m.first_signal_at is None:
            continue
        if m.mechanism == Mechanism.DEBT_REDUCTION and not _financing_material(series, m.first_signal_at, th):
            continue
        kind = {Mechanism.UTILIZATION: CatalystKind.UTILIZATION, Mechanism.PRODUCT_MIX: CatalystKind.PRODUCT_MIX,
                Mechanism.DEBT_REDUCTION: CatalystKind.FINANCING_COST,
                Mechanism.SEGMENT_TURNAROUND: CatalystKind.SEGMENT_TURNAROUND}.get(m.mechanism)
        if kind is None:
            continue
        seg = re.match(r"([^:]+):", m.magnitude_basis or "")
        out.append(Seed(kind, m.first_signal_at, f"{m.mechanism.value}: {m.magnitude_basis or m.attribution}",
                        f"{kind.value}:{m.period_end}", list(m.source_doc_ids), list(m.evidence_ids),
                        facts=[m.magnitude_basis], q={"magnitude": m.magnitude, "unit": m.magnitude_unit,
                                                      "segment": seg.group(1).strip() if seg else None,
                                                      "period_end": m.period_end.isoformat() if m.period_end else None}))
    for r in rationales:
        if r.action == "upgraded" and r.published_at and _financing_material(series, r.published_at, th):
            out.append(Seed(CatalystKind.FINANCING_COST, r.published_at,
                            f"credit rating upgraded to {r.long_term_rating} ({r.outlook}) by {r.agency}",
                            f"upgrade:{r.agency}:{r.rationale_date}", [r.doc_id],
                            facts=[f"{r.rationale_date} {r.agency}: upgraded to {r.long_term_rating}"],
                            q={"upgrade": True}))
    return out


def _financing_material(series, t: datetime, th) -> bool:
    """Financing is a material earnings lever only when interest is a real share of EBITDA."""
    p = series.cadence(Metric.REVENUE)
    if p is None:
        return False
    fc, _ = _ttm_at(series, Metric.FINANCE_COST, p, t)
    eb, _ = _ttm_at(series, Metric.EBITDA, p, t)
    return bool(fc and eb and eb > 0 and fc / eb >= th["financing_min_share_of_ebitda"])


def _dedupe(seeds: list[Seed]) -> list[Seed]:
    """The same change restated later (annual report, next rationale) is one catalyst: keep the
    earliest disclosure, merge documents and facts."""
    seeds = sorted(seeds, key=lambda s: (s.at, s.kind.value, s.key))
    out: list[Seed] = []
    for s in seeds:
        dup = next((x for x in out if x.kind == s.kind and x.key == s.key and (s.at - x.at).days <= 400), None)
        if dup:
            dup.doc_ids = sorted(set(dup.doc_ids) | set(s.doc_ids))
            dup.facts += [f for f in s.facts if f not in dup.facts][:3]
            dup.expectations += [f for f in s.expectations if f not in dup.expectations][:3]
            if s.completed and not dup.completed:
                dup.facts.append(f"{_d(s.at)}: reported completed")
            continue
        out.append(s)
    return out


# --- per-period measurements (relevant periods only) ---------------------------------------------------

def _first_public(series, metric, end, p) -> Optional[datetime]:
    pt = series.get(metric, end, p)
    return (pt.first_public_at or pt.available_at) if pt else None


def _cum_growth(series, metric, ends, p) -> Optional[float]:
    now = [series.get(metric, e, p) for e in ends]
    ya = [series.get(metric, _year_ago(e), p) for e in ends]
    if not ends or any(x is None for x in now + ya):
        return None
    a, b = sum(x.value for x in ya), sum(x.value for x in now)
    return (b / a - 1) * 100 if a > 0 else None


def _margin(series, ends, p) -> Optional[float]:
    rev = [series.get(Metric.REVENUE, e, p) for e in ends]
    ebitda = [series.get(Metric.EBITDA, e, p) for e in ends]
    if not ends or any(x is None for x in rev + ebitda) or sum(x.value for x in rev) <= 0:
        return None
    return sum(x.value for x in ebitda) / sum(x.value for x in rev) * 100


def _gross_or_ebitda_change(series, ends, p) -> tuple[Optional[float], str]:
    from .mechanisms import _usual_cost_parts
    parts = _usual_cost_parts(series, p)
    if parts:
        now = [series.gross_margin(e, p, parts)[0] for e in ends]
        ya = [series.gross_margin(_year_ago(e), p, parts)[0] for e in ends]
        if ends and None not in now + ya:
            return (sum(now) / len(now) - sum(ya) / len(ya)) * 100, "gross margin"
    now, ya = _margin(series, ends, p), _margin(series, [_year_ago(e) for e in ends], p)
    if now is not None and ya is not None:
        return (now - ya) * 100, "EBITDA margin"
    return None, ""


def relevant_periods(series: FinancialSeries, p: str, start: date, as_of: date, n: int, th: dict
                     ) -> list[tuple[date, Optional[datetime], date]]:
    """The first ``n`` reporting periods that END after ``start`` (calendar periods, so a missing
    filing shows up as a gap): (period end, first public time or None, results deadline)."""
    out, e = [], next_period_end_on_or_after(start + timedelta(days=1), p)
    for _ in range(n):
        deadline = e + timedelta(days=th["results_deadline_days_year_end"] if e.month == 3
                                 else th["results_deadline_days"])
        at = _first_public(series, Metric.REVENUE, e, p)
        out.append((e, at if at and _d(at) <= as_of else None, deadline))
        e = next_period_end_on_or_after(e + timedelta(days=1), p)
    return out


def _judge_series(ms: CatalystMilestone, periods, as_of, value_fn, met, consistent, label, n_required) -> None:
    """Common logic: judged on the published relevant periods; muted periods stay consistent
    until all required periods are in."""
    published = [e for e, at, _ in periods if at is not None]
    overdue = [e for e, at, dl in periods if at is None and as_of > dl]
    if not published:
        ms.status = MilestoneStatus.DATA_UNAVAILABLE if overdue else MilestoneStatus.PENDING
        if overdue:
            ms.observed = f"results for {overdue[0]} were due by {periods[0][2]} and are not available"
        return
    # a gap BEFORE a published period is a data problem, not a business verdict
    if overdue and overdue[0] < published[-1] and len(published) < n_required:
        ms.status = MilestoneStatus.DATA_UNAVAILABLE
        ms.observed = f"results for {overdue[0]} not available"
        return
    v = value_fn(published)
    if v is None:
        ms.status = MilestoneStatus.DATA_UNAVAILABLE
        ms.observed = f"{label}: comparison not available for {', '.join(str(e) for e in published)}"
        return
    ms.observed = f"{label} over {', '.join(str(e) for e in published)}: {v:+.1f}"
    ms.periods_judged = len(published)
    ms.value = v
    ms.observed_at = max(at for _, at, _ in periods if at is not None)
    done = len(published) >= n_required
    if met(v):
        ms.status = MilestoneStatus.MET
    elif consistent(v):
        ms.status = MilestoneStatus.MISSED if done else MilestoneStatus.CONSISTENT_MUTED
    else:
        ms.status = MilestoneStatus.CONTRADICTED if done else MilestoneStatus.MISSED


# --- one catalyst ---------------------------------------------------------------------------------------

def _cid(ticker: str, s: Seed) -> str:
    return hashlib.sha1(f"{ticker}|{s.kind.value}|{_d(s.at)}|{s.key}".encode()).hexdigest()[:12]


def _ttm_at(series, metric, p, t: datetime) -> tuple[Optional[float], Optional[date]]:
    known = [e for e, at in _results_published(series, p) if at <= t]
    for e in reversed(known):
        v, _ = series.ttm(metric, e, p)
        if v is None:
            fy = series.get(metric, e, "FY")               # the reported year covers the same 12 months
            if fy is not None and (fy.first_public_at or fy.available_at) and (fy.first_public_at or fy.available_at) <= t:
                v = fy.value
        if v is not None:
            return v, e
    return None, None


def _margins_known(series, p, t: datetime) -> list[float]:
    known = [e for e, at in _results_published(series, p) if at <= t][-4:]
    return [m for m in (series.margin(e, p) for e in known) if m is not None]


def _latest_book(evidence, as_of: date) -> Optional[Evidence]:
    books = [b for b in order_book_snapshots(evidence) if (as_of - _d(b.available_at)).days <= 200]
    return books[-1] if books else None


def build_catalyst(ticker: str, s: Seed, series: FinancialSeries, evidence: list[Evidence],
                   events: list[EconomicEvent], rationales: list[RatingRationale], all_seeds: list[Seed],
                   as_of: date, th: dict, series_at=None) -> Catalyst:
    p = series.cadence(Metric.REVENUE) or "Q"
    c = Catalyst(_cid(ticker, s), ticker, s.kind, s.at, list(s.doc_ids), s.change, facts=list(s.facts),
                 expectations=list(s.expectations), quantities=dict(s.q))
    start = _d(s.at)
    # detection-time figures come from what was public at detection (frozen; later filings and
    # restatements do not move them)
    s_det = series_at(s.at) if series_at else series
    ttm_rev, rev_end = _ttm_at(s_det, Metric.REVENUE, p, s.at)
    ttm_ebitda, _ = _ttm_at(s_det, Metric.EBITDA, p, s.at)
    margins = _margins_known(s_det, p, s.at)
    m_base = (ttm_ebitda / ttm_rev * 100) if ttm_rev and ttm_ebitda is not None else None
    c.quantities.update({"ttm_revenue_at_detection": ttm_rev, "ttm_ebitda_at_detection": ttm_ebitda,
                         "ttm_ebitda_margin_at_detection": m_base})

    # corroborating rating-agency context (dated; plans are not proof of execution)
    fields = {CatalystKind.ORDERS: ("order_book", "execution_horizon", "customer_concentration", "payment_protection"),
              CatalystKind.CAPACITY: ("capacity", "capacity_change", "utilization", "capex", "capex_funding",
                                      "completion"),
              CatalystKind.UTILIZATION: ("utilization", "capacity"),
              CatalystKind.CONTRACT_PRICING: ("pass_through",),
              CatalystKind.FINANCING_COST: ("dscr", "interest_coverage", "debt_obligations", "liquidity")}.get(s.kind, ())
    for r in rationales:
        if not r.published_at or _d(r.published_at) > as_of or abs((r.published_at - s.at).days) > 400:
            continue
        for f in r.facts:
            if f.field in fields:
                line = f"{r.rationale_date} {r.agency}: {f.field} {f.text}"
                (c.expectations if f.expected else c.corroboration).append(
                    line + (" (plan / forecast reported by the agency; not proof of execution)" if f.expected else ""))
    c.corroboration = list(dict.fromkeys(c.corroboration))[:8]
    c.expectations = list(dict.fromkeys(c.expectations))[:8]

    # --- execution window ---
    if s.kind == CatalystKind.ORDERS:
        months = s.q.get("horizon_months")
        if months:
            c.window_start, c.window_end = start, _add_months(start, months)
            c.window_basis = f"stated execution period: {s.q.get('horizon_basis')}"
        else:
            months = th["default_order_horizon_months"]
            c.window_start, c.window_end = start, _add_months(start, months)
            c.window_basis = f"execution period not disclosed: {months} months assumed (analyst assumption)"
            c.uncertainties.append("execution period of the order book not disclosed")
        c.quantities["horizon_months_used"] = months
    elif s.kind == CatalystKind.CAPACITY:
        if s.completed:
            c.window_start = start
            c.window_basis = "capacity reported in operation at disclosure"
        else:
            when = s.q.get("expected_completion")
            c.window_start = date.fromisoformat(when) if when else None
            c.window_basis = (f"expected completion {when} (stated)" if when else
                              "completion date not disclosed")
            if not when:
                c.uncertainties.append("commissioning date not disclosed")
        c.window_end = _add_months(c.window_start, 12) if c.window_start else None
    else:
        c.window_start, c.window_end = start, _add_months(start, 9)
        c.window_basis = "next two reporting periods"

    # --- mechanism chain ---
    book = _latest_book(evidence, as_of)
    cover = (book.quantity.value / ttm_rev) if (book and ttm_rev) else None
    util = [f for r in rationales for f in r.get("utilization") if not f.expected and r.published_at
            and _d(r.published_at) <= as_of]
    other_orders = [x for x in all_seeds if x.kind == CatalystKind.ORDERS and abs((x.at - s.at).days) <= 400]
    chain: list[ChainLink] = []
    if s.kind == CatalystKind.ORDERS:
        chain.append(ChainLink("demand", "supported", s.change, list(s.doc_ids)))
    elif s.kind in (CatalystKind.CAPACITY, CatalystKind.UTILIZATION):
        basis = []
        if cover is not None and cover >= th["demand_cover_min"]:
            basis.append(f"order book {book.quantity.value:,.0f} cr = {cover:.2f}x TTM revenue ({_d(book.available_at)})")
            c.quantities["demand_order_book"] = book.quantity.value
        if util and util[-1].value is not None and util[-1].value >= th["utilization_tight_pct"]:
            basis.append(f"stated utilisation {util[-1].value:g}% before the expansion")
        if other_orders:
            basis.append(f"order catalyst(s) within a year: {other_orders[0].change[:80]}")
        if s.q.get("bottleneck") and basis:
            basis.append("testing / bottleneck capacity limits deliveries only while demand exceeds it")
        chain.append(ChainLink("demand", "supported" if basis else "unsupported",
                               "; ".join(basis) or "no order book, utilisation or order evidence: added capacity may "
                                                    "only add depreciation"))
    elif s.kind == CatalystKind.CUSTOMER_APPROVAL:
        chain.append(ChainLink("demand", "unknown", "an approval is not an order; volumes and prices not disclosed"))
    else:
        chain.append(ChainLink("demand", "not_applicable", "margin / cost mechanism"))

    if s.kind == CatalystKind.ORDERS:
        months = c.quantities["horizon_months_used"]
        size = s.q.get("book") or s.q.get("inflow")
        need = size * 12 / months if size else None
        cap_seed = [x for x in all_seeds if x.kind == CatalystKind.CAPACITY and x.completed
                    and abs((x.at - s.at).days) <= 400]
        if need and ttm_rev and need <= th["deliverable_run_rate_multiple"] * ttm_rev:
            chain.append(ChainLink("deliverable_capacity", "supported",
                                   f"execution needs ~{need:,.0f} cr/yr vs TTM revenue {ttm_rev:,.0f} cr"))
        elif cap_seed:
            chain.append(ChainLink("deliverable_capacity", "supported",
                                   f"execution needs ~{need:,.0f} cr/yr; capacity added: {cap_seed[0].change[:80]}"
                                   if need else f"capacity added: {cap_seed[0].change[:80]}", cap_seed[0].doc_ids))
        else:
            chain.append(ChainLink("deliverable_capacity", "unknown",
                                   f"execution needs ~{need:,.0f} cr/yr vs TTM revenue {ttm_rev or 0:,.0f} cr; no "
                                   "capacity evidence" if need else "order size or run-rate unknown"))
        chain.append(ChainLink("revenue_conversion", "supported" if s.q.get("horizon_months") else "unknown",
                               c.window_basis))
    elif s.kind == CatalystKind.CAPACITY:
        chain.append(ChainLink("deliverable_capacity", "supported" if s.completed else "unknown",
                               "reported in operation" if s.completed else f"pending commissioning; {c.window_basis}"))
        chain.append(ChainLink("revenue_conversion", "unknown", "first output and sales not yet observed"))
    else:
        chain.append(ChainLink("deliverable_capacity", "not_applicable", ""))
        chain.append(ChainLink("revenue_conversion", "supported" if s.kind != CatalystKind.CUSTOMER_APPROVAL
                               else "unknown", "visible in the next reporting periods"))
    pt = [f for r in rationales for f in r.get("pass_through") if f.value is not None and r.published_at
          and _d(r.published_at) <= as_of]
    if m_base is not None:
        chain.append(ChainLink("recurring_profit", "supported",
                               f"TTM EBITDA margin {m_base:.1f}% at detection"
                               + (f"; quarterly range {min(margins):.1f}-{max(margins):.1f}%" if margins else "")
                               + (f"; {pt[-1].value:g}% of orders with price-variation clauses" if pt else "")))
    else:
        chain.append(ChainLink("recurring_profit", "unknown", "EBITDA margin at detection not available"))
    cash = []
    for r in rationales:
        if r.published_at and _d(r.published_at) <= as_of and abs((r.published_at - s.at).days) <= 400:
            for f in r.facts:
                if f.field in ("working_capital_intensity", "receivable_days", "capex", "capex_funding", "dscr",
                               "liquidity"):
                    cash.append(f"{f.field} {f.text}{' (expected)' if f.expected else ''} ({r.agency} {r.rationale_date})")
    capex_ev = [e for e in evidence if e.usable and e.metric == Metric.CAPEX and e.quantity
                and e.quantity.unit == Unit.INR_CRORE and e.available_at and abs((e.available_at - s.at).days) <= 400]
    if capex_ev:
        cash.append(f"stated capex up to {max(e.quantity.value for e in capex_ev):,.0f} cr")
    cash = list(dict.fromkeys(cash))
    by_field: dict = {}
    for x in cash:                                   # latest statement per item
        by_field[x.split(" ")[0]] = x
    chain.append(ChainLink("cash_requirements", "supported" if cash else "unknown",
                           "; ".join(by_field.values()) or "working-capital and funding needs not disclosed"))
    c.chain = chain

    # --- potential earnings contribution (only where inputs support it) ---
    c.contribution = _contribution(s, c, s_det, p, ttm_rev, ttm_ebitda, m_base, margins, chain, th)

    # --- milestones fixed at detection, judged on relevant periods ---
    c.milestones, c.invalidators = _milestones(s, c, series, p, evidence, events, as_of, th)
    if c.contribution.status != "estimated" and s.kind in (CatalystKind.PRODUCT_MIX, CatalystKind.CONTRACT_PRICING):
        mm = next((m for m in c.milestones if m.status == MilestoneStatus.MET and m.value is not None), None)
        rev_now = series.ttm(Metric.REVENUE, series.latest_period(Metric.REVENUE, p), p)[0] \
            if series.latest_period(Metric.REVENUE, p) else None
        if mm and rev_now and ttm_ebitda:
            realised = mm.value / 10000 * rev_now
            k = c.contribution
            k.status = "estimated" if realised / ttm_ebitda >= th["materiality_share_of_ttm_ebitda"] else "immaterial"
            k.downside_crore, k.base_crore, k.upside_crore = round(realised * 0.5, 2), round(realised, 2), round(realised, 2)
            k.share_of_ttm_ebitda = round(realised / ttm_ebitda, 3)
            k.basis = (f"measured: {mm.value:+.0f} bps margin change over the relevant periods x current TTM revenue "
                       f"{rev_now:,.0f} cr (realised, not forecast); downside assumes half of it reverses")
    _stage(c, as_of, th)
    c.quantities["current"] = (c.window_end is None or as_of <= _add_months(c.window_end,
                                                                             th["current_after_window_months"]))
    return c


def _contribution(s, c, series, p, ttm_rev, ttm_ebitda, m_base, margins, chain, th) -> EarningsContribution:
    out = EarningsContribution()
    demand = next(x for x in chain if x.link == "demand")

    def finish(incr_rev, basis):
        if incr_rev is None or m_base is None or not margins:
            out.status = "potentially_material_unresolved"
            out.basis = basis + "; margin inputs not available"
            return out
        lo, hi = min(margins), max(margins)
        out.downside_crore = round(incr_rev * th["conversion_downside"] * lo / 100, 2)
        out.base_crore = round(incr_rev * m_base / 100, 2)
        out.upside_crore = round(incr_rev * hi / 100, 2)
        out.assumptions += [f"downside: {th['conversion_downside']:.0%} conversion at the lowest recent quarterly "
                            f"EBITDA margin {lo:.1f}%", f"base: TTM EBITDA margin {m_base:.1f}%",
                            f"upside: highest recent quarterly margin {hi:.1f}%",
                            "incremental to the revenue run-rate known at detection; before D&A, interest and tax"]
        out.share_of_ttm_ebitda = round(out.base_crore / ttm_ebitda, 3) if ttm_ebitda else None
        out.status = ("estimated" if out.share_of_ttm_ebitda is not None
                      and out.share_of_ttm_ebitda >= th["materiality_share_of_ttm_ebitda"] else "immaterial")
        out.basis = basis
        return out

    if s.kind == CatalystKind.ORDERS:
        size = s.q.get("book")
        if size is None:
            out.status = "potentially_material_unresolved"
            out.basis = ("order inflow may replace or extend the current run-rate; backlog not disclosed, so the "
                         "incremental revenue is unknown")
            return out
        months = c.quantities["horizon_months_used"]
        annual = size * 12 / months
        incr = annual - ttm_rev if ttm_rev else None
        if incr is not None and incr <= 0:
            out.status = "immaterial"
            out.basis = f"order book executable at ~{annual:,.0f} cr/yr does not exceed TTM revenue {ttm_rev:,.0f} cr"
            return out
        return finish(incr, f"order book {size:,.0f} cr over {months} months = ~{annual:,.0f} cr/yr vs TTM revenue "
                            f"{ttm_rev or 0:,.0f} cr")
    if s.kind == CatalystKind.CAPACITY:
        if demand.status != "supported":
            out.status = "potentially_material_unresolved"
            out.basis = "no demand evidence: capacity alone does not establish earnings (depreciation will rise)"
            return out
        frac = (s.q["to"] / s.q["from"] - 1) if s.q.get("from") else None
        by_capacity = frac * ttm_rev if (frac and ttm_rev) else None
        book = c.quantities.get("demand_order_book")
        by_demand = max(0.0, book - ttm_rev) if (book is not None and ttm_rev) else None
        if by_capacity is None or by_demand is None:
            out.status = "potentially_material_unresolved"
            out.basis = "demand supported but its size relative to the new capacity is not quantified"
            return out
        return finish(min(by_capacity, by_demand),
                      f"bounded by demand: min(capacity-implied {by_capacity:,.0f} cr, order book above run-rate "
                      f"{by_demand:,.0f} cr) - capacity growth is never assumed to be earnings growth")
    if s.kind in (CatalystKind.PRODUCT_MIX, CatalystKind.SEGMENT_TURNAROUND) and s.q.get("magnitude") is not None \
            and "crore" in (s.q.get("unit") or ""):
        n = PERIODS_PER_YEAR.get(p, 4)
        base = s.q["magnitude"] * n
        out.downside_crore, out.base_crore, out.upside_crore = round(base * 0.5, 2), round(base, 2), round(base, 2)
        out.share_of_ttm_ebitda = round(base / ttm_ebitda, 3) if ttm_ebitda else None
        out.status = ("estimated" if out.share_of_ttm_ebitda and out.share_of_ttm_ebitda
                      >= th["materiality_share_of_ttm_ebitda"] else "immaterial" if out.share_of_ttm_ebitda
                      is not None else "potentially_material_unresolved")
        out.basis = f"latest-period change x {n} periods (base); half of it (downside)"
        return out
    if s.kind == CatalystKind.FINANCING_COST and s.q.get("magnitude") and "net debt" in (s.q.get("unit") or ""):
        fc, _ = _ttm_at(series, Metric.FINANCE_COST, p, s.at)
        gross = [series.get(m, _d(s.at), "I") for m in (Metric.BORROWINGS_CURRENT, Metric.BORROWINGS_NONCURRENT)]
        g = sum(x.value for x in gross if x)
        if fc and g:
            rate = fc / (g + s.q["magnitude"])
            base = s.q["magnitude"] * rate
            out.downside_crore, out.base_crore, out.upside_crore = round(base * 0.5, 2), round(base, 2), round(base, 2)
            out.share_of_ttm_ebitda = round(base / ttm_ebitda, 3) if ttm_ebitda else None
            out.status = ("estimated" if out.share_of_ttm_ebitda and out.share_of_ttm_ebitda
                          >= th["materiality_share_of_ttm_ebitda"] else "immaterial")
            out.basis = f"net debt reduction x implied rate {rate:.1%} (pre-tax interest saving)"
            return out
    out.status = "potentially_material_unresolved"
    out.basis = {CatalystKind.CONTRACT_PRICING: "price change stated; input costs and pass-through decide the margin "
                                                "effect",
                 CatalystKind.CUSTOMER_APPROVAL: "volumes and pricing under the approval not disclosed",
                 CatalystKind.UTILIZATION: "utilisation change stated; the revenue and margin effect is not quantified",
                 CatalystKind.FINANCING_COST: "rating upgrade: the change in borrowing cost is not disclosed"}.get(
        s.kind, "inputs for a magnitude are not disclosed")
    return out


def _milestones(s, c, series, p, evidence, events, as_of, th):
    ms: list[CatalystMilestone] = []
    inv: list[str] = []
    n = th["confirm_periods"]
    later_ev = [e for e in evidence if e.usable and e.available_at and e.available_at > s.at
                and _d(e.available_at) <= as_of]

    def periods(start):
        return relevant_periods(series, p, start, as_of, n, th) if start else []

    if s.kind == CatalystKind.ORDERS:
        start = c.window_start
        conv = CatalystMilestone(
            "Are the orders converting on schedule?",
            f"revenue growth >= {th['revenue_test_growth_pct']:.0f}% YoY over the first {n} reporting periods ending "
            f"after {start} (each period and both together)", relevant_from=next_period_end_on_or_after(
                start + timedelta(days=1), p), due_by=c.window_end)
        pr = periods(start)
        _judge_series(conv, pr, as_of, lambda ends: _cum_growth(series, Metric.REVENUE, ends, p),
                      lambda v: v >= th["revenue_test_growth_pct"], lambda v: v >= 0, "revenue YoY %", n)
        ms.append(conv)
        m0 = c.quantities.get("ttm_ebitda_margin_at_detection")
        mm = CatalystMilestone("Are margins acceptable while executing?",
                               f"EBITDA margin over the relevant periods no more than {th['margin_tolerance_bps']:.0f} "
                               f"bps below the TTM margin at detection ({m0:.1f}%)" if m0 is not None else
                               "EBITDA margin over the relevant periods vs detection (detection margin unknown)",
                               relevant_from=conv.relevant_from, due_by=c.window_end)
        if m0 is not None:
            _judge_series(mm, pr, as_of, lambda ends: (_margin(series, ends, p) - m0) * 100
                          if _margin(series, ends, p) is not None else None,
                          lambda v: v >= -th["margin_tolerance_bps"], lambda v: v >= -2 * th["margin_tolerance_bps"],
                          "EBITDA margin vs detection, bps", n)
        else:
            mm.status = MilestoneStatus.DATA_UNAVAILABLE
        ms.append(mm)
        cancelled = [e for e in events if e.current_stage in (EventStage.CANCELLED,) and any(
            h.stage == EventStage.CANCELLED and h.at and s.at < h.at and _d(h.at) <= as_of for h in e.history)]
        nc = CatalystMilestone("No material order cancellations?", "cancellations after detection", status=
                               MilestoneStatus.CONTRADICTED if cancelled else MilestoneStatus.PENDING)
        if cancelled:
            nc.observed = "; ".join(f"{e.event_id} cancelled" for e in cancelled)
        ms.append(nc)
        inv += ["order cancellations or customer disputes", "execution period passes with revenue flat",
                "margins fall well below the detection level (low-priced orders)",
                "working-capital stretch funded by debt"]
    elif s.kind == CatalystKind.CAPACITY:
        due = c.window_start
        com = CatalystMilestone("Commissioned and producing commercially?",
                                "commissioning / commercial production stated" + (f" by {due}" if due else ""),
                                due_by=due + timedelta(days=th["commissioning_grace_days"]) if due else None)
        if s.completed:
            com.status, com.observed, com.observed_at = MilestoneStatus.MET, "reported in operation at disclosure", s.at
        else:
            done = [e for e in later_ev if _COMMISSIONED.search(e.quote) and re.search(r"capacit|plant|line|unit|facilit",
                                                                                         e.quote, re.I)]
            dropped = [e for e in later_ev if _ABANDONED.search(e.quote) and re.search(r"capex|project|plant|capacit|"
                                                                                       r"expansion", e.quote, re.I)]
            slipped = [e for e in later_ev if _DELAYED.search(e.quote) and re.search(r"capex|project|plant|capacit|"
                                                                                    r"expansion|commission", e.quote, re.I)]
            if dropped:
                com.status, com.observed = MilestoneStatus.CONTRADICTED, f"\"{dropped[0].quote[:160]}\""
            elif done:
                com.status, com.observed, com.observed_at = (MilestoneStatus.MET, f"\"{done[0].quote[:160]}\"",
                                                             done[0].available_at)
                c.window_start = _d(done[0].available_at)
            elif slipped:
                com.status, com.observed = MilestoneStatus.MISSED, f"delay stated: \"{slipped[0].quote[:160]}\""
            elif com.due_by and as_of > com.due_by:
                com.status, com.observed = MilestoneStatus.MISSED, f"no commissioning statement by {com.due_by}"
        ms.append(com)
        out_ms = CatalystMilestone("Is the new output reaching revenue?",
                                   f"revenue growth >= {th['revenue_test_growth_pct']:.0f}% YoY over the first {n} "
                                   "periods starting about two months after commissioning")
        if com.status == MilestoneStatus.MET and c.window_start:
            st = c.window_start + timedelta(days=60)
            out_ms.relevant_from = next_period_end_on_or_after(st + timedelta(days=1), p)
            _judge_series(out_ms, periods(st), as_of, lambda ends: _cum_growth(series, Metric.REVENUE, ends, p),
                          lambda v: v >= th["revenue_test_growth_pct"], lambda v: v >= 0, "revenue YoY %", n)
        else:
            out_ms.status = MilestoneStatus.NOT_YET_RELEVANT
        ms.append(out_ms)
        inv += ["commissioning deadline missed or project deferred", "no orders to load the capacity",
                "capex overrun / debt funding stretches liquidity", "customer qualification of the new line delayed"]
    elif s.kind in (CatalystKind.PRODUCT_MIX, CatalystKind.CONTRACT_PRICING):
        m = CatalystMilestone("Are higher-value products / prices visible in margins?" if s.kind ==
                              CatalystKind.PRODUCT_MIX else "Do the price changes hold in margins?",
                              f"gross (else EBITDA) margin up >= {th['margin_test_bps']:.0f} bps YoY over the "
                              f"relevant periods", relevant_from=next_period_end_on_or_after(_d(s.at) + timedelta(days=1), p))
        _judge_series(m, periods(_d(s.at)), as_of, lambda ends: _gross_or_ebitda_change(series, ends, p)[0],
                      lambda v: v >= th["margin_test_bps"], lambda v: v > -th["margin_test_bps"], "margin change bps", n)
        ms.append(m)
        inv += ["input costs rise faster than prices", "price cuts / discounting",
                "mix gain from one-off or development revenue"]
    elif s.kind == CatalystKind.FINANCING_COST:
        m = CatalystMilestone("Is interest expense falling?",
                              f"finance cost down >= {th['finance_cost_drop_pct']:.0f}% YoY over the relevant periods",
                              relevant_from=next_period_end_on_or_after(_d(s.at) + timedelta(days=1), p))
        _judge_series(m, periods(_d(s.at)), as_of, lambda ends: _cum_growth(series, Metric.FINANCE_COST, ends, p),
                      lambda v: v <= -th["finance_cost_drop_pct"], lambda v: v <= 5.0, "finance cost YoY %", n)
        ms.append(m)
        sh0 = series.shares_diluted_crore(prev_period_end(m.relevant_from, p), p) if m.relevant_from else None
        pub = [e for e, at, _ in periods(_d(s.at)) if at is not None]
        sh1 = series.shares_diluted_crore(pub[-1], p) if pub else None
        dil = CatalystMilestone("Without damaging dilution?",
                                f"diluted share count up no more than {th['dilution_max_pct']:.0f}%")
        if sh0 and sh1:
            chg = (sh1 / sh0 - 1) * 100
            dil.observed = f"diluted shares {chg:+.1f}%"
            dil.status = MilestoneStatus.MET if chg <= th["dilution_max_pct"] else MilestoneStatus.CONTRADICTED
        else:
            dil.status = MilestoneStatus.PENDING if not pub else MilestoneStatus.DATA_UNAVAILABLE
        ms.append(dil)
        inv += ["repayment funded by equity or asset sales", "new debt-funded capex"]
    elif s.kind == CatalystKind.SEGMENT_TURNAROUND:
        seg = s.q.get("segment")
        m = CatalystMilestone(f"Are recurring losses in {seg or 'the segment'} actually shrinking?",
                              "segment result better than a year earlier in the relevant periods",
                              relevant_from=next_period_end_on_or_after(_d(s.at) + timedelta(days=1), p))

        def seg_change(ends):
            if not seg:
                return None
            now = [series.segment(seg, Metric.SEGMENT_RESULT, e, p) for e in ends]
            ya = [series.segment(seg, Metric.SEGMENT_RESULT, _year_ago(e), p) for e in ends]
            if None in now + ya:
                return None
            return sum(x.value for x in now) - sum(x.value for x in ya)
        _judge_series(m, periods(_d(s.at)), as_of, seg_change, lambda v: v > 0, lambda v: v >= 0,
                      "segment result change vs year-ago, cr", n)
        ms.append(m)
        inv += ["repeated one-off or restructuring costs", "segment scope changed"]
    else:   # utilisation, customer approval
        m = CatalystMilestone("Is it reaching revenue?" if s.kind == CatalystKind.UTILIZATION else
                              "Do first orders from the approving customer follow?",
                              f"revenue growth >= {th['revenue_test_growth_pct']:.0f}% YoY over the relevant periods"
                              if s.kind == CatalystKind.UTILIZATION else "an order from the customer after the approval",
                              relevant_from=next_period_end_on_or_after(_d(s.at) + timedelta(days=1), p),
                              due_by=_add_months(_d(s.at), 12))
        if s.kind == CatalystKind.UTILIZATION:
            _judge_series(m, periods(_d(s.at)), as_of, lambda ends: _cum_growth(series, Metric.REVENUE, ends, p),
                          lambda v: v >= th["revenue_test_growth_pct"], lambda v: v >= 0, "revenue YoY %", n)
        else:
            cp = (s.q.get("counterparty") or "").lower()
            follow = [e for e in events if e.first_public_at and e.first_public_at > s.at and cp
                      and cp in (e.counterparty or "").lower() and _d(e.first_public_at) <= as_of]
            if follow:
                m.status, m.observed = MilestoneStatus.MET, f"order {follow[0].event_id} from {follow[0].counterparty}"
            elif as_of > m.due_by:
                m.status, m.observed = MilestoneStatus.MISSED, "no order from the customer within 12 months"
        ms.append(m)
    return ms, inv


_FINANCIAL_TESTS = ("converting on schedule", "reaching revenue", "visible in margins", "hold in margins",
                    "interest expense falling", "losses", "first orders")


_GUARDS = ("margins acceptable", "dilution", "cancellations")


def _is_guard(m: CatalystMilestone) -> bool:
    """Guards can contradict or delay a thesis but never validate it on their own."""
    return any(g in m.question.lower() for g in _GUARDS)


def _stage(c: Catalyst, as_of: date, th: dict) -> None:
    why: list[str] = []
    demand = next((x for x in c.chain if x.link == "demand"), None)
    st = [m.status for m in c.milestones]
    fin = [m for m in c.milestones if any(k in m.question.lower() for k in _FINANCIAL_TESTS)]
    if MilestoneStatus.CONTRADICTED in st:
        m = next(m for m in c.milestones if m.status == MilestoneStatus.CONTRADICTED)
        c.stage, why = ResearchStage.CONTRADICTED, [f"contradicted: {m.question} {m.observed}"]
    elif fin and all(m.status == MilestoneStatus.MET for m in fin) and demand and demand.status != "unsupported" \
            and c.contribution.status == "estimated" \
            and not any(s in (MilestoneStatus.MISSED, MilestoneStatus.DATA_UNAVAILABLE) for s in st) \
            and all(m.periods_judged >= th["confirm_periods"] or m.question.startswith("Do first orders")
                    for m in fin):
        c.stage, why = ResearchStage.CONFIRMED, [f"{m.question} {m.observed}" for m in fin]
    elif fin and fin[0].status == MilestoneStatus.DATA_UNAVAILABLE and MilestoneStatus.MISSED not in st:
        c.stage, why = ResearchStage.DATA_UNAVAILABLE, [f"{fin[0].question} {fin[0].observed}"]
    elif MilestoneStatus.MISSED in st:
        m = next(m for m in c.milestones if m.status == MilestoneStatus.MISSED)
        c.stage, why = ResearchStage.DELAYED, [f"behind the timetable: {m.question} {m.observed}"]
    elif any(m.status == MilestoneStatus.MET for m in c.milestones if not _is_guard(m)) \
            and demand and demand.status != "unsupported":
        c.stage = ResearchStage.VALIDATING
        why = [f"{m.question} {m.observed or 'met'}" for m in c.milestones
               if m.status == MilestoneStatus.MET and not _is_guard(m)]
        why += [f"{m.question} consistent so far ({m.observed})" for m in c.milestones
                if m.status == MilestoneStatus.CONSISTENT_MUTED]
        if fin and all(m.status == MilestoneStatus.MET for m in fin) and c.contribution.status != "estimated":
            why.append(f"mechanism verified, but materiality is not established ({c.contribution.status.replace('_', ' ')}"
                       "): not confirmed for investment review")
    else:
        supported = (demand is not None and demand.status in ("supported", "not_applicable")
                     and not any(x.status == "contradicted" for x in c.chain)
                     and c.contribution.status == "estimated"
                     and any(x.link in ("revenue_conversion", "deliverable_capacity") and x.status == "supported"
                             for x in c.chain))
        c.stage = ResearchStage.SUPPORTED if supported else ResearchStage.POTENTIAL
        if supported:
            why = [f"mechanism, materiality ({c.contribution.share_of_ttm_ebitda:.0%} of TTM EBITDA, base) and an "
                   f"execution pathway ({c.window_basis}) are supported"]
        else:
            gaps = [f"{x.link}: {x.status}" for x in c.chain if x.status in ("unknown", "unsupported")]
            if c.contribution.status != "estimated":
                gaps.append(f"magnitude: {c.contribution.status.replace('_', ' ')}")
            why = ["announced change; support incomplete (" + "; ".join(gaps) + ")"]
        pending = [m for m in c.milestones if m.status in (MilestoneStatus.PENDING, MilestoneStatus.CONSISTENT_MUTED)]
        why += [f"waiting: {m.question} ({m.observed})" if m.observed else f"waiting: {m.question}" for m in pending][:2]
    if c.stage not in (ResearchStage.CONFIRMED, ResearchStage.CONTRADICTED) and c.window_end and \
            as_of > c.window_end + timedelta(days=th["window_grace_days"]) and c.stage != ResearchStage.DATA_UNAVAILABLE:
        c.stage = ResearchStage.DELAYED
        why.insert(0, f"execution window ended {c.window_end} without confirmation")
    c.stage_reasons = why


# --- entry point -----------------------------------------------------------------------------------------

def detect_catalysts(ticker: str, series: FinancialSeries, evidence: list[Evidence], events: list[EconomicEvent],
                     mechanisms: list[MechanismResult], rationales: list[RatingRationale], as_of: date,
                     thresholds: Optional[dict] = None, measurements: Optional[list] = None) -> list[Catalyst]:
    """``measurements`` (optional) lets detection-time figures be rebuilt from what was public
    at each catalyst's first disclosure."""
    th = {**DEFAULT_CATALYST_THRESHOLDS, **(thresholds or {})}
    p = series.cadence(Metric.REVENUE)
    rats = [r for r in rationales if r.published_at and _d(r.published_at) <= as_of]
    seeds = _dedupe(_order_seeds(series, evidence, events, rats, p, as_of, th) + _capacity_seeds(evidence, rats, th)
                    + _other_seeds(series, evidence, mechanisms, rats, as_of, th))
    seeds = [s for s in seeds if _d(s.at) <= as_of]
    cache: dict = {}

    def series_at(t: datetime) -> FinancialSeries:
        if measurements is None:
            return series
        if t not in cache:
            cache[t] = FinancialSeries.build(ticker, [m for m in measurements if m.available_at and m.available_at <= t])
        return cache[t]
    return [build_catalyst(ticker, s, series, evidence, events, rats, seeds, as_of, th, series_at) for s in seeds]


_ACTIVE = (ResearchStage.SUPPORTED, ResearchStage.VALIDATING, ResearchStage.CONFIRMED)


def research_summary(catalysts: list[Catalyst], evidence_status: str, current: list[str]) -> dict:
    """Detected / Why / Waiting for / Investment review - and current performance kept apart."""
    rank = {ResearchStage.CONFIRMED: 6, ResearchStage.VALIDATING: 5, ResearchStage.SUPPORTED: 4,
            ResearchStage.DELAYED: 3, ResearchStage.DATA_UNAVAILABLE: 2, ResearchStage.POTENTIAL: 1,
            ResearchStage.CONTRADICTED: 0}
    history = [c for c in catalysts if not c.quantities.get("current", True)]
    live = sorted((c for c in catalysts if c.quantities.get("current", True)),
                  key=lambda c: (-rank[c.stage], c.first_public_at or datetime.max.replace(tzinfo=IST)))
    active = [c for c in live if c.stage in _ACTIVE]
    if active:
        detected = "credible prospective earnings change"
    elif any(c.stage == ResearchStage.POTENTIAL for c in live):
        detected = "potential catalyst(s); economics or execution support incomplete"
    elif live:
        detected = "no live supported catalyst (delayed, contradicted or data unavailable)"
    else:
        detected = "no forward catalyst in the disclosures"
    why = []
    for c in (active or live)[:3]:
        contrib = (f"base ~{c.contribution.base_crore:,.1f} cr EBITDA/yr ({c.contribution.share_of_ttm_ebitda:.0%} of "
                   f"TTM)" if c.contribution.status == "estimated" else "potentially material; magnitude unresolved"
                   if c.contribution.status == "potentially_material_unresolved" else c.contribution.status)
        links = ", ".join(f"{x.link} {x.status}" for x in c.chain)
        why.append(f"[{c.stage.value}] {c.kind.value} since {_d(c.first_public_at)}: {c.operating_change} | {links} | "
                   f"{contrib}")
    waiting = []
    for c in active + [x for x in live if x.stage in (ResearchStage.POTENTIAL, ResearchStage.DELAYED,
                                                      ResearchStage.DATA_UNAVAILABLE) and x not in active]:
        for m in c.milestones:
            if m.status in (MilestoneStatus.PENDING, MilestoneStatus.CONSISTENT_MUTED, MilestoneStatus.NOT_YET_RELEVANT,
                            MilestoneStatus.DATA_UNAVAILABLE):
                due = f" (from {m.relevant_from}" + (f", due by {m.due_by})" if m.due_by else ")") \
                    if m.relevant_from else (f" (due by {m.due_by})" if m.due_by else "")
                waiting.append(f"{c.kind.value} {_d(c.first_public_at)}: {m.question}{due} [{m.status.value}]")
    confirmed = [c for c in live if c.stage == ResearchStage.CONFIRMED]
    return {
        "detected": detected,
        "why": why,
        "waiting_for": waiting[:8],
        "investment_review": ("open: inputs assembled below (not an instruction to invest)" if confirmed else
                              "not started: only after a catalyst is confirmed, with valuation and downside assessed"),
        "history": [f"{c.kind.value} {_d(c.first_public_at)}: {c.stage.value}" for c in history][-6:],
        "current_performance": {"evidence_status": evidence_status, "basis": current[:4],
                                "note": "reported results so far; recorded separately from the forward setup"},
        "rules_version": RULES_VERSION,
    }


def investment_review(catalysts: list[Catalyst], series: FinancialSeries, drivers: list[DriverChange],
                      evidence: list[Evidence], rationales: list[RatingRationale], bridge, valuation: dict,
                      events: list[EconomicEvent], as_of: date) -> InvestmentReview:
    rv = InvestmentReview()
    confirmed = [c for c in catalysts if c.stage == ResearchStage.CONFIRMED]
    if not confirmed:
        return rv
    rv.opened_by = [f"{c.kind.value} {_d(c.first_public_at)} ({c.catalyst_id})" for c in confirmed]
    p = series.cadence(Metric.REVENUE) or "Q"
    end = series.latest_period(Metric.REVENUE, p)
    ttm_now = series.ttm(Metric.EBITDA, end, p)[0] if end else None
    for c in confirmed:
        base, at_det = c.contribution.base_crore, c.quantities.get("ttm_ebitda_at_detection")
        if base is not None and ttm_now is not None and at_det is not None:
            realised = ttm_now - at_det
            if realised >= base:
                rv.remaining_upside.append(
                    f"{c.kind.value}: TTM EBITDA already up {realised:,.1f} cr since detection, more than this catalyst's "
                    f"base estimate ({base:,.1f} cr/yr): no remaining upside is supported by this catalyst alone - any "
                    "further upside needs a newer catalyst")
            else:
                rv.remaining_upside.append(
                    f"{c.kind.value}: base contribution {base:,.1f} cr/yr; TTM EBITDA up {realised:,.1f} cr since "
                    f"detection -> remaining ~{base - realised:,.1f} cr (before overlap with other catalysts)")
        else:
            rv.remaining_upside.append(f"{c.kind.value}: remaining upside not quantifiable "
                                       f"({c.contribution.status.replace('_', ' ')})")
    rv.valuation = valuation if valuation else {"status": "UNAVAILABLE",
                                                "reason": "no market-data adapter configured; valuation under "
                                                          "conservative assumptions cannot be computed"}
    down = next((s for s in getattr(bridge, "scenarios", []) if s.name == "downside_reported_lows"), None)
    if down is not None:
        rv.liquidity_and_downside.append(f"downside case (reported lows): recurring diluted EPS "
                                         f"{down.recurring_diluted_eps}")
    for d in drivers:
        if d.driver == "cash_conversion" and d.current is not None:
            rv.cash_and_financing.append(f"cash conversion {d.current:.2f}x ({d.basis})")
        if d.driver == "share_count_change" and d.current is not None and d.prior is not None:
            rv.cash_and_financing.append(f"diluted shares {d.prior:.3f} -> {d.current:.3f} crore")
    for r in rationales[-2:]:
        for f in r.facts:
            if f.field in ("working_capital_intensity", "dscr", "liquidity", "bank_limit_utilization",
                           "customer_concentration"):
                rv.cash_and_financing.append(f"{r.agency} {r.rationale_date}: {f.field} {f.text}"
                                             + (" (expected)" if f.expected else ""))
    if any(e.usable and e.metric == Metric.FUNDRAISE and e.available_at and _d(e.available_at) <= as_of
           for e in evidence):
        rv.cash_and_financing.append("fund-raise / warrant / preferential issue mentioned: check dilution")
    rel = [e for e in events if getattr(e.relationship, "value", "") == "confirmed_related"]
    if rel:
        rv.governance.append(f"{len(rel)} related-party order(s) (excluded from external demand)")
    for pat, label in ((r"qualified opinion|emphasis of matter|material uncertainty", "auditor qualification / "
                        "emphasis of matter mentioned"), (r"\bpledg\w*", "promoter share pledge mentioned")):
        if any(e.usable and re.search(pat, e.quote, re.I) for e in evidence):
            rv.governance.append(label)
    if not rv.governance:
        rv.governance.append("no related-party, auditor or pledge flags found in the parsed text (limited check)")
    rv.missing = [x for x, ok in (("valuation (market data)", valuation and valuation.get("status") == "COMPUTED"),
                                  ("market liquidity (traded value)", False),
                                  ("cash-flow statement", any(d.driver == "cash_conversion" for d in drivers)))
                  if not ok]
    rv.status = "inputs_incomplete" if rv.missing else "inputs_assembled"
    return rv
