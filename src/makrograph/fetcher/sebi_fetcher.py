"""SEBI (Securities and Exchange Board of India) circulars and press releases fetcher.

Scrapes SEBI's public listing pages for:
  - Press releases
  - Circulars and master circulars

Strategy:
  1. Fetch HTML listing pages (requests + BeautifulSoup)
  2. Extract PDF / HTML document links from table rows
  3. Apply keyword and date filters
  4. Return SourceDocuments for the standard pipeline

Endpoints used:
  Press releases: https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doPressRelease=yes&type=1
  Circulars:      https://www.sebi.gov.in/legal/circulars.html

Config keys (under `sebi:` in settings.yaml):
    doc_types         - list of types to fetch:
                          "press_release"    (default)
                          "circular"         (default)
    keywords          - optional keyword whitelist (empty = fetch all)
    start_date        - ISO date string (default "2023-01-01")
    max_results       - max documents per run (default 200)
    api_delay_seconds - seconds between requests (default 1.0)
"""

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

SEBI_BASE = "https://www.sebi.gov.in"

_LISTING_URLS = {
    "press_release": (
        f"{SEBI_BASE}/sebiweb/other/OtherAction.do"
        "?doPressRelease=yes&type=1"
    ),
    # /legal/circulars.html returned 404 after SEBI's SPA migration.
    # New approach: fetch sitemap.xml and extract /legal/circulars/* and
    # /legal/master-circulars/* URLs — titles come from the <title> tag on
    # each page (static HTML, no JS needed).
    "circular": f"{SEBI_BASE}/sitemap.xml",
}

_SEBI_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Referer": SEBI_BASE,
}

_DEFAULT_DOC_TYPES = ["press_release", "circular"]

_DEFAULT_KEYWORDS = [
    "FPI", "FII", "mutual fund", "IPO", "listing",
    "insider trading", "takeover", "delisting",
    "ESG", "green bond", "social bond",
    "AIF", "PMS", "REIT", "InvIT",
    "margin", "derivatives", "futures", "options",
    "cyber security", "IEPF",
    "foreign portfolio", "buyback", "QIB",
    "algorithmic trading", "algo", "co-location",
    "ASBA", "UPI", "T+1", "T+0",
    "credit rating", "debenture", "NCD",
]


class SEBIFetcher(SourceAdapter):
    """Fetches SEBI circulars and press releases."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.doc_types: list[str] = config.get("doc_types", _DEFAULT_DOC_TYPES)
        self.keywords: list[str] = [k.lower() for k in config.get("keywords", [])]
        self.start_date: str = config.get("start_date", "2023-01-01")
        self.api_delay: float = config.get("api_delay_seconds", 1.0)

        if not _BS4_AVAILABLE:
            logger.warning(
                "[sebi] beautifulsoup4 not installed. "
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
        return "sebi_india"

    def _scrape_listing(self, doc_type: str) -> list[SourceDocument]:
        """Scrape a SEBI listing page and extract document links.

        For 'circular': SEBI's /legal/circulars.html returned 404 after their
        SPA migration.  New approach: parse sitemap.xml for /legal/circulars/*
        and /legal/master-circulars/* URLs, then fetch each page's <title> tag
        (static HTML, no JS) to get the circular title + derive date from path.

        For 'press_release': original OtherAction.do URL still works.
        """
        listing_url = _LISTING_URLS.get(doc_type)
        if not listing_url:
            logger.warning(f"[sebi] Unknown doc_type: {doc_type!r}")
            return []

        # ── Circular: sitemap-based approach ─────────────────────────────────
        if doc_type == "circular":
            return self._scrape_circulars_from_sitemap(listing_url)

        if not _BS4_AVAILABLE:
            return []

        try:
            self._throttle()
            resp = self.session.get(listing_url, headers=_SEBI_HEADERS, timeout=self.timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.error(f"[sebi] Listing fetch failed ({doc_type}): {exc}")
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        docs: list[SourceDocument] = []
        start_dt = _parse_iso_date(self.start_date)

        for row in soup.find_all(["tr", "li"]):
            a_tags = row.find_all("a", href=True)
            if not a_tags:
                continue

            pub_dt = _extract_date_from_row(row)
            if start_dt and pub_dt and pub_dt < start_dt:
                continue

            for a_tag in a_tags:
                href: str = a_tag["href"]
                if not href or href.startswith("#") or href.startswith("javascript"):
                    continue

                abs_url = urljoin(SEBI_BASE, href)
                if not abs_url.startswith("http"):
                    continue

                is_sebi_domain = "sebi.gov.in" in abs_url
                is_pdf = abs_url.lower().endswith(".pdf")
                if not (is_sebi_domain or is_pdf):
                    continue

                title = (
                    a_tag.get("title") or a_tag.get_text(strip=True) or ""
                ).strip()[:500]

                if not title or len(title) < 5:
                    title = (
                        href.rstrip("/").split("/")[-1]
                        .replace("-", " ").replace("_", " ").split(".")[0]
                    )

                if self.keywords and not any(k in title.lower() for k in self.keywords):
                    continue

                docs.append(SourceDocument(
                    url=abs_url, title=title,
                    doc_type="pdf" if is_pdf else "html",
                    source_name=self.source_name, published_at=pub_dt,
                    company="", ticker="", filing_type=doc_type,
                    metadata={"country": "IN", "source": "SEBI",
                              "doc_type_label": doc_type},
                ))

        logger.debug(f"[sebi] {doc_type}: {len(docs)} docs from listing")
        return docs

    def _scrape_circulars_from_sitemap(self, sitemap_url: str) -> list[SourceDocument]:
        """Fetch SEBI circulars via sitemap.xml.

        Each /legal/circulars/mon-yyyy/slug URL has:
          - Date derivable from the path segment (e.g. jun-2024)
          - Title available in the static <title> tag (no JS needed)
        """
        import re as _re
        start_dt = _parse_iso_date(self.start_date)
        docs: list[SourceDocument] = []

        # 1. Fetch sitemap and extract circular URLs
        try:
            self._throttle()
            r = self.session.get(sitemap_url, headers=_SEBI_HEADERS, timeout=self.timeout)
            r.raise_for_status()
        except Exception as exc:
            logger.error(f"[sebi] Sitemap fetch failed: {exc}")
            return []

        # Extract all URLs matching /legal/circulars/ or /legal/master-circulars/
        circular_urls = _re.findall(
            r'<loc>(https://www\.sebi\.gov\.in/legal/(?:master-)?circulars/[^<]+)</loc>',
            r.text
        )
        logger.info(f"[sebi] Sitemap: {len(circular_urls)} circular URLs found")

        for url in circular_urls:
            # Parse date from URL path: /legal/circulars/jun-2024/title-slug
            pub_dt = _date_from_sebi_url(url)
            if start_dt and pub_dt and pub_dt < start_dt:
                continue

            # Derive human-readable title from URL slug
            slug = url.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").title()

            # Optionally fetch <title> tag for cleaner title (throttled)
            if _BS4_AVAILABLE:
                try:
                    self._throttle()
                    rp = self.session.get(url, headers=_SEBI_HEADERS, timeout=self.timeout)
                    soup = BeautifulSoup(rp.text, "lxml")
                    title_tag = soup.find("title")
                    if title_tag:
                        raw = title_tag.text.strip()
                        # Strip "SEBI | " prefix
                        title = raw.replace("SEBI | ", "").replace("SEBI |", "").strip()
                except Exception:
                    pass  # use slug-derived title

            if self.keywords and not any(k in title.lower() for k in self.keywords):
                continue

            is_mc = "/master-circulars/" in url
            docs.append(SourceDocument(
                url=url, title=title[:500],
                doc_type="html",
                source_name=self.source_name,
                published_at=pub_dt,
                company="", ticker="",
                filing_type="master_circular" if is_mc else "circular",
                metadata={"country": "IN", "source": "SEBI",
                          "doc_type_label": "circular"},
            ))
            if len(docs) >= self.max_results:
                break

        logger.info(f"[sebi] Circulars from sitemap: {len(docs)}")
        return docs

    def discover(self, since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[SourceDocument]:
        """Discover SEBI documents across all configured doc_types."""
        all_docs: list[SourceDocument] = []
        seen: set[str] = set()

        for doc_type in self.doc_types:
            docs = self._scrape_listing(doc_type)
            for d in docs:
                if since and d.published_at and d.published_at <= since:
                    continue
                if d.url not in seen:
                    seen.add(d.url)
                    all_docs.append(d)
            if len(all_docs) >= self.max_results:
                break

        logger.info(f"[sebi] Discovered {len(all_docs)} documents")
        return all_docs[: self.max_results]


def _extract_date_from_row(tag) -> Optional[datetime]:
    """Try to extract a publish date from a table row or list item.

    Prefers the first <td> cell (SEBI tables have date in column 1) then
    falls back to scanning the entire row text.
    """
    # For <tr> rows, check first <td> first — SEBI date column
    candidates = []
    if hasattr(tag, "find_all"):
        tds = tag.find_all("td", recursive=False)
        if tds:
            candidates.append(tds[0].get_text(" ", strip=True))
    candidates.append(tag.get_text(" ", strip=True))

    for text in candidates:
        result = _parse_date_from_text(text)
        if result:
            return result
    return None


def _parse_date_from_text(text: str) -> Optional[datetime]:
    """Parse the first recognisable date from arbitrary text."""
    # ISO: 2024-01-15
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                tzinfo=timezone.utc,
            )
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


_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

def _date_from_sebi_url(url: str) -> Optional[datetime]:
    """Parse a date from SEBI URL path like /legal/circulars/jun-2024/..."""
    import re as _re
    m = _re.search(r'/([a-z]{3})-(\d{4})/', url)
    if m:
        month = _MONTH_MAP.get(m.group(1).lower())
        year  = int(m.group(2))
        if month:
            try:
                return datetime(year, month, 1, tzinfo=timezone.utc)
            except Exception:
                pass
    return None
