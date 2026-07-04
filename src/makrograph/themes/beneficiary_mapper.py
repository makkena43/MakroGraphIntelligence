"""Theme beneficiary mapping: identify which stocks/companies benefit from each theme.

Maps investment themes to:
    - Direct beneficiaries: companies directly operating in the theme space
    - Indirect beneficiaries: suppliers, enablers, and infrastructure providers
    - Disruptees: companies at risk of disruption by the theme
"""

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..ontology.ontology_model import ThemeBeneficiary
from ..intelligence.company_classifier import CompanyClassifier, CompanyRole

logger = logging.getLogger(__name__)

# ── Ambiguous keyword disambiguation ─────────────────────────────────────────
# For keywords that are common English words, require at least one of these
# disambiguating context terms to appear in the company's signal context,
# otherwise the match is likely incidental (e.g. "wafer" biscuit vs wafer fab).
_KEYWORD_CONTEXT_REQUIRED: dict[str, list[str]] = {
    "wafer":      ["semiconductor", "fab", "silicon", "chip", "solar", "photovoltaic", "monocrystalline"],
    "hospital":   ["healthcare infra", "construction", "epc", "medical infra", "beds", "greenfield hospital"],
    "board":      ["circuit board", "pcb", "display", "panel"],
    "sharp":      ["display", "electronics", "lcd", "panel"],
    "award":      ["contract award", "order award", "project award", "epc award", "tender award"],
    "discovery":  ["drug discovery", "clinical", "r&d", "molecule", "research pipeline"],
    "complex":    ["petrochemical complex", "industrial complex", "chemical complex"],
    "classic":    ["classical", "vehicle model"],  # very unlikely to match — effectively blocks
}

# ── Noise-filter constants ────────────────────────────────────────────────────

# Words that indicate a genuine economic relationship in context.
# Pepsi mentioning "energy" in a brand list has none of these.
# An energy infrastructure company talking about supply shortage will have several.
_ECONOMIC_INDICATORS: frozenset[str] = frozenset([
    "supply", "demand", "shortage", "constraint", "bottleneck", "capacity",
    "capex", "capital expenditure", "investment", "invest", "procure",
    "revenue", "margin", "cost reduction", "infrastructure", "buildout",
    "manufacture", "production", "fabricat", "foundry", "wafer",
    "order", "backlog", "lead time", "utilization", "throughput",
    "contract", "shipment", "delivery", "allocat", "ramp", "expand",
    "spending", "budget", "acquisition", "purchase", "partnership",
])

# Sector codes used only for the noise-gate block logic.
# key = theme keyword  →  frozenset of sector codes to reject
# Sectors that are NEVER suppliers of technology/industrial constraints.
# A P&G or Walmart will mention "AI" and "cloud" in their filings but they
# are CONSUMERS of these services, not suppliers of constrained goods.
_NON_SUPPLIER_SECTORS = frozenset({
    "food", "beverage", "restaurant", "cannabis", "homebuilder",
    "cosmetic", "apparel", "retail", "consumer_goods", "finance",
    "insurance", "real_estate", "media", "entertainment", "gaming",
    "education", "healthcare_services", "hospital", "pharmacy_chain",
    "airline", "cruise", "hotel", "travel",
})

_SECTOR_BLOCK_PAIRS: dict[str, frozenset] = {
    # Technology & AI themes — block all non-tech sectors
    "ai":               _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "artificial intelligence": _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "cloud":            _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "data center":      _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "datacenter":       _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "chip":             _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "semiconductor":    _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "wafer":            _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "gpu":              _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "hbm":              _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "cybersecurity":    _NON_SUPPLIER_SECTORS | frozenset(["medical_device"]),
    "quantum":          _NON_SUPPLIER_SECTORS,
    # Energy & Industrial themes
    "energy":           _NON_SUPPLIER_SECTORS,
    "solar":            _NON_SUPPLIER_SECTORS,
    "nuclear":          _NON_SUPPLIER_SECTORS,
    "power":            _NON_SUPPLIER_SECTORS,
    "transformer":      _NON_SUPPLIER_SECTORS,
    "grid":             _NON_SUPPLIER_SECTORS,
    "wind":             _NON_SUPPLIER_SECTORS,
    "battery":          _NON_SUPPLIER_SECTORS,
    "lithium":          _NON_SUPPLIER_SECTORS,
    # EV & Mobility
    "electric vehicle": _NON_SUPPLIER_SECTORS,
    "ev":               _NON_SUPPLIER_SECTORS,
    "charging":         _NON_SUPPLIER_SECTORS,
    # Defence
    "defence":          _NON_SUPPLIER_SECTORS,
    "defense":          _NON_SUPPLIER_SECTORS,
    "missile":          _NON_SUPPLIER_SECTORS,
    "radar":            _NON_SUPPLIER_SECTORS,
    # Railways / Infrastructure
    "railway":          _NON_SUPPLIER_SECTORS,
    "wagon":            _NON_SUPPLIER_SECTORS,
}

# Ticker → sector (fast path for known names)
# EXPAND this list with all major non-tech companies that could appear in filings
_KNOWN_TICKER_SECTORS: dict[str, str] = {
    # Consumer goods / FMCG — never supply tech/industrial constraints
    "PG": "consumer_goods",    "CL": "consumer_goods",   "KMB": "consumer_goods",
    "CHD": "consumer_goods",   "COTY": "consumer_goods",  "EL": "cosmetic",
    "ULTA": "cosmetic",        "REV": "cosmetic",
    # Beverages — consumer, not industrial suppliers
    "MNST": "beverage",   "KO": "beverage",    "PEP": "beverage",
    "BUD": "beverage",    "TAP": "beverage",   "STZ": "beverage",
    "CELH": "beverage",   "SAM": "beverage",
    # Homebuilders — construction, not tech/semiconductor suppliers
    "PHM": "homebuilder", "DHI": "homebuilder", "LEN": "homebuilder",
    "NVR": "homebuilder", "TOL": "homebuilder", "TMHC": "homebuilder",
    "KBH": "homebuilder", "MDC": "homebuilder", "CCS": "homebuilder",
    "FND": "retail",      # Floor & Decor = flooring retail, not industrial
    # Home improvement retail
    "HD": "retail",    "LOW": "retail",
    # Apparel / athletic
    "LULU": "apparel", "NKE": "apparel", "UA": "apparel",
    # Electronics manufacturing services (supplier-type, not retail)
    # FLEX is actually a manufacturing services company - let it through
    # Food & beverage
    "PEP": "food", "KO": "food", "MDLZ": "food", "GIS": "food",
    "CAG": "food", "CPB": "food", "HRL": "food", "SJM": "food",
    "POST": "food", "LANC": "food", "INGR": "food", "MKC": "food",
    "K": "food",   "SFM": "food", "FRPT": "food",
    # Restaurants
    "CMG": "restaurant", "MCD": "restaurant", "SBUX": "restaurant",
    "YUM": "restaurant", "DPZ": "restaurant", "QSR": "restaurant",
    "DENN": "restaurant", "EAT": "restaurant", "TXRH": "restaurant",
    "DRI": "restaurant", "BJRI": "restaurant",
    # Retail (never tech suppliers)
    "WMT": "retail",  "TGT": "retail",  "COST": "retail", "KR": "retail",
    "ACI": "retail",  "SWY": "retail",  "AMZN": "retail",  # retail div, not AWS
    "EBAY": "retail", "ETSY": "retail",
    # Apparel
    "NKE": "apparel", "UA": "apparel", "PVH": "apparel", "VFC": "apparel",
    "HBI": "apparel", "LEVI": "apparel", "TPR": "apparel",
    # Homebuilders
    "PHM": "homebuilder", "DHI": "homebuilder", "LEN": "homebuilder",
    "TOL": "homebuilder", "NVR": "homebuilder", "MDC": "homebuilder",
    "KBH": "homebuilder", "MHO": "homebuilder",
    # Cannabis
    "CRON": "cannabis", "TLRY": "cannabis", "CGC": "cannabis",
    "ACB": "cannabis",
    # Finance / Asset Management (NOT suppliers of physical goods)
    "BLK": "finance",  "BX": "finance",   "KKR": "finance",  "APO": "finance",
    "BAC": "finance",  "JPM": "finance",  "WFC": "finance",  "C": "finance",
    "GS": "finance",   "MS": "finance",   "AXP": "finance",  "V": "finance",
    "MA": "finance",
    # More finance / asset management
    "FMBH": "finance",  "PFBC": "finance",  "WSFS": "finance",
    "BGC": "finance",   "BXSL": "finance",  "GLPI": "finance",
    "RKT": "finance",   "UWMC": "finance",  "GHLD": "finance",
    "FICO": "finance",  "SPGI": "finance",  "MCO": "finance",
    # Media / Marketing services
    "STGW": "media",    "IPG": "media",     "OMC": "media",
    "WPP": "media",     "PUBGY": "media",
    # Gaming / Entertainment
    "LYV": "entertainment", "RBLX": "entertainment", "TTWO": "entertainment",
    "EA": "entertainment",  "ATVI": "entertainment",
    # Insurance / professional risk services
    "UNH": "insurance", "AET": "insurance", "CI": "insurance",
    "AON": "insurance", "MMC": "insurance", "AIG": "insurance",
    "MET": "insurance", "PRU": "insurance", "AFL": "insurance",
    # Enterprise SaaS (backlog = deferred revenue, not supply constraint)
    "WDAY": "media",  "CRM": "media",  "NOW": "media",
    "ADBE": "media",  "INTU": "media",  "ORCL": "media",
    # Healthcare services (NOT medical device manufacturers)
    "CVS": "pharmacy_chain", "WBA": "pharmacy_chain",
    "HCA": "hospital",       "THC": "hospital",
    # Consumer platforms / retail (demand = transactions, not supply constraint;
    # "orders" in their filings means consumer orders, not order books)
    "DASH": "restaurant", "UBER": "restaurant", "AZO": "retail",
    "TJX": "retail", "ORLY": "retail", "CCL": "hotel", "NCLH": "hotel",
    # Media / Entertainment
    "DIS": "entertainment", "NFLX": "entertainment", "WBD": "entertainment",
    "PARA": "entertainment", "FOX": "media",
    # Airline / Travel
    "DAL": "airline", "UAL": "airline", "AAL": "airline", "LUV": "airline",
    "MAR": "hotel",   "HLT": "hotel",   "IHG": "hotel",
    # India FMCG / Consumer
    "HINDUNILVR.NS": "consumer_goods", "NESTLEIND.NS": "consumer_goods",
    "ITC.NS": "consumer_goods",        "MARICO.NS": "consumer_goods",
    "DABUR.NS": "consumer_goods",      "COLPAL.NS": "consumer_goods",
    "GODREJCP.NS": "consumer_goods",   "BAJAJCON.NS": "consumer_goods",
    "BRITANNIA.NS": "food",            "TATACONSUM.NS": "food",
    "MCDOWELL-N.NS": "food",
}

# Company name substring → sector (slower fallback)
_COMPANY_NAME_SECTOR_PATTERNS: list[tuple[str, str]] = [
    # Consumer goods / FMCG — will mention cloud/AI but are buyers, not suppliers
    ("procter & gamble", "consumer_goods"), ("procter", "consumer_goods"),
    ("p&g", "consumer_goods"), ("colgate", "consumer_goods"),
    ("kimberly-clark", "consumer_goods"), ("kimberly clark", "consumer_goods"),
    ("reckitt", "consumer_goods"), ("unilever", "consumer_goods"),
    ("henkel", "consumer_goods"), ("clorox", "consumer_goods"),
    ("spectrum brands", "consumer_goods"), ("energizer", "consumer_goods"),
    ("avon", "cosmetic"), ("estee lauder", "cosmetic"), ("l'oreal", "cosmetic"),
    # Food & beverage
    ("pepsico", "food"), ("pepsi", "food"), ("coca-cola", "food"), ("coke", "food"),
    ("kraft heinz", "food"), ("kraft", "food"), ("general mills", "food"),
    ("conagra", "food"), ("campbell", "food"), ("mondelez", "food"),
    ("hormel", "food"), ("mccormick", "food"), ("kellogg", "food"),
    ("hershey", "food"), ("mars ", "food"), ("nestle", "food"),
    ("hindustan unilever", "consumer_goods"), ("itc limited", "consumer_goods"),
    ("marico", "consumer_goods"), ("dabur", "consumer_goods"),
    ("britannia", "food"), ("tata consumer", "food"),
    # Restaurants
    ("chipotle", "restaurant"), ("mcdonald", "restaurant"), ("starbucks", "restaurant"),
    ("domino", "restaurant"), ("restaurant brands", "restaurant"), ("yum!", "restaurant"),
    # Retail (never tech/industrial suppliers)
    ("walmart", "retail"), ("target corp", "retail"), ("costco", "retail"),
    ("kroger", "retail"), ("amazon", "retail"),  # retail division, not AWS
    # Homebuilders
    ("pultegroup", "homebuilder"), ("d.r. horton", "homebuilder"),
    ("lennar", "homebuilder"), ("toll brothers", "homebuilder"),
    ("kb home", "homebuilder"), ("meritage", "homebuilder"),
    # Finance / Asset Management
    ("blackstone", "finance"), ("blackrock", "finance"), ("vanguard", "finance"),
    ("brookfield asset", "finance"), ("kkr", "finance"), ("apollo", "finance"),
    ("bank of america", "finance"), ("jpmorgan", "finance"), ("wells fargo", "finance"),
    ("goldman sachs", "finance"), ("morgan stanley", "finance"),
    # Airlines / Hotels
    ("delta air", "airline"), ("united airlines", "airline"), ("american airlines", "airline"),
    ("southwest airlines", "airline"), ("marriott", "hotel"), ("hilton", "hotel"),
    # Cannabis
    ("cronos group", "cannabis"), ("tilray", "cannabis"), ("canopy growth", "cannabis"),
    # Healthcare services (NOT device manufacturers)
    ("cvs health", "pharmacy_chain"), ("walgreens", "pharmacy_chain"),
    ("hca healthcare", "hospital"),
    # Media
    ("stagwell", "media"), ("interpublic", "media"), ("omnicom", "media"),
    ("live nation", "entertainment"), ("gaming and leisure", "finance"),
    ("gaming & leisure", "finance"), ("rocket companies", "finance"),
    ("first mid", "finance"), ("blackstone secured", "finance"),
    ("lifeway foods", "food"), ("lifeway", "food"),
    ("bgc group", "finance"), ("bgc partners", "finance"),
    ("dollar general", "retail"), ("dollar tree", "retail"),
    ("cypherpunk", "finance"),
    ("disney", "entertainment"), ("netflix", "entertainment"), ("comcast", "media"),
    ("news corp", "media"), ("fox corp", "media"),
    # ── Generic sector words (not company names) ─────────────────────────────
    # Any company whose NAME declares a blocked sector. Works for future
    # companies in any country — these are industry words, not tickers.
    ("financial", "finance"), (" bank", "finance"), ("bancorp", "finance"),
    ("bancshares", "finance"), ("bankers", "finance"),
    ("insurance", "insurance"), ("assurance", "insurance"),
    ("cruise", "hotel"), ("resorts", "hotel"), ("casino", "entertainment"),
    ("breweries", "beverage"), ("brewery", "beverage"),
    ("distilleries", "beverage"), ("distillery", "beverage"),
    ("beverages", "beverage"), ("foods", "food"), ("dairy", "food"),
    ("fashions", "apparel"), ("garments", "apparel"),
    ("jewellers", "apparel"), ("jewelers", "apparel"), ("jewellery", "apparel"),
    ("jewelry", "apparel"), ("diamonds", "apparel"), ("gems ", "apparel"),
    ("paints", "consumer_goods"), ("writing", "consumer_goods"),
    ("realty", "real_estate"), ("housing", "real_estate"),
]

# Icons surfaced in the UI for each role
ROLE_ICONS: dict[str, str] = {
    CompanyRole.INFRASTRUCTURE_PROVIDER: "🏗️",
    CompanyRole.SUPPLIER: "🔧",
    CompanyRole.BOTTLENECK_PLAYER: "⚡",
    CompanyRole.BENEFICIARY: "💚",
    CompanyRole.DOWNSTREAM_USER: "📥",
    CompanyRole.HIDDEN_ENABLER: "🔦",
}


@dataclass
class BeneficiaryResult:
    """All beneficiaries for a single theme."""
    theme_slug: str
    theme_name: str
    direct: list[ThemeBeneficiary] = field(default_factory=list)
    indirect: list[ThemeBeneficiary] = field(default_factory=list)
    disruptees: list[ThemeBeneficiary] = field(default_factory=list)

    @property
    def all_beneficiaries(self) -> list[ThemeBeneficiary]:
        return sorted(
            self.direct + self.indirect + self.disruptees,
            key=lambda b: -b.relevance_score,
        )

    def top_n(self, n: int = 10) -> list[ThemeBeneficiary]:
        return self.all_beneficiaries[:n]


class BeneficiaryMapper:
    """Maps themes to their direct and indirect beneficiaries.

    Uses:
        1. Signal data: which companies produced which signals
        2. Graph relationships: supply chain traversal
        3. Entity co-occurrence: companies mentioned with theme keywords
        4. Sector membership: companies in beneficiary sectors
        5. Company Capability DB: product-level capability matching (Change 1)
        6. Causal chain membership: boosts companies in active chains (Change 4/8)
    """

    def __init__(self, config: dict):
        self.min_signal_count = config.get("min_signals_for_beneficiary", 1)
        self.min_relevance = config.get("min_relevance_score", 10.0)
        self.use_graph = config.get("use_graph_for_beneficiaries", True)
        self.max_beneficiaries = config.get("max_beneficiaries_per_theme", 30)
        self._classifier = CompanyClassifier(config)
        # Lazy-load CompanyCapabilityDB (Change 1)
        self._capability_db = None

    def _get_capability_db(self):
        if self._capability_db is None:
            try:
                from ..india.company_capability_db import CompanyCapabilityDB
                self._capability_db = CompanyCapabilityDB()
            except Exception:
                self._capability_db = False  # disable if import fails
        return self._capability_db if self._capability_db is not False else None

    def map_theme(
        self,
        theme_slug: str,
        theme_name: str,
        signal_records: list[dict],
        entity_records: list[dict],
        graph_store=None,
        theme_keywords: list[str] = None,
        as_of_date=None,
        pg_store=None,
        since_date=None,
        country: str = None,
        causal_chain_beneficiaries: set[str] = None,
    ) -> BeneficiaryResult:
        """Identify all beneficiaries for a single theme.

        Args:
            as_of_date: The replay/analysis date. Used to stamp first_seen_at /
                        last_seen_at on beneficiaries so that historical quarterly
                        runs produce correctly-dated rows instead of always using
                        date.today().
            causal_chain_beneficiaries: Set of company names (lower-case) that are
                named as beneficiary_sectors in active causal chains related to this
                theme. Companies in this set get a confidence boost (Changes 4 & 8).
        """
        _as_of = as_of_date or date.today()
        _causal_ben_set = causal_chain_beneficiaries or set()
        result = BeneficiaryResult(theme_slug=theme_slug, theme_name=theme_name)

        # Strategy 1: Signal-based beneficiaries (primary source)
        signal_beneficiaries = self._from_signals(theme_slug, signal_records, theme_keywords, as_of_date=_as_of)
        result.direct.extend(signal_beneficiaries)

        # Strategy 2: Entity co-occurrence beneficiaries
        entity_beneficiaries = self._from_entities(
            theme_slug, entity_records, theme_keywords or [], as_of_date=_as_of
        )
        result.direct.extend(entity_beneficiaries)

        # Strategy 4: Entity-extraction matching (DB query)
        # Catches companies whose documents have the theme entity extracted but the
        # keyword doesn't appear in the 200-char signal context window.
        # Example: Shakti Pumps has "Solar" extracted (18 docs) + demand_surge signals
        # but signal context says "PM Kusum scheme" not "solar" directly.
        if pg_store and since_date and as_of_date and theme_keywords:
            try:
                entity_match_rows = pg_store.get_companies_by_theme_entity(
                    entity_keywords=theme_keywords,
                    since_date=since_date,
                    as_of_date=as_of_date,
                    country=country,
                    min_mentions=2,
                )
                for row in entity_match_rows:
                    company = row.get("company") or ""
                    ticker  = row.get("ticker") or ""
                    if not company:
                        continue
                    key = ticker or company
                    # Only add if not already captured by Strategies 1 or 2
                    existing_keys = {
                        (b.ticker or b.company_name or b.entity_name).upper()
                        for b in result.direct
                    }
                    if key.upper() in existing_keys:
                        continue
                    mentions  = int(row.get("entity_mentions") or 0)
                    sig_count = int(row.get("signal_count") or 0)
                    relevance = min(math.log1p(mentions) * 18.0 + math.log1p(sig_count) * 8.0, 100.0)
                    if relevance >= self.min_relevance:
                        result.direct.append(ThemeBeneficiary(
                            theme_slug=theme_slug,
                            entity_name=company,
                            ticker=ticker,
                            company_name=company,
                            beneficiary_type="direct",
                            relevance_score=relevance,
                            signal_count=sig_count,
                            first_seen_at=_as_of,
                            last_seen_at=row.get("filed_at_max") or _as_of,
                            reasoning=f"Entity '{', '.join(theme_keywords)}' extracted "
                                      f"from {mentions} docs with {sig_count} investment signals.",
                        ))
            except Exception as _e:
                logger.debug(f"Strategy 4 entity-match query failed for {theme_slug}: {_e}")

        # Strategy 3: Graph supply chain (indirect)
        if self.use_graph and graph_store:
            indirect = self._from_graph(theme_slug, graph_store, as_of_date=_as_of)
            result.indirect.extend(indirect)

        # ── Change 1: Apply Company Capability DB boost ──────────────────────
        # Boost relevance for companies with documented product-level capability
        # matching the theme keywords. This rewards specialist manufacturers over
        # diversified conglomerates that merely mention the keyword in passing.
        cap_db = self._get_capability_db()
        if cap_db and theme_keywords:
            for b in result.direct:
                cap_boost = cap_db.get_capability_boost(
                    b.company_name or b.entity_name or "", theme_keywords
                )
                if cap_boost > 0:
                    b.relevance_score = min(b.relevance_score + cap_boost, 100.0)
                    b.reasoning = (b.reasoning or "") + f" | Capability match: +{cap_boost:.1f}"

        # ── Changes 4 & 8: Causal chain beneficiary confidence boost ─────────
        # Companies explicitly named in active causal chains for this theme get
        # a +15 relevance boost — structural chain evidence > incidental mention.
        if _causal_ben_set:
            for b in result.direct:
                company_lower = (b.company_name or b.entity_name or "").lower()
                if company_lower in _causal_ben_set or (b.ticker or "").lower() in _causal_ben_set:
                    b.relevance_score = min(b.relevance_score + 15.0, 100.0)
                    b.reasoning = (b.reasoning or "") + " | Causal chain beneficiary: +15"

        # Sort by relevance_score DESC before truncating so high-relevance companies
        # from any strategy (incl. Strategy 4 entity-match) are not cut by
        # low-relevance entries that happened to be appended first.
        result.direct.sort(key=lambda b: b.relevance_score, reverse=True)
        result.direct = self._deduplicate(result.direct)[:self.max_beneficiaries]
        result.indirect = self._deduplicate_against(result.indirect, result.direct)[:10]

        # Rank within theme
        for rank, b in enumerate(result.all_beneficiaries, 1):
            b.rank_in_theme = rank

        logger.debug(
            f"Theme '{theme_slug}': {len(result.direct)} direct, "
            f"{len(result.indirect)} indirect beneficiaries"
        )
        return result

    @staticmethod
    def _keywords_from_theme(slug: str, name: str, seed: dict) -> list[str]:
        """Derive keyword filter for a theme's beneficiary search.

        Priority:
          1. Seed theme's explicit trigger_keywords (most precise)
          2. Auto-theme: extract entity name from theme_name
             e.g. "Semiconductor Supply Bottleneck" → ["semiconductor"]
                  "AI Supply Bottleneck"            → ["ai", "artificial intelligence"]
                  "Data Center Buildout"             → ["data center", "datacenter"]
          3. Fall back to theme name words (better than no filter at all)
        """
        # 1. Seed keywords
        if seed.get("trigger_keywords"):
            return [k.lower() for k in seed["trigger_keywords"]]

        # 1b. Downstream constraint themes: slug = "downstream-<sector>-via-<primary>"
        #     Use the primary driver (after "via") as the main keyword, plus the
        #     sector (before "via") so we catch both upstream suppliers and the
        #     downstream sector companies.
        #     e.g. "downstream-energy-via-chip" → ["chip", "semiconductor", "energy"]
        if slug.startswith("downstream-") and "-via-" in slug:
            parts = slug[len("downstream-"):].split("-via-", 1)
            sector_part = parts[0].replace("-", " ")
            driver_part = parts[1].replace("-", " ") if len(parts) > 1 else ""
            kws = []
            if driver_part:
                kws.append(driver_part)
                # Add standard aliases for the primary driver
                _DRIVER_ALIASES = {
                    "chip": ["semiconductor", "microchip", "chip"],
                    "semiconductor": ["chip", "wafer", "fab", "foundry"],
                    "data center": ["datacenter", "hyperscaler", "data centre"],
                    "artificial intelligence": ["ai", "machine learning", "llm"],
                    "cloud": ["cloud computing", "hyperscaler", "aws", "azure"],
                    "electric vehicle": ["ev", "battery electric", "bev"],
                    "cybersecurity": ["security", "cyber", "infosec"],
                }
                for alias in _DRIVER_ALIASES.get(driver_part, []):
                    if alias not in kws:
                        kws.append(alias)
            if sector_part and sector_part not in kws:
                kws.append(sector_part)
            if kws:
                return kws

        # 2. Auto-theme: strip the signal-label suffix to get the entity.
        #    Keep this list in sync with theme detector output patterns.
        SIGNAL_SUFFIXES = (
            # Full causal-chain discovered name patterns (longest first — must beat shorter ones)
            " Demand Surge → Capex Response",
            " Demand Surge → Supply Constraint",
            " Adoption → Infrastructure Buildout",
            " Demand → Supply Constraint",
            " Adoption → Downstream Demand",
            # Supply-side labels
            " Supply Bottleneck", " Supply Constraint", " Supply Easing",
            " Supply Chain Buildout", " Supply Chain",
            " Power Constraint", " Capacity Constraint",
            # Multi-word suffixes before single-word ones (longest-first sort handles this,
            # but listing explicitly makes the intent clear)
            " Critical Shortage", " Critical Bottleneck", " Critical Constraint",
            " Shortage", " Bottleneck", " Constraint",
            # Demand-side labels
            " Demand-Supply Tension", " Demand Surge", " Demand Acceleration", " Demand Slowdown",
            # Capex labels
            " Capex Surge", " Capex Buildout", " Capex Pullback", " Capex Response",
            # Other suffixes
            " Buildout", " Build-out", " Technology Adoption",
        )
        # Sort longest suffix first so " Supply Chain Buildout" wins over " Buildout"
        entity_name = name
        for suffix in sorted(SIGNAL_SUFFIXES, key=len, reverse=True):
            if name.endswith(suffix):
                entity_name = name[: -len(suffix)].strip()
                break
        # Also strip trailing arrow fragments: "X → Y" → "X"
        if " → " in entity_name:
            entity_name = entity_name.split(" → ")[0].strip()
        # Strip leading/trailing punctuation left behind by "sector: label" name patterns
        # e.g. "cybersecurity:" → "cybersecurity"
        entity_name = entity_name.strip(":").strip()

        kws = [entity_name.lower()]

        # Add well-known aliases so context matching is broader
        _ALIASES = {
            "ai":                  ["artificial intelligence", "machine learning", "llm"],
            "artificial intelligence": ["ai", "machine learning"],
            "data center":         ["datacenter", "data centre"],
            "semiconductor":       ["chip", "wafer", "foundry", "fab"],
            "electric vehicle":    ["ev", "bev", "phev"],
            "generative ai":       ["genai", "llm", "large language model"],
            "cloud":               ["cloud computing", "hyperscaler"],
            "cybersecurity":       ["security", "cyber", "infosec"],
            "machine learning":    ["ai", "deep learning", "neural network"],
        }
        for alias_list in _ALIASES.get(entity_name.lower(), []):
            kws.append(alias_list)

        return kws

    def map_all_themes(
        self,
        themes: list,
        signal_records: list[dict],
        entity_records: list[dict],
        seed_themes: list[dict] = None,
        graph_store=None,
        as_of_date=None,
        pg_store=None,
        since_date=None,
        country: str = None,
        active_causal_chains: list[dict] = None,
    ) -> list[BeneficiaryResult]:
        """Map beneficiaries for all themes.

        Args:
            as_of_date: The replay/analysis date. Passed down so beneficiary
                        first_seen_at / last_seen_at are stamped with the correct
                        historical date rather than today.
            active_causal_chains: List of active causal chain dicts (from
                pg_store.get_active_causal_chains). Used to build per-theme
                causal beneficiary sets for Changes 4 & 8.
        """
        seed_map = {}
        if seed_themes:
            seed_map = {s["slug"]: s for s in seed_themes}

        # Build a mapping: theme_keyword → set of beneficiary company names (lower-case)
        # from active causal chains so each theme gets its own causal boost set.
        chain_keyword_to_beneficiaries: dict[str, set[str]] = {}
        if active_causal_chains:
            for chain in active_causal_chains:
                sectors = chain.get("beneficiary_sectors") or []
                if isinstance(sectors, str):
                    import json as _json
                    try:
                        sectors = _json.loads(sectors)
                    except Exception:
                        sectors = [sectors]
                chain_name = (chain.get("chain_name") or "").lower()
                terminal = (chain.get("terminal_effect") or "").lower()
                for kw_source in (chain_name, terminal):
                    for tok in kw_source.replace("-", " ").split():
                        if len(tok) >= 3:
                            chain_keyword_to_beneficiaries.setdefault(tok, set()).update(
                                s.lower() for s in sectors
                            )

        results = []
        for theme in themes:
            slug = theme.theme_slug if hasattr(theme, "theme_slug") else theme.get("theme_slug", "")
            name = theme.theme_name if hasattr(theme, "theme_name") else theme.get("theme_name", "")
            seed = seed_map.get(slug, {})
            keywords = self._keywords_from_theme(slug, name, seed)

            # Build the causal-chain beneficiary set for this theme's keywords
            theme_causal_bens: set[str] = set()
            for kw in keywords:
                for tok in kw.lower().split():
                    if tok in chain_keyword_to_beneficiaries:
                        theme_causal_bens.update(chain_keyword_to_beneficiaries[tok])

            result = self.map_theme(
                theme_slug=slug,
                theme_name=name,
                signal_records=signal_records,
                entity_records=entity_records,
                graph_store=graph_store,
                theme_keywords=keywords,
                as_of_date=as_of_date,
                pg_store=pg_store,
                since_date=since_date,
                country=country,
                causal_chain_beneficiaries=theme_causal_bens,
            )
            results.append(result)

        return results

    # ── Noise-filter helpers ──────────────────────────────────────────────────

    @staticmethod
    def _get_company_sector(company: str, ticker: str) -> str:
        """Return an informal sector code for noise-gate logic."""
        if ticker:
            s = _KNOWN_TICKER_SECTORS.get(ticker.upper())
            if s:
                return s
        name_lower = (company or "").lower()
        for fragment, sector in _COMPANY_NAME_SECTOR_PATTERNS:
            if fragment in name_lower:
                return sector
        return "unknown"

    @staticmethod
    def _economic_context_score(ctx: str, keywords: list[str]) -> float:
        """Score how economically relevant a keyword match is (0.0–1.0).

        Finds sentences in ``ctx`` that contain a theme keyword, then counts
        how many economic-indicator words appear in those same sentences.
        A brand-list or compliance mention of the keyword will score near 0.
        A genuine supply-chain discussion will score ≥ 0.65.
        """
        if not ctx or not keywords:
            return 0.0

        sentences = re.split(r"[.!?;\n]", ctx)
        best = 0.0
        for sent in sentences:
            sent_lower = sent.lower()
            # Use word-boundary matching to avoid "ai" matching "paints", "rain" etc.
            if not any(re.search(r'\b' + re.escape(kw) + r'\b', sent_lower) for kw in keywords):
                continue
            hits = sum(1 for ind in _ECONOMIC_INDICATORS if ind in sent_lower)
            if hits == 0:
                score = 0.0
            elif hits == 1:
                score = 0.40
            elif hits == 2:
                score = 0.65
            else:
                score = min(0.40 + hits * 0.15, 1.0)
            best = max(best, score)
        return best

    @staticmethod
    def _sector_allowed_for_theme(
        sector: str,
        keywords: list[str],
        seller_signal_count: int = 0,
    ) -> bool:
        """Return False if the company's sector is blocked for these theme keywords.

        CRITICAL CHANGE from original:
        - "unknown" sector NO LONGER gets a free pass.
        - Unknown sector companies are allowed ONLY if they have seller-perspective
          signals (capacity_constraint_seller, backlog, fully-allocated language).
          This prevents P&G / Walmart from being tagged to AI/Cloud/Defence themes
          just because they mention these topics in their filings.

        Rule:
          known non-supplier sector → BLOCKED for all tech/industrial themes
          unknown sector + seller signals ≥ 1 → ALLOWED (real manufacturer, just uncatalogued)
          unknown sector + zero seller signals → BLOCKED (P&G type contamination)
          known compatible sector → ALLOWED
        """
        if sector == "unknown":
            # Require a HARD seller constraint signal — not just demand_surge or capex.
            # Generic sellers fire demand_surge freely (any company can say "we see strong demand").
            # Real constrained suppliers fire capacity_constraint_seller / backlog_duration /
            # capacity_utilization_high — these are specific, rare, and investable.
            return seller_signal_count >= 2

        for kw in keywords:
            blocked = _SECTOR_BLOCK_PAIRS.get(kw, frozenset())
            if sector in blocked:
                return False
        return True

    # ─────────────────────────────────────────────────────────────────────────

    def _from_signals(
        self, theme_slug: str, signal_records: list[dict], keywords: list[str] = None, as_of_date=None
    ) -> list[ThemeBeneficiary]:
        """Extract beneficiaries from signal data, classifying each company's role."""
        company_signal_map: dict[str, dict] = defaultdict(lambda: {
            "signal_count":         0,
            "capex_signals":        0,
            "seller_signals":       0,   # capacity_constraint_seller + seller-perspective
            "buyer_signals":        0,   # supply_bottleneck buyer-perspective (margin pressure)
            "ticker":               "",
            "contexts":             [],
            "company":              "",
            "quarterly_mentions":   {},
        })

        # Signal types that unambiguously indicate SELLER perspective
        _SELLER_SIGNAL_TYPES = frozenset({
            "capacity_constraint_seller", "guidance_revenue", "pricing_power_emerging",
        })
        # Signal types that are ambiguous — use perspective field to decide
        _AMBIGUOUS_CONSTRAINT_TYPES = frozenset({
            "supply_bottleneck", "inventory_drawdown", "capacity_shortage",
            "demand_exceeds_supply",
        })

        kw_lower = [k.lower() for k in (keywords or [])]

        for sig in signal_records:
            company = sig.get("company") or sig.get("entity_name") or ""
            # get_all_signals_in_window returns "doc_ticker"; also handle plain "ticker"
            ticker = sig.get("ticker") or sig.get("doc_ticker") or ""
            ctx = (sig.get("context_text") or "").lower()
            stype = sig.get("signal_type", "")
            # Compute quarter from filed_at (signals have filed_at from the enriched query)
            _filed = sig.get("filed_at") or sig.get("doc_filed_at")
            filed_quarter = ""
            if _filed:
                try:
                    from datetime import date as _date
                    if hasattr(_filed, "month"):
                        _d = _filed
                    else:
                        _d = _date.fromisoformat(str(_filed))
                    q = (_d.month - 1) // 3 + 1
                    filed_quarter = f"Q{q}-{_d.year}"
                except Exception:
                    pass

            if not company:
                continue

            # Gate 1: keyword must appear as a whole word in context.
            # Plain substring (`kw in ctx`) causes false positives for short keywords:
            # "ai" matches "p**ai**nts", "r**ai**n", "acquis**i**tion" etc.
            if kw_lower and not any(
                re.search(r'\b' + re.escape(kw) + r'\b', ctx)
                for kw in kw_lower
            ):
                continue

            # Gate 1b: ambiguous-keyword disambiguation.
            # For words that are common in non-investment contexts (e.g. "wafer"
            # the biscuit), require at least one domain-specific context term.
            for kw in kw_lower:
                required_ctx = _KEYWORD_CONTEXT_REQUIRED.get(kw)
                if required_ctx and not any(term in ctx for term in required_ctx):
                    company = ""   # invalidate this signal
                    break
            if not company:
                continue

            # Gate 2: keyword must appear alongside economic indicator words in the
            # same sentence — filters out brand lists, ministry names, ESG boilerplate.
            if kw_lower:
                econ_score = self._economic_context_score(ctx, kw_lower)
                if econ_score < 0.35:
                    logger.debug(
                        f"Noise filter (economic context): skipping signal for "
                        f"'{company}' — keyword present but no economic context "
                        f"(score={econ_score:.2f})"
                    )
                    continue

            key = ticker or company
            perspective = sig.get("perspective", "neutral")

            company_signal_map[key]["signal_count"] += 1
            company_signal_map[key]["ticker"]  = ticker
            company_signal_map[key]["company"] = company
            if ctx:
                company_signal_map[key]["contexts"].append(ctx[:200])
            if "capex" in stype:
                company_signal_map[key]["capex_signals"] += 1

            # Track seller vs buyer perspective counts
            # This is the critical discriminator: NVIDIA/Waaree are SELLERS of
            # constrained goods; their customers are BUYERS of those constrained goods.
            if stype in _SELLER_SIGNAL_TYPES or perspective == "seller":
                company_signal_map[key]["seller_signals"] += 1
            elif stype in _AMBIGUOUS_CONSTRAINT_TYPES and perspective == "buyer":
                company_signal_map[key]["buyer_signals"] += 1

            if filed_quarter:
                qmap = company_signal_map[key]["quarterly_mentions"]
                qmap[filed_quarter] = qmap.get(filed_quarter, 0) + 1

            # Track world-class quantified signals separately for justification
            if stype in ("backlog_duration", "capacity_utilization_high",
                         "supply_concentration", "demand_pull",
                         "competitor_constrained", "realized_margin_expansion"):
                company_signal_map[key].setdefault("quality_signals", []).append({
                    "type": stype,
                    "quote": ctx[:200],
                    "quarter": filed_quarter,
                })

        beneficiaries = []
        for key, data in company_signal_map.items():
            if data["signal_count"] < self.min_signal_count:
                continue

            company_name = data.get("company", key)

            # Gate 3 + 4: sector must align with theme's supply-chain position.
            # e.g. PepsiCo (food sector) is never a legitimate energy-theme beneficiary.
            sector = self._get_company_sector(company_name, data.get("ticker", ""))
            if kw_lower and not self._sector_allowed_for_theme(sector, kw_lower, seller_signal_count=company_signal_map.get(key,{}).get("seller_signals",0)):
                logger.debug(
                    f"Noise filter (sector): skipping '{company_name}' "
                    f"(sector={sector}) for keywords={kw_lower}"
                )
                continue

            all_ctx = " ".join(data["contexts"])

            # Classify company role in this theme
            clf = self._classifier.classify(
                company=company_name,
                theme=theme_slug,
                quarter="",
                text=all_ctx,
                theme_snippets=data["contexts"],
            )
            primary_role = clf.primary_role()
            role_str = primary_role.value if primary_role else CompanyRole.BENEFICIARY.value
            role_icon = ROLE_ICONS.get(role_str, "💚")

            # Role boost — BOTTLENECK_PLAYER and SUPPLIER are the investable roles
            role_boost = 20.0 if primary_role in (
                CompanyRole.BOTTLENECK_PLAYER, CompanyRole.INFRASTRUCTURE_PROVIDER
            ) else 15.0 if primary_role == CompanyRole.SUPPLIER else 0.0

            # PERSPECTIVE BOOST — the critical discriminator:
            # Companies with seller-perspective signals are NVIDIA/Waaree type:
            # "Our capacity is constrained / our lead times extended / our backlog is full"
            # These have PRICING POWER. Companies with only buyer signals are their customers.
            seller_count = data.get("seller_signals", 0)
            buyer_count  = data.get("buyer_signals", 0)
            total_signals = max(data["signal_count"], 1)

            # Seller perspective ratio (0→1): pure sellers score 1.0
            seller_ratio = seller_count / total_signals
            # Heavy buyer weight is a RED FLAG — this company is hurt, not helped
            buyer_ratio  = buyer_count  / total_signals

            # Perspective multiplier:
            # 100% seller = 1.3×  (NVIDIA, Waaree type — clear pricing power)
            # 50/50 mixed = 1.0×  (neutral)
            # 100% buyer  = 0.6×  (customer of constrained supplier — avoid)
            perspective_mult = 0.6 + (seller_ratio * 0.7)   # maps 0→0.6, 1→1.3

            # Capex signals are the strongest forward indicator — capacity investment
            capex_boost = min(data["capex_signals"] * 6.0, 25.0)

            # Log-scale base score prevents high-volume noise companies from dominating
            base_score = math.log1p(data["signal_count"]) * 22.0

            # SIGNAL VELOCITY BONUS — the world-class differentiator:
            # Companies where constraint signals are ACCELERATING quarter-over-quarter
            # are caught EARLIER (Q1-Q2) before consensus prices them in (Q4).
            # This is what separates finding NVIDIA at $200 vs $400.
            qmap = data.get("quarterly_mentions", {})
            velocity_bonus = 0.0
            if len(qmap) >= 2:
                sorted_quarters = sorted(qmap.keys())
                counts = [qmap[q] for q in sorted_quarters]
                # Acceleration = signals in last half vs first half of period
                mid = len(counts) // 2
                recent_avg = sum(counts[mid:]) / max(len(counts[mid:]), 1)
                early_avg  = sum(counts[:mid]) / max(mid, 1)
                if early_avg > 0:
                    accel_ratio = recent_avg / early_avg
                    if accel_ratio >= 2.0:
                        velocity_bonus = 15.0  # strong acceleration = early signal
                    elif accel_ratio >= 1.5:
                        velocity_bonus = 8.0
                    elif accel_ratio >= 1.2:
                        velocity_bonus = 4.0

            # QUALITY SIGNAL BONUS — quantified signals (backlog months, utilization %)
            # are higher conviction than generic keyword matches
            quality_bonus = min(len(data.get("quality_signals", [])) * 8.0, 20.0)

            relevance = min(
                (base_score + role_boost + capex_boost + velocity_bonus + quality_bonus)
                * perspective_mult,
                100.0
            )

            # Build reasoning summary
            reasoning_parts = [f"Role: {role_icon} {role_str.replace('_', ' ').title()}"]
            if data["capex_signals"]:
                reasoning_parts.append(f"{data['capex_signals']} capex signal(s)")
            if clf.role_evidence:
                top_evidence = next(iter(clf.role_evidence.values()), [])
                if top_evidence:
                    reasoning_parts.append(f'Evidence: "{top_evidence[0][:100]}"')

            beneficiaries.append(ThemeBeneficiary(
                theme_slug=theme_slug,
                entity_name=company_name,
                ticker=data.get("ticker"),
                company_name=company_name,
                beneficiary_type="direct",
                company_role=role_str,
                relevance_score=relevance,
                signal_count=data["signal_count"],
                last_seen_at=as_of_date or date.today(),
                reasoning=" | ".join(reasoning_parts),
            ))

        return beneficiaries

    def _from_entities(
        self, theme_slug: str, entity_records: list[dict], keywords: list[str], as_of_date=None
    ) -> list[ThemeBeneficiary]:
        """Extract beneficiaries from entity co-occurrence."""
        if not keywords:
            return []

        kw_lower = [k.lower() for k in keywords]
        company_scores: dict[str, dict] = defaultdict(lambda: {
            "score": 0.0, "ticker": "", "name": "", "count": 0
        })

        for ent in entity_records:
            if ent.get("entity_type") != "COMPANY":
                continue
            name = ent.get("canonical_name", "")
            ticker = ent.get("ticker", "")
            name_lower = name.lower()

            # Check name or context against keywords using word-boundary matching.
            # Plain substring (`kw in name`) causes false positives for short keywords:
            # e.g. "ai" matches "p**ai**nts", "r**ai**n", "Ch**ai**rman" etc.
            context = ent.get("context", "").lower()
            def _word_match(kw: str, text: str) -> bool:
                if not text:
                    return False
                return bool(re.search(r'\b' + re.escape(kw) + r'\b', text))

            match_score = sum(
                1 for kw in kw_lower
                if _word_match(kw, name_lower) or _word_match(kw, context)
            )
            if match_score == 0:
                continue

            # Economic context gate: keyword in context must co-occur with economic words
            if context:
                econ = self._economic_context_score(context, kw_lower)
                if econ < 0.35:
                    continue

            # Sector gate
            ticker = ent.get("ticker", "")
            sector = self._get_company_sector(name, ticker)
            if not self._sector_allowed_for_theme(sector, kw_lower, seller_signal_count=0):
                continue

            key = ticker or name
            company_scores[key]["score"] += match_score * 15.0
            company_scores[key]["ticker"] = ticker
            company_scores[key]["name"] = name
            company_scores[key]["count"] += 1

        beneficiaries = []
        for key, data in company_scores.items():
            relevance = min(data["score"], 100.0)
            if relevance < self.min_relevance:
                continue
            beneficiaries.append(ThemeBeneficiary(
                theme_slug=theme_slug,
                entity_name=data["name"] or key,
                ticker=data["ticker"],
                company_name=data["name"] or key,
                beneficiary_type="direct",
                relevance_score=relevance,
                signal_count=data["count"],
                last_seen_at=as_of_date or date.today(),
            ))

        return beneficiaries

    def _from_graph(self, theme_slug: str, graph_store, as_of_date=None) -> list[ThemeBeneficiary]:
        """Get indirect beneficiaries via graph supply chain traversal."""
        beneficiaries = []
        try:
            theme_entities = graph_store.get_theme_entities(theme_slug)
            for ent in theme_entities:
                if ent.get("role") == "indirect":
                    beneficiaries.append(ThemeBeneficiary(
                        theme_slug=theme_slug,
                        entity_name=ent["name"],
                        ticker=ent.get("ticker", ""),
                        beneficiary_type="indirect",
                        relevance_score=float(ent.get("relevance", 30.0)),
                        last_seen_at=as_of_date or date.today(),
                    ))
        except Exception as e:
            logger.warning(f"Graph beneficiary mapping failed: {e}")
        return beneficiaries

    def _deduplicate(self, beneficiaries: list[ThemeBeneficiary]) -> list[ThemeBeneficiary]:
        """Remove duplicate company entries, keeping highest relevance."""
        seen: dict[str, ThemeBeneficiary] = {}
        for b in beneficiaries:
            key = (b.ticker or b.company_name or b.entity_name).upper()
            if key not in seen or b.relevance_score > seen[key].relevance_score:
                seen[key] = b
        result = list(seen.values())
        result.sort(key=lambda b: -b.relevance_score)
        return result

    def _deduplicate_against(
        self,
        candidates: list[ThemeBeneficiary],
        existing: list[ThemeBeneficiary],
    ) -> list[ThemeBeneficiary]:
        """Remove from candidates any that already appear in existing list."""
        existing_keys = {(b.ticker or b.company_name or b.entity_name).upper() for b in existing}
        return [
            b for b in candidates
            if (b.ticker or b.company_name or b.entity_name).upper() not in existing_keys
        ]

    def persist(self, results: list[BeneficiaryResult], pg_store, theme_id_map: dict,
                window_start=None, window_end=None):
        """Write all beneficiaries to PostgreSQL.

        Two-pass batch approach:
          Pass 1: upsert all unique entities in one loop → build name→id map
          Pass 2: upsert all beneficiaries using the id map (no per-row entity lookup)
        Both passes still use individual upserts (for conflict safety), but
        the entity lookup round-trip is eliminated from the hot loop.
        """
        if not pg_store:
            return

        # Pass 1: collect all unique companies and upsert entities once each
        unique_companies: dict[str, dict] = {}
        for result in results:
            theme_id = theme_id_map.get(result.theme_slug)
            if not theme_id:
                continue
            for b in result.all_beneficiaries:
                if not (b.ticker or b.company_name):
                    continue
                key = (b.ticker or b.company_name or b.entity_name).upper()
                if key not in unique_companies:
                    unique_companies[key] = {
                        "entity_text": b.entity_name,
                        "entity_type": "COMPANY",
                        "canonical_name": b.company_name or b.entity_name,
                        "ticker": b.ticker,
                    }

        entity_id_map: dict[str, int] = {}
        for key, entity_data in unique_companies.items():
            try:
                eid = pg_store.upsert_entity(entity_data)
                if eid:
                    entity_id_map[key] = eid
            except Exception as e:
                logger.warning(f"Entity upsert failed for {key}: {e}")

        # Verify which theme_ids still exist in the DB (guard against post-run cleanup deletes)
        all_theme_ids = {tid for tid in theme_id_map.values() if tid}
        valid_theme_ids: set[int] = set()
        if all_theme_ids and pg_store:
            try:
                valid_theme_ids = pg_store.get_existing_theme_ids(all_theme_ids)
            except Exception:
                valid_theme_ids = all_theme_ids  # assume all valid if check fails

        # Pass 2: build batch rows then bulk-upsert in one transaction
        batch: list[dict] = []
        for result in results:
            theme_id = theme_id_map.get(result.theme_slug)
            if not theme_id:
                continue
            if valid_theme_ids and theme_id not in valid_theme_ids:
                logger.debug(f"Skipping beneficiaries for deleted theme {result.theme_slug} (id={theme_id})")
                continue

            for b in result.all_beneficiaries:
                key = (b.ticker or b.company_name or b.entity_name).upper()
                entity_id = entity_id_map.get(key)
                if not entity_id:
                    continue
                batch.append({
                    "theme_id":          theme_id,
                    "entity_id":         entity_id,
                    "ticker":            b.ticker,
                    "company_name":      b.company_name,
                    "beneficiary_type":  b.beneficiary_type,
                    "company_role":      getattr(b, "company_role", ""),
                    "relevance_score":   b.relevance_score,
                    "signal_count":      b.signal_count,
                    "capex_signals":     getattr(b, "capex_signals", 0),
                    "quarterly_mentions": getattr(b, "quarterly_mentions", {}),
                    "first_seen_at":     b.first_seen_at,
                    "last_seen_at":      b.last_seen_at,
                    "rank_in_theme":     b.rank_in_theme,
                    "reasoning":         b.reasoning,
                    "window_start":      window_start,
                    "window_end":        window_end,
                })

        try:
            total = pg_store.bulk_upsert_beneficiaries(batch)
        except Exception as e:
            logger.warning(f"Bulk beneficiary upsert failed, falling back to row-by-row: {e}")
            total = 0
            for row in batch:
                try:
                    pg_store.upsert_beneficiary(row)
                    total += 1
                except Exception as re:
                    logger.warning(f"Failed to persist beneficiary: {re}")

        logger.info(
            f"Persisted {total} beneficiaries for {len(results)} themes "
            f"({len(unique_companies)} unique entities upserted)"
        )
