"""The read-only export must reproduce exactly what the detector saw."""

import importlib.util
from pathlib import Path

from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
from makrograph.earnings_inflection.source_repository import FixtureRepository

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests/earnings_inflection/fixtures"


def _export_module():
    spec = importlib.util.spec_from_file_location("export_ei_fixture", ROOT / "scripts/export_ei_fixture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_export_round_trip_gives_identical_assessments(tmp_path):
    tickers = ["ACMEGRID", "SMEFAB", "PLAINCO", "CONTRACO", "SAMPLEBANK"]
    summary = _export_module().export(FixtureRepository(FIX), tickers, "2024-10-31", tmp_path, since=None)
    assert all(summary[t]["documents"] > 0 for t in tickers)
    before = EarningsInflectionPipeline({}, FixtureRepository(FIX)).run(tickers, "2024-10-31")
    after = EarningsInflectionPipeline({}, FixtureRepository(tmp_path)).run(tickers, "2024-10-31")
    sig = lambda r: {a.ticker: (a.evidence_status, len(a.drivers), len(a.events)) for a in r.assessments}  # noqa: E731
    assert sig(before) == sig(after)
