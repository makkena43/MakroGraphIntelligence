"""Versioned, immutable text artifacts and extraction results (WP1).

Closes the ingestion-to-reader gap: a PDF that was downloaded but whose text
never reached ``mg_documents.raw_text`` (live mode stores only
``local_path``) is unreadable to the detector.  This module stores the
extracted text of each source document as an immutable artifact keyed by

    (document id, raw-byte hash, extraction method, parser version)

so that

* re-extraction with a new parser/OCR version creates a NEW version and never
  overwrites the text an earlier run used (replays stay reproducible);
* the original file is preserved (copied by content hash) even when parsing
  fails; nothing here deletes originals;
* failures are recorded with explicit, recoverable states and retry counts
  instead of a permanent "unsupported";
* completeness is explicit: pages expected / processed / with text and
  max-page truncation, so non-empty text is never mistaken for a complete
  extraction.

Layout (local filesystem, under an explicitly configured root)::

    <root>/originals/<raw_sha256><suffix>
    <root>/docs/<doc_key>/<version_id>/text.txt        pages joined by "\\f"
    <root>/docs/<doc_key>/<version_id>/extraction.json  ExtractionResult
    <root>/docs/<doc_key>/attempts.jsonl                append-only failures

Nothing in this module performs network access.  Extraction runs only when
explicitly invoked (``extract_document`` / the earnings-inflection
``--extract`` CLI mode, or the ingestion pipeline's opt-in
``text_artifact_root`` option).  Shared by ingestion and the research reader,
so it lives in ``makrograph.parser``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Protocol

ARTIFACT_SCHEMA_VERSION = "ei-text-artifact-1"
PAGE_SEPARATOR = "\f"


class ExtractionStatus(str, Enum):
    COMPLETE = "COMPLETE"                       # every page processed, every page had text
    PARTIAL = "PARTIAL"                         # truncated, or some pages without text
    OCR_REQUIRED = "OCR_REQUIRED"               # pages exist but no text layer (scan)
    PARSE_FAILED_RETRYABLE = "PARSE_FAILED_RETRYABLE"
    ENCRYPTED = "ENCRYPTED"
    EMPTY = "EMPTY"                             # zero pages / zero-byte content
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"   # e.g. ZIP without a PDF, Excel
    MISSING_ORIGINAL = "MISSING_ORIGINAL"       # original file not available locally
    NOT_EXTRACTED = "NOT_EXTRACTED"             # reader view: original exists, never extracted
    LEGACY_UNVERSIONED = "LEGACY_UNVERSIONED"   # reader view: legacy raw_text / .txt, no version history

    @property
    def usable(self) -> bool:
        return self in (ExtractionStatus.COMPLETE, ExtractionStatus.PARTIAL, ExtractionStatus.LEGACY_UNVERSIONED)

    @property
    def retryable(self) -> bool:
        return self in (ExtractionStatus.PARSE_FAILED_RETRYABLE, ExtractionStatus.OCR_REQUIRED)


@dataclass
class ExtractionResult:
    version_id: str
    doc_id: str
    status: ExtractionStatus
    method: str                       # "pdfplumber" | "pymupdf" | "ocr:<provider>" | "legacy_txt" | "fake"
    parser_version: str
    raw_hash: str
    text_hash: str = ""
    pages_expected: Optional[int] = None
    pages_processed: int = 0
    pages_with_text: int = 0
    truncated: bool = False
    max_pages: Optional[int] = None
    quality_issues: list[str] = field(default_factory=list)
    retry_count: int = 0
    failure_reason: str = ""
    extracted_at: Optional[datetime] = None
    original_ref: str = ""            # path of the preserved original inside the store
    schema_version: str = ARTIFACT_SCHEMA_VERSION

    @property
    def complete(self) -> bool:
        return self.status == ExtractionStatus.COMPLETE

    def to_json(self) -> dict:
        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        d["extracted_at"] = self.extracted_at.isoformat() if self.extracted_at else None
        return d

    @classmethod
    def from_json(cls, d: dict) -> "ExtractionResult":
        d = dict(d)
        d["status"] = ExtractionStatus(d["status"])
        d["extracted_at"] = datetime.fromisoformat(d["extracted_at"]) if d.get("extracted_at") else None
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown fields in extraction record: {sorted(unknown)}")
        return cls(**d)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _doc_key(doc_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(doc_id))[:120]


def classify_pages(pages: list[str], pages_expected: Optional[int], truncated: bool
                   ) -> tuple[ExtractionStatus, list[str]]:
    """Derive the completeness status from per-page text."""
    issues: list[str] = []
    with_text = sum(1 for p in pages if p.strip())
    if (pages_expected or 0) == 0 and not pages:
        return ExtractionStatus.EMPTY, ["document has no pages"]
    if with_text == 0:
        return ExtractionStatus.OCR_REQUIRED, ["no text layer on any page (scanned or image-only)"]
    status = ExtractionStatus.COMPLETE
    if truncated or (pages_expected is not None and len(pages) < pages_expected):
        status = ExtractionStatus.PARTIAL
        issues.append(f"processed {len(pages)} of {pages_expected} pages (max-page limit or truncation)")
    blank = len(pages) - with_text
    if blank:
        status = ExtractionStatus.PARTIAL
        issues.append(f"{blank} page(s) without a text layer (may need OCR)")
    return status, issues


class TextArtifactStore:
    """Filesystem store of immutable, versioned extraction artifacts."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    # -- paths ---------------------------------------------------------------

    def _doc_dir(self, doc_id: str) -> Path:
        return self.root / "docs" / _doc_key(doc_id)

    def _preserve_original(self, original_path: Optional[Path], raw: Optional[bytes]) -> tuple[str, str]:
        """Copy the original into the store by content hash (never moved or deleted)."""
        if raw is None and (original_path is None or not Path(original_path).exists()):
            return "", ""
        raw_hash = sha256_bytes(raw) if raw is not None else sha256_file(Path(original_path))
        suffix = Path(original_path).suffix.lower() if original_path else ".bin"
        dest = self.root / "originals" / f"{raw_hash}{suffix}"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            if raw is not None:
                tmp.write_bytes(raw)
            else:
                shutil.copyfile(original_path, tmp)
            tmp.replace(dest)
        return raw_hash, str(dest.relative_to(self.root))

    @staticmethod
    def version_id(raw_hash: str, method: str, parser_version: str) -> str:
        return hashlib.sha256(f"{raw_hash}|{method}|{parser_version}".encode()).hexdigest()[:20]

    # -- writes ----------------------------------------------------------------

    def record_extraction(self, *, doc_id: str, method: str, parser_version: str, pages: list[str],
                          original_path: Optional[os.PathLike] = None, raw: Optional[bytes] = None,
                          pages_expected: Optional[int] = None, truncated: bool = False,
                          max_pages: Optional[int] = None, quality_issues: Iterable[str] = (),
                          extracted_at: Optional[datetime] = None) -> ExtractionResult:
        """Store a successful (complete or partial) extraction; idempotent and immutable."""
        raw_hash, original_ref = self._preserve_original(Path(original_path) if original_path else None, raw)
        if not raw_hash:
            raw_hash = text_hash(PAGE_SEPARATOR.join(pages))      # text-only source (e.g. legacy .txt)
        vid = self.version_id(raw_hash, method, parser_version)
        vdir = self._doc_dir(doc_id) / vid
        meta = vdir / "extraction.json"
        if meta.exists():                                         # immutable: never overwrite a version
            return ExtractionResult.from_json(json.loads(meta.read_text()))
        status, issues = classify_pages(pages, pages_expected if pages_expected is not None else len(pages),
                                        truncated)
        if not status.usable:
            return self.record_failure(doc_id=doc_id, method=method, parser_version=parser_version,
                                       status=status, reason="; ".join(issues), original_path=original_path,
                                       raw=raw, pages_expected=pages_expected)
        text = PAGE_SEPARATOR.join(pages)
        res = ExtractionResult(
            version_id=vid, doc_id=str(doc_id), status=status, method=method, parser_version=parser_version,
            raw_hash=raw_hash, text_hash=text_hash(text),
            pages_expected=pages_expected if pages_expected is not None else len(pages),
            pages_processed=len(pages), pages_with_text=sum(1 for p in pages if p.strip()),
            truncated=truncated, max_pages=max_pages, quality_issues=list(quality_issues) + issues,
            retry_count=len(self.attempts(doc_id, method, parser_version)),
            extracted_at=extracted_at or datetime.now(timezone.utc), original_ref=original_ref,
        )
        tmp = vdir.with_name(vdir.name + ".tmp")
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "text.txt").write_text(text, encoding="utf-8")
        (tmp / "extraction.json").write_text(json.dumps(res.to_json(), indent=1))
        tmp.replace(vdir)
        return res

    def record_failure(self, *, doc_id: str, method: str, parser_version: str, status: ExtractionStatus,
                       reason: str, original_path: Optional[os.PathLike] = None, raw: Optional[bytes] = None,
                       pages_expected: Optional[int] = None) -> ExtractionResult:
        """Append a failed attempt; the original is preserved for a later retry/OCR."""
        if status.usable:
            raise ValueError("record_failure needs a failure status")
        raw_hash, original_ref = self._preserve_original(Path(original_path) if original_path else None, raw)
        prior = self.attempts(doc_id, method, parser_version)
        res = ExtractionResult(
            version_id=self.version_id(raw_hash or "none", method, parser_version), doc_id=str(doc_id),
            status=status, method=method, parser_version=parser_version, raw_hash=raw_hash,
            pages_expected=pages_expected, retry_count=len(prior), failure_reason=reason,
            extracted_at=datetime.now(timezone.utc), original_ref=original_ref,
        )
        d = self._doc_dir(doc_id)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "attempts.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(res.to_json()) + "\n")
        return res

    # -- reads -----------------------------------------------------------------

    def attempts(self, doc_id: str, method: Optional[str] = None,
                 parser_version: Optional[str] = None) -> list[ExtractionResult]:
        p = self._doc_dir(doc_id) / "attempts.jsonl"
        if not p.exists():
            return []
        out = [ExtractionResult.from_json(json.loads(l)) for l in p.read_text().splitlines() if l.strip()]
        return [r for r in out if (method is None or r.method == method)
                and (parser_version is None or r.parser_version == parser_version)]

    def versions(self, doc_id: str) -> list[ExtractionResult]:
        d = self._doc_dir(doc_id)
        if not d.exists():
            return []
        out = []
        for meta in d.glob("*/extraction.json"):
            out.append(ExtractionResult.from_json(json.loads(meta.read_text())))
        return sorted(out, key=lambda r: (r.extracted_at or datetime.min.replace(tzinfo=timezone.utc), r.version_id))

    def latest_status(self, doc_id: str) -> Optional[ExtractionResult]:
        """Most recent outcome (success or failure) for coverage reporting."""
        allr = self.versions(doc_id) + self.attempts(doc_id)
        return max(allr, key=lambda r: r.extracted_at) if allr else None

    def select(self, doc_id: str, *, pinned_version: Optional[str] = None,
               extracted_by: Optional[datetime] = None) -> Optional[ExtractionResult]:
        """The artifact to read: a pinned version (replay of an earlier run), else
        the latest usable version extracted no later than ``extracted_by``."""
        vs = self.versions(doc_id)
        if pinned_version:
            return next((v for v in vs if v.version_id == pinned_version), None)
        if extracted_by is not None:
            vs = [v for v in vs if v.extracted_at and v.extracted_at <= extracted_by]
        return vs[-1] if vs else None

    def read_pages(self, res: ExtractionResult) -> list[str]:
        p = self._doc_dir(res.doc_id) / res.version_id / "text.txt"
        text = p.read_text(encoding="utf-8")
        if text_hash(text) != res.text_hash:
            raise ValueError(f"artifact {res.version_id} for {res.doc_id} failed its integrity check")
        return text.split(PAGE_SEPARATOR)


# ---------------------------------------------------------------------------
# Extraction runner (explicitly invoked; local files only)
# ---------------------------------------------------------------------------

class OcrProvider(Protocol):
    name: str
    version: str
    network: bool                     # providers that send data off-machine are refused unless authorised

    def ocr(self, path: Path) -> list[str]: ...


class FakeOcrProvider:
    """Offline test provider: returns configured page texts by original file hash."""
    name, version, network = "fake", "fake-ocr-1", False

    def __init__(self, pages_by_hash: dict[str, list[str]]):
        self._pages = pages_by_hash

    def ocr(self, path: Path) -> list[str]:
        h = sha256_file(path)
        if h not in self._pages:
            raise RuntimeError("fake OCR has no text for this file")
        return self._pages[h]


class LocalOcrmypdfProvider:
    """Local OCR via the ``ocrmypdf`` command (no network, no cost).  Disabled
    unless explicitly selected; requires ocrmypdf + tesseract installed."""
    name, network = "ocrmypdf", False

    def __init__(self, parser):
        import subprocess
        self._sp = subprocess
        self._parser = parser
        if not shutil.which("ocrmypdf"):
            raise RuntimeError("ocrmypdf is not installed")
        self.version = self._sp.run(["ocrmypdf", "--version"], capture_output=True, text=True).stdout.strip()

    def ocr(self, path: Path) -> list[str]:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "ocr.pdf"
            r = self._sp.run(["ocrmypdf", "--skip-text", "--quiet", str(path), str(out)], capture_output=True)
            if r.returncode != 0:
                raise RuntimeError(f"ocrmypdf failed: {r.stderr[:200]!r}")
            return self._parser.parse(out).pages


def record_parse_result(store: TextArtifactStore, doc_id: str, path: Path, res, parser_version: str
                        ) -> ExtractionResult:
    """Record an already-computed ``ParseResult`` (no re-parse).  Shared by the
    ingestion pipeline (live and historical modes) and the explicit extractor,
    so both paths produce identical artifacts for the same original."""
    method = res.engine_used or "pdf"
    kind = getattr(res, "failure_kind", "")
    if kind == "encrypted":
        return store.record_failure(doc_id=doc_id, method=method, parser_version=parser_version,
                                    status=ExtractionStatus.ENCRYPTED, reason=res.error or "encrypted",
                                    original_path=path)
    if kind in ("not_pdf",):
        return store.record_failure(doc_id=doc_id, method=method, parser_version=parser_version,
                                    status=ExtractionStatus.UNSUPPORTED_FORMAT, reason=res.error or kind,
                                    original_path=path)
    if kind == "error" and not any(p.strip() for p in res.pages):
        return store.record_failure(doc_id=doc_id, method=method, parser_version=parser_version,
                                    status=ExtractionStatus.PARSE_FAILED_RETRYABLE,
                                    reason=res.error or "parser error", original_path=path)
    status, _ = classify_pages(res.pages, res.pages_expected, res.truncated)
    if not status.usable:
        return store.record_failure(doc_id=doc_id, method=method, parser_version=parser_version,
                                    status=status, reason="no text layer; queued for OCR"
                                    if status == ExtractionStatus.OCR_REQUIRED else status.value,
                                    original_path=path, pages_expected=res.pages_expected)
    return store.record_extraction(doc_id=doc_id, method=method, parser_version=parser_version,
                                   pages=res.pages, original_path=path, pages_expected=res.pages_expected,
                                   truncated=res.truncated, max_pages=getattr(res, "max_pages", None))


INGESTION_STATUS = {      # processing_status written by opt-in granular ingestion handling
    ExtractionStatus.OCR_REQUIRED: "ocr_required",
    ExtractionStatus.ENCRYPTED: "encrypted",
    ExtractionStatus.PARSE_FAILED_RETRYABLE: "parse_failed",
    ExtractionStatus.EMPTY: "empty",
    ExtractionStatus.UNSUPPORTED_FORMAT: "unsupported",
    ExtractionStatus.MISSING_ORIGINAL: "missing_original",
}


def extract_document(store: TextArtifactStore, doc_id: str, path: Optional[os.PathLike], parser,
                     ocr: Optional[OcrProvider] = None, max_retries: int = 2,
                     allow_network_ocr: bool = False) -> ExtractionResult:
    """Extract one local original into the store.

    ``parser`` must offer ``parse(path) -> ParseResult`` with ``pages``,
    ``pages_expected``, ``truncated``, ``failure_kind``, ``engine_used`` and
    ``parser_version`` (see ``makrograph.parser.pdf_parser``).
    """
    if path is None or not Path(path).exists():
        return store.record_failure(doc_id=doc_id, method="none", parser_version="none",
                                    status=ExtractionStatus.MISSING_ORIGINAL,
                                    reason="original file not available locally; downloading requires an "
                                           "authorised ingestion run")
    path = Path(path)
    if path.suffix.lower() == ".txt":
        text = path.read_text(errors="replace")
        return store.record_extraction(doc_id=doc_id, method="legacy_txt", parser_version="txt-1",
                                       pages=text.split(PAGE_SEPARATOR), original_path=path)
    if path.suffix.lower() != ".pdf":
        return store.record_failure(doc_id=doc_id, method="none", parser_version="none",
                                    status=ExtractionStatus.UNSUPPORTED_FORMAT,
                                    reason=f"unsupported file type {path.suffix}", original_path=path)
    pv = getattr(parser, "parser_version", "unknown")
    method = getattr(parser, "primary_engine", "pdf")
    prior_failures = [a for a in store.attempts(doc_id, None, pv)
                      if a.status == ExtractionStatus.PARSE_FAILED_RETRYABLE]
    if len(prior_failures) > max_retries:
        return prior_failures[-1]                                 # retry budget spent
    try:
        res = parser.parse(path)
    except Exception as e:                                         # parser crash: retryable
        return store.record_failure(doc_id=doc_id, method=method, parser_version=pv,
                                    status=ExtractionStatus.PARSE_FAILED_RETRYABLE, reason=str(e)[:300],
                                    original_path=path)
    status, _ = classify_pages(res.pages, res.pages_expected, res.truncated)
    if status == ExtractionStatus.OCR_REQUIRED and getattr(res, "failure_kind", "") in ("no_text", "") \
            and ocr is not None:
        if getattr(ocr, "network", True) and not allow_network_ocr:
            raise PermissionError(f"OCR provider {ocr.name} sends data off-machine; not authorised")
        try:
            pages = ocr.ocr(path)
        except Exception as e:
            return store.record_failure(doc_id=doc_id, method=f"ocr:{ocr.name}", parser_version=ocr.version,
                                        status=ExtractionStatus.OCR_REQUIRED, reason=f"OCR failed: {e}"[:300],
                                        original_path=path, pages_expected=res.pages_expected)
        return store.record_extraction(doc_id=doc_id, method=f"ocr:{ocr.name}", parser_version=ocr.version,
                                       pages=pages, original_path=path, pages_expected=res.pages_expected,
                                       quality_issues=["text produced by OCR"])
    return record_parse_result(store, doc_id, path, res, pv)
