#!/usr/bin/env python3
"""Materialise the broad company-policy discovery scan at a dated cut-off.

The expensive corpus-wide policy scan runs here on the monthly research
schedule, rather than inside every selector/report request.  The selector may
use this snapshot only on or after its ``--as-of`` date, preserving both speed
and point-in-time availability.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import psycopg2.extras


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "stock_report"))

from extract_report_data import connect  # noqa: E402
import select_stocks as selector  # noqa: E402


SCHEMA = """
CREATE TABLE IF NOT EXISTS mg_policy_company_signals (
    id BIGSERIAL PRIMARY KEY, country VARCHAR(10) NOT NULL DEFAULT 'IN',
    as_of_date DATE NOT NULL, scheme_name TEXT NOT NULL, ticker VARCHAR(32) NOT NULL,
    signal_tier VARCHAR(24) NOT NULL, first_mention_date DATE,
    n_docs_total INTEGER NOT NULL DEFAULT 0, n_last12m INTEGER NOT NULL DEFAULT 0,
    n_prior12m INTEGER NOT NULL DEFAULT 0, first_commit_date DATE,
    n_commit_docs INTEGER NOT NULL DEFAULT 0, industry TEXT,
    source_method VARCHAR(80) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, as_of_date, scheme_name, ticker)
);
CREATE INDEX IF NOT EXISTS idx_policy_company_signals_asof
    ON mg_policy_company_signals(country, as_of_date DESC, scheme_name, signal_tier);
CREATE TABLE IF NOT EXISTS mg_policy_product_discoveries (
    id BIGSERIAL PRIMARY KEY, country VARCHAR(10) NOT NULL DEFAULT 'IN',
    as_of_date DATE NOT NULL, scheme_name TEXT NOT NULL,
    product_label TEXT NOT NULL, normalized_product TEXT NOT NULL,
    source_document_count INTEGER NOT NULL DEFAULT 0,
    source_issuer_count INTEGER NOT NULL DEFAULT 0,
    first_source_date DATE, last_source_date DATE,
    source_method VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, as_of_date, scheme_name, normalized_product)
);
CREATE INDEX IF NOT EXISTS idx_policy_product_discoveries_asof
    ON mg_policy_product_discoveries(country, as_of_date DESC, normalized_product);
"""


def policy_product_discoveries(cur, as_of: date, country: str) -> list[tuple]:
    """Discover product vocabulary from dated policy-bearing company filings.

    Entity extraction is normally required to recur across multiple issuers,
    which is right for generic financial-table noise but too strict for a new
    policy-led product layer.  This retains a one-issuer phrase only after the
    filing itself matched an active, dated policy definition.  Downstream role
    extraction and human review remain separate gates.
    """
    from company_product_roles import _valid_product_label, normalized_product

    schemes = list(selector.INDIA_POLICY_SCHEMES.items())
    if not schemes:
        return []

    # Resolve the high-precision entity set first.  This avoids joining and
    # regex-scanning the entire document corpus when an older database has no
    # manufacturing-grammar PRODUCT entities yet.
    cur.execute("""
        SELECT id, COALESCE(NULLIF(BTRIM(canonical_name), ''), BTRIM(entity_text)) AS label
        FROM mg_entities
        WHERE entity_type='PRODUCT'
          AND COALESCE(metadata->>'source', '') LIKE 'generic_manufacturing_phrase_v%%'
          AND COALESCE(first_seen_at, created_at::date) <= %s
    """, (as_of,))
    entity_labels = {
        row["id"]: row["label"] for row in cur.fetchall()
        if _valid_product_label(row.get("label"))
    }
    if not entity_labels:
        return []

    flags = [f"(d.raw_text ~* %s) AS scheme_{index}"
             for index, _ in enumerate(schemes)]
    patterns = [pattern for _, pattern in schemes]
    combined = "|".join(f"(?:{pattern})" for pattern in patterns)
    cur.execute(f"""
        SELECT de.entity_id, d.id AS document_id, UPPER(TRIM(d.ticker)) AS ticker,
               d.filed_at::date AS filed_at, {', '.join(flags)}
        FROM mg_document_entities de
        JOIN mg_documents d ON d.id=de.document_id
        WHERE de.entity_id = ANY(%s) AND d.country=%s
          AND d.ticker IS NOT NULL AND BTRIM(d.ticker) <> ''
          AND d.filed_at::date <= %s AND d.raw_text ~* %s
    """, tuple(patterns + [list(entity_labels), country, as_of, combined]))

    aggregates: dict[tuple[str, str], dict] = {}
    for row in cur.fetchall():
        label = entity_labels[row["entity_id"]]
        normalized = normalized_product(label)
        for index, (scheme, _) in enumerate(schemes):
            if not row[f"scheme_{index}"]:
                continue
            bucket = aggregates.setdefault((scheme, normalized), {
                "labels": set(), "documents": set(), "issuers": set(), "dates": [],
            })
            bucket["labels"].add(label)
            bucket["documents"].add(row["document_id"])
            bucket["issuers"].add(row["ticker"])
            bucket["dates"].append(row["filed_at"])

    discovered = []
    for (scheme, normalized), bucket in aggregates.items():
        label = sorted(bucket["labels"], key=lambda value: (len(value), value.casefold()))[0]
        discovered.append((
            country, as_of, scheme, label, normalized,
            len(bucket["documents"]), len(bucket["issuers"]),
            min(bucket["dates"]), max(bucket["dates"]),
            "policy_entity_product_snapshot_v2",
        ))
    return discovered


def snapshot_company_policy_signals(
    conn,
    as_of: date,
    country: str = "IN",
    dry_run: bool = False,
) -> dict:
    """Build one dated company-policy and policy-product snapshot.

    This callable form makes the discovery stage part of both the live and
    historical pipelines instead of an optional report-time side job.
    """
    country = country.upper()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(SCHEMA)
        selector.ensure_reference_tables(cur)
        selector.INDIA_POLICY_SCHEMES.clear()
        selector.TRACKED_SCHEME_META.clear()
        cur.execute("""
            SELECT scheme_name, pattern, scheme_class, status, first_detected
            FROM mg_tracked_schemes
            WHERE status='active' AND country=%s
              AND (first_detected IS NULL OR first_detected <= %s)
        """, (country, as_of))
        for row in cur.fetchall():
            selector.INDIA_POLICY_SCHEMES[row["scheme_name"]] = row["pattern"]
        screen = selector.compute_policy_beneficiary_screen(cur, as_of, country=country)
        product_payloads = policy_product_discoveries(cur, as_of, country)
        payloads = []
        for tier, bucket in (("QUALIFIED", screen["qualified"]),
                             ("EARLY_PING", screen["early_pings"])):
            for scheme, entries in bucket.items():
                for entry in entries:
                    commitment = entry.get("commitment") or {}
                    payloads.append((
                        country, as_of, scheme, (entry.get("ticker") or "").upper(), tier,
                        entry.get("first_mention"), int(entry.get("n_docs_total") or 0),
                        int(entry.get("n_last12m") or 0), int(entry.get("n_prior12m") or 0),
                        commitment.get("first_commit"), int(commitment.get("n_commit_docs") or 0),
                        entry.get("industry"), "company_filing_policy_snapshot_v1",
                    ))
        if not dry_run:
            cur.execute("DELETE FROM mg_policy_company_signals WHERE country=%s AND as_of_date=%s",
                        (country, as_of))
            cur.execute("DELETE FROM mg_policy_product_discoveries WHERE country=%s AND as_of_date=%s",
                        (country, as_of))
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO mg_policy_company_signals
                  (country, as_of_date, scheme_name, ticker, signal_tier, first_mention_date,
                   n_docs_total, n_last12m, n_prior12m, first_commit_date, n_commit_docs,
                   industry, source_method)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, payloads, page_size=500)
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO mg_policy_product_discoveries
                  (country, as_of_date, scheme_name, product_label, normalized_product,
                   source_document_count, source_issuer_count, first_source_date,
                   last_source_date, source_method)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, product_payloads, page_size=500)
    if not dry_run:
        conn.commit()
    return {
        "as_of": str(as_of),
        "company_policy_signals": len(payloads),
        "policy_product_discoveries": len(product_payloads),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--country", default="IN")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    country = args.country.upper()
    conn = connect()
    try:
        stats = snapshot_company_policy_signals(
            conn, args.as_of, country=country, dry_run=args.dry_run,
        )
    finally:
        conn.close()
    print(f"{'Would snapshot' if args.dry_run else 'Snapshotted'} "
          f"{stats['company_policy_signals']} company-policy signals and "
          f"{stats['policy_product_discoveries']} policy-product discoveries as of {args.as_of}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
