#!/usr/bin/env python3
"""
Stock Report Data Extractor — pulls every data section needed for the
per-stock PDF research report, strictly as-of a given date (no forward-
looking rows ever leave this script).

Usage:
    python scripts/stock_report/extract_report_data.py --symbol TITAGARH --as-of 2025-06-01
    python scripts/stock_report/extract_report_data.py --symbol "titagarh rail" --as-of 2025/06/01

Output:
    data/reports/<SYMBOL>_<ASOF>_data.json   (path printed on stdout)

Sections produced:
    company            — security_master + fundamentals_snapshot
    themes             — mg_themes x mg_theme_beneficiaries (stage, history, key dates)
    constraints        — bottleneck themes, mg_india_beneficiaries constrained products,
                         mg_capacity_gaps, mg_import_dependencies
    concalls           — last N concall/transcript docs with text excerpts + urls
    price_action       — VCP metrics, lifetime-high-volume, breakout metrics
    bulk_deals / block_deals / insider_trades
    peers              — same-industry companies with fundamentals
    sub_themes         — child themes + related constrained products with company lists
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

import psycopg2
import psycopg2.extras

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "data", "reports")

CONCALL_FILING_PATTERN = (
    "(filing_type ILIKE '%%con%%call%%' OR filing_type ILIKE '%%transcript%%' "
    "OR filing_type ILIKE '%%investor%%meet%%' OR title ILIKE '%%transcript%%' "
    "OR title ILIKE '%%earnings call%%')"
)


def jsonify(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"not serializable: {type(obj)}")


def connect():
    return psycopg2.connect(
        host=os.environ.get("MAKROGRAPH_PG_HOST", "localhost"),
        port=int(os.environ.get("MAKROGRAPH_PG_PORT", "5432")),
        dbname=os.environ.get("MAKROGRAPH_PG_DB", "makrograph"),
        user=os.environ.get("MAKROGRAPH_PG_USER", "postgres"),
        password=os.environ.get("MAKROGRAPH_PG_PASSWORD", ""),
    )


def q(cur, sql, params=None):
    cur.execute(sql, params or ())
    return [dict(r) for r in cur.fetchall()]


def parse_as_of(raw: str) -> date:
    raw = raw.strip().replace("/", "-")
    return datetime.strptime(raw, "%Y-%m-%d").date()


# ──────────────────────────────────────────────────────────────────────────
# Symbol resolution
# ──────────────────────────────────────────────────────────────────────────

def resolve_symbol(cur, user_input: str, country: str = "auto"):
    """Accept an NSE/BSE symbol, US ticker, or company-name fragment.
    Returns (master_dict, country)."""
    token = user_input.strip().upper()
    if country in ("auto", "IN"):
        rows = q(cur, "SELECT * FROM security_master WHERE upper(nse_symbol)=%s OR upper(bse_symbol)=%s", (token, token))
        if rows:
            return rows[0], "IN"
        rows = q(cur,
                 "SELECT * FROM security_master WHERE company_name ILIKE %s ORDER BY length(company_name) LIMIT 5",
                 (f"%{user_input.strip()}%",))
        if rows:
            return rows[0], "IN"
        rows = q(cur, "SELECT DISTINCT symbol FROM nse_bhavcopy_data WHERE symbol=%s LIMIT 1", (token,))
        if rows:
            return {"nse_symbol": token, "bse_symbol": None, "company_name": token,
                    "sector_nse": None, "industry_nse": None, "sector_bse": None, "industry_bse": None,
                    "isin": None}, "IN"
    if country in ("auto", "US"):
        rows = q(cur, """
            SELECT ticker, company, cik FROM mg_documents
            WHERE country='US' AND (upper(ticker)=%s OR company ILIKE %s)
            ORDER BY filed_at DESC LIMIT 1
        """, (token, f"%{user_input.strip()}%"))
        if rows:
            r = rows[0]
            return {"nse_symbol": r["ticker"].upper(), "bse_symbol": None,
                    "company_name": r["company"], "cik": r["cik"],
                    "sector_nse": None, "industry_nse": None, "sector_bse": None,
                    "industry_bse": None, "isin": None}, "US"
    return None, None


# ──────────────────────────────────────────────────────────────────────────
# Company + fundamentals
# ──────────────────────────────────────────────────────────────────────────

def fetch_company(cur, master, as_of):
    symbol = master["nse_symbol"]
    fund = q(cur, """
        SELECT company_name, sector, industry, market_cap, pe_ratio, pb_ratio, book_value,
               dividend_yield, roce, roe, eps, debt_to_equity, sales_growth_3y, profit_growth_3y,
               promoter_holding, fii_holding, dii_holding, pledge_percentage, screener_url,
               peer_comparison_json, quarterly_data_json, last_updated
        FROM fundamentals_snapshot
        WHERE upper(nse_symbol)=%s OR upper(bse_symbol)=%s
        ORDER BY last_updated DESC LIMIT 1
    """, (symbol, symbol))
    fund_row = fund[0] if fund else {}
    peer_json = fund_row.pop("peer_comparison_json", None)
    quarterly_json = fund_row.pop("quarterly_data_json", None)
    try:
        peers_snapshot = json.loads(peer_json) if peer_json else None
    except (ValueError, TypeError):
        peers_snapshot = None
    try:
        quarterly = json.loads(quarterly_json) if quarterly_json else None
    except (ValueError, TypeError):
        quarterly = None
    return {
        "symbol": symbol,
        "bse_symbol": master.get("bse_symbol"),
        "isin": master.get("isin"),
        "company_name": master.get("company_name") or fund_row.get("company_name"),
        "sector": master.get("sector_nse") or master.get("sector_bse") or fund_row.get("sector"),
        "industry": master.get("industry_nse") or master.get("industry_bse") or fund_row.get("industry"),
        "fundamentals_snapshot": fund_row or None,
        "fundamentals_note": ("fundamentals_snapshot is a CURRENT scrape (see last_updated), "
                              "not an as-of-date value — treat ratios as indicative only"),
        "quarterly_data": quarterly,
        "peer_comparison_snapshot": peers_snapshot,
    }


# ──────────────────────────────────────────────────────────────────────────
# Themes & constraints
# ──────────────────────────────────────────────────────────────────────────

def _stage_history_upto(meta, as_of):
    hist = (meta or {}).get("stage_history") or []
    out = []
    for h in hist:
        d = h.get("date") or h.get("as_of") or h.get("snapshot_date")
        try:
            if d and datetime.strptime(str(d)[:10], "%Y-%m-%d").date() <= as_of:
                out.append(h)
        except ValueError:
            out.append(h)
    return out


def fetch_themes(cur, symbol, company_name, as_of, country="IN"):
    rows = q(cur, """
        SELECT t.id AS theme_id, t.theme_name, t.theme_slug, t.description, t.sectors,
               t.conviction, t.first_detected, t.last_updated, t.stage, t.stage_label,
               t.stage_evidence, t.hypothesis_text, t.strength_score, t.momentum_score,
               t.parent_theme_slug, t.metadata,
               b.beneficiary_type, b.relevance_score, b.signal_count, b.first_seen_at,
               b.last_seen_at, b.rank_in_theme, b.reasoning, b.company_role, b.capex_signals,
               b.quarterly_mentions
        FROM mg_theme_beneficiaries b
        JOIN mg_themes t ON t.id = b.theme_id
        WHERE t.country=%s AND t.is_active
          AND upper(b.ticker)=%s
          AND t.first_detected <= %s
          AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
        ORDER BY b.relevance_score DESC NULLS LAST
        LIMIT 25
    """, (country, symbol, as_of, as_of))

    themes, constraints = [], []
    for r in rows:
        meta = r.pop("metadata") or {}
        r["stage_history_asof"] = _stage_history_upto(meta, as_of)
        r["supply_constraint_count"] = meta.get("supply_constraint_count")
        r["is_bottleneck"] = meta.get("is_bottleneck")
        r["bottleneck_theme_name"] = meta.get("bottleneck_theme_name")
        r["tension_score"] = meta.get("tension_score")
        r["progression_evidence"] = meta.get("progression_evidence")
        r["confirmed_quarters"] = meta.get("confirmed_quarters")
        # theme momentum trajectory as-of the report date
        snaps = q(cur, """
            SELECT snapshot_date, strength_score, momentum_score, doc_count, company_count
            FROM mg_theme_snapshots
            WHERE theme_id=%s AND snapshot_date <= %s
            ORDER BY snapshot_date DESC LIMIT 8
        """, (r["theme_id"], as_of))
        r["snapshots_asof"] = snaps
        if r.get("is_bottleneck") or (r.get("supply_constraint_count") or 0) > 0:
            constraints.append(r)
        themes.append(r)
    return themes, constraints


def fetch_india_beneficiary_view(cur, symbol, company_name, as_of):
    return q(cur, """
        SELECT company, ticker, theme_name, constrained_product, supply_chain_node,
               supply_chain_stage, beneficiary_type, conviction_score, rationale,
               signal_count, has_order_book_signals, import_substitution_play, as_of_date
        FROM mg_india_beneficiaries
        WHERE (upper(ticker)=%s OR company ILIKE %s)
          AND (as_of_date IS NULL OR as_of_date <= %s)
        ORDER BY conviction_score DESC NULLS LAST
        LIMIT 20
    """, (symbol, f"%{company_name}%", as_of))


def fetch_capacity_and_imports(cur, theme_names, sector, as_of):
    gaps, imports = [], []
    if theme_names:
        gaps = q(cur, """
            SELECT sector, component, required_quantity, domestic_capacity, gap, gap_pct, unit,
                   supply_chain_stage, theme_name, severity, target_year, as_of_date
            FROM mg_capacity_gaps
            WHERE theme_name = ANY(%s) AND (as_of_date IS NULL OR as_of_date <= %s)
            ORDER BY gap_pct DESC NULLS LAST LIMIT 15
        """, (theme_names, as_of))
    if sector:
        imports = q(cur, """
            SELECT sector, component, import_share, import_value_bn_usd, primary_origin,
                   substitute_possible, substitution_horizon_years, risk_level, as_of_date
            FROM mg_import_dependencies
            WHERE sector ILIKE %s AND (as_of_date IS NULL OR as_of_date <= %s)
            ORDER BY import_share DESC NULLS LAST LIMIT 15
        """, (f"%{sector}%", as_of))
    return gaps, imports


# ──────────────────────────────────────────────────────────────────────────
# Concalls
# ──────────────────────────────────────────────────────────────────────────

def fetch_concalls(cur, symbol, company_name, as_of, limit=20, excerpt_chars=7000, country="IN"):
    # US has no exchange concall filings — EDGAR 10-K/10-Q/8-K carry the management
    # commentary (MD&A, earnings 8-Ks) and serve the same role in the report
    doc_filter = CONCALL_FILING_PATTERN if country == "IN" else \
        "(filing_type IN ('10-K','10-Q','8-K'))"
    rows = q(cur, f"""
        SELECT id, source_name, filing_type, fiscal_period, filed_at, title, url,
               sentiment_score, nlp_summary, word_count,
               CASE WHEN raw_text IS NOT NULL THEN length(raw_text) ELSE 0 END AS text_len,
               left(raw_text, {int(excerpt_chars)}) AS text_excerpt
        FROM mg_documents
        WHERE country=%s
          AND (upper(ticker)=%s OR company ILIKE %s)
          AND {doc_filter}
          AND filed_at <= %s
        ORDER BY filed_at DESC
        LIMIT %s
    """, (country, symbol, f"%{company_name}%", as_of, limit))
    if country == "US":
        for r in rows:
            r["doc_kind"] = "sec_filing_" + (r.get("filing_type") or "")
        return rows
    for r in rows:
        # transcripts/recordings > schedule intimations — tag so the model can prioritise
        blob = f"{r.get('filing_type','')} {r.get('title','')}".lower()
        if "transcript" in blob:
            r["doc_kind"] = "transcript"
        elif "recording" in blob or "audio" in blob:
            r["doc_kind"] = "recording_link"
        elif "presentation" in blob:
            r["doc_kind"] = "presentation"
        elif "schedule" in blob or "intimation" in blob:
            r["doc_kind"] = "schedule_notice"
        else:
            r["doc_kind"] = "concall_update"
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Price action
# ──────────────────────────────────────────────────────────────────────────

def fetch_price_series(cur, symbol, as_of, days=800):
    start = as_of - timedelta(days=days * 1.6)
    return q(cur, """
        SELECT trade_date, open, high, low, close, tottrdqty AS volume,
               delivery_pct, totaltrades
        FROM nse_bhavcopy_data
        WHERE symbol=%s AND series IN ('EQ','BE') AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date
    """, (symbol, start, as_of))


def _sma(vals, n, idx):
    lo = max(0, idx - n + 1)
    window = [v for v in vals[lo:idx + 1] if v is not None]
    return sum(window) / len(window) if window else None


def _find_swings(highs, lows, closes, dates, lookback=5):
    """Simple swing detector: a swing high/low is the extreme of +/- lookback bars."""
    swings = []
    n = len(closes)
    for i in range(lookback, n - lookback):
        win_h = highs[i - lookback:i + lookback + 1]
        win_l = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(win_h):
            swings.append(("H", i, highs[i], dates[i]))
        elif lows[i] == min(win_l):
            swings.append(("L", i, lows[i], dates[i]))
    # collapse consecutive same-type swings keeping the more extreme one
    cleaned = []
    for s in swings:
        if cleaned and cleaned[-1][0] == s[0]:
            if (s[0] == "H" and s[2] >= cleaned[-1][2]) or (s[0] == "L" and s[2] <= cleaned[-1][2]):
                cleaned[-1] = s
        else:
            cleaned.append(s)
    return cleaned


def compute_price_action(series, as_of):
    if len(series) < 60:
        return {"error": f"only {len(series)} trading days of price data up to {as_of} — too thin for pattern analysis"}

    dates = [r["trade_date"] for r in series]
    closes = [float(r["close"]) if r["close"] is not None else None for r in series]
    highs = [float(r["high"]) if r["high"] is not None else 0.0 for r in series]
    lows = [float(r["low"]) if r["low"] is not None else 0.0 for r in series]
    vols = [float(r["volume"]) if r["volume"] is not None else 0.0 for r in series]

    last_i = len(series) - 1
    last_close = closes[last_i]
    last_date = dates[last_i]

    # 52-week window
    one_yr_ago = as_of - timedelta(days=365)
    yr_idx = [i for i, d in enumerate(dates) if d >= one_yr_ago]
    hi52 = max(highs[i] for i in yr_idx)
    hi52_date = dates[max(yr_idx, key=lambda i: highs[i])]
    lo52 = min(lows[i] for i in yr_idx if lows[i] > 0)
    pct_from_hi52 = (last_close - hi52) / hi52 * 100.0

    # lifetime (within loaded history) highest-volume day
    ltv_i = max(range(len(series)), key=lambda i: vols[i])
    day_move_pct = None
    if ltv_i > 0 and closes[ltv_i - 1]:
        day_move_pct = (closes[ltv_i] - closes[ltv_i - 1]) / closes[ltv_i - 1] * 100.0
    lifetime_high_volume = {
        "date": dates[ltv_i],
        "volume": vols[ltv_i],
        "close": closes[ltv_i],
        "day_move_pct": round(day_move_pct, 2) if day_move_pct is not None else None,
        "days_ago": (as_of - dates[ltv_i]).days,
        "note": "highest-volume session in loaded history (from 2019-06 onwards)",
        "price_change_since_pct": round((last_close - closes[ltv_i]) / closes[ltv_i] * 100.0, 2),
    }

    # volume trend
    v10 = _sma(vols, 10, last_i)
    v50 = _sma(vols, 50, last_i)
    vol_dry_ratio = round(v10 / v50, 2) if v50 else None

    # moving averages
    ma50 = _sma(closes, 50, last_i)
    ma200 = _sma(closes, 200, last_i)

    # ── VCP: contraction sequence from swing highs/lows over last ~9 months ──
    nine_mo_start = as_of - timedelta(days=270)
    sub_idx = [i for i, d in enumerate(dates) if d >= nine_mo_start]
    off = sub_idx[0] if sub_idx else 0
    swings = _find_swings(highs[off:], lows[off:], closes[off:], dates[off:], lookback=5)
    contractions = []
    cur_high = None
    for s in swings:
        if s[0] == "H":
            cur_high = s
        elif s[0] == "L" and cur_high is not None:
            depth = (cur_high[2] - s[2]) / cur_high[2] * 100.0
            if depth > 1.0:
                contractions.append({
                    "high_date": cur_high[3], "high": round(cur_high[2], 2),
                    "low_date": s[3], "low": round(s[2], 2),
                    "depth_pct": round(depth, 2),
                })
            cur_high = None
    recent_contr = contractions[-4:]
    depths = [c["depth_pct"] for c in recent_contr]
    tightening = all(depths[i] > depths[i + 1] for i in range(len(depths) - 1)) if len(depths) >= 2 else False

    # tightness of the last 10 sessions
    rng10 = (max(highs[last_i - 9:last_i + 1]) - min(lows[last_i - 9:last_i + 1])) / last_close * 100.0

    vcp = {
        "contractions_last_9mo": recent_contr,
        "contraction_depths_pct": depths,
        "successive_tightening": tightening,
        "volume_dry_up_ratio_10d_vs_50d": vol_dry_ratio,
        "last_10d_range_pct": round(rng10, 2),
        "pct_below_52w_high": round(pct_from_hi52, 2),
        "above_50dma": bool(ma50 and last_close > ma50),
        "above_200dma": bool(ma200 and last_close > ma200),
        "heuristic_verdict": bool(
            tightening and len(depths) >= 2
            and (vol_dry_ratio is not None and vol_dry_ratio < 0.85)
            and pct_from_hi52 > -20.0
            and (ma200 is None or last_close > ma200)
        ),
        "note": ("heuristic_verdict True = tightening contractions + volume dry-up + within 20% of 52w high "
                 "+ above 200DMA. Treat as a screen, apply judgment on the numbers."),
    }

    # ── Breakout formation ──
    # pivot = highest high of the base window (last 60 sessions, excluding final 5)
    base_hi = max(highs[max(0, last_i - 60):last_i - 4]) if last_i > 10 else hi52
    dist_to_pivot = (last_close - base_hi) / base_hi * 100.0
    last_vol_vs_50d = round(vols[last_i] / v50, 2) if v50 else None
    recent_up_thrust = None
    if v50:
        for i in range(last_i, max(last_i - 5, 0), -1):
            if closes[i] and closes[i - 1] and closes[i] > closes[i - 1] and vols[i] > 1.5 * v50:
                recent_up_thrust = {"date": dates[i], "volume_x_50d": round(vols[i] / v50, 2),
                                    "close_change_pct": round((closes[i] - closes[i - 1]) / closes[i - 1] * 100, 2)}
                break
    breakout = {
        "pivot_price_60d_base": round(base_hi, 2),
        "distance_to_pivot_pct": round(dist_to_pivot, 2),
        "broke_out": dist_to_pivot > 0,
        "last_volume_vs_50d_avg": last_vol_vs_50d,
        "recent_high_volume_up_day_last5": recent_up_thrust,
        "near_pivot_watch": bool(-5.0 <= dist_to_pivot <= 0),
    }

    # 1/3/6/12-month returns
    def ret_since(days_back):
        target = as_of - timedelta(days=days_back)
        prior = [i for i, d in enumerate(dates) if d <= target]
        if not prior:
            return None
        c0 = closes[prior[-1]]
        return round((last_close - c0) / c0 * 100.0, 2) if c0 else None

    # institutional accumulation days (last 6 months): delivered quantity spikes to
    # >2.5x its own 50d average on an up close — normalizes across high/low-churn stocks
    six_mo = as_of - timedelta(days=182)
    delivered = [vols[i] * float(series[i]["delivery_pct"]) / 100.0
                 if series[i].get("delivery_pct") is not None else None
                 for i in range(len(series))]
    accumulation_days = []
    for i in range(50, len(series)):
        if dates[i] < six_mo or delivered[i] is None:
            continue
        d50 = _sma([x for x in delivered], 50, i - 1)
        up = closes[i] is not None and closes[i - 1] and closes[i] > closes[i - 1]
        if d50 and delivered[i] > 2.5 * d50 and up:
            accumulation_days.append({
                "date": dates[i],
                "delivered_qty_x_50d": round(delivered[i] / d50, 1),
                "delivery_pct": float(series[i]["delivery_pct"]),
                "close_change_pct": round((closes[i] - closes[i - 1]) / closes[i - 1] * 100, 2),
            })
    accumulation_days = accumulation_days[-8:]

    return {
        "as_of_trading_date": last_date,
        "last_close": last_close,
        "accumulation_days_6mo": accumulation_days,
        "week52_high": round(hi52, 2), "week52_high_date": hi52_date,
        "week52_low": round(lo52, 2),
        "pct_from_52w_high": round(pct_from_hi52, 2),
        "ma50": round(ma50, 2) if ma50 else None,
        "ma200": round(ma200, 2) if ma200 else None,
        "returns_pct": {"1m": ret_since(30), "3m": ret_since(91), "6m": ret_since(182), "12m": ret_since(365)},
        "avg_volume_50d": round(v50, 0) if v50 else None,
        "avg_delivery_pct_20d": round(
            sum(float(r["delivery_pct"]) for r in series[-20:] if r["delivery_pct"] is not None)
            / max(1, len([r for r in series[-20:] if r["delivery_pct"] is not None])), 2),
        "lifetime_high_volume": lifetime_high_volume,
        "vcp": vcp,
        "breakout": breakout,
    }


# ──────────────────────────────────────────────────────────────────────────
# Deals & insider activity
# ──────────────────────────────────────────────────────────────────────────

def fetch_deals(cur, table, symbol, as_of, months=18):
    start = as_of - timedelta(days=months * 30)
    rows = q(cur, f"""
        SELECT trade_date, client_name, buy_sell, quantity, trade_price, remarks
        FROM {table}
        WHERE upper(symbol)=%s AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date DESC LIMIT 60
    """, (symbol, start, as_of))
    agg = {}
    for r in rows:
        key = (r["client_name"] or "?").strip().upper()
        a = agg.setdefault(key, {"client": r["client_name"], "buy_qty": 0, "sell_qty": 0, "n_deals": 0})
        qty = float(r["quantity"] or 0)
        if (r["buy_sell"] or "").upper().startswith("B"):
            a["buy_qty"] += qty
        else:
            a["sell_qty"] += qty
        a["n_deals"] += 1
    top_clients = sorted(agg.values(), key=lambda a: a["buy_qty"] + a["sell_qty"], reverse=True)[:12]
    for a in top_clients:
        a["net_qty"] = a["buy_qty"] - a["sell_qty"]
    return {"window_months": months, "deal_count": len(rows), "deals": rows[:30], "by_client": top_clients}


def fetch_insider(cur, symbol, as_of, months=24):
    start = as_of - timedelta(days=months * 30)
    rows = q(cur, """
        SELECT trade_date, insider_name, designation, transaction_type, quantity,
               value_traded, post_transaction_percentage
        FROM nse_insider_trades
        WHERE upper(symbol)=%s
          AND trade_date BETWEEN %s AND %s          -- also guards corrupt 1923/3034 dates
        ORDER BY trade_date DESC LIMIT 80
    """, (symbol, start, as_of))
    buys = [r for r in rows if "acq" in (r["transaction_type"] or "").lower() or "buy" in (r["transaction_type"] or "").lower()]
    sells = [r for r in rows if "disp" in (r["transaction_type"] or "").lower() or "sell" in (r["transaction_type"] or "").lower()]
    return {
        "window_months": months,
        "total_transactions": len(rows),
        "buy_value_total": sum(float(r["value_traded"] or 0) for r in buys),
        "sell_value_total": sum(float(r["value_traded"] or 0) for r in sells),
        "transactions": rows[:40],
    }


# ──────────────────────────────────────────────────────────────────────────
# Quarterly financials, shareholding, corporate & policy events
# ──────────────────────────────────────────────────────────────────────────

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _period_end(label):
    """'Mar 2025' -> date(2025,3,31) (approx month end)."""
    try:
        mon, yr = label.strip().split()
        m = MONTHS[mon.lower()[:3]]
        y = int(yr)
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        return nxt - timedelta(days=1)
    except (ValueError, KeyError):
        return None


def _clean_keys(d):
    return {re.sub(r"[\xa0+]+$", "", k).strip(): v for k, v in d.items()}


def fetch_quarterly_financials(company, as_of, publish_lag_days=45):
    """Quarters from the screener scrape whose results were public before as_of.
    A quarter ending E is included only if E + publish_lag_days <= as_of."""
    qd = company.get("quarterly_data") or {}
    rows = qd.get("quarters_data") or []
    out = []
    for r in rows:
        r = _clean_keys(r)
        pe = _period_end(r.get("quarter", ""))
        if pe and pe + timedelta(days=publish_lag_days) <= as_of:
            out.append({"quarter": r.get("quarter"), "sales": r.get("Sales"),
                        "opm_pct": r.get("OPM %"), "net_profit": r.get("Net Profit"),
                        "eps": r.get("EPS in Rs")})
    return {"note": f"quarters included only if quarter-end + {publish_lag_days}d <= as-of (results public)",
            "quarters": out[-10:]}


def fetch_shareholding_trend(cur, symbol, company, as_of, publish_lag_days=45):
    sh_rows = []
    row = q(cur, "SELECT shareholding_json FROM fundamentals_snapshot WHERE upper(nse_symbol)=%s LIMIT 1", (symbol,))
    if row and row[0].get("shareholding_json"):
        try:
            data = json.loads(row[0]["shareholding_json"]).get("periods_data") or []
        except (ValueError, TypeError):
            data = []
        for r in data:
            r = _clean_keys(r)
            pe = _period_end(r.get("period", ""))
            if pe and pe + timedelta(days=publish_lag_days) <= as_of:
                sh_rows.append({"period": r.get("period"), "promoters": r.get("Promoters"),
                                "fii": r.get("FIIs"), "dii": r.get("DIIs"),
                                "public": r.get("Public"),
                                "n_shareholders": r.get("No. of Shareholders")})
    return {"note": "quarterly shareholding pattern (published historically — as-of safe)",
            "periods": sh_rows[-8:]}


CORP_EVENT_TYPES = (
    "press release", "bagging", "awarding", "acquisition", "amalgamation", "merger",
    "scheme of arrangement", "capacity addition", "buyback", "rights issue",
    "qualified institution", "open offer", "demerger", "agreements", "memorandum",
    "commencement of commercial", "outcome of board meeting",
)


def fetch_corporate_events(cur, symbol, company_name, as_of, months=18, country="IN"):
    start = as_of - timedelta(days=months * 30)
    like = " OR ".join(f"filing_type ILIKE '%%{t}%%'" for t in CORP_EVENT_TYPES)
    if country == "US":
        like = "filing_type = '8-K'"   # material corporate events on EDGAR
    rows = q(cur, f"""
        SELECT filed_at, filing_type, left(title, 220) AS title, url,
               left(nlp_summary, 300) AS summary
        FROM mg_documents
        WHERE country=%s AND (upper(ticker)=%s OR company ILIKE %s)
          AND filed_at BETWEEN %s AND %s AND ({like})
        ORDER BY filed_at DESC LIMIT 25
    """, (country, symbol, f"%{company_name}%", start, as_of))
    return {"window_months": months, "events": rows}


def fetch_policy_events(cur, themes, sector, as_of, months=24, country="IN"):
    """Policy events (IN: PIB/SEBI/ministries; US: Congress/Federal Register)
    touching the company's theme keywords, before as_of."""
    tokens = set()
    for t in themes[:5]:
        for w in re.split(r"[^a-zA-Z]+", t["theme_name"].lower()):
            if len(w) > 4 and w not in ("demand", "supply", "tension", "severe",
                                        "constraint", "infrastructure", "theme"):
                tokens.add(w)
    if sector:
        tokens.add(sector.lower())
    if not tokens:
        return []
    conds, params = [], []
    for tok in list(tokens)[:8]:
        conds.append("(title ILIKE %s OR sectors_affected::text ILIKE %s OR keywords::text ILIKE %s)")
        params.extend([f"%{tok}%"] * 3)
    # many India policy rows are undated scrapes — fall back through the date fields;
    # fetched_at is when the pipeline first saw it (safe upper bound for as-of checks)
    start = as_of - timedelta(days=months * 30)
    params = [country, as_of, start] + params
    return q(cur, f"""
        SELECT COALESCE(introduced_date, enacted_date, effective_date, fetched_at::date) AS event_date,
               (introduced_date IS NULL AND enacted_date IS NULL AND effective_date IS NULL) AS date_is_fetch_date,
               policy_type, source, left(title, 200) AS title, raw_url,
               impact_direction, impact_magnitude, status
        FROM mg_policy_events
        WHERE country=%s
          AND COALESCE(introduced_date, enacted_date, effective_date, fetched_at::date) <= %s
          AND (COALESCE(introduced_date, enacted_date, effective_date) IS NULL
               OR COALESCE(introduced_date, enacted_date, effective_date) >= %s)
          AND ({' OR '.join(conds)})
        ORDER BY event_date DESC LIMIT 12
    """, params)


CONSTRAINT_SIGNAL_TYPES = (
    "supply_shortage", "supply_constraint", "capacity_constraint", "demand_surge",
    "capex_increase", "tender_pipeline", "order_win", "regulatory_tailwind",
    "policy_support", "localization_opportunity", "import_dependency", "price_increase",
)


def fetch_constraint_evidence(cur, symbol, as_of, limit=15):
    """Dated signal quotes (with source-document links) explaining WHY the
    demand/supply tension exists for this company — the authenticity trail."""
    rows = q(cur, """
        SELECT s.signal_type, s.direction, s.filed_at, s.confidence,
               left(regexp_replace(s.context_text, '\\s+', ' ', 'g'), 240) AS evidence,
               left(d.title, 150) AS doc_title, d.url AS doc_url, d.filing_type
        FROM mg_signals s
        JOIN mg_documents d ON d.id = s.document_id
        WHERE upper(d.ticker) = %s AND s.filed_at <= %s
          AND s.signal_type = ANY(%s)
        ORDER BY s.filed_at DESC, s.confidence DESC
        LIMIT %s
    """, (symbol, as_of, list(CONSTRAINT_SIGNAL_TYPES), limit))
    counts = q(cur, """
        SELECT s.signal_type, count(*) AS n, min(s.filed_at) AS first_seen, max(s.filed_at) AS last_seen
        FROM mg_signals s JOIN mg_documents d ON d.id = s.document_id
        WHERE upper(d.ticker) = %s AND s.filed_at <= %s
        GROUP BY s.signal_type ORDER BY n DESC LIMIT 15
    """, (symbol, as_of))
    return {"signal_counts": counts, "evidence_quotes": rows,
            "note": ("evidence_quotes are verbatim extracts from filings with source links — "
                     "use for constraint root-cause + authenticity assessment")}


def fetch_key_links(cur, symbol, bse_symbol, as_of, country="IN", cik=None):
    """Public web links: company pages + latest filings with document URLs."""
    if country == "US":
        links = {
            "sec_edgar_filings": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik or symbol}&type=&dateb=&owner=include&count=40",
            "edgar_full_text_search": f"https://efts.sec.gov/LATEST/search-index?q=%22{symbol}%22",
            "yahoo_finance": f"https://finance.yahoo.com/quote/{symbol}",
            "stockanalysis": f"https://stockanalysis.com/stocks/{symbol.lower()}/",
            "finviz": f"https://finviz.com/quote.ashx?t={symbol}",
            "openinsider": f"http://openinsider.com/screener?s={symbol}",
        }
        filings = q(cur, """
            SELECT filed_at, filing_type, left(title, 130) AS title, url
            FROM mg_documents
            WHERE country='US' AND upper(ticker)=%s AND filed_at <= %s
              AND filing_type IN ('10-K','10-Q')
            ORDER BY filed_at DESC LIMIT 6
        """, (symbol, as_of))
        return {"company_pages": links, "filing_documents": filings,
                "note": "US links: EDGAR archive links are permanent; aggregator pages show current data"}
    links = {
        "screener": f"https://www.screener.in/company/{symbol}/consolidated/",
        "nse_quote": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}",
        "nse_announcements": f"https://www.nseindia.com/companies-listing/corporate-filings-announcements?symbol={symbol}",
        "nse_insider_trading": f"https://www.nseindia.com/companies-listing/corporate-filings-insider-trading?symbol={symbol}",
        "trendlyne": f"https://trendlyne.com/equity/{symbol}/",
        "tijorifinance": f"https://www.tijorifinance.com/company/{symbol.lower()}/",
    }
    if bse_symbol:
        links["bse_search"] = f"https://www.bseindia.com/stock-share-price/x/x/{bse_symbol}/"
    row = q(cur, "SELECT screener_url FROM fundamentals_snapshot WHERE upper(nse_symbol)=%s LIMIT 1", (symbol,))
    if row and row[0].get("screener_url"):
        links["screener"] = row[0]["screener_url"]
    presentations = q(cur, """
        SELECT filed_at, filing_type, left(title, 130) AS title, url
        FROM mg_documents
        WHERE country='IN' AND upper(ticker)=%s AND filed_at <= %s
          AND (filing_type ILIKE '%%presentation%%' OR filing_type ILIKE '%%annual report%%')
        ORDER BY filed_at DESC LIMIT 5
    """, (symbol, as_of))
    return {"company_pages": links, "filing_documents": presentations,
            "note": "concall links are in the concalls section (url per doc); policy links in policy_events.raw_url"}


# ──────────────────────────────────────────────────────────────────────────
# Peers & sub-themes
# ──────────────────────────────────────────────────────────────────────────

def fetch_peers(cur, master, symbol, themes, as_of):
    industry = master.get("industry_nse") or master.get("industry_bse")
    theme_peers = []
    theme_ids = [t["theme_id"] for t in themes[:3]]
    if theme_ids:
        theme_peers = q(cur, """
            SELECT DISTINCT ON (b.ticker) b.ticker, b.company_name, b.relevance_score,
                   b.rank_in_theme, t.theme_name
            FROM mg_theme_beneficiaries b
            JOIN mg_themes t ON t.id = b.theme_id
            WHERE b.theme_id = ANY(%s) AND b.ticker IS NOT NULL AND upper(b.ticker) <> %s
              AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
            ORDER BY b.ticker, b.rank_in_theme ASC NULLS LAST
        """, (theme_ids, symbol, as_of))
        theme_peers = sorted(theme_peers,
                             key=lambda r: (r["rank_in_theme"] if r["rank_in_theme"] is not None else 9999,
                                            -(r["relevance_score"] or 0)))[:12]
    if not industry:
        return {"industry": None, "peers": [], "theme_peers": theme_peers}
    rows = q(cur, """
        SELECT sm.nse_symbol, sm.company_name, sm.industry_nse,
               f.market_cap, f.pe_ratio, f.roce, f.roe, f.debt_to_equity,
               f.sales_growth_3y, f.profit_growth_3y, f.promoter_holding
        FROM security_master sm
        LEFT JOIN fundamentals_snapshot f
               ON upper(f.nse_symbol) = upper(sm.nse_symbol)
        WHERE sm.industry_nse = %s AND upper(sm.nse_symbol) <> %s
        ORDER BY f.market_cap DESC NULLS LAST
        LIMIT 12
    """, (industry, symbol))
    return {"industry": industry, "peers": rows, "theme_peers": theme_peers,
            "note": ("peer fundamentals are current-snapshot values, not as-of-date; "
                     "industry grouping is broad (NSE industry) — pick closest business "
                     "comparables by judgment; theme_peers share the same detected themes")}


def fetch_sub_themes(cur, themes, symbol, as_of, country="IN"):
    """Child themes of the company's themes + sibling companies on the same constrained products."""
    slugs = [t["theme_slug"] for t in themes]
    names = [t["theme_name"] for t in themes]
    out = {"child_themes": [], "constrained_product_companies": []}
    if slugs:
        children = q(cur, """
            SELECT t.theme_name, t.theme_slug, t.parent_theme_slug, t.first_detected,
                   t.stage_label, t.conviction, t.strength_score
            FROM mg_themes t
            WHERE t.country=%s AND t.is_active AND t.parent_theme_slug = ANY(%s)
              AND t.first_detected <= %s
            ORDER BY t.strength_score DESC LIMIT 12
        """, (country, slugs, as_of))
        for c in children:
            c["top_companies"] = [r["v"] for r in q(cur, """
                SELECT COALESCE(b.ticker, b.company_name) AS v
                FROM mg_theme_beneficiaries b
                JOIN mg_themes t ON t.id=b.theme_id
                WHERE t.theme_slug=%s AND t.country=%s
                  AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
                ORDER BY b.relevance_score DESC NULLS LAST LIMIT 8
            """, (c["theme_slug"], country, as_of))]
        out["child_themes"] = children
    if names and country == "IN":
        rows = q(cur, """
            SELECT constrained_product, theme_name,
                   array_agg(DISTINCT COALESCE(ticker, company)) AS companies
            FROM mg_india_beneficiaries
            WHERE theme_name = ANY(%s) AND constrained_product IS NOT NULL
              AND (as_of_date IS NULL OR as_of_date <= %s)
            GROUP BY constrained_product, theme_name
            ORDER BY count(*) DESC LIMIT 12
        """, (names, as_of))
        for r in rows:
            r["companies"] = [c for c in (r["companies"] or [])][:10]
        out["constrained_product_companies"] = rows
    return out


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Extract as-of-date stock report data to JSON")
    ap.add_argument("--symbol", required=True, help="NSE/BSE symbol, US ticker, or company name fragment")
    ap.add_argument("--as-of", required=True, help="Report date YYYY-MM-DD (also accepts YYYY/MM/DD)")
    ap.add_argument("--country", default="auto", choices=["auto", "IN", "US"],
                    help="market (default: auto-detect — India first, then US)")
    ap.add_argument("--out", default=None, help="Output JSON path (default: data/reports/...)")
    args = ap.parse_args()

    as_of = parse_as_of(args.as_of)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    master, country = resolve_symbol(cur, args.symbol, args.country)
    if not master:
        print(json.dumps({"error": f"symbol/company not found in IN or US data: {args.symbol}"}))
        sys.exit(2)
    symbol = master["nse_symbol"].upper()
    company_name = (master.get("company_name") or symbol).split(" LIMITED")[0].split(" Limited")[0].strip()

    company = fetch_company(cur, master, as_of)
    themes, constraint_themes = fetch_themes(cur, symbol, company_name, as_of, country)
    concalls = fetch_concalls(cur, symbol, company_name, as_of, country=country)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "country": country,
        "as_of_rule": "every query is filtered to dates <= as_of_date; fundamentals/peers snapshots are current-day and flagged",
        "company": company,
        "themes": themes,
        "constraint_themes": constraint_themes,
        "concalls": concalls,
        "peers": fetch_peers(cur, master, symbol, themes, as_of),
        "sub_themes": fetch_sub_themes(cur, themes, symbol, as_of, country),
        "corporate_events": fetch_corporate_events(cur, symbol, company_name, as_of, country=country),
        "policy_events": fetch_policy_events(cur, themes, company.get("sector") or company.get("industry"),
                                             as_of, country=country),
        "constraint_evidence": fetch_constraint_evidence(cur, symbol, as_of),
        "key_links": fetch_key_links(cur, symbol, master.get("bse_symbol"), as_of,
                                     country=country, cik=master.get("cik")),
    }

    if country == "IN":
        india_benef = fetch_india_beneficiary_view(cur, symbol, company_name, as_of)
        theme_names = list({t["theme_name"] for t in themes}
                           | {b["theme_name"] for b in india_benef if b.get("theme_name")})
        gaps, imports = fetch_capacity_and_imports(cur, theme_names, company.get("sector"), as_of)
        series = fetch_price_series(cur, symbol, as_of)
        report.update({
            "india_beneficiary_mappings": india_benef,
            "capacity_gaps": gaps,
            "import_dependencies": imports,
            "price_action": compute_price_action(series, as_of) if series else {"error": "no NSE price data for symbol"},
            "bulk_deals": fetch_deals(cur, "nse_bulk_deals", symbol, as_of),
            "block_deals": fetch_deals(cur, "nse_block_deals", symbol, as_of),
            "insider_trades": fetch_insider(cur, symbol, as_of),
            "quarterly_financials": fetch_quarterly_financials(company, as_of),
            "shareholding_trend": fetch_shareholding_trend(cur, symbol, company, as_of),
        })
    else:
        report["us_data_note"] = (
            "US coverage: EDGAR filings (10-K/10-Q/8-K), themes/constraints, policy events, "
            "theme peers and links. NOT in DB for US: price data, bulk/block deals, insider "
            "trades, fundamentals/shareholding — omit those report sections, state why in one "
            "line, and point the reader to the finviz/openinsider/stockanalysis links instead.")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = args.out or os.path.join(REPORTS_DIR, f"{symbol}_{as_of.isoformat()}_data.json")
    with open(out_path, "w") as f:
        json.dump(report, f, default=jsonify, indent=1)
    conn.close()
    print(out_path)


if __name__ == "__main__":
    main()
