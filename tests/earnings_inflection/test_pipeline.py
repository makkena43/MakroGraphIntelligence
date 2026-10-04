"""End-to-end tests on the synthetic fixture set plus isolation guarantees."""

import ast
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from makrograph.earnings_inflection.contracts import (
    IST, EvidenceStatus, ReviewStatus, ScenarioStatus, find_action_language, to_jsonable,
)

PKG = Path(__file__).resolve().parents[2] / "src/makrograph/earnings_inflection"
ROOT = Path(__file__).resolve().parents[2]


def test_statuses(run_oct24):
    assert run_oct24["ACMEGRID"].evidence_status == EvidenceStatus.EXECUTION_CONFIRMED
    assert run_oct24["PLAINCO"].evidence_status == EvidenceStatus.NO_MATERIAL_CHANGE
    assert run_oct24["CONTRACO"].evidence_status == EvidenceStatus.CONTRADICTED
    bank = run_oct24["SAMPLEBANK"]
    assert bank.scenario_status == ScenarioStatus.UNSUPPORTED_FINANCIAL_MODEL
    assert bank.issuer_model.value == "bank"


def test_review_status_never_self_approved(run_oct24):
    assert all(a.review_status in (ReviewStatus.UNREVIEWED, ReviewStatus.NEEDS_SOURCE_CHECK)
               for a in run_oct24.values())


def test_order_mentions_deduplicated_into_events(run_oct24):
    a = run_oct24["ACMEGRID"]
    # 5 mentions: original order (announcement + Q4 call), repeat order (announcement + Q1 call), LoI
    assert a.coverage["order_mentions"] == 5
    assert a.coverage["economic_events_after_dedup"] == 3
    strengths = sorted(e.commitment_strength.value for e in a.events)
    assert strengths == ["binding", "binding", "provisional"]


def test_invitation_classified_and_undated_doc_excluded(run_oct24):
    a = run_oct24["ACMEGRID"]
    assert a.coverage["by_kind"]["earnings_call_invitation"] == 1
    assert a.coverage["by_kind"]["earnings_call_transcript"] == 2
    assert a.coverage["excluded_unknown_time"] == 1
    # the undated Rs 900 crore claim never reaches the events
    assert all(e.amount.value != 900 for e in a.events)


def test_point_in_time_no_lookahead(repo):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    pipe = EarningsInflectionPipeline({}, repo)
    early = pipe.run(["ACMEGRID"], "2024-05-16").assessments[0]
    assert all(s.available_at <= datetime(2024, 5, 16, 23, 59, 59, tzinfo=IST) for s in early.sources)
    assert not early.guidance                          # FY25 guidance was first stated 2024-05-24
    late = pipe.run(["ACMEGRID"], "2024-11-30").assessments[0]
    assert any(s.doc_id == "ACME-R-2024-09-30" for s in late.sources)
    assert not any(s.doc_id == "ACME-R-2024-09-30" for s in early.sources)


def test_same_day_publication_visible_only_after_timestamp(repo):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    pipe = EarningsInflectionPipeline({}, repo)
    before = pipe.run(["ACMEGRID"], datetime(2024, 8, 8, 17, 0, tzinfo=IST)).assessments[0]
    after = pipe.run(["ACMEGRID"], datetime(2024, 8, 8, 18, 0, tzinfo=IST)).assessments[0]
    ids = lambda a: {s.doc_id for s in a.sources}  # noqa: E731
    assert "ACME-R-2024-06-30" not in ids(before) and "ACME-R-2024-06-30" in ids(after)


def test_filed_at_only_document_end_of_day(repo):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    pipe = EarningsInflectionPipeline({}, repo)
    a = pipe.run(["PLAINCO"], datetime(2024, 8, 12, 12, 0, tzinfo=IST)).assessments[0]
    assert "PLAIN-R-2024-06-30" not in {s.doc_id for s in a.sources}
    b = pipe.run(["PLAINCO"], "2024-08-12").assessments[0]
    assert "PLAIN-R-2024-06-30" in {s.doc_id for s in b.sources}


def test_bridge_uses_ttm_not_annualised_quarter(run_oct24):
    b = run_oct24["ACMEGRID"].bridge
    base = next(s for s in b.scenarios if s.name == "trailing_run_rate")
    assert base.revenue_crore == 125 + 135 + 160 + 165          # four reported quarters
    assert base.revenue_crore != 165 * 4


def test_guidance_reiteration_tracked(run_oct24):
    g = next(x for x in run_oct24["ACMEGRID"].guidance if x.metric.value == "revenue_growth_guidance")
    assert g.original.quantity.low == 25 and [r.direction.value for r in g.revisions] == ["reiterated"]
    assert g.outcome.value == "PENDING"


def test_outputs_have_sources_limitations_and_no_action_language(run_oct24):
    from makrograph.earnings_inflection.rendering import render_json, render_markdown
    for a in run_oct24.values():
        md, js = render_markdown(a), render_json(a)
        assert "## Sources" in md and "## Limitations" in md and "Research-only" in md
        assert not find_action_language(to_jsonable(a) | {"notice": ""})
        data = json.loads(js)
        assert data["limitations"]
        for key in ("action", "recommendation", "position_size", "target_price"):
            assert key not in data


def test_one_company_failure_does_not_abort_run(repo):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline

    class Broken:
        def __getattr__(self, n):
            return getattr(repo, n)

        def documents(self, ticker, country, as_of):
            if ticker == "PLAINCO":
                raise IOError("boom")
            return repo.documents(ticker, country, as_of)

    res = EarningsInflectionPipeline({}, Broken()).run(["PLAINCO", "ACMEGRID"], "2024-10-31")
    assert "PLAINCO" in res.errors and [a.ticker for a in res.assessments] == ["ACMEGRID"]


def test_persistence_disabled_by_default_and_refuses_non_test_db(repo, monkeypatch, run_oct24):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline, PersistenceDisabled, RunResult
    res = RunResult(as_of=datetime(2024, 10, 31, tzinfo=IST), assessments=list(run_oct24.values()))
    with pytest.raises(PersistenceDisabled):
        EarningsInflectionPipeline({}, repo).persist(res)
    monkeypatch.setenv("EI_TEST_DSN", "postgresql://u@h:5432/makrograph")
    with pytest.raises(PersistenceDisabled):
        EarningsInflectionPipeline({"persistence": {"enabled": True}}, repo).persist(res, connect=lambda d: None)


def test_llm_and_budget_off_by_default(repo):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    res = EarningsInflectionPipeline({}, repo).run(["ACMEGRID"], "2024-10-31")
    assert res.budget["enabled"] is False and res.budget["calls"] == 0


def test_evaluation_price_identity_guards():
    from makrograph.earnings_inflection.evaluation import PriceSeries
    with pytest.raises(ValueError):
        PriceSeries("X", {}, adjustment_method="assumed adjusted: no daily move > 45%", series_filter="EQ")
    with pytest.raises(ValueError):
        PriceSeries("X", {}, adjustment_method="exchange CA file", series_filter="")


def test_evaluation_starts_after_as_of(run_oct24):
    from makrograph.earnings_inflection.evaluation import PriceSeries, forward_outcomes
    closes = {date(2024, 10, 31): 50.0, date(2024, 11, 1): 100.0, date(2025, 5, 2): 120.0}
    ps = PriceSeries("ACMEGRID", closes, "exchange corporate-action file", "NSE EQ only")
    rows = forward_outcomes([run_oct24["ACMEGRID"]], {"ACMEGRID": ps}, horizons=(182,))
    assert rows[0].start == date(2024, 11, 1) and rows[0].return_pct == 20.0


# ---------------- isolation ----------------

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mods.add(("." * n.level) + (n.module or ""))
        elif isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
    return mods


def test_detection_never_imports_evaluation_or_production_modules():
    banned = ("evaluation", "guidance_radar", "pg_store", "storage", "pipeline.runner", "ranking", "themes",
              "fetcher", "india", "stock_selector", "requests", "aiohttp", "anthropic", "openai")
    for f in PKG.glob("*.py"):
        if f.name == "evaluation.py":
            continue
        for m in _imports(f):
            leaf = m.lstrip(".")
            assert not any(leaf == b or leaf.startswith(b + ".") or leaf.endswith("." + b) for b in banned), (f.name, m)


def test_package_not_wired_into_existing_code():
    hits = subprocess.run(["grep", "-rl", "earnings_inflection", str(ROOT / "src"), str(ROOT / "backend"),
                           str(ROOT / "run_pipeline.py")], capture_output=True, text=True).stdout.split()
    assert all("/earnings_inflection/" in h for h in hits), hits


def test_cli_offline_run(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts/earnings_inflection.py"), "--ticker", "ACMEGRID",
                        "--as-of", "2024-10-31", "--out", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "ACMEGRID_2024-10-31.md").exists() and (tmp_path / "ACMEGRID_2024-10-31.json").exists()


def test_cli_refuses_implicit_market_run():
    r = subprocess.run([sys.executable, str(ROOT / "scripts/earnings_inflection.py"), "--as-of", "2024-10-31"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "required" in r.stderr
