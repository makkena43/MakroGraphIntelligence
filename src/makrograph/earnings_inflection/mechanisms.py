"""Mechanism-specific earnings detection (WP5).

Each mechanism is judged on the evidence and comparison it actually needs, with
its own persistence rule and invalidators.  None requires >=25% revenue growth:

  utilization          same plant / product scope only; capacity denominator must not change
  product_customer_mix segment revenue and result (or issuer-stated mix shares)
  pricing_input_costs  gross margin on a constant cost definition; cause only when stated
  order_quality        WP4-verified commitments, timing, payment terms, cancellations
  debt_reduction       net debt between balance-sheet dates, with the funding source
  segment_turnaround   comparable segment loss -> profit and its group contribution
  organic_volume_share stated volumes, acquisitions and price effects separated

Rules common to all mechanisms
* a negative development is ADVERSE (direction "negative"), never a large positive change;
* small changes are NO_MATERIAL_CHANGE, missing comparisons INSUFFICIENT_DATA;
* management statements alone are ASSERTION; correlation is never reported as cause;
* mechanisms that describe the same earnings change are cross-referenced (``overlaps_with``)
  so contributions are not added up;
* when a magnitude cannot be quantified, a research hypothesis is stated instead.
States are evidence descriptions, not decisions.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from .contracts import (
    DriverChange, EconomicEvent, Evidence, Mechanism, MechanismResult, MechanismState, Metric, Modality, Unit,
)
from .demand import summarise_demand
from .financial_series import FinancialSeries, _year_ago, prev_period_end

DEFAULT_MECHANISM_THRESHOLDS = {
    "utilization_change_pp": 10.0,
    "gross_margin_change_bps": 150.0,
    "mix_share_change_pp": 3.0,
    "mix_min_segment_share_pct": 10.0,
    "mix_margin_gap_pp": 2.0,
    "volume_growth_pct": 10.0,
    "debt_reduction_pct": 10.0,
    "order_cancellation_share": 0.2,
    "statement_max_age_days": 200,
}

S = MechanismState
REALIZED_UPGRADING = (Mechanism.PRICING_INPUT, Mechanism.PRODUCT_MIX, Mechanism.SEGMENT_TURNAROUND,
                      Mechanism.ORGANIC_VOLUME)


def _d(x) -> Optional[date]:
    if x is None:
        return None
    return x.date() if isinstance(x, datetime) else x


def _realized(evidence: list[Evidence], metric: Metric) -> list[Evidence]:
    return sorted((e for e in evidence if e.usable and e.metric == metric and e.modality == Modality.REALIZED),
                  key=lambda e: (e.available_at or datetime.min, e.evidence_id))


def _cue(evidence: list[Evidence], pattern: str) -> list[Evidence]:
    rx = re.compile(pattern, re.I)
    return [e for e in evidence if e.usable and rx.search(e.quote)]


def _times(points) -> Optional[datetime]:
    ts = [p.available_at for p in points if p is not None and p.available_at is not None]
    return max(ts) if ts else None


def detect_mechanisms(series: FinancialSeries, evidence: list[Evidence], events: list[EconomicEvent],
                      drivers: list[DriverChange], as_of_date: date,
                      thresholds: Optional[dict] = None) -> list[MechanismResult]:
    th = {**DEFAULT_MECHANISM_THRESHOLDS, **(thresholds or {})}
    p, end, stale = series.current_period(as_of_date)
    if stale:
        # later results should be public but did not parse: keep the last known reading,
        # clearly marked, instead of dropping the thesis (it never upgrades the status)
        end = series.latest_period(Metric.REVENUE, p) if p else None
    out = [
        utilization(series, evidence, p, end, as_of_date, th),
        product_mix(series, evidence, p, end, as_of_date, th),
        pricing_input(series, evidence, p, end, as_of_date, th),
        order_quality(series, evidence, events, drivers, p, end, as_of_date, th),
        debt_reduction(series, evidence, as_of_date, th),
        segment_turnaround(series, evidence, p, end, th),
        organic_volume(series, evidence, drivers, p, end, as_of_date, th),
    ]
    if stale:
        for r in out:
            if r.mechanism != Mechanism.DEBT_REDUCTION:      # judged on balance-sheet dates, own staleness rule
                r.stale = True
                r.notes.append(f"stale: {stale}")
    _mark_overlaps(out, drivers)
    return out


def _mark_overlaps(results: list[MechanismResult], drivers: list[DriverChange]) -> None:
    """Utilization, pricing/input costs and mix can each explain the same margin change:
    cross-reference them so their contributions are never added together."""
    margin_mechs = [r for r in results if r.qualifies_positive and r.mechanism in
                    (Mechanism.UTILIZATION, Mechanism.PRICING_INPUT, Mechanism.PRODUCT_MIX)]
    for r in margin_mechs:
        r.overlaps_with = sorted({x.mechanism.value for x in margin_mechs if x is not r})
    lev = next((d for d in drivers if d.driver == "operating_leverage" and d.material), None)
    if lev:
        for r in margin_mechs:
            r.overlaps_with.append("operating_leverage driver (same EBITDA margin change)")


# --- 1. utilization ------------------------------------------------------------

def utilization(series, evidence, p, end, as_of_date, th) -> MechanismResult:
    stated = [e for e in _realized(evidence, Metric.UTILIZATION) if e.quantity and e.quantity.unit == Unit.PERCENT]
    r = MechanismResult(Mechanism.UTILIZATION, S.INSUFFICIENT_DATA, confidence="low",
                        invalidators=["management-stated utilisation; capacity denominator not verified"])
    if not stated:
        r.notes.append("no stated capacity utilisation")
        return r
    by_scope: dict[str, list[Evidence]] = {}
    for e in stated:
        by_scope.setdefault(e.facility or "company-level", []).append(e)
    def per_filing(v):
        """One statement per filing (a call can quote several historic levels); the first one."""
        out, seen = [], set()
        for x in v:
            if x.doc_id not in seen:
                seen.add(x.doc_id)
                out.append(x)
        # successive statements at least 45 days apart
        kept = []
        for x in out:
            if not kept or (x.available_at - kept[-1].available_at).days >= 45:
                kept.append(x)
        return kept
    by_scope = {k: per_filing(v) for k, v in by_scope.items()}
    comparable = {k: v for k, v in by_scope.items() if len(v) >= 2
                  and (as_of_date - _d(v[-1].available_at)).days <= 400}
    if not comparable:
        if len(by_scope) > 1:
            r.invalidators.append("utilisation stated for different plants/products ("
                                  + ", ".join(sorted(by_scope)) + "); different scopes are never compared")
        r.notes.append("no plant/product with two utilisation statements 45+ days apart, the latest within 400 days")
        r.evidence_ids = [e.evidence_id for e in stated]
        return r
    scope, seq = max(comparable.items(), key=lambda kv: abs(kv[1][-1].quantity.value - kv[1][0].quantity.value))
    a, b = seq[-2], seq[-1]
    delta = b.quantity.value - a.quantity.value
    r.magnitude, r.magnitude_unit = delta, "pp"
    r.magnitude_basis = f"stated utilisation at {scope}, {_d(a.available_at)} -> {_d(b.available_at)}"
    r.evidence_ids, r.source_doc_ids = [e.evidence_id for e in seq], sorted({e.doc_id for e in seq})
    r.first_signal_at = b.available_at
    r.attribution = "issuer-stated utilisation (same plant/product scope)"
    cap = [e for e in _realized(evidence, Metric.CAPACITY) if (e.facility or "company-level") == scope
           and e.quantity and a.available_at and b.available_at and e.available_at
           and a.available_at <= e.available_at <= b.available_at]
    cap_all = [e for e in _realized(evidence, Metric.CAPACITY) if (e.facility or "company-level") == scope and e.quantity]
    if len({round(e.quantity.value, 3) for e in cap_all}) > 1 or cap:
        r.state = S.ASSERTION
        r.invalidators.append("capacity changed between the statements: utilisation percentages are not comparable")
        return r
    if _cue(evidence, r"\b(?:qualification|validation|approval|homologation)\b.{0,40}\b(?:pending|awaited|delay)"):
        r.invalidators.append("customer/regulatory qualification pending or delayed")
    if [e for e in evidence if e.usable and e.metric == Metric.CAPEX]:
        r.invalidators.append("new capacity adds depreciation and interest before it is utilised")
    if delta >= th["utilization_change_pp"]:
        r.direction, r.state = "positive", S.EMERGING
        rising = all(y.quantity.value >= x.quantity.value for x, y in zip(seq, seq[1:]))
        rev_g = series.yoy(Metric.REVENUE, end, p) if end else None
        if len(seq) >= 3 and rising and rev_g is not None and rev_g > 0:
            r.state, r.confidence = S.CONFIRMED, "medium"
            r.durability = f"{len(seq)} rising statements; revenue up {rev_g:.1f}% YoY (independent realized milestone)"
        else:
            r.durability = "one comparable pair of statements"
    elif delta <= -th["utilization_change_pp"]:
        r.direction, r.state, r.durability = "negative", S.ADVERSE, "latest comparable statement"
    else:
        r.state = S.NO_MATERIAL_CHANGE
    return r


# --- 2. product / customer mix ----------------------------------------------------

_SEG_TOTAL_NAME = re.compile(r"^(?:net\s+|gross\s+|total\s+)?(?:revenue|income|sales|turnover)\b|from\s+operations|"
                             r"^total\b|^(?:net\s+)?income$", re.I)


def _seg_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def segment_groups(series, p) -> dict[str, list[str]]:
    """Reported segment names grouped into segments: company totals are not segments, and spellings
    of one segment (scanned "insulator div sion", "c-bus" for "e-bus") are merged when they are
    near-identical AND never reported side by side in the same period (then they are different)."""
    import difflib
    names = [n for n in series.segment_names() if not _SEG_TOTAL_NAME.search(n.strip())]
    periods = {n: {k[3] for k in series.segments if k[0] == n and k[2] == p} for n in names}
    def same(a, b) -> bool:
        r = difflib.SequenceMatcher(None, _seg_key(a), _seg_key(b)).ratio()
        shared = periods[a] & periods[b]
        if not shared:
            return r >= 0.88                         # one segment re-spelt across filings
        if r < 0.93:
            return False                             # reported side by side: different segments
        for t in shared:                             # scanned copies of one table: same figures
            va = series.segment(a, Metric.SEGMENT_REVENUE, t, p) or series.segment(a, Metric.SEGMENT_RESULT, t, p)
            vb = series.segment(b, Metric.SEGMENT_REVENUE, t, p) or series.segment(b, Metric.SEGMENT_RESULT, t, p)
            if va and vb and abs(va.value - vb.value) > 0.01 * max(abs(va.value), abs(vb.value), 1e-9):
                return False
        return True
    groups: list[list[str]] = []
    for n in sorted(names, key=lambda x: -len(periods[x])):
        g = next((g for g in groups if all(same(n, m) for m in g)), None)
        if g is None:
            groups.append([n])
        else:
            g.append(n)
    return {g[0]: g for g in groups}


def _seg_value(series, names, metric, end, p):
    vals = [series.segment(n, metric, end, p) for n in names]
    vals = [v for v in vals if v is not None]
    return vals[0] if vals else None


def _segments_reconcile(series, p, t, groups) -> Optional[bool]:
    """Reported segment revenues add up to the company's reported revenue for period ``t`` (5%).
    None when no segment revenue is reported (nothing to check against)."""
    company = series.get(Metric.REVENUE, t, p)
    vals = [_seg_value(series, names, Metric.SEGMENT_REVENUE, t, p) for names in groups.values()]
    tot = sum(v.value for v in vals if v is not None and v.value > 0)
    if not tot:
        return None
    return bool(company and company.value > 0 and abs(tot - company.value) <= 0.05 * company.value)


def _segment_mix(series, p, end):
    """{segment: (share_now, share_ya, margin_now, margin_ya, rev_now)} for segments reported in both periods.
    Shares are used only when the segment table reconciles: the same segments are reported in both
    periods and their revenues add up to the company's reported revenue (within 5%) - otherwise a
    mis-read or re-labelled table would show as a mix shift."""
    if end is None:
        return {}, None
    ya = _year_ago(end)
    groups = segment_groups(series, p)
    rows = {}
    present = {end: 0.0, ya: 0.0}
    for n, names in groups.items():
        rn, ry = (_seg_value(series, names, Metric.SEGMENT_REVENUE, end, p),
                  _seg_value(series, names, Metric.SEGMENT_REVENUE, ya, p))
        xn, xy = (_seg_value(series, names, Metric.SEGMENT_RESULT, end, p),
                  _seg_value(series, names, Metric.SEGMENT_RESULT, ya, p))
        for t, v in ((end, rn), (ya, ry)):
            if v is not None and v.value > 0:
                present[t] += v.value
        if rn and ry and xn and xy and rn.value > 0 and ry.value > 0:
            rows[n] = (rn.value, ry.value, xn.value, xy.value)
    if len(rows) < 2:
        return {}, None
    for t, idx in ((end, 0), (ya, 1)):
        company = series.get(Metric.REVENUE, t, p)
        used = sum(v[idx] for v in rows.values())
        if company is None or company.value <= 0 or abs(used - company.value) > 0.05 * company.value \
                or abs(present[t] - used) > 0.05 * present[t]:
            return {}, None          # segments do not reconcile with reported revenue: no mix reading
    tot_n, tot_y = sum(v[0] for v in rows.values()), sum(v[1] for v in rows.values())
    out = {n: (v[0] / tot_n * 100, v[1] / tot_y * 100, v[2] / v[0] * 100, v[3] / v[1] * 100, v[0])
           for n, v in rows.items()}
    return out, (tot_n, tot_y, rows)


def product_mix(series, evidence, p, end, as_of_date, th) -> MechanismResult:
    r = MechanismResult(Mechanism.PRODUCT_MIX, S.INSUFFICIENT_DATA)
    mix, totals = _segment_mix(series, p, end)
    if mix:
        tot_n, tot_y, rows = totals

        def rest_margin(name, idx_res, idx_rev):
            o = [v for k, v in rows.items() if k != name]
            rev = sum(v[idx_rev] for v in o)
            return sum(v[idx_res] for v in o) / rev * 100 if rev else 0.0
        best = None
        for n, (sh_n, sh_y, m_n, m_y, rev_n) in mix.items():
            gap = m_y - rest_margin(n, 3, 1)
            if gap < th["mix_margin_gap_pp"]:
                continue                                   # not the more profitable segment
            shift = sh_n - sh_y
            contrib = shift / 100 * tot_n * gap / 100      # crore, at year-ago margins
            if best is None or abs(shift) > abs(best[1]):
                best = (n, shift, gap, contrib, sh_n, sh_y, m_n)
        r.source_doc_ids = sorted({d for k, pt in series.segments.items() for d in pt.doc_ids})
        groups = segment_groups(series, p)
        r.first_signal_at = _times(_seg_value(series, groups[n], Metric.SEGMENT_REVENUE, end, p) for n in mix)
        r.attribution = "observed segment revenue and segment result (reported segment table)"
        r.confidence = "medium"
        if best is None:
            r.state = S.NO_MATERIAL_CHANGE
            r.notes.append("no reported segment is materially more profitable than the rest")
            return r
        n, shift, gap, contrib, sh_n, sh_y, m_n = best
        r.magnitude, r.magnitude_unit = contrib, "crore segment result"
        r.magnitude_basis = (f"{n}: share of segment revenue {sh_y:.1f}% -> {sh_n:.1f}% (margin gap {gap:.1f} pp vs "
                             f"other segments, year-ago margins; before unallocable costs)")
        r.period_end = end
        if shift >= th["mix_share_change_pp"]:
            r.direction, r.state, r.durability = "positive", S.EMERGING, "one period"
            prev = prev_period_end(end, p)
            pm, _ = _segment_mix(series, p, prev)
            if n in pm and pm[n][0] - pm[n][1] >= th["mix_share_change_pp"]:
                r.state, r.durability = S.CONFIRMED, "share gain in two consecutive periods"
            if sh_n < th["mix_min_segment_share_pct"]:
                r.direction = "neutral"
                r.invalidators.append(f"profitable segment is {sh_n:.1f}% of revenue: too small to move group "
                                      "earnings")
        elif shift <= -th["mix_share_change_pp"]:
            r.direction, r.state = "negative", S.ADVERSE
            r.durability = "latest period"
        else:
            r.state = S.NO_MATERIAL_CHANGE
        if _cue(evidence, r"\b(?:development|NRE|prototype|pilot|sampling)\s+(?:revenue|income|orders?)\b"):
            r.invalidators.append("development / NRE revenue may be mistaken for repeat production")
        return r
    # issuer-stated mix share ("LGD jewellery contributed 51.3% ... compared to 23.5%")
    stated = [e for e in _realized(evidence, Metric.MIX_SHARE) if e.quantity
              and _d(e.available_at) and (as_of_date - _d(e.available_at)).days <= th["statement_max_age_days"]]
    if stated:
        e = stated[-1]
        pcts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", e.quote)]
        r.state, r.confidence = S.ASSERTION, "low"
        r.evidence_ids, r.source_doc_ids, r.first_signal_at = [x.evidence_id for x in stated], sorted(
            {x.doc_id for x in stated}), e.available_at
        r.attribution = "issuer-stated product/customer mix"
        r.hypothesis = (f"Does the rising share of {e.segment or 'the product'} ("
                        + (f"{pcts[1]:g}% -> {pcts[0]:g}%" if len(pcts) >= 2 else f"{pcts[0]:g}%")
                        + ") carry a higher margin? Margin by product is not disclosed, so the earnings effect is "
                        "unknown.")
        r.invalidators.append("no segment profit disclosed: mix contribution to earnings cannot be quantified")
        if len(pcts) >= 2 and pcts[0] > pcts[1]:
            r.direction = "positive"
        if _cue(stated, r"\b(?:development|NRE|prototype|pilot|sampling)\b"):
            r.invalidators.append("development / NRE revenue may be mistaken for repeat production")
        return r
    r.notes.append("no segment table with revenue and result in comparable periods; no stated mix shares")
    return r


# --- 3. pricing / input costs -------------------------------------------------------

def _usual_cost_parts(series, p) -> tuple:
    """The company's usual cost-of-goods definition: the components reported in at least half of
    the periods that have revenue (a period missing one of them is not comparable)."""
    if p is None:
        return ()
    ends = [e for (m, t, e) in series.points if m == Metric.REVENUE and t == p]
    if not ends:
        return ()
    parts = tuple(m for m in (Metric.COST_OF_MATERIALS, Metric.PURCHASES_STOCK, Metric.INVENTORY_CHANGE)
                  if sum(1 for e in ends if series.get(m, e, p) is not None) >= 0.5 * len(ends))
    return parts if (Metric.COST_OF_MATERIALS in parts or Metric.PURCHASES_STOCK in parts) else ()


def pricing_input(series, evidence, p, end, as_of_date, th) -> MechanismResult:
    r = MechanismResult(Mechanism.PRICING_INPUT, S.INSUFFICIENT_DATA)
    recent = lambda m: [e for e in _realized(evidence, m) if _d(e.available_at)  # noqa: E731
                        and (as_of_date - _d(e.available_at)).days <= th["statement_max_age_days"] * 2]
    price_up = [e for e in recent(Metric.PRICING) if e.direction > 0]
    price_dn = [e for e in recent(Metric.PRICING) if e.direction < 0]
    cost_dn = [e for e in recent(Metric.INPUT_COST) if e.direction < 0]
    cost_up = [e for e in recent(Metric.INPUT_COST) if e.direction > 0]
    parts = _usual_cost_parts(series, p)
    gm = series.gross_margin(end, p, parts) if end and parts else (None, ())
    if gm[0] is None:
        if price_up or price_dn or cost_dn or cost_up:
            r.state, r.confidence = S.ASSERTION, "low"
            r.evidence_ids = [e.evidence_id for e in price_up + price_dn + cost_dn + cost_up]
            r.hypothesis = "Issuer comments on pricing/input costs, but no cost-of-goods rows to measure gross margin."
        else:
            r.notes.append("cost-of-goods rows (materials / purchases / inventory change) not reported on the company's "
                           "usual definition for the latest period")
        return r
    now, _ = gm
    ya, _ = series.gross_margin(_year_ago(end), p, parts)
    if ya is None:
        r.notes.append("year-ago gross margin not available on the same cost definition")
        return r
    bps = (now - ya) * 100
    rev = series.get(Metric.REVENUE, end, p)
    r.magnitude, r.magnitude_unit = bps, "bps gross margin"
    r.magnitude_basis = (f"(revenue - {' - '.join(m.value for m in parts)}) / revenue: {ya:.1f}% -> {now:.1f}% vs "
                         f"year-ago period; ~{bps / 10000 * rev.value:,.1f} cr on current revenue")
    r.period_end, r.confidence = end, "medium"
    keys = [series.get(m, d, p) for m in (Metric.REVENUE, *parts) for d in (end, _year_ago(end))]
    r.first_signal_at = _times(keys)
    r.source_doc_ids = sorted({d for k in keys if k for d in k.doc_ids})
    stated = []
    if price_up:
        stated.append("issuer states higher realisations / prices")
    if cost_dn:
        stated.append("issuer states lower input costs")
    if price_dn:
        stated.append("issuer states lower realisations / price cuts")
    if cost_up:
        stated.append("issuer states higher input costs")
    r.attribution = ("; ".join(stated) if stated else
                     "unattributed: gross-margin change observed; the cause is not established from margins alone")
    r.evidence_ids = [e.evidence_id for e in price_up + cost_dn + price_dn + cost_up]
    if bps >= th["gross_margin_change_bps"]:
        r.direction, r.state, r.durability = "positive", S.EMERGING, "one period"
        prev = prev_period_end(end, p)
        pn, _ = series.gross_margin(prev, p, parts)
        py, _ = series.gross_margin(_year_ago(prev), p, parts)
        if pn is not None and py is not None and (pn - py) * 100 >= th["gross_margin_change_bps"]:
            r.state, r.durability = S.CONFIRMED, "gross-margin gain in two consecutive periods"
        if cost_dn and not price_up:
            r.invalidators.append("input-cost tailwind can reverse with commodity prices")
        if price_dn:
            r.invalidators.append("price cuts stated in the period")
    elif bps <= -th["gross_margin_change_bps"]:
        r.direction, r.state, r.durability = "negative", S.ADVERSE, "latest period"
    else:
        r.state = S.NO_MATERIAL_CHANGE
    return r


# --- 4. order quality ------------------------------------------------------------------

def order_quality(series, evidence, events, drivers, p, end, as_of_date, th) -> MechanismResult:
    r = MechanismResult(Mechanism.ORDER_QUALITY, S.INSUFFICIENT_DATA)
    if not events:
        r.notes.append("no disclosed order events")
        return r
    dem = summarise_demand(events, evidence, as_of_date)
    ttm = series.ttm(Metric.REVENUE, end, p)[0] if end else None
    verified = [e for e in events if e.event_id in dem.verified_events]
    r.evidence_ids = [i for e in events for i in e.evidence_ids]
    r.source_doc_ids = sorted({d for e in events for d in e.doc_ids})
    r.attribution = "WP4-qualified order events (deduplicated, dated states)"
    if dem.cancellations_crore and dem.cancellations_crore >= th["order_cancellation_share"] * max(
            dem.verified_inflow_crore + dem.cancellations_crore, 1e-9):
        r.direction, r.state = "negative", S.ADVERSE
        r.magnitude, r.magnitude_unit = dem.cancellations_crore, "crore cancelled"
        r.invalidators.append("cancellations are a large share of disclosed inflow")
        return r
    if verified:
        r.magnitude, r.magnitude_unit = dem.verified_inflow_crore, "crore verified inflow (12 months)"
        r.magnitude_basis = "current value after amendments/cancellations; headline order value is not annual revenue"
        r.first_signal_at = min((e.first_public_at for e in verified if e.first_public_at), default=None)
        material = bool(ttm) and dem.verified_inflow_crore >= 0.25 * ttm
        r.state = S.COMMITMENT if material else S.NO_MATERIAL_CHANGE
        r.direction = "positive" if material else "neutral"
        r.confidence = "medium"
        if not ttm:
            r.state, r.direction = S.COMMITMENT, "neutral"
            r.invalidators.append("no current revenue base: materiality unknown")
        rev_g = next((d for d in drivers if d.driver == "revenue_yoy_growth"), None)
        streak = next((d for d in drivers if d.driver == "material_growth_streak"), None)
        if material and rev_g and rev_g.material:
            r.state, r.durability = S.EMERGING, "orders plus material revenue growth in the latest period"
            if streak and streak.current and streak.current >= 2:
                r.state, r.durability = S.CONFIRMED, "orders plus two consecutive periods of material growth"
        if any(e.duration_months is None for e in verified):
            r.invalidators.append("execution period not disclosed for some orders: revenue timing unknown")
        if not any(e.payment_terms for e in verified):
            r.invalidators.append("payment terms not disclosed: working-capital burden unknown")
        r.invalidators.append("order margins are not disclosed")
        if dem.tax_inclusive_events:
            r.invalidators.append("some order values include taxes")
    elif dem.unverified_events:
        r.state, r.confidence = S.ASSERTION, "low"
        r.magnitude, r.magnitude_unit = dem.unverified_inflow_crore, "crore unverified (early lane)"
        r.invalidators += sorted({x for rs in dem.unverified_events.values() for x in rs})
    if dem.related_party_excluded:
        r.invalidators.append(f"{len(dem.related_party_excluded)} related-party order(s) excluded")
    return r


# --- 5. debt reduction -------------------------------------------------------------------

def _net_debt(series, d):
    parts = [series.get(m, d, "I") for m in (Metric.BORROWINGS_NONCURRENT, Metric.BORROWINGS_CURRENT)]
    if all(x is None for x in parts):
        return None, None
    gross = sum(x.value for x in parts if x)
    cash = series.get(Metric.CASH, d, "I")
    return gross - (cash.value if cash else 0.0), gross


def _flows_between(series, metric, a, b) -> Optional[float]:
    """Cash-flow amount for the interval (a, b]: full years if reported, else half-years,
    else the longest year-to-date figure.  None when nothing is reported."""
    pts = {(t, e): pt.value for (m, t, e), pt in series.points.items() if m == metric and a < e <= b}
    for t in ("FY", "H"):
        vals = [v for (tt, _), v in pts.items() if tt == t]
        if vals:
            return sum(vals)
    for t in ("9M", "Q"):
        vals = [v for (tt, e), v in sorted(pts.items(), key=lambda kv: kv[0][1]) if tt == t]
        if vals:
            return vals[-1] if t == "9M" else sum(vals)
    return None


def debt_reduction(series, evidence, as_of_date, th) -> MechanismResult:
    r = MechanismResult(Mechanism.DEBT_REDUCTION, S.INSUFFICIENT_DATA)
    dates = sorted({e for (m, t, e) in series.points if t == "I" and m in
                    (Metric.BORROWINGS_CURRENT, Metric.BORROWINGS_NONCURRENT)})
    if len(dates) < 2:
        r.notes.append("fewer than two balance-sheet dates with borrowings")
        return r
    b = dates[-1]
    if (as_of_date - b).days > 270:
        r.notes.append(f"latest balance sheet with borrowings is {b} ({(as_of_date - b).days} days old): stale")
        return r
    same_season = [d for d in dates if d < b and (d.month, abs(d.year - b.year)) == (b.month, 1)]
    a = same_season[-1] if same_season else dates[-2]
    if not same_season:
        r.notes.append("compared with the previous balance-sheet date (not the same season): seasonal cash possible")
    na, ga = _net_debt(series, a)
    nb, gb = _net_debt(series, b)
    base = max(abs(na), ga or 0.0, 1e-9)
    change = nb - na
    pct = -change / base * 100
    r.magnitude, r.magnitude_unit = -change, "crore net debt reduction"
    r.magnitude_basis = f"net debt (borrowings - cash) {a}: {na:,.1f} -> {b}: {nb:,.1f}"
    r.period_end = b
    keys = [series.get(m, d, "I") for m in (Metric.BORROWINGS_CURRENT, Metric.BORROWINGS_NONCURRENT, Metric.CASH)
            for d in (a, b)]
    r.first_signal_at = _times(keys)
    r.source_doc_ids = sorted({d for k in keys if k for d in k.doc_ids})
    if abs(pct) < th["debt_reduction_pct"]:
        r.state = S.NO_MATERIAL_CHANGE
        return r
    if change > 0:
        r.direction, r.state, r.confidence = "negative", S.ADVERSE, "medium"
        r.attribution = "net debt rose"
        return r
    equity = _flows_between(series, Metric.EQUITY_ISSUED, a, b) or 0.0
    ocf = _flows_between(series, Metric.OPERATING_CASH_FLOW, a, b)
    sa, sb = series.shares_from_capital(a, "I"), series.shares_from_capital(b, "I")
    diluted = bool(sa and sb and sb > sa * 1.005)
    asset_sale = _cue([e for e in evidence if e.available_at and a <= _d(e.available_at) <= b + timedelta(days=60)],
                      r"\b(?:sale|divest\w*|monetis\w*|monetiz\w*)\s+of\s+(?:land|assets?|investments?|stake|"
                      r"division|business|undertaking|property)")
    reduction = -change
    r.state, r.durability = S.EMERGING, "one balance-sheet interval"
    if equity >= 0.5 * reduction or diluted:
        r.direction = "neutral"
        r.attribution = (f"equity-funded: {equity:,.1f} cr raised from share/warrant issuance" if equity
                         else "share count rose over the interval: likely equity-funded")
        r.invalidators.append("repayment funded by equity issuance: dilution offsets interest savings; "
                              "not an operating improvement")
        r.confidence = "medium"
    elif asset_sale:
        r.direction = "neutral"
        r.attribution = "asset sale / divestment stated in the interval"
        r.invalidators.append("repayment funded by an asset sale is not recurring operating cash")
        r.evidence_ids = [e.evidence_id for e in asset_sale]
    elif ocf is not None and ocf >= 0.5 * reduction:
        r.direction, r.confidence = "positive", "medium"
        r.attribution = f"operating cash flow {ocf:,.1f} cr over the interval funds the reduction"
        if len(dates) >= 3:
            a0 = dates[-3] if a == dates[-2] else None
            if a0 is not None:
                n0, _ = _net_debt(series, a0)
                if n0 is not None and na < n0 * (1 - th["debt_reduction_pct"] / 100):
                    r.state, r.durability = S.CONFIRMED, "net debt fell over two consecutive intervals"
    else:
        r.direction = "positive" if ocf is None else "neutral"
        r.attribution = ("funding source not established (no cash-flow statement in the interval)" if ocf is None
                         else f"operating cash flow {ocf:,.1f} cr is less than half the reduction")
        r.confidence = "low"
    q_ends = sorted(e for (m, t, e) in series.points if m == Metric.FINANCE_COST and t == "Q")
    fc = series.yoy(Metric.FINANCE_COST, q_ends[-1], "Q") if q_ends else None
    if fc is not None:
        r.notes.append(f"finance cost {fc:+.1f}% YoY in the latest quarter")
    return r


# --- 6. segment turnaround -----------------------------------------------------------------

def segment_turnaround(series, evidence, p, end, th) -> MechanismResult:
    r = MechanismResult(Mechanism.SEGMENT_TURNAROUND, S.INSUFFICIENT_DATA)
    if end is None or not series.segment_names():
        r.notes.append("no comparable segment results")
        return r
    ya = _year_ago(end)
    cands = []
    groups = segment_groups(series, p)
    checks = (_segments_reconcile(series, p, end, groups), _segments_reconcile(series, p, ya, groups))
    if False in checks:
        r.notes.append("segment table does not reconcile with reported revenue: segment results not used")
        return r
    unchecked = None in checks
    for n, names in groups.items():
        now, prior = (_seg_value(series, names, Metric.SEGMENT_RESULT, end, p),
                      _seg_value(series, names, Metric.SEGMENT_RESULT, ya, p))
        if now is None or prior is None:
            continue
        cands.append((n, now.value, prior.value, now, prior))
    if not cands:
        r.notes.append("segment results not reported for both the latest and year-ago periods")
        return r
    pbt = series.get(Metric.PBT, end, p)
    turn = [c for c in cands if c[2] < 0 < c[1]]
    worse = [c for c in cands if c[2] > 0 > c[1]]
    r.confidence = "low" if unchecked else "medium"
    r.attribution = "observed segment result (reported segment table)"
    if unchecked:
        r.notes.append("segment revenue not reported: the segment table could not be reconciled")
    pick = max(turn, key=lambda c: c[1] - c[2]) if turn else (max(worse, key=lambda c: c[2] - c[1]) if worse else None)
    if pick is None:
        r.state = S.NO_MATERIAL_CHANGE
        return r
    n, now_v, prior_v, now, prior = pick
    r.magnitude, r.magnitude_unit = now_v - prior_v, "crore segment result change"
    share = f"; {abs(now_v - prior_v) / abs(pbt.value) * 100:.0f}% of group PBT" if pbt and pbt.value else ""
    r.magnitude_basis = f"{n}: segment result {prior_v:,.1f} -> {now_v:,.1f} cr vs year-ago period{share}"
    r.period_end, r.first_signal_at = end, _times([now, prior])
    r.source_doc_ids = sorted(set(now.doc_ids) | set(prior.doc_ids))
    if turn:
        r.direction, r.state, r.durability = "positive", S.EMERGING, "one period of profit after a loss"
        prev = prev_period_end(end, p)
        pn, py = (_seg_value(series, groups[n], Metric.SEGMENT_RESULT, prev, p),
                  _seg_value(series, groups[n], Metric.SEGMENT_RESULT, _year_ago(prev), p))
        if pn and pn.value > 0 and py and py.value < 0:
            r.state, r.durability = S.CONFIRMED, "segment profitable in two consecutive periods after losses"
    else:
        r.direction, r.state, r.durability = "negative", S.ADVERSE, "segment swung to a loss"
    ex = series.get(Metric.EXCEPTIONAL_ITEMS, end, p)
    if ex and abs(ex.value) > 0:
        r.invalidators.append("group exceptional items in the period: check whether the segment result includes "
                              "one-offs")
    if _cue(evidence, r"\brestructur\w*|\bvoluntary retirement|\bVRS\b"):
        r.invalidators.append("restructuring costs mentioned: segment comparisons may include one-offs")
    names_now = {k[0] for k in series.segments if k[2] == p and k[3] == end}
    names_ya = {k[0] for k in series.segments if k[2] == p and k[3] == ya}
    if names_now != names_ya:
        r.invalidators.append("segment composition changed between periods: comparability limited")
    return r


# --- 7. organic volume / share ---------------------------------------------------------------

def organic_volume(series, evidence, drivers, p, end, as_of_date, th) -> MechanismResult:
    r = MechanismResult(Mechanism.ORGANIC_VOLUME, S.INSUFFICIENT_DATA)
    vols = [e for e in _realized(evidence, Metric.VOLUME) if e.quantity and e.quantity.unit == Unit.PERCENT
            and e.direction != 0 and _d(e.available_at)
            and (as_of_date - _d(e.available_at)).days <= th["statement_max_age_days"] * 2]
    acq = [e for e in _realized(evidence, Metric.ACQUISITION) if _d(e.available_at)
           and (as_of_date - _d(e.available_at)).days <= 460]
    share_claims = [e for e in evidence if e.usable and e.metric == Metric.MARKET_SHARE]
    price_up = [e for e in _realized(evidence, Metric.PRICING) if e.direction > 0]
    if acq:
        r.invalidators.append("acquisition in the last 15 months: part of revenue growth may be inorganic")
    if share_claims:
        r.invalidators.append("market-share claims without a market denominator are assertions only")
    if not vols:
        rev_g = next((d for d in drivers if d.driver == "revenue_yoy_growth" and d.material), None)
        if rev_g:
            r.hypothesis = (f"Revenue grew {rev_g.change:.1f}% YoY without disclosed volumes: how much is volume "
                            "vs price" + (" vs acquisition" if acq else "") + "?")
        if share_claims:
            r.state, r.evidence_ids = S.ASSERTION, [e.evidence_id for e in share_claims]
        else:
            r.notes.append("no quantified volume statements")
        return r
    latest = vols[-1]
    growth = latest.quantity.value * latest.direction if latest.quantity.value > 0 else latest.quantity.value
    r.magnitude, r.magnitude_unit = growth, "% stated volume growth"
    r.magnitude_basis = "issuer-stated volume change (units / tonnes / shipments)"
    r.first_signal_at, r.evidence_ids = latest.available_at, [e.evidence_id for e in vols]
    r.source_doc_ids = sorted({e.doc_id for e in vols})
    r.attribution = "issuer-stated volumes (realized)"
    r.confidence = "low" if acq else "medium"
    if growth >= th["volume_growth_pct"]:
        r.direction, r.state, r.durability = "positive", S.EMERGING, "one statement"
        earlier = [e for e in vols[:-1] if (e.quantity.value * e.direction) >= th["volume_growth_pct"]
                   and latest.available_at and e.available_at and (latest.available_at - e.available_at).days >= 60]
        if earlier:
            r.state, r.durability = S.CONFIRMED, "volume growth stated in two separate periods"
        if acq:
            r.direction = "neutral"
    elif growth <= -th["volume_growth_pct"]:
        r.direction, r.state, r.durability = "negative", S.ADVERSE, "latest statement"
    else:
        r.state = S.NO_MATERIAL_CHANGE
    if price_up and r.state in (S.EMERGING, S.CONFIRMED):
        r.notes.append("price increases also stated: revenue growth is part price, part volume")
    return r
