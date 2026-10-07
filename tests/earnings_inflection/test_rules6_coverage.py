"""D14 (catalyst-rules-6): results statements the rules-5 parser could not read.  Layouts are from
the rules-5 cohort's filings (Tata Chemicals, Bata India, CESC), verbatim or trimmed."""

from datetime import date, datetime
from pathlib import Path

from makrograph.earnings_inflection.chunking import chunk_document
from makrograph.earnings_inflection.contracts import Metric, SourceDocument
from makrograph.earnings_inflection.extraction import (_column_tokens, _match_metrics, _table_scale,
                                                       parse_results_tables)

FIX = Path(__file__).parent / "fixtures"


def test_a_stacked_header_cut_into_several_chunks_is_reassembled():
    # "Quarter / ended / 31 / December, / 2020" one word per line; the chunker cuts it into three tables
    text = (FIX / "tatachem_q3fy21_stacked_header.txt").read_text()
    doc = SourceDocument(doc_id="d", source_name="t", ticker="TATACHEM", text=text, published_at=datetime(2021, 1, 28))
    chunks = chunk_document(doc)
    assert sum(c.kind == "table" for c in chunks) >= 3
    rows, _ = parse_results_tables(doc, chunks)
    rev = {(r.period_end, r.value) for r in rows if r.metric == Metric.REVENUE}
    assert (date(2020, 12, 31), 2606.08) in rev and (date(2019, 12, 31), 2623.36) in rev
    assert (date(2020, 3, 31), 10356.75) in rev


def test_a_rupee_glyph_inside_the_unit_brackets():
    assert _table_scale("(` in crore)", "") == 1.0                    # Tata Chemicals' text layer
    assert _table_scale("(` in lakhs)", "") == 0.01


def test_a_lower_case_letter_enumerator():
    assert _match_metrics("a Revenue from operations") == [Metric.REVENUE]    # Bata India
    assert _match_metrics("b   Other income (Refer note 4)") == [Metric.OTHER_INCOME]


def test_quarter_end_dates_without_separators():
    # CESC's scanned header: "30 092021  30 062021  30092020  30.09.2021  30 092020  31 .03.2021"
    got = _column_tokens("Particulars   30 092021   30 062021   30092020   30.09.2021   30 092020   31 .03.2021")
    assert [d for d, _ in got] == [date(2021, 9, 30), date(2021, 6, 30), date(2020, 9, 30), date(2021, 9, 30),
                                   date(2020, 9, 30), date(2021, 3, 31)]


def test_other_digit_runs_are_not_read_as_dates():
    assert _column_tokens("30 042021   31082021   300920201") == []          # not quarter ends / too long


def test_an_untitled_statement_takes_the_scope_of_the_review_report_before_it():
    # CESC: no statement title; the auditor's review report on the page before names the statement
    hdr = ("Particulars      Three months  Three months  Three months   Six months   Six months    Year\n"
           "                    ended         ended         ended          ended        ended       ended\n"
           "                 30.09.2021    30.06.2021    30.09.2020     30.09.2021   30.09.2020  31.03.2021\n")
    body = ("  Revenue from operations      2091     1931     1989     4022     3574     6921\n"
            "  Other income                   32       14       14       46       48      180\n"
            " Total income                  2123     1945     2003     4068     3622     7101\n")
    review = ("Independent Auditor's Review Report\n1. We have reviewed the accompanying statement of unaudited "
              "{} financial results of CESC Limited for the quarter ended 30 September 2021.\n"
              + "2. This statement is the responsibility of the management and has been approved by the Board.\n" * 12
              + "For Chartered Accountants\nPartner\nMembership No.\nUDIN\nPlace: Kolkata\n")
    pages = [review.format("standalone"), "(Rs. in crore)\n" + hdr + body,
             review.format("consolidated"), "(Rs. in crore)\n" + hdr + body.replace("2091", "3494")]
    doc = SourceDocument(doc_id="d", source_name="t", ticker="CESC", text="\f".join(pages),
                         published_at=datetime(2021, 11, 11), pages=pages)
    rows, _ = parse_results_tables(doc, chunk_document(doc))
    q = {(r.scope.value, r.value) for r in rows if r.metric == Metric.REVENUE and r.period_end == date(2021, 9, 30)
         and r.period_type == "Q"}
    assert q == {("standalone", 2091.0), ("consolidated", 3494.0)}


def test_a_table_that_fails_its_own_identity_is_not_used_at_all():
    # revenue + other income != total income: the rows are misaligned, so profit rows are not trusted either
    text = ("Statement of Consolidated Financial Results          (Rs. in crore)\n"
            "Particulars          30.09.2024   30.06.2024   30.09.2023   31.03.2024\n"
            "Revenue from operations   632.43   601.10   560.20   2450.00\n"
            "Other income                9.30     4.10     3.00     20.00\n"
            "Total income              700.00   650.00   600.00   2600.00\n"
            "Profit before tax         -65.45   -60.17   -50.00   -200.00\n")
    doc = SourceDocument(doc_id="d", source_name="t", ticker="RBA", text=text, published_at=datetime(2024, 10, 28))
    rows, issues = parse_results_tables(doc, chunk_document(doc))
    assert not [r for r in rows if r.metric in (Metric.REVENUE, Metric.PBT)]
    assert any("figures are not used" in i for i in issues)


PRICOL = """                                                       Statement of Audited Financial Results for the Quarter and Year Ended 31st March, 2024
                                                                                                                                              (Rs. in Lakhs)
                                                              Standalone
                                                                                                                       Consolidated
            Particulars                                For the Three Months Ended       For the Year Ended       For the Three Months Ended       For the Year Ended
                                                  31-Mar-2024  31-Dec-2023  31-Mar-2023  31-Mar-2024  31-Mar-2023  31-Mar-2024  31-Dec-2023  31-Mar-2023  31-Mar-2024  31-Mar-2023
 1. Income
   (a) Revenue from Operations                     56,269.01    55,322.33    50,113.79  2,19,175.34  1,87,191.81    56,621.24    55,719.10    50,968.55  2,20,816.89  1,90,283.12
   (b) Other Operating Revenue                      1,795.20     1,539.51     1,379.88     6,361.34     5,572.95     1,795.20     1,539.51     1,379.88     6,361.34     5,572.95
   (c) Other Income                                   317.05       118.28       192.58     1,047.35       402.36       435.34       193.51       188.66     1,315.83       458.53
   Total Income                                    58,381.26    56,980.12    51,686.25  2,26,584.03  1,93,167.12    58,851.78    57,452.12    52,537.09  2,28,494.06  1,96,314.60
"""


def test_side_by_side_scope_labels_on_their_own_lines_and_other_operating_revenue():
    # Pricol (pdfplumber layout of its scanned statement): "Standalone" over the left half, "Consolidated"
    # one line lower over the right half; revenue printed as (a) + (b) other operating revenue
    doc = SourceDocument(doc_id="d", source_name="t", ticker="PRICOLLTD", text=PRICOL, published_at=datetime(2024, 5, 15))
    rows, issues = parse_results_tables(doc, chunk_document(doc))
    q = {(r.scope.value, round(r.value, 2)) for r in rows if r.metric == Metric.REVENUE
         and r.period_end == date(2024, 3, 31) and r.period_type == "Q"}
    assert q == {("standalone", 580.64), ("consolidated", 584.16)}               # lakh -> crore
    assert not any("rows mis-read" in i for i in issues)
