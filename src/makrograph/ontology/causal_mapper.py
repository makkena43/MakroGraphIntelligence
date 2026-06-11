"""Causal Ontology Layer — maps thematic cause/effect chains.

Models multi-hop probabilistic chain reactions between macro themes.

Example canonical chains:
    AI Datacenter Buildout
      → ENABLES → Power Demand Surge          (lag 90d)
      → ENABLES → Transformer Shortage        (lag 180d)
      → ENABLES → Copper Demand Increase      (lag 270d)
      → ENABLES → Mining Capex Cycle          (lag 360d)

    Semiconductor Supply Reshoring
      → DEPENDS_ON → Advanced Packaging       (lag 60d)
      → DEPENDS_ON → CoWoS / HBM Wafer        (lag 90d)
      → CONSTRAINS → GPU Supply               (lag 120d)

Causal chains are scored by how many of their upstream signals
are currently active in the database, producing an activation_score (0-100).
"""

import json
import logging
from collections import defaultdict
from datetime import date
from typing import Optional

from ..ontology.ontology_model import (
    CausalChain, CausalLink, NodeType, RelationType,
)

logger = logging.getLogger(__name__)


def _build_chain(chain_def: dict) -> CausalChain:
    """Parse a chain definition dict into a CausalChain dataclass."""
    links = []
    for ldef in chain_def.get("links", []):
        rel_name = ldef.get("relation", "ENABLES")
        try:
            rel = RelationType(rel_name)
        except ValueError:
            rel = RelationType.ENABLES

        cause_type_str = ldef.get("cause_type", "TECHNOLOGY").title()
        effect_type_str = ldef.get("effect_type", "CONCEPT").title()
        try:
            cause_type = NodeType(cause_type_str)
        except ValueError:
            cause_type = NodeType.CONCEPT
        try:
            effect_type = NodeType(effect_type_str)
        except ValueError:
            effect_type = NodeType.CONCEPT

        links.append(CausalLink(
            cause=ldef["cause"],
            cause_type=cause_type,
            effect=ldef["effect"],
            effect_type=effect_type,
            mechanism=ldef.get("mechanism", ""),
            probability=ldef.get("probability", 0.7),
            lag_days=ldef.get("lag_days", 90),
            relation=rel,
        ))

    return CausalChain(
        chain_id=chain_def["chain_id"],
        name=chain_def["name"],
        description=chain_def.get("description", ""),
        links=links,
    )


class CausalMapper:
    """Scores causal chains based on active signals and entities in the database.

    Activation scoring:
        - For each link in a chain, check if the cause entity is present
          in recent signals/entities from PostgreSQL.
        - activation_score = weighted sum of active links / total links × 100
        - Probability weights reduce score for less-certain links.
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._chains: list[CausalChain] = []
        logger.info("CausalMapper initialised — chains will be discovered from signal data")

    @property
    def chains(self) -> list[CausalChain]:
        return self._chains

    # Alias map: chain library cause name → alternative entity names that
    # mean the same thing but may be stored differently in the DB.
    _CAUSE_ALIASES: dict[str, list[str]] = {
        "artificial intelligence": ["ai", "a.i.", "ai technology"],
        "generative ai":           ["gen ai", "genai", "llm", "large language model"],
        "electric vehicle":        ["ev", "evs", "electric vehicles", "bev"],
        "data center":             ["datacenter", "data centres", "data centre"],
        "machine learning":        ["ml", "deep learning"],
        "semiconductor":           ["chip", "chips", "microchip", "microchips", "ic"],
        "power demand":            ["electricity demand", "power consumption", "power", "electricity", "energy demand", "grid demand"],
        "copper demand":           ["copper", "copper supply"],
        "transformer shortage":    ["grid transformer", "electrical transformer"],
        "mining capex":            ["mining investment", "mine capex"],
        "cloud":                   ["cloud computing", "hyperscaler", "hyperscalers"],
        "cybersecurity":           ["cyber security", "information security", "infosec"],
        "lithium":                 ["lithium carbonate", "lithium hydroxide", "li"],
        "battery":                 ["batteries", "battery storage", "battery pack"],
        "gpu":                     ["graphics card", "graphics processing unit"],
        "wafer":                   ["silicon wafer", "wafers"],
    }

    def score_chains(self, active_entities: set[str], active_signals: list[dict]) -> list[CausalChain]:
        """Score all chains given currently active entities and signals.

        Args:
            active_entities: set of canonical entity names seen in recent docs
            active_signals: list of signal dicts from mg_signals

        Returns:
            Chains sorted by activation_score descending
        """
        signal_entity_names = {
            s.get("canonical_name", "").lower()
            for s in active_signals
            if s.get("canonical_name")
        }
        all_active = {e.lower() for e in active_entities} | signal_entity_names

        def _is_active(cause_name: str) -> bool:
            """Check if a cause entity is active, trying primary name + aliases.

            Matching rules (ordered by precision — stops at first hit):
              1. Exact match against the active-entity set.
              2. Exact alias match (from _CAUSE_ALIASES).
              3. Partial substring match — ONLY for multi-word phrases (≥ 2 words
                 AND ≥ 8 characters).  Single tokens like "materials", "data" are
                 far too generic and produce false positives against every entity
                 that contains those tokens → inflated 100-pt scores.
              4. Alias partial substring (same multi-word guard).
            """
            primary = cause_name.lower().strip()
            # 1. Exact match
            if primary in all_active:
                return True
            # 2. Exact alias match
            for alias in self._CAUSE_ALIASES.get(primary, []):
                if alias in all_active:
                    return True
            # 3. Partial substring — only for multi-word phrases
            words = primary.split()
            if len(words) >= 2 and len(primary) >= 8:
                for active_name in all_active:
                    if len(active_name) >= 4 and (primary in active_name or active_name in primary):
                        return True
            # 4. Alias partial substring (same guard)
            for alias in self._CAUSE_ALIASES.get(primary, []):
                alias_words = alias.split()
                if len(alias_words) >= 2 and len(alias) >= 8:
                    for active_name in all_active:
                        if len(active_name) >= 4 and alias in active_name:
                            return True
            return False

        for chain in self._chains:
            if not chain.links:
                continue

            # Minimum chain depth = 2 links (3 nodes: cause → intermediate → effect).
            # Single-hop chains (A → B) are too speculative for production surfacing.
            # They get zero score and are excluded from the active-chain display.
            if len(chain.links) < 2:
                chain.activation_score = 0.0
                continue

            # Auto-discovered chains already carry evidence-based scores from
            # discover_chains_from_data().  Re-scoring them with the entity-presence
            # heuristic would overwrite that calibrated score with a noisier estimate.
            # Preserve their score; only re-score the static library chains.
            if chain.chain_id.startswith("data-"):
                continue  # keep discovery-time score

            total_weight = sum(lnk.probability for lnk in chain.links)
            active_weight = sum(
                lnk.probability
                for lnk in chain.links
                if _is_active(lnk.cause)
            )
            chain.activation_score = round(
                (active_weight / total_weight) * 100.0 if total_weight > 0 else 0.0,
                1,
            )

        self._chains.sort(key=lambda c: c.activation_score, reverse=True)
        return self._chains

    def get_second_order_beneficiaries(self, theme_slug: str) -> list[dict]:
        """Return entities 2+ hops downstream from a theme's anchor entity.

        These are the hidden infrastructure beneficiaries that
        markets typically under-appreciate early in a cycle.
        """
        beneficiaries = []
        for chain in self._chains:
            if theme_slug.lower() in chain.chain_id.lower() or \
               theme_slug.lower().replace("-", " ") in chain.name.lower():
                for depth, link in enumerate(chain.links):
                    if depth >= 1:  # skip direct (depth=0)
                        beneficiaries.append({
                            "entity": link.effect,
                            "entity_type": link.effect_type.value,
                            "hop": depth + 1,
                            "mechanism": link.mechanism,
                            "probability": link.probability,
                            "lag_days": link.lag_days,
                            "chain": chain.name,
                        })
        return beneficiaries

    def discover_chains_from_data(self, pg_store, as_of_date=None, lookback_days: int = 730) -> list[CausalChain]:
        """Auto-discover causal chains from actual signal sequences in the database.

        Algorithm — mines five causal patterns from signal data:

        Pattern 1 — Demand → Supply Constraint (company filings)
            Entity with demand_surge AND supply_bottleneck across ≥3 companies.

        Pattern 2 — Demand Surge → Capex Response (company filings)
            Demand surge followed by capex_increase in the same sector.

        Pattern 3 — Technology Adoption → Infrastructure Buildout (company filings)
            Tech adoption signals driving downstream demand surge.

        Pattern 4 — Policy/Macro Source Demand+Supply Tension
            Same as Pattern 1 but from RBI/PIB/Invest India/SEBI documents,
            counted by document count (≥2 docs) instead of company count.

        Pattern 5 — Policy-Confirmed + Company-Corroborated Demand
            Entities with regulatory_tailwind or demand_surge in policy docs
            AND demand_surge in company filings — highest cross-source confidence.

        All discovered chains are added to self._chains, scored, and returned.
        """
        from datetime import date as _date, timedelta as _td
        import re as _re

        if as_of_date is None:
            as_of_date = _date.today()
        floor = as_of_date - _td(days=lookback_days)

        discovered: list[CausalChain] = []
        existing_ids = {c.chain_id for c in self._chains}
        tension_pairs = []
        demand_capex_pairs = []
        tech_downstream = []
        policy_tension = []
        policy_company_confirmed = []

        try:
            with pg_store._conn() as conn:
                from psycopg2.extras import RealDictCursor
                with conn.cursor(cursor_factory=RealDictCursor) as cur:

                    # ─── Pattern 1: Demand + Supply Tension pairs ─────────────────
                    # Find entities that have BOTH demand_surge AND supply_bottleneck
                    # signals across multiple companies — high-conviction 2-hop chain.
                    cur.execute("""
                        WITH demand_entities AS (
                            SELECT e.canonical_name,
                                   COUNT(DISTINCT d.company) AS demand_cos,
                                   COUNT(*)                  AS demand_signals,
                                   MIN(s.filed_at)           AS first_seen,
                                   MAX(s.filed_at)           AS last_seen
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'demand_surge'
                              AND s.filed_at BETWEEN %s AND %s
                              AND e.entity_type IN ('TECHNOLOGY','PRODUCT','SECTOR','CONCEPT')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT d.company) >= 3
                        ),
                        supply_entities AS (
                            SELECT e.canonical_name,
                                   COUNT(DISTINCT d.company) AS supply_cos,
                                   COUNT(*)                  AS supply_signals,
                                   MIN(s.filed_at)           AS first_seen
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'supply_bottleneck'
                              AND s.filed_at BETWEEN %s AND %s
                              AND e.entity_type IN ('TECHNOLOGY','PRODUCT','SECTOR','CONCEPT')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT d.company) >= 2
                        )
                        SELECT d.canonical_name,
                               d.demand_cos, d.demand_signals, d.first_seen AS demand_first,
                               s.supply_cos, s.supply_signals, s.first_seen AS supply_first
                        FROM demand_entities d
                        JOIN supply_entities s ON lower(d.canonical_name) = lower(s.canonical_name)
                        ORDER BY (d.demand_signals + s.supply_signals) DESC
                        LIMIT 40
                    """, (floor, as_of_date, floor, as_of_date))
                    tension_pairs = cur.fetchall()

                    # ─── Pattern 2: Demand entities with downstream capex ─────────
                    # Entities showing demand_surge that later trigger capex_increase
                    # across the same set of companies (capital deployed to solve shortage).
                    cur.execute("""
                        WITH demand_ents AS (
                            SELECT e.canonical_name,
                                   e.entity_type,
                                   COUNT(DISTINCT d.company)  AS n_cos,
                                   COUNT(*)                   AS n_demand,
                                   MIN(s.filed_at)            AS first_demand,
                                   MAX(s.filed_at)            AS last_demand
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'demand_surge'
                              AND s.filed_at BETWEEN %s AND %s
                              AND e.entity_type IN ('TECHNOLOGY','PRODUCT','SECTOR')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name, e.entity_type
                            HAVING COUNT(DISTINCT d.company) >= 4
                        ),
                        capex_ents AS (
                            SELECT e.canonical_name,
                                   COUNT(DISTINCT d.company)  AS n_cos,
                                   COUNT(*)                   AS n_capex,
                                   MIN(s.filed_at)            AS first_capex
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'capex_increase'
                              AND s.filed_at BETWEEN %s AND %s
                              AND e.entity_type IN ('TECHNOLOGY','PRODUCT','SECTOR')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT d.company) >= 3
                        )
                        SELECT d.canonical_name          AS demand_entity,
                               d.entity_type,
                               d.n_cos                   AS demand_cos,
                               d.n_demand,
                               d.first_demand,
                               c.canonical_name          AS capex_entity,
                               c.n_capex,
                               c.first_capex
                        FROM demand_ents d
                        JOIN capex_ents c ON lower(d.canonical_name) = lower(c.canonical_name)
                        ORDER BY d.n_demand DESC
                        LIMIT 30
                    """, (floor, as_of_date, floor, as_of_date))
                    demand_capex_pairs = cur.fetchall()

                    # ─── Pattern 3: Technology adoption → downstream demand ────────
                    # Technologies with strong adoption signals paired with entities
                    # that show downstream demand_surge (infrastructure pull-through).
                    cur.execute("""
                        WITH tech_adoption AS (
                            SELECT e.canonical_name AS tech,
                                   COUNT(DISTINCT d.company)  AS n_cos,
                                   COUNT(*)                   AS n_adopt,
                                   MIN(s.filed_at)            AS first_seen
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'technology_adoption'
                              AND s.filed_at BETWEEN %s AND %s
                              AND e.entity_type = 'TECHNOLOGY'
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT d.company) >= 4
                            ORDER BY COUNT(*) DESC
                            LIMIT 25
                        ),
                        downstream_demand AS (
                            SELECT e.canonical_name AS downstream,
                                   COUNT(DISTINCT d.company)  AS n_cos,
                                   COUNT(*)                   AS n_demand
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type IN ('demand_surge','capex_increase')
                              AND s.filed_at BETWEEN %s AND %s
                              AND e.entity_type IN ('TECHNOLOGY','CONCEPT','SECTOR')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT d.company) >= 3
                        )
                        SELECT t.tech, t.n_cos AS tech_cos, t.n_adopt, t.first_seen,
                               d.downstream, d.n_cos AS down_cos, d.n_demand
                        FROM tech_adoption t
                        CROSS JOIN downstream_demand d
                        WHERE lower(t.tech) != lower(d.downstream)
                        ORDER BY t.n_adopt DESC, d.n_demand DESC
                        LIMIT 60
                    """, (floor, as_of_date, floor, as_of_date))
                    tech_downstream = cur.fetchall()

                    # ─── Pattern 4: Policy/macro source demand+supply tension ─────
                    # Same as Pattern 1 but for macro/policy documents (RBI, PIB,
                    # Invest India, SEBI) which have no company field.
                    # Counts by document instead of by distinct company.
                    cur.execute("""
                        WITH policy_sources AS (
                            SELECT id FROM mg_documents
                            WHERE source_name IN (
                                'rbi_india','pib_india','invest_india','sebi_india'
                            )
                            AND filed_at BETWEEN %s AND %s
                        ),
                        policy_demand AS (
                            SELECT e.canonical_name,
                                   COUNT(DISTINCT s.document_id) AS n_docs,
                                   COUNT(*)                      AS n_signals,
                                   MIN(s.filed_at)               AS first_seen
                            FROM mg_signals s
                            JOIN policy_sources ps ON ps.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'demand_surge'
                              AND e.entity_type IN ('TECHNOLOGY','PRODUCT','SECTOR','CONCEPT')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT s.document_id) >= 2
                        ),
                        policy_supply AS (
                            SELECT e.canonical_name,
                                   COUNT(DISTINCT s.document_id) AS n_docs,
                                   COUNT(*)                      AS n_signals,
                                   MIN(s.filed_at)               AS first_seen
                            FROM mg_signals s
                            JOIN policy_sources ps ON ps.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'supply_bottleneck'
                              AND e.entity_type IN ('TECHNOLOGY','PRODUCT','SECTOR','CONCEPT')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT s.document_id) >= 1
                        )
                        SELECT d.canonical_name,
                               d.n_docs  AS demand_docs,
                               d.n_signals AS demand_signals,
                               d.first_seen AS demand_first,
                               s.n_docs  AS supply_docs,
                               s.n_signals AS supply_signals,
                               s.first_seen AS supply_first
                        FROM policy_demand d
                        JOIN policy_supply s ON lower(d.canonical_name) = lower(s.canonical_name)
                        ORDER BY (d.n_signals + s.n_signals) DESC
                        LIMIT 40
                    """, (floor, as_of_date))
                    policy_tension = cur.fetchall()

                    # ─── Pattern 5: Regulatory tailwind from policy docs → demand ─
                    # Entities mentioned with regulatory_tailwind in policy sources
                    # and demand_surge in company filings = policy-confirmed demand theme.
                    cur.execute("""
                        WITH policy_sources AS (
                            SELECT id FROM mg_documents
                            WHERE source_name IN (
                                'rbi_india','pib_india','invest_india','sebi_india'
                            )
                            AND filed_at BETWEEN %s AND %s
                        ),
                        policy_tailwind AS (
                            SELECT e.canonical_name,
                                   COUNT(DISTINCT s.document_id) AS n_docs,
                                   MIN(s.filed_at)               AS first_seen
                            FROM mg_signals s
                            JOIN policy_sources ps ON ps.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type IN ('regulatory_tailwind','demand_surge')
                              AND e.entity_type IN ('SECTOR','CONCEPT','PRODUCT','TECHNOLOGY')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT s.document_id) >= 2
                        ),
                        company_demand AS (
                            SELECT e.canonical_name,
                                   COUNT(DISTINCT d.company) AS n_cos,
                                   COUNT(*)                  AS n_signals
                            FROM mg_signals s
                            JOIN mg_documents d ON d.id = s.document_id
                            JOIN mg_document_entities de ON de.document_id = s.document_id
                            JOIN mg_entities e ON e.id = de.entity_id
                            WHERE s.signal_type = 'demand_surge'
                              AND s.filed_at BETWEEN %s AND %s
                              AND d.company IS NOT NULL AND d.company != ''
                              AND e.entity_type IN ('SECTOR','CONCEPT','PRODUCT','TECHNOLOGY')
                              AND length(e.canonical_name) >= 3
                            GROUP BY e.canonical_name
                            HAVING COUNT(DISTINCT d.company) >= 2
                        )
                        SELECT pt.canonical_name,
                               pt.n_docs   AS policy_docs,
                               pt.first_seen AS policy_first,
                               cd.n_cos    AS company_cos,
                               cd.n_signals AS company_signals
                        FROM policy_tailwind pt
                        JOIN company_demand cd ON lower(pt.canonical_name) = lower(cd.canonical_name)
                        ORDER BY pt.n_docs DESC, cd.n_cos DESC
                        LIMIT 30
                    """, (floor, as_of_date, floor, as_of_date))
                    policy_company_confirmed = cur.fetchall()

        except Exception as e:
            logger.warning(f"discover_chains_from_data query failed: {e}")
            return discovered

        # ── Build CausalChain objects from query results ──────────────────────

        # Lazy-import noise filter so it's applied at chain-build time too.
        # This catches entities that slipped through NLP ingestion (e.g. old
        # DB rows, SEBI boilerplate) before they become permanent chains.
        try:
            from ..themes.theme_detector import _is_noise_entity as _noise_check
        except Exception:
            _noise_check = lambda _: False  # noqa: E731

        # Pattern 1: Demand-Supply Tension chains
        for row in tension_pairs:
            entity = row["canonical_name"]
            if _noise_check(entity):
                continue
            chain_id = "data-tension-" + _re.sub(r"[^a-z0-9]+", "-", entity.lower())[:30]
            if chain_id in existing_ids:
                continue

            demand_cos = row["demand_cos"] or 0
            supply_cos = row["supply_cos"] or 0
            # Probability proportional to company breadth (more companies = more certain)
            p_demand = min(0.95, 0.6 + demand_cos * 0.05)
            p_supply = min(0.92, 0.55 + supply_cos * 0.05)
            # Estimate lag from first_seen dates
            lag = 90
            if row.get("supply_first") and row.get("demand_first"):
                try:
                    lag = max(30, abs((row["supply_first"] - row["demand_first"]).days))
                    lag = min(lag, 365)
                except Exception:
                    lag = 90

            chain = CausalChain(
                chain_id=chain_id,
                name=f"{entity} Demand → Supply Constraint",
                description=(
                    f"Data-detected: {demand_cos} companies report surging demand for '{entity}' "
                    f"while {supply_cos} companies report supply constraints — "
                    f"classic demand-pull bottleneck. Auto-discovered from signal data."
                ),
                links=[
                    CausalLink(
                        cause=entity,
                        cause_type=NodeType.TECHNOLOGY,
                        effect=f"{entity} Supply Shortage",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"Demand for {entity} outpacing supply capacity across {demand_cos} companies",
                        probability=p_demand,
                        lag_days=lag,
                        relation=RelationType.ENABLES,
                    ),
                    CausalLink(
                        cause=f"{entity} Supply Shortage",
                        cause_type=NodeType.CONCEPT,
                        effect=f"{entity} Price / Allocation Pressure",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"Supply shortage forces allocation, pricing power, and margin expansion for {entity} producers",
                        probability=p_supply,
                        lag_days=90,
                        relation=RelationType.ENABLES,
                    ),
                ],
            )
            chain.activation_score = round(min(p_demand * p_supply * 100, 100.0), 1)
            discovered.append(chain)
            existing_ids.add(chain_id)

        # Pattern 2: Demand → Capex Response chains
        for row in demand_capex_pairs:
            entity = row["demand_entity"]
            if _noise_check(entity):
                continue
            chain_id = "data-demand-capex-" + _re.sub(r"[^a-z0-9]+", "-", entity.lower())[:25]
            if chain_id in existing_ids:
                continue

            demand_cos = row["demand_cos"] or 0
            n_capex = row["n_capex"] or 0
            p1 = min(0.95, 0.6 + demand_cos * 0.05)
            p2 = min(0.90, 0.5 + n_capex * 0.04)

            # Estimate lag between demand first seen and capex response
            lag = 180
            if row.get("first_capex") and row.get("first_demand"):
                try:
                    lag = max(60, (row["first_capex"] - row["first_demand"]).days)
                    lag = min(lag, 540)
                except Exception:
                    lag = 180

            chain = CausalChain(
                chain_id=chain_id,
                name=f"{entity} Demand Surge → Capex Response",
                description=(
                    f"Data-detected: {demand_cos} companies report surging demand for '{entity}', "
                    f"triggering {n_capex} capex commitments — capital being deployed to solve shortage. "
                    f"Lag ~{lag} days. Auto-discovered from signal data."
                ),
                links=[
                    CausalLink(
                        cause=entity,
                        cause_type=NodeType.TECHNOLOGY,
                        effect=f"{entity} Capacity Expansion",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"{demand_cos} companies reporting surging {entity} demand drives capital investment",
                        probability=p1,
                        lag_days=90,
                        relation=RelationType.ENABLES,
                    ),
                    CausalLink(
                        cause=f"{entity} Capacity Expansion",
                        cause_type=NodeType.CONCEPT,
                        effect=f"{entity} Supply Chain Buildout",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"Capex committed by {n_capex}+ companies creates multi-year equipment and infrastructure demand",
                        probability=p2,
                        lag_days=lag,
                        relation=RelationType.ENABLES,
                    ),
                ],
            )
            chain.activation_score = round(min(p1 * p2 * 100, 100.0), 1)
            discovered.append(chain)
            existing_ids.add(chain_id)

        # Pattern 3: Technology Adoption → Downstream Demand (3-hop)
        # Group by tech to avoid one entry per downstream per tech
        tech_groups: dict[str, list[dict]] = {}
        for row in tech_downstream:
            tech = row["tech"]
            tech_groups.setdefault(tech, []).append(row)

        for tech, rows in list(tech_groups.items())[:20]:  # top 20 techs
            if _noise_check(tech):
                continue
            # Pick strongest downstream that passes the noise filter.
            # Must iterate — the top-signal downstream is often a noise entity
            # (e.g. "SEBI" with 20k signals swamps all real entities).
            rows_sorted = sorted(rows, key=lambda r: r["n_demand"], reverse=True)
            downstream = None
            for candidate_row in rows_sorted:
                candidate = candidate_row["downstream"]
                if candidate and candidate.lower() != tech.lower() and not _noise_check(candidate):
                    downstream = candidate
                    break
            if not downstream:
                continue

            chain_id = "data-tech-" + _re.sub(r"[^a-z0-9]+", "-", tech.lower())[:25]
            if chain_id in existing_ids:
                continue

            tech_cos = rows_sorted[0]["tech_cos"] or 0
            down_cos = rows_sorted[0]["down_cos"] or 0
            n_adopt = rows_sorted[0]["n_adopt"] or 0

            p1 = min(0.95, 0.65 + tech_cos * 0.04)
            p2 = min(0.90, 0.60 + down_cos * 0.04)

            chain = CausalChain(
                chain_id=chain_id,
                name=f"{tech} Adoption → {downstream} Demand",
                description=(
                    f"Data-detected: {tech_cos} companies adopting '{tech}' ({n_adopt} adoption signals) "
                    f"is driving downstream demand for '{downstream}' across {down_cos} companies. "
                    f"Technology pull-through effect. Auto-discovered from signal data."
                ),
                links=[
                    CausalLink(
                        cause=tech,
                        cause_type=NodeType.TECHNOLOGY,
                        effect=f"{tech} Adoption Surge",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"{tech_cos} companies adopting '{tech}' ({n_adopt} signals) — broad-based technology pull-through",
                        probability=p1,
                        lag_days=60,
                        relation=RelationType.ENABLES,
                    ),
                    CausalLink(
                        cause=f"{tech} Adoption Surge",
                        cause_type=NodeType.CONCEPT,
                        effect=f"{downstream} Demand",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"{tech} adoption drives incremental demand for {downstream} across {down_cos} companies",
                        probability=p2,
                        lag_days=120,
                        relation=RelationType.ENABLES,
                    ),
                    CausalLink(
                        cause=f"{downstream} Demand",
                        cause_type=NodeType.CONCEPT,
                        effect=f"{downstream} Capacity / Infrastructure Constraint",
                        effect_type=NodeType.PRODUCT,
                        mechanism=f"Rising {downstream} demand from {tech} pull-through strains existing capacity",
                        probability=round(p2 * 0.85, 2),
                        lag_days=180,
                        relation=RelationType.ENABLES,
                    ),
                ],
            )
            chain.activation_score = round(min(p1 * p2 * 100, 100.0), 1)
            discovered.append(chain)
            existing_ids.add(chain_id)

        # ── Pattern 4: Policy/macro source demand+supply tension ─────────────
        for row in policy_tension:
            entity = row["canonical_name"]
            if _noise_check(entity):
                continue
            chain_id = "policy-tension-" + _re.sub(r"[^a-z0-9]+", "-", entity.lower())[:30]
            if chain_id in existing_ids:
                continue

            demand_docs = row["demand_docs"] or 0
            supply_docs = row["supply_docs"] or 0
            p_demand = min(0.92, 0.65 + demand_docs * 0.06)
            p_supply = min(0.90, 0.60 + supply_docs * 0.08)

            lag = 90
            if row.get("supply_first") and row.get("demand_first"):
                try:
                    lag = max(30, abs((row["supply_first"] - row["demand_first"]).days))
                    lag = min(lag, 365)
                except Exception:
                    lag = 90

            chain = CausalChain(
                chain_id=chain_id,
                name=f"{entity} — Policy Demand → Supply Constraint",
                description=(
                    f"Policy-detected: {demand_docs} macro/policy documents (RBI/PIB/Invest India/SEBI) "
                    f"report surging demand for '{entity}' while {supply_docs} flag supply constraints. "
                    f"Auto-discovered from India macro/policy signal data."
                ),
                links=[
                    CausalLink(
                        cause=entity,
                        cause_type=NodeType.SECTOR,
                        effect=f"{entity} Demand Surge",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"Policy sources signal demand acceleration for {entity} across {demand_docs} documents",
                        probability=p_demand,
                        lag_days=lag,
                        relation=RelationType.ENABLES,
                    ),
                    CausalLink(
                        cause=f"{entity} Demand Surge",
                        cause_type=NodeType.CONCEPT,
                        effect=f"{entity} Supply Constraint",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"Supply bottleneck flagged in {supply_docs} policy documents alongside demand surge",
                        probability=p_supply,
                        lag_days=lag,
                        relation=RelationType.ENABLES,
                    ),
                ],
                first_detected=row.get("demand_first"),
            )
            chain.activation_score = round(min(p_demand * p_supply * 100, 100.0), 1)
            chain.metadata = {"source": "policy_discovery", "country": "IN"}
            discovered.append(chain)
            existing_ids.add(chain_id)

        # ── Pattern 5: Policy-confirmed + company-corroborated demand ────────
        for row in policy_company_confirmed:
            entity = row["canonical_name"]
            if _noise_check(entity):
                continue
            chain_id = "policy-co-" + _re.sub(r"[^a-z0-9]+", "-", entity.lower())[:30]
            if chain_id in existing_ids:
                continue

            policy_docs = row["policy_docs"] or 0
            company_cos = row["company_cos"] or 0
            p1 = min(0.95, 0.70 + policy_docs * 0.05)
            p2 = min(0.92, 0.65 + company_cos * 0.04)

            chain = CausalChain(
                chain_id=chain_id,
                name=f"{entity} — Policy Mandate → Company Demand Confirmation",
                description=(
                    f"Cross-source confirmation: {policy_docs} macro/policy docs signal regulatory "
                    f"tailwind or demand for '{entity}', corroborated by {company_cos} companies "
                    f"reporting demand surge in filings. High-conviction policy-to-market chain."
                ),
                links=[
                    CausalLink(
                        cause=f"{entity} Policy Signal",
                        cause_type=NodeType.SECTOR,
                        effect=f"{entity} Demand",
                        effect_type=NodeType.CONCEPT,
                        mechanism=f"Regulatory tailwind / demand signal across {policy_docs} policy documents",
                        probability=p1,
                        lag_days=180,
                        relation=RelationType.ENABLES,
                    ),
                    CausalLink(
                        cause=f"{entity} Demand",
                        cause_type=NodeType.CONCEPT,
                        effect=f"{entity} — Market Opportunity",
                        effect_type=NodeType.PRODUCT,
                        mechanism=f"{company_cos} companies corroborate demand surge in earnings filings",
                        probability=p2,
                        lag_days=90,
                        relation=RelationType.ENABLES,
                    ),
                ],
                first_detected=row.get("policy_first"),
            )
            chain.activation_score = round(min(p1 * p2 * 100, 100.0), 1)
            chain.metadata = {"source": "policy_company_confirmed", "country": "IN"}
            discovered.append(chain)
            existing_ids.add(chain_id)

        # Add discovered chains to self._chains so they participate in scoring + persist
        if discovered:
            self._chains.extend(discovered)
            logger.info(f"Auto-discovered {len(discovered)} new causal chains from signal data")
        else:
            logger.info("No new causal chains discovered from data (insufficient signals)")

        return discovered

    def persist(self, pg_store, as_of_date=None, country: str = "US") -> int:
        """Write all scored chains to mg_causal_chains in PostgreSQL.

        Args:
            pg_store:    PGStore instance.
            as_of_date:  The actual data date (MAX filed_at from documents).
                         Stored as last_scored_at so the UI shows when the
                         data was current, not when the pipeline ran.
                         Defaults to date.today() only when not supplied.
        """
        # scored_at = None means no signal data exists yet; we store NULL
        # rather than today's pipeline-run date which would be misleading.
        scored_at = as_of_date  # may be None — handled per field below
        saved = 0
        for chain in self._chains:
            if chain.activation_score <= 0:
                continue  # don't persist zero-score chains — no signal evidence
            try:
                # first_detected: use scored_at when available (data evidence date).
                # When scored_at is None (no data yet), leave first_detected as NULL
                # so the UI shows nothing rather than today's date.
                first_det = chain.first_detected if chain.first_detected else scored_at

                links_json = json.dumps([
                    {
                        "cause": lnk.cause,
                        "cause_type": lnk.cause_type.value,
                        "effect": lnk.effect,
                        "effect_type": lnk.effect_type.value,
                        "mechanism": lnk.mechanism,
                        "probability": lnk.probability,
                        "lag_days": lnk.lag_days,
                        "relation": lnk.relation.value,
                    }
                    for lnk in chain.links
                ])
                pg_store.upsert_causal_chain({
                    "chain_id": chain.chain_id,
                    "chain_name": chain.name,
                    "description": chain.description,
                    "depth": chain.depth,
                    "terminal_effect": chain.terminal_effect,
                    "activation_score": chain.activation_score,
                    "links": links_json,
                    "first_detected": first_det,
                    "last_scored_at": scored_at,
                    "country": country,
                })
                saved += 1
            except Exception as e:
                logger.warning(f"Failed to persist chain {chain.chain_id}: {e}")
        return saved

    def log_results(self, chains: list[CausalChain]):
        logger.info("=== CAUSAL CHAIN ACTIVATION ===")
        for c in chains:
            if c.activation_score > 0:
                logger.info(
                    f"  [{c.activation_score:5.1f}] {c.name}  "
                    f"({c.depth} hops → {c.terminal_effect})"
                )
