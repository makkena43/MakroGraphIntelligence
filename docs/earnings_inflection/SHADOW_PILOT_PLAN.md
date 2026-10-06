# Prospective shadow pilot: plan (not started)

**Status: plan only.** Nothing here is activated. There are no schedules, notifications, trading or portfolio actions. Starting the pilot needs your explicit go-ahead, plus a read-only production adapter and a test database for storing runs.

## Purpose

Measure, prospectively and before any performance claim, whether the detector surfaces real earnings inflections early enough and at a review cost you can sustain.

Results are judged only against outcomes that happen **after** each run. No historical tuning on named winners is allowed.

## Frozen configuration

- **Before the first run:**
  - record `evaluation.config_hash(config)` and the code version (git SHA);
  - write a `FrozenConfig`;
  - commit it.
- **During the pilot:** any later change to thresholds, weights or rules starts a **new** pilot arm. It is never applied to the running one.

## Universe and cadence (decided before the first run)

- **Universe:** a versioned eligibility snapshot. It includes small caps and SME reporters, and records failures and delistings as they happen.
- **Run dates:** one universe run (`--universe`, run-once) after each results season, at about 15 May, 15 Aug, 15 Nov and 15 Feb, plus incremental runs (`--previous`) every two weeks.
- **Limits:** `max_issuers`, `max_documents` and the LLM budget are set per run. Deferred issuers are carried to the next run.

## Review protocol

1. A reviewer reads each EXECUTION_RESEARCH and COMMITMENT_RESEARCH entry and records:
   - `REVIEWED_ACCEPTED` or `REVIEWED_REJECTED`;
   - the reason;
   - the minutes spent;
   - whether the cited sources support the claim.
2. A sample of 10 issuers per run from the other lanes and from "no qualifying evidence" is also reviewed, to estimate misses.
3. Labels are written **before** any price outcome is looked at, by a reviewer who does not see prices.

## Catalyst metrics (added with catalyst-rules-1; frozen with the config hash)

- **First defensible catalyst date:** per issuer.
- **Timing:** days from the first public disclosure to execution validating, and to confirmed for investment review.
- **Failure counts:** false positives (supported catalysts later contradicted), delays, failed theses, and catalysts stuck at data unavailable.
- **Misses:** candidates missed because documents were missing or unparsed, kept apart from detector misses (`missed_candidates`).
- **Returns:** measured from the **confirmation / review date** only (`review_entries`), never from the earlier watch-list date.
- **Cohort:** include unfamiliar companies and failures. INDOTECH is a regression example, not the template.

## Success metrics (fixed now)

| Metric | Definition | Target to continue |
|---|---|---|
| Source support rate | share of shortlisted entries whose citations support the claim | >= 90% |
| Precision (reviewed) | accepted / reviewed in the flag lanes | >= 50%, with a Wilson 95% CI reported |
| Data-coverage failure rate | eligible issuers not assessed, or with missing expected periods | falling run over run; < 20% by run 4 |
| Detection delay | days from the first public signal (reviewer) to the shortlist | median <= 30 days |
| Review burden | reviewer minutes per run | <= 6 hours |
| Outcome check (later) | lane returns after costs vs matched benchmarks, censored windows excluded | reported only once at least 30 completed 12-month windows exist; no claim before that |

## Stopping and reporting

- **Stop rule:** stop or redesign if source support stays below 80% for two runs, or if the review burden stays above target.
- **Quarterly report:** produced with `evaluation_report(...)`. It must include sample sizes, censoring counts, missed cases and false alarms.
- **No claim:** do not claim "multibagger detection" from a handful of favourable examples.
