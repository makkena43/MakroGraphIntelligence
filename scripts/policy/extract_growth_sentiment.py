#!/usr/bin/env python3
"""Persist management growth-ambition and concall-tone signals into mg_signals.

Ingestion-level conviction layer for the greatest-constraint stock selection:
once a constraint is real and a company makes the product, whether management is
underwriting a 2x / 5x / 10x expansion (and how upbeat the last call was) is a
strong differentiator between makers. See src/makrograph/nlp/growth_sentiment.py
for the precision-first extractors.

Persists two signal_types (one row per document, entity_id NULL — the signal is
issuer/document level and joins to companies via mg_documents.ticker):
  - growth_ambition     signal_value=multiple, signal_unit=basis, direction='up',
                        perspective='company'|'market'
  - management_sentiment signal_value=net tone [-1,1], direction up/down/flat

Idempotent: deletes this document's prior rows of these two types before
reinserting, so reruns never accumulate. Point-in-time safe: filed_at is copied
from the document; nothing is backdated.

USAGE
    python scripts/policy/extract_growth_sentiment.py [--country IN] [--limit N]
        [--filing-types "Con. Call,Investor Presentation,..."]
"""
import argparse
import os
import re
import sys

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.makrograph.nlp.growth_sentiment import extract_growth_signals, concall_tone  # noqa: E402

_TEXT_CAP = 200000
# Document types that carry management commentary / guidance / call tone.
_DEFAULT_TYPES = ("Con. Call", "Investor Presentation", "Press Release", "Analyst",
                  "Financial Result", "Board Meeting", "order", "Earnings")
_CALL_TYPES = ("Con. Call", "Analyst", "Earnings")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="IN")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--filing-types", default=",".join(_DEFAULT_TYPES))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    types = [t.strip() for t in args.filing_types.split(",") if t.strip()]
    type_re = "|".join(re.escape(t) for t in types) if types else ".?"

    conn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")
    rconn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")
    rcur = rconn.cursor(name="growth_scan", cursor_factory=psycopg2.extras.RealDictCursor)
    rcur.itersize = 400
    limit = f" LIMIT {args.limit}" if args.limit else ""
    rcur.execute(f"""
        SELECT id, UPPER(TRIM(ticker)) AS ticker, filed_at::date AS filed, filing_type, raw_text
        FROM mg_documents
        WHERE country=%s AND ticker IS NOT NULL AND BTRIM(ticker) <> ''
          AND raw_text IS NOT NULL AND filing_type ~* %s
        ORDER BY filed_at{limit}
    """, (args.country, type_re))

    w = conn.cursor()
    seen = growth_rows = tone_rows = 0
    for row in rcur:
        seen += 1
        text = (row["raw_text"] or "")[:_TEXT_CAP]
        sigs = extract_growth_signals(text)
        is_call = bool(row["filing_type"] and any(c.lower() in row["filing_type"].lower() for c in _CALL_TYPES))
        tone = concall_tone(text) if is_call else None
        if not sigs and not tone:
            continue
        if args.dry_run:
            if seen <= 8 and sigs:
                print(f"  {row['ticker']}: {[(s.multiple, s.basis, s.perspective) for s in sigs[:3]]}")
            continue
        # Idempotent replace for this document.
        w.execute("DELETE FROM mg_signals WHERE document_id=%s AND signal_type IN "
                  "('growth_ambition','management_sentiment')", (row["id"],))
        if sigs:
            top = sigs[0]
            w.execute("""
                INSERT INTO mg_signals (document_id, entity_id, signal_type, signal_value,
                    signal_unit, direction, confidence, context_text, extracted_by,
                    filed_at, country, perspective)
                VALUES (%s,NULL,'growth_ambition',%s,%s,'up',%s,%s,'growth_sentiment_v1',%s,%s,%s)
            """, (row["id"], top.multiple, top.basis, top.confidence, top.context[:500],
                  row["filed"], args.country, top.perspective))
            growth_rows += 1
        if tone and (tone[1] + tone[2]) >= 3:   # need a few tone words to be meaningful
            net, pos, neg = tone
            direction = "up" if net > 0.1 else "down" if net < -0.1 else "flat"
            w.execute("""
                INSERT INTO mg_signals (document_id, entity_id, signal_type, signal_value,
                    signal_unit, direction, confidence, context_text, extracted_by,
                    filed_at, country, perspective)
                VALUES (%s,NULL,'management_sentiment',%s,'net_tone',%s,%s,%s,'growth_sentiment_v1',%s,%s,'company')
            """, (row["id"], net, direction, min(0.5 + (pos + neg) / 40.0, 0.9),
                  f"pos={pos} neg={neg}", row["filed"], args.country))
            tone_rows += 1
        if seen % 2000 == 0:
            conn.commit()
            print(f"  scanned {seen}, growth={growth_rows}, tone={tone_rows}", flush=True)
    conn.commit()
    print(f"DONE: {seen} docs, growth_ambition={growth_rows}, management_sentiment={tone_rows}"
          + (" (dry-run)" if args.dry_run else ""))
    rconn.close(); conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
