"""Orders vs later results, from the *_timeline.json files.  Usage: python orders_vs_results.py DIR

For every company-quarter E (dated at the publication of E's results):
  X_orders = orders announced in the 12 months to that date / TTM revenue
  X_book   = latest order book stated in the 200 days to that date / TTM revenue
  Y_rev    = TTM revenue 4 quarters later / TTM revenue at E - 1
  Y_profit = TTM profit 4 quarters later / TTM profit at E - 1 (positive base only)
Bucketed, pooled over companies.  Only what was public at the date enters X."""
import glob, json, re, sys
NOT_ORDER = re.compile(r"\?|tender|pipeline|participat|\bbid|bidding|will come|market|opportunit|potential|expect|"
                       r"target|guidance|enquir|inquir|l1\b|lowest bidder", re.I)
TALK = re.compile(r"analyst|transcript|annual report|investor presentation|earnings call", re.I)
from datetime import date, timedelta
from statistics import median

rows = []
per = {}
for f in sorted(glob.glob(sys.argv[1] + "/*_timeline.json")):
    d = json.load(open(f))
    path = [p for p in d["ttm_path"] if p[1] is not None and p[3]]
    # order values only from order announcements / press releases, never from call talk; no tenders or bids
    orders = [(date.fromisoformat(o["date"]), o["crore"]) for o in d["orders"]
              if not TALK.search(o["type"]) and not NOT_ORDER.search(o["quote"])]
    books = [(date.fromisoformat(b["date"]), b["crore"]) for b in d["books"] if not NOT_ORDER.search(b["quote"])]
    idx = {p[0]: i for i, p in enumerate(path)}
    for i, (e, rev, prof, pub) in enumerate(path):
        t = date.fromisoformat(pub)
        if t < date(2021, 1, 1) or i + 4 >= len(path) or not rev:
            continue
        e4 = path[i + 4]
        if (date.fromisoformat(e4[0]) - date.fromisoformat(e)).days > 380:
            continue                                            # a gap in the series
        o12 = sum(v for dt, v in orders if t - timedelta(days=365) < dt <= t)
        bk = [v for dt, v in books if dt <= t and (t - dt).days <= 200]
        r = {"sym": d["symbol"], "quarter": e, "date": pub, "x_orders": o12 / rev,
             "x_book": (bk[-1] / rev) if bk else None,
             "y_rev": e4[1] / rev - 1,
             "y_profit": (e4[2] / prof - 1) if (prof and prof > 0 and e4[2] is not None) else None}
        rows.append(r)
        per.setdefault(d["symbol"], []).append(r)


def summarise(name, sel):
    yr = [r["y_rev"] for r in sel]
    yp = [r["y_profit"] for r in sel if r["y_profit"] is not None]
    hit = sum(1 for v in yp if v >= 0.25)
    print(f"  {name:28s} n={len(sel):3d}  revenue growth next 4Q: median {median(yr) * 100 if yr else 0:+5.0f}%   "
          f"profit growth: median {median(yp) * 100 if yp else 0:+5.0f}%   profit +25%: {hit}/{len(yp)}"
          f" ({hit / len(yp) * 100 if yp else 0:.0f}%)")


print(f"{len(rows)} company-quarters from {len(per)} companies")
print("Orders announced in the previous 12 months / TTM revenue:")
for lo, hi in ((0, 1e-9), (1e-9, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 99)):
    summarise(f"{lo:.2f}-{hi:.2f}" if hi < 99 else ">= 1.00", [r for r in rows if lo <= r["x_orders"] < hi]
              if lo else [r for r in rows if r["x_orders"] == 0])
print("Stated order book / TTM revenue:")
summarise("no book stated", [r for r in rows if r["x_book"] is None])
for lo, hi in ((0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 99)):
    summarise(f"{lo:.1f}-{hi:.1f}" if hi < 99 else ">= 3.0", [r for r in rows if r["x_book"] is not None and lo <= r["x_book"] < hi])
summarise("all company-quarters", rows)
print("\nPer company: strongest order quarter and what followed")
for sym, rs in per.items():
    best = max(rs, key=lambda r: r["x_orders"])
    if best["x_orders"] <= 0:
        continue
    yp = "n/a" if best["y_profit"] is None else f"{best['y_profit'] * 100:+.0f}%"
    print(f"  {sym:11s} {best['date']}  orders 12m = {best['x_orders']:.2f}x TTM revenue  -> revenue next 4Q "
          f"{best['y_rev'] * 100:+.0f}%, profit {yp}")
json.dump(rows, open(sys.argv[1] + "/orders_vs_results.json", "w"), indent=1)
