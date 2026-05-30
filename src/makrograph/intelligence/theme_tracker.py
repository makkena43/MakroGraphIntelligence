"""
Theme Strength Tracker
~~~~~~~~~~~~~~~~~~~~~~
Detects themes in document text and computes a composite strength score
that tracks: mention frequency, growth rate, company breadth, management
confidence signals, capex commitments, and quarterly streak.

Design principle: no ML clustering, no heavy models. Pure signal extraction
using keyword ontology + scoring rules. Accurate signals beat noisy models.

Scoring formula (all components normalized 0-1, then weighted):
    strength = (
        0.25 * mention_score        # raw frequency normalized
      + 0.20 * growth_score         # quarter-over-quarter acceleration
      + 0.15 * company_breadth      # how many companies discuss this
      + 0.20 * confidence_score     # management language strength
      + 0.10 * capex_score          # capex / budget commitments
      + 0.10 * streak_score         # consecutive quarters present
    )
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Theme ontology — extend this dictionary as the investment universe grows.
# Each theme maps to (primary_keywords, confidence_boosters, capex_markers).
# ---------------------------------------------------------------------------

THEME_ONTOLOGY: dict[str, dict] = {
    "AI_Datacenter": {
        "keywords": [
            "artificial intelligence", "ai datacenter", "gpu cluster", "inference",
            "large language model", "llm", "foundation model", "generative ai",
            "ai workload", "ai server", "ai accelerator",
        ],
        "confidence_boosters": [
            "significant investment", "major expansion", "accelerating demand",
            "strong order book", "multi-year", "strategic priority",
        ],
        "capex_markers": [
            "capex", "capital expenditure", "datacenter build", "compute capacity",
            "server procurement", "infrastructure spend",
        ],
    },
    "Semiconductor_Memory": {
        "keywords": [
            "hbm", "high bandwidth memory", "dram", "nand", "memory chip",
            "semiconductor", "wafer", "fab", "node", "hbm2e", "hbm3",
        ],
        "confidence_boosters": [
            "supply tightening", "pricing power", "strong demand", "allocation",
            "design win", "long-term agreement",
        ],
        "capex_markers": [
            "fab expansion", "new fab", "capacity addition", "wafer capacity",
            "equipment order",
        ],
    },
    "Power_Grid_Transmission": {
        "keywords": [
            "power grid", "transmission line", "electricity infrastructure",
            "grid upgrade", "substation", "hvdc", "high voltage", "transformer",
            "power evacuation",
        ],
        "confidence_boosters": [
            "government mandate", "regulatory approval", "order inflow",
            "tendering", "strong pipeline",
        ],
        "capex_markers": [
            "capex outlay", "project award", "epc contract", "grid investment",
        ],
    },
    "Optical_Fiber_Network": {
        "keywords": [
            "optical fiber", "fiber optic", "dark fiber", "wavelength",
            "fiberization", "fiber rollout", "broadband infrastructure",
            "submarine cable",
        ],
        "confidence_boosters": [
            "bharat net", "government tender", "5g backhaul", "rising order book",
        ],
        "capex_markers": [
            "fiber capex", "network deployment", "cable laying",
        ],
    },
    "Defense_Electronics": {
        "keywords": [
            "defense electronics", "military", "indigenization", "atmanirbhar",
            "radar", "electronic warfare", "missile system", "drd0",
            "make in india defense",
        ],
        "confidence_boosters": [
            "order inflow", "government contract", "export potential",
            "strategic partnership",
        ],
        "capex_markers": [
            "manufacturing expansion", "defense capex", "facility upgrade",
        ],
    },
    "Renewable_Energy": {
        "keywords": [
            "solar", "wind energy", "renewable", "green hydrogen", "electrolyzer",
            "energy storage", "battery storage", "pump storage",
            "clean energy",
        ],
        "confidence_boosters": [
            "ppa signed", "capacity addition", "government target",
            "power purchase agreement", "strong pipeline",
        ],
        "capex_markers": [
            "renewable capex", "plant commissioning", "capacity expansion",
            "solar park",
        ],
    },
    "EV_Adoption": {
        "keywords": [
            "electric vehicle", "ev", "battery pack", "charging infrastructure",
            "bms", "ev sales", "two-wheeler ev", "four-wheeler ev",
        ],
        "confidence_boosters": [
            "rising penetration", "subsidy", "fleet electrification",
            "government push",
        ],
        "capex_markers": [
            "battery plant", "gigafactory", "cell manufacturing",
        ],
    },
}

# Sentiment / confidence language patterns
STRONG_CONFIDENCE_PATTERNS = [
    r"\bsignificant(?:ly)?\b",
    r"\bsubstantial(?:ly)?\b",
    r"\baccelerat(?:ing|ion|ed)\b",
    r"\bstrong\b",
    r"\brobust\b",
    r"\bexcellent\b",
    r"\brecord\b",
    r"\bunprecedented\b",
    r"\bcommit(?:ted|ment|ting)\b",
    r"\bpriority\b",
    r"\bguidance\b",
    r"\bconfident(?:ly)?\b",
    r"\bpositiv(?:e|ely)\b",
]

WEAK_CONFIDENCE_PATTERNS = [
    r"\bhope(?:ful)?\b",
    r"\bexpect(?:ing)?\b",
    r"\banticipat(?:e|ing)\b",
    r"\bpossibly\b",
    r"\bpotential(?:ly)?\b",
    r"\bmight\b",
    r"\bcould\b",
]

NEGATIVE_PATTERNS = [
    r"\bheadwind\b",
    r"\binventory correction\b",
    r"\bdestocking\b",
    r"\bweak demand\b",
    r"\bpressure\b",
    r"\bchallenging\b",
    r"\bsluggish\b",
    r"\bdecline\b",
    r"\bdrop\b",
    r"\bfall(?:ing)?\b",
]


@dataclass
class ThemeSignal:
    """All signals extracted for a single theme in a single document."""
    theme: str
    company: str
    quarter: str
    mention_count: int = 0
    confidence_score: float = 0.0     # 0-1: how strongly management discusses it
    capex_mentioned: bool = False
    capex_count: int = 0
    has_negative_signal: bool = False
    snippets: list[str] = field(default_factory=list)  # raw text evidence
    strength_score: float = 0.0       # final composite (set by ThemeTracker)

    def to_dict(self) -> dict:
        return {
            "theme": self.theme,
            "company": self.company,
            "quarter": self.quarter,
            "mention_count": self.mention_count,
            "confidence_score": round(self.confidence_score, 3),
            "capex_mentioned": self.capex_mentioned,
            "capex_count": self.capex_count,
            "has_negative_signal": self.has_negative_signal,
            "strength_score": round(self.strength_score, 3),
            "snippet_count": len(self.snippets),
        }


class ThemeTracker:
    """
    Extracts theme signals from a single earnings call / annual report text,
    then scores them. Aggregation across companies and quarters is done by
    GraphStore which calls ThemeTracker per document.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.ontology = config.get("theme_ontology", THEME_ONTOLOGY)
        self.snippet_window = config.get("snippet_window_chars", 300)
        self.min_mentions = config.get("min_mentions", 1)
        # Compile keyword regex patterns once
        self._patterns: dict[str, dict] = self._compile_patterns()
        self._confidence_re = [re.compile(p, re.IGNORECASE) for p in STRONG_CONFIDENCE_PATTERNS]
        self._weak_re = [re.compile(p, re.IGNORECASE) for p in WEAK_CONFIDENCE_PATTERNS]
        self._negative_re = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_PATTERNS]

    def _compile_patterns(self) -> dict:
        compiled = {}
        for theme, spec in self.ontology.items():
            compiled[theme] = {
                "keyword_re": [
                    re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
                    for kw in spec["keywords"]
                ],
                "confidence_re": [
                    re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
                    for kw in spec["confidence_boosters"]
                ],
                "capex_re": [
                    re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
                    for kw in spec["capex_markers"]
                ],
            }
        return compiled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, text: str, company: str, quarter: str) -> list[ThemeSignal]:
        """
        Extract all theme signals present in the document.
        Returns only themes that meet the min_mentions threshold.
        """
        signals = []
        for theme, patterns in self._patterns.items():
            signal = self._extract_theme(text, theme, patterns, company, quarter)
            if signal.mention_count >= self.min_mentions:
                signal.strength_score = self._compute_strength(signal)
                signals.append(signal)

        signals.sort(key=lambda s: s.strength_score, reverse=True)
        logger.debug(
            f"{company} {quarter}: {len(signals)} themes detected — "
            + ", ".join(f"{s.theme}({s.strength_score:.2f})" for s in signals)
        )
        return signals

    def compute_growth_score(
        self,
        current_mentions: int,
        previous_mentions: Optional[int],
    ) -> float:
        """
        Growth rate of mentions quarter-over-quarter.
        Returns a 0-1 score (capped at 1 for >= 200% growth).
        """
        if previous_mentions is None or previous_mentions == 0:
            return 0.5  # no prior data — neutral
        import math as _math
        growth = (current_mentions - previous_mentions) / max(previous_mentions, 1)
        # tanh maps [-inf,+inf] → (-1,1); shift to [0,1].
        # growth=0→0.5 (neutral), growth=+1→0.82, growth=-1→0.18
        score = 0.5 + 0.5 * _math.tanh(1.5 * growth)
        return round(score, 3)

    def compute_streak_score(self, streak_quarters: int) -> float:
        """
        Repeated quarterly mentions score.
        streak_quarters: how many consecutive quarters the theme appeared.
        sqrt gives diminishing returns: 1Q→0.25, 4Q→0.50, 16Q→1.0.
        """
        import math as _math
        return round(min(1.0, _math.sqrt(streak_quarters) / 4.0), 3)

    def compute_composite_strength(
        self,
        signal: ThemeSignal,
        prev_mentions: Optional[int],
        company_count: int,
        streak_quarters: int,
    ) -> float:
        """
        Full composite score combining all six dimensions.
        Called by GraphStore after aggregating cross-company data.
        """
        import math as _math
        mention_score = min(_math.log1p(signal.mention_count) / _math.log1p(100.0), 1.0)
        growth_score = self.compute_growth_score(signal.mention_count, prev_mentions)
        breadth_score = min(_math.log1p(company_count) / _math.log1p(20.0), 1.0)
        confidence_score = signal.confidence_score
        capex_score = min(_math.log1p(signal.capex_count) / _math.log1p(10.0), 1.0)
        streak_score = self.compute_streak_score(streak_quarters)

        composite = (
            0.25 * mention_score
            + 0.20 * growth_score
            + 0.15 * breadth_score
            + 0.20 * confidence_score
            + 0.10 * capex_score
            + 0.10 * streak_score
        )

        # Penalty for negative signals
        if signal.has_negative_signal:
            composite *= 0.70

        return round(composite, 3)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_theme(
        self,
        text: str,
        theme: str,
        patterns: dict,
        company: str,
        quarter: str,
    ) -> ThemeSignal:
        signal = ThemeSignal(theme=theme, company=company, quarter=quarter)

        # 1. Count keyword mentions and collect snippets
        for kw_re in patterns["keyword_re"]:
            for match in kw_re.finditer(text):
                signal.mention_count += 1
                # Collect surrounding context as evidence snippet
                start = max(0, match.start() - self.snippet_window // 2)
                end = min(len(text), match.end() + self.snippet_window // 2)
                snippet = text[start:end].strip()
                if snippet and snippet not in signal.snippets:
                    signal.snippets.append(snippet)

        if signal.mention_count == 0:
            return signal

        # Use surrounding text of each snippet for signal analysis
        context = " ".join(signal.snippets[:10])  # cap to keep it fast

        # 2. Confidence score from language strength
        strong_hits = sum(1 for p in self._confidence_re if p.search(context))
        # Also check theme-specific confidence boosters
        theme_conf_hits = sum(1 for p in patterns["confidence_re"] if p.search(context))
        weak_hits = sum(1 for p in self._weak_re if p.search(context))

        raw_conf = (strong_hits + theme_conf_hits * 1.5) - (weak_hits * 0.5)
        signal.confidence_score = round(min(1.0, max(0.0, raw_conf / 8.0)), 3)

        # 3. Capex detection
        capex_hits = sum(1 for p in patterns["capex_re"] if p.search(context))
        signal.capex_mentioned = capex_hits > 0
        signal.capex_count = capex_hits

        # 4. Negative signal detection
        neg_hits = sum(1 for p in self._negative_re if p.search(context))
        signal.has_negative_signal = neg_hits >= 2  # require at least 2 to avoid false positives

        return signal

    def _compute_strength(self, signal: ThemeSignal) -> float:
        """Single-document strength — without cross-company / growth data."""
        import math as _math
        mention_score = min(_math.log1p(signal.mention_count) / _math.log1p(100.0), 1.0)
        capex_score = min(_math.log1p(signal.capex_count) / _math.log1p(10.0), 1.0)
        composite = (
            0.40 * mention_score
            + 0.35 * signal.confidence_score
            + 0.25 * capex_score
        )
        if signal.has_negative_signal:
            composite *= 0.70
        return round(composite, 3)
