#!/usr/bin/env python3
"""
Multi-anchor validation of the constraint-scoped maker lists.

WHAT IS BEING TESTED
--------------------
Not "does the mapper find names" -- it obviously does -- but "do the names it
finds actually go up, and does the industry-mismatch flag separate the good
ones from the noise". A single anchor (2022-12-31) suggested yes, strongly:
clean makers +75.0% median at 36m vs -1.1% for flagged ones. That was n=18
on one date, which is suggestive and nothing more. This repeats it across
every anchor in the cohort window.

WHY THIS PATH AND NOT THE GLOBAL ONE
------------------------------------
The identical capability data, poured into the moonshot screen as a blanket
universe expansion, produced ZERO extra winners on 951 winner-instances --
measured twice. Scoped to constraints the judgment layer has already
selected, it looks completely different. If that difference is real it should
survive across anchors; if it was one lucky quarter, this run will say so.

POINT-IN-TIME
-------------
Constraints at each anchor come from the beneficiary snapshot as of that date
and makers from the capability snapshot as of that date -- no forward
knowledge enters selection. Returns are graded strictly afterwards using the
alias-aware, right-censored, outlier-guarded calculator.

USAGE
-----
    python scripts/research/maker_validation.py
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

from extract_report_data import connect                    # noqa: E402
from company_capabilities import capability_makers          # noqa: E402
from moonshot_screen import alias_map                       # noqa: E402
from winner_autopsy import fwd_return, ANCHORS              # noqa: E402

HORIZONS = (("12m", 365), ("24m", 730), ("36m", 1095))


def constraints_at(cur, as_of):
    cur.execute("""SELECT MAX(as_of_date) AS s FROM mg_india_beneficiaries
                   WHERE as_of_date <= %s""", (as_of,))
    row = cur.fetchone()
    if not row or not row["s"]:
        return [], set()
    snap = row["s"]
    cur.execute("""SELECT DISTINCT constrained_product AS p,
                          UPPER(TRIM(ticker)) AS t
                   FROM mg_india_beneficiaries
                   WHERE as_of_date = %s AND constrained_product IS NOT NULL""",
                (snap,))
    prods, mapped = set(), set()
    for r in cur.fetchall():
        prods.add(r["p"])
        if r["t"]:
            mapped.add(r["t"])
    return sorted(prods), mapped


def med(xs):
    return statistics.median(xs) if xs else None


def main():
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aliases = alias_map(cur)

    buckets = {"clean": defaultdict(list), "flagged": defaultdict(list)}
    per_anchor = []

    for astr in ANCHORS:
        as_of = date.fromisoformat(astr)
        prods, mapped = constraints_at(cur, as_of)
        if not prods:
            continue
        makers = capability_makers(cur, as_of, prods)
        clean, flagged = set(), set()
        for info in makers.values():
            for m in info["makers"]:
                t = m["ticker"]
                if t in mapped:
                    continue          # already in the beneficiary universe
                (flagged if m["needs_review"] else clean).add(t)
        # a name flagged under one constraint and clean under another counts
        # as clean; the flag is a per-constraint mismatch, not a verdict
        flagged -= clean

        row = {"anchor": astr, "constraints": len(prods),
               "clean": len(clean), "flagged": len(flagged)}
        for label, group in (("clean", clean), ("flagged", flagged)):
            for h, days in HORIZONS:
                rs = [x for x in (fwd_return(cur, aliases, t, as_of, days)
                                  for t in group) if x is not None]
                buckets[label][h].extend(rs)
                row[f"{label}_{h}"] = med(rs)
        per_anchor.append(row)
        print(f"  {astr}: {len(prods):2d} constraints | "
              f"clean {len(clean):3d} (36m med "
              f"{row['clean_36m'] if row['clean_36m'] is not None else 'n/a'}) | "
              f"flagged {len(flagged):3d} (36m med "
              f"{row['flagged_36m'] if row['flagged_36m'] is not None else 'n/a'})",
              flush=True)

    print("\n=== POOLED ACROSS ALL ANCHORS ===")
    print(f"{'group':10} " + " ".join(f"{h:>18}" for h, _ in HORIZONS))
    for label in ("clean", "flagged"):
        cells = []
        for h, _ in HORIZONS:
            rs = buckets[label][h]
            m = med(rs)
            cells.append(f"{m:8.1f}% (n={len(rs):4d})" if m is not None
                         else f"{'n/a':>18}")
        print(f"{label:10} " + " ".join(f"{c:>18}" for c in cells))

    print("\n=== anchors where clean beat flagged at 36m ===")
    wins = sum(1 for r in per_anchor
               if r["clean_36m"] is not None and r["flagged_36m"] is not None
               and r["clean_36m"] > r["flagged_36m"])
    comparable = sum(1 for r in per_anchor
                     if r["clean_36m"] is not None and r["flagged_36m"] is not None)
    print(f"  {wins}/{comparable} anchors")
    print("\nIf clean consistently beats flagged the industry-mismatch flag is "
          "doing real work and must be respected in the judgment layer. If the "
          "two are indistinguishable, the flag is cosmetic and the maker lists "
          "need a different quality gate before they inform any decision.")
    conn.close()


if __name__ == "__main__":
    main()
