"""D4 (catalyst-rules-5): a data gap never erases an adverse verdict."""

from datetime import date

from makrograph.earnings_inflection.contracts import CatalystKind, ResearchStage

from .test_catalysts import BOOKS, QE, kind, rows, run

REV = [100] * 6 + [140, 140, 90, 90, 90, 90]          # confirmed, then revenue falls 10%: contradicted


def orders(as_of, skip=()):
    cats, _ = run(rows(REV, skip=skip), BOOKS, as_of)
    return kind(cats, CatalystKind.ORDERS)[0]


def test_contradiction_stands_when_later_results_go_missing():
    o = orders(date(2025, 4, 1), skip=(QE[10], QE[11]))  # latest complete window still judges it
    assert o.stage == ResearchStage.CONTRADICTED


def _cat(stage, reason):
    from datetime import datetime
    from makrograph.earnings_inflection.contracts import IST, Catalyst
    c = Catalyst("x", "T", CatalystKind.ORDERS, datetime(2023, 1, 1, tzinfo=IST), [], "book")
    c.stage, c.stage_reasons = stage, [reason]
    return c


def test_a_data_gap_after_an_adverse_verdict_keeps_the_verdict():
    from datetime import datetime
    from makrograph.earnings_inflection.catalysts import _keep_adverse
    from makrograph.earnings_inflection.contracts import IST
    bad = _cat(ResearchStage.CONTRADICTED, "contradicted: price change not visible in margins")
    hist = [(datetime(2023, 1, 1, tzinfo=IST), _cat(ResearchStage.POTENTIAL, "p")),
            (datetime(2023, 11, 14, tzinfo=IST), bad)]
    later = _keep_adverse(_cat(ResearchStage.DATA_UNAVAILABLE, "margin comparison not available"), bad, hist)
    assert later.stage == ResearchStage.CONTRADICTED            # rules-4: DATA_UNAVAILABLE (Borosil)
    assert later.stage_reasons[0] == "contradicted as last judged (2023-11-14); later results not available"
    for stage in (ResearchStage.VALIDATING, ResearchStage.SUPPORTED):      # positive stages are not carried
        ok = _cat(stage, "s")
        assert _keep_adverse(_cat(ResearchStage.DATA_UNAVAILABLE, "gap"), ok, hist).stage == \
            ResearchStage.DATA_UNAVAILABLE
    new = _keep_adverse(_cat(ResearchStage.VALIDATING, "new data"), bad, hist)
    assert new.stage == ResearchStage.VALIDATING                 # new data judges it again


def test_a_gap_before_any_verdict_is_still_data_unavailable():
    cats, _ = run(rows([100] * 6, skip=()), BOOKS, date(2024, 3, 1))     # results after Jun-23 never parsed
    o = kind(cats, CatalystKind.ORDERS)[0]
    assert o.stage == ResearchStage.DATA_UNAVAILABLE
