#!/usr/bin/env python3
"""Append-only decision log (PMS track-record instrumentation).

Every judgment decision (BUY/NO/EXIT/TRIM) is written to mg_decisions with a
SHA-256 of the judgment sidecar, so the live track record accumulates in the
DB from the day a decision is made — auditable, immutable (UPDATE/DELETE are
blocked by rules), and independent of report files.

Usage:
    log_decisions.py <judgment.json> [--source live|backfill]

Idempotent: a (as_of, ticker, action, judgment_sha256) tuple is only ever
inserted once — re-running on the same file is a no-op.
"""
import argparse
import hashlib
import json
import os
import re
import sys

import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_report_data import connect  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS mg_decisions (
    id SERIAL PRIMARY KEY,
    as_of DATE NOT NULL,
    country TEXT DEFAULT 'IN',
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,          -- BUY | NO | EXIT | TRIM
    size TEXT,
    weight_pct NUMERIC,
    category TEXT,
    rationale TEXT,
    flips_when TEXT,
    constraint_name TEXT,
    judgment_file TEXT,
    judgment_sha256 TEXT,
    logged_at TIMESTAMPTZ DEFAULT now(),
    source TEXT DEFAULT 'live'
);
-- append-only: block in-place edits (dropping these rules would itself be a
-- visible, deliberate act — the point is no silent history rewriting)
CREATE OR REPLACE RULE mg_decisions_no_update AS ON UPDATE TO mg_decisions DO INSTEAD NOTHING;
CREATE OR REPLACE RULE mg_decisions_no_delete AS ON DELETE TO mg_decisions DO INSTEAD NOTHING;
"""


def parse_meta_from_filename(path):
    m = re.search(r"stock_selector_([A-Z]{2})_(\d{4}-\d{2}-\d{2})_judgment\.json$",
                  os.path.basename(path))
    if not m:
        return None, None
    return m.group(1), m.group(2)


def log_file(cur, path, source):
    country, as_of = parse_meta_from_filename(path)
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    doc = json.loads(raw)
    as_of = doc.get("as_of") or as_of
    if not as_of:
        print(f"  SKIP {path}: no as_of derivable")
        return 0
    decision = doc.get("decision") or {}
    weights = {w["ticker"]: w for w in (decision.get("portfolio_weights") or [])}
    rows = []
    for b in decision.get("buys") or []:
        w = weights.get(b["ticker"], {})
        rows.append(dict(action="BUY", ticker=b["ticker"], size=b.get("size"),
                         weight_pct=w.get("weight_pct"), rationale=b.get("why"),
                         flips_when=None, category=b.get("category")))
    for n in decision.get("nos") or []:
        rows.append(dict(action="NO", ticker=n["ticker"], size=None, weight_pct=None,
                         rationale=None, flips_when=n.get("flips_when"), category=None))
    for e in decision.get("exits") or []:
        rows.append(dict(action=e.get("action", "EXIT"), ticker=e["ticker"], size=None,
                         weight_pct=None, rationale=e.get("why"), flips_when=None,
                         category=e.get("rule")))
    constraints = "; ".join(c["name"] for c in decision.get("constraints_shortlisted") or [])
    n_new = 0
    for r in rows:
        cur.execute("""
            INSERT INTO mg_decisions (as_of, country, ticker, action, size, weight_pct,
                                       category, rationale, flips_when, constraint_name,
                                       judgment_file, judgment_sha256, source)
            SELECT %(as_of)s, %(country)s, %(ticker)s, %(action)s, %(size)s, %(weight_pct)s,
                   %(category)s, %(rationale)s, %(flips_when)s, %(constraints)s,
                   %(file)s, %(sha)s, %(source)s
            WHERE NOT EXISTS (
                -- Identity is the DECISION (date, country, ticker, action, size),
                -- never the file hash or the source label. Keying on
                -- judgment_sha256 silently double-logged every decision:
                -- portfolio_construct.py writes weights BACK into the judgment
                -- file, so a re-run hashes differently and re-inserted the same
                -- call (142 rows for 71 real decisions, every count 2x wrong).
                -- A genuinely revised decision (action or size changed) still
                -- lands as a new row, which is the append-only behaviour we want.
                SELECT 1 FROM mg_decisions
                WHERE as_of = %(as_of)s AND ticker = %(ticker)s
                  AND action = %(action)s
                  AND COALESCE(size,'') = COALESCE(%(size)s,'')
                  AND COALESCE(country,'') = COALESCE(%(country)s,''))
        """, dict(as_of=as_of, country=country or doc.get("country", "IN"),
                  constraints=constraints, file=os.path.basename(path), sha=sha,
                  source=source, **r))
        n_new += cur.rowcount
    print(f"  {os.path.basename(path)}: {len(rows)} decisions, {n_new} newly logged")
    return n_new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("judgment_files", nargs="+")
    ap.add_argument("--source", default="live", choices=["live", "backfill"])
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(DDL)
    total = 0
    for path in args.judgment_files:
        total += log_file(cur, path, args.source)
    conn.commit()
    cur.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT as_of) AS d FROM mg_decisions")
    row = cur.fetchone()
    print(f"Decision log: {row['n']} rows across {row['d']} decision dates. +{total} new.")
    conn.close()


if __name__ == "__main__":
    main()
