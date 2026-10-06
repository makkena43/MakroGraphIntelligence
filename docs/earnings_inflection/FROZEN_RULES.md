# Frozen catalyst rules

Rules version **catalyst-rules-2** · fingerprint `59c9ec2217ec6c4d` (`evaluation.rules_fingerprint(DEFAULT_CATALYST_THRESHOLDS, RULES_VERSION)`).

Frozen before any further historical evaluation.
- **Any change** to a threshold, test or stage rule needs a new rules version and a new fingerprint.
- **Evaluation:** results are reported per fingerprint, and evaluations under different fingerprints are never pooled.
- **INDOTECH** is a regression example only. The thresholds below were not tuned to it, but its quarterly pattern was known when the two-period combined test was written.

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
