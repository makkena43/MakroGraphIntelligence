# Earnings Inflection Detector: implementation, coverage and limitations report

Spec: "Earnings Inflection Detector — implementation specification", v1.0 draft, 2026-10-04
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
| PLAINCO | flat business; latest results have a date-only `filed_at` | NO_MATERIAL_CHANGE |
| CONTRACO | FY24 guidance of 30% growth, actual 5% | CONTRADICTED (bridge not computed: missing quarter) |
| SAMPLEBANK | bank | UNSUPPORTED_FINANCIAL_MODEL |

## 4. Coverage and known limitations

- **Live coverage unknown.** Production document counts by source and type, text availability and timestamp completeness are unmeasured. Running `--source postgres --preflight-only` against a read-only role produces this. It hasn't been run.
- **Text dependency.** Detection needs `raw_text` or parsed `.txt` files. Scanned PDFs, image-only annual reports and parser-dropped tables yield nothing. No re-parse into isolated output is wired yet; `parser/pdf_parser.py` would be the starting point.
- **Lexical extraction** is precision-oriented and will miss:
  - paraphrased guidance ("high-twenties growth")
  - numbers that sit in slides
  - Hindi or regional-language text
  - segment tables
  - cash-flow statements (operating cash flow is not parsed from tables yet)
- **Results-table parser** is built for the SEBI-format statement in which period columns appear on one header line. Multi-line or merged headers, layouts with "Nine months" columns, and segment-reporting tables need more fixtures drawn from real filings.
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
