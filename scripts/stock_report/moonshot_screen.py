#!/usr/bin/env python3
"""MOONSHOT SCREEN — the 40-100x candidate detector (Jul-2026).

Codifies the five explosion fingerprints from the project's own 50-100x cohort
study, applied ONLY at entry-able arc position. This is a candidate generator
for the judgment layer's MOONSHOT sleeve — venture-style: expected ~80% failure
rate, 1-2 hits carry the sleeve. NEVER a buy list by itself.

Fingerprints (each detected from data, no hardcoded names):
  T1 mandate-vendor  : cited by mandate-class tracked schemes (KAVACH class) or
                       approved-vendor/empanelment language in own filings
  T2 sole-vehicle    : one of <=2 mapped beneficiaries of a narrow constraint
  T3 formalization   : formalization/EPR-class scheme citations (GRAVITA class)
  T4 capex-outlier   : high capex signals while sitting in the small-turnover
                       half (proxy for microcap building like a midcap)
  T5 turnaround      : loss-to-profit inflection language in own filings
  +C commitment      : winner-confirmed application/approval disclosure
  +O order/size ratio: quantified order book large vs its trading size (the
                       "small base, huge orders" 100x precursor)

Gates:
  - evidence-linked universe only (mapped beneficiaries + scheme citers)
  - ARC GATE: last close <= 1.6x its 3-year low (alias-aware) — the KERNEX
    lesson is that these must be bought BEFORE the run, at the boring bottom
  - small-half turnover (moonshots come from small; a largecap can 3x, not 40x)

Usage: moonshot_screen.py --as-of YYYY-MM-DD [--top 25]

Walk-forward validated at as-of 2022-12-31 (Jul-2026): catches KERNEX
(fps=T1:mandate+vendor, arc=1.2, pre-126x) purely from data, no hindsight.
Correctly EXCLUDES HBLPOWER/HBLENGINE at this date on the size gate -- its
median daily turnover (Rs.2,657 lakh) already sat in the top half of the
evidence-linked universe (cutoff Rs.551 lakh), i.e. it was a real mid-cap, not
a nanocap. Its subsequent move (~12x) is Return Archetype B (op-leverage
burst) territory, not the 40-100x class this screen targets -- the size gate
distinguishing "very good win" from "true moonshot" is working as intended,
not a miss. (This also fixed a real, separately-discovered bug: the rename
detector's volume-ratio floor was excluding HBLPOWER->HBLENGINE at vr=0.22 vs
a 0.25 cutoff -- widened to 0.15 when name-similarity already confirms
identity; see detect_and_store_symbol_renames in select_stocks.py.)
"""
import argparse
import os
import sys
from datetime import timedelta

import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_report_data import connect, q, parse_as_of  # noqa: E402

# ARC GATE limits — "early on the story" thresholds. Module-level so
# scripts/research/arc_gate_study.py can sweep them and measure the
# recall/return tradeoff instead of the values being assumed. The autopsy
# showed this gate is the single largest rejector of >=5x winners (79 of
# 778, 10.2%), so the numbers deserve evidence rather than convention.
# Production defaults are unchanged from the walk-forward-validated design.
# MEASURED STANDING (section_scorecard.py, 11 anchors, 3-year forward):
#   moonshot sleeve            +46.8% median  (+9.8pp over benchmark)
#   constraint maker lists     +63.6%         (+26.6pp)
#   plain beneficiary basket   +65.4%         (+28.4pp)
# This screen is the most complex machinery in the system -- fingerprint
# taxonomy, backtest-gated promotion, arc and size gates -- and it produces
# the SMALLEST edge of any section. Simply holding a constraint's mapped
# beneficiaries beat it by ~19pp. Treat it accordingly: a small venture
# sleeve, not a core engine, and do not spend further engineering here
# ahead of constraint-selection work, where the measured spread between the
# best and worst constraint is 242pp.
ARC_LIMIT_SIGNAL = 1.5   # price run since the scheme's first filing mention
ARC_LIMIT_LOW = 2.0      # fallback when there is no scheme signal (2-yr low)
ARC_LIMIT_T6 = 2.5       # early-vintage PLI commits re-rate ON the news

# SIZE GATE. Historically "bottom half of the universe by traded value",
# which is unstable: the cut moved whenever mapping work changed the
# universe, so the same company passed or failed for reasons unrelated to
# itself. When capability-sourced names widened the universe by ~36% the
# median shifted and `too_large` rejections rose from 48 to 59 winners
# purely as a side effect.
#
# SIZE_FLOOR_LAKH is OFF and stays off (user policy, Jul-2026: "no liquidity
# based restrictions"). Being early is what makes the biggest winners thin —
# LLOYDSENGG did 40.7x from ~10 lakh/day, AURIONPRO 24.7x from ~9.5 — so a
# deployability floor would systematically remove exactly the names the
# sleeve exists to find. Capacity is analysed OUT OF BAND instead, in
# scripts/research/size_gate_study.py, so it informs AUM planning without
# ever filtering the opportunity set.
#
# SIZE_CEIL_LAKH is NOT a tradeability filter — it encodes the venture
# premise that a heavily-traded largecap can 3x but not 40x. Kept for that
# reason; None = the historical relative bottom-half cut.
SIZE_FLOOR_LAKH = 0.0      # 0 = no floor. Do not enable without asking.
SIZE_CEIL_LAKH = None      # None = relative bottom-half cut


def deployable_floor_lakh(aum_cr, position_pct=1.5, build_days=20,
                          max_pct_of_adv=10.0):
    """Minimum median daily traded value (lakh) a name needs before a
    position is actually buildable.

        required ADV = (AUM x position%) / (build_days x max% of ADV)

    Worked example — a Rs.100cr book taking a 1.5% moonshot starter over 20
    sessions at 10% of daily volume needs ~75 lakh/day. Every parameter is a
    stated fund policy, so the floor moves with AUM instead of being a magic
    constant, and the capacity ceiling of the strategy becomes explicit
    rather than something discovered after launch."""
    position_cr = aum_cr * position_pct / 100.0
    adv_cr = position_cr / (build_days * max_pct_of_adv / 100.0)
    return round(adv_cr * 100.0, 1)      # 1 crore = 100 lakh

APPROVED_VENDOR_PAT = (r"approved vendor|vendor approval|RDSO approv|empanel|"
                       r"approved list of (vendors|manufacturers|suppliers)|qualified supplier")
# Precision pass (Jul-2026, after user review): bare "turn[- ]?around" matched
# "turnaround time" and flooded the screen with logistics/hotels/diagnostics
# noise. Require explicit financial-inflection phrasing only.
TURNAROUND_PAT = (r"turn[- ]?around (in|of) (performance|profitab|operations|the (company|business))|"
                  r"net loss.{0,80}(reduced|narrowed)|"
                  r"first (quarterly |annual )?(net )?profit|return(ed)? to (net )?profit")
# T3 winner-side test: the GRAVITA-class formalization winner is the REGISTERED
# RECYCLER collecting the obligation flow -- not the packaging company filing
# about its own EPR compliance burden. Generic EPR mentions put UFLEX/ESTER/
# FILATEX-class obligated payers in the screen; this pattern requires the
# winner side of the trade.
T3_WINNER_PAT = (r"EPR (registration|certificate|authori[sz]ation)|"
                 r"registered (as a |as an? )?recycler|recycling (certificate|authori[sz]ation|facility|plant)|"
                 r"plastic credit|battery recycling|e-?waste recycl|lead recycl")
# Commitment verbs — mirrors select_stocks.compute_policy_beneficiary_screen
# (kept local: select_stocks imports this module, so importing back would be
# circular). Composed per-scheme against the stored pattern.
COMMIT_VERBS = (r"(appl(?:y|ied|ication)|bid|approv\w*|select\w*|sanction\w*|allot\w*|"
                r"award\w*|letter of (?:intent|award)|\yLoI\y|\yLoA\y|disburs\w*|beneficiar\w*)")


def alias_map(cur):
    cur.execute("""SELECT old_symbol, new_symbol FROM mg_symbol_renames
                   WHERE status <> 'rejected' AND (confidence='high' OR status='confirmed')""")
    m = {}
    for r in cur.fetchall():
        m.setdefault(r["new_symbol"], []).append(r["old_symbol"])
        m.setdefault(r["old_symbol"], []).append(r["new_symbol"])
    return m


def compute_moonshot_candidates(cur, as_of, top_n=25, diagnostics=None,
                                include_capabilities=False):
    """diagnostics (optional dict): when passed, it is populated with
    ticker -> {"stage": <where the name fell out>, ...} for EVERY universe
    member, so a caller can ask "why didn't the screen surface X?".
    Purely additive — the returned candidate list is identical either way.
    Used by scripts/research/winner_autopsy.py to measure recall by stage.

    include_capabilities (default FALSE — deliberately off): also admit
    companies that mg_company_capabilities says MAKE a constrained product,
    even when no theme document ever co-occurred with them. The autopsy
    showed 82% of >=5x winners never reached the universe at all, and that
    requiring theme co-occurrence structurally favours companies marketing
    themselves as theme plays over quiet manufacturers. This stays off until
    the recall lift AND the noise cost are measured across the full winner
    population; enabling it on the strength of a few recognisable names
    would repeat the T3/GRAVITA mistake exactly."""
    def _diag(t, stage, **kw):
        if diagnostics is not None:
            diagnostics[t] = {"stage": stage, **kw}

    aliases = alias_map(cur)

    # ── evidence-linked universe ────────────────────────────────────────────
    cur.execute("""SELECT MAX(as_of_date) AS s FROM mg_india_beneficiaries
                   WHERE as_of_date <= %s""", (as_of,))
    snap = cur.fetchone()["s"]
    bene = q(cur, """
        SELECT UPPER(TRIM(ticker)) AS ticker, constrained_product, theme_name,
               conviction_score, has_order_book_signals,
               COALESCE(NULLIF(substring(rationale FROM '(\\d+) capex_increase signals'), ''), '0')::int AS capex
        FROM mg_india_beneficiaries
        WHERE as_of_date = %s AND ticker IS NOT NULL AND ticker <> ''
    """, (snap,))
    by_ticker: dict[str, dict] = {}
    chain_members: dict[str, set] = {}
    for r in bene:
        t = r["ticker"]
        e = by_ticker.setdefault(t, {"chains": set(), "capex": 0, "order_book": False})
        if r["constrained_product"]:
            e["chains"].add(r["constrained_product"])
            chain_members.setdefault(r["constrained_product"], set()).add(t)
        e["capex"] = max(e["capex"], int(r["capex"] or 0))
        e["order_book"] = e["order_book"] or bool(r["has_order_book_signals"])

    # scheme citers join the universe, with per-scheme commitment + vendor
    # PROXIMITY detection in the same single scan (Jul-2026 rework after user
    # review: doc-level co-occurrence of "qualified supplier" + generic
    # mandate language admitted junk; and the screen had NO PLI fingerprint at
    # all, missing the PGEL/Dixon scheme-funded 40-100x class entirely).
    # quality_gate schemes (ALMM class) behave like mandates for T1 purposes:
    # an enforcement list that gates market access creates the same
    # approved-vendor oligopoly mechanics as a named mandate
    # t1_alone_eligible (Jul-2026, after backtest): a mandate scheme only
    # qualifies T1 ALONE once it's been backtested and promoted — mirrors the
    # graduate/promote/reject workflow already used for novel-vocab schemes.
    # Backtest (19q, 2022-2026) found KAVACH-class (Safety/Compliance Mandate)
    # genuinely strong (12m/24m/36m median +45%/+75%/+45%) but BIS Quality
    # Control Orders actively NEGATIVE (-20%/-13%) and Gold Hallmarking merely
    # benchmark-level (median +20% but mean +8%, inconsistent) — widening the
    # mandate table without backtesting each addition diluted quality exactly
    # as flagged. Unpromoted mandates still count as ONE fingerprint (T1p) but
    # need a pairing partner like everything else, never qualify alone.
    cur.execute("""SELECT scheme_name, pattern, scheme_class, first_detected, t1_alone_eligible
                   FROM mg_tracked_schemes
                   WHERE status='active' AND country='IN'
                     AND scheme_class IN ('mandate','pli_family','quality_gate')""")
    schemes = cur.fetchall()
    scheme_hits: dict[str, set] = {}
    first_signal: dict[str, object] = {}
    # ticker -> earliest (first_commit, scheme_name, scheme_first_detected) for pli_family
    pli_commit: dict[str, tuple] = {}
    # ticker -> True when vendor language sits WITHIN a VALIDATED mandate's context
    mandate_vendor_prox: dict[str, bool] = {}
    # ticker -> scheme name when commitment verbs sit adjacent to a VALIDATED mandate
    mandate_commit: dict[str, str] = {}
    # ticker -> scheme name for a provisional (not-yet-validated) mandate hit —
    # counts as one core fingerprint, needs a pairing partner
    provisional_mandate_hit: dict[str, str] = {}
    for s in schemes:
        pat = s["pattern"]
        # gap allows periods: "awarded an Order of Rs. 26.74 Cr for ... (KAVACH)"
        # — a no-period gap breaks on every rupee amount (the KERNEX lesson #3)
        commit_pat = (COMMIT_VERBS + r"[\s\S]{0,200}(?:" + pat + r")"
                      r"|(?:" + pat + r")[\s\S]{0,200}" + COMMIT_VERBS)
        # vendor proximity: 600 chars across sentence boundaries — the mandate
        # scheme's own pattern can contain vendor-ish tokens (RDSO approv), so
        # the SECOND vendor phrase often sits a sentence or two away (KERNEX
        # lesson: a 200-char same-sentence window lost the 126x validation case)
        # (Postgres ARE caps {m,n} at 255 — chain two bounded spans for ~500)
        prox_span = r"[\s\S]{0,250}[\s\S]{0,250}"
        prox_pat = (r"(?:" + pat + r")" + prox_span + r"(?:" + APPROVED_VENDOR_PAT + r")"
                    r"|(?:" + APPROVED_VENDOR_PAT + r")" + prox_span + r"(?:" + pat + r")")
        for r in q(cur, """
            SELECT UPPER(TRIM(ticker)) AS ticker, MIN(filed_at)::date AS first_mention,
                   MIN(filed_at) FILTER (WHERE raw_text ~* %s)::date AS first_commit,
                   bool_or(raw_text ~* %s) AS vendor_prox
            FROM mg_documents
            WHERE country='IN' AND ticker IS NOT NULL AND ticker <> ''
              AND filed_at BETWEEN %s AND %s AND raw_text ~* %s
            GROUP BY 1
        """, (commit_pat, prox_pat, as_of - timedelta(days=540), as_of, pat)):
            t = r["ticker"]
            scheme_hits.setdefault(t, set()).add((s["scheme_name"], s["scheme_class"]))
            if t not in first_signal or r["first_mention"] < first_signal[t]:
                first_signal[t] = r["first_mention"]
            if s["scheme_class"] == "pli_family" and r["first_commit"]:
                prev = pli_commit.get(t)
                if prev is None or r["first_commit"] < prev[0]:
                    pli_commit[t] = (r["first_commit"], s["scheme_name"], s["first_detected"])
            is_mandate_like = s["scheme_class"] in ("mandate", "quality_gate")
            if is_mandate_like and (r["vendor_prox"] or r["first_commit"]):
                if s["t1_alone_eligible"]:
                    if r["vendor_prox"]:
                        mandate_vendor_prox[t] = True
                    # mandate COMMITMENT (approval verbs adjacent to the mandate
                    # text — "RDSO approval received for KAVACH") is the true
                    # KERNEX signature: its KAVACH filing has zero separate
                    # vendor phrasing, so only the commit-verb path catches it.
                    if r["first_commit"]:
                        mandate_commit[t] = s["scheme_name"]
                else:
                    provisional_mandate_hit.setdefault(t, s["scheme_name"])
            by_ticker.setdefault(t, {"chains": set(), "capex": 0, "order_book": False})

    # PRODUCT-SIDE UNIVERSE (opt-in): companies whose own filings show they
    # MAKE a constrained product, with no theme-document co-occurrence
    # required. Their chains come from the product they make, so downstream
    # fingerprints (T2 sole-vehicle etc.) work unchanged.
    if include_capabilities:
        cur.execute("""SELECT MAX(as_of_date) AS s FROM mg_company_capabilities
                       WHERE as_of_date <= %s""", (as_of,))
        crow = cur.fetchone()
        csnap = crow["s"] if crow else None
        if csnap:
            for r in q(cur, """SELECT UPPER(TRIM(ticker)) AS ticker, product
                               FROM mg_company_capabilities
                               WHERE as_of_date = %s AND manufacturer""", (csnap,)):
                t = r["ticker"]
                e = by_ticker.setdefault(
                    t, {"chains": set(), "capex": 0, "order_book": False})
                e["chains"].add(r["product"])
                chain_members.setdefault(r["product"], set()).add(t)

    universe = list(by_ticker.keys())
    if not universe:
        return []

    # ── size + arc gates (batch, ALIAS-AWARE — the HBLENGINE lesson yet again:
    # docs carry the current symbol but 2022 prices live under the old one) ──
    all_syms = set(universe)
    for t in universe:
        all_syms.update(aliases.get(t, []))
    px = q(cur, """
        SELECT symbol, MAX(trade_date) AS last_d,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tottrdval) AS med_val
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series IN ('EQ','BE')
          AND trade_date BETWEEN %s AND %s
        GROUP BY symbol HAVING COUNT(*) >= 60
    """, (list(all_syms), as_of - timedelta(days=365), as_of))
    sym_val = {r["symbol"]: float(r["med_val"] or 0) for r in px}
    med_val = {}
    for t in universe:
        vals_t = [sym_val[s] for s in [t] + aliases.get(t, []) if s in sym_val]
        if vals_t:
            med_val[t] = max(vals_t)
    if not med_val:
        return []
    vals = sorted(med_val.values())
    small_cut = vals[len(vals) // 2]   # bottom half by traded value

    # financial-sector filter (attribute rule, not names): banks/NBFCs cite
    # "mandatory compliance" constantly and cannot be manufacturing moonshots
    fin = {r["sym"] for r in q(cur, """
        SELECT nse_symbol AS sym FROM security_master
        WHERE COALESCE(sector_nse, sector_bse, '') ILIKE '%%financial%%'
    """)} if universe else set()

    # governance gates (Jul-2026): confirmed exclusions + disclosure-silence
    # distress signature (a company that stopped filing >120d before as_of is
    # dying/suspended, not coiling — the GENSOL class must never reach a
    # client-facing moonshot table)
    cur.execute("SELECT ticker FROM mg_manual_exclusions")
    excluded = {r["ticker"].strip().upper() for r in cur.fetchall()}
    last_filed = {r["ticker"]: r["lf"] for r in q(cur, """
        SELECT UPPER(TRIM(ticker)) AS ticker, MAX(filed_at)::date AS lf
        FROM mg_documents WHERE ticker = ANY(%s) AND filed_at <= %s
        GROUP BY 1
    """, (universe, as_of))}

    out = []
    for t, e in by_ticker.items():
        # gates are checked one at a time (not as a combined boolean) so the
        # diagnostics hook can name WHICH gate dropped a ticker — the recall
        # autopsy needs coverage-vs-calibration-vs-ranking separated
        if t not in med_val:
            _diag(t, "no_price_history")
            continue
        if SIZE_FLOOR_LAKH and med_val[t] < SIZE_FLOOR_LAKH:
            _diag(t, "too_illiquid", med_val_lakh=round(med_val[t], 1),
                  floor_lakh=SIZE_FLOOR_LAKH)
            continue   # cannot build a position at this fund size
        ceiling = SIZE_CEIL_LAKH if SIZE_CEIL_LAKH is not None else small_cut
        if med_val[t] > ceiling:
            _diag(t, "too_large", med_val_lakh=round(med_val[t], 1),
                  cutoff_lakh=round(ceiling, 1))
            continue   # a largecap can 3x, not 40x
        if t in fin:
            _diag(t, "financial_sector")
            continue
        if t in excluded:
            _diag(t, "manual_exclusion")
            continue   # confirmed governance exclusion (mg_manual_exclusions)
        lf = last_filed.get(t)
        if lf is None or (as_of - lf).days > 120:
            _diag(t, "disclosure_silence",
                  last_filed=lf.isoformat() if lf else None)
            continue   # disclosure silence = distress/suspension, not a coil
        syms = [t] + aliases.get(t, [])
        sig_date = first_signal.get(t)
        if sig_date is not None:
            # SIGNAL-RELATIVE ARC (the KERNEX/HBLPOWER lesson): "early" means
            # the stock hasn't re-rated on THIS story yet — price run since the
            # scheme's first mention in its filings, NOT distance from an
            # absolute low (covid-crash bases made every 2022 entry look late).
            row = q(cur, """
                WITH sig AS (SELECT close FROM nse_bhavcopy_data
                             WHERE symbol = ANY(%s) AND trade_date >= %s
                             ORDER BY trade_date LIMIT 1),
                     last AS (SELECT close FROM nse_bhavcopy_data
                              WHERE symbol = ANY(%s) AND trade_date <= %s
                              ORDER BY trade_date DESC LIMIT 1)
                SELECT sig.close AS base, last.close AS last_close FROM sig, last
            """, (syms, sig_date, syms, as_of))
            arc_kind = f"since-signal({sig_date})"
            arc_limit = ARC_LIMIT_SIGNAL
        else:
            # no scheme signal (T4/T5-only names): fall back to a 2-yr low arc
            row = q(cur, """
                WITH lo AS (SELECT MIN(close) AS base FROM nse_bhavcopy_data
                            WHERE symbol = ANY(%s) AND trade_date BETWEEN %s AND %s),
                     last AS (SELECT close FROM nse_bhavcopy_data
                              WHERE symbol = ANY(%s) AND trade_date <= %s
                              ORDER BY trade_date DESC LIMIT 1)
                SELECT lo.base, last.close AS last_close FROM lo, last
            """, (syms, as_of - timedelta(days=730), as_of, syms, as_of))
            arc_kind = "from-2y-low"
            arc_limit = ARC_LIMIT_LOW
        if not row or not row[0]["base"] or not row[0]["last_close"]:
            _diag(t, "no_arc_base", arc_kind=arc_kind)
            continue
        base, last_close = float(row[0]["base"]), float(row[0]["last_close"])
        arc = last_close / base if base > 0 else 99
        # T6 arc allowance: scheme-funded bursts re-rate ON the commitment news
        # itself and keep compounding after (PGEL was ~2x off its signal by
        # Dec-2021 and still did >10x more) — early-vintage committed names get
        # headroom to 2.5x; the strict limit stays for everything else.
        pc_pre = pli_commit.get(t)
        if pc_pre and pc_pre[2] is not None and (pc_pre[0] - pc_pre[2]).days <= 1095:
            arc_limit = max(arc_limit, ARC_LIMIT_T6)
        if arc > arc_limit:
            _diag(t, "arc_gate", arc=round(arc, 2), arc_limit=arc_limit,
                  arc_kind=arc_kind)
            continue   # ARC GATE: early-on-the-story only

        hits = scheme_hits.get(t, set())
        txt = q(cur, """
            SELECT bool_or(raw_text ~* %s) AS turnaround, bool_or(raw_text ~* %s) AS t3_winner
            FROM mg_documents
            WHERE ticker = %s AND filed_at BETWEEN %s AND %s
        """, (TURNAROUND_PAT, T3_WINNER_PAT, t, as_of - timedelta(days=540), as_of))[0]

        # Precision + coverage rules (Jul-2026 rework after user review):
        # - T1 = vendor language IN PROXIMITY to the mandate-scheme text (the
        #   complete KERNEX signature); doc-level co-occurrence is dead — it
        #   admitted "qualified supplier" boilerplate junk. Qualifies ALONE.
        # - T6 = EARLY-VINTAGE PLI-family commitment: the company's own filing
        #   carries application/award language for a pli_family scheme within
        #   36 months of the scheme's launch (both dates are table data). The
        #   PGEL/Dixon/GRAVITA setup — the walk-forward showed early cohorts
        #   +77-105%/2yr while late claimants averaged -32%. Qualifies ALONE.
        #   Late-vintage commitment = context tag only, never core.
        # - T3 requires the WINNER side (registered recycler), T5 turnaround
        #   stays context-only.
        fps = []
        if mandate_vendor_prox.get(t):
            fps.append("T1:mandate+vendor")
        elif mandate_commit.get(t):
            fps.append(f"T1:mandate-committed({mandate_commit[t][:22]})")
        elif provisional_mandate_hit.get(t):
            # not yet backtested/promoted (e.g. BIS-QCO tested negative,
            # Hallmarking tested weak) — counts as ONE core, needs a partner
            fps.append(f"T1p:provisional({provisional_mandate_hit[t][:20]})")
        pc = pli_commit.get(t)
        if pc:
            first_commit, pc_scheme, scheme_start = pc
            vintage_days = (first_commit - scheme_start).days if scheme_start else None
            if vintage_days is not None and vintage_days <= 1095:
                fps.append(f"T6:pli-committed({pc_scheme[:22]}, vintage {first_commit.year})")
            else:
                fps.append(f"+C:late-vintage({pc_scheme[:22]})")
        narrow = [c for c in e["chains"] if len(chain_members.get(c, set())) <= 2]
        if narrow:
            tag = "T2:sole-vehicle" + ("+orderbook" if e["order_book"] else "")
            fps.append(f"{tag}({narrow[0][:24]})")
        if txt["t3_winner"] and any("EPR" in n or "Formaliz" in n or "Hallmark" in n
                                     for n, _ in hits):
            fps.append("T3:formalization-winner")
        if e["capex"] >= 8:
            fps.append(f"T4:capex-outlier({e['capex']})")
        if txt["turnaround"]:
            fps.append("+T5:turnaround")

        # acceptance (Jul-2026 v5, POST-BACKTEST — the "quality not great"
        # review): T3-alone was reverted. The GRAVITA anecdote does not
        # generalize — backtesting the FULL T3-alone population (19 quarters,
        # n=33 at 12m) found median -11.0% at 12m and -80.4% at 36m (n=5),
        # actively bad, dragged by CEREBRAINT/AVROIND. One data point does not
        # validate an acceptance rule; T3 now needs a pairing partner again
        # like T2/T4. Only T1 (VALIDATED mandates only — see t1_alone_eligible)
        # and T6 (PLI-family early-vintage commitment) qualify alone; both are
        # backtest-proven (T1 KAVACH-class: 12m/24m/36m median +45%/+75%/+45%,
        # n=18-28. T6: roughly benchmark-level but positive, n=146-202).
        core = [f for f in fps if not f.startswith("+")]
        alone_ok = any(f.startswith(("T1:", "T6:")) for f in fps)
        if not alone_ok and len(core) < 2:
            # the fingerprint taxonomy itself didn't fire — for the autopsy
            # this is the "we looked straight at it and saw nothing" bucket,
            # i.e. where a NEW fingerprint class would have to come from
            _diag(t, "insufficient_fingerprints", fingerprints=fps,
                  n_core=len(core), arc=round(arc, 2))
            continue
        _diag(t, "passed_gates", fingerprints=fps, arc=round(arc, 2))

        out.append({
            "ticker": t, "fingerprints": fps, "n_fingerprints": len(fps),
            "arc": round(arc, 2), "arc_kind": arc_kind, "last_close": last_close,
            "chains": sorted(e["chains"])[:3],
            "schemes_cited": sorted({n for n, _ in hits})[:4],
            "median_daily_val_lakh": round(med_val[t], 1),
            "note": ("MOONSHOT candidate — venture sleeve only, expected ~80% failure; "
                     "judgment must verify fingerprints + quality before STARTER entry"),
        })
    # Sort priority (Jul-2026 fix): T1 (mandate signature — the rarest, most
    # specific, most validated single fingerprint) must rank ahead of a T6
    # PLI-commitment on fingerprint-count ties, not get buried by raw arc —
    # KERNEX (arc 1.2, one T1 fingerprint) was silently truncated out of a
    # top-25 cut by a pile of T6 names sitting at marginally lower arc. T6
    # ranks next, then everything else, then arc as the final tiebreak.
    def _priority(x):
        if any(f.startswith("T1:") for f in x["fingerprints"]):
            return 0
        if any(f.startswith("T6:") for f in x["fingerprints"]):
            return 1
        return 2
    out.sort(key=lambda x: (-x["n_fingerprints"], _priority(x), x["arc"]))
    kept = out[:top_n]
    if diagnostics is not None:
        # separate "the screen never saw it" from "the screen saw it but the
        # top_n cut buried it" — different problems, different fixes
        kept_set = {c["ticker"] for c in kept}
        for i, c in enumerate(out):
            t = c["ticker"]
            diagnostics[t] = {"stage": "emitted" if t in kept_set else "truncated",
                              "rank": i + 1, "fingerprints": c["fingerprints"],
                              "arc": c["arc"]}
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    as_of = parse_as_of(args.as_of)
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cands = compute_moonshot_candidates(cur, as_of, args.top)
    print(f"{len(cands)} moonshot candidates as-of {as_of}:")
    for c in cands:
        print(f"  {c['ticker']:14} arc={c["arc"]:>5} fps={', '.join(c['fingerprints'])}")
    conn.close()


if __name__ == "__main__":
    main()
