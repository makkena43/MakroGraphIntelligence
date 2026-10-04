#!/usr/bin/env python3
"""
Stock Selector Data Extractor — surfaces, for a given as-of date, what is
happening/emerging in the last 6-12 months across the MakroGraph DB:
major themes, major constraints (with evidence), supply-side beneficiaries,
and a ranked candidate-stock list with a light technical overlay.

Usage:
    python scripts/stock_report/select_stocks.py --as-of 2026-07-05 [--window-months 12]

Output:
    data/reports/stock_selector_<ASOF>_data.json  (path printed on stdout)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_report_data import connect, q, jsonify, parse_as_of, REPORTS_DIR  # noqa: E402
from moonshot_screen import compute_moonshot_candidates  # noqa: E402
from company_capabilities import capability_makers, candidate_terms, term_regex  # noqa: E402
from company_product_roles import (  # noqa: E402
    EXTRACTION_METHOD,
    fetch_company_product_discoveries,
    fetch_company_role_review_queue,
    merge_exact_role_makers,
)
from constraint_ledger import (  # noqa: E402
    fetch_constraint_aliases,
    fetch_constraint_ledger,
    fetch_observation_summary,
    normalize_product_label,
)
from constraint_ranker import build_final_selection, rank_greatest_constraints  # noqa: E402
from constraint_emergence import (  # noqa: E402
    attach_constraint_exposure_companies, attach_emerging_constraint_makers,
    detect_emerging_constraints, fetch_all_constraint_emergence,
    fetch_verified_maker_universe)
from constraint_emergence import _norm as _emergence_canon  # noqa: E402
from investment_mechanisms import build_investment_mechanisms  # noqa: E402
from src.makrograph.constraint_contract import (  # noqa: E402
    COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
    CONSTRAINT_CANDIDATE_EXTRACTOR_VERSION,
)


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VALIDATION_LOGIC_FILES = (
    "src/makrograph/constraint_contract.py",
    "src/makrograph/india/constraint_candidate_engine.py",
    "scripts/policy/queue_constraint_observations.py",
    "scripts/policy/materialize_constraint_ledger.py",
    "scripts/stock_report/company_product_roles.py",
    "scripts/stock_report/constraint_ledger.py",
    "scripts/stock_report/constraint_ranker.py",
    "scripts/stock_report/constraint_emergence.py",
    "scripts/stock_report/select_stocks.py",
)


def build_logic_manifest() -> dict:
    """Hash the exact detector/selector implementation under evaluation."""
    hashes = {}
    combined = hashlib.sha256()
    for relative in VALIDATION_LOGIC_FILES:
        path = os.path.join(PROJECT_ROOT, relative)
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        hashes[relative] = digest
        combined.update(relative.encode("utf-8"))
        combined.update(digest.encode("ascii"))
    return {
        "logic_hash": combined.hexdigest(),
        "files": hashes,
        "constraint_candidate_extractor_version": CONSTRAINT_CANDIDATE_EXTRACTOR_VERSION,
        "company_role_extractor_version": COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
    }


def validation_status(as_of: date, country: str) -> dict:
    """Load performance only when it was produced by this exact logic version."""
    logic = build_logic_manifest()
    manifest_path = os.path.join(
        REPORTS_DIR, f"selector_validation_manifest_{country.upper()}.json"
    )
    status = {
        "status": "UNVALIDATED_VERSION",
        "reason": "No version-matched forward-test manifest is available.",
        "logic": logic,
        "validation_manifest": None,
    }
    if not os.path.exists(manifest_path):
        return status
    try:
        with open(manifest_path) as handle:
            validation = json.load(handle)
        generated_date = date.fromisoformat(validation["generated_at"][:10])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        status["reason"] = f"Validation manifest is unreadable: {exc}"
        return status
    if generated_date > as_of:
        status["reason"] = "Validation completed after this historical report date and is withheld."
        return status
    if validation.get("logic_hash") != logic["logic_hash"]:
        status["reason"] = "Detector/selector code changed after the recorded forward test."
        status["validation_manifest"] = validation
        return status
    status.update({
        "status": validation.get("status") or "VALIDATED_MATCHING_VERSION",
        "reason": "Forward-test logic hash matches this report.",
        "validation_manifest": validation,
    })
    return status


def build_data_manifest(cur, as_of: date, country: str) -> dict:
    """Fingerprint the point-in-time evidence snapshots consumed by the report."""
    specs = (
        ("mg_constraint_observation_queue", "observed_at", "country"),
        ("mg_constraint_ledgers", "as_of_date", "country"),
        ("mg_constraint_candidates", "as_of_date", "country"),
        ("mg_company_product_roles", "as_of_date", "country"),
        ("mg_documents", "filed_at", "country"),
    )
    tables = {}
    for table, date_column, country_column in specs:
        cur.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",))
        if not cur.fetchone()["name"]:
            continue
        cur.execute(
            f"SELECT COUNT(*)::bigint AS n, MAX({date_column})::date AS max_date "
            f"FROM {table} WHERE {country_column}=%s AND {date_column}::date <= %s",
            (country, as_of),
        )
        row = cur.fetchone()
        tables[table] = {
            "rows_available": int(row["n"] or 0),
            "max_evidence_date": row["max_date"].isoformat() if row["max_date"] else None,
        }
    serialized = json.dumps(tables, sort_keys=True, separators=(",", ":"))
    return {
        "as_of_date": as_of.isoformat(),
        "tables": tables,
        "data_snapshot_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def canonical_product_key(s: str | None) -> str:
    """One canonical key for the SAME product written differently across the
    subsystems that never agreed on a vocabulary:
      capability mapper   "Printed Circuit Boards"
      beneficiary mapper  "PCB / Printed Circuit Board"
      trade feed          "Solar Wafers" / "Defense Electronics (radar, avionics)"
      HS taxonomy         "Electrical transformers ... (parts thereof)"
    Exact-string matching between any two of these silently dropped whole
    signals — the corroboration join, the upstream-pipeline cross-check, and the
    import-momentum leading indicator each broke this exact way and were each
    patched with a private copy of this normalisation. Routing every
    cross-subsystem product match through ONE function stops those copies from
    drifting and makes the next match site correct by default.

    Normalisation: lowercase, keep the last "/"-segment (drops an acronym alias
    prefix), strip parentheticals, collapse non-alphanumerics to spaces, and
    singularise each word (>3 chars) so "Boards"=="Board", "Wafers"=="Wafer".
    Mirror any change here in backfill_corroboration's SQL key.
    """
    s = re.sub(r"^.*/\s*", "", (s or "").lower())
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w
                    for w in s.split())


# Abstract/financial/process head nouns that never denote a manufacturable
# product. The generic manufacturing-grammar extractor that seeds the US product
# vocabulary catches verbose SEC prose ("we manufacture X to reduce overhead
# costs / address climate change"), so a chunk of "products" are really the
# object of a financial or operational clause. A real constraint product is a
# concrete noun phrase; reject phrases whose HEAD noun is abstract, or that
# carry a sentence-fragment period. Judged on the singularised head token so it
# stays a rule, not a product blocklist, and it rejects ZERO real India or US
# product ("materials"/"components"/"electronics" are deliberately NOT here so
# Cathode Active Materials / Passive Components / Defense electronics survive).
_ABSTRACT_HEAD = {
    "charge", "benefit", "loan", "saving", "time", "change", "disposal",
    "located", "defect", "synergy", "calculation", "cost", "technology",
    "gathering", "well", "utilization", "claim", "proposal", "agreement",
    "date", "reserve", "practice", "growth", "excellence", "operation",
    "activity", "trial", "requirement", "relationship", "service", "solution",
    "fee", "rate", "margin", "expense", "site", "location", "portion",
    "process", "initiative", "strategy", "opportunity", "risk", "exposure",
    "measure", "metric", "target", "guidance", "outlook", "synergie",
}


def _looks_like_product(label: str | None) -> bool:
    p = (label or "").strip().lower()
    if "." in p or len(p) < 3:
        return False
    words = re.findall(r"[a-z0-9&/+-]+", p)
    if not words:
        return False
    head = words[-1]
    head = head[:-1] if len(head) > 3 and head.endswith("s") else head
    return head not in _ABSTRACT_HEAD


# ──────────────────────────────────────────────────────────────────────────
# Themes
# ──────────────────────────────────────────────────────────────────────────

def fetch_theme_landscape(cur, as_of, window_months, country="IN"):
    win_start = as_of - timedelta(days=window_months * 30)
    themes = q(cur, """
        SELECT id AS theme_id, theme_name, theme_slug, description, sectors, conviction,
               first_detected, stage, stage_label, stage_evidence, strength_score,
               momentum_score, company_count, doc_count, metadata,
               is_canonical, parent_theme_slug
        FROM mg_themes
        WHERE country=%s AND is_active
          AND first_detected <= %s
        ORDER BY strength_score DESC NULLS LAST
        LIMIT 400
    """, (country, as_of,))

    out = []
    for t in themes:
        meta = t.pop("metadata") or {}
        t["supply_constraint_count"] = meta.get("supply_constraint_count") or 0
        t["is_bottleneck"] = bool(meta.get("is_bottleneck"))
        t["tension_score"] = meta.get("tension_score")
        t["explosive_score"] = meta.get("explosive_score")
        t["confirmed_quarters"] = meta.get("confirmed_quarters")
        snaps = q(cur, """
            SELECT snapshot_date, strength_score, momentum_score, company_count
            FROM mg_theme_snapshots
            WHERE theme_id=%s AND snapshot_date <= %s
            ORDER BY snapshot_date DESC LIMIT 14
        """, (t["theme_id"], as_of))
        now_s = snaps[0]["strength_score"] if snaps else t["strength_score"]
        prev = [s for s in snaps if s["snapshot_date"] <= as_of - timedelta(days=182)]
        prev_s = prev[0]["strength_score"] if prev else None
        t["strength_now"] = round(now_s or 0, 1)
        t["strength_6mo_ago"] = round(prev_s, 1) if prev_s is not None else None
        t["strength_delta_6mo"] = round((now_s or 0) - prev_s, 1) if prev_s is not None else None
        t["last_snapshot_date"] = snaps[0]["snapshot_date"] if snaps else None

        # emergence via beneficiary growth (snapshots are often flat): how many
        # companies got newly linked to the theme inside the window vs before it
        growth = q(cur, """
            SELECT count(*) FILTER (WHERE first_seen_at >= %s AND first_seen_at <= %s) AS new_in_window,
                   count(*) FILTER (WHERE first_seen_at < %s)  AS before_window
            FROM mg_theme_beneficiaries
            WHERE theme_id=%s AND first_seen_at IS NOT NULL AND first_seen_at <= %s
        """, (win_start, as_of, win_start, t["theme_id"], as_of))[0]
        t["new_beneficiaries_in_window"] = growth["new_in_window"]
        t["beneficiaries_before_window"] = growth["before_window"]
        base = max(1, growth["before_window"])
        t["beneficiary_growth_ratio"] = round(growth["new_in_window"] / base, 2)

        t["is_emerging_window"] = bool(
            (t["first_detected"] and t["first_detected"] >= win_start)
            or (t["strength_delta_6mo"] is not None and t["strength_delta_6mo"] >= 10)
            or growth["new_in_window"] >= 8
            or (growth["new_in_window"] >= 4 and t["beneficiary_growth_ratio"] >= 0.4)
        )
        out.append(t)

    major = sorted(out, key=lambda x: -(x["strength_now"] or 0))[:15]
    emerging = sorted([t for t in out if t["is_emerging_window"]],
                      key=lambda x: -x["new_beneficiaries_in_window"])[:15]
    return major, emerging, out


# ──────────────────────────────────────────────────────────────────────────
# Constraints
# ──────────────────────────────────────────────────────────────────────────

def fetch_constraints(cur, as_of, window_months, country="IN"):
    win_start = as_of - timedelta(days=window_months * 30)
    # constrained products from the beneficiary mapper — the core "what is short"
    products = q(cur, """
        SELECT constrained_product, theme_name,
               count(DISTINCT company)                    AS n_companies,
               round(avg(conviction_score)::numeric, 3)   AS avg_conviction,
               round(max(conviction_score)::numeric, 3)   AS max_conviction,
               bool_or(has_order_book_signals)            AS any_order_book,
               bool_or(import_substitution_play)          AS any_import_sub,
               min(as_of_date) AS first_mapped, max(as_of_date) AS last_mapped
        FROM mg_india_beneficiaries
        WHERE constrained_product IS NOT NULL
          AND as_of_date <= %s AND as_of_date >= %s
        GROUP BY constrained_product, theme_name
        ORDER BY max(conviction_score) DESC NULLS LAST, count(DISTINCT company) DESC
        -- This is the constraint *universe*, not the reader-facing shortlist.
        -- A downstream equipment bottleneck can rank below an upstream input
        -- yet be the listed layer that captures the economics.  The final
        -- report remains compact; truncating here would make that monetiser
        -- invisible before the Early/Timing gate can assess it.
        LIMIT 100
    """, (as_of, win_start)) if country == "IN" else []

    # Upstream ingestion/NLP constraint denominator. This materialized stage
    # can discover a product before the legacy beneficiary mapper has attached
    # a company. It expands research coverage only; the downstream ranker still
    # requires its own dated physical and company gates.
    cur.execute("SELECT to_regclass('public.mg_constraint_candidates') AS name")
    if cur.fetchone().get("name"):
        upstream = q(cur, """
            SELECT DISTINCT ON (constraint_key, normalized_product)
                   constraint_key, product_label AS constrained_product,
                   theme_name, company_count AS n_companies,
                   research_priority, research_state, mechanism,
                   physical_quality, detection_origins, evidence_legs,
                   missing_legs, independent_source_count,
                   first_detected_date AS first_mapped,
                   last_evidence_date AS last_mapped, as_of_date
            FROM mg_constraint_candidates
            WHERE country=%s AND as_of_date <= %s
              AND research_priority >= 20
            ORDER BY constraint_key, normalized_product, as_of_date DESC,
                     research_priority DESC
        """, (country, as_of))
        for row in upstream:
            products.append({
                **row,
                "avg_conviction": None,
                "max_conviction": None,
                "any_order_book": bool((row.get("evidence_legs") or {}).get("binding_source_count")),
                "any_import_sub": row.get("mechanism") == "LOCALISATION_QUALIFICATION",
                "detection_origin": "UPSTREAM_CONSTRAINT_PIPELINE",
                "upstream_candidate": row,
            })

    # A physical reference is admissible only on its own stated observation
    # date.  Using ``created_at`` here would let a later refreshed value appear
    # in an earlier replay merely because the database row already existed.
    # Preserve every component's newest *as-of* record; rank_greatest_constraints
    # decides whether it is still current enough to support a physical grade.
    #
    # Publication lag: structural capacity/import figures are stated for a
    # reference date (as_of_date) but only PUBLISHED some months later (a
    # Dec-2024 capacity number is reported in ~Apr-2025).  Selecting purely by
    # the newest as_of_date grabs a figure that was not yet knowable at the run
    # date; the grader then correctly rejects it on source_published_at, but the
    # older, already-published observation underneath is never seen and the
    # component silently loses its magnitude leg.  Restrict candidates to rows
    # PUBLISHED by the run date (NULL publication = a live/legacy row governed by
    # as_of_date alone), so DISTINCT ON returns the most recent figure that was
    # actually available at the anchor.  This is a point-in-time correctness fix,
    # never a look-ahead.
    gaps = q(cur, """
        SELECT DISTINCT ON (lower(BTRIM(component)))
               sector, component, gap, gap_pct, unit, supply_chain_stage,
               theme_name, severity, target_year, as_of_date,
               source_url, source_title, source_published_at, source_family,
               provenance_status, ingestion_method, measurement_basis
        FROM mg_capacity_gaps
        WHERE component IS NOT NULL AND BTRIM(component) <> ''
          AND as_of_date <= %s
          AND (source_published_at IS NULL OR source_published_at <= %s)
        ORDER BY lower(BTRIM(component)), as_of_date DESC NULLS LAST, gap_pct DESC NULLS LAST
    """, (as_of, as_of)) if country == "IN" else []

    imports = q(cur, """
        SELECT DISTINCT ON (lower(BTRIM(component)))
               sector, component, import_share, import_value_bn_usd, primary_origin,
               substitute_possible, substitution_horizon_years, risk_level, as_of_date,
               source_url, source_title, source_published_at, source_family,
               provenance_status, ingestion_method, measurement_basis
        FROM mg_import_dependencies
        WHERE component IS NOT NULL AND BTRIM(component) <> ''
          AND as_of_date <= %s
          AND (source_published_at IS NULL OR source_published_at <= %s)
        ORDER BY lower(BTRIM(component)), as_of_date DESC NULLS LAST, import_share DESC NULLS LAST
    """, (as_of, as_of)) if country == "IN" else []
    return products, gaps, imports


# ──────────────────────────────────────────────────────────────────────────
# Supply-side beneficiaries
# ──────────────────────────────────────────────────────────────────────────

SUPPLY_SIDE_TYPES = ("direct_supplier", "critical_supplier", "input_supplier")

# ── Lesson 2: per-chain sector allowlist ─────────────────────────────────────
# A company's industry_nse must contain at least ONE of these substrings to be
# considered a plausible supply-side participant in the chain.  Key is a
# lowercase substring of the chain / constrained-product name.
# Chains not matched by any key are passed through unchecked (no rule = no filter).
CHAIN_SECTOR_ALLOWLIST: dict[str, list[str]] = {
    "battery":        ["automobile", "auto component", "capital goods", "power",
                       "electrical", "chemical", "diversified"],
    "defense":        ["capital goods", "defence", "defense", "aerospace",
                       "information technology", "electrical", "engineering", "diversified"],
    "optical fiber":  ["capital goods", "telecommunication", "electrical",
                       "information technology", "engineering", "diversified"],
    "transformer":    ["capital goods", "power", "electrical", "engineering",
                       "construction", "diversified"],
    "rolling stock":  ["capital goods", "construction", "engineering", "railway",
                       "electrical", "mechanical", "diversified"],
    "solar":          ["power", "capital goods", "electrical", "engineering",
                       "renewabl", "diversified"],
    "pcb":            ["capital goods", "information technology", "electrical",
                       "electronic", "diversified"],
    "ems":            ["capital goods", "information technology", "electrical",
                       "electronic", "diversified"],
    "crgo":           ["capital goods", "steel", "electrical", "engineering",
                       "metal", "diversified"],
    "cable":          ["capital goods", "electrical", "engineering",
                       "construction", "diversified"],
    "wire":           ["capital goods", "electrical", "engineering", "diversified"],
    "semiconductor":  ["capital goods", "information technology", "electrical",
                       "electronic", "chemical", "diversified"],
}

# ── Lesson 3: supply_chain_node values that mark a company as a consumer ─────
# Companies with these nodes are downstream operators, not supply-side capacity
# owners.  They receive a heavy conviction discount rather than exclusion so they
# remain visible in the per-chain lists but cannot surface in ranked_candidates.
DEMAND_CONSUMER_NODES: frozenset[str] = frozenset({
    "consumer", "operator", "end_user", "buyer", "off-taker",
    "offtaker", "end-user", "subscriber",
})

# ── Lesson 4: risk-flag filing types from mg_documents ───────────────────────
HIGH_RISK_FILING_TYPES: frozenset[str] = frozenset({
    "Fraud/Default/Arrest",
    "CIRP - Commencement",
    "Corporate Insolvency Resolution Process",
    "Liquidation",
    "Suspension of Trading",
})
ELEVATED_RISK_FILING_TYPES: frozenset[str] = frozenset({
    "Change in Auditors",
    "Resignation of Statutory Auditor",
    "Defaults on Payment of Interest/Principal",
    "One Time Settlement",
    "One time settlement",
    "Reasons for Delayed/Non-submission of Financial Results",
    # "Action(s) initiated or orders passed" removed — validation showed it is a
    # routine regulatory disclosure filed by virtually every listed company and
    # added noise without predictive value (ELEVATED outperformed NORMAL in 2025).
})

# The GENSOL lesson (Jul-2026): its SEBI enforcement arrived under the noisy
# filing_type that was removed above — filing-TYPE screens alone miss it. These
# TEXT patterns catch severe governance events in the disclosure title/body
# regardless of how the exchange classified the filing. Linguistic patterns,
# not entity names.
GOVERNANCE_TEXT_PATTERNS: dict[str, str] = {
    "SEBI interim/final order":  r"SEBI.{0,60}(interim order|final order|restrain|debar|impound)",
    "search/seizure/raid":       r"search and seizure|ED raid|CBI (raid|search)|income tax (raid|search|survey)",
    "forensic audit":            r"forensic audit",
    "funds diversion":           r"diversion of funds|siphon|misappropriat|falsif",
    "promoter pledge invoked":   r"invocation of pledge|pledge.{0,30}invoked",
    "rating default grade":      r"downgrade[d]? to .{0,20}(\yD\y|default)|rating.{0,30}withdrawn.{0,40}non.?cooperat",
    "insolvency petition":       r"insolvency (petition|application)|section 7 of (the )?IBC|NCLT.{0,50}admitted",
}

# ── Lesson 6: large-cap market proxy for regime detection ────────────────────
# Market benchmark basket: computed point-in-time as the top-25 symbols by
# trailing-12m traded value (see init_reference_data) — no hardcoded tickers
# (user rule, Jul-2026). Populated in place so downstream queries are unchanged.
MARKET_PROXY_IN: list[str] = []

def _freshness_multiplier(min_confirmed_quarters: int) -> tuple[float, str]:
    """Lesson 1 + 5: fresher themes generated higher alpha in 2020-2024 backtest.

    Q4 (lowest fundamental score, freshest discovery) outperformed Q1 by +48pp alpha.
    Mature Consensus themes (16+ quarters) are typically already priced in.
    """
    if min_confirmed_quarters == 0:
        return 1.10, "new_theme"        # just detected, not yet confirmed
    if min_confirmed_quarters <= 4:
        return 1.12, "fresh"            # early Accelerating — highest alpha potential
    if min_confirmed_quarters <= 8:
        return 1.05, "developing"       # mid-Accelerating
    if min_confirmed_quarters <= 15:
        return 1.00, "established"
    return 0.93, "consensus"            # 16+ quarters = priced in, marginal buyers shrinking


def _get_chain_key(chain_name: str) -> str | None:
    """Return the CHAIN_SECTOR_ALLOWLIST key that matches chain_name, or None."""
    name_lower = (chain_name or "").lower()
    for key in CHAIN_SECTOR_ALLOWLIST:
        if key in name_lower:
            return key
    return None


def _sector_check(industry_nse: str | None, chain_name: str) -> bool | None:
    """True = compatible, False = mismatch, None = no rule / unknown."""
    if not industry_nse:
        return None
    key = _get_chain_key(chain_name)
    if not key:
        return None
    ind_lower = industry_nse.lower()
    return any(a in ind_lower for a in CHAIN_SECTOR_ALLOWLIST[key])


def fetch_capability_beneficiaries(cur, as_of):
    """Supply-side names sourced from what companies MAKE, not from what they
    talk about (mg_company_capabilities, built by company_capabilities.py).

    Why this path exists: the beneficiary mapper links a company to a theme
    only when its filings CO-OCCUR with theme-narrative documents, so a
    pure-play filing dedicated documents is invisible. The winner autopsy
    measured the cost — 82% of >=5x winners never reached the universe, and
    97% of those already had substantive filing text. Worse, the bias is not
    random: it selects FOR companies marketing themselves as theme plays and
    AGAINST quiet manufacturers (the Voltamp/TARIL failure mode named in the
    skill file — both are recovered by this path).

    Provenance is kept explicit rather than blended away:
      - beneficiary_type 'product_manufacturer'
      - evidence_source 'capability_mapper'
      - conviction derived ONLY from manufacturing-evidence density, and
        deliberately capped BELOW the theme-mapped range so these names can
        never outrank a company with real chain evidence. They are leads for
        the judgment layer, not pre-scored picks.
    Point-in-time: uses the newest capability snapshot <= as_of."""
    try:
        cur.execute("""SELECT MAX(as_of_date) AS s FROM mg_company_capabilities
                       WHERE as_of_date <= %s""", (as_of,))
        row = cur.fetchone()
        snap = row["s"] if row else None
    except Exception:
        return {}                      # table absent on an older DB snapshot
    if not snap:
        return {}

    rows = q(cur, """SELECT UPPER(TRIM(c.ticker)) AS ticker, c.product,
                            c.n_docs, c.mfg_docs,
                            s.company_name, s.industry_nse, s.industry_bse,
                            s.sector_bse
                     FROM mg_company_capabilities c
                     LEFT JOIN security_master s
                            ON UPPER(TRIM(s.nse_symbol)) = UPPER(TRIM(c.ticker))
                     WHERE c.as_of_date = %s AND c.manufacturer""", (snap,))

    by_product = defaultdict(list)
    for r in rows:
        # Evidence density -> conviction, capped at 0.45 so a capability lead
        # always sits below a genuinely chain-mapped beneficiary. The cap is
        # the point: these carry manufacturing proof but NO constraint-chain
        # evidence, and the ranking must reflect that asymmetry.
        mfg = int(r["mfg_docs"] or 0)
        conviction = min(0.45, 0.15 + 0.05 * mfg)
        by_product[r["product"]].append({
            "company": r["company_name"] or r["ticker"],
            "ticker": r["ticker"],
            "theme_name": r["product"],
            "constrained_product": r["product"],
            "supply_chain_node": "manufacturer",
            "supply_chain_stage": None,
            "beneficiary_type": "product_manufacturer",
            "conviction_score": conviction,
            "rationale": (f"own filings evidence manufacture of {r['product']} "
                          f"({r['n_docs']} filings, {mfg} with capacity language); "
                          f"no theme-chain mapping — judgment must verify the "
                          f"constraint link before this is actionable"),
            "signal_count": int(r["n_docs"] or 0),
            "has_order_book_signals": False,
            "import_substitution_play": True,
            "as_of_date": snap,
            "industry_nse": r["industry_nse"],
            "industry_detail": r["industry_bse"],
            "sector": r["sector_bse"],
            "sector_mismatch": False,
            "capex_signals": 0,
            "demand_side_flag": False,
            "evidence_source": "capability_mapper",
        })
    return dict(by_product)


def fetch_supply_beneficiaries(cur, as_of, window_months, per_product=None, country="IN"):
    """Return the full per-product discovery population.

    Presentation code can display a compact top slice, but truncating here
    made the eighth mapper row the effective company-universe boundary and
    hid valid names before the role-validation stage could inspect them.
    """
    win_start = as_of - timedelta(days=window_months * 30)
    rows = q(cur, """
        SELECT DISTINCT ON (constrained_product, company)
               company, ticker, theme_name, constrained_product, supply_chain_node,
               supply_chain_stage, beneficiary_type, conviction_score, rationale,
               signal_count, has_order_book_signals, import_substitution_play, as_of_date,
               corroborated
        FROM mg_india_beneficiaries
        WHERE beneficiary_type = ANY(%s)
          AND as_of_date <= %s AND as_of_date >= %s
        ORDER BY constrained_product, company, as_of_date DESC
    """, (list(SUPPLY_SIDE_TYPES), as_of, win_start)) if country == "IN" else []

    # NOTE (investigated, deliberately NOT wired): a constraint can have zero
    # direct/critical/input suppliers because the product genuinely isn't made
    # in India — Semiconductor IC is the case: no domestic fab, so all 187 of
    # its beneficiaries are typed 'ecosystem_participant'. Pulling those in as a
    # fallback surfaces the constraint but with an INCOHERENT maker list — the
    # ecosystem tag is pure co-occurrence (solar/power/auto names like
    # WEBELSOLAR, POWERINDIA read as "semiconductor ecosystem"), and with no
    # capability coverage for ICs there is nothing to clean it. Showing
    # WEBELSOLAR as a semiconductor play is worse than honest omission (unlike
    # CRGO Steel, whose uncorroborated makers are a COHERENT set of transformer
    # firms that consume CRGO). Left out until the ecosystem mapping is coherent
    # or IC design/ATMP capability coverage exists.

    # Literal same-product company roles from the upstream constraint stage.
    # They are kept as research candidates (not direct/critical beneficiaries)
    # until the separate earnings-capture gate is proved.
    upstream_rows = []
    cur.execute("SELECT to_regclass('public.mg_constraint_company_candidates') AS name")
    if cur.fetchone().get("name"):
        upstream_rows = q(cur, """
            SELECT DISTINCT ON (constraint_key, normalized_product, ticker)
                   company, ticker, product_label AS constrained_product,
                   product_label AS theme_name, role_type, role_state,
                   link_type, independent_document_count, physical_evidence_count,
                   pipeline_evidence_count, earnings_capture_count,
                   demand_signal_count, role_confidence, selection_state,
                   earnings_capture_status, adjudication_state,
                   adjudication_reason, as_of_date
            FROM mg_constraint_company_candidates
            WHERE country=%s AND as_of_date <= %s
            ORDER BY constraint_key, normalized_product, ticker,
                     as_of_date DESC, role_confidence DESC
        """, (country, as_of))
        # Cross-check against capability data. Without this, an upstream
        # DISCOVERY-stage lead (role_confidence as low as 0.55, ZERO physical
        # evidence, a single independent document — e.g. INOXWIND on 'Solar
        # Cell') never gets a corroborated=False verdict, because this table's
        # own policy only ever sets True (APPROVED_OPERATING_MAKER) or None —
        # by design, so a thin pipeline lead isn't erased before review. But
        # that leaves it invisible to the corroboration gate FOREVER, even once
        # capability data proves the company isn't a maker of that product.
        # Reusing capability data (already validated, not a "no evidence"
        # verdict) here closes that gap the same way backfill_corroboration
        # does for the beneficiary-mapper rows.
        _norm_prod_key = canonical_product_key
        cap_makers_by_key = defaultdict(set)   # normalized product -> {tickers}
        cap_products_by_key = set()            # normalized products WITH coverage
        try:
            cap_rows = q(cur, """
                SELECT product, UPPER(TRIM(ticker)) AS ticker
                FROM mg_company_capabilities
                WHERE manufacturer AND as_of_date = (
                    SELECT MAX(as_of_date) FROM mg_company_capabilities
                    WHERE as_of_date <= %s)
            """, (as_of,))
            for r in cap_rows:
                k = _norm_prod_key(r["product"])
                cap_makers_by_key[k].add(r["ticker"])
                cap_products_by_key.add(k)
        except Exception:
            pass

        for row in upstream_rows:
            approved_operating = row.get("selection_state") == "APPROVED_OPERATING_MAKER"
            upstream_corroborated = True if approved_operating else None
            if upstream_corroborated is None:
                pk = _norm_prod_key(row.get("constrained_product"))
                if pk in cap_products_by_key:
                    tick = (row.get("ticker") or "").strip().upper()
                    upstream_corroborated = tick in cap_makers_by_key[pk]
                else:
                    # No capability coverage for this product — the case for
                    # EVERY US product (the capability mapper is India-only). An
                    # issuer-owned product role that passed the strict role test
                    # AND carries physical or repeated-independent-document
                    # evidence IS corroboration: it is the same "own-filing
                    # manufacturing proof" the capability mapper provides for
                    # India, just sourced from the role extractor. Without this,
                    # the confidence gate (needs stage OR corroborated OR
                    # import_sub) drops the entire US list, since US products
                    # have none of the other three.
                    if (int(row.get("physical_evidence_count") or 0) > 0
                            or int(row.get("independent_document_count") or 0) >= 2):
                        upstream_corroborated = True
            rows.append({
                "company": row.get("company") or row.get("ticker"),
                "ticker": row.get("ticker"),
                "theme_name": row.get("theme_name"),
                "constrained_product": row.get("constrained_product"),
                "supply_chain_node": row.get("role_type"),
                "supply_chain_stage": 0,
                "beneficiary_type": (
                    "direct_supplier" if approved_operating else "product_role_candidate"
                ),
                "conviction_score": float(row.get("role_confidence") or 0),
                "rationale": (
                    f"Upstream same-product role: {row.get('selection_state')}; "
                    f"{row.get('independent_document_count') or 0} independent issuer disclosures; "
                    "earnings capture remains unproved."
                ),
                "signal_count": int(row.get("independent_document_count") or 0),
                "has_order_book_signals": row.get("earnings_capture_status") in {
                    "INDICATED", "CONFIRMED",
                },
                "capex_signals": int(row.get("pipeline_evidence_count") or 0),
                "physical_evidence_count": int(row.get("physical_evidence_count") or 0),
                "pipeline_evidence_count": int(row.get("pipeline_evidence_count") or 0),
                "earnings_capture_count": int(row.get("earnings_capture_count") or 0),
                "import_substitution_play": False,
                "as_of_date": row.get("as_of_date"),
                # True for an approved operating packet, OR when capability
                # data independently confirms the ticker makes this product.
                # False when the product IS capability-covered and this ticker
                # is NOT among its confirmed makers (a real denial, not an
                # absence of evidence). None only when capability has nothing
                # to say about the product at all — that pass-through is what
                # protects a genuine early pipeline lead from being erased.
                "corroborated": upstream_corroborated,
                "company_candidate_state": row.get("selection_state"),
                "earnings_capture_status": row.get("earnings_capture_status"),
                "evidence_source": "constraint_company_candidate_pipeline",
            })

    # Lesson 2: batch-fetch classification for all tickers. NSE macro sector when
    # present; BSE 4-level taxonomy (enriched Jul-2026 from BSE public API) as
    # fallback AND as the granular sub-industry string for pure-play detection.
    tickers = list({r["ticker"] for r in rows if r.get("ticker")})
    if tickers and country == "IN":
        sm = q(cur, """SELECT nse_symbol, industry_nse, industry_bse, sector_bse
                       FROM security_master WHERE nse_symbol = ANY(%s)""", (tickers,))
        industry_map = {r["nse_symbol"]: r for r in sm}
    else:
        industry_map = {}

    by_product = defaultdict(list)
    for r in rows:
        chain = r["constrained_product"] or r["theme_name"]
        smrow = industry_map.get(r.get("ticker") or "") or {}
        industry = smrow.get("industry_nse") or smrow.get("industry_bse")
        r["industry_nse"] = smrow.get("industry_nse")
        r["industry_detail"] = smrow.get("industry_bse")   # e.g. "Electrical Equipment | Heavy Electrical Equipment"
        r["sector"] = smrow.get("sector_bse")

        # Lesson 2: sector allowlist check — combined NSE+BSE text for coverage
        combined = " / ".join(x for x in (smrow.get("industry_nse"), smrow.get("industry_bse")) if x)
        compatible = _sector_check(combined or None, chain)
        r["sector_mismatch"] = compatible is False  # True → known-incompatible sector

        # Capex-expansion evidence: the mapper embeds it in rationale text
        m = re.search(r"(\d+)\s+capex_increase signals", r.get("rationale") or "")
        r["capex_signals"] = max(
            int(r.get("capex_signals") or 0), int(m.group(1)) if m else 0
        )

        # Lesson 3: demand-side consumer node check
        node = (r.get("supply_chain_node") or "").lower().strip()
        r["demand_side_flag"] = node in DEMAND_CONSUMER_NODES

        # Corroboration (Aug-2026): the beneficiary mapper scores conviction on
        # signal VOLUME alone, so it maps banks/staffing/cement to constraints
        # at high conviction. `corroborated` records whether the company's own
        # filings independently evidence making the product (via the capability
        # mapper). Validated: keeping corroborated + not-judgeable lifted the
        # basket 56%->77% at 3y while dropped names returned 55%. TRUE=confirmed
        # maker, FALSE=co-occurrence junk, NULL=product not coverable so can't
        # judge (kept). Surfaced, not silently dropped — the judgment layer
        # de-weights uncorroborated names.
        r["corroborated"] = r.get("corroborated")
        r["corroboration_flag"] = (
            "confirmed maker" if r.get("corroborated") is True else
            "UNCORROBORATED — mapped on signal co-occurrence only, own filings "
            "show no manufacture of this product" if r.get("corroborated") is False
            else "not assessable (product outside capability-mapper coverage)")

        by_product[chain].append(r)

    result = {}
    for prod, lst in by_product.items():
        # One ticker may arrive through both the legacy mapper and the new
        # issuer-role pipeline. Preserve the legacy economic-role row while
        # attaching the stronger literal-role audit fields from upstream.
        deduped = {}
        for row in lst:
            ticker = (row.get("ticker") or "").strip().upper()
            key = ticker or (row.get("company") or "").strip().casefold()
            previous = deduped.get(key)
            if previous is None:
                deduped[key] = row
                continue
            upstream = row if row.get("evidence_source") == "constraint_company_candidate_pipeline" else previous \
                if previous.get("evidence_source") == "constraint_company_candidate_pipeline" else None
            mapper = previous if upstream is row else row if upstream is previous else None
            if upstream and mapper:
                mapper["company_candidate_state"] = upstream.get("company_candidate_state")
                mapper["earnings_capture_status"] = upstream.get("earnings_capture_status")
                mapper["physical_evidence_count"] = upstream.get("physical_evidence_count")
                mapper["pipeline_evidence_count"] = upstream.get("pipeline_evidence_count")
                mapper["earnings_capture_count"] = upstream.get("earnings_capture_count")
                mapper["literal_role_evidence_source"] = upstream.get("evidence_source")
                mapper["corroborated"] = bool(mapper.get("corroborated") or upstream.get("corroborated"))
                mapper["signal_count"] = max(
                    int(mapper.get("signal_count") or 0),
                    int(upstream.get("signal_count") or 0),
                )
                deduped[key] = mapper
            elif float(row.get("conviction_score") or 0) > float(previous.get("conviction_score") or 0):
                deduped[key] = row
        lst = list(deduped.values())
        lst.sort(key=lambda r: (
            r["sector_mismatch"],           # mismatches sorted to bottom of per-chain list
            r["demand_side_flag"],          # demand-side consumers next
            -(r["conviction_score"] or 0),
            not r["has_order_book_signals"],
        ))
        result[prod] = lst[:per_product] if per_product else lst
    return result


def fetch_role_ledger_products(cur, as_of, existing_products, country="IN"):
    """Bridge the verified-operating-maker ledger into the DECISION spine.

    Root fix (Aug-2026, user: "Investment List / As-of Decision not reflected
    with new stocks; cross-check other sections for the full universe").

    The universe expansion added pharma/specialty-chemical/cement/wind/API
    makers to ``mg_company_product_roles`` (and to the Investable-Universe
    display section), but the decision pipeline still built its constraint
    universe ONLY from the legacy beneficiary/capacity/import mapper — the old
    ~15 solar/electronics products. So the new makers were invisible to every
    decision section (Investment List, As-of Decision, ranked candidates,
    greatest constraints), or were mis-bucketed as pure co-occurrence noise
    (a cement maker showing under "Solar Cell", a pharma maker under "Defense
    electronics").

    This reshapes the SAME verified universe the Investable-Universe section
    uses into two decision-layer inputs — constraint-universe rows and
    candidate-supply rows — keyed on the ledger's canonical
    ``normalized_product`` so the constraint, its makers and its supply rows
    all match cleanly through ``rank_greatest_constraints`` / ``rank_candidates``
    (the alias/ledger labels do not normalize consistently — "specialty
    chemicals" vs "specialty chemical", "integrated cement" vs "cement" — so
    keying on the one canonical field is the robust join).

    Deliberate boundaries, so this widens recall WITHOUT inventing conviction:
    * ``any_import_sub=True`` but NO fabricated capacity/import magnitude and NO
      order-book flag — a constraint with no measured India binding leg stays
      research-grade. ``build_final_selection``'s A/B physical-quality gate is
      untouched, so a maker here reaches position authority ONLY once a real
      measured constraint magnitude exists. Emergence/binding signal (already
      blended by the ranker) decides which surface as "about to bind"; a
      non-scarce product (e.g. domestically self-sufficient cement) simply
      ranks low and truncates out. No hardcoded include/exclude list.
    """
    _EV = ("OPERATING_PRODUCER_EVIDENCED", "EARNINGS_CAPTURE_EVIDENCED",
           "EXACT_ROLE_EVIDENCED")
    rows = q(cur, """
        SELECT r.normalized_product,
               UPPER(TRIM(r.ticker))                       AS ticker,
               COALESCE(NULLIF(s.company_name, ''), r.company, r.ticker) AS company,
               r.adjudication_state, r.adjudication_reason, r.last_evidence_date,
               r.independent_document_count, r.physical_evidence_count,
               r.pipeline_evidence_count, r.earnings_capture_count, r.evidence
        FROM mg_company_product_roles r
        LEFT JOIN security_master s ON UPPER(TRIM(s.nse_symbol)) = UPPER(TRIM(r.ticker))
        WHERE r.country=%s AND r.as_of_date=%s
          AND r.adjudication_state = ANY(%s) AND TRIM(r.ticker) <> ''
    """, (country, as_of, list(_EV)))
    if not rows:
        return [], [], {}, {}

    # Canonicalize onto the emergence-taxonomy head (folds the label fragments
    # the India-text garble and the mapper both emit: "active pharmaceutical" /
    # "bulk drug" / "Pharma APIs" are ONE constraint, "integrated cement" /
    # "white cement" / "Cement" are ONE). Without this a 1-name "bulk drug"
    # bucket competes against — and can outrank — the 12-name pharma set.
    by_canon: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_canon[_emergence_canon(r["normalized_product"])].append(r)

    # Prefer the CLEAN display label for each canonical head: (1) the existing
    # legacy-mapper constraint spelling if one shares this canon (so verified
    # makers merge into that bucket by shared supply key, and the corroboration
    # gate cleans its co-occurrence names); else (2) the curated emergence-
    # taxonomy label ("Pharma APIs", "Cement", "Specialty Chemicals", "Wind
    # Turbine", "Optical Fibre") — which also matches the mapper spelling for
    # shared constraints, so the merge still happens; else (3) the cleanest
    # fragment. This is what collapses the "bulk drug" / "highly potent api" /
    # "active pharmaceutical" fragments into one ranked row.
    existing_by_canon: dict[str, str] = {}
    for p in existing_products:
        lbl = (p.get("constrained_product") or "").strip()
        if lbl:
            existing_by_canon.setdefault(_emergence_canon(lbl), lbl)
    taxonomy_by_canon: dict[str, str] = {}
    try:
        for tr in q(cur, "SELECT DISTINCT constraint_label FROM "
                    "mg_constraint_signal_activity WHERE country=%s", (country,)):
            taxonomy_by_canon.setdefault(_emergence_canon(tr["constraint_label"]),
                                         tr["constraint_label"])
    except Exception:
        pass

    product_rows: list[dict] = []
    supply_by_product: dict[str, list[dict]] = {}
    makers_by_label: dict[str, dict] = {}
    labels: list[str] = []
    _rank = {"OPERATING_PRODUCER_EVIDENCED": 0, "EARNINGS_CAPTURE_EVIDENCED": 1,
             "EXACT_ROLE_EVIDENCED": 2}
    for canon, mk in sorted(by_canon.items(), key=lambda kv: kv[0]):
        # Merge duplicate ticker rows across the fragment labels in this group.
        best_by_ticker: dict[str, dict] = {}
        for r in mk:
            t = r["ticker"]
            if t not in best_by_ticker or _rank.get(r["adjudication_state"], 3) < \
                    _rank.get(best_by_ticker[t]["adjudication_state"], 3):
                best_by_ticker[t] = r
        mk = list(best_by_ticker.values())
        existing_label = existing_by_canon.get(canon)
        label = (existing_label or taxonomy_by_canon.get(canon)
                 or min((r["normalized_product"] for r in mk),
                        key=lambda s: (any(ch.isdigit() for ch in s), len(s), s)))
        supply_by_product.setdefault(label, []).extend(
            _rl_supply_row(m, label, as_of) for m in mk)
        # Maker bucket keyed by the DISPLAY label so verified_current_makers
        # attaches even when the constraint spelling ("Pharma APIs") differs
        # from the ledger normalized_product ("active pharmaceutical") — the
        # label mismatch that merge_exact_role_makers cannot bridge and that
        # left every bridged constraint with 0 verified makers (so no company
        # could ever clear the As-of Decision).
        makers_by_label.setdefault(
            label, {"snapshot": as_of, "modal_industry": None, "makers": [],
                    "role_candidates": [], "rejected_candidates": [],
                    "coverage": {}})["makers"].extend(
            _rl_maker(m) for m in mk)
        if existing_label:
            # Already a registered mapper constraint — makers merged into its
            # supply above; do not emit a duplicate constraint row.
            continue
        labels.append(label)
        last_ev = max((m["last_evidence_date"] for m in mk
                       if m["last_evidence_date"]), default=None)
        product_rows.append({
            "constrained_product": label, "theme_name": None,
            "n_companies": len({m["ticker"] for m in mk}),
            "avg_conviction": None, "max_conviction": None,
            "any_order_book": False, "any_import_sub": True,
            "first_mapped": None, "last_mapped": last_ev,
            "detection_origin": "ROLE_LEDGER_VERIFIED",
        })
    return labels, product_rows, supply_by_product, makers_by_label


def _merge_role_ledger_maker_buckets(constraint_makers: dict, rl_makers: dict) -> dict:
    """Merge the bridge's role-ledger maker buckets into constraint_makers,
    keyed by display label. New label -> whole bucket; existing label -> append
    makers the bucket does not already list (dedup by ticker), so a bridged
    verified maker attaches to a legacy mapper constraint too."""
    for label, bucket in (rl_makers or {}).items():
        existing = constraint_makers.get(label)
        if not existing:
            constraint_makers[label] = bucket
            continue
        have = {(x.get("ticker") or "").upper()
                for x in existing.get("makers") or []}
        existing.setdefault("makers", []).extend(
            m for m in bucket.get("makers") or []
            if (m.get("ticker") or "").upper() not in have)
        if existing.get("snapshot") is None:
            existing["snapshot"] = bucket.get("snapshot")
    return constraint_makers


def _rl_maker(m: dict) -> dict:
    """Shape a role-ledger row as a capability_makers-style maker dict so
    rank_greatest_constraints can classify it into verified_current_makers.
    Mirrors the ``promoted`` shape merge_exact_role_makers builds."""
    adj = m["adjudication_state"]
    commercial = adj == "EARNINGS_CAPTURE_EVIDENCED"
    maker_status = (
        "direct product supplier — repeated commercial capture" if commercial else
        "operating manufacturer — independently corroborated")
    return {
        "ticker": m["ticker"], "company": m["company"], "industry": "",
        "n_docs": int(m.get("independent_document_count") or 0),
        "mfg_docs": int(m.get("physical_evidence_count") or 0),
        "direct_evidence_count": int(m.get("independent_document_count") or 0),
        "strict_evidence_count": int(m.get("physical_evidence_count") or 0),
        "last_evidence_date": (m["last_evidence_date"].isoformat()
                               if m.get("last_evidence_date") else None),
        "evidence_samples": m.get("evidence") or [],
        "maker_status": maker_status,
        "research_route": (
            "Verify owned production versus outsourced supply, then underwrite earnings capture"
            if commercial else "Validate same-product earnings capture and underwriting"),
        "needs_review": False, "review_reason": None, "industry_mismatch": False,
        "entity_scope": "company filing",
        "source": "own-filings company-product-role ledger (automatic exact-role adjudication)",
        # EXACT_ROLE_EVIDENCED is not a promoted producer state; map it to
        # OPERATING so an exact-role verified maker is not silently quarantined.
        "adjudication_state": ("OPERATING_PRODUCER_EVIDENCED"
                               if adj == "EXACT_ROLE_EVIDENCED" else adj),
        "adjudication_reason": m.get("adjudication_reason"),
        "missing_evidence": [],
        "pipeline_evidence_count": int(m.get("pipeline_evidence_count") or 0),
        "earnings_capture_count": int(m.get("earnings_capture_count") or 0),
    }


def _rl_supply_row(m: dict, label: str, as_of) -> dict:
    """One candidate-supply row for a role-ledger verified operating maker."""
    return {
        "company": m["company"], "ticker": m["ticker"],
        "theme_name": label, "constrained_product": label,
        "supply_chain_node": "operating maker (role ledger)",
        "supply_chain_stage": 0,
        "beneficiary_type": "direct_supplier",
        "conviction_score": 0.7,
        "rationale": (
            f"Verified operating maker of {label} ({m['adjudication_state']}); "
            f"{int(m['physical_evidence_count'] or 0)} dated physical-evidence "
            "passages in own filings."),
        "signal_count": int(m["independent_document_count"] or 0),
        "has_order_book_signals": False,
        "import_substitution_play": True,
        "as_of_date": (m["last_evidence_date"] or as_of),
        "corroborated": True,
        "corroborated_products": [label],
    }


# ──────────────────────────────────────────────────────────────────────────
# Improvements 1-5: evidence dashboard, cross-theme overlap, scoring
# breakdown, visual analytics data, bear-case analysis
# ──────────────────────────────────────────────────────────────────────────

def fetch_policy_event_counts(cur, themes, as_of, country):
    """Single DB query → Python-side count of policy events per theme keyword."""
    rows = q(cur, """
        SELECT lower(title) AS title FROM mg_policy_events
        WHERE country=%s AND COALESCE(introduced_date, enacted_date, fetched_at::date) <= %s
    """, (country, as_of))
    titles = [r["title"] for r in rows]
    counts = {}
    for t in themes:
        raw = t["theme_name"].split(":")[0].split("←")[0].strip().lower()
        words = [w for w in raw.split() if len(w) >= 4][:2]
        counts[t["theme_id"]] = sum(1 for title in titles if any(w in title for w in words))
    return counts


def _evidence_trend(t):
    """Is evidence for this theme accelerating, maturing, flat, or shrinking?

    Uses beneficiary GROWTH RATE (new/before) as the primary signal — more
    meaningful than the absolute strength delta, which is large for all themes.
    """
    new_cos = t.get("new_beneficiaries_in_window", 0)
    before = max(1, t.get("beneficiaries_before_window", 1))
    growth_ratio = new_cos / before
    stage = t.get("stage_label", "")
    delta = t.get("strength_delta_6mo")

    # Genuine new themes (born recently) with strong inflow
    if before == 1 and new_cos >= 10:
        return "↑ Accelerating"
    # Established themes with meaningful new additions (>20% growth)
    if growth_ratio > 0.20:
        return "↑ Accelerating" if stage == "Accelerating" else "↑ Growing"
    # Slowing crowd adoption on mature themes (2-20% new)
    if growth_ratio > 0.02:
        return "→ Maturing"
    # Fully saturated or slightly shrinking
    if delta is not None and delta < -5:
        return "↓ Fading"
    return "→ Saturated"


def build_evidence_dashboard(themes, policy_counts):
    """Theme-signal evidence record; never a substitute for constraint proof.

    The old formula saturated at 100 whenever an established theme had many
    NLP bottleneck mentions and quarters.  It made a mature narrative look as
    complete as a measured physical shortage.  Source diversity now matters:
    filings, repeated confirmation, mapped breadth, fresh additions, and an
    independent policy source each contribute separately.  The constraint card
    remains the only physical-investability authority.
    """
    out = []
    for t in themes:
        sc = t.get("supply_constraint_count") or 0
        confirmed = t.get("confirmed_quarters") or 0
        filings = t.get("doc_count") or 0
        companies = t.get("company_count") or 0
        fresh = t.get("new_beneficiaries_in_window") or 0
        policies = policy_counts.get(t["theme_id"], 0)
        confidence = round(
            (min(sc, 100) / 100.0) * 25
            + (min(filings, 100) / 100.0) * 20
            + (min(confirmed, 12) / 12.0) * 20
            + (min(companies, 30) / 30.0) * 15
            + (min(fresh, 12) / 12.0) * 10
            + (min(policies, 3) / 3.0) * 10
        )
        if policies == 0:
            confidence = min(confidence, 80)
        if sc == 0:
            confidence = min(confidence, 60)
        out.append({
            "theme_name": t["theme_name"],
            "theme_id": t["theme_id"],
            "stage": t["stage_label"],
            "first_detected": t["first_detected"],
            "companies_mapped": t.get("company_count") or 0,
            "filings_covered": t.get("doc_count") or 0,
            "bottleneck_signals": sc,
            "confirmed_quarters": confirmed,
            "policy_events": policies,
            "strength_score": t.get("strength_now") or 0,
            "strength_delta_6mo": t.get("strength_delta_6mo"),
            "new_beneficiaries_in_window": t.get("new_beneficiaries_in_window", 0),
            "conviction_label": t.get("conviction") or "—",
            "evidence_confidence_pct": confidence,
            "confidence_scope": "theme-signal/source diversity — not physical constraint proof",
            "evidence_trend": _evidence_trend(t),          # NEW
        })
    return sorted(out, key=lambda x: -x["evidence_confidence_pct"])


_SS_TYPE_SCORE = {
    "direct_supplier": 40, "critical_supplier": 35,
    "input_supplier": 25, "ecosystem_participant": 10,
}


def _supply_side_confidence(best_rank, capex_signals, n_chains, max_conv_or_rel,
                             order_book=False, best_type_score=None):
    """Supply-side confidence 0-100: how likely is this company actually a capacity owner?

    Components:
      Rank quality   (0-40): rank 1 in any chain = graph found it as top supply-side match
      Capex signals  (0-15): company investing to expand capacity = supply-side behaviour
      Chain depth    (0-20): independent chains finding the same name = corroboration
      Conviction/rel (0-15): filing relevance weight
      Order-book     (0-10): hard order-book evidence (IN only)
    """
    rank_pts = 40 if best_rank <= 1 else 30 if best_rank <= 3 else 15 if best_rank <= 5 else 5
    capex_pts = min(15, int(capex_signals or 0) * 4)
    chain_pts = min(20, int(n_chains or 1) * 3)
    conv_pts = min(15, round(float(max_conv_or_rel or 0) / 100.0 * 15))
    ob_pts = 10 if order_book else 0
    type_pts = min(best_type_score or 0, 0)   # bonus only for IN (passed through)
    return min(100, rank_pts + capex_pts + chain_pts + conv_pts + ob_pts)


# Stocks excluded regardless of score (enforcement actions, fraud).
# Loaded from mg_manual_exclusions at scan start — {ticker: reason}. The
# judgment layer adds/removes rows with SQL, never code edits.
MANUAL_EXCLUDE: dict[str, str] = {}


def compute_cross_theme_overlap(supply_by_chain):
    """Improvement 2 — companies appearing across 2+ independent constraint chains,
    with a supply-side confidence score on each."""
    ticker_chains = defaultdict(list)
    ticker_meta = {}   # ticker → best capex/rank across chains
    for chain_name, companies in supply_by_chain.items():
        for c in companies:
            tick = (c.get("ticker") or "").strip().upper()
            if not tick or tick in MANUAL_EXCLUDE:
                continue
            conv = float(c.get("conviction_score") or c.get("relevance_score") or 0)
            rank = c.get("rank_in_theme") or 99
            ticker_chains[tick].append({
                "chain": chain_name,
                "rank": rank,
                "conviction": conv,
            })
            m = ticker_meta.setdefault(tick, {"best_rank": 999, "max_conv": 0,
                                               "total_capex": 0, "order_book": False,
                                               "best_type_score": 0})
            m["best_rank"] = min(m["best_rank"], rank)
            m["max_conv"] = max(m["max_conv"], conv)
            m["total_capex"] += int(c.get("capex_signals") or 0)
            m["order_book"] = m["order_book"] or bool(c.get("has_order_book_signals"))
            ts = _SS_TYPE_SCORE.get(c.get("beneficiary_type") or "", 0)
            m["best_type_score"] = max(m["best_type_score"], ts)

    out = []
    for ticker, chains in ticker_chains.items():
        if len(chains) < 2:
            continue
        m = ticker_meta[ticker]
        n = len(chains)
        ss_conf = _supply_side_confidence(m["best_rank"], m["total_capex"], n,
                                           m["max_conv"], m["order_book"])
        overlap_score = round(n * m["max_conv"], 3)
        out.append({
            "ticker": ticker,
            "n_independent_chains": n,
            "overlap_score": overlap_score,
            "best_rank_across_chains": m["best_rank"],
            "supply_side_confidence_pct": ss_conf,   # NEW
            "chains": sorted(chains, key=lambda x: (x.get("rank") or 99, -x["conviction"])),
            "read": (
                "multi-chain conviction" if n >= 4 else
                "cross-chain corroboration"
            ),
        })
    return sorted(out, key=lambda x: (-x["n_independent_chains"], -x["overlap_score"]))[:25]


def generate_bear_cases(major_themes, emerging_themes, candidates, country):
    """Improvement 5 — heuristic bear-case flags per major theme and top candidates."""
    bears = []
    all_themes = major_themes[:8] + [t for t in emerging_themes if t not in major_themes][:4]
    for t in all_themes:
        risks = []
        stage = t.get("stage_label", "")
        confirmed = t.get("confirmed_quarters") or 0
        sc = t.get("supply_constraint_count") or 0
        cos = t.get("company_count") or 0
        delta = t.get("strength_delta_6mo")
        name_lower = t["theme_name"].lower()
        born = t.get("first_detected")

        if stage == "Consensus" and confirmed >= 8:
            risks.append(f"Fully priced: {confirmed} confirmed quarters at Consensus — marginal buyers shrinking, re-rating requires new catalyst")
        if sc < 5 and cos > 20:
            risks.append(f"Breadth without depth: only {sc} hard bottleneck signals across {cos} companies — could be narrative consensus, not physical shortage")
        if delta is not None and -1.0 < delta < 1.0:
            risks.append("Strength score flat for 6 months — theme may have peaked; watch for a re-ignition catalyst before adding")
        if born and born.year == 2024 and stage == "Consensus":
            risks.append("Born-to-crowded in <24 months — the investing window compressed; late entrants face full-priced assets")
        if "energy" in name_lower or "power" in name_lower or "utility" in name_lower or "grid" in name_lower:
            risks.append("Rate sensitivity: utilities are bond proxies — if long rates stay elevated, multiple compression offsets load-growth thesis")
            risks.append("AI efficiency risk: if model training/inference efficiency improves faster than expected (Jevons paradox), power-demand projections overstated")
            risks.append("Interconnection queue: new grid capacity can take 7-12 years to energize; near-term EPS may disappoint vs long thesis")
        if "chip" in name_lower or "semiconductor" in name_lower or "silicon" in name_lower:
            risks.append("Export-control two-sided: China-revenue loss (AMAT/LRCX) can outweigh domestic-capacity benefit in the near term")
            risks.append("AI-capex reversal: if hyperscaler ROI on AI disappoints in 2026, the chip pull-forward cycle reverses sharply")
        if "memory" in name_lower or "nand" in name_lower or "dram" in name_lower or "hbm" in name_lower:
            risks.append("ITC NAND/DRAM investigation (30-Mar-2026): respondent outcome could create uncertainty for domestic memory names")
            risks.append("Memory is cyclical: HBM pricing premium can compress faster than the theme cycle suggests")
        if ("generative" in name_lower or "genai" in name_lower) and stage == "Consensus":
            risks.append("GenAI monetisation risk: if enterprise ROI on GenAI tools disappoints, the demand signal that created this theme deflates")
        if not risks:
            risks.append("No dominant risk pattern detected from available heuristics — apply sector-specific diligence")
        bears.append({
            "theme": t["theme_name"],
            "stage": stage,
            "risk_count": len(risks),
            "risks": risks,
        })
    return bears


def _scoring_breakdown_in(conviction, breadth, order_book, import_sub,
                          freshness_multiplier=1.00, freshness_label="established",
                          technical_flag=None):
    """Transparent IN scoring formula.

    Lesson 1+5: composite = fundamental × freshness_multiplier (theme age discount).
    Fresh themes (1-4 quarters) get ×1.12; Consensus themes (16+) get ×0.93.
    Lesson 2: 200DMA is position-sizing guidance only — NOT a composite input.
              technical_flag is stored for display; it does not move the score.
    Ranked by composite_score (freshness-adjusted); fundamental is tiebreaker.
    """
    components = {
        "conviction_x0.45": round(0.45 * conviction, 3),
        "breadth_x0.25":    round(0.25 * breadth, 3),
        "order_book_x0.20": 0.20 if order_book else 0.0,
        "import_sub_x0.10": 0.10 if import_sub else 0.0,
    }
    fundamental = round(sum(components.values()), 3)
    components["fundamental_score"] = fundamental
    components["freshness_multiplier"] = freshness_multiplier
    components["freshness_label"] = freshness_label
    components["composite_score"] = round(fundamental * freshness_multiplier, 3)
    if technical_flag:
        components["technical_flag"] = technical_flag
    components["formula"] = (
        "fundamental = 0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub; "
        "composite = fundamental × freshness_multiplier "
        "(new_theme=×1.10 | fresh 1-4q=×1.12 | developing 5-8q=×1.05 | established 9-15q=×1.00 | consensus 16+q=×0.93). "
        "Technical state (200DMA) = position-sizing guidance only — not a composite input. "
        "Ranked by composite_score; fundamental_score is tiebreaker."
    )
    return components


def _scoring_breakdown_us(relevance, breadth, rank_score):
    """Improvement 3 — transparent US scoring formula."""
    components = {
        "relevance_x0.40": round(0.4 * min(relevance / 100.0, 1.0), 3),
        "theme_breadth_x0.30": round(0.3 * breadth, 3),
        "theme_rank_x0.30": round(0.3 * rank_score, 3),
    }
    components["composite_score"] = round(sum(components.values()), 3)
    components["formula"] = "0.40×(relevance/100) + 0.30×(n_themes/5) + 0.30×(1 - best_rank/30)"
    return components


def fetch_us_beneficiaries(cur, constraint_themes, as_of, win_start, per_theme=8):
    """US supply-side proxies: top-ranked beneficiaries of bottleneck/constraint
    themes from the theme graph (no supply-chain-node classification for US)."""
    result = {}
    for t in constraint_themes:
        rows = q(cur, """
            SELECT b.ticker, b.company_name AS company, b.beneficiary_type,
                   b.company_role, b.relevance_score, b.rank_in_theme,
                   b.signal_count, b.capex_signals, b.first_seen_at,
                   left(b.reasoning, 200) AS rationale
            FROM mg_theme_beneficiaries b
            WHERE b.theme_id=%s AND b.ticker IS NOT NULL
              AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
            ORDER BY b.rank_in_theme ASC NULLS LAST, b.relevance_score DESC
            LIMIT %s
        """, (t["theme_id"], as_of, per_theme))
        if rows:
            result[t["theme_name"]] = rows
    return result


def rank_us_candidates(themes_by_name, top_n=25):
    agg = {}
    for theme, lst in themes_by_name.items():
        for r in lst:
            tick = (r["ticker"] or "").strip().upper()
            if not tick:
                continue
            a = agg.setdefault(tick, {"ticker": tick, "company": r["company"],
                                      "themes": set(), "best_rank": 999,
                                      "max_relevance": 0.0, "capex_signals": 0,
                                      "signal_count": 0})
            a["themes"].add(theme)
            a["best_rank"] = min(a["best_rank"], r["rank_in_theme"] or 999)
            a["max_relevance"] = max(a["max_relevance"], float(r["relevance_score"] or 0))
            a["capex_signals"] += int(r["capex_signals"] or 0)
            a["signal_count"] += int(r["signal_count"] or 0)
    out = list(agg.values())
    for a in out:
        a["themes"] = sorted(a["themes"])[:6]
        breadth = min(len(a["themes"]), 5) / 5.0
        rank_score = max(0.0, 1.0 - (a["best_rank"] / 30.0)) if a["best_rank"] < 999 else 0
        breakdown = _scoring_breakdown_us(a["max_relevance"], breadth, rank_score)
        a["fundamental_score"] = breakdown["composite_score"]
        a["composite_score"] = breakdown["composite_score"]
        a["scoring_breakdown"] = breakdown
        # Supply-side confidence: how likely is this a genuine capacity owner?
        a["supply_side_confidence_pct"] = _supply_side_confidence(
            a["best_rank"], a["capex_signals"], len(a["themes"]),
            a["max_relevance"])
        a["technical"] = None
    out.sort(key=lambda a: -a["composite_score"])
    return out[:top_n]


# ──────────────────────────────────────────────────────────────────────────
# Candidate ranking + technical overlay
# ──────────────────────────────────────────────────────────────────────────

def _clean_maker_set(constraint_makers):
    """Tickers the capability mapper confirms as CLEAN makers of at least one
    constraint — industry coherent with that product's real producers, not an
    industry-mismatch (which flags utilities/IT/consumers that merely touch the
    product). Validated: clean makers +64.6% median 3y vs +21% for flagged."""
    clean = set()
    for info in (constraint_makers or {}).values():
        for m in (info or {}).get("makers", []) or []:
            if not m.get("needs_review"):
                clean.add((m.get("ticker") or "").strip().upper())
    return clean


def _build_constraint_signals(products, major, emerging, as_of,
                              doc_trade=None, doc_reg=None):
    """Per-constraint point-in-time signal bundle for explosiveness ranking.
    Everything here is knowable at `as_of` — theme stage/emergence, order-book
    pressure, mapping recency, plus the leading-indicator feeds (trade-flow
    import momentum, draft-policy catalysts) which are empty on historical
    anchors and populate on live runs."""
    # Theme names use two incompatible conventions — constrained_products say
    # "semiconductor: Demand-Supply Tension", the theme tables say
    # "defense electronics: Constraint from Healthcare Demand" — but BOTH carry
    # a pre-colon SUBJECT. Key the stage map on that subject.
    _RANK = {"emerging": 4, "hidden formation": 4, "accelerating": 3,
             "developing": 2, "established": 1, "consensus": 0}

    def _subject(name):
        return (name or "").split(":")[0].strip().lower()

    # POINT-IN-TIME maturity — the fix for "a constraint shown once persists,
    # frozen, in every later year". The stored stage_label never matures (a
    # 6-year-old theme sat at "Accelerating", cq=24), and the old
    # most-emerging-variant rule made it worse: NLP keeps spinning up fresh
    # variants ("Defense Critical Shortage", base 0) whose zero prior base reads
    # as brand-new, re-crowning a decade-old constraint "Emerging" annually.
    # Instead derive the stage from the constraint's TRUE age (as_of minus the
    # EARLIEST first_detected across all its variants) and its point-in-time
    # beneficiary growth (accumulated base vs new-in-window, both <= as_of). A
    # constraint then matures monotonically Emerging -> Accelerating -> Consensus
    # as it ages and its growth decelerates against a broadening base — so a
    # mature theme scores as Consensus (market already knows) and stops
    # out-ranking genuinely new constraints year after year.
    def _asdate(v):
        if not v:
            return None
        try:
            return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
        except Exception:
            return None

    subj_dyn = {}
    for t in list(major or []) + list(emerging or []):
        subj = _subject(t.get("theme_name"))
        if not subj:
            continue
        a = subj_dyn.setdefault(subj, {"fd": None, "base": 0, "new": 0})
        fd = _asdate(t.get("first_detected"))
        if fd and (a["fd"] is None or fd < a["fd"]):
            a["fd"] = fd
        a["base"] += int(t.get("beneficiaries_before_window") or 0)
        a["new"] += int(t.get("new_beneficiaries_in_window") or 0)

    def _pit_stage(a):
        # AGE + growth only. strength_delta_6mo was tried and dropped: the
        # snapshots are usually flat and taking the most-negative variant's
        # delta wrongly matured whole subjects (one declining "Severe
        # Constraint" variant dragged still-expanding Solar to Consensus). The
        # accumulated-base-vs-new-in-window growth ratio is the robust signal.
        fd = a["fd"]
        age_q = (as_of - fd).days / 91.0 if fd else None
        base, new = a["base"], a["new"]
        ratio = new / max(base, 1)
        # genuinely new: recent first appearance AND still a small footprint
        if age_q is not None and age_q <= 5 and base < 30:
            return "Emerging"
        # old AND growth has decelerated against a broad base — the market has
        # crowded in. >=3 years and <18% new-vs-base growth: matures the multi-
        # year giants (Defense/Battery) to Consensus so they stop out-ranking
        # newer constraints, while a mature-but-still-expanding one (Solar, with
        # heavy ongoing capex) stays Accelerating/investable.
        if age_q is not None and age_q >= 12 and ratio < 0.18:
            return "Consensus"
        return "Accelerating"           # established and still growing

    stage_by_subject = {s: _pit_stage(a) for s, a in subj_dyn.items()}
    emerging_subjects = {s for s, st in stage_by_subject.items() if st == "Emerging"}

    # Fallback link: a constrained_product often carries only a descriptive
    # theme_name ("Solar Cell Manufacturing Gap") whose whole phrase is its
    # subject, so it never equals a landscape theme subject ("solar") even
    # though that theme is live and Accelerating. Exact-subject matching then
    # silently drops the emergence signal — the single most important
    # 'about to explode' input — for a whole class of real constraints
    # (solar cell, solar pump). Recover it by DISTINCTIVE-TOKEN overlap: link a
    # constraint to any landscape theme sharing a product-identifying word
    # (solar/steel/battery/defense), taking the most-emerging matched stage.
    # Generic/qualifier words are stripped so the overlap is on the product,
    # never on "shortage"/"gap"/"cell". Additive only — used when the exact
    # subject match returns nothing, so a working match is never overridden.
    _STAGE_STOP = {"cell", "cable", "gap", "shortage", "critical", "severe",
                   "constraint", "manufacturing", "localization", "opportunity",
                   "capacity", "deployment", "demand", "pull", "unclassified",
                   "research", "supply", "tension", "contract", "materials",
                   "other", "and", "the", "for", "ion", "active", "raw",
                   "component", "components", "product", "products", "system",
                   "systems", "equipment", "industry", "sector", "domestic"}

    def _dtok(text):
        return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower())
                if w not in _STAGE_STOP}

    subject_tokens = []            # [(distinctive tokens, stage)] over the landscape
    for subj, st in stage_by_subject.items():
        toks = _dtok(subj)
        if toks:
            subject_tokens.append((toks, st))

    def stage_for(theme_name, product=None):
        st = stage_by_subject.get(_subject(theme_name), "")
        if st:
            return st
        ptoks = _dtok(product) if product else set()
        if not ptoks:
            return ""
        best_rank, best = -1, ""
        for toks, stage in subject_tokens:
            if toks & ptoks:
                r = _RANK.get(stage.lower(), -1)
                if r > best_rank:
                    best_rank, best = r, stage
        # A loose product-token link is weaker evidence than a direct
        # theme_name->subject match, so cap it at Accelerating: the rarest
        # "Emerging" (leading-edge) tier must be earned by a direct theme link.
        # Without this, a broad-commodity token (a "steel" product) inherits
        # Emerging from a cross-demand-chain theme ("Steel: Constraint from
        # Solar Demand", whose emergence is really about solar), over-crowning
        # a commodity above a genuinely emerging constraint like Defense.
        if _RANK.get(best.lower(), -1) > _RANK["accelerating"]:
            return "Accelerating"
        return best

    # product -> widening import momentum %, from the trade-flow feed. The
    # component names ("Solar Wafers", "Defense Electronics (radar, avionics)")
    # differ from the constraint names ("Solar Wafer", "Defense electronics")
    # by plural/parenthetical/casing, so an exact match silently dropped the
    # signal — the import-momentum leading indicator (+2.5, a genuine
    # "about-to-explode" forward signal) NEVER fired despite real widening
    # trends in the data. Key on a normalised product key instead (same fix as
    # the corroboration join).
    _norm_pk = canonical_product_key
    trade_by_prod = {}
    for r in (doc_trade or []):
        if (r.get("trend") == "widening") and r.get("component"):
            trade_by_prod[_norm_pk(r["component"])] = r.get("momentum_pct") or 0
    # products with a draft-stage policy catalyst (coarse title match)
    reg_titles = " ".join((r.get("title") or "").lower() for r in (doc_reg or []))

    # AGGREGATE per product across ALL its constrained_products rows. The same
    # product appears under several theme_names ("Semiconductor IC" is both
    # "semiconductor: Demand-Supply Tension" AND "Semiconductor Critical
    # Shortage"); keeping the last row lost the themed one and killed the
    # emergence signal. Take the MOST-emerging stage and OR the binding flags.
    agg = {}
    for p in (products or []):
        prod = p.get("constrained_product")
        if not prod:
            continue
        stage_raw = stage_for(p.get("theme_name"), prod)
        a = agg.setdefault(prod, {"stage": "", "import_sub": False,
                                  "emerging_subj": False})
        if _RANK.get(stage_raw.lower(), -1) > _RANK.get(a["stage"].lower(), -1):
            a["stage"] = stage_raw
        # Structural import-dependence (a localization-opportunity play). Unlike
        # the order-book flag — which the mapper attaches to ~100% of every
        # constraint's beneficiaries, so it never discriminated — this is a real
        # per-product attribute: ~100% of Battery/CRGO/PCB/Semiconductor/Solar-
        # Cell beneficiaries carry it, 0% of Power-Transformer/Rolling-Stock/
        # Defense, cleanly separating import-replacement constraints from
        # domestically-satisfied ones.
        a["import_sub"] = a["import_sub"] or bool(p.get("any_import_sub"))
        if _subject(p.get("theme_name")) in emerging_subjects:
            a["emerging_subj"] = True

    out = {}
    for prod, a in agg.items():
        stage = a["stage"].lower()
        out[prod] = {
            "stage": a["stage"] or None,
            "emerging": "emerg" in stage or "hidden" in stage or a["emerging_subj"],
            "accelerating": "acceler" in stage,
            "consensus": "consensus" in stage,
            "import_substitution": a["import_sub"],
            "import_widening": trade_by_prod.get(_norm_pk(prod)),
            "policy_catalyst": bool(reg_titles and any(w in reg_titles for w in prod.lower().split()[:2])),
        }
    return out


def _clean_constraint_label(product):
    """Readable constraint name for the list. HS-taxonomy labels are legal
    definitions ("Sodium hydroxide (caustic soda); potassium hydroxide ...",
    "Metal-rolling mills and rolls therefor") — keep only the leading clause,
    strip trailing qualifiers, cap length. Curated names ("CRGO Steel",
    "Battery Cell (Li-ion)") pass through unchanged."""
    if not product:
        return product
    lab = re.split(r"[;:]", product)[0].strip()
    lab = re.sub(r"\s*\(.*?\)\s*", " ", lab).strip() if len(lab) > 26 else lab
    lab = re.sub(r"\b(therefor|thereof|n\.e\.c\.).*$", "", lab, flags=re.I).strip()
    return (lab[:44] + "…") if len(lab) > 46 else lab


def _dedup_key(label):
    """Normalised token key so different spellings of one product merge.
    Keyed off the FULL product string (not the shortened label, which can drop
    a distinguishing word), singularising even short tokens so "IC"/"ICs" and
    "Board"/"Boards" collapse. Cross-vocabulary synonyms that share no tokens
    ("PCB" vs "Printed Circuit Board") still need the reviewed alias table —
    this only merges spelling/plural variants, which is the common case."""
    base = re.sub(r"[^a-z0-9 ]+", " ", (label or "").lower())
    stop = {"the", "and", "of", "for", "in", "other", "therefor", "thereof", "nec"}
    toks = sorted({(w[:-1] if len(w) > 2 and w.endswith("s") else w)
                   for w in base.split() if w and w not in stop})
    return " ".join(toks)


def _explosiveness_score(product, sig, country="IN", rank_mode="emerging"):
    """Point-in-time 'about-to-explode' score for a constraint — built ONLY
    from signals available at the report date, never from realized returns.
    A 2022 report ranks on what was visible in 2022. The backtest told us WHICH
    of these signals predict (scarcity was the validated one); it is never
    itself a report input.

    Higher = earlier and more likely to bind. Components:
      EMERGENCE   an emerging/accelerating theme (not yet consensus) scores
                  high — 'about to explode' means the market hasn't crowded in.
      SCARCITY    fewer listed makers = higher (the one backtest-validated
                  constraint signal, +22.9pp narrow vs broad).
      IMPORT DEPENDENCE  the product is a structural import-substitution play
                  (localization opportunity) — the constraint IS the import.
      IMPORT MOMENTUM  a widening import-dependence trend (trade-flow leading
                  indicator) = dependence worsening now, pre-filings.
      POLICY CATALYST  a draft-stage regulatory item touching this product
                  (PIB leading indicator) = a binding rule 2-4 quarters out.

    Two former components were REMOVED as non-discriminating: the mapper's
    order-book flag and 'newly mapped in window' both fired on ~100% of every
    constraint, adding a uniform constant that never changed the ranking and
    made the 'why now' line misleadingly claim order-book evidence everywhere.
    The ranking now rests on emergence, scarcity (the backtest-validated
    signal), structural import dependence, and the live leading indicators.
    """
    s, why = 0.0, []
    n = sig.get("n_makers")
    if rank_mode == "biggest":
        # US "BIGGEST" view. US has no capability mapper, no import-substitution
        # flags, and its constraints are US firms' OWN products (many domestic
        # makers), so India's scarcity signal inverts — it buried the biggest US
        # constraints (integrated circuits, medical devices) beneath niche
        # single-issuer mentions. Rank by evidenced-issuer BREADTH: more
        # independent listed makers with a corroborated role = a bigger,
        # better-established constraint. Emerging still scores; Consensus is
        # NEUTRAL (a mature US industry is discovered, not "not great").
        if sig.get("emerging"):
            s += 3.0; why.append("emerging theme")
        elif sig.get("accelerating"):
            s += 2.0; why.append("accelerating")
        if n is not None:
            if n >= 12:
                s += 3.0; why.append(f"broad evidenced base — {n} listed makers")
            elif n >= 6:
                s += 2.0; why.append(f"{n} listed makers")
            elif n >= 3:
                s += 1.0; why.append(f"{n} listed makers")
            else:
                why.append(f"only {n} listed maker(s)")
        return round(s, 2), why
    # "emerging" view — the about-to-explode screen (India default; also the US
    # 'most emerging' companion view). Emergence leads, scarcity (few listed
    # makers = a concentrated, not-yet-crowded opportunity) is the validated
    # differentiator, plus the import leading indicators where present.
    if sig.get("emerging"):
        s += 3.0; why.append("emerging theme (not consensus)")
    elif sig.get("accelerating"):
        s += 2.0; why.append("accelerating")
    elif sig.get("consensus"):
        s -= 1.0; why.append("consensus (market already knows)")
    if n is not None:
        if n <= 4:
            s += 2.5; why.append(f"scarce — only {n} listed makers")
        elif n <= 10:
            s += 1.0; why.append(f"{n} listed makers")
        else:
            why.append(f"{n} makers (broad)")
    if sig.get("import_substitution"):
        s += 1.5; why.append("import-substitution play (localization opportunity)")
    if sig.get("import_widening"):
        s += 2.5; why.append(f"import dependence widening (+{sig['import_widening']:.0f}% trade momentum)")
    if sig.get("policy_catalyst"):
        s += 2.0; why.append("draft policy/mandate in flight")
    return round(s, 2), why


def compute_final_investment_list(candidates, constraint_signals=None,
                                  clean_makers=None, product_expected_industry=None,
                                  makers_per_constraint=6, top_constraints=8,
                                  country="IN", rank_mode="emerging"):
    """The decision-complete answer, CONSTRAINT-FIRST and FORWARD-LOOKING:
    the constraints most likely to explode NEXT, ranked by point-in-time
    signals only, each with the companies that make its product.

    Ranking companies globally let diversified names dominate; making the
    CONSTRAINT the unit fixes that. Ranking constraints by realized return
    was a point-in-time violation and answered the wrong question ('what
    already worked' vs 'what is about to'); ranking by _explosiveness_score
    fixes that. No forward return appears anywhere.
    """
    constraint_signals = constraint_signals or {}
    clean_makers = clean_makers or set()

    by_constraint = defaultdict(list)
    for c in candidates:
        for p in (c.get("products") or []):
            if _looks_like_product(p):
                by_constraint[p].append(c)

    # WITHIN-CONSTRAINT NOISE GATE — per PRODUCT corroboration, not industry
    # labels. The volume-scored mapper dumps co-occurring names into a
    # constraint's bucket: the Defense-electronics bucket is 389 uncorroborated
    # co-occurrence names (WEBELSOLAR is solar, GENUSPOWER/SALZERELEC are power)
    # against just 9 the capability mapper confirms actually make defense
    # electronics (BEL/HAL/DATAPATTNS/AXISCADES). Industry-label filtering
    # cannot separate them — WEBELSOLAR's own classification is the generic
    # "Other Electrical Equipment", carrying no "solar" token to fail on.
    #
    # Corroboration is per-(company, product): "do the company's OWN filings
    # evidence making THIS product?" It is the measured-positive signal (kept
    # +76.7% vs dropped +54.7%) and it is PRODUCT-scoped, so a company
    # corroborated for solar is NOT a corroborated defense maker. The
    # ticker-level corroborated_maker/pure_play flags are exactly what let a
    # solar-confirmed WEBELSOLAR sort first under Defense; the gate and the
    # maker sort below both switch to the product-scoped flag.
    def _corr_status(c, product):
        # True = capability-confirmed maker of this product; False = mapper
        # co-occurrence the capability mapper contradicts; None = product
        # outside capability coverage (unassessed, e.g. CRGO Steel).
        if product in (c.get("corroborated_products") or ()):
            return True
        if product in (c.get("uncorroborated_products") or ()):
            return False
        return None

    def _corroboration_drop(product, cs):
        """Drop makers the capability mapper CONTRADICTS for this product, once
        the product is in capability coverage at all (>=1 confirmed maker).

        corr=False is a POSITIVE denial, not an absence: the product is covered,
        the company WAS scanned against it, and its own filings carry no
        manufacturing evidence. A single confirmed maker already proves the
        product is in coverage, so one is enough to trust the per-company
        denials — requiring two abstained on thin-but-real coverage and kept
        capability-denied noise (2022 Battery Cell kept TATAPOWER/FLUOROCHEM/
        NEOGEN, every one corr=False, because ARE&M was the lone confirmed
        maker). NULL (unassessed, e.g. CRGO Steel outside coverage) is still
        always kept — that pass-through protects constraints with no coverage
        from being judged at all."""
        core = sum(1 for c in cs if _corr_status(c, product) is True)
        if core < 1:
            return set()      # product not in capability coverage -> don't judge
        return {(c.get("ticker") or "").strip().upper()
                for c in cs if _corr_status(c, product) is False}

    # A pure-services company (IT/software, banks) is a CONSUMER of hardware,
    # never the physical maker of a hardware constraint. Every constraint here
    # is a physical product, so corroboration via filing-language mentions
    # (TCS/INFY/LTTS read "defense electronics" in their own filings) is a
    # false positive for these sectors. Sector-based, not ticker-based.
    _NON_MAKER_SECTORS = {"information technology", "financial services"}

    def _is_non_maker(c):
        for f in (c.get("sector"), c.get("industry")):
            if (f or "").strip().lower() in _NON_MAKER_SECTORS:
                return True
        return False

    blocks = []
    for product, cs in by_constraint.items():
        incoherent = _corroboration_drop(product, cs)   # capability-contradicted makers for this product
        makers, seen = [], set()
        for c in cs:
            tick = (c.get("ticker") or "").strip().upper()
            if not tick or tick in seen:
                continue
            if _is_non_maker(c):
                continue      # IT/financial services never physically make a hardware constraint
            if tick in incoherent:
                continue      # e.g. WEBELSOLAR (solar) under Defense electronics
            seen.add(tick)
            makers.append({
                "ticker": tick, "company": c.get("company"),
                # PRODUCT-scoped, not ticker-wide: confirmed maker of THIS product
                "product_corroborated": _corr_status(c, product) is True,
                "corroborated_maker": bool(c.get("corroborated_maker")),
                "pure_play_confirmed": tick in clean_makers,
                "order_book": bool(c.get("order_book")),
                "composite_score": c.get("composite_score"),
                "industry": c.get("industry_detail") or c.get("industry"),
            })
        makers.sort(key=lambda m: (0 if m["product_corroborated"] else
                                   1 if m["pure_play_confirmed"] else
                                   2 if m["corroborated_maker"] else 3,
                                   -(m["composite_score"] or 0)))
        raw_signal = constraint_signals.get(product, {})
        # Older snapshots sometimes persisted a single numeric diagnostic for
        # a product.  It has no named, point-in-time meaning and therefore must
        # not be guessed into an explosiveness component.  Preserve it only as
        # an audit warning while the typed signal bundle is rebuilt.
        sig = dict(raw_signal) if isinstance(raw_signal, dict) else {
            "legacy_untyped_signal": raw_signal,
            "signal_contract_warning": "numeric legacy signal ignored for ranking",
        }
        sig["n_makers"] = len(makers)
        score, why = _explosiveness_score(product, sig, country, rank_mode)
        # CONFIDENCE gate for the constraint+stocks contract. A constraint we
        # have NO theme for (stage None), NO capability-confirmed maker, and
        # that is not a recognised import-substitution play is unverifiable —
        # its "makers" are pure co-occurrence noise (wind names under
        # Fluorochemicals, solar glass under Polysilicon). Scarcity alone then
        # floated such blocks into the top list once the saturated order-book/
        # recency constants were removed. An unactionable, unverifiable
        # constraint is worse than a shorter list, so it is dropped below.
        n_corr = sum(1 for m in makers if m.get("product_corroborated"))
        verifiable = bool(sig.get("stage")) or n_corr > 0 or bool(sig.get("import_substitution"))
        blocks.append({
            "constraint": product,
            "display_label": _clean_constraint_label(product),
            "explosiveness_score": score,
            "why_now": why,
            "stage": sig.get("stage"),
            "verifiable": verifiable,
            "signal_contract_warning": sig.get("signal_contract_warning"),
            "n_makers": len(makers),
            "makers": makers,   # trimmed after dedup
        })

    # DEDUP near-identical products — HS labels and mapped names describe the
    # same thing ("Printed Circuit Boards" == "Circuits; printed";
    # "Semiconductor IC" == "Semiconductor ICs"). Merge on a normalised token
    # key: keep the higher explosiveness score and cleaner label, union makers.
    merged = {}
    for b in blocks:
        key = _dedup_key(b["constraint"])
        if key not in merged:
            merged[key] = b
        elif b["explosiveness_score"] > merged[key]["explosiveness_score"]:
            um = {m["ticker"]: m for m in merged[key]["makers"]}
            um.update({m["ticker"]: m for m in b["makers"]})
            b["makers"] = list(um.values())
            b["verifiable"] = b.get("verifiable") or merged[key].get("verifiable")
            merged[key] = b
        else:
            have = {m["ticker"] for m in merged[key]["makers"]}
            merged[key]["makers"].extend(m for m in b["makers"] if m["ticker"] not in have)
            merged[key]["verifiable"] = merged[key].get("verifiable") or b.get("verifiable")

    # Drop unverifiable constraints (no theme, no confirmed maker, no import-sub
    # status) — a constraint whose only stocks are co-occurrence noise is not a
    # decision. Kept in the wider JSON universe, never in the ranked list.
    blocks = [b for b in merged.values() if b.get("verifiable")]
    for b in blocks:
        b["makers"].sort(key=lambda m: (0 if m.get("product_corroborated") else
                                        1 if m["pure_play_confirmed"] else
                                        2 if m["corroborated_maker"] else 3,
                                        -(m["composite_score"] or 0)))
        b["n_makers"] = len(b["makers"])
        b["makers"] = b["makers"][:makers_per_constraint]
    blocks.sort(key=lambda b: -b["explosiveness_score"])
    for i, b in enumerate(blocks[:top_constraints], 1):
        b["rank"] = i
    return blocks[:top_constraints]


# Constraint-level 3-year forward returns, measured this session across the
# uncapped 2020-2022 anchors (scripts/research/constraint_grader_backtest.py).
# Used only to TAG a name's constraint as historically-strong for tiering —
# never as a point-in-time input to selection. From DB rows would be better;
# these are the measured medians pending the reference-table history that is
# now accumulating monthly.
MEASURED_CONSTRAINT_RETURN_3Y = {
    "CRGO Steel": 160.9, "Power Transformer": 160.9, "Semiconductor IC": 71.1,
    "Solar Cell": 66.4, "Solar Wafer": 66.4, "EMS / Contract Manufacturing": 66.8,
    "PCB / Printed Circuit Board": 66.8, "Battery Cell (Li-ion)": 59.2,
    "Rolling Stock / Locomotives": 78.6, "Defense electronics": 68.7,
    "Solar Module": 39.8, "Optical Fiber Cable": 39.1,
}


def fetch_growth_sentiment(cur, as_of, country="IN", window_days=540, tickers=None):
    """Per-ticker management growth-ambition + concall tone, point-in-time.

    Reads the ingestion-level signals (mg_signals: growth_ambition,
    management_sentiment; see scripts/policy/extract_growth_sentiment.py). A
    maker guiding a large own-revenue/capacity multiple on an upbeat call is a
    conviction differentiator once the constraint and the maker role are proved.
    This is an annotation only — it does not change composite_score, so the
    validated ranking is untouched. filed_at<=as_of keeps it point-in-time.
    """
    cur.execute("SELECT to_regclass('public.mg_signals') AS n")
    if not cur.fetchone()["n"]:
        return {}
    start = as_of - timedelta(days=window_days)
    tick_filter = ""
    params = [country, start, as_of]
    if tickers:
        tick_filter = " AND UPPER(TRIM(d.ticker)) = ANY(%s)"
        params.append([t.upper() for t in tickers])
    rows = q(cur, f"""
        SELECT UPPER(TRIM(d.ticker)) AS ticker, s.signal_type, s.signal_value,
               s.signal_unit, s.perspective, s.confidence, s.filed_at, s.context_text
        FROM mg_signals s JOIN mg_documents d ON d.id = s.document_id
        WHERE s.signal_type IN ('growth_ambition','management_sentiment')
          AND s.country = %s AND s.filed_at BETWEEN %s AND %s
          AND d.ticker IS NOT NULL AND BTRIM(d.ticker) <> '' {tick_filter}
        ORDER BY d.ticker, s.filed_at DESC
    """, tuple(params))
    out: dict[str, dict] = {}
    for r in rows:
        t = r["ticker"]
        o = out.setdefault(t, {"growth": None, "tone_sum": 0.0, "tone_n": 0})
        if r["signal_type"] == "growth_ambition":
            # Keep the strongest COMPANY-perspective ambition (highest multiple),
            # falling back to market only if no company guidance exists.
            cand = {"multiple": r["signal_value"], "basis": r["signal_unit"],
                    "perspective": r["perspective"], "confidence": r["confidence"],
                    "as_of": r["filed_at"].isoformat() if r["filed_at"] else None,
                    "context": (r["context_text"] or "")[:220]}
            cur_best = o["growth"]
            better = (cur_best is None
                      or (cand["perspective"] == "company" and cur_best["perspective"] != "company")
                      or (cand["perspective"] == cur_best["perspective"]
                          and (cand["multiple"] or 0) > (cur_best["multiple"] or 0)))
            if better:
                o["growth"] = cand
        elif r["signal_type"] == "management_sentiment" and r["signal_value"] is not None:
            o["tone_sum"] += float(r["signal_value"])
            o["tone_n"] += 1
    for t, o in out.items():
        o["concall_tone"] = round(o["tone_sum"] / o["tone_n"], 3) if o["tone_n"] else None
        o.pop("tone_sum", None)
    return out


def rank_candidates(cur, supply_by_product, as_of, top_n=25, theme_quarters=None,
                    country="IN"):
    """Build the ranked candidate list with backtest lessons applied.

    Lesson 1+5: composite = fundamental × freshness_multiplier.
                Fresh themes (1-4 confirmed quarters) get ×1.12 bonus;
                Consensus themes (16+) get ×0.93 discount.
                Ranked by composite_score (freshness-adjusted).
    Lesson 2:   200DMA → position_size_guidance (full/half), NOT a score input.
                Removed ×1.10 bonus for above-200DMA — backtest showed above-200DMA
                stocks had LOWER avg alpha (+29.8%) than below-200DMA (+35.2%).
    Lesson 4:   Risk overlay via fetch_risk_flags — HIGH/ELEVATED/NORMAL tier
                from mg_documents. High-risk candidates flagged but not auto-excluded
                (user decides; the flag prevents silent inclusion).
    Sector filter (prior): sector_mismatch=True excluded from ranked candidates.
    Demand discount (prior): demand_side_flag=True → ×0.3 conviction.
    """
    theme_quarters = theme_quarters or {}

    agg = {}
    for prod, lst in supply_by_product.items():
        for r in lst:
            tick = (r["ticker"] or "").strip().upper()
            if not tick:
                continue
            if tick in MANUAL_EXCLUDE:
                continue
            if r.get("sector_mismatch"):
                continue
            effective_conviction = float(r["conviction_score"] or 0)
            if r.get("demand_side_flag"):
                effective_conviction *= 0.3

            a = agg.setdefault(tick, {
                "ticker": tick, "company": r["company"], "products": set(),
                "themes": set(), "max_conviction": 0.0, "order_book": False,
                "import_sub": False, "types": set(),
                "best_rank": 999, "total_capex": 0, "best_type_score": 0,
                "any_demand_side": False, "_theme_set": set(),
                "industry": r.get("industry_nse") or r.get("industry_detail"),
                "industry_detail": r.get("industry_detail"),
                "sector": r.get("sector"),
                "capex_signals": 0,
                # Decision lanes must never borrow a catalyst or supply-side
                # role from an adjacent product.  The ticker-wide fields above
                # remain useful raw-screen diagnostics; this ledger carries
                # the exact product-level evidence required for a constraint
                # investment thesis.
                "product_evidence": {},
                # corroboration across this ticker's product mappings
                "corr_true": False, "corr_null": False, "corr_false": False,
                # Keep the product-level provenance.  A company may make one
                # mapped product while a second association is noise; decision
                # lanes must anchor to the corroborated product, not reject or
                # promote the entire ticker on an unrelated mapping.
                "corroborated_products": set(),
                "uncorroborated_products": set(),
                "unassessed_products": set(),
            })
            _cv = r.get("corroborated")
            if _cv is True:
                a["corr_true"] = True
                a["corroborated_products"].add(prod)
            elif _cv is False:
                a["corr_false"] = True
                a["uncorroborated_products"].add(prod)
            else:
                a["corr_null"] = True
                a["unassessed_products"].add(prod)
            product_evidence = a["product_evidence"].setdefault(prod, {
                "order_book": False, "capex_signals": 0, "import_sub": False,
                "types": set(), "demand_side": False, "signal_count": 0,
                "max_conviction": 0.0, "company_candidate_states": set(),
                "earnings_capture_statuses": set(),
                "max_physical_evidence_count": 0,
                "max_pipeline_evidence_count": 0,
                "max_earnings_capture_count": 0,
            })
            product_evidence["order_book"] = (
                product_evidence["order_book"] or bool(r["has_order_book_signals"])
            )
            product_evidence["capex_signals"] = max(
                product_evidence["capex_signals"], int(r.get("capex_signals") or 0)
            )
            product_evidence["import_sub"] = (
                product_evidence["import_sub"] or bool(r["import_substitution_play"])
            )
            product_evidence["types"].add(r["beneficiary_type"])
            product_evidence["demand_side"] = (
                product_evidence["demand_side"] or bool(r.get("demand_side_flag"))
            )
            product_evidence["signal_count"] = max(
                product_evidence["signal_count"], int(r.get("signal_count") or 0)
            )
            product_evidence["max_conviction"] = max(
                product_evidence["max_conviction"], effective_conviction
            )
            if r.get("company_candidate_state"):
                product_evidence["company_candidate_states"].add(
                    r["company_candidate_state"]
                )
            if r.get("earnings_capture_status"):
                product_evidence["earnings_capture_statuses"].add(
                    r["earnings_capture_status"]
                )
            product_evidence["max_physical_evidence_count"] = max(
                product_evidence["max_physical_evidence_count"],
                int(r.get("physical_evidence_count") or 0),
            )
            product_evidence["max_pipeline_evidence_count"] = max(
                product_evidence["max_pipeline_evidence_count"],
                int(r.get("pipeline_evidence_count") or 0),
            )
            product_evidence["max_earnings_capture_count"] = max(
                product_evidence["max_earnings_capture_count"],
                int(r.get("earnings_capture_count") or 0),
            )
            a["capex_signals"] = max(a["capex_signals"], int(r.get("capex_signals") or 0))
            a["products"].add(prod)
            a["themes"].add(r["theme_name"])
            a["_theme_set"].add(r["theme_name"])
            a["max_conviction"] = max(a["max_conviction"], effective_conviction)
            a["order_book"] = a["order_book"] or bool(r["has_order_book_signals"])
            a["import_sub"] = a["import_sub"] or bool(r["import_substitution_play"])
            a["types"].add(r["beneficiary_type"])
            a["best_rank"] = min(a["best_rank"], r.get("supply_chain_stage") or 99)
            a["total_capex"] += int(r.get("signal_count") or 0)
            a["best_type_score"] = max(a["best_type_score"],
                                       _SS_TYPE_SCORE.get(r.get("beneficiary_type") or "", 0))
            if r.get("demand_side_flag"):
                a["any_demand_side"] = True

    candidates = list(agg.values())

    # CORROBORATION GATE + RANK KEY (Aug-2026, validated before shipping —
    # scripts/research/mapper_corroboration_test.py, 3 anchors, 3y forward).
    # The beneficiary mapper scores conviction on signal VOLUME alone, so it
    # ranks banks/staffing/cement at 0.85 under real constraints. Corroboration
    # (does the company's OWN filings evidence making the product?) is the
    # company-level signal that measured positive: kept names +76.7% vs dropped
    # +54.7%, coherence 17%->70%.
    #  - DROP a candidate only if every one of its mappings is corroborated=False
    #    (pure co-occurrence junk). A ticker corroborated on ANY product, or
    #    NULL (product outside capability coverage, e.g. CRGO Steel) is KEPT —
    #    NULL pass-through is what protects the best constraints from coverage
    #    gaps.
    #  - corr_tier then LEADS the sort, ahead of composite_score. This
    #    supersedes conviction-led ordering, which the constraint backtest found
    #    anti-predictive (-9.5pp); corroboration is the measured-positive
    #    replacement. composite_score remains the within-tier tiebreaker rather
    #    than being ripped out on thin data.
    dropped_uncorroborated = []
    kept = []
    for a in candidates:
        if a["corr_false"] and not a["corr_true"] and not a["corr_null"]:
            dropped_uncorroborated.append(a["ticker"])
        else:
            kept.append(a)
    candidates = kept
    for a in candidates:
        a["corroborated_maker"] = bool(a["corr_true"])
        a["corroboration_tier"] = 0 if a["corr_true"] else 1  # 1 = not-assessable
        a["corroboration_note"] = (
            "own filings confirm it makes a constrained product" if a["corr_true"]
            else "not assessable — product outside capability-mapper coverage; "
                 "kept, not confirmed")

    # Lesson 5: min confirmed_quarters per candidate (across its themes)
    for a in candidates:
        qtrs = [theme_quarters.get(tn, 20) for tn in a["_theme_set"] if tn]
        a["min_confirmed_quarters"] = min(qtrs) if qtrs else 20

    for a in candidates:
        # Keep a complete machine-readable mapping for decision gates, while
        # the compact ``products`` display field remains bounded for reports.
        # A product that sorts after the first six cannot be allowed to vanish
        # from an otherwise exact company-constraint decision.
        a["all_products"] = sorted(a["products"])
        a["products"] = a["all_products"][:6]
        a["corroborated_products"] = sorted(a["corroborated_products"])
        a["uncorroborated_products"] = sorted(a["uncorroborated_products"])
        a["unassessed_products"] = sorted(a["unassessed_products"])
        a["product_evidence"] = {
            product: {
                **evidence,
                "types": sorted(evidence["types"]),
                "company_candidate_states": sorted(evidence["company_candidate_states"]),
                "earnings_capture_statuses": sorted(evidence["earnings_capture_statuses"]),
            }
            for product, evidence in sorted(a["product_evidence"].items())
        }
        a["themes"] = sorted(a["themes"])[:6]
        a["types"] = sorted(a["types"])
        del a["_theme_set"]
        breadth = min(len(a["products"]), 5) / 5.0
        # Lesson 1+5: freshness multiplier applied here so pre-sort uses it
        fm, fl = _freshness_multiplier(a["min_confirmed_quarters"])
        breakdown = _scoring_breakdown_in(a["max_conviction"], breadth,
                                          a["order_book"], a["import_sub"],
                                          freshness_multiplier=fm, freshness_label=fl)
        a["fundamental_score"] = breakdown["fundamental_score"]
        a["composite_score"] = breakdown["composite_score"]
        a["scoring_breakdown"] = breakdown
        a["supply_side_confidence_pct"] = min(100,
            _supply_side_confidence(a["best_rank"], a["total_capex"],
                                    len(a["products"]), a["max_conviction"] * 100,
                                    a["order_book"])
            + a["best_type_score"])

    # Pre-sort: corroboration tier LEADS (confirmed makers first), then
    # composite as the within-tier tiebreaker. This replaces conviction-led
    # ordering — the validated, company-level, measured-positive signal now
    # decides who appears at the top, not signal volume.
    candidates.sort(key=lambda a: (a["corroboration_tier"],
                                   -a["composite_score"], -a["fundamental_score"]))
    candidates = candidates[:top_n * 2]

    # Technical overlay — bhavcopy (as-of safe)
    tickers = [a["ticker"] for a in candidates]
    px = q(cur, """
        SELECT symbol, trade_date, close, high
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series IN ('EQ','BE')
          AND trade_date BETWEEN %s AND %s
        ORDER BY symbol, trade_date
    """, (tickers, as_of - timedelta(days=420), as_of)) if country == "IN" else []
    price_series = defaultdict(list)
    for r in px:
        price_series[r["symbol"]].append(r)

    for a in candidates:
        fm, fl = _freshness_multiplier(a["min_confirmed_quarters"])
        s = price_series.get(a["ticker"])
        if not s or len(s) < 40:
            a["technical"] = None
            a["position_size_guidance"] = "verify_chart"
            # Keep composite from pre-sort pass
            a["scoring_breakdown"] = _scoring_breakdown_in(
                a["max_conviction"], min(len(a["products"]), 5) / 5.0,
                a["order_book"], a["import_sub"],
                freshness_multiplier=fm, freshness_label=fl,
                technical_flag="no_price_data")
            a["composite_score"] = a["scoring_breakdown"]["composite_score"]
            continue

        closes = [float(r["close"]) for r in s if r["close"] is not None]
        highs  = [float(r["high"])  for r in s if r["high"]  is not None]
        last   = closes[-1]
        hi52   = max(highs[-250:]) if highs else None
        ma200  = sum(closes[-200:]) / min(200, len(closes))
        base   = closes[-126] if len(closes) >= 126 else closes[0]
        pct_from_high = round((last - hi52) / hi52 * 100, 1) if hi52 else None
        above_200 = last > ma200
        young_chain = a.get("min_confirmed_quarters", 20) < 8

        a["technical"] = {
            "last_close":        round(last, 2),
            "pct_from_52w_high": pct_from_high,
            "above_200dma":      above_200,
            "ret_6m_pct":        round((last - base) / base * 100, 1) if base else None,
        }

        # Lesson 2: 200DMA → position sizing guidance, NOT a score multiplier.
        # Backtest showed above-200DMA had higher hit rate (62%) but LOWER avg alpha
        # (+29.8%) vs below-200DMA (+35.2%). The bonus was removing the high-upside names.
        good_setup = above_200 and pct_from_high is not None and pct_from_high > -25
        if good_setup:
            tech_flag = "constructive"
            position_guidance = "full_position"
        elif not above_200 and young_chain:
            tech_flag = "base_building_early_phase"
            position_guidance = "half_position_scale_on_breakout"
        elif not above_200:
            tech_flag = "base_building"
            position_guidance = "half_position_scale_on_breakout"
        else:
            tech_flag = "extended_above_high"
            position_guidance = "verify_extension_before_entry"

        a["position_size_guidance"] = position_guidance
        a["scoring_breakdown"] = _scoring_breakdown_in(
            a["max_conviction"], min(len(a["products"]), 5) / 5.0,
            a["order_book"], a["import_sub"],
            freshness_multiplier=fm, freshness_label=fl,
            technical_flag=tech_flag)
        a["composite_score"] = a["scoring_breakdown"]["composite_score"]

    # Lesson 4: batch-fetch risk flags from mg_documents
    risk_flags = fetch_risk_flags(cur, tickers, as_of)
    for a in candidates:
        rf = risk_flags.get(a["ticker"], {"risk_tier": "NORMAL", "risk_events": []})
        a["risk_tier"] = rf["risk_tier"]
        a["risk_events"] = rf["risk_events"][:5]   # cap to 5 most recent

    # India policy overlay (PLI/ALMM/BCD/...): evidence for the judgment layer, never scored
    policy_ev = fetch_policy_evidence(cur, tickers, as_of) if country == "IN" else {}
    for a in candidates:
        a["policy_evidence"] = policy_ev.get(a["ticker"], {"schemes": {}, "n_policy_filings": 0})

    # Final sort: corroboration tier LEADS (confirmed makers first), then
    # composite (freshness-adjusted), then fundamental. Keeps the validated
    # company-level signal ahead of the volume-driven composite.
    candidates.sort(key=lambda a: (a.get("corroboration_tier", 1),
                                   -a.get("composite_score", 0), -a["fundamental_score"]))

    # Lesson 1+3: Tier 1 (top 5 actionable); T3 split into Watch vs Ignore.
    # Multi-year backtest: T3 outliers (POWERINDIA, STLTECH) consistently had
    # fresh/new_theme freshness and landed at ranks 6-8 before graduating to T1
    # the following year. Consensus/established T3 names never graduated and had
    # higher severe-loss rates. Two sub-tiers remove ambiguity for multi-year holds:
    #   Tier3_Watch  — fresh/new_theme, rank 6-8, NORMAL risk → add if moves to T1 next scan
    #   Tier3_Ignore — established/consensus OR rank 9+ OR ELEVATED/HIGH risk → drop
    result = candidates[:top_n]
    # Attach management growth-ambition + concall tone as a conviction overlay
    # (annotation only; never alters composite_score or ordering).
    gs = fetch_growth_sentiment(cur, as_of, country,
                                tickers=[a["ticker"] for a in result])
    for a in result:
        info = gs.get(a["ticker"]) or {}
        a["growth_ambition"] = info.get("growth")
        a["management_concall_tone"] = info.get("concall_tone")
    for i, a in enumerate(result):
        if i < 5:
            a["discovery_tier"] = "Tier1_HighConviction"
        elif (i <= 7
              and a["scoring_breakdown"].get("freshness_label") in ("fresh", "new_theme")
              and a["risk_tier"] == "NORMAL"):
            a["discovery_tier"] = "Tier3_Watch"
        else:
            a["discovery_tier"] = "Tier3_Ignore"
    return result


def compute_chain_coverage(supply_by_product, major_themes, emerging_themes):
    """Lesson 4 — per-chain coverage density: how complete is the beneficiary extraction?

    Ratio = supply-side companies mapped in this chain /
            total companies mapped to the matched theme.
    When < 5% the selector output is showing a slice of the real universe;
    a coverage_warning flag is set so the briefing can caveat accordingly.
    """
    # Build a name→company_count lookup from all themes
    theme_cos: dict[str, int] = {}
    for t in major_themes + emerging_themes:
        tn = (t.get("theme_name") or "").lower()
        cos = t.get("company_count") or 0
        if tn and cos:
            theme_cos[tn] = max(theme_cos.get(tn, 0), cos)

    coverage: dict[str, dict] = {}
    for chain, companies in supply_by_product.items():
        n_supply = len([c for c in companies
                        if not c.get("sector_mismatch") and not c.get("demand_side_flag")])
        # fuzzy-match chain name against theme names
        chain_lower = chain.lower()
        matched_cos = 0
        for tn, cos in theme_cos.items():
            if any(word in tn for word in chain_lower.split()[:3]) or any(word in chain_lower for word in tn.split()[:3]):
                matched_cos = max(matched_cos, cos)
        density = round(n_supply / matched_cos * 100, 1) if matched_cos else None
        coverage[chain] = {
            "n_supply_mapped": n_supply,
            "theme_company_count": matched_cos or None,
            "coverage_density_pct": density,
            "coverage_warning": (density is not None and density < 5),
        }
    return coverage


# ── Layer types: how a chain monetizes scarcity (drives entry logic) ─────────
# differentiated: qualification barriers / programmatic demand — capex-UP is the
#   entry signal; positions are multi-year compounders (CRGO, transformers).
# commodity: process capacity anyone can add — constraint forms via industry
#   capex CUTS + demand inflection; positions are TRADES with exit triggers
#   (solar modules 2023: +579% then −95%; memory cycle).
# service_manufacturing / epc: revenue grows with the theme but margins are
#   bid away — scarcity rent accrues elsewhere.

# ── Symbol renames: price history lives under the OLD symbol ─────────────────
# The HBLENGINE lesson (Jul-2026): HBL Power renamed to HBL Engineering in
# Dec-2024, so any price lookup by current ticker sees only post-rename data —
# hiding a 12x 2023-24 run and making a late-arc stock look like a fresh entry.
# Same failure hid GVT&D's 26x for years. Any function computing multi-year
# returns/lows by ticker must UNION these aliases.
# Symbol rename aliases: DETECTED from bhavcopy price-series continuity and
# stored in mg_symbol_renames (see detect_and_store_symbol_renames) — no
# hardcoded rename list (user rule; the HBLENGINE lesson stays, the dict goes).
# {symbol: [other symbols of the same listing]} — bidirectional; populated at
# scan start from rows with confidence='high' or status='confirmed'.
SYMBOL_ALIASES: dict[str, list[str]] = {}


def price_symbols(ticker: str) -> list[str]:
    """Current symbol plus any historical aliases, for price-history queries."""
    t = (ticker or "").strip().upper()
    return [t] + SYMBOL_ALIASES.get(t, [])


# Chain layer classifications: loaded from mg_chain_classifications at scan
# start — the judgment layer classifies new chains with SQL inserts (rationale
# column required), never code edits. Unclassified chains are flagged in the
# report as needing classification.
CHAIN_LAYER_TYPE: dict[str, str] = {}


def chain_layer_type(chain_name: str) -> str:
    key = (chain_name or "").strip().lower()
    for k, v in CHAIN_LAYER_TYPE.items():
        if k in key:
            return v
    return "unclassified"




NOVEL_VOCAB_STOP = {
    "THE", "THIS", "THAT", "WITH", "FROM", "HAVE", "BEEN", "WILL", "SHALL", "BOARD",
    "COMPANY", "LIMITED", "INDIA", "INDIAN", "GOVERNMENT", "MINISTRY", "RAILWAY",
    "RAILWAYS", "EXCHANGE", "STOCK", "ANNUAL", "GENERAL", "MEETING", "DIRECTORS",
    "SCHEME", "SCHEMES", "MISSION", "YOJANA", "MANDATORY", "NATIONAL", "CENTRAL",
    "STATE", "PUBLIC", "PRIVATE", "SECURITIES", "REGULATION", "REGULATIONS", "SEBI",
    "ESOP", "ESOS", "GRATUITY", "DIVIDEND", "AGM", "EGM", "NCLT", "NCLAT", "CSR",
    "NOTES", "CRORE", "LAKH",
    "AMALGAMATION", "ARRANGEMENT", "MERGER", "TRUST", "OPTIONS", "EMPLOYEE",
    "EMPLOYEES", "INCENTIVE", "BONUS", "BUYBACK", "RIGHTS", "PREFERENTIAL",
}
# NOTE: scheme acronyms (PLI/FAME/KUSUM/...) are NOT hardcoded here anymore —
# already-tracked schemes are excluded from "novelty" dynamically, from
# mg_tracked_schemes (see tracked_scheme_tokens). A scheme the system tracks
# can't be novel; a scheme it doesn't track yet must be allowed to surface.


def compute_trade_momentum_signals(cur, as_of, min_months: int = 6, widen_threshold_pct: float = 15.0) -> list:
    """DGCI&S-style leading indicator (scripts/policy/ingest_trade_flows.py,
    real UN Comtrade monthly India import data, no company disclosure needed):
    a widening import-dependency trend for an HS code precedes any company's
    filing mention of the underlying constraint by 1-2+ quarters, since trade
    flow is monthly customs reality, not self-reported quarterly commentary.

    JUDGMENT-REVIEW ONLY, like regulatory_watchlist: this confirms a constraint
    is real and accelerating, it does not by itself name beneficiary companies
    (HS codes are commodity-level, not 1:1 to a company's end-market) -- the
    judgment layer cross-references this against constrained_products/
    import_dependencies to decide which existing theme it corroborates.
    Point-in-time: only periods <= as_of's month are read."""
    as_of_period = f"{as_of.year}{as_of.month:02d}"
    try:
        cur.execute("""
            SELECT hs_code, period, value_usd FROM mg_trade_flows
            WHERE reporter_country='India' AND partner_country='World'
              AND flow_direction='import' AND period <= %s
            ORDER BY hs_code, period
        """, (as_of_period,))
        rows = cur.fetchall()
    except Exception:
        return []  # table may not exist yet on an older DB snapshot
    if not rows:
        return []

    by_hs: dict = {}
    for r in rows:
        by_hs.setdefault(r["hs_code"], []).append((r["period"], float(r["value_usd"] or 0)))

    cur.execute("SELECT DISTINCT hs_code, sector, component, risk_level FROM mg_import_dependencies")
    ctx = {r["hs_code"]: r for r in cur.fetchall()}

    out = []
    for hs_code, series in by_hs.items():
        if len(series) < min_months:
            continue
        series.sort()
        half = len(series) // 2
        recent = [v for _, v in series[half:]]
        prior = [v for _, v in series[:half]]
        if not prior or sum(prior) == 0:
            continue
        recent_avg, prior_avg = sum(recent) / len(recent), sum(prior) / len(prior)
        momentum_pct = round((recent_avg - prior_avg) / prior_avg * 100, 1)
        trend = ("widening" if momentum_pct >= widen_threshold_pct
                 else "narrowing" if momentum_pct <= -widen_threshold_pct else "flat")
        c = ctx.get(hs_code, {})
        out.append({
            "hs_code": hs_code, "sector": c.get("sector"), "component": c.get("component"),
            "risk_level": c.get("risk_level"), "months_of_data": len(series),
            "latest_period": series[-1][0], "momentum_pct": momentum_pct, "trend": trend,
        })
    out.sort(key=lambda x: -abs(x["momentum_pct"]))
    return out


def compute_regulatory_watchlist(cur, as_of, months_back: int = 9, min_score: int = 1) -> list:
    """Draft/consultation-stage government announcements (mg_policy_announcements,
    populated by scripts/policy/ingest_pib.py) -- these predate any company filing
    mention by 2-4 quarters (a draft QCO or PLI amendment is public before any
    company discloses compliance with it, since nothing is binding yet).

    JUDGMENT-REVIEW ONLY, never client-facing and never auto-scored: a draft can
    be watered down, delayed, or dropped. This is a watchlist for the judgment
    layer to notice early, not a theme with beneficiaries or evidence_confidence.
    Point-in-time: published_date <= as_of, same discipline as everything else."""
    try:
        cur.execute("""
            SELECT published_date, ministry, title, url, scheme_score, scheme_signals
            FROM mg_policy_announcements
            WHERE stage = 'draft' AND scheme_score >= %s
              AND published_date <= %s
              AND published_date >= %s - (%s || ' months')::interval
            ORDER BY scheme_score DESC, published_date DESC
            LIMIT 30
        """, (min_score, as_of, as_of, months_back))
        rows = cur.fetchall()
    except Exception:
        return []  # table may not exist yet on an older DB snapshot
    return [{"published_date": r["published_date"].isoformat(), "ministry": r["ministry"],
              "title": r["title"], "url": r["url"], "scheme_score": r["scheme_score"],
              "scheme_signals": r["scheme_signals"] or []} for r in rows]


def tracked_scheme_tokens() -> set:
    """Uppercase word tokens of every tracked scheme's name+pattern — these are
    known vocabulary, excluded from novelty detection dynamically."""
    toks = set()
    for name, pattern in INDIA_POLICY_SCHEMES.items():
        for w in re.findall(r"[A-Za-z]{3,}", name + " " + pattern):
            toks.add(w.upper())
    return toks


def detect_novel_policy_vocabulary(cur, as_of, min_docs: int = 3, min_ticks: int = 2,
                                    top_n: int = 60) -> list:
    """The KAVACH problem: scheme regexes only contain words already known — a NEW
    mandate/scheme word (KAVACH, 2022) is invisible until someone studies its
    winner years later. This detector needs NO dictionary: it extracts
    capitalized candidate terms from text windows around policy-context words
    and flags terms bursting from zero (prior 12m ~0 docs -> current 12m >= 3
    docs across >= 2 tickers).

    Validated point-in-time: run as-of Jun-2023, KAVACH qualifies (3 docs,
    3 tickers: KERNEX/HBLENGINE/ASHOKA, prior=0) — at HBL ~Rs.98, before its
    12x. PRECISION IS DELIBERATELY LOW (OCR noise, signatory names survive the
    filters): this list is a MONTHLY JUDGMENT-REVIEW artifact, ~90% discardable
    noise — the reviewer's job is the 10%. Real finds get promoted into
    INDIA_POLICY_SCHEMES. Never a score input, never auto-actionable."""
    context = r"mandat|scheme|yojana|mission|approved by|Railway Board|notification"
    stop = NOVEL_VOCAB_STOP | tracked_scheme_tokens()

    def term_counts(start, end):
        cur.execute("""
            SELECT ticker, raw_text FROM mg_documents
            WHERE country='IN' AND ticker IS NOT NULL AND ticker <> ''
              AND filed_at > %s AND filed_at <= %s AND raw_text ~* %s
        """, (start, end, context))
        docs_by_term, ticks_by_term = {}, {}
        lower_hits, capped_hits = {}, {}   # corpus-wide case stats per term
        for i, row in enumerate(cur.fetchall()):
            tick, text = row["ticker"], row["raw_text"]
            if not text:
                continue
            wins = []
            for m in list(re.finditer(context, text, re.I))[:12]:
                a = max(0, m.start() - 120)
                wins.append(text[a:m.end() + 120])
            blob = " ".join(wins)
            blob_low = blob.lower()
            cands = set(re.findall(r"\b[A-Z]{4,12}\b", blob))
            cands |= {w.upper() for w in re.findall(r"\b[A-Z][a-z]{4,11}\b", blob)}
            for c in cands:
                if c in stop or c.isdigit():
                    continue
                total = len(re.findall(r"\b" + re.escape(c.lower()) + r"\b", blob_low))
                capped = len(re.findall(r"\b(?:" + re.escape(c) + "|" + re.escape(c.capitalize()) + r")\b", blob))
                if total == 0 or capped / total < 0.8:
                    continue
                # ordinary-English test, corpus-wide: a real scheme word/acronym
                # (KAVACH, ECMS, RDSS) is ~never written lowercase in filings;
                # ordinary English ("quantum", "maintain", "could") is — count
                # true-lowercase occurrences across the FULL document text and
                # drop terms whose lowercase form is common (data-driven, no
                # dictionary; kills the QUANTUM/SPECIFY/MAINTAIN noise class).
                lower_hits[c] = lower_hits.get(c, 0) + len(
                    re.findall(r"\b" + re.escape(c.lower()) + r"\b", text))
                capped_hits[c] = capped_hits.get(c, 0) + capped
                person_ctx = len(re.findall(
                    r"(?:Mr|Ms|Mrs|Shri|Smt|Dr)\.?\s+\w{0,12}\s*" + re.escape(c.capitalize()) +
                    r"|" + re.escape(c.capitalize()) +
                    r"[\s,]{1,4}(?:Director|Company Secretary|Chairman|Managing|CFO|CEO|Whole|Officer)", blob))
                if person_ctx >= 2:
                    continue
                if len(c) >= 6 and re.search(r"\b" + re.escape(c.lower()) + r"[a-z]{1,4}\b", blob_low):
                    continue   # OCR truncation of a longer common word
                docs_by_term.setdefault(c, set()).add((tick, i))
                ticks_by_term.setdefault(c, set()).add(tick)
        # NOTE: the ordinary-English filter is applied ONLY to the final
        # candidate list in the caller, never inside a window — filtering the
        # prior-year baseline here would delete common terms from the baseline
        # and make them look "newly bursting" (false novelty; bug caught in
        # validation Jul-2026).
        return docs_by_term, ticks_by_term, lower_hits, capped_hits

    cur.execute("SELECT %s::date - 365 AS pe, %s::date - 730 AS ps", (as_of, as_of))
    row = cur.fetchone()
    prior_end, prior_start = row["pe"], row["ps"]

    cur_docs, cur_ticks, cur_lower, cur_capped = term_counts(prior_end, as_of)
    pri_docs, _, _, _ = term_counts(prior_start, prior_end)

    cur.execute("SELECT STRING_AGG(UPPER(company_name), ' ') AS nb FROM security_master WHERE company_name IS NOT NULL")
    name_blob = cur.fetchone()["nb"] or ""

    out = []
    for term, docs in cur_docs.items():
        n_now, n_tick = len(docs), len(cur_ticks[term])
        n_prior = len(pri_docs.get(term, set()))
        # ordinary-English test (data-driven, no dictionary): a real scheme
        # token (KAVACH/ECMS/RDSS) is ~never written lowercase in filings;
        # ordinary English ("quantum", "maintain") is — drop terms whose
        # lowercase form is common relative to the capitalized form.
        if cur_lower.get(term, 0) >= 0.3 * max(cur_capped.get(term, 1), 1):
            continue
        if n_now >= min_docs and n_tick >= min_ticks and n_prior == 0 and term not in name_blob:
            out.append({
                "term": term, "n_docs_12m": n_now, "n_tickers": n_tick,
                "n_docs_prior_12m": n_prior, "tickers": sorted(cur_ticks[term])[:8],
                "note": "novel policy-context vocabulary — judgment review required (~90% noise by design)",
            })
    out.sort(key=lambda x: (-x["n_tickers"], -x["n_docs_12m"]))
    return out[:top_n]


def compute_policy_beneficiary_screen(cur, as_of, min_docs: int = 4, top_n: int = 15,
                                       country: str = "IN") -> dict:
    """POLICY-FIRST discovery engine (independent of theme chains).

    The Dixon/PGEL/Amber lesson: PLI-driven explosions announce themselves in
    beneficiaries' OWN filings years early (PGEL first PLI mention Dec-2020,
    Amber Aug-2020, Dixon Apr-2021) — but the theme-chain mapper never needs to
    see them. Backtest of this exact screen at Dec-2022: avg +87% over 2 yrs
    (AMBER +293%, MINDACORP +139%, BOSCH +99%).

    For each India scheme: tickers whose own filings mention it >= min_docs
    times (point-in-time <= as_of), with recency trend (12mo vs prior 12mo) —
    rising intensity = the scheme is becoming the story. Purely a discovery
    list for the judgment layer: winner-vs-claimant check and layer-type
    archetype still apply (scheme executors are B-archetype; a claimant with
    mentions but no order conversion is the GREAVESCOT trap).
    """
    # Commitment-stage detector, PER SCHEME and self-extending (Jul-2026 — the
    # solar lesson: the old global pattern only spoke "PLI" phrasing, so a
    # company "selected under the High-Efficiency Solar PV Module scheme"
    # never registered a commitment and the whole solar cohort dropped out of
    # the shortlist). Commitment verbs are composed at runtime against EACH
    # scheme's own stored pattern, so any scheme ever added to
    # mg_tracked_schemes gets commitment detection automatically — no code
    # edits, works for US schemes (CHIPS awards) identically.
    COMMIT_VERBS = (r"(appl(?:y|ied|ication)|bid|approv\w*|select\w*|sanction\w*|allot\w*|"
                    r"award\w*|letter of (?:intent|award)|\yLoI\y|\yLoA\y|disburs\w*|beneficiar\w*)")

    schemes = list(INDIA_POLICY_SCHEMES.items())
    if not schemes:
        return {"qualified": {}, "early_pings": {}}

    # One corpus scan for every scheme.  The old implementation executed a
    # full raw_text regex scan per scheme, making a six-anchor replay scale as
    # anchors × schemes × corpus.  PostgreSQL now emits only per-document
    # booleans; aggregation remains exact and point-in-time in Python.
    flag_sql, flag_params, combined_parts = [], [], []
    for index, (_, pattern) in enumerate(schemes):
        commit_pat = (COMMIT_VERBS + r"[\s\S]{0,200}(?:" + pattern + r")"
                      r"|(?:" + pattern + r")[\s\S]{0,200}" + COMMIT_VERBS)
        flag_sql.extend([
            f"(d.raw_text ~* %s) AS scheme_{index}",
            f"(d.raw_text ~* %s) AS commit_{index}",
        ])
        flag_params.extend([pattern, commit_pat])
        combined_parts.append(f"(?:{pattern})")
    rows = q(cur, f"""
        SELECT UPPER(TRIM(d.ticker)) AS ticker, d.filed_at::date AS filed_at,
               {', '.join(flag_sql)}
        FROM mg_documents d
        WHERE d.country=%s AND d.ticker IS NOT NULL AND BTRIM(d.ticker) <> ''
          AND d.filed_at <= %s AND d.raw_text IS NOT NULL
          AND d.raw_text ~* %s
    """, tuple(flag_params + [country, as_of, "|".join(combined_parts)]))

    aggregate: dict[tuple[str, str], dict] = {}
    for row in rows:
        ticker = row["ticker"]
        filed_at = row["filed_at"]
        for index, (scheme, _) in enumerate(schemes):
            if not row[f"scheme_{index}"]:
                continue
            bucket = aggregate.setdefault((scheme, ticker), {
                "ticker": ticker, "dates": [], "commit_dates": [],
            })
            bucket["dates"].append(filed_at)
            if row[f"commit_{index}"]:
                bucket["commit_dates"].append(filed_at)

    industries = {}
    tickers = sorted({ticker for _, ticker in aggregate})
    if tickers:
        for row in q(cur, """
            SELECT UPPER(TRIM(nse_symbol)) AS ticker,
                   CONCAT_WS(' | ', NULLIF(industry_nse,''),
                             NULLIF(industry_bse,'')) AS industry
            FROM security_master
            WHERE UPPER(TRIM(nse_symbol)) = ANY(%s)
        """, (tickers,)):
            industries[row["ticker"]] = row.get("industry") or None

    out: dict[str, list] = {}
    early: dict[str, list] = {}
    current_start = as_of - timedelta(days=365)
    prior_start = as_of - timedelta(days=730)
    for scheme, _ in schemes:
        lst, pings = [], []
        scheme_rows = [value for (name, _), value in aggregate.items() if name == scheme]
        for r in scheme_rows:
            tick = r["ticker"]
            if tick in MANUAL_EXCLUDE:
                continue
            dates = r["dates"]
            commit_dates = r["commit_dates"]
            n_last12m = sum(observed > current_start for observed in dates)
            n_prior12m = sum(prior_start < observed <= current_start for observed in dates)
            entry = {
                "ticker": tick,
                "industry": industries.get(tick),
                "n_docs_total": len(dates),
                "n_last12m": n_last12m,
                "n_prior12m": n_prior12m,
                "trend": ("rising" if n_last12m > n_prior12m
                          else "fading" if n_last12m < n_prior12m else "flat"),
                "first_mention": min(dates),
                # non-null = winner-confirmed disclosure FOR THIS SCHEME
                "commitment": ({"first_commit": min(commit_dates),
                                "n_commit_docs": len(commit_dates)}
                               if commit_dates else None),
            }
            if entry["n_docs_total"] >= min_docs:
                lst.append(entry)
            elif entry["n_last12m"] >= 1:
                # EARLY PING: below the evidence threshold — watch-only, never a
                # decision input. Exists so a Shakti-2022 (2 KUSUM docs) sits on
                # the radar the whole time it accumulates toward qualification.
                pings.append(entry)
        # winner-confirmed names first, then by recent intensity
        lst.sort(key=lambda x: (x["commitment"] is None, -x["n_last12m"]))
        if lst:
            out[scheme] = lst[:top_n]
        pings.sort(key=lambda x: (x["first_mention"] is None, str(x["first_mention"])), reverse=True)
        if pings:
            early[scheme] = pings[:10]
    return {"qualified": out, "early_pings": early}


def _policy_signal_snapshot(cur, as_of, country: str = "IN") -> dict | None:
    """Load the latest dated broad policy discovery snapshot.

    Snapshot rows are a research discovery input.  A snapshot created after a
    historical report date is deliberately ineligible, even if every source
    document it summarises was older.  This keeps policy-first backtests free
    of later analyst-discovery leakage.
    """
    cur.execute("SELECT to_regclass('public.mg_policy_company_signals') AS name")
    if not cur.fetchone()["name"]:
        return None
    cur.execute("""
        SELECT MAX(as_of_date) AS snapshot_date
        FROM mg_policy_company_signals
        WHERE country=%s AND as_of_date <= %s
    """, (country, as_of))
    snapshot_date = cur.fetchone()["snapshot_date"]
    if not snapshot_date:
        return None
    rows = q(cur, """
        SELECT scheme_name, ticker, signal_tier, first_mention_date, n_docs_total,
               n_last12m, n_prior12m, first_commit_date, n_commit_docs, industry
        FROM mg_policy_company_signals
        WHERE country=%s AND as_of_date=%s
        ORDER BY scheme_name, signal_tier, n_last12m DESC, n_docs_total DESC, ticker
    """, (country, snapshot_date))
    qualified: dict[str, list] = defaultdict(list)
    early: dict[str, list] = defaultdict(list)
    for row in rows:
        # A scheme can be retired/rejected after its historical snapshot.  Its
        # old evidence remains audit data but should not silently join today's
        # live discovery universe.
        if row["scheme_name"] not in INDIA_POLICY_SCHEMES:
            continue
        last, prior = int(row["n_last12m"] or 0), int(row["n_prior12m"] or 0)
        entry = {
            "ticker": (row["ticker"] or "").upper(), "industry": row["industry"],
            "n_docs_total": int(row["n_docs_total"] or 0), "n_last12m": last,
            "n_prior12m": prior,
            "trend": "rising" if last > prior else "fading" if last < prior else "flat",
            "first_mention": row["first_mention_date"],
            "commitment": ({"first_commit": row["first_commit_date"],
                            "n_commit_docs": int(row["n_commit_docs"] or 0)}
                           if row["first_commit_date"] else None),
        }
        (qualified if row["signal_tier"] == "QUALIFIED" else early)[row["scheme_name"]].append(entry)
    return {
        "qualified": dict(qualified), "early_pings": dict(early),
        "provenance": {
            "mode": "dated broad policy discovery snapshot",
            "snapshot_date": snapshot_date,
            "rule": "snapshot date must be on or before report date",
        },
    }


def _candidate_policy_screen(cur, as_of, candidates: list[dict], min_docs: int = 4,
                             top_n: int = 15, country: str = "IN") -> dict:
    """Fast point-in-time policy check for the visible company universe.

    It is a safe fallback when no broad monthly snapshot exists.  It cannot
    discover a company outside ``candidates``; the output says that explicitly
    so a stale/missing snapshot never masquerades as complete policy coverage.
    """
    tickers = sorted({_ticker for candidate in candidates
                      if (_ticker := (candidate.get("ticker") or "").strip().upper())})
    if not tickers:
        return {"qualified": {}, "early_pings": {}, "provenance": {
            "mode": "candidate-scoped fallback", "coverage": "empty visible universe"}}
    commit_verbs = (r"(appl(?:y|ied|ication)|bid|approv\w*|select\w*|sanction\w*|allot\w*|"
                    r"award\w*|letter of (?:intent|award)|\yLoI\y|\yLoA\y|disburs\w*|beneficiar\w*)")
    qualified, early = {}, {}
    for scheme, pattern in INDIA_POLICY_SCHEMES.items():
        # Preserve the scheme-specific source pattern, but evaluate it in
        # PostgreSQL after the indexed, bounded issuer/date filter.  Fetching
        # every filing and re-searching it in Python once per scheme was both
        # slower and needlessly transferred non-matches into the live process.
        matched_rows = q(cur, """
            SELECT UPPER(TRIM(d.ticker)) AS ticker, d.filed_at::date AS filed_at,
                   d.raw_text,
                   CONCAT_WS(' | ', NULLIF(sm.industry_nse,''), NULLIF(sm.industry_bse,'')) AS industry
            FROM mg_documents d
            LEFT JOIN security_master sm ON sm.nse_symbol=d.ticker
            WHERE d.country=%s AND d.ticker=ANY(%s) AND d.filed_at <= %s
              AND d.raw_text ~* %s
        """, (country, tickers, as_of, pattern))
        commit_pattern = (commit_verbs + r"[\s\S]{0,200}(?:" + pattern + r")"
                          r"|(?:" + pattern + r")[\s\S]{0,200}" + commit_verbs)
        commit_re = re.compile(_python_policy_pattern(commit_pattern), flags=re.I)
        matched: dict[str, list[dict]] = defaultdict(list)
        for row in matched_rows:
            matched[row["ticker"]].append(row)
        entries, pings = [], []
        for ticker, matched_docs in matched.items():
            if ticker in MANUAL_EXCLUDE:
                continue
            n_last = sum(doc["filed_at"] > as_of - timedelta(days=365) for doc in matched_docs)
            n_prior = sum(as_of - timedelta(days=730) < doc["filed_at"] <= as_of - timedelta(days=365)
                          for doc in matched_docs)
            committed = [doc for doc in matched_docs if commit_re.search(doc.get("raw_text") or "")]
            entry = {
                "ticker": ticker, "industry": matched_docs[0].get("industry") or None,
                "n_docs_total": len(matched_docs), "n_last12m": n_last, "n_prior12m": n_prior,
                "trend": "rising" if n_last > n_prior else "fading" if n_last < n_prior else "flat",
                "first_mention": min(doc["filed_at"] for doc in matched_docs),
                "commitment": ({"first_commit": min(doc["filed_at"] for doc in committed),
                                "n_commit_docs": len(committed)} if committed else None),
            }
            (entries if entry["n_docs_total"] >= min_docs else pings if n_last >= 1 else []).append(entry)
        entries.sort(key=lambda row: (row["commitment"] is None, -row["n_last12m"], row["ticker"]))
        pings.sort(key=lambda row: (row["first_mention"] is None, str(row["first_mention"])), reverse=True)
        if entries:
            qualified[scheme] = entries[:top_n]
        if pings:
            early[scheme] = pings[:10]
    return {"qualified": qualified, "early_pings": early, "provenance": {
        "mode": "candidate-scoped fallback", "coverage": "visible raw candidate universe only",
        "rule": "run the monthly broad policy snapshot to discover companies outside the raw screen",
    }}


def _policy_company_role_context(cur, as_of: date, policy_screen: dict | None,
                                 country: str = "IN") -> dict[str, dict]:
    """Attach issuer-product proof to policy names outside the mapper universe.

    A broad policy snapshot deliberately discovers companies beyond the raw
    constraint mapper.  Silently dropping those names at presentation time
    reintroduces the precise blind spot the snapshot was built to solve.  This
    function supplies only *research* context from the dated, issuer-owned
    product-role ledger; it neither creates a constraint link nor changes a
    Buy/committee gate.  The automatic extraction's review state remains
    explicit to the analyst.
    """
    tickers = sorted({(entry.get("ticker") or "").strip().upper()
                      for bucket in ("qualified", "early_pings")
                      for entries in ((policy_screen or {}).get(bucket) or {}).values()
                      for entry in (entries or [])
                      if (entry.get("ticker") or "").strip()})
    if not tickers:
        return {}
    try:
        cur.execute("SELECT to_regclass('public.mg_company_product_roles') AS name")
        if not cur.fetchone()["name"]:
            return {}
        rows = q(cur, """
            SELECT DISTINCT ON (ticker, normalized_product, role_type)
                   ticker, company, product_phrase, normalized_product, role_type,
                   role_state, first_evidence_date, last_evidence_date,
                   independent_document_count, physical_evidence_count, review_status
            FROM mg_company_product_roles
            WHERE country=%s AND as_of_date <= %s AND ticker=ANY(%s)
              AND extraction_method=%s AND role_type='MANUFACTURER'
              AND role_state='EVIDENCED' AND physical_evidence_count >= 2
              AND review_status <> 'REJECTED'
            ORDER BY ticker, normalized_product, role_type, as_of_date DESC, id DESC
        """, (country, as_of, tickers, EXTRACTION_METHOD))
    except (psycopg2.Error, KeyError):
        return {}

    by_ticker: dict[str, dict] = {}
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        item = by_ticker.setdefault(ticker, {
            "ticker": ticker, "company": row.get("company") or ticker,
            "issuer_role_evidenced": True,
            "issuer_role_requires_review": row.get("review_status") != "REVIEWED",
            "issuer_role_products": [], "issuer_role_latest_evidence": None,
            "risk_tier": "UNASSESSED", "rank": None, "composite_score": None,
            "order_book": False, "capex_signals": 0, "types": [],
            "corroborated_maker": False, "any_demand_side": False,
        })
        item["issuer_role_products"].append(row.get("product_phrase") or row.get("normalized_product"))
        latest = row.get("last_evidence_date")
        if latest and (item["issuer_role_latest_evidence"] is None or latest > item["issuer_role_latest_evidence"]):
            item["issuer_role_latest_evidence"] = latest
        item["issuer_role_requires_review"] = (
            item["issuer_role_requires_review"] or row.get("review_status") != "REVIEWED"
        )
    return by_ticker


_GENERIC_CHAIN_WORDS = {"critical", "shortage", "demand", "supply", "chain", "constraint",
                        "manufacturing", "contract", "printed", "circuit", "board", "cell",
                        "cable", "steel", "stock", "and", "the", "from"}


def detect_mapping_artifacts(cur, supply_by_chain: dict, as_of) -> list:
    """Mapping-artifact detector (Tier-3, Jul-2026): a company mapped as a
    chain beneficiary whose OWN filings never mention the chain's product in
    24 months is an NLP mapping artifact suspect (the TATAPOWER-in-Battery
    class). Inverse of unmapped_peers. Evidence for judgment — mapped names
    with zero own-filing product mentions should not pass the pure-play leg."""
    out = []
    for chain, members in sorted(supply_by_chain.items(),
                                  key=lambda kv: -len(kv[1] or []))[:10]:
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", chain)
                 if w.lower() not in _GENERIC_CHAIN_WORDS]
        if not words:
            continue
        pattern = "|".join(re.escape(w) for w in words[:3])
        ticks = [(m.get("ticker") or "").upper() for m in (members or [])[:8]
                 if m.get("ticker")]
        if not ticks:
            continue
        rows = q(cur, """
            SELECT ticker, COUNT(*) AS n FROM mg_documents
            WHERE ticker = ANY(%s) AND filed_at BETWEEN %s AND %s
              AND raw_text ~* %s
            GROUP BY ticker
        """, (ticks, as_of - timedelta(days=730), as_of, pattern))
        mentioned = {(r["ticker"] or "").upper() for r in rows}
        zero_hit = [t for t in ticks if t not in mentioned]
        if zero_hit:
            out.append({"chain": chain, "product_terms": words[:3],
                        "artifact_suspects": zero_hit,
                        "n_checked": len(ticks),
                        "note": "mapped as beneficiary but own filings never mention the product in 24m"})
    return out


def compute_orderbook_amounts(cur, as_of, tickers: list[str], lookback_days: int = 540) -> dict:
    """Quantified order books (Tier-2, Jul-2026): parse rupee amounts near
    'order book' / 'order inflow' phrasing in candidates' own filings —
    turns the boolean order_book flag into actual sizes and their trend
    (the GENUSPOWER '12x cover' class of evidence, systematized).
    Returns {ticker: {latest_cr, latest_date, prev_cr, trend_pct, n_readings}}."""
    if not tickers:
        return {}
    rows = q(cur, r"""
        SELECT ticker, filed_at::date AS d,
               regexp_matches(raw_text,
                 '(?i)(?:order\s*book|order\s*inflow|unexecuted\s*order|order\s*backlog)[^.]{0,80}?(?:Rs\.?|INR|₹)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crore|cr\y|lakh|billion|bn\y|million|mn\y)',
                 'g') AS m
        FROM mg_documents
        WHERE ticker = ANY(%s) AND country = 'IN'
          AND filed_at BETWEEN %s AND %s
          AND raw_text ~* '(order\s*book|order\s*inflow|unexecuted\s*order|order\s*backlog)'
    """, (tickers, as_of - timedelta(days=lookback_days), as_of))
    unit_to_cr = {"crore": 1.0, "cr": 1.0, "lakh": 0.01, "billion": 100.0,
                  "bn": 100.0, "million": 0.1, "mn": 0.1}
    readings: dict[str, list] = {}
    for r in rows:
        try:
            amt = float(r["m"][0].replace(",", ""))
            cr = amt * unit_to_cr.get(r["m"][1].lower().strip(), 1.0)
        except (ValueError, IndexError):
            continue
        if 1 <= cr <= 1_000_000:   # sanity: ₹1cr .. ₹10 lakh cr
            readings.setdefault((r["ticker"] or "").upper(), []).append((r["d"], cr))
    out = {}
    for tick, lst in readings.items():
        lst.sort()
        latest_d, latest_cr = lst[-1]
        # prior reading >= 90 days earlier, for a trend
        prior = [(d, c) for d, c in lst if (latest_d - d).days >= 90]
        prev_cr = prior[-1][1] if prior else None
        out[tick] = {
            "latest_cr": round(latest_cr, 0), "latest_date": latest_d,
            "prev_cr": round(prev_cr, 0) if prev_cr else None,
            "trend_pct": round((latest_cr - prev_cr) / prev_cr * 100, 1) if prev_cr else None,
            "n_readings": len(lst),
        }
    return out


def compute_pli_shortlist(policy_screen: dict) -> list:
    """Deterministic, judgment-free PLI-family beneficiary shortlist -- a
    mechanical screen, NOT a judgment-reviewed buy list.

    No hardcoded scheme names or sector lists (user rule, Jul-2026): the
    PLI-family scheme set comes from mg_tracked_schemes (scheme_class=
    'pli_family'), and instead of sector include/exclude lists the filter is
    pure filing evidence -- the company's OWN disclosure must contain
    commitment-stage language ("applied for / approved under ..."). That is
    what separates a beneficiary from an administrator/financier/EPC bystander
    (the IFCI lesson: a scheme's monitoring agency discusses it constantly but
    never files an application under it).
    """
    # specific tranches (Solar PV PLI, White Goods PLI...) before generic "PLI"
    # so the dedupe keeps the sector-resolved scheme name for each ticker
    pli_family = sorted(
        [name for name, meta in TRACKED_SCHEME_META.items()
         if meta.get("scheme_class") in ("pli_family", "us_incentive")],
        key=lambda n: (n == "PLI", n))
    out, seen = [], set()
    for scheme in pli_family:
        qualified = policy_screen.get("qualified", {}).get(scheme, [])
        early = policy_screen.get("early_pings", {}).get(scheme, [])
        for entry, tier in [(e, "qualified") for e in qualified] + [(e, "early_ping") for e in early]:
            committed = bool(entry.get("commitment"))
            if not committed:
                # recall widened (Jul-2026, the solar lesson): a RISING
                # uncommitted citer with real intensity belongs on the radar —
                # labeled distinctly so committed names stay visually senior.
                # (Administrator/financier noise self-filters: those cite at
                # low, fading intensity.)
                if entry.get("trend") != "rising" or (entry.get("n_last12m") or 0) < 3:
                    continue
            t = entry["ticker"]
            if t in seen:
                continue
            seen.add(t)
            out.append({
                "ticker": t,
                "scheme": scheme,
                "industry": entry.get("industry"),
                "tier": tier if committed else "citing-rising (uncommitted)",
                "trend": entry.get("trend"),
                "n_docs_total": entry.get("n_docs_total"),
                "first_mention": entry.get("first_mention"),
                "committed": committed,
            })
    # committed first, then rising trend, then doc count
    out.sort(key=lambda x: (not x["committed"], x["trend"] != "rising",
                             -(x["n_docs_total"] or 0)))
    return out


# PLI is a policy-discovery input, not an investment conclusion.  Keep the
# broad, backwards-compatible phrase screen above for research scorecards, but
# route the client-facing report through the evidence pipeline below.  It
# requires a dated company disclosure, classifies the disclosed action, and
# makes the missing research leg explicit instead of using a green
# "committed" badge for every PLI mention.
_PLI_STAGE_RULES: tuple[tuple[str, int, str], ...] = (
    ("operating / commissioned", 4,
     r"commercial(?:ly)? (?:operat|produc)|commenc\w{0,18}(?:production|operations)|"
     r"commission\w*|operationali[sz]\w*"),
    ("capacity / capex committed", 3,
     r"(?:set(?:ting)? up|establish\w*|invest\w*|capex|capital expenditure|"
     r"manufactur\w* facility|production line|capacity of|expand\w* capacity)"),
    ("awarded / approved", 2,
     r"(?:approv\w*|select\w*|award\w*|sanction\w*|allot\w*|letter of (?:intent|award)|"
     r"\bLoI\b|\bLoA\b|beneficiar\w*)"),
    ("application / claimant", 1,
     r"(?:appl(?:y|ied|ication)|bid\w*|submit\w* (?:an )?(?:application|bid))"),
)
_PLI_MATERIALITY_RE = re.compile(
    r"(?i)(?:₹|rs\.?|inr)?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*"
    r"(?:gw|mw|gwh|mwh|mtpa|mpta|tonnes?|crore|cr\.?|lakh crore|billion|bn)"
)


def _pli_stage(text: str, scheme_match: re.Match | None = None) -> tuple[str, int, re.Match | None]:
    """Return the highest policy/action stage stated next to a scheme reference.

    A filing can discuss an unrelated plant commissioning or general operating
    performance on another page.  An action counts only when it occurs in the
    same local disclosure context as the scheme match.
    """
    for label, rank, pattern in _PLI_STAGE_RULES:
        matches = list(re.finditer(pattern, text, flags=re.I))
        if scheme_match:
            matches = [m for m in matches
                       if max(0, max(m.start(), scheme_match.start()) - min(m.end(), scheme_match.end())) <= 500]
        if matches:
            return label, rank, matches[0]
    return "filing mention only", 0, None


def _python_policy_pattern(pattern: str) -> str:
    """Translate the tracked-scheme PostgreSQL word boundary for Python ``re``.

    The database correctly uses ``\\y`` in its POSIX regexes.  Evidence parsing
    happens in Python after the query, where the equivalent boundary is ``\\b``.
    Keeping the translation here lets the DB remain the authority for scheme
    patterns without silently dropping every such filing from the audit trail.
    """
    return (pattern or "").replace(r"\y", r"\b")


def _pli_excerpt(text: str, anchors: list[re.Match | None], width: int = 420) -> str:
    """A compact, auditable filing passage around the policy/action evidence."""
    anchor = next((m for m in anchors if m is not None), None)
    if anchor is None:
        return ""
    start = max(0, anchor.start() - 160)
    end = min(len(text), anchor.end() + width - 160)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _pli_local_context(text: str, anchors: list[re.Match | None]) -> str:
    """Keep a policy claim tied to the same local disclosure as its action."""
    active = [m for m in anchors if m is not None]
    if not active:
        return ""
    left = max(0, min(m.start() for m in active) - 320)
    right = min(len(text), max(m.end() for m in active) + 520)
    return text[left:right]


def _pli_constraint_links(evidence_text: str, greatest_constraints: list[dict] | None,
                          scheme_match: re.Match | None, stage_match: re.Match | None) -> list[str]:
    """Link an evidenced policy action to *selected* physical constraints only.

    The exact product phrase must occur in the same local disclosure as both
    the policy reference and the company action.  A PLI reference on one PDF
    page and "solar" or "PCB" elsewhere in the filing is not a crosswalk.
    """
    blob = _pli_local_context(evidence_text or "", [scheme_match, stage_match])
    links: list[str] = []
    for row in greatest_constraints or []:
        product = row.get("constraint") or row.get("constrained_product") or ""
        # Reuse the product taxonomy's precise multi-word phrases/acronyms,
        # rather than treating the common word "cell" as a policy link.
        terms = [term for term in candidate_terms(product)
                 if len(term.split()) > 1 or term.isupper()]
        if not terms:
            continue
        linked = any(re.search(term_regex(term).replace(r"\y", r"\b"), blob, re.I)
                     for term in terms)
        if linked:
            links.append(product)
    return links


def _pli_materiality(text: str, anchor: re.Match | None) -> tuple[str | None, str | None]:
    """Return the nearest disclosed capacity/capex magnitude, if any.

    This is evidence display—not a valuation model—so an unqualified rupee or
    capacity amount is never converted into a score.  It simply lets the
    reader distinguish a real physical commitment from a generic policy
    reference.
    """
    if not text:
        return None, None
    where = anchor.start() if anchor else 0
    window = text[max(0, where - 320): min(len(text), where + 520)]
    match = _PLI_MATERIALITY_RE.search(window)
    if not match:
        return None, None
    materiality = re.sub(r"\s+", " ", match.group(0)).strip()
    near = window[max(0, match.start() - 100): min(len(window), match.end() + 140)]
    if re.search(r"\b(?:gw|mw|gwh|mwh|mtpa|mpta|tonnes?|tpd)\b", materiality, re.I):
        kind = "product capacity"
    elif re.search(r"scheme outlay|budget(?:ary)? allocation|government (?:outlay|allocation)|total (?:outlay|investment)", near, re.I):
        kind = "scheme/aggregate amount — not company capex"
    elif re.search(r"\b(?:capex|capital expenditure|invest(?:ment|ed)?|project cost|plant cost)\b", near, re.I):
        kind = "company/project capex"
    else:
        kind = "amount context unqualified"
    return materiality, kind


def compute_pli_pipeline(cur, as_of: date, policy_screen: dict,
                         greatest_constraints: list[dict] | None = None,
                         country: str = "IN") -> dict:
    """Turn the raw PLI phrase screen into a point-in-time research pipeline.

    All source evidence comes from the company's own filing on or before
    ``as_of``.  The function deliberately returns *routes* (research now,
    monitor a named milestone, or archive) rather than Buy-style tiers.  This
    makes policy participation useful without allowing document repetition or
    a generic scheme claim to contaminate the selection decision.
    """
    raw = compute_pli_shortlist(policy_screen)
    if not raw:
        return {
            "as_of_date": as_of.isoformat(), "research_leads": [],
            "monitor_leads": [], "archive": [], "raw_discovery_count": 0,
            "archive_count": 0,
        }

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for entry in raw:
        scheme = entry.get("scheme") or ""
        pattern = INDIA_POLICY_SCHEMES.get(scheme)
        if pattern:
            by_ticker[(entry.get("ticker") or "").upper()].append(entry)

    tickers = sorted(t for t in by_ticker if t)
    docs_by_ticker: dict[str, list[dict]] = defaultdict(list)
    if tickers:
        # The scheme/product matching below is already performed in Python for
        # each ticker and scheme.  Applying every broad policy regex inside the
        # database here can force a full filing-corpus scan before the ticker
        # filter is applied.  Fetch the small, named company universe first,
        # then retain the same exact local evidence test below.
        docs = q(cur, """
            SELECT d.id, UPPER(TRIM(d.ticker)) AS ticker, d.filed_at::date AS filed_at,
                   d.title, d.url, d.raw_text
            FROM mg_documents d
            WHERE d.country = %s AND UPPER(TRIM(d.ticker)) = ANY(%s)
              AND d.filed_at <= %s
            ORDER BY d.filed_at DESC, d.id DESC
        """, (country, tickers, as_of))
        for doc in docs:
            docs_by_ticker[doc["ticker"]].append(doc)

    research, monitor, archive = [], [], []
    for entry in raw:
        ticker = (entry.get("ticker") or "").upper()
        scheme = entry.get("scheme") or "PLI"
        scheme_pattern = INDIA_POLICY_SCHEMES.get(scheme)
        if not ticker or not scheme_pattern:
            continue

        relevant = []
        for doc in docs_by_ticker.get(ticker, []):
            text = doc.get("raw_text") or ""
            try:
                scheme_match = re.search(_python_policy_pattern(scheme_pattern), text, flags=re.I)
            except re.error:
                scheme_match = None
            if not scheme_match:
                continue
            stage, stage_rank, stage_match = _pli_stage(text, scheme_match)
            # Duplicate press releases and boilerplate can look like many
            # independent confirmations.  A normalized excerpt identifies
            # repetitive evidence families; only one representative survives.
            excerpt = _pli_excerpt(text, [stage_match, scheme_match])
            fingerprint = re.sub(r"[^a-z0-9]+", "", excerpt.lower())[:260]
            materiality, materiality_kind = _pli_materiality(text, stage_match)
            relevant.append({
                "filed_at": doc.get("filed_at"), "title": doc.get("title") or "Company filing",
                "url": doc.get("url"), "stage": stage, "stage_rank": stage_rank,
                "excerpt": excerpt, "materiality": materiality,
                "materiality_kind": materiality_kind,
                "fingerprint": fingerprint,
                "constraint_links": _pli_constraint_links(
                    text, greatest_constraints, scheme_match, stage_match
                ),
            })

        # Keep one best representative from each distinct evidence family.
        independent, seen_fingerprints = [], set()
        for item in sorted(relevant, key=lambda x: (x["stage_rank"], x["filed_at"] or date.min), reverse=True):
            key = item["fingerprint"] or f"{item['filed_at']}:{item['title']}"
            if key in seen_fingerprints:
                continue
            seen_fingerprints.add(key)
            independent.append(item)
        best = independent[0] if independent else None
        links = best.get("constraint_links", []) if best else []
        stage_rank = best.get("stage_rank", 0) if best else 0
        materiality = best.get("materiality") if best else None
        materiality_kind = best.get("materiality_kind") if best else None
        direct_company_materiality = materiality_kind in {"product capacity", "company/project capex"}
        independent_dates = {item.get("filed_at") for item in independent if item.get("filed_at")}
        # One attractive company presentation is a lead, not confirmation.
        # Promotion requires two distinct dated evidence families and a local,
        # exact product-policy-capex link. Repeated press releases do not help.
        if (stage_rank >= 3 and links and materiality and direct_company_materiality
                and len(independent_dates) >= 2):
            route = "Research now"
            route_reason = "two independent product-linked capacity/capex disclosures tied to a selected physical constraint"
        elif stage_rank >= 3 and links and materiality and direct_company_materiality:
            route = "Monitor milestone"
            route_reason = "one product-linked capacity/capex disclosure; require a second independent event"
        elif stage_rank >= 2 and links:
            route = "Monitor milestone"
            route_reason = "local policy/product link; verify award terms, capacity, and commissioning"
        elif stage_rank >= 2:
            route = "New-theme monitor"
            route_reason = "real policy action, but no selected physical-constraint match yet"
        elif stage_rank == 1:
            route = "Monitor milestone"
            route_reason = "application/claim only; require award, capex, or commissioning"
        else:
            route = "Archive"
            route_reason = "scheme mention without a company-specific policy action"

        item = {
            "ticker": ticker,
            "scheme": scheme,
            "industry": entry.get("industry"),
            "policy_stage": best.get("stage") if best else "filing mention only",
            "evidence_date": best.get("filed_at") if best else None,
            "evidence_title": best.get("title") if best else None,
            "evidence_url": best.get("url") if best else None,
            "evidence_excerpt": best.get("excerpt") if best else None,
            "materiality": materiality,
            "materiality_kind": materiality_kind,
            "constraint_links": links,
            "research_route": route,
            "route_reason": route_reason,
            "independent_evidence_count": len(independent),
            "independent_evidence_dates": sorted(independent_dates),
            "repetitive_evidence_vetoed": max(0, len(relevant) - len(independent)),
            "source_rule": "company filing dated on or before report date",
        }
        if route == "Research now":
            research.append(item)
        elif route == "Archive":
            archive.append(item)
        else:
            monitor.append(item)

    # Routes decide visibility.  Within a route, direct constraint linkage,
    # stronger disclosed action, and an independent evidence family lead—not
    # the number of times a company repeated the word PLI.
    sort_key = lambda x: (-len(x["constraint_links"]),
                           -({"operating / commissioned": 4,
                               "capacity / capex committed": 3,
                               "awarded / approved": 2,
                               "application / claimant": 1}.get(x["policy_stage"], 0)),
                           -(x["independent_evidence_count"] or 0), x["ticker"])
    research.sort(key=sort_key)
    monitor.sort(key=sort_key)
    archive.sort(key=sort_key)
    return {
        "as_of_date": as_of.isoformat(),
        "research_leads": research[:12],
        "monitor_leads": monitor[:15],
        "archive": archive,
        "raw_discovery_count": len(raw),
        "archive_count": len(archive),
        "methodology": (
            "Own filings only; classify the strongest as-of policy action, deduplicate repetitive "
            "evidence families, require an exact product phrase in the same local policy/action disclosure, "
            "and distinguish company capacity/capex from scheme-wide amounts. Research-now requires two "
            "independent dated disclosures. This is a research workflow, never a Buy screen."
        ),
    }


def compute_chain_capex_momentum(cur, as_of) -> dict:
    """T-COIL evidence: per chain, aggregate capex_increase signals in the latest
    snapshot <= as_of vs the snapshot ~1 year earlier. Rising capex = entry signal
    for differentiated chains; FALLING capex = the coiling signal for commodity
    chains (supply discipline creates tomorrow's squeeze). Evidence field only."""
    rows = q(cur, """
        WITH snaps AS (
            SELECT MAX(as_of_date) FILTER (WHERE as_of_date <= %s)                 AS cur_snap,
                   MAX(as_of_date) FILTER (WHERE as_of_date <= %s::date - 300)     AS prior_snap
            FROM mg_india_beneficiaries
        )
        SELECT b.constrained_product,
               b.as_of_date = s.cur_snap AS is_cur,
               SUM(COALESCE(NULLIF(substring(b.rationale FROM '(\\d+) capex_increase signals'), ''), '0')::int) AS capex
        FROM mg_india_beneficiaries b, snaps s
        WHERE b.constrained_product IS NOT NULL
          AND b.as_of_date IN (s.cur_snap, s.prior_snap)
        GROUP BY 1, 2
    """, (as_of, as_of))
    agg: dict[str, dict] = {}
    for r in rows:
        e = agg.setdefault(r["constrained_product"], {"capex_now": 0, "capex_prior": 0})
        e["capex_now" if r["is_cur"] else "capex_prior"] += int(r["capex"] or 0)
    for chain, e in agg.items():
        e["momentum"] = e["capex_now"] - e["capex_prior"]
        lt = chain_layer_type(chain)
        e["layer_type"] = lt
        if lt == "commodity" and e["momentum"] < 0:
            e["signal"] = "COILING — commodity layer cutting capex; watch for demand catalyst (trade setup, not compounder)"
        elif lt == "differentiated" and e["momentum"] > 0:
            e["signal"] = "CAPEX RISING into differentiated constraint — compounder entry signal"
        else:
            e["signal"] = None
    return agg


def detect_chain_continuity_alerts(cur, as_of, window_months: int = 12) -> list:
    """Coverage alarm: chains that had beneficiary rows in older snapshots but have
    NO fresh mapping in the scan window, or whose latest mapping is stale.

    The solar lesson: solar chains had rows for 2020-22 snapshots, none for
    2023-25 — the mapper silently skipped them and the +579% 2023 solar year
    happened in a blind spot. Transformer/rolling-stock rows stopped after
    Dec-2025 the same way. A vanished chain is a pipeline failure to
    investigate, never evidence the constraint resolved."""
    rows = q(cur, """
        SELECT constrained_product,
               MAX(as_of_date) AS last_mapped,
               MAX(as_of_date) FILTER (WHERE as_of_date <= %s) AS last_mapped_pit
        FROM mg_india_beneficiaries
        WHERE constrained_product IS NOT NULL
        GROUP BY 1
    """, (as_of,))
    alerts = []
    for r in rows:
        lm = r["last_mapped_pit"]
        if lm is None:
            continue
        age_days = (as_of - lm).days
        if age_days > 150:   # no fresh mapping in ~2 quarters
            alerts.append({
                "chain": r["constrained_product"],
                "last_mapped": lm,
                "staleness_days": age_days,
                "alert": ("chain vanished from recent mapper snapshots — pipeline gap, "
                          "NOT evidence of resolution; verify the constraint manually"),
            })
    alerts.sort(key=lambda a: -a["staleness_days"])
    return alerts


def detect_chain_ticker_overlap(cur, as_of, overlap_threshold: float = 0.7) -> list:
    """De-dup detector — the 'electrical equipment' == 'transformer' lesson.

    Jul-2026 cohort-explosiveness study found EMS/PCB/Semiconductor IC sharing
    100% identical tickers in the same snapshot, and (via mg_theme_beneficiaries)
    'electrical equipment: Demand Surge' sharing its whole cohort with
    'transformer: Demand-Supply Tension' — two NLP labels for one constraint.
    Treating each as independent confirmation double-counts one signal.

    Flags any pair of constrained_product chains at the current snapshot whose
    ticker sets overlap >= overlap_threshold. Evidence only — the judgment layer
    must merge such pairs before counting them as separate confirmations."""
    rows = q(cur, """
        SELECT constrained_product, ARRAY_AGG(DISTINCT UPPER(TRIM(ticker))) AS ticks
        FROM mg_india_beneficiaries
        WHERE as_of_date = (SELECT MAX(as_of_date) FROM mg_india_beneficiaries WHERE as_of_date <= %s)
          AND constrained_product IS NOT NULL AND ticker IS NOT NULL
        GROUP BY constrained_product
    """, (as_of,))
    chains = [(r["constrained_product"], set(r["ticks"])) for r in rows]
    dupes = []
    for i in range(len(chains)):
        for j in range(i + 1, len(chains)):
            n1, s1 = chains[i]
            n2, s2 = chains[j]
            if not s1 or not s2:
                continue
            inter = len(s1 & s2)
            overlap = inter / min(len(s1), len(s2))
            if overlap >= overlap_threshold:
                dupes.append({
                    "chain_a": n1, "chain_b": n2,
                    "overlap_pct": round(overlap * 100),
                    "n_a": len(s1), "n_b": len(s2), "n_shared": inter,
                    "note": "same underlying cohort under two labels — merge before counting as independent evidence",
                })
    return dupes


def compute_chain_cohort_explosiveness(cur, as_of) -> dict:
    """Multi-vintage cohort durability test (Jul-2026 explosiveness study).

    A constraint that lifts its WHOLE beneficiary cohort at MULTIPLE separate
    entry vintages (not one lottery winner, not one lucky year) is structurally
    different from a decaying-consensus theme or a commodity round-trip. Only
    CRGO Steel/Transformer passed this test across 2020/2021/2022 entries
    (median 2yr +79% to +136% every vintage; Defense electronics decayed
    57%->12%->4%; Solar's 3yr number collapsed 64%->18% between vintages).

    Strictly point-in-time: only counts a vintage snapshot if snapshot_date +
    730 days <= as_of (the 2-year outcome must have already happened by the
    scan date). Evidence for the judgment layer only — never a score input,
    and never proof by itself (small-n cohorts are noisy)."""
    cur.execute("SELECT DISTINCT as_of_date FROM mg_india_beneficiaries WHERE constrained_product IS NOT NULL")
    all_snaps = sorted(r["as_of_date"] for r in cur.fetchall())
    usable_snaps = [s for s in all_snaps if (as_of - s).days >= 730]
    if not usable_snaps:
        return {}

    out: dict[str, dict] = {}
    for snap in usable_snaps:
        rows = q(cur, """
            SELECT constrained_product, ARRAY_AGG(DISTINCT UPPER(TRIM(ticker))) AS ticks
            FROM mg_india_beneficiaries
            WHERE as_of_date = %s AND constrained_product IS NOT NULL AND ticker IS NOT NULL
            GROUP BY constrained_product
        """, (snap,))
        for r in rows:
            chain = r["constrained_product"]
            tickers = [t for t in r["ticks"] if t]
            if len(tickers) < 8:
                continue
            rets = []
            for t in tickers:
                row = None
                for sym in price_symbols(t):   # alias-aware (HBLENGINE lesson)
                    cur.execute("""
                        WITH e AS (SELECT close FROM nse_bhavcopy_data WHERE symbol=%s
                                   AND trade_date BETWEEN %s::date AND %s::date+10 ORDER BY trade_date LIMIT 1),
                             x AS (SELECT close FROM nse_bhavcopy_data WHERE symbol=%s
                                   AND trade_date <= %s::date+730 ORDER BY trade_date DESC LIMIT 1)
                        SELECT (x.close-e.close)/e.close*100 AS ret FROM e,x WHERE e.close>0
                    """, (sym, snap, snap, sym, snap))
                    row = cur.fetchone()
                    if row and row["ret"] is not None:
                        break
                if row and row["ret"] is not None:
                    rets.append(float(row["ret"]))
            if len(rets) < 8:
                continue
            rets.sort()
            n = len(rets)
            median = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
            pct_gt_100 = round(sum(1 for x in rets if x > 100) / n * 100)
            entry = out.setdefault(chain, {"vintages": []})
            entry["vintages"].append({
                "entry_date": str(snap), "n": n,
                "median_2y_return_pct": round(median, 1),
                "pct_cohort_gt_100pct": pct_gt_100,
            })

    for chain, entry in out.items():
        vintages = entry["vintages"]
        n_broad_wins = sum(1 for v in vintages if v["median_2y_return_pct"] > 50 and v["pct_cohort_gt_100pct"] >= 40)
        entry["n_vintages_tested"] = len(vintages)
        entry["n_broad_cohort_wins"] = n_broad_wins
        entry["durable"] = len(vintages) >= 2 and n_broad_wins == len(vintages)
        if len(vintages) < 2:
            entry["durability_status"] = "insufficient_history"
            entry["note"] = ("insufficient historical vintages for a durability conclusion — "
                             "not evidence that the chain failed or decayed")
        elif entry["durable"]:
            entry["durability_status"] = "passed"
            entry["note"] = ("PASSED durability test: every tested entry vintage produced a broad cohort win — "
                             "the CRGO/transformer fingerprint")
        else:
            entry["durability_status"] = "failed"
            entry["note"] = ("did not repeat a broad cohort win across all tested vintages — "
                             "treat historical persistence as unproven")
    return out


def identify_narrow_fresh_constraints(major_themes: list, emerging_themes: list,
                                       breadth_ceiling: int = 60, max_quarters: int = 10) -> list:
    """Forward-discovery candidates: themes narrow + fresh enough to resemble
    what CRGO Steel looked like in Dec-2020 (23-28 mapped companies, early
    stage) BEFORE anyone had run a cohort study to notice.

    Pure derivation from theme data already fetched this scan — no new query.
    Purely a discovery/manual-mapping-priority list, never a score input; a
    theme still needs the constraint-quality grade (real vs artifact,
    quantified gap, layer type) before it's investable."""
    seen, out = set(), []
    for t in (major_themes or []) + (emerging_themes or []):
        name = t.get("theme_name")
        cc = t.get("company_count")
        q_ = t.get("confirmed_quarters")
        if not name or name in seen or cc is None:
            continue
        seen.add(name)
        if cc <= breadth_ceiling and (q_ is None or q_ <= max_quarters) and \
           (t.get("stage_label") or "") in ("Emerging", "Accelerating", "Hidden Formation"):
            out.append({
                "theme_name": name, "company_count": cc,
                "stage_label": t.get("stage_label"), "confirmed_quarters": q_,
                "first_detected": t.get("first_detected"),
                "note": "narrow + fresh — manual-mapping priority; grade the constraint before treating as investable",
            })
    out.sort(key=lambda x: x["company_count"])
    return out


# ── Coverage-gap detector: term derivation per chain ─────────────────────────
# Word-boundary regex per constrained-product chain; chains without an override
# fall back to the full chain name as a case-insensitive phrase. Kept generic —
# this maps NAMES to search terms, it encodes no sector opinion.
PEER_TERM_OVERRIDES: dict[str, str] = {
    "power transformer":  r"\ytransformers?\y",
    "crgo steel":         r"\yCRGO\y",
    "solar cell":         r"solar cells?",
    "solar module":       r"solar modules?",
    "solar wafer":        r"solar wafers?",
    "battery cell":       r"battery cells?|li-?ion cell",
    "ems / contract manufacturing": r"electronic manufacturing services|\yEMS\y",
    "pcb / printed circuit board":  r"printed circuit",
    "optical fiber cable": r"optical fib",
    "defense electronics": r"defen[cs]e electronics",
    "rolling stock / locomotives": r"rolling stock|locomotives?",
    "semiconductor ic":    r"semiconductor",
}


def _peer_term(chain_name: str) -> str:
    key = (chain_name or "").strip().lower()
    for k, pattern in PEER_TERM_OVERRIDES.items():
        if k in key or key in k:
            return pattern
    return key  # fallback: the chain name itself as a phrase


def detect_unmapped_peers(cur, supply_by_product, as_of,
                          lookback_days: int = 730, min_docs: int = 5,
                          top_n: int = 8) -> dict:
    """Coverage-gap detector: listed companies whose OWN filings repeatedly
    mention a constrained product but that the NLP beneficiary mapper never
    linked to the chain (the Voltamp/TARIL failure mode — pure-plays whose
    dedicated filings never co-occur with theme documents).

    Purely an evidence field: these names are NOT scored or ranked; the
    judgment layer must review them per selected theme. Point-in-time
    (filed_at <= as_of). Companies with a known-incompatible industry_nse
    are dropped; unknown industry passes through (master data is sparse).
    """
    from_date = as_of - timedelta(days=lookback_days)
    out: dict[str, list] = {}
    seen_terms: dict[str, list] = {}
    for chain, companies in supply_by_product.items():
        term = _peer_term(chain)
        mapped = {(c.get("ticker") or "").strip().upper() for c in companies}
        if term in seen_terms:
            rows = seen_terms[term]
        else:
            rows = q(cur, """
                SELECT d.ticker, COUNT(*) AS n, MAX(d.filed_at) AS latest,
                       MAX(sm.company_name) AS company,
                       MAX(CONCAT_WS(' / ', NULLIF(sm.industry_nse,''), NULLIF(sm.industry_bse,''))) AS industry
                FROM mg_documents d
                LEFT JOIN security_master sm ON sm.nse_symbol = d.ticker
                WHERE d.country = 'IN'
                  AND d.ticker IS NOT NULL
                  AND d.filed_at BETWEEN %s AND %s
                  AND d.raw_text ~* %s
                GROUP BY d.ticker
                HAVING COUNT(*) >= %s
                ORDER BY COUNT(*) DESC
                LIMIT 40
            """, (from_date, as_of, term, min_docs))
            seen_terms[term] = rows
        peers = []
        for r in rows:
            tick = (r["ticker"] or "").strip().upper()
            if not tick or tick in mapped or tick in MANUAL_EXCLUDE:
                continue
            if _sector_check(r["industry"], chain) is False:
                continue   # known-incompatible sector; unknown passes
            peers.append({
                "ticker": tick,
                "company": r["company"],
                "industry": r["industry"] or None,
                "industry_match": _sector_check(r["industry"], chain) is True,
                "n_filings_mentioning": int(r["n"]),
                "latest_mention": r["latest"],
                "note": "unmapped by beneficiary extractor — review in judgment layer",
            })
            if len(peers) >= top_n:
                break
        # industry-corroborated pure-plays first, then by filings intensity
        peers.sort(key=lambda p: (not p["industry_match"], -p["n_filings_mentioning"]))
        if peers:
            out[chain] = peers
    return out


def compute_chain_technical_state(cur, supply_by_product, as_of) -> dict:
    """Chain-level technical state: is a chain's mapped cohort extended or
    de-rated as of the scan date?

    POSITION-SIZING CONTEXT ONLY — never selection. Per the user's standing
    rule (and the backtest: above-200DMA showed no alpha edge), price action
    must not change a grade, verdict, or category. This field only informs
    sizing/entry notes on already-selected names: DERATED cohort = full-size
    entries available; EXTENDED_CROWDED = stagger/half-size.
    Labels: DERATED (<=45% above 200DMA and median <= -25% from 52w high),
    EXTENDED_CROWDED (>=70% above and median > -12%), else MIXED.
    """
    all_ticks = sorted({(c.get("ticker") or "").strip().upper()
                        for lst in supply_by_product.values() for c in lst
                        if c.get("ticker") and not c.get("sector_mismatch")
                        and not c.get("demand_side_flag")})
    # Alias-aware: renamed symbols' history lives under old tickers (HBLENGINE lesson)
    alias_of = {}
    expanded = []
    for t in all_ticks:
        for s_ in price_symbols(t):
            expanded.append(s_)
            alias_of[s_] = t
    all_ticks = sorted(set(expanded))
    if not all_ticks:
        return {}
    rows = q(cur, """
        SELECT sym,
               (SELECT close FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 1) AS last,
               (SELECT AVG(close) FROM (
                    SELECT close FROM nse_bhavcopy_data
                    WHERE symbol = sym AND trade_date <= %s
                    ORDER BY trade_date DESC LIMIT 200) a) AS dma200,
               (SELECT MAX(close) FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date BETWEEN %s::date - 365 AND %s) AS high52
        FROM UNNEST(%s::text[]) AS sym
    """, (as_of, as_of, as_of, as_of, all_ticks))
    tech = {}
    for r in rows:
        if r["last"] and r["dma200"] and r["high52"]:
            last = float(r["last"])
            tech[alias_of.get(r["sym"], r["sym"])] = {
                "above_200dma": last > float(r["dma200"]),
                "pct_from_52w_high": round((last - float(r["high52"])) / float(r["high52"]) * 100, 1),
            }

    out = {}
    for chain, companies in supply_by_product.items():
        ticks = [(c.get("ticker") or "").strip().upper() for c in companies
                 if c.get("ticker") and not c.get("sector_mismatch")
                 and not c.get("demand_side_flag")]
        pts = [tech[t] for t in ticks if t in tech]
        if len(pts) < 3:
            continue
        n = len(pts)
        pct_above = round(sum(1 for p in pts if p["above_200dma"]) / n * 100)
        offs = sorted(p["pct_from_52w_high"] for p in pts)
        med = offs[n // 2] if n % 2 else (offs[n // 2 - 1] + offs[n // 2]) / 2
        if pct_above <= 45 and med <= -25:
            label = "DERATED"
            note = "sizing context only: cohort de-rated — full-size entries available on evidence-selected names"
        elif pct_above >= 70 and med > -12:
            label = "EXTENDED_CROWDED"
            note = "sizing context only: cohort near highs — stagger/half-size entries on evidence-selected names"
        else:
            label = "MIXED"
            note = "sizing context only: no cohort-level signal"
        out[chain] = {
            "n_with_price": n,
            "pct_above_200dma": pct_above,
            "median_pct_from_52w_high": round(med, 1),
            "label": label,
            "note": note,
        }
    return out


def fetch_risk_flags(cur, tickers: list[str], as_of, lookback_days: int = 730) -> dict:
    """Lesson 4: downside-risk overlay from mg_documents filing_type field.

    2020-2024 backtest showed ALL severe losses (>30%) involved companies with
    high fundamental scores but no governance/risk filter — GENSOL (0.850→-95%),
    TATASTEEL (0.839→-90%), SIEMENS (0.837→-53%).

    HIGH: fraud, IBC commencement, suspension, liquidation (consider excluding).
    ELEVATED: auditor resignation, payment default, delayed results (size down).
    NORMAL: no red-flag filings in lookback window.
    """
    if not tickers:
        return {}
    from_date = as_of - timedelta(days=lookback_days)
    rows = q(cur, """
        SELECT ticker, filing_type, title, filed_at
        FROM mg_documents
        WHERE ticker = ANY(%s)
          AND filed_at BETWEEN %s AND %s
          AND filing_type IS NOT NULL
        ORDER BY ticker, filed_at DESC
    """, (tickers, from_date, as_of))

    result: dict[str, dict] = {}
    for r in rows:
        tick = (r["ticker"] or "").strip().upper()
        if not tick:
            continue
        ft = r["filing_type"] or ""
        entry = result.setdefault(tick, {"risk_tier": "NORMAL", "risk_events": []})
        if ft in HIGH_RISK_FILING_TYPES:
            entry["risk_tier"] = "HIGH"
            entry["risk_events"].append({
                "type": ft, "date": r["filed_at"], "title": (r["title"] or "")[:80]
            })
        elif ft in ELEVATED_RISK_FILING_TYPES:
            if entry["risk_tier"] == "NORMAL":
                entry["risk_tier"] = "ELEVATED"
            entry["risk_events"].append({
                "type": ft, "date": r["filed_at"], "title": (r["title"] or "")[:80]
            })

    # TEXT-pattern pass (the GENSOL lesson): severe governance language in the
    # title or opening body, regardless of exchange filing_type classification.
    combined = "|".join(f"(?:{p})" for p in GOVERNANCE_TEXT_PATTERNS.values())
    # full-body scan (the GENSOL lesson, part 2): disclosures open with pages of
    # exchange-address boilerplate — a head-only scan misses the SEBI order text
    for r in q(cur, """
        SELECT ticker, filed_at, title,
               left(raw_text, 40000) AS head
        FROM mg_documents
        WHERE ticker = ANY(%s) AND filed_at BETWEEN %s AND %s
          AND (title ~* %s OR raw_text ~* %s)
        ORDER BY filed_at DESC
    """, (tickers, from_date, as_of, combined, combined)):
        tick = (r["ticker"] or "").strip().upper()
        if not tick:
            continue
        text = (r["title"] or "") + " " + (r["head"] or "")
        # patterns are written for Postgres ARE (\y word boundary); convert to
        # Python's \b for the labeling pass — \y is a hard re.error in Python
        hits = [label for label, pat in GOVERNANCE_TEXT_PATTERNS.items()
                if re.search(pat.replace(r"\y", r"\b"), text, re.I)]
        if not hits:
            continue
        entry = result.setdefault(tick, {"risk_tier": "NORMAL", "risk_events": []})
        entry["risk_tier"] = "HIGH"
        entry["risk_events"].append({
            "type": "governance-text: " + ", ".join(hits),
            "date": r["filed_at"], "title": (r["title"] or "")[:80]})
    return result


# Filing types too routine to move risk_tier (validation 2025: filed by nearly
# every company) but relevant when COMBINED with distress signatures.
REVIEW_FILING_TYPES: frozenset[str] = frozenset({
    "Action(s) initiated or orders passed",
})


def detect_disclosure_cessation(cur, tickers: list[str], as_of) -> list:
    """The GENSOL suspension signature (Jul-2026): an enforcement-type filing
    whose BODY was never ingested, followed by the company's disclosures
    CEASING entirely (>90 days silence while the corpus runs on) = suspension/
    delisting distress. Pure data signature — catches what text patterns can't
    (empty bodies) and what filing-type tiers can't (the type alone is routine
    noise; the cessation is what makes it severe)."""
    if not tickers:
        return []
    rows = q(cur, """
        WITH lastdoc AS (
            SELECT ticker, MAX(filed_at)::date AS last_filed
            FROM mg_documents WHERE ticker = ANY(%s) AND filed_at <= %s
            GROUP BY ticker
        )
        SELECT d.ticker, d.filed_at::date AS action_date, l.last_filed,
               (d.raw_text IS NULL OR length(d.raw_text) < 200) AS body_missing
        FROM mg_documents d
        JOIN lastdoc l ON l.ticker = d.ticker
        WHERE d.ticker = ANY(%s) AND d.filing_type = ANY(%s)
          AND d.filed_at <= %s
          AND d.filed_at >= l.last_filed - 30          -- action filed at/near the end
          AND %s::date - l.last_filed > 90             -- then silence
    """, (tickers, as_of, tickers, list(REVIEW_FILING_TYPES), as_of, as_of))
    out = []
    for r in rows:
        out.append({"ticker": (r["ticker"] or "").upper(),
                    "action_date": r["action_date"], "last_filed": r["last_filed"],
                    "body_missing": bool(r["body_missing"]),
                    "signature": "enforcement-type filing followed by >90d disclosure silence"})
    return out


def propose_exclusions(risk_flags: dict) -> list:
    """HIGH-tier tickers not already excluded → exclusion PROPOSALS for the
    judgment layer (confirm with an INSERT into mg_manual_exclusions — a data
    op). Never auto-excludes: severe language can be historic or resolved."""
    out = []
    for tick, rf in risk_flags.items():
        if rf.get("risk_tier") == "HIGH" and tick not in MANUAL_EXCLUDE:
            out.append({"ticker": tick,
                        "events": rf.get("risk_events", [])[:3],
                        "action": ("judgment review: if active fraud/enforcement, INSERT INTO "
                                   "mg_manual_exclusions; if historic/resolved, note and pass")})
    return out


# India policy schemes: loaded from mg_tracked_schemes at scan start (see
# init_reference_data). NOTHING here is hardcoded at runtime — new schemes
# enter via novel-vocab auto-graduation or judgment-layer SQL inserts, never
# code edits (user rule + the ECMS lesson, Jul-2026). {name: pattern}.
INDIA_POLICY_SCHEMES: dict[str, str] = {}
# {name: {"scheme_class":..., "status":..., "first_detected":...}} sidecar meta.
TRACKED_SCHEME_META: dict[str, dict] = {}


# ────────────────────────────────────────────────────────────────────────────
# REFERENCE DATA LAYER — no entity hardcoded at runtime (user rule, Jul-2026).
# The _SEED_* constants below are ONE-TIME MIGRATIONS: they populate an EMPTY
# table on first run and are never consulted again. The DB tables are the
# authority; the judgment layer maintains them with SQL (data operations).
# ────────────────────────────────────────────────────────────────────────────

_SEED_PLI_CTX = (r"scheme|manufactur|electronic|telecom|semiconductor|mobile phone|"
                 r"pharmaceutical|bulk drug|\yKSM\y|\yAPI\y|solar|photovoltaic|"
                 r"advanced chemistry cell|\yACC\y|textile|specialty steel|white goods|"
                 r"air condition|\yLED\y|drone|automobile|auto component|food process")

_SEED_TRACKED_SCHEMES: list[tuple] = [
    # (scheme_name, pattern, scheme_class, notes)
    ("PLI",
     r"production[- ]linked|\yPLI\y.{0,150}(" + _SEED_PLI_CTX + r")|(" + _SEED_PLI_CTX + r").{0,150}\yPLI\y",
     "pli_family",
     "Bare PLI collides with Performance Linked Incentive (HR language); requires sector/scheme context nearby."),
    ("ECMS (Electronics Components)",
     r"\yECMS\y|electronics? components? manufacturing scheme|component manufacturing scheme",
     "pli_family",
     "Graduated from novel-vocab detection (#11/60 on 2026-03-31; AMBER/SYRMA/PGEL/EPACK cohort)."),
    ("ACC Battery", r"advanced chemistry cell", "pli_family", "ACC PLI programme."),
    ("ALMM", r"\yALMM\y|approved list of models", "quality_gate", None),
    ("BCD", r"basic customs duty", "tariff", None),
    ("RDSS", r"\yRDSS\y|revamped distribution sector", "grid_program", None),
    ("EV incentives (FAME→E-DRIVE)",
     r"FAME[- ]?(II|2|scheme|subsid)|electric mobility promotion|EMPS[- ]?2024|PM[- ]?E[- ]?DRIVE|E[- ]DRIVE scheme|e[- ]?bus sewa",
     "incentive",
     "FAME ended Mar-2024 → EMPS-2024 → PM E-DRIVE; scheme-succession vocabulary migration."),
    ("Green Hydrogen (SIGHT)", r"SIGHT scheme|green hydrogen mission|\yNGHM\y|green hydrogen incentive", "incentive", None),
    ("PM-KUSUM", r"PM[- ]?KUSUM|KUSUM (scheme|yojana|component|tender)|kusum solar", "incentive",
     "Bare KUSUM matches the mango variety."),
    ("PM Surya Ghar", r"surya ghar", "incentive", None),
    ("Semicon Mission", r"semicon india|india semiconductor mission|semiconductor mission", "incentive", None),
    ("Defence iDEX/Make", r"\yiDEX\y|make in india defence|defence acquisition procedure", "defence", None),
    ("Safety/Compliance Mandate",
     r"\ykavach\y|made mandatory|mandatory (installation|implementation|compliance)|RDSO approv",
     "mandate", "T1 fingerprint: mandate + approved-vendor oligopoly (KERNEX/KAVACH)."),
    ("EPR/Formalization",
     r"extended producer responsibilit|\yEPR\y (registration|certificate|obligation)|battery waste management rules|plastic waste management rules",
     "mandate", "T3 fingerprint: formalization share-shift (GRAVITA/EPR)."),
]

_SEED_TRACKED_SCHEMES_US: list[tuple] = [
    ("CHIPS Act", r"CHIPS (and Science )?Act|CHIPS for America|section 48D", "us_incentive",
     "US semiconductor manufacturing incentive."),
    ("IRA / 45X", r"Inflation Reduction Act|\y45X\y|advanced manufacturing production credit|section 45X",
     "us_incentive", "US clean-energy manufacturing production credit."),
    ("IRA ITC/PTC energy credits", r"investment tax credit|production tax credit|\yITC\y transferab|\y48C\y",
     "us_incentive", None),
    ("IIJA Infrastructure", r"Infrastructure Investment and Jobs Act|\yIIJA\y|bipartisan infrastructure",
     "us_incentive", None),
    ("DOE Loan Programs", r"Loan Programs Office|DOE loan (guarantee)?|ATVM loan", "us_incentive", None),
]

_SEED_MANUAL_EXCLUSIONS: dict[str, str] = {
    "GENSOL": "SEBI enforcement action Apr-2025; stock suspended/fraud",
}

_SEED_CHAIN_CLASSIFICATIONS: dict[str, str] = {
    "crgo steel":          "differentiated",
    "power transformer":   "differentiated",
    "defense electronics": "differentiated",
    "semiconductor ic":    "differentiated",
    "battery cell":        "commodity",
    "solar cell":          "commodity",
    "solar module":        "commodity",
    "solar wafer":         "commodity",
    "optical fiber":       "commodity",
    "ems / contract":      "service_manufacturing",
    "pcb / printed":       "service_manufacturing",
    "rolling stock":       "epc_cyclical",
}


def ensure_reference_tables(cur) -> None:
    """Create the reference tables if missing and seed them ONCE if empty."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mg_tracked_schemes (
            scheme_name TEXT PRIMARY KEY,
            pattern TEXT NOT NULL,
            scheme_class TEXT DEFAULT 'unclassified',
            status TEXT DEFAULT 'active',
            source TEXT,
            first_detected DATE,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS mg_manual_exclusions (
            ticker TEXT PRIMARY KEY,
            reason TEXT,
            source TEXT,
            created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS mg_chain_classifications (
            chain_key TEXT PRIMARY KEY,
            layer_type TEXT NOT NULL,
            source TEXT,
            rationale TEXT,
            created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS mg_symbol_renames (
            old_symbol TEXT,
            new_symbol TEXT,
            renamed_on DATE,
            price_ratio NUMERIC,
            vol_ratio NUMERIC,
            confidence TEXT,
            status TEXT DEFAULT 'auto',
            detected_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (old_symbol, new_symbol));
    """)
    cur.execute("ALTER TABLE mg_tracked_schemes ADD COLUMN IF NOT EXISTS country TEXT DEFAULT 'IN'")
    cur.execute("SELECT COUNT(*) AS n FROM mg_tracked_schemes WHERE country = 'IN'")
    if cur.fetchone()["n"] == 0:
        for name, pattern, klass, notes in _SEED_TRACKED_SCHEMES:
            cur.execute("""INSERT INTO mg_tracked_schemes
                           (scheme_name, pattern, scheme_class, status, source, notes, country)
                           VALUES (%s, %s, %s, 'active', 'seed_migration', %s, 'IN')
                           ON CONFLICT DO NOTHING""", (name, pattern, klass, notes))
    cur.execute("SELECT COUNT(*) AS n FROM mg_tracked_schemes WHERE country = 'US'")
    if cur.fetchone()["n"] == 0:
        for name, pattern, klass, notes in _SEED_TRACKED_SCHEMES_US:
            cur.execute("""INSERT INTO mg_tracked_schemes
                           (scheme_name, pattern, scheme_class, status, source, notes, country)
                           VALUES (%s, %s, %s, 'active', 'seed_migration', %s, 'US')
                           ON CONFLICT DO NOTHING""", (name, pattern, klass, notes))
    cur.execute("SELECT COUNT(*) AS n FROM mg_manual_exclusions")
    if cur.fetchone()["n"] == 0:
        for tick, reason in _SEED_MANUAL_EXCLUSIONS.items():
            cur.execute("""INSERT INTO mg_manual_exclusions (ticker, reason, source)
                           VALUES (%s, %s, 'seed_migration') ON CONFLICT DO NOTHING""",
                        (tick, reason))
    cur.execute("SELECT COUNT(*) AS n FROM mg_chain_classifications")
    if cur.fetchone()["n"] == 0:
        for key, lt in _SEED_CHAIN_CLASSIFICATIONS.items():
            cur.execute("""INSERT INTO mg_chain_classifications (chain_key, layer_type, source)
                           VALUES (%s, %s, 'seed_migration') ON CONFLICT DO NOTHING""",
                        (key, lt))


def _lcs_len(a: str, b: str) -> int:
    """Longest common contiguous substring length (symbols are <= ~14 chars)."""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def detect_and_store_symbol_renames(cur) -> int:
    """Detect symbol renames from bhavcopy price-series continuity and upsert
    into mg_symbol_renames. A rename = old series ends, new series begins
    within 7 days, with price AND volume continuity at the boundary, mutually
    best-matched. Name similarity (contiguous LCS >= 3 between symbols) plus a
    tight price ratio upgrades confidence to 'high'; the rest stay 'medium'
    for judgment confirmation (UPDATE status='confirmed'/'rejected' — data op).
    Validated Jul-2026: finds GET&D→GVT&D, AMARAJABAT→ARE&M, MINDAIND→UNOMINDA,
    HBLPOWER→HBLENGINE, plus dozens of true renames (STRTECH→STLTECH, TRIL→TARIL,
    MOTHERSUMI→MOTHERSON, KALPATPOWR→KPIL, CEBBCO→JWL...)."""
    cur.execute("""
        WITH eq AS (
            SELECT symbol, trade_date, close, tottrdval,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) rn_desc,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date ASC) rn_asc
            FROM nse_bhavcopy_data WHERE series IN ('EQ','BE')
        ),
        bounds AS (
            SELECT symbol, MIN(trade_date) AS first_d, MAX(trade_date) AS last_d, COUNT(*) AS n
            FROM eq GROUP BY symbol
        ),
        edge AS (
            SELECT symbol,
                   AVG(CASE WHEN rn_desc <= 20 THEN tottrdval END) AS tail_val,
                   AVG(CASE WHEN rn_asc  <= 20 THEN tottrdval END) AS head_val,
                   MAX(CASE WHEN rn_desc = 1 THEN close END) AS last_close,
                   MAX(CASE WHEN rn_asc  = 1 THEN close END) AS first_close
            FROM eq WHERE rn_desc <= 20 OR rn_asc <= 20 GROUP BY symbol
        ),
        maxd AS (SELECT MAX(last_d) AS mx, MIN(first_d) AS mn FROM bounds)
        SELECT e.symbol AS old_symbol, st.symbol AS new_symbol, e.last_d, st.first_d,
               eo.last_close AS old_close, en.first_close AS new_close,
               eo.tail_val AS old_val, en.head_val AS new_val
        FROM bounds e
        JOIN maxd ON TRUE
        JOIN bounds st ON st.first_d BETWEEN e.last_d AND e.last_d + 7
        JOIN edge eo ON eo.symbol = e.symbol
        JOIN edge en ON en.symbol = st.symbol
        WHERE e.last_d < maxd.mx - 30 AND e.n >= 250
          AND st.first_d > maxd.mn + 30 AND st.n >= 60
          AND e.symbol <> st.symbol
    """)
    cands = []
    for r in cur.fetchall():
        oc, nc = float(r["old_close"] or 0), float(r["new_close"] or 0)
        ov, nv = float(r["old_val"] or 0), float(r["new_val"] or 0)
        if oc <= 0 or ov <= 0 or nv <= 0:
            continue
        pr, vr = nc / oc, nv / ov
        name_sim = _lcs_len(r["old_symbol"], r["new_symbol"]) >= 3
        # Volume gate: a genuine rename can see a transient liquidity dip right
        # at the symbol change (market/index trackers catching up) before price
        # continuity even matters. HBLPOWER->HBLENGINE sits at vr=0.22 -- just
        # under a flat 0.25 floor -- and was missed by every version of this
        # detector until traced here. Widen the floor when name similarity
        # already carries the identity signal; keep it tight otherwise so
        # coincidental price-matched unrelated pairs don't slip through.
        vr_floor = 0.15 if name_sim else 0.25
        if not (0.85 <= pr <= 1.18 and vr_floor <= vr <= 4.0):
            continue
        # score: price closeness, with a strong bonus for symbol-name overlap
        # (the HBLPOWER→HBLENGINE lesson: price alone lost the mutual tiebreak)
        score = abs(pr - 1) - (0.10 if name_sim else 0.0)
        cands.append({"old": r["old_symbol"], "new": r["new_symbol"], "pr": pr, "vr": vr,
                      "renamed_on": r["first_d"], "name_sim": name_sim, "score": score})
    best_old, best_new = {}, {}
    for c in cands:
        if c["old"] not in best_old or c["score"] < best_old[c["old"]]["score"]:
            best_old[c["old"]] = c
        if c["new"] not in best_new or c["score"] < best_new[c["new"]]["score"]:
            best_new[c["new"]] = c
    n_upserts = 0
    for c in best_old.values():
        if best_new.get(c["new"]) is not c:
            continue
        conf = "high" if (c["name_sim"] and 0.90 <= c["pr"] <= 1.11) else "medium"
        cur.execute("""INSERT INTO mg_symbol_renames
                       (old_symbol, new_symbol, renamed_on, price_ratio, vol_ratio, confidence)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (old_symbol, new_symbol) DO NOTHING""",
                    (c["old"], c["new"], c["renamed_on"],
                     round(c["pr"], 4), round(c["vr"], 4), conf))
        n_upserts += cur.rowcount
    return n_upserts


def compute_market_proxy(cur, as_of, n: int = 25) -> list[str]:
    """Benchmark basket, point-in-time: top-n listed companies by trailing-12m
    traded value with >= 200 trading days, restricted to symbols present in
    security_master (companies, not ETFs). No hardcoded tickers."""
    cur.execute("""
        SELECT b.symbol
        FROM nse_bhavcopy_data b
        JOIN security_master sm ON sm.nse_symbol = b.symbol
        WHERE b.series = 'EQ' AND b.trade_date BETWEEN %s::date - 365 AND %s
        GROUP BY b.symbol
        HAVING COUNT(*) >= 200
        ORDER BY SUM(b.tottrdval) DESC
        LIMIT %s
    """, (as_of, as_of, n))
    return [r["symbol"] for r in cur.fetchall()]


def init_reference_data(cur, as_of, country: str = "IN") -> dict:
    """Populate every runtime entity container from the DB. Returns a
    provenance dict for the report so the user can see exactly what reference
    data drove the scan (PMS-grade transparency)."""
    ensure_reference_tables(cur)

    # Symbol-renaming discovery is a live maintenance operation. A historical
    # replay must never mutate the reference table from later price history.
    cur.execute("SELECT COUNT(*) AS n FROM mg_symbol_renames")
    cur.fetchone()
    cur.execute("SELECT MAX(trade_date) AS mx FROM nse_bhavcopy_data")
    latest_px = cur.fetchone()["mx"]
    near_live = bool(latest_px and abs((latest_px - as_of).days) <= 45)
    if near_live:
        detect_and_store_symbol_renames(cur)

    MANUAL_EXCLUDE.clear()
    cur.execute("SELECT ticker, reason FROM mg_manual_exclusions")
    for r in cur.fetchall():
        MANUAL_EXCLUDE[r["ticker"].strip().upper()] = r["reason"] or ""

    INDIA_POLICY_SCHEMES.clear()
    TRACKED_SCHEME_META.clear()
    # Only status='active' schemes drive the screens — auto_candidates are
    # review-queue items (rendered in the report's graduation table), never
    # scanned as schemes until the judgment layer promotes them. Otherwise a
    # noisy candidate ("MULTIPLE") would pollute the policy screen.
    cur.execute("""SELECT scheme_name, pattern, scheme_class, status, first_detected
                   FROM mg_tracked_schemes
                   WHERE status = 'active' AND country = %s
                     AND (first_detected IS NULL OR first_detected <= %s)""", (country, as_of))
    for r in cur.fetchall():
        INDIA_POLICY_SCHEMES[r["scheme_name"]] = r["pattern"]
        TRACKED_SCHEME_META[r["scheme_name"]] = {
            "scheme_class": r["scheme_class"], "status": r["status"],
            "first_detected": r["first_detected"]}

    CHAIN_LAYER_TYPE.clear()
    cur.execute("SELECT chain_key, layer_type FROM mg_chain_classifications")
    for r in cur.fetchall():
        CHAIN_LAYER_TYPE[r["chain_key"].strip().lower()] = r["layer_type"]

    SYMBOL_ALIASES.clear()
    cur.execute("""SELECT old_symbol, new_symbol FROM mg_symbol_renames
                   WHERE status <> 'rejected'
                     AND (confidence = 'high' OR status = 'confirmed')
                     AND renamed_on <= %s""", (as_of,))
    for r in cur.fetchall():
        SYMBOL_ALIASES.setdefault(r["new_symbol"], []).append(r["old_symbol"])
        SYMBOL_ALIASES.setdefault(r["old_symbol"], []).append(r["new_symbol"])

    MARKET_PROXY_IN.clear()
    MARKET_PROXY_IN.extend(compute_market_proxy(cur, as_of))

    cur.execute("""SELECT old_symbol, new_symbol, renamed_on, price_ratio, confidence
                   FROM mg_symbol_renames WHERE confidence = 'medium' AND status = 'auto'
                     AND renamed_on <= %s
                   ORDER BY renamed_on DESC LIMIT 40""", (as_of,))
    pending_renames = [dict(r) for r in cur.fetchall()]

    return {
        "tracked_schemes": [
            {"name": n_, **TRACKED_SCHEME_META[n_]} for n_ in sorted(INDIA_POLICY_SCHEMES)],
        "manual_exclusions": dict(MANUAL_EXCLUDE),
        "chain_classifications": dict(CHAIN_LAYER_TYPE),
        "symbol_aliases_active": {k: v for k, v in SYMBOL_ALIASES.items()},
        "symbol_renames_pending_review": pending_renames,
        "market_proxy": list(MARKET_PROXY_IN),
        "note": ("All reference data loaded from mg_tracked_schemes / mg_manual_exclusions / "
                 "mg_chain_classifications / mg_symbol_renames + computed market proxy — "
                 "no hardcoded entities (user rule, Jul-2026)."),
    }


def graduate_novel_schemes(cur, as_of, novel_vocab: list, live_scan: bool) -> list:
    """Auto-graduation (the ECMS lesson): novel-vocab terms with broad,
    zero-prior bursts become auto_candidate schemes in mg_tracked_schemes —
    no code edit needed, judgment layer promotes/rejects with SQL. Writes only
    on live scans (backtests must not mutate reference data); always returns
    the candidate list for the report."""
    grads = []
    tracked_upper = {n.upper() for n in INDIA_POLICY_SCHEMES}
    for x in novel_vocab:
        term = (x.get("term") or "").strip()
        if (len(term) >= 4 and term.isalpha() and term.upper() not in tracked_upper
                and (x.get("n_tickers") or 0) >= 3 and (x.get("n_docs_12m") or 0) >= 3
                and (x.get("n_docs_prior_12m") or 0) == 0):
            grads.append({"term": term, "n_docs_12m": x.get("n_docs_12m"),
                          "n_tickers": x.get("n_tickers"), "tickers": x.get("tickers")})
    if live_scan:
        for g in grads:
            cur.execute("""INSERT INTO mg_tracked_schemes
                           (scheme_name, pattern, scheme_class, status, source, first_detected, notes)
                           VALUES (%s, %s, 'unclassified', 'auto_candidate', 'novel_vocab_auto', %s, %s)
                           ON CONFLICT (scheme_name) DO NOTHING""",
                        (g["term"], r"\y" + g["term"] + r"\y", as_of,
                         f"Auto-graduated: {g['n_docs_12m']} docs / {g['n_tickers']} tickers, zero prior-12m. "
                         f"Cohort: {', '.join((g.get('tickers') or [])[:6])}. Judgment: promote or reject."))
    return grads


def compute_theme_track_record(cur, as_of) -> dict:
    """Per-theme historical alpha: when this theme's beneficiaries were mapped in
    past snapshots, did they beat the market over the next 180 days?

    Strictly point-in-time: only snapshots with as_of_date + 180d <= as_of are
    measured, so no forward window leaks past the scan date. Evidence for the
    judgment layer's theme-selection step — never a score input.

    Returns {theme_name: {n_measured, median_excess_180d_pct, hit_rate_pct,
                          n_windows}}.
    """
    rows = q(cur, """
        WITH snaps AS (
            SELECT DISTINCT as_of_date AS snap
            FROM mg_india_beneficiaries
            WHERE as_of_date + 180 <= %s::date
        ),
        bene AS (
            SELECT b.theme_name, UPPER(TRIM(b.ticker)) AS ticker, s.snap
            FROM mg_india_beneficiaries b
            JOIN snaps s ON b.as_of_date = s.snap
            WHERE b.ticker IS NOT NULL AND b.conviction_score >= 0.7
            GROUP BY 1, 2, 3
        )
        SELECT bene.theme_name, bene.snap, bene.ticker,
               ROUND(((x.close - e.close) / e.close * 100)::numeric, 1) AS fwd180
        FROM bene
        JOIN LATERAL (
            SELECT close FROM nse_bhavcopy_data
            WHERE symbol = bene.ticker
              AND trade_date BETWEEN bene.snap AND bene.snap + 10
            ORDER BY trade_date LIMIT 1) e ON e.close > 0
        JOIN LATERAL (
            SELECT close FROM nse_bhavcopy_data
            WHERE symbol = bene.ticker AND trade_date <= bene.snap + 180
            ORDER BY trade_date DESC LIMIT 1) x ON true
    """, (as_of,))

    # Benchmark: median 180d proxy return per snapshot window
    bench_rows = q(cur, """
        WITH snaps AS (
            SELECT DISTINCT as_of_date AS snap
            FROM mg_india_beneficiaries
            WHERE as_of_date + 180 <= %s::date
        ),
        px AS (
            SELECT s.snap, sym,
                   (x.close - e.close) / e.close * 100 AS fwd180
            FROM snaps s
            CROSS JOIN UNNEST(%s::text[]) AS sym
            JOIN LATERAL (
                SELECT close FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date BETWEEN s.snap AND s.snap + 10
                ORDER BY trade_date LIMIT 1) e ON e.close > 0
            JOIN LATERAL (
                SELECT close FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date <= s.snap + 180
                ORDER BY trade_date DESC LIMIT 1) x ON true
        )
        SELECT snap, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fwd180) AS bench
        FROM px GROUP BY snap
    """, (as_of, MARKET_PROXY_IN))
    bench = {r["snap"]: float(r["bench"]) for r in bench_rows}

    by_theme: dict[str, dict] = {}
    for r in rows:
        b = bench.get(r["snap"])
        if b is None or r["fwd180"] is None:
            continue
        excess = float(r["fwd180"]) - b
        t = by_theme.setdefault(r["theme_name"], {"excess": [], "windows": set()})
        t["excess"].append(excess)
        t["windows"].add(r["snap"])

    out = {}
    for name, t in by_theme.items():
        vals = sorted(t["excess"])
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        out[name] = {
            "n_measured": n,
            "n_windows": len(t["windows"]),
            "median_excess_180d_pct": round(med, 1),
            "hit_rate_pct": round(sum(1 for v in vals if v > 0) / n * 100),
        }
    return out


def fetch_policy_evidence(cur, tickers: list[str], as_of, lookback_days: int = 730) -> dict:
    """India-specific policy overlay: which govt schemes does each candidate's own
    filings mention in the lookback window (point-in-time, <= as_of)?

    Company-level scheme mentions (PLI allocation, ALMM listing, BCD protection)
    are hard revenue-visibility evidence the composite formula cannot see.
    Purely an evidence field for the judgment layer — never scored.
    """
    if not tickers:
        return {}
    from_date = as_of - timedelta(days=lookback_days)
    result: dict[str, dict] = {}
    for scheme, pattern in INDIA_POLICY_SCHEMES.items():
        rows = q(cur, """
            SELECT ticker, COUNT(*) AS n, MAX(filed_at) AS latest,
                   (ARRAY_AGG(title ORDER BY filed_at DESC))[1] AS latest_title
            FROM mg_documents
            WHERE ticker = ANY(%s)
              AND country = 'IN'
              AND filed_at BETWEEN %s AND %s
              AND raw_text ~* %s
            GROUP BY ticker
        """, (tickers, from_date, as_of, pattern))
        for r in rows:
            tick = (r["ticker"] or "").strip().upper()
            if not tick:
                continue
            entry = result.setdefault(tick, {"schemes": {}, "n_policy_filings": 0})
            entry["schemes"][scheme] = {
                "n_filings": int(r["n"]),
                "latest": r["latest"],
                "latest_title": (r["latest_title"] or "")[:80],
            }
            entry["n_policy_filings"] += int(r["n"])
    return result


def compute_market_regime(cur, as_of, country: str = "IN") -> dict:
    """Lesson 6: broad-market regime context for position-sizing.

    2020-2024 backtest: hit rate ranged 28-80% depending on the regime.
    Same regime signal in two flat years (2021 mkt=+3%, 2023 mkt=-0.3%) produced
    wildly different selector alpha (-1% vs +43%) — showing that market-index level
    alone doesn't capture the theme-execution dimension. But breadth + 3m trend does.

    Uses the same 25-stock large-cap proxy as the backtest.
    """
    if country != "IN":
        return {"regime_label": "N/A", "note": "Market regime only computed for IN"}

    px = q(cur, """
        SELECT symbol, trade_date, close
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series IN ('EQ','BE')
          AND trade_date BETWEEN %s AND %s
        ORDER BY symbol, trade_date
    """, (MARKET_PROXY_IN, as_of - timedelta(days=420), as_of))

    series_map: dict[str, list[float]] = defaultdict(list)
    for r in px:
        series_map[r["symbol"]].append(float(r["close"]))

    above_200 = 0
    total = 0
    three_month_rets: list[float] = []

    for sym, closes in series_map.items():
        if len(closes) < 40:
            continue
        total += 1
        last = closes[-1]
        ma200 = sum(closes[-200:]) / min(200, len(closes))
        if last > ma200:
            above_200 += 1
        base_3m = closes[-63] if len(closes) >= 63 else closes[0]
        three_month_rets.append((last - base_3m) / base_3m * 100)

    breadth_pct = round(above_200 / total * 100, 0) if total else 0
    avg_3m_ret = round(sum(three_month_rets) / len(three_month_rets), 1) if three_month_rets else 0

    if breadth_pct >= 60 and avg_3m_ret > 3:
        regime = "BULL_FAVORABLE"
        note = (
            f"Broad-market breadth strong ({int(breadth_pct)}% of large caps above 200DMA, "
            f"{avg_3m_ret:+.1f}% avg 3m return). Use the version-matched validation "
            "manifest, not a hardcoded historical hit rate, for sizing evidence."
        )
    elif breadth_pct < 40 or avg_3m_ret < -3:
        regime = "BEAR_CAUTION"
        note = (
            f"Broad-market breadth weak ({int(breadth_pct)}% above 200DMA, "
            f"{avg_3m_ret:+.1f}% avg 3m). Treat timing and liquidity conservatively; "
            "no historical success rate is assumed without a matching validation manifest."
        )
    else:
        regime = "FLAT_MIXED"
        note = (
            f"Mixed breadth ({int(breadth_pct)}% above 200DMA, {avg_3m_ret:+.1f}% avg 3m). "
            "In mixed markets, verify product-level demand, earnings conversion and risk "
            "before sizing; the regime label does not supply an alpha assumption."
        )

    return {
        "regime_label": regime,
        "breadth_pct_above_200dma": breadth_pct,
        "avg_3m_return_pct": avg_3m_ret,
        "stocks_checked": total,
        "note": note,
    }


def fetch_theme_focus(cur, name, as_of, win_start, country):
    """Deep-dive on ONE theme or constrained product (fuzzy name match)."""
    like = f"%{name}%"
    themes = q(cur, """
        SELECT id AS theme_id, theme_name, theme_slug, description, sectors, conviction,
               first_detected, stage, stage_label, stage_evidence, hypothesis_text,
               strength_score, momentum_score, company_count, doc_count, metadata
        FROM mg_themes
        WHERE country=%s AND is_active AND theme_name ILIKE %s AND first_detected <= %s
        ORDER BY strength_score DESC NULLS LAST LIMIT 5
    """, (country, like, as_of))
    for t in themes:
        meta = t.pop("metadata") or {}
        t["supply_constraint_count"] = meta.get("supply_constraint_count") or 0
        t["is_bottleneck"] = bool(meta.get("is_bottleneck"))
        t["tension_score"] = meta.get("tension_score")
        t["snapshots"] = q(cur, """
            SELECT snapshot_date, strength_score, momentum_score, company_count
            FROM mg_theme_snapshots WHERE theme_id=%s AND snapshot_date <= %s
            ORDER BY snapshot_date DESC LIMIT 10
        """, (t["theme_id"], as_of))
        t["beneficiaries"] = q(cur, """
            SELECT b.ticker, b.company_name AS company, b.beneficiary_type, b.company_role,
                   b.relevance_score, b.rank_in_theme, b.signal_count, b.capex_signals,
                   b.first_seen_at, left(b.reasoning, 200) AS rationale
            FROM mg_theme_beneficiaries b
            WHERE b.theme_id=%s AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
            ORDER BY b.rank_in_theme ASC NULLS LAST, b.relevance_score DESC LIMIT 30
        """, (t["theme_id"], as_of))

    product_map, supply = [], {}
    if country == "IN":
        product_map = q(cur, """
            SELECT constrained_product, theme_name,
                   count(DISTINCT company) AS n_companies,
                   round(avg(conviction_score)::numeric,3) AS avg_conviction,
                   bool_or(has_order_book_signals) AS any_order_book,
                   bool_or(import_substitution_play) AS any_import_sub,
                   min(as_of_date) AS first_mapped, max(as_of_date) AS last_mapped
            FROM mg_india_beneficiaries
            WHERE (constrained_product ILIKE %s OR theme_name ILIKE %s) AND as_of_date <= %s
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10
        """, (like, like, as_of))
        rows = q(cur, """
            SELECT DISTINCT ON (company)
                   company, ticker, theme_name, constrained_product, supply_chain_node,
                   beneficiary_type, conviction_score, left(rationale, 220) AS rationale,
                   signal_count, has_order_book_signals, import_substitution_play, as_of_date
            FROM mg_india_beneficiaries
            WHERE (constrained_product ILIKE %s OR theme_name ILIKE %s)
              AND beneficiary_type = ANY(%s) AND as_of_date <= %s
            ORDER BY company, as_of_date DESC
        """, (like, like, list(SUPPLY_SIDE_TYPES), as_of))
        rows.sort(key=lambda r: (-(float(r["conviction_score"] or 0)), not r["has_order_book_signals"]))
        supply = {"matched_supply_side_companies": rows[:25]}
    gaps = q(cur, """
        SELECT sector, component, gap, gap_pct, unit, severity, target_year,
               source_url, source_title, source_published_at, provenance_status,
               ingestion_method, measurement_basis
        FROM mg_capacity_gaps
        WHERE (component ILIKE %s OR theme_name ILIKE %s)
          AND (as_of_date IS NULL OR as_of_date <= %s) LIMIT 8
    """, (like, like, as_of)) if country == "IN" else []
    imports = q(cur, """
        SELECT sector, component, import_share, primary_origin, risk_level,
               substitute_possible, substitution_horizon_years, source_url,
               source_title, source_published_at, provenance_status,
               ingestion_method, measurement_basis
        FROM mg_import_dependencies
        WHERE component ILIKE %s AND (as_of_date IS NULL OR as_of_date <= %s) LIMIT 8
    """, (like, as_of)) if country == "IN" else []
    return {"query": name, "matched_themes": themes, "matched_constrained_products": product_map,
            "capacity_gaps": gaps, "import_dependencies": imports, **supply}


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="As-of-date stock selector extraction")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--country", default="IN", choices=["IN", "US"])
    ap.add_argument("--include-capabilities", action="store_true",
                    help="deprecated compatibility flag; India product-side capability supply is included by default")
    ap.add_argument("--without-capabilities", action="store_true",
                    help="diagnostic only: exclude product-side supply names from "
                         "mg_company_capabilities (companies whose own filings "
                         "prove they manufacture a constrained product, with no "
                         "theme co-occurrence required). IN only.")
    ap.add_argument("--theme", default=None,
                    help="focus on ONE theme/constraint (fuzzy name match) instead of the full scan")
    ap.add_argument("--window-months", type=int, default=12,
                    help="emergence lookback window in months (default 12)")
    ap.add_argument("--include-moonshot", action="store_true",
                    help="run the expensive whole-market moonshot diagnostic; use on the scheduled "
                         "research refresh, not a time-sensitive decision scan")
    ap.add_argument("--include-novel-policy-discovery", action="store_true",
                    help="run the expensive whole-corpus novel-policy vocabulary scout; use on the "
                         "scheduled research refresh, not a time-sensitive decision scan")
    ap.add_argument("--include-coverage-audit", action="store_true",
                    help="run the expensive full unmapped-peer audit; use on the scheduled research "
                         "refresh, not a time-sensitive decision scan")
    ap.add_argument("--include-historical-cohort-study", action="store_true",
                    help="run the expensive multi-vintage forward-return cohort study; use on the "
                         "scheduled research refresh, not a time-sensitive decision scan")
    ap.add_argument("--include-mapping-artifact-audit", action="store_true",
                    help="run the expensive full filing audit for mapper artifacts; use on the "
                         "scheduled research refresh, not a time-sensitive decision scan")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    as_of = parse_as_of(args.as_of)
    win_start = as_of - timedelta(days=args.window_months * 30)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Reference data layer: schemes, exclusions, chain classifications, symbol
    # renames, market proxy — all loaded from DB / computed, zero hardcoding.
    reference_data = init_reference_data(cur, as_of, args.country)
    conn.commit()

    if args.theme:
        focus = fetch_theme_focus(cur, args.theme, as_of, win_start, args.country)
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "as_of_date": as_of.isoformat(),
            "country": args.country,
            "mode": "theme_focus",
            "focus": focus,
        }
        os.makedirs(REPORTS_DIR, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in args.theme)[:40]
        out_path = args.out or os.path.join(
            REPORTS_DIR, f"stock_selector_{args.country}_{safe}_{as_of.isoformat()}_data.json")
        with open(out_path, "w") as f:
            json.dump(report, f, default=jsonify, indent=1)
        conn.close()
        print(out_path)
        print("SUMMARY:", json.dumps({
            "matched_themes": len(focus["matched_themes"]),
            "matched_constrained_products": len(focus["matched_constrained_products"]),
            "supply_side_companies": len(focus.get("matched_supply_side_companies", [])),
        }))
        return

    major, emerging, landscape = fetch_theme_landscape(cur, as_of, args.window_months, args.country)

    # Improvement 1: evidence dashboard (policy counts batch-fetched once)
    all_themes_for_evidence = list({t["theme_id"]: t for t in major + emerging}.values())
    policy_counts = fetch_policy_event_counts(cur, all_themes_for_evidence, as_of, args.country)
    evidence_dashboard = build_evidence_dashboard(all_themes_for_evidence, policy_counts)

    # Lesson 5: build theme_name → confirmed_quarters lookup for phase-adjusted tech
    theme_quarters = {
        t["theme_name"]: (t.get("confirmed_quarters") or 0)
        for t in major + emerging
        if t.get("theme_name")
    }

    us_theme_beneficiaries = {}
    if args.country == "IN":
        products, gaps, imports = fetch_constraints(cur, as_of, args.window_months)
        constraint_ledger = fetch_constraint_ledger(cur, as_of, args.country)
        constraint_aliases = fetch_constraint_aliases(cur, as_of, args.country)
        constraint_observation_summary = fetch_observation_summary(cur, as_of, args.country)
        supply = fetch_supply_beneficiaries(cur, as_of, args.window_months)
        # Product-side supply (opt-in): companies whose own filings prove they
        # MAKE a constrained product, with no theme co-occurrence required.
        # Merged per product so they flow through the normal ranking path,
        # but tagged evidence_source='capability_mapper' and conviction-capped
        # so provenance stays visible all the way into the report JSON.
        capability_supply = {}
        if not args.without_capabilities:
            capability_supply = fetch_capability_beneficiaries(cur, as_of)
            for prod, lst in capability_supply.items():
                have = {(x.get("ticker") or "").upper() for x in supply.get(prod, [])}
                supply.setdefault(prod, []).extend(
                    x for x in lst if x["ticker"] not in have)
            n_names = len({x["ticker"] for l in capability_supply.values() for x in l})
            print(f"  capability supply: {n_names} product-side names across "
                  f"{len(capability_supply)} products")
        # Bridge the FULL verified-operating-maker ledger (the Investable-
        # Universe source: pharma/specialty-chemical/cement/wind/API makers)
        # into the decision spine — the constraint universe AND the candidate
        # pool — keyed on the ledger's canonical normalized_product. Without
        # this the decision sections (Investment List, As-of Decision, ranked
        # candidates, greatest constraints) saw only the legacy solar/
        # electronics mapper, so the expanded universe was invisible there.
        _rl_labels, _rl_products, _rl_supply, _rl_makers = fetch_role_ledger_products(
            cur, as_of, products, args.country)
        if _rl_products:
            products.extend(_rl_products)
            for _lab, _rws in _rl_supply.items():
                have = {(x.get("ticker") or "").upper() for x in supply.get(_lab, [])}
                supply.setdefault(_lab, []).extend(
                    x for x in _rws if (x.get("ticker") or "").upper() not in have)
            print(f"  role-ledger verified: {len(_rl_labels)} products / "
                  f"{sum(len(v) for v in _rl_supply.values())} maker rows bridged into "
                  "the decision universe")
        # Retain a wider evidence-backed universe for the decision layer.
        # The reader-facing raw screen remains compact, but a small-cap or
        # recently mapped capability name must not disappear before the Core /
        # Early lane can examine it.
        # Widened from 80: bridging the full verified-maker ledger (pharma /
        # chemical / cement / wind) into the pool means proven operating makers
        # (e.g. DIVISLAB, SUNPHARMA) must not be truncated by names that merely
        # carry an order-book flag on a legacy solar chain.
        candidates = rank_candidates(cur, supply, as_of, top_n=140,
                                     theme_quarters=theme_quarters)
        cross_theme_overlap = compute_cross_theme_overlap(supply)
        bear_cases = generate_bear_cases(major, emerging, candidates, args.country)
        chain_coverage = compute_chain_coverage(supply, major, emerging)
        # Lesson 6: market regime signal for position-sizing context
        market_regime = compute_market_regime(cur, as_of, args.country)
        # Theme track record: did this theme's beneficiaries beat the market when
        # mapped in past snapshots? Judgment-layer evidence for theme selection.
        theme_track_record = compute_theme_track_record(cur, as_of)
        # Coverage-gap detection is deferred until the decision-driving
        # constraint set is known. Scanning every peripheral mapped product's
        # filing corpus creates long reports without improving the displayed
        # maker universe; every displayed/decision-driving constraint is still
        # covered below.
        unmapped_peers = {}
        chain_technical_state = compute_chain_technical_state(cur, supply, as_of)
        # T-COIL evidence + chain-continuity alarms (the solar-2023 lesson)
        chain_capex_momentum = compute_chain_capex_momentum(cur, as_of)
        chain_continuity_alerts = detect_chain_continuity_alerts(cur, as_of)
        # Policy-first discovery engine (the Dixon/PGEL/Amber lesson)
        # A full-corpus policy scan is a monthly discovery job.  The decision
        # path reads its dated snapshot; without one, use a fast visible-universe
        # fallback and state its coverage limit instead of timing out or
        # pretending the result is comprehensive.
        _pol = (_policy_signal_snapshot(cur, as_of, args.country)
                or _candidate_policy_screen(cur, as_of, candidates, country=args.country))
        policy_beneficiary_screen = _pol["qualified"]
        policy_early_pings = _pol["early_pings"]
        policy_company_role_context = _policy_company_role_context(cur, as_of, _pol, args.country)
        # Tier-2 (Jul-2026): quantified order books + governance exclusion proposals
        _cand_ticks = [c["ticker"] for c in candidates]
        _ob = compute_orderbook_amounts(cur, as_of, _cand_ticks)
        for c in candidates:
            c["order_book_quantified"] = _ob.get(c["ticker"])
        exclusion_proposals = propose_exclusions(
            {c["ticker"]: {"risk_tier": c.get("risk_tier"),
                            "risk_events": c.get("risk_events", [])} for c in candidates})
        # GENSOL suspension signature: check the WIDER mapped universe, not just
        # top-25 (an excluded-worthy name may sit anywhere in the chains)
        _all_mapped = list({(m.get("ticker") or "").upper()
                            for members in supply.values() for m in (members or [])
                            if m.get("ticker")})
        for ce in detect_disclosure_cessation(cur, _all_mapped, as_of):
            if ce["ticker"] not in MANUAL_EXCLUDE:
                exclusion_proposals.append({
                    "ticker": ce["ticker"],
                    "events": [{"type": ce["signature"],
                                "date": ce["action_date"],
                                "title": f"last disclosure {ce['last_filed']}"
                                         + ("; filing body never ingested" if ce["body_missing"] else "")}],
                    "action": "judgment review: suspension signature — verify and INSERT INTO mg_manual_exclusions if confirmed"})
        # Tier-3 (Jul-2026): NLP mapping-artifact suspects
        mapping_artifacts = (detect_mapping_artifacts(cur, supply, as_of)
                             if args.include_mapping_artifact_audit else [])
        mapping_artifact_status = ("computed in this full research run"
                                   if args.include_mapping_artifact_audit else
                                   "deferred — run with --include-mapping-artifact-audit or use the scheduled monthly research refresh")
        # MOONSHOT SLEEVE: an expensive whole-market, venture-style discovery
        # diagnostic.  It is useful on the scheduled research refresh, but it
        # is not a Buy gate and must never make a live decision scan wait
        # minutes.  The report records whether it was intentionally deferred.
        moonshot_candidates = (compute_moonshot_candidates(cur, as_of, top_n=40)
                                if args.include_moonshot else [])
        moonshot_status = ("computed in this full research run" if args.include_moonshot else
                           "deferred — run with --include-moonshot or use the scheduled monthly research refresh")
        # Explosiveness study (Jul-2026): de-dup detector, multi-vintage cohort
        # durability test, and narrow+fresh forward-discovery candidates
        chain_cohort_duplicates = detect_chain_ticker_overlap(cur, as_of)
        # Novel policy vocabulary is deliberately high-recall and scans large
        # filing windows twice.  It is a background scout, not a decision
        # gate; defer it on a live selector run rather than making the report
        # wait for an internal research queue.
        novel_policy_vocabulary = (detect_novel_policy_vocabulary(cur, as_of)
                                   if args.include_novel_policy_discovery else [])
        novel_policy_status = ("computed in this full research run"
                               if args.include_novel_policy_discovery else
                               "deferred — run with --include-novel-policy-discovery or use the scheduled monthly research refresh")
        chain_cohort_explosiveness = (compute_chain_cohort_explosiveness(cur, as_of)
                                      if args.include_historical_cohort_study else {})
        chain_cohort_explosiveness_status = ("computed in this full research run"
                                             if args.include_historical_cohort_study else
                                             "deferred — run with --include-historical-cohort-study or use the scheduled monthly research refresh")
        narrow_fresh_candidates = identify_narrow_fresh_constraints(major, emerging)
        # Own-filing maker lookup is both a report section and the hard
        # company-evidence gate for the constraint-first research queue.
        _cp = [p.get("constrained_product") for p in products
               if p.get("constrained_product")]
        constraint_makers = capability_makers(cur, as_of, _cp)
        # The company-originated ledger can add a maker automatically only on
        # a reviewed *literal product crosswalk* plus repeated issuer-owned
        # role evidence. Human rejections remain dated vetoes; a routine human
        # approval is no longer required. Adjacent products remain discoveries:
        # an irrigation equipment maker must not become a cell maker just
        # because both sit somewhere in a broad renewable-energy narrative.
        constraint_makers = merge_exact_role_makers(cur, as_of, constraint_makers)
        # Attach the bridged role-ledger makers to their constraint by DISPLAY
        # label (fixes verified_current_makers=0 for Pharma APIs / Cement / Wind
        # etc., where the constraint spelling differs from the ledger's
        # normalized_product so merge_exact_role_makers cannot reach them).
        constraint_makers = _merge_role_ledger_maker_buckets(constraint_makers, _rl_makers)
        company_product_discoveries = fetch_company_product_discoveries(cur, as_of)
        # Legacy field name: this is now a maximum-three exception queue. Strong
        # exact evidence is auto-promoted and weak/no-role evidence auto-rejected;
        # only genuinely ambiguous product/role evidence remains here.
        company_role_review_queue = fetch_company_role_review_queue(cur, as_of)
        # Build the full as-of constraint universe once.  The report begins
        # with a compact high-priority list, while the Early/Timing lane may
        # legitimately anchor to a downstream monetisation product that sits
        # outside that compact list.
        _emergence = fetch_all_constraint_emergence(cur, as_of, args.country)
        constraint_universe = rank_greatest_constraints(
            products, gaps, imports, supply, chain_cohort_explosiveness,
            constraint_makers, as_of, top_n=max(24, len(_cp)),
            constraint_ledger=constraint_ledger,
            constraint_aliases=constraint_aliases,
            observation_summary=constraint_observation_summary,
            emergence=_emergence,
        )
        greatest_constraints = constraint_universe[:8]
        # The headline constraint list stays compact, but company discovery
        # must cover every detected chain.  Restricting the producer universe
        # to the top eight made real operating/capacity names disappear merely
        # because their product ranked ninth.  The role-separated company
        # population remains research evidence, never position authority.
        maker_universe_constraints = list(constraint_universe)
        # The report-facing PLI section is a policy-to-constraint workflow,
        # not the old mention-count shortlist.  It runs only after the
        # point-in-time constraint queue exists so a policy action cannot be
        # promoted without showing which physical chain it actually supports.
        pli_pipeline = compute_pli_pipeline(cur, as_of, _pol, greatest_constraints)
        pli_shortlist = (pli_pipeline["research_leads"]
                         + pli_pipeline["monitor_leads"])
        final_decision = build_final_selection(
            candidates, constraint_universe, policy_screen=_pol,
            display_constraints=greatest_constraints,
            market_regime=market_regime)
        opportunity_mechanisms = build_investment_mechanisms(
            candidates, constraint_universe, _pol, final_decision, as_of,
            policy_company_context=policy_company_role_context,
        )
        # Keep every decision-driving constraint visible in the report's
        # evidence and maker-universe sections.  This is a presentation
        # expansion only; PLI remains cross-walked to the initial compact
        # high-priority constraint set above.
        greatest_constraints = final_decision["constraints"]
        _reviewed_products = {c.get("constraint") for c in greatest_constraints
                              if c.get("constraint")}
        _reviewed_supply = {product: rows for product, rows in supply.items()
                            if product in _reviewed_products}
        # The broad unmapped-peer audit catches pure plays outside mapper
        # coverage, but it searches the filing corpus for every displayed
        # chain.  Run it on the scheduled research refresh; live scans still
        # add any already-known own-filing maker leads below.
        unmapped_peers = (detect_unmapped_peers(cur, _reviewed_supply, as_of)
                          if args.include_coverage_audit else {})
        unmapped_peer_status = ("computed in this full research run"
                                if args.include_coverage_audit else
                                "deferred — run with --include-coverage-audit or use the scheduled monthly research refresh")
        # Auto-graduation (the ECMS lesson): broad zero-prior bursts become
        # auto_candidate schemes in mg_tracked_schemes — writes on live scans only
        cur.execute("SELECT MAX(trade_date) AS mx FROM nse_bhavcopy_data")
        _mx = cur.fetchone()["mx"]
        live_scan = bool(_mx and abs((_mx - as_of).days) <= 45)
        scheme_graduation_candidates = graduate_novel_schemes(
            cur, as_of, novel_policy_vocabulary, live_scan)
        conn.commit()
        # MERGE into the existing per-constraint coverage-gap slot rather than
        # adding a parallel field. `unmapped_industry_peers` already IS this
        # concept (the Voltamp/TARIL failure mode) and the judgment layer
        # already reviews it per selected constraint — its detector was just
        # weak, returning 1-3 names where the capability data finds 5-25.
        # Constraint identification is untouched; only the STOCK UNIVERSE
        # underneath each constraint gets completed.
        _already = {t.upper() for lst in supply.values() for c in lst
                    if (t := (c.get("ticker") or ""))}
        for _prod, _info in constraint_makers.items():
            if _prod not in _reviewed_products:
                continue
            _seen = {r["ticker"] for r in unmapped_peers.get(_prod, [])}
            _add = []
            for m in _info["makers"]:
                if m["ticker"] in _seen or m["ticker"] in _already:
                    continue
                _add.append({
                    "ticker": m["ticker"], "company": m["company"],
                    "industry": m["industry"],
                    "industry_match": not m["needs_review"],
                    "n_filings_mentioning": m["n_docs"],
                    "n_filings_showing_manufacture": m["mfg_docs"],
                    "latest_mention": None,
                    "note": ("makes this product per its OWN filings; unmapped by the "
                             "beneficiary extractor — unscreened (no conviction or "
                             "order-book fields), review in judgment layer"
                             + (" — INDUSTRY MISMATCH, likely demand-side or "
                                "unrelated mention" if m["needs_review"] else "")),
                })
            if _add:
                unmapped_peers.setdefault(_prod, []).extend(_add)
                unmapped_peers[_prod].sort(
                    key=lambda r: -(r.get("n_filings_showing_manufacture")
                                    or r.get("n_filings_mentioning") or 0))
        regulatory_watchlist = compute_regulatory_watchlist(cur, as_of)
        trade_momentum_signals = compute_trade_momentum_signals(cur, as_of)
        us_note = None
    else:
        # Keep theme beneficiaries as a broad US discovery diagnostic, but use
        # the same country-scoped constraint/product/company bridge as India
        # for every position-authority decision.
        constraint_extra = sorted(
            [t for t in landscape
             if t["is_bottleneck"] or (t["supply_constraint_count"] or 0) >= 5],
            key=lambda x: -(x["strength_now"] or 0))
        seen, uniq = set(), []
        for t in major + emerging + constraint_extra:
            if t["theme_id"] not in seen:
                seen.add(t["theme_id"]); uniq.append(t)
        us_theme_beneficiaries = fetch_us_beneficiaries(
            cur, uniq[:30], as_of, win_start, per_theme=10
        )
        products, gaps, imports = fetch_constraints(
            cur, as_of, args.window_months, country="US"
        )
        constraint_ledger = fetch_constraint_ledger(cur, as_of, "US")
        constraint_aliases = fetch_constraint_aliases(cur, as_of, "US")
        constraint_observation_summary = fetch_observation_summary(cur, as_of, "US")
        supply = fetch_supply_beneficiaries(
            cur, as_of, args.window_months, country="US"
        )
        # Same verified-maker-ledger bridge as the IN branch (country='US').
        _rl_labels, _rl_products, _rl_supply, _rl_makers = fetch_role_ledger_products(
            cur, as_of, products, "US")
        if _rl_products:
            products.extend(_rl_products)
            for _lab, _rws in _rl_supply.items():
                have = {(x.get("ticker") or "").upper() for x in supply.get(_lab, [])}
                supply.setdefault(_lab, []).extend(
                    x for x in _rws if (x.get("ticker") or "").upper() not in have)
            print(f"  role-ledger verified: {len(_rl_labels)} products bridged "
                  "into the US decision universe")
        candidates = rank_candidates(
            cur, supply, as_of, top_n=80,
            theme_quarters=theme_quarters, country="US",
        )
        cross_theme_overlap = compute_cross_theme_overlap(supply)
        bear_cases = generate_bear_cases(major, emerging, candidates, args.country)
        chain_coverage = {}
        market_regime = compute_market_regime(cur, as_of, "US")
        theme_track_record = {}
        unmapped_peers = {}
        unmapped_peer_status = "not available for US"
        chain_technical_state = {}
        chain_capex_momentum = {}
        chain_continuity_alerts = []
        # US scheme parity (Tier-3, Jul-2026): the policy screen now runs for
        # US filings against country='US' schemes (CHIPS/IRA-45X/IIJA/DOE)
        _pol_us = (_policy_signal_snapshot(cur, as_of, "US")
                   or _candidate_policy_screen(cur, as_of, candidates, country="US"))
        policy_beneficiary_screen = _pol_us["qualified"]
        policy_early_pings = _pol_us["early_pings"]
        policy_company_role_context = _policy_company_role_context(
            cur, as_of, _pol_us, "US"
        )
        exclusion_proposals = []
        mapping_artifacts = []
        mapping_artifact_status = "not available for US"
        moonshot_candidates = []   # moonshot screen is IN-only (no US price/filing base yet)
        moonshot_status = "not available for US"
        chain_cohort_duplicates = []
        chain_cohort_explosiveness = {}
        chain_cohort_explosiveness_status = "not available for US"
        novel_policy_vocabulary = []
        novel_policy_status = "not available for US"
        scheme_graduation_candidates = []
        _cp = [p.get("constrained_product") for p in products
               if p.get("constrained_product")]
        constraint_makers = {
            product: {
                "snapshot": as_of.isoformat(), "modal_industry": None,
                "makers": [], "role_candidates": [], "rejected_candidates": [],
                "coverage": {
                    "status": "country-scoped issuer-role ledger",
                    "candidate_count": 0,
                },
            }
            for product in dict.fromkeys(_cp)
        }
        constraint_makers = merge_exact_role_makers(
            cur, as_of, constraint_makers, country="US"
        )
        constraint_makers = _merge_role_ledger_maker_buckets(constraint_makers, _rl_makers)
        company_product_discoveries = fetch_company_product_discoveries(
            cur, as_of, country="US"
        )
        company_role_review_queue = fetch_company_role_review_queue(
            cur, as_of, country="US"
        )
        regulatory_watchlist = []  # PIB ingester is India-only (pib.gov.in)
        trade_momentum_signals = []  # trade-flow ingester tracks India import codes only
        narrow_fresh_candidates = identify_narrow_fresh_constraints(major, emerging)
        _emergence = fetch_all_constraint_emergence(cur, as_of, args.country)
        constraint_universe = rank_greatest_constraints(
            products, gaps, imports, supply, chain_cohort_explosiveness,
            constraint_makers, as_of, top_n=max(24, len(_cp)),
            constraint_ledger=constraint_ledger,
            constraint_aliases=constraint_aliases,
            observation_summary=constraint_observation_summary,
            emergence=_emergence,
        )
        greatest_constraints = constraint_universe[:8]
        maker_universe_constraints = list(constraint_universe)
        pli_pipeline = compute_pli_pipeline(
            cur, as_of, _pol_us, greatest_constraints, country="US"
        )
        pli_shortlist = pli_pipeline["research_leads"] + pli_pipeline["monitor_leads"]
        final_decision = build_final_selection(
            candidates, constraint_universe, policy_screen=_pol_us,
            display_constraints=greatest_constraints,
            market_regime=market_regime,
        )
        opportunity_mechanisms = build_investment_mechanisms(
            candidates, constraint_universe, _pol_us, final_decision, as_of,
            policy_company_context=policy_company_role_context,
        )
        greatest_constraints = final_decision["constraints"]
        us_note = (
            "US mode now uses country-scoped issuer product roles, constraint candidates, "
            "ledger/observation evidence and the same final decision contract as India. "
            "Legacy India capacity/import tables are never reused. US technical/market-price "
            "entry data is not stored locally, so every eligible lane still requires an external "
            "valuation, liquidity and chart review before action."
        )
        # HONEST COVERAGE FLAG. US theme detection is rich (hundreds of themes,
        # tens of thousands of filings), but the constraint->company discovery
        # stage that feeds makers is only a seed stub (a handful of
        # 'Unclassified Research' products), so the investment list comes back
        # (near-)empty. Without this flag the report reads as "no US
        # opportunities" when the truth is "US constraint ingestion has not been
        # run". Never let an unpopulated pipeline masquerade as a negative
        # finding — the PMS contract is decision-complete, not silently empty.
        _n_constraints = len(constraint_universe or [])
        if _n_constraints < 5:
            us_note += (
                f" ⚠ COVERAGE: US constraint discovery is a seed stub "
                f"({_n_constraints} product(s), mostly 'Unclassified Research'); "
                f"the theme graph is populated but the constraint->company mapping "
                f"pipeline has not been built out for US, so the investment list is "
                f"empty by DATA COVERAGE, not because no US constraints exist. "
                f"Treat US output as not-yet-available, not as a negative signal."
            )

    # Point-in-time constraint signals for the forward-looking Investment List
    # ranking (emergence, scarcity, import momentum, policy catalyst). Built
    # from fields already computed for THIS anchor — no forward data.
    # Pass the FULL theme landscape (not just top-15 major+emerging) so every
    # themed constraint gets its emergence stage — deeper themes (CRGO, PCB,
    # fibre) were previously stage=None only because their theme sat below the
    # top-15 cut, not because they lack a stage.
    _stage_universe = locals().get("landscape") or (major + emerging)
    # Signals are keyed on `products` (the constrained-product rows), but the
    # investment-list BLOCKS are keyed on candidate.products — a broader set,
    # especially for US where fetch_constraints returns only a high-priority
    # slice while the maker blocks span every discovered product. A block whose
    # product has no signal entry gets stage=None (no emergence signal at all).
    # Add a lightweight stub for every candidate product missing from `products`
    # so the stage matcher (exact + sector-synonym fallback) runs for it too.
    # US only: fetch_constraints returns a thin high-priority slice for US, so
    # most maker-block products lack a signal. India's constrained_products
    # already cover its blocks, so the stub is scoped OUT for India to keep the
    # reviewed India ranking exactly stable.
    _sig_products = list(products or [])
    if args.country == "US":
        _have = {p.get("constrained_product") for p in _sig_products}
        for _c in (candidates or []):
            for _p in (_c.get("products") or []):
                if _p and _p not in _have and _looks_like_product(_p):
                    _have.add(_p)
                    _sig_products.append({"constrained_product": _p, "theme_name": _p,
                                          "any_order_book": False, "any_import_sub": False})
    _final_constraint_signals = _build_constraint_signals(
        _sig_products, _stage_universe, emerging, as_of,
        doc_trade=locals().get("trade_momentum_signals"),
        doc_reg=locals().get("regulatory_watchlist"))

    # Two US views (the emerging vs biggest split): "biggest" ranks by
    # evidenced-issuer breadth (the significant US constraints — semiconductors,
    # energy, medical devices), "emerging" is the about-to-explode screen
    # (emergence + scarcity, the concentrated not-yet-crowded plays). India uses
    # a single emerging list (its scarcity signal is backtest-validated). The
    # primary final_investment_list is the biggest view for US, emerging for IN.
    _clean_mk = _clean_maker_set(constraint_makers)
    us_biggest_constraints = None
    us_emerging_constraints = None
    if args.country == "US":
        us_biggest_constraints = compute_final_investment_list(
            candidates, constraint_signals=_final_constraint_signals,
            clean_makers=_clean_mk, country="US", rank_mode="biggest")
        us_emerging_constraints = compute_final_investment_list(
            candidates, constraint_signals=_final_constraint_signals,
            clean_makers=_clean_mk, country="US", rank_mode="emerging")
        _final_investment_list = us_biggest_constraints
    else:
        _final_investment_list = compute_final_investment_list(
            candidates, constraint_signals=_final_constraint_signals,
            clean_makers=_clean_mk, country=args.country, rank_mode="emerging")

    # Narrative-emergence diagnostic plus exact company-role context. Filing
    # acceleration is not binding-state or position authority.
    _emerging_detected = detect_emerging_constraints(cur, as_of, args.country, top_n=10)
    attach_emerging_constraint_makers(cur, as_of, _emerging_detected, args.country)
    attach_constraint_exposure_companies(cur, as_of, _emerging_detected, args.country)
    _maker_linked_emerging = sorted(
        [c for c in _emerging_detected if c.get("stocks_verified")],
        key=lambda c: (-len(c.get("stocks_verified") or []),
                       -float(c.get("emergence_score") or 0)))
    # GENERIC blended constraint+company engine: trade-import acceleration
    # (Comtrade, per HS) blended with filing-signal emergence, companies linked
    # HS-anchored (crosswalk -> role ledger) with no token matching or per-sector
    # data. One uniform pass over every tracked constraint.
    try:
        from constraint_company_engine import detect_constraints_with_companies
        _blended_constraints = detect_constraints_with_companies(cur, as_of, args.country)
    except Exception as _e:
        _blended_constraints = []
        print(f"  (blended constraint+company engine skipped: {_e})")
    # Diagnostic exact operating-maker inventory. It cannot override final_decision.
    _verified_universe = fetch_verified_maker_universe(cur, as_of, args.country)
    _validation = validation_status(as_of, args.country)
    _report_manifest = {
        "logic": build_logic_manifest(),
        "data": build_data_manifest(cur, as_of, args.country),
    }

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "country": args.country,
        "us_data_note": us_note,
        "window_months": args.window_months,
        "as_of_rule": "all queries filtered to <= as_of; emergence window = as_of minus window_months",
        "validation": _validation,
        "report_manifest": _report_manifest,
        "major_themes": major,
        "emerging_themes": emerging,
        "constrained_products": products,
        "greatest_constraints": greatest_constraints,
        # The greatest-constraint-theme DETECTOR: the year's most-inflecting
        # constraints, diversified across mechanism families (Power/T&D, Battery,
        # Electronics, Hydrogen, Railway ...), by YoY signal acceleration —
        # independent of whether a listed pure-play already makes it. This is the
        # "what became constrained THIS year" view; the maker-gated investable
        # list follows separately.
        "greatest_emerging_constraints": _emerging_detected,
        "maker_linked_emerging_research": _maker_linked_emerging,
        "investable_emerging_constraints": [],
        "blended_constraint_company_engine": _blended_constraints,
        "verified_maker_universe": _verified_universe,
        "constraint_ledger": constraint_ledger,
        "constraint_product_aliases": constraint_aliases,
        "constraint_observation_summary": constraint_observation_summary,
        "constraint_company_universe": maker_universe_constraints,
        "maker_universe_constraints": maker_universe_constraints,
        "final_decision": final_decision,
        "opportunity_mechanisms": opportunity_mechanisms,
        "constraint_makers": constraint_makers,
        "company_product_discoveries": company_product_discoveries,
        "company_role_review_queue": company_role_review_queue,
        "capacity_gaps": gaps,
        "import_dependencies": imports,
        "supply_side_beneficiaries": supply,
        "us_theme_beneficiaries_diagnostic": us_theme_beneficiaries,
        "ranked_candidates": candidates,
        "constraint_company_diagnostic": _final_investment_list,
        # Retained as an empty compatibility field so downstream consumers
        # cannot mistake the old explosiveness screen for action authority.
        "final_investment_list": [],
        # US-only companion views (None for India, which uses one list):
        "us_biggest_constraints": us_biggest_constraints,
        "us_emerging_constraints": us_emerging_constraints,
        # Improvements 1-2-3-4-5
        "evidence_dashboard": evidence_dashboard,
        "cross_theme_overlap": cross_theme_overlap,
        "bear_cases": bear_cases,
        "chain_coverage": chain_coverage,
        "theme_track_record": theme_track_record,
        "unmapped_industry_peers": unmapped_peers,
        "unmapped_peer_status": unmapped_peer_status,
        "chain_technical_state": chain_technical_state,
        "chain_capex_momentum": chain_capex_momentum,
        "chain_continuity_alerts": chain_continuity_alerts,
        "policy_beneficiary_screen": policy_beneficiary_screen,
        "policy_early_pings": policy_early_pings,
        "policy_company_role_context": list(policy_company_role_context.values()),
        "pli_shortlist": pli_shortlist,
        "pli_pipeline": pli_pipeline,
        "scheme_graduation_candidates": scheme_graduation_candidates,
        "reference_data": reference_data,
        "exclusion_proposals": exclusion_proposals,
        "mapping_artifacts": mapping_artifacts,
        "mapping_artifact_status": mapping_artifact_status,
        "moonshot_candidates": moonshot_candidates,
        "moonshot_status": moonshot_status,
        "chain_cohort_duplicates": chain_cohort_duplicates,
        "novel_policy_vocabulary": novel_policy_vocabulary,
        "novel_policy_status": novel_policy_status,
        "regulatory_watchlist": regulatory_watchlist,
        "trade_momentum_signals": trade_momentum_signals,
        "chain_cohort_explosiveness": chain_cohort_explosiveness,
        "chain_cohort_explosiveness_status": chain_cohort_explosiveness_status,
        "narrow_fresh_candidates": narrow_fresh_candidates,
        "market_regime": market_regime,
        "backtest_calibration": [],
        "scoring_note": (
            "fundamental_score = 0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub. "
            "composite = fundamental × freshness_multiplier "
            "(new_theme=×1.10 | fresh 1-4q=×1.12 | developing 5-8q=×1.05 | established=×1.00 | consensus 16+q=×0.93). "
            "Ranked by composite_score; fundamental_score is tiebreaker. "
            "200DMA drives position_size_guidance (full/half) NOT the score; any performance "
            "claim must come from the version-matched validation manifest. "
            "Sector-mismatch companies excluded from ranked list (visible in supply_side_beneficiaries). "
            "Demand-side consumers receive ×0.3 conviction discount. "
            "risk_tier (HIGH/ELEVATED/NORMAL) from mg_documents — HIGH-tier candidates should be reviewed before acting. "
            "discovery_tier is a MECHANICAL SCREEN, not the final call: "
            "Tier1=top5 by composite, Tier3_Watch=ranks 6-8 fresh/new_theme + NORMAL risk, Tier3_Ignore=rest. "
            "Final categorization (investment-committee candidate / Early-Timing candidate / Watch / Avoid) is made by the analyst judgment layer "
            "reading the full per-stock evidence — see the stock-selector skill Step 3. "
            "market_regime: BULL_FAVORABLE/FLAT_MIXED/BEAR_CAUTION — see market_regime field for position-sizing context. "
            "Performance claims are loaded only from a version-matched validation manifest; "
            "no hardcoded historical return has decision authority."
        ),
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = args.out or os.path.join(REPORTS_DIR, f"stock_selector_{args.country}_{as_of.isoformat()}_data.json")
    with open(out_path, "w") as f:
        json.dump(report, f, default=jsonify, indent=1)
    conn.close()
    print(out_path)
    print("SUMMARY:", json.dumps({
        "major_themes": len(major), "emerging_themes": len(emerging),
        "constrained_products": len(products), "candidates": len(candidates),
        "top5_candidates": [c["ticker"] for c in candidates[:5]],
        "cross_theme_overlap_leaders": [x["ticker"] for x in cross_theme_overlap[:5]],
        "high_confidence_themes": [e["theme_name"][:35] for e in evidence_dashboard[:3]],
    }))


if __name__ == "__main__":
    main()
