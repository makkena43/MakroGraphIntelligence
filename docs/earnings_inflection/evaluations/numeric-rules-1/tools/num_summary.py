import json, glob, sys
from statistics import median
D = sys.argv[1]
tot = dict(signals=0, episodes=0, delivered=0, not_delivered=0, open=0, nm=0, base_n=0, base_hit=0, infl=0, caught=0,
           defects=0, vanished=0, cat_alerts=0, cat_false=0)
leads, onq = [], 0
rows = []
for f in sorted(glob.glob(D + "/*_eval.json")):
    e = json.load(open(f)); n = e["numeric"]
    eps = n["episodes"]
    c = {k: sum(1 for x in eps if x["outcome"] == k) for k in ("delivered", "not_delivered", "open", "not_measurable")}
    inf = e["inflections"]
    cau = [m for m in inf if m.get("numeric_caught")]
    for m in cau:
        leads.append(m["numeric_lead_days"])
    tot["signals"] += len(n["signals"]); tot["episodes"] += len(eps)
    tot["delivered"] += c["delivered"]; tot["not_delivered"] += c["not_delivered"]; tot["open"] += c["open"]
    tot["nm"] += c["not_measurable"]
    tot["base_n"] += n["base_rate"]["quarters"]; tot["base_hit"] += n["base_rate"]["followed_by_25pct"]
    tot["infl"] += len(inf); tot["caught"] += len(cau)
    tot["defects"] += len(n["prefix_defects"]); tot["vanished"] += len(n["vanished"])
    tot["cat_alerts"] += len(e["alerts"]); tot["cat_false"] += len(e["false_alerts"])
    rows.append((e["symbol"], e.get("series_scope"), e["coverage"]["periods_parsed"], len(n["signals"]), len(eps),
                 c["delivered"], c["not_delivered"], c["open"], c["not_measurable"], len(inf), len(cau),
                 [m.get("numeric_lead_days") for m in cau], n["base_rate"]["followed_by_25pct"], n["base_rate"]["quarters"],
                 len(e["alerts"]), len(e["false_alerts"])))
print("sym scope periods signals episodes deliv notdeliv open nm | infl caught leads | base | cat_alerts false")
for r in rows: print(*r)
judged = tot["delivered"] + tot["not_delivered"]
print(json.dumps(tot))
print("precision %.2f (%d/%d)  base rate %.2f (%d/%d)  caught %d/%d  median lead %s  defects %d vanished %d" % (
    tot["delivered"] / judged if judged else 0, tot["delivered"], judged, tot["base_hit"] / tot["base_n"] if tot["base_n"] else 0,
    tot["base_hit"], tot["base_n"], tot["caught"], tot["infl"], median(leads) if leads else None, tot["defects"], tot["vanished"]))
