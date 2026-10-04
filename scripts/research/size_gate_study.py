#!/usr/bin/env python3
"""
Size-gate study — how many >=5x winners are actually DEPLOYABLE at a given
fund size, and what the relative bottom-half cut costs.

TWO SEPARATE QUESTIONS, DELIBERATELY MEASURED APART
---------------------------------------------------
1. CEILING ("too_large", 48-59 winners rejected in the autopsy): the venture
   premise that a heavily-traded largecap can 3x but not 40x. Today this is
   a RELATIVE cut — the universe median — which means it moves whenever
   mapping work changes the universe rather than when anything about a
   company changes. Widening the universe by ~36% pushed `too_large`
   rejections from 48 to 59 purely as a side effect. This sweep asks what an
   absolute ceiling would cost or save.

2. FLOOR (deployability): a capacity question the screen never asked at all.
   The autopsy's sharpest finding here is KERNEX — 19x ahead of it at the
   Dec-2020 anchor, trading ~1.4 lakh/day, i.e. untradeable for any real
   PMS. Recall measured without a floor counts winners you could never have
   bought. This sweep re-scores recall at each fund size so the number means
   something operationally.

The output is deliberately a CAPACITY CURVE, not a single recommended
number: how much of the historical opportunity set survives at Rs.25cr,
50cr, 100cr, 250cr, 500cr AUM. That curve is a business input (what AUM can
this strategy carry?), not just a screen parameter.

USAGE
-----
    python scripts/research/size_gate_study.py
    python scripts/research/size_gate_study.py --aums 50,100,250
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
sys.path.insert(0, HERE)

from extract_report_data import connect                       # noqa: E402
from moonshot_screen import deployable_floor_lakh, alias_map   # noqa: E402
from winner_autopsy import ANCHORS, HORIZON_DAYS, all_names    # noqa: E402

DEFAULT_AUMS = [25, 50, 100, 250, 500]


def winner_liquidity(cur, names):
    """Median daily traded value at the anchor for every cohort winner,
    alias-aware so a renamed symbol is not scored as untradeable."""
    cur.execute("""SELECT anchor_date, ticker, multiple, med_val_lakh
                   FROM mg_winner_cohort WHERE horizon_days=%s""",
                (HORIZON_DAYS,))
    return [(r["anchor_date"], r["ticker"], float(r["multiple"]),
             float(r["med_val_lakh"] or 0)) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(description="Size-gate capacity curve")
    ap.add_argument("--aums", default=",".join(str(a) for a in DEFAULT_AUMS),
                    help="fund sizes in Rs. crore")
    ap.add_argument("--position-pct", type=float, default=1.5,
                    help="moonshot starter position as %% of book")
    ap.add_argument("--build-days", type=int, default=20)
    ap.add_argument("--max-pct-adv", type=float, default=10.0)
    args = ap.parse_args()
    aums = [float(a) for a in args.aums.split(",")]

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    names = all_names(cur)
    rows = winner_liquidity(cur, names)
    if not rows:
        print("No winner cohort — run winner_autopsy.py --stage cohort first.")
        return

    total = len(rows)
    print(f"Winner cohort: {total} instances (>=5x / {HORIZON_DAYS}d)")
    print(f"Position policy: {args.position_pct}% of book, built over "
          f"{args.build_days} sessions at <={args.max_pct_adv}% of ADV\n")

    print("=== CAPACITY CURVE: how much of the opportunity set is buildable ===")
    print(f"{'AUM (Rs.cr)':>12} {'floor (lakh/d)':>15} {'deployable':>11} "
          f"{'% of cohort':>12} {'median mult':>12}")
    for aum in aums:
        floor = deployable_floor_lakh(aum, args.position_pct,
                                      args.build_days, args.max_pct_adv)
        ok = [r for r in rows if r[3] >= floor]
        mults = sorted(r[2] for r in ok)
        med = mults[len(mults) // 2] if mults else 0
        print(f"{aum:>12.0f} {floor:>15.1f} {len(ok):>11} "
              f"{len(ok)/total*100:>11.1f}% {med:>11.1f}x")

    print("\n=== what the floor excludes (illiquid tail) ===")
    for aum in aums[:3]:
        floor = deployable_floor_lakh(aum, args.position_pct,
                                      args.build_days, args.max_pct_adv)
        lost = sorted((r for r in rows if r[3] < floor),
                      key=lambda r: -r[2])[:5]
        if lost:
            print(f"  at Rs.{aum:.0f}cr (floor {floor} lakh/d), biggest "
                  f"unbuyable winners:")
            for a, t, m, v in lost:
                print(f"      {t:12} {m:6.1f}x  traded {v:8.1f} lakh/d  ({a})")

    print("\nRead this as a business constraint, not a screen setting: the "
          "moonshot sleeve's edge lives in names that stop being buildable "
          "as AUM grows. Decide the ceiling deliberately now rather than "
          "discovering it mid-launch.")
    conn.close()


if __name__ == "__main__":
    main()
