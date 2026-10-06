"""WP9 - offline labelled validation before any performance claim.

Labels and prices here are SYNTHETIC (fictional fixture issuers); they exercise the machinery,
not the detector's real-world quality."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from makrograph.earnings_inflection.discovery import EligibilitySnapshot, UniverseScanner
from makrograph.earnings_inflection.evaluation import (
    FrozenConfig, PriceSeries, build_cohort, classification_report, config_hash, evaluation_report,
    extraction_accuracy, lane_outcomes, lane_return_report,
)
from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
from makrograph.earnings_inflection.source_repository import FixtureRepository

HERE = Path(__file__).parent
FIX = HERE / "fixtures"
SNAP = HERE / "universe" / "snapshot_2024-10-31.json"

LABELS = [  # synthetic reviewer labels (labeler "fixture-reviewer")
    {"ticker": "ACMEGRID", "as_of": "2024-10-31", "should_flag": True, "first_public_signal": "2024-05-24",
     "labeler": "fixture-reviewer"},
    {"ticker": "SMEFAB", "as_of": "2024-10-31", "should_flag": True, "first_public_signal": "2024-05-28",
     "labeler": "fixture-reviewer"},
    {"ticker": "PLAINCO", "as_of": "2024-10-31", "should_flag": False, "labeler": "fixture-reviewer"},
    {"ticker": "CONTRACO", "as_of": "2024-10-31", "should_flag": False, "labeler": "fixture-reviewer"},
    {"ticker": "GRANITEWK", "as_of": "2024-10-31", "should_flag": True, "labeler": "fixture-reviewer",
     "note": "synthetic missed case: reviewer judged the order relevant"},
    {"ticker": "GHOSTCO", "as_of": "2024-10-31", "should_flag": True, "documents_available": False,
     "labeler": "fixture-reviewer"},
]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    repo = FixtureRepository(FIX)
    pipe = EarningsInflectionPipeline({}, repo)
    r = UniverseScanner(pipe, repo, {}).run(EligibilitySnapshot.load(SNAP), "2024-10-31",
                                            tmp_path_factory.mktemp("runs"))
    preds = {}
    for rows in r.shortlist["lanes"].values():
        for x in rows:
            preds[(x["ticker"], "2024-10-31")] = x
    for x in (r.run_dir / "issuers").glob("*.json"):
        import json
        rec = json.loads(x.read_text())
        preds.setdefault((rec["ticker"], "2024-10-31"), rec)
    return r, preds, pipe


def prices(ticker, start=date(2024, 11, 1), days=500, daily=0.001, end=None):
    closes, px, d = {}, 100.0, start
    while d <= (end or start + timedelta(days=days)):
        if d.weekday() < 5:
            closes[d] = round(px, 4)
            px *= 1 + daily
        d += timedelta(days=1)
    return PriceSeries(ticker, closes, "exchange corporate-action file (synthetic)", "NSE EQ only")


def test_offline_labelled_evaluation_runs_end_to_end(run):
    r, preds, _ = run
    cls = classification_report(preds, LABELS)
    # flagged = supported-or-better forward catalyst; SMEFAB's small orders leave it a potential catalyst
    assert (cls["tp"], cls["fp"], cls["fn"], cls["tn"]) == (1, 0, 2, 2)
    assert [m["ticker"] for m in cls["missed_cases"]] == ["SMEFAB", "GRANITEWK"]   # negative / missed cases shown
    assert [m["ticker"] for m in cls["data_coverage_failures"]] == ["GHOSTCO"]     # coverage != detector miss
    assert cls["precision_ci95"][0] is not None and cls["detection_delay_days"]["n"] == 1
    entries = [x for lane in ("EXECUTION_VALIDATING", "POTENTIAL_CATALYST") for x in r.shortlist["lanes"][lane]]
    px = {"ACMEGRID": prices("ACMEGRID"), "SMEFAB": prices("SMEFAB", end=date(2025, 3, 31))}   # SMEFAB delisted
    outs = lane_outcomes(entries, px, {"broad": prices("BROAD", daily=0.0005)}, horizons=(182, 365, 1095),
                         data_until=date(2026, 3, 31))
    rep = lane_return_report(outs)
    frozen = FrozenConfig(config_hash({}), "2024-10-31T00:00:00")
    md = evaluation_report(frozen, {"config_hash": config_hash({})}, cls, None, rep, "synthetic fixture universe")
    assert "95% CI" in md and "Missed cases" in md and "GRANITEWK" in md and "GHOSTCO" in md
    assert "censored" in md


def test_unavailable_outcomes_remain_censored(run):
    r, _, _ = run
    entries = r.shortlist["lanes"]["EXECUTION_VALIDATING"] + r.shortlist["lanes"]["POTENTIAL_CATALYST"]
    outs = lane_outcomes(entries, {"ACMEGRID": prices("ACMEGRID")}, {}, horizons=(182, 1095),
                         data_until=date(2025, 12, 31))
    long = [o for o in outs if o.ticker == "ACMEGRID" and o.horizon_days == 1095][0]
    assert long.censored and long.gross_return_pct is None and "after verified data" in long.censor_reason
    smefab = [o for o in outs if o.ticker == "SMEFAB"]
    assert all(o.censored and "price series" in o.censor_reason for o in smefab)
    short = [o for o in outs if o.ticker == "ACMEGRID" and o.horizon_days == 182][0]
    assert not short.censored and short.entry > date(2024, 10, 31)                # entry after the disclosure
    assert short.net_return_pct < short.gross_return_pct                          # costs applied


def test_future_return_fields_cannot_influence_detection(run, tmp_path):
    r, _, pipe = run
    # outcomes exist only inside evaluation; feeding "future" data around detection changes nothing
    snap = EligibilitySnapshot.load(SNAP)
    for m in snap.members:
        m["future_return_365d"] = 999.0                    # a leaked outcome field in the universe file
    repo = FixtureRepository(FIX)
    again = UniverseScanner(EarningsInflectionPipeline({}, repo), repo, {}).run(snap, "2024-10-31", tmp_path)
    strip = lambda s: {l: [(x["ticker"], x.get("score"), x.get("evidence_status")) for x in rows]  # noqa: E731
                       for l, rows in s["lanes"].items()}
    assert strip(again.shortlist) == strip(r.shortlist)
    import ast
    pkg = Path(__file__).resolve().parents[2] / "src/makrograph/earnings_inflection"
    for f in pkg.glob("*.py"):
        if f.name == "evaluation.py":
            continue
        tree = ast.parse(f.read_text())
        mods = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not any(m.endswith("evaluation") for m in mods), f.name


def test_frozen_config_must_match_the_detector_output():
    frozen = FrozenConfig(config_hash({"thresholds": {"revenue_yoy_pct": 25}}), "2024-10-01T00:00:00")
    with pytest.raises(ValueError):
        frozen.check({"config_hash": config_hash({"thresholds": {"revenue_yoy_pct": 20}})})


def test_company_neutral_cohort_includes_later_failures_and_is_deterministic():
    members = [{"ticker": f"C{i}", "listed_from": "2015-01-01", "sector": "industrials" if i % 2 else "chemicals",
                "size_bucket": "small", "liquidity_bucket": "low", "cadence": "quarterly",
                **({"delisted_on": "2023-06-30"} if i == 3 else {})} for i in range(10)]
    a = build_cohort(members, [date(2022, 3, 31), date(2024, 3, 31)], per_stratum=3)
    assert a == build_cohort(list(reversed(members)), [date(2022, 3, 31), date(2024, 3, 31)], per_stratum=3)
    in_2022 = {x["ticker"] for x in a if x["anchor"] == "2022-03-31"}
    pool_2022 = build_cohort(members, [date(2022, 3, 31)], per_stratum=10)
    assert "C3" in {x["ticker"] for x in pool_2022}               # later-delisted company is eligible in 2022
    assert "C3" not in {x["ticker"] for x in build_cohort(members, [date(2024, 3, 31)], per_stratum=10)}
    assert len(in_2022) == 6


def test_extraction_accuracy_against_keyed_figures():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    from makrograph.earnings_inflection.pipeline import as_of_datetime
    repo = FixtureRepository(FIX)
    parsed = []
    for d in repo.documents_for(["ACMEGRID"], "IN", as_of_datetime("2024-10-31")):
        rows, _ = parse_results_tables(d, chunk_document(d))
        parsed += [{"doc_id": x.doc_id, "metric": x.metric.value, "period_end": x.period_end.isoformat(),
                    "period_type": x.period_type, "scope": x.scope.value, "value": x.value} for x in rows]
    q1 = next(p for p in parsed if p["metric"] == "revenue" and p["period_end"] == "2024-06-30" and p["period_type"] == "Q")
    labels = [{**q1}, {**q1, "value": q1["value"] * 2}, {**q1, "metric": "pat", "period_end": "2010-06-30"}]
    rep = extraction_accuracy(parsed, labels)
    assert (rep["correct"], rep["wrong_value"], rep["not_parsed"]) == (1, 1, 1)
