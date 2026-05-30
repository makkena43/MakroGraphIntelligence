"""Neo4j macro graph: Country, Commodity, Policy, MacroIndicator nodes.

Writes nodes and relationships that link macro/policy data to the
existing company/technology/theme knowledge graph, enabling queries like:

  "Which sectors BENEFIT when oil prices spike?"
  "Which companies SUPPLY_TO countries with export-control policies?"
  "Find all themes corroborated by both a FRED rate shock and a Congress bill."

All writes use MERGE so they are idempotent and safe to re-run.
"""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Map commodity_id → which sector uses it as a primary input
COMMODITY_SECTOR_INPUTS = {
    "WTI_CRUDE":        ["Energy", "Transportation", "Industrials", "Materials"],
    "BRENT_CRUDE":      ["Energy", "Transportation", "Industrials"],
    "HENRY_HUB":        ["Energy", "Utilities", "Industrials"],
    "COAL_THERMAL":     ["Utilities", "Industrials"],
    "URANIUM":          ["Utilities"],
    "COPPER":           ["Technology", "Industrials", "Utilities", "EV"],
    "LITHIUM":          ["Technology", "EV", "Energy Storage"],
    "COBALT":           ["Technology", "EV"],
    "SILICON_WAFER":    ["Technology", "Semiconductors"],
    "RARE_EARTH":       ["Technology", "Defense", "EV"],
    "CORN":             ["Agriculture", "Consumer Staples", "Biofuels"],
    "WHEAT":            ["Agriculture", "Consumer Staples"],
    "SOYBEAN":          ["Agriculture", "Consumer Staples"],
    "FERTILIZER_N":     ["Agriculture"],
    "BALTIC_DRY":       ["Industrials", "Materials", "Energy"],
    "CONTAINER_RATE":   ["Consumer Discretionary", "Technology", "Industrials"],
}

# Countries that are major exporters of key commodities
COUNTRY_COMMODITY_EXPORTS = {
    "SA": ["WTI_CRUDE", "BRENT_CRUDE"],
    "RU": ["WTI_CRUDE", "BRENT_CRUDE", "HENRY_HUB", "WHEAT", "FERTILIZER_N"],
    "US": ["WTI_CRUDE", "HENRY_HUB", "CORN", "SOYBEAN", "WHEAT"],
    "CN": ["RARE_EARTH", "SILICON_WAFER", "COAL_THERMAL"],
    "AU": ["COAL_THERMAL", "COPPER", "RARE_EARTH"],
    "CL": ["COPPER", "LITHIUM"],
    "BR": ["SOYBEAN", "CORN"],
    "KZ": ["URANIUM"],
    "CA": ["WTI_CRUDE", "WHEAT", "URANIUM"],
}


class MacroGraphStore:
    """Write macro/policy nodes and relationships to Neo4j.

    Requires an active neo4j.GraphDatabase driver session.
    Pass the GraphStore instance (which owns the driver) from the pipeline.
    """

    def __init__(self, graph_store):
        self._gs = graph_store  # existing GraphStore with ._driver

    def _run(self, cypher: str, params: dict = None):
        if not self._gs or not self._gs._driver:
            return
        try:
            with self._gs._driver.session() as session:
                session.run(cypher, **(params or {}))
        except Exception as e:
            logger.error(f"MacroGraph Cypher error: {e}\n{cypher[:200]}")

    # ----------------------------------------------------------
    # MACRO INDICATOR NODES
    # ----------------------------------------------------------
    def upsert_macro_indicator(self, row: dict):
        """Create/update a MacroIndicator node from a macro series row."""
        self._run(
            """
            MERGE (m:MacroIndicator {series_id: $series_id})
            SET m.series_name  = $series_name,
                m.source       = $source,
                m.country      = $country,
                m.frequency    = $frequency,
                m.units        = $units,
                m.latest_value = $value,
                m.latest_date  = date($obs_date),
                m.last_updated = date()
            WITH m
            MATCH (c:Country {iso2: $country})
            MERGE (c)-[:HAS_INDICATOR]->(m)
            """,
            {
                "series_id":  row.get("series_id", ""),
                "series_name": row.get("series_name", ""),
                "source":     row.get("source", "fred"),
                "country":    row.get("country", "US"),
                "frequency":  row.get("frequency", ""),
                "units":      row.get("units", ""),
                "value":      row.get("value"),
                "obs_date":   str(row.get("observation_date", "")),
            },
        )

    def upsert_macro_indicators_bulk(self, rows: list[dict]):
        """Update MacroIndicator nodes with the latest observation per series."""
        # Deduplicate to latest obs per series_id
        latest: dict[str, dict] = {}
        for r in rows:
            sid = r.get("series_id", "")
            if sid not in latest or str(r.get("observation_date", "")) > str(latest[sid].get("observation_date", "")):
                latest[sid] = r
        for row in latest.values():
            self.upsert_macro_indicator(row)
        logger.info(f"MacroGraph: upserted {len(latest)} MacroIndicator nodes")

    # ----------------------------------------------------------
    # COMMODITY NODES
    # ----------------------------------------------------------
    def upsert_commodity(self, row: dict):
        """Create/update a Commodity node from a commodity series row."""
        self._run(
            """
            MERGE (c:Commodity {commodity_id: $commodity_id})
            SET c.name         = $name,
                c.category     = $category,
                c.units        = $units,
                c.last_price   = $value,
                c.last_price_date = date($obs_date),
                c.last_updated = date()
            """,
            {
                "commodity_id": row.get("commodity_id", ""),
                "name":         row.get("commodity_name", row.get("commodity_id", "")),
                "category":     row.get("category", "energy"),
                "units":        row.get("units", ""),
                "value":        row.get("value"),
                "obs_date":     str(row.get("observation_date", "")),
            },
        )

    def upsert_commodities_bulk(self, rows: list[dict]):
        """Update Commodity nodes and wire sector input relationships."""
        latest: dict[str, dict] = {}
        for r in rows:
            cid = r.get("commodity_id", "")
            if cid not in latest or str(r.get("observation_date", "")) > str(latest[cid].get("observation_date", "")):
                latest[cid] = r
        for row in latest.values():
            self.upsert_commodity(row)
            self._wire_commodity_sector_inputs(row.get("commodity_id", ""))
            self._wire_country_exports(row.get("commodity_id", ""))
        logger.info(f"MacroGraph: upserted {len(latest)} Commodity nodes")

    def _wire_commodity_sector_inputs(self, commodity_id: str):
        """Create IS_INPUT_FOR relationships to Sector nodes."""
        sectors = COMMODITY_SECTOR_INPUTS.get(commodity_id, [])
        for sector in sectors:
            self._run(
                """
                MATCH (c:Commodity {commodity_id: $cid})
                MERGE (s:Sector {name: $sector})
                MERGE (c)-[:IS_INPUT_FOR {criticality: $crit}]->(s)
                """,
                {
                    "cid":    commodity_id,
                    "sector": sector,
                    "crit":   80.0 if len(sectors) <= 2 else 50.0,
                },
            )

    def _wire_country_exports(self, commodity_id: str):
        """Create EXPORTS relationships from Country to Commodity."""
        for country, commodities in COUNTRY_COMMODITY_EXPORTS.items():
            if commodity_id in commodities:
                self._run(
                    """
                    MATCH (co:Country {iso2: $iso2})
                    MATCH (c:Commodity {commodity_id: $cid})
                    MERGE (co)-[:EXPORTS {year: $year}]->(c)
                    """,
                    {"iso2": country, "cid": commodity_id, "year": date.today().year},
                )

    # ----------------------------------------------------------
    # POLICY NODES
    # ----------------------------------------------------------
    def upsert_policy(self, event: dict):
        """Create/update a Policy node and wire sector/tech relationships."""
        self._run(
            """
            MERGE (p:Policy {policy_id: $policy_id})
            SET p.title              = $title,
                p.policy_type        = $policy_type,
                p.status             = $status,
                p.enacted_date       = CASE WHEN $enacted_date <> '' THEN date($enacted_date) ELSE null END,
                p.effective_date     = CASE WHEN $eff_date <> '' THEN date($eff_date) ELSE null END,
                p.impact_direction   = $direction,
                p.impact_magnitude   = $magnitude,
                p.sectors_affected   = $sectors,
                p.technologies_affected = $techs,
                p.last_updated       = date()
            """,
            {
                "policy_id":  event.get("policy_id", ""),
                "title":      (event.get("title", "") or "")[:500],
                "policy_type": event.get("policy_type", "notice"),
                "status":     event.get("status", ""),
                "enacted_date": str(event.get("enacted_date") or ""),
                "eff_date":   str(event.get("effective_date") or ""),
                "direction":  event.get("impact_direction", "neutral"),
                "magnitude":  float(event.get("impact_magnitude", 0.0)),
                "sectors":    event.get("sectors_affected", []),
                "techs":      event.get("technologies_affected", []),
            },
        )

        # Wire to affected sectors
        direction = event.get("impact_direction", "neutral")
        rel = "SUBSIDISES" if direction == "positive" else "RESTRICTS"
        for sector in event.get("sectors_affected", []):
            self._run(
                f"""
                MATCH (p:Policy {{policy_id: $pid}})
                MERGE (s:Sector {{name: $sector}})
                MERGE (p)-[:{rel}]->(s)
                """,
                {"pid": event.get("policy_id", ""), "sector": sector},
            )

        # Wire to affected technologies
        for tech in event.get("technologies_affected", []):
            self._run(
                f"""
                MATCH (p:Policy {{policy_id: $pid}})
                MERGE (t:Technology {{name: $tech}})
                MERGE (p)-[:{rel}]->(t)
                """,
                {"pid": event.get("policy_id", ""), "tech": tech},
            )

    def upsert_policies_bulk(self, events: list[dict]):
        """Upsert all policy events as Policy nodes."""
        for e in events:
            try:
                self.upsert_policy(e)
            except Exception as ex:
                logger.debug(f"MacroGraph policy node error: {ex}")
        logger.info(f"MacroGraph: upserted {len(events)} Policy nodes")

    # ----------------------------------------------------------
    # MACRO CONSTRAINT RELATIONSHIPS
    # ----------------------------------------------------------
    def link_indicator_to_theme(
        self,
        series_id: str,
        theme_slug: str,
        constraint_type: str,
        correlation: float = 0.0,
    ):
        """Wire a MacroIndicator to a Theme with CONSTRAINS or CORRELATES_WITH."""
        self._run(
            """
            MATCH (m:MacroIndicator {series_id: $sid})
            MATCH (t:Theme {slug: $slug})
            MERGE (m)-[r:CONSTRAINS]->(t)
            SET r.constraint_type = $ctype,
                r.correlation     = $corr,
                r.updated_at      = date()
            """,
            {
                "sid":  series_id,
                "slug": theme_slug,
                "ctype": constraint_type,
                "corr": correlation,
            },
        )

    # ----------------------------------------------------------
    # COUNTRY INDICATOR SUMMARY
    # ----------------------------------------------------------
    def update_country_summary(self, country_iso2: str, indicators: dict):
        """Update Country node with latest macro snapshot."""
        props = {}
        if "NY.GDP.MKTP.CD" in indicators:
            props["gdp_usd_bn"] = (indicators["NY.GDP.MKTP.CD"] or 0) / 1e9
        if "NY.GDP.MKTP.KD.ZG" in indicators:
            props["gdp_growth_pct"] = indicators["NY.GDP.MKTP.KD.ZG"]
        if "FP.CPI.TOTL.ZG" in indicators:
            props["inflation_pct"] = indicators["FP.CPI.TOTL.ZG"]
        if "GC.DOD.TOTL.GD.ZS" in indicators:
            props["debt_to_gdp"] = indicators["GC.DOD.TOTL.GD.ZS"]
        if "FI.RES.TOTL.CD" in indicators:
            props["fx_reserves_usd_bn"] = (indicators["FI.RES.TOTL.CD"] or 0) / 1e9
        if "SP.POP.TOTL" in indicators:
            props["population_mn"] = (indicators["SP.POP.TOTL"] or 0) / 1e6

        if not props:
            return

        set_clause = ", ".join(f"c.{k} = ${k}" for k in props)
        props["iso2"] = country_iso2
        props["last_updated"] = str(date.today())
        self._run(
            f"MERGE (c:Country {{iso2: $iso2}}) SET {set_clause}, c.last_updated = date($last_updated)",
            props,
        )
