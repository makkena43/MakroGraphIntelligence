"""NSE/BSE Security Master (Sector/Industry) Fetcher.

Builds a ``security_master`` table mapping NSE + BSE symbols to sector and
industry classification.  Adapted from
MDsquare_Quant_Investing/stock_sector_industry_data.py.

NSE data is fetched live from NSE archives.  BSE sector/industry data
requires the BSE-published Equity.csv / EQT0.csv scrip master (no stable
public CSV endpoint), so this fetcher will use it if present under
``data/bse_downloads/`` and otherwise leaves BSE sector columns NULL.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger(__name__)

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


class SectorMasterFetcher:
    """Fetch NSE (+ optional BSE) sector/industry data into ``security_master``.

    Args:
        pg_config: PostgreSQL connection dict (host, port, dbname, user, password).
        bse_downloads_dir: Optional directory containing BSE Equity.csv / EQT0.csv.
    """

    _NSE_EQUITY_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    _NSE_SECTOR_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

    def __init__(self, pg_config: dict, bse_downloads_dir: Optional[str] = None) -> None:
        self._pg_config = pg_config
        self._bse_downloads_dir = bse_downloads_dir

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
        """Create ``security_master`` if it doesn't exist (idempotent)."""
        ddl = """
            CREATE TABLE IF NOT EXISTS security_master (
                isin           VARCHAR(20),
                nse_symbol     VARCHAR(30),
                bse_symbol     VARCHAR(30),
                company_name   VARCHAR(200),
                sector_nse     VARCHAR(150),
                industry_nse   TEXT,
                sector_bse     VARCHAR(150),
                industry_bse   TEXT,
                last_updated   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_security_master_isin UNIQUE (isin)
            );
            CREATE INDEX IF NOT EXISTS idx_security_master_nse ON security_master (nse_symbol);
            CREATE INDEX IF NOT EXISTS idx_security_master_bse ON security_master (bse_symbol);
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            logger.info("security_master table ensured")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # NSE
    # ------------------------------------------------------------------

    def fetch_nse_data(self) -> pd.DataFrame:
        """Fetch NSE-listed companies with symbol, name, sector, industry."""
        cols = ["isin", "nse_symbol", "company_name", "sector_nse", "industry_nse"]
        try:
            resp = requests.get(self._NSE_EQUITY_URL, headers=_NSE_HEADERS, timeout=30)
            resp.raise_for_status()
            equity_df = pd.read_csv(io.StringIO(resp.text))
        except Exception as exc:
            logger.warning("Could not fetch NSE equity list: %s", exc)
            return pd.DataFrame(columns=cols)

        equity_df.columns = [c.strip() for c in equity_df.columns]
        rename_map = {}
        for col in equity_df.columns:
            up = col.upper()
            if "SYMBOL" in up:
                rename_map[col] = "nse_symbol"
            elif "NAME" in up and "COMPANY" in up:
                rename_map[col] = "company_name"
            elif "ISIN" in up:
                rename_map[col] = "isin"
        equity_df = equity_df.rename(columns=rename_map)
        if "isin" not in equity_df.columns or "nse_symbol" not in equity_df.columns:
            logger.warning("NSE equity list missing required columns")
            return pd.DataFrame(columns=cols)

        equity_df["isin"] = equity_df["isin"].astype(str)
        keep = [c for c in ["nse_symbol", "company_name", "isin"] if c in equity_df.columns]
        equity_df = equity_df[keep].copy()

        # Sector data (best-effort — Nifty500 constituents only)
        try:
            sector_resp = requests.get(self._NSE_SECTOR_URL, headers=_NSE_HEADERS, timeout=30)
            sector_resp.raise_for_status()
            sector_df = pd.read_csv(io.StringIO(sector_resp.text))
            sector_df.columns = [c.strip() for c in sector_df.columns]
            symbol_col = next((c for c in sector_df.columns if "SYMBOL" in c.upper()), None)
            industry_col = next((c for c in sector_df.columns if "INDUSTRY" in c.upper()), None)
            sector_col = next((c for c in sector_df.columns if "SECTOR" in c.upper()), None)
            if symbol_col:
                rn = {symbol_col: "nse_symbol"}
                if industry_col:
                    rn[industry_col] = "industry_nse"
                if sector_col:
                    rn[sector_col] = "sector_nse"
                sector_df = sector_df.rename(columns=rn)
                sector_cols = [c for c in ["nse_symbol", "industry_nse", "sector_nse"] if c in sector_df.columns]
                sector_df = sector_df[sector_cols]
                result = pd.merge(equity_df, sector_df, on="nse_symbol", how="left")
            else:
                result = equity_df.copy()
        except Exception as exc:
            logger.debug("NSE sector list unavailable: %s", exc)
            result = equity_df.copy()

        for col in ["sector_nse", "industry_nse"]:
            if col not in result.columns:
                result[col] = None

        logger.info("Fetched %d NSE companies", len(result))
        return result[[c for c in cols if c in result.columns]]

    # ------------------------------------------------------------------
    # BSE (best-effort, requires locally downloaded scrip master)
    # ------------------------------------------------------------------

    def fetch_bse_data(self) -> pd.DataFrame:
        """Read BSE scrip master from local CSV if available, else return empty."""
        cols = ["isin", "bse_symbol", "company_name", "sector_bse", "industry_bse"]
        if not self._bse_downloads_dir:
            logger.info("No bse_downloads_dir configured — skipping BSE sector data")
            return pd.DataFrame(columns=cols)

        equity_file = Path(self._bse_downloads_dir) / "Equity.csv"
        eqt0_file = Path(self._bse_downloads_dir) / "EQT0.csv"
        src = equity_file if equity_file.exists() else (eqt0_file if eqt0_file.exists() else None)
        if src is None:
            logger.info("BSE scrip master not found in %s — skipping", self._bse_downloads_dir)
            return pd.DataFrame(columns=cols)

        bse_df = pd.read_csv(src)
        col_map = {
            "Security Id": "bse_symbol", "Issuer Name": "company_name",
            "ISIN No": "isin", "Sector Name": "sector_bse",
            "Industry New Name": "industry_bse",
        }
        bse_df = bse_df.rename(columns=col_map)
        if "Status" in bse_df.columns:
            bse_df = bse_df[bse_df["Status"] == "Active"].copy()
        for col in cols:
            if col not in bse_df.columns:
                bse_df[col] = None
        bse_df["isin"] = bse_df["isin"].astype(str)
        logger.info("Loaded %d BSE securities from %s", len(bse_df), src)
        return bse_df[cols]

    # ------------------------------------------------------------------
    # Merge + upsert
    # ------------------------------------------------------------------

    def build_security_master(self) -> pd.DataFrame:
        """Merge NSE + BSE sector data on ISIN."""
        nse = self.fetch_nse_data()
        bse = self.fetch_bse_data()

        for col in ["isin", "nse_symbol", "sector_nse", "industry_nse"]:
            if col not in nse.columns:
                nse[col] = None
        for col in ["isin", "bse_symbol", "sector_bse", "industry_bse"]:
            if col not in bse.columns:
                bse[col] = None

        if nse.empty and bse.empty:
            return pd.DataFrame(columns=["isin", "nse_symbol", "bse_symbol", "company_name",
                                          "sector_nse", "industry_nse", "sector_bse", "industry_bse"])

        nse["isin"] = nse["isin"].astype(str)
        bse["isin"] = bse["isin"].astype(str)

        merged = pd.merge(nse, bse, on="isin", how="outer", suffixes=("_nse_co", "_bse_co"))
        if "company_name_nse_co" in merged.columns:
            merged["company_name"] = merged["company_name_nse_co"].fillna(merged.get("company_name_bse_co"))
        elif "company_name" not in merged.columns:
            merged["company_name"] = None

        keep = ["isin", "nse_symbol", "bse_symbol", "company_name",
                "sector_nse", "industry_nse", "sector_bse", "industry_bse"]
        for col in keep:
            if col not in merged.columns:
                merged[col] = None
        return merged[keep]

    def upsert(self, df: pd.DataFrame) -> int:
        """Upsert the security master DataFrame into ``security_master``."""
        if df.empty:
            return 0
        sql = """
            INSERT INTO security_master (
                isin, nse_symbol, bse_symbol, company_name,
                sector_nse, industry_nse, sector_bse, industry_bse, last_updated
            ) VALUES %s
            ON CONFLICT (isin) DO UPDATE SET
                nse_symbol   = COALESCE(EXCLUDED.nse_symbol, security_master.nse_symbol),
                bse_symbol   = COALESCE(EXCLUDED.bse_symbol, security_master.bse_symbol),
                company_name = COALESCE(EXCLUDED.company_name, security_master.company_name),
                sector_nse   = COALESCE(EXCLUDED.sector_nse, security_master.sector_nse),
                industry_nse = COALESCE(EXCLUDED.industry_nse, security_master.industry_nse),
                sector_bse   = COALESCE(EXCLUDED.sector_bse, security_master.sector_bse),
                industry_bse = COALESCE(EXCLUDED.industry_bse, security_master.industry_bse),
                last_updated = NOW()
        """
        cols = ["isin", "nse_symbol", "bse_symbol", "company_name",
                "sector_nse", "industry_nse", "sector_bse", "industry_bse"]
        df = df.dropna(subset=["isin"])
        df = df[df["isin"].astype(str).str.lower() != "nan"]
        now = pd.Timestamp.utcnow()
        rows = [tuple(None if pd.isna(row[c]) else row[c] for c in cols) + (now,)
                for _, row in df.iterrows()]

        conn = self._conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=1000)
            conn.commit()
            logger.info("Upserted %d rows into security_master", len(rows))
            return len(rows)
        except Exception:
            conn.rollback()
            logger.exception("Error upserting security_master")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Fetch + upsert the full security master.  Returns rows upserted."""
        self.ensure_table()
        df = self.build_security_master()
        return self.upsert(df)
