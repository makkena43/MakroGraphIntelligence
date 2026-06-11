"""Order Book Pressure Detector (India Pipeline — Layer 9).

Converts order-book growth, high utilization, execution visibility, and
tender pipeline commentary in India company filings into supply-constraint
signals for the existing signal pipeline.

This bridges the gap between Indian management commentary (which discusses
order wins and execution timelines, not "supply shortages") and the US-style
supply_bottleneck signals that feed theme detection.

Detection patterns:
  1. Order Book Surge — rapid order book growth signals demand pull
  2. Utilization Near-Ceiling — capacity utilization >85% signals near-constraint
  3. Execution Visibility — long order-to-execution timelines signal backlog
  4. Tender Pipeline Commentary — management discussing tender wins / pipeline
  5. Lead Time Extension — delivery schedule delays signal supply pressure
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NLP patterns for order book pressure signals in India concall/filing text
# ---------------------------------------------------------------------------

# Pattern set: (signal_type, direction, patterns, context_keywords)
_ORDER_BOOK_PATTERNS: list[dict] = [
    {
        "name": "order_book_surge",
        "signal_type": "supply_bottleneck",
        "direction": "negative",
        "patterns": [
            re.compile(r"order\s+book\s+(?:grew|grown|increased|surged|reached|stands\s+at|hit|crossed)\b.{0,80}(?:crore|cr\.|lakh|billion|%)", re.I),
            re.compile(r"(?:record|highest.?ever|all.?time\s+high)\s+order\s+book", re.I),
            re.compile(r"order\s+(?:inflow|intake)\s+of\s+(?:Rs\.?|INR|₹)?\s*[\d,]+", re.I),
            re.compile(r"L1\s+(?:position|status)\s+(?:for|in)\b.{0,60}(?:crore|project|tender)", re.I),
        ],
        "min_matches": 1,
        "confidence": 0.72,
    },
    {
        "name": "utilization_ceiling",
        "signal_type": "supply_bottleneck",
        "direction": "negative",
        "patterns": [
            re.compile(r"(?:capacity\s+utilisation|capacity\s+utilization)\s+(?:of\s+)?(?:8[5-9]|9\d)%", re.I),
            re.compile(r"(?:running\s+at|operating\s+at)\s+(?:full|near.?full|near\s+full|~?(?:8[5-9]|9\d))%?\s+capacity", re.I),
            re.compile(r"(?:sold\s+out|fully\s+booked|capacity\s+full|no\s+spare\s+capacity|fully\s+allocated)", re.I),
            re.compile(r"(?:debottlenecking|capacity\s+expansion\s+underway|capacity\s+addition\s+in\s+progress)", re.I),
        ],
        "min_matches": 1,
        "confidence": 0.80,
    },
    {
        "name": "execution_visibility",
        "signal_type": "supply_bottleneck",
        "direction": "negative",
        "patterns": [
            re.compile(r"(?:execution\s+visibility|revenue\s+visibility)\s+(?:of|for)\s+(?:\d+)\s*(?:years?|months?|quarters?)", re.I),
            re.compile(r"(?:backlog|order\s+book)\s+(?:execution|of)\s+(?:\d+)\s*(?:years?|months?|quarters?)", re.I),
            re.compile(r"(?:18|24|36|48|60)\s*months?\s+(?:execution|revenue)\s+visibility", re.I),
            re.compile(r"tender\s+pipeline\s+of\s+(?:Rs\.?|INR)?\s*[\d,]+", re.I),
        ],
        "min_matches": 1,
        "confidence": 0.75,
    },
    {
        "name": "lead_time_extension",
        "signal_type": "supply_bottleneck",
        "direction": "negative",
        "patterns": [
            re.compile(r"(?:lead\s+time|delivery\s+schedule|delivery\s+timeline)\s+(?:extended|increased|longer|stretched)", re.I),
            re.compile(r"(?:waiting\s+(?:period|time)|queue)\s+(?:of|is)\s+(?:\d+)\s*(?:months?|weeks?)", re.I),
            re.compile(r"(?:order\s+to\s+delivery|booking\s+to\s+delivery)\s+(?:of|is)\s+(?:\d+)\s*(?:months?|years?)", re.I),
            re.compile(r"(?:customers?\s+(?:waiting|queuing)|advance\s+booking)\b.{0,60}(?:months?|years?)", re.I),
        ],
        "min_matches": 1,
        "confidence": 0.82,
    },
    {
        "name": "pricing_power_signal",
        "signal_type": "supply_bottleneck",
        "direction": "negative",
        "patterns": [
            re.compile(r"(?:price\s+hike|price\s+increase|price\s+revision)\s+(?:of\s+)?[\d]+%", re.I),
            re.compile(r"(?:pass(?:ed|ing)\s+on|passed\s+through)\s+(?:cost|price|input\s+cost)", re.I),
            re.compile(r"(?:pricing\s+power|able\s+to\s+raise\s+prices|demand\s+supports\s+pricing)", re.I),
        ],
        "min_matches": 1,
        "confidence": 0.70,
    },
    {
        "name": "capex_expansion_signal",
        "signal_type": "capex_increase",
        "direction": "positive",
        "patterns": [
            re.compile(r"(?:capacity\s+expansion|greenfield|brownfield)\s+(?:of|for|at|capex|investment)", re.I),
            re.compile(r"(?:capex|capital\s+expenditure)\s+(?:of|guidance|plan)\s+(?:Rs\.?|INR|₹)?\s*[\d,]+", re.I),
            re.compile(r"(?:new\s+(?:plant|facility|line|unit|manufacturing\s+facility))\s+(?:coming|planned|under\s+construction)", re.I),
        ],
        "min_matches": 1,
        "confidence": 0.75,
    },
]


@dataclass
class OrderBookSignal:
    document_id: int
    company: str
    ticker: str
    detection_name: str
    signal_type: str
    direction: str
    confidence: float
    context_text: str
    matched_pattern: str
    sector_tags: list[str]
    filed_at: Optional[date]


def _extract_sector_tags(text: str) -> list[str]:
    """Quick sector tagging from context text."""
    tags = []
    sector_patterns = [
        (re.compile(r"\b(?:transformer|substation|CRGO|power\s+equipment)\b", re.I), "power_transmission"),
        (re.compile(r"\b(?:solar|PV|photovoltaic|module)\b", re.I), "solar"),
        (re.compile(r"\b(?:railway|rail|wagon|locomotive|metro)\b", re.I), "railway_infrastructure"),
        (re.compile(r"\b(?:cable|conductor|wire|copper)\b", re.I), "power_transmission"),
        (re.compile(r"\b(?:electronics|EMS|PCB|smart\s+meter)\b", re.I), "electronics_manufacturing"),
        (re.compile(r"\b(?:battery|BESS|energy\s+storage)\b", re.I), "battery_storage"),
        (re.compile(r"\b(?:fiber|fibre|5G|telecom)\b", re.I), "5g_telecom"),
        (re.compile(r"\b(?:defence|defense|military)\b", re.I), "defense_electronics"),
    ]
    for pattern, sector in sector_patterns:
        if pattern.search(text):
            tags.append(sector)
    return list(dict.fromkeys(tags))  # deduplicate preserving order


class OrderBookPressureDetector:
    """Layer 9: Convert India concall/filing text into supply-constraint signals.

    Usage in pipeline::

        detector = OrderBookPressureDetector(config)
        signals = detector.detect_from_text(raw_text, doc_id, company, ticker, filed_at)
        # These signals are in the same schema as mg_signals and can be inserted directly.
    """

    def __init__(self, config: dict = None):
        self._cfg = config or {}

    def detect_from_text(
        self,
        text: str,
        document_id: int,
        company: str,
        ticker: str,
        filed_at: Optional[date] = None,
    ) -> list[OrderBookSignal]:
        """Scan a document's raw text for order book pressure signals."""
        signals: list[OrderBookSignal] = []
        if not text:
            return signals

        sector_tags = _extract_sector_tags(text)

        for rule in _ORDER_BOOK_PATTERNS:
            for pattern in rule["patterns"]:
                m = pattern.search(text)
                if m:
                    start = max(0, m.start() - 100)
                    end = min(len(text), m.end() + 150)
                    ctx = text[start:end].strip()
                    signals.append(OrderBookSignal(
                        document_id=document_id,
                        company=company,
                        ticker=ticker,
                        detection_name=rule["name"],
                        signal_type=rule["signal_type"],
                        direction=rule["direction"],
                        confidence=rule["confidence"],
                        context_text=ctx[:500],
                        matched_pattern=m.group(0)[:100],
                        sector_tags=sector_tags,
                        filed_at=filed_at,
                    ))
                    break  # one match per rule per document

        if signals:
            logger.debug(f"[OrderBookDetector] {company}: {len(signals)} order book signals")
        return signals

    def detect_from_db_batch(
        self,
        pg_store,
        batch_size: int = 200,
        lookback_days: int = 90,
        as_of_date: Optional[date] = None,
    ) -> dict:
        """Run order book detection across a batch of India documents from the DB.

        Returns stats dict with signals_generated count.
        """
        _as_of = as_of_date or date.today()
        _floor = _as_of - timedelta(days=lookback_days)
        stats = {"docs_scanned": 0, "signals_generated": 0, "docs_with_signals": 0}

        if not pg_store:
            return stats

        try:
            from psycopg2.extras import RealDictCursor
            with pg_store._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT id, company, ticker, filed_at, raw_text, title
                        FROM mg_documents
                        WHERE country = 'IN'
                          AND filed_at BETWEEN %s AND %s
                          AND (raw_text IS NOT NULL AND raw_text != ''
                               OR title IS NOT NULL)
                        ORDER BY filed_at DESC
                        LIMIT %s
                    """, (_floor, _as_of, batch_size))
                    docs = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"[OrderBookDetector] DB query failed: {e}")
            return stats

        all_signals: list[dict] = []
        for doc in docs:
            text = (doc.get("raw_text") or "") or (doc.get("title") or "")
            if not text:
                continue
            detected = self.detect_from_text(
                text, doc["id"],
                doc.get("company") or "", doc.get("ticker") or "",
                doc.get("filed_at"),
            )
            if detected:
                stats["docs_with_signals"] += 1
                for sig in detected:
                    all_signals.append({
                        "document_id": sig.document_id,
                        "entity_id":   None,
                        "signal_type": sig.signal_type,
                        "direction":   sig.direction,
                        "confidence":  sig.confidence,
                        "signal_value": None,
                        "signal_unit":  None,
                        "context_text": sig.context_text[:500],
                        "extracted_by": f"ob:{sig.detection_name}"[:30],
                        "filed_at":     sig.filed_at,
                        "country":      "IN",
                    })
            stats["docs_scanned"] += 1

        # Insert signals using existing batch insert
        if all_signals:
            try:
                pg_store.batch_insert_signals(all_signals)
                stats["signals_generated"] = len(all_signals)
            except Exception as e:
                logger.warning(f"[OrderBookDetector] batch insert failed: {e}")
                # Fallback: individual inserts
                for sd in all_signals:
                    try:
                        pg_store.insert_signal(sd)
                        stats["signals_generated"] += 1
                    except Exception:
                        pass

        logger.info(f"[OrderBookDetector] Scanned {stats['docs_scanned']} docs, "
                    f"generated {stats['signals_generated']} signals from "
                    f"{stats['docs_with_signals']} docs with hits")
        return stats
