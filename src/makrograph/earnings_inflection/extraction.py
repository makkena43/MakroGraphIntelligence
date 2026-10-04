"""Evidence extraction: deterministic parsing first, optional constrained LLM.

Deterministic layer
-------------------
* ``parse_results_tables`` reads exchange results statements
  ("Statement of ... Financial Results") into ``FinancialMeasurement`` rows,
  using the table header for period columns and the unit line for scale.
* ``extract_sentence_evidence`` turns sentences into ``Evidence`` with exact
  quotes, normalised quantities, modality (realized / forward / conditional /
  negated) and tier (assertion / commitment / realized execution).

The lexical cues partially overlap with ``india/order_book_detector.py`` but
are *not* reused as signals: order language there means supply constraint;
here it is only a candidate commercial commitment that must later be
validated, deduplicated and compared with realized results.

LLM layer
---------
``ConstrainedLLMExtractor`` is disabled unless explicitly enabled AND given a
budget.  It receives a caller-supplied ``complete(prompt) -> str`` function
(no network client is constructed here), must return JSON, and every item
is dropped unless its quote is found verbatim in the source chunk.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Callable, Iterable, Optional

from .chunking import sentences, split_numeric_row
from .contracts import (
    Chunk, CommitmentStrength, Evidence, EvidenceTier, FinancialMeasurement, Metric,
    Modality, Quantity, Scope, SourceDocument, Unit,
)

# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------

_INR_SCALE_TO_CRORE = {
    "crore": 1.0, "crores": 1.0, "cr": 1.0, "cr.": 1.0, "crs": 1.0,
    "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
    "mn": 0.1, "million": 0.1, "bn": 100.0, "billion": 100.0,
    "thousand": 0.0001,
}
_CURRENCY = r"(?:rs\.?|inr|₹|rupees)"
_NUMBER = r"(\d[\d,]*(?:\.\d+)?)"
_SCALE = r"(crores?|cr\.?|crs|lakhs?|lacs?|lac|mn|million|bn|billion)"
INR_AMOUNT = re.compile(rf"{_CURRENCY}\s*{_NUMBER}\s*{_SCALE}\b|{_NUMBER}\s*{_SCALE}\b", re.I)
USD_AMOUNT = re.compile(r"(?:us\$|usd|\$)\s*(\d[\d,]*(?:\.\d+)?)\s*(mn|million|bn|billion)\b", re.I)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)(?:\s*(?:to|-|–)\s*(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent))?", re.I)
PERCENT_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%\s*)?(?:to|-|–)\s*(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)", re.I)
BPS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bps|basis points)", re.I)
CAPACITY_QTY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(MW|GW|MTPA|TPA|tonnes per annum|units)\b", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_inr(text: str) -> Optional[Quantity]:
    m = INR_AMOUNT.search(text)
    if not m:
        return None
    if m.group(1):
        val, scale = m.group(1), m.group(2)
    else:
        val, scale = m.group(3), m.group(4)
        # Without a currency marker only "crore"/"lakh" are unambiguous money words
        # in Indian filings; "1 million" may be doses, units or tonnes.
        if scale.lower().startswith(("mn", "million", "bn", "billion")):
            return None
    scale = scale.lower().rstrip(".")
    factor = _INR_SCALE_TO_CRORE.get(scale, _INR_SCALE_TO_CRORE.get(scale + ".", 1.0))
    return Quantity(round(_num(val) * factor, 4), Unit.INR_CRORE, m.group(0))


def parse_usd(text: str) -> Optional[Quantity]:
    m = USD_AMOUNT.search(text)
    if not m:
        return None
    factor = 1000.0 if m.group(2).lower().startswith("b") else 1.0
    return Quantity(_num(m.group(1)) * factor, Unit.USD_MN, m.group(0))


def parse_percent(text: str) -> Optional[Quantity]:
    r = PERCENT_RANGE.search(text)
    if r:
        lo, hi = float(r.group(1)), float(r.group(2))
        if lo <= hi:
            return Quantity((lo + hi) / 2, Unit.PERCENT, r.group(0), low=lo, high=hi)
    m = PERCENT.search(text)
    if not m:
        return None
    return Quantity(float(m.group(1)), Unit.PERCENT, m.group(0))


# ---------------------------------------------------------------------------
# Period labels (Indian fiscal year: April-March)
# ---------------------------------------------------------------------------

_Q_LABEL = re.compile(r"\b(Q[1-4])\s*[-']?\s*FY\s*'?(\d{2,4})\b", re.I)
_H_LABEL = re.compile(r"\b(H[12])\s*[-']?\s*FY\s*'?(\d{2,4})\b", re.I)
_FY_LABEL = re.compile(r"\bFY\s*'?(\d{2,4})(?:\s*[-–/]\s*'?(\d{2,4}))?\b", re.I)


def _fy(y: str) -> int:
    n = int(y)
    return 2000 + n if n < 100 else n


def find_period_labels(text: str) -> list[str]:
    labels = []
    for m in _Q_LABEL.finditer(text):
        labels.append(f"{m.group(1).upper()}FY{_fy(m.group(2)) % 100:02d}")
    for m in _H_LABEL.finditer(text):
        labels.append(f"{m.group(1).upper()}FY{_fy(m.group(2)) % 100:02d}")
    stripped = _H_LABEL.sub(" ", _Q_LABEL.sub(" ", text))
    for m in _FY_LABEL.finditer(stripped):
        y = _fy(m.group(2)) if m.group(2) else _fy(m.group(1))
        labels.append(f"FY{y % 100:02d}")
    return labels


def fy_label_end(label: str) -> Optional[date]:
    """Period end date for an Indian fiscal label (FY25 -> 2025-03-31, Q1FY25 -> 2024-06-30)."""
    m = re.fullmatch(r"(Q[1-4]|H[12]|9M)?FY(\d{2})", label or "")
    if not m:
        return None
    fy = 2000 + int(m.group(2))
    p = m.group(1)
    if not p:
        return date(fy, 3, 31)
    return {"Q1": date(fy - 1, 6, 30), "Q2": date(fy - 1, 9, 30), "Q3": date(fy - 1, 12, 31),
            "Q4": date(fy, 3, 31), "H1": date(fy - 1, 9, 30), "H2": date(fy, 3, 31),
            "9M": date(fy - 1, 12, 31)}[p]


def fy_label_for(period_end: date, period_type: str) -> str:
    fy = period_end.year + 1 if period_end.month > 3 else period_end.year
    if period_type == "FY":
        return f"FY{fy % 100:02d}"
    if period_type == "H":
        h = {9: "H1", 3: "H2"}.get(period_end.month, "H?")
        return f"{h}FY{fy % 100:02d}"
    q = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(period_end.month, "Q?")
    return f"{q}FY{fy % 100:02d}"


# ---------------------------------------------------------------------------
# Results-statement tables
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Row labels are matched AFTER stripping enumerators such as "I", "IV",
# "1.", "a)", "(2)", "Less:", so the patterns below anchor at the start of
# the remaining text.
_ENUMERATOR = re.compile(
    r"^\s*(?:(?:\(?(?:[ivxlIVXL]{1,5}|\d{1,2}|[a-zA-Z])\s*[.):]|(?:[ivxlIVXL]{1,5}|\d{1,2})\s)\s*|"
    r"(?:less|add)\s*:\s*|[-–•*]\s*)+")


def _strip_enumerator(label: str) -> str:
    return _ENUMERATOR.sub("", label, count=1).strip()


_P = r"profit\s*(?:/\s*\(?\s*loss\s*\)?)?"           # "Profit", "Profit/(Loss)", "Profit / (loss)"
_ROW_METRICS = [
    (re.compile(rf"(?:net\s+)?{_P}.{{0,40}}attributable\s+to\s+(?:the\s+)?(?:owners|equity|shareholders)", re.I),
     Metric.PAT_ATTRIBUTABLE),
    (re.compile(r"(?:total\s+)?(?:revenue|income)\s+from\s+operations\b", re.I), Metric.REVENUE),
    (re.compile(r"(?:net\s+)?sales\b", re.I), Metric.REVENUE),
    (re.compile(r"other\s+income\b", re.I), Metric.OTHER_INCOME),
    (re.compile(r"total\s+expenses?\b", re.I), Metric.TOTAL_EXPENSES),
    (re.compile(r"ebitda\b(?!\s*margin)|earnings\s+before\s+interest,?\s+tax(?:es)?,?\s+depreciation", re.I),
     Metric.EBITDA),
    (re.compile(r"depreciation", re.I), Metric.DEPRECIATION),
    (re.compile(r"finance\s+costs?\b|interest\s+(?:and\s+finance\s+)?(?:costs?|expenses?)\b", re.I), Metric.FINANCE_COST),
    (re.compile(r"exceptional\s+items?\b", re.I), Metric.EXCEPTIONAL_ITEMS),
    (re.compile(rf"(?:\(\s*loss\s*\)\s*/\s*)?{_P}\s*before\s+tax", re.I), Metric.PBT),
    (re.compile(r"(?:total\s+)?(?:income\s+)?tax\s+expenses?\b", re.I), Metric.TAX),
    (re.compile(rf"(?:net\s+)?{_P}\s*(?:after\s+tax|for\s+the\s+(?:period|year|quarter|half))", re.I), Metric.PAT),
    (re.compile(r"(?:owners|equity\s+(?:share)?holders|shareholders)\s+of\s+the\s+(?:company|parent|holding)", re.I),
     Metric.PAT_ATTRIBUTABLE),
]
_TAX_PARTS = re.compile(r"(?:current\s+tax|deferred\s+tax|(?:tax\s+(?:in\s+respect\s+of|relating\s+to|for)\s+)?"
                        r"(?:earlier|prior)\s+(?:years?|periods?))\b", re.I)
_COMPREHENSIVE = re.compile(r"comprehensive\s+income", re.I)

_CELL = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")


def _cell_value(s: str) -> float:
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    v = float(s.strip("()-").replace(",", ""))
    return -v if neg else v


_SCALE_WORD = r"(crores?|lakhs?|lacs?|million|mn|thousands?)"
_SCALE_PATTERNS = [
    re.compile(rf"(?:rs\.?|inr|₹|rupees|amounts?|figures)[^\n]{{0,30}}?\b{_SCALE_WORD}\b", re.I),
    re.compile(rf"\(\s*in\s+{_SCALE_WORD}\b", re.I),
]


def _table_scale(header: str, page_text: str) -> Optional[float]:
    for src in (header, page_text):
        for pat in _SCALE_PATTERNS:
            m = pat.search(src)
            if not m:
                continue
            w = m.group(1).lower()
            if w.startswith("crore"):
                return 1.0
            if w.startswith(("lakh", "lac")):
                return 0.01
            if w in ("million", "mn"):
                return 0.1
            if w.startswith("thousand"):
                return 0.0001
    return None


_SCOPE_TITLE = re.compile(r"^.*(?:statement|financial\s+results|results\s+for).*$", re.I | re.M)


def _scope_from(text: str) -> Scope:
    # Prefer the scope named in a statement title line; fall back to word presence.
    for line in _SCOPE_TITLE.findall(text):
        t = line.lower()
        has_c, has_s = "consolidated" in t, "standalone" in t
        if has_c != has_s:
            return Scope.CONSOLIDATED if has_c else Scope.STANDALONE
    t = text.lower()
    has_c, has_s = "consolidated" in t, "standalone" in t
    if has_c and not has_s:
        return Scope.CONSOLIDATED
    if has_s and not has_c:
        return Scope.STANDALONE
    if has_c and has_s:
        # "Statement of Standalone and Consolidated ..." - take the first mentioned
        return Scope.CONSOLIDATED if t.index("consolidated") < t.index("standalone") else Scope.STANDALONE
    return Scope.UNKNOWN


def _title_scope(text: str) -> Scope:
    for line in _SCOPE_TITLE.findall(text):
        t = line.lower()
        has_c, has_s = "consolidated" in t, "standalone" in t
        if has_c != has_s:
            return Scope.CONSOLIDATED if has_c else Scope.STANDALONE
    return Scope.UNKNOWN


def _resolve_scope(c: Chunk, page_texts: list[str]) -> Scope:
    # A statement title inside the table's own header / first rows wins: text extracted
    # without page breaks puts standalone and consolidated statements on one "page".
    own = _title_scope(c.header + "\n" + c.text[:800])
    if own != Scope.UNKNOWN:
        return own
    page = page_texts[c.page - 1] if 0 < c.page <= len(page_texts) else ""
    sc = _scope_from(c.header + "\n" + page[:1500])
    if sc != Scope.UNKNOWN:
        return sc
    if c.page >= 2:                                   # title at the bottom of the previous page
        sc = _scope_from(page_texts[c.page - 2][-800:])
        if sc != Scope.UNKNOWN:
            return sc
    # whole document: only if every statement title names the same scope
    scopes = {_scope_from(l) for pt in page_texts for l in _SCOPE_TITLE.findall(pt)} - {Scope.UNKNOWN}
    return scopes.pop() if len(scopes) == 1 else Scope.UNKNOWN


def _match_metrics(label: str) -> list[Metric]:
    lab = _strip_enumerator(label)
    if re.search(r"\bbasic\b", lab, re.I) and re.search(r"\bdiluted\b", lab, re.I):
        return [Metric.BASIC_EPS, Metric.DILUTED_EPS]
    if re.search(r"\bdiluted\b", lab, re.I):
        return [Metric.DILUTED_EPS]
    if re.match(r"basic\b", lab, re.I):
        return [Metric.BASIC_EPS]
    for pat, m in _ROW_METRICS:
        if pat.match(lab):
            return [m]
    return []


def parse_results_tables(doc: SourceDocument, chunks: list[Chunk]) -> tuple[list[FinancialMeasurement], list[str]]:
    """Parse results statements into measurements.  Returns (rows, issues)."""
    out: list[FinancialMeasurement] = []
    issues: list[str] = []
    page_texts = (doc.pages or (doc.text or "").split("\f"))
    for c in chunks:
        if c.kind != "table":
            continue
        page_text = page_texts[c.page - 1] if 0 < c.page <= len(page_texts) else ""
        lines = [l for l in c.text.split("\n") if l.strip()]
        resolved, col_issue = resolve_columns(c.header.split("\n") + lines[:4])
        if not resolved or col_issue:
            # headers wrapped over many lines put some period dates beyond the first rows
            wide, wide_issue = resolve_columns(c.header.split("\n") + lines[:12])
            if wide and not wide_issue and len(wide) >= len(resolved):
                resolved, col_issue = wide, wide_issue
        if not resolved:
            continue
        if col_issue:
            issues.append(f"{doc.doc_id}:p{c.page}: {col_issue}")
        cols = [d for d, _ in resolved]
        ptypes = [t for _, t in resolved]
        n = len(cols)
        scale = _table_scale(c.header, page_text)
        scope = _resolve_scope(c, page_texts)
        if scale is None:
            issues.append(f"{doc.doc_id}:p{c.page}: results table without unit line; amounts not used")
        found: dict[Metric, list] = {}
        tax_parts: list[list[Optional[float]]] = []
        pending, after_comprehensive, misaligned = "", False, 0
        for l in lines:
            label, cells = split_numeric_row(l)
            if _COMPREHENSIVE.search(label):
                after_comprehensive = True
            if not cells:
                # a label wrapped onto the next line ("Revenue from" / "operations 1,234 ...")
                pending = label if len(label) < 80 else ""
                continue
            is_tax_part = bool(_TAX_PARTS.match(_strip_enumerator(label)))
            # Join a wrapped label only when this line has no enumerator of its own:
            # "(1) Current tax" under "VIII Tax expense" is a sub-row, not a continuation.
            continuation = pending and not is_tax_part and _strip_enumerator(label) == label.strip()
            metrics = _match_metrics(label) or (_match_metrics(f"{pending} {label}") if continuation else [])
            pending = ""
            if not metrics and not is_tax_part:
                continue
            if Metric.PAT_ATTRIBUTABLE in metrics and after_comprehensive:
                continue   # "owners of the company" share of comprehensive income, not of profit
            if len(cells) < n:
                continue   # blank cells dropped by the PDF text; cannot align safely
            if len(cells) > n + 1:
                misaligned += 1  # more values than identified columns: header was mis-read
                continue
            cells = cells[-n:]   # one extra leading value = note reference column
            vals: list[Optional[float]] = []
            for cell in cells:
                try:
                    vals.append(_cell_value(cell) if _CELL.fullmatch(cell) else None)   # "-"/"nil" = blank
                except ValueError:
                    vals.append(None)
            if is_tax_part and not metrics:
                tax_parts.append(vals)
                continue
            for m in metrics:
                if m not in found:            # first occurrence wins (later = sub-totals / OCI)
                    found[m] = [l, vals]
        if tax_parts and Metric.TAX not in found:
            summed = [None if all(p[i] is None for p in tax_parts) else sum(p[i] or 0.0 for p in tax_parts)
                      for i in range(n)]
            found[Metric.TAX] = ["(sum of current/deferred/earlier-year tax rows)", summed]
        if misaligned:
            issues.append(f"{doc.doc_id}:p{c.page}: {misaligned} row(s) had more values than the {n} "
                          f"identified period columns; skipped")
        for metric, (line, vals) in found.items():
            for col_i, (d, v) in enumerate(zip(cols, vals)):
                if v is None or ptypes[col_i] is None:
                    continue
                if metric in (Metric.DILUTED_EPS, Metric.BASIC_EPS):
                    unit, val = Unit.INR_PER_SHARE, v
                else:
                    if scale is None:
                        continue
                    unit, val = Unit.INR_CRORE, round(v * scale, 4)
                out.append(FinancialMeasurement(
                    ticker=doc.ticker, metric=metric, period_end=d, period_type=ptypes[col_i],
                    value=val, unit=unit, scope=scope, doc_id=doc.doc_id, available_at=doc.available_at,
                    quote=f"{c.header.splitlines()[-1] if c.header else ''} | {line.strip()}"[:400],
                ))
    return out, issues


# --- column headers -------------------------------------------------------
#
# SEBI results statements put period columns in a fixed order:
#   quarter columns (current, previous, year-ago quarter)
#   -> cumulative columns (half-year / nine months, current and prior year)
#   -> year-ended column(s).
# Cumulative columns REPEAT the end date of a quarter column (e.g. 31.12.2024
# is both "quarter ended" and "nine months ended"), so the first repeated date
# marks the start of the cumulative block.  Header words are used only when
# they give exactly one label per column; titles such as "results for the
# quarter and nine months ended ..." are never used to type columns.

_MONTH_END = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
_COL_TOKEN = re.compile(
    r"(?P<lab>\b(?P<lp>Q[1-4]|H[12]|9M)\s*[-']?\s*FY\s*'?(?P<ly>\d{2}(?:\d{2})?)\b)"
    r"|(?P<fy>\bFY\s*'?(?P<fy1>\d{2}(?:\d{2})?)(?:\s*[-–/]\s*'?(?P<fy2>\d{2}(?:\d{2})?))?\b)"
    r"|(?P<num>\b(?P<nd>\d{1,2})[./-](?P<nm>\d{1,2})[./-](?P<ny>\d{4}|\d{2})\b)"
    r"|(?P<dmy>\b(?P<dd>\d{1,2})(?:st|nd|rd|th)?[\s-]*(?P<dmon>[A-Za-z]{3,9})[\s,'-]*(?P<dy>\d{4}|\d{2})\b)"
    r"|(?P<mdy>\b(?P<mmon>[A-Za-z]{3,9})\s+(?P<md>\d{1,2}),?\s+(?P<my>\d{4})\b)"
    # month-year only: "Jun-24", "Jun'24", "June 2024" (never "Sep 30": that is a day, not a year)
    r"|(?P<my_only>\b(?P<omon>[A-Za-z]{3,9})(?:\s*[-']\s*(?P<oy2>\d{2})|\s*[-']?\s*(?P<oy4>\d{4}))\b)",
    re.I,
)
_COL_GROUP_WORDS = re.compile(
    r"(?P<q>quarter ended|three months ended)|(?P<h>half[- ]year ended|six months ended)"
    r"|(?P<n>nine months ended)|(?P<y>year ended)", re.I)
_TITLE_LINE = re.compile(r"statement of|results for|financial results|unaudited|audited", re.I)


def _yr(y: str) -> int:
    n = int(y)
    return 2000 + n if n < 100 else n


def _column_tokens(line: str) -> list[tuple[date, Optional[str]]]:
    """Ordered (period_end, explicit_type) tokens found in a header line."""
    out: list[tuple[date, Optional[str]]] = []
    for m in _COL_TOKEN.finditer(line):
        try:
            if m.group("lab"):
                lp = m.group("lp").upper()
                d = fy_label_end(f"{lp}FY{_yr(m.group('ly')) % 100:02d}")
                if d:
                    out.append((d, "Q" if lp.startswith("Q") else "H" if lp.startswith("H") else "9M"))
            elif m.group("fy"):
                y = _yr(m.group("fy2") or m.group("fy1"))
                out.append((date(y, 3, 31), "FY"))
            elif m.group("num"):
                out.append((date(_yr(m.group("ny")), int(m.group("nm")), int(m.group("nd"))), None))
            elif m.group("dmy"):
                mon = _MONTHS.get(m.group("dmon")[:3].lower())
                if mon:
                    out.append((date(_yr(m.group("dy")), mon, int(m.group("dd"))), None))
            elif m.group("mdy"):
                mon = _MONTHS.get(m.group("mmon")[:3].lower())
                if mon:
                    out.append((date(int(m.group("my")), mon, int(m.group("md"))), None))
            elif m.group("my_only"):
                mon = _MONTHS.get(m.group("omon")[:3].lower())
                if mon:
                    y = _yr(m.group("oy2") or m.group("oy4"))
                    out.append((date(y, mon, _MONTH_END[mon]), None))
        except ValueError:
            continue
    return [t for t in out if t[0].month in (3, 6, 9, 12)]


_DAY_TOK = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[,.]?$", re.I)
_YEAR_TOK = re.compile(r"^'?(\d{4}|\d{2})[,.]?$")


def _stacked_dates(lines: list[str]) -> list[tuple[date, Optional[str]]]:
    """Dates whose day / month / year are stacked on consecutive lines:
        "31st 30th 31st"  /  "December September December"  /  "2023 2023 2022"
    (also month-over-year and day-month-over-year).  Counts must agree."""
    rows = [l.split() for l in lines]
    if any(not r for r in rows):
        return []
    if len(rows) == 3:
        d, m, y = rows
        if (len(d) == len(m) == len(y) and all(_DAY_TOK.match(t) for t in d)
                and all(t[:3].lower() in _MONTHS for t in m) and all(_YEAR_TOK.match(t) for t in y)):
            return _column_tokens(" ".join(f"{a} {b} {c}" for a, b, c in zip(d, m, y)))
    if len(rows) == 2:
        top, y = rows
        if len(top) == len(y) and all(_YEAR_TOK.match(t) for t in y):
            return _column_tokens(" ".join(f"{a} {b}" for a, b in zip(top, y)))
    return []


def _header_candidates(header_lines: list[str]):
    """(index, tokens) candidates: single lines, two wrapped lines joined, stacked dates."""
    toks = [_column_tokens(l) for l in header_lines]
    for i, line in enumerate(header_lines):
        if _TITLE_LINE.search(line) and len(toks[i]) < 3:
            continue          # a title naming one or two periods is not a column header
        yield i, toks[i]
        # period dates wrapped onto the next line: "30.09.2023 30.06.2023 30.09.2022" / "30.09.2023 ..."
        if i + 1 < len(header_lines) and toks[i] and toks[i + 1] and not _TITLE_LINE.search(header_lines[i + 1]):
            yield i + 1, toks[i] + toks[i + 1]
        for k in (2, 3):
            window = [re.sub(r"(?i)^\s*(?:particulars|sl\.?\s*no\.?|sr\.?\s*no\.?)\s*", "", l)
                      for l in header_lines[i:i + k]]
            if len(window) == k:
                st = _stacked_dates(window)
                if len(st) >= 2:
                    yield i + k - 1, st


def resolve_columns(header_lines: list[str]) -> tuple[list[tuple[date, Optional[str]]], str]:
    """Return [(period_end, period_type)] for the table's value columns.

    period_type is "Q", "H", "9M", "FY" or None (unidentifiable column: its
    values are not used).  The second element is an issue string or "".
    """
    best_i, best = -1, []
    for i, toks in _header_candidates(header_lines):
        if len(toks) >= 2 and len(toks) >= len(best):
            best_i, best = i, toks
    if not best:
        return [], ""
    n = len(best)

    # 1) explicit labels on every column (Q3 FY25, 9M FY25, FY24 ...)
    if all(t is not None for _, t in best):
        return best, ""

    # 2) header words giving exactly one group label per column
    above = " ".join(l for l in header_lines[max(0, best_i - 2):best_i] if not _TITLE_LINE.search(l))
    words = [("Q" if g.group("q") else "H" if g.group("h") else "9M" if g.group("n") else "FY")
             for g in _COL_GROUP_WORDS.finditer(above)]
    if len(words) == n:
        return [(d, t or w) for (d, t), w in zip(best, words)], ""

    # 3) positional rule from repeated dates.  The leading block is quarters unless
    #    the first header word says otherwise (SME half-yearly or annual-only statements).
    base = words[0] if words and words[0] in ("H", "FY") and "Q" not in words else "Q"
    seen: set[date] = set()
    cumulative = False
    out: list[tuple[date, Optional[str]]] = []
    for d, explicit in best:
        if explicit:
            out.append((d, explicit))
            seen.add(d)
            continue
        if d in seen:
            cumulative = True
        seen.add(d)
        if not cumulative:
            out.append((d, base))
        else:
            out.append((d, {3: "FY", 9: "H", 12: "9M"}.get(d.month)))   # June repeat: unidentifiable
    # A trailing March column after the quarter block with "year ended" in the
    # header but no repeated date (e.g. Q1 statement without previous quarter).
    if (base == "Q" and not cumulative and n >= 3 and best[-1][0].month == 3 and best[-1][1] is None
            and re.search(r"(?<!half )(?<!half-)\byear ended", above, re.I)
            and best[-1][0] > best[0][0].replace(year=best[0][0].year - 1)
            and best[-1][0] < best[0][0]):
        out[-1] = (best[-1][0], "FY")
    issue = ""
    q_dates = [d for d, t in out if t == "Q"]
    if q_dates and q_dates[0].replace(year=q_dates[0].year - 1) not in q_dates:
        issue = f"no year-ago quarter column found for {q_dates[0]}"
    if issue:
        layout = _sebi_layout_from_date_bag(header_lines)
        if layout and len(layout) > n:
            return layout, ""
    return out, issue


def _month_end(y: int, m: int) -> date:
    return date(y, m, 30 if m in (6, 9) else 31)


def _sebi_layout_from_date_bag(header_lines: list[str]) -> list[tuple[date, Optional[str]]]:
    """Header cells wrapped across many lines ("Preceding 3 months ended on 30.09.2023",
    "Corresponding 3 months in the previous year ended on 31.12.2022", ...) scramble the
    reading order of the dates, so positional reading fails.  The SEBI results layout is
    fixed, so take the latest quarter end D and require every date that layout implies to
    appear somewhere in the header; then the columns are known by construction:
      Q2/Q3: Q(D) Q(D-3m) Q(D-1y) YTD(D) YTD(D-1y) FY(prev Mar)
    (Q1 / Q4 layouts repeat dates, so only Q2/Q3 are inferred.)
    The caller still checks each data row's cell count against this width."""
    bag = {d for l in header_lines for d, _ in _column_tokens(l)}
    if len(bag) < 3:
        return []
    top = max(bag)
    y, m = top.year, top.month
    prev_q = _month_end(y if m > 3 else y - 1, m - 3 if m > 3 else 12)
    yago = _month_end(y - 1, m)
    if m not in (9, 12):
        return []     # Q1 / Q4 layouts repeat dates, so a date bag cannot distinguish them
    layout = [(top, "Q"), (prev_q, "Q"), (yago, "Q"), (top, "H" if m == 9 else "9M"),
              (yago, "H" if m == 9 else "9M"), (_month_end(y, 3), "FY")]
    return layout if {d for d, _ in layout} <= bag else []


# ---------------------------------------------------------------------------
# Sentence-level evidence
# ---------------------------------------------------------------------------

_NEGATION = re.compile(
    r"\b(?:not|no|never|neither|nor|unlikely|did not|didn't|do not|don't|does not|doesn't|won't|"
    r"will not|cannot|can't|without|yet to|failed to|none)\b", re.I)
_FORWARD = re.compile(
    r"\b(?:expect(?:s|ed|ing)?|target(?:s|ing)?|guid(?:e|ance|ing)|will|shall|aim(?:s|ing)?|plan(?:s|ning)?|"
    r"anticipat\w*|outlook|envisag\w*|going forward|by FY\s*'?\d{2}|next (?:year|quarter|fiscal)|"
    r"projected|estimate[sd]?|intend\w*|aspir\w*|confident of|should (?:be|reach|grow))\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:if|subject to|could|may|might|depending on|assuming|provided that|potential(?:ly)?)\b", re.I)
_REALIZED = re.compile(
    r"\b(?:reported|achieved|grew|rose|increased|declined|was|were|stood at|recorded|posted|received|"
    r"bagged|secured|won|executed|delivered|commissioned|completed|registered|clocked|improved|expanded)\b", re.I)

_METRIC_CUES: list[tuple[re.Pattern, Metric]] = [
    (re.compile(r"\border\s*book\b|\bunexecuted orders?\b|\bbacklog\b", re.I), Metric.ORDER_BOOK),
    (re.compile(r"\border (?:inflow|intake)s?\b", re.I), Metric.ORDER_INFLOW),
    (re.compile(r"\b(?:received|bagged|secured|won|awarded|receipt of|bagging)\b.{0,80}\b(?:orders?|contracts?|letter of (?:award|intent)|LoA|LoI|purchase orders?|work orders?)\b|"
                r"\b(?:orders?|contracts?)\b.{0,40}\b(?:worth|valued at|aggregating|amounting to|of value)\b|"
                r"\bletter of (?:award|intent)\b|\bL-?1\b(?: bidder| position)?|\bMoU\b", re.I), Metric.ORDER_WIN),
    (re.compile(r"\bebitda margins?\b|\boperating margins?\b", re.I), Metric.EBITDA_MARGIN),
    (re.compile(r"\bcapacity utili[sz]ation\b|\butili[sz]ation (?:level|rate)s?\b", re.I), Metric.UTILIZATION),
    (re.compile(r"\b(?:installed|production|manufacturing) capacity\b|\bcapacity (?:of|to)\b.{0,30}\b(?:MW|GW|TPA|MTPA|units)\b", re.I), Metric.CAPACITY),
    (re.compile(r"\bcapex\b|\bcapital expenditure\b", re.I), Metric.CAPEX),
    (re.compile(r"\b(?:qip|preferential (?:issue|allotment)|rights issue|fund ?rais\w*|warrants)\b", re.I), Metric.FUNDRAISE),
    (re.compile(r"\bcash (?:flow )?from operations\b|\boperating cash flow\b", re.I), Metric.OPERATING_CASH_FLOW),
    (re.compile(r"\bnet debt\b", re.I), Metric.NET_DEBT),
    (re.compile(r"\b(?:revenue|turnover|top ?line|sales)\b", re.I), Metric.REVENUE),
    (re.compile(r"\bebitda\b", re.I), Metric.EBITDA),
    (re.compile(r"\b(?:pat|net profit|profit after tax)\b", re.I), Metric.PAT),
]

_BINDING = re.compile(r"\b(?:purchase order|work order|contract (?:signed|executed|awarded)|letter of award|LoA|"
                      r"received (?:an? )?(?:order|contract)|bagged|secured|awarded|firm order)\b", re.I)
_PROVISIONAL = re.compile(r"\bL-?1\b|\blowest bidder\b|\bletter of intent\b|\bLoI\b|\bselected as\b|\bshortlisted\b", re.I)
_NON_BINDING = re.compile(r"\bMoU\b|\bmemorandum of understanding\b|\bframework agreement\b|\bin (?:advanced )?discussions?\b|"
                          r"\bpipeline\b|\benquir(?:y|ies)\b|\bbids? submitted\b", re.I)
_DISTINCT = re.compile(r"\b(?:repeat|fresh|another|additional|second|third|new|follow[- ]on) (?:order|contract|purchase order)\b", re.I)
_COUNTERPARTY_NAMED = re.compile(
    r"\b(?:from|by|with)\s+(?:M/s\.?\s+)?((?:[A-Z][A-Za-z0-9&.\-]*|of|and|the)(?:\s+(?:[A-Z][A-Za-z0-9&.\-]*|of|and|the|\(India\))){0,7})")
_COUNTERPARTY_UNNAMED = re.compile(
    r"\b(?:a|an|one of the)\s+(?:leading|large|major|reputed|marquee|prestigious|global|domestic|international|"
    r"fortune \d+|european|us[- ]based|government)\b[^.,;]{0,40}?\b(?:customer|client|oem|player|company|utility|psu|entity)\b", re.I)
_SEGMENT = re.compile(r"\b(?:in|for|from) (?:the|our) ([a-z][a-z &/-]{2,40}?) (?:segment|division|business|vertical)\b", re.I)

_GUIDANCE_METRIC = {
    Metric.REVENUE: Metric.REVENUE_GUIDANCE,
    Metric.EBITDA_MARGIN: Metric.MARGIN_GUIDANCE,
}
_GROWTH_WORDS = re.compile(r"\b(?:growth|grow|cagr|increase)\b", re.I)
_CAPITALISED_STOPWORDS = {"The", "This", "We", "Our", "It", "In", "For", "During", "Company", "Board", "Management"}


def _clean_counterparty(raw: str) -> str:
    words = raw.strip().split()
    while words and words[-1].lower() in {"of", "and", "the"}:
        words.pop()
    name = " ".join(words).strip(" .,")
    if not name or name.split()[0] in _CAPITALISED_STOPWORDS:
        return ""
    if re.fullmatch(r"(?:Rs|INR|FY\d*|Q\d|Crore|Lakh|Ministry)", name.split()[0]):
        return ""
    return name


def _negated_near(s: str, cue: re.Match) -> bool:
    """Negation scopes over a cue only when it sits immediately around it.

    "we do not expect", "order has not been received", "revenue was not ..."
    are negated; "revenue grew 40%, with no single customer above 10%" is not.
    """
    before = s[max(0, cue.start() - 40):cue.start()]
    after = s[cue.end():cue.end() + 8]
    neg_before = list(_NEGATION.finditer(before))
    if neg_before:
        # no clause boundary between the negation and the cue
        tail = before[neg_before[-1].end():]
        if not re.search(r"[,;:]|\b(?:but|while|whereas|although)\b", tail):
            return True
    return bool(re.match(r"\s*(?:not|n't)\b", after))


def classify_modality(s: str) -> Modality:
    fwd = _FORWARD.search(s)
    real = _REALIZED.search(s)
    for cue in (fwd, real):
        if cue and _negated_near(s, cue):
            return Modality.NEGATED
    if fwd:
        return Modality.CONDITIONAL if _CONDITIONAL.search(s) else Modality.FORWARD
    if _CONDITIONAL.search(s):
        return Modality.CONDITIONAL
    return Modality.REALIZED


def _evidence_id(doc_id: str, quote: str, metric: Metric) -> str:
    return hashlib.sha1(f"{doc_id}|{metric.value}|{quote}".encode()).hexdigest()[:16]


_WORDLIKE = re.compile(r"^[A-Za-z][a-z]*[aeiouy][a-z]*[.,;:!?)]*$|^[A-Z]{2,6}[.,;:)]*$", re.I)


def is_garbled(text: str) -> bool:
    """True for PDF text that has decomposed into letters/fragments, e.g.
    "D c I o T m ! , E w D e b F" (letter-spaced or mis-encoded fonts)."""
    toks = [t for t in text.split() if not re.fullmatch(r"[\d.,%()₹/-]+", t)]
    if len(toks) < 5:
        return False
    singles = sum(1 for t in toks if len(t.strip(".,;:!?()")) <= 1)
    wordlike = sum(1 for t in toks if _WORDLIKE.match(t))
    return singles / len(toks) > 0.35 or wordlike / len(toks) < 0.5


def garbled_ratio(text: str) -> float:
    lines = [l for l in text.split("\n") if len(l.split()) >= 5]
    return (sum(1 for l in lines if is_garbled(l)) / len(lines)) if lines else 0.0


def extract_sentence_evidence(doc: SourceDocument, chunks: list[Chunk]) -> list[Evidence]:
    """Deterministic evidence from prose (tables are handled by the results parser)."""
    out: list[Evidence] = []
    seen = set()
    for c in chunks:
        if c.kind == "table":
            continue
        for s in sentences(c):
            s_clean = re.sub(r"\s+", " ", s).strip()
            if len(s_clean) < 12 or is_garbled(s_clean):
                continue
            metric = next((m for pat, m in _METRIC_CUES if pat.search(s_clean)), None)
            if metric is None:
                continue
            modality = classify_modality(s_clean)
            qty = None
            if metric in (Metric.EBITDA_MARGIN, Metric.UTILIZATION):
                qty = parse_percent(s_clean)
            elif metric == Metric.CAPACITY:
                m = CAPACITY_QTY.search(s_clean)
                if m:
                    unit = {"mw": Unit.MW, "gw": Unit.GW, "units": Unit.UNITS}.get(m.group(2).lower(), Unit.TONNES_PER_ANNUM)
                    qty = Quantity(_num(m.group(1)), unit, m.group(0))
            else:
                qty = parse_inr(s_clean) or parse_usd(s_clean)
                if qty is None and metric == Metric.REVENUE and _GROWTH_WORDS.search(s_clean):
                    qty = parse_percent(s_clean)
            labels = find_period_labels(s_clean)

            tier, strength, out_metric = EvidenceTier.REALIZED_EXECUTION, CommitmentStrength.NOT_APPLICABLE, metric
            if metric == Metric.ORDER_WIN:
                tier = EvidenceTier.COMMERCIAL_COMMITMENT
                if _NON_BINDING.search(s_clean):
                    strength = CommitmentStrength.NON_BINDING
                    tier = EvidenceTier.MANAGEMENT_ASSERTION
                elif _PROVISIONAL.search(s_clean):
                    strength = CommitmentStrength.PROVISIONAL
                elif _BINDING.search(s_clean):
                    strength = CommitmentStrength.BINDING
                else:
                    strength = CommitmentStrength.PROVISIONAL
                if modality in (Modality.FORWARD, Modality.CONDITIONAL):
                    tier = EvidenceTier.MANAGEMENT_ASSERTION
            elif modality in (Modality.FORWARD, Modality.CONDITIONAL):
                tier = EvidenceTier.MANAGEMENT_ASSERTION
                out_metric = _GUIDANCE_METRIC.get(metric, metric)
                if out_metric == Metric.REVENUE_GUIDANCE and qty and qty.unit == Unit.PERCENT:
                    out_metric = Metric.REVENUE_GROWTH_GUIDANCE
            elif modality == Modality.NEGATED:
                tier = EvidenceTier.MANAGEMENT_ASSERTION if _FORWARD.search(s_clean) else EvidenceTier.REALIZED_EXECUTION
                if _FORWARD.search(s_clean):
                    out_metric = _GUIDANCE_METRIC.get(metric, metric)
                    if out_metric == Metric.REVENUE_GUIDANCE and qty and qty.unit == Unit.PERCENT:
                        out_metric = Metric.REVENUE_GROWTH_GUIDANCE

            # Without a quantity, revenue/EBITDA/PAT prose is too vague to use.
            if qty is None and out_metric in (Metric.REVENUE, Metric.EBITDA, Metric.PAT, Metric.EBITDA_MARGIN,
                                              Metric.UTILIZATION, Metric.ORDER_BOOK, Metric.ORDER_INFLOW):
                continue

            cp_named, cp = False, ""
            if metric in (Metric.ORDER_WIN, Metric.ORDER_INFLOW):
                m = _COUNTERPARTY_NAMED.search(s_clean)
                if m:
                    cp = _clean_counterparty(m.group(1))
                    cp_named = bool(cp)
                if not cp:
                    mu = _COUNTERPARTY_UNNAMED.search(s_clean)
                    if mu:
                        cp = mu.group(0)
            seg = _SEGMENT.search(s_clean)

            target_label = ""
            if tier == EvidenceTier.MANAGEMENT_ASSERTION and labels:
                target_label = labels[-1]
            ev = Evidence(
                evidence_id=_evidence_id(doc.doc_id, s_clean, out_metric),
                doc_id=doc.doc_id, ticker=doc.ticker, metric=out_metric, tier=tier, modality=modality,
                quote=s_clean, available_at=doc.available_at, page=c.page, chunk_id=c.chunk_id,
                quantity=qty, period_label=labels[0] if labels and not target_label else "",
                target_period_label=target_label, scope=_scope_from(s_clean),
                segment=seg.group(1).strip() if seg else "",
                counterparty=cp, counterparty_named=cp_named, commitment_strength=strength,
                distinct_marker=bool(_DISTINCT.search(s_clean)),
            )
            if ev.evidence_id in seen:
                continue
            seen.add(ev.evidence_id)
            out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Optional constrained LLM extractor
# ---------------------------------------------------------------------------

class LLMDisabled(RuntimeError):
    pass


LLM_PROMPT = """You extract dated, quotable earnings evidence from ONE chunk of an
Indian company filing. Return ONLY a JSON array. Each item:
{{"metric": one of {metrics}, "quote": exact verbatim sentence from the chunk,
 "modality": "realized"|"forward"|"conditional"|"negated",
 "value": number or null, "unit": "INR_crore"|"percent"|"USD_mn"|null,
 "period_label": e.g. "Q2FY25" or "", "counterparty": name or ""}}
Do not paraphrase quotes. Do not infer numbers that are not written. Do not give
opinions, ratings or recommendations. If nothing qualifies return [].

CHUNK (doc {doc_id}, page {page}):
{text}
"""


class ConstrainedLLMExtractor:
    def __init__(self, complete: Optional[Callable[[str], str]], budget, enabled: bool = False,
                 est_tokens_per_call: int = 2500, model: str = ""):
        self._complete = complete
        self._budget = budget
        self._enabled = enabled
        self._est = est_tokens_per_call
        self._model = model

    def extract(self, doc: SourceDocument, chunks: Iterable[Chunk]) -> tuple[list[Evidence], list[str]]:
        if not self._enabled or self._complete is None:
            raise LLMDisabled("LLM extraction is disabled (llm.enabled=false or no client injected)")
        out, rejected = [], []
        for c in chunks:
            prompt = LLM_PROMPT.format(metrics=[m.value for m in Metric], doc_id=doc.doc_id, page=c.page,
                                       text=(c.header + "\n" + c.text) if c.header else c.text)
            cached = self._budget.cache_get(prompt)
            if cached is None:
                res = self._budget.reserve(self._est)
                raw = self._complete(prompt)
                self._budget.commit(res, actual_tokens=self._est)
                self._budget.cache_put(prompt, raw)
            else:
                raw = cached
            try:
                items = json.loads(raw)
                assert isinstance(items, list)
            except Exception:
                rejected.append(f"{c.chunk_id}: non-JSON response")
                continue
            src_norm = re.sub(r"\s+", " ", c.header + " " + c.text)
            for it in items:
                try:
                    quote = re.sub(r"\s+", " ", str(it["quote"])).strip()
                    metric = Metric(it["metric"])
                    modality = Modality(it.get("modality", "realized"))
                except Exception:
                    rejected.append(f"{c.chunk_id}: schema violation")
                    continue
                if not quote or quote not in src_norm:
                    rejected.append(f"{c.chunk_id}: quote not verbatim")
                    continue
                qty = None
                if it.get("value") is not None and it.get("unit"):
                    try:
                        qty = Quantity(float(it["value"]), Unit(it["unit"]), raw=quote)
                    except Exception:
                        rejected.append(f"{c.chunk_id}: bad quantity")
                        continue
                    # the number must literally appear in the quote
                    if not re.search(re.escape(f"{qty.value:g}").replace(r"\.", r"[.,]?"), quote.replace(",", "")):
                        rejected.append(f"{c.chunk_id}: value not present in quote")
                        continue
                tier = (EvidenceTier.MANAGEMENT_ASSERTION if modality in (Modality.FORWARD, Modality.CONDITIONAL)
                        else EvidenceTier.COMMERCIAL_COMMITMENT if metric == Metric.ORDER_WIN
                        else EvidenceTier.REALIZED_EXECUTION)
                out.append(Evidence(
                    evidence_id=_evidence_id(doc.doc_id, quote, metric) + "L", doc_id=doc.doc_id,
                    ticker=doc.ticker, metric=metric, tier=tier, modality=modality, quote=quote,
                    available_at=doc.available_at, page=c.page, chunk_id=c.chunk_id, quantity=qty,
                    period_label=str(it.get("period_label") or ""), counterparty=str(it.get("counterparty") or ""),
                    counterparty_named=bool(it.get("counterparty")), extractor="llm",
                ))
        return out, rejected
