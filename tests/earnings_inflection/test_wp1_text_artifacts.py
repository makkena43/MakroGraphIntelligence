"""WP1 - close the ingestion-to-reader gap."""

import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from makrograph.earnings_inflection.contracts import IST
from makrograph.earnings_inflection.text_artifacts import (
    ExtractionStatus, FakeOcrProvider, TextArtifactStore, classify_pages, extract_document, sha256_file,
)

ROOT = Path(__file__).resolve().parents[2]
fitz = pytest.importorskip("fitz")


def make_pdf(path: Path, pages: list[str], encrypt: bool = False) -> Path:
    """Generate a PDF: each string is one page; "" gives a page with no text layer."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        y = 72
        for line in text.split("\n"):
            if line:
                page.insert_text((40, y), line, fontsize=8)
            y += 11
    kw = {}
    if encrypt:
        kw = dict(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.save(str(path), **kw)
    doc.close()
    return path


def parser(tmp_path, **cfg):
    from makrograph.parser.pdf_parser import PDFParser
    return PDFParser({"output_dir": str(tmp_path / "parsed"), **cfg})


RESULTS_PAGE = "\n".join([
    "Statement of Unaudited Standalone Financial Results for the quarter ended 30 June 2024",
    "(Rs. in crore)",
    "Particulars Quarter ended Year ended",
    "30.06.2024 31.03.2024 30.06.2023 31.03.2024",
    "1. Revenue from operations 270.00 260.00 230.00 950.00",
])


# ---------------- store: versioning, immutability, completeness ----------------

def test_artifacts_are_versioned_and_never_overwritten(tmp_path):
    store = TextArtifactStore(tmp_path / "a")
    orig = tmp_path / "x.pdf"
    orig.write_bytes(b"%PDF-1.4 original")
    v1 = store.record_extraction(doc_id="9", method="pdfplumber", parser_version="p1", pages=["old text"],
                                 original_path=orig)
    again = store.record_extraction(doc_id="9", method="pdfplumber", parser_version="p1", pages=["DIFFERENT"],
                                    original_path=orig)
    assert again.version_id == v1.version_id and store.read_pages(again) == ["old text"]   # immutable
    v2 = store.record_extraction(doc_id="9", method="pdfplumber", parser_version="p2", pages=["new text"],
                                 original_path=orig)
    assert v2.version_id != v1.version_id and len(store.versions("9")) == 2
    assert store.read_pages(store.select("9", pinned_version=v1.version_id)) == ["old text"]
    assert store.read_pages(store.select("9")) == ["new text"]
    assert (tmp_path / "a" / v1.original_ref).read_bytes() == b"%PDF-1.4 original"         # original kept


def test_tampered_artifact_is_rejected(tmp_path):
    store = TextArtifactStore(tmp_path)
    v = store.record_extraction(doc_id="1", method="m", parser_version="p", pages=["a"], raw=b"r")
    (tmp_path / "docs" / "1" / v.version_id / "text.txt").write_text("edited")
    with pytest.raises(ValueError):
        store.read_pages(v)


def test_completeness_statuses():
    assert classify_pages(["a", "b"], 2, False)[0] == ExtractionStatus.COMPLETE
    assert classify_pages(["a", ""], 2, False)[0] == ExtractionStatus.PARTIAL
    assert classify_pages(["a"], 5, True)[0] == ExtractionStatus.PARTIAL
    assert classify_pages(["", ""], 2, False)[0] == ExtractionStatus.OCR_REQUIRED
    assert classify_pages([], 0, False)[0] == ExtractionStatus.EMPTY


# ---------------- extraction runner on real PDFs ----------------

def test_text_pdf_extracts_completely_with_pages(tmp_path):
    pdf = make_pdf(tmp_path / "r.pdf", [RESULTS_PAGE, "Notes: reviewed by the audit committee."])
    store = TextArtifactStore(tmp_path / "a")
    res = extract_document(store, "100", pdf, parser(tmp_path))
    assert res.status == ExtractionStatus.COMPLETE and res.pages_expected == 2 and res.pages_with_text == 2
    pages = store.read_pages(res)
    assert "Revenue from operations" in pages[0] and "audit committee" in pages[1]


def test_scanned_pdf_queues_for_ocr_and_keeps_original_then_fake_ocr_recovers(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["", ""])
    store = TextArtifactStore(tmp_path / "a")
    res = extract_document(store, "200", pdf, parser(tmp_path))
    assert res.status == ExtractionStatus.OCR_REQUIRED and res.status.retryable
    assert (tmp_path / "a" / res.original_ref).exists()                       # recoverable
    assert store.select("200") is None
    ocr = FakeOcrProvider({sha256_file(pdf): [RESULTS_PAGE, "page two"]})
    res2 = extract_document(store, "200", pdf, parser(tmp_path), ocr=ocr)
    assert res2.status == ExtractionStatus.COMPLETE and res2.method == "ocr:fake"
    assert "text produced by OCR" in res2.quality_issues


def test_network_ocr_provider_refused_by_default(tmp_path):
    class Remote(FakeOcrProvider):
        name, version, network = "remote", "r1", True
    pdf = make_pdf(tmp_path / "scan.pdf", [""])
    with pytest.raises(PermissionError):
        extract_document(TextArtifactStore(tmp_path / "a"), "1", pdf, parser(tmp_path),
                         ocr=Remote({sha256_file(pdf): ["x"]}))


def test_encrypted_pdf_is_labelled(tmp_path):
    pdf = make_pdf(tmp_path / "enc.pdf", [RESULTS_PAGE], encrypt=True)
    res = extract_document(TextArtifactStore(tmp_path / "a"), "300", pdf, parser(tmp_path))
    assert res.status == ExtractionStatus.ENCRYPTED


def test_max_page_truncation_is_partial(tmp_path):
    pdf = make_pdf(tmp_path / "long.pdf", [RESULTS_PAGE, "p2", "p3"])
    res = extract_document(TextArtifactStore(tmp_path / "a"), "400", pdf, parser(tmp_path, max_pages=2))
    assert res.status == ExtractionStatus.PARTIAL and res.truncated and res.pages_processed == 2
    assert any("processed 2 of 3" in i for i in res.quality_issues)


def test_missing_original_and_retry_limit(tmp_path):
    store = TextArtifactStore(tmp_path / "a")
    assert extract_document(store, "500", tmp_path / "nope.pdf", None).status == ExtractionStatus.MISSING_ORIGINAL

    class Crashing:
        parser_version, primary_engine = "crash-1", "crash"

        def parse(self, path):
            raise RuntimeError("boom")
    pdf = make_pdf(tmp_path / "c.pdf", [RESULTS_PAGE])
    for _ in range(5):
        r = extract_document(store, "600", pdf, Crashing(), max_retries=2)
    assert r.status == ExtractionStatus.PARSE_FAILED_RETRYABLE
    assert len(store.attempts("600")) == 3                                      # stopped retrying


# ---------------- reader + pipeline ----------------

def _fixture_dir(tmp_path, pdf_name="R1.pdf"):
    d = tmp_path / "fx"
    d.mkdir()
    make_pdf(d / pdf_name, [RESULTS_PAGE])
    (d / "docs.json").write_text(json.dumps({"issuers": {"PDFCO": {"name": "Pdf Co Limited",
                                                                   "industry": "Engineering"}},
                                             "documents": [{"doc_id": "R1", "ticker": "PDFCO",
                                                            "title": "Outcome of Board Meeting",
                                                            "published_at": "2024-08-08T17:00:00+05:30",
                                                            "local_path": pdf_name}]}))
    return d


def test_pdf_only_fixture_row_unreadable_until_extracted_then_parsed(tmp_path):
    from makrograph.earnings_inflection.backfill import extract_missing_text
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    store = TextArtifactStore(tmp_path / "a")
    repo = FixtureRepository(_fixture_dir(tmp_path), artifact_store=store)
    a = EarningsInflectionPipeline({}, repo).run(["PDFCO"], "2024-09-30").assessments[0]
    assert a.coverage["unreadable_documents"][0]["status"] == "NOT_EXTRACTED"
    assert any("no readable text" in l for l in a.limitations)
    assert a.coverage["text_sources"] == {"none": 1}

    report = extract_missing_text(repo, ["PDFCO"], "IN", datetime(2024, 9, 30, tzinfo=IST), store,
                                  parser(tmp_path))
    assert report["by_status"] == {"COMPLETE": 1} and report["deferred"] == []
    res = EarningsInflectionPipeline({}, repo).run(["PDFCO"], "2024-09-30")
    a2 = res.assessments[0]
    assert a2.coverage["text_sources"] == {"artifact": 1} and a2.coverage["financial_rows"] > 0
    src = res.manifest["sources"]["PDFCO"][0]
    assert src["extraction_version_id"] and src["text_source"].startswith("artifact:")


def test_extract_is_bounded_and_reports_deferrals(tmp_path):
    from makrograph.earnings_inflection.backfill import extract_missing_text
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    d = _fixture_dir(tmp_path)
    data = json.loads((d / "docs.json").read_text())
    make_pdf(d / "R2.pdf", [RESULTS_PAGE])
    data["documents"].append({**data["documents"][0], "doc_id": "R2", "local_path": "R2.pdf",
                              "published_at": "2024-08-09T17:00:00+05:30"})
    (d / "docs.json").write_text(json.dumps(data))
    store = TextArtifactStore(tmp_path / "a")
    rep = extract_missing_text(FixtureRepository(d, artifact_store=store), ["PDFCO"], "IN",
                               datetime(2024, 9, 30, tzinfo=IST), store, parser(tmp_path), max_docs=1)
    assert rep["processed"] == 1 and rep["deferred"] == [{"ticker": "PDFCO", "doc_id": "R2"}]


def test_replay_reads_the_pinned_parser_version(tmp_path):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.source_repository import FixtureRepository
    d = _fixture_dir(tmp_path)
    store = TextArtifactStore(tmp_path / "a")
    v1 = store.record_extraction(doc_id="R1", method="pdfplumber", parser_version="old",
                                 pages=[RESULTS_PAGE], original_path=d / "R1.pdf")
    first = EarningsInflectionPipeline({}, FixtureRepository(d, artifact_store=store)).run(["PDFCO"], "2024-09-30")
    store.record_extraction(doc_id="R1", method="pdfplumber", parser_version="new",
                            pages=[RESULTS_PAGE.replace("270.00", "999.00")], original_path=d / "R1.pdf")
    pins = EarningsInflectionPipeline.pins_from_manifest(first.manifest)
    assert pins == {"R1": v1.version_id}
    replay = EarningsInflectionPipeline({"pinned_extractions": pins},
                                        FixtureRepository(d, artifact_store=store)).run(["PDFCO"], "2024-09-30")
    latest = EarningsInflectionPipeline({}, FixtureRepository(d, artifact_store=store)).run(["PDFCO"], "2024-09-30")
    assert replay.manifest["sources"]["PDFCO"][0]["text_hash"] == first.manifest["sources"]["PDFCO"][0]["text_hash"]
    assert latest.manifest["sources"]["PDFCO"][0]["text_hash"] != first.manifest["sources"]["PDFCO"][0]["text_hash"]


def test_missing_expected_result_periods_are_reported(repo):
    from makrograph.earnings_inflection.coverage import expected_result_periods
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    assert expected_result_periods(date(2024, 3, 31), "Q", 4) == [date(2023, 3, 31), date(2023, 6, 30),
                                                                   date(2023, 9, 30), date(2023, 12, 31)]
    assert expected_result_periods(date(2024, 11, 1), "H", 2) == [date(2023, 9, 30), date(2024, 3, 31)]
    res = EarningsInflectionPipeline({}, repo).run(["CONTRACO"], "2024-10-31")
    a = res.assessments[0]
    assert "2024-06-30" in a.coverage["result_periods"]["missing"]
    assert res.status == "PARTIAL"
    assert any(l.startswith("Results not parsed for") for l in a.limitations)


def test_cli_extract_and_manifest(tmp_path):
    d = _fixture_dir(tmp_path)
    art, out = tmp_path / "a", tmp_path / "out"
    base = [sys.executable, str(ROOT / "scripts/earnings_inflection.py"), "--fixtures", str(d),
            "--ticker", "PDFCO", "--as-of", "2024-09-30", "--artifact-root", str(art)]
    r = subprocess.run(base + ["--extract"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["by_status"] == {"COMPLETE": 1}
    r = subprocess.run(base + ["--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    m = json.loads((out / "manifest.json").read_text())
    assert m["schema_version"] == "ei-run-manifest-1" and m["sources"]["PDFCO"][0]["extraction_version_id"]
    r = subprocess.run(base + ["--out", str(tmp_path / "out2"), "--replay-manifest", str(out / "manifest.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------- ingestion: opt-in artifact writing, equivalence, failure states ----------------

class _Cur:
    def __init__(self, st):
        self.st = st

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.st.log.append((sql, params))
        self._rows = []
        if sql.strip().startswith("SELECT") and not self.st.served:
            self.st.served = True
            self._rows = [dict(self.st.row)]

    def fetchall(self):
        return self._rows


class _Store:
    _cursor_factory = None

    def __init__(self, row):
        self.row, self.log, self.served = row, [], False

    @contextmanager
    def _conn(self):
        st = self

        class C:
            def cursor(self, cursor_factory=None):
                return _Cur(st)
        yield C()


def _ingest(tmp_path, tag, pages, source_pdf=None, **kw):
    from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
    root = tmp_path / tag
    pdf_dir = root / "data" / "india" / "pdfs"
    pdf_dir.mkdir(parents=True)
    if source_pdf:                                            # same original bytes for both runs
        shutil.copyfile(source_pdf, pdf_dir / "ACME_7_r.pdf")
    else:
        make_pdf(pdf_dir / "ACME_7_r.pdf", pages)              # already downloaded: no network
    p = IntelligencePipeline.__new__(IntelligencePipeline)
    p._pg_store = _Store({"id": 7, "ticker": "ACME", "url": "https://example.invalid/r.pdf",
                          "filing_type": "Financial Result Updates", "title": "Results"})
    p.config = {"parser": {"output_dir": str(root / "parsed")}, "storage": {"project_root": str(root)}}
    p.run_pdf_fetch_india(**kw)
    return p._pg_store.log, pdf_dir / "ACME_7_r.pdf"


def test_live_and_historical_ingestion_write_identical_artifacts(tmp_path):
    art_live, art_hist = tmp_path / "art_live", tmp_path / "art_hist"
    src = make_pdf(tmp_path / "source.pdf", [RESULTS_PAGE])
    _ingest(tmp_path, "live", None, source_pdf=src, text_artifact_root=str(art_live))
    _, pdf = _ingest(tmp_path, "hist", None, source_pdf=src, text_artifact_root=str(art_hist),
                     store_text_to_db=True, delete_after_parse=True)
    a, b = TextArtifactStore(art_live).select("7"), TextArtifactStore(art_hist).select("7")
    assert a and b and a.text_hash == b.text_hash and a.version_id == b.version_id
    assert not pdf.exists()                                     # historical mode deleted its download ...
    assert (art_hist / b.original_ref).exists()                 # ... but the original is preserved


def test_ingestion_defaults_unchanged_for_scanned_pdf(tmp_path):
    log, pdf = _ingest(tmp_path, "default", [""])
    upd = [p for s, p in log if s.strip().startswith("UPDATE")]
    assert upd and upd[0][:2] == ("UNSUPPORTED_FORMAT", "unsupported") and not pdf.exists()


def test_ingestion_opt_in_keeps_scanned_original_with_recoverable_status(tmp_path):
    log, pdf = _ingest(tmp_path, "granular", [""], granular_failure_status=True, keep_failed_originals=True,
                       text_artifact_root=str(tmp_path / "art"))
    upd = [p for s, p in log if s.strip().startswith("UPDATE")]
    assert upd[0][1] == "ocr_required" and pdf.exists()
    sel = [s for s, _ in log if s.strip().startswith("SELECT")][0]
    assert "'ocr_required'" in sel                              # not re-queued in a loop
    assert TextArtifactStore(tmp_path / "art").latest_status("7").status == ExtractionStatus.OCR_REQUIRED
