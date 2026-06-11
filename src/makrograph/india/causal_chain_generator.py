"""India Causal Chain Generator (India Pipeline — Layer 10).

Generates India-specific causal chains in the format:
    Policy Trigger → Demand Surge → Supply Bottleneck → Required Product

Chains are discovered dynamically from:
  - Macro/policy documents (RBI, PIB, Invest India, SEBI) via Pattern 4 in CausalMapper
  - Company filings (NSE/BSE) via Patterns 1-3 in CausalMapper

Actual beneficiary companies are discovered dynamically from signal data
by the India Ranking Engine (india_ranking_engine.py) using
Theme → Bottleneck → Required Product → Supplier → Rank.

Output: CausalChain objects (same schema as causal_mapper.py) + persistence
to mg_causal_chains with country='IN'.
"""

import logging
import re
from datetime import date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# India causal chain library — populated from signal data, not hardcoded
# ---------------------------------------------------------------------------
# Chains are auto-discovered by CausalMapper.discover_chains_from_data():
#   Pattern 1-3: company filings (NSE/BSE) — demand/supply/capex/tech patterns
#   Pattern 4:   macro/policy sources (RBI, PIB, Invest India, SEBI)
#
# This list is intentionally empty. Do NOT add hardcoded entries here.

INDIA_CAUSAL_CHAINS: list[dict] = [
    # intentionally empty — all chains come from data discovery
    # by IndiaRankingEngine using Theme→Bottleneck→Product→Supplier→Rank.
]


def build_india_causal_chains() -> list:  # noqa: kept for compat
    return []


class IndiaCausalChainGenerator:
    """Layer 10: Generate and persist India-specific causal chains."""

    def __init__(self, config: dict = None):
        self._cfg = config or {}

    def generate(self) -> list:
        return []

    def score_and_persist(self, pg_store, as_of_date=None) -> int:
        """Discover India chains from signal data and persist them.

        All chains are auto-discovered — nothing is hardcoded:
          Pattern 1-3: NSE/BSE company filings (demand/supply/capex/tech signals)
          Pattern 4:   Macro/policy sources — RBI, PIB, Invest India, SEBI
                       (mined by document count, not company count)
        """
        from ..ontology.causal_mapper import CausalMapper

        _as_of = as_of_date or date.today()
        mapper = CausalMapper(self._cfg)
        mapper._chains = []

        try:
            discovered = mapper.discover_chains_from_data(
                pg_store, as_of_date=_as_of, lookback_days=730
            )
            logger.info(
                f"[IndiaCausalChainGenerator] Discovered {len(discovered)} India chains "
                f"from signal data"
            )
        except Exception as e:
            logger.warning(f"[IndiaCausalChainGenerator] Chain discovery failed: {e}")

        chains = mapper._chains
        if not chains:
            logger.info("[IndiaCausalChainGenerator] No chains discovered — skipping persist")
            return 0

        saved = mapper.persist(pg_store, as_of_date=_as_of, country="IN")
        logger.info(f"[IndiaCausalChainGenerator] Persisted {saved} India causal chains")
        return saved
