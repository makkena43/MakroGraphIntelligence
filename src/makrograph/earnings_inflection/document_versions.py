"""Document content classification, publication provenance and version lineage.

* Classification looks at document *contents*, not only the coarse title or
  ``filing_type`` stored by the NSE/BSE fetchers.  A conference-call
  invitation is never treated as a transcript.
* Availability is the earliest moment the information was demonstrably
  public.  ``published_at`` (exchange dissemination timestamp) is preferred;
  a bare ``filed_at`` date is treated conservatively as available at the END
  of that day (IST).  Documents with neither are excluded from point-in-time
  runs and reported in coverage.
* Re-filed / restated documents are linked, never overwritten: the later
  version supersedes the earlier one for calculations, and both remain
  citable.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, time
from typing import Iterable, Optional

from .contracts import IST, DocumentKind, SourceDocument

# --- content signals --------------------------------------------------------

_SPEAKER_TURN = re.compile(r"^\s*(?:Moderator|Operator|Management|[A-Z][a-zA-Z.]+(?:\s[A-Z][a-zA-Z.]+){0,3})\s*:", re.M)
_TRANSCRIPT_MARKERS = re.compile(
    r"ladies and gentlemen|question[- ]and[- ]answer|first question is from|"
    r"next question is from|over to you|hand(?:ing)? (?:the )?(?:call|conference) over",
    re.I,
)
_INVITATION_MARKERS = re.compile(
    r"(?:invit(?:e|es|ation)|intimation).{0,120}(?:conference call|earnings call|concall|analyst(?:s)? (?:meet|call))|"
    r"dial[- ]in|universal access number|diamond ?pass|pre-?registration|"
    r"call details|schedule of (?:analyst|investor)",
    re.I | re.S,
)
_RESULTS_MARKERS = re.compile(
    r"statement of (?:standalone|consolidated)?\s*(?:audited|unaudited|un-audited)?\s*(?:standalone|consolidated)?\s*financial results|"
    r"(?:audited|unaudited|un-audited) financial results for the (?:quarter|year|half)|"
    r"limited review report",
    re.I,
)
_ANNUAL_REPORT_MARKERS = re.compile(
    r"directors'? report|board'?s report|management discussion and analysis|"
    r"corporate governance report|notice of (?:the )?\d+(?:st|nd|rd|th) annual general meeting|annual report",
    re.I,
)
_PRESENTATION_MARKERS = re.compile(r"investor presentation|earnings presentation|investor deck|safe harbou?r", re.I)
_ORDER_MARKERS = re.compile(
    r"(?:receipt|bagging|award|secured|received|won)\b.{0,60}\b(?:order|contract|letter of (?:award|intent))|"
    r"letter of (?:award|intent)|purchase order|work order",
    re.I | re.S,
)


def classify_document(doc: SourceDocument) -> tuple[DocumentKind, str]:
    """Return (kind, basis).  ``basis`` explains which evidence was used."""
    text = doc.full_text()
    head = text[:20000]
    title = f"{doc.title or ''} {doc.filing_type or ''} {doc.doc_type or ''}"

    if head.strip():
        speaker_turns = len(_SPEAKER_TURN.findall(head))
        transcript_hits = len(_TRANSCRIPT_MARKERS.findall(head))
        invitation = bool(_INVITATION_MARKERS.search(head))
        # A transcript has a dialogue structure; an invitation has call logistics
        # and no dialogue.  Invitations frequently say "earnings conference call"
        # which a title-only classifier mistakes for a transcript.
        if transcript_hits >= 1 and speaker_turns >= 4:
            return DocumentKind.EARNINGS_CALL_TRANSCRIPT, f"content:dialogue(turns={speaker_turns})"
        if invitation:
            return DocumentKind.EARNINGS_CALL_INVITATION, "content:call-logistics-no-dialogue"
        if _RESULTS_MARKERS.search(head):
            return DocumentKind.FINANCIAL_RESULTS, "content:results-statement"
        if _PRESENTATION_MARKERS.search(head):
            return DocumentKind.INVESTOR_PRESENTATION, "content:presentation"
        if len(_ANNUAL_REPORT_MARKERS.findall(head)) >= 2:
            return DocumentKind.ANNUAL_REPORT, "content:annual-report-sections"
        if _ORDER_MARKERS.search(head):
            return DocumentKind.ORDER_ANNOUNCEMENT, "content:order-language"
        return DocumentKind.OTHER_ANNOUNCEMENT, "content:no-specific-markers"

    # No text: title only, and explicitly labelled as such.
    t = title.lower()
    if "transcript" in t:
        return DocumentKind.EARNINGS_CALL_TRANSCRIPT, "title_only"
    if re.search(r"conference call|earnings call|analyst.{0,20}meet", t):
        return DocumentKind.EARNINGS_CALL_INVITATION, "title_only"
    if "result" in t:
        return DocumentKind.FINANCIAL_RESULTS, "title_only"
    if "annual report" in t:
        return DocumentKind.ANNUAL_REPORT, "title_only"
    if "presentation" in t:
        return DocumentKind.INVESTOR_PRESENTATION, "title_only"
    if re.search(r"\border\b|contract|letter of award", t):
        return DocumentKind.ORDER_ANNOUNCEMENT, "title_only"
    return DocumentKind.UNKNOWN, "title_only:unclassified"


# --- availability ----------------------------------------------------------

def availability(doc: SourceDocument) -> tuple[Optional[datetime], str]:
    if doc.published_at is not None:
        ts = doc.published_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
            return ts, "published_at(naive, assumed IST)"
        return ts, "published_at"
    if doc.filed_at is not None:
        return datetime.combine(doc.filed_at, time(23, 59, 59), tzinfo=IST), "filed_at(end-of-day IST, conservative)"
    return None, "unknown"


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# --- period key + lineage --------------------------------------------------

_PERIOD = re.compile(
    r"(?:quarter|year|half[- ]year|nine months|period)\s+ended\s+"
    r"(?:(\d{1,2})(?:st|nd|rd|th)?[\s./-]*([A-Za-z]{3,9}|\d{1,2})[\s.,/-]*(\d{4})|"
    r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4}))",
    re.I,
)


def primary_period_key(doc: SourceDocument) -> str:
    m = _PERIOD.search(doc.full_text()[:8000])
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(0).lower())


# "regrouped/reclassified" boilerplate appears in almost every Indian results
# note and is NOT a restatement signal.
_RESTATED = re.compile(r"\brestated\b|\brevised (?:results|financial)", re.I)


def link_versions(docs: Iterable[SourceDocument]) -> list[SourceDocument]:
    """Classify, stamp availability and link re-filed versions.

    Identical content (by normalised hash) is collapsed to the earliest
    available copy; the later duplicates are marked ``superseded_by`` the
    earliest because they add no new information.  Different content for the
    same (ticker, kind, primary period) is a revision: the later version
    supersedes the earlier one.
    """
    docs = list(docs)
    for d in docs:
        d.validate()
        d.kind, d.kind_basis = classify_document(d)
        d.available_at, d.availability_basis = availability(d)
        if not d.content_hash:
            d.content_hash = content_hash(d.full_text()) if d.full_text() else ""

    def _key(d):
        return (d.available_at is None, d.available_at or datetime.max.replace(tzinfo=IST), d.doc_id)

    by_hash = defaultdict(list)
    for d in docs:
        if d.content_hash:
            by_hash[d.content_hash].append(d)
    for group in by_hash.values():
        group.sort(key=_key)
        for dup in group[1:]:
            dup.superseded_by = group[0].doc_id

    groups = defaultdict(list)
    for d in docs:
        if d.superseded_by or d.kind not in (DocumentKind.FINANCIAL_RESULTS, DocumentKind.ANNUAL_REPORT):
            continue
        pk = primary_period_key(d)
        if pk:
            groups[(d.ticker, d.kind, pk)].append(d)
    for group in groups.values():
        group.sort(key=_key)
        for older, newer in zip(group, group[1:]):
            older.superseded_by = newer.doc_id
            newer.supersedes.append(older.doc_id)
    return docs


def is_restatement(doc: SourceDocument) -> bool:
    return bool(doc.supersedes) or bool(_RESTATED.search(doc.full_text()[:20000]))
