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
from .financial_series import FinancialSeries


def build_bridge(series: Optional[FinancialSeries], issuer_model: IssuerModel,
                 guidance: list[GuidanceRecord]) -> EarningsBridge:
    if issuer_model.is_financial:
        return EarningsBridge(ScenarioStatus.UNSUPPORTED_FINANCIAL_MODEL,
                              missing_inputs=[f"operating-margin bridge not applicable to {issuer_model.value}"])
    missing: list[str] = []
    end = series.latest_quarter() if series else None
    if end is None:
        return EarningsBridge(ScenarioStatus.NOT_COMPUTED_MISSING_INPUTS, missing_inputs=["quarterly revenue series"])

    def ttm(metric):
        v, miss = series.ttm(metric, end)
        missing.extend(miss)
        return v

    rev, ebitda, da, fin = ttm(Metric.REVENUE), ttm(Metric.EBITDA), ttm(Metric.DEPRECIATION), ttm(Metric.FINANCE_COST)
    pbt, tax = ttm(Metric.PBT), ttm(Metric.TAX)
    pat, pat_attr = series.ttm(Metric.PAT, end)[0], series.ttm(Metric.PAT_ATTRIBUTABLE, end)[0]
    shares = series.shares_diluted_crore(end)
    if shares is None:
        missing.append("diluted share count (reported diluted EPS and PAT for latest quarter)")
    if missing:
        return EarningsBridge(ScenarioStatus.NOT_COMPUTED_MISSING_INPUTS,
                              base_period_label=f"TTM to {fy_label_for(end, 'Q')}", missing_inputs=sorted(set(missing)))

    tax_rate = tax / pbt if pbt and pbt > 0 else 0.25
    tax_note = "effective TTM tax rate" if pbt and pbt > 0 else "25% statutory-style assumption (TTM PBT <= 0)"
    minority = (1 - pat_attr / pat) if (pat and pat_attr and pat > 0) else 0.0
    base_margin = ebitda / rev * 100
    common = {"D&A (TTM, held flat)": round(da, 2), "finance cost (TTM, held flat)": round(fin, 2),
              "tax rate": f"{tax_rate:.1%} ({tax_note})", "minority share of PAT": f"{minority:.1%}",
              "diluted shares (crore, implied PAT/EPS)": round(shares, 4), "other income": "excluded"}

    def scen(name, revenue, margin, extra_notes=()):
        e = revenue * margin / 100
        pat_r = (e - da - fin) * (1 - tax_rate) * (1 - minority)
        return BridgeScenario(name=name, assumptions={"revenue_crore": round(revenue, 2), "ebitda_margin_pct": round(margin, 2), **common},
                              revenue_crore=round(revenue, 2), ebitda_crore=round(e, 2),
                              recurring_pat_attributable_crore=round(pat_r, 2),
                              recurring_diluted_eps=round(pat_r / shares, 2), notes=list(extra_notes))

    label = f"TTM to {fy_label_for(end, 'Q')}"
    scenarios = [scen("trailing_run_rate", rev, base_margin,
                      ["sum of last four reported quarters; latest quarter NOT annualised"])]
    g = series.yoy(Metric.REVENUE, end)
    m_latest = series.margin(end)
    if g is not None and m_latest is not None:
        scenarios.append(scen("latest_quarter_trend_persists", rev * (1 + g / 100), m_latest,
                              [f"TTM revenue grown at latest quarter YoY ({g:.1f}%) for one year; latest-quarter margin "
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
