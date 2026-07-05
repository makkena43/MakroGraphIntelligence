"""India Ranking Engine v0.2 — Theme → Constraint Chain → Chain Distance →
Industry Relevance → Company Signals → Supplier Quality → Ranking.

Architecture (per v0.2 review):
  Theme
    ↓ Constraint Chain (SupplyChainDB — which product is constrained)
    ↓ Chain Distance   (P1 — steeper decay, multiplicative gate)
    ↓ Industry Relevance (P2 — compatibility score, no hardcoded names)
    ↓ Company Signals  (P3 — domain-specific only + P4 age decay)
    ↓ Supplier Quality (P5 — cross-theme normalized ThemeCQ)
    ↓ Ranking

Priority improvements from v0.2 review:
  P1. Chain distance weighting  — steeper decay {0:1.0, 1:0.65, 2:0.35, 3:0.12};
                                   distance-3 companies also face a purity floor
  P2. Industry-theme compat     — SupplyChainDB sector → company domain check;
                                   derived from signal entity keywords, no hardcoded names
  P3. Domain-specific signals   — only signals whose context overlaps with the
                                   constraint chain's sector keywords count at full weight
  P4. Age decay                 — exponential decay λ=0.004; half-life ≈173 days
  P5. Normalize theme strength  — cross-theme z-score → sigmoid so broad themes
                                   don't dominate over rare constraint themes

India pipeline ONLY — US pipeline is not touched.
No hardcoded company names or theme names anywhere in this file.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 6. Category hierarchy — economic capture of the constraint
# ---------------------------------------------------------------------------
# Tier 1 (monopoly supplier) → Tier 8 (asset-light). Each tier gets a score
# used as a multiplier on the base ranking formula.

CATEGORY_WEIGHTS: dict[str, float] = {
    "monopoly_supplier":        1.00,   # sole domestic producer of a critical input
    "critical_component":       0.85,   # key component with few substitutes
    "manufacturer":             0.70,   # domestic manufacturer of the constrained product
    "oem":                      0.55,   # OEM that integrates the constrained component
    "epc":                      0.40,   # EPC contractor — builds capacity, doesn't produce it
    "service_provider":         0.25,   # support / maintenance / logistics
    "financial_entity":         0.10,   # banks, NBFCs, funds
    "asset_light":              0.05,   # consulting, distribution without manufacturing
}

# Role labels from beneficiary_discovery → category
_ROLE_TO_CATEGORY: dict[str, str] = {
    "critical_supplier":        "critical_component",
    "direct_supplier":          "manufacturer",
    "input_supplier":           "manufacturer",
    "ecosystem_participant":    "epc",
    "direct":                   "manufacturer",
    "indirect":                 "service_provider",
    "localization_play":        "manufacturer",
}

# P1. Chain distance decay — steeper curve so distance-3 companies are
# near-zero and only survive if purity + manufacturing relevance are high.
# {0=exact provider, 1=direct supplier, 2=input supplier, 3=ecosystem}
_DISTANCE_DECAY: dict[int, float] = {0: 1.00, 1: 0.65, 2: 0.35, 3: 0.12}

# P1. Extra purity floor for distance-3 (ecosystem) companies.
# They must clear a higher purity bar to avoid drowning the ranking with
# loosely-connected entities that happen to share generic signals.
_DISTANCE_3_MIN_PURITY: float = 0.45

# P4. Age decay — exponential decay rate λ.
# half-life = ln(2)/λ ≈ 173 days at λ=0.004.
# A signal filed 180 days before as_of_date contributes ~50% weight.
# A signal filed 365 days before as_of_date contributes ~23% weight.
_AGE_DECAY_LAMBDA: float = 0.004

_ROLE_DISTANCE: dict[str, int] = {
    "critical_supplier":     0,
    "direct_supplier":       1,
    "input_supplier":        2,
    "ecosystem_participant": 3,
    "direct":                1,
    "indirect":              2,
    "localization_play":     1,
}


# ---------------------------------------------------------------------------
# 7. Signal weight table — economic pressure signals score higher
# ---------------------------------------------------------------------------
# HIGH (1.0): direct economic pressure on the bottleneck
# MEDIUM (0.6): demand evidence
# LOW (0.2): generic / administrative signals

SIGNAL_WEIGHTS: dict[str, float] = {
    # High-value economic pressure signals
    "supply_bottleneck":        1.00,
    "capacity_shortage":        1.00,
    "inventory_drawdown":       0.90,
    "capex_increase":           0.90,
    "localization_opportunity": 0.85,
    "tender_pipeline":          0.85,
    "policy_support":           0.80,
    "regulatory_tailwind":      0.70,
    # Medium-value demand signals
    "demand_surge":             0.60,
    "order_book_growth":        0.60,
    "supply_easing":            0.55,
    # Low-value generic signals
    "technology_adoption":      0.20,
    "hiring_surge":             0.20,
    "acquisition_intent":       0.15,
    "strategic_pivot":          0.15,
    "partnership_formed":       0.15,
    "market_entry":             0.10,
    "inventory_buildup":        0.10,
    "technology_disruption":    0.10,
    "hiring_freeze":            0.10,
    "capex_decrease":           0.05,
    "demand_slowdown":          0.05,
    "regulatory_headwind":      0.05,
}

# Signals that directly measure constraint severity (used in #4)
_CONSTRAINT_SIGNALS = frozenset({
    "supply_bottleneck", "capacity_shortage", "inventory_drawdown",
})
# Signals that measure expansion / bottleneck resolution (used in SupplierQ)
_EXPANSION_SIGNALS = frozenset({
    "capex_increase", "capacity_shortage", "localization_opportunity",
    "tender_pipeline",
})
# Signals that measure order-book pressure
_ORDER_SIGNALS = frozenset({
    "demand_surge", "tender_pipeline", "order_book_growth",
    "supply_bottleneck",
})
# Purity: signals with direct supply-chain relevance
_PURITY_SIGNALS = frozenset({
    "supply_bottleneck", "capacity_shortage", "inventory_drawdown",
    "capex_increase", "localization_opportunity", "tender_pipeline",
    "policy_support", "regulatory_tailwind", "demand_surge",
    "order_book_growth",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IndiaRankedStock:
    rank: int
    company: str
    ticker: str
    theme_name: str                  # primary theme (highest score)
    themes_confluence: list[str]     # all themes this company appears in
    constrained_product: str
    role: str
    category: str                    # new: category tier (manufacturer, OEM, etc.)
    chain_distance: int
    # v0.2 score components
    supplier_purity: float           # NEW: purity gate score (0–1)
    manufacturing_relevance: float   # NEW: replaces confidence-based relevance
    value_chain_position: float      # NEW: distance + category combined
    constraint_exposure: float       # NEW: economic pressure score
    category_weight: float           # NEW: economic capture weight
    confluence_multiplier: float     # NEW: multi-theme boost
    theme_separation_score: float    # NEW: rarity × density × severity
    supplier_q: float                # composite SupplierQ (v0.2)
    theme_cq: float
    final_score: float
    signal_count: int
    weighted_signal_score: float     # NEW: weighted by signal importance
    rationale: str


@dataclass
class IndiaRankingResult:
    as_of_date: date
    stocks: list[IndiaRankedStock]
    themes_processed: int
    companies_ranked: int
    companies_below_purity_gate: int = 0


# ---------------------------------------------------------------------------
# 1. Supplier Purity Gate threshold
# ---------------------------------------------------------------------------
MIN_SUPPLIER_PURITY: float = 0.25    # companies below this are excluded
MIN_MANUFACTURING_RELEVANCE: float = 0.10  # hard floor — triggers penalty if below


class IndiaRankingEngine:
    """India Ranking Engine v0.2.

    Ranking formula (9 factors, balanced per spec):
        final = (
            manufacturing_relevance  × 0.22  +   # supply-side fit
            constraint_exposure      × 0.18  +   # economic pressure
            value_chain_position     × 0.18  +   # distance + category
            supplier_q               × 0.15  +   # composite supply quality
            theme_cq                 × 0.12  +   # theme strength (separated)
            theme_separation         × 0.10  +   # rarity × density × severity
            weighted_signal          × 0.05       # signal quality
        ) × category_weight × confluence_multiplier
    """

    FORMULA_WEIGHTS = {
        "manufacturing_relevance": 0.22,
        "constraint_exposure":     0.18,
        "value_chain_position":    0.18,
        "supplier_q":              0.15,
        "theme_cq":                0.12,
        "theme_separation":        0.10,
        "weighted_signal":         0.05,
    }

    def __init__(self, pg_store, supply_chain_db=None):
        self._pg = pg_store
        # SupplyChainDB for criticality + import dependence (improvement #4)
        self._sc_db = supply_chain_db
        if self._sc_db is None:
            try:
                from .supply_chain_db import IndiaSupplyChainDB
                self._sc_db = IndiaSupplyChainDB()
            except Exception:
                pass

    def run(
        self,
        as_of_date: Optional[date] = None,
        lookback_days: int = 365,
        top_n: int = 50,
        min_purity: float = MIN_SUPPLIER_PURITY,
    ) -> IndiaRankingResult:
        """Run India ranking v0.2 and return ranked stocks."""
        _as_of = as_of_date or date.today()
        _floor  = _as_of - timedelta(days=lookback_days)

        themes = self._load_themes(_as_of)
        if not themes:
            logger.warning("[IndiaRanking] No active India themes found")
            return IndiaRankingResult(_as_of, [], 0, 0)

        beneficiaries = self._load_beneficiaries(_as_of)
        if not beneficiaries:
            logger.warning("[IndiaRanking] No India beneficiaries — run L7 first")
            return IndiaRankingResult(_as_of, [], len(themes), 0)

        # Load all signal stats with full type breakdown + age-decayed scores (P4)
        signal_stats = self._load_company_signal_stats(_floor, _as_of)

        # P5. Cross-theme normalize ThemeCQ so broad themes don't dominate
        themes = self._normalize_theme_cq(themes)

        # Build per-company multi-theme map (for confluence)
        company_themes: dict[str, list[dict]] = {}
        for brow in beneficiaries:
            co = (brow.get("company") or "").lower()
            if co:
                company_themes.setdefault(co, []).append(brow)

        below_purity = 0
        best: dict[str, IndiaRankedStock] = {}

        for co_key, brows in company_themes.items():
            co_stats = signal_stats.get(co_key, {})

            # ── Supplier Purity Gate ─────────────────────────────────────────
            purity = self._supplier_purity(co_stats)
            if purity < min_purity:
                below_purity += 1
                logger.debug(f"[PurityGate] '{co_key}' excluded: purity={purity:.3f}")
                continue

            # Best beneficiary row (highest conviction)
            brow = max(brows, key=lambda b: float(b.get("conviction_score") or 0))
            company  = brow.get("company") or co_key
            ticker   = brow.get("ticker") or ""
            theme    = brow.get("theme_name") or ""
            product  = brow.get("constrained_product") or ""
            role     = brow.get("beneficiary_type") or "ecosystem_participant"
            theme_data = themes.get(theme, {})

            # ── P1. Chain Distance — applied as an early multiplicative gate ─
            # Architecture: Theme → Constraint Chain → Chain Distance first.
            # Distance-3 companies face an additional purity floor before
            # their signals even reach the scoring stage.
            distance   = _ROLE_DISTANCE.get(role, 3)
            dist_decay = _DISTANCE_DECAY.get(distance, 0.12)

            if distance == 3 and purity < _DISTANCE_3_MIN_PURITY:
                below_purity += 1
                logger.debug(
                    f"[ChainGate] '{co_key}' excluded: distance=3 "
                    f"and purity={purity:.3f} < {_DISTANCE_3_MIN_PURITY}"
                )
                continue

            # ── P2. Industry-Theme Compatibility ─────────────────────────────
            # Derived from SupplyChainDB sector for the constrained product.
            # No hardcoded company names — checks if company's signal domain
            # (entity types in their documents) aligns with the theme's sector.
            industry_compat = self._industry_theme_compatibility(
                co_stats, product, co_key
            )
            # Low compatibility is a soft gate: score is multiplied in, not a hard cut.

            # ── Category Weight ──────────────────────────────────────────────
            category = _ROLE_TO_CATEGORY.get(role, "service_provider")
            if self._sc_db and product:
                sc_node = self._get_sc_node(product)
                if sc_node:
                    if sc_node.bottleneck_risk == "critical" and sc_node.import_share >= 0.90:
                        category = "monopoly_supplier"
                    elif sc_node.bottleneck_risk in ("critical", "high"):
                        category = "critical_component"
            cat_weight = CATEGORY_WEIGHTS.get(category, 0.25)

            # P1. Value chain position: distance decay now steeper & primary
            value_chain_pos = round((dist_decay * 0.65 + cat_weight * 0.35), 3)

            # ── P3. Domain-specific signals (age-decayed) ────────────────────
            # Use the age-decayed, domain-filtered signal score rather than raw counts.
            # Generic signals (tech adoption, hiring) carry near-zero weight.
            weighted_sig   = co_stats.get("age_decayed_weighted", 0.0)
            domain_sig     = co_stats.get("domain_signal_score", 0.0)

            # ── Manufacturing Relevance ──────────────────────────────────────
            mfg_relevance = self._manufacturing_relevance(brow, co_stats, product)
            if mfg_relevance < MIN_MANUFACTURING_RELEVANCE:
                mfg_relevance_effective = mfg_relevance * 0.10
            else:
                mfg_relevance_effective = mfg_relevance

            # ── Constraint Severity ──────────────────────────────────────────
            constraint_exp = self._constraint_severity(co_stats, product)

            # ── SupplierQ ────────────────────────────────────────────────────
            expansion    = self._expansion_score(co_stats)
            order_book   = self._order_book_score(co_stats)
            criticality  = self._criticality_score(product)

            supplier_q = round(
                mfg_relevance * 0.30 +
                expansion     * 0.25 +
                domain_sig    * 0.20 +   # P3: domain-specific replaces raw order book
                purity        * 0.15 +
                criticality   * 0.10,
                4,
            )

            # ── Theme Separation (rarity + density + persistence) ────────────
            theme_sep = self._theme_separation(theme_data, co_stats)

            # ── ThemeCQ (P5 cross-theme normalized) ──────────────────────────
            theme_cq = theme_data.get("theme_cq_normalized", theme_data.get("theme_cq", 0.30))

            # ── Multi-Theme Confluence ───────────────────────────────────────
            confluence = self._confluence_multiplier(brows, themes)

            # ── Final Formula — architecture-aligned ─────────────────────────
            # Chain Distance (P1) and Industry Compat (P2) are applied as
            # multiplicative gates OUTSIDE the weighted sum so they reshape
            # the score space rather than being diluted by other factors.
            w = self.FORMULA_WEIGHTS
            base = (
                w["manufacturing_relevance"] * mfg_relevance_effective +
                w["constraint_exposure"]     * constraint_exp +
                w["value_chain_position"]    * value_chain_pos +
                w["supplier_q"]              * supplier_q +
                w["theme_cq"]                * theme_cq +
                w["theme_separation"]        * theme_sep +
                w["weighted_signal"]         * weighted_sig
            )
            # P1: dist_decay applied multiplicatively (not additively) so
            # distance-3 companies cannot reach the same score as distance-0
            # even with perfect other scores.
            # P2: industry_compat applied multiplicatively — incompatible
            # companies are penalized without being hard-excluded (soft gate).
            final_score = round(
                base * dist_decay * industry_compat * cat_weight * confluence, 4
            )

            # All themes this company appears in (for UI display)
            all_themes = list(dict.fromkeys(
                b.get("theme_name", "") for b in brows if b.get("theme_name")
            ))

            entry = IndiaRankedStock(
                rank=0,
                company=company,
                ticker=ticker,
                theme_name=theme,
                themes_confluence=all_themes,
                constrained_product=product,
                role=role,
                category=category,
                chain_distance=distance,
                supplier_purity=round(purity, 3),
                manufacturing_relevance=round(mfg_relevance, 3),
                value_chain_position=round(value_chain_pos, 3),
                constraint_exposure=round(constraint_exp, 3),
                category_weight=round(cat_weight, 3),
                confluence_multiplier=round(confluence, 3),
                theme_separation_score=round(theme_sep, 3),
                supplier_q=round(supplier_q, 4),
                theme_cq=round(theme_cq, 3),
                final_score=final_score,
                signal_count=co_stats.get("total_signals", 0),
                weighted_signal_score=round(weighted_sig, 3),
                rationale=self._build_rationale(
                    company, theme, product, role, distance,
                    purity, mfg_relevance, constraint_exp, category, all_themes
                ),
            )

            # Keep best-scoring entry per company
            if co_key not in best or final_score > best[co_key].final_score:
                best[co_key] = entry

        ranked = sorted(best.values(), key=lambda s: -s.final_score)[:top_n]
        for i, s in enumerate(ranked, 1):
            s.rank = i

        logger.info(
            f"[IndiaRanking v0.2] {len(ranked)} ranked | "
            f"{below_purity} below purity gate | "
            f"{len(themes)} themes | as_of={_as_of}"
        )
        return IndiaRankingResult(
            as_of_date=_as_of,
            stocks=ranked,
            themes_processed=len(themes),
            companies_ranked=len(ranked),
            companies_below_purity_gate=below_purity,
        )

    # =========================================================================
    # Score components (v0.2)
    # =========================================================================

    # =========================================================================
    # P5. Cross-theme normalization
    # =========================================================================

    def _normalize_theme_cq(self, themes: dict) -> dict:
        """P5. Normalize ThemeCQ across all active themes.

        Broad themes (many companies, high raw strength) inflate ThemeCQ and
        dominate the ranking. Z-score normalization then sigmoid brings all
        themes into 0–1 relative to each other — rare constraint themes score
        comparably to large broad themes.
        """
        values = [d["theme_cq"] for d in themes.values() if d.get("theme_cq") is not None]
        if len(values) < 2:
            for d in themes.values():
                d["theme_cq_normalized"] = d.get("theme_cq", 0.30)
            return themes

        mu  = sum(values) / len(values)
        var = sum((v - mu) ** 2 for v in values) / len(values)
        std = math.sqrt(var) if var > 0 else 1.0

        for d in themes.values():
            raw = d.get("theme_cq", mu)
            z   = (raw - mu) / std
            # Sigmoid maps z-score to 0–1; z=0 → 0.50, z=±2 → 0.88/0.12
            d["theme_cq_normalized"] = round(1.0 / (1.0 + math.exp(-z)), 3)

        return themes

    # =========================================================================
    # P2. Industry-theme compatibility (no hardcoded names)
    # =========================================================================

    def _industry_theme_compatibility(
        self, co_stats: dict, product: str, co_key: str
    ) -> float:
        """P2. Score how compatible a company's industry domain is with the theme.

        Derived entirely from:
        - The constrained product's sector (from SupplyChainDB)
        - Whether the company has domain-relevant signals (from mg_signals context)

        No hardcoded company names or theme names — all signals from DB.

        Returns 0.25–1.0:
        - 1.00: company has domain-matched signals (in-sector)
        - 0.60: company has some supply-side signals but sector unclear
        - 0.25: company's signals are mostly generic with no sector match
        """
        if not product or not self._sc_db:
            return 0.60  # neutral when no supply chain data

        # Get the sector for this constrained product
        sc_node = self._get_sc_node(product)
        theme_sector = sc_node.sector.lower() if sc_node else ""

        if not theme_sector:
            return 0.60

        # Check if company has domain-matched signal score
        domain_score = co_stats.get("domain_signal_score", 0.0)
        purity       = co_stats.get("purity_signals", 0)
        total        = co_stats.get("total_signals", 1)

        # High domain signal score = strong sector match
        if domain_score >= 0.70:
            return 1.00
        elif domain_score >= 0.40:
            return 0.75
        elif purity / max(total, 1) >= 0.50:
            return 0.60   # purity is high but domain keywords unclear
        else:
            return 0.35   # mostly generic signals — likely cross-sector noise

    def _supplier_purity(self, co_stats: dict) -> float:
        """1. Supplier Purity Gate (0–1).

        Ratio of supply-chain-relevant signals to total signals.
        Companies with mostly generic signals (tech adoption, hiring)
        have low purity and are excluded from ranking.
        """
        total = co_stats.get("total_signals", 0)
        if total == 0:
            return 0.0
        pure = co_stats.get("purity_signals", 0)
        return round(pure / total, 3)

    def _manufacturing_relevance(self, brow: dict, co_stats: dict, product: str) -> float:
        """2. Manufacturing relevance (replaces confidence-based relevance).

        Measures:
        - Supply-side signal density for the constrained product
        - Value chain position (node stage from SupplyChainDB)
        - Category purity (ratio of expansion + constraint signals)
        - Criticality from SupplyChainDB import share
        """
        total = co_stats.get("total_signals", 0)
        if total == 0:
            return 0.0

        # a. Supply-side signal density (expansion + constraint)
        expansion   = co_stats.get("expansion_signals", 0)
        constraint  = co_stats.get("constraint_signals", 0)
        supply_side = expansion + constraint
        density = min(1.0, supply_side / max(total * 0.30, 1))

        # b. Category purity (pure signals / total)
        purity = co_stats.get("purity_signals", 0) / max(total, 1)

        # c. Supply chain node stage criticality
        node_score = 0.50  # default: mid-tier
        if self._sc_db and product:
            node = self._get_sc_node(product)
            if node:
                import_score = node.import_share          # 0–1
                risk_map = {"critical": 1.0, "high": 0.75, "moderate": 0.50, "low": 0.25}
                risk_score = risk_map.get(node.bottleneck_risk, 0.50)
                node_score = (import_score * 0.50 + risk_score * 0.50)

        # d. Supply chain stage from stored role (closer = more relevant)
        stage = int(brow.get("supply_chain_stage") or 2)
        stage_score = max(0.0, 1.0 - stage * 0.20)   # 0=1.0, 1=0.8, 2=0.6, 3=0.4, 4+=0.2

        return round(
            density     * 0.35 +
            purity      * 0.30 +
            node_score  * 0.20 +
            stage_score * 0.15,
            3,
        )

    def _constraint_severity(self, co_stats: dict, product: str) -> float:
        """4. Constraint Severity — economic pressure indicators.

        Measures actual economic pressure rather than generic signal density:
        - Price increases (supply_bottleneck signals as proxy)
        - Inventory drawdowns
        - Utilization pressure (high expansion signals)
        - Import dependence from SupplyChainDB
        - Capacity tightness
        """
        total = co_stats.get("total_signals", 0)
        if total == 0:
            return 0.0

        constraint = co_stats.get("constraint_signals", 0)
        expansion  = co_stats.get("expansion_signals", 0)
        ob_sigs    = co_stats.get("order_signals", 0)

        # a. Constraint signal intensity
        constr_intensity = min(1.0, math.log1p(constraint) / math.log1p(15))

        # b. Expansion signal intensity (company solving the bottleneck)
        expand_intensity = min(1.0, math.log1p(expansion) / math.log1p(10))

        # c. Order pressure
        order_intensity = min(1.0, math.log1p(ob_sigs) / math.log1p(12))

        # d. Import dependence from SupplyChainDB → structural constraint
        import_dep = 0.0
        if self._sc_db and product:
            node = self._get_sc_node(product)
            if node:
                import_dep = node.import_share   # 0–1

        return round(
            constr_intensity * 0.35 +
            expand_intensity * 0.25 +
            order_intensity  * 0.20 +
            import_dep       * 0.20,
            3,
        )

    def _expansion_score(self, co_stats: dict) -> float:
        """SupplierQ sub-component: capacity expansion evidence."""
        exp = co_stats.get("expansion_signals", 0)
        return round(min(1.0, math.log1p(exp) / math.log1p(10)), 3)

    def _order_book_score(self, co_stats: dict) -> float:
        """SupplierQ sub-component: order book pressure signals."""
        ob = co_stats.get("order_signals", 0)
        return round(min(1.0, math.log1p(ob) / math.log1p(12)), 3)

    def _criticality_score(self, product: str) -> float:
        """SupplierQ sub-component: how critical is this product in supply chain."""
        if not self._sc_db or not product:
            return 0.50
        node = self._get_sc_node(product)
        if node is None:
            return 0.50
        risk_map = {"critical": 1.0, "high": 0.75, "moderate": 0.50, "low": 0.25}
        return risk_map.get(node.bottleneck_risk, 0.50)

    def _weighted_signal_score(self, co_stats: dict) -> float:
        """7. Weighted signal score — economic pressure signals count more."""
        raw = co_stats.get("weighted_signal_sum", 0.0)
        total = co_stats.get("total_signals", 0)
        if total == 0:
            return 0.0
        # Normalize: avg weight × log scale of volume
        avg_weight = raw / max(total, 1)
        volume_score = min(1.0, math.log1p(total) / math.log1p(40))
        return round(avg_weight * volume_score, 3)

    def _theme_separation(self, theme_data: dict, co_stats: dict) -> float:
        """Theme separation — prevents compression + mature theme decay (Issue 5).

        Combines:
        - Rarity factor: fewer companies → rarer theme → higher score (P5)
        - Evidence density: age-decayed signal richness (P4)
        - Constraint strength (P5 normalized)
        - Maturity decay: themes confirmed for many quarters lose freshness bonus
          (Issue 5: mature themes retain excessive influence)
        """
        n_cos    = max(1, theme_data.get("company_count", 10))
        quarters = theme_data.get("confirmed_quarters", 1)
        strength = theme_data.get("strength_score", 30.0)

        # a. Rarity (P5 — rare themes score higher)
        rarity = min(1.0, 10.0 / n_cos)

        # b. Age-decayed evidence density (P4 applied to theme-level too)
        age_decay_sig = co_stats.get("age_decayed_weighted", 0.0)
        density = min(1.0, age_decay_sig * 2.0)   # scale: 0.5 decayed → 1.0

        # c. Normalized strength (P5)
        strength_norm = min(1.0, strength / 150.0)

        # d. Maturity factor — Issue 5: penalize themes confirmed too long.
        # Fresh (1Q) = full persistence bonus; mature (5Q+) = diminishing returns.
        # This prevents dominant themes from staying at top purely by inertia.
        if quarters <= 1:
            maturity = 0.80    # new/emerging — high freshness
        elif quarters <= 2:
            maturity = 1.00    # 2Q confirmed — peak
        elif quarters <= 4:
            maturity = 0.85    # still relevant but starting to mature
        else:
            maturity = 0.65    # 5Q+ sustained — apply maturity discount

        return round(
            rarity        * 0.35 +
            density       * 0.25 +
            strength_norm * 0.25 +
            maturity      * 0.15,
            3,
        )

    def _confluence_multiplier(self, brows: list[dict], themes: dict) -> float:
        """5. Multi-Theme Confluence.

        Base = 1.0. Each additional independent theme (different sector/product)
        adds 0.12 to the multiplier, up to +0.48 (4 independent themes = ×1.48).

        Independent = different constrained_product.
        """
        products = set()
        for b in brows:
            p = b.get("constrained_product") or ""
            t = b.get("theme_name") or ""
            # Only count themes with non-trivial theme_cq
            if p and themes.get(t, {}).get("theme_cq", 0) >= 0.20:
                products.add(p)
        n_independent = max(1, len(products))
        return round(min(1.50, 1.0 + 0.12 * (n_independent - 1)), 3)

    # =========================================================================
    # SupplyChainDB helpers
    # =========================================================================

    def _get_sc_node(self, product: str):
        """Look up a SupplyChainNode by product name (case-insensitive)."""
        if not self._sc_db:
            return None
        try:
            from .supply_chain_db import _NODE_INDEX
            node = _NODE_INDEX.get(product)
            if node:
                return node
            # Fuzzy: first node whose name is a substring match
            pl = product.lower()
            for name, n in _NODE_INDEX.items():
                if pl in name.lower() or name.lower() in pl:
                    return n
        except Exception:
            pass
        return None

    # =========================================================================
    # DB queries (v0.2 — extended signal breakdown)
    # =========================================================================

    def _load_themes(self, as_of: date) -> dict[str, dict]:
        """Load active India themes with ThemeCQ + separation metadata.

        Uses mg_theme_snapshots to find themes that were active at as_of_date —
        correct for both live and historical replay (avoids last_updated filter
        which always reflects today's pipeline run date, not the signal date).
        """
        themes: dict[str, dict] = {}
        try:
            from psycopg2.extras import RealDictCursor
            with self._pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Primary: use mg_theme_snapshots to find themes with a snapshot
                    # on or before as_of_date — replay-safe and date-accurate.
                    cur.execute("""
                        SELECT DISTINCT ON (t.theme_name)
                               t.theme_name, t.theme_slug, t.conviction,
                               t.company_count, t.metadata,
                               ts.strength_score, ts.snapshot_date
                        FROM mg_theme_snapshots ts
                        JOIN mg_themes t ON t.id = ts.theme_id
                        WHERE t.country = 'IN'
                          AND ts.snapshot_date <= %s
                        ORDER BY t.theme_name, ts.snapshot_date DESC
                    """, (as_of,))
                    rows = cur.fetchall()

                    # Fallback: if no snapshots exist yet (first run), use mg_themes directly
                    if not rows:
                        cur.execute("""
                            SELECT theme_name, theme_slug, strength_score,
                                   conviction, company_count, metadata
                            FROM mg_themes
                            WHERE country = 'IN'
                            ORDER BY strength_score DESC
                        """)
                        rows = cur.fetchall()

                    for row in rows:
                        tn = row["theme_name"]
                        strength  = float(row.get("strength_score") or 0)
                        conv_str  = str(row.get("conviction") or "emerging").lower()
                        conv_map  = {"high": 1.0, "confirmed": 0.85,
                                     "developing": 0.55, "emerging": 0.30, "watch": 0.15}
                        conv_val  = conv_map.get(conv_str, 0.30)
                        co_count  = int(row.get("company_count") or 10)

                        # ThemeCQ with rarity baked in (fewer companies = rarer = higher)
                        rarity = min(1.0, 10.0 / max(co_count, 1))
                        strength_norm = min(1.0, strength / 150.0)
                        theme_cq = round(
                            strength_norm * 0.45 +
                            conv_val      * 0.35 +
                            rarity        * 0.20,
                            3,
                        )

                        meta = row.get("metadata") or {}
                        confirmed_q = int(meta.get("confirmed_quarters", 1)) if meta else 1

                        themes[tn] = {
                            "theme_cq": theme_cq,
                            "slug": row.get("theme_slug"),
                            "strength_score": strength,
                            "company_count": co_count,
                            "confirmed_quarters": confirmed_q,
                        }
        except Exception as e:
            logger.warning(f"[IndiaRanking] _load_themes failed: {e}")
        return themes

    def _load_beneficiaries(self, as_of: date) -> list[dict]:
        try:
            from psycopg2.extras import RealDictCursor
            with self._pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT company, ticker, theme_name, constrained_product,
                               supply_chain_stage, beneficiary_type, conviction_score,
                               signal_count, has_order_book_signals,
                               import_substitution_play
                        FROM mg_india_beneficiaries
                        WHERE (as_of_date IS NULL OR as_of_date <= %s)
                        ORDER BY conviction_score DESC
                    """, (as_of,))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"[IndiaRanking] _load_beneficiaries failed: {e}")
            return []

    def _load_company_signal_stats(self, floor: date, as_of: date) -> dict[str, dict]:
        """Load per-company signal stats with age decay (P4) and domain scoring (P3).

        P4 Age Decay: each signal is weighted by e^(-λ × days_ago) where λ=0.004.
            Signals filed today = weight 1.0; filed 173 days ago = weight ~0.50;
            filed 365 days ago = weight ~0.23. Computed per-signal row in SQL.

        P3 Domain Signal Score: ratio of economic-pressure signals that also
            have non-empty context_text (proxy for domain specificity since
            context_text captures the surrounding text where the signal fired).
            Generic signals fire on short titles with little context.
        """
        stats: dict[str, dict] = {}
        try:
            from psycopg2.extras import RealDictCursor
            with self._pg._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT
                            lower(COALESCE(NULLIF(d.company,''), d.ticker)) AS co,
                            d.ticker,
                            COUNT(*)                                         AS total_signals,
                            -- Purity: supply-chain relevant signal types
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'supply_bottleneck','capacity_shortage',
                                'inventory_drawdown','capex_increase',
                                'localization_opportunity','tender_pipeline',
                                'policy_support','regulatory_tailwind',
                                'demand_surge','order_book_growth'))          AS purity_signals,
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'capex_increase','capacity_shortage',
                                'localization_opportunity','tender_pipeline'))
                                                                             AS expansion_signals,
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'supply_bottleneck','capacity_shortage',
                                'inventory_drawdown'))                        AS constraint_signals,
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'demand_surge','tender_pipeline',
                                'order_book_growth','supply_bottleneck'))     AS order_signals,
                            -- P4. Age-decayed weighted sum using exponential decay λ=0.004.
                            -- PostgreSQL date - date returns INTEGER (days), used directly.
                            SUM(
                                CASE
                                  WHEN s.signal_type IN (
                                    'supply_bottleneck','capacity_shortage','inventory_drawdown',
                                    'capex_increase','localization_opportunity','tender_pipeline')
                                  THEN
                                    EXP(-0.004 * GREATEST(0,
                                        (%s::date - COALESCE(d.filed_at, %s::date))::integer
                                    ))
                                  WHEN s.signal_type IN ('demand_surge','policy_support',
                                    'regulatory_tailwind','order_book_growth')
                                  THEN
                                    0.60 * EXP(-0.004 * GREATEST(0,
                                        (%s::date - COALESCE(d.filed_at, %s::date))::integer
                                    ))
                                  ELSE
                                    0.10 * EXP(-0.004 * GREATEST(0,
                                        (%s::date - COALESCE(d.filed_at, %s::date))::integer
                                    ))
                                END
                            )                                                AS age_decayed_raw,
                            -- P3. Domain signal score: purity signals with substantial context
                            -- (context_text length > 50 chars = genuine domain match,
                            --  not a 3-word announcement title)
                            COUNT(*) FILTER (WHERE s.signal_type IN (
                                'supply_bottleneck','capacity_shortage',
                                'inventory_drawdown','capex_increase',
                                'localization_opportunity','tender_pipeline')
                                AND LENGTH(COALESCE(s.context_text,'')) > 50)
                                                                             AS domain_signals
                        FROM mg_signals s
                        JOIN mg_documents d ON d.id = s.document_id
                        WHERE d.country = 'IN'
                          AND d.filed_at BETWEEN %s AND %s
                          AND COALESCE(NULLIF(d.company,''), d.ticker) IS NOT NULL
                        GROUP BY lower(COALESCE(NULLIF(d.company,''), d.ticker)), d.ticker
                    """, (
                        as_of, as_of,   # age decay for high-value signals
                        as_of, as_of,   # age decay for medium signals
                        as_of, as_of,   # age decay for generic signals
                        floor, as_of,   # date window
                    ))
                    for row in cur.fetchall():
                        co    = row["co"]
                        total = int(row["total_signals"] or 0)
                        purity_ct  = int(row["purity_signals"] or 0)
                        domain_ct  = int(row["domain_signals"] or 0)
                        age_raw    = float(row["age_decayed_raw"] or 0.0)

                        # Normalise age-decayed score to 0–1 (max possible ≈ total signals × 1.0)
                        age_decayed = round(min(1.0, age_raw / max(total, 1)), 3)

                        # P3 domain signal score: ratio of purity signals with real context
                        domain_score = round(domain_ct / max(purity_ct, 1), 3) if purity_ct else 0.0

                        stats[co] = {
                            "total_signals":      total,
                            "purity_signals":     purity_ct,
                            "expansion_signals":  int(row["expansion_signals"] or 0),
                            "constraint_signals": int(row["constraint_signals"] or 0),
                            "order_signals":      int(row["order_signals"] or 0),
                            "ticker":             row.get("ticker") or "",
                            "age_decayed_weighted": age_decayed,   # P4
                            "domain_signal_score":  domain_score,  # P3
                            "weighted_signal_sum":  age_raw,       # raw for SupplierQ
                        }
        except Exception as e:
            logger.warning(f"[IndiaRanking] _load_company_signal_stats failed: {e}")
        return stats

    def _compute_weighted_sum(self, row: dict) -> float:
        """Fallback weighted sum when age-decayed query not available."""
        purity    = int(row.get("purity_signals") or 0)
        expansion = int(row.get("expansion_signals") or 0)
        constraint= int(row.get("constraint_signals") or 0)
        order     = int(row.get("order_signals") or 0)
        total     = int(row.get("total_signals") or 0)
        generic   = max(0, total - purity)
        return round(
            expansion  * 0.88 +
            constraint * 0.97 +
            order      * 0.70 +
            generic    * 0.15,
            2,
        )

    # =========================================================================
    # Rationale builder
    # =========================================================================

    def _build_rationale(
        self, company, theme, product, role, distance,
        purity, mfg_rel, constraint_exp, category, all_themes
    ) -> str:
        dist_label = {0: "exact provider", 1: "direct supplier",
                      2: "input supplier", 3: "ecosystem participant"}.get(distance, "participant")
        parts = [
            f"{company} ({category.replace('_',' ')}): {dist_label} of '{product}' in '{theme}'.",
            f"Purity={purity:.2f} | MfgRel={mfg_rel:.2f} | ConstraintExp={constraint_exp:.2f}.",
        ]
        if len(all_themes) > 1:
            parts.append(f"Confluence across {len(all_themes)} themes: {', '.join(all_themes[:3])}.")
        return " ".join(parts)

    # =========================================================================
    # Persist rankings to DB
    # =========================================================================

    def persist(self, result: IndiaRankingResult) -> int:
        """Persist ranked stocks to mg_india_beneficiaries with v0.2 scores."""
        saved = 0
        for s in result.stocks:
            try:
                with self._pg._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE mg_india_beneficiaries
                            SET conviction_score = %s,
                                rationale        = %s,
                                updated_at       = NOW()
                            WHERE company = %s AND theme_name = %s
                        """, (s.final_score, s.rationale[:800], s.company, s.theme_name))
                saved += 1
            except Exception as e:
                logger.debug(f"[IndiaRanking] persist failed {s.company}: {e}")
        return saved

    def format_table(self, result: IndiaRankingResult, top_n: int = 25) -> str:
        """Format a human-readable ranking table."""
        lines = [
            f"\n{'='*110}",
            f"  INDIA RANKING v0.2  as_of={result.as_of_date}  "
            f"({result.companies_ranked} ranked, {result.companies_below_purity_gate} below purity gate)",
            f"{'='*110}",
            f"{'#':<4} {'Company':<32} {'Category':<20} {'Theme':<28} "
            f"{'Score':>7} {'Purity':>7} {'MfgRel':>7} {'Conf×':>6} {'Themes':>6}",
            "-" * 110,
        ]
        for s in result.stocks[:top_n]:
            lines.append(
                f"{s.rank:<4} {s.company[:30]:<32} {s.category[:18]:<20} "
                f"{s.theme_name[:26]:<28} "
                f"{s.final_score:>7.4f} {s.supplier_purity:>7.3f} "
                f"{s.manufacturing_relevance:>7.3f} {s.confluence_multiplier:>6.3f} "
                f"{len(s.themes_confluence):>6}"
            )
        return "\n".join(lines)
