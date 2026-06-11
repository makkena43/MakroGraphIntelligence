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
    yield
    logging.info("MakroGraph API shutting down")


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


@app.get("/api/themes/shortlisted")
def get_shortlisted(country: str = "US", min_quarters: int = 3) -> list[dict]:
    pg = get_pg()
    if not pg:
        return []
    try:
        return pg.get_shortlisted_themes(min_quarters=min_quarters, country=country)
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

                # ── Dynamic signal re-scoring ──────────────────────────────────
                # Extract the primary entity keyword from each chain name and
                # look up actual signal counts in the date window.
                # Chain names follow the pattern: "{Entity} Demand → ..." or
                # "{Entity} Adoption → ..." — first word(s) before " Demand" / " Adoption".
                import re as _re, math as _math

                def _chain_keywords(chain_name: str) -> list[str]:
                    """Extract the driving entity name from a chain name."""
                    name = chain_name or ""
                    # Strip common suffixes to get the core entity
                    for sep in [" Demand →", " Adoption →", " Demand Surge →"]:
                        if sep in name:
                            return [name.split(sep)[0].strip().lower()]
                    # Fallback: first segment before →
                    parts = name.split("→")
                    return [parts[0].strip().lower()] if parts else []

                # Collect all unique keywords across chains
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
                                       COUNT(DISTINCT d.company) AS cos,
                                       COUNT(*) AS sigs
                                FROM mg_signals s
                                JOIN mg_documents d ON d.id = s.document_id
                                JOIN mg_document_entities de ON de.document_id = s.document_id
                                JOIN mg_entities e ON e.id = de.entity_id
                                WHERE d.country = %s
                                  AND d.filed_at BETWEEN %s AND %s
                                  AND s.signal_type IN (
                                      'supply_bottleneck','demand_surge',
                                      'capex_increase','technology_adoption'
                                  )
                                  AND lower(e.canonical_name) IN ({placeholders})
                                GROUP BY lower(e.canonical_name)""",
                            [country, _from, _as_of] + list(all_keywords),
                        )
                        for r in cur.fetchall():
                            entity_signal_counts[r["ename"]] = {
                                "cos": int(r["cos"] or 0),
                                "sigs": int(r["sigs"] or 0),
                            }
                    except Exception as _se:
                        logging.warning("causal-chains entity scoring: %s", _se)

                for row in rows:
                    keywords = _chain_keywords(row.get("chain_name", ""))
                    total_cos  = sum(entity_signal_counts.get(k, {}).get("cos",  0) for k in keywords)
                    total_sigs = sum(entity_signal_counts.get(k, {}).get("sigs", 0) for k in keywords)

                    co_score  = min(1.0, _math.log1p(total_cos)  / _math.log1p(600))
                    sig_score = min(1.0, _math.log1p(total_sigs) / _math.log1p(5000))
                    raw_score = co_score * 0.5 + sig_score * 0.5
                    new_score = max(20.0, round(20.0 + raw_score * 80.0, 1))
                    row["activation_score"]      = new_score
                    row["signal_hits_in_window"] = total_sigs
                    row["companies_in_window"]   = total_cos

                rows.sort(key=lambda r: -r["activation_score"])

                # Strip raw links JSON from response (not needed by UI)
                for row in rows:
                    row.pop("links", None)
                    row.pop("chain_id", None)

                return rows
    except Exception as e:
        logging.error("get_causal_chains: %s", e)
        return []


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


@app.get("/api/india/rankings")
def get_india_rankings(
    as_of: str = None,
    lookback_days: int = 365,
    top_n: int = 50,
) -> dict:
    """India-specific ranking using Theme→Bottleneck→Product→Supplier→Rank formula.

    India SupplierQ: 35% ProductRelevance + 25% CapacityExpansion +
                     20% MarketShare + 10% OrderBook + 10% ConstraintExposure
    India Score:     30% ThemeCQ + 25% ProductRelevance + 20% SupplierQ +
                     15% ChainDistance + 10% CapacityExpansion
    Hard Rule: ProductRelevance=0 → score × 0.10 penalty
    """
    pg = get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="DB not available")
    try:
        from makrograph.india.india_ranking_engine import IndiaRankingEngine
        _as_of = date.fromisoformat(as_of) if as_of else date.today()
        engine = IndiaRankingEngine(pg)
        result = engine.run(as_of_date=_as_of, lookback_days=lookback_days, top_n=top_n)
        return {
            "as_of_date":        str(result.as_of_date),
            "themes_processed":  result.themes_processed,
            "companies_ranked":  result.companies_ranked,
            "stocks": [
                {
                    "rank":                    s.rank,
                    "company":                 s.company,
                    "ticker":                  s.ticker,
                    "theme_name":              s.theme_name,
                    "constrained_product":     s.constrained_product,
                    "role":                    s.role,
                    "chain_distance":          s.chain_distance,
                    "product_relevance":       s.product_relevance,
                    "capacity_expansion":      s.capacity_expansion_score,
                    "market_share":            s.market_share_score,
                    "order_book":              s.order_book_score,
                    "constraint_exposure":     s.constraint_exposure_score,
                    "supplier_q":              s.supplier_q,
                    "theme_cq":                s.theme_cq,
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
    def __init__(self, q: asyncio.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s", "%H:%M:%S"
        ))

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except asyncio.QueueFull:
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
        handler = _QueueHandler.__new__(_QueueHandler)
        logging.Handler.__init__(handler)
        handler.q = q
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s", "%H:%M:%S"
        ))
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
                msg = await asyncio.wait_for(q.get(), timeout=120)
            except asyncio.TimeoutError:
                yield "data: [TIMEOUT] No activity for 120s\n\n"
                break
            if msg == "__END__":
                yield "data: [DONE]\n\n"
                break
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
