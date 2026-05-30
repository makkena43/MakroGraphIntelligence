"""Dynamic theme ranking engine.

Composite score incorporates:
    - Signal strength, frequency, and capex commitment
    - Cross-sector breadth
    - Momentum: quarterly acceleration (Q-over-Q mention growth)
    - Management confidence (avg signal confidence)
    - Repeated quarterly mentions bonus
    - Conviction multiplier
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from ..ontology.ontology_model import InvestmentTheme, ThemeConviction

logger = logging.getLogger(__name__)


@dataclass
class RankedTheme:
    """A theme with all ranking scores computed."""
    theme: InvestmentTheme
    rank: int
    composite_score: float
    signal_score: float
    breadth_score: float
    momentum_score: float
    capex_score: float
    confidence_score: float
    conviction_multiplier: float
    persistence_multiplier: float = 1.0   # ≥ 1.0; grows with confirmed-quarter count
    confirmed_quarters: int = 0           # number of distinct quarters with strength ≥ 30
    eligibility_score: float = 0.0        # 0–1 six-factor gate score
    rank_change: int = 0
    rank_change_label: str = ""


class ThemeRanker:
    """Ranks investment themes using a multi-factor composite scoring model.

    Composite Score Formula:
        composite = (
            w_signal     * signal_score      +   # frequency + capex commits
            w_breadth    * breadth_score     +   # cross-sector spread
            w_momentum   * momentum_score    +   # Q-over-Q acceleration
            w_capex      * capex_score       +   # capex signal weight
            w_confidence * confidence_score  +   # management conviction
        ) * conviction_multiplier
    """

    # Supply-demand tension is the primary intelligence signal.
    # A theme where demand is surging but supply is constrained = pricing power
    # = earnings acceleration = potential 5x stock move.
    DEFAULT_WEIGHTS = {
        "tension":    0.40,   # supply-demand imbalance — the 5x thesis driver
        "capex":      0.25,   # capital committed by buyers = demand is structural
        "momentum":   0.20,   # Q-over-Q score acceleration
        "breadth":    0.10,   # cross-sector validation
        "confidence": 0.05,   # management conviction in language
    }

    CONVICTION_MULTIPLIERS = {
        ThemeConviction.CONFIRMED: 1.25,
        ThemeConviction.DEVELOPING: 1.00,
        ThemeConviction.EMERGING: 0.75,
        ThemeConviction.DECLINING: 0.35,
    }

    # ── Theme Eligibility Score ─────────────────────────────────────────────────
    # Six-factor gate: a theme must score > MIN_ELIGIBILITY to be ranked.
    # This eliminates statistically-valid but economically-hollow themes.
    #
    # Factors (each 0–1):
    #   demand_acceleration  — how fast demand signals are growing
    #   supply_constraint    — presence of supply tightness signals + constraint keywords
    #   capex_commitment     — companies committing capital (structural, not speculative)
    #   company_proxy        — company count as beneficiary-strength proxy (mapped post-rank)
    #   persistence          — multi-quarter confirmation
    #   bottleneck_flag      — explicit bottleneck / constraint keyword present
    #
    # Weights:
    # Eligibility gate: theme must score > 0.75 to be surface-ranked.
    # Weights sum to 1.0.  Threshold raised from 0.35 → 0.75 so only themes
    # with strong multi-factor evidence (demand + supply + capex + persistence)
    # survive — eliminates single-signal noise themes.
    MIN_ELIGIBILITY_SCORE: float = 0.75
    ELIGIBILITY_WEIGHTS: dict[str, float] = {
        "demand_acceleration": 0.20,
        "supply_constraint":   0.25,
        "capex_commitment":    0.20,
        "company_proxy":       0.15,
        "persistence":         0.10,
        "bottleneck_flag":     0.10,
    }

    # Persistence score tiers (used inside eligibility, not the composite multiplier).
    # Q1 = 0.3  (single snapshot — could be noise)
    # Q2 = 0.7  (two-quarter confirmation — likely structural)
    # Q3+ = 1.0 (sustained trend — high conviction)
    _PERSISTENCE_TIER: dict[int, float] = {1: 0.3, 2: 0.7}
    _PERSISTENCE_TIER_3PLUS: float = 1.0

    def _compute_eligibility_score(self, theme: "InvestmentTheme", n_confirmed_q: int) -> float:
        """Compute 0–1 eligibility score for theme. Must exceed MIN_ELIGIBILITY_SCORE.

        Uses only metadata already available at rank time (no extra DB queries).
        """
        meta = theme.metadata or {}

        demand_ct   = float(meta.get("demand_count", 0) or 0)
        supply_ct   = float(meta.get("supply_constraint_count", 0) or 0)
        capex_ct    = float(meta.get("capex_count", 0) or 0)
        n_cos       = float(theme.company_count or 0)
        ckw         = float(meta.get("constraint_kw_count", 0) or 0)
        wt_score    = float(meta.get("weighted_constraint_score", 0) or 0)
        theme_type  = meta.get("theme_type", "")

        # ── Adaptive denominators: scale with the configured ticker universe ─
        # Denominators scale proportionally with universe size so small universes
        # (e.g. 8 tickers) don't face the same bar as large ones (500 tickers).
        # Formula: denominator = min(universe * factor, cap)
        #   Small universe (8):  demand≈20, supply≈16, capex≈6, company≈6
        #   Large universe (100): demand=50, supply=40, capex=15, company=10
        _universe = max(
            len(self._config.get("edgar", {}).get("ticker_list", [])),
            int(self._config.get("min_companies_for_theme", 5)),
            5,
        )
        _demand_denom  = min(_universe * 2.5,  50.0)   # caps at 50 for huge universes
        _supply_denom  = min(_universe * 2.0,  40.0)   # caps at 40
        _capex_denom   = min(_universe * 0.75, 15.0)   # caps at 15
        _company_denom = min(_universe * 0.75, 10.0)   # caps at 10 — key scaling knob

        # 1. Demand acceleration [0–1]
        demand_acc = min(demand_ct / _demand_denom, 1.0)

        # 2. Supply constraint intensity [0–1]
        # Constraint keywords count double vs plain supply signals
        supply_strength = min((supply_ct + ckw * 1.5 + wt_score * 0.5) / _supply_denom, 1.0)

        # 3. Capex commitment [0–1]
        capex_strength = min(capex_ct / _capex_denom, 1.0)

        # 4. Company proxy for beneficiary strength [0–1]
        company_score = min(n_cos / _company_denom, 1.0)

        # 5. Persistence [0–1]  — discrete tiers Q1=0.3 / Q2=0.7 / Q3+=1.0
        persistence_score = (
            self._PERSISTENCE_TIER_3PLUS
            if n_confirmed_q >= 3
            else self._PERSISTENCE_TIER.get(n_confirmed_q, 0.3)
        )

        # 6. Bottleneck flag [0 or 1]
        is_bottleneck = float(
            bool(meta.get("is_bottleneck"))
            or theme_type == "bottleneck"
            or ckw >= 3
        )

        score = (
            self.ELIGIBILITY_WEIGHTS["demand_acceleration"] * demand_acc
            + self.ELIGIBILITY_WEIGHTS["supply_constraint"]   * supply_strength
            + self.ELIGIBILITY_WEIGHTS["capex_commitment"]    * capex_strength
            + self.ELIGIBILITY_WEIGHTS["company_proxy"]       * company_score
            + self.ELIGIBILITY_WEIGHTS["persistence"]         * persistence_score
            + self.ELIGIBILITY_WEIGHTS["bottleneck_flag"]     * is_bottleneck
        )
        return round(score, 3)

    def __init__(self, config: dict):
        self._config = config  # stored for adaptive denominator scaling
        w = config.get("ranking_weights", {})
        self.weights = {**self.DEFAULT_WEIGHTS, **w}
        self.min_score_to_rank = config.get("min_composite_score", 20.0)
        elig = config.get("min_eligibility_score", None)
        self.min_eligibility = elig if elig is not None else self.MIN_ELIGIBILITY_SCORE
        self._previous_ranks: dict[str, int] = {}

    # Persistence multipliers: each additional confirmed quarter lifts composite score.
    # 1 quarter  → ×1.00 (baseline — single snapshot, no proof of persistence)
    # 2 quarters → ×1.12 (two-quarter confirmation: momentum is real)
    # 3 quarters → ×1.25 (three quarters: structural, high conviction)
    # 4+ quarters → ×1.40 (sustained multi-year trend: maximum persistence lift)
    PERSISTENCE_MULTIPLIERS = {1: 1.00, 2: 1.12, 3: 1.25}
    PERSISTENCE_MULTIPLIER_4PLUS = 1.40

    def rank(
        self,
        themes: list[InvestmentTheme],
        evolution_data: dict = None,
        pg_store=None,
        as_of_date=None,
    ) -> list[RankedTheme]:
        """Compute composite scores and return ranked list.

        Persistence weighting (Point 11):
            Themes that have been confirmed across ≥2 distinct fiscal quarters
            get a multiplier applied to their composite score.  This rewards
            structural themes that persist quarter-over-quarter vs one-time blips.
            Multipliers: 1Q×1.0, 2Q×1.12, 3Q×1.25, 4Q+×1.40
        """
        scored = []

        # ── Batch-fetch confirmed-quarter counts (single SQL, avoids N+1) ────
        confirmed_quarters_map: dict[str, int] = {}
        if pg_store and themes:
            try:
                slugs = [t.theme_slug for t in themes]
                confirmed_quarters_map = pg_store.get_confirmed_quarter_counts(slugs)
            except Exception as _pq_err:
                logger.debug(f"Could not load confirmed quarter counts: {_pq_err}")

        for theme in themes:
            tension_score = self._compute_supply_demand_tension(theme, pg_store)
            breadth_score = self._compute_breadth_score(theme)
            momentum_score = self._compute_momentum_score(theme, pg_store, evolution_data, as_of_date=as_of_date)
            capex_score = self._compute_capex_score(theme, pg_store)
            confidence_score = self._compute_confidence_score(theme, pg_store)
            conviction_mult = self.CONVICTION_MULTIPLIERS.get(theme.conviction, 1.0)

            # ── Persistence multiplier ────────────────────────────────────────
            n_confirmed = confirmed_quarters_map.get(theme.theme_slug, 0)
            if n_confirmed == 0:
                n_confirmed = int((theme.metadata or {}).get("quarter_count", 1))
            persistence_mult = (
                self.PERSISTENCE_MULTIPLIER_4PLUS if n_confirmed >= 4
                else self.PERSISTENCE_MULTIPLIERS.get(n_confirmed, 1.00)
            )

            # ── Theme Eligibility Gate ────────────────────────────────────────
            # Six-factor score must exceed MIN_ELIGIBILITY_SCORE (default 0.35).
            # Eliminates statistically valid but economically hollow themes.
            eligibility = self._compute_eligibility_score(theme, n_confirmed)
            if eligibility < self.min_eligibility:
                logger.debug(
                    f"Eligibility gate: '{theme.theme_slug}' scored {eligibility:.3f} "
                    f"< {self.min_eligibility:.2f} — excluded"
                )
                continue

            base_composite = (
                self.weights["tension"]     * tension_score
                + self.weights["breadth"]    * breadth_score
                + self.weights["momentum"]   * momentum_score
                + self.weights["capex"]      * capex_score
                + self.weights["confidence"] * confidence_score
            )
            composite = base_composite * conviction_mult * persistence_mult

            if composite < self.min_score_to_rank:
                continue

            scored.append(RankedTheme(
                theme=theme,
                rank=0,
                composite_score=round(composite, 2),
                signal_score=round(tension_score, 2),
                breadth_score=round(breadth_score, 2),
                momentum_score=round(momentum_score, 2),
                capex_score=round(capex_score, 2),
                confidence_score=round(confidence_score, 2),
                conviction_multiplier=conviction_mult,
                persistence_multiplier=round(persistence_mult, 2),
                confirmed_quarters=n_confirmed,
                eligibility_score=eligibility,
            ))

        scored.sort(key=lambda r: -r.composite_score)
        for i, rt in enumerate(scored, 1):
            rt.rank = i
            slug = rt.theme.theme_slug
            prev = self._previous_ranks.get(slug)
            if prev is not None:
                rt.rank_change = prev - i
                rt.rank_change_label = (
                    f"↑{rt.rank_change}" if rt.rank_change > 0
                    else f"↓{abs(rt.rank_change)}" if rt.rank_change < 0
                    else "→"
                )
            else:
                rt.rank_change_label = "NEW"

        self._previous_ranks = {rt.theme.theme_slug: rt.rank for rt in scored}
        n_persistent = sum(1 for rt in scored if rt.confirmed_quarters >= 2)
        logger.info(
            f"Ranked {len(scored)} themes "
            f"(persistence-boosted ≥2Q: {n_persistent}/{len(scored)}). "
            f"Top: {scored[0].theme.theme_name if scored else 'none'}"
        )
        return scored

    # ------------------------------------------------------------------ #
    # Score components
    # ------------------------------------------------------------------ #

    def _compute_supply_demand_tension(self, theme: InvestmentTheme, pg_store=None) -> float:
        """Supply-demand tension score (0-100) — the PRIMARY ranking factor.

        High score = demand surging + supply constrained = pricing power + earnings acceleration.
        Low score = demand fine + supply fine = no structural edge.

        Formula:
            tension = 2 × D × S / (D + S)   (harmonic mean of demand and supply_constraint counts)
        This is non-zero ONLY when BOTH are present — one alone is not enough.
        """
        # 1. Use pre-computed tension from auto-detection if available
        if theme.metadata and theme.metadata.get("tension_score") is not None:
            base = float(theme.metadata["tension_score"])
            # Capex conviction boosts tension further
            capex = theme.metadata.get("capex_count", 0)
            capex_lift = min(capex * 5.0, 20.0)
            return min(base + capex_lift, 100.0)

        # 2. Seed-based themes: query the DB for supply vs demand signals
        if pg_store:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT
                                 SUM(CASE WHEN s.signal_type IN
                                     ('demand_surge','capex_increase','hiring_surge',
                                      'technology_adoption','market_entry')
                                     THEN 1 ELSE 0 END)                AS demand_count,
                                 SUM(CASE WHEN s.signal_type IN
                                     ('supply_bottleneck','inventory_drawdown')
                                     THEN 1 ELSE 0 END)                AS supply_count,
                                 SUM(CASE WHEN s.signal_type = 'capex_increase'
                                     THEN 1 ELSE 0 END)                AS capex_count
                               FROM mg_signals s
                               JOIN mg_documents d ON d.id = s.document_id
                               WHERE d.filed_at >= NOW() - INTERVAL '180 days'
                                 AND s.signal_type = ANY(%s)""",
                            (list(theme.signal_types or
                                  ["demand_surge", "supply_bottleneck", "capex_increase"]),),
                        )
                        row = cur.fetchone()
                        if row and row[0] is not None:
                            d_count = float(row[0] or 0)
                            s_count = float(row[1] or 0)
                            capex_ct = float(row[2] or 0)
                            if d_count > 0 and s_count > 0:
                                tension = 2.0 * d_count * s_count / (d_count + s_count)
                                return min(tension * 8.0 + capex_ct * 4.0, 100.0)
                            elif capex_ct >= 3:
                                return min(capex_ct * 10.0, 50.0)
            except Exception:
                pass

        # 3. Fallback: use signal_types list as proxy
        if theme.signal_types:
            has_demand = any(s in theme.signal_types for s in
                             ("demand_surge", "capex_increase", "technology_adoption"))
            has_constraint = any(s in theme.signal_types for s in
                                 ("supply_bottleneck", "inventory_drawdown"))
            has_capex = "capex_increase" in theme.signal_types
            if has_demand and has_constraint:
                return 55.0
            if has_capex:
                return 40.0
            if has_demand:
                return 25.0
        return 10.0

    def _compute_breadth_score(self, theme: InvestmentTheme) -> float:
        """Cross-sector spread (0-100).

        Breadth confirms the theme is systemic, not company-specific.
        Weight is deliberately LOW (0.10) — breadth alone is not alpha.
        """
        sector_count = len(theme.sectors) if theme.sectors else 0
        company_count = theme.company_count or 0
        # Each sector adds 12 pts (max 5 sectors = 60), each company adds 2 pts (max 20)
        sector_score = min(sector_count * 12.0, 60.0)
        company_score = min(company_count * 2.0, 40.0)
        return min(sector_score + company_score, 100.0)

    def _compute_momentum_score(
        self,
        theme: InvestmentTheme,
        pg_store=None,
        evolution_data: dict = None,
        as_of_date=None,
    ) -> float:
        """Slope-based momentum from snapshot history (0-100).

        Uses the slope of the last N strength snapshots (rolling first derivative)
        rather than simplistic first-vs-last growth percentage.

        Global multiplier cap: when evolution data provides an avg_momentum that
        would apply the same shift to many unrelated themes, it is capped to ±20
        from neutral (50). This prevents a single macro quarter multiplier from
        synchronising AI, Wind, Battery, and Cloud to identical trajectories.
        """
        if evolution_data and theme.theme_slug in evolution_data:
            raw = float(evolution_data[theme.theme_slug].get("avg_momentum", 50.0))
            # Cap global macro adjustment to ±20 from neutral so theme-specific
            # evidence always dominates over dataset-wide multiplier effects.
            deviation = raw - 50.0
            capped = 50.0 + max(-20.0, min(20.0, deviation))
            return round(capped, 2)

        if pg_store:
            try:
                snapshots = self._load_recent_snapshots(
                    theme, pg_store, months=9, as_of_date=as_of_date
                )
                n = len(snapshots)
                if n >= 3:
                    scores = [s.get("strength_score", 0) for s in snapshots]
                    # Slope via simple half-window split:
                    # avg(second half) - avg(first half) — more stable than first-vs-last.
                    mid = n // 2
                    first_half_avg = sum(scores[:mid]) / mid
                    second_half_avg = sum(scores[mid:]) / (n - mid)
                    slope = second_half_avg - first_half_avg
                    # Normalise: ±10 pt slope maps to ±30 from neutral (50±30 = 20..80)
                    return round(min(max(50.0 + slope * 3.0, 0.0), 100.0), 2)
                elif n == 2:
                    delta = snapshots[-1].get("strength_score", 0) - snapshots[0].get("strength_score", 0)
                    return round(min(max(50.0 + delta * 3.0, 0.0), 100.0), 2)
            except Exception:
                pass

        # Neutral fallback — no historical snapshot data yet.
        # Do NOT use theme.momentum_score (set to strength*0.9 before this fix).
        return 50.0

    def _compute_capex_score(self, theme: InvestmentTheme, pg_store=None) -> float:
        """Weight of capex-specific signals (0-100).

        Capex commitments are the strongest forward indicator — companies that
        have committed capital are far more likely to follow through than those
        that just mentioned a technology.
        """
        if not pg_store:
            # Fallback: check if capex in signal_types
            if theme.signal_types and any(
                "capex" in s for s in theme.signal_types
            ):
                return 60.0
            return 20.0

        try:
            capex_count = self._count_capex_signals(theme, pg_store)
            return min(capex_count * 10.0, 100.0)
        except Exception:
            return 20.0

    def _compute_confidence_score(self, theme: InvestmentTheme, pg_store=None) -> float:
        """Average management confidence from signals (0-100).

        High-confidence signals come from explicit management statements
        (e.g. "we are committing $2B to AI infrastructure over 3 years")
        vs low-confidence ("AI may present opportunities").
        """
        if not pg_store:
            return 50.0
        try:
            avg_conf = self._avg_signal_confidence(theme, pg_store)
            return avg_conf * 100.0
        except Exception:
            return 50.0

    # ------------------------------------------------------------------ #
    # pg_store helpers — graceful if tables don't exist
    # ------------------------------------------------------------------ #

    def _count_active_quarters(self, theme: InvestmentTheme, pg_store) -> int:
        """Count distinct snapshot quarters for the theme."""
        with pg_store._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(DISTINCT DATE_TRUNC('quarter', snapshot_date))
                       FROM mg_theme_snapshots
                       WHERE theme_id = (
                           SELECT id FROM mg_themes WHERE theme_slug = %s LIMIT 1
                       )""",
                    (theme.theme_slug,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0

    def _load_recent_snapshots(self, theme: InvestmentTheme, pg_store, months: int = 6, as_of_date=None) -> list[dict]:
        """Load recent strength snapshots for momentum calculation."""
        ceil = as_of_date if as_of_date else date.today()
        cutoff = ceil - timedelta(days=months * 30)
        with pg_store._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT snapshot_date, strength_score, momentum_score
                       FROM mg_theme_snapshots
                       WHERE theme_id = (
                           SELECT id FROM mg_themes WHERE theme_slug = %s LIMIT 1
                       ) AND snapshot_date >= %s AND snapshot_date <= %s
                       ORDER BY snapshot_date""",
                    (theme.theme_slug, cutoff, ceil),
                )
                return [{"snapshot_date": r[0], "strength_score": r[1], "momentum_score": r[2]}
                        for r in cur.fetchall()]

    def _count_capex_signals(self, theme: InvestmentTheme, pg_store) -> int:
        """Count capex signals from documents filed in the last 180 days."""
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT COUNT(*) FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE s.signal_type IN ('capex_increase', 'capex_decrease')
                             AND d.filed_at >= NOW() - INTERVAL '180 days'""",
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
        except Exception:
            return 0

    def _avg_signal_confidence(self, theme: InvestmentTheme, pg_store) -> float:
        """Average confidence of signals related to the theme."""
        with pg_store._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT AVG(s.confidence)
                       FROM mg_signals s
                       JOIN mg_document_entities de ON de.entity_id = s.entity_id
                       WHERE s.signal_type = ANY(%s)""",
                    (list(theme.signal_types or ["capex_increase"]),),
                )
                row = cur.fetchone()
                val = row[0] if row and row[0] is not None else 0.5
                return float(val)

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    def get_top_themes(
        self, ranked: list[RankedTheme], n: int = 10,
        conviction_filter: list[ThemeConviction] = None,
    ) -> list[RankedTheme]:
        filtered = ranked
        if conviction_filter:
            filtered = [r for r in ranked if r.theme.conviction in conviction_filter]
        return filtered[:n]

    def format_ranking_table(self, ranked: list[RankedTheme]) -> str:
        lines = [
            f"{'#':<4} {'Theme':<40} {'Score':<8} {'Tension':<9} {'Conv.':<12} "
            f"{'Cos.':<6} {'Q':<4} {'×Pers':<7} {'Elig.':<7} {'Chg':<6}",
            "-" * 124,
        ]
        for rt in ranked[:25]:
            t = rt.theme
            d = t.metadata.get("demand_count", "?") if t.metadata else "?"
            s = t.metadata.get("supply_constraint_count", "?") if t.metadata else "?"
            tension_label = f"D{d}/S{s}"
            elig_str = f"{rt.eligibility_score:.2f}"
            lines.append(
                f"{rt.rank:<4} {t.theme_name[:38]:<40} {rt.composite_score:<8.1f} "
                f"{tension_label:<9} {t.conviction.value:<12} {t.company_count:<6} "
                f"{rt.confirmed_quarters:<4} {rt.persistence_multiplier:<7.2f} "
                f"{elig_str:<7} {rt.rank_change_label:<6}"
            )
        return "\n".join(lines)

    def persist_ranks(self, ranked: list[RankedTheme], pg_store):
        if not pg_store:
            return
        for rt in ranked:
            try:
                pg_store.upsert_theme({
                    **rt.theme.to_dict(),
                    "strength_score": rt.composite_score,
                    "momentum_score": rt.momentum_score,
                })
            except Exception as e:
                logger.warning(f"Failed to persist theme {rt.theme.theme_slug}: {e}")
        logger.info(f"Persisted {len(ranked)} theme rankings to PostgreSQL")
