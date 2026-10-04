#!/usr/bin/env python3
"""Trade-flow -> IMPORT-observation connector (event-sourced model).

Emits reviewed-primary IMPORT observation packets for the event-sourced
constraint ledger from REAL UN-Comtrade monthly India import data
(mg_trade_flows) joined to a reviewed domestic-production denominator
(mg_domestic_production). The import ratio is computed, point-in-time:

    import_share = trailing_12m_imports / (trailing_12m_imports + domestic_annual)

so the numerator is a real customs number (Comtrade, current within the IMPORT
TTL) and the denominator is a reviewed, cited domestic-production figure. No
fabrication: a constraint only emits a physical IMPORT measure when BOTH legs
exist. Where domestic production is genuinely negligible (no listed operating
producer — e.g. semiconductor fabs, polysilicon), the reviewed row records that
with its citation and the share resolves to ~1.0.

Comtrade alone cannot state an import SHARE (it has imports, not domestic
output); this is why the reviewed domestic denominator is required. Trade
momentum/level alone remains research context under the contract.

Usage:
    python scripts/policy/build_trade_flow_observations.py --as-of 2024-12-31 \
        [--output data/reference/constraint_observations/trade_flow_<asof>.json] [--emit-only]
    # then: ingest_primary_constraint_observations.py + materialize_constraint_ledger.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg2.extras

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "stock_report"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_constraint_ledger import connect  # noqa: E402
from constraint_ledger import fetch_constraint_aliases, normalize_product_label  # noqa: E402

DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "reference" / "constraint_observations"
_COMTRADE = ("https://comtradeapi.un.org/public/v1/preview/C/M/HS"
             "?reporterCode=699&partnerCode=0&flowCode=M&cmdCode={hs}&period={period}")


def _ensure_domestic_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mg_domestic_production (
            country              VARCHAR(4)  NOT NULL,
            constraint_key       TEXT        NOT NULL,
            product_label        TEXT        NOT NULL,
            hs_code              TEXT,
            effective_year       INT         NOT NULL,
            domestic_value_usd   NUMERIC,          -- annual domestic output (USD); 0 = negligible
            primary_origin       TEXT[],
            substitution_horizon_years INT,
            source_url           TEXT        NOT NULL,
            source_title         TEXT        NOT NULL,
            source_family        TEXT        NOT NULL,
            review_status        TEXT        NOT NULL DEFAULT 'REVIEWED',
            note                 TEXT,
            created_at           TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (country, constraint_key, effective_year)
        )
    """)


def _trailing_imports(cur, hs_code: str, as_of: date) -> tuple[float, str | None]:
    """Sum of Comtrade import value over the trailing 12 months <= as_of, and the
    latest period used (as the observation's economic date)."""
    lo = f"{(as_of.year - 1)}{as_of.month:02d}"
    hi = f"{as_of.year}{as_of.month:02d}"
    cur.execute("""
        SELECT period, value_usd FROM mg_trade_flows
        WHERE reporter_country='India' AND partner_country='World'
          AND flow_direction='import' AND hs_code=%s AND period > %s AND period <= %s
        ORDER BY period
    """, (hs_code, lo, hi))
    rows = cur.fetchall()
    total = sum(float(r["value_usd"] or 0) for r in rows)
    latest = rows[-1]["period"] if rows else None
    return total, latest


def _period_to_date(period: str) -> date:
    return date(int(period[:4]), int(period[4:6]), 28)


def build_observations(cur, country: str, as_of: date) -> tuple[list[dict], list[str]]:
    aliases = fetch_constraint_aliases(cur, as_of, country)
    reviewed_exact = {
        (a["constraint_key"], normalize_product_label(a["product_label"]))
        for a in aliases.values()
        if a.get("status") == "REVIEWED" and a.get("match_scope") == "EXACT"
    }
    cur.execute("""SELECT * FROM mg_domestic_production
                   WHERE country=%s AND effective_year <= %s AND review_status='REVIEWED'
                   ORDER BY constraint_key, effective_year""", (country, as_of.year))
    # latest reviewed domestic figure per constraint, known by as_of year
    dom_by_key: dict[str, dict] = {}
    for r in cur.fetchall():
        dom_by_key[r["constraint_key"]] = dict(r)   # ordered asc -> keeps latest

    obs, skipped = [], []
    for key, dom in dom_by_key.items():
        label = dom["product_label"]
        if (key, normalize_product_label(label)) not in reviewed_exact:
            skipped.append(f"{label}: no REVIEWED/EXACT alias")
            continue
        hs = dom.get("hs_code")
        imports, latest_period = _trailing_imports(cur, hs, as_of) if hs else (0.0, None)
        if not latest_period:
            skipped.append(f"{label}: no Comtrade import data <= {as_of}")
            continue
        domestic = float(dom["domestic_value_usd"]) if dom["domestic_value_usd"] is not None else None
        if domestic is None:
            skipped.append(f"{label}: domestic_value_usd not set")
            continue
        denom = imports + domestic
        if denom <= 0:
            skipped.append(f"{label}: zero denominator")
            continue
        share = round(imports / denom, 4)
        observed = _period_to_date(latest_period)
        # customs data for a month is available with a reporting lag; make the
        # availability date conservative (never earlier than the economic month).
        available = min(observed + timedelta(days=75), as_of)
        obs.append({
            "constraint_key": key,
            "product_label": label,
            "observation_type": "IMPORT",
            "observed_at": observed.isoformat(),
            "published_at": available.isoformat(),
            "available_at": available.isoformat(),
            "economic_period_start": _period_to_date(
                f"{observed.year - 1}{observed.month:02d}").isoformat(),
            "economic_period_end": observed.isoformat(),
            "source_url": _COMTRADE.format(hs=hs, period=latest_period),
            "source_title": (f"UN Comtrade India monthly imports HS {hs} "
                             f"({label}); domestic denominator: {dom['source_title']}"),
            "source_family": "UN_COMTRADE_IMPORTS",
            "metrics": {
                "measurement_basis": "CURRENT_IMPORT_SHARE",
                "import_share": share,
                "numerator": round(imports, 2),
                "denominator": round(denom, 2),
                "numerator_basis": "trailing_12m_comtrade_import_usd",
                "denominator_basis": "imports_plus_reviewed_domestic_output_usd",
                "domestic_value_usd": round(domestic, 2),
                "import_value_bn_usd": round(imports / 1e9, 4),
                "primary_origin": dom.get("primary_origin"),
                "substitution_horizon_years": dom.get("substitution_horizon_years"),
                "domestic_source_url": dom["source_url"],
                "excerpt": (f"India trailing-12m imports of {label} (HS {hs}) were "
                            f"${imports/1e9:.2f}bn to {latest_period}; against reviewed "
                            f"domestic output ${domestic/1e9:.2f}bn this is a "
                            f"{share*100:.0f}% current import share. {dom.get('note') or ''}").strip(),
            },
        })
    return obs, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", default="IN")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _ensure_domestic_table(cur)
    conn.commit()
    obs, skipped = build_observations(cur, args.country.upper(), as_of)
    out = args.output or (DEFAULT_OUT_DIR / f"trade_flow_{args.country.upper()}_{args.as_of}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"country": args.country.upper(), "observations": obs}, indent=2))
    print(f"Wrote {len(obs)} IMPORT observations -> {out}")
    for o in obs:
        m = o["metrics"]
        print(f"  {o['product_label'][:26]:26} share={m['import_share']} "
              f"imports=${m['import_value_bn_usd']}bn obs_at={o['observed_at']}")
    if skipped:
        print("Skipped (no rigorous leg):")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
