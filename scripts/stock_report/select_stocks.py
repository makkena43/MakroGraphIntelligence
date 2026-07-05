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
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_report_data import connect, q, jsonify, parse_as_of, REPORTS_DIR  # noqa: E402


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
    return major, emerging


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

    gaps = q(cur, """
        SELECT sector, component, gap, gap_pct, unit, supply_chain_stage,
               theme_name, severity, target_year, as_of_date
        FROM mg_capacity_gaps
        WHERE as_of_date IS NULL OR as_of_date <= %s
        ORDER BY gap_pct DESC NULLS LAST LIMIT 15
    """, (as_of,))

    imports = q(cur, """
        SELECT sector, component, import_share, import_value_bn_usd, primary_origin,
               substitute_possible, substitution_horizon_years, risk_level
        FROM mg_import_dependencies
        WHERE as_of_date IS NULL OR as_of_date <= %s
        ORDER BY import_share DESC NULLS LAST LIMIT 15
    """, (as_of,))
    return products, gaps, imports


# ──────────────────────────────────────────────────────────────────────────
# Supply-side beneficiaries
# ──────────────────────────────────────────────────────────────────────────

SUPPLY_SIDE_TYPES = ("direct_supplier", "critical_supplier", "input_supplier")


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
    by_product = defaultdict(list)
    for r in rows:
        by_product[r["constrained_product"] or r["theme_name"]].append(r)
    result = {}
    for prod, lst in by_product.items():
        lst.sort(key=lambda r: (-(r["conviction_score"] or 0), not r["has_order_book_signals"]))
        result[prod] = lst[:per_product]
    return result


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
        a["fundamental_score"] = round(0.4 * min(a["max_relevance"] / 100.0, 1.0)
                                       + 0.3 * breadth + 0.3 * rank_score, 3)
        a["composite_score"] = a["fundamental_score"]
        a["technical"] = None  # no US price data in DB
    out.sort(key=lambda a: -a["composite_score"])
    return out[:top_n]


# ──────────────────────────────────────────────────────────────────────────
# Candidate ranking + technical overlay
# ──────────────────────────────────────────────────────────────────────────

def rank_candidates(cur, supply_by_product, as_of, top_n=25):
    agg = {}
    for prod, lst in supply_by_product.items():
        for r in lst:
            tick = (r["ticker"] or "").strip().upper()
            if not tick:
                continue
            a = agg.setdefault(tick, {
                "ticker": tick, "company": r["company"], "products": set(),
                "themes": set(), "max_conviction": 0.0, "order_book": False,
                "import_sub": False, "types": set(),
            })
            a["products"].add(prod)
            a["themes"].add(r["theme_name"])
            a["max_conviction"] = max(a["max_conviction"], float(r["conviction_score"] or 0))
            a["order_book"] = a["order_book"] or bool(r["has_order_book_signals"])
            a["import_sub"] = a["import_sub"] or bool(r["import_substitution_play"])
            a["types"].add(r["beneficiary_type"])

    candidates = list(agg.values())
    for a in candidates:
        a["products"] = sorted(a["products"])[:6]
        a["themes"] = sorted(a["themes"])[:6]
        a["types"] = sorted(a["types"])
        breadth = min(len(a["products"]), 5) / 5.0
        a["fundamental_score"] = round(
            0.45 * a["max_conviction"] + 0.25 * breadth
            + (0.20 if a["order_book"] else 0) + (0.10 if a["import_sub"] else 0), 3)
    candidates.sort(key=lambda a: -a["fundamental_score"])
    candidates = candidates[:top_n * 2]

    # light technical overlay from bhavcopy (as-of safe)
    tickers = [a["ticker"] for a in candidates]
    px = q(cur, """
        SELECT symbol, trade_date, close, high
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series IN ('EQ','BE')
          AND trade_date BETWEEN %s AND %s
        ORDER BY symbol, trade_date
    """, (tickers, as_of - timedelta(days=420), as_of))
    series = defaultdict(list)
    for r in px:
        series[r["symbol"]].append(r)
    for a in candidates:
        s = series.get(a["ticker"])
        if not s or len(s) < 40:
            a["technical"] = None
            continue
        closes = [float(r["close"]) for r in s if r["close"] is not None]
        highs = [float(r["high"]) for r in s if r["high"] is not None]
        last = closes[-1]
        hi52 = max(highs[-250:]) if highs else None
        ma200 = sum(closes[-200:]) / min(200, len(closes))
        base = closes[-126] if len(closes) >= 126 else closes[0]
        a["technical"] = {
            "last_close": round(last, 2),
            "pct_from_52w_high": round((last - hi52) / hi52 * 100, 1) if hi52 else None,
            "above_200dma": last > ma200,
            "ret_6m_pct": round((last - base) / base * 100, 1) if base else None,
        }
        tech_ok = (a["technical"]["above_200dma"] and
                   a["technical"]["pct_from_52w_high"] is not None and
                   a["technical"]["pct_from_52w_high"] > -25)
        a["composite_score"] = round(a["fundamental_score"] * (1.15 if tech_ok else 0.85), 3)
    for a in candidates:
        if "composite_score" not in a:
            a["composite_score"] = a["fundamental_score"]
    candidates.sort(key=lambda a: -a["composite_score"])
    return candidates[:top_n]


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

    major, emerging = fetch_theme_landscape(cur, as_of, args.window_months, args.country)
    if args.country == "IN":
        products, gaps, imports = fetch_constraints(cur, as_of, args.window_months)
        supply = fetch_supply_beneficiaries(cur, as_of, args.window_months)
        candidates = rank_candidates(cur, supply, as_of)
        us_note = None
    else:
        constraint_themes = [t for t in major + emerging
                             if t["is_bottleneck"] or (t["supply_constraint_count"] or 0) >= 5]
        seen, uniq = set(), []
        for t in constraint_themes:
            if t["theme_id"] not in seen:
                seen.add(t["theme_id"]); uniq.append(t)
        products, gaps, imports = [], [], []
        supply = fetch_us_beneficiaries(cur, uniq[:12], as_of, win_start)
        candidates = rank_us_candidates(supply)
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
        "scoring_note": ("fundamental_score = 0.45*max_conviction + 0.25*product_breadth "
                         "+ 0.20*order_book + 0.10*import_substitution; composite applies "
                         "±15% technical overlay (above 200DMA and within 25% of 52w high). "
                         "A screen, not advice — apply judgment."),
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
    }))


if __name__ == "__main__":
    main()
