"""BSE Bulk Deals / Block Deals / Insider Trading — live Selenium fetcher.

Ported from the MDsquare_Quant_Investing project's
``bse_insider_bulk_block_selenium_ingestion.py`` and adapted to write into
the shared ``bse_bulk_deals`` / ``bse_block_deals`` / ``bse_insider_trades``
tables (schema owned by ``backend/main.py::_ensure_deal_tables``), matching
the same 8/10-column layout used for the ``nse_*`` tables and the
Algo_Test copy path (`buy_sell`/`trade_price` naming, not the divergent
`deal_type`/`price`/`exchange` schema used in the original standalone
script).

BSE's bulk/block-deal and insider-trading pages require a full browser
session (calendar date-pickers, no stable JSON API), so this fetcher
drives headless Chrome the same way the source project did.

Live-fetched rows have no natural numeric ``id``, so a deterministic id is
derived by hashing the row's natural key — this makes repeated fetches
idempotent via ``ON CONFLICT (id) DO NOTHING`` without any schema changes.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import random
import tempfile
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

_BSE_HOME = "https://www.bseindia.com/"
_BULK_BLOCK_URL = "https://www.bseindia.com/markets/equity/eqreports/bulknblockdeals.aspx"
_INSIDER_URL = "https://www.bseindia.com/corporates/Insider_Trading_new.aspx"

_BSE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_BULK_BLOCK_COL_MAP = {
    "Deal Date": "trade_date",
    "Security Code": "symbol",
    "Company": "security_name",
    "Client Name": "client_name",
    "Deal Type": "buy_sell",
    "Quantity": "quantity",
    "Price": "trade_price",
}

_INSIDER_COL_MAP = {
    "Security Code": "symbol",
    "Security Name": "security_name",
    "Name of Person": "insider_name",
    "Category of person": "designation",
    "Transaction Type ( Buy/Sale/Pledge/Revoke/Invoke)": "transaction_type",
    "Number of Securities Acquired/Disposed/Pledge etc.": "quantity",
    "Value  of Securities Acquired/Disposed/Pledge etc": "value_traded",
    "Date of acquisition of shares/sale of shares/Date of Allotment(From date)": "trade_date",
    "Number of Securities held Post  acquisition/Disposed/Pledge etc": "post_transaction_holdings",
    "Post-Transaction % of Shareholding": "post_transaction_percentage",
}


def _deal_id(*parts) -> int:
    key = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:15], 16)


def _human_delay(lo: float = 1.5, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


def _period_to_range(period: str) -> tuple[datetime, datetime]:
    end = datetime.now()
    days = {"1D": 1, "1W": 7, "1M": 30, "3M": 90, "6M": 180, "1Y": 365}.get(period, 7)
    return end - timedelta(days=days), end


class BSEDealsFetcher:
    """Fetch BSE bulk deals, block deals, and insider trades via Selenium
    and upsert into ``bse_bulk_deals`` / ``bse_block_deals`` / ``bse_insider_trades``.
    """

    def __init__(self, pg_config: dict) -> None:
        self._pg_config = pg_config
        self._download_dir = tempfile.mkdtemp(prefix="bse_deals_")

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
        opts.add_argument(f"--user-agent={_BSE_UA}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        prefs = {
            "download.default_directory": self._download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
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
        driver.set_window_size(1366, 768)
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

    def _latest_csv(self, pattern: str = "*.csv") -> Optional[str]:
        files = glob.glob(os.path.join(self._download_dir, pattern))
        return max(files, key=os.path.getctime) if files else None

    def _read_csv(self, path: str) -> pd.DataFrame:
        for encoding in ("utf-8", "latin1", "cp1252"):
            try:
                return pd.read_csv(path, encoding=encoding)
            except Exception:
                continue
        raise ValueError(f"Could not read {path} with any known encoding")

    # ------------------------------------------------------------------
    # Calendar date-picker (BSE uses a jQuery UI datepicker)
    # ------------------------------------------------------------------

    def _select_date(self, driver, field_id: str, target_date: datetime) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait, Select
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException

        try:
            field = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, field_id)))
            field.clear()
            _human_delay(0.5, 1)
            field.click()
            _human_delay(1.5, 2.5)

            try:
                WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, "ui-datepicker-div")))
                try:
                    Select(driver.find_element(By.CLASS_NAME, "ui-datepicker-year")).select_by_value(str(target_date.year))
                    _human_delay(0.8, 1.2)
                except Exception:
                    pass
                try:
                    Select(driver.find_element(By.CLASS_NAME, "ui-datepicker-month")).select_by_value(str(target_date.month - 1))
                    _human_delay(0.8, 1.2)
                except Exception:
                    pass

                day_links = driver.find_elements(
                    By.XPATH,
                    "//table[contains(@class,'ui-datepicker-calendar')]//td[not(contains(@class,'ui-datepicker-other-month'))]//a",
                )
                target_day = target_date.day
                chosen = next((d for d in day_links if d.text.strip() == str(target_day)), None)
                if chosen is None and day_links:
                    chosen = day_links[-1] if target_day > 15 else day_links[0]
                if chosen is not None:
                    driver.execute_script("arguments[0].scrollIntoView(true);", chosen)
                    _human_delay(0.3, 0.6)
                    chosen.click()
                    _human_delay(0.8, 1.2)
                    if field.get_attribute("value"):
                        return True
            except TimeoutException:
                logger.debug(f"[bse_deals] Calendar popup not shown for {field_id}, trying direct input")

            date_string = target_date.strftime("%d/%m/%Y")
            field.clear()
            _human_delay(0.3, 0.5)
            field.send_keys(date_string)
            _human_delay(0.8, 1.2)
            return bool(field.get_attribute("value"))
        except Exception:
            logger.exception(f"[bse_deals] Date selection failed for {field_id}")
            return False

    # ------------------------------------------------------------------
    # Download: bulk / block
    # ------------------------------------------------------------------

    def _download_bulk_or_block(self, deal_type: str, period: str) -> pd.DataFrame:
        """deal_type: 'bulk' | 'block'."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait, Select
        from selenium.webdriver.support import expected_conditions as EC

        driver = self._get_driver()
        try:
            driver.get(_BSE_HOME)
            _human_delay(2, 4)
            driver.get(_BULK_BLOCK_URL)
            _human_delay(3, 5)

            start_date, end_date = _period_to_range(period)

            try:
                checkbox = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "ContentPlaceHolder1_chkAllMarket"))
                )
                if not checkbox.is_selected():
                    checkbox.click()
                    _human_delay(0.8, 1.2)
            except Exception:
                logger.debug("[bse_deals] 'All Market' checkbox not found")

            target_option = "Bulk Deal" if deal_type == "bulk" else "Block Deal"
            try:
                dropdown = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "ContentPlaceHolder1_rblDT"))
                )
                Select(dropdown).select_by_visible_text(target_option)
                _human_delay(1.5, 2.5)
            except Exception:
                logger.warning(f"[bse_deals] Could not select deal type dropdown for {target_option}")
                return pd.DataFrame()

            if not self._select_date(driver, "ContentPlaceHolder1_txtDate", start_date):
                return pd.DataFrame()
            if not self._select_date(driver, "ContentPlaceHolder1_txtToDate", end_date):
                return pd.DataFrame()

            try:
                submit_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "ContentPlaceHolder1_btnSubmit"))
                )
                driver.execute_script("arguments[0].scrollIntoView(true);", submit_btn)
                _human_delay(0.8, 1.2)
                submit_btn.click()
                _human_delay(3, 5)
            except Exception:
                logger.warning(f"[bse_deals] Submit failed for {deal_type} deals")
                return pd.DataFrame()

            self._clear_downloads()
            try:
                download_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "ContentPlaceHolder1_btnDownload"))
                )
                driver.execute_script("arguments[0].scrollIntoView(true);", download_btn)
                _human_delay(0.8, 1.2)
                download_btn.click()
                _human_delay(6, 10)
            except Exception:
                logger.warning(f"[bse_deals] No download button found for {deal_type} deals")
                return pd.DataFrame()

            latest = self._latest_csv()
            if not latest:
                logger.warning(f"[bse_deals] No CSV downloaded for {deal_type} deals")
                return pd.DataFrame()
            df = self._read_csv(latest)
            logger.info(f"[bse_deals] Downloaded {len(df)} raw {deal_type} deal rows")
            return df
        except Exception:
            logger.exception(f"[bse_deals] Error downloading {deal_type} deals")
            return pd.DataFrame()
        finally:
            driver.quit()

    def _download_insider(self, period: str) -> pd.DataFrame:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        driver = self._get_driver()
        try:
            driver.get(_BSE_HOME)
            _human_delay(2, 4)
            driver.get(_INSIDER_URL)
            _human_delay(3, 5)

            start_date, end_date = _period_to_range(period)
            # BSE insider page caps history at 3 months.
            max_start = end_date - timedelta(days=90)
            if start_date < max_start:
                start_date = max_start

            if not self._select_date(driver, "ContentPlaceHolder1_txtDate", start_date):
                return pd.DataFrame()
            if not self._select_date(driver, "ContentPlaceHolder1_txtTodate", end_date):
                return pd.DataFrame()

            try:
                submit_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "ContentPlaceHolder1_btnSubmit"))
                )
                driver.execute_script("arguments[0].scrollIntoView(true);", submit_btn)
                _human_delay(0.8, 1.2)
                submit_btn.click()
                _human_delay(3, 5)
            except Exception:
                logger.warning("[bse_deals] Submit failed for insider trades")
                return pd.DataFrame()

            try:
                alert = driver.switch_to.alert
                alert.accept()
                _human_delay(1, 2)
            except Exception:
                pass

            self._clear_downloads()
            clicked = False
            try:
                link = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.ID, "ContentPlaceHolder1_lnkDownload"))
                )
                driver.execute_script("arguments[0].scrollIntoView(true);", link)
                _human_delay(0.8, 1.2)
                link.click()
                clicked = True
            except Exception:
                for elem in driver.find_elements(By.XPATH, "//a[contains(text(), 'Download') or contains(@href, 'download')]"):
                    try:
                        elem.click()
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                logger.warning("[bse_deals] No download control found for insider trades")
                return pd.DataFrame()

            _human_delay(15, 25)
            latest = self._latest_csv("SEBI_PIT*.csv") or self._latest_csv()
            if not latest:
                logger.warning("[bse_deals] No CSV downloaded for insider trades")
                return pd.DataFrame()
            df = self._read_csv(latest)
            logger.info(f"[bse_deals] Downloaded {len(df)} raw insider trade rows")
            return df
        except Exception:
            logger.exception("[bse_deals] Error downloading insider trades")
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
        for col in ["trade_date", "symbol", "security_name", "client_name", "buy_sell", "quantity", "trade_price"]:
            if col not in df.columns:
                df[col] = None
        df["remarks"] = None
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
        df["trade_date"] = df["trade_date"].apply(self._parse_bse_date)
        for col in ["quantity", "value_traded", "post_transaction_holdings", "post_transaction_percentage"]:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "").str.replace("%", "").str.strip(),
                errors="coerce",
            )
        df = df.dropna(subset=["trade_date", "symbol"])
        df["symbol"] = df["symbol"].astype(str).str.strip()
        return df

    @staticmethod
    def _parse_bse_date(value):
        if pd.isna(value) or value == "":
            return None
        s = str(value).strip()
        for fmt in ("%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %B %Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        try:
            return pd.to_datetime(s, errors="coerce").date()
        except Exception:
            return None

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
            logger.info(f"[bse_deals] Upserted {len(rows)} rows into {table}")
            return len(rows)
        except Exception:
            conn.rollback()
            logger.exception(f"[bse_deals] Error upserting into {table}")
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
            INSERT INTO bse_insider_trades ({", ".join(cols)}) VALUES %s
            ON CONFLICT (id) DO NOTHING
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
            conn.commit()
            logger.info(f"[bse_deals] Upserted {len(rows)} rows into bse_insider_trades")
            return len(rows)
        except Exception:
            conn.rollback()
            logger.exception("[bse_deals] Error upserting into bse_insider_trades")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def fetch_bulk_deals(self, period: str = "1W") -> int:
        df = self._clean_bulk_block(self._download_bulk_or_block("bulk", period))
        return self._upsert_bulk_block("bse_bulk_deals", df)

    def fetch_block_deals(self, period: str = "1W") -> int:
        df = self._clean_bulk_block(self._download_bulk_or_block("block", period))
        return self._upsert_bulk_block("bse_block_deals", df)

    def fetch_insider_trades(self, period: str = "3M") -> int:
        df = self._clean_insider(self._download_insider(period))
        return self._upsert_insider(df)
