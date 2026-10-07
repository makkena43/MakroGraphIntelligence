"""Historical correctness (review round 3, item 3): what the detector says it knew at a date must be
rebuilt from what was public at that date.  Each event is taken in the state it stood then,
catalysts are generated from evidence available then, later failures are kept, and appending future
disclosures never changes an earlier assessment (prefix invariance)."""

import copy
from datetime import date, datetime, timedelta

from makrograph.earnings_inflection.catalysts import detect_catalysts
from makrograph.earnings_inflection.contracts import (
    IST, CatalystKind, CustomerVerification, EconomicEvent, EventStage, EventStateChange, Mechanism, MechanismResult,
    MechanismState, Metric, Quantity, ResearchStage, Unit, ValueBasis,
)
from makrograph.earnings_inflection.demand import event_state_at
from makrograph.earnings_inflection.financial_series import FinancialSeries

from .test_catalysts import BOOKS, at, kind, rows, stmt  # shared synthetic issuer

REV = [100] * 12                      # flat: 400 cr TTM revenue, threshold 0.25x = 100 cr of inflow


def order(eid, bound, amount, *changes, verification=CustomerVerification.ISSUER_NAMED, counterparty="Utility A"):
    hist = [EventStateChange(EventStage.BINDING_ORDER, at(bound), f"d{eid}", "x", amount)]
    hist += [EventStateChange(st, at(d), f"c{eid}{i}", "y", amt) for i, (st, d, amt) in enumerate(changes)]
    last = hist[-1]
    stage = EventStage.CANCELLED if last.stage == EventStage.CANCELLED and last.amount is None else \
        EventStage.BINDING_ORDER
    cur = amount
    for h in hist[1:]:
        cur = 0 if (h.stage == EventStage.CANCELLED and h.amount is None) else (h.amount if h.amount is not None
                                                                               else cur)
    return EconomicEvent(eid, "T", Metric.ORDER_WIN, Quantity(cur or amount, Unit.INR_CRORE, str(amount)),
                         counterparty, at(bound), evidence_ids=[f"x{eid}"], doc_ids=[f"d{eid}"], current_stage=stage,
                         history=hist, customer_verification=verification, value_basis=ValueBasis.FIRM,
                         cancelled_amount=0.0 if cur else amount)


def detect(as_of, events, evidence=(), mechanisms=(), **kw):
    cutoff = at(as_of) + timedelta(days=1)
    vis = [r for r in rows(REV) if r.available_at < cutoff]
    s = FinancialSeries.build("T", vis)
    ev = [e for e in evidence if e.available_at < cutoff]
    return detect_catalysts("T", s, ev, list(events), list(mechanisms), [], as_of, measurements=vis, **kw)


def inflow(cats):
    return [c for c in kind(cats, CatalystKind.ORDERS) if "binding" in c.operating_change and "TTM revenue" in c.operating_change]


def stage_on(c, d):
    """The stage the record says the catalyst had on date ``d``."""
    cut = at(d) + timedelta(days=1)
    return [st for t, st in c.stage_history if datetime.fromisoformat(t) < cut][-1]


# --- event state at a timestamp ----------------------------------------------------------------------

def test_event_state_is_rebuilt_from_its_dated_history():
    e = order("e1", date(2023, 5, 10), 120, (EventStage.AMENDED, date(2023, 9, 1), 80),
              (EventStage.CANCELLED, date(2023, 11, 20), None))
    assert event_state_at(e, at(date(2023, 5, 9))) is None                      # not public yet
    s = event_state_at(e, at(date(2023, 6, 1)))
    assert s.current_stage == EventStage.BINDING_ORDER and s.amount.value == 120 and s.cancelled_amount == 0
    s = event_state_at(e, at(date(2023, 10, 1)))
    assert s.amount.value == 80 and s.current_stage == EventStage.BINDING_ORDER
    s = event_state_at(e, at(date(2023, 12, 1)))
    assert s.current_stage == EventStage.CANCELLED and s.amount.value == 0 and s.cancelled_amount == 80
    assert e.current_stage == EventStage.CANCELLED and len(e.history) == 3   # the stored event is not changed


# --- catalysts generated from what was public then; failures retained ------------------------------------

def test_an_order_verified_then_cancelled_still_yields_its_catalyst_as_a_failure():
    live = order("e1", date(2023, 5, 10), 120)
    early = inflow(detect(date(2023, 6, 30), [live]))
    assert len(early) == 1 and early[0].first_public_at.date() == date(2023, 5, 10)
    dead = order("e1", date(2023, 5, 10), 120, (EventStage.CANCELLED, date(2023, 11, 20), None))
    late = inflow(detect(date(2024, 3, 1), [dead]))
    assert len(late) == 1, "a later cancellation must not erase the earlier candidate"
    c = late[0]
    assert c.catalyst_id == early[0].catalyst_id
    assert c.stage == ResearchStage.CONTRADICTED and "cancel" in c.stage_reasons[0]
    assert c.quantities["inflow"] == 120                                       # sized as known then


def test_a_later_amendment_does_not_resize_the_original_signal():
    early = inflow(detect(date(2023, 6, 30), [order("e1", date(2023, 5, 10), 120)]))[0]
    cut = order("e1", date(2023, 5, 10), 120, (EventStage.AMENDED, date(2023, 10, 5), 30))
    c = inflow(detect(date(2024, 3, 1), [cut]))[0]
    assert c.initial_assessment == early.initial_assessment
    assert c.quantities["inflow"] == 120 and c.stage == ResearchStage.CONTRADICTED   # 90 of 120 cr removed


def test_a_later_verification_dates_the_catalyst_when_it_became_verifiable():
    anon = order("e1", date(2023, 5, 10), 120, verification=CustomerVerification.ANONYMOUS, counterparty=None)
    named = copy.deepcopy(anon)
    named.counterparty, named.customer_verification = "Utility A", CustomerVerification.CORROBORATED
    t_named = at(date(2023, 8, 1))

    def events_at(t):
        return [x for x in (event_state_at(named if t >= t_named else anon, t),) if x is not None]
    naming = stmt(Metric.ORDER_WIN, "The order received in May 2023 is from Utility A.", date(2023, 8, 1))
    c = inflow(detect(date(2024, 3, 1), [named], [naming], events_at=events_at))
    assert len(c) == 1 and c[0].first_public_at == t_named         # not back-dated to the 10 May award
    assert inflow(detect(date(2023, 7, 31), [anon], events_at=events_at)) == []


def test_a_mechanism_is_dated_when_it_became_detectable_not_by_the_period_it_describes():
    early_signal = at(date(2023, 5, 15))
    m = MechanismResult(Mechanism.UTILIZATION, MechanismState.EMERGING, "positive", 12.0, "pp",
                        "utilisation 60% -> 72%", date(2023, 3, 31), early_signal)
    t_detect = datetime.combine(date(2023, 11, 14), datetime.min.time(), tzinfo=IST)   # Sep-23 results

    def mechanisms_at(t):
        return [m] if t >= t_detect else []
    cats = detect(date(2024, 3, 1), [], mechanisms=[m], mechanisms_at=mechanisms_at)
    u = kind(cats, CatalystKind.UTILIZATION)
    assert len(u) == 1 and u[0].first_public_at == t_detect


# --- prefix invariance ---------------------------------------------------------------------------------

def _prefix_invariant(early_cats, late_cats, d):
    by_id = {c.catalyst_id: c for c in late_cats}
    for c in early_cats:
        assert c.catalyst_id in by_id, f"{c.catalyst_id} known on {d} disappeared later"
        later = by_id[c.catalyst_id]
        assert later.first_public_at == c.first_public_at
        assert later.initial_assessment == c.initial_assessment
        assert stage_on(later, d) == c.stage.value
        for f in ("materiality_supported_at", "execution_supported_at", "supported_at", "validating_at",
                  "confirmed_at"):
            if getattr(c, f) is not None:
                assert getattr(later, f) == getattr(c, f), f


def test_appending_future_disclosures_never_changes_an_earlier_assessment():
    d, d2 = date(2023, 9, 30), date(2024, 9, 1)
    now = [order("e1", date(2023, 5, 10), 120)]
    full = [order("e1", date(2023, 5, 10), 120, (EventStage.CANCELLED, date(2023, 11, 20), None)),
            order("e2", date(2024, 2, 10), 150, (EventStage.AMENDED, date(2024, 6, 1), 60))]
    early = detect(d, now, BOOKS)
    assert early, "fixture must produce catalysts before the cutoff"
    late = detect(d2, full, BOOKS)
    _prefix_invariant(early, late, d)
    # and at every intermediate month the record of earlier dates is unchanged
    prev = early
    for m in (date(2023, 12, 31), date(2024, 3, 31), date(2024, 6, 30)):
        cur = detect(m, full, BOOKS)
        _prefix_invariant(prev, cur, d if prev is early else last)
        prev, last = cur, m


def test_pipeline_assessment_is_prefix_invariant(tmp_path):
    from .test_discovery_catalysts import build

    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    pipe = EarningsInflectionPipeline({}, FixtureRepository(build(tmp_path)))
    d = date(2024, 8, 15)
    early = pipe.run(["QUIETCO"], d.isoformat()).assessments[0].catalysts
    late = pipe.run(["QUIETCO"], "2024-12-31").assessments[0].catalysts
    assert early
    _prefix_invariant(early, late, d)


def test_thesis_leading_order_signal_survives_a_later_cancellation():
    from makrograph.earnings_inflection.thesis import DEFAULT_THESIS_THRESHOLDS, _leading_orders
    s = FinancialSeries.build("T", rows(REV))
    live = _leading_orders(s, [], [order("e1", date(2023, 5, 10), 120)], "Q", date(2023, 6, 30),
                           DEFAULT_THESIS_THRESHOLDS)
    dead = _leading_orders(s, [], [order("e1", date(2023, 5, 10), 120, (EventStage.CANCELLED, date(2023, 11, 20),
                                                                         None))], "Q", date(2024, 3, 1),
                           DEFAULT_THESIS_THRESHOLDS)
    assert live and dead and live[0][0] == dead[0][0] and live[0][1] == dead[0][1]
    assert live[0][1].startswith("issuer-disclosed binding orders")


def test_a_later_refiling_does_not_rewrite_figures_known_earlier(tmp_path):
    """A revised results filing replaces the earlier figures only from its own publication date:
    the catalyst assessed before it keeps the figures as originally filed."""
    import json

    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.source_repository import FixtureRepository

    from .test_discovery_catalysts import build, results_text
    fx = build(tmp_path)
    data = json.loads((fx / "quiet.json").read_text())
    q = date(2024, 3, 31)                       # as filed: 130; revised on 25 Jul 2024 (after detection): 50
    orig = next(d for d in data["documents"] if d["doc_id"] == f"QR-{q}")
    orig["text"] = orig["text"].replace("Revenue from operations          100.00",
                                        "Revenue from operations          130.00")
    data["documents"].append({
        "doc_id": "QR-revised", "ticker": "QUIETCO", "source_name": "nse", "doc_type": "announcement",
        "filing_type": "Financial Results", "title": f"Revised financial results for quarter ended {q}",
        "company": "Quiet Co Limited", "published_at": "2024-07-25T18:00:00+05:30",
        "text": results_text(q).replace("Revenue from operations          100.00",
                                        "Revenue from operations          50.00")})
    (fx / "quiet.json").write_text(json.dumps(data))
    pipe = EarningsInflectionPipeline({}, FixtureRepository(fx))
    before = pipe.run(["QUIETCO"], "2024-07-20").assessments[0].catalysts
    after = pipe.run(["QUIETCO"], "2024-08-05").assessments[0].catalysts     # revision public, Jun-24 not yet
    assert pipe.last_series.get(Metric.REVENUE, q, "Q").value == 50.0        # the revision is the current figure
    assert before
    _prefix_invariant(before, after, date(2024, 7, 20))
    c = next(x for x in after if x.catalyst_id == before[0].catalyst_id)
    assert c.quantities["ttm_revenue_at_detection"] == before[0].quantities["ttm_revenue_at_detection"] == 430.0


def test_a_figure_later_superseded_keeps_the_catalyst_date_it_supported():
    """D9 (rules-5): a quarter first published in an investor presentation (highlight figure) and
    later restated by the statement must not drop out of the scan times; otherwise the catalyst it
    supported is re-dated to the statement and vanishes from the earlier record (Borosil, 2025)."""
    from dataclasses import replace
    from makrograph.earnings_inflection.contracts import Metric, Unit, Scope, FinancialMeasurement
    q = date(2023, 6, 30)
    t_pres, t_stmt = at(date(2023, 7, 20)), at(date(2023, 8, 25))
    base = [r for r in rows(REV) if r.period_end != q]
    pres = [FinancialMeasurement("T", Metric.REVENUE, q, "Q", 101.0, Unit.INR_CRORE, Scope.STANDALONE, "pres",
                                 t_pres, display_unit=0.01, source="reported_highlight")]
    stmt = [FinancialMeasurement("T", m, q, "Q", v, Unit.INR_CRORE, Scope.STANDALONE, "stmt", t_stmt, display_unit=0.01)
            for m, v in ((Metric.REVENUE, 100.0), (Metric.EBITDA, 15.0))]
    m = MechanismResult(Mechanism.UTILIZATION, MechanismState.EMERGING, "positive", 12.0, "pp",
                        "utilisation 60% -> 72%", q, t_pres)

    def mechanisms_at(t):                       # detectable once the Jun-23 quarter is public at all
        return [m] if t >= t_pres else []

    def run_to(as_of, ms):
        cut = at(as_of) + timedelta(days=1)
        vis = [r for r in ms if r.available_at < cut]
        return detect_catalysts("T", FinancialSeries.build("T", vis), [], [], [m], [], as_of, measurements=vis,
                                mechanisms_at=mechanisms_at)
    early = kind(run_to(date(2023, 8, 1), base + pres), CatalystKind.UTILIZATION)
    late = kind(run_to(date(2024, 3, 1), base + pres + stmt), CatalystKind.UTILIZATION)
    assert len(early) == 1 and early[0].first_public_at == t_pres
    assert [c.catalyst_id for c in late] == [early[0].catalyst_id]       # not re-dated to the statement
    assert late[0].first_public_at == t_pres
