"""Event-Centric Extraction — Business Event Detector.

Transitions from document-centric to event-centric architecture.
Markets react to EVENTS, not raw filings.

Event types extracted:
    factory_expansion         — new fab, plant, or facility
    factory_closure           — shutdown, consolidation
    shortage                  — supply constraint, allocation
    oversupply                — excess inventory, glut
    price_increase            — ASP increase, pricing power
    price_decrease            — ASP erosion, price war
    export_restriction        — export controls, sanctions
    import_restriction        — tariff, import ban
    investment_announcement   — capex commitment, new program
    partnership_announcement  — JV, alliance, memorandum
    acquisition               — M&A completed or announced
    regulatory_approval       — FDA, FCC, CHIPS Act approval
    regulatory_ban            — prohibition, enforcement action
    technology_breakthrough   — new process node, efficiency record
    demand_surge              — bookings spike, backlog growth
    demand_collapse           — order cancellation, inventory correction
    supply_chain_disruption   — logistics breakdown, force majeure
    hiring_announcement       — headcount expansion, campus opening
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..ontology.ontology_model import EventType, BusinessEvent, NodeType

logger = logging.getLogger(__name__)


# -------------------------------------------------------
# EVENT PATTERNS
# (regex, event_type, direction, confidence)
# -------------------------------------------------------
EVENT_PATTERNS: list[tuple] = [
    # FACTORY / CAPACITY
    (r"\b(?:new|expand|opening|construct|build|break.?ground)\b.{0,80}(?:fab|factory|plant|facility|campus|site|foundry)",
     EventType.FACTORY_EXPANSION, "positive", 0.85),
    (r"\b(?:fab|factory|plant|facility)\b.{0,60}(?:expand|open|new|invest|billion|million)",
     EventType.FACTORY_EXPANSION, "positive", 0.80),
    (r"\b(?:clos|shut.?down|consolidat|exit|divestiture)\b.{0,80}(?:fab|factory|plant|facility|site)",
     EventType.FACTORY_CLOSURE, "negative", 0.85),

    # SHORTAGE / SUPPLY CONSTRAINT
    (r"\b(?:shortage|constrain|bottleneck|allocation|lead.?time|supply.?tight|capacity.?limit)",
     EventType.SHORTAGE, "negative", 0.82),
    (r"\b(?:unable to meet|demand.{0,30}exceed.{0,30}supply|supply.{0,30}constrain)",
     EventType.SHORTAGE, "negative", 0.80),

    # OVERSUPPLY
    (r"\b(?:oversupply|excess.{0,20}inventory|glut|channel.{0,20}inventory|elevated.{0,20}stock)",
     EventType.OVERSUPPLY, "negative", 0.82),
    (r"\b(?:inventory.{0,30}correct|destocking|inventory.{0,20}normaliz)",
     EventType.OVERSUPPLY, "negative", 0.78),

    # PRICING
    (r"\b(?:price.{0,30}increas|ASP.{0,30}up|pricing.{0,20}power|rais.{0,20}price)",
     EventType.PRICE_INCREASE, "positive", 0.80),
    (r"\b(?:price.{0,30}declin|ASP.{0,30}down|price.{0,30}erosion|pricing.{0,20}pressure)",
     EventType.PRICE_DECREASE, "negative", 0.80),

    # EXPORT / IMPORT RESTRICTIONS
    (r"\b(?:export.{0,30}control|export.{0,20}restrict|export.{0,20}licens|entity.{0,20}list)",
     EventType.EXPORT_RESTRICTION, "negative", 0.88),
    (r"\b(?:sanction|embargo|denied.{0,20}export|BIS.{0,20}rule)",
     EventType.EXPORT_RESTRICTION, "negative", 0.85),
    (r"\b(?:import.{0,30}tariff|import.{0,20}ban|import.{0,20}restrict|customs.{0,20}duty)",
     EventType.IMPORT_RESTRICTION, "negative", 0.82),

    # INVESTMENT ANNOUNCEMENTS
    (r"\b(?:invest|commit|allocat|pledge|fund|earmark)\b.{0,60}(?:\$[\d,.]+\s*(?:billion|million|B|M|bn|mn))",
     EventType.INVESTMENT_ANNOUNCEMENT, "positive", 0.88),
    (r"(?:\$[\d,.]+\s*(?:billion|million|B|M|bn|mn)).{0,60}(?:invest|capex|capital.{0,15}expenditure|program|initiative)",
     EventType.INVESTMENT_ANNOUNCEMENT, "positive", 0.85),

    # PARTNERSHIPS
    (r"\b(?:partner|alliance|joint.?venture|JV|collaboration|MOU|memorandum.{0,20}understanding)\b",
     EventType.PARTNERSHIP_ANNOUNCEMENT, "positive", 0.78),
    (r"\b(?:strateg.{0,15}partner|long.?term.{0,15}agreement|supply.{0,15}agreement|exclusiv.{0,15}deal)\b",
     EventType.PARTNERSHIP_ANNOUNCEMENT, "positive", 0.75),

    # ACQUISITIONS
    (r"\b(?:acqui(?:re|red|ring|sition)|purchase.{0,30}(?:company|business|assets?)|merger|take.?over)\b",
     EventType.ACQUISITION, "positive", 0.85),

    # REGULATORY
    (r"\b(?:FDA.{0,20}approv|FCC.{0,20}approv|regulatory.{0,20}approv|CHIPS.{0,20}Act|grant.{0,20}award)\b",
     EventType.REGULATORY_APPROVAL, "positive", 0.85),
    (r"\b(?:FDA.{0,20}reject|banned|prohibited|enforcement.{0,20}action|fine|penalty.{0,20}regulat)\b",
     EventType.REGULATORY_BAN, "negative", 0.82),

    # TECHNOLOGY BREAKTHROUGH
    (r"\b(?:breakthrough|new.{0,20}record|industry.{0,20}first|next.?gen|advanced.{0,20}process|novel.{0,20}technolog)\b",
     EventType.TECHNOLOGY_BREAKTHROUGH, "positive", 0.75),
    (r"\b(?:2nm|3nm|angstrom|GAA|backside.{0,15}power|new.{0,20}architecture)\b",
     EventType.TECHNOLOGY_BREAKTHROUGH, "positive", 0.80),

    # DEMAND SURGE / COLLAPSE
    (r"\b(?:backlog|record.{0,20}order|demand.{0,20}surpas|unprecedented.{0,20}demand|bookings.{0,20}strong)\b",
     EventType.DEMAND_SURGE, "positive", 0.80),
    (r"\b(?:demand.{0,30}weaken|order.{0,20}cancel|demand.{0,20}soften|shipment.{0,20}decline)\b",
     EventType.DEMAND_COLLAPSE, "negative", 0.78),

    # SUPPLY CHAIN DISRUPTION
    (r"\b(?:supply.{0,20}disruption|logistic.{0,20}challenge|force.{0,10}majeure|natural.{0,20}disaster.{0,30}supply)\b",
     EventType.SUPPLY_CHAIN_DISRUPTION, "negative", 0.82),

    # HIRING
    (r"\b(?:hiring|headcount.{0,20}increas|new.{0,20}campus|expand.{0,20}workforce|creat.{0,20}\d+.{0,10}job)\b",
     EventType.HIRING_ANNOUNCEMENT, "positive", 0.75),
]

# Keywords that help identify the subject entity near an event match
COMPANY_CONTEXT_WINDOW = 150  # chars before/after match to search for entity


@dataclass
class ExtractedEvent:
    """A business event detected in text."""
    event_type: EventType
    description: str
    direction: str
    confidence: float
    context_text: str
    subject_entity: str = ""
    subject_type: str = "Company"
    magnitude: Optional[float] = None
    magnitude_unit: str = ""
    second_order_entities: list[str] = field(default_factory=list)
    position: int = 0


class EventExtractor:
    """Extracts discrete business events from financial document text.

    Used by the event-centric pipeline stage to convert filing text
    into structured event records stored in mg_events.
    """

    def __init__(self, config: dict):
        self._min_confidence = config.get("min_confidence", 0.65)
        self._max_events_per_doc = config.get("max_events_per_doc", 200)

    def extract(self, text: str, document_id: Optional[int] = None,
                company: str = "", filed_at: Optional[date] = None) -> list[BusinessEvent]:
        """Extract business events from document text.

        Returns a list of BusinessEvent objects ready for database storage.
        """
        if not text or not text.strip():
            return []

        raw_events = self._extract_patterns(text)
        business_events = []

        for ev in raw_events[:self._max_events_per_doc]:
            subject = ev.subject_entity or company
            magnitude, unit = self._extract_magnitude(ev.context_text)

            business_events.append(BusinessEvent(
                event_type=ev.event_type,
                subject_entity=subject,
                subject_type=NodeType.COMPANY,
                description=ev.description,
                magnitude=magnitude,
                magnitude_unit=unit,
                direction=ev.direction,
                confidence=ev.confidence,
                document_id=document_id,
                filed_at=filed_at or date.today(),
                context_text=ev.context_text[:500],
                second_order_entities=ev.second_order_entities,
            ))

        return business_events

    def _extract_patterns(self, text: str) -> list[ExtractedEvent]:
        """Apply all EVENT_PATTERNS to the text."""
        found: list[ExtractedEvent] = []
        seen_positions: set[tuple] = set()

        for pattern, event_type, direction, confidence in EVENT_PATTERNS:
            if confidence < self._min_confidence:
                continue
            for match in re.finditer(pattern, text, re.IGNORECASE):
                start = match.start()
                bucket = (event_type, start // 200)  # deduplicate nearby matches
                if bucket in seen_positions:
                    continue
                seen_positions.add(bucket)

                ctx_start = max(0, start - 100)
                ctx_end = min(len(text), match.end() + 200)
                context = text[ctx_start:ctx_end].strip()

                found.append(ExtractedEvent(
                    event_type=event_type,
                    description=match.group(0)[:200],
                    direction=direction,
                    confidence=confidence,
                    context_text=context,
                    position=start,
                ))

        found.sort(key=lambda e: e.position)
        return found

    @staticmethod
    def _extract_magnitude(context: str) -> tuple[Optional[float], str]:
        """Extract a dollar amount from the context window."""
        pattern = r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|trillion|B|M|bn|mn)?"
        m = re.search(pattern, context, re.IGNORECASE)
        if not m:
            return None, ""
        try:
            raw = float(m.group(1).replace(",", ""))
            unit = (m.group(2) or "").lower()
            if unit in ("billion", "b", "bn"):
                return raw, "USD_bn"
            elif unit in ("million", "m", "mn"):
                return raw, "USD_mn"
            elif unit in ("trillion",):
                return raw * 1000.0, "USD_bn"
            return raw, "USD"
        except (ValueError, AttributeError):
            return None, ""

    def extract_batch(self, docs: list[dict]) -> list[BusinessEvent]:
        """Extract events from multiple documents.

        Args:
            docs: list of {document_id, text, company, filed_at}
        """
        all_events: list[BusinessEvent] = []
        for doc in docs:
            events = self.extract(
                text=doc.get("text", ""),
                document_id=doc.get("document_id"),
                company=doc.get("company", ""),
                filed_at=doc.get("filed_at"),
            )
            all_events.extend(events)
        logger.info(
            f"EventExtractor batch: {len(docs)} docs → {len(all_events)} events extracted"
        )
        return all_events
