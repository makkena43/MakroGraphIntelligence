#!/usr/bin/env python3
"""Check the selector watchlist (mg_selector_watchlist) against newly ingested
filings and record decisions in mg_trigger_check_runs.

Usage:
  python scripts/stock_report/check_triggers.py            # since last run
  python scripts/stock_report/check_triggers.py --since 2026-07-01

Decision semantics per category:
  CORE_BUY    -> standing BUY (no pattern; act per position guidance)
  TIMING_BUY  -> BUY only when its filing trigger FIRES (action line = what to confirm)
  WATCH       -> stays WATCH until its catalyst FIRES

Also usable as a library: run_trigger_check(conn, since) -> list[dict]
(the FastAPI endpoint /api/selector/check-triggers calls this).
Alert freshness is bounded by mg_documents ingestion freshness.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

import psycopg2
import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
STATE = os.path.join(ROOT, "data", "reports", ".trigger_check_state.json")


def run_trigger_check(conn, since: str, country: str = "IN") -> list[dict]:
    """Evaluate every active watchlist entry; insert a row per stock into
    mg_trigger_check_runs; return the decisions."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT MAX(filed_at) AS mx FROM mg_documents WHERE country=%s", (country,))
    data_through = cur.fetchone()["mx"]

    cur.execute("""SELECT * FROM mg_selector_watchlist
                   WHERE active AND country=%s ORDER BY category, ticker""", (country,))
    entries = cur.fetchall()
    results = []
    for e in entries:
        fired, matches = False, []
        if e["pattern"]:
            col = "title" if e["pattern_field"] == "title" else "raw_text"
            cur.execute(f"""
                SELECT filed_at::date AS d, LEFT(title, 100) AS title
                FROM mg_documents
                WHERE ticker=%s AND country=%s AND filed_at > %s AND {col} ~* %s
                ORDER BY filed_at DESC LIMIT 5
            """, (e["ticker"], country, since, e["pattern"]))
            matches = [dict(r, d=str(r["d"])) for r in cur.fetchall()]
            fired = bool(matches)

        if e["category"] == "CORE_BUY":
            decision = "BUY (standing Core) — " + (e["action"] or "")
        elif fired and e["category"] == "TIMING_BUY":
            decision = "TRIGGER FIRED → BUY after confirming: " + (e["action"] or "")
        elif fired:  # WATCH fired
            decision = "CATALYST FIRED → upgrade to BUY (trade sizing) after confirming: " + (e["action"] or "")
        elif e["category"] == "TIMING_BUY":
            decision = "WAIT — trigger not fired (" + (e["trigger_desc"] or "") + ")"
        else:
            decision = "WATCH — catalyst not fired (" + (e["trigger_desc"] or "") + ")"

        cur.execute("""
            INSERT INTO mg_trigger_check_runs
                (since_date, country, ticker, category, fired, n_matches, matched, decision)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (since, country, e["ticker"], e["category"], fired,
              len(matches), json.dumps(matches), decision))
        results.append({
            "ticker": e["ticker"], "category": e["category"], "theme": e["theme"],
            "trigger": e["trigger_desc"], "fired": fired, "matches": matches,
            "decision": decision, "action": e["action"],
        })
    conn.commit()
    return {"run_date": date.today().isoformat(), "since": since,
            "data_through": str(data_through), "results": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="check filings after this date (YYYY-MM-DD)")
    ap.add_argument("--country", default="IN")
    args = ap.parse_args()

    if args.since:
        since = args.since
    elif os.path.exists(STATE):
        since = json.load(open(STATE)).get("last_run", "2026-07-05")
    else:
        since = "2026-07-05"

    conn = psycopg2.connect(host="localhost", dbname="makrograph", user="postgres")
    out = run_trigger_check(conn, since, args.country)
    conn.close()

    print(f"Trigger check {out['run_date']} — filings after {since} (data through {out['data_through']})")
    print("=" * 78)
    for r in out["results"]:
        mark = "🔔" if r["fired"] else ("✅" if r["category"] == "CORE_BUY" else "  ")
        print(f"{mark} {r['ticker']:12} [{r['category']:10}] {r['decision']}")
        for m in r["matches"]:
            print(f"      {m['d']}  {m['title']}")

    with open(STATE, "w") as f:
        json.dump({"last_run": date.today().isoformat(),
                   "checked_at": datetime.now().isoformat(timespec="seconds")}, f)


if __name__ == "__main__":
    main()
