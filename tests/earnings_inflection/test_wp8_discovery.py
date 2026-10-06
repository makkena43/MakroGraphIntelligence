"""WP8 - bounded universe discovery and explainable research shortlisting."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from makrograph.earnings_inflection.discovery import EligibilitySnapshot, UniverseScanner, fair_order
from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
from makrograph.earnings_inflection.source_repository import FixtureRepository

HERE = Path(__file__).parent
FIX = HERE / "fixtures"
SNAP = HERE / "universe" / "snapshot_2024-10-31.json"


def scan(tmp_path, snap=None, repo_path=FIX, as_of="2024-10-31", **kw):
    repo = FixtureRepository(repo_path)
    cfg = kw.pop("cfg", {})
    return UniverseScanner(EarningsInflectionPipeline({}, repo), repo, cfg).run(
        snap or EligibilitySnapshot.load(SNAP), as_of, tmp_path, **kw)


def lanes(run):
    return {lane: [(r["ticker"], r.get("score")) for r in rows] for lane, rows in run.shortlist["lanes"].items()}


def test_small_offline_universe_gives_deterministic_lanes_and_ranks(tmp_path):
    a, b = scan(tmp_path / "a"), scan(tmp_path / "b")
    assert lanes(a) == lanes(b)
    # lanes follow the current forward catalyst; reported performance is a separate dimension
    assert [t for t, _ in lanes(a)["EXECUTION_VALIDATING"]] == ["ACMEGRID"]
    assert [t for t, _ in lanes(a)["POTENTIAL_CATALYST"]] == ["SMEFAB"]       # small orders: watch, not flagged
    assert [t for t, _ in lanes(a)["CONTRADICTED_OR_STALE"]] == ["CONTRACO"]
    assert a.shortlist["lanes"]["PROSPECTIVE_SUPPORTED"] == []                 # never padded
    assert a.shortlist["lanes"]["CONFIRMED_FOR_REVIEW"] == []
    row = a.shortlist["lanes"]["EXECUTION_VALIDATING"][0]
    assert row["reported_performance"]["evidence_status"] == "EXECUTION_CONFIRMED"
    assert row["forward_setup"]["lead"]["stage"] == "execution_validating"
    for k in ("security", "as_of", "lane", "change_since_previous", "mechanisms", "first_defensible_signal",
              "score_components", "strongest_citations", "contradictions", "scenarios_available",
              "valuation_available", "next_milestones", "review_effort", "reported_performance", "forward_setup"):
        assert k in row
    assert "not probabilities" in a.shortlist["ranking"]["note"]
    assert (a.run_dir / "shortlist.md").read_text().count("research question") >= 2


def test_symbol_order_does_not_alter_coverage(tmp_path):
    snap = EligibilitySnapshot.load(SNAP)
    shuffled = replace(snap, members=list(reversed(snap.members)))
    a, b = scan(tmp_path / "a", snap), scan(tmp_path / "b", shuffled)
    assert a.counts == b.counts and lanes(a) == lanes(b)
    assert fair_order("s", ["A", "B", "C"]) == fair_order("s", ["C", "A", "B"])
    tickers = [m["ticker"] for m in snap.members]
    assert fair_order(snap.snapshot_id, tickers) != sorted(tickers)      # not first-alphabetical


def test_missing_document_issuers_stay_in_the_denominator(tmp_path):
    r = scan(tmp_path)
    assert set(r.shortlist["not_assessed"]) == {"GHOSTCO", "OLDDELIST"}
    assert r.counts["eligible"] == 7 and r.counts["completed"] == 5 and r.counts["coverage_rate"] == pytest.approx(5 / 7, abs=1e-3)
    assert r.counts["excluded"] == 1 and "SAMPLEBANK" in r.shortlist["excluded"]
    # assessed, no current forward catalyst and no qualifying reported change (PLAINCO; GRANITEWK has
    # management assertions only, which are not catalysts)
    assert r.counts["no_qualifying_evidence"] == 2


def test_checkpoint_resume_reproduces_the_full_run(tmp_path):
    full = scan(tmp_path / "full", cfg={"page_size": 2})
    part = scan(tmp_path / "part", cfg={"page_size": 2}, stop_after_pages=1)
    assert part.status == "PARTIAL" and not part.shortlist["coverage_complete"]
    resumed = scan(tmp_path / "part", cfg={"page_size": 2}, resume=part.run_dir)
    assert resumed.status == "COMPLETE" and lanes(resumed) == lanes(full) and resumed.counts == full.counts


def test_runs_are_immutable(tmp_path):
    r = scan(tmp_path)
    with pytest.raises(FileExistsError):
        r.run_dir.mkdir(parents=True, exist_ok=False)
    assert r.run_dir.name.startswith("2024-10-31_fixture-in-2024-10_")


def test_partial_budget_run_cannot_claim_complete_coverage(tmp_path):
    r = scan(tmp_path, cfg={"max_issuers": 3})
    assert r.status == "PARTIAL" and not r.shortlist["coverage_complete"]
    assert r.counts["deferred"] == 4 and len(r.shortlist["deferred"]) == 4


def test_historical_run_with_a_later_snapshot_is_labelled_limited(tmp_path):
    snap = replace(EligibilitySnapshot.load(SNAP), as_of=__import__("datetime").date(2025, 1, 1))
    r = scan(tmp_path, snap)
    assert r.shortlist["cohort_note"].startswith("LIMITED COHORT")


def test_newer_disclosures_generate_deltas_and_unchanged_issuers_carry_forward(tmp_path):
    first = scan(tmp_path / "r1", as_of="2024-11-01")
    later = scan(tmp_path / "r2", as_of="2024-12-31", previous=first.run_dir)
    recs = {r["ticker"]: r for rows in later.shortlist["lanes"].values() for r in rows}
    delta = recs["SMEFAB"]["change_since_previous"]
    assert delta.startswith("lead catalyst executable_orders potential_catalyst -> executable_orders "
                            "execution_validating")
    assert "reported status EXECUTION_EMERGING -> EXECUTION_CONFIRMED" in delta
    carried = [json.loads(p.read_text()) for p in (later.run_dir / "issuers").glob("*.json")]
    assert any(r.get("carried_forward") for r in carried)            # issuers without new documents
