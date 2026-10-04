#!/usr/bin/env python3
"""Record an optional point-in-time human override on a company product role.

Routine exact-role promotion is automatic.  This command remains for governance:
a dated REJECTED decision vetoes a machine false positive, while APPROVED records
an optional analyst attestation but is not required for promotion.  The decision
date is always today and is never backfilled into a historical run.

Example:
    python scripts/stock_report/review_company_role.py \
      --ticker ABC --product "example component" --role MANUFACTURER \
      --status APPROVED --evidence-through 2026-07-15 --reviewer analyst@firm \
      --note "Verified two issuer-owned plant disclosures and product scope." \
      --source-url https://example.com/source --source-title "Issuer annual report"
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from company_product_roles import (  # noqa: E402
    EXTRACTION_METHOD,
    ensure_table,
    normalized_product,
)
from extract_report_data import connect  # noqa: E402


ROLE_TYPES = (
    "MANUFACTURER", "EPC_OR_INSTALLER", "SYSTEM_INTEGRATOR", "INPUT_SUPPLIER",
    "DIRECT_ROLE_UNCLASSIFIED",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="IN")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--product", required=True,
                        help="literal product label from the company-role ledger or bounded exception queue")
    parser.add_argument("--role", required=True, choices=ROLE_TYPES)
    parser.add_argument("--status", required=True, choices=("APPROVED", "REJECTED"))
    parser.add_argument("--evidence-through", required=True, type=date.fromisoformat)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument("--source-url")
    parser.add_argument("--source-title")
    args = parser.parse_args()
    if args.evidence_through > date.today():
        parser.error("--evidence-through cannot be after the review date")

    country, ticker = args.country.upper(), args.ticker.upper()
    product = normalized_product(args.product)
    if not product:
        parser.error("--product must contain a product label")

    conn = connect()
    try:
        ensure_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT 1
                FROM mg_company_product_roles
                WHERE country=%s AND ticker=%s AND normalized_product=%s
                  AND role_type=%s AND role_state='EVIDENCED'
                  AND extraction_method=%s
                  AND first_evidence_date <= %s
                LIMIT 1
            """, (country, ticker, product, args.role, EXTRACTION_METHOD,
                  args.evidence_through))
            if not cur.fetchone():
                parser.error(
                    "no evidenced extracted role exists by --evidence-through; review the queue or rebuild the role ledger first"
                )
            cur.execute("""
                INSERT INTO mg_company_role_reviews
                  (country, ticker, normalized_product, role_type, review_status,
                   evidence_through_date, decision_available_from, reviewer,
                   review_note, source_url, source_title)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE, %s, %s, %s, %s)
                ON CONFLICT (country, ticker, normalized_product, role_type, decision_available_from)
                DO UPDATE SET review_status=EXCLUDED.review_status,
                              evidence_through_date=EXCLUDED.evidence_through_date,
                              reviewer=EXCLUDED.reviewer, review_note=EXCLUDED.review_note,
                              source_url=EXCLUDED.source_url, source_title=EXCLUDED.source_title,
                              updated_at=NOW()
            """, (country, ticker, product, args.role, args.status, args.evidence_through,
                  args.reviewer, args.note, args.source_url, args.source_title))
        conn.commit()
    finally:
        conn.close()
    print(
        f"Recorded optional {args.status} override for {ticker} / {product} / {args.role}; "
        "it is available only from today's decision date. Routine promotion remains automatic."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
