"""Supply Chain Intelligence Layer.

Maps upstream/downstream industrial dependencies, detects second-order
beneficiaries, models bottleneck propagation chains, and builds
industrial infrastructure dependency graphs.

Architecture:
    Company A -[SUPPLIES]-> Company B  (direct tier-1)
    Company B -[DEPENDS_ON]-> Technology X
    Technology X -[CONSTRAINS]-> Company C  (bottleneck)

Second-order beneficiary detection:
    Theme: AI Infrastructure
      Direct:    NVDA (GPUs)
      2nd-order: TSMC (wafer supply), ASML (lithography), ASE (packaging)
      3rd-order: Air Products (specialty gases), Shin-Etsu (silicon wafers)
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SupplyNode:
    """A node in the supply chain graph."""
    name: str
    node_type: str          # Company | Technology | Commodity | Component
    ticker: Optional[str] = None
    sector: str = ""
    tier: int = 0           # 0=anchor, 1=direct, 2=second-order, 3=third-order
    bottleneck_score: float = 0.0   # 0-100: how critical this node is
    exposure_score: float = 0.0     # 0-100: exposure to theme risk
    metadata: dict = field(default_factory=dict)


@dataclass
class SupplyEdge:
    """A directed supply chain relationship."""
    source: str
    target: str
    relation: str           # SUPPLIES | DEPENDS_ON | CONSTRAINS | BENEFITS_FROM
    weight: float = 1.0
    description: str = ""
    evidence_count: int = 0  # how many filings support this edge


@dataclass
class SupplyChainMap:
    """Complete supply chain mapping for a theme or technology."""
    theme_slug: str
    anchor_entity: str
    nodes: list[SupplyNode] = field(default_factory=list)
    edges: list[SupplyEdge] = field(default_factory=list)
    bottlenecks: list[str] = field(default_factory=list)
    hidden_beneficiaries: list[dict] = field(default_factory=list)
    created_at: Optional[date] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = date.today()

    def get_tier(self, tier: int) -> list[SupplyNode]:
        return [n for n in self.nodes if n.tier == tier]

    def summary(self) -> dict:
        return {
            "theme_slug": self.theme_slug,
            "anchor_entity": self.anchor_entity,
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "bottlenecks": self.bottlenecks,
            "tier_1_count": len(self.get_tier(1)),
            "tier_2_count": len(self.get_tier(2)),
            "tier_3_count": len(self.get_tier(3)),
            "hidden_beneficiaries": len(self.hidden_beneficiaries),
        }


# -------------------------------------------------------
# KNOWN SUPPLY CHAIN TEMPLATES
# -------------------------------------------------------
SUPPLY_CHAIN_TEMPLATES: dict[str, dict] = {
    "ai-infrastructure": {
        "anchor": "Artificial Intelligence",
        "tiers": {
            1: [
                {"name": "GPU", "type": "Technology", "description": "Compute backbone for AI training"},
                {"name": "Data Center", "type": "Technology", "description": "Hosting infrastructure"},
                {"name": "Cloud", "type": "Technology", "description": "Hyperscaler delivery layer"},
            ],
            2: [
                {"name": "Semiconductor", "type": "Technology", "description": "Chip fabrication"},
                {"name": "High Bandwidth Memory", "type": "Technology", "description": "HBM for AI accelerators"},
                {"name": "Wafer", "type": "Technology", "description": "Substrate for chip production"},
            ],
            3: [
                {"name": "Power Demand", "type": "Concept", "description": "Electricity grid capacity"},
                {"name": "Cooling", "type": "Concept", "description": "Thermal management infrastructure"},
                {"name": "Fiber Optic", "type": "Technology", "description": "Networking backbone"},
            ],
        },
        "bottlenecks": ["Wafer", "High Bandwidth Memory", "Power Demand"],
        "relations": [
            ("Artificial Intelligence", "GPU", "DEPENDS_ON"),
            ("GPU", "Semiconductor", "DEPENDS_ON"),
            ("GPU", "High Bandwidth Memory", "DEPENDS_ON"),
            ("Semiconductor", "Wafer", "DEPENDS_ON"),
            ("Data Center", "Power Demand", "DEPENDS_ON"),
            ("Data Center", "Cooling", "DEPENDS_ON"),
            ("Cloud", "Data Center", "DEPENDS_ON"),
            ("High Bandwidth Memory", "Wafer", "DEPENDS_ON"),
        ],
    },
    "semiconductor-supply-chain": {
        "anchor": "Semiconductor",
        "tiers": {
            1: [
                {"name": "GPU", "type": "Technology", "description": "High-performance chips"},
                {"name": "Chip", "type": "Technology", "description": "Integrated circuits"},
                {"name": "Wafer", "type": "Technology", "description": "Silicon wafer substrate"},
            ],
            2: [
                {"name": "Lithography", "type": "Technology", "description": "EUV/DUV patterning"},
                {"name": "CoWoS", "type": "Technology", "description": "Advanced packaging"},
                {"name": "Chemical Mechanical Planarization", "type": "Concept", "description": "CMP materials"},
            ],
            3: [
                {"name": "Specialty Gases", "type": "Concept", "description": "Semiconductor process gases"},
                {"name": "Silicon", "type": "Concept", "description": "Raw material substrate"},
                {"name": "Photoresist", "type": "Concept", "description": "Chemical patterning agent"},
            ],
        },
        "bottlenecks": ["Wafer", "CoWoS", "Lithography"],
        "relations": [
            ("Semiconductor", "Wafer", "DEPENDS_ON"),
            ("Semiconductor", "Lithography", "DEPENDS_ON"),
            ("GPU", "CoWoS", "DEPENDS_ON"),
            ("Chip", "Wafer", "DEPENDS_ON"),
            ("Wafer", "Silicon", "DEPENDS_ON"),
            ("Lithography", "Specialty Gases", "DEPENDS_ON"),
        ],
    },
    "ev-battery": {
        "anchor": "Electric Vehicle",
        "tiers": {
            1: [
                {"name": "Battery", "type": "Technology", "description": "Energy storage system"},
                {"name": "Motor", "type": "Technology", "description": "Electric drivetrain"},
                {"name": "Power Electronics", "type": "Technology", "description": "Inverter, onboard charger"},
            ],
            2: [
                {"name": "Lithium", "type": "Concept", "description": "Lithium carbonate/hydroxide"},
                {"name": "Cobalt", "type": "Concept", "description": "Cathode material"},
                {"name": "Nickel", "type": "Concept", "description": "High-nickel cathode"},
            ],
            3: [
                {"name": "Mining Capex", "type": "Concept", "description": "Upstream mining investment"},
                {"name": "Rare Earth", "type": "Concept", "description": "Permanent magnets for motors"},
                {"name": "Copper", "type": "Concept", "description": "EV wiring (4x conventional auto)"},
            ],
        },
        "bottlenecks": ["Lithium", "Cobalt", "Rare Earth"],
        "relations": [
            ("Electric Vehicle", "Battery", "DEPENDS_ON"),
            ("Battery", "Lithium", "DEPENDS_ON"),
            ("Battery", "Cobalt", "DEPENDS_ON"),
            ("Battery", "Nickel", "DEPENDS_ON"),
            ("Motor", "Rare Earth", "DEPENDS_ON"),
            ("Lithium", "Mining Capex", "ENABLES"),
            ("Copper", "Electric Vehicle", "ENABLED_BY"),
        ],
    },
}


class SupplyChainAnalyzer:
    """Builds and analyzes supply chain maps from templates and live graph data.

    Workflow:
        1. Load template for a theme
        2. Enrich with live Neo4j graph relationships
        3. Score bottleneck nodes by in-degree centrality
        4. Surface hidden second/third-order beneficiaries
        5. Optionally persist map to Neo4j
    """

    def __init__(self, config: dict, graph_store=None):
        self._cfg = config
        self._graph_store = graph_store

    def build_map(self, theme_slug: str) -> Optional[SupplyChainMap]:
        """Build a supply chain map for the given theme slug."""
        template = SUPPLY_CHAIN_TEMPLATES.get(theme_slug)
        if not template:
            logger.debug(f"No supply chain template for theme: {theme_slug}")
            return None

        sc_map = SupplyChainMap(
            theme_slug=theme_slug,
            anchor_entity=template["anchor"],
        )

        sc_map.nodes.append(SupplyNode(
            name=template["anchor"],
            node_type="Technology",
            tier=0,
            bottleneck_score=0.0,
        ))

        for tier, members in template.get("tiers", {}).items():
            for m in members:
                sc_map.nodes.append(SupplyNode(
                    name=m["name"],
                    node_type=m["type"],
                    tier=int(tier),
                    metadata={"description": m.get("description", "")},
                ))

        for src, tgt, rel in template.get("relations", []):
            sc_map.edges.append(SupplyEdge(
                source=src,
                target=tgt,
                relation=rel,
                weight=1.0,
            ))

        sc_map.bottlenecks = list(template.get("bottlenecks", []))

        in_degree: dict[str, int] = defaultdict(int)
        for edge in sc_map.edges:
            in_degree[edge.target] += 1

        for node in sc_map.nodes:
            if node.name in sc_map.bottlenecks:
                node.bottleneck_score = min(100.0, in_degree[node.name] * 25.0 + 50.0)

        sc_map.hidden_beneficiaries = [
            {
                "entity": n.name,
                "type": n.node_type,
                "tier": n.tier,
                "bottleneck_score": n.bottleneck_score,
                "description": n.metadata.get("description", ""),
            }
            for n in sc_map.nodes
            if n.tier >= 2
        ]

        if self._graph_store:
            self._enrich_from_neo4j(sc_map)

        logger.info(
            f"Supply chain map built: {theme_slug} — "
            f"{len(sc_map.nodes)} nodes, {len(sc_map.edges)} edges, "
            f"{len(sc_map.bottlenecks)} bottlenecks"
        )
        return sc_map

    def _enrich_from_neo4j(self, sc_map: SupplyChainMap):
        """Add live relationships from Neo4j to the template map."""
        try:
            anchor = sc_map.anchor_entity
            live_edges = self._graph_store.get_supply_chain(anchor, depth=3)
            existing = {(e.source, e.target) for e in sc_map.edges}
            added = 0
            for row in live_edges:
                src, tgt = row.get("from_name", ""), row.get("to_name", "")
                if src and tgt and (src, tgt) not in existing:
                    sc_map.edges.append(SupplyEdge(
                        source=src, target=tgt,
                        relation="DEPENDS_ON",
                        weight=row.get("weight", 1.0),
                        evidence_count=1,
                    ))
                    existing.add((src, tgt))
                    added += 1
            if added:
                logger.debug(f"Neo4j enriched {sc_map.theme_slug} with {added} live edges")
        except Exception as e:
            logger.debug(f"Neo4j enrichment skipped: {e}")

    def write_to_neo4j(self, sc_map: SupplyChainMap):
        """Persist supply chain edges to Neo4j using new relationship types."""
        if not self._graph_store:
            return
        written = 0
        for edge in sc_map.edges:
            try:
                self._graph_store.upsert_relationship(
                    source_type="Technology",
                    source_name=edge.source,
                    rel_type=edge.relation,
                    target_type="Technology",
                    target_name=edge.target,
                    properties={"weight": edge.weight, "source": "supply_chain_template"},
                )
                written += 1
            except Exception as e:
                logger.warning(f"Failed to write supply edge {edge.source}→{edge.target}: {e}")
        logger.info(f"Supply chain written to Neo4j: {written} edges for {sc_map.theme_slug}")

    def get_bottleneck_report(self, theme_slug: str) -> dict:
        """Return a bottleneck summary for a theme."""
        sc_map = self.build_map(theme_slug)
        if not sc_map:
            return {}
        return {
            "theme": theme_slug,
            "anchor": sc_map.anchor_entity,
            "bottlenecks": sc_map.bottlenecks,
            "hidden_beneficiaries": sc_map.hidden_beneficiaries,
            "summary": sc_map.summary(),
        }

    def get_all_reports(self) -> list[dict]:
        """Return bottleneck reports for all known themes."""
        reports = []
        for slug in SUPPLY_CHAIN_TEMPLATES:
            report = self.get_bottleneck_report(slug)
            if report:
                reports.append(report)
        return reports
