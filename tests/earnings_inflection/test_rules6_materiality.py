"""D13 (catalyst-rules-6): an immaterial change is a research note, never an alert, and an alert is
credited with an earnings inflection only on substance, not on timing alone."""

from datetime import date

from makrograph.earnings_inflection.catalysts import DEFAULT_CATALYST_THRESHOLDS, _stage
from makrograph.earnings_inflection.contracts import CustomerVerification, RelationshipStatus, ResearchStage
from makrograph.earnings_inflection.evaluation import catalyst_timeline, inflection_credit

from .test_catalysts import rows, run
from .test_point_in_time_catalysts import inflow, order


def validating_inflow():
    e = order("e1", date(2023, 5, 10), 120, verification=CustomerVerification.ISSUER_NAMED)
    e.relationship, e.duration_months = RelationshipStatus.UNKNOWN, 12
    cats, _ = run(rows([100] * 6 + [140] * 6, pat=10.0), [], date(2024, 3, 1), events=[e])
    c = inflow(cats)[0]
    assert c.stage == ResearchStage.VALIDATING
    return c


def test_an_immaterial_change_is_never_an_alert():
    c = validating_inflow()
    c.contribution.status, c.contribution.share_of_ttm_ebitda = "immaterial", 0.003   # 1.4 cr vs ~500 cr EBITDA
    _stage(c, date(2024, 3, 1), DEFAULT_CATALYST_THRESHOLDS)
    assert c.stage == ResearchStage.POTENTIAL
    assert c.stage_reasons[0].startswith("immaterial: 0.3% of TTM EBITDA")
    assert c.confidence == "low"


def test_material_or_unsized_changes_are_unaffected():
    c = validating_inflow()                                    # unresolved magnitude: still validating
    _stage(c, date(2024, 3, 1), DEFAULT_CATALYST_THRESHOLDS)
    assert c.stage == ResearchStage.VALIDATING


def test_contradictions_of_immaterial_changes_are_still_reported():
    c = validating_inflow()
    c.contribution.status = "immaterial"
    c.milestones[0].status = c.milestones[0].status.__class__("contradicted")
    _stage(c, date(2024, 3, 1), DEFAULT_CATALYST_THRESHOLDS)
    assert c.stage == ResearchStage.CONTRADICTED


W = {"from": "2023-03-31", "end_published": "2024-05-15"}


def alert(cid, supported, verdict, contribution="potentially_material_unresolved"):
    return {"catalyst_id": cid, "supported_at": supported, "verdict": verdict,
            "contribution_when_flagged": contribution}


def test_timing_alone_does_not_credit_an_inflection():
    got = inflection_credit(W, [alert("seg", "2023-08-10", "confirmed", "immaterial"),
                                alert("late", "2023-09-01", "delayed")])
    assert got["missed"] and got["timing_only"] == ["seg", "late"]


def test_a_substantive_alert_in_the_window_is_credited():
    got = inflection_credit(W, [alert("ord", "2023-08-10", "executed_unconfirmable"),
                                alert("after", "2024-06-01", "confirmed")])
    assert got["alert_before"] == ["ord"] and not got["missed"]


def test_timeline_records_materiality_when_first_flagged():
    snaps = [{"as_of": d, "catalysts": [{
        "catalyst_id": "x", "kind": "segment_turnaround", "stage": st, "first_public_at": "2023-05-10",
        "operating_change": "turnaround", "contribution": contrib, "base_crore": 1.4}]}
        for d, st, contrib in (("2023-05-31", "potential_catalyst", "not_quantified"),
                               ("2023-08-31", "execution_validating", "immaterial"),
                               ("2023-11-30", "execution_validating", "estimated"))]
    r = catalyst_timeline(snaps)["catalysts"][0]
    assert r["contribution_when_flagged"] == "immaterial"
