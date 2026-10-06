"""Page- and table-aware chunking plus chronological retrieval.

Design rules (spec §2, "behaviors NOT to inherit"):
* Never compact text newest-first by keyword: original promises, negations,
  table headers and cash-flow warnings must survive.
* A table chunk always carries its header lines and unit line.
* Retrieval returns chunks oldest-document-first, in page order, and reports
  exactly what was omitted when a budget applies - nothing is dropped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .contracts import Chunk, SourceDocument

_NUM = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_UNIT_LINE = re.compile(r"\((?:rs\.?|inr|₹|amount)[^)]{0,40}(?:crore|lakh|lacs|million|mn|thousand)[^)]{0,20}\)|"
                        r"(?:rs\.?|₹|inr)\s*in\s*(?:crore|lakh|lakhs|lacs|million|mn)", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'₹])")
# a full stop after these is an abbreviation, not a sentence end ("purchase order No. X", "Rs. 450", "M/s. ABC")
_ABBREV_END = re.compile(r"(?:\b(?:No|Nos|Rs|Ltd|Pvt|Co|Inc|M/s|Mr|Ms|Dr|viz|approx|Sr|Jr)|\b[A-Z])\.$")


def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for part in _SENT_SPLIT.split(text):
        if out and _ABBREV_END.search(out[-1]):
            out[-1] = out[-1] + " " + part
        else:
            out.append(part)
    return out


_CELL_TOKEN = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?|\(?[-+]?\d[\d,]*(?:\.\d+)?%\)?|-|–|—|nil", re.I)
_DATE_TOKEN = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")


_COLUMN_GAP = re.compile(r"\d\)?\s{2,}\(?-?\d")
_SPACED_GROUP = re.compile(r"(?:(?<=\s\s)|(?<=^))(\(?-?\d{1,3}) ,?(\d{3}(?:\.\d{1,2})?\)?)(?=\s{2,}|\s*$)")


def split_numeric_row(line: str) -> tuple[str, list[str]]:
    """Split "label  c1 c2 c3" into (label, trailing numeric cells).

    Works for both column-aligned text and pdfplumber's default output, which
    separates columns with a SINGLE space.  Dashes / "nil" count as empty cells.
    """
    if _COLUMN_GAP.search(line):
        # column-aligned text (cells apart by 2+ spaces): a SINGLE space inside a cell is a
        # scanned thousands separator - "50 321" is 50,321 and "1 ,917" is 1,917, not two cells
        line = _SPACED_GROUP.sub(r"\1,\2", line)
    tokens = line.strip().replace("|", " ").split()
    cells: list[str] = []
    while tokens:
        # OCR split a decimal across two tokens: "8,476." "75" -> "8,476.75"
        if len(tokens) >= 2 and _SPLIT_DECIMAL_HEAD.fullmatch(tokens[-2]) and _SPLIT_DECIMAL_TAIL.fullmatch(tokens[-1]):
            tokens[-2:] = [tokens[-2] + tokens[-1]]
        # OCR split a grouped figure: "1,1" "0,377.07" -> "1,10,377.07"; "3,01" ",882.96"
        if (len(tokens) >= 2 and _NUMERICISH.fullmatch(tokens[-2]) and not _GROUPED.fullmatch(tokens[-2])
                and _GROUPED.fullmatch(_trim(tokens[-2] + tokens[-1]))):
            tokens[-2:] = [tokens[-2] + tokens[-1]]
        tok = normalise_figure(_repair_ocr_number(tokens[-1]))
        if not (_CELL_TOKEN.fullmatch(tok) or tok == UNREADABLE):
            break
        tokens.pop()
        cells.append(tok)
    return " ".join(tokens), cells[::-1]


# A figure whose digits survived but whose separators were scanned wrongly.
# Kept as a positional placeholder so the other columns stay aligned; its value is unknown.
UNREADABLE = "?"
_NUMERICISH = re.compile(r"\(?-?[\d.,]*\d[\d.,]*\)?")
_GROUPED = re.compile(r"\(?-?(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?\)?")


def _trim(tok: str) -> str:
    """Stray trailing separator: "1,400.95," / "15,756.86." -> "1,400.95"."""
    return tok[:-1] if len(tok) > 1 and tok[-1] in ",." and tok[-2].isdigit() else tok


_STRAY = re.compile(r"^[\[{|]+|[\]}|]+$")


def normalise_figure(tok: str) -> str:
    """Repair separators in a scanned figure, or mark it unreadable.

    * valid grouping (western 1,234,567.89 or Indian 1,23,45,678.90) is kept;
    * dots scanned in place of commas are repaired when the result is a valid
      grouping: "11.30.069" -> "11,30,069", "2.42,845.54" -> "2,42,845.54";
    * a figure with separators that still does not form a valid grouping
      ("47,2563") becomes UNREADABLE instead of a wrong value.
    Tokens without separators, words and dashes are returned unchanged.
    """
    if re.search(r"\d", tok):
        tok = _STRAY.sub("", tok)          # scanned table rules: "17,111.84}", "18,589.83]"
    tok = _trim(tok)
    if not _NUMERICISH.fullmatch(tok) or _GROUPED.fullmatch(tok):
        return tok
    core = tok.strip("()-")
    if "," not in core and core.count(".") <= 1:
        return tok
    m = re.fullmatch(r"(.*)\.(\d{1,2})", core)
    head, dec = (m.group(1), "." + m.group(2)) if m else (core, "")
    fixed = head.replace(".", ",") + dec
    cand = tok.replace(core, fixed)
    if _GROUPED.fullmatch(cand):
        return cand
    return UNREADABLE if sum(ch.isdigit() for ch in core) >= 4 else tok


_SPLIT_DECIMAL_HEAD = re.compile(r"\(?-?[\d,]+\.")
_SPLIT_DECIMAL_TAIL = re.compile(r"\d{1,3}\)?")
_OCR_NUMBER = re.compile(r"\(?-?[\dSOlI,]*\d[\dSOlI,]*(?:\.[\dSOlI]+)?\)?|\(?-?[\dSOlI,]+\.\d[\dSOlI]*\)?")
_OCR_DIGITS = str.maketrans({"S": "5", "O": "0", "l": "1", "I": "1"})


def _repair_ocr_number(tok: str) -> str:
    """Letters scanned in place of digits inside a figure ("S,981.34", "6,S59.95").
    Only tokens that already contain a digit and nothing but digit-like letters are touched."""
    if _CELL_TOKEN.fullmatch(tok) or not _OCR_NUMBER.fullmatch(tok):
        return tok
    return tok.translate(_OCR_DIGITS)


def _is_tabular(line: str) -> bool:
    if "|" in line and line.count("|") >= 2:
        return True
    if len(_DATE_TOKEN.findall(line)) >= 2:          # period header row
        return True
    if len(line) > 250 or _ends_sentence(line):
        return False
    label, cells = split_numeric_row(line)
    numeric = [c for c in cells if _NUM.fullmatch(c)]
    # two numeric trailing columns, or three+ cells where blanks are shown as "-"
    # (e.g. exceptional items); a sentence ending "... 2024." never qualifies
    return (len(cells) >= 2 and len(numeric) >= 2) or (len(cells) >= 3 and len(numeric) >= 1)


def split_pages(doc: SourceDocument) -> list[str]:
    if doc.pages:
        return list(doc.pages)
    return (doc.text or "").split("\f")


def chunk_document(doc: SourceDocument, max_chars: int = 1500) -> list[Chunk]:
    chunks: list[Chunk] = []
    offset = 0
    ordinal = 0
    for page_no, page in enumerate(split_pages(doc), start=1):
        lines = page.split("\n")
        unit_line = ""
        prose_buf: list[str] = []
        i = 0
        pos = offset

        def flush_prose():
            nonlocal ordinal, prose_buf
            text = " ".join(l.strip() for l in prose_buf if l.strip())
            prose_buf = []
            if not text:
                return
            for piece in _pack_sentences(text, max_chars):
                chunks.append(Chunk(f"{doc.doc_id}:{ordinal}", doc.doc_id, page_no, ordinal, "prose", piece,
                                    char_start=pos, char_end=pos + len(piece)))
                ordinal += 1

        while i < len(lines):
            line = lines[i]
            if _UNIT_LINE.search(line):
                unit_line = line.strip()
            if _is_tabular(line):
                # header = up to 4 preceding non-empty, non-tabular lines on this page
                header_lines = [l.strip() for l in prose_buf[-4:] if l.strip()]
                prose_buf = prose_buf[:-4] if len(prose_buf) > 4 else []
                flush_prose()
                rows = []
                while i < len(lines) and (_is_tabular(lines[i]) or (lines[i].strip() and not _ends_sentence(lines[i])
                                                                     and rows and len(lines[i]) < 80)):
                    if _UNIT_LINE.search(lines[i]):
                        unit_line = lines[i].strip()
                    rows.append(lines[i].rstrip())
                    i += 1
                header = "\n".join(([unit_line] if unit_line and unit_line not in header_lines else []) + header_lines)
                body = "\n".join(rows)
                chunks.append(Chunk(f"{doc.doc_id}:{ordinal}", doc.doc_id, page_no, ordinal, "table", body,
                                    header=header, char_start=pos, char_end=pos + len(body)))
                ordinal += 1
                continue
            prose_buf.append(line)
            i += 1
        flush_prose()
        offset += len(page) + 1
    return chunks


def _ends_sentence(line: str) -> bool:
    return line.rstrip().endswith((".", "?", "!"))


def _pack_sentences(text: str, max_chars: int) -> list[str]:
    sents = _split_sentences(text)
    out, cur = [], ""
    for s in sents:
        while len(s) > max_chars:          # hard split; nothing is dropped
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:max_chars])
            s = s[max_chars:]
        if cur and len(cur) + 1 + len(s) > max_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def sentences(chunk: Chunk) -> list[str]:
    if chunk.kind == "table":
        return [l for l in chunk.text.split("\n") if l.strip()]
    return [s for s in _split_sentences(chunk.text) if s.strip()]


@dataclass
class Retrieval:
    selected: list[Chunk]
    omitted: list[Chunk] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.omitted


def retrieve(docs_chronological: Iterable[tuple[SourceDocument, list[Chunk]]],
             pattern: re.Pattern, char_budget: Optional[int] = None) -> Retrieval:
    """All matching chunks, oldest document first, page order preserved.

    When ``char_budget`` is exceeded the LATER chunks are omitted (so original
    promises are kept) and returned in ``omitted`` for disclosure.
    """
    matched: list[Chunk] = []
    for _doc, chunks in docs_chronological:
        for c in chunks:
            if pattern.search(c.text) or (c.header and pattern.search(c.header)):
                matched.append(c)
    if char_budget is None:
        return Retrieval(matched)
    sel, omitted, used = [], [], 0
    for c in matched:
        size = len(c.text) + len(c.header)
        if used + size <= char_budget:
            sel.append(c)
            used += size
        else:
            omitted.append(c)
    return Retrieval(sel, omitted)
