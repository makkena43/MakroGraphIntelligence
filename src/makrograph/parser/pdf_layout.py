"""Layout-preserving page text from pdfplumber word boxes.

Results statements are tables.  Plain text extraction (pdfplumber ``extract_text()``) loses the
columns, and ``pdftotext -layout`` splits scanned numbers ("32 13" for 3213) and dates ("30 092021").
Here each page is rebuilt from pdfplumber's words: a word is one run of characters with no gap
wider than ``x_tolerance``, so a number stays whole, and its horizontal position becomes its text
column.  Words in different table columns are always separated by at least two spaces, which is
what the column-aligned table readers rely on.

No OCR: a page without a text layer yields an empty string.
"""

from __future__ import annotations

import logging
from pathlib import Path
from statistics import median
from typing import Iterable, Optional

LAYOUT_VERSION = "words-v1"


def _lines(words: list[dict], y_tolerance: float) -> list[list[dict]]:
    """Group words into text lines by their vertical position."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if out:
            ref = out[-1]
            top = median(x["top"] for x in ref)
            height = median(x["bottom"] - x["top"] for x in ref) or 1.0
            if abs(w["top"] - top) <= max(y_tolerance, 0.4 * height):
                ref.append(w)
                continue
        out.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in out]


def words_to_text(words: Iterable[dict], y_tolerance: float = 2.5, min_gap_chars: float = 1.5) -> str:
    """Column-aligned text for one page's words (dicts with text, x0, x1, top, bottom)."""
    words = [w for w in words if (w.get("text") or "").strip()]
    if not words:
        return ""
    widths = [(w["x1"] - w["x0"]) / len(w["text"]) for w in words if len(w["text"]) >= 2 and w["x1"] > w["x0"]]
    cw = median(widths) if widths else 5.0          # typical character width on this page
    rows = []
    for line in _lines(words, y_tolerance):
        s, prev_x1 = "", None
        for w in line:
            col = int(round(w["x0"] / cw))
            if prev_x1 is not None:
                gap = (w["x0"] - prev_x1) / cw
                sep = 1 if gap < min_gap_chars else 2       # a wide gap is a column boundary
                col = max(col, len(s) + sep)
            s = s.ljust(col) + w["text"]
            prev_x1 = w["x1"]
        rows.append(s.rstrip())
    return "\n".join(rows)


def layout_pages(pdf_path: str | Path, max_pages: Optional[int] = None, x_tolerance: float = 1.5) -> list[str]:
    """Layout text of each page (empty for pages without a text layer)."""
    import pdfplumber
    for name in ("pdfplumber", "pdfminer"):
        logging.getLogger(name).setLevel(logging.ERROR)
    pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            if max_pages is not None and i >= max_pages:
                break
            try:
                words = page.extract_words(x_tolerance=x_tolerance, y_tolerance=2, keep_blank_chars=False,
                                           use_text_flow=False)
            except Exception:                                   # noqa: BLE001 - a damaged page is skipped
                words = []
            pages.append(words_to_text(words))
    return pages
