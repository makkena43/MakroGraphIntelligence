"""Standalone test for all India ingest fetchers.

Runs each fetcher's discover() independently against live endpoints
using a historical start date so we know data should exist.

Usage:
    python tests/test_india_fetchers.py               # test all fetchers
    python tests/test_india_fetchers.py --source nse  # test one fetcher
    python tests/test_india_fetchers.py --download    # also download first 2 docs

Set start_date to any past date — default is 2024-01-01 so there
is guaranteed data on every source.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("india_fetcher_test")

_SEPARATOR = "=" * 65

_BASE_CFG = {
    "download_dir": str(ROOT / "data" / "raw"),
    "user_agent": "MakroGraph/0.2 (India Research Test)",
    "request_timeout_seconds": 30,
    "retry_attempts": 2,
    "retry_delay_seconds": 1,
    "max_results_per_run": 20,
    "api_delay_seconds": 1.2,
}

START_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)  # overridden by --days at runtime


# ─────────────────────────────────────────────────────────────────────────────
# Per-source test helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_nse(download: bool = False) -> dict:
    """Test NSE India corporate announcements fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: NSE India  (api.nseindia.com)")
    print(_SEPARATOR)

    from makrograph.fetcher.nse_fetcher import NSEFetcher

    cfg = {
        **_BASE_CFG,
        "announcement_type": "equities",
        "start_date": "2024-01-01",
        "api_delay_seconds": 1.2,
        "max_results_per_run": 20,
    }

    with NSEFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=START_DATE)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            print(f"  [{i+1}] {d.ticker:>10}  {(d.published_at or '').isoformat()[:10] if d.published_at else 'N/A':10}  {d.title[:70]}")

        result = {"source": "nse_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            print(f"\n  Downloading first 2 docs …")
            fetch_results, _ = fetcher.fetch_discovered_from_list(docs[:2])
            ok = sum(1 for r in fetch_results if r.success)
            print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
            result["downloaded"] = ok

    return result


def test_bse(download: bool = False) -> dict:
    """Test BSE India announcements fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: BSE India  (api.bseindia.com)")
    print(_SEPARATOR)

    from makrograph.fetcher.bse_fetcher import BSEFetcher

    cfg = {
        **_BASE_CFG,
        "start_date": "2024-01-01",
        "scrip_list": [],
        "api_delay_seconds": 0.5,
        "max_results_per_run": 20,
        "use_selenium": True,
        "selenium_headless": True,
    }

    with BSEFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=START_DATE)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            print(f"  [{i+1}] {d.ticker:>8}  {(d.published_at or '').isoformat()[:10] if d.published_at else 'N/A':10}  [{d.filing_type:20}]  {d.title[:55]}")

        result = {"source": "bse_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            print(f"\n  Downloading first 2 docs …")
            fetch_results, _ = fetcher.fetch_discovered_from_list(docs[:2])
            ok = sum(1 for r in fetch_results if r.success)
            print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
            result["downloaded"] = ok

    return result


def test_screener(download: bool = False) -> dict:
    """Test Screener.in annual-report / concall link fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: Screener.in  (www.screener.in)")
    print(_SEPARATOR)

    from makrograph.fetcher.screener_fetcher import ScreenerFetcher

    cfg = {
        **_BASE_CFG,
        "start_date": "2023-01-01",
        "symbol_list": ["INFY", "TCS", "RELIANCE"],
        "use_selenium": False,
        "api_delay_seconds": 1.5,
        "max_results_per_run": 20,
    }

    with ScreenerFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=None)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            print(f"  [{i+1}] {d.ticker:>10}  [{d.filing_type:25}]  {d.title[:50]}")

        result = {"source": "screener_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            pdfs = [d for d in docs if d.url.lower().endswith(".pdf")][:2]
            if pdfs:
                print(f"\n  Downloading {len(pdfs)} PDF(s) …")
                fetch_results, _ = fetcher.fetch_discovered_from_list(pdfs)
                ok = sum(1 for r in fetch_results if r.success)
                print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
                result["downloaded"] = ok

    return result


def test_pib(download: bool = False) -> dict:
    """Test PIB India press release RSS fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: PIB India  (pib.gov.in RSS)")
    print(_SEPARATOR)

    from makrograph.fetcher.pib_fetcher import PIBFetcher

    cfg = {
        **_BASE_CFG,
        "start_date": "2024-01-01",
        "api_delay_seconds": 0.5,
        "max_results_per_run": 20,
        "fetch_full_text": False,
        "keywords": [
            "PLI", "semiconductor", "railway", "defence", "EV",
            "solar", "infrastructure", "capex",
        ],
    }

    with PIBFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=START_DATE)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            dt_str = d.published_at.strftime("%Y-%m-%d") if d.published_at else "N/A"
            ministry = d.metadata.get("ministry", "")[:20]
            print(f"  [{i+1}] {dt_str:10}  [{ministry:20}]  {d.title[:55]}")

        result = {"source": "pib_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            print(f"\n  Downloading first 2 pages …")
            fetch_results, _ = fetcher.fetch_discovered_from_list(docs[:2])
            ok = sum(1 for r in fetch_results if r.success)
            print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
            result["downloaded"] = ok

    return result


def test_invest_india(download: bool = False) -> dict:
    """Test Invest India sector reports fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: Invest India  (investindia.gov.in)")
    print(_SEPARATOR)

    from makrograph.fetcher.invest_india_fetcher import InvestIndiaFetcher

    cfg = {
        **_BASE_CFG,
        "start_date": "2022-01-01",
        "sections": ["sector-reports", "investment-announcements"],
        "api_delay_seconds": 1.0,
        "max_results_per_run": 20,
        "keywords": [],
    }

    with InvestIndiaFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=None)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            section = d.metadata.get("section", "")
            print(f"  [{i+1}] [{section:25}]  [{d.doc_type:8}]  {d.title[:55]}")

        result = {"source": "invest_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            pdfs = [d for d in docs if d.url.lower().endswith(".pdf")][:2]
            if pdfs:
                print(f"\n  Downloading {len(pdfs)} PDF(s) …")
                fetch_results, _ = fetcher.fetch_discovered_from_list(pdfs)
                ok = sum(1 for r in fetch_results if r.success)
                print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
                result["downloaded"] = ok

    return result


def test_commerce_india(download: bool = False) -> dict:
    """Test Ministry of Commerce + DGFT fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: Commerce India + DGFT  (commerce.gov.in / dgft.gov.in)")
    print(_SEPARATOR)

    from makrograph.fetcher.commerce_india_fetcher import CommerceIndiaFetcher

    cfg = {
        **_BASE_CFG,
        "start_date": "2023-01-01",
        "sources": ["commerce", "dgft"],
        "api_delay_seconds": 1.0,
        "max_results_per_run": 20,
        "keywords": [],
    }

    with CommerceIndiaFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=None)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            src_page = d.metadata.get("source_page", "")[-40:]
            print(f"  [{i+1}] [{d.filing_type:22}]  {d.title[:55]}")
            print(f"         src: {src_page}")

        result = {"source": "commerce_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            pdfs = [d for d in docs if d.url.lower().endswith(".pdf")][:2]
            if pdfs:
                print(f"\n  Downloading {len(pdfs)} PDF(s) …")
                fetch_results, _ = fetcher.fetch_discovered_from_list(pdfs)
                ok = sum(1 for r in fetch_results if r.success)
                print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
                result["downloaded"] = ok

    return result


def test_sebi(download: bool = False) -> dict:
    """Test SEBI India circulars and press releases fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: SEBI India  (sebi.gov.in)")
    print(_SEPARATOR)

    from makrograph.fetcher.sebi_fetcher import SEBIFetcher

    cfg = {
        **_BASE_CFG,
        "start_date": "2024-01-01",
        "doc_types": ["press_release", "circular"],
        "api_delay_seconds": 1.0,
        "max_results_per_run": 20,
        "keywords": [],
    }

    with SEBIFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=START_DATE)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            dt_str = d.published_at.strftime("%Y-%m-%d") if d.published_at else "N/A"
            print(f"  [{i+1}] {dt_str:10}  [{d.filing_type:15}]  {d.title[:60]}")

        result = {"source": "sebi_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            pdfs = [d for d in docs if d.url.lower().endswith(".pdf")][:2]
            if pdfs:
                print(f"\n  Downloading {len(pdfs)} PDF(s) …")
                fetch_results, _ = fetcher.fetch_discovered_from_list(pdfs)
                ok = sum(1 for r in fetch_results if r.success)
                print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
                result["downloaded"] = ok

    return result


def test_rbi(download: bool = False) -> dict:
    """Test RBI India monetary policy and press releases RSS fetcher."""
    print(f"\n{_SEPARATOR}")
    print("SOURCE: RBI India  (rbi.org.in RSS)")
    print(_SEPARATOR)

    from makrograph.fetcher.rbi_fetcher import RBIFetcher

    cfg = {
        **_BASE_CFG,
        "start_date": "2024-01-01",
        "api_delay_seconds": 0.5,
        "max_results_per_run": 20,
        "fetch_full_text": False,
        "keywords": [
            "repo rate", "monetary policy", "inflation", "CPI",
            "forex", "liquidity", "credit", "NBFC", "interest rate", "MPC",
        ],
    }

    with RBIFetcher(cfg) as fetcher:
        t0 = time.time()
        docs = fetcher.discover(since=START_DATE)
        elapsed = round(time.time() - t0, 2)

        print(f"  Discovered: {len(docs)} documents in {elapsed}s")
        for i, d in enumerate(docs[:5]):
            dt_str = d.published_at.strftime("%Y-%m-%d") if d.published_at else "N/A"
            category = d.metadata.get("category", "")[:15]
            print(f"  [{i+1}] {dt_str:10}  [{d.filing_type:20}]  [{category:15}]  {d.title[:45]}")

        result = {"source": "rbi_india", "discovered": len(docs), "elapsed_s": elapsed, "ok": len(docs) >= 0}

        if download and docs:
            print(f"\n  Downloading first 2 pages …")
            fetch_results, _ = fetcher.fetch_discovered_from_list(docs[:2])
            ok = sum(1 for r in fetch_results if r.success)
            print(f"  Downloads: {ok}/{len(fetch_results)} succeeded")
            result["downloaded"] = ok

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_MAP = {
    "nse":      test_nse,
    "bse":      test_bse,
    "screener": test_screener,
    "pib":      test_pib,
    "invest":   test_invest_india,
    "commerce": test_commerce_india,
    "sebi":     test_sebi,
    "rbi":      test_rbi,
}


def main():
    parser = argparse.ArgumentParser(description="Test India ingest fetchers independently")
    parser.add_argument(
        "--source",
        choices=list(_SOURCE_MAP.keys()),
        default=None,
        help="Which fetcher to test (default: all)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Also download the first 2 documents from each source",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Use a start date of N days ago instead of the hardcoded 2024-01-01",
    )
    args = parser.parse_args()

    global START_DATE
    if args.days is not None:
        START_DATE = datetime.now(timezone.utc) - timedelta(days=args.days)
        logger.info(f"Using dynamic start date: {START_DATE.date()} ({args.days} days ago)")

    sources_to_test = [args.source] if args.source else list(_SOURCE_MAP.keys())

    print(f"\n{'#'*65}")
    print("# MakroGraph — India Fetcher Test Suite")
    print(f"# Start date filter: {START_DATE.date()}")
    print(f"# Download mode:     {'ON' if args.download else 'OFF (discover only)'}")
    print(f"{'#'*65}")

    all_results = []
    for src_key in sources_to_test:
        try:
            result = _SOURCE_MAP[src_key](download=args.download)
            all_results.append(result)
        except Exception as exc:
            logger.error(f"[{src_key}] FAILED: {exc}", exc_info=True)
            all_results.append({"source": src_key, "discovered": 0, "ok": False, "error": str(exc)})

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{_SEPARATOR}")
    print("SUMMARY")
    print(_SEPARATOR)
    total_docs = 0
    for r in all_results:
        status = "✅ OK" if r.get("ok") else "❌ FAIL"
        n = r.get("discovered", 0)
        t = r.get("elapsed_s", 0)
        err = f"  ERROR: {r['error']}" if r.get("error") else ""
        dl_info = f"  downloaded={r['downloaded']}" if "downloaded" in r else ""
        print(f"  {status}  {r['source']:<20}  discovered={n:>4}  elapsed={t:>5}s{dl_info}{err}")
        total_docs += n

    print(f"\n  Total documents discovered: {total_docs}")
    print(_SEPARATOR)


if __name__ == "__main__":
    main()
