#!/usr/bin/env python3
"""
Arc-gate recalibration study — choose the arc limit on a measured
recall/return tradeoff instead of an assumed number.

WHY
---
The winner autopsy found the arc gate is the single largest rejector of
>=5x winners: 79 of 778 winner-instances (10.2%) are dropped because the
stock had already run past the limit since its signal first appeared. That
does NOT mean the gate is wrong — it exists to stop you buying a story the
market has already paid for, and the skill file sanctions it explicitly as
cycle-position input (distinct from the banned chart technicals).

What was never measured is its PRICE. Loosening the gate necessarily
recovers winners; the question is whether the names it lets back in are as
good as the ones already passing, or whether recall is bought by degrading
the median. This script measures both sides at once:

  for each candidate arc limit:
    (a) how many winners are recovered            <- recall side
    (b) forward returns of everything emitted     <- precision side

A limit is only worth raising if (b) holds up while (a) improves. Recall
bought with worse returns is not an improvement, and the aggregate median
is exactly the statistic that hid BIS-QCO's -20% inside a healthy-looking
T1 number earlier this year — so returns are reported per limit, not pooled.

POINT-IN-TIME
-------------
The screen at each anchor reads only data <= anchor; forward returns are
graded strictly after the fact. Returns reuse the autopsy's alias-aware,
right-censored, outlier-guarded calculator so a rename or a truncated
horizon cannot silently distort a limit's score.

USAGE
-----
    python scripts/research/arc_gate_study.py
    python scripts/research/arc_gate_study.py --limits 1.5,2.0,2.5,3.0
    python scripts/research/arc_gate_study.py --include-capabilities
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
sys.path.insert(0, HERE)

from extract_report_data import connect                     # noqa: E402
import moonshot_screen                                       # noqa: E402
from moonshot_screen import compute_moonshot_candidates, alias_map  # noqa: E402
from winner_autopsy import fwd_return, ANCHORS, HORIZON_DAYS, all_names  # noqa: E402

DEFAULT_LIMITS = [1.5, 2.0, 2.5, 3.0, 99.0]   # 99 = effectively no gate
HORIZONS = {"12m": 365, "24m": 730, "36m": 1095}


def med(xs):
    return sorted(xs)[len(xs) // 2] if xs else None


def run_limit(cur, aliases, names, winners, limit, top_n, include_caps):
    """Patch the screen's arc limits to `limit`, then measure both sides.

    The screen hardcodes 1.5 (since-signal), 2.0 (from-2y-low) and 2.5 (T6
    headroom) inline. Rather than thread three parameters through production
    code for a study, the constants are scaled together so their RELATIVE
    structure is preserved — the T6 allowance stays looser than the base
    limit, which is the design intent."""
    emitted, recovered = [], 0
    for astr in ANCHORS:
        anchor = date.fromisoformat(astr)
        diag = {}
        cands = compute_moonshot_candidates(
            cur, anchor, top_n=top_n, diagnostics=diag,
            include_capabilities=include_caps)
        got = {c["ticker"] for c in cands}
        for t, mult in winners.get(anchor, []):
            for alt in names.get(t, {t}):
                if alt in got:
                    recovered += 1
                    break
        for c in cands:
            emitted.append((anchor, c["ticker"]))
    return emitted, recovered


def main():
    ap = argparse.ArgumentParser(description="Arc-gate recall/return tradeoff")
    ap.add_argument("--limits", default=",".join(str(x) for x in DEFAULT_LIMITS))
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--include-capabilities", action="store_true")
    args = ap.parse_args()
    limits = [float(x) for x in args.limits.split(",")]

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    aliases = alias_map(cur)
    names = all_names(cur)

    cur.execute("""SELECT anchor_date, ticker, multiple FROM mg_winner_cohort
                   WHERE horizon_days=%s""", (HORIZON_DAYS,))
    winners = defaultdict(list)
    for r in cur.fetchall():
        winners[r["anchor_date"]].append((r["ticker"], float(r["multiple"])))
    total_winners = sum(len(v) for v in winners.values())
    if not total_winners:
        print("No winner cohort — run winner_autopsy.py --stage cohort first.")
        return

    base_sig, base_low, base_t6 = 1.5, 2.0, 2.5
    print(f"Winner cohort: {total_winners} instances across {len(winners)} anchors")
    print(f"Baseline arc limits: since-signal {base_sig}, from-2y-low "
          f"{base_low}, T6 headroom {base_t6}\n")

    results = []
    for lim in limits:
        scale = lim / base_sig
        moonshot_screen.ARC_LIMIT_SIGNAL = lim
        moonshot_screen.ARC_LIMIT_LOW = round(base_low * scale, 3)
        moonshot_screen.ARC_LIMIT_T6 = round(base_t6 * scale, 3)
        label = "no gate" if lim >= 99 else f"{lim}x"
        print(f"=== arc limit {label} "
              f"(low {moonshot_screen.ARC_LIMIT_LOW}, "
              f"T6 {moonshot_screen.ARC_LIMIT_T6}) ===", flush=True)

        emitted, recovered = run_limit(cur, aliases, names, winners, lim,
                                       args.top_n, args.include_capabilities)
        rets = {h: [] for h in HORIZONS}
        for anchor, ticker in emitted:
            for h, days in HORIZONS.items():
                r = fwd_return(cur, aliases, ticker, anchor, days)
                if r is not None:
                    rets[h].append(r)
        row = {"limit": label, "emitted": len(emitted), "recovered": recovered,
               "recall_pct": round(recovered / total_winners * 100, 1)}
        for h in HORIZONS:
            row[h] = med(rets[h])
            row[h + "_n"] = len(rets[h])
        results.append(row)
        print(f"  emitted={row['emitted']} winners_caught={recovered} "
              f"({row['recall_pct']}%) | "
              + " ".join(f"{h} median "
                         f"{row[h] if row[h] is not None else 'n/a'}% "
                         f"(n={row[h + '_n']})" for h in HORIZONS) + "\n",
              flush=True)

    print("=== TRADEOFF TABLE ===")
    hdr = f"{'arc limit':>10} {'emitted':>8} {'caught':>7} {'recall':>7}"
    for h in HORIZONS:
        hdr += f" {h + ' med':>10} {'n':>5}"
    print(hdr)
    for r in results:
        line = (f"{r['limit']:>10} {r['emitted']:>8} {r['recovered']:>7} "
                f"{r['recall_pct']:>6}%")
        for h in HORIZONS:
            v = r[h]
            line += f" {(f'{v}%' if v is not None else 'n/a'):>10} {r[h + '_n']:>5}"
        print(line)
    print("\nRead BOTH columns: a higher limit that lifts `caught` while the "
          "median columns hold is a real improvement; one that lifts `caught` "
          "while medians fall is buying recall with returns.")
    conn.close()


if __name__ == "__main__":
    main()
