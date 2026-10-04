#!/usr/bin/env python3
"""Collect dated constraint observations into the review queue.

The queue joins the existing import, capacity-gap, trade, mapper-demand and
filing corpora to the database-managed product aliases.  Source-qualified,
locally bound issuer observations can be accepted automatically as company
binding/resolution evidence.  They never become a quantified industry
capacity/import measurement and therefore cannot manufacture an A/B physical
grade.  Ambiguous or reference rows without primary-source provenance remain
pending.

Usage:
    python scripts/policy/queue_constraint_observations.py --as-of 2026-07-31
    python scripts/policy/queue_constraint_observations.py --dry-run
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2.extras


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "stock_report"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT_ROOT))

from company_capabilities import match_component  # noqa: E402
from constraint_ledger import fetch_constraint_aliases, normalize_product_label  # noqa: E402
from seed_constraint_ledger import connect, ensure_schema  # noqa: E402
from src.makrograph.constraint_contract import (  # noqa: E402
    COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
)


COMMISSIONING_RE = (
    r"commercial\s+(?:production|operations)|commenc\w*\s+(?:commercial\s+)?production|"
    r"start(?:ed|ing)?\s+production|(?:plant|facilit\w*|production\s+line|capacity)"
    r".{0,80}?commission(?:ed|ing)|commission(?:ed|ing).{0,80}?"
    r"(?:plant|facilit\w*|production\s+line|capacity)"
)
LEAD_TIME_RE = r"lead\s*time|delivery\s+(?:period|schedule|timeline)"
LEAD_TIME_PRESSURE_RE = re.compile(
    r"(?:lead\s*time|delivery\s+(?:period|schedule|timeline)).{0,100}?"
    r"(?:extend\w*|increase\w*|longer|delay\w*|stretch\w*|"
    r"\d+(?:\.\d+)?\s*(?:days?|weeks?|months?|quarters?))|"
    r"(?:extend\w*|increase\w*|longer|delay\w*|stretch\w*).{0,100}?"
    r"(?:lead\s*time|delivery\s+(?:period|schedule|timeline))",
    re.I | re.S,
)
THIRD_PARTY_ASSERTION_RE = re.compile(
    r"\b(?:industry|market|competitors?|other\s+(?:companies|manufacturers|producers)|"
    r"successful\s+bidders?)\b",
    re.I,
)
LOCAL_DISCLOSURE_BREAK_RE = re.compile(r"(?:[\r\n\f]\s*){2,}|[•▪◦]")


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def phrase_terms(product_label: str) -> list[str]:
    """Generate source-search phrases mechanically from the reviewed label."""
    tokens = normalize_product_label(product_label).split()
    phrases = {" ".join(tokens)} if tokens else set()
    phrases.update(" ".join(tokens[index:index + 2])
                   for index in range(max(0, len(tokens) - 1)))
    # A one-token acronym (for example CRGO or PCB) is specific enough to
    # search.  Generic one-word product heads are intentionally not used.
    raw_tokens = re.findall(r"[A-Za-z0-9]+", product_label)
    phrases.update(token.casefold() for token in raw_tokens
                   if token.isupper() and 2 <= len(token) <= 5 and token.isalpha())
    return sorted(phrase for phrase in phrases if phrase)


def queue_row(cur, row: dict, dry_run: bool) -> bool:
    observed_at = row["observed_at"]
    published_at = row.get("published_at") or observed_at
    available_at = row.get("available_at") or published_at
    revision = int(row.get("revision") or 1)
    source_family = row.get("source_family") or row.get("source_url") or row["source_key"]
    # One publisher can issue many yearly/quarterly observations.  Source
    # family is an independence key, not event identity.  Date + revision make
    # the event append-only and allow a corrected release to coexist with the
    # original instead of rewriting history.
    source_key = (
        f"{row['source_key']}|observed={observed_at.isoformat()}|revision={revision}"
    )
    source_hash = row.get("source_hash") or hashlib.sha256(
        "|".join(str(value or "") for value in (
            row.get("source_url"), row.get("source_title"), source_key,
            observed_at, published_at, available_at,
            json.dumps(json_safe(row.get("metrics") or {}), sort_keys=True),
        )).encode("utf-8")
    ).hexdigest()
    if dry_run:
        return True
    cur.execute("""
        INSERT INTO mg_constraint_observation_queue
          (country, constraint_key, product_label, observation_type, observed_at,
           source_table, source_row_id, source_key, source_url, source_title,
           metrics, provenance_status, review_status, published_at, available_at,
           source_family, source_hash, product_scope, economic_period_start,
           economic_period_end, revision)
        VALUES (%(country)s, %(constraint_key)s, %(product_label)s,
                %(observation_type)s, %(observed_at)s, %(source_table)s,
                %(source_row_id)s, %(source_key)s, %(source_url)s, %(source_title)s,
                %(metrics)s, %(provenance_status)s, %(review_status)s,
                %(published_at)s, %(available_at)s, %(source_family)s,
                %(source_hash)s, %(product_scope)s, %(economic_period_start)s,
                %(economic_period_end)s, %(revision)s)
        ON CONFLICT (country, constraint_key, observation_type, source_key) DO NOTHING
    """, {
        **row,
        "source_key": source_key,
        "published_at": published_at,
        "available_at": available_at,
        "source_family": source_family,
        "source_hash": source_hash,
        "product_scope": row.get("product_scope") or "EXACT",
        "economic_period_start": row.get("economic_period_start"),
        "economic_period_end": row.get("economic_period_end"),
        "revision": revision,
        "review_status": row.get("review_status") or "PENDING",
        "metrics": psycopg2.extras.Json(json_safe(row["metrics"])),
    })
    return cur.rowcount == 1


def aliases(cur, country: str, as_of: date) -> list[dict]:
    return sorted(
        (dict(row) for row in fetch_constraint_aliases(cur, as_of, country).values()),
        key=lambda row: row.get("product_label") or "",
    )


def fetch_reference_rows(cur, table: str, fields: str, as_of: date) -> list[dict]:
    cur.execute(f"SELECT {fields} FROM {table} WHERE as_of_date <= %s ORDER BY as_of_date, id", (as_of,))
    return [dict(row) for row in cur.fetchall()]


def upsert_capacity_and_demand(cur, alias: dict, rows: list[dict], country: str, dry_run: bool) -> int:
    if alias.get("status") != "REVIEWED" or alias.get("match_scope") != "EXACT":
        return 0
    names = list({str(row.get("component") or "") for row in rows if row.get("component")})
    match = match_component(alias["product_label"], names)
    if not match:
        return 0
    count = 0
    for row in rows:
        if row.get("component") != match:
            continue
        source_qualified = bool(
            row.get("provenance_status") == "PRIMARY_SOURCE" and
            row.get("source_url") and row.get("source_published_at") and
            row.get("source_published_at") >= row.get("as_of_date")
        )
        base = {
            "country": country, "constraint_key": alias["constraint_key"],
            "product_label": alias["product_label"], "observed_at": row["as_of_date"],
            "source_table": "mg_capacity_gaps", "source_row_id": row["id"],
            "source_key": (
                row.get("source_family") or row.get("source_url") or
                f"mg_capacity_gaps:{row['id']}"
            ),
            "source_url": row.get("source_url"),
            "source_title": row.get("source_title") or f"Capacity-gap reference row: {row.get('component')}",
            "published_at": row.get("source_published_at"),
            "available_at": row.get("source_published_at"),
            "source_family": row.get("source_family"),
            "product_scope": "EXACT",
            "provenance_status": (
                "PRIMARY_SOURCE" if source_qualified else "PENDING_SOURCE"
            ),
            "review_status": "ACCEPTED" if source_qualified else "PENDING",
        }
        capacity_metrics = {key: row.get(key) for key in (
            "component", "domestic_capacity", "gap", "gap_pct", "unit",
            "supply_chain_stage", "severity", "target_year", "confidence",
            "measurement_basis",
        )}
        capacity_metrics.update({
            "numerator": row.get("domestic_capacity"),
            "denominator": row.get("required_quantity"),
            "numerator_semantics": "domestic operating capacity",
            "denominator_semantics": "current demand" if row.get("measurement_basis") in {
                "CURRENT_SUPPLY_DEMAND", "CURRENT_INSTALLED_VS_CURRENT_DEMAND",
            } else "future policy target requirement",
        })
        if queue_row(cur, {**base, "observation_type": "CAPACITY",
                           "metrics": capacity_metrics}, dry_run):
            count += 1
        if row.get("required_quantity") is not None:
            current_demand = row.get("measurement_basis") in {
                "CURRENT_SUPPLY_DEMAND", "CURRENT_INSTALLED_VS_CURRENT_DEMAND",
            }
            demand_metrics = {key: row.get(key) for key in (
                "component", "required_quantity", "unit", "target_year", "theme_name",
            )}
            demand_metrics["measurement_basis"] = (
                "CURRENT_DEMAND" if current_demand else "FUTURE_POLICY_TARGET"
            )
            if queue_row(cur, {**base, "observation_type": "DEMAND",
                               "revision": 2,
                               "review_status": (
                                   "ACCEPTED" if source_qualified and current_demand else "PENDING"
                               ),
                               "provenance_status": (
                                   "PRIMARY_SOURCE" if source_qualified else "PENDING_SOURCE"
                               ),
                               "metrics": demand_metrics}, dry_run):
                count += 1
    return count


def upsert_import_and_trade(cur, alias: dict, imports: list[dict], country: str,
                            as_of: date, dry_run: bool) -> int:
    if alias.get("status") != "REVIEWED" or alias.get("match_scope") != "EXACT":
        return 0
    names = list({str(row.get("component") or "") for row in imports if row.get("component")})
    match = match_component(alias["product_label"], names)
    count, hs_codes = 0, set()
    for row in imports:
        if row.get("component") != match:
            continue
        source_qualified = bool(
            row.get("provenance_status") == "PRIMARY_SOURCE" and
            row.get("source_url") and row.get("source_published_at") and
            row.get("source_published_at") >= row.get("as_of_date")
        )
        if row.get("hs_code"):
            hs_codes.add(str(row["hs_code"]))
        metrics = {key: row.get(key) for key in (
            "component", "import_share", "import_value_bn_usd", "primary_origin",
            "hs_code", "substitute_possible", "substitution_horizon_years", "risk_level",
            "measurement_basis",
        )}
        metrics["ratio_reported_by_source"] = bool(
            row.get("measurement_basis") == "SOURCE_REPORTED_IMPORT_SHARE"
        )
        if queue_row(cur, {
            "country": country, "constraint_key": alias["constraint_key"],
            "product_label": alias["product_label"], "observation_type": "IMPORT",
            "observed_at": row["as_of_date"], "source_table": "mg_import_dependencies",
            "source_row_id": row["id"],
            "source_key": (
                row.get("source_family") or row.get("source_url") or
                f"mg_import_dependencies:{row['id']}"
            ),
            "source_url": row.get("source_url"),
            "source_title": row.get("source_title") or f"Import-dependency reference row: {row.get('component')}",
            "published_at": row.get("source_published_at"),
            "available_at": row.get("source_published_at"),
            "source_family": row.get("source_family"),
            "product_scope": "EXACT",
            "metrics": metrics,
            "provenance_status": (
                "PRIMARY_SOURCE" if source_qualified else "PENDING_SOURCE"
            ),
            "review_status": "ACCEPTED" if source_qualified else "PENDING",
        }, dry_run):
            count += 1
    # Product/HS semantics belong in the reviewed, effective-dated crosswalk,
    # not in mutable code or a current import snapshot.  These codes can add
    # monthly momentum observations even when no legacy import row matches.
    cur.execute("""
        SELECT hs_code
        FROM mg_product_hs_crosswalks
        WHERE country=%s AND normalized_product=%s
          AND review_status='REVIEWED' AND relationship_scope='EXACT'
          AND effective_from <= %s
          AND (effective_to IS NULL OR effective_to >= %s)
    """, (country, normalize_product_label(alias["product_label"]), as_of, as_of))
    hs_codes.update(str(row["hs_code"]) for row in cur.fetchall())
    if not hs_codes:
        return count
    cur.execute("""
        SELECT id, hs_code, product_name, flow_direction, year, period, value_usd,
               quantity, quantity_unit, reporter_country, partner_country
        FROM mg_trade_flows
        WHERE CAST(hs_code AS TEXT) = ANY(%s) AND year <= %s
        ORDER BY year, id
    """, (sorted(hs_codes), as_of.year))
    for row in (dict(item) for item in cur.fetchall()):
        period = str(row.get("period") or "")
        if len(period) >= 6 and period[:6].isdigit():
            year, month = int(period[:4]), int(period[4:6])
            observed_at = date(year, month, calendar.monthrange(year, month)[1])
        else:
            observed_at = date(int(row["year"]), 12, 31)
        if observed_at > as_of:
            continue
        if queue_row(cur, {
            "country": country, "constraint_key": alias["constraint_key"],
            "product_label": alias["product_label"], "observation_type": "TRADE_FLOW",
            "observed_at": observed_at, "source_table": "mg_trade_flows",
            "source_row_id": row["id"], "source_key": f"mg_trade_flows:{row['id']}",
            "source_url": None, "source_title": f"Trade flow: {row.get('product_name') or row.get('hs_code')}",
            "metrics": {**row, "measurement_basis": "IMPORT_MOMENTUM_ONLY"},
            "provenance_status": "PENDING_SOURCE", "product_scope": "EXACT",
        }, dry_run):
            count += 1
    return count


def upsert_mapper_demand(cur, alias: dict, rows: list[dict], country: str, dry_run: bool) -> int:
    target = normalize_product_label(alias["product_label"])
    by_date: dict[date, list[dict]] = {}
    for row in rows:
        if normalize_product_label(row.get("constrained_product")) != target:
            continue
        by_date.setdefault(row["as_of_date"], []).append(row)
    count = 0
    for observed_at, cohort in by_date.items():
        # This is intentionally a dated *aggregate*, not one alert for every
        # mapper-company row.  The mapper is a discovery input, so hundreds of
        # co-occurrences would otherwise bury the import/capacity/filing rows
        # that an analyst actually needs to review.
        metrics = {
            "constrained_product": alias["product_label"],
            "mapped_company_rows": len(cohort),
            "mapped_company_count": len({row.get("ticker") for row in cohort if row.get("ticker")}),
            "themes": sorted({row.get("theme_name") for row in cohort if row.get("theme_name")}),
            "order_book_signal_rows": sum(bool(row.get("has_order_book_signals")) for row in cohort),
            "import_substitution_rows": sum(bool(row.get("import_substitution_play")) for row in cohort),
            "corroborated_rows": sum(bool(row.get("corroborated")) for row in cohort),
        }
        if queue_row(cur, {
            "country": country, "constraint_key": alias["constraint_key"],
            "product_label": alias["product_label"], "observation_type": "DEMAND",
            "observed_at": observed_at, "source_table": "mg_india_beneficiaries",
            "source_row_id": None,
            "source_key": f"mg_india_beneficiaries:{target}:{observed_at.isoformat()}",
            "source_url": None, "source_title": f"Mapper demand-signal cohort: {alias['product_label']}",
            "metrics": metrics, "provenance_status": "PENDING_SOURCE",
        }, dry_run):
            count += 1
    return count


def _exact_role_tickers(cur, country: str, as_of: date) -> dict[str, set[str]]:
    """Current automatically promoted producer tickers by exact product."""
    cur.execute("""
        SELECT DISTINCT normalized_product, UPPER(TRIM(ticker)) AS ticker
        FROM mg_company_product_roles
        WHERE country=%s AND as_of_date <= %s
          AND extraction_method=%s
          AND constraint_link_type IN ('EXACT', 'EXACT_PRODUCT')
          AND role_state='EVIDENCED'
          AND adjudication_state IN (
            'OPERATING_PRODUCER_EVIDENCED', 'PIPELINE_EVIDENCED',
            'EARNINGS_CAPTURE_EVIDENCED'
          )
    """, (country, as_of, COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION))
    output: dict[str, set[str]] = {}
    for row in cur.fetchall():
        output.setdefault(normalize_product_label(row["normalized_product"]), set()).add(
            (row["ticker"] or "").upper()
        )
    return output


def _local_product_signal(text: str, product_label: str, signal_re: str) -> dict | None:
    """Return exact-product local signal evidence, never a document-wide join."""
    normalized = normalize_product_label(product_label)
    if not normalized:
        return None
    product_re = re.compile(
        r"\b" + r"\s+".join(re.escape(token) for token in normalized.split()) + r"s?\b",
        re.I,
    )
    compiled_signal = re.compile(signal_re, re.I | re.S)
    boundaries = list(LOCAL_DISCLOSURE_BREAK_RE.finditer(text or ""))
    for hit in product_re.finditer(text or ""):
        left, right = 0, len(text)
        for boundary in boundaries:
            if boundary.end() <= hit.start():
                left = boundary.end()
            elif boundary.start() >= hit.end():
                right = boundary.start()
                break
        if right - left > 760:
            left = max(left, hit.start() - 320)
            right = min(right, hit.end() + 360)
        context = text[left:right]
        signal = compiled_signal.search(context)
        if not signal or THIRD_PARTY_ASSERTION_RE.search(context):
            continue
        excerpt = re.sub(r"\s+", " ", context).strip()[:600]
        return {"excerpt": excerpt, "exact_product": hit.group(0), "signal": signal.group(0)}
    return None


def filing_timing_documents(cur, aliases_to_search: list[dict], country: str, as_of: date) -> list[dict]:
    """Scan the filing corpus once, then map hits to aliases in Python.

    A separate ``ILIKE`` corpus scan for every product label made the collector
    scale linearly with coverage.  One union scan has the same point-in-time
    semantics and keeps a monthly coverage refresh practical.
    """
    terms = sorted({term for alias in aliases_to_search for term in phrase_terms(alias["product_label"])})
    if not terms:
        return []
    company_scope = """
          AND ticker IN (
              SELECT DISTINCT ticker
              FROM mg_company_product_roles
              WHERE country=%s AND as_of_date <= %s
                AND role_state <> 'REJECTED' AND extraction_method=%s
          )
        """
    company_params: tuple[Any, ...] = (
        country, as_of, COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
    )
    cur.execute(f"""
        SELECT id, url, title, doc_type, ticker, company, filed_at, raw_text,
               raw_text ~* %s AS has_commissioning,
               raw_text ~* %s AS has_lead_time
        FROM mg_documents
        WHERE country=%s AND filed_at <= %s
          {company_scope}
          AND raw_text ILIKE ANY(%s)
          AND (raw_text ~* %s OR raw_text ~* %s)
        ORDER BY filed_at DESC, id DESC
        LIMIT 1000
    """, (
        COMMISSIONING_RE, LEAD_TIME_RE, country, as_of, *company_params,
        [f"%{term}%" for term in terms], COMMISSIONING_RE, LEAD_TIME_RE,
    ))
    return [dict(row) for row in cur.fetchall()]


def upsert_filing_timing(cur, alias: dict, documents: list[dict], country: str,
                         exact_role_tickers: dict[str, set[str]], dry_run: bool) -> int:
    product_key = normalize_product_label(alias["product_label"])
    eligible_tickers = exact_role_tickers.get(product_key) or set()
    if not eligible_tickers:
        return 0
    count = 0
    for row in documents:
        ticker = (row.get("ticker") or "").strip().upper()
        if ticker not in eligible_tickers:
            continue
        for observation_type, matched in (
            ("COMMISSIONING", row.get("has_commissioning")),
            ("LEAD_TIME", row.get("has_lead_time")),
        ):
            if not matched:
                continue
            detail = _local_product_signal(
                row.get("raw_text") or "", alias["product_label"],
                COMMISSIONING_RE if observation_type == "COMMISSIONING" else LEAD_TIME_RE,
            )
            if not detail:
                continue
            if (observation_type == "LEAD_TIME" and
                    not LEAD_TIME_PRESSURE_RE.search(detail["excerpt"])):
                continue
            metrics = {
                key: row.get(key) for key in ("doc_type", "ticker", "company", "filed_at")
            }
            metrics.update({
                **detail,
                "evidence_scope": "exact product and issuer pressure/resolution signal in one local disclosure",
                "automatic_acceptance": True,
            })
            if observation_type == "LEAD_TIME":
                metrics["source_reported_pressure"] = True
            else:
                metrics["commissioning_status"] = "PLANNED_OR_IN_PROGRESS"
            if queue_row(cur, {
                "country": country, "constraint_key": alias["constraint_key"],
                "product_label": alias["product_label"], "observation_type": observation_type,
                "observed_at": row["filed_at"], "source_table": "mg_documents",
                "source_row_id": row["id"],
                "source_key": (
                    f"issuer:{ticker}:{row['filed_at'].isoformat()}:"
                    f"{alias['constraint_key']}:{observation_type}"
                ),
                "source_url": row.get("url"), "source_title": row.get("title") or f"{row.get('doc_type')} filing",
                "metrics": metrics, "provenance_status": "PRIMARY_SOURCE",
                "review_status": "ACCEPTED", "published_at": row["filed_at"],
                "available_at": row["filed_at"], "product_scope": "EXACT",
                "revision": 2,
            }, dry_run):
                count += 1
    return count


def collect_observations(
    cur,
    country: str,
    as_of: date,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Collect every automated evidence lead for one dated snapshot.

    This callable form lets the ingestion/NLP pipeline populate the evidence
    queue on every run. Exact local issuer observations and source-qualified
    physical rows are machine accepted; ambiguous rows remain pending.
    """
    alias_rows = aliases(cur, country, as_of)
    capacity_rows = fetch_reference_rows(
        cur, "mg_capacity_gaps",
        "id, component, required_quantity, domestic_capacity, gap, gap_pct, unit, supply_chain_stage, theme_name, severity, target_year, confidence, as_of_date, source_url, source_title, source_published_at, source_family, provenance_status, ingestion_method, measurement_basis",
        as_of,
    ) if country == "IN" else []
    import_rows = fetch_reference_rows(
        cur, "mg_import_dependencies",
        "id, component, import_share, import_value_bn_usd, primary_origin, hs_code, substitute_possible, substitution_horizon_years, risk_level, as_of_date, source_url, source_title, source_published_at, source_family, provenance_status, ingestion_method, measurement_basis",
        as_of,
    ) if country == "IN" else []
    mapper_rows = fetch_reference_rows(
        cur, "mg_india_beneficiaries",
        "id, ticker, theme_name, constrained_product, supply_chain_node, supply_chain_stage, beneficiary_type, conviction_score, signal_count, has_order_book_signals, import_substitution_play, corroborated, as_of_date",
        as_of,
    ) if country == "IN" else []
    filing_rows = filing_timing_documents(cur, alias_rows, country, as_of)
    role_tickers = _exact_role_tickers(cur, country, as_of)
    total = 0
    for alias in alias_rows:
        total += upsert_capacity_and_demand(cur, alias, capacity_rows, country, dry_run)
        total += upsert_import_and_trade(cur, alias, import_rows, country, as_of, dry_run)
        total += upsert_mapper_demand(cur, alias, mapper_rows, country, dry_run)
        total += upsert_filing_timing(
            cur, alias, filing_rows, country, role_tickers, dry_run
        )
    return total, len(alias_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="IN")
    parser.add_argument("--as-of", help="queue only source rows available by this date")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    country = args.country.upper()

    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if args.as_of:
                as_of = parse_date(args.as_of)
            else:
                if country == "IN":
                    cur.execute("SELECT MAX(as_of_date) AS as_of FROM mg_india_beneficiaries")
                else:
                    cur.execute(
                        "SELECT MAX(filed_at)::date AS as_of FROM mg_documents WHERE country=%s",
                        (country,),
                    )
                as_of = cur.fetchone()["as_of"]
                if not as_of:
                    raise RuntimeError(f"no dated source rows found for country {country}")
            if not args.dry_run:
                ensure_schema(cur)
            total, alias_count = collect_observations(
                cur, country, as_of, dry_run=args.dry_run,
            )
        if not args.dry_run:
            conn.commit()

    verb = "Would queue" if args.dry_run else "Queued/updated"
    print(
        f"{verb} {total} observations across {alias_count} covered products as of {as_of}. "
        "Only exact-product local issuer observations were auto-accepted; industry physical measurements still require source-qualified capacity/import data."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
