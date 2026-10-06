"""Forward catalysts: discovery without current growth, mechanism chain, thesis-specific
confirmation on relevant periods, research stages, persistence and the review gate.
Synthetic issuer; thresholds fixed in catalysts.DEFAULT_CATALYST_THRESHOLDS before these cases."""

from datetime import date, datetime, timedelta

import pytest

from makrograph.earnings_inflection.catalyst_ledger import CatalystLedger
from makrograph.earnings_inflection.catalysts import (
    DEFAULT_CATALYST_THRESHOLDS, RULES_VERSION, detect_catalysts, investment_review, parse_when, research_summary,
)
from makrograph.earnings_inflection.contracts import (
    IST, CatalystKind, CustomerVerification, EconomicEvent, EventStage, EventStateChange, Evidence, EvidenceTier,
    FinancialMeasurement, Metric, MilestoneStatus, Modality, Quantity, RatingRationale, RationaleFact,
    ResearchStage, Scope, Unit, ValueBasis,
)
from makrograph.earnings_inflection.evaluation import (
    catalyst_report, catalyst_timeline, missed_candidates, review_entries, rules_fingerprint,
)
from makrograph.earnings_inflection.financial_series import FinancialSeries

QE = [date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31), date(2023, 3, 31),
      date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31), date(2024, 6, 30),
      date(2024, 9, 30), date(2024, 12, 31)]


def pub(end):
    return datetime.combine(end + timedelta(days=45), datetime.min.time(), tzinfo=IST)


def fm(metric, end, value):
    return FinancialMeasurement("T", metric, end, "Q", value, Unit.INR_CRORE, Scope.STANDALONE, f"r{end:%y%m}",
                                pub(end), display_unit=0.01)


def rows(revenue, margin=0.15, skip=()):
    out = []
    for d, v in zip(QE, revenue):
        if d in skip:
            continue
        out += [fm(Metric.REVENUE, d, v), fm(Metric.EBITDA, d, round(v * margin, 2))]
    return out


def at(d):
    return datetime.combine(d, datetime.min.time(), tzinfo=IST)


def book(value, when, doc):
    return Evidence(evidence_id=f"ob{doc}", doc_id=doc, ticker="T", metric=Metric.ORDER_BOOK,
                    tier=EvidenceTier.COMMERCIAL_COMMITMENT, modality=Modality.REALIZED,
                    quote=f"The company's order book stood at Rs. {value:g} crore as of the quarter end.",
                    available_at=at(when), quantity=Quantity(value, Unit.INR_CRORE, f"{value}"))


def stmt(metric, quote, when, modality=Modality.REALIZED, doc=None):
    return Evidence(evidence_id=f"s{abs(hash((quote, when)))}", doc_id=doc or f"d{when:%y%m%d}", ticker="T",
                    metric=metric, tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=modality, quote=quote,
                    available_at=at(when))


def run(rs, evidence, as_of, events=(), rationales=(), th=None):
    cutoff = at(as_of) + timedelta(days=1)
    vis = [r for r in rs if r.available_at < cutoff]
    s = FinancialSeries.build("T", vis)
    ev = [e for e in evidence if e.available_at < cutoff]
    return detect_catalysts("T", s, ev, list(events), [], list(rationales), as_of, th, measurements=vis), s


def kind(cats, k):
    return [c for c in cats if c.kind == k]


FLAT = [100] * 6                      # Mar-22 .. Jun-23 flat: no current growth at detection
BOOKS = [book(250, date(2022, 8, 10), "b22"), book(500, date(2023, 8, 20), "b23")]   # +100%, 12-month default


def test_discovery_does_not_require_current_growth_and_records_performance_separately():
    cats, _ = run(rows(FLAT + [100] * 6), BOOKS, date(2023, 9, 15))
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.first_public_at.date() == date(2023, 8, 20)
    assert o.stage == ResearchStage.SUPPORTED                     # credible mechanism + materiality + pathway
    assert o.contribution.status == "estimated" and o.contribution.base_crore == pytest.approx(15.0)  # (500-400)x15%
    assert o.contribution.downside_crore < o.contribution.base_crore <= o.contribution.upside_crore
    assert "assumed" in o.window_basis                             # horizon not disclosed: labelled assumption
    rs = research_summary(cats, "NO_MATERIAL_CHANGE", ["no driver crossed materiality thresholds"])
    assert rs["detected"] == "credible prospective earnings change"
    assert rs["current_performance"]["evidence_status"] == "NO_MATERIAL_CHANGE"
    assert any("converting on schedule" in w for w in rs["waiting_for"])
    assert rs["investment_review"].startswith("not started")


def test_quarters_before_the_catalyst_never_count_and_a_muted_quarter_stays_consistent():
    # Jun-23 (before the catalyst) +40% must not count; Sep-23 muted +5%, Dec-23 +45%
    rev = [100] * 5 + [140, 105, 145] + [140] * 4
    books = [book(250, date(2022, 8, 10), "b22"), book(560, date(2023, 8, 20), "b23")]   # TTM then 440
    cats, _ = run(rows(rev), books, date(2023, 12, 1))
    o = kind(cats, CatalystKind.ORDERS)[0]
    conv = o.milestones[0]
    assert conv.relevant_from == date(2023, 9, 30) and conv.status == MilestoneStatus.CONSISTENT_MUTED
    assert o.stage == ResearchStage.SUPPORTED                      # muted, not failed
    cats, s = run(rows(rev), books, date(2024, 3, 1))
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.milestones[0].status == MilestoneStatus.MET and o.milestones[0].periods_judged == 2   # +25% together
    assert o.stage == ResearchStage.CONFIRMED
    rv = investment_review(cats, s, [], [], [], None, {}, [], date(2024, 3, 1))
    assert rv.status == "inputs_incomplete" and "valuation (market data)" in rv.missing
    assert rv.remaining_upside and "not authorise" in rv.note


def test_execution_beyond_the_run_rate_without_a_stated_horizon_is_only_potential():
    big = [book(250, date(2022, 8, 10), "b22"), book(600, date(2023, 8, 20), "b23")]   # needs 600/yr vs 400
    cats, _ = run(rows(FLAT + [100] * 6), big, date(2023, 9, 15))
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.stage == ResearchStage.POTENTIAL
    assert {x.link: x.status for x in o.chain}["deliverable_capacity"] == "unknown"


def test_a_cancelled_order_contradicts_even_with_strong_growth():
    ev = EconomicEvent("e1", "T", Metric.ORDER_WIN, Quantity(300, Unit.INR_CRORE, "300"), "Utility A",
                       at(date(2023, 8, 25)), evidence_ids=["x"], doc_ids=["d1"], current_stage=EventStage.CANCELLED,
                       history=[EventStateChange(EventStage.BINDING_ORDER, at(date(2023, 8, 25)), "d1", "x"),
                                EventStateChange(EventStage.CANCELLED, at(date(2023, 11, 20)), "d2", "y")],
                       customer_verification=CustomerVerification.ISSUER_NAMED, value_basis=ValueBasis.FIRM)
    cats, _ = run(rows([100] * 6 + [160] * 6), BOOKS, date(2024, 3, 1), events=[ev])
    o = [c for c in kind(cats, CatalystKind.ORDERS) if "order book" in c.operating_change][0]
    assert o.stage == ResearchStage.CONTRADICTED and "cancel" in o.stage_reasons[0]


CAP_PLAN = stmt(Metric.CAPACITY, "The Board approved capex to increase the production capacity from 100 MW to 200 MW, "
                                 "expected by March 2024.", date(2023, 6, 1), Modality.FORWARD, "plan")


def test_capacity_without_demand_stays_potential_and_magnitude_unresolved():
    cats, _ = run(rows([100] * 12), [CAP_PLAN], date(2023, 7, 1))
    c = kind(cats, CatalystKind.CAPACITY)[0]
    assert c.stage == ResearchStage.POTENTIAL
    assert c.chain[0].status == "unsupported" and "depreciation" in c.chain[0].basis
    assert c.contribution.status == "potentially_material_unresolved"
    assert c.window_start == date(2024, 3, 31)


def test_capacity_contribution_is_bounded_by_demand_not_by_capacity():
    ev = [CAP_PLAN, book(500, date(2023, 6, 20), "bk")]       # book 500 vs TTM 400: demand supported
    cats, _ = run(rows([100] * 12), ev, date(2023, 7, 1))
    c = kind(cats, CatalystKind.CAPACITY)[0]
    assert c.chain[0].status == "supported"
    # capacity +100% would imply +400 cr; demand above run-rate is only 100 cr
    assert c.contribution.base_crore == pytest.approx(15.0) and "bounded by demand" in c.contribution.basis


def test_missed_commissioning_is_delayed_and_a_shelved_project_is_contradicted():
    ev = [CAP_PLAN, book(500, date(2023, 6, 20), "bk")]
    cats, _ = run(rows([100] * 12), ev, date(2024, 7, 15))
    assert kind(cats, CatalystKind.CAPACITY)[0].stage == ResearchStage.DELAYED
    shelved = ev + [stmt(Metric.CAPEX, "The expansion project has been put on hold given weak demand.",
                         date(2024, 2, 1), doc="hold")]
    cats, _ = run(rows([100] * 6 + [150] * 6), shelved, date(2024, 3, 1))     # strong growth, but shelved
    assert kind(cats, CatalystKind.CAPACITY)[0].stage == ResearchStage.CONTRADICTED


def test_missing_results_are_data_unavailable_not_a_business_verdict():
    cats, _ = run(rows([100] * 6 + [150] * 6, skip=(date(2023, 9, 30),)), BOOKS, date(2023, 12, 20))
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.stage == ResearchStage.DATA_UNAVAILABLE and o.milestones[0].status == MilestoneStatus.DATA_UNAVAILABLE


def test_catalysts_are_separate_and_older_ones_are_not_overwritten(tmp_path):
    books = BOOKS + [book(1100, date(2024, 8, 20), "b24")]
    rev = [100] * 6 + [130] * 6
    early, _ = run(rows(rev), books, date(2023, 9, 15))
    later, _ = run(rows(rev), books, date(2024, 9, 15))
    e = kind(early, CatalystKind.ORDERS)[0]
    l_old = next(c for c in later if c.catalyst_id == e.catalyst_id)       # same record, same identity
    assert l_old.first_public_at == e.first_public_at and l_old.contribution.base_crore == e.contribution.base_crore
    assert len(kind(later, CatalystKind.ORDERS)) == 2
    led = CatalystLedger(tmp_path)
    assert led.record("T", "2023-09-15", early, RULES_VERSION) >= 1
    assert led.record("T", "2023-09-15", early, RULES_VERSION) == 0           # unchanged: nothing appended
    led.record("T", "2024-09-15", later, RULES_VERSION)
    hist = led.history("T")[e.catalyst_id]
    assert [v["version"] for v in hist] == list(range(1, len(hist) + 1)) and hist[0]["as_of"] == "2023-09-15"
    with pytest.raises(ValueError):
        led.record("T", "2023-01-01", early, RULES_VERSION)


def test_agency_plans_are_context_not_facts():
    r = RatingRationale("rr", "ICRA", at(date(2023, 8, 20)), date(2023, 8, 18), facts=[
        RationaleFact("execution_horizon", None, "", "next 9 months", True, "to be executed over the next 9 months"),
        RationaleFact("capex", 40.0, "crore", "Rs 40.0 crore", True, "plans a capex of Rs 40 crore")])
    cats, _ = run(rows(FLAT + [100] * 6), BOOKS, date(2023, 9, 15), rationales=[r])
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.quantities["horizon_months"] == 9 and "ICRA" in o.window_basis
    assert all("not proof of execution" in x for x in o.expectations if "ICRA" in x and "plan" in x)
    assert not any("capex" in f for f in o.facts)


def test_financing_needs_interest_to_matter():
    r = RatingRationale("up", "CARE", at(date(2023, 8, 1)), date(2023, 8, 1), action="upgraded",
                        long_term_rating="CARE A")
    cats, _ = run(rows([100] * 12), [], date(2023, 9, 1), rationales=[r])
    assert not kind(cats, CatalystKind.FINANCING_COST)        # no finance cost reported: not a material lever


def test_parse_when():
    ref = date(2024, 6, 30)
    assert parse_when("March 2025-end", ref) == date(2025, 3, 31)
    assert parse_when("next 12 months", ref) == date(2025, 6, 30)
    assert parse_when("Q2FY26", ref) == date(2025, 9, 30) and parse_when("FY2026", ref) == date(2026, 3, 31)


def test_timeline_measures_early_detection_and_returns_from_confirmation_only():
    def snap(d, stage):
        return {"ticker": "T", "as_of": d, "evidence_status": "x", "mechanisms": [], "rules_version": RULES_VERSION,
                "catalysts": [{"catalyst_id": "c1", "kind": "executable_orders", "stage": stage,
                               "first_public_at": "2023-08-20T00:00:00+05:30", "operating_change": "book up",
                               "contribution": "estimated", "base_crore": 30.0, "share_of_ttm_ebitda": 0.5,
                               "window": [None, None], "milestones": [], "stage_reasons": []},
                              {"catalyst_id": "c2", "kind": "capacity_or_bottleneck",
                               "stage": "contradicted" if d > "2023-10" else "supported_prospective_inflection",
                               "first_public_at": "2023-06-01T00:00:00+05:30", "operating_change": "cap",
                               "contribution": "estimated", "base_crore": 10.0, "share_of_ttm_ebitda": 0.2,
                               "window": [None, None], "milestones": [], "stage_reasons": []}]}
    snaps = [snap("2023-08-31", "supported_prospective_inflection"), snap("2023-11-30", "execution_validating"),
             snap("2024-02-29", "confirmed_for_investment_review")]
    tl = catalyst_timeline(snaps)
    assert tl["first_defensible_catalyst"] == date(2023, 6, 1)
    assert (tl["supported"], tl["confirmed"], tl["false_positives"]) == (2, 1, 1)
    assert tl["days_to_confirmed"] == [(date(2024, 2, 29) - date(2023, 8, 20)).days]
    entries = review_entries(tl, "T")
    assert entries == [{"ticker": "T", "as_of": "2024-02-29", "lane": "CONFIRMED_FOR_INVESTMENT_REVIEW",
                        "catalyst_id": "c1"}]                       # not the 2023-08 watch-list date
    fp = rules_fingerprint(DEFAULT_CATALYST_THRESHOLDS, RULES_VERSION)
    md = catalyst_report("T", tl, fingerprint=fp)
    assert fp in md and "not a validation sample" in md and "contradicted" in md
    miss = missed_candidates([{"ticker": "U", "should_detect": True}, {"ticker": "V", "should_detect": True}],
                             {"V": {**tl, "supported": 0, "data_unavailable": 0}},
                             {"U": {"missing_periods": 3}, "V": {}})
    assert [(m["ticker"], m["reason"]) for m in miss] == [("U", "data coverage"), ("V", "detector miss")]
