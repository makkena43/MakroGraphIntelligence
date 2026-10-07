"""Results figures from NSE XBRL filings.  The defects reproduced here are real: Rain Industries'
Q4 CY21 consolidated filing leaves the "OneD"/"FourD" contexts undefined, gives its year-to-date
segment contexts the quarter's dates, and reports owners' profit as +969.59 mn where the statement
prints (969.59)."""

from datetime import date, datetime

from makrograph.earnings_inflection.contracts import FinancialMeasurement, Metric, Scope, SourceDocument, Unit
from makrograph.earnings_inflection.xbrl_results import (XBRL_DOC_TYPE, parse_xbrl_results, reconcile_with_xbrl,
                                                          xbrl_document)

NS = ('xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
      'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
      'xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin"')

# (tag, OneD value, FourD value) in rupees; Rain Q4 CY21, consolidated
FACTS = [("RevenueFromOperations", 40260540000, 145267820000), ("OtherIncome", 546240000, 1931160000),
         ("Income", 40806780000, 147198980000), ("CostOfMaterialsConsumed", 20753630000, 66175520000),
         ("PurchasesOfStockInTrade", 4011060000, 12988190000),
         ("ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade", -3152720000, -5080550000),
         ("EmployeeBenefitExpense", 3400000000, 13000000000), ("FinanceCosts", 1192210000, 4789140000),
         ("DepreciationDepletionAndAmortisationExpense", 1998600000, 7981530000),
         ("OtherExpenses", 10363590000, 30574460000), ("Expenses", 38566770000, 134429330000),
         ("ProfitBeforeExceptionalItemsAndTax", 2240010000, 12769650000), ("ExceptionalItemsBeforeTax", 0, 0),
         ("ProfitBeforeTax", 2240010000, 12769650000), ("TaxExpense", 2957620000, 5828790000),
         ("ProfitLossForPeriodFromContinuingOperations", -717610000, 6940860000),
         ("ProfitLossFromDiscontinuedOperationsAfterTax", 0, 0),
         ("ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod", -5450000, -5450000),
         ("ProfitLossForPeriod", -723060000, 6935410000),
         ("ProfitOrLossAttributableToOwnersOfParent", 969590000, 5801580000),      # the filer's sign error (Q)
         ("ProfitOrLossAttributableToNonControllingInterests", 246530000, 1133830000)]


def filing(facts=FACTS, nature="Consolidated", unit="iso4217:INR", declare=True):
    ctx = ('<xbrli:context id="FourReportableSegmentRevenue01D"><xbrli:entity><xbrli:identifier scheme="s">RAIN'
           '</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2021-10-01</xbrli:startDate>'
           '<xbrli:endDate>2021-12-31</xbrli:endDate></xbrli:period><xbrli:scenario><xbrldi:explicitMember '
           'dimension="in-bse-fin:ReportableSegmentsAxis">in-bse-fin:Seg01</xbrldi:explicitMember></xbrli:scenario>'
           '</xbrli:context>')
    body = [f'<xbrli:unit id="INR"><xbrli:measure>{unit}</xbrli:measure></xbrli:unit>', ctx,
            f'<in-bse-fin:NatureOfReportStandaloneConsolidated contextRef="OneD">{nature}'
            '</in-bse-fin:NatureOfReportStandaloneConsolidated>',
            '<in-bse-fin:Symbol contextRef="OneD">RAIN</in-bse-fin:Symbol>',
            '<in-bse-fin:DescriptionOfReportableSegment contextRef="FourReportableSegmentRevenue01D">Carbon'
            '</in-bse-fin:DescriptionOfReportableSegment>',
            '<in-bse-fin:SegmentRevenue contextRef="FourReportableSegmentRevenue01D" unitRef="INR">104989600000'
            '</in-bse-fin:SegmentRevenue>']
    if declare:
        body += ['<in-bse-fin:DateOfStartOfReportingPeriod contextRef="OneD">2021-10-01</in-bse-fin:DateOfStartOfReportingPeriod>',
                 '<in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2021-12-31</in-bse-fin:DateOfEndOfReportingPeriod>',
                 '<in-bse-fin:DateOfStartOfReportingPeriod contextRef="FourD">2021-01-01</in-bse-fin:DateOfStartOfReportingPeriod>',
                 '<in-bse-fin:DateOfEndOfReportingPeriod contextRef="FourD">2021-12-31</in-bse-fin:DateOfEndOfReportingPeriod>']
    for tag, q, fy in facts:
        body.append(f'<in-bse-fin:{tag} contextRef="OneD" unitRef="INR" decimals="-6">{q}.00</in-bse-fin:{tag}>')
        body.append(f'<in-bse-fin:{tag} contextRef="FourD" unitRef="INR" decimals="-6">{fy}.00</in-bse-fin:{tag}>')
    return f'<?xml version="1.0" encoding="UTF-8"?><xbrli:xbrl {NS}>{"".join(body)}</xbrli:xbrl>'


def doc(text, when=datetime(2022, 2, 25, 20, 56)):
    return SourceDocument(doc_id="xbrl_x", source_name="nse_xbrl", ticker="RAIN", doc_type=XBRL_DOC_TYPE,
                          published_at=when, text=text)


def get(rows, metric, ptype, segment=""):
    return next(r for r in rows if r.metric == metric and r.period_type == ptype and r.segment == segment)


def test_periods_come_from_the_declared_dates_not_the_context_elements():
    rows, _ = parse_xbrl_results(doc(filing()))
    assert get(rows, Metric.REVENUE, "Q").value == 4026.054 and get(rows, Metric.REVENUE, "Q").period_end == date(2021, 12, 31)
    assert round(get(rows, Metric.REVENUE, "FY").value, 3) == 14526.782
    # the year-to-date segment context carries the quarter's dates; its group declares the year
    seg = get(rows, Metric.SEGMENT_REVENUE, "FY", "Carbon")
    assert round(seg.value, 2) == 10498.96 and seg.scope == Scope.CONSOLIDATED
    assert get(rows, Metric.REVENUE, "Q").unit == Unit.INR_CRORE


def test_a_figure_that_breaks_the_statement_arithmetic_is_unresolved_not_repaired():
    rows, issues = parse_xbrl_results(doc(filing()))
    owners = get(rows, Metric.PAT_ATTRIBUTABLE, "Q")
    assert owners.integrity == "unresolved" and owners.value > 0           # kept as filed, never sign-flipped
    assert any("owners' share + non-controlling share" in i for i in issues)
    assert get(rows, Metric.PAT, "Q").integrity == "validated"              # continuing + associates = PAT
    assert get(rows, Metric.PAT_ATTRIBUTABLE, "FY").integrity == "validated"
    for m in (Metric.REVENUE, Metric.DEPRECIATION, Metric.FINANCE_COST, Metric.PBT, Metric.TAX):
        assert get(rows, m, "Q").integrity == "validated"


def test_no_declared_period_and_no_context_dates_means_no_figures():
    rows, issues = parse_xbrl_results(doc(filing(declare=False)))
    assert not [r for r in rows if not r.segment] and any("without a declared period" in i for i in issues)


def test_a_non_rupee_unit_is_not_used():
    rows, issues = parse_xbrl_results(doc(filing(unit="iso4217:USD")))
    assert not [r for r in rows if r.unit == Unit.INR_CRORE] and any("not INR" in i for i in issues)


def test_entities_are_refused():
    rows, issues = parse_xbrl_results(doc('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>'))
    assert rows == [] and issues


def pdf_row(value, when, metric=Metric.REVENUE):
    return FinancialMeasurement(ticker="RAIN", metric=metric, period_end=date(2021, 12, 31), period_type="Q",
                                value=value, unit=Unit.INR_CRORE, scope=Scope.CONSOLIDATED, doc_id="pdf",
                                available_at=when)


def test_reconciliation_rejects_disagreeing_pdf_figures_only_after_the_xbrl_was_public():
    xrows, _ = parse_xbrl_results(doc(filing()))
    misread_later = pdf_row(402.6054, datetime(2022, 5, 4))              # a lost digit in a later comparative
    agreeing_later = pdf_row(4026.05, datetime(2022, 5, 4))
    misread_before = pdf_row(402.6054, datetime(2022, 2, 25, 20, 50))     # filed minutes before the XBRL
    out, stats = reconcile_with_xbrl(xrows + [misread_later, agreeing_later, misread_before])
    by = {(id(r)): r for r in out}
    assert next(r for r in out if r.available_at == datetime(2022, 5, 4) and r.value < 1000).integrity == "rejected"
    assert next(r for r in out if r.value == 4026.05).integrity != "rejected"
    assert next(r for r in out if r.available_at == datetime(2022, 2, 25, 20, 50)).integrity != "rejected"
    assert stats["compared"] == 2 and stats["disagree"] == 1 and by


def test_unresolved_xbrl_figures_are_never_the_reference():
    xrows, _ = parse_xbrl_results(doc(filing()))
    pdf_owners = pdf_row(-96.959, datetime(2022, 5, 4), Metric.PAT_ATTRIBUTABLE)   # the statement's true sign
    out, stats = reconcile_with_xbrl(xrows + [pdf_owners])
    assert next(r for r in out if r.doc_id == "pdf").integrity != "rejected" and stats["compared"] == 0


def test_dissemination_time_dates_the_document():
    d = xbrl_document("RAIN", {"xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_1_2_3.xml",
                               "exchdisstime": "25-Feb-2022 20:56:04", "filingDate": "25-Feb-2022 20:53",
                               "consolidated": "Consolidated", "fromDate": "01-Oct-2021", "toDate": "31-Dec-2021"},
                      filing())
    assert d.published_at.isoformat() == "2022-02-25T20:56:04+05:30" and d.doc_id == "xbrl_INDAS_1_2_3"


def test_pipeline_uses_xbrl_unless_disabled(tmp_path):
    import json
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    fx = tmp_path / "RAIN"
    (fx / "xbrl").mkdir(parents=True)
    (fx / "xbrl" / "q.xml").write_text(filing())
    (fx / "xbrl_index.json").write_text(json.dumps({"documents": [{
        "doc_id": "xbrl_q", "ticker": "RAIN", "source_name": "nse_xbrl", "doc_type": XBRL_DOC_TYPE,
        "filing_type": "XBRL Financial Results", "published_at": "2022-02-25T20:56:04+05:30",
        "text_file": "xbrl/q.xml"}]}))
    (fx / "export.json").write_text(json.dumps({"issuers": {"RAIN": {"name": "Rain Industries"}}, "documents": [{
        "doc_id": "pr1", "ticker": "RAIN", "doc_type": "announcement", "filing_type": "Press Release",
        "published_at": "2022-02-26T10:00:00+05:30",
        "text": "Rain Industries reported consolidated revenue growth driven by higher realisations in the quarter."}]}))
    off = EarningsInflectionPipeline({"xbrl_results": False}, FixtureRepository(fx))
    off.run(["RAIN"], "2022-03-31")
    assert (Metric.REVENUE, "Q", date(2021, 12, 31)) not in off.last_series.points
    on = EarningsInflectionPipeline({}, FixtureRepository(fx))                      # on by default
    a = on.run(["RAIN"], "2022-03-31").assessments[0]
    p = on.last_series.points[(Metric.REVENUE, "Q", date(2021, 12, 31))]
    assert round(p.value, 3) == 4026.054 and on.last_series.scope == Scope.CONSOLIDATED
    assert (Metric.PAT_ATTRIBUTABLE, "Q", date(2021, 12, 31)) not in on.last_series.points    # sign error held out
    assert a.coverage.get("xbrl_filings") == 1
    before = EarningsInflectionPipeline({"xbrl_results": True}, FixtureRepository(fx))
    before.run(["RAIN"], "2022-02-24")                    # the XBRL was disseminated on 25 Feb at 20:56
    assert (Metric.REVENUE, "Q", date(2021, 12, 31)) not in before.last_series.points


def test_integrated_filing_taxonomy_is_read_the_same_way():
    # from the December 2024 quarter results are filed as SEBI integrated filings (in-capmkt), same elements
    text = filing().replace('xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin"',
                            'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"').replace(
        "in-bse-fin:", "in-capmkt:")
    rows, _ = parse_xbrl_results(doc(text))
    assert get(rows, Metric.REVENUE, "Q").value == 4026.054 and get(rows, Metric.REVENUE, "Q").integrity == "validated"
