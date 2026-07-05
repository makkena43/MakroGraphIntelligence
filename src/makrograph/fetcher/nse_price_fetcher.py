"""NSE Daily Bhavcopy (OHLCV + Delivery) Fetcher.

Downloads NSE equity bhavcopy data and upserts into ``nse_bhavcopy_data``
in the makrograph PostgreSQL database.

Uses the ``nse`` package (pip install nse) which handles NSE session-cookie
management automatically.  Falls back to a direct HTTPS archive download
if the package is unavailable.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from io import StringIO
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


class NSEPriceFetcher:
    """Fetch NSE bhavcopy (OHLCV + delivery) and store in ``nse_bhavcopy_data``.

    Args:
        pg_config: PostgreSQL connection dict — same keys as PGStore config
                   (host, port, dbname, user, password).
    """

    _NSE_ARCHIVE_URL = (
        "https://archives.nseindia.com/products/content/"
        "sec_bhavdata_full_{date_fmt}.csv"
    )
    _NSE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.nseindia.com/",
    }

    def __init__(self, pg_config: dict) -> None:
        self._pg_config = pg_config
        self._download_dir = tempfile.mkdtemp(prefix="nse_bhavcopy_")

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _conn(self):
        return psycopg2.connect(
            host=self._pg_config.get("host", "localhost"),
            port=self._pg_config.get("port", 5432),
            dbname=self._pg_config.get("dbname", "makrograph"),
            user=self._pg_config.get("user", "postgres"),
            password=self._pg_config.get("password", ""),
        )

    def ensure_table(self) -> None:
        """Create ``nse_bhavcopy_data`` if it doesn't exist (idempotent)."""
        ddl = """
            CREATE TABLE IF NOT EXISTS nse_bhavcopy_data (
                trade_date   DATE           NOT NULL,
                symbol       VARCHAR(30)    NOT NULL,
                series       VARCHAR(5),
                prev_close   NUMERIC(12, 2),
                open         NUMERIC(12, 2),
                high         NUMERIC(12, 2),
                low          NUMERIC(12, 2),
                last         NUMERIC(12, 2),
                close        NUMERIC(12, 2),
                avg_price    NUMERIC(12, 2),
                tottrdqty    NUMERIC(25, 2),
                tottrdval    NUMERIC(20, 2),
                totaltrades  INTEGER,
                delivery_qty NUMERIC(25, 2),
                delivery_pct NUMERIC(8, 2),
                PRIMARY KEY (trade_date, symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_nse_bhavcopy_symbol
                ON nse_bhavcopy_data (symbol);
            CREATE INDEX IF NOT EXISTS idx_nse_bhavcopy_date
                ON nse_bhavcopy_data (trade_date);
            CREATE INDEX IF NOT EXISTS idx_nse_bhavcopy_sym_date
                ON nse_bhavcopy_data (symbol, trade_date DESC);
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            logger.info("nse_bhavcopy_data table ensured")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _download_via_nse_package(self, trade_date: date) -> Optional[str]:
        """Use the ``nse`` package to download delivery bhavcopy."""
        try:
            from nse import NSE  # type: ignore
            nse = NSE(download_folder=self._download_dir, server=True)
            file_path = nse.deliveryBhavcopy(date=trade_date)
            if file_path and os.path.exists(file_path):
                with open(file_path, "r") as fh:
                    return fh.read()
        except ImportError:
            logger.debug("nse package not installed; falling back to direct download")
        except Exception as exc:
            logger.debug("nse package download failed: %s", exc)
        return None

    def _download_direct(self, trade_date: date) -> Optional[str]:
        """Direct HTTPS download from NSE archives (no package needed)."""
        import requests

        date_fmt = trade_date.strftime("%d%m%Y")  # e.g. 04072024
        url = self._NSE_ARCHIVE_URL.format(date_fmt=date_fmt)
        session = requests.Session()
        # Warm up NSE session cookie
        try:
            session.get("https://www.nseindia.com", headers=self._NSE_HEADERS, timeout=10)
        except Exception:
            pass
        try:
            resp = session.get(url, headers=self._NSE_HEADERS, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.content.decode("utf-8", errors="replace")
            logger.debug("NSE direct download status=%s for %s", resp.status_code, url)
        except Exception as exc:
            logger.debug("NSE direct download error: %s", exc)
        return None

    def download_bhavcopy(self, trade_date: date) -> Optional[str]:
        """Download bhavcopy CSV for ``trade_date``.  Returns raw CSV string or None."""
        csv_text = self._download_via_nse_package(trade_date)
        if not csv_text:
            csv_text = self._download_direct(trade_date)
        if not csv_text:
            logger.warning("NSE bhavcopy unavailable for %s (holiday / weekend?)", trade_date)
        return csv_text

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse_bhavcopy(self, csv_text: str) -> Optional[pd.DataFrame]:
        """Parse raw bhavcopy CSV into a clean DataFrame."""
        try:
            df = pd.read_csv(StringIO(csv_text))
            if df.empty:
                return None
            df.columns = [c.strip() for c in df.columns]

            # Normalise date column
            date_col = next((c for c in df.columns if c in ("TIMESTAMP", "DATE1")), None)
            if date_col == "DATE1":
                df["TIMESTAMP"] = df["DATE1"].str.strip()
                date_col = "TIMESTAMP"
            if date_col:
                df["TIMESTAMP"] = df["TIMESTAMP"].astype(str).str.strip()
                df["trade_date"] = pd.to_datetime(df["TIMESTAMP"], format="%d-%b-%Y",
                                                   errors="coerce")
                if df["trade_date"].isna().all():
                    df["trade_date"] = pd.to_datetime(df["TIMESTAMP"], dayfirst=True,
                                                       errors="coerce")
            else:
                df["trade_date"] = pd.NaT

            col_map = {
                "SYMBOL": "symbol", "SERIES": "series",
                "PREV_CLOSE": "prev_close", "OPEN_PRICE": "open",
                "HIGH_PRICE": "high", "LOW_PRICE": "low",
                "LAST_PRICE": "last", "CLOSE_PRICE": "close",
                "AVG_PRICE": "avg_price", "TTL_TRD_QNTY": "tottrdqty",
                "TURNOVER_LACS": "tottrdval", "NO_OF_TRADES": "totaltrades",
                "DELIV_QTY": "delivery_qty", "DELIV_PER": "delivery_pct",
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

            for col in ["delivery_qty", "delivery_pct", "series"]:
                if col not in df.columns:
                    df[col] = None

            numeric_cols = ["prev_close", "open", "high", "low", "last", "close",
                            "avg_price", "tottrdqty", "tottrdval", "delivery_qty",
                            "delivery_pct"]
            for col in numeric_cols:
                if col in df.columns and df[col].dtype == object:
                    df[col] = df[col].apply(
                        lambda x: None if pd.isna(x) or str(x).strip() in ("-", "", "- ", " -")
                        else x
                    )
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            keep = ["trade_date", "symbol", "series", "prev_close", "open", "high",
                    "low", "last", "close", "avg_price", "tottrdqty", "tottrdval",
                    "totaltrades", "delivery_qty", "delivery_pct"]
            for col in keep:
                if col not in df.columns:
                    df[col] = None

            df = df[keep].dropna(subset=["trade_date", "symbol"])
            df["symbol"] = df["symbol"].astype(str).str.strip()
            logger.info("Parsed %d NSE bhavcopy rows", len(df))
            return df

        except Exception:
            logger.exception("Error parsing NSE bhavcopy CSV")
            return None

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    def upsert(self, df: pd.DataFrame) -> int:
        """Upsert DataFrame rows into ``nse_bhavcopy_data``.  Returns row count."""
        if df is None or df.empty:
            return 0
        sql = """
            INSERT INTO nse_bhavcopy_data (
                trade_date, symbol, series, prev_close, open, high, low, last,
                close, avg_price, tottrdqty, tottrdval, totaltrades,
                delivery_qty, delivery_pct
            ) VALUES %s
            ON CONFLICT (trade_date, symbol) DO UPDATE SET
                series       = EXCLUDED.series,
                prev_close   = EXCLUDED.prev_close,
                open         = EXCLUDED.open,
                high         = EXCLUDED.high,
                low          = EXCLUDED.low,
                last         = EXCLUDED.last,
                close        = EXCLUDED.close,
                avg_price    = EXCLUDED.avg_price,
                tottrdqty    = EXCLUDED.tottrdqty,
                tottrdval    = EXCLUDED.tottrdval,
                totaltrades  = EXCLUDED.totaltrades,
                delivery_qty = EXCLUDED.delivery_qty,
                delivery_pct = EXCLUDED.delivery_pct
        """
        cols = ["trade_date", "symbol", "series", "prev_close", "open", "high",
                "low", "last", "close", "avg_price", "tottrdqty", "tottrdval",
                "totaltrades", "delivery_qty", "delivery_pct"]
        rows = [tuple(row[c] if not pd.isna(row[c]) else None for c in cols)
                for _, row in df.iterrows()]
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
            conn.commit()
            logger.info("Upserted %d rows into nse_bhavcopy_data", len(rows))
            return len(rows)
        except Exception:
            conn.rollback()
            logger.exception("Error upserting NSE bhavcopy data")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def fetch_date(self, trade_date: date) -> int:
        """Fetch + upsert a single trading date.  Returns rows inserted."""
        if trade_date.weekday() >= 5:
            logger.debug("Skipping weekend: %s", trade_date)
            return 0
        csv_text = self.download_bhavcopy(trade_date)
        if not csv_text:
            return 0
        df = self.parse_bhavcopy(csv_text)
        return self.upsert(df)

    def fetch_date_range(self, start: date, end: date) -> int:
        """Fetch every trading day in [start, end].  Returns total rows inserted."""
        self.ensure_table()
        total = 0
        current = start
        while current <= end:
            if current.weekday() < 5:
                count = self.fetch_date(current)
                total += count
                logger.info("NSE %s → %d rows (cumulative %d)", current, count, total)
            current += timedelta(days=1)
        return total

    def fetch_latest(self, days_back: int = 5) -> int:
        """Fetch the last ``days_back`` calendar days (skips weekends)."""
        end = date.today()
        start = end - timedelta(days=days_back)
        return self.fetch_date_range(start, end)
