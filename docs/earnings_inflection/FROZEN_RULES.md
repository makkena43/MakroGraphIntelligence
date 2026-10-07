# Frozen catalyst rules

Current rules version **catalyst-rules-6** · fingerprint `76b4dd78c36b9506` (`evaluation.rules_fingerprint(DEFAULT_CATALYST_THRESHOLDS, RULES_VERSION)`).

Frozen before any further historical evaluation.
- **Any change** to a threshold, test or stage rule needs a new rules version and a new fingerprint.
- **Evaluation:** results are reported per fingerprint, and evaluations under different fingerprints are never pooled.
- **Earlier results are kept unchanged** under `docs/earnings_inflection/evaluations/<rules version>/`. They are not re-labelled or overwritten when the rules change.
- **INDOTECH** is a regression example only. The thresholds below were not tuned to it, but its quarterly pattern was known when the two-period combined test was written.

## Version history

| Version | Fingerprint | What changed | Archived results |
|---|---|---|---|
| catalyst-rules-1 | `f6fe4d677e763c50` | first frozen catalyst rules | `evaluations/catalyst-rules-1/` (INDOTECH replay 2020-06 to 2025-12) |
| catalyst-rules-2 | `59c9ec2217ec6c4d` | review round 2: catalyst-driven lanes, project identity, knowability dates, linked cancellations, continuous monitoring, three earnings tiers, full ledger fingerprint | `evaluations/catalyst-rules-2/` (eight exported issuers, 2024-03-31) |
| catalyst-rules-3 | `1b744b3fe6944180` | review round 3, with **no threshold changed** (only rule logic). See the list below. | `evaluations/catalyst-rules-3/` |
| catalyst-rules-4 | `8d896bb571e58393` | defects D1 and D3 from the rules-3 cohort, with **no threshold changed**. See the list below. | `evaluations/catalyst-rules-4/` (named-company case study; cohort development check) |
| catalyst-rules-5 | `b253e4cca34d124d` | defects D2 and D4–D11 plus unstated scope. **Thresholds added** (book plausibility, restatement, monthly volume); existing ones unchanged. See the list below. | `evaluations/catalyst-rules-5/` (development check; pre-registered 12-issuer cohort) |
| catalyst-rules-6 | `76b4dd78c36b9506` | defects D12–D15 from the rules-5 cohort. **One threshold added** (`book_conflict_days`); existing ones unchanged. See the list below. | `evaluations/catalyst-rules-6/` (development check only); needs a new pre-registered cohort |

**catalyst-rules-3 changes:**
- **Historical state:** every input is rebuilt at each historical timestamp: event state, mechanisms, and figures as filed. Later failures are kept, and appending future disclosures does not change earlier assessments (prefix invariance).
- **Recovery:** the current status after a recovery comes from the latest complete window.
- **Earnings materiality:** missing, zero and negative earnings are handled separately. An unknown parent share is never 100%. Assumptions cannot establish materiality.
- **Capex:** rating-rationale capex counts only when it names the project.
- **Demand:** issuer-disclosed binding demand is separated from independently supported external demand, with different confidence and verification requirements.

**catalyst-rules-4 changes:**
- **D1 (parsing):** consolidated profit attributable to owners of the parent is now read.
  - The Ind AS profit-attribution block is recognised even when it comes after the OCI lines.
  - Owners' rows under the OCI and total-comprehensive-income headings are never taken as profit.
  - Tables that continue across chunk or page breaks keep their columns.
  - Scanned "Profil" and loss-makers' "Loss for the period" labels are recognised.
- **D3 (stage):** when the execution window ends, a catalyst becomes "delayed" only if its execution test was not met.
  - An executed catalyst that cannot be confirmed from the disclosures stays "execution validating" and records `confirmation_blocked`. The typical case is an order inflow without a disclosed backlog.
  - Its evaluation verdict is `executed_unconfirmable`, counted neither as confirmed nor as a false alert.
  - The confirmation test is unchanged.

**catalyst-rules-5 changes:**
- **D9 (historical correctness):** re-assessment and mechanism-scan times come from every figure's original filing time, so a figure later superseded (a presentation figure replaced by the annual report) no longer re-dates or removes a catalyst.
- **D8 / D7 (order value):** an order value stated in the sentence after the award, or in the SEBI annexure, is attached when the filing has exactly one unquantified award and exactly one value.
  - "Approximately ₹X crore" is treated as firm; "up to" is a ceiling.
- **D10 (segment tables):**
  - Segment spellings are merged, and company totals are not segments.
  - Mix and turnaround readings require segment revenues to reconcile with reported revenue within 5%. A turnaround with no segment revenue reported is kept at low confidence.
- **D4 (data gaps):** a data gap never erases a contradicted or delayed verdict.
- **D5 (restatements):** order-book readings within 45 days and 10% of each other are one catalyst.
- **D6 (plausibility):** a book below 10% of TTM revenue is not the order book, and a jump above 4× needs a second filing at the new level.
- **D11 (monthly volumes):** monthly unit sales become a VOLUME catalyst (`volume_run_rate`).
  - Trigger: three months up at least 25% year on year, with two of the three months individually up as much, in two consecutive windows.
  - A depressed year-ago base must also be beaten against two years earlier.
  - It is verified against reported revenue.
- **D2 (unit lines):** scanned unit lines are read, including the rupee sign scanned as ¥ or Z, plural "Millions", and damaged scale words on lines that read as unit lines.
- **Scope:** statements without a stated scope are standalone when no consolidated statements had been filed by that figure's filing time.

**catalyst-rules-6 changes** (defects found by the pre-registered rules-5 cohort):
- **D12 (price statements):** a price catalyst reads its direction from the sentence itself ("realisations decreased by 15.4%" is never a rise, whatever the evidence's tag). Quarterly restatements of the same direction are one catalyst; a run ends at an opposite statement or after 200 days without one.
- **D13 (materiality):** a catalyst whose change is immaterial (below the 15%-of-TTM-EBITDA materiality share) is capped at "potential": a research note, never an alert. Contradictions are still reported.
  - The evaluation credits an alert with an earnings inflection only if it was flagged inside the window, was not immaterial when flagged, and its own verdict was not contradicted, delayed or data-unavailable (`evaluation.inflection_credit`). Timing alone no longer counts; the timing-only list is kept for comparison.
- **D14 (results coverage):** statements the rules-5 parser could not read are read:
  - a period header stacked one word per line and cut into several chunks;
  - a rupee glyph inside the unit brackets ("(` in crore)");
  - lower-case letter enumerators ("a Revenue from operations");
  - quarter-end dates whose separators were lost in scanning ("30 092021"; only the four quarter-end day/month pairs);
  - an untitled statement takes the scope of the auditor's review report on it.
  - Still not read, by design: scanned text layers with split digits ("32 13") or damaged decimals ("4.407" for 4,407), and unit lines whose glyph reads as a letter ("(t In crore)").
- **D15 (order-book readings):** an amount added to or retired from the book, or a book "and pipeline", is not a book reading; nor is a past level that the same sentence updates ("was hovering around Rs. 800 Cr, and … has expanded to Rs. 900 Cr"). Readings within 10 days are one catalyst however much they differ, and the conflicting reading is recorded in the catalyst's facts.

Results under rules-3 are not re-scored. The seven rules-3 cohort issuers exposed D1 and D3, so re-running rules-4 on them is a development check, not an evaluation.

The thresholds of versions 2–4 are unchanged in versions 5 and 6; version 5 adds the D5, D6 and D11 thresholds and version 6 adds `book_conflict_days`:

| Threshold | Value |
|---|---|
| `order_inflow_to_ttm_revenue` | 0.25 |
| `order_book_growth_pct` | 30.0 |
| `book_min_share_of_ttm_revenue` | 0.1 |
| `book_max_uncorroborated_multiple` | 4.0 |
| `book_restatement_days` | 45 |
| `book_restatement_pct` | 10.0 |
| `book_conflict_days` | 10 |
| `volume_growth_pct` | 25.0 |
| `capacity_expansion_pct` | 20.0 |
| `materiality_share_of_ttm_ebitda` | 0.15 |
| `default_order_horizon_months` | 12 |
| `conversion_downside` | 0.8 |
| `deliverable_run_rate_multiple` | 1.3 |
| `demand_cover_min` | 1.0 |
| `utilization_tight_pct` | 75.0 |
| `revenue_test_growth_pct` | 15.0 |
| `margin_tolerance_bps` | 150.0 |
| `margin_test_bps` | 150.0 |
| `finance_cost_drop_pct` | 10.0 |
| `dilution_max_pct` | 5.0 |
| `cancellation_contradicts_share` | 0.2 |
| `cancellation_note_share` | 0.05 |
| `financing_min_share_of_ebitda` | 0.1 |
| `confirm_periods` | 2 |
| `monitor_periods` | 8 |
| `commissioning_grace_days` | 90 |
| `window_grace_days` | 120 |
| `current_after_window_months` | 12 |
| `results_deadline_days` | 45 |
| `results_deadline_days_year_end` | 60 |
| `capex_useful_life_years` | 15.0 |
| `capex_debt_share_if_unstated` | 0.5 |
| `incremental_debt_rate_pct` | 9.0 |
| `fallback_tax_rate_pct` | 25.17 |
