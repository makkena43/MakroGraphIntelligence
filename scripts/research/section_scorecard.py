#!/usr/bin/env python3
"""
Section scorecard — every section of the report measured the SAME way, plus
which constraints actually paid.

Everything here is one anchor set, one horizon, one benchmark, so the numbers
are comparable to each other. Prior measurements in this project were run
piecemeal (different anchors, different cohorts) and were not.

PART 1 — sections: for each report section, the forward return of everything
it would have surfaced, against a point-in-time market proxy.
PART 2 — constraints: forward return by CONSTRAINT, which is the question
that actually matters. Stock selection sits downstream of constraint choice;
if a constraint is wrong, no amount of screening inside it helps.

Point-in-time throughout; returns graded after the fact with the alias-aware,
right-censored, outlier-guarded calculator.
"""
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
sys.path.insert(0, HERE)

from extract_report_data import connect                                  # noqa: E402
from moonshot_screen import compute_moonshot_candidates, alias_map        # noqa: E402
from company_capabilities import capability_makers                        # noqa: E402
from select_stocks import (compute_policy_beneficiary_screen,             # noqa: E402
                           compute_pli_shortlist)
from winner_autopsy import fwd_return, ANCHORS                            # noqa: E402

HORIZON = 1095      # 3 years


def med(v):
    return statistics.median(v) if v else None


def show(label, vals, bench_med):
    if not vals:
        print(f"  {label:38} n=0")
        return
    m, mn = med(vals), sum(vals) / len(vals)
    edge = "" if bench_med is None else f"   vs bench {m - bench_med:+6.1f}pp"
    print(f"  {label:38} median {m:7.1f}%  mean {mn:7.1f}%  n={len(vals):4d}{edge}")


def main():
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    al = alias_map(cur)

    sec = defaultdict(list)
    by_constraint = defaultdict(list)
    bench_all = []

    for astr in ANCHORS:
        d = date.fromisoformat(astr)

        # benchmark: point-in-time top-25 by traded value
        cur.execute("""SELECT symbol FROM nse_bhavcopy_data WHERE series IN ('EQ','BE')
                       AND trade_date BETWEEN %s AND %s GROUP BY symbol
                       ORDER BY SUM(tottrdval) DESC LIMIT 25""",
                    (d - timedelta(days=365), d))
        bench_all += [x for x in (fwd_return(cur, al, r["symbol"], d, HORIZON)
                                  for r in cur.fetchall()) if x is not None]

        # 1. MOONSHOT
        for c in compute_moonshot_candidates(cur, d, top_n=40):
            r = fwd_return(cur, al, c["ticker"], d, HORIZON)
            if r is not None:
                sec["1. Moonshot sleeve"].append(r)

        # 2. PLI SHORTLIST (mechanical)
        try:
            pol = compute_policy_beneficiary_screen(cur, d, country="IN")
            for p in compute_pli_shortlist(pol):
                r = fwd_return(cur, al, p["ticker"], d, HORIZON)
                if r is None:
                    continue
                sec["2. PLI shortlist (all)"].append(r)
                if p.get("committed"):
                    sec["2b. PLI shortlist — committed only"].append(r)
        except Exception as e:
            print(f"    (PLI screen failed at {astr}: {e})", file=sys.stderr)

        # 3. CONSTRAINT MAKERS + per-constraint attribution
        cur.execute("""SELECT MAX(as_of_date) s FROM mg_india_beneficiaries
                       WHERE as_of_date <= %s""", (d,))
        row = cur.fetchone()
        if row and row["s"]:
            snap = row["s"]
            cur.execute("""SELECT DISTINCT constrained_product p, UPPER(TRIM(ticker)) t
                           FROM mg_india_beneficiaries
                           WHERE as_of_date=%s AND constrained_product IS NOT NULL""",
                        (snap,))
            prods, mapped_by_prod = set(), defaultdict(set)
            for r_ in cur.fetchall():
                prods.add(r_["p"])
                if r_["t"]:
                    mapped_by_prod[r_["p"]].add(r_["t"])

            mk = capability_makers(cur, d, sorted(prods))
            for prod, info in mk.items():
                for m in info["makers"]:
                    r = fwd_return(cur, al, m["ticker"], d, HORIZON)
                    if r is None:
                        continue
                    key = ("3. Constraint makers — clean" if not m["needs_review"]
                           else "3b. Constraint makers — flagged")
                    sec[key].append(r)
                    if not m["needs_review"]:
                        by_constraint[prod].append(r)
            # existing mapped beneficiaries, for contrast
            for prod, ticks in mapped_by_prod.items():
                for t in list(ticks)[:40]:
                    r = fwd_return(cur, al, t, d, HORIZON)
                    if r is not None:
                        sec["4. Existing beneficiary mapper"].append(r)
        print(f"  scored {astr}", flush=True)

    bm = med(bench_all)
    print(f"\n{'='*78}\nPART 1 — SECTION SCORECARD  ({HORIZON//365}-year forward, "
          f"{len(ANCHORS)} anchors)\n{'='*78}")
    show("BENCHMARK (top-25 traded value)", bench_all, None)
    print()
    for k in sorted(sec):
        show(k, sec[k], bm)

    print(f"\n{'='*78}\nPART 2 — WHICH CONSTRAINTS ACTUALLY PAID\n{'='*78}")
    rows = [(med(v), len(v), k) for k, v in by_constraint.items() if len(v) >= 5]
    for m, n, k in sorted(rows, reverse=True):
        flag = "  <-- beats benchmark" if bm is not None and m > bm else ""
        print(f"  {k[:46]:48} median {m:7.1f}%  n={n:3d}{flag}")
    if not rows:
        print("  (no constraint has >=5 scored makers)")
    conn.close()


if __name__ == "__main__":
    main()
