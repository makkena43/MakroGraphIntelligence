"""WP5 - mechanism-specific earnings detection: positive, negative and missing-data cases."""

from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.assessments import decide_status
from makrograph.earnings_inflection.contracts import (
    IST, CommitmentStrength, CustomerVerification, EconomicEvent, EventStage, EventStateChange, Evidence,
    EvidenceStatus, EvidenceTier, FinancialMeasurement, Mechanism, MechanismState, Metric, Modality, Quantity,
    RelationshipStatus, Scope, Unit, ValueBasis,
)
from makrograph.earnings_inflection.drivers import compute_drivers
from makrograph.earnings_inflection.financial_series import FinancialSeries
from makrograph.earnings_inflection.identity import IssuerModel
from makrograph.earnings_inflection.mechanisms import detect_mechanisms

AS_OF = date(2024, 3, 31)
QE = [date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31),
      date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]


def fm(metric, end, value, ptype="Q", segment="", doc=None):
    when = datetime(end.year, end.month, 28, tzinfo=IST) if end.month != 12 else datetime(end.year + 1, 2, 10,
                                                                                           tzinfo=IST)
    return FinancialMeasurement("T", metric, end, ptype, value, Unit.INR_CRORE, Scope.CONSOLIDATED,
                                doc or f"r{end:%y%m}", when, display_unit=0.01, segment=segment)


def ev(metric, quote, when, value=None, unit=Unit.PERCENT, facility="", direction=0, modality=Modality.REALIZED,
       segment=""):
    return Evidence(evidence_id=f"e{abs(hash((metric, quote, when)))}", doc_id=f"d{when:%y%m%d}", ticker="T",
                    metric=metric, tier=EvidenceTier.REALIZED_EXECUTION, modality=modality, quote=quote,
                    available_at=datetime.combine(when, datetime.min.time(), tzinfo=IST),
                    quantity=Quantity(value, unit, f"{value}") if value is not None else None,
                    facility=facility, direction=direction, segment=segment)


def revenue_rows(base=100.0, growth=0.02):
    """Quarterly revenue growing `growth` YoY (low sales growth by default)."""
    return [fm(Metric.REVENUE, d, base * (1 + growth) ** (i // 4)) for i, d in enumerate(QE)]


def mech(rows, evidence=(), events=(), name=None):
    s = FinancialSeries.build("T", rows)
    drivers, _ = compute_drivers(s, list(events), list(evidence), IssuerModel.OPERATING, AS_OF)
    out = detect_mechanisms(s, list(evidence), list(events), drivers, AS_OF)
    return (next(m for m in out if m.mechanism == name) if name else out), s, drivers


# --- utilization ---------------------------------------------------------------------

def test_utilization_different_plants_are_never_compared():
    e = [ev(Metric.UTILIZATION, "Plant A utilisation was 40%", date(2023, 8, 10), 40, facility="plant a"),
         ev(Metric.UTILIZATION, "Plant B utilisation was 90%", date(2024, 2, 10), 90, facility="plant b")]
    m, _, _ = mech(revenue_rows(), e, name=Mechanism.UTILIZATION)
    assert m.state == MechanismState.INSUFFICIENT_DATA and m.magnitude is None
    assert any("different plants" in i for i in m.invalidators)


def test_utilization_same_plant_improvement_and_decline():
    up = [ev(Metric.UTILIZATION, "Plant A utilisation was 60%", date(2023, 8, 10), 60, facility="plant a"),
          ev(Metric.UTILIZATION, "Plant A utilisation was 80%", date(2024, 2, 10), 80, facility="plant a")]
    m, _, _ = mech(revenue_rows(), up, name=Mechanism.UTILIZATION)
    assert m.state == MechanismState.EMERGING and m.direction == "positive" and m.magnitude == 20
    down = [ev(Metric.UTILIZATION, "Plant A utilisation was 80%", date(2023, 8, 10), 80, facility="plant a"),
            ev(Metric.UTILIZATION, "Plant A utilisation was 60%", date(2024, 2, 10), 60, facility="plant a")]
    m, _, _ = mech(revenue_rows(), down, name=Mechanism.UTILIZATION)
    assert m.state == MechanismState.ADVERSE and m.direction == "negative"


def test_utilization_with_changed_capacity_is_not_comparable():
    e = [ev(Metric.UTILIZATION, "Plant A utilisation was 60%", date(2023, 8, 10), 60, facility="plant a"),
         ev(Metric.CAPACITY, "Plant A capacity of 100 MW", date(2023, 9, 10), 100, Unit.MW, facility="plant a"),
         ev(Metric.CAPACITY, "Plant A capacity of 150 MW", date(2023, 12, 10), 150, Unit.MW, facility="plant a"),
         ev(Metric.UTILIZATION, "Plant A utilisation was 80%", date(2024, 2, 10), 80, facility="plant a")]
    m, _, _ = mech(revenue_rows(), e, name=Mechanism.UTILIZATION)
    assert m.state == MechanismState.ASSERTION and any("capacity changed" in i for i in m.invalidators)


# --- pricing / input costs (gross margin) --------------------------------------------------

def gm_rows(margins):
    """Revenue +2% YoY; materials set so gross margin follows `margins` (one per quarter in QE)."""
    rows = revenue_rows()
    for r, gm in zip(rows[:], margins):
        rows.append(fm(Metric.COST_OF_MATERIALS, r.period_end, r.value * (1 - gm / 100)))
    return rows


def test_low_sales_growth_sustained_margin_gain_confirms_its_own_mechanism():
    rows = gm_rows([30, 30, 30, 30, 30, 30, 33.5, 34])
    m, s, drivers = mech(rows, name=Mechanism.PRICING_INPUT)
    assert m.state == MechanismState.CONFIRMED and m.direction == "positive"
    assert "unattributed" in m.attribution                         # no stated cause -> none claimed
    allm = detect_mechanisms(s, [], [], drivers, AS_OF)
    status, why = decide_status(drivers, [], [], [], 3, s.ttm(Metric.REVENUE, QE[-1])[0],
                                datetime(2024, 3, 31, tzinfo=IST), 365, allm)
    assert status == EvidenceStatus.EXECUTION_CONFIRMED
    assert any("pricing_input_costs confirmed" in w for w in why)


def test_gross_margin_decline_is_adverse_and_stated_cause_is_reported():
    e = [ev(Metric.INPUT_COST, "Raw material prices increased sharply", date(2024, 2, 10), direction=1)]
    m, _, _ = mech(gm_rows([30] * 7 + [26]), e, name=Mechanism.PRICING_INPUT)
    assert m.state == MechanismState.ADVERSE and m.direction == "negative"
    assert "higher input costs" in m.attribution


def test_gross_margin_needs_cost_rows():
    m, _, _ = mech(revenue_rows(), name=Mechanism.PRICING_INPUT)
    assert m.state == MechanismState.INSUFFICIENT_DATA


def test_input_cost_tailwind_is_flagged_as_reversible():
    e = [ev(Metric.INPUT_COST, "lower raw material costs", date(2024, 2, 10), direction=-1)]
    m, _, _ = mech(gm_rows([30] * 7 + [34]), e, name=Mechanism.PRICING_INPUT)
    assert m.state == MechanismState.EMERGING and any("can reverse" in i for i in m.invalidators)


# --- product / customer mix -------------------------------------------------------------------

def seg_rows(share_a_now, share_a_ya, margin_a=25.0, margin_b=5.0, prev_shift=0.0):
    rows = revenue_rows()
    for d, sh in ((QE[-1], share_a_now), (QE[3], share_a_ya), (QE[-2], share_a_ya + prev_shift), (QE[2], share_a_ya)):
        tot = 100.0
        a, b = tot * sh / 100, tot * (1 - sh / 100)
        rows += [fm(Metric.SEGMENT_REVENUE, d, a, segment="specialty"), fm(Metric.SEGMENT_REVENUE, d, b, segment="commodity"),
                 fm(Metric.SEGMENT_RESULT, d, a * margin_a / 100, segment="specialty"),
                 fm(Metric.SEGMENT_RESULT, d, b * margin_b / 100, segment="commodity")]
    return rows


def test_mix_shift_to_more_profitable_segment():
    m, _, _ = mech(seg_rows(40, 30), name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.EMERGING and m.direction == "positive" and m.magnitude > 0
    m, _, _ = mech(seg_rows(40, 30, prev_shift=5), name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.CONFIRMED


def test_mix_shift_away_from_profitable_segment_is_adverse():
    m, _, _ = mech(seg_rows(25, 35), name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.ADVERSE and m.direction == "negative"


def test_profitable_segment_too_small_does_not_qualify():
    m, _, _ = mech(seg_rows(8, 3), name=Mechanism.PRODUCT_MIX)
    assert not m.qualifies_positive and any("too small" in i for i in m.invalidators)


def test_stated_mix_without_segment_profit_is_a_research_hypothesis():
    e = [ev(Metric.MIX_SHARE, "Lab grown jewellery contributed 51.3% to Q3 revenue compared to 23.5% in Q3 FY23",
            date(2024, 2, 8), 51.3, segment="lab grown jewellery")]
    m, _, _ = mech(revenue_rows(), e, name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.ASSERTION and m.magnitude is None and "23.5% -> 51.3%" in m.hypothesis


def test_mix_missing_data():
    m, _, _ = mech(revenue_rows(), name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.INSUFFICIENT_DATA


# --- debt reduction ---------------------------------------------------------------------------

def bs_rows(debt_a, debt_b, cash=10.0, ocf=None, equity=None, cap_b=10.0):
    a, b = date(2023, 3, 31), date(2023, 9, 30)
    rows = revenue_rows() + [fm(Metric.BORROWINGS_NONCURRENT, a, debt_a, "I"), fm(Metric.CASH, a, cash, "I"),
                             fm(Metric.BORROWINGS_NONCURRENT, b, debt_b, "I"), fm(Metric.CASH, b, cash, "I"),
                             fm(Metric.PAID_UP_CAPITAL, a, 10.0, "I"), fm(Metric.PAID_UP_CAPITAL, b, cap_b, "I"),
                             fm(Metric.FACE_VALUE, a, 10.0, "I"), fm(Metric.FACE_VALUE, b, 10.0, "I")]
    if ocf is not None:
        rows.append(fm(Metric.OPERATING_CASH_FLOW, b, ocf, "H"))
    if equity is not None:
        rows.append(fm(Metric.EQUITY_ISSUED, b, equity, "H"))
    return rows


def test_operating_funded_debt_reduction_differs_from_dilution_funded():
    op, _, _ = mech(bs_rows(200, 120, ocf=90), name=Mechanism.DEBT_REDUCTION)
    assert op.state == MechanismState.EMERGING and op.direction == "positive" and "operating cash" in op.attribution
    eq, _, _ = mech(bs_rows(200, 120, ocf=10, equity=80, cap_b=12.0), name=Mechanism.DEBT_REDUCTION)
    assert eq.direction == "neutral" and "equity-funded" in eq.attribution
    assert any("dilution" in i for i in eq.invalidators)


def test_debt_increase_is_adverse_and_missing_balance_sheet_is_insufficient():
    m, _, _ = mech(bs_rows(120, 200), name=Mechanism.DEBT_REDUCTION)
    assert m.state == MechanismState.ADVERSE and m.direction == "negative"
    m, _, _ = mech(revenue_rows(), name=Mechanism.DEBT_REDUCTION)
    assert m.state == MechanismState.INSUFFICIENT_DATA


# --- segment turnaround -----------------------------------------------------------------------

def turn_rows(ya_res, now_res, prev_now=None, prev_ya=None):
    rows = revenue_rows()
    rows += [fm(Metric.SEGMENT_RESULT, QE[3], ya_res, segment="pumps"),
             fm(Metric.SEGMENT_RESULT, QE[-1], now_res, segment="pumps")]
    if prev_now is not None:
        rows += [fm(Metric.SEGMENT_RESULT, QE[-2], prev_now, segment="pumps"),
                 fm(Metric.SEGMENT_RESULT, QE[2], prev_ya, segment="pumps")]
    return rows


def test_segment_loss_to_profit_turnaround():
    m, _, _ = mech(turn_rows(-5, 4), name=Mechanism.SEGMENT_TURNAROUND)
    assert m.state == MechanismState.EMERGING and m.direction == "positive" and m.magnitude == 9
    m, _, _ = mech(turn_rows(-5, 4, prev_now=2, prev_ya=-3), name=Mechanism.SEGMENT_TURNAROUND)
    assert m.state == MechanismState.CONFIRMED


def test_segment_swing_to_loss_is_adverse_and_missing_is_insufficient():
    m, _, _ = mech(turn_rows(6, -2), name=Mechanism.SEGMENT_TURNAROUND)
    assert m.state == MechanismState.ADVERSE and m.direction == "negative"
    m, _, _ = mech(revenue_rows(), name=Mechanism.SEGMENT_TURNAROUND)
    assert m.state == MechanismState.INSUFFICIENT_DATA


# --- organic volume ---------------------------------------------------------------------------

def test_volume_growth_persistence_and_acquisition_qualification():
    e = [ev(Metric.VOLUME, "Sales volumes grew 18% YoY", date(2023, 11, 10), 18, direction=1),
         ev(Metric.VOLUME, "Sales volumes grew 15% YoY", date(2024, 2, 10), 15, direction=1)]
    m, _, _ = mech(revenue_rows(), e, name=Mechanism.ORGANIC_VOLUME)
    assert m.state == MechanismState.CONFIRMED and m.direction == "positive"
    acq = e + [ev(Metric.ACQUISITION, "completed the acquisition of XYZ Ltd", date(2023, 10, 1))]
    m, _, _ = mech(revenue_rows(), acq, name=Mechanism.ORGANIC_VOLUME)
    assert m.direction == "neutral" and any("inorganic" in i for i in m.invalidators)


def test_volume_decline_and_missing_volume():
    e = [ev(Metric.VOLUME, "Sales volumes declined 12% YoY", date(2024, 2, 10), 12, direction=-1)]
    m, _, _ = mech(revenue_rows(), e, name=Mechanism.ORGANIC_VOLUME)
    assert m.state == MechanismState.ADVERSE
    m, _, _ = mech(revenue_rows(growth=0.4), name=Mechanism.ORGANIC_VOLUME)
    assert m.state == MechanismState.INSUFFICIENT_DATA and "volume vs price" in m.hypothesis


# --- order quality -----------------------------------------------------------------------------

def order(amount, stage=EventStage.BINDING_ORDER, cancelled=0.0, when=date(2023, 11, 1)):
    at = datetime.combine(when, datetime.min.time(), tzinfo=IST)
    hist = [EventStateChange(EventStage.BINDING_ORDER, at, "o1", "x", amount)]
    if cancelled:
        hist.append(EventStateChange(EventStage.CANCELLED, at.replace(month=12), "o2", "y", amount - cancelled))
    return EconomicEvent(event_id=f"evt{amount}{stage.value}", ticker="T", kind=Metric.ORDER_WIN,
                         amount=Quantity(amount - cancelled, Unit.INR_CRORE, ""), counterparty="Grid Corp Limited",
                         commitment_strength=CommitmentStrength.BINDING, first_public_at=at, doc_ids=["o1"],
                         evidence_ids=["x"], current_stage=EventStage.CANCELLED if cancelled >= amount else stage,
                         history=hist, original_amount=Quantity(amount, Unit.INR_CRORE, ""),
                         cancelled_amount=cancelled, customer_verification=CustomerVerification.ISSUER_NAMED,
                         relationship=RelationshipStatus.UNKNOWN, value_basis=ValueBasis.FIRM)


def test_order_quality_commitment_adverse_and_missing():
    m, _, _ = mech(revenue_rows(), events=[order(300)], name=Mechanism.ORDER_QUALITY)
    assert m.state == MechanismState.COMMITMENT and m.direction == "positive"
    assert any("payment terms" in i for i in m.invalidators)
    m, _, _ = mech(revenue_rows(), events=[order(300, cancelled=200)], name=Mechanism.ORDER_QUALITY)
    assert m.state == MechanismState.ADVERSE
    m, _, _ = mech(revenue_rows(), name=Mechanism.ORDER_QUALITY)
    assert m.state == MechanismState.INSUFFICIENT_DATA


# --- no double counting ------------------------------------------------------------------------

def test_margin_mechanisms_describing_the_same_change_are_cross_referenced():
    rows = gm_rows([30] * 7 + [34]) + seg_rows(40, 30)[len(revenue_rows()):]
    e = [ev(Metric.UTILIZATION, "Plant A utilisation was 60%", date(2023, 8, 10), 60, facility="plant a"),
         ev(Metric.UTILIZATION, "Plant A utilisation was 80%", date(2024, 2, 10), 80, facility="plant a")]
    out, _, _ = mech(rows, e)
    pos = {m.mechanism: m for m in out if m.qualifies_positive}
    assert {Mechanism.PRICING_INPUT, Mechanism.PRODUCT_MIX, Mechanism.UTILIZATION} <= set(pos)
    assert "utilization" in pos[Mechanism.PRICING_INPUT].overlaps_with


# --- extraction inputs for the mechanisms --------------------------------------------------

DFPCL_SEGMENTS = """STATEMENT OF UNAUDITED CONSOLIDATED FINANCIAL RESULTS FOR THE QUARTER AND NINE MONTHS ENDED 31 DECEMBER 2023
UNAUDITED SEGMENT-WISE REVENUE, RESULTS, ASSETS AND LIABILITIES (Amounts in Rs Lakhs unless otherwise stated)
Consolidated
Sr. Particulars Quarter Ended Nine Months Ended Year Ended
No. 31 December 2023 30 September 2023 31 December 2022 31 December 2023 31 December 2022 31 March 2023
(Unaudited) (Unaudited) (Unaudited) (Unaudited) (Unaudited) (Audited)
1 Segment revenue
(a) Chemicals
Manufactured 1,02,639 1,15,285 1,59, 187 3,38,300 4,85,633 6,32,802
Traded 501 1,743 2,293 5694 6,215 8,332
Total 1,03,140 1,17,028 1,61,480 3,43,994 4,91,848 6,41,134
(b) Fertilisers
Total 81,568 1,24,883 1,13,446 3,13,327 3,57,108 4,86,831
(c) Realty 398 361 550 1,182 1,559 1,412
l'l d\\ Others 158 144 - 478 - 692
Total income from operations 1,85,264 2,42,416 2,75,476 6,58,981 8,50,515 11,30,069
2 Segment results [profit/ (loss) before tax and finance costs]
(a) Chemicals 24,394 25,026 45,054 83,148 1,61,370 1,99,170
(b) Fertilisers (76) 4,226 5,904 8,111 22,021 26,602
Less: Unallocable expenditure (net of unallocable income) 3,688 3,600 2,200 10,000 9,000 12,000
"""


def test_segment_rows_are_parsed_with_clean_names():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.contracts import SourceDocument
    from makrograph.earnings_inflection.extraction import parse_results_tables
    d = SourceDocument(doc_id="S", source_name="t", ticker="T", text=DFPCL_SEGMENTS,
                       published_at=datetime(2024, 2, 1, tzinfo=IST))
    d.available_at = d.published_at
    rows, _ = parse_results_tables(d, chunk_document(d))
    seg = {(r.metric, r.segment): r.value for r in rows if r.segment and r.period_end == QE[-1] and r.period_type == "Q"}
    assert seg[(Metric.SEGMENT_REVENUE, "chemicals")] == pytest.approx(1031.40)
    assert seg[(Metric.SEGMENT_REVENUE, "others")] == pytest.approx(1.58)
    assert seg[(Metric.SEGMENT_RESULT, "fertilisers")] == pytest.approx(-0.76)
    assert not [k for k in seg if "unallocable" in k[1] or "manufactured" in k[1] or "total" in k[1]]
    assert not [r for r in rows if r.metric == Metric.REVENUE]          # segment totals are not company revenue


def test_integer_table_scanned_thousands_dot():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.contracts import SourceDocument
    from makrograph.earnings_inflection.extraction import parse_results_tables
    text = """STATEMENT OF UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED 31 DECEMBER 2023
(Rs. in lakhs)
Particulars Quarter ended Quarter ended Quarter ended
31-Dec-23 30-Sep-23 31-Dec-22
Revenue from operations 13,947 9,581 8,362
Cost of materials consumed 10,573 10,498 7,118
Changes in inventories of finished goods and work-in-progress (168) (3,710) (1.076)
Employee benefits expense 737 689 764
Finance costs 56 128 65
Other expenses 1,280 980 1,047
"""
    d = SourceDocument(doc_id="I", source_name="t", ticker="T", text=text, published_at=datetime(2024, 2, 1, tzinfo=IST))
    d.available_at = d.published_at
    rows, _ = parse_results_tables(d, chunk_document(d))
    inv = {r.period_end: r.value for r in rows if r.metric == Metric.INVENTORY_CHANGE}
    assert inv[date(2022, 12, 31)] == pytest.approx(-10.76)


@pytest.mark.parametrize("text,metric,direction", [
    ("EBITDA margin improved on lower raw material costs and better realisations.", Metric.INPUT_COST, -1),
    ("EBITDA margin improved on lower raw material costs and better realisations.", Metric.PRICING, 1),
    ("Sales volumes declined 6% YoY in the quarter.", Metric.VOLUME, -1),
])
def test_mechanism_statements_carry_their_local_direction(text, metric, direction):
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.contracts import SourceDocument
    from makrograph.earnings_inflection.extraction import extract_sentence_evidence
    d = SourceDocument(doc_id="x", source_name="t", ticker="T", text=text)
    evs = [e for e in extract_sentence_evidence(d, chunk_document(d)) if e.metric == metric]
    assert evs and evs[0].direction == direction
