"""Draw the pre-registered rules-5 cohort (COHORT_PREREGISTRATION.md). Usage: draw_cohort.py LISTS_DIR OUT_JSON"""
import csv, hashlib, json, subprocess, sys, time
LD, OUT = sys.argv[1], sys.argv[2]
SEED = "ei-rules5-cohort-2026-10-07|"
DEV = {"INDOTECH", "SUPRIYA", "GOLDIAM", "SHAILY", "DEEPAKFERT", "ASALCBR", "PGIL", "REFEX", "GENUSPOWER", "HBLENGINE",
       "TARIL", "OLECTRA", "BORORENEW", "GMMPFAUDLR", "SWSOLAR", "WAAREEENER", "SHAKTIPUMP", "SMLMAH", "HBLPOWER", "SMLISUZU"}
members = {}
for f in ("ind_niftysmallcap250list.csv", "ind_niftymicrocap250_list.csv"):
    for r in csv.DictReader(open(f"{LD}/{f}", encoding="utf-8-sig")):
        members.setdefault(r["Symbol"].strip(), {"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(),
                                                 "industry": r["Industry"].strip(), "index": f})
log, picked, per_ind = [], [], {}
elig = []
for m in members.values():
    if m["industry"] == "Financial Services":
        log.append((m["symbol"], "excluded: financial services")); continue
    if m["symbol"] in DEV:
        log.append((m["symbol"], "excluded: development issuer")); continue
    elig.append(m)
elig.sort(key=lambda m: hashlib.sha256((SEED + m["symbol"]).encode()).hexdigest())
for m in elig:
    if len(picked) == 12:
        break
    if per_ind.get(m["industry"], 0) >= 2:
        log.append((m["symbol"], f"skipped: industry cap ({m['industry']})")); continue
    url = (f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={m['symbol']}"
           "&from_date=01-01-2021&to_date=30-06-2021")
    n = None
    for _ in range(3):
        r = subprocess.run(["curl", "-s", "-m", "30", "-A", "Mozilla/5.0", "-H", "Accept: application/json", url],
                           capture_output=True, text=True)
        try:
            n = len(json.loads(r.stdout)); break
        except Exception:
            time.sleep(3)
    time.sleep(1)
    if not n:
        log.append((m["symbol"], f"skipped: no announcement 2021-01-01..2021-06-30 ({'none' if n == 0 else 'unreadable'})")); continue
    per_ind[m["industry"]] = per_ind.get(m["industry"], 0) + 1
    picked.append({**m, "rank_hash": hashlib.sha256((SEED + m["symbol"]).encode()).hexdigest()[:12],
                   "h1_2021_announcements": n})
    log.append((m["symbol"], "selected"))
json.dump({"seed": SEED, "universe": len(members), "eligible": len(elig), "selected": picked,
           "log": log}, open(OUT, "w"), indent=1)
for p in picked: print(p["symbol"], "|", p["name"], "|", p["industry"], "|", p["h1_2021_announcements"])
print("universe", len(members), "eligible", len(elig), "skips", [l for l in log if l[1].startswith("skipped")])
