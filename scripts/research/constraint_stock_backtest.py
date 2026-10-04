#!/usr/bin/env python3
"""Backtest the "Greatest Emerging Constraints" detector section, by stock tier.

For each anchor year, detects the year's emerging constraints, attaches their
stocks (verified operating makers / pre-operational pipeline / filing-exposure —
exactly as the report renders them), then measures the forward PRICE return of
every stock from the anchor to the latest available date. Aggregates by tier so
we can see whether "verified maker", "pipeline", and "exposure" each pay off.

Point-in-time: constraint detection and the stock set are frozen at each anchor;
only the price window looks forward. Alias-aware / right-censored / outlier-
guarded via winner_autopsy.fwd_return.
"""
import os
import statistics
import sys
from collections import defaultdict
from datetime import date

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
sys.path.insert(0, HERE)
from extract_report_data import connect                                    # noqa: E402
from constraint_emergence import (detect_emerging_constraints,             # noqa: E402
                                  attach_emerging_constraint_makers,
                                  attach_constraint_exposure_companies)
from moonshot_screen import alias_map                                      # noqa: E402
from winner_autopsy import fwd_return                                      # noqa: E402

ANCHORS = [date(2022, 12, 31), date(2023, 12, 31), date(2024, 12, 31),
           date(2025, 12, 31)]   # 2026-07-05 has ~no elapsed window
HORIZONS = [(180, "6m"), (365, "12m"), (730, "24m"), (1095, "36m")]


def _med(v):
    return round(statistics.median(v), 1) if v else None


def _bench(cur, al, as_of, horizon):
    """Point-in-time 25-stock trailing-turnover large-cap basket, equal-weighted."""
    cur.execute("""SELECT symbol FROM nse_bhavcopy_data WHERE series IN ('EQ','BE')
                   AND trade_date BETWEEN %s AND %s GROUP BY symbol
                   ORDER BY SUM(tottrdval) DESC LIMIT 25""",
                (as_of - __import__("datetime").timedelta(days=365), as_of))
    rs = [fwd_return(cur, al, r["symbol"], as_of, horizon) for r in cur.fetchall()]
    rs = [x for x in rs if x is not None]
    return _med(rs)


def main() -> int:
    conn = connect()
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT MAX(trade_date) d FROM nse_bhavcopy_data WHERE series IN ('EQ','BE')")
    latest = cur.fetchone()["d"]
    al = alias_map(cur)
    print(f"Detector-section backtest — forward returns at every elapsed horizon, "
          f"through {latest}. Each cell = median % of that tier's stocks; benchmark = "
          f"point-in-time 25-stock large-cap basket.\n")

    # Per (anchor, tier, horizon-label) -> list of returns, pooled per tier too.
    pool = defaultdict(lambda: defaultdict(list))     # tier -> hlabel -> [rets]

    for as_of in ANCHORS:
        # Elapsed horizons for this anchor + the to-latest catch-all.
        hs = [(d, lbl) for d, lbl in HORIZONS if as_of + __import__("datetime").timedelta(days=d) <= latest]
        hs.append(((latest - as_of).days, "to-latest"))
        hlabels = [lbl for _, lbl in hs]
        print("=" * 100)
        print(f"ANCHOR {as_of}   horizons: {', '.join(hlabels)}")
        print("=" * 100)
        # benchmark row
        brow = "  " + f"{'BENCHMARK (25 large-cap)':38}"
        for d, lbl in hs:
            b = _bench(cur, al, as_of, d)
            brow += f"{(f'{b:+.0f}%' if b is not None else '-'):>11}"
        print(brow)

        det = detect_emerging_constraints(cur, as_of, "IN", top_n=8)
        attach_emerging_constraint_makers(cur, as_of, det, "IN")
        attach_constraint_exposure_companies(cur, as_of, det, "IN")
        for c in det:
            label, traj = c["constraint"], c.get("trajectory", "")
            for tier, stocks in (("verified", c.get("stocks_verified") or []),
                                 ("pipeline", c.get("stocks_pipeline") or []),
                                 ("exposure", c.get("stocks_exposure") or [])):
                if not stocks:
                    continue
                cells = ""
                for d, lbl in hs:
                    rets = [r for r in (fwd_return(cur, al, tk, as_of, d) for tk in stocks)
                            if r is not None]
                    for r in rets:
                        pool[tier][lbl].append(r)
                    m = _med(rets)
                    cells += f"{(f'{m:+.0f}%' if m is not None else '-'):>11}"
                print(f"  {label[:24]:24} {tier:8} n={len(stocks):<2}{cells}")
        print()

    print("=" * 100)
    print("POOLED by tier — median across all anchors' stocks, per horizon")
    print("=" * 100)
    all_labels = ["6m", "12m", "24m", "36m", "to-latest"]
    print("  " + f"{'tier':22}" + "".join(f"{l:>11}" for l in all_labels))
    for tier in ("verified", "pipeline", "exposure"):
        row = "  " + f"{tier:22}"
        for lbl in all_labels:
            v = pool[tier][lbl]
            row += f"{(f'{_med(v):+.0f}%' if v else '-'):>11}"
        print(row + f"   (n={sum(len(pool[tier][l]) for l in ['to-latest'])})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
