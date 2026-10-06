"""Forward-looking thesis: leading candidates, first-results validation, confidence upgrade,
per-mechanism positive/negative evidence and stale preservation.  Synthetic issuer."""

from datetime import date, datetime, timedelta

from makrograph.earnings_inflection.assessments import decide_status
from makrograph.earnings_inflection.contracts import (
    IST, Evidence, EvidenceStatus, EvidenceTier, FinancialMeasurement, Mechanism, MechanismState, Metric,
    MilestoneOutcome, Modality, Quantity, Scope, ThesisStage, Unit,
)
from makrograph.earnings_inflection.drivers import compute_drivers
from makrograph.earnings_inflection.financial_series import FinancialSeries
from makrograph.earnings_inflection.identity import IssuerModel
from makrograph.earnings_inflection.mechanisms import detect_mechanisms
from makrograph.earnings_inflection.thesis import build_thesis, order_book_snapshots

QE = [date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31), date(2023, 3, 31),
      date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31)]


def published(end):
    return datetime.combine(end + timedelta(days=45), datetime.min.time(), tzinfo=IST)


def fm(metric, end, value, when=None, doc=None):
    when = when or published(end)
    return FinancialMeasurement("T", metric, end, "Q", value, Unit.INR_CRORE, Scope.STANDALONE,
                                doc or f"r{end:%y%m}", when, display_unit=0.01)


def rows(revenue, cost=None):
    out = [fm(Metric.REVENUE, d, v) for d, v in zip(QE, revenue)]
    if cost:
        out += [fm(Metric.COST_OF_MATERIALS, d, v) for d, v in zip(QE, cost)]
    return out


def book(value, when, doc, text=None):
    return Evidence(evidence_id=f"ob{doc}", doc_id=doc, ticker="T", metric=Metric.ORDER_BOOK,
                    tier=EvidenceTier.COMMERCIAL_COMMITMENT, modality=Modality.REALIZED,
                    quote=text or f"The company's order book stood at Rs. {value:g} crore as of the date.",
                    available_at=datetime.combine(when, datetime.min.time(), tzinfo=IST),
                    quantity=Quantity(value, Unit.INR_CRORE, f"{value}"))


def stated(metric, quote, when, value=None, direction=0):
    return Evidence(evidence_id=f"s{abs(hash((quote, when)))}", doc_id=f"d{when:%y%m%d}", ticker="T", metric=metric,
                    tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=Modality.REALIZED, quote=quote,
                    available_at=datetime.combine(when, datetime.min.time(), tzinfo=IST),
                    quantity=Quantity(value, Unit.PERCENT, f"{value}%") if value is not None else None,
                    direction=direction)


def thesis(all_rows, evidence, as_of, stale_note=""):
    cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=IST)
    visible = [r for r in all_rows if r.available_at <= cutoff]
    ev = [e for e in evidence if e.available_at <= cutoff]
    s = FinancialSeries.build("T", visible)
    drivers, _ = compute_drivers(s, [], ev, IssuerModel.OPERATING, as_of)
    mechs = detect_mechanisms(s, ev, [], drivers, as_of)
    return build_thesis(mechs, s, ev, [], as_of, stale_note=stale_note), drivers, mechs, ev


def mt(t, mech):
    return next(x for x in t.mechanisms if x.mechanism == mech)


FLAT_THEN_UP = [100, 100, 100, 100, 100, 100, 140, 140, 140]     # growth from the Sep-2023 quarter
BOOKS = [book(300, date(2022, 8, 10), "rating22"), book(450, date(2023, 8, 20), "rating23")]


def test_leading_candidate_surfaces_before_any_results_show_it():
    t, drivers, mechs, ev = thesis(rows(FLAT_THEN_UP), BOOKS, date(2023, 9, 30))
    o = mt(t, Mechanism.ORDER_QUALITY)
    assert o.stage == ThesisStage.LEADING and o.confidence == "low"
    assert "300.0 -> 450.0" in o.leading_signal and o.leading_signal_at.date() == date(2023, 8, 20)
    assert o.validation.outcome == MilestoneOutcome.PENDING and o.validation.expected_by == date(2023, 11, 14)
    assert t.stage == ThesisStage.LEADING and t.earliest_defensible_at.date() == date(2023, 8, 20)
    # the backward-looking evidence status is unchanged: no realized growth yet
    status, _ = decide_status(drivers, [], ev, [], 2, 400.0, datetime(2023, 9, 30, tzinfo=IST), mechanisms=mechs)
    assert status not in (EvidenceStatus.EXECUTION_EMERGING, EvidenceStatus.EXECUTION_CONFIRMED)


def test_first_results_validate_then_a_second_period_raises_confidence():
    t, *_ = thesis(rows(FLAT_THEN_UP), BOOKS, date(2023, 12, 31))
    o = mt(t, Mechanism.ORDER_QUALITY)
    assert o.stage == ThesisStage.FIRST_RESULTS_VALIDATED and o.confidence == "medium"
    assert o.validation.period_end == date(2023, 9, 30) and "+40.0%" in o.validation.observed
    t, *_ = thesis(rows(FLAT_THEN_UP), BOOKS, date(2024, 3, 31))
    o = mt(t, Mechanism.ORDER_QUALITY)
    assert o.stage == ThesisStage.CONFIRMED and o.confidence == "high" and "2023-12-31" in o.confirmation
    assert o.earliest_defensible_at.date() == date(2023, 8, 20)        # admission date does not move


def test_first_results_that_do_not_show_it_and_adverse_results():
    t, *_ = thesis(rows([100] * 9), BOOKS, date(2023, 12, 31))
    o = mt(t, Mechanism.ORDER_QUALITY)
    assert o.stage == ThesisStage.FIRST_RESULTS_NOT_VALIDATED and o.confidence == "low"
    assert any("revenue +0.0%" in x for x in [o.validation.observed])
    t, *_ = thesis(rows([100, 100, 100, 100, 100, 100, 80, 80, 80]), BOOKS, date(2023, 12, 31))
    o = mt(t, Mechanism.ORDER_QUALITY)
    assert o.stage == ThesisStage.ADVERSE and any("-20.0%" in x for x in o.negative_evidence)


def test_results_due_but_not_parsed_are_overdue_not_a_verdict():
    r = [x for x in rows(FLAT_THEN_UP) if x.period_end != date(2023, 9, 30)]
    t, *_ = thesis(r, BOOKS, date(2024, 1, 31))
    o = mt(t, Mechanism.ORDER_QUALITY)
    # the next published quarter (Dec-2023) is not yet out on 31 Jan: the Sep-2023 one is missing
    assert o.validation.outcome == MilestoneOutcome.OVERDUE and o.stage == ThesisStage.LEADING and o.stale


def test_a_high_but_flat_order_book_is_not_a_change():
    flat = [book(900, date(2022, 8, 10), "a"), book(920, date(2023, 8, 20), "b")]     # 2x+ revenue, flat
    t, *_ = thesis(rows(FLAT_THEN_UP), flat, date(2023, 9, 30))
    assert mt(t, Mechanism.ORDER_QUALITY).stage == ThesisStage.NONE


def test_order_book_amount_must_follow_the_words_order_book():
    wrong = book(504, date(2023, 8, 20), "c", "Strong order book position - In FY2024, the company reported an "
                                             "operating income of ~Rs. 504 crore, up 36%.")
    assert order_book_snapshots([BOOKS[0], wrong]) == [BOOKS[0]]
    assert order_book_snapshots([book(437, date(2023, 7, 7), "d", "pending order book of Rs. 437 crore as on "
                                                                     "June 17, 2023")])


def test_pricing_signal_needs_a_stated_size_and_is_judged_on_gross_margin():
    cost = [70] * 6 + [84, 84, 84]                                  # revenue 140, cost 84: margin 30% -> 40%
    vague = [stated(Metric.PRICING, "Realisations improved during the quarter.", date(2023, 8, 20), direction=1)]
    t, *_ = thesis(rows(FLAT_THEN_UP, cost), vague, date(2023, 9, 30))
    assert mt(t, Mechanism.PRICING_INPUT).stage == ThesisStage.NONE
    sized = [stated(Metric.PRICING, "We increased prices by 6% across the range.", date(2023, 8, 20), 6, 1)]
    t, *_ = thesis(rows(FLAT_THEN_UP, cost), sized, date(2023, 9, 30))
    assert mt(t, Mechanism.PRICING_INPUT).stage == ThesisStage.LEADING
    t, *_ = thesis(rows(FLAT_THEN_UP, cost), sized, date(2023, 12, 31))
    p = mt(t, Mechanism.PRICING_INPUT)
    assert p.stage in (ThesisStage.FIRST_RESULTS_VALIDATED, ThesisStage.CONFIRMED)
    assert "+1000 bps" in p.validation.observed


def test_each_mechanism_keeps_its_own_evidence_and_outcomes_are_not_attributed():
    t, *_ = thesis(rows(FLAT_THEN_UP), BOOKS, date(2024, 3, 31))
    assert t.outcome_history and all("revenue" in x for x in t.outcome_history)
    other = [m for m in t.mechanisms if m.mechanism not in (Mechanism.ORDER_QUALITY,)]
    assert not any(x.startswith("after the signal") for m in other for x in m.positive_evidence)


def test_stale_series_keeps_the_last_reading_but_never_upgrades_the_status():
    as_of = date(2025, 6, 30)                                   # results after Mar-2024 never parsed
    t, drivers, mechs, ev = thesis(rows(FLAT_THEN_UP), BOOKS, as_of, stale_note="latest quarter is old")
    assert all(m.stale for m in mechs if m.mechanism != Mechanism.DEBT_REDUCTION)
    assert not any(m.qualifies_positive for m in mechs)
    assert t.stale and "last known reading" in " ".join(t.notes)
    status, _ = decide_status(drivers, [], ev, [], 2, None, datetime(2025, 6, 30, tzinfo=IST), mechanisms=mechs)
    assert status != EvidenceStatus.EXECUTION_CONFIRMED


def test_first_public_time_is_not_moved_by_later_comparative_repeats():
    r = rows(FLAT_THEN_UP)
    repeat = fm(Metric.REVENUE, date(2023, 9, 30), 140, when=datetime(2024, 11, 14, tzinfo=IST), doc="later")
    s = FinancialSeries.build("T", r + [repeat])
    pt = s.get(Metric.REVENUE, date(2023, 9, 30))
    assert pt.first_public_at == published(date(2023, 9, 30)) and pt.available_at.year == 2024


# --- replay evaluation (outcome sandbox) ----------------------------------------------------

def snap(as_of, status, mechs):
    return {"ticker": "T", "as_of": as_of, "evidence_status": status, "mechanisms": mechs}


def lead(mech, at, stage, outcome, observed_at=None, period_end=None):
    return {"mechanism": mech, "stage": stage, "confidence": "low", "leading_signal": "book up",
            "leading_signal_at": at, "earliest_defensible_at": at, "confirmation": "", "stale": False,
            "validation": {"outcome": outcome, "metric_basis": "", "expected_by": None, "period_end": period_end,
                           "observed": "revenue +40.0% YoY", "observed_at": observed_at}}


def test_timeline_scores_lead_time_false_alarms_and_unresolved_signals():
    from makrograph.earnings_inflection.evaluation import thesis_timeline, timeline_report
    snaps = [
        snap("2023-08-31", "ASSERTION_ONLY", [lead("order_quality", "2023-08-20", "leading", "pending"),
                                              lead("pricing_input_costs", "2023-08-25", "leading", "pending")]),
        snap("2023-11-30", "EXECUTION_EMERGING", [
            lead("order_quality", "2023-08-20", "first_results_validated", "validated", "2023-11-14", "2023-09-30"),
            lead("pricing_input_costs", "2023-08-25", "first_results_not_validated", "not_validated",
                 "2023-11-14", "2023-09-30"),
            lead("product_customer_mix", "2023-11-20", "leading", "pending")]),
        snap("2024-02-29", "EXECUTION_CONFIRMED", [
            lead("order_quality", "2023-08-20", "confirmed", "validated", "2023-11-14", "2023-09-30")]),
    ]
    tl = thesis_timeline(snaps)
    assert (tl["leading_episodes"], tl["validated"], tl["false_alarms"], tl["unresolved"]) == (3, 1, 1, 1)
    assert tl["lead_times_days"] == [86]
    assert tl["earliest_defensible_signal"] == date(2023, 8, 20)          # false alarm does not count
    assert tl["days_before_first_confirmed_status"] == (date(2024, 2, 29) - date(2023, 8, 20)).days
    oq = next(e for e in tl["episodes"] if e.mechanism == "order_quality")
    assert oq.confirmed_as_of == date(2024, 2, 29) and oq.first_seen_as_of == date(2023, 8, 31)
    md = timeline_report("T", tl)
    assert "false alarms 1" in md and "not a validation sample" in md


def test_earnings_delivery_is_measured_or_censored_never_zero():
    from makrograph.earnings_inflection.evaluation import ThesisEpisode, earnings_delivery
    r = rows(FLAT_THEN_UP) + [fm(Metric.PAT, d, v) for d, v in zip(QE, [5] * 6 + [9, 9, 9])]
    s = FinancialSeries.build("T", r)
    ep = ThesisEpisode("order_quality", "leading", date(2023, 3, 1), "book up", date(2023, 3, 31))
    d = earnings_delivery(ep, s)
    assert not d["censored"] and d["base_period"] == "2022-12-31" and d["horizon_period"] == "2023-12-31"
    assert round(d["ttm_revenue_change_pct"]) == 20 and round(d["ttm_pat_change_pct"]) == 40
    late = ThesisEpisode("order_quality", "leading", date(2023, 8, 20), "book up", date(2023, 8, 31))
    d = earnings_delivery(late, s)
    assert d["censored"] and d.get("ttm_pat_change_pct") is None


def test_cli_replay_writes_snapshots_and_a_timeline(tmp_path):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("ei_cli", root / "scripts/earnings_inflection.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    rc = cli.main(["--fixtures", str(Path(__file__).parent / "fixtures"), "--ticker", "ACMEGRID",
                   "--replay-from", "2024-08-01", "--replay-to", "2024-10-31", "--out", str(tmp_path)])
    assert rc == 0
    import json
    snaps = json.loads((tmp_path / "ACMEGRID_replay.json").read_text())
    assert [s["as_of"] for s in snaps] == ["2024-08-31", "2024-09-30", "2024-10-31"]
    assert "Replay timeline" in (tmp_path / "ACMEGRID_timeline.md").read_text()
