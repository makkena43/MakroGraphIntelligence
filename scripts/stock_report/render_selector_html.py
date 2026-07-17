"""
Render selector JSON → standalone HTML report.
Usage:
  python scripts/stock_report/render_selector_html.py data/reports/stock_selector_IN_2025-12-31_data.json
  python scripts/stock_report/render_selector_html.py data/reports/stock_selector_IN_2026-07-04_data.json
Output: data/reports/stock_selector_IN_<date>_v3_report.html
"""
import json
import os
import sys
from datetime import date, datetime

# Known data-pipeline exclusions with reason
MANUAL_EXCLUDE = {
    "GENSOL": "SEBI enforcement action Apr-2025 (filed as 'Action(s) initiated or orders passed' — not in risk_tier set); stock at ~₹21 vs peak ₹1,400+, -97%; pipeline has not cleaned beneficiary table. EXCLUDE.",
}

NLP_NOISE_KEYWORDS = [
    "years ", "half ", "shareholders", "regulations 33", "auditor's",
    "pharma demand",  # "defense electronics: Constraint from Pharma Demand"
]

def is_nlp_noise(theme_name: str) -> bool:
    n = theme_name.lower()
    return any(kw in n for kw in NLP_NOISE_KEYWORDS)

def fmt_date(d):
    if not d:
        return "—"
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d").strftime("%d-%b-%Y")
    except Exception:
        return str(d)[:10]

def pill(label, cls):
    return f'<span class="pill-{cls}">{label}</span>'

def stage_pill(stage):
    m = {
        "Accelerating": ("pill-acc", "Accelerating"),
        "Consensus": ("pill-con", "Consensus"),
        "Emerging": ("pill-em", "Emerging"),
        "Hidden Formation": ("pill-hid", "Hidden Formation"),
    }
    c, l = m.get(stage, ("pill-em", stage))
    return f'<span class="{c}">{l}</span>'

def tier_badge(tier):
    if tier == "Tier1_HighConviction":
        return '<span class="badge-t1">T1 — BUY</span>'
    if tier == "Tier3_Watch":
        return '<span class="badge-t3w">T3 — WATCH</span>'
    if tier == "Tier3_Ignore":
        return '<span class="badge-t3i">T3 — IGNORE</span>'
    return f'<span class="badge-t3i">{tier}</span>'

def pos_badge(pos):
    if not pos:
        return "—"
    short = (pos
             .replace("full_position", "full_position")
             .replace("half_position_scale_on_breakout", "half (on breakout)")
             .replace("verify_extension_before_entry", "verify extension"))
    cls = "pos-full" if "full" in pos else ("pos-half" if "half" in pos else "pos-verify")
    return f'<span class="{cls}">{short}</span>'

def chain_pill(chain):
    n = chain.lower()
    if "battery" in n or "li-ion" in n:
        return f'<span class="chain-battery">{chain}</span>'
    if "defense" in n or "pcb" in n or "ems" in n:
        return f'<span class="chain-defense">{chain}</span>'
    if "solar" in n:
        return f'<span class="chain-solar">{chain}</span>'
    if "fiber" in n or "optical" in n or "telecom" in n:
        return f'<span class="chain-telecom">{chain}</span>'
    if "hydrogen" in n:
        return f'<span class="chain-hydro">{chain}</span>'
    return f'<span class="chain-infra">{chain}</span>'

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Georgia', serif; font-size: 11px; color: #1a1a1a; background: #fff; line-height: 1.55; }
.page { max-width: 960px; margin: 0 auto; padding: 24px; }

/* HEADER */
.report-header { background: #1a3a5c; color: #fff; padding: 18px 24px; border-radius: 4px 4px 0 0; }
.report-header h1 { font-size: 20px; font-weight: bold; letter-spacing: 0.5px; }
.report-header .subtitle { font-size: 11px; color: #a8c4e0; margin-top: 4px; }
.report-header .meta { font-size: 10px; color: #7aacce; margin-top: 8px; display: flex; gap: 24px; flex-wrap: wrap; }
.report-header .meta span { font-weight: bold; color: #c8a02c; }

/* REGIME BAR */
.regime-bar { display: flex; align-items: center; gap: 10px; background: #e8f5e9; border: 1px solid #a5d6a7; border-left: 5px solid #2e7d32; padding: 8px 14px; margin: 14px 0; border-radius: 2px; font-size: 10.5px; }
.regime-bar.bear { background: #fff3e0; border-color: #ffb74d; border-left-color: #e65100; }
.regime-bar.flat { background: #f5f5f5; border-color: #bdbdbd; border-left-color: #757575; }
.regime-label { font-weight: bold; font-size: 12px; }

/* TOP PICKS */
.picks-box { border: 2px solid #c8a02c; border-left: 6px solid #c8a02c; background: #fffdf5; padding: 14px 18px; margin: 16px 0; border-radius: 2px; }
.picks-box h2 { font-size: 13px; color: #1a3a5c; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
.picks-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.pick-pill { background: #1a3a5c; color: #fff; padding: 4px 13px; border-radius: 12px; font-size: 11px; font-weight: bold; }
.pick-pill.primary { background: #c8a02c; color: #1a1a1a; }
.pick-pill.excluded { background: #c0392b; color: #fff; text-decoration: line-through; }
.picks-box p { font-size: 10.5px; color: #444; line-height: 1.7; }

/* JUDGMENT LAYER */
.judgment-box { border: 2px solid #1a3a5c; border-left: 6px solid #1a3a5c; background: #f7fafd; padding: 14px 18px; margin: 16px 0; border-radius: 2px; page-break-inside: avoid; }
.judgment-box h2 { font-size: 13px; color: #1a3a5c; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
.judgment-box .jnote { font-size: 9.5px; color: #666; font-style: italic; margin-bottom: 10px; }
.judgment-box h3 { font-size: 11px; color: #1a3a5c; margin: 12px 0 6px; }
.jtable { width: 100%; border-collapse: collapse; font-size: 10px; margin: 6px 0; }
.jtable th { background: #1a3a5c; color: #fff; padding: 4px 8px; text-align: left; font-size: 9.5px; }
.jtable td { border-bottom: 1px solid #d8e2ec; padding: 4px 8px; vertical-align: top; }
.grade-A  { background: #2e7d32; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.grade-B  { background: #c8a02c; color: #1a1a1a; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.grade-C  { background: #c0392b; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.grade-X  { background: #757575; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.cat-core   { background: #2e7d32; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.cat-timing { background: #1a3a5c; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.cat-watch  { background: #888; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.cat-avoid  { background: #c0392b; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.div-flag { color: #c0392b; font-weight: bold; font-size: 10px; }
.jportfolio { background: #fffdf5; border-left: 3px solid #c8a02c; padding: 8px 12px; margin-top: 10px; font-size: 10px; }

/* EXCLUDE BANNER */
.exclude-banner { background: #fdf2f2; border: 2px solid #c0392b; border-left: 6px solid #c0392b; padding: 12px 16px; margin: 12px 0; border-radius: 2px; }
.exclude-banner h4 { color: #c0392b; font-size: 11px; margin-bottom: 6px; }
.exclude-banner p { font-size: 10.5px; color: #444; }

/* PHASE LEGEND */
.phase-legend { background: #f0f4f8; border-left: 3px solid #1a3a5c; padding: 8px 14px; margin: 12px 0; font-size: 10px; }
.phase-legend strong { color: #1a3a5c; }
.pill-em { background: #e8f4e8; color: #2d6a2d; padding: 1px 7px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.pill-acc { background: #1a3a5c; color: #fff; padding: 1px 7px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.pill-con { background: #888; color: #fff; padding: 1px 7px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.pill-hid { background: #6b4f00; color: #fff; padding: 1px 7px; border-radius: 10px; font-size: 9px; font-weight: bold; }
.pill-noise { background: #c0392b; color: #fff; padding: 1px 7px; border-radius: 10px; font-size: 9px; font-weight: bold; }

/* SECTIONS */
.section { margin: 22px 0; }
.section h2 { font-size: 13px; color: #1a3a5c; border-bottom: 2px solid #c8a02c; padding-bottom: 4px; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.section h3 { font-size: 11px; color: #1a3a5c; margin: 12px 0 6px; font-style: italic; }

/* TABLES */
table { width: 100%; border-collapse: collapse; font-size: 10px; margin-bottom: 10px; }
th { background: #1a3a5c; color: #fff; padding: 5px 8px; text-align: left; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.3px; }
td { padding: 5px 8px; border-bottom: 1px solid #e8ecf0; vertical-align: top; }
tr:nth-child(even) td { background: #f7f9fb; }
.num { text-align: right; font-family: monospace; }
.ticker { font-weight: bold; font-family: monospace; color: #1a3a5c; font-size: 11px; }
.t1-row { background: #fffdf0 !important; }
.t1-row td { }
.t3w-row { background: #f0f8ff !important; }
.excluded-row td { color: #aaa; text-decoration: line-through; background: #fff5f5 !important; }

/* TIER BADGES */
.badge-t1 { background: #c8a02c; color: #1a1a1a; padding: 2px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; white-space: nowrap; }
.badge-t3w { background: #2196F3; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; white-space: nowrap; }
.badge-t3i { background: #9e9e9e; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; white-space: nowrap; }
.badge-excl { background: #c0392b; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 9px; font-weight: bold; }

/* POSITION BADGES */
.pos-full  { background: #27ae60; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; font-weight: bold; }
.pos-half  { background: #e67e22; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; font-weight: bold; }
.pos-verify{ background: #8e44ad; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; font-weight: bold; }

/* FRESHNESS BADGES */
.fresh-new   { background: #1b5e20; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; font-weight: bold; }
.fresh-fresh { background: #2e7d32; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; font-weight: bold; }
.fresh-dev   { background: #558b2f; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; }
.fresh-est   { background: #999; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; }
.fresh-con   { background: #777; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; }

/* RISK BADGES */
.risk-high { background: #c0392b; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; font-weight: bold; }
.risk-elev { background: #e67e22; color: #fff; padding: 1px 7px; border-radius: 8px; font-size: 9px; }
.risk-norm { color: #555; font-size: 9px; }

/* CHAIN PILLS */
.chain-battery { background: #1b5e20; color: #fff; padding: 1px 6px; border-radius: 8px; font-size: 8.5px; margin: 1px; display: inline-block; }
.chain-defense { background: #1a3a5c; color: #fff; padding: 1px 6px; border-radius: 8px; font-size: 8.5px; margin: 1px; display: inline-block; }
.chain-solar   { background: #e65100; color: #fff; padding: 1px 6px; border-radius: 8px; font-size: 8.5px; margin: 1px; display: inline-block; }
.chain-telecom { background: #5b21b6; color: #fff; padding: 1px 6px; border-radius: 8px; font-size: 8.5px; margin: 1px; display: inline-block; }
.chain-hydro   { background: #01579b; color: #fff; padding: 1px 6px; border-radius: 8px; font-size: 8.5px; margin: 1px; display: inline-block; }
.chain-infra   { background: #b45309; color: #fff; padding: 1px 6px; border-radius: 8px; font-size: 8.5px; margin: 1px; display: inline-block; }

/* MOMENTUM BAR */
.bar-bg   { background: #e0e6ed; border-radius: 3px; height: 6px; width: 100%; margin-top: 3px; }
.bar-fill { height: 6px; background: #1a3a5c; border-radius: 3px; }
.bar-fill.consensus { background: #888; }

/* NOISE / BEAR BOX */
.noise-box { background: #fdf2f2; border: 1px solid #e74c3c; border-left: 4px solid #e74c3c; padding: 10px 14px; margin: 10px 0; border-radius: 2px; }
.noise-box h4 { color: #c0392b; font-size: 10.5px; margin-bottom: 6px; }
.noise-box p  { font-size: 10px; color: #555; }
.bear-box  { background: #fff8f0; border-left: 3px solid #e67e22; padding: 8px 14px; margin: 8px 0; font-size: 10px; }
.bear-box h4  { color: #d35400; font-size: 10px; margin-bottom: 4px; }
.note-box  { background: #f0f7ff; border-left: 3px solid #1a3a5c; padding: 8px 14px; margin: 8px 0; font-size: 10px; color: #333; }

/* SCORING BREAKDOWN */
.score-detail { font-size: 9.5px; color: #555; margin-top: 2px; font-family: monospace; }

/* TIER LEGEND */
.tier-legend { background: #f5f5f5; border: 1px solid #ddd; padding: 10px 14px; margin: 14px 0; font-size: 10px; display: flex; gap: 20px; flex-wrap: wrap; }

/* FOOTER */
.footer { border-top: 1px solid #ddd; margin-top: 24px; padding-top: 10px; font-size: 9px; color: #888; text-align: center; }

@media print {
  .page { padding: 12px; }
  .report-header { border-radius: 0; }
  tr { page-break-inside: avoid; }
  .section { page-break-inside: avoid; }
}
"""

def freshness_badge(label):
    m = {
        "new_theme": ("fresh-new", "new_theme ×1.10"),
        "fresh":     ("fresh-fresh", "fresh ×1.12"),
        "developing":("fresh-dev", "developing ×1.05"),
        "established":("fresh-est", "established ×1.00"),
        "consensus": ("fresh-con", "consensus ×0.93"),
    }
    c, t = m.get(label, ("fresh-est", label))
    return f'<span class="{c}">{t}</span>'

def risk_badge(rt):
    if rt == "HIGH":
        return '<span class="risk-high">HIGH ⚠</span>'
    if rt == "ELEVATED":
        return '<span class="risk-elev">ELEVATED</span>'
    return '<span class="risk-norm">NORMAL</span>'

def _grade_cls(grade: str) -> str:
    g = (grade or "").strip().upper()
    if g.startswith("A"):
        return "grade-A"
    if g.startswith("B"):
        return "grade-B"
    if g.startswith("C"):
        return "grade-C"
    return "grade-X"


def _cat_cls(cat: str) -> str:
    c = (cat or "").upper()
    if "CORE" in c:
        return "cat-core"
    if "TIMING" in c:
        return "cat-timing"
    if "WATCH" in c:
        return "cat-watch"
    return "cat-avoid"


def render_judgment(j: dict) -> str:
    """Judgment-layer section from the *_judgment.json sidecar (skill Step 3).
    The formula tiers above are the screen; this section is the final call."""
    parts = [f"""
<div class="judgment-box">
  <h2>Analyst Verdict — The Final Call</h2>
  <div class="jnote"><strong>How to read this section:</strong> the score-ranked list above is only a screen.
  The final call is made in four steps — <strong>Step 1</strong> grades each theme by how real and binding its
  supply constraint is (A = act, B = act with a trigger, C = reject); <strong>Step 2</strong> lists companies the
  screen missed entirely; <strong>Step 3</strong> checks which government schemes each company's own filings cite;
  <strong>Step 4</strong> gives the final Buy / Watch / Avoid verdict per stock, overriding the formula ranks.<br>
  {j.get('pit_note','')}</div>
  <h3>Step 1 — Which themes deserve capital (constraint-quality grade A/B/C — never by score or past returns)</h3>
  <table class="jtable">
    <tr><th>Constraint / theme</th><th>Grade</th><th>Verdict</th><th>Deciding evidence</th></tr>
"""]
    for v in j.get("constraint_verdicts", []):
        parts.append(
            f'<tr><td><strong>{v["name"]}</strong></td>'
            f'<td><span class="{_grade_cls(v["grade"])}">{v["grade"]}</span></td>'
            f'<td>{v["verdict"]}</td><td>{v["evidence"]}</td></tr>')
    parts.append("</table>")
    if j.get("unmapped_peer_review"):
        parts.append("""
  <h3>Step 2 — Stocks the screen missed (their filings discuss the constrained product, but the data pipeline never mapped them — not scored, verify externally)</h3>
  <table class="jtable">
    <tr><th>Theme</th><th>Companies found</th><th>Analyst read</th></tr>
""")
        for u in j["unmapped_peer_review"]:
            parts.append(f'<tr><td><strong>{u["chain"]}</strong></td><td>{u["reviewed"]}</td><td>{u["read"]}</td></tr>')
        parts.append("</table>")
    parts.append("""
  <h3>Step 3 — Government policy check (schemes cited in each company's own filings — supporting evidence, never a score input)</h3>
  <table class="jtable">
    <tr><th>Ticker</th><th>Schemes cited</th><th>Read</th></tr>
""")
    for p in j.get("policy_highlights", []):
        parts.append(f'<tr><td><strong>{p["ticker"]}</strong></td><td>{p["schemes"]}</td><td>{p["read"]}</td></tr>')
    parts.append("""
  </table>
  <h3>Step 4 — Final stock verdicts (► = analyst overrides the formula rank)</h3>
  <table class="jtable">
    <tr><th>Verdict</th><th>Ticker</th><th>Label</th><th>Expect</th><th>Formula rank</th><th>Conviction</th><th>Why</th></tr>
""")
    for c in j.get("categorization", []):
        div = '<span class="div-flag">►</span> ' if c.get("divergence") else ""
        parts.append(
            f'<tr><td><span class="{_cat_cls(c["category"])}">{c["category"]}</span></td>'
            f'<td>{div}<strong>{c["ticker"]}</strong></td>'
            f'<td><strong>{c.get("archetype", "—")}</strong></td>'
            f'<td>{c.get("expect", "—")}</td>'
            f'<td>{c.get("formula","")}</td><td>{c.get("conviction","")}</td>'
            f'<td>{c["rationale"]}</td></tr>')
    parts.append("""
    <tr><td colspan="7" style="background:#f0f4f8;font-size:9px;color:#555">
    <strong>Label legend:</strong> A1 early compounder (10-40x/3-5yr, hold through drawdowns) ·
    A2 mid-life compounder (2-4x/2-3yr, event-based exit) · A3 late compounder (50-150%, holders stay) ·
    B operating-leverage burst (2-5x, exit clock = scheme life) ·
    C cyclical squeeze / MU-class (2-8x in 12-24mo then GIVE-BACK — always a trade, exit written on entry) ·
    D ballast (30-80%, low drawdown, regulated cap) · E re-rating + kicker (2-3x if unlock lands; can be binary).
    Bands are historical analogs from this system's 2020-26 backtest, not forecasts.</td></tr>
""")
    parts.append("</table>")
    if j.get("timing_sleeve"):
        parts.append("""
  <h3>Step 5 — Constraint-cycle timing (layer type × capex momentum; price state sizes entries only, never selects)</h3>
  <table class="jtable">
    <tr><th>Chain</th><th>Layer</th><th>Capex momentum</th><th>Read / action</th></tr>
""")
        for t in j["timing_sleeve"]:
            parts.append(f'<tr><td><strong>{t["chain"]}</strong></td><td>{t["layer"]}</td>'
                         f'<td>{t["momentum"]}</td><td>{t["read"]}</td></tr>')
        parts.append("</table>")
    parts.append(f"""
  <div class="jportfolio"><strong>Portfolio shape:</strong> {j.get('portfolio_note','')}</div>
</div>
""")
    return "".join(parts)


def render(doc: dict, as_of_str: str) -> str:
    country = doc.get("country", "IN")
    as_of_dt = datetime.strptime(as_of_str, "%Y-%m-%d")
    as_of_label = as_of_dt.strftime("%d-%b-%Y")
    generated = datetime.now().strftime("%d-%b-%Y %H:%M")

    major_themes = doc.get("major_themes", [])
    emerging_themes = doc.get("emerging_themes", [])
    candidates = doc.get("ranked_candidates", [])
    evidence = doc.get("evidence_dashboard", [])
    overlap = doc.get("cross_theme_overlap", [])
    bear_cases = doc.get("bear_cases", [])
    regime = doc.get("market_regime", {})
    supply = doc.get("supply_side_beneficiaries", [])

    # Identify exclusions
    excluded_tickers = {t for t in MANUAL_EXCLUDE if any(c["ticker"] == t for c in candidates)}

    # Effective T1 (after exclusions)
    t1_effective = [c for c in candidates
                    if c.get("discovery_tier") == "Tier1_HighConviction"
                    and c["ticker"] not in excluded_tickers]
    t3_watch = [c for c in candidates if c.get("discovery_tier") == "Tier3_Watch"]

    # Regime styling
    rl = regime.get("regime_label", "")
    regime_cls = "bear" if "BEAR" in rl else ("flat" if "FLAT" in rl else "")

    parts = []
    country_title = "India" if country == "IN" else "US"
    country_label = "India (NSE/BSE)" if country == "IN" else "United States (EDGAR)"

    # ── HEAD ────────────────────────────────────────────────────────────────────
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{country_title} Stock Selector — {as_of_label} | MakroGraph Intelligence</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
""")

    # ── HEADER ──────────────────────────────────────────────────────────────────
    parts.append(f"""
<div class="report-header">
  <h1>{country_title} Stock Selector — {as_of_label}</h1>
  <div class="subtitle">Point-in-time opportunity scan | MakroGraph Intelligence | Supply-Demand Theme Engine v3</div>
  <div class="meta">
    <div>As-of: <span>{as_of_label}</span></div>
    <div>Country: <span>{country_label}</span></div>
    <div>Major themes: <span>{len(major_themes)}</span></div>
    <div>Emerging themes: <span>{len(emerging_themes)}</span></div>
    <div>Candidates ranked: <span>{len(candidates)}</span></div>
    <div>Generated: <span>{generated}</span></div>
  </div>
</div>
""")

    # ── MARKET REGIME ───────────────────────────────────────────────────────────
    breadth = regime.get("breadth_pct_above_200dma", 0)
    avg3m   = regime.get("avg_3m_return_pct", 0)
    parts.append(f"""
<div class="regime-bar {regime_cls}">
  <div class="regime-label">{rl}</div>
  <div>— {breadth:.0f}% of large-caps above 200DMA &nbsp;|&nbsp; avg 3m return {avg3m:+.1f}%</div>
  <div style="font-size:9.5px;color:#555">{regime.get('note','')}</div>
</div>
""")

    # ── CHAIN CONTINUITY ALERTS (pipeline blind spots — the solar-2023 lesson) ──
    alerts = doc.get("chain_continuity_alerts") or []
    if alerts:
        items = "".join(
            f"<li><strong>{a['chain']}</strong> — last mapped {fmt_date(str(a.get('last_mapped','')))} "
            f"({a['staleness_days']} days stale)</li>" for a in alerts)
        parts.append(f"""
<div class="exclude-banner" style="border-color:#e67e22;background:#fdf6ec">
  <h4 style="color:#b35b09">⚠ PIPELINE BLIND SPOTS — chains with stale mapping (vanished ≠ resolved)</h4>
  <p>These chains stopped receiving fresh beneficiary mapping. This is a data-pipeline gap, NOT evidence the
  constraint resolved — the 2023 solar rally (+579% top name) happened inside exactly such a blind spot.
  Last-known grades stay alive; manual verification is top priority.</p>
  <ul style="font-size:10.5px;margin:6px 0 0 18px">{items}</ul>
</div>
""")

    # ── EXCLUSION BANNERS ───────────────────────────────────────────────────────
    for ticker, reason in MANUAL_EXCLUDE.items():
        if ticker in excluded_tickers:
            parts.append(f"""
<div class="exclude-banner">
  <h4>⛔ DATA PIPELINE EXCLUSION — {ticker}</h4>
  <p>{reason}</p>
</div>
""")

    # ── TOP PICKS BOX ───────────────────────────────────────────────────────────
    picks_rows = ""
    for i, c in enumerate(candidates[:8]):
        excl = c["ticker"] in excluded_tickers
        cls  = "excluded" if excl else ("primary" if i < 5 else "")
        label = f"{i+1}. {c['ticker']} {c.get('composite_score',0):.3f}"
        if excl:
            label += " ⛔"
        picks_rows += f'<span class="pick-pill {cls}">{label}</span>\n'

    t1_tickers = ", ".join(c["ticker"] for c in t1_effective[:5])
    fresh_count = sum(1 for c in t1_effective if c.get("scoring_breakdown", {}).get("freshness_label") in ("fresh", "new_theme"))
    parts.append(f"""
<div class="picks-box">
  <h2>Effective T1 Picks — {as_of_label}</h2>
  <div class="picks-row">{picks_rows}</div>
  <p><strong>Effective T1 (buy):</strong> {t1_tickers or '—'}.
  &nbsp;|&nbsp; <strong>Fresh-theme picks in T1:</strong> {fresh_count}/5 (higher = stronger multi-year compounding setup).
  &nbsp;|&nbsp; <strong>Scoring:</strong> composite = fundamental × freshness_multiplier; 200DMA → position_size_guidance only (not the score).
  &nbsp;|&nbsp; Tier3_Watch names (ranks 6-8 + fresh + NORMAL risk) are graduation candidates — buy only when they appear in T1 on next annual scan.</p>
</div>
""")

    # ── JUDGMENT LAYER (skill Step 3) ───────────────────────────────────────────
    judgment = doc.get("_judgment")
    if judgment:
        parts.append(render_judgment(judgment))

    # ── TIER LEGEND ─────────────────────────────────────────────────────────────
    parts.append("""
<div class="tier-legend">
  <div><span class="badge-t1">T1 — BUY</span> &nbsp;Rank 1-5: actionable, buy per position_size_guidance</div>
  <div><span class="badge-t3w">T3 — WATCH</span> &nbsp;Rank 6-8 + fresh/new_theme + NORMAL risk: watchlist, buy when graduates to T1</div>
  <div><span class="badge-t3i">T3 — IGNORE</span> &nbsp;Rank 9+ or consensus/established or ELEVATED/HIGH risk: drop entirely</div>
</div>
""")

    # ── PHASE LEGEND ────────────────────────────────────────────────────────────
    parts.append("""
<div class="phase-legend">
  <strong>Phase legend:</strong>&nbsp;
  <span class="pill-em">Emerging</span> first signals — watch only &nbsp;|&nbsp;
  <span class="pill-acc">Accelerating</span> capex committed, revenue visible, not crowded — <em>the investable phase</em> &nbsp;|&nbsp;
  <span class="pill-con">Consensus</span> broadly known, momentum decelerating — avoid chasing &nbsp;|&nbsp;
  <span class="pill-hid">Hidden Formation</span> early signal only
</div>
""")

    # ── SECTION 1: MAJOR THEMES ─────────────────────────────────────────────────
    parts.append('<div class="section"><h2>1. Major Themes</h2>')
    parts.append("""<table>
<tr><th>Theme</th><th>Plain-English Meaning</th><th>Started</th><th>Phase</th>
<th class="num">Strength</th><th class="num">Qtrs</th><th class="num">Cos</th></tr>""")

    for t in major_themes[:10]:
        name  = t.get("theme_name") or t.get("name", "")
        desc  = t.get("description", "")
        stage = t.get("stage_label", "")
        qtrs  = t.get("confirmed_quarters", 0)
        start = fmt_date(t.get("first_detected"))
        strength = t.get("strength_now", 0)
        cos   = t.get("companies_mapped", "—")
        cls   = "t1-row" if stage == "Accelerating" else ""

        # Plain English from description (strip the template prefix)
        meaning = desc
        for prefix in ["⚡ Supply-Demand Tension:", "💎 EXPLOSIVE THEME —", "🚨 BOTTLENECK ALERT:", "📈 Demand Running Ahead"]:
            if meaning.startswith(prefix):
                meaning = meaning[len(prefix):].strip()
                break
        if len(meaning) > 180:
            meaning = meaning[:180] + "…"

        parts.append(f"""<tr class="{cls}">
  <td><strong>{name}</strong></td>
  <td>{meaning}</td>
  <td>{start}</td>
  <td>{stage_pill(stage)}</td>
  <td class="num">{strength:.1f}</td>
  <td class="num">{qtrs}</td>
  <td class="num">{cos}</td>
</tr>""")

    parts.append("</table>")

    # Bear cases for major themes
    bear_by_theme = {b["theme"]: b for b in bear_cases}
    for t in major_themes[:8]:
        name = t.get("theme_name") or t.get("name", "")
        if name in bear_by_theme:
            bc = bear_by_theme[name]
            risks = " &nbsp;|&nbsp; ".join(bc.get("risks", []))
            parts.append(f'<div class="bear-box"><h4>Bear cases — {name}</h4><p>{risks}</p></div>')

    parts.append("</div>")

    # ── SECTION 2: EMERGING THEMES ──────────────────────────────────────────────
    parts.append('<div class="section"><h2>2. Emerging Themes (12-Month Window)</h2>')

    noise = [t for t in emerging_themes if is_nlp_noise(t.get("theme_name") or t.get("name", ""))]
    real  = [t for t in emerging_themes if not is_nlp_noise(t.get("theme_name") or t.get("name", ""))]

    if noise:
        noise_names = " ".join(f'<span class="pill-noise">{t.get("theme_name") or t.get("name","")}</span>' for t in noise)
        parts.append(f"""<div class="noise-box">
<h4>⚠ NLP Artifact Themes — Discard</h4>
<p>These labels arise from regulatory/audit/compliance language in filings and do NOT represent real economic constraints: {noise_names}</p>
</div>""")

    if real:
        parts.append("""<table>
<tr><th>Theme</th><th>First Detected</th><th>Phase</th>
<th class="num">Qtrs</th><th class="num">New Cos</th><th>Signal Quality</th></tr>""")
        for t in real:
            name  = t.get("theme_name") or t.get("name", "")
            stage = t.get("stage_label", "")
            qtrs  = t.get("confirmed_quarters", 0)
            start = fmt_date(t.get("first_detected"))
            new_cos = t.get("new_beneficiaries_in_window", "—")
            desc  = t.get("description", "")
            cls   = "t1-row" if stage == "Accelerating" else ""
            parts.append(f"""<tr class="{cls}">
  <td><strong>{name}</strong></td><td>{start}</td><td>{stage_pill(stage)}</td>
  <td class="num">{qtrs}</td><td class="num">{new_cos}</td>
  <td style="font-size:9.5px;color:#555">{desc[:200]}</td>
</tr>""")
        parts.append("</table>")

        for t in real[:5]:
            name = t.get("theme_name") or t.get("name", "")
            if name in bear_by_theme:
                bc = bear_by_theme[name]
                risks = " &nbsp;|&nbsp; ".join(bc.get("risks", []))
                parts.append(f'<div class="bear-box"><h4>Bear cases — {name}</h4><p>{risks}</p></div>')
    else:
        parts.append('<div class="note-box">No actionable new themes in this window — all emerging labels are NLP artifacts.</div>')

    parts.append("</div>")

    # ── SECTION 3: RANKED CANDIDATES ────────────────────────────────────────────
    parts.append('<div class="section"><h2>3. Ranked Candidates</h2>')
    parts.append("""<p style="font-size:10px;color:#555;margin-bottom:8px;">
<strong>Scoring:</strong> fundamental = 0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub;
composite = fundamental × freshness_multiplier (new_theme ×1.10 | fresh ×1.12 | developing ×1.05 | established ×1.00 | consensus ×0.93).
200DMA → position_size_guidance only, NOT a score input.
</p>""")

    parts.append("""<table>
<tr>
  <th>#</th><th>Ticker</th><th>Tier</th>
  <th class="num">Composite</th><th>Freshness</th><th>Risk</th>
  <th>200DMA</th><th class="num">%52wH</th>
  <th>Position</th><th>Products / Themes</th>
</tr>""")

    for i, c in enumerate(candidates):
        tick = c["ticker"]
        excl = tick in excluded_tickers
        tier = c.get("discovery_tier", "")
        sb   = c.get("scoring_breakdown", {})
        tech = c.get("technical") or {}
        fl   = sb.get("freshness_label", "?")
        rt   = c.get("risk_tier", "NORMAL")
        pos  = c.get("position_size_guidance", "?")
        above200 = tech.get("above_200dma")
        pct52 = tech.get("pct_from_52w_high")
        comp  = c.get("composite_score", 0)
        products = c.get("products") or []
        themes   = c.get("themes") or []

        row_cls = "excluded-row" if excl else ("t1-row" if "Tier1" in tier else ("t3w-row" if "Watch" in tier else ""))

        above_str = "✓ above" if above200 else ("✗ below" if above200 is False else "?")
        pct52_str = f"{pct52:+.1f}%" if pct52 is not None else "—"
        tier_badge_html = '<span class="badge-excl">EXCLUDE ⛔</span>' if excl else tier_badge(tier)
        prod_str = " ".join(chain_pill(p) for p in (products or themes)[:4])

        parts.append(f"""<tr class="{row_cls}">
  <td class="num">{i+1}</td>
  <td class="ticker">{tick}</td>
  <td>{tier_badge_html}</td>
  <td class="num"><strong>{comp:.3f}</strong></td>
  <td>{freshness_badge(fl)}</td>
  <td>{risk_badge(rt)}</td>
  <td style="font-size:9px">{above_str}</td>
  <td class="num">{pct52_str}</td>
  <td>{pos_badge(pos) if not excl else '—'}</td>
  <td>{prod_str}</td>
</tr>""")

    parts.append("</table>")

    # Scoring breakdown — top 3 effective T1
    top3 = t1_effective[:3]
    if top3:
        parts.append('<h3>Scoring Breakdown — Top 3 Effective T1</h3>')
        parts.append('<table><tr><th>Component</th>')
        for c in top3:
            parts.append(f'<th class="num">{c["ticker"]}</th>')
        parts.append("</tr>")

        sb_fields = [
            ("conviction_x0.45",   "Conviction ×0.45"),
            ("breadth_x0.25",      "Breadth ×0.25"),
            ("order_book_x0.20",   "Order Book ×0.20"),
            ("import_sub_x0.10",   "Import Sub ×0.10"),
            ("fundamental_score",  "Fundamental sub-total"),
            ("freshness_multiplier","Freshness multiplier"),
            ("composite_score",    "Composite (final)"),
        ]
        for field, label in sb_fields:
            bold = field in ("fundamental_score", "composite_score")
            parts.append(f'<tr><td>{"<strong>" if bold else ""}{label}{"</strong>" if bold else ""}</td>')
            for c in top3:
                sb = c.get("scoring_breakdown", {})
                val = sb.get(field, "—")
                fmt = f"<strong>{val}</strong>" if bold else str(val)
                parts.append(f'<td class="num">{fmt}</td>')
            parts.append("</tr>")

        parts.append("</table>")

    # Tier3_Watch callout
    if t3_watch:
        parts.append('<h3>Tier3_Watch — Graduation Watchlist</h3>')
        parts.append("""<table>
<tr><th>Ticker</th><th>Rank</th><th>Composite</th><th>Freshness</th><th>Action</th></tr>""")
        for c in t3_watch:
            idx = candidates.index(c) + 1
            fl  = c.get("scoring_breakdown", {}).get("freshness_label", "?")
            parts.append(f"""<tr class="t3w-row">
<td class="ticker">{c['ticker']}</td>
<td class="num">{idx}</td>
<td class="num">{c.get('composite_score',0):.3f}</td>
<td>{freshness_badge(fl)}</td>
<td style="font-size:9.5px">Watchlist — buy when this ticker appears in T1 on next annual scan</td>
</tr>""")
        parts.append("</table>")
    else:
        parts.append('<div class="note-box">No Tier3_Watch names at this date — ranks 6-8 are all consensus/established or ELEVATED risk. Watchlist is empty.</div>')

    parts.append("</div>")

    # ── SECTION 4: EVIDENCE DASHBOARD ───────────────────────────────────────────
    parts.append('<div class="section"><h2>4. Evidence Dashboard</h2>')
    parts.append("""<table>
<tr><th>Theme</th><th class="num">Cos</th><th class="num">Filings</th>
<th class="num">Bottleneck Signals</th><th class="num">Qtrs</th>
<th class="num">Policy Events</th><th class="num">Confidence</th><th>Momentum</th></tr>""")

    for e in evidence[:10]:
        name = e.get("theme_name") or e.get("theme", "")
        cos  = e.get("companies_mapped", "—")
        fil  = e.get("filings_covered", "—")
        bot  = e.get("bottleneck_signals", "—")
        qtrs = e.get("confirmed_quarters", 0)
        pol  = e.get("policy_events", 0)
        conf = e.get("evidence_confidence_pct", 0)
        bar_cls = "consensus" if qtrs >= 16 else ""
        pol_flag = " ⚠" if pol == 0 else ""
        parts.append(f"""<tr>
  <td>{name}</td>
  <td class="num">{cos}</td><td class="num">{fil}</td>
  <td class="num">{bot}</td><td class="num">{qtrs}</td>
  <td class="num">{pol}{pol_flag}</td>
  <td class="num">{conf}%</td>
  <td><div class="bar-bg"><div class="bar-fill {bar_cls}" style="width:{conf}%"></div></div></td>
</tr>""")

    parts.append("</table></div>")

    # ── SECTION 5: CROSS-THEME OVERLAP ──────────────────────────────────────────
    parts.append('<div class="section"><h2>5. Cross-Theme Overlap (Multi-Chain Structural Chokepoints)</h2>')
    parts.append("""<table>
<tr><th>Ticker</th><th class="num">Chains</th><th class="num">Overlap Score</th>
<th>Chain detail</th><th>Read</th></tr>""")

    for o in overlap[:8]:
        tick   = o.get("ticker", "")
        n_ch   = o.get("n_independent_chains", 0)
        score  = o.get("overlap_score", 0)
        chains = o.get("chains", [])
        excl   = tick in excluded_tickers
        row_cls = "excluded-row" if excl else ("t1-row" if n_ch >= 3 else "")
        chain_pills = " ".join(chain_pill(ch.get("chain","")) + f' <span style="font-size:8px;color:#888">conv={ch.get("conviction",0):.2f}</span>' for ch in chains[:4])
        note = "EXCLUDED — data pipeline flag" if excl else ("Structural chokepoint — 4+ independent chains" if n_ch >= 4 else ("Strong overlap — 3 chains" if n_ch == 3 else ""))
        parts.append(f"""<tr class="{row_cls}">
  <td class="ticker">{"<s>" if excl else ""}{tick}{"</s>" if excl else ""}</td>
  <td class="num">{n_ch}</td>
  <td class="num">{score:.2f}</td>
  <td>{chain_pills}</td>
  <td style="font-size:9.5px;color:#555">{note}</td>
</tr>""")

    parts.append("</table></div>")

    # ── SECTION 6: PRIORITY PICKS & NEXT STEPS ──────────────────────────────────
    parts.append('<div class="section"><h2>6. Priority Action</h2>')
    parts.append("""<table>
<tr><th>Priority</th><th>Ticker</th><th>Tier</th><th>Position</th><th>Rationale</th></tr>""")

    rank = 1
    for c in t1_effective[:5]:
        sb    = c.get("scoring_breakdown", {})
        fl    = sb.get("freshness_label", "?")
        prods = ", ".join((c.get("products") or c.get("themes") or [])[:3])
        fresh_note = " — fresh theme, highest multi-year compounding potential" if fl in ("fresh", "new_theme") else ""
        rt    = c.get("risk_tier", "NORMAL")
        pos   = c.get("position_size_guidance", "?")
        parts.append(f"""<tr class="t1-row">
  <td class="num"><strong>{rank}</strong></td>
  <td class="ticker">{c['ticker']}</td>
  <td>{tier_badge(c.get('discovery_tier',''))}</td>
  <td>{pos_badge(pos)}</td>
  <td style="font-size:9.5px">{prods}{fresh_note}</td>
</tr>""")
        rank += 1

    for c in t3_watch:
        prods = ", ".join((c.get("products") or c.get("themes") or [])[:3])
        parts.append(f"""<tr class="t3w-row">
  <td class="num">Watch</td>
  <td class="ticker">{c['ticker']}</td>
  <td>{tier_badge(c.get('discovery_tier',''))}</td>
  <td>—</td>
  <td style="font-size:9.5px">{prods} — buy only when graduates to T1 on next annual scan</td>
</tr>""")

    parts.append(f"""</table>
<p style="font-size:10px;color:#555;margin-top:8px;">
Suggested next step: generate full research report for the top T1 picks —
<strong>{"generate report for " + t1_effective[0]["ticker"] + " as of " + as_of_str if t1_effective else "—"}</strong>
</p>
</div>""")

    # ── FOOTER ──────────────────────────────────────────────────────────────────
    parts.append(f"""
<div class="footer">
  Point-in-time report — contains no information dated after <strong>{as_of_label}</strong>.
  Generated {generated} by MakroGraph Intelligence. &nbsp;|&nbsp;
  All data sourced from makrograph DB (localhost). &nbsp;|&nbsp;
  Scoring v3: composite = fundamental × freshness_multiplier; 200DMA = position guidance only. &nbsp;|&nbsp;
  Not investment advice — data-driven view only.
</div>
</div><!-- /page -->
</body>
</html>""")

    return "\n".join(parts)


def main():
    paths = sys.argv[1:] if len(sys.argv) > 1 else []
    if not paths:
        print("Usage: python render_selector_html.py <json_path> [<json_path2> ...]")
        sys.exit(1)

    for path in paths:
        with open(path) as f:
            doc = json.load(f)

        as_of = doc.get("as_of_date", "")
        country = doc.get("country", "IN")

        # Optional judgment sidecar (skill Step 3 output, written by the analyst layer)
        jpath = os.path.join(os.path.dirname(path),
                             f"stock_selector_{country}_{as_of}_judgment.json")
        if os.path.exists(jpath):
            with open(jpath) as jf:
                doc["_judgment"] = json.load(jf)

        html = render(doc, as_of)

        out_dir = os.path.dirname(path)
        base = f"stock_selector_{country}_{as_of}_v3_report.html"
        out_path = os.path.join(out_dir, base)
        with open(out_path, "w") as f:
            f.write(html)
        print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
