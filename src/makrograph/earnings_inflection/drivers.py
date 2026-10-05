"""Deterministic candidate driver calculations.

Thresholds come from config and only flag a change as *material* for
disclosure; they are descriptive, not decision rules.  No price or
share-price-momentum input exists here: a prior rally never rejects a
business and is not a driver.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from .contracts import IST, DriverChange, EconomicEvent, Evidence, IssuerModel, Metric, Unit
from .demand import summarise_demand
from .financial_series import PERIOD_WORD, FinancialSeries, _year_ago, prev_period_end

DEFAULT_THRESHOLDS = {
    "revenue_yoy_pct": 25.0,
    "revenue_yoy_acceleration_pp": 10.0,
    "ebitda_margin_change_bps": 200.0,
    "pat_yoy_pct": 30.0,
    "book_to_bill": 1.2,
    "order_book_cover_years": 2.0,
    "utilization_change_pp": 10.0,
}


def _dc(driver, s: FinancialSeries, end, cur, prior, unit, basis, material, docs=(), notes=None, change=None,
        keys=(), extra=()):
    """A driver with provenance: ``keys`` are the series points used, ``extra`` are
    (public_time, system_time, doc_id) of non-series inputs (events, statements)."""
    if change is None and cur is not None and prior is not None:
        change = cur - prior
    know, sysk, kdocs = s.provenance(keys) if keys else (None, None, [])
    pub_times = ([know] if keys else []) + [x[0] for x in extra]
    sys_times = ([sysk] if keys else []) + [x[1] for x in extra]
    knowable = None if (not pub_times or any(t is None for t in pub_times)) else max(pub_times)
    system = None if (not sys_times or any(t is None for t in sys_times)) else max(sys_times)
    all_docs = sorted(set(docs) | set(kdocs) | {x[2] for x in extra})
    return DriverChange(driver=driver, ticker=s.ticker, period_end=end, current=cur, prior=prior, change=change,
                        unit=unit, basis=basis, material=material, source_doc_ids=all_docs,
                        notes=list(notes or []), knowable_at=knowable, system_known_at=system)


def _ttm_ends(end, p):
    out, d = [], end
    for _ in range(4 if p == "Q" else 2):
        out.append(d)
        d = prev_period_end(d, p)
    return out


def _keys(metric, p, *ends):
    return [(metric, p, e) for e in ends]


def compute_drivers(series: FinancialSeries, events: list[EconomicEvent], evidence: list[Evidence],
                    issuer_model: IssuerModel, as_of_date: date, thresholds: Optional[dict] = None
                    ) -> tuple[list[DriverChange], list[str]]:
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: list[DriverChange] = []
    missing: list[str] = []
    p, end, stale = series.current_period(as_of_date)
    if stale:
        missing.append(stale)
    elif end is None:
        missing.append("no comparable quarterly or half-yearly revenue series")
    if end is not None:
        w = PERIOD_WORD[p]                       # "quarter" | "half-year"
        prev_p = prev_period_end(end, p)
        docs = series.get(Metric.REVENUE, end, p).doc_ids
        cadence_note = ([f"half-yearly reporter: each period is 6 months; persistence needs "
                         f"consecutive half-years"] if p == "H" else [])

        # Revenue growth and acceleration (same period YoY; no annualisation)
        g, g_prev = series.yoy(Metric.REVENUE, end, p), series.yoy(Metric.REVENUE, prev_p, p)
        if g is None:
            missing.append(f"year-ago revenue for {w} ending {end}")
        else:
            out.append(_dc("revenue_yoy_growth", series, end, g, None, "pct", f"{w} vs same {w} last year",
                           g >= th["revenue_yoy_pct"], docs, change=g, notes=cadence_note,
                           keys=_keys(Metric.REVENUE, p, end, _year_ago(end))))
            if g_prev is not None:
                out.append(_dc("revenue_growth_acceleration", series, end, g, g_prev, "pp",
                               f"YoY growth this {w} minus YoY growth previous {w}",
                               (g - g_prev) >= th["revenue_yoy_acceleration_pp"], docs, notes=cadence_note,
                               keys=_keys(Metric.REVENUE, p, end, _year_ago(end), prev_p, _year_ago(prev_p))))

        if not issuer_model.is_financial:
            m, m_prev = series.margin(end, p), series.margin(_year_ago(end), p)
            if m is not None and m_prev is not None:
                bps = (m - m_prev) * 100
                ebitda_src = series.get(Metric.EBITDA, end, p).source
                mkeys = _keys(Metric.EBITDA, p, end, _year_ago(end)) + _keys(Metric.REVENUE, p, end, _year_ago(end))
                out.append(_dc("ebitda_margin_change", series, end, m, m_prev, "bps", f"EBITDA/revenue vs year-ago {w}",
                               abs(bps) >= th["ebitda_margin_change_bps"], docs, change=bps,
                               notes=([f"EBITDA {ebitda_src}"] if ebitda_src != "reported" else []) + cadence_note,
                               keys=mkeys))
                rg = series.yoy(Metric.REVENUE, end, p)
                eg = series.yoy(Metric.EBITDA, end, p)
                if rg and eg is not None and rg > 0:
                    out.append(_dc("operating_leverage", series, end, eg / rg, None, "x",
                                   "EBITDA YoY growth / revenue YoY growth", eg / rg >= 1.5 and bps > 0, docs,
                                   change=eg / rg, notes=cadence_note, keys=mkeys))
            else:
                missing.append(f"EBITDA (reported or derivable) for current and year-ago {w}")

        out += _profit_drivers(series, p, end, w, th, docs, cadence_note, missing)

        # consecutive periods of material revenue growth (persistence)
        streak, d, skeys = 0, end, _keys(Metric.REVENUE, p, end, _year_ago(end))
        while True:
            gg = series.yoy(Metric.REVENUE, d, p)
            if gg is None or gg < th["revenue_yoy_pct"]:
                break
            streak += 1
            skeys += _keys(Metric.REVENUE, p, d, _year_ago(d))
            d = prev_period_end(d, p)
        out.append(_dc("material_growth_streak", series, end, float(streak), None, f"{w}s",
                       f"consecutive {w}s with revenue YoY >= {th['revenue_yoy_pct']}%", streak >= 2, docs,
                       change=float(streak), notes=cadence_note, keys=skeys))

        ttm_rev, miss = series.ttm(Metric.REVENUE, end, p)
        if ttm_rev is None:
            missing.extend(miss)
        else:
            # External demand from deduplicated, dated order events (see demand.py):
            # verified inflow, backlog snapshot and cancellations are kept separate.
            dem = summarise_demand(events, evidence, as_of_date)
            tkeys = _keys(Metric.REVENUE, p, *_ttm_ends(end, p))
            if dem.verified_inflow_crore > 0:
                btb = dem.verified_inflow_crore / ttm_rev
                ev_extra = [(e.first_public_at, None, d) for e in events if e.event_id in dem.verified_events
                            for d in e.doc_ids[:1]]
                out.append(_dc("disclosed_order_inflow_to_ttm_revenue", series, end, btb, None, "x",
                               "verified external binding order inflow (12m to as-of, deduplicated, current value "
                               "after amendments/cancellations) / TTM revenue; disclosed orders are a subset of "
                               "true inflow", btb >= th["book_to_bill"], change=btb,
                               notes=[f"{dem.verified_inflow_crore:.1f} cr verified inflow; TTM revenue "
                                      f"{ttm_rev:.1f} cr"] + dem.notes(),
                               keys=tkeys, extra=ev_extra))
            if dem.unverified_events:
                out.append(_dc("unverified_order_value_to_ttm_revenue", series, end,
                               dem.unverified_inflow_crore / ttm_rev, None, "x",
                               "early-lane order value (provisional / anonymous / ceiling) / TTM revenue; "
                               "headline values, not executable revenue", False,
                               change=dem.unverified_inflow_crore / ttm_rev, keys=tkeys,
                               notes=sorted({r for rs in dem.unverified_events.values() for r in rs})))
            if dem.backlog_crore is not None:
                cover = dem.backlog_crore / ttm_rev
                out.append(_dc("order_book_cover", series, end, cover, None, "years of TTM revenue",
                               f"company-stated order book as of {dem.backlog_as_of} / TTM revenue (dated "
                               "snapshot; not added to inflow)", cover >= th["order_book_cover_years"],
                               keys=tkeys, extra=[(datetime.combine(dem.backlog_as_of, datetime.min.time(),
                                                                    tzinfo=IST), None, dem.backlog_doc)]))
            if dem.cancellations_crore:
                out.append(_dc("order_cancellations_to_ttm_revenue", series, end,
                               dem.cancellations_crore / ttm_rev, None, "x",
                               "orders cancelled in the 12 months to as-of / TTM revenue", False,
                               change=dem.cancellations_crore / ttm_rev, keys=tkeys))

    # Stated utilisation is compared only within the same plant / product scope
    # (never plant A vs plant B, never a plant vs the company total).
    by_scope: dict[str, list[Evidence]] = {}
    for e in evidence:
        if e.usable and e.metric == Metric.UTILIZATION and e.quantity and e.modality.value == "realized":
            by_scope.setdefault(e.facility or "", []).append(e)
    for scope, util in sorted(by_scope.items()):
        util.sort(key=lambda e: e.available_at)
        if len(util) < 2:
            continue
        a, b = util[0], util[-1]
        where = f"{scope} " if scope else "company-level "
        out.append(_dc("capacity_utilization_change", series, None, b.quantity.value, a.quantity.value, "pp",
                       f"stated {where}utilisation, earliest vs latest (same scope only)",
                       abs(b.quantity.value - a.quantity.value) >= th["utilization_change_pp"],
                       notes=["management-stated, not observed; capacity denominators not verified"],
                       extra=[(a.available_at, None, a.doc_id), (b.available_at, None, b.doc_id)]))
    if len(by_scope) > 1:
        missing.append("utilisation stated for different plants/products; not compared across scopes ("
                       + ", ".join(k or "company-level" for k in sorted(by_scope)) + ")")
    out += _balance_sheet_and_cash_drivers(series, th)
    return out, missing


def _profit_drivers(series: FinancialSeries, p: str, end, w: str, th: dict, docs, cadence_note, missing) -> list:
    """Profit change on a recurring, parent-attributable basis.

    Basis order: recurring parent PAT -> recurring PBT (when the after-tax effect of
    exceptional items is undisclosed) -> total PAT (only when parent-attributable PAT
    is genuinely not reported; labelled).  A 0% parent growth never falls back to group
    PAT.  Zero / negative / low bases give absolute changes and turnaround status, not
    percentages."""
    ya = _year_ago(end)
    out: list[DriverChange] = []
    parent_now, b1 = series.recurring_pat(end, p, attributable=True)
    parent_ya, b2 = series.recurring_pat(ya, p, attributable=True)
    keys = _keys(Metric.PAT_ATTRIBUTABLE, p, end, ya) + _keys(Metric.EXCEPTIONAL_ITEMS, p, end, ya)
    basis, now, prior, notes = "", None, None, list(cadence_note)
    if parent_now is not None and parent_ya is not None:
        basis, now, prior = "recurring parent-attributable PAT", parent_now, parent_ya
    elif series.get(Metric.PAT_ATTRIBUTABLE, end, p) is not None and series.get(Metric.PAT_ATTRIBUTABLE, ya, p) is not None:
        now, _ = series.recurring_pbt(end, p)
        prior, _ = series.recurring_pbt(ya, p)
        basis = "recurring PBT (pre-exceptional); after-tax effect of exceptional items not disclosed"
        keys += _keys(Metric.PBT_PRE_EXCEPTIONAL, p, end, ya) + _keys(Metric.PBT, p, end, ya)
        notes.append("parent PAT includes exceptional items; recurring PAT/EPS incomplete")
    elif series.get(Metric.PAT_ATTRIBUTABLE, end, p) is None and series.get(Metric.PAT_ATTRIBUTABLE, ya, p) is None:
        now, b1 = series.recurring_pat(end, p, attributable=False)
        prior, b2 = series.recurring_pat(ya, p, attributable=False)
        basis = "total PAT (parent-attributable PAT not reported; labelled substitute)"
        keys += _keys(Metric.PAT, p, end, ya)
        notes.append("total PAT substitutes for parent-attributable PAT; minority share unknown")
    if now is None or prior is None:
        missing.append(f"recurring earnings for current and year-ago {w} ({b1}; {b2})")
        return out
    prof = series.change_profile(now, prior)
    # Non-operating income spike: profit growth driven by other income is not recurring improvement.
    oi_now, oi_ya = series.get(Metric.OTHER_INCOME, end, p), series.get(Metric.OTHER_INCOME, ya, p)
    pbt_now = series.get(Metric.PBT, end, p)
    spike = bool(oi_now and pbt_now and pbt_now.value > 0 and oi_now.value > 0.5 * pbt_now.value
                 and (oi_ya is None or oi_now.value > 3 * max(oi_ya.value, 1e-9)))
    if spike:
        notes.append("other income exceeds half of PBT and more than tripled: possible non-operating gain "
                     "(e.g. asset sale); not treated as recurring improvement")
        keys += _keys(Metric.OTHER_INCOME, p, end, ya)
    rev = series.get(Metric.REVENUE, end, p)
    if prof["kind"] in ("growth", "decline"):
        out.append(_dc("pat_yoy_growth", series, end, prof["pct"], None, "pct", f"{basis}, vs year-ago {w}",
                       prof["pct"] >= th["pat_yoy_pct"] and not spike, docs, change=prof["pct"], notes=notes,
                       keys=keys))
    elif prof["kind"] in ("loss_to_profit", "low_base", "loss_narrowed"):
        material = (prof["kind"] in ("loss_to_profit", "low_base") and not spike and rev is not None
                    and prof["abs"] >= th.get("turnaround_abs_pct_of_revenue", 2.0) / 100 * rev.value)
        out.append(_dc(f"pat_{prof['kind']}", series, end, now, prior, "crore",
                       f"{basis}; absolute change vs year-ago {w} (percentage growth not meaningful on a "
                       f"zero/negative/low base)", material, docs, change=prof["abs"], notes=notes, keys=keys))
    else:
        out.append(_dc(f"pat_{prof['kind']}", series, end, now, prior, "crore", f"{basis} vs year-ago {w}",
                       False, docs, change=prof["abs"], notes=notes, keys=keys))
    return out


def _balance_sheet_and_cash_drivers(series: FinancialSeries, th: dict) -> list:
    """Informational (non-qualifying) debt, cash-conversion and share-count changes."""
    out: list[DriverChange] = []
    inst = sorted(e for (m, t, e) in series.points if t == "I" and m in (Metric.BORROWINGS_CURRENT,
                                                                        Metric.BORROWINGS_NONCURRENT))
    if len(inst) >= 2:
        def gross(d):
            parts = [series.get(m, d, "I") for m in (Metric.BORROWINGS_NONCURRENT, Metric.BORROWINGS_CURRENT)]
            return None if all(x is None for x in parts) else sum(x.value for x in parts if x)

        def net(d):
            g, c = gross(d), series.get(Metric.CASH, d, "I")
            return None if g is None or c is None else g - c.value
        a, b = inst[0], inst[-1]
        na, nb = net(a), net(b)
        cur, prior, label = (nb, na, "net debt (borrowings - cash)") if na is not None and nb is not None             else (gross(b), gross(a), "gross borrowings")
        ks = [(m, "I", d) for d in (a, b) for m in (Metric.BORROWINGS_NONCURRENT, Metric.BORROWINGS_CURRENT,
                                                    Metric.CASH)]
        notes = []
        sa, sb = series.shares_from_capital(a, "I"), series.shares_from_capital(b, "I")
        if sa and sb and sb > sa * 1.005 and cur is not None and prior is not None and cur < prior:
            notes.append("debt fell while the share count rose: deleveraging may be equity-funded")
        out.append(_dc("debt_change", series, b, cur, prior, "crore", f"{label}, {a} to {b}", False,
                       notes=notes, keys=ks))
    for ptype in ("FY", "H"):
        ends = sorted(e for (m, t, e) in series.points if m == Metric.OPERATING_CASH_FLOW and t == ptype)
        if not ends:
            continue
        e = ends[-1]
        cfo = series.get(Metric.OPERATING_CASH_FLOW, e, ptype)
        pat = series.get(Metric.PAT_ATTRIBUTABLE, e, ptype) or series.get(Metric.PAT, e, ptype)
        if cfo and pat and pat.value > 0:
            ratio = cfo.value / pat.value
            out.append(_dc("cash_conversion", series, e, ratio, None, "x CFO/PAT",
                           f"cumulative {ptype} operating cash flow / PAT (the interval actually reported)",
                           False, change=ratio,
                           notes=["working-capital absorption" if ratio < 0.5 else "cash backs profit"],
                           keys=[(Metric.OPERATING_CASH_FLOW, ptype, e), (Metric.PAT_ATTRIBUTABLE, ptype, e),
                                 (Metric.PAT, ptype, e)]))
        break
    cap_ends = sorted({e for (m, t, e) in series.points if m == Metric.PAID_UP_CAPITAL})
    shares = [(e, t, series.shares_from_capital(e, t)) for e in cap_ends
              for t in ("Q", "H", "9M", "FY", "I") if series.shares_from_capital(e, t)]
    if len(shares) >= 2:
        (a, ta, sa), (b, tb, sb) = shares[0], shares[-1]
        if abs(sb - sa) > 0.005 * sa:
            out.append(_dc("share_count_change", series, b, sb, sa, "crore shares",
                           "paid-up capital / face value (labelled basis, not issuance history)", False,
                           notes=["share issuance or buyback: per-share figures need the diluted weighted count"],
                           keys=[(Metric.PAID_UP_CAPITAL, ta, a), (Metric.PAID_UP_CAPITAL, tb, b)]))
    return out
