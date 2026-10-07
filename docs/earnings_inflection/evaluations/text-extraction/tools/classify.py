"""Local classifier for amount roles: logistic regression on words and grammar around each amount.
Trained on the dev half, scored on the test half (split by issuer), plus leave-one-issuer-out on all."""
import hashlib, json, re, sys
from collections import Counter
sys.path.insert(0, sys.argv[0].rsplit("/", 1)[0])
from makrograph.earnings_inflection.amount_roles import AMT, BOOK, FUTURE, INFLOW, PAST, VALUE, normalise, nlp, roles   # noqa: E402
from sklearn.feature_extraction import DictVectorizer   # noqa: E402
from sklearn.linear_model import LogisticRegression     # noqa: E402

rows = [json.loads(l) for l in open(sys.argv[1])]
split = lambda r: "dev" if int(hashlib.sha256(r["symbol"].encode()).hexdigest(), 16) % 2 == 0 else "test"  # noqa: E731


def features(sentence):
    doc = nlp()(normalise(sentence))
    rule = roles(sentence)
    feats = []
    for k, m in enumerate(AMT.finditer(doc.text)):
        toks = [t for t in doc if m.start() <= t.idx < m.end()]
        num = next((t for t in toks if t.like_num), toks[-1] if toks else doc[0])
        f = {"rule=" + (rule[k] if k < len(rule) else "other"): 1}
        before, after = doc.text[max(0, m.start() - 60):m.start()].lower(), doc.text[m.end():m.end() + 40].lower()
        for w in re.findall(r"[a-z]+", before)[-8:]:
            f["b:" + w] = 1
        for w in re.findall(r"[a-z]+", after)[:5]:
            f["a:" + w] = 1
        h, path = num, []
        for _ in range(5):
            path.append(h.dep_ + ">" + h.head.lemma_.lower())
            f["path:" + h.dep_ + ">" + h.head.lemma_.lower()] = 1
            if h.head is h:
                break
            h = h.head
        f["root_lemma:" + h.lemma_.lower()] = 1
        for name, rx, text in (("book_near", BOOK, before + after), ("future_near", FUTURE, before[-35:]),
                               ("past_near", PAST, before), ("inflow_sent", INFLOW, doc.text),
                               ("value_near", VALUE, before + after)):
            if rx.search(text):
                f[name] = 1
        feats.append(f)
    return feats


X, y, part, sym = [], [], [], []
for r in rows:
    fs = features(r["sentence"])
    for f, a in zip(fs, r["amounts"]):
        X.append(f)
        y.append(a["label"] if a["label"] != "book_past" else "other")
        part.append(split(r))
        sym.append(r["symbol"])
ROLES = ("book_level", "order_value", "inflow_total")


def report(name, gold, pred):
    out = []
    for role in ROLES:
        tp = sum(g == role and p == role for g, p in zip(gold, pred))
        fp = sum(g != role and p == role for g, p in zip(gold, pred))
        fn = sum(g == role and p != role for g, p in zip(gold, pred))
        out.append(f"{role}: P {tp / (tp + fp) if tp + fp else 0:.2f} R {tp / (tp + fn) if tp + fn else 0:.2f} "
                   f"(tp {tp} fp {fp} fn {fn})")
    print(f"  {name:22s} " + " | ".join(out))


v = DictVectorizer()
tr = [i for i, p in enumerate(part) if p == "dev"]
te = [i for i, p in enumerate(part) if p == "test"]
clf = LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced")
clf.fit(v.fit_transform([X[i] for i in tr]), [y[i] for i in tr])
pred = clf.predict(v.transform([X[i] for i in te]))
print(f"== test half ({len(te)} amounts), trained on dev half ({len(tr)} amounts)")
report("classifier", [y[i] for i in te], pred)
report("spacy rules (in features)", [y[i] for i in te],
       [next((k.split("=")[1] for k in X[i] if k.startswith("rule=")), "other") for i in te])
# leave-one-issuer-out over the whole sample
pred_all = [None] * len(y)
for s in sorted(set(sym)):
    trn = [i for i in range(len(y)) if sym[i] != s]
    tst = [i for i in range(len(y)) if sym[i] == s]
    vv = DictVectorizer()
    c = LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced")
    c.fit(vv.fit_transform([X[i] for i in trn]), [y[i] for i in trn])
    for i, p in zip(tst, c.predict(vv.transform([X[i] for i in tst]))):
        pred_all[i] = p
print(f"== leave-one-issuer-out, all {len(y)} amounts")
report("classifier", y, pred_all)
report("spacy rules", y, [next((k.split("=")[1] for k in X[i] if k.startswith("rule=")), "other") for i in range(len(y))])
