"""Quote, unit, chronology, scope and reconciliation checks.

Issues are attached to records rather than silently dropping them.  Issues
prefixed ``FATAL:`` make a record unusable for assessment; others are
disclosed as caveats.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
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


class Integrity:
    VALIDATED = "validated"
    DEFINITION_DIFFERENCE = "definition_difference"   # explainable, labelled definition difference
    UNRESOLVED = "unresolved"                         # excluded from dependent calculations
    REJECTED = "rejected"
    ORDER = {"unchecked": 0, VALIDATED: 1, DEFINITION_DIFFERENCE: 2, UNRESOLVED: 3, REJECTED: 4}
    BLOCKING = (UNRESOLVED, REJECTED)


@dataclass
class ReconciliationResult:
    check: str
    doc_id: str
    period_end: date
    period_type: str
    scope: str
    status: str
    detail: str
    metrics: list[str] = field(default_factory=list)   # metrics whose rows the status applies to


def _tol(value: float, rows: list[FinancialMeasurement], rel: float) -> float:
    unit = max((r.display_unit or 0.0) for r in rows) if rows else 0.0
    return max(2 * (unit or 0.01), rel * abs(value))


def reconcile_structured(rows: list[FinancialMeasurement], rel_tolerance: float = 0.005
                         ) -> list[ReconciliationResult]:
    """Financial identities within one document / period / scope.

    Statuses are attached to the rows (``integrity``); rows left UNRESOLVED or
    REJECTED are preserved as reported facts but excluded from dependent
    calculations by ``FinancialSeries``.  Unrelated metrics are unaffected.
    """
    grp: dict = defaultdict(dict)
    for r in rows:
        grp[(r.doc_id, r.period_end, r.period_type, r.scope)].setdefault(r.metric, r)
    out: list[ReconciliationResult] = []

    def res(key, check, status, detail, metrics):
        out.append(ReconciliationResult(check, key[0], key[1], key[2], key[3].value, status, detail,
                                        [m.value for m in metrics]))

    for key, m in grp.items():
        v = {k: r.value for k, r in m.items()}
        g = lambda *ms: [m[x] for x in ms if x in m]  # noqa: E731
        M = Metric
        # exceptional items: sign proven from pre-exceptional PBT, never assumed
        if M.PBT_PRE_EXCEPTIONAL in v and M.PBT in v and M.EXCEPTIONAL_ITEMS in v:
            pre, pbt, exc = v[M.PBT_PRE_EXCEPTIONAL], v[M.PBT], v[M.EXCEPTIONAL_ITEMS]
            t = _tol(pbt, g(M.PBT_PRE_EXCEPTIONAL, M.PBT, M.EXCEPTIONAL_ITEMS), rel_tolerance)
            if abs(pre - exc - pbt) <= t:
                m[M.EXCEPTIONAL_ITEMS].integrity_notes.append("exceptional_sign=charge_positive")
                res(key, "exceptional_items", Integrity.VALIDATED, "PBT = pre-exceptional PBT - exceptional (charge)",
                    [M.EXCEPTIONAL_ITEMS])
            elif abs(pre + exc - pbt) <= t:
                m[M.EXCEPTIONAL_ITEMS].integrity_notes.append("exceptional_sign=gain_positive")
                res(key, "exceptional_items", Integrity.VALIDATED, "PBT = pre-exceptional PBT + exceptional (gain)",
                    [M.EXCEPTIONAL_ITEMS])
            else:
                res(key, "exceptional_items", Integrity.UNRESOLVED,
                    f"pre-exceptional PBT {pre:g}, exceptional {exc:g} and PBT {pbt:g} do not reconcile",
                    [M.EXCEPTIONAL_ITEMS, M.PBT_PRE_EXCEPTIONAL])
        if M.PAT in v and M.PBT in v and M.TAX in v:
            pat, pbt, tax = v[M.PAT], v[M.PBT], v[M.TAX]
            t = _tol(pat, g(M.PAT, M.PBT, M.TAX), rel_tolerance)
            if abs(pbt - tax - pat) <= t:
                res(key, "pat_pbt_tax", Integrity.VALIDATED, "PAT = PBT - tax", [M.PAT, M.PBT, M.TAX])
            else:
                res(key, "pat_pbt_tax", Integrity.UNRESOLVED,
                    f"PAT {pat:g} != PBT {pbt:g} - tax {tax:g} (difference {pbt - tax - pat:+.2f})",
                    [M.PAT, M.PBT, M.TAX, M.PAT_ATTRIBUTABLE])
        if M.PAT_ATTRIBUTABLE in v and M.NCI_PROFIT in v and M.PAT in v:
            t = _tol(v[M.PAT], g(M.PAT, M.PAT_ATTRIBUTABLE, M.NCI_PROFIT), rel_tolerance)
            if abs(v[M.PAT_ATTRIBUTABLE] + v[M.NCI_PROFIT] - v[M.PAT]) <= t:
                res(key, "parent_plus_nci", Integrity.VALIDATED, "owners + NCI = PAT", [M.PAT_ATTRIBUTABLE])
            else:
                res(key, "parent_plus_nci", Integrity.UNRESOLVED,
                    f"owners {v[M.PAT_ATTRIBUTABLE]:g} + NCI {v[M.NCI_PROFIT]:g} != PAT {v[M.PAT]:g}",
                    [M.PAT_ATTRIBUTABLE, M.NCI_PROFIT])
        if M.EBITDA in v and M.REVENUE in v and M.TOTAL_EXPENSES in v and M.DEPRECIATION in v:
            calc = v[M.REVENUE] - (v[M.TOTAL_EXPENSES] - v[M.DEPRECIATION] - v.get(M.FINANCE_COST, 0.0))
            t = _tol(v[M.EBITDA], g(M.EBITDA, M.REVENUE, M.TOTAL_EXPENSES), rel_tolerance)
            if abs(calc - v[M.EBITDA]) <= t:
                res(key, "ebitda_definition", Integrity.VALIDATED, "reported EBITDA matches revenue - operating costs",
                    [M.EBITDA])
            elif M.OTHER_INCOME in v and abs(calc + v[M.OTHER_INCOME] - v[M.EBITDA]) <= t:
                res(key, "ebitda_definition", Integrity.DEFINITION_DIFFERENCE,
                    "reported EBITDA includes other income; the consistent (operating) definition is used",
                    [M.EBITDA])
            else:
                res(key, "ebitda_definition", Integrity.UNRESOLVED,
                    f"reported EBITDA {v[M.EBITDA]:g} vs revenue - operating costs {calc:.2f}", [M.EBITDA])
        cf = (M.OPERATING_CASH_FLOW, M.INVESTING_CASH_FLOW, M.FINANCING_CASH_FLOW, M.NET_CHANGE_IN_CASH)
        if all(x in v for x in cf):
            t = _tol(v[M.NET_CHANGE_IN_CASH], g(*cf), rel_tolerance)
            ok = abs(v[cf[0]] + v[cf[1]] + v[cf[2]] - v[cf[3]]) <= t
            res(key, "cash_flow_identity", Integrity.VALIDATED if ok else Integrity.UNRESOLVED,
                "CFO + CFI + CFF = net change in cash" if ok else "cash-flow components do not sum", list(cf))
        if M.TOTAL_ASSETS in v and M.TOTAL_EQUITY_AND_LIABILITIES in v:
            t = _tol(v[M.TOTAL_ASSETS], g(M.TOTAL_ASSETS, M.TOTAL_EQUITY_AND_LIABILITIES), rel_tolerance)
            ok = abs(v[M.TOTAL_ASSETS] - v[M.TOTAL_EQUITY_AND_LIABILITIES]) <= t
            res(key, "balance_sheet_identity", Integrity.VALIDATED if ok else Integrity.UNRESOLVED,
                "assets = equity + liabilities" if ok else "balance sheet does not balance",
                [M.TOTAL_ASSETS, M.TOTAL_EQUITY_AND_LIABILITIES, M.BORROWINGS_CURRENT, M.BORROWINGS_NONCURRENT,
                 M.CASH] if not ok else [M.TOTAL_ASSETS, M.TOTAL_EQUITY_AND_LIABILITIES])
        for x in m.values():                                  # implausible values are rejected outright
            if x.metric in (M.REVENUE,) and x.value < 0:
                x.integrity = Integrity.REJECTED
                x.integrity_notes.append("negative revenue")

    by_key = {(r.doc_id, r.period_end, r.period_type, r.scope.value, r.metric.value): r for r in rows}
    for rr in out:
        for mv in rr.metrics:
            row = by_key.get((rr.doc_id, rr.period_end, rr.period_type, rr.scope, mv))
            if row is None:
                continue
            if Integrity.ORDER[rr.status] > Integrity.ORDER.get(row.integrity, 0):
                row.integrity = rr.status
            row.integrity_notes.append(f"{rr.check}: {rr.detail}")
    return out


def margin_conflicts(evidence: list[Evidence], series, tolerance_pp: float = 1.0) -> list[str]:
    """A stated margin that disagrees with the margin computed from the statement is
    flagged (both are preserved; the stated figure is not silently corrected)."""
    from .extraction import fy_label_end
    out = []
    for e in evidence:
        if not (e.usable and e.metric == Metric.EBITDA_MARGIN and e.quantity and e.modality == Modality.REALIZED
                and e.period_label):
            continue
        end = fy_label_end(e.period_label)
        ptype = "FY" if e.period_label.startswith("FY") else "H" if e.period_label.startswith("H") else "Q"
        computed = series.margin(end, ptype) if (end and series is not None) else None
        if computed is not None and abs(computed - e.quantity.value) > tolerance_pp:
            e.validation_issues.append(f"stated margin {e.quantity.value:g}% vs computed {computed:.2f}%")
            out.append(f"stated EBITDA margin {e.quantity.value:g}% for {e.period_label} conflicts with "
                       f"{computed:.2f}% computed from the reported statement (doc {e.doc_id}); both kept")
    return out


def reconcile(rows: list[FinancialMeasurement], tolerance: float = 0.005) -> list[str]:
    """Backward-compatible summary: one string per non-validated check."""
    return [f"{r.doc_id} {r.period_end} {r.period_type} {r.scope}: {r.check} {r.status}: {r.detail}"
            for r in reconcile_structured(rows, tolerance)
            if r.status not in (Integrity.VALIDATED,)]


def scope_conflicts(rows: Iterable[FinancialMeasurement]) -> list[str]:
    seen = defaultdict(set)
    for r in rows:
        seen[(r.metric, r.period_end, r.period_type)].add(r.scope)
    return [f"{k[0].value} {k[1]}: reported in {sorted(s.value for s in v)}; series uses one scope only"
            for k, v in seen.items() if len(v - {Scope.UNKNOWN}) > 1]
