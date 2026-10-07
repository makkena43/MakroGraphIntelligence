"""Rules-5 cohort results vs the rules-6 development re-run on the same 12 issuers."""
import json, os, sys
SP = sys.argv[1]
ORDER = ["PRICOLLTD", "RAIN", "TATACHEM", "BATAINDIA", "CESC", "AURIONPRO", "HGINFRA", "GPPL", "AFFLE", "RBA",
         "LUMAXIND", "JYOTHYLAB"]
def load(d, s):
    p = os.path.join(SP, d, f"{s}_eval.json")
    return json.load(open(p)) if os.path.exists(p) else None
pc5, pc6 = json.load(open(f"{SP}/parse_cov_c5.json")), json.load(open(f"{SP}/parse_cov_r6c.json"))
tot = {5: {}, 6: {}}
rows, summary = [], {5: dict(alerts=0, false=0, infl=0, caught=0, caught_timing=0, datechg=0, vanished=0, cats=0),
                     6: dict(alerts=0, false=0, infl=0, caught=0, caught_timing=0, datechg=0, vanished=0, cats=0)}
for s in ORDER:
    out = {"symbol": s}
    for v, d, pc in ((5, "cohort5_out", pc5), (6, "cohort6_dev_out", pc6)):
        e = load(d, s)
        if not e:
            out[v] = None
            continue
        verd = {}
        for a in e["alerts"]:
            verd[a["verdict"]] = verd.get(a["verdict"], 0) + 1
            tot[v][a["verdict"]] = tot[v].get(a["verdict"], 0) + 1
        infl = e["inflections"]
        sm = summary[v]
        sm["alerts"] += len(e["alerts"]); sm["false"] += len(e["false_alerts"]); sm["cats"] += e["catalysts_total"]
        sm["infl"] += len(infl); sm["caught"] += sum(not m["missed"] for m in infl)
        sm["caught_timing"] += sum(bool(m.get("timing_only", m["alert_before"])) for m in infl)
        sm["datechg"] += len(e["prefix_defects"]); sm["vanished"] += len(e["vanished_catalysts"])
        out[v] = {"catalysts": e["catalysts_total"], "alerts": len(e["alerts"]), "verdicts": verd,
                  "false": len(e["false_alerts"]), "datechg": len(e["prefix_defects"]),
                  "vanished": len(e["vanished_catalysts"]),
                  "inflections": [{"from": m["from"], "to": m["to"], "pat": m["ttm_parent_pat"], "missed": m["missed"],
                                   "credited": m["alert_before"], "timing_only": m.get("timing_only")} for m in infl],
                  "coverage": {k: pc.get(s, {}).get(k) for k in ("scope", "revenue", "ebitda", "pat", "pat_attributable")},
                  "alert_list": [{k: a.get(k) for k in ("kind", "first_public", "supported_at", "verdict",
                                                       "contribution_when_flagged")} | {"change": a["change"][:110],
                                  "delivered": a["delivery"] and a["delivery"].get("delivered")} for a in e["alerts"]]}
    rows.append(out)
json.dump({"rows": rows, "summary": summary, "verdicts": tot}, open(f"{SP}/compare_r5_r6.json", "w"), indent=1)
for r in rows:
    print(r["symbol"])
    for v in (5, 6):
        x = r[v]
        if x: print(f"  r{v}: cov {x['coverage']} cat {x['catalysts']} alerts {x['alerts']} {x['verdicts']} false {x['false']} "
                    f"datechg {x['datechg']} vanished {x['vanished']} infl {[(i['from'], i['missed'], i['timing_only']) for i in x['inflections']]}")
    if r[6]:
        for a in r[6]["alert_list"]: print("     ALERT", a)
print("SUMMARY", json.dumps(summary)); print("VERDICTS", json.dumps(tot))
