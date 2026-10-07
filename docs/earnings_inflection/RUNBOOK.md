# Earnings Inflection Detector: runbook and pending work

Research-only and evidence-only. The detector reads documents and writes JSON/Markdown files. It never writes to existing tables, never schedules itself and never produces buy/sell decisions.

## A. Run it now on the built-in sample data (no database)

```bash
pip install pyyaml pytest
python -m pytest tests/earnings_inflection tests/test_ingestion_pdf_text.py tests/test_pipeline.py -q   # 304 tests
python scripts/earnings_inflection.py --ticker ACMEGRID --as-of 2024-10-31          # prints the report
python scripts/earnings_inflection.py --ticker ACMEGRID --ticker CONTRACO \
    --as-of 2024-10-31 --out data/earnings_inflection                             # writes .md + .json
```

The sample companies are fictional (`tests/earnings_inflection/fixtures/`).

## B. Run it on your MakroGraph database

### 1. One time: create a read-only database user

Run as the Postgres owner. Use your own database name and password.

```sql
CREATE ROLE ei_reader LOGIN PASSWORD '<choose-one>';
GRANT CONNECT ON DATABASE makrograph TO ei_reader;
GRANT USAGE ON SCHEMA public TO ei_reader;
GRANT SELECT ON mg_documents TO ei_reader;
GRANT SELECT ON fundamentals_snapshot TO ei_reader;      -- optional: industry label only
ALTER ROLE ei_reader SET default_transaction_read_only = on;
```

Postgres mode needs `psycopg2-binary`, which is already in `requirements.txt`.

```bash
export EI_READONLY_DSN="postgresql://ei_reader:<password>@localhost:5432/makrograph"
```

The detector also forces read-only mode on its own connection and refuses non-SELECT SQL. The role is a second safeguard.

### 2. Make sure the filings have text

The detector reads `mg_documents.raw_text` only. Exchange announcements are stored as titles until the PDF-fetch stage downloads and parses them.

- **From the UI:** Pipeline tab → enable **PDF fetch (India)** → run. This sets `do_pdf_fetch_india` on `/api/pipeline/run`. In this mode the PDFs stay on disk and only `local_path` is set. A downloaded PDF is **not** readable text: run the explicit extraction step (B.2b) for those rows.
- **From a script** (stores text in `raw_text`, which is what the detector reads):

```python
# run from the repo root; uses the normal (writable) pipeline DB credentials
import json, yaml
from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
cfg = yaml.safe_load(open("config/settings.yaml"))
for k, v in json.load(open("config/secrets.json")).items():      # same merge as run_india_historical.py
    if not k.startswith("_") and isinstance(v, dict):
        cfg.setdefault(k, {}).update({a: b for a, b in v.items() if b})
p = IntelligencePipeline(cfg); p._init_storage()
p.run_pdf_fetch_india(store_text_to_db=True, delete_after_parse=False,
                      window_start=None, window_end=None)   # or a date range
```

BSE documents and page markers apply **only to documents fetched after the latest ingestion changes**. To re-parse older documents with page markers, clear `raw_text` for the rows you want and run the fetch again. That's a deliberate backfill, so do it on a copy or a date window first.

### 2b. Extract text for PDF-only rows (explicit, bounded, local)

Rows that have a `local_path` but no `raw_text` are reported as unreadable until an explicit extraction step turns them into **versioned text artifacts**. The step only reads local files and writes to a local directory. It never downloads, never writes to the database and never runs network OCR.

```bash
python scripts/earnings_inflection.py --source postgres --ticker TICKER1 --as-of 2026-09-30 \
    --artifact-root data/earnings_inflection/text_artifacts --extract --max-docs 50
# add --ocr ocrmypdf only if the ocrmypdf binary is installed locally (no paid/network OCR)
```

- Each artifact is immutable and keyed by document, raw-file hash, method and parser version. Re-extracting with a newer parser adds a version and never overwrites one.
- Failures are recorded states, not gaps: `OCR_REQUIRED`, `PARSE_FAILED_RETRYABLE` (retried up to 2 times), `ENCRYPTED`, `EMPTY`, `UNSUPPORTED_FORMAT`, `MISSING_ORIGINAL`. Partial extractions (page limit, pages without a text layer) are labelled `PARTIAL` with page counts.
- Anything beyond `--max-docs` is listed as deferred. Run again to continue.
- Later assessments pass the same `--artifact-root` to read the artifacts.

Optional, off by default: `run_pdf_fetch_india(text_artifact_root=..., keep_failed_originals=True, granular_failure_status=True)` records artifacts during ingestion itself. Leave these off in production until that is separately authorised.

### 3. Check what the database actually holds

```bash
python scripts/earnings_inflection.py --source postgres --preflight-only
```

This reports:

- `mg_documents` columns, including whether `raw_text` and `published_at` exist;
- document counts by source and type;
- how many documents have text;
- how many have no timestamp;
- the date range covered.

Run it before trusting any company result.

### 3b. If a company's result looks wrong: diagnose its filings

```bash
python scripts/earnings_inflection.py --source postgres --ticker <SYMBOL> --as-of <date> --diagnose
```

For every filing public by the as-of date it prints:

- classification and its basis;
- character count and the share of garbled lines;
- for each results table: the period columns found (date and Q/H/9M/FY), the unit scale, the scope (standalone or consolidated) and which rows matched.

At the end it shows the revenue series by period and whether the latest period is stale.

When a filing looks like results but no period columns were found, it prints the raw text around the revenue row. Paste that block into a chat to get the parser fixed for that layout.

### 4. Assess companies

```bash
python scripts/earnings_inflection.py --source postgres \
    --ticker TICKER1 --ticker TICKER2 --as-of 2026-09-30 --out data/earnings_inflection
```

- `--as-of` is point-in-time. Only documents public by the end of that day (IST) are used, so you can replay any historical date.
- Pass tickers explicitly. There's intentionally no "whole market" mode.
- Optional config: copy `config/earnings_inflection.example.yaml` and pass `--config`. That's where thresholds, the issuer registry, counterparty reference data, replay mode, and the LLM and persistence switches live. All switches are off by default.
- With `--out`, a `manifest.json` is written next to the reports: code version, config hash, replay mode, cutoff, and the exact text version of every source used. Run status is `COMPLETE`, `PARTIAL` (failures or missing expected result periods) or `FAILED`.

### 4b. Replays

- **Exact replay of an earlier run:** `--replay-manifest data/earnings_inflection/manifest.json` reads exactly the text versions that run used, even if newer extractions exist.
- **Two replay modes** (config `replay_mode`):
  - `PUBLIC_INFORMATION_RECONSTRUCTION` (default) asks what was public at the cutoff, even if this system ingested or extracted it later.
  - `SYSTEM_KNOWLEDGE_REPLAY` asks what this system actually held: a document counts only if it was first seen, its text existed and its alias mapping was recorded by the cutoff. Legacy `raw_text` has no provable text time, so on today's data this mode excludes it and says so in coverage.

### 4c. Identity: link NSE symbols, BSE scrip codes and renames

BSE rows store the numeric scrip code as `ticker`. Supply an issuer registry (config `identity.issuers` or `identity.registry_file`) with effective-dated aliases. The detector then retrieves every alias valid at the cutoff and records which alias each document used. Listing board and series (SME vs main board) come from the dated `securities` entries, never from present-day metadata. A predecessor company is spliced into history only when `comparable: true` is recorded with a decision source.

### 5. Read the output

Each `<TICKER>_<date>.md` answers the five questions:

1. What changed and when it became public.
2. Whether it's an assertion, a commitment or a realized result.
3. EPS and cash scenarios.
4. Contradictions and risks.
5. Next checks.

The `.json` file has the same content in machine-readable form.

| Status | Meaning |
|---|---|
| INSUFFICIENT_EVIDENCE | not enough dated, text-bearing filings; often a data gap, not a company verdict |
| NO_MATERIAL_CHANGE | data present, nothing crossed the descriptive thresholds |
| ASSERTION_ONLY | only management's forward statements |
| EARLY_COMMITMENT_UNVERIFIED | material order activity that cannot be verified: L1/LoI/framework, anonymous customer, ceiling/unquantified value. Kept visible with the reasons; never upgraded |
| COMMITMENT_BACKED | **validated binding external** orders (named customer, firm value, not related party, not cancelled) material relative to revenue, not yet in results |
| EXECUTION_EMERGING | one quarter of material realized change |
| EXECUTION_CONFIRMED | at least 2 consecutive periods of material realized change (quarters; half-years for half-yearly SME reporters) |
| CONTRADICTED | missed even the latest guidance, or 2+ downward revisions without a stated reason |

**Your part:**

- Read the quoted sources.
- Judge any revision reason the report quotes ("Judge whether the stated reason … is credible").
- Check flagged disappearing targets.
- Confirm that named counterparties are real. To record an independent check, add a dated entry under `counterparties` in the config. The issuer's own "not a related party" answer stays an issuer assertion.
- Read the **Order events** table: stage now (with dated history), value now vs original (after amendments and cancellations), value and tax basis, and execution period. A headline order value is not annual revenue.
- Read the reconciliation lines. A period whose PAT = PBT − tax or balance-sheet identity fails is excluded from calculations and listed, not silently used.

`review_status` stays `UNREVIEWED` until a human changes it. The `ei_reviews` table in `schema/earnings_inflection_schema.sql` is meant for that, once a test database is authorised.

### 6. Cadence

- Re-run a company after a material announcement (order, results, call transcript).
- Reassess all tracked companies after each results season.
- There's no scheduler. Run it when you choose.

## D. Mechanisms, LLM, valuation, universe scans and validation (WP5-WP9)

### D.1 Earnings mechanisms (always on)

Each report has an **Earnings mechanisms** table covering utilisation, product/customer mix, pricing/input costs, order quality, debt reduction, segment turnaround and organic volume.

- **States:** each mechanism has its own state:
  - `insufficient_data`
  - `no_material_change`
  - `assertion`
  - `commitment`
  - `emerging`
  - `confirmed`
  - `adverse`
  - `contradicted`
- **Detail:** every mechanism also shows its direction, magnitude, durability, attribution, confidence and invalidators.
- **Overlaps:** mechanisms that describe the same margin change are cross-referenced, and their effects are not additive.
- **Status upgrade:** only a **confirmed** realized mechanism can raise the overall status. For example, two consecutive periods of gross-margin gain confirm even with low sales growth.
- **Research questions:** shown wherever a magnitude cannot be quantified.
- **Thresholds:** `mechanism_thresholds` in the config.

### D.1a Forward catalysts: detect a credible future earnings change, then verify it

Every report now opens with:

- **Detected:** credible prospective earnings change / potential catalyst(s) / none.
- **Why:** dated evidence and the operating mechanism.
- **Waiting for:** the specific milestones over the next relevant reporting periods.
- **Investment review:** only after confirmation, with valuation and downside assessed.

The evidence status is shown separately as *current reported performance*.

**Discovery triggers** are new disclosures of a potentially material change in:
- executable orders or customer approvals;
- capacity commissioning, or removal of a production or testing bottleneck;
- utilisation, product mix or contract pricing;
- financing costs (only when interest is at least 10% of EBITDA) or a loss-making segment.

Current-quarter growth is not required.

**Catalyst records:**
- **Persistence:** each catalyst is a separate record keyed by its first public disclosure, holding:
  - facts and expectations;
  - the mechanism chain (demand → deliverable capacity → revenue conversion → recurring profit → cash);
  - its contribution;
  - its execution window;
  - milestones and invalidators.
- **No overwriting:** catalysts are rebuilt from every document public by the as-of date, so a new quarter never overwrites an older one.
- **Point in time (rules-3):** catalysts are generated by scanning every disclosure time and asking what was public then:
  - each order event in the state it stood then;
  - mechanisms detected from the figures public then (dated at detection, never back-dated to the period they describe);
  - figures as filed, before a later re-filing superseded them.
- **Failures and prefix invariance:** a later cancellation, amendment or verification never removes, resizes or back-dates an earlier catalyst; a failed candidate stays on record as contradicted. `stage_history` lists each stage change. Appending future disclosures does not change any earlier assessment.
- **Ledger:** `catalyst_ledger: DIR` appends a dated version on each change (append-only JSONL); the test-database mirror is `ei_catalyst*`.

**Contribution:**
- **When it is computed:** EBITDA per year (downside/base/upside) is computed only when the inputs exist. It is frozen at detection, from what was public then.
- **Capacity:** bounded by demand, never by the capacity ratio. Without demand evidence it stays potential, because capacity alone may only add depreciation.
- **Otherwise:** "potentially material; magnitude unresolved".

**Stages:**

| Stage | When |
|---|---|
| potential | an announced change; economics or execution support incomplete |
| supported | the mechanism, materiality and execution pathway are credible |
| execution validating | a primary milestone is met (guards such as margins held never validate on their own) |
| confirmed for investment review | the primary milestones are met over the required relevant periods, with materiality established |
| delayed | the timetable slipped |
| contradicted | e.g. a cancellation, a shelved project or an adverse result |
| data unavailable | results are due but missing; this is not a business verdict |

Catalysts older than their window plus 12 months are history: they are listed, but don't drive the headline.

**Confirmation questions** are fixed at detection. They are judged only on reporting periods that end after the catalyst could contribute (for capacity, about two months after commissioning). The first two such periods are judged singly and together; a muted quarter remains consistent.

**Credit-rating rationales** (ICRA, CARE, CRISIL, India Ratings, Acuite, Infomerics, Brickwork):
- **Source:** read in full from the exchange-filed copies. Every dated version is kept.
- **Fields extracted:**
  - capacity, utilisation and expansion;
  - capex, funding and completion;
  - order book and execution horizon;
  - customer concentration and payment protection;
  - pass-through;
  - working capital, DSCR, interest cover and obligations;
  - liquidity and bank-limit utilisation.
- **Plans vs observations:** an agency repeating a plan is labelled corroborating context, never proof of execution.
- **Agency websites:** retrieval stays disabled until network access is authorised.

**Replay:** `--replay-from/--replay-to` also writes `T_catalysts.md`, which records:
- the first defensible (supported) catalyst;
- days to validating and to confirmed;
- false positives, delays and data gaps;
- the frozen-rules fingerprint.

`evaluation.review_entries` dates returns from the confirmation date, never from the earlier watch-list date. `missed_candidates` separates data-coverage misses from detector misses.

#### Catalyst rules (catalyst-rules-5; `FROZEN_RULES.md`, which also keeps the earlier versions)

- **Knowability:**
  - Each catalyst records its first disclosure plus the dates materiality, execution, support, validation and confirmation became supportable.
  - The initial assessment uses only what was public at first disclosure; later evidence appears as dated upgrades.
  - An execution horizon counts only from rationales already public at the cutoff.
- **Project identity:** commissioning, delay and abandonment count only when they name the same facility or target capacity, and only as realised statements (no forecasts, negations or mere risks). Unattributable statements stay unresolved.
- **Cancellations:** linked to the orders behind each catalyst and weighed against its demand (20% or more contradicts; 5% or more is noted). Unrelated cancellations are company-level risks.
- **Monitoring:** the original-timetable verdict is kept, and later windows keep being monitored. "Recovered late", "deteriorated" and "confirmed on schedule" are distinct labels. The current status is always the latest complete window, so a recovery that later fails again is "deteriorated", not still met. Stated delays set explicit revised deadlines; original deadlines never move.
- **Execution window (rules-4):** when the window ends without confirmation, the catalyst becomes "delayed" only if its execution test was missed.
  - If execution was verified but confirmation is impossible from the disclosures (for example, an order inflow without a backlog cannot be sized), it stays "execution validating".
  - `confirmation_blocked` names the reason, and the catalyst ages into history after the usual 12 months.
- **Order books (rules-5):**
  - Readings within 45 days and 10% of each other are one restatement.
  - A reading below 10% of TTM revenue is not the order book.
  - A jump above 4× needs a second filing at the new level.
- **Order values (rules-5):** a value stated after the award sentence or in the SEBI annexure is linked when a filing has one award and one value.
- **Monthly volumes (rules-5):** issuers' monthly unit-sales updates can seed a `volume_run_rate` catalyst, which is verified against reported revenue.
- **Segment tables (rules-5):** they are used only when segment revenues reconcile with reported revenue within 5%.
- **Data gaps (rules-5):** a gap never erases a contradicted or delayed verdict.
- **Demand basis:**
  - *Issuer-disclosed binding demand:* firm, binding, customer named by the issuer, not related. This includes an order book a rating agency repeats. It gets lower confidence, and confirmation carries the review condition "confirm the customer and its independence from an attributable external source".
  - *Independently supported external demand:* an attributable external source confirms both the customer and that it is unrelated.
  - An issuer's own statement that a customer is unrelated is not independent support.
- **Earnings tiers:**
  - illustrative operating upside, never counted;
  - supported incremental EBITDA (for capacity this needs a stated order-book horizon and evidence that capacity is constraining);
  - recurring parent earnings after D&A, interest, tax, minority share and dilution.
  - Confirmation needs material recurring earnings; otherwise there is an explicit open investment-review condition.
  - **Missing, zero and negative earnings are different.**
    - There is no fallback from parent PAT to total PAT.
    - The parent share is either reported, or 100% only for standalone statements; it is never assumed.
    - Missing PAT leaves materiality unresolved, and so does zero PAT.
    - For a loss-maker, the increment is measured against the size of the loss.
  - **Assumptions cannot establish materiality.** If materiality depends on an assumed tax rate, it is unresolved. An assumed funding mix is tested at 100% debt.
  - **Capex** is attributed to a capacity catalyst only when the source names the project (facility or target capacity). Company-wide rationale capex leaves the bridge unresolved.
- **Ledger:** the fingerprint covers the complete record plus the rules version and config hash.
- **Shared ingestion (opt-in, off):** `download_high_value_pdfs(include_credit_rating_rationales=True)` also downloads company-filed rating rationales (NSE "Credit Rating" categories). The current database export holds none, so production catalysts cannot see them until this is enabled with authorisation.

### D.1b Forward-looking thesis and replay timelines

Each report also has a **Forward-looking thesis** section, next to (never replacing) the evidence status.

- **Leading candidate:** a source-backed change in economics not yet in reported results, per mechanism:
  - verified binding external orders >= 25% of TTM revenue in 12 months (order quality);
  - a stated order book up >= 30% on a snapshot 5-15 months earlier (a high but flat book is context, not a change);
  - completed capacity expansion >= 20% (utilisation);
  - issuer-stated realised price / input-cost / mix / volume changes **with their size** (wording alone is not enough).
  Bare forward-looking statements stay ASSERTION_ONLY.
- **First-results check:** the first results published after the signal, on the mechanism's own metric
  (revenue growth for orders / capacity / volume; gross, else EBITDA, margin for pricing / mix):
  `validated`, `not_validated`, `adverse`; `pending` until the SEBI deadline, then `overdue` (a data gap, not a verdict).
- **Confidence:** low (leading) -> medium (first results validated) -> high (a second period repeats it).
  Two-period confirmation upgrades confidence; it is not an admission gate.
- **Evidence per mechanism:** dated positive and negative items; reported revenue / margin outcomes are listed
  once, attributed to no mechanism.
- **Stale series:** mechanisms keep their last reading, labelled stale, and never upgrade the status. The report also
  shows the **last known evidence status**, recomputed from what was public when the latest parsed results came out.
- **Thresholds:** `thesis_thresholds` in the config.

Replay one issuer month by month (each month sees only documents public by then) and score it:

```bash
python scripts/earnings_inflection.py --fixtures DIR --ticker T --replay-from 2020-06-01 --replay-to 2025-12-31 --out OUT
```

Writes `T_replay.json` (snapshots) and `T_timeline.md`. The timeline is scored in the outcome sandbox:
- earliest defensible signal;
- first EMERGING / CONFIRMED status;
- per episode: first-results verdict, confirmation date and lead time;
- false alarms and unresolved signals;
- earnings delivery: TTM revenue / PAT four periods after the last results public before the signal. It uses later
  data, so it is evaluation only. Missing figures make it censored, never zero.

One issuer is an example, not a validation sample.

### D.2 LLM extraction (off by default)

```yaml
llm: {enabled: true, provider: fake, fake_responses: my_fake.json}   # offline dry run
budget: {enabled: true, max_calls: 50, max_tokens: 500000, max_spend_usd: 2.0, usd_per_1k_tokens: 0.004}
```

- **Provider `anthropic`:** uses `claude-opus-5-5` at low effort, with server-side refusal fallback off (set `llm.fallbacks: true` to enable). Credentials come from `ANTHROPIC_API_KEY` or an `ant auth login` profile and are never printed.
- **Preflight:** a missing client or zero budget fails preflight **once** (exit code 2) before any company is processed.
- **What is sent:** only ambiguous or material narrative chunks.
- **Validation:** every returned field is checked against the quote. Rejections, failed calls and budget stops are counted in `coverage.llm` and the manifest. A budget stop marks the run PARTIAL.
- **Cost:** a real provider run is a paid run and needs your separate authorisation.

### D.3 Earnings bridge and valuation context

- **Cases:** the bridge shows downside (reported lows), base (TTM run-rate) and upside (latest trend persists) cases. A management case appears only when the guidance year sits on a reported fiscal-year base.
- **Assumption register:** every input is listed with its source. Capex D&A and interest assumptions are in `bridge_assumptions`.
- **Valuation:** context only, and optional (`valuation.enabled`). It needs an injected market-data adapter with identity-checked series and sourced splits/bonuses, and it never changes the evidence status. No adapter to the production price tables is wired yet.

### D.4 Universe scan and research shortlist

```bash
python scripts/earnings_inflection.py --universe tests/earnings_inflection/universe/snapshot_2024-10-31.json \
    --as-of 2024-10-31 --runs-root data/earnings_inflection/runs --max-issuers 50 --page-size 10
python scripts/earnings_inflection.py --universe SNAP.json --as-of DATE --resume data/earnings_inflection/runs/<run_id>
python scripts/earnings_inflection.py --universe SNAP.json --as-of LATER --previous data/earnings_inflection/runs/<run_id>
```

- **Snapshot:** a versioned JSON eligibility list (`snapshot_id`, `as_of`, `source`, `members` with `status` and optional `excluded_reason`). Keep delisted and suspended members in it.
- **Outputs:** each run writes an immutable directory containing:
  - `issuers/*.json`
  - `shortlist.json`
  - `shortlist.md`
  - `checkpoint.json`
- **Lanes (forward catalyst first):** CONFIRMED_FOR_REVIEW, EXECUTION_VALIDATING, PROSPECTIVE_SUPPORTED, POTENTIAL_CATALYST, REPORTED_PERFORMANCE_ONLY (growth with no current catalyst), DATA_REPAIR and CONTRADICTED_OR_STALE.
  - Every record also carries `reported_performance` (the evidence status) as a separate dimension, plus `forward_setup` with the lead catalyst and its dates.
  - The first defensible signal is the date support was established, not the first mention.
  - Scores are visible components, not probabilities. Lanes are never padded.
- **Example:** `docs/earnings_inflection/examples/research_shortlist_synthetic.md` (synthetic issuers).
- **Production use:** needs a production snapshot built from your universe tables and the read-only adapter.

### D.5 Validation (evaluation.py; isolated from detection)

1. Freeze the config: `FrozenConfig(config_hash(cfg), date)`.
2. Build a company-neutral cohort with `build_cohort`.
3. Collect reviewer labels.
4. Run `classification_report`, `extraction_accuracy`, `lane_outcomes` and `lane_return_report` with verified adjusted prices, then `evaluation_report`.

Censored windows are never reported as returns. See `SHADOW_PILOT_PLAN.md` for the prospective pilot, which has not started.

## C. Pending work, in priority order

### Must do before trusting results on real data

1. **Run the preflight** (B.3) and share the output. Until then, coverage is unknown.
2. **Fill the issuer registry** (B.4c) for tracked companies: NSE symbol, BSE scrip code, ISIN, renames and SME migrations, all with dates.
3. **Run the explicit extraction** (B.2b) for tracked companies. Then check the `unreadable_documents` and `result_periods.missing` coverage lines.
4. **Build a production eligibility snapshot** (D.4) from your universe tables, including delisted members.
5. **Label a pre-registered control set** (D.5), with winners and non-winners chosen before you see statuses, and run the validation report.

### Still open

6. Production market-data adapter for valuation: series-filtered adjusted prices and a corporate-action file.
7. Promise ledger beyond revenue and margin: capacity commissioning, customer qualification, capex vs budget.
8. Incremental-vs-replacement order business, and order margins.
9. A persistent rolling evidence file per company.

### Optional, needs your approval

10. Turn on the LLM with a real provider and a hard budget. This is a paid run.
11. Paid or network OCR. The code refuses it today; only local `ocrmypdf` is wired.
12. Apply `schema/earnings_inflection_schema.sql` to a **test** database.
13. Start the shadow pilot (`SHADOW_PILOT_PLAN.md`).
14. Turn on the opt-in ingestion options in production.
