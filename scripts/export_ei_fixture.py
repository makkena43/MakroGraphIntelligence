#!/usr/bin/env python
"""Export what the Earnings Inflection Detector reads for some tickers, as a fixture.

Read-only: uses the same read-only Postgres adapter as the detector
(EI_READONLY_DSN, default_transaction_read_only=on, SELECT only).  Nothing is
written to the database and nothing is downloaded.

The output directory is a FixtureRepository, so the exact same documents can
be assessed and diagnosed anywhere without database access:

    python scripts/export_ei_fixture.py --ticker SHAILY --ticker REFEX --as-of 2024-03-31
    python scripts/earnings_inflection.py --fixtures filings_export/2024-03-31 \
        --ticker SHAILY --as-of 2024-03-31 --diagnose

Layout:
    <out>/export.json          documents (metadata + per-document text file) and issuer metadata
    <out>/texts/<doc_id>.txt   the text the detector would read (raw_text or parsed .txt)
    <out>/originals/<doc_id>.pdf
                               with --with-pdfs: the stored PDF of documents that have NO text,
                               so the explicit --extract step can be tested on them

Only public exchange filings are exported.  No credentials are written.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from makrograph.earnings_inflection.pipeline import as_of_datetime  # noqa: E402
from makrograph.earnings_inflection.source_repository import PostgresReadOnlyRepository  # noqa: E402


def _iso(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def export(repo, tickers: list[str], as_of: str, out: Path, since: date | None = None,
           country: str = "IN", pdf_roots: list[Path] = (), with_pdfs: bool = False,
           max_pdf_mb: float = 25.0) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "texts").mkdir(exist_ok=True)
    cutoff = as_of_datetime(as_of)
    docs = repo.documents_for(tickers, country, cutoff)
    summary = {t: {"documents": 0, "with_text": 0, "pdf_only": 0, "pdf_copied": 0, "pdf_missing": 0}
               for t in tickers}
    rows = []
    for d in sorted(docs, key=lambda x: (x.ticker, x.filed_at or date.min, x.doc_id)):
        if since and d.filed_at and d.filed_at < since:
            continue
        s = summary.setdefault(d.ticker, {"documents": 0, "with_text": 0, "pdf_only": 0, "pdf_copied": 0,
                                          "pdf_missing": 0})
        s["documents"] += 1
        rec = {"doc_id": d.doc_id, "ticker": d.ticker, "country": d.country, "source_name": d.source_name,
               "company": d.company, "title": d.title, "doc_type": d.doc_type, "filing_type": d.filing_type,
               "url": d.url, "filed_at": _iso(d.filed_at), "published_at": _iso(d.published_at),
               "first_seen_at": _iso(d.first_seen_at), "content_hash": d.content_hash,
               "exported_text_source": d.text_source}
        text = d.full_text()
        if text and text.strip():
            s["with_text"] += 1
            (out / "texts" / f"{d.doc_id}.txt").write_text(text)
            rec["text_file"] = f"texts/{d.doc_id}.txt"
        elif d.local_path:
            s["pdf_only"] += 1
            if with_pdfs:
                src = next((c for c in [Path(d.local_path)] + [r / d.local_path for r in pdf_roots]
                            if c.is_file()), None)
                if src is None or src.stat().st_size > max_pdf_mb * 1e6:
                    s["pdf_missing"] += 1
                else:
                    (out / "originals").mkdir(exist_ok=True)
                    dst = out / "originals" / f"{d.doc_id}{src.suffix.lower() or '.pdf'}"
                    shutil.copyfile(src, dst)
                    rec["local_path"] = str(dst.relative_to(out))
                    s["pdf_copied"] += 1
        rows.append(rec)
    issuers = {}
    for t in tickers:
        try:
            issuers[t] = repo.issuer_metadata(t)
        except Exception:
            issuers[t] = {}
    payload = {"exported": {"as_of": as_of, "since": _iso(since), "tickers": tickers, "summary": summary},
               "issuers": issuers, "documents": rows}
    (out / "export.json").write_text(json.dumps(payload, indent=1, default=str))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", action="append", required=True, help="repeatable")
    ap.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents public by then are exported")
    ap.add_argument("--since", default="2021-01-01", help="skip documents filed before this date (default 2021-01-01)")
    ap.add_argument("--out", help="output directory (default filings_export/<as-of>)")
    ap.add_argument("--dsn-env", default="EI_READONLY_DSN")
    ap.add_argument("--text-root", help="where parsed <local_path>.txt files live, as for the detector")
    ap.add_argument("--with-pdfs", action="store_true", help="copy stored PDFs of documents that have no text")
    ap.add_argument("--pdf-root", action="append", default=[], help="directory local_path is relative to (repeatable)")
    args = ap.parse_args(argv)
    repo = PostgresReadOnlyRepository(dsn_env=args.dsn_env, text_root=args.text_root)
    out = Path(args.out or ROOT / "filings_export" / args.as_of)
    summary = export(repo, [t.upper() for t in args.ticker], args.as_of, out,
                     date.fromisoformat(args.since) if args.since else None,
                     pdf_roots=[Path(p) for p in args.pdf_root] + [ROOT], with_pdfs=args.with_pdfs)
    print(json.dumps(summary, indent=1))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
