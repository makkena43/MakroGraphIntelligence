#!/usr/bin/env python3
"""GENERIC constraint + company engine (blended detection, HS-anchored link).

One uniform engine, no per-sector rules and no per-sector magnitude data:

  DETECT  (blended)  = trade-import acceleration (UN Comtrade, per HS) blended
                       with filing-signal emergence (per taxonomy constraint).
  LINK    (HS-anchored) = constraint HS -> mg_product_hs_crosswalks -> the
                       role-ledger products classified under the same HS ->
                       their evidenced makers. No token matching.

Point-in-time: only trade periods and signals dated <= as_of are read. Companies
come from the role ledger (own-filing evidence), never from token overlap.
"""

from __future__ import annotations

from datetime import date

from extract_report_data import q
from constraint_emergence import fetch_all_constraint_emergence, _norm as _emg_norm


def _hs_import_series(cur, as_of: date) -> dict[str, dict]:
    """Per HS: trailing-12m imports, prior-12m imports, YoY growth (Comtrade)."""
    lo1 = f"{as_of.year - 1}{as_of.month:02d}"
    lo2 = f"{as_of.year - 2}{as_of.month:02d}"
    hi = f"{as_of.year}{as_of.month:02d}"
    rows = q(cur, """
        SELECT hs_code,
               SUM(value_usd) FILTER (WHERE period > %s AND period <= %s) AS recent,
               SUM(value_usd) FILTER (WHERE period > %s AND period <= %s) AS prior,
               MAX(product_name) AS product_name
        FROM mg_trade_flows
        WHERE reporter_country='India' AND partner_country='World' AND flow_direction='import'
        GROUP BY hs_code
    """, (lo1, hi, lo2, lo1))
    out = {}
    for r in rows:
        recent = float(r["recent"] or 0)
        prior = float(r["prior"] or 0)
        if recent <= 0:
            continue
        out[r["hs_code"]] = {
            "hs_code": r["hs_code"], "product_name": r["product_name"],
            "import_bn": round(recent / 1e9, 3),
            "yoy_pct": round((recent / prior - 1) * 100, 1) if prior > 0 else None,
        }
    return out


def _hs_to_makers(cur, as_of: date, country: str) -> dict[str, list[str]]:
    """HS -> evidenced role-ledger makers, via the reviewed product<->HS crosswalk.
    This is the clean generic link (no token overlap)."""
    rows = q(cur, """
        SELECT DISTINCT x.hs_code, UPPER(TRIM(r.ticker)) AS ticker
        FROM mg_company_product_roles r
        JOIN mg_product_hs_crosswalks x
          ON x.country=r.country
         AND lower(btrim(x.normalized_product)) = lower(btrim(r.normalized_product))
         AND x.review_status='REVIEWED' AND x.relationship_scope='EXACT'
        WHERE r.country=%s AND r.as_of_date=%s AND r.ticker <> ''
          AND r.adjudication_state IN ('OPERATING_PRODUCER_EVIDENCED',
                                       'EARNINGS_CAPTURE_EVIDENCED','EXACT_ROLE_EVIDENCED')
    """, (country, as_of))
    hs_makers: dict[str, set] = {}
    for r in rows:
        hs_makers.setdefault(r["hs_code"], set()).add(r["ticker"])
    return {hs: sorted(tk) for hs, tk in hs_makers.items()}


def _label_to_hs(cur, country: str) -> dict[str, str]:
    """Taxonomy/product label -> HS, from the reviewed crosswalk (DB-driven, so
    both detection legs share the same HS key with no re-classification)."""
    rows = q(cur, """SELECT lower(btrim(normalized_product)) AS p, hs_code
                     FROM mg_product_hs_crosswalks
                     WHERE country=%s AND review_status='REVIEWED'""", (country,))
    return {r["p"]: r["hs_code"] for r in rows}


def _emergence_by_hs(cur, as_of: date, country: str) -> dict[str, float]:
    """Filing-signal emergence per taxonomy constraint, folded onto HS via the
    reviewed crosswalk (so both detection legs share one key)."""
    label_hs = _label_to_hs(cur, country)
    emg = fetch_all_constraint_emergence(cur, as_of, country)
    by_hs: dict[str, float] = {}
    for label, m in emg.items():
        hs = label_hs.get((label or "").strip().lower())
        if not hs:
            continue
        by_hs[hs] = max(by_hs.get(hs, 0.0), float(m.get("emergence_score") or 0.0))
    return by_hs


def detect_constraints_with_companies(cur, as_of: date, country: str = "IN") -> list[dict]:
    """Return blended constraints, each with HS-linked evidenced companies."""
    trade = _hs_import_series(cur, as_of)
    hs_makers = _hs_to_makers(cur, as_of, country)
    emg_hs = _emergence_by_hs(cur, as_of, country)
    hs_universe = set(trade) | set(emg_hs) | set(hs_makers)

    out = []
    for hs in hs_universe:
        t = trade.get(hs, {})
        yoy = t.get("yoy_pct")
        # trade acceleration score: normalized YoY import growth (leading), 0..1
        trade_score = max(0.0, min(1.0, (yoy or 0) / 100.0)) if yoy is not None else 0.0
        signal_score = max(0.0, min(1.0, emg_hs.get(hs, 0.0) / 2.0))   # emergence ~0..2
        blended = round(0.5 * trade_score + 0.5 * signal_score, 3)
        out.append({
            "hs_code": hs,
            "constraint": t.get("product_name") or hs,
            "import_bn": t.get("import_bn"),
            "trade_yoy_pct": yoy,
            "trade_score": round(trade_score, 3),
            "signal_emergence": round(emg_hs.get(hs, 0.0), 2),
            "signal_score": round(signal_score, 3),
            "blended_score": blended,
            "companies": hs_makers.get(hs, []),
            "company_count": len(hs_makers.get(hs, [])),
        })
    out.sort(key=lambda r: (-r["blended_score"], -(r["import_bn"] or 0)))
    return out


if __name__ == "__main__":
    import sys
    import psycopg2.extras
    from extract_report_data import connect
    aod = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2024, 12, 31)
    cur = connect().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rows = detect_constraints_with_companies(cur, aod)
    print(f"GENERIC blended constraint+company detection @ {aod}\n")
    print(f"{'constraint':30}{'imp$bn':>7}{'YoY%':>7}{'emg':>5}{'blend':>7}  companies")
    for r in rows:
        print(f"  {r['constraint'][:28]:28}{(r['import_bn'] or 0):>7.2f}"
              f"{('%+.0f'%r['trade_yoy_pct']) if r['trade_yoy_pct'] is not None else '  n/a':>7}"
              f"{r['signal_emergence']:>5}{r['blended_score']:>7}  {r['companies'][:6]}")
