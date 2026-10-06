"""WP7 - earnings bridge repair and optional valuation context."""

from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.contracts import (
    IST, Evidence, EvidenceTier, FinancialMeasurement, GuidanceRecord, GuidanceRevision, GuidanceOutcome,
    IssuerModel, Metric, Modality, Quantity, RevisionDirection, ScenarioStatus, Scope, Unit,
)
from makrograph.earnings_inflection.earnings_bridge import build_bridge
from makrograph.earnings_inflection.financial_series import FinancialSeries
from makrograph.earnings_inflection.valuation import (
    CorporateAction, InMemoryMarketData, Security, valuation_context,
)

QE = [date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31),
      date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31)]
AS_OF = date(2025, 6, 15)


def fm(metric, end, value, ptype="Q"):
    return FinancialMeasurement("T", metric, end, ptype, value, Unit.INR_CRORE if metric not in (
        Metric.DILUTED_EPS, Metric.FACE_VALUE) else Unit.INR_PER_SHARE, Scope.CONSOLIDATED, f"r{end:%y%m}",
        datetime(end.year, end.month, 28, tzinfo=IST), display_unit=0.01)


def rows(shares=10.0, cap_shares=10.0):
    out = []
    for i, d in enumerate(QE):
        rev = 100 + 5 * i
        ebitda, da, fin = rev * 0.15, 3.0, 2.0
        pbt = ebitda - da - fin
        tax = pbt * 0.25
        pat = pbt - tax
        out += [fm(Metric.REVENUE, d, rev), fm(Metric.EBITDA, d, ebitda), fm(Metric.DEPRECIATION, d, da),
                fm(Metric.FINANCE_COST, d, fin), fm(Metric.PBT, d, pbt), fm(Metric.TAX, d, tax), fm(Metric.PAT, d, pat),
                fm(Metric.PAT_ATTRIBUTABLE, d, pat), fm(Metric.DILUTED_EPS, d, round(pat / shares, 4)),
                fm(Metric.PAID_UP_CAPITAL, d, cap_shares * 10), fm(Metric.FACE_VALUE, d, 10.0)]
    out += [fm(Metric.REVENUE, date(2024, 3, 31), sum(100 + 5 * i for i in range(4)), "FY"),
            fm(Metric.REVENUE, date(2025, 3, 31), sum(100 + 5 * i for i in range(4, 8)), "FY")]
    return out


def guidance(metric, label, value, unit=Unit.PERCENT):
    rev = GuidanceRevision(datetime(2025, 5, 20, tzinfo=IST), "g", f"g{label}", Quantity(value, unit, f"{value}%"),
                           RevisionDirection.ORIGINAL, f"we expect {value}% in {label}")
    return GuidanceRecord(f"g-{metric.value}{label}", "T", metric, label, rev, outcome=GuidanceOutcome.PENDING,
                          latest_outcome=GuidanceOutcome.PENDING)


def capex(quote):
    return Evidence(evidence_id="c1", doc_id="c", ticker="T", metric=Metric.CAPEX,
                    tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=Modality.FORWARD, quote=quote,
                    available_at=datetime(2025, 5, 20, tzinfo=IST), quantity=Quantity(150.0, Unit.INR_CRORE, "150"))


def bridge(rs=None, g=(), ev=()):
    return build_bridge(FinancialSeries.build("T", rs or rows()), IssuerModel.OPERATING, list(g), AS_OF, list(ev))


def test_management_case_uses_its_own_fiscal_base():
    b = bridge(g=[guidance(Metric.REVENUE_GROWTH_GUIDANCE, "FY26", 20.0)])
    m = next(s for s in b.scenarios if s.name == "management_stated_case")
    fy25 = sum(100 + 5 * i for i in range(4, 8))
    assert m.revenue_crore == pytest.approx(fy25 * 1.2)            # FY25 base, not a rolling TTM
    assert "FY25 base" in b.management_case_note


def test_mismatched_fiscal_guidance_cannot_produce_a_management_case():
    b = bridge(g=[guidance(Metric.REVENUE_GROWTH_GUIDANCE, "FY26", 20.0),
                  guidance(Metric.MARGIN_GUIDANCE, "FY27", 18.0)])
    assert not any(s.name == "management_stated_case" for s in b.scenarios)
    assert "fiscal targets differ" in b.management_case_note
    b = bridge(g=[guidance(Metric.REVENUE_GROWTH_GUIDANCE, "FY27", 20.0)])      # FY26 base not reported
    assert not any(s.name == "management_stated_case" for s in b.scenarios)
    assert "not reported yet" in b.management_case_note


def test_capex_depreciation_and_funding_are_explicit():
    b = bridge(ev=[capex("We plan capex of Rs 150 crore funded through internal accruals.")])
    reg = {r["input"]: r for r in b.assumption_register}
    assert reg["incremental D&A from capex (cr/yr)"]["value"] == 10.0
    assert reg["incremental D&A from capex (cr/yr)"]["source"] == "analyst assumption"
    assert reg["incremental interest from capex (cr/yr)"]["value"] == 0.0
    b = bridge(ev=[capex("We plan capex of Rs 150 crore to be funded by a term loan.")])
    assert {r["input"]: r for r in b.assumption_register}["incremental interest from capex (cr/yr)"]["value"] == 13.5
    b = bridge(ev=[capex("We plan capex of Rs 150 crore for the new plant.")])
    r = {r["input"]: r for r in b.assumption_register}["incremental interest from capex (cr/yr)"]
    assert r["source"].startswith("analyst assumption") and r["value"] == pytest.approx(6.75)


def test_downside_base_upside_are_cited():
    b = bridge()
    names = [s.name for s in b.scenarios]
    assert names[:3] == ["downside_reported_lows", "trailing_run_rate", "latest_period_trend_persists"]
    down, base = b.scenarios[0], b.scenarios[1]
    assert down.recurring_diluted_eps <= base.recurring_diluted_eps


def test_unavailable_recurring_eps_remains_unavailable():
    rs = [r for r in rows() if r.metric != Metric.DILUTED_EPS]
    b = bridge(rs)
    assert b.status == ScenarioStatus.NOT_COMPUTED_MISSING_INPUTS and not b.scenarios
    assert any("diluted share count" in m for m in b.missing_inputs)


def test_equity_warrant_share_collisions_are_rejected():
    b = bridge(rows(shares=10.0, cap_shares=13.0))     # capital row includes warrants / partly paid
    assert b.status == ScenarioStatus.NOT_COMPUTED_MISSING_INPUTS
    assert any("share-count bases disagree" in m for m in b.missing_inputs)


# --- valuation --------------------------------------------------------------------------

SEC = Security("NSE", "T", "EQ")


def market(actions=()):
    prices = {d: 100.0 for d in (date(2024, 6, 14), date(2024, 12, 13), date(2025, 3, 14))}
    prices.update({date(2025, 6, 13): 60.0, date(2025, 6, 20): 999.0})       # 2025-06-20 is after the as-of
    return InMemoryMarketData({("NSE", "T", "EQ"): prices}, {("NSE", "T", "EQ"): list(actions)})


def test_valuation_uses_only_prices_up_to_the_as_of_date():
    v = valuation_context(bridge(), SEC, "EQ", AS_OF, market(), 10.0, QE[-1])
    assert v["status"] == "COMPUTED" and v["price"]["close"] == 60.0 and v["price"]["date"] == "2025-06-13"
    assert v["market_cap_crore"] == 600.0


def test_split_and_bonus_adjust_shares_with_an_as_of_cutoff():
    acts = [CorporateAction(date(2025, 5, 1), "bonus", 2.0, "NSE CA file"),
            CorporateAction(date(2025, 7, 1), "split", 5.0, "NSE CA file")]      # after the as-of: ignored
    v = valuation_context(bridge(), SEC, "EQ", AS_OF, market(acts), 10.0, QE[-1])
    assert v["shares_crore"] == 20.0 and len(v["share_adjustments"]) == 1
    # a 1:1 bonus halves the old price: 100 before the bonus is 50 in today's shares, so 60 is +20%
    assert v["price_change_context"]["90d"]["pct"] == pytest.approx(20.0)
    with pytest.raises(ValueError):
        CorporateAction(date(2025, 5, 1), "bonus", 2.0)                          # unsourced action rejected


def test_series_mismatch_makes_valuation_unavailable_not_wrong():
    v = valuation_context(bridge(), Security("NSE", "T", "W1"), "EQ", AS_OF, market(), 10.0, QE[-1])
    assert v["status"] == "UNAVAILABLE" and "series" in v["reason"]


def test_valuation_never_changes_the_evidence_status():
    from pathlib import Path
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    repo = FixtureRepository(Path(__file__).parent / "fixtures")
    plain = EarningsInflectionPipeline({}, repo).run(["ACMEGRID"], "2024-10-31").assessments[0]
    mk = InMemoryMarketData({("NSE", "ACMEGRID", "EQ"): {date(2024, 10, 30): 400.0, date(2024, 8, 2): 300.0}})
    val = EarningsInflectionPipeline({"valuation": {"enabled": True}}, repo, market_data=mk).run(
        ["ACMEGRID"], "2024-10-31").assessments[0]
    assert val.evidence_status == plain.evidence_status and val.status_rationale == plain.status_rationale
    assert val.valuation["status"] == "COMPUTED" and val.valuation["price_change_context"]
