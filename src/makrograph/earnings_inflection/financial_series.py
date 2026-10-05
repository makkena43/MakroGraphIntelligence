"""Comparable quarterly / annual measurement series.

Rules:
* One scope per series (consolidated preferred, else standalone); scopes are
  never mixed inside a calculation.
* For each (metric, period) the latest version public by the as-of date is
  used; earlier versions are kept as lineage (restatements are visible).
* Trailing-twelve-month (TTM) values are the SUM of the periods covering 12
  months: four quarters, or two half-years for issuers that report
  half-yearly (SME platforms).  The latest period is NEVER annualised.
* Each series has one reporting cadence ("Q" or "H"); quarterly and
  half-yearly figures are never mixed in a growth or TTM calculation.
* Present-day snapshot tables (``fundamentals_snapshot``) are rejected for
  historical calculations unless the exact dated version is proven.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
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


def _prev_half_end(d: date) -> date:
    """Half-years end 30 Sep (H1) and 31 Mar (H2) in the Indian fiscal year."""
    return {9: date(d.year, 3, 31), 3: date(d.year - 1, 9, 30)}[d.month]


PERIODS_PER_YEAR = {"Q": 4, "H": 2}
# Oldest acceptable latest period, in days before the as-of date: period length
# + SEBI results deadline (60 days for the last period of the year) + 30 days grace.
MAX_PERIOD_AGE_DAYS = {"Q": 92 + 60 + 30, "H": 183 + 60 + 30}
PERIOD_WORD = {"Q": "quarter", "H": "half-year"}


def prev_period_end(d: date, ptype: str) -> date:
    return _prev_half_end(d) if ptype == "H" else _prev_quarter_end(d)


def _year_ago(d: date) -> date:
    return date(d.year - 1, d.month, d.day if not (d.month == 2 and d.day == 29) else 28)


@dataclass
class SeriesPoint:
    value: float
    unit: Unit
    source: str            # reported | derived
    doc_ids: list[str]
    versions: int = 1      # >1 means a later filing revised this value
    available_at: Optional[datetime] = None    # public availability of the version used
    system_at: Optional[datetime] = None       # when MakroGraph held it (None = unproven)
    inputs: list = field(default_factory=list) # keys of the points a derived value depends on


def _latest(times):
    times = list(times)
    if not times or any(t is None for t in times):
        return None
    return max(times)


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
                                             sorted({v.doc_id for v in vs}), len(distinct_vals),
                                             latest.available_at, latest.system_available_at)
            if len(distinct_vals) > 1:
                series.lineage_notes.append(
                    f"{key[0].value} {key[1]} {key[2]}: revised from {vs[0].value} ({vs[0].doc_id}) "
                    f"to {latest.value} ({latest.doc_id})")
        series._derive_missing_q4()
        series._derive_missing_h2()
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
                self.points[(metric, "Q", end)] = self._derived(p.value - nine.value, p.unit, "derived:FY-9M",
                                                                [p, nine], [(metric, "FY", end),
                                                                            (metric, "9M", date(end.year - 1, 12, 31))])

    def _derive_missing_h2(self) -> None:
        """H2 = FY - H1 when a half-yearly reporter publishes only H1 and the full year."""
        for (metric, ptype, end), p in list(self.points.items()):
            if ptype != "FY" or metric in (Metric.DILUTED_EPS, Metric.BASIC_EPS):
                continue
            if (metric, "H", end) in self.points:
                continue
            h1 = self.points.get((metric, "H", date(end.year - 1, 9, 30)))
            if h1:
                self.points[(metric, "H", end)] = self._derived(p.value - h1.value, p.unit, "derived:FY-H1",
                                                                [p, h1], [(metric, "FY", end),
                                                                          (metric, "H", date(end.year - 1, 9, 30))])

    @staticmethod
    def _derived(value, unit, source, parts: list["SeriesPoint"], keys: list) -> "SeriesPoint":
        return SeriesPoint(value, unit, source, sorted({d for p in parts for d in p.doc_ids}), 1,
                           _latest(p.available_at for p in parts), _latest(p.system_at for p in parts),
                           list(keys))

    def provenance(self, keys) -> tuple[Optional[datetime], Optional[datetime], list[str]]:
        """(knowable_at, system_known_at, doc_ids) of the points a calculation used."""
        pts = [self.points[k] for k in keys if k in self.points]
        if not pts:
            return None, None, []
        return (_latest(p.available_at for p in pts), _latest(p.system_at for p in pts),
                sorted({d for p in pts for d in p.doc_ids}))

    def _derive_ebitda(self) -> None:
        for (metric, ptype, end), rev in list(self.points.items()):
            if metric != Metric.REVENUE or (Metric.EBITDA, ptype, end) in self.points:
                continue
            te = self.points.get((Metric.TOTAL_EXPENSES, ptype, end))
            da = self.points.get((Metric.DEPRECIATION, ptype, end))
            fc = self.points.get((Metric.FINANCE_COST, ptype, end))
            if te and da and fc:
                self.points[(Metric.EBITDA, ptype, end)] = self._derived(
                    rev.value - (te.value - da.value - fc.value), rev.unit,
                    "derived:revenue-(expenses-D&A-finance cost), excl. other income",
                    [rev, te, da, fc], [(m, ptype, end) for m in (Metric.REVENUE, Metric.TOTAL_EXPENSES,
                                                                 Metric.DEPRECIATION, Metric.FINANCE_COST)])

    # -- access ------------------------------------------------------------

    def get(self, metric: Metric, end: date, ptype: str = "Q") -> Optional[SeriesPoint]:
        return self.points.get((metric, ptype, end))

    def period_ends(self, metric: Metric = Metric.REVENUE, ptype: str = "Q") -> list[date]:
        ends = sorted(e for (m, t, e) in self.points if m == metric and t == ptype)
        if ptype == "H":
            ends = [e for e in ends if e.month in (3, 9)]
        return ends

    def quarter_ends(self, metric: Metric = Metric.REVENUE) -> list[date]:
        return self.period_ends(metric, "Q")

    def latest_period(self, metric: Metric = Metric.REVENUE, ptype: str = "Q") -> Optional[date]:
        e = self.period_ends(metric, ptype)
        return e[-1] if e else None

    def latest_quarter(self, metric: Metric = Metric.REVENUE) -> Optional[date]:
        return self.latest_period(metric, "Q")

    def cadence(self, metric: Metric = Metric.REVENUE) -> Optional[str]:
        """Reporting cadence to analyse: "Q", "H" (half-yearly, typical for SME issuers) or None.

        The most recent cadence that supports a year-on-year comparison wins;
        quarterly is preferred on ties (mainboard Q2 statements also carry H1
        columns).  A company that migrated from SME to mainboard is analysed
        half-yearly until four quarters with a year-ago comparison exist.
        """
        cands = []
        for p in ("Q", "H"):
            end = self.latest_period(metric, p)
            if end is not None:
                cands.append((end, p == "Q", p, self.yoy(metric, end, p) is not None))
        if not cands:
            return None
        cands.sort(key=lambda c: (c[3], c[0], c[1]), reverse=True)
        return cands[0][2]

    def current_period(self, as_of: date, metric: Metric = Metric.REVENUE
                       ) -> tuple[Optional[str], Optional[date], str]:
        """(cadence, latest period end, stale_note).

        The latest parsed period is unusable when results for later periods
        should already be public: drivers would otherwise describe a period
        that is a year or more old.  Then end is None and stale_note explains.
        """
        p = self.cadence(metric)
        end = self.latest_period(metric, p) if p else None
        if end is None:
            return p, None, ""
        age = (as_of - end).days
        if age > MAX_PERIOD_AGE_DAYS[p]:
            return p, None, (f"latest parsed {PERIOD_WORD[p]} ends {end} ({age} days before as-of); "
                             f"results for later {PERIOD_WORD[p]}s were not parsed from the filings")
        return p, end, ""

    def ttm(self, metric: Metric, end: date, ptype: str = "Q") -> tuple[Optional[float], list[str]]:
        """Sum of the periods covering 12 months to ``end``; (None, missing) if any is absent."""
        vals, missing, d = [], [], end
        for _ in range(PERIODS_PER_YEAR[ptype]):
            p = self.get(metric, d, ptype)
            if p is None:
                missing.append(f"{metric.value} {PERIOD_WORD[ptype]} ending {d}")
            else:
                vals.append(p.value)
            d = prev_period_end(d, ptype)
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
        """Implied diluted shares = attributable PAT / diluted EPS (both reported).

        Falls back to the previous period when the latest one is derived
        (e.g. H2 = FY - H1 carries no EPS).
        """
        for d in (end, prev_period_end(end, ptype)):
            eps = self.get(Metric.DILUTED_EPS, d, ptype)
            pat = self.get(Metric.PAT_ATTRIBUTABLE, d, ptype) or self.get(Metric.PAT, d, ptype)
            if eps and pat and abs(eps.value) > 1e-9:
                return pat.value / eps.value
        return None
