#!/usr/bin/env python3
"""NSE / BSE Price Data & Screener Fundamentals Fetcher.

Fetches daily OHLCV bhavcopy data from NSE and BSE, plus screener.in
fundamentals, and stores them in the makrograph PostgreSQL database.

Modes
-----
  daily          Fetch the last N days of NSE + BSE bhavcopy (default: 5 days).
  historical     Fetch NSE + BSE bhavcopy for a specified date range.
  copy-from-algo Copy existing price data from the Algo_Test (MDsquare) PG database
                 directly into makrograph — fastest way to seed historical data.
  fundamentals   Fetch screener.in fundamentals for a comma-separated list of symbols.

Usage
-----
  # Daily incremental (last 5 days)
  python scripts/fetch_price_data.py --mode daily

  # Historical backfill (slow — fetches from NSE/BSE APIs)
  python scripts/fetch_price_data.py --mode historical --start 2019-01-01 --end 2024-12-31

  # Historical seed from Algo_Test DB (fast — direct PG-to-PG copy)
  python scripts/fetch_price_data.py --mode copy-from-algo

  # Screener fundamentals for specific symbols
  python scripts/fetch_price_data.py --mode fundamentals --symbols RELIANCE,TCS,INFY,HDFCBANK

  # Full rebuild: tables + copy history + fundamentals
  python scripts/fetch_price_data.py --mode copy-from-algo --symbols RELIANCE,TCS,INFY
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import yaml
import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("fetch_price_data")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    cfg_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(cfg_path) as fh:
        return yaml.safe_load(fh)


def _pg_config(settings: dict) -> dict:
    pg = settings.get("postgresql", {})
    # Allow env-var override of password
    password = os.getenv("MAKROGRAPH_PG_PASSWORD", pg.get("password", ""))
    return {
        "host": pg.get("host", "localhost"),
        "port": pg.get("port", 5432),
        "dbname": pg.get("dbname", "makrograph"),
        "user": pg.get("user", "postgres"),
        "password": password,
    }


def _algo_test_config() -> dict:
    """Return connection config for the Algo_Test (MDsquare) PG database."""
    return {
        "host": os.getenv("ALGO_TEST_PG_HOST", "localhost"),
        "port": int(os.getenv("ALGO_TEST_PG_PORT", "5432")),
        "dbname": os.getenv("ALGO_TEST_PG_DBNAME", "Algo_Test"),
        "user": os.getenv("ALGO_TEST_PG_USER", "postgres"),
        "password": os.getenv("ALGO_TEST_PG_PASSWORD", "mak43"),
    }

# ---------------------------------------------------------------------------
# Ensure tables exist
# ---------------------------------------------------------------------------

def ensure_tables(pg_cfg: dict) -> None:
    """Create price tables in makrograph if they don't exist yet."""
    from makrograph.fetcher.nse_price_fetcher import NSEPriceFetcher
    from makrograph.fetcher.bse_price_fetcher import BSEPriceFetcher
    from makrograph.fetcher.screener_fundamentals_fetcher import ScreenerFundamentalsFetcher

    NSEPriceFetcher(pg_cfg).ensure_table()
    BSEPriceFetcher(pg_cfg).ensure_table()
    ScreenerFundamentalsFetcher(pg_cfg).ensure_table()
    logger.info("All price tables ensured in makrograph")

# ---------------------------------------------------------------------------
# Mode: daily
# ---------------------------------------------------------------------------

def run_daily(pg_cfg: dict, days_back: int = 5) -> None:
    """Fetch the last ``days_back`` calendar days for NSE and BSE."""
    from makrograph.fetcher.nse_price_fetcher import NSEPriceFetcher
    from makrograph.fetcher.bse_price_fetcher import BSEPriceFetcher

    logger.info("=== NSE daily fetch (last %d days) ===", days_back)
    nse_total = NSEPriceFetcher(pg_cfg).fetch_latest(days_back)
    logger.info("NSE: %d rows inserted/updated", nse_total)

    logger.info("=== BSE daily fetch (last %d days) ===", days_back)
    bse_total = BSEPriceFetcher(pg_cfg).fetch_latest(days_back)
    logger.info("BSE: %d rows inserted/updated", bse_total)

# ---------------------------------------------------------------------------
# Mode: historical
# ---------------------------------------------------------------------------

def run_historical(pg_cfg: dict, start: date, end: date, exchange: str = "both") -> None:
    """Fetch NSE/BSE bhavcopy for a date range via the exchange APIs."""
    from makrograph.fetcher.nse_price_fetcher import NSEPriceFetcher
    from makrograph.fetcher.bse_price_fetcher import BSEPriceFetcher

    if exchange in ("nse", "both"):
        logger.info("=== NSE historical fetch: %s → %s ===", start, end)
        nse_total = NSEPriceFetcher(pg_cfg).fetch_date_range(start, end)
        logger.info("NSE historical: %d total rows", nse_total)

    if exchange in ("bse", "both"):
        logger.info("=== BSE historical fetch: %s → %s ===", start, end)
        bse_total = BSEPriceFetcher(pg_cfg).fetch_date_range(start, end)
        logger.info("BSE historical: %d total rows", bse_total)

# ---------------------------------------------------------------------------
# Mode: copy-from-algo  (fast PG-to-PG bulk copy)
# ---------------------------------------------------------------------------

def _copy_table(src_conn, dst_conn, table: str, columns: list[str]) -> int:
    """Bulk-copy all rows of ``table`` from src to dst using execute_values."""
    cols_str = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    select_sql = f"SELECT {cols_str} FROM {table}"
    insert_sql = f"""
        INSERT INTO {table} ({cols_str})
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    with src_conn.cursor() as src_cur:
        src_cur.execute(select_sql)
        rows = src_cur.fetchall()

    if not rows:
        logger.info("  %s: no rows in source", table)
        return 0

    with dst_conn.cursor() as dst_cur:
        psycopg2.extras.execute_values(dst_cur, insert_sql, rows, page_size=1000)
    dst_conn.commit()
    logger.info("  %s: copied %d rows", table, len(rows))
    return len(rows)


def run_copy_from_algo(pg_cfg: dict, algo_cfg: dict) -> None:
    """Copy all price + fundamentals data from Algo_Test DB into makrograph."""
    logger.info("=== Copying data from Algo_Test → makrograph ===")
    logger.info("Source: %s@%s:%s/%s", algo_cfg["user"], algo_cfg["host"],
                algo_cfg["port"], algo_cfg["dbname"])
    logger.info("Target: %s@%s:%s/%s", pg_cfg["user"], pg_cfg["host"],
                pg_cfg["port"], pg_cfg["dbname"])

    # Ensure destination tables exist
    ensure_tables(pg_cfg)

    try:
        src_conn = psycopg2.connect(**algo_cfg)
    except Exception as exc:
        logger.error("Cannot connect to Algo_Test DB: %s", exc)
        logger.error(
            "Set env vars ALGO_TEST_PG_HOST / ALGO_TEST_PG_DBNAME / "
            "ALGO_TEST_PG_USER / ALGO_TEST_PG_PASSWORD to override defaults."
        )
        sys.exit(1)

    dst_conn = psycopg2.connect(**pg_cfg)

    # Column lists must exactly match destination schema
    nse_cols = [
        "trade_date", "symbol", "series", "prev_close", "open", "high", "low",
        "last", "close", "avg_price", "tottrdqty", "tottrdval", "totaltrades",
        "delivery_qty", "delivery_pct",
    ]
    bse_cols = [
        "trade_date", "symbol", "series", "prev_close", "open", "high", "low",
        "last", "close", "avg_price", "tottrdqty", "tottrdval", "totaltrades",
        "delivery_qty", "delivery_pct", "bse_instrument_id",
    ]
    fund_cols = [
        "nse_symbol", "bse_symbol", "bse_instrument_id", "company_name",
        "sector", "industry", "market_cap", "pe_ratio", "pb_ratio", "book_value",
        "dividend_yield", "roce", "roe", "face_value", "eps", "debt_to_equity",
        "price_to_book", "sales_growth_3y", "profit_growth_3y", "current_ratio",
        "promoter_holding", "fii_holding", "dii_holding", "pledge_percentage",
        "screener_url", "data_json", "quarterly_data_json", "pl_data_json",
        "balance_sheet_json", "cash_flow_json", "shareholding_json",
        "peer_comparison_json", "price_data_json",
    ]

    try:
        # Check which tables exist in source
        with src_conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('nse_bhavcopy_data', 'bse_bhavcopy_data',
                                     'fundamentals_snapshot')
            """)
            available = {r[0] for r in cur.fetchall()}

        if "nse_bhavcopy_data" in available:
            _copy_table(src_conn, dst_conn, "nse_bhavcopy_data", nse_cols)
        else:
            logger.warning("nse_bhavcopy_data not found in source DB — skipping")

        if "bse_bhavcopy_data" in available:
            _copy_table(src_conn, dst_conn, "bse_bhavcopy_data", bse_cols)
        else:
            logger.warning("bse_bhavcopy_data not found in source DB — skipping")

        if "fundamentals_snapshot" in available:
            # Filter fund_cols to only those that exist in source
            with src_conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'fundamentals_snapshot'
                      AND table_schema = 'public'
                """)
                src_fund_cols = {r[0] for r in cur.fetchall()}
            available_fund_cols = [c for c in fund_cols if c in src_fund_cols]
            _copy_table(src_conn, dst_conn, "fundamentals_snapshot", available_fund_cols)
        else:
            logger.warning("fundamentals_snapshot not found in source DB — skipping")

        logger.info("=== Copy complete ===")
    finally:
        src_conn.close()
        dst_conn.close()

# ---------------------------------------------------------------------------
# Mode: fundamentals
# ---------------------------------------------------------------------------

def run_fundamentals(pg_cfg: dict, symbols: list[str]) -> None:
    """Fetch screener.in fundamentals for the given symbol list."""
    from makrograph.fetcher.screener_fundamentals_fetcher import ScreenerFundamentalsFetcher

    logger.info("=== Screener fundamentals fetch: %d symbols ===", len(symbols))
    fetcher = ScreenerFundamentalsFetcher(pg_cfg)
    results = fetcher.fetch_symbols(symbols)

    ok = sum(1 for v in results.values() if v == "ok")
    errors = {k: v for k, v in results.items() if v != "ok"}
    logger.info("Done: %d/%d succeeded", ok, len(symbols))
    if errors:
        logger.warning("Errors: %s", errors)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch NSE/BSE price data and screener fundamentals into makrograph DB."
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "historical", "copy-from-algo", "fundamentals"],
        required=True,
        help="Operation mode",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Start date YYYY-MM-DD (historical mode)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="End date YYYY-MM-DD (historical mode, defaults to today)",
    )
    parser.add_argument(
        "--exchange",
        choices=["nse", "bse", "both"],
        default="both",
        help="Which exchange to fetch (historical mode, default: both)",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=5,
        help="Days back for daily mode (default: 5)",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated NSE symbols for fundamentals mode",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = _load_settings()
    pg_cfg = _pg_config(settings)

    if args.mode == "daily":
        ensure_tables(pg_cfg)
        run_daily(pg_cfg, days_back=args.days_back)

    elif args.mode == "historical":
        if not args.start:
            logger.error("--start is required for historical mode")
            sys.exit(1)
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()
        run_historical(pg_cfg, start, end, exchange=args.exchange)

    elif args.mode == "copy-from-algo":
        algo_cfg = _algo_test_config()
        run_copy_from_algo(pg_cfg, algo_cfg)
        # If symbols also provided, fetch their fundamentals after copying
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            run_fundamentals(pg_cfg, symbols)

    elif args.mode == "fundamentals":
        if not args.symbols:
            logger.error("--symbols is required for fundamentals mode")
            sys.exit(1)
        ensure_tables(pg_cfg)
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        run_fundamentals(pg_cfg, symbols)


if __name__ == "__main__":
    main()
