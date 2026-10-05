"""Explicit, bounded text extraction for documents the reader cannot read yet (WP1).

Read-only retrieval never parses PDFs or downloads anything.  This module is
the separate, explicitly invoked step that turns locally available originals
(``mg_documents.local_path``) into versioned text artifacts:

* bounded by ``max_docs`` (deterministic order: ticker, then publication time,
  then document id) and reports every document it deferred;
* local files only - a missing original is recorded as ``MISSING_ORIGINAL``
  (downloading requires a separately authorised ingestion run);
* recoverable failures stay retryable (``OCR_REQUIRED``,
  ``PARSE_FAILED_RETRYABLE``) with a retry limit; originals are preserved.

The parser and optional OCR provider are injected by the caller, so this
module imports no parsing library and performs no network access.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from .text_artifacts import ExtractionStatus, TextArtifactStore, extract_document

NEEDS_EXTRACTION = {ExtractionStatus.NOT_EXTRACTED.value, ExtractionStatus.OCR_REQUIRED.value,
                    ExtractionStatus.PARSE_FAILED_RETRYABLE.value}


def _resolve_path(local_path: str, text_root: Optional[str]) -> Optional[Path]:
    if not local_path or local_path == "UNSUPPORTED_FORMAT":
        return None
    p = Path(local_path)
    if not p.is_absolute() and text_root:
        p = Path(text_root) / p
    return p


def extract_missing_text(repo, tickers: list[str], country: str, as_of: datetime, store: TextArtifactStore,
                         parser, ocr=None, max_docs: int = 50, max_retries: int = 2,
                         text_root: Optional[str] = None) -> dict:
    """Extract text for unreadable documents of the given tickers.  Returns a report."""
    candidates = []
    for t in tickers:
        for d in repo.documents(t, country, as_of):
            if d.extraction_status in NEEDS_EXTRACTION:
                candidates.append((t, d.published_at or datetime.min, d.doc_id, d))
    candidates.sort(key=lambda c: (c[0], str(c[1]), c[2]))
    done, deferred = candidates[:max_docs], candidates[max_docs:]
    results = []
    for t, _, doc_id, d in done:
        res = extract_document(store, doc_id, _resolve_path(d.local_path, text_root), parser, ocr=ocr,
                               max_retries=max_retries)
        results.append({"ticker": t, "doc_id": doc_id, "title": d.title[:80], "status": res.status.value,
                        "version_id": res.version_id if res.status.usable else "",
                        "pages": f"{res.pages_with_text}/{res.pages_expected}" if res.pages_expected else "",
                        "reason": res.failure_reason[:160]})
    return {
        "candidates": len(candidates),
        "processed": len(done),
        "deferred": [{"ticker": t, "doc_id": i} for t, _, i, _ in deferred],
        "by_status": dict(Counter(r["status"] for r in results)),
        "results": results,
        "note": "local originals only; no network download was attempted",
    }
