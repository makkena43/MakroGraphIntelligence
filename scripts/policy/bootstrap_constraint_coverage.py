#!/usr/bin/env python3
"""Maintain complete, point-in-time constraint coverage for the mapper universe.

This is deliberately a coverage tool, not an evidence seeder.  It makes every
currently mapped product visible in a reviewable alias table and creates a
DISCOVERY/WATCH ledger row only where a chain has no ledger record at all.
It never creates ledger evidence or changes an investment gate.

Usage:
    python scripts/policy/bootstrap_constraint_coverage.py
    python scripts/policy/bootstrap_constraint_coverage.py --as-of 2022-12-31
    python scripts/policy/bootstrap_constraint_coverage.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2.extras


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALIAS_DIRECTORY = PROJECT_ROOT / "data" / "reference" / "constraint_ledger"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_constraint_ledger import connect, ensure_schema  # noqa: E402
from src.makrograph.nlp.product_quality import is_product_label  # noqa: E402


def normalize_label(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").casefold())).strip()


def auto_constraint_key(product_label: str) -> str:
    """Stable but explicitly non-semantic key for a newly discovered label."""
    slug = "_".join(normalize_label(product_label).split())[:130].strip("_")
    return f"discovery_{slug or 'unlabelled_product'}"


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_reviewed_aliases(path: Path, country: str) -> list[dict]:
    payload = json.loads(path.read_text())
    rows = payload.get("aliases") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("alias packet must contain an 'aliases' list")
    output: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"alias {index} must be an object")
        product_label = (raw.get("product_label") or "").strip()
        constraint_key = (raw.get("constraint_key") or "").strip()
        scope = (raw.get("match_scope") or "").strip().upper()
        if not product_label or not constraint_key or scope not in {"EXACT", "FAMILY", "BROAD", "POLICY"}:
            raise ValueError(f"alias {index} needs product_label, constraint_key, and valid match_scope")
        normalized = normalize_label(product_label)
        if normalized in seen:
            raise ValueError(f"duplicate normalized product label in alias packet: {product_label!r}")
        seen.add(normalized)
        output.append({
            "country": country,
            "product_label": product_label,
            "normalized_label": normalized,
            "constraint_key": constraint_key,
            "match_scope": scope,
            "status": "REVIEWED",
            "source": raw.get("source") or "reviewed product-to-chain crosswalk",
            "review_note": raw.get("review_note"),
            "effective_from": (
                parse_date(raw["effective_from"]) if raw.get("effective_from") else None
            ),
            "effective_to": (
                parse_date(raw["effective_to"]) if raw.get("effective_to") else None
            ),
            "provenance_url": raw.get("provenance_url"),
            "reviewed_at": (
                parse_date(raw["reviewed_at"]) if raw.get("reviewed_at") else None
            ),
        })
    return output


def product_universe(cur, country: str, as_of: date) -> list[dict]:
    # A direct issuer product role expands *coverage*, not a constraint grade.
    # It supplies an auto-discovery alias/ledger row so the demand, import, and
    # capacity collectors know which new product chain needs independent
    # research.  It never supplies physical ledger evidence itself.
    has_roles = False
    cur.execute("SELECT to_regclass('public.mg_company_product_roles') AS name")
    has_roles = bool(cur.fetchone()["name"])
    role_select = """
        SELECT product_phrase AS product_label,
               MIN(first_evidence_date) AS first_seen_date,
               MAX(last_evidence_date) AS last_seen_date
        FROM mg_company_product_roles
        WHERE country = %s AND as_of_date <= %s AND role_state <> 'REJECTED'
          AND review_status <> 'REJECTED'
        GROUP BY product_phrase
    """ if has_roles else ""
    cur.execute("SELECT to_regclass('public.mg_company_capabilities') AS name")
    has_capabilities = country == "IN" and bool(cur.fetchone()["name"])
    capability_select = """
        SELECT product AS product_label,
               MIN(as_of_date) AS first_seen_date,
               MAX(as_of_date) AS last_seen_date
        FROM mg_company_capabilities
        WHERE as_of_date <= %s
        GROUP BY product
    """ if has_capabilities else ""
    mapper_select = """
        SELECT constrained_product AS product_label,
               MIN(as_of_date) AS first_seen_date,
               MAX(as_of_date) AS last_seen_date
        FROM mg_india_beneficiaries
        WHERE constrained_product IS NOT NULL AND BTRIM(constrained_product) <> ''
          AND as_of_date <= %s
        GROUP BY constrained_product
    """ if country == "IN" else ""
    selects = [part for part in (mapper_select, capability_select, role_select) if part]
    if not selects:
        return []
    params = []
    if mapper_select:
        params.append(as_of)
    if capability_select:
        params.append(as_of)
    if role_select:
        params.extend([country, as_of])
    rows = cur.execute(f"""
        WITH products AS (
            {" UNION ALL ".join(selects)}
        )
        SELECT product_label, MIN(first_seen_date) AS first_seen_date,
               MAX(last_seen_date) AS last_seen_date
        FROM products
        GROUP BY product_label
        ORDER BY product_label
    """, tuple(params))
    del rows
    return [
        dict(row) for row in cur.fetchall()
        if is_product_label(row.get("product_label"), min_words=1, max_words=8)
    ]


def reject_non_product_aliases(cur, country: str, as_of: date) -> int:
    """Effective-date rejection of legacy automatic labels that are not products."""
    cur.execute("""
        SELECT product_label, normalized_label, constraint_key, match_scope,
               source, provenance_url
        FROM mg_constraint_product_aliases
        WHERE country=%s AND status='AUTO_DISCOVERY'
    """, (country,))
    rejected = [
        dict(row) for row in cur.fetchall()
        if not is_product_label(row.get("product_label"), min_words=1, max_words=8)
    ]
    for row in rejected:
        cur.execute("""
            UPDATE mg_constraint_product_alias_versions
            SET effective_to=%s
            WHERE country=%s AND normalized_label=%s AND effective_from < %s
              AND status='AUTO_DISCOVERY'
              AND (effective_to IS NULL OR effective_to >= %s)
        """, (as_of - timedelta(days=1), country, row["normalized_label"], as_of, as_of))
        cur.execute("""
            INSERT INTO mg_constraint_product_alias_versions
              (country, product_label, normalized_label, constraint_key, match_scope,
               status, effective_from, source, review_note, provenance_url)
            VALUES (%s,%s,%s,%s,%s,'REJECTED',%s,%s,%s,%s)
            ON CONFLICT (country, normalized_label, effective_from) DO UPDATE SET
              status='REJECTED', review_note=EXCLUDED.review_note
        """, (
            country, row["product_label"], row["normalized_label"],
            row["constraint_key"], row["match_scope"], as_of, row["source"],
            "Automatic label rejected by generic physical-product phrase quality gate.",
            row.get("provenance_url"),
        ))
        cur.execute("""
            UPDATE mg_constraint_product_aliases
            SET status='REJECTED', effective_to=%s,
                review_note='Automatic label rejected by generic physical-product phrase quality gate.',
                updated_at=NOW()
            WHERE country=%s AND normalized_label=%s AND status='AUTO_DISCOVERY'
        """, (as_of, country, row["normalized_label"]))
    return len(rejected)


def upsert_alias(cur, alias: dict, first_seen: date | None, last_seen: date | None) -> None:
    effective_from = alias.get("effective_from") or first_seen or date.today()
    reviewed_at = alias.get("reviewed_at") or (
        effective_from if alias.get("status") == "REVIEWED" else None
    )
    cur.execute("""
        INSERT INTO mg_constraint_product_aliases
          (country, product_label, normalized_label, constraint_key, match_scope,
           status, first_seen_date, last_seen_date, source, review_note,
           effective_from, effective_to, provenance_url, reviewed_at)
        VALUES (%(country)s, %(product_label)s, %(normalized_label)s,
                %(constraint_key)s, %(match_scope)s, %(status)s,
                %(first_seen_date)s, %(last_seen_date)s, %(source)s, %(review_note)s,
                %(effective_from)s, %(effective_to)s, %(provenance_url)s, %(reviewed_at)s)
        ON CONFLICT (country, normalized_label) DO UPDATE SET
          product_label = EXCLUDED.product_label,
          constraint_key = CASE
              WHEN mg_constraint_product_aliases.status = 'REVIEWED'
                THEN mg_constraint_product_aliases.constraint_key
              WHEN EXCLUDED.status IN ('REVIEWED', 'AUTO_DISCOVERY')
                THEN EXCLUDED.constraint_key
              ELSE mg_constraint_product_aliases.constraint_key END,
          match_scope = CASE
              WHEN mg_constraint_product_aliases.status = 'REVIEWED'
                THEN mg_constraint_product_aliases.match_scope
              WHEN EXCLUDED.status IN ('REVIEWED', 'AUTO_DISCOVERY')
                THEN EXCLUDED.match_scope
              ELSE mg_constraint_product_aliases.match_scope END,
          status = CASE
              WHEN mg_constraint_product_aliases.status = 'REVIEWED' THEN 'REVIEWED'
              WHEN EXCLUDED.status IN ('REVIEWED', 'AUTO_DISCOVERY') THEN EXCLUDED.status
              ELSE mg_constraint_product_aliases.status END,
          first_seen_date = CASE
              WHEN mg_constraint_product_aliases.first_seen_date IS NULL THEN EXCLUDED.first_seen_date
              WHEN EXCLUDED.first_seen_date IS NULL THEN mg_constraint_product_aliases.first_seen_date
              ELSE LEAST(mg_constraint_product_aliases.first_seen_date, EXCLUDED.first_seen_date) END,
          last_seen_date = CASE
              WHEN mg_constraint_product_aliases.last_seen_date IS NULL THEN EXCLUDED.last_seen_date
              WHEN EXCLUDED.last_seen_date IS NULL THEN mg_constraint_product_aliases.last_seen_date
              ELSE GREATEST(mg_constraint_product_aliases.last_seen_date, EXCLUDED.last_seen_date) END,
          source = CASE
              WHEN mg_constraint_product_aliases.status = 'REVIEWED'
                THEN mg_constraint_product_aliases.source
              WHEN EXCLUDED.status IN ('REVIEWED', 'AUTO_DISCOVERY') THEN EXCLUDED.source
              ELSE mg_constraint_product_aliases.source END,
          review_note = CASE
              WHEN mg_constraint_product_aliases.status = 'REVIEWED'
                THEN mg_constraint_product_aliases.review_note
              WHEN EXCLUDED.status IN ('REVIEWED', 'AUTO_DISCOVERY') THEN EXCLUDED.review_note
              ELSE mg_constraint_product_aliases.review_note END,
          effective_from = LEAST(
              COALESCE(mg_constraint_product_aliases.effective_from, EXCLUDED.effective_from),
              EXCLUDED.effective_from),
          effective_to = EXCLUDED.effective_to,
          provenance_url = COALESCE(EXCLUDED.provenance_url,
                                    mg_constraint_product_aliases.provenance_url),
          reviewed_at = COALESCE(mg_constraint_product_aliases.reviewed_at,
                                 EXCLUDED.reviewed_at),
          updated_at = NOW()
    """, {
        **alias, "first_seen_date": first_seen, "last_seen_date": last_seen,
        "effective_from": effective_from, "effective_to": alias.get("effective_to"),
        "provenance_url": alias.get("provenance_url"), "reviewed_at": reviewed_at,
    })
    cur.execute("""
        INSERT INTO mg_constraint_product_alias_versions
          (country, product_label, normalized_label, constraint_key, match_scope,
           status, effective_from, effective_to, provenance_url, source,
           review_note, reviewed_at)
        VALUES (%(country)s, %(product_label)s, %(normalized_label)s,
                %(constraint_key)s, %(match_scope)s, %(status)s,
                %(effective_from)s, %(effective_to)s, %(provenance_url)s,
                %(source)s, %(review_note)s, %(reviewed_at)s)
        ON CONFLICT (country, normalized_label, effective_from) DO UPDATE SET
          product_label=EXCLUDED.product_label,
          constraint_key=EXCLUDED.constraint_key,
          match_scope=EXCLUDED.match_scope,
          status=EXCLUDED.status,
          effective_to=EXCLUDED.effective_to,
          provenance_url=EXCLUDED.provenance_url,
          source=EXCLUDED.source,
          review_note=EXCLUDED.review_note,
          reviewed_at=EXCLUDED.reviewed_at
    """, {
        **alias, "effective_from": effective_from,
        "effective_to": alias.get("effective_to"),
        "provenance_url": alias.get("provenance_url"), "reviewed_at": reviewed_at,
    })


def create_discovery_ledger(cur, country: str, alias: dict, first_seen: date | None) -> bool:
    """Create an initial coverage record if the chain was not known at first sight."""
    observed = first_seen or date.today()
    cur.execute("""
        SELECT 1 FROM mg_constraint_ledgers
        WHERE country=%s AND constraint_key=%s AND as_of_date <= %s
        LIMIT 1
    """, (country, alias["constraint_key"], observed))
    if cur.fetchone():
        return False
    scope = "EXACT_CHAIN" if alias["match_scope"] == "EXACT" else "BROAD"
    cur.execute("""
        INSERT INTO mg_constraint_ledgers
          (country, constraint_key, constraint_name, as_of_date, state, classification,
           product_scope, investment_eligibility, binding_demand_status,
           resupply_barrier_status, resolution_status, next_validation_date,
           summary, review_note, physical_state, trajectory,
           evidence_completeness, mechanism, derivation_method)
        VALUES (%s, %s, %s, %s, 'DISCOVERY', 'WATCH', %s, 'RESEARCH_ONLY',
                'UNPROVED', 'UNPROVED', 'UNKNOWN', %s, %s, %s,
                'DISCOVERY', 'STABLE_OR_UNKNOWN', 'UNMEASURED',
                'UNCLASSIFIED_RESEARCH', 'COVERAGE_PLACEHOLDER')
        ON CONFLICT (country, constraint_key, as_of_date) DO NOTHING
    """, (
        country, alias["constraint_key"], alias["product_label"], observed, scope,
        observed + timedelta(days=90),
        "Systematic product-universe coverage record. No dated physical evidence has been reviewed for this chain.",
        "Automated coverage bootstrap created this discovery row. Add dated primary evidence before changing classification, state, or investment eligibility.",
    ))
    return cur.rowcount == 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="IN")
    parser.add_argument("--as-of", help="maximum mapper date to cover; defaults to its latest date")
    parser.add_argument("--aliases", default=None,
                        help="reviewed alias JSON packet; defaults to <country>_reviewed_product_aliases.json when present")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    country = args.country.upper()
    alias_path = (Path(args.aliases) if args.aliases else
                  ALIAS_DIRECTORY / f"{country}_reviewed_product_aliases.json")
    alias_packet = load_reviewed_aliases(alias_path, country) if alias_path.exists() else []

    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if args.as_of:
                as_of = parse_date(args.as_of)
            else:
                if country == "IN":
                    cur.execute("SELECT MAX(as_of_date) AS as_of FROM mg_india_beneficiaries")
                else:
                    cur.execute("SELECT MAX(filed_at)::date AS as_of FROM mg_documents WHERE country=%s", (country,))
                as_of = cur.fetchone()["as_of"]
                if not as_of:
                    raise RuntimeError("mg_india_beneficiaries has no dated product rows")
            universe = product_universe(cur, country, as_of)
            packet_by_label = {row["normalized_label"]: row for row in alias_packet}
            all_aliases: list[tuple[dict, date | None, date | None]] = []
            for item in universe:
                normalized = normalize_label(item["product_label"])
                alias = packet_by_label.get(normalized)
                if alias is None:
                    alias = {
                        "country": country,
                        "product_label": item["product_label"],
                        "normalized_label": normalized,
                        "constraint_key": auto_constraint_key(item["product_label"]),
                        # This generated chain has the exact same normalized
                        # product name. Literal product-to-self identity is not
                        # a semantic adjacency guess, so it can be automatic.
                        # The ledger remains WATCH/UNMEASURED until independent
                        # constraint evidence arrives.
                        "match_scope": "EXACT",
                        "status": "AUTO_DISCOVERY",
                        "source": "automatic literal product-to-self constraint identity",
                        "review_note": (
                            "Exact canonical identity only; no scarcity, physical grade, "
                            "company promotion or investment authority is implied."
                        ),
                    }
                all_aliases.append((alias, item.get("first_seen_date"), item.get("last_seen_date")))

            if args.dry_run:
                reviewed = sum(alias["status"] == "REVIEWED" for alias, _, _ in all_aliases)
                print(f"Would cover {len(all_aliases)} mapper products as of {as_of} ({reviewed} reviewed, {len(all_aliases) - reviewed} auto-discovered).")
                for alias, first_seen, last_seen in all_aliases:
                    print(f"  {alias['status']:<14} {alias['product_label']} -> {alias['constraint_key']} [{alias['match_scope']}] {first_seen}..{last_seen}")
                return 0

            ensure_schema(cur)
            rejected_noise = reject_non_product_aliases(cur, country, as_of)
            for alias in alias_packet:
                # Persist the reviewed packet even if a label is not in the
                # current mapper universe, so historical aliases are explicit.
                upsert_alias(cur, alias, None, None)
            created = 0
            for alias, first_seen, last_seen in all_aliases:
                upsert_alias(cur, alias, first_seen, last_seen)
                created += int(create_discovery_ledger(cur, country, alias, first_seen))
        conn.commit()

    print(
        f"Covered {len(all_aliases)} mapper products as of {as_of}: "
        f"{len(alias_packet)} reviewed aliases, {created} new DISCOVERY ledger records; "
        f"effective-dated rejection applied to {rejected_noise} legacy non-product labels."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
