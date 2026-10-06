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


def rows(revenue, margin=0.15, skip=(), pat=None):
    out = []
    for d, v in zip(QE, revenue):
        if d in skip:
            continue
        out += [fm(Metric.REVENUE, d, v), fm(Metric.EBITDA, d, round(v * margin, 2))]
        if pat is not None:
            out += [fm(Metric.PAT, d, pat), fm(Metric.PBT, d, round(pat / 0.75, 2)),
                    fm(Metric.TAX, d, round(pat / 0.75 - pat, 2))]
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


def order(eid, bound, amount, *changes):
    hist = [EventStateChange(EventStage.BINDING_ORDER, at(bound), f"d{eid}", "x", amount)]
    hist += [EventStateChange(st, at(d), f"c{eid}{i}", "y", amt) for i, (st, d, amt) in enumerate(changes)]
    stage = hist[-1].stage if hist[-1].stage == EventStage.CANCELLED and hist[-1].amount is None else \
        EventStage.BINDING_ORDER
    return EconomicEvent(eid, "T", Metric.ORDER_WIN, Quantity(amount, Unit.INR_CRORE, str(amount)), "Utility A",
                         at(bound), evidence_ids=["x"], doc_ids=[f"d{eid}"], current_stage=stage, history=hist,
                         customer_verification=CustomerVerification.ISSUER_NAMED, value_basis=ValueBasis.FIRM)


def book_catalyst(events, as_of=date(2024, 3, 1)):
    cats, _ = run(rows([100] * 6 + [160] * 6), BOOKS, as_of, events=events)
    return next(c for c in kind(cats, CatalystKind.ORDERS) if "order book" in c.operating_change)


def test_a_cancelled_linked_order_contradicts_even_with_strong_growth():
    # bound while the book grew (between the two snapshots): 120 of the 250 cr increase, then cancelled
    o = book_catalyst([order("e1", date(2023, 5, 10), 120, (EventStage.CANCELLED, date(2023, 11, 20), None))])
    assert o.stage == ResearchStage.CONTRADICTED and "cancel" in o.stage_reasons[0]


def test_an_unrelated_cancellation_is_a_company_risk_not_a_contradiction():
    o = book_catalyst([order("e2", date(2023, 9, 25), 300, (EventStage.CANCELLED, date(2023, 11, 20), None))])
    assert o.stage != ResearchStage.CONTRADICTED
    assert any("company-level risk" in u and "e2" in u for u in o.uncertainties)


def test_partial_cancellations_are_weighed_against_the_catalysts_demand():
    small = book_catalyst([order("e3", date(2023, 5, 10), 120, (EventStage.CANCELLED, date(2023, 11, 20), 100))])
    assert small.stage != ResearchStage.CONTRADICTED                    # 20 of 250 cr = 8%: noted
    assert any("partial cancellation" in u for u in small.uncertainties)
    big = book_catalyst([order("e4", date(2023, 5, 10), 120, (EventStage.AMENDED, date(2023, 11, 20), 40))])
    assert big.stage == ResearchStage.CONTRADICTED                       # 80 of 250 cr = 32%: contradicts


CAP_PLAN = stmt(Metric.CAPACITY, "The Board approved capex to increase the production capacity from 100 MW to 200 MW, "
                                 "expected by March 2024.", date(2023, 6, 1), Modality.FORWARD, "plan")


def test_capacity_without_demand_stays_potential_and_magnitude_unresolved():
    cats, _ = run(rows([100] * 12), [CAP_PLAN], date(2023, 7, 1))
    c = kind(cats, CatalystKind.CAPACITY)[0]
    assert c.stage == ResearchStage.POTENTIAL
    assert c.chain[0].status == "unsupported" and "depreciation" in c.chain[0].basis
    assert c.contribution.status == "potentially_material_unresolved"
    assert c.window_start == date(2024, 3, 31)


def test_capacity_needs_horizon_and_a_constraint_before_its_increment_is_supported():
    ev = [CAP_PLAN, book(500, date(2023, 6, 20), "bk")]       # book 500 vs TTM 400: demand present
    cats, _ = run(rows([100] * 12), ev, date(2023, 7, 1))
    k = kind(cats, CatalystKind.CAPACITY)[0].contribution
    assert k.status == "potentially_material_unresolved" and k.base_crore is None
    assert "execution period of the order book" in k.basis and "constrains deliveries" in k.basis
    assert k.illustrative_crore == pytest.approx(60.0) and "never counted" in k.illustrative_basis


def capacity_rationale(day, util, horizon=12, capex=None, funding=None):
    facts = [RationaleFact("execution_horizon", None, "", f"next {horizon} months", True, "to be executed"),
             RationaleFact("utilization", util, "%", f"{util}%", False, f"capacity utilisation stood at {util}%")]
    if capex:
        facts.append(RationaleFact("capex", capex, "crore", f"Rs {capex} crore", True, f"capex of Rs {capex} crore"))
    if funding:
        facts.append(RationaleFact("capex_funding", None, "", funding, True, f"funded through {funding}"))
    return RatingRationale(f"cr{day}", "CARE", at(day), day, facts=facts)


def test_capacity_increment_is_bounded_by_executable_demand_and_bridged_to_parent_earnings():
    r = capacity_rationale(date(2023, 6, 20), 88, capex=60, funding="a term loan")
    ev = [CAP_PLAN, book(500, date(2023, 6, 20), "bk")]
    cats, _ = run(rows([100] * 12), ev, date(2023, 7, 1), rationales=[r])
    k = kind(cats, CatalystKind.CAPACITY)[0].contribution
    # tight capacity: executable 500/yr less what existing capacity delivers (400) = 100, capacity-implied 400
    assert k.base_crore == pytest.approx(15.0) and "bounded by executable demand" in k.basis
    # parent bridge: D&A 60/15 = 4, debt-funded interest 60 x 9% = 5.4, tax fallback 25.17%
    assert k.bridge_status == "computed"
    assert k.pat_base_crore == pytest.approx(round((15 - 4 - 5.4) * (1 - 0.2517), 2))
    assert any("D&A" in a for a in k.bridge_assumptions) and any("term loan" in a for a in k.bridge_assumptions)


def test_confirmation_needs_material_recurring_parent_earnings():
    rev = [100] * 6 + [140] * 6
    ok, _ = run(rows(rev, pat=10.0), BOOKS, date(2024, 3, 1))            # TTM parent PAT 40: 15 x 0.75 = 28%
    o = kind(ok, CatalystKind.ORDERS)[0]
    assert o.stage == ResearchStage.CONFIRMED and o.contribution.earnings_materiality == "established"
    big, _ = run(rows(rev, pat=30.0), BOOKS, date(2024, 3, 1))           # TTM parent PAT 120: 9%
    o = kind(big, CatalystKind.ORDERS)[0]
    assert o.stage == ResearchStage.VALIDATING and o.contribution.earnings_materiality == "not_material"
    assert "not material" in o.stage_reasons[0]
    nobridge, s = run(rows(rev), BOOKS, date(2024, 3, 1))               # no PAT rows: materiality on sign only
    o = kind(nobridge, CatalystKind.ORDERS)[0]
    assert o.contribution.bridge_status == "computed"


def test_unresolved_parent_earnings_is_an_explicit_review_condition():
    from makrograph.earnings_inflection.catalysts import _stage
    from makrograph.earnings_inflection.contracts import Catalyst, CatalystMilestone, ChainLink, EarningsContribution
    c = Catalyst("x", "T", CatalystKind.CAPACITY, at(date(2023, 6, 1)), [], "cap", chain=[ChainLink("demand",
                 "supported")], contribution=EarningsContribution(status="estimated", base_crore=20.0,
                                                                  bridge_status="unresolved",
                                                                  bridge_missing=["capex of the expansion"],
                                                                  earnings_materiality="unresolved"),
                 milestones=[CatalystMilestone("Is the new output reaching revenue?", "", status=MilestoneStatus.MET,
                                               periods_judged=2)])
    _stage(c, date(2024, 6, 1), DEFAULT_CATALYST_THRESHOLDS)
    assert c.stage == ResearchStage.CONFIRMED
    assert c.review_conditions and "open investment-review condition" in c.review_conditions[0]


def test_missed_commissioning_is_delayed_and_a_shelved_project_is_contradicted():
    ev = [CAP_PLAN, book(500, date(2023, 6, 20), "bk")]
    cats, _ = run(rows([100] * 12), ev, date(2024, 7, 15))
    assert kind(cats, CatalystKind.CAPACITY)[0].stage == ResearchStage.DELAYED
    shelved = ev + [stmt(Metric.CAPEX, "The 200 MW expansion project has been put on hold given weak demand.",
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


# --- milestones are linked to the project's identity --------------------------------------------

PLANT_A = stmt(Metric.CAPACITY, "The Board approved expanding the Plant A facility capacity from 100 MW to 200 MW, "
                                "expected by March 2024.", date(2023, 6, 1), Modality.FORWARD, "planA")
DEMAND = book(500, date(2023, 6, 20), "bkA")


def cap_after(*later, as_of=date(2024, 3, 1)):
    cats, _ = run(rows([100] * 12), [PLANT_A, DEMAND, *later], as_of)
    return kind(cats, CatalystKind.CAPACITY)[0]


def test_another_plants_commissioning_does_not_confirm_this_project():
    c = cap_after(stmt(Metric.CAPACITY, "Plant B commenced commercial production during the quarter.",
                       date(2024, 1, 10), doc="b"))
    com = c.milestones[0]
    assert com.status != MilestoneStatus.MET and c.stage != ResearchStage.VALIDATING
    assert any("not attributable" in u for u in c.uncertainties)
    c = cap_after(stmt(Metric.CAPACITY, "Plant A commenced commercial production during the quarter.",
                       date(2024, 1, 10), doc="a"))
    assert c.milestones[0].status == MilestoneStatus.MET


def test_forecast_or_negated_commissioning_is_not_commissioning():
    for q in ("Plant A is yet to be commissioned.", "Plant A is expected to be commissioned in Q1 FY25.",
              "Commercial production at Plant A will be started next quarter."):
        c = cap_after(stmt(Metric.CAPACITY, q, date(2024, 1, 10), doc="x"))
        assert c.milestones[0].status != MilestoneStatus.MET, q


def test_delays_and_abandonment_must_concern_this_project():
    c = cap_after(stmt(Metric.CAPEX, "The Plant B expansion has been deferred.", date(2024, 1, 10), doc="d"),
                  as_of=date(2024, 1, 31))
    assert c.stage not in (ResearchStage.DELAYED, ResearchStage.CONTRADICTED)
    c = cap_after(stmt(Metric.CAPEX, "Any delay could defer the Plant A project and it may be cancelled.",
                       date(2024, 1, 10), doc="r"), as_of=date(2024, 1, 31))
    assert c.stage != ResearchStage.CONTRADICTED                       # a stated risk is not an abandonment
    c = cap_after(stmt(Metric.CAPEX, "Commissioning of Plant A has been delayed to Q2 FY25.", date(2024, 1, 10),
                       doc="dA"), as_of=date(2024, 1, 31))
    assert c.stage == ResearchStage.DELAYED
    c = cap_after(stmt(Metric.CAPEX, "The Plant A project has been shelved.", date(2024, 1, 10), doc="sA"),
                  as_of=date(2024, 1, 31))
    assert c.stage == ResearchStage.CONTRADICTED


def test_an_unidentified_project_is_never_matched_implicitly():
    plan = stmt(Metric.CAPACITY, "We plan to add new capacity, expected by March 2024.", date(2023, 6, 1),
                Modality.FORWARD, "p0")
    from makrograph.earnings_inflection.catalysts import project_identity, _quote_matches_project
    ident = project_identity(plan.quote, {})
    assert ident == {"facilities": [], "capacities": []}
    assert not _quote_matches_project("Plant B commenced commercial production.", ident)


# --- what was knowable when ---------------------------------------------------------------------

def horizon_rationale(day, months):
    return RatingRationale(f"rr{day}", "ICRA", at(day), day, facts=[
        RationaleFact("execution_horizon", None, "", f"next {months} months", True,
                      f"to be executed over the next {months} months")])


def test_a_later_rationale_never_improves_the_original_signal():
    later = horizon_rationale(date(2023, 9, 10), 9)                  # published 21 days AFTER the 20-Aug catalyst
    cats, _ = run(rows(FLAT + [100] * 6), BOOKS, date(2023, 9, 30), rationales=[later])
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.first_public_at.date() == date(2023, 8, 20)
    assert o.initial_assessment["horizon_basis"] == "" and "assumed" in o.initial_assessment["window_basis"]
    assert "ICRA" in o.quantities["horizon_basis"]                  # the current record uses it ...
    assert any(u.startswith("2023-09-10") and "execution horizon now stated" in u for u in o.upgrades)  # ... dated
    cats, _ = run(rows(FLAT + [100] * 6), BOOKS, date(2023, 8, 31), rationales=[later])
    assert kind(cats, CatalystKind.ORDERS)[0].quantities["horizon_basis"] == ""    # not yet public


def test_support_dates_are_separate_from_first_disclosure():
    big = [book(250, date(2022, 8, 10), "b22"), book(600, date(2023, 8, 20), "b23")]   # needs 600/yr vs 400
    stretch = horizon_rationale(date(2023, 10, 5), 15)                               # 480/yr: deliverable
    cats, _ = run(rows(FLAT + [100] * 6), big, date(2023, 10, 31), rationales=[stretch])
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.initial_assessment["stage"] == "potential_catalyst"         # what was knowable on 20 Aug
    assert o.first_public_at.date() == date(2023, 8, 20)                 # first disclosure
    assert o.materiality_supported_at.date() == date(2023, 8, 20)        # material even at 12 months assumed
    assert o.execution_supported_at.date() == date(2023, 10, 5)          # pathway only once the horizon was public
    assert o.supported_at.date() == date(2023, 10, 5) and o.stage == ResearchStage.SUPPORTED
    assert any(u.startswith("2023-10-05") and "potential_catalyst -> supported" in u for u in o.upgrades)



def test_timeline_uses_point_in_time_support_and_confirmation_dates():
    base = {"catalyst_id": "c9", "kind": "executable_orders", "first_public_at": "2023-08-20T00:00:00+05:30",
            "operating_change": "book up", "contribution": "estimated", "base_crore": 30.0, "share_of_ttm_ebitda": 0.5,
            "window": [None, None], "milestones": [], "stage_reasons": [], "initial_stage": "potential_catalyst"}
    snaps = [{"ticker": "T", "as_of": "2023-10-31", "evidence_status": "x", "mechanisms": [],
              "catalysts": [{**base, "stage": "supported_prospective_inflection",
                             "supported_at": "2023-10-05T00:00:00+05:30"}]},
             {"ticker": "T", "as_of": "2024-02-29", "evidence_status": "x", "mechanisms": [],
              "catalysts": [{**base, "stage": "confirmed_for_investment_review", "supported_at":
                             "2023-10-05T00:00:00+05:30", "confirmed_at": "2024-02-14T00:00:00+05:30"}]}]
    tl = catalyst_timeline(snaps)
    r = tl["catalysts"][0]
    assert tl["first_defensible_catalyst"] == date(2023, 10, 5)            # support date, not first mention
    assert r["days_to_supported"] == 46 and r["days_to_confirmed"] == (date(2024, 2, 14) - date(2023, 8, 20)).days
    assert review_entries(tl, "T")[0]["as_of"] == "2024-02-14"


# --- continuous monitoring after the original window --------------------------------------------

def orders_at(rev, as_of):
    cats, _ = run(rows(rev), BOOKS, as_of)
    return kind(cats, CatalystKind.ORDERS)[0]


def test_missed_original_timetable_then_recovered_is_labelled_as_such():
    rev = [100] * 6 + [102, 103, 130, 130, 130, 130]       # Sep/Dec-23 muted; Mar/Jun-24 +30%
    o = orders_at(rev, date(2024, 3, 1))
    conv = o.milestones[0]
    assert conv.status == MilestoneStatus.MISSED and conv.timetable == "missed" and o.stage == ResearchStage.DELAYED
    o = orders_at(rev, date(2024, 6, 1))                    # Dec + Mar window: +16.5%
    conv = o.milestones[0]
    assert conv.original_status == MilestoneStatus.MISSED      # the original verdict is kept
    assert conv.timetable == "recovered_late" and conv.status == MilestoneStatus.MET
    assert o.stage == ResearchStage.CONFIRMED and o.stage_reasons[0].startswith("missed the original timetable")
    assert o.confirmed_at.date() == date(2024, 5, 15)        # Mar-24 results, not the original schedule


def test_confirmed_on_schedule_then_deterioration_is_tracked():
    rev = [100] * 6 + [140, 140, 90, 90, 90, 90]
    o = orders_at(rev, date(2024, 3, 1))
    assert o.stage == ResearchStage.CONFIRMED and o.stage_reasons[0] == "confirmed on schedule"
    o = orders_at(rev, date(2024, 9, 1))                    # Mar/Jun-24 -10%
    conv = o.milestones[0]
    assert conv.original_status == MilestoneStatus.MET and conv.timetable == "deteriorated"
    assert o.stage == ResearchStage.CONTRADICTED
    assert o.confirmed_at.date() == date(2024, 2, 14)        # the earlier confirmation stays on record


def test_a_stated_delay_sets_an_explicit_revised_deadline():
    late = stmt(Metric.CAPEX, "Commissioning of Plant A has been delayed to June 2024.", date(2024, 2, 1), doc="dl")
    c = cap_after(late, as_of=date(2024, 4, 15))
    com = c.milestones[0]
    assert com.status == MilestoneStatus.MISSED and com.revised_due_by == date(2024, 6, 30) + timedelta(days=90)
    assert com.due_by == date(2024, 3, 31) + timedelta(days=90)        # the original deadline is not moved
    done = stmt(Metric.CAPACITY, "Plant A commenced commercial production in July 2024.", date(2024, 7, 20), doc="ok")
    c = cap_after(late, done, as_of=date(2024, 8, 1))
    com = c.milestones[0]
    assert com.status == MilestoneStatus.MET and com.timetable == "recovered_late"
    assert com.original_status == MilestoneStatus.MISSED
