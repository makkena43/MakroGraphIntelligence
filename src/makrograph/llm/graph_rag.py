"""GraphRAG: Graph-Augmented Retrieval for Investment Reasoning.

Architecture:
    Query → Neo4j Subgraph Extraction → Context Serialization
          → Multi-hop Traversal → LLM Reasoning → Structured Answer

GraphRAG answers questions that require traversing the knowledge graph,
e.g.:
    "Which companies are exposed to AI chip supply constraints?"
    "Trace the full supply chain impact of a TSMC production cut."
    "What's the investment implication of the energy-transition theme
     across industrials and utilities?"

Unlike plain RAG (embedding similarity), GraphRAG uses graph structure
to surface non-obvious indirect relationships via multi-hop traversal.

Three reasoning modes:
    LOCAL   — answer from a single entity's 1-2 hop neighbourhood
    GLOBAL  — answer from community summaries (all connected entities)
    HYBRID  — local + global merged

Reference: Inspired by Microsoft GraphRAG (arxiv.org/abs/2404.16130)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

MAX_CONTEXT_NODES = 40
MAX_CONTEXT_EDGES = 80


@dataclass
class GraphContext:
    """Serialized subgraph context for LLM input."""
    query: str
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    community_summaries: list[str] = field(default_factory=list)
    temporal_facts: list[dict] = field(default_factory=list)
    signal_evidence: list[dict] = field(default_factory=list)

    def to_prompt_context(self, max_chars: int = 3000) -> str:
        """Serialize graph context to a compact LLM-readable string."""
        lines = [f"## Knowledge Graph Context for: '{self.query}'\n"]

        if self.nodes:
            lines.append(f"### Entities ({len(self.nodes)}):")
            for n in self.nodes[:MAX_CONTEXT_NODES]:
                props = ", ".join(f"{k}={v}" for k, v in n.items()
                                  if k not in ("name", "type") and v)
                lines.append(f"  [{n.get('type','?')}] {n.get('name','')} {f'({props})' if props else ''}")

        if self.edges:
            lines.append(f"\n### Relationships ({len(self.edges)}):")
            for e in self.edges[:MAX_CONTEXT_EDGES]:
                lines.append(
                    f"  {e.get('source','')} --[{e.get('rel','')}]--> {e.get('target','')}"
                    + (f"  (weight={e.get('weight',1):.1f})" if e.get("weight") else "")
                )

        if self.temporal_facts:
            lines.append(f"\n### Temporal Facts (recent changes):")
            for tf in self.temporal_facts[:10]:
                lines.append(
                    f"  {tf.get('source','')} --[{tf.get('relationship','')}]--> "
                    f"{tf.get('target','')} | valid: {tf.get('created','')} → {tf.get('expired','active')}"
                )

        if self.signal_evidence:
            lines.append(f"\n### Signal Evidence:")
            for sig in self.signal_evidence[:10]:
                lines.append(
                    f"  [{sig.get('signal_type','')}] {sig.get('company','')} "
                    f"— {sig.get('context_text','')[:120]}"
                )

        if self.community_summaries:
            lines.append(f"\n### Community Summaries:")
            for summ in self.community_summaries[:3]:
                lines.append(f"  • {summ}")

        result = "\n".join(lines)
        return result[:max_chars]


@dataclass
class GraphRAGAnswer:
    """Structured answer from GraphRAG reasoning."""
    query: str
    answer: str
    confidence: str = "medium"      # low | medium | high
    evidence_nodes: list[str] = field(default_factory=list)
    reasoning_path: list[str] = field(default_factory=list)
    follow_up_questions: list[str] = field(default_factory=list)
    graph_context_size: int = 0


class GraphRAG:
    """Graph-augmented retrieval and reasoning engine.

    Combines:
        - Neo4j subgraph extraction (structural context)
        - Graphiti temporal facts (bi-temporal evidence)
        - PostgreSQL signal evidence (quantified signals)
        - LLM multi-hop reasoning over the combined context
    """

    def __init__(
        self,
        graph_store=None,
        graphiti_store=None,
        pg_store=None,
        llm_reasoner=None,
        config: dict = None,
    ):
        self.graph_store = graph_store
        self.graphiti_store = graphiti_store
        self.pg_store = pg_store
        self.llm = llm_reasoner
        cfg = config or {}
        self.max_hops = cfg.get("graph_rag_max_hops", 2)
        self.mode = cfg.get("graph_rag_mode", "hybrid")      # local | global | hybrid
        self.max_context_chars = cfg.get("graph_rag_context_chars", 3000)

    def answer(self, query: str, entity_hint: str = None) -> GraphRAGAnswer:
        """Answer a natural-language query using graph-augmented reasoning."""
        context = self._build_context(query, entity_hint)
        answer_text = self._reason(query, context)

        return GraphRAGAnswer(
            query=query,
            answer=answer_text or "Insufficient graph context to answer.",
            evidence_nodes=[n.get("name", "") for n in context.nodes[:5]],
            graph_context_size=len(context.nodes) + len(context.edges),
        )

    def explain_theme(self, theme_slug: str) -> GraphRAGAnswer:
        """Generate a multi-hop explanation of an investment theme."""
        if not self.graph_store:
            return GraphRAGAnswer(query=theme_slug, answer="Graph store unavailable.")

        try:
            entities = self.graph_store.get_theme_entities(theme_slug)
        except Exception:
            entities = []

        context = GraphContext(query=f"Explain investment theme: {theme_slug}")
        context.nodes = [{"name": e["name"], "type": e["entity_type"],
                          "role": e.get("role", "")} for e in entities]

        # Build edges within the theme subgraph
        try:
            subgraph = self.graph_store.export_subgraph(theme_slug)
            context.edges = subgraph.get("edges", [])
        except Exception:
            pass

        # Add signal evidence
        context.signal_evidence = self._load_signal_evidence(
            [e["name"] for e in entities if e.get("entity_type") == "COMPANY"]
        )

        answer_text = self._reason(
            f"Provide a detailed investment thesis for the theme '{theme_slug}'. "
            "Explain the structural drivers, key beneficiaries, supply chain linkages, "
            "and the biggest risks.",
            context,
        )
        return GraphRAGAnswer(
            query=theme_slug,
            answer=answer_text or "",
            evidence_nodes=[n["name"] for n in context.nodes[:10]],
            graph_context_size=len(context.nodes) + len(context.edges),
        )

    def trace_supply_chain_risk(
        self, company: str, risk_type: str = "supply_bottleneck"
    ) -> GraphRAGAnswer:
        """Trace supply chain exposure to a specific risk type via multi-hop traversal."""
        context = GraphContext(query=f"Supply chain risk for {company}: {risk_type}")

        if self.graph_store:
            try:
                chain = self.graph_store.get_supply_chain(company, depth=self.max_hops)
                for item in chain:
                    context.nodes.append({
                        "name": item.get("company", ""),
                        "type": "Company",
                        "depth": item.get("depth_level", 0),
                    })
            except Exception:
                pass

        context.signal_evidence = self._load_signal_evidence([company], signal_type=risk_type)

        if self.graphiti_store and self.graphiti_store.is_available:
            context.temporal_facts = self.graphiti_store.query_at(
                date.today(), f"supply chain {company}"
            )

        answer_text = self._reason(
            f"Analyze the supply chain risk '{risk_type}' for {company}. "
            "Identify which upstream/downstream companies are affected, "
            "the severity of the risk, and which other public companies are exposed.",
            context,
        )
        return GraphRAGAnswer(
            query=f"{company} supply chain risk",
            answer=answer_text or "",
            evidence_nodes=[n["name"] for n in context.nodes],
            graph_context_size=len(context.nodes),
        )

    def find_cross_theme_opportunities(self, min_sectors: int = 3) -> GraphRAGAnswer:
        """Identify companies at the intersection of multiple themes."""
        context = GraphContext(query="Cross-theme investment opportunities")

        if self.graph_store:
            try:
                cross = self.graph_store.get_cross_sector_technologies(min_sectors=min_sectors)
                for item in cross:
                    context.nodes.append({
                        "name": item["technology"],
                        "type": "Technology",
                        "sector_count": item["sector_count"],
                        "sectors": ", ".join(item.get("sectors", [])),
                    })
            except Exception:
                pass

        answer_text = self._reason(
            "Identify the top investment opportunities at the intersection of multiple "
            f"themes (minimum {min_sectors} sectors). For each opportunity, explain "
            "the investment thesis and list the top 3 public company beneficiaries.",
            context,
        )
        return GraphRAGAnswer(
            query="cross-theme opportunities",
            answer=answer_text or "",
            evidence_nodes=[n["name"] for n in context.nodes[:10]],
            graph_context_size=len(context.nodes),
        )

    # ----------------------------------------------------------
    # CONTEXT BUILDERS
    # ----------------------------------------------------------
    def _build_context(self, query: str, entity_hint: str = None) -> GraphContext:
        """Build a comprehensive graph context for an arbitrary query."""
        context = GraphContext(query=query)

        if self.graph_store and entity_hint:
            # Local: get neighbourhood of the entity
            try:
                if self.mode in ("local", "hybrid"):
                    for node_type in ("Company", "Technology", "Concept"):
                        companies = self.graph_store.get_companies_by_technology(entity_hint)
                        for c in companies[:10]:
                            context.nodes.append({
                                "name": c.get("company", ""),
                                "type": "Company",
                                "ticker": c.get("ticker", ""),
                            })
                            context.edges.append({
                                "source": c["company"],
                                "rel": c.get("relationship", "RELATES_TO"),
                                "target": entity_hint,
                                "weight": c.get("weight", 1.0),
                            })
            except Exception as e:
                logger.debug(f"Context build (local) warning: {e}")

            if self.mode in ("global", "hybrid"):
                try:
                    bottlenecks = self.graph_store.find_bottleneck_nodes()
                    context.community_summaries = [
                        f"{b['company']} is a supply-chain hub "
                        f"(centrality={b['centrality']}, supplies_to={b['out_supply']})"
                        for b in bottlenecks[:5]
                    ]
                except Exception:
                    pass

        # Add temporal facts from Graphiti
        if self.graphiti_store and self.graphiti_store.is_available and entity_hint:
            context.temporal_facts = self.graphiti_store.query_at(date.today(), query)

        # Add signal evidence from PostgreSQL
        if entity_hint:
            context.signal_evidence = self._load_signal_evidence([entity_hint])

        return context

    def _load_signal_evidence(
        self, companies: list[str], signal_type: str = None, days: int = 90
    ) -> list[dict]:
        """Load relevant investment signals from PostgreSQL."""
        if not self.pg_store or not companies:
            return []
        try:
            signals = []
            types_to_check = (
                [signal_type] if signal_type
                else ["capex_increase", "demand_surge", "supply_bottleneck",
                      "technology_adoption", "regulatory_tailwind"]
            )
            for stype in types_to_check:
                batch = self.pg_store.get_signals_by_type(stype, days=days)
                for sig in batch:
                    if sig.get("company") in companies or not companies:
                        signals.append(sig)
            return signals[:20]
        except Exception as e:
            logger.debug(f"Signal evidence load warning: {e}")
            return []

    # ----------------------------------------------------------
    # LLM REASONING
    # ----------------------------------------------------------
    def _reason(self, question: str, context: GraphContext) -> Optional[str]:
        """Pass graph context + question to LLM for reasoning."""
        if not self.llm or not self.llm.enabled:
            logger.debug("LLM disabled. GraphRAG reasoning skipped.")
            return None

        ctx_str = context.to_prompt_context(max_chars=self.max_context_chars)
        prompt = (
            f"You are an expert investment analyst with access to a live knowledge graph "
            f"of companies, technologies, supply chains, and investment signals extracted "
            f"from SEC filings and earnings calls.\n\n"
            f"{ctx_str}\n\n"
            f"## Question\n{question}\n\n"
            f"## Instructions\n"
            f"- Use only the graph context provided above\n"
            f"- Cite specific entities and relationships from the context\n"
            f"- Be concise but complete (max 4 paragraphs)\n"
            f"- End with: CONFIDENCE: [low/medium/high]\n\n"
            f"## Answer"
        )

        return self.llm._call_llm(prompt, "graph_rag_reasoning")

    def batch_theme_analysis(self, theme_slugs: list[str]) -> list[GraphRAGAnswer]:
        """Generate GraphRAG analysis for multiple themes."""
        answers = []
        for slug in theme_slugs:
            answer = self.explain_theme(slug)
            answers.append(answer)
            logger.debug(f"GraphRAG answered for theme: {slug}")
        return answers
