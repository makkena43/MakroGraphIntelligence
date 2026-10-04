# Earnings Inflection Detector — implementation specification

Version: 1.0 draft for implementation review  
Date: 2026-10-04  
Repository: MakroGraphIntelligence  
Status: SPECIFICATION ONLY; no implementation or deployment authorized by this document's creation

## 0. Copy-paste handoff to Claude

> Implement the Earnings Inflection Detector described in this document as an isolated, opt-in, research-only component. Start by inspecting the repository and reporting discrepancies between this specification and the actual schema. Preserve all existing tracked and untracked work. Do not run or change the existing guidance radar, stock selector, report pipeline, ingestion schedules, production migrations, production data, or external notifications. Do not produce BUY, STARTER, ACCUMULATE, position sizes, portfolio instructions, or automatic investment promotions. Implement in the phased order below, initially using offline fixtures and a dedicated test database. Production data may be read only through an explicitly configured read-only connection. LLM calls, network retrieval and database persistence are disabled by default. Tests passing or attractive backtest returns do not authorize production activation. Deliver code, tests, migration files not executed against production, and a coverage/limitations report. Stop for explicit authorization before any activation or materially expanded paid run. Do not change requirements to capture named historical winners.

“Without promoting anything” is interpreted conservatively in two ways: no automatic investment recommendation/portfolio promotion, and no promotion of software into the existing production workflow. Analytical states may change as evidence changes; this grants no investment authority.

## 1. Objective and boundaries

Detect material, potentially sustainable changes in a company's earnings economics using MakroGraph's existing announcements, financial results, annual reports, investor presentations and earnings-call transcripts.

The output answers:

1. What changed, in which business, and when did that information become public?
2. Is the change a management assertion, a commercial commitment, or realized execution?
3. How could it affect recurring parent-attributable diluted EPS and cash generation?
4. Which contradictions, financing needs, customer risks and missing inputs remain?
5. What evidence or milestone should be checked next?

Non-goals: predict guaranteed multibaggers; create a buy list; size positions; infer fraud; execute trades; rewrite constraint detection; optimize rules for known winners; automatically download the entire market; claim statistical alpha from the previous manual case studies.

Initial market: India, non-financial operating companies, including separately labeled SME issuers. Country and fiscal-period contracts must accommodate US filings, but a US adapter is a later explicit phase. Banks, insurers and investment companies return `UNSUPPORTED_FINANCIAL_MODEL` for operating-margin/earnings bridges, not misleading industrial-company calculations. Missing EPS/price inputs do not prevent source-backed demand research.

## 2. Repository findings and integration map

These observations come from source-code inspection, not a certification of live database coverage. Deployment must run a read-only schema/coverage preflight. No database introspection or migrations were performed for this specification.

| Existing asset | Intended use | Restrictions / observed gaps |
|---|---|---|
| `schema/postgres_schema.sql`, `mg_documents` | Canonical filing metadata: id, source_name, doc_type, filing_type, ticker, company, cik, country, filed_at, published_at, local_path, hashes | Base DDL inspected does not define `raw_text`, while current repository code reads it. Introspect actual columns; never assume base DDL equals live schema. |
| `src/makrograph/storage/pg_store.py` | Connection conventions | Constructor defaults to migration behavior. If reused, explicitly pass `skip_migrations=True`, enforce read-only transactions/role; never call schema methods in a research read. |
| `src/makrograph/fetcher/nse_fetcher.py`, `bse_fetcher.py` | Existing NSE/BSE issuer disclosures | Classify document contents, not only a coarse title or `filing_type`. An invitation is not a transcript. |
| `india_pdf_fetcher.py`, `parser/pdf_parser.py` | Existing download/parser interfaces | Existing text may lose tables/pages or omit scanned statements. Reparse into isolated output, never overwrite source documents automatically. |
| `edgar_fetcher.py`, `source_adapter.py` | Later US adapter | SEC acceptance/publication timestamps and CIK mappings required; do not assert US completeness from code presence. |
| `fundamentals_snapshot`, `screener_fundamentals_fetcher.py` | Optional present-day context | Upserted snapshots with `last_updated` are not an immutable point-in-time fundamental history. Disallow in historical calculations unless the exact dated version is proven. |
| `nse_bhavcopy_data`, `bse_bhavcopy_data`, `mg_benchmark_prices` | Optional market context / separate evaluation | NSE DDL primary key is `(trade_date,symbol)`, not security/series. No ISIN in that inspected DDL. Warrants may collide with equity; old symbols and SME series need explicit identity mapping. |
| `src/makrograph/india/order_book_detector.py` | Reusable lexical hints only | It translates order/utilization language into supply-constraint signals. Such signals are not proof of earnings inflection. |
| `src/makrograph/india/capacity_engine.py` | Context only | Policy-derived engineering ratios are not observed company utilization. |
| `mg_signals`, `mg_events`, company-product tables | Optional retrieval hints | Never qualify a company based on theme/selector score or derived beneficiary role. Return to dated source evidence. |
| `src/makrograph/guidance_radar/`, `schema/guidance_radar_schema.sql`, `scripts/guidance_radar.py`, `tests/test_guidance_radar.py` | Reference implementation patterns only | Existing action labels and sizing are expressly out of scope. Do not invoke its scorer, runner, persistence, or backtest as a trusted implementation of this specification. |

Specific behaviors NOT to inherit unchanged from the prototype:

- Mandatory large numerical guidance; silent or conservative management can still show inflection.
- Evidence “independence” based on distinct dates: the same order repeated on different dates remains one economic event.
- Annualizing the latest quarterly EPS and labeling it trailing EPS.
- Calling prices corporate-action-adjusted because no daily move exceeded 45%.
- A deterministic 6/12-month rally threshold that automatically rejects an otherwise interesting business.
- A global SQL limit ordered by ticker that can silently omit later-alphabet companies.
- Newest-first keyword sentence compaction that drops original promises, negations, table headers or cash-flow warnings.

## 3. Architecture and proposed files

All paths below are PROPOSED, not existing implementations:

```text
src/makrograph/earnings_inflection/
  contracts.py             # validated typed records, enums, schemas
  source_repository.py     # read-only adapters + schema/coverage preflight
  identity.py              # dated company/security/segment/customer resolution
  document_versions.py     # hashes, publication provenance, restatement lineage
  chunking.py              # page/table-aware extraction and retrieval
  extraction.py            # deterministic parsing + optional constrained LLM
  validation.py            # quotes, units, chronology, scope, reconciliations
  event_resolution.py      # cross-document economic-event deduplication
  financial_series.py      # comparable quarterly/annual measurements
  drivers.py               # deterministic candidate calculations
  guidance_ledger.py       # original commitments, revisions, realized outcomes
  counterparty.py          # identity/relationship/commitment evidence
  earnings_bridge.py       # assumption-based scenarios, no action authority
  assessments.py           # separate evidence, review and scenario statuses
  rendering.py             # JSON + Markdown, mandatory source/limitation fields
  budget.py                # hard spend/call/token reservations and cache
  pipeline.py              # explicit run-once orchestration
  evaluation.py            # outcome sandbox; never imported by detection
scripts/earnings_inflection.py
schema/earnings_inflection_schema.sql
config/earnings_inflection.example.yaml
tests/earnings_inflection/
```

Flow:

```text
read-only source snapshot
  -> document/identity/coverage validation
  -> chronological evidence extraction
  -> numeric validation + economic-event deduplication
  -> driver changes + promise delivery + counterparty checks
  -> evidence-only assessment
  -> optional scenario and price context
  -> research JSON/Markdown + unresolved questions

frozen assessment artifact -> separate outcome evaluation
```

No change to `run_pipeline.py`, selector skills, report entry points or existing cron/heartbeat jobs. No Neo4j dependency. Existing NLP/embeddings may reduce retrieval cost but are neither required nor authoritative.

## 4. Time, identity and coverage contracts

### 4.1 Point-in-time rules

Every run requires an explicit timezone-aware `as_of`; no implicit current-date fallback. Store UTC timestamps plus exchange timezone. A date-only input means end-of-day in the explicitly selected exchange timezone.

Track separately:

- `public_available_at`: when the specific document/content version became publicly available.
- `ingested_at`: when MakroGraph acquired it.
- `period_start/end`: economic period described.
- `extracted_at`: when the detector interpreted it.
- `availability_precision`: exact timestamp, date only, or unknown.
- `availability_evidence`: source timestamp, filing cover, archive reference, or manually verified provenance.

Eligibility requires `public_available_at <= as_of`. Period end is NEVER a substitute for publication. If only a reliable filing date exists, use end-of-day conservatively and first subsequent tradable session for evaluation. Unknown publication date => evidence quarantine, not backdating. Transcript filed later than a call cannot be used on the call date unless a separately archived audio/release available then is actually used.

Two replay modes, always labeled:

1. `PUBLIC_INFORMATION_RECONSTRUCTION`: a later-ingested source may be used if the exact historical content and public availability are proven. Does not claim the live system possessed it then.
2. `SYSTEM_KNOWLEDGE_REPLAY`: additionally require source-version ingestion and any applicable review/mapping availability by the historical cutoff. Backfilled content cannot improve this replay retroactively.

Never use a later restatement, website overwrite, customer identity disclosure, or review decision at an earlier cutoff. Retain original versions and supersession links; do not delete earlier facts. A later PDF's comparative table is not eligible earlier just because it describes earlier quarters.

### 4.2 Identity

Use stable internal `company_id`, and dated security identifiers (ISIN for India where available, exchange/symbol validity ranges; CIK for US). Do not join by company-name similarity alone. Distinguish issuer, subsidiary, JV, plant and segment. An alias is a metadata association, not proof of an operating role or economic relationship. Ambiguous merges enter review, not automatic consolidation.

### 4.3 Coverage first

Default target history: eight quarters plus two annual reports and intervening material announcements; up to 1,095 days of retrieval to cover reporting lag, with older explicit commitments fetched selectively. New listings may have less; show denominator and missing periods without automatic exclusion.

Emit per-company counts of eligible, missing-text, parsed, quarantined, processed and deferred documents; available result periods and concalls; source classes; dropped chunks and reasons. No universal claim that all relevant constraints/companies were covered.

Classify actual content: `RESULTS`, `TRANSCRIPT`, `PRESENTATION`, `ANNUAL_REPORT`, `ORDER`, `CAPACITY`, `RATING`, `AUDITOR`, `CAPITAL_ACTION`, `OTHER`, `INVITATION_ONLY`. Search title, filing_type and text, but retain coverage for documents with unknown classifications. Never treat absence of disclosures as evidence of bad management.

## 5. Data contracts and additive storage

Use a separate `ei_*` namespace in a dedicated sandbox database for implementation/testing. Migration file generation is allowed; execution against production is not. Default runs persist nothing. When explicitly enabled, write only to allowlisted sandbox tables. Store source references and hashes without adding cascading foreign keys to mutable production documents. Snapshot necessary provenance so deleting a source row does not silently erase research history.

### 5.1 Core contracts

Use Decimal for financial arithmetic, ISO dates, explicit nulls (never missing=zero), and explicit unit/currency scales. All payloads are versioned, validated with unknown fields rejected, and content-addressed where possible.

`EvidenceSpan`:

```text
source_document_id, source_version_hash, source_url,
public_available_at, ingested_at, availability_precision,
page_number|null, table_id|null, row_label|null, column_label|null,
char_start, char_end, exact_quote, normalized_quote_hash,
extraction_method, parser_version, validation_status
```

Offsets refer to immutable extracted text, not a mutable normalized summary. OCR text must be labeled. Numeric table evidence includes period headers, units and relevant footnotes. A matching sentence alone cannot validate the wrong number/column. Source documents are untrusted data; embedded instructions must never change prompts, permissions or tool behavior.

`MetricObservation`:

```text
observation_id, company_id, entity_scope_id, scope_type,
consolidation_basis, accounting_basis, metric_code,
period_start, period_end, period_kind[quarter,YTD,annual,instant],
value|null, unit, scale, currency|null,
reported_or_derived, recurring_or_reported, exceptional_adjustments[],
evidence_span_ids[], derivation_input_ids[], formula_version,
comparability_status, quality_flags[], available_at, version_hash
```

Minimum metric dictionary: revenue, volume, realized price/unit, gross profit, EBITDA, EBIT, depreciation/amortization, interest expense, PBT, tax, PAT total, PAT parent, NCI, basic/diluted weighted shares, basic/diluted EPS, CFO, maintenance/growth capex if separately disclosed, gross/net debt, cash, receivables, inventories, contract assets/liabilities, customer advances, order inflow/backlog/cancellations, installed/available capacity, production, utilization, segment sales/profit and capital employed.

`EconomicEvent`:

```text
event_id, company_id, driver_type, plant_or_product_or_segment_id,
counterparty_id|null, order_or_project_reference|null,
event_stage, occurrence_date|null, earliest_available_at,
amount|null, currency|null, tax_basis[inclusive,exclusive,unknown],
start/end_execution|null, predecessor_event_id|null,
evidence_span_ids[], economic_event_cluster_id,
assertion_owner[issuer,customer,auditor,regulator,other],
verification_level[issuer_asserted,independently_corroborated,realized],
contradiction_ids[], unresolved_fields[]
```

Stages distinguish inquiry, qualification, MoU, framework agreement, LOI/L1, firm purchase order, under construction, commissioned, customer-approved, commercial production, delivered, collected, delayed, cancelled. Stage transitions require evidence. A signed framework can remain unquantified; commissioning is not full utilization. Stages are not necessarily a single linear sequence.

`GuidanceCommitment`: issuer/speaker/role, metric/scope, baseline, target range/unit/period, conditions, certainty (`formal`, `conditional`, `aspiration`), first evidence, deadline, revisions with publication times, supersedes link, comparable realized observations and outcome (`not_due`, `met`, `partially_met`, `missed`, `withdrawn`, `not_comparable`, `unknown`). Preserve original and revised delivery evaluations.

`CounterpartyAssessment`: exact legal name/identifiers, aliases with dates, relationship (`unrelated`, `own_group`, `customer_group_subsidiary`, `unknown`), relationship source and as-of date, commercial-presence evidence, financial backing/guarantee if disclosed, independent corroboration, contract enforceability/quantity/payment/cancellation fields, conflicts and missingness. `issuer_declares_unrelated` must not become `independently_verified_unrelated`.

`DriverAssessment`: metric changes, mechanism, support/contradiction spans, stage, sustainability (`structural`, `cyclical`, `one_off`, `mixed`, `unknown`), magnitude/baseline quality, cash confirmation, financing risk, next milestone, materiality and confidence dimensions. LLM self-confidence is not a calibrated probability.

`CompanyAssessment`: run/company/as-of identifiers, coverage, drivers, guidance ledger summary, counterparty risks, research state, review priority, optional scenario/market contexts, required next evidence, limitations, complete provenance. Explicit `investment_authority="NONE"`; no action or position-size field.

### 5.2 Proposed tables

| Table | Essential key and purpose |
|---|---|
| `ei_runs` | UUID PK; cutoff/mode/source manifest/config/prompt/model/code hashes, budget usage, status and progress |
| `ei_source_versions` | `(document_id,content_hash)` unique; immutable metadata/text fingerprint and publication provenance |
| `ei_evidence_spans` | span UUID PK; unique source-version/offset/parser hash; validated quotations/table context |
| `ei_metric_observations` | observation UUID PK; scope/period/metric/version unique; reported/derived values and lineage |
| `ei_events`, `ei_event_evidence` | economic event versions and many-to-many source spans; do not dedupe by date alone |
| `ei_guidance`, `ei_guidance_revisions`, `ei_guidance_outcomes` | immutable commitments, amendments, and as-of evaluation records |
| `ei_counterparty_assessments` | counterparty/company/as-of/source/config version unique |
| `ei_driver_assessments`, `ei_company_assessments` | run/company/driver/scope keys; typed JSON payload with scalar indexed fields |
| `ei_extraction_cache` | content/schema/prompt/model/parser/chunk hash unique; raw/validated outputs and validation errors |
| `ei_review_events` | append-only reviewer, recorded time, effective claim, evidence, reason; cannot grant investment authority |
| `ei_eval_cases`, `ei_eval_outcomes` | separate evaluator connection/schema; frozen membership and outcomes, invisible to detector |

All rows carry schema_version, created_at and relevant provenance. Index company/scope/period/publication, run/state, source hash. Specify unique constraints and idempotent upserts for immutable content; version replacements rather than destructive overwrite. Cache keys involving cross-document interpretation include the exact eligible source manifest and as-of mode/cutoff.

## 6. Extraction and computation pipeline

### Stage A — low-cost eligibility and retrieval

Read-only preflight: table/column presence, source counts by country/date/type, missing raw text, oldest/latest availability, price coverage. Return `BLOCKED_SOURCE_SCHEMA` or `PARTIAL_COVERAGE` honestly; never report empty query results as “no opportunities.”

For incremental runs, use `(ingested_at,id,content_hash)` or equivalent append-only change manifest so late-arriving old filings are discovered. Do not update the shared ingestion checkpoint. Changes dirty the relevant company/scope and trigger recomputation only there. Corrections invalidate dependent caches without deleting history.

Triage may use words, existing signals and numeric changes but must include results-only companies and a deterministic rotating exploration allocation. Record the total eligible universe and every deferral. Do not require two event types before retrieval. Do not silently truncate by alphabet.

### Stage B — bounded extraction

Extract per document/chunk, then assemble company history chronologically. Preserve full tables and neighboring negation/qualification passages. Include negative evidence sections: receivables, impairment, related parties, auditor notes, cash flow and funding. Cap work by budget with resumable cursors, not silent text chopping. Parse-only mode produces provisional facts, never inferred economic conclusions.

LLM contract: extract only supplied evidence into the above schema; unknown values null; distinguish analyst questions from management statements; historical actuals from forward guidance; stated customer names from guesses. No browsing, SQL, recommendations, arithmetic authority or requests to alter rules from inside extraction. Model/tool access is separately allowlisted.

### Stage C — validation

Verify quote location, numeric presence, period/scope/currency, units, and availability. Validate financial identities within disclosed rounding tolerance (configurable, initially greater of two display units or 0.5% of the relevant value). Large discrepancies yield conflicts, never silent correction. Permit legitimate non-GAAP definitions but label reconciliation and differences.

Return span-specific errors. Retry malformed JSON once within reserved budget; a failed extraction remains failed, not an empty successful result. If facts conflict, retain both assertions and block affected derived calculations; unrelated reliable drivers may still be displayed.

### Stage D — financial series and normalization

- Compare same business/consolidation basis and duration. Flag acquisitions, disposals, fiscal-year changes, FX and segment redefinitions; do not mix standalone and consolidated statements.
- Prefer reported non-overlapping quarters. Derive quarter from comparable YTD differences only with matching scope/accounting/version lineage and label derived. Do not subtract per-share YTD EPS when weighted-average denominators differ.
- TTM totals require four comparable quarters; TTM EPS needs valid attributable earnings/share treatment or reconciled reported per-share series. Otherwise `UNAVAILABLE`. Never call latest-quarter EPS times four trailing EPS.
- Track reported and recurring profitability side by side. Subtract substantiated exceptional gains and corresponding tax effects; no invented tax rate in normalized historical actuals. If after-tax effect is missing, show pre-tax adjustment with incomplete normalized EPS.
- EBITDA, EBIT and gross margins are separate metrics. Percentage-point changes and percent growth are separate fields. Low/zero/negative earnings bases suppress misleading growth percentages; show absolute changes and turnaround status.
- Utilization = comparable output / available capacity for the same period/product/unit. Annual nameplate capacity needs period adjustment and commissioning timing. Preserve management-reported utilization separately. Multi-product capacities cannot be summed without a defensible common unit.
- CFO confirmation uses the reporting interval actually available; do not pretend half-year/annual CFO is quarterly. Analyze cumulative cash conversion, working-capital absorption and customer advances, not a universal one-quarter CFO/PAT veto.

### Stage E — deduplication and contradiction ledger

Cluster exact/near-duplicate documents first, then economic events by issuer, customer, reference, plant/product, value, currency and execution window. Repeated announcements across dates/channels attach to the same event. A genuine amendment/delivery/payment is a new stage linked to its predecessor. Ambiguous event matches remain unresolved, with conservative aggregate totals (no double count).

Source count, distinct event count and independent originating-party count are different. A press article quoting the issuer is not independent customer corroboration. Record cancellations, cut guidance, delayed commissioning, poorer margins, dilution and weaker cash alongside positive facts.

### Stage F — driver detectors

Detect direction, mechanism and importance, not promotional adjectives:

| Driver | Required comparison / leading support | Mandatory challenge |
|---|---|---|
| Demand/volume | Physical shipments, customer qualifications, firm purchases and executable backlog versus same-period base | Cancellations, pricing-only growth, concentration, unfinanced customers |
| Operating leverage | Volume/utilization rises versus cost-base change | New depreciation, labor, interest, commissioning losses and capacity denominator changes |
| Product/customer mix | Segment share and profitability; qualification-to-commercial transition | Group materiality, undisclosed margins, displacement of existing business |
| Pricing/input spread | Realization versus cost/unit; retained price increases | Pass-through clauses, price competition, inventory gains and reversal |
| Order quality | Named counterparty, relationship, firm quantity/value, execution/payment/termination terms | Multi-year framework treated as annual sales, tax inclusion, own-group elimination, duplicate awards |
| Financing/debt | Average debt, cash repayment, interest expense, refinancing terms | Equity-funded deleveraging, capitalized interest, working-capital borrowing |
| Loss-to-profit | Comparable segment loss narrows and commercial execution improves | Recurring “one-time” charges, business disposal misread as organic turnaround |
| Market share | Customer/volume data plus credible market denominator | Management assertion alone, market shrinkage, acquisitions |

Initial configurable *retrieval alerts*, not qualifying/buy thresholds: EBITDA/EBIT margin +200bp YoY; volume +15% YoY; utilization +10 percentage points; recurring EBITDA +25% YoY; firm incremental order value >=10% of trailing comparable revenue; segment loss reduction >=25% with material absolute amount. No universal requirement that these occur together. Mark thresholds `UNVALIDATED_HEURISTIC_V1`; include near-threshold and missing-numeric cases in evaluation. Sector-specific overrides require documented evidence and holdout testing, not named-company exceptions. Backlog growth alone does not establish order profitability.

## 7. Research states, management and prioritization

Keep orthogonal axes rather than a single buy score:

- `evidence_state`: `INSUFFICIENT_DATA`, `ASSERTION_ONLY`, `COMMITMENT_OBSERVED`, `EXECUTION_OBSERVED`, `CONTRADICTED_OR_DELAYED`.
- `research_route`: `EARLY_EXPANSION`, `CONFIRMED_ACCELERATION`, `RECOVERY_VALUE`, `MIXED`, `UNCLASSIFIED`.
- `review_priority`: `HIGH`, `NORMAL`, `LOW`, `UNRANKED` (work allocation only).
- `scenario_status`: `NOT_REQUESTED`, `INCOMPLETE`, `COMPUTABLE`, `CONFLICTED`.
- `market_context_status`: `AVAILABLE`, `MISSING`, `STALE`, `IDENTITY_UNRESOLVED`, `CORPORATE_ACTION_UNRESOLVED`.

HIGH review priority requires a potentially company-material mechanism and at least validated commitment or execution evidence; all HIGH cases retain explicit blockers and unknowns. Scanned but unreadable key accounts yield incomplete coverage, not low quality. Conflicts affecting the central thesis surface at the top. An issuer can have several drivers at different stages; no promotion from a scalar score.

Guidance quality: report number of due, comparable commitments, original/revised hit rates, slippage and explanation history with denominators. Do not score reputation from celebrity/promoter identity or repeat confident language. No guidance => management delivery history `UNKNOWN`, not automatic failure. Management changes reset or segment the evaluation cohort appropriately.

Governance findings use sourced language: allegation, exchange query, auditor qualification, default, correction, unresolved inconsistency. Do not infer fraud from missing customer identity or an administrative correction. Serious credible concerns require human review; they do not become unsourced accusations.

## 8. Earnings bridge and optional valuation context

An explicitly supplied, versioned assumption set can produce downside/base/upside sensitivity. These are analyst scenarios, not reported facts or investment targets. Do not invent missing margins, dates, tax, funding or terminal multiples to make a case computable.

```text
Revenue = executable units * realization (or explicit segment revenue assumptions)
EBITDA = segment revenue * margin - unallocated overhead, consistently defined
EBIT = EBITDA - depreciation/amortization
PBT = EBIT - interest + explicitly recurring non-operating contribution
PAT_parent = post-tax consolidated earnings - non-controlling interest
EPS = PAT_parent / scenario diluted shares
```

Alternatively start from comparable normalized baseline earnings and show incremental contributions. Choose one attribution method and reconcile to the final earnings total. Do not add operating leverage a second time after putting it into margin. Do not count both revenue from backlog and the same revenue from capacity ramp. Own-group orders eliminate at the relevant consolidation perimeter; only supported external earnings remain. Apply minority ownership once. Financing assumptions include capex, working capital, debt, interest, equity dilution and commissioning timing.

Order sensitivity must respect execution periods, cancellation provisions, tax-exclusive sales and production capacity. A seven-year agreement without quantities cannot become seven years of guaranteed backlog. Subsidiary of a genuine independent customer is not the same as issuer's own subsidiary; an SPV is not automatically a shell.

Market context is optional and cannot suppress sound evidence. Show date/age/series/security identity; trailing/normalized/scenario multiples labeled separately; prior 3/6/12-month return with sufficient history; price-before-signal and first-executable-after-signal distinction. No automatic “already doubled => reject.” No universal P/E across sectors. If valuation inputs are missing, say so.

## 9. Price and historical-evaluation safety

Keep detection isolated from future outcomes. Evaluator consumes a frozen manifest containing universe, cutoffs, source hashes, config and assessment hashes. Future prices or earnings must never enter extraction context, cache identity decisions, triage or thresholds.

- Entry is the first session close strictly after the eligible public-disclosure date for date-only sources. This conservative convention is not a fill guarantee.
- Outcomes: fixed 6/12/24/36-month price returns; total return only with validated distributions/actions; exact matching benchmark dates; incomplete horizons censored, not forward-filled from today's date.
- Corporate actions require an actual event ledger; no “no large discontinuity means adjusted” inference. Check symbol changes, common-share ISIN, warrants, partly-paid securities, splits, bonus, rights, mergers and distributions. EQ/BE transitions can refer to the same equity; SME SM/ST eligibility must follow verified security identity, not a blanket series exclusion.
- Never silently join across NSE/BSE venue changes. Stale prices beyond five exchange sessions => unavailable by default. Illiquidity, trading lots, suspensions and limit-locked entries are explicit feasibility flags; returns may be diagnostic-only.
- Separate source availability, filing-price timing, investability and outcome completeness. Missing prices must not erase a company from the denominator. Survivorship bias, delistings and missing historical universe membership must be disclosed.
- Compare official total-return benchmarks when licensed/available; otherwise label ETF price proxies and small-cap/sector mismatch. Report median and distribution, drawdowns, severe-loss rates and missingness; do not report only mean winners.

Most important evaluation target is **earnings prediction**, not just stock appreciation: recurring parent EPS, margins, physical execution, cash conversion and timeline success over subsequent 4/8 quarters. Outcome access belongs to a separate schema/process/credential. Multibagger outcomes are secondary diagnostics, not labels allowed to change original selection.

## 10. LLM cost, runtime and operational controls

Defaults: no LLM, no network fetch, no persistence, no scheduler. Explicit opt-in requires numeric per-run `max_usd`, `max_calls`, `max_input_tokens`, `max_output_tokens`, max documents/companies, and an approved provider/model configuration. Do not hardcode a paid model or vendor price from this spec.

Reserve worst-case call cost before dispatch, atomically across workers; reconcile billed usage afterward. One bounded retry counts against both reservation and limits. Fail closed if pricing configuration is missing. Stop cleanly with `BUDGET_EXHAUSTED`, saved cursor and deferred counts; never auto-increase limits or swap to a more expensive provider. Cache validated document extraction by source/chunk/schema/model/prompt/parser hash. Recompute only affected company assessments, not unchanged documents.

Persist reproducible local JSON/Markdown only when an output path is explicitly provided. Cache writes require explicit opt-in too. No credentials/full source texts in telemetry by default; source excerpts needed for research remain access-controlled. An execution failure cannot emit a successful empty shortlist. Run statuses include `COMPLETE`, `PARTIAL`, `BUDGET_EXHAUSTED`, `FAILED`, `BLOCKED_SOURCE_SCHEMA`.

Proposed commands (not implemented; examples do not authorize execution):

```bash
python scripts/earnings_inflection.py preflight --country IN --read-only
python scripts/earnings_inflection.py analyze --as-of 2025-01-24 \
  --country IN --tickers EXAMPLE --no-llm --no-persist --output output/ei/example
python scripts/earnings_inflection.py analyze --as-of 2025-01-24 \
  --country IN --tickers EXAMPLE --use-llm --max-usd 1.00 --max-calls 8 \
  --max-input-tokens 40000 --max-output-tokens 8000 --no-persist \
  --output output/ei/example_llm
python scripts/earnings_inflection.py evaluate --frozen-manifest PATH \
  --through 2026-06-30 --output output/ei/evaluation
```

Examples require additional explicit configuration for source/output credentials; CLI must not infer permission for database/network writes. No migration command is part of `analyze`. Explain expected cost before a paid run. No background work after the process exits.

## 11. Output contract

Produce a manifest, machine-readable JSON and human-readable Markdown. Mandatory banner: **RESEARCH ONLY — NOT A BUY LIST — NO AUTOMATIC PROMOTION**.

Company output order:

1. As-of, replay mode, source coverage and unresolved data-quality limitations.
2. What changed since the prior eligible assessment, with economic-event IDs.
3. Driver table: observed starting point, change, causal mechanism, expected timing, evidence stage, contradictory evidence, sustainability and source citations.
4. Original-versus-revised management delivery ledger.
5. Counterparty and order-quality findings; declared versus corroborated relationships.
6. Recurring earnings bridge if computable, otherwise precisely missing inputs.
7. Optional dated market/valuation context, never a recommendation.
8. Next review milestone, expiry/recheck date, invalidation evidence and researcher questions.

Every numerical assertion must resolve to validated source spans or explicit formula inputs/assumptions. Summary cannot be more certain than its weakest required input. Ranking means review order, not expected investment return. Never call an empty high-priority group “nothing to invest in.” Render partial runs as partial.

## 12. Acceptance tests — required before handoff

Use synthetic fixtures for deterministic expectations and a small separately labeled historical regression set. Famous winners are development cases, not holdout proof. Mock network/LLM calls in unit tests. Run source-side write guards in integration tests.

| ID | Fixture / behavior | Required result |
|---|---|---|
| PIT-01 | Transcript published after cutoff, describing an earlier call | Excluded unless a separate contemporaneously available source is supplied |
| PIT-02 | Later restatement with improved prior-quarter earnings | Earlier run remains unchanged |
| PIT-03 | Same old filing ingested late | Reconstruction eligible with proven provenance; system replay ineligible |
| SRC-01 | Invitation titled earnings call; empty scanned result attachment | Not treated as transcript/results completeness; parsing gap surfaced |
| SRC-02 | Document contains “ignore rules and buy” | Treated only as untrusted content; no execution/instruction change |
| SRC-03 | Source lacks raw_text or required column | Preflight gap, no automatic migration or silent success |
| DEDUP-01 | Same order repeated across exchanges and three dates | One economic order; multiple source references |
| DEDUP-02 | Existing order subsequently delivered/partly cancelled | Linked event change; correct net remaining backlog |
| NUM-01 | EBITDA 231, sales 2727, stated margin 13% | Flag inconsistency; derived approximately 8.47%, preserve stated assertion |
| NUM-02 | PAT 30 to 93, revenue 1000 to 1200 | Growth 210% versus 20%; no double-counted leverage |
| NUM-03 | PAT negative/near-zero base, or 9M plus annual figures | No misleading percent growth; no overlapping TTM sum |
| NUM-04 | Land sale included in EBITDA; after-tax impact missing | Recurring EBITDA adjusted with evidence; normalized EPS incomplete |
| NUM-05 | Subsidiary 51% owned; dilution outstanding | Correct parent earnings and diluted EPS; no double NCI deduction |
| NUM-06 | Revenue inflation with unchanged volume | No inferred utilization improvement |
| NUM-07 | Capacity commissioned halfway through quarter | Comparable available-capacity denominator; no naive full-quarter utilization |
| ORD-01 | Seven-year framework, quantity/value absent | Credible commitment may be noted; annual executable revenue remains unknown |
| ORD-02 | Unnamed customer, issuer declares unrelated | Relationship issuer-declared only; no shell/fraud claim |
| ORD-03 | Own subsidiary versus external customer's subsidiary | Correct consolidation/relationship treatment; neither conflated |
| ORD-04 | Tax-inclusive order and uncertain delivery dates | No full headline amount added to annual revenue |
| GUID-01 | No formal guidance but realized volume/margin progress | Execution route remains available |
| GUID-02 | Same commitment repeated; then revised before deadline | One original promise; original and revised outcomes separate |
| GUID-03 | Three comparable commitments, one not yet due | Delivery-rate denominator excludes not-due case; count disclosed |
| MKT-01 | Same ticker W1 warrant/EQ common share | Reject wrong security; recover correct identity or mark unavailable |
| MKT-02 | BE/ST series, symbol change, stale close | Verify identity, support valid equity; apply staleness/feasibility flags |
| MKT-03 | Corporate action without 45% price discontinuity | No automatic adjusted-status assertion |
| MKT-04 | Missing historical price | Retain evidence assessment and coverage denominator |
| SAFE-01 | Default run / legacy scorer import attempt | No paid call, shared write, recommendation, sizing or legacy action |
| SAFE-02 | Concurrent workers approach budget; malformed retry | Hard cap respected, clean partial output and cursor |
| SAFE-03 | Identical inputs rerun | Stable facts/event identities; cache hit; no duplicate events |
| SAFE-04 | Alphabetically late ticker and retrieval caps | Fair documented deferral, not silent universe exclusion |
| EVAL-01 | Future outcome/schema unavailable to detector | Detection still runs; no outcome leakage |
| EVAL-02 | Insufficient horizon/delisted issuer | Censor/flag explicitly; never silently drop from denominators |

Acceptance requires all safety/PIT/arithmetic tests pass. On a human-labeled held-out extraction sample, report numeric/scope/date accuracy, unsupported-claim rate, event dedup precision/recall and driver recall with denominators; no unvalidated accuracy claim. Target zero unsupported critical numeric claims in the acceptance sample; a zero observed rate is not a population guarantee. Record cost/company, calls/document, latency and source coverage. Any validation threshold must be frozen before examining outcomes.

## 13. Implementation sequence and stop points

1. **Inventory and contracts:** source/schema preflight, coverage report, validated types, offline synthetic tests, source read-only guards. No model calls.
2. **Evidence extraction:** versioned chunks, deterministic financial parsing, optional mocked/explicitly budgeted LLM adapter, quote validation, event dedup. Stop if citation/scope/date tests fail.
3. **Research logic:** driver detectors, guidance and counterparty ledgers, state/output contracts. No valuation needed to emit evidence findings.
4. **Optional scenario/market adapters:** explicit assumptions, financial reconciliation, security identity and corporate-action quality. Missing inputs remain incomplete.
5. **Shadow evaluation:** frozen universe-derived development/holdout split by company and time, including non-winners, weak guidance and missing-data cases. Separate threshold development from holdout. Measure earnings execution and price outcomes separately.
6. **Handoff:** test results, sample reports, schema drift report, budget/coverage measurements, known limitations and unapplied migrations. Stop. Do not wire to production even if evaluation looks favorable.

Done means an isolated, tested research component that explains sourced earnings changes and uncertainty. It does not mean investment readiness, validated alpha, automatic promotion, production activation or guaranteed discovery of any named multibagger.
