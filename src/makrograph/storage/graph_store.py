"""Neo4j graph store for ontology nodes, edges, and relationship queries."""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


class GraphStore:
    """Neo4j-backed knowledge graph.

    Manages company/technology/sector/concept nodes and their relationships.
    Supports ontology evolution, cross-sector theme detection, and supply-chain queries.
    """

    def __init__(self, config: dict):
        from neo4j import GraphDatabase

        uri = config.get("uri", "bolt://localhost:7687")
        user = config.get("user", "neo4j")
        password = config.get("password", "makrograph")

        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        # Verify connectivity immediately so callers can catch the error at init
        # time rather than silently holding a broken driver that fails on every query.
        self._driver.verify_connectivity()
        logger.info(f"GraphStore connected: {uri}")

    def _run(self, cypher: str, params: dict = None) -> list[dict]:
        """Execute Cypher and return all records as a list of dicts (eagerly consumed)."""
        with self._driver.session() as session:
            result = session.run(cypher, params or {})
            return result.data()   # fully consumed inside session context

    def _run_single(self, cypher: str, params: dict = None) -> dict | None:
        """Execute Cypher and return the first record as a dict, or None."""
        rows = self._run(cypher, params)
        return rows[0] if rows else None

    def apply_schema(self, schema_path: str = "schema/neo4j_schema.cypher"):
        """Apply constraints and indexes from schema file."""
        from pathlib import Path
        cypher = Path(schema_path).read_text()
        statements = [s.strip() for s in cypher.split(";") if s.strip() and not s.strip().startswith("//")]
        with self._driver.session() as session:
            for stmt in statements:
                if stmt.strip():
                    try:
                        session.run(stmt)
                    except Exception as e:
                        logger.warning(f"Schema stmt skipped: {e}")
        logger.info("Neo4j schema applied")

    # ----------------------------------------------------------
    # NODE OPERATIONS
    # ----------------------------------------------------------
    def upsert_company(self, name: str, properties: dict = None) -> str:
        props = properties or {}
        cypher = """
            MERGE (c:Company {name: $name})
            SET c += $props,
                c.last_seen_at = $today,
                c.mention_count = COALESCE(c.mention_count, 0) + 1
            RETURN c.name as name
        """
        props["name"] = name
        rows = self._run(cypher, {"name": name, "props": props, "today": str(date.today())})
        return rows[0]["name"] if rows else name

    def upsert_technology(self, name: str, properties: dict = None) -> str:
        props = properties or {}
        cypher = """
            MERGE (t:Technology {name: $name})
            SET t += $props,
                t.last_seen_at = $today,
                t.mention_count = COALESCE(t.mention_count, 0) + 1
            RETURN t.name as name
        """
        rows = self._run(cypher, {"name": name, "props": props, "today": str(date.today())})
        return rows[0]["name"] if rows else name

    def upsert_concept(self, name: str, concept_type: str = "macro_trend", properties: dict = None) -> str:
        props = properties or {}
        props["concept_type"] = concept_type
        cypher = """
            MERGE (c:Concept {name: $name})
            SET c += $props,
                c.last_seen_at = $today,
                c.mention_count = COALESCE(c.mention_count, 0) + 1
            RETURN c.name as name
        """
        rows = self._run(cypher, {"name": name, "props": props, "today": str(date.today())})
        return rows[0]["name"] if rows else name

    def upsert_node(self, node_type: str, name: str, properties: dict = None) -> str:
        """Generic node upsert for any node type."""
        props = properties or {}
        if node_type == "Company":
            return self.upsert_company(name, props)
        if node_type == "Technology":
            return self.upsert_technology(name, props)
        if node_type == "Concept":
            return self.upsert_concept(name, properties=props)
        cypher = f"""
            MERGE (n:{node_type} {{name: $name}})
            SET n += $props, n.last_seen_at = $today
            RETURN n.name as name
        """
        rows = self._run(cypher, {"name": name, "props": props, "today": str(date.today())})
        return rows[0]["name"] if rows else name

    # ----------------------------------------------------------
    # RELATIONSHIP OPERATIONS
    # ----------------------------------------------------------
    def upsert_relationship(
        self,
        source_type: str,
        source_name: str,
        rel_type: str,
        target_type: str,
        target_name: str,
        properties: dict = None,
    ):
        """Create or update a typed relationship between two nodes."""
        props = properties or {}
        cypher = f"""
            MATCH (a:{source_type} {{name: $src}})
            MATCH (b:{target_type} {{name: $tgt}})
            MERGE (a)-[r:{rel_type}]->(b)
            SET r += $props,
                r.last_seen_at = $today,
                r.weight = COALESCE(r.weight, 0) + $weight_delta
        """
        self._run(cypher, {
            "src": source_name,
            "tgt": target_name,
            "props": props,
            "today": str(date.today()),
            "weight_delta": props.pop("weight_delta", 1.0),
        })

    def upsert_theme_node(
        self,
        slug: str,
        name: str,
        canonical_name: str = "",
        aliases: list[str] | None = None,
        is_canonical: bool = True,
        strength_score: float = 0.0,
        conviction: str = "emerging",
        description: str = "",
    ) -> None:
        """Create or update a :Theme node in Neo4j.

        Properties stored on the node:
            slug, name, canonical_name, aliases, is_canonical,
            strength_score, conviction, description, last_updated
        """
        cypher = """
            MERGE (t:Theme {slug: $slug})
            SET t.name            = $name,
                t.canonical_name  = $canonical_name,
                t.aliases         = $aliases,
                t.is_canonical    = $is_canonical,
                t.strength_score  = $strength_score,
                t.conviction      = $conviction,
                t.description     = $description,
                t.last_updated    = $today
        """
        self._run(cypher, {
            "slug":           slug,
            "name":           name,
            "canonical_name": canonical_name or name,
            "aliases":        aliases or [],
            "is_canonical":   is_canonical,
            "strength_score": strength_score,
            "conviction":     conviction,
            "description":    description[:300] if description else "",
            "today":          str(date.today()),
        })

    def link_subtheme(self, child_slug: str, parent_slug: str) -> None:
        """Write (:Theme {slug: child})-[:SUB_THEME_OF]->(:Theme {slug: parent}).

        Both nodes are MERGED (created if absent) so this is safe to call
        even before upsert_theme_node().  The relationship carries a
        ``since`` date for temporal tracking.

        Cypher:
            (:Theme)-[:SUB_THEME_OF]->(:Theme)

        Company → mentions → Evidence → supports → Subtheme → SUB_THEME_OF → ParentTheme
        """
        cypher = """
            MERGE (child:Theme  {slug: $child_slug})
            MERGE (parent:Theme {slug: $parent_slug})
            MERGE (child)-[r:SUB_THEME_OF]->(parent)
            SET r.since = $today
        """
        self._run(cypher, {
            "child_slug":  child_slug,
            "parent_slug": parent_slug,
            "today":       str(date.today()),
        })

    def persist_theme_hierarchy(self, themes: list[dict]) -> int:
        """Persist a full theme list with canonical hierarchy to Neo4j.

        For each theme dict:
          1. MERGE :Theme node with all properties
          2. If is_canonical=False → write :SUB_THEME_OF relationship to parent

        Returns count of relationships written.
        """
        rels_written = 0
        for t in themes:
            slug = t.get("theme_slug") or ""
            if not slug:
                continue
            try:
                self.upsert_theme_node(
                    slug=slug,
                    name=t.get("theme_name", slug),
                    canonical_name=t.get("canonical_name", ""),
                    aliases=t.get("aliases", []),
                    is_canonical=t.get("is_canonical", True),
                    strength_score=float(t.get("strength_score", 0.0)),
                    conviction=t.get("conviction", "emerging"),
                    description=t.get("description", ""),
                )
                parent = t.get("parent_theme_slug")
                if parent:
                    self.link_subtheme(slug, parent)
                    rels_written += 1
            except Exception as e:
                logger.debug(f"Theme hierarchy Neo4j persist failed for {slug}: {e}")
        return rels_written

    def link_to_theme(self, entity_type: str, entity_name: str, theme_slug: str,
                      role: str = "beneficiary", relevance: float = 0.5):
        cypher = f"""
            MATCH (e:{entity_type} {{name: $name}})
            MERGE (t:Theme {{slug: $slug}})
            MERGE (e)-[r:PART_OF]->(t)
            SET r.role = $role, r.relevance_score = $relevance, r.last_updated = $today
        """
        self._run(cypher, {
            "name": entity_name,
            "slug": theme_slug,
            "role": role,
            "relevance": relevance,
            "today": str(date.today()),
        })

    # ----------------------------------------------------------
    # QUERY OPERATIONS
    # ----------------------------------------------------------
    def get_companies_by_technology(self, technology: str) -> list[dict]:
        """Find all companies developing or using a technology."""
        cypher = """
            MATCH (c:Company)-[r:DEVELOPS|USES]->(t:Technology {name: $tech})
            RETURN c.name as company, c.ticker as ticker, type(r) as relationship,
                   r.weight as weight
            ORDER BY r.weight DESC
            LIMIT 50
        """
        result = self._run(cypher, {"tech": technology})
        return [dict(r) for r in result]

    def get_supply_chain(self, company: str, depth: int = 3) -> list[dict]:
        """Trace upstream/downstream supply chain for a company."""
        cypher = """
            MATCH path = (c:Company {name: $company})-[:SUPPLIES_TO*1..$depth]->(end:Company)
            WITH end, length(path) as depth_level
            RETURN end.name as company, end.ticker as ticker, depth_level
            ORDER BY depth_level
        """
        result = self._run(cypher, {"company": company, "depth": depth})
        return [dict(r) for r in result]

    def get_emerging_relationships(self, days: int = 90) -> list[dict]:
        """Find relationships formed in last N days (new investments, partnerships)."""
        since = str(date.today())
        cypher = """
            MATCH (a)-[r]->(b)
            WHERE r.since_date >= $since
               OR r.last_seen_at >= $since
            RETURN labels(a)[0] as from_type, a.name as from_name,
                   type(r) as relationship,
                   labels(b)[0] as to_type, b.name as to_name,
                   r.weight as weight, r.last_seen_at as last_seen
            ORDER BY r.weight DESC
            LIMIT 100
        """
        result = self._run(cypher, {"since": since})
        return [dict(r) for r in result]

    def get_theme_entities(self, theme_slug: str) -> list[dict]:
        """Get all entities associated with a theme."""
        cypher = """
            MATCH (e)-[r:PART_OF]->(t:Theme {slug: $slug})
            RETURN labels(e)[0] as entity_type, e.name as name,
                   COALESCE(e.ticker, '') as ticker,
                   r.role as role, r.relevance_score as relevance
            ORDER BY r.relevance_score DESC
        """
        result = self._run(cypher, {"slug": theme_slug})
        return [dict(r) for r in result]

    def find_bottleneck_nodes(self) -> list[dict]:
        """Find nodes with high betweenness (supply chain bottlenecks)."""
        cypher = """
            MATCH (c:Company)
            WITH c, SIZE([(c)-[:SUPPLIES_TO]->() | 1]) as out_supply,
                     SIZE([()-[:SUPPLIES_TO]->(c) | 1]) as in_supply
            WHERE out_supply + in_supply >= 3
            RETURN c.name as company, c.ticker as ticker,
                   out_supply, in_supply,
                   out_supply + in_supply as centrality
            ORDER BY centrality DESC
            LIMIT 30
        """
        result = self._run(cypher)
        return [dict(r) for r in result]

    def get_cross_sector_technologies(self, min_sectors: int = 3) -> list[dict]:
        """Technologies adopted by companies across multiple sectors — auto-detected themes.

        Returns company list alongside sectors so callers can compute company_count.
        """
        cypher = """
            MATCH (t:Technology)<-[:DEVELOPS|USES]-(c:Company)-[:PART_OF]->(s:Sector)
            WITH t, COUNT(DISTINCT s) AS sector_count,
                 COLLECT(DISTINCT s.name) AS sectors,
                 COLLECT(DISTINCT c.name) AS companies
            WHERE sector_count >= $min_sectors
            RETURN t.name AS technology, sector_count, sectors, companies,
                   COALESCE(t.mention_count, SIZE(companies)) AS mentions
            ORDER BY sector_count DESC, mentions DESC
        """
        result = self._run(cypher, {"min_sectors": min_sectors})
        return [dict(r) for r in result]

    def get_supply_chain_clusters(self, min_suppliers: int = 3) -> list[dict]:
        """Find targets with multiple suppliers across sectors — bottleneck/theme signal.

        Example output: target='AI GPU', suppliers=['Nvidia', 'AMD', 'Intel'],
        sectors=['Technology', 'Industrials'] — surfaces HBM or GPU supply theme
        without any pre-defined template.
        """
        cypher = """
            MATCH (supplier:Company)-[:SUPPLIES_TO]->(target)
            MATCH (supplier)-[:PART_OF]->(s:Sector)
            WITH target, COUNT(DISTINCT supplier) AS supplier_count,
                 COLLECT(DISTINCT supplier.name) AS suppliers,
                 COLLECT(DISTINCT s.name) AS sectors
            WHERE supplier_count >= $min_suppliers
            RETURN labels(target)[0] AS target_type,
                   COALESCE(target.name, target.name) AS target,
                   supplier_count, suppliers, sectors
            ORDER BY supplier_count DESC
            LIMIT 40
        """
        result = self._run(cypher, {"min_suppliers": min_suppliers})
        return [dict(r) for r in result]

    def get_capex_concentrated_technologies(self, min_companies: int = 3) -> list[dict]:
        """Technologies where multiple companies are making capex commitments.

        This surfaces themes like 'Nuclear Power Buildout' or 'HVDC Grid Expansion'
        purely from investment-relationship data, not from keywords.
        """
        cypher = """
            MATCH (c:Company)-[r:INVESTS_IN]->(t)
            WHERE r.investment_type IN ['capex', 'expansion', 'greenfield']
               OR r.amount_usd > 0
            WITH t, COUNT(DISTINCT c) AS company_count,
                 COLLECT(DISTINCT c.name) AS companies,
                 SUM(COALESCE(r.amount_usd, 0)) AS total_investment
            WHERE company_count >= $min_companies
            RETURN labels(t)[0] AS target_type, t.name AS target,
                   company_count, companies, total_investment
            ORDER BY total_investment DESC, company_count DESC
            LIMIT 30
        """
        result = self._run(cypher, {"min_companies": min_companies})
        return [dict(r) for r in result]

    def export_subgraph(self, theme_slug: str) -> dict:
        """Export theme subgraph as nodes + edges dict for visualization."""
        entities = self.get_theme_entities(theme_slug)
        nodes = [{"id": e["name"], "type": e["entity_type"], "role": e["role"]} for e in entities]
        cypher = """
            MATCH (a)-[r]->(b)
            WHERE (a)-[:PART_OF]->(:Theme {slug: $slug})
              AND (b)-[:PART_OF]->(:Theme {slug: $slug})
            RETURN a.name as source, type(r) as rel, b.name as target, r.weight as weight
        """
        result = self._run(cypher, {"slug": theme_slug})
        edges = [{"source": r["source"], "rel": r["rel"], "target": r["target"], "weight": r["weight"]}
                 for r in result]
        return {"nodes": nodes, "edges": edges}

    def close(self):
        self._driver.close()
        logger.info("GraphStore connection closed")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
