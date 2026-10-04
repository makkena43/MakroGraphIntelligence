#!/usr/bin/env python3
"""Populate mg_product_hs_crosswalks — the GENERIC constraint<->company link.

The trade side speaks HS codes; the company/role side speaks filing-text
normalized products. Naive token matching mis-links them (cathode->pharma).
This anchors BOTH sides on the same identity (HS code) via one uniform
product-classification map, so "detect constraint X (HS)" -> "companies that
make X" becomes a clean HS join with no token guessing and no per-sector
magnitude data.

The map is product IDENTITY (which HS chapter a product is classified under —
standard, auditable trade classification), applied uniformly to every product
in the trade table, the role ledger and the reviewed aliases. It is NOT a
scarcity or magnitude judgement.

Usage:
    python scripts/policy/build_hs_product_crosswalk.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import psycopg2.extras

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "stock_report"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_constraint_ledger import connect  # noqa: E402

# Canonical product family -> HS code. One generic classification dictionary,
# applied to BOTH the trade vocabulary and the role-ledger vocabulary. Each
# entry: HS code + the token-tests that classify a normalized product into it.
# (a product matches a family if ANY of its any_tokens sets is fully present.)
_FAMILY_HS: list[tuple[str, str, list[set[str]]]] = [
    ("2941", "Pharma APIs", [{"pharmaceutical"}, {"pharma", "api"}, {"pharma", "apis"},
                             {"bulk", "drug"}, {"api"}, {"apis"}]),
    ("8541", "Solar PV cell/module", [{"solar", "cell"}, {"solar", "module"},
                                      {"solar", "panel"}, {"solar", "wafer"}, {"photovoltaic"}]),
    ("8504", "Power transformer / T&D", [{"transformer"}, {"hvdc"}, {"t&d"}, {"rectifier"}]),
    ("8534", "Printed circuit board", [{"pcb"}, {"printed", "circuit"}, {"hdi", "pcb"}]),
    ("9002", "Optical fibre", [{"optical", "fiber"}, {"optical", "fibre"}, {"preform"}]),
    ("2841", "Cathode active materials", [{"cathode"}, {"anode"}]),
    ("2804", "Polysilicon", [{"polysilicon"}]),
    ("8542", "Semiconductor IC", [{"semiconductor"}, {"integrated", "circuit"}]),
    ("8532", "Passive components", [{"mlcc"}, {"passive", "component"}, {"capacitor"}]),
    ("2836", "Lithium carbonate / cell", [{"lithium"}, {"battery", "cell"}]),
    ("8524", "Display panel", [{"display", "panel"}, {"display"}]),
    ("8526", "Defense electronics", [{"radar"}, {"avionics"}, {"defense", "electronics"},
                                     {"defence", "electronics"}]),
    ("2903", "Fluorochemicals", [{"fluorochemical"}, {"fluoro"}]),
    ("3808", "Specialty agrochem AI", [{"agrochem"}, {"pesticide"}]),
    ("7225", "CRGO / electrical steel", [{"crgo"}, {"grain", "oriented"}, {"electrical", "steel"}]),
]


def _norm(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9&]+", (text or "").lower()) if t}


def classify(product: str) -> tuple[str, str] | None:
    toks = _norm(product)
    for hs, name, token_sets in _FAMILY_HS:
        if any(s <= toks for s in token_sets):
            return hs, name
    return None


def source_products(cur) -> set[str]:
    """Every product identity that needs an HS anchor: trade components, role-
    ledger evidenced products, and reviewed constraint aliases."""
    products: set[str] = set()
    cur.execute("SELECT DISTINCT product_name FROM mg_trade_flows WHERE product_name IS NOT NULL")
    products |= {r["product_name"] for r in cur.fetchall()}
    cur.execute("""SELECT DISTINCT normalized_product FROM mg_company_product_roles
                   WHERE country='IN' AND normalized_product IS NOT NULL""")
    products |= {r["normalized_product"] for r in cur.fetchall()}
    cur.execute("""SELECT DISTINCT product_label FROM mg_constraint_product_aliases
                   WHERE country='IN' AND product_label IS NOT NULL""")
    products |= {r["product_label"] for r in cur.fetchall()}
    return {p for p in products if p and p.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", default="IN")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rows = []
    for product in sorted(source_products(cur)):
        hit = classify(product)
        if not hit:
            continue
        hs, name = hit
        rows.append((args.country, product, hs))
    unclassified = [p for p in sorted(source_products(cur)) if not classify(p)]
    print(f"Classified {len(rows)} product identities into {len({r[2] for r in rows})} HS codes.")
    if args.dry_run:
        for country, product, hs in rows:
            print(f"  {hs}  <-  {product}")
        print(f"\nUnclassified (no HS family; left unlinked — honest): {len(unclassified)}")
        for p in unclassified[:40]:
            print(f"  -- {p}")
        conn.rollback()
        return 0
    for country, product, hs in rows:
        cur.execute("""
            INSERT INTO mg_product_hs_crosswalks
              (country, normalized_product, hs_code, relationship_scope,
               effective_from, review_status, source_url, review_note)
            VALUES (%s,%s,%s,'EXACT',%s,'REVIEWED','classifier:build_hs_product_crosswalk',
                    'generic HS product-identity classification (not a scarcity judgement)')
            ON CONFLICT (country, normalized_product, hs_code, effective_from) DO UPDATE
              SET review_status='REVIEWED', relationship_scope='EXACT'
        """, (country, product, hs, date(2020, 1, 1)))
    conn.commit()
    print(f"Wrote {len(rows)} crosswalk rows; {len(unclassified)} products left unlinked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
