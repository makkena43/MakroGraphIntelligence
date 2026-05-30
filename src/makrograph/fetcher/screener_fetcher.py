"""Screener.in company data fetcher.

Scrapes Screener.in company pages to discover:
  - Annual report PDF links
  - Investor presentation links
  - Conference call / concall transcript links
  - Peer company lists

Symbol discovery (when symbol_list is empty):
  Automatically pulls the full NSE EQUITY_L.csv (~2 400 listed companies) so
  you never have to maintain a curated ticker list.  Every active NSE-listed
  company is scraped up to max_results_per_run documents total.

Uses requests + BeautifulSoup for standard page scraping.
Falls back to Selenium (headless Chrome) for JavaScript-heavy pages when
`use_selenium: true` is set in config.

Screener.in company page: https://www.screener.in/company/{SYMBOL}/

Config keys (under `screener:` in settings.yaml):
    symbol_list       - list of NSE ticker symbols e.g. [INFY, TCS]
                        leave EMPTY to auto-discover all NSE-listed companies (recommended)
    use_selenium      - bool, use Selenium for JS-rendered pages (default false)
    selenium_headless - bool, headless Chrome (default true)
    start_date        - ISO date, ignore documents older than this (default "2023-01-01")
    max_results       - max total docs per run across all symbols (default 500)
    api_delay_seconds - seconds between page requests (default 1.5)
    extract_peers     - bool, also enqueue peer company pages (default false)
"""

import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False
    BeautifulSoup = None

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

SCREENER_BASE = "https://www.screener.in"
SCREENER_COMPANY_URL = f"{SCREENER_BASE}/company"
SCREENER_SEARCH_URL = f"{SCREENER_BASE}/api/company/search/"

# Full NSE equity list — all active listed companies (~2 400), no auth needed
NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

_SCREENER_HEADERS = {
    "Referer": SCREENER_BASE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
}

_DOC_LINK_PATTERNS = re.compile(
    r"\.(pdf|pptx|ppt|xlsx|xls|docx)$", re.IGNORECASE
)

_LABEL_TO_TYPE = {
    "annual report": "annual_report",
    "annual-report": "annual_report",
    "presentation": "presentation",
    "concall": "concall_transcript",
    "conference call": "concall_transcript",
    "transcript": "concall_transcript",
    "investor": "investor_presentation",
    "earnings": "earnings",
    "result": "result",
}


class ScreenerFetcher(SourceAdapter):
    """Fetches annual reports, presentations, and concall links from Screener.in."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.symbol_list: list[str] = [s.upper() for s in config.get("symbol_list", [])]
        self.use_selenium: bool = config.get("use_selenium", False)
        self.selenium_headless: bool = config.get("selenium_headless", True)
        self.start_date: str = config.get("start_date", "2023-01-01")
        self.extract_peers: bool = config.get("extract_peers", False)
        self.api_delay: float = config.get("api_delay_seconds", 1.5)
        self._driver = None
        self._resolved_symbols: list[str] = []   # populated on first discover()

        if not _BS4_AVAILABLE:
            logger.warning(
                "[screener] beautifulsoup4 not installed. "
                "Install with: pip install beautifulsoup4 lxml"
            )

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        })

    @property
    def source_name(self) -> str:
        return "screener_india"

    # ── Symbol discovery ───────────────────────────────────────────────────

    def _fetch_all_nse_symbols(self) -> list[str]:
        """Download NSE EQUITY_L.csv and return all active listed symbols (~2 400).

        The CSV is public, requires no authentication, and covers every company
        listed on NSE — far broader than any index constituent list.
        """
        try:
            resp = self.session.get(NSE_EQUITY_LIST_URL, timeout=self.timeout)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            symbols = [
                row.get("SYMBOL", "").strip().upper()
                for row in reader
                if row.get("SYMBOL", "").strip()
            ]
            logger.info(f"[screener] Auto-discovered {len(symbols)} symbols from NSE equity list")
            return symbols
        except Exception as exc:
            logger.error(f"[screener] NSE equity list download failed: {exc}")
            return []

    def _get_symbols(self) -> list[str]:
        """Return the effective symbol list (configured or auto-discovered from NSE)."""
        if self.symbol_list:
            return self.symbol_list
        if not self._resolved_symbols:
            self._resolved_symbols = self._fetch_all_nse_symbols()
        return self._resolved_symbols

    def _get_selenium_driver(self):
        """Lazily initialise a headless Selenium ChromeDriver."""
        if self._driver is not None:
            return self._driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service

            opts = Options()
            if self.selenium_headless:
                opts.add_argument("--headless")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument(
                "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            self._driver = webdriver.Chrome(options=opts)
            logger.info("[screener] Selenium ChromeDriver initialized")
            return self._driver
        except Exception as exc:
            logger.error(f"[screener] Selenium init failed: {exc}")
            return None

    def _fetch_page_html(self, url: str) -> str:
        """Fetch HTML for a URL, using Selenium if configured, else requests."""
        if self.use_selenium:
            driver = self._get_selenium_driver()
            if driver:
                try:
                    import time as _time
                    driver.get(url)
                    _time.sleep(2)
                    return driver.page_source
                except Exception as exc:
                    logger.warning(f"[screener] Selenium fetch failed for {url}: {exc}")
        try:
            resp = self.session.get(url, headers=_SCREENER_HEADERS, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.error(f"[screener] HTTP fetch failed for {url}: {exc}")
            return ""

    def _scrape_company_docs(self, symbol: str) -> list[SourceDocument]:
        """Scrape all document links from a Screener company page."""
        url = f"{SCREENER_COMPANY_URL}/{symbol}/"
        html = self._fetch_page_html(url)
        if not html or not _BS4_AVAILABLE:
            return []

        soup = BeautifulSoup(html, "lxml")
        company_name = ""
        h1 = soup.find("h1")
        if h1:
            company_name = h1.get_text(strip=True)

        docs: list[SourceDocument] = []
        start_dt = _parse_iso_date(self.start_date)

        for a_tag in soup.find_all("a", href=True):
            href: str = a_tag["href"]
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue

            abs_url = urljoin(SCREENER_BASE, href)
            raw_label = a_tag.get_text(strip=True)

            # Skip links with no meaningful label or very short navigation text
            if not raw_label or len(raw_label) < 4:
                continue

            # Only pick actual document files (.pdf, .pptx, .xlsx, etc.)
            if not _DOC_LINK_PATTERNS.search(href):
                continue

            # ── Deduplication guard ───────────────────────────────────────────
            # NSE, BSE, InvestIndia, and Commerce fetchers already retrieve docs
            # from these domains.  Screener pages just re-link to the same exchange
            # attachments — fetching them here would waste ~1 hour of scraping and
            # produce URL-level duplicates the deduplicator would have to reject.
            # Only keep URLs that are genuinely Screener-exclusive (hosted on
            # screener.in itself or company/unknown domains).
            _EXCHANGE_DOMAINS = (
                "bseindia.com",
                "nsearchives.nseindia.com",
                "nseindia.com",
                "static.investindia.gov.in",
                "investindia.gov.in",
                "content.dgft.gov.in",
                "dgft.gov.in",
                "sebi.gov.in",
                "rbi.org.in",
            )
            if any(d in abs_url for d in _EXCHANGE_DOMAINS):
                continue

            label = raw_label.lower()
            filing_type = _classify_screener_label(label)

            pub_dt = _extract_date_from_label(a_tag)

            # Lower-bound filter: skip docs older than start_date
            # When pub_dt is None (no date in label) we keep the doc —
            # we can't tell when it was published so assume it's relevant.
            if start_dt and pub_dt and pub_dt < start_dt:
                continue

            docs.append(SourceDocument(
                url=abs_url,
                title=a_tag.get_text(strip=True)[:500] or filing_type,
                doc_type="document",
                source_name=self.source_name,
                published_at=pub_dt,
                company=company_name,
                ticker=symbol,
                filing_type=filing_type,
                metadata={
                    "exchange": "NSE/BSE",
                    "country": "IN",
                    "symbol": symbol,
                    "screener_url": url,
                },
            ))

        logger.debug(f"[screener] {symbol}: {len(docs)} docs found")
        return docs

    def discover(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[SourceDocument]:
        """Discover documents from Screener.in for all configured (or auto-discovered) symbols.

        Args:
            since: Skip docs published on or before this datetime (checkpoint lower bound).
            until: Skip docs published after this datetime (UI end_date upper bound).
                   Docs with no detectable date are always included (date unknown).

        When symbol_list is empty, auto-discovers all ~2 400 NSE-listed companies
        from EQUITY_L.csv so no curated list is needed.

        Respects max_results_per_run — stops after collecting that many docs across
        all symbols (0 = unlimited).
        """
        symbols = self._get_symbols()
        if not symbols:
            logger.warning("[screener] No symbols available (config + index download both empty)")
            return []

        max_results: int = self.config.get("max_results_per_run", 0)
        unlimited = (max_results == 0)
        source = "config" if self.symbol_list else "NSE equity list (all listed)"
        logger.info(
            f"[screener] Scraping {len(symbols)} symbols from {source} "
            f"({'unlimited' if unlimited else max_results} doc cap)"
        )

        all_docs: list[SourceDocument] = []
        for i, symbol in enumerate(symbols, 1):
            if not unlimited and len(all_docs) >= max_results:
                break

            self._throttle()
            docs = self._scrape_company_docs(symbol)

            for d in docs:
                # Lower bound: skip docs we already have
                if since and d.published_at and d.published_at <= since:
                    continue
                # Upper bound: skip docs newer than the requested end date
                # (only applies when pub_dt is known — unknown dates are kept)
                if until and d.published_at and d.published_at > until:
                    continue
                all_docs.append(d)
                if not unlimited and len(all_docs) >= max_results:
                    break

            if i % 50 == 0:
                logger.info(
                    f"[screener] Progress: {i}/{len(symbols)} companies | "
                    f"{len(all_docs)} docs collected"
                )

        logger.info(
            f"[screener] Discovered {len(all_docs)} documents from "
            f"{min(i, len(symbols))} companies"
        )
        return all_docs

    def close(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        super().close()


def _classify_screener_label(label: str) -> str:
    """Map a link label to a structured filing type."""
    for key, val in _LABEL_TO_TYPE.items():
        if key in label:
            return val
    return "document"


def _extract_date_from_label(tag) -> Optional[datetime]:
    """Try to parse a date from text near a link (e.g. 'Annual Report 2023-24')."""
    text = tag.get_text(" ", strip=True)
    m = re.search(r"(20\d{2})[-–/](20\d{2}|[0-9]{2})", text)
    if m:
        year = int(m.group(1))
        try:
            return datetime(year, 4, 1, tzinfo=timezone.utc)
        except Exception:
            pass
    m2 = re.search(r"(20\d{2})", text)
    if m2:
        try:
            return datetime(int(m2.group(1)), 1, 1, tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
