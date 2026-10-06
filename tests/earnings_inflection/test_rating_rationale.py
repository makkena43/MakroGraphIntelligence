"""Credit-rating rationales: full-field extraction, observed vs expected, dated versions.
Synthetic rationales (fictional issuers) in the formats used by Indian agencies."""

from datetime import date, datetime

from makrograph.earnings_inflection.contracts import IST, DocumentKind, SourceDocument
from makrograph.earnings_inflection.document_versions import classify_document
from makrograph.earnings_inflection.rating_rationale import parse_rationale, rationale_history

CARE = """Press Release
Fictional Gears Limited
June 12, 2024
Fictional Gears Limited: Ratings upgraded
Ratings
Long Term Bank Facilities 120.00 CARE BBB+; Stable Upgraded from CARE BBB; Stable
Rationale and key rating drivers
The upgrade reflects the commissioning of the new forging line in March 2024, which increased the installed capacity
of the company to 36,000 TPA from 24,000 TPA. The capacity utilisation stood at 82% in FY2024.
The company's order book stood at Rs. 410 crore as of May 31, 2024, which is 1.3 times the operating income of FY2024,
to be executed over the next 15 months. The company plans to incur a capex of Rs. 60 crore in FY2025 for a heat
treatment unit, to be funded through a term loan of Rs. 40 crore and internal accruals; the unit is expected to be
commissioned by March 2025.
The top five customers accounted for 58% of revenue in FY2024. Around 70% of the contracts have a raw material
price variation clause. Sales to export customers are backed by letters of credit.
The working capital intensity of operations remained high at 28%, with receivable days of 96 days and inventory days
of 75 days. The DSCR is expected to be 1.6 times in FY2025, with repayment obligations of Rs. 18 crore.
Liquidity: Adequate
The average utilisation of the fund-based working capital limits was 74% for the 12 months ended April 2024.
Rating sensitivities
Positive factors: Sustained revenue above Rs. 400 crore with PBILDT margin above 14%.
Negative factors: DSCR below 1.2 times on a sustained basis.
CARE Ratings Limited
"""

CRISIL = """Rating Rationale
September 4, 2023
Fictional Gears Limited: Rating reaffirmed at 'CRISIL BBB/Stable'
Key Rating Drivers & Detailed Description
The order book of the company stood at Rs 290 crore as on July 31, 2023.
Liquidity: Adequate
CRISIL Ratings Limited
"""


def doc(text, did, when):
    d = SourceDocument(doc_id=did, source_name="nse", ticker="FGL", text=text, title="Credit Rating",
                       published_at=datetime.combine(when, datetime.min.time(), tzinfo=IST))
    d.available_at = d.published_at
    return d


def facts(r, name):
    return [(f.value, f.expected, f.text) for f in r.get(name)]


def test_rationale_is_its_own_document_kind():
    assert classify_document(doc(CARE, "c", date(2024, 6, 13)))[0] == DocumentKind.CREDIT_RATING_RATIONALE


def test_full_rationale_fields_and_observed_vs_expected():
    r = parse_rationale(doc(CARE, "c", date(2024, 6, 13)))
    assert (r.agency, r.rationale_date, r.action) == ("CARE", date(2024, 6, 12), "upgraded")
    assert r.long_term_rating == "CARE BBB+" and r.outlook == "Stable"
    assert facts(r, "capacity_change")[0][:2] == (36000.0, False)
    assert facts(r, "utilization")[0][:2] == (82.0, False)
    assert facts(r, "order_book")[0][:2] == (410.0, False)                 # observed snapshot
    assert any("next 15 months" in t for _, _, t in facts(r, "execution_horizon"))
    capex = facts(r, "capex")[0]
    assert capex[0] == 60.0 and capex[1] is True                            # a plan: corroborating context only
    assert any("term loan" in t for _, _, t in facts(r, "capex_funding"))
    assert any(e for _, e, t in facts(r, "completion") if "March 2025" in t)
    assert facts(r, "customer_concentration")[0][0] == 58.0
    assert facts(r, "pass_through")[0][0] == 70.0
    assert facts(r, "payment_protection")
    assert facts(r, "working_capital_intensity")[0][0] == 28.0
    assert facts(r, "receivable_days")[0][0] == 96.0 and facts(r, "inventory_days")[0][0] == 75.0
    assert facts(r, "dscr")[0][:2] == (1.6, True)
    assert facts(r, "debt_obligations")[0][0] == 18.0
    assert facts(r, "bank_limit_utilization")[0][0] == 74.0               # liquidity, not plant utilisation
    assert len(facts(r, "utilization")) == 1
    assert any("DSCR below 1.2" in x for x in r.sensitivities)
    assert not any(f.value == 1.2 for f in r.get("dscr"))                  # a downgrade trigger is not a fact


def test_every_dated_version_is_kept():
    old = parse_rationale(doc(CRISIL, "k", date(2023, 9, 5)))
    new = parse_rationale(doc(CARE, "c", date(2024, 6, 13)))
    dup = parse_rationale(doc(CARE, "c2", date(2024, 6, 14)))              # same rationale filed twice
    hist = rationale_history([new, dup, old])
    assert [h.rationale_date for h in hist] == [date(2023, 9, 4), date(2024, 6, 12)]
    assert old.agency == "CRISIL" and old.long_term_rating == "CRISIL BBB" and facts(old, "order_book")[0][0] == 290.0
