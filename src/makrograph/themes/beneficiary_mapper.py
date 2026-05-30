"""Theme beneficiary mapping: identify which stocks/companies benefit from each theme.

Maps investment themes to:
    - Direct beneficiaries: companies directly operating in the theme space
    - Indirect beneficiaries: suppliers, enablers, and infrastructure providers
    - Disruptees: companies at risk of disruption by the theme
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..ontology.ontology_model import ThemeBeneficiary
from ..intelligence.company_classifier import CompanyClassifier, CompanyRole

logger = logging.getLogger(__name__)

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
_SECTOR_BLOCK_PAIRS: dict[str, frozenset] = {
    "energy":           frozenset(["food", "beverage", "restaurant", "cannabis",
                                    "medical_device", "homebuilder", "cosmetic", "apparel"]),
    "solar":            frozenset(["food", "beverage", "restaurant", "cannabis", "homebuilder"]),
    "nuclear":          frozenset(["food", "beverage", "restaurant", "cannabis"]),
    "semiconductor":    frozenset(["food", "beverage", "restaurant", "cannabis",
                                    "homebuilder", "cosmetic", "apparel", "medical_device"]),
    "chip":             frozenset(["food", "beverage", "restaurant", "cannabis",
                                    "homebuilder", "cosmetic"]),
    "wafer":            frozenset(["food", "beverage", "restaurant", "cannabis", "homebuilder"]),
    "data center":      frozenset(["food", "beverage", "restaurant", "cannabis", "homebuilder"]),
    "datacenter":       frozenset(["food", "beverage", "restaurant", "cannabis", "homebuilder"]),
    "ai":               frozenset(["food", "beverage", "restaurant", "cannabis", "homebuilder"]),
    "artificial intelligence": frozenset(["food", "beverage", "restaurant", "cannabis",
                                           "homebuilder"]),
    "lithium":          frozenset(["food", "beverage", "restaurant", "cannabis", "homebuilder"]),
    "electric vehicle": frozenset(["food", "beverage", "restaurant", "cannabis"]),
    "ev":               frozenset(["food", "beverage", "restaurant", "cannabis"]),
    "cybersecurity":    frozenset(["food", "beverage", "restaurant", "cannabis", "homebuilder"]),
}

# Ticker → sector (fast path for known names)
_KNOWN_TICKER_SECTORS: dict[str, str] = {
    # Food & beverage
    "PEP": "food", "KO": "food", "MDLZ": "food", "GIS": "food",
    "CAG": "food", "CPB": "food", "HRL": "food", "SJM": "food",
    "POST": "food", "LANC": "food", "INGR": "food",
    # Restaurants / food service
    "CMG": "restaurant", "MCD": "restaurant", "SBUX": "restaurant",
    "YUM": "restaurant", "DPZ": "restaurant", "QSR": "restaurant",
    "DENN": "restaurant", "EAT": "restaurant", "TXRH": "restaurant",
    # Food distribution
    "SYY": "food", "USFD": "food",
    # Homebuilders
    "PHM": "homebuilder", "DHI": "homebuilder", "LEN": "homebuilder",
    "TOL": "homebuilder", "NVR": "homebuilder", "MDC": "homebuilder",
    "KBH": "homebuilder", "MHO": "homebuilder",
    # Cannabis
    "CRON": "cannabis", "TLRY": "cannabis", "CGC": "cannabis",
    "ACB": "cannabis", "CURLF": "cannabis",
    # Medical devices / biotech
    "ESTA": "medical_device",
    # Cosmetics / apparel
    "EL": "cosmetic", "ULTA": "cosmetic", "NKE": "apparel", "UA": "apparel",
}

# Company name substring → sector (slower fallback)
_COMPANY_NAME_SECTOR_PATTERNS: list[tuple[str, str]] = [
    ("pepsico", "food"), ("pepsi", "food"), ("coca-cola", "food"), ("coke", "food"),
    ("kraft", "food"), ("general mills", "food"), ("conagra", "food"),
    ("campbell", "food"), ("mondelez", "food"), ("hormel", "food"),
    ("chipotle", "restaurant"), ("mcdonald", "restaurant"), ("starbucks", "restaurant"),
    ("domino", "restaurant"), ("restaurant brands", "restaurant"),
    ("sysco", "food"), ("us foods", "food"),
    ("pultegroup", "homebuilder"), ("pulte ", "homebuilder"),
    ("d.r. horton", "homebuilder"), ("lennar", "homebuilder"), ("toll brothers", "homebuilder"),
    ("cronos group", "cannabis"), ("tilray", "cannabis"), ("canopy growth", "cannabis"),
    ("establishment labs", "medical_device"),
    # Financial entity names that accidentally embed theme keywords
    ("blackstone energy", "finance"), ("brookfield asset", "finance"),
    ("blackrock", "finance"), ("vanguard", "finance"),
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
    """

    def __init__(self, config: dict):
        self.min_signal_count = config.get("min_signals_for_beneficiary", 1)
        self.min_relevance = config.get("min_relevance_score", 10.0)
        self.use_graph = config.get("use_graph_for_beneficiaries", True)
        self.max_beneficiaries = config.get("max_beneficiaries_per_theme", 30)
        self._classifier = CompanyClassifier(config)

    def map_theme(
        self,
        theme_slug: str,
        theme_name: str,
        signal_records: list[dict],
        entity_records: list[dict],
        graph_store=None,
        theme_keywords: list[str] = None,
        as_of_date=None,
    ) -> BeneficiaryResult:
        """Identify all beneficiaries for a single theme.

        Args:
            as_of_date: The replay/analysis date. Used to stamp first_seen_at /
                        last_seen_at on beneficiaries so that historical quarterly
                        runs produce correctly-dated rows instead of always using
                        date.today().
        """
        _as_of = as_of_date or date.today()
        result = BeneficiaryResult(theme_slug=theme_slug, theme_name=theme_name)

        # Strategy 1: Signal-based beneficiaries (primary source)
        signal_beneficiaries = self._from_signals(theme_slug, signal_records, theme_keywords, as_of_date=_as_of)
        result.direct.extend(signal_beneficiaries)

        # Strategy 2: Entity co-occurrence beneficiaries
        entity_beneficiaries = self._from_entities(
            theme_slug, entity_records, theme_keywords or [], as_of_date=_as_of
        )
        result.direct.extend(entity_beneficiaries)

        # Strategy 3: Graph supply chain (indirect)
        if self.use_graph and graph_store:
            indirect = self._from_graph(theme_slug, graph_store, as_of_date=_as_of)
            result.indirect.extend(indirect)

        # Deduplicate across categories
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
    ) -> list[BeneficiaryResult]:
        """Map beneficiaries for all themes.

        Args:
            as_of_date: The replay/analysis date. Passed down so beneficiary
                        first_seen_at / last_seen_at are stamped with the correct
                        historical date rather than today.
        """
        seed_map = {}
        if seed_themes:
            seed_map = {s["slug"]: s for s in seed_themes}

        results = []
        for theme in themes:
            slug = theme.theme_slug if hasattr(theme, "theme_slug") else theme.get("theme_slug", "")
            name = theme.theme_name if hasattr(theme, "theme_name") else theme.get("theme_name", "")
            seed = seed_map.get(slug, {})
            # Derive keywords: seed keywords OR entity extracted from theme name
            keywords = self._keywords_from_theme(slug, name, seed)

            result = self.map_theme(
                theme_slug=slug,
                theme_name=name,
                signal_records=signal_records,
                entity_records=entity_records,
                graph_store=graph_store,
                theme_keywords=keywords,
                as_of_date=as_of_date,
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
    def _sector_allowed_for_theme(sector: str, keywords: list[str]) -> bool:
        """Return False if the company's sector is blocked for these theme keywords."""
        if sector == "unknown":
            return True  # give unknowns benefit of the doubt
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
            "signal_count": 0, "capex_signals": 0,
            "ticker": "", "contexts": [], "company": "",
            "quarterly_mentions": {},
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
            company_signal_map[key]["signal_count"] += 1
            company_signal_map[key]["ticker"] = ticker
            company_signal_map[key]["company"] = company
            if ctx:
                company_signal_map[key]["contexts"].append(ctx[:200])
            if "capex" in stype:
                company_signal_map[key]["capex_signals"] += 1
            if filed_quarter:
                qmap = company_signal_map[key]["quarterly_mentions"]
                qmap[filed_quarter] = qmap.get(filed_quarter, 0) + 1

        beneficiaries = []
        for key, data in company_signal_map.items():
            if data["signal_count"] < self.min_signal_count:
                continue

            company_name = data.get("company", key)

            # Gate 3 + 4: sector must align with theme's supply-chain position.
            # e.g. PepsiCo (food sector) is never a legitimate energy-theme beneficiary.
            sector = self._get_company_sector(company_name, data.get("ticker", ""))
            if kw_lower and not self._sector_allowed_for_theme(sector, kw_lower):
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

            # Bottleneck and infrastructure providers get relevance boost
            role_boost = 20.0 if primary_role in (
                CompanyRole.BOTTLENECK_PLAYER, CompanyRole.INFRASTRUCTURE_PROVIDER
            ) else 10.0 if primary_role == CompanyRole.SUPPLIER else 0.0

            # Capex signals are the strongest forward signal — extra weight
            capex_boost = min(data["capex_signals"] * 8.0, 25.0)

            relevance = min(data["signal_count"] * 8.0 + role_boost + capex_boost, 100.0)

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
            if not self._sector_allowed_for_theme(sector, kw_lower):
                continue

            key = ticker or name
            company_scores[key]["score"] += match_score * 15.0
            company_scores[key]["ticker"] = ticker
            company_scores[key]["name"] = name
            company_scores[key]["count"] += 1

        beneficiaries = []
        for key, data in company_scores.items():
            relevance = min(data["score"], 60.0)
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

    def persist(self, results: list[BeneficiaryResult], pg_store, theme_id_map: dict):
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

        # Pass 2: upsert beneficiaries using pre-built entity id map
        total = 0
        for result in results:
            theme_id = theme_id_map.get(result.theme_slug)
            if not theme_id:
                continue

            for b in result.all_beneficiaries:
                key = (b.ticker or b.company_name or b.entity_name).upper()
                entity_id = entity_id_map.get(key)
                if not entity_id:
                    continue
                try:
                    pg_store.upsert_beneficiary({
                        "theme_id": theme_id,
                        "entity_id": entity_id,
                        "ticker": b.ticker,
                        "company_name": b.company_name,
                        "beneficiary_type": b.beneficiary_type,
                        "company_role": getattr(b, "company_role", ""),
                        "relevance_score": b.relevance_score,
                        "signal_count": b.signal_count,
                        "capex_signals": getattr(b, "capex_signals", 0),
                        "quarterly_mentions": getattr(b, "quarterly_mentions", {}),
                        "first_seen_at": b.first_seen_at,
                        "last_seen_at": b.last_seen_at,
                        "rank_in_theme": b.rank_in_theme,
                        "reasoning": b.reasoning,
                    })
                    total += 1
                except Exception as e:
                    logger.warning(f"Failed to persist beneficiary {b.entity_name}: {e}")

        logger.info(
            f"Persisted {total} beneficiaries for {len(results)} themes "
            f"({len(unique_companies)} unique entities upserted)"
        )
