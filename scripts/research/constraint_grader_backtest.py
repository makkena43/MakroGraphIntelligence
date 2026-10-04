#!/usr/bin/env python3
"""
Constraint-grading backtest — which measurable properties of a constraint
actually predict its forward return?

WHY THIS IS THE ONLY MEASUREMENT THAT MATTERS
---------------------------------------------
The section scorecard (scripts/research/section_scorecard.py, 11 anchors,
3-year forward) found:

  spread between BEST and WORST constraint      242 pp
      Defense electronics +227.3%  ...  Solar Module -14.8%
  spread between BEST and WORST stock-selection  ~40 pp
      beneficiary mapper +65.4%  ...  moonshot sleeve +46.8%

Constraint choice dominates stock choice by roughly six to one. And a
deliberately sloppy basket -- the existing mapper, which files a staffing
firm and a cement company under Defense electronics -- still beat the
benchmark by 28pp, because it had exposure to constraints that worked.
So the leverage is entirely upstream, and the grading framework that picks
constraints has never been tested.

WHAT IS TESTED
--------------
Not the judgment grades themselves: those exist for only five dates and were
authored in Jul-2026 with hindsight. Instead this tests the OBSERVABLE
features a grader could use at the time, each computed point-in-time:

  gap_quantified        a capacity gap with a real number attached
  gap_pct               size of that gap
  import_share          import dependence
  import_concentrated   dependence on one origin country
  order_book_share      share of mapped beneficiaries showing order-book evidence
  avg_conviction        mapper conviction (tests the mapper's own signal)
  n_companies           how broad the mapped cohort is
  breadth_narrow        few listed players = scarcity of vehicles

against the constraint's forward basket return. A feature earns its place in
the grading framework only if constraints scoring high on it measurably beat
constraints scoring low.

Point-in-time: features read only as-of data; returns graded afterwards.
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

from extract_report_data import connect            # noqa: E402
from moonshot_screen import alias_map              # noqa: E402
from company_capabilities import match_component   # noqa: E402
from winner_autopsy import fwd_return, ANCHORS     # noqa: E402

HORIZON = 1095
MIN_BASKET = 4          # constraints with fewer scored names are not evidence


def features_and_return(cur, al, as_of):
    """One row per constraint at this anchor: its point-in-time features and
    the forward return of its mapped beneficiary basket."""
    cur.execute("""SELECT MAX(as_of_date) s FROM mg_india_beneficiaries
                   WHERE as_of_date <= %s""", (as_of,))
    row = cur.fetchone()
    if not row or not row["s"]:
        return []
    snap = row["s"]

    cur.execute("""
        SELECT constrained_product AS p,
               COUNT(*)                                        AS n_companies,
               AVG(conviction_score)                           AS avg_conv,
               AVG(CASE WHEN has_order_book_signals THEN 1 ELSE 0 END) AS ob_share,
               AVG(CASE WHEN import_substitution_play THEN 1 ELSE 0 END) AS imp_share,
               ARRAY_AGG(DISTINCT UPPER(TRIM(ticker)))         AS ticks
        FROM mg_india_beneficiaries
        WHERE as_of_date = %s AND constrained_product IS NOT NULL
        GROUP BY 1
    """, (snap,))
    base = cur.fetchall()

    # Capacity gap / import dependence.
    #
    # LOOK-AHEAD CAVEAT, stated loudly: both tables hold exactly ONE snapshot
    # (2026-07-06), so a point-in-time filter returns nothing for any 2020-23
    # anchor -- which is why these two legs previously reached the grader on
    # ZERO of 86 constraint-anchors. They are used here as STATIC structural
    # reference, i.e. the 2026 assessment applied to earlier anchors. That is
    # mild look-ahead: import dependence on polysilicon or a CRGO capacity
    # gap are slow-moving structural facts, but the assessment itself was
    # written knowing how the period turned out. Treat any signal below as
    # INDICATIVE ONLY until these tables carry real history.
    cur.execute("SELECT component, gap_pct, severity FROM mg_capacity_gaps")
    grows = cur.fetchall()
    cur.execute("SELECT component, import_share, primary_origin FROM mg_import_dependencies")
    irows = cur.fetchall()
    gnames = [r["component"] for r in grows]
    inames = [r["component"] for r in irows]
    gaps = {r["component"]: r for r in grows}
    imps = {r["component"]: r for r in irows}

    out = []
    for b in base:
        rets = [x for x in (fwd_return(cur, al, t, as_of, HORIZON)
                            for t in (b["ticks"] or [])[:60]) if x is not None]
        if len(rets) < MIN_BASKET:
            continue
        gm = match_component(b["p"], gnames)
        im_name = match_component(b["p"], inames)
        g = gaps.get(gm) if gm else None
        im = imps.get(im_name) if im_name else None
        origins = (im["primary_origin"] or "") if im else ""
        out.append({
            "anchor": as_of, "constraint": b["p"],
            "fwd": statistics.median(rets), "n": len(rets),
            "gap_quantified": bool(g and g["gap_pct"] is not None),
            "gap_pct": float(g["gap_pct"]) if g and g["gap_pct"] is not None else None,
            "import_share": float(im["import_share"]) if im and im["import_share"] is not None else None,
            "import_concentrated": bool(origins) and len(origins.split(",")) <= 2,
            "order_book_share": float(b["ob_share"] or 0),
            "avg_conviction": float(b["avg_conv"] or 0),
            "n_companies": int(b["n_companies"] or 0),
        })
    return out


def split_report(rows, label, keyfn):
    """Median forward return of constraints scoring HIGH vs LOW on a feature."""
    hi = [r["fwd"] for r in rows if keyfn(r) is True]
    lo = [r["fwd"] for r in rows if keyfn(r) is False]
    if len(hi) < 3 or len(lo) < 3:
        print(f"  {label:34} insufficient split (hi={len(hi)} lo={len(lo)})")
        return
    mh, ml = statistics.median(hi), statistics.median(lo)
    verdict = "PREDICTS" if mh - ml > 15 else ("inverse" if ml - mh > 15 else "no signal")
    print(f"  {label:34} high {mh:7.1f}% (n={len(hi):3d})   "
          f"low {ml:7.1f}% (n={len(lo):3d})   spread {mh-ml:+7.1f}pp   {verdict}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchors", default=None,
                    help="comma-separated anchor dates; default = the module list. "
                         "Use the DISTINCT beneficiary-snapshot dates only: quarterly "
                         "anchors all fall back to the same yearly snapshot, which "
                         "pseudo-replicates rows and inflates n.")
    args = ap.parse_args()
    anchors = ([a.strip() for a in args.anchors.split(",")] if args.anchors
               else ANCHORS)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    al = alias_map(cur)

    rows = []
    for a in anchors:
        rows += features_and_return(cur, al, date.fromisoformat(a))
        print(f"  scored {a}", flush=True)

    if not rows:
        print("no constraint rows")
        return
    allf = [r["fwd"] for r in rows]
    print(f"\n{'='*92}\nCONSTRAINT-GRADING BACKTEST — {len(rows)} constraint-anchors, "
          f"3-year forward\n{'='*92}")
    print(f"  overall constraint median: {statistics.median(allf):.1f}%   "
          f"best {max(allf):.1f}%   worst {min(allf):.1f}%\n")

    med_conv = statistics.median(r["avg_conviction"] for r in rows)
    med_ob = statistics.median(r["order_book_share"] for r in rows)
    med_n = statistics.median(r["n_companies"] for r in rows)

    print("  FEATURE                            constraints scoring HIGH vs LOW")
    print("  " + "-"*88)
    split_report(rows, "quantified capacity gap", lambda r: r["gap_quantified"])
    split_report(rows, "import dependence known", lambda r: r["import_share"] is not None)
    split_report(rows, "import origin concentrated", lambda r: r["import_concentrated"])
    split_report(rows, "order-book evidence (>median)",
                 lambda r: r["order_book_share"] > med_ob)
    split_report(rows, "mapper conviction (>median)",
                 lambda r: r["avg_conviction"] > med_conv)
    split_report(rows, "NARROW cohort (<median companies)",
                 lambda r: r["n_companies"] < med_n)

    print(f"\n  {'-'*88}\n  PER-CONSTRAINT (pooled across anchors)")
    byc = defaultdict(list)
    for r in rows:
        byc[r["constraint"]].append(r["fwd"])
    for k, v in sorted(byc.items(), key=lambda kv: -statistics.median(kv[1])):
        print(f"    {k[:50]:52} median {statistics.median(v):7.1f}%  anchors={len(v)}")
    conn.close()


if __name__ == "__main__":
    main()
