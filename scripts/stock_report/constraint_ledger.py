"""Point-in-time constraint ledger helpers for the stock selector.

The beneficiary mapper answers *where to look*.  This ledger records what was
actually known about a supply constraint at a dated point in time, including
the scope of the evidence and its next required review.  It deliberately does
not turn policy, demand, or broad supply-chain evidence into an investible
physical constraint.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from extract_report_data import q
from src.makrograph.constraint_contract import effective_alias_scope, observation_ttl_days


LEDGER_STALE_DAYS = 365

def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def normalize_product_label(value: str | None) -> str:
    """Stable product-label key shared by the coverage bootstrap and selector.

    Semantic aliases deliberately live in ``mg_constraint_product_aliases``.
    This only normalizes punctuation and whitespace; it never guesses that two
    different products belong to the same economic chain.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").casefold())).strip()


def fetch_constraint_aliases(cur, as_of: date, country: str = "IN") -> dict[str, dict]:
    """Return reviewed or discovery product aliases known at ``as_of``.

    A missing alias table is handled as empty coverage for backwards
    compatibility.  In particular, this function has no code fallback map:
    that would make an unreviewed code change silently alter historical
    research results.
    """
    cur.execute("SELECT to_regclass('public.mg_constraint_product_aliases') AS name")
    if not cur.fetchone()["name"]:
        return {}
    cur.execute("SELECT to_regclass('public.mg_constraint_product_alias_versions') AS name")
    has_versions = bool(cur.fetchone()["name"])
    alias_source = """
        SELECT v.product_label, v.normalized_label, v.constraint_key, v.match_scope,
               v.status, v.effective_from AS first_seen_date,
               v.effective_to AS last_seen_date, v.source, v.review_note,
               v.effective_from, v.effective_to, v.provenance_url, v.reviewed_at,
               v.country, v.id
        FROM mg_constraint_product_alias_versions v
        WHERE v.country=%s AND v.effective_from <= %s
          AND (v.effective_to IS NULL OR v.effective_to >= %s)
        UNION ALL
        SELECT a.product_label, a.normalized_label, a.constraint_key, a.match_scope,
               a.status, a.first_seen_date, a.last_seen_date, a.source, a.review_note,
               a.effective_from, a.effective_to, a.provenance_url, a.reviewed_at,
               a.country, a.id
        FROM mg_constraint_product_aliases a
        WHERE a.country=%s
          AND NOT EXISTS (
              SELECT 1 FROM mg_constraint_product_alias_versions v
              WHERE v.country=a.country AND v.normalized_label=a.normalized_label
          )
    """ if has_versions else """
        SELECT a.product_label, a.normalized_label, a.constraint_key, a.match_scope,
               a.status, a.first_seen_date, a.last_seen_date, a.source, a.review_note,
               a.effective_from, a.effective_to, a.provenance_url, a.reviewed_at,
               a.country, a.id
        FROM mg_constraint_product_aliases a WHERE a.country=%s
    """
    source_params = ((country, as_of, as_of, country) if has_versions else (country,))
    rows = q(cur, f"""
        WITH alias_at_date AS ({alias_source})
        SELECT a.product_label, a.normalized_label, a.constraint_key, a.match_scope,
               status, first_seen_date,
               LEAST(last_seen_date, %s::date) AS last_seen_date,
               source, review_note, effective_from, effective_to,
               provenance_url, reviewed_at,
               l.constraint_name AS latest_constraint_name,
               l.as_of_date AS latest_constraint_date
        FROM alias_at_date a
        LEFT JOIN LATERAL (
            SELECT constraint_name, as_of_date
            FROM mg_constraint_ledgers l
            WHERE l.country=a.country AND l.constraint_key=a.constraint_key
              AND l.as_of_date <= %s
            ORDER BY l.as_of_date DESC, l.id DESC
            LIMIT 1
        ) l ON TRUE
        WHERE a.status <> 'REJECTED'
          AND (a.first_seen_date IS NULL OR a.first_seen_date <= %s)
          AND (a.effective_from IS NULL OR a.effective_from <= %s)
          AND (a.effective_to IS NULL OR a.effective_to >= %s)
        ORDER BY a.normalized_label,
                 CASE a.status WHEN 'REVIEWED' THEN 0 ELSE 1 END, a.id DESC
    """, source_params + (as_of, as_of, as_of, as_of, as_of))
    aliases: dict[str, dict] = {}
    for row in rows:
        # The database uniqueness rule normally makes this first-write-win.
        # Keep reviewed data preferred if a legacy database has duplicates.
        normalized = row.get("normalized_label") or normalize_product_label(row.get("product_label"))
        effective_scope, basis = effective_alias_scope(
            row.get("product_label"), row.get("match_scope"),
            row.get("latest_constraint_name"),
        )
        row["stored_match_scope"] = row.get("match_scope")
        row["match_scope"] = effective_scope
        row["match_scope_basis"] = basis
        aliases.setdefault(normalized, row)
    return aliases


def fetch_observation_summary(cur, as_of: date, country: str = "IN") -> dict[str, dict]:
    """Summarize unreviewed automated observations without treating them as evidence."""
    cur.execute("SELECT to_regclass('public.mg_constraint_observation_queue') AS name")
    if not cur.fetchone()["name"]:
        return {}
    rows = q(cur, """
        SELECT constraint_key, COUNT(*)::int AS pending_count,
               ARRAY_AGG(DISTINCT observation_type ORDER BY observation_type) AS observation_types,
               MAX(observed_at) AS latest_observed_at
        FROM mg_constraint_observation_queue
        WHERE country = %s AND observed_at <= %s AND review_status = 'PENDING'
        GROUP BY constraint_key
    """, (country, as_of))
    return {row["constraint_key"]: row for row in rows}


def fetch_constraint_ledger(cur, as_of: date, country: str = "IN") -> dict[str, dict]:
    """Return the latest ledger snapshot per chain that was known by ``as_of``.

    The selector must still work against an older database, so a missing ledger
    is represented as an empty mapping rather than failing a historical replay.
    """
    cur.execute("SELECT to_regclass('public.mg_constraint_ledgers') AS name")
    if not cur.fetchone()["name"]:
        return {}

    rows = q(cur, """
        SELECT DISTINCT ON (l.constraint_key)
               l.id, l.constraint_key, l.constraint_name, l.as_of_date, l.state,
               l.classification, l.product_scope, l.investment_eligibility,
               l.import_dependency_ratio, l.measurement_date,
               l.import_value, l.import_value_unit,
               l.primary_origin, l.primary_origin_ratio, l.capacity_gap_ratio,
               l.domestic_capacity, l.capacity_unit, l.demand_volume, l.demand_unit,
               l.binding_demand_status,
               l.resupply_barrier_status, l.resolution_status,
               l.next_validation_date, l.summary, l.review_note,
               l.physical_state, l.trajectory, l.evidence_completeness,
               l.mechanism, l.derivation_method
        FROM mg_constraint_ledgers l
        WHERE l.country = %s AND l.as_of_date <= %s
        ORDER BY l.constraint_key, l.as_of_date DESC, l.id DESC
    """, (country, as_of))
    if not rows:
        return {}

    evidence = q(cur, """
        SELECT e.ledger_id, e.observation_id, e.evidence_type, e.source_date,
               e.published_at, e.available_at, e.source_hash, e.source_url,
               e.source_title, e.source_publisher, e.claim, e.value_numeric,
               e.value_unit, e.is_primary, e.independence_key,
               e.admissibility_status, e.quarantine_reason
        FROM mg_constraint_ledger_evidence e
        JOIN mg_constraint_ledgers l ON l.id = e.ledger_id
        JOIN (
            SELECT DISTINCT ON (constraint_key) id, constraint_key
            FROM mg_constraint_ledgers
            WHERE country = %s AND as_of_date <= %s
            ORDER BY constraint_key, as_of_date DESC, id DESC
        ) latest ON latest.id = l.id
        ORDER BY e.source_date DESC, e.id DESC
    """, (country, as_of))
    by_id: dict[int, list[dict]] = {}
    for item in evidence:
        by_id.setdefault(item["ledger_id"], []).append(item)

    output: dict[str, dict] = {}
    for item in rows:
        item["evidence"] = by_id.get(item.pop("id"), [])
        output[item["constraint_key"]] = item
    return output


def ledger_context_for_product(
    product: str,
    ledger_by_key: dict[str, dict] | None,
    as_of: date,
    aliases_by_label: dict[str, dict] | None = None,
    observation_summary: dict[str, dict] | None = None,
) -> dict:
    """Return a report-safe ledger view for one mapper product label."""
    alias = (aliases_by_label or {}).get(normalize_product_label(product))
    key = alias.get("constraint_key") if alias else None
    match_scope = alias.get("match_scope") if alias else "NONE"
    queued = (observation_summary or {}).get(key or "")
    ledger = (ledger_by_key or {}).get(key or "")
    if not ledger:
        return {
            "matched": False,
            "constraint_key": key,
            "match_scope": match_scope,
            "alias_status": alias.get("status") if alias else "UNCOVERED",
            "alias_source": alias.get("source") if alias else None,
            "queued_observations": queued,
            "status": (
                "covered product alias has no dated ledger record yet"
                if alias else "no product alias coverage record"
            ),
            "physical_measure_current": False,
            "can_support_buy_gate": False,
        }

    evidence_all = list(ledger.get("evidence") or [])
    evidence = [
        row for row in evidence_all
        if row.get("admissibility_status") == "ADMISSIBLE"
        and row.get("is_primary")
        and not (row.get("source_url") or "").casefold().startswith("scheme:")
        and (_as_date(row.get("available_at")) or _as_date(row.get("published_at"))
             or _as_date(row.get("source_date")) or date.max) <= as_of
    ]
    source_dates = [_as_date(row.get("source_date")) for row in evidence]
    source_dates = [item for item in source_dates if item]
    latest_source_date = max(source_dates) if source_dates else _as_date(ledger.get("as_of_date"))
    age_days = (as_of - latest_source_date).days if latest_source_date else None
    measurement_date = _as_date(ledger.get("measurement_date"))
    next_validation = _as_date(ledger.get("next_validation_date"))
    current = bool(latest_source_date and latest_source_date >= as_of - timedelta(days=LEDGER_STALE_DAYS))
    review_due = bool(next_validation and next_validation < as_of)
    classification = ledger.get("classification") or "WATCH"
    state = ledger.get("state") or "DISCOVERY"
    exact_physical = (match_scope == "EXACT" and classification == "PHYSICAL_CONSTRAINT" and
                      ledger.get("product_scope") == "EXACT_CHAIN")
    has_measure = (ledger.get("import_dependency_ratio") is not None or
                   ledger.get("capacity_gap_ratio") is not None)
    current_evidence = [
        row for row in evidence
        if (_as_date(row.get("source_date")) or date.min) >= as_of - timedelta(days=LEDGER_STALE_DAYS)
    ]
    # Several fields extracted from the same press release are corroboration
    # of one source, not three independent proofs.  The ledger requires an
    # analyst-provided independence key; source URL is a safe fallback for
    # legacy packets that predate that field.
    independent_keys = {
        row.get("independence_key") or row.get("source_url")
        for row in current_evidence
        if row.get("independence_key") or row.get("source_url")
    }
    current_types = sorted({row.get("evidence_type") for row in current_evidence
                            if row.get("evidence_type")})
    independent_count = len(independent_keys)
    physical_measure_evidence = [
        row for row in evidence
        if row.get("observation_id") is not None
        and row.get("evidence_type") in {"SUPPLY", "IMPORT"}
        and row.get("value_numeric") is not None
    ]
    physical_ttl = max(
        (observation_ttl_days("CAPACITY" if row.get("evidence_type") == "SUPPLY" else "IMPORT")
         for row in physical_measure_evidence),
        default=0,
    )
    physical_measure_current = bool(
        exact_physical and measurement_date and
        physical_ttl and measurement_date >= as_of - timedelta(days=physical_ttl) and
        not review_due and has_measure and
        physical_measure_evidence and
        state in {"MEASURED", "BINDING", "INVESTIBLE", "RESOLVING"}
    )
    can_support_buy_gate = bool(
        physical_measure_current and state in {"MEASURED", "BINDING", "INVESTIBLE"}
    )
    return {
        "matched": True,
        "constraint_key": key,
        "constraint_name": ledger.get("constraint_name"),
        "match_scope": match_scope,
        "alias_status": alias.get("status") if alias else "UNCOVERED",
        "alias_source": alias.get("source") if alias else None,
        "ledger_as_of_date": ledger.get("as_of_date"),
        "state": state,
        "classification": classification,
        "product_scope": ledger.get("product_scope"),
        "investment_eligibility": ledger.get("investment_eligibility"),
        "import_dependency_ratio": ledger.get("import_dependency_ratio"),
        "measurement_date": measurement_date,
        "import_value": ledger.get("import_value"),
        "import_value_unit": ledger.get("import_value_unit"),
        "primary_origin": ledger.get("primary_origin"),
        "primary_origin_ratio": ledger.get("primary_origin_ratio"),
        "capacity_gap_ratio": ledger.get("capacity_gap_ratio"),
        "domestic_capacity": ledger.get("domestic_capacity"),
        "capacity_unit": ledger.get("capacity_unit"),
        "demand_volume": ledger.get("demand_volume"),
        "demand_unit": ledger.get("demand_unit"),
        "binding_demand_status": ledger.get("binding_demand_status"),
        "resupply_barrier_status": ledger.get("resupply_barrier_status"),
        "resolution_status": ledger.get("resolution_status"),
        "physical_state": ledger.get("physical_state"),
        "trajectory": ledger.get("trajectory"),
        "evidence_completeness": ledger.get("evidence_completeness"),
        "mechanism": ledger.get("mechanism"),
        "derivation_method": ledger.get("derivation_method"),
        "next_validation_date": ledger.get("next_validation_date"),
        "review_due": review_due,
        "latest_source_date": latest_source_date,
        "evidence_age_days": age_days,
        "current": current,
        "physical_measure_current": physical_measure_current,
        "can_support_buy_gate": can_support_buy_gate,
        "summary": ledger.get("summary"),
        "review_note": ledger.get("review_note"),
        "evidence": evidence,
        "quarantined_evidence": [row for row in evidence_all if row not in evidence],
        "physical_measure_observation_ids": sorted({
            row["observation_id"] for row in physical_measure_evidence
        }),
        "current_evidence_types": current_types,
        "current_independent_source_count": independent_count,
        "evidence_diversity_status": (
            "two or more independent current sources" if independent_count >= 2 else
            "one current source family — corroboration incomplete" if independent_count == 1 else
            "no current independently identifiable source"
        ),
        "queued_observations": queued,
        "status": (
            "current exact physical ledger evidence" if can_support_buy_gate else
            "current exact physical record, but not a measured/binding Buy-gate leg" if exact_physical and current and not review_due else
            "family or policy context — cannot support the physical Buy gate" if match_scope != "EXACT" or classification != "PHYSICAL_CONSTRAINT" else
            "dated ledger evidence is stale or due for revalidation"
        ),
    }
