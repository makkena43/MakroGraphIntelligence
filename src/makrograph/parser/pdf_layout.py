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

LAYOUT_VERSION = "words-v3"


def _char_width(w: dict) -> float:
    return (w["x1"] - w["x0"]) / max(1, len(w["text"]))


def _skew(words: list[dict]) -> float:
    """The page's tilt (change in ``top`` per point of x), measured on pieces of one word that sit
    side by side with almost no gap; 0 for an untilted page."""
    ws = sorted(words, key=lambda w: w["x0"])
    slopes = []
    for i, a in enumerate(ws):
        h = max(1.0, a["bottom"] - a["top"])
        for b in ws[i + 1:i + 40]:
            dx = b["x0"] - a["x1"]
            if dx > 0.3 * _char_width(a):
                break
            if -0.5 < dx and abs(b["top"] - a["top"]) < 0.5 * h and b["x0"] > a["x0"]:
                slopes.append((b["top"] - a["top"]) / (b["x0"] - a["x0"]))
    if len(slopes) < 20:
        return 0.0
    m = median(slopes)
    return m if abs(m) > 0.002 else 0.0


def _lines(words: list[dict], y_tolerance: float) -> list[list[dict]]:
    """Group words into text lines.  A page scanned at a slight tilt is straightened first (each
    word's ``top`` corrected by the measured skew), so a row stays one line across the page."""
    k = _skew(words)
    out: list[list[dict]] = []
    level = lambda w: w["top"] - k * w["x0"]                       # noqa: E731
    for w in sorted(words, key=lambda w: (level(w), w["x0"])):
        if out:
            ref = out[-1]
            top = median(level(x) for x in ref)
            height = median(x["bottom"] - x["top"] for x in ref) or 1.0
            if abs(level(w) - top) <= max(y_tolerance, 0.4 * height):
                ref.append(w)
                continue
        out.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in out]


def _join_fragments(line: list[dict], ratio: float = 0.3) -> list[dict]:
    """Re-join pieces of one word: scanned text layers often store every character separately with
    almost no gap ("5 6 , 2 6 9 . 0 1").  Pieces closer than ``ratio`` of a character width are one word;
    a space between words is wider (about 0.25-0.7 of a character width in the filings seen)."""
    out: list[dict] = []
    for w in line:
        if out:
            prev = out[-1]
            gap = w["x0"] - prev["x1"]
            if gap < ratio * min(_char_width(prev), _char_width(w)):
                out[-1] = {**prev, "text": prev["text"] + w["text"], "x1": w["x1"],
                           "bottom": max(prev["bottom"], w["bottom"])}
                continue
        out.append(dict(w))
    return out


def words_to_text(words: Iterable[dict], y_tolerance: float = 2.5, min_gap_chars: float = 1.5) -> str:
    """Column-aligned text for one page's words (dicts with text, x0, x1, top, bottom)."""
    words = [w for w in words if (w.get("text") or "").strip()]
    if not words:
        return ""
    lines = [_join_fragments(line) for line in _lines(words, y_tolerance)]
    widths = [_char_width(w) for line in lines for w in line if w["x1"] > w["x0"]]
    cw = median(widths) if widths else 5.0          # typical character width on this page
    rows = []
    for line in lines:
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
