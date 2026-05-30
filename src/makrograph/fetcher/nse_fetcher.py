"""NSE India corporate announcements and filings fetcher.

Fetch strategy
--------------
Primary: curl_cffi Chrome-fingerprint (recommended — bypasses Akamai bot detection)
    Uses curl_cffi.requests with impersonate="chrome124" to spoof a real Chrome
    TLS fingerprint.  Install: pip install curl_cffi

Fallback: plain requests + manual session warm-up
    Visits NSE homepage + filings page to acquire Akamai session cookies
    (AKA_A2, bm_sz).  Less reliable — NSE's bot-detection may still block.

Both paths call the date-range bulk endpoint (one call per month-chunk):
    GET /api/corporate-announcements?index=equities
                                    &from_date=<DD-MM-YYYY>
                                    &to_date=<DD-MM-YYYY>

    Returns all companies' announcements for the window — ~70-80 calls for a
    full 2020→today backfill; 1 call for a weekly incremental run.

    If API returns empty (session blocked), the fetcher logs a clear warning
    and returns [].  No per-symbol loop or equity-CSV fallback — those only
    return recent-data or metadata, both useless for historical replay.

Endpoints used:
  - https://www.nseindia.com/api/corporate-announcements?index=equities
                                                         &from_date=DD-MM-YYYY
                                                         &to_date=DD-MM-YYYY
  - https://nsearchives.nseindia.com/corporate/ANNOUNCEMENTS/<file>

Config keys (under `nse:` in settings.yaml):
    symbol_list          - NSE symbols e.g. [INFY, TCS]
                           If set, ONLY these symbols are kept from the bulk fetch.
                           Empty (default) = all listed companies — recommended.
    announcement_type    - "equities" | "debt" | "sme"  (default: equities)
    start_date           - ISO date string, fetch docs from this date (default "2020-01-01")
    end_date             - ISO date string, fetch docs up to this date (default: today)
    max_results_per_run  - max total docs per run; 0 = unlimited  (default: 0)
    api_delay_seconds    - seconds between month-chunk requests (default 0.5)
    important_categories - whitelist of NSE categoryDesc values (see settings.yaml)
    important_keywords   - subject-line keyword filter (see settings.yaml)
"""

import logging
import time
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

try:
    from curl_cffi import requests as _cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    _cffi_requests = None

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

NSE_BASE             = "https://www.nseindia.com"
NSE_ARCHIVES_BASE    = "https://nsearchives.nseindia.com"

NSE_ANNOUNCEMENTS_URL = f"{NSE_BASE}/api/corporate-announcements"

_NSE_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
)

_ANNOUNCE_REFERER = f"{NSE_BASE}/companies-listing/corporate-filings-announcements"

_NSE_HEADERS = {
    "accept":                   "application/json, text/plain, */*",
    "accept-language":          "en-US,en;q=0.9,en-IN;q=0.8",
    "cache-control":            "no-cache",
    "referer":                  _ANNOUNCE_REFERER,
    "sec-ch-ua":                '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
    "sec-ch-ua-mobile":         "?0",
    "sec-ch-ua-platform":       '"Windows"',
    "sec-fetch-dest":           "empty",
    "sec-fetch-mode":           "cors",
    "sec-fetch-site":           "same-origin",
    "user-agent":               _NSE_BROWSER_UA,
}

# Headers for the initial browser-like warm-up GETs
_NSE_WARM_HEADERS = {
    "accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language":          "en-US,en;q=0.9",
    "cache-control":            "max-age=0",
    "sec-fetch-dest":           "document",
    "sec-fetch-mode":           "navigate",
    "sec-fetch-site":           "none",
    "sec-fetch-user":           "?1",
    "upgrade-insecure-requests":"1",
    "user-agent":               _NSE_BROWSER_UA,
}


class NSEFetcher(SourceAdapter):
    """Fetches NSE India corporate announcements for equity, debt, and SME segments."""

    def __init__(self, config: dict):
        super().__init__(config)
        # Optional symbol filter — keeps only these tickers from bulk responses.
        # Empty = no filter (all listed companies kept).
        self.symbol_list: list[str] = [s.upper() for s in config.get("symbol_list", [])]
        self.symbol_set:  set[str]  = set(self.symbol_list)

        self.announcement_type: str = config.get("announcement_type", "equities")
        self.start_date: str        = config.get("start_date", "2020-01-01")
        self.end_date: str          = config.get("end_date", _date.today().strftime("%Y-%m-%d"))
        self.api_delay: float       = config.get("api_delay_seconds", 0.5)
        self.fetch_company_info: bool = config.get("fetch_company_info", False)
        self.max_results: int       = config.get("max_results_per_run", 0)

        # ── Announcement importance filters ───────────────────────────────────
        # important_categories: whitelist of NSE categoryDesc values.
        #   Empty list → no category filter (keep all).
        # important_keywords:   any of these words in the subject keeps the doc,
        #   even when its category is not in the whitelist.
        #   Empty list → no keyword filter.
        # An item is KEPT if category matches OR keyword matches.
        # Both empty → keep everything (backward-compatible default).
        self.important_categories: set[str] = {
            c.lower() for c in config.get("important_categories", [])
        }
        self.important_keywords: list[str] = [
            k.lower() for k in config.get("important_keywords", [])
        ]
        # Hard exclude list — these categories are ALWAYS dropped even if a keyword
        # matches in their text (e.g. "Trading Window" contains "win", "Record Date"
        # for dividend mentions "dividend" — both are compliance noise, not signals).
        self.excluded_categories: set[str] = {
            c.lower() for c in config.get("excluded_categories", [])
        }

        self._session_ready: bool   = False

        self.session.headers.update(_NSE_HEADERS)

    @property
    def source_name(self) -> str:
        return "nse_india"

    # ------------------------------------------------------------------
    # SESSION / REQUEST HELPERS
    # ------------------------------------------------------------------

    def _warm_session(self):
        """Obtain NSE session cookies by mimicking a real browser visit.

        NSE requires visiting the homepage + filings page to set bot-detection
        cookies (AKA_A2, bm_sz, ak_bmsc).  Must be done before any API call.
        Skipped when curl_cffi is available (handles cookies automatically).
        """
        if self._session_ready or _CURL_CFFI_AVAILABLE:
            self._session_ready = True
            return
        # Visit homepage + filings page to acquire Akamai cookies
        for url in [NSE_BASE, _ANNOUNCE_REFERER]:
            try:
                self.session.get(url, headers=_NSE_WARM_HEADERS, timeout=self.timeout)
                time.sleep(1.0)   # Give Akamai time to issue cookies
            except Exception:
                pass
        self._session_ready = True

    def _get_json(self, url: str, params: dict) -> list:
        """Make a GET request and return parsed JSON list.

        Strategy order:
          1. curl_cffi with Chrome impersonation (bypasses Akamai TLS fingerprint check)
          2. Plain requests session (may be blocked by Akamai for historical dates)
        """
        # Strategy 1: curl_cffi — Chrome TLS fingerprint
        if _CURL_CFFI_AVAILABLE:
            try:
                resp = _cffi_requests.get(
                    url,
                    params=params,
                    headers=_NSE_HEADERS,
                    impersonate="chrome124",
                    timeout=self.timeout,
                )
                if resp.status_code == 200 and resp.content:
                    data = resp.json()
                    if isinstance(data, list):
                        return data
                    return (
                        data.get("data") or data.get("announcements")
                        or data.get("corpAnnouncements") or []
                    )
                logger.debug(f"[nse_india] curl_cffi HTTP {resp.status_code} for {url}")
            except Exception as exc:
                logger.debug(f"[nse_india] curl_cffi failed: {exc}")

        # Strategy 2: plain requests with warm-up cookies
        self._warm_session()
        try:
            resp = self.session.get(url, params=params, headers=_NSE_HEADERS, timeout=self.timeout)
            if resp.status_code == 200 and resp.content:
                data = resp.json()
                if isinstance(data, list):
                    return data
                return (
                    data.get("data") or data.get("announcements")
                    or data.get("corpAnnouncements") or []
                )
            logger.debug(f"[nse_india] requests HTTP {resp.status_code} for {url}")
        except Exception as exc:
            logger.debug(f"[nse_india] requests failed: {exc}")

        return []

    # ------------------------------------------------------------------
    # DATE-CHUNK GENERATOR
    # ------------------------------------------------------------------

    def _generate_month_chunks(
        self,
        start_dt: _date,
        end_dt: _date,
    ) -> list[tuple[str, str]]:
        """Generate (from_date, to_date) pairs in monthly windows.

        NSE's bulk endpoint returns all companies' announcements for a given date
        window.  We use monthly chunks (not yearly) so each response stays small
        and NSE's rate-limiter is happy.

        Dates are in DD-MM-YYYY format required by the NSE API.

        Example (2020-01-01 → 2020-03-15):
            [("01-01-2020", "31-01-2020"),
             ("01-02-2020", "29-02-2020"),
             ("01-03-2020", "15-03-2020")]
        """
        import calendar
        chunks: list[tuple[str, str]] = []
        cur = _date(start_dt.year, start_dt.month, 1)

        while cur <= end_dt:
            _, last_day = calendar.monthrange(cur.year, cur.month)
            chunk_end = min(_date(cur.year, cur.month, last_day), end_dt)
            chunks.append((cur.strftime("%d-%m-%Y"), chunk_end.strftime("%d-%m-%Y")))

            # Advance to first of next month
            if cur.month == 12:
                cur = _date(cur.year + 1, 1, 1)
            else:
                cur = _date(cur.year, cur.month + 1, 1)

        return chunks

    # ------------------------------------------------------------------
    # BULK DATE-RANGE API (primary path)
    # ------------------------------------------------------------------

    def _fetch_bulk_date_range(self, from_date: str, to_date: str) -> list[dict]:
        """Fetch all-company announcements for a date window via bulk endpoint."""
        return self._get_json(
            NSE_ANNOUNCEMENTS_URL,
            {"index": self.announcement_type, "from_date": from_date, "to_date": to_date},
        )

    # ------------------------------------------------------------------
    # PER-SYMBOL FALLBACK
    # ------------------------------------------------------------------

    def _fetch_announcements_for_symbol(self, symbol: str) -> list[dict]:
        """Per-symbol fetch — most-recent ~100-300 announcements for one ticker.

        NOTE: Does NOT pass date params so returns only recent announcements.
        Used only for incremental (live) single-symbol runs, not historical replay.
        """
        return self._get_json(
            NSE_ANNOUNCEMENTS_URL,
            {"index": self.announcement_type, "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # DOCUMENT BUILDER
    # ------------------------------------------------------------------

    def _resolve_attachment_url(self, item: dict) -> str:
        attachment = (
            item.get("attchmntFile")
            or item.get("attchmnt")
            or item.get("attachment")
            or item.get("fileUrl")
            or ""
        )
        if not attachment:
            return ""
        if attachment.startswith("http"):
            return attachment
        if attachment.startswith("/"):
            return f"{NSE_BASE}{attachment}"
        return f"{NSE_ARCHIVES_BASE}/corporate/ANNOUNCEMENTS/{attachment}"

    def _item_to_doc(
        self,
        item: dict,
        since: Optional[datetime],
        start_dt: Optional[datetime],
    ) -> Optional[SourceDocument]:
        """Convert a raw NSE API item to a SourceDocument, or None if filtered out."""
        try:
            pub_dt = _parse_nse_dt(
                item.get("an_dt") or item.get("exchdisstime") or item.get("dt") or ""
            )
            # Hard lower bound from config start_date
            if pub_dt and start_dt and pub_dt < start_dt:
                return None
            # Checkpoint lower bound — skip docs we already have
            if since and pub_dt and pub_dt <= since:
                return None

            url = self._resolve_attachment_url(item)
            if not url:
                return None

            symbol = (item.get("symbol") or "").upper()

            # Symbol filter — only when a curated list is configured
            if self.symbol_set and symbol not in self.symbol_set:
                return None

            # NSE API field mapping (verified from live API):
            #   desc         → announcement TYPE / category  (e.g. "Outcome of Board Meeting")
            #   attchmntText → brief description of what the announcement says
            #   sm_name      → company name
            #   attchmntFile → full URL to the PDF attachment
            category_raw = (item.get("desc") or "").strip()
            subject_raw  = (item.get("attchmntText") or "").strip()

            # ── Importance filter ─────────────────────────────────────────────
            # Step 1: Hard exclude — always drop these category types (compliance
            #         noise that accidentally contains signal keywords, e.g.
            #         "Trading Window" contains "win", "Record Date" mentions dividend).
            if self.excluded_categories and category_raw.lower() in self.excluded_categories:
                return None

            # Step 2: Keep if category whitelist OR keyword matches.
            #         Both lists empty → keep all (no filter).
            if self.important_categories or self.important_keywords:
                category_match = bool(
                    self.important_categories
                    and category_raw.lower() in self.important_categories
                )
                keyword_match = bool(
                    self.important_keywords
                    and any(k in subject_raw.lower() for k in self.important_keywords)
                )
                if not category_match and not keyword_match:
                    return None

            # Use attchmntText (the actual announcement description) as title.
            # Fall back to category name if text is empty.
            title = (subject_raw or category_raw or "NSE Announcement")[:500]

            return SourceDocument(
                url=url,
                title=title,
                doc_type="announcement",
                source_name=self.source_name,
                published_at=pub_dt,
                company=(item.get("sm_name") or item.get("comp") or ""),
                ticker=symbol,
                filing_type=category_raw or "NSE Announcement",
                metadata={
                    "exchange":    "NSE",
                    "country":     "IN",
                    "symbol":      symbol,
                    "nse_item_id": str(item.get("seq_id") or item.get("id") or ""),
                    "category":    category_raw,
                    "industry":    item.get("smIndustry") or "",
                },
            )
        except Exception as exc:
            logger.debug(f"[nse_india] Item parse error: {exc}")
            return None

    # ------------------------------------------------------------------
    # MAIN DISCOVERY
    # ------------------------------------------------------------------

    def discover(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[SourceDocument]:
        """Discover NSE corporate announcements for all companies across a date range.

        Window resolution (lower bound):
          max(config start_date, since checkpoint)  — whichever is more recent.

        Window resolution (upper bound) — first match wins:
          1. `until` argument from caller (UI end_date / replay window_end)
          2. config `end_date` if explicitly set
          3. today (default for weekly incremental runs)

        Primary path — bulk date-range fetch
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Splits the window into MONTHLY chunks. Each chunk issues one API call:
            GET /api/corporate-announcements?index=equities
                                            &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
        Returns all companies' announcements — no per-symbol loop.
        ~70 calls for a full 2020→today backfill; 1 call for a weekly run.

        Fallback: per-symbol loop → equity CSV (if bulk returns nothing).
        """
        start_dt = _parse_date_filter(self.start_date)

        # ── Lower bound ──────────────────────────────────────────────────────
        if since and start_dt and since > start_dt:
            window_start_dt = since.date()
        elif start_dt:
            window_start_dt = start_dt.date()
        else:
            window_start_dt = _date(2020, 1, 1)

        # ── Upper bound — UI until > config end_date > today ─────────────────
        if until is not None:
            window_end_dt = until.date()
        else:
            try:
                window_end_dt = datetime.strptime(self.end_date, "%Y-%m-%d").date()
            except Exception:
                window_end_dt = _date.today()
        # Never fetch into the future
        window_end_dt = min(window_end_dt, _date.today())

        chunks = self._generate_month_chunks(window_start_dt, window_end_dt)
        unlimited = (self.max_results == 0)

        logger.info(
            f"[nse_india] Bulk date-range fetch: {window_start_dt} → {window_end_dt} "
            f"({len(chunks)} monthly chunks, "
            f"{'unlimited' if unlimited else self.max_results} doc cap, "
            f"{'all companies' if not self.symbol_set else f'{len(self.symbol_set)} symbols'})"
        )

        docs: list[SourceDocument] = []
        seen_urls: set[str] = set()
        chunks_with_data = 0
        chunks_empty = 0

        for idx, (from_date, to_date) in enumerate(chunks, 1):
            if not unlimited and len(docs) >= self.max_results:
                break

            items = self._fetch_bulk_date_range(from_date, to_date)

            if items:
                chunks_with_data += 1
            else:
                chunks_empty += 1

            new_in_chunk = 0
            filtered_out = 0
            for item in items:
                doc = self._item_to_doc(item, since, start_dt)
                if doc is None:
                    filtered_out += 1
                    continue
                if doc.url not in seen_urls:
                    seen_urls.add(doc.url)
                    docs.append(doc)
                    new_in_chunk += 1
                if not unlimited and len(docs) >= self.max_results:
                    break

            logger.info(
                f"[nse_india] Chunk {idx}/{len(chunks)}: "
                f"{from_date} → {to_date} | "
                f"{len(items)} raw → {new_in_chunk} kept, {filtered_out} filtered "
                f"(total {len(docs)})"
            )

            if idx < len(chunks):
                time.sleep(self.api_delay)

        # ── If every chunk returned empty — session was blocked ───────────────
        if chunks_empty == len(chunks) and not docs:
            _cffi_hint = "" if _CURL_CFFI_AVAILABLE else " Install curl_cffi for reliable access: pip install curl_cffi"
            logger.warning(
                f"[nse_india] NSE bulk API returned empty for ALL {len(chunks)} chunks "
                f"({window_start_dt} → {window_end_dt}). "
                f"Likely causes: (1) NSE Akamai bot-detection blocked the session, "
                f"(2) date range is too far in the past for the live API.{_cffi_hint}"
            )
        elif chunks_empty > 0:
            logger.info(
                f"[nse_india] {chunks_with_data}/{len(chunks)} chunks had data "
                f"({chunks_empty} empty — possible bot-detection on those windows)"
            )

        logger.info(f"[nse_india] Discovered {len(docs)} documents total")
        return docs



# ------------------------------------------------------------------
# DATE HELPERS
# ------------------------------------------------------------------

def _parse_nse_dt(dt_str: str) -> Optional[datetime]:
    """Parse NSE datetime strings into timezone-aware UTC datetime."""
    if not dt_str:
        return None
    dt_str = dt_str.strip()
    for fmt in [
        "%d-%b-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d-%b-%Y",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    ]:
        try:
            return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_date_filter(date_str: str) -> Optional[datetime]:
    """Parse a plain ISO date string like '2020-01-01' into a UTC datetime."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
