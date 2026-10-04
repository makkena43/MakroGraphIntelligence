"""Quote, unit, chronology, scope and reconciliation checks.

Issues are attached to records rather than silently dropping them.  Issues
prefixed ``FATAL:`` make a record unusable for assessment; others are
disclosed as caveats.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .contracts import Evidence, FinancialMeasurement, Metric, Modality, Scope, SourceDocument, Unit
from .extraction import fy_label_end


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def verify_quote(ev: Evidence, doc: SourceDocument) -> None:
    if _norm(ev.quote) not in _norm(doc.full_text()):
        ev.validation_issues.append("FATAL: quote not found verbatim in source document")


def check_units(ev: Evidence) -> None:
    q = ev.quantity
    if q is None:
        return
    if ev.metric in (Metric.EBITDA_MARGIN, Metric.MARGIN_GUIDANCE, Metric.UTILIZATION) and q.unit != Unit.PERCENT:
        ev.validation_issues.append(f"FATAL: {ev.metric.value} expects percent, got {q.unit.value}")
    if ev.metric == Metric.UTILIZATION and not (0 <= q.value <= 130):
        ev.validation_issues.append(f"FATAL: implausible utilisation {q.value}%")
    if ev.metric in (Metric.EBITDA_MARGIN, Metric.MARGIN_GUIDANCE) and not (-100 <= q.value <= 100):
        ev.validation_issues.append(f"FATAL: implausible margin {q.value}%")
    if q.unit in (Unit.INR_CRORE, Unit.USD_MN) and q.value <= 0:
        ev.validation_issues.append("FATAL: non-positive amount")
    if q.unit == Unit.USD_MN:
        ev.validation_issues.append("currency USD: not converted to INR (no dated FX series wired)")


def check_chronology(ev: Evidence, as_of: datetime) -> None:
    if ev.available_at is None:
        ev.validation_issues.append("FATAL: unknown publication time")
        return
    if ev.available_at > as_of:
        ev.validation_issues.append("FATAL: published after as-of (look-ahead)")
    if ev.modality == Modality.REALIZED and ev.period_label:
        end = fy_label_end(ev.period_label)
        if end and end > ev.available_at.date():
            # "We achieved X in FY26" published before FY26 ended -> really a forecast
            ev.validation_issues.append(f"realized claim for unfinished period {ev.period_label}; treated as assertion")
    if ev.target_period_label:
        end = fy_label_end(ev.target_period_label)
        if end and end < ev.available_at.date() and ev.modality in (Modality.FORWARD, Modality.CONDITIONAL):
            ev.validation_issues.append(f"forward statement about already-ended period {ev.target_period_label}")


def validate_evidence(evs: Iterable[Evidence], docs_by_id: dict[str, SourceDocument], as_of: datetime) -> list[Evidence]:
    out = []
    for ev in evs:
        doc = docs_by_id.get(ev.doc_id)
        if doc is None:
            ev.validation_issues.append("FATAL: source document missing")
        else:
            verify_quote(ev, doc)
        check_units(ev)
        check_chronology(ev, as_of)
        out.append(ev)
    return out


def validate_measurements(rows: Iterable[FinancialMeasurement], as_of: datetime) -> tuple[list[FinancialMeasurement], list[str]]:
    """Drop look-ahead rows and rows for periods that had not ended when published."""
    keep, issues = [], []
    for r in rows:
        if r.available_at is None or r.available_at > as_of:
            issues.append(f"{r.doc_id}: {r.metric.value} {r.period_end} excluded (not public by as-of)")
            continue
        if r.period_end > r.available_at.date():
            issues.append(f"{r.doc_id}: {r.metric.value} period {r.period_end} ends after publication; excluded")
            continue
        keep.append(r)
    return keep, issues


def reconcile(rows: list[FinancialMeasurement], tolerance: float = 0.02) -> list[str]:
    """Cross-checks within one document/period/scope.

    * Reported EBITDA vs revenue - (total expenses - D&A - finance cost)
    * PAT vs PBT - tax
    """
    issues = []
    grp = defaultdict(dict)
    for r in rows:
        grp[(r.doc_id, r.period_end, r.period_type, r.scope)][r.metric] = r.value
    for (doc_id, pe, pt, scope), m in grp.items():
        if Metric.PAT in m and Metric.PBT in m and Metric.TAX in m:
            exc = m.get(Metric.EXCEPTIONAL_ITEMS, 0.0)
            expected_candidates = [m[Metric.PBT] - m[Metric.TAX], m[Metric.PBT] + exc - m[Metric.TAX]]
            if not any(abs(m[Metric.PAT] - e) <= max(0.5, tolerance * abs(m[Metric.PAT])) for e in expected_candidates):
                issues.append(f"{doc_id} {pe} {pt} {scope.value}: PAT {m[Metric.PAT]} != PBT - tax")
        if Metric.EBITDA in m and Metric.REVENUE in m and Metric.TOTAL_EXPENSES in m and Metric.DEPRECIATION in m:
            calc = m[Metric.REVENUE] - (m[Metric.TOTAL_EXPENSES] - m[Metric.DEPRECIATION] - m.get(Metric.FINANCE_COST, 0.0))
            if abs(calc - m[Metric.EBITDA]) > max(0.5, tolerance * abs(m[Metric.EBITDA])):
                issues.append(f"{doc_id} {pe} {pt} {scope.value}: reported EBITDA {m[Metric.EBITDA]} vs computed {calc:.2f} "
                              f"(definitions may differ, e.g. other income)")
    return issues


def scope_conflicts(rows: Iterable[FinancialMeasurement]) -> list[str]:
    seen = defaultdict(set)
    for r in rows:
        seen[(r.metric, r.period_end, r.period_type)].add(r.scope)
    return [f"{k[0].value} {k[1]}: reported in {sorted(s.value for s in v)}; series uses one scope only"
            for k, v in seen.items() if len(v - {Scope.UNKNOWN}) > 1]
