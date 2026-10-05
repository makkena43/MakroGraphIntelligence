# Earnings Inflection Detector: runbook and pending work

Research-only and evidence-only. The detector reads documents and writes JSON/Markdown files. It never writes to existing tables, never schedules itself and never produces buy/sell decisions.

## A. Run it now on the built-in sample data (no database)

```bash
pip install pyyaml pytest
python -m pytest tests/earnings_inflection tests/test_ingestion_pdf_text.py tests/test_pipeline.py -q   # 201 tests
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

## C. Pending work, in priority order

### Must do before trusting results on real data

1. **Run the preflight** (B.3) and share the output. Until then, coverage is unknown.
2. **Fill the issuer registry** (B.4c) for tracked companies: NSE symbol ↔ BSE scrip code ↔ ISIN, renames, SME migrations with dates. The mechanism exists; the data doesn't. Without it, BSE filings stay invisible when you run by NSE symbol.
3. **Run the explicit extraction** (B.2b) for tracked companies and check the `unreadable_documents` and `result_periods.missing` coverage lines.
4. **Fetch annual reports** for tracked companies (not the whole market).
5. **Check the results parser on about 20 real statements.** Cover nine-month columns, multi-line headers, standalone and consolidated statements on the same page, and balance-sheet / cash-flow pages. Add them as test fixtures.

### Tracker gaps still open

6. EBIT margin and **incremental depreciation for new capacity** in the bridge, plus a real downside scenario.
7. Order economics still open: incremental vs replacement business, and materiality against **earnings** (WP4 covers value basis, tax basis, execution period, cancellations and backlog).
8. Promise ledger beyond revenue and margin: capacity commissioning, customer qualification, capex vs budget, debt and working-capital targets, dilution.
9. Segment tables (mix, turnaround).
10. A persistent rolling evidence file per company (about 8 quarters, latest 2 annual reports) and a one-page summary view.

### Not received

11. The amendment's **WP4 acceptance criteria and any WP5+ (universe discovery / research shortlist)** were cut off in the pasted text. Nothing was built for them. There is still no whole-market mode.

### Validation before freezing thresholds

12. Freeze the rules, then build a **pre-registered, point-in-time sample that includes failed inflections**: missed guidance, cancelled orders, margin reversals. Measure whether the states separate successes from failures, using `evaluation.py` with properly adjusted prices.

### Optional, needs your approval

13. Turn on the LLM extractor with a Claude client and a hard budget. It would improve recall on paraphrased guidance.
14. Paid or network OCR (refused by the code today; only local `ocrmypdf` is wired).
15. Apply `schema/earnings_inflection_schema.sql` to a **test** database to store runs, source versions, events and reviews. It was checked only against a throwaway local Postgres 16 instance, which has been deleted.
16. Turn on the opt-in ingestion options (`text_artifact_root`, `keep_failed_originals`, `granular_failure_status`) in production.
