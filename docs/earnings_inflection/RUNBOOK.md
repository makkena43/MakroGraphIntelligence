# Earnings Inflection Detector: runbook and pending work

Research-only and evidence-only. The detector reads documents and writes JSON/Markdown files. It never writes to existing tables, never schedules itself and never produces buy/sell decisions.

## A. Run it now on the built-in sample data (no database)

```bash
pip install pyyaml pytest
python -m pytest tests/earnings_inflection tests/test_ingestion_pdf_text.py -q   # 64 tests
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

- **From the UI:** Pipeline tab → enable **PDF fetch (India)** → run. This sets `do_pdf_fetch_india` on `/api/pipeline/run`. In this mode the PDFs stay on disk and only `local_path` is set. The detector doesn't read those files yet (pending item 3), so use the script mode below for the detector.
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

### 4. Assess companies

```bash
python scripts/earnings_inflection.py --source postgres \
    --ticker TICKER1 --ticker TICKER2 --as-of 2026-09-30 --out data/earnings_inflection
```

- `--as-of` is point-in-time. Only documents public by the end of that day (IST) are used, so you can replay any historical date.
- Pass tickers explicitly. There's intentionally no "whole market" mode.
- Optional config: copy `config/earnings_inflection.example.yaml` and pass `--config`. That's where thresholds, symbol history, and the LLM and persistence switches live. All switches are off by default.

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
| COMMITMENT_BACKED | firm or provisional orders material relative to revenue, not yet in results (the "early" route) |
| EXECUTION_EMERGING | one quarter of material realized change |
| EXECUTION_CONFIRMED | at least 2 consecutive quarters of material realized change |
| CONTRADICTED | missed even the latest guidance, or 2+ downward revisions without a stated reason |

**Your part:**

- Read the quoted sources.
- Judge any revision reason the report quotes ("Judge whether the stated reason … is credible").
- Check flagged disappearing targets.
- Confirm that named counterparties are real.

`review_status` stays `UNREVIEWED` until a human changes it. The `ei_reviews` table in `schema/earnings_inflection_schema.sql` is meant for that, once a test database is authorised.

### 6. Cadence

- Re-run a company after a material announcement (order, results, call transcript).
- Reassess all tracked companies after each results season.
- There's no scheduler. Run it when you choose.

## C. Pending work, in priority order

### Must do before trusting results on real data

1. **Run the preflight** (B.3) and share the output. Until then, coverage is unknown.
2. **Link NSE and BSE identities.** BSE rows store the **numeric scrip code** as `ticker`, NSE rows store the symbol, and the detector queries one ticker string. So a company's BSE filings are invisible when you run it by NSE symbol. This needs an alias map (symbol ↔ scrip code ↔ ISIN) that the repository queries together. It's small, and it's the main thing that makes the BSE ingestion fix useful.
3. **Backfill text:**
   - Re-fetch BSE high-value documents.
   - Optionally re-parse older PDFs to get page markers.
   - Read `local_path` `.txt`/PDF files where `raw_text` is empty (UI live mode).
4. **Fetch annual reports** for tracked companies (not the whole market).
5. **Check the results parser on about 20 real statements.** Cover nine-month columns, multi-line headers, and standalone and consolidated statements on the same page. Add them as test fixtures.

### Tracker gaps (from `TRACKER_RECOMMENDATION_GAP_ANALYSIS.md`)

6. EBIT margin and **incremental depreciation for new capacity** in the bridge, plus a real downside scenario.
7. Order economics:
   - annual executable revenue vs multi-year headline value;
   - order value including GST vs accounting revenue;
   - incremental vs replacement business;
   - materiality against **earnings**, not only revenue.
8. Promise ledger beyond revenue and margin: capacity commissioning, customer qualification, capex vs budget, debt and working-capital targets, dilution.
9. Segment tables (mix, turnaround) and cash-flow statements (operating cash flow, debt) from results and annual reports.
10. Claim-level records: link each claim to its starting point and supporting evidence; keep a persistent rolling evidence file per company (about 8 quarters, latest 2 annual reports).
11. A one-page summary view and review dates per company.

### Validation before freezing thresholds

12. Freeze the rules, then build a **pre-registered, point-in-time sample that includes failed inflections**: missed guidance, cancelled orders, margin reversals. Measure whether the states separate successes from failures, using `evaluation.py` with properly adjusted prices.

### Optional, needs your approval

13. Turn on the LLM extractor with a Claude client and a hard budget. It would improve recall on paraphrased guidance.
14. OCR for scanned filings.
15. Apply `schema/earnings_inflection_schema.sql` to a **test** database to store runs and reviews.
