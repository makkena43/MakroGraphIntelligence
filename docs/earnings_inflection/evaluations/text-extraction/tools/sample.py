"""Draw a random, stratified sample of sentences with a rupee amount near an order/capacity/price word.
Usage: python -I sample.py OUT_JSONL FX_DIR... (seeded; the draw does not look at any extractor output)"""
import glob, hashlib, json, os, re, sys
OUT, FXS = sys.argv[1], sys.argv[2:]
AMT = re.compile(r"(?:rs\.?|inr|₹)\s*~?\s*[\d,]+(?:\.\d+)?\s*(?:crores?|cr\b|lakhs?|lacs?|mn|million|billion|bn)", re.I)
TOPIC = re.compile(r"\border(?:s| book|book| inflow| win)?\b|\bbacklog\b|\bcapacity\b|\brealisation|\brealization|\bprice", re.I)
SEED = "ei-nlp-sample-2026-10-07|"
cands = []
for fx in FXS:
    for sym in sorted(os.listdir(fx)):
        ex = os.path.join(fx, sym, "export.json")
        if not os.path.exists(ex):
            continue
        for d in json.load(open(ex))["documents"]:
            if not d.get("text_file"):
                continue
            t = open(os.path.join(fx, sym, d["text_file"]), errors="replace").read()
            t = re.sub(r"\s+", " ", t)
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", t):
                if 40 <= len(s) <= 400 and AMT.search(s) and TOPIC.search(s):
                    h = hashlib.sha256((SEED + sym + d["doc_id"] + s).encode()).hexdigest()
                    cands.append((h, sym, d["doc_id"], d.get("filing_type", ""), d.get("published_at", "")[:10], s))
cands.sort()
seen, picked, per = set(), [], {}
for h, sym, doc, ft, dt, s in cands:
    key = s.lower()[:120]
    if key in seen or per.get(sym, 0) >= 10:
        continue
    seen.add(key)
    per[sym] = per.get(sym, 0) + 1
    picked.append({"id": h[:10], "symbol": sym, "doc_id": doc, "filing_type": ft, "date": dt, "sentence": s})
    if len(picked) == 180:
        break
with open(OUT, "w") as f:
    for p in picked:
        f.write(json.dumps(p, ensure_ascii=False) + "\n")
print("candidates", len(cands), "picked", len(picked), "issuers", len(per))
