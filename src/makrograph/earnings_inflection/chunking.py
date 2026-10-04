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


def _is_tabular(line: str) -> bool:
    if "|" in line and line.count("|") >= 2:
        return True
    nums = _NUM.findall(line)
    return len(nums) >= 2 and bool(re.search(r"\S\s{2,}\S|\t", line))


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
    sents = _SENT_SPLIT.split(text)
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
    return [s for s in _SENT_SPLIT.split(chunk.text) if s.strip()]


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
