"""Deterministic candidate driver calculations.

Thresholds come from config and only flag a change as *material* for
disclosure; they are descriptive, not decision rules.  No price or
share-price-momentum input exists here: a prior rally never rejects a
business and is not a driver.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .contracts import DriverChange, EconomicEvent, Evidence, IssuerModel, Metric, Unit
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


def _dc(driver, s: FinancialSeries, end, cur, prior, unit, basis, material, docs=(), notes=None, change=None):
    if change is None and cur is not None and prior is not None:
        change = cur - prior
    return DriverChange(driver=driver, ticker=s.ticker, period_end=end, current=cur, prior=prior, change=change,
                        unit=unit, basis=basis, material=material, source_doc_ids=list(docs), notes=list(notes or []))


def compute_drivers(series: FinancialSeries, events: list[EconomicEvent], evidence: list[Evidence],
                    issuer_model: IssuerModel, as_of_date: date, thresholds: Optional[dict] = None
                    ) -> tuple[list[DriverChange], list[str]]:
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: list[DriverChange] = []
    missing: list[str] = []
    p = series.cadence()
    end = series.latest_period(Metric.REVENUE, p) if p else None
    if end is None:
        missing.append("no comparable quarterly or half-yearly revenue series")
    else:
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
                           g >= th["revenue_yoy_pct"], docs, change=g, notes=cadence_note))
            if g_prev is not None:
                out.append(_dc("revenue_growth_acceleration", series, end, g, g_prev, "pp",
                               f"YoY growth this {w} minus YoY growth previous {w}",
                               (g - g_prev) >= th["revenue_yoy_acceleration_pp"], docs, notes=cadence_note))

        if not issuer_model.is_financial:
            m, m_prev = series.margin(end, p), series.margin(_year_ago(end), p)
            if m is not None and m_prev is not None:
                bps = (m - m_prev) * 100
                ebitda_src = series.get(Metric.EBITDA, end, p).source
                out.append(_dc("ebitda_margin_change", series, end, m, m_prev, "bps", f"EBITDA/revenue vs year-ago {w}",
                               abs(bps) >= th["ebitda_margin_change_bps"], docs, change=bps,
                               notes=([f"EBITDA {ebitda_src}"] if ebitda_src != "reported" else []) + cadence_note))
                rg = series.yoy(Metric.REVENUE, end, p)
                eg = series.yoy(Metric.EBITDA, end, p)
                if rg and eg is not None and rg > 0:
                    out.append(_dc("operating_leverage", series, end, eg / rg, None, "x",
                                   "EBITDA YoY growth / revenue YoY growth", eg / rg >= 1.5 and bps > 0, docs,
                                   change=eg / rg, notes=cadence_note))
            else:
                missing.append(f"EBITDA (reported or derivable) for current and year-ago {w}")

        pat_g = series.yoy(Metric.PAT_ATTRIBUTABLE, end, p) or series.yoy(Metric.PAT, end, p)
        if pat_g is not None:
            out.append(_dc("pat_yoy_growth", series, end, pat_g, None, "pct", f"PAT vs year-ago {w}",
                           pat_g >= th["pat_yoy_pct"], docs, change=pat_g, notes=cadence_note))

        # consecutive periods of material revenue growth (persistence)
        streak, d = 0, end
        while True:
            gg = series.yoy(Metric.REVENUE, d, p)
            if gg is None or gg < th["revenue_yoy_pct"]:
                break
            streak += 1
            d = prev_period_end(d, p)
        out.append(_dc("material_growth_streak", series, end, float(streak), None, f"{w}s",
                       f"consecutive {w}s with revenue YoY >= {th['revenue_yoy_pct']}%", streak >= 2, docs,
                       change=float(streak), notes=cadence_note))

        ttm_rev, miss = series.ttm(Metric.REVENUE, end, p)
        if ttm_rev is None:
            missing.extend(miss)
        else:
            # Book-to-bill from DEDUPLICATED binding/provisional order events over the trailing 12 months
            window_start = as_of_date - timedelta(days=365)
            inflow = sum(e.amount.value for e in events
                         if e.amount and e.amount.unit == Unit.INR_CRORE and e.first_public_at
                         and e.first_public_at.date() >= window_start
                         and e.commitment_strength.value in ("binding", "provisional"))
            if inflow > 0:
                btb = inflow / ttm_rev
                out.append(_dc("disclosed_order_inflow_to_ttm_revenue", series, end, btb, None, "x",
                               "sum of deduplicated disclosed order events (12m to as-of) / TTM revenue; "
                               "disclosed orders are a subset of true inflow", btb >= th["book_to_bill"],
                               change=btb, docs=sorted({d for e in events for d in e.doc_ids}), notes=[f"{inflow:.1f} cr disclosed inflow; TTM revenue {ttm_rev:.1f} cr"]))
            ob = [e for e in evidence if e.usable and e.metric == Metric.ORDER_BOOK and e.quantity
                  and e.quantity.unit == Unit.INR_CRORE and e.modality.value == "realized"]
            if ob:
                ob.sort(key=lambda e: e.available_at)
                latest = ob[-1]
                cover = latest.quantity.value / ttm_rev
                prior_cover = None
                older = [e for e in ob if e.available_at <= latest.available_at - timedelta(days=300)]
                if older:
                    prior_cover = older[-1].quantity.value / ttm_rev
                out.append(_dc("order_book_cover", series, end, cover, prior_cover, "years of TTM revenue",
                               "latest stated order book / TTM revenue (prior uses same TTM base)",
                               cover >= th["order_book_cover_years"], [latest.doc_id]))

    util = sorted((e for e in evidence if e.usable and e.metric == Metric.UTILIZATION and e.quantity
                   and e.modality.value == "realized"), key=lambda e: e.available_at)
    if len(util) >= 2:
        a, b = util[0], util[-1]
        out.append(DriverChange("capacity_utilization_change", series.ticker, None, b.quantity.value, a.quantity.value,
                                b.quantity.value - a.quantity.value, "pp", "stated utilisation, earliest vs latest",
                                abs(b.quantity.value - a.quantity.value) >= th["utilization_change_pp"],
                                notes=["management-stated, not observed"], source_doc_ids=[a.doc_id, b.doc_id]))
    return out, missing
