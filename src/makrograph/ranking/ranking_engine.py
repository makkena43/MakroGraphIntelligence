"""
Thematic Investing Ranking Engine  v6
======================================
Post-processing *decision layer* — NOT a separate pipeline step.

v6 architectural correction: CQ now belongs to the THEME/EDGE, not the company.

Core problem fixed: v5 attached CQ to each company by averaging graph properties,
making CQ a near-constant multiplier (0.52–0.60) that changed nothing.

Correct architecture (v6):
  MacroTheme → Bottleneck → Critical Constraint → Supply Chain Position → Company

  1. ThemeCQ scored at THEME LEVEL (independent of any company).
     "AI Critical Shortage" (25 companies) → theme_cq ≈ 0.76
     "Materials: Constraint from AI Demand" (315 companies) → theme_cq ≈ 0.12
     6× spread between specific bottleneck and broad generic themes.

  2. EdgeCQ = theme_cq × RoleDistanceDecay × BottleneckSignalFactor
     Companies inherit CQ from their BEST bottleneck edge; they do not own it.

  3. Final score: BEST-EDGE DOMINANT (not theme-count additive)
     final = (0.55×best_edge_cq + 0.25×avg_top3_cq + 0.20×supplier_q)
             × (1 + confluence_bonus) × cat_weight
     Confluence bonus is logarithmic, capped at +25%, and deduplicates
     same-macro themes — so 17-theme AZO can't beat 2-theme ANET.

  4. RoleDistanceDecay (user spec):
     hop=0 (rank 1–10):   direct supplier → 1.0
     hop=1 (rank 11–25):  1-hop           → 0.8
     hop=2 (rank 26–50):  2-hop           → 0.6
     hop=3 (rank 51–100): 3-hop           → 0.3
     hop=4 (rank >100):   peripheral      → 0.1

  5. BreadthPenalty: max(0.25, min(1.0, 60/company_count))
     315-company theme gets 0.25× multiplier before any company sees it.
     25-company theme gets 1.0× — full CQ value passes through.

  6. BottleneckSignalFactor: rewards capex_increase, supply_bottleneck,
     inventory_drawdown, supply_constraint, capacity_expansion.
     Generic signals (hiring_freeze, partnership_formed) don't boost CQ.

  7. Persistence via momentum × conviction (not age).
     Declining momentum confirmed theme ≠ persistent constraint.
     Age alone doesn't make a constraint structural.

  8. Theme deduplication: themes cluster by macro driver (AI, Semiconductor,
     Power, Networking, Cloud, etc.). Only best edge per cluster counts toward
     confluence bonus — same constraint appearing 5× as different theme names
     doesn't stack.

Same-results fix (2021 ≡ 2022) carried from v4:
  - Window-relative novelty (vs max snapshot date IN the window).
  - ticker_signals via mg_documents.ticker (real per-year signals).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

# ── Role normalisation (DB → canonical) ──────────────────────────────────────
ROLE_NORMALIZE: dict[str, str] = {
    "supplier":                "supply",
    "bottleneck_player":       "supply",
    "infrastructure_provider": "supply",
    "beneficiary":             "beneficiary",
    "direct":                  "direct",
}

# ── Signal type clusters ──────────────────────────────────────────────────────
_SUPPLY_SIG = {
    "supply_bottleneck", "capex_increase", "inventory_drawdown", "hiring_surge",
    "supply_constraint", "capacity_expansion", "infrastructure_spend",
    "inventory_buildup",
}
_BENEFICIARY_SIG = {
    "demand_surge", "technology_adoption", "regulatory_tailwind", "market_entry",
    "revenue_growth", "market_share_gain", "end_market_growth",
    "technology_disruption",
}
_DIRECT_SIG = {
    "strategic_pivot", "acquisition_intent", "partnership_formed",
    "product_launch", "r_and_d_increase", "guidance_raise",
}
_NEGATIVE_SIG = {
    "competition_threat", "regulatory_headwind", "demand_slowdown",
    "supply_easing", "hiring_freeze", "margin_compression",
    "inventory_buildup",
}

# ── Bottleneck-specific signals (these are genuine constraint evidence) ────────
# Generic signals (hiring_freeze, partnership_formed, acquisition_intent)
# are excluded — they don't indicate supply-chain bottleneck presence.
_BOTTLENECK_SIGNALS: set[str] = {
    "supply_bottleneck", "capex_increase", "inventory_drawdown",
    "supply_constraint", "capacity_expansion", "infrastructure_spend",
}

# ── Category weights ──────────────────────────────────────────────────────────
CATEGORY_WEIGHT: dict[str, float] = {
    "supply":      1.5,
    "beneficiary": 1.2,
    "direct":      0.8,
}

# ── Conviction → score ────────────────────────────────────────────────────────
_CONVICTION_SCORE = {"confirmed": 1.0, "developing": 0.60, "emerging": 0.25}

# ── Bottleneck keyword tiers for ThemeCQ ─────────────────────────────────────
# Evaluated in order; first match wins.
# 0.90 = critical chokepoint  |  0.25 = default generic theme
_BN_TIERS: list[tuple[list[str], float]] = [
    (
        ["severe constraint", "critical shortage", "ai chips", "hbm",
         "advanced packaging", "gpu shortage", "chip shortage"],
        0.90,
    ),
    (
        ["semiconductor", "data center", "networking", "power grid",
         "liquid cooling", "inference cluster", "training cluster",
         "artificial intelligence critical", "gpu", "high-bandwidth",
         "wafer", "fab capacity"],
        0.78,
    ),
    (
        ["shortage", "constraint", "infrastructure", "capacity limit",
         "supply gap", "bottleneck", "interconnect", "optical", "power"],
        0.55,
    ),
    (
        ["demand-supply tension", "supply tension", "chip demand",
         "equipment demand"],
        0.40,
    ),
    (
        ["constraint from", "materials:", "cloud:", "energy:", "demand from"],
        0.35,
    ),
]
_BN_DEFAULT = 0.25

# ── Role-distance decay (user specification) ──────────────────────────────────
# hop inferred from rank_in_theme: 1-10=hop0, 11-25=hop1, 26-50=hop2,
# 51-100=hop3, >100=hop4.
_ROLE_DIST_DECAY: dict[int, float] = {
    0: 1.0,   # direct supplier
    1: 0.80,  # 1-hop
    2: 0.60,  # 2-hop
    3: 0.30,  # 3-hop
    4: 0.10,  # peripheral
}

# ── Macro-cluster map for theme deduplication ─────────────────────────────────
# Same constraint appearing under multiple theme names (e.g., "AI demand",
# "Cloud AI demand", "Artificial Intelligence Shortage") only earns one
# independent confluence point rather than stacking linearly.
_MACRO_CLUSTERS: list[tuple[list[str], str]] = [
    (["artificial intelligence critical", "ai chips", "gpu shortage",
      "generative ai", "llm", "machine learning"], "ai_bottleneck"),
    (["semiconductor", "chip", "wafer", "fab", "hbm", "advanced packaging",
      "chiplet"], "semiconductor"),
    (["data center", "hyperscaler"], "cloud_infra"),
    (["cloud", "saas", "software", "ai demand", "artificial intelligence"], "cloud_sw"),
    (["power grid", "power", "energy", "electricity", "grid",
      "cooling", "thermal", "liquid cool"], "power"),
    (["networking", "network", "switch", "router", "interconnect",
      "optical"], "networking"),
    (["defense", "military", "aerospace"], "defense"),
    (["cybersecurity", "security", "cyber"], "cyber"),
    (["supply chain", "logistics", "shipping", "freight"], "logistics"),
    (["china", "geopolitical", "tariff", "trade war"], "geopolitical"),
    (["biotech", "pharma", "drug", "therapeutic"], "biotech"),
    (["consumer", "retail", "shopping", "auto parts", "home improvement",
      "cruise", "travel", "restaurant", "food"], "consumer"),
]
_MACRO_DEFAULT_CLUSTER = "other"

# ── Edge threshold (still used to filter out graph noise) ────────────────────
EDGE_THRESHOLD  = 0.03      # loose relevance gate — keeps specialized themes whose
                            # signal_count=0 in beneficiary table (relevance alone passes)
MIN_EDGE_CQ     = 0.02      # minimum edge_cq to enter company_map (removes true noise)
HOP_CUTOFF      = 4         # drop rank>100 peripheral entries
CQ_FLOOR        = 0.45      # any theme above this CQ enters regardless of rank_score rank
                            # ensures specific bottleneck themes are never excluded by
                            # momentum-dominated top-N sorting

# ── Theme scoring weights (rank_score, kept for display) ──────────────────────
_TW = {
    "momentum":         0.30,
    "persistence":      0.20,
    "novelty":          0.15,
    "acceleration":     0.15,
    "signal_intensity": 0.15,
    "bottleneck":       0.05,
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ThemeScore:
    theme_id:         int
    theme_name:       str
    theme_slug:       str
    conviction:       str
    momentum:         float        = 0.0
    persistence:      float        = 0.0
    novelty:          float        = 0.0
    acceleration:     float        = 0.0
    signal_intensity: float        = 0.0
    bottleneck:       float        = 0.0
    company_count:    int          = 0
    first_detected:   "date|None"  = None
    # v6: Theme-level ConstraintQuality.  Computed once per theme; the same
    # value propagates to ALL companies in that theme.  High-CQ themes promote
    # all their direct participants; low-CQ themes can't be spam-stacked.
    theme_cq:         float        = 0.0

    @property
    def rank_score(self) -> float:
        return (
            self.momentum         * _TW["momentum"]         +
            self.persistence      * _TW["persistence"]      +
            self.novelty          * _TW["novelty"]          +
            self.acceleration     * _TW["acceleration"]     +
            self.signal_intensity * _TW["signal_intensity"] +
            self.bottleneck       * _TW["bottleneck"]
        )

    @property
    def rank_score_pct(self) -> float:
        return round(self.rank_score * 100, 1)


@dataclass
class StockRanking:
    ticker:             str
    company_name:       str
    themes:             list[str]        = field(default_factory=list)
    theme_slugs:        list[str]        = field(default_factory=list)
    company_role:       str              = ""
    role_confidence:    float            = 0.0
    # When this stock was FIRST identified as a beneficiary for any theme.
    # None means the date is unknown (older data before field was added).
    first_seen_at:      "date | None"    = None
    # v6: effective_theme = best single edge_cq score (primary ranking signal)
    effective_theme:    float            = 0.0
    supplier_quality:   float            = 0.0
    edge_score:         float            = 0.0   # avg relevance edge
    confluence_score:   float            = 0.0   # independent-cluster count
    category_weight:    float            = 1.0
    final_score:        float            = 0.0
    rank:               int              = 0
    signal_highlights:  list[str]        = field(default_factory=list)
    quality_breakdown:  dict[str, float] = field(default_factory=dict)
    conf_breakdown:     dict[str, float] = field(default_factory=dict)
    per_theme_edges:    dict[str, float] = field(default_factory=dict)
    # v6: constraint quality at the edge level (inherited from theme, not company)
    constraint_quality: float            = 0.0
    cq_breakdown:       dict             = field(default_factory=dict)


# ── Engine ────────────────────────────────────────────────────────────────────

class RankingEngine:
    """
    Pure decision layer — reads pg_store data, never writes pipeline state.

    v6 scoring flow:
      1. Score themes → compute theme_cq for each (independent of companies).
      2. For each company-theme edge: edge_cq = theme_cq × role_dist × signal_factor.
      3. Company final score = best_edge_cq dominant + tight log confluence bonus.

    This ensures infrastructure bottleneck companies (ANET, VRT, LRCX) rank
    above generic demand-side names (COST, AMZN, AZO) even when the latter
    appear in more themes.
    """

    def __init__(self, pg_store):
        self._pg = pg_store

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────────────

    def run(
        self,
        date_from:       date,
        date_to:         date,
        top_n_themes:    int   = 15,   # raised from 10; dual-criterion adds more
        min_final_score: float = 0.0,
        country:         str   = "US",
    ) -> tuple[list[ThemeScore], list[StockRanking]]:
        """Full ranking pass.  Returns (ranked_themes, ranked_stocks)."""
        logger.info("RankingEngine v6  %s → %s  top_n=%d  country=%s", date_from, date_to, top_n_themes, country)

        data = self._pg.get_ranking_data(date_from=date_from, date_to=date_to, country=country)

        # ── Step 1: Score themes (6-factor + theme_cq) ────────────────────────
        theme_scores = self._score_themes(data)
        theme_scores.sort(key=lambda t: t.rank_score, reverse=True)

        # Dual-criterion theme pool — the root cause of v5 persistence:
        # Sorting by rank_score (momentum) puts broad generic themes first.
        # "Materials: Constraint from AI Demand" (315 companies, many signals)
        # ranks above "AI Critical Shortage" (25 companies, fewer signals),
        # so ANET/VRT/LRCX are excluded before CQ scoring even runs.
        #
        # Fix: union of top-N by rank_score AND any theme above the CQ floor.
        # High-CQ themes (specific bottlenecks) are always in the pool.
        by_rank   = {t.theme_id for t in theme_scores[:top_n_themes]}
        by_cq     = {t.theme_id for t in theme_scores if t.theme_cq >= CQ_FLOOR}
        active_ids = by_rank | by_cq
        top_themes = [t for t in theme_scores if t.theme_id in active_ids]

        if not top_themes:
            logger.warning("No themes scored for %s → %s", date_from, date_to)
            return [], []

        top_theme_map: dict[int, ThemeScore] = {t.theme_id: t for t in top_themes}
        top_theme_ids = set(top_theme_map)
        top_n_actual  = len(top_themes)

        logger.debug(
            "v6 theme pool: %d by rank + %d by CQ≥%.2f = %d total active",
            len(by_rank), len(by_cq), CQ_FLOOR, len(top_themes),
        )

        # ── Step 2: Build signal lookup ───────────────────────────────────────
        sig_by_ticker: dict[str, list[dict]] = {}
        entity_meta:   dict[int, dict]       = {}

        for s in data.get("ticker_signals", []):
            tkr = (s.get("ticker") or "").strip().upper()
            if tkr:
                sig_by_ticker.setdefault(tkr, []).append(s)

        for em in data.get("entity_meta", []):
            eid = em.get("entity_id") or em.get("id")
            if eid:
                entity_meta[eid] = em

        logger.debug(
            "v6 signals: %d tickers with signals, %d entity-meta records",
            len(sig_by_ticker), len(entity_meta),
        )

        # ── Step 3: Scaling constants ─────────────────────────────────────────
        top_bene    = [b for b in data["beneficiaries"] if b["theme_id"] in top_theme_ids]
        max_signals = max((int(b.get("signal_count") or 0) for b in top_bene), default=1) or 1
        max_mention = max(
            (int(em.get("mention_count") or 1) for em in entity_meta.values()), default=2
        ) or 2

        # Log theme_cq spread to verify discrimination
        cq_vals = [ts.theme_cq for ts in top_themes]
        logger.debug(
            "v6 theme_cq spread: min=%.3f  max=%.3f  (spread = %.2f×)",
            min(cq_vals, default=0), max(cq_vals, default=0),
            (max(cq_vals, default=1) / max(min(cq_vals, default=1), 0.01)),
        )

        # ── Step 4: Accumulate per-company data across top themes ─────────────
        company_map: dict[str, dict] = {}

        for b in top_bene:
            ticker = (b.get("ticker") or "").strip().upper()
            if not ticker:
                continue

            theme_id = b["theme_id"]
            rank_pos = int(b.get("rank_in_theme") or 50)
            hop      = self._hop_distance(rank_pos)
            if hop > HOP_CUTOFF:
                continue

            eid         = b.get("entity_id")
            ent_meta    = entity_meta.get(eid, {}) if eid else {}
            stored_role = (b.get("company_role") or b.get("beneficiary_type") or "").lower()
            ticker_sigs = sig_by_ticker.get(ticker, [])

            # Role + confidence
            role, role_conf = self._infer_role_v4(
                stored_role, ticker_sigs, ent_meta,
                relevance    = float(b.get("relevance_score") or 0),
                signal_count = int(b.get("signal_count") or 0),
                max_signals  = max_signals,
                max_mention  = max_mention,
            )

            # Gate 1: loose relevance/evidence filter (EDGE_THRESHOLD=0.03 now)
            # Keeps graph clean without penalising specialised themes whose
            # signal_count=0 in the beneficiary table.  A company with
            # relevance=34 and no stored signals still passes at hop=2.
            ts   = top_theme_map[theme_id]
            edge = self._edge_score_v4(b, role_conf, ts.rank_score, max_signals, hop)
            if edge < EDGE_THRESHOLD:
                continue

            # v6: EdgeCQ = theme_cq × role-distance decay × bottleneck signal factor
            # theme_cq is the constraint quality of the THEME — companies inherit it.
            # "AI Critical Shortage" theme_cq=0.77 propagates to all its companies.
            # "Materials: Constraint from AI" theme_cq=0.12 propagates to all of its.
            role_dist     = _ROLE_DIST_DECAY.get(hop, 0.10)
            bn_sig_ratio  = self._bottleneck_signal_ratio(ticker_sigs)
            # Floor at 0.40: no bottleneck signals → 40% of theme CQ passes through.
            # Prevents over-penalising infrastructure cos that file generic calls.
            signal_factor = 0.40 + 0.60 * bn_sig_ratio
            edge_cq       = ts.theme_cq * role_dist * signal_factor

            # Gate 2: minimum edge_cq (true noise filter, replaces the aggressive
            # edge threshold).  Entries below this are genuine graph noise.
            if edge_cq < MIN_EDGE_CQ:
                continue

            b_first_seen = b.get("first_seen_at")

            if ticker not in company_map:
                company_map[ticker] = {
                    "company_name":  b.get("company_name") or ticker,
                    "roles":         {},
                    "theme_entries": [],
                    "signal_count":  0,
                    "ticker_sigs":   ticker_sigs,
                    "entity_id":     eid,
                    "theme_ids":     set(),
                    "first_seen_at": b_first_seen,
                }

            cm = company_map[ticker]
            cm["roles"][role] = max(cm["roles"].get(role, 0.0), role_conf)
            cm["theme_ids"].add(theme_id)
            # Track the EARLIEST first_seen_at across all theme entries
            if b_first_seen is not None:
                if cm["first_seen_at"] is None or b_first_seen < cm["first_seen_at"]:
                    cm["first_seen_at"] = b_first_seen
            cm["theme_entries"].append({
                "theme_id":      theme_id,
                "theme_ts":      ts,
                "edge":          edge,
                "edge_cq":       edge_cq,
                "role_conf":     role_conf,
                "role":          role,
                "hop":           hop,
                "rank_pos":      rank_pos,
                "relevance":     float(b.get("relevance_score") or 0),
                "sig_count":     int(b.get("signal_count") or 0),
                "signal_factor": signal_factor,
            })
            cm["signal_count"] += int(b.get("signal_count") or 0)

        logger.debug(
            "v6 company_map: %d companies  edge_gate=%.2f  min_cq=%.2f",
            len(company_map), EDGE_THRESHOLD, MIN_EDGE_CQ,
        )

        # ── Step 5: Score each company — best-edge dominant ───────────────────
        rankings: list[StockRanking] = []

        for ticker, cm in company_map.items():
            entries    = cm["theme_entries"]
            role       = self._primary_role(cm["roles"])
            role_conf  = cm["roles"].get(role, 0.30)
            cat_weight = CATEGORY_WEIGHT.get(role, 1.0)
            n_themes   = len(cm["theme_ids"])
            avg_edge   = sum(e["edge"] for e in entries) / len(entries)

            # Sort entries by edge_cq DESC so best bottleneck edge is first
            sorted_ent = sorted(entries, key=lambda e: e["edge_cq"], reverse=True)
            best_entry  = sorted_ent[0]
            best_edge_cq = best_entry["edge_cq"]

            # Top-3 average (weighted toward best constraint)
            top3_cq  = [e["edge_cq"] for e in sorted_ent[:3]]
            avg_top3 = sum(top3_cq) / len(top3_cq)

            # Supplier quality (structural supply-chain positioning)
            best_relevance = max(e["relevance"] for e in entries)
            best_sig_count = max(e["sig_count"] for e in entries)
            supplier_q, quality_bd = self._supplier_quality_v4(
                role, role_conf, entries,
                best_relevance, best_sig_count, max_signals,
                n_themes, top_n_actual,
            )

            # ── Theme deduplication: count INDEPENDENT macro constraints ──────
            # Two themes about "AI demand" and "Cloud AI demand" share the same
            # macro driver → counted as one constraint, not two.
            # A company with AI + Semiconductor + Power edges earns 3 independent
            # constraints, yielding a meaningful confluence bonus.
            seen_clusters: set[str] = set()
            n_independent = 0
            for e in sorted_ent:
                cluster = self._macro_cluster(e["theme_ts"].theme_name)
                if (cluster not in seen_clusters
                        and e["edge_cq"] > 0.25 * best_edge_cq):
                    seen_clusters.add(cluster)
                    n_independent += 1

            # Logarithmic confluence bonus, capped at +25%.
            # log(1+1)/log(5)=0.43 → +10.7% for 1 independent constraint
            # log(1+3)/log(5)=0.86 → +21.5% for 3 independent constraints
            # log(1+17)/log(5)≈1.77→ capped at +25% for 17 same-macro themes
            confluence_bonus  = min(0.25, math.log(1 + n_independent) / math.log(5))
            confluence_display = round(n_independent + confluence_bonus, 3)

            # CQ breakdown for display: show which theme drives the best edge
            best_ts  = best_entry["theme_ts"]
            cq_bd = {
                "Best Theme":    best_ts.theme_name[:40],
                "Theme CQ":      round(best_ts.theme_cq, 3),
                "Role Decay":    round(_ROLE_DIST_DECAY.get(best_entry["hop"], 0.1), 2),
                "Signal Factor": round(best_entry["signal_factor"], 3),
                "Best Edge CQ":  round(best_edge_cq, 3),
                "N Constraints": n_independent,
                "Conf Bonus":    round(confluence_bonus, 3),
            }

            # ── Final score: BEST-EDGE DOMINANT ──────────────────────────────
            # This is the correct architecture: a company's score is primarily
            # how good its BEST constraint-quality connection is, not how many
            # themes it appears in.
            #
            # (0.55 × best_edge_cq)      ← best bottleneck quality
            # (0.25 × avg_top3)          ← corroborating evidence from top-3 edges
            # (0.20 × supplier_q)        ← structural supply-chain positioning
            # × (1 + confluence_bonus)   ← small bonus for independent constraints
            # × cat_weight               ← supply > beneficiary > direct
            final_score = (
                0.55 * best_edge_cq +
                0.25 * avg_top3     +
                0.20 * supplier_q
            ) * (1.0 + confluence_bonus) * cat_weight

            if final_score < min_final_score:
                continue

            # Build ordered output (de-duped, sorted by edge_cq desc)
            seen:         set[int]         = set()
            uniq_names:   list[str]        = []
            uniq_slugs:   list[str]        = []
            per_theme_ed: dict[str, float] = {}
            for e in sorted_ent:
                if e["theme_id"] not in seen:
                    seen.add(e["theme_id"])
                    uniq_names.append(e["theme_ts"].theme_name)
                    uniq_slugs.append(e["theme_ts"].theme_slug)
                    per_theme_ed[e["theme_ts"].theme_slug] = round(e["edge_cq"], 4)

            conf_bd = self._conf_breakdown_v4(
                role, cm["ticker_sigs"],
                entity_meta.get(cm.get("entity_id"), {}),
                cm["signal_count"], max_signals, max_mention,
            )

            rankings.append(StockRanking(
                ticker             = ticker,
                company_name       = cm["company_name"],
                themes             = uniq_names,
                theme_slugs        = uniq_slugs,
                company_role       = role,
                role_confidence    = round(role_conf, 3),
                first_seen_at      = cm.get("first_seen_at"),
                effective_theme    = round(best_edge_cq, 4),   # = best constraint quality
                supplier_quality   = round(supplier_q, 4),
                edge_score         = round(avg_edge, 4),
                confluence_score   = round(confluence_display, 3),
                category_weight    = cat_weight,
                final_score        = round(final_score, 4),
                signal_highlights  = self._signal_highlights_v4(cm["ticker_sigs"]),
                quality_breakdown  = quality_bd,
                conf_breakdown     = conf_bd,
                per_theme_edges    = per_theme_ed,
                constraint_quality = round(best_edge_cq, 4),
                cq_breakdown       = cq_bd,
            ))

        rankings.sort(key=lambda r: r.final_score, reverse=True)
        for i, r in enumerate(rankings, 1):
            r.rank = i

        eff_vals = [r.effective_theme for r in rankings]
        logger.info(
            "v6 done  themes=%d  stocks=%d  best_edge_cq=%.3f–%.3f  "
            "final=%.3f–%.3f  edge_thr=%.2f",
            len(top_themes), len(rankings),
            min(eff_vals, default=0), max(eff_vals, default=0),
            min((r.final_score for r in rankings), default=0),
            max((r.final_score for r in rankings), default=0),
            EDGE_THRESHOLD,
        )
        return top_themes, rankings

    # ─────────────────────────────────────────────────────────────────────────
    # THEME SCORING  (6-factor + ThemeCQ — window-relative)
    # ─────────────────────────────────────────────────────────────────────────

    def _score_themes(self, data: dict) -> list[ThemeScore]:
        themes_raw    = data["themes"]
        snapshots     = data["snapshots"]
        chains        = data["chains"]
        theme_sig_cnt = data.get("theme_signal_counts", {})
        theme_sig_all = data.get("theme_signal_counts_all", {})

        snap_by_theme: dict[int, list[dict]] = {}
        for s in snapshots:
            snap_by_theme.setdefault(s["theme_id"], []).append(s)
        for v in snap_by_theme.values():
            v.sort(key=lambda x: x["snapshot_date"])

        max_quarters = max((len(v) for v in snap_by_theme.values()), default=1) or 1
        max_sig_cnt  = max(theme_sig_cnt.values(), default=1) or 1

        # Window-relative novelty reference (same-results fix)
        all_last_dates = [v[-1]["snapshot_date"] for v in snap_by_theme.values() if v]
        window_latest  = max(all_last_dates) if all_last_dates else date.today()

        chain_kws: set[str] = set()
        for c in chains:
            for word in (c.get("chain_name") or "").lower().split():
                if len(word) > 4:
                    chain_kws.add(word)

        scored: list[ThemeScore] = []

        for t in themes_raw:
            snaps          = snap_by_theme.get(t["id"], [])
            theme_name     = (t.get("theme_name") or "").strip()
            theme_slug     = (t.get("theme_slug") or "").strip()
            conviction     = t.get("conviction") or "emerging"
            company_count  = int(t.get("company_count") or 0)
            first_detected = t.get("first_detected")

            if not snaps:
                # Minimal score for themes with no snapshots in window
                scored.append(ThemeScore(
                    theme_id      = t["id"],
                    theme_name    = theme_name,
                    theme_slug    = theme_slug,
                    conviction    = conviction,
                    bottleneck    = _CONVICTION_SCORE.get(conviction, 0.25),
                    company_count = company_count,
                    first_detected= first_detected,
                    theme_cq      = 0.05,   # near-zero for no-evidence themes
                ))
                continue

            latest = snaps[-1]

            # ── 6 theme scoring factors ───────────────────────────────────────

            momentum = min(1.0, max(0.0,
                float(latest.get("momentum_score") or 0) / 100.0
            ))

            persistence = min(1.0, len(snaps) / max_quarters)

            days_behind = max(0, (window_latest - latest["snapshot_date"]).days)
            novelty     = max(0.0, 1.0 - days_behind / 365.0)
            tname_lower = theme_name.lower()
            if chain_kws and any(kw in tname_lower for kw in chain_kws):
                novelty = min(1.0, novelty + 0.20)

            if len(snaps) >= 2:
                m_last = float(snaps[-1].get("momentum_score") or 0)
                m_prev = float(snaps[-2].get("momentum_score") or 0)
                delta  = m_last - m_prev
                acceleration = min(1.0, max(0.0, (delta + 30) / 60.0))
            else:
                acceleration = 0.5

            window_sig   = theme_sig_cnt.get(t["id"], 0)
            all_time_sig = theme_sig_all.get(t["id"], 0) if theme_sig_all else 0
            if all_time_sig > 0 and max_sig_cnt > 0:
                ratio    = min(1.0, window_sig / all_time_sig)
                relative = min(1.0, window_sig / max_sig_cnt)
                signal_intensity = 0.5 * ratio + 0.5 * relative
            elif window_sig > 0:
                signal_intensity = min(1.0, window_sig / max_sig_cnt)
            else:
                signal_intensity = 0.0

            bottleneck = _CONVICTION_SCORE.get(conviction, 0.25)

            # ── ThemeCQ: constraint quality of the theme itself ───────────────
            # This is computed ONCE per theme and is the same value for every
            # company that appears in this theme.  Companies inherit it via
            # their role-distance-decayed edge.
            #
            # Formula: theme_cq = (0.40×bn_base + 0.30×scarcity + 0.20×persist + 0.10×evidence)
            #                     × breadth_penalty
            #
            # BreadthPenalty: max(0.25, min(1.0, 60/company_count))
            #   company_count=25  → penalty=1.0   (tight, specific)
            #   company_count=100 → penalty=0.60
            #   company_count=315 → penalty=0.19 → floor at 0.25
            #
            # Persistence via momentum×conviction (NOT age):
            #   declining-momentum confirmed theme ≠ persistent constraint.
            #   age alone doesn't make something structural.
            bn_base       = self._theme_bn_base(theme_name)
            raw_scarcity  = 1.0 - company_count / 60.0
            scarcity_cq   = max(0.05, raw_scarcity)
            breadth_penalty = max(0.25, min(1.0, 60.0 / max(1, company_count)))
            # momentum × conviction: only high-momentum confirmed themes persist
            persist_cq    = min(1.0, momentum * _CONVICTION_SCORE.get(conviction, 0.25))
            theme_cq = min(1.0, max(0.02, (
                0.40 * bn_base        +
                0.30 * scarcity_cq    +
                0.20 * persist_cq     +
                0.10 * signal_intensity
            ) * breadth_penalty))

            scored.append(ThemeScore(
                theme_id         = t["id"],
                theme_name       = theme_name,
                theme_slug       = theme_slug,
                conviction       = conviction,
                momentum         = round(momentum,         4),
                persistence      = round(persistence,      4),
                novelty          = round(novelty,          4),
                acceleration     = round(acceleration,     4),
                signal_intensity = round(signal_intensity, 4),
                bottleneck       = round(bottleneck,       4),
                company_count    = company_count,
                first_detected   = first_detected,
                theme_cq         = round(theme_cq,         4),
            ))

        return scored

    # ─────────────────────────────────────────────────────────────────────────
    # THEME KEYWORD SCORING
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _theme_bn_base(theme_name: str) -> float:
        """Keyword-based bottleneck base score for a theme name (0.25–0.90)."""
        tname = theme_name.lower()
        for keywords, score in _BN_TIERS:
            if any(kw in tname for kw in keywords):
                return score
        return _BN_DEFAULT

    @staticmethod
    def _macro_cluster(theme_name: str) -> str:
        """Return the macro cluster label for a theme (for deduplication)."""
        tname = theme_name.lower()
        for keywords, cluster in _MACRO_CLUSTERS:
            if any(kw in tname for kw in keywords):
                return cluster
        return _MACRO_DEFAULT_CLUSTER

    @staticmethod
    def _bottleneck_signal_ratio(sigs: list[dict]) -> float:
        """
        Fraction of signals that are genuine bottleneck evidence.
        Generic signals (hiring_freeze, partnership_formed) don't count.
        Returns 0.0–1.0.
        """
        if not sigs:
            return 0.0
        bn_count = sum(
            1 for s in sigs
            if s.get("signal_type") in _BOTTLENECK_SIGNALS
            and s.get("direction") not in ("negative", "decreasing")
        )
        return min(1.0, bn_count / max(1, len(sigs)))

    # ─────────────────────────────────────────────────────────────────────────
    # ROLE CONFIDENCE  (redesigned, wider spread 15–90 %)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _infer_role_v4(
        stored_role:  str,
        ticker_sigs:  list[dict],
        ent_meta:     dict,
        relevance:    float,
        signal_count: int,
        max_signals:  int,
        max_mention:  int,
    ) -> tuple[str, float]:
        normalized = ROLE_NORMALIZE.get(stored_role)

        def _pos(types):
            return sum(
                1 for s in ticker_sigs
                if s.get("signal_type") in types
                and s.get("direction") not in ("negative", "decreasing")
            )
        supply_n = _pos(_SUPPLY_SIG)
        bene_n   = _pos(_BENEFICIARY_SIG)
        direct_n = _pos(_DIRECT_SIG)
        neg_n    = sum(1 for s in ticker_sigs if s.get("signal_type") in _NEGATIVE_SIG)
        total_n  = supply_n + bene_n + direct_n

        if total_n > 0:
            if supply_n >= bene_n and supply_n >= direct_n:
                sig_role = "supply"
            elif bene_n >= direct_n:
                sig_role = "beneficiary"
            else:
                sig_role = "direct"
        else:
            sig_role = None

        role = normalized or sig_role or "beneficiary"

        evidence     = min(1.0, signal_count / max(1, max_signals * 0.6))
        mc           = max(1, int(ent_meta.get("mention_count") or 1))
        source_count = math.log(mc + 1) / math.log(max(2, max_mention) + 1)
        rel_norm     = min(1.0, relevance / 100.0)
        if normalized and sig_role:
            role_align = 1.0 if normalized == sig_role else 0.5
        elif normalized:
            role_align = 0.80
        else:
            role_align = 0.45
        graph_consistency = rel_norm * role_align

        raw_ner  = float(ent_meta.get("confidence") or 1.0)
        ner_conf = min(1.0, max(0.0, (raw_ner - 0.75) / 0.25))

        conf = (
            0.30 * evidence          +
            0.30 * source_count      +
            0.20 * graph_consistency +
            0.20 * ner_conf
        )
        if normalized:
            conf += 0.15
        conf -= min(0.15, neg_n * 0.03)

        return role, min(1.0, max(0.12, conf))

    @staticmethod
    def _conf_breakdown_v4(
        role: str, ticker_sigs: list[dict], ent_meta: dict,
        sig_count: int, max_signals: int, max_mention: int,
    ) -> dict[str, float]:
        evidence  = min(1.0, sig_count / max(1, max_signals * 0.6))
        mc        = max(1, int(ent_meta.get("mention_count") or 1))
        src_freq  = math.log(mc + 1) / math.log(max(2, max_mention) + 1)
        raw_ner   = float(ent_meta.get("confidence") or 1.0)
        ner_conf  = min(1.0, max(0.0, (raw_ner - 0.75) / 0.25))
        return {
            "Evidence":       round(evidence,  3),
            "Source Count":   round(src_freq,  3),
            "Graph Consist.": round(0.0,        3),
            "NER Conf":       round(ner_conf,  3),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # GRAPH DISTANCE  (proxy via rank_in_theme — updated thresholds v6)
    # rank 1–10=hop0, 11–25=hop1, 26–50=hop2, 51–100=hop3, >100=hop4
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _hop_distance(rank_in_theme: int) -> int:
        if rank_in_theme <= 10:   return 0   # direct
        if rank_in_theme <= 25:   return 1   # 1-hop
        if rank_in_theme <= 50:   return 2   # 2-hop
        if rank_in_theme <= 100:  return 3   # 3-hop
        return 4                             # peripheral

    # ─────────────────────────────────────────────────────────────────────────
    # EDGE SCORE  (relevance gate — NOT used for final ranking in v6)
    # Still used to filter out irrelevant graph noise (edge < EDGE_THRESHOLD).
    # Final ranking uses edge_cq = theme_cq × role_dist × signal_factor.
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _edge_score_v4(
        b: dict, role_conf: float, theme_rank: float,
        max_signals: int, hop: int,
    ) -> float:
        relevance    = min(1.0, float(b.get("relevance_score") or 0) / 100.0)
        sig_count    = int(b.get("signal_count") or 0)
        evidence     = min(1.0, sig_count / max(1, max_signals * 0.4))
        hop_decay    = 0.7 ** hop
        combined_rel = 0.6 * relevance + 0.4 * evidence
        theme_factor = 0.40 + 0.60 * theme_rank
        return min(1.0, combined_rel * role_conf * hop_decay * theme_factor)

    # ─────────────────────────────────────────────────────────────────────────
    # SUPPLIER QUALITY  (5-factor structural supply-chain positioning)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _supplier_quality_v4(
        role:          str,
        role_conf:     float,
        entries:       list[dict],
        best_relevance:float,
        best_sig_count:int,
        max_signals:   int,
        n_themes:      int,
        top_n:         int,
    ) -> tuple[float, dict[str, float]]:
        role_factor     = {"supply": 1.0, "direct": 0.65, "beneficiary": 0.35}
        role_specificity = role_conf * role_factor.get(role, 0.35)

        avg_conv      = (
            sum(_CONVICTION_SCORE.get(e["theme_ts"].conviction, 0.25) for e in entries)
            / len(entries)
        )
        breadth_bonus    = min(0.25, (len(entries) - 1) * 0.08)
        chain_centrality = min(1.0, avg_conv + breadth_bonus)

        evidence         = min(1.0, best_sig_count / max(1, max_signals * 0.5))
        best_rank        = min(e["rank_pos"] for e in entries)
        bottleneck_pos   = 1.0 / (1.0 + best_rank * 0.10)
        theme_hit_rate   = min(1.0, (n_themes / max(1, top_n)) * 2.0)

        sq = (
            0.25 * role_specificity  +
            0.20 * chain_centrality  +
            0.20 * evidence          +
            0.20 * bottleneck_pos    +
            0.15 * theme_hit_rate
        )
        breakdown = {
            "Role Specificity": round(role_specificity, 3),
            "Chain Centrality": round(chain_centrality, 3),
            "Evidence":         round(evidence,         3),
            "Bottleneck Pos":   round(bottleneck_pos,   3),
            "Theme Hit Rate":   round(theme_hit_rate,   3),
        }
        return min(1.0, max(0.05, sq)), breakdown

    # ─────────────────────────────────────────────────────────────────────────
    # SIGNAL HIGHLIGHTS  (bottleneck signals first, then by confidence)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _signal_highlights_v4(sigs: list[dict]) -> list[str]:
        """
        Return up to 4 signal labels.  Bottleneck-specific signals rank first
        (supply_bottleneck, capex_increase, inventory_drawdown, etc.).
        Generic signals (hiring_freeze, partnership_formed) appear last.
        """
        seen: set[str] = set()
        out:  list[str] = []

        def sort_key(s):
            is_bn  = 2.0 if s.get("signal_type") in _BOTTLENECK_SIGNALS else 0.0
            is_pos = 1.0 if s.get("direction") not in ("negative", "decreasing") else 0.0
            conf   = float(s.get("confidence") or 0)
            return is_bn + is_pos + conf

        for s in sorted(sigs, key=sort_key, reverse=True):
            st = s.get("signal_type", "")
            if st and st not in seen:
                out.append(st.replace("_", " ").title())
                seen.add(st)
            if len(out) >= 4:
                break
        return out

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _primary_role(roles: dict[str, float]) -> str:
        priority = {"supply": 3, "direct": 2, "beneficiary": 1}
        return max(roles, key=lambda r: priority.get(r, 0) * 0.40 + roles[r] * 0.60)
