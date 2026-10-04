"""Dated company / security / segment / counterparty identity resolution.

Symbols are not identities.  NSE renames, mergers and SME migrations mean a
symbol can point to different issuers over time, and the inspected
``nse_bhavcopy_data`` DDL keys rows by ``(trade_date, symbol)`` with no ISIN
and no series in the key, so a warrant or partly-paid line can collide with
the equity line.  This module keeps identity explicit and dated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .contracts import IssuerModel, ListingSegment

# Equity series that represent the ordinary share of an operating issuer.
NSE_MAINBOARD_EQUITY_SERIES = {"EQ", "BE", "BZ"}
NSE_SME_SERIES = {"SM", "ST", "SZ"}
# Anything else (W1/W2 warrants, partly paid E1.., N*/Y* debt, RR, IV ...) is
# not the operating equity and must never be blended into an equity series.


@dataclass
class SymbolSpan:
    symbol: str
    exchange: str
    issuer_id: str
    valid_from: date
    valid_to: Optional[date] = None
    isin: str = ""
    series: str = ""


@dataclass
class IssuerProfile:
    issuer_id: str
    name: str
    country: str = "IN"
    industry: str = ""
    issuer_model: IssuerModel = IssuerModel.UNKNOWN
    listing_segment: ListingSegment = ListingSegment.UNKNOWN
    segments: list[str] = field(default_factory=list)
    classification_basis: str = ""


class IdentityResolver:
    """Resolve (symbol, date) to a dated issuer id.

    With no explicit history the symbol itself is used as the issuer id and
    ``resolution_basis`` reports ``symbol_assumed_stable`` so downstream
    output can disclose it.
    """

    def __init__(self, spans: Optional[list[SymbolSpan]] = None):
        self._spans = list(spans or [])

    def resolve(self, symbol: str, on: date, exchange: str = "") -> tuple[str, str]:
        sym = (symbol or "").upper().strip()
        hits = [s for s in self._spans
                if s.symbol.upper() == sym
                and (not exchange or s.exchange.upper() == exchange.upper())
                and s.valid_from <= on and (s.valid_to is None or on <= s.valid_to)]
        if len(hits) == 1:
            return hits[0].issuer_id, "dated_symbol_history"
        if len(hits) > 1:
            ids = sorted({h.issuer_id for h in hits})
            if len(ids) == 1:
                return ids[0], "dated_symbol_history"
            raise ValueError(f"ambiguous symbol {sym} on {on}: {ids}")
        return sym, "symbol_assumed_stable"

    def symbols_for(self, issuer_id: str) -> list[SymbolSpan]:
        return [s for s in self._spans if s.issuer_id == issuer_id]


def is_operating_equity_series(series: Optional[str], allow_sme: bool = True) -> Optional[bool]:
    """True/False for a known series, ``None`` when the series is unknown.

    Callers must treat ``None`` as *ambiguous identity*, not as equity.
    """
    if not series:
        return None
    s = series.upper().strip()
    if s in NSE_MAINBOARD_EQUITY_SERIES:
        return True
    if s in NSE_SME_SERIES:
        return allow_sme
    return False


def listing_segment_from(series: Optional[str] = None, board: str = "") -> ListingSegment:
    if series and series.upper() in NSE_SME_SERIES:
        return ListingSegment.SME
    if re.search(r"\bsme\b|emerge", board or "", re.I):
        return ListingSegment.SME
    if series and series.upper() in NSE_MAINBOARD_EQUITY_SERIES:
        return ListingSegment.MAINBOARD
    if board:
        return ListingSegment.MAINBOARD
    return ListingSegment.UNKNOWN


_INDUSTRY_MAP = [
    (re.compile(r"\bbank", re.I), IssuerModel.BANK),
    (re.compile(r"insurance|assurance|reinsur", re.I), IssuerModel.INSURER),
    (re.compile(r"asset management|investment compan|holding compan|mutual fund|venture capital", re.I), IssuerModel.INVESTMENT_COMPANY),
    (re.compile(r"\bnbfc\b|non[- ]banking financ|housing financ|microfinanc|consumer financ|financial services|lending", re.I), IssuerModel.NBFC),
]
_NAME_STRONG = [
    (re.compile(r"\bbank\b", re.I), IssuerModel.BANK),
    (re.compile(r"\b(?:insurance|assurance)\b", re.I), IssuerModel.INSURER),
]
_NAME_WEAK = re.compile(r"\b(?:finance|financial|finserv|capital|investments?|holdings?|credit)\b", re.I)


def classify_issuer_model(name: str = "", industry: str = "", text_hint: str = "") -> tuple[IssuerModel, str]:
    """Classify the business model used for earnings bridges.

    Industry metadata wins; a strong name keyword ("Bank", "Insurance") is
    next; weak name keywords ("Capital", "Finance") only produce UNKNOWN with
    a review note because many industrial companies carry them.  Financial
    statement layouts ("Interest earned", "Net premium") are used as a hint.
    """
    for pat, model in _INDUSTRY_MAP:
        if industry and pat.search(industry):
            return model, f"industry:{industry}"
    if industry:
        return IssuerModel.OPERATING, f"industry:{industry}"
    for pat, model in _NAME_STRONG:
        if pat.search(name or ""):
            return model, "name_keyword"
    hint = text_hint[:30000]
    if re.search(r"interest earned|advances to customers|gross npa|net interest income", hint, re.I):
        return IssuerModel.BANK, "statement_layout:bank"
    if re.search(r"net premium|gross written premium|policyholders'? account", hint, re.I):
        return IssuerModel.INSURER, "statement_layout:insurer"
    if _NAME_WEAK.search(name or ""):
        return IssuerModel.UNKNOWN, "name_ambiguous_financial_keyword:needs_review"
    if re.search(r"revenue from operations|cost of materials consumed", hint, re.I):
        return IssuerModel.OPERATING, "statement_layout:operating"
    return IssuerModel.UNKNOWN, "no_classification_evidence"


# --- counterparties ---------------------------------------------------------

_SUFFIXES = re.compile(
    r"\b(?:private|pvt|public|limited|ltd|llp|inc|incorporated|corp|corporation|company|co|plc|gmbh|ag|sa|bv)\b\.?",
    re.I,
)


def normalize_counterparty(name: str) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = _SUFFIXES.sub(" ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


_LEGAL_FORM = re.compile(r"\b(?:private|pvt|public|limited|ltd|llp|inc|incorporated|plc|gmbh|ag|sa|bv)\b\.?", re.I)


def _acronym(name: str) -> str:
    """Acronym over the full name minus legal form ("National Thermal Power Corporation" -> "ntpc")."""
    n = re.sub(r"[^a-z0-9 ]+", " ", _LEGAL_FORM.sub(" ", (name or "").lower().replace("&", " and ")))
    return "".join(w[0] for w in n.split() if w not in {"of", "and", "the"})


def same_counterparty(a: str, b: str) -> Optional[bool]:
    """True/False when both are named; ``None`` when either is unknown."""
    na, nb = normalize_counterparty(a), normalize_counterparty(b)
    if not na or not nb:
        return None
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    # acronym match: "ntpc" vs "national thermal power corporation"
    acr_a, acr_b = _acronym(a), _acronym(b)
    if (len(na.split()) == 1 and na == acr_b) or (len(nb.split()) == 1 and nb == acr_a):
        return True
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return overlap >= 0.8
