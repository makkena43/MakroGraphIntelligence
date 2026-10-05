# Earnings Inflection Detector — root-to-shortlist change specification

Date: 2026-10-05  
Target reviewed branch: `claude/optimistic-bardeen-03g48v`  
Status: implementation handoff; not production activation authorization

## 1. Instructions to Claude

Implement the work packages below in dependency order. First inspect the current branch, applicable repository instructions, existing tests, and `docs/EARNINGS_INFLECTION_DETECTOR_SPEC.md`. Reconcile differences with current code; do not assume the review describes a later revision. Preserve unrelated work. Reuse and repair the existing earnings-inflection component rather than create a second detector.

This document is an amendment to the original specification. It adds bounded, explicitly invoked universe discovery and a **research shortlist**, not investment recommendations. Existing safety boundaries remain. Shared ingestion fixes may be implemented behind backward-compatible, disabled-by-default options with regression tests; do not activate them in production. Do not change the stock selector, guidance radar, constraint logic, existing schedules, portfolio actions, or production data. No BUY/STARTER/ACCUMULATE labels, position sizes, guaranteed-return claims, or automatic investment promotion.

Deliver implementation, offline tests, optional test-database migrations, configuration examples, runbook updates and an honest coverage/limitations report. Do not execute production migrations, network backfills, paid LLM/OCR runs, schedules, or notifications without separate authorization. Production reads require an explicitly configured read-only adapter. Never print credentials.

Complete and verify each work package before advancing. Do not stop after writing interfaces and claim the feature complete: implement executable paths and tests. If an external dependency blocks a path, label it blocked and finish independent offline work. Do not lower evidence standards to fill a shortlist or tune rules to named historical winners.

## 2. Objective and review baseline

Surface companies with potentially material, durable improvements in recurring parent-attributable earnings and cash generation. Detect early commitments separately from realized execution; explain uncertainty instead of hiding it in one score.

Each shortlisted company must answer: what changed; when it became knowable; which earnings mechanism it affects; potential earnings contribution and timing; evidence strength; contradictory evidence; financing/execution risks; missing information; valuation context when available; and the next falsifiable milestone.

The review ran `tests/earnings_inflection` and `tests/test_ingestion_pdf_text.py`: 112 tests passed. This is a code-test baseline, not evidence of live coverage or investment performance. Three offline checks showed: provisional orders can qualify as commitment-backed; utilization from different plants can be compared; PDF-only source rows provide no text through the current reader. Add regression tests before fixes.

Initial scope: India non-financial operating companies, including explicitly labeled SME reporters. Preserve bank/NBFC/insurance exclusions from industrial earnings models. US fiscal calendars and security identity must not be assumed supported; return an explicit unsupported status until implemented in a separately scoped adapter.

## 3. Required common contracts

Use validated, versioned records; exact table/class names may follow repository conventions. New persistent tables belong to the isolated `ei_*` namespace unless an additive shared-ingestion change is explicitly justified. Migrations must not execute on import or normal startup.

| Record | Minimum fields |
| --- | --- |
| Document version | stable document ID, issuer ID, source URL/attachment ID, raw-byte hash, text hash, parser/OCR version, published time and provenance, first-seen time, extraction time, immutable artifact reference, supersedes link |
| Extraction result | version ID, status, page counts expected/processed, text/table coverage, quality issues, retry count, method, complete/partial flag |
| Evidence | document version, page/span or table cells, exact quote, metric, normalized and original units, period, issuer/segment/plant/product scope, modality, evidence tier, validation state |
| Financial measurement | metric, period start/end, duration, fiscal calendar, scope, currency, scale, sign convention, reported/derived status, dependencies, restatement lineage |
| Commercial event version | stable event ID, customer/relationship status and basis, contract status, amount/currency/tax basis, executable amount if disclosed, delivery window, cancellation/amendment links, ownership/exposure, source evidence |
| Assessment | issuer ID, as-of, replay mode, evidence/review/scenario states, mechanism results, dependencies, contradictions, deferrals, source coverage, next checks |
| Run manifest | run ID, code/config/model/prompt versions, universe snapshot, cutoff, source-version IDs/hashes, scan cursor, completed/failed/deferred counts, budget consumption, run status |

Represent unknown values explicitly; never substitute zero. Store source facts, management assertions, calculated facts and analyst assumptions separately. Every derived signal must expose the inputs needed to reproduce it.

## 4. Ordered work packages

### WP1 — Close the ingestion-to-reader gap (P1)

Primary files: `pipeline/intelligence_pipeline.py`, `parser/pdf_parser.py`, `earnings_inflection/source_repository.py`, `document_versions.py`.

1. Make extracted text accessible to the detector in both live and historical modes. Persist a versioned text artifact or immutable text record whenever extraction succeeds; support existing legacy `raw_text` and `.txt` records without claiming they have proven version history.
2. Do not silently parse PDFs or initiate network work inside read-only retrieval. Use an explicit, bounded extraction/backfill command for PDF-only rows; source retrieval reports the missing artifact until it is available.
3. Preserve raw originals. Replace permanent unsupported handling for recoverable documents with `OCR_REQUIRED`, `PARSE_FAILED_RETRYABLE`, `ENCRYPTED`, `PARTIAL`, `EMPTY`, `UNSUPPORTED_FORMAT`, or equivalent states. Keep retry limits and failure reasons.
4. Implement a pluggable OCR fallback with an offline fake/test provider. Paid/network providers remain disabled. Never delete originals simply because parsing failed.
5. Retain page boundaries, table headers, units, footnotes and page-level coverage. Expose truncation, including max-page limits; nonempty text does not prove complete extraction.
6. Version artifacts by content and extraction method. Reprocessing must not overwrite the evidence used by an earlier run.
7. Emit coverage by issuer, period, source and document type. A call invitation is not a transcript; a downloaded PDF is not successful extraction. Expected coverage comes from the universe and reporting cadence, not only documents already present.

Acceptance: PDF-only legacy rows become readable after explicit extraction; historical/live paths produce equivalent evidence from the same source; scans enter OCR queue and remain recoverable; partial reports are labeled; two parser versions remain replayable; existing ingestion tests remain green and production defaults unchanged.

### WP2 — Repair identity, provenance and point-in-time replay (P1)

Primary files: `identity.py`, `source_repository.py`, `document_versions.py`, `pipeline.py`.

1. Resolve documents using stable issuer identity and effective-dated symbol aliases, not only the caller's ticker. Keep company identity separate from tradable security identity, ISIN, exchange, series and listing board.
2. Do not splice predecessors/merged businesses into a continuous financial series without an explicit comparability decision. Handle SME migrations without assuming quarterly history exists.
3. Implement two explicitly named modes: historical public-information reconstruction (publication cutoff, with reconstruction limitations) and system-knowledge replay (publication plus actual first-seen/artifact-availability cutoff). If legacy timestamps cannot prove replay, return unsupported/partial rather than invent dates.
4. Do not use today's industry, shares, listing status or universe membership as historical facts. Present-day metadata may be displayed only as labeled context, not drive historical qualification.
5. For derived signals, earliest defensible detection time is the latest availability of all necessary inputs, not the oldest cited document. Separate economic period, disclosure time and system processing time.

Acceptance: old/new ticker requests recover the same valid issuer history; future aliases/restatements cannot leak backward; a later-ingested old filing appears in reconstruction but not earlier system replay; derived signal timestamps do not predate their final required input.

### WP3 — Enforce financial integrity before qualification (P1)

Primary files: `validation.py`, `financial_series.py`, `drivers.py`, `assessments.py`.

1. Replace warning-only reconciliation with structured results: validated, explainable-definition-difference, unresolved, rejected. Preserve reported facts but exclude unresolved measurements from dependent calculations and qualification. Do not reject all company evidence because one metric failed.
2. Reconcile PAT/PBT/tax, EBITDA definitions, cash-flow identities where parsed, units and consolidated/standalone scope. Derivation dependencies include every input document.
3. Fix zero-value fallback: use total PAT only if parent-attributable PAT growth is genuinely unavailable, never when it equals 0%. Label total-PAT substitutes.
4. Separate reported and recurring PAT/EBITDA. Resolve exceptional-item signs and tax treatment from source; do not assume all positive exceptional items are charges. If adjustment cannot be proven, recurring earnings are unavailable.
5. Preserve quarter/half-year/annual duration, including year-ago comparability and FY-minus-cumulative derivations. Do not sum overlapping periods or annualize one period and call it TTM.
6. Parse parent attribution, diluted EPS/share-count basis, balance-sheet debt/cash, working capital and cash flow. Reconcile share changes and dilution; implied PAT/EPS shares are a labeled fallback with scope and period checks, not authoritative issuance history.
7. Use explicit loss-to-profit transitions when prior earnings are zero/negative; do not rank meaningless percentage growth.

Acceptance: inconsistent PAT cannot qualify its PAT signal; unaffected revenue evidence survives; parent PAT flat/group PAT rising does not create parent growth; land gains do not become recurring improvement; consolidated and standalone rows cannot mix; zero/negative bases and SME half-years have fixtures.

### WP4 — Make external-demand evidence economically meaningful (P1)

Primary files: `contracts.py`, `extraction.py`, `event_resolution.py`, `counterparty.py`, `drivers.py`, `assessments.py`.

1. Model inquiry/MoU/framework, preferred bidder/L1/LoI, binding executable order, execution, cancellation, expiration and amendment as distinct dated event states. A later amendment may reduce or reverse strength/value; do not retain only the strongest-ever state.
2. Exclude internal/subsidiary/related-party flows from verified external-order inflow. Relationship status must be `confirmed_related`, `issuer_asserted_unrelated`, `independently_supported_unrelated`, or `unknown`, with sources. Issuer assertions alone are not independent verification.
3. Capture customer identity and verification status. Anonymous customers remain unverified. Do not infer a shell company or fraud from missing information. External enrichment must use dated, attributable sources and explicit retrieval authorization.
4. Separate contract ceiling, guaranteed minimum, executable releases and unquantified frameworks. Capture taxes, currency, duration, payment terms and termination conditions when disclosed. Unknown margins stay unknown; customer prestige does not prove profitable economics.
5. Deduplicate by event identity, customer/product/project/time and amendment relationships. Similar amount alone must not merge distinct orders; repeated exchange/call/deck descriptions do not create independent evidence. Record ambiguous matches.
6. Calculate inflow from eligible new events, backlog from a dated outstanding snapshot, and cancellations separately. Do not add backlog and inflow as separate incremental demand. Apply freshness and scope checks; long contract duration is not guaranteed annual revenue.
7. Only validated binding external commitments may support a verified commitment classification. Provisional/unverified events remain visible in an early research lane with explicit limitations, not silently discarded or upgraded.

Acceptance: provisional 30/100 revenue case no longer qualifies as verified commitment; subsidiary orders excluded; unnamed customer retained as unverified; cancellation reverses eligibility after its publication; tax-inclusive and seven-year unquantified frameworks do not inflate executable revenue; duplicate disclosures count once; distinct same-size orders remain distinct.

### WP5 — Implement mechanism-specific earnings detection (P1)

Primary files: `drivers.py`, `extraction.py`, `financial_series.py`, `assessments.py`; split modules if needed.

| Mechanism | Required evidence/comparison | Key invalidation or qualification constraint |
| --- | --- | --- |
| Utilization | same plant/product/segment and capacity denominator; production/shipments where available | cross-plant comparisons, changing capacity, qualification delays, depreciation/interest burden |
| Product/customer mix | segment revenue/profit or quantified mix contribution; commercialization milestones | profitable segment too small; development revenue mistaken for repeat production |
| Pricing/input costs | comparable realized pricing, gross margin and input trends; contract pass-through terms | commodity reversal or price cuts; no claim of observed cause from margin correlation alone |
| Order quality | WP4-qualified commitments, executable timing, financing/payment terms | cancellations, low/unproven margin, working-capital burden, related customers |
| Debt reduction | gross/net debt, interest, operating cash, funding source | equity-funded or asset-sale repayment mislabeled as operating improvement; seasonal cash |
| Segment turnaround | comparable segment loss/profit series and group earnings contribution | repeated one-offs, restructuring costs, scope changes |
| Organic volume/share | units/shipments, repeat customer orders, organic vs acquisition revenue | price inflation/acquisition growth mistaken for volume; share claims without market denominator |

For each mechanism return assertion/commitment/emerging/confirmed/contradicted status, magnitude, timing, durability, attribution basis, evidence confidence and invalidators. Confirmation must follow mechanism-specific persistence or independent realized milestones; do not require >=25% revenue growth for every mechanism. Negative developments must be represented, not counted as positive because absolute change is large.

No exact numeric guidance is mandatory. Guidance is corroborating evidence, not a prerequisite. Avoid double-counting utilization, margin expansion and operating leverage when they describe the same earnings change. When quantitative attribution is unavailable, surface a research hypothesis with unknown magnitude.

Acceptance: Plant A 40%/Plant B 90% yields no utilization delta; same-plant comparable improvement does; low-sales-growth sustained margin improvement can confirm its own mechanism; operating-funded debt reduction differs from dilution-funded repayment; loss turnaround and adverse changes are covered. Each mechanism needs positive, negative and missing-data fixtures.

### WP6 — Complete LLM extraction and management promise tracking (P2)

Primary files: `extraction.py`, `budget.py`, `guidance_ledger.py`, CLI and config.

1. Wire an explicit provider adapter into the CLI. `llm.enabled=true` without a valid configured client must fail preflight once, not fail separately for every company. Default remains disabled; offline fake adapter required.
2. Deterministic parsing handles arithmetic/structured tables; LLM handles relevant narrative, table ambiguity, scope, target periods, negation and explanations. It must not decide investment action or invent missing values.
3. Structured extraction must preserve quote/span, source version, scope, units, modality, target period and commitment strength. Validate all fields; quote presence alone does not validate interpretation. Treat filing instructions as untrusted document content.
4. Cache by content hash, chunk/parser version, prompt/schema/model/config. Deduplicate overlapping deterministic/LLM evidence. Apply call/token/currency budgets before dispatch; record actual provider usage and failed-call charges; unsupported hard cost guarantees must be disclosed.
5. Maintain original guidance, every revision, reasons, actual outcomes and target-period matching. An explained revision is not automatically credible; an unexplained change is not proof of fraud. Report sample size and insufficient history instead of unsupported management-reputation scores.
6. Restrict LLM use to selected ambiguous/material chunks with context. Preserve missing/failed extraction counts; no silent conversion to “no signal.”

Acceptance: enabled client works through CLI using fake provider; missing client fails preflight; invalid units/periods/quotes rejected; prompt injection has no operational effect; budget stops mark partial run; revision history remains immutable and outcomes compare both original and revised targets.

### WP7 — Repair earnings bridge; add optional valuation research (P2)

Primary files: `earnings_bridge.py`, `financial_series.py`, new isolated market-data adapter if needed.

1. Match revenue and margin assumptions by scope, currency, fiscal target and base period. FY growth guidance applies to its stated FY base, not an arbitrary rolling TTM. Do not combine FY26 growth and FY27 margins without an explicit separate scenario.
2. Build downside/base/upside scenarios from cited facts and visible analyst assumptions. Model revenue conversion timing, margins, D&A from capex, interest/funding, taxes, minority interest, dilution and working capital where supportable. Never invent source-based precision.
3. Show recurring parent PAT/EPS bridge, earnings contribution by mechanism and overlapping effects. Label unsupported inputs; no optimistic default should silently fill a material gap.
4. Optional valuation uses as-of price, correct security/series, shares/dilution and verified corporate actions. Display scenario multiples, implied expectations and downside sensitivity, not buy advice. Missing valuation must not suppress credible demand research.
5. Show dated price-change context and business expectations; do not reject a company solely because its stock already rose a fixed percentage. Never use subsequent returns in candidate scoring.

Acceptance: mismatched fiscal guidance cannot produce a management case; capex-related D&A/funding assumptions are explicit; unavailable recurring EPS remains unavailable; equity/warrant collisions rejected; split/bonus adjustments and as-of cutoffs tested; valuation does not change evidence truth.

### WP8 — Add bounded discovery and explainable research shortlisting (P2)

Primary files: `pipeline.py`, `assessments.py`, `rendering.py`, `scripts/earnings_inflection.py`, config, isolated schema.

1. Preserve explicit single-ticker mode. Add explicit universe mode using a versioned eligibility snapshot. Historical runs require contemporaneous membership, including failures/delistings where data exist, or a clearly limited cohort label.
2. Require scope, as-of, max issuers/documents, page/chunk limits and spend ceilings. Implement fair pagination/checkpointing; do not silently select only companies with ingested documents or first alphabetic tickers. Missing coverage remains in the denominator.
3. Add incremental run-once mode driven by new/amended document versions. Reassess affected issuer evidence plus dependencies; avoid reprocessing unchanged history. No scheduler activation.
4. Use separate research lanes: `EXECUTION_RESEARCH`, `COMMITMENT_RESEARCH`, `ASSERTION_WATCH`, `DATA_REPAIR`, `CONTRADICTED_OR_STALE`. Retain per-mechanism states so one missed promise does not erase unrelated valid evidence. Mixed cases display prominent contradictions.
5. Rank within lanes using explicit evidence quality, potential recurring earnings materiality, timing, persistence and risk dimensions. Configuration and component contributions must be visible. Do not present heuristic scores as probabilities; no hidden LLM ranking, valuation-as-evidence substitution, or unsupported return predictions.
6. Missing noncritical data create deferrals; failed critical comparability/identity/source checks block only dependent qualification. Define the critical checks per lane in config/tests. Never fill a top-N quota by relaxing rules. 5–10 reviewed opportunities per year is a user workflow aspiration, not a forced output count.
7. JSON/Markdown shortlist contains: issuer/security, as-of, lane, change since previous assessment, mechanism, first defensible signal date, materiality/timing, strongest citations, contradictions, scenario/valuation availability, next milestone/deadline, and review effort estimate if supportable.
8. Include scanned/eligible/excluded/failed/deferred/completed counts and coverage rates. Differentiate “no qualifying evidence” from “not assessed.” Persist immutable run IDs/artifacts; do not overwrite ticker/date outputs on reruns. Research-state transitions grant no investment authority.

Acceptance: a small offline universe produces deterministic lane/rank output; symbol order does not alter coverage; missing-document issuers appear in coverage; checkpoint resume reproduces full-run output; same inputs produce same results; newer disclosures generate meaningful deltas; partial-budget run cannot claim complete coverage.

### WP9 — Validate detection quality before performance claims (P2)

Primary files: `evaluation.py`, tests, implementation report and runbook.

1. Keep realized returns strictly outside detector imports/features/prompts. Freeze config before examining holdout outcomes.
2. Build a company-neutral cohort rule and source-labeled mechanism fixtures across sectors, market-cap/liquidity groups, quarterly/SME reporters and adverse outcomes. Named historical winners may be regression examples only, not the validation universe.
3. Validate document coverage, extraction accuracy, numeric/scope accuracy, event deduplication, classification precision/recall against independently reviewed labels, detection delay and human review burden. Separate data-coverage failure from detector false negatives.
4. Then evaluate returns for dated lanes, not only survivors/top winners. Use feasible next tradable entry after disclosure, verified adjusted prices/total-return basis, costs/liquidity, delistings/suspensions, and matched broad/small-cap/sector benchmarks. Report sample sizes, overlapping-window dependence, drawdowns and censored observations. Do not label incomplete 36-month observations as 36-month returns.
5. Backtest scope may include 2020–2025 anchors and forward outcomes only to the verified available date. Do not fabricate availability through year-end 2026. Missing historical artifacts or universe membership must constrain claims.
6. Produce a prospective shadow-pilot plan with no trading/notifications activated. Define review cadence and success metrics before running it; do not claim “multibagger detection” from a handful of favorable examples.

Acceptance: offline labeled evaluation runs end to end with uncertainty/coverage reporting; future return fields cannot influence detection; unavailable outcomes remain censored; negative/missed cases appear in the report. Network-scale historical evaluation requires separate authorization and budget.

## 5. Completion and delivery checklist

For every work package provide: changed files, tests added, test command/output, migration/config changes, compatibility impact, remaining limitations and activation status. Maintain a requirements matrix mapping each numbered requirement to implementation and verification. Distinguish implemented-and-tested, implemented-but-unverified, blocked and deferred; do not count schema-only support as working ingestion.

Deliver:

- Offline regression fixtures for all review findings and all seven mechanisms.
- End-to-end fixture commands for extraction, single issuer, universe scan, replay and evaluation.
- Additive migration files with test rollback/recovery instructions; no production execution.
- Updated runbook, config and implementation report; coverage/sample outputs labeled synthetic where applicable.
- One compact research-shortlist example with actionable **research questions**, not trading instructions.
- A production-readiness checklist listing missing providers, historical identity/price data, source permissions and cost estimates.

Recommended release sequence: WP1–WP4 data integrity; WP5–WP7 analytical completeness; WP8 discovery; WP9 independent validation. WP9 regression work starts with WP1 and continues throughout. Do not declare investment readiness solely because tests pass or backtests look attractive.
