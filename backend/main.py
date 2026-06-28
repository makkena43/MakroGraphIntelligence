"""MakroGraph Intelligence — FastAPI Backend.

All data endpoints that power the React frontend.
Run with:  uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

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
                         AND tb.company_name IS NOT NULL AND tb.company_name != \'\'
                       ORDER BY tb.relevance_score DESC""",
                    (theme_ids,)
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
                    Uses ticker match (reliable) then company name match (fallback)."""
                    results_map: dict[str, dict] = {}
                    
                    if tiks:
                        cur.execute(
                            """SELECT UPPER(TRIM(d.ticker)) AS doc_ticker,
                                      COUNT(*) FILTER (WHERE s.signal_type IN (
                                          'supply_bottleneck','inventory_drawdown',
                                          'capacity_shortage','demand_exceeds_supply'
                                      )) AS c_count,
                                      COUNT(*) FILTER (WHERE s.signal_type IN (
                                          'demand_surge','technology_adoption'
                                      )) AS d_count,
                                      COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase') AS capex_count,
                                      (ARRAY_AGG(s.context_text ORDER BY s.confidence DESC)
                                       FILTER (WHERE s.signal_type IN (
                                           'supply_bottleneck','inventory_drawdown',
                                           'capacity_shortage','demand_exceeds_supply'
                                       ) AND s.context_text IS NOT NULL))[1] AS best_quote,
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
                            """SELECT d.company,
                                      COUNT(*) FILTER (WHERE s.signal_type IN (
                                          'supply_bottleneck','inventory_drawdown',
                                          'capacity_shortage','demand_exceeds_supply'
                                      )) AS c_count,
                                      COUNT(*) FILTER (WHERE s.signal_type IN (
                                          'demand_surge','technology_adoption'
                                      )) AS d_count,
                                      COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase') AS capex_count,
                                      (ARRAY_AGG(s.context_text ORDER BY s.confidence DESC)
                                       FILTER (WHERE s.signal_type IN (
                                           'supply_bottleneck','inventory_drawdown',
                                           'capacity_shortage','demand_exceeds_supply'
                                       ) AND s.context_text IS NOT NULL))[1] AS best_quote,
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

    if c_count >= 5 and k_count >= 2:
        action_sentence = (
            f"The company has {c_count} supply constraint signals AND {k_count} capex expansion signals — "
            f"they see the demand and are investing to capture it. This is the highest-conviction setup: "
            f"constrained supplier actively expanding."
        )
    elif c_count >= 3 and k_count >= 1:
        action_sentence = (
            f"With {c_count} constraint signals and confirmed capex commitment, "
            f"this company is positioned as the SUPPLY SIDE of a structural shortage{component_str}."
        )
    elif c_count >= 2:
        action_sentence = (
            f"With {c_count} seller-perspective constraint signals (avg confidence {avg_confidence:.0%}), "
            f"the company shows early evidence of supply-side constraint advantage."
        )
    else:
        action_sentence = f"Shows {c_count} constraint signals with policy/demand tailwind support."

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

        tier1      = [r for r in results if r.investment_tier == "tier_1"]
        tier1_watch= [r for r in results if r.investment_tier == "tier_1_watch"]

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
        from_d = date(yr - 1, 1, 1)   # look back 1 year for signals

        # ─── Stage 1: Get NEW/ESCALATING themes ────────────────────────────
        focus_themes = pg.get_year_focus_analysis(yr, country)
        actionable   = [t for t in focus_themes
                        if t.get("focus_class") in ("new", "escalating", "no_prior")]

        stage1_out = []
        for t in actionable:
            # Get constraint components for this theme (what's physically constrained)
            components: list[dict] = []
            try:
                components = pg.get_constraint_components(
                    theme_id=t.get("id", 0) or 0,
                    from_date=from_d,
                    to_date=to_d,
                    top_n=5,
                )
            except Exception:
                pass
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

        if not actionable_slugs:
            return {
                "year": yr, "country": country,
                "stats": {"stage1_themes": 0},
                "stages": [], "final_shortlist": [],
            }

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
                cur.execute(
                    """SELECT
                           d.company,
                           COALESCE(NULLIF(d.ticker,''), d.company) AS ticker,
                           -- Count SELLER-perspective constraint signals
                           COUNT(*) FILTER (WHERE
                               s.signal_type = 'capacity_constraint_seller'
                               OR (s.signal_type IN (
                                       'supply_bottleneck','inventory_drawdown',
                                       'capacity_shortage','demand_exceeds_supply'
                                   )
                                   AND (
                                       -- Signals tagged seller by pipeline
                                       COALESCE(s.perspective,'neutral') = 'seller'
                                       -- OR context_text contains seller-language
                                       -- (for existing data before pipeline re-run)
                                       OR s.context_text ILIKE '%our capacity%'
                                       OR s.context_text ILIKE '%our backlog%'
                                       OR s.context_text ILIKE '%cannot meet demand%'
                                       OR s.context_text ILIKE '%can''t meet demand%'
                                       OR s.context_text ILIKE '%fully booked%'
                                       OR s.context_text ILIKE '%fully allocated%'
                                       OR s.context_text ILIKE '%sold out%'
                                       OR s.context_text ILIKE '%our lead time%'
                                       OR s.context_text ILIKE '%waiting list%'
                                       OR s.context_text ILIKE '%oversubscribed%'
                                       OR s.context_text ILIKE '%customers waiting%'
                                   )
                               )
                           )                                           AS c_count,
                           COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase')
                                                                        AS capex_count,
                           COUNT(*) FILTER (WHERE s.signal_type IN (
                               'demand_surge','capex_increase'
                           ))                                           AS d_count,
                           ROUND(AVG(s.confidence) FILTER (WHERE
                               s.signal_type = 'capacity_constraint_seller'
                               OR (s.signal_type IN ('supply_bottleneck','capacity_shortage')
                                   AND COALESCE(s.perspective,'neutral') = 'seller')
                               OR (s.signal_type IN ('supply_bottleneck','capacity_shortage')
                                   AND (s.context_text ILIKE '%our capacity%'
                                        OR s.context_text ILIKE '%our backlog%'
                                        OR s.context_text ILIKE '%fully booked%'
                                        OR s.context_text ILIKE '%cannot meet demand%'))
                           )::numeric, 3)                               AS avg_conf,
                           -- Best seller-perspective quote
                           (ARRAY_AGG(s.context_text ORDER BY s.confidence DESC)
                            FILTER (WHERE
                               (s.signal_type = 'capacity_constraint_seller'
                                OR (s.signal_type IN ('supply_bottleneck','capacity_shortage')
                                    AND (COALESCE(s.perspective,'neutral') = 'seller'
                                         OR s.context_text ILIKE '%our capacity%'
                                         OR s.context_text ILIKE '%backlog%'
                                         OR s.context_text ILIKE '%fully booked%')))
                               AND s.context_text IS NOT NULL
                               AND LENGTH(s.context_text) > 40))[1]    AS best_quote,
                           (ARRAY_AGG(d.filed_at ORDER BY s.confidence DESC)
                            FILTER (WHERE s.signal_type IN (
                                'capacity_constraint_seller','supply_bottleneck'
                            )))[1]::date                                AS best_quote_date,
                           (ARRAY_AGG(s.context_text ORDER BY s.confidence DESC)
                            FILTER (WHERE s.signal_type = 'capex_increase'
                              AND s.context_text IS NOT NULL))[1]       AS capex_quote,
                           MAX(d.filed_at)::date                        AS last_filing
                       FROM mg_signals s
                       JOIN mg_documents d ON d.id = s.document_id
                       WHERE d.country = %s
                         AND d.filed_at BETWEEN %s AND %s
                         AND d.company IS NOT NULL AND d.company != ''
                       GROUP BY d.company, COALESCE(NULLIF(d.ticker,''), d.company)
                       HAVING
                           -- Must have seller-perspective constraint signals
                           COUNT(*) FILTER (WHERE
                               s.signal_type = 'capacity_constraint_seller'
                               OR (s.signal_type IN (
                                       'supply_bottleneck','inventory_drawdown',
                                       'capacity_shortage'
                                   )
                                   AND (COALESCE(s.perspective,'neutral') = 'seller'
                                        OR s.context_text ILIKE '%our capacity%'
                                        OR s.context_text ILIKE '%our backlog%'
                                        OR s.context_text ILIKE '%cannot meet demand%'
                                        OR s.context_text ILIKE '%fully booked%'
                                        OR s.context_text ILIKE '%our lead time%'))
                           ) >= %s
                       ORDER BY c_count DESC, avg_conf DESC NULLS LAST
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
                        "avg_conf":     float(r["avg_conf"] or 0),
                        "best_quote":   (r["best_quote"] or "")[:300],
                        "best_quote_date": str(r["best_quote_date"] or ""),
                        "capex_quote":  (r["capex_quote"] or "")[:200],
                        "last_filing":  str(r["last_filing"] or ""),
                    }

                if not co_meta:
                    return {"year": yr, "country": country,
                            "stats": {"stage1_themes": len(stage1_out),
                                      "stage2_signal_companies": 0},
                            "stages": stage1_out, "final_shortlist": []}

                # 2b. Resolve theme IDs for theme-matching enrichment
                cur.execute(
                    "SELECT id, theme_name, theme_slug, conviction "
                    "FROM mg_themes WHERE theme_slug=ANY(%s) AND is_active=TRUE",
                    (actionable_slugs,)
                )
                theme_rows = {r["id"]: dict(r) for r in cur.fetchall()}

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

            # Theme quality
            theme_ids  = co_themes_map.get(name, set())
            best_theme = max(
                (t for t in stage1_out if t.get("theme_id") in theme_ids),
                key=lambda t: FOCUS_SCORE.get(t.get("focus",""),0) *
                              CONV_SCORE.get(t.get("conviction",""),0),
                default=None
            )
            focus_s = FOCUS_SCORE.get(best_theme.get("focus","") if best_theme else "", 0.3)
            conv_s  = CONV_SCORE.get(best_theme.get("conviction","") if best_theme else "", 0.3)

            # Rank score (0-100)
            rank_score = round(
                min(100, (
                    c_count * avg_conf * 30      # signal count × quality
                    + k_count * 15               # capex commitment
                    + d_count * 5                # demand confirmation
                    + focus_s * conv_s * 30      # theme quality
                    + len(theme_ids) * 5         # multi-theme overlap
                )), 1
            )

            n_themes = len(theme_ids)

            # ── World-class additions ─────────────────────────────────────────
            # Constraint cycle stage + exit signals + conviction tier
            # These are the 20% that makes this system world-class.

            # Check for supply easing / exit signals in the signal list
            has_supply_easing  = any(
                s.get("signal_type") in ("supply_easing","demand_slowdown","inventory_buildup")
                for s in (meta.get("quality_signals") or [])
            )
            has_realized_margin = any(
                s.get("signal_type") == "realized_margin_expansion"
                for s in (meta.get("quality_signals") or [])
            )

            # Theme first_detected date + quarters with signal
            theme_first_det = ""
            theme_quarters  = 0
            theme_momentum  = 0.0
            theme_accel     = 0.0
            if best_theme:
                theme_first_det = str(best_theme.get("first_detected","") or "")
                theme_quarters  = int(best_theme.get("this_snap_count") or 0)
                theme_momentum  = float(best_theme.get("this_avg_momentum") or 0)
                theme_accel     = float(best_theme.get("strength_delta") or 0)

            c_stage, stage_conf, time_horizon_m, conviction_tier, exit_triggers = (
                _compute_constraint_stage(
                    first_detected_str    = theme_first_det,
                    quarters_with_signal  = theme_quarters,
                    momentum_score        = theme_momentum,
                    signal_acceleration   = theme_accel / max(abs(theme_accel), 1) if theme_accel else 0,
                    has_realized_margin   = has_realized_margin,
                    has_supply_easing     = has_supply_easing,
                    c_count               = c_count,
                    k_count               = k_count,
                )
            )

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
                "theme":             best_theme["theme_name"] if best_theme else "",
                "theme_focus":       best_theme["focus"] if best_theme else "",
                "constraint_signals": c_count,
                "capex_signals":      k_count,
                "demand_signals":     d_count,
                "avg_confidence":    round(avg_conf, 3),
                "constrained_component": (
                    best_theme["constrained_components"][0]["component"]
                    if best_theme and best_theme.get("constrained_components") else ""
                ),
                "best_constraint_quote": (best_c.get("context_text","") or "")[:300] if best_c else "",
                "best_constraint_date":  str(best_c.get("filed_date","")) if best_c else "",
                "capex_quote":           (best_k.get("context_text","") or "")[:200] if best_k else "",
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
            })

        # Sort: Stage 1 (strongest alpha) → Stage 2 → Stage 3 → Stage 4 (weakest)
        # Within same stage: sort by rank_score DESC
        results.sort(key=lambda r: (
            r.get("constraint_stage", 3),   # lower stage = better
            -r["rank_score"],
        ))
        for i, r in enumerate(results):
            r["rank"] = i + 1

        stats = {
            "stage1_themes":          len(stage1_out),
            "stage2_signal_companies": len(co_meta),          # companies with signals this year
            "stage3_with_evidence":   len(co_meta),           # all signal companies have evidence
            "stage4_qualify":         len(results),
            "final_count":            min(len(results), 50),
        }

        return {
            "year":            yr,
            "country":         country,
            "period":          f"{from_d} → {to_d}",
            "stats":           stats,
            "constraint_regimes": stage1_out,
            "final_shortlist": results[:50],
        }

    except Exception as e:
        logging.error("investment_final_shortlist: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Final shortlist failed: {e}")


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
