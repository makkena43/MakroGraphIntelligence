"""What does each rupee amount in a sentence refer to?  Grammar-based (spaCy dependency parse), no LLM.

Measured on a labelled random sample (docs/earnings_inflection/evaluations/text-extraction/): on the
held-out half it finds about 6 in 10 current order-book statements where the regex extractor finds about
1 in 8.  spaCy and ``en_core_web_sm`` are optional dependencies: without them ``available()`` is False and
callers keep the regex extractor.

Roles: book_level (current outstanding order book), order_value (a specific order / contract won),
inflow_total (orders received over a period), other.  ``book_past`` is scored as ``other``
(a superseded level is not the current book)."""
import re

_NLP = None

AMT = re.compile(r"(?:rs\.?|inr|₹)\s*~?\s*[\d,]+(?:\.\d+)?\s*(?:crores?|cr\b|lakhs?|lacs?|mn|million|billion|bn)", re.I)

BOOK = re.compile(r"\b(?:order\s*book|orderbook|backlog|order\s+position|unexecuted\s+order|order\s+balance|"
                  r"orders?\s+on\s+hand|unexecuted\s+order\s+value)\b", re.I)
INFLOW = re.compile(r"\border\s+(?:inflows?|intake|booking)s?\b|\b(?:received|secured|won|bagged|added|got)\b[^.]{0,40}\borders?\b"
                    r"|\borders?\s+(?:received|secured|won|worth)\b", re.I)
VALUE = re.compile(r"\b(?:worth|valued\s+at|value\s+of|contract\s+value|order\s+value|size\s+of\s+the\s+order|"
                   r"consideration\s+or\s+size|amount\s+of\s+the\s+(?:work\s+)?order|value\s+of\s+(?:these|the)\b|"
                   r"(?:order|contract)\s+(?:for|of)\b)", re.I)
FLOW_VERB = {"add", "execute", "retire", "receive", "secure", "win", "bag", "book", "get", "realize", "realise"}
STATE_VERB = {"stand", "be", "remain", "exceed", "surpass", "reach", "have", "touch", "cross", "sit", "hold"}
# modality / time / part-of are judged on the words around the amount, not on the whole clause
# (a relative clause "... which will be giving us ..." says nothing about the amount)
FUTURE = re.compile(r"\b(?:expect\w*|will|would|should|could|plan\w*|target\w*|looking\s+at|guidance|by\s+the\s+end|"
                    r"opening|to\s+be\s+quoted)\b", re.I)
PAST = re.compile(r"\b(?:at\s+the\s+end\s+of\s+(?:march|june|september|december)|in\s+the\s+month\s+of|"
                  r"was\s+hovering|used\s+to|as\s+at\s+march\s+31,\s+20\d\d|had\s+told)\b", re.I)
PART_BEFORE = re.compile(r"\b(?:out\s+of|of\s+which|comprising|contributing|portion|part\s+of)\b", re.I)
PART_AFTER = re.compile(r"^[^.]{0,30}\bwhich\s+is\s+\d+\s*%\s+of\b|^\W{0,3}\d+\s*%\s+of\b", re.I)


def available() -> bool:
    try:
        nlp()
        return True
    except Exception:                                            # noqa: BLE001 - optional dependency
        return False


def nlp():
    global _NLP
    if _NLP is None:
        import spacy
        _NLP = spacy.load("en_core_web_sm")
    return _NLP


def normalise(s: str) -> str:
    """Amounts the tokenizer splits badly: "Rs." (sentence end), "INR2,300" (no space), "₹" glyph."""
    s = re.sub(r"\b(Rs|rs|RS)\.\s*", "Rs ", s)
    s = re.sub(r"\b(INR|Rs)(?=\d)", r"\1 ", s)
    s = s.replace("₹", "Rs ").replace("~", "")
    return re.sub(r"\s+", " ", s)


def _amount_spans(doc):
    """(start_char, end_char, head_token) for each amount, in sentence order."""
    out = []
    for m in AMT.finditer(doc.text):
        toks = [t for t in doc if m.start() <= t.idx < m.end()]
        num = next((t for t in toks if t.like_num), toks[-1] if toks else None)
        out.append((m.start(), m.end(), num))
    return out


def _clause(tok):
    """The nearest verb governing ``tok`` and the text of that verb's clause."""
    h = tok
    for _ in range(8):
        if h.pos_ in ("VERB", "AUX") or h.dep_ == "ROOT":
            break
        h = h.head
    return h, " ".join(t.text for t in h.subtree)


def roles(sentence: str) -> list[str]:
    doc = nlp()(normalise(sentence))
    out = []
    for start, end, tok in _amount_spans(doc):
        if tok is None:
            out.append("other")
            continue
        verb, clause = _clause(tok)
        before = doc.text[max(0, start - 60):start]
        after = doc.text[end:end + 60]
        local = doc.text[max(0, start - 70):min(len(doc.text), end + 45)]
        lemma = verb.lemma_.lower() if verb is not None else ""
        aux = {c.lower_ for c in verb.children if c.dep_ in ("aux", "auxpass")} if verb is not None else set()
        modifies_book = any(BOOK.search(" ".join(t.text for t in h.subtree)) and h.pos_ == "NOUN"
                            for h in (tok.head, tok.head.head) if h is not tok)
        future = FUTURE.search(before[-35:]) or bool(aux & {"will", "would", "should", "could"})
        if PART_BEFORE.search(before[-40:]) or PART_AFTER.search(after) or PAST.search(before):
            out.append("other")
        elif BOOK.search(local) and (modifies_book or (BOOK.search(clause) and lemma not in FLOW_VERB and not future))                 and not re.search(r"\band\s+pipeline\b", local, re.I):
            out.append("book_level")
        elif future:
            out.append("other")
        elif INFLOW.search(clause) and re.search(r"\b(?:quarter|year|fy\s?\d|period|month|q[1-4]|nine|during|in\s+the)\b",
                                                  clause, re.I):
            out.append("inflow_total")
        elif VALUE.search(local) or (re.search(r"\b(?:order|contract|work\s+order|loa|letter\s+of)", clause, re.I)
                                      and lemma in {"award", "receive", "secure", "win", "bag", "place"}):
            out.append("order_value")
        else:
            out.append("other")
    return out
