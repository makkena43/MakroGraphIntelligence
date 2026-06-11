"""Investment signal extraction from financial document text.

Signals extracted:
    capex_increase / capex_decrease   - Capital expenditure guidance
    demand_surge / demand_slowdown    - Demand trajectory signals
    supply_bottleneck / supply_easing - Supply chain tightness
    demand_exceeds_supply             - Explicit demand > supply constraint
    strategic_pivot                   - Business strategy change
    partnership_formed                - New strategic partnerships
    acquisition_intent                - M&A signals
    technology_adoption               - New tech investments
    technology_disruption             - Incumbent displacement signals
    competition_threat                - Competitive pressure mentions
    market_entry                      - New market entry signals
    regulatory_tailwind / headwind    - Regulatory environment signals
    hiring_surge / hiring_freeze      - Workforce direction
    inventory_buildup / drawdown      - Inventory cycle signals

Performance notes:
  - All patterns are pre-compiled at module import time (re.compile).
    This eliminates regex compilation overhead on every extract() call.
  - Text is truncated to MAX_TEXT_CHARS before regex scanning. SEC 10-K
    filings often exceed 200k words; the signal-rich content (MD&A, risk
    factors, earnings commentary) is almost always in the first 80k chars.
  - The old generic r"\bsupply\b" pattern has been replaced with tighter
    patterns requiring explicit shortage/bottleneck/constraint context so
    that normal "supply" mentions don't flood the supply_bottleneck bucket.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Maximum characters of document text to scan with regex.
# SEC 10-K filings can be 500k+ chars. Signal-rich sections (MD&A, risk
# factors, earnings call) are almost always in the first 80k characters.
MAX_TEXT_CHARS = 80_000


@dataclass
class InvestmentSignal:
    """A single investment signal extracted from text."""
    signal_type: str
    direction: str                          # positive | negative | neutral
    confidence: float = 0.7
    signal_value: Optional[float] = None
    signal_unit: Optional[str] = None
    context_text: str = ""
    entity_text: str = ""
    extracted_by: str = "rule"
    position: int = 0

    @property
    def is_bullish(self) -> bool:
        return self.direction == "positive"

    @property
    def is_bearish(self) -> bool:
        return self.direction == "negative"


# -----------------------------------------------------------------------
# RAW SIGNAL PATTERNS
# Each tuple: (raw_regex, signal_type, direction, confidence)
#
# Ordering matters: higher-confidence specific patterns should come first
# so that deduplicate() keeps the best match per signal type per window.
# -----------------------------------------------------------------------
_RAW_PATTERNS: list[tuple[str, str, str, float]] = [

    # ── CAPEX ────────────────────────────────────────────────────────────
    (r"\b(?:capital expenditure|capex|capital spending)\b.{0,60}(?:increas|rais|expan|doubl|grow)",
     "capex_increase", "positive", 0.85),
    (r"\b(?:increas|rais|expan|doubl)\b.{0,60}(?:capital expenditure|capex|capital spending)",
     "capex_increase", "positive", 0.85),
    # Investment with any major currency (USD / INR — same intent, same signal)
    (r"\binvest(?:ing|ment)?.{0,40}"
     r"(?:\$\s?\d+(?:\.\d+)?\s*(?:billion|million|B|M\b)|"
     r"(?:Rs\.?\s*|INR\s*|₹\s*)\d[\d,]*\s*(?:crores?|lakhs?|Crs?\b|cr\b))\b",
     "capex_increase", "positive", 0.82),
    # INR amount near expansion/plant/manufacturing context
    (r"(?:Rs\.?\s*|INR\s*|₹\s*)\d[\d,]*(?:\.\d+)?\s*(?:crores?|lakhs?|Crs?\b|cr\b)"
     r".{0,60}(?:invest|capex|plant|expand|facilit|manufactur|greenfield|brownfield)",
     "capex_increase", "positive", 0.82),
    # India-specific capex: greenfield/brownfield plant, new manufacturing line, capacity addition
    (r"\b(?:greenfield|brownfield)\b.{0,60}(?:plant|facilit|project|unit|manufactur|invest)",
     "capex_increase", "positive", 0.85),
    (r"\b(?:new\s+(?:plant|facilit|manufactur|unit|line|capacity)|"
     r"capacity\s+(?:addition|expansion|augment|enhance|creat|build))\b",
     "capex_increase", "positive", 0.83),
    (r"\b(?:set(?:ting)?\s+up|commission(?:ing)?|establish(?:ing)?)\b.{0,40}"
     r"(?:plant|unit|facilit|manufactur|capacity|production)",
     "capex_increase", "positive", 0.82),
    (r"\b(?:capital expenditure|capex)\b.{0,60}(?:decreas|reduc|cut|lower|trim)",
     "capex_decrease", "negative", 0.80),
    # Capital raises for plant/growth — only equity raises with explicit capex/expansion intent
    # EXCLUDED: NCD, debenture, rights issue, QIP alone — these are debt/equity raises that
    # fund working capital or refinancing, NOT plant investment (common in Indian filings).
    (r"\b(?:QIP|qualified\s+institutional\s+placement|preferential\s+allotment)\b"
     r".{0,80}(?:expand|capex|manufactur|plant|facilit|greenfield|brownfield|capacity)",
     "capex_increase", "positive", 0.78),

    # ── DEMAND EXCEEDING SUPPLY (highest value signal — detect first) ─────
    (r"\b(?:demand|orders?|customer(?:s)?|request(?:s)?)\b.{0,80}"
     r"(?:exceed|outstrip|outpac|overwhelm|surpass).{0,50}(?:supply|capacity|production|output)",
     "demand_surge", "positive", 0.92),
    (r"\b(?:can(?:not|'t)\s+(?:meet|keep\s+up\s+with|satisfy|fulfill)|"
     r"unable\s+to\s+(?:meet|satisfy|fulfill)).{0,60}(?:demand|orders?|request|need)",
     "supply_bottleneck", "negative", 0.92),
    # "sold out" / "allocation constrained" — but NOT "fully subscribed" (oversubscribed rights
    # issue / IPO is investor demand signal, not a supply bottleneck in goods/services)
    (r"\b(?:sold\s+out|fully\s+booked|"
     r"allocation.{0,30}(?:limit|constrain|scarc)|"
     r"lead.?time(?:s)?\s+(?:extend|lengthen|stretch|grow|increas))",
     "supply_bottleneck", "negative", 0.90),
    (r"\bwaiting\s+(?:list|time|period).{0,40}(?:grow|increas|lengthen|extend)",
     "supply_bottleneck", "negative", 0.88),

    # ── DEMAND SURGE ─────────────────────────────────────────────────────
    (r"\b(?:demand|orders?|backlog|pipeline)\b.{0,60}"
     r"(?:surge|boom|strong(?:er)?|record|accelerat|exceed|outpac|robust|exceptional)",
     "demand_surge", "positive", 0.85),
    (r"\brecord\b.{0,40}(?:demand|orders?|revenue|sales|bookings?|backlog)",
     "demand_surge", "positive", 0.85),
    (r"\b(?:demand|orders?|bookings?)\b.{0,30}(?:up|grew|grow|increas).{0,20}\d+\s*%",
     "demand_surge", "positive", 0.88),
    (r"\bdemand\s+(?:outlook|trend|environment)\b.{0,40}(?:positive|strong|favor|improv|robust)",
     "demand_surge", "positive", 0.80),
    # Order wins — "bagging" is common in Indian/Asian business English for receiving an order;
    # "awarded", "received", "secured", "won" are universal equivalents
    (r"\b(?:bagg(?:ing|ed)|receiv(?:ing|ed)|secur(?:ing|ed)|award(?:ed)?|win(?:ning|s)?|won)\b"
     r".{0,60}(?:order|contract|project|work\s+order|purchase\s+order)",
     "demand_surge", "positive", 0.88),
    (r"\bnew\s+order(?:s)?\b.{0,60}(?:receiv|secur|bagg|win|award|obtain)",
     "demand_surge", "positive", 0.85),
    # Orders announced with any currency amount
    (r"\b(?:order|contract|project)\b.{0,40}"
     r"(?:(?:Rs\.?\s*|INR\s*|₹\s*)\d[\d,]*\s*(?:crores?|lakhs?|Crs?\b)|"
     r"\$\s*\d+(?:\.\d+)?(?:\s*(?:billion|million|B|M\b))?)",
     "demand_surge", "positive", 0.85),
    # Financial performance growth YoY/QoQ — applies globally (US quarterly reports too)
    (r"\b(?:PAT|profit\s+after\s+tax|net\s+profit|revenue|turnover|sales|earnings|EPS)\b"
     r".{0,40}(?:grew|increas|up|rise|rose|surged|jumped).{0,20}\d+\s*%",
     "demand_surge", "positive", 0.80),
    (r"\bgrew?\b.{0,30}\d+\s*%\b.{0,30}(?:YOY|year.on.year|YTD|QOQ|quarter)",
     "demand_surge", "positive", 0.75),

    # ── DEMAND SLOWDOWN ───────────────────────────────────────────────────
    (r"\b(?:demand|orders?)\b.{0,60}(?:weak|soft|declin|slow|disappoint|compress|soften|muted)",
     "demand_slowdown", "negative", 0.80),
    # Financial performance decline YoY/QoQ — same pattern as surge, inverted
    (r"\b(?:PAT|profit\s+after\s+tax|net\s+profit|revenue|turnover|sales|earnings|EPS)\b"
     r".{0,40}(?:declin|decreas|fell|drop|down|lower).{0,20}\d+\s*%",
     "demand_slowdown", "negative", 0.80),
    (r"\bdeclin\w*\b.{0,30}\d+\s*%\b.{0,30}(?:YOY|year.on.year|QOQ|quarter)",
     "demand_slowdown", "negative", 0.75),

    # ── SUPPLY BOTTLENECK ─────────────────────────────────────────────────
    (r"\b(?:supply\s+(?:chain\s+)?(?:shortage|constraint|crunch|tightness|disruption|bottleneck)|"
     r"capacity\s+(?:constraint|crunch|limit|shortfall|tighten)|"
     r"component\s+(?:shortage|scarcity|crunch)|"
     r"material\s+(?:shortage|scarcity|constraint))",
     "supply_bottleneck", "negative", 0.87),
    (r"\b(?:supply\s+chain)\b.{0,60}(?:tighten|strain|disrupt|challeng|stress|squeez)",
     "supply_bottleneck", "negative", 0.83),
    (r"\b(?:bottleneck|constrain|shortage|scarcity|crunch)\b.{0,60}"
     r"(?:material|component|chip|wafer|lith|cobalt|copper|power|grid|labor|talent|bandwidth|rack|gpu)",
     "supply_bottleneck", "negative", 0.88),
    (r"\b(?:limited\s+(?:supply|availability|capacity)|"
     r"supply\s+(?:limited|constrained|tight|insufficient))",
     "supply_bottleneck", "negative", 0.82),

    # ── SUPPLY EASING ─────────────────────────────────────────────────────
    (r"\b(?:supply\s+chain)\b.{0,60}(?:normal|ease|improv|resol|recover|stabiliz)",
     "supply_easing", "positive", 0.75),
    (r"\b(?:inventory\s+(?:normal|recover|stabiliz|build)|"
     r"supply\s+(?:recover|improv|ease|increase|catch\s+up))",
     "supply_easing", "positive", 0.75),

    # ── INVENTORY ─────────────────────────────────────────────────────────
    (r"\b(?:inventory).{0,40}(?:build|accumulat|higher|days\s+on\s+hand|increas)",
     "inventory_buildup", "neutral", 0.70),
    (r"\b(?:inventory).{0,40}(?:draw|declin|reduc|normaliz|burn|digest|thin)",
     "inventory_drawdown", "neutral", 0.70),

    # ── STRATEGIC ────────────────────────────────────────────────────────
    (r"\b(?:strategic\s+pivot|shift\s+strateg|new\s+(?:strategic\s+)?direction|"
     r"strategic\s+review|business\s+transformation)\b",
     "strategic_pivot", "neutral", 0.80),
    (r"\b(?:pivot(?:ing|ed)?|repositioning)\b.{0,60}(?:strateg|business|model|focus|portfol)",
     "strategic_pivot", "neutral", 0.78),
    (r"\b(?:partner(?:ship)?|joint\s+venture|collaborat|alliance).{0,40}(?:announc|form|sign|enter)",
     "partnership_formed", "positive", 0.80),
    # M&A — require deal-action verb to avoid firing on routine "informed about Merger" boilerplate.
    # "buy-back" / "buyback" excluded — that is share repurchase, not acquisition.
    # "buy" alone excluded to prevent "buy-back" and financial buyouts from casual context.
    (r"\b(?:acqui(?:sition|re|ring)|merger(?!\s+of\s+equals)|takeover|buyout)\b"
     r".{0,80}(?:agree|announc|approv|complet|clos|sign|propos|plan|intend|contemplat|pursu)"
     r"|"
     r"(?:agree|announc|approv|complet|clos|sign|propos|plan|intend|contemplat|pursu)"
     r".{0,80}\b(?:acqui(?:sition|re|ring)|merger|takeover|buyout)\b",
     "acquisition_intent", "positive", 0.80),

    # ── TECHNOLOGY ────────────────────────────────────────────────────────
    (r"\b(?:deploy(?:ing|ment)?|adopt(?:ing)?|implement(?:ing)?|integrat(?:ing)?).{0,40}"
     r"(?:AI|artificial\s+intelligence|machine\s+learning|LLM|GPU|cloud|automation|robotics)",
     "technology_adoption", "positive", 0.85),
    (r"\b(?:replac|displac|disrupt|obsolete|legacy).{0,40}(?:technolog|platform|product|system)",
     "technology_disruption", "negative", 0.75),

    # ── COMPETITION ───────────────────────────────────────────────────────
    (r"\b(?:compet(?:ition|itor|itive)|market\s+share.{0,30}(?:loss|declin|gain|erode))",
     "competition_threat", "negative", 0.75),
    (r"\b(?:enter(?:ing)?|launch(?:ing)?).{0,30}(?:market|segment|geography)",
     "market_entry", "positive", 0.70),

    # ── REGULATORY ────────────────────────────────────────────────────────
    # Generic language (works for any market)
    (r"\b(?:regulat(?:ion|ory)).{0,60}(?:favorable|tailwind|benefit|support|approv)",
     "regulatory_tailwind", "positive", 0.80),
    (r"\b(?:regulat(?:ion|ory)).{0,60}(?:headwind|restrict|penalt|fine|challeng|concern)",
     "regulatory_headwind", "negative", 0.80),
    # US regulators
    (r"\b(?:FDA|EPA|FTC|CFPB|CFTC|antitrust).{0,40}(?:approv|clear|pass|grant)",
     "regulatory_tailwind", "positive", 0.85),
    (r"\b(?:FDA|EPA|FTC|CFPB).{0,40}(?:investigat|reject|fine|penalt|block)",
     "regulatory_headwind", "negative", 0.85),
    # India regulators — same signal type, same pattern structure as FDA/EPA above
    # Note: "prohibit" excluded — "SEBI (Prohibition of Insider Trading) Regulations"
    # is a regulation name, not enforcement action. "order" excluded for same reason.
    (r"\b(?:SEBI|RBI|Reserve\s+Bank|NCLT|NCLAT|CCI|DPIIT|MoF|Ministry\s+of\s+Finance)\b"
     r".{0,60}(?:approv|clear|allow|permit|grant|nod|green.?light|sanction)",
     "regulatory_tailwind", "positive", 0.85),
    (r"\b(?:SEBI|RBI|NCLT|CCI)\b.{0,60}"
     r"(?:reject|fine|penalt|investigat|suspen|restrain|block|violat|"
     r"impos\w*\s+(?:penalty|fine)|show.cause|adverse\s+order|enforcement\s+action)",
     "regulatory_headwind", "negative", 0.85),

    # ── HIRING ────────────────────────────────────────────────────────────
    (r"\b(?:hir(?:ing|e)|headcount|workforce|recruit).{0,40}"
     r"(?:expan|increas|surge|ramp|significan|accelerat)",
     "hiring_surge", "positive", 0.75),
    (r"\b(?:layoff|headcount.{0,20}reduc|workforce.{0,20}reduc|restructur|right.?siz)",
     "hiring_freeze", "negative", 0.80),
]

# Pre-compiled at module import — shared across all SignalExtractor instances.
# Each entry is now a compiled Pattern object (not a raw string).
SIGNAL_PATTERNS: list[tuple[re.Pattern, str, str, float]] = [
    (re.compile(raw, re.IGNORECASE), sig_type, direction, conf)
    for raw, sig_type, direction, conf in _RAW_PATTERNS
]

_MONEY_RE = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s*(billion|million|trillion|B|M|bn|mn)?",
    re.IGNORECASE,
)

# Indian number format: Rs. 1,234.56 crore / INR 500 lakh / ₹1200 Cr
_MONEY_INR_RE = re.compile(
    r"(?:Rs\.?\s*|INR\s*|₹\s*)(\d[\d,]*(?:\.\d+)?)\s*"
    r"(crores?|lakhs?|Crs?\b|L\b|cr\b)",
    re.IGNORECASE,
)

# ── Theme-entity extractor for India signals ──────────────────────────────────
# When a signal fires, scan its context for known TECHNOLOGY/SECTOR keywords.
# The first match becomes entity_text so the signal is linked to the theme
# entity rather than just the filer company.
# Order matters: longer/more specific matches are listed first so they win
# over generic overlapping terms (e.g. "data center" before "data").
_THEME_ENTITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdata\s+cent(?:er|re)s?\b",          re.I), "Data Center"),
    (re.compile(r"\bartificial\s+intelligence\b",        re.I), "Artificial Intelligence"),
    (re.compile(r"\bgenerative\s+ai\b",                  re.I), "Generative AI"),
    (re.compile(r"\bmachine\s+learning\b",               re.I), "Machine Learning"),
    (re.compile(r"\belectric\s+vehicle|ev\s+(?:charging|manufactur|segment)\b", re.I), "Electric Vehicle"),
    (re.compile(r"\bvande\s+bharat\b",                   re.I), "Vande Bharat"),
    (re.compile(r"\bspecialty\s+chem(?:ical)?s?\b",      re.I), "Specialty Chemicals"),
    (re.compile(r"\bsemiconductor\b",                    re.I), "Semiconductor"),
    (re.compile(r"\breal\s+estate\b",                    re.I), "Real Estate"),
    (re.compile(r"\brenewable\s+energy\b",               re.I), "Renewable Energy"),
    (re.compile(r"\bcybersecurit\w+\b",                  re.I), "Cybersecurity"),
    (re.compile(r"\bagrochemic(?:al)?s?\b",              re.I), "Agrochemicals"),
    (re.compile(r"\bherbicid\w+\b",                      re.I), "Herbicides"),
    (re.compile(r"\baerospace\b",                        re.I), "Aerospace"),
    (re.compile(r"\bdefence|defense\b",                  re.I), "Defense"),
    (re.compile(r"\bautomotive\b",                       re.I), "Automotive"),
    (re.compile(r"\btextile\b",                          re.I), "Textiles"),
    (re.compile(r"\bpharmaceut\w+|pharma\b",             re.I), "Pharma"),
    (re.compile(r"\bhealthcare|hospital\b",              re.I), "Healthcare"),
    (re.compile(r"\bsolar\b",                            re.I), "Solar"),
    (re.compile(r"\bwind\s+(?:energy|power|turbine|farm|project)\b", re.I), "Wind"),
    (re.compile(r"\bbattery\b",                          re.I), "Battery"),
    (re.compile(r"\blithium\b",                          re.I), "Lithium"),
    (re.compile(r"\bcement\b",                           re.I), "Cement"),
    (re.compile(r"\bsteel\b",                            re.I), "Steel"),
    (re.compile(r"\bcloud\b",                            re.I), "Cloud"),
    (re.compile(r"\brobotics?\b",                        re.I), "Robotics"),
    (re.compile(r"\bfoundr(?:y|ies)\b",                  re.I), "Foundry"),
    (re.compile(r"\btransformer\b",                      re.I), "Transformer"),
    (re.compile(r"\bwafer\b",                            re.I), "Wafer"),
    (re.compile(r"\bbiotech\b",                          re.I), "Biotech"),
    (re.compile(r"\bnbfc\b",                             re.I), "NBFC"),
]

def _extract_theme_entity(context: str) -> str:
    """Find the most relevant theme entity in a signal's context window.

    Scans left-to-right through ordered patterns (specific → generic).
    Returns the canonical entity name or empty string if none found.
    """
    for pattern, entity_name in _THEME_ENTITY_PATTERNS:
        if pattern.search(context):
            return entity_name
    return ""


class SignalExtractor:
    """Extracts investment signals from financial document text using pre-compiled pattern rules."""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.min_confidence = cfg.get("min_confidence", 0.65)
        self.context_window = cfg.get("context_window_chars", 200)
        self.max_signals_per_doc = cfg.get("max_signals_per_doc", 100)
        # Cap text length scanned per document. SEC filings can be 500k+ chars;
        # signal-rich content is almost always within the first 80k chars.
        self.max_text_chars = cfg.get("max_text_chars_for_signals", MAX_TEXT_CHARS)

    def extract(self, text: str, document_id: int = None) -> list[InvestmentSignal]:
        """Extract all investment signals from document text."""
        if not text:
            return []

        # Truncate to signal-rich portion — avoids scanning boilerplate footnotes
        scan_text = text[:self.max_text_chars]

        signals: list[InvestmentSignal] = []

        for compiled_pattern, signal_type, direction, confidence in SIGNAL_PATTERNS:
            if confidence < self.min_confidence:
                continue
            for match in compiled_pattern.finditer(scan_text):
                start = match.start()
                ctx_start = max(0, start - self.context_window // 2)
                ctx_end = min(len(scan_text), match.end() + self.context_window // 2)
                context = scan_text[ctx_start:ctx_end].strip()

                value, unit = self._extract_amount(context)
                # Extract the theme entity (technology/sector keyword) from context.
                # This links the signal to WHAT it's about, not just WHO filed it.
                # e.g. "50% capacity expansion in Solar Glass" → entity_text="Solar"
                theme_entity = _extract_theme_entity(context)
                signals.append(InvestmentSignal(
                    signal_type=signal_type,
                    direction=direction,
                    confidence=confidence,
                    signal_value=value,
                    signal_unit=unit,
                    context_text=context,
                    entity_text=theme_entity,
                    extracted_by="rule",
                    position=start,
                ))

            if len(signals) >= self.max_signals_per_doc:
                break

        return self._deduplicate(signals)

    def _extract_amount(self, context: str) -> tuple[Optional[float], Optional[str]]:
        """Extract a monetary value from surrounding context."""
        match = _MONEY_RE.search(context)
        if match:
            raw = float(match.group(1))
            unit = (match.group(2) or "").lower()
            multiplier_map = {
                "trillion": 1e12, "billion": 1e9, "million": 1e6,
                "bn": 1e9, "mn": 1e6, "b": 1e9, "m": 1e6,
            }
            return raw * multiplier_map.get(unit, 1), "USD_" + (unit or "units")
        return None, None

    def _deduplicate(self, signals: list[InvestmentSignal]) -> list[InvestmentSignal]:
        """Remove near-duplicate signals (same type within 500-char window)."""
        seen: dict[str, int] = {}
        result = []
        for sig in sorted(signals, key=lambda s: -s.confidence):
            key = sig.signal_type
            if key not in seen or abs(sig.position - seen[key]) > 500:
                seen[key] = sig.position
                result.append(sig)
        return result

    def extract_batch(
        self, doc_texts: list[tuple[int, str]]
    ) -> dict[int, list[InvestmentSignal]]:
        """Extract signals from multiple documents. Returns {doc_id: [signals]}."""
        results = {}
        for doc_id, text in doc_texts:
            results[doc_id] = self.extract(text, document_id=doc_id)
        total = sum(len(v) for v in results.values())
        logger.info(f"Signal extraction: {total} signals from {len(doc_texts)} docs")
        return results

    def get_signal_summary(self, signals: list[InvestmentSignal]) -> dict:
        """Summarize signals: count by type and direction."""
        by_type: dict[str, dict] = {}
        for sig in signals:
            if sig.signal_type not in by_type:
                by_type[sig.signal_type] = {"positive": 0, "negative": 0, "neutral": 0, "total": 0}
            by_type[sig.signal_type][sig.direction] += 1
            by_type[sig.signal_type]["total"] += 1
        return by_type
