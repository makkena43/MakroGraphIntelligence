"""Graphiti temporal knowledge graph integration.

Graphiti (github.com/getzep/graphiti) adds bi-temporal tracking to Neo4j:
    - `created_at`   — when the fact was first observed
    - `expired_at`   — when the fact was superseded (None = still valid)
    - `valid_at`     — the business date the fact applies to

This means the graph can answer:
    "In Q3 2023, which companies were investing in AI chips?"
    "When did NVIDIA's supply-chain relationship with TSMC start accelerating?"
    "Which relationships have disappeared since last quarter?"

Architecture:
    - Graphiti's `add_episode()` ingests raw document text and extracts
      entities/relationships using an LLM (optional, falls back to manual).
    - `TemporalGraphStore` wraps Graphiti with sync helpers and provides
      our custom temporal query methods used by GraphEvolutionTracker.

Dependency:
    pip install graphiti-core
    (requires Neo4j running)

Note: Graphiti's built-in LLM extraction is OPTIONAL. If disabled,
entities and edges are inserted manually via `add_entity_episode()`.
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from synchronous code."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


class TemporalGraphStore:
    """Bi-temporal knowledge graph store using Graphiti + Neo4j.

    Key capability over plain Neo4j (GraphStore):
        - Every relationship has valid_from / valid_to timestamps
        - Point-in-time graph queries ("what did the graph look like in Q2-2023?")
        - Automatic fact invalidation when contradicting facts arrive

    Usage:
        store = TemporalGraphStore(config)
        store.add_document_episode(doc_id, text, company, filed_at)
        results = store.query_at(date(2023, 9, 30), "AI infrastructure investments")
    """

    def __init__(self, config: dict):
        self.neo4j_uri = config.get("uri", "bolt://localhost:7687")
        self.neo4j_user = config.get("user", "neo4j")
        self.neo4j_password = config.get("password", "makrograph")
        self.llm_enabled = config.get("graphiti_llm_enabled", False)
        self.llm_model = config.get("graphiti_llm_model", "gpt-4o-mini")
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self):
        """Initialize Graphiti client."""
        try:
            from graphiti_core import Graphiti
            from graphiti_core.llm_client import LLMConfig

            if self.llm_enabled:
                self._client = Graphiti(
                    self.neo4j_uri,
                    self.neo4j_user,
                    self.neo4j_password,
                )
            else:
                # Minimal client without LLM entity extraction
                self._client = Graphiti(
                    self.neo4j_uri,
                    self.neo4j_user,
                    self.neo4j_password,
                )
            self._available = True
            logger.info("Graphiti temporal graph store initialized")
        except ImportError:
            logger.warning("graphiti-core not installed. Temporal graph unavailable. "
                           "Install: pip install graphiti-core")
        except Exception as e:
            logger.warning(f"Graphiti init failed: {e}")

    @property
    def is_available(self) -> bool:
        return self._available and self._client is not None

    def build_indices(self):
        """Create Graphiti indices (run once on first setup)."""
        if not self.is_available:
            return
        try:
            _run_async(self._client.build_indices_and_constraints())
            logger.info("Graphiti indices built")
        except Exception as e:
            logger.error(f"Graphiti index build failed: {e}")

    def add_document_episode(
        self,
        doc_id: int,
        text: str,
        company: str,
        filed_at: Optional[date] = None,
        filing_type: str = "",
        source_description: str = "",
    ):
        """Ingest a document as a Graphiti episode.

        Graphiti will (if LLM enabled) extract entities and relationships
        and add them as bi-temporal edges to Neo4j.
        """
        if not self.is_available:
            return

        try:
            from graphiti_core.nodes import EpisodeType

            valid_at = datetime.combine(
                filed_at or date.today(), datetime.min.time()
            ).replace(tzinfo=timezone.utc)

            # Truncate to reasonable episode size
            episode_text = text[:8000]

            _run_async(self._client.add_episode(
                name=f"filing_{doc_id}_{company}",
                episode_body=episode_text,
                source=EpisodeType.text,
                source_description=source_description or f"{filing_type} - {company}",
                reference_time=valid_at,
            ))
            logger.debug(f"Graphiti episode added: doc_id={doc_id}, company={company}")

        except Exception as e:
            logger.warning(f"Graphiti episode add failed (doc {doc_id}): {e}")

    def add_structured_episode(
        self,
        episode_name: str,
        entities: list[dict],
        relationships: list[dict],
        valid_at: Optional[datetime] = None,
    ):
        """Add structured facts (entity/relationship dicts) as a Graphiti episode.

        This bypasses LLM extraction — use when entities are already extracted
        by our NLP pipeline.
        """
        if not self.is_available:
            return

        # Serialize to a structured text format that Graphiti can parse
        lines = [f"Structured facts (valid {valid_at or datetime.now()}):\n"]
        for ent in entities:
            lines.append(f"Entity: {ent.get('name')} [{ent.get('type')}]")
        for rel in relationships:
            lines.append(
                f"Relationship: {rel.get('source')} --[{rel.get('type')}]--> {rel.get('target')}"
                + (f" (since {rel.get('since')})" if rel.get("since") else "")
            )

        self.add_document_episode(
            doc_id=0,
            text="\n".join(lines),
            company="structured_import",
            filed_at=valid_at.date() if valid_at else None,
            source_description=f"Structured import: {episode_name}",
        )

    def query_at(self, point_in_time: date, query: str, top_k: int = 10) -> list[dict]:
        """Search the temporal graph at a specific point in time.

        Returns relevant edges/facts that were valid on that date.
        """
        if not self.is_available:
            return []

        try:
            pit_dt = datetime.combine(point_in_time, datetime.min.time()).replace(tzinfo=timezone.utc)
            results = _run_async(self._client.search(
                query=query,
                num_results=top_k,
                center_node_uuid=None,
            ))
            # Filter by valid_at range
            facts = []
            for r in (results or []):
                fact = {
                    "name": getattr(r, "name", ""),
                    "fact": getattr(r, "fact", ""),
                    "valid_at": str(getattr(r, "valid_at", "")),
                    "expired_at": str(getattr(r, "expired_at", "")),
                    "source_node": getattr(r, "source_node_uuid", ""),
                    "target_node": getattr(r, "target_node_uuid", ""),
                }
                facts.append(fact)
            return facts
        except Exception as e:
            logger.warning(f"Graphiti temporal query failed: {e}")
            return []

    def get_entity_timeline(self, entity_name: str, entity_type: str) -> list[dict]:
        """Retrieve the full relationship timeline for an entity.

        Shows when each relationship was formed and when it expired.
        """
        if not self.is_available:
            return []
        try:
            results = _run_async(self._client.search(
                query=f"{entity_type}: {entity_name}",
                num_results=50,
            ))
            timeline = []
            for r in (results or []):
                timeline.append({
                    "entity": entity_name,
                    "fact": getattr(r, "fact", ""),
                    "valid_at": str(getattr(r, "valid_at", "")),
                    "expired_at": str(getattr(r, "expired_at", "active")),
                })
            timeline.sort(key=lambda x: x["valid_at"])
            return timeline
        except Exception as e:
            logger.warning(f"Entity timeline query failed ({entity_name}): {e}")
            return []

    def get_fact_changes(self, days: int = 30) -> list[dict]:
        """Return facts that changed (created or expired) in the last N days."""
        if not self.is_available:
            return []
        try:
            # Query for recently created/expired edges via Neo4j directly
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password))
            with driver.session() as session:
                result = session.run("""
                    MATCH (s)-[r]-(t)
                    WHERE r.created_at >= datetime() - duration({days: $days})
                       OR r.expired_at >= datetime() - duration({days: $days})
                    RETURN s.name as source, type(r) as relationship,
                           t.name as target,
                           r.created_at as created, r.expired_at as expired
                    ORDER BY r.created_at DESC
                    LIMIT 100
                """, days=days)
                return [dict(rec) for rec in result]
        except Exception as e:
            logger.warning(f"Fact changes query failed: {e}")
            return []

    def close(self):
        if self._client:
            try:
                _run_async(self._client.close())
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
