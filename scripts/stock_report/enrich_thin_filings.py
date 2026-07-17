#!/usr/bin/env python3
"""Enrich thin filings by extracting text from their attachment PDFs.

Problem this solves (the Shakti-2022 lesson): many NSE disclosures land in
mg_documents as short cover letters — the scheme/order detail lives in the
attachment PDF, which the ingester never text-extracted. ~15k IN docs since
2020 have raw_text < 1500 chars. Scheme regexes and evidence scans can only
see what's in raw_text.

What it does: for targeted thin docs, downloads the attachment (pdf, or first
pdf inside a zip), extracts text with PyMuPDF (first 20 pages), APPENDS it to
raw_text under an [ATTACHMENT TEXT] marker (original text preserved), and
stamps attachment_enriched_at.

Usage (run before the monthly selector scan):
  python scripts/stock_report/enrich_thin_filings.py --watchlist            # active watchlist tickers
  python scripts/stock_report/enrich_thin_filings.py --tickers SHAKTIPUMP,DIXON
  python scripts/stock_report/enrich_thin_filings.py --all --max-docs 500   # broad backfill, capped

Polite to nsearchives (0.6s sleep, UA header); resumable (skips enriched docs).
"""
import argparse
import io
import time
import zipfile
import urllib.request

import fitz
import psycopg2
import psycopg2.extras

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
MARKER = "\n\n[ATTACHMENT TEXT — enriched]\n"


def extract_pdf_text(data: bytes, max_pages: int = 20) -> str:
    doc = fitz.open(stream=data, filetype="pdf")
    out = []
    for i in range(min(len(doc), max_pages)):
        out.append(doc.load_page(i).get_text())
    return "\n".join(out).strip()


def fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tickers", help="comma-separated tickers")
    g.add_argument("--watchlist", action="store_true", help="active mg_selector_watchlist tickers")
    g.add_argument("--all", action="store_true", help="all IN thin docs (use --max-docs!)")
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--max-docs", type=int, default=200)
    ap.add_argument("--thin-below", type=int, default=1500)
    args = ap.parse_args()

    conn = psycopg2.connect(host="localhost", dbname="makrograph", user="postgres")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("ALTER TABLE mg_documents ADD COLUMN IF NOT EXISTS attachment_enriched_at TIMESTAMPTZ")
    conn.commit()

    if args.tickers:
        ticks = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.watchlist:
        cur.execute("SELECT DISTINCT ticker FROM mg_selector_watchlist WHERE active")
        ticks = [r["ticker"] for r in cur.fetchall()]
    else:
        ticks = None

    tick_clause = "AND ticker = ANY(%s)" if ticks else ""
    params = [args.since, args.thin_below] + ([ticks] if ticks else []) + [args.max_docs]
    cur.execute(f"""
        SELECT id, ticker, url, filed_at::date AS d
        FROM mg_documents
        WHERE country = 'IN' AND filed_at >= %s
          AND LENGTH(COALESCE(raw_text,'')) < %s
          AND url ~* '\\.(pdf|zip)$'
          AND attachment_enriched_at IS NULL
          {tick_clause}
        ORDER BY filed_at DESC
        LIMIT %s
    """, params)
    docs = cur.fetchall()
    print(f"Enriching {len(docs)} thin docs" + (f" for {len(ticks)} tickers" if ticks else ""))

    ok = fail = 0
    for i, dd in enumerate(docs):
        try:
            data = fetch(dd["url"])
            if dd["url"].lower().endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")]
                    if not pdfs:
                        raise ValueError("no pdf in zip")
                    data = z.read(pdfs[0])
            text = extract_pdf_text(data)
            if len(text) > 200:
                cur.execute("""
                    UPDATE mg_documents
                    SET raw_text = COALESCE(raw_text,'') || %s || %s,
                        attachment_enriched_at = NOW()
                    WHERE id = %s
                """, (MARKER, text[:400000], dd["id"]))
                ok += 1
            else:
                # scanned/image PDF — mark attempted so we don't loop on it
                cur.execute("UPDATE mg_documents SET attachment_enriched_at = NOW() WHERE id = %s",
                            (dd["id"],))
                fail += 1
        except Exception:
            cur.execute("UPDATE mg_documents SET attachment_enriched_at = NOW() WHERE id = %s",
                        (dd["id"],))
            fail += 1
        if i % 20 == 19:
            conn.commit()
            print(f"  {i+1}/{len(docs)} (ok={ok} thin/fail={fail})", flush=True)
        time.sleep(0.6)
    conn.commit()
    print(f"Done: {ok} enriched, {fail} unextractable/failed (marked, won't retry)")
    conn.close()


if __name__ == "__main__":
    main()
