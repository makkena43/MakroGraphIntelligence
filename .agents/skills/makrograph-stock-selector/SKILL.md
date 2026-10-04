---
name: makrograph-stock-selector
description: >
  Surface a ranked list of Indian or US stocks worth analyzing for a given date, from
  the MakroGraph DB — major + emerging themes (6-12 month window), major constraints
  with explanations, and supply-side beneficiaries. Also answers single-theme queries:
  "who benefits from the transformer constraint", "solar cell companies as of a given date".
  Trigger on: "which stocks should I analyze", "stock selector", "today's
  opportunities", "give me stocks for a given date", "what themes are emerging", "shortlist
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
**US mode** (`--country US`): the monthly/post-NLP pipeline must first build the
country-scoped company-product-role and constraint-candidate snapshots. The selector
then uses the same exact constraint-product-company decision contract as India.
Legacy India capacity/import tables are never reused for US; US measurement must come
from its country-scoped ledger/observation evidence. There is no local US technical
overlay, so valuation, liquidity and chart entry checks remain mandatory. See
`us_data_note` in the JSON.

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

### Step 1b — Verify the decision gates are present

Check the SUMMARY line for `cross_theme_overlap_leaders` and `high_confidence_themes`.
If the JSON was generated before these improvements, re-run Step 1. The JSON must
contain: `final_decision`, `greatest_constraints`, `evidence_dashboard`, `cross_theme_overlap`,
`bear_cases`, `constraint_company_universe`, and each candidate must have
`scoring_breakdown`. Use
`jq '.final_decision'` to confirm. `final_decision` is the only action authority;
it contains separate `priorities` (investment-committee hand-off),
`early_timing_candidates` (strict measured starter), and
`discovery_starter_candidates` (proof-led experimental starter) lanes. Raw ranks,
tiers, quarantine, cross-theme overlap, and mapped beneficiary counts are diagnostics.

### Step 1c — Verify constraint-detection coverage before judging stocks

Read `final_decision.constraint_detection_coverage` and each displayed
constraint's `detection_origins`. The detector must union four dated inputs:

1. mapper products (company discovery);
2. capacity-gap and import-dependence reference records (physical discovery);
3. reviewed `EXACT` product aliases linked to the dated constraint ledger.
4. the upstream ingestion/NLP constraint-candidate snapshot, which itself
   carries product, evidence-leg, source-family, and same-product company-role
   provenance.

Do not allow the mapper to define the constraint denominator: a current
reference-only physical chain may be real before any listed producer has been
mapped. It must appear as `MAP_COMPANIES` / research-only, with no stock
recommendation, until the company-role pipeline recovers an exact listed
supplier link. Conversely, a broad, family, or policy alias is context only and
must never be converted into an item-level physical shortage.

For every physical claim, use the source record's stated `as_of_date` or ledger
`source_date`; never use a database `created_at` timestamp to make a later
refresh appear in an earlier report. Inspect `constraint_quality.source_diversity`:
multiple fields from one release are one source family, not independent
confirmation. Multiple uploads by one issuer on one date are one evidence
event, and grade A requires at least two independent evidence dates. Source
diversity improves research confidence but does not relax the physical,
company-role, or position gates.

The selector consumes the dated upstream `mg_constraint_candidates` and
`mg_constraint_company_candidates` snapshots when present. Confirm that
`UPSTREAM_CONSTRAINT_PIPELINE` appears among detection origins for chains first
found by ingestion/NLP, and show the upstream mechanism, research priority and
missing evidence legs. Those fields order research only. Company rows must carry
an automatic adjudication state. Treat `OPERATING_PRODUCER_EVIDENCED`,
`PIPELINE_EVIDENCED`, and `EARNINGS_CAPTURE_EVIDENCED` as progressively stronger
product-role evidence; none is itself a Buy. Treat `QUARANTINED_*` as no-position
exceptions and `AUTO_REJECTED_NO_ROLE` / `AUTO_REJECTED_WRONG_ROLE` as audit
outcomes, not research recommendations. Routine exact-role promotion must not
depend on a manual approval row; a dated human rejection may still veto a false
positive. If the snapshots are absent for the report
date, flag upstream coverage as stale/incomplete instead of treating the mapper
universe as complete.

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
   **Lead with `greatest_constraints` when it is present.** This is the
   constraint-first research queue: show its research tier, corresponding themes,
   mapping freshness, own-filing maker proof as of the report date, and physical context before
   discussing individual stocks. It is not a buy list. Never describe a mapped
   company as a verified maker when the capability snapshot is stale or it is
   machine-quarantined; in that case say stock selection is blocked pending the
   printed as-of evidence gate. Mapped-company counts, mapper conviction, capacity gaps,
   import dependence, and broad order-book flags remain context until they pass a
   clean point-in-time predictive test.
   **Measurement-coverage rule:** never show bare `UNMEASURED`. Read
   `constraint_quality.measurement_coverage` and write one of: current exact
   measurement, stale exact measurement, family-level measurement, family/broad
   context only, policy/deployment evidence only, or exact measurement missing.
   “Detected — measurement missing”
   is a data-coverage state, not evidence that the constraint or industry is absent.
   Print its `next_action`; do not borrow a broad/family ratio for an atomic product.
   **Maker-universe rule:** read `constraint_company_universe` and cover every
   detected constraint, not only the compact headline shortlist. For each atomic
   product node, separate (a) automatically promoted, independently corroborated
   operating direct producers, (b) automatically promoted evidenced capacity/localisation
   pipelines or direct product roles, and (c) at most three machine-quarantined
   exceptions. Never render a separate mapper-only pseudo-universe. Print aggregate
   automatic rejection counts and reasons for unsupported or wrong-role hits. A
   document-level product/manufacturing corroboration remains quarantined; it cannot
   become operating proof, earnings capture, or position authority. A parent/group
   disclosure with exact product-and-capacity proof
   is valid evidence even when its exchange industry label differs; disclose the
   entity scope instead of vetoing it. Do not put EPC firms, developers, buyers,
   generic policy references, or lexical collisions in the direct-maker table.
   Auto-reject those from the producer universe while retaining the counted audit
   outcome. Two evidence events must be non-duplicative; one clean capacity plan
   remains quarantined, not an operating maker or Buy. Strong exact evidence promotes
   mechanically; ordinary promotion must never require the user to perform manual
   issuer research.
   **Company-originated discovery rule:** read `company_product_discoveries`
   separately from `greatest_constraints`. These rows establish only that an
   issuer's own dated filing names a product and a direct economic role. Present
   them as an *unlinked research queue* until independent dated import/capacity,
   demand, and resupply-barrier evidence creates a constraint ledger record. Do
   not connect an adjacent product by inference. A reviewed exact product alias
   is required before a company-role row can enter a producer universe.
4. **Beneficiary stocks under EVERY theme/constraint (MANDATORY)** (from
   `supply_side_beneficiaries`): each theme or constraint presented in the briefing
   must carry its own beneficiary stock list (top 5-8 tickers in theme-rank order) —
   never present a theme without its stocks. For IN include beneficiary_type
   (direct/critical/input supplier), conviction, order-book flag. Supply side is the
   focus — capacity owners, not demand-side consumers; flag demand-side names that
   appear inside supply themes (e.g. software names in an energy-constraint theme)
   as noise instead of hiding them. Group related theme-chains (e.g. five utility
   variants) into one row with the union of their top names. Mapper names are discovery
   inputs only: if their exact own-filing role is not promoted by the company-role
   adjudicator, do not present them as corresponding producers.
5. **Raw candidate diagnostics — never a recommendation** (from `ranked_candidates`): a table
   of the top 10-15: ticker, company, products/themes it spans, conviction, order-book,
   technical state (above 200DMA? % from 52w high). Present this as what it is — a
   mechanical screen that surfaces candidates. The composite score and `discovery_tier`
   are NOT the final call and must be labelled `raw screen` in client-facing output.
   The final categorization comes from `final_decision`; do not call a T1 name a Buy.
6. **Evidence dashboard — MANDATORY (Improvement 1)** (`evidence_dashboard`): a table
   for the top 6-8 themes showing companies_mapped, filings_covered,
   bottleneck_signals, confirmed_quarters, policy_events, and evidence_confidence_pct.
   The confidence_pct encodes how much hard evidence exists vs narrative momentum — a
   100% Consensus theme with 0 policy events is market-known but policy-unsupported.
   Flag any theme with confidence < 50 as "low-evidence, treat as watch only."

7. **Cross-theme overlap — diagnostic only** (`cross_theme_overlap`): a table
   of companies that appear in 3+ independent constraint chains, sorted by
   n_independent_chains then overlap_score. Do not describe it as a conviction multiplier
   or structural chokepoint: duplicated product labels and demand-side mappings can inflate
   it. Show ticker, chain count, and list the chains grouped by type (energy / semi / infra),
   then state that it is excluded from selection until every product role is verified.

8. **Transparent scoring — MANDATORY (Improvement 3)** (`scoring_breakdown` on each
   candidate): when presenting the ranked-candidate table include one sentence on the
   formula: IN = "0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub,
   ×1.15 technical"; US = "0.40×relevance + 0.30×breadth + 0.30×rank". For the top 3
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
past returns and NOT by the strength score. Keep **physical quality** separate from
**evidence completeness**: `UNMEASURED` means that the as-of record lacks the
quantified supply/import leg; it is not a verdict that the constraint is weak.

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
- **UNMEASURED (render as its explicit measurement-coverage state; research only in the physical-constraint mechanism)** — the theme
  exists in contemporaneous mapper/company filings but no point-in-time quantified
  gap or import observation has been captured. Seek the measurement before Core
  sizing; do not relabel the constraint weak or erase a company-led opportunity.
  It cannot enter the physical-constraint Early/Timing position lane. A dated
  exact company role and catalyst make it a high-priority research lead while
  measurement is obtained. NLP artifacts (labels
  like "X: Constraint from ESG/FDA/Real Estate Demand" — name the real chain or
  discard; a beneficiary list dominated by services/software names in a hardware
  constraint is an artifact). Missing structured coverage lowers certainty; it is
  never a generic reason to erase a dated company-led opportunity.

**Then, for selected themes only, two overlay checks** (they shape HOW to play it,
not whether the constraint is real):
- **Monetization**: WHICH value-chain layer captures the scarcity economics, and is
  there a listed pure-play on that layer? A Grade-A constraint with no investable
  pure-play is analysis, not an opportunity — say so instead of force-fitting the
  nearest conglomerate. Also: does scarcity become supplier margin, or does the
  buyer/regulator cap it (regulated tariffs absorb the rent)?
- **Crowdedness / timing (`chain_technical_state`)**: computed per chain —
  pct_above_200dma + median distance from 52-wk high across the mapped cohort.
  EXTENDED_CROWDED = constraint may be real but priced; size/timing adjusts, the
  grade does not. **DERATED + Grade A/B constraint + evidence intact = the
  priority setup** ("quality at a discount" — the Dec-2025 transformer pattern:
  chain −18 to −30% off highs while order books never deteriorated). Always
  check this field for every selected theme and say which state it is in.

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
candidate list is blind there — flag it for the scheduled company-role and crosswalk
refresh, rather than assigning the user an open-ended manual task);
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
- **Resolution risk**: every constraint carries a supply-response clock. A capacity
  plan is not operating supply: record whether the listed-company response is
  `ANNOUNCED_UNDATED`, has a stated target, or remains unproved. Do not infer a
  constraint's death date from a plan; verify commissioning, utilization,
  qualification, and the industry-wide balance.
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
- **Crowdedness**: consensus freshness + extended technicals (near 52w high, far above
  200DMA) = the market already knows. Fresh theme + base-building technicals = the
  asymmetric setup.
- **Governance/risk**: read the actual `risk_events` text, not just the tier. Auditor
  resignations, CIRP, SEBI actions, promoter pledge spikes = Avoid regardless of score
  (the GENSOL lesson: 0.850 composite → −95%).
- **Cross-theme role**: is the overlap count real (structural chokepoint spanning
  independent chains) or an artifact (mapped everywhere because every filing mentions it)?

**Output categories** (yours, not the script's):
| Category | Meaning |
|---|---|
| **Core Buy** | Evidence-backed, right phase, clean risk — act now per position guidance |
| **Timing Buy** | Strict A/B Early/Timing lane cleared; state exactly what confirmation is required |
| **Discovery Starter** | Exact producer/capacity role and same-product catalyst cleared before physical measurement; obey the per-name and 5% aggregate caps |
| **Watch** | Promising but thin evidence (single-signal, low theme confidence) — name the trigger that would upgrade it |
| **Avoid** | Governance risk, consensus+crowded, demand-side leak, or noise mapping — say which |

For every categorized stock give three one-liners: **Why now** (or why not),
**What kills it** (stock-specific, from bear_cases/risk_events — not boilerplate),
and **Conviction** (High/Medium/Low with the single strongest piece of evidence).

**Divergence table (MANDATORY)**: formula rank/tier vs your category, with a one-line
reason wherever they differ. This is where the intelligence shows — a rank-9 stock on a
fresh theme promoted to Core Buy (the STLTECH lesson: rank 9, fresh, +293%), or a rank-3
consensus name demoted to Watch. If you have zero divergences, you haven't analyzed —
you've echoed the formula; look again, especially at ranks 6-12 with fresh freshness.

**Backtest priors** (weigh them, don't obey them): fresh themes outperform consensus;
order-book evidence is the most honest signal; above-200DMA showed NO alpha edge
(don't reward extension); HIGH risk tier → out, always; hit rates ranged 28%-80% by
year, so in weak-breadth regimes prefer fewer Core Buys over forced five.

**Point-in-time discipline for judgment**: reason ONLY from evidence in the JSON plus
general industry structure knowable before the as-of date. Never let knowledge of what
actually happened after the as-of date leak into a historical categorization — if you
catch yourself thinking "this one went up later", discard that and re-derive from the
evidence. State this discipline once in the briefing for historical dates.

## Hard rules

- No data dated after the as-of date, ever. The script enforces this; don't add
  DB queries without a `<= as_of` filter.
- **The report date is the decision date.** Evaluate every gate only with information
  dated on or before `as_of`; never require today’s data or a later reference refresh.
  A Core Buy needs A/B **measured physical-quality** constraint evidence, a corroborated producer/supplier role,
  focused exact-product exposure, order-book evidence, and no HIGH risk. An Early/Timing candidate in
  the physical-constraint lane is a capped starter, not a relaxed Core Buy: it needs an
  A/B measured constraint, a fresh exact operating-maker role, an as-of order-book or
  repeated capex catalyst, normal risk, and a named confirmation event. `UNMEASURED` is
  not a weak-quality verdict and cannot enter Core or Early/Timing. It may enter only the
  separately labelled Discovery Starter lane when dated binding-demand evidence plus either
  structured constraint evidence or exact-role catalysts from at least two independent producer-ledger companies
  agree. Each stock also needs an exact operating/pipeline producer-ledger role, a same-product catalyst,
  and NORMAL risk. Mapper-only and quarantined roles have no position authority. Cap producer-ledger names
  at 1.0% when unmeasured (1.5% for A/B), five names and 5% aggregate.
  Discovery is a 6–12 month lane: revalidate at least quarterly and either graduate it to
  Early/Core on new evidence or exit; unused capacity stays in cash.
  Generic policy mentions,
  mapper conviction, company count, or cross-theme overlap cannot substitute for role and
  company-catalyst proof.
- Judge every gate on one exact constraint-product-company tuple. An unrelated failed
  mapper tag or a diversified company's adjacent products cannot veto or validate the
  selected tuple.
- The final investable shortlist is the only action list. If it is empty, say
  `NO ALLOCATION FROM THIS STRATEGY`; never promote Raw, PLI, policy, producer-universe,
  or quarantine rows to fill a desired pick count.
- For historical reports, include only calibration outcomes completed before the as-of
  date. One cohort vintage is **insufficient history**, not a durability failure.
- Apply judgment on top of scores: broad noisy themes (100-relevance breadth themes),
  single-signal mappings, and thin tickers (technical=null) should be called out, not
  hidden. The score is a screen, not the conclusion.
- If two candidates tie, prefer: order-book evidence > conviction > breadth.
- DB: Postgres `makrograph` only (localhost, user postgres).
- Dates DD-Mon-YYYY in prose; keep the briefing scannable (tables + short lines).
