"""Coverage accounting (WP1).

Expected coverage comes from the reporting cadence and SEBI results deadlines,
not from whatever documents happen to be present: a quarter whose results were
due before the as-of date but never parsed is a gap, reported as such.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Iterable, Optional

from .contracts import DocumentKind, SourceDocument
from .financial_series import prev_period_end

# SEBI LODR Reg. 33: 45 days after a quarter / half-year; 60 days for the last
# period of the financial year (audited results).
RESULTS_DEADLINE_DAYS = {"Q": 45, "H": 45}
YEAR_END_DEADLINE_DAYS = 60
DEFAULT_LOOKBACK = {"Q": 8, "H": 4}


def _latest_period_end_on_or_before(d: date, cadence: str) -> date:
    months = (3, 6, 9, 12) if cadence == "Q" else (3, 9)
    y, cands = d.year, []
    for yy in (y, y - 1):
        for m in months:
            end = date(yy, m, 30 if m in (6, 9) else 31)
            if end <= d:
                cands.append(end)
    return max(cands)


def results_deadline(period_end: date, cadence: str) -> date:
    days = YEAR_END_DEADLINE_DAYS if period_end.month == 3 else RESULTS_DEADLINE_DAYS[cadence]
    return period_end + timedelta(days=days)


def expected_result_periods(as_of: date, cadence: str, lookback: Optional[int] = None) -> list[date]:
    """Period ends whose results should be public by ``as_of`` (most recent ``lookback``)."""
    n = lookback or DEFAULT_LOOKBACK[cadence]
    end = _latest_period_end_on_or_before(as_of, cadence)
    while results_deadline(end, cadence) > as_of:
        end = prev_period_end(end, cadence)
    out = []
    for _ in range(n):
        out.append(end)
        end = prev_period_end(end, cadence)
    return sorted(out)


def result_period_coverage(as_of: date, cadence: str, parsed_period_ends: Iterable[date],
                           lookback: Optional[int] = None) -> dict:
    expected = expected_result_periods(as_of, cadence, lookback)
    parsed = sorted(set(parsed_period_ends))
    return {
        "cadence": {"Q": "quarterly", "H": "half-yearly"}[cadence],
        "expected": [d.isoformat() for d in expected],
        "parsed": [d.isoformat() for d in parsed if d >= expected[0]] if expected else [],
        "missing": [d.isoformat() for d in expected if d not in set(parsed)],
    }


def document_coverage(docs: list[SourceDocument]) -> dict:
    """Counts by content kind, text source and extraction status.

    An invitation is counted as an invitation (not a transcript), and a PDF that
    was downloaded but never extracted is counted as NOT_EXTRACTED, not as text.
    """
    def src(d):
        return d.text_source.split(":")[0] if d.text_source else "unknown"
    return {
        "by_kind": dict(Counter(d.kind.value for d in docs)),
        "transcripts": sum(1 for d in docs if d.kind == DocumentKind.EARNINGS_CALL_TRANSCRIPT),
        "call_invitations_only": sum(1 for d in docs if d.kind == DocumentKind.EARNINGS_CALL_INVITATION),
        "text_sources": dict(Counter(src(d) for d in docs)),
        "extraction_status": dict(Counter(d.extraction_status or "unknown" for d in docs)),
        "partial_extractions": [d.doc_id for d in docs if d.extraction_status == "PARTIAL"],
        "unreadable_documents": [{"doc_id": d.doc_id, "status": d.extraction_status, "title": d.title[:60]}
                                 for d in docs if not d.full_text().strip()],
    }
