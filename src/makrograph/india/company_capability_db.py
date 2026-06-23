"""Company Capability Intelligence Database (Change 1 — Both US and India).

Maps companies to the specific products / technologies / capabilities they
manufacture or provide.  This is especially important for India where product-
level mapping is much more granular than sector-level classification.

Usage in BeneficiaryMapper:
    db = CompanyCapabilityDB()
    capable = db.get_capable_companies("solar_cell")    # → [Adani Solar, ...]
    themes  = db.get_themes_for_company("Dixon Technologies")  # → [electronics_manufacturing, ...]

The knowledge base is static but additive — new entries can be appended
without touching any pipeline logic.  The DB uses capability_key strings
that align with supply_chain_db.py sector names for cross-referencing.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CompanyCapability:
    """One product/capability record for a company."""
    company_name: str                # canonical company name
    ticker: str                      # NSE/BSE ticker (or empty for private)
    market: str                      # "IN" or "US"
    capability_keys: list[str]       # e.g. ["solar_cell", "solar_module"]
    products: list[str]              # human-readable product list
    supply_chain_stage: str          # "manufacturer" | "assembler" | "component" | "material"
    is_listed: bool = True
    confidence: float = 0.90         # data confidence


# ---------------------------------------------------------------------------
# India Company → Capability mapping
# ---------------------------------------------------------------------------

_INDIA_CAPABILITIES: list[dict] = [

    # ── Solar ─────────────────────────────────────────────────────────────
    {"company": "Adani Solar", "ticker": "ADANIENT", "market": "IN",
     "keys": ["solar_cell", "solar_module", "solar_wafer"],
     "products": ["Solar cells", "Solar modules", "Solar wafers"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "Waaree Energies", "ticker": "WAAREEENER", "market": "IN",
     "keys": ["solar_module", "solar_cell"],
     "products": ["Solar modules", "Solar cells"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "Vikram Solar", "ticker": "", "market": "IN",
     "keys": ["solar_module", "solar_cell"],
     "products": ["Solar modules", "Solar cells"],
     "stage": "manufacturer", "confidence": 0.90},

    {"company": "Premier Energies", "ticker": "PREMIERENE", "market": "IN",
     "keys": ["solar_cell", "solar_module"],
     "products": ["Solar cells", "Solar modules"],
     "stage": "manufacturer", "confidence": 0.90},

    {"company": "Borosil Renewables", "ticker": "BORORENEW", "market": "IN",
     "keys": ["solar_glass"],
     "products": ["Solar glass / anti-reflective glass"],
     "stage": "component", "confidence": 0.95},

    {"company": "Inox Wind", "ticker": "INOXWIND", "market": "IN",
     "keys": ["wind_turbines_units"],
     "products": ["Wind turbine blades", "Wind turbines"],
     "stage": "manufacturer", "confidence": 0.92},

    {"company": "Suzlon Energy", "ticker": "SUZLON", "market": "IN",
     "keys": ["wind_turbines_units"],
     "products": ["Wind turbines"],
     "stage": "manufacturer", "confidence": 0.95},

    # ── Power Transformers ────────────────────────────────────────────────
    {"company": "Hitachi Energy India", "ticker": "POWERINDIA", "market": "IN",
     "keys": ["power_transformers_units", "hvdc_cables_km"],
     "products": ["Power transformers", "HVDC equipment"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "CG Power and Industrial Solutions", "ticker": "CGPOWER", "market": "IN",
     "keys": ["power_transformers_units", "traction_motors_units"],
     "products": ["Power transformers", "Traction motors"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "Transformers and Rectifiers India", "ticker": "TARIL", "market": "IN",
     "keys": ["power_transformers_units"],
     "products": ["Power transformers"],
     "stage": "manufacturer", "confidence": 0.92},

    {"company": "Voltamp Transformers", "ticker": "VOLTAMP", "market": "IN",
     "keys": ["power_transformers_units"],
     "products": ["Power transformers", "Distribution transformers"],
     "stage": "manufacturer", "confidence": 0.90},

    {"company": "Indo Tech Transformers", "ticker": "INDOTECH", "market": "IN",
     "keys": ["power_transformers_units"],
     "products": ["Power transformers"],
     "stage": "manufacturer", "confidence": 0.85},

    # ── Electronics / EMS ─────────────────────────────────────────────────
    {"company": "Dixon Technologies", "ticker": "DIXON", "market": "IN",
     "keys": ["ems_capacity_bn_usd", "mobile_phones", "consumer_electronics"],
     "products": ["Consumer electronics EMS", "Mobile phone manufacturing", "LED TVs"],
     "stage": "assembler", "confidence": 0.95},

    {"company": "Amber Enterprises", "ticker": "AMBER", "market": "IN",
     "keys": ["ems_capacity_bn_usd", "consumer_electronics"],
     "products": ["Air conditioner components", "Consumer electronics EMS"],
     "stage": "assembler", "confidence": 0.92},

    {"company": "Kaynes Technology", "ticker": "KAYNES", "market": "IN",
     "keys": ["ems_capacity_bn_usd", "pcb_sqm_mn"],
     "products": ["Electronics manufacturing services", "PCB assembly"],
     "stage": "assembler", "confidence": 0.92},

    {"company": "Syrma SGS Technology", "ticker": "SYRMA", "market": "IN",
     "keys": ["ems_capacity_bn_usd", "pcb_sqm_mn"],
     "products": ["EMS", "PCB assembly", "RFID"],
     "stage": "assembler", "confidence": 0.90},

    # ── Cables & Conductors ───────────────────────────────────────────────
    {"company": "Polycab India", "ticker": "POLYCAB", "market": "IN",
     "keys": ["copper_conductor_mt", "hvdc_cables_km", "copper_ohe_mt"],
     "products": ["Electrical cables", "Wires", "Copper conductors"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "KEI Industries", "ticker": "KEI", "market": "IN",
     "keys": ["hvdc_cables_km", "copper_conductor_mt"],
     "products": ["HV cables", "Extra high voltage cables"],
     "stage": "manufacturer", "confidence": 0.93},

    {"company": "Sterlite Technologies", "ticker": "STLTECH", "market": "IN",
     "keys": ["optical_fiber_km_mn"],
     "products": ["Optical fiber", "Optical fiber cables"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "HFCL", "ticker": "HFCL", "market": "IN",
     "keys": ["optical_fiber_km_mn", "5g_bts"],
     "products": ["Optical fiber cables", "Telecom equipment"],
     "stage": "manufacturer", "confidence": 0.90},

    # ── Railways ──────────────────────────────────────────────────────────
    {"company": "Titagarh Wagons", "ticker": "TWL", "market": "IN",
     "keys": ["rolling_stock"],
     "products": ["Rail wagons", "Metro coaches"],
     "stage": "manufacturer", "confidence": 0.92},

    {"company": "Jupiter Wagons", "ticker": "JWL", "market": "IN",
     "keys": ["rolling_stock"],
     "products": ["Rail wagons"],
     "stage": "manufacturer", "confidence": 0.90},

    {"company": "BEML", "ticker": "BEML", "market": "IN",
     "keys": ["rolling_stock"],
     "products": ["Metro rail coaches", "Defense vehicles", "Earth moving equipment"],
     "stage": "manufacturer", "confidence": 0.90},

    # ── Defense ───────────────────────────────────────────────────────────
    {"company": "Bharat Electronics", "ticker": "BEL", "market": "IN",
     "keys": ["defense_electronics", "radar_systems"],
     "products": ["Radar systems", "Defense electronics", "Avionics"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "Hindustan Aeronautics", "ticker": "HAL", "market": "IN",
     "keys": ["aerospace", "defense_aircraft"],
     "products": ["Aircraft manufacturing", "Helicopter manufacturing", "Aircraft MRO"],
     "stage": "manufacturer", "confidence": 0.95},

    {"company": "Data Patterns India", "ticker": "DATAPATTNS", "market": "IN",
     "keys": ["defense_electronics", "radar_systems"],
     "products": ["Defense electronics", "Radar subsystems"],
     "stage": "manufacturer", "confidence": 0.90},

    # ── Battery / EV ──────────────────────────────────────────────────────
    {"company": "Exide Industries", "ticker": "EXIDEIND", "market": "IN",
     "keys": ["battery_cells_gwh", "ev_battery_pack"],
     "products": ["Lead-acid batteries", "Lithium-ion cells (planned)"],
     "stage": "manufacturer", "confidence": 0.88},

    {"company": "Amara Raja Energy & Mobility", "ticker": "AMARAJABAT", "market": "IN",
     "keys": ["battery_cells_gwh", "ev_battery_pack"],
     "products": ["Lead-acid batteries", "Li-ion cell (giga plant planned)"],
     "stage": "manufacturer", "confidence": 0.88},

    {"company": "Ola Electric", "ticker": "OLAELEC", "market": "IN",
     "keys": ["ev_battery_pack", "electric_vehicle"],
     "products": ["EV scooters", "Lithium cells (gigafactory)"],
     "stage": "manufacturer", "confidence": 0.87},

    # ── Specialty Chemicals ───────────────────────────────────────────────
    {"company": "PI Industries", "ticker": "PIIND", "market": "IN",
     "keys": ["specialty_chemicals", "agrochemicals"],
     "products": ["Agrochemical intermediates", "CRAMS"],
     "stage": "manufacturer", "confidence": 0.93},

    {"company": "Aarti Industries", "ticker": "AARTIIND", "market": "IN",
     "keys": ["specialty_chemicals"],
     "products": ["Benzene-based specialty chemicals", "Pharma intermediates"],
     "stage": "manufacturer", "confidence": 0.92},

    {"company": "Navin Fluorine", "ticker": "NAVINFLUOR", "market": "IN",
     "keys": ["specialty_chemicals", "fluorine_chemicals"],
     "products": ["Fluorochemicals", "Specialty fluorides"],
     "stage": "manufacturer", "confidence": 0.92},
]

# ---------------------------------------------------------------------------
# US Company → Capability mapping
# ---------------------------------------------------------------------------

_US_CAPABILITIES: list[dict] = [

    # ── Semiconductors ────────────────────────────────────────────────────
    {"company": "NVIDIA", "ticker": "NVDA", "market": "US",
     "keys": ["gpu", "ai_accelerator", "semiconductor"],
     "products": ["GPUs", "AI accelerators", "Networking chips"],
     "stage": "designer", "confidence": 0.99},

    {"company": "TSMC", "ticker": "TSM", "market": "US",
     "keys": ["semiconductor_wafer", "foundry"],
     "products": ["Semiconductor foundry", "Advanced process nodes"],
     "stage": "manufacturer", "confidence": 0.99},

    {"company": "ASML", "ticker": "ASML", "market": "US",
     "keys": ["lithography", "semiconductor_equipment"],
     "products": ["EUV lithography machines", "DUV lithography"],
     "stage": "equipment", "confidence": 0.99},

    # ── Data Center / AI Infrastructure ───────────────────────────────────
    {"company": "Vertiv Holdings", "ticker": "VRT", "market": "US",
     "keys": ["data_center_cooling", "ups_power_mw"],
     "products": ["Data center cooling", "UPS systems", "Power infrastructure"],
     "stage": "component", "confidence": 0.95},

    {"company": "Eaton Corporation", "ticker": "ETN", "market": "US",
     "keys": ["ups_power_mw", "power_distribution"],
     "products": ["Power management", "UPS", "Circuit breakers"],
     "stage": "component", "confidence": 0.95},

    # ── Power Transformers (US) ───────────────────────────────────────────
    {"company": "GE Vernova", "ticker": "GEV", "market": "US",
     "keys": ["power_transformers_units", "wind_turbines_units"],
     "products": ["Power transformers", "Wind turbines", "Grid equipment"],
     "stage": "manufacturer", "confidence": 0.95},
]


# ---------------------------------------------------------------------------
# CompanyCapabilityDB — unified interface
# ---------------------------------------------------------------------------

_ALL_CAPABILITIES: list[CompanyCapability] = []
for _entry in _INDIA_CAPABILITIES + _US_CAPABILITIES:
    _ALL_CAPABILITIES.append(CompanyCapability(
        company_name=_entry["company"],
        ticker=_entry.get("ticker", ""),
        market=_entry["market"],
        capability_keys=_entry["keys"],
        products=_entry["products"],
        supply_chain_stage=_entry["stage"],
        confidence=_entry.get("confidence", 0.90),
    ))

# Build reverse index: capability_key → list[CompanyCapability]
_CAPABILITY_INDEX: dict[str, list[CompanyCapability]] = {}
for _cap in _ALL_CAPABILITIES:
    for _key in _cap.capability_keys:
        _CAPABILITY_INDEX.setdefault(_key.lower(), []).append(_cap)

# Company name → list[CompanyCapability] (case-insensitive)
_COMPANY_INDEX: dict[str, list[CompanyCapability]] = {}
for _cap in _ALL_CAPABILITIES:
    _COMPANY_INDEX.setdefault(_cap.company_name.lower(), []).append(_cap)


class CompanyCapabilityDB:
    """Query company ↔ product/capability knowledge base (Change 1).

    Integrated into BeneficiaryMapper to boost relevance scores for companies
    that have documented manufacturing capability in the theme's supply chain.
    """

    def get_capable_companies(
        self,
        capability_key: str,
        market: str = None,
    ) -> list[CompanyCapability]:
        """Return all companies with a given capability key.

        Args:
            capability_key: e.g. "solar_cell", "power_transformers_units"
            market: optional filter ("IN" or "US")
        """
        caps = _CAPABILITY_INDEX.get(capability_key.lower(), [])
        if market:
            caps = [c for c in caps if c.market == market]
        return caps

    def get_capable_company_names(
        self,
        capability_key: str,
        market: str = None,
    ) -> set[str]:
        """Return a set of company names (lower-case) for fast membership checks."""
        return {c.company_name.lower() for c in self.get_capable_companies(capability_key, market)}

    def get_themes_for_company(
        self,
        company_name: str,
    ) -> list[str]:
        """Return all capability keys (theme proxies) for a given company."""
        caps = _COMPANY_INDEX.get(company_name.lower(), [])
        keys: list[str] = []
        for cap in caps:
            keys.extend(cap.capability_keys)
        return list(set(keys))

    def has_capability(self, company_name: str, capability_key: str) -> bool:
        """Return True if the company has the given capability."""
        return capability_key.lower() in {
            k.lower()
            for cap in _COMPANY_INDEX.get(company_name.lower(), [])
            for k in cap.capability_keys
        }

    def get_capability_boost(
        self,
        company_name: str,
        theme_keywords: list[str],
    ) -> float:
        """Return a relevance score boost (0–25) based on documented capability match.

        Used by BeneficiaryMapper to reward companies with proven product-level fit.
        """
        if not company_name or not theme_keywords:
            return 0.0
        caps = _COMPANY_INDEX.get(company_name.lower(), [])
        if not caps:
            return 0.0
        # Check if any capability key overlaps with theme keywords (stem matching)
        all_keys = {k.lower() for cap in caps for k in cap.capability_keys}
        kw_lower = [k.lower() for k in theme_keywords]
        match_count = sum(
            1 for kw in kw_lower
            if any(kw in key or key in kw for key in all_keys)
        )
        if match_count == 0:
            return 0.0
        # Boost scales with match count and capability confidence
        max_confidence = max(cap.confidence for cap in caps)
        return round(min(match_count * 8.0 * max_confidence, 25.0), 2)

    def all_companies(self, market: str = None) -> list[CompanyCapability]:
        caps = _ALL_CAPABILITIES
        if market:
            caps = [c for c in caps if c.market == market]
        return caps
