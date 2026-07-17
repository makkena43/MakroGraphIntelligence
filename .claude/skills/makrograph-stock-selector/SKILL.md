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

### Step 1 — Extract

```bash
.venv/bin/python scripts/stock_report/select_stocks.py --as-of <YYYY-MM-DD> [--country IN|US]
```

Run from project root; prints the JSON path (data/reports/stock_selector_<country>_<date>_data.json).
Default emergence window is 12 months (`--window-months 6` for a tighter scan).
**US mode** (`--country US`): constraints come from bottleneck themes in the theme
graph (no constrained-product mapper / capacity-gap / import tables for US), and
there is NO technical overlay (no US price data in DB) — leave price/technical
columns out entirely for US candidates and point to finviz/stockanalysis for chart
checks. See `us_data_note` in the JSON.

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
contain: `evidence_dashboard`, `cross_theme_overlap`, `bear_cases`, and each candidate
must have `scoring_breakdown`. Use `jq '.evidence_dashboard[:3]'` to confirm.

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
past returns and NOT by the strength score. Grade every major/emerging theme A/B/C:

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
- **Grade C (reject)** — asserted, not measured: theme exists in labels/filings
  count only, no quantified gap, no resupply barrier, or an NLP artifact (labels
  like "X: Constraint from ESG/FDA/Real Estate Demand" — name the real chain or
  discard; a beneficiary list dominated by services/software names in a hardware
  constraint is an artifact).

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
- Apply judgment on top of scores: broad noisy themes (100-relevance breadth themes),
  single-signal mappings, and thin tickers (technical=null) should be called out, not
  hidden. The score is a screen, not the conclusion.
- If two candidates tie, prefer: order-book evidence > conviction > breadth.
- DB: Postgres `makrograph` only (localhost, user postgres).
- Dates DD-Mon-YYYY in prose; keep the briefing scannable (tables + short lines).
