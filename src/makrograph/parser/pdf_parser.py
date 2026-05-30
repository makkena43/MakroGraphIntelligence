"""Reliable PDF text extraction with dual-engine fallback."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    """Result of parsing a single document."""
    source_path: Path
    text: str = ""
    page_count: int = 0
    tables: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    engine_used: str = ""
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return len(self.text.strip()) > 0 and self.error is None


class PDFParser:
    """PDF parser with pdfplumber primary + PyMuPDF fallback."""

    def __init__(self, config: dict):
        self.primary_engine = config.get("engine", "pdfplumber")
        self.fallback_engine = config.get("fallback_engine", "pymupdf")
        self.max_pages = config.get("max_pages", 500)
        self.extract_tables = config.get("extract_tables", True)
        self.output_dir = Path(config.get("output_dir", "data/parsed"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def parse(self, pdf_path: Path) -> ParseResult:
        """Parse a PDF file, trying primary engine then fallback."""
        pdf_path = Path(pdf_path)
        result = ParseResult(source_path=pdf_path)

        if not pdf_path.exists():
            result.error = f"File not found: {pdf_path}"
            return result

        if not pdf_path.suffix.lower() == ".pdf":
            result.error = f"Not a PDF file: {pdf_path}"
            return result

        # Try primary engine
        if self.primary_engine == "pdfplumber":
            result = self._parse_with_pdfplumber(pdf_path)
        else:
            result = self._parse_with_pymupdf(pdf_path)

        # Fallback if primary failed or extracted no text
        if not result.success and self.fallback_engine:
            # Debug-level only — noisy for encrypted/scanned PDFs which always fail both engines
            logger.debug(
                f"Primary engine ({self.primary_engine}) failed for {pdf_path.name}, "
                f"trying fallback ({self.fallback_engine})"
            )
            if self.fallback_engine == "pymupdf":
                result = self._parse_with_pymupdf(pdf_path)
            else:
                result = self._parse_with_pdfplumber(pdf_path)

        if result.success:
            logger.info(
                f"Parsed {pdf_path.name}: {result.page_count} pages, "
                f"{len(result.text):,} chars ({result.engine_used})"
            )
        else:
            # Debug-level — encrypted/scanned/corrupt PDFs are common in Indian corporate filings
            # They are marked 'unsupported' in DB and never retried
            logger.debug(f"Failed to parse {pdf_path.name}: {result.error}")

        return result

    def _parse_with_pdfplumber(self, pdf_path: Path) -> ParseResult:
        """Extract text using pdfplumber."""
        result = ParseResult(source_path=pdf_path, engine_used="pdfplumber")
        try:
            import pdfplumber
            import logging as _logging
            # Suppress noisy pdfplumber font-descriptor warnings (FontBBox, non-standard fonts)
            # — these are harmless and only clutter logs; text extraction still succeeds.
            _logging.getLogger("pdfplumber").setLevel(_logging.ERROR)
            _logging.getLogger("pdfminer").setLevel(_logging.ERROR)

            pages_text = []
            tables = []

            with pdfplumber.open(pdf_path) as pdf:
                result.page_count = len(pdf.pages)
                result.metadata = pdf.metadata or {}

                for i, page in enumerate(pdf.pages):
                    if i >= self.max_pages:
                        logger.info(f"Reached max pages ({self.max_pages}), stopping")
                        break

                    text = page.extract_text()
                    if text:
                        pages_text.append(text)

                    if self.extract_tables:
                        page_tables = page.extract_tables()
                        if page_tables:
                            for table in page_tables:
                                tables.append({
                                    "page": i + 1,
                                    "data": table,
                                })

            result.text = "\n\n".join(pages_text)
            result.tables = tables

        except Exception as e:
            result.error = f"pdfplumber error: {e}"
            logger.error(f"pdfplumber failed on {pdf_path.name}: {e}")

        return result

    def _parse_with_pymupdf(self, pdf_path: Path) -> ParseResult:
        """Extract text using PyMuPDF (fitz)."""
        result = ParseResult(source_path=pdf_path, engine_used="pymupdf")
        try:
            import fitz  # PyMuPDF
            # Suppress MuPDF low-level errors (e.g. "unknown keyword: 'of'") —
            # these are benign syntax warnings from malformed/old Indian corporate PDFs.
            # Text extraction still succeeds despite these messages.
            fitz.TOOLS.mupdf_display_errors(False)

            doc = fitz.open(pdf_path)
            result.page_count = len(doc)
            result.metadata = dict(doc.metadata) if doc.metadata else {}

            pages_text = []
            for i, page in enumerate(doc):
                if i >= self.max_pages:
                    break
                text = page.get_text("text")
                if text.strip():
                    pages_text.append(text)

            result.text = "\n\n".join(pages_text)
            doc.close()

        except Exception as e:
            result.error = f"pymupdf error: {e}"
            logger.error(f"PyMuPDF failed on {pdf_path.name}: {e}")

        return result

    def parse_batch(self, pdf_paths: list[Path]) -> list[ParseResult]:
        """Parse multiple PDFs."""
        results = []
        total = len(pdf_paths)
        for i, path in enumerate(pdf_paths, 1):
            logger.info(f"Parsing [{i}/{total}]: {path.name}")
            result = self.parse(path)
            results.append(result)
        success = sum(1 for r in results if r.success)
        logger.info(f"Parse batch complete: {success}/{total} successful")
        return results

    def save_text(self, parse_result: ParseResult) -> Optional[Path]:
        """Save extracted text to output directory."""
        if not parse_result.success:
            return None
        output_path = self.output_dir / f"{parse_result.source_path.stem}.txt"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(parse_result.text)
        return output_path
