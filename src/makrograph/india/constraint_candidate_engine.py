"""Point-in-time constraint discovery between NLP and stock selection.

This stage is the durable bridge the original pipeline lacked.  It unions
product discoveries from issuer NLP, reviewed aliases, physical reference
tables, policy vocabulary, accepted observation packets and the dated ledger;
then records which evidence legs exist and which companies have a literal
same-product role.

The output is a research-priority snapshot, never a Buy list.  A high score
means "research this chain now".  It cannot replace the selector's physical,
company-capture, risk, valuation or position gates.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import psycopg2.extras

from ..nlp.entity_extractor import EntityExtractor
from ..nlp.product_quality import is_product_label
from ..constraint_contract import (
    COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
    CONSTRAINT_CANDIDATE_EXTRACTOR_VERSION,
    effective_alias_scope,
    derive_constraint_state,
    latest_observation_revisions,
    nonphysical_observation_admissibility,
    physical_observation_admissibility,
    product_identity_constraint_key,
)

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = CONSTRAINT_CANDIDATE_EXTRACTOR_VERSION
ROLE_EXTRACTOR_VERSION = COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION
CURRENT_EVIDENCE_DAYS = 540

DEMAND_SIGNALS = frozenset({
    "demand_surge", "demand_pull", "tender_pipeline", "backlog_duration",
    "capacity_constraint_seller", "capacity_utilization_high",
    "demand_exceeds_supply",
})
BINDING_SIGNALS = frozenset({
    "capacity_constraint_seller", "capacity_utilization_high",
    "demand_exceeds_supply", "backlog_duration", "demand_pull",
})
BARRIER_SIGNALS = frozenset({
    "qualification_barrier", "lead_time_extension", "supply_concentration",
    "competitive_moat",
})
POLICY_SIGNALS = frozenset({
    "policy_support", "localization_opportunity", "regulatory_tailwind",
})
RESOLUTION_SIGNALS = frozenset({
    "supply_response_commissioning", "supply_easing",
})
RELEVANT_SIGNALS = sorted(
    DEMAND_SIGNALS | BINDING_SIGNALS | BARRIER_SIGNALS |
    POLICY_SIGNALS | RESOLUTION_SIGNALS | {"import_dependency_quantified"}
)

_UNIT_TOKENS = frozenset({
    "mt", "kt", "tonne", "tonnes", "ton", "kg", "gw", "gwh", "mw",
    "mwh", "kwh", "km", "mn", "bn", "cr", "usd", "inr", "sqm",
    "sqft", "unit", "units", "capacity", "pct", "percent", "value",
    "qty", "quantity", "year", "annual",
})


def normalize_product(value: str | None) -> str:
    base = re.sub(r"\(.*?\)", " ", (value or "").casefold())
    base = base.split("/")[-1]
    base = re.sub(r"[^a-z0-9]+", " ", base)
    words = []
    for word in base.split():
        if word in _UNIT_TOKENS:
            continue
        if len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        words.append(word)
    return " ".join(words).strip()


def product_tokens(value: str | None) -> set[str]:
    return {token for token in normalize_product(value).split() if len(token) > 1}


def display_product(value: str | None) -> str:
    text = re.sub(r"[_-]+", " ", value or "")
    words = [word for word in text.split() if word.casefold() not in _UNIT_TOKENS]
    return " ".join(words).strip().title()


def discovery_constraint_key(product_label: str) -> str:
    return product_identity_constraint_key(normalize_product(product_label))


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def score_constraint_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Score research urgency from independent evidence legs.

    This pure function intentionally does not accept price returns, company
    names, theme strength or desired pick counts.  It can therefore be tested
    without a database and cannot be tuned to make a known winner appear.
    """
    measured_gap = bool(profile.get("has_capacity_measure"))
    measured_import = bool(profile.get("has_import_measure"))
    measured = measured_gap or measured_import
    demand = bool(profile.get("demand_source_count"))
    binding = bool(profile.get("binding_source_count"))
    barrier = bool(profile.get("barrier_source_count"))
    policy = bool(profile.get("policy_source_count"))
    resolving = bool(profile.get("resolution_source_count"))
    source_count = int(profile.get("independent_source_count") or 0)
    independent_date_count = int(profile.get("independent_date_count") or 0)
    company_count = int(profile.get("company_count") or 0)
    ledger_evidenced = bool(profile.get("ledger_evidenced"))
    physical_observation_count = int(profile.get("physical_observation_count") or 0)
    non_policy_source_count = int(profile.get("non_policy_source_count") or 0)
    physical_provenance = measured and physical_observation_count >= 1
    scarcity_candidate = bool(
        physical_provenance or (
            demand and binding and barrier
            and non_policy_source_count >= 2
            and independent_date_count >= 2
        )
    )

    if (physical_provenance and binding and barrier and demand
            and non_policy_source_count >= 2
            and independent_date_count >= 2):
        physical_quality = "A"
    elif physical_provenance and demand and (binding or barrier):
        physical_quality = "B"
    elif physical_provenance:
        physical_quality = "WEAK"
    else:
        physical_quality = "UNMEASURED"

    if measured and binding:
        mechanism = "PHYSICAL_CONSTRAINT"
    elif measured_import and policy:
        mechanism = "LOCALISATION_QUALIFICATION"
    elif demand and policy:
        mechanism = "DEPLOYMENT_DEMAND_PULL"
    else:
        mechanism = "UNCLASSIFIED_RESEARCH"
    candidate_class = (
        "SCARCITY_CANDIDATE" if scarcity_candidate else
        "DEPLOYMENT_THEME" if demand and policy else
        "PRODUCT_DISCOVERY"
    )

    score = 0
    score += 30 if physical_provenance else 0
    score += 16 if demand else 0
    score += 16 if binding else 0
    score += 14 if barrier else 0
    score += 8 if policy else 0
    score += min(10, source_count * 2)
    score += min(6, company_count * 2)
    score += 12 if ledger_evidenced else 0
    if resolving:
        score -= 10
    score = max(0, min(100, score))

    missing = []
    if not measured:
        missing.append("dated capacity gap or import measurement")
    elif not physical_provenance:
        missing.append("accepted exact-product primary observation behind the measurement")
    if not demand:
        missing.append("product-specific demand evidence")
    if not binding:
        missing.append("binding utilization, backlog, allocation, or lead-time evidence")
    if not barrier:
        missing.append("qualification, concentration, or resupply barrier")
    if not company_count:
        missing.append("literal listed-company product role")

    # An unmeasured chain may be urgent research, but it must never be labelled
    # with the same INVESTIGATE_NOW state as a measured physical constraint.
    if score >= 70 and physical_quality in {"A", "B"}:
        research_state = "INVESTIGATE_NOW"
    elif scarcity_candidate and (score >= 45 or ledger_evidenced):
        research_state = "MEASURE_NEXT"
    else:
        research_state = "DISCOVERY"

    state_axes = derive_constraint_state({
        "measured": physical_provenance,
        "demand": demand,
        "binding": binding,
        "barrier": barrier,
        "resolving": resolving,
    })
    return {
        "research_priority": score,
        "research_state": research_state,
        "mechanism": mechanism,
        "candidate_class": candidate_class,
        "physical_quality": physical_quality,
        **state_axes,
        "missing_legs": missing,
        "resolution_risk": "ACTIVE" if resolving else "UNMEASURED",
    }


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",))
    row = cur.fetchone()
    return bool(row and row.get("name"))


def _rows(cur, sql: str, params: tuple = ()) -> list[dict]:
    cur.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def _reference_alias(label: str, aliases: list[dict]) -> dict | None:
    """Use token matching only for a reviewed exact alias.

    Family/broad/policy aliases are never allowed to turn adjacent reference
    data into an item-level physical constraint.
    """
    tokens = product_tokens(label)
    choices = []
    for alias in aliases:
        if alias.get("status") != "REVIEWED" or alias.get("match_scope") != "EXACT":
            continue
        alias_tokens = product_tokens(alias.get("product_label"))
        if alias_tokens and (tokens <= alias_tokens or alias_tokens <= tokens):
            choices.append((len(tokens ^ alias_tokens), -len(alias_tokens), alias))
    return sorted(choices, key=lambda item: item[:2])[0][2] if choices else None


def _context_matches_product(context: str, product_label: str) -> bool:
    normalized_context = normalize_product(context)
    normalized_product = normalize_product(product_label)
    if not normalized_context or not normalized_product:
        return False
    phrase = r"\b" + r"\s+".join(re.escape(part) + "s?" for part in normalized_product.split()) + r"\b"
    return bool(re.search(phrase, normalized_context, re.I))


class ConstraintCandidateEngine:
    """Create a point-in-time research snapshot from all upstream evidence."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def run(
        self,
        pg_store,
        as_of_date: date | None = None,
        country: str = "IN",
        lookback_days: int = CURRENT_EVIDENCE_DAYS,
    ) -> dict[str, Any]:
        as_of = as_of_date or date.today()
        with pg_store._conn() as conn:
            # DDL from concurrent historical backfills otherwise remains
            # locked for the entire collect/persist transaction and can
            # deadlock even though each worker writes a different as-of date.
            # Serialize only schema setup, commit it, then let dated snapshots
            # proceed independently.
            with conn.cursor() as schema_cur:
                schema_cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("makrograph_constraint_candidate_schema",),
                )
            self.ensure_schema(conn)
            conn.commit()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                candidates, company_rows = self.collect(cur, as_of, country, lookback_days)
            self.persist(conn, candidates, company_rows, as_of, country)
        top = sorted(candidates, key=lambda row: (-row["research_priority"], row["product_label"]))[:20]
        return {
            "constraint_candidates": len(candidates),
            "constraint_company_candidates": len(company_rows),
            "investigate_now": sum(row["research_state"] == "INVESTIGATE_NOW" for row in candidates),
            "measure_next": sum(row["research_state"] == "MEASURE_NEXT" for row in candidates),
            "candidate_theme_names": [row["theme_name"] for row in top],
            "top_candidates": [{
                "product_label": row["product_label"],
                "mechanism": row["mechanism"],
                "physical_quality": row["physical_quality"],
                "research_priority": row["research_priority"],
                "research_state": row["research_state"],
            } for row in top[:10]],
        }

    def collect(
        self,
        cur,
        as_of: date,
        country: str,
        lookback_days: int,
    ) -> tuple[list[dict], list[dict]]:
        cutoff = as_of - timedelta(days=lookback_days)
        # A snapshot has one row per literal physical product. Theme and
        # ledger keys are related context, not alternative product identities.
        # Otherwise a family alias plus an exact issuer role creates duplicate
        # candidates and splits the producer evidence between them.
        profiles: dict[str, dict] = {}

        def register(
            label: str | None,
            origin: str,
            constraint_key: str | None = None,
            alias: dict | None = None,
        ) -> dict | None:
            product_label = (label or "").strip()
            if not is_product_label(product_label, min_words=1, max_words=8):
                return None
            normalized = normalize_product(product_label)
            if not normalized:
                return None
            exact_theme = bool(alias and alias.get("match_scope") == "EXACT")
            key = (constraint_key if exact_theme and constraint_key
                   else discovery_constraint_key(product_label))
            profile = profiles.setdefault(normalized, {
                "country": country,
                "constraint_key": key,
                "product_label": product_label,
                "normalized_product": normalized,
                "related_constraint_keys": set(),
                "alias_status": None,
                "match_scope": None,
                "origins": set(),
                "evidence_dates": [],
                "non_policy_evidence_dates": set(),
                "source_classes": set(),
                "source_families": set(),
                "non_policy_source_families": set(),
                "physical_observation_ids": set(),
                "demand_sources": set(),
                "binding_sources": set(),
                "barrier_sources": set(),
                "policy_sources": set(),
                "resolution_sources": set(),
                "capacity_gap_pct": None,
                "capacity_as_of": None,
                "import_share": None,
                "import_as_of": None,
                "primary_origin": None,
                "substitution_horizon_years": None,
                "ledger_state": None,
                "ledger_classification": None,
                "role_rows": [],
                "capability_rows": [],
            })
            profile["origins"].add(origin)
            if constraint_key:
                profile["related_constraint_keys"].add(constraint_key)
            profile["related_constraint_keys"].add(profile["constraint_key"])
            if alias:
                profile["alias_status"] = alias.get("status")
                profile["match_scope"] = alias.get("match_scope")
                profile["product_label"] = alias.get("product_label") or product_label
            return profile

        aliases: list[dict] = []
        if _table_exists(cur, "mg_constraint_product_aliases"):
            has_alias_versions = _table_exists(cur, "mg_constraint_product_alias_versions")
            alias_source = """
                SELECT v.product_label, v.normalized_label, v.constraint_key,
                       v.match_scope, v.status, v.effective_from AS first_seen_date,
                       v.effective_to AS last_seen_date, v.effective_from,
                       v.effective_to, v.country, v.id
                FROM mg_constraint_product_alias_versions v
                WHERE v.country=%s AND v.effective_from <= %s
                  AND (v.effective_to IS NULL OR v.effective_to >= %s)
                UNION ALL
                SELECT a.product_label, a.normalized_label, a.constraint_key,
                       a.match_scope, a.status, a.first_seen_date,
                       a.last_seen_date, a.effective_from, a.effective_to,
                       a.country, a.id
                FROM mg_constraint_product_aliases a
                WHERE a.country=%s AND NOT EXISTS (
                    SELECT 1 FROM mg_constraint_product_alias_versions v
                    WHERE v.country=a.country AND v.normalized_label=a.normalized_label
                )
            """ if has_alias_versions else """
                SELECT a.product_label, a.normalized_label, a.constraint_key,
                       a.match_scope, a.status, a.first_seen_date,
                       a.last_seen_date, a.effective_from, a.effective_to,
                       a.country, a.id
                FROM mg_constraint_product_aliases a WHERE a.country=%s
            """
            alias_params = ((country, as_of, as_of, country)
                            if has_alias_versions else (country,))
            aliases = _rows(cur, f"""
                WITH alias_at_date AS ({alias_source})
                SELECT a.product_label, a.normalized_label, a.constraint_key,
                       a.match_scope, a.status, a.first_seen_date, a.last_seen_date,
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
            """, alias_params + (as_of, as_of, as_of, as_of))
            for alias in aliases:
                scope, basis = effective_alias_scope(
                    alias.get("product_label"), alias.get("match_scope"),
                    alias.get("latest_constraint_name"),
                )
                alias["stored_match_scope"] = alias.get("match_scope")
                alias["match_scope"] = scope
                alias["match_scope_basis"] = basis
                register(alias["product_label"], "PRODUCT_ALIAS",
                         alias["constraint_key"], alias)

        # These two legacy reference tables contain India observations and do
        # not carry a country column.  Never leak them into a US snapshot.  US
        # physical measures must arrive through the country-scoped constraint
        # ledger/observation pipeline until its reference tables are migrated.
        if country == "IN" and _table_exists(cur, "mg_capacity_gaps"):
            for row in _rows(cur, """
                SELECT DISTINCT ON (lower(BTRIM(component))) id, component,
                       gap_pct, as_of_date, theme_name, severity, target_year,
                       source_url, source_title, source_published_at,
                       source_family, provenance_status, ingestion_method
                FROM mg_capacity_gaps
                WHERE as_of_date <= %s AND component IS NOT NULL
                ORDER BY lower(BTRIM(component)), as_of_date DESC, id DESC
            """, (as_of,)):
                alias = _reference_alias(row["component"], aliases)
                profile = register(
                    alias["product_label"] if alias else display_product(row["component"]),
                    "CAPACITY_REFERENCE",
                    alias.get("constraint_key") if alias else None,
                    alias,
                )
                if not profile:
                    continue
                observed = _as_date(row.get("as_of_date"))
                profile["capacity_gap_pct"] = row.get("gap_pct")
                profile["capacity_as_of"] = observed
                if observed:
                    profile["evidence_dates"].append(observed)
                source_ready = bool(
                    row.get("provenance_status") == "PRIMARY_SOURCE" and
                    row.get("source_url") and row.get("source_published_at") and
                    _as_date(row.get("source_published_at")) <= as_of
                )
                # Reference rows are discovery inputs only. Even a sourced row
                # must pass through the immutable accepted-observation contract
                # before it can create a measurement in this same run.
                if row.get("gap_pct") is not None:
                    profile["capacity_gap_pct"] = None
                    profile["capacity_as_of"] = None
                    profile["source_classes"].add(
                        "PRIMARY_CAPACITY_AWAITING_OBSERVATION" if source_ready
                        else "UNVERIFIED_CAPACITY_CONTEXT"
                    )

        if country == "IN" and _table_exists(cur, "mg_import_dependencies"):
            for row in _rows(cur, """
                SELECT DISTINCT ON (lower(BTRIM(component))) id, component,
                       import_share, primary_origin, substitution_horizon_years,
                       as_of_date, risk_level, source_url, source_title,
                       source_published_at, source_family, provenance_status,
                       ingestion_method
                FROM mg_import_dependencies
                WHERE as_of_date <= %s AND component IS NOT NULL
                ORDER BY lower(BTRIM(component)), as_of_date DESC, id DESC
            """, (as_of,)):
                alias = _reference_alias(row["component"], aliases)
                profile = register(
                    alias["product_label"] if alias else display_product(row["component"]),
                    "IMPORT_REFERENCE",
                    alias.get("constraint_key") if alias else None,
                    alias,
                )
                if not profile:
                    continue
                observed = _as_date(row.get("as_of_date"))
                profile["import_share"] = row.get("import_share")
                profile["import_as_of"] = observed
                profile["primary_origin"] = row.get("primary_origin")
                profile["substitution_horizon_years"] = row.get("substitution_horizon_years")
                if observed:
                    profile["evidence_dates"].append(observed)
                source_ready = bool(
                    row.get("provenance_status") == "PRIMARY_SOURCE" and
                    row.get("source_url") and row.get("source_published_at") and
                    _as_date(row.get("source_published_at")) <= as_of
                )
                if row.get("import_share") is not None:
                    profile["import_share"] = None
                    profile["import_as_of"] = None
                    profile["source_classes"].add(
                        "PRIMARY_IMPORT_AWAITING_OBSERVATION" if source_ready
                        else "UNVERIFIED_IMPORT_CONTEXT"
                    )

        latest_ledgers: dict[str, dict] = {}
        if _table_exists(cur, "mg_constraint_ledgers"):
            ledger_rows = _rows(cur, """
                SELECT DISTINCT ON (constraint_key) *
                FROM mg_constraint_ledgers
                WHERE country=%s AND as_of_date <= %s
                ORDER BY constraint_key, as_of_date DESC, id DESC
            """, (country, as_of))
            latest_ledgers = {row["constraint_key"]: row for row in ledger_rows}
            evidence_by_ledger: dict[int, list[dict]] = defaultdict(list)
            if ledger_rows and _table_exists(cur, "mg_constraint_ledger_evidence"):
                ledger_ids = [row["id"] for row in ledger_rows]
                for evidence in _rows(cur, """
                    SELECT ledger_id, observation_id, evidence_type, source_date,
                           published_at, available_at, source_url, value_numeric,
                           is_primary, independence_key, admissibility_status
                    FROM mg_constraint_ledger_evidence
                    WHERE ledger_id = ANY(%s) AND source_date <= %s
                      AND COALESCE(available_at, published_at, source_date) <= %s
                """, (ledger_ids, as_of, as_of)):
                    if (not evidence.get("is_primary") or
                            evidence.get("admissibility_status") != "ADMISSIBLE" or
                            (evidence.get("source_url") or "").casefold().startswith("scheme:")):
                        continue
                    evidence_by_ledger[evidence["ledger_id"]].append(evidence)
            alias_by_constraint = defaultdict(list)
            for alias in aliases:
                if alias.get("match_scope") == "EXACT":
                    alias_by_constraint[alias["constraint_key"]].append(alias)
            for row in ledger_rows:
                linked_aliases = alias_by_constraint.get(row["constraint_key"]) or [None]
                for alias in linked_aliases:
                    profile = register(
                        alias["product_label"] if alias else row["constraint_name"],
                        "CONSTRAINT_LEDGER", row["constraint_key"], alias,
                    )
                    if not profile:
                        continue
                    profile["ledger_state"] = row.get("state")
                    profile["ledger_classification"] = row.get("classification")
                    observed = _as_date(row.get("as_of_date"))
                    if observed:
                        profile["evidence_dates"].append(observed)
                    admissible = evidence_by_ledger.get(row["id"], [])
                    physical_types = {
                        item.get("evidence_type") for item in admissible
                        if item.get("observation_id") is not None
                        and item.get("value_numeric") is not None
                    }
                    measurement = _as_date(row.get("measurement_date"))
                    if measurement and measurement >= cutoff:
                        if (row.get("capacity_gap_ratio") is not None and
                                "SUPPLY" in physical_types):
                            profile["capacity_gap_pct"] = float(row["capacity_gap_ratio"]) * 100
                            profile["capacity_as_of"] = measurement
                        if (row.get("import_dependency_ratio") is not None and
                                "IMPORT" in physical_types):
                            profile["import_share"] = row["import_dependency_ratio"]
                            profile["import_as_of"] = measurement
                    for evidence in admissible:
                        source_date = _as_date(evidence.get("source_date"))
                        if not source_date or source_date < cutoff:
                            continue
                        source_key = (evidence.get("independence_key") or
                                      evidence.get("source_url") or
                                      f"ledger-evidence:{evidence['ledger_id']}")
                        evidence_type = evidence.get("evidence_type")
                        profile["source_families"].add(source_key)
                        profile["source_classes"].add("LEDGER_ADMISSIBLE_EVIDENCE")
                        profile["evidence_dates"].append(source_date)
                        if evidence_type != "POLICY":
                            profile["non_policy_source_families"].add(source_key)
                            profile["non_policy_evidence_dates"].add(source_date)
                        if evidence_type in {"SUPPLY", "IMPORT"} and evidence.get("observation_id"):
                            profile["physical_observation_ids"].add(evidence["observation_id"])
                        if evidence_type == "DEMAND":
                            profile["demand_sources"].add(source_key)
                        elif evidence_type == "BINDING":
                            profile["binding_sources"].add(source_key)
                        elif evidence_type == "BARRIER":
                            profile["barrier_sources"].add(source_key)
                        elif evidence_type == "POLICY":
                            profile["policy_sources"].add(source_key)
                        elif evidence_type == "RESOLUTION":
                            profile["resolution_sources"].add(source_key)

        if _table_exists(cur, "mg_policy_product_discoveries"):
            for row in _rows(cur, """
                SELECT product_label, normalized_product, scheme_name,
                       source_document_count, source_issuer_count,
                       first_source_date, last_source_date
                FROM mg_policy_product_discoveries
                WHERE country=%s AND as_of_date <= %s
            """, (country, as_of)):
                alias = _reference_alias(row["product_label"], aliases)
                profile = register(
                    alias["product_label"] if alias else row["product_label"],
                    "POLICY_PRODUCT_NLP",
                    alias.get("constraint_key") if alias else None,
                    alias,
                )
                if profile and _as_date(row.get("last_source_date")) and _as_date(row["last_source_date"]) >= cutoff:
                    source_key = f"policy-product:{row['scheme_name']}:{row['normalized_product']}"
                    profile["policy_sources"].add(source_key)
                    profile["source_families"].add(source_key)
                    profile["source_classes"].add("ISSUER_POLICY_DISCOVERY")
                    profile["evidence_dates"].append(_as_date(row["last_source_date"]))

        # Government-originated product discovery precedes issuer disclosure.
        # Products are extracted only from generic manufacturing grammar; a
        # policy keyword or a broad sector word cannot create a product chain.
        if _table_exists(cur, "mg_policy_announcements"):
            policy_extractor = EntityExtractor({"use_spacy": False, "use_finbert": False})
            for row in _rows(cur, """
                SELECT id, published_date, title, url, raw_text, scheme_score
                FROM mg_policy_announcements
                WHERE published_date BETWEEN %s AND %s
                  AND raw_text IS NOT NULL AND BTRIM(raw_text) <> ''
                  AND scheme_score >= 1
                ORDER BY published_date, id
            """, (cutoff, as_of)):
                observed = _as_date(row.get("published_date"))
                source_key = f"government-policy:{row['id']}"
                extracted = policy_extractor.extract(
                    f"{row.get('title') or ''}\n{row.get('raw_text') or ''}"
                )
                for entity in extracted.by_type("PRODUCT"):
                    if not is_product_label(entity.entity_text, min_words=2, max_words=5):
                        continue
                    alias = _reference_alias(entity.entity_text, aliases)
                    profile = register(
                        alias["product_label"] if alias else entity.entity_text,
                        "GOVERNMENT_POLICY_NLP",
                        alias.get("constraint_key") if alias else None,
                        alias,
                    )
                    if not profile:
                        continue
                    profile["policy_sources"].add(source_key)
                    profile["source_families"].add(source_key)
                    profile["source_classes"].add("GOVERNMENT_PRIMARY_SOURCE")
                    if observed:
                        profile["evidence_dates"].append(observed)

        role_rows: list[dict] = []
        if _table_exists(cur, "mg_company_product_roles"):
            review_expr = """
                       EXISTS (
                         SELECT 1 FROM mg_company_role_reviews review
                         WHERE review.country=r.country AND review.ticker=r.ticker
                           AND review.normalized_product=r.normalized_product
                           AND review.role_type=r.role_type
                           AND review.review_status='APPROVED'
                           AND review.decision_available_from <= %s
                       )
            """ if _table_exists(cur, "mg_company_role_reviews") else "FALSE"
            review_params = (as_of,) if "%s" in review_expr else ()
            role_rows = _rows(cur, f"""
                SELECT DISTINCT ON (ticker, normalized_product, role_type)
                       r.ticker, r.company, r.product_phrase, r.normalized_product,
                       r.role_type, r.role_state, r.constraint_key,
                       r.constraint_link_type, r.first_evidence_date,
                       r.last_evidence_date, r.independent_document_count,
                       r.physical_evidence_count, r.pipeline_evidence_count,
                       r.earnings_capture_count, r.demand_or_policy_count,
                       r.evidence, r.adjudication_state, r.adjudication_reason,
                       {review_expr} AS review_approved
                FROM mg_company_product_roles r
                WHERE r.country=%s AND r.as_of_date <= %s
                  AND r.role_state <> 'REJECTED' AND r.review_status <> 'REJECTED'
                  AND r.extraction_method=%s
                ORDER BY ticker, normalized_product, role_type, as_of_date DESC, id DESC
            """, review_params + (country, as_of, ROLE_EXTRACTOR_VERSION))
            aliases_by_normalized = {normalize_product(alias["product_label"]): alias for alias in aliases}
            for row in role_rows:
                alias = aliases_by_normalized.get(normalize_product(row["product_phrase"]))
                exact_key = (row.get("constraint_key")
                             if row.get("constraint_link_type") in {"EXACT", "EXACT_PRODUCT"}
                             else alias.get("constraint_key") if alias and alias.get("match_scope") == "EXACT"
                             else None)
                profile = register(
                    alias["product_label"] if alias else row["product_phrase"],
                    "COMPANY_PRODUCT_ROLE", exact_key, alias,
                )
                if profile:
                    profile["role_rows"].append(row)
                    observed = _as_date(row.get("last_evidence_date"))
                    if observed:
                        profile["evidence_dates"].append(observed)

        # The legacy capability snapshot and security-master join are India
        # specific. Cross-country exact roles come from the country-scoped
        # issuer role ledger above.
        if country == "IN" and _table_exists(cur, "mg_company_capabilities"):
            cur.execute("SELECT MAX(as_of_date) AS snapshot FROM mg_company_capabilities WHERE as_of_date <= %s", (as_of,))
            snapshot_row = cur.fetchone()
            snapshot = snapshot_row.get("snapshot") if snapshot_row else None
            if snapshot:
                if _table_exists(cur, "security_master"):
                    capability_rows = _rows(cur, """
                        SELECT c.ticker, c.product, c.n_docs, c.mfg_docs,
                               c.trade_docs, c.manufacturer,
                               COALESCE(s.company_name, c.ticker) AS company
                        FROM mg_company_capabilities c
                        LEFT JOIN security_master s
                          ON UPPER(TRIM(s.nse_symbol))=UPPER(TRIM(c.ticker))
                        WHERE c.as_of_date=%s AND c.manufacturer
                    """, (snapshot,))
                else:
                    capability_rows = _rows(cur, """
                        SELECT ticker, product, n_docs, mfg_docs, trade_docs,
                               manufacturer, ticker AS company
                        FROM mg_company_capabilities
                        WHERE as_of_date=%s AND manufacturer
                    """, (snapshot,))
                for row in capability_rows:
                    alias = _reference_alias(row["product"], aliases)
                    profile = register(
                        alias["product_label"] if alias else row["product"],
                        "CAPABILITY_MAPPER",
                        alias.get("constraint_key") if alias else None,
                        alias,
                    )
                    if profile:
                        profile["capability_rows"].append(row)

        if _table_exists(cur, "mg_constraint_observation_queue"):
            observation_rows = _rows(cur, """
                SELECT id, constraint_key, product_label, observation_type,
                       observed_at, published_at, available_at, source_key,
                       source_family, source_hash, source_url, product_scope,
                       revision, metrics, review_status, provenance_status
                FROM mg_constraint_observation_queue
                WHERE country=%s AND observed_at BETWEEN %s AND %s
                  AND COALESCE(available_at, published_at, observed_at) <= %s
            """, (country, cutoff, as_of, as_of))
            for row in latest_observation_revisions(observation_rows):
                if (row.get("review_status") != "ACCEPTED" or
                        row.get("provenance_status") != "PRIMARY_SOURCE"):
                    continue
                for profile in profiles.values():
                    if row["constraint_key"] not in profile["related_constraint_keys"]:
                        continue
                    source_key = (row.get("source_family") or row.get("source_key") or
                                  row.get("source_url"))
                    kind = row["observation_type"]
                    if kind in {"CAPACITY", "IMPORT"}:
                        admitted, reason = physical_observation_admissibility(row, as_of)
                        if not admitted:
                            profile["source_classes"].add("REJECTED_PHYSICAL_OBSERVATION")
                            continue
                    elif kind in {"DEMAND", "LEAD_TIME", "COMMISSIONING"}:
                        admitted, reason = nonphysical_observation_admissibility(row, as_of)
                        if not admitted:
                            profile["source_classes"].add("REJECTED_STATE_OBSERVATION")
                            continue
                    else:
                        # Trade-flow and policy observations can seed coverage,
                        # but do not establish a demand/binding/barrier leg.
                        profile["source_classes"].add("RESEARCH_CONTEXT_OBSERVATION")
                        continue
                    profile["source_families"].add(source_key)
                    profile["non_policy_source_families"].add(source_key)
                    profile["source_classes"].add("ACCEPTED_OBSERVATION")
                    profile["evidence_dates"].append(row["observed_at"])
                    profile["non_policy_evidence_dates"].add(row["observed_at"])
                    metrics = row.get("metrics") or {}
                    if kind == "CAPACITY" and metrics.get("gap_pct") is not None:
                        profile["capacity_gap_pct"] = float(metrics["gap_pct"])
                        profile["capacity_as_of"] = row["observed_at"]
                        profile["origins"].add("ACCEPTED_CAPACITY_OBSERVATION")
                        profile["physical_observation_ids"].add(row["id"])
                    elif kind == "IMPORT" and metrics.get("import_share") is not None:
                        profile["import_share"] = float(metrics["import_share"])
                        profile["import_as_of"] = row["observed_at"]
                        profile["primary_origin"] = metrics.get("primary_origin")
                        profile["substitution_horizon_years"] = metrics.get(
                            "substitution_horizon_years"
                        )
                        profile["origins"].add("ACCEPTED_IMPORT_OBSERVATION")
                        profile["physical_observation_ids"].add(row["id"])
                        if int(metrics.get("substitution_horizon_years") or 0) >= 3:
                            profile["barrier_sources"].add(source_key)
                    elif kind == "DEMAND":
                        profile["demand_sources"].add(source_key)
                    elif kind == "LEAD_TIME":
                        profile["binding_sources"].add(source_key)
                        profile["barrier_sources"].add(source_key)
                    elif kind == "COMMISSIONING":
                        profile["resolution_sources"].add(source_key)

        if _table_exists(cur, "mg_signals"):
            signal_rows = _rows(cur, """
                SELECT s.id, s.document_id, s.signal_type, s.context_text,
                       s.filed_at, COALESCE(s.perspective, 'neutral') AS perspective,
                       d.ticker, d.content_hash
                FROM mg_signals s
                JOIN mg_documents d ON d.id=s.document_id
                WHERE d.country=%s AND s.filed_at BETWEEN %s AND %s
                  AND s.signal_type = ANY(%s)
            """, (country, cutoff, as_of, RELEVANT_SIGNALS))
            for row in signal_rows:
                context = row.get("context_text") or ""
                # Multiple exchange uploads by one issuer on one date are one
                # evidence event, not independent corroboration.
                source_key = f"filing:{row.get('ticker') or 'UNKNOWN'}:{row['filed_at']}"
                for profile in profiles.values():
                    if not _context_matches_product(context, profile["product_label"]):
                        continue
                    signal_type = row["signal_type"]
                    profile["origins"].add("NLP_SIGNAL")
                    profile["source_families"].add(source_key)
                    profile["source_classes"].add("ISSUER_FILING")
                    profile["evidence_dates"].append(row["filed_at"])
                    if signal_type not in POLICY_SIGNALS:
                        profile["non_policy_source_families"].add(source_key)
                        profile["non_policy_evidence_dates"].add(row["filed_at"])
                    if signal_type in DEMAND_SIGNALS:
                        profile["demand_sources"].add(source_key)
                    if signal_type in BINDING_SIGNALS:
                        profile["binding_sources"].add(source_key)
                    if signal_type in BARRIER_SIGNALS:
                        profile["barrier_sources"].add(source_key)
                    if signal_type in POLICY_SIGNALS:
                        profile["policy_sources"].add(source_key)
                    if signal_type in RESOLUTION_SIGNALS:
                        profile["resolution_sources"].add(source_key)
                    if signal_type == "import_dependency_quantified":
                        profile["origins"].add("NLP_IMPORT_MEASURE")

        candidates: list[dict] = []
        company_candidates: list[dict] = []
        for profile in profiles.values():
            current_capacity = bool(
                profile["capacity_gap_pct"] is not None and profile["capacity_as_of"] and
                profile["capacity_as_of"] >= cutoff
            )
            current_import = bool(
                profile["import_share"] is not None and profile["import_as_of"] and
                profile["import_as_of"] >= cutoff
            )
            company_rows = self._company_candidates(profile, as_of, country)
            score_input = {
                "has_capacity_measure": current_capacity,
                "has_import_measure": current_import,
                "demand_source_count": len(profile["demand_sources"]),
                "binding_source_count": len(profile["binding_sources"]),
                "barrier_source_count": len(profile["barrier_sources"]),
                "policy_source_count": len(profile["policy_sources"]),
                "resolution_source_count": len(profile["resolution_sources"]),
                "independent_source_count": len(profile["source_families"]),
                "non_policy_source_count": len(profile["non_policy_source_families"]),
                "independent_date_count": len(profile["non_policy_evidence_dates"]),
                "physical_observation_count": len(profile["physical_observation_ids"]),
                "company_count": len({row["ticker"] for row in company_rows}),
                "ledger_evidenced": (
                    profile["ledger_state"] == "EVIDENCED" and
                    profile["ledger_classification"] == "PHYSICAL_CONSTRAINT"
                ),
            }
            scored = score_constraint_profile(score_input)
            first_detected = min(profile["evidence_dates"]) if profile["evidence_dates"] else None
            last_evidence = max(profile["evidence_dates"]) if profile["evidence_dates"] else None
            evidence_legs = {
                **score_input,
                "source_class_count": len(profile["source_classes"]),
                "source_classes": sorted(profile["source_classes"]),
                "capacity_gap_pct": float(profile["capacity_gap_pct"])
                    if profile["capacity_gap_pct"] is not None else None,
                "capacity_as_of": profile["capacity_as_of"].isoformat()
                    if profile["capacity_as_of"] else None,
                "import_share": float(profile["import_share"])
                    if profile["import_share"] is not None else None,
                "import_as_of": profile["import_as_of"].isoformat()
                    if profile["import_as_of"] else None,
                "primary_origin": profile["primary_origin"],
                "substitution_horizon_years": profile["substitution_horizon_years"],
                "ledger_state": profile["ledger_state"],
                "ledger_classification": profile["ledger_classification"],
                "related_constraint_keys": sorted(profile["related_constraint_keys"]),
                "physical_observation_ids": sorted(profile["physical_observation_ids"]),
                "physical_state": scored["physical_state"],
                "trajectory": scored["trajectory"],
                "evidence_completeness": scored["evidence_completeness"],
            }
            candidate = {
                "country": country,
                "as_of_date": as_of,
                "constraint_key": profile["constraint_key"],
                "product_label": profile["product_label"],
                "normalized_product": profile["normalized_product"],
                "theme_name": f"{profile['product_label']} — {scored['mechanism'].replace('_', ' ').title()}",
                "mechanism": scored["mechanism"],
                "candidate_class": scored["candidate_class"],
                "physical_quality": scored["physical_quality"],
                "physical_state": scored["physical_state"],
                "trajectory": scored["trajectory"],
                "evidence_completeness": scored["evidence_completeness"],
                "research_priority": scored["research_priority"],
                "research_state": scored["research_state"],
                "resolution_risk": scored["resolution_risk"],
                "detection_origins": sorted(profile["origins"]),
                "evidence_legs": evidence_legs,
                "missing_legs": scored["missing_legs"],
                "independent_source_count": score_input["independent_source_count"],
                "company_count": score_input["company_count"],
                "reviewed_company_count": sum(row["selection_state"] == "APPROVED_OPERATING_MAKER"
                                              for row in company_rows),
                "first_detected_date": first_detected,
                "last_evidence_date": last_evidence,
                "next_action": self._next_action(scored["missing_legs"]),
                "extractor_version": EXTRACTOR_VERSION,
            }
            candidates.append(candidate)
            for row in company_rows:
                company_candidates.append({
                    **row,
                    "constraint_key": profile["constraint_key"],
                    "normalized_product": profile["normalized_product"],
                    "product_label": profile["product_label"],
                })

        candidates.sort(key=lambda row: (-row["research_priority"], row["product_label"]))
        company_candidates.sort(key=lambda row: (
            row["constraint_key"], -row["role_confidence"], row["ticker"]
        ))
        return candidates, company_candidates

    @staticmethod
    def _company_candidates(profile: dict, as_of: date, country: str) -> list[dict]:
        by_ticker: dict[str, dict] = {}
        for row in profile["role_rows"]:
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            exact = row.get("constraint_link_type") in {"EXACT", "EXACT_PRODUCT"}
            approved = bool(row.get("review_approved"))
            evidenced = row.get("role_state") == "EVIDENCED"
            operating = row.get("role_type") == "MANUFACTURER" and int(row.get("physical_evidence_count") or 0) >= 2
            adjudication = row.get("adjudication_state") or ""
            auto_operating = adjudication in {
                "OPERATING_PRODUCER_EVIDENCED", "EARNINGS_CAPTURE_EVIDENCED",
            } and row.get("role_type") == "MANUFACTURER"
            auto_pipeline = adjudication == "PIPELINE_EVIDENCED"
            auto_commercial = (
                adjudication == "EARNINGS_CAPTURE_EVIDENCED" and
                row.get("role_type") == "DIRECT_PRODUCT_SUPPLIER"
            )
            if exact and evidenced and operating and (approved or auto_operating):
                state = "APPROVED_OPERATING_MAKER"
            elif exact and evidenced and auto_pipeline:
                state = "PIPELINE_DIRECT_ROLE"
            elif exact and evidenced and auto_commercial:
                state = "COMMERCIAL_DIRECT_ROLE"
            elif exact and evidenced:
                state = "EXACT_ROLE_REVIEW"
            else:
                state = "PRODUCT_ROLE_DISCOVERY"
            confidence = min(0.95, 0.30 +
                             0.10 * int(row.get("independent_document_count") or 0) +
                             0.10 * int(row.get("physical_evidence_count") or 0) +
                             (0.15 if exact else 0) +
                             (0.15 if approved or auto_operating or auto_commercial else 0))
            earnings_count = int(row.get("earnings_capture_count") or 0)
            pipeline_count = int(row.get("pipeline_evidence_count") or 0)
            by_ticker[ticker] = {
                "country": country, "as_of_date": as_of,
                "ticker": ticker, "company": row.get("company") or ticker,
                "role_type": row.get("role_type"), "role_state": row.get("role_state"),
                "link_type": row.get("constraint_link_type") or "UNLINKED",
                "independent_document_count": int(row.get("independent_document_count") or 0),
                "physical_evidence_count": int(row.get("physical_evidence_count") or 0),
                "pipeline_evidence_count": pipeline_count,
                "earnings_capture_count": earnings_count,
                "demand_signal_count": int(row.get("demand_or_policy_count") or 0),
                "role_confidence": round(confidence, 3),
                "selection_state": state,
                "earnings_capture_status": (
                    "CONFIRMED" if earnings_count >= 2 else
                    "INDICATED" if earnings_count else "UNPROVED"
                ),
                "evidence": row.get("evidence") or [],
                "adjudication_state": adjudication,
                "adjudication_reason": row.get("adjudication_reason"),
                "extractor_version": EXTRACTOR_VERSION,
            }
        for row in profile["capability_rows"]:
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker or ticker in by_ticker:
                continue
            mfg_docs = int(row.get("mfg_docs") or 0)
            by_ticker[ticker] = {
                "country": country, "as_of_date": as_of,
                "ticker": ticker, "company": row.get("company") or ticker,
                "role_type": "MANUFACTURER", "role_state": "DISCOVERY",
                "link_type": "MECHANICAL_PRODUCT_MATCH",
                "independent_document_count": int(row.get("n_docs") or 0),
                "physical_evidence_count": mfg_docs,
                "pipeline_evidence_count": 0,
                "earnings_capture_count": 0,
                "demand_signal_count": 0,
                "role_confidence": round(min(0.60, 0.25 + 0.05 * mfg_docs), 3),
                "selection_state": "CAPABILITY_LEAD",
                "earnings_capture_status": "UNPROVED",
                "evidence": [],
                "adjudication_state": None,
                "adjudication_reason": None,
                "extractor_version": EXTRACTOR_VERSION,
            }
        return list(by_ticker.values())

    @staticmethod
    def _next_action(missing_legs: list[str]) -> str:
        if not missing_legs:
            return "Validate resolution clock, earnings capture, valuation and risk before underwriting."
        return "Collect: " + "; ".join(missing_legs[:3]) + "."

    @staticmethod
    def ensure_schema(conn) -> None:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mg_constraint_candidates (
                    id BIGSERIAL PRIMARY KEY,
                    country VARCHAR(10) NOT NULL DEFAULT 'IN',
                    as_of_date DATE NOT NULL,
                    constraint_key VARCHAR(160) NOT NULL,
                    product_label TEXT NOT NULL,
                    normalized_product TEXT NOT NULL,
                    theme_name TEXT NOT NULL,
                    mechanism VARCHAR(40) NOT NULL,
                    physical_quality VARCHAR(16) NOT NULL,
                    physical_state VARCHAR(24) NOT NULL DEFAULT 'DISCOVERY',
                    trajectory VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN',
                    evidence_completeness VARCHAR(24) NOT NULL DEFAULT 'UNMEASURED',
                    research_priority INTEGER NOT NULL,
                    research_state VARCHAR(24) NOT NULL,
                    resolution_risk VARCHAR(24) NOT NULL,
                    detection_origins TEXT[] NOT NULL DEFAULT '{}',
                    evidence_legs JSONB NOT NULL DEFAULT '{}'::jsonb,
                    missing_legs TEXT[] NOT NULL DEFAULT '{}',
                    independent_source_count INTEGER NOT NULL DEFAULT 0,
                    company_count INTEGER NOT NULL DEFAULT 0,
                    reviewed_company_count INTEGER NOT NULL DEFAULT 0,
                    first_detected_date DATE,
                    last_evidence_date DATE,
                    next_action TEXT NOT NULL,
                    extractor_version VARCHAR(80) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(country, as_of_date, constraint_key, normalized_product)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_constraint_candidates_priority
                ON mg_constraint_candidates(country, as_of_date DESC,
                                             research_priority DESC)
            """)
            cur.execute("""
                ALTER TABLE mg_constraint_candidates
                  ADD COLUMN IF NOT EXISTS physical_state VARCHAR(24) NOT NULL DEFAULT 'DISCOVERY',
                  ADD COLUMN IF NOT EXISTS trajectory VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN',
                  ADD COLUMN IF NOT EXISTS evidence_completeness VARCHAR(24) NOT NULL DEFAULT 'UNMEASURED'
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mg_constraint_company_candidates (
                    id BIGSERIAL PRIMARY KEY,
                    country VARCHAR(10) NOT NULL DEFAULT 'IN',
                    as_of_date DATE NOT NULL,
                    constraint_key VARCHAR(160) NOT NULL,
                    normalized_product TEXT NOT NULL,
                    product_label TEXT NOT NULL,
                    ticker VARCHAR(32) NOT NULL,
                    company TEXT,
                    role_type VARCHAR(32) NOT NULL,
                    role_state VARCHAR(24) NOT NULL,
                    link_type VARCHAR(32) NOT NULL,
                    independent_document_count INTEGER NOT NULL DEFAULT 0,
                    physical_evidence_count INTEGER NOT NULL DEFAULT 0,
                    pipeline_evidence_count INTEGER NOT NULL DEFAULT 0,
                    earnings_capture_count INTEGER NOT NULL DEFAULT 0,
                    demand_signal_count INTEGER NOT NULL DEFAULT 0,
                    role_confidence NUMERIC(6,3) NOT NULL DEFAULT 0,
                    selection_state VARCHAR(40) NOT NULL,
                    earnings_capture_status VARCHAR(24) NOT NULL DEFAULT 'UNPROVED',
                    adjudication_state VARCHAR(48),
                    adjudication_reason TEXT,
                    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
                    extractor_version VARCHAR(80) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(country, as_of_date, constraint_key, normalized_product, ticker)
                )
            """)
            cur.execute("""
                ALTER TABLE mg_constraint_company_candidates
                  ADD COLUMN IF NOT EXISTS pipeline_evidence_count INTEGER NOT NULL DEFAULT 0,
                  ADD COLUMN IF NOT EXISTS earnings_capture_count INTEGER NOT NULL DEFAULT 0,
                  ADD COLUMN IF NOT EXISTS adjudication_state VARCHAR(48),
                  ADD COLUMN IF NOT EXISTS adjudication_reason TEXT
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_constraint_company_candidates_chain
                ON mg_constraint_company_candidates(country, as_of_date DESC,
                                                     constraint_key, role_confidence DESC)
            """)

    @staticmethod
    def persist(
        conn,
        candidates: list[dict],
        company_rows: list[dict],
        as_of: date,
        country: str,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mg_constraint_company_candidates WHERE country=%s AND as_of_date=%s",
                        (country, as_of))
            cur.execute("DELETE FROM mg_constraint_candidates WHERE country=%s AND as_of_date=%s",
                        (country, as_of))
            if candidates:
                payloads = []
                for row in candidates:
                    payloads.append({
                        **row,
                        "evidence_legs": psycopg2.extras.Json(row["evidence_legs"]),
                    })
                psycopg2.extras.execute_batch(cur, """
                    INSERT INTO mg_constraint_candidates
                      (country, as_of_date, constraint_key, product_label,
                       normalized_product, theme_name, mechanism, physical_quality,
                       physical_state, trajectory, evidence_completeness,
                       research_priority, research_state, resolution_risk,
                       detection_origins, evidence_legs, missing_legs,
                       independent_source_count, company_count, reviewed_company_count,
                       first_detected_date, last_evidence_date, next_action,
                       extractor_version)
                    VALUES
                      (%(country)s, %(as_of_date)s, %(constraint_key)s, %(product_label)s,
                       %(normalized_product)s, %(theme_name)s, %(mechanism)s,
                       %(physical_quality)s, %(physical_state)s, %(trajectory)s,
                       %(evidence_completeness)s, %(research_priority)s, %(research_state)s,
                       %(resolution_risk)s, %(detection_origins)s, %(evidence_legs)s,
                       %(missing_legs)s, %(independent_source_count)s, %(company_count)s,
                       %(reviewed_company_count)s, %(first_detected_date)s,
                       %(last_evidence_date)s, %(next_action)s, %(extractor_version)s)
                """, payloads, page_size=250)
            if company_rows:
                payloads = []
                for row in company_rows:
                    payloads.append({**row, "evidence": psycopg2.extras.Json(row["evidence"])})
                psycopg2.extras.execute_batch(cur, """
                    INSERT INTO mg_constraint_company_candidates
                      (country, as_of_date, constraint_key, normalized_product,
                       product_label, ticker, company, role_type, role_state,
                       link_type, independent_document_count, physical_evidence_count,
                       pipeline_evidence_count, earnings_capture_count,
                       demand_signal_count, role_confidence, selection_state,
                       earnings_capture_status, adjudication_state,
                       adjudication_reason, evidence, extractor_version)
                    VALUES
                      (%(country)s, %(as_of_date)s, %(constraint_key)s,
                       %(normalized_product)s, %(product_label)s, %(ticker)s,
                       %(company)s, %(role_type)s, %(role_state)s, %(link_type)s,
                       %(independent_document_count)s, %(physical_evidence_count)s,
                       %(pipeline_evidence_count)s, %(earnings_capture_count)s,
                       %(demand_signal_count)s, %(role_confidence)s,
                       %(selection_state)s, %(earnings_capture_status)s,
                       %(adjudication_state)s, %(adjudication_reason)s,
                       %(evidence)s, %(extractor_version)s)
                """, payloads, page_size=250)
