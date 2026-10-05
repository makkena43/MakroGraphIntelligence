# Earnings Inflection Detector: implementation, coverage and limitations report

Spec: "Earnings Inflection Detector — implementation specification", v1.0 draft, 2026-10-04
Amendment: "root-to-shortlist change specification", 2026-10-05, WP1–WP4 (see §6)
Status: **Research-only, opt-in, NOT activated.** Nothing is wired into the guidance radar, stock selector, report pipeline, ingestion schedules, backend, notifications or production database.

## 1. Discrepancies between the specification and this repository

Found by reading the code at branch `claude/optimistic-bardeen-03g48v`. No database was introspected.

| # | Spec statement | What the repository has | Effect on this implementation |
|---|---|---|---|
| D1 | `src/makrograph/guidance_radar/`, `schema/guidance_radar_schema.sql`, `scripts/guidance_radar.py`, `tests/test_guidance_radar.py` exist as reference patterns | **None of these files exist** in this checkout. No tracked file mentions `guidance_radar`. | Nothing was borrowed from or invoked in a prototype. If the prototype lives on another branch or as untracked local files, it was not touched. |
| D2 | `mg_benchmark_prices` is an existing table | **Not defined or referenced anywhere** in the repository | The evaluation sandbox takes an injected `PriceSeries` and has no table dependency. Preflight reports whether the table exists. |
| D3 | `mg_documents` base DDL has no `raw_text`, but code reads it | Confirmed. `PGStore.update_raw_text`, `order_book_detector`, `policy_intelligence` and the pipelines read or write `raw_text`. `schema/postgres_schema.sql` doesn't define it, and `ensure_country_columns` doesn't add it either. | The read-only repository introspects `information_schema.columns`, selects `NULL AS raw_text` when the column is missing, and can fall back to parsed `.txt` files under `source.text_root`. |
| D4 | `PGStore` defaults to migrations | Confirmed: `PGStore(config)` runs `ensure_country_columns()` (ALTER TABLE …) unless `skip_migrations=True` | `PGStore` is **not used**. `PostgresReadOnlyRepository` opens its own psycopg2 connection with `default_transaction_read_only=on`, checks it with `SHOW`, rolls back after every query and rejects any non-SELECT SQL. |
| D5 | NSE bhavcopy primary key `(trade_date, symbol)`, no ISIN | Confirmed in `nse_price_fetcher.py`. `bse_bhavcopy_data` has the same key plus `bse_instrument_id`. | Detection uses no prices. Evaluation requires a series-filtered, explicitly adjusted series and rejects "no daily move > 45%" adjustment claims. `identity.is_operating_equity_series` returns `None` (ambiguous) for an unknown series. |
| D6 | BSE/NSE `filing_type` is coarse | Confirmed: `_classify_bse_subject` maps on subject words, for example any "result" or "board" goes to `board_decision`. | `document_versions.classify_document` classifies from content: dialogue structure for transcripts, call logistics for invitations, "Statement of … Financial Results" for results. Title-only classification is labelled and sets review status `NEEDS_SOURCE_CHECK`. |
| D7 | `fundamentals_snapshot` is an upserted snapshot | Confirmed (`ON CONFLICT … DO UPDATE`, `last_updated`) | Used only for a present-day industry label (a classification hint, disclosed as such). `financial_series.assert_point_in_time_source` rejects it for historical calculations. |
| D8 | The spec text | **The pasted specification stops after the §3 flow diagram.** Later sections (detailed state definitions, thresholds, phase list, acceptance criteria) were not available. | States, thresholds and phases below are my conservative reading of §0–§3. **Please supply the remaining sections.** Anything they contradict should change. |
| D9 | `order_book_detector.py` existing query | It uses `ORDER BY filed_at DESC LIMIT batch_size` over all of India | Not reused. The new reader pages per ticker by primary key with no cross-company LIMIT. |

## 2. What was delivered

```
src/makrograph/earnings_inflection/   (14 modules + __init__)
scripts/earnings_inflection.py         explicit run-once CLI
schema/earnings_inflection_schema.sql  isolated schema — NOT EXECUTED anywhere
config/earnings_inflection.example.yaml everything paid/writing defaults to off
tests/earnings_inflection/             49 tests, synthetic fixtures (fictional issuers)
```

Phases, in the order built:

1. **Contracts and guards** (`contracts.py`). Typed records and separate enums for evidence status, review status and scenario status. A guard rejects generated text containing BUY / STARTER / ACCUMULATE / position sizing / price-target language. Verbatim quotes and titles are exempt.
2. **Read-only sources plus preflight** (`source_repository.py`). Offline fixtures, and an opt-in Postgres reader (DSN from `EI_READONLY_DSN` only).
3. **Documents and identity** (`document_versions.py`, `identity.py`):
   - Content classification.
   - Availability: `published_at`. A date-only `filed_at` counts as available at the end of that day (IST). Documents with no date are excluded and counted.
   - Version linking happens *after* the as-of filter.
   - Dated symbol history.
   - Detection of financial issuers (bank, insurer, NBFC, investment company).
   - Counterparty normalisation.
4. **Extraction** (`chunking.py`, `extraction.py`):
   - Page- and table-aware chunks. Table headers and unit lines stay attached to their tables.
   - Retrieval runs oldest-first and reports anything it omits.
   - Results-statement parser maps columns to periods and scales lakh/million to crore. A table with no unit line produces no amounts.
   - Sentence evidence carries modality (realized / forward / conditional / negated, with scoped negation), tier (assertion / commitment / realized) and commitment strength (binding / provisional / non-binding).
   - Optional constrained LLM extractor: off by default, takes an injected client, keeps only quotes found verbatim in the source, and checks that the value appears in the quote.
5. **Validation and event resolution** (`validation.py`, `event_resolution.py`). Checks: verbatim quotes, units, look-ahead, claims about periods that hadn't ended, PAT = PBT − tax and EBITDA reconciliation, scope conflicts. Order deduplication ignores dates: the same order in an announcement and a call transcript is one event. A "repeat/another order" of the same size is a separate event.
6. **Series and drivers** (`financial_series.py`, `drivers.py`):
   - One scope per series, using the latest version of each figure; restatements are recorded as lineage.
   - TTM is the sum of four reported quarters. **The latest quarter is never annualised.**
   - Derived EBITDA and Q4 (FY − 9M) are labelled as derived.
   - Drivers: YoY growth, growth acceleration, margin change in bps, operating leverage, PAT growth, run of consecutive material quarters, disclosed inflow / TTM revenue, order-book cover, stated utilisation.
   - **No price or rally input.**
7. **Promises and counterparties** (`guidance_ledger.py`, `counterparty.py`):
   - The original statement is always kept, and the outcome is judged against it.
   - Later statements are recorded as reiterated, raised, lowered or withdrawn.
   - Outcomes: PENDING / MET / PARTIALLY_MET / MISSED / UNVERIFIABLE. Results get a 75-day window to be published.
   - Customer concentration and binding-strength flags come from deduplicated events only.
8. **Bridge and assessment** (`earnings_bridge.py`, `assessments.py`):
   - Assumption-based scenarios for recurring parent-attributable diluted EPS. Financial issuers get `UNSUPPORTED_FINANCIAL_MODEL`.
   - Missing inputs produce `NOT_COMPUTED_MISSING_INPUTS`, while evidence research continues.
   - Evidence status, from weakest to strongest: INSUFFICIENT_EVIDENCE → NO_MATERIAL_CHANGE → ASSERTION_ONLY → COMMITMENT_BACKED → EXECUTION_EMERGING → EXECUTION_CONFIRMED. CONTRADICTED sits outside the ladder.
   - Management that gives no guidance can still reach EXECUTION_CONFIRMED on realized results alone.
9. **Rendering, budget, pipeline, evaluation**:
   - JSON and Markdown answer the spec's five questions. Rendering fails if sources or limitations are missing.
   - Hard limits on calls, tokens and spend, enforced at reservation time, with a cache.
   - Run-once pipeline: one company's failure is recorded without aborting the run.
   - Persistence is off by default and refuses any database whose name doesn't match the test pattern.
   - `evaluation.py` is an outcome sandbox. A test enforces that no detection module imports it.

## 3. How to run (offline)

```bash
pip install pytest            # only test dependency; the package itself adds no new runtime deps
python -m pytest tests/earnings_inflection -q
python scripts/earnings_inflection.py --ticker ACMEGRID --as-of 2024-10-31
python tests/earnings_inflection/fixtures/build_fixtures.py   # regenerate fixtures
```

Fixture outcomes at as-of 2024-10-31. These show the mechanics only and are not validation:

| Fictional issuer | Scenario exercised | Status |
|---|---|---|
| ACMEGRID | order announced, then repeated in a call (deduplicated); repeat order; LoI from an unnamed buyer; guidance reiterated; 2 quarters of material growth | EXECUTION_CONFIRMED |
| SMEFAB | SME issuer reporting half-yearly; 2 consecutive half-years of material growth; one disclosed order | EXECUTION_EMERGING at 2024-10-31 (H1FY25 results are public only on 2024-11-12); EXECUTION_CONFIRMED at 2024-12-31 |
| PLAINCO | flat business; latest results have a date-only `filed_at` | NO_MATERIAL_CHANGE |
| CONTRACO | FY24 guidance of 30% growth, actual 5% | CONTRADICTED (bridge not computed: missing quarter) |
| SAMPLEBANK | bank | INSUFFICIENT_EVIDENCE (scenarios: UNSUPPORTED_FINANCIAL_MODEL) |

## 4. Coverage and known limitations

- **Live coverage unknown.** Production document counts by source and type, text availability and timestamp completeness are unmeasured. Running `--source postgres --preflight-only` against a read-only role produces this. It hasn't been run.
- **Text dependency.** Detection needs `raw_text` or parsed `.txt` files. Scanned PDFs, image-only annual reports and parser-dropped tables yield nothing. No re-parse into isolated output is wired yet; `parser/pdf_parser.py` would be the starting point.
- **Lexical extraction** is precision-oriented and will miss:
  - paraphrased guidance ("high-twenties growth")
  - numbers that sit in slides
  - Hindi or regional-language text
  - segment tables
  - cash-flow statements (operating cash flow is not parsed from tables yet)
- **Results-table parser** handles these layouts:
  - Q1–Q4 statements, including half-year, nine-month and year-ended columns. The cumulative block is identified by repeated end dates, so the year-ago quarter is never confused with a cumulative column.
  - SME half-yearly and annual-only statements.
  - Date headers as `dd.mm.yyyy`, `dd.mm.yy`, `30-Jun-24`, `Jun-24` and `June 30, 2024`, and fiscal-label headers such as `Q3 FY25`, `9M FY25` and `FY24`.

  It still needs period columns on one header line. Dates split across lines, merged headers and segment-reporting tables need fixtures drawn from real filings.
- **SME half-yearly reporters** are analysed in half-years:
  - Year-on-year growth compares the same half a year earlier.
  - Trailing 12 months is the sum of the last two halves; the latest half is never doubled.
  - When only H1 and the full year are filed, H2 is derived as FY − H1 and labelled as derived.
  - The bridge uses the same cadence, and its base period is shown as, for example, "TTM to H1FY25".
  - Persistence means two consecutive half-years, a full year of evidence, so changes surface up to six months later than for quarterly reporters. Each report states this.
  - The cadence is chosen per company. After an SME-to-mainboard migration, the company stays half-yearly until four quarters with year-ago comparisons exist.
- **Point in time:** `filed_at` alone gives day resolution. Exchange dissemination timestamps should be preferred wherever fetchers store them.
- **Identity:** without a supplied symbol history the ticker is assumed stable, and this is disclosed in each output. SME migration, mergers and renames need a curated history.
- **Financial issuers:** the bank / NBFC / insurer classification uses industry metadata, then strong name keywords, then statement layout. Weak keywords ("Capital", "Finance") leave the model UNKNOWN for review.
- **Signs and FX:** exceptional items are assumed positive = charge. USD amounts are flagged, not converted.
- **No statistical claim.** Thresholds are descriptive defaults and were **not tuned to named historical winners**. The evaluation sandbox exists, but no backtest was run.
- **US adapter:** the contracts allow other countries and fiscal periods, but the fiscal-label helpers assume India's April–March year. A US adapter (SEC acceptance timestamps, CIK mapping) is a later, explicit phase.

## 5. Requires explicit authorisation before proceeding

- Running the read-only preflight against the production database (needs a read-only role and its DSN).
- Applying `schema/earnings_inflection_schema.sql` to a test database (it hasn't been executed anywhere), and later to production.
- Enabling the LLM extractor or any paid run, which means setting budget limits above 0.
- Any integration with the existing radar, selector, report pipeline, schedules, UI or notifications.

## 6. Root-to-shortlist amendment (2026-10-05): WP1–WP4

Each work package was finished, tested and pushed before the next one started:

| Commit | Work package |
|---|---|
| `05a5e6a` | WP1 |
| `b3fa38e` | WP2 |
| `20a8a9f` | WP3 |
| `8e7063f` | WP4 |
| this commit | docs, config and schema |

All tests are offline. 201 pass across `tests/earnings_inflection`, `tests/test_ingestion_pdf_text.py` and `tests/test_pipeline.py`. Elsewhere in the repository, 9 test files couldn't be collected in this container because `psycopg2` wasn't installed (the same before and after these changes), and a full-repository run didn't finish within 10 minutes. **The full repository suite was not run to completion here.**

### What was **not received**

The pasted amendment stops at **WP4 item 7** ("Only validated binding external commitments may support a verified commitment classification…"). Not received, and not built:

- the WP4 acceptance criteria;
- WP5 onward, presumably universe discovery and the research shortlist.

There is still no whole-market or shortlist mode, and nothing ranks companies. Please send the remaining sections.

### WP1: ingestion-to-reader gap

**Delivered:**

- **Text artifacts.** `parser/text_artifacts.py` stores versioned, immutable, content-addressed text artifacts, keyed by document, raw hash, method and parser version.
  - It keeps page coverage: extracted vs expected pages, truncation, and the page limit.
  - Failure states are recoverable, with a retry limit. Originals are preserved.
  - A `FakeOcrProvider` covers tests. Network OCR providers are refused in code.
- **Extraction step.** `earnings_inflection/backfill.py` (`--extract`) is explicit and bounded: local originals only, `--max-docs`, and deferred documents are reported.
- **Reader.** It prefers artifacts and labels the text source (`artifact`, `raw_text`, `text_file`, `none`).
- **Coverage report:**
  - expected result periods by cadence, with filing deadlines of 45 days, or 60 for March year ends;
  - parsed vs missing periods;
  - unreadable documents by status.
- **Replay.** The run manifest records exact text versions. `--replay-manifest` pins them, so live and historical runs over the same artifacts give the same result.
- **Ingestion options.** `run_pdf_fetch_india` gains three options: `text_artifact_root`, `keep_failed_originals` and `granular_failure_status`. All default to the old behaviour, have regression tests and are **not activated**.

**Limitations:**

- Coverage of the real database is still unmeasured. The preflight and extraction have not been run against it.
- OCR quality is untested. Only local `ocrmypdf` is wired, and it was not run here.
- Truncation follows the parser's page limit. Very long annual reports will be `PARTIAL` by design.

### WP2: identity, provenance and point-in-time replay

**Delivered:**

- **Issuer registry.** `IssuerRegistry` holds effective-dated aliases (symbol, scrip code, ISIN, name), each with a `recorded_at`.
  - Dated securities give board and series.
  - Predecessor links are spliced only when `comparable` is true and a decision source is recorded.
  - Dated industry history is supported.
- **Replay modes.** `PUBLIC_INFORMATION_RECONSTRUCTION` and `SYSTEM_KNOWLEDGE_REPLAY`.
- **Present-day context.** Present-day metadata appears only as labelled context. It is never used for calculations or the listing segment.
- **Derived signals.** Every derived signal carries `knowable_at`, the latest public input, and `system_known_at`, the latest time the system held that input. A signal is never dated before its newest input.

**Limitations:**

- The registry is empty for real companies; it needs curated data.
- `SYSTEM_KNOWLEDGE_REPLAY` treats `mg_documents.created_at` as first-seen time. Legacy `raw_text` has no provable text time, so today's data mostly falls out of that mode, and the coverage block says so.
- Corporate-action share adjustments are not modelled.

### WP3: financial integrity before qualification

**Delivered:**

- **Reconciliation.** `reconcile_structured` gives each check a status: `validated`, `definition_difference`, `unresolved` or `rejected`.
  - Checks: PAT = PBT − tax, owners + NCI, exceptional-item sign, EBITDA definition, cash-flow identity, balance-sheet identity, and negative revenue.
  - Tolerance is the larger of 2 display units and 0.5%.
  - Blocking rows are removed from the series and listed. They never feed growth or margin calculations.
- **Zero-PAT fallback fixed.** A zero or missing parent-attributable PAT no longer silently falls back.
- **Reported vs recurring earnings:**
  - The exceptional-item sign is proven from "profit before exceptional items".
  - Recurring PAT is not invented when the after-tax effect is undisclosed; recurring PBT is used and labelled instead.
- **Changes from a loss.** Loss to profit, loss narrowed and low base are labelled instead of reported as huge percentage growth.
- **Balance-sheet columns.** "As at" columns are read as instants, with current and non-current borrowings kept separate.
- **Cash flow and share basis:**
  - cash-flow rows are parsed;
  - share count is derived from paid-up capital and face value and cross-checked in the bridge;
  - debt, cash-conversion and share-count drivers are informational only.

**Limitations:**

- Validated only on synthetic fixtures and the earlier PANACEABIO/POKARNA layouts. Multi-page balance sheets and Ind-AS 116 lease lines are not specifically handled.
- Segment tables are still not parsed.

### WP4: external demand (items 1–7 as received)

**Delivered:**

- **Dated order states.** Each order event keeps a dated stage history: enquiry, MoU/framework, preferred bidder, binding order, execution, amended, cancelled, expired.
  - A later retelling never downgrades a stage.
  - A partial cancellation reduces the current value and keeps the original.
  - A lifecycle note that matches no known order is kept as an orphan with no value.
- **Relationships.** Each status has a source.
  - "Independently supported unrelated" comes only from config reference data dated on or before the cutoff.
  - The SEBI "not a related party" answer stays an issuer assertion.
  - Customer verification is anonymous, issuer-named or corroborated.
- **Contract economics.** Each order records:
  - value basis: firm, ceiling, guaranteed minimum or executable release;
  - tax-inclusive or tax-exclusive value;
  - execution period, delivery, payment terms, termination terms, order reference and product.
  - An annual executable estimate is computed only when an execution period is disclosed.
- **Deduplication by identity:**
  - Orders merge on the same reference, or on the same named customer, amount and compatible product.
  - Different references stay different orders.
  - Unnamed same-amount mentions are linked as ambiguous and counted once.
  - The sentence splitter no longer breaks after "No.", "Rs." or "Ltd.".
- **Demand kept separate.** `demand.py` reports verified inflow, unverified (early-lane) inflow, related-party exclusions, cancellations and a fresh backlog snapshot (at most 200 days old) separately. Backlog is never added to inflow.
- **Classification.** Only validated binding external orders support `COMMITMENT_BACKED`. Everything else that is material appears as `EARLY_COMMITMENT_UNVERIFIED` with per-event reasons. Nothing is discarded or upgraded.

**Limitations:**

- Extraction is lexical, so stage and economics wording outside the tested phrasings will be missed and the field marked unresolved. Hindi and regional-language text, and orders that appear only in tables or slides, are not read.
- Customer verification beyond "named by the issuer" needs reference data supplied by you. There is no registry lookup (MCA or similar), because that would need network access.
- Whether an order is incremental or replacement business, and its margin, are not inferred.

### Test database schema (not executed against any shared database)

`schema/earnings_inflection_schema.sql` gains:

- run-manifest columns on `ei_runs`;
- `EARLY_COMMITMENT_UNVERIFIED` in the status check, with an in-place upgrade for an existing test database;
- new tables `ei_source_versions`, `ei_extraction_results`, `ei_events` and `ei_event_states`.

`persist()` writes the manifest, source versions, events and event states. It still refuses any database whose name doesn't match the test pattern.

The schema was applied twice, fresh and as an upgrade from the previous version, to a **throwaway local Postgres 16 instance** created and deleted in this container. A 5-company fixture run was persisted there.

### Safety fix found along the way

The persistence guard read the database name with a regex. That regex also matched socket paths: `host=/run/x_test dbname=makrograph` was treated as a test database. It now reads only the `dbname` keyword or the URI path, with regression tests.

### Boundaries kept

- Not touched: stock selector, guidance radar, constraint logic, schedules, portfolio actions and production data.
- Not run: production migrations, network backfills, paid LLM or OCR, and notifications.
- No action labels or position sizes are produced.
- Thresholds were not tuned to named historical winners.
