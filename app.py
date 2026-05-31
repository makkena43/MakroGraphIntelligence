"""MakroGraph Intelligence — Streamlit Dashboard.

5-tab UI with sidebar country selector:
  Sidebar  — 🌍 Country (USA / India), quick navigation, live stats
  Tab 1    — 🚀 Pipeline Runner
  Tab 2    — 📞 Concall & Filings Analysis
  Tab 3    — 🗺️  Themes & Companies
  Tab 4    — 🌐  Macro & Policy
  Tab 5    — 🏢  Company Explorer
"""

import copy
import io
import logging
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st
import yaml

# ── project root on path ────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

# ── page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MakroGraph Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Suppress Neo4j notification spam (UNRECOGNIZED Cypher warnings from older Neo4j versions)
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

# ── Hot-reload guard ─────────────────────────────────────────────────────────
# Streamlit caches imported modules across reruns.  Explicitly reload the
# core pipeline modules on every app boot so code changes take effect without
# restarting the Streamlit server — just close + reopen the browser tab.
import importlib as _importlib
for _mod_name in [
    "makrograph.ontology.causal_mapper",
    "makrograph.pipeline.intelligence_pipeline",
    "makrograph.themes.theme_ranker",
    "makrograph.themes.theme_detector",
    "makrograph.themes.beneficiary_mapper",
    "makrograph.themes.theme_canonicalizer",
]:
    try:
        import sys as _sys
        if _mod_name in _sys.modules:
            _importlib.reload(_sys.modules[_mod_name])
    except Exception:
        pass

# ── load config ──────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def load_config():
    import os, json
    with open(ROOT / "config" / "settings.yaml") as f:
        cfg = yaml.safe_load(f)
    # Merge secrets.json (gitignored) — deep merge into cfg
    secrets_path = ROOT / "config" / "secrets.json"
    if secrets_path.exists():
        with open(secrets_path) as f:
            secrets = json.load(f)
        for section, values in secrets.items():
            if section.startswith("_"):
                continue
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(
                    {k: v for k, v in values.items() if v}
                )
    # Environment variables override secrets.json (highest priority)
    _env_overrides = {
        ("neo4j",       "password"):  "MAKROGRAPH_NEO4J_PASSWORD",
        ("postgresql",  "password"):  "MAKROGRAPH_PG_PASSWORD",
        ("gemini",      "api_key"):   "GEMINI_API_KEY",
        ("fred",        "api_key"):   "FRED_API_KEY",
        ("eia",         "api_key"):   "EIA_API_KEY",
        ("congress",    "api_key"):   "CONGRESS_API_KEY",
    }
    for (section, key), env_var in _env_overrides.items():
        val = os.environ.get(env_var)
        if val:
            cfg.setdefault(section, {})[key] = val
    return cfg

cfg = load_config()

# ── DB connection ────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def get_pg():
    from makrograph.storage.pg_store import PGStore
    try:
        return PGStore(cfg.get("postgresql", {}))
    except Exception as e:
        return None

pg = get_pg()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

CONVICTION_COLOR = {
    "confirmed": "#22c55e",
    "developing": "#f59e0b",
    "emerging": "#6366f1",
}
CONVICTION_ICON = {"confirmed": "✅", "developing": "🔶", "emerging": "🔮"}
BEN_TYPE_ICON = {"direct": "🟢", "indirect": "🟡", "disruptee": "🔴"}


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:#fff;padding:2px 9px;'
        f'border-radius:12px;font-size:0.75rem;font-weight:700">{text}</span>'
    )


def _pct_bar(value: float, max_val: float = 100) -> str:
    pct = min(100, value / max(max_val, 1) * 100)
    return (
        f'<div style="background:#1e293b;border-radius:4px;height:8px;width:100%">'
        f'<div style="background:#818cf8;width:{pct:.0f}%;height:8px;border-radius:4px"></div></div>'
    )


class _StreamlitLogHandler(logging.Handler):
    """Routes log records to a list that Streamlit can consume."""
    def __init__(self):
        super().__init__()
        self.records: list[str] = []
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s — %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.records.append(self.format(record))


@contextmanager
def _capture_logs():
    handler = _StreamlitLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)


def _call_gemini_api(prompt: str, api_key: str, model: str = "gemini-flash-latest") -> str:
    """POST a prompt to the Google Gemini REST API and return the response text."""
    import requests as _req
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1200},
    }
    resp = _req.post(
        url,
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADERS  (cached per query so the Themes tab is fast)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30, show_spinner=False)
def load_themes(min_strength: float = 0.0, country: str = "US"):
    if not pg:
        return []
    return pg.get_active_themes(min_strength=min_strength, country=country)


@st.cache_data(ttl=60, show_spinner=False)
def load_ranking_table(country: str = "US") -> list[dict]:
    """Build ranking table rows from mg_themes + stored metadata."""
    import json as _json
    if not pg:
        return []
    rows = pg.get_active_themes(min_strength=0.0, country=country)
    rows = sorted(rows, key=lambda r: float(r.get("strength_score") or 0), reverse=True)
    table = []
    for rank, r in enumerate(rows, 1):
        try:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            d   = meta.get("demand_count", 0) or 0
            s   = meta.get("supply_constraint_count", 0) or 0
            q   = int(meta.get("quarter_count") or meta.get("confirmed_quarters") or 0)
            pers = float(meta.get("persistence_multiplier") or 1.0)
            theme_type = meta.get("theme_type", "") or "auto"
            _elig = meta.get("eligibility_score")
            if _elig is not None:
                elig = round(float(_elig), 2)
            else:
                _dem = float(meta.get("demand_count", 0) or 0)
                _sup = float(meta.get("supply_constraint_count", 0) or 0)
                _cap = float(meta.get("capex_count", 0) or 0)
                _cos = float(r.get("company_count") or 0)
                _ckw = float(meta.get("constraint_kw_count", 0) or 0)
                _wt  = float(meta.get("weighted_constraint_score", 0) or 0)
                _bn  = bool(meta.get("is_bottleneck") or theme_type == "bottleneck" or _ckw >= 3)
                _p   = 1.0 if q >= 3 else (0.7 if q == 2 else 0.3)
                elig = round(
                    min(_dem / 50.0, 1.0) * 0.20
                    + min((_sup + _ckw * 1.5 + _wt * 0.5) / 40.0, 1.0) * 0.25
                    + min(_cap / 15.0, 1.0) * 0.20
                    + min(_cos / 10.0, 1.0) * 0.15
                    + _p * 0.10 + float(_bn) * 0.10, 2)
            # Freshness: how long ago was this theme first detected?
            _first_det = r.get("first_detected")
            if _first_det:
                _fd_date = _first_det if isinstance(_first_det, date) else date.fromisoformat(str(_first_det)[:10])
                _age_days = (date.today() - _fd_date).days
                if _age_days <= 90:
                    _fresh = "🟢 Fresh"
                elif _age_days <= 365:
                    _fresh = "🟡 Active"
                else:
                    _fresh = "🔴 Mature"
                _first_det_str = str(_fd_date)
            else:
                _age_days = 9999
                _fresh = "❓ Unknown"
                _first_det_str = "—"

            table.append({
                "#":            rank,
                "Theme":        r.get("theme_name", ""),
                "Score":        round(float(r.get("strength_score") or 0), 1),
                "D/S":          f'D{int(d)}/S{int(s)}',
                "Conv":         (r.get("conviction") or "emerging").title(),
                "Cos":          int(r.get("company_count") or 0),
                "Q":            q,
                "Pers":         round(pers, 2),
                "Elig":         elig,
                "Type":         theme_type,
                "First Seen":   _first_det_str,
                "Freshness":    _fresh,
                "_slug":        r.get("theme_slug", ""),
            })
        except Exception:
            pass
    return table


@st.cache_data(ttl=30, show_spinner=False)
def load_beneficiaries(theme_id: int):
    if not pg:
        return []
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT b.ticker, b.company_name, b.beneficiary_type, b.company_role,
                              b.relevance_score, b.signal_count,
                              COALESCE(b.capex_signals, 0) AS capex_signals,
                              b.rank_in_theme, b.reasoning, b.first_seen_at, b.last_seen_at
                       FROM mg_theme_beneficiaries b
                       WHERE b.theme_id = %s
                       ORDER BY b.rank_in_theme NULLS LAST, b.relevance_score DESC""",
                    (theme_id,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


@st.cache_data(ttl=30, show_spinner=False)
def load_snapshots(theme_id: int):
    if not pg:
        return []
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT snapshot_date, strength_score, momentum_score, doc_count
                       FROM mg_theme_snapshots
                       WHERE theme_id = %s
                       ORDER BY snapshot_date""",
                    (theme_id,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


@st.cache_data(ttl=30, show_spinner=False)
def load_causal_chains(country: str = "US"):
    if not pg:
        return []
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT chain_name, depth, terminal_effect,
                              activation_score, last_scored_at, first_detected
                       FROM mg_causal_chains
                       WHERE is_active = TRUE AND country = %s
                       ORDER BY activation_score DESC""",
                    (country,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


@st.cache_data(ttl=30, show_spinner=False)
def load_pipeline_kpis(country: str = "US"):
    if not pg:
        return {}
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        (SELECT COUNT(*)
                            FROM mg_documents
                            WHERE country = %s)                                                          AS total_docs,
                        (SELECT COUNT(DISTINCT de.entity_id)
                            FROM mg_document_entities de
                            JOIN mg_documents d ON d.id = de.document_id
                            WHERE d.country = %s)                                                        AS total_entities,
                        (SELECT COUNT(*)
                            FROM mg_signals
                            WHERE country = %s)                                                          AS total_signals,
                        (SELECT COUNT(*)
                            FROM mg_themes
                            WHERE is_active = TRUE AND country = %s)                                     AS active_themes,
                        (SELECT COUNT(*)
                            FROM mg_events
                            WHERE country = %s)                                                          AS total_events,
                        (SELECT COUNT(*)
                            FROM mg_causal_chains
                            WHERE is_active = TRUE AND country = %s)                                     AS active_chains,
                        (SELECT COUNT(*)
                            FROM mg_replay_runs)                                                         AS replay_runs
                """, (country, country, country, country, country, country))
                row = cur.fetchone()
                return dict(row) if row else {}
    except Exception:
        return {}


@st.cache_data(ttl=30, show_spinner=False)
def load_replay_history():
    if not pg:
        return []
    try:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT replay_batch, replay_date, docs_ingested, docs_nlp,
                              themes_detected, themes_snapped, causal_score,
                              duration_sec, status
                       FROM mg_replay_runs
                       ORDER BY replay_date DESC LIMIT 60"""
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL CSS
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
/* ── Slate dark theme base (not pure black) ─────────────────── */
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
.main, body {
    background: #0f172a !important;
    color: #e2e8f0 !important;
}
[data-testid="stHeader"] {
    background: #0f172a !important;
    border-bottom: 1px solid #1e293b;
}

/* All text elements */
p, span, div, label, li, h1, h2, h3, h4, h5, h6,
.stMarkdown, .stMarkdown p, .stMarkdown span,
[data-testid="stMarkdownContainer"],
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] span,
[data-testid="stMarkdownContainer"] div {
    color: #e2e8f0 !important;
}

/* Widget labels & controls */
.stRadio label, .stCheckbox label,
.stSlider label, .stDateInput label,
.stMultiSelect label, .stSelectbox label,
.stTextInput label, .stTextArea label,
[data-baseweb="tab"] {
    color: #e2e8f0 !important;
}

/* Tab bar */
[data-testid="stTab"] { color: #e2e8f0 !important; font-size: 1rem; }

/* Buttons */
.stButton > button {
    background: #1e293b !important;
    color: #e2e8f0 !important;
    border: 1px solid #334155 !important;
}
.stButton > button:hover {
    background: #818cf8 !important;
    border-color: #818cf8 !important;
    color: #ffffff !important;
}

/* Alert / info boxes */
[data-testid="stAlert"],
[data-testid="stAlert"] p,
[data-testid="stAlert"] span { color: #e2e8f0 !important; }

/* Widget input backgrounds */
[data-baseweb="input"], [data-baseweb="select"],
[data-baseweb="textarea"],
[data-baseweb="base-input"] {
    background: #1e293b !important;
    color: #e2e8f0 !important;
    border-color: #334155 !important;
}

/* Multiselect / select dropdown */
[data-baseweb="popover"],
[data-baseweb="menu"] {
    background: #1e293b !important;
    color: #e2e8f0 !important;
}
[data-baseweb="option"] { color: #e2e8f0 !important; background: #1e293b !important; }
[data-baseweb="option"]:hover { background: #334155 !important; }

/* Expander */
[data-testid="stExpander"],
[data-testid="stExpander"] summary,
[data-testid="stExpander"] p {
    background: #1e293b !important;
    color: #e2e8f0 !important;
    border-color: #334155 !important;
}

/* Dataframe — let the Streamlit theme (config.toml) drive cell colours;
   only force the outer wrapper border so it blends with the page. */
[data-testid="stDataFrame"] {
    border: 1px solid #334155;
    border-radius: 8px;
    overflow: hidden;
}
/* Dataframe toolbar icons */
[data-testid="stDataFrameResizable"] button { color: #94a3b8 !important; }

/* Metric widget */
[data-testid="stMetric"] label,
[data-testid="stMetricLabel"],
[data-testid="stMetricValue"] { color: #e2e8f0 !important; }

/* Progress bar track */
.stProgress > div { background: #1e293b !important; }

.block-container { padding-top:1.5rem; padding-bottom:2rem; }

/* Theme card */
.theme-card {
    background:#1e293b; border:1px solid #334155; border-radius:12px;
    padding:1.2rem 1.4rem; margin-bottom:1rem;
    box-shadow:0 1px 4px rgba(0,0,0,0.3);
}
.theme-card:hover { border-color:#818cf8; box-shadow:0 2px 8px rgba(129,140,248,0.2); }
.theme-name { font-size:1.1rem; font-weight:700; color:#f1f5f9; margin-bottom:4px; }
.theme-slug { font-size:0.75rem; color:#94a3b8; margin-bottom:8px; }
.metric-row { display:flex; gap:1.5rem; flex-wrap:wrap; margin:10px 0; }
.metric-item { display:flex; flex-direction:column; }
.metric-label { font-size:0.68rem; color:#94a3b8; text-transform:uppercase; letter-spacing:.05em; }
.metric-value { font-size:1.1rem; font-weight:700; color:#f1f5f9; }

/* KPI cards */
.kpi-card {
    background:#1e293b; border:1px solid #334155; border-radius:10px;
    padding:1rem 1.2rem; text-align:center;
    box-shadow:0 1px 3px rgba(0,0,0,0.25);
}
.kpi-num  { font-size:2rem; font-weight:800; color:#818cf8; line-height:1; }
.kpi-label{ font-size:0.8rem; color:#94a3b8; margin-top:4px; }

/* Log area */
.log-box {
    background:#0f172a; border:1px solid #1e293b; border-radius:8px;
    padding:0.8rem 1rem; font-family:monospace; font-size:0.78rem;
    color:#7dd3fc; max-height:400px; overflow-y:auto;
}

/* Theme list card (clickable) */
.theme-list-card {
    background:#1e293b; border:1.5px solid #334155; border-radius:10px;
    padding:10px 14px; margin-bottom:4px; cursor:pointer;
    transition: border-color 0.15s, box-shadow 0.15s;
}
.theme-list-card:hover { border-color:#818cf8; }
.theme-list-card.selected {
    border-color:#818cf8; background:#1e3a5f;
    box-shadow:0 0 0 2px rgba(129,140,248,0.2);
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# COUNTRY CODE — derived early from session_state so it is available for the
# header KPI cards (which render before the sidebar block executes).
# The sidebar radio widget (key="sidebar_country") updates session_state on
# every interaction, so the next rerun picks up the new value here.
# ─────────────────────────────────────────────────────────────────────────────
_early_cc     = st.session_state.get("sidebar_country", "🇺🇸  USA")
COUNTRY_CODE  = "US" if "USA" in _early_cc else "IN"
COUNTRY_LABEL = "USA 🇺🇸" if COUNTRY_CODE == "US" else "India 🇮🇳"
COUNTRY_FLAG  = "🇺🇸" if COUNTRY_CODE == "US" else "🇮🇳"

# ─────────────────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────────────────

st.markdown(
    "<h1 style='color:#f1f5f9;font-size:1.8rem;margin-bottom:0'>📊 MakroGraph Intelligence</h1>"
    "<p style='color:#94a3b8;font-size:0.9rem;margin-top:4px'>Event-Centric Macro Research Platform</p>",
    unsafe_allow_html=True,
)

kpis = load_pipeline_kpis(COUNTRY_CODE)
if kpis:
    cols = st.columns(7)
    labels = [
        ("Docs", "total_docs"),
        ("Entities", "total_entities"),
        ("Signals", "total_signals"),
        ("Themes", "active_themes"),
        ("Events", "total_events"),
        ("Causal Chains", "active_chains"),
        ("Replay Runs", "replay_runs"),
    ]
    for col, (label, key) in zip(cols, labels):
        with col:
            st.markdown(
                f'<div class="kpi-card">'
                f'<div class="kpi-num">{kpis.get(key, 0):,}</div>'
                f'<div class="kpi-label">{label}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR — Country selector + Navigation
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        "<h2 style='color:#818cf8;font-size:1.15rem;margin-bottom:0'>📊 MakroGraph</h2>"
        "<p style='color:#475569;font-size:0.75rem;margin-top:2px'>Intelligence Platform</p>",
        unsafe_allow_html=True,
    )
    st.markdown("---")

    # ── Country selector ──────────────────────────────────────────────────────
    st.markdown(
        "<div style='font-size:0.78rem;color:#94a3b8;font-weight:600;"
        "text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px'>"
        "🌍 Market / Country</div>",
        unsafe_allow_html=True,
    )
    # The radio widget stores its value in session_state["sidebar_country"].
    # COUNTRY_CODE / LABEL / FLAG are derived early (above) from that same key
    # so they are available before the sidebar block runs.
    st.radio(
        "Country",
        ["🇺🇸  USA", "🇮🇳  India"],
        index=0 if COUNTRY_CODE == "US" else 1,
        key="sidebar_country",
        label_visibility="collapsed",
        help="Sets the active market. India pipeline (NSE/BSE) is planned — USA (SEC/EDGAR) is live.",
    )

    if COUNTRY_CODE == "IN":
        st.success(
            "🇮🇳 **India pipeline active.**  "
            "NSE · BSE · Screener · PIB · Invest India · Commerce/DGFT — "
            "all sources configured and ready.",
            icon="✅",
        )

    st.markdown("---")

    # ── Quick navigation ──────────────────────────────────────────────────────
    st.markdown(
        "<div style='font-size:0.78rem;color:#94a3b8;font-weight:600;"
        "text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px'>"
        "📍 Navigation</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        """
<div style="font-size:0.82rem;line-height:2">
🚀 <b>Pipeline Runner</b> — run ingest + NLP + themes<br>
📞 <b>Concall Analysis</b> — browse filings by date<br>
🗺️ <b>Themes & Companies</b> — detected macro themes<br>
⭐ <b>Shortlisted Themes</b> — persisted multi-quarter themes<br>
🏆 <b>Stock Rankings</b> — thematic stock ranking<br>
🤖 <b>AI Analysis</b> — Gemini: themes + bottlenecks + stocks<br>
🌐 <b>Macro & Policy</b> — FRED, EIA, Congress data<br>
🏢 <b>Company Explorer</b> — per-ticker deep-dive
</div>""",
        unsafe_allow_html=True,
    )
    st.markdown("---")

    # ── Live DB stats ──────────────────────────────────────────────────────────
    _kpis = load_pipeline_kpis(COUNTRY_CODE)
    if _kpis:
        st.markdown(
            "<div style='font-size:0.78rem;color:#94a3b8;font-weight:600;"
            "text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px'>"
            "📊 Database</div>",
            unsafe_allow_html=True,
        )
        for _lbl, _key in [
            ("Docs", "total_docs"),
            ("Signals", "total_signals"),
            ("Themes", "active_themes"),
            ("Chains", "active_chains"),
        ]:
            _v = _kpis.get(_key, 0)
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;'
                f'font-size:0.80rem;padding:1px 0">'
                f'<span style="color:#94a3b8">{_lbl}</span>'
                f'<span style="color:#818cf8;font-weight:700">{_v:,}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("---")
    st.markdown(
        f'<div style="font-size:0.72rem;color:#334155;text-align:center">'
        f'v0.2.0 · {date.today()}</div>',
        unsafe_allow_html=True,
    )

# ─────────────────────────────────────────────────────────────────────────────
# TABS  (5 tabs)
# ─────────────────────────────────────────────────────────────────────────────

tab_run, tab_concall, tab_themes, tab_shortlisted, tab_ranking, tab_ai, tab_macro, tab_company = st.tabs([
    "🚀  Pipeline Runner",
    "📞  Concall & Filings",
    "🗺️  Themes & Companies",
    "⭐  Shortlisted Themes",
    "🏆  Stock Rankings",
    "🤖  AI Analysis",
    "🌐  Macro & Policy",
    "🏢  Company Explorer",
])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 1 — PIPELINE RUNNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_run:
    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:10px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} Pipeline running for <b>{COUNTRY_LABEL}</b> — '
        f'change market in the sidebar ←</div>',
        unsafe_allow_html=True,
    )
    st.markdown("#### Configure & Run")

    # ── Mode selector ────────────────────────────────────────────────────────
    mode = st.radio(
        "Run mode",
        ["Live / Single-batch run", "Historical Replay (month-by-month)"],
        horizontal=True,
        help=(
            "Live: fetches everything since the last checkpoint up to today. "
            "Historical Replay: fetches filings in monthly windows and snapshots "
            "themes with the replay date."
        ),
    )
    is_replay = mode.startswith("Historical")

    col_l, col_r = st.columns(2)

    with col_l:
        start_date = st.date_input(
            "Start date",
            value=date(2022, 1, 1) if is_replay else (date.today() - timedelta(days=90)),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
        )

    with col_r:
        end_date = st.date_input(
            "End date  (replay ceiling / live cutoff)",
            value=date(2022, 12, 31) if is_replay else date.today(),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
        )

    # ── Company Universe (US) / India Sources ────────────────────────────────
    if COUNTRY_CODE == "US":
        st.markdown("**Company Universe**")
        _selected_tickers = cfg.get("edgar", {}).get("ticker_list", [])
        _universe_options = [
            f"Selected companies ({len(_selected_tickers)} tickers from config)",
            "All US companies — batched  (process N per run, auto-advances each click)",
            "All US companies — complete  (all ~6 000, slow, one run covers everything)",
        ]
        _universe_choice = st.selectbox(
            "Company universe",
            _universe_options,
            index=0,
            label_visibility="collapsed",
        )

        _fetch_mode_map = {
            _universe_options[0]: "selected",
            _universe_options[1]: "all_us",
            _universe_options[2]: "all_us_complete",
        }
        _fetch_mode_ui = _fetch_mode_map[_universe_choice]

        if _fetch_mode_ui == "all_us_complete":
            _max_co = 999999
            st.warning(
                "**Complete run selected.** This will fetch all ~6 000 NYSE + NASDAQ companies. "
                "Expect several hours per replay month. Leave it running — it's fine to be slow."
            )
        elif _fetch_mode_ui == "all_us":
            _max_co = st.slider(
                "Max companies per run",
                min_value=50, max_value=1000, value=200, step=50,
                help="Each run advances automatically to the next slice.",
            )
            _offset_file = ROOT / "data" / "db" / "edgar_company_offset.json"
            if _offset_file.exists():
                import json as _json
                try:
                    _cur_offset = int(_json.loads(_offset_file.read_text()).get("offset", 0))
                    _next_end = _cur_offset + _max_co
                    st.info(
                        f"Next batch: companies **{_cur_offset + 1} → {_next_end}** "
                        f"(≈{round(_cur_offset / 6000 * 100, 1)}% of universe done). "
                        "Each click automatically advances to the next slice."
                    )
                except Exception:
                    pass
            reset_offset = st.checkbox("Reset to company 1 (restart full cycle)", value=False)
            if reset_offset:
                try:
                    import json as _json
                    _offset_file = ROOT / "data" / "db" / "edgar_company_offset.json"
                    _offset_file.parent.mkdir(parents=True, exist_ok=True)
                    _offset_file.write_text(_json.dumps({"offset": 0}, indent=2))
                    st.success("Offset reset — next run starts from company 1.")
                except Exception as e:
                    st.error(f"Could not reset offset: {e}")
        else:
            _max_co = cfg.get("edgar", {}).get("max_companies_per_run", 200)

    else:  # India
        _fetch_mode_ui = "selected"   # not used by India path
        _max_co = 200                  # not used by India path
        st.markdown("**India Data Sources**")
        _nse_syms    = cfg.get("nse", {}).get("symbol_list", [])
        _scr_syms    = cfg.get("screener", {}).get("symbol_list", [])
        _india_sources_cfg = [
            ("NSE India",     cfg.get("nse", {}).get("enabled", True),
             f"{len(_nse_syms)} symbols" if _nse_syms else "all listed"),
            ("BSE India",     cfg.get("bse", {}).get("enabled", True),
             "all listed"),
            ("Screener.in",   cfg.get("screener", {}).get("enabled", True),
             f"{len(_scr_syms)} symbols" if _scr_syms else "all NSE listed"),
            ("PIB India",     cfg.get("pib", {}).get("enabled", True),
             "keyword filter"),
            ("Invest India",  cfg.get("invest_india", {}).get("enabled", True),
             "sector reports"),
            ("Commerce/DGFT", cfg.get("commerce_india", {}).get("enabled", True),
             "notifications"),
        ]
        _src_cols = st.columns(len(_india_sources_cfg))
        for _col, (_src_name, _src_enabled, _src_detail) in zip(_src_cols, _india_sources_cfg):
            _status = "✅" if _src_enabled else "⏸️"
            _col.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
                f'padding:8px 10px;text-align:center">'
                f'<div style="font-size:0.78rem;color:#94a3b8">{_status} {_src_name}</div>'
                f'<div style="font-size:0.72rem;color:#475569;margin-top:2px">{_src_detail}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        st.caption("Configure symbols and keywords in `config/settings.yaml` under each source block.")

    # ── Stage toggles ────────────────────────────────────────────────────────
    st.markdown("**Stages to run**")
    s1, s2, s3, s4, s5, s6, s7 = st.columns(7)
    do_ingest        = s1.checkbox("Ingest",         value=True)
    do_nlp           = s2.checkbox("NLP",            value=True)
    do_graph         = s3.checkbox("Graph",          value=True)
    do_events        = s4.checkbox("Events",         value=True)
    do_causal        = s5.checkbox("Causal",         value=True)
    do_themes        = s6.checkbox("Themes",         value=True)
    do_contradictions = s7.checkbox("Contradictions", value=True)

    # India-only: PDF content fetch for high-value filing categories
    do_pdf_fetch_india = False
    _pdf_fetch_workers = 4
    if COUNTRY_CODE == "IN":
        if is_replay:
            # Historical mode — PDF fetch is baked into each monthly iteration
            do_pdf_fetch_india = st.checkbox(
                "📄 PDF Fetch + NLP per month (India historical mode) "
                "— each month: ingest announcements → download PDFs → extract text to DB "
                "→ delete files → run NLP. Zero disk accumulation.",
                value=True,
                help="Recommended for historical runs. Text is stored in the database "
                     "(raw_text column) so PDFs are never kept on disk. "
                     "Uncheck only if you want to ingest metadata only and run PDF fetch separately.",
            )
        else:
            do_pdf_fetch_india = st.checkbox(
                "📄 Fetch PDFs for high-value India filings "
                "(Press Release, Board Meeting outcomes, Order wins, Acquisitions, Transcripts) "
                "— downloads PDFs and extracts text for richer NLP signals.",
                value=False,
            )
            if do_pdf_fetch_india:
                _pdf_fetch_workers = st.slider(
                    "PDF fetch parallel workers", min_value=1, max_value=8, value=6,
                    help="How many PDFs to download simultaneously. 6 is a safe default.",
                )

    skip_neo4j = st.checkbox("Skip Neo4j (graph stage)", value=False)

    # NLP batch size — visible only when NLP is selected
    _nlp_batch_size = 500
    if do_nlp:
        _nlp_col1, _nlp_col2 = st.columns([2, 3])
        _nlp_batch_size = _nlp_col1.number_input(
            "NLP chunk size (docs per memory batch)", min_value=10, max_value=5000,
            value=500, step=100, key="nlp_batch_size",
            help="All documents in the selected date range will be processed. "
                 "This controls how many are loaded into memory at once. "
                 "500 is safe; increase to 1000–2000 if you have plenty of RAM.",
        )

    # ── Resume (replay only) ─────────────────────────────────────────────────
    resume_from = None
    if is_replay:
        replay_history = load_replay_history()
        if replay_history:
            last_batch = replay_history[0]["replay_batch"]
            st.info(f"Last completed replay batch: **{last_batch}** — tick 'Resume' to skip already-processed months.")
        resume = st.checkbox("Resume from last completed batch", value=bool(replay_history))
        if resume and replay_history:
            last_date = replay_history[0]["replay_date"]
            if isinstance(last_date, str):
                last_date = date.fromisoformat(last_date)
            resume_from = last_date + timedelta(days=1)
            st.caption(f"Will resume from **{resume_from}**")

    st.markdown("---")

    # ── RUN button ───────────────────────────────────────────────────────────
    run_clicked = st.button("▶  Run Pipeline", type="primary", use_container_width=False)

    log_placeholder = st.empty()
    stats_placeholder = st.empty()

    if run_clicked:
        if start_date >= end_date:
            st.error("End date must be after start date.")
        else:
            # Apply company-universe overrides to a shallow copy of the config
            # so the in-memory cfg dict used by the rest of the app is untouched.
            run_cfg = copy.deepcopy(cfg)
            run_cfg.setdefault("edgar", {})
            run_cfg["edgar"]["fetch_mode"] = _fetch_mode_ui
            run_cfg["edgar"]["max_companies_per_run"] = _max_co
            run_cfg.setdefault("market", {})["country"] = COUNTRY_CODE

            log_lines: list[str] = []

            def _update_log(handler):
                new = handler.records[len(log_lines):]
                log_lines.extend(new)
                log_placeholder.markdown(
                    '<div class="log-box">' +
                    "\n".join(log_lines[-80:]) +          # last 80 lines
                    "</div>",
                    unsafe_allow_html=True,
                )

            with _capture_logs() as handler:
                if is_replay:
                    # ── Historical Replay ──────────────────────────────────
                    from makrograph.pipeline.historical_runner import HistoricalRunner

                    runner = HistoricalRunner(
                        config=run_cfg,
                        start_date=start_date,
                        end_date=end_date,
                        replay_mode="monthly",
                        skip_ingest=not do_ingest,
                        skip_neo4j=skip_neo4j,
                        skip_nlp=not do_nlp,
                        skip_graph=not do_graph,
                        skip_events=not do_events,
                        skip_causal=not do_causal,
                        skip_themes=not do_themes,
                        skip_pdf_fetch=not do_pdf_fetch_india,
                    )

                    with st.spinner("Running historical replay…"):
                        runner._init_pipeline()
                        from makrograph.pipeline.historical_runner import generate_monthly_timeline
                        timeline = generate_monthly_timeline(start_date, end_date)

                        all_results = []
                        prog = st.progress(0, text="Initialising…")

                        for idx, (ws, we) in enumerate(timeline):
                            if resume_from and we < resume_from:
                                prog.progress((idx + 1) / len(timeline), text=f"Skipping {we.strftime('%Y-%m')}…")
                                continue

                            prog.progress((idx + 1) / len(timeline), text=f"Replay {we.strftime('%Y-%m')} …")
                            result = runner._run_month(ws, we)
                            runner._log_result(result)
                            all_results.append(result)
                            _update_log(handler)

                        prog.empty()
                        runner._close()

                    # Summary table
                    if all_results:
                        import pandas as pd
                        df = pd.DataFrame([r.to_dict() for r in all_results])
                        disp_cols = ["replay_batch", "docs_ingested", "docs_nlp",
                                     "themes_detected", "themes_snapped",
                                     "causal_score", "duration_sec", "status"]
                        df_disp = df[[c for c in disp_cols if c in df.columns]]
                        df_disp.columns = [c.replace("_", " ").title() for c in df_disp.columns]
                        with stats_placeholder.container():
                            st.success(f"Replay complete — {len(all_results)} months processed")
                            st.dataframe(df_disp, use_container_width=True, hide_index=True)

                else:
                    # ── Live / single-batch run ────────────────────────────
                    from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
                    from datetime import timezone

                    since_dt = datetime(
                        start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc
                    )
                    until_dt = datetime(
                        end_date.year, end_date.month, end_date.day,
                        23, 59, 59, tzinfo=timezone.utc
                    )

                    pipeline = IntelligencePipeline(run_cfg)

                    with st.spinner("Running pipeline…"):
                        pipeline._init_storage()
                        all_stats: dict = {}

                        if do_ingest:
                            prog_label = st.empty()
                            prog_label.caption("Stage: Ingest")
                            if COUNTRY_CODE == "IN":
                                all_stats["ingest"] = pipeline.run_ingest_india(
                                    since=since_dt, until=until_dt
                                )
                            else:
                                all_stats["ingest"] = pipeline.run_ingest(since=since_dt)
                            _update_log(handler)

                        # PDF fetch: runs in Live mode only (heavy download, not per-month)
                        if do_pdf_fetch_india and not is_replay:
                            prog_label = st.empty()
                            prog_label.caption(
                                f"Stage: PDF Fetch (India — up to 145K docs, "
                                f"{_pdf_fetch_workers} workers)…"
                            )
                            all_stats["pdf_fetch_india"] = pipeline.run_pdf_fetch_india(
                                max_workers=_pdf_fetch_workers,
                            )
                            _update_log(handler)

                        if do_nlp:
                            pipeline._init_nlp()
                            all_stats["nlp"] = pipeline.run_nlp(
                                batch_size=_nlp_batch_size,
                                window_start=start_date,
                                window_end=end_date,
                                country=COUNTRY_CODE,
                            )
                            _update_log(handler)

                        if do_graph and not skip_neo4j:
                            pipeline._init_graph_builder()
                            all_stats["graph"] = pipeline.run_graph(
                                window_start=start_date,
                                window_end=end_date,
                                country=COUNTRY_CODE,
                            )
                            _update_log(handler)

                        if do_events:
                            pipeline._init_intelligence()
                            all_stats["events"] = pipeline.run_events(
                                window_start=start_date,
                                window_end=end_date,
                                country=COUNTRY_CODE,
                            )
                            _update_log(handler)

                        if do_causal:
                            all_stats["causal"] = pipeline.run_causal_chains(
                                as_of_date=end_date
                            )
                            _update_log(handler)

                        if do_themes:
                            pipeline._init_themes()
                            all_stats["themes"] = pipeline.run_themes(
                                as_of_date=end_date,
                                country=COUNTRY_CODE,
                            )
                            _update_log(handler)

                        if do_contradictions:
                            all_stats["contradictions"] = pipeline.run_contradictions()
                            _update_log(handler)

                        pipeline.close()

                    with stats_placeholder.container():
                        st.success("Pipeline run complete")
                        for stage, stats in all_stats.items():
                            with st.expander(f"📋 {stage.upper()} stats"):
                                cols = st.columns(len(stats))
                                for col, (k, v) in zip(cols, stats.items()):
                                    col.metric(k.replace("_", " ").title(), v)

            # Force theme cache refresh
            load_themes.clear()
            load_pipeline_kpis.clear()
            load_replay_history.clear()

    # ── Replay history ───────────────────────────────────────────────────────
    replay_hist = load_replay_history()
    if replay_hist:
        st.markdown("---")
        st.markdown("#### Replay History")
        import pandas as pd
        df_h = pd.DataFrame(replay_hist)
        disp = ["replay_batch", "docs_ingested", "docs_nlp",
                "themes_detected", "causal_score", "duration_sec", "status"]
        df_h = df_h[[c for c in disp if c in df_h.columns]]
        df_h.columns = [c.replace("_", " ").title() for c in df_h.columns]
        st.dataframe(df_h, use_container_width=True, hide_index=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2 — THEMES & COMPANIES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@st.cache_data(ttl=30, show_spinner=False)
def load_themes_as_of(as_of: date, from_d: date, min_s: float, country: str = "US"):
    if not pg:
        return []
    return pg.get_themes_as_of(as_of_date=as_of, from_date=from_d, min_strength=min_s, country=country)

@st.cache_data(ttl=30, show_spinner=False)
def load_beneficiaries_as_of(theme_id: int, as_of: date):
    if not pg:
        return []
    return pg.get_beneficiaries_as_of(theme_id, as_of)

@st.cache_data(ttl=30, show_spinner=False)
def load_snapshots_window(theme_id: int, from_d: date, to_d: date):
    if not pg:
        return []
    return pg.get_snapshots_in_window(theme_id, from_d, to_d)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2 — CONCALL & FILINGS ANALYSIS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@st.cache_data(ttl=60, show_spinner=False)
def load_concall_docs(country: str, from_d: str, to_d: str,
                      ticker_q: str, ftype: str, limit: int):
    if not pg:
        return []
    try:
        return pg.get_concall_documents(
            country=country,
            from_date=date.fromisoformat(from_d) if from_d else None,
            to_date=date.fromisoformat(to_d) if to_d else None,
            ticker_search=ticker_q or None,
            filing_type_filter=ftype,
            limit=limit,
        )
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner=False)
def load_doc_signals(doc_id: int):
    if not pg:
        return []
    try:
        return pg.get_document_signals(doc_id, limit=60)
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner=False)
def load_doc_themes(doc_id: int):
    if not pg:
        return []
    try:
        return pg.get_document_theme_contributions(doc_id)
    except Exception:
        return []


with tab_concall:
    # Country banner
    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:12px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} <b>{COUNTRY_LABEL}</b> filings — '
        f'change market in the sidebar ←  &nbsp;|&nbsp; '
        f'<b>Country</b> and <b>date</b> are stored with every document for multi-market analysis.</div>',
        unsafe_allow_html=True,
    )

    if COUNTRY_CODE == "IN":
        st.info(
            "🇮🇳 **India pipeline coming soon.**\n\n"
            "NSE/BSE concall transcripts + quarterly results ingestion will be added in the next phase. "
            "The country field (`country = 'IN'`) is already embedded in every document record — "
            "simply ingest India data and it will appear here automatically.",
        )
    else:
        # ── Filter controls ──────────────────────────────────────────────────
        st.markdown("#### 🔍 Filters")
        cf1, cf2, cf3, cf4, cf5 = st.columns([2, 2, 2, 2, 1])

        with cf1:
            cc_from = st.date_input(
                "From date",
                value=date(2022, 1, 1),
                min_value=date(2000, 1, 1),
                max_value=date.today(),
                key="cc_from",
                help="Filing date range — start. Supports historical dates back to 2000.",
            )
        with cf2:
            cc_to = st.date_input(
                "To date",
                value=date.today(),
                min_value=date(2000, 1, 1),
                max_value=date.today(),
                key="cc_to",
            )
        with cf3:
            cc_ticker = st.text_input(
                "Ticker / Company search",
                value="",
                placeholder="e.g. NVDA or NVIDIA",
                key="cc_ticker",
            )
        with cf4:
            cc_ftype = st.selectbox(
                "Filing type",
                ["All", "10-K", "10-Q", "8-K", "DEF 14A", "S-1"],
                key="cc_ftype",
            )
        with cf5:
            cc_limit = st.selectbox("Rows", [50, 100, 200, 500], index=1, key="cc_limit")

        st.markdown(
            '<div style="font-size:0.75rem;color:#475569;margin-bottom:8px">'
            '📅 <b>Date</b> is sourced from <code>filed_at</code> on each document — '
            'exact filing date from SEC EDGAR. Use historical dates to explore any period.</div>',
            unsafe_allow_html=True,
        )

        if st.button("🔄 Refresh Filings", key="cc_refresh"):
            load_concall_docs.clear()
            load_doc_signals.clear()
            load_doc_themes.clear()
            st.rerun()

        # ── Document table ───────────────────────────────────────────────────
        import pandas as _pd_cc
        docs = load_concall_docs(
            COUNTRY_CODE, str(cc_from), str(cc_to),
            cc_ticker.strip(), cc_ftype, int(cc_limit),
        )

        if not docs:
            st.info(
                "No documents found for the selected filters. "
                "Run the Pipeline to ingest SEC filings first."
            )
        else:
            st.markdown(
                f'<div style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">'
                f'<b>{len(docs)}</b> filings found · country: <b>{COUNTRY_CODE}</b> · '
                f'{cc_from} → {cc_to}</div>',
                unsafe_allow_html=True,
            )

            # Build display dataframe
            df_cc = _pd_cc.DataFrame([
                {
                    "Date":       str(d.get("filed_at", "") or "")[:10],
                    "Ticker":     d.get("ticker") or "—",
                    "Company":    (d.get("company") or "")[:40],
                    "Type":       d.get("filing_type") or d.get("doc_type") or "—",
                    "Period":     d.get("fiscal_period") or "—",
                    "Country":    d.get("country") or "US",
                    "Words":      int(d.get("word_count") or 0),
                    "Signals":    int(d.get("signal_count") or 0),
                    "Entities":   int(d.get("entity_count") or 0),
                    "Avg Conf":   float(d.get("avg_confidence") or 0),
                    "Status":     d.get("processing_status") or "—",
                    "_id":        d["id"],
                }
                for d in docs
            ])

            # Colour-code signal count column
            max_sig = max(int(d.get("signal_count") or 1) for d in docs) or 1

            col_left, col_right = st.columns([3, 2], gap="large")

            with col_left:
                st.markdown("##### 📄 Filings")
                display_cols = ["Date", "Ticker", "Company", "Type", "Period",
                                "Country", "Words", "Signals", "Entities", "Status"]
                selected_rows = st.dataframe(
                    df_cc[display_cols],
                    use_container_width=True,
                    hide_index=True,
                    height=420,
                    on_select="rerun",
                    selection_mode="single-row",
                    column_config={
                        "Signals": st.column_config.ProgressColumn(
                            "Signals", min_value=0, max_value=max_sig, format="%d",
                        ),
                        "Date": st.column_config.TextColumn("📅 Date", width="small"),
                        "Words": st.column_config.NumberColumn("Words", format="%d"),
                        "Avg Conf": st.column_config.NumberColumn("Avg Conf", format="%.3f"),
                        "Country": st.column_config.TextColumn("🌍", width="small"),
                    },
                )

            with col_right:
                # If user selected a row → show detail
                sel_idx = (
                    selected_rows.selection.rows[0]
                    if selected_rows and selected_rows.selection.rows
                    else None
                )

                if sel_idx is not None:
                    sel_doc = docs[sel_idx]
                    doc_id  = sel_doc["id"]

                    # Doc header
                    st.markdown(
                        f'<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;'
                        f'padding:12px 16px;margin-bottom:10px">'
                        f'<div style="font-weight:700;color:#fff;font-size:0.92rem">'
                        f'{sel_doc.get("ticker","?")} — {(sel_doc.get("company") or "")[:50]}</div>'
                        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px">'
                        f'<span style="font-size:0.75rem;color:#818cf8">'
                        f'📅 Filed: {str(sel_doc.get("filed_at",""))[:10]}</span>'
                        f'<span style="font-size:0.75rem;color:#94a3b8">'
                        f'📋 {sel_doc.get("filing_type","?")} · {sel_doc.get("fiscal_period","")}</span>'
                        f'<span style="font-size:0.75rem;color:#94a3b8">'
                        f'🌍 {sel_doc.get("country","US")}</span>'
                        f'<span style="font-size:0.75rem;color:#94a3b8">'
                        f'📝 {int(sel_doc.get("word_count") or 0):,} words</span>'
                        f'</div>'
                        f'<div style="font-size:0.72rem;color:#475569;margin-top:4px">'
                        f'{(sel_doc.get("title") or "")[:120]}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    dtab_sigs, dtab_doc_themes = st.tabs(["⚡ Signals", "🗺️ Theme Links"])

                    with dtab_sigs:
                        sigs = load_doc_signals(doc_id)
                        if sigs:
                            DIR_C = {
                                "increasing":"#22c55e", "positive":"#22c55e",
                                "decreasing":"#ef4444", "negative":"#ef4444",
                                "neutral":"#94a3b8",
                            }
                            for sg in sigs[:20]:
                                dc = DIR_C.get(sg.get("direction","neutral"), "#94a3b8")
                                st.markdown(
                                    f'<div style="border-left:3px solid {dc};'
                                    f'background:#0f0f0f;border-radius:6px;'
                                    f'padding:7px 10px;margin-bottom:5px">'
                                    f'<div style="display:flex;justify-content:space-between">'
                                    f'<span style="color:#cbd5e1;font-size:0.78rem;font-weight:600">'
                                    f'{sg.get("signal_type","?")}'
                                    f'{"  ·  " + sg.get("entity_name","") if sg.get("entity_name") else ""}'
                                    f'</span>'
                                    f'<span style="font-size:0.70rem;color:{dc}">'
                                    f'{sg.get("direction","?")}'
                                    f' · conf: {float(sg.get("confidence") or 0):.2f}'
                                    f'</span></div>'
                                    f'<div style="font-size:0.74rem;color:#94a3b8;'
                                    f'font-style:italic;margin-top:3px">'
                                    f'"{(sg.get("context_text") or "")[:220]}"'
                                    f'</div></div>',
                                    unsafe_allow_html=True,
                                )
                        else:
                            st.caption("No signals extracted yet — run the NLP stage.")

                    with dtab_doc_themes:
                        doc_themes = load_doc_themes(doc_id)
                        if doc_themes:
                            for dt in doc_themes:
                                conv = dt.get("conviction","emerging")
                                tc = CONVICTION_COLOR.get(conv, "#6366f1")
                                st.markdown(
                                    f'<div style="background:#1e293b;border:1px solid #334155;'
                                    f'border-radius:8px;padding:8px 12px;margin-bottom:5px;'
                                    f'display:flex;justify-content:space-between;align-items:center">'
                                    f'<div><div style="font-weight:700;color:#fff;font-size:0.82rem">'
                                    f'{dt["theme_name"]}</div>'
                                    f'<div style="font-size:0.70rem;color:#475569">'
                                    f'{dt["theme_slug"]}</div></div>'
                                    f'<div style="text-align:right">'
                                    f'<div style="color:#818cf8;font-weight:700">'
                                    f'{int(dt.get("signal_count",0))} signals</div>'
                                    f'{_badge(conv, tc)}'
                                    f'</div></div>',
                                    unsafe_allow_html=True,
                                )
                        else:
                            st.caption("No linked themes yet — run NLP + Themes stages.")
                else:
                    st.markdown(
                        '<div style="background:#1e293b;border:1px dashed #334155;border-radius:10px;'
                        'padding:40px 20px;text-align:center;color:#475569;font-size:0.85rem">'
                        '← Click a row to see signals & theme links</div>',
                        unsafe_allow_html=True,
                    )

            # ── Summary stats strip ──────────────────────────────────────────
            st.markdown("---")
            _total_sigs = sum(int(d.get("signal_count") or 0) for d in docs)
            _total_words = sum(int(d.get("word_count") or 0) for d in docs)
            _companies = len({d.get("ticker") for d in docs if d.get("ticker")})
            _filing_types = sorted({d.get("filing_type") for d in docs if d.get("filing_type")})
            ss1, ss2, ss3, ss4, ss5 = st.columns(5)
            for _col, _lbl, _val in [
                (ss1, "Filings",        f"{len(docs):,}"),
                (ss2, "Companies",      f"{_companies:,}"),
                (ss3, "Total Signals",  f"{_total_sigs:,}"),
                (ss4, "Total Words",    f"{_total_words:,}"),
                (ss5, "Filing Types",   ", ".join(_filing_types[:4])),
            ]:
                _col.markdown(
                    f'<div class="kpi-card">'
                    f'<div class="kpi-num" style="font-size:1.3rem">{_val}</div>'
                    f'<div class="kpi-label">{_lbl}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


with tab_themes:
    # ── Country context banner ─────────────────────────────────────────────
    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:10px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} Showing themes for <b>{COUNTRY_LABEL}</b> — '
        f'change market in the sidebar ← '
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── 🔖 CANONICAL REVIEW PANEL ─────────────────────────────────────────
    # Workflow:
    #   1. Copy the combined prompt (one box, all clusters).
    #   2. Paste into Claude / GPT / any LLM chat manually.
    #   3. Copy the numbered response back here.
    #   4. Click "Parse & Approve All" — done in one shot.
    _pending_reviews: list[dict] = []
    if pg:
        try:
            _pending_reviews = pg.get_pending_canonical_reviews()
        except Exception:
            _pending_reviews = []

    if _pending_reviews:
        from makrograph.storage.pg_store import PGStore as _PGStore

        # Build the combined prompt once
        _combined_prompt = _PGStore.build_combined_canonical_prompt(_pending_reviews)

        with st.expander(
            f"🔖 {len(_pending_reviews)} Theme Cluster{'s' if len(_pending_reviews)!=1 else ''} "
            f"Need Canonical Names  ▸ expand to review",
            expanded=st.session_state.get("canon_review_open", False),
        ):
            st.session_state["canon_review_open"] = True

            st.markdown(
                '<div style="background:#292103;border-left:4px solid #f59e0b;'
                'border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:0.82rem;color:#d97706">'
                '<b>How to use:</b> '
                '① Copy the prompt below → '
                '② Paste into any LLM (Claude, ChatGPT, etc.) → '
                '③ Copy the numbered response → '
                '④ Paste into the response box → '
                '⑤ Click <b>Parse &amp; Approve All</b>'
                '</div>',
                unsafe_allow_html=True,
            )

            # ── Cluster preview table ──────────────────────────────────────
            st.markdown("**Clusters detected:**")
            for _i, _rev in enumerate(_pending_reviews, 1):
                _mnames = _rev.get("member_names") or []
                _suggest = _rev.get("suggested_name", "")
                st.markdown(
                    f'<div style="background:#1e293b;border-left:3px solid #f59e0b;'
                    f'border-radius:6px;padding:5px 12px;margin-bottom:4px;font-size:0.8rem">'
                    f'<b style="color:#fbbf24">Cluster {_i}:</b> '
                    f'<span style="color:#e2e8f0">{" + ".join(_mnames)}</span>'
                    f'<span style="color:#4b5563;margin-left:10px">→ auto: <i>{_suggest}</i></span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            st.markdown("---")

            # ── Step 1: Combined prompt ────────────────────────────────────
            st.markdown("**① Copy this prompt → paste into your LLM:**")
            st.text_area(
                label="combined_prompt",
                value=_combined_prompt,
                height=260,
                disabled=True,
                label_visibility="collapsed",
                key="canon_combined_prompt",
                help="Select all (Ctrl+A / ⌘+A) and copy, then paste into Claude, ChatGPT, etc.",
            )

            st.markdown("---")

            # ── Step 2: Paste LLM response ────────────────────────────────
            st.markdown(
                "**③ Paste the LLM's numbered response here:**  "
                "*Expected format: one line per cluster, e.g.* `1. AI Infrastructure Power Constraint`"
            )
            _llm_response = st.text_area(
                label="llm_response_input",
                value=st.session_state.get("canon_llm_response", ""),
                height=160,
                placeholder=(
                    "1. AI Infrastructure Power Constraint\n"
                    "2. HBM Supply Constraint from AI Demand\n"
                    "3. EV Battery Materials Shortage"
                ),
                label_visibility="collapsed",
                key="canon_llm_response_input",
            )

            # ── Step 3: Parse + Approve ────────────────────────────────────
            _pa_col, _dismiss_all_col, _preview_col = st.columns([2, 1, 3])

            with _pa_col:
                if st.button(
                    "✅ Parse & Approve All", key="canon_bulk_approve", type="primary"
                ):
                    import re as _re
                    _parsed: dict[str, str] = {}   # cluster_id → approved_name
                    _parse_errors: list[str] = []

                    # Parse numbered lines: "1. Name Here" or "1) Name Here"
                    _lines = [l.strip() for l in _llm_response.strip().splitlines() if l.strip()]
                    _numbered = []
                    for _line in _lines:
                        _m = _re.match(r'^(\d+)[.)]\s+(.+)$', _line)
                        if _m:
                            _numbered.append((int(_m.group(1)), _m.group(2).strip()))

                    if not _numbered:
                        st.error(
                            "Could not parse any numbered names from the response. "
                            "Expected format: `1. Canonical Name Here`"
                        )
                    else:
                        # Match by position (1-indexed) to _pending_reviews order
                        for _num, _name in _numbered:
                            _idx = _num - 1
                            if 0 <= _idx < len(_pending_reviews):
                                _cid = _pending_reviews[_idx]["cluster_id"]
                                _parsed[_cid] = _name
                            else:
                                _parse_errors.append(
                                    f"Line {_num} has no matching cluster (only {len(_pending_reviews)} clusters)"
                                )

                        if _parsed:
                            try:
                                _approved_count = pg.bulk_approve_canonical_reviews(_parsed)
                                st.success(
                                    f"✅ Approved {_approved_count} canonical name{'s' if _approved_count!=1 else ''}! "
                                    f"Next pipeline run will use these names."
                                )
                                # Show what was approved
                                for _cid, _name in _parsed.items():
                                    st.markdown(f"  • **{_name}**")
                                for _err in _parse_errors:
                                    st.warning(_err)
                                st.session_state["canon_review_open"] = False
                                st.rerun()
                            except Exception as _e:
                                st.error(f"Bulk approve failed: {_e}")
                        else:
                            st.error("No valid cluster → name mappings could be built from the response.")

            with _dismiss_all_col:
                if st.button("✗ Dismiss All", key="canon_dismiss_all", type="secondary"):
                    _dismissed = 0
                    for _rev in _pending_reviews:
                        try:
                            pg.dismiss_canonical_review(_rev["cluster_id"])
                            _dismissed += 1
                        except Exception:
                            pass
                    if _dismissed:
                        st.info(f"Dismissed {_dismissed} cluster(s). These themes will stay separate.")
                        st.session_state["canon_review_open"] = False
                        st.rerun()

            with _preview_col:
                if _llm_response.strip():
                    # Live preview of parsed names
                    import re as _re2
                    _preview_lines = []
                    for _line in _llm_response.strip().splitlines():
                        _m2 = _re2.match(r'^(\d+)[.)]\s+(.+)$', _line.strip())
                        if _m2:
                            _n2, _nm2 = int(_m2.group(1)), _m2.group(2).strip()
                            if 1 <= _n2 <= len(_pending_reviews):
                                _preview_lines.append(
                                    f'<div style="font-size:0.78rem;color:#86efac">'
                                    f'✓ Cluster {_n2} → <b>{_nm2}</b></div>'
                                )
                            else:
                                _preview_lines.append(
                                    f'<div style="font-size:0.78rem;color:#f87171">'
                                    f'✗ Line {_n2}: no matching cluster</div>'
                                )
                    if _preview_lines:
                        st.markdown("**Preview:**")
                        st.markdown(
                            '<div style="background:#052e16;border-radius:6px;padding:8px 12px">'
                            + "".join(_preview_lines) + "</div>",
                            unsafe_allow_html=True,
                        )

        st.divider()

    # ── Ranking Table ────────────────────────────────────────────────────────
    with st.expander("📊 Theme Ranking Table", expanded=True):
        _ranking_rows = load_ranking_table(country=COUNTRY_CODE)
        if _ranking_rows:
            import pandas as _pd_rank
            _display_df = _pd_rank.DataFrame(
                [{k: v for k, v in row.items() if k != "_slug"} for row in _ranking_rows]
            )
            st.dataframe(
                _display_df,
                use_container_width=True,
                hide_index=True,
                height=min(50 + len(_ranking_rows) * 36, 600),
                column_config={
                    "#":          st.column_config.NumberColumn("#", width="small"),
                    "Theme":      st.column_config.TextColumn("Theme", width="large"),
                    "Score":      st.column_config.NumberColumn("Score", format="%.1f", width="small"),
                    "D/S":        st.column_config.TextColumn("D/S", width="small",
                                      help="Demand signals / Supply signals"),
                    "Conv":       st.column_config.TextColumn("Conv", width="small"),
                    "Cos":        st.column_config.NumberColumn("Cos", width="small",
                                      help="Companies citing this theme"),
                    "Q":          st.column_config.NumberColumn("Q", width="small",
                                      help="Confirmed quarters"),
                    "Pers":       st.column_config.NumberColumn("Pers", format="%.2f",
                                      width="small", help="Persistence multiplier"),
                    "Elig":       st.column_config.NumberColumn("Elig", format="%.2f",
                                      width="small", help="Eligibility score 0–1"),
                    "Type":       st.column_config.TextColumn("Type", width="small"),
                    "First Seen": st.column_config.TextColumn("First Seen",
                                      help="Date this theme was first detected by the pipeline"),
                    "Freshness":  st.column_config.TextColumn("Freshness",
                                      help="🟢 Fresh <90d · 🟡 Active 90–365d · 🔴 Mature >365d (potential exhaustion)"),
                },
            )
            _sel_theme = st.selectbox(
                "Jump to theme",
                options=[r["Theme"] for r in _ranking_rows],
                index=None,
                placeholder="Select a theme to inspect…",
                key="ranking_jump",
                label_visibility="collapsed",
            )
            if _sel_theme:
                _slug = next(
                    (r["_slug"] for r in _ranking_rows if r["Theme"] == _sel_theme), None
                )
                if _slug:
                    st.session_state["selected_theme_slug"] = _slug
                    st.rerun()
        else:
            st.caption("No ranked themes yet — run the intelligence pipeline first.")

    # ── Filter bar ───────────────────────────────────────────────────────────
    st.markdown("#### Filters")
    fc1, fc2, fc3, fc4, fc5, fc6 = st.columns([2, 2, 1.5, 2, 1, 1])

    with fc1:
        t2_from = st.date_input(
            "From date",
            value=date(2020, 1, 1),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
            key="t2_from",
        )
    with fc2:
        t2_to = st.date_input(
            "As-of date",
            value=date.today(),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
            key="t2_to",
            help="Show themes and companies as they looked on this date",
        )
    with fc3:
        t2_strength = st.slider("Min score", 0, 100, 0, 5, key="t2_str")
    with fc4:
        t2_conviction = st.multiselect(
            "Conviction",
            ["confirmed", "developing", "emerging"],
            default=["confirmed", "developing", "emerging"],
            key="t2_conv",
        )
    with fc5:
        use_live = st.checkbox("Live (current)", value=(t2_to == date.today()), key="t2_live")
    with fc6:
        if st.button("🔄 Refresh", key="t2_refresh"):
            load_themes.clear()
            load_themes_as_of.clear()
            load_beneficiaries.clear()
            load_beneficiaries_as_of.clear()
            load_snapshots.clear()
            load_snapshots_window.clear()
            load_causal_chains.clear()
            load_pipeline_kpis.clear()
            load_ranking_table.clear()
            st.rerun()

    # ── Date mode indicator ──────────────────────────────────────────────────
    if use_live:
        st.markdown(
            '<div style="background:#172554;border-left:3px solid #3b82f6;'
            'border-radius:6px;padding:7px 14px;margin-bottom:10px;'
            'color:#93c5fd;font-size:0.82rem">📡 <b>Live mode</b> — '
            'showing current theme scores from the latest pipeline run.</div>',
            unsafe_allow_html=True,
        )
        themes = load_themes(t2_strength, country=COUNTRY_CODE)
        date_label = "Current"
    else:
        st.markdown(
            f'<div style="background:#052e16;border-left:3px solid #22c55e;'
            f'border-radius:6px;padding:7px 14px;margin-bottom:10px;'
            f'color:#86efac;font-size:0.82rem">🕰️ <b>Historical view</b> — '
            f'showing theme scores as of <b>{t2_to}</b> '
            f'(window: {t2_from} → {t2_to}).</div>',
            unsafe_allow_html=True,
        )
        themes = load_themes_as_of(t2_to, t2_from, t2_strength, country=COUNTRY_CODE)
        date_label = str(t2_to)

    if t2_conviction:
        themes = [t for t in themes if t.get("conviction") in t2_conviction]

    if not themes:
        st.info(
            "No themes found for the selected date range / filters. "
            "Try widening the date window or lowering the min score."
        )
    else:
        col_list, col_detail = st.columns([1, 2], gap="large")

        # ── LEFT: theme list ──────────────────────────────────────────────
        with col_list:
            st.markdown(
                f'<div style="color:#94a3b8;font-size:0.8rem;margin-bottom:8px">'
                f'{len(themes)} theme{"s" if len(themes)!=1 else ""} '
                f'as of <b style="color:#818cf8">{date_label}</b></div>',
                unsafe_allow_html=True,
            )

            selected_slug = st.session_state.get("selected_theme_slug",
                                                  themes[0]["theme_slug"])
            # Ensure selection is valid for current filter
            if not any(t["theme_slug"] == selected_slug for t in themes):
                selected_slug = themes[0]["theme_slug"]
                st.session_state["selected_theme_slug"] = selected_slug

            for t in themes:
                slug = t["theme_slug"]
                conv = t.get("conviction", "emerging")
                # Use snapshot score when in historical mode
                score = t.get("snap_strength", t.get("strength_score", 0)) \
                    if not use_live else t.get("strength_score", 0)
                icon = CONVICTION_ICON.get(conv, "🔮")
                color = CONVICTION_COLOR.get(conv, "#6366f1")
                is_sel = slug == selected_slug
                border = "#818cf8" if is_sel else "#334155"
                bg = "#1e3a5f" if is_sel else "#1e293b"

                snap_date = t.get("snap_date", "")
                snap_note = (
                    f'<span style="font-size:0.68rem;color:#475569">snap: {str(snap_date)[:10]}</span>'
                    if snap_date and not use_live else ""
                )

                # Stage for list card
                from src.makrograph.themes.theme_stage import stage_from_theme_dict, STAGE_ICONS, STAGE_COLORS
                _list_ts   = stage_from_theme_dict(t)
                _list_stg  = _list_ts.stage
                _list_icon = _list_ts.icon
                _list_col  = _list_ts.color
                _list_lbl  = _list_ts.label

                st.markdown(
                    f'<div style="background:{bg};border:1.5px solid {border};'
                    f'border-radius:10px;padding:10px 14px;margin-bottom:4px">'
                    # Stage pill — top right
                    f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
                    f'<div style="font-weight:700;color:#ffffff;font-size:0.88rem;flex:1">'
                    f'{icon} {t["theme_name"]}</div>'
                    f'<span style="background:{_list_col}22;color:{_list_col};'
                    f'padding:1px 7px;border-radius:8px;font-size:0.66rem;font-weight:700;'
                    f'border:1px solid {_list_col}44;white-space:nowrap;margin-left:6px">'
                    f'{_list_icon} S{_list_stg}</span>'
                    f'</div>'
                    f'<div style="display:flex;gap:8px;margin-top:5px;align-items:center;flex-wrap:wrap">'
                    f'<span style="font-size:0.72rem;color:#94a3b8">Score: '
                    f'<b style="color:#818cf8">{score:.0f}</b></span>'
                    f'{_badge(conv, color)}{snap_note}'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )
                if st.button("Select", key=f"sel_{slug}_{date_label}", type="secondary"):
                    st.session_state["selected_theme_slug"] = slug
                    selected_slug = slug
                    st.rerun()

        # ── RIGHT: theme detail ───────────────────────────────────────────
        with col_detail:
            theme = next(
                (t for t in themes if t["theme_slug"] == selected_slug), themes[0]
            )
            selected_slug = theme["theme_slug"]

            conv = theme.get("conviction", "emerging")
            color = CONVICTION_COLOR.get(conv, "#6366f1")

            # Scores: prefer snapshot in historical mode
            disp_strength  = theme.get("snap_strength",  theme.get("strength_score",  0)) if not use_live else theme.get("strength_score",  0)
            disp_momentum  = theme.get("snap_momentum",  theme.get("momentum_score",  0)) if not use_live else theme.get("momentum_score",  0)
            disp_docs      = theme.get("snap_doc_count", theme.get("doc_count",       0)) if not use_live else theme.get("doc_count",       0)
            disp_companies = theme.get("snap_company_count", theme.get("company_count", 0)) if not use_live else theme.get("company_count", 0)

            # Constraint severity label (Low / Medium / High / Critical)
            def _severity_label(s: float) -> tuple[str, str]:
                if s >= 80: return "CRITICAL", "#ef4444"
                if s >= 60: return "HIGH",     "#f97316"
                if s >= 30: return "MEDIUM",   "#f59e0b"
                return "LOW", "#64748b"
            sev_label, sev_color = _severity_label(disp_strength)

            # Delta vs current (only meaningful in historical mode)
            curr_strength = theme.get("strength_score", 0)
            delta_str = ""
            if not use_live and curr_strength and disp_strength:
                diff = curr_strength - disp_strength
                delta_color = "#22c55e" if diff >= 0 else "#ef4444"
                delta_str = (
                    f'<span style="font-size:0.72rem;color:{delta_color};margin-left:6px">'
                    f'{"▲" if diff >= 0 else "▼"} {abs(diff):.1f} vs now</span>'
                )

            # ── Earnings impact & supply-demand tension from metadata ──
            meta = theme.get("metadata") or {}
            if isinstance(meta, str):
                import json as _json
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}

            earnings_impact = meta.get("earnings_impact", "")
            tension_val = meta.get("tension_score", 0)
            demand_ct = meta.get("demand_count", "?")
            supply_ct = meta.get("supply_constraint_count", "?")
            supply_thesis = meta.get("supply_constraint", "")
            key_bens = meta.get("key_beneficiaries", [])
            ben_sectors = meta.get("beneficiary_sectors", [])
            quarter_count = meta.get("quarter_count", None)
            in_causal_chain = meta.get("in_causal_chain", False)
            persistence_mult = meta.get("persistence_multiplier", None)
            confirmed_quarters = meta.get("confirmed_quarters", None)
            # Downstream constraint / causal plausibility
            driven_by = meta.get("driven_by", "")
            edge_type = meta.get("edge_type", "")
            edge_weight = meta.get("edge_weight", None)
            economic_score = meta.get("economic_score", None)
            is_downstream = meta.get("theme_type") == "downstream_constraint"
            # Bottleneck / constraint keyword
            is_bottleneck = meta.get("is_bottleneck", False) or meta.get("theme_type") == "bottleneck"
            constraint_kw_count = meta.get("constraint_kw_count", 0)
            wt_constraint = meta.get("weighted_constraint_score", None)
            capex_lag = meta.get("capex_lag", None)
            # Beneficiary validation
            ben_warning = meta.get("beneficiary_warning", False)
            strong_ben_count = meta.get("strong_beneficiary_count", None)

            # Earnings impact badge color
            EI_COLORS = {
                "5x+": ("#22c55e", "#052e16"),
                "3-5x": ("#f59e0b", "#451a03"),
                "2-3x": ("#60a5fa", "#172554"),
                "moderate": ("#94a3b8", "#1e293b"),
            }
            ei_fg, ei_bg = EI_COLORS.get(earnings_impact, ("#94a3b8", "#1e293b"))

            # ── Stage badge ──────────────────────────────────────────────
            from src.makrograph.themes.theme_stage import (
                stage_from_theme_dict, STAGE_ICONS, STAGE_COLORS,
                STAGE_LABELS, STAGE_RETURN_POTENTIAL, STAGE_DESCRIPTIONS,
            )
            _ts      = stage_from_theme_dict(theme)
            stg_n    = _ts.stage
            stg_icon = _ts.icon
            stg_lbl  = _ts.label
            stg_col  = _ts.color
            stg_ret  = _ts.return_potential
            stg_desc = _ts.description
            stg_ev   = _ts.evidence

            # ── Theme header card ────────────────────────────────────────
            st.markdown(
                f'<div class="theme-card">'

                # Stage bar — full-width highlight at top of card
                f'<div style="background:{stg_col}18;border-left:4px solid {stg_col};'
                f'padding:6px 12px;margin-bottom:10px;border-radius:4px;'
                f'display:flex;align-items:center;justify-content:space-between">'
                f'<span style="color:{stg_col};font-weight:700;font-size:0.82rem">'
                f'{stg_icon} Stage {stg_n} · {stg_lbl}</span>'
                f'<span style="color:{stg_col};font-size:0.72rem;opacity:0.85">'
                f'Return potential: {stg_ret}</span>'
                f'</div>'

                f'<div class="theme-name">{theme["theme_name"]}</div>'
                f'<div class="theme-slug">{theme["theme_slug"]}</div>'
                f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">'
                f'{_badge(conv.upper(), color)}'
                f'<span style="background:{sev_color}22;color:{sev_color};padding:2px 10px;'
                f'border:1px solid {sev_color}44;border-radius:8px;font-size:0.72rem;font-weight:700">'
                f'⚠️ {sev_label}</span>'
                + (
                    f'<span style="background:{ei_bg};color:{ei_fg};padding:2px 10px;'
                    f'border:1px solid {ei_fg}66;border-radius:8px;font-size:0.72rem;font-weight:700">'
                    f'🚀 {earnings_impact} Earnings Impact</span>'
                    if earnings_impact else ""
                )
                + (
                    f'<span style="background:#052e16;color:#86efac;padding:2px 10px;'
                    f'border-radius:8px;font-size:0.72rem">📅 as of {date_label}</span>'
                    if not use_live else ""
                )
                + (
                    f'<span style="background:#1e2a3e;color:#a5b4fc;padding:2px 10px;'
                    f'border:1px solid #4338ca44;border-radius:8px;font-size:0.72rem">'
                    f'📆 {quarter_count}Q span</span>'
                    if quarter_count and quarter_count >= 2 else (
                    f'<span style="background:#292524;color:#78716c;padding:2px 10px;'
                    f'border:1px solid #44403c44;border-radius:8px;font-size:0.72rem">'
                    f'📆 1Q only</span>'
                    if quarter_count == 1 else ""
                ))
                + (
                    f'<span style="background:#052e16;color:#4ade80;padding:2px 10px;'
                    f'border:1px solid #15803d55;border-radius:8px;font-size:0.72rem;font-weight:700">'
                    f'⛓️ Causal Chain</span>'
                    if in_causal_chain else ""
                )
                + (
                    f'<span style="background:#1e2a3e;color:#c4b5fd;padding:2px 10px;'
                    f'border:1px solid #7c3aed44;border-radius:8px;font-size:0.72rem;font-weight:700">'
                    f'📈 ×{persistence_mult:.2f} Persistence</span>'
                    if persistence_mult and persistence_mult > 1.0 else ""
                )
                + (
                    f'<span style="background:#3b0000;color:#f87171;padding:2px 10px;'
                    f'border:1px solid #dc262655;border-radius:8px;font-size:0.72rem;font-weight:700">'
                    f'🚨 Bottleneck ({constraint_kw_count} constraint signals)</span>'
                    if is_bottleneck and constraint_kw_count else ""
                )
                + (
                    f'<span style="background:#292524;color:#f59e0b;padding:2px 10px;'
                    f'border:1px solid #92400e55;border-radius:8px;font-size:0.72rem">'
                    f'⚠️ Few Beneficiaries ({strong_ben_count})</span>'
                    if ben_warning and strong_ben_count is not None else ""
                )
                + '</div>'

                # ── Supply-Demand Tension row ─────────────────────────────
                + f'<div style="display:flex;gap:16px;margin-bottom:10px;align-items:center">'
                f'<div style="font-size:0.72rem;color:#94a3b8">'
                f'<b style="color:#ef4444">Demand:</b> {demand_ct} signals &nbsp;'
                f'<b style="color:#f59e0b">Supply Constraint:</b> {supply_ct} signals &nbsp;'
                f'<b style="color:#818cf8">Tension:</b> {tension_val:.0f}/60'
                + (
                    f' &nbsp;<b style="color:#4ade80">Capex Lag:</b> '
                    f'<span style="color:{"#4ade80" if (capex_lag or 0) > 0 else "#ef4444"}">'
                    f'{capex_lag:+d}</span>'
                    if capex_lag is not None else ""
                )
                + (
                    f' &nbsp;<b style="color:#f87171">Constraint KW:</b> {constraint_kw_count}'
                    if constraint_kw_count else ""
                )
                + f'</div></div>'

                # ── Sectors row ───────────────────────────────────────────
                + '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px">'
                + "".join(
                    f'<span style="background:#172554;color:#93c5fd;padding:2px 8px;'
                    f'border-radius:8px;font-size:0.72rem">{s}</span>'
                    for s in (theme.get("sectors") or [])[:6]
                )
                + '</div>'

                # ── Beneficiary sectors (from thesis) ─────────────────────
                + (
                    '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px">'
                    '<span style="font-size:0.68rem;color:#94a3b8;margin-right:4px">Beneficiary Sectors:</span>'
                    + "".join(
                        f'<span style="background:#052e16;color:#86efac;padding:2px 8px;'
                        f'border-radius:8px;font-size:0.70rem">{s}</span>'
                        for s in ben_sectors[:6]
                    )
                    + '</div>'
                    if ben_sectors else ""
                )

                # ── Key beneficiary tickers ───────────────────────────────
                + (
                    '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px">'
                    '<span style="font-size:0.68rem;color:#94a3b8;margin-right:4px">Key Beneficiaries:</span>'
                    + "".join(
                        f'<span style="background:#2e1065;color:#c4b5fd;padding:2px 8px;'
                        f'border:1px solid #6d28d9;border-radius:8px;font-size:0.72rem;font-weight:700">{t}</span>'
                        for t in key_bens[:8]
                    )
                    + '</div>'
                    if key_bens else ""
                )

                # ── Metrics row ───────────────────────────────────────────
                + f'<div class="metric-row">'
                f'<div class="metric-item"><span class="metric-label">Strength</span>'
                f'<span class="metric-value" style="color:#4f46e5">{disp_strength:.1f}</span>'
                f'{delta_str}</div>'
                f'<div class="metric-item"><span class="metric-label">Momentum</span>'
                f'<span class="metric-value" style="color:#f59e0b">{disp_momentum:.1f}</span></div>'
                f'<div class="metric-item"><span class="metric-label">Docs</span>'
                f'<span class="metric-value">{disp_docs}</span></div>'
                f'<div class="metric-item"><span class="metric-label">Companies</span>'
                f'<span class="metric-value">{disp_companies}</span></div>'
                + (
                    f'<div class="metric-item"><span class="metric-label">Quarters</span>'
                    f'<span class="metric-value" style="color:{"#818cf8" if (confirmed_quarters or quarter_count or 0) >= 2 else "#78716c"}">'
                    f'{confirmed_quarters or quarter_count}Q</span></div>'
                    if (confirmed_quarters or quarter_count) else ""
                )
                + f'<div class="metric-item"><span class="metric-label">First Detected</span>'
                f'<span class="metric-value" style="font-size:0.9rem">'
                f'{str(theme.get("first_detected","—"))}</span></div>'
                + f'</div>'

                # ── Supply constraint thesis ──────────────────────────────
                + (
                    f'<div style="background:#1e2a3e;border-left:3px solid #f59e0b;'
                    f'padding:8px 12px;border-radius:4px;margin-top:8px;font-size:0.74rem;'
                    f'color:#d4d4d8;line-height:1.4">'
                    f'<b style="color:#f59e0b">⚡ Supply Constraint:</b> {supply_thesis}'
                    f'</div>'
                    if supply_thesis else ""
                )

                # ── Downstream / causal plausibility strip ────────────────
                + (
                    f'<div style="background:#052e16;border-left:3px solid #22c55e;'
                    f'padding:7px 12px;border-radius:4px;margin-top:8px;font-size:0.72rem;'
                    f'color:#bbf7d0;line-height:1.5">'
                    f'<b style="color:#4ade80">💎 Picks-and-Shovels:</b> '
                    f'Demand driver: <b>{driven_by}</b> &nbsp;|&nbsp; '
                    f'Edge: <b>{edge_type.replace("_"," ").title()}</b> '
                    f'(weight={edge_weight:.2f}) &nbsp;|&nbsp; '
                    f'Economic score: <b style="color:{"#4ade80" if (economic_score or 0) >= 0.85 else "#f59e0b"}">'
                    f'{economic_score:.2f}</b>'
                    f'</div>'
                    if is_downstream and driven_by and edge_weight is not None else ""
                )

                # ── Stage evidence ────────────────────────────────────────
                + f'<div style="background:{stg_col}12;border:1px solid {stg_col}30;'
                f'padding:8px 12px;border-radius:6px;margin-top:10px;font-size:0.73rem;'
                f'color:#cbd5e1;line-height:1.5">'
                f'<b style="color:{stg_col}">{stg_icon} Why Stage {stg_n}:</b> {stg_ev}'
                f'<div style="margin-top:4px;color:#94a3b8;font-size:0.70rem">'
                f'<i>{stg_desc}</i></div>'
                f'</div>'

                + f'</div>',
                unsafe_allow_html=True,
            )

            # ── Strength / Momentum chart ────────────────────────────────
            import pandas as pd
            import plotly.graph_objects as go

            all_snaps = load_snapshots(theme["id"])
            if all_snaps:
                df_snap = pd.DataFrame(all_snaps)
                df_snap["snapshot_date"] = pd.to_datetime(df_snap["snapshot_date"])
                df_window = df_snap[
                    (df_snap["snapshot_date"] >= pd.Timestamp(t2_from))
                    & (df_snap["snapshot_date"] <= pd.Timestamp(t2_to))
                ] if not use_live else df_snap

                fig = go.Figure()
                if not df_window.empty:
                    fig.add_trace(go.Scatter(
                        x=df_window["snapshot_date"], y=df_window["strength_score"],
                        name="Strength", line=dict(color="#818cf8", width=2),
                        fill="tozeroy", fillcolor="rgba(99,102,241,0.12)",
                    ))
                    if "momentum_score" in df_window.columns:
                        fig.add_trace(go.Scatter(
                            x=df_window["snapshot_date"], y=df_window["momentum_score"],
                            name="Momentum", line=dict(color="#f59e0b", width=1.5, dash="dot"),
                        ))
                    if not use_live:
                        vline_x = str(t2_to)
                        fig.add_shape(
                            type="line", x0=vline_x, x1=vline_x,
                            y0=0, y1=1, yref="paper",
                            line=dict(color="#16a34a", width=1.5, dash="dash"),
                        )
                        fig.add_annotation(
                            x=vline_x, y=1, yref="paper", text=f"as-of {t2_to}",
                            showarrow=False, xanchor="left",
                            font=dict(color="#15803d", size=10),
                        )
                else:
                    fig.add_trace(go.Scatter(
                        x=df_snap["snapshot_date"], y=df_snap["strength_score"],
                        name="Strength (outside window)",
                        line=dict(color="#64748b", width=1.5),
                        fill="tozeroy", fillcolor="rgba(148,163,184,0.08)",
                    ))
                fig.update_layout(
                    height=190, margin=dict(l=0, r=0, t=4, b=0),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#172033",
                    legend=dict(font=dict(color="#94a3b8", size=11),
                                orientation="h", yanchor="bottom", y=1, x=0),
                    xaxis=dict(color="#94a3b8", gridcolor="#1e1e1e", linecolor="#2a2a2a"),
                    yaxis=dict(color="#94a3b8", gridcolor="#1e1e1e", linecolor="#2a2a2a"),
                    font=dict(color="#94a3b8"),
                )
                st.plotly_chart(fig, use_container_width=True,
                                config={"displayModeBar": False, "responsive": True})

            # ── Quarterly persistence badges ─────────────────────────────
            @st.cache_data(ttl=120, show_spinner=False)
            def load_quarterly_persistence(tid: int, as_of_d: str):
                if not pg:
                    return []
                try:
                    return pg.get_quarterly_persistence(tid, as_of_d)
                except Exception:
                    return []

            quarters = load_quarterly_persistence(theme["id"], str(t2_to))
            if quarters:
                # Group by year
                years: dict[int, list] = {}
                for q in quarters:
                    years.setdefault(q["year"], []).append(q)

                q_html = '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">'
                for year in sorted(years):
                    for qr in years[year]:
                        qn = qr["quarter"]
                        confirmed = qr.get("confirmed", False)
                        bg = "#14532d" if confirmed else "#292524"
                        tc = "#86efac" if confirmed else "#78716c"
                        bc = "#22c55e" if confirmed else "#57534e"
                        check = "✓" if confirmed else "·"
                        strength = qr.get("max_strength", 0)
                        q_html += (
                            f'<div style="background:{bg};border:1px solid {bc};'
                            f'border-radius:6px;padding:4px 10px;font-size:0.73rem;color:{tc}">'
                            f'<b>{check} Q{qn}-{year}</b>'
                            f'<div style="font-size:0.66rem;color:{tc}88">{strength:.0f}pts</div>'
                            f'</div>'
                        )
                q_html += '</div>'
                # Count confirmed quarters for persistence check
                confirmed_count = sum(1 for q in quarters if q.get("confirmed"))
                persistence_note = ""
                if confirmed_count >= 3:
                    persistence_note = (
                        f'<div style="color:#86efac;font-size:0.75rem;margin-bottom:8px">'
                        f'✅ Time-persistent: confirmed across {confirmed_count} quarters</div>'
                    )
                elif confirmed_count >= 1:
                    persistence_note = (
                        f'<div style="color:#f59e0b;font-size:0.75rem;margin-bottom:8px">'
                        f'⏳ {confirmed_count}/3 quarters confirmed — watch for persistence</div>'
                    )
                st.markdown(
                    f'<div style="margin-bottom:2px;font-size:0.78rem;color:#94a3b8;'
                    f'font-weight:600">📅 Quarterly Persistence</div>'
                    + persistence_note + q_html,
                    unsafe_allow_html=True,
                )

            # ── Detail sub-tabs ──────────────────────────────────────────
            dtab_ben, dtab_source, dtab_evidence, dtab_macro = st.tabs([
                "🏢 Beneficiaries",
                "📄 Source Companies",
                "💬 Evidence",
                "🌐 Macro Context",
            ])

            # ── BENEFICIARIES tab ────────────────────────────────────────
            with dtab_ben:
                bens = (
                    load_beneficiaries(theme["id"])
                    if use_live
                    else load_beneficiaries_as_of(theme["id"], t2_to)
                )
                ROLE_ICONS = {
                    "infrastructure_provider": "🏗️",
                    "supplier": "🔧",
                    "bottleneck_player": "⚡",
                    "beneficiary": "💚",
                    "downstream_user": "📥",
                    "hidden_enabler": "🔦",
                }
                BEN_CHAIN_ORDER = [
                    "infrastructure_provider", "supplier", "bottleneck_player",
                    "beneficiary", "downstream_user", "hidden_enabler",
                ]
                if bens:
                    # Show as chain first (visual)
                    st.markdown(
                        '<div style="font-size:0.77rem;color:#94a3b8;margin-bottom:6px">'
                        '📡 Supply / Beneficiary Chain (role order)</div>',
                        unsafe_allow_html=True,
                    )
                    chain_sorted = sorted(
                        bens,
                        key=lambda b: BEN_CHAIN_ORDER.index(b.get("company_role","beneficiary"))
                        if b.get("company_role") in BEN_CHAIN_ORDER else 99
                    )
                    # Build horizontal chain bar
                    chain_html = '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px;align-items:center">'
                    prev_role = None
                    for b in chain_sorted[:20]:
                        role = b.get("company_role") or b.get("beneficiary_type","beneficiary")
                        if prev_role and role != prev_role:
                            chain_html += '<span style="color:#475569;font-size:1rem">→</span>'
                        icon = ROLE_ICONS.get(role, "⚪")
                        ticker = b.get("ticker") or b.get("company_name","?")[:8]
                        rel = int(b.get("relevance_score",0) or 0)
                        rel_bar = "█" * (rel // 20) + "░" * (5 - rel // 20)
                        chain_html += (
                            f'<div style="background:#1e3a5f;border:1px solid #2d4a6f;'
                            f'border-radius:6px;padding:4px 8px;font-size:0.72rem;text-align:center;'
                            f'min-width:60px">'
                            f'<div style="color:#ffffff;font-weight:700">{icon} {ticker}</div>'
                            f'<div style="color:#64748b;font-size:0.62rem">'
                            f'{role.replace("_"," ")[:14]}</div>'
                            f'<div style="color:#818cf8;font-size:0.60rem">{rel_bar} {rel}</div>'
                            f'</div>'
                        )
                        prev_role = role
                    chain_html += '</div>'
                    st.markdown(chain_html, unsafe_allow_html=True)

                    # Full table below the chain
                    def _fmt_row(row):
                        btype = row.get("beneficiary_type", "direct")
                        role = row.get("company_role") or btype
                        role_icon = ROLE_ICONS.get(role, "⚪")
                        role_label = f"{role_icon} {role.replace('_', ' ').title()}"
                        capex = int(row.get("capex_signals", 0) or 0)
                        qm = row.get("quarterly_mentions") or {}
                        qm_str = ", ".join(sorted(qm.keys())[-3:]) if qm else "—"
                        return {
                            "Role":       role_label,
                            "Ticker":     row.get("ticker") or "—",
                            "Company":    row.get("company_name", ""),
                            "Relevance":  int(row.get("relevance_score", 0) or 0),
                            "Signals":    int(row.get("signal_count", 0) or 0),
                            "Capex ⚡":   capex,
                            "Active Qtrs": qm_str,
                            "First Seen": str(row.get("first_seen_at", "") or "")[:10],
                            "Reasoning":  (row.get("reasoning") or "")[:160],
                        }
                    rows = [_fmt_row(b) for b in bens]
                    df_bens = pd.DataFrame(rows)
                    st.dataframe(
                        df_bens,
                        use_container_width=True,
                        hide_index=True,
                        height=min(50 + len(rows) * 38, 460),
                        column_config={
                            "Relevance": st.column_config.ProgressColumn(
                                "Relevance", min_value=0, max_value=100, format="%d",
                            ),
                            "Capex ⚡": st.column_config.NumberColumn(
                                "Capex ⚡", help="Number of capital expenditure signals"),
                        },
                    )
                else:
                    st.caption(
                        f'No beneficiaries mapped{"  as of " + str(t2_to) if not use_live else ""}. '
                        "Run the intelligence pipeline to populate."
                    )

            # ── SOURCE COMPANIES tab ─────────────────────────────────────
            with dtab_source:
                st.markdown(
                    '<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px">'
                    'Companies whose SEC filings generated the signals that <b>identified</b> this theme. '
                    'These are the companies talking about it — not necessarily the beneficiaries.</div>',
                    unsafe_allow_html=True,
                )

                @st.cache_data(ttl=120, show_spinner=False)
                def load_source_companies(slug: str, as_of_d: str, since_d: str):
                    if not pg:
                        return []
                    try:
                        return pg.get_source_companies_for_theme(
                            slug, as_of_d, since_date=since_d
                        )
                    except Exception:
                        return []

                src_companies = load_source_companies(
                    selected_slug, str(t2_to), str(t2_from)
                )
                if src_companies:
                    def _fmt_src(r):
                        filing_types = r.get("filing_types") or []
                        return {
                            "Ticker":       r.get("ticker") or "—",
                            "Company":      r.get("company", ""),
                            "# Filings":    int(r.get("doc_count", 0) or 0),
                            "Signals":      int(r.get("signal_count", 0) or 0),
                            "Avg Confid.":  float(r.get("avg_confidence", 0) or 0),
                            "First Mention":str(r.get("first_mention", "") or "")[:10],
                            "Last Mention": str(r.get("last_mention", "") or "")[:10],
                            "Filing Types": ", ".join(filing_types[:3]),
                        }
                    df_src = pd.DataFrame([_fmt_src(r) for r in src_companies])
                    st.dataframe(
                        df_src,
                        use_container_width=True,
                        hide_index=True,
                        height=min(50 + len(df_src) * 38, 460),
                        column_config={
                            "Signals": st.column_config.ProgressColumn(
                                "Signals", min_value=0,
                                max_value=max(int(r.get("signal_count") or 1) for r in src_companies),
                                format="%d",
                            ),
                            "Avg Confid.": st.column_config.NumberColumn(
                                "Avg Confid.", format="%.2f",
                                help="Average NLP signal confidence (0–1)",
                            ),
                        },
                    )
                    st.caption(
                        f"{len(src_companies)} source companies in window "
                        f"{t2_from} → {t2_to}"
                    )
                else:
                    st.info(
                        "No source companies found. This may mean the theme was detected via "
                        "graph/supply-chain inference rather than direct signal clustering, "
                        "or no documents are in the selected window."
                    )

            # ── EVIDENCE tab ─────────────────────────────────────────────
            with dtab_evidence:
                st.markdown(
                    '<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px">'
                    'Raw text excerpts from SEC filings — exactly what companies said '
                    'that triggered the signals behind this theme.</div>',
                    unsafe_allow_html=True,
                )

                @st.cache_data(ttl=120, show_spinner=False)
                def load_evidence(slug: str, as_of_d: str, since_d: str):
                    if not pg:
                        return []
                    try:
                        return pg.get_signal_evidence_for_theme(
                            slug, as_of_d, since_date=since_d
                        )
                    except Exception:
                        return []

                evidence = load_evidence(selected_slug, str(t2_to), str(t2_from))
                DIR_COLOR = {
                    "increasing": "#22c55e", "positive": "#22c55e",
                    "decreasing": "#ef4444", "negative": "#ef4444",
                    "neutral": "#94a3b8", "stable": "#94a3b8",
                }
                if evidence:
                    for ev in evidence[:25]:
                        dir_col = DIR_COLOR.get(ev.get("direction","neutral"), "#94a3b8")
                        conf = float(ev.get("confidence", 0) or 0)
                        st.markdown(
                            f'<div style="background:#172033;border:1px solid #2d3f57;'
                            f'border-left:3px solid {dir_col};border-radius:7px;'
                            f'padding:9px 13px;margin-bottom:6px">'
                            f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px">'
                            f'<span style="font-weight:700;color:#cbd5e1;font-size:0.80rem">'
                            f'{ev.get("company","?")}'
                            f'{"  (" + ev.get("ticker","") + ")" if ev.get("ticker") else ""}'
                            f'</span>'
                            f'<span style="font-size:0.70rem;color:#475569">'
                            f'{ev.get("filing_type","?")} · {str(ev.get("filed_at",""))[:10]} · '
                            f'conf: <span style="color:#818cf8">{conf:.2f}</span></span>'
                            f'</div>'
                            f'<div style="margin-top:5px;font-size:0.78rem;color:#e2e8f0;'
                            f'font-style:italic;line-height:1.45">'
                            f'"{(ev.get("context_text") or "")[:400]}"'
                            f'</div>'
                            f'<div style="margin-top:4px;font-size:0.68rem;color:#475569">'
                            f'Signal: {ev.get("signal_type","?")} · '
                            f'<span style="color:{dir_col}">{ev.get("direction","")}</span>'
                            f'</div></div>',
                            unsafe_allow_html=True,
                        )
                else:
                    st.info(
                        "No evidence snippets found for this window. "
                        "Signals require context_text to be populated during NLP extraction."
                    )

            # ── MACRO CONTEXT tab ────────────────────────────────────────
            with dtab_macro:
                st.markdown(
                    '<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px">'
                    'Macro/policy signals that <b>corroborate</b> (tailwinds) or '
                    '<b>constrain</b> (headwinds) this theme.</div>',
                    unsafe_allow_html=True,
                )

                @st.cache_data(ttl=120, show_spinner=False)
                def load_theme_macro(slug: str, as_of_d: str):
                    if not pg:
                        return []
                    try:
                        return pg.get_theme_macro_context(slug, as_of_d)
                    except Exception:
                        return []

                macro_ctx = load_theme_macro(selected_slug, str(t2_to))
                LINK_COLOR = {
                    "corroborates": "#22c55e", "amplifies": "#16a34a",
                    "constrains": "#ef4444",   "reduces": "#f97316",
                }
                LINK_ICON = {
                    "corroborates": "✅", "amplifies": "🚀",
                    "constrains": "⚠️",  "reduces": "🔻",
                }
                if macro_ctx:
                    total_tailwind = sum(
                        m["strength"] for m in macro_ctx
                        if m.get("link_type") in ("corroborates","amplifies")
                    )
                    total_headwind = sum(
                        m["strength"] for m in macro_ctx
                        if m.get("link_type") in ("constrains","reduces")
                    )
                    net = total_tailwind - total_headwind
                    net_col = "#22c55e" if net > 0 else ("#ef4444" if net < 0 else "#94a3b8")
                    st.markdown(
                        f'<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
                        f'padding:10px 14px;margin-bottom:10px;display:flex;gap:20px">'
                        f'<div style="text-align:center"><div style="color:#86efac;font-size:1.1rem;font-weight:800">'
                        f'+{total_tailwind:.0f}</div><div style="color:#64748b;font-size:0.70rem">tailwinds</div></div>'
                        f'<div style="text-align:center"><div style="color:#fca5a5;font-size:1.1rem;font-weight:800">'
                        f'-{total_headwind:.0f}</div><div style="color:#64748b;font-size:0.70rem">headwinds</div></div>'
                        f'<div style="text-align:center"><div style="color:{net_col};font-size:1.1rem;font-weight:800">'
                        f'{net:+.0f}</div><div style="color:#64748b;font-size:0.70rem">net macro score</div></div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    for ml in macro_ctx:
                        lt = ml.get("link_type","corroborates")
                        lc = LINK_COLOR.get(lt, "#94a3b8")
                        li = LINK_ICON.get(lt, "·")
                        ev_text = (
                            ml.get("macro_description")
                            or ml.get("policy_title")
                            or ml.get("evidence_text","")
                            or ""
                        )[:300]
                        series_lbl = ""
                        if ml.get("series_id"):
                            series_lbl = f'📈 {ml["series_id"]}'
                        elif ml.get("commodity_id"):
                            series_lbl = f'🛢️ {ml["commodity_id"]}'
                        st.markdown(
                            f'<div style="background:#172033;border:1px solid #2d3f57;'
                            f'border-left:3px solid {lc};border-radius:7px;'
                            f'padding:8px 13px;margin-bottom:5px">'
                            f'<div style="display:flex;justify-content:space-between">'
                            f'<span style="color:{lc};font-size:0.78rem;font-weight:700">'
                            f'{li} {lt.upper()}'
                            f'{"  ·  " + series_lbl if series_lbl else ""}</span>'
                            f'<span style="color:#475569;font-size:0.70rem">'
                            f'strength: {ml.get("strength",0):.0f} · '
                            f'{str(ml.get("as_of_date",""))[:10]}</span>'
                            f'</div>'
                            f'<div style="color:#cbd5e1;font-size:0.78rem;margin-top:4px">'
                            f'{ev_text}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                else:
                    st.info(
                        "No macro context yet. Fetch macro data in the 🌐 Macro & Policy tab, "
                        "then the Constraint Engine scores each theme automatically."
                    )

    # ── Causal Chains ─────────────────────────────────────────────────────────
    st.markdown("---")
    _cc_col1, _cc_col2 = st.columns([3, 1])
    with _cc_col1:
        st.markdown("#### ⛓️ Active Causal Chains")
    with _cc_col2:
        _cc_clear = st.button("Show all", key="cc_clear", help="Remove chain filter")

    # Auto-fill filter from selected theme; user can also type manually
    _selected_theme_name = ""
    if "selected_theme_slug" in st.session_state:
        _sel_slug = st.session_state["selected_theme_slug"]
        _matching = [t for t in (themes if "themes" in dir() else []) if t.get("theme_slug") == _sel_slug]
        if _matching:
            _selected_theme_name = _matching[0].get("theme_name", "")

    _chain_filter = st.text_input(
        "Filter chains by keyword (auto-filled from selected theme)",
        value="" if _cc_clear else _selected_theme_name,
        placeholder="e.g. AI, semiconductor, energy…",
        key="chain_keyword_filter",
        label_visibility="collapsed",
    )

    chains = load_causal_chains(COUNTRY_CODE)

    # Apply keyword filter
    if _chain_filter.strip():
        _kws = [k.strip().lower() for k in _chain_filter.replace(",", " ").split() if k.strip()]
        chains = [
            c for c in chains
            if any(kw in (c.get("chain_name", "") + " " + (c.get("terminal_effect") or "")).lower()
                   for kw in _kws)
        ]
        st.caption(f"Showing {len(chains)} chain(s) matching '{_chain_filter.strip()}'")

    if chains:
        for chain in chains:
            score = chain.get("activation_score", 0)
            score_color = "#22c55e" if score >= 80 else ("#f59e0b" if score >= 40 else "#6366f1")
            st.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;'
                f'padding:12px 16px;margin-bottom:8px;display:flex;align-items:center;gap:16px;'
                f'box-shadow:0 1px 3px rgba(0,0,0,0.4)">'
                f'<div style="min-width:54px;text-align:center;background:{score_color}22;'
                f'border-radius:8px;padding:6px 4px">'
                f'<div style="font-size:1.3rem;font-weight:800;color:{score_color}">{score:.0f}</div>'
                f'<div style="font-size:0.64rem;color:#94a3b8">SCORE</div></div>'
                f'<div>'
                f'<div style="font-weight:700;color:#ffffff;font-size:0.9rem">{chain["chain_name"]}</div>'
                f'<div style="color:#94a3b8;font-size:0.78rem;margin-top:3px">'
                f'Depth: {chain.get("depth","?")} hops  →  Terminal: '
                f'<span style="color:#93c5fd">{chain.get("terminal_effect","")}</span>  |  '
                f'Evidence: {str(chain.get("last_scored_at") or chain.get("first_detected") or "—")[:10]}</div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
    else:
        st.caption("No matching causal chains." if _chain_filter.strip() else "No active causal chains. Run the causal stage to populate.")

    # ── Contradiction Radar ────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🔄 Contradiction Radar")
    st.caption(
        "Detects when management narratives reverse between quarters "
        "(e.g. 'Demand strong' → 'Inventory correction'). "
        "Run the intelligence pipeline to populate."
    )

    @st.cache_data(ttl=60, show_spinner=False)
    def load_contradictions(country: str = "US"):
        if not pg:
            return []
        try:
            with pg._conn() as conn:
                from psycopg2.extras import RealDictCursor
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """SELECT company, theme, from_quarter, to_quarter,
                                  change_type, from_sentiment, to_sentiment,
                                  evidence, detected_at
                           FROM mg_contradictions
                           WHERE country = %s
                           ORDER BY detected_at DESC LIMIT 40""",
                        (country,),
                    )
                    return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    contradictions = load_contradictions(COUNTRY_CODE)
    CONTRA_TYPE_COLOR = {
        "demand_reversal": "#ef4444",
        "margin_reversal": "#f59e0b",
        "capex_reversal": "#6366f1",
        "positive_to_negative": "#ef4444",
        "negative_to_positive": "#22c55e",
        "general_reversal": "#94a3b8",
    }
    if contradictions:
        for c in contradictions[:10]:
            ctype = c.get("change_type", "general_reversal")
            color = CONTRA_TYPE_COLOR.get(ctype, "#94a3b8")
            delta = round((c.get("to_sentiment", 0) or 0) - (c.get("from_sentiment", 0) or 0), 2)
            delta_arrow = "▼" if delta < 0 else "▲"
            delta_color = "#ef4444" if delta < 0 else "#22c55e"
            evidence = c.get("evidence") or {}
            from_phrase = (evidence.get("from_phrases") or ["—"])[0]
            to_phrase = (evidence.get("to_phrases") or ["—"])[0]
            st.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;border-left:3px solid {color};'
                f'border-radius:8px;padding:10px 14px;margin-bottom:6px">'
                f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">'
                f'<span style="font-weight:700;color:#ffffff;font-size:0.88rem">'
                f'{c.get("company","?")} — {c.get("theme","?")}</span>'
                f'<span style="font-size:0.72rem;color:{color};background:{color}22;'
                f'padding:2px 8px;border-radius:8px">{ctype.replace("_"," ").title()}</span>'
                f'</div>'
                f'<div style="color:#94a3b8;font-size:0.75rem;margin-top:4px">'
                f'{c.get("from_quarter","?")} → {c.get("to_quarter","?")} &nbsp;|&nbsp; '
                f'Δ sentiment: <span style="color:{delta_color}">{delta_arrow} {abs(delta):.2f}</span></div>'
                f'<div style="margin-top:5px;font-size:0.78rem">'
                f'<span style="color:#64748b">Before:</span> '
                f'<span style="color:#93c5fd">"{from_phrase}"</span>'
                f' &nbsp;→&nbsp; '
                f'<span style="color:#64748b">After:</span> '
                f'<span style="color:#fca5a5">"{to_phrase}"</span>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
            'padding:12px 16px;color:#94a3b8;font-size:0.82rem">'
            '📭 No contradictions recorded yet. '
            'Run the intelligence pipeline (with earnings call documents) to detect narrative reversals.</div>',
            unsafe_allow_html=True,
        )

    # ── Macro Triggers ────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🌐 Macro Trigger Events")
    st.caption(
        "Government policies, budgets, tariffs, and infrastructure announcements "
        "that amplify or dampen investment themes."
    )

    @st.cache_data(ttl=120, show_spinner=False)
    def load_macro_events():
        try:
            from makrograph.intelligence.macro_trigger import MacroTriggerLayer
            mtl = MacroTriggerLayer(cfg)
            events = mtl.get_recent_events(days=365, limit=40)
            mtl.close()
            return events
        except Exception:
            return []

    macro_events = load_macro_events()
    MACRO_IMPACT_COLOR = {
        "positive": "#22c55e",
        "negative": "#ef4444",
        "neutral": "#94a3b8",
        "mixed": "#f59e0b",
    }
    MACRO_MAG_BADGE = {
        "low": "🟡",
        "medium": "🟠",
        "high": "🔴",
        "game_changer": "💥",
    }
    if macro_events:
        for ev in macro_events[:12]:
            direction = ev.get("impact_direction", "neutral")
            magnitude = ev.get("impact_magnitude", "medium")
            color = MACRO_IMPACT_COLOR.get(direction, "#94a3b8")
            mag_badge = MACRO_MAG_BADGE.get(magnitude, "🟠")
            themes_str = ", ".join(ev.get("themes") or []) or "—"
            st.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;border-left:3px solid {color};'
                f'border-radius:8px;padding:9px 14px;margin-bottom:5px">'
                f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">'
                f'<span style="font-weight:700;color:#ffffff;font-size:0.85rem">'
                f'{mag_badge} {ev.get("title","")}</span>'
                f'<span style="font-size:0.72rem;color:#94a3b8">{ev.get("event_date","")}</span>'
                f'</div>'
                f'<div style="color:#94a3b8;font-size:0.74rem;margin-top:3px">'
                f'Category: {ev.get("category","").replace("_"," ").title()} &nbsp;|&nbsp; '
                f'Impact: <span style="color:{color}">{direction}</span> / {magnitude} &nbsp;|&nbsp; '
                f'Themes: <span style="color:#818cf8">{themes_str}</span></div>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
            'padding:12px 16px;color:#94a3b8;font-size:0.82rem">'
            '📭 No macro events recorded. Use <code>MacroTriggerLayer.add_event()</code> '
            'to log government policies, budgets, and infrastructure announcements.</div>',
            unsafe_allow_html=True,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB — SHORTLISTED THEMES
# Themes that have persisted across 3+ quarters with growing strength
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@st.cache_data(ttl=60, show_spinner=False)
def load_shortlisted_themes(min_quarters: int = 3, country: str = "US") -> list[dict]:
    if not pg:
        return []
    try:
        return pg.get_shortlisted_themes(min_quarters=min_quarters, country=country)
    except Exception as _e:
        import traceback, logging as _logging
        _logging.getLogger(__name__).error(
            "get_shortlisted_themes failed: %s\n%s", _e, traceback.format_exc()
        )
        return []


with tab_shortlisted:
    import json as _json_sl
    import plotly.graph_objects as go_sl
    import pandas as pd_sl

    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:10px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} Showing shortlisted themes for <b>{COUNTRY_LABEL}</b> — '
        f'change market in the sidebar ←</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:8px 16px;margin-bottom:16px;font-size:0.82rem;color:#c7d2fe">'
        f'⭐ <b>Shortlisted Themes</b> — auto-discovered themes that have persisted across '
        f'multiple quarters with sustained or growing strength. No bias, no hardcoding — '
        f'100% signal-driven.</div>',
        unsafe_allow_html=True,
    )

    # ── Controls ──────────────────────────────────────────────────────────
    sl_c1, sl_c2, sl_c3 = st.columns([1, 1, 1])
    with sl_c1:
        sl_min_q = st.selectbox(
            "Min quarters present",
            options=[2, 3, 4],
            index=1,
            key="sl_min_q",
            help="Only show themes that appeared in at least this many distinct quarters",
        )
    with sl_c2:
        sl_trend = st.selectbox(
            "Trend filter",
            ["All (any direction)", "Growing only (strength ↑)", "Declining only (strength ↓)"],
            index=0,
            key="sl_trend",
        )
    with sl_c3:
        if st.button("🔄 Refresh", key="sl_refresh"):
            load_shortlisted_themes.clear()
            st.rerun()

    sl_themes = load_shortlisted_themes(min_quarters=sl_min_q, country=COUNTRY_CODE)

    # Apply trend filter
    if sl_trend == "Growing only (strength ↑)":
        sl_themes = [t for t in sl_themes if (t.get("strength_trend") or 0) >= 0]
    elif sl_trend == "Declining only (strength ↓)":
        sl_themes = [t for t in sl_themes if (t.get("strength_trend") or 0) < 0]

    if not sl_themes:
        st.info(
            f"No themes found with {sl_min_q}+ confirmed quarters yet. "
            "Run the pipeline across multiple months/quarters to build up snapshots."
        )
    else:
        st.markdown(
            f'<div style="color:#94a3b8;font-size:0.82rem;margin-bottom:12px">'
            f'<b style="color:#f1f5f9">{len(sl_themes)}</b> themes shortlisted '
            f'(≥{sl_min_q} quarters of sustained signal evidence)</div>',
            unsafe_allow_html=True,
        )

        for rank, theme in enumerate(sl_themes, 1):
            q_series_raw = theme.get("quarter_series") or []
            # parse if string
            if isinstance(q_series_raw, str):
                try:
                    q_series_raw = _json_sl.loads(q_series_raw)
                except Exception:
                    q_series_raw = []
            q_series: list[dict] = q_series_raw

            confirmed_q   = int(theme.get("confirmed_quarters") or 0)
            avg_strength  = float(theme.get("avg_strength") or 0)
            peak_strength = float(theme.get("peak_strength") or 0)
            strength_trend = float(theme.get("strength_trend") or 0)
            cur_strength  = float(theme.get("strength_score") or 0)
            momentum      = float(theme.get("momentum_score") or 0)
            conviction    = theme.get("conviction") or "emerging"
            name          = theme.get("theme_name", "")
            slug          = theme.get("theme_slug", "")
            companies     = int(theme.get("company_count") or 0)
            first_det     = str(theme.get("first_detected") or "")[:10]

            # Trend arrow and color
            if strength_trend > 5:
                trend_icon, trend_col = "▲", "#22c55e"
            elif strength_trend > 0:
                trend_icon, trend_col = "↗", "#86efac"
            elif strength_trend == 0:
                trend_icon, trend_col = "→", "#94a3b8"
            else:
                trend_icon, trend_col = "▼", "#ef4444"

            conv_color = CONVICTION_COLOR.get(conviction, "#6366f1")
            conv_icon  = CONVICTION_ICON.get(conviction, "🔮")

            # Quarter badge string
            q_badges = "".join(
                f'<span style="background:{"#14532d" if i < confirmed_q - 1 else "#1e3a5f"};'
                f'color:{"#86efac" if i < confirmed_q - 1 else "#93c5fd"};'
                f'border:1px solid {"#22c55e" if i < confirmed_q - 1 else "#3b82f6"};'
                f'border-radius:5px;padding:2px 7px;font-size:0.68rem;font-weight:700;margin-right:3px">'
                f'Q{q["quarter"]}-{q["year"]} <span style="opacity:0.7">{q["strength"]:.0f}</span>'
                f'</span>'
                for i, q in enumerate(q_series)
            )

            with st.container():
                st.markdown(
                    f'<div style="background:#1e293b;border:1.5px solid #334155;'
                    f'border-radius:12px;padding:14px 18px;margin-bottom:10px">'
                    # Header row
                    f'<div style="display:flex;justify-content:space-between;align-items:flex-start;'
                    f'margin-bottom:8px">'
                    f'<div>'
                    f'<span style="color:#94a3b8;font-size:0.72rem;font-weight:700;'
                    f'margin-right:8px">#{rank}</span>'
                    f'<span style="font-size:1rem;font-weight:700;color:#f1f5f9">{name}</span>'
                    f'</div>'
                    f'<div style="display:flex;gap:6px;align-items:center">'
                    f'{_badge(conviction.upper(), conv_color)}'
                    f'<span style="background:{trend_col}22;color:{trend_col};'
                    f'padding:2px 10px;border-radius:8px;font-size:0.78rem;font-weight:700;'
                    f'border:1px solid {trend_col}44">'
                    f'{trend_icon} {strength_trend:+.1f} trend</span>'
                    f'</div></div>'
                    # Quarter badges
                    f'<div style="margin-bottom:10px">{q_badges}</div>'
                    # Metrics row
                    f'<div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:8px">'
                    f'<div><span style="font-size:0.68rem;color:#94a3b8;display:block">CURRENT SCORE</span>'
                    f'<span style="font-size:1.2rem;font-weight:800;color:#818cf8">{cur_strength:.0f}</span></div>'
                    f'<div><span style="font-size:0.68rem;color:#94a3b8;display:block">AVG (all qtrs)</span>'
                    f'<span style="font-size:1.2rem;font-weight:700;color:#e2e8f0">{avg_strength:.0f}</span></div>'
                    f'<div><span style="font-size:0.68rem;color:#94a3b8;display:block">PEAK</span>'
                    f'<span style="font-size:1.2rem;font-weight:700;color:#f59e0b">{peak_strength:.0f}</span></div>'
                    f'<div><span style="font-size:0.68rem;color:#94a3b8;display:block">MOMENTUM</span>'
                    f'<span style="font-size:1.2rem;font-weight:700;'
                    f'color:{"#22c55e" if momentum >= 0 else "#ef4444"}">'
                    f'{momentum:+.1f}</span></div>'
                    f'<div><span style="font-size:0.68rem;color:#94a3b8;display:block">QUARTERS</span>'
                    f'<span style="font-size:1.2rem;font-weight:700;color:#c4b5fd">{confirmed_q}Q</span></div>'
                    f'<div><span style="font-size:0.68rem;color:#94a3b8;display:block">COMPANIES</span>'
                    f'<span style="font-size:1.2rem;font-weight:700;color:#e2e8f0">{companies}</span></div>'
                    + (f'<div><span style="font-size:0.68rem;color:#94a3b8;display:block">FIRST SEEN</span>'
                    f'<span style="font-size:0.85rem;font-weight:600;color:#64748b">{first_det}</span></div>'
                    if first_det else '')
                    + f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Sparkline — quarter strength over time
                if len(q_series) >= 2:
                    labels = [f"Q{q['quarter']}-{q['year']}" for q in q_series]
                    strengths = [float(q.get("strength") or 0) for q in q_series]
                    momenta   = [float(q.get("momentum") or 0) for q in q_series]

                    fig_sl = go_sl.Figure()
                    fig_sl.add_trace(go_sl.Scatter(
                        x=labels, y=strengths,
                        name="Strength", mode="lines+markers",
                        line=dict(color="#818cf8", width=2.5),
                        marker=dict(size=7),
                        fill="tozeroy", fillcolor="rgba(129,140,248,0.10)",
                    ))
                    if any(m != 0 for m in momenta):
                        fig_sl.add_trace(go_sl.Scatter(
                            x=labels, y=momenta,
                            name="Momentum", mode="lines",
                            line=dict(color="#f59e0b", width=1.5, dash="dot"),
                        ))
                    fig_sl.update_layout(
                        height=140,
                        margin=dict(l=0, r=0, t=4, b=0),
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="#172033",
                        legend=dict(font=dict(color="#94a3b8", size=10),
                                    orientation="h", y=1, x=1, xanchor="right"),
                        xaxis=dict(color="#64748b", gridcolor="#1e293b",
                                   tickfont=dict(size=10)),
                        yaxis=dict(color="#64748b", gridcolor="#1e293b",
                                   tickfont=dict(size=10)),
                        font=dict(color="#94a3b8"),
                    )
                    st.plotly_chart(fig_sl, use_container_width=True,
                                    config={"displayModeBar": False},
                                    key=f"sl_spark_{slug}")

                # Jump-to-detail button
                col_btn, _ = st.columns([1, 4])
                with col_btn:
                    if st.button("View details →", key=f"sl_jump_{slug}"):
                        st.session_state["selected_theme_slug"] = slug
                        st.rerun()

        # ── Summary table ──────────────────────────────────────────────────────
        with st.expander("📋 Shortlist summary table", expanded=False):
            tbl_rows = []
            for t in sl_themes:
                qs = t.get("quarter_series") or []
                if isinstance(qs, str):
                    try: qs = _json_sl.loads(qs)
                    except: qs = []
                tbl_rows.append({
                    "Theme":    t.get("theme_name",""),
                    "Quarters": int(t.get("confirmed_quarters") or 0),
                    "Avg Score": float(t.get("avg_strength") or 0),
                    "Current":   float(t.get("strength_score") or 0),
                    "Peak":      float(t.get("peak_strength") or 0),
                    "Trend":     float(t.get("strength_trend") or 0),
                    "Momentum":  float(t.get("momentum_score") or 0),
                    "Companies": int(t.get("company_count") or 0),
                    "Conviction": (t.get("conviction") or "emerging").title(),
                    "First Seen": str(t.get("first_detected") or "")[:10],
                })
            if tbl_rows:
                df_sl = pd_sl.DataFrame(tbl_rows)
                st.dataframe(
                    df_sl, use_container_width=True, hide_index=True,
                    column_config={
                        "Avg Score": st.column_config.NumberColumn("Avg Score", format="%.1f"),
                        "Current":   st.column_config.NumberColumn("Current",   format="%.1f"),
                        "Peak":      st.column_config.NumberColumn("Peak",      format="%.1f"),
                        "Trend":     st.column_config.NumberColumn("Trend",     format="%+.1f"),
                        "Momentum":  st.column_config.NumberColumn("Momentum",  format="%+.1f"),
                    },
                )

        # ── Gemini AI Analysis ────────────────────────────────────────────────
        st.markdown("---")
        _sl_gemini_key   = cfg.get("gemini", {}).get("api_key", "")
        _sl_gemini_model = cfg.get("gemini", {}).get("model", "gemini-flash-latest")

        st.markdown(
            '<div style="background:#0f172a;border-left:3px solid #a855f7;border-radius:8px;'
            'padding:8px 16px;margin-bottom:12px">'
            '<span style="font-size:0.92rem;font-weight:700;color:#f1f5f9">🤖 Gemini AI Analysis</span>'
            f'<span style="font-size:0.75rem;color:#64748b;margin-left:10px">'
            f'Google Gemini Flash · {COUNTRY_LABEL} · {len(sl_themes)} shortlisted themes</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        if not _sl_gemini_key:
            st.caption("💡 Set `gemini.api_key` in `config/settings.yaml` to enable Gemini analysis.")
        else:
            _sl_gcol1, _sl_gcol2 = st.columns([1, 5])
            with _sl_gcol1:
                _sl_run_gem = st.button(
                    "✨ Run Gemini Analysis",
                    key="sl_gemini_run",
                    type="primary",
                    help="Send shortlisted themes to Google Gemini Flash for investment analysis",
                )
            with _sl_gcol2:
                if st.session_state.get("sl_gemini_analysis"):
                    if st.button("🗑  Clear", key="sl_gemini_clear"):
                        st.session_state.pop("sl_gemini_analysis", None)
                        st.rerun()

            if _sl_run_gem:
                with st.spinner("Calling Google Gemini API…"):
                    try:
                        _sl_theme_lines = []
                        for _si, _st_t in enumerate(sl_themes[:15], 1):
                            _sl_theme_lines.append(
                                f"{_si}. {_st_t.get('theme_name','')} "
                                f"[{(_st_t.get('conviction') or '').upper()}] "
                                f"| Score: {float(_st_t.get('strength_score') or 0):.0f} "
                                f"| {int(_st_t.get('confirmed_quarters') or 0)} quarters "
                                f"| {int(_st_t.get('company_count') or 0)} companies"
                            )
                        _sl_themes_block = "\n".join(_sl_theme_lines) or "No themes available."
                        _sl_market = "India (NSE/BSE)" if COUNTRY_CODE == "IN" else "USA (NYSE/NASDAQ)"
                        _sl_prompt = (
                            f"You are an expert macro investment analyst. The following investment themes "
                            f"were auto-detected from {_sl_market} market company filings and earnings data.\n\n"
                            f"SHORTLISTED THEMES ({len(sl_themes)} themes):\n{_sl_themes_block}\n\n"
                            "Provide a concise investment analysis covering:\n"
                            "1. Top 3 themes with the strongest multi-year investment case (2-3 sentences each)\n"
                            "2. Cross-theme connections and amplifying factors\n"
                            "3. Key macro risks to monitor\n"
                            "4. Sector rotation implications\n\n"
                            "Be specific, data-driven, and actionable. Write for a professional equity investor."
                        )
                        st.session_state["sl_gemini_analysis"] = _call_gemini_api(
                            _sl_prompt, _sl_gemini_key, _sl_gemini_model
                        )
                    except Exception as _sl_ge:
                        st.error(f"Gemini API error: {_sl_ge}")

            if st.session_state.get("sl_gemini_analysis"):
                st.markdown(
                    '<div style="border-left:3px solid #a855f7;background:#1e0a3b;'
                    'border-radius:0 8px 8px 0;padding:6px 14px 2px 14px;margin-bottom:4px">'
                    '<span style="font-size:0.70rem;font-weight:700;color:#a855f7;'
                    'text-transform:uppercase;letter-spacing:.08em">🤖 Gemini Flash — Investment Analysis</span>'
                    '</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(st.session_state["sl_gemini_analysis"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB — STOCK RANKINGS
# Thematic investing ranking layer: Theme Strength → Supplier Quality →
# Confluence → Category Weight → Final Score
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_ranking:
    import pandas as _pd_rk
    from datetime import date as _date_rk, timedelta as _td_rk

    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:10px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} Rankings for <b>{COUNTRY_LABEL}</b> — '
        f'change market in the sidebar ←</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div style="background:#172033;border-left:3px solid #f59e0b;border-radius:6px;'
        'padding:8px 16px;margin-bottom:16px;font-size:0.82rem;color:#fde68a">'
        '🏆 <b>Stock Rankings</b> — post-detection ranking layer. '
        'Converts detected themes into investable stock candidates using '
        'Theme Strength × Supplier Quality × Confluence × Category Weight.</div>',
        unsafe_allow_html=True,
    )

    # ── Controls ──────────────────────────────────────────────────────────────
    rk_c1, rk_c2, rk_c3, rk_c4 = st.columns([1.2, 1.2, 0.8, 0.8])
    with rk_c1:
        rk_date_from = st.date_input(
            "Date from",
            value=_date_rk(2020, 1, 1),
            key="rk_date_from",
            help="Start of the analysis window (filters snapshots and signals)",
        )
    with rk_c2:
        rk_date_to = st.date_input(
            "Date to",
            value=_date_rk.today(),
            key="rk_date_to",
        )
    with rk_c3:
        rk_top_n = st.selectbox(
            "Top N themes (momentum)",
            options=[10, 15, 20, 30],
            index=1,
            key="rk_top_n",
            help=(
                "Momentum-based quota: top-N themes by rank_score. "
                "Any theme with CQ ≥ 0.45 is also included automatically, "
                "so specific bottleneck themes (AI shortage, semiconductor) "
                "always enter the pool even if they rank outside top-N."
            ),
        )
    with rk_c4:
        rk_role_filter = st.selectbox(
            "Role filter",
            ["All roles", "Supply only", "Beneficiary only", "Direct only"],
            key="rk_role_filter",
        )

    rk_run = st.button("▶  Run Ranking", type="primary", key="rk_run")

    # Invalidate cached results whenever the date range, top-N, OR country changes
    # so stale US results are never shown when the user switches to India.
    _rk_criteria_key = f"{rk_date_from}|{rk_date_to}|{rk_top_n}|{COUNTRY_CODE}"
    if st.session_state.get("rk_criteria_key") != _rk_criteria_key:
        st.session_state.pop("rk_results", None)
        st.session_state["rk_criteria_key"] = _rk_criteria_key

    # ── Execute ───────────────────────────────────────────────────────────────
    if rk_run or st.session_state.get("rk_results"):
        if rk_run:
            if not pg:
                st.error("No database connection — cannot run ranking.")
            else:
                with st.spinner("Running ranking engine …"):
                    try:
                        from makrograph.ranking import RankingEngine
                        _engine = RankingEngine(pg)
                        _rk_themes, _rk_stocks = _engine.run(
                            date_from   = rk_date_from,
                            date_to     = rk_date_to,
                            top_n_themes= rk_top_n,
                            country     = COUNTRY_CODE,
                        )
                        st.session_state["rk_results"] = {
                            "themes": _rk_themes,
                            "stocks": _rk_stocks,
                            "date_from": str(rk_date_from),
                            "date_to":   str(rk_date_to),
                            "top_n":     rk_top_n,
                        }
                    except Exception as _rk_exc:
                        import traceback as _tb
                        st.error(f"Ranking failed: {_rk_exc}")
                        st.code(_tb.format_exc())
                        st.session_state.pop("rk_results", None)

        rk_res = st.session_state.get("rk_results", {})
        rk_themes_res: list = rk_res.get("themes", [])
        rk_stocks_res: list = rk_res.get("stocks", [])

        if not rk_res:
            pass  # error already shown above
        elif not rk_stocks_res:
            st.info(
                "No ranked stocks found for this period. "
                "Make sure the pipeline has run across the selected date range."
            )
        else:
            # ── apply role filter ─────────────────────────────────────────────
            _role_map = {
                "Supply only":      "supply",
                "Beneficiary only": "beneficiary",
                "Direct only":      "direct",
            }
            _role_sel = _role_map.get(rk_role_filter)
            _display_stocks = (
                [s for s in rk_stocks_res if s.company_role == _role_sel]
                if _role_sel else rk_stocks_res
            )

            # ── meta banner ───────────────────────────────────────────────────
            _supply_cnt = sum(1 for s in _display_stocks if s.company_role == "supply")
            _bene_cnt   = sum(1 for s in _display_stocks if s.company_role == "beneficiary")
            _direct_cnt = sum(1 for s in _display_stocks if s.company_role == "direct")
            st.markdown(
                f'<div style="background:#172033;border:1px solid #334155;border-radius:8px;'
                f'padding:10px 16px;margin-bottom:14px;font-size:0.82rem;color:#94a3b8">'
                f'📅 Analysis window: <b style="color:#f1f5f9">{rk_res["date_from"]}</b>'
                f' → <b style="color:#f1f5f9">{rk_res["date_to"]}</b>'
                f' &nbsp;·&nbsp; Top <b style="color:#f59e0b">{rk_res["top_n"]}</b> themes'
                f' &nbsp;·&nbsp; <b style="color:#22c55e">{len(_display_stocks)}</b> stocks'
                f' &nbsp;(<span style="color:#4ade80">{_supply_cnt} supply</span>'
                f' · <span style="color:#93c5fd">{_bene_cnt} bene</span>'
                f' · <span style="color:#c4b5fd">{_direct_cnt} direct</span>)'
                f'</div>',
                unsafe_allow_html=True,
            )

            # ━━ Section A: Top Themes (6-factor + ThemeCQ) ━━━━━━━━━━━━━━━━━━
            with st.expander("📊 Theme Scores — rank_score + ThemeCQ (dual-criterion pool)", expanded=True):
                st.markdown(
                    '<div style="font-size:0.75rem;color:#64748b;margin-bottom:6px">'
                    "Themes enter the pool by <b>rank_score</b> (momentum quota) OR by "
                    "<b>ThemeCQ ≥ 0.45</b> (bottleneck quality floor). "
                    "The ★ column marks CQ-floor entries — these are the themes that would "
                    "have been excluded under the old top-N-only selection.</div>",
                    unsafe_allow_html=True,
                )
                _from_cq_floor = {
                    t.theme_name for t in rk_themes_res
                    if getattr(t, "theme_cq", 0) >= 0.45
                }
                _th_rows = []
                for _i, _t in enumerate(rk_themes_res, 1):
                    _fd = getattr(_t, "first_detected", None)
                    if _fd:
                        _fd_d = _fd if isinstance(_fd, date) else date.fromisoformat(str(_fd)[:10])
                        _th_age = (date.today() - _fd_d).days
                        _th_fresh = "🟢 Fresh" if _th_age <= 90 else ("🟡 Active" if _th_age <= 365 else "🔴 Mature")
                        _fd_str = str(_fd_d)
                    else:
                        _th_fresh = "❓"
                        _fd_str = "—"
                    _th_rows.append({
                        "★":              "CQ" if _t.theme_name in _from_cq_floor else "",
                        "Theme":          _t.theme_name,
                        "ThemeCQ":        round(getattr(_t, "theme_cq", 0), 3),
                        "Co.Count":       getattr(_t, "company_count", 0),
                        "Conviction":     _t.conviction.title(),
                        "RankScore":      _t.rank_score_pct,
                        "Momentum":       round(_t.momentum, 3),
                        "Persist.":       round(_t.persistence, 3),
                        "Novelty":        round(_t.novelty, 3),
                        "Sig.Int":        round(_t.signal_intensity, 3),
                        "First Detected": _fd_str,
                        "Freshness":      _th_fresh,
                    })
                _th_df = _pd_rk.DataFrame(_th_rows)
                _prog = lambda lbl: st.column_config.ProgressColumn(lbl, min_value=0, max_value=1, format="%.3f")
                st.dataframe(
                    _th_df, use_container_width=True, hide_index=True,
                    column_config={
                        "ThemeCQ":        st.column_config.ProgressColumn("ThemeCQ", min_value=0, max_value=1, format="%.3f"),
                        "RankScore":      st.column_config.ProgressColumn("RankScore (0–100)", min_value=0, max_value=100, format="%.1f"),
                        "Momentum":       _prog("Momentum"),
                        "Persist.":       _prog("Persist."),
                        "Novelty":        _prog("Novelty"),
                        "Sig.Int":        _prog("Sig.Int"),
                        "First Detected": st.column_config.TextColumn("First Detected",
                                              help="When this theme was first detected by the pipeline"),
                        "Freshness":      st.column_config.TextColumn("Freshness",
                                              help="🟢 <90d · 🟡 90–365d · 🔴 >365d (potentially exhausted)"),
                    },
                )

            # ━━ Section B: Stock Ranking Cards ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            st.markdown(
                '<div style="font-size:0.9rem;font-weight:700;color:#f1f5f9;'
                'margin:18px 0 10px">🏅 Ranked Stock Candidates</div>',
                unsafe_allow_html=True,
            )

            _ROLE_COLOR = {
                "supply":      ("#166534", "#4ade80"),
                "beneficiary": ("#1e3a5f", "#93c5fd"),
                "direct":      ("#3b1f6e", "#c4b5fd"),
            }
            _ROLE_LABEL = {
                "supply": "Supply",
                "beneficiary": "Beneficiary",
                "direct": "Direct",
            }

            for _stk in _display_stocks[:50]:   # cap at 50 cards
                _bg, _fg = _ROLE_COLOR.get(_stk.company_role, ("#1e293b", "#94a3b8"))
                _role_lbl = _ROLE_LABEL.get(_stk.company_role, _stk.company_role.title())

                # ── Freshness badge for this stock ───────────────────────────
                _stk_fsa = getattr(_stk, "first_seen_at", None)
                if _stk_fsa:
                    _stk_fsd = _stk_fsa if isinstance(_stk_fsa, date) else date.fromisoformat(str(_stk_fsa)[:10])
                    _stk_age = (date.today() - _stk_fsd).days
                    if _stk_age <= 90:
                        _stk_fresh_col, _stk_fresh_lbl = "#22c55e", f"🟢 Fresh · first seen {_stk_fsd}"
                    elif _stk_age <= 365:
                        _stk_fresh_col, _stk_fresh_lbl = "#f59e0b", f"🟡 Active · first seen {_stk_fsd}"
                    else:
                        _stk_fresh_col, _stk_fresh_lbl = "#ef4444", f"🔴 Mature · first seen {_stk_fsd} ({_stk_age}d ago)"
                else:
                    _stk_fresh_col, _stk_fresh_lbl = "#475569", "❓ Date unknown"
                _theme_pills = " ".join(
                    f'<span style="background:#0f2942;color:#7dd3fc;border:1px solid #1e3a5f;'
                    f'border-radius:4px;padding:1px 6px;font-size:0.68rem">{_tn[:30]}</span>'
                    for _tn in _stk.themes[:4]
                )
                _sig_pills = " ".join(
                    f'<span style="background:#172033;color:#86efac;border:1px solid #14532d;'
                    f'border-radius:4px;padding:1px 6px;font-size:0.68rem">{_sh}</span>'
                    for _sh in _stk.signal_highlights
                ) or '<span style="color:#475569;font-size:0.68rem">no signal highlights</span>'

                _score_bar_pct = min(100, int(_stk.final_score * 35))  # scale for visual
                _role_conf_pct = int(_stk.role_confidence * 100)

                # Theme pills with edge score shown
                _theme_pills_parts = []
                for _ti, _tn in enumerate(_stk.themes[:4]):
                    _slug = _stk.theme_slugs[_ti] if _ti < len(_stk.theme_slugs) else ""
                    _es   = _stk.per_theme_edges.get(_slug, 0)
                    _theme_pills_parts.append(
                        f'<span style="background:#0f2942;color:#7dd3fc;border:1px solid #1e3a5f;'
                        f'border-radius:4px;padding:1px 6px;font-size:0.68rem">'
                        f'{_tn[:28]}'
                        f'<span style="color:#475569;margin-left:3px">e={_es:.2f}</span>'
                        f'</span>'
                    )
                _theme_pills = " ".join(_theme_pills_parts)

                # Best constraint block (precomputed to avoid implicit-concat break)
                _best_theme_label  = str(_stk.cq_breakdown.get("Best Theme", "—"))
                _best_theme_cq     = float(_stk.cq_breakdown.get("Theme CQ", 0))
                _best_role_decay   = float(_stk.cq_breakdown.get("Role Decay", 0))
                _best_sig_factor   = float(_stk.cq_breakdown.get("Signal Factor", 0))
                _best_edge_cq_val  = float(_stk.cq_breakdown.get("Best Edge CQ", 0))
                _best_constraint_html = (
                    f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;'
                    f'background:#0f172a;border-radius:6px;padding:5px 8px">'
                    f'<span style="font-size:0.58rem;color:#64748b;white-space:nowrap">BEST CONSTRAINT</span>'
                    f'<span style="font-size:0.68rem;color:#c4b5fd;flex:1;overflow:hidden;'
                    f'white-space:nowrap;text-overflow:ellipsis">{_best_theme_label}</span>'
                    f'<span style="font-size:0.68rem;color:#e879f9;white-space:nowrap">'
                    f'ThemeCQ={_best_theme_cq:.3f} · '
                    f'Decay={_best_role_decay:.1f} · '
                    f'Sig={_best_sig_factor:.2f} → '
                    f'EdgeCQ=<b>{_best_edge_cq_val:.3f}</b>'
                    f'</span>'
                    f'</div>'
                )

                st.markdown(
                    f'<div style="background:#1e293b;border:1.5px solid #334155;'
                    f'border-radius:12px;padding:14px 18px;margin-bottom:8px">'

                    # Header row
                    f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap">'
                    f'<span style="color:#475569;font-size:0.72rem;font-weight:700;min-width:28px">'
                    f'#{_stk.rank}</span>'
                    f'<span style="font-size:1.1rem;font-weight:800;color:#f59e0b">'
                    f'{_stk.ticker}</span>'
                    f'<span style="font-size:0.9rem;color:#e2e8f0">{_stk.company_name}</span>'
                    # role badge with confidence
                    f'<span style="background:{_bg};color:{_fg};border:1px solid {_fg}33;'
                    f'border-radius:5px;padding:1px 8px;font-size:0.70rem;font-weight:700;'
                    f'margin-left:auto">'
                    f'{_role_lbl} · {_stk.category_weight}× '
                    f'<span style="opacity:0.7">({_role_conf_pct}% conf)</span>'
                    f'</span>'
                    # freshness badge
                    f'<span style="color:{_stk_fresh_col};font-size:0.68rem;white-space:nowrap">'
                    f'{_stk_fresh_lbl}</span>'
                    f'</div>'

                    # Score bar
                    f'<div style="background:#0f172a;border-radius:6px;height:6px;'
                    f'margin-bottom:10px;overflow:hidden">'
                    f'<div style="background:linear-gradient(90deg,#f59e0b,#fbbf24);'
                    f'width:{_score_bar_pct}%;height:100%"></div></div>'

                    # Score breakdown (v6: best-edge dominant)
                    # Final = (0.55×BestEdgeCQ + 0.25×AvgTop3 + 0.20×SupplierQ)
                    #         × (1+ConfluenceBonus) × CategoryWeight
                    f'<div style="display:flex;gap:14px;margin-bottom:10px;flex-wrap:wrap">'
                    f'<div><div style="font-size:0.58rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Final Score</div>'
                    f'<div style="font-size:1.05rem;font-weight:800;color:#f59e0b">{_stk.final_score:.4f}</div></div>'
                    f'<div><div style="font-size:0.58rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Best Edge CQ 55%</div>'
                    f'<div style="font-size:0.95rem;font-weight:700;color:#818cf8">{_stk.effective_theme:.3f}</div></div>'
                    f'<div><div style="font-size:0.58rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Supplier 20%</div>'
                    f'<div style="font-size:0.95rem;font-weight:700;color:#22c55e">{_stk.supplier_quality:.3f}</div></div>'
                    f'<div><div style="font-size:0.58rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Constraints</div>'
                    f'<div style="font-size:0.95rem;font-weight:700;color:#38bdf8">{_stk.confluence_score:.1f} '
                    f'<span style="font-size:0.65rem;color:#475569">(of {len(_stk.themes)}T)</span></div></div>'
                    f'<div><div style="font-size:0.58rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Cat.Wt</div>'
                    f'<div style="font-size:0.95rem;font-weight:700;color:{_fg}">{_stk.category_weight}×</div></div>'
                    f'</div>'
                    f'{_best_constraint_html}'

                    # Theme-edge pills
                    f'<div style="margin-bottom:7px">'
                    f'<span style="font-size:0.65rem;color:#64748b;margin-right:6px">THEMES + EDGE</span>'
                    f'{_theme_pills}</div>'

                    # Signal highlights
                    f'<div>'
                    f'<span style="font-size:0.65rem;color:#64748b;margin-right:6px">SIGNALS</span>'
                    f'{_sig_pills}</div>'

                    f'</div>',
                    unsafe_allow_html=True,
                )

            # ━━ Section C: Full table (downloadable) ━━━━━━━━━━━━━━━━━━━━━━━━
            with st.expander("📋 Full ranking table (CSV export)", expanded=False):
                _tbl_rows = []
                for _s in _display_stocks:
                    _s_fsa = getattr(_s, "first_seen_at", None)
                    _s_fsa_str = str(_s_fsa)[:10] if _s_fsa else "—"
                    _s_age = (date.today() - (_s_fsa if isinstance(_s_fsa, date) else date.fromisoformat(str(_s_fsa)[:10]))).days if _s_fsa else None
                    _s_fresh = ("🟢 Fresh" if (_s_age or 9999) <= 90 else ("🟡 Active" if (_s_age or 9999) <= 365 else "🔴 Mature")) if _s_fsa else "❓"
                    _tbl_rows.append({
                        "Rank":              _s.rank,
                        "Ticker":            _s.ticker,
                        "Company":           _s.company_name,
                        "First Seen":        _s_fsa_str,
                        "Freshness":         _s_fresh,
                        "Role":              _s.company_role.title(),
                        "Role Conf.":        round(_s.role_confidence, 3),
                        "Cat. Weight":       _s.category_weight,
                        "Best Edge CQ":      round(_s.constraint_quality, 4),
                        "Theme CQ":          round(float(_s.cq_breakdown.get("Theme CQ", 0)), 3),
                        "Role Decay":        round(float(_s.cq_breakdown.get("Role Decay", 0)), 2),
                        "Signal Factor":     round(float(_s.cq_breakdown.get("Signal Factor", 0)), 3),
                        "Supplier 20%":      round(_s.supplier_quality, 4),
                        "N Constraints":     int(_s.cq_breakdown.get("N Constraints", 0)),
                        "Conf Bonus":        round(float(_s.cq_breakdown.get("Conf Bonus", 0)), 3),
                        "Final Score":       round(_s.final_score, 4),
                        "# Themes":          len(_s.themes),
                        "Best Theme":        str(_s.cq_breakdown.get("Best Theme", "")),
                        "Themes":            "; ".join(_s.themes[:5]),
                        "Signal Highlights": "; ".join(_s.signal_highlights),
                    })
                _full_df = _pd_rk.DataFrame(_tbl_rows)
                st.dataframe(_full_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "⬇  Download CSV",
                    data=_full_df.to_csv(index=False),
                    file_name=f"stock_rankings_{rk_res['date_from']}_{rk_res['date_to']}.csv",
                    mime="text/csv",
                    key="rk_download",
                )

            # ━━ Section D: Supplier Quality Breakdown ━━━━━━━━━━━━━━━━━━━━━━━
            with st.expander("🔬 Supplier quality breakdown (top 20)", expanded=False):
                _qual_rows = []
                for _s in _display_stocks[:20]:
                    _row = {"Ticker": _s.ticker, "Company": _s.company_name}
                    _row.update(_s.quality_breakdown)
                    _row["Avg Quality"] = round(_s.supplier_quality, 3)
                    _qual_rows.append(_row)
                if _qual_rows:
                    st.dataframe(
                        _pd_rk.DataFrame(_qual_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

            # ━━ Section E: Best-Edge CQ Breakdown ━━━━━━━━━━━━━━━━━━━━━━━━━━
            with st.expander("🔩 Best-edge constraint quality (top 20)", expanded=False):
                st.markdown(
                    '<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px">'
                    "<b>v6 architecture</b>: CQ belongs to the THEME/EDGE, not the company. "
                    "ThemeCQ is scored once per theme (keyword + scarcity + momentum×conviction + signals) "
                    "then decayed by role distance. A company's score = its BEST bottleneck edge, "
                    "not a count of themes it appears in. "
                    "Expected: ANET/VRT/LRCX &gt; COST/AMZN/AZO even if the latter appear in more themes.</div>",
                    unsafe_allow_html=True,
                )
                _cq_rows = []
                for _s in _display_stocks[:20]:
                    _crow = {
                        "Rank":          _s.rank,
                        "Ticker":        _s.ticker,
                        "Company":       _s.company_name,
                        "Role":          _s.company_role.title(),
                        "Best Theme":    str(_s.cq_breakdown.get("Best Theme", ""))[:35],
                        "Theme CQ":      round(float(_s.cq_breakdown.get("Theme CQ", 0)), 3),
                        "Role Decay":    round(float(_s.cq_breakdown.get("Role Decay", 0)), 2),
                        "Signal Factor": round(float(_s.cq_breakdown.get("Signal Factor", 0)), 3),
                        "Best Edge CQ":  round(float(_s.cq_breakdown.get("Best Edge CQ", 0)), 3),
                        "N Constraints": int(_s.cq_breakdown.get("N Constraints", 0)),
                        "Conf Bonus":    round(float(_s.cq_breakdown.get("Conf Bonus", 0)), 3),
                        "Final Score":   round(_s.final_score, 4),
                    }
                    _cq_rows.append(_crow)
                if _cq_rows:
                    st.dataframe(
                        _pd_rk.DataFrame(_cq_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

            # ━━ Section F: Gemini AI Analysis ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            st.markdown("---")
            _rk_gemini_key   = cfg.get("gemini", {}).get("api_key", "")
            _rk_gemini_model = cfg.get("gemini", {}).get("model", "gemini-flash-latest")

            st.markdown(
                '<div style="background:#0f172a;border-left:3px solid #a855f7;border-radius:8px;'
                'padding:8px 16px;margin-bottom:12px">'
                '<span style="font-size:0.92rem;font-weight:700;color:#f1f5f9">🤖 Gemini AI Analysis</span>'
                f'<span style="font-size:0.75rem;color:#64748b;margin-left:10px">'
                f'Google Gemini Flash · {COUNTRY_LABEL} · {len(rk_themes_res)} themes · '
                f'{len(_display_stocks)} stocks</span>'
                '</div>',
                unsafe_allow_html=True,
            )

            if not _rk_gemini_key:
                st.caption("💡 Set `gemini.api_key` in `config/settings.yaml` to enable Gemini analysis.")
            else:
                _rk_gcol1, _rk_gcol2 = st.columns([1, 5])
                with _rk_gcol1:
                    _rk_run_gem = st.button(
                        "✨ Run Gemini Analysis",
                        key="rk_gemini_run",
                        type="primary",
                        help="Send ranked themes + stocks to Google Gemini Flash for portfolio analysis",
                    )
                with _rk_gcol2:
                    if st.session_state.get("rk_gemini_analysis"):
                        if st.button("🗑  Clear", key="rk_gemini_clear"):
                            st.session_state.pop("rk_gemini_analysis", None)
                            st.rerun()

                if _rk_run_gem:
                    with st.spinner("Calling Google Gemini API…"):
                        try:
                            _rk_theme_lines = []
                            for _ri, _rt in enumerate(rk_themes_res[:15], 1):
                                _rk_theme_lines.append(
                                    f"{_ri}. {_rt.theme_name} [{_rt.conviction.upper()}] "
                                    f"| RankScore: {_rt.rank_score_pct:.1f} "
                                    f"| Momentum: {_rt.momentum:.3f} "
                                    f"| {getattr(_rt, 'company_count', 0)} companies"
                                )
                            _rk_themes_block = "\n".join(_rk_theme_lines) or "No themes ranked yet."

                            _rk_stock_lines = []
                            for _rs in _display_stocks[:20]:
                                _rk_stock_lines.append(
                                    f"  #{_rs.rank} {_rs.ticker} ({_rs.company_name}) "
                                    f"| {_rs.company_role} | Score: {_rs.final_score:.4f} "
                                    f"| Themes: {', '.join(_rs.themes[:3])}"
                                )
                            _rk_stocks_block = "\n".join(_rk_stock_lines) or "No stocks ranked yet."

                            _rk_market = "India (NSE/BSE)" if COUNTRY_CODE == "IN" else "USA (NYSE/NASDAQ)"
                            _rk_prompt = (
                                f"You are an expert thematic portfolio manager. The following stocks were "
                                f"ranked using multi-factor thematic analysis of {_rk_market} company filings.\n\n"
                                f"ACTIVE THEMES ({len(rk_themes_res)} themes):\n{_rk_themes_block}\n\n"
                                f"TOP RANKED STOCKS:\n{_rk_stocks_block}\n\n"
                                "Provide:\n"
                                "1. Top 5 high-conviction positions with brief rationale (1-2 sentences each)\n"
                                "2. Portfolio construction guidance (supply chain vs end beneficiary vs direct plays)\n"
                                "3. Key theme concentration risks to hedge\n"
                                "4. One contrarian view worth considering\n\n"
                                "Be concise and actionable. Write for a professional portfolio manager."
                            )
                            st.session_state["rk_gemini_analysis"] = _call_gemini_api(
                                _rk_prompt, _rk_gemini_key, _rk_gemini_model
                            )
                        except Exception as _rk_ge:
                            st.error(f"Gemini API error: {_rk_ge}")

                if st.session_state.get("rk_gemini_analysis"):
                    st.markdown(
                        '<div style="border-left:3px solid #a855f7;background:#1e0a3b;'
                        'border-radius:0 8px 8px 0;padding:6px 14px 2px 14px;margin-bottom:4px">'
                        '<span style="font-size:0.70rem;font-weight:700;color:#a855f7;'
                        'text-transform:uppercase;letter-spacing:.08em">'
                        '🤖 Gemini Flash — Portfolio Analysis</span>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(st.session_state["rk_gemini_analysis"])

    else:
        # Pre-run placeholder
        st.markdown(
            '<div style="background:#1e293b;border:1px dashed #334155;border-radius:10px;'
            'padding:32px;text-align:center;color:#475569">'
            '<div style="font-size:2rem;margin-bottom:8px">🏆</div>'
            '<div style="font-size:0.9rem;color:#64748b">Select a date range and click '
            '<b style="color:#f59e0b">▶ Run Ranking</b> to compute the thematic stock ranking.'
            '</div>'
            '<div style="font-size:0.78rem;color:#334155;margin-top:12px">'
            'Theme Strength (30% Bottleneck · 25% Downstream · 20% Freshness · '
            '15% Breadth · 10% Acceleration) × Supplier Quality × Confluence × Category Weight'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB — AI ANALYSIS (Gemini)
# Comprehensive AI analysis of all themes, bottlenecks, and ranked stocks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_ai:
    import json as _json_ai
    from datetime import date as _date_ai, timedelta as _td_ai

    _ai_gemini_key   = cfg.get("gemini", {}).get("api_key", "")
    _ai_gemini_model = cfg.get("gemini", {}).get("model", "gemini-flash-latest")
    _ai_market = "India (NSE/BSE)" if COUNTRY_CODE == "IN" else "USA (NYSE/NASDAQ)"

    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:10px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} AI Analysis for <b>{COUNTRY_LABEL}</b> — '
        f'change market in the sidebar ←</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div style="background:#1a0a2e;border-left:3px solid #a855f7;border-radius:6px;'
        'padding:8px 16px;margin-bottom:16px;font-size:0.82rem;color:#e9d5ff">'
        '🤖 <b>AI Analysis</b> — Comprehensive Gemini Flash analysis covering all detected themes, '
        'supply-chain bottlenecks, shortlisted multi-quarter themes, and ranked stocks. '
        'One click → full professional investment brief.</div>',
        unsafe_allow_html=True,
    )

    # ── Load data independently ────────────────────────────────────────────────
    _ai_all_themes = load_themes(min_strength=0.0, country=COUNTRY_CODE)
    _ai_sl_themes  = load_shortlisted_themes(min_quarters=2, country=COUNTRY_CODE)

    # Identify bottleneck themes from metadata
    _ai_bottlenecks = []
    for _t in _ai_all_themes:
        _bmeta = _t.get("metadata") or {}
        if isinstance(_bmeta, str):
            try:
                _bmeta = _json_ai.loads(_bmeta)
            except Exception:
                _bmeta = {}
        if (
            _bmeta.get("theme_type") == "bottleneck"
            or _bmeta.get("is_bottleneck")
            or _bmeta.get("constraint_kw_count", 0) >= 3
        ):
            _ai_bottlenecks.append(_t)

    # ── KPI cards ─────────────────────────────────────────────────────────────
    _ai_c1, _ai_c2, _ai_c3, _ai_c4 = st.columns(4)
    for _col, _val, _lbl, _clr in [
        (_ai_c1, len(_ai_all_themes),   "Active Themes",      "#818cf8"),
        (_ai_c2, len(_ai_bottlenecks),  "Bottleneck Themes",  "#f59e0b"),
        (_ai_c3, len(_ai_sl_themes),    "Shortlisted Themes", "#22c55e"),
        (_ai_c4, "Flash",               "Gemini Model",       "#a855f7"),
    ]:
        with _col:
            st.markdown(
                f'<div class="kpi-card">'
                f'<div class="kpi-num" style="color:{_clr}">{_val}</div>'
                f'<div class="kpi-label">{_lbl}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    if not _ai_gemini_key:
        st.warning(
            "⚠️ **Gemini API key not configured.**  "
            "Set `gemini.api_key` in `config/settings.yaml` to enable AI analysis.",
            icon="🔑",
        )
    elif not _ai_all_themes and not _ai_sl_themes:
        st.info(
            "No themes detected yet. Run the pipeline (🚀 Pipeline Runner tab) "
            "to populate themes first.",
            icon="ℹ️",
        )
    else:
        # ── Analysis mode selector ─────────────────────────────────────────────
        _ai_mode = st.radio(
            "Analysis mode",
            [
                "🔍 Theme Focus — in-depth analysis of all active themes + connections",
                "⚠️ Bottleneck Focus — supply constraint, risk & second-order effects",
                "🏆 Portfolio Focus — ranked stocks, positioning & construction guidance",
                "🌐 Master Analysis — all themes + bottlenecks + stocks (comprehensive brief)",
            ],
            index=3,
            key="ai_mode_selector",
            help=(
                "Theme Focus: full theme landscape analysis.\n"
                "Bottleneck Focus: supply constraint deep-dive.\n"
                "Portfolio Focus: stock picking and sizing.\n"
                "Master Analysis: all-in-one investment brief."
            ),
        )

        # ── Action buttons ─────────────────────────────────────────────────────
        _ai_btn_c1, _ai_btn_c2, _ai_btn_spacer = st.columns([1, 1, 4])
        with _ai_btn_c1:
            _ai_run = st.button(
                "✨ Run AI Analysis",
                key="ai_run_btn",
                type="primary",
                help="Send all pipeline data to Gemini Flash and generate investment analysis",
            )
        with _ai_btn_c2:
            if st.session_state.get("ai_analysis_result"):
                if st.button("🗑  Clear result", key="ai_clear_btn"):
                    st.session_state.pop("ai_analysis_result", None)
                    st.session_state.pop("ai_analysis_meta", None)
                    st.rerun()

        # ── Execute ────────────────────────────────────────────────────────────
        if _ai_run:
            with st.spinner("🤖 Building context and calling Gemini Flash…"):
                try:
                    # ── Build all-themes block ─────────────────────────────────
                    _ai_theme_lines = []
                    for _i, _t in enumerate(_ai_all_themes[:25], 1):
                        _m = _t.get("metadata") or {}
                        if isinstance(_m, str):
                            try:
                                _m = _json_ai.loads(_m)
                            except Exception:
                                _m = {}
                        _ttype = _m.get("theme_type", "auto")
                        _bn_flag = " 🔴[BOTTLENECK]" if (
                            _m.get("is_bottleneck") or _ttype == "bottleneck"
                            or _m.get("constraint_kw_count", 0) >= 3
                        ) else ""
                        _ai_theme_lines.append(
                            f"{_i}. {_t.get('theme_name', '')} "
                            f"[{(_t.get('conviction') or 'emerging').upper()}]{_bn_flag} "
                            f"| Score:{float(_t.get('strength_score') or 0):.0f} "
                            f"| Q:{int(_t.get('confirmed_quarters') or 0)} "
                            f"| Cos:{int(_t.get('company_count') or 0)} "
                            f"| Type:{_ttype}"
                        )
                    _ai_themes_block = "\n".join(_ai_theme_lines) or "No themes detected."

                    # ── Build shortlisted block ────────────────────────────────
                    _ai_sl_lines = []
                    for _i, _t in enumerate(_ai_sl_themes[:15], 1):
                        _ai_sl_lines.append(
                            f"{_i}. {_t.get('theme_name', '')} "
                            f"[{(_t.get('conviction') or 'emerging').upper()}] "
                            f"| Score:{float(_t.get('strength_score') or 0):.0f} "
                            f"| {int(_t.get('confirmed_quarters') or 0)} quarters "
                            f"| {int(_t.get('company_count') or 0)} companies"
                        )
                    _ai_sl_block = "\n".join(_ai_sl_lines) or "No shortlisted themes yet."

                    # ── Build bottleneck block ─────────────────────────────────
                    _ai_bn_lines = []
                    for _i, _t in enumerate(_ai_bottlenecks[:10], 1):
                        _ai_bn_lines.append(
                            f"{_i}. {_t.get('theme_name', '')} "
                            f"| Score:{float(_t.get('strength_score') or 0):.0f} "
                            f"| Companies:{int(_t.get('company_count') or 0)}"
                        )
                    _ai_bn_block = (
                        "\n".join(_ai_bn_lines)
                        if _ai_bn_lines else "No bottleneck themes detected."
                    )

                    # ── Fetch ranked stocks (best-effort) ──────────────────────
                    _ai_stocks_block = (
                        "No ranked stocks available — open 🏆 Stock Rankings tab "
                        "and click ▶ Run Ranking first, or wait for the pipeline to complete."
                    )
                    _ai_stock_count = 0
                    if pg:
                        try:
                            from makrograph.ranking import RankingEngine as _AI_RE
                            _ai_engine = _AI_RE(pg)
                            _, _ai_rk_stocks = _ai_engine.run(
                                date_from    = _date_ai.today() - _td_ai(days=730),
                                date_to      = _date_ai.today(),
                                top_n_themes = 15,
                                country      = COUNTRY_CODE,
                            )
                            _ai_rk_stocks = list(_ai_rk_stocks)[:20]
                            if _ai_rk_stocks:
                                _ai_stock_count = len(_ai_rk_stocks)
                                _slines = []
                                for _s in _ai_rk_stocks:
                                    _slines.append(
                                        f"  #{_s.rank} {_s.ticker} ({_s.company_name}) "
                                        f"| {_s.company_role} "
                                        f"| Score:{_s.final_score:.4f} "
                                        f"| Themes:{', '.join(_s.themes[:3])}"
                                    )
                                _ai_stocks_block = "\n".join(_slines)
                        except Exception as _ai_re_err:
                            _ai_stocks_block = (
                                f"Ranking engine unavailable ({type(_ai_re_err).__name__}). "
                                "Run 🏆 Stock Rankings tab first."
                            )

                    # ── Build prompt by mode ────────────────────────────────────
                    _mode_label = _ai_mode.split("—")[0].strip()

                    if "Theme Focus" in _mode_label:
                        _ai_prompt = (
                            f"You are an expert macro investment analyst covering {_ai_market}.\n\n"
                            f"ALL ACTIVE INVESTMENT THEMES ({len(_ai_all_themes)}):\n"
                            f"{_ai_themes_block}\n\n"
                            f"SHORTLISTED THEMES (≥2 sustained quarters, {len(_ai_sl_themes)}):\n"
                            f"{_ai_sl_block}\n\n"
                            "Provide a detailed theme analysis:\n"
                            "1. **Top 5 Themes** with the strongest multi-year investment case "
                            "(2-3 sentences each — include companies, sectors, time horizon)\n"
                            "2. **Cross-Theme Connections** — amplifying or conflicting forces\n"
                            "3. **Emerging Themes** — just appeared or accelerating fast (watch list)\n"
                            "4. **Key Macro Risks** across these themes\n"
                            "5. **Sector Rotation Implications** — which sectors to overweight/underweight\n\n"
                            "Be specific, data-driven, and actionable. Write for a professional equity investor."
                        )

                    elif "Bottleneck" in _mode_label:
                        _ai_prompt = (
                            f"You are an expert macro analyst specializing in supply constraints "
                            f"and bottlenecks for {_ai_market}.\n\n"
                            f"ALL ACTIVE THEMES ({len(_ai_all_themes)}):\n{_ai_themes_block}\n\n"
                            f"IDENTIFIED BOTTLENECK / SUPPLY-CONSTRAINT THEMES ({len(_ai_bottlenecks)}):\n"
                            f"{_ai_bn_block}\n\n"
                            "Provide a supply constraint analysis:\n"
                            "1. **Critical Bottlenecks** — why each matters and expected duration "
                            "(2-3 sentences each)\n"
                            "2. **Second-Order Effects** — which downstream sectors are most exposed\n"
                            "3. **Resolution Timeline** — which constraints ease vs. persist (6–18 month view)\n"
                            "4. **Beneficiaries** — companies/sectors that profit from constraint resolution\n"
                            "5. **Hedging Strategies** — how to protect portfolios exposed to these constraints\n\n"
                            "Be specific and data-driven. Write for a professional risk manager."
                        )

                    elif "Portfolio" in _mode_label:
                        _ai_prompt = (
                            f"You are an expert thematic portfolio manager covering {_ai_market}.\n\n"
                            f"ACTIVE THEMES ({len(_ai_all_themes)}):\n{_ai_themes_block}\n\n"
                            f"TOP RANKED STOCKS:\n{_ai_stocks_block}\n\n"
                            "Provide:\n"
                            "1. **Top 10 Stock Picks** with brief rationale (1-2 sentences each, "
                            "include role: supply/demand/direct)\n"
                            "2. **Portfolio Construction** — tier the positions:\n"
                            "   - Tier 1 Core (high conviction, full position)\n"
                            "   - Tier 2 Tactical (medium conviction, half position)\n"
                            "   - Tier 3 Speculative (asymmetric upside, small position)\n"
                            "3. **Concentration Risks** — key theme overlaps to hedge\n"
                            "4. **Sizing Guidance** — suggested % weights per tier\n"
                            "5. **Contrarian View** — one idea the market is underpricing\n\n"
                            "Be concise and actionable. Write for a professional portfolio manager."
                        )

                    else:  # Master Analysis
                        _ai_prompt = (
                            f"You are an elite macro investment research team covering {_ai_market}. "
                            f"Produce a comprehensive investment brief based on this pipeline data.\n\n"
                            f"=== PIPELINE DATA ===\n\n"
                            f"ALL ACTIVE THEMES ({len(_ai_all_themes)}, auto-detected from company filings):\n"
                            f"{_ai_themes_block}\n\n"
                            f"SUSTAINED SHORTLISTED THEMES (≥2 quarters, {len(_ai_sl_themes)}):\n"
                            f"{_ai_sl_block}\n\n"
                            f"SUPPLY-CHAIN BOTTLENECKS ({len(_ai_bottlenecks)}):\n"
                            f"{_ai_bn_block}\n\n"
                            f"TOP RANKED STOCKS (thematic multi-factor scoring):\n"
                            f"{_ai_stocks_block}\n\n"
                            "=== COMPREHENSIVE INVESTMENT BRIEF ===\n\n"
                            "**1. MACRO LANDSCAPE** (3-4 sentences)\n"
                            "   Overall structural forces and investment environment.\n\n"
                            "**2. TOP 5 CONVICTION THEMES**\n"
                            "   For each: thesis (2 sentences) | key sectors | time horizon | key risk.\n\n"
                            "**3. BOTTLENECK & CONSTRAINT ANALYSIS**\n"
                            "   Critical supply constraints, second-order downstream effects, "
                            "duration estimates.\n\n"
                            "**4. TOP 10 STOCK RECOMMENDATIONS**\n"
                            "   For each: role (supply/demand/direct) | 1-sentence rationale | conviction level.\n\n"
                            "**5. PORTFOLIO CONSTRUCTION**\n"
                            "   Tier 1 core | Tier 2 tactical | Tier 3 speculative. "
                            "Suggested weight ranges.\n\n"
                            "**6. KEY RISKS & HEDGES**\n"
                            "   Top 3 macro risks and suggested hedges.\n\n"
                            "**7. CONTRARIAN VIEW**\n"
                            "   One underappreciated angle the consensus is missing.\n\n"
                            "Use markdown headers and bullet points. Be specific, data-driven, actionable."
                        )

                    _ai_result = _call_gemini_api(_ai_prompt, _ai_gemini_key, _ai_gemini_model)
                    st.session_state["ai_analysis_result"] = _ai_result
                    st.session_state["ai_analysis_meta"] = {
                        "mode":       _ai_mode,
                        "themes":     len(_ai_all_themes),
                        "sl":         len(_ai_sl_themes),
                        "bottlenecks": len(_ai_bottlenecks),
                        "stocks":     _ai_stock_count,
                        "market":     _ai_market,
                        "ts":         datetime.now().strftime("%H:%M:%S"),
                    }

                except Exception as _ai_err:
                    st.error(f"Gemini API error: {_ai_err}")

        # ── Display Result ─────────────────────────────────────────────────────
        if st.session_state.get("ai_analysis_result"):
            _ai_meta = st.session_state.get("ai_analysis_meta", {})
            st.markdown(
                f'<div style="background:#1a0a2e;border:1px solid #7c3aed;border-radius:8px;'
                f'padding:8px 16px;margin:12px 0 6px 0;display:flex;'
                f'justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'
                f'<div>'
                f'<span style="font-size:0.92rem;font-weight:700;color:#f1f5f9">'
                f'🤖 Gemini Flash — AI Investment Analysis</span>'
                f'<span style="font-size:0.72rem;color:#a855f7;margin-left:10px">'
                f'{_ai_meta.get("mode","")}</span>'
                f'</div>'
                f'<div style="font-size:0.70rem;color:#64748b;text-align:right">'
                f'{_ai_meta.get("market","")} · '
                f'{_ai_meta.get("themes",0)} themes · '
                f'{_ai_meta.get("sl",0)} shortlisted · '
                f'{_ai_meta.get("bottlenecks",0)} bottlenecks · '
                f'{_ai_meta.get("stocks",0)} stocks · '
                f'Generated {_ai_meta.get("ts","")}'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div style="background:#0f0a1e;border:1px solid #4c1d95;border-radius:8px;'
                'padding:20px 26px;margin-top:2px">',
                unsafe_allow_html=True,
            )
            st.markdown(st.session_state["ai_analysis_result"])
            st.markdown('</div>', unsafe_allow_html=True)

        # ── Data preview (collapsible) ─────────────────────────────────────────
        with st.expander(
            f"📊 Data preview — {len(_ai_all_themes)} themes · "
            f"{len(_ai_bottlenecks)} bottlenecks · "
            f"{len(_ai_sl_themes)} shortlisted",
            expanded=False,
        ):
            _prev_c1, _prev_c2, _prev_c3 = st.columns(3)

            with _prev_c1:
                st.markdown(f"**All Active Themes ({len(_ai_all_themes)})**")
                for _pt in _ai_all_themes[:12]:
                    _pc = CONVICTION_COLOR.get(_pt.get("conviction", "emerging"), "#6366f1")
                    st.markdown(
                        f"- {_pt.get('theme_name','')} "
                        f"`{(_pt.get('conviction') or 'emerging').upper()}` "
                        f"· {float(_pt.get('strength_score') or 0):.0f}pts"
                    )
                if len(_ai_all_themes) > 12:
                    st.caption(f"…and {len(_ai_all_themes) - 12} more themes")

            with _prev_c2:
                st.markdown(f"**Shortlisted (≥2Q, {len(_ai_sl_themes)})**")
                for _pt in _ai_sl_themes[:12]:
                    st.markdown(
                        f"- {_pt.get('theme_name','')} "
                        f"· {int(_pt.get('confirmed_quarters') or 0)}Q "
                        f"· {int(_pt.get('company_count') or 0)} cos"
                    )
                if len(_ai_sl_themes) > 12:
                    st.caption(f"…and {len(_ai_sl_themes) - 12} more")

            with _prev_c3:
                st.markdown(f"**Bottleneck Themes ({len(_ai_bottlenecks)})**")
                if _ai_bottlenecks:
                    for _pt in _ai_bottlenecks[:12]:
                        st.markdown(f"- 🔴 {_pt.get('theme_name','')} · {float(_pt.get('strength_score') or 0):.0f}pts")
                    if len(_ai_bottlenecks) > 12:
                        st.caption(f"…and {len(_ai_bottlenecks) - 12} more")
                else:
                    st.caption("No bottleneck themes detected yet.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 3 — MACRO & POLICY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_macro:
    from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline  # noqa: F811

    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:10px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} Macro & Policy data for <b>{COUNTRY_LABEL}</b> — '
        f'change market in the sidebar ←</div>',
        unsafe_allow_html=True,
    )

    st.markdown("### 🌐 Macro & Policy Intelligence")
    if COUNTRY_CODE == "IN":
        st.caption(
            "India macro context: RBI monetary policy, SEBI circulars, PIB government press releases, "
            "InvestIndia sector reports, Commerce/DGFT trade notices — "
            "enriching themes with India regulatory and policy tailwinds/headwinds."
        )
    else:
        st.caption(
            "Economic series, commodity prices, and government policy events — "
            "enriching themes with real-world constraints and tailwinds."
        )

    # ── Macro fetch controls ──────────────────────────────────────────────────
    if COUNTRY_CODE == "IN":
        st.markdown(
            '<div style="background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;'
            'padding:10px 14px;margin-bottom:12px;font-size:0.80rem;color:#93c5fd">'
            '🇮🇳 <b>India Macro Sources (Stage 6 of run_macro):</b><br>'
            '&nbsp;&nbsp;• <b>RBI</b> — Monetary policy, repo rate, inflation, forex (RSS)<br>'
            '&nbsp;&nbsp;• <b>SEBI</b> — Circulars, press releases (web scraping)<br>'
            '&nbsp;&nbsp;• <b>PIB</b> — Govt press releases: PLI, semiconductor, defence, EV (RSS)<br>'
            '&nbsp;&nbsp;• <b>InvestIndia</b> — Sector reports, investment announcements (web)<br>'
            '&nbsp;&nbsp;• <b>Commerce/DGFT</b> — Trade policy, tariff, export/import notices (web)<br>'
            'Stored in <code>mg_policy_events</code> with <code>country=\'IN\'</code> — '
            'same table as US Congress / Federal Register.</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;'
            'padding:10px 14px;margin-bottom:12px;font-size:0.80rem;color:#93c5fd">'
            '📅 <b>Historical data is fully supported.</b> Set any start/end date from 2000 onward. '
            'FRED has data back to the 1940s; EIA/World Bank from the 1980s–90s; '
            'Congress from 2001; Federal Register from 1994. '
            'Enable <b>ALFRED vintage mode</b> (checkbox below) to fetch the value that '
            'was <i>first published</i> at each date — essential for backtest accuracy.</div>',
            unsafe_allow_html=True,
        )

    mc1, mc2 = st.columns(2)
    with mc1:
        macro_from = st.date_input(
            "Start date (historical)", value=date(2018, 1, 1),
            min_value=date(2000, 1, 1), max_value=date.today(), key="macro_from",
        )
    with mc2:
        macro_to = st.date_input(
            "End date", value=date.today(),
            min_value=date(2000, 1, 1), max_value=date.today(), key="macro_to",
        )

    mopt1, mopt2, mopt3 = st.columns([2, 2, 2])
    with mopt1:
        use_alfred = st.checkbox(
            "ALFRED vintage mode (no look-ahead bias)",
            value=False,
            key="macro_alfred",
            disabled=(COUNTRY_CODE == "IN"),
            help=(
                "ALFRED vintage mode applies to FRED (US only). "
                "Not applicable for India macro sources."
                if COUNTRY_CODE == "IN" else
                "Fetches the version of each FRED data point that was first published on that date. "
                "Required for accurate historical backtesting. Slower — ALFRED returns one vintage per call."
            ),
        )
    with mopt2:
        run_constraint_engine = st.checkbox(
            "Run Constraint Engine after fetch",
            value=True,
            key="macro_constraint",
            help="After fetching data, score all active themes against macro signals automatically.",
        )
    with mopt3:
        st.markdown("<br>", unsafe_allow_html=True)
        _macro_btn_label = (
            "⬇️ Fetch India Macro & Policy Data" if COUNTRY_CODE == "IN"
            else "⬇️ Fetch Macro & Policy Data"
        )
        _macro_btn_help = (
            "Fetches RBI, SEBI, PIB, InvestIndia, Commerce/DGFT policy events for India (country=IN)."
            if COUNTRY_CODE == "IN" else
            "Fetches FRED, EIA, World Bank, Congress, and Federal Register data for the selected date range."
        )
        run_macro_btn = st.button(
            _macro_btn_label, key="btn_run_macro",
            use_container_width=True,
            help=_macro_btn_help,
        )

    if run_macro_btn:
        _spinner_msg = (
            "Fetching India macro & policy data (RBI, SEBI, PIB, InvestIndia, Commerce/DGFT)…"
            if COUNTRY_CODE == "IN"
            else "Fetching macro & policy data… (this may take a few minutes for long date ranges)"
        )
        with st.spinner(_spinner_msg):
            try:
                run_cfg = copy.deepcopy(cfg)
                # Override ALFRED mode from UI checkbox (US only)
                if "fred" not in run_cfg:
                    run_cfg["fred"] = {}
                run_cfg["fred"]["use_alfred"] = use_alfred
                with IntelligencePipeline(run_cfg) as pip:
                    pip._init_storage()
                    pip._init_macro()
                    macro_stats = pip.run_macro(
                        start_date=str(macro_from),
                        end_date=str(macro_to),
                    )
                # Skip constraint engine if unchecked
                if not run_constraint_engine:
                    macro_stats["themes_constraint_scored"] = "skipped"
                alfred_note = " (ALFRED vintage)" if use_alfred else ""
                if COUNTRY_CODE == "IN":
                    st.success(
                        f"✅ India macro fetch complete — "
                        f"🇮🇳 Policy events (RBI/SEBI/PIB/InvestIndia/Commerce): "
                        f"{macro_stats.get('india_macro_events', 0)} events stored | "
                        f"Themes scored: {macro_stats.get('themes_constraint_scored', 0)}"
                    )
                else:
                    st.success(
                        f"✅ Macro fetch complete{alfred_note} — "
                        f"FRED: {macro_stats.get('fred_rows',0)} rows | "
                        f"EIA: {macro_stats.get('eia_rows',0)} rows | "
                        f"World Bank: {macro_stats.get('world_bank_rows',0)} rows | "
                        f"Congress: {macro_stats.get('congress_events',0)} events | "
                        f"Fed Register: {macro_stats.get('federal_register_events',0)} events | "
                        f"Themes scored: {macro_stats.get('themes_constraint_scored',0)}"
                    )
            except Exception as ex:
                st.error(f"Macro fetch failed: {ex}")

    st.markdown("---")

    # ── Helper: load macro data from PostgreSQL ───────────────────────────────
    @st.cache_data(ttl=120, show_spinner=False)
    def load_macro_series_history(series_id: str, start_d: str, end_d: str):
        try:
            from makrograph.macro.macro_store import MacroStore
            pg_cfg = cfg.get("postgresql", {})
            with MacroStore(pg_cfg) as ms:
                return ms.get_series_history(
                    series_id,
                    start_date=date.fromisoformat(start_d),
                    end_date=date.fromisoformat(end_d),
                )
        except Exception:
            return []

    @st.cache_data(ttl=120, show_spinner=False)
    def load_commodity_history(commodity_id: str, start_d: str, end_d: str):
        try:
            from makrograph.macro.macro_store import MacroStore
            pg_cfg = cfg.get("postgresql", {})
            with MacroStore(pg_cfg) as ms:
                return ms.get_commodity_history(
                    commodity_id,
                    start_date=date.fromisoformat(start_d),
                    end_date=date.fromisoformat(end_d),
                )
        except Exception:
            return []

    @st.cache_data(ttl=120, show_spinner=False)
    def load_policy_events(as_of_d: str, sector_filter: list):
        try:
            from makrograph.macro.macro_store import MacroStore
            pg_cfg = cfg.get("postgresql", {})
            with MacroStore(pg_cfg) as ms:
                return ms.get_recent_policy_events(
                    as_of_date=date.fromisoformat(as_of_d),
                    sectors=sector_filter or None,
                    limit=60,
                )
        except Exception:
            return []

    @st.cache_data(ttl=120, show_spinner=False)
    def load_macro_events_new(as_of_d: str, since_days_val: int):
        try:
            from makrograph.macro.macro_store import MacroStore
            pg_cfg = cfg.get("postgresql", {})
            with MacroStore(pg_cfg) as ms:
                return ms.get_macro_events(
                    as_of_date=date.fromisoformat(as_of_d),
                    since_days=since_days_val,
                )
        except Exception:
            return []

    # ── Economic Series Charts ────────────────────────────────────────────────
    st.markdown("#### 📈 Economic Series")

    SERIES_MENU = {
        "GDP": "US Real GDP",
        "CPIAUCSL": "CPI (All Urban)",
        "CPILFESL": "Core CPI (ex Food & Energy)",
        "UNRATE": "Unemployment Rate",
        "DGS10": "10Y Treasury Yield",
        "DGS2": "2Y Treasury Yield",
        "T10Y2Y": "10Y-2Y Yield Spread",
        "FEDFUNDS": "Fed Funds Rate",
        "INDPRO": "Industrial Production",
        "M2SL": "M2 Money Supply",
        "BAMLH0A0HYM2": "HY Credit Spread",
        "DCOILWTICO": "WTI Crude Oil (FRED)",
        "DHHNGSP": "Henry Hub Gas (FRED)",
    }
    econ_col1, econ_col2 = st.columns(2)
    with econ_col1:
        selected_series = st.selectbox(
            "Select series", list(SERIES_MENU.keys()),
            format_func=lambda k: f"{k} — {SERIES_MENU[k]}",
            key="macro_series_select",
        )
    with econ_col2:
        macro_chart_years = st.slider("Years of history", 1, 10, 5, key="macro_years")

    series_start = str(date(date.today().year - macro_chart_years, 1, 1))
    series_data = load_macro_series_history(selected_series, series_start, str(macro_to))

    if series_data:
        import pandas as _pd
        import plotly.graph_objects as _go
        df_s = _pd.DataFrame(series_data)
        df_s["observation_date"] = _pd.to_datetime(df_s["observation_date"])
        df_s = df_s.sort_values("observation_date")

        _fig_s = _go.Figure()
        _fig_s.add_trace(_go.Scatter(
            x=df_s["observation_date"], y=df_s["value"],
            mode="lines", name=selected_series,
            line=dict(color="#818cf8", width=2),
            fill="tozeroy", fillcolor="rgba(129,140,248,0.08)",
        ))
        # Threshold reference lines
        _THRESH_REFS = {"DGS10": 4.5, "T10Y2Y": 0.0, "CPIAUCSL": 5.0,
                        "FEDFUNDS": 5.0, "UNRATE": 6.0}
        if selected_series in _THRESH_REFS:
            _ref = _THRESH_REFS[selected_series]
            _fig_s.add_shape(
                type="line", x0=df_s["observation_date"].min(), x1=df_s["observation_date"].max(),
                y0=_ref, y1=_ref,
                line=dict(color="#ef4444", width=1.5, dash="dot"),
            )
            _fig_s.add_annotation(
                x=df_s["observation_date"].max(), y=_ref,
                text=f" Threshold {_ref}", showarrow=False,
                xanchor="right", font=dict(color="#ef4444", size=11),
            )

        _units = series_data[0].get("units", "") if series_data else ""
        _fig_s.update_layout(
            title=dict(text=f"{selected_series} — {SERIES_MENU.get(selected_series,'')}",
                       font=dict(size=14, color="#e2e8f0")),
            paper_bgcolor="#172033", plot_bgcolor="#172033",
            font=dict(color="#94a3b8"),
            yaxis_title=_units, xaxis_title="",
            height=320, margin=dict(l=0, r=0, t=40, b=20),
            hovermode="x unified",
        )
        _fig_s.update_xaxes(gridcolor="#1e293b")
        _fig_s.update_yaxes(gridcolor="#1e293b")
        st.plotly_chart(_fig_s, use_container_width=True)
    else:
        st.info(
            f"No data for {selected_series}. Fetch macro data first (button above) "
            "and ensure FRED_API_KEY is set."
        )

    st.markdown("---")

    # ── Commodity Charts ──────────────────────────────────────────────────────
    st.markdown("#### 🛢️ Commodity Prices")

    COMMODITY_MENU = {
        "WTI_CRUDE":      "WTI Crude Oil (USD/bbl)",
        "BRENT_CRUDE":    "Brent Crude Oil (USD/bbl)",
        "HENRY_HUB":      "Henry Hub Gas (USD/MMBtu)",
        "US_CRUDE_INVENTORY": "US Crude Inventory (Mn Bbls)",
        "REFINERY_UTIL":  "US Refinery Utilization (%)",
        "ELEC_RETAIL_US": "US Electricity Retail Price (¢/kWh)",
        "COPPER":         "Copper (USD/MT)",
        "LITHIUM":        "Lithium Carbonate (USD/MT)",
        "BALTIC_DRY":     "Baltic Dry Index",
    }
    comm_col1, comm_col2 = st.columns(2)
    with comm_col1:
        selected_comm = st.selectbox(
            "Select commodity", list(COMMODITY_MENU.keys()),
            format_func=lambda k: COMMODITY_MENU[k],
            key="macro_comm_select",
        )

    comm_data = load_commodity_history(selected_comm, series_start, str(macro_to))

    if comm_data:
        import pandas as _pd2
        import plotly.graph_objects as _go2
        df_c = _pd2.DataFrame(comm_data)
        df_c["observation_date"] = _pd2.to_datetime(df_c["observation_date"])
        df_c = df_c.sort_values("observation_date")

        _COMM_THRESH = {
            "WTI_CRUDE": 100.0, "BRENT_CRUDE": 100.0,
            "HENRY_HUB": 5.0, "COPPER": 10000.0,
            "LITHIUM": 50000.0, "BALTIC_DRY": 3000.0,
        }
        _fig_c = _go2.Figure()
        _fig_c.add_trace(_go2.Scatter(
            x=df_c["observation_date"], y=df_c["value"],
            mode="lines", name=selected_comm,
            line=dict(color="#f59e0b", width=2),
            fill="tozeroy", fillcolor="rgba(245,158,11,0.08)",
        ))
        if selected_comm in _COMM_THRESH:
            _ref_c = _COMM_THRESH[selected_comm]
            _fig_c.add_shape(
                type="line",
                x0=df_c["observation_date"].min(), x1=df_c["observation_date"].max(),
                y0=_ref_c, y1=_ref_c,
                line=dict(color="#ef4444", width=1.5, dash="dot"),
            )
            _fig_c.add_annotation(
                x=df_c["observation_date"].max(), y=_ref_c,
                text=f" Constraint threshold", showarrow=False,
                xanchor="right", font=dict(color="#ef4444", size=11),
            )
        _units_c = COMMODITY_MENU.get(selected_comm, "")
        _fig_c.update_layout(
            title=dict(text=_units_c, font=dict(size=14, color="#e2e8f0")),
            paper_bgcolor="#172033", plot_bgcolor="#172033",
            font=dict(color="#94a3b8"),
            height=300, margin=dict(l=0, r=0, t=40, b=20),
            hovermode="x unified",
        )
        _fig_c.update_xaxes(gridcolor="#1e293b")
        _fig_c.update_yaxes(gridcolor="#1e293b")
        st.plotly_chart(_fig_c, use_container_width=True)
    else:
        st.info("No commodity data yet. Fetch macro data first (requires EIA_API_KEY).")

    st.markdown("---")

    # ── Macro Threshold Events ────────────────────────────────────────────────
    st.markdown("#### ⚠️ Macro Threshold Events")
    st.caption("Automatically triggered when key economic series cross critical levels.")

    mev_window = st.slider("Look-back (days)", 90, 1825, 365, step=90, key="mev_window")
    mev_events = load_macro_events_new(str(macro_to), mev_window)

    SEVERITY_COLOR = {
        range(0, 40):   "#64748b",
        range(40, 65):  "#f59e0b",
        range(65, 80):  "#f97316",
        range(80, 101): "#ef4444",
    }
    def _severity_color(sev: float) -> str:
        s = int(sev)
        for r, c in SEVERITY_COLOR.items():
            if s in r:
                return c
        return "#94a3b8"

    if mev_events:
        for mev in mev_events[:20]:
            sev = mev.get("severity", 0)
            col = _severity_color(sev)
            at_risk = ", ".join(mev.get("sectors_at_risk") or []) or "—"
            benefit = ", ".join(mev.get("sectors_benefit") or []) or "—"
            st.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;'
                f'border-left:3px solid {col};border-radius:8px;padding:10px 14px;margin-bottom:5px">'
                f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">'
                f'<span style="font-weight:700;color:#ffffff;font-size:0.84rem">'
                f'{mev.get("event_type","").replace("_"," ").upper()}</span>'
                f'<span style="font-size:0.72rem;color:#94a3b8">'
                f'{mev.get("event_date","")} &nbsp;|&nbsp; severity: '
                f'<span style="color:{col}">{sev:.0f}/100</span></span>'
                f'</div>'
                f'<div style="color:#cbd5e1;font-size:0.78rem;margin-top:4px">'
                f'{mev.get("description","")}</div>'
                f'<div style="font-size:0.73rem;margin-top:5px">'
                f'⚠️ At-risk sectors: <span style="color:#fca5a5">{at_risk}</span> &nbsp;|&nbsp; '
                f'✅ Benefiting sectors: <span style="color:#86efac">{benefit}</span>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
    else:
        st.info(
            "No macro threshold events yet. Fetch macro data first, then the Constraint Engine "
            "will automatically detect threshold crossings."
        )

    st.markdown("---")

    # ── Policy Events ─────────────────────────────────────────────────────────
    st.markdown("#### 🏛️ Policy Events (Congress + Federal Register)")
    st.caption("Bills, regulations, and executive orders affecting sectors and technologies.")

    SECTORS_LIST = [
        "Energy", "Technology", "Healthcare", "Industrials", "Financials",
        "Agriculture", "Materials", "Utilities", "Defense",
    ]
    pol_col1, pol_col2 = st.columns([3, 1])
    with pol_col1:
        pol_sector_filter = st.multiselect(
            "Filter by sector", SECTORS_LIST, default=[], key="pol_sector_filter",
        )
    with pol_col2:
        pol_direction_filter = st.selectbox(
            "Impact", ["All", "positive", "negative", "neutral", "mixed"],
            key="pol_direction_filter",
        )

    policy_events_data = load_policy_events(str(macro_to), pol_sector_filter)
    if pol_direction_filter != "All":
        policy_events_data = [
            p for p in policy_events_data
            if p.get("impact_direction") == pol_direction_filter
        ]

    POL_DIR_COLOR = {
        "positive": "#22c55e", "negative": "#ef4444",
        "neutral": "#94a3b8", "mixed": "#f59e0b",
    }
    POL_TYPE_ICON = {
        "bill": "📜", "rule": "⚖️", "executive_order": "🖊️",
        "notice": "📢", "resolution": "🗳️",
    }

    if policy_events_data:
        for pe in policy_events_data[:30]:
            dir_col = POL_DIR_COLOR.get(pe.get("impact_direction", "neutral"), "#94a3b8")
            icon = POL_TYPE_ICON.get(pe.get("policy_type", "notice"), "📄")
            mag = pe.get("impact_magnitude", 0)
            sectors_str = ", ".join(pe.get("sectors_affected") or []) or "—"
            techs_str = ", ".join(pe.get("technologies_affected") or []) or "—"
            enacted = pe.get("enacted_date") or pe.get("introduced_date") or "?"
            st.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;'
                f'border-left:3px solid {dir_col};border-radius:8px;padding:9px 14px;margin-bottom:5px">'
                f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">'
                f'<span style="font-weight:700;color:#ffffff;font-size:0.83rem">'
                f'{icon} {pe.get("title","")[:140]}</span>'
                f'<span style="font-size:0.71rem;color:#94a3b8">'
                f'{pe.get("source","").replace("_"," ").title()} &nbsp;|&nbsp; {enacted}</span>'
                f'</div>'
                f'<div style="font-size:0.73rem;margin-top:5px;color:#94a3b8">'
                f'Impact: <span style="color:{dir_col}">{pe.get("impact_direction","neutral")}</span>'
                f' ({mag:.0f}/100) &nbsp;|&nbsp; '
                f'Status: {pe.get("status","")}</div>'
                f'<div style="font-size:0.73rem;margin-top:3px">'
                f'Sectors: <span style="color:#818cf8">{sectors_str}</span> &nbsp;|&nbsp; '
                f'Technologies: <span style="color:#34d399">{techs_str}</span>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
    else:
        st.info(
            "No policy events yet. Fetch macro data first. "
            "Congress API requires CONGRESS_API_KEY; Federal Register is free."
        )

    st.markdown("---")

    # ── API Key Status ────────────────────────────────────────────────────────
    st.markdown("#### 🔑 API Key Status")
    import os as _os
    key_status = [
        ("FRED_API_KEY",     "FRED (economic series)",      "https://fred.stlouisfed.org/docs/api/fred/"),
        ("EIA_API_KEY",      "EIA (energy data)",           "https://www.eia.gov/opendata/"),
        ("CONGRESS_API_KEY", "Congress.gov (legislation)",  "https://api.congress.gov/sign-up/"),
    ]
    for env_var, label, signup_url in key_status:
        _cfg_key = env_var.lower().replace("_api_key", "")   # e.g. "fred", "eia", "congress"
        has_key = bool(_os.environ.get(env_var) or cfg.get(_cfg_key, {}).get("api_key", ""))
        status_icon = "✅" if has_key else "❌"
        status_text = "configured" if has_key else f"missing — get free key at {signup_url}"
        st.markdown(
            f'<div style="font-size:0.80rem;color:{"#86efac" if has_key else "#fca5a5"};'
            f'padding:3px 0">{status_icon} <b>{label}</b>: {status_text}</div>',
            unsafe_allow_html=True,
        )
    st.markdown(
        '<div style="font-size:0.80rem;color:#86efac;padding:3px 0">'
        '✅ <b>Federal Register</b>: free, no API key required</div>'
        '<div style="font-size:0.80rem;color:#86efac;padding:3px 0">'
        '✅ <b>World Bank</b>: free, no API key required</div>',
        unsafe_allow_html=True,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 5 — COMPANY EXPLORER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@st.cache_data(ttl=60, show_spinner=False)
def load_company_search(query: str, country: str):
    if not pg or not query:
        return []
    try:
        return pg.search_companies(query, country=country, limit=20)
    except Exception:
        return []

@st.cache_data(ttl=60, show_spinner=False)
def load_company_profile(ticker: str, country: str, as_of_d: str):
    if not pg:
        return {}
    try:
        return pg.get_company_profile(ticker, country=country,
                                      as_of_date=date.fromisoformat(as_of_d))
    except Exception:
        return {}

@st.cache_data(ttl=60, show_spinner=False)
def load_company_timeline(ticker: str, country: str, from_d: str, to_d: str):
    if not pg:
        return []
    try:
        return pg.get_company_signal_timeline(
            ticker, country=country,
            from_date=date.fromisoformat(from_d),
            to_date=date.fromisoformat(to_d),
        )
    except Exception:
        return []

@st.cache_data(ttl=60, show_spinner=False)
def load_company_themes(ticker: str, country: str, as_of_d: str):
    if not pg:
        return []
    try:
        return pg.get_company_themes(ticker, country=country,
                                     as_of_date=date.fromisoformat(as_of_d))
    except Exception:
        return []


with tab_company:
    import pandas as _pd_co
    import plotly.graph_objects as _go_co

    # Country banner
    st.markdown(
        f'<div style="background:#1e1b6b;border-left:3px solid #818cf8;border-radius:6px;'
        f'padding:6px 14px;margin-bottom:12px;font-size:0.80rem;color:#c7d2fe">'
        f'{COUNTRY_FLAG} Exploring <b>{COUNTRY_LABEL}</b> companies — '
        f'change market in the sidebar ←</div>',
        unsafe_allow_html=True,
    )

    # ── Search controls ──────────────────────────────────────────────────────
    ex1, ex2, ex3 = st.columns([3, 2, 2])
    with ex1:
        co_search = st.text_input(
            "Search ticker or company name",
            placeholder="e.g. NVDA, Microsoft, AMD",
            key="co_search",
        )
    with ex2:
        co_from = st.date_input("From", value=date(2020, 1, 1),
                                 min_value=date(2000, 1, 1), max_value=date.today(),
                                 key="co_from")
    with ex3:
        co_to = st.date_input("To / As-of", value=date.today(),
                               min_value=date(2000, 1, 1), max_value=date.today(),
                               key="co_to")

    if COUNTRY_CODE == "IN":
        st.info("🇮🇳 India company data will be available once the NSE/BSE pipeline is added.")
    else:
        # ── Search results ───────────────────────────────────────────────────
        results = load_company_search(co_search.strip(), COUNTRY_CODE) if co_search.strip() else []

        if co_search.strip() and not results:
            st.warning(f"No companies found matching '{co_search}'. Try a shorter ticker or partial name.")

        # Active ticker: pick from search results or let user confirm
        active_ticker = None
        if results:
            st.markdown(
                f'<div style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">'
                f'{len(results)} match{"es" if len(results)!=1 else ""} — select one:</div>',
                unsafe_allow_html=True,
            )
            _res_options = {
                f"{r['ticker']} — {r.get('company','')[:50]} ({r.get('filing_count',0)} filings)": r["ticker"]
                for r in results
            }
            _chosen_label = st.selectbox(
                "Select company", list(_res_options.keys()),
                key="co_select", label_visibility="collapsed",
            )
            active_ticker = _res_options[_chosen_label]
        elif not co_search.strip():
            st.markdown(
                '<div style="background:#1e293b;border:1px dashed #334155;border-radius:10px;'
                'padding:30px;text-align:center;color:#475569;font-size:0.85rem">'
                '🔍 Type a ticker (NVDA) or company name above to explore a company.</div>',
                unsafe_allow_html=True,
            )

        if active_ticker:
            profile = load_company_profile(active_ticker, COUNTRY_CODE, str(co_to))
            timeline = load_company_timeline(active_ticker, COUNTRY_CODE, str(co_from), str(co_to))
            co_themes = load_company_themes(active_ticker, COUNTRY_CODE, str(co_to))

            # ── Company header card ──────────────────────────────────────────
            company_name = profile.get("company", active_ticker)
            st.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;border-radius:12px;'
                f'padding:14px 18px;margin-bottom:14px">'
                f'<div style="font-size:1.2rem;font-weight:800;color:#ffffff">'
                f'{COUNTRY_FLAG} {active_ticker} &nbsp;—&nbsp; {company_name}</div>'
                f'<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:8px">'
                f'<div class="metric-item"><span class="metric-label">Country</span>'
                f'<span class="metric-value" style="font-size:0.9rem">'
                f'{profile.get("country","US")}</span></div>'
                f'<div class="metric-item"><span class="metric-label">Filings</span>'
                f'<span class="metric-value">{profile.get("filing_count",0)}</span></div>'
                f'<div class="metric-item"><span class="metric-label">Total Signals</span>'
                f'<span class="metric-value">{profile.get("total_signals",0)}</span></div>'
                f'<div class="metric-item"><span class="metric-label">Avg Confidence</span>'
                f'<span class="metric-value" style="font-size:0.9rem">'
                f'{float(profile.get("avg_confidence") or 0):.3f}</span></div>'
                f'<div class="metric-item"><span class="metric-label">First Filing</span>'
                f'<span class="metric-value" style="font-size:0.9rem">'
                f'{str(profile.get("first_filing","—"))[:10]}</span></div>'
                f'<div class="metric-item"><span class="metric-label">Last Filing</span>'
                f'<span class="metric-value" style="font-size:0.9rem">'
                f'{str(profile.get("last_filing","—"))[:10]}</span></div>'
                f'<div class="metric-item"><span class="metric-label">Filing Types</span>'
                f'<span class="metric-value" style="font-size:0.8rem">'
                f'{", ".join(profile.get("filing_types") or [])}</span></div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

            co_tab_tl, co_tab_themes, co_tab_docs = st.tabs([
                "📈 Signal Timeline", "🗺️ Theme Contributions", "📄 Recent Filings"
            ])

            # ── Signal timeline chart ────────────────────────────────────────
            with co_tab_tl:
                if timeline:
                    df_tl = _pd_co.DataFrame(timeline)
                    df_tl["month"] = _pd_co.to_datetime(df_tl["month"])
                    df_tl = df_tl.sort_values("month")

                    fig_tl = _go_co.Figure()
                    fig_tl.add_trace(_go_co.Bar(
                        x=df_tl["month"], y=df_tl["signals"],
                        name="Signals",
                        marker_color="#818cf8",
                    ))
                    fig_tl.add_trace(_go_co.Scatter(
                        x=df_tl["month"], y=df_tl["filings"],
                        name="Filings", yaxis="y2",
                        line=dict(color="#f59e0b", width=2),
                        mode="lines+markers",
                    ))
                    fig_tl.update_layout(
                        height=300,
                        paper_bgcolor="#172033", plot_bgcolor="#172033",
                        font=dict(color="#94a3b8"),
                        margin=dict(l=0, r=0, t=10, b=20),
                        hovermode="x unified",
                        legend=dict(orientation="h", y=1.12, x=0,
                                    font=dict(color="#94a3b8", size=11)),
                        yaxis=dict(title="Signals", gridcolor="#1e293b",
                                   color="#94a3b8"),
                        yaxis2=dict(title="Filings", overlaying="y", side="right",
                                    color="#f59e0b", showgrid=False),
                        xaxis=dict(gridcolor="#1e293b", color="#94a3b8"),
                    )
                    st.plotly_chart(fig_tl, use_container_width=True,
                                    config={"displayModeBar": False})

                    # Monthly detail table
                    st.markdown("##### Monthly breakdown")
                    df_tl_disp = df_tl[["month", "filings", "signals", "avg_confidence"]].copy()
                    df_tl_disp["month"] = df_tl_disp["month"].dt.strftime("%Y-%m")
                    df_tl_disp.columns = ["Month", "Filings", "Signals", "Avg Conf"]
                    df_tl_disp = df_tl_disp.sort_values("Month", ascending=False)
                    st.dataframe(df_tl_disp, use_container_width=True, hide_index=True,
                                 height=min(50 + len(df_tl_disp) * 35, 380))
                else:
                    st.info("No timeline data. Ingest and run NLP on filings for this company.")

            # ── Theme contributions ──────────────────────────────────────────
            with co_tab_themes:
                st.markdown(
                    f'<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px">'
                    f'Themes that <b>{active_ticker}</b>\'s filings contributed signals to '
                    f'(as of {co_to}). These are themes where this company\'s documents '
                    f'were the <i>source</i>.</div>',
                    unsafe_allow_html=True,
                )
                if co_themes:
                    for ct in co_themes:
                        conv = ct.get("conviction","emerging")
                        tc = CONVICTION_COLOR.get(conv, "#6366f1")
                        st.markdown(
                            f'<div style="background:#1e293b;border:1px solid #334155;'
                            f'border-radius:8px;padding:9px 14px;margin-bottom:5px;'
                            f'display:flex;justify-content:space-between;align-items:center;'
                            f'flex-wrap:wrap;gap:8px">'
                            f'<div>'
                            f'<div style="font-weight:700;color:#fff;font-size:0.88rem">'
                            f'{ct["theme_name"]}</div>'
                            f'<div style="font-size:0.70rem;color:#475569">'
                            f'{ct["theme_slug"]} · first detected: '
                            f'{str(ct.get("first_detected",""))[:10]}</div>'
                            f'</div>'
                            f'<div style="text-align:right;min-width:100px">'
                            f'<div style="color:#818cf8;font-weight:700;font-size:0.9rem">'
                            f'{int(ct.get("company_signal_count",0))} signals</div>'
                            f'<div style="margin-top:3px">{_badge(conv, tc)}</div>'
                            f'<div style="font-size:0.70rem;color:#94a3b8">'
                            f'strength: {float(ct.get("strength_score") or 0):.1f}</div>'
                            f'</div></div>',
                            unsafe_allow_html=True,
                        )
                    st.caption(
                        f"{len(co_themes)} themes sourced from {active_ticker} filings as of {co_to}. "
                        "Note: theme matching uses entity text — run NLP stage first."
                    )
                else:
                    st.info(
                        "No themes linked yet. Run NLP + Themes stages and ensure this "
                        "company has processed documents."
                    )

            # ── Recent filings ───────────────────────────────────────────────
            with co_tab_docs:
                recent_filings = load_concall_docs(
                    COUNTRY_CODE, str(co_from), str(co_to),
                    active_ticker, "All", 50,
                )
                if recent_filings:
                    df_rf = _pd_co.DataFrame([
                        {
                            "Date":     str(d.get("filed_at",""))[:10],
                            "Type":     d.get("filing_type","—"),
                            "Period":   d.get("fiscal_period","—"),
                            "Words":    int(d.get("word_count") or 0),
                            "Signals":  int(d.get("signal_count") or 0),
                            "Status":   d.get("processing_status","—"),
                        }
                        for d in recent_filings
                    ])
                    st.dataframe(
                        df_rf, use_container_width=True, hide_index=True,
                        height=min(50 + len(df_rf) * 35, 400),
                        column_config={
                            "Signals": st.column_config.ProgressColumn(
                                "Signals", min_value=0,
                                max_value=max(int(d.get("signal_count") or 1) for d in recent_filings),
                                format="%d",
                            ),
                        },
                    )
                else:
                    st.info("No filings found for this ticker in the selected date range.")
