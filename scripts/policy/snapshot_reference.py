#!/usr/bin/env python3
"""
Audit structural constraint references at a run date without manufacturing
history by restamping stale estimates.

WHY
---
mg_capacity_gaps and mg_import_dependencies held exactly one snapshot
(2026-07-06). A point-in-time backtest therefore found nothing at any past
anchor, so two of the four constraint-grading legs could never be validated
(the constraint-grader backtest showed them reaching the grader on ZERO of 86
anchors until a look-ahead workaround was used). A grade you cannot backtest
is a grade you cannot trust — which is why only "scarcity of listed vehicles"
survived testing.

Older versions cloned the latest capacity/import row under every run date.
That changed neither the measurement nor its source, but made it look fresh.
It also refreshed import *value* from trade flows while carrying forward a
stale import-*share*, which is not a valid import-dependence measurement.

This command is now deliberately read-only. It reports source-qualified rows
already observed by the run date and trade-flow coverage that can feed a real
import-share computation. A new dated physical row must be written by an
ingestor that supplies its primary URL, publication date, source family and
measurement date. Missing data stays missing.

NO HARDCODING: reads components/HS codes from the existing rows; nothing about
which product maps to which code is written here (that mapping already lives
in the table rows).

USAGE
-----
    python scripts/policy/snapshot_reference.py --as-of 2026-08-31
    python scripts/policy/snapshot_reference.py --as-of 2026-08-31 --dry-run
"""
import argparse
import os
import sys
from datetime import date, timedelta

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
from extract_report_data import connect  # noqa: E402


def latest_import_value_by_hs(cur, as_of, months=12):
    """Trailing-12m total India import value per HS code, from real trade data
    (mg_trade_flows), as of the run date. Returns {hs_code: value_usd}."""
    since = f"{(as_of - timedelta(days=months*31)).year}{(as_of - timedelta(days=months*31)).month:02d}"
    upto = f"{as_of.year}{as_of.month:02d}"
    cur.execute("""
        SELECT hs_code, SUM(value_usd) AS v
        FROM mg_trade_flows
        WHERE reporter_country='India' AND flow_direction='import'
          AND period >= %s AND period <= %s
        GROUP BY hs_code
    """, (since, upto))
    return {r["hs_code"]: float(r["v"] or 0) for r in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of)

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    trade = latest_import_value_by_hs(cur, as_of)
    print(f"trade-flow HS codes with fresh import value: {len(trade)}")
    for table in ("mg_capacity_gaps", "mg_import_dependencies"):
        cur.execute(f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (
                     WHERE provenance_status='PRIMARY_SOURCE'
                       AND source_url IS NOT NULL
                       AND source_published_at <= %s
                       AND as_of_date <= %s
                   ) AS source_ready,
                   MAX(as_of_date) FILTER (WHERE as_of_date <= %s) AS latest_observation
            FROM {table}
        """, (as_of, as_of, as_of))
        row = cur.fetchone()
        print(
            f"{table}: source-qualified={row['source_ready']}/{row['total']}, "
            f"latest observation by run date={row['latest_observation']}"
        )
    print(
        "No rows written. Trade value alone does not establish import dependence; "
        "stale capacity/import estimates were not restamped."
    )
    conn.close()


if __name__ == "__main__":
    main()
