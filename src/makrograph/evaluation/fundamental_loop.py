"""Fundamental feedback loop: grade past detections against SUBSEQUENT FILINGS.

No price data anywhere. A detection of company X in year N is judged by what
X's own year-N+1 filings show:

    CONFIRMED — the explosion arrived in the fundamentals:
        pricing/margin signals appear or grow, AND
        (order-anchored seller demand persists OR constraint signals grow)
    DECAYED  — the thesis evaporated:
        constraint AND order-demand signals both vanish
    PARTIAL  — anything in between (still alive, not yet confirmed)

Detections come from the exact production detector (backend.main.
get_investment_final_shortlist), and outcomes use the exact production SQL
predicates (CONSTRAINT_PRED_SQL / DS_SELLER_PRED_SQL) — so the loop measures
the real system, not a reimplementation.

Results are persisted to mg_fundamental_eval and aggregated into hit rates
by stage / detection path / explosion legs / score band. Those measured rates
are what should eventually replace the hand-tuned scoring weights.

Usage:
    PYTHONPATH=src:. python -m makrograph.evaluation.fundamental_loop --country US --years 2020-2024
"""

import argparse
import logging

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS mg_fundamental_eval (
    id               BIGSERIAL PRIMARY KEY,
    country          VARCHAR(4)  NOT NULL,
    detection_year   INT         NOT NULL,
    outcome_year     INT         NOT NULL,
    ticker           TEXT        NOT NULL,
    company          TEXT        NOT NULL,
    -- what the detector said at detection time
    stage            INT,
    detection_path   TEXT,
    trajectory       TEXT,
    rank_score       DOUBLE PRECISION,
    explosion_legs   INT,
    explosion        BOOLEAN,
    peer_corroboration INT,
    c_count          INT,
    -- what the NEXT year's filings showed
    next_c_count     INT,
    next_ds_seller   INT,
    next_pricing     INT,
    next_capex       INT,
    next_easing      INT,
    next_order_growth DOUBLE PRECISION,
    outcome          TEXT,       -- confirmed | partial | decayed | no_filings
    evaluated_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (country, detection_year, ticker)
);
"""

OUTCOME_SQL_TEMPLATE = """
SELECT UPPER(TRIM(d.ticker)) AS tk,
       COUNT(*) FILTER (WHERE {cpred})     AS next_c,
       COUNT(*) FILTER (WHERE {dspred})    AS next_ds,
       COUNT(*) FILTER (WHERE s.signal_type IN
           ('realized_margin_expansion','pricing_power_emerging')) AS next_pricing,
       COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase')    AS next_capex,
       COUNT(*) FILTER (WHERE s.signal_type = 'supply_easing')     AS next_easing,
       MAX(s.signal_value) FILTER (WHERE s.signal_unit = 'order_growth_pct')
                                                                   AS next_og,
       COUNT(DISTINCT d.id)                                        AS next_docs
FROM mg_signals s
JOIN mg_documents d ON d.id = s.document_id
WHERE d.country = %s
  AND d.filed_at BETWEEN %s AND %s
  AND UPPER(TRIM(d.ticker)) = ANY(%s)
GROUP BY UPPER(TRIM(d.ticker))
"""


def classify_outcome(det: dict, nxt: dict | None) -> str:
    """Grade one detection against next-year signal evidence."""
    if nxt is None or int(nxt.get("next_docs") or 0) == 0:
        return "no_filings"          # delisted / not covered — excluded from rates
    next_c   = int(nxt.get("next_c") or 0)
    next_ds  = int(nxt.get("next_ds") or 0)
    next_pr  = int(nxt.get("next_pricing") or 0)
    next_og  = float(nxt.get("next_og") or 0)
    c_now    = int(det.get("constraint_signals") or 0)

    thesis_alive   = (next_c >= 2) or (next_ds >= 2)
    pricing_proof  = next_pr >= 1 or next_og >= 25
    growing        = next_c > c_now

    if (thesis_alive and pricing_proof) or growing:
        return "confirmed"
    if next_c == 0 and next_ds == 0:
        return "decayed"
    return "partial"


def run(country: str, years: list[int], apply: bool = True) -> list[dict]:
    """Evaluate detection years; outcome year = detection year + 1."""
    from datetime import date
    import backend.main as app_main   # the production detector + predicates

    store = app_main.get_pg()
    rows_out: list[dict] = []

    with store._conn() as conn:
        cur = conn.cursor()
        cur.execute(DDL)

        for yr in years:
            res = app_main.get_investment_final_shortlist(country=country, year=yr)
            detections = res.get("final_shortlist", [])
            logger.info("%s %s: %d detections", country, yr, len(detections))
            if not detections:
                continue

            tickers = [(d.get("ticker") or "").upper() for d in detections if d.get("ticker")]
            o_from, o_to = date(yr + 1, 1, 1), date(yr + 1, 12, 31)
            cur.execute(
                OUTCOME_SQL_TEMPLATE.format(
                    cpred=app_main.CONSTRAINT_PRED_SQL,
                    dspred=app_main.DS_SELLER_PRED_SQL,
                ),
                (country, o_from, o_to, tickers),
            )
            cols = [c.name for c in cur.description]
            nxt_map = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

            for det in detections:
                tk = (det.get("ticker") or "").upper()
                nxt = nxt_map.get(tk)
                outcome = classify_outcome(det, nxt)
                row = {
                    "country": country, "detection_year": yr, "outcome_year": yr + 1,
                    "ticker": tk, "company": det.get("company",""),
                    "stage": det.get("constraint_stage"),
                    "detection_path": det.get("detection_path"),
                    "trajectory": det.get("trajectory"),
                    "rank_score": det.get("rank_score"),
                    "explosion_legs": det.get("explosion_legs"),
                    "explosion": bool(det.get("explosion_potential")),
                    "peer_corroboration": det.get("peer_corroboration"),
                    "c_count": det.get("constraint_signals"),
                    "next_c_count": int((nxt or {}).get("next_c") or 0),
                    "next_ds_seller": int((nxt or {}).get("next_ds") or 0),
                    "next_pricing": int((nxt or {}).get("next_pricing") or 0),
                    "next_capex": int((nxt or {}).get("next_capex") or 0),
                    "next_easing": int((nxt or {}).get("next_easing") or 0),
                    "next_order_growth": float((nxt or {}).get("next_og") or 0),
                    "outcome": outcome,
                }
                rows_out.append(row)
                if apply:
                    cur.execute(
                        """INSERT INTO mg_fundamental_eval
                           (country, detection_year, outcome_year, ticker, company,
                            stage, detection_path, trajectory, rank_score,
                            explosion_legs, explosion, peer_corroboration, c_count,
                            next_c_count, next_ds_seller, next_pricing, next_capex,
                            next_easing, next_order_growth, outcome)
                           VALUES (%(country)s,%(detection_year)s,%(outcome_year)s,
                                   %(ticker)s,%(company)s,%(stage)s,%(detection_path)s,
                                   %(trajectory)s,%(rank_score)s,%(explosion_legs)s,
                                   %(explosion)s,%(peer_corroboration)s,%(c_count)s,
                                   %(next_c_count)s,%(next_ds_seller)s,%(next_pricing)s,
                                   %(next_capex)s,%(next_easing)s,%(next_order_growth)s,
                                   %(outcome)s)
                           ON CONFLICT (country, detection_year, ticker)
                           DO UPDATE SET outcome = EXCLUDED.outcome,
                                         next_c_count = EXCLUDED.next_c_count,
                                         next_ds_seller = EXCLUDED.next_ds_seller,
                                         next_pricing = EXCLUDED.next_pricing,
                                         next_capex = EXCLUDED.next_capex,
                                         next_easing = EXCLUDED.next_easing,
                                         next_order_growth = EXCLUDED.next_order_growth,
                                         stage = EXCLUDED.stage,
                                         detection_path = EXCLUDED.detection_path,
                                         trajectory = EXCLUDED.trajectory,
                                         rank_score = EXCLUDED.rank_score,
                                         explosion_legs = EXCLUDED.explosion_legs,
                                         explosion = EXCLUDED.explosion,
                                         peer_corroboration = EXCLUDED.peer_corroboration,
                                         c_count = EXCLUDED.c_count,
                                         evaluated_at = now()""",
                        row,
                    )
        conn.commit()
    return rows_out


def report(rows: list[dict]) -> str:
    """Hit-rate table by the dimensions that drive scoring."""
    from collections import defaultdict

    graded = [r for r in rows if r["outcome"] != "no_filings"]
    lines = [f"graded={len(graded)} (excluded no_filings={len(rows)-len(graded)})"]

    def _table(keyfn, label):
        agg: dict = defaultdict(lambda: {"confirmed": 0, "partial": 0, "decayed": 0})
        for r in graded:
            agg[keyfn(r)][r["outcome"]] += 1
        lines.append(f"\n-- by {label} --")
        for k in sorted(agg, key=str):
            a = agg[k]; n = sum(a.values())
            lines.append(
                f"  {str(k):<16} n={n:<4} confirmed={a['confirmed']/n*100:5.1f}%  "
                f"partial={a['partial']/n*100:5.1f}%  decayed={a['decayed']/n*100:5.1f}%"
            )

    _table(lambda r: f"stage {r['stage']}", "constraint stage")
    _table(lambda r: r["detection_path"], "detection path")
    _table(lambda r: f"legs {r['explosion_legs']}", "explosion legs")
    _table(lambda r: "explosion" if r["explosion"] else "no-explosion", "explosion flag")
    _table(lambda r: f"score {int((r['rank_score'] or 0)//20)*20}-{int((r['rank_score'] or 0)//20)*20+19}",
           "score band")
    _table(lambda r: f"corr {'0' if not r['peer_corroboration'] else '1-4' if r['peer_corroboration']<5 else '5+'}",
           "peer corroboration")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="US")
    ap.add_argument("--years", default="2020-2024", help="e.g. 2020-2024 or 2022,2023")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if "-" in args.years:
        a, b = args.years.split("-")
        years = list(range(int(a), int(b) + 1))
    else:
        years = [int(y) for y in args.years.split(",")]
    rows = run(args.country, years, apply=not args.dry_run)
    print(report(rows))
