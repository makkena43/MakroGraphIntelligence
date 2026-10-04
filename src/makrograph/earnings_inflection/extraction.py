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
    m = re.fullmatch(r"(Q[1-4]|H[12])?FY(\d{2})", label or "")
    if not m:
        return None
    fy = 2000 + int(m.group(2))
    p = m.group(1)
    if not p:
        return date(fy, 3, 31)
    return {"Q1": date(fy - 1, 6, 30), "Q2": date(fy - 1, 9, 30), "Q3": date(fy - 1, 12, 31),
            "Q4": date(fy, 3, 31), "H1": date(fy - 1, 9, 30), "H2": date(fy, 3, 31)}[p]


def fy_label_for(period_end: date, period_type: str) -> str:
    fy = period_end.year + 1 if period_end.month > 3 else period_end.year
    if period_type == "FY":
        return f"FY{fy % 100:02d}"
    q = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(period_end.month, "Q?")
    return f"{q}FY{fy % 100:02d}"


# ---------------------------------------------------------------------------
# Results-statement tables
# ---------------------------------------------------------------------------

_DATE_TOKEN = re.compile(
    r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})|"
    r"(\d{1,2})(?:st|nd|rd|th)?\s*[- ]?([A-Za-z]{3,9})[,\s-]*(\d{4})|"
    r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})",
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_ROW_METRICS = [
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*revenue from operations", re.I), Metric.REVENUE),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*(?:net )?sales(?:/income from operations)?\b", re.I), Metric.REVENUE),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*other income", re.I), Metric.OTHER_INCOME),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*total expenses", re.I), Metric.TOTAL_EXPENSES),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*ebitda\b(?!\s*margin)", re.I), Metric.EBITDA),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*depreciation", re.I), Metric.DEPRECIATION),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*finance costs?", re.I), Metric.FINANCE_COST),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*exceptional items?", re.I), Metric.EXCEPTIONAL_ITEMS),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*profit(?:/\(loss\))? before tax", re.I), Metric.PBT),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*(?:total )?tax expense", re.I), Metric.TAX),
    (re.compile(r"^\s*(?:\d+\.?|[a-z]\)|\([a-z0-9]+\))?\s*(?:net )?profit(?:/\(loss\))? (?:after tax|for the (?:period|year|quarter))", re.I), Metric.PAT),
    (re.compile(r"^\s*-?\s*(?:owners|equity (?:share)?holders) of the (?:company|parent)", re.I), Metric.PAT_ATTRIBUTABLE),
    (re.compile(r"diluted", re.I), Metric.DILUTED_EPS),
    (re.compile(r"^\s*(?:\(?[a-z]\)?\s*)?basic", re.I), Metric.BASIC_EPS),
]

_CELL = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")


def _parse_date(tok_match) -> Optional[date]:
    g = tok_match.groups()
    try:
        if g[0]:
            y = int(g[2]); y = y + 2000 if y < 100 else y
            return date(y, int(g[1]), int(g[0]))
        if g[3]:
            return date(int(g[5]), _MONTHS[g[4][:3].lower()], int(g[3]))
        if g[6]:
            return date(int(g[8]), _MONTHS[g[6][:3].lower()], int(g[7]))
    except (KeyError, ValueError):
        return None
    return None


def _cell_value(s: str) -> float:
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    v = float(s.strip("()-").replace(",", ""))
    return -v if neg else v


def _table_scale(header: str, page_text: str) -> Optional[float]:
    for src in (header, page_text):
        m = re.search(r"(?:rs\.?|inr|₹|amount)[^\n]{0,25}?\b(crores?|lakhs?|lacs|million|mn|thousands?)\b", src, re.I)
        if m:
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


def _scope_from(text: str) -> Scope:
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
        header_blob = c.header + "\n" + "\n".join(lines[:3])
        # column periods: dates found in header area, in order
        cols: list[date] = []
        col_line_idx = -1
        for idx, l in enumerate((c.header + "\n" + c.text).split("\n")):
            ds = [d for d in (_parse_date(m) for m in _DATE_TOKEN.finditer(l)) if d]
            if len(ds) >= 2:
                cols = ds
                col_line_idx = idx
                break
        if not cols:
            continue
        # period types per column from header words, default by month span heuristic
        ptypes = _column_period_types(header_blob, len(cols))
        scale = _table_scale(c.header, page_text)
        scope = _scope_from(c.header + "\n" + page_text[:600])
        if scale is None:
            issues.append(f"{doc.doc_id}:p{c.page}: results table without unit line; amounts not used")
        for l in lines:
            label, cells = split_numeric_row(l)
            metric = next((m for pat, m in _ROW_METRICS if pat.search(label)), None)
            if metric is None:
                continue
            # A row with fewer cells than period columns cannot be aligned safely
            # (pdf text drops blank cells); skip it rather than shift values.
            if len(cells) < len(cols):
                continue
            cells = cells[-len(cols):]
            for col_i, (d, cell) in enumerate(zip(cols, cells)):
                if not _CELL.fullmatch(cell):
                    continue            # "-" / "nil" = not reported
                try:
                    v = _cell_value(cell)
                except ValueError:
                    continue
                if metric in (Metric.DILUTED_EPS, Metric.BASIC_EPS):
                    unit, val = Unit.INR_PER_SHARE, v
                else:
                    if scale is None:
                        continue
                    unit, val = Unit.INR_CRORE, round(v * scale, 4)
                out.append(FinancialMeasurement(
                    ticker=doc.ticker, metric=metric, period_end=d, period_type=ptypes[col_i],
                    value=val, unit=unit, scope=scope, doc_id=doc.doc_id,
                    available_at=doc.available_at, quote=f"{c.header.splitlines()[-1] if c.header else ''} | {l.strip()}"[:400],
                ))
    return out, issues


def _column_period_types(header: str, n: int) -> list[str]:
    h = header.lower()
    groups = []
    for m in re.finditer(r"(quarter ended|three months ended|year ended|half[- ]year ended|six months ended|nine months ended)", h):
        w = m.group(1)
        groups.append("Q" if ("quarter" in w or "three" in w) else "FY" if "year ended" == w else
                      "H" if ("half" in w or "six" in w) else "9M")
    if not groups:
        return ["Q"] * n
    if len(groups) == n:
        return groups
    # Common layout: "Quarter ended" spans 3 cols, "Year ended" spans the rest.
    if groups[0] == "Q" and "FY" in groups:
        fy_cols = 1 if n <= 4 else 2
        if "9M" in groups:
            q_cols = n - fy_cols - 2
            return ["Q"] * q_cols + ["9M", "9M"] + ["FY"] * fy_cols
        return ["Q"] * (n - fy_cols) + ["FY"] * fy_cols
    return [groups[0]] * n


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


def extract_sentence_evidence(doc: SourceDocument, chunks: list[Chunk]) -> list[Evidence]:
    """Deterministic evidence from prose (tables are handled by the results parser)."""
    out: list[Evidence] = []
    seen = set()
    for c in chunks:
        if c.kind == "table":
            continue
        for s in sentences(c):
            s_clean = re.sub(r"\s+", " ", s).strip()
            if len(s_clean) < 12:
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
