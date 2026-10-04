"""Ingestion-level extractors for management growth-ambition and concall tone.

Two signals the constraint pipeline was missing, both strong conviction inputs
for "greatest constraint -> which stock" once the constraint itself is real:

1. GROWTH AMBITION — management guiding a *multiple* (>=2x) of its own revenue,
   capacity, production, EPS, or order book. Either stated explicitly ("5x by
   FY28", "double our capacity", "grow three-fold") or derivable from a stated
   from->to target ("revenue to reach Rs.2,000 cr from Rs.400 cr"). A maker of a
   constrained product whose management is underwriting 5-10x is exactly the
   "about to inflect" name; a market-size "poised to double" is a weaker theme
   tailwind, kept separate via ``perspective``.

2. CONCALL TONE — a bounded lexical sentiment for conference-call / analyst-meet
   documents, so a constrained-product maker on an upbeat, order-visibility call
   ranks ahead of one hedging on demand.

These are precision-first: the corpus is full of "double digit" (a percentage,
not a multiple), "double click", "double-sided auction" — all explicitly
excluded. Nothing here names a company, product, or theme; it reads only form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── Growth ambition ─────────────────────────────────────────────────────────

# The company's own growth basis must appear near the multiple, else it is a
# market/industry statistic or unrelated number.
_BASIS_RE = re.compile(
    r"\b(revenue|turnover|top\s*line|topline|sales|capacit(?:y|ies)|production|"
    r"output|volumes?|eps|earnings per share|pat|profit(?:ability)?|bottom\s*line|"
    r"order\s*book|order\s*inflow|throughput|dispatch(?:es)?)\b", re.I)

# Subject: does the sentence own the growth (company) or describe the market?
_COMPANY_SUBJ_RE = re.compile(
    r"\b(we|our|us|the\s+company|company'?s|the\s+group|group'?s|management|"
    r"the\s+board|the\s+business)\b", re.I)
_MARKET_SUBJ_RE = re.compile(
    r"\b(market|industry|sector|segment\s+is|demand\s+is|tam\b|addressable|"
    r"per\s+capita|economy|nationwide|country'?s)\b", re.I)

# Multiplier forms. "double/triple/..." map to a numeric multiple; "N x", "N
# times", "N-fold" carry it directly.
_WORD_MULT = {
    "double": 2.0, "doubling": 2.0, "triple": 3.0, "tripling": 3.0,
    "quadruple": 4.0, "quadrupling": 4.0, "fivefold": 5.0, "tenfold": 10.0,
    "two-fold": 2.0, "three-fold": 3.0, "four-fold": 4.0, "five-fold": 5.0,
}
_MULT_X_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*[xX]\b")
_MULT_TIMES_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*times\b", re.I)
_MULT_FOLD_RE = re.compile(r"\b(\d+(?:\.\d+)?)[\s-]?fold\b", re.I)
_MULT_WORD_RE = re.compile(
    r"\b(doubl(?:e|ing)|tripl(?:e|ing)|quadrupl(?:e|ing)|fivefold|tenfold|"
    r"two-fold|three-fold|four-fold|five-fold)\b", re.I)

# Kills the dominant false positives seen in the corpus.
_EXCLUDE_RE = re.compile(
    r"double\s*(digit|click|count|book|entry)|double[- ]sided|double[- ]blind|"
    r"single\s+digit", re.I)

# Forward-looking guidance verbs. A growth MULTIPLE is only an *ambition* when
# it is stated as a plan/target, not a backward "we grew 2x last year" or an
# incidental number. Required for the ambiguous word-forms (double/triple) and
# bare multiples; from->to targets are specific enough to stand alone.
_GUIDANCE_RE = re.compile(
    r"\b(target\w*|aim\w*|plan\w*|expect\w*|guid\w+|will\b|intend\w*|envisage\w*|"
    r"project\w*|aspir\w*|roadmap|on\s+track|set\s+to|poised\s+to|looking\s+to|"
    r"goal|by\s+(?:fy)?\s*20\d{2}|over\s+the\s+next|coming\s+years|"
    r"aspiration|ambition|coming\s+\d+\s+years)\b", re.I)

# from -> to derivation, e.g. "from Rs. 400 crore to Rs. 2,000 crore".
_FROMTO_RE = re.compile(
    r"from\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(cr(?:ore)?|bn|billion|mn|million|lakh)?"
    r"[\s\S]{0,40}?to\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(cr(?:ore)?|bn|billion|mn|million|lakh)?",
    re.I)
_UNIT_SCALE = {"cr": 1e7, "crore": 1e7, "bn": 1e9, "billion": 1e9,
               "mn": 1e6, "million": 1e6, "lakh": 1e5, None: 1.0, "": 1.0}


_PAST_ACHIEVE_RE = re.compile(
    r"\b(achieved|delivered|grew|registered|reported|posted|clocked|recorded|"
    r"have\s+grown|has\s+grown|witnessed|last\s+year|in\s+the\s+past|"
    r"over\s+the\s+(?:last|past))\b", re.I)


@dataclass
class GrowthSignal:
    multiple: float
    basis: str            # revenue / capacity / production / eps / order_book ...
    perspective: str      # "company" | "market"
    horizon_year: int | None
    context: str
    confidence: float


def _basis_label(window: str) -> str | None:
    m = _BASIS_RE.search(window)
    if not m:
        return None
    b = m.group(1).lower().replace(" ", "")
    if b.startswith("revenue") or b in ("turnover", "topline", "sales"):
        return "revenue"
    if b.startswith("capacit"):
        return "capacity"
    if b in ("production", "output", "throughput", "dispatch", "dispatches", "volume", "volumes"):
        return "production"
    if b in ("eps", "earningspershare"):
        return "eps"
    if "profit" in b or b == "pat" or b == "bottomline":
        return "profit"
    if b.startswith("order"):
        return "order_book"
    return "revenue"


def _horizon(window: str) -> int | None:
    m = re.search(r"\b(?:by|in|fy|until|till)\s*'?(?:fy)?\s*(20\d{2}|2\d)\b", window, re.I)
    if not m:
        return None
    y = int(m.group(1))
    return y if y > 100 else 2000 + y


def extract_growth_signals(text: str, *, max_signals: int = 12) -> list[GrowthSignal]:
    """Find management growth-multiple statements in one document's text."""
    flat = re.sub(r"\s+", " ", text or "")
    if not flat:
        return []
    out: list[GrowthSignal] = []
    seen: set[tuple] = set()

    def consider(mult: float, start: int, end: int, *, specific: bool = False) -> None:
        if not (2.0 <= mult <= 100.0):
            return
        window = flat[max(0, start - 90):min(len(flat), end + 90)]
        if _EXCLUDE_RE.search(window):
            return
        basis = _basis_label(window)
        if not basis:
            return
        # Backward-looking achievement ("achieved 65x growth", "grew 3x last
        # year") is history, not ambition — drop it even if a guidance verb also
        # appears elsewhere in the window.
        if _PAST_ACHIEVE_RE.search(window):
            return
        # A multiple is an *ambition* only when framed as forward guidance.
        # from->to targets are specific enough to skip this; ambiguous word and
        # bare-number forms require a plan/target/expect verb nearby.
        if not specific and not _GUIDANCE_RE.search(window):
            return
        is_market = bool(_MARKET_SUBJ_RE.search(window))
        is_company = bool(_COMPANY_SUBJ_RE.search(window))
        # A bare multiple with a basis but no clear subject is treated as company
        # guidance only when it is not obviously a market statistic.
        perspective = "company" if (is_company and not is_market) else \
                      "market" if is_market else "company"
        key = (round(mult, 1), basis, window[:40])
        if key in seen:
            return
        seen.add(key)
        conf = 0.5
        if is_company and not is_market:
            conf += 0.25
        if basis in ("revenue", "capacity", "production", "order_book"):
            conf += 0.1
        if _FROMTO_RE.search(window):
            conf += 0.15
        out.append(GrowthSignal(
            multiple=round(mult, 2), basis=basis, perspective=perspective,
            horizon_year=_horizon(window),
            context=window.strip()[:300], confidence=round(min(conf, 0.95), 2),
        ))

    for m in _MULT_X_RE.finditer(flat):
        consider(float(m.group(1)), m.start(), m.end())
    for m in _MULT_TIMES_RE.finditer(flat):
        consider(float(m.group(1)), m.start(), m.end())
    for m in _MULT_FOLD_RE.finditer(flat):
        consider(float(m.group(1)), m.start(), m.end())
    for m in _MULT_WORD_RE.finditer(flat):
        consider(_WORD_MULT.get(m.group(1).lower().rstrip("ing") + ("e" if m.group(1).lower().endswith("ing") else ""),
                                _WORD_MULT.get(m.group(1).lower(), 0.0)),
                 m.start(), m.end())
    # Derived multiples from explicit from->to targets tied to a growth basis.
    for m in _FROMTO_RE.finditer(flat):
        try:
            a = float(m.group(1).replace(",", "")) * _UNIT_SCALE.get((m.group(2) or "").lower(), 1.0)
            b = float(m.group(3).replace(",", "")) * _UNIT_SCALE.get((m.group(4) or "").lower(), 1.0)
        except (ValueError, AttributeError):
            continue
        if a > 0 and b / a >= 2.0:
            consider(b / a, m.start(), m.end(), specific=True)

    out.sort(key=lambda g: (g.perspective != "company", -g.confidence, -g.multiple))
    return out[:max_signals]


# ── Concall / management tone ────────────────────────────────────────────────

_POS = re.compile(
    r"\b(record|robust|strong(?:er|est)?|healthy|momentum|accelerat\w+|"
    r"outperform\w*|upbeat|confiden\w+|expansion|expand\w*|ramp[- ]?up|"
    r"tailwind|well[- ]positioned|order\s*book|visibility|traction|"
    r"double|multi[- ]?year|inflection|scal(?:e|ing)\s+up|beat\b)\b", re.I)
_NEG = re.compile(
    r"\b(weak(?:er|ness)?|soft(?:ness)?|subdued|sluggish|declin\w+|headwind|"
    r"pressure|cautious|challeng\w+|slowdown|deferr\w+|muted|shortfall|"
    r"de[- ]?growth|miss(?:ed)?\b|impairment|write[- ]?off|degrowth)\b", re.I)


def concall_tone(text: str) -> tuple[float, int, int]:
    """Return (net_tone in [-1,1], pos_hits, neg_hits) for a call/analyst doc."""
    flat = text or ""
    if not flat:
        return 0.0, 0, 0
    p = len(_POS.findall(flat))
    n = len(_NEG.findall(flat))
    if p + n == 0:
        return 0.0, 0, 0
    return round((p - n) / (p + n), 3), p, n
