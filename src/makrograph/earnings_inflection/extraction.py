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
from dataclasses import replace
from datetime import date, datetime
from typing import Callable, Iterable, Optional

from .chunking import sentences, split_numeric_row
from .contracts import (
    COMMITMENT_RANK, Chunk, CommitmentStrength, DocumentKind, EventStage, Evidence, EvidenceTier, FinancialMeasurement, Metric,
    Modality, Quantity, RelationshipStatus, Scope, SourceDocument, TaxBasis, Unit, ValueBasis,
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
    r"^\s*(?:(?:\(?(?:[ivxlIVXL]{1,5}|\d{1,2}|[a-zA-Z])\s*[.):]|(?:[ivxlIVXL]{1,5}|\d{1,2})\s|"
    r"[A-Z]\s+(?=[A-Z][a-z]))\s*|"     # SEBI layout letter column: "A      Revenue from operations"
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
    # cost of goods (WP5 gross margin) and equity issuance (WP5 funding source)
    (re.compile(r"cost\s+of\s+(?:raw\s+)?materials?\s+consumed|raw\s+materials?\s+consumed|"
                r"consumption\s+of\s+raw\s+materials?", re.I), Metric.COST_OF_MATERIALS),
    (re.compile(r"purchases?\s+of\s+(?:stock|traded\s+goods)", re.I), Metric.PURCHASES_STOCK),
    (re.compile(r"changes?\s+in\s+inventor", re.I), Metric.INVENTORY_CHANGE),
    (re.compile(r"proceeds\s+from\s+(?:the\s+)?(?:issue|issuance|allotment)\s+of\s+(?:equity\s+)?(?:shares?|share\s+capital|"
                r"warrants?|convertible\s+warrants?)|(?:share|equity)\s+warrants?\s+(?:money|subscription)", re.I),
     Metric.EQUITY_ISSUED),
    (re.compile(r"ebitda\b(?!\s*margin)|earnings\s+before\s+interest,?\s+tax(?:es)?,?\s+depreciation", re.I),
     Metric.EBITDA),
    (re.compile(r"depreciation", re.I), Metric.DEPRECIATION),
    (re.compile(r"finance\s+costs?\b|interest\s+(?:and\s+finance\s+)?(?:costs?|expenses?)\b", re.I), Metric.FINANCE_COST),
    (re.compile(rf"(?:\(\s*loss\s*\)\s*/\s*)?{_P}\s*before\s+exceptional\s+(?:items?\s+)?(?:and|&)\s+tax", re.I),
     Metric.PBT_PRE_EXCEPTIONAL),
    (re.compile(r"exceptional\s+items?\b", re.I), Metric.EXCEPTIONAL_ITEMS),
    (re.compile(rf"(?:\(\s*loss\s*\)\s*/\s*)?{_P}\s*before\s+tax", re.I), Metric.PBT),
    (re.compile(r"(?:total\s+)?(?:income\s+)?tax\s+expenses?\b", re.I), Metric.TAX),
    (re.compile(rf"(?:net\s+)?{_P}\s*(?:after\s+tax|for\s+the\s+(?:period|year|quarter|half))", re.I), Metric.PAT),
    (re.compile(r"(?:owners|equity\s+(?:share)?holders|shareholders)\s+of\s+the\s+(?:company|parent|holding)", re.I),
     Metric.PAT_ATTRIBUTABLE),
    (re.compile(r"non[- ]controlling\s+interests?\b", re.I), Metric.NCI_PROFIT),
    (re.compile(r"(?:paid[- ]up\s+)?equity\s+share\s+capital\b", re.I), Metric.PAID_UP_CAPITAL),
    # cash-flow statement
    (re.compile(r"net\s+cash\b.{0,70}\boperating\s+activities", re.I), Metric.OPERATING_CASH_FLOW),
    (re.compile(r"net\s+cash\b.{0,70}\binvesting\s+activities", re.I), Metric.INVESTING_CASH_FLOW),
    (re.compile(r"net\s+cash\b.{0,70}\bfinancing\s+activities", re.I), Metric.FINANCING_CASH_FLOW),
    (re.compile(r"net\s+(?:increase|decrease|change)\b.{0,40}\bcash\s+and\s+cash\s+equivalents", re.I),
     Metric.NET_CHANGE_IN_CASH),
    # balance sheet (instant "As at" columns)
    (re.compile(r"cash\s+and\s+cash\s+equivalents\b(?!.{0,40}\b(?:beginning|opening|start)\b)", re.I), Metric.CASH),
    (re.compile(r"trade\s+receivables\b", re.I), Metric.RECEIVABLES),
    (re.compile(r"inventories\b", re.I), Metric.INVENTORIES),
    (re.compile(r"total\s+equity\s+and\s+liabilities\b", re.I), Metric.TOTAL_EQUITY_AND_LIABILITIES),
    (re.compile(r"total\s+assets\b", re.I), Metric.TOTAL_ASSETS),
]
# Short labels used by results highlight tables ("Revenues (in Rs Cr)", "PAT", "Net Profit").
# Accepted only in a company-level (consolidated / standalone) table outside investor
# presentations, where such labels often belong to a segment or product slide.
_HIGHLIGHT_ROWS = [
    (re.compile(r"(?:total\s+|net\s+)?(?:revenues?|sales)\s*(?:\(|$)", re.I), Metric.REVENUE),
    (re.compile(r"(?:pat|net\s+profit|profit\s+after\s+tax)\s*(?:\(|$)", re.I), Metric.PAT),
    (re.compile(r"(?:pbt|profit\s+before\s+tax)\s*(?:\(|$)", re.I), Metric.PBT),
]
_BORROWINGS = re.compile(r"(?:\(?[ivx]+\)?\s*)?(?:long[- ]term\s+|short[- ]term\s+)?borrowings\b", re.I)
_FACE_VALUE = re.compile(r"face\s+value\s*(?:of\s*)?(?:rs\.?|₹|inr|re\.?)?\s*([\d.]+)", re.I)
_SECTION = [
    (re.compile(r"non[- ]current\s+liabilities", re.I), "nc_liab"),
    (re.compile(r"\bcurrent\s+liabilities", re.I), "c_liab"),
    (re.compile(r"non[- ]current\s+assets", re.I), "nc_assets"),
    (re.compile(r"\bcurrent\s+assets", re.I), "c_assets"),
    (re.compile(r"\bequity\b(?!.{0,10}share)", re.I), "equity"),
]
_TAX_PARTS = re.compile(r"(?:current\s+tax|deferred\s+tax|(?:tax\s+(?:in\s+respect\s+of|relating\s+to|for)\s+)?"
                        r"(?:earlier|prior)\s+(?:years?|periods?))\b", re.I)
_COMPREHENSIVE = re.compile(r"comprehensive\s+income", re.I)
# Segment reporting: rows such as "Total income from operations" there are segment
# totals, never the company's own revenue / profit lines.
_SEGMENT_SECTION = re.compile(r"segment\s*[-‐]?\s*(?:wise\s*)?(?:revenue|results?|information|reporting|assets|"
                              r"liabilities)|(?:revenue|results?|information)\s*by\s*(?:business\s*)?segments?", re.I)

_CELL = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")


_NOT_AMOUNT_ROW = re.compile(r"\beps\b|earnings\s*per\s*share|\bbasic\b|\bdiluted\b|face\s*value|par\s*value|"
                             r"\bmargins?\b|\bratios?\b|%|\bper\s+share|no\.?\s*of\s*shares|number\s+of\s+shares", re.I)


def _decimal_convention(lines: list[str]) -> int:
    """Decimals a table prints its amounts with (-1 when it has no clear convention)."""
    from collections import Counter
    counts: Counter = Counter()
    grouped_integers = grouped_three_decimals = 0
    for l in lines:
        label, cells = split_numeric_row(l)
        if _NOT_AMOUNT_ROW.search(label):
            continue            # per-share figures, margins, ratios are printed differently
        for cell in cells:
            core = cell.strip("()-")
            if _CELL.fullmatch(cell) and sum(ch.isdigit() for ch in core) >= 3:
                counts[len(core.split(".")[1]) if "." in core else 0] += 1
                if "," in core and "." not in core:
                    grouped_integers += 1          # "14,467"
                elif "," in core and len(core.split(".")[1]) == 3:
                    grouped_three_decimals += 1    # "1,234.567": a genuine 3-decimal amount
    total = sum(counts.values())
    if total < 10:
        return 0 if total >= 3 and counts[0] == total else -1
    decimal = [(n, k) for k, n in counts.items() if k > 0]
    if decimal:
        n, k = max(decimal)
        if (k == 3 and not grouped_three_decimals and grouped_integers >= 3
                and grouped_integers >= 0.2 * total):
            # an integer table whose scan turned thousands commas into dots ("17.511" next to
            # "14,467"): a table printed with 3 decimals has no comma-grouped whole amounts
            return 0
        # amounts printed with k decimals (a scan that lost many decimal points still shows 20%+)
        if n >= 0.2 * total:
            return k
    return 0 if counts[0] >= 0.7 * total else -1      # -1 = no clear convention in this table


def _integer_table_cells(cells: list[str]) -> list[str]:
    """In a table printed in whole amounts, "(1.076)" / "17.511" are scanned "(1,076)" / "17,511"."""
    return [re.sub(r"^(\(?-?\d{1,3})\.(\d{3}\)?)$", r"\1,\2", x) for x in cells]


def _cell_value(s: str) -> float:
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    v = float(s.strip("()-").replace(",", ""))
    return -v if neg else v


_SCALE_WORD = r"(crores?|cr|lakhs?|lacs?|million|mn|thousands?)"
_SCALE_PATTERNS = [
    re.compile(rf"(?:rs\.?|inr|₹|rupees|amounts?|figures)[^\n]{{0,30}}?\b{_SCALE_WORD}\b", re.I),
    re.compile(rf"\(\s*in\s+{_SCALE_WORD}\b", re.I),
    # text extracted without spaces between words: "(Rs.inlakhs)", "(₹incrore)"
    re.compile(rf"\((?:rs\.?|inr|₹|amounts?)\s*in\s*{_SCALE_WORD}\b", re.I),
    # unit in a highlights-table label: "Revenues (in Rs Cr)", "PAT (in RsCr)", "(₹ Mn)"
    re.compile(rf"\(\s*(?:in\s*)?(?:rs\.?|inr|₹)\s*{_SCALE_WORD}\b", re.I),
]


_LOOSE_UNIT = re.compile(r"(?:rs|inr|₹)\.?\s*in\s*(crores?|cr|lakhs?|lacs?|million|mn|thousands?)", re.I)


def _nearest_scale(text: str) -> Optional[float]:
    """Unit stated by the LAST unit line in ``text`` (the one closest to the table below it).
    Short scanned unit lines are matched loosely ("fRs.inlakhst")."""
    for line in reversed(text.split("\n")):
        sc = _table_scale(line, "")
        if sc is None and len(line.strip()) <= 40:
            m = _LOOSE_UNIT.search(line)
            if m:
                sc = _table_scale(f"(in {m.group(1)})", "")
        if sc is not None:
            return sc
    return None


def _single_document_scale(page_text: str) -> Optional[float]:
    """The unit of a document whose unit lines all agree; None when they differ or are absent."""
    found = {sc for line in page_text.split("\n") for sc in [_nearest_scale(line)] if sc is not None}
    return found.pop() if len(found) == 1 else None


def _label_scale(label: str) -> Optional[float]:
    """A unit stated in the row label itself ("Revenues (in Rs Cr)")."""
    return _table_scale(label, "") if "(" in label else None


def _table_scale(header: str, page_text: str) -> Optional[float]:
    for src in (header, page_text):
        for pat in _SCALE_PATTERNS:
            m = pat.search(src)
            if not m:
                continue
            w = m.group(1).lower()
            if w.startswith("crore") or w == "cr":
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
    # the nearest statement title ABOVE this table (documents whose text has no page breaks
    # hold the standalone and the consolidated statement on one "page")
    first = next((l for l in c.text.split("\n") if l.strip()), "")
    pos = page.find(first) if first else -1
    if pos > 0:
        for line in reversed(_SCOPE_TITLE.findall(page[max(0, pos - 4000):pos])):
            t = line.lower()
            has_c, has_s = "consolidated" in t, "standalone" in t
            if has_c != has_s:
                return Scope.CONSOLIDATED if has_c else Scope.STANDALONE
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


def _despaced(pattern: re.Pattern) -> re.Pattern:
    """The same row pattern for text extracted without spaces between words
    ("Revenuefromoperations", "Profit/(Loss)beforetax(C-D)")."""
    src = pattern.pattern
    for a, b in ((r"\s+", ""), (r"\s*", ""), (r"\b", ""), (" ", "")):
        src = src.replace(a, b)
    return re.compile(src, pattern.flags)


_ROW_METRICS_DESPACED = [(_despaced(p), m) for p, m in _ROW_METRICS]


def _looks_despaced(lab: str) -> bool:
    letters = sum(ch.isalpha() for ch in lab)
    return letters >= 8 and lab.count(" ") * 10 <= letters


def _match_despaced(lab: str) -> list[Metric]:
    flat = re.sub(r"^(?:[A-Za-z]|[ivxlIVXL]{1,4}|\d{1,2})[.)]?\s+", "", lab.strip())
    flat = re.sub(r"\s+", "", _strip_enumerator(flat))
    if re.search(r"basic", flat, re.I) and re.search(r"diluted", flat, re.I):
        return [Metric.BASIC_EPS, Metric.DILUTED_EPS]
    for pat, m in _ROW_METRICS_DESPACED:
        if pat.match(flat):
            return [m]
    return []


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
    # words run together by the PDF text layer: "Revenuefromoperations"
    return _match_despaced(label) if _looks_despaced(lab) else []


def _row_widths(lines: list[str]) -> set[int]:
    """Cell counts of the table's data rows (to check a borrowed header fits)."""
    from collections import Counter
    widths = Counter(len(split_numeric_row(l)[1]) for l in lines)
    widths.pop(0, None)
    return {w for w, k in widths.items() if k >= 2}


_CHANGE_COL = re.compile(r"^(?:yoy|qoq|y-?o-?y|q-?o-?q|growth|change|var(?:iance)?|chg)?\s*%?$", re.I)


def _expand_change_columns(header_lines: list[str], resolved: list) -> list:
    """Highlights tables interleave change columns with periods:
        "Particulars (Rs Mn) Q3FY24 Q3FY23 YoY Q2FY24 QoQ 9MFY24 9MFY23 YoY"
    Rebuild the positional column list with an untyped (unused) column for each change column."""
    for line in header_lines:
        toks = list(_COL_TOKEN.finditer(line))
        if len(toks) < 2:
            continue
        cols, last_end, matched = [], None, 0
        pieces = []
        for m in toks:
            if last_end is not None:
                pieces.append(("gap", line[last_end:m.start()]))
            pieces.append(("tok", m.group(0)))
            last_end = m.end()
        pieces.append(("gap", line[last_end:]))
        it = iter(resolved)
        for kind, text in pieces:
            if kind == "tok":
                d = _column_tokens(text)
                if not d:
                    continue
                nxt = next(it, None)
                if nxt is None or nxt[0] != d[0][0]:
                    break
                cols.append(nxt)
                matched += 1
            else:
                for w in text.split():
                    if _CHANGE_COL.match(w.strip("()")) and cols:
                        cols.append((cols[-1][0], None))
        if matched == len(resolved) and len(cols) > len(resolved):
            return cols
    return []


_TOTAL_INCOME = re.compile(r"total\s*income\b", re.I)
_SECTION_HEADING = re.compile(r"(?:income|expenses|expenditure)", re.I)


def _reorient_split_rows(lines: list[str]) -> tuple[list[str], str]:
    """Scanned statements sometimes put each label and its values on separate lines, with the
    values either BELOW or ABOVE their label.  Pair them in the direction under which the
    statement's own identity holds (revenue + other income = total income, first column);
    if neither or both directions verify, the lines are returned unchanged (ambiguous)."""
    parsed = [(l,) + split_numeric_row(l) for l in lines]
    has_alpha = lambda t: bool(re.search(r"[A-Za-z]", t))  # noqa: E731
    vals_only = [i for i, (_, lab, cells) in enumerate(parsed) if cells and not has_alpha(lab)]
    if len(vals_only) < 3:
        return lines, ""

    def pairing(direction: int) -> dict[int, int]:
        out = {}
        for i in vals_only:
            j = i + direction
            if 0 <= j < len(parsed) and not parsed[j][2] and has_alpha(parsed[j][1]) and j not in out:
                out[j] = i
        return out

    def first_value(i: int) -> Optional[float]:
        try:
            return _cell_value(parsed[i][2][0]) if _CELL.fullmatch(parsed[i][2][0]) else None
        except ValueError:
            return None

    def verifies(mapping: dict[int, int]) -> bool:
        idx = {}
        for j, i in mapping.items():
            lab = parsed[j][1]
            if _TOTAL_INCOME.search(lab):
                idx.setdefault("total", i)
            elif Metric.REVENUE in _match_metrics(lab):
                idx.setdefault("rev", i)
            elif Metric.OTHER_INCOME in _match_metrics(lab):
                idx.setdefault("oi", i)
        if len(idx) < 3:
            return False
        r, o, t = (first_value(idx[k]) for k in ("rev", "oi", "total"))
        return None not in (r, o, t) and abs(r + o - t) <= max(0.02, 0.005 * abs(t))

    ok = [d for d in (-1, +1) if verifies(pairing(d))]
    if len(ok) != 1:
        return lines, ""
    mapping = pairing(ok[0])
    used = set(mapping.values())
    out = []
    for k, (l, lab, cells) in enumerate(parsed):
        if k in used:
            continue
        out.append(f"{lab} {' '.join(parsed[mapping[k]][2])}" if k in mapping else l)
    return out, ("values printed above their labels" if ok[0] == 1 else "values printed below their labels")


def parse_results_tables(doc: SourceDocument, chunks: list[Chunk],
                         unscaled: Optional[list[FinancialMeasurement]] = None,
                         ) -> tuple[list[FinancialMeasurement], list[str]]:
    """Parse results statements into measurements.  Returns (rows, issues).

    Amounts of a table whose unit line is missing or unreadable are never used as crore.
    When ``unscaled`` is given they are collected there with their printed values
    (``value`` = the figure as printed); ``infer_unstated_scales`` may later scale them
    from the issuer's earlier filings."""
    out: list[FinancialMeasurement] = []
    issues: list[str] = []
    page_texts = (doc.pages or (doc.text or "").split("\f"))
    doc_conv = max(0, _decimal_convention([l for c in chunks if c.kind == "table" for l in c.text.split("\n")]))
    prev_tail: list[str] = []
    prev_page = 0
    for c in chunks:
        # lines just before this chunk on the same page: a period-header line can be cut off
        # into its own chunk by a long "(Unaudited) (Audited)" line in between
        before = prev_tail if prev_page == c.page else []
        prev_tail = ([l for l in c.header.split("\n") if l.strip()] + [l for l in c.text.split("\n") if l.strip()])[-8:]
        prev_page = c.page
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
        if (not resolved or col_issue) and before and _row_widths(lines):
            ctx, ctx_issue = resolve_columns(before + c.header.split("\n") + lines[:4])
            if ctx and not ctx_issue and len(ctx) in _row_widths(lines):
                resolved, col_issue = ctx, ctx_issue
                c = Chunk(c.chunk_id, c.doc_id, c.page, c.ordinal, c.kind, c.text,
                          header="\n".join(before + [c.header]), char_start=c.char_start, char_end=c.char_end)
        widths = _row_widths(lines)
        if resolved and widths and len(resolved) not in widths:
            expanded = _expand_change_columns(c.header.split("\n") + lines[:3], resolved)
            if expanded and len(expanded) in widths:
                resolved, col_issue = expanded, ""
        if widths and (not resolved or col_issue or len(resolved) not in widths):
            # header dates unreadable (scanned "31-Pec-22", "Decomber3l", "203"): use the fixed
            # SEBI Regulation 33 column layout, anchored on the dates that did read or on the
            # statement title, and only when it has exactly the width of the data rows
            hdr = (before if prev_page == c.page else []) + c.header.split("\n") + lines[:4]
            layout = _sebi_layout_from_date_bag(hdr)
            basis = "period dates in the header"
            if not layout or len(layout) not in widths:
                layout = _sebi_layout_from_title("\n".join(hdr) + "\n" + page_text[:6000], hdr)
                basis = "the statement title"
            if layout and len(layout) in widths:
                resolved, col_issue = layout, ""
                issues.append(f"{doc.doc_id}:p{c.page}: period columns inferred from the SEBI results layout "
                              f"and {basis} (header dates unreadable)")
        if not resolved:
            continue
        if col_issue:
            issues.append(f"{doc.doc_id}:p{c.page}: {col_issue}")
        cols = [d for d, _ in resolved]
        ptypes = [t for _, t in resolved]
        n = len(cols)
        scale = _table_scale(c.header, "")
        if scale is None:
            # nearest unit line ABOVE this table; a document without page breaks holds several
            # statements, possibly in different units
            pos = page_text.find(lines[0]) if lines else -1
            above = page_text[max(0, pos - 3000):pos] if pos > 0 else ""
            scale = _nearest_scale(above) or _single_document_scale(page_text)
        scope = _resolve_scope(c, page_texts)
        col_scopes = [scope] * n
        if n >= 6 and n % 2 == 0 and resolved[: n // 2] == resolved[n // 2:]:
            # side-by-side statements: the column-group line names the order ("Consolidated  Standalone")
            first_row = next((l for l in lines if split_numeric_row(l)[1] and len(split_numeric_row(l)[1]) >= n), "")
            pos = page_text.find(first_row) if first_row else -1
            above = page_text[max(0, pos - 1500):pos] if pos > 0 else c.header
            group_lines = [l.lower() for l in above.split("\n")
                           if "consolidated" in l.lower() and "standalone" in l.lower()
                           and not _TITLE_LINE.search(l)]
            head = group_lines[-1] if group_lines else ""
            ci, si = head.find("consolidated"), head.find("standalone")
            if ci >= 0 and si >= 0:
                first, second = ((Scope.CONSOLIDATED, Scope.STANDALONE) if ci < si
                                 else (Scope.STANDALONE, Scope.CONSOLIDATED))
                col_scopes = [first] * (n // 2) + [second] * (n // 2)
            else:
                issues.append(f"{doc.doc_id}:p{c.page}: two statements side by side without scope labels; not used")
                continue
        highlights_ok = (doc.kind != DocumentKind.INVESTOR_PRESENTATION
                         and len(set(col_scopes)) == 1 and col_scopes[0] != Scope.UNKNOWN)
        if scale is None and not any(_label_scale(l) for l in lines):
            issues.append(f"{doc.doc_id}:p{c.page}: results table without unit line; amounts not used")
        found: dict[Metric, list] = {}
        tax_parts: list[list[Optional[float]]] = []
        pending, after_comprehensive, misaligned, section, face_value = "", False, 0, "", None
        in_segment = bool(_SEGMENT_SECTION.search("\n".join(c.header.split("\n")[-3:])))
        seg_state: dict = {"kind": None, "heading": "", "rows": {}}
        if in_segment:
            for hl in c.header.split("\n")[-4:]:
                _segment_row(hl, [], seg_state, n)
        last_values_only = False
        total_income = None
        displaced = False
        lines, reoriented = _reorient_split_rows(lines)
        if reoriented:
            issues.append(f"{doc.doc_id}:p{c.page}: labels and values on separate lines ({reoriented}); "
                          "paired using revenue + other income = total income")
        conv = _decimal_convention(lines)
        if conv < 0:
            conv = doc_conv if doc_conv > 0 else 0   # damaged table: use the document's convention
        dropped_cells = 0
        for l in lines:
            label, cells = split_numeric_row(l)
            values_only_above, last_values_only = last_values_only, bool(cells) and not re.search(r"[A-Za-z]", label)
            if _SEGMENT_SECTION.search(label):
                in_segment = True
            if in_segment:
                _segment_row(label, cells, seg_state, n)
                continue
            if _COMPREHENSIVE.search(label):
                after_comprehensive = True
            fv = _FACE_VALUE.search(label)
            if fv:
                try:
                    face_value = float(fv.group(1).rstrip("."))
                except ValueError:
                    pass
            if not cells:
                for pat, name in _SECTION:
                    if pat.search(label) and len(label) < 60:
                        section = name
                        break
                # a label wrapped onto the next line ("Revenue from" / "operations 1,234 ...").
                # When a values-only line sits ABOVE this label, labels may follow their values
                # (scrambled text layer): which values belong to the label is then ambiguous.
                pending = label if len(label) < 80 and not values_only_above else ""
                continue
            if _BORROWINGS.match(_strip_enumerator(label)):
                bm = {"nc_liab": Metric.BORROWINGS_NONCURRENT, "c_liab": Metric.BORROWINGS_CURRENT}.get(section)
                if bm is None:
                    issues.append(f"{doc.doc_id}:p{c.page}: borrowings row outside a current/non-current "
                                  "liabilities section; not used")
                    continue
                label = f"__{bm.value}__ {label}"
            if cells and _SECTION_HEADING.fullmatch(_strip_enumerator(label).strip(" |:.-")):
                displaced = True      # "Income 18,589.83 ...": figures pushed onto a heading line
            if (_TOTAL_INCOME.match(_strip_enumerator(label)) and total_income is None
                    and not _COMPREHENSIVE.search(label) and len(cells) in (n, n + 1)):
                ti_cells = _integer_table_cells(cells[-n:]) if conv == 0 else cells[-n:]
                total_income = [(_cell_value(x) if _CELL.fullmatch(x) else None) for x in ti_cells]
            is_tax_part = bool(_TAX_PARTS.match(_strip_enumerator(label)))
            # Join a wrapped label only when this line has no enumerator of its own:
            # "(1) Current tax" under "VIII Tax expense" is a sub-row, not a continuation.
            continuation = pending and not is_tax_part and _strip_enumerator(label) == label.strip()
            if label.startswith("__borrowings_"):
                metrics = [Metric(label.split("__")[1])]
            else:
                metrics = _match_metrics(label) or (_match_metrics(f"{pending} {label}") if continuation else [])
                highlight = False
                if not metrics and highlights_ok:
                    metrics = [m for pat, m in _HIGHLIGHT_ROWS if pat.match(_strip_enumerator(label))][:1]
                    highlight = bool(metrics)
            pending = ""
            if not metrics and not is_tax_part:
                continue
            if (Metric.PAT_ATTRIBUTABLE in metrics or Metric.NCI_PROFIT in metrics) and after_comprehensive:
                continue   # owners / NCI share of comprehensive income, not of profit
            if len(cells) < n:
                continue   # blank cells dropped by the PDF text; cannot align safely
            if len(cells) > n + 1:
                misaligned += 1  # more values than identified columns: header was mis-read
                continue
            if len(cells) == n + 1 and not re.fullmatch(r"\(?\d{1,2}(?:\.\d{1,2})?[a-z]?\)?", cells[0]):
                misaligned += 1  # an extra leading value that is not a note number: header mis-read
                continue
            cells = cells[-n:]   # one extra leading value = note reference column
            vals: list[Optional[float]] = []
            decimals = 0
            if conv == 0 and not _NOT_AMOUNT_ROW.search(label):
                cells = _integer_table_cells(cells)
            for cell in cells:
                if conv and "." not in cell and sum(ch.isdigit() for ch in cell) > conv + 1:
                    # a figure in a table printed with `conv` decimals that lost its decimal point
                    # (scanned "4733118" for "47,331.18"): unreadable, not 100x larger
                    vals.append(None)
                    dropped_cells += 1
                    continue
                try:
                    vals.append(_cell_value(cell) if _CELL.fullmatch(cell) else None)   # "-"/"nil"/"?" = blank
                except ValueError:
                    vals.append(None)
                if "." in cell:
                    decimals = max(decimals, len(cell.strip("()").split(".")[-1]))
            if is_tax_part and not metrics:
                tax_parts.append(vals)
                continue
            for m in metrics:
                if m not in found:            # first occurrence wins (later = sub-totals / OCI)
                    found[m] = [l, vals, decimals, _label_scale(label) or scale,
                                "reported_highlight" if highlight else "reported"]
        if displaced and (Metric.REVENUE in found or Metric.OTHER_INCOME in found):
            issues.append(f"{doc.doc_id}:p{c.page}: figures printed on a section heading line (labels displaced "
                          "in the text layer); revenue and other income not used")
            found.pop(Metric.REVENUE, None)
            found.pop(Metric.OTHER_INCOME, None)
        if total_income and Metric.REVENUE in found and Metric.OTHER_INCOME in found:
            # the statement's own identity: revenue + other income = total income, per column
            rv, oi = found[Metric.REVENUE][1], found[Metric.OTHER_INCOME][1]
            checks = [abs((r or 0) + (o or 0) - t) <= max(0.02, 0.005 * abs(t))
                      for r, o, t in zip(rv, oi, total_income) if t is not None and r is not None]
            if checks and sum(checks) < len(checks) / 2:
                issues.append(f"{doc.doc_id}:p{c.page}: revenue + other income != total income in "
                              f"{len(checks) - sum(checks)} of {len(checks)} columns (rows mis-read); "
                              "revenue and other income not used")
                found.pop(Metric.REVENUE)
                found.pop(Metric.OTHER_INCOME)
        if tax_parts and Metric.TAX not in found:
            summed = [None if all(p[i] is None for p in tax_parts) else sum(p[i] or 0.0 for p in tax_parts)
                      for i in range(n)]
            found[Metric.TAX] = ["(sum of current/deferred/earlier-year tax rows)", summed, 2, scale, "reported"]
        if dropped_cells:
            issues.append(f"{doc.doc_id}:p{c.page}: {dropped_cells} cell(s) without the table's decimal point "
                          "(scanning damage); treated as unreadable")
        if misaligned:
            issues.append(f"{doc.doc_id}:p{c.page}: {misaligned} row(s) had more values than the {n} "
                          f"identified period columns; skipped")
        for (kind, name), (line, vals, decimals) in seg_state["rows"].items():
            metric = Metric.SEGMENT_REVENUE if kind == "rev" else Metric.SEGMENT_RESULT
            for col_i, (d, v) in enumerate(zip(cols, vals)):
                if v is None or ptypes[col_i] is None or scale is None:
                    continue
                out.append(FinancialMeasurement(
                    ticker=doc.ticker, metric=metric, period_end=d, period_type=ptypes[col_i],
                    value=round(v * scale, 6), unit=Unit.INR_CRORE, scope=col_scopes[col_i], doc_id=doc.doc_id,
                    available_at=doc.available_at, display_unit=(10 ** -decimals) * scale,
                    quote=f"segment | {line.strip()}"[:400], segment=name))
        if face_value and Metric.PAID_UP_CAPITAL in found:
            found[Metric.FACE_VALUE] = [f"face value Rs {face_value:g} per share", [face_value] * n, 2, scale, "reported"]
        for metric, (line, vals, decimals, row_scale, source) in found.items():
            for col_i, (d, v) in enumerate(zip(cols, vals)):
                if v is None or ptypes[col_i] is None:
                    continue
                if metric in (Metric.DILUTED_EPS, Metric.BASIC_EPS, Metric.FACE_VALUE):
                    unit, val, du = Unit.INR_PER_SHARE, v, 10 ** -decimals
                else:
                    if row_scale is None:
                        if unscaled is not None:
                            unscaled.append(FinancialMeasurement(
                                ticker=doc.ticker, metric=metric, period_end=d, period_type=ptypes[col_i],
                                value=v, unit=Unit.INR_CRORE, scope=col_scopes[col_i], doc_id=doc.doc_id,
                                available_at=doc.available_at, source=source,
                                quote=f"{c.header.splitlines()[-1] if c.header else ''} | {line.strip()}"[:400],
                                display_unit=10 ** -decimals))
                        continue
                    unit, val = Unit.INR_CRORE, round(v * row_scale, 6)
                    du = (10 ** -decimals) * row_scale
                out.append(FinancialMeasurement(
                    ticker=doc.ticker, metric=metric, period_end=d, period_type=ptypes[col_i],
                    value=val, unit=unit, scope=col_scopes[col_i], doc_id=doc.doc_id, available_at=doc.available_at,
                    source=source,
                    quote=f"{c.header.splitlines()[-1] if c.header else ''} | {line.strip()}"[:400],
                    display_unit=du,
                ))
    return _reconcile_copies(out, issues, doc.doc_id), issues


# crore per printed unit: crore, lakh, million, thousand, rupee
_STANDARD_SCALES = (1.0, 0.01, 0.1, 1e-4, 1e-7)


def infer_unstated_scales(unscaled: list[FinancialMeasurement], scaled: list[FinancialMeasurement],
                          min_matches: int = 3, agreement: float = 0.8, tol: float = 0.01,
                          ) -> tuple[list[FinancialMeasurement], list[str]]:
    """Scale amounts printed without a readable unit line, from the issuer's own earlier filings.

    A filing's comparative columns repeat figures published before ("31-Dec-23" in a later
    statement).  If at least ``min_matches`` of its printed figures equal the same metric and
    period in filings available no later than it, and at least ``agreement`` of the compared
    figures agree on exactly ONE standard unit (crore, lakh, million, thousand, rupee),
    that unit is used.  Otherwise the figures stay unused.  Point-in-time: only reference
    figures available at or before the filing count."""
    from collections import defaultdict
    by_doc: dict[str, list[FinancialMeasurement]] = defaultdict(list)
    for r in unscaled:
        by_doc[r.doc_id].append(r)
    ref: dict[tuple, list[FinancialMeasurement]] = defaultdict(list)
    for r in scaled:
        if r.unit == Unit.INR_CRORE:
            ref[(r.ticker, r.metric, r.period_end, r.period_type)].append(r)
    out: list[FinancialMeasurement] = []
    issues: list[str] = []
    for doc_id, rows in by_doc.items():
        votes: dict[float, int] = defaultdict(int)
        compared = 0
        for r in rows:
            if not r.value:
                continue
            cands = [x for x in ref.get((r.ticker, r.metric, r.period_end, r.period_type), [])
                     if x.doc_id != doc_id and x.available_at and r.available_at
                     and x.available_at <= r.available_at
                     and (x.scope == r.scope or Scope.UNKNOWN in (x.scope, r.scope))]
            if not cands:
                continue
            compared += 1
            hits = {sc for sc in _STANDARD_SCALES for x in cands
                    if abs(r.value * sc - x.value) <= tol * abs(x.value)}
            if len(hits) == 1:
                votes[hits.pop()] += 1
        if not votes:
            continue
        sc, n = max(votes.items(), key=lambda kv: kv[1])
        if n < min_matches or n < agreement * compared or sum(votes.values()) - n > 0:
            issues.append(f"{doc_id}: unit line unreadable; earlier filings do not establish its unit "
                          f"({n} of {compared} comparative figures agree); amounts not used")
            continue
        name = {1.0: "crore", 0.01: "lakh", 0.1: "million", 1e-4: "thousand", 1e-7: "rupee"}[sc]
        issues.append(f"{doc_id}: unit line unreadable; unit inferred as {name} from {n} of {compared} "
                      "comparative figures matching earlier filings")
        for r in rows:
            out.append(replace(r, value=round(r.value * sc, 6), display_unit=r.display_unit * sc,
                               quote=f"[unit inferred: {name}] {r.quote}"[:400]))
    return out, issues


_SEG_REV = re.compile(r"segment\s*[-\s]?(?:wise\s*)?revenue|revenue\s*(?:by|from)\s*(?:business\s*)?segments?|"
                      r"segment\s*(?:wise\s*)?(?:sales|turnover)", re.I)
_SEG_RES = re.compile(r"segment\s*(?:wise\s*)?(?:results?|profit|ebit)|(?:results?|profit)\s*(?:by|from)\s*segments?|"
                      r"profit\s*/?\s*\(?loss\)?\s*before\s*(?:finance|interest|tax)", re.I)
_SEG_OTHER = re.compile(r"segment\s*(?:wise\s*)?(?:assets|liabilities)|capital\s*employed", re.I)
_SEG_SKIP = re.compile(r"^(?:total|less|add|inter[- ]?segment|unallocab|eliminat|revenue\s+from|income\s+from|net\s+(?:sales|income)|"
                       r"finance|interest|other\s+(?:un)?allocable|other\s+income|profit|exceptional|tax|segment)", re.I)
_SEG_PART = re.compile(r"^(?:manufactured|traded|domestic|exports?|india|overseas|external|internal)\b", re.I)


def _segment_name(label: str) -> str:
    """Clean segment label: enumerators and scanning debris removed ("l'l d\\ Others" -> "others")."""
    name = re.sub(r"[^a-z&/ -]", " ", label.lower())
    words = [w for w in name.split() if len(w) > 1 or w == "&"]
    while words and ((len(words[0]) <= 2 and words[0] not in ("it", "oe"))     # "(a)", "ii", scanned "l d"
                     or not re.search(r"[aeiouy]", words[0])):                   # scanned "lldl"
        words.pop(0)
    return " ".join(words).strip(" -&/")


def _is_segment_name(name: str, label: str) -> bool:
    return bool(name) and len(name) < 50 and bool(re.search(r"[a-z]{3}", name)) and not (
        _SEG_SKIP.match(name) or re.search(r"unallocab|un-allocab|expenditure|segm?en|sel.?ment|interest|\btax\b|"
                                           r"defer|profit|loss|before|after|finance|total", name)
        or sum(ch.isalpha() or ch == " " for ch in label.strip()) < 0.6 * len(label.strip())
        or re.search(r"\b[a-z]\b(?:\s+\b[a-z]\b)+", name))


def _segment_row(label: str, cells: list[str], st: dict, n: int) -> None:
    """Rows of a segment-reporting table: per-segment revenue and result.  Segment totals,
    inter-segment eliminations, unallocable items and sub-lines (manufactured / traded) are
    not segments.  A "Total" line directly under a named heading is that segment's total."""
    if _SEG_OTHER.search(label):
        st["kind"], st["heading"] = "other", ""
        return
    if _SEG_RES.search(label):
        st["kind"], st["heading"] = "res", ""
    elif _SEG_REV.search(label):
        st["kind"], st["heading"] = "rev", ""
    if st["kind"] not in ("rev", "res"):
        return
    name = _segment_name(label)
    if not cells:
        st["heading"] = name if (not _SEG_REV.search(label) and not _SEG_RES.search(label)
                                 and _is_segment_name(name, label)) else ""
        return
    if len(cells) < n or len(cells) > n + 1:
        return
    if re.match(r"total\b", name) and st["heading"]:
        name, st["heading"] = st["heading"], ""
    elif st["heading"] and _SEG_PART.match(name):
        return
    if not _is_segment_name(name, label):
        return
    vals, decimals = [], 0
    for cell in cells[-n:]:
        try:
            vals.append(_cell_value(cell) if _CELL.fullmatch(cell) else None)
        except ValueError:
            vals.append(None)
        if "." in cell:
            decimals = max(decimals, len(cell.strip("()").split(".")[-1]))
    st["rows"].setdefault((st["kind"], name), (label, vals, decimals))


def _reconcile_copies(rows: list[FinancialMeasurement], issues: list[str], doc_id: str) -> list[FinancialMeasurement]:
    """One filing often holds the same statement more than once (scanned copy + OCR copy, a
    restated repeat).  Where the copies disagree on a figure, the value most copies state is
    used; without a majority the figure is not used at all."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        groups[(r.metric, r.period_type, r.period_end, r.scope, r.segment)].append(r)
    out = []
    for key, rs in groups.items():
        clusters: list[list[FinancialMeasurement]] = []
        for r in rs:
            for cl in clusters:
                ref = cl[0]
                if abs(r.value - ref.value) <= max(0.005 * abs(ref.value), (r.display_unit or 0) + (ref.display_unit or 0)):
                    cl.append(r)
                    break
            else:
                clusters.append([r])
        if len(clusters) == 1:
            out.append(min(rs, key=lambda r: r.display_unit or 0.0))
            continue
        clusters.sort(key=len, reverse=True)
        if len(clusters[0]) > len(clusters[1]):
            out.append(min(clusters[0], key=lambda r: r.display_unit or 0.0))
        else:
            issues.append(f"{doc_id}: copies of the statement disagree on {key[0].value} {key[1]} {key[2]} "
                          f"({', '.join(f'{c[0].value:g}' for c in clusters)}); not used")
    return out


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
    r"|(?P<mdy>\b(?P<mmon>[A-Za-z]{3,9})\.?\s*(?P<md>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(?P<my>\d{4})\b)"
    # month-year only: "Jun-24", "Jun'24", "June 2024" (never "Sep 30": that is a day, not a year)
    r"|(?P<my_only>\b(?P<omon>[A-Za-z]{3,9})(?:\s*[-']\s*(?P<oy2>\d{2})|\s*[-']?\s*(?P<oy4>\d{4}))\b)",
    re.I,
)
_COL_GROUP_WORDS = re.compile(
    r"(?P<q>quarter ended|three months ended)|(?P<h>half[- ]year ended|six months ended)"
    r"|(?P<n>nine months ended)|(?P<y>year ended)|(?P<i>\bas\s+at\b|\bas\s+on\b)", re.I)
_TITLE_LINE = re.compile(r"statement of|results for|financial results|unaudited|audited|highlights", re.I)


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
        md = _month_day_over_year(lines[0], lines[1])
        if md:
            return md
    return []


_MD_PART = re.compile(r"\b(?P<mon>[A-Za-z]{3,9})\.?\s*(?P<day>[0-3Il][0-9Il]?)(?:st|nd|rd|th)?\s*,?"
                      r"(?:\s*(?P<year>(?:19|20)\d{2})\b)?", re.I)
_OCR_DAY = str.maketrans({"l": "1", "I": "1", "O": "0"})


def _month_day_over_year(top: str, bottom: str) -> list[tuple[date, Optional[str]]]:
    """Month-day cells over a separate year line, where some cells already carry their year:
        "December 31, September 30, December 31, December 31, December 31, March 31,2023"
        "2023 2023 2022 2023 2022"
    Years from the second line fill the cells that lack one, in order; counts must agree exactly."""
    parts = []
    for m in _MD_PART.finditer(top):
        mon = _MONTHS.get(m.group("mon")[:3].lower())
        if not mon:
            continue
        try:
            day = int(m.group("day").translate(_OCR_DAY))
        except ValueError:
            return []
        parts.append((mon, day, m.group("year")))
    years = [t.strip(",.") for t in bottom.split()]
    if len(parts) < 2 or not years or not all(re.fullmatch(r"(?:19|20)\d{2}", y) for y in years):
        return []
    if sum(1 for p in parts if not p[2]) != len(years):
        return []
    it = iter(years)
    out = []
    for mon, day, yr in parts:
        try:
            out.append((date(int(yr or next(it)), mon, day), None))
        except ValueError:
            return []
    return [t for t in out if t[0].month in (3, 6, 9, 12)] if len(out) >= 2 else []


def _mostly_period_tokens(line: str) -> bool:
    """A header line that is (nearly) only period cells, so a wrapped continuation may be appended."""
    rest = _COL_TOKEN.sub(" ", line)
    rest = re.sub(r"(?i)particulars|sl\.?\s*no\.?|sr\.?\s*no\.?|quarter|half|year|nine|six|three|months?|ended|"
                  r"period|on|as|at|[|(),.:\-–]", " ", rest)
    return sum(ch.isalpha() for ch in rest) <= 6


def _header_candidates(header_lines: list[str]):
    """(index, tokens) candidates: single lines, two wrapped lines joined, stacked dates."""
    toks = [_column_tokens(l) for l in header_lines]
    for i, line in enumerate(header_lines):
        if _TITLE_LINE.search(line) and len(toks[i]) < 3:
            continue          # a title naming one or two periods is not a column header
        yield i, toks[i]
        # period dates wrapped onto the next line: "30.09.2023 30.06.2023 30.09.2022" / "30.09.2023 ..."
        if (i + 1 < len(header_lines) and toks[i] and toks[i + 1] and not _TITLE_LINE.search(header_lines[i + 1])
                and _mostly_period_tokens(line)):
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

    period_type is "Q", "H", "9M", "FY", "I" (instant: balance-sheet "As at")
    or None (unidentifiable column: its values are not used).  The second element is an issue string or "".
    """
    best_i, best = -1, []
    for i, toks in _header_candidates(header_lines):
        if len(toks) >= 2 and len(toks) >= len(best):
            best_i, best = i, toks
    if not best:
        return [], ""
    n = len(best)
    # Consolidated and standalone statements side by side: the same date sequence twice.
    # (each half needs 3+ columns: a Q4 statement "Q, Q-1y, FY, FY-1y" also repeats its dates)
    if n >= 6 and n % 2 == 0 and [d for d, _ in best[: n // 2]] == [d for d, _ in best[n // 2:]]:
        half, issue = _type_columns(best[: n // 2], best_i, header_lines)
        return half + half, issue
    return _type_columns(best, best_i, header_lines)


_COL_WORD = re.compile(r"^(quarter|qtr|period|year|nine|half|six|three|months?|ended|\d+m)$", re.I)


def _per_column_words(header_lines: list[str], best_i: int, best: list) -> list[Optional[str]]:
    """A line of one group word per column: "Quarter Quarter Quarter Period Period Year"
    (with "Ended Ended ..." on the next line).  "Period" is year-to-date: its type follows
    from the month (Sep = half year, Dec = nine months, Mar = full year)."""
    n = len(best)
    for line in header_lines[max(0, best_i - 3):best_i]:
        words = [w for w in re.findall(r"[A-Za-z]+", line) if not re.fullmatch(r"ended|months?|particulars|sl|sr|no",
                                                                                w, re.I)]
        words = [w for w in words if not re.fullmatch(r"consolidated|standalone", w, re.I)]
        # scanned header words: "Quamir", "Quarler" -> quarter
        words = ["quarter" if re.match(r"qua\w{3,5}$", w, re.I) else w for w in words]
        if not words or not all(_COL_WORD.match(w) for w in words):
            continue
        if len(words) == 2 * n:
            words = words[:n]
        if len(words) != n:
            continue
        out = []
        for w, (d, _) in zip(words, best):
            w = w.lower()
            if w in ("quarter", "qtr", "three"):
                out.append("Q")
            elif w == "year":
                out.append("FY")
            elif w in ("half", "six"):
                out.append("H")
            elif w == "nine":
                out.append("9M")
            else:  # period / year-to-date
                out.append({3: "FY", 9: "H", 12: "9M"}.get(d.month))
        return out
    return []


def _type_columns(best: list, best_i: int, header_lines: list[str]) -> tuple[list[tuple[date, Optional[str]]], str]:
    n = len(best)

    # 1) explicit labels on every column (Q3 FY25, 9M FY25, FY24 ...)
    if all(t is not None for _, t in best):
        return best, ""

    # 2) header words giving exactly one group label per column
    above = " ".join(l for l in header_lines[max(0, best_i - 2):best_i] if not _TITLE_LINE.search(l))
    words = [("Q" if g.group("q") else "H" if g.group("h") else "9M" if g.group("n") else
              "I" if g.group("i") else "FY")
             for g in _COL_GROUP_WORDS.finditer(above + " " + header_lines[best_i])]
    # Balance sheets: "As at 30.09.2023 As at 31.03.2023" - instant columns, no durations.
    if words and all(w == "I" for w in words):
        return [(d, "I") for d, _ in best], ""
    words = [w for w in words if w != "I"]
    if len(words) == n:
        return [(d, t or w) for (d, t), w in zip(best, words)], ""
    per_col = _per_column_words(header_lines, best_i, best)
    if per_col and all(per_col):
        return [(d, t or w) for (d, t), w in zip(best, per_col)], ""

    # 3) positional rule from repeated dates.  The leading block is quarters unless
    #    the first header word says otherwise (SME half-yearly or annual-only statements).
    region = " ".join(l for l in header_lines[max(0, best_i - 3):best_i + 1] if not _TITLE_LINE.search(l))
    names_quarter = bool(re.search(r"quarter|three\s*months|\bqtr\b", region, re.I))
    base = words[0] if words and words[0] in ("H", "FY") and "Q" not in words and not names_quarter else "Q"
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


_TITLE_PERIOD = re.compile(
    r"(?P<what>quarter\s*and\s*(?:nine\s*months?|half\s*[- ]?year|six\s*months|(?:financial\s*)?year)"
    r"(?:\s*period)?|quarter|three\s*months)\s*(?:period\s*)?ended\s*(?:on\s*)?"
    r"(?P<date>\d{1,2}(?:st|nd|rd|th)?\s*[A-Za-z]{3,9},?\s*\d{4}|[A-Za-z]{3,9}\.?\s*\d{1,2}(?:st|nd|rd|th)?\s*,?\s*\d{4}"
    r"|\d{1,2}[./-]\d{1,2}[./-]\d{4})", re.I)


def _sebi_layout_from_title(text: str, header_lines: list[str]) -> list[tuple[date, Optional[str]]]:
    """Column layout implied by the statement title ("... for the quarter and nine months ended
    31 December 2023") under the SEBI Regulation 33 format:
      Q1:  Q(D) Q(D-3m) Q(D-1y) FY(previous March)
      Q2/Q3: Q(D) Q(D-3m) Q(D-1y) YTD(D) YTD(D-1y) FY(previous March)
      Q4:  Q(D) Q(D-3m) Q(D-1y) FY(D) FY(D-1y)
    The header must still name quarter columns, and the caller requires the layout to have
    exactly the width of the data rows."""
    head = " ".join(header_lines)
    if not re.search(r"quarter\s*ended|three\s*months\s*ended|quarter", head, re.I):
        return []
    m = _TITLE_PERIOD.search(text)
    if not m:
        return []
    toks = _column_tokens(m.group("date"))
    if not toks:
        return []
    d = toks[0][0]
    # the (partly unreadable) header itself must show this period's year and month
    flat = re.sub(r"\s+", "", head).lower()
    month_names = {k for k, v in _MONTHS.items() if v == d.month}
    if str(d.year) not in flat or not (any(mn in flat for mn in month_names)
                                       or f"{d.day:02d}.{d.month:02d}" in flat or f"{d.day}/{d.month}" in flat):
        return []
    y, mo = d.year, d.month
    prev_q = _month_end(y if mo > 3 else y - 1, mo - 3 if mo > 3 else 12)
    yago = _month_end(y - 1, mo)
    what = re.sub(r"\s+", "", m.group("what").lower())
    if mo == 3 and "year" in what:
        return [(d, "Q"), (prev_q, "Q"), (yago, "Q"), (d, "FY"), (yago, "FY")]
    if mo in (9, 12) and ("nine" in what or "half" in what or "six" in what):
        ytd = "H" if mo == 9 else "9M"
        return [(d, "Q"), (prev_q, "Q"), (yago, "Q"), (d, ytd), (yago, ytd), (_month_end(y, 3), "FY")]
    if mo == 6 and what in ("quarter", "threemonths"):
        return [(d, "Q"), (prev_q, "Q"), (yago, "Q"), (_month_end(y, 3), "FY")]
    return []


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
                r"\bletter of (?:award|intent)\b|\bL-?1\b(?: bidder| position)?|\bMoU\b|\bframework agreement\b|"
                r"\brate contract\b|"
                r"\b(?:orders?|contracts?|LoA|purchase orders?|work orders?)\b.{0,80}\b(?:cancell?ed|terminated|"
                r"short[- ]closed|amended|revised|reduced|expired|lapsed|executed|delivered|completed)\b|"
                r"\b(?:cancell?ation|termination|amendment|short[- ]closure|cancell?ed|terminated|short[- ]closed)\b.{0,60}"
                r"\b(?:orders?|contracts?|LoA)\b",
                re.I), Metric.ORDER_WIN),
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
    r"\b(?:from|by|with|(?:tenders?|orders?|contracts?) of)\s+(?:M/s\.?\s+)?((?:[A-Z][A-Za-z0-9&.\-]*|of|and|the)(?:\s+(?:[A-Z][A-Za-z0-9&.\-]*|of|and|the|\(India\))){0,7})")
_COUNTERPARTY_UNNAMED = re.compile(
    r"\b(?:a|an|one of the)\s+(?:leading|large|major|reputed|marquee|prestigious|global|domestic|international|"
    r"fortune \d+|european|us[- ]based|government)\b[^.,;]{0,40}?\b(?:customer|client|oem|player|company|utility|psu|entity)\b", re.I)
_FACILITY = re.compile(
    r"\b(?:at|in|of|for)\s+(?:the|our|its)\s+((?:[A-Z][\w-]*\s+){1,3})(?:plant|unit|facility|factory|works|site)\b")
_FACILITY_BARE = re.compile(r"\b((?:[A-Z][\w-]*\s+){1,2})(?:plant|unit|facility|factory)\b(?!\s+(?:of|for)\b)")
_SEGMENT = re.compile(r"\b(?:in|for|from) (?:the|our) ([a-z][a-z &/-]{2,40}?) (?:segment|division|business|vertical)\b", re.I)

# --- commercial-event detail (WP4) ------------------------------------------
_CANCEL = re.compile(r"\b(?:cancell?ed|cancell?ation|terminated|termination of the (?:order|contract)|short[- ]closed|"
                     r"foreclosed|withdrawn by the (?:customer|client))\b", re.I)
_EXPIRE = re.compile(r"\b(?:expired|lapsed)\b", re.I)
_AMEND = re.compile(r"\b(?:amended|amendment|revised (?:order |contract )?value|(?:reduced|increased|revised) to|"
                    r"scope (?:reduced|enhanced|revised))\b", re.I)
_EXECUTED = re.compile(r"\b(?:executed|delivered|commissioned|completed (?:the )?(?:supply|execution|delivery|order))\b",
                       re.I)
_INQUIRY = re.compile(r"\bin (?:advanced )?discussions?\b|\bpipeline\b|\benquir(?:y|ies)\b|\bbids? submitted\b|"
                      r"\bnegotiations?\b", re.I)
_FRAMEWORK = re.compile(r"\bMoU\b|\bmemorandum of understanding\b|\bframework agreement\b|\brate contract\b|"
                        r"\bmaster (?:supply|service) agreement\b", re.I)
_RELATED = re.compile(r"\b(?:its|our|the company'?s|the company’s)\s+(?:wholly[- ]owned\s+|step[- ]down\s+|material\s+)?"
                      r"subsidiar(?:y|ies)\b|\bgroup compan(?:y|ies)\b|\bpromoter group (?:entity|company)\b|"
                      r"\bjoint venture of the company\b|\b(?:is|are|being) (?:a )?related part(?:y|ies)\b", re.I)
_ASSERT_UNRELATED = re.compile(
    r"awarded by (?:any )?related part(?:y|ies)[^.]{0,120}?(?:[:\-–]\s*|\?\s*)(?:no\b|not applicable)|"
    r"\bnot (?:a )?related part(?:y|ies)\b|"
    r"promoter(?:s)?(?:\s*/\s*|\s+or\s+)promoter group (?:have|has|do not have|does not have) (?:no|any) interest|"
    r"\bdoes not (?:belong|fall) (?:to|within) (?:the )?promoter", re.I)
_CEILING = re.compile(r"\b(?:up ?to|maximum|ceiling|not exceeding|estimated (?:total )?(?:value|contract value))\b", re.I)
_MINIMUM = re.compile(r"\b(?:minimum (?:guaranteed|offtake|commitment|order)|guaranteed minimum)\b", re.I)
_RELEASE = re.compile(r"\b(?:release|call[- ]off|drawdown) orders?\b", re.I)
_TAX_INCL = re.compile(r"\b(?:inclusive of|including|incl\.?)\s+(?:all\s+)?(?:gst|taxes|tax|duties)\b", re.I)
_TAX_EXCL = re.compile(r"\b(?:exclusive of|excluding|excl\.?|plus)\s+(?:applicable\s+)?(?:gst|taxes|tax)\b|\+\s*gst\b", re.I)
_DURATION = re.compile(r"\b(?:over|within|in|for|spread over)\s+(?:a\s+)?(?:period\s+of\s+)?(\d{1,3})\s*(months?|years?)\b", re.I)
_DURATION_COLON = re.compile(r"\b(?:execut|deliver|complet|suppl)\w*\b[^.]{0,40}?[:\-–]\s*(\d{1,3})\s*(months?|years?)\b", re.I)
_DELIVERY = re.compile(r"\b(?:to be (?:executed|delivered|completed)|delivery|execution)\b[^.;]{0,30}\b(?:by|before|during|in)\s+"
                       r"((?:Q[1-4]\s*)?FY\s*'?\d{2,4}|[A-Z][a-z]+ \d{4})", re.I)
_PAYMENT = re.compile(r"\b(?:payment terms?|advance of|milestone payments?|letter of credit|payment (?:within|after))\b[^.;]{0,100}",
                      re.I)
_TERMINATION = re.compile(r"\b(?:termination|terminate|cancellation clause|liquidated damages)\b[^.;]{0,100}", re.I)
_REFERENCE = re.compile(r"\b(?:PO|purchase order|LoA|LOA|letter of award|work order|contract|tender|NIT)\s*"
                        r"(?:no\.?|number|ref\.?|reference)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-_.]{3,})", re.I)
_PRODUCT = re.compile(r"\bfor (?:the )?(?:supply|manufacture|design|construction|installation|execution|delivery|"
                      r"commissioning|provision) of ([^.,;]{3,70})", re.I)


def relationship_from_text(text: str) -> tuple[RelationshipStatus, str]:
    """Issuer-side relationship disclosure: confirmed related, issuer-asserted unrelated, or unknown.
    Issuer statements never make a counterparty *independently* verified."""
    m = _RELATED.search(text)
    if m:
        return RelationshipStatus.CONFIRMED_RELATED, text[max(0, m.start() - 60):m.end() + 60].strip()
    m = _ASSERT_UNRELATED.search(text)
    if m:
        return RelationshipStatus.ISSUER_ASSERTED_UNRELATED, m.group(0)[:200]
    return RelationshipStatus.UNKNOWN, ""


def order_stage(sentence: str) -> EventStage:
    """Stage described by one sentence.  Lifecycle changes first; then the weakest
    commitment wording present wins (unclear wording is never upgraded to binding)."""
    if _CANCEL.search(sentence):
        return EventStage.CANCELLED
    if _EXPIRE.search(sentence):
        return EventStage.EXPIRED
    if _AMEND.search(sentence):
        return EventStage.AMENDED
    if _RELEASE.search(sentence):
        return EventStage.BINDING_ORDER          # executable release under a framework
    if _INQUIRY.search(sentence):
        return EventStage.INQUIRY
    if _FRAMEWORK.search(sentence):
        return EventStage.MOU_FRAMEWORK
    if _PROVISIONAL.search(sentence):
        return EventStage.PREFERRED_BIDDER
    if _BINDING.search(sentence):
        return EventStage.BINDING_ORDER          # "... to be executed over 18 months" stays binding
    m = _EXECUTED.search(sentence)
    if m and not re.search(r"\b(?:to be|will be|shall be|would be|being|yet to be)\s+$", sentence[:m.start()]):
        return EventStage.EXECUTION
    return EventStage.PREFERRED_BIDDER


def _all_inr(text: str) -> list[Quantity]:
    out, pos = [], 0
    while True:
        q = parse_inr(text[pos:])
        if q is None:
            return out
        out.append(q)
        pos += text[pos:].find(q.raw) + len(q.raw)


def order_details(sentence: str, qty: Optional[Quantity], stage: EventStage) -> dict:
    d: dict = {}
    if stage == EventStage.AMENDED:
        amts = _all_inr(sentence)
        qty = amts[-1] if amts else qty          # "reduced from Rs 450 crore to Rs 300 crore" -> 300
    if qty is None:
        d["value_basis"] = ValueBasis.UNQUANTIFIED
    elif _MINIMUM.search(sentence):
        d["value_basis"] = ValueBasis.GUARANTEED_MINIMUM
    elif _RELEASE.search(sentence):
        d["value_basis"] = ValueBasis.EXECUTABLE_RELEASE
    elif _CEILING.search(sentence) or stage == EventStage.MOU_FRAMEWORK:
        d["value_basis"] = ValueBasis.CEILING
    else:
        d["value_basis"] = ValueBasis.FIRM
    d["tax_basis"] = (TaxBasis.INCLUSIVE if _TAX_INCL.search(sentence) else
                      TaxBasis.EXCLUSIVE if _TAX_EXCL.search(sentence) else TaxBasis.UNKNOWN)
    m = _DURATION.search(sentence) or _DURATION_COLON.search(sentence)
    if m:
        n = int(m.group(1))
        d["duration_months"] = n * 12 if m.group(2).lower().startswith("year") else n
    m = _DELIVERY.search(sentence)
    d["delivery_window"] = m.group(1) if m else ""
    m = _PAYMENT.search(sentence)
    d["payment_terms"] = m.group(0)[:120] if m else ""
    m = _TERMINATION.search(sentence)
    d["termination_terms"] = m.group(0)[:120] if m else ""
    m = _REFERENCE.search(sentence)
    d["reference_id"] = m.group(1).rstrip(".").upper() if m else ""
    m = _PRODUCT.search(sentence)
    d["product"] = m.group(1).strip().lower() if m else ""
    return d, qty


_GUIDANCE_METRIC = {
    Metric.REVENUE: Metric.REVENUE_GUIDANCE,
    Metric.EBITDA_MARGIN: Metric.MARGIN_GUIDANCE,
}
_GROWTH_WORDS = re.compile(r"\b(?:growth|grow|cagr|increase)\b", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[-–]|to)?\s*(?:\d+(?:\.\d+)?)?\s*%|\d+(?:\.\d+)?\s*per\s*cent", re.I)


def _growth_percent(s: str) -> Optional[Quantity]:
    """A growth percentage stated next to its growth word ("growth of 25%", "25% growth",
    "grow by 20-25%"); a percentage elsewhere in the sentence is not taken."""
    m = _GROWTH_PCT_AFTER.search(s) or _GROWTH_PCT_BEFORE.search(s)
    return parse_percent(m.group(0)) if m else None


_PCT_RANGE = r"\d+(?:\.\d+)?\s*%?\s*(?:[-–]|to)?\s*(?:\d+(?:\.\d+)?)?\s*(?:%|per\s*cent)"
# "growth of 25%", "grow by 20-25%", "increase of around 30 per cent", "CAGR of 18%"
_GROWTH_PCT_AFTER = re.compile(r"\b(?:growth|grow|cagr|increase)\w*\b(?:\s+(?:of|by|at|in|to|around|about|approx\w*|"
                               r"close\s+to|nearly|over|more\s+than|upto|up\s+to|~|the|range|revenue|top\s*line|"
                               r"sales|yoy|y-o-y)){0,4}\s*(?:" + _PCT_RANGE + ")", re.I)
# "25% growth", "20-25% YoY growth", "18% revenue CAGR"
_GROWTH_PCT_BEFORE = re.compile("(?:" + _PCT_RANGE + r")\s*(?:(?:yoy|y-o-y|revenue|top\s*line|sales|volume)\s*){0,2}"
                                r"(?:growth|cagr|increase)\b", re.I)


def _NUMBER_SOUP(s: str) -> bool:
    """Text from slides or tables run together into one 'sentence'."""
    nums = re.findall(r"\d[\d,.]*%?", s)
    return len(s) > 300 and len(nums) >= 8
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
    doc_relationship = relationship_from_text(doc.full_text()[:200000])
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
                    qty = _growth_percent(s_clean)
            labels = find_period_labels(s_clean)

            tier, strength, out_metric = EvidenceTier.REALIZED_EXECUTION, CommitmentStrength.NOT_APPLICABLE, metric
            order_kw: dict = {}
            if metric == Metric.ORDER_WIN:
                stage = order_stage(s_clean)
                if modality == Modality.NEGATED and stage in (EventStage.CANCELLED, EventStage.EXPIRED,
                                                               EventStage.AMENDED):
                    continue                            # "has not been cancelled": no state change
                tier = EvidenceTier.COMMERCIAL_COMMITMENT
                strength = {EventStage.BINDING_ORDER: CommitmentStrength.BINDING,
                            EventStage.EXECUTION: CommitmentStrength.BINDING,
                            EventStage.PREFERRED_BIDDER: CommitmentStrength.PROVISIONAL,
                            EventStage.MOU_FRAMEWORK: CommitmentStrength.NON_BINDING,
                            EventStage.INQUIRY: CommitmentStrength.NON_BINDING}.get(stage,
                                                                                   CommitmentStrength.NOT_APPLICABLE)
                if stage in (EventStage.INQUIRY, EventStage.MOU_FRAMEWORK):
                    tier = EvidenceTier.MANAGEMENT_ASSERTION
                if modality in (Modality.FORWARD, Modality.CONDITIONAL) and stage in COMMITMENT_RANK:
                    tier = EvidenceTier.MANAGEMENT_ASSERTION
                if (stage == EventStage.PREFERRED_BIDDER and qty is None and not _PROVISIONAL.search(s_clean)):
                    continue        # e.g. "The order is to be executed over 18 months." - detail, not an order
                details, qty = order_details(s_clean, qty, stage)
                rel, rel_basis = relationship_from_text(s_clean)
                if rel == RelationshipStatus.UNKNOWN:
                    rel, rel_basis = doc_relationship
                order_kw = dict(event_stage=stage, relationship=rel, relationship_basis=rel_basis, **details)
            elif modality in (Modality.FORWARD, Modality.CONDITIONAL):
                if _NUMBER_SOUP(s_clean):
                    continue      # slide / table text run together: no reliable target in it
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
            fac = _FACILITY.search(s_clean) or _FACILITY_BARE.search(s_clean)
            facility = re.sub(r"\s+", " ", fac.group(1)).strip().lower() if fac else ""
            if not facility and seg:
                facility = f"segment:{seg.group(1).strip().lower()}"

            target_label = ""
            if tier == EvidenceTier.MANAGEMENT_ASSERTION and labels:
                target_label = labels[-1]
            ev = Evidence(
                evidence_id=_evidence_id(doc.doc_id, s_clean, out_metric),
                doc_id=doc.doc_id, ticker=doc.ticker, metric=out_metric, tier=tier, modality=modality,
                quote=s_clean, available_at=doc.available_at, page=c.page, chunk_id=c.chunk_id,
                quantity=qty, period_label=labels[0] if labels and not target_label else "",
                target_period_label=target_label, scope=_scope_from(s_clean),
                segment=seg.group(1).strip() if seg else "", facility=facility,
                counterparty=cp, counterparty_named=cp_named, commitment_strength=strength,
                distinct_marker=bool(_DISTINCT.search(s_clean)), **order_kw,
            )
            if ev.evidence_id in seen:
                continue
            seen.add(ev.evidence_id)
            out.append(ev)
    _enrich_single_order_disclosure(doc, out)
    out += _mechanism_evidence(doc, chunks, seen)
    return out


# --- mechanism statements (WP5) -------------------------------------------
# A sentence may support several mechanisms ("EBITDA margin improved on lower raw
# material costs and better realisations"), so this pass is separate from the one
# metric per sentence above and never changes it.
_MECH_CUES: list[tuple[re.Pattern, Metric]] = [
    (re.compile(r"\b(?:sales\s+|production\s+|dispatch\s+)?volumes?\b(?!\s+of\s+business)|\bunits?\s+(?:sold|shipped|"
                r"dispatched)\b|\b(?:shipments|dispatches|tonnage)\b", re.I), Metric.VOLUME),
    (re.compile(r"\b(?:realisations?|realizations?|average\s+selling\s+prices?|ASPs?|price\s+(?:hikes?|increases?|"
                r"cuts?|reductions?|revisions?)|selling\s+prices?|pricing)\b", re.I), Metric.PRICING),
    (re.compile(r"\b(?:raw[- ]material|input|commodity|gas|coal|power\s+and\s+fuel|freight)\s+(?:costs?|prices?)\b",
                re.I), Metric.INPUT_COST),
    (re.compile(r"\b(?:contribut\w+|share)\b[^.]{0,40}?\d+(?:\.\d+)?\s*%[^.]{0,30}?\b(?:revenue|sales|turnover|mix)\b|"
                r"\b(?:revenue|sales|product)\s+mix\b", re.I), Metric.MIX_SHARE),
    (re.compile(r"\bmarket\s+share\b", re.I), Metric.MARKET_SHARE),
    (re.compile(r"\b(?:acquisition\s+of|acquired|inorganic|amalgamation|takeover\s+of)\b", re.I), Metric.ACQUISITION),
]
_UP_WORDS = re.compile(r"\b(?:increas\w*|rose|rise[sn]?|rising|grew|grow\w*|higher|better|improv\w*|up|hike[sd]?|firm\w*|"
                       r"strong\w*|expan\w*|gain\w*|surg\w*|jump\w*)\b", re.I)
_DOWN_WORDS = re.compile(r"\b(?:decreas\w*|declin\w*|fell|fall\w*|lower|reduc\w*|soften\w*|cuts?|down|weak\w*|"
                         r"contract\w*|dropp?\w*|eas(?:ed|ing)|subdued|pressure)\b", re.I)


def _local_direction(s: str, m: re.Match) -> int:
    """Direction from the words next to the cue ("lower raw material costs", "volumes grew 18%")."""
    before = " ".join(s[:m.start()].split()[-2:])
    after = " ".join(s[m.end():].split()[:4])
    return _stated_direction(before) or _stated_direction(after)


def _stated_direction(text: str) -> int:
    up, down = bool(_UP_WORDS.search(text)), bool(_DOWN_WORDS.search(text))
    return 1 if up and not down else -1 if down and not up else 0


def _mechanism_evidence(doc: SourceDocument, chunks: list[Chunk], seen: set) -> list[Evidence]:
    out: list[Evidence] = []
    for c in chunks:
        if c.kind == "table":
            continue
        for s in sentences(c):
            s_clean = re.sub(r"\s+", " ", s).strip()
            if len(s_clean) < 20 or is_garbled(s_clean) or _NUMBER_SOUP(s_clean):
                continue
            for pat, metric in _MECH_CUES:
                m = pat.search(s_clean)
                if not m:
                    continue
                window = s_clean[max(0, m.start() - 60):m.end() + 80]
                pct = re.search(r"[-+]?\d+(?:\.\d+)?\s*%", window)
                qty = parse_percent(pct.group(0)) if pct else None
                if metric in (Metric.VOLUME, Metric.MIX_SHARE) and qty is None:
                    continue           # unquantified volume / mix talk is too vague
                product = ""
                if metric == Metric.MIX_SHARE:
                    pm = re.search(r"([A-Za-z][\w&/ -]{2,50}?)\s+(?:contribut\w+|share)", s_clean)
                    product = re.sub(r"^(?:the|our|its)\s+", "", pm.group(1).strip(), flags=re.I).lower() if pm else ""
                modality = classify_modality(s_clean)
                ev = Evidence(
                    evidence_id=_evidence_id(doc.doc_id, s_clean, metric), doc_id=doc.doc_id, ticker=doc.ticker,
                    metric=metric, tier=(EvidenceTier.MANAGEMENT_ASSERTION
                                         if modality in (Modality.FORWARD, Modality.CONDITIONAL)
                                         else EvidenceTier.REALIZED_EXECUTION),
                    modality=modality, quote=s_clean, available_at=doc.available_at, page=c.page,
                    chunk_id=c.chunk_id, quantity=qty, period_label=(find_period_labels(s_clean) or [""])[0],
                    scope=_scope_from(s_clean), segment=product,
                    facility=(re.sub(r"\s+", " ", f.group(1)).strip().lower()
                              if (f := _FACILITY.search(s_clean) or _FACILITY_BARE.search(s_clean)) else ""),
                    direction=_local_direction(s_clean, m))
                if ev.evidence_id in seen:
                    continue
                seen.add(ev.evidence_id)
                out.append(ev)
    return out


def _enrich_single_order_disclosure(doc: SourceDocument, evs: list[Evidence]) -> None:
    """An order announcement usually states the order once and its terms in other
    sentences (SEBI format: 'Time period by which the order is to be executed: 18
    months').  When a document announces exactly one quantified order, its missing
    terms are filled from the rest of that document."""
    orders = [e for e in evs if e.metric == Metric.ORDER_WIN and e.quantity is not None
              and e.event_stage in (EventStage.BINDING_ORDER, EventStage.PREFERRED_BIDDER, EventStage.MOU_FRAMEWORK)]
    if len(orders) != 1:
        return
    e = orders[0]
    text = re.sub(r"\s+", " ", doc.full_text())
    if e.duration_months is None:
        m = (re.search(r"\b(?:execut|deliver|complet|suppl)\w*\b[^.]{0,60}?" + _DURATION.pattern[2:], text, re.I)
             or _DURATION_COLON.search(text))
        if m:
            n = int(m.group(1))
            e.duration_months = n * 12 if m.group(2).lower().startswith("year") else n
    for attr, pat in (("payment_terms", _PAYMENT), ("termination_terms", _TERMINATION)):
        if not getattr(e, attr):
            m = pat.search(text)
            if m:
                setattr(e, attr, m.group(0)[:120])
    if not e.reference_id:
        m = _REFERENCE.search(text)
        if m:
            e.reference_id = m.group(1).rstrip(".").upper()
    if e.tax_basis == TaxBasis.UNKNOWN:
        e.tax_basis = (TaxBasis.INCLUSIVE if _TAX_INCL.search(text) else
                       TaxBasis.EXCLUSIVE if _TAX_EXCL.search(text) else TaxBasis.UNKNOWN)


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
