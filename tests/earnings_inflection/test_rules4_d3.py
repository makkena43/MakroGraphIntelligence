"""D3 (catalyst-rules-4): an executed catalyst that cannot be confirmed is not "delayed".

An order inflow without a disclosed backlog can never be sized, so it can never reach
confirmation; under rules-3 it expired to "delayed" even when revenue converted as tested. The
execution window now makes a catalyst "delayed" only when its execution test was not met."""

from datetime import date

from makrograph.earnings_inflection.contracts import CustomerVerification, RelationshipStatus, ResearchStage
from makrograph.earnings_inflection.evaluation import catalyst_timeline

from .test_catalysts import rows, run
from .test_point_in_time_catalysts import inflow, order

GROWTH = [100] * 6 + [140] * 6          # revenue +40% from the Sep-23 quarter
FLAT = [100] * 12


def inflow_at(rev, as_of):
    e = order("e1", date(2023, 5, 10), 120, verification=CustomerVerification.ISSUER_NAMED)
    e.relationship, e.duration_months = RelationshipStatus.UNKNOWN, 12
    cats, _ = run(rows(rev, pat=10.0), [], as_of, events=[e])
    return inflow(cats)[0]


def test_executed_unsizable_inflow_stays_validating_with_its_blocker():
    c = inflow_at(GROWTH, date(2024, 12, 1))                  # well after window end + grace
    assert c.stage == ResearchStage.VALIDATING                 # rules-3: DELAYED
    assert "order inflow without a disclosed backlog" in c.confirmation_blocked
    assert c.stage_reasons[0].startswith("execution verified; cannot be confirmed")
    assert c.contribution.status != "estimated"                # the confirmation test was not relaxed


def test_a_real_execution_miss_still_becomes_delayed():
    c = inflow_at(FLAT, date(2024, 12, 1))
    assert c.stage == ResearchStage.DELAYED and not c.confirmation_blocked


def test_before_the_window_ends_nothing_changes():
    c = inflow_at(GROWTH, date(2024, 3, 1))
    assert c.stage == ResearchStage.VALIDATING and not c.confirmation_blocked


def test_evaluation_counts_executed_unconfirmable_separately():
    snaps = [{"as_of": "2024-12-01", "catalysts": [{
        "catalyst_id": "x", "kind": "executable_orders", "stage": "execution_validating",
        "first_public_at": "2023-05-10T00:00:00+05:30", "operating_change": "inflow", "contribution": "x",
        "base_crore": None, "supported_at": "2023-08-14T00:00:00+05:30",
        "confirmation_blocked": "magnitude cannot be sized"}]}]
    tl = catalyst_timeline(snaps)
    assert tl["catalysts"][0]["verdict"] == "executed_unconfirmable"
    assert tl["executed_unconfirmable"] == 1 and tl["delayed"] == 0 and tl["false_positives"] == 0
