"""Scanned thousands separators and unreadable unit lines in results statements.

The first excerpt is copied verbatim from the text layer of a public exchange filing
(results for the year ended 31 March 2024, amounts in lakhs).  The scan printed many
thousands commas as dots ("17.511" for 17,511).  The other statements are synthetic.
"""

from datetime import date, datetime

from makrograph.earnings_inflection.chunking import chunk_document, split_numeric_row
from makrograph.earnings_inflection.contracts import IST, FinancialMeasurement, Metric, Scope, SourceDocument, Unit
from makrograph.earnings_inflection.extraction import (
    _decimal_convention, _match_metrics, infer_unstated_scales, parse_results_tables,
)

SCANNED_DOTS = """                               STATEMENT OF AUDITED FINANCIAL RESULTS FOR THE FINANCIAL YEAR ENDED 31 MARCH 2024
                                                                                                                                                             (Rs. in lakhs)
S. No. Particulars                                                                                Quarter ended                                 Year ended
                                                                                        31-Mar-24  31-Dec-23    31-Mar-23                  31-Mar-24   31-Mar-23

                                                                                         (Audited)      (Unaudited)     (Audited)          (Audited)           (Audited)

  A      Revenue from operations                                                              17.511          13.947          14,467            50.361               37,091
  B      Other income                                                                            249              65              73               631                  266
  C      Total Income (A+B)                                                                   17,760          14,012          14.540            50,992               37,357
         Expenses
         Cost of materials consumed                                                            9,966          10.573           8.130            39,272               27.700
         Changes in inventories of finished goods and work-in-progress                         1,599            (168)          1.761            (3,906)              (1.312)
         Employee benefits expense                                                               989             737             893             3,187                3.095
         Finance costs                                                                           118              56             117                  371               272
         Depreciation and amortisation expense                                                   128             129             200                  491               482
"""


def parse(text, doc_id="D", when=datetime(2024, 5, 23, 18, 0, tzinfo=IST), unscaled=None):
    d = SourceDocument(doc_id=doc_id, source_name="t", ticker="T", text=text, published_at=when)
    d.available_at = when
    return parse_results_tables(d, chunk_document(d), unscaled=unscaled)


def val(rows, metric, end, ptype="Q"):
    hits = {r.value for r in rows if r.metric == metric and r.period_end == end and r.period_type == ptype}
    assert len(hits) <= 1, hits
    return hits.pop() if hits else None


def test_dotted_thousands_in_a_whole_number_table_are_repaired():
    rows, issues = parse(SCANNED_DOTS)
    assert val(rows, Metric.REVENUE, date(2024, 3, 31)) == 175.11            # "17.511" lakh, not 0.175 cr
    assert val(rows, Metric.REVENUE, date(2023, 12, 31)) == 139.47
    assert val(rows, Metric.REVENUE, date(2024, 3, 31), "FY") == 503.61
    assert val(rows, Metric.COST_OF_MATERIALS, date(2023, 12, 31)) == 105.73
    assert val(rows, Metric.INVENTORY_CHANGE, date(2023, 3, 31), "FY") == -13.12
    assert not any("total income" in i for i in issues)                     # identity holds after repair


def test_a_genuine_three_decimal_table_keeps_its_decimals():
    lines = [f"Item {i} 1,234.567 2,345.678 3,456.789" for i in range(5)]
    assert _decimal_convention(lines) == 3
    lines = [f"Item {i} 17.511 13.947 14.467" for i in range(5)]          # no comma-grouped whole amounts
    assert _decimal_convention(lines) == 3
    lines = [f"Item {i} 17.511 13.947 14,467 37,091" for i in range(5)]
    assert _decimal_convention(lines) == 0


def test_letter_enumerators_and_operations_rows():
    assert _match_metrics("A      Revenue from operations") == [Metric.REVENUE]
    assert _match_metrics("G      Profit after tax (E-F)") == [Metric.PAT]
    # "operations" contains "ratio": it must not be read as a ratio row
    assert _decimal_convention([f"A Revenue from operations {x} 13.947 14,467 37,091 1,234"
                                for x in ("17.511", "18.111", "19.222")]) == 0


def test_single_space_inside_a_cell_of_column_aligned_text():
    _, cells = split_numeric_row("A   Revenue from operations     17,734      40,572      50 321")
    assert cells == ["17,734", "40,572", "50,321"]
    _, cells = split_numeric_row("G   Profit after tax     2,478      1 ,917      1,772")
    assert cells == ["2,478", "1,917", "1,772"]
    # single-space text (pdfplumber default) keeps its cells apart
    _, cells = split_numeric_row("Finance costs 56 128 65 253")
    assert cells == ["56", "128", "65", "253"]


# --- unit line unreadable: unit from the issuer's earlier filings -------------------------

NO_UNIT = """STATEMENT OF UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED 30 JUNE 2024
(Rq In lakh*]
Particulars                          Quarter ended
                              30-Jun-24     31-Mar-24     30-Jun-23
Revenue from operations          8,215        17,471         9,322
Other income                       169           289            81
Total income                     8,384        17,760         9,403
Profit before tax                  794         2,707           601
"""


def ref(metric, end, value, when=datetime(2024, 5, 23, tzinfo=IST), doc="earlier"):
    return FinancialMeasurement("T", metric, end, "Q", value, Unit.INR_CRORE, Scope.STANDALONE, doc, when)


def test_unreadable_unit_line_is_not_used_on_its_own():
    rows, issues = parse(NO_UNIT, doc_id="N")
    assert not [r for r in rows if r.metric == Metric.REVENUE]
    assert any("without unit line" in i for i in issues)


def test_unit_inferred_from_matching_comparatives_in_earlier_filings():
    unscaled = []
    parse(NO_UNIT, doc_id="N", when=datetime(2024, 8, 6, tzinfo=IST), unscaled=unscaled)
    refs = [ref(Metric.REVENUE, date(2024, 3, 31), 174.71), ref(Metric.REVENUE, date(2023, 6, 30), 93.22),
            ref(Metric.PBT, date(2024, 3, 31), 27.07)]
    rows, issues = infer_unstated_scales(unscaled, refs)
    assert val(rows, Metric.REVENUE, date(2024, 6, 30)) == 82.15
    assert any("inferred as lakh from 3 of 3" in i for i in issues)
    assert all(r.quote.startswith("[unit inferred: lakh]") for r in rows)


def test_unit_not_inferred_from_too_few_conflicting_or_later_figures():
    unscaled = []
    parse(NO_UNIT, doc_id="N", when=datetime(2024, 8, 6, tzinfo=IST), unscaled=unscaled)
    few = [ref(Metric.REVENUE, date(2024, 3, 31), 174.71), ref(Metric.REVENUE, date(2023, 6, 30), 93.22)]
    assert infer_unstated_scales(unscaled, few)[0] == []
    conflicting = few + [ref(Metric.PBT, date(2024, 3, 31), 2707.0)]            # one figure says crore
    rows, issues = infer_unstated_scales(unscaled, conflicting)
    assert rows == [] and any("do not establish" in i for i in issues)
    later = [ref(m, d, v, when=datetime(2024, 11, 12, tzinfo=IST)) for m, d, v in
             [(Metric.REVENUE, date(2024, 3, 31), 174.71), (Metric.REVENUE, date(2023, 6, 30), 93.22),
              (Metric.PBT, date(2024, 3, 31), 27.07)]]
    assert infer_unstated_scales(unscaled, later)[0] == []                     # not known at filing time
