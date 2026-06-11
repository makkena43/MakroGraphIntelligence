"""Capacity Requirement Generator + Capacity Gap Detector (India Pipeline — Layers 2 & 3).

Layer 2 — CapacityRequirementGenerator:
    Converts policy targets into required upstream capacity across the full
    supply chain (modules, cells, wafers, transformers, cables, EMS, PCB,
    batteries, etc.).

Layer 3 — CapacityGapDetector:
    Computes demand (from policy targets) versus existing domestic capacity.
    Generates investable themes like 'Transformer Capacity Gap' or
    'Solar Wafer Shortage' instead of generic sector-growth themes.

Output: CapacityGap records stored to mg_capacity_gaps.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .policy_intelligence import PolicyTarget

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sector → upstream component requirements
# ---------------------------------------------------------------------------
# For each GW / unit of policy target, how much upstream capacity is needed?
# Values are illustrative engineering ratios used for gap estimation.

_CAPACITY_CONVERSION: dict[str, list[dict]] = {
    "solar": [
        {"component": "solar_modules_gw",   "ratio": 1.05, "unit": "GW",   "supply_chain_stage": "module"},
        {"component": "solar_cells_gw",     "ratio": 1.08, "unit": "GW",   "supply_chain_stage": "cell"},
        {"component": "solar_wafers_gw",    "ratio": 1.12, "unit": "GW",   "supply_chain_stage": "wafer"},
        {"component": "polysilicon_mt",      "ratio": 2800, "unit": "MT/GW", "supply_chain_stage": "polysilicon"},
        {"component": "solar_glass_mn_sqm",  "ratio": 6.0,  "unit": "mn sqm/GW", "supply_chain_stage": "material"},
        {"component": "inverters_gw",        "ratio": 1.0,  "unit": "GW",   "supply_chain_stage": "equipment"},
    ],
    "wind": [
        {"component": "wind_turbines_units",   "ratio": 500.0,  "unit": "turbines/GW", "supply_chain_stage": "turbine"},
        {"component": "steel_for_towers_mt",   "ratio": 130000, "unit": "MT/GW",       "supply_chain_stage": "material"},
        {"component": "copper_winding_mt",     "ratio": 600,    "unit": "MT/GW",       "supply_chain_stage": "material"},
        {"component": "gearboxes_units",       "ratio": 500,    "unit": "units/GW",    "supply_chain_stage": "component"},
    ],
    "power_transmission": [
        {"component": "power_transformers_units", "ratio": 0.02,  "unit": "units/km",   "supply_chain_stage": "transformer"},
        {"component": "crgo_steel_mt",            "ratio": 5.0,   "unit": "MT/unit",    "supply_chain_stage": "material"},
        {"component": "copper_conductor_mt",      "ratio": 8.0,   "unit": "MT/km",      "supply_chain_stage": "conductor"},
        {"component": "hvdc_cables_km",           "ratio": 0.30,  "unit": "km HVDC/km", "supply_chain_stage": "cable"},
    ],
    "railway_infrastructure": [
        {"component": "rail_steel_mt",       "ratio": 120,   "unit": "MT/km",      "supply_chain_stage": "material"},
        {"component": "traction_motors_units", "ratio": 0.5, "unit": "units/km",   "supply_chain_stage": "equipment"},
        {"component": "signalling_systems",   "ratio": 0.01, "unit": "systems/km", "supply_chain_stage": "electronics"},
        {"component": "copper_ohe_mt",        "ratio": 15,   "unit": "MT/km",      "supply_chain_stage": "conductor"},
    ],
    "electronics_manufacturing": [
        {"component": "ems_capacity_bn_usd",  "ratio": 0.40, "unit": "USD Bn/Bn", "supply_chain_stage": "EMS"},
        {"component": "pcb_sqm_mn",           "ratio": 12.0, "unit": "mn sqm/Bn", "supply_chain_stage": "PCB"},
        {"component": "components_bn_usd",    "ratio": 0.35, "unit": "USD Bn/Bn", "supply_chain_stage": "component"},
    ],
    "battery_storage": [
        {"component": "battery_cells_gwh",    "ratio": 1.10, "unit": "GWh",        "supply_chain_stage": "cell"},
        {"component": "lithium_mt",           "ratio": 900,  "unit": "MT/GWh",     "supply_chain_stage": "material"},
        {"component": "cathode_material_mt",  "ratio": 1500, "unit": "MT/GWh",     "supply_chain_stage": "material"},
        {"component": "separator_mn_sqm",     "ratio": 15,   "unit": "mn sqm/GWh", "supply_chain_stage": "component"},
        {"component": "electrolyte_mt",       "ratio": 800,  "unit": "MT/GWh",     "supply_chain_stage": "material"},
    ],
    "data_center": [
        {"component": "ups_power_mw",         "ratio": 1.30, "unit": "MW UPS/MW",  "supply_chain_stage": "power"},
        {"component": "cooling_units_mw",     "ratio": 0.40, "unit": "MW cool/MW", "supply_chain_stage": "cooling"},
        {"component": "distribution_transformer_units", "ratio": 2.0, "unit": "units/MW", "supply_chain_stage": "transformer"},
    ],
    "5g_telecom": [
        {"component": "optical_fiber_km_mn",  "ratio": 3.0,  "unit": "mn km/mn towers", "supply_chain_stage": "fiber"},
        {"component": "radio_units",           "ratio": 3.0,  "unit": "radios/tower",    "supply_chain_stage": "antenna"},
        {"component": "power_backup_kwh",      "ratio": 10,   "unit": "kWh/tower",       "supply_chain_stage": "power"},
    ],
}

# Current domestic production capacity estimates (rough) — used to compute gaps.
# Sources: DPIIT, ministry reports, industry body estimates (as of 2024).
_DOMESTIC_CAPACITY: dict[str, dict] = {
    "solar_modules_gw":       {"capacity": 50.0,    "unit": "GW/year",    "as_of": 2024},
    "solar_cells_gw":         {"capacity": 6.0,     "unit": "GW/year",    "as_of": 2024},
    "solar_wafers_gw":        {"capacity": 0.5,     "unit": "GW/year",    "as_of": 2024},
    "polysilicon_mt":         {"capacity": 0.0,     "unit": "MT/year",    "as_of": 2024},
    "power_transformers_units": {"capacity": 25000, "unit": "units/year", "as_of": 2024},
    "crgo_steel_mt":          {"capacity": 30000,   "unit": "MT/year",    "as_of": 2024},
    "ems_capacity_bn_usd":    {"capacity": 15.0,    "unit": "USD Bn/yr",  "as_of": 2024},
    "pcb_sqm_mn":             {"capacity": 40.0,    "unit": "mn sqm/yr",  "as_of": 2024},
    "battery_cells_gwh":      {"capacity": 8.0,     "unit": "GWh/year",   "as_of": 2024},
    "lithium_mt":             {"capacity": 0.0,     "unit": "MT/year",    "as_of": 2024},
    "optical_fiber_km_mn":    {"capacity": 30.0,    "unit": "mn km/year", "as_of": 2024},
    "copper_conductor_mt":    {"capacity": 700000,  "unit": "MT/year",    "as_of": 2024},
    "rail_steel_mt":          {"capacity": 1200000, "unit": "MT/year",    "as_of": 2024},
}


@dataclass
class CapacityRequirement:
    sector: str
    component: str
    required_quantity: float
    unit: str
    supply_chain_stage: str
    source_target: str
    target_year: Optional[int]


@dataclass
class CapacityGap:
    sector: str
    component: str
    required_quantity: float
    domestic_capacity: float
    gap: float
    gap_pct: float
    unit: str
    supply_chain_stage: str
    theme_name: str              # investable theme name, e.g. "Transformer Capacity Gap"
    severity: str                # "critical" | "high" | "moderate" | "low"
    target_year: Optional[int]
    confidence: float = 0.75


class CapacityRequirementGenerator:
    """Layer 2: Convert policy targets to required upstream component capacity."""

    def generate(self, targets: list[PolicyTarget]) -> list[CapacityRequirement]:
        requirements: list[CapacityRequirement] = []
        for t in targets:
            conversions = _CAPACITY_CONVERSION.get(t.sector, [])
            if not conversions:
                continue
            # Policy value is in the sector's base unit (GW, USD Bn, etc.)
            base_value = t.value
            for conv in conversions:
                required = base_value * conv["ratio"]
                requirements.append(CapacityRequirement(
                    sector=t.sector,
                    component=conv["component"],
                    required_quantity=required,
                    unit=conv["unit"],
                    supply_chain_stage=conv["supply_chain_stage"],
                    source_target=f"{t.source} — {t.metric}",
                    target_year=t.target_year,
                ))
        logger.info(f"[CapacityRequirementGenerator] {len(requirements)} component requirements from {len(targets)} targets")
        return requirements


class CapacityGapDetector:
    """Layer 3: Compute demand vs domestic capacity and generate investable gap themes."""

    def detect(self, requirements: list[CapacityRequirement]) -> list[CapacityGap]:
        gaps: list[CapacityGap] = []
        for req in requirements:
            domestic = _DOMESTIC_CAPACITY.get(req.component)
            if domestic is None:
                continue  # no capacity data — can't compute gap

            dom_cap = domestic["capacity"]
            gap_qty = req.required_quantity - dom_cap
            if gap_qty <= 0:
                continue  # no gap — domestic capacity is sufficient

            gap_pct = (gap_qty / req.required_quantity) * 100 if req.required_quantity > 0 else 0
            severity = self._classify_severity(gap_pct)
            theme_name = self._build_theme_name(req.component, req.supply_chain_stage)

            gaps.append(CapacityGap(
                sector=req.sector,
                component=req.component,
                required_quantity=round(req.required_quantity, 2),
                domestic_capacity=round(dom_cap, 2),
                gap=round(gap_qty, 2),
                gap_pct=round(gap_pct, 1),
                unit=req.unit,
                supply_chain_stage=req.supply_chain_stage,
                theme_name=theme_name,
                severity=severity,
                target_year=req.target_year,
            ))

        # Sort by severity + gap_pct
        _sev_order = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
        gaps.sort(key=lambda g: (_sev_order.get(g.severity, 9), -g.gap_pct))
        logger.info(f"[CapacityGapDetector] {len(gaps)} capacity gaps detected")
        return gaps

    def _classify_severity(self, gap_pct: float) -> str:
        if gap_pct >= 80:
            return "critical"
        if gap_pct >= 60:
            return "high"
        if gap_pct >= 35:
            return "moderate"
        return "low"

    def _build_theme_name(self, component: str, stage: str) -> str:
        """Generate a human-readable investable theme name."""
        name_map = {
            "solar_wafers_gw":        "Solar Wafer Capacity Gap",
            "solar_cells_gw":         "Solar Cell Manufacturing Gap",
            "polysilicon_mt":         "Polysilicon Import Dependency",
            "power_transformers_units": "Transformer Capacity Gap",
            "crgo_steel_mt":          "CRGO Steel Shortage",
            "battery_cells_gwh":      "Battery Cell Manufacturing Gap",
            "lithium_mt":             "Lithium Supply Gap",
            "ems_capacity_bn_usd":    "EMS Capacity Shortage",
            "pcb_sqm_mn":             "PCB Manufacturing Gap",
            "optical_fiber_km_mn":    "Optical Fiber Supply Gap",
            "solar_modules_gw":       "Solar Module Overcapacity Risk",
        }
        if component in name_map:
            return name_map[component]
        # Generic fallback
        readable = component.replace("_", " ").title()
        return f"{readable} Capacity Gap"

    def persist(self, gaps: list[CapacityGap], pg_store) -> int:
        """Upsert capacity gaps into mg_capacity_gaps table."""
        self._ensure_schema(pg_store)
        saved = 0
        today = date.today()
        for g in gaps:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO mg_capacity_gaps
                                (sector, component, required_quantity, domestic_capacity,
                                 gap, gap_pct, unit, supply_chain_stage, theme_name,
                                 severity, target_year, confidence, as_of_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (component, target_year)
                            DO UPDATE SET
                                required_quantity = EXCLUDED.required_quantity,
                                gap               = EXCLUDED.gap,
                                gap_pct           = EXCLUDED.gap_pct,
                                severity          = EXCLUDED.severity,
                                theme_name        = EXCLUDED.theme_name,
                                as_of_date        = EXCLUDED.as_of_date,
                                updated_at        = NOW()
                            """,
                            (g.sector, g.component, g.required_quantity,
                             g.domestic_capacity, g.gap, g.gap_pct, g.unit,
                             g.supply_chain_stage, g.theme_name, g.severity,
                             g.target_year, g.confidence, today),
                        )
                saved += 1
            except Exception as e:
                logger.warning(f"[CapacityGapDetector] persist failed {g.component}: {e}")
        logger.info(f"[CapacityGapDetector] Persisted {saved}/{len(gaps)} capacity gaps")
        return saved

    def _ensure_schema(self, pg_store):
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS mg_capacity_gaps (
                            id                 SERIAL PRIMARY KEY,
                            sector             TEXT NOT NULL,
                            component          TEXT NOT NULL,
                            required_quantity  NUMERIC,
                            domestic_capacity  NUMERIC,
                            gap                NUMERIC,
                            gap_pct            NUMERIC,
                            unit               TEXT,
                            supply_chain_stage TEXT,
                            theme_name         TEXT,
                            severity           TEXT,
                            target_year        INTEGER,
                            confidence         NUMERIC DEFAULT 0.75,
                            as_of_date         DATE,
                            created_at         TIMESTAMPTZ DEFAULT NOW(),
                            updated_at         TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE (component, target_year)
                        )
                    """)
        except Exception as e:
            logger.debug(f"[CapacityGapDetector] schema check: {e}")
