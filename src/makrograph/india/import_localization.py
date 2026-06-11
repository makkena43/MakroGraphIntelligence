"""Import Dependency Engine + Localization Opportunity Engine (India Pipeline — Layers 4 & 5).

Layer 4 — ImportDependencyEngine:
    Tracks sectors where India relies heavily on imports.
    Sources: DGFT / Commerce Ministry trade data, DPIIT, sector reports.

Layer 5 — LocalizationOpportunityEngine:
    Combines import dependency with government incentives (PLI, customs duty,
    SPECS, DLI, etc.) to identify high-conviction localization opportunities.

Output: ImportDependency + LocalizationOpportunity records stored to DB.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static import dependency knowledge base
# ---------------------------------------------------------------------------
# import_share: fraction of domestic demand met by imports (0.0 – 1.0)
# import_value_bn_usd: annual import value
# primary_origin: top import source countries

_IMPORT_DEPENDENCY_DATA: list[dict] = [
    # Solar supply chain
    {"sector": "solar",       "component": "Solar Wafers",
     "import_share": 0.98, "import_value_bn_usd": 1.2,
     "primary_origin": ["China"],
     "hs_code": "8541",
     "substitute_possible": True, "substitution_horizon_years": 5},
    {"sector": "solar",       "component": "Solar Cells",
     "import_share": 0.75, "import_value_bn_usd": 2.1,
     "primary_origin": ["China", "Vietnam"],
     "hs_code": "8541",
     "substitute_possible": True, "substitution_horizon_years": 3},
    {"sector": "solar",       "component": "Polysilicon",
     "import_share": 1.00, "import_value_bn_usd": 0.6,
     "primary_origin": ["China", "Germany"],
     "hs_code": "2804",
     "substitute_possible": False, "substitution_horizon_years": 8},

    # Electronics / Semiconductor
    {"sector": "electronics_manufacturing", "component": "Semiconductor ICs",
     "import_share": 0.95, "import_value_bn_usd": 8.5,
     "primary_origin": ["China", "Taiwan", "South Korea", "USA"],
     "hs_code": "8542",
     "substitute_possible": True, "substitution_horizon_years": 7},
    {"sector": "electronics_manufacturing", "component": "Printed Circuit Boards",
     "import_share": 0.80, "import_value_bn_usd": 3.2,
     "primary_origin": ["China", "Taiwan"],
     "hs_code": "8534",
     "substitute_possible": True, "substitution_horizon_years": 3},
    {"sector": "electronics_manufacturing", "component": "Display Panels",
     "import_share": 0.92, "import_value_bn_usd": 6.5,
     "primary_origin": ["China", "South Korea", "Taiwan"],
     "hs_code": "8524",
     "substitute_possible": True, "substitution_horizon_years": 5},
    {"sector": "electronics_manufacturing", "component": "Passive Components (MLCCs, resistors)",
     "import_share": 0.90, "import_value_bn_usd": 1.8,
     "primary_origin": ["China", "Japan"],
     "hs_code": "8532",
     "substitute_possible": True, "substitution_horizon_years": 4},

    # Power & Electrical
    {"sector": "power_transmission", "component": "CRGO Steel",
     "import_share": 0.75, "import_value_bn_usd": 0.9,
     "primary_origin": ["Japan", "South Korea", "Russia"],
     "hs_code": "7225",
     "substitute_possible": True, "substitution_horizon_years": 4},
    {"sector": "power_transmission", "component": "HVDC Equipment",
     "import_share": 0.85, "import_value_bn_usd": 0.5,
     "primary_origin": ["ABB (EU)", "Hitachi (Japan)", "Siemens (Germany)"],
     "hs_code": "8504",
     "substitute_possible": False, "substitution_horizon_years": 10},

    # Specialty Chemicals
    {"sector": "specialty_chemicals", "component": "Specialty Agrochem AIs",
     "import_share": 0.65, "import_value_bn_usd": 1.1,
     "primary_origin": ["China"],
     "hs_code": "3808",
     "substitute_possible": True, "substitution_horizon_years": 3},
    {"sector": "specialty_chemicals", "component": "Pharma APIs",
     "import_share": 0.68, "import_value_bn_usd": 3.0,
     "primary_origin": ["China"],
     "hs_code": "2941",
     "substitute_possible": True, "substitution_horizon_years": 3},
    {"sector": "specialty_chemicals", "component": "Fluorochemicals",
     "import_share": 0.70, "import_value_bn_usd": 0.8,
     "primary_origin": ["China"],
     "hs_code": "2903",
     "substitute_possible": True, "substitution_horizon_years": 4},

    # Battery / EV
    {"sector": "battery_storage", "component": "Lithium Carbonate / Hydroxide",
     "import_share": 1.00, "import_value_bn_usd": 0.9,
     "primary_origin": ["Chile", "Australia", "China"],
     "hs_code": "2836",
     "substitute_possible": False, "substitution_horizon_years": 10},
    {"sector": "battery_storage", "component": "Cathode Active Materials (LFP/NMC)",
     "import_share": 0.95, "import_value_bn_usd": 1.2,
     "primary_origin": ["China"],
     "hs_code": "2841",
     "substitute_possible": True, "substitution_horizon_years": 4},

    # Defense
    {"sector": "defense_electronics", "component": "Defense Electronics (radar, avionics)",
     "import_share": 0.60, "import_value_bn_usd": 4.0,
     "primary_origin": ["Israel", "France", "USA", "Russia"],
     "hs_code": "8526",
     "substitute_possible": True, "substitution_horizon_years": 6},

    # Telecom
    {"sector": "5g_telecom", "component": "Optical Fiber Preforms",
     "import_share": 0.55, "import_value_bn_usd": 0.4,
     "primary_origin": ["China", "USA"],
     "hs_code": "9002",
     "substitute_possible": True, "substitution_horizon_years": 3},
]

# PLI / incentive schemes indexed by sector
_INCENTIVE_SCHEMES: dict[str, list[str]] = {
    "solar":                  ["PLI Solar PV (₹24,000 Cr)", "ALMM (domestic content)", "BCD on imports"],
    "electronics_manufacturing": ["PLI Mobile/IT Hardware (₹17,000 Cr)", "SPECS scheme", "M-SIPS"],
    "battery_storage":        ["PLI ACC Battery (₹18,100 Cr)", "FAME-II", "Viability Gap Funding"],
    "specialty_chemicals":    ["PLI Pharma (₹15,000 Cr)", "PLI Chemicals (₹62,000 Cr)", "BCD on Chinese APIs"],
    "defense_electronics":    ["DAP 2020 indigenisation", "iDEX", "Technology Development Fund"],
    "5g_telecom":             ["PLI Telecom (₹12,195 Cr)", "USOF subsidy", "PMA rule"],
    "semiconductor":          ["India Semiconductor Mission ($10Bn)", "SPECS (25% fiscal support)"],
    "power_transmission":     ["Make in India for Power Sector", "BEE efficiency standards"],
    "railway_infrastructure": ["Make in India Railways", "DFC project funding"],
    "water_infrastructure":   ["Jal Jeevan Mission (₹3.6L Cr)", "AMRUT 2.0"],
}


@dataclass
class ImportDependency:
    sector: str
    component: str
    import_share: float          # 0.0 – 1.0
    import_value_bn_usd: float
    primary_origin: list[str]
    hs_code: str
    substitute_possible: bool
    substitution_horizon_years: int
    risk_level: str              # "critical" | "high" | "moderate"


@dataclass
class LocalizationOpportunity:
    sector: str
    component: str
    import_share: float
    import_value_bn_usd: float
    incentive_schemes: list[str]
    opportunity_score: float     # 0.0 – 1.0
    theme_name: str
    rationale: str
    horizon_years: int


class ImportDependencyEngine:
    """Layer 4: Track India's import dependencies by sector/component."""

    def get_dependencies(self, min_import_share: float = 0.50) -> list[ImportDependency]:
        results = []
        for d in _IMPORT_DEPENDENCY_DATA:
            if d["import_share"] < min_import_share:
                continue
            risk = "critical" if d["import_share"] >= 0.85 else (
                   "high"     if d["import_share"] >= 0.65 else "moderate")
            results.append(ImportDependency(
                sector=d["sector"],
                component=d["component"],
                import_share=d["import_share"],
                import_value_bn_usd=d["import_value_bn_usd"],
                primary_origin=d["primary_origin"],
                hs_code=d["hs_code"],
                substitute_possible=d["substitute_possible"],
                substitution_horizon_years=d["substitution_horizon_years"],
                risk_level=risk,
            ))
        results.sort(key=lambda x: -x.import_share)
        logger.info(f"[ImportDependencyEngine] {len(results)} import dependencies "
                    f"(threshold ≥ {min_import_share:.0%})")
        return results

    def persist(self, deps: list[ImportDependency], pg_store) -> int:
        self._ensure_schema(pg_store)
        saved = 0
        today = date.today()
        for d in deps:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO mg_import_dependencies
                                (sector, component, import_share, import_value_bn_usd,
                                 primary_origin, hs_code, substitute_possible,
                                 substitution_horizon_years, risk_level, as_of_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (sector, component)
                            DO UPDATE SET
                                import_share                = EXCLUDED.import_share,
                                import_value_bn_usd         = EXCLUDED.import_value_bn_usd,
                                risk_level                  = EXCLUDED.risk_level,
                                as_of_date                  = EXCLUDED.as_of_date,
                                updated_at                  = NOW()
                            """,
                            (d.sector, d.component, d.import_share, d.import_value_bn_usd,
                             ",".join(d.primary_origin), d.hs_code, d.substitute_possible,
                             d.substitution_horizon_years, d.risk_level, today),
                        )
                saved += 1
            except Exception as e:
                logger.warning(f"[ImportDependencyEngine] persist failed {d.component}: {e}")
        return saved

    def _ensure_schema(self, pg_store):
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS mg_import_dependencies (
                            id                           SERIAL PRIMARY KEY,
                            sector                       TEXT NOT NULL,
                            component                    TEXT NOT NULL,
                            import_share                 NUMERIC,
                            import_value_bn_usd          NUMERIC,
                            primary_origin               TEXT,
                            hs_code                      TEXT,
                            substitute_possible          BOOLEAN,
                            substitution_horizon_years   INTEGER,
                            risk_level                   TEXT,
                            as_of_date                   DATE,
                            created_at                   TIMESTAMPTZ DEFAULT NOW(),
                            updated_at                   TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE (sector, component)
                        )
                    """)
        except Exception as e:
            logger.debug(f"[ImportDependencyEngine] schema check: {e}")


class LocalizationOpportunityEngine:
    """Layer 5: Cross-reference import dependencies with government incentives
    to surface high-conviction localization investment opportunities."""

    def identify(
        self,
        dependencies: list[ImportDependency],
        min_import_share: float = 0.60,
        min_incentive_schemes: int = 1,
    ) -> list[LocalizationOpportunity]:
        opportunities: list[LocalizationOpportunity] = []

        for dep in dependencies:
            if dep.import_share < min_import_share:
                continue
            if not dep.substitute_possible:
                continue

            schemes = _INCENTIVE_SCHEMES.get(dep.sector, [])
            if len(schemes) < min_incentive_schemes:
                continue

            # Opportunity score: higher import share + more incentives + shorter horizon
            import_score   = dep.import_share
            incentive_score = min(1.0, len(schemes) / 4.0)
            horizon_score  = max(0.0, 1.0 - dep.substitution_horizon_years / 10.0)
            value_score    = min(1.0, dep.import_value_bn_usd / 5.0)

            opp_score = round(
                import_score   * 0.35 +
                incentive_score * 0.25 +
                horizon_score  * 0.20 +
                value_score    * 0.20,
                3,
            )

            theme_name = f"{dep.component} Localization Opportunity"
            rationale = (
                f"India imports {dep.import_share:.0%} of {dep.component} "
                f"(USD {dep.import_value_bn_usd:.1f}Bn/yr, mainly from "
                f"{', '.join(dep.primary_origin[:2])}). "
                f"Government has {len(schemes)} active incentive scheme(s): "
                f"{'; '.join(schemes[:2])}. "
                f"Domestic substitution feasible within {dep.substitution_horizon_years}y."
            )

            opportunities.append(LocalizationOpportunity(
                sector=dep.sector,
                component=dep.component,
                import_share=dep.import_share,
                import_value_bn_usd=dep.import_value_bn_usd,
                incentive_schemes=schemes,
                opportunity_score=opp_score,
                theme_name=theme_name,
                rationale=rationale,
                horizon_years=dep.substitution_horizon_years,
            ))

        opportunities.sort(key=lambda x: -x.opportunity_score)
        logger.info(f"[LocalizationOpportunityEngine] {len(opportunities)} localization opportunities identified")
        return opportunities

    def persist(self, opportunities: list[LocalizationOpportunity], pg_store) -> int:
        self._ensure_schema(pg_store)
        saved = 0
        today = date.today()
        for opp in opportunities:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO mg_localization_opportunities
                                (sector, component, import_share, import_value_bn_usd,
                                 incentive_schemes, opportunity_score, theme_name,
                                 rationale, horizon_years, as_of_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (sector, component)
                            DO UPDATE SET
                                opportunity_score  = EXCLUDED.opportunity_score,
                                incentive_schemes  = EXCLUDED.incentive_schemes,
                                rationale          = EXCLUDED.rationale,
                                as_of_date         = EXCLUDED.as_of_date,
                                updated_at         = NOW()
                            """,
                            (opp.sector, opp.component, opp.import_share,
                             opp.import_value_bn_usd,
                             ";".join(opp.incentive_schemes),
                             opp.opportunity_score, opp.theme_name,
                             opp.rationale[:1000], opp.horizon_years, today),
                        )
                saved += 1
            except Exception as e:
                logger.warning(f"[LocalizationOpportunityEngine] persist failed {opp.component}: {e}")
        return saved

    def _ensure_schema(self, pg_store):
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS mg_localization_opportunities (
                            id                  SERIAL PRIMARY KEY,
                            sector              TEXT NOT NULL,
                            component           TEXT NOT NULL,
                            import_share        NUMERIC,
                            import_value_bn_usd NUMERIC,
                            incentive_schemes   TEXT,
                            opportunity_score   NUMERIC,
                            theme_name          TEXT,
                            rationale           TEXT,
                            horizon_years       INTEGER,
                            as_of_date          DATE,
                            created_at          TIMESTAMPTZ DEFAULT NOW(),
                            updated_at          TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE (sector, component)
                        )
                    """)
        except Exception as e:
            logger.debug(f"[LocalizationOpportunityEngine] schema check: {e}")
