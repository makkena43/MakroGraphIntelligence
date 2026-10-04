"""Outcome sandbox - deliberately isolated from detection.

No detection module may import this file (enforced by a test).  It consumes
already-produced assessments plus an injected, identity-checked price series
and reports forward outcomes for research.  It never feeds outcomes back into
thresholds, and its output is not evidence of alpha: the case set, survivorship
and multiple testing must be addressed before any statistical claim.

Price identity rules (spec §2): NSE bhavcopy is keyed by (trade_date, symbol)
without series/ISIN, so the caller must supply series-filtered, corporate-
action-adjusted closes and say how adjustment was done.  Inferring adjustment
from "no daily move above 45%" is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .contracts import Assessment


@dataclass
class PriceSeries:
    ticker: str
    closes: dict[date, float]
    adjustment_method: str            # e.g. "exchange corporate-action file, splits+bonus"
    series_filter: str                # e.g. "NSE EQ/BE only"

    def __post_init__(self):
        bad = ("no daily move", "heuristic", "assumed adjusted", "")
        if self.adjustment_method.strip().lower() in bad or "45%" in self.adjustment_method:
            raise ValueError("corporate-action adjustment must be explicit, not inferred from price moves")
        if not self.series_filter:
            raise ValueError("series filter required (warrants/partly-paid lines collide on symbol)")

    def close_on_or_after(self, d: date, max_days: int = 7) -> Optional[tuple[date, float]]:
        for i in range(max_days + 1):
            x = d + timedelta(days=i)
            if x in self.closes:
                return x, self.closes[x]
        return None


@dataclass
class OutcomeRow:
    ticker: str
    as_of: date
    evidence_status: str
    horizon_days: int
    start: Optional[date]
    end: Optional[date]
    return_pct: Optional[float]
    benchmark_return_pct: Optional[float] = None
    notes: list[str] = field(default_factory=list)


def forward_outcomes(assessments: list[Assessment], prices: dict[str, PriceSeries],
                     horizons=(182, 365), benchmark: Optional[PriceSeries] = None) -> list[OutcomeRow]:
    rows = []
    for a in assessments:
        ps = prices.get(a.ticker)
        # start strictly AFTER the as-of day to avoid same-day look-ahead
        start_d = a.as_of.date() + timedelta(days=1)
        for h in horizons:
            r = OutcomeRow(a.ticker, a.as_of.date(), a.evidence_status.value, h, None, None, None)
            if ps is None:
                r.notes.append("no identity-checked price series")
                rows.append(r)
                continue
            s, e = ps.close_on_or_after(start_d), ps.close_on_or_after(start_d + timedelta(days=h))
            if s and e:
                r.start, r.end = s[0], e[0]
                r.return_pct = round((e[1] / s[1] - 1) * 100, 2)
                if benchmark:
                    bs, be = benchmark.close_on_or_after(s[0]), benchmark.close_on_or_after(e[0])
                    if bs and be:
                        r.benchmark_return_pct = round((be[1] / bs[1] - 1) * 100, 2)
            else:
                r.notes.append("price data incomplete for horizon")
            rows.append(r)
    return rows
