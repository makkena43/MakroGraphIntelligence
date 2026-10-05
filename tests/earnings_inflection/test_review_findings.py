"""Regression tests for the three offline review findings (2026-10-05 amendment, §2).

Written before the fixes:
1. provisional orders could qualify a company as COMMITMENT_BACKED;
2. utilisation figures from different plants were compared with each other;
3. PDF-only source rows produced no text through the read-only reader.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from makrograph.earnings_inflection.contracts import (
    IST, CommitmentStrength, EconomicEvent, EvidenceStatus, Metric, Quantity, SourceDocument, Unit,
)


# 1 ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="fixed in WP4")
def test_provisional_orders_do_not_make_a_verified_commitment():
    from makrograph.earnings_inflection.assessments import decide_status
    l1 = EconomicEvent("e1", "T", Metric.ORDER_WIN, Quantity(900.0, Unit.INR_CRORE), "State Power Utility Limited",
                       datetime(2024, 2, 1, tzinfo=IST), commitment_strength=CommitmentStrength.PROVISIONAL)
    status, why = decide_status([], [l1], [], [], 1, 1000.0, datetime(2024, 3, 31, tzinfo=IST))
    assert status != EvidenceStatus.COMMITMENT_BACKED
    # still visible, in the early (unverified) lane, with its limitation stated
    assert status == EvidenceStatus.EARLY_COMMITMENT_UNVERIFIED
    assert any("provisional" in w.lower() for w in why)


# 2 ---------------------------------------------------------------------------

def _doc(doc_id, text, when):
    d = SourceDocument(doc_id=doc_id, source_name="t", ticker="T", text=text, published_at=when)
    d.available_at = when
    return d


def _util_drivers(texts):
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.drivers import compute_drivers
    from makrograph.earnings_inflection.extraction import extract_sentence_evidence
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    from makrograph.earnings_inflection.contracts import IssuerModel
    ev = []
    for i, (t, when) in enumerate(texts):
        d = _doc(f"D{i}", t, when)
        ev += extract_sentence_evidence(d, chunk_document(d))
    drivers, _ = compute_drivers(FinancialSeries.build("T", []), [], ev, IssuerModel.OPERATING, date(2024, 9, 30))
    return [d for d in drivers if d.driver == "capacity_utilization_change"]


@pytest.mark.xfail(strict=True, reason="fixed in WP3")
def test_utilisation_of_different_plants_is_not_compared():
    got = _util_drivers([
        ("Capacity utilisation at the Hosur plant was 62% in FY24.", datetime(2024, 5, 20, tzinfo=IST)),
        ("Capacity utilisation at the Pune plant was 93% in Q1FY25.", datetime(2024, 8, 12, tzinfo=IST)),
    ])
    assert got == []


@pytest.mark.xfail(strict=True, reason="fixed in WP3")
def test_utilisation_of_the_same_plant_is_compared():
    got = _util_drivers([
        ("Capacity utilisation at the Hosur plant was 62% in FY24.", datetime(2024, 5, 20, tzinfo=IST)),
        ("Capacity utilisation at the Hosur plant was 81% in Q1FY25.", datetime(2024, 8, 12, tzinfo=IST)),
    ])
    assert len(got) == 1 and got[0].change == 19.0 and "hosur" in got[0].basis.lower()


# 3 ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, [("x",)], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("SHOW"):
            self._rows = [("on",)]
        elif "information_schema.columns" in sql:
            self.description = [("column_name",)]
            self._rows = [(c,) for c in ("id", "ticker", "country", "filed_at", "published_at", "title",
                                         "local_path", "content_hash", "created_at")]
        elif "FROM mg_documents WHERE" in sql:
            names = [c.split(" AS ")[-1].strip() for c in sql.split("SELECT ")[1].split(" FROM")[0].split(", ")]
            self.description = [(n,) for n in names]
            rows = [r for r in self.conn.docs if r["id"] > params[-2]] if "id > %s" in sql else self.conn.docs
            self._rows = [tuple(r.get(n) for n in names) for r in rows[: params[-1]]]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, docs):
        self.docs = docs

    def set_session(self, **kw):
        pass

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        pass

    def close(self):
        pass


def test_pdf_only_row_is_readable_after_explicit_extraction(tmp_path):
    from makrograph.earnings_inflection.source_repository import PostgresReadOnlyRepository
    from makrograph.earnings_inflection.text_artifacts import ExtractionStatus, TextArtifactStore
    pdf = tmp_path / "PDFONLY_1_results.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake bytes for hashing only")
    row = {"id": 1, "ticker": "PDFONLY", "country": "IN", "filed_at": date(2024, 2, 8), "title": "Results",
           "local_path": str(pdf), "created_at": datetime(2024, 2, 9, tzinfo=IST)}
    store = TextArtifactStore(tmp_path / "artifacts")

    repo = PostgresReadOnlyRepository(dsn="x", connect=lambda *a, **k: _Conn([row]), artifact_store=store)
    before = repo.documents("PDFONLY", "IN", datetime(2024, 3, 31, tzinfo=IST))[0]
    assert before.text is None and before.extraction_status == ExtractionStatus.NOT_EXTRACTED.value

    # explicit, bounded extraction step (here with a fake parser; no network)
    store.record_extraction(doc_id="1", original_path=pdf, method="fake",
                            parser_version="fake-1", pages=["Statement of Unaudited Financial Results"],
                            pages_expected=1)
    after = repo.documents("PDFONLY", "IN", datetime(2024, 3, 31, tzinfo=IST))[0]
    assert "Financial Results" in after.full_text()
    assert after.text_source.startswith("artifact:")
