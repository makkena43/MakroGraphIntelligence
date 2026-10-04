#!/usr/bin/env python3
"""Retired unsafe promoter for scheme-derived constraint magnitudes.

This command previously copied one unverified capacity/import estimate onto
successive year-end dates and used a policy scheme to supply binding demand.
That destroys point-in-time provenance and can make stale or future-target data
look like a current physical constraint. The write path is intentionally gone.

Use ``queue_constraint_observations.py`` followed by
``materialize_constraint_ledger.py``. Only accepted, exact-product,
primary-source observations can create a physical measurement there.

``--audit`` is read-only and reports legacy evidence requiring quarantine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_constraint_ledger import connect  # noqa: E402


DEPRECATION_MESSAGE = (
    "DEPRECATED: scheme-derived magnitudes cannot be promoted to physical "
    "constraint evidence. Run queue_constraint_observations.py and "
    "materialize_constraint_ledger.py instead."
)


def audit_rows(cur) -> list[dict]:
    cur.execute("""
        SELECT e.id AS evidence_id, e.ledger_id, l.constraint_key,
               l.as_of_date, e.evidence_type, e.source_date, e.source_url,
               e.value_numeric, e.value_unit
        FROM mg_constraint_ledger_evidence e
        JOIN mg_constraint_ledgers l ON l.id=e.ledger_id
        WHERE LOWER(COALESCE(e.source_url, '')) LIKE 'scheme:%'
           OR LOWER(COALESCE(e.value_unit, '')) LIKE '%scheme-derived%'
        ORDER BY l.constraint_key, l.as_of_date, e.id
    """)
    return [dict(row) for row in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true", help="read-only legacy contamination audit")
    # Keep the old option parseable for automation, but it no longer enables a
    # write or recreates annual refresh rows.
    parser.add_argument("--dry-run", action="store_true", help="alias for --audit")
    args = parser.parse_args()
    if not (args.audit or args.dry_run):
        print(DEPRECATION_MESSAGE, file=sys.stderr)
        return 2
    with connect() as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            rows = audit_rows(cur)
    print(DEPRECATION_MESSAGE)
    print(f"Legacy synthetic evidence rows requiring quarantine: {len(rows)}")
    for row in rows[:100]:
        print(
            f"  evidence={row['evidence_id']} ledger={row['ledger_id']} "
            f"{row['constraint_key']} {row['as_of_date']} {row['evidence_type']} "
            f"{row['source_url']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
