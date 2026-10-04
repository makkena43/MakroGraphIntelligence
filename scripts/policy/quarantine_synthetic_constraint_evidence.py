#!/usr/bin/env python3
"""Quarantine legacy scheme-restamped constraint evidence without deleting it.

The operation is recoverable: the complete original evidence row is copied to
``mg_constraint_evidence_quarantine`` before its decision admissibility is
disabled. The linked ledger remains available for audit but is excluded from
investment authority until rebuilt from immutable accepted observations.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_constraint_ledger import connect, ensure_schema  # noqa: E402


SYNTHETIC_PREDICATE = """
    LOWER(COALESCE(e.source_url, '')) LIKE 'scheme:%'
    OR LOWER(COALESCE(e.value_unit, '')) LIKE '%scheme-derived%'
"""


def find_rows(cur) -> list[dict]:
    cur.execute(f"""
        SELECT e.id AS evidence_id, e.ledger_id, l.constraint_key,
               l.as_of_date, e.evidence_type, e.source_date, e.source_url,
               e.value_numeric, e.value_unit, e.admissibility_status
        FROM mg_constraint_ledger_evidence e
        JOIN mg_constraint_ledgers l ON l.id=e.ledger_id
        WHERE {SYNTHETIC_PREDICATE}
        ORDER BY l.constraint_key, l.as_of_date, e.id
    """)
    return [dict(row) for row in cur.fetchall()]


def quarantine(cur) -> tuple[int, int]:
    cur.execute(f"""
        INSERT INTO mg_constraint_evidence_quarantine
          (evidence_id, ledger_id, reason, original_payload)
        SELECT e.id, e.ledger_id,
               'scheme-restamped or scheme-derived magnitude cannot establish a physical constraint',
               to_jsonb(e)
        FROM mg_constraint_ledger_evidence e
        WHERE {SYNTHETIC_PREDICATE}
        ON CONFLICT (evidence_id) DO NOTHING
    """)
    copied = cur.rowcount
    cur.execute(f"""
        UPDATE mg_constraint_ledger_evidence e
        SET admissibility_status='QUARANTINED',
            quarantine_reason='scheme-restamped or scheme-derived magnitude',
            is_primary=FALSE
        WHERE {SYNTHETIC_PREDICATE}
          AND admissibility_status <> 'QUARANTINED'
    """)
    quarantined = cur.rowcount
    cur.execute(f"""
        UPDATE mg_constraint_ledgers l
        SET investment_eligibility='EXCLUDED',
            derivation_method='QUARANTINED_SYNTHETIC',
            physical_state='STALE', trajectory='UNKNOWN',
            evidence_completeness='UNMEASURED', updated_at=NOW(),
            review_note=CONCAT_WS(' ', l.review_note,
                'Decision authority removed: legacy scheme-restamped evidence was quarantined.')
        WHERE EXISTS (
            SELECT 1 FROM mg_constraint_ledger_evidence e
            WHERE e.ledger_id=l.id AND ({SYNTHETIC_PREDICATE})
        )
          AND NOT EXISTS (
            SELECT 1 FROM mg_constraint_ledger_evidence valid
            WHERE valid.ledger_id=l.id
              AND valid.admissibility_status='ADMISSIBLE' AND valid.is_primary
              AND valid.evidence_type IN ('SUPPLY','IMPORT')
              AND valid.value_numeric IS NOT NULL
          )
    """)
    return copied, quarantined


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply recoverable quarantine; default is audit only")
    args = parser.parse_args()
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_schema(cur)
            rows = find_rows(cur)
            print(f"Synthetic constraint evidence found: {len(rows)}")
            for row in rows[:100]:
                print(
                    f"  evidence={row['evidence_id']} {row['constraint_key']} "
                    f"snapshot={row['as_of_date']} source={row['source_url']}"
                )
            if not args.apply:
                conn.rollback()
                print("Audit only. Re-run with --apply to quarantine; no rows changed.")
                return 0
            copied, changed = quarantine(cur)
        conn.commit()
    print(f"Quarantined {changed} evidence rows; {copied} new audit copies retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
