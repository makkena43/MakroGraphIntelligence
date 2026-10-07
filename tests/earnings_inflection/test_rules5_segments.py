"""D10 (catalyst-rules-5): segment tables are used for mix / turnaround readings only when they are
consistent: spellings of one segment are merged, company totals are not segments, and segment
revenues must add up to reported revenue.  Olectra's misread table produced "insulator division
share 0.1% -> 94.9%" under rules-4."""

from makrograph.earnings_inflection.contracts import Mechanism, MechanismState, Metric

from .test_wp5_mechanisms import QE, fm, mech, revenue_rows, seg_rows


def test_baseline_mix_shift_still_detected():
    m, _, _ = mech(seg_rows(40, 30), name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.EMERGING


def test_a_table_that_does_not_reconcile_gives_no_mix_reading():
    rows = [r for r in seg_rows(40, 30) if not (r.segment == "commodity" and r.period_end == QE[3])]
    rows += [fm(Metric.SEGMENT_REVENUE, QE[3], 20.0, segment="commodity"),       # year-ago: 30 + 20 = 50 vs 100
             fm(Metric.SEGMENT_RESULT, QE[3], 1.0, segment="commodity")]
    m, _, _ = mech(rows, name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.INSUFFICIENT_DATA


def test_company_total_rows_are_not_segments():
    rows = seg_rows(40, 30)
    for d in (QE[-1], QE[3], QE[-2], QE[2]):
        rows += [fm(Metric.SEGMENT_REVENUE, d, 100.0, segment="net revenue from operations"),
                 fm(Metric.SEGMENT_RESULT, d, -4.0, segment="income")]
    m, _, _ = mech(rows, name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.EMERGING and "specialty" in m.magnitude_basis


def test_scanned_spellings_of_one_segment_are_merged():
    rows = [r.__class__(**{**r.__dict__, "segment": "speci alty"}) if r.segment == "specialty"
            and r.period_end in (QE[3], QE[2]) else r for r in seg_rows(40, 30)]
    m, _, _ = mech(rows, name=Mechanism.PRODUCT_MIX)
    assert m.state == MechanismState.EMERGING


def test_segment_turnaround_needs_a_reconciling_table_when_revenue_is_reported():
    rows = revenue_rows()
    for d, res in ((QE[3], -5.0), (QE[-1], 4.0)):
        rows += [fm(Metric.SEGMENT_REVENUE, d, 30.0, segment="pumps"),         # 30 of ~100 reported: misread
                 fm(Metric.SEGMENT_RESULT, d, res, segment="pumps")]
    m, _, _ = mech(rows, name=Mechanism.SEGMENT_TURNAROUND)
    assert m.state == MechanismState.INSUFFICIENT_DATA and any("reconcile" in n for n in m.notes)
