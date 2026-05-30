"""
Contradiction Detector
~~~~~~~~~~~~~~~~~~~~~~~
Detects when management narratives change between quarters for the same
company + theme. Uses sentiment scoring on text snippets, then flags
reversals that cross a configurable threshold.

Classic contradictions to catch:
  "Demand is strong"         (Q1)  →  "Inventory correction underway" (Q2)
  "Strong order book"        (Q2)  →  "Customer deferrals"            (Q3)
  "Capacity expansion on track"    →  "Capex review under progress"
  "Pricing power intact"           →  "Margin pressure building"

Sentiment model: lightweight lexicon (no ML). Scores a snippet as a
float in [-1, +1]. A reversal is flagged when Δsentiment > threshold and
at least one reversal keyword pair is detected.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentiment lexicon — positive / negative phrase lists
# ---------------------------------------------------------------------------

POSITIVE_PHRASES = [
    "strong demand", "robust demand", "accelerating demand",
    "order book", "order inflow", "strong pipeline",
    "margin expansion", "pricing power", "market share gain",
    "capacity expansion", "capex committed", "long-term agreement",
    "record revenue", "record orders", "confident", "positive outlook",
    "strong growth", "excellent", "exceptional", "favorable",
]

NEGATIVE_PHRASES = [
    "inventory correction", "inventory build", "destocking",
    "customer deferral", "push-out", "order cancellation",
    "weak demand", "sluggish demand", "demand softness",
    "margin pressure", "pricing pressure", "headwind",
    "challenging environment", "uncertainty", "cautious",
    "capex review", "capex pause", "capex deferred",
    "volume decline", "revenue decline", "loss of market share",
]

# Reversal keyword pairs — presence of (positive_word in Q_n, negative_word in Q_{n+1})
# or vice versa adds weight to contradiction detection.
REVERSAL_PAIRS = [
    ("strong demand", "inventory correction"),
    ("strong demand", "weak demand"),
    ("order inflow", "order cancellation"),
    ("order inflow", "customer deferral"),
    ("pricing power", "pricing pressure"),
    ("margin expansion", "margin pressure"),
    ("capacity expansion", "capex pause"),
    ("capacity expansion", "capex deferred"),
    ("confident", "cautious"),
    ("positive outlook", "uncertain"),
]


class ContradictionType(str, Enum):
    POSITIVE_TO_NEGATIVE = "positive_to_negative"
    NEGATIVE_TO_POSITIVE = "negative_to_positive"   # recovery signal
    DEMAND_REVERSAL = "demand_reversal"
    MARGIN_REVERSAL = "margin_reversal"
    CAPEX_REVERSAL = "capex_reversal"
    GENERAL_REVERSAL = "general_reversal"


@dataclass
class Contradiction:
    """A detected narrative contradiction for a company+theme pair."""
    company: str
    theme: str
    from_quarter: str
    to_quarter: str
    contradiction_type: ContradictionType
    from_sentiment: float       # -1 to +1
    to_sentiment: float         # -1 to +1
    delta: float                # to_sentiment - from_sentiment (negative = bad flip)
    from_phrases: list[str] = field(default_factory=list)   # evidence from Q_n
    to_phrases: list[str] = field(default_factory=list)     # evidence from Q_{n+1}
    reversal_pairs_found: list[tuple] = field(default_factory=list)
    confidence: float = 0.0

    def is_significant(self, threshold: float = 0.4) -> bool:
        return abs(self.delta) >= threshold

    def to_dict(self) -> dict:
        return {
            "company": self.company,
            "theme": self.theme,
            "from_quarter": self.from_quarter,
            "to_quarter": self.to_quarter,
            "type": self.contradiction_type.value,
            "from_sentiment": round(self.from_sentiment, 3),
            "to_sentiment": round(self.to_sentiment, 3),
            "delta": round(self.delta, 3),
            "from_phrases": self.from_phrases[:3],
            "to_phrases": self.to_phrases[:3],
            "reversal_pairs": [list(p) for p in self.reversal_pairs_found[:3]],
            "confidence": round(self.confidence, 3),
        }


class ContradictionDetector:
    """
    Detects narrative reversals by comparing sentiment-scored snippets
    from consecutive quarters for the same company + theme.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.sentiment_threshold = config.get("sentiment_threshold", 0.35)
        self.reversal_confidence_boost = config.get("reversal_confidence_boost", 0.25)
        # Compile all phrase patterns once
        self._pos_patterns = [
            (phrase, re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE))
            for phrase in POSITIVE_PHRASES
        ]
        self._neg_patterns = [
            (phrase, re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE))
            for phrase in NEGATIVE_PHRASES
        ]
        self._reversal_pairs: list[tuple[str, str, re.Pattern, re.Pattern]] = [
            (pos, neg,
             re.compile(r"\b" + re.escape(pos) + r"\b", re.IGNORECASE),
             re.compile(r"\b" + re.escape(neg) + r"\b", re.IGNORECASE))
            for pos, neg in REVERSAL_PAIRS
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_sentiment(self, text: str) -> tuple[float, list[str], list[str]]:
        """
        Score text sentiment on [-1, +1].
        Returns (score, positive_phrases_found, negative_phrases_found).
        """
        pos_found = [phrase for phrase, pat in self._pos_patterns if pat.search(text)]
        neg_found = [phrase for phrase, pat in self._neg_patterns if pat.search(text)]

        pos_score = len(pos_found)
        neg_score = len(neg_found)
        total = pos_score + neg_score

        if total == 0:
            return 0.0, [], []

        sentiment = (pos_score - neg_score) / total
        return round(sentiment, 3), pos_found, neg_found

    def detect(
        self,
        company: str,
        theme: str,
        from_quarter: str,
        from_snippets: list[str],
        to_quarter: str,
        to_snippets: list[str],
    ) -> Optional[Contradiction]:
        """
        Compare snippets from two consecutive quarters for same company+theme.
        Returns a Contradiction if a significant reversal is found, else None.

        Args:
            company:        Company name
            theme:          Theme key
            from_quarter:   Earlier quarter label (e.g. "Q1-2024")
            from_snippets:  Text evidence from from_quarter
            to_quarter:     Later quarter label (e.g. "Q2-2024")
            to_snippets:    Text evidence from to_quarter
        """
        from_text = " ".join(from_snippets)
        to_text = " ".join(to_snippets)

        from_score, from_pos, from_neg = self.score_sentiment(from_text)
        to_score, to_pos, to_neg = self.score_sentiment(to_text)
        delta = to_score - from_score

        if abs(delta) < self.sentiment_threshold:
            return None  # not a significant shift

        # Determine type
        contradiction_type = self._classify_type(
            from_text, to_text, delta, from_pos, from_neg, to_pos, to_neg
        )

        # Check reversal keyword pairs for additional evidence
        reversal_pairs_found = self._find_reversal_pairs(from_text, to_text)

        # Confidence: based on delta magnitude + reversal pair matches
        confidence = min(1.0, abs(delta) * 0.8 + len(reversal_pairs_found) * self.reversal_confidence_boost)

        contradiction = Contradiction(
            company=company,
            theme=theme,
            from_quarter=from_quarter,
            to_quarter=to_quarter,
            contradiction_type=contradiction_type,
            from_sentiment=from_score,
            to_sentiment=to_score,
            delta=delta,
            from_phrases=from_pos + from_neg,
            to_phrases=to_pos + to_neg,
            reversal_pairs_found=reversal_pairs_found,
            confidence=round(confidence, 3),
        )

        logger.info(
            f"Contradiction detected: {company} | {theme} | "
            f"{from_quarter}({from_score:+.2f}) → {to_quarter}({to_score:+.2f}) "
            f"Δ={delta:+.2f} [{contradiction_type.value}]"
        )
        return contradiction

    def detect_from_graph(self, graph_store, company: str = None, theme: str = None) -> list[Contradiction]:
        """
        Scan the graph store for all company+theme pairs and detect contradictions
        across consecutive quarters.

        Args:
            graph_store: GraphStore instance
            company:     Optional filter to single company
            theme:       Optional filter to single theme
        """
        # Get all distinct company+theme mention pairs ordered by quarter
        where = ""
        params: list = []
        if company:
            where += " AND c.name = ?"
            params.append(company)
        if theme:
            where += " AND t.name = ?"
            params.append(theme)

        rows = graph_store.conn.execute(
            f"""SELECT c.name AS company, t.name AS theme,
                       q.label AS quarter,
                       m.snippets, m.confidence
                FROM mentions m
                JOIN companies c ON c.id = m.company_id
                JOIN themes t    ON t.id = m.theme_id
                JOIN quarters q  ON q.id = m.quarter_id
                WHERE 1=1 {where}
                ORDER BY c.name, t.name, q.year, q.quarter_num""",
            params,
        ).fetchall()

        # Group by (company, theme) — list of (quarter, snippets)
        timeline: dict[tuple, list[tuple]] = {}
        for row in rows:
            key = (row["company"], row["theme"])
            snippets = []
            try:
                snippets = json.loads(row["snippets"]) if row["snippets"] else []
            except Exception:
                pass
            timeline.setdefault(key, []).append((row["quarter"], snippets))

        contradictions = []
        for (comp, thm), quarters in timeline.items():
            if len(quarters) < 2:
                continue
            for i in range(len(quarters) - 1):
                from_q, from_s = quarters[i]
                to_q, to_s = quarters[i + 1]
                result = self.detect(comp, thm, from_q, from_s, to_q, to_s)
                if result and result.is_significant(self.sentiment_threshold):
                    contradictions.append(result)
                    # Persist to graph store
                    try:
                        graph_store.record_contradiction(
                            company=comp,
                            theme=thm,
                            from_quarter=from_q,
                            to_quarter=to_q,
                            change_type=result.contradiction_type.value,
                            from_sentiment=result.from_sentiment,
                            to_sentiment=result.to_sentiment,
                            evidence={
                                "from_phrases": result.from_phrases[:3],
                                "to_phrases": result.to_phrases[:3],
                                "reversal_pairs": [list(p) for p in result.reversal_pairs_found[:3]],
                            },
                        )
                    except Exception as e:
                        logger.error(f"Failed to persist contradiction: {e}")

        logger.info(f"Contradiction scan complete: {len(contradictions)} contradictions found.")
        return contradictions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_type(
        self,
        from_text: str,
        to_text: str,
        delta: float,
        from_pos: list,
        from_neg: list,
        to_pos: list,
        to_neg: list,
    ) -> ContradictionType:
        """Determine the sub-type of contradiction."""
        # Positive → negative flip
        direction = ContradictionType.POSITIVE_TO_NEGATIVE if delta < 0 else ContradictionType.NEGATIVE_TO_POSITIVE

        # Demand-specific
        demand_keywords = {"strong demand", "weak demand", "inventory correction", "destocking"}
        if (set(from_pos + from_neg) & demand_keywords) or (set(to_pos + to_neg) & demand_keywords):
            return ContradictionType.DEMAND_REVERSAL

        # Margin-specific
        margin_keywords = {"margin expansion", "pricing power", "margin pressure", "pricing pressure"}
        if (set(from_pos + from_neg) & margin_keywords) or (set(to_pos + to_neg) & margin_keywords):
            return ContradictionType.MARGIN_REVERSAL

        # Capex-specific
        capex_keywords = {"capacity expansion", "capex committed", "capex pause", "capex deferred"}
        if (set(from_pos + from_neg) & capex_keywords) or (set(to_pos + to_neg) & capex_keywords):
            return ContradictionType.CAPEX_REVERSAL

        return direction

    def _find_reversal_pairs(self, from_text: str, to_text: str) -> list[tuple[str, str]]:
        """Find explicit reversal keyword pairs across quarters."""
        found = []
        for pos, neg, pos_re, neg_re in self._reversal_pairs:
            # positive in from_text AND negative in to_text → classic reversal
            if pos_re.search(from_text) and neg_re.search(to_text):
                found.append((pos, neg))
            # Reverse (recovery): negative in from_text AND positive in to_text
            elif neg_re.search(from_text) and pos_re.search(to_text):
                found.append((neg, pos))  # show the direction
        return found
