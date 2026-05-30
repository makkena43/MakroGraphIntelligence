"""Federal Register API fetcher.

Fetches proposed rules, final rules, executive orders, and notices from
the Federal Register — the official journal of the US federal government.

No API key required. Free public API.
Endpoint: https://www.federalregister.gov/api/v1/documents

Covers:
  - EPA environmental regulations
  - SEC / CFTC financial regulations
  - FDA drug/device rules
  - DOE energy regulations
  - FTC / DOJ antitrust actions
  - CHIPS Act implementation rules
  - Export control rules (BIS / Commerce)
  - Executive Orders
"""

import logging
from datetime import datetime
from typing import Optional

from .source_adapter import SourceAdapter, SourceDocument
from .congress_fetcher import _classify_impact

logger = logging.getLogger(__name__)

FEDERAL_REGISTER_BASE = "https://www.federalregister.gov/api/v1"

# Agencies whose documents are most relevant for sector/theme analysis
RELEVANT_AGENCIES = [
    "environmental-protection-agency",
    "securities-and-exchange-commission",
    "energy-department",
    "commerce-department",
    "federal-energy-regulatory-commission",
    "federal-trade-commission",
    "food-and-drug-administration",
    "treasury-department",
    "federal-reserve-system",
    "bureau-of-industry-and-security",
    "office-of-the-united-states-trade-representative",
    "agriculture-department",
    "interior-department",
]

RELEVANT_DOC_TYPES = ["RULE", "PRULE", "EXEC_ORDER", "NOTICE"]


class FederalRegisterFetcher(SourceAdapter):
    """Fetches Federal Register documents (regulations, executive orders).

    Config keys (under `federal_register:` in settings.yaml):
        start_date        - ISO date, default "2020-01-01"
        end_date          - ISO date, default today
        agencies          - list of agency slugs to filter (default: relevant set)
        doc_types         - RULE | PRULE | EXEC_ORDER | NOTICE
        keywords          - additional keyword search terms
        max_results       - max documents per run
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.start_date: str = config.get("start_date", "2020-01-01")
        self.end_date: str = config.get("end_date", datetime.utcnow().strftime("%Y-%m-%d"))
        self.agencies: list[str] = config.get("agencies", RELEVANT_AGENCIES)
        self.doc_types: list[str] = config.get("doc_types", RELEVANT_DOC_TYPES)
        self.keywords: list[str] = config.get("keywords", [
            "semiconductor", "critical mineral", "clean energy", "export control",
            "carbon", "electricity", "data center", "artificial intelligence",
            "supply chain", "tariff", "subsidy", "infrastructure",
        ])

        self.session.headers.update({"Accept": "application/json"})

    @property
    def source_name(self) -> str:
        return "federal_register"

    def _search_page(self, page: int = 1, per_page: int = 20,
                     keyword: str = None) -> dict:
        """One page of FR results for a single keyword term.

        Using one keyword per call (instead of OR-joining many) is more
        reliable across FR API versions and avoids the agencies+term conflict.
        """
        url = f"{FEDERAL_REGISTER_BASE}/documents.json"
        params = {
            "per_page": per_page,
            "page": page,
            "order": "newest",
            "fields[]": [
                "document_number", "title", "abstract", "type",
                "agencies", "publication_date", "effective_on",
                "html_url", "full_text_xml_url",
            ],
            "conditions[publication_date][gte]": self.start_date,
            "conditions[publication_date][lte]": self.end_date,
            "conditions[type][]": self.doc_types,
        }
        # Keyword search — single term per request (most reliable)
        if keyword:
            params["conditions[term]"] = keyword

        try:
            return self._api_get(url, params=params)
        except Exception as e:
            logger.error(f"Federal Register API error (page {page}): {e}")
            return {}

    def fetch_documents(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Fetch FR documents and classify sector/tech impact.

        Iterates over each keyword separately (one keyword per API request is
        most reliable — combining agencies+term filters causes zero results).
        Deduplicates by document_number across keywords.

        Returns list of policy event dicts for MacroStore.upsert_policy_event().
        """
        import time as _time

        if start_date:
            self.start_date = start_date
        if end_date:
            self.end_date = end_date

        seen_doc_nums: set[str] = set()
        events: list[dict] = []

        # Iterate keywords one at a time — most reliable FR API pattern
        for kw in self.keywords:
            if len(events) >= self.max_results:
                break

            page = 1
            kw_count = 0

            while len(events) < self.max_results:
                data = self._search_page(page=page, per_page=20, keyword=kw)
                results = data.get("results", [])
                if not results:
                    break

                for doc in results:
                    doc_num = doc.get("document_number", "")
                    if not doc_num or doc_num in seen_doc_nums:
                        continue
                    seen_doc_nums.add(doc_num)

                    title = doc.get("title", "")
                    abstract = doc.get("abstract", "") or ""
                    doc_type = doc.get("type", "NOTICE")
                    pub_date = doc.get("publication_date", "")
                    effective_date = doc.get("effective_on")
                    html_url = doc.get("html_url", "")

                    agency_names = [
                        a.get("name", "") for a in doc.get("agencies", [])
                    ]
                    agency_str = "; ".join(agency_names[:3])

                    combined_text = f"{title} {abstract}"
                    sectors, techs, direction, magnitude = _classify_impact(combined_text)

                    policy_id = f"federal_register::{doc_num}"
                    mapped_type = _map_doc_type(doc_type)

                    events.append({
                        "policy_id":    policy_id,
                        "source":       "federal_register",
                        "policy_type":  mapped_type,
                        "title":        title[:1000],
                        "description":  abstract[:2000],
                        "status":       "final" if doc_type == "RULE" else "proposed",
                        "introduced_date": pub_date or None,
                        "enacted_date": pub_date if doc_type in ("RULE", "EXEC_ORDER") else None,
                        "effective_date": effective_date,
                        "sponsor":      agency_str,
                        "sectors_affected": sectors,
                        "technologies_affected": techs,
                        "impact_direction": direction,
                        "impact_magnitude": magnitude,
                        "keywords":     [kw],
                        "raw_url":      html_url,
                    })
                    kw_count += 1

                    if len(events) >= self.max_results:
                        break

                total_pages = data.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1
                _time.sleep(0.3)  # polite rate-limiting between pages

            logger.info(f"Federal Register [{kw}]: {kw_count} new documents")

        logger.info(f"Federal Register: fetched {len(events)} unique documents [{self.start_date} → {self.end_date}]")
        return events

    def discover(self, since: Optional[datetime] = None) -> list[SourceDocument]:
        return []


def _map_doc_type(fr_type: str) -> str:
    mapping = {
        "RULE": "rule",
        "PRULE": "rule",
        "EXEC_ORDER": "executive_order",
        "NOTICE": "notice",
        "PNOTICE": "notice",
    }
    return mapping.get(fr_type, "notice")
