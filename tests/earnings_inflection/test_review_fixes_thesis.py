"""Regression tests for the correctness review of the thesis / replay / parser changes."""

from datetime import date, datetime

from makrograph.earnings_inflection import thesis as T
from makrograph.earnings_inflection.chunking import split_numeric_row
from makrograph.earnings_inflection.contracts import (
    IST, CustomerVerification, EconomicEvent, EventStage, EventStateChange, Evidence, EvidenceTier, Metric,
    Modality, Quantity, Unit, ValueBasis,
)
from makrograph.earnings_inflection.evaluation import ThesisEpisode, earnings_delivery
from makrograph.earnings_inflection.financial_series import FinancialSeries

from .test_thesis import BOOKS, FLAT_THEN_UP, book, fm, rows, thesis, mt


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=IST)


def test_order_signal_is_dated_when_the_order_became_binding():
    s = FinancialSeries.build("T", rows([100] * 9))
    e = EconomicEvent("e1", "T", Metric.ORDER_WIN, Quantity(200, Unit.INR_CRORE, "200"), "NTPC", dt(2024, 1, 10),
                      evidence_ids=["x"], doc_ids=["d1"], current_stage=EventStage.BINDING_ORDER,
                      history=[EventStateChange(EventStage.PREFERRED_BIDDER, dt(2024, 1, 10), "d1", "x"),
                               EventStateChange(EventStage.BINDING_ORDER, dt(2024, 3, 5), "d2", "y")],
                      customer_verification=CustomerVerification.ISSUER_NAMED, value_basis=ValueBasis.FIRM)
    sig = T._leading_orders(s, [], [e], "Q", date(2024, 3, 31), T.DEFAULT_THESIS_THRESHOLDS)
    assert sig and sig[0][0] == dt(2024, 3, 5)          # not the L1 / LoI date


def mix(quote, direction=0):
    return Evidence(evidence_id=f"m{abs(hash(quote))}", doc_id="d", ticker="T", metric=Metric.MIX_SHARE,
                    tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=Modality.REALIZED, quote=quote,
                    available_at=dt(2024, 2, 1), direction=direction)


def test_a_declining_mix_share_is_not_a_positive_signal():
    assert T._leading_mix([mix("Share of premium products declined from 35% to 28%.", -1)]) == []
    assert T._leading_mix([mix("Share of premium products increased from 28% to 35%.", 1)])
    assert T._leading_mix([mix("LGD jewellery contributed 51.3% of sales compared to 23.5% last year.")])


def cap(quote, when):
    return Evidence(evidence_id=f"c{when}", doc_id=f"d{when}", ticker="T", metric=Metric.CAPACITY,
                    tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=Modality.REALIZED, quote=quote,
                    available_at=datetime.combine(when, datetime.min.time(), tzinfo=IST))


def test_an_old_capacity_signal_does_not_hide_a_newer_one():
    ev = [cap("The company increased capacity from 100 MW to 150 MW.", date(2021, 5, 1)),
          cap("The company increased capacity from 150 MW to 300 MW.", date(2024, 5, 1))]
    s = FinancialSeries.build("T", rows([100] * 9))
    sig = T.leading_signals(T.Mechanism.UTILIZATION, s, ev, [], "Q", date(2024, 6, 30), T.DEFAULT_THESIS_THRESHOLDS)
    assert [x[0].date() for x in sig] == [date(2024, 5, 1)]


def test_validation_never_uses_a_quarter_that_ended_before_the_signal():
    # signal 2023-08-20: the Jun-2023 quarter (public 2023-08-14) predates it; Sep-2023 is the first
    t, *_ = thesis(rows(FLAT_THEN_UP), BOOKS, date(2023, 12, 31))
    assert mt(t, T.Mechanism.ORDER_QUALITY).validation.period_end == date(2023, 9, 30)
    early = [book(300, date(2022, 8, 10), "a"), book(450, date(2023, 7, 20), "b")]
    t, *_ = thesis(rows([100] * 5 + [140] * 4), early, date(2023, 9, 30))      # Jun-23 +40% but ends before
    v = mt(t, T.Mechanism.ORDER_QUALITY).validation
    assert v.period_end is None and v.outcome.value == "pending" and v.expected_by == date(2023, 11, 14)


def test_half_year_deadlines_follow_september_and_march_ends():
    th = T.DEFAULT_THESIS_THRESHOLDS
    assert T._deadline(dt(2025, 8, 1), "H", th) == date(2025, 11, 14)
    assert T._deadline(dt(2025, 2, 1), "H", th) == date(2025, 5, 30)
    assert T._deadline(dt(2025, 2, 1), "Q", th) == date(2025, 5, 30)


def test_delivery_horizon_counts_calendar_periods_and_censors_gaps():
    r = [x for x in rows(FLAT_THEN_UP) if x.period_end not in (date(2023, 3, 31), date(2023, 6, 30))]
    s = FinancialSeries.build("T", r + [fm(Metric.PAT, d, 5) for d in (date(2022, 12, 31),)])
    ep = ThesisEpisode("order_quality", "leading", date(2023, 3, 1), "x", date(2023, 3, 31))
    d = earnings_delivery(ep, s)
    assert d["horizon_period"] == "2023-12-31"
    s2 = FinancialSeries.build("T", [x for x in rows(FLAT_THEN_UP) if x.period_end != date(2023, 12, 31)])
    d2 = earnings_delivery(ep, s2)
    assert d2["censored"] and "2023-12-31" in d2["reason"]


def test_spaced_groups_are_not_merged_on_a_single_column_gap():
    assert split_numeric_row("Other expenses (note 3)  45 120")[1][-2:] == ["45", "120"]
    assert split_numeric_row("A   Revenue     17,734      40,572      50 321")[1] == ["17,734", "40,572", "50,321"]


def test_a_stale_confirmed_mechanism_does_not_upgrade_the_thesis():
    from makrograph.earnings_inflection.contracts import MechanismResult, MechanismState, ThesisStage
    s = FinancialSeries.build("T", rows(FLAT_THEN_UP))
    mr = MechanismResult(T.Mechanism.PRICING_INPUT, MechanismState.CONFIRMED, direction="positive", stale=True,
                         durability="two periods")
    t = T.mechanism_thesis(mr, s, [], [], date(2025, 6, 30), T.DEFAULT_THESIS_THRESHOLDS, "old series")
    assert t.stage != ThesisStage.CONFIRMED
