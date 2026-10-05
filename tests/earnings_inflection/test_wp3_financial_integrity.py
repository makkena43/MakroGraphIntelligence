"""WP3 - financial integrity before qualification."""

from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.chunking import chunk_document
from makrograph.earnings_inflection.contracts import (
    IST, FinancialMeasurement, IssuerModel, Metric, Scope, SourceDocument, Unit,
)
from makrograph.earnings_inflection.drivers import compute_drivers
from makrograph.earnings_inflection.extraction import parse_results_tables
from makrograph.earnings_inflection.financial_series import FinancialSeries
from makrograph.earnings_inflection.validation import Integrity, reconcile_structured

Q_HEADER = ["(Rs. in crore)", "Particulars Quarter ended Year ended", "30.06.2024 31.03.2024 30.06.2023 31.03.2024"]


def statement(rows, scope="Consolidated", header=Q_HEADER, doc_id="S", published=datetime(2024, 8, 8, tzinfo=IST)):
    text = "\n".join([f"Statement of {scope} Unaudited Financial Results for the quarter ended 30 June 2024",
                      *header, *[f"{lab} {' '.join(vals)}" for lab, vals in rows]])
    d = SourceDocument(doc_id=doc_id, source_name="t", ticker="T", text=text, published_at=published)
    d.available_at = published
    out, issues = parse_results_tables(d, chunk_document(d))
    return out, issues


def run(rows, **kw):
    ms, _ = statement(rows, **kw)
    recon = reconcile_structured(ms)
    s = FinancialSeries.build("T", ms)
    drivers, missing = compute_drivers(s, [], [], IssuerModel.OPERATING, date(2024, 9, 30))
    return s, drivers, missing, recon


def drv(drivers, name):
    return next((d for d in drivers if d.driver == name), None)


BASE = [("1. Revenue from operations", ["300.00", "280.00", "200.00", "1,100.00"]),
        ("2. Other income", ["2.00", "2.00", "2.00", "8.00"]),
        ("4. Total expenses", ["260.00", "245.00", "185.00", "960.00"]),
        ("Depreciation and amortisation", ["10.00", "10.00", "8.00", "38.00"]),
        ("Finance costs", ["5.00", "5.00", "5.00", "20.00"])]


def pl(pbt_now, tax_now, pat_now, pbt_ya, tax_ya, pat_ya, attr_now=None, attr_ya=None):
    rows = BASE + [("5. Profit before tax", [f"{pbt_now:.2f}", "37.00", f"{pbt_ya:.2f}", "148.00"]),
                   ("6. Tax expense", [f"{tax_now:.2f}", "9.00", f"{tax_ya:.2f}", "37.00"]),
                   ("7. Profit for the period", [f"{pat_now:.2f}", "28.00", f"{pat_ya:.2f}", "111.00"])]
    if attr_now is not None:
        rows.append(("- Owners of the Company", [f"{attr_now:.2f}", "27.00", f"{attr_ya:.2f}", "106.00"]))
    return rows


# ---------------- reconciliation gates dependent calculations ----------------

def test_inconsistent_pat_cannot_qualify_but_revenue_survives():
    rows = pl(42, 10.5, 60.0, 17, 4.25, 12.75)          # PAT 60 != 42 - 10.5 = 31.5
    s, drivers, missing, recon = run(rows)
    pat = [r for r in recon if r.check == "pat_pbt_tax" and r.period_end == date(2024, 6, 30)][0]
    assert pat.status == Integrity.UNRESOLVED
    assert drv(drivers, "pat_yoy_growth") is None and any("recurring earnings" in m for m in missing)
    assert drv(drivers, "revenue_yoy_growth").current == pytest.approx(50.0)
    assert s.get(Metric.PAT, date(2024, 6, 30), "Q") is None                   # excluded from calculations
    assert any("unresolved" in x for x in s.excluded)                          # ... but preserved as a fact


def test_validated_pat_qualifies():
    s, drivers, _, recon = run(pl(42, 10.5, 31.5, 17, 4.25, 12.75))
    assert all(r.status == Integrity.VALIDATED for r in recon if r.check == "pat_pbt_tax")
    assert drv(drivers, "pat_yoy_growth").current == pytest.approx(147.06, abs=0.01)


def test_parent_flat_while_group_rises_shows_no_parent_growth():
    s, drivers, _, _ = run(pl(42, 10.5, 31.5, 17, 4.25, 12.75, attr_now=12.0, attr_ya=12.0))
    d = drv(drivers, "pat_yoy_growth")
    assert d.current == 0.0 and "parent-attributable" in d.basis and not d.material


def test_total_pat_substitute_is_labelled_only_when_parent_not_reported():
    _, drivers, _, _ = run(pl(42, 10.5, 31.5, 17, 4.25, 12.75))
    assert "total PAT" in drv(drivers, "pat_yoy_growth").basis


def test_land_gain_in_exceptional_items_is_not_recurring():
    rows = BASE + [("Profit before exceptional items and tax", ["20.00", "37.00", "17.00", "148.00"]),
                   ("Exceptional items (gain on sale of land)", ["(80.00)", "-", "-", "-"]),
                   ("5. Profit before tax", ["100.00", "37.00", "17.00", "148.00"]),
                   ("6. Tax expense", ["25.00", "9.00", "4.25", "37.00"]),
                   ("7. Profit for the period", ["75.00", "28.00", "12.75", "111.00"]),
                   ("- Owners of the Company", ["75.00", "28.00", "12.75", "111.00"])]
    s, drivers, _, recon = run(rows)
    assert s.exceptional(date(2024, 6, 30), "Q")[0] == pytest.approx(80.0)      # sign proven: gain
    assert s.recurring_pat(date(2024, 6, 30), "Q")[0] is None                   # after-tax effect undisclosed
    d = drv(drivers, "pat_yoy_growth")
    assert "recurring PBT" in d.basis and d.current == pytest.approx(17.65, abs=0.01)  # 20 vs 17, not 75 vs 12.75
    assert not d.material


def test_exceptional_sign_unprovable_makes_recurring_unavailable():
    rows = pl(42, 10.5, 31.5, 17, 4.25, 12.75, attr_now=31.5, attr_ya=12.75)
    rows.insert(5, ("Exceptional items", ["12.00", "-", "-", "-"]))
    s, drivers, missing, _ = run(rows)
    v, basis = s.recurring_pat(date(2024, 6, 30), "Q")
    assert v is None and "sign cannot be proven" in basis
    assert drv(drivers, "pat_yoy_growth") is None


def test_other_income_spike_is_not_recurring_improvement():
    rows = [r if r[0] != "2. Other income" else ("2. Other income", ["40.00", "2.00", "2.00", "8.00"]) for r in BASE]
    rows += [("5. Profit before tax", ["77.00", "37.00", "17.00", "148.00"]),
             ("6. Tax expense", ["19.25", "9.00", "4.25", "37.00"]),
             ("7. Profit for the period", ["57.75", "28.00", "12.75", "111.00"])]
    _, drivers, _, _ = run(rows)
    d = drv(drivers, "pat_yoy_growth")
    assert not d.material and any("non-operating" in n for n in d.notes)


# ---------------- scope, bases, durations ----------------

def test_consolidated_and_standalone_never_mix():
    cons, _ = statement(BASE, scope="Consolidated", doc_id="C")
    stand, _ = statement([("1. Revenue from operations", ["999.00", "999.00", "999.00", "999.00"])],
                         scope="Standalone", doc_id="S2")
    s = FinancialSeries.build("T", cons + stand)
    assert s.scope == Scope.CONSOLIDATED and s.get(Metric.REVENUE, date(2023, 6, 30)).value == 200.0
    only_cur = [m for m in cons if m.period_end == date(2024, 6, 30)]
    only_ya = [m for m in stand if m.period_end == date(2023, 6, 30)]
    s2 = FinancialSeries.build("T", only_cur + only_ya)
    assert s2.yoy(Metric.REVENUE, date(2024, 6, 30)) is None                  # no cross-scope comparison


@pytest.mark.parametrize("now,prior,kind,pct", [
    (12.0, -5.0, "loss_to_profit", None), (12.0, 0.5, "low_base", None), (93.0, 30.0, "growth", 210.0),
    (-2.0, -9.0, "loss_narrowed", None), (-3.0, 4.0, "profit_to_loss", None)])
def test_change_profile_handles_zero_negative_and_low_bases(now, prior, kind, pct):
    prof = FinancialSeries("T", Scope.CONSOLIDATED).change_profile(now, prior)
    assert prof["kind"] == kind and (prof["pct"] == pytest.approx(pct) if pct else prof["pct"] is None)


def test_loss_to_profit_is_absolute_not_percent():
    _, drivers, _, _ = run(pl(42, 10.5, 31.5, -8, 0.0, -8.0, attr_now=31.5, attr_ya=-8.0))
    d = drv(drivers, "pat_loss_to_profit")
    assert d.unit == "crore" and d.change == pytest.approx(39.5) and d.material
    assert drv(drivers, "pat_yoy_growth") is None


def test_sme_half_year_turnaround():
    H = ["(Rs. in crore)", "Particulars Half year ended Year ended", "30.09.2024 31.03.2024 30.09.2023 31.03.2024"]
    rows = [("1. Revenue from operations", ["60.00", "55.00", "40.00", "95.00"]),
            ("5. Profit before tax", ["8.00", "3.00", "-2.00", "1.00"]),
            ("6. Tax expense", ["2.00", "0.75", "0.00", "0.75"]),
            ("7. Profit for the period", ["6.00", "2.25", "-2.00", "0.25"])]
    s, drivers, _, _ = run(rows, header=H)
    assert s.cadence() == "H"
    d = drv(drivers, "pat_loss_to_profit")
    assert d is not None and "half-year" in d.basis


def test_ttm_never_sums_overlapping_periods():
    rows = [FinancialMeasurement("T", Metric.REVENUE, date(2024, 12, 31), t, v, Unit.INR_CRORE, Scope.CONSOLIDATED,
                                 "d", datetime(2025, 2, 1, tzinfo=IST)) for t, v in (("9M", 900.0), ("Q", 320.0))]
    rows.append(FinancialMeasurement("T", Metric.REVENUE, date(2024, 3, 31), "FY", 1100.0, Unit.INR_CRORE,
                                     Scope.CONSOLIDATED, "d", datetime(2025, 2, 1, tzinfo=IST)))
    v, missing = FinancialSeries.build("T", rows).ttm(Metric.REVENUE, date(2024, 12, 31), "Q")
    assert v is None and missing


# ---------------- definitions and stated vs computed ----------------

def test_reported_ebitda_including_other_income_is_a_definition_difference():
    rows = BASE + [("EBITDA", ["57.00", "52.00", "30.00", "186.00"])]       # = operating 55 + other income 2
    s, drivers, _, recon = run(rows)
    r = [x for x in recon if x.check == "ebitda_definition" and x.period_end == date(2024, 6, 30)][0]
    assert r.status == Integrity.DEFINITION_DIFFERENCE
    assert s.get(Metric.EBITDA, date(2024, 6, 30)).value == pytest.approx(55.0)   # consistent definition used


def test_stated_margin_conflict_flagged_and_both_preserved():
    from makrograph.earnings_inflection.contracts import Evidence, EvidenceTier, Modality, Quantity
    from makrograph.earnings_inflection.validation import margin_conflicts
    rows = [FinancialMeasurement("T", m, date(2024, 3, 31), "FY", v, Unit.INR_CRORE, Scope.CONSOLIDATED, "d",
                                 datetime(2024, 5, 1, tzinfo=IST)) for m, v in ((Metric.REVENUE, 2727.0),
                                                                                 (Metric.EBITDA, 231.0))]
    s = FinancialSeries.build("T", rows)
    ev = Evidence("e", "d", "T", Metric.EBITDA_MARGIN, EvidenceTier.REALIZED_EXECUTION, Modality.REALIZED,
                  "EBITDA margin was 13% in FY24.", datetime(2024, 5, 2, tzinfo=IST),
                  quantity=Quantity(13.0, Unit.PERCENT), period_label="FY24")
    out = margin_conflicts([ev], s)
    assert out and "8.47%" in out[0] and ev.usable and ev.quantity.value == 13.0


# ---------------- balance sheet, cash flow, share capital ----------------

def _parse(text, published=datetime(2024, 11, 10, tzinfo=IST)):
    d = SourceDocument(doc_id="BS", source_name="t", ticker="T", text=text, published_at=published)
    d.available_at = published
    return parse_results_tables(d, chunk_document(d))


BS_TEXT = "\n".join([
    "Statement of Consolidated Assets and Liabilities", "(Rs. in crore)",
    "Particulars As at 30.09.2024 As at 31.03.2024",
    "ASSETS", "Non-current assets",
    "Property, plant and equipment 500.00 480.00",
    "Current assets", "Inventories 120.00 100.00", "Trade receivables 150.00 130.00",
    "Cash and cash equivalents 40.00 60.00",
    "Total assets 810.00 770.00",
    "EQUITY AND LIABILITIES", "Equity",
    "Equity share capital (Face value Rs 10 each) 20.00 20.00", "Other equity 390.00 340.00",
    "Non-current liabilities", "Borrowings 250.00 300.00",
    "Current liabilities", "Borrowings 100.00 80.00", "Trade payables 50.00 30.00",
    "Total equity and liabilities 810.00 770.00",
])

CF_TEXT = "\n".join([
    "Statement of Consolidated Cash Flows", "(Rs. in crore)",
    "Particulars Half year ended", "30.09.2024 30.09.2023",
    "Net cash generated from operating activities (A) 45.00 20.00",
    "Net cash used in investing activities (B) (30.00) (25.00)",
    "Net cash used in financing activities (C) (35.00) 10.00",
    "Net increase/(decrease) in cash and cash equivalents (A+B+C) (20.00) 5.00",
])


def test_balance_sheet_instant_columns_sections_and_identity():
    rows, issues = _parse(BS_TEXT)
    got = {(r.metric, r.period_end): r.value for r in rows}
    assert {r.period_type for r in rows} == {"I"}
    assert got[(Metric.BORROWINGS_NONCURRENT, date(2024, 9, 30))] == 250.0
    assert got[(Metric.BORROWINGS_CURRENT, date(2024, 9, 30))] == 100.0
    assert got[(Metric.CASH, date(2024, 9, 30))] == 40.0 and got[(Metric.FACE_VALUE, date(2024, 9, 30))] == 10.0
    recon = reconcile_structured(rows)
    assert all(r.status == Integrity.VALIDATED for r in recon if r.check == "balance_sheet_identity")
    s = FinancialSeries.build("T", rows)
    assert s.shares_from_capital(date(2024, 9, 30), "I") == pytest.approx(2.0)


def test_cash_flow_identity_and_cash_conversion():
    rows, _ = _parse(CF_TEXT)
    recon = reconcile_structured(rows)
    assert [r.status for r in recon if r.check == "cash_flow_identity"] == [Integrity.VALIDATED] * 2
    bad = [r for r in rows]
    for r in bad:
        if r.metric == Metric.NET_CHANGE_IN_CASH and r.period_end == date(2024, 9, 30):
            r.value = 99.0
    for r in bad:
        r.integrity, r.integrity_notes = "unchecked", []
    assert any(r.status == Integrity.UNRESOLVED for r in reconcile_structured(bad) if r.check == "cash_flow_identity")


def test_debt_driver_and_equity_funded_deleveraging_note():
    bs, _ = _parse(BS_TEXT)
    later = BS_TEXT.replace("As at 30.09.2024 As at 31.03.2024", "As at 31.03.2025 As at 30.09.2024") \
        .replace("Borrowings 250.00 300.00", "Borrowings 150.00 250.00") \
        .replace("Equity share capital (Face value Rs 10 each) 20.00 20.00",
                 "Equity share capital (Face value Rs 10 each) 24.00 20.00")
    bs2, _ = _parse(later, datetime(2025, 5, 20, tzinfo=IST))
    s = FinancialSeries.build("T", bs + bs2)
    drivers, _ = compute_drivers(s, [], [], IssuerModel.OPERATING, date(2025, 6, 30))
    d = drv(drivers, "debt_change")
    assert d.prior == pytest.approx(300 + 80 - 60) and d.current == pytest.approx(150 + 100 - 40)  # Mar-24 vs Mar-25
    assert any("equity-funded" in n for n in d.notes) and not d.material
    assert drv(drivers, "share_count_change").current == pytest.approx(2.4)
