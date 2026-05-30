"""Congress.gov API fetcher.

Discovers bills, resolutions, and enacted legislation that may affect
sectors, technologies, or commodity supply chains.

Endpoint: https://api.congress.gov/v3/bill
API key: free at https://api.congress.gov/sign-up/
Set CONGRESS_API_KEY env var or congress.api_key in settings.yaml.

Policy impact classification is rule-based using keyword matching across
title + summary text. Sector and technology tags are derived from a
curated keyword map.
"""

import logging
import os
import re
from datetime import datetime
from typing import Optional

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

CONGRESS_BASE = "https://api.congress.gov/v3"

# Keyword → sector mapping for impact classification
SECTOR_KEYWORDS: dict[str, list[str]] = {
    "Energy": [
        "energy", "oil", "gas", "petroleum", "lng", "pipeline", "refinery",
        "renewable", "solar", "wind", "nuclear", "electricity", "grid",
        "power plant", "carbon", "emissions", "clean energy", "fossil fuel",
    ],
    "Technology": [
        "semiconductor", "chip", "ai", "artificial intelligence", "cloud",
        "cybersecurity", "broadband", "5g", "quantum", "software", "data center",
        "microelectronics", "export control", "advanced technology",
    ],
    "Healthcare": [
        "pharmaceutical", "drug", "medicare", "medicaid", "biotech", "vaccine",
        "biosimilar", "medical device", "fda", "prescription", "health care",
        "clinical trial", "gene therapy",
    ],
    "Industrials": [
        "infrastructure", "manufacturing", "factory", "steel", "aluminum",
        "supply chain", "reshoring", "onshoring", "defense", "aerospace",
        "construction", "transportation", "logistics", "port",
    ],
    "Financials": [
        "bank", "credit", "finance", "lending", "capital requirement",
        "interest rate", "federal reserve", "monetary policy", "fintech",
        "crypto", "digital asset", "stablecoin",
    ],
    "Agriculture": [
        "agriculture", "farm", "crop", "fertilizer", "food", "usda",
        "ethanol", "biofuel", "livestock", "grain", "drought", "irrigation",
    ],
    "Materials": [
        "mining", "rare earth", "critical mineral", "lithium", "cobalt",
        "copper", "nickel", "tungsten", "strategic reserve",
    ],
    "Utilities": [
        "utility", "electric utility", "water", "wastewater", "municipal",
        "rate regulation", "transmission", "distribution grid",
    ],
    "Defense": [
        "defense", "military", "pentagon", "dod", "national security",
        "weapons", "munitions", "veteran", "nato", "armed forces",
    ],
}

TECH_KEYWORDS: dict[str, list[str]] = {
    "AI": ["artificial intelligence", "machine learning", "llm", "foundation model", "ai"],
    "Semiconductors": ["semiconductor", "chip", "fab", "wafer", "tsmc", "intel", "nvidia", "microelectronics"],
    "Clean Energy": ["solar panel", "wind turbine", "ev", "electric vehicle", "battery", "fuel cell", "hydrogen"],
    "Nuclear": ["nuclear", "uranium", "reactor", "smr", "small modular reactor"],
    "Biotech": ["gene", "mrna", "crispr", "biotech", "biologic", "immunotherapy"],
    "Quantum": ["quantum computing", "quantum communication", "qubit"],
    "5G/Telecom": ["5g", "6g", "telecom", "broadband", "spectrum", "fiber"],
}


def _classify_impact(text: str) -> tuple[list[str], list[str], str, float]:
    """Return (sectors, technologies, direction, magnitude) from text."""
    text_lower = text.lower()

    sectors: list[str] = []
    for sector, kws in SECTOR_KEYWORDS.items():
        if any(kw in text_lower for kw in kws):
            sectors.append(sector)

    technologies: list[str] = []
    for tech, kws in TECH_KEYWORDS.items():
        if any(kw in text_lower for kw in kws):
            technologies.append(tech)

    # Simple direction heuristic
    positive_words = ["subsidy", "grant", "incentive", "credit", "support", "invest", "fund", "promote", "expand"]
    negative_words = ["restrict", "ban", "sanction", "tariff", "penalt", "prohibit", "limit", "reduce", "cut"]
    pos = sum(1 for w in positive_words if w in text_lower)
    neg = sum(1 for w in negative_words if w in text_lower)
    if pos > neg:
        direction = "positive"
    elif neg > pos:
        direction = "negative"
    elif pos == neg and pos > 0:
        direction = "mixed"
    else:
        direction = "neutral"

    magnitude = min(100.0, (len(sectors) * 10 + len(technologies) * 8 + (pos + neg) * 5))
    return sectors, technologies, direction, magnitude


class CongressFetcher(SourceAdapter):
    """Fetches legislation and bills from Congress.gov.

    Config keys (under `congress:` in settings.yaml):
        api_key           - Congress.gov API key (or CONGRESS_API_KEY env var)
        bill_types        - list: hr | s | hjres | sjres | hconres | sconres
        start_date        - ISO date string
        end_date          - ISO date string
        max_results       - max bills to retrieve per run (default 200)
        keywords          - additional keyword filter (applied to title search)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = (
            config.get("api_key")
            or os.environ.get("CONGRESS_API_KEY", "")
        )
        self.bill_types: list[str] = config.get("bill_types", ["hr", "s"])
        self.start_date: str = config.get("start_date", "2020-01-01")
        self.end_date: str = config.get("end_date", datetime.utcnow().strftime("%Y-%m-%d"))
        self.keywords: list[str] = config.get("keywords", [])

        self.session.headers.update({"Accept": "application/json"})

    @property
    def source_name(self) -> str:
        return "congress"

    def _bills_page(self, congress: int, bill_type: str, offset: int = 0, limit: int = 20) -> dict:
        if not self.api_key:
            return {}
        url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}"
        params = {
            "api_key": self.api_key,
            "format": "json",
            "offset": offset,
            "limit": limit,
            "fromDateTime": self.start_date + "T00:00:00Z",
            "toDateTime": self.end_date + "T23:59:59Z",
            "sort": "updateDate+desc",
        }
        try:
            return self._api_get(url, params=params)
        except Exception as e:
            logger.error(f"Congress API {congress}/{bill_type} offset={offset}: {e}")
            return {}

    def _bill_detail(self, congress: int, bill_type: str, number: str) -> dict:
        url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}"
        try:
            return self._api_get(url, params={"api_key": self.api_key, "format": "json"})
        except Exception:
            return {}

    @staticmethod
    def _current_congress(year: int) -> int:
        """Return congress number for a given year (117th started 2021)."""
        return 93 + (year - 1973) // 2

    def fetch_bills(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Fetch bills and classify sector/tech impact.

        Returns list of policy event dicts for MacroStore.upsert_policy_event().
        """
        if not self.api_key:
            logger.warning("CONGRESS_API_KEY not set — skipping Congress fetch.")
            return []

        sd = start_date or self.start_date
        ed = end_date or self.end_date
        self.start_date = sd
        self.end_date = ed

        start_year = int(sd[:4])
        end_year = int(ed[:4])
        congresses = sorted(
            {self._current_congress(y) for y in range(start_year, end_year + 1)}
        )

        events: list[dict] = []
        total_limit = self.max_results

        for congress in congresses:
            for bill_type in self.bill_types:
                offset = 0
                while len(events) < total_limit:
                    page = self._bills_page(congress, bill_type, offset, limit=20)
                    bills = page.get("bills", [])
                    if not bills:
                        break

                    for b in bills:
                        number = b.get("number", "")
                        title = b.get("title", "")
                        if not title:
                            continue

                        # Keyword filter if configured
                        if self.keywords:
                            if not any(kw.lower() in title.lower() for kw in self.keywords):
                                continue

                        policy_id = f"congress::{congress}-{bill_type.upper()}-{number}"
                        latest_action = b.get("latestAction", {})
                        action_date = latest_action.get("actionDate", "")
                        action_text = latest_action.get("text", "")
                        enacted = b.get("laws") is not None or "became law" in action_text.lower()

                        sectors, techs, direction, magnitude = _classify_impact(title + " " + action_text)

                        events.append({
                            "policy_id":    policy_id,
                            "source":       "congress",
                            "policy_type":  "bill",
                            "title":        title[:1000],
                            "description":  action_text[:2000],
                            "status":       "enacted" if enacted else _map_status(action_text),
                            "introduced_date": b.get("introducedDate"),
                            "enacted_date": action_date if enacted else None,
                            "sponsor":      _extract_sponsor(b),
                            "sectors_affected": sectors,
                            "technologies_affected": techs,
                            "impact_direction": direction,
                            "impact_magnitude": magnitude,
                            "keywords":     self.keywords,
                            "raw_url": f"https://www.congress.gov/bill/{_ordinal(congress)}-congress/{bill_type}-bill/{number}",
                        })

                        if len(events) >= total_limit:
                            break

                    if len(bills) < 20:
                        break
                    offset += 20

        logger.info(f"Congress: fetched {len(events)} bills [{sd} → {ed}]")
        return events

    def discover(self, since: Optional[datetime] = None) -> list[SourceDocument]:
        return []


def _map_status(action_text: str) -> str:
    text = action_text.lower()
    if "passed house" in text or "passed senate" in text:
        return "passed"
    if "referred" in text:
        return "introduced"
    if "signed" in text:
        return "enacted"
    return "introduced"


def _extract_sponsor(bill: dict) -> str:
    sponsors = bill.get("sponsors", [])
    if sponsors:
        s = sponsors[0]
        return f"{s.get('firstName', '')} {s.get('lastName', '')}".strip()
    return ""


def _ordinal(n: int) -> str:
    suffixes = {1: "st", 2: "nd", 3: "rd"}
    return f"{n}{suffixes.get(n % 10 if n % 100 not in (11, 12, 13) else 0, 'th')}"
