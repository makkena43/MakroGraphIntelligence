"""Optional valuation research context (WP7).  Off by default.

Reads an INJECTED market-data adapter (no network here) and turns the bridge's
scenarios into multiples.  Rules:
* price = last close ON OR BEFORE the as-of date for the identity-checked security
  (exchange + symbol + series); a series mismatch (warrants, partly paid, SME vs
  main board) makes valuation unavailable rather than wrong;
* share counts are adjusted for verified splits / bonuses ex-dated after the share-count
  date and on or before the as-of date; actions after the as-of date are never used;
* output is context - scenario multiples, implied earnings at visible reference multiples,
  downside sensitivity and dated price changes.  It never changes evidence status, never
  rejects a company because its price already rose, and contains no action or target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Protocol

from .contracts import EarningsBridge, ScenarioStatus


@dataclass(frozen=True)
class Security:
    exchange: str
    symbol: str
    series: str
    isin: str = ""


@dataclass(frozen=True)
class CorporateAction:
    ex_date: date
    kind: str                 # "split" | "bonus"
    ratio: float              # new shares per old share: 1:5 split -> 5.0, 1:1 bonus -> 2.0
    source: str = ""

    def __post_init__(self):
        if self.kind not in ("split", "bonus") or self.ratio <= 0:
            raise ValueError("only verified splits / bonuses with a positive ratio are applied")
        if not self.source:
            raise ValueError("corporate action needs a source (exchange corporate-action file, notice id)")


class MarketData(Protocol):
    source: str

    def closes(self, security: Security, start: date, end: date) -> dict[date, float]: ...

    def corporate_actions(self, security: Security) -> list[CorporateAction]: ...


@dataclass
class InMemoryMarketData:
    """Fixture / offline adapter: unadjusted, series-filtered closes per security."""
    prices: dict = field(default_factory=dict)            # (exchange, symbol, series) -> {date: close}
    actions: dict = field(default_factory=dict)           # (exchange, symbol, series) -> [CorporateAction]
    source: str = "in-memory fixture"

    def closes(self, security, start, end):
        key = (security.exchange, security.symbol, security.series)
        return {d: v for d, v in self.prices.get(key, {}).items() if start <= d <= end}

    def corporate_actions(self, security):
        return list(self.actions.get((security.exchange, security.symbol, security.series), []))


def _close_on_or_before(series: dict[date, float], d: date, max_days: int = 7) -> Optional[tuple[date, float]]:
    for i in range(max_days + 1):
        x = d - timedelta(days=i)
        if x in series:
            return x, series[x]
    return None


def _factor(actions: list[CorporateAction], after: date, upto: date) -> float:
    f = 1.0
    for a in actions:
        if after < a.ex_date <= upto:
            f *= a.ratio
    return f


def valuation_context(bridge: EarningsBridge, security: Optional[Security], expected_series: Optional[str],
                      as_of: date, market: Optional[MarketData], shares_crore: Optional[float],
                      shares_as_of: Optional[date], reference_pe: tuple[float, ...] = (15.0, 25.0)) -> dict:
    def unavailable(reason):
        return {"status": "UNAVAILABLE", "reason": reason,
                "note": "valuation is optional context; its absence does not affect the evidence assessment"}
    if market is None:
        return unavailable("no market-data adapter configured")
    if security is None:
        return unavailable("security identity unknown for the as-of date")
    if expected_series and security.series.upper() != expected_series.upper():
        return unavailable(f"security series {security.series} does not match the listed series {expected_series} "
                           "at the as-of date (warrant / partly-paid / board collision)")
    if bridge.status != ScenarioStatus.COMPUTED_ASSUMPTION_BASED or not shares_crore or shares_as_of is None:
        return unavailable("earnings scenarios or share count unavailable")
    closes = market.closes(security, as_of - timedelta(days=400), as_of)          # never after the cutoff
    px = _close_on_or_before(closes, as_of)
    if px is None:
        return unavailable(f"no close for {security.symbol} {security.series} within 7 days before {as_of}")
    px_date, price = px
    actions = [a for a in market.corporate_actions(security) if a.ex_date <= as_of]
    shares_adj = shares_crore * _factor(actions, shares_as_of, as_of)
    mcap = price * shares_adj                                                      # crore shares x INR = crore
    rows = []
    for s in bridge.scenarios:
        pat = s.recurring_pat_attributable_crore
        rows.append({"scenario": s.name, "recurring_pat_crore": pat,
                     "pe": round(mcap / pat, 1) if pat and pat > 0 else None,
                     "pe_note": "" if pat and pat > 0 else "not meaningful (loss)"})
    base = next((r for r in rows if r["scenario"] == "trailing_run_rate"), None)
    down = next((r for r in rows if r["scenario"] == "downside_reported_lows"), None)
    changes = {}
    for days in (90, 180, 365):
        past = _close_on_or_before(closes, as_of - timedelta(days=days))
        if past:
            adj_past = past[1] / _factor(actions, past[0], px_date)   # restate the old price in today's shares
            changes[f"{days}d"] = {"from": past[0].isoformat(), "pct": round((price / adj_past - 1) * 100, 1)}
    return {
        "status": "COMPUTED",
        "security": {"exchange": security.exchange, "symbol": security.symbol, "series": security.series,
                     "isin": security.isin},
        "price": {"date": px_date.isoformat(), "close": price, "source": market.source},
        "shares_crore": round(shares_adj, 4),
        "share_adjustments": [{"ex_date": a.ex_date.isoformat(), "kind": a.kind, "ratio": a.ratio,
                               "source": a.source} for a in actions if shares_as_of < a.ex_date <= as_of],
        "market_cap_crore": round(mcap, 1),
        "scenario_multiples": rows,
        "implied_recurring_pat_crore": {f"at {pe:g}x": round(mcap / pe, 1) for pe in reference_pe},
        "reference_multiples_source": "analyst assumption (config valuation.reference_pe)",
        "downside_sensitivity": ({"downside_pe": down["pe"], "base_pe": base["pe"]} if down and base else {}),
        "price_change_context": changes,
        "note": ("Context only: multiples on assumption-based scenarios. A prior price rise does not exclude a "
                 "company and valuation never changes the evidence status."),
    }
