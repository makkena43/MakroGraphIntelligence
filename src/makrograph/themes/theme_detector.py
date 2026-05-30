"""Cross-sector investment theme detection engine.

Detects emerging themes from:
    1. Cross-sector technology co-occurrence (Neo4j / evolution tracker)
    2. Signal clustering (multiple companies showing same signals)
    3. BERTopic emerging topic clusters
    4. Entity mention acceleration patterns
"""

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..ontology.ontology_model import InvestmentTheme, ThemeConviction

logger = logging.getLogger(__name__)

# ── Entity quality filter ────────────────────────────────────────────────────
# Two-layer approach:
#   Layer 1: STRUCTURAL pattern matching (regex) — catches SEC/XBRL boilerplate
#            that is ALWAYS noise regardless of context (dates, form headers, etc.)
#   Layer 2: STATISTICAL filtering via SmartNoiseFilter (see below) — uses the
#            DATA ITSELF to decide what's noise vs. real theme. No hardcoded word
#            lists or company-count thresholds.

# ── Regex-based STRUCTURAL noise detectors (unchanging boilerplate patterns) ──
_NOISE_RE = re.compile(
    r"(?i)"
    # Dates (ISO, US, bare year, month-prefixed)
    r"^\d{4}-\d{2}-\d{2}"
    r"|^\d{1,2}/\d{1,2}/\d{2,4}"
    r"|^(19|20)\d{2}$"
    r"|^(19|20)\d{2}[-\s]"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|^(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"

    # SEC form structure
    r"|^(?:item|exhibit|section|schedule|form|note|table|part|appendix|annex)\s"
    r"|^(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)[.]?\s"

    # XBRL tags
    r"|member\b|axis\b|domain\b"

    # SEC legal / regulatory boilerplate acts (NOT investment-relevant legislation)
    r"|\b(?:exchange act|securities act|privacy act|patriot act)\b"
    r"|^(?:the\s+)?(?:securities and exchange|exchange commission)\b"
    r"|^(?:the\s+)?(?:securities|exchange|preceding|past|quarterly period)"
    r"|^(?:rule|regulation)\s+\d"
    r"|^(?:united states|washington|state of|address of|commission file)"
    r"|^(?:i\.?r\.?s\.?|irs)\s"
    r"|registrant.s\s"
    r"|^issuer\b"

    # SEC filing types
    r"|form\s+(?:10-[kq]|8-k|s-[1-9]|20-f)"
    r"|annual report|quarterly report|current report"

    # Time references in entity names
    r"|\b(?:year|quarter|period|months?)\s+ended\b"
    r"|\bfiscal\s+(?:year|20\d{2})\b"
    r"|\b(?:first|second|third|fourth)\s+quarter\b"
    r"|(?:last|next)\s+(?:business\s+)?day\b"
    r"|\byear-end\b|\bmulti-year\b"
    r"|^\d+-(?:month|week|year|day)"
    r"|^(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen)-"
    r"|^p\d+[ymd]$"
    r"|\b\d+\s+months?\b|\b\d+\s+years?\b"
    r"|\barea code\b"

    # Financial statement boilerplate
    r"|^(?:condensed\s+)?consolidated\s+(?:statements?|balance|financial)"
    r"|^financial\s+(?:condition|data|statements?|information)"
    r"|^(?:management.s?\s+discussion|mine safety|interactive data)"
    r"|(?:par value|accelerated filer|non-accelerated filer)"
    r"|(?:election of directors|general counsel|board of directors)"
    r"|(?:stock exchange|nyse|nasdaq)\b"
    r"|^(?:each exchange|this (?:annual|quarterly|current))"
    r"|\bdate of report\b"

    # CIK / accession numbers
    r"|\d{7,}"

    # Accounting / financial reporting standards
    r"|\b(?:gaap|fasb|ifrs|asc|sfas)\b"
    r"|^(?:documents?\s+incorporated|cover page|press release)"
    r"|^(?:results of operations|jurisdiction of incorporation)"
    r"|^(?:additional information|investor relations)"
    r"|\b(?:merger agreement|credit agreement|collaboration agreement)\b"
    r"|\bindenture\b"

    # Temporal / reporting-period terms that slip through entity extraction
    r"|^(?:semi.?annual|annual|monthly|weekly|quarterly|bi.?annual)(?:ly)?\b"
    r"|^(?:three|six|nine|twelve)\s+months?\b"
    r"|^(?:second|third|fourth|first)\s+fiscal"
    r"|^the\s+year\b|^the\s+quarter\b|^the\s+period\b|^the\s+month\b"
    r"|^(?:year|quarter|month|period|week)\s+\d"

    # SEC legal / underwriting boilerplate that escapes XBRL filter
    r"|^(?:the\s+)?underwriting\s+agreement"
    r"|^(?:the\s+)?committee\b"
    r"|^(?:the\s+)?(?:board|committee|management|officers?)\b"
    r"|^plan\b|^plans\b"
    r"|^filer\b|^registrant\b|^issuer\b"
    r"|^exhibit\b|^exhibits\b"
    r"|inline\s+xbrl|interactive\s+data"

    # Common single-word corporate/legal/financial terms with no theme specificity
    r"|^today\b|^tomorrow\b|^yesterday\b"
    r"|^charter\b|^charters\b"
    r"|^agreement\b|^agreements\b"
    r"|^diluted\b|^accretive\b|^dilutive\b"
    r"|^compensatory\b|^compensation\b"
    r"|^operations\b|^operation\b"
    r"|^treasury\b"
    r"|^one\s+year\b|^two\s+year|^three\s+year"
    r"|^item\b|^items\b"
    r"|^proceeds\b|^consideration\b|^transaction\b"
    r"|^amendment\b|^amendments\b"
    r"|^notice\b|^notices\b"
    r"|^certificate\b|^certificates\b"
    r"|^prospectus\b|^prospectuses?\b"
    r"|^warrant\b|^warrants\b"
    r"|^covenant\b|^covenants\b"
    # SEC address boilerplate
    r"|^d\.?\s*c\.?\s*\d{5}"        # D.C. 20549 and variants
    r"|^washington\s*,?\s*d\.?\s*c"  # Washington, D.C.
    # Abstract/too-generic economic terms that slip through
    r"|^shortage\b|^shortages\b"
    r"|^geopolitical\b|^geopolitics\b"
    r"|^notes?\b"                    # "Notes" as standalone (SEC footnotes)

    # Bank / underwriter entity names that appear in every equity filing
    r"|\b(?:bofa|merrill lynch|goldman sachs|jp morgan|morgan stanley|"
    r"wells fargo|citigroup|barclays|deutsche bank|credit suisse|ubs|"
    r"bank of america|national association|trust company|bancorp)\b"

    # Geographic / address boilerplate
    r"|\b(?:park avenue|madison avenue|fifth avenue|wall street|"
    r"main street|broadway)\b"
)

# Case-SENSITIVE: XBRL camelCase tags
_XBRL_CAMEL_RE = re.compile(
    r"^[A-Z][a-z]+(?:[A-Z][a-z]+){2,}"
)

_STOPWORDS = frozenset({
    "the", "of", "and", "for", "in", "a", "an", "to", "or", "by",
    "on", "at", "with", "as", "is", "are", "was", "were", "its", "our",
    "this", "that", "which", "each", "such", "other", "any", "all",
})

# Generic economic/financial nouns that are valid SIGNAL CONTEXTS but too broad
# to be an investable theme on their own as entity names.
# E.g. "capital expenditure" is a SIGNAL TYPE, not a theme candidate.
# Real themes need specific technology/sector/product names.
_GENERIC_ECONOMIC_NOUNS: frozenset[str] = frozenset({
    # SEC boilerplate / abstract noise — not investable standalone themes
    "shortage", "shortages",        # context word, not a theme (e.g. "chip shortage" is a signal)
    "geopolitical", "geopolitics",  # abstract risk modifier
    "notes",                        # SEC footnote header
    "d.c. 20549",                   # SEC mailing address
    "washington d.c.",
    # Macro / economic concepts
    "inflation", "deflation", "recession", "stagflation", "monetary policy",
    "fiscal policy", "interest rate", "interest rates", "federal reserve",
    "central bank", "exchange rate", "currency", "liquidity", "credit",
    "yield curve", "gdp", "gross domestic product", "consumer price index",
    "cpi", "ppi", "unemployment", "employment",

    # Generic financial terms (these are signal types, not themes)
    "capital expenditure", "capital expenditures", "capex", "opex",
    "revenue", "revenue growth", "earnings", "earnings growth",
    "profit margin", "gross margin", "operating margin", "ebitda",
    "cash flow", "free cash flow", "working capital", "balance sheet",
    "debt", "leverage", "buyback", "dividend", "valuation", "multiple",

    # Generic business concepts
    "overcapacity", "excess capacity", "demand", "supply", "pricing",
    "cost", "costs", "expenses", "investment", "investments",
    "growth", "expansion", "market share", "competition", "margin",
    "profitability", "productivity", "efficiency", "scale", "volume",
    "company", "companies", "business", "businesses", "enterprise", "enterprises",
    "regulatory", "regulation", "regulations", "compliance", "legal",
    "sanctions", "tariff", "tariffs", "trade", "export", "import",
    "supply chain", "supply chains", "value chain", "logistics",
    "restructuring", "transformation", "transition", "pivot",
    "guidance", "outlook", "forecast", "strategy", "strategic",
    "risk", "risks", "uncertainty", "volatility", "headwind", "tailwind",

    # Generic technology terms (too broad without specificity)
    "technology", "innovation", "digitalization", "digital transformation",
    "automation", "electrification", "decarbonization", "sustainability",
    "infrastructure", "platform", "software", "hardware", "services",
    "solutions", "products", "systems", "applications", "tools",

    # Generic sector names (sectors are context, not themes)
    "energy sector", "technology sector", "financial sector",
    "healthcare sector", "industrial sector", "consumer sector",

    # Ordinals / numerals — extracted from NSE boilerplate (e.g. "thousands of shares")
    "thousands", "millions", "billions", "hundreds",
    "first", "second", "third", "fourth", "fifth",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "crore", "lakh", "lakhs", "crores",          # Indian number words
    "percent", "per cent", "percentage",

    # Generic corporate / legal / operational nouns (no investment specificity)
    "operations", "operation", "treasury",
    "agreement", "agreements", "charter", "charters",
    "diluted", "accretive", "dilutive", "compensatory", "compensation",
    "today", "item", "items",
    "one year", "two year", "three year",
    "proceeds", "consideration", "transaction",
    "amendment", "amendments", "notice", "notices",
    "certificate", "certificates", "prospectus",
    "warrant", "warrants", "covenant", "covenants",
    "buildout", "build-out",  # suffix, not a theme on its own
})

# ── Entity alias canonicalization ─────────────────────────────────────────────
# Normalize variant spellings → canonical form so the same physical bottleneck
# always surfaces as one entity, never splits into "chip" + "semiconductor".
_ENTITY_ALIASES: dict[str, str] = {
    # Chips / semiconductors
    "chip":                 "semiconductor",
    "chips":                "semiconductor",
    "microchip":            "semiconductor",
    "microchips":           "semiconductor",
    "integrated circuit":   "semiconductor",
    "ic":                   "semiconductor",
    # AI / ML
    "ml":                   "artificial intelligence",
    "machine learning":     "artificial intelligence",
    "deep learning":        "artificial intelligence",
    "neural network":       "artificial intelligence",
    "llm":                  "artificial intelligence",
    "large language model": "artificial intelligence",
    "genai":                "artificial intelligence",
    "generative ai":        "artificial intelligence",
    # GPU / AI accelerator
    "gpu":                  "ai accelerator",
    "gpus":                 "ai accelerator",
    "graphics processing unit": "ai accelerator",
    "npu":                  "ai accelerator",
    "tpu":                  "ai accelerator",
    # Memory
    "high bandwidth memory": "hbm",
    "hbm memory":           "hbm",
    "hbm3":                 "hbm",
    "hbm3e":                "hbm",
    "dram":                 "memory",
    "nand":                 "nand flash",
    # Data center
    "datacenter":           "data center",
    "data centre":          "data center",
    "hyperscaler":          "data center",
    "hyperscale":           "data center",
    # Power / grid
    "electrical grid":      "power grid",
    "electricity grid":     "power grid",
    "transmission grid":    "power grid",
    "power infrastructure": "power grid",
    # EV
    "ev":                   "electric vehicle",
    "bev":                  "electric vehicle",
    "phev":                 "electric vehicle",
    "battery electric vehicle": "electric vehicle",
    # Packaging
    "cowos":                "advanced packaging",
    "chiplet":              "advanced packaging",
    "2.5d":                 "advanced packaging",
    "3d ic":                "advanced packaging",
    # Cloud
    "cloud computing":      "cloud",
    "cloud infrastructure": "cloud",
    "public cloud":         "cloud",
    # Cooling
    "liquid cooling":       "cooling",
    "immersion cooling":    "cooling",
    "direct liquid cooling": "cooling",
}

# ── Constraint / bottleneck signal keywords ──────────────────────────────────
# These phrases in management language are THE strongest evidence of a supply
# bottleneck that can generate explosive pricing power for the constrained supplier.
# Weight > 1.0 means this phrase boosts the entity's strength score.
_CONSTRAINT_KEYWORDS: dict[str, float] = {
    "shortage":          1.50,
    "shortages":         1.50,
    "sold out":          1.50,
    "fully booked":      1.50,
    "cannot meet demand": 1.50,
    "demand exceeds":    1.45,
    "allocation":        1.40,
    "rationing":         1.40,
    "lead time":         1.35,
    "extended lead":     1.35,
    "backlog":           1.30,
    "bottleneck":        1.30,
    "constrained":       1.25,
    "capacity tight":    1.25,
    "supply tight":      1.25,
    "order push":        1.20,
    "push-out":          1.20,
    "wait list":         1.20,
    "supply limited":    1.15,
}

# Known bottleneck entity → canonical theme name mapping.
# These are the explosive picks-and-shovels plays MakroGraph is designed to surface.
_BOTTLENECK_THEMES: dict[str, str] = {
    "transformer":        "Grid Transformer Shortage",
    "power grid":         "Power Grid Capacity Constraint",
    "copper":             "Copper Supply Bottleneck",
    "hbm":                "HBM Memory Supply Constraint",
    "advanced packaging": "Advanced Packaging Bottleneck",
    "cooling":            "Data Center Cooling Bottleneck",
    "liquid nitrogen":    "Semiconductor Gas Shortage",
    "neon":               "Semiconductor Gas Shortage",
    "argon":              "Semiconductor Gas Shortage",
    "photoresist":        "Semiconductor Chemical Shortage",
    "cobalt":             "EV Battery Material Shortage",
    "lithium":            "Lithium Supply Bottleneck",
    "gallium":            "Critical Mineral Export Constraint",
    "germanium":          "Critical Mineral Export Constraint",
    "rare earth":         "Rare Earth Supply Constraint",
    "silicon carbide":    "SiC Wafer Supply Constraint",
    "silicon":            "Wafer Supply Constraint",
    "ai accelerator":     "AI Accelerator Supply Constraint",
    "substation":         "Grid Infrastructure Bottleneck",
    "natural gas":        "Natural Gas Supply Constraint",
    "uranium":            "Nuclear Fuel Supply Constraint",
}


def _normalize_entity_name(name: str) -> str:
    """Canonicalize entity name using alias table.

    Converts variant spellings to a single canonical form so that
    'chip', 'microchip', and 'semiconductor' all become 'semiconductor'.
    Preserves case of the canonical form (lowercase).
    """
    lower = name.lower().strip()
    return _ENTITY_ALIASES.get(lower, lower)


def _is_noise_entity(name: str) -> bool:
    """Layer 1: Return True if the entity is STRUCTURALLY noise.

    Uses format/pattern-based checks AND a semantic generic-noun list.
    The semantic list covers concepts that are valid economic contexts
    but too generic to be investable themes (e.g. 'capital expenditure',
    'inflation', 'growth').
    """
    if not name:
        return True
    # Allow critical 2-char+ short technology identifiers:
    #   - Pure uppercase alpha acronyms: AI, EV, 5G-style (digit + letter)
    _SHORT_TECH_RE = re.compile(r"^(\d[A-Z]|[A-Z]\d|[A-Z]{2})$")
    if len(name) <= 3 and _SHORT_TECH_RE.match(name):
        return False
    if len(name) < 3:
        return True
    # Filter other short names unless they're uppercase acronyms (GPU, HBM, LNG)
    if len(name) < 4 and not name.isupper():
        return True
    if not name[0].isalpha():
        return True
    if re.match(r"^[\d\s%$.,\-/()]+$", name):
        return True
    if _NOISE_RE.search(name):
        return True
    if _XBRL_CAMEL_RE.match(name):
        return True

    name_lower = name.lower().strip()
    words = name_lower.split()

    # >5 words → almost always SEC section header
    if len(words) > 5:
        return True

    # Multi-word phrases composed entirely of stopwords
    if all(w in _STOPWORDS for w in words):
        return True

    # Generic economic/financial nouns — valid contexts but not investable themes
    if name_lower in _GENERIC_ECONOMIC_NOUNS:
        return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# SMART NOISE FILTER — Statistical, data-driven noise detection
# ══════════════════════════════════════════════════════════════════════════════

class SmartNoiseFilter:
    """Statistically determines which entities are noise vs. real investment themes.

    Instead of hardcoded word blacklists or fixed company-count thresholds,
    this class computes noise scores from the DATA DISTRIBUTION itself:

    1. **Inverse Company Frequency (ICF)**: entities appearing in too many
       companies relative to the dataset are generic (like IDF in search).
    2. **Sector Entropy**: real themes concentrate in specific sectors;
       noise spreads uniformly. Low entropy = likely real theme.
    3. **Gini Concentration**: measures how skewed the signal distribution
       is across companies. High Gini = concentrated = real theme.
    4. **Word-shape scoring**: domain-specific morphology (acronyms,
       capitalized multi-word, technical suffixes) boosts theme likelihood.
    5. **Signal coherence**: real themes generate correlated signal types
       (demand + supply together), noise generates random signal types.

    The filter auto-adapts to any dataset size — no fixed thresholds needed.
    """

    def __init__(self, total_companies: int, total_sectors: int = 0):
        self._total_companies = max(total_companies, 1)
        self._total_sectors = max(total_sectors, 1)
        # Adaptive minimum: at least 2 companies, scales with sqrt of dataset
        # For 10 companies → min 2, for 50 → min 3, for 200 → min 4
        self._adaptive_min_companies = max(2, int(math.sqrt(self._total_companies) * 0.5))
        # Maximum company ratio before an entity is considered ubiquitous
        # Adapts: smaller datasets tolerate higher ratios
        self._ubiquity_cap = min(0.7, 0.4 + 3.0 / self._total_companies)

    @property
    def adaptive_min_companies(self) -> int:
        """Dynamic min company threshold based on dataset size."""
        return self._adaptive_min_companies

    def compute_noise_score(self, entity_name: str, n_companies: int,
                            n_sectors: int, signal_counts: Counter,
                            total_signals: int = 0) -> float:
        """Compute a noise score in [0, 1]. Higher = more likely noise.

        Returns a composite score combining all statistical signals.
        Entities scoring > threshold (default 0.55) are filtered as noise.
        """
        scores = []

        # ── 1. Inverse Company Frequency ─────────────────────────────────────
        # ICF: entities appearing in too many companies = too generic = noise.
        company_ratio = n_companies / self._total_companies
        icf_noise = self._sigmoid(company_ratio, midpoint=self._ubiquity_cap, steepness=8.0)
        scores.append((icf_noise, 0.30))  # 30% weight

        # ── 2. Sector Entropy ────────────────────────────────────────────────
        # Real themes concentrate in specific sectors; noise is uniform.
        if self._total_sectors > 1 and n_sectors > 0:
            max_entropy = math.log2(self._total_sectors)
            entity_entropy = math.log2(n_sectors) if n_sectors > 1 else 0.0
            entropy_ratio = entity_entropy / max_entropy if max_entropy > 0 else 0.0
            entropy_noise = entropy_ratio ** 0.8
        else:
            entropy_noise = 0.0
        scores.append((entropy_noise, 0.15))  # 15% weight

        # ── 3. Gini Concentration of signals ─────────────────────────────────
        # Real themes: signals concentrated in specific companies (high Gini).
        # Noise: signals spread evenly (low Gini → 1 signal per company).
        if n_companies > 0 and total_signals > 0:
            avg_signals_per_company = total_signals / n_companies
            concentration_proxy = 1.0 - min(avg_signals_per_company / 5.0, 1.0)
            gini_noise = concentration_proxy * 0.5
        else:
            gini_noise = 0.3  # neutral
        scores.append((gini_noise, 0.15))  # 15% weight

        # ── 4. Word-shape scoring ────────────────────────────────────────────
        word_shape_noise = self._word_shape_noise_score(entity_name)
        scores.append((word_shape_noise, 0.25))  # 25% weight

        # ── 5. Signal coherence ──────────────────────────────────────────────
        coherence_noise = self._signal_coherence_noise(signal_counts)
        scores.append((coherence_noise, 0.15))  # 15% weight

        # Weighted average
        total_weight = sum(w for _, w in scores)
        noise_score = sum(s * w for s, w in scores) / total_weight
        return noise_score

    def is_noise(self, entity_name: str, n_companies: int, n_sectors: int,
                 signal_counts: Counter, total_signals: int = 0,
                 threshold: float = 0.55) -> bool:
        """Return True if the entity should be filtered as noise.

        The threshold auto-adjusts based on dataset characteristics:
        - Smaller datasets (< 10 companies): more lenient (higher threshold)
        - Larger datasets (> 50 companies): stricter filtering
        """
        adaptive_threshold = threshold
        if self._total_companies < 10:
            adaptive_threshold = threshold + 0.05
        elif self._total_companies > 50:
            adaptive_threshold = threshold - 0.05

        score = self.compute_noise_score(
            entity_name, n_companies, n_sectors, signal_counts, total_signals
        )
        return score > adaptive_threshold

    def passes_minimum_evidence(self, n_companies: int, n_docs: int) -> bool:
        """Check if entity has enough evidence to be considered a theme.

        Uses adaptive thresholds instead of hardcoded values like 'min 3 companies'.
        """
        if n_companies < self._adaptive_min_companies:
            return False
        # Adaptive doc threshold: at least 1.5x the company count
        min_docs = max(2, int(self._adaptive_min_companies * 1.2))
        if n_docs < min_docs:
            return False
        return True

    def _word_shape_noise_score(self, name: str) -> float:
        """Score how much the word SHAPE suggests noise vs. real theme.

        Returns 0.0 (definitely a theme) to 1.0 (definitely noise).

        Heuristics (learned from patterns in SEC filings):
          - Uppercase acronyms (GPU, HBM, AI, EV) → very likely theme
          - Capitalized multi-word ("Advanced Packaging") → likely theme
          - Technical suffixes/prefixes → theme
          - All-lowercase single generic word → likely noise
          - Contains digits mixed with text → depends on pattern
        """
        if not name:
            return 1.0

        words = name.split()
        lower = name.lower()

        # Pure uppercase acronym (2-6 chars): GPU, HBM, AI, EV, ASIC
        if name.isupper() and 2 <= len(name) <= 6 and name.isalpha():
            return 0.05

        # Mixed case acronym with numbers: HBM3, 5G, H100
        if len(name) <= 6 and any(c.isupper() for c in name) and any(c.isdigit() for c in name):
            return 0.1

        # Capitalized multi-word (Title Case): "Advanced Packaging", "Data Center"
        if len(words) >= 2 and all(
            w[0].isupper() or w in _STOPWORDS for w in words if w
        ):
            return 0.15

        # Technical compound terms with hyphens: "multi-chip", "high-bandwidth"
        if "-" in name and len(words) <= 3:
            return 0.2

        # Domain-specific indicators that strongly suggest real themes
        _THEME_INDICATORS = (
            "chip", "semi", "solar", "grid", "power", "data", "cloud",
            "fiber", "nuclear", "battery", "lithium", "hydrogen",
            "drone", "satellite", "quantum", "biotech", "genomic",
            "wafer", "foundry", "transformer", "turbine", "laser",
            "radar", "sensor", "roboti", "autonomous", "neural",
        )
        if any(indicator in lower for indicator in _THEME_INDICATORS):
            return 0.1

        # Single word analysis (no blacklist — use word properties)
        if len(words) == 1:
            word = lower.strip()
            # Very short single words (not acronyms) are usually noise
            if len(word) <= 4:
                return 0.75
            # Abstract noun suffixes (often generic, not actionable)
            _ABSTRACT_SUFFIXES = ("tion", "ment", "ness", "ity", "ance", "ence")
            if any(word.endswith(s) for s in _ABSTRACT_SUFFIXES) and len(word) <= 10:
                return 0.6
            # Action/process suffixes: could go either way
            if word.endswith("ing") and len(word) <= 9:
                return 0.55
            # Medium-length single word — no strong signal
            return 0.45

        # Multi-word, all lowercase, no capitalization pattern
        if name == lower and len(words) >= 2:
            return 0.4

        # Default: neutral
        return 0.35

    def _signal_coherence_noise(self, signal_counts: Counter) -> float:
        """Score signal incoherence (0 = coherent theme, 1 = random noise).

        Coherent themes have:
          - Both demand AND supply signals (tension = real theme)
          - Dominated by 1-2 signal types (not spread across all types)

        Noise entities:
          - Only 1 signal occurrence total
          - Spread thin across many types with no dominance
        """
        if not signal_counts:
            return 0.8

        total = sum(signal_counts.values())
        n_types = len(signal_counts)

        if total <= 1:
            return 0.7

        # Check for demand-supply tension (hallmark of real themes)
        DEMAND = {"demand_surge", "capex_increase", "hiring_surge",
                  "technology_adoption", "market_entry"}
        SUPPLY = {"supply_bottleneck", "inventory_drawdown"}
        has_demand = any(k in DEMAND for k in signal_counts)
        has_supply = any(k in SUPPLY for k in signal_counts)

        if has_demand and has_supply:
            return 0.05  # very coherent — definitely a theme

        # Dominance: is one signal type dominant? (good for themes)
        top_count = signal_counts.most_common(1)[0][1]
        dominance = top_count / total

        if dominance > 0.6 and total >= 3:
            return 0.2  # strongly dominated = focused theme
        if n_types >= 4 and dominance < 0.3:
            return 0.6  # spread thin = probably noise

        return 0.4  # neutral

    @staticmethod
    def _sigmoid(x: float, midpoint: float = 0.5, steepness: float = 10.0) -> float:
        """Sigmoid function for smooth threshold transitions."""
        z = steepness * (x - midpoint)
        if z > 500:
            return 1.0
        if z < -500:
            return 0.0
        return 1.0 / (1.0 + math.exp(-z))



# SEED_THEMES removed — all theme detection is fully automatic.
# Themes are discovered purely from signal co-occurrence in filings.
SEED_THEMES = [   # kept as empty list so existing references compile cleanly
    # ══════════════════════════════════════════════════════════════════════
]   # empty — all detection is fully data-driven


@dataclass
class ThemeCandidate:
    """A candidate theme under evaluation before confirmation."""
    seed: dict
    matched_companies: list[str] = field(default_factory=list)
    matched_signals: dict = field(default_factory=dict)   # signal_type -> count
    matched_keywords: list[str] = field(default_factory=list)
    matched_sectors: list[str] = field(default_factory=list)
    entity_ids: list[int] = field(default_factory=list)
    doc_count: int = 0
    raw_score: float = 0.0


class ThemeDetector:
    """Detects and scores investment themes from signals, entities, and graph data.

    Three detection strategies:
        1. Seed-based: match documents against predefined theme templates
        2. Signal clustering: group similar signals across companies
        3. Graph-based: use Neo4j cross-sector queries to surface emergent themes
    """

    def __init__(self, config: dict):
        self.min_companies = config.get("min_companies_for_theme", 2)
        self.min_doc_count = config.get("min_docs_for_theme", 3)
        self.signal_window_days = config.get("signal_window_days", 90)
        self.use_graph = config.get("use_graph_detection", True)
        self._graph_unavailable = False  # circuit breaker: set after first connection failure

    def detect_from_signals(
        self,
        signal_records: list[dict],
        entity_records: list[dict],
        include_auto_cluster: bool = False,
    ) -> list[InvestmentTheme]:
        """Purely data-driven theme detection — no hardcoded seed templates.

        Runs automatic signal clustering: any entity/technology that generates
        signals across 2+ companies and 2+ sectors becomes a theme candidate.
        """
        themes: list[InvestmentTheme] = []

        if include_auto_cluster:
            auto_themes = self.detect_from_signal_clusters(signal_records, entity_records)
            themes.extend(auto_themes)

        return themes

    def detect_from_signal_clusters(
        self,
        signal_records: list[dict],
        entity_records: list[dict],
    ) -> list[InvestmentTheme]:
        """Purely data-driven theme discovery with no pre-defined seed list.

        Algorithm:
          1. Build a map: entity_name → {companies, sectors, signal_types, doc_ids, contexts}
          2. For each entity that appears across N+ companies and S+ sectors → theme candidate
          3. Score by company breadth × signal volume × signal type diversity
          4. Auto-name the theme from entity + dominant signal (e.g. "HBM Supply Bottleneck")
          5. Auto-slug: "auto-{entity_slug}-{dominant_signal_type}"

        This means the pipeline will surface themes like:
          "AI Capex Surge"          (AI mentioned in capex_increase by 5+ companies)
          "HBM Supply Bottleneck"   (HBM in supply_bottleneck signals, 3+ companies)
          "Solar Energy Buildout"   (solar in capex_increase, 4+ companies, 2+ sectors)
        without any human ever writing a template.
        """
        # Build entity → signal cluster map
        entity_map: dict[str, dict] = defaultdict(lambda: {
            "companies": set(),
            "sectors": set(),
            "signal_types": Counter(),
            "doc_ids": set(),
            "contexts": [],
            "capex_count": 0,
        })

        # Track (signal_id, entity_name) to avoid counting same signal twice for same entity
        seen_signal_entity: set[tuple] = set()

        for sig in signal_records:
            # Each row is a (signal, entity) pair — canonical_name is the entity
            # from the same document, not the signal's own entity_id link.
            entity_name = (sig.get("canonical_name") or "").strip()
            if _is_noise_entity(entity_name):
                continue

            entity_type = sig.get("entity_type", "")
            company = sig.get("company") or sig.get("doc_ticker") or ""
            # Sector entities in the doc serve as sector tags for this signal
            sector = entity_name if entity_type == "SECTOR" else (sig.get("sector") or "")
            stype = sig.get("signal_type") or ""
            sig_id = sig.get("signal_id") or sig.get("id") or id(sig)
            doc_id = sig.get("document_id")
            ctx = (sig.get("context_text") or "")[:200]

            # Deduplicate: count each signal once per entity cluster
            dedup_key = (sig_id, entity_name)
            already_counted = dedup_key in seen_signal_entity
            seen_signal_entity.add(dedup_key)

            cluster = entity_map[entity_name]
            if company:
                cluster["companies"].add(company)
            if sector:
                cluster["sectors"].add(sector)
            if doc_id:
                cluster["doc_ids"].add(doc_id)
            if ctx and not already_counted:
                cluster["contexts"].append(ctx)
            # Only count signal type and capex once per signal per entity
            if not already_counted:
                if stype:
                    cluster["signal_types"][stype] += 1
                if "capex" in stype:
                    cluster["capex_count"] += 1

        # ── Signal type classification ──────────────────────────────────────
        # A theme is valuable when demand is surging AND supply is constrained.
        # That tension = pricing power → margin expansion → earnings acceleration → 5x stock.
        DEMAND_SIGNALS = frozenset({
            "demand_surge", "capex_increase", "hiring_surge",
            "technology_adoption", "market_entry",
        })
        SUPPLY_CONSTRAINT_SIGNALS = frozenset({
            "supply_bottleneck", "inventory_drawdown",
        })

        auto_themes: list[InvestmentTheme] = []
        existing_seed_slugs: set[str] = set()  # no seed list — all auto-discovered

        # ── STATISTICAL NOISE FILTER ─────────────────────────────────────────
        # Uses data distribution to determine noise — no hardcoded thresholds.
        all_companies_in_dataset: set[str] = set()
        all_sectors_in_dataset: set[str] = set()
        for cluster in entity_map.values():
            all_companies_in_dataset.update(cluster["companies"])
            all_sectors_in_dataset.update(cluster["sectors"])
        total_companies_in_dataset = max(len(all_companies_in_dataset), 1)
        total_sectors_in_dataset = max(len(all_sectors_in_dataset), 1)

        noise_filter = SmartNoiseFilter(
            total_companies=total_companies_in_dataset,
            total_sectors=total_sectors_in_dataset,
        )

        for entity_name, cluster in entity_map.items():
            if _is_noise_entity(entity_name):
                continue

            signal_counts = cluster["signal_types"]
            n_companies = len(cluster["companies"])
            n_sectors = len(cluster["sectors"])
            n_docs = len(cluster["doc_ids"])
            total_signals = sum(signal_counts.values())

            # ── Statistical noise check (replaces hardcoded ubiquity + blacklist) ──
            if noise_filter.is_noise(entity_name, n_companies, n_sectors,
                                     signal_counts, total_signals):
                continue

            # ── Supply-Demand Tension Gate ─────────────────────────────────
            demand_count = sum(
                v for k, v in signal_counts.items() if k in DEMAND_SIGNALS
            )
            supply_constraint_count = sum(
                v for k, v in signal_counts.items() if k in SUPPLY_CONSTRAINT_SIGNALS
            )
            capex_count = cluster["capex_count"]

            # Gate 1: Classic supply-demand tension (most reliable)
            has_tension = demand_count >= 2 and supply_constraint_count >= 2
            # Gate 2: Heavy capex commitment = structural theme
            min_capex_cos = max(3, noise_filter.adaptive_min_companies)
            has_capex_conviction = (
                capex_count >= min_capex_cos
                and n_companies >= min_capex_cos
            )
            # Gate 3: Demand surging ahead of supply (early detection)
            # Multiple companies report strong demand before supply constraints
            # emerge — this is Stage 0/1: the investment sweet spot.
            min_demand_cos = max(4, noise_filter.adaptive_min_companies)
            has_demand_surge_early = (
                demand_count >= 4
                and supply_constraint_count < 2
                and n_companies >= min_demand_cos
                and capex_count >= 2
            )

            if not has_tension and not has_capex_conviction and not has_demand_surge_early:
                continue

            # Adaptive minimum evidence
            if not noise_filter.passes_minimum_evidence(n_companies, n_docs):
                continue

            # ── Tension Score ─────────────────────────────────────────────
            if has_tension:
                tension_score = min(
                    2.0 * demand_count * supply_constraint_count
                    / (demand_count + supply_constraint_count)
                    * 12.0,
                    60.0,
                )
            elif has_demand_surge_early:
                tension_score = min(demand_count * 4.0, 30.0)
            else:
                tension_score = 0.0

            capex_bonus = min(capex_count * 8.0, 30.0)
            breadth_bonus = min(n_companies * 2.0, 20.0)
            quarterly_bonus = 10.0 if n_docs >= 6 else (5.0 if n_docs >= 3 else 0.0)

            strength = min(
                tension_score + capex_bonus + breadth_bonus + quarterly_bonus,
                100.0,
            )

            # Dominant signal
            for priority in ("supply_bottleneck", "demand_surge", "capex_increase"):
                if signal_counts.get(priority, 0) > 0:
                    dominant_signal = priority
                    break
            else:
                dominant_signal = signal_counts.most_common(1)[0][0] if signal_counts else "demand_surge"

            conviction = (
                ThemeConviction.CONFIRMED if tension_score >= 40 and n_companies >= 4
                else ThemeConviction.DEVELOPING if tension_score >= 20 or has_capex_conviction
                else ThemeConviction.EMERGING
            )

            theme_name = self._auto_theme_name(
                entity_name, dominant_signal, capex_count,
                has_tension=has_tension, has_demand_early=has_demand_surge_early,
            )
            entity_slug = re.sub(r"[^a-z0-9]+", "-", entity_name.lower()).strip("-")[:30]
            sig_slug = dominant_signal.replace("_", "-")[:15]
            theme_slug = f"auto-{entity_slug}-{sig_slug}"
            if theme_slug in existing_seed_slugs:
                continue

            if has_tension:
                description = (
                    f"⚡ Supply-Demand Tension: '{entity_name}' — "
                    f"{demand_count} demand signals vs {supply_constraint_count} supply constraints "
                    f"across {n_companies} companies ({n_docs} filings). "
                    f"Capex committed: {capex_count}."
                    + (" High conviction." if tension_score >= 40 else "")
                )
            elif has_demand_surge_early:
                description = (
                    f"📈 Demand Running Ahead of Supply: '{entity_name}' — "
                    f"{demand_count} demand surge signals across {n_companies} companies. "
                    f"Supply constraints not yet visible — early-stage formation. "
                    f"Capex: {capex_count} commits. Watch for supply bottleneck emergence."
                )
            else:
                description = (
                    f"🏗️ Capex Buildout: '{entity_name}' — "
                    f"{capex_count} capex commits across {n_companies} companies ({n_docs} filings). "
                    f"Demand signals: {demand_count}."
                )

            auto_themes.append(InvestmentTheme(
                theme_name=theme_name,
                theme_slug=theme_slug,
                description=description,
                sectors=list(cluster["sectors"]),
                signal_types=list(signal_counts.keys()),
                strength_score=round(strength, 2),
                momentum_score=50.0,  # neutral placeholder; ranker computes from snapshot slope
                conviction=conviction,
                doc_count=n_docs,
                company_count=n_companies,
                metadata={
                    "demand_count": demand_count,
                    "supply_constraint_count": supply_constraint_count,
                    "tension_score": round(tension_score, 2),
                    "capex_count": capex_count,
                },
            ))

        # Sort by tension score descending, cap at top 25 auto-detected themes
        # (quality over quantity — 25 high-signal themes >> 60 noisy themes)
        auto_themes.sort(key=lambda t: -(t.metadata.get("tension_score", 0) + t.strength_score))
        auto_themes = auto_themes[:25]

        logger.info(
            f"Supply-demand tension detection: {len(auto_themes)} themes "
            f"from {len(entity_map)} entities "
            f"(adaptive min_companies={noise_filter.adaptive_min_companies}, "
            f"tension gate: demand>=2 + supply>=2)"
        )
        return auto_themes

    def detect_from_clusters_agg(
        self,
        cluster_rows: list[dict],
        causal_chain_entities: Optional[frozenset] = None,
    ) -> list[InvestmentTheme]:
        """Fast path: auto-detect themes from PRE-AGGREGATED cluster rows.

        Accepts the output of pg_store.get_entity_signal_clusters_in_window()
        (one row per entity, with signal_type_counts dict already computed).
        Runs the same tension-gate and scoring logic as detect_from_signal_clusters
        but skips the 600K-row Python loop entirely.

        Args:
            cluster_rows: list of dicts from get_entity_signal_clusters_in_window.
                Each row: {canonical_name, entity_type, companies(list),
                           document_ids(list), signal_type_counts(dict),
                           total_signals(int), capex_count(int),
                           first_signal_date(date), quarter_count(int)}
            causal_chain_entities: optional frozenset of lowercase entity-name
                keywords from ACTIVE causal chains.  Any theme entity whose name
                (lowercased) matches a keyword here gets a +15 causal-chain
                evidence boost to its strength score.
        """
        DEMAND_SIGNALS = frozenset({
            "demand_surge", "capex_increase", "hiring_surge",
            "technology_adoption", "market_entry",
        })
        SUPPLY_CONSTRAINT_SIGNALS = frozenset({
            "supply_bottleneck", "inventory_drawdown",
        })
        existing_seed_slugs: set[str] = set()  # no seed list — all auto-discovered
        auto_themes: list[InvestmentTheme] = []

        # ── STATISTICAL NOISE FILTER ─────────────────────────────────────────
        # Uses data distribution to determine noise — no hardcoded thresholds.
        all_companies_in_dataset: set[str] = set()
        all_sectors_in_dataset: set[str] = set()
        for row in cluster_rows:
            for c in (row.get("companies") or []):
                if c:
                    all_companies_in_dataset.add(c)
            entity_type = row.get("entity_type", "")
            if entity_type == "SECTOR":
                ename = (row.get("canonical_name") or "").strip()
                if ename:
                    all_sectors_in_dataset.add(ename)
        total_companies_in_dataset = max(len(all_companies_in_dataset), 1)
        total_sectors_in_dataset = max(len(all_sectors_in_dataset), 1)

        noise_filter = SmartNoiseFilter(
            total_companies=total_companies_in_dataset,
            total_sectors=total_sectors_in_dataset,
        )

        for row in cluster_rows:
            entity_name_raw = (row.get("canonical_name") or "").strip()
            if _is_noise_entity(entity_name_raw):
                continue

            # Canonicalize entity name (chip→semiconductor, ML→AI, etc.)
            entity_name = _normalize_entity_name(entity_name_raw)
            # Re-check after normalization in case the alias maps to a noise term
            if _is_noise_entity(entity_name):
                continue

            # signal_type_counts comes as a dict from json_object_agg
            raw_counts = row.get("signal_type_counts") or {}
            signal_counts = Counter({k: int(v) for k, v in raw_counts.items()})
            companies = list(row.get("companies") or [])
            doc_ids = list(row.get("document_ids") or [])
            capex_count = int(row.get("capex_count") or 0)
            constraint_kw_count = int(row.get("constraint_keyword_count") or 0)

            # Earliest filing date from actual documents that generated signals —
            # used as first_detected so the UI shows the real signal origin date,
            # not the pipeline execution date.
            first_signal_date = row.get("first_signal_date")
            if isinstance(first_signal_date, str):
                try:
                    from datetime import date as _date
                    first_signal_date = _date.fromisoformat(first_signal_date)
                except Exception:
                    first_signal_date = None

            n_companies = len(companies)
            n_docs = len(doc_ids)
            total_signals = int(row.get("total_signals") or sum(signal_counts.values()))
            quarter_count = int(row.get("quarter_count") or 1)

            # ── Statistical noise check (replaces hardcoded ubiquity + blacklist) ──
            if noise_filter.is_noise(entity_name, n_companies, n_sectors=0,
                                     signal_counts=signal_counts,
                                     total_signals=total_signals):
                logger.debug(f"Noise-filtered entity '{entity_name}' "
                             f"({n_companies}/{total_companies_in_dataset} companies)")
                continue

            # Sector entities contribute to sector set
            entity_type = row.get("entity_type", "")
            sectors: set[str] = {entity_name} if entity_type == "SECTOR" else set()

            demand_count = sum(v for k, v in signal_counts.items() if k in DEMAND_SIGNALS)
            supply_constraint_count = sum(v for k, v in signal_counts.items() if k in SUPPLY_CONSTRAINT_SIGNALS)

            # ── Gate 1: Demand-supply TENSION (highest quality signal) ────────
            has_tension = demand_count >= 2 and supply_constraint_count >= 2

            # ── Gate 2: Strong capex commitment (companies spending big) ──────
            min_capex_cos = max(3, noise_filter.adaptive_min_companies)
            has_capex_conviction = (
                capex_count >= min_capex_cos
                and n_companies >= min_capex_cos
            )

            # ── Gate 3: Demand surge without supply yet (early formation) ─────
            # Companies are reporting very high demand but supply constraints
            # haven't been widely discussed yet. This is Stage 0-1: demand is
            # running ahead of what the market can supply — the investable edge
            # is detecting this BEFORE supply bottlenecks become visible.
            min_demand_cos = max(4, noise_filter.adaptive_min_companies)
            has_demand_surge_early = (
                demand_count >= 4                    # strong demand signal count
                and supply_constraint_count < 2      # supply not yet constrained
                and n_companies >= min_demand_cos    # multiple companies reporting it
                and capex_count >= 2                 # and capex is being committed
            )

            if not has_tension and not has_capex_conviction and not has_demand_surge_early:
                continue

            # Adaptive minimum evidence
            if not noise_filter.passes_minimum_evidence(n_companies, n_docs):
                continue

            # ── Score ─────────────────────────────────────────────────────────
            if has_tension:
                tension_score = min(
                    2.0 * demand_count * supply_constraint_count
                    / (demand_count + supply_constraint_count) * 12.0,
                    60.0,
                )
            elif has_demand_surge_early:
                # Score based purely on demand intensity — no supply yet
                tension_score = min(demand_count * 4.0, 30.0)
            else:
                tension_score = 0.0

            capex_bonus = min(capex_count * 8.0, 30.0)
            breadth_bonus = min(n_companies * 2.0, 20.0)

            # ── Constraint keyword boost ──────────────────────────────────────────
            # Signals whose context text contains shortage/backlog/lead-time phrases
            # carry far higher investment signal than generic demand mentions.
            # Each constraint-keyword hit adds 6 pts (max 25).
            constraint_bonus = min(constraint_kw_count * 6.0, 25.0)

            # ── Capex lag score ───────────────────────────────────────────────────
            # lag = demand_signals - capex_signals.
            # Positive lag: demand is running ahead of capacity commitment →
            # explosive opportunity window before supply catches up.
            capex_lag = demand_count - capex_count   # demand_count already computed above
            capex_lag_bonus = min(max(capex_lag * 3.0, 0.0), 15.0)

            # ── Quarter-span bonus: actual distinct fiscal quarters, not doc count ──
            # 1 quarter  →  0 pts  (single reporting period — no persistence evidence)
            # 2 quarters →  8 pts  (minimum persistence: two distinct quarters)
            # 3 quarters → 14 pts  (clear multi-quarter trend)
            # 4+ quarters → 20 pts  (sustained, high conviction)
            quarter_bonus = (
                20.0 if quarter_count >= 4
                else 14.0 if quarter_count >= 3
                else 8.0 if quarter_count >= 2
                else 0.0
            )

            # ── Causal-chain evidence boost (+15) ─────────────────────────────────
            # If this entity appears in an ACTIVE causal chain (e.g. the entity
            # "HBM" is in the "AI→Memory→HBM" chain), it has structural evidence
            # as a bottleneck — boost score and mark in metadata.
            entity_lower = entity_name.lower()
            in_causal_chain = bool(
                causal_chain_entities
                and any(kw in entity_lower or entity_lower in kw
                        for kw in causal_chain_entities)
            )
            causal_boost = 15.0 if in_causal_chain else 0.0

            strength = min(
                tension_score + capex_bonus + breadth_bonus
                + quarter_bonus + causal_boost
                + constraint_bonus + capex_lag_bonus,
                100.0,
            )

            # ── Dominant signal and theme character ───────────────────────────
            for priority in ("supply_bottleneck", "demand_surge", "capex_increase"):
                if signal_counts.get(priority, 0) > 0:
                    dominant_signal = priority
                    break
            else:
                dominant_signal = signal_counts.most_common(1)[0][0] if signal_counts else "demand_surge"

            # ── Conviction: gated by temporal persistence ─────────────────────
            # CONFIRMED requires ≥2 distinct quarters AND strong tension (≥40) AND
            # ≥4 companies — one quarter alone cannot confirm a multi-sector theme.
            # DEVELOPING requires either ≥2 quarters OR very strong capex conviction
            # from a single quarter (temporary allowance until persistence builds).
            raw_conviction = (
                ThemeConviction.CONFIRMED if tension_score >= 40 and n_companies >= 4
                else ThemeConviction.DEVELOPING if tension_score >= 20 or has_capex_conviction
                else ThemeConviction.EMERGING
            )
            if quarter_count < 2:
                # Single-quarter themes are capped at EMERGING regardless of signal strength
                conviction = ThemeConviction.EMERGING
            elif quarter_count < 3 and raw_conviction == ThemeConviction.CONFIRMED:
                # Two-quarter themes can reach DEVELOPING but not CONFIRMED
                conviction = ThemeConviction.DEVELOPING
            else:
                conviction = raw_conviction

            # ── Descriptive theme name based on what's actually happening ─────
            theme_name = self._auto_theme_name(
                entity_name, dominant_signal, capex_count,
                has_tension=has_tension, has_demand_early=has_demand_surge_early,
            )
            entity_slug = re.sub(r"[^a-z0-9]+", "-", entity_name.lower()).strip("-")[:30]
            sig_slug = dominant_signal.replace("_", "-")[:15]
            theme_slug = f"auto-{entity_slug}-{sig_slug}"
            if theme_slug in existing_seed_slugs:
                continue

            # ── Description text ──────────────────────────────────────────────
            if has_tension:
                description = (
                    f"⚡ Supply-Demand Tension: '{entity_name}' — "
                    f"{demand_count} demand signals vs {supply_constraint_count} supply constraints "
                    f"across {n_companies} companies ({n_docs} filings). "
                    f"Capex committed: {capex_count}."
                    + (" High conviction." if tension_score >= 40 else "")
                )
            elif has_demand_surge_early:
                description = (
                    f"📈 Demand Running Ahead of Supply: '{entity_name}' — "
                    f"{demand_count} demand surge signals across {n_companies} companies. "
                    f"Supply constraints not yet visible — early formation. "
                    f"Capex: {capex_count} commits. Watch for supply bottleneck emergence."
                )
            else:
                description = (
                    f"🏗️ Capex Buildout: '{entity_name}' — "
                    f"{capex_count} capex commits across {n_companies} companies ({n_docs} filings). "
                    f"Demand: {demand_count} signals."
                )

            # First-seen: only anchor to the earliest filing date once the theme
            # has persisted across ≥2 quarters.  Single-quarter themes set
            # first_detected=None so hundreds of unconfirmed themes don't all
            # cluster to the same mass-filing date (e.g. quarterly earnings day).
            confirmed_first_detected = first_signal_date if quarter_count >= 2 else None

            auto_themes.append(InvestmentTheme(
                theme_name=theme_name,
                theme_slug=theme_slug,
                description=description,
                sectors=list(sectors),
                signal_types=list(signal_counts.keys()),
                strength_score=round(strength, 2),
                momentum_score=50.0,  # neutral placeholder; ranker computes from snapshot slope
                conviction=conviction,
                first_detected=confirmed_first_detected,  # None until multi-quarter persistence
                doc_count=n_docs,
                company_count=n_companies,
                metadata={
                    "demand_count":             demand_count,
                    "supply_constraint_count":  supply_constraint_count,
                    "tension_score":            round(tension_score, 2),
                    "capex_count":              capex_count,
                    "quarter_count":            quarter_count,
                    "quarter_bonus":            round(quarter_bonus, 2),
                    "in_causal_chain":          in_causal_chain,
                    "causal_boost":             round(causal_boost, 2),
                    "constraint_kw_count":      constraint_kw_count,
                    "constraint_bonus":         round(constraint_bonus, 2),
                    "capex_lag":                capex_lag,
                    "capex_lag_bonus":          round(capex_lag_bonus, 2),
                    # Mark entity as bottleneck theme if it maps to a known bottleneck
                    "is_bottleneck":            entity_name.lower() in _BOTTLENECK_THEMES,
                    "bottleneck_theme_name":    _BOTTLENECK_THEMES.get(entity_name.lower(), ""),
                    "entity_name_normalized":   entity_name,
                },
            ))

        auto_themes.sort(key=lambda t: -(t.metadata.get("tension_score", 0) + t.strength_score))
        auto_themes = auto_themes[:25]
        n_causal = sum(1 for t in auto_themes if t.metadata.get("in_causal_chain"))
        n_multi_quarter = sum(1 for t in auto_themes if t.metadata.get("quarter_count", 1) >= 2)
        logger.info(
            f"Cluster-agg detection: {len(auto_themes)} themes "
            f"from {len(cluster_rows)} aggregated entity rows "
            f"(min_companies={noise_filter.adaptive_min_companies}, "
            f"multi-quarter={n_multi_quarter}/{len(auto_themes)}, "
            f"causal-chain-boosted={n_causal}/{len(auto_themes)})"
        )
        return auto_themes

    # =================================================================
    # CAUSAL PLAUSIBILITY LAYER
    # Validates downstream themes using industry adjacency + edge-weighted
    # path scoring. Prevents economically invalid chains like
    # "Healthcare: Constraint from Data Center Demand".
    # =================================================================

    # ── Edge type weights ─────────────────────────────────────────────────────
    # Reflects how strong the economic transmission mechanism is.
    # Input Dependency and Supply Constraint are direct economic relationships.
    # Co-mention is just textual proximity — very weak.
    _EDGE_WEIGHTS: dict[str, float] = {
        "input_dependency":  1.00,  # A requires B as a physical input (strongest)
        "supply_constraint": 1.00,  # B is constrained and A depends on it
        "capex":             0.90,  # A is committing capital to acquire/build B
        "pricing_power":     0.80,  # A's price is driven by B's scarcity
        "regulation":        0.70,  # Regulatory change in B directly affects A
        "geopolitical":      0.60,  # Geopolitical event in B disrupts A
        "semantic":          0.40,  # Topics are semantically related (NLP-derived)
        "co_mentioned":      0.20,  # A and B appear in the same document (weakest)
    }

    # ── Industry Adjacency Map ────────────────────────────────────────────────
    # Maps primary driver keywords → frozenset of ALLOWED downstream sectors.
    # If a downstream entity doesn't match ANY allowed adjacency, it is blocked.
    # Keys are lowercase substrings — matched with `in` against entity names.
    _INDUSTRY_ADJACENCY: dict[str, frozenset] = {
        # AI / ML mega-trend
        "artificial intelligence": frozenset({
            "semiconductor", "chip", "gpu", "hbm", "memory", "nand", "dram",
            "data center", "datacenter", "cooling", "power", "electricity", "grid",
            "networking", "bandwidth", "fiber", "switch", "interconnect",
            "cloud", "storage", "server", "rack", "liquid cooling",
            "copper", "transformer", "substation", "ups",
        }),
        "machine learning": frozenset({
            "semiconductor", "chip", "gpu", "memory", "data center", "cloud",
            "networking", "storage",
        }),
        # Data center buildout
        "data center": frozenset({
            "power", "electricity", "grid", "utility", "utilities",
            "cooling", "hvac", "liquid cooling", "chiller",
            "networking", "fiber", "switch", "cable",
            "semiconductor", "chip", "server", "rack",
            "real estate", "construction", "concrete", "steel",
            "copper", "transformer", "generator", "ups", "battery",
            "natural gas", "diesel", "fuel cell",
        }),
        # Semiconductor / chips
        "semiconductor": frozenset({
            "silicon", "wafer", "advanced packaging", "cowos", "hbm",
            "chemical", "photoresist", "etchant", "gas", "argon", "neon",
            "equipment", "lithography", "etch", "deposition",
            "rare earth", "gallium", "germanium", "indium",
            "packaging", "substrate", "pcb",
        }),
        # Electric vehicles
        "electric vehicle": frozenset({
            "battery", "lithium", "cobalt", "nickel", "manganese", "graphite",
            "cathode", "anode", "electrolyte", "separator",
            "charging", "charger", "charging infrastructure",
            "power", "grid", "copper", "aluminum",
            "motor", "inverter", "semiconductor", "chip",
        }),
        "ev": frozenset({  # shorthand alias
            "battery", "lithium", "cobalt", "nickel", "copper",
            "charging", "semiconductor", "power",
        }),
        # Cloud computing
        "cloud": frozenset({
            "data center", "server", "storage", "networking", "semiconductor",
            "gpu", "memory", "fiber", "bandwidth",
        }),
        # Power / energy infrastructure
        "power demand": frozenset({
            "transformer", "copper", "aluminum", "grid", "substation",
            "natural gas", "nuclear", "coal", "renewable", "solar", "wind",
            "battery storage", "fuel cell",
        }),
        "electricity": frozenset({
            "transformer", "copper", "grid", "substation", "utility",
            "natural gas", "nuclear", "solar", "wind", "battery",
        }),
        # Reshoring / supply chain
        "reshoring": frozenset({
            "semiconductor", "chip", "manufacturing", "automation",
            "robot", "construction", "real estate", "labor",
        }),
        "supply chain": frozenset({
            "logistics", "shipping", "port", "container", "rail", "truck",
            "warehouse", "automation", "robot",
        }),
        # Defense / aerospace
        "defense": frozenset({
            "semiconductor", "chip", "titanium", "aluminum", "composite",
            "fuel", "propellant", "rare earth", "lithium",
        }),
    }

    # ── Explicit blocklist: (primary_keyword, downstream_keyword) pairs ───────
    # These combinations are NEVER economically causal regardless of co-occurrence.
    _ADJACENCY_BLOCKLIST: frozenset[tuple] = frozenset({
        ("data center",         "healthcare"),
        ("data center",         "pharmaceutical"),
        ("data center",         "biotech"),
        ("data center",         "insurance"),
        ("data center",         "banking"),
        ("artificial intelligence", "healthcare"),   # unless specifically medical AI
        ("artificial intelligence", "pharmaceutical"),
        ("artificial intelligence", "insurance"),
        ("semiconductor",       "healthcare"),
        ("semiconductor",       "pharmaceutical"),
        ("electric vehicle",    "healthcare"),
        ("electric vehicle",    "pharmaceutical"),
        ("cloud",               "healthcare"),
        ("cloud",               "pharmaceutical"),
        # Prevent generic finance/legal entities from being classified as downstream
        ("data center",         "regulation"),
        ("data center",         "compliance"),
        ("artificial intelligence", "securities"),
        ("artificial intelligence", "legal"),
    })

    # Min path score to create a downstream theme (product of edge weights along path)
    _MIN_PATH_SCORE: float = 0.70
    # Max hops allowed in the inferred causal chain
    _MAX_HOPS: int = 3
    # Min distinct companies that must appear in BOTH primary and downstream docs
    _MIN_COMPANY_OVERLAP: int = 3
    # Min economic adjacency score [0, 1] — how tightly related the industries are
    _MIN_ECONOMIC_SCORE: float = 0.70

    @staticmethod
    def _classify_edge_type(
        primary_name: str,
        ds_name: str,
        supply_sigs: int,
        capex_sigs: int,
        cooccur_pct: float,
    ) -> str:
        """Classify the dominant economic relationship type between primary and downstream.

        Used to assign the correct edge weight for path scoring.
        """
        p = primary_name.lower()
        d = ds_name.lower()

        # Direct physical input dependency keywords
        _INPUT_DEPS = {
            ("semiconductor", "silicon"), ("semiconductor", "wafer"),
            ("semiconductor", "chemical"), ("semiconductor", "gas"),
            ("semiconductor", "photoresist"), ("semiconductor", "rare earth"),
            ("data center", "cooling"), ("data center", "power"),
            ("data center", "copper"), ("data center", "transformer"),
            ("electric vehicle", "battery"), ("electric vehicle", "lithium"),
            ("electric vehicle", "cobalt"), ("electric vehicle", "copper"),
            ("power demand", "copper"), ("power demand", "transformer"),
        }
        for pk, dk in _INPUT_DEPS:
            if pk in p and dk in d:
                return "input_dependency"

        if supply_sigs >= 3:
            return "supply_constraint"
        if capex_sigs >= 3:
            return "capex"
        if cooccur_pct >= 50:
            return "semantic"
        return "co_mentioned"

    def _compute_path_score(
        self,
        primary_name: str,
        ds_name: str,
        supply_sigs: int,
        capex_sigs: int,
        cooccur_pct: float,
        n_hops: int = 1,
    ) -> float:
        """Compute causal path plausibility score.

        Score = product of edge weights along the inferred path.
        For a direct (1-hop) path: score = weight of that single edge.
        For multi-hop: each additional hop multiplies by the estimated
        intermediate edge weight (assumed 0.8 for known causal chains).

        Returns 0.0 if path is economically implausible (wrong adjacency).
        """
        edge_type = self._classify_edge_type(
            primary_name, ds_name, supply_sigs, capex_sigs, cooccur_pct
        )
        base_weight = self._EDGE_WEIGHTS.get(edge_type, 0.20)

        # Long-path penalty: score = edge_weight_product / hops²
        # 1-hop: score = weight           (no penalty)
        # 2-hop: score = weight / 4       (strong penalty — path is less certain)
        # 3-hop: score = weight / 9       (very strong — only tight chains survive)
        # This forces multi-hop themes to have very high-weight upstream edges
        # (input_dependency=1.0 or supply_constraint=1.0) to clear _MIN_PATH_SCORE.
        hops_sq = max(1, n_hops ** 2)
        return round(base_weight / hops_sq, 3)

    def _check_adjacency(self, primary_name: str, ds_name: str) -> tuple[bool, float]:
        """Check industry adjacency between primary driver and downstream entity.

        Returns (is_allowed, adjacency_score) where adjacency_score ∈ [0, 1].
          1.0 = direct adjacency (known input dependency)
          0.5 = indirect / weak adjacency
          0.0 = blocked (economically implausible)
        """
        p_lower = primary_name.lower()
        d_lower = ds_name.lower()

        # Check explicit blocklist first
        for pk, dk in self._ADJACENCY_BLOCKLIST:
            if pk in p_lower and dk in d_lower:
                return False, 0.0

        # Check adjacency map
        for primary_kw, allowed_set in self._INDUSTRY_ADJACENCY.items():
            if primary_kw not in p_lower:
                continue
            # Direct hit: downstream entity matches an allowed downstream
            if any(allowed_kw in d_lower for allowed_kw in allowed_set):
                # Score based on how specific the match is
                score = 1.0 if any(
                    allowed_kw in d_lower and len(allowed_kw) >= 5
                    for allowed_kw in allowed_set
                ) else 0.75
                return True, score

        # No adjacency rule found — apply conservative default
        # If co-occurrence is very high (70%+) it might still be valid
        # but we score it low and let the path score gate filter it
        return True, 0.45  # allowed but weak — path score will likely kill it

    # =================================================================
    # BOTTLENECK THEME DETECTION
    # Detects specific supply bottlenecks by searching for constraint
    # keywords in signal context text — shortages, backlogs, lead times.
    # These are the highest-conviction explosive themes.
    # =================================================================
    def detect_bottleneck_themes(
        self,
        pg_store,
        as_of_date=None,
        lookback_days: int = 365,
        min_constraint_signals: int = 3,
        min_companies: int = 3,
        country: str = None,
    ) -> list[InvestmentTheme]:
        """Detect bottleneck themes by scanning for constraint language in signal context.

        Unlike detect_from_clusters_agg (which relies on signal_type labels),
        this detector searches for THE ACTUAL WORDS management uses when describing
        supply constraints: shortage, backlog, lead time, sold out, fully booked.

        These are the explosive investment signals — when multiple companies report
        the same bottleneck in the same constraint language, pricing power follows.

        Returns InvestmentTheme objects with theme_type='bottleneck' and
        metadata capturing the specific constraint phrases found.
        """
        from datetime import date as _date, timedelta as _td
        if as_of_date is None:
            as_of_date = _date.today()
        if hasattr(as_of_date, "date"):
            as_of_date = as_of_date.date()
        floor = as_of_date - _td(days=lookback_days)

        themes: list[InvestmentTheme] = []
        existing_seed_slugs: set[str] = set()  # no seed list — all auto-discovered

        try:
            from psycopg2.extras import RealDictCursor
            with pg_store._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Query: find entities whose signal context_text contains
                    # constraint keywords across multiple companies.
                    # Weight each keyword by its severity (shortage > lead time).
                    cur.execute("""
                        WITH constraint_signals AS (
                            SELECT
                                e.canonical_name,
                                e.entity_type,
                                COALESCE(NULLIF(d.company,''), d.ticker) AS company,
                                s.document_id,
                                s.context_text,
                                d.filed_at,
                                -- Keyword-weighted score: shortage=1.5, backlog=1.3, etc.
                                CASE
                                    WHEN s.context_text ILIKE ANY(ARRAY[
                                        '%%sold out%%','%%fully booked%%','%%cannot meet demand%%',
                                        '%%shortage%%','%%shortages%%','%%rationing%%','%%demand exceeds%%'
                                    ]) THEN 1.50
                                    WHEN s.context_text ILIKE ANY(ARRAY[
                                        '%%allocation%%','%%lead time%%','%%extended lead%%'
                                    ]) THEN 1.35
                                    WHEN s.context_text ILIKE ANY(ARRAY[
                                        '%%backlog%%','%%bottleneck%%','%%constrained%%',
                                        '%%capacity tight%%','%%supply tight%%','%%supply limited%%'
                                    ]) THEN 1.25
                                    WHEN s.context_text ILIKE ANY(ARRAY[
                                        '%%order push%%','%%push-out%%','%%wait list%%'
                                    ]) THEN 1.15
                                    ELSE 0
                                END AS kw_weight
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE d.filed_at BETWEEN %s AND %s
                              AND s.context_text IS NOT NULL
                              AND length(s.context_text) > 20
                              AND e.entity_type IN ('TECHNOLOGY','PRODUCT','CONCEPT','SECTOR')
                              AND length(e.canonical_name) >= 3
                              AND e.canonical_name ~ '^[A-Za-z]'
                              AND (%s IS NULL OR d.country = %s)
                        ),
                        filtered AS (
                            SELECT * FROM constraint_signals WHERE kw_weight > 0
                        ),
                        agg AS (
                            SELECT
                                canonical_name,
                                entity_type,
                                COUNT(DISTINCT company)    AS n_companies,
                                COUNT(DISTINCT document_id) AS n_docs,
                                ROUND(SUM(kw_weight)::numeric, 2) AS weighted_constraint_score,
                                COUNT(*) AS raw_signal_count,
                                MIN(filed_at)::date AS first_date,
                                COUNT(DISTINCT date_trunc('quarter', filed_at))::int AS quarter_count,
                                -- Most severe constraint phrase found
                                MAX(kw_weight) AS max_kw_weight
                            FROM filtered
                            GROUP BY canonical_name, entity_type
                            HAVING COUNT(DISTINCT company) >= %s
                               AND COUNT(*) >= %s
                        )
                        SELECT * FROM agg
                        ORDER BY weighted_constraint_score DESC, n_companies DESC
                        LIMIT 20
                    """, (floor, as_of_date, country, country, min_companies, min_constraint_signals))

                    rows = cur.fetchall()

        except Exception as e:
            logger.warning(f"detect_bottleneck_themes query failed: {e}")
            return themes

        for row in rows:
            entity_name_raw = (row.get("canonical_name") or "").strip()
            if _is_noise_entity(entity_name_raw):
                continue

            # Canonicalize aliases
            entity_name = _normalize_entity_name(entity_name_raw)
            if _is_noise_entity(entity_name):
                continue

            n_companies    = int(row.get("n_companies") or 0)
            n_docs         = int(row.get("n_docs") or 0)
            quarter_count  = int(row.get("quarter_count") or 1)
            wt_score       = float(row.get("weighted_constraint_score") or 0)
            max_kw         = float(row.get("max_kw_weight") or 0)
            first_date     = row.get("first_date")

            if isinstance(first_date, str):
                try:
                    from datetime import date as _d2
                    first_date = _d2.fromisoformat(first_date)
                except Exception:
                    first_date = None

            # Look up canonical bottleneck theme name
            bottleneck_name = _BOTTLENECK_THEMES.get(entity_name.lower())
            if bottleneck_name:
                theme_name = bottleneck_name
            else:
                # Severity derived from COMPOSITE evidence, not a single extreme keyword.
                # Using only max_kw_weight caused the template effect: any entity with
                # one "sold out" mention received "Critical Shortage" regardless of how
                # many companies or quarters corroborated it.
                # Composite severity index: avg constraint weight × breadth × persistence.
                avg_kw = wt_score / max(n_docs, 1)
                severity_index = avg_kw * min(n_companies / 3.0, 2.0) * min(quarter_count / 2.0, 1.5)
                severity = (
                    "Critical Shortage"  if severity_index >= 1.20 and quarter_count >= 2
                    else "Severe Constraint" if severity_index >= 0.70
                    else "Supply Bottleneck"
                )
                theme_name = f"{entity_name.title()} {severity}"

            entity_slug  = re.sub(r"[^a-z0-9]+", "-", entity_name.lower()).strip("-")[:30]
            theme_slug   = f"bottleneck-{entity_slug}"
            if theme_slug in existing_seed_slugs:
                continue

            # Strength = weighted constraint score × company breadth × quarter persistence
            strength = min(
                wt_score * 5.0
                + n_companies * 3.0
                + (quarter_count - 1) * 8.0,
                100.0,
            )

            conviction = (
                ThemeConviction.CONFIRMED  if quarter_count >= 3 and n_companies >= 5
                else ThemeConviction.DEVELOPING if quarter_count >= 2 or n_companies >= 4
                else ThemeConviction.EMERGING
            )

            description = (
                f"🚨 BOTTLENECK ALERT: '{entity_name.title()}' — "
                f"constraint language detected in {n_docs} filings "
                f"across {n_companies} companies over {quarter_count} quarter(s). "
                f"Weighted constraint score: {wt_score:.1f}. "
                f"This entity is supply-constrained with explicit management language "
                f"(shortage/backlog/lead-time). Pricing power follows."
            )

            # Bottleneck first_detected: require ≥2 quarters to avoid mass-clustering
            # to the same day when a large earnings batch is processed.
            bottleneck_first_detected = first_date if quarter_count >= 2 else None

            themes.append(InvestmentTheme(
                theme_name=theme_name,
                theme_slug=theme_slug,
                description=description,
                sectors=[],
                signal_types=["supply_bottleneck"],
                strength_score=round(strength, 2),
                momentum_score=50.0,  # neutral placeholder; ranker computes from snapshot slope
                conviction=conviction,
                first_detected=bottleneck_first_detected,
                doc_count=n_docs,
                company_count=n_companies,
                metadata={
                    "theme_type":                "bottleneck",
                    "entity_name_normalized":    entity_name,
                    "weighted_constraint_score": wt_score,
                    "constraint_kw_count":       int(row.get("raw_signal_count") or 0),
                    "max_kw_weight":             max_kw,
                    "quarter_count":             quarter_count,
                    "is_bottleneck":             True,
                    "bottleneck_theme_name":     bottleneck_name or theme_name,
                    "tension_score":             round(min(wt_score * 4.0, 60.0), 2),
                    "supply_constraint_count":   int(row.get("raw_signal_count") or 0),
                },
            ))

        logger.info(
            f"Bottleneck detection: {len(themes)} bottleneck themes "
            f"from constraint-keyword scan (min_companies={min_companies}, "
            f"min_signals={min_constraint_signals})"
        )
        return themes

    # =================================================================
    # DOWNSTREAM CONSTRAINT DETECTION
    # Finds the "Memory because of AI" / "Power because of Data Centers"
    # picks-and-shovels themes. These are typically the highest-return
    # opportunities — supply-constrained components serving a mega-trend.
    # =================================================================
    def detect_downstream_constraint_themes(
        self,
        pg_store,
        as_of_date=None,
        lookback_days: int = 365,
        country: str = None,
    ) -> list[InvestmentTheme]:
        """Detect second-order/downstream constraint themes via document co-occurrence.

        Algorithm:
          1. Identify PRIMARY DRIVERS  — high-momentum technology/sector entities
             with 8+ companies discussing them.
             (e.g. "Artificial Intelligence", "Data Center", "Electric Vehicle", "Cloud")

          2. For each primary, find DOWNSTREAM ENTITIES that:
             (a) appear in 30%+ of the primary's documents (co-occurrence)
             (b) have their own supply_bottleneck signals (2+ companies)
             (c) are NOT the primary itself
             (d) are not generic noise (filtered via _is_noise_entity)

          3. Score by concentration: fewer suppliers + more constraints
             = higher pricing power = more explosive return potential.

          4. Tag with metadata.theme_type="downstream_constraint" and
             metadata.driven_by="<primary entity>" so the UI can highlight them.

        Returns InvestmentTheme objects ready to be merged into the main theme list.
        """
        from datetime import date as _date, timedelta as _td

        if as_of_date is None:
            as_of_date = _date.today()
        if hasattr(as_of_date, "date"):
            as_of_date = as_of_date.date()
        floor = as_of_date - _td(days=lookback_days)

        themes: list[InvestmentTheme] = []
        existing_seed_slugs: set[str] = set()  # no seed list — all auto-discovered

        try:
            with pg_store._conn() as conn:
                from psycopg2.extras import RealDictCursor
                with conn.cursor(cursor_factory=RealDictCursor) as cur:

                    # ── Step 1: Find PRIMARY DRIVERS (high-momentum entities) ───
                    # An entity is a primary driver if it appears in many documents
                    # with strong demand_surge or technology_adoption signals across
                    # 8+ distinct companies.
                    cur.execute("""
                        SELECT e.id          AS entity_id,
                               e.canonical_name,
                               e.entity_type,
                               COUNT(DISTINCT d.company)   AS n_companies,
                               COUNT(DISTINCT d.id)        AS n_docs,
                               COUNT(*) FILTER (WHERE s.signal_type IN ('demand_surge','technology_adoption','capex_increase'))
                                                           AS momentum_signals
                        FROM mg_entities e
                        JOIN mg_document_entities de ON de.entity_id = e.id
                        JOIN mg_documents d ON d.id = de.document_id
                        LEFT JOIN mg_signals s ON s.document_id = d.id
                        WHERE d.filed_at BETWEEN %s AND %s
                          AND (%s IS NULL OR d.country = %s)
                          AND e.entity_type IN ('TECHNOLOGY','CONCEPT','SECTOR','PRODUCT')
                          AND length(e.canonical_name) >= 3
                          AND e.canonical_name !~* '^(item|exhibit|section|the |annual|quarterly)'
                        GROUP BY e.id, e.canonical_name, e.entity_type
                        HAVING COUNT(DISTINCT d.company) >= 4
                           AND COUNT(*) FILTER (WHERE s.signal_type IN ('demand_surge','technology_adoption','capex_increase')) >= 3
                        ORDER BY momentum_signals DESC, n_companies DESC
                        LIMIT 15
                    """, (floor, as_of_date, country, country))
                    primary_drivers = cur.fetchall()

                    if not primary_drivers:
                        logger.info("No primary drivers found — skipping downstream-constraint detection")
                        return themes

                    logger.info(
                        f"Downstream-constraint detection: found {len(primary_drivers)} primary drivers: "
                        + ", ".join(p["canonical_name"] for p in primary_drivers[:5])
                    )

                    # ── Step 2: For each primary, find DOWNSTREAM constrained entities ──
                    # An entity is downstream if it appears in >= 30% of primary's documents
                    # AND has its own supply_bottleneck signals across 2+ companies.
                    for primary in primary_drivers:
                        primary_id   = primary["entity_id"]
                        primary_name = primary["canonical_name"]
                        primary_docs = primary["n_docs"]

                        if primary_docs < 5:
                            continue

                        min_cooccur_pct = 30.0  # downstream must appear in ≥30% of primary's docs

                        cur.execute("""
                            WITH primary_docs AS (
                                SELECT DISTINCT d.id AS doc_id
                                FROM mg_documents d
                                JOIN mg_document_entities de ON de.document_id = d.id
                                WHERE de.entity_id = %s
                                  AND d.filed_at BETWEEN %s AND %s
                                  AND (%s IS NULL OR d.country = %s)
                            ),
                            cooccurring AS (
                                SELECT e.id   AS entity_id,
                                       e.canonical_name,
                                       e.entity_type,
                                       COUNT(DISTINCT de.document_id)            AS cooccur_docs,
                                       COUNT(DISTINCT d2.company)                AS cooccur_cos,
                                       MIN(d2.filed_at)::date                   AS first_doc_date
                                FROM mg_entities e
                                JOIN mg_document_entities de ON de.entity_id = e.id
                                JOIN primary_docs pd ON pd.doc_id = de.document_id
                                JOIN mg_documents d2 ON d2.id = de.document_id
                                WHERE e.id != %s
                                  AND e.entity_type IN ('TECHNOLOGY','PRODUCT','CONCEPT','SECTOR')
                                  AND length(e.canonical_name) >= 3
                                  AND e.canonical_name !~* '^(item|exhibit|section|the |annual|quarterly|today|charter|agreement|operations|treasury|diluted|compensatory|one year|two year)'
                                GROUP BY e.id, e.canonical_name, e.entity_type
                                HAVING COUNT(DISTINCT de.document_id) >= GREATEST(2, %s * 0.3)
                            ),
                            constraint_sigs AS (
                                SELECT e.id AS entity_id,
                                       COUNT(*) FILTER (WHERE s.signal_type IN ('supply_bottleneck','inventory_drawdown'))
                                                                              AS supply_sigs,
                                       COUNT(*) FILTER (WHERE s.signal_type = 'demand_surge')
                                                                              AS demand_sigs,
                                       COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase')
                                                                              AS capex_sigs,
                                       COUNT(DISTINCT s.document_id) FILTER (WHERE s.signal_type IN ('supply_bottleneck','inventory_drawdown'))
                                                                              AS supply_docs
                                FROM mg_entities e
                                JOIN mg_document_entities de ON de.entity_id = e.id
                                JOIN mg_signals s ON s.document_id = de.document_id
                                WHERE s.filed_at BETWEEN %s AND %s
                                GROUP BY e.id
                                HAVING COUNT(*) FILTER (WHERE s.signal_type IN ('supply_bottleneck','inventory_drawdown')) >= 2
                            )
                            SELECT c.entity_id,
                                   c.canonical_name,
                                   c.entity_type,
                                   c.cooccur_docs,
                                   c.cooccur_cos,
                                   c.first_doc_date,
                                   cs.supply_sigs,
                                   cs.demand_sigs,
                                   cs.capex_sigs,
                                   cs.supply_docs,
                                   (c.cooccur_docs::float / NULLIF(%s, 0)) * 100 AS cooccur_pct
                            FROM cooccurring c
                            JOIN constraint_sigs cs ON cs.entity_id = c.entity_id
                            WHERE (c.cooccur_docs::float / NULLIF(%s, 0)) >= %s / 100.0
                            ORDER BY cs.supply_sigs DESC, c.cooccur_docs DESC
                            LIMIT 8
                        """, (
                            primary_id, floor, as_of_date, country, country,  # primary_docs CTE
                            primary_id, primary_docs,                          # cooccurring CTE
                            floor, as_of_date,                                 # constraint_sigs CTE
                            primary_docs, primary_docs, min_cooccur_pct,       # final WHERE clause
                        ))
                        downstream = cur.fetchall()

                        for d in downstream:
                            ds_name = d["canonical_name"]
                            if _is_noise_entity(ds_name):
                                continue

                            cooccur_pct  = float(d.get("cooccur_pct") or 0)
                            supply_sigs  = int(d.get("supply_sigs") or 0)
                            demand_sigs  = int(d.get("demand_sigs") or 0)
                            capex_sigs   = int(d.get("capex_sigs") or 0)
                            n_cos        = int(d.get("cooccur_cos") or 0)
                            n_docs_ds    = int(d.get("cooccur_docs") or 0)

                            # ── Gate 1: Industry Adjacency ────────────────────────────
                            # Block economically implausible chains (e.g. Healthcare
                            # from Data Center Demand) before any scoring happens.
                            is_adjacent, adjacency_score = self._check_adjacency(
                                primary_name, ds_name
                            )
                            if not is_adjacent:
                                logger.debug(
                                    f"Adjacency BLOCKED: '{ds_name}' is not an economically "
                                    f"valid downstream of '{primary_name}'"
                                )
                                continue

                            # ── Gate 2: Path Score (edge-weight product) ──────────────
                            # Score reflects the strength of the economic transmission
                            # mechanism. Weak co-mention chains (score ~0.2) are killed.
                            path_score = self._compute_path_score(
                                primary_name, ds_name,
                                supply_sigs, capex_sigs, cooccur_pct,
                                n_hops=1,
                            )
                            if path_score < self._MIN_PATH_SCORE:
                                logger.debug(
                                    f"Path score too weak ({path_score:.2f} < {self._MIN_PATH_SCORE}): "
                                    f"'{primary_name}' → '{ds_name}' — skipping"
                                )
                                continue

                            # ── Gate 3: Company Overlap ───────────────────────────────
                            # At least MIN_COMPANY_OVERLAP companies must appear in
                            # BOTH primary driver docs AND downstream entity docs.
                            # This ensures the connection is real, not textual coincidence.
                            if n_cos < self._MIN_COMPANY_OVERLAP:
                                logger.debug(
                                    f"Company overlap too low ({n_cos} < {self._MIN_COMPANY_OVERLAP}): "
                                    f"'{ds_name}' via '{primary_name}'"
                                )
                                continue

                            # ── Gate 4: Combined Economic Score ───────────────────────
                            # Combine adjacency score + path score into final gate.
                            economic_score = (adjacency_score * 0.5 + path_score * 0.5)
                            if economic_score < self._MIN_ECONOMIC_SCORE:
                                logger.debug(
                                    f"Economic score too low ({economic_score:.2f}): "
                                    f"'{ds_name}' via '{primary_name}'"
                                )
                                continue

                            ds_slug = re.sub(r"[^a-z0-9]+", "-", ds_name.lower()).strip("-")[:30]
                            primary_slug = re.sub(r"[^a-z0-9]+", "-", primary_name.lower()).strip("-")[:25]
                            theme_slug = f"downstream-{ds_slug}-via-{primary_slug}"
                            if theme_slug in existing_seed_slugs:
                                continue

                            # Earliest document date where this entity co-occurs with
                            # the primary driver — this is the true first_detected date,
                            # not the pipeline execution date.
                            first_doc_date = d.get("first_doc_date")
                            if isinstance(first_doc_date, str):
                                try:
                                    from datetime import date as _date2
                                    first_doc_date = _date2.fromisoformat(first_doc_date)
                                except Exception:
                                    first_doc_date = None

                            # ── Score: concentration + tension + co-occurrence strength
                            # Edge type is now used to weight the final strength.
                            edge_type = self._classify_edge_type(
                                primary_name, ds_name, supply_sigs, capex_sigs, cooccur_pct
                            )
                            edge_weight = self._EDGE_WEIGHTS.get(edge_type, 0.20)

                            if n_cos > 0:
                                concentration = min(supply_sigs / n_cos, 5.0) * 8.0
                            else:
                                concentration = 0.0
                            # Co-occurrence boost is now scaled by edge weight —
                            # input dependencies get full boost, co-mentions get 20%.
                            cooccur_boost = min(cooccur_pct * 0.4 * edge_weight, 25.0)
                            tension_boost = min(2.0 * demand_sigs * supply_sigs /
                                                max(demand_sigs + supply_sigs, 1) * 5.0, 25.0)
                            capex_boost = min(capex_sigs * 4.0, 15.0)
                            # Economic score bonus: valid adjacency + strong path get a lift
                            economic_bonus = round(economic_score * 15.0, 1)
                            strength = min(
                                concentration + cooccur_boost + tension_boost
                                + capex_boost + economic_bonus + 10,
                                100.0,
                            )

                            # Conviction — downstream themes also require multi-company evidence
                            if supply_sigs >= 4 and demand_sigs >= 2 and cooccur_pct >= 40 and n_cos >= 5:
                                conv = ThemeConviction.CONFIRMED
                            elif supply_sigs >= 3 or (cooccur_pct >= 50 and n_cos >= 4):
                                conv = ThemeConviction.DEVELOPING
                            else:
                                conv = ThemeConviction.EMERGING

                            theme_name = f"{ds_name}: Constraint from {primary_name} Demand"
                            description = (
                                f"💎 EXPLOSIVE THEME — '{ds_name}' is supply-constrained "
                                f"({supply_sigs} bottleneck signals across {n_cos} companies) "
                                f"AND co-mentioned in {cooccur_pct:.0f}% of '{primary_name}' documents. "
                                f"Demand from {primary_name} is driving scarcity → pricing power for {ds_name} suppliers. "
                                f"Edge type: {edge_type} (weight={edge_weight:.2f}). "
                                f"Economic score: {economic_score:.2f}. Picks-and-shovels play."
                            )

                            sig_types = []
                            if supply_sigs > 0: sig_types.append("supply_bottleneck")
                            if demand_sigs > 0: sig_types.append("demand_surge")
                            if capex_sigs  > 0: sig_types.append("capex_increase")

                            themes.append(InvestmentTheme(
                                theme_name=theme_name,
                                theme_slug=theme_slug,
                                description=description,
                                sectors=[],
                                signal_types=sig_types,
                                strength_score=round(strength, 2),
                                momentum_score=50.0,  # neutral; ranker computes from snapshot slope
                                conviction=conv,
                                first_detected=first_doc_date,
                                doc_count=n_docs_ds,
                                company_count=n_cos,
                                metadata={
                                    "theme_type":              "downstream_constraint",
                                    "driven_by":               primary_name,
                                    "driven_by_companies":     primary["n_companies"],
                                    "driven_by_docs":          primary_docs,
                                    "cooccurrence_pct":        round(cooccur_pct, 1),
                                    "supply_constraint_count": supply_sigs,
                                    "demand_count":            demand_sigs,
                                    "capex_count":             capex_sigs,
                                    "tension_score": round(
                                        min(2.0 * demand_sigs * supply_sigs /
                                            max(demand_sigs + supply_sigs, 1) * 12.0, 60.0), 2
                                    ) if demand_sigs > 0 and supply_sigs > 0 else 0.0,
                                    "concentration_ratio":     round(supply_sigs / max(n_cos, 1), 2),
                                    # Causal plausibility metadata
                                    "edge_type":               edge_type,
                                    "edge_weight":             edge_weight,
                                    "path_score":              round(path_score, 3),
                                    "adjacency_score":         round(adjacency_score, 3),
                                    "economic_score":          round(economic_score, 3),
                                },
                            ))

        except Exception as e:
            logger.warning(f"detect_downstream_constraint_themes failed: {e}")
            return themes

        if themes:
            edge_types = {}
            for t in themes:
                et = t.metadata.get("edge_type", "unknown")
                edge_types[et] = edge_types.get(et, 0) + 1
        else:
            edge_types = {}

        logger.info(
            f"Downstream-constraint detection: {len(themes)} economically valid themes "
            f"(gates: adjacency≥{self._MIN_ECONOMIC_SCORE}, path≥{self._MIN_PATH_SCORE}, "
            f"companies≥{self._MIN_COMPANY_OVERLAP}). "
            f"Edge types: {edge_types}"
        )
        return themes

    @staticmethod
    def _auto_theme_name(
        entity: str,
        dominant_signal: str,
        capex_count: int,
        has_tension: bool = False,
        has_demand_early: bool = False,
    ) -> str:
        """Generate a descriptive theme name based on WHAT IS ACTUALLY HAPPENING.

        Priority:
          1. Tension (demand AND supply signals) → "X: Demand-Supply Tension"
          2. Demand surge without supply → "X: Demand Running Ahead"
          3. Capex buildout → "X: Capex Buildout"
          4. Specific signal label
        """
        if has_tension:
            # Most actionable: demand outpacing supply with constraints visible
            return f"{entity}: Demand-Supply Tension"

        if has_demand_early:
            # Second best: demand surging, supply hasn't caught up yet
            return f"{entity}: Demand Surge"

        if capex_count >= 3:
            # Capital is being committed at scale
            return f"{entity}: Capex Buildout"

        # Fallback: specific signal label
        signal_label = {
            "capex_increase":      "Capex Surge",
            "capex_decrease":      "Capex Pullback",
            "demand_surge":        "Demand Surge",
            "demand_slowdown":     "Demand Slowdown",
            "supply_bottleneck":   "Supply Constraint",
            "supply_easing":       "Supply Recovery",
            "technology_adoption": "Technology Adoption",
            "technology_disruption": "Technology Disruption",
            "regulatory_tailwind": "Regulatory Tailwind",
            "regulatory_headwind": "Regulatory Headwind",
            "partnership_formed":  "Partnership Wave",
            "acquisition_intent":  "M&A Activity",
            "strategic_pivot":     "Strategic Repositioning",
            "market_entry":        "Market Expansion",
            "hiring_surge":        "Talent Surge",
            "inventory_buildup":   "Inventory Buildup",
            "inventory_drawdown":  "Inventory Correction",
        }.get(dominant_signal, "Emerging Opportunity")

        return f"{entity}: {signal_label}"

    def detect_from_graph(self, graph_store) -> list[InvestmentTheme]:
        """Graph-based detection: find cross-sector technologies via Neo4j.

        Queries Neo4j for any technology/concept that appears across 3+ sectors.
        These are auto-discovered from the graph — no pre-defined list.
        Also picks up company-level relationships: if 4+ companies SUPPLY_TO or
        INVESTS_IN the same target technology across 2+ sectors, that's a theme.
        """
        if not self.use_graph or graph_store is None or self._graph_unavailable:
            return []

        themes: list[InvestmentTheme] = []
        existing_seed_slugs: set[str] = set()  # no seed list — all auto-discovered

        try:
            # 1. Cross-sector technology adoption (original query)
            cross_sector = graph_store.get_cross_sector_technologies(min_sectors=3)
            for item in cross_sector:
                tech = item["technology"]
                sectors = item.get("sectors", [])
                mentions = item.get("mentions", 1)
                companies = item.get("companies", [])

                slug = "graph-" + re.sub(r"[^a-z0-9]+", "-", tech.lower()).strip("-")
                if slug in existing_seed_slugs:
                    continue

                strength = min(mentions * 2.0 + len(companies) * 3.0, 100.0)
                conviction = (
                    ThemeConviction.CONFIRMED if mentions >= 20
                    else ThemeConviction.DEVELOPING if mentions >= 10
                    else ThemeConviction.EMERGING
                )

                themes.append(InvestmentTheme(
                    theme_name=f"{tech} Cross-Sector Adoption",
                    theme_slug=slug,
                    description=(
                        f"'{tech}' is adopted across {len(sectors)} sector(s): "
                        f"{', '.join(sectors[:4])}. "
                        f"Detected in {len(companies)} company filings."
                    ),
                    sectors=sectors,
                    signal_types=["technology_adoption"],
                    strength_score=round(strength, 2),
                    momentum_score=50.0,  # neutral; ranker computes from snapshot slope
                    conviction=conviction,
                    doc_count=mentions,
                    company_count=len(companies),
                ))

            # 2. Supply-chain convergence: multiple companies supplying same target
            try:
                supply_clusters = graph_store.get_supply_chain_clusters(min_suppliers=3)
                for item in supply_clusters:
                    target = item.get("target", "")
                    suppliers = item.get("suppliers", [])
                    sectors = item.get("sectors", [])
                    if not target or len(suppliers) < 3:
                        continue

                    slug = "supply-" + re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")
                    if slug in existing_seed_slugs:
                        continue

                    strength = min(len(suppliers) * 8.0 + len(sectors) * 10.0, 100.0)
                    themes.append(InvestmentTheme(
                        theme_name=f"{target} Supply Chain Concentration",
                        theme_slug=slug,
                        description=(
                            f"{len(suppliers)} suppliers converging on '{target}' "
                            f"across {len(sectors)} sectors — potential bottleneck."
                        ),
                        sectors=sectors,
                        signal_types=["supply_bottleneck"],
                        strength_score=round(strength, 2),
                        momentum_score=50.0,  # neutral; ranker computes from snapshot slope
                        conviction=ThemeConviction.DEVELOPING if len(suppliers) >= 5 else ThemeConviction.EMERGING,
                        doc_count=len(suppliers),
                        company_count=len(suppliers),
                    ))
            except Exception:
                pass  # graph_store may not implement get_supply_chain_clusters yet

            # 3. Capex concentration: multiple companies investing in the same target
            try:
                capex_clusters = graph_store.get_capex_concentrated_technologies(min_companies=3)
                for item in capex_clusters:
                    target = item.get("target", "")
                    companies = item.get("companies", [])
                    total_inv = item.get("total_investment", 0) or 0
                    if not target or len(companies) < 3:
                        continue

                    slug = "capex-" + re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")
                    if slug in existing_seed_slugs:
                        continue

                    # Investment amount drives strength
                    inv_score = min(total_inv / 1e9 * 10.0, 30.0)  # per $1B
                    strength = min(len(companies) * 8.0 + inv_score, 100.0)

                    themes.append(InvestmentTheme(
                        theme_name=f"{target} Capex Buildout",
                        theme_slug=slug,
                        description=(
                            f"{len(companies)} companies committed capex toward '{target}'. "
                            + (f"Total: ${total_inv/1e9:.1f}B." if total_inv > 0 else "")
                        ),
                        sectors=[],
                        signal_types=["capex_increase"],
                        strength_score=round(strength, 2),
                        momentum_score=50.0,  # neutral; ranker computes from snapshot slope
                        conviction=ThemeConviction.DEVELOPING if len(companies) >= 5 else ThemeConviction.EMERGING,
                        doc_count=len(companies),
                        company_count=len(companies),
                    ))
            except Exception:
                pass

            logger.info(f"Graph-based detection: {len(themes)} themes from cross-sector signals")
        except Exception as e:
            self._graph_unavailable = True
            logger.warning(
                f"Neo4j unavailable — graph theme detection disabled for this run. "
                f"Start Neo4j to enable it. ({type(e).__name__})"
            )

        return themes

    def detect_from_topics(
        self, topic_results: list, evolution_data: dict = None
    ) -> list[InvestmentTheme]:
        """Convert emerging BERTopic clusters into investment themes."""
        themes = []
        for topic in topic_results:
            if not getattr(topic, "is_emerging", False):
                continue
            if topic.doc_count < self.min_doc_count:
                continue

            top_words = topic.top_words[:5] if topic.top_words else []
            slug = "topic-" + str(topic.topic_id) + "-" + re.sub(
                r"[^a-z0-9]+", "-", " ".join(top_words[:2]).lower()
            ).strip("-")

            strength = min(topic.doc_count * 3.0, 100.0)
            themes.append(InvestmentTheme(
                theme_name=f"Emerging: {topic.label}",
                theme_slug=slug,
                description=f"Emerging topic cluster (BERTopic #{topic.topic_id}). Key terms: {', '.join(top_words)}",
                signal_types=["topic_emergence"],
                strength_score=strength,
                momentum_score=strength,
                conviction=ThemeConviction.EMERGING,
                doc_count=topic.doc_count,
            ))

        logger.info(f"Topic-based detection: {len(themes)} emerging themes")
        return themes

    def merge_themes(self, theme_lists: list[list[InvestmentTheme]]) -> list[InvestmentTheme]:
        """Merge and deduplicate themes from all detection strategies."""
        seen_slugs: dict[str, InvestmentTheme] = {}

        for theme_list in theme_lists:
            for theme in theme_list:
                if theme.theme_slug in seen_slugs:
                    # Merge: take max scores
                    existing = seen_slugs[theme.theme_slug]
                    existing.strength_score = max(existing.strength_score, theme.strength_score)
                    existing.momentum_score = max(existing.momentum_score, theme.momentum_score)
                    existing.doc_count = max(existing.doc_count, theme.doc_count)
                    existing.company_count = max(existing.company_count, theme.company_count)
                    if not existing.sectors and theme.sectors:
                        existing.sectors = theme.sectors
                else:
                    seen_slugs[theme.theme_slug] = theme

        merged = list(seen_slugs.values())
        merged.sort(key=lambda t: -(t.momentum_score + t.strength_score))
        logger.info(f"Theme merge: {sum(len(l) for l in theme_lists)} -> {len(merged)} unique themes")
        return merged

    def _evaluate_seed(
        self,
        seed: dict,
        signal_records: list[dict],
        entity_records: list[dict],
    ) -> ThemeCandidate:
        """Score a seed theme against current signal/entity data."""
        candidate = ThemeCandidate(seed=seed)

        keywords = [kw.lower() for kw in seed.get("trigger_keywords", [])]
        trigger_signals = set(seed.get("trigger_signals", []))

        # Check entity mentions
        company_set = set()
        sector_set = set()
        for ent in entity_records:
            name = (ent.get("canonical_name") or "").lower()
            etype = ent.get("entity_type", "")
            ticker = ent.get("ticker", "")

            if any(kw in name for kw in keywords):
                candidate.matched_keywords.append(name)
                if etype == "COMPANY":
                    company_set.add(ticker or name)
                elif etype == "SECTOR":
                    sector_set.add(ent.get("canonical_name", ""))

        candidate.matched_companies = list(company_set)
        candidate.matched_sectors = list(sector_set)

        # Check signal matches
        for sig in signal_records:
            stype = sig.get("signal_type", "")
            if stype in trigger_signals:
                candidate.matched_signals[stype] = candidate.matched_signals.get(stype, 0) + 1
                company = sig.get("company") or sig.get("ticker") or ""
                if company and company not in candidate.matched_companies:
                    candidate.matched_companies.append(company)

        candidate.doc_count = len({s.get("document_id") for s in signal_records
                                    if s.get("signal_type") in trigger_signals})
        return candidate

    def _score_candidate(self, candidate: ThemeCandidate) -> Optional[InvestmentTheme]:
        """Convert a ThemeCandidate to an InvestmentTheme if it meets thresholds.

        Scoring philosophy (earnings-impact driven):
          1. Supply-demand tension is the PRIMARY driver — both must be present
          2. Capex conviction validates demand is structural (multi-year)
          3. Company breadth and keyword matches add confirmation
        """
        seed = candidate.seed
        min_companies = max(self.min_companies, seed.get("min_companies", 2))

        if len(candidate.matched_companies) < min_companies:
            return None
        if candidate.doc_count < self.min_doc_count:
            return None

        # ── Supply-demand tension from matched signals ─────────────────
        DEMAND_SIGNAL_TYPES = {"demand_surge", "capex_increase", "hiring_surge",
                               "technology_adoption", "market_entry"}
        SUPPLY_SIGNAL_TYPES = {"supply_bottleneck", "inventory_drawdown"}

        demand_count = sum(v for k, v in candidate.matched_signals.items()
                           if k in DEMAND_SIGNAL_TYPES)
        supply_count = sum(v for k, v in candidate.matched_signals.items()
                           if k in SUPPLY_SIGNAL_TYPES)
        capex_count = sum(v for k, v in candidate.matched_signals.items()
                          if "capex" in k)

        has_tension = demand_count >= 1 and supply_count >= 1
        if has_tension:
            tension_score = min(
                2.0 * demand_count * supply_count
                / (demand_count + supply_count) * 12.0,
                60.0,
            )
        else:
            tension_score = 0.0

        # ── Capex conviction (structural demand) ──────────────────────
        capex_weight = seed.get("capex_weight", 1.0)
        capex_bonus = min(capex_count * capex_weight * 6.0, 25.0)

        # ── Company breadth ───────────────────────────────────────────
        company_score = min(len(candidate.matched_companies) * 2.5, 20.0)

        # ── Keyword match quality ────────────────────────────────────
        keyword_score = min(len(set(candidate.matched_keywords)) * 2.0, 10.0)

        # ── Quarterly persistence ────────────────────────────────────
        quarterly_bonus = 10.0 if candidate.doc_count >= 6 else (5.0 if candidate.doc_count >= 3 else 0.0)

        strength = min(
            tension_score + capex_bonus + company_score + keyword_score + quarterly_bonus,
            100.0,
        )

        # Conviction: tension-based, not just company count
        n = len(candidate.matched_companies)
        conviction = (
            ThemeConviction.CONFIRMED if tension_score >= 30 and n >= 5
            else ThemeConviction.CONFIRMED if has_tension and n >= 8
            else ThemeConviction.DEVELOPING if tension_score >= 15 or (capex_count >= 3 and n >= 3)
            else ThemeConviction.EMERGING
        )

        # Earnings impact: from seed definition or inferred from tension
        earnings_impact = seed.get("earnings_impact", "moderate")
        if not has_tension and earnings_impact in ("5x+", "3-5x"):
            earnings_impact = "moderate"  # downgrade if no tension in data

        sectors = candidate.matched_sectors or seed.get("sectors", [])

        return InvestmentTheme(
            theme_name=seed["name"],
            theme_slug=seed["slug"],
            description=seed.get("description", ""),
            sectors=sectors,
            signal_types=list(candidate.matched_signals.keys()),
            strength_score=round(strength, 2),
            momentum_score=50.0,  # neutral; ranker computes from snapshot slope
            conviction=conviction,
            doc_count=candidate.doc_count,
            company_count=len(candidate.matched_companies),
            metadata={
                "demand_count": demand_count,
                "supply_constraint_count": supply_count,
                "tension_score": round(tension_score, 2),
                "capex_count": capex_count,
                "earnings_impact": earnings_impact,
                "supply_constraint": seed.get("supply_constraint", ""),
                "key_beneficiaries": seed.get("key_beneficiaries", []),
                "beneficiary_sectors": seed.get("beneficiary_sectors", []),
            },
        )
