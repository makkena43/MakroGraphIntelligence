"""Ministry of Commerce & Industry India fetcher.

Scrapes https://commerce.gov.in to collect:
  - Policy notifications (tariff, trade, FTP - Foreign Trade Policy)
  - Manufacturing initiatives (PLI, SEZ, export promotion)
  - Press releases and circulars

Also covers DGFT (Directorate General of Foreign Trade):
  - https://www.dgft.gov.in  — trade notices, public notices, notifications

Uses requests + BeautifulSoup.

Config keys (under `commerce_india:` in settings.yaml):
    sources           - list of source keys to scrape:
                          "commerce"   → https://commerce.gov.in
                          "dgft"       → https://www.dgft.gov.in
    sections          - list of section slugs (per source) to fetch
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

COMMERCE_BASE = "https://commerce.gov.in"
DGFT_BASE = "https://www.dgft.gov.in"

# commerce.gov.in pages are JS-rendered and return empty link lists — skipped.
# DGFT /CP/?opt=* pages are accessible and return PDF links on content.dgft.gov.in.
_SOURCE_PAGES = {
    "commerce": [],    # JS-rendered; no static links accessible
    "dgft": [
        f"{DGFT_BASE}/CP/?opt=notification",
        f"{DGFT_BASE}/CP/?opt=public-notice",
        f"{DGFT_BASE}/CP/?opt=trade-notice",
    ],
}

# CDN domains considered same-origin for DGFT document downloads
_TRUSTED_CDN_DOMAINS = {
    "content.dgft.gov.in",
    "static.investindia.gov.in",
}

_COMMERCE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
}


class CommerceIndiaFetcher(SourceAdapter):
    """Fetches Ministry of Commerce policy notifications and manufacturing initiatives."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.sources: list[str] = config.get("sources", ["commerce", "dgft"])
        self.keywords: list[str] = [k.lower() for k in config.get("keywords", [])]
        self.start_date: str = config.get("start_date", "2023-01-01")
        self.api_delay: float = config.get("api_delay_seconds", 1.0)

        if not _BS4_AVAILABLE:
            logger.warning(
                "[commerce_india] beautifulsoup4 not installed. "
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
        return "commerce_india"

    def _fetch_listing_page(
        self, page_url: str, base_url: str, source_label: str = "MinistryOfCommerce"
    ) -> list[SourceDocument]:
        """Scrape a listing page and return discovered SourceDocuments."""
        try:
            self._throttle()
            resp = self.session.get(
                page_url, headers=_COMMERCE_HEADERS, timeout=self.timeout
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.error(f"[commerce_india] Fetch failed for {page_url}: {exc}")
            return []

        if not _BS4_AVAILABLE:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        docs: list[SourceDocument] = []
        start_dt = _parse_iso_date(self.start_date)

        for a_tag in soup.find_all("a", href=True):
            href: str = a_tag["href"]
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue

            abs_url = urljoin(base_url, href)
            if not abs_url.startswith("http"):
                continue

            link_domain = abs_url.split("/")[2] if len(abs_url.split("/")) > 2 else ""
            is_pdf = href.lower().endswith(".pdf")
            is_cdn = link_domain in _TRUSTED_CDN_DOMAINS

            # Only accept PDFs or documents hosted on trusted CDN domains.
            # Reject same-site nav / SPA links even if the domain matches.
            if not (is_pdf or is_cdn):
                continue

            title = (
                a_tag.get("title")
                or a_tag.get_text(strip=True)
                or ""
            ).strip()[:500]

            _GENERIC_LABELS = {
                "download", "click here", "view", "pdf", "here",
                "read more", "view pdf", "download pdf", "open",
                "(type : pdf)", "(type: pdf)",
            }

            def _is_generic(t: str) -> bool:
                return not t or len(t) < 8 or t.lower().strip("() ") in _GENERIC_LABELS

            # Strategy 1: URL filename — most reliable for CDN links like DGFT.
            # content.dgft.gov.in URLs follow: /dgftprod/<UUID>/<filename>.pdf
            if _is_generic(title) and ("content.dgft.gov.in" in abs_url or is_cdn):
                import re as _re, urllib.parse as _up
                raw_fname = _up.unquote(abs_url.rstrip("/").split("/")[-1])
                fname = raw_fname.replace("_", " ").replace("-", " ")
                # Strip leading UUID-like segments  (e.g. "abc12345 Notification 2024.pdf")
                fname = _re.sub(r"^[a-z0-9\-]{8,}\s+", "", fname).split(".")[0].strip()
                if fname and len(fname) >= 5 and not _is_generic(fname):
                    title = fname[:500]

            # Strategy 2: Walk up the DOM to the containing row/cell for a text title.
            if _is_generic(title):
                for ancestor in a_tag.parents:
                    if ancestor.name in ("tr", "li", "div", "td", "article"):
                        candidate = ancestor.get_text(" ", strip=True)
                        # Strip generic tokens left by the download link
                        for gl in _GENERIC_LABELS:
                            candidate = candidate.replace(gl.title(), " ").replace(gl, " ")
                        import re as _re
                        candidate = _re.sub(r"\s{2,}", " ", candidate).strip()
                        if candidate and len(candidate) >= 8 and not _is_generic(candidate):
                            title = candidate[:500]
                            break

            if _is_generic(title):
                continue

            if self.keywords and not any(k in title.lower() for k in self.keywords):
                continue

            pub_dt = _extract_date_nearby(a_tag, soup)
            if start_dt and pub_dt and pub_dt < start_dt:
                continue

            is_pdf = href.lower().endswith(".pdf")
            doc_type = "notification" if is_pdf else "article"
            filing_type = _classify_commerce_url(page_url, title)

            docs.append(SourceDocument(
                url=abs_url,
                title=title,
                doc_type=doc_type,
                source_name=self.source_name,
                published_at=pub_dt,
                company="",
                ticker="",
                filing_type=filing_type,
                metadata={
                    "country": "IN",
                    "source_page": page_url,
                    "source": source_label,
                },
            ))

        seen: set[str] = set()
        unique = []
        for d in docs:
            if d.url not in seen:
                seen.add(d.url)
                unique.append(d)

        logger.debug(f"[commerce_india] {page_url}: {len(unique)} docs")
        return unique

    def discover(self, since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[SourceDocument]:
        """Discover Ministry of Commerce documents across all configured sources."""
        all_docs: list[SourceDocument] = []

        for src_key in self.sources:
            base = COMMERCE_BASE if src_key == "commerce" else DGFT_BASE
            source_label = "DGFT" if src_key == "dgft" else "MinistryOfCommerce"
            pages = _SOURCE_PAGES.get(src_key, [])
            for page_url in pages:
                docs = self._fetch_listing_page(page_url, base, source_label)
                for d in docs:
                    if since and d.published_at and d.published_at <= since:
                        continue
                    all_docs.append(d)
                if len(all_docs) >= self.max_results:
                    break
            if len(all_docs) >= self.max_results:
                break

        logger.info(f"[commerce_india] Discovered {len(all_docs)} documents")
        return all_docs[: self.max_results]


def _classify_commerce_url(page_url: str, title: str) -> str:
    """Classify a filing type based on the source page URL and title."""
    u = page_url.lower()
    t = title.lower()
    if "notification" in u or "notification" in t:
        return "policy_notification"
    if "public-notice" in u or "public notice" in t:
        return "public_notice"
    if "trade-notice" in u or "trade notice" in t:
        return "trade_notice"
    if "press" in u or "press release" in t:
        return "press_release"
    if any(w in t for w in ("pli", "production linked", "sez", "export")):
        return "manufacturing_initiative"
    return "article"


def _extract_date_nearby(tag, soup) -> Optional[datetime]:
    """Look for a date string in the tag's text or nearby siblings."""
    combined = tag.get_text(" ", strip=True)
    parent = tag.parent
    if parent:
        combined += " " + parent.get_text(" ", strip=True)

    date_re = re.compile(
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
        r"|(\d{4}-\d{2}-\d{2})"
        r"|(\d{1,2}\s+\w+\s+\d{4})"
    )
    m = date_re.search(combined)
    if m:
        ds = m.group(0)
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(ds.strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
