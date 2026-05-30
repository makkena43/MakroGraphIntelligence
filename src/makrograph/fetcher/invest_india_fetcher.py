"""Invest India sector reports and investment announcements fetcher.

Scrapes https://www.investindia.gov.in to collect:
  - Sector opportunity reports (PDF)
  - Investment announcement articles
  - State-wise FDI highlights

Uses requests + BeautifulSoup.

Config keys (under `invest_india:` in settings.yaml):
    sections          - list of page slugs to scrape:
                          "sector-reports"          (default)
                          "investment-announcements"
                          "success-stories"
    keywords          - keyword whitelist for announcement titles (empty = all)
    start_date        - ISO date string (default "2023-01-01")
    max_results       - max documents per run (default 200)
    api_delay_seconds - seconds between page requests (default 1.0)
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

INVESTINDIA_BASE = "https://www.investindia.gov.in"
INVESTINDIA_STATIC = "https://static.investindia.gov.in"
INVESTINDIA_STATIC2 = "https://www.investindia.gov.in/s3fs-public"

# Confirmed working pages (verified 2025-05)
_DEFAULT_SECTIONS = [
    "sector-reports",   # crawls /sectors index → each sector sub-page
    "brochures",        # crawls /brochure listing
]

_SECTION_URLS = {
    "sector-reports":           f"{INVESTINDIA_BASE}/sectors",
    "brochures":                f"{INVESTINDIA_BASE}/brochure",
    "investment-announcements": f"{INVESTINDIA_BASE}/investment-announcements",
    "success-stories":          f"{INVESTINDIA_BASE}/success-stories",
    "fdi-statistics":           f"{INVESTINDIA_BASE}/sector/fdi-statistics",
}

_INVEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Referer": INVESTINDIA_BASE,
}


class InvestIndiaFetcher(SourceAdapter):
    """Fetches Invest India sector reports and investment announcements."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.sections: list[str] = config.get("sections", _DEFAULT_SECTIONS)
        self.keywords: list[str] = [k.lower() for k in config.get("keywords", [])]
        self.start_date: str = config.get("start_date", "2023-01-01")
        self.api_delay: float = config.get("api_delay_seconds", 1.0)

        if not _BS4_AVAILABLE:
            logger.warning(
                "[invest_india] beautifulsoup4 not installed. "
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
        return "invest_india"

    def _get_sector_sub_urls(self, index_url: str) -> list[str]:
        """From the /sectors index page, collect individual sector page URLs."""
        try:
            self._throttle()
            resp = self.session.get(index_url, headers=_INVEST_HEADERS, timeout=self.timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            urls = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/sector/") or href.startswith(f"{INVESTINDIA_BASE}/sector/"):
                    full = urljoin(INVESTINDIA_BASE, href)
                    if full not in urls:
                        urls.append(full)
            logger.debug(f"[invest_india] Found {len(urls)} sector sub-pages")
            return urls
        except Exception as exc:
            logger.error(f"[invest_india] Could not fetch sector index: {exc}")
            return []

    def _extract_pdfs_from_page(self, page_url: str, section_key: str) -> list[SourceDocument]:
        """Fetch a page and extract all PDF links from static.investindia.gov.in."""
        try:
            self._throttle()
            resp = self.session.get(page_url, headers=_INVEST_HEADERS, timeout=self.timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.debug(f"[invest_india] Skipping {page_url}: {exc}")
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        docs = []
        start_dt = _parse_iso_date(self.start_date)

        for a_tag in soup.find_all("a", href=True):
            href: str = a_tag["href"]
            if not href:
                continue
            # Only pick PDFs hosted on InvestIndia's static CDN
            is_static = (
                "static.investindia.gov.in" in href
                or "investindia.gov.in/s3fs-public" in href
            )
            if not (is_static or href.lower().endswith(".pdf")):
                continue

            abs_url = urljoin(INVESTINDIA_BASE, href)
            title = (a_tag.get("title") or a_tag.get_text(strip=True) or "").strip()[:500]

            _GENERIC = {"download", "click here", "view", "pdf", "here", "read more", "view pdf", "download pdf"}
            if title.lower() in _GENERIC or len(title) < 5:
                # Walk up the DOM for a nearby heading or container text
                for ancestor in a_tag.parents:
                    if ancestor.name in ("div", "li", "td", "article", "section"):
                        heading = ancestor.find(["h1", "h2", "h3", "h4", "h5", "strong"])
                        if heading:
                            candidate = heading.get_text(strip=True)
                            if candidate and len(candidate) >= 5 and candidate.lower() not in _GENERIC:
                                title = candidate[:500]
                                break
                    if title.lower() not in _GENERIC and len(title) >= 5:
                        break

            if title.lower() in _GENERIC or len(title) < 5:
                # Derive from URL filename; strip leading UUID/hash prefixes
                fname = (
                    href.rstrip("/").split("/")[-1]
                    .replace("%20", " ").replace("_", " ")
                )
                fname = re.sub(r"^[a-z0-9]{8,}\s+", "", fname).split(".")[0].strip()
                title = fname or title

            if not title or len(title) < 4:
                continue

            if self.keywords and not any(k in title.lower() for k in self.keywords):
                continue

            pub_dt = _extract_date_from_text(href + " " + title)
            if start_dt and pub_dt and pub_dt < start_dt:
                continue

            filing_type = _classify_invest_india_section(section_key, href, title)
            docs.append(SourceDocument(
                url=abs_url,
                title=title,
                doc_type="report",
                source_name=self.source_name,
                published_at=pub_dt,
                company="",
                ticker="",
                filing_type=filing_type,
                metadata={
                    "country": "IN",
                    "section": section_key,
                    "source_page": page_url,
                    "source": "InvestIndia",
                },
            ))
        return docs

    def _fetch_section(self, section_key: str) -> list[SourceDocument]:
        """Collect documents for a given section using the correct URL strategy."""
        index_url = _SECTION_URLS.get(section_key, f"{INVESTINDIA_BASE}/{section_key}")

        if section_key == "sector-reports":
            # Crawl /sectors index → each sector sub-page for PDFs
            sub_urls = self._get_sector_sub_urls(index_url)
            docs: list[SourceDocument] = []
            for sub_url in sub_urls[: min(20, len(sub_urls))]:
                docs.extend(self._extract_pdfs_from_page(sub_url, section_key))
                if len(docs) >= self.max_results:
                    break
        else:
            # Brochure page and other listing pages — extract PDFs directly
            docs = self._extract_pdfs_from_page(index_url, section_key)

        seen: set[str] = set()
        unique: list[SourceDocument] = []
        for d in docs:
            if d.url not in seen:
                seen.add(d.url)
                unique.append(d)
        logger.debug(f"[invest_india] Section '{section_key}': {len(unique)} docs")
        return unique

    def discover(self, since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[SourceDocument]:
        """Discover Invest India documents across all configured sections."""
        all_docs: list[SourceDocument] = []
        for section in self.sections:
            docs = self._fetch_section(section)
            for d in docs:
                if since and d.published_at and d.published_at <= since:
                    continue
                all_docs.append(d)
            if len(all_docs) >= self.max_results:
                break

        logger.info(f"[invest_india] Discovered {len(all_docs)} documents")
        return all_docs[: self.max_results]


def _classify_invest_india_section(section: str, href: str, title: str) -> str:
    """Derive a filing type from section name and link context."""
    if "report" in section or href.endswith(".pdf"):
        return "sector_report"
    if "announcement" in section:
        return "investment_announcement"
    if "success" in section:
        return "success_story"
    return "article"


def _extract_date_from_text(text: str) -> Optional[datetime]:
    """Try to parse a year from the text and return a rough publish date."""
    m = re.search(r"(20\d{2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
