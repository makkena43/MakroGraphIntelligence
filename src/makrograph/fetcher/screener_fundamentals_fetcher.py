"""Screener.in Fundamentals Fetcher.

Scrapes key financial metrics from screener.in and upserts into
``fundamentals_snapshot`` in the makrograph PostgreSQL database.

Adapted from MDsquare_Quant_Investing/fundamentals_loader.py.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class ScreenerFundamentalsFetcher:
    """Fetch fundamentals from screener.in and store in ``fundamentals_snapshot``.

    Args:
        pg_config:  PostgreSQL connection dict (host, port, dbname, user, password).
        sleep_min:  Minimum delay between requests (seconds) to avoid rate-limiting.
        sleep_max:  Maximum delay between requests (seconds).
    """

    _BASE_URL = "https://www.screener.in"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.screener.in/",
        "DNT": "1",
    }

    def __init__(self, pg_config: dict, sleep_min: float = 1.5, sleep_max: float = 3.0) -> None:
        self._pg_config = pg_config
        self._sleep_min = sleep_min
        self._sleep_max = sleep_max
        self._session = requests.Session()
        self._session.headers.update(self._HEADERS)

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
        """Create ``fundamentals_snapshot`` if it doesn't exist (idempotent)."""
        ddl = """
            CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
                id                   SERIAL PRIMARY KEY,
                nse_symbol           VARCHAR(30),
                bse_symbol           VARCHAR(30),
                bse_instrument_id    VARCHAR(20),
                company_name         VARCHAR(200),
                sector               VARCHAR(100),
                industry             VARCHAR(100),
                market_cap           NUMERIC(20, 2),
                pe_ratio             NUMERIC(20, 2),
                pb_ratio             NUMERIC(20, 2),
                book_value           NUMERIC(20, 2),
                dividend_yield       NUMERIC(10, 2),
                roce                 NUMERIC(10, 2),
                roe                  NUMERIC(10, 2),
                face_value           NUMERIC(10, 2),
                eps                  NUMERIC(10, 2),
                debt_to_equity       NUMERIC(10, 2),
                price_to_book        NUMERIC(10, 2),
                sales_growth_3y      NUMERIC(10, 2),
                profit_growth_3y     NUMERIC(10, 2),
                current_ratio        NUMERIC(10, 2),
                promoter_holding     NUMERIC(10, 2),
                fii_holding          NUMERIC(10, 2),
                dii_holding          NUMERIC(10, 2),
                pledge_percentage    NUMERIC(10, 2),
                screener_url         VARCHAR(200),
                data_json            TEXT,
                quarterly_data_json  TEXT,
                pl_data_json         TEXT,
                balance_sheet_json   TEXT,
                cash_flow_json       TEXT,
                shareholding_json    TEXT,
                peer_comparison_json TEXT,
                price_data_json      TEXT,
                last_updated         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_fundamentals_symbols UNIQUE (nse_symbol, bse_symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_fundamentals_nse
                ON fundamentals_snapshot (nse_symbol);
            CREATE INDEX IF NOT EXISTS idx_fundamentals_bse
                ON fundamentals_snapshot (bse_symbol);
            CREATE INDEX IF NOT EXISTS idx_fundamentals_upd
                ON fundamentals_snapshot (last_updated);
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            logger.info("fundamentals_snapshot table ensured")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Scraping helpers
    # ------------------------------------------------------------------

    def _sleep(self) -> None:
        time.sleep(random.uniform(self._sleep_min, self._sleep_max))

    def _get(self, url: str, max_retries: int = 3) -> Optional[requests.Response]:
        """Rate-limited GET with retry."""
        for attempt in range(max_retries):
            self._sleep()
            try:
                resp = self._session.get(url, timeout=30)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 429:
                    wait = min(60 * (attempt + 1), 300)
                    logger.warning("Rate limited — sleeping %ds", wait)
                    time.sleep(wait)
                else:
                    logger.debug("HTTP %s for %s", resp.status_code, url)
            except Exception as exc:
                logger.debug("Request error (attempt %d): %s", attempt + 1, exc)
        return None

    @staticmethod
    def _parse_num(text: Optional[str]) -> Optional[float]:
        """Parse a number string, handling commas, percentages, and Cr/L scales."""
        if not text or text.strip() in ("-", "N.A.", "N.A", "NA", ""):
            return None
        text = text.strip()
        multiplier = 1.0
        lower = text.lower()
        if "cr" in lower or "crore" in lower:
            multiplier = 1e7
            text = re.sub(r"cr(ores?)?", "", lower, flags=re.I).strip()
        elif " l" in lower or lower.endswith("l"):
            multiplier = 1e5
            text = re.sub(r"\bl\b", "", lower, flags=re.I).strip()
        text = text.replace(",", "").replace("₹", "").replace("%", "").strip()
        try:
            return float(text) * multiplier
        except (ValueError, TypeError):
            m = re.search(r"([+-]?\d+\.?\d*)", text)
            if m:
                return float(m.group(1)) * multiplier
        return None

    # ------------------------------------------------------------------
    # Scrape a single company
    # ------------------------------------------------------------------

    def fetch_company(self, nse_symbol: str) -> Optional[dict]:
        """Scrape screener.in for ``nse_symbol`` and return a data dict.

        Returns None if the company page cannot be fetched.
        """
        url = f"{self._BASE_URL}/company/{nse_symbol}/consolidated/"
        resp = self._get(url)
        if resp is None:
            # Try standalone (non-consolidated)
            url = f"{self._BASE_URL}/company/{nse_symbol}/"
            resp = self._get(url)
        if resp is None:
            logger.warning("Could not fetch screener page for %s", nse_symbol)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        data: dict = {
            "nse_symbol": nse_symbol,
            "bse_symbol": None,
            "bse_instrument_id": None,
            "company_name": None,
            "sector": None,
            "industry": None,
            "screener_url": url,
            "market_cap": None,
            "pe_ratio": None,
            "pb_ratio": None,
            "book_value": None,
            "dividend_yield": None,
            "roce": None,
            "roe": None,
            "face_value": None,
            "eps": None,
            "debt_to_equity": None,
            "price_to_book": None,
            "sales_growth_3y": None,
            "profit_growth_3y": None,
            "current_ratio": None,
            "promoter_holding": None,
            "fii_holding": None,
            "dii_holding": None,
            "pledge_percentage": None,
            "data_json": None,
        }

        # Company name
        h1 = soup.find("h1", class_=re.compile(r"company-name|h1"))
        if not h1:
            h1 = soup.find("h1")
        if h1:
            data["company_name"] = h1.get_text(strip=True)

        # Sector / Industry from breadcrumb or company-info
        for a in soup.find_all("a", href=re.compile(r"/screens/|/sector/")):
            txt = a.get_text(strip=True)
            if txt and data["sector"] is None:
                data["sector"] = txt
            elif txt and data["industry"] is None:
                data["industry"] = txt

        # Key ratios from the top-level ratio list
        ratio_section = soup.find("ul", id="top-ratios")
        if ratio_section is None:
            ratio_section = soup.find("ul", class_=re.compile(r"ratios|top-ratios"))
        if ratio_section:
            for li in ratio_section.find_all("li"):
                name_tag = li.find("span", class_=re.compile(r"name"))
                val_tag = li.find("span", class_=re.compile(r"value|number"))
                if not name_tag or not val_tag:
                    continue
                name = name_tag.get_text(strip=True).lower()
                val = self._parse_num(val_tag.get_text(strip=True))
                if "market cap" in name:
                    data["market_cap"] = val
                elif name.startswith("p/e") or name == "stock p/e":
                    data["pe_ratio"] = val
                elif "book value" in name:
                    data["book_value"] = val
                elif "dividend yield" in name:
                    data["dividend_yield"] = val
                elif "roce" in name:
                    data["roce"] = val
                elif "roe" in name:
                    data["roe"] = val
                elif "face value" in name:
                    data["face_value"] = val
                elif "eps" in name:
                    data["eps"] = val
                elif "debt" in name and "equity" in name:
                    data["debt_to_equity"] = val
                elif "price to book" in name or "p/b" in name:
                    data["price_to_book"] = val
                    data["pb_ratio"] = val
                elif "sales growth" in name and "3yr" in name:
                    data["sales_growth_3y"] = val
                elif "profit growth" in name and "3yr" in name:
                    data["profit_growth_3y"] = val
                elif "current ratio" in name:
                    data["current_ratio"] = val

        # Shareholding: promoter / FII / DII
        holding_section = soup.find(id="shareholding")
        if holding_section:
            text = holding_section.get_text(" ", strip=True).lower()
            for label, key in [
                ("promoter", "promoter_holding"),
                ("fii", "fii_holding"),
                ("dii", "dii_holding"),
                ("pledge", "pledge_percentage"),
            ]:
                m = re.search(rf"{label}[^\d]*(\d+\.?\d*)", text)
                if m:
                    data[key] = float(m.group(1))

        data["data_json"] = json.dumps({"scraped_at": datetime.utcnow().isoformat()})
        return data

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    def upsert(self, record: dict) -> bool:
        """Upsert a single fundamentals record.  Returns True on success."""
        sql = """
            INSERT INTO fundamentals_snapshot (
                nse_symbol, bse_symbol, bse_instrument_id, company_name,
                sector, industry, market_cap, pe_ratio, pb_ratio, book_value,
                dividend_yield, roce, roe, face_value, eps, debt_to_equity,
                price_to_book, sales_growth_3y, profit_growth_3y, current_ratio,
                promoter_holding, fii_holding, dii_holding, pledge_percentage,
                screener_url, data_json, last_updated
            ) VALUES (
                %(nse_symbol)s, %(bse_symbol)s, %(bse_instrument_id)s,
                %(company_name)s, %(sector)s, %(industry)s, %(market_cap)s,
                %(pe_ratio)s, %(pb_ratio)s, %(book_value)s, %(dividend_yield)s,
                %(roce)s, %(roe)s, %(face_value)s, %(eps)s, %(debt_to_equity)s,
                %(price_to_book)s, %(sales_growth_3y)s, %(profit_growth_3y)s,
                %(current_ratio)s, %(promoter_holding)s, %(fii_holding)s,
                %(dii_holding)s, %(pledge_percentage)s, %(screener_url)s,
                %(data_json)s, NOW()
            )
            ON CONFLICT ON CONSTRAINT uq_fundamentals_symbols DO UPDATE SET
                company_name       = COALESCE(EXCLUDED.company_name, fundamentals_snapshot.company_name),
                sector             = COALESCE(EXCLUDED.sector, fundamentals_snapshot.sector),
                industry           = COALESCE(EXCLUDED.industry, fundamentals_snapshot.industry),
                market_cap         = COALESCE(EXCLUDED.market_cap, fundamentals_snapshot.market_cap),
                pe_ratio           = COALESCE(EXCLUDED.pe_ratio, fundamentals_snapshot.pe_ratio),
                pb_ratio           = COALESCE(EXCLUDED.pb_ratio, fundamentals_snapshot.pb_ratio),
                book_value         = COALESCE(EXCLUDED.book_value, fundamentals_snapshot.book_value),
                dividend_yield     = COALESCE(EXCLUDED.dividend_yield, fundamentals_snapshot.dividend_yield),
                roce               = COALESCE(EXCLUDED.roce, fundamentals_snapshot.roce),
                roe                = COALESCE(EXCLUDED.roe, fundamentals_snapshot.roe),
                face_value         = COALESCE(EXCLUDED.face_value, fundamentals_snapshot.face_value),
                eps                = COALESCE(EXCLUDED.eps, fundamentals_snapshot.eps),
                debt_to_equity     = COALESCE(EXCLUDED.debt_to_equity, fundamentals_snapshot.debt_to_equity),
                price_to_book      = COALESCE(EXCLUDED.price_to_book, fundamentals_snapshot.price_to_book),
                sales_growth_3y    = COALESCE(EXCLUDED.sales_growth_3y, fundamentals_snapshot.sales_growth_3y),
                profit_growth_3y   = COALESCE(EXCLUDED.profit_growth_3y, fundamentals_snapshot.profit_growth_3y),
                current_ratio      = COALESCE(EXCLUDED.current_ratio, fundamentals_snapshot.current_ratio),
                promoter_holding   = COALESCE(EXCLUDED.promoter_holding, fundamentals_snapshot.promoter_holding),
                fii_holding        = COALESCE(EXCLUDED.fii_holding, fundamentals_snapshot.fii_holding),
                dii_holding        = COALESCE(EXCLUDED.dii_holding, fundamentals_snapshot.dii_holding),
                pledge_percentage  = COALESCE(EXCLUDED.pledge_percentage, fundamentals_snapshot.pledge_percentage),
                screener_url       = COALESCE(EXCLUDED.screener_url, fundamentals_snapshot.screener_url),
                data_json          = EXCLUDED.data_json,
                last_updated       = NOW()
        """
        # Ensure bse_symbol / nse_symbol are never both NULL (constraint requires at least one)
        if not record.get("nse_symbol") and not record.get("bse_symbol"):
            logger.warning("Skipping record with no symbol: %s", record)
            return False
        if record.get("nse_symbol") is None:
            record["nse_symbol"] = ""
        if record.get("bse_symbol") is None:
            record["bse_symbol"] = ""

        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, record)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            logger.exception("Error upserting fundamentals for %s", record.get("nse_symbol"))
            return False
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def fetch_symbols(self, symbols: list[str]) -> dict:
        """Fetch fundamentals for a list of NSE symbols.

        Returns ``{symbol: 'ok'|'error'}`` status dict.
        """
        self.ensure_table()
        results: dict = {}
        for i, symbol in enumerate(symbols, 1):
            logger.info("[%d/%d] Fetching screener fundamentals for %s", i, len(symbols), symbol)
            try:
                record = self.fetch_company(symbol)
                if record:
                    ok = self.upsert(record)
                    results[symbol] = "ok" if ok else "upsert_failed"
                else:
                    results[symbol] = "not_found"
            except Exception as exc:
                logger.exception("Error fetching %s", symbol)
                results[symbol] = f"error: {exc}"
        return results
