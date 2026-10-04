#!/usr/bin/env python3
"""Portfolio construction: decision sizes → risk-budgeted weights (PMS-grade).

FULL/HALF/STARTER labels are conviction, not weights. This converts them into
portfolio weights under explicit risk limits:
  - size units: FULL=2.0, HALF=1.0, STARTER=0.5
  - single-stock cap (default 15%)
  - single-constraint-bucket cap (default 40%) — stocks sharing a constraint
    chain are ONE bet (the 2020-23 lesson: GVT&D+POWERINDIA+GENUSPOWER+APARINDS
    was a great research book but a single CRGO bet — unacceptable
    concentration for managed money)
  - cash floor (default 10%): capped-away weight goes to cash, never forced
    into weaker names

Constraint buckets come from the data JSON's candidate→products/themes mapping
(no hardcoded names). Writes `decision.portfolio_weights` + `portfolio_params`
back into the judgment JSON so the renderer and the decision log pick them up.

Usage: portfolio_construct.py <judgment.json> <data.json>
       [--stock-cap 15] [--bucket-cap 40] [--cash-floor 10]
"""
import argparse
import json
import re
from collections import defaultdict

SIZE_UNITS = {"FULL": 2.0, "HALF": 1.0, "STARTER": 0.5}


def size_units(size_label: str) -> float:
    base = (size_label or "").split(" ")[0].split("(")[0].strip().upper()
    return SIZE_UNITS.get(base, 0.5)


def constraint_bucket(tickers, data_doc):
    """Map each ticker to a constraint bucket (first constrained product /
    theme family it belongs to in this snapshot). Unmapped → own bucket."""
    tick_products = {}
    for c in data_doc.get("ranked_candidates") or []:
        prods = c.get("products") or c.get("themes") or []
        if prods:
            tick_products[c["ticker"].upper()] = prods[0]
    for chain, members in (data_doc.get("supply_side_beneficiaries") or {}).items():
        if isinstance(members, list):
            for m in members:
                t = (m.get("ticker") or "").upper()
                if t and t not in tick_products:
                    tick_products[t] = chain
    return {t: tick_products.get(t, f"unmapped:{t}") for t in tickers}


def construct(judgment, data_doc, stock_cap=15.0, bucket_cap=40.0, cash_floor=10.0):
    decision = judgment.get("decision") or {}
    buys = decision.get("buys") or []
    if not buys:
        return None

    # split baskets ("EXIDEIND + ARE&M") into legs sharing the position's units
    legs = []
    for b in buys:
        parts = [p.strip().upper() for p in re.split(r"\s*\+\s*", b["ticker"]) if p.strip()]
        u = size_units(b.get("size"))
        for p in parts:
            legs.append({"ticker": p, "units": u / len(parts), "position": b["ticker"]})

    buckets = constraint_bucket([l["ticker"] for l in legs], data_doc)
    investable = 100.0 - cash_floor
    total_units = sum(l["units"] for l in legs)
    for l in legs:
        l["raw_pct"] = investable * l["units"] / total_units

    # single-stock cap, excess to cash (never redistributed into weaker names)
    for l in legs:
        l["weight_pct"] = min(l["raw_pct"], stock_cap)

    # constraint-bucket cap: scale down every leg in an over-cap bucket
    by_bucket = defaultdict(list)
    for l in legs:
        by_bucket[buckets[l["ticker"]]].append(l)
    for bucket, blegs in by_bucket.items():
        tot = sum(l["weight_pct"] for l in blegs)
        if tot > bucket_cap:
            scale = bucket_cap / tot
            for l in blegs:
                l["weight_pct"] *= scale

    invested = sum(l["weight_pct"] for l in legs)
    weights = [{
        "ticker": l["ticker"], "position": l["position"],
        "constraint_bucket": buckets[l["ticker"]],
        "weight_pct": round(l["weight_pct"], 1),
    } for l in sorted(legs, key=lambda x: -x["weight_pct"])]

    return {
        "portfolio_weights": weights,
        "portfolio_params": {
            "size_units": SIZE_UNITS, "stock_cap_pct": stock_cap,
            "bucket_cap_pct": bucket_cap, "cash_floor_pct": cash_floor,
            "cash_pct": round(100.0 - invested, 1),
            "n_positions": len(buys),
            "n_constraint_buckets": len({b for b in buckets.values()
                                          if not b.startswith("unmapped:")}),
            "note": ("Weights are risk limits applied to conviction sizes; capped-away "
                     "weight goes to CASH, never forced into weaker names. Buckets group "
                     "same-constraint stocks as one bet."),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("judgment_json")
    ap.add_argument("data_json")
    ap.add_argument("--stock-cap", type=float, default=15.0)
    ap.add_argument("--bucket-cap", type=float, default=40.0)
    ap.add_argument("--cash-floor", type=float, default=10.0)
    args = ap.parse_args()

    with open(args.judgment_json) as f:
        judgment = json.load(f)
    with open(args.data_json) as f:
        data_doc = json.load(f)

    result = construct(judgment, data_doc, args.stock_cap, args.bucket_cap, args.cash_floor)
    if result is None:
        print("No buys in decision — nothing to construct.")
        return

    judgment.setdefault("decision", {})["portfolio_weights"] = result["portfolio_weights"]
    judgment["decision"]["portfolio_params"] = result["portfolio_params"]
    with open(args.judgment_json, "w") as f:
        json.dump(judgment, f, indent=1, ensure_ascii=False)

    p = result["portfolio_params"]
    print(f"{'Ticker':12} {'Weight':>7}  {'Bucket'}")
    for w in result["portfolio_weights"]:
        print(f"{w['ticker']:12} {w['weight_pct']:>6.1f}%  {w['constraint_bucket']}")
    print(f"{'CASH':12} {p['cash_pct']:>6.1f}%")
    print(f"\n{p['n_positions']} positions, {p['n_constraint_buckets']} constraint buckets. "
          f"Caps: stock {p['stock_cap_pct']}%, bucket {p['bucket_cap_pct']}%, "
          f"cash floor {p['cash_floor_pct']}%. Written to {args.judgment_json}")


if __name__ == "__main__":
    main()
