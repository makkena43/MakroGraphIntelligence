#!/usr/bin/env python3
"""Re-fetch and re-extract documents with layout-aware text, fixing scrambled
raw_text (multi-column slide decks whose columns interleaved under the original
flow-order extraction).

WHY
---
Investor-presentation PDFs are multi-column slide decks. The original ingestion
used pdfplumber ``extract_text`` (flow order), which interleaves adjacent
columns — a capacity table becomes "Cells: 500 MW Inception manufacturing Cells:
75 MW" and a maker's operating-capacity evidence is unreadable, so a genuine
operating producer (e.g. Premier Energies) is quarantined at
physical_evidence_count<=1. PyMuPDF block extraction ordered by column then
vertical position preserves reading order (see
src/makrograph/parser/pdf_parser.py::_pymupdf_page_text_layout). India source
PDFs are deleted after ingestion, so this re-fetches from the stored NSE URL.

SAFE / RESUMABLE
----------------
- Only REPLACES raw_text when the fresh layout extraction is non-empty and not a
  drastic shrink (>=60% of the old length) — a failed/short fetch never destroys
  existing text.
- Marks processing_status so reruns skip done docs; fetch failures (deleted/old
  URLs) are marked and left with their original text.
- Polite: browser headers + cookie prime, delay + retry/backoff, per-run budget.
- filed_at is untouched — point-in-time integrity preserved.

USAGE
    python scripts/policy/reextract_documents.py --country IN \
        --filing-types "Investor Presentation" [--tickers KAYNES,PREMIERENE] \
        [--max-fetches 500] [--delay 1.2] [--limit N]
"""
import argparse
import io
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_DONE = "reextract_layout_v2"
_FAIL = "reextract_failed_v2"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept": "application/pdf,*/*",
                      "Referer": "https://www.nseindia.com/"})
    try:
        s.get("https://www.nseindia.com/", timeout=15)   # prime cookies
    except Exception:
        pass
    return s


def _fetch(sess: requests.Session, url: str, retries: int = 2) -> bytes | None:
    for attempt in range(retries + 1):
        try:
            r = sess.get(url, timeout=45)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                return r.content
            if r.status_code in (403, 429):
                time.sleep(8 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(4 * (attempt + 1))
    return None


def _layout_text(pdf_bytes: bytes, max_pages: int = 500) -> str:
    import fitz
    from src.makrograph.parser.pdf_parser import _pymupdf_page_text_layout
    fitz.TOOLS.mupdf_display_errors(False)
    doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    parts = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        t = _pymupdf_page_text_layout(page)
        if t.strip():
            parts.append(t)
    doc.close()
    text = "\n\n".join(parts)
    # Postgres text cannot store NUL; strip it and other C0 control chars
    # (except tab/newline) that malformed PDFs occasionally emit.
    return text.replace("\x00", "").translate(
        {c: None for c in range(32) if c not in (9, 10, 13)})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="IN")
    ap.add_argument("--filing-types", default="Investor Presentation")
    ap.add_argument("--tickers", default=None, help="comma-separated ticker allowlist")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-fetches", type=int, default=500)
    ap.add_argument("--delay", type=float, default=1.2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    types = [t.strip() for t in args.filing_types.split(",") if t.strip()]
    type_re = "|".join(t.replace(".", r"\.") for t in types)

    conn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    q = """SELECT id, url, UPPER(TRIM(ticker)) AS ticker, length(raw_text) AS old_len
           FROM mg_documents
           WHERE country=%s AND url IS NOT NULL AND filing_type ~* %s
             AND (processing_status IS DISTINCT FROM %s)
             AND (processing_status IS DISTINCT FROM %s)"""
    params = [args.country, type_re, _DONE, _FAIL]
    if args.tickers:
        q += " AND UPPER(TRIM(ticker)) = ANY(%s)"
        params.append([t.strip().upper() for t in args.tickers.split(",")])
    q += " ORDER BY filed_at DESC"
    if args.limit:
        q += f" LIMIT {args.limit}"
    cur.execute(q, params)
    docs = cur.fetchall()
    print(f"{len(docs)} candidate docs to re-extract (type~ {type_re}, country {args.country})")

    sess = _session()
    fetches = replaced = failed = skipped_short = 0
    w = conn.cursor()
    for d in docs:
        if fetches >= args.max_fetches:
            print(f"fetch budget {args.max_fetches} reached; stopping (resumable)."); break
        time.sleep(args.delay)
        pdf = _fetch(sess, d["url"])
        fetches += 1
        if not pdf:
            failed += 1
            if not args.dry_run:
                w.execute("UPDATE mg_documents SET processing_status=%s WHERE id=%s", (_FAIL, d["id"]))
                conn.commit()
            continue
        try:
            text = _layout_text(pdf)
        except Exception as e:
            failed += 1
            print(f"  parse fail {d['id']} ({d['ticker']}): {e}")
            continue
        old_len = d["old_len"] or 0
        if len(text) < 200 or (old_len and len(text) < 0.6 * old_len):
            skipped_short += 1   # keep original — fresh extraction is worse/empty
            if not args.dry_run:
                w.execute("UPDATE mg_documents SET processing_status=%s WHERE id=%s", (_DONE, d["id"]))
                conn.commit()
            continue
        if args.dry_run:
            print(f"  would replace {d['id']} ({d['ticker']}): {old_len} -> {len(text)} chars")
            continue
        w.execute("""UPDATE mg_documents SET raw_text=%s, word_count=%s, processing_status=%s,
                     updated_at=now() WHERE id=%s""",
                  (text, len(text.split()), _DONE, d["id"]))
        conn.commit()
        replaced += 1
        if replaced % 50 == 0:
            print(f"  replaced {replaced}, failed {failed}, fetches {fetches}", flush=True)
    print(f"DONE: fetches={fetches} replaced={replaced} failed={failed} kept_original={skipped_short}"
          + (" (dry-run)" if args.dry_run else ""))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
