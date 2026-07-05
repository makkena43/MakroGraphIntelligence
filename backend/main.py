"""MakroGraph Intelligence — FastAPI Backend.

All data endpoints that power the React frontend.
Run with:  uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import psycopg2
import yaml
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

# ─── Config ──────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open(ROOT / "config" / "settings.yaml") as f:
        cfg = yaml.safe_load(f)
    secrets_path = ROOT / "config" / "secrets.json"
    if secrets_path.exists():
        with open(secrets_path) as f:
            secrets = json.load(f)
        for section, values in secrets.items():
            if section.startswith("_"):
                continue
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(
                    {k: v for k, v in values.items() if v}
                )
    _env_overrides = {
        ("neo4j",      "password"):  "MAKROGRAPH_NEO4J_PASSWORD",
        ("postgresql", "password"):  "MAKROGRAPH_PG_PASSWORD",
        ("anthropic",  "api_key"):   "ANTHROPIC_API_KEY",
        ("fred",       "api_key"):   "FRED_API_KEY",
        ("eia",        "api_key"):   "EIA_API_KEY",
        ("congress",   "api_key"):   "CONGRESS_API_KEY",
    }
    for (section, key), env_var in _env_overrides.items():
        val = os.environ.get(env_var)
        if val:
            cfg.setdefault(section, {})[key] = val
    return cfg


CFG: dict = _load_config()

# ─── DB ──────────────────────────────────────────────────────────────────────

_pg = None

def get_pg():
    global _pg
    if _pg is None:
        try:
            from makrograph.storage.pg_store import PGStore
            _pg = PGStore(CFG.get("postgresql", {}))
        except Exception as e:
            logging.warning("PGStore init failed: %s", e)
    return _pg


# ─── App ─────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.info("MakroGraph API starting (DB lazily connected on first request)")
    _ensure_ai_summaries_table()
    yield
    logging.info("MakroGraph API shutting down")


def _ensure_ai_summaries_table() -> None:
    """Create mg_ai_summaries table if it doesn't exist."""
    try:
        pg = get_pg()
        if not pg:
            return
        with pg._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS mg_ai_summaries (
                        id            SERIAL PRIMARY KEY,
                        country       VARCHAR(10)  NOT NULL,
                        year          VARCHAR(10)  NOT NULL,
                        context_type  VARCHAR(50)  NOT NULL,
                        summary_text  TEXT         NOT NULL,
                        industry_insights JSONB    DEFAULT '{}',
                        generated_at  TIMESTAMP    DEFAULT NOW(),
                        UNIQUE (country, year, context_type)
                    )
                """)
            conn.commit()
        logging.info("mg_ai_summaries table ready")
    except Exception as e:
        logging.warning("Could not ensure mg_ai_summaries table: %s", e)


app = FastAPI(title="MakroGraph Intelligence API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Claude helper ───────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> str:
    import requests as _req
    acfg    = CFG.get("anthropic", {})
    api_key = acfg.get("api_key", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    model   = acfg.get("model", "claude-sonnet-4-6")
    temp    = float(acfg.get("temperature", 0.4))
    tokens  = int(acfg.get("max_tokens", 8192))
    timeout = int(acfg.get("timeout_seconds", 300))
    ctout   = int(acfg.get("connect_timeout_seconds", 10))
    resp = _req.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": tokens,
            "temperature": temp,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=(ctout, timeout),
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG / KPIs
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/debug/pipeline-readiness")
def debug_pipeline_readiness(country: str = "US", year: int = 2020) -> dict:
    """Pre-pipeline diagnostic: shows exactly what data exists and what will be processed.

    Call this BEFORE running the pipeline to understand:
    1. How many documents exist for country+year
    2. What status they're in (fetched/nlp_done/etc)
    3. Whether reset is needed
    4. Whether Ingest needs to run first
    """
    pg = get_pg()
    if not pg:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        from_d = f"{year}-01-01"
        to_d   = f"{year}-12-31"

        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:

                # Document counts by status
                cur.execute("""
                    SELECT processing_status,
                           COUNT(*) AS doc_count,
                           MIN(filed_at)::date AS earliest,
                           MAX(filed_at)::date AS latest,
                           COUNT(DISTINCT company) AS companies
                    FROM mg_documents
                    WHERE country = %s AND filed_at BETWEEN %s AND %s
                    GROUP BY processing_status
                    ORDER BY doc_count DESC
                """, (country, from_d, to_d))
                docs_by_status = [dict(r) for r in cur.fetchall()]

                # Total docs
                cur.execute("""SELECT COUNT(*) AS total FROM mg_documents
                               WHERE country=%s AND filed_at BETWEEN %s AND %s""",
                            (country, from_d, to_d))
                total_docs = cur.fetchone()["total"]

                # Docs ready for NLP
                cur.execute("""SELECT COUNT(*) AS ready FROM mg_documents
                               WHERE country=%s AND filed_at BETWEEN %s AND %s
                               AND processing_status='fetched'""",
                            (country, from_d, to_d))
                ready_for_nlp = cur.fetchone()["ready"]

                # Signals already extracted
                cur.execute("""
                    SELECT COUNT(*) AS signal_count,
                           COUNT(DISTINCT signal_type) AS signal_types,
                           COUNT(*) FILTER (WHERE s.signal_type='capacity_constraint_seller') AS seller_sigs,
                           COUNT(*) FILTER (WHERE COALESCE(s.perspective,'neutral')='seller') AS seller_perspective
                    FROM mg_signals s
                    JOIN mg_documents d ON d.id=s.document_id
                    WHERE d.country=%s AND d.filed_at BETWEEN %s AND %s
                """, (country, from_d, to_d))
                signals = dict(cur.fetchone())

                # Themes already created for this country
                cur.execute("""SELECT COUNT(*) AS theme_count FROM mg_themes WHERE country=%s AND is_active=TRUE""",
                            (country,))
                themes = cur.fetchone()["theme_count"]

                # Beneficiaries
                cur.execute("""
                    SELECT COUNT(*) AS bene_count FROM mg_theme_beneficiaries tb
                    JOIN mg_themes t ON t.id=tb.theme_id WHERE t.country=%s
                """, (country,))
                benes = cur.fetchone()["bene_count"]

        # Diagnosis
        diagnosis = []
        action_needed = []

        if total_docs == 0:
            diagnosis.append("❌ NO DOCUMENTS FOUND for this country+year")
            action_needed.append("Run INGEST stage first to fetch documents from EDGAR/NSE/BSE")
        elif ready_for_nlp == 0 and total_docs > 0:
            diagnosis.append(f"⚠️ {total_docs} documents exist but NONE in 'fetched' status")
            action_needed.append("Run reset SQL: scripts/reset_us_2020.sql (or appropriate reset script)")
            action_needed.append("Then re-run NLP stage")
        elif ready_for_nlp > 0:
            diagnosis.append(f"✅ {ready_for_nlp} documents ready for NLP processing")

        if signals["signal_count"] > 0:
            diagnosis.append(f"ℹ️ {signals['signal_count']} signals already extracted from prior run")
            if signals["seller_sigs"] == 0:
                action_needed.append("Reset and re-run NLP to get new signal types (capacity_constraint_seller, etc.)")

        if themes > 0:
            diagnosis.append(f"ℹ️ {themes} themes already exist")
        if benes > 0:
            diagnosis.append(f"ℹ️ {benes} beneficiary mappings already exist")

        return {
            "country":         country,
            "year":            year,
            "total_documents": total_docs,
            "docs_by_status":  docs_by_status,
            "ready_for_nlp":   ready_for_nlp,
            "signals":         signals,
            "themes_count":    themes,
            "beneficiaries":   benes,
            "diagnosis":       diagnosis,
            "action_needed":   action_needed,
            "conclusion":      (
                "READY — run NLP stage" if ready_for_nlp > 0 else
                "NEED INGEST — no documents in DB" if total_docs == 0 else
                "NEED RESET — documents exist but already processed"
            ),
        }
    except Exception as e:
        logging.error("pipeline_readiness: %s", e)
        return {"error": str(e)}


@app.get("/api/debug/data-coverage")
def debug_data_coverage(country: str = "US") -> dict:
    """Shows what year-specific data actually exists in the DB.
    Use this to understand why year intelligence shows same results."""
    pg = get_pg()
    if not pg:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Snapshot dates — tells us if pipeline was run per-year
                cur.execute("""
                    SELECT EXTRACT(YEAR FROM s.snapshot_date)::int AS yr,
                           COUNT(DISTINCT s.theme_id) AS themes,
                           COUNT(*) AS snapshots
                    FROM mg_theme_snapshots s
                    JOIN mg_themes t ON t.id = s.theme_id
                    WHERE t.country = %s AND t.is_active = TRUE
                    GROUP BY 1 ORDER BY 1
                """, (country,))
                snapshot_dist = [dict(r) for r in cur.fetchall()]

                # Document filing dates — actual historical data
                cur.execute("""
                    SELECT EXTRACT(YEAR FROM filed_at)::int AS yr,
                           COUNT(*) AS docs,
                           COUNT(DISTINCT company) AS companies
                    FROM mg_documents
                    WHERE country = %s
                    GROUP BY 1 ORDER BY 1
                """, (country,))
                doc_dist = [dict(r) for r in cur.fetchall()]

                # Signal filing dates
                cur.execute("""
                    SELECT EXTRACT(YEAR FROM d.filed_at)::int AS yr,
                           COUNT(*) AS signals,
                           COUNT(*) FILTER (WHERE s.signal_type IN
                               ('supply_bottleneck','inventory_drawdown',
                                'capacity_shortage','demand_exceeds_supply')) AS constraint_sigs
                    FROM mg_signals s
                    JOIN mg_documents d ON d.id = s.document_id
                    WHERE d.country = %s
                    GROUP BY 1 ORDER BY 1
                """, (country,))
                signal_dist = [dict(r) for r in cur.fetchall()]

                # Check entity_id linkage rate
                cur.execute("""
                    SELECT COUNT(*) AS total_signals,
                           COUNT(entity_id) AS with_entity_id,
                           ROUND(COUNT(entity_id)::numeric / COUNT(*) * 100, 1) AS entity_pct
                    FROM mg_signals s
                    JOIN mg_documents d ON d.id = s.document_id
                    WHERE d.country = %s
                """, (country,))
                linkage = dict(cur.fetchone())

                # Beneficiary company_name vs document company match rate (sample)
                cur.execute("""
                    SELECT COUNT(DISTINCT tb.company_name) AS bene_companies,
                           COUNT(DISTINCT LOWER(TRIM(d.company))) AS doc_companies,
                           COUNT(DISTINCT CASE
                               WHEN LOWER(TRIM(tb.company_name)) = LOWER(TRIM(d.company))
                               THEN tb.company_name END) AS matching
                    FROM mg_theme_beneficiaries tb
                    JOIN mg_themes t ON t.id = tb.theme_id
                    CROSS JOIN (SELECT DISTINCT company FROM mg_documents WHERE country = %s LIMIT 500) d
                    WHERE t.country = %s AND t.is_active = TRUE
                """, (country, country))
                match_rate = dict(cur.fetchone())

        return {
            "snapshot_years": snapshot_dist,
            "document_years": doc_dist,
            "signal_years": signal_dist,
            "entity_id_linkage": linkage,
            "company_name_match_sample": match_rate,
        }
    except Exception as e:
        logging.error("debug_data_coverage: %s", e)
        return {"error": str(e)}


@app.get("/api/investment-shortlist")
def get_investment_shortlist(
    country: str = "US",
    year: int | None = None,
    top_n: int = 60,
    capex_focus: bool = False,   # when True, boost companies with capex_increase signals
) -> list[dict]:
    """Unified investment shortlist across all constraint themes.

    Investability Score (0-100):
      35% Theme conviction    — CONFIRMED > DEVELOPING > EMERGING
      25% Escalation signal   — NEW/ESCALATING theme vs PERSISTENT
      25% Constraint exposure — constraint signals in beneficiary's own filings
      15% Theme overlap       — appears in multiple themes = higher conviction

    Companies are de-duplicated: a company in 3 themes appears once
    with ALL theme context merged.

    Sorted by Investability Score DESC so the top 10 rows are the highest
    conviction investment ideas.
    """
    pg = get_pg()
    if not pg:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        from collections import defaultdict
        import math as _math

        from_d = date(year, 1, 1) if year else date(date.today().year - 1, 1, 1)
        to_d   = date(year, 12, 31) if year else date.today()
        if year and year >= date.today().year:
            to_d = date.today()

        # ── Step 1: Get shortlisted themes with YoY focus class ──────────────
        # Use quarter_series to classify themes as new/escalating/persistent
        all_themes_raw = pg.get_shortlisted_themes(min_quarters=1, country=country, year=None)

        import json as _json
        _CONV_SCORE = {"high": 1.0, "confirmed": 0.85, "developing": 0.65, "emerging": 0.40, "watch": 0.20}
        _FOCUS_SCORE = {"new": 1.0, "escalating": 0.90, "no_prior": 0.60, "persistent": 0.35, "easing": 0.10, "demand_only": 0.0}

        theme_meta: dict[str, dict] = {}
        for t in all_themes_raw:
            slug = t.get("theme_slug") or ""
            if not slug:
                continue
            raw = t.get("quarter_series", [])
            try:
                qs = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception:
                qs = []

            yr = year or date.today().year
            this_qs  = [q for q in qs if int(q.get("year", 0)) == yr]
            prior_qs = [q for q in qs if int(q.get("year", 0)) == yr - 1]

            if not this_qs:
                continue

            this_avg  = sum(float(q.get("strength", 0)) for q in this_qs) / len(this_qs)
            prior_avg = (sum(float(q.get("strength", 0)) for q in prior_qs) / len(prior_qs)) if prior_qs else 0.0
            delta_pct = ((this_avg - prior_avg) / prior_avg * 100) if prior_avg > 0 else None

            fd = str(t.get("first_detected") or "")
            if (f"{yr}-01-01" <= fd <= f"{yr}-12-31") and not prior_qs:
                focus = "new"
            elif prior_qs and delta_pct is not None and delta_pct >= 20:
                focus = "escalating"
            elif prior_qs and delta_pct is not None and delta_pct <= -15:
                focus = "easing"
            elif prior_qs:
                focus = "persistent"
            else:
                focus = "no_prior"

            conv = str(t.get("conviction") or "emerging").lower()
            theme_meta[slug] = {
                "theme_name":    t.get("theme_name", ""),
                "theme_slug":    slug,
                "conviction":    conv,
                "focus_class":   focus,
                "this_avg":      round(this_avg, 1),
                "delta_pct":     round(delta_pct, 1) if delta_pct is not None else None,
                "conv_score":    _CONV_SCORE.get(conv, 0.3),
                "focus_score":   _FOCUS_SCORE.get(focus, 0.3),
            }

        actionable_slugs = [s for s, m in theme_meta.items() if m["focus_class"] in ("new", "escalating", "no_prior")]
        all_slugs        = list(theme_meta.keys())

        if not all_slugs:
            return []

        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:

                # Step 2: resolve theme IDs
                cur.execute(
                    "SELECT id, theme_slug FROM mg_themes WHERE theme_slug = ANY(%s) AND is_active = TRUE",
                    (all_slugs,)
                )
                slug_to_id = {r["theme_slug"]: r["id"] for r in cur.fetchall()}

                # Step 3: beneficiary companies across all matched themes
                theme_ids = list(slug_to_id.values())
                if not theme_ids:
                    return []

                cur.execute(
                    """SELECT tb.theme_id, tb.company_name, tb.ticker,
                              tb.company_role, tb.relevance_score, tb.rank_in_theme
                       FROM mg_theme_beneficiaries tb
                       WHERE tb.theme_id = ANY(%s)
                         AND tb.company_name IS NOT NULL AND tb.company_name != ''
                         AND (tb.window_end IS NULL OR tb.window_end >= %s)
                         AND (tb.window_start IS NULL OR tb.window_start <= %s)
                       ORDER BY tb.relevance_score DESC""",
                    (theme_ids, from_d, to_d)
                )
                bene_rows = list(cur.fetchall())

                # Build company map
                id_to_slug = {v: k for k, v in slug_to_id.items()}
                co_data: dict[str, dict] = {}
                co_theme_slugs: dict[str, set] = defaultdict(set)

                for r in bene_rows:
                    slug = id_to_slug.get(r["theme_id"], "")
                    name = r["company_name"]
                    co_theme_slugs[name].add(slug)
                    if name not in co_data or float(r["relevance_score"] or 0) > float(co_data[name].get("relevance_score", 0)):
                        co_data[name] = {
                            "company":        name,
                            "ticker":         r["ticker"] or "",
                            "company_role":   r["company_role"] or "beneficiary",
                            "relevance_score":float(r["relevance_score"] or 0),
                            "rank_in_theme":  int(r["rank_in_theme"] or 50),
                        }

                if not co_data:
                    return []

                # Sector gate: remove retail/finance/consumer/etc. before signal query
                try:
                    from makrograph.themes import beneficiary_mapper as _bm_mod
                    _BLOCKED = frozenset({
                        "retail", "consumer_goods", "food", "beverage", "restaurant",
                        "homebuilder", "apparel", "cosmetic", "cannabis",
                        "finance", "insurance", "pharmacy_chain", "hospital",
                        "airline", "hotel", "entertainment", "media", "real_estate",
                    })
                    to_remove = []
                    for nm, meta in co_data.items():
                        ticker = (meta.get("ticker") or "").upper()
                        sector = _bm_mod._KNOWN_TICKER_SECTORS.get(ticker) if ticker else None
                        if sector is None:
                            for frag, sec in _bm_mod._COMPANY_NAME_SECTOR_PATTERNS:
                                if frag in nm.lower():
                                    sector = sec; break
                        if sector in _BLOCKED:
                            to_remove.append(nm)
                    for nm in to_remove:
                        co_data.pop(nm, None)
                        co_theme_slugs.pop(nm, None)
                except Exception:
                    pass

                if not co_data:
                    return []

                # Step 4: constraint + capex signals for THIS year AND prior year
                # We need YoY delta — companies with SPIKING constraint signals
                # are far more interesting than companies that always file a lot.
                company_names  = list(co_data.keys())
                tickers_invest = list(set(
                    m["ticker"].upper().strip()
                    for m in co_data.values()
                    if m.get("ticker")
                ))
                
                def _sig_query(cur, date_from, date_to, cnames, tiks, cntry):
                    """Get signal counts per company for a date window.
                    Uses ticker match (reliable) then company name match (fallback).
                    Counts SELLER-perspective constraint signals only:
                      - capacity_constraint_seller (explicit seller signal, new pipeline)
                      - supply_bottleneck/capacity_shortage with perspective='seller'
                      - supply_bottleneck/capacity_shortage with seller-language context
                    Buyer-side supply_bottleneck (e.g. CVNA needing cars) is excluded."""
                    results_map: dict[str, dict] = {}

                    _SELLER_C = """(
                        s.signal_type = 'capacity_constraint_seller'
                        OR (s.signal_type IN (
                                'supply_bottleneck','inventory_drawdown',
                                'capacity_shortage','demand_exceeds_supply'
                            )
                            AND (COALESCE(s.perspective,'neutral') = 'seller'
                                 OR s.context_text ILIKE '%%%%our capacity%%%%'
                                 OR s.context_text ILIKE '%%%%our backlog%%%%'
                                 OR s.context_text ILIKE '%%%%cannot meet demand%%%%'
                                 OR s.context_text ILIKE '%%%%fully booked%%%%'
                                 OR s.context_text ILIKE '%%%%our lead time%%%%'))
                    )"""

                    if tiks:
                        cur.execute(
                            f"""SELECT UPPER(TRIM(d.ticker)) AS doc_ticker,
                                      COUNT(*) FILTER (WHERE {_SELLER_C}) AS c_count,
                                      COUNT(*) FILTER (WHERE s.signal_type IN (
                                          'demand_surge','technology_adoption'
                                      )) AS d_count,
                                      COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase') AS capex_count,
                                      MAX(CASE WHEN {_SELLER_C} AND s.context_text IS NOT NULL
                                               AND LENGTH(s.context_text) > 40
                                               THEN s.context_text ELSE NULL END) AS best_quote,
                                      MAX(d.filed_at)::date AS last_filing
                                 FROM mg_signals s
                                 JOIN mg_documents d ON d.id = s.document_id
                                 WHERE d.filed_at BETWEEN %s AND %s
                                   AND d.country = %s
                                   AND UPPER(TRIM(d.ticker)) = ANY(%s)
                                 GROUP BY UPPER(TRIM(d.ticker))""",
                            (date_from, date_to, cntry, tiks)
                        )
                        for r in cur.fetchall():
                            results_map[r["doc_ticker"]] = dict(r)

                    # Fallback: company name match for those not found by ticker
                    unfound_names = [n for n in cnames if not any(
                        co_data.get(n, {}).get("ticker","").upper() == tk
                        for tk in results_map
                    )]
                    if unfound_names:
                        cur.execute(
                            f"""SELECT d.company,
                                      COUNT(*) FILTER (WHERE {_SELLER_C}) AS c_count,
                                      COUNT(*) FILTER (WHERE s.signal_type IN (
                                          'demand_surge','technology_adoption'
                                      )) AS d_count,
                                      COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase') AS capex_count,
                                      MAX(CASE WHEN {_SELLER_C} AND s.context_text IS NOT NULL
                                               AND LENGTH(s.context_text) > 40
                                               THEN s.context_text ELSE NULL END) AS best_quote,
                                      MAX(d.filed_at)::date AS last_filing
                                 FROM mg_signals s
                                 JOIN mg_documents d ON d.id = s.document_id
                                 WHERE d.filed_at BETWEEN %s AND %s
                                   AND d.country = %s
                                   AND d.company = ANY(%s)
                                 GROUP BY d.company""",
                            (date_from, date_to, cntry, unfound_names)
                        )
                        for r in cur.fetchall():
                            results_map[r["company"]] = dict(r)
                    return results_map

                # This-year signals
                this_sigs = _sig_query(cur, from_d, to_d, company_names, tickers_invest, country)
                
                # Prior-year signals (for delta computation)
                prior_from = date(from_d.year - 1, from_d.month, from_d.day)
                prior_to   = date(to_d.year - 1, to_d.month, to_d.day)
                prior_sigs = _sig_query(cur, prior_from, prior_to, company_names, tickers_invest, country)

                def _lookup(sigs_map: dict, name: str, ticker: str) -> dict:
                    """Find signal row by ticker first, then company name."""
                    tk = ticker.upper().strip() if ticker else ""
                    return (
                        sigs_map.get(tk)
                        or sigs_map.get(name)
                        or {"c_count":0,"d_count":0,"capex_count":0,"best_quote":None,"last_filing":None}
                    )

                # Build merged lookup
                sig_rows = {}
                for name, meta in co_data.items():
                    this_r  = _lookup(this_sigs,  name, meta.get("ticker",""))
                    prior_r = _lookup(prior_sigs, name, meta.get("ticker",""))
                    sig_rows[name] = {
                        "c_count":       int(this_r.get("c_count") or 0),
                        "d_count":       int(this_r.get("d_count") or 0),
                        "capex_count":   int(this_r.get("capex_count") or 0),
                        "prior_c_count": int(prior_r.get("c_count") or 0),
                        "best_quote":    this_r.get("best_quote"),
                        "last_filing":   str(this_r.get("last_filing") or ""),
                    }

                sig_lower = sig_rows  # already keyed by canonical name

        # Step 5: Score each company
        # max values for normalisation
        # Normalise across cohort
        max_c      = max((v.get("c_count",0)     for v in sig_rows.values()), default=1) or 1
        max_delta  = max((
            max(0, v.get("c_count",0) - v.get("prior_c_count",0))
            for v in sig_rows.values()
        ), default=1) or 1
        max_capex  = max((v.get("capex_count",0) for v in sig_rows.values()), default=1) or 1
        max_themes = max((len(s) for s in co_theme_slugs.values()), default=1) or 1

        results = []
        for name, meta in co_data.items():
            t_slugs = co_theme_slugs.get(name, set())
            sigs    = sig_rows.get(name, {
                "c_count":0,"d_count":0,"capex_count":0,
                "prior_c_count":0,"best_quote":None,"last_filing":""
            })

            c_count      = int(sigs.get("c_count") or 0)
            d_count      = int(sigs.get("d_count") or 0)
            capex_count  = int(sigs.get("capex_count") or 0)
            prior_c      = int(sigs.get("prior_c_count") or 0)
            # YoY constraint delta (how much MORE constraint evidence than prior year)
            c_delta      = max(0, c_count - prior_c)
            # Delta pct for display
            c_delta_pct  = round((c_delta / prior_c * 100), 0) if prior_c > 0 else None

            if c_count == 0 and d_count == 0 and capex_count == 0:
                continue   # no year-specific evidence at all

            # Theme quality scores
            best_focus_score = max(
                (theme_meta.get(s, {}).get("focus_score", 0.3) for s in t_slugs), default=0.3
            )
            best_conv_score = max(
                (theme_meta.get(s, {}).get("conv_score", 0.3) for s in t_slugs), default=0.3
            )
            theme_quality       = best_focus_score * best_conv_score
            theme_overlap_score = min(1.0, len(t_slugs) / max(max_themes, 1))

            # ── Core scoring components ──────────────────────────────────────
            # constraint_score: absolute count (how active is the constraint NOW)
            constraint_score = min(1.0, c_count / max_c)

            # delta_score: YoY INCREASE in constraint — companies newly seeing
            # more pressure rank higher. Biggest differentiator across years.
            delta_score = min(1.0, c_delta / max(max_delta, 1))

            # demand_ratio: demand pressing on constraint
            dc_ratio_score = min(1.0, (d_count / c_count) / 3.0) if c_count > 0 else 0.0

            # capex_score: company investing to address constraint = long-term conviction
            capex_score = min(1.0, capex_count / max(max_capex, 1))

            # ── Investability Score ──────────────────────────────────────────
            # YoY constraint DELTA (30%) is the primary year-differentiator.
            # Big-company bias is broken: NVDA always has 20 constraint signals
            # but if it had 20 last year too, delta=0 → low score this year.
            # A smaller company that SPIKED from 2→8 constraint signals gets
            # delta=6 → ranks much higher in that specific year.
            raw = (
                0.30 * delta_score         # YoY spike — the key year discriminator
              + 0.25 * constraint_score    # absolute constraint level this year
              + 0.25 * theme_quality       # NEW/ESCALATING × CONFIRMED
              + 0.10 * dc_ratio_score      # demand pressing on constraint
              + 0.05 * capex_score         # investing to solve constraint (base)
              + 0.05 * theme_overlap_score # structural depth
            )
            inv_score = round(raw * 100, 1)

            # ── Action classification ─────────────────────────────────────────
            # 🔴 ACT NOW: escalating/new + confirmed + ≥3 constraint + demand ≥ constraint
            # 🟡 RESEARCH: promising but missing one condition
            # 🔵 WATCH: has constraint evidence, persistent theme
            if (best_focus_score >= 0.90 and best_conv_score >= 0.85
                    and c_count >= 3 and d_count >= c_count):
                action = "act_now"
            elif (best_focus_score >= 0.90 and best_conv_score >= 0.65 and c_count >= 1):
                action = "research"
            elif c_count >= 1:
                action = "watch"
            else:
                action = "skip"

            if action == "skip":
                continue

            # Theme detail list
            theme_list = [
                {
                    "name":    theme_meta.get(s, {}).get("theme_name", s),
                    "focus":   theme_meta.get(s, {}).get("focus_class", ""),
                    "conv":    theme_meta.get(s, {}).get("conviction", ""),
                    "delta":   theme_meta.get(s, {}).get("delta_pct"),
                    "strength":theme_meta.get(s, {}).get("this_avg", 0),
                }
                for s in t_slugs if s in theme_meta
            ]
            theme_list.sort(key=lambda t: -(t.get("strength") or 0))

            results.append({
                "company":              name,
                "ticker":               meta.get("ticker") or "",
                "company_role":         meta.get("company_role") or "",
                "investability_score":  inv_score,
                "action":               action,
                "theme_count":          len(t_slugs),
                "themes":               theme_list[:4],
                "constraint_signals":   c_count,
                "prior_constraint":     prior_c,
                "constraint_delta":     c_delta,
                "constraint_delta_pct": c_delta_pct,
                "demand_signals":       d_count,
                "capex_signals":        capex_count,
                "best_quote":           (sigs.get("best_quote") or "")[:300],
                "last_filing":          str(sigs.get("last_filing") or ""),
                "conviction":           max(
                    (theme_meta.get(s,{}).get("conviction","emerging") for s in t_slugs),
                    key=lambda c: _CONV_SCORE.get(c, 0), default="emerging"
                ),
            })

        # capex_focus mode: boost companies with BOTH capex + constraint signals
        if capex_focus:
            max_capex_f = max((r.get("capex_signals", 0) for r in results), default=1) or 1
            for r in results:
                cap  = r.get("capex_signals", 0)
                cstr = r.get("constraint_signals", 0)
                if cap > 0 and cstr > 0:
                    boost = min(1.0, cap / max_capex_f) * 25   # up to +25 points
                    r["investability_score"] = min(100.0, r["investability_score"] + boost)
                    # Promote action tier when capex confirms the constraint thesis
                    if cstr >= 3 and cap >= 1 and r["action"] in ("watch", "research"):
                        r["action"] = "act_now"
                    elif cstr >= 1 and cap >= 1 and r["action"] == "watch":
                        r["action"] = "research"

        results.sort(key=lambda r: (
            {"act_now": 0, "research": 1, "watch": 2}.get(r.get("action", "watch"), 3),
            -r["investability_score"],
        ))
        return results[:top_n]

    except Exception as e:
        logging.error("get_investment_shortlist: %s", e, exc_info=True)
        return []


def _compute_constraint_stage(
    first_detected_str: str | None,
    quarters_with_signal: int,
    momentum_score: float,
    signal_acceleration: float,
    has_realized_margin: bool,
    has_supply_easing: bool,
    c_count: int,
    k_count: int,
) -> tuple[int, float, int, str, list[str]]:
    """World-class constraint cycle stage detection.

    The four stages mirror how every structural constraint plays out:

    Stage 1 — Emerging (most alpha):
        Constraint just starting. Management first mentions it. 1-2 quarters old.
        <30% of peer companies reporting same constraint.
        Margins not yet expanded (coming). Analyst consensus: "not aware".
        → STRONG BUY. This is NVIDIA in Q1 2022, Shakti Pumps in Q1 FY25.

    Stage 2 — Accelerating (strong alpha):
        3-9 months old. 30-70% of peers report it. Margin lift JUST starting.
        Management more explicit: "we are capacity constrained".
        → BUY. Best risk-adjusted entry. GE Vernova T&D in Q2 2024.

    Stage 3 — Peak/Consensus (shrinking alpha):
        9-18 months old. 70%+ of peers report it. Margins already expanded.
        Sell-side analysts initiating coverage. "Widely known".
        → HOLD. Dixon in 2023, Waaree in Q3 2024.

    Stage 4 — Resolving (negative alpha):
        18+ months old. Supply easing visible. Margins flattening/compressing.
        Competitor capacity coming online. Constraint thesis closing.
        → REDUCE/EXIT.

    Returns: (stage, stage_confidence, time_horizon_months, conviction_tier, exit_triggers)
    """
    from datetime import date as _date
    import math as _math

    # Compute age in months
    age_months = 0
    if first_detected_str:
        try:
            fd = _date.fromisoformat(str(first_detected_str)[:10])
            age_months = max(0, round((_date.today() - fd).days / 30))
        except Exception:
            age_months = 0

    # Normalize signals to 0-1 indicators
    is_accelerating    = signal_acceleration > 0.3   # momentum rising fast
    has_strong_capex   = k_count >= 2
    has_margin         = has_realized_margin

    # Stage determination (ordered by priority)
    exit_triggers: list[str] = []

    if has_supply_easing:
        exit_triggers.append("supply_easing_detected")
    if age_months > 18 and momentum_score < 40:
        exit_triggers.append("momentum_fading_after_18m")
    if has_margin and age_months > 12:
        exit_triggers.append("margin_expansion_late_stage")

    # --- Stage 4: Resolving ---
    if (has_supply_easing
            or (age_months > 18 and momentum_score < 30)
            or (age_months > 24)):
        stage = 4
        stage_conf = 0.75 if has_supply_easing else 0.60
        time_horizon = 6
        conviction = "reduce"

    # --- Stage 3: Peak/Consensus ---
    elif (age_months > 9 and not is_accelerating) or (age_months > 12 and has_margin):
        stage = 3
        stage_conf = 0.65
        time_horizon = 12
        conviction = "hold"
        if has_margin and age_months > 15:
            exit_triggers.append("pricing_power_mature_watch_compression")

    # --- Stage 2: Accelerating ---
    elif (3 <= age_months <= 9 and (is_accelerating or c_count >= 4)) \
            or (quarters_with_signal >= 2 and c_count >= 3):
        stage = 2
        stage_conf = 0.72
        time_horizon = 18
        conviction = "buy"
        if has_strong_capex:
            time_horizon = 24   # capex commitment extends the runway

    # --- Stage 1: Emerging (highest alpha) ---
    else:
        stage = 1
        stage_conf = 0.55 + min(0.35, (c_count * 0.05))  # more signals = more confident
        time_horizon = 30
        conviction = "strong_buy"
        if k_count == 0:
            time_horizon = 24  # no capex = shorter visibility

    # Adjust confidence for quality signals
    if has_margin:
        stage_conf = min(0.92, stage_conf + 0.10)
    if has_strong_capex:
        stage_conf = min(0.90, stage_conf + 0.08)

    # Map conviction to readable tier
    tier_label = {
        "strong_buy": "🔴 STRONG BUY — Stage 1, early edge",
        "buy":        "🟡 BUY — Stage 2, accelerating",
        "hold":       "🟢 HOLD — Stage 3, consensus forming",
        "reduce":     "⚫ REDUCE — Stage 4, thesis closing",
    }.get(conviction, "🟡 BUY")

    return stage, round(stage_conf, 2), time_horizon, tier_label, exit_triggers


def _generate_auto_thesis(
    company: str,
    ticker: str,
    theme: str,
    constraint_stage: int,
    conviction_tier: str,
    constrained_component: str,
    best_quote: str,
    capex_quote: str,
    c_count: int,
    k_count: int,
    avg_confidence: float,
    time_horizon_months: int,
    seller_ratio: float = 0.0,
    has_supply_easing: bool = False,
) -> str:
    """Auto-generate a 2-3 sentence investment thesis in plain English.

    No Claude API call — purely rule-based from signal data.
    This is the 'why should I invest' explanation every stock needs.
    """
    stage_narrative = {
        1: "is experiencing the EARLY STAGE of a structural constraint — before analyst consensus and price discovery.",
        2: "is in an ACCELERATING constraint cycle with signals building across multiple quarters.",
        3: "is a well-established constraint play — thesis is confirmed but consensus is forming.",
        4: "constraint is maturing — supply is beginning to ease. Monitor for thesis closure.",
    }.get(constraint_stage, "shows constraint signals.")

    component_str = f" in {constrained_component}" if constrained_component else ""
    quote_str = f' Management explicitly stated: "{best_quote[:180]}"' if best_quote else ""

    # ── Identify which pathway the company is on ─────────────────────────
    # There are two ways a constrained supplier monetises the shortage:
    #
    # Pathway A — PRICING POWER (scarcity → higher ASP)
    #   When capacity can't be added quickly (long fab lead times, complex manufacturing)
    #   Examples: NVIDIA (2-3 year TSMC ramp), Micron HBM
    #   Signals: realized_margin_expansion, pricing_power_emerging
    #
    # Pathway B — VOLUME EXPANSION (scarcity → new capacity → revenue from volume)
    #   When demand certainty justifies building, and company CAN build
    #   Examples: GE Vernova T&D India, Waaree Energies, Shakti Pumps, Comfort Systems
    #   Signals: capex_increase + demand_surge + tender_pipeline
    #
    # Pathway C — BOTH (highest conviction, longest runway)
    #   Examples: Parker-Hannifin, Microchip Technology, LRCX
    #   Signals: all of the above

    has_capex   = k_count >= 1
    comp_str    = f" in {constrained_component}" if constrained_component else ""

    if c_count >= 3 and k_count >= 2:
        # Pathway C: constraint confirmed + capacity being built = revenue from VOLUME
        action_sentence = (
            f"CONSTRAINT + CAPACITY EXPANSION{comp_str}: {c_count} supply constraint signals confirm "
            f"demand exceeds current output. Management has committed {k_count} capex signals — "
            f"they're building to capture the opportunity. Revenue grows from volume as new "
            f"capacity comes online. This is the GE Vernova/Waaree/Parker-Hannifin playbook."
        )
    elif c_count >= 2 and k_count >= 1:
        action_sentence = (
            f"Constraint + capacity investment confirmed{comp_str}: {c_count} signals show "
            f"the company cannot meet current demand, and {k_count} capex signal(s) confirm "
            f"they're responding by expanding. Revenue grows as new capacity is delivered."
        )
    elif c_count >= 2 and k_count == 0:
        action_sentence = (
            f"Supply constraint confirmed{comp_str}: {c_count} seller-perspective signals "
            f"(confidence {avg_confidence:.0%}) show demand exceeding capacity. No capex announced yet — "
            f"watch for capacity expansion or pricing announcements. Early stage, high alpha window."
        )
    elif k_count >= 2:
        action_sentence = (
            f"Capacity expansion play{comp_str}: {k_count} capex signals show the company is "
            f"investing to meet demand. First constraint signal confirms supply is genuinely tight. "
            f"Revenue grows as new capacity comes online over the next {time_horizon_months} months."
        )
    else:
        action_sentence = (
            f"Early constraint signal detected{comp_str}: first evidence of supply-demand "
            f"imbalance. Monitor for capex announcements (volume expansion) or pricing "
            f"announcements (ASP expansion) to confirm which pathway this company will take."
        )

    if has_supply_easing:
        exit_str = " ⚠️ Monitor: supply easing signals detected — reduce if confirmed."
    elif time_horizon_months >= 24:
        exit_str = f" Thesis has {time_horizon_months}-month runway before expected resolution."
    else:
        exit_str = f" Time horizon: {time_horizon_months} months. Watch for exit signals."

    return f"{company} ({ticker}) {stage_narrative} {action_sentence}{quote_str}{exit_str}"


@app.get("/api/quality-compounders")
def get_quality_compounders(
    country: str = "IN",
    year: int | None = None,
    min_quality_score: float = 0.35,
) -> dict:
    """Tier 1 — Multi-decade quality compounder detection.

    Finds companies with:
    - High ROIC + reinvestment (compounding engine)
    - Competitive moat (brand, distribution, IP, switching costs)
    - Expanding TAM (long runway)
    - Capital-disciplined management

    These are 5-20 year holds. Different from Tier 2 (constraint plays).
    This is how Jhunjhunwala found Titan, Lynch found Dunkin Donuts,
    Buffett found Coca-Cola — BEFORE they were famous.
    """
    pg = get_pg()
    if not pg:
        return {}
    try:
        yr     = year or date.today().year
        to_d   = date(yr, 12, 31) if yr < date.today().year else date.today()
        from_d = date(yr - 2, 1, 1)   # 3 years of signals for sustainability check

        # Get signals including quality signal types
        quality_signal_types = [
            "roic_high_sustained", "roic_reinvestment", "earnings_quality_high",
            "competitive_moat", "tam_expansion_structural", "management_quality",
            "margin_sustainability", "pricing_power_emerging", "realized_margin_expansion",
            "supply_concentration", "demand_surge", "capex_increase", "market_entry",
        ]
        signal_records = pg.get_all_signals_in_window(
            signal_types=quality_signal_types,
            since_date=from_d,
            as_of_date=to_d,
            country=country,
        )

        from makrograph.ranking.quality_ranker import QualityRanker
        ranker  = QualityRanker(min_signals=2, min_quality_score=min_quality_score)
        results = ranker.rank(signal_records=signal_records, country=country)

        # Filter: Tier 1 quality compounders should be capital-goods / technology /
        # industrial / financial services companies — not restaurants, retail, apparel.
        # Quality signals (moat, ROIC) fire on McDonald's brand language which is
        # technically valid for a pure quality framework but not investable in the
        # constraint+quality lens we're building.
        _TIER1_BLOCKED = frozenset({
            "retail", "consumer_goods", "food", "beverage", "restaurant",
            "apparel", "cosmetic", "cannabis", "entertainment", "media",
            "airline", "hotel", "homebuilder",
        })
        try:
            from makrograph.themes import beneficiary_mapper as _bm
            def _t1_allowed(ticker: str, company: str) -> bool:
                s = _bm._KNOWN_TICKER_SECTORS.get(ticker.upper(), "")
                if s in _TIER1_BLOCKED:
                    return False
                co = company.lower()
                for frag, sector in _bm._COMPANY_NAME_SECTOR_PATTERNS:
                    if frag in co and sector in _TIER1_BLOCKED:
                        return False
                return True
        except Exception:
            def _t1_allowed(ticker, company): return True

        tier1      = [r for r in results if r.investment_tier == "tier_1" and _t1_allowed(r.ticker, r.company_name)]
        tier1_watch= [r for r in results if r.investment_tier in ("tier_1","tier_1_watch") and _t1_allowed(r.ticker, r.company_name) and r not in tier1]

        def _to_dict(r) -> dict:
            return {
                "ticker":           r.ticker,
                "company":          r.company_name,
                "quality_score":    r.quality_score,
                "investment_tier":  r.investment_tier,
                "tier_confidence":  r.tier_confidence,
                "roic_score":       r.roic_score,
                "moat_score":       r.moat_score,
                "tam_score":        r.tam_score,
                "management_score": r.management_score,
                "signal_count":     r.signal_count,
                "signal_quarters":  r.signal_quarters,
                "best_roic_quote":  r.best_roic_quote,
                "best_moat_quote":  r.best_moat_quote,
                "best_tam_quote":   r.best_tam_quote,
                "signal_types":     r.signal_types_found,
                "quality_thesis":   r.quality_thesis,
                "suggested_hold":   "5-20 years" if r.investment_tier == "tier_1" else "2-5 years",
            }

        return {
            "year":        yr,
            "country":     country,
            "period":      f"{from_d} → {to_d}",
            "tier_1_count":       len(tier1),
            "tier_1_watch_count": len(tier1_watch),
            "tier_1":             [_to_dict(r) for r in tier1],
            "tier_1_watch":       [_to_dict(r) for r in tier1_watch],
            "all_ranked":         [_to_dict(r) for r in results],
            "generated_at":       datetime.now().isoformat(),
        }
    except Exception as e:
        logging.error("get_quality_compounders: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/today")
def get_todays_opportunities(
    country: str = "IN",
    as_of_year: int | None = None,
) -> dict:
    """THE master endpoint. Single source of truth for investment opportunities.

    Returns a clean, unified list of stocks grouped by conviction stage.
    This is what the user sees every morning — no tab-hopping required.

    Each stock includes:
    - auto_thesis: 2-3 sentence plain-English investment case
    - constraint_stage: 1 (early) to 4 (resolving)
    - conviction_tier: STRONG BUY / BUY / HOLD / REDUCE
    - full evidence trail: quotes, signals, capex, theme
    - exit_triggers: what to watch to reduce position
    """
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")

    try:
        yr = as_of_year or date.today().year

        # Get the full investment funnel (Tier 2 + Tier 3)
        shortlist_data = get_investment_final_shortlist(
            country=country,
            year=yr,
            min_constraint_signals=1,
            require_capex=False,
            min_avg_confidence=0.65,
        )

        # Get Tier 1 quality compounders (runs in parallel conceptually)
        tier1_data: dict = {}
        try:
            tier1_data = get_quality_compounders(country=country, year=yr, min_quality_score=0.30)
        except Exception as e:
            logging.warning("today: tier1 failed: %s", e)

        stocks   = shortlist_data.get("final_shortlist", [])
        regimes  = shortlist_data.get("constraint_regimes", [])
        stats    = shortlist_data.get("stats", {})

        # Generate auto-thesis for every stock
        for s in stocks:
            s["auto_thesis"] = _generate_auto_thesis(
                company             = str(s.get("company","") or ""),
                ticker              = str(s.get("ticker","") or ""),
                theme               = str(s.get("theme","") or ""),
                constraint_stage    = int(s.get("constraint_stage", 3)),
                conviction_tier     = str(s.get("conviction_tier","") or ""),
                constrained_component = str(s.get("constrained_component","") or ""),
                best_quote          = str(s.get("best_constraint_quote","") or ""),
                capex_quote         = str(s.get("capex_quote","") or ""),
                c_count             = int(s.get("constraint_signals", 0)),
                k_count             = int(s.get("capex_signals", 0)),
                avg_confidence      = float(s.get("avg_confidence", 0)),
                time_horizon_months = int(s.get("time_horizon_months", 12)),
                has_supply_easing   = bool(s.get("has_supply_easing", False)),
            )

        # Group by conviction stage
        by_stage: dict[int, list] = {1: [], 2: [], 3: [], 4: []}
        for s in stocks:
            stage = int(s.get("constraint_stage", 3))
            by_stage.setdefault(stage, []).append(s)

        # Summary counts
        summary = {
            "total":         len(stocks),
            "stage1_count":  len(by_stage.get(1, [])),
            "stage2_count":  len(by_stage.get(2, [])),
            "stage3_count":  len(by_stage.get(3, [])),
            "stage4_count":  len(by_stage.get(4, [])),
            "active_regimes": len(regimes),
            "as_of":         date.today().isoformat(),
            "year":          yr,
            "country":       country,
        }

        return {
            "summary":               summary,
            # Tier 1: Multi-decade quality compounders (5-20 year holds)
            "tier1_compounders":     tier1_data.get("tier_1", []),
            "tier1_watch":           tier1_data.get("tier_1_watch", []),
            # Tier 2: Constraint cycle winners (2-5 year holds) grouped by stage
            "stage1_strong_buy":     by_stage.get(1, []),
            "stage2_buy":            by_stage.get(2, []),
            "stage3_hold":           by_stage.get(3, []),
            "stage4_reduce":         by_stage.get(4, []),
            "constraint_regimes":    regimes,
            "all_stocks":            stocks,
            "generated_at":          datetime.now().isoformat(),
        }

    except Exception as e:
        logging.error("get_todays_opportunities: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


CONSTRAINT_PRED_SQL = """(
    s.signal_type = 'capacity_constraint_seller'
    OR (s.signal_type = 'capacity_utilization_high'
        AND COALESCE(s.perspective,'neutral') != 'buyer'
        -- The extractor fires this on ANY capacity table in a
        -- filing; only count it when the text states TIGHTNESS.
        AND (s.context_text ILIKE '%%full capacity%%'
             OR s.context_text ILIKE '%%fully utilized%%'
             OR s.context_text ILIKE '%%fully utilised%%'
             OR s.context_text ILIKE '%%high utilization%%'
             OR s.context_text ILIKE '%%high utilisation%%'
             OR s.context_text ILIKE '%%peak utilization%%'
             OR s.context_text ILIKE '%%peak utilisation%%'
             OR s.context_text ILIKE '%%optimal utilization%%'
             OR s.context_text ILIKE '%%optimum utilisation%%'
             OR s.context_text ILIKE '%%running at capacity%%'
             OR s.context_text ILIKE '%%capacity is full%%'))
    OR (s.signal_type IN (
            'supply_bottleneck','inventory_drawdown',
            'capacity_shortage','demand_exceeds_supply',
            'backlog_duration'
        )
        AND (
            COALESCE(s.perspective,'neutral') = 'seller'
            OR s.context_text ILIKE '%%our capacity%%'
            OR s.context_text ILIKE '%%our backlog%%'
            OR s.context_text ILIKE '%%order book%%'
            OR s.context_text ILIKE '%%order inflow%%'
            OR s.context_text ILIKE '%%book-to-bill%%'
            OR s.context_text ILIKE '%%capacity utilization%%'
            OR s.context_text ILIKE '%%cannot meet demand%%'
            OR s.context_text ILIKE '%%cant meet demand%%'
            OR s.context_text ILIKE '%%fully booked%%'
            OR s.context_text ILIKE '%%fully allocated%%'
            OR s.context_text ILIKE '%%sold out%%'
            OR s.context_text ILIKE '%%our lead time%%'
            OR s.context_text ILIKE '%%waiting list%%'
            OR s.context_text ILIKE '%%oversubscribed%%'
            OR s.context_text ILIKE '%%customers waiting%%'
        )
    )
)"""
# Demand-led evidence needs an ORDER-language anchor: constrained
# suppliers sell through order books / order inflow / backlog
# ("orders +173%", "order book at 3x revenue"). A consumer or
# service business with surging demand (travel bookings, loan
# growth, same-store sales) never phrases demand as orders —
# this one word family separates GE Vernova from DoorDash.
DS_SELLER_PRED_SQL = """(
    s.signal_type = 'demand_surge'
    AND COALESCE(s.perspective,'') = 'seller'
    AND (s.context_text ILIKE '%%orders%%'
         OR s.context_text ILIKE '%%order book%%'
         OR s.context_text ILIKE '%%order inflow%%'
         OR s.context_text ILIKE '%%order intake%%'
         OR s.context_text ILIKE '%%order backlog%%'
         OR s.context_text ILIKE '%%backlog%%'
         OR s.context_text ILIKE '%%book-to-bill%%')
)"""


# ── Domain keyword families ───────────────────────────────────────────────────
# Generic industry vocabulary (no companies, no tickers). Used for:
#   1. theme ↔ company evidence matching
#   2. peer corroboration (companies talking about the same constrained domain)
#   3. supply-chain propagation (constrained domain → upstream components)
THEME_KW_FAMILIES: dict[str, list[str]] = {
    "wafer":         ["wafer","semiconductor","chip","foundry","fab ","fabs"],
    "chip":          ["wafer","semiconductor","chip","foundry","fab "],
    "foundry":       ["wafer","semiconductor","chip","foundry","fab "],
    "asic":          ["wafer","semiconductor","chip","asic"],
    "semiconductor": ["wafer","semiconductor","chip","foundry","fab "],
    "aerospace":     ["aerospace","aircraft","defense","defence","aviation","engine","space"],
    "defense":       ["defense","defence","aerospace","military","missile","aircraft"],
    "defence":       ["defense","defence","aerospace","military","missile","aircraft"],
    "cloud":         ["cloud","data center","datacenter","hyperscale","server"],
    "data center":   ["cloud","data center","datacenter","hyperscale","server"],
    "solar":         ["solar","photovoltaic","module","renewable"],
    "wind":          ["wind","turbine","renewable"],
    "power":         ["power","grid","transmission","substation","transformer","electricity","switchgear"],
    "grid":          ["power","grid","transmission","substation","transformer","electricity"],
    "transformer":   ["transformer","transmission","substation","switchgear"],
    "battery":       ["battery","battery cell","energy storage","lithium"],
    "cement":        ["cement","concrete","aggregates"],
    "railway":       ["railway","rail","wagon","locomotive","metro"],
    "robotic":       ["robot","automation","cnc","precision"],
    "steel":         ["steel","metal","forging","alloy"],
    "industrial":    ["industrial","manufacturing","machinery","automation"],
    "medical":       ["medical","healthcare","device","diagnostic"],
    "pharma":        ["pharma","drug","api ","formulation"],
    "telecom":       ["telecom","fiber","spectrum","network"],
    "cable":         ["cable","wire","conductor","fiber"],
}

# Supply-chain propagation: when a domain is CONFIRMED constrained (multiple
# independent companies), its upstream input domains deserve a WATCH flag —
# their constraint typically shows up in filings one or more quarters later.
# Component-level industrial ontology, fully generic.
UPSTREAM_COMPONENTS: dict[str, list[str]] = {
    "transformer":   ["crgo steel","electrical steel","copper","lamination","insulation","bushing"],
    "power":         ["transformer","conductor","cable","tower","insulator","switchgear","crgo"],
    "grid":          ["transformer","conductor","cable","tower","insulator","switchgear"],
    "wafer":         ["polysilicon","photoresist","lithography","quartz","gases","substrate"],
    "semiconductor": ["wafer","polysilicon","photoresist","lithography","substrate","packaging"],
    "data center":   ["transformer","generator","cooling","hvac","optical","copper","switchgear","ups "],
    "cloud":         ["transformer","generator","cooling","optical","server","memory"],
    "aerospace":     ["forging","casting","titanium","fastener","avionics","composite","engine"],
    "defense":       ["forging","casting","electronics","propellant","optics","radar"],
    "solar":         ["polysilicon","wafer","glass","inverter","module","silver paste"],
    "wind":          ["gearbox","blade","casting","forging","bearing","generator"],
    "battery":       ["lithium","cathode","anode","separator","electrolyte","copper foil"],
    "railway":       ["wheel","axle","forging","casting","signalling","wagon"],
    "cement":        ["clinker","limestone","petcoke","grinding"],
}


def _trim_to_sentence(text: str, max_len: int = 300) -> str:
    """Clean quote edges: drop a leading word fragment (context windows often
    start mid-word: 'ctors on both a regional...') and end on a sentence or
    clause boundary instead of cutting mid-word."""
    if not text:
        return ""
    t = text.strip()
    # Leading fragment: first token isn't a real word start (lowercase start
    # and the fragment is short) — drop through the first space.
    first = t.split(" ", 1)
    if len(first) == 2 and first[0] and first[0][0].islower() and len(first[0]) <= 4:
        t = first[1].lstrip()
    if len(t) > max_len:
        cut = t[:max_len]
        # Prefer sentence end, then clause break, then last full word
        for sep in (". ", "; ", ", "):
            pos = cut.rfind(sep)
            if pos > max_len * 0.5:
                return cut[:pos + 1].strip()
        pos = cut.rfind(" ")
        return (cut[:pos] + "…") if pos > 0 else cut
    return t


def _quality_quote(raw: str, max_len: int = 300) -> str:
    """Return the quote only if it contains real operational/supply language.

    Filters out SEC legal boilerplate that accidentally matched NLP patterns:
    - bond issuance / securities registration language
    - accounting / GAAP boilerplate
    - pure risk-factor disclaimers with no operational content
    Returns empty string for low-quality quotes so the UI shows nothing
    rather than misleading legal text.
    """
    if not raw:
        return ""
    text = raw.strip()
    lower = text.lower()

    # ── Language-based rejection (company-agnostic) ──────────────────────────
    # Every rejection rule here is based purely on WHAT THE TEXT SAYS,
    # never on WHO wrote it. These patterns catch the same bad language
    # regardless of whether it's DLR, SPG, Boeing, GM or any future company.

    # Rule 1: Securities / bond issuance language
    # Any filing text about offering notes/bonds to investors is not supply constraint.
    # Matches: "Euro Notes were sold outside the US in reliance on Regulation S"
    #          "Securities Act of 1933", "exempt from registration"
    _SECURITIES_OFFERING = [
        "securities act of 1933", "securities act of 1934",
        "regulation s", "in reliance on regulation",
        "exempt from registration", "isin ", "cusip ",
        "sold outside the united states",
        "placement memorandum", "offering memorandum",
        "underwriting agreement",
    ]
    if any(ind in lower for ind in _SECURITIES_OFFERING):
        return ""

    # Rule 2: Partnership / REIT financial distribution language
    # Any company structured as a partnership or REIT uses this language in 10-K
    # financial statements — it describes HOW PROFITS ARE SPLIT, not supply constraints.
    # Matches: "Net income available to Partners", "General Partner", "Limited Partners"
    #          "Operating Partnership", "preferred units"
    _PARTNERSHIP_FINANCIAL = [
        "net income available to partners",
        "income (loss) available to partners",
        "general partner $", "limited partners -",
        "limited partners allocated",
        "operating partnership after preferred",
        "preferred distribution", "preferred units",
        "noncontrolling interests. we allocate",
        "allocate net operating results",
    ]
    if any(ind in lower for ind in _PARTNERSHIP_FINANCIAL):
        return ""

    # Rule 3: Accounting / tax boilerplate
    # Tax basis, goodwill impairment, GAAP reconciliation — not supply signals.
    # Matches: "excess of book basis over tax basis", "EBITDA measures exclude GAAP"
    _ACCOUNTING_BOILERPLATE = [
        "book basis over tax basis", "tax basis over book basis",
        "ebitda measures exclude", "gaap charges", "non-gaap",
        "goodwill impairment", "forward-looking statements",
        "safe harbor", "equity-based compensation",
        "limited partnership units",
        # M&A purchase price allocation — not supply constraint
        "fair value of the acquired assets",
        "acquired assets and assumed liabilities",
        "purchase price allocation", "purchase accounting",
        # Software/IP royalty language — not industrial supply constraint
        "sales-based royalty", "royalty payments from the remaining",
        "intellectual property license", "software license fee",
        # Risk-factor disclaimer phrasing (forward-looking, not operational)
        "may not continue", "may slow or may not", "growth may slow",
        "may decelerate", "no assurance that",
        "no guarantee that", "there can be no assurance",
    ]
    if any(ind in lower for ind in _ACCOUNTING_BOILERPLATE):
        return ""

    # Rule 4: Product liability / recall / legal settlement language
    # Recalls, inflators, Max groundings — these are liability events, not supply constraints.
    # Matches: "Takata inflators", "737 MAX aircraft", "product recall"
    _LIABILITY_LANGUAGE = [
        "product recall", "safety recall", "recall campaign",
        "recalled certain vehicles", "recall certain",
        "class action", "settlement agreement", "legal settlement",
        "litigation reserve", "contingent liability",
        "inflator", "airbag recall",
        # Aircraft grounding — backlog exists but aircraft can't be delivered
        "remain grounded", "are grounded", "grounding of",
        "at delivery and acceptance of",  # contract accounting trigger point, not supply
    ]
    if any(ind in lower for ind in _LIABILITY_LANGUAGE):
        return ""

    # Rule 5: Customer concentration / segment reporting cross-references
    # "Customer accounted for >10% of revenues" = concentration risk disclosure
    # "See Note 14. Segment and Geographic Reporting" = cross-reference, no content
    _CROSS_REFERENCE = [
        "accounted for greater than 10%",
        "accounted for more than 10%",
        "represented more than 10%",
        "see note ", "refer to note ", "further information on reporting",
        "for further information on segment",
        # Accounting segment restructuring — "costs fully allocated to each reportable segment"
        "fully allocated to each reportable segment",
        "allocated to each reportable segment based on",
        "previously included in unallocated expenses",
        # IT risk factor cross-lists — list of risks that happen to mention "backlog converts"
        "failure to implement system enhanc",
        "failure to implement technology",
        "communications systems or the failure",
    ]
    if any(ind in lower for ind in _CROSS_REFERENCE):
        return ""

    # Rule 5b: Regulatory safety / compliance ratings (not supply constraint)
    # Vehicle safety ratings, environmental certifications, drug approvals
    # Matches: "EPA ratings as determined by NHTSA" (Tesla safety compliance)
    _SAFETY_REGULATORY = [
        "new car assessment program",
        "nhtsa", "nhts",
        "safety rating", "safety compliance",
        "epa rating", "emissions rating",
        "fuel economy rating",
        "sold outside of the u.s. are subject to similar foreign compliance",
    ]
    if any(ind in lower for ind in _SAFETY_REGULATORY):
        return ""

    # Rule 5c: Backlog explicitly DECLINING = easing, not constraint
    # "backlog to reduce", "backlog declining", "expect backlog to decrease"
    # This company is Stage 4 (resolving), not Stage 1 (emerging)
    _BACKLOG_EASING = [
        "backlog to reduce",
        "og to reduce as fiscal",   # truncated "backlog to reduce" from context window
        "backlog will reduce",
        "expect our backlog to decrease",
        "expect backlog to decrease",
        "backlog has decreased",
        "backlog declined",
        "reduction in backlog",
        "working through our backlog",
        "normalizing backlog",
        "backlog normalization",
    ]
    if any(ind in lower for ind in _BACKLOG_EASING):
        return ""

    # Rule 6: Explicit negation — "does not have significant backlog"
    # A company explicitly saying they have NO backlog is not a constraint signal.
    _EXPLICIT_NEGATION = [
        "does not have significant backlog",
        "does not maintain a backlog",
        "we do not have a backlog",
        "backlog is not significant",
        "nature of its business does not",
        "nature of the business does not",
    ]
    if any(ind in lower for ind in _EXPLICIT_NEGATION):
        return ""

    # Rule 7: Buyer-perspective constraint (company is HURT, not advantaged)
    # The constraint is on their INPUTS or CUSTOMERS, not their own capacity.
    _BUYER_INDICATORS = [
        # Direct adverse outcome language
        "adverse effect", "adversely affect", "negatively impact",
        "harm our", "hurt our", "reduce our revenue",
        "result in lower", "result in reduced", "decline in demand",
        "negatively impact our backlog",    # adverse effect on THEIR backlog
        "reduce our backlog", "impact our backlog",
        # Third-party is the constrained party, not the company
        "contractors to experience", "customers to experience",
        "our customers face", "customers are experiencing",
        "customers cannot", "customers may not be able",
        # Input shortage — company is the BUYER of constrained goods
        "unable to source", "unable to procure", "difficulty procuring",
        "shortage of components", "shortage of raw material",
        "shortage of skilled workers, resulting in",
        "labor shortage", "worker shortage", "staffing shortage",
        "our suppliers", "from our suppliers", "supplier cannot",
        # BUYER navigating a constrained environment (CSCO, DELL, MMM pattern)
        # "we face competition for components that are supply-constrained" = buyer
        # "navigate environments with constrained supply chains" = buyer
        "face competition for certain components",
        "navigate environments with constrained supply",
        "navigate a constrained supply",
        "raw material price inflation and constrained supply",
        "constrained supply throughout the global marketplace",
        "experienced raw material", "raw material constrained",
        # Financial services non-operational language
        "as a broker or investment advisor",
        "in our capacity as a broker",
        "investment advisor",
        # Compliance risk (not supply constraint)
        "resource allocation limitations",
        "lack of vendor cooperation",
        "regulatory requirement", "regulatory approval",
        # Hypothetical risk factors
        "if we are unable to meet", "if we fail to meet",
        "we may be unable to meet",
        "we may experience reduced customer demand or constrained supply",
        "we may experience changes in customer demand or constrained supply",
        "could adversely", "may adversely", "might adversely",
        "due to fluctuating", "due to our foundry", "due to our supplier",
    ]
    if any(ind in lower for ind in _BUYER_INDICATORS):
        return ""

    # Require genuine SELLER-perspective operational language
    # These indicate the COMPANY itself is the constrained supplier
    _SELLER_WORDS = [
        "our backlog", "our capacity", "our lead time", "our production",
        "backlog increased", "backlog grew", "backlog at record",
        "fully booked", "fully allocated", "sold out",
        "cannot meet", "unable to meet", "supply constrained",
        "supply-constrained", "remain constrained", "at capacity",
        "at full capacity", "operating at", "utilization",
        "non-cancellable", "placed orders in advance",
        "customers ordering", "customers waiting",
        "order backlog", "growing backlog", "record backlog",
    ]
    if not any(w in lower for w in _SELLER_WORDS):
        # Fall back: allow if it has basic operational language
        _OPERATIONAL_FALLBACK = [
            "backlog", "lead time", "capacity", "shortage",
            "constrain", "allocat", "capex", "capital expenditure",
            # order-book vocabulary — how constrained suppliers express demand
            "orders", "order book", "order inflow", "order intake",
            "book-to-bill",
        ]
        if not any(w in lower for w in _OPERATIONAL_FALLBACK):
            return ""

    return text[:max_len]


def _save_shortlist_snapshot(country: str, results: list[dict]) -> None:
    """One row per company per run-day. Same-day reruns overwrite (idempotent)."""
    pg = get_pg()
    if pg is None or not results:
        return
    from datetime import date as _d
    with pg._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mg_shortlist_snapshots (
                id            BIGSERIAL PRIMARY KEY,
                snapshot_date DATE        NOT NULL,
                country       VARCHAR(4)  NOT NULL,
                ticker        TEXT        NOT NULL,
                company       TEXT,
                stage         INT,
                list_tier     TEXT,
                trajectory    TEXT,
                rank_score    DOUBLE PRECISION,
                explosion_legs INT,
                peer_corroboration INT,
                exit_triggers TEXT[],
                created_at    TIMESTAMPTZ DEFAULT now(),
                UNIQUE (snapshot_date, country, ticker)
            )""")
        for r in results:
            cur.execute(
                """INSERT INTO mg_shortlist_snapshots
                   (snapshot_date, country, ticker, company, stage, list_tier,
                    trajectory, rank_score, explosion_legs, peer_corroboration,
                    exit_triggers)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (snapshot_date, country, ticker) DO UPDATE SET
                     stage=EXCLUDED.stage, list_tier=EXCLUDED.list_tier,
                     trajectory=EXCLUDED.trajectory, rank_score=EXCLUDED.rank_score,
                     explosion_legs=EXCLUDED.explosion_legs,
                     peer_corroboration=EXCLUDED.peer_corroboration,
                     exit_triggers=EXCLUDED.exit_triggers""",
                (_d.today(), country, (r.get("ticker") or "").upper(),
                 r.get("company"), r.get("constraint_stage"), r.get("list_tier"),
                 r.get("trajectory"), r.get("rank_score"), r.get("explosion_legs"),
                 r.get("peer_corroboration"), r.get("exit_triggers") or []),
            )
        conn.commit()


@app.get("/api/breakout-scan")
def get_breakout_scan(
    country: str = "IN",
    year: int | None = None,
    as_of: str | None = None,
    top_n: int = 50,
) -> dict:
    """Price/volume READINESS scan over the fundamentally shortlisted stocks.

    Philosophy: fundamentals pick the NAMES (the constraint engine), price
    action picks the MOMENT. Takes the shortlist for the chosen year, reads
    NSE daily bars (close + volume + delivery %%) up to `as_of`, classifies:

        breakout  — closed above its 60-day base on expanded volume within
                    the last ~7 sessions (the move has started)
        coiling   — within ~7%% of 52w high, tight 20d range, volume drying
                    up / delivery %% rising ('about to break out')
        basing    — constructive but not yet tight
        extended  — >150%% 1y run-up; the easy part may be over
        no_setup  — fundamentals present, chart not ready

    jump_score blends fundamental rank_score (55%%) with price readiness (45%%).
    """
    from datetime import date as _date, timedelta as _td

    pg = get_pg()
    if pg is None:
        raise HTTPException(status_code=503, detail="DB unavailable")
    if country != "IN":
        raise HTTPException(status_code=400, detail="Price data currently available for IN only")

    base = get_investment_final_shortlist(country=country, year=year)
    shortlist = base.get("final_shortlist", [])
    if not shortlist:
        return {"as_of": as_of, "year": base.get("year"), "stocks": [],
                "note": base.get("data_note") or "empty shortlist"}
    fund = {(c.get("ticker") or "").upper(): c for c in shortlist}
    tickers = [t for t in fund if t]

    with pg._conn() as conn:
        cur = conn.cursor()
        if as_of:
            asof_d = _date.fromisoformat(as_of[:10])
        else:
            cur.execute("SELECT MAX(trade_date) FROM nse_bhavcopy_data")
            asof_d = cur.fetchone()[0]
        cur.execute(
            """SELECT symbol, trade_date, close::float, high::float, low::float,
                      tottrdqty::float AS vol, COALESCE(delivery_pct,0)::float AS dlv
               FROM nse_bhavcopy_data
               WHERE symbol = ANY(%s) AND series IN ('EQ','BE')
                 AND trade_date <= %s AND trade_date > %s
               ORDER BY symbol, trade_date""",
            (tickers, asof_d, asof_d - _td(days=460)),
        )
        series: dict[str, list] = {}
        for sym, dt, close, high, low, vol, dlv in cur.fetchall():
            series.setdefault(sym, []).append((dt, close, high, low, vol, dlv))

    def _analyze(rows: list) -> dict | None:
        if len(rows) < 60:
            return None
        # Truncate at the most recent suspected split/bonus (>38%% one-day gap
        # in unadjusted bhavcopy data) so 52w-high / run-up metrics don't span
        # a corporate action. If too little post-split history remains, skip.
        _cl = [r[1] for r in rows]
        _split_at = None
        for _i in range(1, len(_cl)):
            if _cl[_i - 1] > 0 and _cl[_i] / _cl[_i - 1] < 0.62:
                _split_at = _i
        had_split = _split_at is not None
        if had_split:
            rows = rows[_split_at:]
            if len(rows) < 60:
                return None
        dts    = [r[0] for r in rows]
        closes = [r[1] for r in rows]
        highs  = [r[2] for r in rows]
        lows   = [r[3] for r in rows]
        vols   = [r[4] for r in rows]
        dlvs   = [r[5] for r in rows]
        last   = closes[-1]
        n      = len(closes)
        lb     = min(250, n)
        hi52   = max(highs[-lb:])
        dist_high = round((hi52 - last) / hi52 * 100, 1) if hi52 else 99.0
        rng20  = round((max(highs[-20:]) - min(lows[-20:])) / last * 100, 1)
        rng_prior = ((max(highs[-60:-20]) - min(lows[-60:-20])) / last * 100) if n >= 60 else 99
        contraction = rng20 < 0.65 * rng_prior
        v20 = sum(vols[-20:]) / 20
        v60 = sum(vols[-60:]) / 60 if n >= 60 else v20
        dryup = v20 < 0.80 * v60 if v60 else False
        upv = sum(v for c0, c1, v in zip(closes[-41:-1], closes[-40:], vols[-40:]) if c1 > c0)
        dnv = sum(v for c0, c1, v in zip(closes[-41:-1], closes[-40:], vols[-40:]) if c1 < c0)
        accum_ratio = round(upv / dnv, 2) if dnv else 9.99
        d20 = sum(dlvs[-20:]) / 20
        d60 = sum(dlvs[-60:]) / 60 if n >= 60 else d20
        dlv_rising = d20 > d60 + 2
        split_suspect = had_split
        runup_1y = round((last / closes[-lb] - 1) * 100, 1)
        # Breakout: any of last 7 sessions closed above prior-60d high on ≥1.6× volume
        breakout_day = None
        for i in range(max(n - 7, 61), n):
            prior_high = max(highs[i - 60:i])
            av = sum(vols[max(0, i - 20):i]) / min(20, i)
            if closes[i] > prior_high and vols[i] >= 1.6 * av:
                breakout_day = str(dts[i])
        if breakout_day:
            setup = "breakout"
        elif dist_high <= 7 and rng20 <= 12 and (contraction or dryup or dlv_rising or accum_ratio >= 1.3):
            setup = "coiling"
        elif runup_1y > 150:
            setup = "extended"
        elif dist_high <= 20 and rng20 <= 18:
            setup = "basing"
        else:
            setup = "no_setup"
        readiness = max(0, min(100, round(
            (25 if setup == "breakout" else 0)
            + max(0, 25 - dist_high * 2.0)            # near highs
            + max(0, 15 - rng20)                       # tightness
            + (10 if contraction else 0)
            + (8 if dryup else 0)
            + min(12, max(0, (accum_ratio - 1) * 12))  # accumulation
            + (10 if dlv_rising else 0)
            - (15 if setup == "extended" else 0)
        )))
        step = 5
        spark = closes[::-1][::step][::-1][-52:]
        vspark = vols[::-1][::step][::-1][-52:]
        return {
            "setup": setup, "breakout_day": breakout_day,
            "readiness": readiness,
            "last_close": round(last, 2),
            "dist_from_52w_high_pct": dist_high,
            "range_20d_pct": rng20,
            "range_contraction": contraction,
            "volume_dryup": dryup,
            "accumulation_ratio": accum_ratio,
            "delivery_pct_20d": round(d20, 1),
            "delivery_rising": dlv_rising,
            "runup_1y_pct": runup_1y,
            "split_suspect": split_suspect,
            "spark_close": [round(x, 2) for x in spark],
            "spark_vol": vspark,
        }

    out = []
    for tk, f in fund.items():
        pa = _analyze(series.get(tk, []))
        row = {
            "ticker": tk, "company": f.get("company"),
            "list_tier": f.get("list_tier"), "stage": f.get("constraint_stage"),
            "trajectory": f.get("trajectory"),
            "fundamental_score": f.get("rank_score"),
            "theme": f.get("theme"), "explosion": f.get("explosion_potential"),
            "peer_corroboration": f.get("peer_corroboration"),
            "shortlisted_date": f.get("shortlisted_date"),
            "price": pa,
        }
        if pa:
            row["jump_score"] = round(
                0.55 * float(f.get("rank_score") or 0) + 0.45 * pa["readiness"], 1)
        else:
            row["jump_score"] = None
        out.append(row)

    _prio = {"breakout": 0, "coiling": 1, "basing": 2, "no_setup": 3, "extended": 4}
    out.sort(key=lambda r: (
        _prio.get((r["price"] or {}).get("setup", "no_setup"), 5),
        -(r["jump_score"] or 0),
    ))
    return {
        "as_of": str(asof_d), "year": base.get("year"), "country": country,
        "priced": sum(1 for r in out if r["price"]),
        "stocks": out[:top_n],
    }


@app.get("/api/thesis-ledger")
def get_thesis_ledger(ticker: str, country: str = "IN") -> dict:
    """The research file for one company: every constraint-thesis event in
    chronological order — signals with quotes and magnitudes, plus the
    detector's own run history (stage/tier transitions from snapshots).
    This is the record of 'what was knowable on which date'.
    """
    pg = get_pg()
    if pg is None:
        raise HTTPException(status_code=503, detail="DB unavailable")
    tk = ticker.strip().upper()
    from psycopg2.extras import RealDictCursor
    with pg._conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""SELECT d.filed_at::date AS dt, s.signal_type,
                           COALESCE(s.perspective,'') AS perspective,
                           s.confidence, s.signal_value, s.signal_unit,
                           s.context_text,
                           ({CONSTRAINT_PRED_SQL})   AS is_constraint,
                           ({DS_SELLER_PRED_SQL})    AS is_order_demand,
                           (s.signal_type = 'capex_increase')          AS is_capex,
                           (s.signal_type IN ('realized_margin_expansion',
                                              'pricing_power_emerging')) AS is_pricing,
                           (s.signal_type = 'supply_easing')           AS is_easing
                    FROM mg_signals s
                    JOIN mg_documents d ON d.id = s.document_id
                    WHERE d.country = %s AND UPPER(TRIM(d.ticker)) = %s
                      AND (({CONSTRAINT_PRED_SQL}) OR ({DS_SELLER_PRED_SQL})
                           OR s.signal_type IN ('capex_increase',
                               'realized_margin_expansion','pricing_power_emerging',
                               'supply_easing','competitor_constrained'))
                    ORDER BY d.filed_at""",
                (country, tk),
            )
            events = []
            for r in cur.fetchall():
                kind = ("constraint" if r["is_constraint"] else
                        "order_demand" if r["is_order_demand"] else
                        "capex" if r["is_capex"] else
                        "pricing" if r["is_pricing"] else
                        "easing" if r["is_easing"] else r["signal_type"])
                q = _quality_quote(r["context_text"] or "")
                events.append({
                    "date":        str(r["dt"]),
                    "kind":        kind,
                    "signal_type": r["signal_type"],
                    "perspective": r["perspective"],
                    "confidence":  float(r["confidence"] or 0),
                    "magnitude":   (f"{r['signal_value']:g} {r['signal_unit']}"
                                    if r["signal_value"] is not None else None),
                    "quote":       _trim_to_sentence(q, 220) if q else None,
                })
            # Detector run history (live snapshots): stage / tier over time
            cur.execute(
                """SELECT snapshot_date, stage, list_tier, trajectory, rank_score
                   FROM mg_shortlist_snapshots
                   WHERE country=%s AND ticker=%s ORDER BY snapshot_date""",
                (country, tk),
            )
            runs = [{**dict(r), "snapshot_date": str(r["snapshot_date"])}
                    for r in cur.fetchall()]
    first_c = next((e["date"] for e in events if e["kind"] == "constraint"), None)
    first_d = next((e["date"] for e in events if e["kind"] == "order_demand"), None)
    return {
        "ticker": tk, "country": country,
        "first_constraint_signal": first_c,
        "first_order_demand_signal": first_d,
        "event_count": len(events),
        "events": events,
        "detector_run_history": runs,
    }


def _ensure_paper_trades(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mg_paper_trades (
            id           BIGSERIAL PRIMARY KEY,
            country      VARCHAR(4) NOT NULL,
            ticker       TEXT NOT NULL,
            company      TEXT,
            action       TEXT NOT NULL,          -- buy | pass | exit
            note         TEXT,
            decided_at   DATE NOT NULL DEFAULT CURRENT_DATE,
            entry_price  DOUBLE PRECISION,
            exit_price   DOUBLE PRECISION,
            exited_at    DATE,
            status       TEXT NOT NULL DEFAULT 'open',  -- open | closed | passed
            created_at   TIMESTAMPTZ DEFAULT now()
        )""")


@app.post("/api/paper-trade")
def post_paper_trade(payload: dict) -> dict:
    """Record a paper decision: {ticker, country, action: buy|pass|exit, note}.
    Prices are captured from the latest NSE close automatically — the journal
    records what was knowable, not what you wish you'd paid.
    """
    pg = get_pg()
    if pg is None:
        raise HTTPException(status_code=503, detail="DB unavailable")
    tk = (payload.get("ticker") or "").strip().upper()
    action = (payload.get("action") or "").lower()
    country = payload.get("country") or "IN"
    note = (payload.get("note") or "")[:500]
    if not tk or action not in ("buy", "pass", "exit"):
        raise HTTPException(status_code=400, detail="ticker and action (buy|pass|exit) required")
    with pg._conn() as conn:
        cur = conn.cursor()
        _ensure_paper_trades(cur)
        cur.execute(
            """SELECT close::float FROM nse_bhavcopy_data
               WHERE symbol=%s AND series IN ('EQ','BE')
               ORDER BY trade_date DESC LIMIT 1""", (tk,))
        r = cur.fetchone()
        px = float(r[0]) if r else None
        if action == "exit":
            cur.execute(
                """UPDATE mg_paper_trades
                   SET status='closed', exit_price=%s, exited_at=CURRENT_DATE,
                       note = COALESCE(note,'') || ' | EXIT: ' || %s
                   WHERE country=%s AND ticker=%s AND status='open' AND action='buy'
                   RETURNING id""",
                (px, note, country, tk))
            ids = cur.fetchall()
            conn.commit()
            return {"ok": True, "closed": len(ids), "exit_price": px}
        cur.execute(
            """INSERT INTO mg_paper_trades
               (country, ticker, company, action, note, entry_price, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (country, tk, payload.get("company"), action, note, px,
             "open" if action == "buy" else "passed"))
        pid = cur.fetchone()[0]
        conn.commit()
    return {"ok": True, "id": pid, "entry_price": px}


@app.get("/api/paper-trades")
def get_paper_trades(country: str = "IN") -> dict:
    """The paper journal with live P&L against the latest NSE close."""
    pg = get_pg()
    if pg is None:
        raise HTTPException(status_code=503, detail="DB unavailable")
    from psycopg2.extras import RealDictCursor
    with pg._conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _ensure_paper_trades(cur)
            conn.commit()
            cur.execute(
                "SELECT * FROM mg_paper_trades WHERE country=%s ORDER BY decided_at DESC, id DESC",
                (country,))
            rows = [dict(r) for r in cur.fetchall()]
            open_tks = [r["ticker"] for r in rows if r["status"] == "open"]
            px = {}
            if open_tks:
                cur.execute(
                    """SELECT DISTINCT ON (symbol) symbol, close::float, trade_date
                       FROM nse_bhavcopy_data
                       WHERE symbol=ANY(%s) AND series IN ('EQ','BE')
                       ORDER BY symbol, trade_date DESC""", (open_tks,))
                px = {r["symbol"]: (r["close"], str(r["trade_date"])) for r in cur.fetchall()}
    for r in rows:
        r["decided_at"] = str(r["decided_at"])
        r["exited_at"] = str(r["exited_at"]) if r.get("exited_at") else None
        r["created_at"] = str(r.get("created_at") or "")
        if r["status"] == "open" and r["ticker"] in px and r.get("entry_price"):
            cp, cd = px[r["ticker"]]
            r["current_price"] = cp
            r["price_as_of"] = cd
            r["pnl_pct"] = round((cp / r["entry_price"] - 1) * 100, 1)
        elif r["status"] == "closed" and r.get("entry_price") and r.get("exit_price"):
            r["pnl_pct"] = round((r["exit_price"] / r["entry_price"] - 1) * 100, 1)
    opens = [r for r in rows if r["status"] == "open" and r.get("pnl_pct") is not None]
    return {
        "country": country, "trades": rows,
        "open_count": len([r for r in rows if r["status"] == "open"]),
        "open_avg_pnl_pct": (round(sum(r["pnl_pct"] for r in opens) / len(opens), 1)
                             if opens else None),
    }


@app.get("/api/changes-since-last-run")
def get_changes_since_last_run(country: str = "US") -> dict:
    """Diff the two most recent live-run snapshots: the weekly 'what changed'
    feed — new entrants, dropped names, stage moves, conviction transitions,
    fresh easing flags. This is what to read first every week.
    """
    pg = get_pg()
    if pg is None:
        raise HTTPException(status_code=503, detail="DB unavailable")
    from psycopg2.extras import RealDictCursor
    with pg._conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT DISTINCT snapshot_date FROM mg_shortlist_snapshots
                   WHERE country=%s ORDER BY snapshot_date DESC LIMIT 2""",
                (country,))
            dates = [r["snapshot_date"] for r in cur.fetchall()]
            if not dates:
                return {"country": country, "runs_available": 0}
            cur.execute(
                "SELECT * FROM mg_shortlist_snapshots WHERE country=%s AND snapshot_date=%s",
                (country, dates[0]))
            now_rows = {r["ticker"]: dict(r) for r in cur.fetchall()}
            prev_rows: dict = {}
            if len(dates) > 1:
                cur.execute(
                    "SELECT * FROM mg_shortlist_snapshots WHERE country=%s AND snapshot_date=%s",
                    (country, dates[1]))
                prev_rows = {r["ticker"]: dict(r) for r in cur.fetchall()}

    def _slim(r):
        return {k: (str(v) if k == "snapshot_date" else v) for k, v in r.items()
                if k in ("ticker","company","stage","list_tier","rank_score","trajectory")}

    new_entrants = [_slim(r) for t, r in now_rows.items() if t not in prev_rows]
    dropped      = [_slim(r) for t, r in prev_rows.items() if t not in now_rows]
    stage_moves, tier_moves, new_easing = [], [], []
    for t, r in now_rows.items():
        p = prev_rows.get(t)
        if not p:
            continue
        if r["stage"] != p["stage"]:
            stage_moves.append({**_slim(r), "from_stage": p["stage"], "to_stage": r["stage"]})
        if r["list_tier"] != p["list_tier"]:
            tier_moves.append({**_slim(r), "from_tier": p["list_tier"], "to_tier": r["list_tier"]})
        if ("easing_language_watch" in (r.get("exit_triggers") or [])
                and "easing_language_watch" not in (p.get("exit_triggers") or [])):
            new_easing.append(_slim(r))

    return {
        "country": country,
        "runs_available": len(dates),
        "current_run": str(dates[0]),
        "previous_run": str(dates[1]) if len(dates) > 1 else None,
        "new_entrants": new_entrants,
        "dropped": dropped,
        "stage_moves": stage_moves,
        "tier_moves": tier_moves,
        "new_easing_flags": new_easing,
    }


@app.get("/api/fundamental-calibration")
def get_fundamental_calibration(country: str = "US") -> dict:
    """Measured hit rates of past detections, graded against the companies'
    own subsequent filings (mg_fundamental_eval, filled by
    makrograph.evaluation.fundamental_loop). No price data involved.
    """
    pg = get_pg()
    if pg is None:
        raise HTTPException(status_code=503, detail="DB unavailable")
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                dims = {
                    "by_stage":     "stage::text",
                    "by_path":      "detection_path",
                    "by_legs":      "explosion_legs::text",
                    "by_year":      "detection_year::text",
                    "by_corroboration": (
                        "CASE WHEN COALESCE(peer_corroboration,0)=0 THEN '0' "
                        "WHEN peer_corroboration<5 THEN '1-4' ELSE '5+' END"
                    ),
                }
                out: dict = {"country": country}
                for name, expr in dims.items():
                    cur.execute(
                        f"""SELECT {expr} AS bucket,
                                   COUNT(*) AS n,
                                   ROUND(100.0*COUNT(*) FILTER (WHERE outcome='confirmed')/COUNT(*),1) AS confirmed_pct,
                                   ROUND(100.0*COUNT(*) FILTER (WHERE outcome='decayed')/COUNT(*),1)   AS decayed_pct
                            FROM mg_fundamental_eval
                            WHERE country = %s AND outcome != 'no_filings'
                            GROUP BY 1 ORDER BY 1""",
                        (country,),
                    )
                    out[name] = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT COUNT(*) AS graded FROM mg_fundamental_eval "
                    "WHERE country=%s AND outcome != 'no_filings'", (country,))
                out["graded_detections"] = cur.fetchone()["graded"]
                return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"calibration failed: {e}")


@app.get("/api/investment-final-shortlist")
def get_investment_final_shortlist(
    country: str = "IN",
    year: int | None = None,
    min_constraint_signals: int = 2,
    require_capex: bool = False,
    min_avg_confidence: float = 0.70,
) -> dict:
    """5-stage deterministic investment funnel. No Claude. Pure signal arithmetic.

    Stage 1: NEW/ESCALATING constraint themes with named components
    Stage 2: Supply-side companies in those themes (role='supply')
    Stage 3: Companies with direct constraint evidence in their own filings
    Stage 4: Evidence quality rank (confidence × count × capex)
    Stage 5: Final ranked shortlist with all evidence attached
    """
    pg = get_pg()
    if not pg:
        return {}
    try:
        import json as _j
        from psycopg2.extras import RealDictCursor
        from collections import defaultdict

        yr    = year or date.today().year
        to_d  = date(yr, 12, 31) if yr < date.today().year else date.today()
        # Year-specific window: historical years use only that year's filings so
        # the same companies don't bleed across years. Current/next year uses a
        # 18-month lookback to avoid sparse data for mid-year runs.
        is_current = (yr >= date.today().year - 1)
        from_d = date(yr - 1, 7, 1) if is_current else date(yr, 1, 1)
        # Prior year for delta computation (always the full prior calendar year)
        prior_from_d = date(yr - 1, 1, 1)
        prior_to_d   = date(yr - 1, 12, 31)

        # ─── Stage 1: Get NEW/ESCALATING themes ────────────────────────────
        focus_themes = pg.get_year_focus_analysis(yr, country)
        actionable   = [t for t in focus_themes
                        if t.get("focus_class") in ("new", "escalating", "no_prior")]

        stage1_out = []
        for t in actionable:
            # Skip per-theme component lookup in the loop — too slow with 100+ themes.
            # Components are fetched on-demand per theme in the detail view.
            components: list[dict] = []
            stage1_out.append({
                "theme_id":   t.get("id"),
                "theme_name": t.get("theme_name",""),
                "theme_slug": t.get("theme_slug",""),
                "focus":      t.get("focus_class",""),
                "conviction": t.get("conviction",""),
                "delta_pct":  t.get("delta_pct"),
                "this_avg":   t.get("this_avg_strength", 0),
                "constrained_components": [
                    {"component": c.get("component",""),
                     "signal_type": c.get("signal_type",""),
                     "frequency": c.get("frequency", 0),
                     "best_quote": (c.get("best_quote") or "")[:200]}
                    for c in components[:3] if c.get("component","") not in ("unspecified","")
                ],
            })

        actionable_slugs = [t["theme_slug"] for t in stage1_out if t["theme_slug"]]

        # No theme snapshots in this window (common for LIVE runs before the
        # theme stage has processed the latest months) is NOT fatal: company
        # discovery is signal-primary (Stage 2). Themes are enrichment — the
        # shortlist proceeds with blank theme fields rather than going dark.
        if not actionable_slugs:
            logging.info("final-shortlist %s/%s: no active themes in window — "
                         "continuing signal-only", country, yr)

        # ─── Stage 2: Discover companies FROM SIGNALS (year-specific) ─────────
        # PRIMARY SOURCE: mg_signals.document_id → mg_documents.filed_at
        # This is the quality data — what companies actually said in their filings
        # in THIS year. Naturally different each year because filing dates are real.
        #
        # mg_theme_beneficiaries is used ONLY for role enrichment (supply vs demand)
        # not for company discovery — that table has no year dimension.
        CONSTRAINT = ("supply_bottleneck","inventory_drawdown",
                      "capacity_shortage","demand_exceeds_supply")

        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:

                # 2a. Find companies with SELLER-PERSPECTIVE constraint signals
                # Primary: capacity_constraint_seller signal type (new, explicit)
                # Secondary: supply_bottleneck with perspective='seller' (pipeline sets this)
                # Tertiary: supply_bottleneck with seller-language context patterns
                #           (for existing data before pipeline re-run)
                #
                # SELLER language = "our capacity/backlog/lead times" → they have pricing power
                # BUYER language  = "component shortage / supply chain disrupted" → they are hurt
                #
                # The key investment insight: only seller-perspective = pricing power + order visibility
                #
                # Two qualification paths (both country-agnostic):
                #   HARD constraint: explicit bottleneck signal types with seller language
                #   DEMAND-LED constraint: seller demand surge + capacity investment +
                #     pricing/margin evidence. This is how constraint expresses itself
                #     when management talks order books instead of bottlenecks
                #     (GE Vernova pattern: "orders +173%, better pricing, adding capacity"
                #     — demand exceeds capacity without a single "shortage" sentence).
                #
                # _CPRED is defined ONCE (module-level CONSTRAINT_PRED_SQL) and
                # reused for count / confidence / quote / prior-year / first-ever
                # AND by the fundamental-feedback evaluator, so every consumer
                # agrees on what "constraint" means.
                _CPRED = CONSTRAINT_PRED_SQL
                _DS_SELLER = DS_SELLER_PRED_SQL
                cur.execute(
                    f"""SELECT
                           d.company,
                           COALESCE(NULLIF(d.ticker,''), d.company) AS ticker,
                           -- HARD seller-perspective constraint signals
                           COUNT(*) FILTER (WHERE {_CPRED})             AS c_count,
                           -- DEMAND-LED evidence: seller-tagged demand surge
                           COUNT(*) FILTER (WHERE {_DS_SELLER})         AS ds_seller,
                           -- DISTINCT EVIDENCE DAYS: one order announced via
                           -- press release + intimation + transcript is ONE
                           -- event, not three. Days ≈ events. (Shadow-tested
                           -- before it may replace raw counts in scoring.)
                           COUNT(DISTINCT d.filed_at::date) FILTER (WHERE {_CPRED})
                                                                        AS c_days,
                           COUNT(DISTINCT d.filed_at::date) FILTER (WHERE {_DS_SELLER})
                                                                        AS ds_days,
                           -- QUALITY-WEIGHTED evidence: confidence-weighted,
                           -- with a premium for quantified claims (a stated
                           -- number is a stronger commitment than adjectives).
                           SUM(CASE WHEN {_CPRED} OR {_DS_SELLER}
                               THEN s.confidence
                                    * (CASE WHEN s.signal_value IS NOT NULL
                                            THEN 1.5 ELSE 1.0 END)
                               ELSE 0 END)                              AS evidence_quality,
                           COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase')
                                                                        AS capex_count,
                           COUNT(*) FILTER (WHERE s.signal_type IN (
                               'demand_surge','capex_increase'
                           ))                                           AS d_count,
                           -- Pricing power signals: the CRITICAL middle step
                           -- Constraint → Pricing → Capex → Revenue explosion
                           COUNT(*) FILTER (WHERE s.signal_type IN (
                               'realized_margin_expansion', 'pricing_power_emerging',
                               'supply_concentration'
                           ))                                           AS pricing_count,
                           -- Competitor constrained: a RIVAL's capacity is down
                           -- while this company can ship — share gain + pricing
                           -- power without building anything. Best setup there is.
                           COUNT(*) FILTER (WHERE s.signal_type = 'competitor_constrained')
                                                                        AS comp_constrained,
                           -- Supply easing: the thesis-closing signal (exit trigger)
                           COUNT(*) FILTER (WHERE s.signal_type = 'supply_easing')
                                                                        AS easing_count,
                           -- Quantified magnitudes (filled by magnitude_extractor):
                           -- intensity beats mention-counting; an order book up
                           -- 173 pct is a different animal from "orders grew".
                           MAX(s.signal_value) FILTER (WHERE s.signal_unit = 'order_growth_pct')
                                                                        AS max_order_growth,
                           MAX(s.signal_value) FILTER (WHERE s.signal_unit = 'utilization_pct')
                                                                        AS max_utilization,
                           MAX(s.signal_value) FILTER (WHERE s.signal_unit = 'margin_change_bps')
                                                                        AS max_margin_bps,
                           MAX(s.signal_value) FILTER (WHERE s.signal_unit = 'booktobill_ratio')
                                                                        AS max_booktobill,
                           ROUND(AVG(s.confidence) FILTER (WHERE {_CPRED} OR {_DS_SELLER}
                           )::numeric, 3)                               AS avg_conf,
                           -- Best quote by TIER then CONFIDENCE: hard-constraint quotes
                           -- (prefix '1') outrank demand-led quotes (prefix '0'), then
                           -- LPAD confidence prefix ensures MAX picks highest confidence.
                           SUBSTRING(
                               MAX(CASE
                                   WHEN {_CPRED}
                                        AND s.context_text IS NOT NULL
                                        AND LENGTH(s.context_text) > 40
                                   THEN '1' || LPAD((s.confidence * 1000)::int::text, 5, '0') || s.context_text
                                   WHEN {_DS_SELLER}
                                        AND s.context_text IS NOT NULL
                                        AND LENGTH(s.context_text) > 40
                                   THEN '0' || LPAD((s.confidence * 1000)::int::text, 5, '0') || s.context_text
                                   ELSE NULL END)
                           , 7)                                         AS best_quote,
                           MAX(CASE WHEN {_CPRED} OR {_DS_SELLER}
                                THEN d.filed_at ELSE NULL END)::date    AS best_quote_date,
                           MAX(CASE WHEN s.signal_type = 'capex_increase'
                               AND s.context_text IS NOT NULL
                               THEN s.context_text ELSE NULL END)      AS capex_quote,
                           MAX(d.filed_at)::date                        AS last_filing,
                           MIN(d.filed_at)::date                        AS first_filing,
                           COUNT(DISTINCT d.id)                         AS filing_count,
                           -- Signal-text corpus for theme matching: what does
                           -- this company actually talk about? (wafers, grids,
                           -- transformers...) Used to verify theme relevance.
                           LEFT(STRING_AGG(LEFT(s.context_text, 150), ' '), 5000)
                                                                        AS ctx_blob
                       FROM mg_signals s
                       JOIN mg_documents d ON d.id = s.document_id
                       WHERE d.country = %s
                         AND d.filed_at BETWEEN %s AND %s
                         AND d.company IS NOT NULL AND d.company != ''
                       GROUP BY d.company, COALESCE(NULLIF(d.ticker,''), d.company)
                       HAVING
                           (
                               -- Path A: hard seller-constraint signals
                               COUNT(*) FILTER (WHERE {_CPRED}) >= %s
                               -- Path B: demand-led constraint (GE Vernova pattern) —
                               -- surging seller demand + building capacity + margin proof.
                               -- All three legs required so raw demand noise never qualifies.
                               OR (
                                   COUNT(*) FILTER (WHERE {_DS_SELLER}) >= 3
                                   AND COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase') >= 1
                                   AND COUNT(*) FILTER (WHERE s.signal_type IN (
                                       'realized_margin_expansion','pricing_power_emerging'
                                   )) >= 1
                               )
                           )
                           -- Minimum filing count: 1-2 filings = penny stock / tiny co.
                           AND COUNT(DISTINCT d.id) >= 3
                       ORDER BY c_count DESC, ds_seller DESC, avg_conf DESC NULLS LAST
                       LIMIT 200""",
                    (country, from_d, to_d, min_constraint_signals)
                )
                signal_companies = cur.fetchall()

                # Build primary company map from signals
                co_meta: dict[str, dict] = {}
                for r in signal_companies:
                    name = r["company"]
                    co_meta[name] = {
                        "ticker":       r["ticker"] or "",
                        "role":         "",           # enriched below
                        "c_count":      int(r["c_count"] or 0),
                        "capex_count":  int(r["capex_count"] or 0),
                        "d_count":      int(r["d_count"] or 0),
                        "avg_conf":      float(r["avg_conf"] or 0),
                        "best_quote":    (r["best_quote"] or "")[:300],
                        "best_quote_date": str(r["best_quote_date"] or ""),
                        "capex_quote":   (r["capex_quote"] or "")[:200],
                        "last_filing":   str(r["last_filing"] or ""),
                        "first_filing":  str(r["first_filing"] or ""),
                        "pricing_count": int(r["pricing_count"] or 0),
                        "ds_seller":     int(r["ds_seller"] or 0),
                        "comp_constrained": int(r["comp_constrained"] or 0),
                        "easing_count":  int(r["easing_count"] or 0),
                        "c_days":        int(r["c_days"] or 0),
                        "ds_days":       int(r["ds_days"] or 0),
                        "evidence_quality": round(float(r["evidence_quality"] or 0), 2),
                        "filing_count":  int(r["filing_count"] or 0),
                        "max_order_growth": float(r["max_order_growth"] or 0),
                        "max_utilization":  float(r["max_utilization"] or 0),
                        "max_margin_bps":   float(r["max_margin_bps"] or 0),
                        "max_booktobill":   float(r["max_booktobill"] or 0),
                        "ctx_blob":      (r["ctx_blob"] or "").lower(),
                    }
                    # Demand-led detection (Path B in HAVING): no hard constraint
                    # signals, but seller demand surge + capex + pricing all fired.
                    # Give it an effective constraint count from demand evidence so
                    # downstream scoring works, and label the path for transparency.
                    m = co_meta[name]
                    if m["c_count"] == 0 and m["ds_seller"] >= 3:
                        m["c_count"] = min(m["ds_seller"], 4)
                        m["detection_path"] = "demand_led"
                    else:
                        m["detection_path"] = "hard_constraint"

                # ── Sector gate: remove non-supplier companies before any further work ──
                # The beneficiary mapper has _KNOWN_TICKER_SECTORS + name patterns.
                # This is the same gate applied during theme mapping — apply it here
                # to the shortlist so retail/finance/homebuilder/airline/entertainment
                # companies never appear regardless of what signals they fired.
                # Apply quality-quote filter immediately to co_meta.
                # Companies whose ONLY constraint evidence is boilerplate (bond language,
                # partnership distributions, recalls, etc.) get their c_count set to 0.
                # This is language-based, not company-based — works for any future company.
                for name, meta in co_meta.items():
                    raw_q = meta.get("best_quote", "")
                    filtered_q = _quality_quote(raw_q)
                    meta["best_quote"] = _trim_to_sentence(filtered_q)
                    # If the best quote is boilerplate, clear the quote but DO NOT zero
                    # c_count — the company may have other valid signals. The HAVING clause
                    # in SQL already screened for >= N seller signals; if best_quote is
                    # bad it's a display issue, not evidence of zero real signals.

                # Quote FALLBACK: the SQL picked exactly one candidate per
                # company (highest confidence); if _quality_quote rejected it,
                # try the next-best texts instead of showing an empty evidence
                # box — the quote is what the human verifies before investing.
                _no_quote = [n for n, m in co_meta.items() if not m.get("best_quote")]
                if _no_quote:
                    _nq_tickers = [
                        (co_meta[n].get("ticker") or "").upper() for n in _no_quote
                        if co_meta[n].get("ticker")
                    ]
                    if _nq_tickers:
                        cur.execute(
                            f"""SELECT UPPER(TRIM(d.ticker)) AS tk, s.context_text,
                                       s.confidence
                                FROM mg_signals s
                                JOIN mg_documents d ON d.id = s.document_id
                                WHERE d.country = %s
                                  AND d.filed_at BETWEEN %s AND %s
                                  AND UPPER(TRIM(d.ticker)) = ANY(%s)
                                  AND ({_CPRED} OR {_DS_SELLER})
                                  AND s.context_text IS NOT NULL
                                  AND LENGTH(s.context_text) > 60
                                ORDER BY s.confidence DESC""",
                            (country, from_d, to_d, _nq_tickers)
                        )
                        _cand: dict[str, list] = {}
                        for r in cur.fetchall():
                            _cand.setdefault(r["tk"], []).append(r["context_text"])
                        for n in _no_quote:
                            tk = (co_meta[n].get("ticker") or "").upper()
                            for txt in _cand.get(tk, [])[:8]:
                                q = _quality_quote(txt)
                                if q:
                                    co_meta[n]["best_quote"] = _trim_to_sentence(q)
                                    break

                _BLOCKED_SECTORS = frozenset({
                    "retail", "consumer_goods", "food", "beverage", "restaurant",
                    "homebuilder", "apparel", "cosmetic", "cannabis",
                    "finance", "insurance", "pharmacy_chain", "hospital",
                    "airline", "hotel", "entertainment", "media",
                    "real_estate",   # REITs that are property owners not suppliers
                })

                try:
                    from makrograph.themes import beneficiary_mapper as _bm_mod

                    def _get_sector(ticker, company):
                        if ticker:
                            s = _bm_mod._KNOWN_TICKER_SECTORS.get(ticker.upper())
                            if s:
                                return s
                        name_lower = (company or "").lower()
                        for frag, sector in _bm_mod._COMPANY_NAME_SECTOR_PATTERNS:
                            if frag in name_lower:
                                return sector
                        return "unknown"

                    to_remove = []
                    for nm, meta in co_meta.items():
                        ticker = (meta.get("ticker") or "").upper()
                        sector = _get_sector(ticker, nm)
                        if sector in _BLOCKED_SECTORS:
                            to_remove.append(nm)
                    for nm in to_remove:
                        del co_meta[nm]
                except Exception as _se:
                    logging.warning("Sector gate failed: %s", _se)

                if not co_meta:
                    # Distinguish "no opportunities" from "no data": report how
                    # fresh the signal extraction actually is so a stale
                    # pipeline is visible instead of looking like a quiet market.
                    cur.execute(
                        """SELECT MAX(d.filed_at)::date AS latest FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE d.country = %s""", (country,))
                    _fr = cur.fetchone()
                    _latest = str((_fr or {}).get("latest") or "")
                    return {"year": yr, "country": country,
                            "period": f"{from_d} → {to_d}",
                            "latest_signal_date": _latest,
                            "data_note": (
                                f"No signals in window; latest extracted signal is {_latest}. "
                                "Run the NLP stage on newer filings."
                                if _latest and str(_latest) < str(from_d) else ""
                            ),
                            "stats": {"stage1_themes": len(stage1_out),
                                      "stage2_signal_companies": 0},
                            "stages": stage1_out, "final_shortlist": []}

                # 2a-delta: prior-year c_count for YoY delta scoring
                # Companies with SPIKING signals this year rank much higher than
                # companies with the same level every year (persistent plays).
                tickers_for_delta = [m["ticker"].upper() for m in co_meta.values() if m.get("ticker")]
                if tickers_for_delta:
                    # Prior-year counts use the SAME _CPRED / _DS_SELLER definitions
                    # as the main query so YoY trajectory compares like with like.
                    cur.execute(
                        f"""SELECT UPPER(TRIM(d.ticker)) AS tk,
                                  COUNT(*) FILTER (WHERE {_CPRED})     AS prior_c,
                                  COUNT(*) FILTER (WHERE {_DS_SELLER}) AS prior_ds,
                                  COUNT(DISTINCT d.id)                 AS prior_docs
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE d.country = %s
                             AND d.filed_at BETWEEN %s AND %s
                             AND UPPER(TRIM(d.ticker)) = ANY(%s)
                           GROUP BY UPPER(TRIM(d.ticker))""",
                        (country, prior_from_d, prior_to_d, tickers_for_delta)
                    )
                    prior_rows = {r["tk"]: r for r in cur.fetchall()}
                    for nm, meta in co_meta.items():
                        tk = (meta.get("ticker") or "").upper()
                        pr = prior_rows.get(tk)
                        # prior_docs=0 means the company simply wasn't filing last
                        # year (IPO / new listing) — that is NOT evidence a
                        # constraint newly emerged. Trajectory logic uses this to
                        # avoid handing every fresh IPO the "new constraint" crown.
                        meta["prior_docs"] = int(pr["prior_docs"] or 0) if pr else 0
                        if meta.get("detection_path") == "demand_led":
                            # Demand-led companies: trajectory measured on the same
                            # evidence family that qualified them (seller demand surge).
                            meta["prior_c_count"] = min(int(pr["prior_ds"] or 0), 4) if pr else 0
                        else:
                            meta["prior_c_count"] = int(pr["prior_c"] or 0) if pr else 0

                    # True first-ever constraint signal date (NOT clipped to the
                    # query window, unlike first_filing which is just MIN within
                    # the year). Capped at the selected year's end so backtests
                    # don't peek forward.
                    cur.execute(
                        f"""SELECT UPPER(TRIM(d.ticker)) AS tk,
                                  MIN(d.filed_at) FILTER (WHERE {_CPRED})::date     AS first_ever_c,
                                  MIN(d.filed_at) FILTER (WHERE {_DS_SELLER})::date AS first_ever_ds
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE d.country = %s
                             AND d.filed_at <= %s
                             AND UPPER(TRIM(d.ticker)) = ANY(%s)
                           GROUP BY UPPER(TRIM(d.ticker))""",
                        (country, to_d, tickers_for_delta)
                    )
                    fe_rows = {r["tk"]: r for r in cur.fetchall()}
                    for nm, meta in co_meta.items():
                        tk = (meta.get("ticker") or "").upper()
                        fr = fe_rows.get(tk)
                        if not fr:
                            meta["first_ever_signal"] = ""
                        elif meta.get("detection_path") == "demand_led":
                            meta["first_ever_signal"] = str(fr["first_ever_ds"] or fr["first_ever_c"] or "")
                        else:
                            meta["first_ever_signal"] = str(fr["first_ever_c"] or "")

                    # Shortlisted date: the day this company CROSSED the
                    # qualification thresholds inside the window — i.e. when a
                    # weekly pipeline run would first have surfaced it.
                    #   Path A: date of the Nth hard-constraint signal
                    #   Path B: latest of (3rd order-demand, 1st capex, 1st pricing)
                    #   Both gated by the 3rd signal-bearing filing.
                    cur.execute(
                        f"""SELECT UPPER(TRIM(d.ticker)) AS tk,
                                   d.filed_at::date AS dt,
                                   d.id AS doc_id,
                                   ({_CPRED})     AS is_c,
                                   ({_DS_SELLER}) AS is_ds,
                                   (s.signal_type = 'capex_increase') AS is_k,
                                   (s.signal_type IN ('realized_margin_expansion',
                                                      'pricing_power_emerging')) AS is_p
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            WHERE d.country = %s
                              AND d.filed_at BETWEEN %s AND %s
                              AND UPPER(TRIM(d.ticker)) = ANY(%s)
                            ORDER BY d.filed_at""",
                        (country, from_d, to_d, tickers_for_delta)
                    )
                    _tl: dict[str, dict] = {}
                    for r in cur.fetchall():
                        t = _tl.setdefault(r["tk"], {"c": [], "ds": [], "k": [], "p": [], "docs": {}})
                        if r["is_c"]:  t["c"].append(r["dt"])
                        if r["is_ds"]: t["ds"].append(r["dt"])
                        if r["is_k"]:  t["k"].append(r["dt"])
                        if r["is_p"]:  t["p"].append(r["dt"])
                        if r["doc_id"] not in t["docs"]:
                            t["docs"][r["doc_id"]] = r["dt"]
                    for nm, meta in co_meta.items():
                        tk = (meta.get("ticker") or "").upper()
                        t = _tl.get(tk)
                        if not t:
                            meta["shortlisted_date"] = ""
                            continue
                        n = max(int(min_constraint_signals), 1)
                        path_a = t["c"][n - 1] if len(t["c"]) >= n else None
                        path_b = (max(t["ds"][2], t["k"][0], t["p"][0])
                                  if len(t["ds"]) >= 3 and t["k"] and t["p"] else None)
                        qual = min((d for d in (path_a, path_b) if d), default=None)
                        doc_dates = sorted(t["docs"].values())
                        doc_gate = doc_dates[2] if len(doc_dates) >= 3 else None
                        if qual and doc_gate:
                            meta["shortlisted_date"] = str(max(qual, doc_gate))
                        else:
                            meta["shortlisted_date"] = str(qual or "")

                # 2b. Resolve theme IDs — prefer SPECIFIC themes (≤40 companies).
                # Generic catch-alls like "Materials: Constraint from Cloud Demand"
                # with 138 companies tell us nothing useful about a specific company.
                # We always store all themes but flag which are specific enough to display.
                cur.execute(
                    "SELECT id, theme_name, theme_slug, conviction, company_count "
                    "FROM mg_themes WHERE theme_slug=ANY(%s) AND is_active=TRUE",
                    (actionable_slugs,)
                )
                theme_rows = {r["id"]: dict(r) for r in cur.fetchall()}
                # Mark which themes are specific enough to be useful
                _SPECIFIC_THEME_IDS = {
                    tid for tid, t in theme_rows.items()
                    if (t.get("company_count") or 999) <= 40
                }

                # 2c. Enrich: look up company roles from beneficiary table (optional)
                # Use company name OR ticker matching — just for role label, NOT for filtering
                company_names = list(co_meta.keys())
                tickers       = [m["ticker"].upper() for m in co_meta.values() if m["ticker"]]

                if company_names:
                    cur.execute(
                        """SELECT DISTINCT ON (tb.company_name)
                                  tb.company_name, tb.ticker,
                                  tb.company_role, tb.theme_id
                           FROM mg_theme_beneficiaries tb
                           WHERE (tb.company_name = ANY(%s)
                                  OR UPPER(TRIM(tb.ticker)) = ANY(%s))
                             AND tb.company_name IS NOT NULL
                           ORDER BY tb.company_name, tb.relevance_score DESC""",
                        (company_names, tickers or ["__none__"])
                    )
                    for r in cur.fetchall():
                        nm = r["company_name"]
                        if nm in co_meta and not co_meta[nm]["role"]:
                            co_meta[nm]["role"] = r["company_role"] or ""

                # 2d. Theme matching: which actionable themes mention these companies?
                # Join through signal entity text and beneficiary name matching
                co_themes_map: dict[str, set] = defaultdict(set)
                if theme_rows:
                    cur.execute(
                        """SELECT DISTINCT tb.company_name, tb.theme_id
                           FROM mg_theme_beneficiaries tb
                           WHERE tb.theme_id = ANY(%s)
                             AND (tb.company_name = ANY(%s)
                                  OR UPPER(TRIM(tb.ticker)) = ANY(%s))""",
                        (list(theme_rows.keys()), company_names, tickers or ["__none__"])
                    )
                    for r in cur.fetchall():
                        co_themes_map[r["company_name"]].add(r["theme_id"])

                # Also assign the closest theme based on signal entity_text matching
                # to themes' constraint components (best-effort)
                raw_sigs = []   # already captured above in co_meta

        # ─── Stage 3 / Stage 4: Evidence already aggregated in Stage 2 ──────
        # mg_signals query above already computed c_count, avg_conf, best_quote
        # No second signal query needed — this IS the quality source.
        co_evidence: dict[str, dict] = {
            nm: {
                "constraint": [{"signal_type": "supply_bottleneck",
                                "confidence": meta["avg_conf"],
                                "context_text": meta["best_quote"],
                                "filed_date": meta["best_quote_date"]}]
                              if meta["best_quote"] else [],
                "capex":      [{"signal_type": "capex_increase",
                                "confidence": 0.80,
                                "context_text": meta["capex_quote"],
                                "filed_date": ""}]
                              if meta["capex_quote"] else [],
                "demand":     []
            }
            for nm, meta in co_meta.items()
        }
        ticker_to_name = {m["ticker"].upper(): nm for nm, m in co_meta.items() if m["ticker"]}

        # Patch: use pre-aggregated counts directly
        for r in []:  # no-op — signals already aggregated
            co = ticker_to_name.get((r.get("ticker") or "").upper()) or r.get("company","")
            if co not in co_meta:
                continue
            ev = co_evidence[co]
            sig = dict(r)
            if r["signal_type"] in CONSTRAINT:
                ev["constraint"].append(sig)
            elif r["signal_type"] == "capex_increase":
                ev["capex"].append(sig)
            else:
                ev["demand"].append(sig)

        # ─── Stage 4.5: Peer corroboration + supply-chain propagation ────────
        # One company claiming constraint is an anecdote; several independent
        # companies talking about the same constrained domain in the same
        # window is a fact. Assign each company its domain families from its
        # own signal corpus, then count peers per family.
        _co_families: dict[str, set] = {}
        for _nm, _m in co_meta.items():
            _blob = (_m.get("ctx_blob","") or "") + " " + _nm.lower()
            fams = {
                fam for fam, words in THEME_KW_FAMILIES.items()
                if any(w in _blob for w in words)
            }
            _co_families[_nm] = fams
        _family_counts: dict[str, int] = {}
        for _nm, fams in _co_families.items():
            if co_meta[_nm].get("c_count", 0) >= 2:
                for f in fams:
                    _family_counts[f] = _family_counts.get(f, 0) + 1

        # Confirmed-constrained domains (≥3 independent companies) propagate a
        # WATCH to their upstream input components: suppliers of those inputs
        # typically report their own constraint one or more quarters later.
        _confirmed_domains = {f for f, n in _family_counts.items() if n >= 3}
        upstream_watch: list[dict] = []
        _seen_up: set = set()
        for _dom in sorted(_confirmed_domains):
            for _comp in UPSTREAM_COMPONENTS.get(_dom, []):
                if _comp in _seen_up:
                    continue
                _seen_up.add(_comp)
                # Companies already in candidates whose corpus mentions the
                # upstream component but who have weak constraint counts —
                # the earliest place the propagation will surface.
                _early = [
                    co_meta[n].get("ticker","") for n, m in co_meta.items()
                    if _comp in (m.get("ctx_blob","") or "") and m.get("c_count",0) < 2
                ][:5]
                upstream_watch.append({
                    "constrained_domain": _dom,
                    "upstream_component": _comp,
                    "confirmed_by_peers": _family_counts.get(_dom, 0),
                    "early_candidates":   [t for t in _early if t],
                })

        # ─── Stage 4.6: Integrity pre-computation ────────────────────────────
        # Language-only checks (no prices): these never disqualify a company,
        # they surface a ⚠ telling the human where to dig before trusting the
        # narrative. Calibrated against the candidate pool itself, so they are
        # country- and year-relative, never absolute or hardcoded.
        _PROMO_WORDS = (
            "robust", "strong", "exceptional", "outstanding", "landmark",
            "milestone", "best-ever", "best ever", "record ", "unprecedented",
            "phenomenal", "stellar", "remarkable", "extraordinary",
            "transformational", "marquee", "prestigious",
        )
        _CURRENCY_MARKS = ("₹", "rs.", "rs ", " crore", " lakh", "$", " million",
                           " billion", "inr ", "usd ", " cr ", " mn ", " bn ")
        _promo_density: dict[str, float] = {}
        for _nm, _m in co_meta.items():
            _blob = _m.get("ctx_blob","") or ""
            _wc = max(len(_blob.split()), 1)
            _promo_density[_nm] = sum(_blob.count(w) for w in _PROMO_WORDS) / _wc * 1000
        _pd_vals = sorted(_promo_density.values())
        _pd_median = _pd_vals[len(_pd_vals)//2] if _pd_vals else 0.0

        # ─── Stage 5: Score and rank ─────────────────────────────────────────
        FOCUS_SCORE = {"new":1.0,"escalating":0.9,"no_prior":0.6,"persistent":0.3}
        CONV_SCORE  = {"high":1.0,"confirmed":0.85,"developing":0.65,"emerging":0.4}

        results = []
        for name, meta in co_meta.items():
            # Use pre-aggregated signal data from Stage 2 SQL
            c_count  = meta.get("c_count", 0)
            k_count  = meta.get("capex_count", 0)
            d_count  = meta.get("d_count", 0)
            avg_conf = meta.get("avg_conf", 0.0)

            if c_count < min_constraint_signals:
                continue
            if require_capex and k_count == 0:
                continue
            if avg_conf < min_avg_confidence:
                continue

            # For compatibility with downstream code
            c_sigs = [{"confidence": avg_conf, "context_text": meta.get("best_quote",""),
                        "filed_date": meta.get("best_quote_date","")}] * c_count
            k_sigs = [{"confidence": 0.80}] * k_count
            d_sigs = [{}] * d_count

            best_c_dict = {"context_text": meta.get("best_quote",""),
                           "confidence": avg_conf,
                           "filed_date": meta.get("best_quote_date","")}
            best_k_dict = {"context_text": meta.get("capex_quote","")}
            best_c = best_c_dict if meta.get("best_quote") else None
            best_k = best_k_dict if meta.get("capex_quote") else None

            # Theme quality — pick the most SPECIFIC actionable theme for this company.
            # Prefer themes whose name contains the company's sector keyword over
            # generic "Materials: Constraint from ESG Demand" catch-alls.
            theme_ids  = co_themes_map.get(name, set())
            ticker_upper = meta.get("ticker","").upper()
            company_lower = name.lower()

            # Sector → theme keyword priority mapping
            _SECTOR_THEME_KEYWORDS = {
                "data center": ["data center","cloud","datacenter"],
                "semiconductor": ["semiconductor","chip","wafer","hbm"],
                "defense": ["defense","defence","aerospace","military"],
                "energy": ["solar","wind","power","energy","electric"],
                "industrial": ["industrial","manufacturing","automation"],
                "medical": ["medical","healthcare","pharma"],
            }

            # Evidence text: what the company ITSELF said. Theme keywords must
            # appear here (or in the name) to count as a real match — matching
            # against the theme universe alone produced junk like
            # "Airbnb → Defense: Constraint from EPA Demand".
            # The corpus is everything this company's signals said this year —
            # a semiconductor company's signals talk wafers and fabs, a T&D
            # company's talk grids and transformers. No sector table needed.
            _evidence_text = (
                (meta.get("best_quote","") or "").lower() + " " +
                (meta.get("capex_quote","") or "").lower() + " " +
                (meta.get("ctx_blob","") or "")
            )
            _sector_kws: list[str] = []

            def _theme_relevance(theme_name: str, ticker: str, co_name: str) -> float:
                """Score how relevant a theme is to this specific company."""
                tn = theme_name.lower()
                co = co_name.lower()
                score = 0.0
                # Penalise extremely generic ESG/catch-all themes
                if "esg" in tn:
                    score -= 2.0
                if "severe constraint" in tn and "energy" not in co and "power" not in co:
                    score -= 1.0
                # Reward themes whose keywords appear in the company's NAME or
                # in its OWN evidence text (quotes from its filings).
                # Keyword FAMILIES: a theme keyword matches if the company's
                # evidence uses any synonym from the same domain family —
                # "wafer" theme ↔ company talking "semiconductor"/"fab"/"chip".
                # (Defined at module level as THEME_KW_FAMILIES; also used for
                # peer corroboration and supply-chain propagation.)
                for kw, family in THEME_KW_FAMILIES.items():
                    if kw in tn:
                        if any(w in co or w in _evidence_text for w in family):
                            score += 3.0
                        else:
                            score += 0.5
                # Demand-Supply Tension themes are more specific than "X: Constraint from Y"
                if "demand-supply tension" in tn:
                    score += 1.5
                return score

            candidate_themes = [t for t in stage1_out if t.get("theme_id") in theme_ids]

            # Prefer specific themes (≤40 companies) over generic catch-alls.
            # "semiconductor: Demand-Supply Tension" (12 cos) > "Materials: Constraint from Cloud Demand" (138 cos)
            specific_candidates = [t for t in candidate_themes if t.get("theme_id") in _SPECIFIC_THEME_IDS]
            pool = specific_candidates if specific_candidates else candidate_themes

            best_theme = max(
                pool,
                key=lambda t: (
                    _theme_relevance(t.get("theme_name",""), ticker_upper, company_lower)
                    + FOCUS_SCORE.get(t.get("focus",""), 0) * 2
                    + CONV_SCORE.get(t.get("conviction",""), 0)
                    # Penalise overly broad themes directly in scoring
                    - max(0, (theme_rows.get(t.get("theme_id",0),{}).get("company_count",0) - 20)) * 0.05
                ),
                default=None
            )
            focus_s = FOCUS_SCORE.get(best_theme.get("focus","") if best_theme else "", 0.3)
            conv_s  = CONV_SCORE.get(best_theme.get("conviction","") if best_theme else "", 0.3)

            # Display gate: a theme is only SHOWN if it has genuine keyword
            # overlap with the company (name or own evidence). A wrong theme
            # is worse than no theme — it destroys trust in every other row.
            # Scoring (focus_s/conv_s) still uses the best candidate either way.
            theme_display_ok = bool(
                best_theme
                and _theme_relevance(best_theme.get("theme_name",""),
                                     ticker_upper, company_lower) >= 3.0
            )

            # Coverage fallback: signal-discovered companies often have NO
            # beneficiary-table link (theme_ids empty) or only irrelevant links.
            # Their own signal corpus still tells us which active theme they
            # belong to — scan the FULL theme universe and take the best
            # evidence-backed match. Same relevance function, same ≥3.0 bar,
            # so this can never show a worse theme than the linked path.
            if not theme_display_ok and stage1_out:
                fb = max(
                    stage1_out,
                    key=lambda t: (
                        _theme_relevance(t.get("theme_name",""), ticker_upper, company_lower)
                        - max(0, (theme_rows.get(t.get("theme_id",0),{}).get("company_count",0) - 20)) * 0.05
                    ),
                    default=None
                )
                # Higher bar than the linked path (6.0 ≈ two independent keyword
                # matches): without a beneficiary-table link there is no prior
                # tying this company to the theme, so demand stronger evidence.
                if fb is not None and _theme_relevance(
                        fb.get("theme_name",""), ticker_upper, company_lower) >= 6.0:
                    best_theme = fb
                    theme_display_ok = True
                    focus_s = FOCUS_SCORE.get(fb.get("focus",""), focus_s)
                    conv_s  = CONV_SCORE.get(fb.get("conviction",""), conv_s)

            # ── The Investment Cycle Score ─────────────────────────────────────
            # The thesis is: Supply Constraint → Pricing Power → Capex → Revenue
            # Each step of the cycle multiplies conviction:
            #
            # Step 1 (CONSTRAINT): Company can't meet demand → they have pricing power
            #   capacity_constraint_seller, backlog_duration, capacity_utilization_high
            #   BASE SCORE — without this, nothing else matters
            #
            # Step 2 (PRICING): They're actually charging more
            #   realized_margin_expansion, pricing_power_emerging, supply_concentration
            #   MULTIPLIER — constraint alone could be temporary; pricing confirms it's real
            #
            # Step 3 (CAPEX): They're investing to capture the opportunity
            #   capex_increase — management believes in the demand; putting money behind it
            #   DURATION EXTENDER — capex = multi-year revenue visibility
            #
            # Step 4 (DEMAND): Demand still growing while they're constrained
            #   demand_surge — confirms the constraint isn't about to resolve naturally
            #   CONFIDENCE BOOSTER — sustained demand = the cycle will last longer
            #
            # All four present = HIGHEST CONVICTION
            # Three present = STRONG BUY
            # Two present = BUY
            # One (constraint only) = WATCH

            pricing_count = meta.get("pricing_count", 0)
            prior_c       = meta.get("prior_c_count", 0)
            c_delta       = max(0, c_count - prior_c)  # YoY spike in constraint signals

            # Noise guard: with signal counts this small (2-7 per year), a ±1
            # difference is filing-timing noise, not a trajectory. Only call a
            # change real if it's ≥2 signals AND ≥25% of the base — otherwise flat.
            #
            # "new" additionally requires the company to have FILED last year
            # (prior_docs > 0): a constraint newly appearing in an established
            # filer is Stage-1 alpha; a fresh IPO with no filing history is just
            # unproven — treat as "rising" so it can't outrank real emergences.
            prior_docs = meta.get("prior_docs", 0)
            _raw_diff = c_count - prior_c
            _base     = max(prior_c, 1)
            if prior_c == 0 and c_count > 0:
                trajectory = "new" if prior_docs > 0 else "rising"
            elif abs(_raw_diff) < 2 or abs(_raw_diff) / _base < 0.25:
                trajectory = "flat" if prior_c > 0 else "none"
            elif _raw_diff > 0:
                trajectory = "rising"
            else:
                trajectory = "declining"

            # Step 1 base: hard constraint signals × confidence
            step1 = min(c_count, 6) * avg_conf * 18

            # Step 2 multiplier: EITHER pricing OR capex expansion makes constraint investable
            # Pathway A: pricing power (realized_margin_expansion, pricing_power_emerging)
            # Pathway B: volume expansion (capex_increase shows management is building capacity)
            # Both equally valid — GE Vernova revenue exploded from VOLUME, not pricing
            pricing_signal = min(pricing_count, 3) * 0.15   # up to 0.45 from pricing
            capex_signal   = min(k_count, 4) * 0.12          # up to 0.48 from capex
            step2_mult = 1.0 + max(pricing_signal, capex_signal)  # take the better pathway

            # Step 3 duration: capex = years of revenue visibility ahead
            step3 = min(k_count, 5) * 10

            # Step 4 confidence: sustained demand = constraint won't resolve soon
            step4 = min(d_count - k_count, 3) * 4  # demand above and beyond capex

            # Theme quality: first detected this year = early mover advantage
            theme_bonus = focus_s * conv_s * 20

            # Competitor-constrained bonus: a rival's capacity is impaired while
            # this company can ship. Share gain arrives without any capex —
            # the highest-quality constraint variant (Micron vs impaired peers,
            # pipe makers when import supply was cut).
            comp_constrained = meta.get("comp_constrained", 0)
            comp_bonus = min(comp_constrained, 3) * 7   # up to +21

            # Intensity bonus from QUANTIFIED magnitudes: management stating a
            # number is a stronger claim than vague language, and the size of
            # the number is the size of the story.
            _og  = meta.get("max_order_growth", 0)   # % order growth
            _ut  = meta.get("max_utilization", 0)    # % capacity utilization
            _mb  = meta.get("max_margin_bps", 0)     # margin expansion bps
            _btb = meta.get("max_booktobill", 0)     # book-to-bill ratio
            intensity_bonus = (
                (12 if _og >= 50 else 6 if _og >= 25 else 0)
                + (10 if _ut >= 90 else 5 if _ut >= 80 else 0)
                + (6 if _mb >= 200 else 3 if _mb >= 100 else 0)
                + (6 if _btb >= 1.2 else 0)
            )
            intensity_bonus = min(intensity_bonus, 24)

            # PROMOTED from shadow testing (US 2020-24 calibration):
            # evidence_quality gate halved the decay rate (8.6% vs 15.6% base)
            # without hurting the confirm rate — quality avoids losers.
            quality_bonus = min(meta.get("evidence_quality", 0), 8.0) * 1.5  # up to +12

            raw_score = (
                step1 * step2_mult
                + step3
                + max(step4, 0)
                + theme_bonus
                + comp_bonus
                + intensity_bonus
                + quality_bonus
                + (5 if meta.get("capex_quote") else 0)
            )

            # PROMOTED dedup insight: many signals on very few distinct days is
            # one event told repeatedly (press release + intimation + transcript).
            # Measured neutral on confirms — adopted as a counting-integrity
            # haircut, damping the promotional filing pattern.
            _cd = meta.get("c_days", 0)
            if c_count >= 3 and _cd > 0 and c_count / _cd >= 3.0:
                raw_score *= 0.90

            # YoY delta ceiling: hard cap by (noise-guarded) trajectory so
            # persistent companies cannot crowd out newly-emerging ones.
            # New entrant → max 100, rising → max 88, flat → max 70, declining → max 55
            delta_ceiling = {
                "new": 100, "rising": 88, "flat": 70, "declining": 55,
            }.get(trajectory, 55)

            # Scale within the ceiling instead of hard-capping, so companies in
            # the same trajectory bucket still differentiate by evidence strength
            # (raw_score ~120-200 for strong evidence; /150 normalizes).
            rank_score = round(delta_ceiling * min(1.0, raw_score / 150), 1)

            n_themes = len(theme_ids)

            # ── World-class additions ─────────────────────────────────────────
            # Constraint cycle stage + exit signals + conviction tier
            # These are the 20% that makes this system world-class.

            # Supply easing — thesis-closing evidence. Counted directly in SQL
            # (the old check read meta["quality_signals"], a key that was never
            # populated, so the exit override silently never fired).
            # CAUTION: the extractor tags all supply_easing as perspective=
            # neutral and mis-fires on capacity-expansion language ("investing
            # in ... our supply chain"). So easing text alone NEVER forces an
            # exit — it only does when the YoY trajectory independently agrees
            # (declining). Otherwise it's surfaced as a watch trigger.
            easing_count = meta.get("easing_count", 0)
            has_easing_text  = easing_count >= 2 and easing_count * 2 >= c_count
            has_supply_easing = has_easing_text and trajectory == "declining"
            has_realized_margin = meta.get("pricing_count", 0) >= 1

            # ── Constraint stage: use COMPANY signal trajectory, not theme age ──
            # Theme `first_detected` is set once in 2020 for persistent themes →
            # age=78 months → everything ends up Stage 4. Wrong.
            # The correct signal is: how is THIS COMPANY's constraint evolving?
            #   prior_c=0, c_count>0  → Stage 1 (just emerged, highest alpha)
            #   delta>0 and prior_c>0 → Stage 2 (growing, still early)
            #   delta=0 and prior_c>0 → Stage 3 (flat/persistent, consensus)
            #   c_count < prior_c     → Stage 4 (declining, thesis resolving)
            # supply_easing signal overrides → Stage 4 regardless.
            # `trajectory` is the noise-guarded YoY classification computed above
            # (±1 signal on small counts = "flat", not a real move).
            if has_supply_easing:
                c_stage = 4
                stage_conf = 0.75
                time_horizon_m = 6
                conviction_tier = "⚫ REDUCE — Stage 4, thesis closing"
                exit_triggers = ["supply_easing_detected"]
            elif trajectory == "new":
                c_stage = 1
                stage_conf = round(0.55 + min(0.35, c_count * 0.05), 2)
                time_horizon_m = 30 if k_count >= 1 else 24
                conviction_tier = "🔴 STRONG BUY — Stage 1, early edge"
                exit_triggers = []
            elif trajectory == "rising":
                c_stage = 2
                stage_conf = 0.72
                time_horizon_m = 24 if k_count >= 2 else 18
                conviction_tier = "🟡 BUY — Stage 2, accelerating"
                exit_triggers = []
            elif trajectory == "flat":
                c_stage = 3
                stage_conf = 0.65
                time_horizon_m = 12
                conviction_tier = "🟢 HOLD — Stage 3, consensus forming"
                exit_triggers = ["momentum_flat_watch_for_easing"]
            else:
                c_stage = 4
                stage_conf = 0.60
                time_horizon_m = 6
                conviction_tier = "⚫ REDUCE — Stage 4, thesis closing"
                exit_triggers = ["constraint_signals_declining"]

            if has_realized_margin:
                stage_conf = min(0.92, stage_conf + 0.10)
            if k_count >= 2:
                stage_conf = min(0.90, stage_conf + 0.08)

            # Peer corroboration: other constrained companies in this
            # company's domain families. Independent confirmation raises
            # confidence; a lone claim in a domain nobody else sees stays low.
            peer_corr = max(
                (_family_counts.get(f, 0) for f in _co_families.get(name, ())),
                default=0,
            )
            peer_corr = max(0, peer_corr - 1)   # exclude self
            stage_conf = min(0.95, stage_conf + min(peer_corr, 5) * 0.02)

            # Easing language without a declining trajectory: not an exit, but
            # worth watching — surface it as a trigger on any stage.
            if has_easing_text and not has_supply_easing:
                exit_triggers = exit_triggers + ["easing_language_watch"]

            # ── Explosion potential: the full thesis chain lit up at once ──────
            # Constraint (c) + a monetization pathway (pricing OR capex) +
            # demand pressure (d) + early trajectory (new/rising) =
            # the NVDA-2022 / GE-Vernova setup. Each leg is generic arithmetic.
            explosion_legs = {
                "constraint":      c_count >= 2,
                # Monetization pathway: pricing power, capacity buildout, OR an
                # impaired competitor (share gain needs no capex at all)
                "pathway":         pricing_count >= 1 or k_count >= 2 or comp_constrained >= 1,
                "demand_pressure": d_count >= max(c_count, 4),
                "early_cycle":     trajectory in ("new", "rising"),
            }
            explosion_score = sum(explosion_legs.values())
            explosion_potential = explosion_score == 4

            # Stage-adjusted rank score: Stage 1 gets 25% bonus, Stage 4 gets 30% penalty
            stage_mult = {1: 1.25, 2: 1.10, 3: 0.90, 4: 0.70}.get(c_stage, 1.0)
            rank_score_adjusted = round(min(100, rank_score * stage_mult), 1)

            # Legacy conviction field
            conviction = (
                "high"   if (avg_conf >= 0.85 and c_count >= 3 and focus_s >= 0.9) else
                "high"   if (n_themes >= 2 and avg_conf >= 0.80 and c_count >= 2)   else
                "medium" if (avg_conf >= 0.75 and c_count >= 2)                     else
                "low"
            )

            results.append({
                "rank":              0,
                "company":           name,
                "ticker":            meta["ticker"],
                "company_role":      meta["role"],
                "theme":             best_theme["theme_name"] if (best_theme and theme_display_ok) else "",
                "theme_focus":       best_theme["focus"] if (best_theme and theme_display_ok) else "",
                "constraint_signals": c_count,
                "prior_constraint":   prior_c,
                "constraint_delta":   c_delta,
                "is_new_constraint":  prior_c == 0 and c_count > 0,
                "capex_signals":      k_count,
                "demand_signals":     d_count,
                "avg_confidence":    round(avg_conf, 3),
                "constrained_component": (
                    best_theme["constrained_components"][0]["component"]
                    if best_theme and best_theme.get("constrained_components") else ""
                ),
                "best_constraint_quote": _quality_quote(best_c.get("context_text","") if best_c else ""),
                "best_constraint_date":  str(best_c.get("filed_date","")) if best_c else "",
                "capex_quote":           _quality_quote(best_k.get("context_text","") if best_k else ""),
                "theme_count":           n_themes,
                "theme_names":           [theme_rows[tid]["theme_name"] for tid in theme_ids if tid in theme_rows],
                "conviction":            conviction,
                "time_horizon":          f"{time_horizon_m}m",
                "rank_score":            rank_score_adjusted,
                # ── World-class fields ─────────────────────────────────────────
                "constraint_stage":      c_stage,           # 1=emerging, 2=accel, 3=peak, 4=resolving
                "stage_confidence":      stage_conf,        # 0.55-0.92
                "conviction_tier":       conviction_tier,   # "🔴 STRONG BUY" etc.
                "time_horizon_months":   time_horizon_m,    # 6/12/18/24/30/36
                "exit_triggers":         exit_triggers,     # what to watch to sell
                "has_margin_expansion":  has_realized_margin,
                "has_supply_easing":     has_supply_easing,
                "first_signal_date":     meta.get("first_ever_signal", "") or meta.get("first_filing", ""),
                "last_signal_date":      meta.get("last_filing", ""),
                "shortlisted_date":      meta.get("shortlisted_date", ""),
                "trajectory":            trajectory,
                "explosion_potential":   explosion_potential,
                "explosion_legs":        explosion_score,   # 0-4 of the thesis chain lit
                "detection_path":        meta.get("detection_path", "hard_constraint"),
                "competitor_constrained": comp_constrained,
                "supply_easing_signals": easing_count,
                # Integrity flags (language-only, human-verification pointers):
                #   promotional_language — superlative density far above the
                #     candidate-pool median (promotion-heavy communication)
                #   unverified_narrative — demand-led story with ZERO peers
                #     seeing the same domain constrained (nobody corroborates)
                #   percent_only_claims — growth percentages announced but no
                #     absolute figures anywhere (percentages don't reconcile)
                "integrity_flags": [
                    f for f, cond in (
                        ("promotional_language",
                         _pd_median > 0 and _promo_density.get(name, 0) > 2.5 * _pd_median
                         and _promo_density.get(name, 0) > 2.0),
                        ("unverified_narrative",
                         meta.get("detection_path") == "demand_led" and peer_corr == 0),
                        ("percent_only_claims",
                         bool(_og) and not any(
                             cm in (meta.get("ctx_blob","") or "") for cm in _CURRENCY_MARKS)),
                    ) if cond
                ],
                # Selectivity: the conviction bar is set from MEASURED hit rates
                # (mg_fundamental_eval): peer corroboration 5+ confirmed 39% vs
                # 17% below it; top score band confirmed 50%; early trajectory
                # is where the payoff asymmetry lives. Everything else stays
                # visible as the watch list — nothing is hidden, only ranked.
                # Score bar raised 80→90: shadow variant v_score90 measured
                # 47.8% confirmed / 8.7% decayed — the strongest gate tested.
                "list_tier": (
                    "conviction"
                    if (trajectory in ("new", "rising")
                        and peer_corr >= 5
                        and explosion_score >= 3
                        and rank_score_adjusted >= 90)
                    else "watch"
                ),
                "peer_corroboration":    peer_corr,
                # Signal QUALITY over quantity (shadow-tested, not yet scored):
                #   constraint_days / demand_days — distinct evidence DAYS
                #     (dedups same-day press release + intimation + transcript)
                #   evidence_quality — confidence-weighted, quantified-claim
                #     premium; high count + low quality = promotional pattern
                "constraint_days":       meta.get("c_days", 0),
                "demand_days":           meta.get("ds_days", 0),
                "evidence_quality":      meta.get("evidence_quality", 0),
                "filings_in_window":     meta.get("filing_count", 0),
                # ── WHY SHORTLISTED: the full audit trail for the card ─────────
                # Answers the three questions a great investor asks:
                # why this (qualification), why now (trajectory), and what
                # would change my mind (invalidation conditions).
                "why_shortlisted": {
                    "path": meta.get("detection_path", "hard_constraint"),
                    "path_explanation": (
                        "Management explicitly describes ITS OWN capacity/backlog as "
                        "constrained (seller-perspective bottleneck language)."
                        if meta.get("detection_path") != "demand_led" else
                        "No explicit bottleneck sentence, but the GE-Vernova pattern is "
                        "lit: surging ORDER-anchored demand + building capacity + "
                        "margin/pricing proof — demand exceeding capacity, inferred."
                    ),
                    "qualification_checks": [
                        {"check": f"seller-constraint signals ≥ {min_constraint_signals}",
                         "value": c_count, "passed": c_count >= min_constraint_signals},
                        {"check": "order-anchored demand signals ≥ 3 (Path B)",
                         "value": meta.get("ds_seller", 0),
                         "passed": meta.get("ds_seller", 0) >= 3},
                        {"check": "capacity investment (capex signals ≥ 1)",
                         "value": k_count, "passed": k_count >= 1},
                        {"check": "pricing/margin proof ≥ 1",
                         "value": pricing_count, "passed": pricing_count >= 1},
                        {"check": "filings in window ≥ 3 (not a shell)",
                         "value": meta.get("filing_count", 0),
                         "passed": meta.get("filing_count", 0) >= 3},
                    ],
                    "trajectory_explanation": (
                        f"Prior year: {prior_c} constraint signals → this window: "
                        f"{c_count} → '{trajectory}'"
                        + (" (fresh emergence in an established filer — highest alpha)"
                           if trajectory == "new" else
                           " (evidence growing — cycle accelerating)"
                           if trajectory == "rising" else
                           " (steady multi-year story — consensus territory)"
                           if trajectory == "flat" else
                           " (evidence fading — thesis resolving)")
                    ),
                    "corroboration_explanation": (
                        f"{peer_corr} other companies show constraint evidence in the "
                        f"same domain this window"
                        + (" — independently confirmed." if peer_corr >= 5 else
                           " — thin corroboration; verify independently." if peer_corr
                           else " — NOBODY else sees this; treat as unverified.")
                    ),
                    "score_breakdown": {
                        "constraint_base":     round(step1, 1),
                        "pathway_multiplier":  round(step2_mult, 2),
                        "capex_duration":      step3,
                        "demand_confidence":   max(step4, 0),
                        "theme_bonus":         round(theme_bonus, 1),
                        "competitor_bonus":    comp_bonus,
                        "intensity_bonus":     intensity_bonus,
                        "quality_bonus":       round(quality_bonus, 1),
                        "raw_total":           round(raw_score, 1),
                        "trajectory_ceiling":  delta_ceiling,
                        "stage_multiplier":    stage_mult,
                        "final":               rank_score_adjusted,
                    },
                    "would_change_my_mind": (
                        [t for t in exit_triggers] +
                        ([f"supply easing language appearing ({easing_count} mentions already)"]
                         if easing_count else []) +
                        ["peer corroboration collapsing (domain no longer confirmed)"
                         if peer_corr >= 5 else
                         "still awaiting independent confirmation from peers"]
                    ),
                },
                "order_growth_pct":      _og or None,
                "utilization_pct":       _ut or None,
                "margin_expansion_bps":  _mb or None,
                "book_to_bill":          _btb or None,
                "intensity_bonus":       intensity_bonus,
            })

        # Sort: conviction list first (measured-quality bar), then by stage
        # (Stage 1 strongest alpha → Stage 4), then rank_score DESC.
        results.sort(key=lambda r: (
            0 if r.get("list_tier") == "conviction" else 1,
            r.get("constraint_stage", 3),   # lower stage = better
            -r["rank_score"],
        ))
        for i, r in enumerate(results):
            r["rank"] = i + 1

        # Universe coverage: how much of the filing universe the signal layer
        # actually reads. High coverage (~95%) means a company's SILENCE while
        # its domain is constrained is itself information.
        try:
            with pg._conn() as _cov_conn:
                cur2 = _cov_conn.cursor()
                cur2.execute(
                    """SELECT COUNT(DISTINCT company) FROM mg_documents
                       WHERE country=%s AND filed_at BETWEEN %s AND %s""",
                    (country, from_d, to_d))
                _cos_with_docs = cur2.fetchone()[0] or 0
                cur2.execute(
                    """SELECT COUNT(DISTINCT d.company) FROM mg_signals s
                       JOIN mg_documents d ON d.id=s.document_id
                       WHERE d.country=%s AND d.filed_at BETWEEN %s AND %s""",
                    (country, from_d, to_d))
                _cos_with_signals = cur2.fetchone()[0] or 0
        except Exception:
            _cos_with_docs = _cos_with_signals = 0

        stats = {
            "stage1_themes":          len(stage1_out),
            "stage2_signal_companies": len(co_meta),          # companies with signals this year
            "stage3_with_evidence":   len(co_meta),           # all signal companies have evidence
            "stage4_qualify":         len(results),
            "final_count":            min(len(results), 50),
            "universe_companies_with_filings": _cos_with_docs,
            "universe_companies_with_signals": _cos_with_signals,
            "signal_coverage_pct": (
                round(100.0 * _cos_with_signals / _cos_with_docs, 1)
                if _cos_with_docs else 0
            ),
        }

        # Persist a run snapshot for LIVE runs only (current year / no year):
        # the diff between consecutive snapshots is the weekly "what changed"
        # feed. Historical-year queries are backtests, not runs — not stored.
        if year is None or yr >= date.today().year:
            try:
                _save_shortlist_snapshot(country, results[:50])
            except Exception as _se:
                logging.warning("snapshot save failed: %s", _se)

        return {
            "year":            yr,
            "country":         country,
            "period":          f"{from_d} → {to_d}",
            "stats":           stats,
            "constraint_regimes": stage1_out,
            "final_shortlist": results[:50],
            # Supply-chain propagation: domains confirmed constrained by ≥3
            # independent companies, with the upstream input components to
            # watch — their suppliers typically report constraint a quarter+
            # later. This is where NEXT quarter's Stage-1 names come from.
            "upstream_watch":  upstream_watch,
        }

    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        logging.error("investment_final_shortlist: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=f"Final shortlist failed: {e} | {tb[-500:]}")


@app.get("/api/debug/window-end-dist")
def debug_window_end(country: str = "US") -> list[dict]:
    """Returns count of beneficiary rows per window_end year — lets you verify
    that year-by-year replays produced distinct window_end buckets."""
    pg = get_pg()
    if not pg:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT
                           EXTRACT(YEAR FROM tb.window_end)::int AS window_year,
                           COUNT(DISTINCT tb.entity_id)          AS companies,
                           COUNT(*)                              AS beneficiary_rows,
                           MIN(tb.window_end)::date             AS earliest,
                           MAX(tb.window_end)::date             AS latest
                       FROM mg_theme_beneficiaries tb
                       JOIN mg_themes t ON t.id = tb.theme_id
                       WHERE t.country = %s AND t.is_active = TRUE
                         AND tb.window_end IS NOT NULL
                       GROUP BY 1
                       ORDER BY 1""",
                    (country,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logging.error("debug_window_end: %s", e)
        return []

@app.get("/api/kpis")
def get_kpis(country: str = "US") -> dict:
    pg = get_pg()
    if not pg:
        return {}
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM mg_documents WHERE country=%s)            AS total_docs,
                        (SELECT COUNT(DISTINCT de.entity_id)
                           FROM mg_document_entities de
                           JOIN mg_documents d ON d.id=de.document_id
                          WHERE d.country=%s)                                           AS total_entities,
                        (SELECT COUNT(*) FROM mg_signals WHERE country=%s)              AS total_signals,
                        (SELECT COUNT(*) FROM mg_themes WHERE is_active=TRUE AND country=%s) AS active_themes,
                        (SELECT COUNT(*) FROM mg_events WHERE country=%s)               AS total_events,
                        (SELECT COUNT(*) FROM mg_causal_chains WHERE is_active=TRUE AND country=%s) AS active_chains,
                        (SELECT COUNT(*) FROM mg_replay_runs)                           AS replay_runs
                """, (country,)*6)
                row = cur.fetchone()
                return dict(row) if row else {}
    except Exception as e:
        logging.error("get_kpis: %s", e)
        return {}


@app.get("/api/config/info")
def get_config_info() -> dict:
    acfg = CFG.get("anthropic", {})
    ecfg = CFG.get("edgar", {})
    return {
        "gemini_configured": bool(acfg.get("api_key")),
        "gemini_model": acfg.get("model", "claude-sonnet-4-6"),
        "ticker_list": ecfg.get("ticker_list", []),
        "version": "0.2.0",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# THEMES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/themes")
def get_themes(
    country: str = "US",
    min_strength: float = 0.0,
    as_of: str | None = None,
    from_date: str | None = None,
) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        if as_of:
            return pg.get_themes_as_of(
                as_of_date=date.fromisoformat(as_of),
                from_date=date.fromisoformat(from_date) if from_date else date(2020, 1, 1),
                min_strength=min_strength,
                country=country,
            )
        return pg.get_active_themes(min_strength=min_strength, country=country)
    except Exception as e:
        logging.error("get_themes: %s", e)
        return []


@app.get("/api/themes/ranking")
def get_ranking(country: str = "US") -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        rows = pg.get_active_themes(min_strength=0.0, country=country)
        rows = sorted(rows, key=lambda r: float(r.get("strength_score") or 0), reverse=True)
        table = []
        for rank, r in enumerate(rows, 1):
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            d   = meta.get("demand_count", 0) or 0
            s   = meta.get("supply_constraint_count", 0) or 0
            q   = int(meta.get("quarter_count") or meta.get("confirmed_quarters") or 0)
            pers = float(meta.get("persistence_multiplier") or 1.0)
            theme_type = meta.get("theme_type", "") or "auto"
            _elig = meta.get("eligibility_score")
            if _elig is not None:
                elig = round(float(_elig), 2)
            else:
                _dem = float(meta.get("demand_count", 0) or 0)
                _sup = float(meta.get("supply_constraint_count", 0) or 0)
                _cap = float(meta.get("capex_count", 0) or 0)
                _cos = float(r.get("company_count") or 0)
                _ckw = float(meta.get("constraint_kw_count", 0) or 0)
                _wt  = float(meta.get("weighted_constraint_score", 0) or 0)
                _bn  = bool(meta.get("is_bottleneck") or theme_type == "bottleneck" or _ckw >= 3)
                _p   = 1.0 if q >= 3 else (0.7 if q == 2 else 0.3)
                elig = round(
                    min(_dem / 50.0, 1.0) * 0.20
                    + min((_sup + _ckw * 1.5 + _wt * 0.5) / 40.0, 1.0) * 0.25
                    + min(_cap / 15.0, 1.0) * 0.20
                    + min(_cos / 10.0, 1.0) * 0.15
                    + _p * 0.10 + float(_bn) * 0.10, 2)
            _first_det = r.get("first_detected")
            if _first_det:
                _fd_date = _first_det if isinstance(_first_det, date) else date.fromisoformat(str(_first_det)[:10])
                _age_days = (date.today() - _fd_date).days
                _fresh = "Fresh" if _age_days <= 90 else ("Active" if _age_days <= 365 else "Mature")
                _first_det_str = str(_fd_date)
            else:
                _age_days = 9999
                _fresh = "Unknown"
                _first_det_str = "—"
            table.append({
                "rank":        rank,
                "theme":       r.get("theme_name", ""),
                "score":       round(float(r.get("strength_score") or 0), 1),
                "ds":          f'D{int(d)}/S{int(s)}',
                "conviction":  (r.get("conviction") or "emerging").title(),
                "companies":   int(r.get("company_count") or 0),
                "quarters":    q,
                "persistence": round(pers, 2),
                "eligibility": elig,
                "type":        theme_type,
                "first_seen":  _first_det_str,
                "freshness":   _fresh,
                "slug":        r.get("theme_slug", ""),
            })
        return table
    except Exception as e:
        logging.error("get_ranking: %s", e)
        return []


@app.get("/api/themes/year-focus")
def get_year_focus(country: str = "US", year: int | None = None) -> list[dict]:
    """Year-over-year constraint delta — surfaces only what CHANGED this year.

    Returns themes classified as:
      new        — first constraint evidence this year
      escalating — constraint signals grew ≥50% vs prior year
      easing     — constraint signals fell ≥25%
      persistent — same level (structural/priced-in)

    Ordered: new → escalating → persistent → easing.
    Callers should default-show only new + escalating.
    """
    pg = get_pg()
    if not pg:
        return []
    try:
        yr = year or date.today().year
        return pg.get_year_focus_analysis(yr, country)
    except Exception as e:
        logging.error("get_year_focus: %s", e)
        return []


@app.get("/api/themes/year-constraints")
def get_year_constraints(
    country: str = "US",
    year: int | None = None,
) -> list[dict]:
    """Per-theme constraint signal summary for a year.
    Returns themes ranked by raw constraint evidence (supply_bottleneck etc.)
    NOT by number of companies or concall volume — purely by signal intensity."""
    pg = get_pg()
    if not pg:
        return []
    try:
        yr = year or date.today().year
        from_d = date(yr, 1, 1)
        to_d   = date(yr, 12, 31)
        return pg.get_year_constraint_summary(from_d, to_d, country)
    except Exception as e:
        logging.error("get_year_constraints: %s", e)
        return []


@app.get("/api/themes/{theme_id}/constraint-components")
def get_constraint_components(
    theme_id: int,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Mine supply_bottleneck signal context_text to surface what physical
    components are actually constrained for this theme in the year window.
    Data-driven — no hardcoded component lists."""
    pg = get_pg()
    if not pg:
        return []
    try:
        _from = date.fromisoformat(from_date) if from_date else date(date.today().year, 1, 1)
        _to   = date.fromisoformat(to_date)   if to_date   else date.today()
        return pg.get_constraint_components(theme_id, _from, _to)
    except Exception as e:
        logging.error("get_constraint_components: %s", e)
        return []


@app.get("/api/themes/shortlisted")
def get_shortlisted(country: str = "US", min_quarters: int = 3, year: int | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_shortlisted_themes(min_quarters=min_quarters, country=country, year=year)
    except Exception as e:
        logging.error("get_shortlisted: %s", e)
        return []


@app.get("/api/themes/{theme_id}/beneficiaries")
def get_beneficiaries(theme_id: int, as_of: str | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        if as_of:
            return pg.get_beneficiaries_as_of(theme_id, date.fromisoformat(as_of))
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT b.ticker, b.company_name, b.beneficiary_type, b.company_role,
                              b.relevance_score, b.signal_count,
                              COALESCE(b.capex_signals,0) AS capex_signals,
                              b.rank_in_theme, b.reasoning, b.first_seen_at, b.last_seen_at
                       FROM mg_theme_beneficiaries b
                       WHERE b.theme_id=%s
                       ORDER BY b.rank_in_theme NULLS LAST, b.relevance_score DESC""",
                    (theme_id,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logging.error("get_beneficiaries: %s", e)
        return []


@app.get("/api/themes/{theme_id}/snapshots")
def get_snapshots(theme_id: int, from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        if from_date and to_date:
            return pg.get_snapshots_in_window(
                theme_id,
                date.fromisoformat(from_date),
                date.fromisoformat(to_date),
            )
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT snapshot_date, strength_score, momentum_score, doc_count
                       FROM mg_theme_snapshots WHERE theme_id=%s ORDER BY snapshot_date""",
                    (theme_id,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logging.error("get_snapshots: %s", e)
        return []


@app.get("/api/themes/{theme_id}/quarterly")
def get_quarterly(theme_id: int, as_of: str | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_quarterly_persistence(theme_id, as_of or str(date.today()))
    except Exception as e:
        logging.error("get_quarterly: %s", e)
        return []


@app.get("/api/themes/{slug}/source-companies")
def get_source_companies(slug: str, as_of: str | None = None, from_date: str | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_source_companies_for_theme(
            slug,
            as_of or str(date.today()),
            since_date=from_date or str(date(2020, 1, 1)),
        )
    except Exception as e:
        logging.error("get_source_companies: %s", e)
        return []


@app.get("/api/themes/{slug}/evidence")
def get_evidence(slug: str, as_of: str | None = None, from_date: str | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_signal_evidence_for_theme(
            slug,
            as_of or str(date.today()),
            since_date=from_date or str(date(2020, 1, 1)),
        )
    except Exception as e:
        logging.error("get_evidence: %s", e)
        return []


@app.get("/api/themes/{slug}/macro-context")
def get_macro_context(slug: str, as_of: str | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_theme_macro_context(slug, as_of or str(date.today()))
    except Exception as e:
        logging.error("get_macro_context: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# CANONICAL REVIEWS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/canonical/pending")
def get_pending_canonical() -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_pending_canonical_reviews()
    except Exception as e:
        logging.error("get_pending_canonical: %s", e)
        return []


class CanonicalApproveBody(BaseModel):
    approvals: dict[str, str]


@app.post("/api/canonical/approve")
def approve_canonical(body: CanonicalApproveBody) -> dict:
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")
    try:
        count = pg.bulk_approve_canonical_reviews(body.approvals)
        return {"approved": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/canonical/dismiss/{cluster_id}")
def dismiss_canonical(cluster_id: str) -> dict:
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")
    try:
        pg.dismiss_canonical_review(cluster_id)
        return {"dismissed": cluster_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CanonicalAIBody(BaseModel):
    prompt: str


@app.post("/api/canonical/ai-resolve")
def canonical_ai_resolve(body: CanonicalAIBody) -> dict:
    try:
        result = _call_claude(body.prompt)
        return {"result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# CAUSAL CHAINS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/causal-chains")
def get_causal_chains(
    country: str = "US",
    as_of: str = None,
    from_date: str = None,
) -> list[dict]:
    """Return active causal chains, optionally filtered and scored by date window.

    When as_of / from_date are provided (historical mode), chains are:
      - Filtered to only those first_detected <= as_of
      - Re-scored dynamically: activation_score is augmented by actual signal
        evidence from the [from_date, as_of] window so scores reflect what was
        active in that specific period, not the current state.
    """
    pg = get_pg()
    if not pg:
        return []
    try:
        _as_of    = date.fromisoformat(as_of)    if as_of    else date.today()
        _from     = date.fromisoformat(from_date) if from_date else (_as_of - timedelta(days=365))

        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Filter chains to those first detected on or before as_of date.
                # NULL first_detected chains are always included (legacy/policy chains).
                _fd_clause = "AND (first_detected IS NULL OR first_detected <= %s)"
                _fd_params = (country, _as_of)

                # ── Fetch chains active in the year window ─────────────────
                # "Active" = first_detected <= as_of AND the chain's entity had
                # ACTUAL constraint or demand signals in [from_date, as_of].
                # We do this in two steps: fetch candidates, then prune by signal evidence.
                cur.execute(
                    f"""SELECT chain_id, chain_name, depth, terminal_effect,
                              activation_score, last_scored_at, first_detected, links
                       FROM mg_causal_chains
                       WHERE is_active = TRUE
                         AND country   = %s
                         {_fd_clause}
                       ORDER BY activation_score DESC""",
                    _fd_params,
                )
                rows = [dict(r) for r in cur.fetchall()]

                if not rows:
                    return []

                import re as _re, math as _math

                def _chain_keywords(chain_name: str) -> list[str]:
                    """Extract entity keywords from all segments of a chain name."""
                    name = chain_name or ""
                    # Split on arrow separators to get each hop label
                    segments = _re.split(r'→|->|–>', name)
                    keywords = []
                    for seg in segments:
                        # Strip trailing qualifiers: Demand, Supply, Constraint, Surge, etc.
                        clean = _re.sub(
                            r'\b(demand|supply|constraint|surge|shortage|adoption|pressure|tension|response|infrastructure|buildout)\b',
                            '', seg, flags=_re.I
                        ).strip()
                        if clean and len(clean) > 2:
                            keywords.append(clean.lower())
                    return keywords or [name.split()[0].lower()]

                # ── Re-score chains using CONSTRAINT signals in the window ─────
                # Constraint signals (supply_bottleneck, inventory_drawdown, etc.)
                # are the primary evidence that a causal chain is active.
                # Demand signals provide corroboration. We weight constraint 2×
                # so chains backed by real supply tightness float to the top.
                all_keywords = set()
                for row in rows:
                    all_keywords.update(_chain_keywords(row.get("chain_name", "")))
                all_keywords.discard("")

                entity_signal_counts: dict[str, dict] = {}
                if all_keywords:
                    try:
                        placeholders = ",".join(["%s"] * len(all_keywords))
                        cur.execute(
                            f"""SELECT lower(e.canonical_name) AS ename,
                                       COUNT(*) FILTER (
                                           WHERE s.signal_type IN (
                                               'supply_bottleneck','inventory_drawdown',
                                               'capacity_shortage','demand_exceeds_supply'
                                           )
                                       )                        AS constraint_sigs,
                                       COUNT(*) FILTER (
                                           WHERE s.signal_type IN (
                                               'demand_surge','technology_adoption',
                                               'capex_increase'
                                           )
                                       )                        AS demand_sigs,
                                       COUNT(DISTINCT d.company) AS cos
                                FROM mg_signals s
                                JOIN mg_documents d ON d.id = s.document_id
                                JOIN mg_document_entities de ON de.document_id = s.document_id
                                JOIN mg_entities e ON e.id = de.entity_id
                                WHERE d.country = %s
                                  AND d.filed_at BETWEEN %s AND %s
                                  AND lower(e.canonical_name) IN ({placeholders})
                                GROUP BY lower(e.canonical_name)""",
                            [country, _from, _as_of] + list(all_keywords),
                        )
                        for r in cur.fetchall():
                            entity_signal_counts[r["ename"]] = {
                                "constraint_sigs": int(r["constraint_sigs"] or 0),
                                "demand_sigs":     int(r["demand_sigs"]     or 0),
                                "cos":             int(r["cos"]             or 0),
                            }
                    except Exception as _se:
                        logging.warning("causal-chains entity scoring: %s", _se)

                active_rows = []
                for row in rows:
                    keywords = _chain_keywords(row.get("chain_name", ""))
                    c_sigs = sum(entity_signal_counts.get(k, {}).get("constraint_sigs", 0) for k in keywords)
                    d_sigs = sum(entity_signal_counts.get(k, {}).get("demand_sigs", 0) for k in keywords)
                    cos    = sum(entity_signal_counts.get(k, {}).get("cos", 0) for k in keywords)

                    # Drop chains with zero signal evidence in the year window
                    # (unless it's the live view where we keep everything)
                    if from_date and (c_sigs + d_sigs) == 0:
                        continue

                    # Score: constraint evidence weighted 2× demand evidence
                    # (constraint signals are rarer and more diagnostic)
                    total_weighted = c_sigs * 2 + d_sigs
                    sig_score  = min(1.0, _math.log1p(total_weighted) / _math.log1p(3000))
                    cos_score  = min(1.0, _math.log1p(cos) / _math.log1p(200))
                    new_score  = max(10.0, round(10.0 + (sig_score * 0.7 + cos_score * 0.3) * 90.0, 1))

                    row["activation_score"]       = new_score
                    row["constraint_signals"]      = c_sigs
                    row["demand_signals"]          = d_sigs
                    row["companies_in_window"]     = cos
                    active_rows.append(row)

                active_rows.sort(key=lambda r: -r["activation_score"])

                for row in active_rows:
                    row.pop("chain_id", None)

                return active_rows
    except Exception as e:
        logging.error("get_causal_chains: %s", e)
        return []


# ── India PLI Schemes master list ────────────────────────────────────────────
# Sourced from Ministry of Commerce & Industry, DPIIT press releases.
# Each scheme has: sector, budget (₹ Cr), incentive structure, timeline,
# key beneficiaries, and a layman impact explanation.
_INDIA_PLI_SCHEMES = [
    {
        "policy_id": "pli-mobile-2020",
        "title": "PLI for Mobile Phones & Electronic Components",
        "policy_type": "PLI",
        "sector": "Electronics",
        "year": 2020,
        "enacted_date": "2020-04-01",
        "budget_crore": 12195,
        "incentive": "4–6% on incremental sales for 5 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Dixon Technologies", "Foxconn (Apple)", "Samsung", "Lava", "Optiemus"],
        "technologies_affected": ["Smartphones", "Feature phones", "Electronic components"],
        "layman_impact": "India is now the world's 2nd largest mobile phone producer. Apple makes ~14% of iPhones here. Dixon, Foxconn get cash from govt for every extra phone they make vs baseline.",
    },
    {
        "policy_id": "pli-semiconductor-2021",
        "title": "PLI for Semiconductors & Display Fabs",
        "policy_type": "PLI",
        "sector": "Semiconductors",
        "year": 2021,
        "enacted_date": "2021-12-15",
        "budget_crore": 76000,
        "incentive": "50% capex subsidy for fab setup",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Tata Electronics", "CG Power (Renesas)", "Micron Technology", "Kaynes"],
        "technologies_affected": ["ATMP packaging", "Fab", "OSAT", "Display manufacturing"],
        "layman_impact": "Govt pays 50% of cost to set up chip factories. Micron opened India's first chip plant (Sanand, Gujarat). Tata plans full fab by 2026. This ends India's 100% import dependency on chips.",
    },
    {
        "policy_id": "pli-solar-2021",
        "title": "PLI for Solar PV Modules (Tranche I & II)",
        "policy_type": "PLI",
        "sector": "Renewable Energy",
        "year": 2021,
        "enacted_date": "2021-04-07",
        "budget_crore": 24000,
        "incentive": "4–5% on module sales for 5 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Waaree Energies", "Adani Solar", "Premier Energies", "Vikram Solar", "Goldi Solar"],
        "technologies_affected": ["Solar cells", "Solar modules", "Wafers", "Polysilicon"],
        "layman_impact": "India had almost zero solar module manufacturing. Now Waaree, Adani Solar are building GW-scale factories. Govt pays them per MW of module output above baseline. Target: 65 GW domestic capacity by 2026.",
    },
    {
        "policy_id": "pli-battery-2021",
        "title": "PLI for Advanced Chemistry Cell (ACC) Batteries",
        "policy_type": "PLI",
        "sector": "Energy Storage / EV",
        "year": 2021,
        "enacted_date": "2021-05-12",
        "budget_crore": 18100,
        "incentive": "₹2,000–20 per kWh for 5 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Ola Electric", "Reliance (Faradion)", "Hyundai", "ACME Cleantech"],
        "technologies_affected": ["Li-ion cells", "NMC", "LFP chemistry", "Solid-state"],
        "layman_impact": "India imports nearly all EV batteries from China. This PLI pays companies to build battery cell factories here. Every kWh of cell made earns govt subsidy. Reduces EV cost and China dependency.",
    },
    {
        "policy_id": "pli-auto-2021",
        "title": "PLI for Automobiles & Auto Components",
        "policy_type": "PLI",
        "sector": "Automotive",
        "year": 2021,
        "enacted_date": "2021-09-15",
        "budget_crore": 25938,
        "incentive": "13–18% on incremental sales (EVs get higher slab)",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Tata Motors", "M&M", "Hyundai India", "Maruti Suzuki", "Bharat Forge", "Sona BLW"],
        "technologies_affected": ["EVs", "Hybrid vehicles", "Hydrogen fuel cell", "Auto electronics"],
        "layman_impact": "Incentivises car makers to invest in EV platforms. EV makers get biggest payouts. Maruti and others are now building India-specific EV models. Auto component makers like Bharat Forge also qualify.",
    },
    {
        "policy_id": "pli-pharma-2020",
        "title": "PLI for Bulk Drugs (Active Pharmaceutical Ingredients)",
        "policy_type": "PLI",
        "sector": "Pharmaceuticals",
        "year": 2020,
        "enacted_date": "2020-07-27",
        "budget_crore": 6940,
        "incentive": "10–20% on incremental domestic sales for 6 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 4,
        "key_companies": ["Dr Reddy's", "Divi's Labs", "Aurobindo Pharma", "Jubilant Pharmova"],
        "technologies_affected": ["APIs", "Key starting materials", "Fermentation-based drugs"],
        "layman_impact": "India imports 70% of drug ingredients from China. This PLI pays pharma companies to make APIs in India instead. Reduces supply chain risk. Divi's Labs is a major beneficiary — makes APIs for global drug cos.",
    },
    {
        "policy_id": "pli-pharma2-2021",
        "title": "PLI for Pharmaceuticals (Finished Dose Formulations)",
        "policy_type": "PLI",
        "sector": "Pharmaceuticals",
        "year": 2021,
        "enacted_date": "2021-07-01",
        "budget_crore": 15000,
        "incentive": "4–10% on incremental sales for 6 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 4,
        "key_companies": ["Sun Pharma", "Cipla", "Lupin", "Biocon", "Granules India"],
        "technologies_affected": ["Biopharmaceuticals", "Orphan drugs", "Complex generics"],
        "layman_impact": "Focus on high-value drugs — biosimilars, cancer drugs, complex generics. Companies get govt cash to develop products India currently imports. Biocon's Krabeva (biosimilar Herceptin) is a flagship example.",
    },
    {
        "policy_id": "pli-steel-2021",
        "title": "PLI for Specialty Steel",
        "policy_type": "PLI",
        "sector": "Steel",
        "year": 2021,
        "enacted_date": "2021-07-22",
        "budget_crore": 6322,
        "incentive": "4–12% on incremental production for 5 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 4,
        "key_companies": ["Tata Steel", "JSW Steel", "SAIL", "Jindal Stainless"],
        "technologies_affected": ["Coated steel", "High-strength steel", "Electrical steel (CRGO)", "Alloy steel"],
        "layman_impact": "India imports specialty steel (like transformer steel CRGO) almost entirely. This PLI pays Tata/JSW to make it locally. CRGO for transformers is a critical supply chain gap — grid expansion depends on it.",
    },
    {
        "policy_id": "pli-textile-2021",
        "title": "PLI for Textile Products (Man-Made Fibre & Technical Textiles)",
        "policy_type": "PLI",
        "sector": "Textiles",
        "year": 2021,
        "enacted_date": "2021-09-08",
        "budget_crore": 10683,
        "incentive": "15% (Yr 1-2), 11% (Yr 3-5) on incremental turnover",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 3,
        "key_companies": ["Trident Group", "Welspun India", "Vardhman Textiles", "RSWM"],
        "technologies_affected": ["MMF fabrics", "Technical textiles", "Geotextiles", "Agrotextiles"],
        "layman_impact": "India is strong in cotton textiles but weak in synthetic/technical fabrics. This PLI incentivises companies to move up the value chain. Technical textiles (used in cars, hospitals, construction) are growing 20% annually.",
    },
    {
        "policy_id": "pli-food-2021",
        "title": "PLI for Food Processing Industries",
        "policy_type": "PLI",
        "sector": "Food Processing",
        "year": 2021,
        "enacted_date": "2021-03-31",
        "budget_crore": 10900,
        "incentive": "10–15% on incremental sales for 6 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 3,
        "key_companies": ["ITC", "Nestle India", "Hindustan Unilever", "Adani Wilmar", "Britannia"],
        "technologies_affected": ["Ready-to-eat", "Processed fruits/veg", "Marine products", "Innovative/organic"],
        "layman_impact": "India wastes 30% of food due to poor processing. This PLI builds cold chains and processing capacity. Companies that add mango pulp, ready meals, or marine exports above their 2019-20 baseline get govt cash.",
    },
    {
        "policy_id": "pli-whitegoods-2021",
        "title": "PLI for White Goods (AC & LED Lights)",
        "policy_type": "PLI",
        "sector": "Consumer Electronics",
        "year": 2021,
        "enacted_date": "2021-04-16",
        "budget_crore": 6238,
        "incentive": "4–6% on incremental domestic value addition",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 3,
        "key_companies": ["Voltas", "Blue Star", "Havells", "Orient Electric", "Amber Enterprises"],
        "technologies_affected": ["Air conditioners", "AC components", "LED lights", "Energy-efficient compressors"],
        "layman_impact": "India imports most AC components (compressors, heat exchangers) from China. This PLI pays companies to make them locally. Amber Enterprises (makes ACs for Voltas, Daikin) is major beneficiary.",
    },
    {
        "policy_id": "pli-telecom-2021",
        "title": "PLI for Telecom & Networking Products",
        "policy_type": "PLI",
        "sector": "Telecom",
        "year": 2021,
        "enacted_date": "2021-02-24",
        "budget_crore": 12195,
        "incentive": "6–7% on incremental sales for 5 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 4,
        "key_companies": ["Dixon Technologies", "Tejas Networks", "HFCL", "Sterlite Tech"],
        "technologies_affected": ["4G/5G equipment", "Routers", "Switches", "Optical fiber"],
        "layman_impact": "India cannot afford to use Huawei. This PLI builds domestic 4G/5G gear manufacturing. Tejas Networks (Tata group) now supplies 4G gear to BSNL for ₹10,000 Cr+ order. Strategic — reduces telecom import dependence.",
    },
    {
        "policy_id": "pli-medtech-2020",
        "title": "PLI for Medical Devices",
        "policy_type": "PLI",
        "sector": "Healthcare",
        "year": 2020,
        "enacted_date": "2020-07-27",
        "budget_crore": 3420,
        "incentive": "5% on incremental sales for 5 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 3,
        "key_companies": ["Siemens Healthineers India", "Trivitron Healthcare", "Poly Medicure"],
        "technologies_affected": ["MRI machines", "CT scanners", "Cardiac stents", "Surgical instruments"],
        "layman_impact": "India imports 85% of medical devices. This PLI incentivises local manufacture of high-value equipment. Most India-made devices are low-end; PLI pushes towards CT/MRI machines.",
    },
    {
        "policy_id": "pli-drone-2021",
        "title": "PLI for Drones & Drone Components",
        "policy_type": "PLI",
        "sector": "Defence / Aviation",
        "year": 2021,
        "enacted_date": "2021-09-15",
        "budget_crore": 120,
        "incentive": "20% on value addition for 3 years",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 3,
        "key_companies": ["IdeaForge", "Throttle Aerospace", "Garuda Aerospace"],
        "technologies_affected": ["Agricultural drones", "Surveillance drones", "Delivery drones", "Drone components"],
        "layman_impact": "Smallest PLI but strategically important. Agricultural drones can spray pesticides 40× faster than manual. Army needs surveillance drones. IdeaForge (listed) is key beneficiary.",
    },
    # Budget / Policy announcements
    {
        "policy_id": "budget-infra-2023",
        "title": "Union Budget 2023-24: ₹10 Lakh Crore Infrastructure Push",
        "policy_type": "BUDGET",
        "sector": "Infrastructure",
        "year": 2023,
        "enacted_date": "2023-02-01",
        "budget_crore": 1000000,
        "incentive": "Capex outlay for roads, railways, defence, housing",
        "status": "Enacted",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["L&T", "KNR Constructions", "PNC Infratech", "IRB Infrastructure"],
        "technologies_affected": ["Roads", "Highways", "Railways", "Urban metro", "Defence"],
        "layman_impact": "Govt spending ₹10 lakh crore — think of it as the world's biggest contractor paying construction companies to build. Every infra company in India benefits. Roads, railways, airports all get boosted simultaneously.",
    },
    {
        "policy_id": "pm-surya-ghar-2024",
        "title": "PM Surya Ghar Muft Bijli Yojana (Free Solar for Homes)",
        "policy_type": "SUBSIDY",
        "sector": "Renewable Energy",
        "year": 2024,
        "enacted_date": "2024-02-13",
        "budget_crore": 75021,
        "incentive": "300 units free electricity/month + subsidy on rooftop solar",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Waaree Energies", "Premier Energies", "REC Solar", "Goldi Solar"],
        "technologies_affected": ["Rooftop solar panels", "Inverters", "Net metering", "Solar EPC"],
        "layman_impact": "Govt gives every household with rooftop solar 300 units free/month + cash subsidy. Target: 1 Cr homes. This creates instant demand for rooftop solar installation. Solar panel makers and EPC companies are direct winners.",
    },
    {
        "policy_id": "railway-capex-2024",
        "title": "Indian Railways ₹2.52 Lakh Crore Capex 2024-25",
        "policy_type": "INFRASTRUCTURE",
        "sector": "Railways",
        "year": 2024,
        "enacted_date": "2024-02-01",
        "budget_crore": 252000,
        "incentive": "New lines, electrification, Vande Bharat, metro",
        "status": "Enacted",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Titagarh Rail Systems", "RVNL", "IRCON", "Texmaco Rail", "Medha Servo"],
        "technologies_affected": ["Wagons", "Locomotives", "Signalling", "Vande Bharat coaches", "Metro rail"],
        "layman_impact": "Railways budget is 9× of 2013-14 level. Every train coach, locomotive, rail, bolt is procured from Indian companies. Titagarh Rail and Texmaco make wagons; RVNL builds the lines. All seeing multi-year order books.",
    },
    {
        "policy_id": "defence-offset-make2",
        "title": "Defence Indigenisation: 68% Procurement from Domestic",
        "policy_type": "MANDATE",
        "sector": "Defence",
        "year": 2021,
        "enacted_date": "2021-08-09",
        "budget_crore": 130000,
        "incentive": "Positive indigenisation list — imports banned",
        "status": "Active",
        "impact_direction": "positive",
        "impact_magnitude": 5,
        "key_companies": ["Bharat Electronics", "HAL", "Solar Industries", "Paras Defence", "Data Patterns"],
        "technologies_affected": ["Missiles", "Radars", "Fighter jets (Tejas)", "Submarines", "Ammunition"],
        "layman_impact": "Govt banned imports of 411+ defence items — Army MUST buy them locally. BEL (radars), HAL (helicopters), Solar Industries (ammunition) have 5-10 year order visibility. A procurement mandate is the strongest possible policy signal.",
    },
]


@app.get("/api/india/pli-policies")
def get_pli_policies(
    year: int | None = None,
    sector: str | None = None,
) -> list[dict]:
    """Return India PLI schemes and Government policies.

    Primary source: embedded _INDIA_PLI_SCHEMES (authoritative, structured data
    from Ministry of Commerce / DPIIT announcements).
    Secondary: any matching events from mg_policy_events DB.
    """
    try:
        # Filter embedded PLI schemes
        schemes = _INDIA_PLI_SCHEMES
        if year:
            # Show schemes announced IN this year ("new/changed this year")
            # PLUS all schemes active in prior years that are still running (status=Active)
            # This way: 2020 shows only 2020 launches; 2024 shows 2024 launches AND
            # all still-active schemes from prior years.
            # Toggle via `sector="new_only"` to see only what launched this year.
            schemes = [
                s for s in schemes
                if s.get("year", 0) == year  # launched exactly this year
                or (s.get("year", 0) < year and s.get("status", "Active") == "Active")
            ]
        if sector == "new_only" and year:
            schemes = [s for s in schemes if s.get("year", 0) == year]
        elif sector and sector != "All":
            schemes = [s for s in schemes if sector.lower() in s.get("sector", "").lower()]

        # Group by sector
        sector_map: dict[str, list[dict]] = {}
        for s in schemes:
            sec = s.get("sector", "General")
            sector_map.setdefault(sec, []).append({
                "policy_id":            s["policy_id"],
                "title":                s["title"],
                "policy_type":          s["policy_type"],
                "status":               s.get("status", "Active"),
                "enacted_date":         s.get("enacted_date", ""),
                "introduced_date":      s.get("enacted_date", ""),
                "budget_crore":         s.get("budget_crore", 0),
                "incentive":            s.get("incentive", ""),
                "impact_direction":     s.get("impact_direction", "positive"),
                "impact_magnitude":     s.get("impact_magnitude", 3),
                "sectors_affected":     [s.get("sector", "")],
                "technologies_affected":s.get("technologies_affected", []),
                "key_companies":        s.get("key_companies", []),
                "layman_impact":        s.get("layman_impact", ""),
            })

        result = []
        for sec, policies in sorted(sector_map.items()):
            policies_sorted = sorted(policies, key=lambda x: str(x.get("enacted_date") or ""), reverse=True)
            result.append({
                "sector":        sec,
                "policy_count":  len(policies_sorted),
                "policies":      policies_sorted,
            })

        return result
    except Exception as e:
        logging.error("get_pli_policies: %s", e)
        return []


class ThemeResearchBody(BaseModel):
    theme_slugs: list[str]
    from_date: str
    to_date: str
    country: str = "IN"
    top_n: int = 30


@app.post("/api/themes/company-research")
def theme_company_research(body: ThemeResearchBody) -> list[dict]:
    """Research companies for given theme slugs.
    
    Reliable 3-step approach:
    1. Resolve themes from slugs (with name fallback)
    2. Get beneficiary companies from mg_theme_beneficiaries
    3. Join signals via ticker OR company name (two separate queries merged)
    """
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")
    try:
        from psycopg2.extras import RealDictCursor
        from collections import defaultdict
        from_d = date.fromisoformat(body.from_date)
        to_d   = date.fromisoformat(body.to_date)

        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:

                # 1. Resolve theme IDs
                cur.execute(
                    "SELECT id, theme_name, theme_slug, conviction "
                    "FROM mg_themes WHERE theme_slug = ANY(%s) AND is_active = TRUE",
                    (body.theme_slugs,)
                )
                rows = list(cur.fetchall())
                if not rows and body.theme_slugs:
                    frags = [s.replace("-"," ").replace("_"," ")
                             for s in body.theme_slugs[:15]]
                    ph = " OR ".join(["LOWER(theme_name) LIKE %s"] * len(frags))
                    cur.execute(
                        f"SELECT id, theme_name, theme_slug, conviction "
                        f"FROM mg_themes WHERE is_active = TRUE AND ({ph}) LIMIT 40",
                        [f"%{f}%" for f in frags]
                    )
                    rows = list(cur.fetchall())

                themes = {r["id"]: dict(r) for r in rows}
                theme_ids = list(themes.keys())
                logging.info("theme_research: %d themes", len(themes))
                if not theme_ids:
                    return []

                # 2. Get beneficiary companies
                cur.execute(
                    """SELECT tb.theme_id, tb.company_name, tb.ticker,
                              tb.company_role, tb.relevance_score
                       FROM mg_theme_beneficiaries tb
                       WHERE tb.theme_id = ANY(%s)
                         AND tb.company_name IS NOT NULL AND tb.company_name != ''
                       ORDER BY tb.relevance_score DESC""",
                    (theme_ids,)
                )
                bene_rows = list(cur.fetchall())
                logging.info("theme_research: %d beneficiary rows", len(bene_rows))

                co_meta: dict[str, dict] = {}
                co_themes: dict[str, set] = defaultdict(set)
                tickers: list[str] = []

                for r in bene_rows:
                    name = r["company_name"]
                    co_themes[name].add(r["theme_id"])
                    if name not in co_meta or float(r["relevance_score"] or 0) > co_meta[name].get("relevance_score", 0):
                        co_meta[name] = {
                            "company_name":   name,
                            "ticker":         r["ticker"] or "",
                            "company_role":   r["company_role"] or "beneficiary",
                            "relevance_score": float(r["relevance_score"] or 0),
                        }
                    if r["ticker"]:
                        tickers.append(r["ticker"].upper().strip())

                if not co_meta:
                    logging.warning("theme_research: 0 companies in beneficiary table for these themes")
                    return []

                company_names = list(co_meta.keys())
                tickers = list(set(t for t in tickers if t))

                # 3. Pull signals — try TWO matching strategies and merge results
                SIGNAL_TYPES = (
                    "supply_bottleneck", "inventory_drawdown",
                    "capacity_shortage", "demand_exceeds_supply",
                    "demand_surge", "capex_increase", "technology_adoption"
                )

                raw_signals: list[dict] = []

                # Strategy A: match by ticker (most reliable when ticker is present)
                if tickers:
                    cur.execute(
                        """SELECT d.company, d.ticker,
                                  d.filed_at::date AS filed_date, d.filing_type,
                                  s.signal_type, s.confidence, s.context_text
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE d.filed_at BETWEEN %s AND %s
                             AND d.country = %s
                             AND UPPER(TRIM(d.ticker)) = ANY(%s)
                             AND s.signal_type = ANY(%s)
                           ORDER BY s.confidence DESC LIMIT 3000""",
                        (from_d, to_d, body.country, tickers, list(SIGNAL_TYPES))
                    )
                    raw_signals = list(cur.fetchall())
                    logging.info("theme_research: %d signals via ticker match", len(raw_signals))

                # Strategy B: match by exact company name (if ticker match gave few results)
                if len(raw_signals) < 20:
                    cur.execute(
                        """SELECT d.company, d.ticker,
                                  d.filed_at::date AS filed_date, d.filing_type,
                                  s.signal_type, s.confidence, s.context_text
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE d.filed_at BETWEEN %s AND %s
                             AND d.country = %s
                             AND d.company = ANY(%s)
                             AND s.signal_type = ANY(%s)
                           ORDER BY s.confidence DESC LIMIT 3000""",
                        (from_d, to_d, body.country, company_names, list(SIGNAL_TYPES))
                    )
                    name_sigs = list(cur.fetchall())
                    logging.info("theme_research: %d signals via name match", len(name_sigs))
                    # Merge, dedup by (company, filed_date, signal_type, context first 50 chars)
                    seen = {(r["company"], str(r["filed_date"]), r["signal_type"]) for r in raw_signals}
                    for r in name_sigs:
                        key = (r["company"], str(r["filed_date"]), r["signal_type"])
                        if key not in seen:
                            raw_signals.append(r)
                            seen.add(key)

                logging.info("theme_research: %d total signals after merge", len(raw_signals))

        # 4. Aggregate per company
        CONSTRAINT = {"supply_bottleneck","inventory_drawdown","capacity_shortage","demand_exceeds_supply"}
        co_sigs: dict[str, dict] = defaultdict(lambda: {"c":[],"d":[]})
        
        # Build lookup: ticker → canonical name
        ticker_to_name: dict[str, str] = {}
        for name, meta in co_meta.items():
            tk = meta.get("ticker","").upper().strip()
            if tk:
                ticker_to_name[tk] = name

        for sig in raw_signals:
            co_doc = (sig.get("company") or "").strip()
            doc_ticker = (sig.get("ticker") or "").upper().strip()
            # Resolve to canonical beneficiary name
            matched = (
                ticker_to_name.get(doc_ticker)
                or co_doc
                or None
            )
            if not matched:
                continue
            bucket = "c" if sig["signal_type"] in CONSTRAINT else "d"
            co_sigs[matched][bucket].append(sig)

        # 5. Build response
        results = []
        for name, meta in co_meta.items():
            sigs = co_sigs.get(name, {"c":[],"d":[]})
            c_sigs, d_sigs = sigs["c"], sigs["d"]

            seen_texts: set[str] = set()
            top_quotes = []
            for sig in sorted(c_sigs + d_sigs, key=lambda x: -float(x.get("confidence") or 0)):
                text = (sig.get("context_text") or "").strip()
                if text and text not in seen_texts and len(top_quotes) < 3:
                    seen_texts.add(text)
                    top_quotes.append({
                        "text":        text[:350],
                        "signal_type": sig["signal_type"],
                        "filed_date":  str(sig.get("filed_date") or ""),
                        "confidence":  round(float(sig.get("confidence") or 0), 2),
                    })

            theme_names = [themes[tid]["theme_name"] for tid in co_themes.get(name,set()) if tid in themes]
            results.append({
                "company":          name,
                "ticker":           meta.get("ticker") or "",
                "company_role":     meta.get("company_role") or "",
                "relevance_score":  round(meta.get("relevance_score") or 0, 1),
                "themes":           list(set(theme_names)),
                "constraint_count": len(c_sigs),
                "demand_count":     len(d_sigs),
                "total_signals":    len(c_sigs) + len(d_sigs),
                "dc_ratio":         round(len(d_sigs)/len(c_sigs),2) if c_sigs else None,
                "top_quotes":       top_quotes,
                "has_constraint_evidence": len(c_sigs) > 0,
            })

        results.sort(key=lambda r: (-r["constraint_count"], -r["total_signals"], -r["relevance_score"]))
        return results[:body.top_n]

    except Exception as e:
        logging.error("theme_company_research: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))




@app.get("/api/india/chain-beneficiaries")
def get_india_chain_beneficiaries(
    as_of: str = None,
    min_conviction: float = 0.70,
) -> list[dict]:
    """Return India supply-chain beneficiaries from mg_india_beneficiaries.

    Used by ShortlistedTab and ThemesTab to show which companies benefit
    from each causal chain / capacity gap theme.
    """
    pg = get_pg()
    if not pg:
        return []
    try:
        _as_of = date.fromisoformat(as_of) if as_of else date.today()
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT company, ticker, theme_name, constrained_product,
                              supply_chain_node, supply_chain_stage, beneficiary_type,
                              conviction_score, rationale, signal_count,
                              has_order_book_signals, import_substitution_play
                       FROM mg_india_beneficiaries
                       WHERE conviction_score >= %s
                         AND (as_of_date IS NULL OR as_of_date <= %s)
                       ORDER BY conviction_score DESC, theme_name""",
                    (min_conviction, _as_of),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logging.debug("get_india_chain_beneficiaries: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# CONTRADICTIONS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/contradictions")
def get_contradictions(country: str = "US") -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT company, theme, from_quarter, to_quarter,
                              change_type, from_sentiment, to_sentiment,
                              evidence, detected_at
                       FROM mg_contradictions WHERE country=%s
                       ORDER BY detected_at DESC LIMIT 40""",
                    (country,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logging.error("get_contradictions: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# REPLAY HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/replay-history")
def get_replay_history() -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT replay_batch, replay_date, docs_ingested, docs_nlp,
                              themes_detected, themes_snapped, causal_score,
                              duration_sec, status
                       FROM mg_replay_runs ORDER BY replay_date DESC LIMIT 60"""
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logging.error("get_replay_history: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# FILINGS / CONCALL
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/filings")
def get_filings(
    country: str = "US",
    from_date: str | None = None,
    to_date: str | None = None,
    ticker: str | None = None,
    filing_type: str = "All",
    limit: int = 100,
) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_concall_documents(
            country=country,
            from_date=date.fromisoformat(from_date) if from_date else None,
            to_date=date.fromisoformat(to_date) if to_date else None,
            ticker_search=ticker or None,
            filing_type_filter=filing_type,
            limit=limit,
        )
    except Exception as e:
        logging.error("get_filings: %s", e)
        return []


@app.get("/api/filings/{doc_id}/signals")
def get_doc_signals(doc_id: int, limit: int = 60) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_document_signals(doc_id, limit=limit)
    except Exception as e:
        logging.error("get_doc_signals: %s", e)
        return []


@app.get("/api/filings/{doc_id}/themes")
def get_doc_themes(doc_id: int) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_document_theme_contributions(doc_id)
    except Exception as e:
        logging.error("get_doc_themes: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# COMPANY EXPLORER
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/company/search")
def search_companies(q: str = "", country: str = "US") -> list[dict]:
    pg = get_pg()
    if not pg or not q:
        return []
    try:
        return pg.search_companies(q, country=country, limit=20)
    except Exception as e:
        logging.error("search_companies: %s", e)
        return []


@app.get("/api/company/{ticker}/profile")
def get_company_profile(ticker: str, country: str = "US", as_of: str | None = None) -> dict:
    pg = get_pg()
    if not pg:
        return {}
    try:
        return pg.get_company_profile(
            ticker, country=country,
            as_of_date=date.fromisoformat(as_of) if as_of else date.today(),
        )
    except Exception as e:
        logging.error("get_company_profile: %s", e)
        return {}


@app.get("/api/company/{ticker}/timeline")
def get_company_timeline(
    ticker: str, country: str = "US",
    from_date: str | None = None, to_date: str | None = None,
) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_company_signal_timeline(
            ticker, country=country,
            from_date=date.fromisoformat(from_date) if from_date else date(2020, 1, 1),
            to_date=date.fromisoformat(to_date) if to_date else date.today(),
        )
    except Exception as e:
        logging.error("get_company_timeline: %s", e)
        return []


@app.get("/api/company/{ticker}/themes")
def get_company_themes(ticker: str, country: str = "US", as_of: str | None = None) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_company_themes(
            ticker, country=country,
            as_of_date=date.fromisoformat(as_of) if as_of else date.today(),
        )
    except Exception as e:
        logging.error("get_company_themes: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# COMPANY DEEP DIVE
# ═══════════════════════════════════════════════════════════════════════════════

class CompanyDiveBody(BaseModel):
    company:       str            # name or ticker
    country:       str  = "IN"
    year:          int  | None = None
    force_refresh: bool = False


@app.get("/api/company/sentiment-board")
def get_investable_signals(
    country: str = "IN",
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    min_quarters: int = 3,      # consecutive quarters required (default 3, not 2)
    min_sentiment: float = 7.0, # minimum per-quarter sentiment (default 7.0, not 6.0)
    top_n: int = 30,            # return only top N by signal strength
) -> list[dict]:
    """Investable signal = confirmed 2-quarter positive sentiment (hard gate)
    ranked by capex + constraint signals + theme membership (boosters, not gates).

    Gate 1 (HARD): both last 2 quarters must have NLP sentiment >= 6.0.
    Boosters: capex signals, constraint signals, theme membership.
    Companies with all three rise to the top. Companies with only sentiment
    but no capex/constraint appear at the bottom — user can decide.
    """
    pg = get_pg()
    if not pg:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        if from_date and to_date:
            from_d = date.fromisoformat(from_date)
            to_d   = date.fromisoformat(to_date)
        elif year:
            from_d = date(year, 1, 1)
            to_d   = date(year, 12, 31) if year < date.today().year else date.today()
        else:
            from_d = date(date.today().year - 1, 1, 1)
            to_d   = date.today()

        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    WITH

                    -- Resolve signal direction (stored OR inferred from signal_type)
                    sig_dir AS (
                        SELECT s.document_id, s.signal_type,
                               COALESCE(s.confidence, 0.75) AS conf,
                               CASE
                                   WHEN COALESCE(s.direction,'') = 'positive' THEN 1
                                   WHEN COALESCE(s.direction,'') = 'negative' THEN -1
                                   WHEN s.signal_type IN (
                                       'demand_surge','capex_increase','technology_adoption',
                                       'hiring_surge','market_entry','regulatory_tailwind',
                                       'tender_pipeline','policy_support'
                                   ) THEN 1
                                   WHEN s.signal_type IN (
                                       'supply_bottleneck','inventory_drawdown','capacity_shortage',
                                       'demand_exceeds_supply','demand_slowdown','hiring_freeze',
                                       'regulatory_headwind','capex_decrease'
                                   ) THEN -1
                                   ELSE 0
                               END AS dir_val
                        FROM mg_signals s
                    ),

                    -- Per-filing: sentiment + evidence counts
                    filing_sent AS (
                        SELECT
                            d.id   AS doc_id,
                            d.company,
                            COALESCE(NULLIF(d.ticker,''), d.company) AS ticker,
                            d.filed_at::date  AS filing_date,
                            ROUND(GREATEST(1.0, LEAST(10.0,
                                CASE WHEN SUM(sd.conf) = 0 THEN 5.5
                                ELSE 5.5 + SUM(sd.dir_val::float * sd.conf)
                                          / SUM(sd.conf) * 4.5
                                END
                            ))::numeric, 1)  AS filing_sentiment,
                            COUNT(*) FILTER (WHERE sd.signal_type = 'capex_increase') AS capex_sigs,
                            COUNT(*) FILTER (WHERE sd.signal_type IN (
                                'supply_bottleneck','inventory_drawdown',
                                'capacity_shortage','demand_exceeds_supply'
                            ))  AS constraint_sigs,
                            COUNT(*) AS total_sigs
                        FROM mg_documents d
                        JOIN sig_dir sd ON sd.document_id = d.id
                        WHERE d.country = %s
                          AND d.filed_at BETWEEN %s AND %s
                          AND d.company IS NOT NULL AND d.company != ''
                        GROUP BY d.id, d.company, d.ticker, d.filed_at
                        HAVING COUNT(*) >= 3
                    ),

                    -- Quarter-level aggregation
                    quarterly AS (
                        SELECT company, ticker,
                               DATE_TRUNC('quarter', filing_date)::date AS quarter,
                               ROUND(AVG(filing_sentiment)::numeric,1) AS q_sent,
                               SUM(capex_sigs)       AS q_capex,
                               SUM(constraint_sigs)  AS q_cstr
                        FROM filing_sent
                        GROUP BY company, ticker, DATE_TRUNC('quarter', filing_date)
                    ),

                    -- Rank quarters newest-first
                    q_ranked AS (
                        SELECT *,
                            ROW_NUMBER() OVER (PARTITION BY company ORDER BY quarter DESC) AS rn
                        FROM quarterly
                    ),

                    -- GATE 1 (HARD): min_quarters consecutive recent quarters all >= min_sentiment
                    -- Default: 3 quarters all >= 7.0 (much stricter than 2Q >= 6.0)
                    confirmed AS (
                        SELECT
                            company, ticker,
                            MAX(CASE WHEN rn=1 THEN q_sent END) AS q1,
                            MAX(CASE WHEN rn=2 THEN q_sent END) AS q2,
                            MAX(CASE WHEN rn=3 THEN q_sent END) AS q3,
                            SUM(q_capex)  AS total_capex,
                            SUM(q_cstr)   AS total_cstr,
                            COUNT(*)      AS quarters_in_period
                        FROM q_ranked
                        GROUP BY company, ticker
                        -- ALL of the last min_quarters quarters must be >= min_sentiment
                        HAVING COUNT(*) FILTER (WHERE rn <= %s AND q_sent >= %s) >= %s
                    ),

                    -- Period totals (for filing count, last filing)
                    period AS (
                        SELECT company,
                               MAX(filing_date) AS last_filing,
                               COUNT(DISTINCT doc_id) AS filing_count,
                               SUM(capex_sigs)      AS period_capex,
                               SUM(constraint_sigs) AS period_cstr
                        FROM filing_sent
                        GROUP BY company
                    ),

                    -- Theme membership (LEFT join — booster not gate, uses ILIKE)
                    theme_info AS (
                        SELECT
                            tb.company_name,
                            tb.ticker AS theme_ticker,
                            COUNT(DISTINCT t.id) AS theme_count,
                            MAX(CASE WHEN t.conviction IN ('confirmed','high') THEN 1 ELSE 0 END) AS has_confirmed,
                            STRING_AGG(DISTINCT t.theme_name, '; '
                                ORDER BY t.theme_name) AS theme_names
                        FROM mg_theme_beneficiaries tb
                        JOIN mg_themes t ON t.id = tb.theme_id
                        WHERE t.is_active = TRUE AND t.country = %s
                          AND tb.company_name IS NOT NULL
                        GROUP BY tb.company_name, tb.ticker
                    )

                    SELECT
                        c.ticker,
                        c.company,
                        ROUND(((c.q1 + c.q2) / 2.0)::numeric, 1) AS sentiment_score,
                        c.q1   AS last_quarter_sentiment,
                        c.q2   AS prior_quarter_sentiment,
                        c.quarters_in_period,
                        p.period_capex    AS capex_signals,
                        p.period_cstr     AS constraint_signals,
                        p.filing_count,
                        p.last_filing,
                        COALESCE(ti.theme_count, 0)   AS theme_count,
                        COALESCE(ti.has_confirmed, 0) AS in_confirmed_theme,
                        COALESCE(ti.theme_names, '')  AS constraint_themes,
                        -- Signal strength: pure sentiment + capex booster + constraint booster + theme booster
                        ROUND((
                            ((c.q1 + c.q2) / 2.0 - 6.0) * 15.0          -- sentiment above threshold
                          + LEAST(40.0, p.period_capex    * 8.0)          -- capex commitment booster
                          + LEAST(30.0, p.period_cstr     * 5.0)          -- constraint evidence booster
                          + LEAST(15.0, COALESCE(ti.theme_count, 0) * 5.0) -- theme membership booster
                        )::numeric, 1) AS signal_strength,
                        -- Labels for the three signals
                        CASE WHEN p.period_capex > 0 THEN true ELSE false END AS has_capex,
                        CASE WHEN p.period_cstr  > 0 THEN true ELSE false END AS has_constraint,
                        CASE WHEN COALESCE(ti.theme_count, 0) > 0 THEN true ELSE false END AS in_theme
                    FROM confirmed c
                    JOIN period p ON p.company = c.company
                    LEFT JOIN theme_info ti
                           ON LOWER(TRIM(c.company)) ILIKE LOWER(TRIM(ti.company_name))
                           OR (c.ticker != '' AND UPPER(TRIM(c.ticker)) = UPPER(TRIM(ti.theme_ticker)))
                    ORDER BY signal_strength DESC
                    LIMIT %s
                """, (country, from_d, to_d,
                      min_quarters, min_sentiment, min_quarters,  # HAVING clause params
                      country,                                     # theme_info WHERE
                      top_n))
                rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logging.error("get_investable_signals: %s", e, exc_info=True)
        return []


@app.get("/api/company/investable-signals")
def investable_signals_endpoint(
    country: str = "IN",
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    min_quarters: int = 3,
    min_sentiment: float = 7.0,
    top_n: int = 30,
) -> list[dict]:
    return get_investable_signals(country=country, year=year,
                                  from_date=from_date, to_date=to_date,
                                  min_quarters=min_quarters,
                                  min_sentiment=min_sentiment,
                                  top_n=top_n)


def get_sentiment_board(
    country: str = "IN",
    year: int | None = None,
    from_date: str | None = None,   # YYYY-MM-DD overrides year
    to_date: str | None = None,     # YYYY-MM-DD overrides year
    min_filings: int = 1,
    min_signals: int = 3,      # per filing minimum signals
    min_directional: int = 2,  # must have ≥2 positive OR negative signals to matter
    limit: int = 500,
) -> list[dict]:
    """Sentiment board for all companies — computed purely from NLP/spaCy signals,
    no Claude API call needed.

    Sentiment formula (1-10):
        base = 5.0
        + 0.40 per demand/positive signal (cap +4.0)
        − 0.35 per constraint/negative signal (cap −3.5)
        clamped to [1, 10]

    Positive signal types: demand_surge, capex_increase, technology_adoption,
                           regulatory_tailwind, market_entry, hiring_surge
    Negative signal types: supply_bottleneck, inventory_drawdown, capacity_shortage,
                           demand_exceeds_supply, demand_slowdown, hiring_freeze
    """
    pg = get_pg()
    if not pg:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        # Date logic — strict year isolation so each year shows different companies:
        # - Year selected: ONLY filings from that calendar year (Jan 1 – Dec 31).
        #   This ensures 2021 shows 2021 filings, 2022 shows 2022 filings, etc.
        # - Explicit from/to dates: exact window
        # - "All time": last 3 years
        if from_date and to_date:
            from_d = date.fromisoformat(from_date)
            to_d   = date.fromisoformat(to_date)
        elif year:
            from_d = date(year, 1, 1)
            to_d   = date(year, 12, 31) if year < date.today().year else date.today()
        else:
            from_d = date(date.today().year - 2, 1, 1)
            to_d   = date.today()

        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    WITH

                    -- Step 1: Score every individual filing in the period
                    -- Resolve each signal's direction:
                    -- prefer stored direction, fall back to signal_type mapping
                    sig_with_dir AS (
                        -- Scoped to the date window for performance
                        SELECT
                            s.document_id,
                            s.signal_type,
                            COALESCE(s.confidence, 0.75) AS conf,
                            CASE
                                WHEN COALESCE(s.direction,'') = 'positive' THEN 1
                                WHEN COALESCE(s.direction,'') = 'negative' THEN -1
                                WHEN s.signal_type IN (
                                    'demand_surge','capex_increase','technology_adoption',
                                    'hiring_surge','market_entry','regulatory_tailwind',
                                    'tender_pipeline','policy_support','localization_opportunity'
                                ) THEN 1
                                WHEN s.signal_type IN (
                                    'supply_bottleneck','inventory_drawdown','capacity_shortage',
                                    'demand_exceeds_supply','demand_slowdown','hiring_freeze',
                                    'supply_easing','regulatory_headwind','capex_decrease',
                                    'technology_disruption'
                                ) THEN -1
                                ELSE 0
                            END AS dir_val
                        FROM mg_signals s
                        JOIN mg_documents d2 ON d2.id = s.document_id
                        WHERE d2.country = %s
                          AND d2.filed_at BETWEEN %s AND %s
                    ),

                    filing_scores AS (
                        SELECT
                            d.id                                              AS doc_id,
                            d.company,
                            COALESCE(NULLIF(d.ticker,''), d.company)          AS ticker,
                            d.filed_at::date                                  AS filing_date,
                            d.filing_type,
                            COUNT(*) FILTER (WHERE sd.dir_val  = 1)          AS pos,
                            COUNT(*) FILTER (WHERE sd.dir_val  = -1)         AS neg,
                            COUNT(*) FILTER (WHERE sd.signal_type IN (
                                'supply_bottleneck','inventory_drawdown',
                                'capacity_shortage','demand_exceeds_supply'
                            ))                                                AS cstr,
                            COUNT(*) FILTER (WHERE sd.signal_type = 'capex_increase')
                                                                              AS capex,
                            COUNT(*)                                          AS total,
                            COUNT(*) FILTER (WHERE sd.dir_val  = 1)          AS pos_conf,
                            COUNT(*) FILTER (WHERE sd.dir_val  = -1)         AS neg_conf,
                            -- Confidence-weighted sentiment score (1-10):
                            -- net = Σ(dir_val × confidence) for all signals
                            -- max_possible = Σ(confidence) when all positive
                            -- sentiment = 5.5 + net / max_possible × 4.5
                            -- → all positive  = 10.0
                            -- → all negative  = 1.0
                            -- → all neutral   = 5.5
                            ROUND(GREATEST(1.0, LEAST(10.0,
                                CASE
                                    WHEN SUM(sd.conf) = 0 THEN 5.5
                                    ELSE 5.5 + SUM(sd.dir_val::float * sd.conf)
                                              / SUM(sd.conf) * 4.5
                                END
                            ))::numeric, 1)                                   AS filing_sentiment,
                            (SELECT s2.signal_type FROM mg_signals s2
                             WHERE s2.document_id = d.id
                             GROUP BY s2.signal_type
                             ORDER BY COUNT(*) DESC LIMIT 1)                  AS dominant_signal
                        FROM mg_documents d
                        JOIN sig_with_dir sd ON sd.document_id = d.id
                        WHERE d.country = %s
                          AND d.filed_at BETWEEN %s AND %s
                          AND d.company IS NOT NULL AND d.company != ''
                        GROUP BY d.id, d.company, d.ticker, d.filed_at, d.filing_type
                        HAVING COUNT(*) >= %s
                    ),

                    -- Step 2: Pick the MOST RECENT filing per company
                    --         This is the "what they last said" sentiment
                    latest AS (
                        SELECT DISTINCT ON (company)
                            company, ticker, filing_date AS last_filing,
                            filing_type   AS last_filing_type,
                            filing_sentiment AS sentiment_score,
                            pos AS positive_sigs,
                            neg AS negative_sigs,
                            cstr AS constraint_sigs,
                            capex AS capex_sigs,
                            total AS total_sigs,
                            dominant_signal
                        FROM filing_scores
                        ORDER BY company, filing_date DESC
                    ),

                    -- Step 3: Quarter-level sentiment (group filings by quarter)
                    quarterly AS (
                        SELECT
                            company,
                            DATE_TRUNC('quarter', filing_date)::date     AS quarter,
                            ROUND(AVG(filing_sentiment)::numeric, 1)     AS q_sentiment,
                            SUM(pos)                                      AS q_pos,
                            SUM(neg)                                      AS q_neg
                        FROM filing_scores
                        GROUP BY company, DATE_TRUNC('quarter', filing_date)
                    ),

                    -- Step 4: Rank quarters per company (most recent first)
                    quarterly_ranked AS (
                        SELECT *,
                            ROW_NUMBER() OVER (PARTITION BY company ORDER BY quarter DESC) AS rn
                        FROM quarterly
                    ),

                    -- Step 5: Check if last 2 quarters confirm the same direction.
                    -- "Persistence" rule: a sentiment is only trusted when 2 consecutive
                    -- quarters show the same bullish (≥6.5) or bearish (≤4.5) direction.
                    persistence AS (
                        SELECT
                            company,
                            MAX(CASE WHEN rn=1 THEN q_sentiment END)  AS q1,   -- most recent
                            MAX(CASE WHEN rn=2 THEN q_sentiment END)  AS q2,   -- prior quarter
                            MAX(CASE WHEN rn=1 THEN quarter   END)    AS q1_date,
                            COUNT(*) FILTER (WHERE rn <= 4)           AS quarters_in_period,
                            ROUND(AVG(q_sentiment)::numeric, 1)       AS confirmed_avg,
                            -- Persistence classification
                            CASE
                                WHEN MAX(CASE WHEN rn=1 THEN q_sentiment END) >= 6.5
                                 AND MAX(CASE WHEN rn=2 THEN q_sentiment END) >= 6.5
                                    THEN 'confirmed_bullish'   -- ✅ 2 consecutive bullish quarters
                                WHEN MAX(CASE WHEN rn=1 THEN q_sentiment END) <= 4.5
                                 AND MAX(CASE WHEN rn=2 THEN q_sentiment END) <= 4.5
                                    THEN 'confirmed_bearish'   -- ✅ 2 consecutive bearish quarters
                                WHEN MAX(CASE WHEN rn=2 THEN q_sentiment END) IS NULL
                                 AND MAX(CASE WHEN rn=1 THEN q_sentiment END) >= 6.5
                                    THEN 'single_bullish'      -- only 1 quarter of data
                                WHEN MAX(CASE WHEN rn=2 THEN q_sentiment END) IS NULL
                                 AND MAX(CASE WHEN rn=1 THEN q_sentiment END) <= 4.5
                                    THEN 'single_bearish'
                                ELSE 'mixed'                   -- no consistent direction
                            END AS persistence_status
                        FROM quarterly_ranked
                        GROUP BY company
                    ),

                    -- Step 6: Aggregate period totals and trend
                    period_totals AS (
                        SELECT
                            company,
                            COUNT(DISTINCT doc_id)                                    AS filing_count,
                            MIN(filing_date)                                          AS first_filing,
                            SUM(pos)                                                  AS period_pos,
                            SUM(neg)                                                  AS period_neg,
                            SUM(cstr)                                                 AS period_cstr,
                            ROUND(AVG(filing_sentiment)::numeric, 1)                 AS period_avg_sentiment,
                            (ARRAY_AGG(filing_sentiment ORDER BY filing_date DESC))[1] AS last_sentiment,
                            (ARRAY_AGG(filing_sentiment ORDER BY filing_date ASC))[1]  AS first_sentiment
                        FROM filing_scores
                        GROUP BY company
                        HAVING COUNT(DISTINCT doc_id) >= %s
                           AND (SUM(pos) >= %s OR SUM(neg) >= %s)
                    ),

                    -- Step 7: Theme membership for cross-validation
                    theme_membership AS (
                        SELECT
                            LOWER(TRIM(tb.company_name))  AS co_lower,
                            tb.ticker                      AS theme_ticker,
                            COUNT(DISTINCT t.id)           AS theme_count,
                            MAX(CASE WHEN t.conviction IN ('confirmed','high') THEN 1 ELSE 0 END)
                                                           AS has_confirmed_theme,
                            STRING_AGG(DISTINCT t.theme_name, '; ' ORDER BY t.theme_name)
                                                           AS theme_names
                        FROM mg_theme_beneficiaries tb
                        JOIN mg_themes t ON t.id = tb.theme_id
                        WHERE t.is_active = TRUE
                          AND t.country = %s
                          AND tb.company_name IS NOT NULL
                        GROUP BY LOWER(TRIM(tb.company_name)), tb.ticker
                    )

                    SELECT
                        l.ticker,
                        l.company,
                        l.last_filing,
                        l.last_filing_type,
                        COALESCE(
                            CASE WHEN pr.persistence_status IN ('confirmed_bullish','confirmed_bearish')
                                 THEN pr.confirmed_avg END,
                            l.sentiment_score
                        )                                                             AS sentiment_score,
                        l.positive_sigs,
                        l.negative_sigs,
                        l.constraint_sigs,
                        l.capex_sigs,
                        l.total_sigs,
                        l.dominant_signal,
                        p.filing_count,
                        p.first_filing,
                        p.period_pos,
                        p.period_cstr,
                        p.period_avg_sentiment,
                        COALESCE(pr.persistence_status, 'single_bullish')            AS persistence_status,
                        COALESCE(pr.quarters_in_period, 1)                           AS quarters_in_period,
                        CASE
                            WHEN p.filing_count < 2 THEN 'stable'
                            WHEN p.last_sentiment - p.first_sentiment >  0.5 THEN 'improving'
                            WHEN p.last_sentiment - p.first_sentiment < -0.5 THEN 'declining'
                            ELSE 'stable'
                        END                                                           AS sentiment_trend,
                        -- Theme membership (cross-validation with constraint intelligence)
                        COALESCE(tm.theme_count, 0)         AS theme_count,
                        COALESCE(tm.has_confirmed_theme, 0) AS in_confirmed_theme,
                        COALESCE(tm.theme_names, '')        AS theme_names,
                        -- COMPOUND INVESTABILITY SCORE (0-100):
                        -- Combines NLP sentiment + constraint evidence + theme validation.
                        -- Only the intersection of all three produces a high score.
                        ROUND((
                            -- Sentiment quality (40 pts): confirmed 2Q bullish scores highest
                            (CASE
                                WHEN pr.persistence_status = 'confirmed_bullish'
                                  AND COALESCE(pr.confirmed_avg, l.sentiment_score) >= 7.0
                                THEN 40.0
                                WHEN pr.persistence_status IN ('single_bullish','confirmed_bullish')
                                  AND COALESCE(pr.confirmed_avg, l.sentiment_score) >= 7.0
                                THEN 25.0
                                WHEN COALESCE(pr.confirmed_avg, l.sentiment_score) >= 6.0
                                THEN 10.0
                                ELSE 0.0
                            END)
                            -- Constraint evidence (30 pts): company explicitly mentioned constraints
                            + LEAST(30.0, l.constraint_sigs * 5.0)
                            -- Capex commitment (15 pts): company investing to solve constraint
                            + LEAST(15.0, l.capex_sigs * 5.0)
                            -- Theme validation (15 pts): in confirmed constraint theme
                            + COALESCE(LEAST(15.0, tm.theme_count * 5.0) * tm.has_confirmed_theme, 0)
                        )::numeric, 1)                                                AS invest_score
                    FROM latest l
                    JOIN period_totals p  ON p.company  = l.company
                    JOIN persistence   pr ON pr.company = l.company
                    LEFT JOIN theme_membership tm
                           ON LOWER(TRIM(l.company)) = tm.co_lower
                           OR UPPER(TRIM(l.ticker))  = UPPER(TRIM(tm.theme_ticker))
                    ORDER BY invest_score DESC, l.company ASC
                    LIMIT %s
                    """,
                    (country, from_d, to_d,            # sig_with_dir scoped WHERE
                     country, from_d, to_d,            # filing_scores WHERE
                     min_signals,                       # filing_scores HAVING per-filing
                     min_filings,                       # period_totals HAVING filing count
                     min_directional, min_directional,  # period_totals HAVING directional
                     country,                           # theme_membership WHERE country
                     limit)
                )
                rows = cur.fetchall()

        return [dict(r) for r in rows]
    except Exception as e:
        logging.error("sentiment_board: %s", e)
        return []


@app.get("/api/company/all-analysed")
def get_all_analysed_companies(country: str = "IN") -> list[dict]:
    """Return all previously analysed companies with conviction + sentiment summary."""
    pg = get_pg()
    if not pg:
        return []
    try:
        import json as _j
        from psycopg2.extras import RealDictCursor
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT country, year, summary_text, industry_insights, generated_at
                       FROM mg_ai_summaries
                       WHERE context_type = 'company_deep_dive'
                         AND country = %s
                       ORDER BY generated_at DESC""",
                    (country,)
                )
                rows = cur.fetchall()

        results = []
        for row in rows:
            yr_key  = str(row["year"] or "")          # e.g. "RELIANCE_2024"
            parts   = yr_key.rsplit("_", 1)
            ticker  = parts[0] if len(parts) == 2 else yr_key
            year_n  = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None

            brief: dict = {}
            raw = row["industry_insights"]
            if raw:
                # JSONB comes back as dict from psycopg2, not a string
                if isinstance(raw, dict):
                    brief = raw
                elif isinstance(raw, str):
                    try: brief = _j.loads(raw)
                    except: brief = {}

            rec         = brief.get("recommendation") or {}
            action      = str(rec.get("action","") or "")
            conviction  = str(rec.get("conviction","") or "")

            # Overall sentiment: average of concall timeline scores if available
            timeline    = brief.get("concall_timeline") or []
            if timeline and isinstance(timeline, list):
                scores = [float(c.get("sentiment_score",5)) for c in timeline if c.get("sentiment_score")]
                avg_sentiment = round(sum(scores)/len(scores), 1) if scores else None
            else:
                avg_sentiment = None

            # ticker from key OR from saved brief field
            resolved_ticker  = brief.get("ticker") or ticker
            resolved_company = brief.get("company") or ticker
            results.append({
                "ticker":          resolved_ticker,
                "year":            year_n,
                "company":         resolved_company,
                "db_key":          yr_key,   # exact DB key for direct lookup
                "action":          action,
                "conviction":      conviction,
                "avg_sentiment":   avg_sentiment,
                "investment_thesis": (brief.get("investment_thesis") or "")[:150],
                "best_quote":      (brief.get("best_quote") or "")[:120],
                "generated_at":    str(row["generated_at"] or ""),
                "pli_matches":     len(brief.get("pli_matches") or []),
            })

        return results
    except Exception as e:
        logging.error("get_all_analysed_companies: %s", e)
        return []


@app.get("/api/company/deep-dive")
def get_saved_company_dive(company: str, country: str = "IN", year: int | None = None) -> dict:
    """Return previously saved company deep-dive analysis, or {} if not generated.
    Key format: TICKER_YEAR (e.g. RELIANCE_2024). Searches by ticker and name."""
    pg = get_pg()
    if not pg:
        return {}
    try:
        yr       = str(year or date.today().year)
        co_clean = company.upper().strip().replace(' ','_')
        key_exact = f"{co_clean}_{yr}"
        from psycopg2.extras import RealDictCursor
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Exact key match first, then LIKE to handle partial ticker matches
                cur.execute(
                    """SELECT summary_text, industry_insights, generated_at, year
                       FROM mg_ai_summaries
                       WHERE country=%s
                         AND context_type='company_deep_dive'
                         AND (year = %s OR year LIKE %s)
                       ORDER BY
                         CASE WHEN year = %s THEN 0 ELSE 1 END,
                         generated_at DESC
                       LIMIT 1""",
                    (country, key_exact, f"%_{yr}", key_exact)
                )
                row = cur.fetchone()
                if not row:
                    return {}
                import json as _j
                # JSONB comes back as dict from psycopg2, str needs parsing
                saved = row["industry_insights"] or {}
                if isinstance(saved, str):
                    try: saved = _j.loads(saved)
                    except: saved = {}

                # Reconstruct the full result shape the frontend expects
                # The brief fields (executive_summary, recommendation, etc.) are at top level
                # The structural fields (themes, peers, etc.) are also at top level
                brief_fields = {
                    k: v for k, v in saved.items()
                    if k not in ("themes","peers","pli_matches","budget_support",
                                 "concall_timeline","recent_filings","data_summary",
                                 "ticker","company")
                }
                return {
                    "company":         saved.get("company", company),
                    "ticker":          saved.get("ticker", co_clean),
                    "country":         country,
                    "year":            int(yr),
                    "brief":           brief_fields,
                    "themes":          saved.get("themes", []),
                    "peers":           saved.get("peers", []),
                    "pli_matches":     saved.get("pli_matches", []),
                    "budget_support":  saved.get("budget_support", []),
                    "concall_timeline":saved.get("concall_timeline", []),
                    "recent_filings":  saved.get("recent_filings", []),
                    "data_summary":    saved.get("data_summary", {}),
                    "generated_at":    str(row["generated_at"]),
                    "from_cache":      True,
                }
    except Exception as e:
        logging.error("get_saved_company_dive: %s", e)
        return {}


@app.post("/api/company/deep-dive")
def run_company_deep_dive(body: CompanyDiveBody) -> dict:
    """Company-level deep dive: fetch 2-3 years of concalls + signals, call Claude.

    Steps:
    1. Search company in mg_documents by name/ticker
    2. Pull filings + signals for last 3 years (year-2 to year)
    3. Pull themes this company appears in
    4. Find industry peers (same themes)
    5. For India: if data sparse, trigger fresh fetch
    6. Call Claude for investment analysis
    7. Cache result in mg_ai_summaries
    """
    pg = get_pg()
    acfg = CFG.get("anthropic", {})
    if not acfg.get("api_key"):
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")

    try:
        import json as _json
        from psycopg2.extras import RealDictCursor
        from collections import defaultdict

        yr      = body.year or date.today().year
        to_d    = date(yr, 12, 31) if yr < date.today().year else date.today()
        from_d  = date(yr - 2, 1, 1)   # 3 years of data
        market  = "India (NSE/BSE)" if body.country == "IN" else "USA (NYSE/NASDAQ)"

        # ── 1. Find company in DB (search by name/ticker) ─────────────────────
        found_ticker = body.company.upper().strip()
        found_name   = body.company
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Try ticker first
                cur.execute(
                    """SELECT DISTINCT ticker, company FROM mg_documents
                       WHERE country=%s AND (
                           UPPER(ticker)=%s OR LOWER(company) ILIKE %s
                       )
                       ORDER BY company LIMIT 1""",
                    (body.country, found_ticker, f"%{body.company.lower()}%")
                )
                row = cur.fetchone()
                if row:
                    found_ticker = row["ticker"] or found_ticker
                    found_name   = row["company"] or found_name

        # Cache key — always use resolved ticker for stable lookup
        co_key = f"{found_ticker.upper().replace(' ','_')}_{yr}"

        # ── 2. Pull filings (documents) for last 3 years ──────────────────────
        filings: list[dict] = []
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, title, filing_type, filed_at, url, ticker, company
                       FROM mg_documents
                       WHERE country=%s
                         AND filed_at BETWEEN %s AND %s
                         AND (UPPER(ticker)=%s OR LOWER(company) ILIKE %s)
                       ORDER BY filed_at DESC
                       LIMIT 100""",
                    (body.country, from_d, to_d,
                     found_ticker, f"%{body.company.lower()}%")
                )
                filings = [dict(r) for r in cur.fetchall()]

        # India: sparse data → trigger fresh fetch from NSE/BSE
        if body.country == "IN" and len(filings) < 5 and not body.force_refresh:
            logging.info("company_dive: sparse India data (%d docs) — triggering fresh fetch", len(filings))
            try:
                from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
                _cfg = copy.deepcopy(CFG)
                _cfg.setdefault("screener", {})["enabled"] = True
                _cfg.setdefault("screener", {})["ticker_list"] = [found_ticker]
                with IntelligencePipeline(_cfg) as pip:
                    pip._init_storage()
                    pip.run_ingest_india(since=from_d, until=to_d)
                # Re-query after fetch
                with pg._conn() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(
                            """SELECT id, title, filing_type, filed_at, url, ticker, company
                               FROM mg_documents
                               WHERE country=%s AND filed_at BETWEEN %s AND %s
                                 AND (UPPER(ticker)=%s OR LOWER(company) ILIKE %s)
                               ORDER BY filed_at DESC LIMIT 100""",
                            (body.country, from_d, to_d, found_ticker, f"%{body.company.lower()}%")
                        )
                        filings = [dict(r) for r in cur.fetchall()]
            except Exception as fe:
                logging.warning("company_dive: fresh fetch failed: %s", fe)

        # ── 3. Pull signals grouped by document ──────────────────────────────
        doc_ids = [f["id"] for f in filings]
        signals_by_type: dict[str, list[dict]] = defaultdict(list)
        # Also group by document for per-concall sentiment
        signals_by_doc: dict[int, list[dict]] = defaultdict(list)

        POSITIVE_SIGNALS = {"demand_surge","capex_increase","technology_adoption",
                            "regulatory_tailwind","market_entry","hiring_surge"}
        NEGATIVE_SIGNALS = {"supply_bottleneck","inventory_drawdown","capacity_shortage",
                            "demand_exceeds_supply","demand_slowdown","hiring_freeze"}

        if doc_ids:
            with pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """SELECT s.signal_type, s.confidence, s.context_text,
                                  s.document_id,
                                  d.filed_at::date AS filed_date,
                                  d.filing_type, d.title, d.id AS doc_id
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE s.document_id = ANY(%s)
                             AND s.context_text IS NOT NULL
                             AND LENGTH(s.context_text) > 30
                           ORDER BY d.filed_at DESC, s.confidence DESC
                           LIMIT 500""",
                        (doc_ids,)
                    )
                    for r in cur.fetchall():
                        row = dict(r)
                        st  = row["signal_type"]
                        did = row["doc_id"]
                        if len(signals_by_type[st]) < 5:
                            signals_by_type[st].append(row)
                        signals_by_doc[did].append(row)

        # ── Per-concall sentiment scoring ─────────────────────────────────────
        def _concall_sentiment(sigs: list[dict]) -> float:
            """Score 1-10: positive signals push up, negative push down. Base = 5."""
            score = 5.0
            for s in sigs:
                conf = float(s.get("confidence") or 0.7)
                st   = s.get("signal_type","")
                if st in POSITIVE_SIGNALS:
                    score += 0.6 * conf
                elif st in NEGATIVE_SIGNALS:
                    score -= 0.5 * conf
            return max(1.0, min(10.0, round(score, 1)))

        # Build per-concall timeline (one entry per unique filing date)
        concall_timeline: list[dict] = []
        seen_dates: set[str] = set()
        for filing in filings[:20]:
            did  = filing["id"]
            dt   = str(filing.get("filed_at",""))[:10]
            if dt in seen_dates:
                continue
            seen_dates.add(dt)
            doc_sigs = signals_by_doc.get(did, [])
            sentiment = _concall_sentiment(doc_sigs)
            # Pick best highlight per signal category
            constraint_quotes = [s["context_text"][:180] for s in doc_sigs
                                  if s["signal_type"] in NEGATIVE_SIGNALS
                                  and float(s.get("confidence",0)) > 0.7][:1]
            demand_quotes     = [s["context_text"][:180] for s in doc_sigs
                                  if s["signal_type"] in POSITIVE_SIGNALS
                                  and float(s.get("confidence",0)) > 0.7][:1]
            concall_timeline.append({
                "date":             dt,
                "filing_type":      filing.get("filing_type",""),
                "title":            filing.get("title","")[:60],
                "sentiment_score":  sentiment,
                "signal_count":     len(doc_sigs),
                "positive_signals": sum(1 for s in doc_sigs if s["signal_type"] in POSITIVE_SIGNALS),
                "negative_signals": sum(1 for s in doc_sigs if s["signal_type"] in NEGATIVE_SIGNALS),
                "constraint_quote": constraint_quotes[0] if constraint_quotes else "",
                "demand_quote":     demand_quotes[0] if demand_quotes else "",
            })

        # ── India policy / PLI cross-reference (run AFTER co_themes is populated) ──
        pli_matches: list[dict] = []
        budget_support: list[str] = []
        if False:  # placeholder — moved below after co_themes fetch
            pass
            # Match company sectors (from themes) against PLI schemes
            co_sectors = set()
            for t in co_themes:
                name = t.get("theme_name","").lower()
                for kw, sector in [
                    ("solar","Renewable Energy"),("electric vehicle","Automotive"),
                    ("ev ","Automotive"),("battery","Energy Storage / EV"),
                    ("semiconductor","Semiconductors"),("pharma","Pharmaceuticals"),
                    ("telecom","Telecom"),("steel","Steel"),("textile","Textiles"),
                    ("food","Food Processing"),("white goods","Consumer Electronics"),
                    ("mobile","Electronics"),("defence","Defence / Aviation"),
                    ("drone","Defence / Aviation"),("medic","Healthcare"),
                    ("railway","Railways"),("infrastructure","Infrastructure"),
                ]:
                    if kw in name:
                        co_sectors.add(sector)

            for scheme in _INDIA_PLI_SCHEMES:
                scheme_sector = scheme.get("sector","")
                # Match by sector
                if scheme_sector in co_sectors:
                    pli_matches.append({
                        "scheme": scheme["title"],
                        "budget_crore": scheme.get("budget_crore",0),
                        "incentive": scheme.get("incentive",""),
                        "match_reason": f"Company sector '{scheme_sector}' matches",
                        "layman_impact": scheme.get("layman_impact","")[:200],
                    })
                # Direct company name match in key_companies list
                elif any(body.company.lower() in co.lower() or found_ticker.lower() in co.lower()
                         for co in scheme.get("key_companies",[])):
                    pli_matches.append({
                        "scheme": scheme["title"],
                        "budget_crore": scheme.get("budget_crore",0),
                        "incentive": scheme.get("incentive",""),
                        "match_reason": "Company directly listed as beneficiary",
                        "layman_impact": scheme.get("layman_impact","")[:200],
                    })

            # Budget support check from causal chains / themes
            if co_sectors & {"Infrastructure","Railways","Defence / Aviation"}:
                budget_support.append("Budget 2024-25 allocated ₹11.11 lakh Cr capex — direct beneficiary")
            if "Renewable Energy" in co_sectors:
                budget_support.append("PM Surya Ghar scheme (₹75K Cr) drives rooftop solar demand")
            if "Semiconductors" in co_sectors:
                budget_support.append("PLI Semiconductor ₹76K Cr scheme + Tata/Micron fab investments")

        # ── 4. Themes this company appears in ─────────────────────────────────
        co_themes: list[dict] = []
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT t.theme_name, t.theme_slug, t.conviction,
                              tb.company_role, tb.relevance_score
                       FROM mg_theme_beneficiaries tb
                       JOIN mg_themes t ON t.id = tb.theme_id
                       WHERE t.is_active=TRUE AND t.country=%s
                         AND (LOWER(tb.company_name) ILIKE %s
                              OR UPPER(tb.ticker)=%s)
                       ORDER BY tb.relevance_score DESC
                       LIMIT 10""",
                    (body.country, f"%{body.company.lower()}%", found_ticker)
                )
                co_themes = [dict(r) for r in cur.fetchall()]

        # ── 5. Industry peers (same themes, top 10 by relevance) ─────────────
        peers: list[dict] = []
        if co_themes:
            theme_slugs = [t["theme_slug"] for t in co_themes[:3]]
            with pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """SELECT DISTINCT ON (tb.company_name)
                                  tb.company_name, tb.ticker, tb.company_role,
                                  tb.relevance_score, t.theme_name
                           FROM mg_theme_beneficiaries tb
                           JOIN mg_themes t ON t.id = tb.theme_id
                           WHERE t.theme_slug = ANY(%s)
                             AND t.country = %s
                             AND LOWER(tb.company_name) NOT ILIKE %s
                             AND tb.company_name IS NOT NULL
                           ORDER BY tb.company_name, tb.relevance_score DESC
                           LIMIT 20""",
                        (theme_slugs, body.country, f"%{body.company.lower()}%")
                    )
                    peers = [dict(r) for r in cur.fetchall()]

        # ── 5b. India PLI / policy cross-reference (now that co_themes is ready) ─
        if body.country == "IN":
            co_sectors = set()
            for t in co_themes:
                name_lc = t.get("theme_name","").lower()
                for kw, sector in [
                    ("solar","Renewable Energy"),("electric vehicle","Automotive"),
                    ("ev ","Automotive"),("battery","Energy Storage / EV"),
                    ("semiconductor","Semiconductors"),("pharma","Pharmaceuticals"),
                    ("telecom","Telecom"),("steel","Steel"),("textile","Textiles"),
                    ("food","Food Processing"),("white goods","Consumer Electronics"),
                    ("mobile","Electronics"),("defence","Defence / Aviation"),
                    ("drone","Defence / Aviation"),("medic","Healthcare"),
                    ("railway","Railways"),("infrastructure","Infrastructure"),
                ]:
                    if kw in name_lc:
                        co_sectors.add(sector)

            for scheme in _INDIA_PLI_SCHEMES:
                scheme_sector = scheme.get("sector","")
                if scheme_sector in co_sectors:
                    pli_matches.append({
                        "scheme": scheme["title"],
                        "budget_crore": scheme.get("budget_crore",0),
                        "incentive": scheme.get("incentive",""),
                        "match_reason": f"Sector '{scheme_sector}' matched from themes",
                        "layman_impact": scheme.get("layman_impact","")[:200],
                    })
                elif any(body.company.lower() in co.lower() or found_ticker.lower() in co.lower()
                         for co in scheme.get("key_companies",[])):
                    pli_matches.append({
                        "scheme": scheme["title"],
                        "budget_crore": scheme.get("budget_crore",0),
                        "incentive": scheme.get("incentive",""),
                        "match_reason": "Company directly listed as PLI beneficiary",
                        "layman_impact": scheme.get("layman_impact","")[:200],
                    })

            if co_sectors & {"Infrastructure","Railways","Defence / Aviation"}:
                budget_support.append("Union Budget 2024-25: ₹11.11 lakh Cr capex — direct beneficiary")
            if "Renewable Energy" in co_sectors:
                budget_support.append("PM Surya Ghar (₹75K Cr) drives rooftop solar demand")
            if "Semiconductors" in co_sectors:
                budget_support.append("PLI Semiconductor ₹76K Cr + Tata/Micron fab investments")

        # ── 5c. Pre-compute conviction + action from data (backend rules) ────────
        # Don't ask Claude to decide conviction — data-driven rules are more
        # reliable than LLM judgment which defaults to "medium".
        import statistics as _stats

        sentiment_scores = [c["sentiment_score"] for c in concall_timeline
                            if c.get("sentiment_score") is not None]
        overall_sentiment = round(_stats.mean(sentiment_scores), 1) if sentiment_scores else 5.0
        sentiment_trend_up = (
            len(sentiment_scores) >= 3 and
            sentiment_scores[-1] > sentiment_scores[0]  # last concall better than first
        )

        total_constraint = sum(len(v) for k, v in signals_by_type.items()
                               if k in {"supply_bottleneck","inventory_drawdown",
                                        "capacity_shortage","demand_exceeds_supply"})
        total_demand     = sum(len(v) for k, v in signals_by_type.items()
                               if k in {"demand_surge","technology_adoption","capex_increase"})
        total_capex      = len(signals_by_type.get("capex_increase", []))
        has_pli          = len(pli_matches) > 0

        # Conviction rules — explicit thresholds, no LLM guessing
        # Conviction thresholds — practical, data-driven
        # HIGH: sentiment trending positive with any direct signal evidence
        # MEDIUM: mixed or sparse signals
        # LOW: clear negatives or no evidence at all
        has_signals   = total_constraint > 0 or total_demand > 0 or total_capex > 0
        bullish_trend = overall_sentiment >= 6.5
        strong_trend  = overall_sentiment >= 7.5

        if strong_trend and has_signals:
            pre_conviction = "high"
        elif bullish_trend and has_signals and len(filings) >= 2:
            pre_conviction = "high"
        elif bullish_trend and len(filings) >= 3:
            pre_conviction = "high"                      # consistent positive sentiment, enough data
        elif overall_sentiment < 4.0 or (not has_signals and len(filings) < 2):
            pre_conviction = "low"
        elif overall_sentiment < 5.0 and not has_signals:
            pre_conviction = "low"
        else:
            pre_conviction = "medium"

        # Action rules
        if pre_conviction == "high" and (total_constraint >= 1 or total_capex >= 1):
            pre_action = "strong_buy"
        elif pre_conviction == "high":
            pre_action = "buy"
        elif overall_sentiment >= 6.0 and has_signals:
            pre_action = "buy"
        elif overall_sentiment < 4.5:
            pre_action = "hold"
        else:
            pre_action = "buy" if overall_sentiment >= 5.5 and has_signals else "hold"

        if total_capex >= 1 and total_constraint >= 1:
            pre_action = "strong_buy"   # capex + constraint = company solving the bottleneck

        # ── 6. Build Claude prompt ────────────────────────────────────────────
        def _sig_block(stype: str, label: str) -> str:
            sigs = signals_by_type.get(stype, [])
            if not sigs:
                return ""
            lines = [f"\n{label} ({len(sigs)} signals):"]
            for s in sigs[:2]:
                dt   = str(s.get("filed_date",""))[:7]
                conf = float(s.get("confidence",0))
                text = (s.get("context_text","") or "").strip()[:180]
                lines.append(f'  [{dt} conf:{conf:.2f}] "{text}"')
            return "\n".join(lines)

        # Concall timeline block for prompt
        timeline_block = "\n".join(
            f"  {c['date']} | {c['filing_type']} | Sentiment:{c['sentiment_score']}/10 "
            f"| +{c['positive_signals']}/-{c['negative_signals']} signals"
            + (f' | Constraint: "{c["constraint_quote"][:100]}"' if c.get("constraint_quote") else "")
            + (f' | Demand: "{c["demand_quote"][:100]}"' if c.get("demand_quote") else "")
            for c in concall_timeline[:12]
        ) or "No concall data available."

        themes_block = "\n".join(
            f"  • {t['theme_name']} [{t['conviction'].upper()}] | Role:{t['company_role']}"
            for t in co_themes
        ) or "Not mapped to any active themes."

        peers_block = ", ".join(
            f"{p['ticker'] or p['company_name']}" for p in peers[:15]
        ) or "None found."

        pli_block = ""
        if pli_matches:
            pli_block = "\n\nINDIA PLI / BUDGET POLICY SUPPORT:\n" + "\n".join(
                f"  • {m['scheme']} | ₹{m['budget_crore']:,} Cr | {m['incentive']} | {m['match_reason']}"
                for m in pli_matches
            )
        if budget_support:
            pli_block += "\n  Budget support: " + " | ".join(budget_support)

        sig_sections = "".join(filter(None, [
            _sig_block("supply_bottleneck",     "⚠️ SUPPLY BOTTLENECK"),
            _sig_block("inventory_drawdown",    "⚠️ INVENTORY DRAWDOWN"),
            _sig_block("demand_surge",          "📈 DEMAND SURGE"),
            _sig_block("capex_increase",        "🔨 CAPEX INCREASE"),
            _sig_block("demand_exceeds_supply", "🔴 DEMAND > SUPPLY"),
        ]))

        prompt = f"""You are an elite buy-side analyst covering {market}.
Analyse {found_name} ({found_ticker}) for {yr} using {yr-2}–{yr} concall data.

=== PRE-COMPUTED DATA METRICS (use these directly in recommendation) ===
Overall avg sentiment:  {overall_sentiment}/10
Total constraint sigs:  {total_constraint}
Total demand sigs:      {total_demand}
Total capex sigs:       {total_capex}
Filings found:          {len(filings)}
Sentiment trending:     {"UP (improving)" if sentiment_trend_up else "DOWN or flat"}
PLI policy support:     {"YES - " + str(len(pli_matches)) + " schemes matched" if has_pli else "None matched"}

PRE-COMPUTED RECOMMENDATION (derive from metrics above):
  action    = {pre_action}
  conviction = {pre_conviction}
Use these EXACTLY in your recommendation field. Override only if the qualitative evidence
strongly contradicts the data (e.g. known fraud, management change). Explain any override.

CONCALL TIMELINE (pre-scored sentiment 1-10, 10=most bullish):
{timeline_block}

SIGNAL EVIDENCE:
{sig_sections or "No signal evidence found."}

THEMES: {themes_block}
PEERS: {peers_block}{pli_block}

Respond ONLY in valid JSON:
{{
  "company_overview": "2-3 sentences: what this company does and market position",
  "investment_thesis": "3-4 sentences: core investment case for {yr} from the data",
  "concall_highlights": [
    {{
      "date": "YYYY-MM",
      "sentiment_score": 7,
      "sentiment_label": "Bullish|Neutral|Cautious|Bearish",
      "key_highlights": ["highlight 1 from management", "highlight 2", "highlight 3"],
      "standout_quote": "best single quote that captures management tone",
      "trend_vs_prior": "improving|stable|deteriorating"
    }}
  ],
  "sentiment_trend": "overall: improving|declining|volatile — 1-2 sentences on how management tone changed over the period",
  "constraint_analysis": {{
    "is_constrained_supplier": true,
    "constraint_type": "what they supply that is constrained",
    "evidence": "specific quote",
    "severity": "critical|high|moderate|low"
  }},
  "demand_analysis": {{
    "demand_trend": "growing|stable|declining",
    "key_demand_drivers": ["driver1", "driver2"],
    "demand_evidence": "specific quote"
  }},
  "capex_signal": {{
    "investing_to_grow": true,
    "capex_evidence": "specific quote",
    "implication": "what this means for future margins"
  }},
  "financial_signals": [
    {{"signal": "management commentary on margins/order book/revenue", "implication": "investment implication"}}
  ],
  "policy_support": {{
    "has_pli_benefit": {"true" if pli_matches else "false"},
    "schemes": [{{"name": "scheme name", "benefit": "how company benefits", "incentive": "incentive rate"}}],
    "budget_tailwinds": ["tailwind 1", "tailwind 2"],
    "policy_risk": "any policy risk to this company"
  }},
  "recommendation": {{
    "action": "strong_buy|buy|hold|sell",
    "conviction": "high|medium|low",
    "time_horizon": "3m|6m|12m|2y+",
    "price_trigger": "specific event that triggers buy",
    "stop_loss_trigger": "what invalidates the thesis"
  }},
  "peer_comparison": "1-2 sentences vs peers",
  "key_risks": ["risk1", "risk2", "risk3"],
  "best_quote": "single most powerful management quote from the data"
}}

Rules:
- concall_highlights: produce one entry per filing date in the timeline above
- Use sentiment_score from timeline (already pre-computed)
- key_highlights must be from actual signal quotes provided
- policy_support: based on PLI/budget data provided (if country=IN)
- Base ALL claims on signal evidence provided; say "insufficient data" if sparse"""

        logging.info("company_dive: calling Claude for %s (%d docs, %d signal types, %d pli matches)",
                     found_name, len(filings), len(signals_by_type), len(pli_matches))
        raw = _call_claude(prompt)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            brief = _json.loads(cleaned)
        except _json.JSONDecodeError:
            brief = {"raw": raw}

        result = {
            "company": found_name, "ticker": found_ticker,
            "country": body.country, "year": yr,
            "data_summary": {
                "filings_found":  len(filings),
                "signal_types":   list(signals_by_type.keys()),
                "themes_count":   len(co_themes),
                "peers_count":    len(peers),
                "pli_matches":    len(pli_matches),
                "period":         f"{yr-2}–{yr}",
            },
            "themes":            co_themes,
            "peers":             peers,
            "pli_matches":       pli_matches,
            "budget_support":    budget_support,
            "concall_timeline":  concall_timeline,
            "recent_filings": [
                {"date": str(f.get("filed_at",""))[:10],
                 "type": f.get("filing_type",""),
                 "title": f.get("title","")[:80]}
                for f in filings[:15]
            ],
            "brief":        brief,
            "generated_at": datetime.now().isoformat(),
            "from_cache":   False,
        }

        # Save to DB
        if pg and not brief.get("raw"):
            try:
                with pg._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO mg_ai_summaries
                               (country, year, context_type, summary_text, industry_insights, generated_at)
                               VALUES (%s,%s,'company_deep_dive',%s,%s::jsonb,NOW())
                               ON CONFLICT (country,year,context_type) DO UPDATE SET
                               summary_text=EXCLUDED.summary_text,
                               industry_insights=EXCLUDED.industry_insights,
                               generated_at=NOW()""",
                            (body.country, co_key,
                             brief.get("investment_thesis","")[:500],
                             # Save full result so cache loads everything including
                             # concall_timeline, pli_matches, budget_support
                             _json.dumps({
                                 **brief,
                                 "ticker":           found_ticker,
                                 "company":          found_name,
                                 "themes":           co_themes,
                                 "peers":            peers,
                                 "pli_matches":      pli_matches,
                                 "budget_support":   budget_support,
                                 "concall_timeline": concall_timeline,
                                 "recent_filings":   result["recent_filings"],
                                 "data_summary":     result["data_summary"],
                             }))
                        )
                    conn.commit()
            except Exception as e:
                logging.warning("company_dive save: %s", e)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logging.error("company_dive: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Company deep dive failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MACRO & POLICY
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/macro/series")
def get_macro_series(series_id: str = "GDP", from_date: str | None = None, to_date: str | None = None, country: str | None = None) -> list[dict]:
    try:
        from makrograph.macro.macro_store import MacroStore
        fd = from_date or str(date(date.today().year - 5, 1, 1))
        td = to_date or str(date.today())
        with MacroStore(CFG.get("postgresql", {})) as ms:
            return ms.get_series_history(series_id, date.fromisoformat(fd), date.fromisoformat(td), country=country)
    except Exception as e:
        logging.error("get_macro_series: %s", e)
        return []


@app.get("/api/macro/commodity")
def get_commodity(commodity_id: str = "WTI_CRUDE", from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    try:
        from makrograph.macro.macro_store import MacroStore
        fd = from_date or str(date(date.today().year - 5, 1, 1))
        td = to_date or str(date.today())
        with MacroStore(CFG.get("postgresql", {})) as ms:
            return ms.get_commodity_history(commodity_id, date.fromisoformat(fd), date.fromisoformat(td))
    except Exception as e:
        logging.error("get_commodity: %s", e)
        return []


@app.get("/api/macro/events")
def get_macro_threshold_events(as_of: str | None = None, since_days: int = 365) -> list[dict]:
    try:
        from makrograph.macro.macro_store import MacroStore
        with MacroStore(CFG.get("postgresql", {})) as ms:
            return ms.get_macro_events(
                as_of_date=date.fromisoformat(as_of) if as_of else date.today(),
                since_days=since_days,
            )
    except Exception as e:
        logging.error("get_macro_threshold_events: %s", e)
        return []


@app.get("/api/macro/policy-events")
def get_policy_events(
    as_of: str | None = None,
    sectors: str | None = None,
    impact_direction: str | None = None,
    country: str | None = None,
) -> list[dict]:
    try:
        from makrograph.macro.macro_store import MacroStore
        sector_list = [s.strip() for s in sectors.split(",")] if sectors else None
        with MacroStore(CFG.get("postgresql", {})) as ms:
            events = ms.get_recent_policy_events(
                as_of_date=date.fromisoformat(as_of) if as_of else date.today(),
                sectors=sector_list,
                limit=60,
                country=country,
            )
        if impact_direction and impact_direction != "All":
            events = [e for e in events if e.get("impact_direction") == impact_direction]
        return events
    except Exception as e:
        logging.error("get_policy_events: %s", e)
        return []


class MacroFetchBody(BaseModel):
    from_date: str
    to_date: str
    use_alfred: bool = False
    run_constraint_engine: bool = True
    country: str = "US"


@app.post("/api/macro/fetch")
def fetch_macro(body: MacroFetchBody) -> dict:
    try:
        from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
        run_cfg = copy.deepcopy(CFG)
        run_cfg.setdefault("fred", {})["use_alfred"] = body.use_alfred
        with IntelligencePipeline(run_cfg) as pip:
            pip._init_storage()
            pip._init_macro()
            stats = pip.run_macro(start_date=body.from_date, end_date=body.to_date, country=body.country)
        if not body.run_constraint_engine:
            stats["themes_constraint_scored"] = "skipped"
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# STOCK RANKINGS
# ═══════════════════════════════════════════════════════════════════════════════

class RankingBody(BaseModel):
    from_date: str
    to_date: str
    top_n_themes: int = 15
    country: str = "US"
    focus_theme_slugs: list[str] = []   # when set, only rank companies from these themes


@app.get("/api/india/rankings")
def get_india_rankings(
    as_of: str = None,
    lookback_days: int = 365,
    top_n: int = 50,
    min_purity: float = 0.25,
) -> dict:
    """India-specific ranking v0.2 — 10-factor balanced formula.

    v0.2 formula (9 factors balanced):
      22% Manufacturing Relevance  + 18% Constraint Severity
      + 18% Value Chain Position   + 15% SupplierQ
      + 12% ThemeCQ                + 10% Theme Separation
      + 5% Weighted Signal Score
      × Category Weight × Confluence Multiplier

    Supplier Purity Gate: companies below min_purity (default 0.25) excluded.
    """
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")
    try:
        from makrograph.india.india_ranking_engine import IndiaRankingEngine
        _as_of = date.fromisoformat(as_of) if as_of else date.today()
        engine = IndiaRankingEngine(pg)
        result = engine.run(
            as_of_date=_as_of,
            lookback_days=lookback_days,
            top_n=top_n,
            min_purity=min_purity,
        )
        return {
            "as_of_date":                 str(result.as_of_date),
            "themes_processed":           result.themes_processed,
            "companies_ranked":           result.companies_ranked,
            "companies_below_purity_gate": result.companies_below_purity_gate,
            "ranking_version":            "v0.2",
            "stocks": [
                {
                    "rank":                    s.rank,
                    "company":                 s.company,
                    "ticker":                  s.ticker,
                    "theme_name":              s.theme_name,
                    "themes_confluence":       s.themes_confluence,
                    "constrained_product":     s.constrained_product,
                    "role":                    s.role,
                    "category":                s.category,
                    "chain_distance":          s.chain_distance,
                    # v0.2 score components
                    "supplier_purity":         s.supplier_purity,
                    "manufacturing_relevance": s.manufacturing_relevance,
                    "value_chain_position":    s.value_chain_position,
                    "constraint_exposure":     s.constraint_exposure,
                    "category_weight":         s.category_weight,
                    "confluence_multiplier":   s.confluence_multiplier,
                    "theme_separation":        s.theme_separation_score,
                    "supplier_q":              s.supplier_q,
                    "theme_cq":                s.theme_cq,
                    "weighted_signal_score":   s.weighted_signal_score,
                    "final_score":             s.final_score,
                    "signal_count":            s.signal_count,
                    "rationale":               s.rationale,
                }
                for s in result.stocks
            ],
        }
    except Exception as e:
        logging.error("india_rankings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# INDUSTRY RANKING
# Events → Themes → Causal Chains → Industries → Human Research
# ─────────────────────────────────────────────────────────────────────────────

import re as _re

# Industry keyword → canonical industry name + icon
# Rules:
#  - All keywords matched as whole words (regex \b boundaries)
#  - Short ambiguous words (ev, ai, el) replaced with longer forms
#  - More specific industries listed FIRST so they win over broad ones
_INDUSTRY_MAP: list[tuple[list[str], str, str]] = [
    # Exact product/component matches first (most specific)
    (["solar cell", "solar wafer", "solar module", "solar glass",
      "polysilicon", "solar energy", "solar rooftop",
      "solar panel", "photovoltaic", "pv module"],                             "Solar & Renewables",    "☀️"),
    (["offshore wind", "wind turbine", "wind energy", "wind farm",
      "wind power", "inox wind", "suzlon"],                                    "Wind Energy",           "🌬️"),
    (["power transformer", "crgo steel", "crgo", "hvdc cable",
      "hv cable", "power grid", "power transmission", "electrical equipment",
      "transformer shortage", "grid equipment", "substation"],                 "Power Equipment",       "⚡"),
    (["battery cell", "battery pack", "acc battery", "battery storage",
      "energy storage", "lithium ion", "cathode active",
      "lithium supply", "gigatactory", "gigafactory"],                         "Battery & Storage",     "🔋"),
    (["electric vehicle", "traction motor", "ev battery", "ev charging",
      "bev", "phev", "automobile", "automotive supply",
      "vehicle electrification"],                                               "EV & Automotive",       "🚗"),
    (["semiconductor ic", "printed circuit board", "pcb shortage",
      "ems capacity", "display panel", "passive component",
      "semiconductor fab", "electronics manufacturing"],                        "Electronics & Semicon", "💻"),
    (["defense electronics", "radar system", "defence procurement",
      "indigenous defence", "mod procurement", "defence offset",
      "aerospace", "avionics", "military aircraft"],                           "Defense & Aerospace",   "🛡️"),
    (["rolling stock", "railway wagon", "locomotive", "metro rail",
      "vande bharat", "dedicated freight corridor", "railway electrification",
      "railway capex", "rail steel"],                                           "Railways",              "🚂"),
    (["optical fiber", "fiber cable", "fiber preform",
      "5g rollout", "5g equipment", "telecom tower", "bts unit"],              "Telecom & Fiber",       "📡"),
    (["specialty chemicals", "agrochemical", "herbicide",
      "pharmaceutical", "bulk drug", "active pharmaceutical",
      "biotech", "pharma"],                                                    "Pharma & Chemicals",    "⚗️"),
    (["healthcare", "hospital infrastructure", "medical device",
      "diagnostic", "health infra"],                                           "Healthcare",            "🏥"),
    (["cement", "ready mix", "road infrastructure", "water infrastructure",
      "real estate", "affordable housing", "jal jeevan",
      "sagarmala port", "airport"],                                            "Infrastructure",        "🏗️"),
    (["steel shortage", "steel demand", "steel supply", "steel capex",
      "special steel", "stainless steel", "iron ore", "aluminium",
      "copper conductor"],                                                      "Steel & Metals",        "🔩"),
    (["cloud computing", "data center", "hyperscaler",
      "artificial intelligence", "machine learning", "gpu shortage",
      "generative ai", "llm", "ai infrastructure"],                            "Data & AI Infra",       "🖥️"),
    (["green hydrogen", "electrolyzer", "hydrogen production"],                 "Green Hydrogen",        "🟢"),
    (["port capacity", "logistics", "shipping", "container"],                  "Ports & Logistics",     "🚢"),
    (["textiles", "textile manufacturing", "apparel", "garment"],              "Textiles",              "🧵"),
    (["food processing", "agri", "agriculture", "crop"],                       "Agri & Food",           "🌾"),
    # Broad sector names — checked LAST so they don't steal from specific ones above
    (["solar", "renewable", "mnre", "clean energy"],                           "Solar & Renewables",    "☀️"),
    (["wind"],                                                                  "Wind Energy",           "🌬️"),
    # "power" and "energy" as standalone words → Power Equipment sector
    (["power", "energy", "powergrid", "electricity", "grid"],                  "Power Equipment",       "⚡"),
    (["battery", "lithium"],                                                   "Battery & Storage",     "🔋"),
    (["automotive"],                                                            "EV & Automotive",       "🚗"),
    (["semiconductor", "chip", "wafer", "foundry", "pcb", "ems"],             "Electronics & Semicon", "💻"),
    (["defense", "defence"],                                                    "Defense & Aerospace",   "🛡️"),
    (["railway", "rail"],                                                       "Railways",              "🚂"),
    (["telecom", "fiber optic"],                                                "Telecom & Fiber",       "📡"),
    (["chemical", "agrochemic", "agro"],                                       "Pharma & Chemicals",    "⚗️"),
    (["hospital", "healthcare"],                                                "Healthcare",            "🏥"),
    (["cement", "infrastructure", "construction"],                             "Infrastructure",        "🏗️"),
    # "material" / "materials" → Steel & Metals (commodity constraint proxy)
    (["steel", "metal", "material", "commodity", "copper", "iron"],            "Steel & Metals",        "🔩"),
    (["cloud", "data centre"],                                                  "Data & AI Infra",       "🖥️"),
    (["textile", "apparel"],                                                    "Textiles",              "🧵"),
]

# Compile each keyword as a whole-word regex pattern
_INDUSTRY_PATTERNS: list[tuple[list[_re.Pattern], str, str]] = [
    (
        [_re.compile(r'\b' + _re.escape(kw) + r'\b', _re.I) for kw in kws],
        industry,
        icon,
    )
    for kws, industry, icon in _INDUSTRY_MAP
]


def _extract_entity(theme_name: str) -> str:
    """Strip generic ranking suffixes to get the core entity/sector.

    e.g. 'Power: Constraint from Energy Demand'  → 'Power'
         'Materials Critical Shortage'            → 'Materials'
         'cloud: Demand-Supply Tension'           → 'cloud'
         'Automotive Critical Shortage'           → 'Automotive'
    """
    # Pattern: "X: Constraint from Y Demand" → use X
    m = _re.match(r'^([^:]+):\s*constraint\s+from', theme_name, _re.I)
    if m:
        return m.group(1).strip()
    # Pattern: "X: Demand-Supply Tension" or "X: Constraint ..." → use X
    m = _re.match(r'^([^:]+):', theme_name)
    if m:
        return m.group(1).strip()
    # Pattern: "X Critical Shortage" / "X Severe Constraint" / "X Supply Gap" → use X
    cleaned = _re.sub(
        r'\b(Critical|Severe|Supply|Demand|Gap|Shortage|Constraint|Tension|'
        r'Manufacturing|Localization|Opportunity|Capacity|Import|Dependency)\b.*$',
        '', theme_name, flags=_re.I,
    ).strip()
    return cleaned or theme_name


def _classify_industry(text: str) -> tuple[str, str]:
    """Classify a theme/chain name into an industry using whole-word matching.

    First strips generic suffixes so 'Power: Constraint from Energy Demand'
    is evaluated as 'Power', not as a string containing 'constraint' (which
    would false-match 'ai' in 'constr-ai-nt').
    """
    entity = _extract_entity(text)
    # Try entity name first (most accurate), then full text as fallback
    for src in (entity, text):
        tl = src.lower()
        for patterns, industry, icon in _INDUSTRY_PATTERNS:
            if any(p.search(tl) for p in patterns):
                return industry, icon
    return "Other", "📦"


@app.get("/api/industries")
def get_industry_ranking(
    country:    str  = "IN",
    as_of:      str  = None,
    from_date:  str  = None,
    top_n:      int  = 20,
) -> list[dict]:
    """Rank industries by theme strength + causal chain activation + company evidence.

    Pipeline position: Events → Themes → Causal Chains → **Industries** → Human Research

    For each industry:
        - themes:       active themes in this year window
        - theme_score:  sum of snap_strength (or strength_score) for themes
        - chain_score:  sum of activation_score for causal chains in this industry
        - company_count: distinct companies from beneficiaries
        - top_companies: top 5 companies by conviction
        - signal_count: total signals in the window
        - constraint_products: constrained products (from beneficiaries)
    """
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")

    from datetime import date as _date
    _as_of    = _date.fromisoformat(as_of)    if as_of    else _date.today()
    _from     = _date.fromisoformat(from_date) if from_date else _date(_as_of.year, 1, 1)

    try:
        from psycopg2.extras import RealDictCursor

        # 1. Themes for this year
        themes = pg.get_themes_as_of(
            as_of_date=_as_of, from_date=_from,
            min_strength=0, country=country,
        ) if as_of else pg.get_active_themes(min_strength=0, country=country)

        # 2. Causal chains scoped to this year window
        # Use last_scored_at to pin chains to the period they were active in.
        # Falls back to first_detected when last_scored_at is NULL.
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT chain_name, terminal_effect, activation_score, links
                    FROM mg_causal_chains
                    WHERE country = %s
                      AND activation_score > 10
                      AND (
                          COALESCE(last_scored_at, first_detected) BETWEEN %s AND %s
                      )
                    ORDER BY activation_score DESC
                """, (country, _from, _as_of))
                chains = [dict(r) for r in cur.fetchall()]

        # 3. Beneficiaries — India only (mg_india_beneficiaries has no US data)
        beneficiaries: list[dict] = []
        if country == "IN":
            with pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if as_of:
                        cur.execute("""
                            SELECT company, ticker, constrained_product, conviction_score,
                                   theme_name, signal_count
                            FROM mg_india_beneficiaries
                            WHERE as_of_date = %s
                            ORDER BY conviction_score DESC
                        """, (_as_of,))
                    else:
                        cur.execute("""
                            SELECT company, ticker, constrained_product, conviction_score,
                                   theme_name, signal_count
                            FROM mg_india_beneficiaries
                            WHERE as_of_date = (SELECT MAX(as_of_date) FROM mg_india_beneficiaries)
                            ORDER BY conviction_score DESC
                        """)
                    beneficiaries = [dict(r) for r in cur.fetchall()]

        # ── Signal-type weights (severity-based, not volume-based) ──────────────
        # Constraint signals: supply pressure, shortages → high weight
        # Demand signals: growth pull → medium weight
        # Negative signals: easing, slowdown → subtract pressure
        _CONSTRAINT_TYPES = {
            "supply_bottleneck":     3.0,
            "capacity_shortage":     3.0,
            "inventory_drawdown":    1.5,
            "localization_opportunity": 1.5,
        }
        _DEMAND_TYPES = {
            "demand_surge":          2.0,
            "capex_increase":        1.5,
            "tender_pipeline":       1.5,
            "policy_support":        1.0,
            "hiring_surge":          1.0,
        }
        _NEGATIVE_TYPES = {
            "demand_slowdown":      -1.0,
            "supply_easing":        -1.0,
            "capex_decrease":       -1.0,
            "inventory_buildup":    -0.5,
        }
        _ALL_WEIGHTED = {**_CONSTRAINT_TYPES, **_DEMAND_TYPES, **_NEGATIVE_TYPES}

        # ── Conviction multipliers for theme scoring ──────────────────────────
        _CONV_MULT = {
            "high": 1.5, "confirmed": 1.2,
            "developing": 1.0, "emerging": 0.6, "watch": 0.3,
        }

        # 4. Weighted signals per entity (constraint pressure + demand pull)
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT lower(e.canonical_name) AS entity,
                           s.signal_type,
                           COUNT(*)                AS cnt
                    FROM mg_signals s
                    JOIN mg_documents d           ON d.id = s.document_id
                    JOIN mg_document_entities de  ON de.document_id = s.document_id
                    JOIN mg_entities e            ON e.id = de.entity_id
                    WHERE d.country = %s
                      AND d.filed_at BETWEEN %s AND %s
                      AND s.signal_type = ANY(%s)
                    GROUP BY lower(e.canonical_name), s.signal_type
                    ORDER BY cnt DESC
                    LIMIT 1000
                """, (country, _from, _as_of, list(_ALL_WEIGHTED.keys())))
                # entity → {constraint_score, demand_score}
                entity_signals: dict[str, dict] = {}
                for r in cur.fetchall():
                    ent  = r["entity"]
                    stype = r["signal_type"]
                    cnt  = int(r["cnt"])
                    w    = _ALL_WEIGHTED.get(stype, 0.0)
                    weighted = w * cnt
                    if ent not in entity_signals:
                        entity_signals[ent] = {"constraint": 0.0, "demand": 0.0}
                    if stype in _CONSTRAINT_TYPES:
                        entity_signals[ent]["constraint"] += weighted
                    elif stype in _DEMAND_TYPES:
                        entity_signals[ent]["demand"] += weighted
                    else:  # negative — reduces both
                        entity_signals[ent]["constraint"] += weighted
                        entity_signals[ent]["demand"]     += weighted

        # 5. Aggregate into industries
        import math
        industries: dict[str, dict] = {}

        def _get(ind, icon):
            if ind not in industries:
                industries[ind] = {
                    "industry": ind, "icon": icon,
                    "theme_score": 0.0, "chain_score": 0.0,
                    "constraint_score": 0.0, "demand_score": 0.0,
                    "themes": [], "chains": [], "top_companies": [],
                    "constraint_products": set(),
                    "companies_set": set(),
                    "conviction_sum": 0.0,
                }
            return industries[ind]

        # Themes → industry  (conviction-weighted strength, not raw sum)
        for t in themes:
            name  = str(t.get("theme_name") or "")
            score = float(t.get("snap_strength") or t.get("strength_score") or 0)
            conv  = str(t.get("conviction") or "emerging").lower()
            mult  = _CONV_MULT.get(conv, 1.0)
            ind, icon = _classify_industry(name)
            rec = _get(ind, icon)
            rec["theme_score"] += score * mult          # conviction-weighted
            rec["themes"].append({
                "name": name,
                "score": round(score, 1),
                "conviction": conv,
                "slug": str(t.get("theme_slug") or ""),
                "snap_date": str(t.get("snap_date") or ""),
            })

        # Causal chains → industry
        for ch in chains:
            cname    = str(ch.get("chain_name") or "")
            terminal = str(ch.get("terminal_effect") or "")
            cscore   = float(ch.get("activation_score") or 0)
            for src in (cname, terminal):
                ind, icon = _classify_industry(src)
                if ind != "Other":
                    rec = _get(ind, icon)
                    rec["chain_score"] += cscore
                    rec["chains"].append({
                        "name": cname, "terminal": terminal,
                        "score": round(cscore, 1),
                    })
                    break

        # Beneficiaries → industry  (avg conviction quality, not breadth count)
        for b in beneficiaries:
            product = str(b.get("constrained_product") or "")
            theme_n = str(b.get("theme_name") or "")
            company = str(b.get("company") or "")
            ticker  = str(b.get("ticker") or "")
            conv    = float(b.get("conviction_score") or 0)
            ind, icon = _classify_industry(product or theme_n)
            rec = _get(ind, icon)
            if product:
                rec["constraint_products"].add(product)
            if company and company not in rec["companies_set"]:
                rec["companies_set"].add(company)
                rec["conviction_sum"] += conv
                rec["top_companies"].append({
                    "company": company, "ticker": ticker, "conviction": round(conv, 3),
                })

        # Entity weighted signals → industry constraint / demand scores
        for entity, scores in entity_signals.items():
            ind, icon = _classify_industry(entity)
            if ind in industries:
                industries[ind]["constraint_score"] += scores["constraint"]
                industries[ind]["demand_score"]     += scores["demand"]

        # 6. Compute final score + clean up
        results = []
        for ind, rec in industries.items():
            theme_score      = rec["theme_score"]
            chain_score      = rec["chain_score"]
            constraint_score = max(0.0, rec["constraint_score"])   # floor at 0
            demand_score     = max(0.0, rec["demand_score"])
            co_count         = len(rec["companies_set"])

            # Avg conviction quality (0–1 range × 100 to normalize with other scores)
            avg_conv = (rec["conviction_sum"] / co_count * 100) if co_count > 0 else 0.0

            # Net pressure = constraint + demand (both severity-weighted, not counts)
            pressure_score = math.log1p(constraint_score + demand_score) * 8

            if country == "IN":
                # India: beneficiary conviction quality is meaningful (mg_india_beneficiaries)
                # 40% conviction-weighted theme strength
                # 25% causal chain activation
                # 25% net signal pressure (severity-weighted supply+demand)
                # 10% avg beneficiary conviction quality
                final = round(
                    theme_score    * 0.40 +
                    chain_score    * 0.25 +
                    pressure_score * 0.25 +
                    avg_conv       * 0.10,
                    2,
                )
            else:
                # US: no beneficiaries table — redistribute to theme + pressure
                # 45% conviction-weighted theme strength
                # 25% causal chain activation
                # 30% net signal pressure (severity-weighted supply+demand)
                final = round(
                    theme_score    * 0.45 +
                    chain_score    * 0.25 +
                    pressure_score * 0.30,
                    2,
                )

            rec["themes"].sort(key=lambda x: -x["score"])
            rec["top_companies"].sort(key=lambda x: -x["conviction"])

            results.append({
                "industry":           ind,
                "icon":               rec["icon"],
                "final_score":        final,
                "theme_score":        round(theme_score, 1),
                "chain_score":        round(chain_score, 1),
                "constraint_score":   round(constraint_score, 1),
                "demand_score":       round(demand_score, 1),
                "pressure_score":     round(pressure_score, 1),
                "avg_conviction":     round(avg_conv, 1),
                "theme_count":        len(rec["themes"]),
                "chain_count":        len(set(c["name"] for c in rec["chains"])),
                "company_count":      co_count,
                "themes":             rec["themes"][:8],
                "chains":             list({c["name"]: c for c in rec["chains"]}.values())[:5],
                "top_companies":      rec["top_companies"][:8],
                "constraint_products": sorted(rec["constraint_products"])[:6],
            })

        results.sort(key=lambda x: -x["final_score"])
        return results[:top_n]

    except Exception as e:
        logging.error("get_industry_ranking: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rankings/run")
def run_rankings(body: RankingBody) -> dict:
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")
    try:
        from makrograph.ranking import RankingEngine
        engine = RankingEngine(pg)
        rk_themes, rk_stocks = engine.run(
            date_from=date.fromisoformat(body.from_date),
            date_to=date.fromisoformat(body.to_date),
            top_n_themes=body.top_n_themes,
            country=body.country,
            focus_theme_slugs=body.focus_theme_slugs or [],
        )
        rk_stocks = list(rk_stocks)

        from datetime import date as _date_cls
        cq_floor = 0.45
        cq_floor_names = {t.theme_name for t in rk_themes if getattr(t, "theme_cq", 0) >= cq_floor}

        themes_out = [
            {
                "theme_name":      t.theme_name,
                "theme_slug":      getattr(t, "theme_slug", ""),
                "conviction":      t.conviction,
                "rank_score_pct":  t.rank_score_pct,
                "momentum":        round(t.momentum, 3),
                "persistence":     round(getattr(t, "persistence", 0), 3),
                "novelty":         round(getattr(t, "novelty", 0), 3),
                "signal_intensity":round(getattr(t, "signal_intensity", 0), 3),
                "theme_cq":        round(getattr(t, "theme_cq", 0), 3),
                "company_count":   getattr(t, "company_count", 0),
                "first_detected":  str(getattr(t, "first_detected", "") or ""),
                "from_cq_floor":   t.theme_name in cq_floor_names,
            }
            for t in rk_themes
        ]

        def _freshness(fsa) -> str:
            if not fsa:
                return "❓"
            try:
                fsd = fsa if isinstance(fsa, _date_cls) else _date_cls.fromisoformat(str(fsa)[:10])
                age = (_date_cls.today() - fsd).days
                if age <= 90:   return f"🟢 Fresh · {fsd}"
                if age <= 365:  return f"🟡 Active · {fsd}"
                return f"🔴 Mature · {fsd} ({age}d)"
            except Exception:
                return "❓"

        stocks_out = [
            {
                "rank":              s.rank,
                "ticker":            s.ticker,
                "company_name":      s.company_name,
                "company_role":      s.company_role,
                "role_confidence":   s.role_confidence,
                "category_weight":   s.category_weight,
                "final_score":       s.final_score,
                "effective_theme":   s.effective_theme,
                "supplier_quality":  s.supplier_quality,
                "confluence_score":  s.confluence_score,
                "constraint_quality":s.constraint_quality,
                "edge_score":        s.edge_score,
                "first_seen_at":     str(s.first_seen_at) if s.first_seen_at else None,
                "freshness":         _freshness(s.first_seen_at),
                "themes":            s.themes[:5],
                "theme_slugs":       s.theme_slugs[:5],
                "per_theme_edges":   s.per_theme_edges,
                "signal_highlights": s.signal_highlights,
                "supplier_quality":  s.supplier_quality,
                "quality_breakdown": s.quality_breakdown,
                "conf_breakdown":    s.conf_breakdown,
                "cq_breakdown":      s.cq_breakdown,
            }
            for s in rk_stocks
        ]
        return {
            "themes":    themes_out,
            "stocks":    stocks_out,
            "date_from": body.from_date,
            "date_to":   body.to_date,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# AI ANALYSIS (Gemini)
# ═══════════════════════════════════════════════════════════════════════════════

class AIAnalysisBody(BaseModel):
    prompt: str
    mode: str = "master"
    market: str = "USA"
    themes_count: int = 0
    sl_count: int = 0
    bottlenecks_count: int = 0
    stocks_count: int = 0


@app.post("/api/ai/analyze")
def run_ai_analysis(body: AIAnalysisBody) -> dict:
    try:
        result = _call_claude(body.prompt)
        return {
            "result": result,
            "mode": body.mode,
            "market": body.market,
            "themes_count": body.themes_count,
            "sl_count": body.sl_count,
            "bottlenecks_count": body.bottlenecks_count,
            "stocks_count": body.stocks_count,
            "generated_at": datetime.now().strftime("%H:%M:%S"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error("run_ai_analysis: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"AI analysis failed: {e}")


class InvestmentBriefBody(BaseModel):
    country: str = "US"
    year: int | None = None
    min_constraint: int = 1
    top_n_companies: int = 50
    force_refresh: bool = False   # when True, regenerate even if saved brief exists


@app.get("/api/ai/investment-brief")
def get_saved_investment_brief(country: str = "US", year: int | None = None) -> dict:
    """Return a previously saved investment brief, or {} if not yet generated."""
    pg = get_pg()
    if not pg:
        return {}
    try:
        yr = str(year or date.today().year)
        from psycopg2.extras import RealDictCursor
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT summary_text, industry_insights, generated_at
                       FROM mg_ai_summaries
                       WHERE country = %s AND year = %s AND context_type = 'investment_brief'""",
                    (country, yr)
                )
                row = cur.fetchone()
                if not row:
                    return {}
                import json as _j
                brief_data = row["industry_insights"] or {}
                if isinstance(brief_data, str):
                    try: brief_data = _j.loads(brief_data)
                    except: brief_data = {}
                return {
                    "year":         int(yr),
                    "country":      country,
                    "market":       "India (NSE/BSE)" if country == "IN" else "USA (NYSE/NASDAQ)",
                    "brief":        brief_data,
                    "generated_at": str(row["generated_at"]),
                    "from_cache":   True,
                }
    except Exception as e:
        logging.error("get_saved_brief: %s", e)
        return {}


@app.post("/api/ai/investment-brief")
def run_investment_brief(body: InvestmentBriefBody) -> dict:
    """Investment brief where theme ↔ company matching is validated by signal evidence.

    Data pipeline:
    1. Load focus themes (NEW/ESCALATING with YoY delta)
    2. Load shortlisted companies (investability scored)
    3. For EACH company, pull their actual constraint + demand quotes from filings
    4. Group companies under their matched themes — only show companies whose
       constraint quotes actually mention entities relevant to that theme
    5. Send structured theme→company→signal-evidence data to Claude
    6. Claude validates pairs and produces clean investable brief
    """
    pg = get_pg()
    acfg    = CFG.get("anthropic", {})
    api_key = acfg.get("api_key", "")
    if not api_key:
        raise HTTPException(status_code=400,
            detail="Anthropic API key not configured. Add to config/secrets.json: anthropic.api_key")

    try:
        import json as _json
        from psycopg2.extras import RealDictCursor
        from collections import defaultdict

        yr    = body.year or date.today().year
        from_d = date(yr, 1, 1)
        to_d   = date(yr, 12, 31) if yr < date.today().year else date.today()
        market = "India (NSE/BSE)" if body.country == "IN" else "USA (NYSE/NASDAQ)"

        # ── Step 1: Focus themes ──────────────────────────────────────────────
        focus_themes: list[dict] = []
        if pg:
            try:
                focus_themes = pg.get_year_focus_analysis(yr, body.country)
            except Exception as e:
                logging.warning("brief/themes: %s", e)

        actionable = [t for t in focus_themes
                      if t.get("focus_class") in ("new","escalating","no_prior")]

        if not actionable:
            return {
                "year": yr, "country": body.country, "market": market,
                "brief": {"executive_summary":
                    f"No actionable (NEW/ESCALATING) themes found for {market} {yr}. "
                    "Run the pipeline for this year first."},
                "data_summary": {}, "generated_at": datetime.now().isoformat()
            }

        # ── Step 2: Investment shortlist ──────────────────────────────────────
        shortlist: list[dict] = []
        if pg:
            try:
                shortlist = get_investment_shortlist(
                    country=body.country, year=body.year,
                    top_n=body.top_n_companies, capex_focus=True,
                )
            except Exception as e:
                logging.warning("brief/shortlist: %s", e)

        # ── Step 3: Build structured theme→company evidence from SHORTLIST ────
        # The shortlist already has constraint_count, best_quote, capex_signals,
        # constraint_delta from its own pre-computed scoring. We use THAT data
        # instead of re-querying signals (which fails due to date/name mismatches).
        # This avoids the "all excluded" problem: we have real scored evidence,
        # just present it properly to Claude.
        
        theme_co_map: dict[str, list[dict]] = {}
        for co in shortlist:
            for co_theme in (co.get("themes") or []):
                tn = (co_theme.get("name","") if isinstance(co_theme, dict) else str(co_theme))
                if tn:
                    theme_co_map.setdefault(tn, []).append(co)

        sections: list[str] = []
        # Cap themes and companies per theme to keep prompt size manageable
        max_themes_in_prompt = min(10, len(actionable))
        for t in actionable[:max_themes_in_prompt]:
            tname  = t.get("theme_name","")
            focus  = t.get("focus_class","")
            delta  = t.get("delta_pct")
            conv   = t.get("conviction","")
            this_s = t.get("this_avg_strength", 0)
            prior_s= t.get("prior_avg_strength", 0)
            delta_str = (f"+{delta:.0f}% YoY" if delta and delta>0
                         else ("new — no prior year" if not delta else f"{delta:.0f}% YoY"))

            section = [
                f"\nTHEME: \"{tname}\" [{focus.upper()}] [{conv.upper()}]",
                f"Strength: {this_s:.0f} | Prior: {prior_s:.0f} | {delta_str}",
            ]

            matched_cos = theme_co_map.get(tname, [])[:5]   # max 5 per theme
            if not matched_cos:
                section.append("  No companies with direct theme mapping found.")
            else:
                section.append("  COMPANIES (pre-scored from concall signals):")
                for c in matched_cos:
                    action      = c.get("action","watch")
                    score       = c.get("investability_score", 0)
                    csigs       = c.get("constraint_signals", 0)
                    dsigs       = c.get("demand_signals", 0)
                    capex       = c.get("capex_signals", 0)
                    c_delta     = c.get("constraint_delta", 0)
                    quote       = (c.get("best_quote") or "").strip()[:180]
                    prior_c     = c.get("prior_constraint", 0)
                    # Evidence tier
                    if csigs >= 3 and quote:
                        tier = "STRONG — direct constraint evidence + quote"
                    elif csigs >= 1 and (dsigs >= 1 or capex >= 1):
                        tier = "MODERATE — constraint + demand signals confirmed"
                    elif csigs >= 1:
                        tier = "WEAK — constraint signals only, no demand confirmation"
                    else:
                        tier = "THEME-MAPPED — beneficiary by supply-chain structure"

                    co_lines = [
                        f"  • {c.get('ticker','')} ({c.get('company','')}) | {action.upper()} | Score:{score:.0f}",
                        f"    Evidence tier: {tier}",
                        f"    Signals: ⚠️{csigs} constraint | 📈{dsigs} demand"
                        + (f" | 🔨{capex} capex" if capex else "")
                        + (f" | ↑+{c_delta} YoY vs prior:{prior_c}" if c_delta > 0 else ""),
                    ]
                    if quote:
                        co_lines.append(f"    Best quote: \"{quote}\"")
                    section.extend(co_lines)
            sections.append("\n".join(section))

        # ── Causal chains ────────────────────────────────────────────────────
        chains_txt = ""
        if pg:
            try:
                with pg._conn() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(
                            """SELECT chain_name, terminal_effect, activation_score
                               FROM mg_causal_chains
                               WHERE is_active = TRUE AND country = %s
                               ORDER BY activation_score DESC LIMIT 8""",
                            (body.country,)
                        )
                        chains_txt = "\n".join(
                            f"  {r['chain_name']} → {r['terminal_effect']} (score:{r['activation_score']:.0f})"
                            for r in cur.fetchall()
                        )
            except Exception as e:
                logging.warning("brief/chains: %s", e)

        # ── PLI context (India) ───────────────────────────────────────────────
        pli_ctx = ""
        if body.country == "IN":
            high = [s for s in _INDIA_PLI_SCHEMES
                    if s.get("year",0) <= yr and s.get("impact_magnitude",0) >= 4]
            pli_ctx = ("\n\nACTIVE INDIA PLI SCHEMES (high-impact):\n" +
                "\n".join(f"  - {s['title']}: {', '.join(s.get('key_companies',[])[:3])}"
                           for s in high[:5]))

        # ─────────────────────────────────────────────────────────────────────
        # TWO-PASS CHUNKED APPROACH
        # Pass 1 (small/fast): theme intelligence — executive summary, ranked
        #         themes, industries. No company data = small prompt.
        # Pass 2 (chunked): companies split into batches of 4 themes each.
        #         Each batch is a separate Claude call, results merged.
        # This avoids timeouts while producing richer output than trimming.
        # ─────────────────────────────────────────────────────────────────────

        def _clean_json(raw: str) -> dict:
            s = raw.strip()
            if s.startswith("```"):
                lines = s.split("\n")
                s = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            try:
                return _json.loads(s)
            except _json.JSONDecodeError:
                return {"raw": raw}

        # ── Pass 1: Theme Intelligence ────────────────────────────────────────
        theme_lines = "\n".join(
            f"{i+1}. [{t.get('focus_class','').upper()}] {t.get('theme_name','')} "
            f"[{str(t.get('conviction','')).upper()}] "
            f"Strength:{t.get('this_avg_strength',0):.0f} "
            f"Prior:{t.get('prior_avg_strength',0):.0f} "
            f"Delta:{'+' if (t.get('delta_pct') or 0)>0 else ''}{t.get('delta_pct') or 'new'}%"
            for i, t in enumerate(actionable[:15])
        )

        pass1_prompt = f"""Analyst covering {market} for {yr}. Respond ONLY in valid JSON.

CONSTRAINT INTELLIGENCE {yr}:
Causal chains: {chains_txt or 'none'}
{pli_ctx}

NEW/ESCALATING THEMES ({len(actionable)}):
{theme_lines}

Output JSON:
{{
  "executive_summary": "3-4 sentences on dominant constraint regime and investment opportunity in {yr}",
  "top_themes": [
    {{"rank":1,"name":"exact theme","focus":"new|escalating","strength_delta":"+34%",
      "investment_thesis":"2-sentence thesis with specific constraint dynamics",
      "constrained_component":"specific material/component bottlenecked",
      "key_catalyst":"trigger for payoff","time_horizon":"6m|12m|18m|2y+",
      "conviction":"high|medium|low","key_risk":"main risk"}}
  ],
  "top_industries": [
    {{"rank":1,"industry":"name","rationale":"1-2 sentences","constraint_driver":"bottleneck"}}
  ],
  "key_risks": ["risk1","risk2","risk3"],
  "contrarian_view": "underappreciated angle"
}}
Rules: top_themes = all actionable themes ranked by investment priority (up to 10).
top_industries = 5 industries. JSON only, no markdown."""

        logging.info("brief pass1: sending theme prompt (%d chars)", len(pass1_prompt))
        pass1_raw  = _call_claude(pass1_prompt)
        pass1      = _clean_json(pass1_raw)
        top_themes = pass1.get("top_themes", [])
        # Get the ranked theme names from pass1 to drive pass2 ordering
        ranked_theme_names = [t.get("name","") for t in top_themes]

        # ── Pass 2: Company Rankings (chunked by theme batches) ───────────────
        # Split sections into batches of 3 themes each to keep prompts small
        BATCH_SIZE = 3
        section_batches = [sections[i:i+BATCH_SIZE] for i in range(0, len(sections), BATCH_SIZE)]

        all_companies: list[dict] = []
        seen_tickers: set[str]   = set()

        for batch_idx, batch in enumerate(section_batches):
            batch_prompt = f"""Analyst covering {market} for {yr}. Respond ONLY in valid JSON.

EVIDENCE TIERS:
- STRONG: ≥3 constraint signals + quote → high conviction
- MODERATE: constraint + demand/capex signals → medium-high
- WEAK: constraint signals only → medium
- THEME-MAPPED: beneficiary mapping only → low

THEMES AND COMPANIES (batch {batch_idx+1}/{len(section_batches)}):
{"".join(batch)}

Output JSON array of companies:
[
  {{"rank":1,"ticker":"TICKER","company":"Name",
    "action":"act_now|research|watch",
    "evidence_tier":"strong|moderate|weak|theme_mapped",
    "theme":"theme name",
    "investability_score":85,
    "thesis":"1-sentence thesis from signal evidence",
    "constraint_evidence_used":"quote or signal summary",
    "capex_responding":true,
    "conviction":"high|medium|low",
    "key_risk":"main risk"}}
]
Rules:
- Include ALL companies from the data above
- act_now: score≥60 AND strong/moderate evidence
- research: score≥40 OR moderate/weak + escalating theme
- watch: others
- NEVER omit a company — assign conviction even if evidence is weak
- JSON array only, no wrapper object"""

            logging.info("brief pass2 batch %d: %d chars", batch_idx+1, len(batch_prompt))
            try:
                batch_raw = _call_claude(batch_prompt)
                batch_cos = _clean_json(batch_raw)
                # batch_cos should be a list; handle if Claude wraps it
                if isinstance(batch_cos, dict):
                    batch_cos = batch_cos.get("top_companies", batch_cos.get("companies", []))
                if isinstance(batch_cos, list):
                    for co in batch_cos:
                        ticker = str(co.get("ticker","")).strip().upper()
                        if ticker and ticker not in seen_tickers:
                            seen_tickers.add(ticker)
                            all_companies.append(co)
            except Exception as e:
                logging.warning("brief pass2 batch %d failed: %s", batch_idx+1, e)

        # Re-rank merged companies by investability_score DESC then action priority
        ACTION_ORDER = {"act_now": 0, "research": 1, "watch": 2}
        all_companies.sort(key=lambda c: (
            ACTION_ORDER.get(str(c.get("action","watch")), 3),
            -float(c.get("investability_score", 0))
        ))
        # Re-assign sequential ranks
        for i, co in enumerate(all_companies):
            co["rank"] = i + 1

        # Merge pass1 + pass2 into final result
        parsed = {
            "executive_summary": pass1.get("executive_summary",""),
            "validation_notes":  f"Analysed {len(all_companies)} companies across {len(sections)} themes in {len(section_batches)} batches.",
            "top_themes":        top_themes,
            "top_industries":    pass1.get("top_industries",[]),
            "top_companies":     all_companies,
            "key_risks":         pass1.get("key_risks",[]),
            "contrarian_view":   pass1.get("contrarian_view",""),
        }

        result = {
            "year": yr,
            "country": body.country,
            "market": market,
            "data_summary": {
                "actionable_themes":       len(actionable),
                "companies_analysed":      len(shortlist),
                "companies_with_evidence": sum(1 for c in shortlist if c.get("constraint_signals", 0) > 0),
                "causal_chains":           len(chains_txt.split("\n")) if chains_txt else 0,
                "claude_batches":          len(section_batches) + 1,
            },
            "brief":        parsed,
            "generated_at": datetime.now().isoformat(),
            "from_cache":   False,
        }

        # Persist to mg_ai_summaries — loads instantly on next visit
        if pg and not parsed.get("raw"):
            try:
                exec_sum = parsed.get("executive_summary", "")
                with pg._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO mg_ai_summaries
                                   (country, year, context_type, summary_text, industry_insights, generated_at)
                               VALUES (%s, %s, 'investment_brief', %s, %s::jsonb, NOW())
                               ON CONFLICT (country, year, context_type) DO UPDATE SET
                                   summary_text      = EXCLUDED.summary_text,
                                   industry_insights = EXCLUDED.industry_insights,
                                   generated_at      = NOW()""",
                            (body.country, str(yr), exec_sum, _json.dumps(parsed))
                        )
                    conn.commit()
                logging.info("brief saved: %s/%s", body.country, yr)
            except Exception as save_err:
                logging.warning("brief save failed (non-fatal): %s", save_err)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logging.error("investment_brief: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Investment brief failed: {e}")



@app.get("/api/ai/cache")
def get_ai_cache(country: str = "US") -> dict:
    cache_path = ROOT / "data" / "ai_analysis_cache.json"
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path) as f:
            full = json.load(f)
        return full.get(country, {})
    except Exception:
        return {}


# ─── Persistent AI Summaries (Year Intelligence + Industries) ─────────────────

@app.get("/api/ai/summary")
def get_ai_summary(country: str = "IN", year: str = "2024", context_type: str = "year_intelligence") -> dict:
    """Fetch stored AI summary for (country, year, context_type)."""
    pg = get_pg()
    if not pg:
        return {}
    try:
        from psycopg2.extras import RealDictCursor
        with pg._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT summary_text, industry_insights, generated_at
                    FROM mg_ai_summaries
                    WHERE country = %s AND year = %s AND context_type = %s
                """, (country, year, context_type))
                row = cur.fetchone()
        if not row:
            return {}
        return {
            "summary_text":       row["summary_text"],
            "industry_insights":  row["industry_insights"] or {},
            "generated_at":       row["generated_at"].isoformat() if row["generated_at"] else None,
        }
    except Exception as e:
        logging.error("get_ai_summary: %s", e)
        return {}


class YearSummaryBody(BaseModel):
    country: str = "IN"
    year: str = "2024"


@app.post("/api/ai/year-summary")
def generate_year_summary(body: YearSummaryBody) -> dict:
    """Generate and store an AI narrative for a given country/year using that year's themes and chains."""
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")

    from datetime import date as _date
    from psycopg2.extras import RealDictCursor

    year = body.year
    country = body.country
    as_of_date   = _date(int(year), 12, 31) if year != "live" else _date.today()
    from_date    = _date(int(year), 1, 1)   if year != "live" else _date(as_of_date.year, 1, 1)

    # Fetch themes for this year
    themes = pg.get_themes_as_of(
        as_of_date=as_of_date, from_date=from_date, min_strength=0, country=country
    ) if year != "live" else pg.get_active_themes(min_strength=0, country=country)

    themes_sorted = sorted(themes, key=lambda t: float(t.get("snap_strength") or t.get("strength_score") or 0), reverse=True)[:15]

    # Fetch causal chains for this year
    with pg._conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT chain_name, terminal_effect, activation_score
                FROM mg_causal_chains
                WHERE country = %s AND activation_score > 10
                  AND COALESCE(last_scored_at, first_detected) BETWEEN %s AND %s
                ORDER BY activation_score DESC LIMIT 10
            """, (country, from_date, as_of_date))
            chains = [dict(r) for r in cur.fetchall()]

    country_label = "India" if country == "IN" else "US"
    year_label = year if year != "live" else "current live"

    theme_lines = "\n".join(
        f"  - {t.get('theme_name')} (strength: {float(t.get('snap_strength') or t.get('strength_score') or 0):.0f}, conviction: {t.get('conviction', '')})"
        for t in themes_sorted
    )
    chain_lines = "\n".join(
        f"  - {c['chain_name']} → {c['terminal_effect']} (score: {c['activation_score']:.0f})"
        for c in chains
    )

    prompt = f"""You are a macroeconomic intelligence analyst. Analyze the following {year_label} data for {country_label} and write a concise, insightful 3-4 paragraph narrative.

TOP THEMES ({year_label}):
{theme_lines or "  (none)"}

ACTIVE CAUSAL CHAINS ({year_label}):
{chain_lines or "  (none)"}

Write a structured analysis covering:
1. The dominant macro regime and what's driving it
2. The 2-3 most significant investment themes and their conviction level
3. Key causal chains in play and what they signal
4. Overall risk/opportunity outlook for investors

Be specific to {year_label} data only. Do not reference past or future years. Keep it under 400 words. Use plain text (no markdown headers or bullet points)."""

    summary_text = _call_claude(prompt)

    # Upsert into DB
    with pg._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mg_ai_summaries (country, year, context_type, summary_text, industry_insights, generated_at)
                VALUES (%s, %s, 'year_intelligence', %s, '{}', NOW())
                ON CONFLICT (country, year, context_type)
                DO UPDATE SET summary_text = EXCLUDED.summary_text, generated_at = NOW()
            """, (country, year, summary_text))
        conn.commit()

    return {"summary_text": summary_text, "generated_at": datetime.now().isoformat()}


class IndustrySummaryBody(BaseModel):
    country: str = "IN"
    year: str = "2024"


@app.post("/api/ai/industry-summary")
def generate_industry_summary(body: IndustrySummaryBody) -> dict:
    """Generate and store AI industry ranking insights for a given country/year."""
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")

    from datetime import date as _date
    import math as _math
    from psycopg2.extras import RealDictCursor

    year = body.year
    country = body.country
    as_of_str   = f"{year}-12-31" if year != "live" else None
    from_str    = f"{year}-01-01" if year != "live" else None

    # Re-use the existing ranking logic by calling the endpoint internally
    as_of_date  = _date(int(year), 12, 31) if year != "live" else _date.today()
    from_date   = _date(int(year), 1, 1)   if year != "live" else _date(as_of_date.year, 1, 1)

    themes = pg.get_themes_as_of(
        as_of_date=as_of_date, from_date=from_date, min_strength=0, country=country
    ) if year != "live" else pg.get_active_themes(min_strength=0, country=country)

    with pg._conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT chain_name, terminal_effect, activation_score
                FROM mg_causal_chains
                WHERE country = %s AND activation_score > 10
                  AND COALESCE(last_scored_at, first_detected) BETWEEN %s AND %s
                ORDER BY activation_score DESC
            """, (country, from_date, as_of_date))
            chains = [dict(r) for r in cur.fetchall()]

    # Quick industry aggregation (same logic as /api/industries)
    industries: dict[str, dict] = {}

    def _get_ind(ind, icon):
        if ind not in industries:
            industries[ind] = {"industry": ind, "icon": icon, "theme_score": 0.0,
                               "chain_score": 0.0, "themes": [], "company_count": 0}
        return industries[ind]

    for t in themes:
        name = str(t.get("theme_name") or "")
        score = float(t.get("snap_strength") or t.get("strength_score") or 0)
        ind, icon = _classify_industry(name)
        rec = _get_ind(ind, icon)
        rec["theme_score"] += score
        rec["themes"].append(name)

    for ch in chains:
        cname = str(ch.get("chain_name") or "")
        terminal = str(ch.get("terminal_effect") or "")
        cscore = float(ch.get("activation_score") or 0)
        for src in (cname, terminal):
            ind, icon = _classify_industry(src)
            if ind != "Other":
                rec = _get_ind(ind, icon)
                rec["chain_score"] += cscore
                break

    # Sort by combined score
    ranked = sorted(
        industries.values(),
        key=lambda r: r["theme_score"] * 0.6 + r["chain_score"] * 0.4,
        reverse=True,
    )[:12]

    country_label = "India" if country == "IN" else "US"
    year_label = year if year != "live" else "current live"

    ind_lines = "\n".join(
        f"  {i+1}. {r['industry']} — theme score: {r['theme_score']:.0f}, chain score: {r['chain_score']:.0f}, top themes: {', '.join(r['themes'][:3])}"
        for i, r in enumerate(ranked)
    )

    prompt = f"""You are a sector allocation analyst. Below is the ranked industry list for {country_label} in {year_label}, derived purely from macroeconomic themes, causal chains, and signal density active in that year.

RANKED INDUSTRIES ({year_label}):
{ind_lines or "  (none)"}

Your task:
1. Write a 2-3 sentence overall narrative explaining the macro regime that produced this ranking.
2. For EACH industry in the list, write exactly ONE sentence explaining why it ranks where it does based on the {year_label} data (themes and causal chains driving or suppressing it).

Format your response as JSON with this exact structure:
{{
  "overall": "...",
  "insights": {{
    "Industry Name 1": "one sentence insight",
    "Industry Name 2": "one sentence insight"
  }}
}}

Be specific to {year_label} data only. Do not reference other years."""

    raw = _call_claude(prompt)

    # Parse the JSON response
    import re as _re
    insights: dict = {}
    overall = ""
    try:
        # Extract JSON block if wrapped in markdown
        json_match = _re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            parsed = json.loads(json_match.group())
            overall = parsed.get("overall", "")
            insights = parsed.get("insights", {})
    except Exception:
        overall = raw[:500]

    # Upsert into DB
    with pg._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mg_ai_summaries (country, year, context_type, summary_text, industry_insights, generated_at)
                VALUES (%s, %s, 'industries', %s, %s, NOW())
                ON CONFLICT (country, year, context_type)
                DO UPDATE SET summary_text = EXCLUDED.summary_text,
                              industry_insights = EXCLUDED.industry_insights,
                              generated_at = NOW()
            """, (country, year, overall, json.dumps(insights)))
        conn.commit()

    return {
        "summary_text":      overall,
        "industry_insights": insights,
        "generated_at":      datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUN  (SSE streaming)
# ═══════════════════════════════════════════════════════════════════════════════

class PipelineRunBody(BaseModel):
    country: str = "US"
    start_date: str
    end_date: str
    is_replay: bool = False
    do_ingest: bool = True
    do_nlp: bool = True
    do_graph: bool = True
    do_events: bool = True
    do_causal: bool = True
    do_india_intelligence: bool = False
    do_themes: bool = True
    do_contradictions: bool = True
    do_pdf_fetch_india: bool = False
    pdf_fetch_workers: int = 6
    skip_neo4j: bool = False
    nlp_batch_size: int = 500
    fetch_mode: str = "selected"
    force_reprocess_nlp: bool = False   # Reset docs to 'fetched' before NLP so they are reprocessed
    max_companies: int = 200
    resume: bool = False


class _QueueHandler(logging.Handler):
    def __init__(self, q: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.q = q
        self.loop = loop
        self.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s", "%H:%M:%S"
        ))

    def emit(self, record):
        try:
            self.loop.call_soon_threadsafe(self.q.put_nowait, self.format(record))
        except Exception:
            pass


@app.post("/api/pipeline/run")
async def run_pipeline(body: PipelineRunBody):
    """Streams pipeline log lines as SSE events."""

    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    loop = asyncio.get_event_loop()

    def _push(msg: str):
        try:
            loop.call_soon_threadsafe(q.put_nowait, msg)
        except Exception:
            pass

    def _run_in_thread():
        handler = _QueueHandler(q, loop)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            run_cfg = copy.deepcopy(CFG)
            run_cfg.setdefault("edgar", {})["fetch_mode"] = body.fetch_mode
            run_cfg["edgar"]["max_companies_per_run"] = body.max_companies
            run_cfg.setdefault("market", {})["country"] = body.country

            start = date.fromisoformat(body.start_date)
            end   = date.fromisoformat(body.end_date)

            if body.is_replay:
                from makrograph.pipeline.historical_runner import HistoricalRunner, generate_monthly_timeline
                runner = HistoricalRunner(
                    config=run_cfg,
                    start_date=start, end_date=end,
                    replay_mode="monthly",
                    skip_ingest=not body.do_ingest,
                    skip_neo4j=body.skip_neo4j,
                    skip_nlp=not body.do_nlp,
                    skip_graph=not body.do_graph,
                    skip_events=not body.do_events,
                    skip_causal=not body.do_causal,
                    skip_themes=not body.do_themes,
                    skip_pdf_fetch=not body.do_pdf_fetch_india,
                )
                runner._init_pipeline()
                timeline = generate_monthly_timeline(start, end)
                for ws, we in timeline:
                    _push(f"[REPLAY] Month {we.strftime('%Y-%m')}")
                    result = runner._run_month(ws, we)
                    runner._log_result(result)
                runner._close()
            else:
                from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
                since_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
                until_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
                pipeline = IntelligencePipeline(run_cfg)
                pipeline._init_storage()
                if body.do_ingest:
                    _push("[STAGE] Ingest starting…")
                    if body.country == "IN":
                        pipeline.run_ingest_india(since=since_dt, until=until_dt)
                    else:
                        pipeline.run_ingest(since=since_dt)
                if body.do_pdf_fetch_india:
                    _push("[STAGE] PDF Fetch (India)…")
                    pipeline.run_pdf_fetch_india(max_workers=body.pdf_fetch_workers)
                if body.do_nlp:
                    if body.force_reprocess_nlp:
                        _push("[STAGE] Force-resetting document status to 'fetched' for NLP re-run…")
                        pg = get_pg()
                        if pg:
                            try:
                                from psycopg2.extras import RealDictCursor
                                with pg._conn() as conn:
                                    with conn.cursor() as cur:
                                        cur.execute("""
                                            UPDATE mg_documents
                                            SET processing_status = 'fetched',
                                                sentiment_score = NULL,
                                                nlp_summary = NULL
                                            WHERE country = %s
                                              AND filed_at BETWEEN %s AND %s
                                              AND processing_status IN ('nlp_done', 'graph_built', 'embedded')
                                        """, (body.country, start, end))
                                        reset_count = cur.rowcount
                                    conn.commit()
                                _push(f"[STAGE] Reset {reset_count} documents to 'fetched' status")
                            except Exception as e:
                                _push(f"[ERROR] Force-reset failed: {e}")
                    _push("[STAGE] NLP…")
                    pipeline._init_nlp()
                    pipeline.run_nlp(batch_size=body.nlp_batch_size, window_start=start,
                                     window_end=end, country=body.country)
                if body.do_graph and not body.skip_neo4j:
                    _push("[STAGE] Graph…")
                    pipeline._init_graph_builder()
                    pipeline.run_graph(window_start=start, window_end=end, country=body.country)
                if body.do_events:
                    _push("[STAGE] Events…")
                    pipeline._init_intelligence()
                    pipeline.run_events(window_start=start, window_end=end, country=body.country)
                if body.do_causal:
                    _push("[STAGE] Causal chains…")
                    pipeline.run_causal_chains(as_of_date=end, country=body.country)
                if body.do_india_intelligence and body.country == "IN":
                    _push("[STAGE] India Intelligence (Layers 1-10)…")
                    intel_stats = pipeline.run_india_intelligence(
                        as_of_date=end,
                        lookback_days=(end - start).days or 365,
                    )
                    _push(
                        f"[INDIA-INTEL] "
                        f"policy={intel_stats.get('policy_targets',0)} "
                        f"gaps={intel_stats.get('capacity_gaps',0)} "
                        f"import_deps={intel_stats.get('import_dependencies',0)} "
                        f"localization={intel_stats.get('localization_opportunities',0)} "
                        f"beneficiaries={intel_stats.get('beneficiaries_discovered',0)} "
                        f"order_book_signals={intel_stats.get('order_book_signals_generated',0)} "
                        f"causal_chains={intel_stats.get('causal_chains_persisted',0)}"
                    )
                    if intel_stats.get("errors"):
                        _push(f"[INDIA-INTEL] Non-fatal errors: {intel_stats['errors']}")
                if body.do_themes:
                    _push("[STAGE] Themes…")
                    pipeline._init_themes()
                    pipeline.run_themes(as_of_date=end, country=body.country)
                if body.do_contradictions:
                    _push("[STAGE] Contradictions…")
                    pipeline.run_contradictions()
                pipeline.close()
            _push("[DONE] Pipeline complete.")
        except Exception as exc:
            _push(f"[ERROR] {exc}")
        finally:
            root.removeHandler(handler)
            loop.call_soon_threadsafe(q.put_nowait, "__END__")

    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()

    async def event_generator():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"  # SSE comment — keeps connection alive, ignored by frontend
                continue
            if msg == "__END__":
                yield "data: [DONE]\n\n"
                break
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════════════════
# PRICE DATA  (NSE/BSE bhavcopy + screener fundamentals + sector master)
# ═══════════════════════════════════════════════════════════════════════════════

def _price_pg_config() -> dict:
    pg = CFG.get("postgresql", {})
    return {
        "host": pg.get("host", "localhost"),
        "port": pg.get("port", 5432),
        "dbname": pg.get("dbname", "makrograph"),
        "user": pg.get("user", "postgres"),
        "password": os.getenv("MAKROGRAPH_PG_PASSWORD", pg.get("password", "")),
    }


@app.get("/api/price-data/status")
async def price_data_status():
    """Return date ranges + row counts for NSE/BSE bhavcopy and fundamentals."""
    pg_cfg = _price_pg_config()
    result: dict = {}
    try:
        conn = psycopg2.connect(**pg_cfg)
        with conn.cursor() as cur:
            for table, key in [("nse_bhavcopy_data", "nse"), ("bse_bhavcopy_data", "bse")]:
                cur.execute(
                    "SELECT to_regclass(%s)", (f"public.{table}",)
                )
                exists = cur.fetchone()[0] is not None
                if not exists:
                    result[key] = {"exists": False, "min_date": None, "max_date": None, "row_count": 0}
                    continue
                cur.execute(f"SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM {table}")
                min_d, max_d, count = cur.fetchone()
                result[key] = {
                    "exists": True,
                    "min_date": min_d.isoformat() if min_d else None,
                    "max_date": max_d.isoformat() if max_d else None,
                    "row_count": count,
                }
            cur.execute("SELECT to_regclass('public.fundamentals_snapshot')")
            if cur.fetchone()[0] is not None:
                cur.execute("SELECT COUNT(*), MAX(last_updated) FROM fundamentals_snapshot")
                count, last_upd = cur.fetchone()
                result["fundamentals"] = {
                    "exists": True, "row_count": count,
                    "last_updated": last_upd.isoformat() if last_upd else None,
                }
            else:
                result["fundamentals"] = {"exists": False, "row_count": 0, "last_updated": None}

            cur.execute("SELECT to_regclass('public.security_master')")
            if cur.fetchone()[0] is not None:
                cur.execute("SELECT COUNT(*), MAX(last_updated) FROM security_master")
                count, last_upd = cur.fetchone()
                result["sector_master"] = {
                    "exists": True, "row_count": count,
                    "last_updated": last_upd.isoformat() if last_upd else None,
                }
            else:
                result["sector_master"] = {"exists": False, "row_count": 0, "last_updated": None}
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


def _safe_float(v) -> float | None:
    """Convert a DB numeric to float, coercing NaN/Infinity (which are not
    valid JSON) to None."""
    if v is None:
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    return f


@app.get("/api/price-data/high-volume")
async def price_data_high_volume(
    start_date: str = Query(...),
    end_date: str = Query(...),
    exchange: str = Query("both"),
    min_volume: float = Query(1_000_000),
    limit: int = Query(100),
):
    """For each symbol, find its all-time (lifetime) highest-volume trading
    day across the *entire* table history — not just within the selected
    range — then keep only symbols whose lifetime-peak day (a) falls inside
    the given date range and (b) exceeds ``min_volume``.  This surfaces
    stocks that set a new all-time volume record during the selected window,
    rather than merely the busiest day within that window.
    """
    pg_cfg = _price_pg_config()
    start_d = date.fromisoformat(start_date)
    end_d = date.fromisoformat(end_date)

    table_map = {"nse": "nse_bhavcopy_data", "bse": "bse_bhavcopy_data"}
    exchanges = list(table_map.keys()) if exchange == "both" else [exchange]
    if any(ex not in table_map for ex in exchanges):
        raise HTTPException(status_code=400, detail="exchange must be 'nse', 'bse', or 'both'")

    rows_out: list[dict] = []
    try:
        conn = psycopg2.connect(**pg_cfg)
        with conn.cursor() as cur:
            for ex in exchanges:
                table = table_map[ex]
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cur.fetchone()[0] is None:
                    continue
                cur.execute(
                    f"""
                    WITH lifetime_peak AS (
                        SELECT DISTINCT ON (symbol)
                            symbol, trade_date, series, tottrdqty, tottrdval, close, delivery_pct
                        FROM {table}
                        ORDER BY symbol, tottrdqty DESC
                    )
                    SELECT symbol, trade_date, series, tottrdqty, tottrdval, close, delivery_pct
                    FROM lifetime_peak
                    WHERE trade_date BETWEEN %s AND %s
                      AND tottrdqty >= %s
                    """,
                    (start_d, end_d, min_volume),
                )
                for symbol, trade_date, series, volume, value, close, delivery_pct in cur.fetchall():
                    rows_out.append({
                        "exchange": ex.upper(),
                        "symbol": symbol,
                        "series": series,
                        "trade_date": trade_date.isoformat() if trade_date else None,
                        "volume": _safe_float(volume),
                        "value": _safe_float(value),
                        "close": _safe_float(close),
                        "delivery_pct": _safe_float(delivery_pct),
                    })
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    rows_out.sort(key=lambda r: r["volume"] or 0, reverse=True)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "min_volume": min_volume,
        "count": len(rows_out[:limit]),
        "total_matches": len(rows_out),
        "results": rows_out[:limit],
    }


class PriceDataRunBody(BaseModel):
    mode: str                       # "daily" | "historical" | "copy-from-algo" | "fundamentals" | "sector-master"
    exchange: str = "both"          # "nse" | "bse" | "both"
    start_date: str | None = None
    end_date: str | None = None
    days_back: int = 5
    symbols: list[str] = []


@app.post("/api/price-data/run")
async def run_price_data(body: PriceDataRunBody):
    """Streams price-data fetch log lines as SSE events."""

    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    loop = asyncio.get_event_loop()

    def _run_in_thread():
        handler = _QueueHandler(q, loop)
        root = logging.getLogger()
        root.addHandler(handler)
        pg_cfg = _price_pg_config()
        try:
            if body.mode == "daily":
                from makrograph.fetcher.nse_price_fetcher import NSEPriceFetcher
                from makrograph.fetcher.bse_price_fetcher import BSEPriceFetcher
                if body.exchange in ("nse", "both"):
                    n = NSEPriceFetcher(pg_cfg)
                    n.ensure_table()
                    total = n.fetch_latest(body.days_back)
                    logging.info(f"[DONE-NSE] {total} rows inserted/updated")
                if body.exchange in ("bse", "both"):
                    b = BSEPriceFetcher(pg_cfg)
                    b.ensure_table()
                    total = b.fetch_latest(body.days_back)
                    logging.info(f"[DONE-BSE] {total} rows inserted/updated")

            elif body.mode == "historical":
                if not body.start_date:
                    raise ValueError("start_date is required for historical mode")
                start = date.fromisoformat(body.start_date)
                end = date.fromisoformat(body.end_date) if body.end_date else date.today()
                from makrograph.fetcher.nse_price_fetcher import NSEPriceFetcher
                from makrograph.fetcher.bse_price_fetcher import BSEPriceFetcher
                if body.exchange in ("nse", "both"):
                    n = NSEPriceFetcher(pg_cfg)
                    total = n.fetch_date_range(start, end)
                    logging.info(f"[DONE-NSE] {total} total rows for {start} → {end}")
                if body.exchange in ("bse", "both"):
                    b = BSEPriceFetcher(pg_cfg)
                    total = b.fetch_date_range(start, end)
                    logging.info(f"[DONE-BSE] {total} total rows for {start} → {end}")

            elif body.mode == "copy-from-algo":
                import psycopg2.extras as _pgx
                algo_cfg = {
                    "host": os.getenv("ALGO_TEST_PG_HOST", "localhost"),
                    "port": int(os.getenv("ALGO_TEST_PG_PORT", "5432")),
                    "dbname": os.getenv("ALGO_TEST_PG_DBNAME", "Algo_Test"),
                    "user": os.getenv("ALGO_TEST_PG_USER", "postgres"),
                    "password": os.getenv("ALGO_TEST_PG_PASSWORD", "mak43"),
                }
                from makrograph.fetcher.nse_price_fetcher import NSEPriceFetcher
                from makrograph.fetcher.bse_price_fetcher import BSEPriceFetcher
                from makrograph.fetcher.screener_fundamentals_fetcher import ScreenerFundamentalsFetcher
                NSEPriceFetcher(pg_cfg).ensure_table()
                BSEPriceFetcher(pg_cfg).ensure_table()
                ScreenerFundamentalsFetcher(pg_cfg).ensure_table()

                date_filter_sql = ""
                date_filter_params: tuple = ()
                if body.start_date:
                    start_d = date.fromisoformat(body.start_date)
                    end_d = date.fromisoformat(body.end_date) if body.end_date else date.today()
                    date_filter_sql = " WHERE trade_date BETWEEN %s AND %s"
                    date_filter_params = (start_d, end_d)
                    logging.info(f"Filtering copy to trade_date range {start_d} → {end_d}")

                logging.info(f"Connecting to Algo_Test at {algo_cfg['host']}:{algo_cfg['port']}/{algo_cfg['dbname']}")
                src_conn = psycopg2.connect(**algo_cfg)
                dst_conn = psycopg2.connect(**pg_cfg)
                try:
                    with src_conn.cursor() as cur:
                        cur.execute("""
                            SELECT table_name FROM information_schema.tables
                            WHERE table_schema = 'public'
                              AND table_name IN ('nse_bhavcopy_data', 'bse_bhavcopy_data',
                                                 'fundamentals_snapshot')
                        """)
                        available = {r[0] for r in cur.fetchall()}

                    def _copy(table, columns, filter_sql="", filter_params=()):
                        cols_str = ", ".join(columns)
                        with src_conn.cursor() as scur:
                            scur.execute(f"SELECT {cols_str} FROM {table}{filter_sql}", filter_params)
                            rows = scur.fetchall()
                        if not rows:
                            logging.info(f"  {table}: no rows in source")
                            return 0
                        with dst_conn.cursor() as dcur:
                            _pgx.execute_values(
                                dcur,
                                f"INSERT INTO {table} ({cols_str}) VALUES %s ON CONFLICT DO NOTHING",
                                rows, page_size=1000,
                            )
                        dst_conn.commit()
                        logging.info(f"  {table}: copied {len(rows)} rows")
                        return len(rows)

                    if "nse_bhavcopy_data" in available and body.exchange in ("nse", "both"):
                        _copy("nse_bhavcopy_data", [
                            "trade_date", "symbol", "series", "prev_close", "open", "high", "low",
                            "last", "close", "avg_price", "tottrdqty", "tottrdval", "totaltrades",
                            "delivery_qty", "delivery_pct",
                        ], date_filter_sql, date_filter_params)
                    if "bse_bhavcopy_data" in available and body.exchange in ("bse", "both"):
                        _copy("bse_bhavcopy_data", [
                            "trade_date", "symbol", "series", "prev_close", "open", "high", "low",
                            "last", "close", "avg_price", "tottrdqty", "tottrdval", "totaltrades",
                            "delivery_qty", "delivery_pct", "bse_instrument_id",
                        ], date_filter_sql, date_filter_params)
                    if "fundamentals_snapshot" in available:
                        with src_conn.cursor() as cur:
                            cur.execute("""
                                SELECT column_name FROM information_schema.columns
                                WHERE table_name = 'fundamentals_snapshot' AND table_schema = 'public'
                            """)
                            src_cols = {r[0] for r in cur.fetchall()}
                        fund_cols = [c for c in [
                            "nse_symbol", "bse_symbol", "bse_instrument_id", "company_name",
                            "sector", "industry", "market_cap", "pe_ratio", "pb_ratio", "book_value",
                            "dividend_yield", "roce", "roe", "face_value", "eps", "debt_to_equity",
                            "price_to_book", "sales_growth_3y", "profit_growth_3y", "current_ratio",
                            "promoter_holding", "fii_holding", "dii_holding", "pledge_percentage",
                            "screener_url", "data_json", "quarterly_data_json", "pl_data_json",
                            "balance_sheet_json", "cash_flow_json", "shareholding_json",
                            "peer_comparison_json", "price_data_json",
                        ] if c in src_cols]
                        _copy("fundamentals_snapshot", fund_cols)
                    logging.info("[DONE] Copy from Algo_Test complete")
                finally:
                    src_conn.close()
                    dst_conn.close()

            elif body.mode == "fundamentals":
                if not body.symbols:
                    raise ValueError("symbols is required for fundamentals mode")
                from makrograph.fetcher.screener_fundamentals_fetcher import ScreenerFundamentalsFetcher
                fetcher = ScreenerFundamentalsFetcher(pg_cfg)
                results = fetcher.fetch_symbols(body.symbols)
                ok = sum(1 for v in results.values() if v == "ok")
                logging.info(f"[DONE] Fundamentals: {ok}/{len(body.symbols)} succeeded — {results}")

            elif body.mode == "sector-master":
                from makrograph.fetcher.sector_master_fetcher import SectorMasterFetcher
                bse_dir = CFG.get("bse", {}).get("downloads_dir")
                fetcher = SectorMasterFetcher(pg_cfg, bse_downloads_dir=bse_dir)
                total = fetcher.run()
                logging.info(f"[DONE] Sector master: {total} rows upserted")

            else:
                raise ValueError(f"Unknown mode: {body.mode}")

            logging.info("[DONE] Price data fetch complete.")
        except Exception as exc:
            logging.error(f"[ERROR] {exc}")
        finally:
            root.removeHandler(handler)
            loop.call_soon_threadsafe(q.put_nowait, "__END__")

    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()

    async def event_generator():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if msg == "__END__":
                yield "data: [DONE]\n\n"
                break
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
