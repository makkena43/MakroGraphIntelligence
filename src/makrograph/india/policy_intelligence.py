"""Policy Intelligence Engine (India Pipeline — Layer 1).

Sources:
  - PLI (Production Linked Incentive) scheme announcements
  - Union Budget press releases
  - Economic Survey chapters
  - NITI Aayog documents
  - RBI monetary policy and sector reports
  - MNRE (Ministry of New and Renewable Energy) targets
  - Power Ministry capacity targets
  - Railways capex plans
  - DPIIT sector reports
  - Sector ministries (Telecom, Steel, Chemicals, Electronics)

Output: structured PolicyTarget records stored to mg_policy_targets.
These feed directly into CapacityRequirementGenerator (Layer 2).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PolicyTarget:
    """A quantified government target extracted from a policy document."""
    source: str                  # e.g. "MNRE", "Union Budget 2024", "PLI Electronics"
    sector: str                  # e.g. "solar", "semiconductor", "railway"
    metric: str                  # e.g. "installed_capacity_gw", "production_target_bn_usd"
    value: float                 # numeric value
    unit: str                    # e.g. "GW", "GW-hours", "INR Crore", "USD Billion"
    target_year: Optional[int]   # FY or CY target year
    confidence: float = 0.8
    raw_text: str = ""
    doc_url: str = ""
    extracted_at: Optional[date] = None


# ---------------------------------------------------------------------------
# Static policy target knowledge base
# ---------------------------------------------------------------------------
# These are well-known, published government targets that form the backbone
# of India's policy intelligence.  Real-time extraction from PIB/MNRE pages
# supplements this base — see fetch_live_targets().

_STATIC_POLICY_TARGETS: list[dict] = [
    # Renewable Energy
    {"source": "MNRE", "sector": "solar", "metric": "installed_capacity_gw",
     "value": 280.0, "unit": "GW", "target_year": 2030, "confidence": 0.95},
    {"source": "MNRE", "sector": "wind", "metric": "installed_capacity_gw",
     "value": 140.0, "unit": "GW", "target_year": 2030, "confidence": 0.95},
    {"source": "MNRE", "sector": "renewable_energy", "metric": "total_installed_capacity_gw",
     "value": 500.0, "unit": "GW", "target_year": 2030, "confidence": 0.98},
    {"source": "MNRE", "sector": "green_hydrogen", "metric": "production_mmtpa",
     "value": 5.0, "unit": "MMTPA", "target_year": 2030, "confidence": 0.90},

    # Power & Transmission
    {"source": "Power Ministry", "sector": "power_transmission", "metric": "transmission_addition_km",
     "value": 50000.0, "unit": "circuit-km", "target_year": 2027, "confidence": 0.85},
    {"source": "Power Ministry", "sector": "power_distribution", "metric": "smart_meter_rollout_mn",
     "value": 250.0, "unit": "million meters", "target_year": 2026, "confidence": 0.82},

    # Electronics & Semiconductor
    {"source": "DPIIT", "sector": "semiconductor", "metric": "fab_capacity_wafers_per_month",
     "value": 50000.0, "unit": "wafers/month", "target_year": 2027, "confidence": 0.80},
    {"source": "PLI Electronics", "sector": "electronics_manufacturing",
     "metric": "production_target_bn_usd", "value": 300.0, "unit": "USD Billion",
     "target_year": 2026, "confidence": 0.88},
    {"source": "PLI Mobile", "sector": "mobile_phones", "metric": "production_target_bn_usd",
     "value": 160.0, "unit": "USD Billion", "target_year": 2026, "confidence": 0.88},

    # Railways
    {"source": "Railways Ministry", "sector": "railway_infrastructure",
     "metric": "capex_inr_crore", "value": 240000.0, "unit": "INR Crore",
     "target_year": 2024, "confidence": 0.92},
    {"source": "Railways Ministry", "sector": "dedicated_freight_corridor",
     "metric": "track_km", "value": 3000.0, "unit": "km", "target_year": 2025,
     "confidence": 0.90},
    {"source": "Railways Ministry", "sector": "railway_electrification",
     "metric": "track_electrification_km", "value": 100000.0, "unit": "km",
     "target_year": 2024, "confidence": 0.93},

    # EV & Battery
    {"source": "FAME-II", "sector": "electric_vehicle",
     "metric": "ev_target_mn_units", "value": 30.0, "unit": "million units",
     "target_year": 2030, "confidence": 0.85},
    {"source": "PLI ACC Battery", "sector": "battery_storage",
     "metric": "acc_capacity_gwh", "value": 50.0, "unit": "GWh",
     "target_year": 2026, "confidence": 0.88},

    # Defense
    {"source": "MoD", "sector": "defense_electronics",
     "metric": "domestic_procurement_inr_crore", "value": 175000.0,
     "unit": "INR Crore", "target_year": 2025, "confidence": 0.87},

    # Telecom
    {"source": "DoT", "sector": "5g_telecom", "metric": "bts_tower_target",
     "value": 500000.0, "unit": "towers", "target_year": 2025, "confidence": 0.83},

    # Data Centers
    {"source": "DPIIT", "sector": "data_center", "metric": "capacity_mw",
     "value": 1000.0, "unit": "MW", "target_year": 2027, "confidence": 0.75},

    # Specialty Chemicals / Chemical PLI
    {"source": "PLI Chemicals", "sector": "specialty_chemicals",
     "metric": "production_target_inr_crore", "value": 62000.0,
     "unit": "INR Crore", "target_year": 2028, "confidence": 0.80},

    # Water
    {"source": "Jal Shakti Ministry", "sector": "water_infrastructure",
     "metric": "tap_connections_mn", "value": 192.0, "unit": "million",
     "target_year": 2024, "confidence": 0.90},
]

# ---------------------------------------------------------------------------
# Regex patterns for extracting targets from policy text
# ---------------------------------------------------------------------------

_TARGET_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # e.g. "500 GW renewable energy by 2030"
    ("renewable_energy", "installed_capacity_gw",
     re.compile(r"(\d[\d,\.]*)\s*GW\b.{0,60}(?:renewable|solar|wind|clean\s+energy)", re.I)),
    ("solar", "installed_capacity_gw",
     re.compile(r"(\d[\d,\.]*)\s*GW\b.{0,40}solar", re.I)),
    ("wind", "installed_capacity_gw",
     re.compile(r"(\d[\d,\.]*)\s*GW\b.{0,40}wind", re.I)),
    ("power_transmission", "transmission_addition_km",
     re.compile(r"(\d[\d,\.]*)\s*(?:circuit\s*)?km\b.{0,60}transmission", re.I)),
    ("electronics_manufacturing", "production_target_bn_usd",
     re.compile(r"USD?\s*(\d[\d,\.]*)\s*[Bb]illion.{0,60}electronics", re.I)),
    ("semiconductor", "fab_units",
     re.compile(r"(\d[\d,\.]*)\s*(?:semiconductor\s+)?fab", re.I)),
    ("railway_infrastructure", "capex_inr_crore",
     re.compile(r"(?:Rs\.?|INR|₹)\s*(\d[\d,\.]*)\s*(?:crore|lakh\s+crore).{0,60}rail", re.I)),
    ("battery_storage", "acc_capacity_gwh",
     re.compile(r"(\d[\d,\.]*)\s*GWh\b.{0,60}(?:battery|ACC|storage)", re.I)),
    ("green_hydrogen", "production_mmtpa",
     re.compile(r"(\d[\d,\.]*)\s*(?:MMTPA|MT\s+per\s+year).{0,60}(?:green\s+)?hydrogen", re.I)),
]


class PolicyIntelligenceEngine:
    """Layer 1: Extract and store government policy targets for India.

    Usage::

        engine = PolicyIntelligenceEngine(config)
        targets = engine.get_targets()          # static + DB
        engine.persist(targets, pg_store)       # write to mg_policy_targets
    """

    def __init__(self, config: dict = None):
        self._cfg = config or {}

    def get_static_targets(self) -> list[PolicyTarget]:
        """Return the built-in static policy target knowledge base."""
        today = date.today()
        return [
            PolicyTarget(
                source=t["source"],
                sector=t["sector"],
                metric=t["metric"],
                value=t["value"],
                unit=t["unit"],
                target_year=t.get("target_year"),
                confidence=t.get("confidence", 0.80),
                extracted_at=today,
            )
            for t in _STATIC_POLICY_TARGETS
        ]

    def extract_from_text(self, text: str, source: str = "unknown") -> list[PolicyTarget]:
        """Extract policy targets from raw policy document text using regex patterns."""
        targets: list[PolicyTarget] = []
        today = date.today()

        for sector, metric, pattern in _TARGET_PATTERNS:
            for match in pattern.finditer(text):
                raw_val = match.group(1).replace(",", "")
                try:
                    value = float(raw_val)
                except ValueError:
                    continue
                # Try to extract a target year from nearby text
                year = self._extract_year(text, match.start())
                targets.append(PolicyTarget(
                    source=source,
                    sector=sector,
                    metric=metric,
                    value=value,
                    unit=self._infer_unit(metric),
                    target_year=year,
                    confidence=0.70,
                    raw_text=match.group(0)[:200],
                    extracted_at=today,
                ))

        logger.info(f"[PolicyIntelligence] Extracted {len(targets)} targets from '{source}' text")
        return targets

    def _extract_year(self, text: str, pos: int) -> Optional[int]:
        window = text[max(0, pos - 100): pos + 200]
        m = re.search(r"\b(20[2-4]\d)\b", window)
        if m:
            return int(m.group(1))
        m = re.search(r"\bFY\s*(\d{2,4})\b", window, re.I)
        if m:
            yr = int(m.group(1))
            return yr if yr > 100 else 2000 + yr
        return None

    def _infer_unit(self, metric: str) -> str:
        if "gw" in metric:
            return "GW"
        if "gwh" in metric:
            return "GWh"
        if "km" in metric:
            return "km"
        if "bn_usd" in metric or "billion" in metric:
            return "USD Billion"
        if "crore" in metric:
            return "INR Crore"
        if "mmtpa" in metric:
            return "MMTPA"
        return "units"

    def persist(self, targets: list[PolicyTarget], pg_store) -> int:
        """Upsert policy targets into mg_policy_targets table."""
        self._ensure_schema(pg_store)
        saved = 0
        for t in targets:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO mg_policy_targets
                                (source, sector, metric, value, unit, target_year,
                                 confidence, raw_text, doc_url, extracted_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (source, sector, metric, target_year)
                            DO UPDATE SET
                                value        = EXCLUDED.value,
                                confidence   = EXCLUDED.confidence,
                                raw_text     = EXCLUDED.raw_text,
                                extracted_at = EXCLUDED.extracted_at,
                                updated_at   = NOW()
                            """,
                            (t.source, t.sector, t.metric, t.value, t.unit,
                             t.target_year, t.confidence, t.raw_text[:500] if t.raw_text else "",
                             t.doc_url, t.extracted_at or date.today()),
                        )
                saved += 1
            except Exception as e:
                logger.warning(f"[PolicyIntelligence] persist failed for {t.source}/{t.sector}: {e}")
        logger.info(f"[PolicyIntelligence] Persisted {saved}/{len(targets)} policy targets")
        return saved

    def load_from_db(self, pg_store, sector: str = None) -> list[PolicyTarget]:
        """Load policy targets from mg_policy_targets, optionally filtered by sector."""
        self._ensure_schema(pg_store)
        targets: list[PolicyTarget] = []
        try:
            with pg_store._conn() as conn:
                from psycopg2.extras import RealDictCursor
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if sector:
                        cur.execute(
                            "SELECT * FROM mg_policy_targets WHERE sector = %s ORDER BY target_year",
                            (sector,),
                        )
                    else:
                        cur.execute("SELECT * FROM mg_policy_targets ORDER BY sector, target_year")
                    for row in cur.fetchall():
                        targets.append(PolicyTarget(
                            source=row["source"],
                            sector=row["sector"],
                            metric=row["metric"],
                            value=float(row["value"]),
                            unit=row["unit"],
                            target_year=row.get("target_year"),
                            confidence=float(row.get("confidence", 0.8)),
                            raw_text=row.get("raw_text", ""),
                            doc_url=row.get("doc_url", ""),
                            extracted_at=row.get("extracted_at"),
                        ))
        except Exception as e:
            logger.warning(f"[PolicyIntelligence] load_from_db failed: {e}")
        return targets

    def _ensure_schema(self, pg_store):
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS mg_policy_targets (
                            id            SERIAL PRIMARY KEY,
                            source        TEXT NOT NULL,
                            sector        TEXT NOT NULL,
                            metric        TEXT NOT NULL,
                            value         NUMERIC NOT NULL,
                            unit          TEXT,
                            target_year   INTEGER,
                            confidence    NUMERIC DEFAULT 0.8,
                            raw_text      TEXT,
                            doc_url       TEXT,
                            extracted_at  DATE,
                            created_at    TIMESTAMPTZ DEFAULT NOW(),
                            updated_at    TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE (source, sector, metric, target_year)
                        )
                    """)
        except Exception as e:
            logger.debug(f"[PolicyIntelligence] schema check: {e}")
