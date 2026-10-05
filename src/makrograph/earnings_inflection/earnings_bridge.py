"""Assumption-based earnings bridge (no valuation, no price, no action).

Translates a revenue / margin scenario into recurring parent-attributable
diluted EPS using the company's own trailing cost structure.  Every
assumption is printed with the scenario.  Banks, insurers, NBFCs and
investment companies return ``UNSUPPORTED_FINANCIAL_MODEL``: an industrial
EBITDA bridge would be misleading for them.
"""

from __future__ import annotations

from typing import Optional

from .contracts import (
    BridgeScenario, EarningsBridge, GuidanceOutcome, GuidanceRecord, IssuerModel, Metric, ScenarioStatus,
)
from .extraction import fy_label_for
from .financial_series import PERIOD_WORD, PERIODS_PER_YEAR, FinancialSeries, prev_period_end


def build_bridge(series: Optional[FinancialSeries], issuer_model: IssuerModel,
                 guidance: list[GuidanceRecord], as_of_date=None) -> EarningsBridge:
    if issuer_model.is_financial:
        return EarningsBridge(ScenarioStatus.UNSUPPORTED_FINANCIAL_MODEL,
                              missing_inputs=[f"operating-margin bridge not applicable to {issuer_model.value}"])
    missing: list[str] = []
    stale = ""
    if series is None:
        p = end = None
    elif as_of_date is not None:
        p, end, stale = series.current_period(as_of_date)
    else:
        p = series.cadence()
        end = series.latest_period(Metric.REVENUE, p) if p else None
    if end is None:
        return EarningsBridge(ScenarioStatus.NOT_COMPUTED_MISSING_INPUTS,
                              missing_inputs=[stale or "quarterly or half-yearly revenue series"])
    w = PERIOD_WORD[p]

    def ttm(metric):
        v, miss = series.ttm(metric, end, p)
        missing.extend(miss)
        return v

    rev, ebitda, da, fin = ttm(Metric.REVENUE), ttm(Metric.EBITDA), ttm(Metric.DEPRECIATION), ttm(Metric.FINANCE_COST)
    pbt, tax = ttm(Metric.PBT), ttm(Metric.TAX)
    pat, pat_attr = series.ttm(Metric.PAT, end, p)[0], series.ttm(Metric.PAT_ATTRIBUTABLE, end, p)[0]
    shares = series.shares_diluted_crore(end, p)
    if shares is None:
        missing.append(f"diluted share count (reported diluted EPS and PAT for latest {w})")
    if missing:
        return EarningsBridge(ScenarioStatus.NOT_COMPUTED_MISSING_INPUTS,
                              base_period_label=f"TTM to {fy_label_for(end, p)}", missing_inputs=sorted(set(missing)))

    tax_rate = tax / pbt if pbt and pbt > 0 else 0.25
    tax_note = "effective TTM tax rate" if pbt and pbt > 0 else "25% statutory-style assumption (TTM PBT <= 0)"
    minority = (1 - pat_attr / pat) if (pat and pat_attr and pat > 0) else 0.0
    base_margin = ebitda / rev * 100
    share_basis = "implied: parent PAT / diluted EPS of the latest period (labelled fallback)"
    cap_shares = next((series.shares_from_capital(end, t) for t in (p, "I", "FY")
                       if series.shares_from_capital(end, t)), None)
    if cap_shares:
        gap = shares / cap_shares - 1
        share_basis += f"; paid-up capital / face value gives {cap_shares:.4f} crore ({gap:+.1%})"
    ends, d = [], end
    for _ in range(4 if p == "Q" else 2):
        ends.append(d)
        d = prev_period_end(d, p)
    exc_periods = [e for e in ends if (series.exceptional(e, p)[0] or 0) != 0]
    if exc_periods:
        tax_note += ("; TTM PBT includes exceptional items in " + ", ".join(str(e) for e in exc_periods)
                     + ", so the effective rate is distorted (scenario assumption, not a reported fact)")
    common = {"D&A (TTM, held flat)": round(da, 2), "finance cost (TTM, held flat)": round(fin, 2),
              "tax rate": f"{tax_rate:.1%} ({tax_note})", "minority share of PAT": f"{minority:.1%}",
              "diluted shares (crore)": round(shares, 4), "share count basis": share_basis,
              "other income": "excluded", "EBITDA definition": "revenue - operating costs (excl. other income)"}

    def scen(name, revenue, margin, extra_notes=()):
        e = revenue * margin / 100
        pat_r = (e - da - fin) * (1 - tax_rate) * (1 - minority)
        return BridgeScenario(name=name, assumptions={"revenue_crore": round(revenue, 2), "ebitda_margin_pct": round(margin, 2), **common},
                              revenue_crore=round(revenue, 2), ebitda_crore=round(e, 2),
                              recurring_pat_attributable_crore=round(pat_r, 2),
                              recurring_diluted_eps=round(pat_r / shares, 2), notes=list(extra_notes))

    label = f"TTM to {fy_label_for(end, p)}"
    n = PERIODS_PER_YEAR[p]
    scenarios = [scen("trailing_run_rate", rev, base_margin,
                      [f"sum of last {n} reported {w}s; latest {w} NOT annualised"])]
    g = series.yoy(Metric.REVENUE, end, p)
    m_latest = series.margin(end, p)
    if g is not None and m_latest is not None:
        scenarios.append(scen("latest_period_trend_persists", rev * (1 + g / 100), m_latest,
                              [f"TTM revenue grown at latest {w} YoY ({g:.1f}%) for one year; latest-{w} margin "
                               "held; an illustration of persistence, not a forecast"]))
    rg = [r for r in guidance if r.metric == Metric.REVENUE_GROWTH_GUIDANCE and r.outcome == GuidanceOutcome.PENDING]
    mg = [r for r in guidance if r.metric == Metric.MARGIN_GUIDANCE and r.outcome == GuidanceOutcome.PENDING]
    if rg:
        latest_rg = (rg[-1].revisions or [rg[-1].original])[-1]
        margin = ((mg[-1].revisions or [mg[-1].original])[-1].quantity.value
                  if mg and (mg[-1].revisions or [mg[-1].original])[-1].quantity else base_margin)
        if latest_rg.quantity:
            scenarios.append(scen("management_stated_case", rev * (1 + latest_rg.quantity.value / 100), margin,
                                  [f"uses management's latest stated growth for {rg[-1].target_period_label}"
                                   + ("" if mg else "; margin held at TTM (no margin guidance)"),
                                   "management assertion, not realized evidence"]))
    return EarningsBridge(ScenarioStatus.COMPUTED_ASSUMPTION_BASED, base_period_label=label, scenarios=scenarios)
