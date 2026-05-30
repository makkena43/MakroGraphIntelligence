"""Quick smoke-test for all India fetchers.

Tests each fetcher for a 5-day window (2024-01-15 to 2024-01-19).
For each fetcher, reports:
  - How many documents were discovered
  - Sample titles / URLs (first 3)
  - Any errors encountered
  - Whether the fallback path was hit (geo-blocked, JS-protected, etc.)

Usage:
    cd /Users/makkenasrinivas/PycharmProjects/MakroGraphIntelligence
    .venv/bin/python test_india_fetchers.py
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("india_test")

# ── Test window: 5 days in January 2024 ────────────────────────────────────
# Using a historical date so sites with start_date filtering can still surface docs.
WINDOW_SINCE = datetime(2024, 1, 14, tzinfo=timezone.utc)   # exclusive lower bound
WINDOW_END   = datetime(2024, 1, 19, tzinfo=timezone.utc)   # inclusive upper bound

# For fetchers with 'start_date' config, set it to WINDOW_SINCE.
# We pass `since=WINDOW_SINCE` to discover() and note doc count / sample titles.

COMMON_OVERRIDES = {
    "start_date":        "2024-01-14",
    "max_results_per_run": 50,       # keep the test fast
    "api_delay_seconds": 0.3,
    "timeout_seconds":   15,
}


def run_fetcher(name: str, fetcher_cls, extra_cfg: dict = None) -> dict:
    cfg = {**COMMON_OVERRIDES, **(extra_cfg or {})}
    result = {
        "fetcher": name,
        "docs":    0,
        "samples": [],
        "error":   None,
        "fallback": False,
    }
    try:
        with fetcher_cls(cfg) as f:
            docs = f.discover(since=WINDOW_SINCE)

        result["docs"] = len(docs)
        for d in docs[:3]:
            result["samples"].append({
                "title": (d.title or "")[:100],
                "url":   (d.url or "")[:120],
                "pub":   d.published_at.date().isoformat() if d.published_at else "—",
                "type":  d.filing_type,
                "src":   d.metadata.get("source", ""),
            })

        # Detect fallback: company_info docs come from NSE CSV fallback;
        # source_csv_fallback or scrip_list_fallback in BSE metadata.
        fallback_indicators = ["equity_csv_fallback", "scrip_list_fallback", "company_info"]
        for d in docs[:10]:
            if (d.doc_type in fallback_indicators
                    or d.metadata.get("source") in fallback_indicators
                    or any(k in (d.metadata or {}).values() for k in fallback_indicators)):
                result["fallback"] = True
                break

    except Exception as exc:
        result["error"] = str(exc)
        logger.error(f"[{name}] FAILED: {exc}", exc_info=True)

    return result


def print_result(r: dict):
    status = "✅" if r["docs"] > 0 else ("⚠️ " if r["fallback"] else "❌")
    fallback_note = "  [FALLBACK]" if r["fallback"] else ""
    err_note      = f"  ERROR: {r['error']}" if r["error"] else ""
    print(f"\n{'─'*60}")
    print(f"  {status} {r['fetcher']:<30}  {r['docs']} docs{fallback_note}{err_note}")
    for s in r["samples"]:
        pub = f"[{s['pub']}]" if s["pub"] != "—" else "[date?]"
        print(f"     {pub} {s['type']:<22} {s['title'][:75]}")
        print(f"            {s['url'][:95]}")


def main():
    from makrograph.fetcher.nse_fetcher    import NSEFetcher
    from makrograph.fetcher.bse_fetcher    import BSEFetcher
    from makrograph.fetcher.rbi_fetcher    import RBIFetcher
    from makrograph.fetcher.sebi_fetcher   import SEBIFetcher
    from makrograph.fetcher.pib_fetcher    import PIBFetcher
    from makrograph.fetcher.invest_india_fetcher  import InvestIndiaFetcher
    from makrograph.fetcher.commerce_india_fetcher import CommerceIndiaFetcher
    from makrograph.fetcher.screener_fetcher import ScreenerFetcher

    print("=" * 60)
    print("  INDIA FETCHER SMOKE TEST")
    print(f"  Window : 2024-01-15 → 2024-01-19")
    print(f"  (since={WINDOW_SINCE.date()}, max_results=50 per fetcher)")
    print("=" * 60)

    results = []

    # 1. NSE ─────────────────────────────────────────────────────────────────
    results.append(run_fetcher("NSEFetcher", NSEFetcher, {
        "announcement_type": "equities",
        "symbol_list": ["INFY", "TCS", "RELIANCE", "HDFCBANK"],
        "fetch_company_info": False,
    }))

    # 2. BSE ─────────────────────────────────────────────────────────────────
    results.append(run_fetcher("BSEFetcher", BSEFetcher, {
        "scrip_list": [],
        "use_selenium": False,   # no browser in CI; will hit scrip-list fallback
        "selenium_headless": True,
        "end_date": "2024-01-19",
    }))

    # 3. RBI ─────────────────────────────────────────────────────────────────
    results.append(run_fetcher("RBIFetcher", RBIFetcher, {
        "fetch_full_text": False,
        "keywords": ["repo rate", "monetary policy", "inflation", "CPI",
                     "interest rate", "MPC", "rupee", "liquidity", "credit"],
    }))

    # 4. SEBI ────────────────────────────────────────────────────────────────
    results.append(run_fetcher("SEBIFetcher", SEBIFetcher, {
        "doc_types": ["press_release", "circular"],
        "keywords": [],   # empty = all
    }))

    # 5. PIB ─────────────────────────────────────────────────────────────────
    results.append(run_fetcher("PIBFetcher", PIBFetcher, {
        "fetch_full_text": False,   # skip body fetch in test (faster)
        "keywords": ["PLI", "semiconductor", "railway", "solar", "capex",
                     "infrastructure", "5G", "EV", "defence"],
        "rss_feeds": ["https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"],
    }))

    # 6. Invest India ─────────────────────────────────────────────────────────
    results.append(run_fetcher("InvestIndiaFetcher", InvestIndiaFetcher, {
        "sections": ["sector-reports", "brochures"],
        "keywords": [],
        "max_results_per_run": 30,
    }))

    # 7. Commerce India / DGFT ────────────────────────────────────────────────
    results.append(run_fetcher("CommerceIndiaFetcher", CommerceIndiaFetcher, {
        "sources": ["dgft"],
        "keywords": [],   # empty = all
    }))

    # 8. Screener.in ──────────────────────────────────────────────────────────
    results.append(run_fetcher("ScreenerFetcher", ScreenerFetcher, {
        "symbol_list": ["INFY", "TCS", "RELIANCE", "HDFCBANK", "WIPRO"],
        "use_selenium": False,
        "extract_peers": False,
    }))

    # ── Summary ──────────────────────────────────────────────────────────────
    for r in results:
        print_result(r)

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    total_docs  = sum(r["docs"] for r in results)
    ok_count    = sum(1 for r in results if r["docs"] > 0)
    fallback_n  = sum(1 for r in results if r["fallback"])
    error_n     = sum(1 for r in results if r["error"])

    for r in results:
        icon = "✅" if r["docs"] > 0 else ("⚠️" if r["fallback"] else "❌")
        fb   = " (fallback)" if r["fallback"] else ""
        er   = f" ERROR: {r['error'][:60]}" if r["error"] else ""
        print(f"  {icon}  {r['fetcher']:<30}  {r['docs']:>4} docs{fb}{er}")

    print(f"\n  Total docs discovered : {total_docs}")
    print(f"  Fetchers with docs    : {ok_count}/8")
    print(f"  Fetchers using fallback: {fallback_n}/8")
    print(f"  Fetchers with errors  : {error_n}/8")
    print()


if __name__ == "__main__":
    main()
