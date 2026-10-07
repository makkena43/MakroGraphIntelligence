"""Results figures from the exchange's XBRL filings (NSE ``corporates-financial-results``).

Listed Indian companies file each results statement twice: as a PDF and as XBRL in the
exchange's ``in-bse-fin`` taxonomy.  The XBRL carries tagged values in rupees with explicit
period dates and scope, so nothing is read from page layout and no unit is inferred.

- Each filing holds the current period only (the quarter, and the year to date); comparatives
  come from the next filings.
- Point-in-time: a figure is available from the exchange's dissemination time of its XBRL
  filing, never earlier.
- The parser fails closed: a context without readable dates, a non-INR monetary unit, or a
  statement whose revenue + other income differs from total income yields no figures, and the
  reason is returned as an issue.

``reconcile_with_xbrl`` then makes XBRL the reference for every (metric, period, scope) it
covers: PDF-read figures that disagree are rejected (and counted, which measures the PDF
parser's accuracy for free).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .contracts import FinancialMeasurement, Metric, Scope, SourceDocument, Unit

XBRL_DOC_TYPE = "xbrl_results"
_IST = timezone(timedelta(hours=5, minutes=30))
_MAX_BYTES = 5_000_000          # results XBRL files are ~50-150 KB; anything far larger is not one

_NS_FIN = "in-bse-fin"
_CRORE = 1e7

# company-level P&L tags -> metric
_TAGS: dict[str, Metric] = {
    "RevenueFromOperations": Metric.REVENUE,
    "OtherIncome": Metric.OTHER_INCOME,
    "Expenses": Metric.TOTAL_EXPENSES,
    "CostOfMaterialsConsumed": Metric.COST_OF_MATERIALS,
    "PurchasesOfStockInTrade": Metric.PURCHASES_STOCK,
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": Metric.INVENTORY_CHANGE,
    "DepreciationDepletionAndAmortisationExpense": Metric.DEPRECIATION,
    "FinanceCosts": Metric.FINANCE_COST,
    "ExceptionalItemsBeforeTax": Metric.EXCEPTIONAL_ITEMS,
    "ProfitBeforeExceptionalItemsAndTax": Metric.PBT_PRE_EXCEPTIONAL,
    "ProfitBeforeTax": Metric.PBT,
    "TaxExpense": Metric.TAX,
    "ProfitLossForPeriod": Metric.PAT,
    "ProfitOrLossAttributableToOwnersOfParent": Metric.PAT_ATTRIBUTABLE,
    "ProfitOrLossAttributableToNonControllingInterests": Metric.NCI_PROFIT,
    "PaidUpValueOfEquityShareCapital": Metric.PAID_UP_CAPITAL,
}
_PER_SHARE_TAGS: dict[str, Metric] = {
    "FaceValueOfEquityShareCapital": Metric.FACE_VALUE,
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": Metric.BASIC_EPS,
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": Metric.DILUTED_EPS,
}
_SEGMENT_TAGS: dict[str, Metric] = {
    "SegmentRevenue": Metric.SEGMENT_REVENUE,
    "SegmentProfitLossBeforeTaxAndFinanceCosts": Metric.SEGMENT_RESULT,
}


def is_xbrl_document(doc: SourceDocument) -> bool:
    return doc.doc_type == XBRL_DOC_TYPE


def _local(tag: str) -> tuple[str, str]:
    """('{ns}Name' or 'prefix:Name') -> (namespace-or-prefix, Name)."""
    if tag.startswith("{"):
        ns, name = tag[1:].split("}", 1)
        return ns, name
    return "", tag


def _group(context_id: str) -> str:
    """'FourReportableSegmentRevenue01D' -> 'Four' (NSE context naming: group word, then detail)."""
    m = re.match(r"[A-Z][a-z]+", context_id or "")
    return m.group(0) if m else context_id


def _period_type(start: date, end: date) -> Optional[str]:
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    return {3: "Q", 6: "H", 9: "9M", 12: "FY"}.get(months)


def _parse_date(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


def parse_xbrl_results(doc: SourceDocument) -> tuple[list[FinancialMeasurement], list[str]]:
    """Measurements from one results XBRL filing.  Returns (rows, issues)."""
    text = doc.full_text() if hasattr(doc, "full_text") else (doc.text or "")
    if not text or len(text) > _MAX_BYTES or "<!ENTITY" in text[:5000]:
        return [], [f"{doc.doc_id}: not a results XBRL file (empty, oversized or declares entities); not used"]
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        return [], [f"{doc.doc_id}: XBRL not well-formed ({str(e)[:80]}); not used"]

    contexts: dict[str, dict] = {}
    units: dict[str, str] = {}
    facts: list[tuple[str, str, Optional[str], str]] = []      # (name, contextRef, unitRef, text)
    fin_ns = None
    for el in root:
        ns, name = _local(el.tag)
        if name == "context":
            cid = el.get("id", "")
            start = end = None
            dims: dict[str, str] = {}
            for sub in el.iter():
                _, sname = _local(sub.tag)
                if sname == "startDate":
                    start = _parse_date(sub.text)
                elif sname == "endDate":
                    end = _parse_date(sub.text)
                elif sname == "instant":
                    start = end = _parse_date(sub.text)
                elif sname in ("explicitMember", "typedMember"):
                    dims[sub.get("dimension", "")] = (sub.text or "").strip() or "".join(
                        (c.text or "").strip() for c in sub)
            contexts[cid] = {"start": start, "end": end, "dims": dims}
        elif name == "unit":
            measures = [(m.text or "").strip() for m in el.iter() if _local(m.tag)[1] == "measure"]
            units[el.get("id", "")] = "/".join(measures)
        else:
            if fin_ns is None and ns.endswith("/in-bse-fin"):
                fin_ns = ns
            if ns == fin_ns:
                facts.append((name, el.get("contextRef", ""), el.get("unitRef"), (el.text or "").strip()))

    if fin_ns is None:
        return [], [f"{doc.doc_id}: no in-bse-fin facts; not a results XBRL file"]

    def first(tag: str) -> Optional[str]:
        return next((v for n, _, _, v in facts if n == tag and v), None)

    nature = (first("NatureOfReportStandaloneConsolidated") or "").lower()
    scope = (Scope.CONSOLIDATED if nature.startswith("consolidated") else
             Scope.STANDALONE if nature.startswith("standalone") else Scope.UNKNOWN)
    symbol = first("Symbol")
    ticker = doc.ticker
    issues: list[str] = []
    if symbol and ticker and symbol.upper() != ticker.upper():
        issues.append(f"{doc.doc_id}: XBRL symbol {symbol} differs from {ticker} (renamed symbol?)")

    # segment names: DescriptionOfReportableSegment shares the segment's dimension member
    seg_names: dict[tuple, str] = {}
    for n, cref, _, v in facts:
        if n == "DescriptionOfReportableSegment" and cref in contexts:
            seg_names[tuple(sorted(contexts[cref]["dims"].items()))] = v

    # The filing declares each context group's reporting period ("OneD": the quarter, "FourD": the year to
    # date).  Context period elements are not reliable: NSE files leave "OneD"/"FourD" undefined or give the
    # year-to-date contexts the quarter's dates.  A fact's period is its group's declared period.
    declared: dict[str, dict] = {}
    for n, cref, _, v in facts:
        if n in ("DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod"):
            g = _group(cref)
            declared.setdefault(g, {})["start" if "Start" in n else "end"] = _parse_date(v)

    def period(cref: str) -> tuple[Optional[date], Optional[date]]:
        d = declared.get(_group(cref))
        if d and d.get("start") and d.get("end"):
            return d["start"], d["end"]
        if declared:
            return None, None           # declarations exist but not for this group: unknown period
        ctx = contexts.get(cref) or {}
        return ctx.get("start"), ctx.get("end")

    rows: list[FinancialMeasurement] = []
    totals: dict[tuple, float] = {}
    when = doc.published_at
    for n, cref, uref, v in facts:
        metric = _TAGS.get(n) or _PER_SHARE_TAGS.get(n) or _SEGMENT_TAGS.get(n)
        if metric is None and n not in _AUX_TAGS:
            continue
        start, end = period(cref)
        if not start or not end:
            issues.append(f"{doc.doc_id}: {n} in context {cref} without a declared period; not used")
            continue
        ctx = contexts.get(cref) or {"dims": {}}
        ptype = _period_type(start, end)
        if ptype is None:
            continue
        try:
            val = float(v.replace(",", ""))
        except ValueError:
            continue
        unit_name = units.get(uref or "", "")
        segment = ""
        if n in _SEGMENT_TAGS:
            segment = seg_names.get(tuple(sorted(ctx["dims"].items())), "")
            if not segment:
                continue
        elif ctx["dims"]:
            continue                       # company-level tags only from contexts without dimensions
        if n in _PER_SHARE_TAGS:
            if "INR" not in unit_name:
                continue
            unit, value = Unit.INR_PER_SHARE, val
        else:
            if unit_name != "iso4217:INR":
                issues.append(f"{doc.doc_id}: {n} in unit {unit_name or '?'}, not INR; not used")
                continue
            unit, value = Unit.INR_CRORE, val / _CRORE
        if n in _AUX_TAGS:
            if not ctx["dims"]:
                totals[(n, ptype, end)] = value
            continue
        rows.append(FinancialMeasurement(
            ticker=ticker, metric=metric, period_end=end, period_type=ptype, value=value, unit=unit,
            scope=scope, doc_id=doc.doc_id, available_at=when, source="reported", display_unit=1e-7,
            integrity="unchecked", evidence_id=f"xbrl:{doc.doc_id}:{n}:{cref}", quote=f"XBRL {n} = {v}",
            segment=segment))

    _check_identities(rows, totals, doc.doc_id, issues)
    return rows, issues


# reported totals used only to check the statement's own arithmetic
_AUX_TAGS = ("Income", "ProfitLossForPeriodFromContinuingOperations", "ProfitLossFromDiscontinuedOperationsAfterTax",
             "EmployeeBenefitExpense", "OtherExpenses", "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod")


def _check_identities(rows: list[FinancialMeasurement], totals: dict, doc_id: str, issues: list[str]) -> None:
    """The statement's arithmetic, per period.  A figure in a passed identity is ``validated``; the
    figures of a failed identity are ``unresolved`` (kept out of the series and never used as the
    reference for PDF figures).  Nothing is repaired: a sign the filer dropped stays an error."""
    by: dict[tuple, FinancialMeasurement] = {(r.metric, r.period_type, r.period_end): r for r in rows if not r.segment}
    periods = {(r.period_type, r.period_end) for r in rows if not r.segment}
    passed, failed = set(), set()

    def ok(a: float, b: float) -> bool:
        return abs(a - b) <= max(0.05, 0.005 * max(abs(a), abs(b)))

    for pt, end in periods:
        g = lambda m: by.get((m, pt, end))                                   # noqa: E731
        t = lambda n: totals.get((n, pt, end))                               # noqa: E731
        rev, oi, exp_ = g(Metric.REVENUE), g(Metric.OTHER_INCOME), g(Metric.TOTAL_EXPENSES)
        pre, exc, pbt, tax = g(Metric.PBT_PRE_EXCEPTIONAL), g(Metric.EXCEPTIONAL_ITEMS), g(Metric.PBT), g(Metric.TAX)
        pat, own, nci = g(Metric.PAT), g(Metric.PAT_ATTRIBUTABLE), g(Metric.NCI_PROFIT)
        income, cont, disc = t("Income"), t("ProfitLossForPeriodFromContinuingOperations"), \
            t("ProfitLossFromDiscontinuedOperationsAfterTax")
        checks = []
        if rev and income is not None:
            checks.append(("revenue + other income = total income", [rev, oi],
                           ok(rev.value + (oi.value if oi else 0.0), income)))
        parts = [g(Metric.COST_OF_MATERIALS), g(Metric.PURCHASES_STOCK), g(Metric.INVENTORY_CHANGE),
                 g(Metric.FINANCE_COST), g(Metric.DEPRECIATION)]
        emp, other = t("EmployeeBenefitExpense"), t("OtherExpenses")
        if exp_ and emp is not None and other is not None and g(Metric.DEPRECIATION) and g(Metric.FINANCE_COST):
            checks.append(("expense lines sum to total expenses", [p for p in parts if p] + [exp_],
                           ok(sum(p.value for p in parts if p) + emp + other, exp_.value)))
        if income is not None and exp_ and pre:
            checks.append(("total income - expenses = profit before exceptional items", [exp_, pre],
                           ok(income - exp_.value, pre.value)))
        if pre and pbt:
            checks.append(("profit before exceptional items + exceptional items = PBT", [pre, exc, pbt],
                           ok(pre.value + (exc.value if exc else 0.0), pbt.value)))
        if pbt and tax and cont is not None:
            checks.append(("PBT - tax = profit from continuing operations", [pbt, tax], ok(pbt.value - tax.value, cont)))
        if pat and cont is not None:
            # some filers add the share of associates' / joint ventures' profit after tax
            assoc = t("ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod") or 0.0
            checks.append(("continuing + discontinued = profit for the period", [pat],
                           ok(cont + (disc or 0.0), pat.value) or ok(cont + (disc or 0.0) + assoc, pat.value)))
        if pat and own and nci:
            checks.append(("owners' share + non-controlling share = profit for the period", [own, nci],
                           ok(own.value + nci.value, pat.value)))
        for name, members, good in checks:
            members = [m for m in members if m is not None]
            if good:
                passed.update(id(m) for m in members)
            else:
                failed.update(id(m) for m in members)
                issues.append(f"{doc_id}: XBRL fails '{name}' for {pt} {end}; those figures are not used")
    for r in rows:
        if id(r) in failed:
            r.integrity = "unresolved"
            r.integrity_notes.append("fails the XBRL statement's own arithmetic")
        elif id(r) in passed:
            r.integrity = "validated"


def _from_xbrl(r: FinancialMeasurement) -> bool:
    return r.evidence_id.startswith("xbrl:")


def reconcile_with_xbrl(rows: list[FinancialMeasurement], tol: float = 0.01,
                        ) -> tuple[list[FinancialMeasurement], dict]:
    """XBRL figures are the reference for the (metric, period type, period end, scope, segment) they
    cover.  A PDF-read figure for a covered key that disagrees by more than ``tol`` is marked
    ``rejected``; a re-filed (restated) figure is kept as it is.  Returns (rows, stats), where stats
    counts agreements and disagreements: the PDF parser's accuracy against XBRL."""
    ref: dict[tuple, FinancialMeasurement] = {}
    for r in rows:
        if _from_xbrl(r) and r.integrity == "validated":
            k = (r.metric, r.period_type, r.period_end, r.scope, r.segment)
            if k not in ref or (r.available_at and ref[k].available_at and r.available_at < ref[k].available_at):
                ref[k] = r
    stats = {"compared": 0, "agree": 0, "disagree": 0, "examples": []}
    out = []
    for r in rows:
        k = (r.metric, r.period_type, r.period_end, r.scope, r.segment)
        x = ref.get(k)
        if x is None or _from_xbrl(r) or r.restated or not (
                x.available_at and r.available_at and x.available_at <= r.available_at):
            out.append(r)               # not covered, or filed before the XBRL was public
            continue
        stats["compared"] += 1
        if abs(r.value - x.value) <= tol * max(abs(x.value), 0.05):
            stats["agree"] += 1
            out.append(r)
        else:
            stats["disagree"] += 1
            if len(stats["examples"]) < 20:
                stats["examples"].append(f"{r.metric.value} {r.period_type} {r.period_end} {r.scope.value}: "
                                         f"PDF {r.value:g} ({r.doc_id}) vs XBRL {x.value:g} ({x.doc_id})")
            out.append(replace(r, integrity="rejected",
                               integrity_notes=r.integrity_notes + [f"disagrees with XBRL {x.value:g} ({x.doc_id})"]))
    return out, stats


def xbrl_document(ticker: str, entry: dict, text: str) -> SourceDocument:
    """A SourceDocument for one NSE results-API entry and its XBRL text (used by fetchers / tests)."""
    disseminated = None
    for key in ("exchdisstime", "broadCastDate", "filingDate"):
        s = entry.get(key)
        if s:
            for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
                try:
                    disseminated = datetime.strptime(s, fmt).replace(tzinfo=_IST)   # exchange times are IST
                    break
                except ValueError:
                    continue
        if disseminated:
            break
    m = re.search(r"/([^/]+)\.xml$", entry.get("xbrl", ""))
    return SourceDocument(doc_id=f"xbrl_{m.group(1) if m else entry.get('seqNumber', '')}", source_name="nse_xbrl",
                          ticker=ticker, doc_type=XBRL_DOC_TYPE, filing_type="XBRL Financial Results",
                          title=f"{entry.get('consolidated', '')} results {entry.get('fromDate', '')} to "
                                f"{entry.get('toDate', '')}",
                          url=entry.get("xbrl", ""), published_at=disseminated, text=text)
