#!/usr/bin/env python3
"""Compute a future target build requirement without calling it a current gap.

WHY THIS EXISTS
---------------
The historical capacity backfill (real, cited domestic-production figures from
MNRE / Mercom / GTRI etc.) stored a ``domestic_capacity`` numerator but left
``gap_pct`` NULL, because those sources report *capacity*, not a gap. A NULL
gap_pct means the constraint grader's magnitude leg (``quantified_gap``) can
never fire for that product, so a constraint like Solar Cell — which has real
listed makers and an as-of order-book demand signal — is stuck at UNMEASURED
and can never reach the A/B grade the Early/Timing lane requires.

A gap needs a *required* quantity as the denominator. The India capacity engine
(src/makrograph/india/capacity_engine.py) already derives that requirement from
dated government policy targets (e.g. 280 GW solar by 2030 -> 302.4 GW required
cell capacity via the documented engineering ratio), and those engine rows are
already in mg_capacity_gaps. This step pairs that policy-target-derived
requirement with each cited point-in-time domestic-capacity observation and
stores the resulting number in ``forward_target_gap_pct``. It deliberately
leaves ``gap_pct`` NULL because a future policy target is not current demand.

WHAT MAKES THIS POINT-IN-TIME SAFE (not a look-ahead)
-----------------------------------------------------
- The numerator (domestic_capacity) is the row's own cited figure, with its own
  as_of_date and source_published_at preserved untouched. The grader's
  ``gap_source_ready`` check still requires source_published_at <= the report
  anchor, so a capacity figure only *published* after an anchor (e.g. a Dec-2025
  number reported in Mar-2026) correctly stays invisible at that anchor.
- The denominator is a policy target that predates every anchor here (the
  280 GW-by-2030 / 50 GWh ACC targets were announced 2021-2022). It is constant
  across anchors, so it introduces no future information.
- gap_pct is arithmetic on those two real, dated inputs. ingestion_method
  records the hybrid derivation so an auditor sees exactly how it was formed.

NO HARDCODING: the required denominator is read from the engine-computed rows
already in mg_capacity_gaps (per component); nothing about which product maps to
which requirement is written here. A component with no engine-derived
requirement is skipped (reported, never guessed).

USAGE
-----
    python scripts/policy/compute_capacity_gap_pct.py [--dry-run]
"""
import argparse
import os
import sys
from datetime import date

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
from extract_report_data import connect  # noqa: E402


def ensure_columns(cur) -> None:
    cur.execute("""
        ALTER TABLE mg_capacity_gaps
          ADD COLUMN IF NOT EXISTS measurement_basis TEXT DEFAULT 'UNKNOWN',
          ADD COLUMN IF NOT EXISTS forward_target_gap NUMERIC,
          ADD COLUMN IF NOT EXISTS forward_target_gap_pct NUMERIC
    """)


def required_by_component(cur) -> dict[str, dict]:
    """Latest policy-target-derived requirement per component, from the engine
    rows already in the table (those carry required_quantity + unit)."""
    cur.execute("""
        SELECT DISTINCT ON (component) component, required_quantity, unit, target_year
        FROM mg_capacity_gaps
        WHERE required_quantity IS NOT NULL
          AND COALESCE(measurement_basis, 'UNKNOWN') = 'FUTURE_POLICY_TARGET'
        ORDER BY component, as_of_date DESC
    """)
    return {r["component"]: r for r in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    ensure_columns(cur)
    req = required_by_component(cur)

    # Cited capacity observations that carry a real domestic_capacity but no gap.
    cur.execute("""
        SELECT id, component, as_of_date, domestic_capacity, unit,
               source_title, source_published_at
        FROM mg_capacity_gaps
        WHERE provenance_status = 'PRIMARY_SOURCE'
          AND domestic_capacity IS NOT NULL
          AND (gap_pct IS NULL OR
               ingestion_method = 'CITED_CAPACITY_VS_POLICY_TARGET_REQUIREMENT')
        ORDER BY component, as_of_date
    """)
    rows = cur.fetchall()
    updated, skipped = 0, []
    for r in rows:
        comp = r["component"]
        rq = req.get(comp)
        if not rq or not rq["required_quantity"] or float(rq["required_quantity"]) <= 0:
            skipped.append((comp, r["as_of_date"], "no policy-target requirement for component"))
            continue
        required = float(rq["required_quantity"])
        domestic = float(r["domestic_capacity"])
        gap = max(required - domestic, 0.0)
        gap_pct = round(gap / required * 100.0, 1)
        print(f"  {comp:20} as_of {r['as_of_date']}: domestic {domestic} / future target {required} "
              f"-> forward_target_gap_pct {gap_pct}% (not current binding gap)")
        if args.dry_run:
            continue
        cur.execute("""
            UPDATE mg_capacity_gaps
            SET required_quantity = %s,
                forward_target_gap = %s,
                forward_target_gap_pct = %s,
                gap = NULL,
                gap_pct = NULL,
                measurement_basis = 'FUTURE_POLICY_TARGET',
                ingestion_method = 'CITED_CAPACITY_VS_FUTURE_POLICY_TARGET'
            WHERE id = %s
        """, (round(required, 2), round(gap, 2), gap_pct, r["id"]))
        updated += 1
    if not args.dry_run:
        conn.commit()
    print(f"\n{'DRY-RUN: would update' if args.dry_run else 'Updated'} {updated if not args.dry_run else len(rows)-len(skipped)} rows; skipped {len(skipped)}")
    for comp, d, why in skipped:
        print(f"  skipped {comp} {d}: {why}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
