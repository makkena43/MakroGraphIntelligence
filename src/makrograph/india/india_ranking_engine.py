"""India Ranking Engine — Theme → Bottleneck → Required Product → Supplier → Rank.

Implements the India-specific ranking formula from the spec.
US pipeline is NOT touched — this runs only when country='IN'.

India SupplierQ Formula:
  35% Product Relevance
  25% Capacity Expansion
  20% Market Share
  10% Order Book
  10% Constraint Exposure

India Ranking Formula:
  30% ThemeCQ + 25% Product Relevance + 20% SupplierQ + 15% Chain Distance + 10% Capacity Expansion

Hard Rule: If Product Relevance = 0 → severe ranking penalty (score × 0.10).

Supply Chain Distance:
  0 = Exact provider of the constrained product
  1 = Direct supplier (one hop)
  2 = Input supplier (two hops)
  3 = Ecosystem participant (three+ hops)
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IndiaRankedStock:
    rank: int
    company: str
    ticker: str
    theme_name: str
    constrained_product: str
    role: str                        # critical_supplier / direct_supplier / input_supplier / ecosystem_participant
    chain_distance: int              # 0–3
    product_relevance: float         # 0.0–1.0
    capacity_expansion_score: float  # 0.0–1.0
    market_share_score: float        # 0.0–1.0
    order_book_score: float          # 0.0–1.0
    constraint_exposure_score: float # 0.0–1.0
    supplier_q: float                # composite SupplierQ
    theme_cq: float                  # inherited from theme
    final_score: float               # India ranking score
    signal_count: int
    rationale: str


@dataclass
class IndiaRankingResult:
    as_of_date: date
    stocks: list[IndiaRankedStock]
    themes_processed: int
    companies_ranked: int


# ---------------------------------------------------------------------------
# Role → chain distance mapping
# ---------------------------------------------------------------------------
_ROLE_DISTANCE: dict[str, int] = {
    "critical_supplier":     0,
    "direct_supplier":       1,
    "input_supplier":        2,
    "ecosystem_participant": 3,
    # Legacy labels from old beneficiary table
    "direct":                1,
    "indirect":              2,
    "localization_play":     1,
}

_DISTANCE_DECAY: dict[int, float] = {0: 1.0, 1: 0.8, 2: 0.6, 3: 0.3}

# Signal types that indicate capacity expansion (bottleneck resolution evidence)
_CAPEX_SIGNALS = frozenset({
    "capex_increase", "capacity_expansion", "infrastructure_spend",
})
_CONSTRAINT_SIGNALS = frozenset({
    "supply_bottleneck", "inventory_drawdown", "supply_constraint",
})
_ORDER_BOOK_SIGNALS = frozenset({
    "demand_surge", "supply_bottleneck",
})


class IndiaRankingEngine:
    """Ranks India companies by how well they solve a supply bottleneck.

    Flow:
        1. Load active India themes + their ThemeCQ scores
        2. For each theme, load beneficiaries from mg_india_beneficiaries
        3. For each company, compute SupplierQ from its signal evidence
        4. Apply India Ranking Formula
        5. Hard Rule: ProductRelevance=0 → collapse score
    """

    def __init__(self, pg_store):
        self._pg = pg_store

    def run(
        self,
        as_of_date: Optional[date] = None,
        lookback_days: int = 365,
        top_n: int = 50,
        min_product_relevance: float = 0.0,
    ) -> IndiaRankingResult:
        """Run the India ranking engine and return ranked stocks."""
        _as_of = as_of_date or date.today()
        _floor  = _as_of - timedelta(days=lookback_days)

        themes = self._load_themes(_as_of)
        if not themes:
            logger.warning("[IndiaRanking] No active India themes found")
            return IndiaRankingResult(_as_of, [], 0, 0)

        beneficiaries = self._load_beneficiaries(_as_of)
        if not beneficiaries:
            logger.warning("[IndiaRanking] No India beneficiaries in DB — run L7 first")
            return IndiaRankingResult(_as_of, [], len(themes), 0)

        signal_stats = self._load_company_signal_stats(_floor, _as_of)

        ranked_stocks: list[IndiaRankedStock] = []
        seen: set[str] = set()

        for brow in beneficiaries:
            company = brow.get("company") or ""
            ticker  = brow.get("ticker") or ""
            theme   = brow.get("theme_name") or ""
            product = brow.get("constrained_product") or ""
            role    = brow.get("beneficiary_type") or "ecosystem_participant"

            if not company or not product:
                continue

            # Dedup — keep highest-scoring entry per company
            co_key = company.lower()

            theme_cq = themes.get(theme, {}).get("theme_cq", 0.30)
            co_stats = signal_stats.get(co_key, {})

            # ── SupplierQ components ─────────────────────────────────────────
            product_relevance     = self._product_relevance(brow, co_stats)
            capacity_expansion    = self._capacity_expansion_score(co_stats)
            market_share          = self._market_share_score(co_stats, len(beneficiaries))
            order_book            = self._order_book_score(co_stats)
            constraint_exposure   = self._constraint_exposure_score(co_stats)

            supplier_q = round(
                product_relevance  * 0.35 +
                capacity_expansion * 0.25 +
                market_share       * 0.20 +
                order_book         * 0.10 +
                constraint_exposure* 0.10,
                4,
            )

            # ── Chain Distance ───────────────────────────────────────────────
            distance    = _ROLE_DISTANCE.get(role, 3)
            dist_decay  = _DISTANCE_DECAY.get(distance, 0.3)
            chain_dist_score = dist_decay  # 0.3–1.0

            # ── India Ranking Formula ────────────────────────────────────────
            raw_score = round(
                theme_cq           * 0.30 +
                product_relevance  * 0.25 +
                supplier_q         * 0.20 +
                chain_dist_score   * 0.15 +
                capacity_expansion * 0.10,
                4,
            )

            # Hard Rule: ProductRelevance = 0 → severe penalty
            if product_relevance == 0.0:
                raw_score *= 0.10

            # Skip if below minimum relevance threshold
            if product_relevance < min_product_relevance:
                continue

            rationale = self._build_rationale(
                company, theme, product, role, distance,
                product_relevance, capacity_expansion, co_stats
            )

            # Keep the best score per company (a company may appear in multiple themes)
            existing = next((s for s in ranked_stocks if s.company.lower() == co_key), None)
            entry = IndiaRankedStock(
                rank=0,
                company=company,
                ticker=ticker,
                theme_name=theme,
                constrained_product=product,
                role=role,
                chain_distance=distance,
                product_relevance=round(product_relevance, 3),
                capacity_expansion_score=round(capacity_expansion, 3),
                market_share_score=round(market_share, 3),
                order_book_score=round(order_book, 3),
                constraint_exposure_score=round(constraint_exposure, 3),
                supplier_q=supplier_q,
                theme_cq=round(theme_cq, 3),
                final_score=round(raw_score, 4),
                signal_count=co_stats.get("total_signals", 0),
                rationale=rationale,
            )
            if existing is None:
                ranked_stocks.append(entry)
            elif raw_score > existing.final_score:
                ranked_stocks.remove(existing)
                ranked_stocks.append(entry)

        # Sort and assign ranks
        ranked_stocks.sort(key=lambda s: -s.final_score)
        ranked_stocks = ranked_stocks[:top_n]
        for i, s in enumerate(ranked_stocks, 1):
            s.rank = i

        logger.info(
            f"[IndiaRanking] {len(ranked_stocks)} companies ranked "
            f"across {len(themes)} themes as_of={_as_of}"
        )
        return IndiaRankingResult(
            as_of_date=_as_of,
            stocks=ranked_stocks,
            themes_processed=len(themes),
            companies_ranked=len(ranked_stocks),
        )

    # ── Score components ──────────────────────────────────────────────────────

    def _product_relevance(self, brow: dict, co_stats: dict) -> float:
        """35% weight. A company with supply-side signals for the constrained
        product gets high relevance. Zero if no product-specific signals."""
        conviction = float(brow.get("conviction_score") or 0)
        sig_count  = co_stats.get("total_signals", 0)
        if conviction == 0 and sig_count == 0:
            return 0.0  # triggers hard rule penalty
        # Blend stored conviction with live signal evidence
        sig_score = min(1.0, math.log1p(sig_count) / math.log1p(50))
        return round(conviction * 0.60 + sig_score * 0.40, 3)

    def _capacity_expansion_score(self, co_stats: dict) -> float:
        """25% weight. capex_increase signals = company is solving the bottleneck."""
        capex = co_stats.get("capex_signals", 0)
        return round(min(1.0, math.log1p(capex) / math.log1p(10)), 3)

    def _market_share_score(self, co_stats: dict, total_cos: int) -> float:
        """20% weight. Relative signal dominance vs other companies in the theme."""
        total = co_stats.get("total_signals", 0)
        if total == 0 or total_cos == 0:
            return 0.0
        # Proxy: more signals in this company vs average signals across all companies
        return round(min(1.0, math.log1p(total) / math.log1p(max(total_cos, 1) * 3)), 3)

    def _order_book_score(self, co_stats: dict) -> float:
        """10% weight. Demand surge or supply bottleneck signals indicate order book pressure."""
        ob = co_stats.get("order_book_signals", 0)
        return round(min(1.0, math.log1p(ob) / math.log1p(8)), 3)

    def _constraint_exposure_score(self, co_stats: dict) -> float:
        """10% weight. supply_bottleneck / inventory_drawdown signals confirm exposure."""
        constraint = co_stats.get("constraint_signals", 0)
        return round(min(1.0, math.log1p(constraint) / math.log1p(5)), 3)

    # ── DB queries ────────────────────────────────────────────────────────────

    def _load_themes(self, as_of: date) -> dict[str, dict]:
        """Load active India themes with their ThemeCQ scores."""
        themes: dict[str, dict] = {}
        try:
            from psycopg2.extras import RealDictCursor
            with self._pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT DISTINCT ON (theme_name)
                               theme_name, theme_slug, strength_score,
                               conviction, company_count
                        FROM mg_themes
                        WHERE country = 'IN'
                          AND (last_updated IS NULL OR last_updated::date <= %s)
                        ORDER BY theme_name, last_updated DESC NULLS LAST
                    """, (as_of,))
                    for row in cur.fetchall():
                        tn = row["theme_name"]
                        # Proxy ThemeCQ from strength + conviction + company_count
                        # (mirrors how the US ranking engine computes it)
                        strength  = float(row.get("strength_score") or 0) / 100.0
                        conv_str  = str(row.get("conviction") or "emerging").lower()
                        conv_map  = {"confirmed": 0.9, "developing": 0.6, "emerging": 0.3}
                        conv_val  = conv_map.get(conv_str, 0.3)
                        co_count  = int(row.get("company_count") or 1)
                        breadth   = max(0.25, min(1.0, 60.0 / co_count))
                        theme_cq  = round(strength * 0.50 + conv_val * 0.30 + breadth * 0.20, 3)
                        themes[tn] = {"theme_cq": theme_cq, "slug": row.get("theme_slug")}
        except Exception as e:
            logger.warning(f"[IndiaRanking] _load_themes failed: {e}")
        return themes

    def _load_beneficiaries(self, as_of: date) -> list[dict]:
        """Load India beneficiaries from mg_india_beneficiaries."""
        try:
            from psycopg2.extras import RealDictCursor
            with self._pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT company, ticker, theme_name, constrained_product,
                               beneficiary_type, conviction_score, signal_count,
                               has_order_book_signals, import_substitution_play
                        FROM mg_india_beneficiaries
                        WHERE (as_of_date IS NULL OR as_of_date <= %s)
                        ORDER BY conviction_score DESC
                    """, (as_of,))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"[IndiaRanking] _load_beneficiaries failed: {e}")
            return []

    def _load_company_signal_stats(self, floor: date, as_of: date) -> dict[str, dict]:
        """Load per-company signal stats for the date window."""
        stats: dict[str, dict] = {}
        try:
            from psycopg2.extras import RealDictCursor
            with self._pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT
                            lower(COALESCE(NULLIF(d.company,''), d.ticker)) AS co,
                            d.ticker,
                            COUNT(*)                                          AS total_signals,
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'capex_increase','capacity_expansion'))        AS capex_signals,
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'supply_bottleneck','inventory_drawdown',
                                'supply_constraint'))                          AS constraint_signals,
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'demand_surge','supply_bottleneck'))           AS order_book_signals
                        FROM mg_signals s
                        JOIN mg_documents d ON d.id = s.document_id
                        WHERE d.country = 'IN'
                          AND d.filed_at BETWEEN %s AND %s
                          AND COALESCE(NULLIF(d.company,''), d.ticker) IS NOT NULL
                        GROUP BY lower(COALESCE(NULLIF(d.company,''), d.ticker)), d.ticker
                    """, (floor, as_of))
                    for row in cur.fetchall():
                        stats[row["co"]] = {
                            "total_signals":    int(row["total_signals"] or 0),
                            "capex_signals":    int(row["capex_signals"] or 0),
                            "constraint_signals": int(row["constraint_signals"] or 0),
                            "order_book_signals": int(row["order_book_signals"] or 0),
                            "ticker": row.get("ticker") or "",
                        }
        except Exception as e:
            logger.warning(f"[IndiaRanking] _load_company_signal_stats failed: {e}")
        return stats

    def _build_rationale(
        self, company, theme, product, role, distance,
        pr, capex, co_stats
    ) -> str:
        role_label = role.replace("_", " ").title()
        dist_label = {0: "exact provider", 1: "direct supplier",
                      2: "input supplier", 3: "ecosystem participant"}.get(distance, "participant")
        parts = [
            f"{company} ranked as {role_label} ({dist_label}) for '{product}' "
            f"in '{theme}'.",
            f"ProductRelevance={pr:.2f}.",
        ]
        if co_stats.get("capex_signals", 0):
            parts.append(f"Capacity expansion: {co_stats['capex_signals']} capex signals.")
        if co_stats.get("constraint_signals", 0):
            parts.append(f"Constraint exposure: {co_stats['constraint_signals']} bottleneck signals.")
        return " ".join(parts)
