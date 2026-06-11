"""Tender Intelligence (India Pipeline — Layer 8).

Ingests tender data from:
  - GeM (Government e-Marketplace) — gem.gov.in
  - CPPP (Central Public Procurement Portal) — eprocure.gov.in
  - SECI (Solar Energy Corporation of India) tenders
  - NTPC tenders
  - Railways / IRCTC tenders
  - PowerGrid Corporation tenders
  - State electricity board (SEB) tenders

Tenders are the earliest demand signal for India infrastructure capex —
they appear 6–18 months before management teams discuss shortages in earnings
calls, making them a leading indicator for supply bottlenecks.

Output: TenderSignal records stored to mg_tender_signals.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TenderSignal:
    source: str              # e.g. "SECI", "NTPC", "GeM", "PowerGrid", "Railways"
    tender_id: str
    title: str
    sector: str              # mapped sector (solar, power_transmission, railway, etc.)
    quantity: Optional[float]
    unit: Optional[str]
    estimated_value_inr_cr: Optional[float]
    tender_date: Optional[date]
    deadline: Optional[date]
    component_tags: list[str]   # supply chain components implied
    demand_signal_strength: float   # 0.0 – 1.0
    url: str = ""


# ---------------------------------------------------------------------------
# Sector classification for tender titles
# ---------------------------------------------------------------------------

_TENDER_SECTOR_RULES: list[tuple[re.Pattern, str, list[str]]] = [
    # Solar / RE
    (re.compile(r"\b(?:solar|PV\s+module|photovoltaic|SECI|renewable\s+energy)\b", re.I),
     "solar", ["Solar Module", "Solar Cell", "Solar Wafer"]),
    (re.compile(r"\b(?:wind\s+(?:turbine|energy|power|farm))\b", re.I),
     "wind", ["Rolling Stock / Locomotives"]),
    # Transformer / Power
    (re.compile(r"\b(?:transformer|CRGO|power\s+equipment|substation)\b", re.I),
     "power_transmission", ["Power Transformer", "CRGO Steel"]),
    (re.compile(r"\b(?:HV\s+cable|EHV\s+cable|HVDC|transmission\s+line)\b", re.I),
     "power_transmission", ["HV Cable", "Copper Winding Wire"]),
    # Railways
    (re.compile(r"\b(?:wagon|locomotive|rolling\s+stock|coach|DFC|railway|Vande\s+Bharat)\b", re.I),
     "railway_infrastructure", ["Rolling Stock / Locomotives", "Traction Motor"]),
    # Electronics / EMS
    (re.compile(r"\b(?:electronics|EMS|PCB|printed\s+circuit|smart\s+meter|set.?top.?box)\b", re.I),
     "electronics_manufacturing", ["EMS / Contract Manufacturing", "PCB / Printed Circuit Board"]),
    # Battery / EV
    (re.compile(r"\b(?:battery|BESS|energy\s+storage|EV\s+charging)\b", re.I),
     "battery_storage", ["Battery Cell (Li-ion)"]),
    # Telecom / 5G
    (re.compile(r"\b(?:optical\s+fib(?:re|er)|5G|telecom|tower|BTS)\b", re.I),
     "5g_telecom", ["Optical Fiber Cable", "5G BTS / Radio Unit"]),
    # Defense
    (re.compile(r"\b(?:defence|defense|military|BEL|DRDO|radar|naval)\b", re.I),
     "defense_electronics", []),
    # Water
    (re.compile(r"\b(?:water\s+treatment|STP|ETP|Jal\s+Jeevan|desalination)\b", re.I),
     "water_infrastructure", []),
]

# Value extraction patterns
_VALUE_PATTERN = re.compile(
    r"(?:Rs\.?|INR|₹)\s*([\d,\.]+)\s*(crore|lakh|Cr\b|L\b)", re.I
)
_QTY_PATTERN = re.compile(r"([\d,\.]+)\s*(MW|GW|MVA|kV|km|units?|nos?\.?|sets?)\b", re.I)


def _classify_tender(title: str) -> tuple[str, list[str]]:
    """Return (sector, component_tags) for a tender title."""
    for pattern, sector, tags in _TENDER_SECTOR_RULES:
        if pattern.search(title):
            return sector, tags
    return "infrastructure", []


def _extract_value(text: str) -> Optional[float]:
    m = _VALUE_PATTERN.search(text)
    if not m:
        return None
    raw = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    if "lakh" in unit or unit == "l":
        raw /= 100.0   # lakh → crore
    return round(raw, 2)


def _extract_quantity(text: str) -> tuple[Optional[float], Optional[str]]:
    m = _QTY_PATTERN.search(text)
    if not m:
        return None, None
    raw = float(m.group(1).replace(",", ""))
    unit = m.group(2)
    return raw, unit


def _demand_signal_strength(sector: str, value_cr: Optional[float]) -> float:
    """Estimate demand signal strength from sector priority + tender size."""
    sector_weights = {
        "solar": 0.75, "power_transmission": 0.85,
        "railway_infrastructure": 0.80, "electronics_manufacturing": 0.70,
        "battery_storage": 0.75, "5g_telecom": 0.65,
        "defense_electronics": 0.70, "water_infrastructure": 0.55,
        "wind": 0.65, "infrastructure": 0.40,
    }
    base = sector_weights.get(sector, 0.40)
    if value_cr:
        size_boost = min(0.20, value_cr / 5000.0)
        return round(min(1.0, base + size_boost), 3)
    return base


class TenderIntelligence:
    """Layer 8: Fetch and classify India government tender data.

    Live fetching is intentionally lightweight — we parse tender titles and
    metadata from public API endpoints / RSS feeds, not full PDF documents.
    The tender title alone contains enough sector + quantity information for
    demand signal generation.
    """

    def __init__(self, config: dict = None):
        self._cfg = config or {}
        self._timeout = self._cfg.get("request_timeout_seconds", 20)

    def parse_tender_feed(
        self,
        records: list[dict],
        source: str,
    ) -> list[TenderSignal]:
        """Parse a list of raw tender record dicts into TenderSignal objects.

        Each record dict should have at minimum: title, tender_id (or id), url.
        Optional: tender_date, deadline, estimated_value.
        """
        signals: list[TenderSignal] = []
        for rec in records:
            title = rec.get("title") or rec.get("work_title") or rec.get("name") or ""
            if not title:
                continue
            tender_id = str(rec.get("tender_id") or rec.get("bid_number") or rec.get("id") or "")
            url = rec.get("url") or rec.get("link") or ""

            sector, tags = _classify_tender(title)
            value = _extract_value(title) or _extract_value(rec.get("estimated_value", ""))
            qty, unit = _extract_quantity(title)
            strength = _demand_signal_strength(sector, value)

            tender_date = self._parse_date(rec.get("tender_date") or rec.get("published_date"))
            deadline    = self._parse_date(rec.get("deadline") or rec.get("bid_end_date"))

            signals.append(TenderSignal(
                source=source,
                tender_id=tender_id,
                title=title[:500],
                sector=sector,
                quantity=qty,
                unit=unit,
                estimated_value_inr_cr=value,
                tender_date=tender_date,
                deadline=deadline,
                component_tags=tags,
                demand_signal_strength=strength,
                url=url,
            ))
        logger.info(f"[TenderIntelligence] Parsed {len(signals)} tender signals from '{source}'")
        return signals

    def _parse_date(self, raw) -> Optional[date]:
        if not raw:
            return None
        if isinstance(raw, date):
            return raw
        if isinstance(raw, datetime):
            return raw.date()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(str(raw)[:10], fmt).date()
            except ValueError:
                pass
        return None

    def get_aggregated_demand(
        self, signals: list[TenderSignal], sector: str = None
    ) -> dict[str, float]:
        """Aggregate tender demand by sector → total estimated value (INR Cr)."""
        agg: dict[str, float] = {}
        for s in signals:
            if sector and s.sector != sector:
                continue
            agg[s.sector] = agg.get(s.sector, 0.0) + (s.estimated_value_inr_cr or 0.0)
        return agg

    def persist(self, signals: list[TenderSignal], pg_store) -> int:
        self._ensure_schema(pg_store)
        saved = 0
        today = date.today()
        for s in signals:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO mg_tender_signals
                                (source, tender_id, title, sector, quantity, unit,
                                 estimated_value_inr_cr, tender_date, deadline,
                                 component_tags, demand_signal_strength, url,
                                 ingested_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (source, tender_id)
                            DO UPDATE SET
                                demand_signal_strength = EXCLUDED.demand_signal_strength,
                                estimated_value_inr_cr = EXCLUDED.estimated_value_inr_cr,
                                updated_at             = NOW()
                            """,
                            (s.source, s.tender_id or "unknown", s.title, s.sector,
                             s.quantity, s.unit, s.estimated_value_inr_cr,
                             s.tender_date, s.deadline,
                             ",".join(s.component_tags),
                             s.demand_signal_strength, s.url, today),
                        )
                saved += 1
            except Exception as e:
                logger.warning(f"[TenderIntelligence] persist failed {s.tender_id}: {e}")
        return saved

    def load_recent(
        self, pg_store, sector: str = None, days: int = 90
    ) -> list[TenderSignal]:
        """Load recent tender signals from DB."""
        self._ensure_schema(pg_store)
        floor = date.today() - timedelta(days=days)
        signals: list[TenderSignal] = []
        try:
            from psycopg2.extras import RealDictCursor
            with pg_store._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if sector:
                        cur.execute("""
                            SELECT * FROM mg_tender_signals
                            WHERE sector = %s AND ingested_at >= %s
                            ORDER BY demand_signal_strength DESC
                        """, (sector, floor))
                    else:
                        cur.execute("""
                            SELECT * FROM mg_tender_signals
                            WHERE ingested_at >= %s
                            ORDER BY demand_signal_strength DESC
                        """, (floor,))
                    for row in cur.fetchall():
                        tags = (row.get("component_tags") or "").split(",")
                        signals.append(TenderSignal(
                            source=row["source"],
                            tender_id=row["tender_id"],
                            title=row["title"],
                            sector=row["sector"],
                            quantity=row.get("quantity"),
                            unit=row.get("unit"),
                            estimated_value_inr_cr=row.get("estimated_value_inr_cr"),
                            tender_date=row.get("tender_date"),
                            deadline=row.get("deadline"),
                            component_tags=[t for t in tags if t],
                            demand_signal_strength=float(row.get("demand_signal_strength") or 0),
                            url=row.get("url") or "",
                        ))
        except Exception as e:
            logger.warning(f"[TenderIntelligence] load_recent failed: {e}")
        return signals

    def _ensure_schema(self, pg_store):
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS mg_tender_signals (
                            id                      SERIAL PRIMARY KEY,
                            source                  TEXT NOT NULL,
                            tender_id               TEXT NOT NULL,
                            title                   TEXT,
                            sector                  TEXT,
                            quantity                NUMERIC,
                            unit                    TEXT,
                            estimated_value_inr_cr  NUMERIC,
                            tender_date             DATE,
                            deadline                DATE,
                            component_tags          TEXT,
                            demand_signal_strength  NUMERIC,
                            url                     TEXT,
                            ingested_at             DATE,
                            created_at              TIMESTAMPTZ DEFAULT NOW(),
                            updated_at              TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE (source, tender_id)
                        )
                    """)
        except Exception as e:
            logger.debug(f"[TenderIntelligence] schema check: {e}")
