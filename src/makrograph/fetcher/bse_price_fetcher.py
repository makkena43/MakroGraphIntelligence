"""BSE Daily Bhavcopy (OHLCV) Fetcher.

Downloads BSE equity bhavcopy data and upserts into ``bse_bhavcopy_data``
in the makrograph PostgreSQL database.

Uses the direct BSE CSV URL pattern (no third-party package needed):
  https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger(__name__)

_BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.4472.124 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Referer": "https://www.bseindia.com/",
}


class BSEPriceFetcher:
    """Fetch BSE bhavcopy (OHLCV) and store in ``bse_bhavcopy_data``.

    Args:
        pg_config: PostgreSQL connection dict (host, port, dbname, user, password).
    """

    _DIRECT_URL = (
        "https://www.bseindia.com/download/BhavCopy/Equity/"
        "BhavCopy_BSE_CM_0_0_0_{date_fmt}_F_0000.CSV"
    )

    def __init__(self, pg_config: dict) -> None:
        self._pg_config = pg_config

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
        """Create ``bse_bhavcopy_data`` if it doesn't exist (idempotent)."""
        ddl = """
            CREATE TABLE IF NOT EXISTS bse_bhavcopy_data (
                trade_date        DATE           NOT NULL,
                symbol            VARCHAR(30)    NOT NULL,
                series            VARCHAR(5),
                prev_close        NUMERIC(12, 2),
                open              NUMERIC(12, 2),
                high              NUMERIC(12, 2),
                low               NUMERIC(12, 2),
                last              NUMERIC(12, 2),
                close             NUMERIC(12, 2),
                avg_price         NUMERIC(12, 2),
                tottrdqty         BIGINT,
                tottrdval         NUMERIC(20, 2),
                totaltrades       INTEGER,
                delivery_qty      BIGINT,
                delivery_pct      NUMERIC(8, 2),
                bse_instrument_id VARCHAR(50),
                PRIMARY KEY (trade_date, symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_bse_bhavcopy_symbol
                ON bse_bhavcopy_data (symbol);
            CREATE INDEX IF NOT EXISTS idx_bse_bhavcopy_date
                ON bse_bhavcopy_data (trade_date);
            CREATE INDEX IF NOT EXISTS idx_bse_bhavcopy_sym_date
                ON bse_bhavcopy_data (symbol, trade_date DESC);
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            logger.info("bse_bhavcopy_data table ensured")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _fetch_raw(self, trade_date: date) -> Optional[bytes]:
        """Return raw bytes from BSE (CSV or ZIP).  Returns None on failure."""
        date_fmt = trade_date.strftime("%Y%m%d")
        url = self._DIRECT_URL.format(date_fmt=date_fmt)
        logger.debug("BSE bhavcopy URL: %s", url)
        try:
            resp = requests.get(url, headers=_BSE_HEADERS, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 200:
                return resp.content
            logger.debug("BSE direct URL status=%s for %s", resp.status_code, url)
        except Exception as exc:
            logger.debug("BSE fetch error: %s", exc)
        return None

    def download_bhavcopy(self, trade_date: date) -> Optional[pd.DataFrame]:
        """Download and return raw BSE bhavcopy as a DataFrame, or None."""
        raw = self._fetch_raw(trade_date)
        if raw is None:
            logger.warning("BSE bhavcopy unavailable for %s", trade_date)
            return None
        # Try CSV first
        try:
            df = pd.read_csv(io.StringIO(raw.decode("utf-8", errors="replace")))
            if not df.empty:
                return df
        except Exception:
            pass
        # Try ZIP
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            csv_files = [n for n in zf.namelist() if n.upper().endswith(".CSV")]
            if csv_files:
                with zf.open(csv_files[0]) as fh:
                    return pd.read_csv(fh)
        except Exception as exc:
            logger.debug("BSE ZIP parse error: %s", exc)
        logger.warning("Could not parse BSE bhavcopy for %s", trade_date)
        return None

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------

    def transform(self, df: pd.DataFrame, trade_date: date) -> pd.DataFrame:
        """Normalise raw BSE DataFrame to match ``bse_bhavcopy_data`` schema."""
        df = df.copy()
        df.columns = [c.strip() for c in df.columns]

        # Determine trade_date from data when available
        if "TradDt" in df.columns:
            try:
                df["trade_date"] = pd.to_datetime(df["TradDt"]).dt.date
            except Exception:
                df["trade_date"] = trade_date
        else:
            df["trade_date"] = trade_date

        modern_format = "TckrSymb" in df.columns

        if modern_format:
            df["symbol"] = df["TckrSymb"].astype(str).str.strip()
            df.loc[df["symbol"] == "", "symbol"] = "UNKNOWN"
            if "FinInstrmId" in df.columns:
                df["bse_instrument_id"] = df["FinInstrmId"].astype(str).str.strip()
            col_map = {
                "OpnPric": "open", "HghPric": "high", "LwPric": "low",
                "ClsPric": "close", "LastPric": "last",
                "PrvsClsgPric": "prev_close",
                "TtlTradgVol": "tottrdqty", "TtlTrfVal": "tottrdval",
                "TtlNbOfTxsExctd": "totaltrades",
                "SctySrs": "series", "AvrgPric": "avg_price",
            }
        else:
            col_map = {
                "SC_CODE": "symbol", "OPEN": "open", "HIGH": "high",
                "LOW": "low", "CLOSE": "close", "LAST": "last",
                "PREVCLOSE": "prev_close", "NO_OF_SHRS": "tottrdqty",
                "NET_TURNOV": "tottrdval", "NO_TRADES": "totaltrades",
                "SC_GROUP": "series",
            }
            # Alternate old-format names
            for src, tgt in [
                ("SECURITY CODE", "symbol"), ("OPEN_PRICE", "open"),
                ("HIGH_PRICE", "high"), ("LOW_PRICE", "low"),
                ("CLOSE_PRICE", "close"), ("PREV_CLOSE", "prev_close"),
                ("VOLUME", "tottrdqty"), ("TURNOVER", "tottrdval"),
                ("TRADES", "totaltrades"),
            ]:
                if src in df.columns:
                    col_map[src] = tgt

        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        defaults = {
            "symbol": "UNKNOWN", "series": "EQ", "last": 0.0,
            "avg_price": 0.0, "delivery_qty": 0, "delivery_pct": 0.0,
            "prev_close": 0.0, "tottrdqty": 0, "tottrdval": 0.0,
            "totaltrades": 0, "open": 0.0, "high": 0.0, "low": 0.0,
            "close": 0.0, "bse_instrument_id": None,
        }
        for col, default in defaults.items():
            if col not in df.columns:
                df[col] = default

        df["symbol"] = df["symbol"].astype(str).str.strip()
        df.loc[df["symbol"] == "", "symbol"] = "UNKNOWN"
        df.loc[df["symbol"].isna(), "symbol"] = "UNKNOWN"

        keep = ["trade_date", "symbol", "series", "prev_close", "open", "high",
                "low", "last", "close", "avg_price", "tottrdqty", "tottrdval",
                "totaltrades", "delivery_qty", "delivery_pct", "bse_instrument_id"]
        return df[[c for c in keep if c in df.columns]]

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    def upsert(self, df: pd.DataFrame) -> int:
        """Upsert transformed DataFrame into ``bse_bhavcopy_data``."""
        if df is None or df.empty:
            return 0
        sql = """
            INSERT INTO bse_bhavcopy_data (
                trade_date, symbol, series, prev_close, open, high, low, last,
                close, avg_price, tottrdqty, tottrdval, totaltrades,
                delivery_qty, delivery_pct, bse_instrument_id
            ) VALUES %s
            ON CONFLICT (trade_date, symbol) DO NOTHING
        """
        cols = ["trade_date", "symbol", "series", "prev_close", "open", "high",
                "low", "last", "close", "avg_price", "tottrdqty", "tottrdval",
                "totaltrades", "delivery_qty", "delivery_pct", "bse_instrument_id"]
        rows = []
        for _, row in df.iterrows():
            rows.append(tuple(
                None if (c in row and pd.isna(row[c])) else row.get(c)
                for c in cols
            ))
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
            conn.commit()
            logger.info("Upserted %d rows into bse_bhavcopy_data", len(rows))
            return len(rows)
        except Exception:
            conn.rollback()
            logger.exception("Error upserting BSE bhavcopy data")
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
        raw_df = self.download_bhavcopy(trade_date)
        if raw_df is None:
            return 0
        df = self.transform(raw_df, trade_date)
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
                logger.info("BSE %s → %d rows (cumulative %d)", current, count, total)
            current += timedelta(days=1)
        return total

    def fetch_latest(self, days_back: int = 5) -> int:
        """Fetch the last ``days_back`` calendar days."""
        end = date.today()
        start = end - timedelta(days=days_back)
        return self.fetch_date_range(start, end)
