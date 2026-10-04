#!/usr/bin/env python3
"""
Systematic winner autopsy — measure the screen's RECALL and discover new
fingerprint candidates from the full winner population instead of anecdotes.

WHY THIS EXISTS
---------------
The moonshot fingerprint taxonomy (T1-T6) was reverse-engineered from three
winners: KERNEX, GRAVITA, HBLPOWER. That is selection on the dependent
variable with n=3, and it produced at least one rule that did not generalize
-- T3-alone, derived from the single GRAVITA example, later measured at
-11% median (12m) and -80% (36m) across its real population and was reverted.

This script inverts the procedure. Instead of asking "what do our three
winners have in common", it asks:
  (a) of EVERY stock that actually exploded, how many did the screen even
      look at, and where exactly did the rest fall out?  (RECALL)
  (b) for the ones it looked straight at and dismissed, what was
      disproportionately present in their filings vs a matched control
      group?  (FINGERPRINT DISCOVERY, from hundreds not three)

Anything (b) turns up is a CANDIDATE only. It enters the same
`mg_tracked_schemes.t1_alone_eligible = false` provisional path as every
other unproven signal and must show a multi-quarter population edge before
it can qualify a name alone. No promotion on anecdote -- that is the whole
lesson this script encodes.

POINT-IN-TIME / DATA INTEGRITY
------------------------------
- Screen replay at each anchor reads only data <= anchor (the screen already
  enforces this); forward returns are graded strictly after the fact.
- `series IN ('EQ','BE')` is MANDATORY, never optional. BRITANNIA carries
  N2/N3 bonus-debenture rows at unrelated prices and has ZERO EQ rows in
  2020-2023; an unfiltered query mixes debenture and equity prices into one
  series. That is the source of the "BRITANNIA 198x" artifact recorded in
  SKILL.md -- it was never a multibagger, it was two instruments in one
  column.
- Returns are chained as prod(close/prev_close), NOT exit/entry. NSE adjusts
  `prev_close` on ex-dates, so this is immune to splits and bonuses; a naive
  exit/entry across a 1:1 bonus understates the move by half.
- Names with any single-day factor outside [0.5, 2.0] are flagged
  `ca_suspect` and excluded by default (--include-suspect to keep them):
  beyond NSE circuit limits, so almost always an unadjusted corporate action
  or a bad row rather than a real move.
- Coverage floor (--min-days) drops sparse series, which otherwise produce
  meaningless multiples from a handful of prints.

USAGE
-----
    python scripts/research/winner_autopsy.py --stage cohort
    python scripts/research/winner_autopsy.py --stage recall
    python scripts/research/winner_autopsy.py --stage mine
    python scripts/research/winner_autopsy.py --stage all
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))

from extract_report_data import connect            # noqa: E402
from moonshot_screen import compute_moonshot_candidates, alias_map  # noqa: E402

# Anchors: quarterly, each needing a full 3-year forward window inside the
# price data (which ends 2026-07-03), so the last usable anchor is mid-2023.
ANCHORS = [
    "2020-12-31", "2021-03-31", "2021-06-30", "2021-09-30", "2021-12-31",
    "2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31", "2023-03-31",
    "2023-06-30",
]
HORIZON_DAYS = 1095          # 3 years
DEFAULT_THRESHOLD = 5.0      # "winner" = >= 5x over the horizon
MIN_DAYS_FWD = 600           # of ~745 trading days in 3y -- drops sparse series
# NO LIQUIDITY FLOOR (user policy, Jul-2026): measuring recall against a
# liquidity-filtered denominator would quietly answer a different question —
# "how many winners could a fund of some assumed size have bought" — when the
# question asked is "how many winners existed". The capacity curve in
# scripts/research/size_gate_study.py reports the deployability angle
# separately, where it belongs, instead of baking it into the cohort.
# Set >0 only to reproduce a size-constrained view deliberately.
MIN_MED_VAL = 0.0            # median daily traded value floor (lakh) at anchor
CA_LO, CA_HI = 0.5, 2.0      # single-day factor bounds; outside = CA suspect

# Where the screen dropped a name -> which KIND of problem it represents.
# The three classes have completely different fixes, which is the whole point
# of measuring them separately.
STAGE_CLASS = {
    "not_in_universe":          "coverage",     # never mapped / never cited a scheme
    "no_price_history":         "coverage",
    "too_large":                "calibration",
    "financial_sector":         "calibration",
    "manual_exclusion":         "calibration",
    "disclosure_silence":       "calibration",
    "no_arc_base":              "calibration",
    "arc_gate":                 "calibration",
    "insufficient_fingerprints": "fingerprint_blind",  # looked at it, saw nothing
    "truncated":                "ranking",
    "emitted":                  "caught",
}


TRUNC_BUFFER = 45      # days a horizon may fall short before it is discarded


def fwd_return(cur, aliases, ticker, as_of, horizon_days):
    """Forward return %, graded strictly after the fact.

    Three guards that materially change results if omitted:
      - ALIAS-AWARE: a rename mid-horizon otherwise silently truncates the
        series (the HBLPOWER->HBLENGINE class of error).
      - RIGHT-CENSORED: if the nearest available exit price falls more than
        TRUNC_BUFFER days short of the target, return None rather than a
        quietly shortened horizon.
      - OUTLIER-GUARDED: values outside (-95%, +2000%) are dropped as probable
        data errors rather than allowed to dominate a mean.
    series is restricted to EQ/BE — mixing in debenture series is what
    produced the fictitious "BRITANNIA 198x".
    """
    target = as_of + timedelta(days=horizon_days)
    syms = [ticker] + list(aliases.get(ticker, []))
    cur.execute("""
        WITH entry AS (SELECT close FROM nse_bhavcopy_data
                       WHERE symbol=ANY(%s) AND series IN ('EQ','BE')
                         AND trade_date BETWEEN %s AND %s
                       ORDER BY trade_date LIMIT 1),
             exitp AS (SELECT close, trade_date FROM nse_bhavcopy_data
                       WHERE symbol=ANY(%s) AND series IN ('EQ','BE')
                         AND trade_date <= %s
                       ORDER BY trade_date DESC LIMIT 1)
        SELECT entry.close AS e, exitp.close AS x, exitp.trade_date AS xd
        FROM entry, exitp
    """, (syms, as_of, as_of + timedelta(days=10), syms, target))
    row = cur.fetchone()
    if not row or row["e"] is None or row["x"] is None:
        return None
    if (target - row["xd"]).days > TRUNC_BUFFER:
        return None
    e, x = float(row["e"]), float(row["x"])
    if e <= 0:
        return None
    ret = (x - e) / e * 100
    return round(ret, 1) if -95 < ret < 2000 else None


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mg_winner_cohort (
                id SERIAL PRIMARY KEY,
                anchor_date DATE NOT NULL,
                ticker TEXT NOT NULL,
                horizon_days INT NOT NULL,
                multiple NUMERIC,
                n_days INT,
                med_val_lakh NUMERIC,
                min_day_factor NUMERIC,
                max_day_factor NUMERIC,
                ca_suspect BOOLEAN DEFAULT false,
                computed_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (anchor_date, ticker, horizon_days)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mg_winner_recall (
                id SERIAL PRIMARY KEY,
                anchor_date DATE NOT NULL,
                ticker TEXT NOT NULL,
                multiple NUMERIC,
                stage TEXT,
                stage_class TEXT,
                detail TEXT,
                computed_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (anchor_date, ticker)
            )
        """)
    conn.commit()


def canonical_map(cur):
    """symbol -> CURRENT ticker, following rename chains to their terminal
    symbol (A->B->C maps A and B to C).

    Direction matters and is easy to get backwards: `alias_map` in the screen
    is deliberately SYMMETRIC (it unions old+new for price lookups, where
    either label is fine). Here it is not fine -- mg_documents and
    mg_india_beneficiaries carry the CURRENT symbol, so a cohort keyed on the
    old one silently fails every join and reports the company as invisible to
    the pipeline. That bug alone inflated `not_in_universe` with GET&D
    (->GVT&D), TRIL (->TARIL) and LSIL (->LLOYDSENGG) -- three of the largest
    multiples in the whole cohort.

    Uses the broader non-rejected rename set rather than the screen's
    high-confidence-only view, because the question here is "is this the same
    COMPANY", not "may the screen safely union these price series"."""
    cur.execute("""SELECT old_symbol, new_symbol FROM mg_symbol_renames
                   WHERE status <> 'rejected'""")
    nxt = {r["old_symbol"]: r["new_symbol"] for r in cur.fetchall()}
    sym2canon = {}
    for old in nxt:
        seen, cur_sym = {old}, old
        while cur_sym in nxt and nxt[cur_sym] not in seen:
            cur_sym = nxt[cur_sym]
            seen.add(cur_sym)          # chain-follow with cycle guard
        sym2canon[old] = cur_sym
    return sym2canon


def all_names(cur):
    """ticker -> every symbol that company has traded/filed under, so a recall
    lookup can match the screen's universe whichever label it used."""
    cur.execute("""SELECT old_symbol, new_symbol FROM mg_symbol_renames
                   WHERE status <> 'rejected'""")
    groups = {}
    for r in cur.fetchall():
        o, n = r["old_symbol"], r["new_symbol"]
        g = groups.setdefault(o, {o}) | groups.setdefault(n, {n}) | {o, n}
        for s in g:
            groups[s] = g
    return groups


def build_cohort(conn, cur, threshold, include_suspect=False):
    sym2canon = canonical_map(cur)
    total_rows = 0

    for astr in ANCHORS:
        anchor = date.fromisoformat(astr)
        end = anchor + timedelta(days=HORIZON_DAYS)

        # liquidity at the anchor (point-in-time): trailing-12m median traded
        # value, so we never "discover" a winner that was untradeable
        cur.execute("""
            SELECT symbol,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tottrdval) AS med_val
            FROM nse_bhavcopy_data
            WHERE series IN ('EQ','BE') AND trade_date BETWEEN %s AND %s
            GROUP BY symbol HAVING COUNT(*) >= 100
        """, (anchor - timedelta(days=365), anchor))
        liq = {}
        for r in cur.fetchall():
            c = sym2canon.get(r["symbol"], r["symbol"])
            liq[c] = max(liq.get(c, 0.0), float(r["med_val"] or 0))

        # forward multiple = prod(close/prev_close) = exp(sum(ln(...))).
        # prev_close is NSE's corporate-action-adjusted previous close, so
        # this chain is split/bonus-immune by construction.
        cur.execute("""
            SELECT symbol,
                   SUM(LN(close / prev_close))            AS log_mult,
                   COUNT(*)                               AS n_days,
                   MIN(close / prev_close)                AS min_f,
                   MAX(close / prev_close)                AS max_f
            FROM nse_bhavcopy_data
            WHERE series IN ('EQ','BE')
              AND trade_date > %s AND trade_date <= %s
              AND prev_close > 0 AND close > 0
            GROUP BY symbol
        """, (anchor, end))

        agg = {}
        for r in cur.fetchall():
            c = sym2canon.get(r["symbol"], r["symbol"])
            a = agg.setdefault(c, {"log": 0.0, "n": 0, "lo": 9e9, "hi": 0.0})
            a["log"] += float(r["log_mult"] or 0)
            a["n"] += int(r["n_days"] or 0)
            a["lo"] = min(a["lo"], float(r["min_f"] or 9e9))
            a["hi"] = max(a["hi"], float(r["max_f"] or 0))

        rows = []
        for t, a in agg.items():
            if a["n"] < MIN_DAYS_FWD:
                continue
            med = liq.get(t)
            if med is None or med < MIN_MED_VAL:
                continue
            try:
                mult = pow(2.718281828459045, a["log"])
            except OverflowError:
                continue
            if mult < threshold:
                continue
            suspect = a["lo"] < CA_LO or a["hi"] > CA_HI
            if suspect and not include_suspect:
                continue
            rows.append((anchor, t, HORIZON_DAYS, round(mult, 3), a["n"],
                         round(med, 1), round(a["lo"], 4), round(a["hi"], 4), suspect))

        with conn.cursor() as w:
            for row in rows:
                w.execute("""
                    INSERT INTO mg_winner_cohort
                      (anchor_date, ticker, horizon_days, multiple, n_days,
                       med_val_lakh, min_day_factor, max_day_factor, ca_suspect)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (anchor_date, ticker, horizon_days) DO UPDATE
                      SET multiple = EXCLUDED.multiple, n_days = EXCLUDED.n_days,
                          med_val_lakh = EXCLUDED.med_val_lakh,
                          ca_suspect = EXCLUDED.ca_suspect, computed_at = now()
                """, row)
        conn.commit()
        total_rows += len(rows)
        top = sorted(rows, key=lambda x: -x[3])[:5]
        print(f"  {astr}: {len(rows):3d} winners >={threshold}x  "
              f"top={[(r[1], r[3]) for r in top]}")

    print(f"\nCohort built: {total_rows} (anchor, ticker) winner-instances "
          f"at >={threshold}x / {HORIZON_DAYS}d")


def measure_recall(conn, cur, top_n=40, include_capabilities=False, store=True):
    cur.execute("""SELECT anchor_date, ticker, multiple FROM mg_winner_cohort
                   WHERE horizon_days=%s ORDER BY anchor_date, multiple DESC""",
                (HORIZON_DAYS,))
    winners = defaultdict(list)
    for r in cur.fetchall():
        winners[r["anchor_date"]].append((r["ticker"], float(r["multiple"])))
    if not winners:
        print("No cohort rows — run --stage cohort first.")
        return

    overall = Counter()
    overall_cls = Counter()
    per_anchor = {}
    names = all_names(cur)

    for anchor in sorted(winners):
        diag = {}
        cands = compute_moonshot_candidates(
            cur, anchor, top_n=top_n, diagnostics=diag,
            include_capabilities=include_capabilities)
        print(f"    (screen universe at {anchor}: {len(diag)} tickers, "
              f"emitted {len(cands)})")
        cnt = Counter()
        rows = []
        for t, mult in winners[anchor]:
            # match under EVERY name the company has traded/filed under —
            # a rename must never be scored as "the pipeline never saw it"
            d = None
            for alt in names.get(t, {t}):
                if alt in diag:
                    d = diag[alt]
                    break
            stage = d["stage"] if d else "not_in_universe"
            cls = STAGE_CLASS.get(stage, "other")
            detail = ""
            if d:
                detail = ", ".join(f"{k}={v}" for k, v in d.items() if k != "stage")
            cnt[stage] += 1
            overall[stage] += 1
            overall_cls[cls] += 1
            rows.append((anchor, t, mult, stage, cls, detail[:400]))
        per_anchor[anchor] = (len(winners[anchor]), cnt)
        if not store:
            print(f"  {anchor}: {len(winners[anchor]):3d} winners | " +
                  " ".join(f"{k}={v}" for k, v in cnt.most_common()))
            continue
        with conn.cursor() as w:
            for row in rows:
                w.execute("""
                    INSERT INTO mg_winner_recall
                      (anchor_date, ticker, multiple, stage, stage_class, detail)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (anchor_date, ticker) DO UPDATE
                      SET multiple=EXCLUDED.multiple, stage=EXCLUDED.stage,
                          stage_class=EXCLUDED.stage_class, detail=EXCLUDED.detail,
                          computed_at=now()
                """, row)
        conn.commit()
        print(f"  {anchor}: {len(winners[anchor]):3d} winners | " +
              " ".join(f"{k}={v}" for k, v in cnt.most_common()))

    total = sum(overall.values())
    print(f"\n=== RECALL over {total} winner-instances ===")
    for stage, n in overall.most_common():
        print(f"  {stage:26} {n:4d}  ({n/total*100:5.1f}%)  [{STAGE_CLASS.get(stage,'other')}]")
    print("\n=== by problem class (each has a DIFFERENT fix) ===")
    for cls, n in overall_cls.most_common():
        print(f"  {cls:20} {n:4d}  ({n/total*100:5.1f}%)")
    caught = overall_cls.get("caught", 0)
    print(f"\nSCREEN RECALL (emitted / all winners): {caught}/{total} = "
          f"{caught/total*100:.1f}%")


# --- stage C: differential mining -------------------------------------------
GENERIC = set("""
THE AND FOR WITH THAT THIS FROM HAVE BEEN WILL SHALL WERE WHICH THEIR THERE
LIMITED LIMITE COMPANY COMPANIES BOARD DIRECTOR DIRECTORS MEETING REPORT
ANNUAL QUARTER QUARTERLY RESULTS FINANCIAL YEAR MARCH JUNE SEPTEMBER DECEMBER
EXCHANGE STOCK SECURITIES REGULATION REGULATIONS SEBI NSE BSE INDIA INDIAN
PURSUANT REGULATION DISCLOSURE INTIMATION SUBMISSION PLEASE FIND ATTACHED
DEAR MADAM SCRIP CODE SYMBOL ISIN THANKING YOURS FAITHFULLY SINCERELY
SECRETARY COMPLIANCE OFFICER MANAGING CHIEF EXECUTIVE OFFICER CFO
LISTING OBLIGATIONS REQUIREMENTS ACT SECTION RULE RULES NOTICE
TOTAL AMOUNT VALUE LAKH LAKHS CRORE CRORES RUPEES SHARE SHARES EQUITY
PRIVATE PUBLIC GENERAL SPECIAL ORDINARY RESOLUTION APPROVED APPROVAL
STATEMENT STATEMENTS AUDITED UNAUDITED STANDALONE CONSOLIDATED
INFORMATION DETAILS RESPECT REGARD ABOVE BELOW FOLLOWING ENCLOSED
""".split())


def mine_signals(cur, max_docs_per_ticker=6, top_terms=40):
    """Differential term frequency: what shows up in winners' filings at the
    anchor that does NOT show up in a liquidity-matched control group.

    Deliberately blunt (capitalized-token frequency, document-level counting)
    — the output is a RANKED HYPOTHESIS LIST for human review, exactly like
    novel_policy_vocabulary, not an automated promotion path."""
    cur.execute("""SELECT anchor_date, ticker, multiple, stage_class
                   FROM mg_winner_recall
                   WHERE stage_class IN ('fingerprint_blind','coverage')""")
    missed = [(r["anchor_date"], r["ticker"], float(r["multiple"])) for r in cur.fetchall()]
    if not missed:
        print("No missed winners recorded — run --stage recall first.")
        return
    print(f"Mining {len(missed)} missed winner-instances "
          f"(fingerprint_blind + coverage)")

    by_anchor = defaultdict(list)
    for a, t, m in missed:
        by_anchor[a].append(t)

    win_df, ctl_df = Counter(), Counter()
    n_win = n_ctl = 0

    for anchor, tickers in sorted(by_anchor.items()):
        # liquidity-matched controls: same anchor, similar traded value, but
        # NOT winners — otherwise "is a smallcap" would dominate every term
        cur.execute("""
            WITH liq AS (
              SELECT symbol,
                     PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tottrdval) AS mv
              FROM nse_bhavcopy_data
              WHERE series IN ('EQ','BE') AND trade_date BETWEEN %s AND %s
              GROUP BY symbol HAVING COUNT(*) >= 100
            ),
            band AS (SELECT MIN(mv) lo, MAX(mv) hi FROM liq
                     WHERE symbol = ANY(%s))
            SELECT l.symbol FROM liq l, band b
            WHERE l.mv BETWEEN b.lo AND b.hi
              AND NOT (l.symbol = ANY(%s))
            ORDER BY random() LIMIT %s
        """, (anchor - timedelta(days=365), anchor, tickers, tickers,
              len(tickers) * 3))
        controls = [r["symbol"] for r in cur.fetchall()]

        for group, syms in (("win", tickers), ("ctl", controls)):
            for t in syms:
                cur.execute("""
                    SELECT raw_text FROM mg_documents
                    WHERE country='IN' AND UPPER(TRIM(ticker))=%s
                      AND filed_at BETWEEN %s AND %s AND raw_text IS NOT NULL
                    ORDER BY filed_at DESC LIMIT %s
                """, (t, anchor - timedelta(days=540), anchor, max_docs_per_ticker))
                docs = [r["raw_text"] for r in cur.fetchall()]
                if not docs:
                    continue
                terms = set()
                for txt in docs:
                    for w in re.findall(r"\b[A-Z][A-Za-z]{3,15}\b", txt[:60000]):
                        u = w.upper()
                        if u not in GENERIC and not u.isdigit():
                            terms.add(u)
                if group == "win":
                    n_win += 1
                    win_df.update(terms)
                else:
                    n_ctl += 1
                    ctl_df.update(terms)

    if not n_win or not n_ctl:
        print("Not enough documents to compare.")
        return

    print(f"\nDocument-bearing tickers: winners={n_win} controls={n_ctl}")
    print(f"\n=== terms over-represented in MISSED WINNERS vs matched controls ===")
    print(f"{'term':24} {'win%':>7} {'ctl%':>7} {'ratio':>7}")
    scored = []
    for term, wc in win_df.items():
        wp = wc / n_win
        if wp < 0.15:            # must appear in >=15% of winners to matter
            continue
        cp = ctl_df.get(term, 0) / n_ctl
        ratio = wp / (cp + 1.0 / n_ctl)   # smoothed
        scored.append((ratio, term, wp, cp))
    for ratio, term, wp, cp in sorted(scored, reverse=True)[:top_terms]:
        print(f"{term:24} {wp*100:6.1f}% {cp*100:6.1f}% {ratio:7.2f}x")
    print("\nThese are HYPOTHESES, not fingerprints. Any candidate must enter "
          "mg_tracked_schemes with t1_alone_eligible=false and show a "
          "multi-quarter population edge before it can qualify a name alone.")


def main():
    ap = argparse.ArgumentParser(description="Systematic winner autopsy")
    ap.add_argument("--stage", choices=["cohort", "recall", "mine", "all"],
                    default="all")
    ap.add_argument("--include-capabilities", action="store_true",
                    help="admit product-side (mg_company_capabilities) names "
                         "into the screen universe — the A/B test for whether "
                         "widening coverage actually lifts recall")
    ap.add_argument("--no-store", action="store_true",
                    help="report only; do not overwrite mg_winner_recall "
                         "(use when A/B testing so the baseline survives)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="winner definition, forward multiple (default 5.0)")
    ap.add_argument("--include-suspect", action="store_true",
                    help="keep names with corporate-action-suspect day factors")
    ap.add_argument("--top-n", type=int, default=40,
                    help="screen top_n used during recall replay")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    ensure_tables(conn)

    if args.stage in ("cohort", "all"):
        print(f"=== STAGE A: winner cohort (>={args.threshold}x over "
              f"{HORIZON_DAYS}d, EQ/BE only, CA-adjusted) ===")
        build_cohort(conn, cur, args.threshold, args.include_suspect)
    if args.stage in ("recall", "all"):
        mode = ("universe = beneficiaries + scheme citers + PRODUCT-SIDE capabilities"
                if args.include_capabilities else
                "universe = beneficiaries + scheme citers (baseline)")
        print(f"\n=== STAGE B: recall by pipeline stage ({mode}) ===")
        measure_recall(conn, cur, args.top_n,
                       include_capabilities=args.include_capabilities,
                       store=not args.no_store)
    if args.stage in ("mine", "all"):
        print("\n=== STAGE C: differential signal mining ===")
        mine_signals(cur)

    conn.close()


if __name__ == "__main__":
    main()
