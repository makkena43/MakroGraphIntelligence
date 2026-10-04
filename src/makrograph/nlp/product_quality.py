"""Generic precision guards for physical-product discovery.

The constraint pipeline needs an open product vocabulary, but a general NER
model's ``PRODUCT`` label is not precise enough for that job: filing tables
regularly turn accounting captions, reporting periods, people and locations
into apparent products.  These rules describe *forms that are not products*;
they contain no company, theme, industry, or winner vocabulary.
"""

from __future__ import annotations

import re


NON_PRODUCT_TOKENS = frozenset(
    "a an and are as at be been being by company companies for from has have in "
    "is it its of on or our that the their this to was were will with within "
    "manufacture manufacturing manufactured manufacturer manufacturers produce "
    "production produced producing fabricate fabrication fabricated assemble "
    "assembly assembled plant plants facility facilities factory factories unit "
    "units capacity capacities line lines project projects business businesses "
    "market markets products product services service operations operation annual "
    "report presentation group subsidiary subsidiaries technology systems equipment "
    "solution solutions development developed developing new existing growth expansion "
    "addition increase including range whole various strong leading pioneer efficient "
    "customer customers government under through more than per year years fiscal crore "
    "lakh million global standard standards quality compliance finished good goods "
    "reportable segment segments identity number numbers order book books revenue profit "
    "pat ebitda ebit margin margins cash flow flows equity share shares capital issue "
    "financial finance result results income expenditure expense expenses depreciation "
    "interest tax earnings sales exceptional item items debt total comprehensive net gross "
    "paid unpaid credit value audited unaudited auditor auditors audit accountant accountants "
    "chartered law laws legal regulation regulations listing shareholder shareholders eligible "
    "notice agm cin quarter half period pre covid covid pro forma liabilities liability "
    "partnership llp director directors officer officers manager management employee employees "
    "north south east west pradesh india indian state states city district limited ltd private pvt "
    "registered office leadership team award awards opinion track record portfolio geographies "
    "world class target entity public sector joint venture sq ft foot feet location spread mr etc".split()
)

# A real product is a concrete noun phrase whose HEAD noun (last word) names a
# thing. When the manufacturing-grammar extractor runs over verbose US SEC prose
# ("we manufacture X to reduce overhead costs / address climate change / shorten
# lead times"), the captured "product" is often the object of a financial or
# operational clause — its head noun is abstract. Rejecting on the head noun
# removes "cost savings / climate change / lead times / home loans / cycle times
# / underutilization charges / process technologies / water disposal" while
# keeping every real product (concrete heads: wafer, cell, engine, memory,
# diesel, hydrogen, device, circuit, materials, components, electronics...).
# "materials / components / electronics" are deliberately NOT abstract so
# Cathode Active Materials / Passive Components / Defense electronics survive.
_ABSTRACT_HEAD = frozenset(
    "charge charges benefit benefits loan loans saving savings time times change "
    "changes disposal located defect defects synergy synergies calculation cost "
    "costs technology technologies gathering well wells utilization claim claims "
    "proposal proposals agreement agreements date dates reserve reserves practice "
    "practices growth excellence operation operations activity activities trial "
    "trials requirement requirements relationship relationships service services "
    "solution solutions fee fees rate rates margin margins expense expenses site "
    "sites location locations portion portions process processes initiative "
    "initiatives strategy strategies opportunity opportunities risk risks exposure "
    "measure measures metric metrics target targets guidance outlook synergie "
    "efficiency efficiencies improvement improvements benefit integration integrated automated "
    "opinion award awards record portfolio geography geographies office team class sector space "
    "place side entity capability capabilities defence defense field limited ltd us so now here "
    "along across furthermore compliant complex largest added advanced based opened sixth estate "
    "handling slip".split()
)

_FISCAL_TOKEN_RE = re.compile(
    r"^(?:q[1-4]|h[12]|fy)?[\'’]?(?:fy)?(?:19|20)?\d{2}$|^(?:q[1-4]|h[12])(?:fy)?\d*$",
    re.I,
)
_OCR_REPEAT_RE = re.compile(r"^(?:([a-z])\1){3,}[a-z]*$", re.I)
_WEB_RE = re.compile(r"\b(?:https?[:.]|www[.\s])|\.(?:com|in|org|net)\b", re.I)
_MEASUREMENT_TOKENS = frozenset({
    "gw", "gwh", "mw", "mwh", "kw", "kwh", "mt", "kt", "kg", "km",
    "sqm", "sqft", "sq", "ft", "cr", "crore", "mn", "bn", "million",
})
_GENERIC_DISCLOSURE_PHRASES = frozenset({
    "raw material", "raw materials", "finished good", "finished goods",
    "value added", "fully integrated", "track record", "joint venture",
    "private limited", "pvt ltd", "co ltd", "excellence award",
    "registered office", "public sector", "world class",
})


def product_label_quality(
    value: str | None,
    *,
    min_words: int = 2,
    max_words: int = 8,
) -> tuple[bool, str]:
    """Return a conservative lexical product verdict and an audit reason.

    This is only a precision gate.  Passing it never proves a company role or
    a physical constraint; those require their own dated evidence stages.
    """
    raw = re.sub(r"\s+", " ", (value or "").strip())
    if not raw:
        return False, "empty"
    if _WEB_RE.search(raw):
        return False, "web_or_domain"
    if re.search(r"[,;!?]|\.(?:\s|$)", raw):
        return False, "list_not_noun_phrase"
    words = re.findall(r"[A-Za-z][A-Za-z0-9+./&-]*", raw)
    if not min_words <= len(words) <= max_words:
        return False, "word_count"
    lowered = [word.casefold().strip("./") for word in words]
    lexical_phrase = " ".join(re.sub(r"[^a-z0-9]+", " ", raw.casefold()).split())
    if lexical_phrase in _GENERIC_DISCLOSURE_PHRASES:
        return False, "generic_disclosure_phrase"
    if len(lowered) == 2 and lowered[0] in _MEASUREMENT_TOKENS:
        return False, "measurement_prefix_without_product_modifier"
    if any(_FISCAL_TOKEN_RE.fullmatch(word) for word in lowered):
        return False, "reporting_period"
    if any(_OCR_REPEAT_RE.fullmatch(word) for word in lowered):
        return False, "ocr_artifact"
    meaningful = [
        word for word in lowered
        if word not in NON_PRODUCT_TOKENS
        and word not in _MEASUREMENT_TOKENS
        and len(word) >= 2
    ]
    if len(meaningful) < min(2, min_words):
        return False, "disclosure_boilerplate"
    # The head noun (last alphabetic word) must name a thing, not an abstract
    # financial/operational concept — the object of "manufacture X to cut costs"
    # is not a product.
    head = lowered[-1]
    head = head[:-1] if len(head) > 3 and head.endswith("s") else head
    if head in {(w[:-1] if len(w) > 3 and w.endswith("s") else w) for w in _ABSTRACT_HEAD}:
        return False, "abstract_head_noun"
    return True, "lexically_plausible_product"


def is_product_label(value: str | None, *, min_words: int = 2, max_words: int = 8) -> bool:
    return product_label_quality(value, min_words=min_words, max_words=max_words)[0]
