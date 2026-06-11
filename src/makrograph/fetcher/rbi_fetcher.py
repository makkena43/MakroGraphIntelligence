"""Reserve Bank of India (RBI) press releases and monetary policy fetcher.

Fetches RBI press releases via the public RSS feed with keyword filtering.
When fetch_full_text=True, individual article pages are also fetched for
tighter keyword verification against the full article body.

RSS feed:
  https://rbi.org.in/scripts/rss_RBI.aspx

Config keys (under `rbi:` in settings.yaml):
    rss_feeds         - list of RSS feed URLs (defaults to the standard RBI feed)
    keywords          - list of keyword strings to filter releases (case-insensitive)
                        empty list = fetch all releases
    start_date        - ISO date, skip releases older than this (default "2023-01-01")
    max_results       - max releases per run (default 200)
    fetch_full_text   - bool; if true, fetches full HTML body of each article and
                        applies keyword check on body text (stricter, slower)
                        (default false)
    api_delay_seconds - seconds between requests (default 0.5)
"""

import calendar
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

RBI_BASE = "https://www.rbi.org.in"
RBI_HTML_LISTING = f"{RBI_BASE}/Scripts/BS_PressReleaseDisplay.aspx"

_DEFAULT_RSS_FEEDS = [
    "https://rbi.org.in/scripts/rss_RBI.aspx",
]

_DEFAULT_KEYWORDS = [
    # Monetary policy
    "repo rate", "monetary policy", "MPC", "policy statement", "interest rate",
    "inflation", "CPI", "WPI", "GDP", "liquidity",
    # Credit & banking
    "credit", "NBFC", "bank", "banking", "NPA", "bad loan", "financial stability",
    "priority sector", "MSME", "micro finance", "sectoral credit",
    # External sector
    "forex", "foreign exchange", "reserve", "FDI", "FPI", "capital flows",
    "current account", "balance of payments", "rupee", "INR", "exchange rate",
    # India infrastructure / growth themes relevant to macro
    "infrastructure", "renewable energy", "solar", "green finance", "ESG",
    "electric vehicle", "EV", "battery", "semiconductor", "PLI",
    "capital expenditure", "capex", "investment", "credit growth",
    "power sector", "transmission", "grid", "5G", "telecom",
    "defence", "defense", "export", "import", "trade",
    # Digital / fintech
    "digital currency", "CBDC", "UPI", "payment system",
    # Regulatory
    "penalty", "circular", "directive", "regulation", "framework",
]


class RBIFetcher(SourceAdapter):
    """Fetches RBI press releases and monetary policy statements via RSS."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.rss_feeds: list[str] = config.get("rss_feeds", _DEFAULT_RSS_FEEDS)
        self.keywords: list[str] = config.get("keywords", _DEFAULT_KEYWORDS)
        self.start_date: str = config.get("start_date", "2023-01-01")
        self.fetch_full_text: bool = config.get("fetch_full_text", False)
        self.api_delay: float = config.get("api_delay_seconds", 0.5)

        self._kw_lower: list[str] = [k.lower() for k in self.keywords]

        if not _FEEDPARSER_AVAILABLE:
            logger.warning(
                "[rbi] feedparser not installed. Install with: pip install feedparser"
            )

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        })

    @property
    def source_name(self) -> str:
        return "rbi_india"

    def _is_relevant(self, text: str) -> bool:
        """Return True if text contains at least one configured keyword."""
        if not self._kw_lower:
            return True
        lower = text.lower()
        return any(kw in lower for kw in self._kw_lower)

    def _fetch_article_text(self, url: str) -> str:
        """Fetch and extract plain text from an RBI HTML press release page.

        RBI press releases are at BS_PressReleaseDisplay.aspx?prid=XXXXX.
        Content lives in <table class="tablebg">. Fallbacks try common div selectors.
        Note: rbidocs.rbi.org.in PDF URLs are blocked — never call this with those.
        """
        if not _BS4_AVAILABLE:
            return ""
        try:
            self._throttle()
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            # Primary: RBI press release table content
            table = soup.find("table", {"class": "tablebg"})
            if table:
                text = table.get_text(" ", strip=True)
                if len(text) > 100:
                    return text
            # Fallbacks for other RBI page types
            for sel in [
                {"class": "innner-page-main-about-us-content-right-part"},
                {"id": "main-content"},
                {"class": "ms-rtestate-field"},
                {"class": "content"},
            ]:
                div = soup.find("div", sel)
                if div:
                    text = div.get_text(" ", strip=True)
                    if len(text) > 100:
                        return text
            return soup.get_text(" ", strip=True)
        except Exception as exc:
            logger.debug(f"[rbi] Full text fetch failed for {url}: {exc}")
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
                ts = calendar.timegm(entry.published_parsed)
                pub_dt = datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc)

            category = ""
            if hasattr(entry, "tags") and entry.tags:
                first_tag = entry.tags[0]
                category = (
                    first_tag.get("term", "")
                    if isinstance(first_tag, dict)
                    else str(first_tag)
                )

            return SourceDocument(
                url=link,
                title=title[:500],
                doc_type="press_release",
                source_name=self.source_name,
                published_at=pub_dt,
                company="",
                ticker="",
                filing_type=_classify_rbi_title(title),
                metadata={
                    "country": "IN",
                    "source": "RBI",
                    "category": category,
                    "summary": summary[:300],
                },
            )
        except Exception as exc:
            logger.debug(f"[rbi] Entry parse error: {exc}")
            return None

    def _fetch_rss(self, feed_url: str) -> list[SourceDocument]:
        """Fetch and parse one RBI RSS feed, returning keyword-filtered SourceDocuments.

        Fetches raw bytes and passes them to feedparser to handle encoding issues.
        """
        if not _FEEDPARSER_AVAILABLE:
            return []
        try:
            self._throttle()
            resp = self.session.get(feed_url, timeout=self.timeout)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            docs = []
            for entry in feed.entries:
                doc = self._parse_feed_entry(entry)
                if doc:
                    docs.append(doc)
            logger.debug(
                f"[rbi] Feed {feed_url}: {len(feed.entries)} total, {len(docs)} relevant"
            )
            return docs
        except Exception as exc:
            logger.error(f"[rbi] RSS fetch failed ({feed_url}): {exc}")
            return []

    def _scrape_html_listing(self, since: Optional[datetime] = None) -> list[SourceDocument]:
        """Scrape RBI press release HTML listing — primary data source.

        Table structure on BS_PressReleaseDisplay.aspx:
          - Single-<td> rows  = date headers e.g. "May 22, 2026"
          - Two-<td> rows     = releases:
              td[0] title + prid HTML link
              td[1] file-size + PDF link (rbidocs.rbi.org.in)
        """
        if not _BS4_AVAILABLE:
            return []

        start_dt = _parse_iso_date(self.start_date)
        docs: list[SourceDocument] = []
        seen_urls: set[str] = set()

        # RBI HTML listing: the base URL and Year= param both return the same
        # ~60 most-recent releases (server ignores the Year param).
        # Fetch only the base URL — deduplication handles the rest.
        _listing_urls = [RBI_HTML_LISTING]

        for listing_url in _listing_urls:
            current_date: Optional[datetime] = None
            try:
                self._throttle()
                resp = self.session.get(listing_url, timeout=self.timeout)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning(f"[rbi] HTML listing fetch failed ({listing_url}): {exc}")
                continue

            try:
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception:
                soup = BeautifulSoup(resp.text, "html.parser")

            all_tables = soup.find_all("table")
            table = max(all_tables, key=lambda t: len(t.find_all("tr")), default=None)
            if not table:
                continue

            for row in table.find_all("tr"):
                tds = row.find_all("td")

                if len(tds) == 1:
                    current_date = _parse_date_from_text(tds[0].get_text(strip=True))
                    continue

                if len(tds) < 2:
                    continue

                title_a = tds[0].find("a", href=True)
                pdf_a   = tds[1].find("a", href=True)

                if not title_a:
                    continue

                title = title_a.get_text(strip=True)[:500]
                if not title:
                    continue

                pub_dt = current_date or _parse_date_from_text(title)

                if start_dt and pub_dt and pub_dt < start_dt:
                    continue
                if since and pub_dt and pub_dt <= since:
                    continue

                if not self._is_relevant(title):
                    continue

                # Always use the HTML prid page — rbidocs PDF URLs are blocked (401).
                # The prid HTML page at BS_PressReleaseDisplay.aspx?prid=X is
                # accessible and has full text in table.tablebg.
                doc_url  = urljoin(RBI_BASE, title_a["href"])
                doc_type = "html"

                if doc_url in seen_urls:
                    continue
                seen_urls.add(doc_url)

                docs.append(SourceDocument(
                    url=doc_url,
                    title=title,
                    doc_type=doc_type,
                    source_name=self.source_name,
                    published_at=pub_dt,
                    company="",
                    ticker="",
                    filing_type=_classify_rbi_title(title),
                    metadata={
                        "country": "IN",
                        "source": "RBI",
                        "category": "press_release",
                    },
                ))

                if len(docs) >= self.max_results:
                    logger.debug(f"[rbi] max_results={self.max_results} reached")
                    return docs

            logger.debug(f"[rbi] URL {listing_url[-40:]}: running total={len(docs)}")

        logger.info(f"[rbi] HTML listing: {len(docs)} relevant docs across {len(_listing_urls)} year pages")
        return docs

    def discover(self, since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[SourceDocument]:
        """Discover RBI press releases.

        Primary:  HTML listing page (BS_PressReleaseDisplay.aspx) — always accessible.
        Fallback: RSS feeds (may be geo-blocked or broken outside India).

        When fetch_full_text=True, each candidate article's HTML body is fetched
        and keyword-checked; only body-matching entries are kept.  The first 2000
        chars of the body are stored in metadata["article_text"].
        """
        start_dt = _parse_iso_date(self.start_date)
        all_docs: list[SourceDocument] = self._scrape_html_listing(since)

        if not all_docs and _FEEDPARSER_AVAILABLE:
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

        logger.info(f"[rbi] Discovered {len(unique_docs)} press releases")
        return unique_docs[: self.max_results]


def _classify_rbi_title(title: str) -> str:
    """Classify RBI release type from title text."""
    t = title.lower()
    if any(w in t for w in ("monetary policy", "mpc", "repo rate", "policy rate", "policy statement")):
        return "monetary_policy"
    if any(w in t for w in ("inflation", "cpi", "wpi", "consumer price")):
        return "inflation_data"
    if any(w in t for w in ("forex", "foreign exchange", "reserve", "balance of payments", "current account")):
        return "forex_data"
    if any(w in t for w in ("circular", "notification", "directive", "master direction")):
        return "regulatory_circular"
    if any(w in t for w in ("bank", "nbfc", "credit", "npa", "loan")):
        return "banking_sector"
    if any(w in t for w in ("payment", "upi", "neft", "rtgs", "prepaid")):
        return "payments"
    return "press_release"


def _parse_date_from_text(text: str) -> Optional[datetime]:
    """Parse the first recognisable date from arbitrary text.

    Handles: ISO (2024-01-15), DD/MM/YYYY, DD-MM-YYYY,
    "15 Jan 2024", "15 January 2024", "Jan 15, 2024", "January 15, 2024".
    """
    # ISO: 2024-01-15
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except Exception:
            pass

    # DD/MM/YYYY or DD-MM-YYYY
    m2 = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if m2:
        ds = m2.group(0)
        for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(ds, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    # "15 Jan 2024" or "15 January 2024"
    m3 = re.search(r"(\d{1,2})\s+(\w{3,9})\s+(\d{4})", text)
    if m3:
        ds = f"{m3.group(1)} {m3.group(2)} {m3.group(3)}"
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(ds, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    # "Jan 15, 2024" or "January 15, 2024"
    m4 = re.search(r"(\w{3,9})\s+(\d{1,2}),?\s+(\d{4})", text)
    if m4:
        ds = f"{m4.group(1)} {m4.group(2)} {m4.group(3)}"
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(ds, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    return None


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
