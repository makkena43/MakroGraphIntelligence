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
    timeout = int(acfg.get("timeout_seconds", 120))
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
                                  s.signal_type, s.confidence, s.context_text, s.entity_text
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
                                  s.signal_type, s.confidence, s.context_text, s.entity_text
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
        raise HTTPException(status_code=500, detail=str(e))


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
    do_india_intelligence: bool = False   # Layers 1-10 India upstream intelligence
    do_themes: bool = True
    do_contradictions: bool = True
    do_pdf_fetch_india: bool = False
    pdf_fetch_workers: int = 6
    skip_neo4j: bool = False
    nlp_batch_size: int = 500
    fetch_mode: str = "selected"
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
