---
name: makrograph-stock-selector
description: >
  Surface a ranked list of Indian or US stocks worth analyzing for a given date, from
  the MakroGraph DB — major + emerging themes (6-12 month window), major constraints
  with explanations, and supply-side beneficiaries. Also answers single-theme queries:
  "who benefits from the transformer constraint", "solar cell companies as of <date>".
  Trigger on: "which stocks should I analyze", "stock selector", "today's
  opportunities", "give me stocks for <date>", "what themes are emerging", "shortlist
  stocks", "US opportunities", or any theme/constraint + beneficiaries question.
  Default country is India; use US when the user says US/America/NASDAQ/NYSE. If no
  date given, use today. Strictly point-in-time: nothing dated after the given date.
---

# Stock Selector (as-of-date opportunity scan)

One command replaces clicking through the UI tabs: for a given date, what themes are
happening/emerging (last 6-12 months), what constraints drive them and why, which
supply-side companies benefit, and which stocks deserve a full report next.

## Process efficiency (read before Step 1)

- **Check the printed `SUMMARY:` line first** (theme/candidate counts, top-5 tickers)
  instead of dumping the full JSON with a Python heredoc — it's usually enough to
  confirm the run worked before you start writing the briefing.
- **Use `jq` for targeted pulls** (e.g., `jq '.ranked_candidates[:15]' file.json`)
  rather than printing entire arrays to inspect them.
- The chat briefing is text you write directly from the JSON fields — no HTML/PDF
  step unless asked, so there's no multi-draft rewrite cost here; just avoid
  re-reading the same JSON section more than once.

## Workflow

### Step 0 — Monthly production run (PMS mode, Jul-2026)

For the live monthly cycle use the orchestrator instead of individual scripts:

```bash
.venv/bin/python scripts/stock_report/run_monthly.py --as-of <YYYY-MM-DD> [--country IN]
```

It gates on data freshness (bhavcopy ≤7d, filings ≤21d, mapping snapshot ≤120d
— abort on stale, `--force` is loud), runs the scan, STOPS if the judgment
sidecar (with `decision` block) doesn't exist yet — the judgment pass is
Claude's job via this skill — then resumes: portfolio_construct (weights/caps),
render, PDF, and log_decisions (append-only mg_decisions track record; the
GVT&D/POWERINDIA-class evidence for PMS marketing accrues there automatically).
Also run monthly: `scripts/policy/ingest_pib.py` (day-0 scheme announcements →
mg_policy_announcements; scheme_score ≥3 rows feed the 3b-pre scorecard) and
`scripts/portal/build_portal.py` (static track-record site; publishing needs
compliance sign-off).

New judgment evidence available per scan (Jul-2026): each candidate carries
`order_book_quantified` (₹cr latest/prev/trend parsed from its own filings —
verify against the boolean flag; single-order vs full-backlog readings can mix,
read n_readings); report fields `exclusion_proposals` (HIGH governance events +
the GENSOL suspension signature: enforcement-type filing then >90d disclosure
silence) and `mapping_artifacts` (mapped beneficiaries whose own filings never
mention the product — presumptive pure-play-leg failures).

### Step 1 — Extract

```bash
.venv/bin/python scripts/stock_report/select_stocks.py --as-of <YYYY-MM-DD> [--country IN|US]
```

Run from project root; prints the JSON path (data/reports/stock_selector_<country>_<date>_data.json).
Default emergence window is 12 months (`--window-months 6` for a tighter scan).
**US mode** (`--country US`): build the country-scoped company-product-role and
constraint-candidate snapshots first, then use the same exact
constraint-product-company decision contract as India. Never reuse legacy India
capacity/import tables for US. Local US technical data is unavailable, so valuation,
liquidity and chart entry checks remain mandatory. See `us_data_note` in the JSON.

**Theme-focus mode** — when the user names ONE theme or constraint (e.g. "transformer",
"Solar Cell", "PCB", "Artificial Intelligence"), add `--theme "<name>"` (fuzzy match):

```bash
.venv/bin/python scripts/stock_report/select_stocks.py --as-of <date> --theme "transformer" [--country US]
```

The JSON then has mode=theme_focus with `focus`: matched_themes (stage, snapshots,
full beneficiary list with ranks), matched_constrained_products,
matched_supply_side_companies (IN), capacity_gaps and import_dependencies (IN).
Present: what the theme/constraint is + why it exists, key dates (first_detected /
first_mapped), stage now, then the beneficiary table (rank, ticker, type, conviction,
order-book), and suggest full reports for the top 2-3 names. If the name matches
nothing, list a few available theme names (query mg_themes) and ask which one.

### Step 1b — Verify the new improvement fields are present

Check the SUMMARY line for `cross_theme_overlap_leaders` and `high_confidence_themes`.
If the JSON was generated before these improvements, re-run Step 1. The JSON must
contain: `final_decision`, `evidence_dashboard`, `cross_theme_overlap`, `bear_cases`,
and each candidate must have `scoring_breakdown`. The only position-authority lanes
are `priorities`, `early_timing_candidates`, and the separately capped
`discovery_starter_candidates`. Use `jq '.final_decision'` to confirm.

### Step 1c — Verify constraint-detection coverage before judging stocks

Read `final_decision.constraint_detection_coverage` and each displayed
constraint's `detection_origins`. The detector must union mapper products,
dated capacity/import reference records, reviewed `EXACT` aliases linked to
the dated constraint ledger, and the upstream ingestion/NLP candidate
snapshot. A current reference-only chain may be real
before any listed producer has been mapped; classify it `MAP_COMPANIES` / no
position until the company-role pipeline recovers an exact listed supplier.
Never turn broad, family, or policy aliases into item-level physical shortages.

Use the reference record's `as_of_date` or ledger `source_date`, never a
database `created_at` timestamp, in historical work. Inspect
`constraint_quality.source_diversity`: multiple fields from the same release
are one source family, not independent confirmations. Multiple uploads by one
issuer on one date are one evidence event, and grade A requires at least two
independent evidence dates. Diversity is a research quality diagnostic, not a
relaxation of the physical or company gates.

Use dated `mg_constraint_candidates` and
`mg_constraint_company_candidates` snapshots as the upstream detection
contract. `UPSTREAM_CONSTRAINT_PIPELINE` expands research coverage but never
relaxes physical or Buy gates. Company states through
`APPROVED_OPERATING_MAKER` prove only a product role; earnings capture and
underwriting remain separate.

### Step 2 — Analyze and present (chat answer by default)

Output a crisp chat briefing (only build a PDF if the user asks — same html_to_pdf.py
pipeline as the stock-report skill). Structure:

1. **Major themes — explainer table (MANDATORY columns)**: for the top 5-8 by
   strength_now, a table with exactly these columns:
   | Theme | Plain-English meaning | Started (`first_detected`, DD-Mon-YYYY) |
   | Phase now (`stage_label` + one-phrase read from `stage_evidence`) | Qtrs confirmed |
   The plain-English column translates the cryptic "X: Constraint from Y Demand" label
   into what is actually short and why (use `description` + `stage_evidence`; flag NLP
   label artifacts like "ESG/FDA Demand" and name the real chain). Always include a
   phase legend line: Emerging = first signals; Accelerating = capex committed,
   revenue visible, not crowded (the investable phase); Consensus = broadly discussed,
   momentum decelerating — market already knows.
2. **Emerging in the window** (from `emerging_themes`): same columns as above plus
   companies-mapped-in-window (`new_beneficiaries_in_window`). Say WHEN each emerged
   (`first_detected`) and what phase it has reached NOW — call out any theme that
   raced from birth to Consensus quickly (late to join) vs ones still Accelerating
   (the actionable ones).
3. **Major constraints — with explanation** (from `constrained_products`, `capacity_gaps`,
   `import_dependencies`): for each of the top 5-8 constrained products: what is short,
   why (domestic capacity vs demand, import dependence + origin country, qualification
   barriers), how broad (n_companies mapped), how convinced the pipeline is
   (avg/max conviction), and whether order-book evidence exists (any_order_book).
   **Maker-universe rule:** cover every selected constraint explicitly. For each
   atomic product node, separate (a) independently corroborated operating direct
   producers, (b) evidenced capacity pipelines/direct product roles, and (c)
   coverage gaps. A parent/group disclosure with exact product-and-capacity proof
   is valid evidence even when its exchange industry label differs; disclose the
   entity scope instead of vetoing it. Do not put EPC firms, developers, buyers,
   generic policy references, or lexical collisions in the direct-maker table.
   Keep those in the audit data or, where separately evidenced, an adjacent
   beneficiary table. Two evidence events must be non-duplicative; one clean
   capacity plan is a research lead, not an operating maker or Buy.
4. **Beneficiary stocks under EVERY theme/constraint (MANDATORY)** (from
   `supply_side_beneficiaries`): each theme or constraint presented in the briefing
   must carry its own beneficiary stock list (top 5-8 tickers in theme-rank order) —
   never present a theme without its stocks. For IN include beneficiary_type
   (direct/critical/input supplier), conviction, order-book flag. Supply side is the
   focus — capacity owners, not demand-side consumers; flag demand-side names that
   appear inside supply themes (e.g. software names in an energy-constraint theme)
   as noise instead of hiding them. Group related theme-chains (e.g. five utility
   variants) into one row with the union of their top names.
5. **Ranked candidate list — the formula screen** (from `ranked_candidates`): a table
   of the top 10-15: ticker, company, products/themes it spans, conviction, order-book,
   technical state (above 200DMA? % from 52w high). Present this as what it is — a
   mechanical screen that surfaces candidates. The composite score and `discovery_tier`
   are NOT the final call; the final categorization comes from Step 3 (judgment layer).
6. **Evidence dashboard — MANDATORY (Improvement 1)** (`evidence_dashboard`): a table
   for the top 6-8 themes showing companies_mapped, filings_covered,
   bottleneck_signals, confirmed_quarters, policy_events, and evidence_confidence_pct.
   The confidence_pct encodes how much hard evidence exists vs narrative momentum — a
   100% Consensus theme with 0 policy events is market-known but policy-unsupported.
   Flag any theme with confidence < 50 as "low-evidence, treat as watch only."

7. **Cross-theme overlap — MANDATORY (Improvement 2)** (`cross_theme_overlap`): a table
   of companies that appear in 3+ independent constraint chains, sorted by
   n_independent_chains then overlap_score. This is the "conviction multiplier" — a
   stock appearing in 8 independent chains is not noise, it's a structural chokepoint.
   Show ticker, chain count, and list the chains grouped by type (energy / semi / infra).
   Clearly flag demand-side names (PLTR, META) that appear here as cross-chain because
   EVERY theme mentions them, not because they own the constrained capacity.

8. **Transparent scoring — MANDATORY (Improvement 3)** (`scoring_breakdown` on each
   candidate): when presenting the ranked-candidate table include one sentence on the
   formula: IN = "0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub,
   × freshness multiplier — no technical term"; US = "0.40×relevance + 0.30×breadth +
   0.30×rank". For the top 3
   candidates show the actual component values from scoring_breakdown — never just the
   final number. Users should never wonder why a stock ranks where it does.

9. **Visual analytics (Improvement 4)** — for PDF/HTML output only:
   - Momentum bar: a CSS inline-style width bar proportional to evidence_confidence_pct
     (e.g., `<div style="width:{pct}%;background:#1a3a5c;height:6px">`) next to each
     theme in the evidence dashboard.
   - Overlap network: a two-column table — ticker | chains (listed as colored pills by
     category: energy=green, semi=blue, infra=amber).
   - Policy timeline: the policy_events count becomes a dated mini-timeline table.
   These do NOT apply to chat-text briefings; render the same information as compact
   text tables there.

10. **Bear-case analysis — MANDATORY (Improvement 5)** (`bear_cases`): after each Bucket A
    and B theme, a "What invalidates this" block with 1-3 specific risks from the
    bear_cases JSON (not generic boilerplate). Always include the two structural risks
    that apply to every energy theme (rate sensitivity, AI efficiency) and the two that
    apply to every chip theme (export-control two-sided, capex-reversal). End with:
    "If 2+ of these risks materialise simultaneously, revisit the bucket assignment."

11. **Next step**: suggest running the full report for the top picks, e.g.
    "generate report for KAYNES as of <date>" (the makrograph-stock-report skill).

### Step 2.5 — THE DECISION section (MANDATORY, rendered SECOND — the user's rule)

**Report section order (user mandate, updated Aug-2026):** THE DECISION (top) →
Constraint maker lists ("Who actually makes this") → PLI Shortlist → rest of
report content. Everything after THE DECISION is mechanical/discovery input;
the audit trail follows.

**MOONSHOT SLEEVE REMOVED from the client report (user decision, Aug-2026),
superseding the Jul-2026 "top of report" mandate.** The unified section
scorecard (11 anchors, 3-year forward) measured it the weakest section:
+46.8% median, only +9.8pp over benchmark, versus constraint maker lists
+63.6% (+26.6pp) and a plain mapped-beneficiary basket +65.4% (+28.4pp).
Most complex machinery in the system, smallest edge. The screen still runs
and `moonshot_candidates` stays in the data JSON as an internal watch-list,
but it does NOT render. Reinstate only as a small, explicit venture
allocation if ever — never as a default client-facing section.

**What the scorecard established the report SHOULD lead with:** constraint
choice dominates stock choice ~6:1 (best-vs-worst constraint spread 242pp vs
~40pp between stock-selection methods). Grade constraints primarily on
SCARCITY OF LISTED VEHICLES — the one grading leg that predicted on both the
flawed data (+26pp) and clean uncapped data (+22.9pp). Quantified capacity
gap went INVERSE on clean data (-19.3pp); conviction score is mildly inverse
(-9.5pp) yet still orders ranked_candidates — a live defect to fix. Caveat:
scarcity rests on n=33 over 3 snapshots with CRGO Steel + Power Transformer
(one correlated bet) carrying much of it — firm up before betting heavily.

**PMS-grade standard (user rule, Jul-2026):** the user is launching PMS
(Portfolio Management Services) around Aug-Sep 2026. The report is a production
deliverable — the user must never need their own judgment to extract the
constraint shortlist or the stock list. Any ambiguity that survives into the
rendered report is a DEFECT. Concretely: (1) THE DECISION box carries everything
actionable; (2) the mechanical `pli_shortlist` field renders directly below it
(sector-filtered, evidence-sorted, labeled not-judgment-reviewed — a discovery
feed, never a second buy list); (3) no verdict anywhere may read "user should
verify/decide" — the judgment layer does the verifying and states the outcome.

The user runs the pipeline MONTHLY, not yearly. That changes the semantics of
every verdict: "Timing Buy — wait for trigger X" is ambiguity the SYSTEM must
resolve, because next month's scan re-decides everything anyway. Every report's
judgment sidecar MUST carry a `decision` block, rendered as the first content
section, with exactly two words allowed: **BUY** (with size: FULL/HALF and a
one-line why) and **NO** (with the exact named condition that flips it to BUY
at a future monthly run). Rules:
- A fired trigger is RESOLVED into BUY or NO in the same report — never left
  "pending confirmation". Take the decision; the monthly rerun corrects it.
- Timing Buy / Watch / archetypes remain as internal machinery in Step 4 (do
  not remove those sections) — but the decision box collapses them: a Timing
  Buy whose trigger has fired becomes BUY; one whose trigger hasn't becomes NO
  with the flip condition stated.
- The box also lists the SHORTLISTED CONSTRAINTS (1-3, plain one-line why) and
  ends with a one-line portfolio summary (n positions, sizes).
- An empty BUY list is an acceptable decision; an ambiguous one is not.
- **PORTFOLIO CONSTRUCTION (PMS-grade, Jul-2026):** after the decision block is
  written, run `scripts/stock_report/portfolio_construct.py <judgment.json>
  <data.json>` — it converts FULL/HALF/STARTER into risk-budgeted weights
  (FULL=2u, HALF=1u, STARTER=0.5u) under explicit limits: single stock ≤15%,
  single CONSTRAINT BUCKET ≤40% (same-chain stocks are ONE bet — the 2020-23
  CRGO book was 4 tickers but 1 bet), cash floor 10%; capped-away weight goes
  to cash, never redistributed into weaker names. The decision box renders the
  allocation table. Note: tickers absent from the snapshot's candidate/chain
  mapping fall into their own bucket (conservative for stock cap, but it can
  UNDER-aggregate a real shared bet — when writing the decision, if two
  unmapped buys share a thesis (e.g. two battery names), keep them as one
  basket position ("A + B") so they share units.
- **EXIT RULES (mandatory, per Return Archetype — entries without exits are
  half a system):** every OPEN position is re-tested at every monthly scan and
  a fired exit goes in `decision.exits` [{ticker, action: EXIT|TRIM, rule,
  why}], rendered in the decision box and logged to mg_decisions. The rules:
  - **C (cyclical squeeze / MU-class):** ALWAYS a trade. EXIT when chain capex
    momentum flips positive-to-negative-to-positive cycle completes (supply
    arriving), OR price gives back >30% from its post-entry peak — whichever
    fires first. Never marry a C.
  - **B (op-leverage burst, scheme-funded):** EXIT when the scheme's
    disbursement runway ends (scheme end date/last tranche) or citation trend
    goes fading for 2 consecutive scans with no successor scheme.
  - **A1/A2 (compounders):** hold while the constraint stays Grade A/B+ AND
    the four legs stay intact; TRIM to HALF when the chain state turns
    EXTENDED_CROWDED; EXIT only on constraint resolution (gap closing in
    data), a failed leg (order book stops converting, capex stops), or a
    governance red flag. Multi-year holding is the intent — exits here are
    thesis-failure exits, not price exits.
  - **A3 (late compounder):** TRIM half on any quarter with >50% run;
    remainder follows A2 rules.
  - **E (re-rating + kicker):** EXIT when the re-rating completes (PE band
    reached its historical top) or the kicker event resolves either way.
  - **D (ballast):** exit only on regulatory-cap change; not expected to fire.
  Technicals still never SELECT — the >30% give-back rule on C-archetypes is a
  cycle-position rule scoped to trades that are DEFINED by cycle position.

### Step 2.6 — THE MOONSHOT SLEEVE (40-100x hunting, user mandate Jul-2026)

The user's explicit goal: catch the 40-100x-in-3-4-years class (KERNEX 126x,
GRAVITA 46x, HBLPOWER 12x — the cohort the explosion-fingerprint study was
built on). Be honest about the math and engineer around it: ~1-in-500 stocks
does this per window; NO system picks them reliably one at a time. What works
is a VENTURE-STYLE SLEEVE: fish only where 100x happens, enter at arc-bottom,
size small, hold long. A 10-name sleeve where 2 hit 40x+ and 8 die returns
~8-9x on the sleeve — the discipline is (a) never miss the setup class,
(b) never sell the winners at 3x.

Wired into `select_stocks.py` (Jul-2026) — every IN scan calls
`compute_moonshot_candidates` and the report renders a "Moonshot Sleeve"
section (mechanical, clearly labeled not-a-buy-list, same tier as the PLI
Shortlist — this one IS client-facing per the report/internal-review split,
because candidate names are decision-relevant, unlike audit/review-queue
content). It emits candidates that pass ALL of:
- **evidence-linked universe** (mapped beneficiaries + mandate/PLI-class
  scheme citers — no random smallcaps),
- **small-half turnover** (a largecap can 3x, not 40x),
- **ARC GATE, signal-relative and alias-aware**: price run since the SIGNAL's
  first appearance in the company's own filings (≤1.5x), or since its 2-yr low
  when there's no scheme signal (≤2.0x) — not distance from an absolute low,
  which covid-crash bases distort (the KERNEX lesson: bought at the boring
  bottom, BEFORE the mandate monetizes, measured from when the story starts),
- **BACKTEST-GATED T1 PROMOTION (Jul-2026 v5 — "quality not great except PLI"
  review, the correct diagnosis):** widening the mandate table (BIS-QCO,
  Hallmarking, AIS-140, ALMM) without backtesting each addition individually
  diluted quality exactly as the user predicted. A 19-quarter backtest
  (2022-2026, 349 pick-instances) resolved it BY SUB-FINGERPRINT: KAVACH-class
  (Safety/Compliance Mandate) is genuinely strong (12m/24m/36m median
  +44.8%/+75.3%/+45.1%, n=18-28, KERNEX's 3 entries all +170% to +365% at
  24-36m) — BIS Quality Control Orders is ACTIVELY NEGATIVE (-20.1%/-12.7%,
  n=2-8) — Gold Hallmarking is benchmark-level and inconsistent (median +20%
  but mean +8%, n=10-11). Also found T3-alone (allowed on the strength of the
  single GRAVITA anecdote) is bad in aggregate: median -11.0% at 12m, -80.4%
  at 36m (n=5, dragged by CEREBRAINT/AVROIND) — reverted, T3 needs a pairing
  partner again. LESSON: one validated example (GRAVITA, KAVACH) does not
  justify an acceptance rule for the whole scheme/fingerprint class — backtest
  the full population before promoting.
  **Mechanism (mirrors the novel-vocab auto-graduate/promote/reject workflow):**
  `mg_tracked_schemes.t1_alone_eligible` (boolean, default false) gates
  whether a mandate/quality_gate scheme can make T1 qualify ALONE. New
  mandate schemes enter as `t1_alone_eligible=false` — their hits still count
  as one T1p (provisional) fingerprint, needing a pairing partner like T2/T4,
  same treatment T3 now gets. Promotion to `t1_alone_eligible=true` requires
  a backtest run (`scratchpad`-style: quarterly snapshots, forward returns by
  sub-fingerprint) showing a genuine multi-quarter edge, not a single ticker
  story. Currently promoted: Safety/Compliance Mandate (KAVACH-class) only.
- **T6 + T1 FINAL DESIGN (Jul-2026 v3, walk-forward locked):** the screen's
  two alone-qualifying fingerprints are (a) **T1 mandate signature** — vendor
  language within ~500 chars of the mandate text, OR commitment verbs within
  200 chars of it ("awarded an Order of Rs. X Cr for ... KAVACH" — the literal
  KERNEX filing); gaps must ALLOW periods, since every rupee amount contains
  them (a no-period gap silently killed the KERNEX catch); and (b) **T6
  early-vintage PLI commitment** — per-scheme commitment to a pli_family
  scheme within 36 months of the scheme's launch date (table data; NULL
  launch dates silently demote everything — keep them filled). T6 names get
  arc headroom to 2.5x since-signal because scheme-funded bursts re-rate on
  the commitment news itself (PGEL was ~2x by Dec-2021 and did >10x more).
  Validated: KERNEX @ Dec-2022 (pre-126x), PGEL @ Dec-2021 (pre-50x),
  LUMAXTECH/NEOGEN/SANSERA (the +77-105%/2yr early-PLI cohort) all caught;
  logistics/hotels ("turnaround time"), obligated-payer EPR packaging, and
  \yLED\y-matching-"led" false commits all structurally dead. Weak-scheme
  claimants (food-PLI class) still appear when genuinely committed — the
  scheme-side 5-question scorecard discount is the judgment layer's job, per
  the two-sided funnel.
- **FINGERPRINT PRECISION RULES (Jul-2026, after user review flagged noise —
  "moonshot stocks look crap and same stocks coming every year"):** a bare
  mandate-scheme citation is NOT a fingerprint (it matched "mandatory
  compliance" boilerplate); vendor language counts only WITH a tracked-scheme
  citation; T3 requires the WINNER side (registered recycler/certificate
  holder — the obligated packaging payer is the losing side of formalization);
  T5 turnaround is context-only and never carries acceptance (the old pattern
  matched "turnaround time" and admitted logistics/hotels); candidates in
  mg_manual_exclusions or with >120d disclosure silence are dropped
  (distress ≠ coil). Result of the fix at Dec-2022: 12 noisy names → 3 sharp
  ones (KERNEX still caught). RECURRENCE NOTE: after this fix, a name
  recurring across years at flat arc with fingerprints intact is the FEATURE
  (KERNEX sat at the boring bottom for 2 years — that recurring appearance IS
  the entry window); junk recurring was the bug, and precision, not
  deduplication, was the correct fix.
- **≥2 explosion fingerprints** (T1:mandate+vendor alone also qualifies — it's
  the complete KERNEX/126x fingerprint on its own): T1 mandate(+approved-vendor), T2
  sole-listed-vehicle of a narrow constraint, T3 formalization share-shift,
  T4 capex-outlier-for-its-size, T5 turnaround inflection; plus bonus signals
  ✅commitment and order-book/size ratio.

Judgment rules for the sleeve (decision box gets a MOONSHOT section):
- Every candidate is verified by hand-of-judgment: fingerprints real? quality
  gates (no GENSOL signatures, real revenue base)? scheme scored ≥3/5 on the
  3b-pre scorecard where scheme-driven?
- Sizes: 1-2% each, 5-10 names, STARTER semantics. The sleeve TOTAL ≤10-15%
  of book — it is expected to lose on most names.
- **HOLD RULE: no exit before 3 years except THESIS-FAILURE** (fingerprint
  invalidated, mandate cancelled, governance signature). Price doing nothing
  for 18 months is the EXPECTED path (KERNEX was flat 2020-2022). First
  profit-review only after 10x. Selling a fingerprint-intact moonshot at 3x
  is the defined failure mode of this sleeve.
- Report each candidate with: fingerprints, arc position, what kills it, and
  the explicit "expected ~80% single-name failure" disclosure (PMS clients
  must see the sleeve's venture math, not per-name conviction).
- Walk-forward standard: the screen must retro-catch KERNEX/HBLPOWER (2022,
  pre-run) and GRAVITA-class formalization names — re-validate after any
  detector change.
- **Patient early-investor rule (user standing preference):** the user accepts a
  position doing nothing for a year+ if the thesis is real — they want to be
  EARLY, pre-catalyst. So distinguish two kinds of "not yet": (a) waiting on
  CATALYST TIMING only (evidence is real, the constraint/scheme is validated,
  just unknown when it pays — e.g. a coil forming, a vintage-1 scheme cohort
  with an established ✅committed supplier) → that is a **BUY at STARTER size**,
  not a NO; (b) waiting on VERIFICATION or QUALITY (order book unverified,
  cash-burn survival risk, artifact suspicion, claimant-not-winner) → stays NO.
  Sizes are FULL / HALF / STARTER. Patience covers timing risk, never
  quality risk.
- **MANDATORY ARC CHECK before ANY buy (the HBLENGINE lesson, Jul-2026):**
  every BUY/STARTER line must state the stock's run since its ~3-yr cycle low,
  and that check MUST include renamed-symbol and BSE-era history (HBLPOWER→
  HBLENGINE hid a 12x KAVACH run; AMARAJABAT→ARE&M; GET&D→GVT&D; TINNARUBR's
  Apr-2025 NSE listing hid a completed BSE multibagger). Root cause to guard
  against: citation-intensity screens structurally LAG — a company talks most
  about a scheme AFTER winning and re-rating on it, so a "rising" policy screen
  can surface a story that already paid 10x. A name that already ran ≥5x on the
  same thesis is a late-arc entry (A3 at best) and needs a NEW leg to justify
  any buy — being early on a scheme label is worthless if you're late on the
  stock. This arc check is cycle-position/archetype input (which the user
  explicitly requires), distinct from the banned chart-technicals (200DMA etc.).

### Step 3 — Judgment layer (MANDATORY — this is the product)

The formula is a screen anyone could build. The value of this skill is the reasoning
layer on top. Judgment applies at THREE levels — themes, constraints/policy, stocks —
in that order, because a wrong theme call poisons every stock under it.

**Presentation rule:** the 3a/3b/3c labels below are INTERNAL process names — never
put them in a briefing, report, or judgment sidecar. User-facing headings must be
plain English that stands alone: "Which themes deserve capital", "Stocks the screen
missed", "Government policy check", "Final stock verdicts". Every section heading
should tell a first-time reader what the table means without any legend.

**3a. Theme SELECTION — pick 3-5 by CONSTRAINT QUALITY, reject the rest (MANDATORY)**

The script surfaces ~30 themes; most don't deserve capital. Selection is decided by
the quality of the underlying constraint — the physical/economic reality — NOT by
past returns and NOT by the strength score. Keep **physical quality** separate from
**evidence completeness**: `UNMEASURED` means the as-of record lacks the quantified
supply/import leg; it is not a verdict that the constraint is weak.

**Constraint-quality grade:**
- **Grade A (select)** — all four:
  1. *Quantified gap*: demand vs domestic capacity with numbers (`capacity_gaps`:
     gap_pct, target_year) or named import dependence with concentrated origin
     (`import_dependencies`) — not just filings saying "shortage".
  2. *Hard resupply barriers*: qualification/certification cycles, technology moat,
     multi-year capex lead time, land/grid/raw-material access. Ask: if prices
     doubled tomorrow, how fast could new supply arrive? >2 years = hard barrier.
  3. *Binding NOW*: order books stretching, pricing power, rising imports despite
     duties, capacity-utilization stress in beneficiaries' own filings — current
     evidence, not projections.
  4. *Certain demand side*: contracted / policy-mandated (PLI targets, RDSS rollout,
     defense procurement pipeline) or structural (grid replacement cycle) — not
     cyclical hope.
- **Grade B (selectable with a stated catalyst)** — real constraint but one leg
  soft: gap quantified but resolving (announced capacity commissions within ~18
  months), or binding evidence mixed, or demand cyclical. Needs a 1-2 quarter
  confirmation catalyst (policy deadline, commissioning date, tender award) to be
  selected; otherwise Watch.
- **WEAK (reject for Core Buy)** — a physical measure exists but binding demand or a
  resupply barrier is not proved. This is a genuine weak-quality verdict.
- **UNMEASURED (not Core or Early/Timing)** — the theme
  exists in contemporaneous mapper/company filings but no point-in-time quantified
  gap or import observation has been captured. Seek measurement before Core sizing;
  do not relabel the constraint weak or erase a company-led opportunity. It cannot
  enter the physical-constraint Core or Early/Timing lane. It may enter only the
  separately labelled Discovery Starter lane when dated binding-demand evidence plus
  either structured constraint evidence or exact-role catalysts from at least two
  independent producer-ledger companies agree. Each stock also needs an exact operating/pipeline
  producer-ledger role, a same-product catalyst, and NORMAL risk. Mapper-only and quarantined
  roles have no position authority. Cap producer-ledger names at 1.0% when unmeasured,
  five names and 5% aggregate. Revalidate quarterly over a 6–12 month discovery window;
  graduate to Early/Core on new evidence or exit.
  NLP artifacts (labels
  like "X: Constraint from ESG/FDA/Real Estate Demand" — name the real chain or
  discard; a beneficiary list dominated by services/software names in a hardware
  constraint is an artifact). Missing structured coverage lowers certainty; it is
  never a generic reason to erase a dated company-led opportunity.

**Layer type decides the playbook (`layer_type` in `chain_capex_momentum`)** —
grade the constraint first, then read its layer:
- **differentiated** (CRGO, transformers, defense electronics, semis): qualification
  barriers protect margins. Entry signal = chain capex RISING (`momentum > 0`) —
  capacity converts to revenue at protected prices. These are the multi-year
  compounders (CRGO cohorts: +198/+218/+252% over 2-yr holds, three years running;
  stay in until the constraint resolves — rotating early was the historical mistake).
- **commodity** (solar cell/module/wafer, battery cells, optical fiber, memory-type):
  anyone can add capacity, so the constraint forms via industry capex CUTS +
  a demand inflection — the sign INVERTS. `momentum < 0` on a crushed commodity
  cohort = the COILING setup (solar Dec-2022 → +119% in 2023; US memory Dec-2022 →
  flat 2023 → +238-651% in 2024-25). These are TRADES: mandatory exit trigger
  (capacity restarts / spot-price rollover), never multi-year compounders
  (WEBELSOLAR: +579% in 2023, −95% in 2024).
- **service_manufacturing / epc_cyclical** (EMS, PCB, rolling stock, T&D EPC):
  revenue rides the theme, margins are bid away — scarcity rent accrues elsewhere.
  Never Core; Satellite only with margin evidence.

**T-COIL timing sleeve (theme-entry timing, distinct from Core/Satellite):**
eligibility = theme graded A/B (evidence); trigger = capex momentum with the
correct sign for the layer (differentiated: rising; commodity: cutting + a named
demand catalyst). The chain price state (`chain_technical_state`) only sizes and
prioritizes entries — it never decides eligibility (technicals rule). The fiber
sequence is the template: Dec-2024 de-rated but capex momentum negative → wait
(avoided −34%); Dec-2025 momentum turns +29 → enter → +222%.

**Chain-continuity alarms (`chain_continuity_alerts`) — MANDATORY check:** a chain
whose mapping went stale (>150 days without fresh beneficiary rows) has VANISHED
from the pipeline, not resolved. The solar lesson: solar chains had no 2023-25
snapshots and the +579% solar year happened in the blind spot; transformer rows
stopped after Dec-2025 the same way. For every alerted chain: state it in the
briefing, keep its last-known grade alive, and treat it as top-priority manual
coverage work.

**Cohort de-dup (`chain_cohort_duplicates`) — MANDATORY check before counting
confirmations:** two constrained_product labels can be the SAME underlying
company cohort under different NLP names — proven Jul-2026: EMS/PCB/
Semiconductor IC shared 100% identical tickers in one snapshot, and (via a
second table) "electrical equipment: Demand Surge" turned out to be the exact
same companies as "transformer: Demand-Supply Tension" (APARINDS, CGPOWER,
GVT&D, KEC, TARIL — not a second discovery, one signal counted twice). Any pair
flagged here (≥70% ticker overlap) must be MERGED before treating them as
independent evidence for a theme, a cross-theme-overlap score, or a "multiple
confirmations" claim.

**Multi-vintage cohort durability (`chain_cohort_explosiveness`) — the
"did this constraint actually go crazy, broadly" test.** For chains with ≥2
years of history, this computes whether the WHOLE beneficiary cohort (not one
lottery winner) produced a broad win (median 2yr return >50% AND ≥40% of the
cohort >100%) at EVERY tested entry vintage, not just one lucky year.
`durable: true` is rare and decisive — in the 2020-2026 backtest only CRGO
Steel/Transformer passed it (every vintage: median 2yr +79% to +136%, 50-61%
of the cohort >100%); Defense electronics decayed vintage over vintage
(+57%→+12%→+4%) despite similar order-book/capex-signal counts, and Solar's
3-year number collapsed between vintages (+64%→+18%) — the commodity
round-trip signature. Weigh `durable: true` heavily toward Core; a chain that
never passed with ≥2 vintages available is a single-cycle or decaying theme
regardless of how good this quarter's evidence looks.
**Anti-patterns proven NOT to discriminate — do not use these as quality
signals**: order-book flag (saturated near 100% across almost every chain in
recent snapshots — no longer informative); raw capex-signal COUNT compared
across chains (Defense averaged MORE capex signals than CRGO and still
decayed — only the momentum DIRECTION inside the correct layer type matters,
via `chain_capex_momentum`); "scarcity ratio" of listed suppliers ÷ companies
discussing the constraint (Defense had a smaller ratio than CRGO and still
underperformed — mapping QUALITY, not a ratio, is what matters).

**Narrow + fresh forward-discovery candidates (`narrow_fresh_candidates`) —
review every scan.** Derived from theme breadth/stage already in the JSON:
themes narrow enough (≤60 mapped companies) and early-stage enough (≤10
confirmed quarters, Emerging/Accelerating/Hidden Formation) to resemble what
CRGO Steel looked like in Dec-2020 (23-28 companies, early stage) BEFORE
anyone had run a cohort study to notice. This is a discovery list, NOT a
grade — a candidate here still needs the full constraint-quality grade
(quantified gap, layer type, real vs artifact cohort) before it's investable.
Flag the single best-fitting candidate per scan as the top manual-mapping
priority, the same way chain_continuity_alerts flags coverage regressions.

**Then, for selected themes only, two overlay checks** (they shape HOW to play it,
not whether the constraint is real):
- **Monetization**: WHICH value-chain layer captures the scarcity economics, and is
  there a listed pure-play on that layer? A Grade-A constraint with no investable
  pure-play is analysis, not an opportunity — say so instead of force-fitting the
  nearest conglomerate. Also: does scarcity become supplier margin, or does the
  buyer/regulator cap it (regulated tariffs absorb the rent)?
- **Chain price state (`chain_technical_state`) — POSITION SIZING ONLY, never
  selection**: pct_above_200dma + median distance from 52-wk high across the
  mapped cohort. This field must NEVER change a grade, a verdict, or which
  stocks are selected — the user's standing rule is that technicals do not
  influence selection, and the backtest agrees (above-200DMA showed no alpha
  edge). Use it for one thing: sizing/entry notes on already-selected names
  (DERATED cohort = full-size entries available; EXTENDED = stagger/half-size).
  Report it as context, not as a reason.

`theme_track_record` is CONTEXT ONLY — never a selection input. A high-quality
constraint with poor past returns is often the early entry (the market hasn't paid
it yet); a well-paid track record can mean exhausted. Use it for one thing:
calibrating HOW the theme paid historically (which layer, which phase), and say
explicitly when your grade disagrees with the track record and why.

Also apply: **dedup** (five utility-variant themes are ONE theme — merge before
counting breadth/overlap); **contradictory pairs** (a "Capacity Gap" and an
"Overcapacity Risk" theme on the same product refer to different value-chain layers
— never let a stock ride both as two bullish signals); **coverage gaps**
(`chain_coverage`: a fresh Accelerating theme with NO mapped supply chain means the
candidate list is blind there — flag it as where manual work is most valuable);
**unmapped peers (`unmapped_industry_peers`) — MANDATORY review**: for every
SELECTED theme, this field lists companies whose own filings repeatedly mention
the constrained product but that the beneficiary mapper never linked (the
Voltamp/TARIL failure mode — pure-plays often file dedicated documents that never
co-occur with theme documents, so the extractor misses exactly the best-fit
names). Review each: pure-play on the constrained layer → bring into 3c
categorization alongside screened candidates (mark it "unscreened — no
conviction/order-book fields; verify externally"); unrelated mention → dismiss
with a word. Never present a selected theme without checking this list.

**Verdict table format** (mandatory in the briefing):
| Theme | Grade | Verdict | Deciding evidence |
The deciding evidence names the strongest constraint-quality fact for selects
(quantified gap / barrier / binding signal) and the failed leg for rejects
(unquantified / resolvable / not-binding / artifact / no-pure-play). Stocks in 3c
may only come from selected themes — any exception must say why it overrides the
theme verdict.

**3b. Constraint & policy judgment (India-specific)**:
- **Binding or narrative?** A constraint is binding when: import dependence with
  concentrated origin + qualification barriers + capacity_gap quantified. It is
  narrative when it only appears in theme labels. Say which.
- **Resolution risk**: every constraint carries its own death date — announced
  capacity additions resolve it. If beneficiaries' own capex WILL resolve the
  constraint in ~2 years, the trade has a clock on it; say so.
- **Policy read (`policy_evidence` per candidate)**: which schemes back the chain —
  PLI, ALMM, BCD, RDSS, FAME, PM-KUSUM, PM Surya Ghar, Semicon Mission, ACC, iDEX.
  A candidate whose OWN filings cite a PLI allocation/ALMM listing has revenue
  visibility the formula can't see — upgrade evidence quality. Zero policy mentions
  in a policy-driven chain = the company may be a bystander to the policy story.
- **Policy cuts both ways**: PLI-funded buildout CREATES tomorrow's overcapacity
  (module PLI → "Solar Module Overcapacity Risk"); BCD protection can vanish in a
  budget; ALMM enforcement can be deferred. For each policy-backed theme name the
  single policy reversal that would kill it.
- **Beneficiary vs claimant**: a filing *mentioning* PLI ≠ *winning* an allocation.
  Read the latest_title; "approved under PLI" beats "expects to benefit from PLI".
- **PLI report pipeline**: client-facing PLI output is a research workflow, never a
  ranked shortlist. A visible row needs a dated company filing, a stated stage
  (application / award / capacity-capex / operating), an exact constrained-product
  phrase in the same local disclosure as the policy/action, a primary source passage,
  and materiality classified as product capacity, company/project capex, scheme-wide
  amount, or unqualified. An award, scheme outlay, or generic PLI mention is never
  company capacity. Promote to `Research now` only after two independent dated
  product-linked capacity/capex events; otherwise route it to `Monitor milestone`.
  Keep credible but unlinked policy activity as an internal new-theme monitor, and
  count independent dated evidence families rather than repeated promotional releases.
  PLI also belongs in the constraint's **resolution clock**: funded new capacity can
  create an early-company lead while shortening the shortage's investment window.

**3b-pre. SCHEME-FIRST PLAYBOOK — evaluating a NEW scheme at announcement and
predicting beneficiaries BEFORE any filings exist (the forward method).**
Derived from measured outcomes of every PLI-era scheme 2020-2026. When a new
scheme/mandate is announced (budget day, cabinet approval, ministry
notification — the day-0 sources are OUTSIDE the filings corpus: PIB releases,
gazette; check them in the monthly ritual), score the SCHEME first on five
questions, then predict the winners:

SCHEME QUALITY — five questions (answerable on announcement day):
1. **Does it substitute concentrated imports?** The single best predictor.
   Import-substituting schemes paid (electronics/white-goods/telecom PLI:
   Dixon +360%, Amber +293%, PGEL +196% over 2yrs; pharma-API +207%);
   domestic-oriented schemes disappointed (food-processing −8%, chemicals
   −32%). Cross-check `import_dependencies` for the product.
2. **Is it paired with a trade/demand barrier?** Scheme + BCD hike / ALMM-type
   approved-list / trusted-source rule / compulsory mandate = the winners'
   common fingerprint. A subsidy without a moat leaks to price cuts.
3. **Does the incentive reward INCREMENTAL PRODUCTION** (% of incremental
   sales — strong) or just capex/interest (weak)? Production-linked structure
   forces output growth into protected demand.
4. **Outlay vs sector size**: outlay meaningful relative to the sector's
   annual profit pool, or symbolic?
5. **Do eligibility thresholds favor listed incumbents?** Minimum-investment /
   existing-capacity criteria pre-select the established #1-#3 domestic
   players — which is exactly who the vintage rule says to buy.

BENEFICIARY PREDICTION (day 0, zero filings needed): from the scheme's target
product, list the established domestic top-3 listed manufacturers via
classification data (`security_master` industry) + existing capacity; they
are the probable allottees (2021 proof: Dixon/Amber/Havells/Blue Star were
predictable from white-goods eligibility criteria months before committing).
Then confirm through the existing ladder as evidence arrives: ministry
allottee lists (public PDFs — manual monthly check, not yet ingested) →
commitment disclosures (`✅` detector) → citation intensity → vintage/cohort
quality. ARC-CHECK every predicted name before any buy. Enter in scheme years
1-2 with established-incumbent cohorts only; stand down when the citing
cohort turns IPO/microcap-heavy.

KNOWN DATA GAP (say it in briefings until fixed): day-0 scheme announcements
and ministry allottee lists live on PIB/gazette/ministry sites, which the
document pipeline does NOT ingest — those two checks are manual monthly steps
for now, and PIB ingestion is the highest-value pipeline addition for this
playbook.

**THE TWO-SIDED FUNNEL — how scheme-side and company-side signals meet.**
The playbook above (3b-pre) is TOP-DOWN: score the scheme, predict winners from
eligibility + classification. The detectors below are BOTTOM-UP: what companies
themselves say in filings, as a five-rung ladder of increasing conviction —
1. `novel_policy_vocabulary` — a NEW scheme word bursting before we know it
   (KAVACH, dictionary-free)
2. `policy_early_pings` — 1-3 mentions of a known scheme (Shakti-2022 tier,
   watch-only radar)
3. **✅ commitment disclosure** — "applied for / approved under" phrasing,
   single-doc, same-day winner confirmation (Dixon Apr-2021, Aarti Jun-2020)
4. `policy_beneficiary_screen` — ≥4-doc citation intensity + 12-month trend
5. vintage/cohort-quality + fading-cohort exit
CROSS-CONFIRMATION RULE: the two sides validate each other. A scheme-side
PREDICTED beneficiary that then ✅commits in its own filings = the highest-
confidence policy signal in the system (predicted → confirmed). The inverse
catches the trap: a company loudly citing a scheme the 5-question scorecard
rated ≤2/5 is a claimant marketing a weak scheme (GREAVESCOT/FAME,
Foods & Inns/food-PLI) — company-side noise never overrides scheme-side
quality. And EVERY name from either side passes the arc check before any buy.

**3b-bis. Policy-explosion discovery (`policy_beneficiary_screen`) — MANDATORY
review for India.** This is the SECOND discovery engine, independent of theme
chains: for each scheme (PLI, KUSUM, ALMM, RDSS...) it lists companies whose OWN
filings cite the scheme intensively, with a 12-month trend. The Dixon/PGEL/Amber
lesson: PLI winners announced themselves in their own filings YEARS before any
theme chain mapped them (PGEL Dec-2020, Amber Aug-2020, Dixon Apr-2021); this
screen at Dec-2022 returned avg +87%/2yr. Review rules: (1) rising trend +
supplier-classified industry = candidate for 3c categorization even with NO
theme-chain membership — label archetype B (scheme-funded operating leverage,
exit clock = scheme life); (2) winner-vs-claimant is the gate — "allocated/
approved under" beats "expects to benefit", and mentions without order
conversion is the GREAVESCOT trap; (3) a scheme whose whole cohort is fading =
the scheme story is ending — exit-review any B-positions riding it.
(3b) **Early pings (`policy_early_pings`) — watch-only radar, never a decision
input**: names below the evidence threshold (1-3 scheme mentions) with recent
activity, freshest first. Purpose: a Shakti-2022 (2 KUSUM docs) stays on the
radar for the months it takes to qualify. In briefings: one compact line per
scheme, clearly marked "below threshold". A ping graduating into the qualified
screen IS reportable news.
(3c) **Thin-filing enrichment**: run
`scripts/stock_report/enrich_thin_filings.py --watchlist` (and periodically
`--all --max-docs 500`) BEFORE the monthly scan — it extracts attachment-PDF
text into raw_text so scheme/evidence regexes can see what cover letters hide
(~15k thin IN docs; the reason Shakti's KUSUM attribution was invisible).
(3d) **Novel-vocabulary review (`novel_policy_vocabulary`) — MANDATORY every
monthly scan (the KAVACH lesson).** Scheme regexes only contain words already
known — KAVACH was detectable Aug-Sep-2022 (KERNEX at ~₹250, HBLPOWER at ~₹98,
both pre-10x) but the pattern for it was only written in 2026 after studying
the winners. This field lists capitalized terms BURSTING from zero in policy-
context filing windows, dictionary-free. It is ~90% noise BY DESIGN (OCR
fragments, signatory names) — the review is SEMANTIC, not lexical: scan the
list, discard the obvious junk in seconds, and investigate any term cited by
2+ companies from a coherent industry cluster (the validation case: "KAVACH —
KERNEX + HBLENGINE", two rail-safety suppliers, mid-2023). A real find gets
(a) promoted into INDIA_POLICY_SCHEMES as a named pattern, and (b) its citing
tickers arc-checked immediately — the whole point is to be at the ₹98 end of
the curve, not the ₹1,099 end.
**Review ALL terms, never a truncated head (the ECMS lesson, Jul-2026):** on
the 31-Mar-2026 scan, "ECMS" (Electronics Components Manufacturing Scheme,
₹22,919cr, the next PLI-class electronics wave) sat at #11 of 60 with a
textbook good-vintage cohort already citing it (AMBER ×10 docs since May-2025,
SYRMA, PGEL, EPACK, UNOMINDA, MOTHERSON — established incumbents, not IPO
tourists) — and was missed for months because only the top-8 terms were
eyeballed. The detector's recall worked; the review's coverage failed. Rules:
(i) the judgment layer reads the FULL term list from the data JSON's
`novel_policy_vocabulary` field every scan — NEVER rendered into the
client-facing report (PMS zero-judgment rule, Jul-2026: the user should never
see machine review-queue noise); (ii) the monthly review covers every term —
60 terms takes under a minute of semantic scanning; (iii) a graduating scheme
is promoted in the DATABASE, never in code — see the no-hardcode layer below.

**NO-HARDCODE REFERENCE LAYER (user rule, Jul-2026 — "no hardcoded constraints,
PLI schemes or stock names at any stage"):** every entity the pipeline uses is
DB data or computation, maintained by the judgment layer with SQL, never code
edits. The tables (all in makrograph):
- `mg_tracked_schemes` (scheme_name, pattern, scheme_class, status) — the
  policy-scheme dictionary. Novel-vocab terms meeting the objective bar
  (≥3 tickers, ≥3 docs, zero prior-12m, alphabetic) AUTO-INSERT as
  status='auto_candidate' on live scans; the monthly judgment review promotes
  (`UPDATE ... SET status='active', scheme_class='pli_family'|...`) or rejects
  (`status='rejected'`). PLI-family membership = scheme_class='pli_family'.
- `mg_manual_exclusions` (ticker, reason) — fraud/enforcement exclusions.
- `mg_chain_classifications` (chain_key, layer_type, rationale) — the
  differentiated/commodity/service/epc layer map; new chains surface as
  "unclassified" and the judgment layer INSERTs a classification with
  rationale.
- `mg_symbol_renames` — AUTO-DETECTED from bhavcopy price-series continuity
  (old series ends, new begins ≤7 days later, price+volume continuity,
  mutual-best match; symbol-LCS similarity upgrades to confidence='high', and
  widens the volume-ratio floor to 0.15 since a genuine rename can see a
  transient liquidity dip before price continuity even matters — the
  HBLPOWER→HBLENGINE case sat at vr=0.22, missed for months by a flat 0.25
  floor until traced and fixed). High-confidence pairs feed price_symbols()
  automatically; medium-confidence pairs sit in `reference_data.
  symbol_renames_pending_review` in the data JSON — confirm/reject via UPDATE.
  This found HBLPOWER→HBLENGINE, GET&D→GVT&D, AMARAJABAT→ARE&M,
  MINDAIND→UNOMINDA, STRTECH→STLTECH and dozens more with no dictionary.
- Market proxy: computed top-25 by trailing-12m traded value, point-in-time.
All of this (tracked schemes, graduation candidates, pending renames,
exclusion proposals, mapping-artifact suspects) lives in the data JSON's
`reference_data` / `scheme_graduation_candidates` / `exclusion_proposals` /
`mapping_artifacts` fields — read directly by the judgment layer as an
internal monthly checklist (see `run_monthly.py`'s end-of-run printout).
**NONE of it renders into the client-facing report** — the user should never
need to parse machine review-queue content to act on a report (PMS
zero-judgment rule, Jul-2026). Seeds in code (`_SEED_*`) are one-time
migrations for empty tables only.

**Leading-indicator sources (Jul-2026, "greatest PMS" push) — same
judgment-review-only treatment, same reason: neither is a scored theme with
beneficiaries, both are corroborating signal the judgment layer weighs
before promoting or dismissing a theme.**
- `regulatory_watchlist` — `scripts/policy/ingest_pib.py`'s `stage` column
  (draft/notified/unclear) now catches pre-finalization language ("draft
  notification", "for stakeholder consultation", "invites comments",
  "in-principle approval") that the original 5 keyword families missed
  completely. A draft QCO or PLI amendment is public 2-4 quarters before any
  company's filing mentions compliance — nothing is binding yet, so nobody
  discloses it. `compute_regulatory_watchlist()` surfaces `stage='draft'`
  rows, point-in-time, as a watchlist. A draft can be watered down or
  dropped — never treat it as an investable theme by itself, only as an
  early flag to watch the *notified* mg_policy_announcements feed for.
- `trade_momentum_signals` — `scripts/policy/ingest_trade_flows.py` pulls
  real monthly India import data (free UN Comtrade "preview" endpoint) for
  every HS code already in `mg_import_dependencies` (no hardcoded HS-code
  list — add a row there to track a new one). `compute_trade_momentum_signals()`
  compares recent-half vs prior-half average import value per HS code and
  flags widening/narrowing/flat trend. This is monthly customs reality, not
  a company's self-reported quarterly disclosure — a widening trend here
  typically precedes a filing mention of the same constraint by 1-2+
  quarters. HS codes are commodity-level, not 1:1 to a company, so this
  confirms a constraint's trajectory — it never names a beneficiary by
  itself; cross-reference against `constrained_products`/`import_dependencies`
  to see which existing theme it corroborates.

(4) **VINTAGE RULE (walk-forward validated):** policy schemes pay like vintages.
Years 1-3 of a scheme wave, when established manufacturers commit
(Dixon/Amber/Havells class), returned +77%/+105%/+52% (2021/2022/2023 screen
cohorts, 1-2yr). By the time the citing cohort is dominated by fresh IPOs and
microcaps, the wave is late — the 2024 cohort (OLAELEC, EPACK, SADHNANIQ,
SIGACHI class) averaged −32% the next year. Judge cohort QUALITY: established
suppliers committing early = enter; IPO/microcap-heavy cohort = stand down,
whatever the intensity numbers say.

**3c. Per-stock categorization** — categorize the top ~15 candidates YOURSELF by
reading the full evidence per stock — do not just relabel the formula tiers.

**Per-stock evidence read** (all of it is already in the JSON — no new DB queries):
- **Evidence quality**: order-book signals vs pure narrative mapping; how many filings
  back the mapping (`signal_count`); is it a single-signal mapping? Check
  `evidence_dashboard` confidence for its themes.
- **Theme phase & timing**: `freshness_label` + confirmed quarters of each theme it
  rides. Fresh/accelerating themes are where returns live; consensus themes are where
  drawdowns live. A stock riding one fresh theme beats one riding three consensus themes.
- **Position in the chain**: direct capacity owner > critical supplier > input
  supplier >> demand-side name that leaked into a supply theme (call these out as Avoid).
- **Crowdedness — evidence-based only**: how long has the market known (confirmed
  quarters, consensus freshness), how broadly is the name mapped/discussed. Price
  action (52w-high distance, 200DMA) is NOT crowdedness evidence and must not
  influence the category — technicals affect position sizing only.
- **Governance/risk**: read the actual `risk_events` text, not just the tier. Auditor
  resignations, CIRP, SEBI actions, promoter pledge spikes = Avoid regardless of score
  (the GENSOL lesson: 0.850 composite → −95%).
- **Cross-theme role**: is the overlap count real (structural chokepoint spanning
  independent chains) or an artifact (mapped everywhere because every filing mentions it)?

**Explosion fingerprints (from the 50-100x cohort study — India, 2020-26).**
Five repeatable, non-generic mechanisms behind every 40x+ theme/constraint name;
check candidates against these before settling for a lesser thesis:
1. **Mandate × approved-vendor oligopoly** (KERNEX 126x — KAVACH): regulation
   makes a product compulsory AND certification gates the vendor list to a
   handful. Detect: "Safety/Compliance Mandate" scheme pattern + approval
   disclosures + short vendor list. The 10x came AFTER the first filing mention.
2. **Sole-listed-vehicle on a scarce input** (E2E 116x — GPU cloud; XPROINDIA
   66x — capacitor film; BORORENEW — solar glass): a demand shock or import-lock
   on an input with exactly ONE listed pure-play. Detect: import_dependencies
   concentration + peer/industry search returning a single listed maker →
   listed-monopoly flag.
3. **Formalization share-shift** (GRAVITA 46x — Battery Waste/EPR rules):
   regulation criminalizes informal supply; organized listed players inherit
   volumes. Detect: "EPR/Formalization" scheme pattern + recycler industry.
4. **Micro-cap relative-capex leader inside a Grade-A complex** (PITTIENG 42x —
   laminations; QPOWER today): capex signals every single year at tiny size, one
   layer adjacent to the A-chain. Already covered by four-leg + relative capex —
   extend the below-cutoff review to adjacent-layer microcaps.
5. **Turnaround × constraint stack** (CGPOWER 41x post-fraud; JAIBALAJI,
   V2RETAIL): control change/deleveraging INTO a binding chain — the E×A
   archetype stack. Detect: resolution-plan/new-promoter filings + chain
   membership.
Anti-pattern reminders from the same screen: corporate-action data glitches
masquerade as multibaggers (BRITANNIA "198x" = bonus artifact — verify with
splits), and C-class round-trips look identical on the way up (WEBELSOLAR 90x
peak → −95%).

**Conviction hierarchy — the no-mediocrity rule (user standing instruction).**
The user is here for multi-year compounders, not mediocre diversification. A
Core Buy must satisfy ALL FOUR legs of the explosion profile:
1. **Supplier position**: pure-play or dominant-segment supplier to a Grade-A/B+
   constraint. Corroborate with the classification data (`industry` /
   `industry_detail` on each candidate; e.g. "Heavy Electrical Equipment" for a
   transformer chain). Conglomerates with diluted exposure are NEVER Core.
   US variant: "owns OR has contractually secured" the constrained capacity —
   US scarcity rent often accrues to fabless designers who prepay for capacity
   (the NVDA/AVGO class fails a literal owns-capex test but passes this one).
   US layer map must include the storage sub-layer (SIC "Computer Storage
   Devices": WDC/STX/SNDK) alongside semis/equipment/utilities/networking —
   it was the missed memory-squeeze cohort (+238-651% in 2024).
2. **Own capex expansion underway** (`capex_signals` on each candidate): the
   company is adding capacity INTO the constraint — that is the operating-leverage
   leg that turns scarcity into an earnings explosion. High conviction + zero
   capex signals = a price-taker, not a compounder. Judge capex intensity
   RELATIVE to that year's scan (top quartile of the candidate list), never as
   an absolute number — the filings corpus grows over time (21k docs/yr in
   2020-22 vs 30k in 2025), so absolute thresholds unfairly starve early years
   (the 2022 GENUSPOWER lesson: capex=4 looked weak, was top-half for that
   year's evidence base, and the stock did +173%).
3. **Multi-year runway**: the constraint's resolution clock is ≥2-3 years out
   (qualification barriers, import-substitution horizon, capacity lead times).
   A thesis that resolves in 1-2 quarters is a trade, not a Core Buy — park it
   in Timing as tactical, never as a headline pick.
4. **Earnings-explosion path visible**: order-book evidence + capacity growth
   compounding each other (orders filling capacity as it lands). State the
   mechanism in one line — if you cannot, it is not Core.
Names failing any leg are at best Timing/Watch. **Prefer 1-2 exceptional names
over five mediocre ones — an empty Core list is an acceptable output.** Entry
triggers on Timing names are for validation; the hold thesis must still be
multi-year or the name does not belong in the briefing at all.

**Return archetype (MANDATORY on every Buy/Timing/Watch name)** — label each pick
so the user can calibrate expectations. Derived from layer type × legs × cycle
position × how much is already paid; bands are historical analogs, never promises:
| Archetype | Profile | Historical analogs |
|---|---|---|
| **A1 Early compounder** | Differentiated, four legs, constraint young, entry unpaid — 10-40x over 3-5 yrs, hold through drawdowns | APARINDS '20 (+3,961%), POWERINDIA '20 |
| **A2 Mid-life compounder** | Same chain, entered after the first re-rating — 2-4x over 2-3 yrs | POWERINDIA '23 (+469%), CGPOWER '26 |
| **A3 Late compounder** | Proven but 10x+ already — 50-150%, add only on new legs | POWERINDIA '26 |
| **B Operating-leverage burst** | Funded demand + margin inflection — 2-5x over 2-3 yrs, runway = scheme life | GENUSPOWER '21-25 (+436% 3yr) |
| **C Cyclical squeeze (MU-class)** | Commodity coil → catalyst → parabola → give-back — 2-8x in 12-24 mo, TRADE with written exit | MU '24 (+238%), WEBELSOLAR '23 (+579% then −95%) |
| **D Ballast compounder** | Regulated/capped rent — 30-80% multi-yr, low drawdown, portfolio floor | XEL, utility basket |
| **E Re-rating + kicker** | Discount unwind stacked on constraint exposure — 2-3x | JCI '24-26, INTC on foundry proof |
Expectation discipline: never sell an A-class for C-class behavior or hold a
C-class like an A-class — most historical losses came from archetype confusion
(holding WEBELSOLAR like a compounder; trading POWERINDIA like a cycle).
The user-facing definition of every archetype lives in
`data/reports/return_archetypes_reference.pdf` (+.html) — link it whenever
archetype labels appear in a briefing or report, and keep it updated if the
archetype set ever changes.

**Output categories** (yours, not the script's):
| Category | Meaning |
|---|---|
| **Core Buy** | All four explosion legs present — act now per position guidance |
| **Timing Buy** | Thesis right but one EVIDENCE confirmation missing (order print, policy enforcement date, capacity commissioning, margin turn, external verification for unscreened names) — name the trigger. Chart position is NEVER the trigger |
| **Watch** | Promising but thin evidence (single-signal, low theme confidence) — name the trigger that would upgrade it |
| **Avoid** | Governance risk, consensus+crowded, demand-side leak, or noise mapping — say which |

For every categorized stock give three one-liners: **Why now** (or why not),
**What kills it** (stock-specific, from bear_cases/risk_events — not boilerplate),
and **Conviction** (High/Medium/Low with the single strongest piece of evidence).

**Triggers must be machine-checkable (MANDATORY for Timing/Watch names)**: every
Timing Buy and Watch verdict's trigger gets an entry in
`data/reports/trigger_watchlist_IN.json` — ticker, trigger description, a filing
regex pattern, and an action line (what to confirm before buying). The user
detects fires by running `scripts/stock_report/check_triggers.py [--since DATE]`,
which scans newly ingested filings and prints FIRED (with matched filings +
action) or quiet, checkpointing between runs. A trigger nobody can detect is not
a trigger. Alert freshness is bounded by mg_documents ingestion freshness — say
so when relevant.

**Divergence table (MANDATORY)**: formula rank/tier vs your category, with a one-line
reason wherever they differ. This is where the intelligence shows — a rank-9 stock on a
fresh theme promoted to Core Buy (the STLTECH lesson: rank 9, fresh, +293%), or a rank-3
consensus name demoted to Watch. If you have zero divergences, you haven't analyzed —
you've echoed the formula; look again, especially at ranks 6-12 with fresh freshness.

**Backtest priors** (weigh them, don't obey them): fresh themes outperform consensus;
order-book evidence is the most honest signal; HIGH risk tier → out, always; hit
rates ranged 28%-80% by year, so in weak-breadth regimes prefer fewer Core Buys over
forced five.

**Technicals rule (user standing instruction + backtest-confirmed): price action
never influences selection.** No stock may be selected, demoted, promoted, or
categorized because of its chart — not 200DMA position, not distance from 52-wk
high, not 6-month return, not "broken chart" or "extended". The backtest agrees:
above-200DMA showed NO alpha edge, and the HFCL miss (T3_Ignore at −42% from high,
then +216%) shows chart-based demotion destroys evidence-based calls. Technicals
appear in exactly ONE place: `position_size_guidance` (full/half sizing on
already-selected names). If a name's only weakness is its chart, it is a Buy at
the evidence-implied category, with sizing per guidance.

**Point-in-time discipline for judgment**: reason ONLY from evidence in the JSON plus
general industry structure knowable before the as-of date. Never let knowledge of what
actually happened after the as-of date leak into a historical categorization — if you
catch yourself thinking "this one went up later", discard that and re-derive from the
evidence. State this discipline once in the briefing for historical dates.

## Hard rules

- No data dated after the as-of date, ever. The script enforces this; don't add
  DB queries without a `<= as_of` filter.
- Judge every gate on one exact constraint-product-company tuple. Unrelated failed
  mapper tags or adjacent product mappings cannot veto or validate that tuple.
- The final investable shortlist is the only action list. If empty, say
  `NO ALLOCATION FROM THIS STRATEGY`; do not promote Raw, PLI, policy,
  producer-universe, or quarantine rows to fill a pick count.
- Apply judgment on top of scores: broad noisy themes (100-relevance breadth themes),
  single-signal mappings, and thin tickers (technical=null) should be called out, not
  hidden. The score is a screen, not the conclusion.
- If two candidates tie, prefer: order-book evidence > conviction > breadth.
- DB: Postgres `makrograph` only (localhost, user postgres).
- Dates DD-Mon-YYYY in prose; keep the briefing scannable (tables + short lines).
