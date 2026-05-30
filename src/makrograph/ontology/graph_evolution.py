"""Temporal ontology evolution tracking.

Tracks how entities, relationships, and themes change over time.
Detects acceleration (emerging), stability (confirmed), and decay (declining).
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EvolutionMetrics:
    """Temporal metrics for a node or theme."""
    name: str
    mention_counts: dict = field(default_factory=dict)    # date_str -> count
    signal_counts: dict = field(default_factory=dict)     # date_str -> count
    sector_spread: list[str] = field(default_factory=list)

    def add_mention(self, mention_date: date, count: int = 1):
        key = str(mention_date)
        self.mention_counts[key] = self.mention_counts.get(key, 0) + count

    def momentum(self, window_days: int = 30, compare_days: int = 90,
                  as_of_date: date = None) -> float:
        """Compute momentum: recent window vs prior window.

        Args:
            as_of_date: upper bound for the computation window. Must be the
                        replay_date in historical replay mode so date.today()
                        is never used (future-leakage prevention).
        """
        today = as_of_date if as_of_date is not None else date.today()
        recent_cutoff = today - timedelta(days=window_days)
        prior_cutoff = today - timedelta(days=compare_days)

        recent = sum(
            v for k, v in self.mention_counts.items()
            if str(recent_cutoff) <= k <= str(today)
        )
        prior = sum(
            v for k, v in self.mention_counts.items()
            if str(prior_cutoff) <= k < str(recent_cutoff)
        )

        if prior == 0:
            return min(float(recent) * 10.0, 100.0) if recent > 0 else 0.0

        ratio = recent / prior
        return min((ratio - 1.0) * 50.0 + 50.0, 100.0)

    def total_mentions(self) -> int:
        return sum(self.mention_counts.values())

    def trend_direction(self, as_of_date: date = None) -> str:
        """Classify trend: accelerating | stable | decelerating | dormant."""
        mom = self.momentum(as_of_date=as_of_date)
        if mom >= 65:
            return "accelerating"
        elif mom >= 45:
            return "stable"
        elif mom >= 20:
            return "decelerating"
        return "dormant"


class GraphEvolutionTracker:
    """Tracks and analyzes temporal evolution of the ontology graph.

    Answers questions like:
        - Which technologies are accelerating in mentions?
        - Which supply-chain relationships are newly forming?
        - Which themes are gaining cross-sector breadth?
        - Which companies suddenly increased capex signals?
    """

    def __init__(self, pg_store=None, window_days: int = 30, compare_days: int = 90):
        self.pg_store = pg_store
        self.window_days = window_days
        self.compare_days = compare_days
        self._metrics: dict[str, EvolutionMetrics] = {}

    def record_entity_mention(self, entity_name: str, entity_type: str,
                               mention_date: date, count: int = 1):
        """Record a single entity mention."""
        key = f"{entity_type}:{entity_name}"
        if key not in self._metrics:
            self._metrics[key] = EvolutionMetrics(name=entity_name)
        self._metrics[key].add_mention(mention_date, count)

    def record_sector_spread(self, entity_name: str, entity_type: str, sector: str):
        """Track which sectors are adopting a technology/concept."""
        key = f"{entity_type}:{entity_name}"
        if key not in self._metrics:
            self._metrics[key] = EvolutionMetrics(name=entity_name)
        if sector not in self._metrics[key].sector_spread:
            self._metrics[key].sector_spread.append(sector)

    def get_accelerating_entities(
        self, entity_type: str = "TECHNOLOGY", top_n: int = 20,
        as_of_date: date = None,
    ) -> list[dict]:
        """Return top N accelerating entities by momentum score."""
        results = []
        prefix = f"{entity_type}:"
        for key, metrics in self._metrics.items():
            if not key.startswith(prefix):
                continue
            mom = metrics.momentum(self.window_days, self.compare_days, as_of_date=as_of_date)
            results.append({
                "name": metrics.name,
                "entity_type": entity_type,
                "momentum_score": round(mom, 2),
                "trend": metrics.trend_direction(),
                "total_mentions": metrics.total_mentions(),
                "sector_count": len(metrics.sector_spread),
                "sectors": metrics.sector_spread,
            })
        results.sort(key=lambda x: -x["momentum_score"])
        return results[:top_n]

    def get_cross_sector_signals(self, min_sectors: int = 3,
                                   as_of_date: date = None) -> list[dict]:
        """Find technologies/concepts appearing across >= N sectors (theme signal)."""
        results = []
        for key, metrics in self._metrics.items():
            if len(metrics.sector_spread) >= min_sectors:
                entity_type, name = key.split(":", 1)
                mom = metrics.momentum(self.window_days, self.compare_days, as_of_date=as_of_date)
                results.append({
                    "name": name,
                    "entity_type": entity_type,
                    "sector_count": len(metrics.sector_spread),
                    "sectors": metrics.sector_spread,
                    "momentum_score": round(mom, 2),
                    "total_mentions": metrics.total_mentions(),
                })
        results.sort(key=lambda x: (-x["sector_count"], -x["momentum_score"]))
        return results

    def load_from_pg(self, days: int = 180, as_of_date=None):
        """Load entity mention history from PostgreSQL for trend analysis.

        Args:
            as_of_date: upper-bound date for the query window. Defaults to today.
                        Pass the replay_date in historical replay mode so NOW() is
                        never used — documents are looked up by their real filed_at.
        """
        if not self.pg_store:
            return

        _as_of = as_of_date or date.today()
        if hasattr(_as_of, "date"):
            _as_of = _as_of.date()
        _floor = _as_of - timedelta(days=days)

        try:
            sql = """
                SELECT e.canonical_name, e.entity_type,
                       d.filed_at, COUNT(*) as mention_count
                FROM mg_document_entities de
                JOIN mg_entities e ON e.id = de.entity_id
                JOIN mg_documents d ON d.id = de.document_id
                WHERE d.filed_at >= %s
                  AND d.filed_at <= %s
                GROUP BY e.canonical_name, e.entity_type, d.filed_at
            """
            with self.pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (_floor, _as_of))
                    for row in cur.fetchall():
                        name, etype, filed, count = row
                        if filed:
                            self.record_entity_mention(name, etype, filed, count)

            # Load sector spread
            sector_sql = """
                SELECT e.canonical_name, e.entity_type, e2.canonical_name as sector
                FROM mg_document_entities de
                JOIN mg_entities e ON e.id = de.entity_id
                JOIN mg_documents d ON d.id = de.document_id
                JOIN mg_document_entities de2 ON de2.document_id = d.id
                JOIN mg_entities e2 ON e2.id = de2.entity_id AND e2.entity_type = 'SECTOR'
                WHERE e.entity_type IN ('TECHNOLOGY', 'CONCEPT')
                  AND d.filed_at >= %s
                  AND d.filed_at <= %s
                GROUP BY e.canonical_name, e.entity_type, e2.canonical_name
            """
            with self.pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sector_sql, (_floor, _as_of))
                    for row in cur.fetchall():
                        name, etype, sector = row
                        self.record_sector_spread(name, etype, sector)

            logger.info(f"Loaded evolution metrics for {len(self._metrics)} entities from PostgreSQL")

        except Exception as e:
            logger.error(f"Failed to load evolution metrics from PG: {e}")

    def compute_theme_momentum(self, theme_slug: str, as_of_date: date = None) -> dict:
        """Compute momentum metrics for an entire theme from its entity metrics.

        Args:
            as_of_date: replay-safe date ceiling passed to EvolutionMetrics.momentum().
        """
        if not self.pg_store:
            return {}

        try:
            sql = """
                SELECT e.canonical_name, e.entity_type
                FROM mg_theme_beneficiaries tb
                JOIN mg_themes t ON t.id = tb.theme_id
                JOIN mg_entities e ON e.id = tb.entity_id
                WHERE t.theme_slug = %s
            """
            with self.pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (theme_slug,))
                    entities = cur.fetchall()

            if not entities:
                return {}

            momentums = []
            for name, etype in entities:
                key = f"{etype}:{name}"
                if key in self._metrics:
                    momentums.append(self._metrics[key].momentum(
                        self.window_days, self.compare_days, as_of_date=as_of_date
                    ))

            if not momentums:
                return {}

            return {
                "theme_slug": theme_slug,
                "avg_momentum": round(sum(momentums) / len(momentums), 2),
                "max_momentum": round(max(momentums), 2),
                "entity_count": len(momentums),
            }
        except Exception as e:
            logger.error(f"Theme momentum computation failed: {e}")
            return {}

    def get_summary(self) -> dict:
        """Return high-level evolution summary."""
        trends = defaultdict(int)
        for metrics in self._metrics.values():
            trends[metrics.trend_direction()] += 1

        accelerating = self.get_accelerating_entities("TECHNOLOGY", top_n=10)
        cross_sector = self.get_cross_sector_signals(min_sectors=3)

        return {
            "total_tracked_entities": len(self._metrics),
            "trend_distribution": dict(trends),
            "top_accelerating_technologies": accelerating[:5],
            "cross_sector_signals": len(cross_sector),
        }
