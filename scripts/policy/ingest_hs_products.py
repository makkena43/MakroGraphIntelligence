#!/usr/bin/env python3
"""
HS product-taxonomy ingester — scale the constrained-product vocabulary from
a hand-curated handful to the full customs taxonomy, ranked by how much India
actually imports.

WHY
---
The winner autopsy (scripts/research/winner_autopsy.py) found that 82% of
>=5x winners never entered the screen's universe, and that 97% of those
missed names (618 of 637) DO have substantive filing text already in the
corpus. So coverage is not a data problem -- it is a VOCABULARY problem:
company_capabilities.py can only recognise makers of products that exist in
`mg_india_beneficiaries.constrained_product` (12) and
`mg_import_dependencies.component` (16). A company making anything else is
invisible no matter how much it files.

This module replaces that curated list with the official HS taxonomy:
  - HS classification reference (1,266 four-digit categories + descriptions)
  - India's own import value per category, from UN Comtrade
and stores them ranked by import value -- which is the constraint signal
itself. A category India imports heavily, with a listed domestic maker, IS
the import-substitution thesis stated in customs data rather than inferred
from filings narrative.

NO HARDCODING (standing user rule)
----------------------------------
Nothing here names a company, theme, sector or product. HS codes and their
descriptions are external reference data loaded into a table, exactly like
mg_tracked_schemes or mg_symbol_renames. Which products matter is decided by
measured import value, not by opinion. Downstream, company_capabilities.py
derives search terms from the descriptions mechanically and gates them on
corpus frequency, cross-product ambiguity and industry coherence.

SCALE WARNING
-------------
Do NOT feed all 1,266 categories to the capability mapper: each product
costs a corpus pass. Use --top-n (default 150 by import value) and the
batched scanner. Most of the tail is agricultural/raw-material trade with no
listed Indian manufacturer anyway, and will correctly yield nothing.

USAGE
-----
    python scripts/policy/ingest_hs_products.py --year 2024
    python scripts/policy/ingest_hs_products.py --year 2024 --top-n 200
    python scripts/policy/ingest_hs_products.py --year 2024 --dry-run
"""
import argparse
import os
import re
import sys

import psycopg2.extras
import requests

HS_REFERENCE_URL = "https://comtradeapi.un.org/files/v1/app/reference/HS.json"
COMTRADE_URL = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
REPORTER_INDIA = 699
PARTNER_WORLD = 0
FLOW_IMPORT = "M"
TIMEOUT = 60

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
from extract_report_data import connect  # noqa: E402


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mg_hs_products (
                hs_code TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                import_value_usd DOUBLE PRECISION,
                import_year INT,
                value_rank INT,
                tracked BOOLEAN DEFAULT false,
                source TEXT,
                fetched_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_mg_hs_rank
                       ON mg_hs_products (value_rank)""")
    conn.commit()


def fetch_hs_reference(session):
    """code -> human description, four-digit level only.
    Entries arrive as '0101 - Horses, asses, mules and hinnies; live'."""
    r = session.get(HS_REFERENCE_URL, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for row in r.json().get("results", []):
        code = str(row.get("id", "")).strip()
        if len(code) != 4 or not code.isdigit():
            continue
        text = str(row.get("text", "")).strip()
        text = re.sub(r"^\d{4}\s*-\s*", "", text)      # drop the code prefix
        if text:
            out[code] = text
    return out


def fetch_india_imports(session, year):
    """HS4 -> India's import value for the year. One call: cmdCode=AG4 asks
    for every four-digit category at once."""
    params = {"reporterCode": REPORTER_INDIA, "period": year,
              "partnerCode": PARTNER_WORLD, "cmdCode": "AG4",
              "flowCode": FLOW_IMPORT}
    r = session.get(COMTRADE_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    vals = {}
    for row in r.json().get("data", []):
        code = str(row.get("cmdCode", "")).strip()
        if len(code) != 4 or not code.isdigit():
            continue
        # '9999' is the HS standard's "not specified according to kind"
        # aggregate — a structural placeholder, not a product. Excluded as
        # reference-data handling, the same way TOTAL is.
        if code == "9999":
            continue
        vals[code] = vals.get(code, 0.0) + float(row.get("primaryValue") or 0)
    return vals


def main():
    ap = argparse.ArgumentParser(description="Ingest HS product taxonomy")
    ap.add_argument("--year", type=int, required=True,
                    help="import year used to rank products by value")
    ap.add_argument("--top-n", type=int, default=150,
                    help="how many categories to mark tracked (default 150)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    session = requests.Session()
    print(f"Fetching HS reference: {HS_REFERENCE_URL}")
    desc = fetch_hs_reference(session)
    print(f"  HS4 categories with descriptions: {len(desc)}")

    print(f"Fetching India HS4 imports for {args.year}")
    vals = fetch_india_imports(session, args.year)
    print(f"  categories with import value: {len(vals)}")
    if not vals:
        print("No import data returned — aborting rather than writing a "
              "taxonomy with no ranking signal.", file=sys.stderr)
        sys.exit(1)

    ranked = sorted(vals.items(), key=lambda kv: -kv[1])
    rows = []
    for rank, (code, value) in enumerate(ranked, 1):
        if code not in desc:
            continue                                   # no official label, skip
        rows.append((code, desc[code], value, args.year, rank,
                     rank <= args.top_n, "UN_COMTRADE_HS"))

    print(f"\nTop 15 Indian import categories {args.year}:")
    for code, d, v, _y, rank, tracked, _s in rows[:15]:
        print(f"  #{rank:3d} {code}  ${v/1e9:6.2f}bn  {d[:70]}")
    print(f"\n{len(rows)} categories resolved; "
          f"{sum(1 for r in rows if r[5])} marked tracked (top {args.top_n})")

    if args.dry_run:
        print("(dry-run: nothing written)")
        return

    conn = connect()
    ensure_table(conn)
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, """
            INSERT INTO mg_hs_products
              (hs_code, description, import_value_usd, import_year,
               value_rank, tracked, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (hs_code) DO UPDATE SET
              description = EXCLUDED.description,
              import_value_usd = EXCLUDED.import_value_usd,
              import_year = EXCLUDED.import_year,
              value_rank = EXCLUDED.value_rank,
              tracked = EXCLUDED.tracked,
              fetched_at = now()
        """, rows, page_size=200)
    conn.commit()
    conn.close()
    print(f"Wrote {len(rows)} rows to mg_hs_products")


if __name__ == "__main__":
    main()
