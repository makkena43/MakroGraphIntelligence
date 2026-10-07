"""Score amount-role extraction on the labelled sample.  Usage (repo root): PYTHONPATH=src python score.py LABELLED"""
import hashlib, json, re, sys
from collections import Counter
from datetime import datetime
sys.path.insert(0, sys.argv[0].rsplit("/", 1)[0])
from makrograph.earnings_inflection.amount_roles import AMT, roles                                   # noqa: E402
from makrograph.earnings_inflection.chunking import chunk_document  # noqa: E402
from makrograph.earnings_inflection.contracts import Metric, SourceDocument  # noqa: E402
from makrograph.earnings_inflection.extraction import extract_sentence_evidence  # noqa: E402
from makrograph.earnings_inflection.thesis import order_book_snapshots  # noqa: E402

ROLES = ("book_level", "order_value", "inflow_total")


def crore(text):
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)", text.replace("₹", "")[re.search(r"\d", text).start():])
    v = float(m.group(1).replace(",", ""))
    u = m.group(2).lower()
    return v * (0.01 if u.startswith(("lakh", "lac")) else 0.1 if u.startswith(("mn", "million")) else
                100 if u.startswith(("bn", "billion")) else 1.0)


def baseline(sentence, amounts, n):
    """The current rule-based extractor: order-book snapshots and order-win / inflow evidence."""
    doc = SourceDocument(doc_id=f"s{n}", source_name="t", ticker="T", text=sentence, published_at=datetime(2024, 1, 1))
    doc.available_at = doc.published_at
    ev = extract_sentence_evidence(doc, chunk_document(doc))
    books = {round(e.quantity.value, 2) for e in order_book_snapshots(ev)}
    wins = {round(e.quantity.value, 2) for e in ev if e.metric == Metric.ORDER_WIN and e.quantity}
    inflow = {round(e.quantity.value, 2) for e in ev if e.metric == Metric.ORDER_INFLOW and e.quantity}
    out = []
    for a in amounts:
        v = round(crore(a), 2)
        out.append("book_level" if v in books else "order_value" if v in wins else "inflow_total" if v in inflow
                   else "other")
    return out


rows = [json.loads(l) for l in open(sys.argv[1])]
split = lambda r: "dev" if int(hashlib.sha256(r["symbol"].encode()).hexdigest(), 16) % 2 == 0 else "test"  # noqa: E731
res = {"dev": {"baseline": Counter(), "spacy": Counter()}, "test": {"baseline": Counter(), "spacy": Counter()}}
errors = []
for n, r in enumerate(rows):
    gold = [a["label"] if a["label"] != "book_past" else "other" for a in r["amounts"]]
    texts = [a["text"] for a in r["amounts"]]
    preds = {"baseline": baseline(r["sentence"], texts, n), "spacy": roles(r["sentence"])}
    if len(preds["spacy"]) != len(gold):
        preds["spacy"] = (preds["spacy"] + ["other"] * len(gold))[:len(gold)]
    for name, p in preds.items():
        for g, q in zip(gold, p):
            for role in ROLES:
                res[split(r)][name][(role, "tp")] += g == role and q == role
                res[split(r)][name][(role, "fp")] += g != role and q == role
                res[split(r)][name][(role, "fn")] += g == role and q != role
            if g != q:
                errors.append((split(r), name, r["symbol"], g, q, r["sentence"][:200]))
for part in ("dev", "test"):
    print(f"== {part} ({sum(1 for r in rows if split(r) == part)} sentences)")
    for name in ("baseline", "spacy"):
        c = res[part][name]
        line = []
        for role in ROLES:
            tp, fp, fn = c[(role, 'tp')], c[(role, 'fp')], c[(role, 'fn')]
            p = tp / (tp + fp) if tp + fp else 0
            rc = tp / (tp + fn) if tp + fn else 0
            line.append(f"{role}: P {p:.2f} R {rc:.2f} (tp {tp} fp {fp} fn {fn})")
        print(f"  {name:8s} " + " | ".join(line))
json.dump({"results": {p: {n: {f"{k[0]}:{k[1]}": v for k, v in c.items()} for n, c in d.items()} for p, d in res.items()},
           "test_errors": errors}, open(sys.argv[1].replace(".jsonl", "_scores.json"), "w"), indent=1)
