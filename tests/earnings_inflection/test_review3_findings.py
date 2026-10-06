"""Review round 3 findings (items 1, 2, 4, 5), each with an adversarial regression case."""

from datetime import date

from makrograph.earnings_inflection.contracts import CatalystKind, MilestoneStatus, ResearchStage

from .test_catalysts import BOOKS, kind, rows, run


# --- 1. the current status comes from the latest complete window --------------------------------------

def orders_at(rev, as_of):
    cats, _ = run(rows(rev), BOOKS, as_of)
    return kind(cats, CatalystKind.ORDERS)[0]


def test_a_recovered_catalyst_that_deteriorates_again_is_no_longer_positive():
    rev = [100] * 6 + [102, 103, 130, 130, 90, 90]       # miss, recover (Dec+Mar), then -10% (Jun+Sep-24)
    o = orders_at(rev, date(2024, 6, 1))
    assert o.milestones[0].timetable == "recovered_late" and o.stage == ResearchStage.CONFIRMED
    o = orders_at(rev, date(2024, 12, 1))
    conv = o.milestones[0]
    assert conv.original_status == MilestoneStatus.MISSED
    assert conv.timetable == "deteriorated" and conv.status != MilestoneStatus.MET
    assert "recovered" in conv.observed and "since then" in conv.observed
    assert o.stage in (ResearchStage.CONTRADICTED, ResearchStage.DELAYED)
    assert o.confirmed_at is not None                      # the earlier late confirmation stays on record



# --- 2. missing, zero and negative earnings are different; assumptions never establish materiality --------

from makrograph.earnings_inflection.contracts import FinancialMeasurement, Metric, Scope, Unit  # noqa: E402

from .test_catalysts import QE, pub  # noqa: E402

REV = [100] * 6 + [140] * 6                                # confirms on schedule (book +100%)


def results(scope=Scope.STANDALONE, pat=None, parent=None, tax=True):
    out = []
    for d, v in zip(QE, REV):
        f = lambda m, x: FinancialMeasurement("T", m, d, "Q", x, Unit.INR_CRORE, scope, f"r{d:%y%m}",  # noqa: E731
                                              pub(d), display_unit=0.01)
        out += [f(Metric.REVENUE, v), f(Metric.EBITDA, round(v * 0.15, 2))]
        if pat is not None:
            out.append(f(Metric.PAT, pat))
            if tax and pat > 0:
                out += [f(Metric.PBT, round(pat / 0.75, 2)), f(Metric.TAX, round(pat / 0.75 - pat, 2))]
        if parent is not None:
            out.append(f(Metric.PAT_ATTRIBUTABLE, parent))
    return out


def bridge(rs):
    cats, _ = run(rs, BOOKS, date(2024, 3, 1))
    return kind(cats, CatalystKind.ORDERS)[0]


def test_consolidated_without_parent_profit_is_not_assumed_wholly_owned():
    o = bridge(results(Scope.CONSOLIDATED, pat=10.0))      # total PAT reported, owners' share not reported
    k = o.contribution
    assert k.bridge_status == "unresolved" and k.earnings_materiality == "unresolved"
    assert any("parent share not assumed to be 100%" in m for m in k.bridge_missing)
    assert o.review_conditions                             # an open condition, not "established"


def test_zero_parent_profit_is_not_a_base_and_not_a_fallback_to_total_profit():
    o = bridge(results(Scope.CONSOLIDATED, pat=10.0, parent=0.0))
    k = o.contribution
    assert k.earnings_materiality == "unresolved"          # previously `0 or total PAT` -> 40 cr base
    assert any("zero" in m for m in k.bridge_missing)


def test_missing_earnings_is_not_a_loss():
    k = bridge(results()).contribution                     # no PAT rows at all
    assert k.earnings_materiality == "unresolved" and any("not reported" in m for m in k.bridge_missing)


def test_a_loss_maker_is_measured_against_the_loss_not_by_sign():
    small = bridge(results(pat=-30.0)).contribution        # TTM loss 120 cr; increment 15 x (1 - 25%) ~ 11 cr
    assert small.earnings_materiality == "not_material"    # previously "established" on sign alone
    large = bridge(results(pat=-5.0)).contribution         # TTM loss 20 cr
    assert large.earnings_materiality == "unresolved"      # material only at the assumed tax rate


def test_an_assumed_tax_rate_cannot_establish_materiality():
    assumed = bridge(results(pat=10.0, tax=False))
    assert assumed.contribution.earnings_materiality == "unresolved"
    assert "assumed tax rate" in assumed.contribution.bridge_missing[0]
    stated = bridge(results(pat=10.0))
    assert stated.contribution.earnings_materiality == "established"
