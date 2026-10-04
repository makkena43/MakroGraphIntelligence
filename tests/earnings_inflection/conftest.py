import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def repo():
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    return FixtureRepository(FIXTURES)


@pytest.fixture(scope="session")
def run_oct24(repo):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    res = EarningsInflectionPipeline({"country": "IN"}, repo).run(
        ["ACMEGRID", "PLAINCO", "CONTRACO", "SAMPLEBANK"], "2024-10-31")
    assert not res.errors, res.errors
    return {a.ticker: a for a in res.assessments}
