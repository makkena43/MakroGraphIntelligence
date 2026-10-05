"""Layouts taken from real (public) Indian exchange filings that the parser failed on.

Each excerpt is copied verbatim from the text MakroGraph stored for the filing, including
its scanning damage.  Expected values were checked against the filings by hand.
"""

from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.chunking import chunk_document, normalise_figure, split_numeric_row
from makrograph.earnings_inflection.contracts import (
    IST, FinancialMeasurement, Metric, Modality, Scope, SourceDocument, Unit,
)
from makrograph.earnings_inflection.drivers import compute_drivers
from makrograph.earnings_inflection.extraction import parse_results_tables, resolve_columns
from makrograph.earnings_inflection.financial_series import FinancialSeries
from makrograph.earnings_inflection.identity import IssuerModel


def parse(text, doc_id="D", when=datetime(2024, 2, 13, 20, 0, tzinfo=IST)):
    d = SourceDocument(doc_id=doc_id, source_name="t", ticker="T", text=text, published_at=when)
    d.available_at = when
    return parse_results_tables(d, chunk_document(d))


def val(rows, metric, end, ptype="Q", scope=None):
    hits = [r.value for r in rows if r.metric == metric and r.period_end == end and r.period_type == ptype
            and (scope is None or r.scope == scope)]
    assert len(set(hits)) <= 1, hits
    return hits[0] if hits else None


# --- number repair ---------------------------------------------------------

@pytest.mark.parametrize("tok,expected", [
    ("11.30.069", "11,30,069"),          # dots scanned for commas (Indian grouping)
    ("2.42,845.54", "2,42,845.54"),
    ("82.916.27", "82,916.27"),
    ("1,400.95,", "1,400.95"),           # stray trailing separator
    ("17,111.84}", "17,111.84"),         # table rule scanned into the figure
    ("47,2563", "?"),                    # invalid grouping: unreadable, not 472,563
    ("1,85,264", "1,85,264"),
    ("123", "123"),
])
def test_normalise_figure(tok, expected):
    assert normalise_figure(tok) == expected


def test_split_figure_is_rejoined():
    _, cells = split_numeric_row("I Revenue from Operations 82.916.27 1,1 0,377.07")
    assert cells == ["82,916.27", "1,10,377.07"]


# --- INDOTECH: words run together, scanned "31-Pec-22" header date ------------

INDOTECH_Q3FY24 = """STATEMENTOFUNAUDITEDFINANCIALRESULTSFORTHEQUARTERANDNINEMONTHPERIODENDED31DECEMBER2023
(Rs.Inlakhs)
S.No. Particulars Quarterended Ninemonthsperiodended Yearended
31-Dec-23 30-Sep-23 31-Dec-22 31-Dec-23 31-Pec-22 31-Mar-23
(Unaudited) (Unaudited) (Unaudited) (Unaudited) (Unaudited) (Audited)
A Revenuefromoperations 13,947 9,581 8,362 32,850 22,624 37,091
B Otherincome 65 236 37 382 193 266
C Totalincome(A+B) 14,012 9,817 8,399 33,232 22,817 37,357
Expenses
Costofmaterialsconsumed 10,573 10,498 7,118 29,306 19,570 27,700
Employeebenefitsexpense 737 689 764 2,198 2,064 2,957
Financecosts 56 128 65 253 155 272
Otherexpenses 1,280 980 1,047 3,596 3,179 4,688
D Totalexpenses 12,607 8,706 8,015 30,211 22,177 34,787
E Profitbeforetax(C-D) 1,405 1,111 384 3,021 640 2,570
"""


def test_despaced_labels_and_unreadable_header_date_use_the_sebi_layout():
    rows, issues = parse(INDOTECH_Q3FY24)
    # one header date is unreadable; the column layout comes from the readable dates, and
    # the first value is never dropped as a "note reference" (which shifted every column)
    assert val(rows, Metric.REVENUE, date(2023, 12, 31)) == pytest.approx(139.47)
    assert val(rows, Metric.REVENUE, date(2022, 12, 31)) == pytest.approx(83.62)
    assert val(rows, Metric.REVENUE, date(2023, 9, 30)) == pytest.approx(95.81)
    assert val(rows, Metric.PBT, date(2023, 12, 31)) == pytest.approx(14.05)
    assert any("SEBI results layout" in i for i in issues)


# --- SHAILY: month-day line over a year line; decimals lost in scanning -------

SHAILY_Q3FY24 = """Statement of unaudited Consolidated financial results for the quarter and nine months ended December 31,2023
(Rs. In Lakhs)
Quarter ended For the nine months ended Year ended
Sr. No. Particulars December 31, September 30, December 31, December 31, December 31, March 31,2023
2023 2023 2022 2023 2022
(Unaudited) (Unaudited) (Unaudited) {(Unaudited) {(Unaudited) (Audited)
] Income
Revenue from operations 15,843.84 15,756.86 13,626.28 4733118 47,256.93 60,706.58
|Other income 108.00 297.14 2299 492.23 32251 450.44
Total Income 15,951.84 16,054.00 13,649.27 47,823.41 47,579.44 61,157.02
Finance costs 499.53 430.81 479.97 1320.16 1,299.47 1,788.23
Depreciation and amortisation 941.22 810.70 793.75 2,569.24 2,344.13 3,330.55
"""


def test_month_day_over_year_header_and_lost_decimal_points():
    rows, issues = parse(SHAILY_Q3FY24)
    assert val(rows, Metric.REVENUE, date(2023, 12, 31), scope=Scope.CONSOLIDATED) == pytest.approx(158.4384)
    assert val(rows, Metric.REVENUE, date(2022, 12, 31)) == pytest.approx(136.2628)
    # "4733118" (47,331.18 with its separators lost) is unreadable, not 47 lakh crore
    assert val(rows, Metric.REVENUE, date(2023, 12, 31), "9M") is None
    # "2299" in a 2-decimal table is 22.99 lakh with its point lost: unreadable, not 22.99 crore
    assert val(rows, Metric.OTHER_INCOME, date(2022, 12, 31)) is None
    assert any("decimal point" in i for i in issues)


# --- PGIL: consolidated and standalone side by side ---------------------------

PGIL_Q3FY24 = """Statement of Standalone & Consolidated Unaudited Financial Results for the quarter and period ended December 31, 2023
IRs. In Lakh exceDI earnlno oer share dataI
SI.No. Particulars Consolidated Standalone
Quarter Quarter Quarter Period Period Year Quarter Quarter Quarter Period Period Year
Ended Ended Ended Ended Ended Ended Ended Ended Ended Ended Ended Ended
31.12.2023 30.09.2023 31.12.2022 31.12.2023 31.12.2022 31.03.2023 31.12.2023 30.09.2023 31.12.2022 31.12.2023 31.12.2022 31.03.2023
IUnaudltedl I UnauditedI IUnaudltedl IUnaudltedl runaudltedl /Audited\\ IUnaudltedl IUnaudltedl IUnaudltedl IUnaudltedl IUnaudltedl IAudltedl
Revenue
I Revenue from Operations 70,397.95 96,059.17 71,705.83 2,55,878.33 2.42,845.54 3,15,840.92 15,759.90 21,850.25 20,077.03 63,359.68 82.916.27 1,1 0,377.07
II Other Income 277.46 733.40 324.76 1,753.92 1,717.30 2,280.99 1.579.77 637.20 737.67 3,111.27 2.437.25 3,035.51
"""

PGIL_Q1FY24 = """Statement of Standalone & Consolidated Un-audited Flnanclal Results for the quarter ended June 30, 2023
(All a1110unt In Rs. lakh, unless, olllerwlM stated)
SI. No. Particulars Consolidated Standalone
Quamir Quarter Quarter Year Quarter Quarter Quarter Year
Ended Ended Ended Ended Ended Ended Ended Ended
30.06.2023 31.03.2023 30.06.2022 31.03.2023 30.06.2023 31.03.2023 30.06.2022 31.03.2023
(Unaudited) (Audited) (Unaudited) (Audited) (Unaudited) (Audited) (Unaudited) (Audited)
I Revenue from Operations 89,421.20 72,995.38 85,106.45 3,15,840.92 25,749.53 27,460.80 32,786.82 1,10,377.07
II Other Income 446.60 529.13 368.88 2,280.99 448.44 899.09 393.05 3,035.51
"""


def test_side_by_side_statements_get_their_own_scopes():
    rows, _ = parse(PGIL_Q3FY24)
    assert val(rows, Metric.REVENUE, date(2023, 12, 31), scope=Scope.CONSOLIDATED) == pytest.approx(703.9795)
    assert val(rows, Metric.REVENUE, date(2023, 12, 31), scope=Scope.STANDALONE) == pytest.approx(157.599)
    assert val(rows, Metric.REVENUE, date(2022, 12, 31), "9M", Scope.STANDALONE) == pytest.approx(829.1627)
    assert val(rows, Metric.REVENUE, date(2023, 3, 31), "FY", Scope.STANDALONE) == pytest.approx(1103.7707)


def test_quarter_words_split_from_ended_and_scanned_quarter_word():
    rows, _ = parse(PGIL_Q1FY24, when=datetime(2023, 8, 12, tzinfo=IST))
    assert val(rows, Metric.REVENUE, date(2023, 6, 30), "Q", Scope.CONSOLIDATED) == pytest.approx(894.212)
    assert val(rows, Metric.REVENUE, date(2023, 6, 30), "FY") is None      # never typed as a year


def test_four_column_q4_statement_is_not_two_side_by_side_statements():
    cols, _ = resolve_columns(["Quarter ended Year ended", "31.03.2023 31.03.2022 31.03.2023 31.03.2022"])
    assert [t for _, t in cols] == ["Q", "Q", "FY", "FY"]


# --- DEEPAKFERT: segment table, dot-separated figure, title further up --------

DEEPAKFERT_Q3FY24 = """STATEMENT OF UNAUDITED STANDALONE FINANCIAL RESULTS FOR THE QUARTER AND NINE MONTHS ENDED 31 DECEMBER 2023
(Amounts in Rs Lakhs unless otherwise stated)
Sr. Particulars Quarter Ended Nine Months Ended Year Ended
No. 31 December 2023 30 September 2023 31 December 2022 31 December 2023 31 December 2022 31 March 2023
1 Income
(a) Revenue from operations 42,618 47,601 51,283 1,47,976 1,74,518 2,34,982
Notes follow here and continue for a while before the next statement.
PARTI (Amounts in Rs Lakhs unless otherwise stated)
STATEMENT OF UNAUDITED CONSOLIDATED FINANCIAL RESULTS FOR THE QUARTER AND NINE MONTHS ENDED 31 DECEMBER 2023
Sr. Particulars Quarter Ended Nine Months Ended Year Ended
No. 31 December 2023 30 September 2023 31 December 2022 31 December 2023 31 December 2022 31 March 2023
Refer Notes Below) I Unaudited\\ IUn audlted\\ (Unaudited\\ /Unaudited\\ (Unaudited\\ (Audited\\
1 Income
(a) Revenue from operations 1,85,264 2,42,416 2,75,476 6,58,981 8,50,515 11.30.069
UNAUDITED SEGMENT-WISE REVENUE, RESULTS, ASSETS AND LIABILITIES (Amounts in Rs Lakhs unless otherwise stated)
Consolidated
Sr. Particulars Quarter Ended Nine Months Ended Year Ended
No. 31 December 2023 30 September 2023 31 December 2022 31 December 2023 31 December 2022 31 March 2023
1 Segment revenue
(a) Chemicals
Total income from operations 1,03,140 1,17,028 1,61,480 3,43,994 4,91,848 6,41,134
"""


def test_segment_rows_are_not_company_revenue_and_scope_comes_from_the_nearest_title():
    rows, _ = parse(DEEPAKFERT_Q3FY24, when=datetime(2024, 2, 1, tzinfo=IST))
    assert val(rows, Metric.REVENUE, date(2023, 12, 31), scope=Scope.STANDALONE) == pytest.approx(426.18)
    assert val(rows, Metric.REVENUE, date(2023, 12, 31), scope=Scope.CONSOLIDATED) == pytest.approx(1852.64)
    assert val(rows, Metric.REVENUE, date(2023, 3, 31), "FY", Scope.CONSOLIDATED) == pytest.approx(11300.69)
    assert all(abs(r.value - 1031.40) > 1 for r in rows if r.metric == Metric.REVENUE)   # segment total


# --- REFEX: "Dec31,2023" / "Dec 31,2022" header tokens --------------------------

def test_compact_month_day_year_header_tokens():
    cols, issue = resolve_columns(["Quarter Ended Nine Months ended Year ended",
                                   "Particulars Dec 31, 2023 Sep 30, 2023 Dec 31,2022 | Dec31,2023 | Dec31,2022 | Mar 31,2023"])
    assert not issue and [(d.isoformat(), t) for d, t in cols] == [
        ("2023-12-31", "Q"), ("2023-09-30", "Q"), ("2022-12-31", "Q"),
        ("2023-12-31", "9M"), ("2022-12-31", "9M"), ("2023-03-31", "FY")]


# --- press-release highlight tables -------------------------------------------

SUPRIYA_PR = """Supriya Lifescience reports 213% jump in Net Profit in Q3FY24, EBITDA zooms 195%
Key Consolidated Financial Highlights:
Particulars Q3 FY24 Q3 FY23
Revenues (in Rs Cr) 140.07 105.14
Gross Profit (in Rs Cr) 85.45 53.79
EBITDA (in Rs Cr) 41.49 14.05
PAT (in RsCr) 29.79 9.52
PAT Margin 21.6% 9.1%
"""

GOLDIAM_PR = """Financial Highlights (Consolidated) - Q3 & 9M FY24
Particulars (₹ Mn) Q3FY24 Q3FY23 YoY Q2FY24 QoQ 9MFY24 9MFY23 YoY
Revenue 2050 1856 10% 1393 47% 4654 4194 11%
EBITDA 438 402 9% 332 32% 1008 1024 -2%
EBITDA margin 21.3% 21.7% (32 bps) 23.9% (252 bps) 21.7% 24.4% (276 bps)
PAT 324 288 12% 235 38% 731 690 6%
"""


def test_highlight_table_with_unit_in_the_row_label():
    rows, _ = parse(SUPRIYA_PR, when=datetime(2024, 2, 8, tzinfo=IST))
    assert val(rows, Metric.REVENUE, date(2023, 12, 31)) == pytest.approx(140.07)
    assert val(rows, Metric.REVENUE, date(2022, 12, 31)) == pytest.approx(105.14)
    assert val(rows, Metric.PAT, date(2023, 12, 31)) == pytest.approx(29.79)
    assert all(r.source == "reported_highlight" for r in rows if r.metric in (Metric.REVENUE, Metric.PAT))


def test_highlight_table_with_change_columns_between_periods():
    rows, _ = parse(GOLDIAM_PR, when=datetime(2024, 2, 8, tzinfo=IST))
    assert val(rows, Metric.REVENUE, date(2023, 12, 31)) == pytest.approx(205.0)
    assert val(rows, Metric.REVENUE, date(2022, 12, 31)) == pytest.approx(185.6)
    assert val(rows, Metric.REVENUE, date(2023, 9, 30)) == pytest.approx(139.3)
    assert val(rows, Metric.REVENUE, date(2023, 12, 31), "9M") == pytest.approx(465.4)


def test_highlight_labels_are_not_used_in_investor_presentations():
    from makrograph.earnings_inflection.contracts import DocumentKind
    d = SourceDocument(doc_id="P", source_name="t", ticker="T", text=SUPRIYA_PR,
                       published_at=datetime(2024, 2, 8, tzinfo=IST))
    d.available_at, d.kind = d.published_at, DocumentKind.INVESTOR_PRESENTATION
    rows, _ = parse_results_tables(d, chunk_document(d))
    assert not [r for r in rows if r.metric == Metric.REVENUE]


# --- ASALCBR: labels and values on separate lines ------------------------------

ASALCBR_ABOVE = """Statement of Unaudited Financial Results for the Quarter and Nine Months Ended 31 December 2023
(INR in lakhs except as stated)
Quarter Ended Nine Months Ended Year Ended
31.12.2023 | 30.09.2023 | 31.12.2022 | 31.12.2023 | 31.12.2022 | 31.03.2023
Particulars
Unaudited Unaudited Unaudite d | U naudited | Unaud ited Audited
Income
19,269.43 17,111.84} 18,589.83] 52,024.42 | 51,694.20 70,276.88
| Revenue from operations
175.78 249.17 203.69 632.86 698.15 891.50
Il Other income
19,445.21 | 17,361.01 | 18,793.52 | 52,657.28 | 52,392.35 71,168.38
Ill Total Income (I + II)
"""

ASALCBR_SAME_LINE_SCRAMBLE = """Statement of Unaudited Financial Results for the Quarter and Nine Months ended 3ist December, 2022
Rs. in lakhs unless otherwise stated
Quarter Ended Nine Months Ended Year Ended
31.12.2022 30.09.2022 31.12.2021 31.12.2022 31.12.2021 31.03.2022
Unaudited Unaudited Unaudited
Income 18,589.83 14,674.06 16,469.74 51,694.20 36,563.51 51,422.45
Revenue from Operations 203.69 317.85 522.35 698.15 942.22 1,416.00
T
Ot
"""


def test_values_above_their_labels_are_paired_by_the_income_identity():
    rows, issues = parse(ASALCBR_ABOVE, when=datetime(2024, 1, 25, tzinfo=IST))
    assert val(rows, Metric.REVENUE, date(2023, 12, 31)) == pytest.approx(192.6943)
    assert val(rows, Metric.OTHER_INCOME, date(2023, 12, 31)) == pytest.approx(1.7578)
    assert any("values printed above their labels" in i for i in issues)


def test_rows_that_break_the_income_identity_are_not_used():
    rows, issues = parse(ASALCBR_SAME_LINE_SCRAMBLE, when=datetime(2023, 2, 10, tzinfo=IST))
    # the revenue figures sit on a line labelled with the section heading "Income": the labels are
    # displaced, so "Revenue from Operations" here carries other income's figures
    assert not [r for r in rows if r.metric in (Metric.REVENUE, Metric.OTHER_INCOME)]
    assert any("section heading" in i for i in issues)


# --- series: scope choice, highlights vs statements, outliers, conflicts -------

def m(metric, end, value, scope=Scope.CONSOLIDATED, ptype="Q", doc="d", when=None, source="reported", du=0.01):
    return FinancialMeasurement("T", metric, end, ptype, value, Unit.INR_CRORE, scope, doc,
                                when or datetime(end.year, end.month, 28, tzinfo=IST), source=source,
                                display_unit=du)


def quarters(start_year=2022):
    return [date(y, mo, 30 if mo in (6, 9) else 31) for y in (start_year, start_year + 1) for mo in (3, 6, 9, 12)]


def test_scope_with_the_comparable_latest_quarter_is_used_and_disclosed():
    rows = [m(Metric.REVENUE, d, 100 + i, Scope.STANDALONE, doc=f"s{i}") for i, d in enumerate(quarters())]
    rows += [m(Metric.REVENUE, date(2023, 12, 31), 300, Scope.CONSOLIDATED, doc="c1"),
             m(Metric.REVENUE, date(2023, 9, 30), 280, Scope.CONSOLIDATED, doc="c1")]   # no year-ago quarter
    s = FinancialSeries.build("T", rows)
    assert s.scope == Scope.STANDALONE and "no year-ago comparison" in s.scope_note


def test_unstated_scope_is_merged_only_when_the_company_names_a_single_scope():
    rows = [m(Metric.REVENUE, date(2022, 12, 31), 80, Scope.STANDALONE, doc="a"),
            m(Metric.REVENUE, date(2023, 12, 31), 140, Scope.UNKNOWN, doc="b")]
    s = FinancialSeries.build("T", rows)
    assert s.scope == Scope.STANDALONE and s.get(Metric.REVENUE, date(2023, 12, 31), "Q").value == 140


def test_statement_figure_outranks_a_highlight_figure():
    d = date(2023, 12, 31)
    rows = [m(Metric.REVENUE, d, 129.19, doc="stmt", when=datetime(2024, 2, 8, 10, tzinfo=IST)),
            m(Metric.REVENUE, d, 131.8, doc="pr", when=datetime(2024, 2, 8, 18, tzinfo=IST),
              source="reported_highlight", du=0.1)]
    p = FinancialSeries.build("T", rows).get(Metric.REVENUE, d, "Q")
    assert p.value == 129.19 and p.versions == 1


def test_rounded_restatement_is_not_a_revision():
    d = date(2023, 12, 31)
    rows = [m(Metric.REVENUE, d, 205.03, doc="stmt", du=0.0001, when=datetime(2024, 2, 8, 10, tzinfo=IST)),
            m(Metric.REVENUE, d, 205.0, doc="later", du=0.1, when=datetime(2024, 5, 20, tzinfo=IST))]
    p = FinancialSeries.build("T", rows).get(Metric.REVENUE, d, "Q")
    assert p.value == 205.03 and p.versions == 1


def test_single_filing_outlier_is_excluded_and_listed():
    rows = [m(Metric.REVENUE, d, 150 + i, doc=f"d{i}") for i, d in enumerate(quarters())]
    rows.append(m(Metric.REVENUE, date(2021, 12, 31), 5.2, doc="scan"))
    s = FinancialSeries.build("T", rows)
    assert s.get(Metric.REVENUE, date(2021, 12, 31), "Q") is None
    assert any("10x away" in x for x in s.excluded)


def test_value_stated_by_more_filings_wins_a_conflict():
    d = date(2022, 12, 31)
    rows = [m(Metric.REVENUE, d, 185.9, doc="q3fy23", when=datetime(2023, 2, 10, tzinfo=IST)),
            m(Metric.REVENUE, d, 185.9, doc="q3fy24", when=datetime(2024, 1, 25, tzinfo=IST)),
            m(Metric.REVENUE, d, 2.04, doc="scan", when=datetime(2024, 2, 1, tzinfo=IST))]
    s = FinancialSeries.build("T", rows)
    assert s.get(Metric.REVENUE, d, "Q").value == 185.9
    assert any("conflicting values" in x for x in s.lineage_notes)


def test_reposts_of_one_filing_count_once():
    from makrograph.earnings_inflection.financial_series import _filings
    a = m(Metric.REVENUE, date(2022, 3, 31), 4.7, doc="x", when=datetime(2023, 2, 10, tzinfo=IST))
    b = m(Metric.REVENUE, date(2022, 3, 31), 4.7, doc="y", when=datetime(2023, 2, 11, tzinfo=IST))
    assert _filings([a, b]) == 1


def test_operating_leverage_needs_real_revenue_growth():
    rows = []
    for d, rev, ebitda in ((date(2022, 12, 31), 185.9, 20.0), (date(2023, 12, 31), 192.7, 24.5)):
        rows += [m(Metric.REVENUE, d, rev), m(Metric.EBITDA, d, ebitda)]
    s = FinancialSeries.build("T", rows)
    drivers, _ = compute_drivers(s, [], [], IssuerModel.OPERATING, date(2024, 3, 31))
    ol = next(d for d in drivers if d.driver == "operating_leverage")
    assert ol.current > 1.5 and not ol.material          # +3.7% revenue: the ratio is not meaningful


# --- guidance ledger ---------------------------------------------------------------

def _ev(quote, label, when, qty=None, metric=Metric.REVENUE_GROWTH_GUIDANCE):
    from makrograph.earnings_inflection.contracts import Evidence, EvidenceTier, Quantity
    return Evidence(evidence_id=f"e{abs(hash(quote + label + str(when)))}", ticker="T", doc_id="g", chunk_id="g:0",
                    quote=quote, metric=metric, quantity=qty or Quantity(14.0, Unit.PERCENT, "14%"),
                    modality=Modality.FORWARD, tier=EvidenceTier.MANAGEMENT_ASSERTION, target_period_label=label,
                    available_at=when)


def test_statement_about_a_finished_period_is_not_guidance():
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    e = _ev("we expect growth of 14% in Q3FY24", "Q3FY24", datetime(2024, 2, 1, tzinfo=IST))
    assert build_ledger([e], None, datetime(2024, 3, 31, tzinfo=IST)) == []


def test_revisions_of_period_less_statements_do_not_contradict():
    from makrograph.earnings_inflection.assessments import decide_status
    from makrograph.earnings_inflection.contracts import EvidenceStatus, Quantity
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    evs = [_ev(f"revenue of Rs {v} crore expected", "", datetime(2022 + i, 6, 1, tzinfo=IST),
               Quantity(v, Unit.INR_CRORE, f"Rs {v} crore"), Metric.REVENUE_GUIDANCE)
           for i, v in enumerate((400.0, 352.0, 300.0))]
    ledger = build_ledger(evs, None, datetime(2024, 3, 31, tzinfo=IST))
    status, _ = decide_status([], [], evs, ledger, 3, None, datetime(2024, 3, 31, tzinfo=IST))
    assert status != EvidenceStatus.CONTRADICTED


def test_growth_percent_must_sit_next_to_the_growth_word():
    from makrograph.earnings_inflection.extraction import _growth_percent
    assert _growth_percent("Warehouses 67% 12% CIL ... Growth momentum is expected to continue in Q4 FY23") is None
    assert _growth_percent("We expect revenue growth of 25% in FY25").value == 25.0
