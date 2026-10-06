# User-named case study — catalyst-rules-4

Registered **2026-10-06**, before filings for WAAREEENER, SHAKTIPUMP or SMLMAH were fetched.

- **Rules:** frozen at **catalyst-rules-4**, fingerprint `8d896bb571e58393`.
- **No tuning:** the rules and thresholds do not change for this run. A defect found here is recorded and fixed only under a new rules version.
- **Same definitions:** the metric definitions are those of `../catalyst-rules-3/COHORT_PREREGISTRATION.md`, unchanged.

## What this is (and is not)

The user named these four issuers, so they were not selected neutrally. Some are widely known for later earnings growth, so the run is a **case study of the process on named companies**, not an unbiased evaluation. Its results are not pooled with the pre-registered cohort.

| NSE symbol | Notes fixed before fetching |
|---|---|
| WAAREEENER (Waaree Energies) | Exchange filings start at listing on **2024-10-28**. Nothing before listing can be detected from exchange filings: the prospectus is a SEBI document, not an NSE announcement, and is not used. Expect about 5 quarters of history. |
| SHAKTIPUMP (Shakti Pumps) | Unfamiliar to the detector; not used in development. |
| SMLMAH (SML Isuzu, now SML Mahindra) | The filing history is returned under the current symbol. Unfamiliar to the detector. |
| OLECTRA (Olectra Greentech) | **Not unfamiliar.** It was in the rules-3 cohort, and its filing text was used to write the rules-4 D1 parser test. Its result is reported as an in-sample check. |

## Process (identical to the rules-3 cohort)

- **Data:** NSE public announcements and annual reports for 2021-01-01 to 2025-12-31, fetched read-only into a local scratch fixture and converted with `pdftotext`. There is no OCR, and nothing is written to any database.
- **Replay:** quarterly as-of dates at the month-ends of February, May, August and November, from 2021-05-31 to 2025-11-30, plus a 2025-12-31 cut-off. Each run sees only filings public by its date.
- **Metrics:**
  - coverage;
  - alerts (supported or better) and their verdicts at the cut-off;
  - false alerts (contradicted or delayed);
  - executed-but-unconfirmable alerts;
  - earnings delivery;
  - mechanical inflections and misses;
  - prefix invariance of dates;
  - review workload.
- **Not done:** no prices or returns, and no investment labels.
