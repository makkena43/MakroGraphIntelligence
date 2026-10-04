"""
Render selector JSON → standalone HTML report.
Usage:
  python scripts/stock_report/render_selector_html.py data/reports/stock_selector_IN_2025-12-31_data.json
  python scripts/stock_report/render_selector_html.py data/reports/stock_selector_IN_2026-07-04_data.json
Output: data/reports/stock_selector_IN_<date>_v3_report.html
"""
import json
import os
import re
import sys
from html import escape
from datetime import date, datetime

# Exclusions come from the data JSON's reference_data field (mg_manual_exclusions
# table) — no hardcoded tickers here (user rule, Jul-2026). Fallback empty.
def get_manual_exclusions(doc: dict) -> dict:
    return (doc.get("reference_data") or {}).get("manual_exclusions") or {}

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


def fmt_theme_start(d):
    """first_detected is LEFT-CENSORED at the corpus start (01-Jan-2020): 99 IN
    themes cluster in Jan/Feb-2020 because that's when document ingestion began,
    not when those themes were born. Label them honestly."""
    if not d:
        return "—"
    try:
        dt = datetime.strptime(str(d)[:10], "%Y-%m-%d")
        if dt <= datetime(2020, 3, 1):
            return "pre-2020 (data start)"
        return dt.strftime("%d-%b-%Y")
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
        return '<span class="badge-t1">T1 — RAW SCREEN</span>'
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
.judgment-box { border: 2px solid #1a3a5c; border-left: 6px solid #1a3a5c; background: #f7fafd; padding: 14px 18px; margin: 16px 0; border-radius: 2px; }
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
  /* A complete analysis section can span several pages. Keeping the whole
     block together turns any short remainder into an almost blank page;
     keep individual table rows intact instead and let sections flow. */
  .section { page-break-inside: auto; }
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


def _forward_value(row: dict | None) -> str:
    """Compact stock-level return or an honest exclusion label."""
    if not row:
        return "-"
    if row.get("status", "").startswith("measured"):
        value = row.get("return_pct")
        return f"{value:+,.1f}%" if value is not None else "-"
    labels = {
        "excluded_no_complete_price_window": "no full window",
        "excluded_not_tradable_at_anchor": "not tradable",
        "excluded_no_price_near_horizon": "no horizon close",
        "excluded_unexplained_price_discontinuity": "price break",
    }
    return labels.get(row.get("status"), "excluded")


def render_forward_validation(forward: dict) -> str:
    """Render a hindsight appendix without changing point-in-time selection."""
    anchor = forward.get("anchor") or {}
    audit = anchor.get("forward_test") or {}
    horizons = audit.get("horizons") or {}
    definitions = forward.get("section_definitions") or {}
    section_tickers = anchor.get("section_tickers") or {}
    method = forward.get("method") or {}
    alarm = forward.get("actionable_coverage_alarm") or {}
    order = list(definitions) or list(section_tickers)
    horizon_order = [key for key in ("6m", "12m", "24m", "36m", "to_latest") if key in horizons]
    horizon_labels = {"6m": "6m", "12m": "12m", "24m": "24m", "36m": "36m", "to_latest": "To latest"}

    parts = [f"""
<div class="judgment-box forward-audit" style="border-color:#7b3f00;border-left-color:#7b3f00;background:#fffaf2;page-break-before:always">
  <h2 style="color:#7b3f00">Forward-return validation - outcome audit, not as-of evidence</h2>
  <div class="jnote"><strong>Separation rule:</strong> every theme, constraint, company, role and section assignment above
  was frozen at {fmt_date(anchor.get("as_of_date"))}. Only the price window looks forward, through
  {fmt_date(forward.get("latest_price_date"))}. These outcomes measure whether the research machinery surfaced later
  winners; they do not retroactively grant position authority to a research-only stock.</div>
"""]

    actionable = sum(len(section_tickers.get(key) or []) for key in ("core_buy", "early_timing"))
    if actionable == 0:
        parts.append(f"""
  <div class="exclude-banner" style="margin:8px 0">
    <h4>NO ACTION-AUTHORIZED 2022 COHORT</h4>
    <p>Investment-committee hand-off and Early / Timing both emitted zero stocks. The returns below therefore validate
    <strong>research discovery sections only</strong>, not a deployable 2022 portfolio. This is recorded as
    {escape(alarm.get("status") or "an actionable-coverage alarm")} and remains a system-coverage problem.</p>
  </div>
""")

    parts.append("""
  <h3 style="color:#7b3f00">Section cohorts frozen at the anchor</h3>
  <table class="jtable forward-table"><tr><th>Report section</th><th class="num">Stocks</th><th>Frozen 2022 ticker cohort</th></tr>
""")
    for key in order:
        tickers = section_tickers.get(key) or []
        ticker_text = ", ".join(tickers) if tickers else "No stock emitted"
        parts.append(
            f'<tr><td><strong>{escape(definitions.get(key) or key)}</strong></td>'
            f'<td class="num">{len(tickers)}</td><td>{escape(ticker_text)}</td></tr>'
        )
    parts.append("</table>")

    parts.append("""
  <h3 style="color:#7b3f00">Cohort forward returns by report section</h3>
  <div class="jnote">Each cell shows median return, then mean return and measured/requested coverage. Median is the
  primary comparison because a single multi-bagger can dominate the mean. The benchmark is an equal-weighted,
  point-in-time 25-stock trailing-turnover large-cap basket.</div>
  <table class="jtable forward-table"><tr><th>Report section</th>
""")
    for horizon in horizon_order:
        target = fmt_date(horizons[horizon].get("target_date"))
        parts.append(f'<th>{horizon_labels[horizon]}<br><span style="font-size:8px">{target}</span></th>')
    parts.append("</tr>")
    for key in order:
        parts.append(f'<tr><td><strong>{escape(definitions.get(key) or key)}</strong></td>')
        for horizon in horizon_order:
            section = ((horizons[horizon].get("sections") or {}).get(key) or {})
            metrics = section.get("metrics") or {}
            requested = metrics.get("n_requested") or 0
            if not requested:
                cell = "No cohort"
            else:
                median = metrics.get("median_return_pct")
                mean = metrics.get("mean_return_pct")
                measured = metrics.get("n_measured") or 0
                median_text = f"{median:+,.1f}%" if median is not None else "-"
                mean_text = f"{mean:+,.1f}%" if mean is not None else "-"
                cell = (f'<strong>{median_text}</strong><br><span style="font-size:8px;color:#666">'
                        f'mean {mean_text}; {measured}/{requested}</span>')
            parts.append(f'<td class="num">{cell}</td>')
        parts.append("</tr>")
    parts.append('<tr class="t1-row"><td><strong>Benchmark</strong></td>')
    for horizon in horizon_order:
        bench = (horizons[horizon].get("benchmark") or {}).get("return_pct")
        parts.append(f'<td class="num"><strong>{bench:+,.1f}%</strong></td>' if bench is not None else '<td>-</td>')
    parts.append("</tr></table>")

    # One stock may appear in several research sections. Display it once while
    # preserving every section membership and one identical price result per
    # horizon.
    ticker_sections: dict[str, list[str]] = {}
    stock_order: list[str] = []
    for key in order:
        for ticker in section_tickers.get(key) or []:
            ticker = (ticker or "").upper()
            if not ticker:
                continue
            if ticker not in ticker_sections:
                ticker_sections[ticker] = []
                stock_order.append(ticker)
            ticker_sections[ticker].append(definitions.get(key) or key)

    positions: dict[str, dict[str, dict]] = {ticker: {} for ticker in stock_order}
    for horizon in horizon_order:
        for key in order:
            section = ((horizons[horizon].get("sections") or {}).get(key) or {})
            for row in section.get("positions") or []:
                ticker = (row.get("ticker") or "").upper()
                if ticker in positions and horizon not in positions[ticker]:
                    positions[ticker][horizon] = row

    parts.append("""
  <h3 style="color:#7b3f00">Every unique stock surfaced in a visible 2022 section</h3>
  <div class="jnote">Returns are price returns from the first tradable close after the anchor to the last close on or
  before each horizon. Cash dividends and demerger consideration are not included. "Not tradable" means the stock did
  not have a usable close within 10 calendar days of the anchor; it is never credited to the cohort.</div>
  <table class="jtable forward-table"><tr><th>Ticker</th><th>2022 section membership</th>
""")
    for horizon in horizon_order:
        parts.append(f'<th class="num">{horizon_labels[horizon]}</th>')
    parts.append("<th>Latest measured window / adjustment</th></tr>")
    for ticker in stock_order:
        latest = positions[ticker].get("to_latest") or {}
        memberships = "; ".join(dict.fromkeys(ticker_sections[ticker]))
        adjustment_count = len(latest.get("exchange_adjustments") or [])
        if latest.get("status", "").startswith("measured"):
            window = f'{fmt_date(latest.get("entry_date"))} to {fmt_date(latest.get("exit_date"))}'
            if adjustment_count:
                window += f'; {adjustment_count} corporate-action adjustment(s)'
        else:
            window = _forward_value(latest)
            event_date = latest.get("first_available_date") or latest.get("last_available_date") or latest.get("date")
            if event_date:
                window += f'; {fmt_date(event_date)}'
        parts.append(f'<tr><td class="ticker">{escape(ticker)}</td><td style="font-size:8.5px">{escape(memberships)}</td>')
        for horizon in horizon_order:
            parts.append(f'<td class="num">{escape(_forward_value(positions[ticker].get(horizon)))}</td>')
        parts.append(f'<td style="font-size:8.5px">{escape(window)}</td></tr>')
    parts.append("</table>")

    parts.append(f"""
  <h3 style="color:#7b3f00">Method and interpretation limits</h3>
  <div class="note-box"><strong>Selection:</strong> {escape(method.get("selection") or "-")}<br>
  <strong>Outcome:</strong> {escape(method.get("outcomes") or "-")}<br>
  <strong>Tradability:</strong> {escape(method.get("tradability") or "-")}<br>
  <strong>Corporate actions:</strong> {escape(method.get("corporate_actions") or "-")}<br>
  <strong>Return type:</strong> price return, not total return. Survivorship, section overlap, tiny cohorts and the single
  2022 anchor make this a diagnostic, not proof of a repeatable investment strategy.</div>
</div>
""")

    return "".join(parts)


def render_final_decision(decision: dict, opportunity_mechanisms: dict | None = None) -> str:
    """Render the one source of truth produced by the hard-gate layer."""
    parts = [f"""
<div class="judgment-box" style="border-color:#1b5e20;border-left-color:#1b5e20;background:#f4faf4">
  <h2 style="color:#1b5e20">As-of decision — the only section with position authority</h2>
  <div class="jnote">{decision.get('method', '')}</div>
"""]
    summary = decision.get("decision_summary") or {}
    detection_coverage = decision.get("constraint_detection_coverage") or {}
    verdicts = decision.get("stock_verdicts") or []
    parts.append(f"""
  <h3 style="color:#1b5e20">Position authority — do not infer an action from any other report section</h3>
  <div class="jnote">{escape(summary.get("position_authority_policy") or "Only this decision card determines whether a stock has position authority.")}</div>
""")
    if detection_coverage:
        parts.append(
            f'<div class="jnote"><strong>Constraint-detection coverage:</strong> '
            f'{escape(str(detection_coverage.get("constraint_universe_count") or 0))} chains reviewed; '
            f'{escape(str(detection_coverage.get("reference_detected_count") or 0))} came from dated '
            f'capacity/import/ledger references and {escape(str(detection_coverage.get("upstream_pipeline_detected_count") or 0))} '
            f'from the country-scoped ingestion/NLP pipeline; {escape(str(detection_coverage.get("reference_only_count") or 0))} '
            f'need an exact listed-company supplier map. '
            f'{escape(detection_coverage.get("method") or "")}</div>'
        )
        parts.append(
            f'<div class="jnote"><strong>Decision-evidence coverage health: '
            f'{escape(detection_coverage.get("coverage_health") or "UNKNOWN")}</strong> · '
            f'{escape(str(detection_coverage.get("decision_evidence_coverage_pct") or 0))}% of displayed constraints '
            f'have structured constraint evidence or two-company exact-role catalyst triangulation; '
            f'{escape(str(detection_coverage.get("strict_company_role_packet_count") or 0))} have a strict operating/pipeline role packet.</div>'
        )
    actionable = [row for row in verdicts if row.get("state") in {
        "IC_HANDOFF", "CONDITIONAL_STARTER", "DISCOVERY_STARTER",
    }]
    parts.append("""
  <h3 style="color:#1b5e20">Final investable shortlist</h3>
  <table class="jtable"><tr><th>Stock</th><th>Exact constraint</th><th>Lane</th><th>Maximum action now</th><th>Confirmation / invalidation</th></tr>
""")
    if actionable:
        for row in actionable:
            state = row.get("state") or ""
            cap = row.get("max_portfolio_weight_pct")
            action = row.get("position_authority") or "—"
            if cap is not None:
                action = f"Maximum {float(cap):.1f}% portfolio weight. " + action
            confirm = row.get("confirmation_event") or row.get("next_gate") or "—"
            invalidation = row.get("invalidation")
            if invalidation:
                confirm += "; invalidate: " + invalidation
            if row.get("holding_horizon"):
                confirm += "; horizon: " + row["holding_horizon"]
            parts.append(
                f'<tr><td><strong>{escape(row.get("ticker") or "—")}</strong></td>'
                f'<td>{escape(", ".join(row.get("constraints") or []) or "—")}</td>'
                f'<td><span class="{"cat-core" if state == "IC_HANDOFF" else "cat-timing"}">'
                f'{escape(state.replace("_", " "))}</span></td>'
                f'<td>{escape(action)}</td><td>{escape(confirm)}</td></tr>'
            )
    else:
        parts.append(
            '<tr><td colspan="5"><strong>NO ALLOCATION FROM THIS STRATEGY.</strong> '
            'Keep the constraint-strategy capital in cash/T-bills; every company below is research-only.</td></tr>'
        )
    parts.append("</table>")
    if decision.get("discovery_starter_candidates"):
        current_cap = float(summary.get("discovery_maximum_new_capital_pct") or 0)
        sleeve_cap = float(decision.get("discovery_sleeve_max_portfolio_weight_pct") or 5.0)
        parts.append(
            f'<div class="jnote"><strong>Maximum new capital from this report:</strong> {current_cap:.1f}% across the names above. '
            f'<strong>Hard Discovery sleeve ceiling:</strong> {sleeve_cap:.1f}% aggregate. '
            f'The unused {max(0.0, sleeve_cap - current_cap):.1f}% remains cash/T-bills; '
            'Quarantined, raw-screen, producer-universe, policy and PLI rows cannot fill unused capacity.</div>'
        )

    parts.append("""
  <h3>Complete decision ledger</h3>
  <table class="jtable">
    <tr><th>Raw rank</th><th>Stock</th><th>Constraint</th><th>Decision state</th><th>What is permitted now</th><th>Exact next gate</th></tr>
""")
    if verdicts:
        # The machine-readable decision ledger retains every raw-screen name.
        # Keep the PDF decision card readable: show every possible action and
        # only the first research-only rows, while the full raw table later in
        # the report repeats each name's gate status.
        action_rows = [row for row in verdicts if row.get("state") != "RESEARCH_ONLY"]
        research_rows = [row for row in verdicts if row.get("state") == "RESEARCH_ONLY"]
        displayed_verdicts = action_rows + research_rows[:12]
        for row in displayed_verdicts:
            state = row.get("state") or "RESEARCH_ONLY"
            state_class = (
                "cat-core" if state == "IC_HANDOFF" else
                "cat-timing" if state in {"CONDITIONAL_STARTER", "DISCOVERY_STARTER"} else
                "cat-watch" if state == "UNDERWRITE" else "cat-avoid"
            )
            parts.append(
                f'<tr><td>#{escape(str(row.get("raw_screen_rank") or "—"))}</td>'
                f'<td><strong>{escape(row.get("ticker") or "—")}</strong><br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("company") or "")}</span></td>'
                f'<td>{escape(", ".join(row.get("constraints") or []) or "—")}</td>'
                f'<td><span class="{state_class}">{escape(state.replace("_", " "))}</span><br>'
                f'<span style="font-size:8.5px;color:#666">{escape(row.get("action") or "—")}</span></td>'
                f'<td>{escape(row.get("position_authority") or "—")}</td>'
                f'<td>{escape(row.get("next_gate") or "—")}</td></tr>'
            )
        if len(research_rows) > len(research_rows[:12]):
            parts.append(
                f'<tr><td colspan="6"><span class="cat-avoid">RESEARCH ONLY</span> '
                f'{len(research_rows) - 12} additional raw-screen names are not shown here; each is marked '
                f'with its evidence-gate status in the raw candidate diagnostics and has no position authority.</td></tr>'
            )
    else:
        parts.append('<tr><td colspan="6">No stock has cleared an evidence lane at this report date. Research queues below have no position authority.</td></tr>')
    parts.append("</table>")

    constraint_decisions = decision.get("constraint_decisions") or []
    if constraint_decisions:
        parts.append("""
  <h3 style="color:#1b5e20">Constraint-to-company decision map</h3>
  <div class="jnote">The producer universe is separated from the investable action. A verified producer, pipeline lead, or mapper match is not a Buy unless the named stock state above says so.</div>
  <table class="jtable">
    <tr><th>Constraint / theme</th><th>Constraint state</th><th>Corresponding companies</th><th>What is permitted now</th><th>Exact next proof</th></tr>
""")
        for row in constraint_decisions:
            company_groups = []
            for label, tickers in (
                ("IC hand-off", row.get("ic_handoff_tickers") or []),
                ("conditional starter", row.get("conditional_starter_tickers") or []),
                ("discovery starter", row.get("discovery_starter_tickers") or []),
                ("underwrite", row.get("underwrite_tickers") or []),
                ("verified producer", row.get("verified_producer_tickers") or []),
                ("exact mapper role", row.get("exact_mapper_role_tickers") or []),
                ("pipeline/direct role", row.get("pipeline_or_direct_role_tickers") or []),
                ("quarantined exception", row.get("quarantined_exception_tickers") or
                 row.get("unverified_company_lead_tickers") or []),
            ):
                unique = list(dict.fromkeys(t for t in tickers if t))
                if unique:
                    company_groups.append(f'{label}: {", ".join(unique[:8])}')
            state = row.get("state") or "RESEARCH_ONLY"
            state_class = (
                "cat-core" if state == "IC_HANDOFF" else
                "cat-timing" if state in {"CONDITIONAL_STARTER", "DISCOVERY_STARTER"} else
                "cat-watch" if state in {"UNDERWRITE", "MAP_COMPANIES", "RESEARCH_ONLY"} else "cat-avoid"
            )
            themes = ", ".join(row.get("themes") or []) or "—"
            quality = (row.get("measurement_display_label") or
                       row.get("physical_quality_grade") or "measurement coverage unavailable")
            priority = row.get("research_priority_score")
            parts.append(
                f'<tr><td><strong>{escape(row.get("constraint") or "—")}</strong><br><span style="font-size:8.5px;color:#666">'
                f'{escape(themes)} · physical quality {escape(quality)}'
                + (f' · research priority {escape(str(priority))}' if priority is not None else "")
                + '</span></td>'
                f'<td><span class="{state_class}">{escape(state.replace("_", " "))}</span></td>'
                f'<td>{escape("; ".join(company_groups) or "No exact producer/company linkage recovered")}</td>'
                f'<td>{escape(row.get("position_authority") or "—")}</td>'
                f'<td>{escape(row.get("next_proof") or "—")}</td></tr>'
            )
        parts.append("</table>")

    parts.append("""
  <h3 style="color:#1b5e20">Constraint evidence</h3>
  <table class="jtable">
    <tr><th>Constraint</th><th>Dated constraint ledger</th><th>Physical quality / evidence completeness</th><th>Supply-response clock</th><th>Cohort diagnostic</th><th>Verified makers</th><th>Research status</th></tr>
""")
    for constraint in decision.get("constraints") or []:
        makers = constraint.get("verified_current_makers") or []
        physical = constraint.get("physical_evidence_status") or "physical evidence incomplete"
        quality = constraint.get("constraint_quality") or {}
        ledger = constraint.get("constraint_ledger") or {}
        queued = constraint.get("automated_observation_queue") or {}
        clock = constraint.get("resolution_clock") or {}
        cohort = constraint.get("cohort_quality_diagnostic") or {}
        maker_read = (", ".join(m.get("ticker") or "" for m in makers[:4])
                      if makers else "—")
        missing = ", ".join(quality.get("missing_legs") or [])
        selection_veto = quality.get("selection_veto")
        completeness = quality.get("evidence_completeness") or {}
        measurement = quality.get("measurement_coverage") or {}
        diversity = quality.get("source_diversity") or {}
        diversity_read = diversity.get("status")
        quality_read = (f'<strong>{escape(measurement.get("display_label") or quality.get("grade") or "measurement coverage unavailable")}</strong> — '
                        f'{escape(quality.get("physical_quality_verdict") or physical)}'
                        + (f'<br><span style="font-size:8.5px;color:#666">Measurement task: '
                           f'{escape(measurement.get("next_action") or "—")}</span>' if measurement else "")
                        + f'<br><span style="font-size:8.5px;color:#666">evidence completeness: '
                        f'{escape(completeness.get("level") or "THIN")} · {escape(completeness.get("basis") or physical)}</span>'
                        + (f'<br><span style="font-size:8.5px;color:#666">source diversity: {escape(diversity_read)}</span>'
                           if diversity_read else "")
                        + (f'<br><span style="font-size:8.5px;color:#666">Missing: {escape(missing)}</span>' if missing else "")
                        + (f'<br><span style="font-size:8.5px;color:#b35b09">Selection veto: {escape(selection_veto)}</span>' if selection_veto else ""))
        if not ledger.get("matched"):
            alias_status = ledger.get("alias_status") or "UNCOVERED"
            ledger_read = (f'<strong>{escape(alias_status)}</strong><br><span style="font-size:8.5px;color:#666">'
                           f'{escape(ledger.get("status") or "No dated ledger record linked.")}</span>')
        else:
            ledger_read = (f'<strong>{escape(ledger.get("state") or "DISCOVERY")}</strong> · '
                           f'{escape(ledger.get("match_scope") or "NONE")} match'
                           f'<br><span style="font-size:8.5px;color:#666">{escape(ledger.get("status") or "")}</span>')
            if ledger.get("latest_source_date"):
                ledger_read += (f'<br><span style="font-size:8.5px;color:#666">latest source '
                                f'{fmt_date(str(ledger.get("latest_source_date")))}</span>')
            if ledger.get("measurement_date"):
                ledger_read += (f'<br><span style="font-size:8.5px;color:#666">latest physical measure '
                                f'{fmt_date(str(ledger.get("measurement_date")))}</span>')
            if ledger.get("next_validation_date"):
                ledger_read += (f'<br><span style="font-size:8.5px;color:#666"> · next review '
                                f'{fmt_date(str(ledger.get("next_validation_date")))}</span>')
            ledger_metrics = []
            if ledger.get("domestic_capacity") is not None:
                ledger_metrics.append(
                    f'domestic capacity {ledger.get("domestic_capacity")} {ledger.get("capacity_unit") or ""}'.strip()
                )
            if ledger.get("demand_volume") is not None:
                ledger_metrics.append(
                    f'demand {ledger.get("demand_volume")} {ledger.get("demand_unit") or ""}'.strip()
                )
            if ledger.get("import_value") is not None:
                ledger_metrics.append(
                    f'import value {ledger.get("import_value")} {ledger.get("import_value_unit") or ""}'.strip()
                )
            if ledger.get("primary_origin"):
                origin = f'primary origin {ledger.get("primary_origin")}'
                if ledger.get("primary_origin_ratio") is not None:
                    origin += f' ({float(ledger["primary_origin_ratio"]) * 100:.0f}% of recorded imports)'
                ledger_metrics.append(origin)
            if ledger_metrics:
                ledger_read += (f'<br><span style="font-size:8.5px;color:#555">'
                                f'{escape("; ".join(ledger_metrics))}</span>')
        if queued.get("pending_count"):
            types = ", ".join(queued.get("observation_types") or [])
            ledger_read += (f'<br><span style="font-size:8.5px;color:#b35b09">'
                            f'{queued.get("pending_count")} queued automated observation(s): '
                            f'{escape(types)} — source review required; not Buy-gate evidence.</span>')
        capacity = ", ".join(
            f'{event.get("ticker")} {event.get("capacity")}'
            for event in (clock.get("disclosed_capacity_events") or [])[:2]
        )
        resolution_read = escape(clock.get("status") or "no resolution clock")
        resolution_read += (f'<br><span style="font-size:8.5px;color:#666">'
                            f'{escape(clock.get("supply_response_state") or "UNKNOWN")} · '
                            f'{escape(clock.get("stated_resolution_horizon") or "unknown")}</span>')
        if capacity:
            resolution_read += f'<br><span style="font-size:8.5px;color:#666">{escape(capacity)}</span>'
        payoff = cohort.get("median_2y_cohort_return_pct")
        breadth = cohort.get("median_pct_cohort_gt_100pct")
        cohort_read = (f'{escape(cohort.get("durability_status") or "insufficient_history")}'
                       + (f'<br><span style="font-size:8.5px;color:#666">median 2y cohort {payoff:+.1f}% · '
                          f'>{"100"}% names {breadth:.0f}%</span>'
                          if payoff is not None and breadth is not None else
                          '<br><span style="font-size:8.5px;color:#666">no completed multi-vintage payoff record</span>'))
        parts.append(
            f'<tr><td><strong>{constraint.get("constraint", "—")}</strong></td>'
            f'<td>{ledger_read}</td><td>{quality_read}</td><td>{resolution_read}</td><td>{cohort_read}</td><td>{maker_read}</td>'
            f'<td>{constraint.get("stock_selection_status", "—")}</td></tr>')
    parts.append("</table>")

    underwriting_candidates = decision.get("underwriting_candidates") or []
    if underwriting_candidates:
        parts.append("""
  <h3 style="color:#8a5a00">Core underwriting research candidates — not a Buy</h3>
  <div class="jnote">These companies pass the physical-constraint, focused-exposure, mapped-role, order-book, and risk screens,
  but their fresh product-specific operating-maker evidence has not yet been recovered. They require an evidence refresh and
  normal underwriting before any investment decision.</div>
  <table class="jtable"><tr><th>Ticker</th><th>Mapped role / constraint</th><th>Required before an investment decision</th><th>Raw-screen rank</th></tr>
""")
        for row in underwriting_candidates:
            products = row.get("mapper_corroborated_products") or row.get("decision_grade_products") or row.get("products") or []
            parts.append(
                f'<tr><td><span class="cat-watch">UNDERWRITE</span> <strong>{escape(row.get("ticker") or "—")}</strong></td>'
                f'<td>{escape(", ".join(products))}</td>'
                f'<td>Refresh a dated, product-specific operating-maker evidence packet; then perform normal portfolio underwriting.</td>'
                f'<td>#{escape(str(row.get("raw_screen_rank") or "—"))}</td></tr>')
        parts.append("</table>")

    priorities = decision.get("priorities") or []
    if priorities:
        parts.append("""
  <h3 style="color:#1b5e20">Investment-committee candidates — full-evidence hand-off</h3>
  <table class="jtable"><tr><th>Ticker</th><th>As-of constraint</th><th>Entry check / sizing</th><th>Raw-screen rank</th></tr>
""")
        for row in priorities:
            entry = "available" if row.get("technical") else "required before any position"
            entry += "; " + (row.get("position_guidance") or "stage position")
            parts.append(
                f'<tr><td><span class="cat-core">IC CANDIDATE</span> <strong>{row["ticker"]}</strong></td>'
                f'<td>{", ".join(row.get("verified_products") or row.get("products") or [])}</td>'
                f'<td>{entry}</td><td>#{row.get("raw_screen_rank", "—")}</td></tr>')
        parts.append("</table>")
    else:
        parts.append("""
  <div class="note-box"><strong>No company clears the strict investment-committee evidence hand-off.</strong>
  This does not erase earlier opportunities: underwriting research and qualifying Early/Timing candidates are evaluated separately below.
  The report never converts an unverified mapped association into an investment recommendation.</div>
""")

    early_candidates = decision.get("early_timing_candidates") or []
    if early_candidates:
        parts.append("""
  <h3 style="color:#b35b09">Early / Timing candidates — capped starter after separate underwriting</h3>
  <div class="jnote">This lane requires an A/B measured physical constraint as of the run date. Every row also has
  a dated, exact company-product corroboration, a direct/critical supply-side role, an order-book or repeated-capex
  catalyst, normal risk, and a stated confirmation event. It deliberately does not require the later fresh
  operating-maker packet used by the investment-committee lane. It is not an automatic purchase;
  generic policy mentions alone cannot enter.</div>
  <table class="jtable"><tr><th>Ticker</th><th>Constraint / role</th><th>Why it qualifies now</th><th>Position and confirmation</th></tr>
""")
        for row in early_candidates:
            catalysts = "; ".join(row.get("catalysts") or [])
            parts.append(
                f'<tr><td><span class="cat-watch">EARLY / TIMING</span> <strong>{escape(row.get("ticker") or "—")}</strong><br>'
                f'<span style="font-size:8.5px;color:#666">physical quality {escape(row.get("constraint_quality_grade") or "UNMEASURED")} · '
                f'evidence {escape(row.get("constraint_evidence_completeness") or "THIN")} · '
                f'raw rank #{escape(str(row.get("raw_screen_rank") or "—"))}</span></td>'
                f'<td>{escape(row.get("constraint") or "—")}<br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("role") or "—")}</span></td>'
                f'<td>{escape(row.get("category") or "Early candidate")}<br><span style="font-size:8.5px;color:#666">'
                f'{escape(catalysts)}</span></td>'
                f'<td>{escape(row.get("position_action") or "starter only")}<br><span style="font-size:8.5px;color:#666">'
                f'Confirmation event: {escape(row.get("confirmation_event") or row.get("next_gate") or "—")}<br>'
                f'Evidence refresh: {escape(row.get("next_gate") or "—")}</span></td></tr>')
        parts.append("</table>")

    discovery_candidates = decision.get("discovery_starter_candidates") or []
    if discovery_candidates:
        parts.append("""
  <h3 style="color:#b35b09">Discovery starters — proof-led experimental sleeve</h3>
  <div class="jnote">This is not Core and does not pretend an unmeasured chain is measured. A row needs dated
  binding-demand evidence plus either structured constraint evidence or exact-role catalysts from at least two independent
  companies. Every stock also needs an exact operating/pipeline role or corroborated own-filing maker role for the same product,
  a same-product catalyst, and NORMAL risk. Maximum five names and 5% of the portfolio in aggregate; unused capacity stays in cash.</div>
  <table class="jtable"><tr><th>Ticker</th><th>Exact constraint / producer state</th><th>As-of catalyst</th><th>Cap, confirmation and invalidation</th></tr>
""")
        for row in discovery_candidates:
            parts.append(
                f'<tr><td><span class="cat-timing">DISCOVERY STARTER</span> '
                f'<strong>{escape(row.get("ticker") or "—")}</strong><br>'
                f'<span style="font-size:8.5px;color:#666">raw rank #{escape(str(row.get("raw_screen_rank") or "—"))} · '
                f'risk {escape(row.get("risk_tier") or "—")}</span></td>'
                f'<td>{escape(row.get("constraint") or "—")}<br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("producer_state") or "—")} · physical quality '
                f'{escape(row.get("constraint_quality_grade") or "UNMEASURED")} · '
                f'{escape(row.get("constraint_confirmation_state") or "—")}</span></td>'
                f'<td>{escape("; ".join(row.get("catalysts") or []) or "—")}<br>'
                f'<span style="font-size:8.5px;color:#666">Why not Core: {escape(row.get("why_not_core") or "—")}</span></td>'
                f'<td>{escape(row.get("position_action") or "—")}<br>'
                f'<span style="font-size:8.5px;color:#666">Confirm: {escape(row.get("confirmation_event") or "—")}<br>'
                f'Invalidate: {escape(row.get("invalidation") or "—")}<br>'
                f'Horizon: {escape(row.get("holding_horizon") or "—")}<br>'
                f'Review: {escape(row.get("review_frequency") or "—")}</span></td></tr>'
            )
        parts.append("</table>")

    policy_watchlist = decision.get("policy_research_watchlist") or []
    if policy_watchlist:
        parts.append("""
  <h3>Emerging policy research leads — not a Buy</h3>
  <div class="jnote">These are company-owned, dated policy disclosures that the constraint mapper may not yet cover.
  They are intentionally shown for analyst research, but policy activity cannot establish product exposure, earnings
  capture, or an investment recommendation by itself.</div>
  <table class="jtable"><tr><th>Ticker</th><th>Policy signal</th><th>As-of evidence</th><th>Required before any Buy</th></tr>
""")
        for row in policy_watchlist:
            documents = row.get("recent_company_documents") or 0
            commits = row.get("commitment_documents") or 0
            evidence = f'{documents} recent company filing(s)'
            if commits:
                evidence += f'; {commits} commitment-stage disclosure(s)'
            parts.append(
                f'<tr><td><strong>{escape(row.get("ticker") or "—")}</strong><br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("raw_screen_coverage") or "—")}</span></td>'
                f'<td>{escape(row.get("scheme") or "—")}<br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("signal_state") or "—")}</span></td>'
                f'<td>{escape(evidence)}<br><span style="font-size:8.5px;color:#666">first seen '
                f'{fmt_date(row.get("first_mention"))}</span></td>'
                f'<td>{escape(row.get("next_gate") or "—")}</td></tr>')
        parts.append("</table>")

    # Economic mechanism matters before a company is called a beneficiary.
    # A domesticisation or demand-deployment lead can be highly investable, but
    # it is not a physical scarcity claim and therefore has different evidence
    # requirements.  These lanes are intentionally research-only here.
    opportunity_mechanisms = opportunity_mechanisms or {}
    for key, title, note in (
        (
            "localisation_or_qualification",
            "Domesticisation / qualification company leads — research only",
            "Import dependence can create a local opportunity without a physical shortage. "
            "Each name still needs proof of approved domestic capacity, qualification, and product-level earnings capture.",
        ),
        (
            "policy_led_deployment_demand",
            "Policy-led deployment company leads — research only",
            "A company policy disclosure can precede a demand wave, but it does not prove the cited product, award conversion, or earnings capture.",
        ),
    ):
        rows = opportunity_mechanisms.get(key) or []
        if not rows:
            continue
        parts.append(f"""
  <h3>{escape(title)}</h3>
  <div class=\"jnote\">{escape(note)}</div>
  <table class=\"jtable\"><tr><th>Ticker</th><th>Economic mechanism</th><th>Why research now</th><th>Required before any Buy</th></tr>
""")
        for row in rows:
            anchor = row.get("constraint_or_product") or row.get("scheme") or "—"
            catalysts = "; ".join(row.get("catalysts") or []) or "no company catalyst recovered"
            missing = "; ".join(row.get("missing_proof") or []) or "normal portfolio underwriting and entry review"
            parts.append(
                f'<tr><td><strong>{escape(row.get("ticker") or "—")}</strong><br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("role_status") or "role proof incomplete")}</span></td>'
                f'<td>{escape(anchor)}<br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("mechanism") or "—")}</span></td>'
                f'<td>{escape(catalysts)}<br><span style="font-size:8.5px;color:#666">'
                f'{escape(row.get("research_route") or "—")}</span></td>'
                f'<td>{escape(missing)}</td></tr>'
            )
        parts.append("</table>")

    research_queue = decision.get("best_stock_research_queue") or []
    if research_queue:
        parts.append("""
  <h3>Best proof-led stocks to research</h3>
  <div class="jnote">A stock enters this queue only through exact producer/pipeline evidence. It is not a Buy list:
  the remaining constraint-quality and earnings-capture gate is printed beside each name.</div>
  <table class="jtable"><tr><th>Ticker</th><th>Constraint / role</th><th>Why it belongs</th><th>Required before any Buy</th></tr>
""")
        for row in research_queue:
            status = escape(row.get("research_status") or "research lead")
            direction = escape(row.get("benefit_direction") or "earnings direction unproved")
            role = escape(row.get("role_state") or row.get("maker_status") or "—")
            parts.append(f'<tr><td><strong>{escape(row.get("ticker") or "—")}</strong><br>'
                         f'<span style="font-size:8.5px;color:#666">physical quality {escape(row.get("constraint_quality_grade") or "UNMEASURED")} · '
                         f'{escape(row.get("screen_coverage") or "proof-led")}</span></td>'
                         f'<td>{escape(row.get("constraint") or "—")}<br><span style="font-size:8.5px;color:#666">{role}</span></td>'
                         f'<td>{direction}<br><span style="font-size:8.5px;color:#666">{status}</span></td>'
                         f'<td>{escape(row.get("next_gate") or "—")}</td></tr>')
        parts.append("</table>")

    missed = decision.get("unmapped_maker_leads") or []
    if missed and not research_queue:
        parts.append("""
  <h3>Verified makers missed by the raw screen</h3>
  <table class="jtable"><tr><th>Ticker</th><th>Constraint</th><th>Latest dated maker proof</th><th>Status</th></tr>
""")
        for row in missed:
            parts.append(f'<tr><td><strong>{row["ticker"]}</strong></td>'
                         f'<td>{row.get("constraint", "—")}</td>'
                         f'<td>{fmt_date(row.get("last_evidence_date"))}</td>'
                         f'<td>{row.get("status", "—")}</td></tr>')
        parts.append("</table>")

    pipeline_leads = decision.get("unmapped_pipeline_leads") or []
    if pipeline_leads and not research_queue:
        parts.append("""
  <h3>Early producer-capacity leads missed by the raw screen</h3>
  <div class="jnote">These are dated, company-owned capacity/direct-role disclosures known at the report date.
  They are a research queue only: a plan is not proof of commissioning, earnings capture, or a Buy.</div>
  <table class="jtable"><tr><th>Ticker</th><th>Constraint</th><th>As-of role / state</th><th>Latest dated proof</th><th>Next validation</th></tr>
""")
        for row in pipeline_leads:
            parts.append(f'<tr><td><strong>{row["ticker"]}</strong></td>'
                         f'<td>{row.get("constraint", "—")}</td>'
                         f'<td>{row.get("maker_status", "—")}</td>'
                         f'<td>{fmt_date(row.get("last_evidence_date"))}</td>'
                         f'<td>{row.get("research_route", "commissioning / capacity verification")}</td></tr>')
        parts.append("</table>")

    blocked = decision.get("blocked_candidates") or []
    if blocked:
        parts.append("""
  <h3>Raw-screen names blocked by evidence gates</h3>
  <table class="jtable"><tr><th>Raw rank</th><th>Ticker</th><th>Blocked because</th></tr>
""")
        for row in blocked[:8]:
            reasons = "; ".join(row.get("failed_gates") or []) or "not eligible"
            parts.append(f'<tr><td>#{row.get("raw_screen_rank", "—")}</td>'
                         f'<td><strong>{row.get("ticker", "—")}</strong></td><td>{reasons}</td></tr>')
        parts.append("</table>")
    parts.append("</div>")
    return "".join(parts)


def render(doc: dict, as_of_str: str) -> str:
    country = doc.get("country", "IN")
    as_of_dt = datetime.strptime(as_of_str, "%Y-%m-%d")
    as_of_label = as_of_dt.strftime("%d-%b-%Y")
    snapshot_label = as_of_label

    major_themes = doc.get("major_themes", [])
    emerging_themes = doc.get("emerging_themes", [])
    candidates = doc.get("ranked_candidates", [])
    evidence = doc.get("evidence_dashboard", [])
    overlap = doc.get("cross_theme_overlap", [])
    bear_cases = doc.get("bear_cases", [])
    regime = doc.get("market_regime", {})
    supply = doc.get("supply_side_beneficiaries", [])
    final_decision = doc.get("final_decision") or {}

    # Identify exclusions (from reference_data — the mg_manual_exclusions table)
    manual_exclude = get_manual_exclusions(doc)
    excluded_tickers = {t for t in manual_exclude if any(c["ticker"] == t for c in candidates)}

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
    <div>Selection snapshot: <span>{snapshot_label}</span></div>
  </div>
</div>
""")

    validation = doc.get("validation") or {}
    validation_status = validation.get("status") or "UNVALIDATED_VERSION"
    validation_ok = validation_status.startswith("VALIDATED") or validation_status.startswith("PROMISING")
    validation_color = "#2c5f2d" if validation_ok else "#9b2c2c"
    validation_bg = "#eef8f0" if validation_ok else "#fff1f1"
    logic_hash = (((doc.get("report_manifest") or {}).get("logic") or {}).get("logic_hash") or "")[:12]
    parts.append(f"""
<div style="border-left:5px solid {validation_color};background:{validation_bg};padding:8px 12px;margin:8px 0">
  <strong style="color:{validation_color}">{escape(validation_status)}</strong>
  <span style="font-size:9.5px;color:#555"> — {escape(validation.get('reason') or '')}
  Logic {escape(logic_hash or 'not recorded')}. The final as-of decision is the only action-authority section.</span>
</div>
""")

    if final_decision:
        parts.append(render_final_decision(final_decision, doc.get("opportunity_mechanisms")))

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

    # ── NARRATIVE EMERGENCE — research triage, not binding-state evidence. ──
    emerging_cons = doc.get("greatest_emerging_constraints") or []
    if emerging_cons:
        def _stocks_for(c):
            return {"verified": c.get("stocks_verified") or [],
                    "pipeline": c.get("stocks_pipeline") or []}
        yr = (as_of_str or "")[:4]
        parts.append('<div class="section"><h2 style="color:#1a3a5c">'
                     f'Fastest-rising constraint signals in {escape(yr)} — research queue</h2>')
        parts.append("""<p style="font-size:9.5px;color:#666;margin:2px 0 8px">
Ranked by <strong>year-over-year acceleration</strong> in dated shortage / bottleneck / capex / order /
localisation filing signals (point-in-time), <strong>diversified across mechanism families</strong> so the
list reflects the breadth of filing attention, not one sector. Acceleration does <strong>not</strong> establish
a measured or binding physical shortage. Company names are role context only; the independently derived physical
state and final as-of decision determine whether any position is permitted.</p>""")
        _traj_badge = {
            "EARLY_INFLECTION": ('<span style="background:#1b6e3c;color:#fff;font-size:8px;padding:1px 4px;'
                    'border-radius:3px" title="Caught on leading (capex/shortage/localisation) '
                    'signals before broad discussion — earliest lead time">★ EARLY INFLECTION</span>'),
            "NEW": ('<span style="background:#b3541e;color:#fff;font-size:8px;padding:1px 4px;'
                    'border-radius:3px">NEW THIS YEAR</span>'),
            "NEW_INFLECTION": ('<span style="background:#b3541e;color:#fff;font-size:8px;padding:1px 4px;'
                               'border-radius:3px">NEW INFLECTION</span>'),
            "RE_ACCELERATING": ('<span style="background:#8a5a16;color:#fff;font-size:8px;padding:1px 4px;'
                                'border-radius:3px">RE-ACCELERATING</span>'),
            "SUSTAINED": ('<span style="background:#555;color:#fff;font-size:8px;padding:1px 4px;'
                          'border-radius:3px">STRUCTURAL</span>'),
        }
        parts.append("""<table><tr>
  <th>#</th><th>Emerging signal</th><th>Narrative trajectory</th><th>Physical state</th>
  <th class="num">YoY</th><th>Company-role context — not a Buy</th></tr>""")
        for i, c in enumerate(emerging_cons, 1):
            ratio = c.get("emergence_ratio") or 1.0
            yoy = f"+{int((ratio - 1) * 100)}%" if ratio and ratio >= 1 else f"{int((ratio - 1) * 100)}%"
            label = c.get("constraint") or ""
            traj = _traj_badge.get(c.get("trajectory"), "")
            physical = c.get("physical_constraint_state") or {}
            physical_label = (
                f"{physical.get('physical_state') or 'UNMEASURED'} · "
                f"Grade {physical.get('physical_quality') or '—'}"
                if physical else "No exact physical-state record"
            )
            st = _stocks_for(c)
            if st["verified"]:
                stocks = " ".join(f'<span class="ticker" style="color:#1a7a3a">{escape(t)}</span>'
                                  for t in st["verified"][:8])
                if st["pipeline"]:
                    stocks += ' <span style="color:#888;font-size:8px">· pipeline: ' + \
                              ", ".join(escape(t) for t in st["pipeline"][:5]) + "</span>"
            elif st["pipeline"]:
                stocks = ('<span style="color:#8a5a16;font-size:9px">building capacity (pre-operational): </span>'
                          + ", ".join(f'<span class="ticker">{escape(t)}</span>' for t in st["pipeline"][:6]))
            elif c.get("stocks_exposure"):
                stocks = ('<span style="color:#5c4a9e;font-size:9px">exposure (filings carry this constraint\'s '
                          'signals — verify role): </span>'
                          + ", ".join(f'<span class="ticker">{escape(t)}</span>'
                                      for t in (c.get("stocks_exposure") or [])[:8]))
            else:
                stocks = '<span style="color:#999;font-size:9px">no listed pure-play yet — watch item</span>'
            parts.append(
                f'<tr><td class="num">{i}</td>'
                f'<td><strong>{escape(label)}</strong><br>'
                f'<span style="font-size:8px;color:#888">{escape(c.get("family") or "")} · {c.get("recent_signals") or 0} signals/12m</span></td>'
                f'<td>{traj}</td><td style="font-size:9px">{escape(physical_label)}</td>'
                f'<td class="num"><strong>{yoy}</strong></td>'
                f'<td style="font-size:9.5px">{stocks}</td></tr>')
        parts.append("</table>")
        parts.append("""<p style="font-size:8.5px;color:#888;margin-top:4px">
<strong>NEW INFLECTION</strong> = the genuine year-specific signal (flat/absent before, surging now — e.g.
Green Hydrogen 2022, Power Transformer 2024). <strong>RE-ACCELERATING / STRUCTURAL</strong> = the persistent
multi-year localisation backdrop. Neither label proves a physical constraint. A listed maker is still research-only
until the exact physical state, earnings-capture and final decision gates pass.</p></div>""")

    # ── GENERIC blended constraint+company engine (trade x signal, HS-linked). ──
    blended = doc.get("blended_constraint_company_engine") or []
    if blended:
        parts.append('<div class="section"><h2 style="color:#1a3a5c">'
                     'Constraint &amp; company map — blended detection (trade + filings), one generic pass</h2>')
        parts.append("""<p style="font-size:9.5px;color:#666;margin:2px 0 8px">
One uniform engine over every tracked constraint, <strong>no per-sector rules</strong>: detection blends
<strong>customs-import acceleration</strong> (UN Comtrade, per HS code) with <strong>filing-signal emergence</strong>;
companies are linked <strong>HS-anchored</strong> (product&#8596;HS crosswalk &#8594; own-filing role ledger), never by
name/token matching. A constraint India does not yet make correctly shows <em>no company</em>. Detection is not a
physical-shortage or Buy claim; the as-of decision gates still apply.</p>""")
        parts.append("""<table><tr><th>#</th><th>Constraint (HS)</th>
<th class="num">Imports $bn</th><th class="num">Import YoY</th><th class="num">Signal</th>
<th class="num">Blended</th><th>Companies (own-filing evidence, HS-linked)</th></tr>""")
        for i, r in enumerate([b for b in blended if b.get("blended_score", 0) > 0
                               or b.get("companies")][:20], 1):
            yoy = r.get("trade_yoy_pct")
            yoy_s = (f'{yoy:+.0f}%' if yoy is not None else '—')
            cos = (", ".join(f'<span class="ticker">{escape(t)}</span>' for t in r["companies"][:8])
                   if r.get("companies")
                   else '<span style="color:#999;font-size:9px">no listed maker yet — watch</span>')
            parts.append(
                f'<tr><td class="num">{i}</td>'
                f'<td><strong>{escape(r.get("constraint") or "")}</strong> '
                f'<span style="font-size:8px;color:#888">HS {escape(str(r.get("hs_code") or ""))}</span></td>'
                f'<td class="num">{(r.get("import_bn") or 0):.2f}</td>'
                f'<td class="num">{yoy_s}</td>'
                f'<td class="num">{r.get("signal_emergence") or 0}</td>'
                f'<td class="num"><strong>{r.get("blended_score") or 0}</strong></td>'
                f'<td style="font-size:9.5px">{cos}</td></tr>')
        parts.append("</table>")
        parts.append("""<p style="font-size:8.5px;color:#888;margin-top:4px">
Blended score = 0.5&times;normalised import-acceleration + 0.5&times;filing-signal emergence. Companies come only from
the own-filing role ledger joined on HS identity — so the link is generic and auditable, and a mis-labelled
name cannot attach. Extending coverage = adding HS product-identity rows, never per-sector magnitude data.</p></div>""")

    # ── OPERATING-MAKER INVENTORY — role evidence only, never an action list. ──
    universe = doc.get("verified_maker_universe") or []
    if universe:
        total_makers = len({t for u in universe for t in (u.get("makers") or [])})
        parts.append('<div class="section"><h2 style="color:#1a5c2d">'
                     f'Operating-maker inventory — {total_makers} evidenced roles, diagnostic only</h2>')
        parts.append("""<p style="font-size:9.5px;color:#666;margin:2px 0 8px">
Every row establishes only that issuer filings support an operating product role. It does not prove the product
is constrained, that scarcity reaches earnings, or that valuation and risk gates pass. <strong>EMERGING</strong>
means filing attention accelerated; it is context, not a physical-state or return claim. Only the final as-of
decision section below has position authority.</p>""")
        parts.append("""<table><tr>
  <th>#</th><th>Product</th><th>Family</th><th class="num">Makers</th>
  <th>Signal</th><th>Verified operating makers</th></tr>""")
        for i, u in enumerate(universe, 1):
            makers = u.get("makers") or []
            mk = " ".join(f'<span class="ticker" style="color:#1a7a3a">{escape(t)}</span>' for t in makers[:12])
            tag = ""
            if u.get("is_emerging"):
                ratio = u.get("emergence_ratio") or 1.0
                tag = ('<span style="background:#b3541e;color:#fff;font-size:8px;padding:1px 4px;'
                       f'border-radius:3px">EMERGING +{int((ratio-1)*100)}%</span>')
            parts.append(
                f'<tr><td class="num">{i}</td><td><strong>{escape((u.get("product") or "").title())}</strong></td>'
                f'<td style="font-size:9px">{escape(u.get("family") or "")}</td>'
                f'<td class="num">{u.get("n_makers")}</td><td>{tag}</td>'
                f'<td style="font-size:9.5px">{mk}</td></tr>')
        parts.append("</table>")
        parts.append("""<p style="font-size:8.5px;color:#888;margin-top:4px">
Every name here has own-filing operating-maker evidence. Ordered by narrative emergence and maker count for
research triage only. A name remains non-actionable unless the exact constraint-product-company tuple appears
in the final as-of decision.</p></div>""")

    # ── INVESTMENT LIST (decision-complete, rendered FIRST) ─────────────────────
    # The one resolved answer: ranked names, each with the evidence tier it
    # actually carries and that tier's measured 3-year return. Fixes the "report
    # is vague / no clear list" gap — the strict lanes scatter names and the
    # physical-constraint lane is evidence-starved, so nothing resolved before.
    def _il_blocks(lst):
        block_html = ""
        for b in (lst or []):
            why = b.get("why_now") or []
            why_lbl = " · ".join(why[:4]) if why else "mapped constraint"
            stage = b.get("stage") or ""
            maker_html = ""
            for m in b.get("makers", []):
                pp = m.get("pure_play_confirmed")
                corr = m.get("corroborated_maker")
                badge = ('<span style="background:#2e7d32;color:#fff;padding:0 5px;border-radius:8px;font-size:8px">PURE-PLAY ✓</span>'
                         if pp else
                         '<span style="background:#1a3a5c;color:#fff;padding:0 5px;border-radius:8px;font-size:8px">MAKER ✓</span>'
                         if corr else
                         '<span style="background:#8a6d1a;color:#fff;padding:0 5px;border-radius:8px;font-size:8px">mapped</span>')
                maker_html += (
                    f'<tr><td><strong>{m["ticker"]}</strong> '
                    f'<span style="font-size:8.5px;color:#888">{(m.get("company") or "")[:30]}</span></td>'
                    f'<td>{badge}</td>'
                    f'<td style="font-size:9px;color:#666">{(m.get("industry") or "—")[:40]}</td>'
                    f'<td style="text-align:center">{"✓" if m.get("order_book") else "—"}</td></tr>')
            score = b.get("explosiveness_score")
            block_html += (
                f'<div style="margin:10px 0 4px;padding:5px 8px;background:#eef5ee;border-left:4px solid #2e7d32">'
                f'<strong style="color:#1a5c2d">#{b.get("rank","")} {b.get("display_label") or b["constraint"]}</strong> '
                f'<span style="background:#2e7d32;color:#fff;padding:0 6px;border-radius:8px;font-size:9px">'
                f'score {score}</span> '
                f'<span style="font-size:9px;color:#555">{"— " + stage if stage else ""}</span>'
                f'<div style="font-size:9px;color:#1a5c2d;margin-top:2px">▲ why: {why_lbl}</div></div>'
                f'<table class="jtable"><tr><th>Company</th><th>Role</th><th>Industry</th><th>Order book</th></tr>'
                f'{maker_html}</table>')
        return block_html

    def _il_section(lst, heading, blurb):
        return f"""
<div class="picks-box" style="border-color:#2e7d32;border-left-color:#2e7d32;background:#f4faf4">
  <h2 style="color:#1a5c2d">{heading}</h2>
  <div style="font-size:9.5px;color:#555;margin-bottom:4px">{blurb}</div>
  {_il_blocks(lst)}
</div>
"""

    _in_blurb = (
        "Constraint-first and forward-looking. Constraints are ranked by an <strong>explosiveness score</strong> built "
        "ONLY from signals visible at the report date — emergence (an emerging/accelerating theme the market hasn't "
        "crowded into), scarcity of listed makers (a research feature, not proof), structural import dependence and "
        "widening import momentum (trade-flow leading indicator), and a draft policy/mandate in flight. <strong>No "
        "forward return is used.</strong> This is a diagnostic ranking, not action authority. Under each, the companies whose own filings evidence making the product: "
        "<strong>PURE-PLAY ✓</strong>, <strong>MAKER ✓</strong>, or <strong>mapped</strong>.")
    if country == "US":
        # US splits into two complementary reads — see us_biggest / us_emerging.
        big = doc.get("us_biggest_constraints") or doc.get("final_investment_list") or []
        emg = doc.get("us_emerging_constraints") or []
        if big:
            parts.append(_il_section(
                big, "Biggest US Constraints — the largest, best-evidenced supply constraints",
                "Ranked by <strong>evidenced-issuer breadth</strong>: how many independent listed companies' own filings "
                "evidence making the product. This is the significance view — the structural US supply constraints "
                "(semiconductors, energy, medical devices), where the theme is broad and well-established. A mature "
                "(Consensus) theme is not penalised here; size and evidence lead."))
        if emg:
            parts.append(_il_section(
                emg, "Most Emerging US Constraints — concentrated, not-yet-crowded plays",
                "The about-to-explode companion view: ranked by <strong>emergence</strong> (an emerging/accelerating "
                "theme) and <strong>scarcity</strong> (few listed makers = a concentrated opportunity the market hasn't "
                "crowded into). Same evidence gate; different lens — early and narrow rather than large and established."))
    else:
        fil = doc.get("constraint_company_diagnostic") or []
        if fil:
            parts.append(_il_section(
                fil, "Constraint-company screen — research diagnostic, not an investment list",
                _in_blurb + " <strong>This screen cannot override the final as-of decision below.</strong>"))

    # ── MOONSHOT SLEEVE — REMOVED from the client report (Aug-2026) ──
    # Measured the weakest section: +46.8% median 3-year, only +9.8pp over
    # benchmark, versus +63.6% (maker lists) and +65.4% (plain beneficiary
    # basket) — the most complex machinery in the system for the smallest
    # edge. Per user decision it no longer renders. The screen and its
    # `moonshot_candidates` field still run and stay in the data JSON as an
    # internal watch-list; if it is ever reinstated as a small explicit
    # venture allocation, restore the render block from git history rather
    # than as a default report section.

    # Legacy sidecars are retained only for historical render compatibility.
    # They cannot override a selector-produced final_decision.
    decision = None if final_decision else (doc.get("_judgment") or {}).get("decision")
    if decision:
        parts.append("""
<div class="judgment-box" style="border-color:#1b5e20;border-left-color:#1b5e20;background:#f4faf4">
  <h2 style="color:#1b5e20">THE DECISION — what to actually do this month</h2>
  <div class="jnote">Only two words exist here: <strong>BUY</strong> and <strong>NO</strong>. Every "wait for X"
  from the detail sections has been resolved into one or the other — the monthly rerun re-decides everything,
  so nothing is left "pending". All supporting evidence is in the sections below; this box is the conclusion.</div>
""")
        cons = decision.get("constraints_shortlisted") or []
        if cons:
            parts.append('<h3 style="color:#1b5e20">Constraints shortlisted</h3><table class="jtable">')
            parts.append('<tr><th>Constraint</th><th>Why it made the cut</th></tr>')
            for c in cons:
                parts.append(f'<tr><td><strong>{c["name"]}</strong></td><td>{c["why"]}</td></tr>')
            parts.append('</table>')
        buys = decision.get("buys") or []
        if buys:
            parts.append('<h3 style="color:#1b5e20">BUY</h3><table class="jtable">')
            parts.append('<tr><th>Ticker</th><th>Size</th><th>Why (one line)</th></tr>')
            for b in buys:
                parts.append(f'<tr><td><span class="cat-core">BUY</span> <strong>{b["ticker"]}</strong></td>'
                             f'<td><strong>{b["size"]}</strong></td><td>{b["why"]}</td></tr>')
            parts.append('</table>')
        pw = decision.get("portfolio_weights") or []
        pp = decision.get("portfolio_params") or {}
        if pw:
            parts.append('<h3 style="color:#1b5e20">Portfolio allocation (risk-budgeted weights)</h3>'
                         '<table class="jtable"><tr><th>Ticker</th><th>Weight</th><th>Constraint bucket</th></tr>')
            for w in pw:
                bucket = w.get("constraint_bucket", "")
                bucket_disp = "—" if bucket.startswith("unmapped:") else bucket
                parts.append(f'<tr><td><strong>{w["ticker"]}</strong></td>'
                             f'<td><strong>{w["weight_pct"]}%</strong></td><td>{bucket_disp}</td></tr>')
            parts.append(f'<tr><td><strong>CASH</strong></td><td><strong>{pp.get("cash_pct","—")}%</strong></td>'
                         f'<td>capped-away weight parks here, never forced into weaker names</td></tr></table>')
            parts.append(f'<p style="font-size:9.5px;color:#666">Caps: single stock {pp.get("stock_cap_pct","—")}%, '
                         f'single constraint bucket {pp.get("bucket_cap_pct","—")}%, cash floor {pp.get("cash_floor_pct","—")}%. '
                         f'Same-constraint stocks are sized as ONE bet.</p>')
        exits = decision.get("exits") or []
        if exits:
            parts.append('<h3 style="color:#b35b09">EXIT / TRIM — positions leaving the book</h3><table class="jtable">')
            parts.append('<tr><th>Ticker</th><th>Action</th><th>Rule fired</th><th>Why</th></tr>')
            for e in exits:
                parts.append(f'<tr><td><strong>{e["ticker"]}</strong></td>'
                             f'<td><span class="cat-avoid">{e.get("action","EXIT")}</span></td>'
                             f'<td>{e.get("rule","—")}</td><td>{e.get("why","")}</td></tr>')
            parts.append('</table>')
        nos = decision.get("nos") or []
        if nos:
            parts.append('<h3 style="color:#8b0000">NO — with the exact condition that flips it to BUY</h3><table class="jtable">')
            parts.append('<tr><th>Ticker(s)</th><th>Flips to BUY when</th></tr>')
            for n in nos:
                parts.append(f'<tr><td><span class="cat-avoid">NO</span> <strong>{n["ticker"]}</strong></td>'
                             f'<td>{n["flips_when"]}</td></tr>')
            parts.append('</table>')
        if decision.get("portfolio_line"):
            parts.append(f'<div class="jportfolio"><strong>Bottom line:</strong> {decision["portfolio_line"]}</div>')
        parts.append('</div>')

    # ── PLI POLICY-TO-CONSTRAINT PIPELINE ──────────────────────────────────────
    # The old render was a phrase-count table whose green "committed" badge
    # overstated a company's policy status.  The new feed is deliberately a
    # workflow: dated own-filing evidence -> stated policy action -> constraint
    # link -> next research route.  It remains entirely separate from Buy.
    pli_pipeline = doc.get("pli_pipeline") or {}
    pli_research = pli_pipeline.get("research_leads") or []
    pli_monitor = pli_pipeline.get("monitor_leads") or []
    if pli_pipeline or pli_research or pli_monitor:
        # Client-facing policy rows must change a selected physical constraint.
        # Credible but unlinked policy activity stays in the audit/new-theme
        # monitor; otherwise a long PLI phrase screen crowds out the actual
        # constraint decision and implies a product relationship it has not proved.
        selected_pli_research = [p for p in pli_research if p.get("constraint_links")]
        selected_pli_monitor = [p for p in pli_monitor if p.get("constraint_links")]
        withheld_unlinked = (len(pli_research) + len(pli_monitor)
                            - len(selected_pli_research) - len(selected_pli_monitor))
        def _pli_row(p):
            evidence_date = fmt_date(p.get("evidence_date"))
            title = escape((p.get("evidence_title") or "Company filing")[:130])
            url = p.get("evidence_url")
            source = (f'<a href="{escape(url, quote=True)}">{title}</a>' if url else title)
            excerpt = escape((p.get("evidence_excerpt") or "")[:420])
            constraint = ", ".join(p.get("constraint_links") or []) or "No selected constraint match"
            materiality = escape(p.get("materiality") or "Not quantified in cited passage")
            materiality_kind = escape(p.get("materiality_kind") or "amount context unqualified")
            route = escape(p.get("research_route") or "Monitor milestone")
            reason = escape(p.get("route_reason") or "")
            proof = (f'<strong>{evidence_date}</strong> — {source}'
                     + (f'<br><span style="font-size:8.5px;color:#555">“{excerpt}”</span>' if excerpt else ""))
            return (f'<tr><td><strong>{escape(p.get("ticker") or "—")}</strong><br>'
                    f'<span style="font-size:8.5px;color:#666">{escape(p.get("scheme") or "PLI")}</span></td>'
                    f'<td><strong>{escape(p.get("policy_stage") or "filing mention only")}</strong><br>'
                    f'<span style="font-size:8.5px;color:#666">{materiality} — {materiality_kind}</span></td>'
                    f'<td>{escape(constraint)}</td><td>{proof}</td>'
                    f'<td><strong>{route}</strong><br><span style="font-size:8.5px;color:#666">{reason}</span></td></tr>')

        research_rows = "".join(_pli_row(p) for p in selected_pli_research)
        monitor_rows = "".join(_pli_row(p) for p in selected_pli_monitor)
        raw_count = pli_pipeline.get("raw_discovery_count", len(pli_research) + len(pli_monitor))
        archive_count = pli_pipeline.get("archive_count", 0)
        research_block = (f'<h3 style="color:#2c5f2d">Research now — evidenced action + selected constraint</h3>'
                          f'<table class="jtable"><tr><th>Company / scheme</th><th>Policy stage / materiality</th>'
                          f'<th>Constraint link</th><th>Primary filing evidence</th><th>Route</th></tr>{research_rows}</table>'
                          if research_rows else
                          '<p style="font-size:10px;color:#555;margin:8px 0"><strong>No policy lead cleared both the '
                          'company-action and selected-constraint gates.</strong> Nothing is promoted from PLI into research-now.</p>')
        monitor_block = (f'<h3 style="color:#5c4a9e">Monitor milestone — named evidence, an unfinished investment leg</h3>'
                         f'<table class="jtable"><tr><th>Company / scheme</th><th>Policy stage / materiality</th>'
                         f'<th>Constraint link</th><th>Primary filing evidence</th><th>Route</th></tr>{monitor_rows}</table>'
                         if monitor_rows else "")
        parts.append(f"""
<div class="judgment-box" style="border-color:#5c4a9e;border-left-color:#5c4a9e;background:#f7f5fc">
  <h2 style="color:#5c4a9e">Policy evidence that changes a selected constraint</h2>
  <div class="jnote">As of {fmt_date(pli_pipeline.get("as_of_date") or doc.get("as_of_date"))}: every visible row has a
  dated company filing, an exact product phrase in the same policy/action disclosure, a stated policy stage, and a
  specific next research route. A policy mention is not an award; an award is not capacity; capacity is not an
  investable stock. The table is separate from the final Buy decision.
  {raw_count} raw discovery matches were screened; {archive_count} mention-only or repetitive-evidence rows are
  retained in the data audit. {withheld_unlinked} credible-but-unlinked policy row(s) remain internal new-theme monitors,
  not client-facing constraint evidence.</div>
  {research_block}
  {monitor_block}
</div>
""")

    # ── WHO ACTUALLY MAKES THIS? ───────────────────────────────────────────────
    # A maker universe must cover every selected constraint, distinguish an
    # operating producer from an announced capacity plan, and keep rejected
    # lexical matches in the audit data rather than putting them in front of a
    # reader.  `greatest_constraints` is the report's actual research queue;
    # rendering the raw capability dictionary here used to silently omit its
    # most important constraints.
    # The headline shortlist can stay compact; this universe cannot. Omitting
    # a ninth-ranked constraint also omits every corresponding company and
    # turns a presentation choice into a false coverage conclusion.
    maker_constraints = (doc.get("constraint_company_universe")
                         or doc.get("maker_universe_constraints")
                         or doc.get("greatest_constraints") or [])
    maker_blocks = []
    for constraint in maker_constraints:
        product = constraint.get("constraint") or "Unnamed constraint"
        operating = constraint.get("verified_current_makers") or []
        pipeline = constraint.get("pipeline_or_direct_role_makers") or []
        populations = constraint.get("company_populations") or {}
        unverified_leads = (populations.get("quarantined_exceptions") or
                            populations.get("unverified_company_leads") or [])
        adjudication = populations.get("automated_adjudication") or {}
        coverage = constraint.get("maker_coverage") or {}
        measurement = ((constraint.get("constraint_quality") or {})
                       .get("measurement_coverage") or {})

        def _row(r):
            samples = r.get("evidence_samples") or []
            latest = r.get("last_evidence_date") or "—"
            sample = samples[0] if samples else {}
            excerpt = escape((sample.get("excerpt") or "")[:430])
            title = escape((sample.get("title") or "Company filing")[:110])
            url = sample.get("url")
            source = f'<a href="{escape(url, quote=True)}">{title}</a>' if url else title
            ev = (f'{r.get("direct_evidence_count", r.get("strict_evidence_count", 0))} dated direct-product event(s); '
                  f'latest {fmt_date(latest)} — {source}'
                  + (f'<br><span style="font-size:8.5px;color:#555">“{excerpt}”</span>' if excerpt else ""))
            return (f'<tr><td><strong>{escape(r.get("ticker") or "—")}</strong><br>'
                    f'<span style="font-size:8.5px;color:#666">{escape(r.get("entity_scope") or "listed entity")}</span></td>'
                    f'<td><strong>{escape(r.get("adjudication_state") or "ROLE_EVIDENCE")}</strong><br>'
                    f'<span style="font-size:8.5px;color:#555">{escape(r.get("maker_status") or r.get("verification_status") or "—")}</span></td>'
                    f'<td>{ev}</td><td>{escape(r.get("research_route") or r.get("verification_status") or "—")}</td></tr>')

        def _lead_row(r):
            samples = r.get("evidence_samples") or []
            sample = samples[0] if samples else {}
            excerpt = escape((sample.get("excerpt") or "")[:360])
            basis = (r.get("adjudication_state") or r.get("mapping_status") or r.get("verification_status") or
                     r.get("maker_status") or "unverified company-product lead")
            reason = (r.get("adjudication_reason") or r.get("role_evidence_status") or
                      r.get("entity_scope") or "role unresolved")
            missing = "; ".join(r.get("missing_evidence") or [])
            next_step = (r.get("research_route") or
                         "Scheduled evidence refresh; no promotion until the machine gate clears")
            return (f'<tr><td><strong>{escape(r.get("ticker") or "—")}</strong><br>'
                    f'<span style="font-size:8.5px;color:#666">{escape(r.get("company") or "")}</span></td>'
                    f'<td>{escape(basis)}</td>'
                    f'<td>{escape(reason)}'
                    + (f'<br><span style="font-size:8.5px;color:#555">“{excerpt}”</span>' if excerpt else "")
                    + f'</td><td>{escape(missing or next_step)}</td></tr>')

        status = coverage.get("status") or "coverage not computed"
        audit_note = (f'{coverage.get("rejected_no_product_proof", 0)} lexical candidate(s) failed exact product proof and are retained only in the audit data.'
                      if coverage.get("rejected_no_product_proof") else
                      'No rejected lexical candidates are shown in the report.')
        operating_body = (f'<h4 style="margin:8px 0 4px;color:#2c5f2d">Direct operating producers</h4>'
                          f'<table class="jtable"><tr><th>Ticker / scope</th><th>Role and state</th>'
                          f'<th>Primary as-of evidence</th><th>Research route</th></tr>{"".join(_row(r) for r in operating)}</table>'
                          if operating else
                          '<p style="font-size:9.5px;color:#555;margin:6px 0"><strong>No independently corroborated operating producer recovered.</strong> This is a coverage gap, not proof that no producer exists.</p>')
        pipeline_body = (f'<h4 style="margin:8px 0 4px;color:#5c4a9e">Evidenced capacity pipeline / direct role</h4>'
                         f'<table class="jtable"><tr><th>Ticker / scope</th><th>Role and state</th>'
                         f'<th>Primary as-of evidence</th><th>Research route</th></tr>{"".join(_row(r) for r in pipeline)}</table>'
                         if pipeline else '')
        unverified_body = (f'<h4 style="margin:8px 0 4px;color:#8a5a16">Machine-quarantined exceptions (maximum 3)</h4>'
                           f'<table class="jtable"><tr><th>Ticker</th><th>Machine state</th>'
                           f'<th>Why promotion stopped</th><th>Missing proof</th></tr>{"".join(_lead_row(r) for r in unverified_leads)}</table>'
                           if unverified_leads else '')
        maker_blocks.append(
            f'<h3 style="margin:14px 0 6px;color:#2c5f2d">{product} '
            f'<span style="font-weight:400;font-size:12px;color:#666">'
            f'({escape(measurement.get("display_label") or status)}; '
            f'{coverage.get("candidate_count", 0)} candidate screen hits; '
            f'company universe {escape(populations.get("status") or "not classified")})</span></h3>'
            f'{operating_body}{pipeline_body}{unverified_body}'
            f'<p style="font-size:8.8px;color:#666;margin:4px 0">Audit: {audit_note} '
            f'Automatic adjudication promoted {adjudication.get("promoted_operating_count", len(operating))} operating and '
            f'{adjudication.get("promoted_pipeline_count", len(pipeline))} pipeline name(s), quarantined '
            f'{adjudication.get("quarantined_count", len(unverified_leads))}, and rejected '
            f'{adjudication.get("auto_rejected_count", 0)}. Routine promotion does not require manual review.</p>')

    if maker_blocks:
        parts.append(f"""
<div class="judgment-box" style="border-color:#2c5f2d;border-left-color:#2c5f2d;background:#f5faf5">
  <h2 style="color:#2c5f2d">Who actually makes this? — constraint-scoped producer universe</h2>
  <div class="jnote">Every detected constraint is shown, even when it ranked outside the compact headline shortlist or coverage is incomplete. A direct operating producer
  needs two non-duplicative dated filings that put the exact product beside manufacturing action and a physical asset.
  A company with a repeated exact capacity plan or direct product role is shown separately as a pipeline lead—not silently
  dropped and never treated as an operating maker. The machine automatically promotes strong exact roles, quarantines
  ambiguous evidence, and rejects buyers, EPC firms, lexical collisions, and unsupported mapper associations. Only the
  three highest-priority unresolved exceptions are displayed; routine promotion does not require manual research.
  <br><br>
  <strong>Still not a Buy list.</strong> Producer proof establishes role; the final decision separately requires a
  constraint, earnings capture, order conversion, risk, and entry evidence.</div>
  {''.join(maker_blocks)}
</div>
""")

    # ── COMPANY-ORIGINATED PRODUCT DISCOVERIES ───────────────────────────────
    # This is intentionally outside the constraint-scoped producer table.  It
    # makes new issuer product evidence visible even before independent
    # import/capacity/demand work has established whether the product is a
    # binding physical constraint.  That prevents a missing crosswalk from
    # hiding a company, without pretending an adjacent chain is the same one.
    discoveries = doc.get("company_product_discoveries") or []
    if discoveries:
        rows = []
        for row in discoveries:
            samples = row.get("evidence") or []
            sample = samples[0] if samples else {}
            title = escape((sample.get("title") or "Company filing")[:110])
            url = sample.get("url")
            source = (f'<a href="{escape(url, quote=True)}">{title}</a>' if url else title)
            excerpt = escape((sample.get("excerpt") or "")[:360])
            proof = (f'{row.get("independent_document_count", 0)} independent dated filing(s); '
                     f'latest {fmt_date(row.get("last_evidence_date"))} — {source}'
                     + (f'<br><span style="font-size:8.5px;color:#555">“{excerpt}”</span>'
                        if excerpt else ""))
            role = (row.get("role_type") or "DIRECT_ROLE_UNCLASSIFIED").replace("_", " ").title()
            rows.append(
                f'<tr><td><strong>{escape(row.get("ticker") or "—")}</strong><br>'
                f'<span style="font-size:8.5px;color:#666">{escape(row.get("company") or "")}</span></td>'
                f'<td><strong>{escape(row.get("product_phrase") or "—")}</strong><br>'
                f'<span style="font-size:8.5px;color:#666">{escape(role)} · '
                f'{escape(row.get("role_state") or "DISCOVERY")}</span></td>'
                f'<td>{proof}</td><td>{escape(row.get("research_route") or "")}</td></tr>'
            )
        parts.append(f"""
<div class="judgment-box" style="border-color:#8a5a16;border-left-color:#8a5a16;background:#fffaf0">
  <h2 style="color:#8a5a16">Company-originated product discoveries — unlinked research queue</h2>
  <div class="jnote">These are products recovered from the issuer's own dated filing, not from theme co-occurrence.
  They are shown precisely because an emerging product chain may be absent from the current constraint crosswalk.
  <strong>They are not constraints and not Buy candidates.</strong> A product enters the constraint queue only after
  separate dated import/capacity, demand, and resupply-barrier evidence is captured; an analyst must then review any
  product-to-chain link. This separation prevents an adjacent product from being silently labelled as a maker of a
  different bottleneck.</div>
  <table class="jtable"><tr><th>Company</th><th>Own-filing product / role</th><th>Primary as-of evidence</th><th>Required next work</th></tr>
  {''.join(rows)}</table>
</div>
""")

    # NOTE (Jul-2026): novel-vocabulary radar, reference-data/no-hardcode audit,
    # and the governance/mapping-artifact review queue are INTERNAL judgment-
    # layer tooling — consulted directly from the data JSON (novel_policy_
    # vocabulary, reference_data, scheme_graduation_candidates, exclusion_
    # proposals, mapping_artifacts fields) as part of the monthly SQL review,
    # never rendered into the client-facing report. The user does not need,
    # and should never need, to read machine review-queue noise to act on a
    # report (PMS zero-judgment rule). Do not re-add rendering for these.

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

    # ── EXPLOSIVENESS STUDY (durability test, cohort de-dup, narrow+fresh candidates) ──
    dupes = doc.get("chain_cohort_duplicates") or []
    explo = doc.get("chain_cohort_explosiveness") or {}
    narrow = doc.get("narrow_fresh_candidates") or []
    if dupes or explo or narrow:
        parts.append("""
<div class="judgment-box">
  <h2>Constraint Explosiveness Study — "did this chain actually go crazy, broadly?"</h2>
  <div class="jnote">Cohort-wide test (not single-stock cherry-picking): does the WHOLE beneficiary list of a
  chain win at MULTIPLE separate entry vintages, or is one lottery winner masking a dead cohort? Evidence
  only — never a score input.</div>
""")
        if explo:
            durable = {k: v for k, v in explo.items() if v.get("durable")}
            parts.append("""
  <h3>Multi-vintage durability test</h3>
  <table class="jtable">
    <tr><th>Chain</th><th>Durable?</th><th>Vintages tested</th><th>Broad cohort wins</th><th>Read</th></tr>
""")
            for chain, v in sorted(explo.items(), key=lambda x: (not x[1].get("durable"), x[0])):
                status = v.get("durability_status")
                mark = ('<span class="pill-g">PASSED</span>' if v.get("durable") else
                        '<span class="grade-X">INSUFFICIENT HISTORY</span>' if status == "insufficient_history" else
                        '<span class="pill-r">not repeated</span>')
                parts.append(f'<tr><td><strong>{chain}</strong></td><td>{mark}</td>'
                             f'<td>{v.get("n_vintages_tested","—")}</td><td>{v.get("n_broad_cohort_wins","—")}</td>'
                             f'<td>{v.get("note","")}</td></tr>')
            parts.append("</table>")
            if durable:
                parts.append(f'<p><strong>Only {", ".join(durable.keys())} passed</strong> — every other tracked '
                             f'chain either decayed vintage-over-vintage or never repeated a broad win.</p>')
        if dupes:
            parts.append("""
  <h3>Cohort de-dup alert — same companies, different labels</h3>
  <table class="jtable">
    <tr><th>Chain A</th><th>Chain B</th><th>Overlap</th><th>Note</th></tr>
""")
            for d in dupes:
                parts.append(f'<tr><td>{d["chain_a"]}</td><td>{d["chain_b"]}</td>'
                             f'<td>{d["overlap_pct"]}% ({d["n_shared"]}/{min(d["n_a"],d["n_b"])})</td>'
                             f'<td>{d["note"]}</td></tr>')
            parts.append("</table>")
        if narrow:
            parts.append("""
  <h3>Narrow + fresh forward-discovery candidates (manual-mapping priority)</h3>
  <table class="jtable">
    <tr><th>Theme</th><th>Companies</th><th>Stage</th><th>Confirmed qtrs</th></tr>
""")
            for n in narrow[:6]:
                parts.append(f'<tr><td><strong>{n["theme_name"]}</strong></td><td>{n["company_count"]}</td>'
                             f'<td>{n.get("stage_label","")}</td><td>{n.get("confirmed_quarters","—")}</td></tr>')
            parts.append("</table>")
        parts.append("</div>")

    # ── EXCLUSION BANNERS ───────────────────────────────────────────────────────
    for ticker, reason in manual_exclude.items():
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
  <h2>Raw-screen diagnostics — {as_of_label}</h2>
  <div class="picks-row">{picks_rows}</div>
  <p><strong>Highest raw-screen names:</strong> {t1_tickers or '—'}.
  &nbsp;|&nbsp; <strong>Fresh-theme names in raw T1:</strong> {fresh_count}/5.
  &nbsp;|&nbsp; <strong>Scoring:</strong> composite = fundamental × freshness_multiplier; 200DMA → position_size_guidance only (not the score).
  &nbsp;|&nbsp; <strong>Not recommendations:</strong> a name must clear the verified decision gates above before it can be researched further.</p>
</div>
""")

    # ── JUDGMENT LAYER (skill Step 3) ───────────────────────────────────────────
    judgment = None if final_decision else doc.get("_judgment")
    if judgment:
        parts.append(render_judgment(judgment))

    # ── TIER LEGEND ─────────────────────────────────────────────────────────────
    parts.append("""
<div class="tier-legend">
  <div><span class="badge-t1">T1 — RAW SCREEN</span> &nbsp;Top raw composite names; never an action recommendation</div>
  <div><span class="badge-t3w">T3 — RAW WATCH</span> &nbsp;Raw rank 6-8 + fresh/new_theme + NORMAL risk; still requires evidence gates</div>
  <div><span class="badge-t3i">T3 — RAW IGNORE</span> &nbsp;Lower raw-screen priority; not a verdict on the company</div>
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
    parts.append('<div class="section"><h2>1. Major Themes — strongest signals NOW (not the newest)</h2>')
    parts.append("""<p style="font-size:9.5px;color:#666;margin:2px 0 6px">
This table ranks by <strong>current signal strength</strong> — an old theme that is still binding belongs here
(CRGO was detected in 2020 and is still Grade A). For NEW themes see section 2 (Emerging in window).
"Started" = first appearance in the document corpus; the corpus begins 01-Jan-2020, so
<strong>"pre-2020 (data start)" means the theme was already active when data collection began</strong> — its true
start is unknown and earlier.</p>""")
    parts.append("""<table>
<tr><th>Theme</th><th>Plain-English Meaning</th><th>Started</th><th>Phase</th>
<th class="num">Strength</th><th class="num">Qtrs</th><th class="num">Cos</th></tr>""")

    for t in major_themes[:10]:
        name  = t.get("theme_name") or t.get("name", "")
        desc  = t.get("description", "")
        stage = t.get("stage_label", "")
        qtrs  = t.get("confirmed_quarters", 0)
        start = fmt_theme_start(t.get("first_detected"))
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
            start = fmt_theme_start(t.get("first_detected"))
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
    parts.append('<div class="section"><h2>3. Raw candidate diagnostics — not recommendations</h2>')
    parts.append("""<p style="font-size:10px;color:#555;margin-bottom:8px;">
<strong>Scoring:</strong> fundamental = 0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub;
composite = fundamental × freshness_multiplier (new_theme ×1.10 | fresh ×1.12 | developing ×1.05 | established ×1.00 | consensus ×0.93).
200DMA → position_size_guidance only, NOT a score input.
</p>""")

    parts.append("""<table>
<tr>
  <th>#</th><th>Ticker</th><th>Raw tier</th><th>Evidence-gate status</th>
  <th class="num">Composite</th><th>Freshness</th><th>Risk</th>
  <th>Mgmt growth guidance</th>
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
        gate_status = c.get("final_selection_status") or "not evaluated"
        # Management growth-ambition + concall tone (annotation only — not scored)
        ga = c.get("growth_ambition") or {}
        tone = c.get("management_concall_tone")
        if ga.get("multiple"):
            mult = ga["multiple"]
            persp = "own" if ga.get("perspective") == "company" else "mkt"
            growth_str = f"<strong>{mult:g}×</strong> {escape(str(ga.get('basis') or ''))} <span style='font-size:8px;color:#888'>({persp})</span>"
        else:
            growth_str = "—"
        if tone is not None:
            tcol = "#1a7a3a" if tone > 0.2 else "#b3541e" if tone < -0.2 else "#888"
            growth_str += f"<br><span style='font-size:8px;color:{tcol}'>call tone {tone:+.2f}</span>"

        parts.append(f"""<tr class="{row_cls}">
  <td class="num">{i+1}</td>
  <td class="ticker">{tick}</td>
  <td>{tier_badge_html}</td>
  <td style="font-size:9px">{gate_status}</td>
  <td class="num"><strong>{comp:.3f}</strong></td>
  <td>{freshness_badge(fl)}</td>
  <td>{risk_badge(rt)}</td>
  <td style="font-size:9px">{growth_str}</td>
  <td style="font-size:9px">{above_str}</td>
  <td class="num">{pct52_str}</td>
  <td>{pos_badge(pos) if not excl else '—'}</td>
  <td>{prod_str}</td>
</tr>""")

    parts.append("</table>")

    # Scoring breakdown — top raw-screen names, deliberately not action labels.
    top3 = t1_effective[:3]
    if top3:
        parts.append('<h3>Scoring breakdown — top raw-screen names</h3>')
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
        parts.append('<h3>Tier3 raw-watch diagnostics</h3>')
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
<td style="font-size:9.5px">Raw-screen watchlist — evidence gates still apply</td>
</tr>""")
        parts.append("</table>")
    else:
        parts.append('<div class="note-box">No Tier3 raw-watch names at this date.</div>')

    parts.append("</div>")

    # ── SECTION 4: EVIDENCE DASHBOARD ───────────────────────────────────────────
    parts.append('<div class="section"><h2>4. Theme-signal evidence dashboard</h2>')
    parts.append('<p style="font-size:9.5px;color:#666;margin:2px 0 6px">Confidence measures source diversity and theme persistence, not proof of a quantified physical constraint. The decision table above is the constraint-quality authority.</p>')
    parts.append("""<table>
<tr><th>Theme</th><th class="num">Cos</th><th class="num">Filings</th>
<th class="num">Bottleneck Signals</th><th class="num">Qtrs</th>
<th class="num">Policy Events</th><th class="num">Theme-signal confidence</th><th>Momentum</th></tr>""")

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
    parts.append('<div class="section"><h2>5. Raw mapping overlap — not independent constraint proof</h2>')
    parts.append('<p style="font-size:9.5px;color:#666;margin:2px 0 6px">Overlap is shown only as a data-quality diagnostic. Product-stage duplicates and unverified roles are not treated as corroboration or a structural-chokepoint signal.</p>')
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
        note = "EXCLUDED — data pipeline flag" if excl else "mapping overlap — verify product roles before use"
        parts.append(f"""<tr class="{row_cls}">
  <td class="ticker">{"<s>" if excl else ""}{tick}{"</s>" if excl else ""}</td>
  <td class="num">{n_ch}</td>
  <td class="num">{score:.2f}</td>
  <td>{chain_pills}</td>
  <td style="font-size:9.5px;color:#555">{note}</td>
</tr>""")

    parts.append("</table></div>")

    # ── SECTION 6: NEXT RESEARCH STEP ──────────────────────────────────────────
    parts.append('<div class="section"><h2>6. Next research step</h2>')
    gated_priorities = final_decision.get("priorities") or []
    if gated_priorities:
        first = gated_priorities[0]["ticker"]
        parts.append(f'<div class="note-box">A name clears the strict evidence hand-off. Suggested next step: <strong>generate report for {first} as of {as_of_str}</strong> and complete investment-committee underwriting.</div>')
    elif final_decision.get("early_timing_candidates"):
        first = final_decision["early_timing_candidates"][0]["ticker"]
        parts.append(f'<div class="note-box">No company clears the strict evidence hand-off. The highest-ranked Early/Timing candidate is <strong>{first}</strong>: generate its full report as of {as_of_str} and validate the printed confirmation before any position is scaled.</div>')
    elif final_decision.get("discovery_starter_candidates"):
        first = final_decision["discovery_starter_candidates"][0]["ticker"]
        parts.append(f'<div class="note-box">The highest-authority result is a Discovery Starter: <strong>{first}</strong>. Generate its full report as of {as_of_str}; check valuation, liquidity and entry; then obey the printed per-name and 5% aggregate sleeve caps.</div>')
    elif final_decision.get("underwriting_candidates"):
        first = final_decision["underwriting_candidates"][0]["ticker"]
        parts.append(f'<div class="note-box">No stock has position authority. The highest-ranked underwriting research candidate is <strong>{first}</strong>: generate its full report as of {as_of_str}, refresh the exact product-maker evidence, and validate earnings capture before an investment decision.</div>')
    else:
        parts.append('<div class="note-box">No Core, Early/Timing, or Discovery Starter clears the evidence gates. Allocate nothing from this strategy; use the proof-led research queue to add dated company-product, demand, and risk evidence before re-running the selector.</div>')
    parts.append('</div>')

    # ── OPTIONAL HINDSIGHT APPENDIX ───────────────────────────────────────────
    # This field is injected only by the explicit --forward-results report
    # invocation. It never enters the selector JSON and cannot change any
    # point-in-time constraint, ticker cohort, evidence gate, or decision.
    if doc.get("_forward_validation"):
        parts.append(render_forward_validation(doc["_forward_validation"]))

    # ── FOOTER ──────────────────────────────────────────────────────────────────
    parts.append(f"""
<div class="footer">
  Selection and decision pages are point-in-time and contain no selection evidence dated after <strong>{as_of_label}</strong>.
  Any explicitly labelled forward-return appendix is a separate hindsight outcome audit.
  Produced by MakroGraph Intelligence. &nbsp;|&nbsp;
  All data sourced from makrograph DB (localhost). &nbsp;|&nbsp;
  Scoring v3: composite = fundamental × freshness_multiplier; 200DMA = position guidance only. &nbsp;|&nbsp;
  Not investment advice — data-driven view only.
</div>
</div><!-- /page -->
</body>
</html>""")

    return "\n".join(parts)


def main():
    args = sys.argv[1:]
    forward_path = None
    output_dir_override = None
    if "--forward-results" in args:
        index = args.index("--forward-results")
        if index + 1 >= len(args):
            print("--forward-results requires a JSON path", file=sys.stderr)
            sys.exit(1)
        forward_path = args[index + 1]
        del args[index:index + 2]
    if "--output-dir" in args:
        index = args.index("--output-dir")
        if index + 1 >= len(args):
            print("--output-dir requires a directory", file=sys.stderr)
            sys.exit(1)
        output_dir_override = args[index + 1]
        del args[index:index + 2]
    paths = args
    if not paths:
        print("Usage: python render_selector_html.py <json_path> [<json_path2> ...] "
              "[--forward-results <forward_json>] [--output-dir <directory>]")
        sys.exit(1)

    forward_doc = None
    if forward_path:
        with open(forward_path) as ff:
            forward_doc = json.load(ff)

    for path in paths:
        with open(path) as f:
            doc = json.load(f)

        as_of = doc.get("as_of_date", "")
        country = doc.get("country", "IN")

        if forward_doc:
            matching = next(
                (item for item in (forward_doc.get("anchors") or [])
                 if item.get("as_of_date") == as_of),
                None,
            )
            if not matching:
                print(f"No forward-test anchor matching {as_of} in {forward_path}", file=sys.stderr)
                sys.exit(1)
            synced_anchor = dict(matching)
            synced_sections = dict(matching.get("section_tickers") or {})
            # The standalone replay and the report use the same raw cohort but
            # may sort ties differently. Preserve the exact displayed order.
            synced_sections["raw_screen"] = [
                row.get("ticker") for row in (doc.get("ranked_candidates") or [])[:8]
                if row.get("ticker")
            ]
            synced_anchor["section_tickers"] = synced_sections
            doc["_forward_validation"] = {
                "anchor": synced_anchor,
                "latest_price_date": forward_doc.get("latest_price_date"),
                "method": forward_doc.get("method") or {},
                "section_definitions": forward_doc.get("section_definitions") or {},
                "actionable_coverage_alarm": forward_doc.get("actionable_coverage_alarm") or {},
            }

        # Optional judgment sidecar (skill Step 3 output, written by the analyst layer)
        jpath = os.path.join(os.path.dirname(path),
                             f"stock_selector_{country}_{as_of}_judgment.json")
        if os.path.exists(jpath):
            with open(jpath) as jf:
                doc["_judgment"] = json.load(jf)

        html = render(doc, as_of)
        # Keep printable punctuation within the PDF skill's ASCII-hyphen rule.
        html = html.replace("\u2011", "-").replace("\u2012", "-").replace("\u2013", "-").replace("\u2014", "-")

        out_dir = output_dir_override or os.path.dirname(path)
        os.makedirs(out_dir, exist_ok=True)
        base = f"stock_selector_{country}_{as_of}_v3_report.html"
        out_path = os.path.join(out_dir, base)
        with open(out_path, "w") as f:
            f.write(html)
        print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
