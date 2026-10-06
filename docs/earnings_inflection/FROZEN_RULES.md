# Frozen catalyst rules

Current rules version **catalyst-rules-3** · fingerprint `1b744b3fe6944180` (`evaluation.rules_fingerprint(DEFAULT_CATALYST_THRESHOLDS, RULES_VERSION)`).

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

**catalyst-rules-3 changes:**
- **Historical state:** every input is rebuilt at each historical timestamp: event state, mechanisms, and figures as filed. Later failures are kept, and appending future disclosures does not change earlier assessments (prefix invariance).
- **Recovery:** the current status after a recovery comes from the latest complete window.
- **Earnings materiality:** missing, zero and negative earnings are handled separately. An unknown parent share is never 100%. Assumptions cannot establish materiality.
- **Capex:** rating-rationale capex counts only when it names the project.
- **Demand:** issuer-disclosed binding demand is separated from independently supported external demand, with different confidence and verification requirements.

The thresholds are identical in versions 2 and 3:

| Threshold | Value |
|---|---|
| `order_inflow_to_ttm_revenue` | 0.25 |
| `order_book_growth_pct` | 30.0 |
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
