"""End to end: a company with MUTED reported results and a supported prospective catalyst
reaches the research shortlist through universe discovery (synthetic issuer QUIETCO)."""

import json
from datetime import date, timedelta
from pathlib import Path

from makrograph.earnings_inflection.discovery import EligibilitySnapshot, UniverseScanner
from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
from makrograph.earnings_inflection.source_repository import FixtureRepository

QUARTERS = [date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31), date(2023, 3, 31), date(2023, 6, 30),
            date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31), date(2024, 6, 30)]


def results_text(end: date) -> str:
    ya = date(end.year - 1, end.month, end.day)
    prev = QUARTERS[QUARTERS.index(end) - 1] if QUARTERS.index(end) else date(end.year, end.month, 1) - timedelta(1)
    f = lambda d: d.strftime("%d.%m.%Y")  # noqa: E731
    return f"""QUIET CO LIMITED
Statement of Standalone Unaudited Financial Results for the quarter ended {end:%d %B, %Y}
(Rs. in crore)
Particulars                          Quarter ended
                          {f(end)}   {f(prev)}   {f(ya)}
                          Unaudited    Unaudited    Unaudited
1. Revenue from operations          100.00   100.00   100.00
2. Other income                     1.00   1.00   1.00
3. Total income                     101.00   101.00   101.00
4. Total expenses                   89.00   89.00   89.00
   Depreciation and amortisation    3.00   3.00   3.00
   Finance costs                    1.00   1.00   1.00
5. Profit before tax                12.00   12.00   12.00
6. Tax expense                      3.00   3.00   3.00
7. Profit for the period            9.00   9.00   9.00
8. Earnings per share (face value Rs 10 each)
   (a) Basic                        0.90   0.90   0.90
   (b) Diluted                      0.90   0.90   0.90
"""


def rationale(day: str, book: int, horizon: str) -> str:
    return f"""Rationale
{day}
Quiet Co Limited: Ratings reaffirmed
Summary of rating action
Long term - Fund based [ICRA]BBB (Stable); reaffirmed
Key rating drivers and their description
The company's order book stood at Rs. {book} crore as of {day}, to be executed over the {horizon}.
Credit strengths
Healthy order book position supports revenue visibility.
Liquidity position: Adequate
"""


def build(tmp: Path) -> Path:
    docs = []
    for q in QUARTERS:
        pub = q + timedelta(days=40)
        docs.append({"doc_id": f"QR-{q}", "ticker": "QUIETCO", "source_name": "nse", "doc_type": "announcement",
                     "filing_type": "Financial Results", "title": f"Financial results for quarter ended {q}",
                     "company": "Quiet Co Limited", "published_at": f"{pub}T18:00:00+05:30", "text": results_text(q)})
    for day, pub, book, horizon in (("July 10, 2023", "2023-07-12", 220, "next 12 months"),
                                    ("July 15, 2024", "2024-07-17", 520, "next 12 months")):
        docs.append({"doc_id": f"QRAT-{pub}", "ticker": "QUIETCO", "source_name": "nse", "doc_type": "announcement",
                     "filing_type": "Credit Rating", "title": "Credit Rating", "company": "Quiet Co Limited",
                     "published_at": f"{pub}T18:00:00+05:30", "text": rationale(day, book, horizon)})
    d = tmp / "fx"
    d.mkdir()
    (d / "quiet.json").write_text(json.dumps({"issuers": {"QUIETCO": {"name": "Quiet Co Limited",
                                                                      "industry": "Electrical Equipment"}},
                                              "documents": docs}))
    (tmp / "snap.json").write_text(json.dumps({"snapshot_id": "quiet-2024-08", "as_of": "2024-08-01",
                                               "source": "synthetic", "members": [{"ticker": "QUIETCO",
                                                                                   "status": "listed"}]}))
    return d


def test_muted_results_with_a_supported_catalyst_reach_the_shortlist(tmp_path):
    fx = build(tmp_path)
    repo = FixtureRepository(fx)
    pipe = EarningsInflectionPipeline({}, repo)
    a = pipe.run(["QUIETCO"], "2024-08-15").assessments[0]
    assert a.evidence_status.value in ("NO_MATERIAL_CHANGE", "ASSERTION_ONLY")       # muted reported results
    run = UniverseScanner(pipe, repo, {}).run(EligibilitySnapshot.load(tmp_path / "snap.json"), "2024-08-15",
                                              tmp_path / "runs")
    rows = run.shortlist["lanes"]["PROSPECTIVE_SUPPORTED"]
    assert [r["ticker"] for r in rows] == ["QUIETCO"]
    r = rows[0]
    assert r["forward_setup"]["lead"]["kind"] == "executable_orders"
    assert r["reported_performance"]["evidence_status"] == a.evidence_status.value      # kept as its own dimension
    assert r["first_defensible_signal"][:10] == "2024-07-17"
    assert "QUIETCO" in (run.run_dir / "shortlist.md").read_text()
