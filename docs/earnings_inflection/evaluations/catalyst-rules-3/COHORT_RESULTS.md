# Rules-3 unfamiliar cohort — results

Pre-registered in `COHORT_PREREGISTRATION.md` before any data was fetched. Rules **catalyst-rules-3**, fingerprint `1b744b3fe6944180`, unchanged during the run. Quarterly as-of dates 2021-05-31 … 2025-11-30 plus a 2025-12-31 cut-off; each run sees only documents public by its date. Seven issuers: every rate below is anecdotal and given with its counts. No prices or returns.

## Headline

- **Historical correctness on real data:** 0 date changes and 0 vanished catalysts across all snapshots (a catalyst's first-public / support / validation / confirmation dates never changed once recorded).
- **Alerts (supported or better):** 19 across 3 of 7 issuers; verdicts at the cut-off: confirmed 5, data_unavailable 2, delayed 6, open 6.
- **False alerts (contradicted or delayed):** 6 of 19.
- **Earnings delivery (measurable alerts):** 0 of 0 delivered (TTM PAT +15% and revenue up, 4 periods after support).
- **Mechanical earnings inflections:** 0; missed (no alert before the window's end was published): 0.

## What this shows

1. **Historical correctness held on real data.** Across 7 issuers × 21 snapshots and the 67-month INDOTECH replay, no catalyst's first-public, support, validation or confirmation date changed once it was recorded, and no catalyst disappeared. This is the property the round-3 fixes were meant to secure.
2. **Coverage, not detection, dominates the result.** Parsing of quarterly results is the binding constraint:
   - **Parent profit:** owners' share of profit ("PAT attributable to owners") was parsed for **1 of 7** issuers (OLECTRA, 9 quarters). Earnings delivery, the earnings-materiality bridge and the mechanical inflection test therefore could not be measured for any alert. The rules correctly leave these unresolved rather than assuming a 100% parent share.
   - **Missing quarters:** GMMPFAUDLR has 3 parsed revenue quarters, all before 2021, and GENUSPOWER has 7, all from 2024 onward. Their catalysts end "data unavailable": a coverage verdict, not a business one.
   - **Inflection test:** consequently **0** mechanical earnings inflections were measurable, so the missed-inflection rate is **not measured**, not zero.
3. **Alerts: 19, concentrated in 3 issuers.**
   - SWSOLAR has 13 and TARIL has 5, all order-based; BORORENEW has 1, a realised-price statement.
   - At the cut-off: 5 confirmed (all SWSOLAR order books), 6 delayed, 6 open, 2 data unavailable.
4. **The 6 "false alerts" (TARIL 1, SWSOLAR 5) are mostly a detector defect, not business failures.**
   - **TARIL.** The delayed alert is an order inflow (2,050 cr, 1.58× TTM revenue). TTM revenue then rose 52% (1,461 → 2,227 cr). It was marked delayed only because its execution window expired. An inflow without a disclosed backlog is never sized, so it can never confirm and can only expire.
   - **SWSOLAR.** Five alerts are delayed:
     - three January-2024 order-book or inflow catalysts, after which TTM revenue rose from 3,435 to 7,148 cr;
     - one July-2024 inflow, whose delivery is not yet measurable;
     - one product-mix catalyst, during which TTM revenue rose from 1,773 to 3,706 cr.
   - **BORORENEW.** The only alert that behaves like a true failure is BORORENEW's: realisation up 5%, then price declines, contradicted in 3 snapshots. It is **not** counted as a false alert, because a later missing quarter turned its verdict at the cut-off into "data unavailable". So the false-alert count is both inflated (by expiries) and deflated (by masking).
5. **Review workload is inflated by duplicates and misreads.**
   - **Duplicates:** SWSOLAR's 13 alerts include restatements of the same order book within days (4,903 / 4,900 cr on 13 and 18 July 2023; 8,750 / 8,000 cr in January 2024), each counted as a separate catalyst.
   - **Misreads:** GENUSPOWER's 20 order-book "catalysts" include misread values (18 → 1,761 cr, +9,683%; 0.7 → 21,006 cr) that passed with no plausibility guard.
6. **Missed by design or coverage.**
   - OLECTRA's large bus-order disclosures produced no order catalyst; its single catalyst is a customer approval.
   - HBLENGINE produced only a customer approval and a segment turnaround.
   - Whether these are extraction or detector misses needs document review; the mechanical test could not run.

## Defects recorded for the next rules version (not fixed during this run)

| # | Defect | Evidence | Affects |
|---|---|---|---|
| D1 | Consolidated "profit attributable to owners of the parent" not parsed | parsed for 1 of 7 issuers | coverage, materiality, delivery and inflection metrics |
| D2 | Whole years of results unparsed for some issuers | GMMPFAUDLR 3 quarters, GENUSPOWER 7 | coverage |
| D3 | An order inflow that can never be sized expires to "delayed" | TARIL, SWSOLAR | false-alert count |
| D4 | A later missing quarter masks an earlier contradiction ("data unavailable" overrides "contradicted") | BORORENEW | false-alert count |
| D5 | Order-book restatements within days, or values within ~10%, become separate catalysts | SWSOLAR | workload |
| D6 | No plausibility bound on order-book readings or jumps | GENUSPOWER | precision, workload |
| D7 | Large order announcements not extracted as order events | OLECTRA | recall (unverified) |

Under the pre-registration these are reported, not fixed. Fixing them requires a new rules version (catalyst-rules-4) and a new pre-registered cohort. Evaluating rules-4 on these same seven issuers would be tuning to them.

## Coverage

| Issuer | Docs with text | Skipped (no text) | Failed downloads | Annual reports | Credit-rating filings | Quarters parsed (first – last) | Scope | Snapshot errors |
|---|---|---|---|---|---|---|---|---|
| GENUSPOWER | 307 | 1 | 0 | 5 | 6 | 7 (2024-03-31 – 2025-09-30) | consolidated | 0 |
| HBLENGINE | 110 | 4 | 0 | 5 | 4 | 11 (2021-09-30 – 2025-06-30) | consolidated | 0 |
| TARIL | 241 | 2 | 0 | 6 | 5 | 10 (2023-03-31 – 2025-06-30) | consolidated | 0 |
| OLECTRA | 252 | 3 | 0 | 5 | 6 | 21 (2019-12-31 – 2025-09-30) | consolidated | 0 |
| BORORENEW | 344 | 2 | 1 | 5 | 7 | 12 (2022-06-30 – 2025-09-30) | consolidated | 0 |
| GMMPFAUDLR | 320 | 2 | 4 | 4 | 15 | 3 (2019-12-31 – 2020-12-31) | consolidated | 0 |
| SWSOLAR | 367 | 2 | 2 | 5 | 26 | 23 (2020-03-31 – 2025-09-30) | consolidated | 0 |

**Quarterly figures parsed at the cut-off** (consolidated series; 2020-Q1 … 2025-Q3 ≈ 23 possible):

| Issuer | Revenue | EBITDA | PAT | PAT attributable to owners |
|---|---|---|---|---|
| GENUSPOWER | 7 | 2 | 11 | 0 |
| HBLENGINE | 11 | 0 | 3 | 0 |
| TARIL | 10 | 8 | 0 | 0 |
| OLECTRA | 21 | 11 | 9 | 9 |
| BORORENEW | 12 | 11 | 3 | 0 |
| GMMPFAUDLR | 3 | 8 | 0 | 0 |
| SWSOLAR | 23 | 21 | 2 | 0 |

Expected quarters 2021-Q1 … 2025-Q3 ≈ 19 per issuer; fewer parsed quarters are coverage gaps (scanned results, unparsed tables), not detector verdicts.

## Alerts and verdicts

| Issuer | Catalyst | First public | Supported at | Stage path | Verdict | Snapshots flagged | Earnings delivery |
|---|---|---|---|---|---|---|---|
| TARIL | executable_orders: issuer-disclosed binding orders 2,050.0 cr in 12 months = 1.58x TTM revenue known then | 2024-04-08 | 2024-07-19 | potential_catalyst (2024-05-31) → execution_validating (2024-08-31) → delayed (2025-08-31) | **delayed** | 4 | not measurable |
| TARIL | executable_orders: stated order book 2,600.0 -> 3,500.0 cr (+35%; disclosed 2024-04-15 -> 2024-10-08) | 2024-10-08 | 2025-01-08 | potential_catalyst (2024-11-30) → execution_validating (2025-02-28) | **open** | 5 | not measurable |
| TARIL | executable_orders: stated order book 3,500.0 -> 5,132.0 cr (+47%; disclosed 2024-10-10 -> 2025-04-15) | 2025-04-15 | 2025-08-01 | potential_catalyst (2025-05-31) → execution_validating (2025-08-31) | **open** | 3 | not measurable |
| TARIL | executable_orders: issuer-disclosed binding orders 801.6 cr in 12 months = 0.40x TTM revenue known then | 2025-04-21 | 2025-08-01 | potential_catalyst (2025-05-31) → execution_validating (2025-08-31) | **open** | 3 | not measurable |
| TARIL | executable_orders: issuer-disclosed binding orders 726.0 cr in 12 months = 0.36x TTM revenue known then | 2025-04-23 | 2025-08-01 | potential_catalyst (2025-05-31) → execution_validating (2025-08-31) | **open** | 3 | not measurable |
| BORORENEW | contract_pricing: issuer states (2022-08-12): "The turnover is down 5%, wherein you’re seeing a realization  | 2022-08-12 | 2022-11-09 | potential_catalyst (2022-08-31) → execution_validating (2022-11-30) → contradicted (2023-02-28) → data_unavailable (2023-08-31) → contradicted (2023-11-30) → data_unavailable (2024-08-31) → contradicted (2025-08-31) → data_unavailable (2025-11-30) | **data_unavailable** | 1 | not measurable |
| SWSOLAR | product_mix: product_customer_mix: operation and maintenance service: share of segment revenue 3.3% ->  | 2023-01-19 | 2023-10-21 | potential_catalyst (2023-02-28) → data_unavailable (2023-05-31) → contradicted (2023-08-31) → execution_validating (2023-11-30) → delayed (2024-02-28) | **delayed** | 1 | not measurable |
| SWSOLAR | executable_orders: stated order book 2,654.0 -> 4,000.0 cr (+51%; disclosed 2022-10-18 -> 2023-03-27) | 2023-03-27 | 2023-03-27 | data_unavailable (2023-05-31) → contradicted (2023-08-31) → confirmed_for_investment_review (2024-08-31) | **confirmed** | 7 | not measurable |
| SWSOLAR | executable_orders: stated order book 2,654.0 -> 4,913.0 cr (+85%; disclosed 2022-10-18 -> 2023-04-20) | 2023-04-20 | 2024-07-18 | potential_catalyst (2023-05-31) → delayed (2023-08-31) → contradicted (2023-11-30) → confirmed_for_investment_review (2024-08-31) | **confirmed** | 7 | not measurable |
| SWSOLAR | executable_orders: stated order book 2,703.0 -> 4,903.0 cr (+81%; disclosed 2023-01-25 -> 2023-07-13) | 2023-07-13 | 2023-10-21 | potential_catalyst (2023-08-31) → execution_validating (2023-11-30) → delayed (2024-11-30) → confirmed_for_investment_review (2025-02-28) | **confirmed** | 9 | not measurable |
| SWSOLAR | segment_turnaround: segment_turnaround: epc business: segment result -181.1 -> 55.6 cr vs year-ago period | 2023-07-13 | 2023-10-21 | potential_catalyst (2023-08-31) → execution_validating (2023-11-30) → delayed (2024-08-31) → data_unavailable (2025-02-28) | **data_unavailable** | 3 | not measurable |
| SWSOLAR | executable_orders: stated order book 2,703.0 -> 4,900.0 cr (+81%; disclosed 2023-01-25 -> 2023-07-18) | 2023-07-18 | 2023-10-21 | potential_catalyst (2023-08-31) → execution_validating (2023-11-30) → delayed (2024-11-30) → confirmed_for_investment_review (2025-02-28) | **confirmed** | 9 | not measurable |
| SWSOLAR | executable_orders: stated order book 4,900.0 -> 8,750.0 cr (+79%; disclosed 2023-07-18 -> 2024-01-18) | 2024-01-18 | 2024-07-18 | potential_catalyst (2024-02-28) → data_unavailable (2024-05-31) → execution_validating (2024-08-31) → delayed (2025-05-31) | **delayed** | 3 | not measurable |
| SWSOLAR | executable_orders: issuer-disclosed binding orders 4,515.0 cr in 12 months = 2.55x TTM revenue known then | 2024-01-18 | 2024-07-18 | potential_catalyst (2024-02-28) → data_unavailable (2024-05-31) → execution_validating (2024-08-31) → delayed (2025-05-31) | **delayed** | 3 | not measurable |
| SWSOLAR | executable_orders: stated order book 4,900.0 -> 8,000.0 cr (+63%; disclosed 2023-07-18 -> 2024-01-24) | 2024-01-24 | 2024-07-18 | potential_catalyst (2024-02-28) → data_unavailable (2024-05-31) → execution_validating (2024-08-31) → delayed (2025-05-31) | **delayed** | 3 | not measurable |
| SWSOLAR | executable_orders: issuer-disclosed binding orders 2,170.0 cr in 12 months = 0.71x TTM revenue known then | 2024-07-18 | 2025-01-16 | potential_catalyst (2024-08-31) → data_unavailable (2024-11-30) → execution_validating (2025-02-28) → delayed (2025-11-30) | **delayed** | 3 | not measurable |
| SWSOLAR | executable_orders: issuer-disclosed binding orders 2,421.0 cr in 12 months = 0.80x TTM revenue known then | 2024-07-24 | 2025-01-16 | potential_catalyst (2024-08-31) → data_unavailable (2024-11-30) → execution_validating (2025-02-28) | **open** | 5 | not measurable |
| SWSOLAR | executable_orders: issuer-disclosed binding orders 4,726.0 cr in 12 months = 1.56x TTM revenue known then | 2024-10-14 | 2025-01-16 | potential_catalyst (2024-11-30) → execution_validating (2025-02-28) | **open** | 5 | not measurable |
| SWSOLAR | executable_orders: stated order book 8,000.0 -> 10,549.0 cr (+32%; disclosed 2024-04-25 -> 2024-10-22) | 2024-10-22 | 2025-01-16 | potential_catalyst (2024-11-30) → execution_validating (2025-02-28) → confirmed_for_investment_review (2025-05-31) | **confirmed** | 5 | not measurable |

## Missed inflections (mechanical: TTM parent PAT +50% over 4 periods from a positive base)

| Issuer | Window | TTM parent PAT | Alert before | Classification |
|---|---|---|---|---|
| — | none measurable (see coverage: parent PAT series incomplete where noted) | | | |

## Review workload

| Issuer | Alerts / year | Snapshots in a flag lane (of total) | Documents | Catalysts (by kind) |
|---|---|---|---|---|
| GENUSPOWER | 0.0 | 0 / 20 | 307 | executable_orders 20, utilization 1 |
| HBLENGINE | 0.0 | 0 / 20 | 110 | customer_approval 1, segment_turnaround 1 |
| TARIL | 1.05 | 7 / 20 | 241 | executable_orders 10 |
| OLECTRA | 0.0 | 0 / 20 | 252 | customer_approval 1 |
| BORORENEW | 0.21 | 1 / 20 | 344 | capacity_or_bottleneck 1, contract_pricing 4 |
| GMMPFAUDLR | 0.0 | 0 / 20 | 320 |  |
| SWSOLAR | 2.74 | 10 / 20 | 367 | customer_approval 1, executable_orders 14, product_mix 1, segment_turnaround 1 |
