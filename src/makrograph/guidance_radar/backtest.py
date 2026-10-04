"""Leakage-resistant walk-forward evaluation for radar decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from statistics import median
from typing import Optional

from dateutil.relativedelta import relativedelta

from .models import CompanyDecision


@dataclass(frozen=True)
class ForwardOutcome:
    ticker: str
    action: str
    decision_date: date
    entry_date: Optional[date]
    exit_date: Optional[date]
    horizon_months: Optional[int]
    return_pct: Optional[float]
    benchmark_return_pct: Optional[float]
    alpha_pct: Optional[float]
    reliable: bool
    exclusion_reason: str = ""

    def to_dict(self):
        return asdict(self)


class PostgresPriceProvider:
    """Price access with next-session entries and split-suspicion checks."""

    def __init__(self, pg_store, benchmark_symbol: str = "JUNIORBEES"):
        self.pg_store = pg_store
        self.benchmark_symbol = benchmark_symbol

    def forward_return(self, ticker: str, decision_date: date,
                       horizon_months: Optional[int], through_date: date) -> tuple:
        target = through_date if horizon_months is None else min(
            decision_date + relativedelta(months=horizon_months), through_date
        )
        with self.pg_store._conn() as conn:
            with conn.cursor() as cur:
                # Announcements may be published after the close. Enter at the
                # first close strictly after the dated information event.
                cur.execute("""
                    SELECT trade_date, close FROM nse_bhavcopy_data
                    WHERE symbol=%s AND series IN ('EQ','BE') AND trade_date>%s
                    ORDER BY trade_date ASC LIMIT 1
                """, (ticker, decision_date))
                entry = cur.fetchone()
                cur.execute("""
                    SELECT trade_date, close FROM nse_bhavcopy_data
                    WHERE symbol=%s AND series IN ('EQ','BE') AND trade_date<=%s
                    ORDER BY trade_date DESC LIMIT 1
                """, (ticker, target))
                exit_row = cur.fetchone()
                if not entry or not exit_row or exit_row[0] <= entry[0] or not entry[1]:
                    return None, None, None, False, "price coverage unavailable"
                cur.execute("""
                    WITH p AS (
                      SELECT trade_date, close, LAG(close) OVER (ORDER BY trade_date) previous
                      FROM nse_bhavcopy_data
                      WHERE symbol=%s AND series IN ('EQ','BE')
                        AND trade_date BETWEEN %s AND %s
                    )
                    SELECT MAX(ABS(close/NULLIF(previous,0)-1)) FROM p
                """, (ticker, entry[0], exit_row[0]))
                max_gap = float((cur.fetchone() or [0])[0] or 0)
        value = (float(exit_row[1]) / float(entry[1]) - 1.0) * 100.0
        reliable = max_gap < 0.45
        reason = "" if reliable else "unadjusted corporate-action-sized price discontinuity"
        return entry[0], exit_row[0], value, reliable, reason

    def benchmark_return(self, decision_date: date, exit_date: date) -> Optional[float]:
        with self.pg_store._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT trade_date, close FROM nse_bhavcopy_data
                    WHERE symbol=%s AND series IN ('EQ','BE') AND trade_date>%s
                    ORDER BY trade_date ASC LIMIT 1
                """, (self.benchmark_symbol, decision_date))
                entry = cur.fetchone()
                cur.execute("""
                    SELECT close FROM nse_bhavcopy_data
                    WHERE symbol=%s AND series IN ('EQ','BE') AND trade_date<=%s
                    ORDER BY trade_date DESC LIMIT 1
                """, (self.benchmark_symbol, exit_date))
                exit_row = cur.fetchone()
        if not entry or not exit_row or not entry[1]:
            return None
        return (float(exit_row[0]) / float(entry[1]) - 1.0) * 100.0


class WalkForwardEvaluator:
    def __init__(self, price_provider: PostgresPriceProvider):
        self.prices = price_provider

    def evaluate(self, decisions: list[CompanyDecision], through_date: date,
                 horizons: tuple[Optional[int], ...] = (12, 24, 36, None)) -> list[ForwardOutcome]:
        outcomes: list[ForwardOutcome] = []
        for decision in decisions:
            for horizon in horizons:
                # Do not report a nominal 24/36m result when the full horizon
                # has not elapsed; the None horizon explicitly means "to date".
                if horizon is not None and decision.as_of_date + relativedelta(months=horizon) > through_date:
                    continue
                entry, exit_day, value, reliable, reason = self.prices.forward_return(
                    decision.ticker, decision.as_of_date, horizon, through_date,
                )
                benchmark = self.prices.benchmark_return(decision.as_of_date, exit_day) if exit_day else None
                alpha = value - benchmark if value is not None and benchmark is not None else None
                outcomes.append(ForwardOutcome(
                    ticker=decision.ticker,
                    action=decision.action.value,
                    decision_date=decision.as_of_date,
                    entry_date=entry,
                    exit_date=exit_day,
                    horizon_months=horizon,
                    return_pct=round(value, 2) if value is not None else None,
                    benchmark_return_pct=round(benchmark, 2) if benchmark is not None else None,
                    alpha_pct=round(alpha, 2) if alpha is not None else None,
                    reliable=reliable,
                    exclusion_reason=reason,
                ))
        return outcomes

    @staticmethod
    def summarize(outcomes: list[ForwardOutcome]) -> list[dict]:
        groups: dict[tuple[str, Optional[int]], list[ForwardOutcome]] = {}
        for row in outcomes:
            groups.setdefault((row.action, row.horizon_months), []).append(row)
        summary: list[dict] = []
        for (action, horizon), rows in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1] or 999)):
            valid = [r for r in rows if r.reliable and r.return_pct is not None]
            alphas = [r.alpha_pct for r in valid if r.alpha_pct is not None]
            returns = [r.return_pct for r in valid]
            summary.append({
                "action": action,
                "horizon_months": horizon,
                "n_emitted": len(rows),
                "n_measured": len(valid),
                "coverage_pct": round(100 * len(valid) / len(rows), 1) if rows else 0.0,
                "median_return_pct": round(median(returns), 2) if returns else None,
                "median_alpha_pct": round(median(alphas), 2) if alphas else None,
                "positive_pct": round(100 * sum(v > 0 for v in returns) / len(returns), 1) if returns else None,
                "beat_benchmark_pct": round(100 * sum(v > 0 for v in alphas) / len(alphas), 1) if alphas else None,
                "multibagger_pct": round(100 * sum(v >= 100 for v in returns) / len(returns), 1) if returns else None,
                "severe_loss_pct": round(100 * sum(v <= -40 for v in returns) / len(returns), 1) if returns else None,
            })
        return summary
