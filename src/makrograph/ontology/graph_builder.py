"""Builds and updates the knowledge graph from NLP extraction results."""

import logging
import re
from datetime import date
from typing import Optional

from ..nlp.entity_extractor import ExtractionResult, ExtractedEntity
from ..nlp.signal_extractor import InvestmentSignal
from .ontology_model import (
    GraphEvent, NodeType, OntologyEdge, OntologyNode, RelationType,
)

logger = logging.getLogger(__name__)

# Map entity_type strings to NodeType
ENTITY_TYPE_MAP = {
    "COMPANY": NodeType.COMPANY,
    "TECHNOLOGY": NodeType.TECHNOLOGY,
    "SECTOR": NodeType.SECTOR,
    "CONCEPT": NodeType.CONCEPT,
    "PRODUCT": NodeType.PRODUCT,
    "PERSON": NodeType.PERSON,
    "REGULATION": NodeType.REGULATION,
    "LOCATION": NodeType.LOCATION,
}

# Map signal types to graph relationships
SIGNAL_TO_RELATION = {
    "capex_increase": RelationType.INVESTS_IN,
    "technology_adoption": RelationType.USES,
    "partnership_formed": RelationType.INVESTS_IN,
    "acquisition_intent": RelationType.ACQUIRES,
    "technology_disruption": RelationType.DISRUPTS,
    "regulatory_tailwind": RelationType.REGULATED_BY,
    "regulatory_headwind": RelationType.REGULATED_BY,
}


class GraphBuilder:
    """Converts NLP extraction results into ontology nodes and edges.

    Works with both Neo4j (via GraphStore) and PostgreSQL (via PGStore)
    for synchronized storage.
    """

    def __init__(self, graph_store=None, pg_store=None):
        self.graph_store = graph_store
        self.pg_store = pg_store
        self._event_log: list[GraphEvent] = []

    def process_extraction(
        self,
        extraction: ExtractionResult,
        document_metadata: dict = None,
    ) -> tuple[list[OntologyNode], list[OntologyEdge]]:
        """Build nodes and edges from a single document's extraction result."""
        meta = document_metadata or {}
        doc_id = extraction.document_id
        company = meta.get("company", "")
        ticker = meta.get("ticker", "")
        filed_at = meta.get("filed_at")

        nodes: list[OntologyNode] = []
        edges: list[OntologyEdge] = []

        # Add the source company as a node
        if company:
            company_node = OntologyNode(
                name=company,
                node_type=NodeType.COMPANY,
                properties={"ticker": ticker},
                first_seen_at=filed_at,
                last_seen_at=filed_at or date.today(),
            )
            nodes.append(company_node)
            self._upsert_node(company_node)

        # Process each extracted entity
        entity_nodes: dict[str, OntologyNode] = {}
        for ent in extraction.entities:
            node_type = ENTITY_TYPE_MAP.get((ent.entity_type or "").upper())
            if not node_type:
                continue

            node = OntologyNode(
                name=ent.canonical_name,
                node_type=node_type,
                properties={
                    "ticker": ent.metadata.get("ticker", ""),
                    "source": ent.metadata.get("source", "nlp"),
                },
                mention_count=1,
                first_seen_at=filed_at,
                last_seen_at=filed_at or date.today(),
            )
            nodes.append(node)
            entity_nodes[ent.canonical_name] = node
            self._upsert_node(node)

            # Create edge from source company → entity
            if company and node_type in (NodeType.TECHNOLOGY, NodeType.CONCEPT):
                rel = self._infer_relation(ent, node_type)
                if rel:
                    edge = OntologyEdge(
                        source_name=company,
                        source_type=NodeType.COMPANY,
                        relation=rel,
                        target_name=ent.canonical_name,
                        target_type=node_type,
                        weight=ent.confidence,
                        first_seen_at=filed_at,
                        last_seen_at=filed_at or date.today(),
                        properties={"doc_id": doc_id},
                    )
                    edges.append(edge)
                    self._upsert_edge(edge)

        # Build co-occurrence edges between entities in same document
        tech_entities = [n for n in entity_nodes.values() if n.node_type == NodeType.TECHNOLOGY]
        if len(tech_entities) > 1:
            for i, n1 in enumerate(tech_entities):
                for n2 in tech_entities[i+1:]:
                    edge = OntologyEdge(
                        source_name=n1.name,
                        source_type=NodeType.TECHNOLOGY,
                        relation=RelationType.ENABLES,
                        target_name=n2.name,
                        target_type=NodeType.TECHNOLOGY,
                        weight=0.3,
                        properties={"co_occurrence": True, "doc_id": doc_id},
                    )
                    edges.append(edge)
                    self._upsert_edge(edge)

        logger.debug(
            f"Graph builder: {len(nodes)} nodes, {len(edges)} edges "
            f"from doc {doc_id} ({company})"
        )
        return nodes, edges

    def process_signals(
        self,
        signals: list[InvestmentSignal],
        company: str,
        document_metadata: dict = None,
    ) -> list[OntologyEdge]:
        """Convert investment signals into graph relationships."""
        meta = document_metadata or {}
        filed_at = meta.get("filed_at")
        edges: list[OntologyEdge] = []

        for sig in signals:
            rel = SIGNAL_TO_RELATION.get(sig.signal_type)
            if not rel or not sig.entity_text:
                continue

            # Infer target node type from signal type
            target_type = NodeType.TECHNOLOGY
            if "regulatory" in sig.signal_type:
                target_type = NodeType.REGULATION
            elif "partnership" in sig.signal_type or "acquisition" in sig.signal_type:
                target_type = NodeType.COMPANY

            # Ensure target node exists
            target_name = sig.entity_text.strip()
            if not target_name:
                continue

            target_node = OntologyNode(
                name=target_name,
                node_type=target_type,
                first_seen_at=filed_at,
            )
            self._upsert_node(target_node)

            edge = OntologyEdge(
                source_name=company,
                source_type=NodeType.COMPANY,
                relation=rel,
                target_name=target_name,
                target_type=target_type,
                weight=sig.confidence,
                first_seen_at=filed_at,
                last_seen_at=filed_at or date.today(),
                properties={
                    "signal_type": sig.signal_type,
                    "direction": sig.direction,
                    "signal_value": sig.signal_value,
                },
            )
            edges.append(edge)
            self._upsert_edge(edge)

        return edges

    def _infer_relation(self, entity: ExtractedEntity, node_type: NodeType) -> Optional[RelationType]:
        """Infer the most likely relationship between a company and an entity."""
        if node_type == NodeType.TECHNOLOGY:
            ctx = entity.context.lower()
            if any(w in ctx for w in ["develop", "build", "creat", "design", "pioneer"]):
                return RelationType.DEVELOPS
            elif any(w in ctx for w in ["invest", "spend", "fund", "acquir", "capex"]):
                return RelationType.INVESTS_IN
            elif any(w in ctx for w in ["deploy", "use", "adopt", "implement", "leverag"]):
                return RelationType.USES
        elif node_type == NodeType.SECTOR:
            return RelationType.PART_OF
        elif node_type == NodeType.CONCEPT:
            return RelationType.INVESTS_IN
        return None

    def _upsert_node(self, node: OntologyNode):
        """Accumulate node for batch write — no longer writes per-node.

        PG entity upserts are skipped here: entities are already in PostgreSQL
        from the NLP stage. Duplicate writes caused 2x DB overhead with no benefit.
        Neo4j writes are batched via flush_batch() for 10-50x speedup.
        """
        # Just accumulate — actual write happens in flush_batch()
        pass

    def _upsert_edge(self, edge: OntologyEdge):
        """Accumulate edge for batch write — no longer writes per-edge."""
        pass

    def flush_batch(self, nodes: list, edges: list) -> None:
        """Write all accumulated nodes+edges in ONE Neo4j session using UNWIND.

        Called once per doc-batch by run_graph() instead of per-node/edge.
        """
        if not self.graph_store or (not nodes and not edges):
            return
        try:
            node_dicts = [
                {
                    "node_type": n.node_type.value,
                    "name": n.name,
                    "props": {k: v for k, v in n.properties.items() if v},
                }
                for n in nodes
            ]
            edge_dicts = [
                {
                    "src_type":  e.source_type.value,
                    "src_name":  e.source_name,
                    "rel_type":  e.relation.value,
                    "tgt_type":  e.target_type.value,
                    "tgt_name":  e.target_name,
                    "weight":    e.weight,
                    "props":     {k: v for k, v in e.properties.items()
                                  if k != "weight_delta" and v},
                }
                for e in edges
            ]
            self.graph_store.batch_upsert_nodes_and_edges(node_dicts, edge_dicts)
        except Exception as exc:
            logger.warning(f"Batch graph flush failed: {exc}")

    def build_from_pg_entities(
        self,
        pg_entities: list[dict],
        document_metadata: dict = None,
    ) -> tuple[list[OntologyNode], list[OntologyEdge]]:
        """Build graph nodes and edges from pre-extracted PG entity rows.

        This is used when entities are already stored in PostgreSQL from the
        NLP stage — avoids re-parsing documents.

        pg_entities rows have: entity_text, entity_type, canonical_name,
        ticker, confidence, doc_mention_count.
        """
        meta = document_metadata or {}
        company = meta.get("company", "")
        ticker = meta.get("ticker", "")
        filed_at = meta.get("filed_at")
        doc_id = meta.get("id")

        nodes: list[OntologyNode] = []
        edges: list[OntologyEdge] = []

        # Source company node
        if company:
            company_node = OntologyNode(
                name=company,
                node_type=NodeType.COMPANY,
                properties={"ticker": ticker or ""},
                first_seen_at=filed_at,
                last_seen_at=filed_at or date.today(),
            )
            nodes.append(company_node)
            self._upsert_node(company_node)

        # Entity nodes + edges
        entity_nodes: dict[str, OntologyNode] = {}
        for ent in pg_entities:
            etype = (ent.get("entity_type") or "").upper()
            node_type = ENTITY_TYPE_MAP.get(etype)
            if not node_type:
                continue

            name = ent.get("canonical_name") or ent.get("entity_text", "")
            if not name:
                continue

            node = OntologyNode(
                name=name,
                node_type=node_type,
                properties={
                    "ticker": ent.get("ticker") or "",
                    "source": "pg_nlp",
                },
                mention_count=ent.get("doc_mention_count", 1),
                first_seen_at=filed_at,
                last_seen_at=filed_at or date.today(),
            )
            nodes.append(node)
            entity_nodes[name] = node
            self._upsert_node(node)

            # Company → entity edge
            if company and node_type in (NodeType.TECHNOLOGY, NodeType.CONCEPT):
                # Build a minimal ExtractedEntity-like object for _infer_relation
                class _E:
                    context = ""
                    confidence = ent.get("confidence", 0.7)
                rel = self._infer_relation(_E(), node_type)
                if rel is None:
                    rel = RelationType.INVESTS_IN
                edge = OntologyEdge(
                    source_name=company,
                    source_type=NodeType.COMPANY,
                    relation=rel,
                    target_name=name,
                    target_type=node_type,
                    weight=ent.get("confidence", 0.7),
                    first_seen_at=filed_at,
                    last_seen_at=filed_at or date.today(),
                    properties={"doc_id": doc_id},
                )
                edges.append(edge)
                self._upsert_edge(edge)

        # Tech co-occurrence edges
        tech_nodes = [n for n in entity_nodes.values() if n.node_type == NodeType.TECHNOLOGY]
        if len(tech_nodes) > 1:
            for i, n1 in enumerate(tech_nodes[:15]):   # cap at 15 to avoid O(n²) explosion
                for n2 in tech_nodes[i+1:16]:
                    edge = OntologyEdge(
                        source_name=n1.name,
                        source_type=NodeType.TECHNOLOGY,
                        relation=RelationType.ENABLES,
                        target_name=n2.name,
                        target_type=NodeType.TECHNOLOGY,
                        weight=0.3,
                        properties={"co_occurrence": True, "doc_id": doc_id},
                    )
                    edges.append(edge)
                    self._upsert_edge(edge)

        logger.info(
            f"Graph builder (pg): {len(nodes)} nodes, {len(edges)} edges "
            f"from doc {doc_id} ({company})"
        )
        return nodes, edges

    def get_event_log(self) -> list[GraphEvent]:
        return self._event_log

    def clear_event_log(self):
        self._event_log.clear()
