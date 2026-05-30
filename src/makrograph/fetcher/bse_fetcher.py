"""BSE India corporate announcements and board decisions fetcher.

Browser-free strategy (tried in order):
  1. curl_cffi (Chrome TLS fingerprint)  — fast, no browser, works if BSE relaxes protection
  2. Disk-cached Akamai cookies          — reuse today's cookies from a previous browser warm-up
  3. Playwright (Chromium, headless)     — lighter than Selenium, auto-downloads Chromium
  4. Selenium (Chrome, headless)         — legacy fallback
  5. BSE scrip-list API                  — always works, company metadata only

Cookie caching
--------------
After any successful browser warm-up the Akamai session cookies (bm_sz, _abck, ak_bmsc)
are written to  ~/.makrograph/bse_cookies.json  with a TTL timestamp.
On the next run the cached cookies are injected into the requests session and strategy 1
(curl_cffi or plain requests) is retried first.  A browser is only launched when the
cache is missing, expired (> 6 h), or the cached cookies are rejected.

Endpoints used:
  - https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w  (announcements)
  - https://www.bseindia.com/xml-data/corpfiling/AttachHis/{file}     (PDF attachments)
  - https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w        (scrip list, public)

Config keys (under `bse:` in settings.yaml):
    scrip_list        - list of BSE scrip codes, e.g. [500209, 532540]
                        leave empty to pull all announcements for the date range
    start_date        - ISO date string lower bound (default "2023-01-01")
    end_date          - ISO date string upper bound (default today)
    categories        - list of announcement category codes (-1 = all, default)
    max_results       - max announcements per run (default 500)
    api_delay_seconds - seconds between requests (default 0.5)
    use_selenium      - bool, fall back to Selenium if Playwright is unavailable (default false)
    selenium_headless - bool, run browser headless (default true)
    cookie_ttl_hours  - how long cached Akamai cookies are trusted (default 6)
    cookie_cache_path - override cookie cache file path (default ~/.makrograph/bse_cookies.json)
"""

import json
import logging
import time
from datetime import datetime, timezone, date as _date, timedelta
from pathlib import Path
from typing import Optional

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

try:
    from curl_cffi import requests as _cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    _cffi_requests = None

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

BSE_API_BASE    = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachHis"
BSE_HOME        = "https://www.bseindia.com"
BSE_ANN_PAGE    = f"{BSE_HOME}/corporates/ann.html"
BSE_SCRIP_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

_BSE_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
)
_BSE_HEADERS = {
    "Origin":  BSE_HOME,
    "Referer": BSE_ANN_PAGE,
    "Accept":  "application/json, text/plain, */*",
}

_DEFAULT_COOKIE_CACHE = Path.home() / ".makrograph" / "bse_cookies.json"


class BSEFetcher(SourceAdapter):
    """Fetches BSE India corporate announcements, order wins, capex updates, and board decisions."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.scrip_list: list[str] = [str(s) for s in config.get("scrip_list", [])]
        self.start_date: str = config.get("start_date", "2023-01-01")
        self.end_date:   str = config.get(
            "end_date", _date.today().strftime("%Y-%m-%d")
        )
        self.categories:      list[str] = config.get("categories", ["-1"])
        self.api_delay:       float     = config.get("api_delay_seconds", 0.5)
        self.use_selenium:    bool      = config.get("use_selenium", False)
        self.selenium_headless: bool    = config.get("selenium_headless", True)
        self.cookie_ttl_hours: float    = config.get("cookie_ttl_hours", 6.0)
        self.cookie_cache_path: Path    = Path(
            config.get("cookie_cache_path", str(_DEFAULT_COOKIE_CACHE))
        )
        self._session_ready: bool = False
        self._driver = None         # Selenium driver (lazy)
        self._pw_browser = None     # Playwright browser (lazy)

        # ── Announcement importance filters ───────────────────────────────────
        # important_categories: whitelist of BSE CATEGORYNAME/SUBCATNAME values.
        # important_keywords:   any of these words in the subject keeps the item.
        # Both empty → keep all (backward-compatible).
        self.important_categories: set[str] = {
            c.lower() for c in config.get("important_categories", [])
        }
        self.important_keywords: list[str] = [
            k.lower() for k in config.get("important_keywords", [])
        ]

        self.session.headers.update({"User-Agent": _BSE_BROWSER_UA})

    # ── Cookie cache (disk) ────────────────────────────────────────────────

    def _save_cookies(self, cookies: dict) -> None:
        """Persist Akamai cookies to disk with a UTC expiry timestamp."""
        try:
            self.cookie_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(hours=self.cookie_ttl_hours)
                ).isoformat(),
                "cookies": cookies,
            }
            self.cookie_cache_path.write_text(json.dumps(payload))
            logger.debug(f"[bse_india] Cookies saved to {self.cookie_cache_path}")
        except Exception as exc:
            logger.debug(f"[bse_india] Cookie save failed: {exc}")

    def _load_cached_cookies(self) -> Optional[dict]:
        """Load cached cookies if they have not expired."""
        try:
            if not self.cookie_cache_path.exists():
                return None
            payload = json.loads(self.cookie_cache_path.read_text())
            expires_at = datetime.fromisoformat(payload["expires_at"])
            if datetime.now(timezone.utc) >= expires_at:
                logger.debug("[bse_india] Cached cookies expired")
                return None
            remaining = (expires_at - datetime.now(timezone.utc)).seconds // 60
            logger.info(f"[bse_india] Loaded cached cookies (valid for ~{remaining} more minutes)")
            return payload["cookies"]
        except Exception:
            return None

    def _inject_cookies(self, cookies: dict) -> None:
        """Inject a {name: value} cookie dict into the requests session."""
        for name, value in cookies.items():
            self.session.cookies.set(name, value)

    # ── Session warming ────────────────────────────────────────────────────

    def _warm_session(self) -> None:
        """Warm the BSE session using the best available strategy."""
        if self._session_ready:
            return

        # Strategy A: reuse disk-cached Akamai cookies
        cached = self._load_cached_cookies()
        if cached:
            self._inject_cookies(cached)
            self._session_ready = True
            return

        # Strategy B: curl_cffi TLS fingerprint (fastest, no browser)
        if _CURL_CFFI_AVAILABLE:
            if self._warm_via_curl_cffi():
                self._session_ready = True
                return

        # Strategy C: Playwright (lightweight, auto-downloads Chromium)
        if self._warm_via_playwright():
            self._session_ready = True
            return

        # Strategy D: Selenium (legacy)
        if self.use_selenium:
            if self._warm_via_selenium():
                self._session_ready = True
                return

        # Strategy E: plain requests warm (sets minimal cookies, usually not enough)
        try:
            self.session.get(BSE_HOME + "/", timeout=self.timeout)
            time.sleep(self.api_delay)
        except Exception as exc:
            logger.warning(f"[bse_india] Fallback session warm failed: {exc}")
        self._session_ready = True

    def _warm_via_curl_cffi(self) -> bool:
        """Try warming with curl_cffi Chrome TLS fingerprint (no browser needed).

        curl_cffi spoofs the Chrome TLS/HTTP-2 fingerprint.  BSE's Akamai
        currently requires JS-executed challenge cookies, so this usually returns
        empty results; but it's free (no browser launch) and works automatically
        if BSE ever switches to TLS-only bot detection.
        """
        try:
            cffi_session = _cffi_requests.Session(impersonate="chrome124")
            cffi_session.get(BSE_HOME + "/", timeout=self.timeout)
            time.sleep(0.8)
            cffi_session.get(BSE_ANN_PAGE, timeout=self.timeout)
            time.sleep(0.8)
            # Inject cookies into the requests session
            jar_dict = dict(cffi_session.cookies)
            if jar_dict:
                self._inject_cookies(jar_dict)
                self._save_cookies(jar_dict)
                logger.info(f"[bse_india] curl_cffi warmed: {len(jar_dict)} cookies")
                return True
            logger.debug("[bse_india] curl_cffi returned no cookies (JS challenge required)")
            return False
        except Exception as exc:
            logger.debug(f"[bse_india] curl_cffi warm failed: {exc}")
            return False

    def _warm_via_playwright(self) -> bool:
        """Warm session via Playwright Chromium (headless, no ChromeDriver needed).

        Playwright auto-downloads its own pinned Chromium binary the first time
        `playwright install chromium` is run.  It injects the resulting Akamai
        cookies into the shared requests session and caches them to disk.

        Install:
            pip install playwright
            playwright install chromium
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.debug("[bse_india] playwright not installed — skipping (pip install playwright && playwright install chromium)")
            return False

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=self.selenium_headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = browser.new_context(
                    user_agent=_BSE_BROWSER_UA,
                    locale="en-IN",
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()

                logger.info("[bse_india] Playwright: visiting BSE homepage …")
                page.goto(BSE_HOME, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(3_000)

                logger.info("[bse_india] Playwright: visiting announcements page …")
                page.goto(BSE_ANN_PAGE, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(4_000)

                cookies_list = context.cookies()
                browser.close()

            jar_dict = {c["name"]: c["value"] for c in cookies_list}
            if jar_dict:
                self._inject_cookies(jar_dict)
                self._save_cookies(jar_dict)
                logger.info(f"[bse_india] Playwright warm complete: {len(jar_dict)} cookies cached")
                return True

            logger.warning("[bse_india] Playwright returned no cookies")
            return False

        except Exception as exc:
            logger.warning(f"[bse_india] Playwright warm failed: {exc}")
            return False

    def _warm_via_selenium(self) -> bool:
        """Legacy Selenium warm-up (ChromeDriver required)."""
        driver = self._get_selenium_driver()
        if not driver:
            return False
        try:
            logger.info("[bse_india] Selenium: visiting BSE homepage …")
            driver.get(BSE_HOME)
            time.sleep(4)
            driver.get(BSE_ANN_PAGE)
            try:
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.common.by import By
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except Exception:
                pass
            time.sleep(4)
            jar_dict = {c["name"]: c["value"] for c in driver.get_cookies()}
            self._inject_cookies(jar_dict)
            self._save_cookies(jar_dict)
            logger.info(f"[bse_india] Selenium warm complete: {len(jar_dict)} cookies cached")
            return True
        except Exception as exc:
            logger.warning(f"[bse_india] Selenium warm failed: {exc}")
            return False

    def _get_selenium_driver(self):
        """Lazily initialise a Selenium ChromeDriver."""
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
            opts.add_argument(f"--user-agent={_BSE_BROWSER_UA}")
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                self._driver = webdriver.Chrome(
                    service=Service(ChromeDriverManager().install()), options=opts
                )
            except Exception:
                self._driver = webdriver.Chrome(options=opts)
            logger.info("[bse_india] Selenium ChromeDriver initialized")
            return self._driver
        except Exception as exc:
            logger.error(f"[bse_india] Selenium init failed: {exc}")
            return None

    def __exit__(self, *args):
        """Clean up browser instances on context exit."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
        if self._pw_browser:
            try:
                self._pw_browser.close()
            except Exception:
                pass
        super().__exit__(*args)

    # ── Property ───────────────────────────────────────────────────────────

    @property
    def source_name(self) -> str:
        return "bse_india"

    # ── API helpers ────────────────────────────────────────────────────────

    def _to_bse_date(self, iso_str: str) -> str:
        """Convert ISO date 'YYYY-MM-DD' → BSE API format 'YYYYMMDD'."""
        return iso_str.replace("-", "")

    # ── High-value BSE categories that work without Akamai cookies ────────
    # BSE blocks strCat=-1 (all categories in one shot) via Akamai JS challenge.
    # However, per-category queries bypass the protection entirely — confirmed
    # live: each of these returns 50 items per date window with NO cookie auth.
    _COOKIE_FREE_CATEGORIES: tuple = (
        "Board Meeting",           # board meeting outcomes, capex, fundraise
        "Result",                  # quarterly/annual financial results
        "Corp. Action",            # dividends, buybacks, rights issues
        "AGM/EGM",                 # annual/extraordinary general meetings
        "Scheme of Arrangement",   # mergers, demergers, restructuring
        "Acquisition",             # M&A events
        "Amalgamation",            # company amalgamations
        "Analysts/Institutional Investor Meet",  # investor calls & transcripts
        "Press Release",           # PR announcements
    )

    def _fetch_category_window(
        self,
        category: str,
        from_date: str,
        to_date: str,
        scrip: str = "",
    ) -> list[dict]:
        """Fetch one page of BSE announcements for a specific category + date window.

        BSE's AnnSubCategoryGetData endpoint returns up to 50 items per request.
        Per-category queries bypass Akamai (no cookies needed); only strCat=-1
        (all-categories) is blocked by the JS challenge.

        Args:
            category:  BSE CATEGORYNAME string, e.g. "Board Meeting", "Result"
            from_date: YYYYMMDD start (inclusive)
            to_date:   YYYYMMDD end (inclusive)
            scrip:     optional BSE scrip code to filter (empty = all companies)
        """
        url = f"{BSE_API_BASE}/AnnSubCategoryGetData/w"
        params = {
            "strCat":      category,
            "strPrevDate": from_date,
            "strScrip":    scrip,
            "strSearch":   "P",
            "strToDate":   to_date,
            "strType":     "C",
            "subcategory": "-1",
        }
        bse_headers = {
            **_BSE_HEADERS,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }
        try:
            resp = self.session.get(url, params=params, headers=bse_headers, timeout=self.timeout)
            resp.raise_for_status()
            text = resp.text.strip() if resp.content else ""
            if not text or text in ("{}", "[]"):
                return []
            data = resp.json()
            if isinstance(data, dict):
                return data.get("Table") or data.get("data") or data.get("Result") or []
            if isinstance(data, list):
                return data
            return []
        except Exception as exc:
            logger.warning(f"[bse_india] Category '{category}' window {from_date}-{to_date} failed: {exc}")
            return []

    def _fetch_by_categories(self, scrip: str = "") -> list[dict]:
        """Fetch announcements across high-value categories using date-window chunking.

        The BSE API returns at most 50 items per request. To paginate we chunk the
        date range into weekly windows (7-day slices) and query each category
        per window. This is the same approach NSE uses for bulk ingestion.

        Returns:
            De-duplicated list of raw announcement dicts (keyed on NEWSID).
        """
        from datetime import datetime as _dt, timedelta as _td

        start = _dt.strptime(self.start_date, "%Y-%m-%d").date()
        end   = _dt.strptime(self.end_date,   "%Y-%m-%d").date()
        total_days = (end - start).days + 1
        total_windows = (total_days + 6) // 7
        logger.info(
            f"[bse_india] Date range: {start} → {end} "
            f"({total_days} days, ~{total_windows} weekly windows × {len(self._COOKIE_FREE_CATEGORIES)} categories)"
        )

        seen_ids: set = set()
        all_items: list[dict] = []

        # Chunk in 7-day windows so each window << 50 items per category
        window_days = 7
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + _td(days=window_days - 1), end)
            from_str  = chunk_start.strftime("%Y%m%d")
            to_str    = chunk_end.strftime("%Y%m%d")

            for cat in self._COOKIE_FREE_CATEGORIES:
                items = self._fetch_category_window(cat, from_str, to_str, scrip)
                for item in items:
                    news_id = item.get("NEWSID") or item.get("NEWSID", "")
                    if news_id and news_id not in seen_ids:
                        seen_ids.add(news_id)
                        all_items.append(item)
                if items:
                    self._throttle()

            chunk_start = chunk_end + _td(days=1)

        logger.info(f"[bse_india] Category-sweep fetched {len(all_items)} unique items "
                    f"({self.start_date} → {self.end_date})")
        return all_items

    def _fetch_announcements_page(self, scrip: str = "") -> list[dict]:
        """Fetch BSE announcements: try cookie-free category sweep first.

        The category-by-category sweep works without any Akamai cookies.
        Falls back to the warmed-session all-categories query only if scrip
        is specified (single-company lookups can still succeed without Akamai
        for specific scrip codes).
        """
        # Strategy A: cookie-free per-category sweep (works for all market-wide queries)
        if not scrip:
            items = self._fetch_by_categories()
            if items:
                return items
            # If category sweep returns nothing (network issue etc), fall through
            logger.debug("[bse_india] Category sweep returned 0 items, trying scrip-specific fallback")

        # Strategy B: specific scrip query (often works without cookies)
        if scrip:
            items = self._fetch_category_window(
                category="-1",
                from_date=self._to_bse_date(self.start_date),
                to_date=self._to_bse_date(self.end_date),
                scrip=scrip,
            )
            if items:
                return items

        # Strategy C: warmed session (Playwright/cached cookies) for edge cases
        self._warm_session()
        url = f"{BSE_API_BASE}/AnnSubCategoryGetData/w"
        params = {
            "strCat":      "-1",
            "strPrevDate": self._to_bse_date(self.start_date),
            "strScrip":    scrip,
            "strSearch":   "P",
            "strToDate":   self._to_bse_date(self.end_date),
            "strType":     "C",
            "subcategory": "-1",
        }
        bse_headers = {
            **_BSE_HEADERS,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }
        try:
            resp = self.session.get(url, params=params, headers=bse_headers, timeout=self.timeout)
            resp.raise_for_status()
            text = resp.text.strip() if resp.content else ""
            if not text or text in ("{}", "[]"):
                logger.warning(
                    "[bse_india] All-categories API still empty after session warm-up. "
                    "Akamai JS challenge requires a real interactive browser session. "
                    "Category-sweep should cover most high-value filings regardless."
                )
                return []
            data = resp.json()
            if isinstance(data, dict):
                return data.get("Table") or data.get("data") or data.get("Result") or []
            if isinstance(data, list):
                return data
            return []
        except Exception as exc:
            logger.error(f"[bse_india] All-categories API failed: {exc}")
            return []

    def _build_attachment_url(self, filename: str) -> str:
        """Resolve BSE ATTACHMENTNAME to a fully qualified PDF URL.

        BSE uses two path bases depending on filing age:
          - AttachLive  → current/recent filings (primary for new API data)
          - AttachHis   → historical/archived filings (fallback on 404)

        The ATTACHMENTNAME field from the API is one of:
          a) UUID with extension:  "692815d2-2636-4325-babd-6c405e98eb88.pdf"
             → AttachLive/692815d2-2636-4325-babd-6c405e98eb88.pdf
          b) Bare UUID (no ext):   "692815d2-2636-4325-babd-6c405e98eb88"
             → AttachLive/692815d2-2636-4325-babd-6c405e98eb88.pdf
          c) Legacy path:          "Attachments/oldFile.pdf"
             → AttachHis/oldFile.pdf  (old filings, no AttachLive copy)

        We always build an AttachLive URL first. run_pdf_fetch_india retries
        with AttachHis on 404 automatically.
        """
        if not filename:
            return ""
        if filename.startswith("http"):
            return filename
        fname = filename.strip().lstrip("/")
        import re as _re
        _ATTACH_LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive"

        # Case a: UUID.pdf — most common format from current API
        if _re.fullmatch(r"[0-9a-f\-]{36}\.pdf", fname, _re.IGNORECASE):
            return f"{_ATTACH_LIVE}/{fname.lower()}"

        # Case b: bare UUID (no extension)
        if _re.fullmatch(r"[0-9a-f\-]{36}", fname, _re.IGNORECASE):
            return f"{_ATTACH_LIVE}/{fname.lower()}.pdf"

        # Case c: legacy path-style (e.g. "Attachments/annex123.pdf")
        return f"{BSE_ATTACH_BASE}/{fname}"

    def _parse_bse_dt(self, dt_str: str) -> Optional[datetime]:
        """Parse BSE datetime string into UTC-aware datetime."""
        if not dt_str:
            return None
        dt_str = dt_str.strip()
        for fmt in (
            "%m/%d/%Y %H:%M:%S %p",
            "%m/%d/%Y %I:%M:%S %p",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%d-%m-%Y %H:%M:%S",
            "%m/%d/%Y",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    # ── Main entry point ───────────────────────────────────────────────────

    def discover(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[SourceDocument]:
        """Discover BSE corporate announcements in the [since, until] window.

        Args:
            since: Lower bound (checkpoint). Overrides config start_date if more recent.
            until: Upper bound from caller (UI end_date). Overrides config end_date.
        """
        if since:
            self.start_date = since.strftime("%Y-%m-%d")
            logger.info(f"[bse_india] start_date = {self.start_date} (from UI / checkpoint)")
        else:
            logger.info(f"[bse_india] start_date = {self.start_date} (from config default)")

        if until is not None:
            self.end_date = until.strftime("%Y-%m-%d")
        if self.end_date < self.start_date:
            self.end_date = _date.today().strftime("%Y-%m-%d")

        if self.scrip_list:
            raw_items: list[dict] = []
            for scrip in self.scrip_list:
                raw_items.extend(self._fetch_announcements_page(scrip))
                self._throttle()
        else:
            raw_items = self._fetch_announcements_page()

        logger.info(f"[bse_india] Raw items from API: {len(raw_items)}")

        docs: list[SourceDocument] = []
        for item in raw_items[: self.max_results]:
            try:
                pub_dt = self._parse_bse_dt(
                    item.get("NEWS_DT") or item.get("DissemDT") or ""
                )
                if since and pub_dt and pub_dt <= since:
                    continue

                filename = (
                    item.get("ATTACHMENTNAME")
                    or item.get("FILENAME")
                    or item.get("attachmentName")
                    or ""
                )
                url = self._build_attachment_url(filename)
                if not url:
                    continue

                subject   = (item.get("NEWSSUB") or item.get("HEADLINE") or item.get("subject") or "")
                company   = (item.get("SLONGNAME") or item.get("COMPANYLNAME") or item.get("company") or "")
                scrip_code = str(item.get("SCRIP_CD") or item.get("SECURITYCODE") or "")
                category_raw = (
                    item.get("CATEGORYNAME") or item.get("SUBCATNAME") or ""
                ).strip()

                # ── Importance filter ─────────────────────────────────────────
                if self.important_categories or self.important_keywords:
                    category_match = bool(
                        self.important_categories
                        and category_raw.lower() in self.important_categories
                    )
                    keyword_match = bool(
                        self.important_keywords
                        and any(k in subject.lower() for k in self.important_keywords)
                    )
                    if not category_match and not keyword_match:
                        continue

                docs.append(SourceDocument(
                    url=url,
                    title=subject[:500],
                    doc_type="announcement",
                    source_name=self.source_name,
                    published_at=pub_dt,
                    company=company,
                    ticker=scrip_code,
                    filing_type=_classify_bse_subject(subject),
                    metadata={
                        "exchange":   "BSE",
                        "country":    "IN",
                        "scrip_code": scrip_code,
                        "category":   item.get("CATEGORYNAME") or item.get("SUBCATNAME") or "",
                    },
                ))
            except Exception as exc:
                logger.debug(f"[bse_india] Item parse error: {exc}")

        if not docs:
            logger.info(
                "[bse_india] No announcements found via category sweep. "
                "Falling back to BSE scrip list for company metadata."
            )
            docs = self._discover_from_scrip_list()

        logger.info(f"[bse_india] Discovered {len(docs)} documents")
        return docs

    def _discover_from_scrip_list(self) -> list[SourceDocument]:
        """Fallback: build SourceDocuments from the public BSE scrip list API.

        This endpoint requires no cookies and returns all active equity scrips.
        """
        try:
            self._throttle()
            resp = self.session.get(
                BSE_SCRIP_LIST_URL,
                headers={"Referer": BSE_HOME + "/"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list):
                return []
        except Exception as exc:
            logger.warning(f"[bse_india] Scrip list fetch failed: {exc}")
            return []

        scrip_set = set(self.scrip_list)
        docs: list[SourceDocument] = []
        for item in items:
            code = str(item.get("SCRIP_CD") or "").strip()
            name = (item.get("Scrip_Name") or "").strip()
            if not code or not name:
                continue
            if scrip_set and code not in scrip_set:
                continue
            url = (
                f"https://www.bseindia.com/stock-share-price/"
                f"{name.lower().replace(' ', '-')}/{code}/"
            )
            docs.append(SourceDocument(
                url=url,
                title=f"BSE Company: {name} ({code})",
                doc_type="company_info",
                source_name=self.source_name,
                published_at=None,
                company=name,
                ticker=code,
                filing_type="company_info",
                metadata={
                    "exchange":   "BSE",
                    "country":    "IN",
                    "scrip_code": code,
                    "group":      item.get("GROUP") or "",
                    "source":     "scrip_list_fallback",
                },
            ))
            if len(docs) >= self.max_results:
                break

        logger.debug(f"[bse_india] Scrip list fallback: {len(docs)} entries")
        return docs


def _classify_bse_subject(subject: str) -> str:
    """Derive a coarse filing type label from a BSE announcement subject line."""
    if not subject:
        return "announcement"
    s = subject.lower()
    if any(w in s for w in ("order", "contract", "win")):
        return "order_win"
    if any(w in s for w in ("capex", "capital expenditure", "expansion")):
        return "capex_update"
    if any(w in s for w in ("board", "meeting", "dividend", "result")):
        return "board_decision"
    if any(w in s for w in ("acquisition", "merger", "joint venture", "jv")):
        return "corporate_action"
    if any(w in s for w in ("annual report", "agm", "egm")):
        return "annual_report"
    return "announcement"
