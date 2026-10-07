"""Numbers-first early signals of an earnings inflection (``numeric-rules-1``).

Each newly reported quarter is judged once, at the moment its figures became public, on the series
built from the figures public by then (XBRL first, PDF tables as fallback).  A signal is a research
lead, not a forecast or a recommendation.

Signals for quarter E (all year on year against E one year earlier, so seasonality cancels):

- ``revenue_acceleration``  revenue growth >= 20% and >= the median growth of the previous four
  quarters + 10 points (at least three of them known)
- ``operating_leverage``    EBITDA margin up >= 3 points with revenue growth >= 10% and EBITDA > 0
- ``profit_step_up``        profit up >= 50% from a positive base with EBITDA up >= 25%; or a loss
  turned into a profit while EBITDA rose and is positive
- ``finance_cost_relief``   finance cost down >= 25% where it was >= 10% of EBITDA a year earlier

Guards: a quarter whose year-ago revenue is below 5 crore is not judged; a year-ago quarter more than 20%
below the quarter two years earlier (a COVID-lockdown base) needs the signal to hold against two years
earlier too; a profit signal is not raised
when exceptional items are >= 25% of PBT or other income is >= 50% of PBT in that quarter.  Profit is
the owners' share for a consolidated series when both quarters have it, otherwise profit after tax.
The thresholds were set before any outcome was examined and are frozen per rules version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Callable, Iterable, Optional

from .contracts import Metric, Scope

NUMERIC_RULES_VERSION = "numeric-rules-1"

DEFAULT_NUMERIC_THRESHOLDS = {
    "min_year_ago_revenue_cr": 5.0,
    "rev_growth_min_pct": 20.0,
    "rev_accel_pp": 10.0,
    "lev_margin_gain_pp": 3.0,
    "lev_rev_growth_min_pct": 10.0,
    "profit_growth_min_pct": 50.0,
    "profit_ebitda_growth_min_pct": 25.0,
    "fin_cost_drop_pct": 25.0,
    "fin_cost_min_share_of_ebitda": 0.10,
    "exceptional_max_share_of_pbt": 0.25,
    "other_income_max_share_of_pbt": 0.50,
    "max_quarter_age_days": 200,
    "depressed_base_drop": 0.20,
}


@dataclass
class NumericSignal:
    kind: str
    period_end: date
    known_at: datetime
    scope: str
    values: dict = field(default_factory=dict)
    basis: str = ""

    @property
    def signal_id(self) -> str:
        return f"{self.kind}:{self.period_end.isoformat()}"


def _prev_q(d: date) -> date:
    y, m = (d.year, d.month - 3) if d.month > 3 else (d.year - 1, d.month + 9)
    return date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m])


def _year_ago(d: date, years: int = 1) -> date:
    return date(d.year - years, d.month, d.day)


def _val(series, metric: Metric, end: date) -> Optional[float]:
    p = series.get(metric, end, "Q")
    return None if p is None else p.value


def _growth(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or prev <= 0:
        return None
    return (cur / prev - 1.0) * 100.0


def signals_for_quarter(series, end: date, known_at: datetime, th: Optional[dict] = None) -> list[NumericSignal]:
    """Signals for quarter ``end`` on ``series`` (the series as public at ``known_at``).

    Depressed base: when the year-ago quarter's revenue is more than ``depressed_base_drop`` below the
    quarter two years earlier (e.g. a COVID-lockdown quarter), a signal stands only if the same kind of
    signal also holds against the quarter two years earlier."""
    th = {**DEFAULT_NUMERIC_THRESHOLDS, **(th or {})}
    one = _signals_vs(series, end, known_at, th, 1)
    r1, r2 = _val(series, Metric.REVENUE, _year_ago(end)), _val(series, Metric.REVENUE, _year_ago(end, 2))
    if one and r1 is not None and r2 is not None and r2 > 0 and r1 < (1 - th["depressed_base_drop"]) * r2:
        two = {x.kind for x in _signals_vs(series, end, known_at, th, 2)}
        kept = [x for x in one if x.kind in two]
        for x in kept:
            x.values["depressed_base_checked"] = True
            x.basis += " (year-ago base depressed; also holds against two years earlier)"
        return kept
    return one


def _signals_vs(series, end: date, known_at: datetime, th: dict, years: int) -> list[NumericSignal]:
    scope = series.scope.value
    ly = _year_ago(end, years)
    rev, rev_ly = _val(series, Metric.REVENUE, end), _val(series, Metric.REVENUE, ly)
    if rev is None or rev_ly is None or rev_ly < th["min_year_ago_revenue_cr"]:
        return []
    g = _growth(rev, rev_ly)
    out: list[NumericSignal] = []

    def sig(kind, values, basis):
        out.append(NumericSignal(kind, end, known_at, scope, {k: (round(v, 3) if isinstance(v, float) else v)
                                                             for k, v in values.items()}, basis))

    # revenue acceleration against the company's own recent growth
    prior, d = [], end
    for _ in range(4):
        d = _prev_q(d)
        pg = _growth(_val(series, Metric.REVENUE, d), _val(series, Metric.REVENUE, _year_ago(d, years)))
        if pg is not None:
            prior.append(pg)
    if g is not None and g >= th["rev_growth_min_pct"] and len(prior) >= 3 \
            and g >= median(prior) + th["rev_accel_pp"]:
        sig("revenue_acceleration", {"revenue_growth_pct": g, "prior_median_growth_pct": median(prior)},
            f"revenue +{g:.0f}% YoY vs a median of {median(prior):+.0f}% over the previous four quarters")

    eb, eb_ly = _val(series, Metric.EBITDA, end), _val(series, Metric.EBITDA, ly)
    m = eb / rev * 100 if eb is not None and rev > 0 else None
    m_ly = eb_ly / rev_ly * 100 if eb_ly is not None and rev_ly > 0 else None
    if m is not None and m_ly is not None and g is not None and eb > 0 \
            and m - m_ly >= th["lev_margin_gain_pp"] and g >= th["lev_rev_growth_min_pct"]:
        sig("operating_leverage", {"ebitda_margin_pct": m, "ebitda_margin_year_ago_pct": m_ly, "revenue_growth_pct": g},
            f"EBITDA margin {m_ly:.1f}% -> {m:.1f}% with revenue +{g:.0f}%")

    # profit: owners' share for a consolidated series when both quarters have it
    pm = Metric.PAT
    if series.scope == Scope.CONSOLIDATED and _val(series, Metric.PAT_ATTRIBUTABLE, end) is not None \
            and _val(series, Metric.PAT_ATTRIBUTABLE, ly) is not None:
        pm = Metric.PAT_ATTRIBUTABLE
    p, p_ly = _val(series, pm, end), _val(series, pm, ly)
    pbt, exc, oi = _val(series, Metric.PBT, end), _val(series, Metric.EXCEPTIONAL_ITEMS, end), \
        _val(series, Metric.OTHER_INCOME, end)
    clean = pbt is not None and pbt > 0 and abs(exc or 0.0) < th["exceptional_max_share_of_pbt"] * pbt \
        and (oi or 0.0) < th["other_income_max_share_of_pbt"] * pbt
    if p is not None and p_ly is not None and eb is not None and eb_ly is not None and clean:
        ge = _growth(eb, eb_ly)
        if p_ly > 0:
            gp = _growth(p, p_ly)
            if gp is not None and ge is not None and gp >= th["profit_growth_min_pct"] \
                    and ge >= th["profit_ebitda_growth_min_pct"]:
                sig("profit_step_up", {"profit_metric": pm.value, "profit": p, "profit_year_ago": p_ly,
                                       "profit_growth_pct": gp, "ebitda_growth_pct": ge},
                    f"{pm.value} {p_ly:,.1f} -> {p:,.1f} cr (+{gp:.0f}%), EBITDA +{ge:.0f}%")
        elif p > 0 and eb > 0 and eb > eb_ly:
            sig("profit_step_up", {"profit_metric": pm.value, "profit": p, "profit_year_ago": p_ly,
                                   "turnaround": True, "ebitda": eb, "ebitda_year_ago": eb_ly},
                f"{pm.value} turned from {p_ly:,.1f} to {p:,.1f} cr with EBITDA {eb_ly:,.1f} -> {eb:,.1f} cr")

    fc, fc_ly = _val(series, Metric.FINANCE_COST, end), _val(series, Metric.FINANCE_COST, ly)
    if fc is not None and fc_ly is not None and eb_ly is not None and eb_ly > 0 and fc_ly > 0 \
            and fc_ly >= th["fin_cost_min_share_of_ebitda"] * eb_ly \
            and fc <= fc_ly * (1 - th["fin_cost_drop_pct"] / 100):
        sig("finance_cost_relief", {"finance_cost": fc, "finance_cost_year_ago": fc_ly,
                                    "share_of_ebitda_year_ago": fc_ly / eb_ly},
            f"finance cost {fc_ly:,.1f} -> {fc:,.1f} cr ({fc_ly / eb_ly:.0%} of EBITDA a year earlier)")
    return out


def detect_numeric_signals(series_at: Callable[[datetime], object], times: Iterable[datetime],
                           th: Optional[dict] = None) -> list[NumericSignal]:
    """Judge each newly reported quarter once, at the first time in ``times`` when it is the latest
    quarter of the series public then.  ``series_at(t)`` builds the series from figures public by ``t``,
    so a signal never uses a later figure or restatement (prefix invariant)."""
    th = {**DEFAULT_NUMERIC_THRESHOLDS, **(th or {})}
    seen: set[date] = set()
    out: list[NumericSignal] = []
    for t in sorted(set(times)):
        s = series_at(t)
        end = s.latest_quarter() if s is not None else None
        if end is None or end in seen:
            continue
        seen.add(end)
        if (t.date() - end).days > th["max_quarter_age_days"]:
            continue                  # an old quarter first read late (e.g. a comparative): not a new result
        out += signals_for_quarter(s, end, t, th)
    return out
