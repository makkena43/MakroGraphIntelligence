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

_DEFAULT_RSS_FEEDS = [
    "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
]

# When feedparser fetches directly (not via requests) it can handle
# encoding and redirects differently — used as a fallback.
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

    def discover(self, since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[SourceDocument]:
        """Discover PIB press releases from all configured RSS feeds.

        When fetch_full_text=True, each candidate article's full HTML body is
        fetched and keyword-checked for stricter relevance filtering.  The first
        2000 chars of the body are stored in metadata["article_text"].
        """
        start_dt = _parse_iso_date(self.start_date)
        all_docs: list[SourceDocument] = []

        for feed_url in self.rss_feeds:
            docs = self._fetch_rss(feed_url)
            for doc in docs:
                if start_dt and doc.published_at and doc.published_at < start_dt:
                    continue
                if since and doc.published_at and doc.published_at <= since:
                    continue

                if self.fetch_full_text:
                    full_text = self._fetch_article_text(doc.url)
                    if full_text:
                        if not self._is_relevant(full_text):
                            continue
                        doc.metadata["article_text"] = full_text[:2000]

                all_docs.append(doc)

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
