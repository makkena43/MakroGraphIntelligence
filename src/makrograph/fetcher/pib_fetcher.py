"""Press Information Bureau (PIB) India fetcher.

Fetches government press releases covering:
  - PLI (Production Linked Incentive) schemes
  - Semiconductor Mission
  - Power & Renewable Energy
  - Railways & Metro
  - Defence & Aerospace
  - Electric Vehicles (EV) and Auto
  - Infrastructure and Capex announcements

Strategy:
  1. Poll PIB RSS feeds (feedparser) for latest press releases
  2. Filter by ministry and keyword relevance
  3. Fetch individual release HTML (requests + BeautifulSoup) for full text

PIB RSS feeds:
  Note: ModId=6&Lang=1&Regid=3 serves HINDI content regardless of Lang param.
  English press releases require either:
    a) Ministry-specific RSS feeds (confirm ModId per ministry)
    b) Selenium scraping of https://pib.gov.in/ (use_selenium: true)
  The default feeds below target Hindi content and will return 0 English docs
  when run from outside India. Set use_selenium: true in config for full English access.

Config keys (under `pib:` in settings.yaml):
    rss_feeds         - list of RSS feed URLs (defaults to the standard English feed)
    keywords          - list of keyword strings to filter releases (case-insensitive)
    start_date        - ISO date, skip releases older than this (default "2023-01-01")
    max_results       - max releases per run (default 300)
    fetch_full_text   - bool, download full HTML of each release (default true)
    api_delay_seconds - seconds between requests (default 0.5)
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

try:
    import feedparser
    _FEEDPARSER_AVAILABLE = True
except ImportError:
    _FEEDPARSER_AVAILABLE = False
    feedparser = None

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False
    BeautifulSoup = None

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

PIB_BASE = "https://pib.gov.in"

# PIB strategy: allRel.aspx is JS-rendered — cannot scrape directly.
# Two modes:
#   Recent (default): RSS feeds → collect PRIDs → fetch English pages.
#                     Fast, covers last ~20 releases per feed.
#   Historical:       Parallel PRID range scan from start_date anchor to now.
#                     Step=40 with 8 workers → ~5 min for 2020–present.
_PIB_SHARE_URL   = "https://pib.gov.in/PressReleasePage.aspx"
_PIB_MAX_PAGES   = 10   # kept for compat
_PIB_SCAN_STEP    = 200   # sample every 200th PRID → ~3,400 requests for 2020–present
_PIB_SCAN_WORKERS = 1     # sequential only — PIB's CDN blocks parallel scanning
_PIB_CHECKPOINT   = "data/pib_scan_checkpoint.txt"  # last scanned PRID

# RSS feeds — used for PRID discovery in recent mode
_DEFAULT_RSS_FEEDS = [
    "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
    # Ministry-specific feeds for broader recent coverage
    "https://pib.gov.in/RssMain.aspx?ModId=3&Lang=1&Regid=3",   # Finance
    "https://pib.gov.in/RssMain.aspx?ModId=16&Lang=1&Regid=3",  # Commerce
    "https://pib.gov.in/RssMain.aspx?ModId=19&Lang=1&Regid=3",  # Power
    "https://pib.gov.in/RssMain.aspx?ModId=15&Lang=1&Regid=3",  # Heavy Industries
]

# Approximate PRID anchors by year start — used to find range start for historical scan
_PRID_YEAR_ANCHORS = {
    2019: 1550000,
    2020: 1600000,
    2021: 1700000,
    2022: 1800000,
    2023: 1900000,
    2024: 2000000,
    2025: 2100000,
    2026: 2230000,
}

_FEEDPARSER_DIRECT = True

_DEFAULT_KEYWORDS = [
    "PLI", "production linked incentive",
    "semiconductor", "chip", "fab",
    "solar", "renewable energy", "green hydrogen", "wind",
    "railway", "metro", "bullet train", "vande bharat",
    "defence", "defense", "HAL", "BEL", "DRDO",
    "electric vehicle", "EV", "battery",
    "infrastructure", "capex", "capital expenditure",
    "startup", "unicorn", "digital india",
    "steel", "aluminium", "copper",
    "logistics", "port", "highway", "road",
    "5G", "telecom",
]

_MINISTRY_PATTERN = re.compile(
    r"Ministry of ([\w\s&]+?)(?:\s*\||\s*–|\n|$)", re.IGNORECASE
)


class PIBFetcher(SourceAdapter):
    """Fetches PIB India press releases via RSS feeds with keyword filtering."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.rss_feeds: list[str] = config.get("rss_feeds", _DEFAULT_RSS_FEEDS)
        self.keywords: list[str] = config.get("keywords", _DEFAULT_KEYWORDS)
        self.start_date: str = config.get("start_date", "2023-01-01")
        self.fetch_full_text: bool = config.get("fetch_full_text", True)
        self.api_delay: float = config.get("api_delay_seconds", 0.5)

        self._kw_lower: list[str] = [k.lower() for k in self.keywords]

        if not _FEEDPARSER_AVAILABLE:
            logger.warning(
                "[pib] feedparser not installed. Install with: pip install feedparser"
            )

        self.session.headers.update({
            "User-Agent": "MakroGraph/0.2 (India Intelligence Pipeline)",
            "Accept": "text/html,application/xhtml+xml,*/*",
        })

    @property
    def source_name(self) -> str:
        return "pib_india"

    def _is_relevant(self, text: str) -> bool:
        """Check whether a release title or summary contains at least one keyword."""
        lower = text.lower()
        return any(kw in lower for kw in self._kw_lower)

    def _fetch_article_text(self, url: str) -> str:
        """Fetch and extract plain text from a PIB article HTML page.

        Used when fetch_full_text=True to verify keywords are present in the
        full article body (not just the RSS title/summary).
        """
        if not _BS4_AVAILABLE:
            return ""
        try:
            self._throttle()
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for sel in [
                {"class": "innner-page-main-about-us-content-right-part"},
                {"class": "release_content"},
                {"class": "content-area"},
                {"id": "content"},
            ]:
                div = soup.find("div", sel)
                if div:
                    return div.get_text(" ", strip=True)
            return soup.get_text(" ", strip=True)
        except Exception as exc:
            logger.debug(f"[pib] Full text fetch failed for {url}: {exc}")
            return ""

    def _parse_feed_entry(self, entry) -> Optional[SourceDocument]:
        """Convert a feedparser entry to a SourceDocument."""
        try:
            title = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            link = getattr(entry, "link", "") or ""

            if not link:
                return None

            if not self._is_relevant(title + " " + summary):
                return None

            pub_dt: Optional[datetime] = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import calendar
                ts = calendar.timegm(entry.published_parsed)
                pub_dt = datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc)

            ministry = ""
            m = _MINISTRY_PATTERN.search(title)
            if m:
                ministry = m.group(1).strip()

            return SourceDocument(
                url=link,
                title=title[:500],
                doc_type="press_release",
                source_name=self.source_name,
                published_at=pub_dt,
                company="",
                ticker="",
                filing_type="press_release",
                metadata={
                    "country": "IN",
                    "ministry": ministry,
                    "source": "PIB",
                    "summary": summary[:300],
                },
            )
        except Exception as exc:
            logger.debug(f"[pib] Entry parse error: {exc}")
            return None

    def _fetch_rss(self, feed_url: str) -> list[SourceDocument]:
        """Fetch and parse one RSS feed, returning filtered SourceDocuments.

        feedparser.parse(url) fails on PIB due to BOM + ISO-8859-1 encoding.
        We fetch raw bytes with requests and pass them directly to feedparser.
        """
        if not _FEEDPARSER_AVAILABLE:
            return []
        try:
            self._throttle()
            resp = self.session.get(feed_url, timeout=self.timeout)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)   # raw bytes — handles BOM correctly
            docs = []
            for entry in feed.entries:
                doc = self._parse_feed_entry(entry)
                if doc:
                    docs.append(doc)
            logger.debug(f"[pib] Feed {feed_url}: {len(feed.entries)} total, {len(docs)} relevant")
            return docs
        except Exception as exc:
            logger.error(f"[pib] RSS fetch failed ({feed_url}): {exc}")
            return []

    def _fetch_prid_page(self, prid: int) -> Optional["SourceDocument"]:
        """Fetch a single PRID page and return a SourceDocument if English + relevant.

        Uses a plain requests.get (not the retrying session) with a short
        timeout so timed-out PRIDs fail fast rather than burning 24 s each.
        """
        import requests as _req
        url = f"{_PIB_SHARE_URL}?PRID={prid}"
        try:
            # PIB does 2 redirects (pib.gov.in → www.pib.gov.in → lang redirect).
            # Need 15s total to complete the chain from outside India.
            # No retry — just fail fast and skip.
            resp = _req.get(
                url,
                timeout=15,
                allow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
            if resp.status_code != 200:
                return None
        except Exception:
            return None

        if not _BS4_AVAILABLE:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        page_text = soup.get_text(" ", strip=True)

        if "The Page you have requested is not available" in page_text:
            return None
        if not all(ord(c) < 128 for c in page_text[:80]):
            return None  # non-English release

        # Extract title from <h2>
        title = ""
        for tag in ["h2", "h1", "h3"]:
            el = soup.find(tag)
            if el:
                txt = el.get_text(strip=True)
                if 10 < len(txt) < 400 and all(ord(c) < 128 for c in txt[:20]):
                    title = txt
                    break
        if not title:
            for div in soup.find_all("div"):
                txt = div.get_text(strip=True)
                if 10 < len(txt) < 400 and all(ord(c) < 128 for c in txt[:30]):
                    title = txt
                    break
        if not title:
            title = page_text[:200]

        if not self._is_relevant(title + " " + page_text[:500]):
            return None

        pub_dt = _extract_date_from_text(page_text[:500])
        ministry = ""
        m = _MINISTRY_PATTERN.search(page_text[:300])
        if m:
            ministry = m.group(1).strip()

        body = page_text
        for pfx in ["Press Release Page | Press Information Bureau", "Press Information Bureau"]:
            if body.startswith(pfx):
                body = body[len(pfx):].lstrip()

        return SourceDocument(
            url=url,
            title=title[:500],
            doc_type="press_release",
            source_name=self.source_name,
            published_at=pub_dt,
            company="", ticker="",
            filing_type="press_release",
            metadata={
                "country": "IN", "ministry": ministry,
                "source": "PIB", "body_text": body[:3000],
            },
        )

    def _scan_prid_range(
        self,
        prid_start: int,
        prid_end: int,
        step: int,
        since: Optional[datetime],
        until: Optional[datetime],
        max_results: int,
    ) -> list["SourceDocument"]:
        """Sequential throttled PRID range scan with checkpoint support.

        Intentionally single-threaded: PIB's Akamai CDN blocks parallel
        scrapers after ~100 rapid requests. Uses api_delay_seconds between
        each request and saves progress to a checkpoint file so multi-session
        historical backfills don't restart from scratch.

        At api_delay=1s + ~2s response: 3,400 requests ≈ 3 hours total.
        Run `python scripts/run_india_policy_nlp.py` repeatedly — each run
        continues from where the last left off.
        """
        import time as _time
        from pathlib import Path as _Path

        # Resume from checkpoint if it exists
        checkpoint_path = _Path(_PIB_CHECKPOINT)
        checkpoint_prid = prid_start
        if checkpoint_path.exists():
            try:
                saved = int(checkpoint_path.read_text().strip())
                if prid_start <= saved <= prid_end:
                    checkpoint_prid = saved
                    logger.info(f"[pib] Resuming from checkpoint PRID {saved:,}")
            except Exception:
                pass

        prids = list(range(checkpoint_prid, prid_end + 1, step))
        total  = (prid_end - prid_start) // step + 1
        done_before = (checkpoint_prid - prid_start) // step
        logger.info(
            f"[pib] Historical PRID scan: {checkpoint_prid:,}–{prid_end:,} "
            f"step={step} → {len(prids):,} remaining of {total:,} total "
            f"(delay={self.api_delay}s/req, sequential)"
        )

        docs: list[SourceDocument] = []
        start_dt = _parse_iso_date(self.start_date)
        consecutive_403 = 0
        stopped_early = False

        for i, prid in enumerate(prids):
            if len(docs) >= max_results:
                break

            doc = self._fetch_prid_page(prid)

            # Detect rate-limit block (403 Access Denied maps to None from _fetch_prid_page
            # but we can detect it via a direct check — add a small counter heuristic)
            if doc is None:
                consecutive_403 += 1
            else:
                consecutive_403 = 0
                if start_dt and doc.published_at and doc.published_at < start_dt:
                    pass
                elif since and doc.published_at and doc.published_at <= since:
                    pass
                elif until and doc.published_at and doc.published_at > until:
                    pass
                else:
                    docs.append(doc)

            # Progress + checkpoint every 100 PRIDs
            if (i + 1) % 100 == 0:
                logger.info(
                    f"[pib] Scanned {done_before + i + 1}/{total} PRIDs "
                    f"({100*(done_before+i+1)//total}%), {len(docs)} docs so far"
                )
                try:
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint_path.write_text(str(prid))
                except Exception:
                    pass

            # Stop if we're being rate-limited (15+ consecutive failures)
            if consecutive_403 >= 15:
                logger.warning(
                    f"[pib] 15 consecutive failures at PRID {prid:,} — "
                    f"likely rate-limited. Saving checkpoint and stopping. "
                    f"Re-run script later to continue."
                )
                try:
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint_path.write_text(str(prid))
                except Exception:
                    pass
                stopped_early = True
                break

            _time.sleep(self.api_delay)

        # Save final checkpoint only if we ran to completion (not early-stopped)
        if prids and not stopped_early:
            try:
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                checkpoint_path.write_text(str(prids[-1]))
            except Exception:
                pass

        logger.info(f"[pib] Scan session complete: {len(docs)} relevant docs found")
        return docs

    def _scrape_html_listing(
        self, since: Optional[datetime] = None, until: Optional[datetime] = None
    ) -> list[SourceDocument]:
        """Fetch PIB English press releases.

        Two modes depending on how far back start_date reaches:
          Historical (start_date > 60 days ago): parallel PRID range scan from
            the year anchor for start_date up to the latest known PRID.
            Covers 2020–present in ~5 minutes with 8 parallel workers.
          Recent (start_date <= 60 days ago): RSS PRID discovery + parallel
            fetch of ±200 window — fast, covers the last few days.
        """
        if not _BS4_AVAILABLE:
            logger.warning("[pib] BeautifulSoup not available — skipping")
            return []

        from datetime import timedelta as _td
        start_dt = _parse_iso_date(self.start_date)
        now_dt   = datetime.now(timezone.utc)
        is_historical = start_dt is None or (now_dt - start_dt) > _td(days=60)

        if is_historical:
            start_year  = start_dt.year if start_dt else 2020
            anchor_year = max(y for y in _PRID_YEAR_ANCHORS if y <= start_year)
            prid_start  = _PRID_YEAR_ANCHORS[anchor_year]
            prid_end    = max(_PRID_YEAR_ANCHORS.values()) + 50_000
            return self._scan_prid_range(
                prid_start=prid_start,
                prid_end=prid_end,
                step=_PIB_SCAN_STEP,
                since=since,
                until=until,
                max_results=self.max_results,
            )

        # Recent mode: RSS for PRID discovery + parallel fetch
        if not _FEEDPARSER_AVAILABLE:
            logger.warning("[pib] feedparser not available — recent fetch skipped")
            return []

        import re as _re
        prid_set: set[int] = set()
        for feed_url in self.rss_feeds:
            try:
                self._throttle()
                resp = self.session.get(feed_url, timeout=self.timeout)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                for entry in feed.entries:
                    link = entry.get("link", "")
                    m = _re.search(r"PRID=(\d+)", link)
                    if m:
                        prid_set.add(int(m.group(1)))
            except Exception as exc:
                logger.debug(f"[pib] RSS feed {feed_url} failed: {exc}")

        if not prid_set:
            logger.info("[pib] No PRIDs collected from RSS feeds")
            return []

        prid_min  = min(prid_set) - 200
        prid_max  = max(prid_set) + 5
        all_prids = sorted(set(range(prid_min, prid_max + 1, 2)) | prid_set, reverse=True)

        import time as _time
        docs: list[SourceDocument] = []
        for prid in all_prids:
            if len(docs) >= self.max_results:
                break
            doc = self._fetch_prid_page(prid)
            if doc is None:
                continue
            if since and doc.published_at and doc.published_at <= since:
                continue
            if until and doc.published_at and doc.published_at > until:
                continue
            docs.append(doc)
            _time.sleep(self.api_delay)

        logger.debug(f"[pib] Recent RSS→PRID scan: {len(docs)} docs")
        return docs

    def discover(self, since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[SourceDocument]:
        """Discover PIB press releases.

        Uses RSS feeds for PRID discovery then fetches each English release
        page. allRel.aspx is JS-rendered and returns no scrapable links.
        """
        all_docs = self._scrape_html_listing(since=since, until=until)

        seen_urls: set[str] = set()
        unique_docs: list[SourceDocument] = []
        for doc in all_docs:
            if doc.url not in seen_urls:
                seen_urls.add(doc.url)
                unique_docs.append(doc)

        logger.info(f"[pib] Discovered {len(unique_docs)} press releases")
        return unique_docs[: self.max_results]


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_date_from_text(text: str) -> Optional[datetime]:
    """Try to parse a date from arbitrary surrounding text (PIB rows, article pages, etc.)."""
    text = re.sub(r"\s+", " ", text).strip()
    patterns = [
        ("%d %b %Y",  r"\b(\d{1,2}\s+[A-Za-z]{3}\s+20\d{2})\b"),   # 09 JUN 2026
        ("%d %B %Y",  r"\b(\d{1,2}\s+[A-Za-z]{4,9}\s+20\d{2})\b"),  # 15 November 2024
        ("%B %d, %Y", r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+20\d{2})\b"), # November 15, 2024
        ("%d-%m-%Y",  r"\b(\d{2}-\d{2}-20\d{2})\b"),                 # 15-11-2024
        ("%Y-%m-%d",  r"\b(20\d{2}-\d{2}-\d{2})\b"),                 # 2024-11-15
        ("%d/%m/%Y",  r"\b(\d{2}/\d{2}/20\d{2})\b"),                 # 15/11/2024
    ]
    for fmt, pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return datetime.strptime(m.group(1).strip(), fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None
