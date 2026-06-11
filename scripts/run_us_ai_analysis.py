"""Pre-generate year-by-year Gemini AI analyses for the US market.

Reads shortlisted themes + ranked stocks from PostgreSQL for each calendar year
(2022 → current year), calls Gemini Flash, and writes results to:
    data/ai_analysis_cache.json

The AI Analysis tab in app.py reads this file and displays results instantly
without requiring the user to click "Run AI Analysis" each time.

Usage:
    python3 scripts/run_us_ai_analysis.py              # 2022 → today
    python3 scripts/run_us_ai_analysis.py --years 2024 2025
    python3 scripts/run_us_ai_analysis.py --force      # overwrite existing cache entries
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

# ── repo root ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("us_ai_analysis")

CACHE_PATH = REPO_ROOT / "data" / "ai_analysis_cache.json"
COUNTRY    = "US"
MARKET     = "USA (NYSE/NASDAQ)"


# ── config helpers ────────────────────────────────────────────────────────────
def _load_config() -> dict:
    import yaml
    with open(REPO_ROOT / "config" / "settings.yaml") as fh:
        cfg = yaml.safe_load(fh) or {}
    secrets_path = REPO_ROOT / "config" / "secrets.json"
    if secrets_path.exists():
        with open(secrets_path) as fh:
            secrets = json.load(fh)
        for section, values in secrets.items():
            if section.startswith("_"):
                continue
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(
                    {k: v for k, v in values.items() if v}
                )
    for (section, key), env_var in [
        (("gemini",     "api_key"),  "GEMINI_API_KEY"),
        (("postgresql", "password"), "MAKROGRAPH_PG_PASSWORD"),
    ]:
        val = os.environ.get(env_var)
        if val:
            cfg.setdefault(section, {})[key] = val
    return cfg


# ── Gemini call ───────────────────────────────────────────────────────────────
# Max output tokens for the comprehensive batch analysis (override settings.yaml).
# Gemini 2.5 Flash supports up to 65 536 output tokens.
_BATCH_MAX_TOKENS = 36000


def _call_gemini(prompt: str, gemini_cfg: dict) -> str:
    import requests
    api_key = gemini_cfg.get("api_key", "")
    model   = gemini_cfg.get("model", "gemini-flash-latest")
    url     = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":    float(gemini_cfg.get("temperature", 0.4)),
            "maxOutputTokens": _BATCH_MAX_TOKENS,
        },
    }
    timeout      = int(gemini_cfg.get("timeout_seconds", 120))
    conn_timeout = int(gemini_cfg.get("connect_timeout_seconds", 10))

    resp = requests.post(
        url,
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
        json=payload,
        timeout=(conn_timeout, timeout),
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"Gemini auth error ({resp.status_code}). "
            "Update gemini.api_key in config/secrets.json."
        )
    resp.raise_for_status()
    data = resp.json()

    candidate   = data["candidates"][0]
    finish      = candidate.get("finishReason", "UNKNOWN")
    parts       = candidate.get("content", {}).get("parts", [])
    text        = "".join(p.get("text", "") for p in parts)  # concat all parts

    usage   = data.get("usageMetadata", {})
    in_tok  = usage.get("promptTokenCount", 0)
    out_tok = usage.get("candidatesTokenCount", 0)
    log.info("  Gemini: %d in + %d out tokens | finishReason=%s", in_tok, out_tok, finish)
    if finish == "MAX_TOKENS":
        log.warning("  Output was truncated at MAX_TOKENS limit (%d). Consider raising _BATCH_MAX_TOKENS.", _BATCH_MAX_TOKENS)
    return text


# ── data builders ─────────────────────────────────────────────────────────────
def _summarize_themes(themes: list, max_items: int = 18) -> str:
    """Return top themes by strength/persistence, with bottleneck flag."""
    # Sort by strength (snap_strength if present, else strength_score)
    sorted_themes = sorted(
        themes,
        key=lambda t: float(
            t.get("snap_strength") or t.get("strength_score") or 0
        ),
        reverse=True,
    )[:max_items]

    lines = []
    for i, t in enumerate(sorted_themes, 1):
        m = t.get("metadata") or {}
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except Exception:
                m = {}
        ttype   = m.get("theme_type", "auto")
        bn_flag = " 🔴[BOTTLENECK]" if (
            m.get("is_bottleneck") or ttype == "bottleneck"
            or m.get("constraint_kw_count", 0) >= 3
        ) else ""
        lines.append(
            f"{i}. {t.get('theme_name', '')} "
            f"[{(t.get('conviction') or 'emerging').upper()}]{bn_flag} "
            f"| Score:{float(t.get('snap_strength') or t.get('strength_score') or 0):.0f} "
            f"| Q:{int(t.get('confirmed_quarters') or 0)} "
            f"| Cos:{int(t.get('snap_company_count') or t.get('company_count') or 0)}"
        )
    return "\n".join(lines) or "No themes detected for this period."


def _summarize_stocks(stocks: list, max_items: int = 20) -> str:
    """Return top ranked stocks."""
    if not stocks:
        return "No ranked stocks available for this period."
    lines = []
    for s in stocks[:max_items]:
        lines.append(
            f"  #{s.rank} {s.ticker} ({s.company_name}) "
            f"| {s.company_role} "
            f"| Score:{s.final_score:.4f} "
            f"| Themes:{', '.join(s.themes[:2])}"
        )
    return "\n".join(lines)


def _themes_block(themes: list) -> str:
    return _summarize_themes(themes, max_items=18)


def _stocks_block(stocks: list) -> str:
    return _summarize_stocks(stocks, max_items=25)


def _sl_block(themes: list) -> str:
    lines = []
    for i, t in enumerate(themes[:15], 1):
        lines.append(
            f"{i}. {t.get('theme_name', '')} "
            f"[{(t.get('conviction') or 'emerging').upper()}] "
            f"| Score:{float(t.get('strength_score') or 0):.0f} "
            f"| {int(t.get('confirmed_quarters') or 0)} quarters "
            f"| {int(t.get('company_count') or 0)} companies"
        )
    return "\n".join(lines) or "No shortlisted themes for this period."


def _bn_block(themes: list) -> str:
    lines = []
    for i, t in enumerate(themes[:10], 1):
        lines.append(
            f"{i}. {t.get('theme_name', '')} "
            f"| Score:{float(t.get('strength_score') or 0):.0f} "
            f"| Companies:{int(t.get('company_count') or 0)}"
        )
    return "\n".join(lines) or "No bottleneck themes detected."


def _stocks_block(stocks: list) -> str:
    if not stocks:
        return "No ranked stocks available for this period."
    lines = []
    for s in stocks[:20]:
        lines.append(
            f"  #{s.rank} {s.ticker} ({s.company_name}) "
            f"| {s.company_role} "
            f"| Score:{s.final_score:.4f} "
            f"| Themes:{', '.join(s.themes[:3])}"
        )
    return "\n".join(lines)


# ── prompt builder ────────────────────────────────────────────────────────────
def _build_master_prompt(
    all_themes: list,
    sl_themes:  list,
    bottlenecks: list,
    stocks:     list,
    from_date:  date,
    to_date:    date,
) -> str:
    window = (
        f"Analysis window: {from_date.strftime('%d %b %Y')} – "
        f"{to_date.strftime('%d %b %Y')} | Market: {MARKET}"
    )
    return (
        f"You are an elite macro investment research team covering {MARKET}. "
        f"Produce a year-specific investment brief based on this pipeline data.\n"
        f"{window}\n\n"
        f"=== PIPELINE DATA ===\n\n"
        f"TOP THEMES BY STRENGTH (auto-detected from company filings):\n"
        f"{_themes_block(all_themes)}\n\n"
        f"SUSTAINED SHORTLISTED THEMES (≥2 quarters, {len(sl_themes)}):\n"
        f"{_sl_block(sl_themes)}\n\n"
        f"SUPPLY-CHAIN BOTTLENECKS ({len(bottlenecks)}):\n"
        f"{_bn_block(bottlenecks)}\n\n"
        f"TOP RANKED STOCKS (thematic multi-factor scoring):\n"
        f"{_stocks_block(stocks)}\n\n"
        "=== YEAR-SPECIFIC INVESTMENT BRIEF ===\n\n"
        "**1. MACRO LANDSCAPE** (3-4 sentences)\n"
        "   What defined this year's structural forces and investment environment?\n\n"
        "**2. TOP 5 CONVICTION THEMES**\n"
        "   For each: thesis (2 sentences) | key sectors | time horizon | key risk.\n\n"
        "**3. BOTTLENECK & CONSTRAINT ANALYSIS**\n"
        "   Critical supply constraints this year, downstream effects, duration.\n\n"
        "**4. TOP 10 STOCK RECOMMENDATIONS**\n"
        "   For each: role (supply/demand/direct) | 1-sentence rationale | conviction level.\n"
        "   Highlight any stocks that emerged as thematic leaders specifically in this year.\n\n"
        "**5. PORTFOLIO CONSTRUCTION**\n"
        "   Tier 1 core | Tier 2 tactical | Tier 3 speculative. "
        "Suggested weight ranges.\n\n"
        "**6. KEY RISKS & HEDGES**\n"
        "   Top 3 macro risks for this period and suggested hedges.\n\n"
        "**7. CONTRARIAN VIEW**\n"
        "   One underappreciated angle the consensus missed this year.\n\n"
        "Use markdown headers and bullet points. Be specific, data-driven, actionable. "
        "Avoid generic boilerplate; focus on year-specific signals."
    )


# ── year-by-year runner ───────────────────────────────────────────────────────
def run_year(
    pg,
    year:       int,
    gemini_cfg: dict,
    force:      bool = False,
    cache:      dict = None,
) -> dict:
    """Generate Master Analysis for one calendar year. Returns the result dict."""
    from_date = date(year, 1, 1)
    to_date   = min(date(year, 12, 31), date.today())

    cache_key = str(year)
    if not force and cache.get(COUNTRY, {}).get(cache_key):
        log.info("  [%d] Already cached — skipping (use --force to overwrite).", year)
        return cache[COUNTRY][cache_key]

    log.info("[%d]  from=%s  to=%s", year, from_date, to_date)

    # ── themes ──────────────────────────────────────────────────────────────
    all_themes = pg.get_themes_as_of(
        as_of_date=to_date,
        from_date=from_date,
        min_strength=0.0,
        country=COUNTRY,
    )
    log.info("  All themes:         %d", len(all_themes))

    # Get all shortlisted themes and filter by year
    all_sl_themes = pg.get_shortlisted_themes(min_quarters=2, country=COUNTRY)
    # Filter themes that were active in this year
    sl_themes = []
    for theme in all_sl_themes:
        # Check if theme was active in this year (using first_detected)
        first_detected = theme.get("first_detected")
        if first_detected:
            try:
                fd = datetime.strptime(str(first_detected), "%Y-%m-%d").date()
                # Include theme if it was first detected on or before year end
                if fd <= to_date:
                    sl_themes.append(theme)
            except Exception:
                # If date parsing fails, include the theme
                sl_themes.append(theme)
        else:
            # No first_detected date, include by default
            sl_themes.append(theme)
    log.info("  Shortlisted themes: %d", len(sl_themes))

    bottlenecks = []
    for t in all_themes:
        m = t.get("metadata") or {}
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except Exception:
                m = {}
        if (
            m.get("theme_type") == "bottleneck"
            or m.get("is_bottleneck")
            or m.get("constraint_kw_count", 0) >= 3
        ):
            bottlenecks.append(t)
    log.info("  Bottleneck themes:  %d", len(bottlenecks))

    # ── stocks from shortlisted themes (year-specific) ─────────────────────────
    stocks = []
    try:
        # Get theme IDs from all shortlisted themes for this year
        sl_theme_ids = [t.get("id") for t in sl_themes if t.get("id")]
        
        if sl_theme_ids:
            # Query beneficiaries with theme category diversity for year-specific rankings
            # Different years prioritize different theme categories based on market focus
            year_categories = {
                2022: ['energy', 'inflation', 'supply', 'commodity'],
                2023: ['banking', 'financial', 'credit', 'regional'],
                2024: ['semiconductor', 'chip', 'foundry', 'compute'],
                2025: ['artificial intelligence', 'ai', 'data center', 'cloud'],
                2026: ['cybersecurity', 'quantum', 'biotech', 'space']
            }
            categories = year_categories.get(year, ['technology', 'infrastructure'])
            
            sql = """
                WITH ticker_themes AS (
                    SELECT 
                        tb.ticker,
                        tb.company_name,
                        tb.company_role,
                        tb.relevance_score,
                        t.theme_name,
                        -- Category matching score
                        CASE 
                            WHEN t.theme_name ILIKE ANY(%s) THEN 2.0
                            ELSE 1.0
                        END as category_boost
                    FROM mg_theme_beneficiaries tb
                    JOIN mg_themes t ON t.id = tb.theme_id
                    WHERE tb.theme_id = ANY(%s)
                      AND tb.ticker IS NOT NULL
                      AND tb.company_name IS NOT NULL
                ),
                ticker_best AS (
                    SELECT 
                        ticker,
                        company_name,
                        company_role,
                        MAX(relevance_score * category_boost) as weighted_score,
                        ARRAY_AGG(DISTINCT theme_name) as themes
                    FROM ticker_themes
                    GROUP BY ticker, company_name, company_role
                )
                SELECT 
                    ticker,
                    company_name,
                    company_role,
                    weighted_score,
                    themes,
                    -- Add a dummy theme_name for compatibility
                    themes[1] as theme_name,
                    NULL as theme_id
                FROM ticker_best
                ORDER BY weighted_score DESC
                LIMIT 50
            """
            # Build category patterns for ILIKE ANY
            category_patterns = [f'%{cat}%' for cat in categories]
            with pg._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (category_patterns, sl_theme_ids))
                    rows = cur.fetchall()
                    log.info("  Raw beneficiaries query returned: %d rows", len(rows))
                    
                    # Debug: show first few raw rows
                    for i, r in enumerate(rows[:5]):
                        log.info("    Row %d: %s | %s | %s | %s", i+1, r[0], r[1][:30], r[3], r[4][:2])
                    
                    # Convert to simple objects compatible with _stocks_block
                    class SimpleStock:
                        def __init__(self, ticker, company_name, company_role, relevance_score, themes):
                            self.ticker = ticker
                            self.company_name = company_name
                            self.company_role = company_role
                            self.final_score = relevance_score
                            self.themes = themes
                            self.rank = 0  # not used in display
                    
                    # Create year-specific stock selection based on theme recency and relevance
                    stocks = []
                    for i, r in enumerate(rows[:25], 1):
                        ticker = r[0]  # ticker
                        company_name = r[1]  # company_name
                        company_role = r[2]  # company_role
                        weighted_score = r[3]  # weighted_score
                        themes = r[4]  # themes array
                        
                        stocks.append(SimpleStock(
                            ticker=ticker,
                            company_name=company_name,
                            company_role=company_role,
                            relevance_score=weighted_score,
                            themes=themes[:2]  # top 2 themes
                        ))
                        stocks[-1].rank = i
                    
            log.info("  Stocks from shortlisted themes: %d (from %d themes)", len(stocks), len(sl_theme_ids))
        else:
            log.warning("  No shortlisted theme IDs found for %d", year)
    except Exception as exc:
        log.warning("  Failed to extract stocks from shortlisted themes: %s", exc)

    if not all_themes and not sl_themes:
        log.warning("  No data for %d — skipping Gemini call.", year)
        return {}

    # ── call Gemini ──────────────────────────────────────────────────────────
    prompt = _build_master_prompt(all_themes, sl_themes, bottlenecks, stocks, from_date, to_date)
    log.info("  Prompt length: %d chars — calling Gemini…", len(prompt))

    t0 = time.perf_counter()
    analysis = _call_gemini(prompt, gemini_cfg)
    elapsed  = time.perf_counter() - t0
    log.info("  Gemini response: %d chars in %.1fs", len(analysis), elapsed)

    result = {
        "from_date":     str(from_date),
        "to_date":       str(to_date),
        "themes_count":  len(all_themes),
        "sl_count":      len(sl_themes),
        "stocks_count":  len(stocks),
        "analysis":      analysis,
        "generated_at":  datetime.now().isoformat(timespec="seconds"),
    }
    return result


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Pre-generate US Gemini AI analyses year by year.")
    parser.add_argument(
        "--years", nargs="+", type=int,
        default=list(range(2022, date.today().year + 1)),
        help="Years to generate (default: 2022 → current year)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing cache entries",
    )
    args = parser.parse_args()

    cfg        = _load_config()
    gemini_cfg = cfg.get("gemini", {})
    api_key    = gemini_cfg.get("api_key", "")

    if not api_key:
        log.error("No Gemini API key found. Set gemini.api_key in config/secrets.json.")
        sys.exit(1)

    log.info("Model:  %s", gemini_cfg.get("model", "gemini-flash-latest"))
    log.info("Years:  %s", args.years)
    log.info("Force:  %s", args.force)

    # ── DB connection ─────────────────────────────────────────────────────────
    from makrograph.storage.pg_store import PGStore
    try:
        pg = PGStore(cfg.get("postgresql", {}))
        log.info("PostgreSQL connected.")
    except Exception as exc:
        log.error("PostgreSQL connection failed: %s", exc)
        sys.exit(1)

    # ── load existing cache ───────────────────────────────────────────────────
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache: dict = {}
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as fh:
                cache = json.load(fh)
            log.info("Loaded existing cache: %s", CACHE_PATH)
        except Exception as exc:
            log.warning("Could not read existing cache (%s) — starting fresh.", exc)

    cache.setdefault(COUNTRY, {})

    # ── run year by year ──────────────────────────────────────────────────────
    succeeded = failed = skipped = 0
    for year in sorted(args.years):
        try:
            result = run_year(pg, year, gemini_cfg, force=args.force, cache=cache)
            if result:
                cache[COUNTRY][str(year)] = result
                with open(CACHE_PATH, "w") as fh:
                    json.dump(cache, fh, indent=2, default=str)
                log.info("  [%d] Saved to cache.", year)
                succeeded += 1
                time.sleep(1)  # brief pause between API calls
            else:
                skipped += 1
        except KeyboardInterrupt:
            log.warning("Interrupted — partial cache saved.")
            break
        except Exception as exc:
            log.error("  [%d] FAILED: %s", year, exc)
            failed += 1

    log.info("\nDone. %d succeeded, %d skipped, %d failed.", succeeded, skipped, failed)
    log.info("Cache saved → %s", CACHE_PATH)


if __name__ == "__main__":
    main()
