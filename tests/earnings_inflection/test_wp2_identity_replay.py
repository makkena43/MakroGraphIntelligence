"""WP2 - identity, provenance and point-in-time replay."""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from makrograph.earnings_inflection.contracts import IST, FinancialMeasurement, IssuerModel, Metric, Scope, Unit
from makrograph.earnings_inflection.identity import IssuerRegistry, ReplayMode

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from build_fixtures import qend, quarter_pl, results_text  # noqa: E402

REV = {qend(2021, 6): 90, qend(2021, 9): 90, qend(2021, 12): 90, qend(2022, 3): 90, qend(2022, 6): 100, qend(2022, 9): 100, qend(2022, 12): 100, qend(2023, 3): 100,
       qend(2023, 6): 120, qend(2023, 9): 130, qend(2023, 12): 140}
PL = {d: quarter_pl(r, .12) for d, r in REV.items()}


def _doc(doc_id, ticker, q, published, **extra):
    return {"doc_id": doc_id, "ticker": ticker, "title": "Financial Results", "published_at": published,
            "text": results_text("Renamed Industries Limited", q, PL), **extra}


def _fixture(tmp_path, docs, identity=None, issuers=None):
    d = tmp_path / "fx"
    d.mkdir(exist_ok=True)
    (d / "docs.json").write_text(json.dumps({"documents": docs, "identity": identity or {},
                                             "issuers": issuers or {}}))
    return d


ALIASES = {"ISS1": {"name": "Renamed Industries Limited", "aliases": [
    {"alias": "OLDCO", "kind": "nse_symbol", "valid_from": "2015-01-01", "valid_to": "2023-06-30",
     "recorded_at": "2015-01-01T00:00:00+05:30"},
    {"alias": "NEWCO", "kind": "nse_symbol", "valid_from": "2023-07-01", "recorded_at": "2024-05-01T00:00:00+05:30"},
    {"alias": "543210", "kind": "bse_scrip", "valid_from": "2015-01-01", "recorded_at": "2015-01-01T00:00:00+05:30"},
]}}

DOCS = [
    _doc("A", "OLDCO", qend(2023, 3), "2023-05-20T16:00:00+05:30", first_seen_at="2023-05-21T00:00:00+05:30",
         text_available_at="2023-05-21T00:00:00+05:30"),
    _doc("B", "543210", qend(2023, 6), "2023-08-10T16:00:00+05:30", first_seen_at="2023-08-11T00:00:00+05:30",
         text_available_at="2023-08-11T00:00:00+05:30"),
    _doc("C", "NEWCO", qend(2023, 9), "2023-11-09T16:00:00+05:30", first_seen_at="2023-11-10T00:00:00+05:30",
         text_available_at="2023-11-10T00:00:00+05:30"),
    # an old filing ingested late (backfill): public in Feb 2024, first seen by MakroGraph in June 2024
    _doc("D", "NEWCO", qend(2023, 12), "2024-02-08T16:00:00+05:30", first_seen_at="2024-06-15T00:00:00+05:30",
         text_available_at="2024-06-15T00:00:00+05:30"),
]


def _run(d, ticker, as_of, mode=ReplayMode.RECONSTRUCTION, **cfg):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    res = EarningsInflectionPipeline({"replay_mode": mode, **cfg}, FixtureRepository(d)).run([ticker], as_of)
    assert not res.errors, res.errors
    return res.assessments[0], res


def _doc_ids(res):
    return sorted(s["doc_id"] for s in res.manifest["sources"][res.assessments[0].ticker])


def test_old_new_and_bse_aliases_recover_the_same_issuer_history(tmp_path):
    d = _fixture(tmp_path, DOCS, ALIASES)
    _, old = _run(d, "OLDCO", "2024-03-31")
    _, new = _run(d, "NEWCO", "2024-03-31")
    _, bse = _run(d, "543210", "2024-03-31")
    assert _doc_ids(old) == _doc_ids(new) == _doc_ids(bse) == ["A", "B", "C", "D"]
    assert old.assessments[0].coverage["identity_basis"] == "issuer_registry"


def test_reused_symbol_does_not_leak_another_issuers_documents(tmp_path):
    ident = dict(ALIASES)
    ident["ISS2"] = {"name": "Unrelated Later Company", "aliases": [
        {"alias": "OLDCO", "kind": "nse_symbol", "valid_from": "2023-12-01"}]}
    docs = DOCS + [_doc("X", "OLDCO", qend(2023, 12), "2024-02-01T16:00:00+05:30")]
    d = _fixture(tmp_path, docs, ident)
    a, res = _run(d, "NEWCO", "2024-03-31")
    assert "X" not in _doc_ids(res)                        # OLDCO belonged to ISS2 by Feb 2024
    a2, res2 = _run(d, "OLDCO", "2024-03-31")              # today OLDCO means ISS2
    assert _doc_ids(res2) == ["X"]
    a3, res3 = _run(d, "OLDCO", "2023-06-01")              # in mid-2023 OLDCO meant ISS1
    assert _doc_ids(res3) == ["A"]


def test_later_ingested_filing_in_reconstruction_but_not_system_replay(tmp_path):
    d = _fixture(tmp_path, DOCS, {**ALIASES, "ISS1": {**ALIASES["ISS1"], "aliases": [
        {**a, "recorded_at": "2015-01-01T00:00:00+05:30"} for a in ALIASES["ISS1"]["aliases"]]}})
    rec, rres = _run(d, "NEWCO", "2024-03-31", ReplayMode.RECONSTRUCTION)
    sys_, sres = _run(d, "NEWCO", "2024-03-31", ReplayMode.SYSTEM)
    assert "D" in _doc_ids(rres) and "D" not in _doc_ids(sres)
    assert sys_.coverage["system_replay_excluded_not_ingested"] == 1
    assert sys_.replay_mode == ReplayMode.SYSTEM and sres.manifest["replay_mode"] == ReplayMode.SYSTEM
    assert any("System-knowledge replay" in l for l in sys_.limitations)
    assert any("Public-information reconstruction" in l for l in rec.limitations)


def test_alias_mapping_learned_after_cutoff_is_not_used_in_system_replay(tmp_path):
    d = _fixture(tmp_path, DOCS, ALIASES)                   # NEWCO mapping recorded 2024-05-01
    a, res = _run(d, "NEWCO", "2024-03-31", ReplayMode.SYSTEM)
    assert a.coverage["identity_basis"] == "alias_mapping_not_known_by_cutoff"
    assert _doc_ids(res) == ["C"]                            # only filings held under NEWCO itself
    a, res = _run(d, "NEWCO", "2024-03-31", ReplayMode.RECONSTRUCTION)
    assert "A" in _doc_ids(res)


def test_legacy_text_without_provable_time_is_excluded_from_system_replay(tmp_path):
    docs = [dict(x) for x in DOCS]
    docs[0].pop("text_available_at")
    ident = {"ISS1": {**ALIASES["ISS1"], "aliases": [{**a, "recorded_at": "2015-01-01T00:00:00+05:30"}
                                                    for a in ALIASES["ISS1"]["aliases"]]}}
    a, _ = _run(_fixture(tmp_path, docs, ident), "NEWCO", "2024-03-31", ReplayMode.SYSTEM)
    assert a.coverage["system_replay_text_unproven"] == 1


def test_predecessor_history_needs_an_explicit_comparability_decision(tmp_path):
    base = {"ISS1": {"name": "Merged Co", "aliases": [{"alias": "NEWCO", "valid_from": "2022-04-01"}],
                     "predecessors": [{"predecessor_issuer_id": "PRED", "effective_date": "2022-04-01"}]},
            "PRED": {"name": "Pred Co", "aliases": [{"alias": "PREDCO", "valid_to": "2022-03-31"}]}}
    docs = [_doc("P1", "PREDCO", qend(2022, 12), "2023-02-10T16:00:00+05:30"),   # filed under old symbol
            _doc("P0", "PREDCO", qend(2022, 6), "2021-11-10T16:00:00+05:30"),
            _doc("N1", "NEWCO", qend(2023, 12), "2024-02-08T16:00:00+05:30")]
    a, res = _run(_fixture(tmp_path, docs, base), "NEWCO", "2024-03-31")
    assert _doc_ids(res) == ["N1"]
    assert any("predecessor PRED history excluded (no comparability decision" in l for l in a.limitations)
    base["ISS1"]["predecessors"][0].update(comparable=True, decision_source="analyst review 2024-01")
    a, res = _run(_fixture(tmp_path, docs, base), "NEWCO", "2024-03-31")
    assert _doc_ids(res) == ["N1", "P0"]          # only predecessor filings before the effective date


def test_present_day_metadata_is_context_only(tmp_path):
    d = _fixture(tmp_path, DOCS, ALIASES, issuers={"NEWCO": {"industry": "Private Sector Bank",
                                                              "series": "SM"}})
    a, _ = _run(d, "NEWCO", "2024-03-31")
    assert a.issuer_model == IssuerModel.OPERATING              # from the historical statements
    assert a.listing_segment.value == "unknown"                 # present-day series not used
    assert a.coverage["present_day_context"]["industry"] == "Private Sector Bank"
    assert "display only" in a.coverage["present_day_context"]["label"]


def _fm(end, v, pub, sys_t=None):
    return FinancialMeasurement("T", Metric.REVENUE, end, "Q", v, Unit.INR_CRORE, Scope.CONSOLIDATED, f"d{pub}",
                                datetime.fromisoformat(pub), system_available_at=sys_t)


def test_derived_signal_is_not_knowable_before_its_last_input():
    from makrograph.earnings_inflection.drivers import compute_drivers
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(qend(2023, 6), 100, "2023-08-10T16:00:00+05:30"),
            _fm(qend(2024, 6), 140, "2024-08-08T16:00:00+05:30",
                sys_t=datetime(2024, 8, 9, tzinfo=IST)),
            # year-ago value restated in a later filing: the signal needs that later number
            _fm(qend(2023, 6), 101, "2024-08-20T16:00:00+05:30", sys_t=datetime(2024, 8, 21, tzinfo=IST))]
    drivers, _ = compute_drivers(FinancialSeries.build("T", rows), [], [], IssuerModel.OPERATING, date(2024, 9, 30))
    g = next(x for x in drivers if x.driver == "revenue_yoy_growth")
    assert g.knowable_at == datetime.fromisoformat("2024-08-20T16:00:00+05:30")
    assert g.system_known_at == datetime(2024, 8, 21, tzinfo=IST)
    assert g.current == pytest.approx(38.61, abs=0.01)


def test_system_time_unknown_when_any_input_unproven():
    from makrograph.earnings_inflection.drivers import compute_drivers
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(qend(2023, 6), 100, "2023-08-10T16:00:00+05:30"),           # no system time (legacy)
            _fm(qend(2024, 6), 140, "2024-08-08T16:00:00+05:30", sys_t=datetime(2024, 8, 9, tzinfo=IST))]
    drivers, _ = compute_drivers(FinancialSeries.build("T", rows), [], [], IssuerModel.OPERATING, date(2024, 9, 30))
    g = next(x for x in drivers if x.driver == "revenue_yoy_growth")
    assert g.knowable_at == datetime.fromisoformat("2024-08-08T16:00:00+05:30") and g.system_known_at is None


def test_registry_ambiguity_requires_review():
    reg = IssuerRegistry.from_dict({
        "I1": {"aliases": [{"alias": "DUP", "valid_from": "2020-01-01"}]},
        "I2": {"aliases": [{"alias": "DUP", "valid_from": "2020-01-01"}]}})
    with pytest.raises(ValueError):
        reg.resolve("DUP", datetime(2024, 1, 1, tzinfo=IST))
