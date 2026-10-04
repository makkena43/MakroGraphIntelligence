"""Comparable quarterly / annual measurement series.

Rules:
* One scope per series (consolidated preferred, else standalone); scopes are
  never mixed inside a calculation.
* For each (metric, period) the latest version public by the as-of date is
  used; earlier versions are kept as lineage (restatements are visible).
* Trailing-twelve-month (TTM) values are the SUM of four consecutive reported
  quarters.  The latest quarter is NEVER annualised and labelled trailing.
* Present-day snapshot tables (``fundamentals_snapshot``) are rejected for
  historical calculations unless the exact dated version is proven.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .contracts import FinancialMeasurement, Metric, Scope, Unit

FLOW_METRICS = {Metric.REVENUE, Metric.OTHER_INCOME, Metric.TOTAL_EXPENSES, Metric.EBITDA, Metric.DEPRECIATION,
                Metric.FINANCE_COST, Metric.EXCEPTIONAL_ITEMS, Metric.PBT, Metric.TAX, Metric.PAT,
                Metric.PAT_ATTRIBUTABLE, Metric.DILUTED_EPS, Metric.BASIC_EPS}

NON_POINT_IN_TIME_SOURCES = {"fundamentals_snapshot", "screener_fundamentals"}


class PointInTimeViolation(RuntimeError):
    pass


def assert_point_in_time_source(source_name: str, dated_version_proven: bool = False) -> None:
    if source_name in NON_POINT_IN_TIME_SOURCES and not dated_version_proven:
        raise PointInTimeViolation(
            f"{source_name} is an upserted present-day snapshot (last_updated), not point-in-time history")


def _prev_quarter_end(d: date) -> date:
    return {6: date(d.year, 3, 31), 9: date(d.year, 6, 30), 12: date(d.year, 9, 30), 3: date(d.year - 1, 12, 31)}[d.month]


def _year_ago(d: date) -> date:
    return date(d.year - 1, d.month, d.day if not (d.month == 2 and d.day == 29) else 28)


@dataclass
class SeriesPoint:
    value: float
    unit: Unit
    source: str            # reported | derived
    doc_ids: list[str]
    versions: int = 1      # >1 means a later filing revised this value


@dataclass
class FinancialSeries:
    ticker: str
    scope: Scope
    points: dict[tuple[Metric, str, date], SeriesPoint] = field(default_factory=dict)
    lineage_notes: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, ticker: str, rows: list[FinancialMeasurement],
              preference=(Scope.CONSOLIDATED, Scope.STANDALONE, Scope.UNKNOWN)) -> "FinancialSeries":
        rows = [r for r in rows if r.ticker == ticker]
        scope = Scope.UNKNOWN
        for want_q in (True, False):     # prefer a scope with a quarterly series
            hit = next((s for s in preference if any(
                r.scope == s and r.metric == Metric.REVENUE and (r.period_type == "Q" or not want_q) for r in rows)), None)
            if hit is not None:
                scope = hit
                break
        series = cls(ticker, scope)
        versions = defaultdict(list)
        for r in rows:
            if r.scope == scope:
                versions[(r.metric, r.period_type, r.period_end)].append(r)
        for key, vs in versions.items():
            vs.sort(key=lambda r: (r.available_at, r.doc_id))
            latest = vs[-1]
            distinct_vals = {round(v.value, 4) for v in vs}
            series.points[key] = SeriesPoint(latest.value, latest.unit, latest.source,
                                             sorted({v.doc_id for v in vs}), len(distinct_vals))
            if len(distinct_vals) > 1:
                series.lineage_notes.append(
                    f"{key[0].value} {key[1]} {key[2]}: revised from {vs[0].value} ({vs[0].doc_id}) "
                    f"to {latest.value} ({latest.doc_id})")
        series._derive_missing_q4()
        series._derive_ebitda()
        return series

    # -- derivations (always labelled) ------------------------------------

    def _derive_missing_q4(self) -> None:
        for (metric, ptype, end), p in list(self.points.items()):
            if ptype != "FY" or metric in (Metric.DILUTED_EPS, Metric.BASIC_EPS):
                continue
            if (metric, "Q", end) in self.points:
                continue
            nine = self.points.get((metric, "9M", end.replace(month=12, day=31, year=end.year - 1)))
            if nine:
                self.points[(metric, "Q", end)] = SeriesPoint(p.value - nine.value, p.unit, "derived:FY-9M",
                                                              p.doc_ids + nine.doc_ids)

    def _derive_ebitda(self) -> None:
        for (metric, ptype, end), rev in list(self.points.items()):
            if metric != Metric.REVENUE or (Metric.EBITDA, ptype, end) in self.points:
                continue
            te = self.points.get((Metric.TOTAL_EXPENSES, ptype, end))
            da = self.points.get((Metric.DEPRECIATION, ptype, end))
            fc = self.points.get((Metric.FINANCE_COST, ptype, end))
            if te and da and fc:
                self.points[(Metric.EBITDA, ptype, end)] = SeriesPoint(
                    rev.value - (te.value - da.value - fc.value), rev.unit,
                    "derived:revenue-(expenses-D&A-finance cost), excl. other income",
                    sorted(set(rev.doc_ids + te.doc_ids)))

    # -- access ------------------------------------------------------------

    def get(self, metric: Metric, end: date, ptype: str = "Q") -> Optional[SeriesPoint]:
        return self.points.get((metric, ptype, end))

    def quarter_ends(self, metric: Metric = Metric.REVENUE) -> list[date]:
        return sorted(e for (m, t, e) in self.points if m == metric and t == "Q")

    def latest_quarter(self, metric: Metric = Metric.REVENUE) -> Optional[date]:
        q = self.quarter_ends(metric)
        return q[-1] if q else None

    def ttm(self, metric: Metric, end: date) -> tuple[Optional[float], list[str]]:
        """Sum of four consecutive quarters ending ``end``; (None, missing) if any is absent."""
        vals, missing, d = [], [], end
        for _ in range(4):
            p = self.get(metric, d)
            if p is None:
                missing.append(f"{metric.value} quarter ending {d}")
            else:
                vals.append(p.value)
            d = _prev_quarter_end(d)
        if missing:
            return None, missing
        return sum(vals), []

    def yoy(self, metric: Metric, end: date, ptype: str = "Q") -> Optional[float]:
        cur, prev = self.get(metric, end, ptype), self.get(metric, _year_ago(end), ptype)
        if not cur or not prev or prev.value <= 0:
            return None
        return (cur.value / prev.value - 1.0) * 100.0

    def margin(self, end: date, ptype: str = "Q") -> Optional[float]:
        e, r = self.get(Metric.EBITDA, end, ptype), self.get(Metric.REVENUE, end, ptype)
        if not e or not r or r.value <= 0:
            return None
        return e.value / r.value * 100.0

    def recurring_pat_attributable(self, end: date, ptype: str = "Q") -> tuple[Optional[float], list[str]]:
        notes = []
        pat = self.get(Metric.PAT_ATTRIBUTABLE, end, ptype)
        if pat is None:
            pat = self.get(Metric.PAT, end, ptype)
            if pat is None:
                return None, ["PAT not reported"]
            notes.append("parent-attributable PAT not reported; total PAT used (minority interest unknown)")
        val = pat.value
        exc = self.get(Metric.EXCEPTIONAL_ITEMS, end, ptype)
        if exc and exc.value:
            pbt, tax = self.get(Metric.PBT, end, ptype), self.get(Metric.TAX, end, ptype)
            rate = (tax.value / pbt.value) if (pbt and tax and pbt.value > 0) else 0.25
            # statements present exceptional items as a deduction; positive = charge
            val = val + exc.value * (1 - rate)
            notes.append(f"exceptional items {exc.value} excluded at {rate:.0%} tax")
        return val, notes

    def shares_diluted_crore(self, end: date, ptype: str = "Q") -> Optional[float]:
        """Implied diluted shares = attributable PAT / diluted EPS (both reported)."""
        eps = self.get(Metric.DILUTED_EPS, end, ptype)
        pat = self.get(Metric.PAT_ATTRIBUTABLE, end, ptype) or self.get(Metric.PAT, end, ptype)
        if not eps or not pat or abs(eps.value) < 1e-9:
            return None
        return pat.value / eps.value
