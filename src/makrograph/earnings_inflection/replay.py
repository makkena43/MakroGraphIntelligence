"""Point-in-time replay: run the detector at successive as-of dates and keep compact snapshots.

Detection side only - it never reads prices or later outcomes.  Each run sees only the
documents public by its own as-of date (the pipeline's normal cutoff).  Scoring the
snapshots (lead time, false alarms, earnings delivery) is done in ``evaluation``.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Iterable, Optional

from .contracts import Assessment


def month_ends(start: date, end: date) -> list[date]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        d = date(y, m, calendar.monthrange(y, m)[1])
        if start <= d <= end:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _iso(x) -> Optional[str]:
    return x.isoformat() if x is not None else None


def snapshot(a: Assessment) -> dict:
    th = a.thesis
    mechs = []
    for t in (th.mechanisms if th else []):
        if t.stage.value == "none" and not t.leading_signal:
            continue
        v = t.validation
        mechs.append({
            "mechanism": t.mechanism.value, "stage": t.stage.value, "confidence": t.confidence,
            "leading_signal": t.leading_signal, "leading_signal_at": _iso(t.leading_signal_at),
            "earliest_defensible_at": _iso(t.earliest_defensible_at),
            "validation": None if v is None else {
                "outcome": v.outcome.value, "metric_basis": v.metric_basis, "expected_by": _iso(v.expected_by),
                "period_end": _iso(v.period_end), "observed": v.observed, "observed_at": _iso(v.observed_at)},
            "confirmation": t.confirmation, "stale": t.stale, "source_doc_ids": t.source_doc_ids,
        })
    return {
        "ticker": a.ticker, "as_of": a.as_of.date().isoformat(), "evidence_status": a.evidence_status.value,
        "status_rationale": a.status_rationale[:4],
        "thesis_stage": th.stage.value if th else "none", "thesis_confidence": th.confidence if th else "low",
        "thesis_earliest_defensible_at": _iso(th.earliest_defensible_at) if th else None,
        "stale": bool(th and th.stale), "last_known_status": th.last_known_status if th else "",
        "last_known_as_of": _iso(th.last_known_as_of) if th else None,
        "mechanisms": mechs,
        "research_detected": (a.research_summary or {}).get("detected", ""),
        "rules_version": (a.research_summary or {}).get("rules_version", ""),
        "investment_review": a.investment_review.status if a.investment_review else "not_started",
        "catalysts": [{
            "catalyst_id": c.catalyst_id, "kind": c.kind.value, "stage": c.stage.value,
            "first_public_at": _iso(c.first_public_at), "operating_change": c.operating_change,
            "contribution": c.contribution.status, "base_crore": c.contribution.base_crore,
            "share_of_ttm_ebitda": c.contribution.share_of_ttm_ebitda,
            "window": [_iso(c.window_start), _iso(c.window_end)],
            "milestones": [{"question": m.question, "status": m.status.value, "observed": m.observed,
                            "observed_at": _iso(m.observed_at)} for m in c.milestones],
            "stage_reasons": c.stage_reasons[:3]} for c in a.catalysts],
    }


def replay(pipeline, ticker: str, dates: Iterable[date]) -> list[dict]:
    out = []
    for d in dates:
        res = pipeline.run([ticker], d.isoformat())
        if res.assessments:
            out.append(snapshot(res.assessments[0]))
        else:
            out.append({"ticker": ticker, "as_of": d.isoformat(), "error": res.errors.get(ticker, "no assessment")})
    return out
