"""NSE Bulk Deals / Block Deals / Insider Trading — live Selenium fetcher.

Ported from the MDsquare_Quant_Investing project's
``nse_bulk_block_insider_selenium_fixed.py`` and adapted to write into the
shared ``nse_bulk_deals`` / ``nse_block_deals`` / ``nse_insider_trades``
tables (schema owned by ``backend/main.py::_ensure_deal_tables``).

NSE does not expose these disclosures via a stable historical JSON API, so
this fetcher drives a headless Chrome browser against the public NSE pages
and downloads the CSV export, the same approach the source project used.

Since live-fetched rows have no natural numeric ``id`` (unlike rows copied
from the Algo_Test database, which carry over the source row id), a
deterministic id is derived by hashing the row's natural key. This makes
repeated fetches idempotent via ``ON CONFLICT (id) DO NOTHING`` without
requiring any schema changes.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

_PERIOD_DAYS = {"1D": 1, "1W": 7, "1M": 30, "3M": 90, "6M": 180, "1Y": 365}

_BULK_BLOCK_URL = "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"
_INSIDER_URL = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"

_NSE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Column-name variants seen in NSE's CSV exports over time.
_BULK_BLOCK_COL_MAP = {
    "Date": "trade_date", "DATE": "trade_date",
    "Symbol": "symbol", "SYMBOL": "symbol",
    "Security Name": "security_name", "SECURITY NAME": "security_name", "SCRIP NAME": "security_name",
    "Client Name": "client_name", "CLIENT NAME": "client_name",
    "Buy / Sell": "buy_sell", "BUY/SELL": "buy_sell", "BUY / SELL": "buy_sell",
    "Quantity": "quantity", "QUANTITY": "quantity", "QTY": "quantity",
    "Trade Price": "trade_price", "TRADE PRICE": "trade_price", "PRICE": "trade_price",
    "Remarks": "remarks", "REMARKS": "remarks",
}

_INSIDER_COL_MAP = {
    "SYMBOL": "symbol", "SYMBOL \n": "symbol", "Symbol": "symbol",
    "COMPANY": "security_name", "COMPANY \n": "security_name", "Company": "security_name",
    "SECURITY NAME": "security_name",
    "REGULATION": "transaction_type", "REGULATION \n": "transaction_type",
    "NAME OF THE ACQUIRER/DISPOSER": "insider_name", "NAME OF THE ACQUIRER/DISPOSER \n": "insider_name",
    "Name of the Acquirer / Disposer": "insider_name",
    "CATEGORY OF PERSON": "designation", "CATEGORY OF PERSON \n": "designation",
    "Category of Person": "designation",
    "NO. OF SECURITIES (ACQUIRED/DISPLOSED)": "quantity", "NO. OF SECURITIES (ACQUIRED/DISPLOSED) \n": "quantity",
    "No. of Securities": "quantity", "NO. OF SECURITIES": "quantity", "QUANTITY": "quantity",
    "VALUE OF SECURITY (ACQUIRED/DISPLOSED)": "value_traded", "VALUE OF SECURITY (ACQUIRED/DISPLOSED) \n": "value_traded",
    "Value": "value_traded", "VALUE": "value_traded",
    "DATE OF ALLOTMENT/ACQUISITION FROM": "trade_date", "DATE OF ALLOTMENT/ACQUISITION FROM \n": "trade_date",
    "Date": "trade_date", "DATE": "trade_date",
    "ACQUISITION/DISPOSAL TRANSACTION TYPE": "acquisition_mode", "ACQUISITION/DISPOSAL TRANSACTION TYPE \n": "acquisition_mode",
    "NO. OF SECURITIES POST ACQUISITION/DISPOSAL": "post_transaction_holdings",
    "NO. OF SECURITIES POST ACQUISITION/DISPOSAL \n": "post_transaction_holdings",
    "Post Transaction Holdings": "post_transaction_holdings",
    "POST ACQUISITION/DISPOSAL SHAREHOLDING %": "post_transaction_percentage",
    "POST ACQUISITION/DISPOSAL SHAREHOLDING % \n": "post_transaction_percentage",
    "Post Transaction Percentage": "post_transaction_percentage",
}


def _deal_id(*parts) -> int:
    """Deterministic BIGINT-safe id derived from a row's natural key."""
    key = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:15], 16)  # 60 bits — safely within Postgres BIGINT range


class NSEDealsFetcher:
    """Fetch NSE bulk deals, block deals, and insider trades via Selenium
    and upsert into ``nse_bulk_deals`` / ``nse_block_deals`` / ``nse_insider_trades``.
    """

    def __init__(self, pg_config: dict) -> None:
        self._pg_config = pg_config
        self._download_dir = tempfile.mkdtemp(prefix="nse_deals_")

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

    # ------------------------------------------------------------------
    # Browser
    # ------------------------------------------------------------------

    def _get_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument(f"--user-agent={_NSE_UA}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        prefs = {
            "download.default_directory": self._download_dir,
            "download.prompt_for_download": False,
            "safebrowsing.enabled": True,
        }
        opts.add_experimental_option("prefs", prefs)

        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from selenium.webdriver.chrome.service import Service
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
        except Exception:
            driver = webdriver.Chrome(options=opts)

        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        try:
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": self._download_dir,
            })
        except Exception:
            pass
        return driver

    def _clear_downloads(self) -> None:
        for f in glob.glob(os.path.join(self._download_dir, "*.csv")):
            try:
                os.remove(f)
            except OSError:
                pass

    def _latest_csv(self) -> Optional[str]:
        files = glob.glob(os.path.join(self._download_dir, "*.csv"))
        return max(files, key=os.path.getctime) if files else None

    def _read_csv(self, path: str) -> pd.DataFrame:
        try:
            df = pd.read_csv(path, encoding="utf-8")
        except Exception:
            df = pd.read_csv(path, encoding="latin-1")
        df.columns = [c.replace("\n", " ").strip() for c in df.columns]
        return df

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _download_bulk_or_block(self, deal_type: str, period: str) -> pd.DataFrame:
        """deal_type: 'bulk' | 'block'."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait, Select
        from selenium.webdriver.support import expected_conditions as EC

        driver = self._get_driver()
        try:
            driver.get("https://www.nseindia.com/")
            time.sleep(3)
            driver.get(_BULK_BLOCK_URL)
            time.sleep(5)

            wait = WebDriverWait(driver, 15)
            target_option = "Bulk Deals" if deal_type == "bulk" else "Block Deals"
            try:
                dropdown = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "select")))
                Select(dropdown).select_by_visible_text(target_option)
                logger.info(f"[nse_deals] Selected '{target_option}'")
            except Exception:
                for elem in driver.find_elements(By.XPATH, f"//*[contains(text(), '{target_option}')]"):
                    if elem.is_displayed():
                        elem.click()
                        break
            time.sleep(2)

            period_text = period if period in _PERIOD_DAYS else "1W"
            try:
                driver.find_element(By.XPATH, f"//*[contains(text(), '{period_text}')]").click()
                time.sleep(3)
            except Exception:
                logger.warning(f"[nse_deals] Could not select period button '{period_text}'")

            self._clear_downloads()
            csv_selectors = [
                "//*[contains(text(), 'csvDownload')]",
                "//*[contains(text(), '.csv')]",
                "//*[contains(@onclick, 'csv')]",
                "//a[contains(@href, 'csv')]",
                "//button[contains(text(), 'csv')]",
            ]
            clicked = False
            for sel in csv_selectors:
                try:
                    btn = driver.find_element(By.XPATH, sel)
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                logger.warning(f"[nse_deals] No CSV download control found for {deal_type} deals")
                return pd.DataFrame()

            time.sleep(8)
            latest = self._latest_csv()
            if not latest:
                logger.warning(f"[nse_deals] No CSV downloaded for {deal_type} deals")
                return pd.DataFrame()
            df = self._read_csv(latest)
            logger.info(f"[nse_deals] Downloaded {len(df)} raw {deal_type} deal rows")
            return df
        except Exception:
            logger.exception(f"[nse_deals] Error downloading {deal_type} deals")
            return pd.DataFrame()
        finally:
            driver.quit()

    def _download_insider(self, period: str) -> pd.DataFrame:
        from selenium.webdriver.common.by import By

        driver = self._get_driver()
        try:
            driver.get("https://www.nseindia.com/")
            time.sleep(3)
            driver.get(_INSIDER_URL)
            time.sleep(5)

            self._clear_downloads()
            csv_selectors = [
                "//*[contains(text(), 'csvDownload')]",
                "//*[contains(text(), '.csv')]",
                "//a[contains(@href, 'csv')]",
                "//*[contains(@class, 'download') or contains(@id, 'download') or contains(text(), 'Download')]",
            ]
            clicked = False
            for sel in csv_selectors:
                try:
                    btn = driver.find_element(By.XPATH, sel)
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                logger.warning("[nse_deals] No CSV download control found for insider trades")
                return pd.DataFrame()

            time.sleep(8)
            latest = self._latest_csv()
            if not latest:
                logger.warning("[nse_deals] No CSV downloaded for insider trades")
                return pd.DataFrame()
            df = self._read_csv(latest)
            logger.info(f"[nse_deals] Downloaded {len(df)} raw insider trade rows")
            return df
        except Exception:
            logger.exception("[nse_deals] Error downloading insider trades")
            return pd.DataFrame()
        finally:
            driver.quit()

    # ------------------------------------------------------------------
    # Clean
    # ------------------------------------------------------------------

    def _clean_bulk_block(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.rename(columns=_BULK_BLOCK_COL_MAP)
        for col in ["trade_date", "symbol", "security_name", "client_name", "buy_sell", "quantity", "trade_price", "remarks"]:
            if col not in df.columns:
                df[col] = None
        df = df[["trade_date", "symbol", "security_name", "client_name", "buy_sell", "quantity", "trade_price", "remarks"]]
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce", dayfirst=True).dt.date
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
        df["trade_price"] = pd.to_numeric(df["trade_price"], errors="coerce")
        df = df.dropna(subset=["trade_date", "symbol"])
        df["symbol"] = df["symbol"].astype(str).str.strip()
        return df

    def _clean_insider(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.rename(columns=_INSIDER_COL_MAP)
        for col in ["trade_date", "symbol", "security_name", "insider_name", "designation",
                    "transaction_type", "quantity", "value_traded",
                    "post_transaction_holdings", "post_transaction_percentage"]:
            if col not in df.columns:
                df[col] = None
        df = df[["trade_date", "symbol", "security_name", "insider_name", "designation",
                  "transaction_type", "quantity", "value_traded",
                  "post_transaction_holdings", "post_transaction_percentage"]]
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce", dayfirst=True).dt.date
        for col in ["quantity", "value_traded", "post_transaction_holdings", "post_transaction_percentage"]:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "").str.replace("%", "").str.strip(),
                errors="coerce",
            )
        df = df.dropna(subset=["trade_date", "symbol"])
        df["symbol"] = df["symbol"].astype(str).str.strip()
        return df

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    def _upsert_bulk_block(self, table: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        cols = ["id", "trade_date", "symbol", "security_name", "client_name", "buy_sell",
                "quantity", "trade_price", "remarks"]
        rows = []
        for _, r in df.iterrows():
            rid = _deal_id(r["trade_date"], r["symbol"], r["client_name"], r["buy_sell"], r["quantity"], r["trade_price"])
            rows.append((rid, r["trade_date"], r["symbol"], r["security_name"], r["client_name"],
                         r["buy_sell"], r["quantity"], r["trade_price"], r["remarks"]))
        sql = f"""
            INSERT INTO {table} ({", ".join(cols)}) VALUES %s
            ON CONFLICT (id) DO NOTHING
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
            conn.commit()
            logger.info(f"[nse_deals] Upserted {len(rows)} rows into {table}")
            return len(rows)
        except Exception:
            conn.rollback()
            logger.exception(f"[nse_deals] Error upserting into {table}")
            return 0
        finally:
            conn.close()

    def _upsert_insider(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        cols = ["id", "trade_date", "symbol", "security_name", "insider_name", "designation",
                "transaction_type", "quantity", "value_traded",
                "post_transaction_holdings", "post_transaction_percentage"]
        rows = []
        for _, r in df.iterrows():
            rid = _deal_id(r["trade_date"], r["symbol"], r["insider_name"], r["transaction_type"], r["quantity"], r["value_traded"])
            rows.append((rid, r["trade_date"], r["symbol"], r["security_name"], r["insider_name"],
                         r["designation"], r["transaction_type"], r["quantity"], r["value_traded"],
                         r["post_transaction_holdings"], r["post_transaction_percentage"]))
        sql = f"""
            INSERT INTO nse_insider_trades ({", ".join(cols)}) VALUES %s
            ON CONFLICT (id) DO NOTHING
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
            conn.commit()
            logger.info(f"[nse_deals] Upserted {len(rows)} rows into nse_insider_trades")
            return len(rows)
        except Exception:
            conn.rollback()
            logger.exception("[nse_deals] Error upserting into nse_insider_trades")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def fetch_bulk_deals(self, period: str = "1W") -> int:
        df = self._clean_bulk_block(self._download_bulk_or_block("bulk", period))
        return self._upsert_bulk_block("nse_bulk_deals", df)

    def fetch_block_deals(self, period: str = "1W") -> int:
        df = self._clean_bulk_block(self._download_bulk_or_block("block", period))
        return self._upsert_bulk_block("nse_block_deals", df)

    def fetch_insider_trades(self, period: str = "1M") -> int:
        df = self._clean_insider(self._download_insider(period))
        return self._upsert_insider(df)
