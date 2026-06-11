"""India High-Signal PDF Sources Fetcher.

Covers Tier 1-3 government and quasi-government PDF document sources.

Tier 1 — Economic Policy (primary, highest signal):
  economic_survey    — Economic Survey (indiabudget.gov.in/economicsurvey)
  union_budget       — Union Budget (indiabudget.gov.in)
  niti_aayog         — NITI Aayog Reports (niti.gov.in/reports)
  dpiit              — DPIIT (dpiit.gov.in)
  rbi_reports        — RBI Annual / Policy Reports (rbi.org.in)
  cea                — Central Electricity Authority (cea.nic.in)
  power_ministry     — Ministry of Power (powermin.gov.in)
  mnre               — MNRE (mnre.gov.in)

Tier 2 — Sector-Specific:
  powergrid          — PowerGrid Corporation (powergrid.in)
  ntpc               — NTPC (ntpc.co.in)
  seci               — SECI (seci.co.in)
  indian_railways    — Indian Railways (indianrailways.gov.in)
  steel_ministry     — Ministry of Steel (steel.gov.in)
  heavy_industries   — Ministry of Heavy Industries (heavyindustries.gov.in)
  coal_ministry      — Ministry of Coal (coal.nic.in)
  chemicals_ministry — Ministry of Chemicals (chemicals.gov.in)

Tier 3 — Legislative / Research:
  prs_india          — PRS India (prsindia.org)

Tier 4 (optional) — PIB is handled separately by PIBFetcher (pib_fetcher.py).

Recommended processing order (as configured in settings.yaml india_pdf.sources):
  economic_survey → union_budget → niti_aayog → cea → power_ministry → mnre →
  dpiit → rbi_reports → powergrid → ntpc → seci → indian_railways →
  steel_ministry → heavy_industries → coal_ministry → chemicals_ministry → prs_india

Config keys (under `india_pdf:` in settings.yaml):
    sources           — ordered list of source keys (default: all registered)
    start_date        — ISO date; skip docs older than this (default "2020-01-01")
    max_results_per_run — max total docs across all sources (default 500)
    api_delay_seconds — seconds between page requests (default 1.5)
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False
    BeautifulSoup = None  # type: ignore[assignment,misc]

import urllib3

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

# Suppress SSL warnings for Indian government domains that use the NIC root CA
# (which is not in the default Python trust store).
# These warnings are expected and noisy — we already set verify=False selectively.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
}

# Indian government domains with SSL certificate issues (self-signed / NIC CA).
# requests cannot verify these without the NIC root CA installed.
# We disable SSL verification selectively rather than globally.
_NO_VERIFY_DOMAINS: set[str] = {
    "cea.nic.in",
    "niti.gov.in",
    "coal.nic.in",
    "powermin.gov.in",
    "mnre.gov.in",
    "dpiit.gov.in",
    "heavyindustries.gov.in",
    "chemicals.gov.in",
    "steel.gov.in",
    "indianrailways.gov.in",
}


def _should_verify_ssl(url: str) -> bool:
    """Return False for Indian government domains known to have NIC CA cert issues."""
    host = urlparse(url).netloc.lstrip("www.")
    return not any(host == d or host.endswith("." + d) for d in _NO_VERIFY_DOMAINS)

_GENERIC_LINK_LABELS: set[str] = {
    "download", "click here", "view", "pdf", "here", "read more",
    "view pdf", "download pdf", "open", "english", "hindi", "get",
    "link", "read", "(type : pdf)", "(type: pdf)", "more", "details",
    "view details", "visit", "visit website", "view document",
}

# ── Source Registry ────────────────────────────────────────────────────────────
# Each entry defines pages to scrape, trusted domains for link acceptance,
# and metadata for the resulting SourceDocuments.
_SOURCE_REGISTRY: dict[str, dict] = {

    # ── TIER 1: Economic Policy ─────────────────────────────────────────────
    "economic_survey": {
        "label": "Economic Survey of India",
        "tier": 1,
        "base_url": "https://www.indiabudget.gov.in",
        "pages": [
            "https://www.indiabudget.gov.in/economicsurvey/",
        ],
        "trusted_domains": {"indiabudget.gov.in"},
        "filing_type": "economic_survey",
    },
    "union_budget": {
        "label": "Union Budget",
        "tier": 1,
        "base_url": "https://www.indiabudget.gov.in",
        "pages": [
            "https://www.indiabudget.gov.in/",
            "https://www.indiabudget.gov.in/budget_and_economic_survey.php",
        ],
        "trusted_domains": {"indiabudget.gov.in"},
        "filing_type": "budget_document",
    },
    "niti_aayog": {
        "label": "NITI Aayog",
        "tier": 1,
        "base_url": "https://www.niti.gov.in",
        "pages": [
            "https://www.niti.gov.in/reports",
            "https://www.niti.gov.in/publications",
            "https://www.niti.gov.in/vertical-policy",
        ],
        "trusted_domains": {"niti.gov.in", "static.pib.gov.in"},
        "filing_type": "policy_report",
    },
    "dpiit": {
        "label": "DPIIT",
        "tier": 1,
        "base_url": "https://dpiit.gov.in",
        "pages": [
            "https://dpiit.gov.in/publications/annual-reports",
            "https://dpiit.gov.in/publications",
            "https://dpiit.gov.in/sites/default/files/",
            "https://dpiit.gov.in/whats-new",
        ],
        "trusted_domains": {"dpiit.gov.in"},
        "filing_type": "policy_report",
        "accept_html": True,
    },
    "rbi_reports": {
        "label": "RBI Reports",
        "tier": 1,
        "base_url": "https://rbi.org.in",
        "pages": [
            "https://rbi.org.in/scripts/AnnualReportPublications.aspx",
            "https://rbi.org.in/Scripts/PublicationsView.aspx?id=17594",
            "https://rbi.org.in/Scripts/PublicationsView.aspx?id=17595",
        ],
        "trusted_domains": {"rbi.org.in", "rbidocs.rbi.org.in"},
        "filing_type": "central_bank_report",
    },
    "cea": {
        "label": "Central Electricity Authority",
        "tier": 1,
        "base_url": "https://cea.nic.in",
        "pages": [
            "https://cea.nic.in/reports/",
            "https://cea.nic.in/annual-growth-review/",
            "https://cea.nic.in/installed-capacity/",
            "https://cea.nic.in/executive-summary/",
        ],
        "trusted_domains": {"cea.nic.in"},
        "filing_type": "energy_report",
    },
    "power_ministry": {
        "label": "Ministry of Power",
        "tier": 1,
        "base_url": "https://powermin.gov.in",
        "pages": [
            "https://powermin.gov.in/en/content/annual-reports",
            "https://powermin.gov.in/en/content/policies-reports",
            "https://powermin.gov.in/en/content/power-sector-at-a-glance-all-india",
        ],
        "trusted_domains": {"powermin.gov.in"},
        "filing_type": "ministry_report",
        "accept_html": True,
    },
    "mnre": {
        "label": "MNRE",
        "tier": 1,
        "base_url": "https://mnre.gov.in",
        "pages": [
            "https://mnre.gov.in/publications/annual-report/",
            "https://mnre.gov.in/publications/",
        ],
        "trusted_domains": {"mnre.gov.in"},
        "filing_type": "energy_report",
    },

    # ── TIER 2: Sector-Specific ─────────────────────────────────────────────
    "powergrid": {
        "label": "PowerGrid Corporation",
        "tier": 2,
        "base_url": "https://www.powergrid.in",
        "pages": [
            "https://www.powergrid.in/investor/annual-report",
            "https://www.powergrid.in/investor",
        ],
        "trusted_domains": {"powergrid.in"},
        "filing_type": "annual_report",
    },
    "ntpc": {
        "label": "NTPC",
        "tier": 2,
        "base_url": "https://www.ntpc.co.in",
        "pages": [
            "https://www.ntpc.co.in/en/investors/annual-report",
        ],
        "trusted_domains": {"ntpc.co.in"},
        "filing_type": "annual_report",
    },
    "seci": {
        "label": "SECI",
        "tier": 2,
        "base_url": "https://www.seci.co.in",
        "pages": [
            "https://www.seci.co.in/annual-report/",
            "https://www.seci.co.in/",
        ],
        "trusted_domains": {"seci.co.in"},
        "filing_type": "annual_report",
    },
    "indian_railways": {
        "label": "Indian Railways",
        "tier": 2,
        "base_url": "https://indianrailways.gov.in",
        "pages": [
            "https://indianrailways.gov.in/railwayboard/view_section.jsp?lang=0&id=0,1,304,366,554",
            "https://indianrailways.gov.in/railwayboard/view_section.jsp?lang=0&id=0,1,304,366",
            "https://indianrailways.gov.in/railwayboard/view_section.jsp?lang=0&id=0,1,304",
        ],
        "trusted_domains": {"indianrailways.gov.in", "storage.googleapis.com"},
        "filing_type": "ministry_report",
    },
    "steel_ministry": {
        "label": "Ministry of Steel",
        "tier": 2,
        "base_url": "https://steel.gov.in",
        "pages": [
            "https://steel.gov.in/en/publications",
            "https://steel.gov.in/en/annual-report",
        ],
        "trusted_domains": {"steel.gov.in"},
        "filing_type": "ministry_report",
    },
    "heavy_industries": {
        "label": "Ministry of Heavy Industries",
        "tier": 2,
        "base_url": "https://heavyindustries.gov.in",
        "pages": [
            "https://heavyindustries.gov.in/en/publications",
            "https://heavyindustries.gov.in/en/annual-report",
        ],
        "trusted_domains": {"heavyindustries.gov.in"},
        "filing_type": "ministry_report",
    },
    "coal_ministry": {
        "label": "Ministry of Coal",
        "tier": 2,
        "base_url": "https://coal.nic.in",
        "pages": [
            "https://coal.nic.in/en/publications",
            "https://coal.nic.in/en/annual-report",
        ],
        "trusted_domains": {"coal.nic.in"},
        "filing_type": "ministry_report",
    },
    "chemicals_ministry": {
        "label": "Ministry of Chemicals",
        "tier": 2,
        "base_url": "https://chemicals.gov.in",
        "pages": [
            "https://chemicals.gov.in/en/publications",
            "https://chemicals.gov.in/en/annual-report",
        ],
        "trusted_domains": {"chemicals.gov.in"},
        "filing_type": "ministry_report",
    },

    # ── TIER 3: Legislative / Research ─────────────────────────────────────
    "prs_india": {
        "label": "PRS India",
        "tier": 3,
        "base_url": "https://prsindia.org",
        "pages": [
            "https://prsindia.org/bills",
            "https://prsindia.org/budgets",
        ],
        "trusted_domains": {"prsindia.org"},
        "filing_type": "legislative_brief",
        "accept_html": True,
    },
}

_DEFAULT_SOURCES: list[str] = list(_SOURCE_REGISTRY.keys())


# ── Helper utilities ────────────────────────────────────────────────────────────

def _is_generic_label(text: str) -> bool:
    """Return True if the link label carries no document-specific information."""
    t = text.lower().strip("() .")
    return not t or len(t) < 5 or t in _GENERIC_LINK_LABELS


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_date_from_text(text: str) -> Optional[datetime]:
    """Infer a publication date from link text, titles, or URL fragments."""
    text = re.sub(r"\s+", " ", text).strip()

    patterns = [
        ("%d %b %Y",  r"\b(\d{1,2}\s+[A-Za-z]{3}\s+20\d{2})\b"),
        ("%d %B %Y",  r"\b(\d{1,2}\s+[A-Za-z]{4,9}\s+20\d{2})\b"),
        ("%B %d, %Y", r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+20\d{2})\b"),
        ("%d-%m-%Y",  r"\b(\d{2}-\d{2}-20\d{2})\b"),
        ("%Y-%m-%d",  r"\b(20\d{2}-\d{2}-\d{2})\b"),
        ("%d/%m/%Y",  r"\b(\d{2}/\d{2}/20\d{2})\b"),
    ]
    for fmt, pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return datetime.strptime(m.group(1).strip(), fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue

    # Fiscal-year style "2023-24" → Jan 1 of first year
    fy_m = re.search(r"\b(20\d{2})-\d{2}\b", text)
    if fy_m:
        try:
            return datetime(int(fy_m.group(1)), 1, 1, tzinfo=timezone.utc)
        except Exception:
            pass

    # Plain 4-digit year
    yr_m = re.search(r"\b(20\d{2})\b", text)
    if yr_m:
        try:
            return datetime(int(yr_m.group(1)), 1, 1, tzinfo=timezone.utc)
        except Exception:
            pass

    return None


# ── Main Fetcher Class ─────────────────────────────────────────────────────────

class IndiaPDFFetcher(SourceAdapter):
    """Fetches PDF documents from Indian government and quasi-government sources.

    Covers Tier 1-3 in the MakroGraph India source hierarchy.
    Each SourceDocument returned has `source_name` set to its specific source
    key (e.g. "economic_survey", "niti_aayog") so DB records are tagged correctly.

    Config keys under `india_pdf:` in settings.yaml:
        sources               — ordered list of source keys to enable
        start_date            — ISO date; skip docs older than this
        max_results_per_run   — total cap across all sources (default 5000)
        max_results_per_source — per-source cap so one rich source can't
                                 exhaust the global budget (default 500)
        api_delay_seconds     — politeness delay between HTTP requests
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.sources: list[str] = config.get("sources", _DEFAULT_SOURCES)
        self.start_date: str = config.get("start_date", "2020-01-01")
        self.api_delay: float = config.get("api_delay_seconds", 1.5)
        self.max_results_per_source: int = config.get("max_results_per_source", 500)
        # Override base class default so the global budget is large enough
        # to cover all Tier 1-3 sources without truncating early.
        if "max_results_per_run" not in config:
            self.max_results = 5000

        if not _BS4_AVAILABLE:
            logger.warning(
                "[india_pdf] beautifulsoup4 not installed — no HTML parsing available. "
                "Install with: pip install beautifulsoup4 lxml"
            )

        self.session.headers.update(_BROWSER_HEADERS)

    @property
    def source_name(self) -> str:
        return "india_pdf"

    # ── Page scraping ────────────────────────────────────────────────────────

    def _scrape_page(
        self,
        page_url: str,
        source_key: str,
        source_def: dict,
    ) -> list[SourceDocument]:
        """Fetch one listing page and extract PDF (+ optionally HTML) document links.

        Title extraction follows a 3-strategy waterfall:
          1. Direct link text / title attribute
          2. URL filename (cleaned)
          3. DOM ancestor row / cell text
        Documents with only generic labels ("Download", "Click here", etc.) are skipped.
        """
        verify = _should_verify_ssl(page_url)
        try:
            self._throttle()
            resp = self.session.get(page_url, timeout=self.timeout, verify=verify)
            resp.raise_for_status()
        except Exception as exc:
            logger.debug(f"[india_pdf:{source_key}] Page fetch failed ({page_url}): {exc}")
            return []

        if not _BS4_AVAILABLE:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        base_url: str = source_def["base_url"]
        trusted: set[str] = source_def.get("trusted_domains", set())
        filing_type: str = source_def.get("filing_type", "report")
        accept_html: bool = source_def.get("accept_html", False)
        start_dt = _parse_iso_date(self.start_date)
        label: str = source_def["label"]

        docs: list[SourceDocument] = []

        for a_tag in soup.find_all("a", href=True):
            href: str = a_tag["href"].strip()
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue

            abs_url = urljoin(base_url, href) if not href.startswith("http") else href
            if not abs_url.startswith("http"):
                continue

            # Classify the link
            href_lower = href.lower().split("?")[0]
            is_pdf = (
                href_lower.endswith(".pdf")
                or abs_url.lower().split("?")[0].endswith(".pdf")
            )
            parsed_domain = urlparse(abs_url).netloc.lstrip("www.")
            is_trusted = any(
                parsed_domain == td.lstrip("www.") or parsed_domain.endswith("." + td.lstrip("www."))
                for td in trusted
            )

            if not is_pdf and not (accept_html and is_trusted):
                continue

            # ── Strategy 1: Direct link label ──────────────────────────────
            title = (
                a_tag.get("title") or a_tag.get_text(strip=True) or ""
            ).strip()[:500]

            # ── Strategy 2: URL filename ────────────────────────────────────
            if _is_generic_label(title):
                raw_fname = abs_url.rstrip("/").split("/")[-1].split("?")[0]
                fname = (
                    raw_fname.replace("%20", " ")
                    .replace("+", " ")
                    .replace("_", " ")
                    .replace("-", " ")
                    .split(".")[0]
                    .strip()
                )
                # Strip leading UUID / hash prefixes (e.g. "a3f8b2c1 Annual Report 2023")
                fname = re.sub(r"^[a-f0-9]{8,}\s*", "", fname, flags=re.I).strip()
                if fname and len(fname) >= 6 and not _is_generic_label(fname):
                    title = fname[:500]

            # ── Strategy 3: DOM ancestor context ───────────────────────────
            if _is_generic_label(title):
                for ancestor in a_tag.parents:
                    if ancestor.name in ("tr", "li", "div", "td", "article", "section", "p"):
                        candidate = ancestor.get_text(" ", strip=True)
                        for gl in _GENERIC_LINK_LABELS:
                            candidate = candidate.replace(gl.title(), " ").replace(gl, " ")
                        candidate = re.sub(r"\s{2,}", " ", candidate).strip()
                        if candidate and len(candidate) >= 8 and not _is_generic_label(candidate):
                            title = candidate[:500]
                            break

            if not title or len(title) < 5:
                continue

            pub_dt = _extract_date_from_text(title + " " + href)
            if start_dt and pub_dt and pub_dt < start_dt:
                continue

            docs.append(SourceDocument(
                url=abs_url,
                title=title,
                doc_type="pdf" if is_pdf else "html",
                source_name=source_key,
                published_at=pub_dt,
                company="",
                ticker="",
                filing_type=filing_type,
                metadata={
                    "country": "IN",
                    "tier": source_def.get("tier", 1),
                    "source_label": label,
                    "source_page": page_url,
                    "source_key": source_key,
                },
            ))

        # Per-page dedup
        seen: set[str] = set()
        unique = []
        for d in docs:
            if d.url not in seen:
                seen.add(d.url)
                unique.append(d)

        logger.debug(f"[india_pdf:{source_key}] {page_url} → {len(unique)} docs")
        return unique

    # ── Source-level fetch ───────────────────────────────────────────────────

    def _fetch_source(self, source_key: str) -> list[SourceDocument]:
        """Fetch all configured pages for a single source key."""
        source_def = _SOURCE_REGISTRY.get(source_key)
        if not source_def:
            logger.warning(f"[india_pdf] Unknown source: {source_key!r} — skipping")
            return []

        all_docs: list[SourceDocument] = []
        seen_urls: set[str] = set()

        for page_url in source_def.get("pages", []):
            for d in self._scrape_page(page_url, source_key, source_def):
                if d.url not in seen_urls:
                    seen_urls.add(d.url)
                    all_docs.append(d)
                    if len(all_docs) >= self.max_results_per_source:
                        break
            if len(all_docs) >= self.max_results_per_source:
                break

        logger.info(
            f"[india_pdf:{source_key}] {source_def['label']} "
            f"(Tier {source_def.get('tier',1)}) → {len(all_docs)} docs"
        )
        return all_docs

    # ── Public interface ─────────────────────────────────────────────────────

    def discover(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[SourceDocument]:
        """Discover PDF documents across all configured sources in priority order.

        Sources are processed in the order they appear in `self.sources` (which
        matches the recommended Tier 1 → Tier 2 → Tier 3 priority order when
        configured correctly in settings.yaml).

        Returns SourceDocuments with `source_name` = the specific source key
        (e.g. "economic_survey") rather than the generic "india_pdf" adapter name,
        so each DB record is tagged with its originating source.
        """
        all_docs: list[SourceDocument] = []
        seen_urls: set[str] = set()
        source_counts: dict[str, int] = {}

        for source_key in self.sources:
            if len(all_docs) >= self.max_results:
                logger.info(
                    f"[india_pdf] max_results ({self.max_results}) reached — "
                    f"stopping after {source_key}"
                )
                break

            source_docs = self._fetch_source(source_key)
            added = 0

            for d in source_docs:
                if d.url in seen_urls:
                    continue
                if since and d.published_at and d.published_at <= since:
                    continue
                if until and d.published_at and d.published_at > until:
                    continue
                seen_urls.add(d.url)
                all_docs.append(d)
                added += 1

                if len(all_docs) >= self.max_results:
                    break

            source_counts[source_key] = added

        logger.info(
            f"[india_pdf] Discover complete: {len(all_docs)} total docs | "
            f"per-source: {source_counts}"
        )
        return all_docs


def list_sources() -> list[dict]:
    """Return the registered source definitions (useful for inspection / tests)."""
    return [
        {"key": k, "label": v["label"], "tier": v["tier"], "pages": v["pages"]}
        for k, v in _SOURCE_REGISTRY.items()
    ]
