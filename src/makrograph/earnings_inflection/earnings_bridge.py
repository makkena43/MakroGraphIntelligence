"""Assumption-based earnings bridge (no price, no action) - WP7.

Translates revenue / margin cases into recurring parent-attributable diluted EPS
using the company's own reported cost structure.  Every input is listed in an
assumption register with its source: ``reported`` (a parsed figure),
``management assertion`` (guidance) or ``analyst assumption`` (config, visible).

Cases
* downside_reported_lows   - TTM revenue grown at the weakest YoY growth of the TTM periods
                             (0% if none was negative) at the lowest reported period margin
* trailing_run_rate        - base: sum of the last reported periods (never an annualised quarter)
* latest_period_trend_persists - upside illustration: the latest YoY growth and margin persist
* management_stated_case   - only when the guidance's fiscal target year sits directly on a
                             REPORTED fiscal-year base, and any margin guidance names the same year;
                             FY26 growth is never combined with FY27 margins or a rolling TTM base

D&A and interest include the visible effect of stated capex (useful life and funding are
analyst assumptions; "internal accruals" stated by the company means no new debt).  Recurring
EPS stays unavailable when its inputs are; share-count bases that disagree (warrants,
partly-paid or preference capital colliding with equity) block EPS instead of picking one.
Banks, insurers, NBFCs and investment companies return UNSUPPORTED_FINANCIAL_MODEL.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .contracts import (
    BridgeScenario, EarningsBridge, Evidence, GuidanceOutcome, GuidanceRecord, IssuerModel, MechanismResult, Metric,
    Modality, ScenarioStatus, Unit,
)
from .extraction import fy_label_end, fy_label_for
from .financial_series import PERIOD_WORD, PERIODS_PER_YEAR, FinancialSeries, _year_ago, prev_period_end

DEFAULT_ASSUMPTIONS = {
    "capex_useful_life_years": 15.0,        # straight-line D&A on stated capex
    "capex_debt_share_if_unstated": 0.5,    # share of capex assumed debt-funded when funding is not stated
    "incremental_debt_rate_pct": 9.0,
    "fallback_tax_rate_pct": 25.17,         # India new-regime corporate rate incl. surcharge/cess
    "share_basis_max_gap_pct": 10.0,        # EPS-implied vs capital-implied share counts
}


def _reg(register, name, value, source, note=""):
    register.append({"input": name, "value": value, "source": source, **({"note": note} if note else {})})


def _fy_revenue(series: FinancialSeries, fy_end: date, p: str) -> Optional[float]:
    pt = series.get(Metric.REVENUE, fy_end, "FY")
    if pt is not None:
        return pt.value
    v, miss = series.ttm(Metric.REVENUE, fy_end, p)
    return v if not miss else None


def build_bridge(series: Optional[FinancialSeries], issuer_model: IssuerModel, guidance: list[GuidanceRecord],
                 as_of_date=None, evidence: Optional[list[Evidence]] = None,
                 mechanisms: Optional[list[MechanismResult]] = None,
                 assumptions: Optional[dict] = None) -> EarningsBridge:
    A = {**DEFAULT_ASSUMPTIONS, **(assumptions or {})}
    evidence = evidence or []
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
    w, n = PERIOD_WORD[p], PERIODS_PER_YEAR[p]
    label = f"TTM to {fy_label_for(end, p)}"

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
    cap_shares = next((series.shares_from_capital(end, t) for t in (p, "I", "FY")
                       if series.shares_from_capital(end, t)), None)
    if shares and cap_shares and abs(shares / cap_shares - 1) * 100 > A["share_basis_max_gap_pct"]:
        missing.append(f"share-count bases disagree: EPS-implied {shares:.4f} vs paid-up capital / face value "
                       f"{cap_shares:.4f} crore ({shares / cap_shares - 1:+.0%}); warrants, partly-paid or "
                       "preference capital may collide with equity - resolve the security before computing EPS")
    if missing:
        return EarningsBridge(ScenarioStatus.NOT_COMPUTED_MISSING_INPUTS, base_period_label=label,
                              missing_inputs=sorted(set(missing)))

    reg: list[dict] = []
    _reg(reg, "TTM revenue (cr)", round(rev, 2), "reported", f"sum of the last {n} {w}s; never an annualised {w}")
    _reg(reg, "TTM EBITDA (cr)", round(ebitda, 2), "reported" if not any(
        series.get(Metric.EBITDA, e, p) and series.get(Metric.EBITDA, e, p).source.startswith("derived")
        for e in _ends(end, p)) else "derived from reported rows", "revenue - operating costs, excl. other income")
    _reg(reg, "TTM D&A (cr)", round(da, 2), "reported")
    _reg(reg, "TTM finance cost (cr)", round(fin, 2), "reported")
    eff = tax / pbt if pbt and pbt > 0 else None
    if eff is not None and 0.05 <= eff <= 0.40:
        tax_rate, tax_src = eff, "reported (effective TTM rate)"
    else:
        tax_rate, tax_src = A["fallback_tax_rate_pct"] / 100, "analyst assumption (effective rate unusable)"
    exc = [e for e in _ends(end, p) if (series.exceptional(e, p)[0] or 0) != 0]
    _reg(reg, "tax rate", f"{tax_rate:.1%}", tax_src,
         ("TTM PBT includes exceptional items in " + ", ".join(map(str, exc)) + "; rate distorted") if exc else "")
    minority = (1 - pat_attr / pat) if (pat and pat_attr and pat > 0) else 0.0
    _reg(reg, "minority share of PAT", f"{minority:.1%}", "reported" if pat_attr else "assumed 0 (not reported)")
    _reg(reg, "diluted shares (crore)", round(shares, 4), "reported (parent PAT / diluted EPS)",
         f"paid-up capital / face value gives {cap_shares:.4f}" if cap_shares else "")
    fund = [e for e in evidence if e.usable and e.metric == Metric.FUNDRAISE and e.available_at and
            as_of_date and (as_of_date - e.available_at.date()).days <= 365]
    if fund:
        _reg(reg, "announced issuance", f"{len(fund)} mention(s)", "management assertion",
             "warrants / preferential / QIP not yet in the share count: diluted EPS overstated by an unknown amount")

    # stated capex -> incremental D&A and interest (visible analyst assumptions)
    capex = [e for e in evidence if e.usable and e.metric == Metric.CAPEX and e.quantity
             and e.quantity.unit == Unit.INR_CRORE]
    capex_amt = max((e.quantity.value for e in capex), default=0.0)
    inc_da = inc_int = 0.0
    if capex_amt:
        q = " ".join(e.quote for e in capex).lower()
        debt_share = (0.0 if re.search(r"internal accruals?|own funds|internally funded", q)
                      else 1.0 if re.search(r"\b(?:debt|loan|borrow\w*|term loan)\b", q)
                      else A["capex_debt_share_if_unstated"])
        src = ("management assertion (funding stated)" if debt_share in (0.0, 1.0)
               else "analyst assumption (funding not stated)")
        inc_da = capex_amt / A["capex_useful_life_years"]
        inc_int = capex_amt * debt_share * A["incremental_debt_rate_pct"] / 100
        _reg(reg, "stated capex (cr)", capex_amt, "management assertion", capex[-1].quote[:160])
        _reg(reg, "incremental D&A from capex (cr/yr)", round(inc_da, 2), "analyst assumption",
             f"straight line over {A['capex_useful_life_years']:g} years; completion timing unknown, so a full "
             "year is charged in growth cases")
        _reg(reg, "incremental interest from capex (cr/yr)", round(inc_int, 2), src,
             f"{debt_share:.0%} debt-funded at {A['incremental_debt_rate_pct']:g}%")

    def scen(name, revenue, margin, notes=(), with_capex=False):
        e = revenue * margin / 100
        d = da + (inc_da if with_capex else 0.0)
        f = fin + (inc_int if with_capex else 0.0)
        pat_r = (e - d - f) * (1 - tax_rate) * (1 - minority)
        return BridgeScenario(name=name, assumptions={"revenue_crore": round(revenue, 2),
                                                      "ebitda_margin_pct": round(margin, 2),
                                                      "D&A": round(d, 2), "finance cost": round(f, 2)},
                              revenue_crore=round(revenue, 2), ebitda_crore=round(e, 2),
                              recurring_pat_attributable_crore=round(pat_r, 2),
                              recurring_diluted_eps=round(pat_r / shares, 2), notes=list(notes))

    base_margin = ebitda / rev * 100
    ends = _ends(end, p)
    margins = [m for m in (series.margin(e, p) for e in ends) if m is not None]
    growths = [g for g in (series.yoy(Metric.REVENUE, e, p) for e in ends) if g is not None]
    scenarios = []
    low_g = min([0.0] + [g for g in growths if g < 0])
    low_m = min(margins) if margins else base_margin
    scenarios.append(scen("downside_reported_lows", rev * (1 + low_g / 100), low_m,
                          [f"weakest YoY growth among the TTM {w}s ({low_g:.1f}%) and lowest reported {w} margin "
                           f"({low_m:.1f}%); cited facts, not a forecast"]))
    scenarios.append(scen("trailing_run_rate", rev, base_margin,
                          [f"sum of last {n} reported {w}s; latest {w} NOT annualised"]))
    g = series.yoy(Metric.REVENUE, end, p)
    m_latest = series.margin(end, p)
    if g is not None and m_latest is not None:
        scenarios.append(scen("latest_period_trend_persists", rev * (1 + g / 100), m_latest,
                              [f"TTM revenue grown at the latest {w} YoY ({g:.1f}%) for one year; latest-{w} margin "
                               "held; an illustration of persistence, not a forecast"]
                              + (["includes D&A and interest of stated capex"] if capex_amt else []),
                              with_capex=bool(capex_amt)))
    mgmt, note = _management_case(series, p, guidance, base_margin, scen, bool(capex_amt), reg)
    if mgmt:
        scenarios.append(mgmt)
    b = EarningsBridge(ScenarioStatus.COMPUTED_ASSUMPTION_BASED, base_period_label=label, scenarios=scenarios,
                       assumption_register=reg, management_case_note=note)
    b.cash_notes = _cash_notes(series)
    b.mechanism_contributions = _mechanism_contributions(mechanisms or [], rev, fin, series, n)
    return b


def _ends(end, p):
    out, d = [], end
    for _ in range(PERIODS_PER_YEAR[p]):
        out.append(d)
        d = prev_period_end(d, p)
    return out


def _latest_target(g: GuidanceRecord):
    for r in reversed(g.revisions):
        if r.direction.value == "withdrawn":
            return None
        if r.quantity is not None:
            return r
    return g.original


def _management_case(series, p, guidance, base_margin, scen, with_capex, reg):
    """FY growth guidance applies to its own fiscal base year (target FY - 1), which must be reported."""
    rg = [r for r in guidance if r.metric == Metric.REVENUE_GROWTH_GUIDANCE and r.outcome == GuidanceOutcome.PENDING
          and r.target_period_label.upper().startswith("FY")]
    if not rg:
        return None, "no pending fiscal-year revenue growth guidance"
    g = rg[-1]
    target_end = fy_label_end(g.target_period_label)
    latest = _latest_target(g)
    if target_end is None or latest is None or latest.quantity is None:
        return None, f"{g.target_period_label} growth guidance withdrawn or unquantified"
    base_end = _year_ago(target_end)
    base_rev = _fy_revenue(series, base_end, p)
    if base_rev is None:
        return None, (f"{g.target_period_label} growth applies to the {fy_label_for(base_end, 'FY')} base, which is not "
                      "reported yet; growth is never applied to a rolling TTM base")
    mg = [r for r in guidance if r.metric == Metric.MARGIN_GUIDANCE and r.outcome == GuidanceOutcome.PENDING]
    margin, margin_src = base_margin, "TTM margin held (no margin guidance for the same year)"
    same = [r for r in mg if r.target_period_label.upper() == g.target_period_label.upper()]
    other = [r for r in mg if r.target_period_label.upper() != g.target_period_label.upper()
             and r.target_period_label != "unspecified"]
    if same and _latest_target(same[-1]) and _latest_target(same[-1]).quantity:
        q = _latest_target(same[-1]).quantity
        margin = q.low if q.low is not None else q.value
        margin_src = f"management margin guidance for {g.target_period_label} (low end)"
    elif other:
        return None, (f"growth guidance is for {g.target_period_label} but margin guidance is for "
                      f"{other[-1].target_period_label}: fiscal targets differ, no combined management case")
    q = latest.quantity
    growth = q.low if q.low is not None else q.value
    _reg(reg, f"{g.target_period_label} revenue growth", f"{growth:g}%", "management assertion",
         f"latest stated ({latest.quote[:120]}); low end of any range")
    _reg(reg, f"{fy_label_for(base_end, 'FY')} revenue base (cr)", round(base_rev, 2), "reported")
    s = scen("management_stated_case", base_rev * (1 + growth / 100), margin,
             [f"{g.target_period_label} = {fy_label_for(base_end, 'FY')} revenue x (1 + {growth:g}%); {margin_src}",
              "management assertion, not realized evidence"] + (["includes D&A and interest of stated capex"]
                                                                if with_capex else []),
             with_capex=with_capex)
    return s, f"computed for {g.target_period_label} on the reported {fy_label_for(base_end, 'FY')} base"


def _cash_notes(series: FinancialSeries) -> list[str]:
    out = []
    for t in ("FY", "H"):
        ends = sorted(e for (m, tt, e) in series.points if m == Metric.OPERATING_CASH_FLOW and tt == t)
        if ends:
            e = ends[-1]
            cfo = series.get(Metric.OPERATING_CASH_FLOW, e, t)
            pat = series.get(Metric.PAT_ATTRIBUTABLE, e, t) or series.get(Metric.PAT, e, t)
            if cfo and pat and pat.value > 0:
                out.append(f"operating cash flow / PAT for {t} to {e}: {cfo.value / pat.value:.2f}x "
                           + ("(working capital absorbs profit)" if cfo.value < 0.5 * pat.value else ""))
            break
    return out


def _mechanism_contributions(mechs, rev, fin, series, n) -> list[dict]:
    """Annual earnings effect by mechanism where it can be quantified; never summed when the
    mechanisms describe the same change."""
    out = []
    for m in mechs:
        if m.stale or (not m.qualifies_positive and m.state.value != "commitment"):
            continue
        eff, basis = None, "not quantified"
        if m.mechanism.value == "pricing_input_costs" and m.magnitude is not None:
            eff, basis = m.magnitude / 10000 * rev, "gross-margin change x TTM revenue (before overheads)"
        elif m.mechanism.value in ("product_customer_mix", "segment_turnaround") and m.magnitude is not None:
            eff, basis = m.magnitude * n, f"latest-period segment change x {n} (only if sustained for a year)"
        elif m.mechanism.value == "debt_reduction" and m.magnitude is not None:
            gross = [series.get(x, m.period_end, "I") for x in (Metric.BORROWINGS_CURRENT, Metric.BORROWINGS_NONCURRENT)]
            g = sum(x.value for x in gross if x)
            if g > 0 and fin:
                rate = fin / (g + m.magnitude)
                eff, basis = m.magnitude * rate, f"debt reduction x implied interest rate {rate:.1%}"
        out.append({"mechanism": m.mechanism.value, "state": m.state.value,
                    "annual_effect_crore": round(eff, 2) if eff is not None else None, "basis": basis,
                    "overlaps_with": list(m.overlaps_with)})
    if any(x["overlaps_with"] for x in out):
        out.append({"mechanism": "note", "basis": "overlapping mechanisms describe the same margin change: "
                                                   "their effects are not additive"})
    return out
