"""Rules-5: an issuer that files no consolidated statements reports standalone figures even when
its statements do not say so (SML Isuzu). Inferred per figure, from filings public by then."""

import json

from makrograph.earnings_inflection.contracts import Scope
from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
from makrograph.earnings_inflection.source_repository import FixtureRepository

from .test_discovery_catalysts import build


def fixture(tmp_path, consolidated_from=None):
    fx = build(tmp_path)
    data = json.loads((fx / "quiet.json").read_text())
    for d in data["documents"]:
        d["text"] = d["text"].replace("Statement of Standalone Unaudited", "Statement of Unaudited")
        if consolidated_from and d["published_at"][:10] >= consolidated_from and d["doc_id"].startswith("QR-"):
            d["text"] = d["text"].replace("Statement of Unaudited", "Statement of Consolidated Unaudited")
    (fx / "quiet.json").write_text(json.dumps(data))
    return EarningsInflectionPipeline({}, FixtureRepository(fx))


def test_unstated_scope_is_standalone_when_no_consolidated_statements_exist(tmp_path):
    pipe = fixture(tmp_path)
    a = pipe.run(["QUIETCO"], "2024-08-15").assessments[0]
    assert pipe.last_series.scope == Scope.STANDALONE
    assert "scope_inferred" in a.coverage


def test_figures_filed_before_consolidation_began_stay_standalone(tmp_path):
    pipe = fixture(tmp_path, consolidated_from="2024-05-01")
    pipe.run(["QUIETCO"], "2024-04-30")
    assert pipe.last_series.scope == Scope.STANDALONE           # what was knowable then
    pipe.run(["QUIETCO"], "2024-08-15")
    assert pipe.last_series.scope != Scope.UNKNOWN


def test_annual_report_boilerplate_is_not_a_consolidated_statement(tmp_path):
    fx_pipe = fixture(tmp_path)
    data_path = next((tmp_path / "fx").glob("*.json"))
    data = json.loads(data_path.read_text())
    data["documents"].append({"doc_id": "AR", "ticker": "QUIETCO", "source_name": "nse", "doc_type": "announcement",
                              "filing_type": "Annual Report", "title": "Annual Report", "company": "Quiet Co Limited",
                              "published_at": "2023-08-22T18:00:00+05:30",
                              "text": "Reporting boundary: standalone basis (i.e. for the entity and all the entities "
                                      "which form a part of its consolidated financial statements, taken together)."})
    data_path.write_text(json.dumps(data))
    pipe = EarningsInflectionPipeline({}, FixtureRepository(tmp_path / "fx"))
    pipe.run(["QUIETCO"], "2024-08-15")
    assert pipe.last_series.scope == Scope.STANDALONE          # SML Isuzu's BRSR wording (rules-5 first cut: unknown)
