"""Measure supply-chain propagation lags from 6 years of filings.

Validates the hand-written UPSTREAM_COMPONENTS ontology against history:
for each (constrained domain → upstream component) edge, when a domain
became confirmed-constrained (≥3 distinct companies with seller-constraint
evidence mentioning the domain), how many quarters later did companies
talking about the upstream component start showing their own constraint
evidence (≥2 distinct companies)?

Output: measured lag per edge, or verdicts like "no propagation observed" —
which is how ontology edges earn their place or get retired. No prices.

Usage:
    PYTHONPATH=src:. python -m makrograph.evaluation.propagation_lag --country IN
"""

import argparse
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def _quarter(d) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _q_index(q: str) -> int:
    y, n = q.split("Q")
    return int(y) * 4 + int(n)


def run(country: str) -> str:
    import backend.main as app_main
    from backend.main import THEME_KW_FAMILIES, UPSTREAM_COMPONENTS

    store = app_main.get_pg()
    with store._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT UPPER(TRIM(d.ticker)) AS tk, d.filed_at::date AS dt,
                       LOWER(LEFT(s.context_text, 400)) AS txt
                FROM mg_signals s
                JOIN mg_documents d ON d.id = s.document_id
                WHERE d.country = %s
                  AND (({app_main.CONSTRAINT_PRED_SQL})
                       OR ({app_main.DS_SELLER_PRED_SQL}))
                  AND s.context_text IS NOT NULL""",
            (country,),
        )
        rows = cur.fetchall()

    # Per quarter: distinct companies whose constraint evidence mentions
    # each domain / each upstream component
    dom_q: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    comp_q: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    all_components = {c for comps in UPSTREAM_COMPONENTS.values() for c in comps}

    for tk, dt, txt in rows:
        q = _quarter(dt)
        for dom, words in THEME_KW_FAMILIES.items():
            if dom in UPSTREAM_COMPONENTS and any(w in txt for w in words):
                dom_q[dom][q].add(tk)
        for comp in all_components:
            if comp in txt:
                comp_q[comp][q].add(tk)

    def _onset(series: dict[str, set], min_cos: int) -> str | None:
        qs = sorted(series, key=_q_index)
        for q in qs:
            if len(series[q]) >= min_cos:
                return q
        return None

    lines = [f"country={country}, evidence rows={len(rows)}",
             f"{'domain':<14}{'upstream component':<20}{'domain@3cos':<12}{'comp@2cos':<12}lag(q)"]
    for dom, comps in UPSTREAM_COMPONENTS.items():
        d_on = _onset(dom_q.get(dom, {}), 3)
        for comp in comps:
            c_on = _onset(comp_q.get(comp, {}), 2)
            if d_on and c_on:
                lag = _q_index(c_on) - _q_index(d_on)
                lines.append(f"{dom:<14}{comp:<20}{d_on:<12}{c_on:<12}{lag:+d}")
            elif d_on:
                lines.append(f"{dom:<14}{comp:<20}{d_on:<12}{'—':<12}no propagation observed")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="IN")
    args = ap.parse_args()
    print(run(args.country))
