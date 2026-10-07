# Rules-5 company-neutral cohort — results

Pre-registered in `COHORT_PREREGISTRATION.md` before any data was fetched. Rules **catalyst-rules-5**, fingerprint `b253e4cca34d124d`, unchanged during the run. Quarterly as-of dates 2021-05-31 … 2025-11-30 plus a 2025-12-31 cut-off; each run sees only documents public by its date. 12 issuers: every rate below is anecdotal and given with its counts. No prices or returns.

## Headline

- **Historical correctness on real data:** 0 date changes and 0 vanished catalysts across all snapshots (a catalyst's first-public / support / validation / confirmation dates never changed once recorded).
- **Alerts (supported or better):** 27 across 6 of 12 issuers; verdicts at the cut-off: contradicted 8, delayed 2, executed_unconfirmable 15, open 2.
- **False alerts (contradicted or delayed):** 10 of 27.
- **Earnings delivery (measurable alerts):** 7 of 14 delivered (TTM PAT +15% and revenue up, 4 periods after support).
- **Mechanical earnings inflections:** 4; missed (no alert before the window's end was published): 1.

## What this shows

1. **Historical correctness held.** Across 12 issuers drawn without names and 20 snapshots each, no recorded catalyst date changed and none vanished.
2. **The detector was quiet on 6 of the 12 issuers:**
   - **No alerts:** PRICOLLTD, TATACHEM, BATAINDIA, CESC, GPPL and AFFLE.
   - **Why that is not clean:** for TATACHEM (5 quarters of revenue parsed) and BATAINDIA (6), the quiet is mostly missing coverage, not a verdict.
   - **AFFLE missed:** its earnings rose (TTM PAT 39.6 → 88.5 cr, 2021–24), and this is the one inflection counted as missed.
3. **RAIN alone produced 16 of the 27 alerts**, all "price change" catalysts seeded from each quarter's commentary ("average blended realisation increased by ~36%").
   - **Price decreases became catalysts too.** Five of RAIN's alerts are statements that realisations *decreased* ("realisations decreased by 15.4%"). The price rule does not check direction.
   - **Every quarterly restatement became a new catalyst.**
   - **Effect:** this is most of the cohort's false alerts and workload.
4. **Excluding RAIN:**
   - **Count:** 11 alerts across 5 issuers.
   - **Contradicted or delayed (5):** HGINFRA's order inflow, LUMAXIND's net-debt reduction, AURIONPRO's and RBA's mix shifts, and JYOTHYLAB's segment turnaround.
   - **Executed but not confirmable:** LUMAXIND's order-book and utilisation catalysts, and AURIONPRO's order books.
   - **Confirmed:** none.
5. **Inflection "catches" overstate the detector:**
   - **RAIN's two windows** are credited to the price alerts.
   - **JYOTHYLAB's** inflection (TTM owners' PAT 376 → 643 cr) is credited to an alert about an "others" segment moving from −0.3 to +1.1 cr. That is immaterial to a company of that size and unrelated to the earnings rise.
   - **The metric is too loose:** any alert before the window counts as a catch, whatever it is about.
6. **Earnings delivery** was measurable for 14 alerts, and 7 delivered. All 14 are RAIN's price alerts, so this says nothing about the detector in general.

**Overall:** on a neutral sample, the rules are safe on dating, but they are not yet a credible early-detection tool. Precision suffers from price statements and immaterial catalysts; recall suffers from coverage gaps and from inflections that have no disclosed forward catalyst.

## Defects recorded (not fixed during this run)

| # | Defect | Evidence | Affects |
|---|---|---|---|
| D12 | Price-change catalysts ignore direction, and every quarterly restatement is a new catalyst | RAIN: 17 price catalysts, including "realisations decreased by 15.4%" | False alerts, workload |
| D13 | An alert can be raised without materiality, and the inflection metric credits any earlier alert by timing alone | JYOTHYLAB: a ₹1.4 cr "others" segment turnaround is credited with a ₹267 cr PAT inflection | Precision, inflection metric |
| D14 | Results coverage collapses for some large issuers | TATACHEM: 5 quarters, no EBITDA; BATAINDIA: 6; CESC: no EBITDA | Recall, verdicts |
| D15 | Suspicious order-book bases and near-duplicate readings | AURIONPRO: 200 → 900 and 200 → 800 cr three days apart (12.5% apart, so not merged) | Precision, workload |

Fixing these needs catalyst-rules-6 and another fresh, pre-registered cohort. These 12 issuers are now development issuers.

## Coverage

| Issuer | Docs with text | Skipped (no text) | Failed downloads | Annual reports | Credit-rating filings | Quarters parsed (first – last) | Scope | Snapshot errors |
|---|---|---|---|---|---|---|---|---|
| PRICOLLTD | 241 | 2 | 7 | 4 | 14 | 15 (2022-03-31 – 2025-09-30) | consolidated | 0 |
| RAIN | 200 | 0 | 3 | 2 | 4 | 24 (2019-12-31 – 2025-09-30) | standalone | 0 |
| TATACHEM | 408 | 0 | 13 | 5 | 18 | 5 (2020-09-30 – 2022-03-31) | consolidated | 0 |
| BATAINDIA | 271 | 0 | 5 | 5 | 1 | 6 (2020-03-31 – 2023-12-31) | standalone | 0 |
| CESC | 232 | 1 | 2 | 3 | 0 | 10 (2022-12-31 – 2025-09-30) | consolidated | 0 |
| AURIONPRO | 323 | 12 | 3 | 4 | 1 | 20 (2020-06-30 – 2025-09-30) | consolidated | 0 |
| HGINFRA | 395 | 0 | 1 | 5 | 8 | 21 (2020-03-31 – 2025-09-30) | consolidated | 0 |
| GPPL | 141 | 0 | 1 | 4 | 0 | 22 (2020-03-31 – 2025-09-30) | consolidated | 0 |
| AFFLE | 527 | 1 | 7 | 5 | 0 | 15 (2020-03-31 – 2025-09-30) | standalone | 0 |
| RBA | 299 | 0 | 7 | 4 | 6 | 17 (2021-03-31 – 2025-09-30) | consolidated | 0 |
| LUMAXIND | 301 | 1 | 2 | 4 | 6 | 19 (2020-03-31 – 2025-06-30) | consolidated | 0 |
| JYOTHYLAB | 345 | 1 | 3 | 5 | 4 | 13 (2020-03-31 – 2025-09-30) | standalone | 0 |

**Quarterly figures parsed at the cut-off** (consolidated series; 2020-Q1 … 2025-Q3 ≈ 23 possible):

| Issuer | Revenue | EBITDA | PAT | PAT attributable to owners |
|---|---|---|---|---|
| PRICOLLTD | 15 | 15 | 15 | 0 |
| RAIN | 24 | 22 | 24 | 0 |
| TATACHEM | 5 | 0 | 2 | 0 |
| BATAINDIA | 6 | 6 | 9 | 0 |
| CESC | 10 | 0 | 13 | 0 |
| AURIONPRO | 20 | 19 | 13 | 18 |
| HGINFRA | 21 | 20 | 20 | 3 |
| GPPL | 22 | 22 | 3 | 0 |
| AFFLE | 15 | 12 | 21 | 2 |
| RBA | 17 | 17 | 16 | 9 |
| LUMAXIND | 19 | 19 | 16 | 0 |
| JYOTHYLAB | 13 | 13 | 14 | 2 |

Expected quarters 2021-Q1 … 2025-Q3 ≈ 19 per issuer; fewer parsed quarters are coverage gaps (scanned results, unparsed tables), not detector verdicts.

## Alerts and verdicts

| Issuer | Catalyst | First public | Supported at | Stage path | Verdict | Snapshots flagged | Earnings delivery |
|---|---|---|---|---|---|---|---|
| RAIN | contract_pricing: issuer states (2021-04-29): "Further, the average blended realisation increased by ~12.0%  | 2021-04-29 | 2023-05-09 | potential_catalyst (2021-05-31) → data_unavailable (2021-08-31) → execution_validating (2023-05-31) → data_unavailable (2023-08-31) → contradicted (2023-11-30) | **contradicted** | 1 | delivered: TTM revenue 57→136, parent PAT 28.3→50.6 (2023-03-31→2024-03-31) |
| RAIN | contract_pricing: issuer states (2021-07-31): "Further, the average blended realisation increased by ~36.3%  | 2021-07-31 | 2022-11-03 | potential_catalyst (2021-08-31) → data_unavailable (2021-11-30) → execution_validating (2022-11-30) → delayed (2023-02-28) → execution_validating (2023-05-31) → contradicted (2023-08-31) | **contradicted** | 2 | not delivered: TTM revenue 58→108, parent PAT 60.0→57.7 (2022-09-30→2023-09-30) |
| RAIN | contract_pricing: issuer states (2021-10-30): "During Q3 CY21, the average blended realisation increased by  | 2021-10-30 | 2022-11-03 | potential_catalyst (2021-11-30) → data_unavailable (2022-02-28) → contradicted (2022-05-31) → execution_validating (2022-11-30) → delayed (2023-02-28) → execution_validating (2023-05-31) → contradicted (2023-08-31) | **contradicted** | 2 | not delivered: TTM revenue 58→108, parent PAT 60.0→57.7 (2022-09-30→2023-09-30) |
| RAIN | contract_pricing: issuer states (2022-02-25): "During Q4 CY21, the average blended realisation increased by  | 2022-02-25 | 2022-11-03 | potential_catalyst (2022-02-28) → delayed (2022-05-31) → execution_validating (2022-11-30) → delayed (2023-02-28) → execution_validating (2023-05-31) → contradicted (2023-08-31) | **contradicted** | 2 | not delivered: TTM revenue 58→108, parent PAT 60.0→57.7 (2022-09-30→2023-09-30) |
| RAIN | contract_pricing: issuer states (2022-05-04): "Further, the average blended realisation increased by ~63.0%  | 2022-05-04 | 2022-07-29 | potential_catalyst (2022-05-31) → execution_validating (2022-08-31) → delayed (2023-02-28) → execution_validating (2023-05-31) → contradicted (2023-08-31) → execution_validating (2024-11-30) → contradicted (2025-05-31) → execution_validating (2025-11-30) | **executed_unconfirmable** | 7 | delivered: TTM revenue 56→71, parent PAT 27.7→66.7 (2022-06-30→2023-06-30) |
| RAIN | contract_pricing: issuer states (2022-07-29): "Further, the average blended realisation increased by ~78.0%  | 2022-07-29 | 2023-05-09 | potential_catalyst (2022-08-31) → delayed (2022-11-30) → execution_validating (2023-05-31) → contradicted (2023-08-31) → execution_validating (2024-11-30) → contradicted (2025-05-31) | **contradicted** | 3 | delivered: TTM revenue 57→136, parent PAT 28.3→50.6 (2023-03-31→2024-03-31) |
| RAIN | contract_pricing: issuer states (2022-11-03): "During Q3 CY22, the average blended realisation increased by  | 2022-11-03 | 2023-05-09 | potential_catalyst (2022-11-30) → execution_validating (2023-05-31) → contradicted (2023-08-31) → execution_validating (2024-11-30) → contradicted (2025-05-31) → execution_validating (2025-11-30) | **executed_unconfirmable** | 5 | delivered: TTM revenue 57→136, parent PAT 28.3→50.6 (2023-03-31→2024-03-31) |
| RAIN | contract_pricing: issuer states (2023-02-27): "During Q4 CY22, the average blended realisation increased by  | 2023-02-27 | 2023-05-09 | potential_catalyst (2023-02-28) → execution_validating (2023-05-31) → contradicted (2023-08-31) → execution_validating (2024-11-30) → contradicted (2025-05-31) → execution_validating (2025-08-31) | **executed_unconfirmable** | 6 | delivered: TTM revenue 57→136, parent PAT 28.3→50.6 (2023-03-31→2024-03-31) |
| RAIN | contract_pricing: issuer states (2023-05-09): "During Q1 CY23, the average blended realisation increased by  | 2023-05-09 | 2024-08-06 | potential_catalyst (2023-05-31) → delayed (2023-08-31) → contradicted (2023-11-30) → execution_validating (2024-08-31) | **executed_unconfirmable** | 7 | delivered: TTM revenue 140→145, parent PAT 12.1→43.2 (2024-06-30→2025-06-30) |
| RAIN | contract_pricing: issuer states (2023-05-16): "The average blended realisation increased by ~28.7%, driven b | 2023-05-16 | 2024-08-06 | potential_catalyst (2023-05-31) → delayed (2023-08-31) → contradicted (2023-11-30) → execution_validating (2024-08-31) | **executed_unconfirmable** | 7 | delivered: TTM revenue 140→145, parent PAT 12.1→43.2 (2024-06-30→2025-06-30) |
| RAIN | contract_pricing: issuer states (2024-02-24): "During Q4 CY23, realisations decreased by 15.4% due to fall i | 2024-02-24 | 2024-11-06 | potential_catalyst (2024-02-28) → delayed (2024-05-31) → contradicted (2024-08-31) → execution_validating (2024-11-30) | **executed_unconfirmable** | 6 | not delivered: TTM revenue 135→127, parent PAT 18.3→24.9 (2024-09-30→2025-09-30) |
| RAIN | contract_pricing: issuer states (2024-05-09): "During Q1 CY24, realisations decreased by 4.5% due to fall in | 2024-05-09 | 2024-11-06 | potential_catalyst (2024-05-31) → delayed (2024-08-31) → execution_validating (2024-11-30) → data_unavailable (2025-05-31) → delayed (2025-08-31) → execution_validating (2025-11-30) | **executed_unconfirmable** | 4 | not delivered: TTM revenue 135→127, parent PAT 18.3→24.9 (2024-09-30→2025-09-30) |
| RAIN | contract_pricing: issuer states (2024-05-10): "During First Quarter CY24, realizations decreased by 4.5% due | 2024-05-10 | 2024-11-06 | potential_catalyst (2024-05-31) → delayed (2024-08-31) → execution_validating (2024-11-30) → data_unavailable (2025-05-31) → delayed (2025-08-31) → execution_validating (2025-11-30) | **executed_unconfirmable** | 4 | not delivered: TTM revenue 135→127, parent PAT 18.3→24.9 (2024-09-30→2025-09-30) |
| RAIN | contract_pricing: issuer states (2024-08-06): "During Q2 of 2024, realisations decreased by 16.1%, due to fa | 2024-08-06 | 2024-11-06 | potential_catalyst (2024-08-31) → execution_validating (2024-11-30) → data_unavailable (2025-05-31) → execution_validating (2025-11-30) | **executed_unconfirmable** | 4 | not delivered: TTM revenue 135→127, parent PAT 18.3→24.9 (2024-09-30→2025-09-30) |
| RAIN | contract_pricing: issuer states (2024-11-06): "During Q3 2024, average realisations decreased by 9.5% due to | 2024-11-06 | 2025-11-06 | potential_catalyst (2024-11-30) → data_unavailable (2025-02-28) → execution_validating (2025-11-30) | **executed_unconfirmable** | 2 | not measurable |
| RAIN | contract_pricing: issuer states (2025-02-25): "During Q4 2024, average blended realisation decreased by 3.6% | 2025-02-25 | 2025-08-06 | potential_catalyst (2025-02-28) → execution_validating (2025-08-31) | **open** | 3 | not measurable |
| AURIONPRO | executable_orders: stated order book 200.0 -> 900.0 cr (+350%; disclosed 2023-08-02 -> 2024-02-05) | 2024-02-05 | 2024-05-14 | potential_catalyst (2024-02-28) → execution_validating (2024-05-31) | **executed_unconfirmable** | 8 | not measurable |
| AURIONPRO | executable_orders: stated order book 200.0 -> 800.0 cr (+300%; disclosed 2023-08-02 -> 2024-02-08) | 2024-02-08 | 2024-05-14 | potential_catalyst (2024-02-28) → execution_validating (2024-05-31) | **executed_unconfirmable** | 8 | not measurable |
| AURIONPRO | product_mix: product_customer_mix: sale of software services: share of segment revenue 69.2% -> 75.0% ( | 2025-07-22 | 2025-07-22 | supported_prospective_inflection (2025-08-31) → data_unavailable (2025-11-30) → delayed (2025-12-31) | **delayed** | 1 | not measurable |
| HGINFRA | executable_orders: issuer-disclosed binding orders 1,580.2 cr in 12 months = 0.40x TTM revenue known then | 2023-03-22 | 2023-05-10 | execution_validating (2023-05-31) → delayed (2024-05-31) → execution_validating (2024-11-30) → contradicted (2025-02-28) | **contradicted** | 5 | not measurable |
| RBA | product_mix: product_customer_mix: india: share of segment revenue 67.2% -> 72.1% (margin gap 18.9 pp v | 2023-05-17 | 2023-05-17 | supported_prospective_inflection (2023-05-31) → delayed (2023-11-30) | **delayed** | 2 | not measurable |
| LUMAXIND | financing_cost: debt_reduction: net debt (borrowings - cash) 2020-03-31: 18.4 -> 2021-03-31: -2.8 | 2021-06-11 | 2021-08-06 | execution_validating (2021-08-31) → delayed (2022-08-31) → contradicted (2022-11-30) | **contradicted** | 4 | not measurable |
| LUMAXIND | utilization: utilization: stated utilisation at company-level, 2021-10-28 -> 2022-03-15 | 2022-05-24 | 2023-06-08 | potential_catalyst (2022-05-31) → execution_validating (2023-08-31) | **executed_unconfirmable** | 11 | not measurable |
| LUMAXIND | executable_orders: stated order book 1,000.0 -> 1,300.0 cr (+30%; disclosed 2022-11-25 -> 2023-06-08) | 2023-06-08 | 2023-08-09 | execution_validating (2023-08-31) | **executed_unconfirmable** | 11 | not measurable |
| LUMAXIND | executable_orders: stated order book 1,300.0 -> 2,200.0 cr (+69%; disclosed 2023-06-08 -> 2023-11-16) | 2023-11-16 | 2024-05-26 | potential_catalyst (2023-11-30) → execution_validating (2024-05-31) | **executed_unconfirmable** | 8 | not measurable |
| LUMAXIND | executable_orders: stated order book 2,200.0 -> 2,900.0 cr (+32%; disclosed 2024-02-21 -> 2024-11-23) | 2024-11-23 | 2025-02-10 | potential_catalyst (2024-11-30) → execution_validating (2025-02-28) | **open** | 5 | not measurable |
| JYOTHYLAB | segment_turnaround: segment_turnaround: others: segment result -0.3 -> 1.1 cr vs year-ago period | 2025-05-12 | 2025-08-12 | potential_catalyst (2025-05-31) → execution_validating (2025-08-31) → contradicted (2025-11-30) | **contradicted** | 1 | not measurable |

## Missed inflections (mechanical: TTM parent PAT +50% over 4 periods from a positive base)

| Issuer | Window | TTM parent PAT | Alert before | Classification |
|---|---|---|---|---|
| RAIN | 2021-09-30 → 2024-03-31 | 28.0 → 50.6 | e78bc8c82285, 6c725c4732cd, 187931b3680e, 26e4a1540c14, 36a63d760db3, 9553464439f1, 80160218c9e2, cdc4e334efdb | caught  |
| RAIN | 2024-06-30 → 2025-06-30 | 12.1 → 43.2 | 9ad4c99a3687, 3220e10f2fe0, 441328d87514, c128deb0d94c, df734e97e210, 13dd26fbdc81, fe90e2357d49 | caught  |
| AFFLE | 2021-06-30 → 2024-06-30 | 39.6 → 88.5 | — | missed (detector: 1 candidate(s) existed but none reached supported) |
| JYOTHYLAB | 2024-09-30 → 2025-09-30 | 375.9 → 643.1 | 4c1e29ded920 | caught  |

## Review workload

| Issuer | Alerts / year | Snapshots in a flag lane (of total) | Documents | Catalysts (by kind) |
|---|---|---|---|---|
| PRICOLLTD | 0.0 | 0 / 20 | 241 | capacity_or_bottleneck 1 |
| RAIN | 3.37 | 10 / 20 | 200 | contract_pricing 17, utilization 1 |
| TATACHEM | 0.0 | 0 / 20 | 408 | capacity_or_bottleneck 1, utilization 1 |
| BATAINDIA | 0.0 | 0 / 20 | 271 | contract_pricing 2 |
| CESC | 0.0 | 0 / 20 | 232 |  |
| AURIONPRO | 0.63 | 8 / 20 | 323 | customer_approval 2, executable_orders 2, product_mix 2 |
| HGINFRA | 0.21 | 5 / 20 | 395 | executable_orders 2 |
| GPPL | 0.0 | 0 / 20 | 141 |  |
| AFFLE | 0.0 | 0 / 20 | 527 | product_mix 1 |
| RBA | 0.21 | 2 / 20 | 299 | customer_approval 1, product_mix 1 |
| LUMAXIND | 1.05 | 15 / 20 | 301 | executable_orders 4, financing_cost 1, utilization 1 |
| JYOTHYLAB | 0.21 | 1 / 20 | 345 | segment_turnaround 2 |
