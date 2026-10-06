# Pre-registered unfamiliar cohort — catalyst-rules-3

Registered **2026-10-06**, before any document for these issuers was fetched or parsed. Rules frozen at
**catalyst-rules-3**, fingerprint `1b744b3fe6944180` (`FROZEN_RULES.md`). The rules will not change
during this evaluation. If a defect is found, it is recorded here and fixed under a new rules version.
The results reported for this version are not edited.

## Purpose

This run measures coverage, false alerts, defensible detection dates and review workload on issuers
the detector was not developed on. It does not measure returns, and it is not a validation sample. With 7 issuers,
every rate is anecdotal and is reported with its counts.

## Cohort (fixed)

None of these issuers was used in development. That excludes INDOTECH and the eight exported issuers:
SUPRIYA, GOLDIAM, SHAILY, DEEPAKFERT, ASALCBR, PGIL, REFEX and INDOTECH.

The cohort mixes issuers with large disclosed order or capacity changes in 2021–2024. Some of these
changes were widely followed by earnings growth; others were followed by losses, delays or margin
collapse. Composition used general public knowledge to make sure **failures are included**. That
knowledge is **not used for scoring**: outcomes are measured mechanically from the issuers' own later
filings (below).

| NSE symbol | Why included (composition only) |
|---|---|
| GENUSPOWER | Large smart-meter order wins from 2023 |
| HBLENGINE (formerly HBLPOWER) | Defence / railway electronics orders from 2023 |
| TARIL | Transformer order book and capacity expansion in 2023–24 (same sector as INDOTECH: tests transfer, not novelty) |
| OLECTRA | Very large electric-bus orders whose execution and contracting were disputed or delayed (possible failure or delay) |
| BORORENEW | Solar-glass capacity expansion completed in 2022–23, followed by a price collapse and losses (possible failure) |
| GMMPFAUDLR | Order book and capacity growth followed by declining earnings (possible failure) |
| SWSOLAR | Large EPC orders with heavy losses in 2022; later recovery (possible failure, then a new catalyst) |

## Data

- **Sources:** NSE public corporate announcements (attachments) and annual reports for 2021-01-01 to 2025-12-31. They are fetched read-only into a local scratch fixture, text is extracted with `pdftotext -layout`, and nothing is written to any database.
- **Excluded documents:** scanned documents with no extractable text are skipped and counted as coverage gaps. No OCR is used, paid or local.
- **Not used:** credit-rating agency websites. Only rationales the companies filed with the exchange are used.

## Replay

- **As-of dates:** quarterly at the month-ends of February, May, August and November, from 2021-05-31 to 2025-11-30. Each run sees only documents public by its own date.
- **Final snapshot:** a final as-of on 2025-12-31 provides the outcome data cut-off.

## Metrics (definitions fixed now)

- **Coverage:** documents fetched, documents with text, scanned or skipped documents, quarters with parsed results versus quarters expected, rating rationales present, and catalysts by kind.
- **Alert:** a catalyst reaching **supported or better**, i.e. the shortlist flag lanes CONFIRMED_FOR_REVIEW, EXECUTION_VALIDATING and PROSPECTIVE_SUPPORTED.
- **Verdict at the cut-off** (from `evaluation.catalyst_timeline`):
  - confirmed;
  - contradicted;
  - delayed;
  - data_unavailable;
  - open.
- **False alert:** an alert whose verdict at the cut-off is contradicted or delayed.
  - An alert that is still open is reported as unresolved. It is not counted as either true or false.
- **Earnings delivery** (mechanical, from the series known at the cut-off): the change in TTM revenue and TTM parent PAT from the last period reported before the alert's `supported_at` to 4 periods later. "Delivered" means TTM PAT is up at least 15% and TTM revenue is up. Reported alongside the verdict; it never replaces it.
- **Defensible detection date:** `supported_at` and `first_public_at` per alert. Report them, then check their stability: for every alert, the dates must be identical in every later snapshot (prefix invariance on real data). Any change is reported as a defect.
- **Review workload:**
  - alerts per issuer-year;
  - quarterly snapshots in which the issuer is in a flag lane;
  - open review conditions per alert;
  - documents per issuer.
- **Missed inflections** (mechanical):
  - **Inflection:** a 4-period window in which TTM parent PAT (or TTM PAT for standalone series) rises at least 50% from a positive base.
  - **Missed:** no alert at least one reporting period before the window's end.
  - **Classification:** each miss is classed as a coverage miss (missing documents, unparsed results, or no rationale) or a detector miss.

## Not done

- No prices or returns.
- No tuning: thresholds and rules stay as frozen.
- No change to production data, schedules or the shared ingestion defaults.
