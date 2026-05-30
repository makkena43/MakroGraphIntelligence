"""
Company Role Classifier
~~~~~~~~~~~~~~~~~~~~~~~
Classifies a company's role within a given theme using keyword patterns
extracted from the document text. Avoids ML — pure ontology + regex.

Roles (from the spec):
  - infrastructure_provider  : builds the physical layer (fabs, datacenters, cables)
  - supplier                 : provides components / materials to the value chain
  - bottleneck_player        : controls a scarce resource (HBM, rare earth, bandwidth)
  - beneficiary              : derives revenue/margin gain from the theme
  - downstream_user          : consumes the theme's output (buys AI compute, buys power)
  - hidden_enabler           : non-obvious participant (cooling, packaging, chemicals)

A company can have multiple roles for a single theme.

Example: AI Datacenter theme
  - Nvidia       → bottleneck_player, supplier
  - Micron       → supplier (HBM)
  - HFCL         → infrastructure_provider (fiber backbone)
  - Power Grid   → infrastructure_provider (power evacuation)
  - Reliance Jio → downstream_user (buying AI compute)
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CompanyRole(str, Enum):
    INFRASTRUCTURE_PROVIDER = "infrastructure_provider"
    SUPPLIER = "supplier"
    BOTTLENECK_PLAYER = "bottleneck_player"
    BENEFICIARY = "beneficiary"
    DOWNSTREAM_USER = "downstream_user"
    HIDDEN_ENABLER = "hidden_enabler"


# ---------------------------------------------------------------------------
# Role detection ontology — keyword patterns per role (theme-agnostic layer)
# plus theme-specific overrides.
# ---------------------------------------------------------------------------

ROLE_PATTERNS: dict[str, list[str]] = {
    CompanyRole.INFRASTRUCTURE_PROVIDER: [
        r"\bbuild(?:ing)?\b.{0,30}\b(?:datacenter|fab|plant|facility|network|grid|tower)\b",
        r"\bconstruct(?:ing|ion)?\b",
        r"\binstall(?:ing|ation)?\b.{0,30}\b(?:fiber|cable|tower|substation)\b",
        r"\bcommission(?:ing|ed)?\b",
        r"\bepc\b",                         # engineering, procurement, construction
        r"\binfrastructure provider\b",
        r"\bturnkey\b",
        r"\broll(?:out|ing)\b",
    ],
    CompanyRole.SUPPLIER: [
        r"\bsuppl(?:y|ier|ying)\b",
        r"\bmanufactur(?:e|er|ing)\b",
        r"\bcomponent\b",
        r"\braw material\b",
        r"\bchip maker\b",
        r"\bsemiconductor supplier\b",
        r"\bdesign(?:ed)? for\b",
        r"\bOEM\b",
        r"\bcontract manufacturer\b",
        r"\bfoundry\b",
    ],
    CompanyRole.BOTTLENECK_PLAYER: [
        r"\bsol(?:e|y) supplier\b",
        r"\bmonopoly\b",
        r"\bmarket leader\b.{0,20}\bshare\b",
        r"\bscarcity\b",
        r"\btight supply\b",
        r"\ballocation\b",
        r"\bchoke ?point\b",
        r"\bcritical component\b",
        r"\bno alternative\b",
        r"\bproprietary\b",
        r"\bpatent(?:ed)?\b",
        r"\bspecialized\b.{0,20}\bcapability\b",
        r"\bwaiting list\b",
        r"\bbacklog\b",
    ],
    CompanyRole.BENEFICIARY: [
        r"\bbenefitting? from\b",
        r"\bbeneficiar(?:y|ies)\b",
        r"\bgaining? from\b",
        r"\bwinning? orders?\b",
        r"\bmarket share gain\b",
        r"\brevenue (?:growth|uplift|increase)\b",
        r"\bmargin expansion\b",
        r"\border inflow\b",
        r"\bstrong demand for (?:our|its)\b",
        r"\btalwindwind\b",
        r"\btailwind\b",
    ],
    CompanyRole.DOWNSTREAM_USER: [
        r"\bbuying\b.{0,30}\b(?:power|compute|bandwidth|server|chip)\b",
        r"\bpurchasing\b.{0,30}\b(?:equipment|hardware|service)\b",
        r"\bdeploying\b.{0,30}\b(?:ai|cloud|model)\b",
        r"\bconsume(?:r|rs|d)?\b.{0,30}\b(?:power|bandwidth|compute)\b",
        r"\bend.?user\b",
        r"\bdownstream\b",
        r"\bour customers\b.{0,30}\b(?:use|adopt|buy)\b",
    ],
    CompanyRole.HIDDEN_ENABLER: [
        r"\bcooling\b",
        r"\bthermal management\b",
        r"\bpackag(?:ing|ed)?\b.{0,20}\b(?:chip|semiconductor)\b",
        r"\bsubstrate\b",
        r"\bchemical(?:s)?\b.{0,20}\b(?:process|fab|semiconductor)\b",
        r"\btest(?:ing)?\b.{0,20}\b(?:equipment|chip|wafer)\b",
        r"\bprecision\b.{0,20}\b(?:component|part)\b",
        r"\bspecialty gas\b",
        r"\bphotonics\b",
        r"\binterconnect\b",
    ],
}

# Known company → role overrides (seed knowledge — add more as you learn)
# Key: (company_name_fragment.lower(), theme) → [roles]
KNOWN_COMPANY_ROLES: dict[tuple, list[CompanyRole]] = {
    ("nvidia", "AI_Datacenter"): [CompanyRole.BOTTLENECK_PLAYER, CompanyRole.SUPPLIER],
    ("micron", "Semiconductor_Memory"): [CompanyRole.SUPPLIER, CompanyRole.BOTTLENECK_PLAYER],
    ("tsmc", "Semiconductor_Memory"): [CompanyRole.INFRASTRUCTURE_PROVIDER, CompanyRole.BOTTLENECK_PLAYER],
    ("hfcl", "Optical_Fiber_Network"): [CompanyRole.INFRASTRUCTURE_PROVIDER, CompanyRole.SUPPLIER],
    ("power grid", "Power_Grid_Transmission"): [CompanyRole.INFRASTRUCTURE_PROVIDER],
    ("apar", "Power_Grid_Transmission"): [CompanyRole.SUPPLIER],
    ("polycab", "Power_Grid_Transmission"): [CompanyRole.SUPPLIER],
    ("adani green", "Renewable_Energy"): [CompanyRole.INFRASTRUCTURE_PROVIDER],
    ("waaree", "Renewable_Energy"): [CompanyRole.SUPPLIER],
}


@dataclass
class ClassificationResult:
    """Company role classification for one theme."""
    company: str
    theme: str
    quarter: str
    roles: list[CompanyRole] = field(default_factory=list)
    role_evidence: dict[str, list[str]] = field(default_factory=dict)  # role → snippets
    confidence: float = 0.0    # 0-1
    from_known_list: bool = False

    def primary_role(self) -> Optional[CompanyRole]:
        """Return the role with the most evidence."""
        if not self.roles:
            return None
        # known_list roles come first
        if self.from_known_list:
            return self.roles[0]
        # otherwise, most evidence snippets
        best = max(self.roles, key=lambda r: len(self.role_evidence.get(r, [])))
        return best

    def to_dict(self) -> dict:
        return {
            "company": self.company,
            "theme": self.theme,
            "quarter": self.quarter,
            "roles": [r.value for r in self.roles],
            "primary_role": self.primary_role().value if self.primary_role() else None,
            "confidence": round(self.confidence, 3),
            "from_known_list": self.from_known_list,
        }


class CompanyClassifier:
    """
    Classifies a company's role(s) within a theme using text evidence.
    Applies curated known-company overrides first, then falls back to
    regex pattern matching on document text.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.snippet_window = config.get("snippet_window_chars", 400)
        self.min_pattern_hits = config.get("min_pattern_hits", 1)
        self.use_known_list = config.get("use_known_list", True)
        # Compile patterns
        self._role_patterns: dict[CompanyRole, list[re.Pattern]] = {
            role: [re.compile(p, re.IGNORECASE) for p in patterns]
            for role, patterns in ROLE_PATTERNS.items()
        }

    def classify(
        self,
        company: str,
        theme: str,
        quarter: str,
        text: str,
        theme_snippets: list[str] = None,
    ) -> ClassificationResult:
        """
        Classify company's role(s) for a given theme.

        Args:
            company:        Company name (e.g., "Nvidia Corporation")
            theme:          Theme key (e.g., "AI_Datacenter")
            quarter:        Quarter string (e.g., "Q2-2024")
            text:           Full document text
            theme_snippets: Pre-extracted snippets for the theme (from ThemeTracker)
        """
        result = ClassificationResult(company=company, theme=theme, quarter=quarter)

        # 1. Check known-company list first
        if self.use_known_list:
            known_roles = self._lookup_known(company, theme)
            if known_roles:
                result.roles = known_roles
                result.from_known_list = True
                result.confidence = 0.95
                logger.debug(f"Known classification: {company} → {theme}: {[r.value for r in known_roles]}")
                return result

        # 2. Pattern-based classification on theme context
        search_text = " ".join(theme_snippets) if theme_snippets else text
        role_hits: dict[CompanyRole, list[str]] = {}

        for role, patterns in self._role_patterns.items():
            hits = []
            for pat in patterns:
                for match in pat.finditer(search_text):
                    start = max(0, match.start() - 100)
                    end = min(len(search_text), match.end() + 100)
                    hits.append(search_text[start:end].strip())
            if len(hits) >= self.min_pattern_hits:
                role_hits[role] = hits

        result.roles = list(role_hits.keys())
        result.role_evidence = role_hits

        # 3. Confidence: based on number of distinct roles found and evidence density
        total_hits = sum(len(v) for v in role_hits.values())
        result.confidence = round(min(1.0, total_hits / 10.0), 3)

        # 4. Default to beneficiary if we found theme mentions but no role
        if not result.roles and theme_snippets:
            result.roles = [CompanyRole.BENEFICIARY]
            result.confidence = 0.30
            logger.debug(f"Default role: {company} → {theme}: beneficiary (no pattern match)")

        logger.debug(
            f"Classified {company} for {theme}: {[r.value for r in result.roles]} "
            f"(conf={result.confidence:.2f})"
        )
        return result

    def classify_batch(
        self,
        company: str,
        quarter: str,
        text: str,
        theme_signals: list,  # list[ThemeSignal] from ThemeTracker
    ) -> list[ClassificationResult]:
        """
        Classify company roles across all detected themes in one document.
        theme_signals should be ThemeSignal objects from ThemeTracker.extract().
        """
        results = []
        for signal in theme_signals:
            result = self.classify(
                company=company,
                theme=signal.theme,
                quarter=quarter,
                text=text,
                theme_snippets=signal.snippets,
            )
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lookup_known(self, company: str, theme: str) -> list[CompanyRole]:
        """Check curated known-company-role table."""
        company_lower = company.lower()
        for (company_fragment, theme_key), roles in KNOWN_COMPANY_ROLES.items():
            if company_fragment in company_lower and theme_key == theme:
                return roles
        return []

    @staticmethod
    def describe_role(role: CompanyRole) -> str:
        """Human-readable description of each role for LLM prompts."""
        descriptions = {
            CompanyRole.INFRASTRUCTURE_PROVIDER: (
                "Builds or operates the physical infrastructure enabling the theme "
                "(datacenters, fabs, cables, substations)."
            ),
            CompanyRole.SUPPLIER: (
                "Manufactures and supplies components, materials, or equipment "
                "into the theme's value chain."
            ),
            CompanyRole.BOTTLENECK_PLAYER: (
                "Controls a scarce, hard-to-replicate resource or capability "
                "— limits how fast the theme can scale."
            ),
            CompanyRole.BENEFICIARY: (
                "Derives direct revenue or margin uplift from the theme's growth."
            ),
            CompanyRole.DOWNSTREAM_USER: (
                "Consumes the theme's output product or service "
                "(buys AI compute, purchases clean power, etc.)."
            ),
            CompanyRole.HIDDEN_ENABLER: (
                "Non-obvious participant whose product is essential but rarely discussed "
                "(cooling, specialty chemicals, packaging, test equipment)."
            ),
        }
        return descriptions.get(role, "Unknown role.")
