#!/usr/bin/env python3
"""Add emergence-taxonomy matches to the unreviewed research crosswalk queue.

The role extraction only VERIFIES a maker when its product links to a reviewed
exact constraint crosswalk (mg_constraint_product_aliases, status REVIEWED/
AUTO_DISCOVERY, EXACT). After the India-wide product extraction, ~1,300 issuers
are under radar but QUARANTINED_UNLINKED because their product has no alias.

Regex similarity can establish a family-level research lead, but it cannot prove
that a literal issuer product and a taxonomy constraint are economically identical.
Rows written here are therefore ``AUTO_DISCOVERY/FAMILY`` and have no producer or
position authority. A reviewed exact alias must come from the reviewed DB packet.

USAGE
    python scripts/policy/promote_taxonomy_constraints.py [--country IN] [--dry-run]
"""
import argparse
import os
import re
import sys

import psycopg2
import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
from build_constraint_signal_activity import _COMPILED   # noqa: E402


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def _key(label: str) -> str:
    return "emerging_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="IN")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Distinct role-ledger products currently UNLINKED (or any) that match a
    # taxonomy constraint. Match to the FIRST taxonomy label whose pattern hits.
    cur.execute("""SELECT DISTINCT product_phrase, normalized_product
                   FROM mg_company_product_roles WHERE country=%s AND normalized_product IS NOT NULL""",
                (args.country,))
    rows = cur.fetchall()
    to_add = {}   # normalized_label -> (product_label, constraint_key)
    for r in rows:
        np = r["normalized_product"]
        # skip obviously garbled labels (leading unit token from "5 GW ...")
        if re.match(r"^(gw|mw|gwh|mt|kt|tpa|mtpa|nos)\b", np):
            continue
        for label, rx in _COMPILED.items():
            if rx.search(np):
                nl = _norm_label(np)
                if nl and nl not in to_add:
                    to_add[nl] = (r["product_phrase"] or np, _key(label))
                break

    # Only add where no crosswalk already exists for this label.
    cur.execute("SELECT normalized_label FROM mg_constraint_product_aliases")
    existing = {(_norm_label(r["normalized_label"])) for r in cur.fetchall()}
    new_rows = [(args.country, plabel, nl, ckey, "FAMILY", "AUTO_DISCOVERY",
                 "automatic emergence-taxonomy family match; exact review required")
                for nl, (plabel, ckey) in to_add.items() if nl not in existing]

    print(f"{len(rows)} distinct role products; {len(to_add)} match a taxonomy constraint; "
          f"{len(new_rows)} NEW reviewed aliases to add.")
    for _, pl, nl, ck, *_ in new_rows[:20]:
        print(f"   '{pl[:40]}' -> {ck}")
    if args.dry_run:
        print("dry-run: nothing written")
        return 0
    psycopg2.extras.execute_values(cur, """
        INSERT INTO mg_constraint_product_aliases
          (country, product_label, normalized_label, constraint_key, match_scope, status, source,
           first_seen_date, last_seen_date, review_note)
        VALUES %s
    """, [(c, pl, nl, ck, ms, st, src) for (c, pl, nl, ck, ms, st, src) in new_rows],
        template="(%s,%s,%s,%s,%s,%s,%s, CURRENT_DATE, CURRENT_DATE, "
                 "'Machine family match only; promote through a reviewed, dated exact crosswalk.')",
        page_size=500)
    conn.commit()
    print(f"DONE: added {len(new_rows)} family-level research aliases; none are reviewed exact links.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
