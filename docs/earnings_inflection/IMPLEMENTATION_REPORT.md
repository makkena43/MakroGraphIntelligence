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

### What was **not received** (resolved later: the full amendment arrived as `IMPLEMENTATION_CHANGE_SPEC.md`; see §8)

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

## 7. Real-filing validation: 8 stocks, as of 2024-03-31

The filings were exported read-only from the user's database with `scripts/export_ei_fixture.py` and committed to `filings_export/2024-03-31/`. BSE and NSE refuse requests from cloud servers, so nothing was downloaded.

The 8 stocks were chosen **because they later rose**. This is a parsing and evidence check, not a performance test. No control group has been run, and no threshold was tuned to these names. The one rule change, the operating-leverage floor described below, *removed* a hit (ASALCBR).

### Parsing failures found and fixed

All fixes are general, and each has a regression test built from the real text (`test_real_filing_layouts.py`).

| Failure | Stock | Fix |
|---|---|---|
| Words run together in the text layer ("Revenuefromoperations", "(Rs.inlakhs)") | INDOTECH | De-spaced label and unit matching |
| Scanned header dates ("31-Pec-22", "Decomber3l", "203") | INDOTECH, SHAILY | SEBI Reg. 33 column layout anchored on the readable dates or the statement title, used only when its width equals the data rows. A non-note leading value is never dropped (that had shifted every column). |
| Month-day line over a year line | SHAILY | Header joined |
| "Dec31,2023", "Dec 31,2022" | REFEX | Tokenizer |
| Consolidated and standalone side by side (12 columns); "Quarter" and "Ended" on separate lines; "Quamir" | PGIL | Halves split, each scope taken from the column-group line, per-column words |
| Header cut into its own chunk by a long "(Unaudited)" line | PGIL | Lines just before the table are used, only if widths match |
| Figures with dots for commas ("11.30.069", "2.42,845.54"), split figures ("1,1 0,377.07"), invalid grouping ("47,2563") | DEEPAKFERT, PGIL | Repaired when the result is a valid grouping; otherwise a positional unreadable cell |
| Decimal points lost in scanning ("4733118" for 47,331.18) | SHAILY, SUPRIYA | Integer cells in a decimal-printed table are unreadable, not 100x values |
| Segment table "Total income from operations" read as company revenue | DEEPAKFERT | Segment sections excluded |
| Scope and unit taken from the first title or unit line in a single-page document | DEEPAKFERT, SHAILY | Nearest title and unit line above the table |
| Press-release highlight tables ("Revenues (in Rs Cr) 140.07 105.14"; YoY/QoQ columns between periods) | SUPRIYA, GOLDIAM | Short labels, but only in company-level tables outside presentations; unit from the row label; change columns kept positional |
| Labels and values on separate lines, values above labels | ASALCBR | Paired in the direction where revenue + other income = total income |
| Figures on a section-heading line | ASALCBR | Labels treated as displaced; revenue and other income not used |
| A filing "superseded" by an unreadable re-filing lost its numbers | INDOTECH | Only figures the later version provides are replaced |
| Q4 derived for a non-March "FY" | PGIL | Derivation requires a March year end |
| Thousands commas scanned as dots in a whole-lakh table ("17.511" next to "14,467") made the table look 3-decimal, so no repair ran and revenue failed the total-income identity | INDOTECH (2024-25 filings) | A table with comma-grouped whole amounts and no "1,234.567" figures is a whole-number table; its total-income line is repaired the same way (`test_scanned_separators.py`) |
| "ratio" matched inside "ope*ratio*ns", so revenue rows were treated as ratio rows | all | Word boundaries on margin / ratio |
| Bare SEBI letter column ("A      Revenue from operations") not stripped | INDOTECH | Single capital letter + spaces before a capitalised word is an enumerator |
| A space scanned inside a cell of column-aligned text ("50 321", "1 ,917") | INDOTECH | Merged only when the row's columns are 2+ spaces apart; single-space text is unchanged |
| Unit line unreadable ("(Rq In lakh*]", "fFls in hkhc\\") | INDOTECH | Figures kept aside; unit adopted only when ≥3 comparative figures match the issuer's **earlier** filings at exactly one standard unit and ≥80% of compared figures agree; quote marked `[unit inferred: …]`; otherwise unused |

### Series rules added

- **Scope:**
  - One scope per series. The preferred consolidated scope is not used if it is stale or has no year-ago comparison; the reason is disclosed.
  - A company whose statements only ever name one scope gets its unlabelled statements merged into that scope, with disclosure.
- **Repeated figures:**
  - A statement figure outranks a highlight figure.
  - A rounded re-statement of a figure is not a revision.
  - When filings disagree, the value stated by more separate filings wins. Reposts within 5 days count once, and the conflict is recorded.
  - Copies of one statement inside a filing must agree by majority, or the figure is not used.
- **Outliers:** a single-filing value 10x away from the company's median period is excluded and listed.

### Guidance fixes behind three false CONTRADICTED results

- A "target" stated after its period ended is a report, not guidance. This affected DEEPAKFERT (Q3FY24 stated on results day) and SHAILY (Q3FY23).
- Statements without a target period are not compared as revisions of one target. This affected SUPRIYA's "352 crores" / "1,000 crores" across years.
- A growth percentage must sit next to its growth word. Run-on slide text is not a guidance sentence. This affected DEEPAKFERT, where a "67%" warehouse share was read as growth guidance.

### Operating leverage

The EBITDA/revenue-growth ratio is not material when revenue grew by less than 5%. That threshold is configurable. ASALCBR's "6.1x" came from +3.7% revenue.

### Result at 2024-03-31 (Q3FY24 YoY, as parsed)

| Stock | Before | After | Revenue | EBITDA margin | PAT |
|---|---|---|---|---|---|
| INDOTECH | INSUFFICIENT_EVIDENCE | EXECUTION_EMERGING | +66.8% | +485 bps | +178% |
| SUPRIYA | CONTRADICTED | EXECUTION_EMERGING | +33.2% | +1,626 bps | +213% |
| SHAILY | ASSERTION_ONLY | EXECUTION_EMERGING | +16.3% | +582 bps | +156% |
| GOLDIAM | EARLY_COMMITMENT_UNVERIFIED | unchanged | +10.5% | -29 bps | +12.5% |
| ASALCBR | ASSERTION_ONLY | unchanged | +3.7% (the old "-13.7%" was a mis-read) | +162 bps | +16% |
| PGIL | ASSERTION_ONLY | unchanged | -1.8% | n/a | -9.6% |
| REFEX | ASSERTION_ONLY | unchanged | -20.7% | +75 bps | n/a |
| DEEPAKFERT | ASSERTION_ONLY | unchanged | -32.7% | n/a | -76% |

The last five did not show an earnings inflection in filings public by March 2024. For those, the later price move was not visible in reported earnings at that date. That is consistent with re-rating or later-quarter earnings, and is not something this evidence detector should flag.

### Remaining gaps

- **Missing quarters:**
  - SUPRIYA's older ₹-million statements are heavily scanned, so only 4 quarters parse.
  - SHAILY Jun-2023, REFEX Jun-2023 and GOLDIAM Mar-2023 are missing, which blocks TTM and the bridge for those names.
- **Utilisation:** the "capacity utilisation change" assertion (e.g. PGIL 95% to 34%) compares statements that may not share a scope. Mechanism-specific utilisation is WP5.
- **Damaged text layers:** the INDOTECH Sep-2025 results scan (Nov 2025) has its revenue row merged into prose; that quarter stays missing, and later as-of dates report a stale series instead of a status built on old numbers.
- **Unextracted documents:** 7 documents have no text, with `local_path` = `UNSUPPORTED_FORMAT`. The ingestion stage could not handle them, and no original remains to extract.
- **Next:** a pre-registered control group of non-winners, chosen before looking at statuses, is still needed. Without it, no claim can be made that these states separate winners from the rest.

## 8. WP5-WP9 (full amendment, `IMPLEMENTATION_CHANGE_SPEC.md`)

| Commit | Work package |
|---|---|
| `1239974` | WP5 |
| `983ec58` | WP6 |
| `711c0a9` | WP7 |
| `18e87b6` | WP8 |
| this commit | WP9 and docs |

**Tests:** `python -m pytest tests/earnings_inflection tests/test_ingestion_pdf_text.py tests/test_pipeline.py -q` passes **304** tests, all offline.

**Not run:** the full repository suite was not run to completion here (see §6).

**Activation:** everything is off or run-once. There are no schedules, notifications or production writes. The LLM, valuation and persistence are disabled by default.

### Requirements matrix

Legend: **T** = implemented and tested offline. **L** = implemented but limited or unverified on real data. **B** = blocked by an external dependency. **D** = deferred.

| Req | Implementation | Verification | State |
|---|---|---|---|
| **WP5.1** Utilisation: same scope, capacity denominator | `mechanisms.utilization` | `test_wp5_mechanisms` (plant A vs B, same plant, capacity change) | T; real-world utilisation statements are noisy (L) |
| **WP5.1** Mix | segment revenue/result, or stated mix as a hypothesis | positive / adverse / too small / stated / missing | T; segment parsing is new on real scans (L) |
| **WP5.1** Pricing / input costs | gross margin on the usual cost definition; cause only when stated | confirmed at low growth, adverse, reversible tailwind, missing | T |
| **WP5.1** Order quality | WP4 events, payment terms, timing, cancellations | commitment / adverse / missing | T |
| **WP5.1** Debt reduction | net debt, with funding source: operating cash vs equity vs asset sale | operating- vs dilution-funded, adverse, missing | T |
| **WP5.1** Segment turnaround | segment loss → profit, with persistence | emerging / confirmed / adverse / missing | T |
| **WP5.1** Organic volume / share | stated volumes; acquisitions and price separated | confirmed, inorganic, decline, missing | T |
| **WP5.2** States, magnitude, timing, durability, attribution, confidence, invalidators; ADVERSE for negatives | `MechanismResult` | all WP5 tests | T |
| **WP5.3** No 25% growth requirement; no double counting; research hypothesis | `decide_status` upgrade on CONFIRMED mechanisms; `overlaps_with` | low-growth confirmation; overlap test | T |
| **WP6.1** Provider adapter, CLI, one-time preflight, fake adapter | `llm.py`, `llm_provider_anthropic.py` | CLI with fake provider; CLI preflight exit 2 | T; Anthropic adapter not called here (no paid run) |
| **WP6.2-3** Deterministic tables; LLM narrative only; field validation; injection-safe | `select`, `validate_item` | 10 invalid-interpretation cases, injection test | T |
| **WP6.4** Cache key; budget before dispatch; failed-call charge; cost disclosure | `LLMEvidenceExtractor`, `Budget.summary` | cache, budget-stop, failure tests | T |
| **WP6.5** Immutable revisions; original vs latest; track record with sample size | frozen `GuidanceRevision`, `management_track_record` | immutability, both outcomes | T |
| **WP6.6** Selective chunks; failures counted, never "no signal" | stats in coverage and manifest; PARTIAL status | budget / failure tests | T |
| **WP7.1** Fiscal / base matching | `_management_case` | FY base, FY26/FY27 mismatch, unreported base | T |
| **WP7.2** Downside / base / upside; D&A from capex; funding, tax, NCI, dilution | `build_bridge`, assumption register | capex funding cases, cited downside | T; working-capital timing only as cash notes (L) |
| **WP7.3** Mechanism contributions; unsupported inputs labelled | `mechanism_contributions`, register sources | rendering on fixtures | T |
| **WP7.4** Optional valuation: as-of price, series, dilution, corporate actions | `valuation.py` | as-of price, split/bonus cutoff, series mismatch | T; no production market adapter (B) |
| **WP7.5** Price-change context, never rejecting; no future returns | `price_change_context`; evidence unchanged | valuation-never-changes-status test | T |
| **WP8.1-2** Explicit universe, contemporaneous snapshot, limits, fair order, denominators | `discovery.py` | order independence, limited cohort, missing documents | T; no production snapshot (B) |
| **WP8.3** Incremental run-once | `--previous` with document fingerprints | delta and carry-forward test | T; predecessor-issuer dependencies not re-propagated (L) |
| **WP8.4-6** Lanes, per-mechanism states, visible ranking, critical checks, no padding | `lane_for`, `score_components` | deterministic lanes, empty lane not padded | T |
| **WP8.7-8** Shortlist content, counts, immutable runs | `shortlist.json` / `.md` | content and counts tests; `exist_ok=False` | T |
| **WP9.1** Returns outside detection; frozen config | `evaluation.py` isolation, `FrozenConfig` | import guard; leaked outcome field changes nothing | T |
| **WP9.2** Company-neutral cohort | `build_cohort` | determinism, later-delisted inclusion | T; real cohort data not available (B) |
| **WP9.3** Coverage, extraction, classification P/R, delay, burden | `classification_report`, `extraction_accuracy` | synthetic labelled run end to end | T on synthetic labels; no real labels yet (B) |
| **WP9.4-5** Lane returns: next tradable entry, costs, liquidity, delistings, benchmarks, censoring, overlap | `lane_outcomes`, `lane_return_report` | censoring and costs tests | T on synthetic prices; no verified price data wired (B) |
| **WP9.6** Shadow pilot plan | `SHADOW_PILOT_PLAN.md` | — | D (plan only; not started) |

### Real-data observations (8 exported stocks, 2024-03-31)

Mechanism outputs on the export are plausible after two fixes found on real data:
- an OCR thousands-dot in integer tables;
- a usual-cost-definition rule for gross margin.

Two examples:
- **INDOTECH:** gross margin is *down* 235 bps YoY while its EBITDA margin is up 485 bps, i.e. operating leverage on fixed costs.
- **DEEPAKFERT:** the fertiliser segment swung to a loss (ADVERSE).

Most bridges on the export are NOT_COMPUTED, and they name the missing quarter (e.g. depreciation for Jun-2023), because some older quarters still do not parse. This is shown, not filled.

### End-to-end fixture commands

```bash
python scripts/earnings_inflection.py --ticker ACMEGRID --as-of 2024-10-31                    # single issuer
python scripts/earnings_inflection.py --fixtures filings_export/2024-03-31 --ticker INDOTECH --as-of 2024-03-31 --diagnose
python scripts/earnings_inflection.py --universe tests/earnings_inflection/universe/snapshot_2024-10-31.json \
    --as-of 2024-10-31 --runs-root /tmp/ei_runs                                               # universe scan
python scripts/earnings_inflection.py --ticker ACMEGRID --as-of 2024-10-31 --replay-manifest OUT/manifest.json
python -m pytest tests/earnings_inflection/test_wp9_validation.py -q                          # evaluation
```

Example research shortlist (synthetic): `docs/earnings_inflection/examples/research_shortlist_synthetic.md`.

### Production-readiness checklist (all open)

**Data access**
- [ ] Read-only database role and preflight output for the real `mg_documents`.
- [ ] Explicit text extraction run for the tracked universe, with coverage reviewed.
- [ ] Results-parser accuracy measured on at least 50 keyed statements across layouts (`extraction_accuracy`).

**Universe and identity**
- [ ] Production eligibility snapshots with contemporaneous membership, including delistings.
- [ ] Dated issuer registry: NSE symbol, BSE scrip code, ISIN, renames, SME migrations.

**Prices and costs**
- [ ] Verified adjusted price series per security and series, plus a sourced corporate-action file (no provider is wired).
- [ ] Source permissions and terms for exchange filings and prices.
- [ ] LLM cost estimate per run. Roughly: (selected chunks × ~1.5k input + ~0.5k output tokens) at $4 / $20 per million tokens for `claude-opus-5-5`. Confirm before any paid run.

**Validation**
- [ ] Independent reviewer labels and a pre-registered control cohort.
- [ ] Shadow pilot approved and started.

Do not treat passing tests, or attractive backtests, as investment readiness.

## 9. Forward-looking thesis layer (after the INDOTECH replay review)

The evidence-status ladder is backward-looking: it needs reported results and two periods to confirm.
Shortening that to one period would only make the same classifier noisier.  Instead a separate
thesis layer was added next to it (the ladder is unchanged).

| Recommendation | Implementation | Tests |
|---|---|---|
| Surface leading candidates when source-backed economics change | `thesis.leading_signals`: verified binding orders material to TTM revenue known at the time; stated order book +30% vs a snapshot 5-15 months earlier (amount must follow "order book"; a high flat book is not a change); completed capacity expansion (+20%, "from X to Y" or two dated statements); sized issuer statements of realised price / input cost / mix / volume. Bare forward statements are not leading. | `test_thesis.py` (leading before results, flat book, amount guard, unsized pricing) |
| First results as a separate validation milestone | `ThesisMilestone` on the first results published after the signal, measured on the mechanism's own metric; `pending` until the SEBI deadline, then `overdue` (data gap, not a verdict) | validated / not validated / adverse / overdue cases |
| Two-period confirmation upgrades confidence, not admission | low -> medium -> high; admission happens at the leading signal | confirmation case; evidence status unchanged while leading |
| Track each mechanism with positive and negative evidence | `MechanismThesis.positive_evidence / negative_evidence`, dated; reported outcomes listed once in `outcome_history`, attributed to no mechanism | attribution test |
| Preserve the prior thesis when data become unavailable | stale series: mechanisms computed on the last parsed period with `stale=True` (never `qualifies_positive`, never in the bridge); `last_known_status` recomputed from what was public when the latest parsed results came out | stale test |
| Measure earliest detection, false alarms, lead time, earnings delivery | `replay.py` (detection side, snapshots) + `evaluation.thesis_timeline / earnings_delivery / timeline_report` (outcome sandbox; detection never imports it); CLI `--replay-from/--replay-to` | timeline, delivery and CLI tests |

Series points now carry `first_public_at` (first filing stating the value), so a figure repeated later
as a comparative column does not move its date.

Schema version `ei-assessment-4` (adds `Assessment.thesis`, `MechanismResult.stale`).

## 10. Forward catalysts: detect a credible future earnings change, then verify it

The system was refined from "find strong reported growth" to "detect a credible future earnings change, then verify
it". It is the same pipeline; the evidence status remains as *current reported performance*.

**Correctness fixes first** (from an independent review of the thesis / replay / parser changes; each fix has a
regression test in `test_review_fixes_thesis.py`):
- order signals are dated at the binding date;
- declining mix is not a positive signal;
- old capacity signals no longer hide newer ones;
- validation uses only periods ending after the signal;
- half-year deadlines are correct;
- the stale last-known reading is rebuilt only from what was public at its cutoff;
- delivery horizons use calendar periods;
- stale readings never upgrade;
- spaced-thousands merges need two column gaps.

| Request | Implementation | Tests |
|---|---|---|
| 1. Forward catalysts trigger discovery | `catalysts.py` seeds: order-book growth or verified binding inflow, customer approvals, capacity or testing-bottleneck changes (completed, or planned with dates), utilisation, mix, stated price / input-cost changes, financing (only when finance cost is at least 10% of EBITDA), segment turnaround. Current growth is not required. `research_summary.current_performance` is kept apart. | `test_catalysts.py` |
| 2. Full credit-rating rationales | `rating_rationale.py` and the `CREDIT_RATING_RATIONALE` document kind. It extracts capacity / utilisation / expansion, capex / funding / completion, order book / horizon, concentration, payment protection, pass-through, working capital, DSCR / cover / obligations and liquidity, with observed vs expected per fact. Sensitivities are kept apart, and every dated version is kept. Agency evidence is tagged `source_role="rating_agency"` (corroborating context). Agency-website retrieval is **blocked** (network authorisation). | `test_rating_rationale.py` |
| 3. Persistent catalyst record | The `Catalyst` contract holds: first public disclosure, operating change, facts / expectations / corroboration / uncertainties, execution window, contribution, chain, milestones and invalidators. It is rebuilt from all documents by the as-of date (never overwritten). `catalyst_ledger.py` is an opt-in, append-only versioned ledger; the test DB has `ei_catalyst*` and `ei_rating_rationale`. | ledger test |
| 4. Earnings mechanism, not capex | The chain runs demand → deliverable capacity → revenue conversion → recurring profit → cash. Capacity without demand stays potential and its magnitude unresolved. Capacity contribution is bounded by demand. Downside / base / upside are given only with inputs and frozen at detection from what was public then. | capacity tests |
| 5. Discovery separate from confirmation | Stages: potential / supported / execution validating / confirmed for investment review / delayed / contradicted / data unavailable. Catalysts older than window + 12 months are history. | stage tests |
| 6. Thesis-specific confirmation | Questions are fixed at detection (orders: conversion and margins and no cancellations; capacity: commissioned and output; mix / pricing: margins; debt: interest and dilution; turnaround: segment losses). They are judged on the first two periods ending after the catalyst could contribute, singly and together. A muted quarter is consistent. Guards never validate on their own. Confirmation needs established materiality (margin catalysts may establish it from the measured change). | muted / cancelled / shelved / data-gap tests |
| 7. Investment-review gate | `InvestmentReview` opens only from a confirmed catalyst. It covers: remaining upside (with "already exceeded"), conservative valuation if a market adapter exists, cash / financing / dilution, governance flags, liquidity / downside, and missing inputs. It is never an action. | review test |
| 8. Early detection, not hindsight | `evaluation.catalyst_timeline / review_entries / missed_candidates / catalyst_report / rules_fingerprint`. Returns are measured from the confirmation date only. CLI replay writes `T_catalysts.md`. | timeline test |

### Regression example: INDOTECH replay, Jun-2020 → Dec-2025 (month-ends; rules `catalyst-rules-1`, fingerprint f6fe4d677e763c50)

| Catalyst (first public) | Path | Days to supported / validating / confirmed | Base EBITDA/yr |
|---|---|---|---|
| Order book 261 → 437 cr (2023-07-07, ICRA rationale, "next 12 months") | supported 2023-07-31 → confirmed 2024-02-29 | 24 / – / 237 | 5.5 cr (18% of TTM) |
| Capacity 7,000 → 10,000 MVA, completed (2024-07-11) | validating 2024-07-31 → confirmed 2025-07-31 | – / 20 / 385 | 25.5 cr |
| Order book 437 → 700 cr (2024-07-11, "by March 2025-end") | supported 2024-07-31 → validating 2024-11-30 → confirmed 2025-05-31 | 20 / 142 / 324 | 50.9 cr |
| Planned capacity 9,500 → 16,000 MVA (2025-07-18) | potential (commissioning pending) | – | 27.2 cr (bounded by the order book) |
| Rating upgrade, financing (2022-07-15) | contradicted (finance cost rose) | – | unresolved |

- **First defensible catalyst:** 2023-07-07. The backward-looking status reached EXECUTION_CONFIRMED earlier (Aug-2023), from quarters that predate the catalyst. Those quarters do not count toward the catalyst.
- **Delayed confirmations:** the Jul-2024 orders were confirmed in May-2025, not Feb-2025. EBITDA for the Dec-2024 quarter could not be derived from that scanned filing, so the margin check was "data unavailable" until the next filing restated it. This is a data gap, not a business verdict.
- **Remaining upside:** at confirmation, the first order catalyst's realised EBITDA gain already exceeded its base estimate. The review gate says so, rather than reporting upside.

### Unfamiliar companies (exports as of 2024-03-31)

- **SHAILY, ASALCBR, REFEX, PGIL:** no forward catalyst in their disclosures.
- **SUPRIYA:** a potential mix catalyst (magnitude unresolved).
- **GOLDIAM:** lab-grown mix catalysts; the Aug-2023 one was contradicted (margin −293 bps); later ones are potential.
- **DEEPAKFERT:** its 2022 pricing catalyst was confirmed on the measured +557 bps margin, but by 2024 it is history, so the headline is "no live supported catalyst".

### Limits

- **One example only:** INDOTECH is a regression example, not a validation sample. No returns were computed, because no identity-checked price series is available.
- **Rules and cohort:** the two-period combined test was specified by the request ("next one or two relevant quarters"). I had already seen INDOTECH's lumpy quarters when implementing it. The thresholds must be frozen (fingerprint above) and evaluated on a pre-registered cohort that includes failures.
- **Extraction:** lexical extraction still produces some noisy statements. The pricing and mix seeds now require explicit, sized changes.

## 11. Review round 2: shortlist and dates you can trust (rules catalyst-rules-2, fingerprint 59c9ec2217ec6c4d)

| # | Finding | Fix | Tests |
|---|---|---|---|
| 1 | Discovery still ranked by `evidence_status` | Lanes follow the current lead catalyst. Reported performance is a separate field, and a lower lane only when there is no catalyst. Scores come from the lead catalyst. Flags = supported or better. | `test_discovery_catalysts.py`: muted results + supported catalyst reach the shortlist end to end; WP8/WP9 updated |
| 2 | Plant B commissioning confirmed Plant A | Project identity (facility, target capacity). Realised-only matching (no forecast, negation or risk). Unattributable statements are unresolved. Same rules for delays and abandonment. | identity tests |
| 3 | Later rationale attached to an earlier date | Initial assessment frozen at first disclosure. Dated upgrades. Separate materiality / execution / support / validation / confirmation dates. Horizon only from rationales public by the cutoff. | knowability tests |
| 4 | Any cancellation contradicted every order catalyst | Linked event ids per catalyst. Cancelled or amended-down value vs the catalyst's demand (20% contradicts, 5% noted; partials count). Unrelated cancellations are company risks. | cancellation tests |
| 5 | Only the first two periods examined | Original verdict kept. Rolling monitoring. Recovered-late vs on-schedule vs deteriorated. Explicit revised deadlines. | monitoring tests |
| 6 | Materiality was an EBITDA proxy | Three tiers (illustrative / supported EBITDA / recurring parent PAT and EPS). Stricter supported capacity increment. Confirmation needs material recurring earnings or carries an open review condition. | tier and bridge tests |
| 7 | Ledger missed changes | Full-record fingerprint plus rules version plus config hash. | ledger test |

**Also fixed:**
- an implausible share-count jump (+9,904% on INDOTECH) is treated as data to check, not dilution;
- shared ingestion gains an opt-in to download rating rationales (off by default; regression-tested).

**Unfamiliar companies (eight exported issuers, as of 2024-03-31; LIMITED COHORT):**
- no issuer reached supported or better;
- SUPRIYA and GOLDIAM are potential catalysts (mix shifts with unresolved magnitude);
- INDOTECH and SHAILY are reported performance only;
- DEEPAKFERT is data repair.
- The database export contains no credit-rating filings, so INDOTECH's order-book catalyst, visible in NSE filings, is missing here. This is a document-coverage gap, not a detector verdict.

**Still needed before trusting historical detection dates:**
- a pre-registered, company-neutral cohort that includes failed expansions and cancelled orders;
- documents for that cohort, including rating rationales (needs the ingestion option and authorisation);
- an identity-checked adjusted price series for returns from confirmation dates.

## 12. Review round 3: historical correctness (rules catalyst-rules-3, fingerprint 1b744b3fe6944180)

No threshold changed between rules-2 and rules-3; only rule logic changed. Earlier results are kept unchanged under `evaluations/catalyst-rules-1/` and `evaluations/catalyst-rules-2/`.

| # | Finding | Fix | Adversarial tests |
|---|---|---|---|
| 3 (priority) | Historical reconstruction used current order states; seeds came from the current demand summary | Every disclosure time is scanned, and seeds come only from what was public then. Event state at t comes from dated history (`demand.event_state_at`); in the pipeline, events are re-resolved from evidence up to t, with dated reference data. Mechanisms are re-detected from figures up to t and dated at detection. Figures are taken as filed, before a later re-filing superseded them. Event award, amendment and cancellation dates are re-assessment times. Failed candidates stay on record. `stage_history` added. The thesis layer's leading-order scan is fixed the same way. | `test_point_in_time_catalysts.py`: event state at t; a cancelled order keeps its catalyst as contradicted; a later amendment does not resize it; a later verification dates the catalyst at verification; mechanisms are not back-dated; prefix invariance in unit runs, in month-by-month runs and through the pipeline; a re-filing does not rewrite earlier figures (mutation-checked against the old code) |
| 1 | A recovered catalyst stayed positive after deteriorating again | The current status is the latest complete window. Miss → recovery → failure is labelled "deteriorated"; the recovery stays in the text and the confirmation date stays on record. | `test_review3_findings.py` |
| 2 | Missing earnings gave "established" materiality | There is no `parent_PAT or total_PAT` fallback. The parent share is either reported, or 100% only for standalone statements. Missing PAT and zero PAT both leave materiality unresolved. A loss is measured by its size. An assumed tax rate leaves materiality unresolved, and an assumed funding mix is tested at 100% debt. `materiality_basis` added. | consolidated without owners' PAT; zero parent PAT; no PAT; small and large losses; assumed vs stated tax |
| 4 | Rationale capex was not matched to the project | Capex is used only when the quote names the facility or the target capacity. Company-wide capex, or capex for another plant, leaves the bridge unresolved and states why. A mix without proportions is an assumption. | company-wide, other facility, matching project, mix |
| 5 | "Verified external demand" was too strong | `demand_support`: *issuer-disclosed* (named by the issuer, an issuer assertion of independence, or an agency repeating the book) vs *independently supported* (an external source confirms both the customer and its independence). Catalysts carry `demand_basis` and `confidence`. Issuer-disclosed demand gets lower confidence and an investment-review verification condition. The status text no longer says "verified external". | issuer-named, issuer-asserted independence, corroborated and independent, agency-stated book, demand-summary split |
