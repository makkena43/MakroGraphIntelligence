#!/usr/bin/env python3
"""
Stock Selector Data Extractor — surfaces, for a given as-of date, what is
happening/emerging in the last 6-12 months across the MakroGraph DB:
major themes, major constraints (with evidence), supply-side beneficiaries,
and a ranked candidate-stock list with a light technical overlay.

Usage:
    python scripts/stock_report/select_stocks.py --as-of 2026-07-05 [--window-months 12]

Output:
    data/reports/stock_selector_<ASOF>_data.json  (path printed on stdout)
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_report_data import connect, q, jsonify, parse_as_of, REPORTS_DIR  # noqa: E402


# ── Lesson 6: backtest calibration — severe_loss_rate is the primary risk metric ─
# Hit rate alone is misleading: 2024 had 28% hit rate AND 36% severe-loss rate.
# Severe loss = pick lost > 30% over the 12-month measurement window.
# Source: India selector backtest 2020-2024 (Dec-31 selection, Dec-31 next year exit).
BACKTEST_CALIBRATION: list[dict] = [
    {"year": 2020, "market_return_pct": 31.0, "avg_alpha_pct": 113.1, "hit_rate_pct": 76, "severe_loss_rate_pct":  0, "severe_loss_n": 0,  "total_n": 21, "note": "Strong bull — any supply-chain play worked"},
    {"year": 2021, "market_return_pct":  3.0, "avg_alpha_pct":  -1.3, "hit_rate_pct": 43, "severe_loss_rate_pct": 22, "severe_loss_n": 5,  "total_n": 23, "note": "Flat market — 5 picks lost >30%; high-score names were worst (avg -45.9%)"},
    {"year": 2022, "market_return_pct": 23.8, "avg_alpha_pct":  16.4, "hit_rate_pct": 57, "severe_loss_rate_pct":  4, "severe_loss_n": 1,  "total_n": 23, "note": "Recovery bull — one governance blowup (VISHNU -76%)"},
    {"year": 2023, "market_return_pct": -0.3, "avg_alpha_pct":  42.8, "hit_rate_pct": 80, "severe_loss_rate_pct":  0, "severe_loss_n": 0,  "total_n": 25, "note": "Theme supercycle active (T&D/defence) — 0 severe losses despite flat market"},
    {"year": 2024, "market_return_pct":  1.7, "avg_alpha_pct":  -6.1, "hit_rate_pct": 28, "severe_loss_rate_pct": 36, "severe_loss_n": 9,  "total_n": 25, "note": "Worst year — 9/25 picks lost >30%; GENSOL -95% (fraud), SIEMENS -53%, SKFINDIA -59%"},
]
# Key finding: in 2024, ALL 9 severe losers had high fundamental scores (0.675-0.850).
# High conviction ≠ downside protection. risk_tier (Lesson 4) is the governance screen.

# ──────────────────────────────────────────────────────────────────────────
# Themes
# ──────────────────────────────────────────────────────────────────────────

def fetch_theme_landscape(cur, as_of, window_months, country="IN"):
    win_start = as_of - timedelta(days=window_months * 30)
    themes = q(cur, """
        SELECT id AS theme_id, theme_name, theme_slug, description, sectors, conviction,
               first_detected, stage, stage_label, stage_evidence, strength_score,
               momentum_score, company_count, doc_count, metadata,
               is_canonical, parent_theme_slug
        FROM mg_themes
        WHERE country=%s AND is_active
          AND first_detected <= %s
        ORDER BY strength_score DESC NULLS LAST
        LIMIT 400
    """, (country, as_of,))

    out = []
    for t in themes:
        meta = t.pop("metadata") or {}
        t["supply_constraint_count"] = meta.get("supply_constraint_count") or 0
        t["is_bottleneck"] = bool(meta.get("is_bottleneck"))
        t["tension_score"] = meta.get("tension_score")
        t["explosive_score"] = meta.get("explosive_score")
        t["confirmed_quarters"] = meta.get("confirmed_quarters")
        snaps = q(cur, """
            SELECT snapshot_date, strength_score, momentum_score, company_count
            FROM mg_theme_snapshots
            WHERE theme_id=%s AND snapshot_date <= %s
            ORDER BY snapshot_date DESC LIMIT 14
        """, (t["theme_id"], as_of))
        now_s = snaps[0]["strength_score"] if snaps else t["strength_score"]
        prev = [s for s in snaps if s["snapshot_date"] <= as_of - timedelta(days=182)]
        prev_s = prev[0]["strength_score"] if prev else None
        t["strength_now"] = round(now_s or 0, 1)
        t["strength_6mo_ago"] = round(prev_s, 1) if prev_s is not None else None
        t["strength_delta_6mo"] = round((now_s or 0) - prev_s, 1) if prev_s is not None else None
        t["last_snapshot_date"] = snaps[0]["snapshot_date"] if snaps else None

        # emergence via beneficiary growth (snapshots are often flat): how many
        # companies got newly linked to the theme inside the window vs before it
        growth = q(cur, """
            SELECT count(*) FILTER (WHERE first_seen_at >= %s AND first_seen_at <= %s) AS new_in_window,
                   count(*) FILTER (WHERE first_seen_at < %s)  AS before_window
            FROM mg_theme_beneficiaries
            WHERE theme_id=%s AND first_seen_at IS NOT NULL AND first_seen_at <= %s
        """, (win_start, as_of, win_start, t["theme_id"], as_of))[0]
        t["new_beneficiaries_in_window"] = growth["new_in_window"]
        t["beneficiaries_before_window"] = growth["before_window"]
        base = max(1, growth["before_window"])
        t["beneficiary_growth_ratio"] = round(growth["new_in_window"] / base, 2)

        t["is_emerging_window"] = bool(
            (t["first_detected"] and t["first_detected"] >= win_start)
            or (t["strength_delta_6mo"] is not None and t["strength_delta_6mo"] >= 10)
            or growth["new_in_window"] >= 8
            or (growth["new_in_window"] >= 4 and t["beneficiary_growth_ratio"] >= 0.4)
        )
        out.append(t)

    major = sorted(out, key=lambda x: -(x["strength_now"] or 0))[:15]
    emerging = sorted([t for t in out if t["is_emerging_window"]],
                      key=lambda x: -x["new_beneficiaries_in_window"])[:15]
    return major, emerging, out


# ──────────────────────────────────────────────────────────────────────────
# Constraints
# ──────────────────────────────────────────────────────────────────────────

def fetch_constraints(cur, as_of, window_months):
    win_start = as_of - timedelta(days=window_months * 30)
    # constrained products from the beneficiary mapper — the core "what is short"
    products = q(cur, """
        SELECT constrained_product, theme_name,
               count(DISTINCT company)                    AS n_companies,
               round(avg(conviction_score)::numeric, 3)   AS avg_conviction,
               round(max(conviction_score)::numeric, 3)   AS max_conviction,
               bool_or(has_order_book_signals)            AS any_order_book,
               bool_or(import_substitution_play)          AS any_import_sub,
               min(as_of_date) AS first_mapped, max(as_of_date) AS last_mapped
        FROM mg_india_beneficiaries
        WHERE constrained_product IS NOT NULL
          AND as_of_date <= %s AND as_of_date >= %s
        GROUP BY constrained_product, theme_name
        ORDER BY max(conviction_score) DESC NULLS LAST, count(DISTINCT company) DESC
        LIMIT 20
    """, (as_of, win_start))

    # Point-in-time on knowledge date: as_of_date gets bumped to the refresh date
    # by the updater, which would hide these structural rows from any scan dated
    # before the latest refresh. created_at is when the DB first knew the fact —
    # that is the honest cutoff. (Caveat: values may reflect a later refresh.)
    gaps = q(cur, """
        SELECT sector, component, gap, gap_pct, unit, supply_chain_stage,
               theme_name, severity, target_year, as_of_date
        FROM mg_capacity_gaps
        WHERE COALESCE(LEAST(as_of_date, created_at::date), created_at::date) <= %s
        ORDER BY gap_pct DESC NULLS LAST LIMIT 15
    """, (as_of,))

    imports = q(cur, """
        SELECT sector, component, import_share, import_value_bn_usd, primary_origin,
               substitute_possible, substitution_horizon_years, risk_level
        FROM mg_import_dependencies
        WHERE COALESCE(LEAST(as_of_date, created_at::date), created_at::date) <= %s
        ORDER BY import_share DESC NULLS LAST LIMIT 15
    """, (as_of,))
    return products, gaps, imports


# ──────────────────────────────────────────────────────────────────────────
# Supply-side beneficiaries
# ──────────────────────────────────────────────────────────────────────────

SUPPLY_SIDE_TYPES = ("direct_supplier", "critical_supplier", "input_supplier")

# ── Lesson 2: per-chain sector allowlist ─────────────────────────────────────
# A company's industry_nse must contain at least ONE of these substrings to be
# considered a plausible supply-side participant in the chain.  Key is a
# lowercase substring of the chain / constrained-product name.
# Chains not matched by any key are passed through unchecked (no rule = no filter).
CHAIN_SECTOR_ALLOWLIST: dict[str, list[str]] = {
    "battery":        ["automobile", "auto component", "capital goods", "power",
                       "electrical", "chemical", "diversified"],
    "defense":        ["capital goods", "defence", "defense", "aerospace",
                       "information technology", "electrical", "engineering", "diversified"],
    "optical fiber":  ["capital goods", "telecommunication", "electrical",
                       "information technology", "engineering", "diversified"],
    "transformer":    ["capital goods", "power", "electrical", "engineering",
                       "construction", "diversified"],
    "rolling stock":  ["capital goods", "construction", "engineering", "railway",
                       "electrical", "mechanical", "diversified"],
    "solar":          ["power", "capital goods", "electrical", "engineering",
                       "renewabl", "diversified"],
    "pcb":            ["capital goods", "information technology", "electrical",
                       "electronic", "diversified"],
    "ems":            ["capital goods", "information technology", "electrical",
                       "electronic", "diversified"],
    "crgo":           ["capital goods", "steel", "electrical", "engineering",
                       "metal", "diversified"],
    "cable":          ["capital goods", "electrical", "engineering",
                       "construction", "diversified"],
    "wire":           ["capital goods", "electrical", "engineering", "diversified"],
    "semiconductor":  ["capital goods", "information technology", "electrical",
                       "electronic", "chemical", "diversified"],
}

# ── Lesson 3: supply_chain_node values that mark a company as a consumer ─────
# Companies with these nodes are downstream operators, not supply-side capacity
# owners.  They receive a heavy conviction discount rather than exclusion so they
# remain visible in the per-chain lists but cannot surface in ranked_candidates.
DEMAND_CONSUMER_NODES: frozenset[str] = frozenset({
    "consumer", "operator", "end_user", "buyer", "off-taker",
    "offtaker", "end-user", "subscriber",
})

# ── Lesson 4: risk-flag filing types from mg_documents ───────────────────────
HIGH_RISK_FILING_TYPES: frozenset[str] = frozenset({
    "Fraud/Default/Arrest",
    "CIRP - Commencement",
    "Corporate Insolvency Resolution Process",
    "Liquidation",
    "Suspension of Trading",
})
ELEVATED_RISK_FILING_TYPES: frozenset[str] = frozenset({
    "Change in Auditors",
    "Resignation of Statutory Auditor",
    "Defaults on Payment of Interest/Principal",
    "One Time Settlement",
    "One time settlement",
    "Reasons for Delayed/Non-submission of Financial Results",
    # "Action(s) initiated or orders passed" removed — validation showed it is a
    # routine regulatory disclosure filed by virtually every listed company and
    # added noise without predictive value (ELEVATED outperformed NORMAL in 2025).
})

# ── Lesson 6: large-cap market proxy for regime detection ────────────────────
MARKET_PROXY_IN: list[str] = [
    "RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "NESTLEIND", "BRITANNIA", "TITAN",
    "CIPLA", "DRREDDY", "SUNPHARMA", "WIPRO", "ONGC", "NTPC", "POWERGRID",
    "COALINDIA", "HINDALCO", "TATACONSUM", "DIVISLAB", "GRASIM",
]

def _freshness_multiplier(min_confirmed_quarters: int) -> tuple[float, str]:
    """Lesson 1 + 5: fresher themes generated higher alpha in 2020-2024 backtest.

    Q4 (lowest fundamental score, freshest discovery) outperformed Q1 by +48pp alpha.
    Mature Consensus themes (16+ quarters) are typically already priced in.
    """
    if min_confirmed_quarters == 0:
        return 1.10, "new_theme"        # just detected, not yet confirmed
    if min_confirmed_quarters <= 4:
        return 1.12, "fresh"            # early Accelerating — highest alpha potential
    if min_confirmed_quarters <= 8:
        return 1.05, "developing"       # mid-Accelerating
    if min_confirmed_quarters <= 15:
        return 1.00, "established"
    return 0.93, "consensus"            # 16+ quarters = priced in, marginal buyers shrinking


def _get_chain_key(chain_name: str) -> str | None:
    """Return the CHAIN_SECTOR_ALLOWLIST key that matches chain_name, or None."""
    name_lower = (chain_name or "").lower()
    for key in CHAIN_SECTOR_ALLOWLIST:
        if key in name_lower:
            return key
    return None


def _sector_check(industry_nse: str | None, chain_name: str) -> bool | None:
    """True = compatible, False = mismatch, None = no rule / unknown."""
    if not industry_nse:
        return None
    key = _get_chain_key(chain_name)
    if not key:
        return None
    ind_lower = industry_nse.lower()
    return any(a in ind_lower for a in CHAIN_SECTOR_ALLOWLIST[key])


def fetch_supply_beneficiaries(cur, as_of, window_months, per_product=8):
    win_start = as_of - timedelta(days=window_months * 30)
    rows = q(cur, """
        SELECT DISTINCT ON (constrained_product, company)
               company, ticker, theme_name, constrained_product, supply_chain_node,
               supply_chain_stage, beneficiary_type, conviction_score, rationale,
               signal_count, has_order_book_signals, import_substitution_play, as_of_date
        FROM mg_india_beneficiaries
        WHERE beneficiary_type = ANY(%s)
          AND as_of_date <= %s AND as_of_date >= %s
        ORDER BY constrained_product, company, as_of_date DESC
    """, (list(SUPPLY_SIDE_TYPES), as_of, win_start))

    # Lesson 2: batch-fetch classification for all tickers. NSE macro sector when
    # present; BSE 4-level taxonomy (enriched Jul-2026 from BSE public API) as
    # fallback AND as the granular sub-industry string for pure-play detection.
    tickers = list({r["ticker"] for r in rows if r.get("ticker")})
    if tickers:
        sm = q(cur, """SELECT nse_symbol, industry_nse, industry_bse, sector_bse
                       FROM security_master WHERE nse_symbol = ANY(%s)""", (tickers,))
        industry_map = {r["nse_symbol"]: r for r in sm}
    else:
        industry_map = {}

    by_product = defaultdict(list)
    for r in rows:
        chain = r["constrained_product"] or r["theme_name"]
        smrow = industry_map.get(r.get("ticker") or "") or {}
        industry = smrow.get("industry_nse") or smrow.get("industry_bse")
        r["industry_nse"] = smrow.get("industry_nse")
        r["industry_detail"] = smrow.get("industry_bse")   # e.g. "Electrical Equipment | Heavy Electrical Equipment"
        r["sector"] = smrow.get("sector_bse")

        # Lesson 2: sector allowlist check — combined NSE+BSE text for coverage
        combined = " / ".join(x for x in (smrow.get("industry_nse"), smrow.get("industry_bse")) if x)
        compatible = _sector_check(combined or None, chain)
        r["sector_mismatch"] = compatible is False  # True → known-incompatible sector

        # Capex-expansion evidence: the mapper embeds it in rationale text
        m = re.search(r"(\d+)\s+capex_increase signals", r.get("rationale") or "")
        r["capex_signals"] = int(m.group(1)) if m else 0

        # Lesson 3: demand-side consumer node check
        node = (r.get("supply_chain_node") or "").lower().strip()
        r["demand_side_flag"] = node in DEMAND_CONSUMER_NODES

        by_product[chain].append(r)

    result = {}
    for prod, lst in by_product.items():
        lst.sort(key=lambda r: (
            r["sector_mismatch"],           # mismatches sorted to bottom of per-chain list
            r["demand_side_flag"],          # demand-side consumers next
            -(r["conviction_score"] or 0),
            not r["has_order_book_signals"],
        ))
        result[prod] = lst[:per_product]
    return result


# ──────────────────────────────────────────────────────────────────────────
# Improvements 1-5: evidence dashboard, cross-theme overlap, scoring
# breakdown, visual analytics data, bear-case analysis
# ──────────────────────────────────────────────────────────────────────────

def fetch_policy_event_counts(cur, themes, as_of, country):
    """Single DB query → Python-side count of policy events per theme keyword."""
    rows = q(cur, """
        SELECT lower(title) AS title FROM mg_policy_events
        WHERE country=%s AND COALESCE(introduced_date, enacted_date, fetched_at::date) <= %s
    """, (country, as_of))
    titles = [r["title"] for r in rows]
    counts = {}
    for t in themes:
        raw = t["theme_name"].split(":")[0].split("←")[0].strip().lower()
        words = [w for w in raw.split() if len(w) >= 4][:2]
        counts[t["theme_id"]] = sum(1 for title in titles if any(w in title for w in words))
    return counts


def _evidence_trend(t):
    """Is evidence for this theme accelerating, maturing, flat, or shrinking?

    Uses beneficiary GROWTH RATE (new/before) as the primary signal — more
    meaningful than the absolute strength delta, which is large for all themes.
    """
    new_cos = t.get("new_beneficiaries_in_window", 0)
    before = max(1, t.get("beneficiaries_before_window", 1))
    growth_ratio = new_cos / before
    stage = t.get("stage_label", "")
    delta = t.get("strength_delta_6mo")

    # Genuine new themes (born recently) with strong inflow
    if before == 1 and new_cos >= 10:
        return "↑ Accelerating"
    # Established themes with meaningful new additions (>20% growth)
    if growth_ratio > 0.20:
        return "↑ Accelerating" if stage == "Accelerating" else "↑ Growing"
    # Slowing crowd adoption on mature themes (2-20% new)
    if growth_ratio > 0.02:
        return "→ Maturing"
    # Fully saturated or slightly shrinking
    if delta is not None and delta < -5:
        return "↓ Fading"
    return "→ Saturated"


def build_evidence_dashboard(themes, policy_counts):
    """Improvement 1 — per-theme evidence record with trend indicator."""
    out = []
    for t in themes:
        sc = t.get("supply_constraint_count") or 0
        confirmed = t.get("confirmed_quarters") or 0
        confidence = min(100, round(
            (min(sc, 50) / 50.0) * 40
            + (min(confirmed, 10) / 10.0) * 30
            + (min(t.get("strength_now") or 0, 100) / 100.0) * 30
        ))
        out.append({
            "theme_name": t["theme_name"],
            "theme_id": t["theme_id"],
            "stage": t["stage_label"],
            "first_detected": t["first_detected"],
            "companies_mapped": t.get("company_count") or 0,
            "filings_covered": t.get("doc_count") or 0,
            "bottleneck_signals": sc,
            "confirmed_quarters": confirmed,
            "policy_events": policy_counts.get(t["theme_id"], 0),
            "strength_score": t.get("strength_now") or 0,
            "strength_delta_6mo": t.get("strength_delta_6mo"),
            "new_beneficiaries_in_window": t.get("new_beneficiaries_in_window", 0),
            "conviction_label": t.get("conviction") or "—",
            "evidence_confidence_pct": confidence,
            "evidence_trend": _evidence_trend(t),          # NEW
        })
    return sorted(out, key=lambda x: -x["evidence_confidence_pct"])


_SS_TYPE_SCORE = {
    "direct_supplier": 40, "critical_supplier": 35,
    "input_supplier": 25, "ecosystem_participant": 10,
}


def _supply_side_confidence(best_rank, capex_signals, n_chains, max_conv_or_rel,
                             order_book=False, best_type_score=None):
    """Supply-side confidence 0-100: how likely is this company actually a capacity owner?

    Components:
      Rank quality   (0-40): rank 1 in any chain = graph found it as top supply-side match
      Capex signals  (0-15): company investing to expand capacity = supply-side behaviour
      Chain depth    (0-20): independent chains finding the same name = corroboration
      Conviction/rel (0-15): filing relevance weight
      Order-book     (0-10): hard order-book evidence (IN only)
    """
    rank_pts = 40 if best_rank <= 1 else 30 if best_rank <= 3 else 15 if best_rank <= 5 else 5
    capex_pts = min(15, int(capex_signals or 0) * 4)
    chain_pts = min(20, int(n_chains or 1) * 3)
    conv_pts = min(15, round(float(max_conv_or_rel or 0) / 100.0 * 15))
    ob_pts = 10 if order_book else 0
    type_pts = min(best_type_score or 0, 0)   # bonus only for IN (passed through)
    return min(100, rank_pts + capex_pts + chain_pts + conv_pts + ob_pts)


# Stocks excluded regardless of score (enforcement actions, fraud)
MANUAL_EXCLUDE = {
    "GENSOL",   # SEBI enforcement action Apr-2025; stock suspended/fraud
}


def compute_cross_theme_overlap(supply_by_chain):
    """Improvement 2 — companies appearing across 2+ independent constraint chains,
    with a supply-side confidence score on each."""
    ticker_chains = defaultdict(list)
    ticker_meta = {}   # ticker → best capex/rank across chains
    for chain_name, companies in supply_by_chain.items():
        for c in companies:
            tick = (c.get("ticker") or "").strip().upper()
            if not tick or tick in MANUAL_EXCLUDE:
                continue
            conv = float(c.get("conviction_score") or c.get("relevance_score") or 0)
            rank = c.get("rank_in_theme") or 99
            ticker_chains[tick].append({
                "chain": chain_name,
                "rank": rank,
                "conviction": conv,
            })
            m = ticker_meta.setdefault(tick, {"best_rank": 999, "max_conv": 0,
                                               "total_capex": 0, "order_book": False,
                                               "best_type_score": 0})
            m["best_rank"] = min(m["best_rank"], rank)
            m["max_conv"] = max(m["max_conv"], conv)
            m["total_capex"] += int(c.get("capex_signals") or 0)
            m["order_book"] = m["order_book"] or bool(c.get("has_order_book_signals"))
            ts = _SS_TYPE_SCORE.get(c.get("beneficiary_type") or "", 0)
            m["best_type_score"] = max(m["best_type_score"], ts)

    out = []
    for ticker, chains in ticker_chains.items():
        if len(chains) < 2:
            continue
        m = ticker_meta[ticker]
        n = len(chains)
        ss_conf = _supply_side_confidence(m["best_rank"], m["total_capex"], n,
                                           m["max_conv"], m["order_book"])
        overlap_score = round(n * m["max_conv"], 3)
        out.append({
            "ticker": ticker,
            "n_independent_chains": n,
            "overlap_score": overlap_score,
            "best_rank_across_chains": m["best_rank"],
            "supply_side_confidence_pct": ss_conf,   # NEW
            "chains": sorted(chains, key=lambda x: (x.get("rank") or 99, -x["conviction"])),
            "read": (
                "multi-chain conviction" if n >= 4 else
                "cross-chain corroboration"
            ),
        })
    return sorted(out, key=lambda x: (-x["n_independent_chains"], -x["overlap_score"]))[:25]


def generate_bear_cases(major_themes, emerging_themes, candidates, country):
    """Improvement 5 — heuristic bear-case flags per major theme and top candidates."""
    bears = []
    all_themes = major_themes[:8] + [t for t in emerging_themes if t not in major_themes][:4]
    for t in all_themes:
        risks = []
        stage = t.get("stage_label", "")
        confirmed = t.get("confirmed_quarters") or 0
        sc = t.get("supply_constraint_count") or 0
        cos = t.get("company_count") or 0
        delta = t.get("strength_delta_6mo")
        name_lower = t["theme_name"].lower()
        born = t.get("first_detected")

        if stage == "Consensus" and confirmed >= 8:
            risks.append(f"Fully priced: {confirmed} confirmed quarters at Consensus — marginal buyers shrinking, re-rating requires new catalyst")
        if sc < 5 and cos > 20:
            risks.append(f"Breadth without depth: only {sc} hard bottleneck signals across {cos} companies — could be narrative consensus, not physical shortage")
        if delta is not None and -1.0 < delta < 1.0:
            risks.append("Strength score flat for 6 months — theme may have peaked; watch for a re-ignition catalyst before adding")
        if born and born.year == 2024 and stage == "Consensus":
            risks.append("Born-to-crowded in <24 months — the investing window compressed; late entrants face full-priced assets")
        if "energy" in name_lower or "power" in name_lower or "utility" in name_lower or "grid" in name_lower:
            risks.append("Rate sensitivity: utilities are bond proxies — if long rates stay elevated, multiple compression offsets load-growth thesis")
            risks.append("AI efficiency risk: if model training/inference efficiency improves faster than expected (Jevons paradox), power-demand projections overstated")
            risks.append("Interconnection queue: new grid capacity can take 7-12 years to energize; near-term EPS may disappoint vs long thesis")
        if "chip" in name_lower or "semiconductor" in name_lower or "silicon" in name_lower:
            risks.append("Export-control two-sided: China-revenue loss (AMAT/LRCX) can outweigh domestic-capacity benefit in the near term")
            risks.append("AI-capex reversal: if hyperscaler ROI on AI disappoints in 2026, the chip pull-forward cycle reverses sharply")
        if "memory" in name_lower or "nand" in name_lower or "dram" in name_lower or "hbm" in name_lower:
            risks.append("ITC NAND/DRAM investigation (30-Mar-2026): respondent outcome could create uncertainty for domestic memory names")
            risks.append("Memory is cyclical: HBM pricing premium can compress faster than the theme cycle suggests")
        if ("generative" in name_lower or "genai" in name_lower) and stage == "Consensus":
            risks.append("GenAI monetisation risk: if enterprise ROI on GenAI tools disappoints, the demand signal that created this theme deflates")
        if not risks:
            risks.append("No dominant risk pattern detected from available heuristics — apply sector-specific diligence")
        bears.append({
            "theme": t["theme_name"],
            "stage": stage,
            "risk_count": len(risks),
            "risks": risks,
        })
    return bears


def _scoring_breakdown_in(conviction, breadth, order_book, import_sub,
                          freshness_multiplier=1.00, freshness_label="established",
                          technical_flag=None):
    """Transparent IN scoring formula.

    Lesson 1+5: composite = fundamental × freshness_multiplier (theme age discount).
    Fresh themes (1-4 quarters) get ×1.12; Consensus themes (16+) get ×0.93.
    Lesson 2: 200DMA is position-sizing guidance only — NOT a composite input.
              technical_flag is stored for display; it does not move the score.
    Ranked by composite_score (freshness-adjusted); fundamental is tiebreaker.
    """
    components = {
        "conviction_x0.45": round(0.45 * conviction, 3),
        "breadth_x0.25":    round(0.25 * breadth, 3),
        "order_book_x0.20": 0.20 if order_book else 0.0,
        "import_sub_x0.10": 0.10 if import_sub else 0.0,
    }
    fundamental = round(sum(components.values()), 3)
    components["fundamental_score"] = fundamental
    components["freshness_multiplier"] = freshness_multiplier
    components["freshness_label"] = freshness_label
    components["composite_score"] = round(fundamental * freshness_multiplier, 3)
    if technical_flag:
        components["technical_flag"] = technical_flag
    components["formula"] = (
        "fundamental = 0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub; "
        "composite = fundamental × freshness_multiplier "
        "(new_theme=×1.10 | fresh 1-4q=×1.12 | developing 5-8q=×1.05 | established 9-15q=×1.00 | consensus 16+q=×0.93). "
        "Technical state (200DMA) = position-sizing guidance only — not a composite input. "
        "Ranked by composite_score; fundamental_score is tiebreaker."
    )
    return components


def _scoring_breakdown_us(relevance, breadth, rank_score):
    """Improvement 3 — transparent US scoring formula."""
    components = {
        "relevance_x0.40": round(0.4 * min(relevance / 100.0, 1.0), 3),
        "theme_breadth_x0.30": round(0.3 * breadth, 3),
        "theme_rank_x0.30": round(0.3 * rank_score, 3),
    }
    components["composite_score"] = round(sum(components.values()), 3)
    components["formula"] = "0.40×(relevance/100) + 0.30×(n_themes/5) + 0.30×(1 - best_rank/30)"
    return components


def fetch_us_beneficiaries(cur, constraint_themes, as_of, win_start, per_theme=8):
    """US supply-side proxies: top-ranked beneficiaries of bottleneck/constraint
    themes from the theme graph (no supply-chain-node classification for US)."""
    result = {}
    for t in constraint_themes:
        rows = q(cur, """
            SELECT b.ticker, b.company_name AS company, b.beneficiary_type,
                   b.company_role, b.relevance_score, b.rank_in_theme,
                   b.signal_count, b.capex_signals, b.first_seen_at,
                   left(b.reasoning, 200) AS rationale
            FROM mg_theme_beneficiaries b
            WHERE b.theme_id=%s AND b.ticker IS NOT NULL
              AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
            ORDER BY b.rank_in_theme ASC NULLS LAST, b.relevance_score DESC
            LIMIT %s
        """, (t["theme_id"], as_of, per_theme))
        if rows:
            result[t["theme_name"]] = rows
    return result


def rank_us_candidates(themes_by_name, top_n=25):
    agg = {}
    for theme, lst in themes_by_name.items():
        for r in lst:
            tick = (r["ticker"] or "").strip().upper()
            if not tick:
                continue
            a = agg.setdefault(tick, {"ticker": tick, "company": r["company"],
                                      "themes": set(), "best_rank": 999,
                                      "max_relevance": 0.0, "capex_signals": 0,
                                      "signal_count": 0})
            a["themes"].add(theme)
            a["best_rank"] = min(a["best_rank"], r["rank_in_theme"] or 999)
            a["max_relevance"] = max(a["max_relevance"], float(r["relevance_score"] or 0))
            a["capex_signals"] += int(r["capex_signals"] or 0)
            a["signal_count"] += int(r["signal_count"] or 0)
    out = list(agg.values())
    for a in out:
        a["themes"] = sorted(a["themes"])[:6]
        breadth = min(len(a["themes"]), 5) / 5.0
        rank_score = max(0.0, 1.0 - (a["best_rank"] / 30.0)) if a["best_rank"] < 999 else 0
        breakdown = _scoring_breakdown_us(a["max_relevance"], breadth, rank_score)
        a["fundamental_score"] = breakdown["composite_score"]
        a["composite_score"] = breakdown["composite_score"]
        a["scoring_breakdown"] = breakdown
        # Supply-side confidence: how likely is this a genuine capacity owner?
        a["supply_side_confidence_pct"] = _supply_side_confidence(
            a["best_rank"], a["capex_signals"], len(a["themes"]),
            a["max_relevance"])
        a["technical"] = None
    out.sort(key=lambda a: -a["composite_score"])
    return out[:top_n]


# ──────────────────────────────────────────────────────────────────────────
# Candidate ranking + technical overlay
# ──────────────────────────────────────────────────────────────────────────

def rank_candidates(cur, supply_by_product, as_of, top_n=25, theme_quarters=None):
    """Build the ranked candidate list with backtest lessons applied.

    Lesson 1+5: composite = fundamental × freshness_multiplier.
                Fresh themes (1-4 confirmed quarters) get ×1.12 bonus;
                Consensus themes (16+) get ×0.93 discount.
                Ranked by composite_score (freshness-adjusted).
    Lesson 2:   200DMA → position_size_guidance (full/half), NOT a score input.
                Removed ×1.10 bonus for above-200DMA — backtest showed above-200DMA
                stocks had LOWER avg alpha (+29.8%) than below-200DMA (+35.2%).
    Lesson 4:   Risk overlay via fetch_risk_flags — HIGH/ELEVATED/NORMAL tier
                from mg_documents. High-risk candidates flagged but not auto-excluded
                (user decides; the flag prevents silent inclusion).
    Sector filter (prior): sector_mismatch=True excluded from ranked candidates.
    Demand discount (prior): demand_side_flag=True → ×0.3 conviction.
    """
    theme_quarters = theme_quarters or {}

    agg = {}
    for prod, lst in supply_by_product.items():
        for r in lst:
            tick = (r["ticker"] or "").strip().upper()
            if not tick:
                continue
            if tick in MANUAL_EXCLUDE:
                continue
            if r.get("sector_mismatch"):
                continue
            effective_conviction = float(r["conviction_score"] or 0)
            if r.get("demand_side_flag"):
                effective_conviction *= 0.3

            a = agg.setdefault(tick, {
                "ticker": tick, "company": r["company"], "products": set(),
                "themes": set(), "max_conviction": 0.0, "order_book": False,
                "import_sub": False, "types": set(),
                "best_rank": 999, "total_capex": 0, "best_type_score": 0,
                "any_demand_side": False, "_theme_set": set(),
                "industry": r.get("industry_nse") or r.get("industry_detail"),
                "industry_detail": r.get("industry_detail"),
                "sector": r.get("sector"),
                "capex_signals": 0,
            })
            a["capex_signals"] = max(a["capex_signals"], int(r.get("capex_signals") or 0))
            a["products"].add(prod)
            a["themes"].add(r["theme_name"])
            a["_theme_set"].add(r["theme_name"])
            a["max_conviction"] = max(a["max_conviction"], effective_conviction)
            a["order_book"] = a["order_book"] or bool(r["has_order_book_signals"])
            a["import_sub"] = a["import_sub"] or bool(r["import_substitution_play"])
            a["types"].add(r["beneficiary_type"])
            a["best_rank"] = min(a["best_rank"], r.get("supply_chain_stage") or 99)
            a["total_capex"] += int(r.get("signal_count") or 0)
            a["best_type_score"] = max(a["best_type_score"],
                                       _SS_TYPE_SCORE.get(r.get("beneficiary_type") or "", 0))
            if r.get("demand_side_flag"):
                a["any_demand_side"] = True

    candidates = list(agg.values())

    # Lesson 5: min confirmed_quarters per candidate (across its themes)
    for a in candidates:
        qtrs = [theme_quarters.get(tn, 20) for tn in a["_theme_set"] if tn]
        a["min_confirmed_quarters"] = min(qtrs) if qtrs else 20

    for a in candidates:
        a["products"] = sorted(a["products"])[:6]
        a["themes"] = sorted(a["themes"])[:6]
        a["types"] = sorted(a["types"])
        del a["_theme_set"]
        breadth = min(len(a["products"]), 5) / 5.0
        # Lesson 1+5: freshness multiplier applied here so pre-sort uses it
        fm, fl = _freshness_multiplier(a["min_confirmed_quarters"])
        breakdown = _scoring_breakdown_in(a["max_conviction"], breadth,
                                          a["order_book"], a["import_sub"],
                                          freshness_multiplier=fm, freshness_label=fl)
        a["fundamental_score"] = breakdown["fundamental_score"]
        a["composite_score"] = breakdown["composite_score"]
        a["scoring_breakdown"] = breakdown
        a["supply_side_confidence_pct"] = min(100,
            _supply_side_confidence(a["best_rank"], a["total_capex"],
                                    len(a["products"]), a["max_conviction"] * 100,
                                    a["order_book"])
            + a["best_type_score"])

    # Pre-sort by composite (freshness-adjusted) before technical overlay
    candidates.sort(key=lambda a: (-a["composite_score"], -a["fundamental_score"]))
    candidates = candidates[:top_n * 2]

    # Technical overlay — bhavcopy (as-of safe)
    tickers = [a["ticker"] for a in candidates]
    px = q(cur, """
        SELECT symbol, trade_date, close, high
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series IN ('EQ','BE')
          AND trade_date BETWEEN %s AND %s
        ORDER BY symbol, trade_date
    """, (tickers, as_of - timedelta(days=420), as_of))
    price_series = defaultdict(list)
    for r in px:
        price_series[r["symbol"]].append(r)

    for a in candidates:
        fm, fl = _freshness_multiplier(a["min_confirmed_quarters"])
        s = price_series.get(a["ticker"])
        if not s or len(s) < 40:
            a["technical"] = None
            a["position_size_guidance"] = "verify_chart"
            # Keep composite from pre-sort pass
            a["scoring_breakdown"] = _scoring_breakdown_in(
                a["max_conviction"], min(len(a["products"]), 5) / 5.0,
                a["order_book"], a["import_sub"],
                freshness_multiplier=fm, freshness_label=fl,
                technical_flag="no_price_data")
            a["composite_score"] = a["scoring_breakdown"]["composite_score"]
            continue

        closes = [float(r["close"]) for r in s if r["close"] is not None]
        highs  = [float(r["high"])  for r in s if r["high"]  is not None]
        last   = closes[-1]
        hi52   = max(highs[-250:]) if highs else None
        ma200  = sum(closes[-200:]) / min(200, len(closes))
        base   = closes[-126] if len(closes) >= 126 else closes[0]
        pct_from_high = round((last - hi52) / hi52 * 100, 1) if hi52 else None
        above_200 = last > ma200
        young_chain = a.get("min_confirmed_quarters", 20) < 8

        a["technical"] = {
            "last_close":        round(last, 2),
            "pct_from_52w_high": pct_from_high,
            "above_200dma":      above_200,
            "ret_6m_pct":        round((last - base) / base * 100, 1) if base else None,
        }

        # Lesson 2: 200DMA → position sizing guidance, NOT a score multiplier.
        # Backtest showed above-200DMA had higher hit rate (62%) but LOWER avg alpha
        # (+29.8%) vs below-200DMA (+35.2%). The bonus was removing the high-upside names.
        good_setup = above_200 and pct_from_high is not None and pct_from_high > -25
        if good_setup:
            tech_flag = "constructive"
            position_guidance = "full_position"
        elif not above_200 and young_chain:
            tech_flag = "base_building_early_phase"
            position_guidance = "half_position_scale_on_breakout"
        elif not above_200:
            tech_flag = "base_building"
            position_guidance = "half_position_scale_on_breakout"
        else:
            tech_flag = "extended_above_high"
            position_guidance = "verify_extension_before_entry"

        a["position_size_guidance"] = position_guidance
        a["scoring_breakdown"] = _scoring_breakdown_in(
            a["max_conviction"], min(len(a["products"]), 5) / 5.0,
            a["order_book"], a["import_sub"],
            freshness_multiplier=fm, freshness_label=fl,
            technical_flag=tech_flag)
        a["composite_score"] = a["scoring_breakdown"]["composite_score"]

    # Lesson 4: batch-fetch risk flags from mg_documents
    risk_flags = fetch_risk_flags(cur, tickers, as_of)
    for a in candidates:
        rf = risk_flags.get(a["ticker"], {"risk_tier": "NORMAL", "risk_events": []})
        a["risk_tier"] = rf["risk_tier"]
        a["risk_events"] = rf["risk_events"][:5]   # cap to 5 most recent

    # India policy overlay (PLI/ALMM/BCD/...): evidence for the judgment layer, never scored
    policy_ev = fetch_policy_evidence(cur, tickers, as_of)
    for a in candidates:
        a["policy_evidence"] = policy_ev.get(a["ticker"], {"schemes": {}, "n_policy_filings": 0})

    # Final sort: composite (freshness-adjusted) primary, fundamental tiebreaker
    candidates.sort(key=lambda a: (-a.get("composite_score", 0), -a["fundamental_score"]))

    # Lesson 1+3: Tier 1 (top 5 actionable); T3 split into Watch vs Ignore.
    # Multi-year backtest: T3 outliers (POWERINDIA, STLTECH) consistently had
    # fresh/new_theme freshness and landed at ranks 6-8 before graduating to T1
    # the following year. Consensus/established T3 names never graduated and had
    # higher severe-loss rates. Two sub-tiers remove ambiguity for multi-year holds:
    #   Tier3_Watch  — fresh/new_theme, rank 6-8, NORMAL risk → add if moves to T1 next scan
    #   Tier3_Ignore — established/consensus OR rank 9+ OR ELEVATED/HIGH risk → drop
    result = candidates[:top_n]
    for i, a in enumerate(result):
        if i < 5:
            a["discovery_tier"] = "Tier1_HighConviction"
        elif (i <= 7
              and a["scoring_breakdown"].get("freshness_label") in ("fresh", "new_theme")
              and a["risk_tier"] == "NORMAL"):
            a["discovery_tier"] = "Tier3_Watch"
        else:
            a["discovery_tier"] = "Tier3_Ignore"
    return result


def compute_chain_coverage(supply_by_product, major_themes, emerging_themes):
    """Lesson 4 — per-chain coverage density: how complete is the beneficiary extraction?

    Ratio = supply-side companies mapped in this chain /
            total companies mapped to the matched theme.
    When < 5% the selector output is showing a slice of the real universe;
    a coverage_warning flag is set so the briefing can caveat accordingly.
    """
    # Build a name→company_count lookup from all themes
    theme_cos: dict[str, int] = {}
    for t in major_themes + emerging_themes:
        tn = (t.get("theme_name") or "").lower()
        cos = t.get("company_count") or 0
        if tn and cos:
            theme_cos[tn] = max(theme_cos.get(tn, 0), cos)

    coverage: dict[str, dict] = {}
    for chain, companies in supply_by_product.items():
        n_supply = len([c for c in companies
                        if not c.get("sector_mismatch") and not c.get("demand_side_flag")])
        # fuzzy-match chain name against theme names
        chain_lower = chain.lower()
        matched_cos = 0
        for tn, cos in theme_cos.items():
            if any(word in tn for word in chain_lower.split()[:3]) or any(word in chain_lower for word in tn.split()[:3]):
                matched_cos = max(matched_cos, cos)
        density = round(n_supply / matched_cos * 100, 1) if matched_cos else None
        coverage[chain] = {
            "n_supply_mapped": n_supply,
            "theme_company_count": matched_cos or None,
            "coverage_density_pct": density,
            "coverage_warning": (density is not None and density < 5),
        }
    return coverage


# ── Layer types: how a chain monetizes scarcity (drives entry logic) ─────────
# differentiated: qualification barriers / programmatic demand — capex-UP is the
#   entry signal; positions are multi-year compounders (CRGO, transformers).
# commodity: process capacity anyone can add — constraint forms via industry
#   capex CUTS + demand inflection; positions are TRADES with exit triggers
#   (solar modules 2023: +579% then −95%; memory cycle).
# service_manufacturing / epc: revenue grows with the theme but margins are
#   bid away — scarcity rent accrues elsewhere.
CHAIN_LAYER_TYPE: dict[str, str] = {
    "crgo steel":          "differentiated",
    "power transformer":   "differentiated",
    "defense electronics": "differentiated",
    "semiconductor ic":    "differentiated",
    "battery cell":        "commodity",
    "solar cell":          "commodity",
    "solar module":        "commodity",
    "solar wafer":         "commodity",
    "optical fiber":       "commodity",
    "ems / contract":      "service_manufacturing",
    "pcb / printed":       "service_manufacturing",
    "rolling stock":       "epc_cyclical",
}


def chain_layer_type(chain_name: str) -> str:
    key = (chain_name or "").strip().lower()
    for k, v in CHAIN_LAYER_TYPE.items():
        if k in key:
            return v
    return "unclassified"


def compute_policy_beneficiary_screen(cur, as_of, min_docs: int = 4, top_n: int = 15) -> dict:
    """POLICY-FIRST discovery engine (independent of theme chains).

    The Dixon/PGEL/Amber lesson: PLI-driven explosions announce themselves in
    beneficiaries' OWN filings years early (PGEL first PLI mention Dec-2020,
    Amber Aug-2020, Dixon Apr-2021) — but the theme-chain mapper never needs to
    see them. Backtest of this exact screen at Dec-2022: avg +87% over 2 yrs
    (AMBER +293%, MINDACORP +139%, BOSCH +99%).

    For each India scheme: tickers whose own filings mention it >= min_docs
    times (point-in-time <= as_of), with recency trend (12mo vs prior 12mo) —
    rising intensity = the scheme is becoming the story. Purely a discovery
    list for the judgment layer: winner-vs-claimant check and layer-type
    archetype still apply (scheme executors are B-archetype; a claimant with
    mentions but no order conversion is the GREAVESCOT trap).
    """
    # Commitment-stage detector (single-doc precision, no intensity needed):
    # "apply/approved/selected under PLI" phrasing in a company's own disclosure.
    # Cuts detection latency from ~12 months (mention accumulation) to the day
    # of the disclosure: AARTIDRUGS Jun-2020, DIXON Apr-2021, the white-goods
    # allotment class Oct-Nov-2021 — all fired on one filing each.
    COMMIT_PATTERN = (r"(appl(y|ication|ied)|bid) (for|under) [^.]{0,60}(\yPLI\y|Production Linked)"
                      r"|(approv|select|sanction)\w* under [^.]{0,40}(\yPLI\y|Production Linked)"
                      r"|(\yPLI\y|Production Linked Incentive)( Scheme)?[^.]{0,60}(applicat|approv|select|sanction|allot)")
    commits = {}
    for r in q(cur, """
        SELECT d.ticker, MIN(d.filed_at)::date AS first_commit, COUNT(*) AS n
        FROM mg_documents d
        WHERE d.country = 'IN' AND d.ticker IS NOT NULL AND d.ticker <> ''
          AND d.filed_at <= %s AND d.raw_text ~* %s
        GROUP BY d.ticker
    """, (as_of, COMMIT_PATTERN)):
        commits[(r["ticker"] or "").strip().upper()] = {
            "first_commit": r["first_commit"], "n_commit_docs": int(r["n"])}

    out: dict[str, list] = {}
    early: dict[str, list] = {}
    for scheme, pattern in INDIA_POLICY_SCHEMES.items():
        rows = q(cur, """
            SELECT d.ticker,
                   COUNT(*) AS n_docs,
                   MIN(d.filed_at)::date AS first_mention,
                   COUNT(*) FILTER (WHERE d.filed_at >  %s::date - 365) AS n_last12m,
                   COUNT(*) FILTER (WHERE d.filed_at <= %s::date - 365
                                      AND d.filed_at >  %s::date - 730) AS n_prior12m,
                   MAX(CONCAT_WS(' | ', NULLIF(sm.industry_nse,''), NULLIF(sm.industry_bse,''))) AS industry
            FROM mg_documents d
            LEFT JOIN security_master sm ON sm.nse_symbol = d.ticker
            WHERE d.country = 'IN' AND d.ticker IS NOT NULL AND d.ticker <> ''
              AND d.filed_at <= %s
              AND d.raw_text ~* %s
            GROUP BY d.ticker
            HAVING COUNT(*) >= 1
            ORDER BY COUNT(*) FILTER (WHERE d.filed_at > %s::date - 365) DESC,
                     COUNT(*) DESC
            LIMIT 60
        """, (as_of, as_of, as_of, as_of, pattern, as_of))
        lst, pings = [], []
        for r in rows:
            tick = (r["ticker"] or "").strip().upper()
            if tick in MANUAL_EXCLUDE:
                continue
            entry = {
                "ticker": tick,
                "industry": r["industry"] or None,
                "n_docs_total": int(r["n_docs"]),
                "n_last12m": int(r["n_last12m"]),
                "n_prior12m": int(r["n_prior12m"]),
                "trend": ("rising" if r["n_last12m"] > r["n_prior12m"]
                          else "fading" if r["n_last12m"] < r["n_prior12m"] else "flat"),
                "first_mention": r["first_mention"],
                "commitment": commits.get(tick),   # non-null = winner-confirmed disclosure
            }
            if entry["n_docs_total"] >= min_docs:
                lst.append(entry)
            elif entry["n_last12m"] >= 1:
                # EARLY PING: below the evidence threshold — watch-only, never a
                # decision input. Exists so a Shakti-2022 (2 KUSUM docs) sits on
                # the radar the whole time it accumulates toward qualification.
                pings.append(entry)
        # winner-confirmed names first, then by recent intensity
        lst.sort(key=lambda x: (x["commitment"] is None, -x["n_last12m"]))
        if lst:
            out[scheme] = lst[:top_n]
        pings.sort(key=lambda x: (x["first_mention"] is None, str(x["first_mention"])), reverse=True)
        if pings:
            early[scheme] = pings[:10]
    return {"qualified": out, "early_pings": early}


def compute_chain_capex_momentum(cur, as_of) -> dict:
    """T-COIL evidence: per chain, aggregate capex_increase signals in the latest
    snapshot <= as_of vs the snapshot ~1 year earlier. Rising capex = entry signal
    for differentiated chains; FALLING capex = the coiling signal for commodity
    chains (supply discipline creates tomorrow's squeeze). Evidence field only."""
    rows = q(cur, """
        WITH snaps AS (
            SELECT MAX(as_of_date) FILTER (WHERE as_of_date <= %s)                 AS cur_snap,
                   MAX(as_of_date) FILTER (WHERE as_of_date <= %s::date - 300)     AS prior_snap
            FROM mg_india_beneficiaries
        )
        SELECT b.constrained_product,
               b.as_of_date = s.cur_snap AS is_cur,
               SUM(COALESCE(NULLIF(substring(b.rationale FROM '(\\d+) capex_increase signals'), ''), '0')::int) AS capex
        FROM mg_india_beneficiaries b, snaps s
        WHERE b.constrained_product IS NOT NULL
          AND b.as_of_date IN (s.cur_snap, s.prior_snap)
        GROUP BY 1, 2
    """, (as_of, as_of))
    agg: dict[str, dict] = {}
    for r in rows:
        e = agg.setdefault(r["constrained_product"], {"capex_now": 0, "capex_prior": 0})
        e["capex_now" if r["is_cur"] else "capex_prior"] += int(r["capex"] or 0)
    for chain, e in agg.items():
        e["momentum"] = e["capex_now"] - e["capex_prior"]
        lt = chain_layer_type(chain)
        e["layer_type"] = lt
        if lt == "commodity" and e["momentum"] < 0:
            e["signal"] = "COILING — commodity layer cutting capex; watch for demand catalyst (trade setup, not compounder)"
        elif lt == "differentiated" and e["momentum"] > 0:
            e["signal"] = "CAPEX RISING into differentiated constraint — compounder entry signal"
        else:
            e["signal"] = None
    return agg


def detect_chain_continuity_alerts(cur, as_of, window_months: int = 12) -> list:
    """Coverage alarm: chains that had beneficiary rows in older snapshots but have
    NO fresh mapping in the scan window, or whose latest mapping is stale.

    The solar lesson: solar chains had rows for 2020-22 snapshots, none for
    2023-25 — the mapper silently skipped them and the +579% 2023 solar year
    happened in a blind spot. Transformer/rolling-stock rows stopped after
    Dec-2025 the same way. A vanished chain is a pipeline failure to
    investigate, never evidence the constraint resolved."""
    rows = q(cur, """
        SELECT constrained_product,
               MAX(as_of_date) AS last_mapped,
               MAX(as_of_date) FILTER (WHERE as_of_date <= %s) AS last_mapped_pit
        FROM mg_india_beneficiaries
        WHERE constrained_product IS NOT NULL
        GROUP BY 1
    """, (as_of,))
    alerts = []
    for r in rows:
        lm = r["last_mapped_pit"]
        if lm is None:
            continue
        age_days = (as_of - lm).days
        if age_days > 150:   # no fresh mapping in ~2 quarters
            alerts.append({
                "chain": r["constrained_product"],
                "last_mapped": lm,
                "staleness_days": age_days,
                "alert": ("chain vanished from recent mapper snapshots — pipeline gap, "
                          "NOT evidence of resolution; verify the constraint manually"),
            })
    alerts.sort(key=lambda a: -a["staleness_days"])
    return alerts


def detect_chain_ticker_overlap(cur, as_of, overlap_threshold: float = 0.7) -> list:
    """De-dup detector — the 'electrical equipment' == 'transformer' lesson.

    Jul-2026 cohort-explosiveness study found EMS/PCB/Semiconductor IC sharing
    100% identical tickers in the same snapshot, and (via mg_theme_beneficiaries)
    'electrical equipment: Demand Surge' sharing its whole cohort with
    'transformer: Demand-Supply Tension' — two NLP labels for one constraint.
    Treating each as independent confirmation double-counts one signal.

    Flags any pair of constrained_product chains at the current snapshot whose
    ticker sets overlap >= overlap_threshold. Evidence only — the judgment layer
    must merge such pairs before counting them as separate confirmations."""
    rows = q(cur, """
        SELECT constrained_product, ARRAY_AGG(DISTINCT UPPER(TRIM(ticker))) AS ticks
        FROM mg_india_beneficiaries
        WHERE as_of_date = (SELECT MAX(as_of_date) FROM mg_india_beneficiaries WHERE as_of_date <= %s)
          AND constrained_product IS NOT NULL AND ticker IS NOT NULL
        GROUP BY constrained_product
    """, (as_of,))
    chains = [(r["constrained_product"], set(r["ticks"])) for r in rows]
    dupes = []
    for i in range(len(chains)):
        for j in range(i + 1, len(chains)):
            n1, s1 = chains[i]
            n2, s2 = chains[j]
            if not s1 or not s2:
                continue
            inter = len(s1 & s2)
            overlap = inter / min(len(s1), len(s2))
            if overlap >= overlap_threshold:
                dupes.append({
                    "chain_a": n1, "chain_b": n2,
                    "overlap_pct": round(overlap * 100),
                    "n_a": len(s1), "n_b": len(s2), "n_shared": inter,
                    "note": "same underlying cohort under two labels — merge before counting as independent evidence",
                })
    return dupes


def compute_chain_cohort_explosiveness(cur, as_of) -> dict:
    """Multi-vintage cohort durability test (Jul-2026 explosiveness study).

    A constraint that lifts its WHOLE beneficiary cohort at MULTIPLE separate
    entry vintages (not one lottery winner, not one lucky year) is structurally
    different from a decaying-consensus theme or a commodity round-trip. Only
    CRGO Steel/Transformer passed this test across 2020/2021/2022 entries
    (median 2yr +79% to +136% every vintage; Defense electronics decayed
    57%->12%->4%; Solar's 3yr number collapsed 64%->18% between vintages).

    Strictly point-in-time: only counts a vintage snapshot if snapshot_date +
    730 days <= as_of (the 2-year outcome must have already happened by the
    scan date). Evidence for the judgment layer only — never a score input,
    and never proof by itself (small-n cohorts are noisy)."""
    cur.execute("SELECT DISTINCT as_of_date FROM mg_india_beneficiaries WHERE constrained_product IS NOT NULL")
    all_snaps = sorted(r["as_of_date"] for r in cur.fetchall())
    usable_snaps = [s for s in all_snaps if (as_of - s).days >= 730]
    if not usable_snaps:
        return {}

    out: dict[str, dict] = {}
    for snap in usable_snaps:
        rows = q(cur, """
            SELECT constrained_product, ARRAY_AGG(DISTINCT UPPER(TRIM(ticker))) AS ticks
            FROM mg_india_beneficiaries
            WHERE as_of_date = %s AND constrained_product IS NOT NULL AND ticker IS NOT NULL
            GROUP BY constrained_product
        """, (snap,))
        for r in rows:
            chain = r["constrained_product"]
            tickers = [t for t in r["ticks"] if t]
            if len(tickers) < 8:
                continue
            rets = []
            for t in tickers:
                cur.execute("""
                    WITH e AS (SELECT close FROM nse_bhavcopy_data WHERE symbol=%s
                               AND trade_date BETWEEN %s::date AND %s::date+10 ORDER BY trade_date LIMIT 1),
                         x AS (SELECT close FROM nse_bhavcopy_data WHERE symbol=%s
                               AND trade_date <= %s::date+730 ORDER BY trade_date DESC LIMIT 1)
                    SELECT (x.close-e.close)/e.close*100 AS ret FROM e,x WHERE e.close>0
                """, (t, snap, snap, t, snap))
                row = cur.fetchone()
                if row and row["ret"] is not None:
                    rets.append(float(row["ret"]))
            if len(rets) < 8:
                continue
            rets.sort()
            n = len(rets)
            median = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
            pct_gt_100 = round(sum(1 for x in rets if x > 100) / n * 100)
            entry = out.setdefault(chain, {"vintages": []})
            entry["vintages"].append({
                "entry_date": str(snap), "n": n,
                "median_2y_return_pct": round(median, 1),
                "pct_cohort_gt_100pct": pct_gt_100,
            })

    for chain, entry in out.items():
        vintages = entry["vintages"]
        n_broad_wins = sum(1 for v in vintages if v["median_2y_return_pct"] > 50 and v["pct_cohort_gt_100pct"] >= 40)
        entry["n_vintages_tested"] = len(vintages)
        entry["n_broad_cohort_wins"] = n_broad_wins
        entry["durable"] = len(vintages) >= 2 and n_broad_wins == len(vintages)
        entry["note"] = ("PASSED durability test: every tested entry vintage produced a broad cohort win — "
                         "the CRGO/transformer fingerprint" if entry["durable"] else
                         "did not pass durability test across all tested vintages — treat as a single-cycle or decaying theme")
    return out


def identify_narrow_fresh_constraints(major_themes: list, emerging_themes: list,
                                       breadth_ceiling: int = 60, max_quarters: int = 10) -> list:
    """Forward-discovery candidates: themes narrow + fresh enough to resemble
    what CRGO Steel looked like in Dec-2020 (23-28 mapped companies, early
    stage) BEFORE anyone had run a cohort study to notice.

    Pure derivation from theme data already fetched this scan — no new query.
    Purely a discovery/manual-mapping-priority list, never a score input; a
    theme still needs the constraint-quality grade (real vs artifact,
    quantified gap, layer type) before it's investable."""
    seen, out = set(), []
    for t in (major_themes or []) + (emerging_themes or []):
        name = t.get("theme_name")
        cc = t.get("company_count")
        q_ = t.get("confirmed_quarters")
        if not name or name in seen or cc is None:
            continue
        seen.add(name)
        if cc <= breadth_ceiling and (q_ is None or q_ <= max_quarters) and \
           (t.get("stage_label") or "") in ("Emerging", "Accelerating", "Hidden Formation"):
            out.append({
                "theme_name": name, "company_count": cc,
                "stage_label": t.get("stage_label"), "confirmed_quarters": q_,
                "first_detected": t.get("first_detected"),
                "note": "narrow + fresh — manual-mapping priority; grade the constraint before treating as investable",
            })
    out.sort(key=lambda x: x["company_count"])
    return out


# ── Coverage-gap detector: term derivation per chain ─────────────────────────
# Word-boundary regex per constrained-product chain; chains without an override
# fall back to the full chain name as a case-insensitive phrase. Kept generic —
# this maps NAMES to search terms, it encodes no sector opinion.
PEER_TERM_OVERRIDES: dict[str, str] = {
    "power transformer":  r"\ytransformers?\y",
    "crgo steel":         r"\yCRGO\y",
    "solar cell":         r"solar cells?",
    "solar module":       r"solar modules?",
    "solar wafer":        r"solar wafers?",
    "battery cell":       r"battery cells?|li-?ion cell",
    "ems / contract manufacturing": r"electronic manufacturing services|\yEMS\y",
    "pcb / printed circuit board":  r"printed circuit",
    "optical fiber cable": r"optical fib",
    "defense electronics": r"defen[cs]e electronics",
    "rolling stock / locomotives": r"rolling stock|locomotives?",
    "semiconductor ic":    r"semiconductor",
}


def _peer_term(chain_name: str) -> str:
    key = (chain_name or "").strip().lower()
    for k, pattern in PEER_TERM_OVERRIDES.items():
        if k in key or key in k:
            return pattern
    return key  # fallback: the chain name itself as a phrase


def detect_unmapped_peers(cur, supply_by_product, as_of,
                          lookback_days: int = 730, min_docs: int = 5,
                          top_n: int = 8) -> dict:
    """Coverage-gap detector: listed companies whose OWN filings repeatedly
    mention a constrained product but that the NLP beneficiary mapper never
    linked to the chain (the Voltamp/TARIL failure mode — pure-plays whose
    dedicated filings never co-occur with theme documents).

    Purely an evidence field: these names are NOT scored or ranked; the
    judgment layer must review them per selected theme. Point-in-time
    (filed_at <= as_of). Companies with a known-incompatible industry_nse
    are dropped; unknown industry passes through (master data is sparse).
    """
    from_date = as_of - timedelta(days=lookback_days)
    out: dict[str, list] = {}
    seen_terms: dict[str, list] = {}
    for chain, companies in supply_by_product.items():
        term = _peer_term(chain)
        mapped = {(c.get("ticker") or "").strip().upper() for c in companies}
        if term in seen_terms:
            rows = seen_terms[term]
        else:
            rows = q(cur, """
                SELECT d.ticker, COUNT(*) AS n, MAX(d.filed_at) AS latest,
                       MAX(sm.company_name) AS company,
                       MAX(CONCAT_WS(' / ', NULLIF(sm.industry_nse,''), NULLIF(sm.industry_bse,''))) AS industry
                FROM mg_documents d
                LEFT JOIN security_master sm ON sm.nse_symbol = d.ticker
                WHERE d.country = 'IN'
                  AND d.ticker IS NOT NULL
                  AND d.filed_at BETWEEN %s AND %s
                  AND d.raw_text ~* %s
                GROUP BY d.ticker
                HAVING COUNT(*) >= %s
                ORDER BY COUNT(*) DESC
                LIMIT 40
            """, (from_date, as_of, term, min_docs))
            seen_terms[term] = rows
        peers = []
        for r in rows:
            tick = (r["ticker"] or "").strip().upper()
            if not tick or tick in mapped or tick in MANUAL_EXCLUDE:
                continue
            if _sector_check(r["industry"], chain) is False:
                continue   # known-incompatible sector; unknown passes
            peers.append({
                "ticker": tick,
                "company": r["company"],
                "industry": r["industry"] or None,
                "industry_match": _sector_check(r["industry"], chain) is True,
                "n_filings_mentioning": int(r["n"]),
                "latest_mention": r["latest"],
                "note": "unmapped by beneficiary extractor — review in judgment layer",
            })
            if len(peers) >= top_n:
                break
        # industry-corroborated pure-plays first, then by filings intensity
        peers.sort(key=lambda p: (not p["industry_match"], -p["n_filings_mentioning"]))
        if peers:
            out[chain] = peers
    return out


def compute_chain_technical_state(cur, supply_by_product, as_of) -> dict:
    """Chain-level technical state: is a chain's mapped cohort extended or
    de-rated as of the scan date?

    POSITION-SIZING CONTEXT ONLY — never selection. Per the user's standing
    rule (and the backtest: above-200DMA showed no alpha edge), price action
    must not change a grade, verdict, or category. This field only informs
    sizing/entry notes on already-selected names: DERATED cohort = full-size
    entries available; EXTENDED_CROWDED = stagger/half-size.
    Labels: DERATED (<=45% above 200DMA and median <= -25% from 52w high),
    EXTENDED_CROWDED (>=70% above and median > -12%), else MIXED.
    """
    all_ticks = sorted({(c.get("ticker") or "").strip().upper()
                        for lst in supply_by_product.values() for c in lst
                        if c.get("ticker") and not c.get("sector_mismatch")
                        and not c.get("demand_side_flag")})
    if not all_ticks:
        return {}
    rows = q(cur, """
        SELECT sym,
               (SELECT close FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 1) AS last,
               (SELECT AVG(close) FROM (
                    SELECT close FROM nse_bhavcopy_data
                    WHERE symbol = sym AND trade_date <= %s
                    ORDER BY trade_date DESC LIMIT 200) a) AS dma200,
               (SELECT MAX(close) FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date BETWEEN %s::date - 365 AND %s) AS high52
        FROM UNNEST(%s::text[]) AS sym
    """, (as_of, as_of, as_of, as_of, all_ticks))
    tech = {}
    for r in rows:
        if r["last"] and r["dma200"] and r["high52"]:
            last = float(r["last"])
            tech[r["sym"]] = {
                "above_200dma": last > float(r["dma200"]),
                "pct_from_52w_high": round((last - float(r["high52"])) / float(r["high52"]) * 100, 1),
            }

    out = {}
    for chain, companies in supply_by_product.items():
        ticks = [(c.get("ticker") or "").strip().upper() for c in companies
                 if c.get("ticker") and not c.get("sector_mismatch")
                 and not c.get("demand_side_flag")]
        pts = [tech[t] for t in ticks if t in tech]
        if len(pts) < 3:
            continue
        n = len(pts)
        pct_above = round(sum(1 for p in pts if p["above_200dma"]) / n * 100)
        offs = sorted(p["pct_from_52w_high"] for p in pts)
        med = offs[n // 2] if n % 2 else (offs[n // 2 - 1] + offs[n // 2]) / 2
        if pct_above <= 45 and med <= -25:
            label = "DERATED"
            note = "sizing context only: cohort de-rated — full-size entries available on evidence-selected names"
        elif pct_above >= 70 and med > -12:
            label = "EXTENDED_CROWDED"
            note = "sizing context only: cohort near highs — stagger/half-size entries on evidence-selected names"
        else:
            label = "MIXED"
            note = "sizing context only: no cohort-level signal"
        out[chain] = {
            "n_with_price": n,
            "pct_above_200dma": pct_above,
            "median_pct_from_52w_high": round(med, 1),
            "label": label,
            "note": note,
        }
    return out


def fetch_risk_flags(cur, tickers: list[str], as_of, lookback_days: int = 730) -> dict:
    """Lesson 4: downside-risk overlay from mg_documents filing_type field.

    2020-2024 backtest showed ALL severe losses (>30%) involved companies with
    high fundamental scores but no governance/risk filter — GENSOL (0.850→-95%),
    TATASTEEL (0.839→-90%), SIEMENS (0.837→-53%).

    HIGH: fraud, IBC commencement, suspension, liquidation (consider excluding).
    ELEVATED: auditor resignation, payment default, delayed results (size down).
    NORMAL: no red-flag filings in lookback window.
    """
    if not tickers:
        return {}
    from_date = as_of - timedelta(days=lookback_days)
    rows = q(cur, """
        SELECT ticker, filing_type, title, filed_at
        FROM mg_documents
        WHERE ticker = ANY(%s)
          AND filed_at BETWEEN %s AND %s
          AND filing_type IS NOT NULL
        ORDER BY ticker, filed_at DESC
    """, (tickers, from_date, as_of))

    result: dict[str, dict] = {}
    for r in rows:
        tick = (r["ticker"] or "").strip().upper()
        if not tick:
            continue
        ft = r["filing_type"] or ""
        entry = result.setdefault(tick, {"risk_tier": "NORMAL", "risk_events": []})
        if ft in HIGH_RISK_FILING_TYPES:
            entry["risk_tier"] = "HIGH"
            entry["risk_events"].append({
                "type": ft, "date": r["filed_at"], "title": (r["title"] or "")[:80]
            })
        elif ft in ELEVATED_RISK_FILING_TYPES:
            if entry["risk_tier"] == "NORMAL":
                entry["risk_tier"] = "ELEVATED"
            entry["risk_events"].append({
                "type": ft, "date": r["filed_at"], "title": (r["title"] or "")[:80]
            })
    return result


# India policy schemes worth detecting in filings. Word-boundary regex per scheme;
# a filing claiming PLI allocation / ALMM listing is revenue-visibility evidence,
# NOT a score input — the judgment layer weighs it (a scheme can also CREATE
# overcapacity, e.g. module PLI feeding "Solar Module Overcapacity Risk").
INDIA_POLICY_SCHEMES = {
    "PLI":          r"\yPLI\y|production[- ]linked",
    "ALMM":         r"\yALMM\y|approved list of models",
    "BCD":          r"basic customs duty",
    "RDSS":         r"\yRDSS\y|revamped distribution sector",
    # Patterns tightened Jul-2026: bare \yFAME\y matched the English word (hotels),
    # bare KUSUM matched the mango variety (food companies), \yISM\y over-matched.
    # FAME ended Mar-2024 → succeeded by EMPS-2024 → PM E-DRIVE (Oct-2024).
    # Scheme-succession lesson: a "fading" cohort can be vocabulary migration —
    # check the successor scheme's terms before declaring the wave dead.
    "EV incentives (FAME→E-DRIVE)": r"FAME[- ]?(II|2|scheme|subsid)|electric mobility promotion|EMPS[- ]?2024|PM[- ]?E[- ]?DRIVE|E[- ]DRIVE scheme|e[- ]?bus sewa",
    "Green Hydrogen (SIGHT)": r"SIGHT scheme|green hydrogen mission|\yNGHM\y|green hydrogen incentive",
    "PM-KUSUM":     r"PM[- ]?KUSUM|KUSUM (scheme|yojana|component|tender)|kusum solar",
    "PM Surya Ghar": r"surya ghar",
    "Semicon Mission": r"semicon india|india semiconductor mission|semiconductor mission",
    "ACC Battery":  r"advanced chemistry cell",
    "Defence iDEX/Make": r"\yiDEX\y|make in india defence|defence acquisition procedure",
    # Explosion-fingerprint classes (from the 50-100x cohort study, Jul-2026):
    # T1 mandate+approved-vendor oligopoly (KERNEX/KAVACH 126x) and
    # T3 formalization share-shift (GRAVITA/EPR 46x)
    "Safety/Compliance Mandate": r"\ykavach\y|made mandatory|mandatory (installation|implementation|compliance)|RDSO approv",
    "EPR/Formalization": r"extended producer responsibilit|\yEPR\y (registration|certificate|obligation)|battery waste management rules|plastic waste management rules",
}


def compute_theme_track_record(cur, as_of) -> dict:
    """Per-theme historical alpha: when this theme's beneficiaries were mapped in
    past snapshots, did they beat the market over the next 180 days?

    Strictly point-in-time: only snapshots with as_of_date + 180d <= as_of are
    measured, so no forward window leaks past the scan date. Evidence for the
    judgment layer's theme-selection step — never a score input.

    Returns {theme_name: {n_measured, median_excess_180d_pct, hit_rate_pct,
                          n_windows}}.
    """
    rows = q(cur, """
        WITH snaps AS (
            SELECT DISTINCT as_of_date AS snap
            FROM mg_india_beneficiaries
            WHERE as_of_date + 180 <= %s::date
        ),
        bene AS (
            SELECT b.theme_name, UPPER(TRIM(b.ticker)) AS ticker, s.snap
            FROM mg_india_beneficiaries b
            JOIN snaps s ON b.as_of_date = s.snap
            WHERE b.ticker IS NOT NULL AND b.conviction_score >= 0.7
            GROUP BY 1, 2, 3
        )
        SELECT bene.theme_name, bene.snap, bene.ticker,
               ROUND(((x.close - e.close) / e.close * 100)::numeric, 1) AS fwd180
        FROM bene
        JOIN LATERAL (
            SELECT close FROM nse_bhavcopy_data
            WHERE symbol = bene.ticker
              AND trade_date BETWEEN bene.snap AND bene.snap + 10
            ORDER BY trade_date LIMIT 1) e ON e.close > 0
        JOIN LATERAL (
            SELECT close FROM nse_bhavcopy_data
            WHERE symbol = bene.ticker AND trade_date <= bene.snap + 180
            ORDER BY trade_date DESC LIMIT 1) x ON true
    """, (as_of,))

    # Benchmark: median 180d proxy return per snapshot window
    bench_rows = q(cur, """
        WITH snaps AS (
            SELECT DISTINCT as_of_date AS snap
            FROM mg_india_beneficiaries
            WHERE as_of_date + 180 <= %s::date
        ),
        px AS (
            SELECT s.snap, sym,
                   (x.close - e.close) / e.close * 100 AS fwd180
            FROM snaps s
            CROSS JOIN UNNEST(%s::text[]) AS sym
            JOIN LATERAL (
                SELECT close FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date BETWEEN s.snap AND s.snap + 10
                ORDER BY trade_date LIMIT 1) e ON e.close > 0
            JOIN LATERAL (
                SELECT close FROM nse_bhavcopy_data
                WHERE symbol = sym AND trade_date <= s.snap + 180
                ORDER BY trade_date DESC LIMIT 1) x ON true
        )
        SELECT snap, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fwd180) AS bench
        FROM px GROUP BY snap
    """, (as_of, MARKET_PROXY_IN))
    bench = {r["snap"]: float(r["bench"]) for r in bench_rows}

    by_theme: dict[str, dict] = {}
    for r in rows:
        b = bench.get(r["snap"])
        if b is None or r["fwd180"] is None:
            continue
        excess = float(r["fwd180"]) - b
        t = by_theme.setdefault(r["theme_name"], {"excess": [], "windows": set()})
        t["excess"].append(excess)
        t["windows"].add(r["snap"])

    out = {}
    for name, t in by_theme.items():
        vals = sorted(t["excess"])
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        out[name] = {
            "n_measured": n,
            "n_windows": len(t["windows"]),
            "median_excess_180d_pct": round(med, 1),
            "hit_rate_pct": round(sum(1 for v in vals if v > 0) / n * 100),
        }
    return out


def fetch_policy_evidence(cur, tickers: list[str], as_of, lookback_days: int = 730) -> dict:
    """India-specific policy overlay: which govt schemes does each candidate's own
    filings mention in the lookback window (point-in-time, <= as_of)?

    Company-level scheme mentions (PLI allocation, ALMM listing, BCD protection)
    are hard revenue-visibility evidence the composite formula cannot see.
    Purely an evidence field for the judgment layer — never scored.
    """
    if not tickers:
        return {}
    from_date = as_of - timedelta(days=lookback_days)
    result: dict[str, dict] = {}
    for scheme, pattern in INDIA_POLICY_SCHEMES.items():
        rows = q(cur, """
            SELECT ticker, COUNT(*) AS n, MAX(filed_at) AS latest,
                   (ARRAY_AGG(title ORDER BY filed_at DESC))[1] AS latest_title
            FROM mg_documents
            WHERE ticker = ANY(%s)
              AND country = 'IN'
              AND filed_at BETWEEN %s AND %s
              AND raw_text ~* %s
            GROUP BY ticker
        """, (tickers, from_date, as_of, pattern))
        for r in rows:
            tick = (r["ticker"] or "").strip().upper()
            if not tick:
                continue
            entry = result.setdefault(tick, {"schemes": {}, "n_policy_filings": 0})
            entry["schemes"][scheme] = {
                "n_filings": int(r["n"]),
                "latest": r["latest"],
                "latest_title": (r["latest_title"] or "")[:80],
            }
            entry["n_policy_filings"] += int(r["n"])
    return result


def compute_market_regime(cur, as_of, country: str = "IN") -> dict:
    """Lesson 6: broad-market regime context for position-sizing.

    2020-2024 backtest: hit rate ranged 28-80% depending on the regime.
    Same regime signal in two flat years (2021 mkt=+3%, 2023 mkt=-0.3%) produced
    wildly different selector alpha (-1% vs +43%) — showing that market-index level
    alone doesn't capture the theme-execution dimension. But breadth + 3m trend does.

    Uses the same 25-stock large-cap proxy as the backtest.
    """
    if country != "IN":
        return {"regime_label": "N/A", "note": "Market regime only computed for IN"}

    px = q(cur, """
        SELECT symbol, trade_date, close
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series IN ('EQ','BE')
          AND trade_date BETWEEN %s AND %s
        ORDER BY symbol, trade_date
    """, (MARKET_PROXY_IN, as_of - timedelta(days=420), as_of))

    series_map: dict[str, list[float]] = defaultdict(list)
    for r in px:
        series_map[r["symbol"]].append(float(r["close"]))

    above_200 = 0
    total = 0
    three_month_rets: list[float] = []

    for sym, closes in series_map.items():
        if len(closes) < 40:
            continue
        total += 1
        last = closes[-1]
        ma200 = sum(closes[-200:]) / min(200, len(closes))
        if last > ma200:
            above_200 += 1
        base_3m = closes[-63] if len(closes) >= 63 else closes[0]
        three_month_rets.append((last - base_3m) / base_3m * 100)

    breadth_pct = round(above_200 / total * 100, 0) if total else 0
    avg_3m_ret = round(sum(three_month_rets) / len(three_month_rets), 1) if three_month_rets else 0

    if breadth_pct >= 60 and avg_3m_ret > 3:
        regime = "BULL_FAVORABLE"
        note = (
            f"Broad-market breadth strong ({int(breadth_pct)}% of large caps above 200DMA, "
            f"{avg_3m_ret:+.1f}% avg 3m return). Historical selector hit rate: 57-80%. "
            "Standard position sizing applies."
        )
    elif breadth_pct < 40 or avg_3m_ret < -3:
        regime = "BEAR_CAUTION"
        note = (
            f"Broad-market breadth weak ({int(breadth_pct)}% above 200DMA, "
            f"{avg_3m_ret:+.1f}% avg 3m). Historical selector hit rate drops to 28-43% "
            "in similar regimes (2021, 2024 India analogs). Consider halving Tier 1 position sizes."
        )
    else:
        regime = "FLAT_MIXED"
        note = (
            f"Mixed breadth ({int(breadth_pct)}% above 200DMA, {avg_3m_ret:+.1f}% avg 3m). "
            "In flat markets, selector alpha is entirely theme-dependent: +43% alpha in 2023 "
            "(T&D/defence supercycle active), -6% alpha in 2024 (no sector tailwind, governance blowups). "
            "Verify theme execution evidence before sizing up."
        )

    return {
        "regime_label": regime,
        "breadth_pct_above_200dma": breadth_pct,
        "avg_3m_return_pct": avg_3m_ret,
        "stocks_checked": total,
        "note": note,
    }


def fetch_theme_focus(cur, name, as_of, win_start, country):
    """Deep-dive on ONE theme or constrained product (fuzzy name match)."""
    like = f"%{name}%"
    themes = q(cur, """
        SELECT id AS theme_id, theme_name, theme_slug, description, sectors, conviction,
               first_detected, stage, stage_label, stage_evidence, hypothesis_text,
               strength_score, momentum_score, company_count, doc_count, metadata
        FROM mg_themes
        WHERE country=%s AND is_active AND theme_name ILIKE %s AND first_detected <= %s
        ORDER BY strength_score DESC NULLS LAST LIMIT 5
    """, (country, like, as_of))
    for t in themes:
        meta = t.pop("metadata") or {}
        t["supply_constraint_count"] = meta.get("supply_constraint_count") or 0
        t["is_bottleneck"] = bool(meta.get("is_bottleneck"))
        t["tension_score"] = meta.get("tension_score")
        t["snapshots"] = q(cur, """
            SELECT snapshot_date, strength_score, momentum_score, company_count
            FROM mg_theme_snapshots WHERE theme_id=%s AND snapshot_date <= %s
            ORDER BY snapshot_date DESC LIMIT 10
        """, (t["theme_id"], as_of))
        t["beneficiaries"] = q(cur, """
            SELECT b.ticker, b.company_name AS company, b.beneficiary_type, b.company_role,
                   b.relevance_score, b.rank_in_theme, b.signal_count, b.capex_signals,
                   b.first_seen_at, left(b.reasoning, 200) AS rationale
            FROM mg_theme_beneficiaries b
            WHERE b.theme_id=%s AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
            ORDER BY b.rank_in_theme ASC NULLS LAST, b.relevance_score DESC LIMIT 30
        """, (t["theme_id"], as_of))

    product_map, supply = [], {}
    if country == "IN":
        product_map = q(cur, """
            SELECT constrained_product, theme_name,
                   count(DISTINCT company) AS n_companies,
                   round(avg(conviction_score)::numeric,3) AS avg_conviction,
                   bool_or(has_order_book_signals) AS any_order_book,
                   bool_or(import_substitution_play) AS any_import_sub,
                   min(as_of_date) AS first_mapped, max(as_of_date) AS last_mapped
            FROM mg_india_beneficiaries
            WHERE (constrained_product ILIKE %s OR theme_name ILIKE %s) AND as_of_date <= %s
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10
        """, (like, like, as_of))
        rows = q(cur, """
            SELECT DISTINCT ON (company)
                   company, ticker, theme_name, constrained_product, supply_chain_node,
                   beneficiary_type, conviction_score, left(rationale, 220) AS rationale,
                   signal_count, has_order_book_signals, import_substitution_play, as_of_date
            FROM mg_india_beneficiaries
            WHERE (constrained_product ILIKE %s OR theme_name ILIKE %s)
              AND beneficiary_type = ANY(%s) AND as_of_date <= %s
            ORDER BY company, as_of_date DESC
        """, (like, like, list(SUPPLY_SIDE_TYPES), as_of))
        rows.sort(key=lambda r: (-(float(r["conviction_score"] or 0)), not r["has_order_book_signals"]))
        supply = {"matched_supply_side_companies": rows[:25]}
    gaps = q(cur, """
        SELECT sector, component, gap, gap_pct, unit, severity, target_year
        FROM mg_capacity_gaps
        WHERE (component ILIKE %s OR theme_name ILIKE %s)
          AND (as_of_date IS NULL OR as_of_date <= %s) LIMIT 8
    """, (like, like, as_of)) if country == "IN" else []
    imports = q(cur, """
        SELECT sector, component, import_share, primary_origin, risk_level,
               substitute_possible, substitution_horizon_years
        FROM mg_import_dependencies
        WHERE component ILIKE %s AND (as_of_date IS NULL OR as_of_date <= %s) LIMIT 8
    """, (like, as_of)) if country == "IN" else []
    return {"query": name, "matched_themes": themes, "matched_constrained_products": product_map,
            "capacity_gaps": gaps, "import_dependencies": imports, **supply}


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="As-of-date stock selector extraction")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--country", default="IN", choices=["IN", "US"])
    ap.add_argument("--theme", default=None,
                    help="focus on ONE theme/constraint (fuzzy name match) instead of the full scan")
    ap.add_argument("--window-months", type=int, default=12,
                    help="emergence lookback window in months (default 12)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    as_of = parse_as_of(args.as_of)
    win_start = as_of - timedelta(days=args.window_months * 30)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if args.theme:
        focus = fetch_theme_focus(cur, args.theme, as_of, win_start, args.country)
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "as_of_date": as_of.isoformat(),
            "country": args.country,
            "mode": "theme_focus",
            "focus": focus,
        }
        os.makedirs(REPORTS_DIR, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in args.theme)[:40]
        out_path = args.out or os.path.join(
            REPORTS_DIR, f"stock_selector_{args.country}_{safe}_{as_of.isoformat()}_data.json")
        with open(out_path, "w") as f:
            json.dump(report, f, default=jsonify, indent=1)
        conn.close()
        print(out_path)
        print("SUMMARY:", json.dumps({
            "matched_themes": len(focus["matched_themes"]),
            "matched_constrained_products": len(focus["matched_constrained_products"]),
            "supply_side_companies": len(focus.get("matched_supply_side_companies", [])),
        }))
        return

    major, emerging, landscape = fetch_theme_landscape(cur, as_of, args.window_months, args.country)

    # Improvement 1: evidence dashboard (policy counts batch-fetched once)
    all_themes_for_evidence = list({t["theme_id"]: t for t in major + emerging}.values())
    policy_counts = fetch_policy_event_counts(cur, all_themes_for_evidence, as_of, args.country)
    evidence_dashboard = build_evidence_dashboard(all_themes_for_evidence, policy_counts)

    # Lesson 5: build theme_name → confirmed_quarters lookup for phase-adjusted tech
    theme_quarters = {
        t["theme_name"]: (t.get("confirmed_quarters") or 0)
        for t in major + emerging
        if t.get("theme_name")
    }

    if args.country == "IN":
        products, gaps, imports = fetch_constraints(cur, as_of, args.window_months)
        supply = fetch_supply_beneficiaries(cur, as_of, args.window_months)
        candidates = rank_candidates(cur, supply, as_of, theme_quarters=theme_quarters)
        cross_theme_overlap = compute_cross_theme_overlap(supply)
        bear_cases = generate_bear_cases(major, emerging, candidates, args.country)
        chain_coverage = compute_chain_coverage(supply, major, emerging)
        # Lesson 6: market regime signal for position-sizing context
        market_regime = compute_market_regime(cur, as_of, args.country)
        # Theme track record: did this theme's beneficiaries beat the market when
        # mapped in past snapshots? Judgment-layer evidence for theme selection.
        theme_track_record = compute_theme_track_record(cur, as_of)
        # Coverage-gap detector + chain technical state (judgment-layer evidence)
        unmapped_peers = detect_unmapped_peers(cur, supply, as_of)
        chain_technical_state = compute_chain_technical_state(cur, supply, as_of)
        # T-COIL evidence + chain-continuity alarms (the solar-2023 lesson)
        chain_capex_momentum = compute_chain_capex_momentum(cur, as_of)
        chain_continuity_alerts = detect_chain_continuity_alerts(cur, as_of)
        # Policy-first discovery engine (the Dixon/PGEL/Amber lesson)
        _pol = compute_policy_beneficiary_screen(cur, as_of)
        policy_beneficiary_screen = _pol["qualified"]
        policy_early_pings = _pol["early_pings"]
        # Explosiveness study (Jul-2026): de-dup detector, multi-vintage cohort
        # durability test, and narrow+fresh forward-discovery candidates
        chain_cohort_duplicates = detect_chain_ticker_overlap(cur, as_of)
        chain_cohort_explosiveness = compute_chain_cohort_explosiveness(cur, as_of)
        narrow_fresh_candidates = identify_narrow_fresh_constraints(major, emerging)
        us_note = None
    else:
        # beneficiaries for every major + emerging theme, PLUS any strongly
        # supply-constrained theme from the full landscape (catches chains like
        # "Wafer Critical Shortage" / "<sector> <- Semiconductor Demand" whose
        # beneficiaries (MU, LRCX, STX...) would otherwise never surface)
        constraint_extra = sorted(
            [t for t in landscape
             if t["is_bottleneck"] or (t["supply_constraint_count"] or 0) >= 5],
            key=lambda x: -(x["strength_now"] or 0))
        seen, uniq = set(), []
        for t in major + emerging + constraint_extra:
            if t["theme_id"] not in seen:
                seen.add(t["theme_id"]); uniq.append(t)
        products, gaps, imports = [], [], []
        supply = fetch_us_beneficiaries(cur, uniq[:30], as_of, win_start, per_theme=10)
        candidates = rank_us_candidates(supply, top_n=30)
        cross_theme_overlap = compute_cross_theme_overlap(supply)
        bear_cases = generate_bear_cases(major, emerging, candidates, args.country)
        chain_coverage = {}
        market_regime = {"regime_label": "N/A", "note": "Market regime only computed for IN"}
        theme_track_record = {}
        unmapped_peers = {}
        chain_technical_state = {}
        chain_capex_momentum = {}
        chain_continuity_alerts = []
        policy_beneficiary_screen = {}
        policy_early_pings = {}
        chain_cohort_duplicates = []
        chain_cohort_explosiveness = {}
        narrow_fresh_candidates = identify_narrow_fresh_constraints(major, emerging)
        us_note = ("US mode: constraints derived from bottleneck themes in the theme graph "
                   "(no constrained-product mapper or capacity-gap tables for US); no "
                   "technical overlay (no US price data in DB) — verify charts on "
                   "finviz/stockanalysis before acting.")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "country": args.country,
        "us_data_note": us_note,
        "window_months": args.window_months,
        "as_of_rule": "all queries filtered to <= as_of; emergence window = as_of minus window_months",
        "major_themes": major,
        "emerging_themes": emerging,
        "constrained_products": products,
        "capacity_gaps": gaps,
        "import_dependencies": imports,
        "supply_side_beneficiaries": supply,
        "ranked_candidates": candidates,
        # Improvements 1-2-3-4-5
        "evidence_dashboard": evidence_dashboard,
        "cross_theme_overlap": cross_theme_overlap,
        "bear_cases": bear_cases,
        "chain_coverage": chain_coverage,
        "theme_track_record": theme_track_record,
        "unmapped_industry_peers": unmapped_peers,
        "chain_technical_state": chain_technical_state,
        "chain_capex_momentum": chain_capex_momentum,
        "chain_continuity_alerts": chain_continuity_alerts,
        "policy_beneficiary_screen": policy_beneficiary_screen,
        "policy_early_pings": policy_early_pings,
        "chain_cohort_duplicates": chain_cohort_duplicates,
        "chain_cohort_explosiveness": chain_cohort_explosiveness,
        "narrow_fresh_candidates": narrow_fresh_candidates,
        "market_regime": market_regime,
        "backtest_calibration": BACKTEST_CALIBRATION,
        "scoring_note": (
            "fundamental_score = 0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub. "
            "composite = fundamental × freshness_multiplier "
            "(new_theme=×1.10 | fresh 1-4q=×1.12 | developing 5-8q=×1.05 | established=×1.00 | consensus 16+q=×0.93). "
            "Ranked by composite_score; fundamental_score is tiebreaker. "
            "200DMA drives position_size_guidance (full/half) NOT the score — "
            "backtest showed above-200DMA had higher hit rate but lower avg alpha (+29.8% vs +35.2%). "
            "Sector-mismatch companies excluded from ranked list (visible in supply_side_beneficiaries). "
            "Demand-side consumers receive ×0.3 conviction discount. "
            "risk_tier (HIGH/ELEVATED/NORMAL) from mg_documents — HIGH-tier candidates should be reviewed before acting. "
            "discovery_tier is a MECHANICAL SCREEN, not the final call: "
            "Tier1=top5 by composite, Tier3_Watch=ranks 6-8 fresh/new_theme + NORMAL risk, Tier3_Ignore=rest. "
            "Final categorization (Core Buy / Timing Buy / Watch / Avoid) is made by the analyst judgment layer "
            "reading the full per-stock evidence — see the stock-selector skill Step 3. "
            "market_regime: BULL_FAVORABLE/FLAT_MIXED/BEAR_CAUTION — see market_regime field for position-sizing context. "
            "backtest_calibration: 2020-2024 per-year hit_rate + avg_alpha + severe_loss_rate. "
            "Lesson 6 — severe_loss_rate (picks losing >30%) is the primary risk metric, NOT hit_rate. "
            "In 2024: hit_rate=28% AND severe_loss_rate=36% — 9/25 picks lost >30% despite high fundamental scores. "
            "In 2023: hit_rate=80% AND severe_loss_rate=0% — the right year to be fully sized."
        ),
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = args.out or os.path.join(REPORTS_DIR, f"stock_selector_{args.country}_{as_of.isoformat()}_data.json")
    with open(out_path, "w") as f:
        json.dump(report, f, default=jsonify, indent=1)
    conn.close()
    print(out_path)
    print("SUMMARY:", json.dumps({
        "major_themes": len(major), "emerging_themes": len(emerging),
        "constrained_products": len(products), "candidates": len(candidates),
        "top5_candidates": [c["ticker"] for c in candidates[:5]],
        "cross_theme_overlap_leaders": [x["ticker"] for x in cross_theme_overlap[:5]],
        "high_confidence_themes": [e["theme_name"][:35] for e in evidence_dashboard[:3]],
    }))


if __name__ == "__main__":
    main()
