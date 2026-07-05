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

def fetch_theme_landscape(cur, as_of, window_months):
    win_start = as_of - timedelta(days=window_months * 30)
    themes = q(cur, """
        SELECT id AS theme_id, theme_name, theme_slug, description, sectors, conviction,
               first_detected, stage, stage_label, stage_evidence, strength_score,
               momentum_score, company_count, doc_count, metadata,
               is_canonical, parent_theme_slug
        FROM mg_themes
        WHERE country='IN' AND is_active
          AND first_detected <= %s
        ORDER BY strength_score DESC NULLS LAST
        LIMIT 400
    """, (as_of,))

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


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="As-of-date stock selector extraction")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--window-months", type=int, default=12,
                    help="emergence lookback window in months (default 12)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    as_of = parse_as_of(args.as_of)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    major, emerging = fetch_theme_landscape(cur, as_of, args.window_months)
    products, gaps, imports = fetch_constraints(cur, as_of, args.window_months)
    supply = fetch_supply_beneficiaries(cur, as_of, args.window_months)
    candidates = rank_candidates(cur, supply, as_of)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
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
    out_path = args.out or os.path.join(REPORTS_DIR, f"stock_selector_{as_of.isoformat()}_data.json")
    with open(out_path, "w") as f:
        json.dump(report, f, default=jsonify, indent=1)
    conn.close()
    print(out_path)


if __name__ == "__main__":
    main()
