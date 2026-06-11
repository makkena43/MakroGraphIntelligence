"""
India Theme Engine — Clean, holistic approach.

ARCHITECTURE INSIGHT:
---------------------
India signals (mg_signals) attach entity_id → COMPANY entities.
US signals attach entity_id → TECHNOLOGY/SECTOR entities.

This means the US pipeline's entity-cluster approach (find tech entities
with signals across many companies) doesn't work for India.

For India the correct flow is:

  Step 1: Find COMPANIES with accelerating investment signals
          (capex_increase, demand_surge, regulatory_tailwind)
          Recent 90d velocity vs prior 90d.

  Step 2: For those accelerating companies, look at which
          TECHNOLOGY/SECTOR entities appear in their documents.
          These entities ARE the themes.

  Step 3: Aggregate at theme level — how many accelerating
          companies share a theme entity? Signal velocity?

  Step 4: For each emerging theme, score each company by:
          - How many of its investment signals co-occur with this theme entity
          - Recency of those signals
          - What % of its docs mention this entity (concentration)

OUTPUT:
  Themes ranked by explosion potential (velocity × breadth × quality)
  Each theme has a company list ranked by supply strength (not stock ranking)
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Signal types that represent genuine investment commitment
INVESTMENT_SIGNALS = frozenset({
    "capex_increase", "demand_surge", "regulatory_tailwind",
    "technology_adoption", "market_entry", "hiring_surge",
})

# Signal types that represent supply constraint (highest signal quality)
CONSTRAINT_SIGNALS = frozenset({
    "supply_bottleneck", "inventory_drawdown",
})

# Noise entity names that slip through type filtering
_NOISE_NAMES = frozenset({
    "materials", "power", "energy", "financials", "year", "this quarter",
    "last year", "standards", "rules", "codes", "standalone", "consolidated",
    "group", "indian", "india", "financial results", "the financial results",
    "shareholders", "auditor", "auditors", "report", "conclude",
})


@dataclass
class ThemeCompany:
    company: str
    ticker: str
    inv_signals_recent: int       # investment signals in last 90d (in docs mentioning theme)
    inv_signals_total: int        # investment signals in full window
    docs_with_theme: int          # docs mentioning theme entity
    total_docs: int               # total company docs in window
    concentration: float          # docs_with_theme / total_docs
    recency_ratio: float          # recent_signals / total_signals
    strength: float               # composite strength score


@dataclass
class EmergingTheme:
    entity: str                   # theme entity name (e.g. "Solar", "Data Center")
    entity_type: str
    n_companies: int              # distinct companies with this theme
    recent_signals: int           # investment signals last 90d across all companies
    prior_signals: int            # investment signals prior 90d
    velocity_pct: float           # signal acceleration %
    avg_concentration: float      # avg % of company docs mentioning this entity
    theme_score: float            # composite explosion score
    companies: list[ThemeCompany] = field(default_factory=list)


def detect_exploding_themes(
    pg_store,
    country: str = "IN",
    window_days: int = 730,
    recent_days: int = 180,
    min_companies: int = 3,
    min_recent_signals: int = 5,
    as_of_date: Optional[date] = None,
) -> list[EmergingTheme]:
    """
    Detect themes with accelerating investment signals in the India market.

    Uses Year-over-Year comparison (recent 180d vs same 180d one year ago)
    to remove India's seasonal filing-season distortion (Q4 results bulk
    in April-May cause naive recent-vs-prior comparisons to look negative).

    Returns themes sorted by explosion potential — themes where multiple
    companies are showing simultaneous capex + demand acceleration focused
    on the same sector/technology entity.
    """
    _as_of = as_of_date or date.today()
    _window_start = _as_of - timedelta(days=window_days)
    # YoY: compare last `recent_days` vs same window one year ago
    _recent_start = _as_of - timedelta(days=recent_days)
    _prior_start  = _as_of - timedelta(days=recent_days + 365)
    _prior_end    = _as_of - timedelta(days=365)

    sql = """
    -- ── Step 1: Companies with accelerating investment signals ──────────────
    WITH company_signals AS (
        SELECT
            COALESCE(NULLIF(d.company, ''), d.ticker)          AS company,
            d.ticker,
            s.document_id,
            d.filed_at,
            -- Recent: last `recent_days` days
            CASE WHEN s.signal_type = ANY(%(inv_types)s)
                 AND d.filed_at >= %(recent_start)s             THEN 1 ELSE 0 END  AS is_recent_inv,
            -- Prior: same window length, one year ago (YoY — removes filing-season bias)
            CASE WHEN s.signal_type = ANY(%(inv_types)s)
                 AND d.filed_at BETWEEN %(prior_start)s
                                    AND %(prior_end)s           THEN 1 ELSE 0 END AS is_prior_inv,
            CASE WHEN s.signal_type = ANY(%(inv_types)s)       THEN 1 ELSE 0 END  AS is_inv,
            CASE WHEN s.signal_type = ANY(%(con_types)s)       THEN 1 ELSE 0 END  AS is_constraint
        FROM mg_signals s
        JOIN mg_documents d ON d.id = s.document_id
        WHERE d.country   = %(country)s
          AND d.filed_at  BETWEEN %(window_start)s AND %(as_of)s
          AND COALESCE(NULLIF(d.company, ''), d.ticker) IS NOT NULL
    ),
    company_agg AS (
        SELECT
            company, ticker,
            SUM(is_recent_inv)   AS recent_inv,
            SUM(is_prior_inv)    AS prior_inv,
            SUM(is_inv)          AS total_inv,
            SUM(is_constraint)   AS total_constraint,
            COUNT(DISTINCT document_id) AS total_docs
        FROM company_signals
        GROUP BY company, ticker
        HAVING SUM(is_inv) >= 3          -- at least 3 investment signals in window
    ),
    -- Keep only companies that have RECENT investment activity
    active_companies AS (
        SELECT * FROM company_agg
        WHERE recent_inv >= 1
    ),

    -- ── Step 2: Theme entities in those companies' documents ────────────────
    company_theme_docs AS (
        SELECT
            ac.company, ac.ticker,
            ac.recent_inv, ac.prior_inv, ac.total_inv, ac.total_docs,
            e.canonical_name   AS entity,
            e.entity_type,
            COUNT(DISTINCT de.document_id)  AS docs_with_theme,
            -- Investment signals in documents that also mention this entity
            COUNT(DISTINCT cs.document_id) FILTER (
                WHERE cs.is_recent_inv = 1
            )                              AS theme_recent_inv,
            COUNT(DISTINCT cs.document_id) FILTER (
                WHERE cs.is_inv = 1
            )                              AS theme_total_inv
        FROM active_companies ac
        JOIN mg_documents d ON COALESCE(NULLIF(d.company, ''), d.ticker) = ac.company
            AND d.country  = %(country)s
            AND d.filed_at BETWEEN %(window_start)s AND %(as_of)s
        JOIN mg_document_entities de ON de.document_id = d.id
        JOIN mg_entities e ON e.id = de.entity_id
            AND e.entity_type IN ('TECHNOLOGY', 'SECTOR')
            AND length(e.canonical_name) >= 4
            AND e.canonical_name ~ '^[A-Za-z]'
            -- Exclude obvious noise at SQL level
            AND lower(e.canonical_name) NOT IN (
                'materials','power','energy','financials','year','standards',
                'rules','codes','standalone','consolidated','group','indian',
                'india','financial results','shareholders','report'
            )
        JOIN company_signals cs ON cs.document_id = d.id
            AND cs.company = ac.company
        GROUP BY ac.company, ac.ticker,
                 ac.recent_inv, ac.prior_inv, ac.total_inv, ac.total_docs,
                 e.canonical_name, e.entity_type
        HAVING COUNT(DISTINCT de.document_id) >= 2   -- theme mentioned in 2+ docs
    ),

    -- ── Step 3: Theme-level aggregation ─────────────────────────────────────
    theme_agg AS (
        SELECT
            entity, entity_type,
            COUNT(DISTINCT company)           AS n_companies,
            SUM(theme_recent_inv)             AS recent_signals,
            SUM(prior_inv)                    AS prior_signals,
            AVG(docs_with_theme::float
                / NULLIF(total_docs, 0))      AS avg_concentration,
            -- Collect company-level data as JSON for Python to unpack
            jsonb_agg(jsonb_build_object(
                'company',      company,
                'ticker',       ticker,
                'recent_inv',   theme_recent_inv,
                'total_inv',    theme_total_inv,
                'docs_theme',   docs_with_theme,
                'total_docs',   total_docs,
                'conc',         docs_with_theme::float / NULLIF(total_docs, 0),
                'rec_ratio',    CASE WHEN total_inv > 0
                                     THEN theme_recent_inv::float / total_inv
                                     ELSE 0 END
            )) AS company_data
        FROM company_theme_docs
        GROUP BY entity, entity_type
        HAVING COUNT(DISTINCT company) >= %(min_companies)s
           AND SUM(theme_recent_inv)  >= %(min_recent)s
    )

    SELECT
        entity, entity_type, n_companies,
        recent_signals, prior_signals, avg_concentration,
        company_data,
        -- Velocity: how much faster are signals coming in recently vs prior period?
        CASE WHEN prior_signals = 0 THEN 200.0
             ELSE ROUND(((recent_signals::float / NULLIF(prior_signals, 0)) - 1) * 100)::numeric
        END AS velocity_pct
    FROM theme_agg
    WHERE recent_signals >= %(min_recent)s
    ORDER BY velocity_pct DESC, n_companies DESC
    """

    import json as _json
    from psycopg2.extras import RealDictCursor

    inv_types = list(INVESTMENT_SIGNALS)
    con_types = list(CONSTRAINT_SIGNALS)

    with pg_store._conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, {
                "country":      country,
                "window_start": _window_start,
                "as_of":        _as_of,
                "recent_start": _recent_start,
                "prior_start":  _prior_start,
                "prior_end":    _prior_end,
                "inv_types":    inv_types,
                "con_types":    con_types,
                "min_companies": min_companies,
                "min_recent":    min_recent_signals,
            })
            rows = [dict(r) for r in cur.fetchall()]

    themes: list[EmergingTheme] = []

    for row in rows:
        entity = row["entity"].strip()
        if entity.lower() in _NOISE_NAMES:
            continue

        velocity  = float(row["velocity_pct"] or 0)
        n_cos     = int(row["n_companies"] or 0)
        recent    = int(row["recent_signals"] or 0)
        prior     = int(row["prior_signals"] or 0)
        avg_conc  = float(row["avg_concentration"] or 0)

        # Theme explosion score (YoY velocity-aware):
        # velocity: >0 = accelerating YoY, <0 = decelerating
        # Normalize: +100% YoY → vel_norm=1.0, 0% → 0.5, -100% → 0.0
        vel_norm  = max(0.0, min(1.0, (velocity + 100.0) / 200.0))
        co_score  = min(1.0, math.log(1 + n_cos) / math.log(1 + 30))  # 30 companies → 1.0
        # Absolute signal strength: themes with many recent signals are more reliable
        sig_norm  = min(1.0, recent / 200.0)
        theme_score = round(
            vel_norm  * 0.40 +   # YoY acceleration (key: is this theme growing?)
            co_score  * 0.30 +   # breadth (how many companies are in it?)
            avg_conc  * 0.15 +   # purity (is it specific or diffuse?)
            sig_norm  * 0.15,    # absolute signal volume (is it significant?)
            4
        )

        # Build company list
        company_data = row.get("company_data") or []
        if isinstance(company_data, str):
            company_data = _json.loads(company_data)

        companies = []
        for c in company_data:
            if not c.get("company"):
                continue
            conc     = float(c.get("conc") or 0)
            rec_inv  = int(c.get("recent_inv") or 0)
            tot_inv  = int(c.get("total_inv") or 0)
            rec_ratio= float(c.get("rec_ratio") or 0)
            # Company strength for THIS theme:
            # recent investment signals × recency weight × concentration
            strength = round(
                (rec_inv * 2 + tot_inv) *          # signal volume (recent weighted)
                (0.4 + 0.6 * rec_ratio) *           # recency multiplier
                (0.3 + 0.7 * conc),                 # concentration multiplier
                2
            )
            companies.append(ThemeCompany(
                company=c["company"],
                ticker=c.get("ticker") or "",
                inv_signals_recent=rec_inv,
                inv_signals_total=tot_inv,
                docs_with_theme=int(c.get("docs_theme") or 0),
                total_docs=int(c.get("total_docs") or 0),
                concentration=round(conc, 3),
                recency_ratio=round(rec_ratio, 3),
                strength=strength,
            ))

        # Sort companies by strength descending
        companies.sort(key=lambda x: -x.strength)

        themes.append(EmergingTheme(
            entity=entity,
            entity_type=row["entity_type"],
            n_companies=n_cos,
            recent_signals=recent,
            prior_signals=prior,
            velocity_pct=velocity,
            avg_concentration=round(avg_conc, 3),
            theme_score=theme_score,
            companies=companies,
        ))

    # Sort themes by explosion score
    themes.sort(key=lambda t: (-t.theme_score, -t.n_companies))
    return themes


def print_theme_report(themes: list[EmergingTheme], top_themes: int = 15, top_companies: int = 8):
    """Print a clean report of exploding themes and their supply companies."""
    print(f"\n{'='*72}")
    print(f"  INDIA EXPLODING THEMES — {date.today()}")
    print(f"  (sorted by signal velocity × breadth × concentration)")
    print(f"{'='*72}\n")

    shown = 0
    for t in themes:
        if shown >= top_themes:
            break
        shown += 1

        vel_str = f"+{t.velocity_pct:.0f}%" if t.velocity_pct > 0 else f"{t.velocity_pct:.0f}%"
        print(f"{'─'*72}")
        print(f"  #{shown:2d}  {t.entity.upper():30s}  [{t.entity_type}]")
        print(f"       Score: {t.theme_score:.3f}  |  Velocity: {vel_str:>8s}  "
              f"|  Companies: {t.n_companies:3d}  |  Conc: {t.avg_concentration:.2f}")
        print(f"       Signals: {t.recent_signals} recent vs {t.prior_signals} prior (90d windows)")
        print()
        print(f"       {'Company':38s} {'Ticker':10s} {'Strength':>8s} {'Conc':>6s} {'Rec%':>5s}")
        print(f"       {'─'*38} {'─'*10} {'─'*8} {'─'*6} {'─'*5}")
        for i, c in enumerate(t.companies[:top_companies]):
            rec_pct = f"{c.recency_ratio*100:.0f}%"
            conc_pct = f"{c.concentration*100:.0f}%"
            print(f"       {c.company[:38]:38s} {c.ticker:10s} {c.strength:>8.1f} "
                  f"{conc_pct:>6s} {rec_pct:>5s}")
        if len(t.companies) > top_companies:
            print(f"       ... +{len(t.companies)-top_companies} more companies")
        print()
