# Rules frozen for the held-out cohort

Frozen **2026-10-08**, before any filing of the 30 held-out issuers (`COHORT_PREREGISTRATION.md`) was fetched.

| Rules | Version | Fingerprint | Code |
|---|---|---|---|
| Numbers-first detector (primary) | `numeric-rules-1` | `d6c74da7c61b7527` | commit `0202362` |
| Text-led catalyst detector (comparison) | `catalyst-rules-6` | `76b4dd78c36b9506` | commit `0202362` |

Data configuration:
- XBRL results are on (default): `in-bse-fin` filings up to the Dec-2024 quarter and integrated filings (`in-capmkt`) from the Mar-2025 quarter.
- PDF tables (pdftotext) are the fallback.
- Fingerprint: `evaluation.rules_fingerprint(DEFAULT_NUMERIC_THRESHOLDS, NUMERIC_RULES_VERSION)`.

**Development check** (22 development issuers, the same code; `dev_check/`):
- signal episodes followed within 4 quarters by TTM profit at least 25% higher: **25 of 41 judged (61%)**, against a base rate of **168 of 353 quarters (48%)**;
- mechanical inflections with a signal in their window: 18 of 20;
- signal dates changed or vanished in later snapshots: 0.

The one change made during the development check, before this freeze, was the depressed-base guard (commit `0202362`). The thresholds are unchanged from those set before any outcome was seen.

**Evaluation tools:** `tools/` holds `cohort_eval2.py` (replay and metrics), `num_summary.py`, and the read-only fetchers `fetch_cohort.py` and `fetch_xbrl.py`.
