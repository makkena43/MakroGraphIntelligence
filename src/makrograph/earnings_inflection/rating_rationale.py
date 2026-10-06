"""Credit-rating rationales (ICRA, CARE, CRISIL, India Ratings, Acuite, Infomerics, Brickwork).

A rationale is read in full, not reduced to "rating upgraded".  It is the best public
source for several operating facts small companies rarely put in their own filings:
capacity and utilisation, capex scope / funding / completion dates, order book and its
execution horizon, customer concentration and payment protection, input-cost
pass-through, working-capital needs and debt-service metrics.

Two rules:
* every rationale is a dated version; later ones never overwrite earlier ones;
* an agency repeating a plan or a forecast (``expected=True``) is corroborating context,
  never independent proof that execution happened.  Observed facts ("order book stood at
  ... as of June 2024", "incurred capex ... to increase capacity to ...") are dated
  third-party restatements of company information, still not audited figures.

Rationales reach the detector as documents: the copies companies file with the exchange
(Reg. 30 "Credit Rating" filings) and, when explicitly enabled, an agency-site source.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable, Optional, Protocol

from .contracts import RatingRationale, RationaleFact, SourceDocument

AGENCIES = [
    ("ICRA", re.compile(r"\bICRA\b")),
    ("CARE", re.compile(r"\bCARE\s*(?:Ratings|Edge|BBB|A\d|AA|BB|\[)|CareEdge", re.I)),
    ("CRISIL", re.compile(r"\bCRISIL\b", re.I)),
    ("India Ratings", re.compile(r"India\s+Ratings|\bInd-?Ra\b|\bIND\s+(?:AA|A|BBB|BB)\b")),
    ("Acuite", re.compile(r"\bAcuit[eé]\b", re.I)),
    ("Infomerics", re.compile(r"\bInfomerics\b", re.I)),
    ("Brickwork", re.compile(r"\bBrickwork\b|\bBWR\b")),
]
_RATIONALE_MARKERS = re.compile(r"\brationale\b|key\s+rating\s+drivers|rating\s+sensitivities|"
                                r"summary\s+of\s+rating\s+action|credit\s+strengths|credit\s+challenges|"
                                r"rating\s+action|press\s+release", re.I)


def agency_of(text: str) -> str:
    hits = [(len(rx.findall(text)), name) for name, rx in AGENCIES]
    n, name = max(hits)
    return name if n else ""


def is_rating_rationale(text: str) -> bool:
    head = text[:60000]
    return bool(agency_of(head)) and len(set(m.group(0).lower() for m in _RATIONALE_MARKERS.finditer(head))) >= 2


# --- sentence handling ----------------------------------------------------------------------

_EXPECT = re.compile(r"\b(?:expect\w*|plan\w*|propos\w*|will|would|likely|estimat\w*|project\w*|anticipat\w*|"
                     r"going forward|envisag\w*|to be (?:executed|completed|commissioned|funded|incurred))\b", re.I)


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"[ \t]+", " ", text)
    flat = re.sub(r"-\n(?=[a-z])", "", flat)
    flat = re.sub(r"\s*\n\s*", " ", flat)
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(~₹])|(?<=\.)\s+(?=Rs)", flat)
    out, seen = [], set()
    for p in parts:
        p = p.strip()
        key = re.sub(r"\W+", "", p.lower())[:160]
        if 20 <= len(p) <= 1200 and key not in seen:
            seen.add(key)
            out.append(p)
    return out


_NUM = r"([\d,]+(?:\.\d+)?)"
_RS = r"(?:rs\.?|inr|₹)\s*~?\s*"
_CR = r"\s*(crores?|cr\b|lakhs?|lacs?|million|mn|billion|bn)"
_WHEN = r"((?:as\s+(?:of|on|at)\s+)?(?:(?:[A-Z][a-z]+\s+\d{1,2},?\s+)?(?:[A-Z][a-z]+\s+)?\d{4}(?:-end)?|FY\s?\d{2,4}(?:-end)?|" \
        r"H[12]\s*FY\s?\d{2,4}|Q[1-4]\s*FY\s?\d{2,4}))"


def _crore(value: str, unit: str) -> Optional[float]:
    try:
        v = float(value.replace(",", ""))
    except ValueError:
        return None
    u = unit.lower()
    if u.startswith(("lakh", "lac")):
        return v / 100
    if u in ("million", "mn"):
        return v / 10
    if u in ("billion", "bn"):
        return v * 100
    return v


_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("order_book", re.compile(r"order\s*book[^.;]{0,50}?" + _RS + _NUM + _CR, re.I)),
    ("order_book_cover", re.compile(r"order\s*book[^.;]{0,120}?" + _NUM + r"\s*(?:times|x)\b[^.;]{0,30}?"
                                    r"(?:operating income|revenue|sales|OI|turnover)", re.I)),
    ("execution_horizon", re.compile(r"(?:to be executed|executable|execution)\s+(?:by|over|within|in)\s+(?:the\s+)?"
                                     r"((?:next\s+)?[\w\s-]{2,30}?(?:months?|years?|\d{4}(?:-end)?|FY\s?\d{2,4}))", re.I)),
    ("capacity_change", re.compile(r"capacity\s+(?:of\s+(?:\w+\s+){1,3})?(?:to\s+" + _NUM + r"\s*([A-Za-z]{2,6})\s+from\s+"
                                   + _NUM + r"\s*([A-Za-z]{2,6})|from\s+" + _NUM + r"\s*([A-Za-z]{2,6})\s+to\s+" + _NUM
                                   + r"\s*([A-Za-z]{2,6}))", re.I)),
    ("capacity", re.compile(r"(?:installed|production|manufacturing|testing|annual)\s+capacity\s+(?:of|at|is|stood at)\s+"
                            + _NUM + r"\s*(MVA|MW|GW|KW|TPA|MTPA|KTPA|KLPD|KLD|MT|tonnes|tons|units|lakh units|"
                            r"million units|pieces|sets)", re.I)),
    # plant / capacity utilisation only; "average utilisation of fund-based limits" is liquidity
    ("utilization", re.compile(r"(?:capacity|plant|production|operating|asset)\s+utili[sz]ation[^.;]{0,50}?"
                               + _NUM + r"\s*%|utili[sz]ation\s+of\s+(?:the\s+)?(?:installed\s+)?capacity[^.;]{0,40}?"
                               + _NUM + r"\s*%", re.I)),
    ("bank_limit_utilization", re.compile(r"(?:average\s+)?(?:working[- ]capital\s+|fund[- ]based\s+|bank\s+)?"
                                          r"(?:limits?\s+)?utili[sz]ation[^.;]{0,60}?" + _NUM + r"\s*%[^.;]{0,80}?"
                                          r"(?:months|limits|facilit)", re.I)),
    ("capex", re.compile(r"(?:capex|capital expenditure)\s+(?:of|outlay of|programme of|plan of)\s+(?:about\s+|around\s+)?"
                         + _RS + _NUM + _CR, re.I)),
    ("capex_funding", re.compile(r"funded\s+(?:predominantly\s+|largely\s+|mainly\s+|entirely\s+|primarily\s+)?"
                                 r"(?:through|by|via|from)\s+([^;]{3,120}?)(?=;|\.\s+[A-Z]|\.\s*$|$)",
                                 re.I)),
    ("completion", re.compile(r"(?:complet|commission|commenc|operational|COD|start\s+operations)\w*[^.;]{0,60}?"
                              r"\b(?:by|in|from|during|before)\s+" + _WHEN, re.I)),
    ("customer_concentration", re.compile(r"top\s+(\d+|one|two|three|five|ten|twenty)\s+(?:customers|clients)[^.;]{0,80}?"
                                          + _NUM + r"\s*%", re.I)),
    ("payment_protection", re.compile(r"\b(LC[- ]backed|letters? of credit|advance payments?|escrow|payment security|"
                                      r"backed by (?:LCs?|bank guarantees?)|secured payment)", re.I)),
    ("pass_through", re.compile(r"(?:(?:~|about\s+|around\s+)?" + _NUM + r"\s*%\s*of\s+(?:the\s+)?(?:\w+\s+){0,4}"
                                r"(?:orders?|contracts?|order mix|sales)[^.;]{0,100}?)?(price[- ]variation|"
                                r"price[- ]escalation|pass[- ]through|escalation clauses?)", re.I)),
    ("working_capital_intensity", re.compile(r"working[- ]capital intensity[^.;]{0,50}?\(?" + _NUM + r"\s*%", re.I)),
    ("receivable_days", re.compile(r"(?:receivable|debtor)s?\s*(?:period|days|cycle)[^.;]{0,30}?" + _NUM + r"\s*days",
                                   re.I)),
    ("inventory_days", re.compile(r"inventory\s*(?:period|days|holding)[^.;]{0,30}?" + _NUM + r"\s*days", re.I)),
    ("dscr", re.compile(r"(?:DSCR|debt service coverage(?: ratio)?)\b[^.;\d]{0,35}?" + _NUM + r"\s*(?:times|x)\b",
                        re.I)),
    ("interest_coverage", re.compile(r"interest coverage[^.;]{0,25}?" + _NUM + r"\s*(?:times|x)\b", re.I)),
    ("debt_obligations", re.compile(r"(?:repayment|debt)\s+obligations?\s+of\s+" + _RS + _NUM + _CR, re.I)),
    ("liquidity", re.compile(r"liquidity(?:\s+position)?\s*(?:is|:|-|–)\s*(superior|strong|adequate|stretched|poor)",
                             re.I)),
]

_UNITS = {"bank_limit_utilization": "%", "order_book": "crore", "order_book_cover": "x", "capacity": "", "utilization": "%", "capex": "crore",
          "customer_concentration": "%", "pass_through": "% of orders", "working_capital_intensity": "% of OI",
          "receivable_days": "days", "inventory_days": "days", "dscr": "x", "interest_coverage": "x",
          "debt_obligations": "crore"}


_OBSERVED = re.compile(r"\bas\s+(?:of|on|at)\s+[A-Z][a-z]+|\b(?:stood|stands|was|were|has been|had|incurred|"
                       r"increased|reported|remained|remains|improved|declined|commissioned|completed|accounted|"
                       r"accounting)\b", re.I)
_SENSITIVITY = re.compile(r"could be (?:upgraded|downgraded|revised)|specific credit metric|for (?:a |an )?"
                          r"(?:downgrade|upgrade)|rating sensitivities|negative factors?|positive factors?", re.I)


def _is_expected(m: re.Match, sent: str) -> bool:
    """Plan / forecast vs observed: judged on the clause around the matched fact, not the
    whole sentence ("order book of Rs X as of March; revenues are expected to grow")."""
    start = max(sent.rfind(",", 0, m.start()), sent.rfind(";", 0, m.start()), 0)
    clause = sent[start:min(len(sent), m.end() + 40)]
    if re.search(r"^[^.;]{0,30}?\bas\s+(?:of|on|at)\s+[A-Z]", sent[m.end():]) or re.search(
            r"\bas\s+(?:of|on|at)\s+[A-Z]", m.group(0)):
        return False                                    # a dated snapshot is an observation
    if _EXPECT.search(sent[max(0, m.start() - 40):m.start()]) or _EXPECT.search(m.group(0)):
        return True                                     # "expected to be commissioned by ..."
    if _OBSERVED.search(sent[max(0, m.start() - 40):m.end() + 25]):
        return bool(re.search(r"\b(?:will|to be|plans?\s+to|expected\s+to)\b", m.group(0), re.I))
    return bool(_EXPECT.search(clause) or _EXPECT.search(sent[max(0, m.start() - 60):m.start()]))


def _fact(name: str, m: re.Match, sent: str) -> Optional[RationaleFact]:
    expected = _is_expected(m, sent)
    g = [x for x in m.groups() if x is not None]
    value, unit, text = None, _UNITS.get(name, ""), m.group(0)
    if name in ("order_book", "capex", "debt_obligations"):
        value = _crore(g[0], g[1])
        when = re.search(r"as\s+(?:of|on|at)\s+([A-Z][a-z]+\s+(?:\d{1,2},?\s+)?\d{4})", sent)
        text = f"Rs {value:,.1f} crore" + (f" as of {when.group(1)}" if when else "") if value is not None else text
    elif name == "capacity_change":
        if m.group(1):
            to_v, to_u, from_v, from_u = m.group(1), m.group(2), m.group(3), m.group(4)
        else:
            from_v, from_u, to_v, to_u = m.group(5), m.group(6), m.group(7), m.group(8)
        if from_u.lower() != to_u.lower():
            return None
        a, b = float(from_v.replace(",", "")), float(to_v.replace(",", ""))
        value, unit, text = b, to_u, f"{a:g} -> {b:g} {to_u}"
    elif name == "capacity":
        value, unit = float(g[0].replace(",", "")), g[1]
    elif name in ("execution_horizon", "capex_funding", "completion", "payment_protection", "liquidity"):
        text = g[-1].strip() if g else m.group(0)
    elif name == "customer_concentration":
        value, text = float(g[-1]), f"top {g[0]} customers {g[-1]}%"
    elif name == "pass_through":
        num = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", m.group(0))
        value = float(num.group(1)) if num else None
        text = f"{value:g}% of orders with {g[-1].lower()}" if value is not None else g[-1].lower()
    else:
        try:
            value = float(g[0].replace(",", ""))
        except (ValueError, IndexError):
            return None
    return RationaleFact(name, value, unit, text, expected, sent[:500])


_MONTH_DATE = re.compile(r"\b((?:January|February|March|April|May|June|July|August|September|October|November|"
                         r"December)\s+\d{1,2},\s+\d{4})\b")


def parse_rationale(doc: SourceDocument) -> Optional[RatingRationale]:
    text = doc.full_text() or ""
    if not is_rating_rationale(text):
        return None
    agency = agency_of(text[:60000])
    r = RatingRationale(doc_id=doc.doc_id, agency=agency, published_at=getattr(doc, "available_at", None)
                        or doc.published_at, rationale_date=None)
    # the rationale's own date: the date line just above the "<Company>: Ratings ..." heading
    head = re.search(r"(?:^|\n)\s*((?:January|February|March|April|May|June|July|August|September|October|November|"
                     r"December)\s+\d{1,2},\s+\d{4})\s*\n[^\n]{0,160}?:\s*(?:Ratings?|Rating action|Long[- ]term)",
                     text)
    when = head.group(1) if head else (_MONTH_DATE.search(text).group(1) if _MONTH_DATE.search(text) else None)
    if when:
        try:
            r.rationale_date = datetime.strptime(when.replace(",", ""), "%B %d %Y").date()
        except ValueError:
            pass
    # the action in the rationale's heading ("XYZ Limited: Ratings reaffirmed; rated amount enhanced")
    act = (re.search(r":\s*(?:Long[- ]term\s+)?ratings?\s+(upgraded|downgraded|reaffirmed|assigned|withdrawn|revised|"
                     r"placed on (?:rating )?watch)", text, re.I)
           or re.search(r"(?:ratings?|outlook)\s+(?:has been\s+|have been\s+)?(upgraded|downgraded|reaffirmed|"
                        r"assigned|withdrawn|revised|placed on (?:rating )?watch)", text, re.I))
    r.action = act.group(1).lower() if act else ""
    # long-term rating = the one carrying an outlook ("[ICRA]BBB- (Stable)", "IND BBB/Stable")
    lt = re.search(r"\[(ICRA|CARE|IND|CRISIL)\]\s*(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|C|D)\s*"
                   r"(?:\(|/|;)\s*(?:Stable|Positive|Negative|Developing)|\b(CRISIL|CARE|IND|ACUITE|BWR|IVR)\s+"
                   r"(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|C|D)\s*(?:\(|/|;)\s*(?:Stable|Positive|Negative|"
                   r"Developing)", text)
    if lt:
        r.long_term_rating = " ".join(x for x in lt.groups() if x)
    out = re.search(r"(?:\(|;\s*|/)(Stable|Positive|Negative|Developing)\b|outlook\s+(?:is|on[^.]{0,40}?is)\s+"
                    r"(stable|positive|negative|developing)", text, re.I)
    if out:
        r.outlook = (out.group(1) or out.group(2)).capitalize()
    seen = set()
    for sent in _sentences(text):
        if _SENSITIVITY.search(sent):
            r.sensitivities.append(sent[:400])      # triggers for a rating change: not facts
            continue
        for name, rx in _PATTERNS:
            for m in rx.finditer(sent):
                f = _fact(name, m, sent)
                if f is None:
                    continue
                key = (f.field, f.value, f.text.lower()[:60])
                if key not in seen:
                    seen.add(key)
                    r.facts.append(f)
    if "utilization" in {f.field for f in r.facts}:
        r.facts = [f for f in r.facts if not (f.field == "bank_limit_utilization"
                                              and any(f.quote == g.quote for g in r.get("utilization")))]
    r.facts = [f for f in r.facts if not (f.field == "utilization"
                                          and re.search(r"fund[- ]based|working[- ]capital limit|bank limit", f.quote, re.I))]
    return r


def rationale_history(rationales: Iterable[RatingRationale]) -> list[RatingRationale]:
    """Every dated version, oldest first; the same rationale filed twice counts once."""
    out, seen = [], set()
    for r in sorted(rationales, key=lambda x: (x.rationale_date or date.min,
                                               x.published_at.isoformat() if x.published_at else "", x.doc_id)):
        key = (r.agency, r.rationale_date, r.long_term_rating, len(r.facts))
        if r.rationale_date and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# --- sources ------------------------------------------------------------------------------------

class RatingRationaleSource(Protocol):
    def documents(self, ticker: str, as_of: datetime) -> list[SourceDocument]: ...


class AgencyWebSourceDisabled:
    """Agency websites (icra.in, careratings.com, crisilratings.com, indiaratings.co.in) publish
    the same rationales.  Fetching them is network work and needs explicit authorisation; until
    a fetcher is approved and configured this source returns nothing and says why.  Companies'
    own exchange filings already carry most rationales (Reg. 30 "Credit Rating" filings)."""

    name = "agency_web"
    reason = "agency-website retrieval not authorised (network); using exchange-filed rationales only"

    def documents(self, ticker: str, as_of: datetime) -> list[SourceDocument]:
        return []
