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


# ---------------------------------------------------------------------------
# Issuer registry (WP2): stable issuer identity, effective-dated aliases,
# separate security identity, explicit predecessor comparability decisions
# ---------------------------------------------------------------------------

class ReplayMode:
    """How historical as-of runs decide what was knowable.

    PUBLIC_INFORMATION_RECONSTRUCTION: anything publicly available by the cutoff,
        even if MakroGraph ingested it later (states its reconstruction limits).
    SYSTEM_KNOWLEDGE_REPLAY: additionally requires that MakroGraph held the
        document, its extracted text and any identity mapping by the cutoff.
        Legacy rows that cannot prove those times are excluded and counted.
    """
    RECONSTRUCTION = "PUBLIC_INFORMATION_RECONSTRUCTION"
    SYSTEM = "SYSTEM_KNOWLEDGE_REPLAY"
    ALL = (RECONSTRUCTION, SYSTEM)


@dataclass
class AliasRecord:
    alias: str                         # NSE symbol, BSE scrip code, ISIN ...
    kind: str                          # "nse_symbol" | "bse_scrip" | "isin" | "symbol"
    issuer_id: str
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    recorded_at: Optional["datetime"] = None   # when the mapping became known (system replay)
    source: str = ""

    def covers(self, d: date) -> bool:
        return (self.valid_from is None or self.valid_from <= d) and (self.valid_to is None or d <= self.valid_to)


@dataclass
class SecurityRecord:
    """Tradable security identity - deliberately separate from the issuer."""
    isin: str = ""
    exchange: str = ""
    symbol: str = ""
    series: str = ""
    board: str = ""                    # "mainboard" | "sme"
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    source: str = ""

    def covers(self, d: date) -> bool:
        return (self.valid_from is None or self.valid_from <= d) and (self.valid_to is None or d <= self.valid_to)


@dataclass
class PredecessorLink:
    predecessor_issuer_id: str
    effective_date: date
    comparable: Optional[bool] = None   # None = no decision: never spliced into one series
    decision_source: str = ""


@dataclass
class IssuerRecord:
    issuer_id: str
    name: str = ""
    aliases: list[AliasRecord] = field(default_factory=list)
    securities: list[SecurityRecord] = field(default_factory=list)
    predecessors: list[PredecessorLink] = field(default_factory=list)
    industry_history: list[dict] = field(default_factory=list)   # {"valid_from","industry","source"}

    def listing_at(self, d: date) -> Optional[SecurityRecord]:
        eq = [s for s in self.securities if s.covers(d) and (is_operating_equity_series(s.series) is not False)]
        return eq[-1] if eq else None

    def industry_at(self, d: date) -> tuple[str, str]:
        hits = [h for h in self.industry_history if (h.get("valid_from") is None or h["valid_from"] <= d)]
        if not hits:
            return "", ""
        h = max(hits, key=lambda x: x.get("valid_from") or date.min)
        return h["industry"], h.get("source", "")


def _dt(v):
    from datetime import datetime as _datetime
    if v in (None, ""):
        return None
    if isinstance(v, (date, _datetime)):
        return v
    return _datetime.fromisoformat(str(v)) if "T" in str(v) else date.fromisoformat(str(v))


class IssuerRegistry:
    """Resolves a requested symbol to a stable issuer and its eligible aliases."""

    def __init__(self, issuers: Optional[list[IssuerRecord]] = None):
        self.issuers = {i.issuer_id: i for i in (issuers or [])}

    @classmethod
    def from_dict(cls, data: dict) -> "IssuerRegistry":
        out = []
        for iid, rec in (data or {}).items():
            out.append(IssuerRecord(
                issuer_id=iid, name=rec.get("name", ""),
                aliases=[AliasRecord(alias=a["alias"], kind=a.get("kind", "symbol"), issuer_id=iid,
                                     valid_from=_dt(a.get("valid_from")), valid_to=_dt(a.get("valid_to")),
                                     recorded_at=_dt(a.get("recorded_at")), source=a.get("source", ""))
                         for a in rec.get("aliases", [])],
                securities=[SecurityRecord(**{**sec, "valid_from": _dt(sec.get("valid_from")),
                                              "valid_to": _dt(sec.get("valid_to"))})
                            for sec in rec.get("securities", [])],
                predecessors=[PredecessorLink(p["predecessor_issuer_id"], _dt(p["effective_date"]),
                                              p.get("comparable"), p.get("decision_source", ""))
                              for p in rec.get("predecessors", [])],
                industry_history=[{**h, "valid_from": _dt(h.get("valid_from"))}
                                  for h in rec.get("industry_history", [])],
            ))
        return cls(out)

    def merge(self, other: "IssuerRegistry") -> "IssuerRegistry":
        merged = dict(self.issuers)
        merged.update(other.issuers)
        return IssuerRegistry(list(merged.values()))

    @staticmethod
    def _known(a: AliasRecord, cutoff, mode: str) -> bool:
        if mode != ReplayMode.SYSTEM:
            return True
        return a.recorded_at is not None and a.recorded_at <= cutoff

    def resolve(self, requested: str, cutoff, mode: str = ReplayMode.RECONSTRUCTION
                ) -> tuple[Optional[IssuerRecord], str]:
        """Issuer for a requested symbol.  Old and new symbols both resolve; when a
        symbol was reused, the issuer holding it most recently at/before the cutoff wins."""
        sym = (requested or "").upper().strip()
        cut_d = cutoff.date() if hasattr(cutoff, "date") else cutoff
        hits = [(i, a) for i in self.issuers.values() for a in i.aliases
                if a.alias.upper() == sym and self._known(a, cutoff, mode)]
        if not hits:
            unproven = [(i, a) for i in self.issuers.values() for a in i.aliases if a.alias.upper() == sym]
            if unproven and mode == ReplayMode.SYSTEM:
                return None, "alias_mapping_not_known_by_cutoff"
            return None, "symbol_assumed_stable"
        live = [(i, a) for i, a in hits if a.covers(cut_d)]
        pool = live or [(i, a) for i, a in hits if a.valid_from is None or a.valid_from <= cut_d] or hits
        best = max(pool, key=lambda t: t[1].valid_from or date.min)
        ids = {i.issuer_id for i, a in pool if (a.valid_from or date.min) == (best[1].valid_from or date.min)}
        if len(ids) > 1:
            raise ValueError(f"ambiguous alias {sym} at {cut_d}: {sorted(ids)} (needs review)")
        return best[0], "issuer_registry"

    def eligible_aliases(self, issuer: IssuerRecord, cutoff, mode: str) -> list[AliasRecord]:
        cut_d = cutoff.date() if hasattr(cutoff, "date") else cutoff
        return [a for a in issuer.aliases if self._known(a, cutoff, mode)
                and (a.valid_from is None or a.valid_from <= cut_d)]

    def comparable_predecessors(self, issuer: IssuerRecord) -> tuple[list[IssuerRecord], list[str]]:
        """Predecessors explicitly decided comparable; notes for those that are not."""
        use, notes = [], []
        for p in issuer.predecessors:
            pred = self.issuers.get(p.predecessor_issuer_id)
            if p.comparable is True and pred is not None:
                use.append(pred)
                notes.append(f"predecessor {p.predecessor_issuer_id} history included up to {p.effective_date} "
                             f"(comparability decision: {p.decision_source or 'recorded'})")
            else:
                notes.append(f"predecessor {p.predecessor_issuer_id} history excluded "
                             + ("(decided not comparable)" if p.comparable is False
                                else "(no comparability decision recorded)"))
        return use, notes


def alias_for_document(aliases: list[AliasRecord], doc_ticker: str, doc_date: Optional[date]
                       ) -> Optional[AliasRecord]:
    """The alias under which a document belongs to the issuer (validity must cover its date)."""
    t = (doc_ticker or "").upper()
    for a in aliases:
        if a.alias.upper() == t and (doc_date is None or a.covers(doc_date)):
            return a
    return None


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
