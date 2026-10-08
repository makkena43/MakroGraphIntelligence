"""Pre-registered rules-3 cohort replay for one issuer: quarterly snapshots, catalyst timeline, prefix
invariance of dates on real data, earnings delivery per alert, missed inflections, workload.
Usage: python cohort_eval.py SYMBOL FX_ROOT OUT_DIR"""
import json, os, sys, time
from datetime import date

sys.path.insert(0, "src")
from makrograph.earnings_inflection.catalysts import DEFAULT_CATALYST_THRESHOLDS, RULES_VERSION
from makrograph.earnings_inflection.contracts import Metric
from makrograph.earnings_inflection.evaluation import catalyst_timeline, inflection_credit, rules_fingerprint
from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
from makrograph.earnings_inflection.replay import snapshot
from makrograph.earnings_inflection.source_repository import FixtureRepository

SYM, FXR, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(OUT, exist_ok=True)
FLAG = ("supported_prospective_inflection", "execution_validating", "confirmed_for_investment_review")
dates = [date(y, m, d) for y in range(2021, 2026) for m, d in ((2, 28), (5, 31), (8, 31), (11, 30))
         if date(2021, 5, 31) <= date(y, m, d) <= date(2025, 11, 30)] + [date(2025, 12, 31)]
pipe = EarningsInflectionPipeline({}, FixtureRepository(os.path.join(FXR, SYM)))
snaps, t0 = [], time.time()
for d in dates:
    try:
        res = pipe.run([SYM], d.isoformat())
        snaps.append(snapshot(res.assessments[0]) if res.assessments else
                     {"ticker": SYM, "as_of": d.isoformat(), "error": res.errors.get(SYM, "no assessment")})
    except Exception as e:                       # recorded, never hidden
        snaps.append({"ticker": SYM, "as_of": d.isoformat(), "error": f"{type(e).__name__}: {e}"[:300]})
    print(SYM, d, f"{time.time() - t0:.0f}s", snaps[-1].get("error", ""), flush=True)
json.dump(snaps, open(os.path.join(OUT, f"{SYM}_snapshots.json"), "w"), indent=1, default=str)
series = pipe.last_series                        # as known at the cut-off (last run = 2025-12-31)
tl = catalyst_timeline(snaps)

# prefix invariance on real data: dates of a catalyst never change in later snapshots
defects = []
seen: dict = {}
for s in sorted((x for x in snaps if "error" not in x), key=lambda x: x["as_of"]):
    for c in s["catalysts"]:
        key = c["catalyst_id"]
        cur = {k: c.get(k) for k in ("first_public_at", "materiality_supported_at", "execution_supported_at",
                                     "supported_at", "validating_at", "confirmed_at", "initial_stage")}
        if key in seen:
            for k, v in seen[key][1].items():
                if v is not None and cur.get(k) != v:
                    defects.append(f"{key} {k}: {v} (as of {seen[key][0]}) -> {cur.get(k)} (as of {s['as_of']})")
            for k, v in cur.items():
                if v is not None and seen[key][1].get(k) is None:
                    seen[key][1][k] = v
        else:
            seen[key] = (s["as_of"], cur)
# catalysts that disappear from later snapshots
last = {c["catalyst_id"] for c in next((x for x in reversed(snaps) if "error" not in x), {"catalysts": []})["catalysts"]}
vanished = sorted(set(seen) - last)

p = series.cadence(Metric.REVENUE) if series else None
ends = sorted({e for (m, pt, e) in series.points if m == Metric.REVENUE and pt == p}) if p else []
standalone = getattr(series.scope, "value", "") == "standalone" if series else False


def ttm(metric, end):
    v = series.ttm(metric, end, p)[0] if (series and end) else None
    return v


def parent(end):
    # numeric-rules-1 pre-registration: owners' share for a consolidated series when available, else PAT
    pa = ttm(Metric.PAT_ATTRIBUTABLE, end) if not standalone else None
    return pa if pa is not None else ttm(Metric.PAT, end)


def published(end):
    pt = series.get(Metric.REVENUE, end, p)
    return pt and (pt.first_public_at or pt.available_at)


alerts = []
for r in tl["catalysts"]:
    if not r["ever_supported"]:
        continue
    sa = r.get("supported_at") or r["stage_first_as_of"].get("supported_prospective_inflection") or min(
        r["stage_first_as_of"].get(st, date.max) for st in FLAG)
    before = [e for e in ends if published(e) and published(e).date() <= sa]
    deliv = None
    if before and len(ends) > ends.index(before[-1]) + 4:
        e0 = before[-1]
        e1 = ends[ends.index(e0) + 4]
        r0, r1, p0, p1 = ttm(Metric.REVENUE, e0), ttm(Metric.REVENUE, e1), parent(e0), parent(e1)
        deliv = {"from": str(e0), "to": str(e1), "ttm_revenue": [r0, r1], "ttm_parent_pat": [p0, p1],
                 "delivered": bool(r0 and r1 and r1 > r0 and p0 is not None and p1 is not None and p0 > 0
                                   and p1 >= 1.15 * p0),
                 "measurable": None not in (r0, r1, p0, p1) and (p0 or 0) > 0}
    flagged_q = sum(1 for s in snaps if "error" not in s and any(
        c["catalyst_id"] == r["catalyst_id"] and c["stage"] in FLAG for c in s["catalysts"]))
    alerts.append({"catalyst_id": r["catalyst_id"], "kind": r["kind"], "change": r["change"][:160],
                   "first_public": str(r["first_public"]), "supported_at": str(sa),
                   "verdict": r["verdict"], "contribution_when_flagged": r.get("contribution_when_flagged"), "final_stage": r["final_stage"], "stages": [(str(d), s) for d, s in r["stages"]],
                   "flagged_snapshots": flagged_q, "delivery": deliv})

# mechanical inflections: TTM parent PAT +50% over 4 periods from a positive base
inflections = []
for i in range(len(ends) - 4):
    a, b = parent(ends[i]), parent(ends[i + 4])
    if a and b and a > 0 and b >= 1.5 * a:
        inflections.append({"from": str(ends[i]), "to": str(ends[i + 4]), "ttm_parent_pat": [a, b],
                            "end_published": str(published(ends[i + 4]).date()) if published(ends[i + 4]) else None})
merged = []
for w in inflections:                              # overlapping windows = one inflection episode
    if merged and w["from"] <= merged[-1]["to"]:
        merged[-1]["to"], merged[-1]["end_published"] = w["to"], w["end_published"]
        merged[-1]["ttm_parent_pat"][1] = w["ttm_parent_pat"][1]
    else:
        merged.append(dict(w, ttm_parent_pat=list(w["ttm_parent_pat"])))
for m in merged:
    # an alert counts if supported at least one reporting period before the window's end was published
    m.update(inflection_credit(m, alerts))         # rules-6 D13: substance, not timing alone

# ---- numbers-first signals (numeric-rules-1 pre-registration) ----
from makrograph.earnings_inflection.numeric_signals import NUMERIC_RULES_VERSION, DEFAULT_NUMERIC_THRESHOLDS  # noqa: E402
ok_snaps = sorted((x for x in snaps if "error" not in x), key=lambda x: x["as_of"])
nsig = {}
num_defects = []
for s_ in ok_snaps:
    for n in s_.get("numeric_signals", []):
        if n["signal_id"] in nsig and nsig[n["signal_id"]]["known_at"] != n["known_at"]:
            num_defects.append(f"{n['signal_id']}: {nsig[n['signal_id']]['known_at']} -> {n['known_at']} (as of {s_['as_of']})")
        nsig.setdefault(n["signal_id"], n)
last_ids = {n["signal_id"] for n in (ok_snaps[-1].get("numeric_signals", []) if ok_snaps else [])}
num_vanished = sorted(set(nsig) - last_ids)
sigs = sorted(nsig.values(), key=lambda n: (n["period_end"], n["kind"]))
qe = sorted({n["period_end"] for n in sigs})
# signal episodes: quarters with a signal, one quarter's gap allowed
episodes = []
for e in qe:
    d = date.fromisoformat(e)
    if episodes and (d - date.fromisoformat(episodes[-1]["quarters"][-1])).days <= 200:
        episodes[-1]["quarters"].append(e)
    else:
        episodes.append({"quarters": [e]})
for ep in episodes:
    first_q = ep["quarters"][0]
    first = [n for n in sigs if n["period_end"] == first_q]
    ep["first_known_at"] = min(n["known_at"] for n in first)[:10]
    ep["kinds"] = sorted({n["kind"] for n in sigs if n["period_end"] in ep["quarters"]})
    e0 = date.fromisoformat(first_q)
    base = parent(e0) if e0 in ends else None
    later = [ends[i] for i in range(len(ends)) if ends[i] > e0][:4]
    vals = [parent(x) for x in later]
    if base is None:
        ep["outcome"] = "not_measurable"
    elif any(v is not None and (v >= 1.25 * base if base > 0 else v > 0) for v in vals):
        ep["outcome"] = "delivered"
    elif len(later) < 4:
        ep["outcome"] = "open"
    else:
        ep["outcome"] = "not_delivered"
    ep["ttm_profit"] = [base] + vals
# base rate: every quarter with a TTM profit and four later quarters
base_n = base_hit = 0
for i, e in enumerate(ends):
    b = parent(e)
    later = ends[i + 1:i + 5]
    if b is None or len(later) < 4:
        continue
    vals = [parent(x) for x in later]
    base_n += 1
    base_hit += any(v is not None and (v >= 1.25 * b if b > 0 else v > 0) for v in vals)
for m in merged:
    start, endp = m["from"], m["end_published"]
    hits = [n for n in sigs if endp and start <= n["period_end"] and n["known_at"][:10] <= endp]
    m["numeric_caught"] = bool(hits)
    if hits:
        first = min(n["known_at"][:10] for n in hits)
        m["numeric_first_signal"] = first
        m["numeric_lead_days"] = (date.fromisoformat(endp) - date.fromisoformat(first)).days
        m["numeric_signal_kinds"] = sorted({n["kind"] for n in hits})
numeric = {"rules_version": NUMERIC_RULES_VERSION, "thresholds": DEFAULT_NUMERIC_THRESHOLDS,
           "signals": sigs, "episodes": episodes, "prefix_defects": num_defects, "vanished": num_vanished,
           "base_rate": {"quarters": base_n, "followed_by_25pct": base_hit},
           "signals_per_year": round(len(sigs) / 4.75, 2), "episodes_per_year": round(len(episodes) / 4.75, 2)}
cov = json.load(open(os.path.join(FXR, SYM, "coverage.json")))
errors = [s for s in snaps if "error" in s]
years = 4.75
out = {"symbol": SYM, "rules_version": tl["rules_version"],
       "fingerprint": rules_fingerprint(DEFAULT_CATALYST_THRESHOLDS, RULES_VERSION),
       "coverage": {**cov, "cadence": p, "scope": getattr(series.scope, "value", None) if series else None,
                    "periods_parsed": len(ends), "first_period": str(ends[0]) if ends else None,
                    "last_period": str(ends[-1]) if ends else None,
                    "credit_rating_filings": sum(1 for d in json.load(open(os.path.join(FXR, SYM, "export.json")))[
                        "documents"] if "credit rating" in (d.get("filing_type") or "").lower()),
                    "snapshot_errors": [(s["as_of"], s["error"]) for s in errors]},
       "catalysts_total": len(tl["catalysts"]),
       "catalysts_by_kind": {k: sum(1 for r in tl["catalysts"] if r["kind"] == k)
                             for k in sorted({r["kind"] for r in tl["catalysts"]})},
       "alerts": alerts,
       "false_alerts": [a["catalyst_id"] for a in alerts if a["verdict"] in ("contradicted", "delayed")],
       "unresolved_alerts": [a["catalyst_id"] for a in alerts if a["verdict"] == "open"],
       "prefix_defects": defects, "vanished_catalysts": vanished,
       "inflections": merged,
       "numeric": numeric,
       "series_scope": getattr(series.scope, "value", None) if series else None,
       "xbrl": {k: (ok_snaps[-1] if ok_snaps else {}).get(k) for k in ()},
       "workload": {"alerts_per_year": round(len(alerts) / years, 2),
                    "snapshots_in_flag_lane": sum(1 for s in snaps if "error" not in s and any(
                        c["stage"] in FLAG for c in s["catalysts"])),
                    "snapshots": len(snaps), "documents": cov.get("with_text")},
       "timeline": {k: v for k, v in tl.items() if k != "catalysts"}}
json.dump(out, open(os.path.join(OUT, f"{SYM}_eval.json"), "w"), indent=1, default=str)
print(SYM, "EVAL", json.dumps({k: out[k] for k in ("catalysts_total", "false_alerts", "unresolved_alerts",
                                                   "prefix_defects", "vanished_catalysts")}, default=str)[:800])
