# Company-level earnings-inflection tracker: recommendation vs. what is built

Compares the recommended process (seven points) with the code in `src/makrograph/earnings_inflection/` as of commit `2319e44`.
The recommendation asks for **no further software implementation yet**. This document only records what already happens, what partly happens and what does not. It is meant as the checklist to freeze before testing.

Legend: ✅ happening · 🟡 partly · ❌ not built · ⛔ deliberately excluded by the original spec

## 1. Rolling evidence file per company

| Recommendation | Status | What the code actually does |
|---|---|---|
| ~8 quarters of results and calls, latest 2 annual reports, later announcements | 🟡 | Uses **every** document for the ticker that is public by the as-of date. There's no 8-quarter window and no selection of "latest two annual reports". Annual reports are mostly absent from ingestion anyway (see `IMPLEMENTATION_REPORT.md`, gap 2). |
| Persistent, rolling file | ❌ | Each run rebuilds from source documents. Nothing persists between runs; the optional DB persistence is off and stores only snapshots. |
| Claim | ✅ | `Evidence.quote`: the exact sentence, checked verbatim against the source. |
| Source and publication date | ✅ | Document id, `available_at` (exchange timestamp, or end of day for a date-only filing) and page number (accurate since the `\f` page-break change). |
| Business affected | 🟡 | Captured only from phrases like "in the X segment". Plants, products and subsidiaries aren't captured. |
| Starting point (current utilisation, capacity, revenue, margin) | ❌ | Claims aren't linked to a baseline. The company-level revenue and margin series exist separately. |
| Expected change (target, range, deadline) | 🟡 | Value or range and target period for **revenue-growth, revenue and margin guidance** only. Capacity/utilisation targets ("70% utilisation") and commissioning deadlines aren't ledgered. |
| Supporting evidence linked to the claim | ❌ | Orders, shipments, commissioning and financing aren't linked to the claim they support. |
| Contradictions | 🟡 | Missed or lowered guidance, negated forward statements and failed reconciliations are listed. Delays, weaker demand and higher costs aren't detected. |
| Verification status (assertion / commitment / realized) | ✅ | `EvidenceTier` on every record. |
| Repeated guidance = consistency, not new evidence | ✅ | A repeat is recorded as `REITERATED` under the original and adds no weight. |
| Same order across announcement / presentation / call = one event | ✅ | `event_resolution.py` merges on amount, counterparty and a time window, regardless of date. A "repeat order" stays a separate event. Tested. |

## 2. Detect the right change for each driver

| Driver | Status | Notes |
|---|---|---|
| Demand: volumes, order inflow, cancellations, delivery schedules | 🟡 | Order inflow (deduplicated, with binding / provisional / non-binding strength) and order-book cover only. No physical volumes, cancellations or delivery schedules. Enquiries and MoUs are already classed as non-binding. |
| Management guidance: original vs progress vs delivery | 🟡 | Original vs revisions vs realized, for revenue, growth and margin. Operational milestones aren't tracked. |
| Operating margins: gross, EBITDA, EBIT, per-unit profit | 🟡 | **EBITDA margin only.** No gross margin, **no EBIT margin**, no per-unit figures. The "new capacity lifts EBITDA but depreciation eats it" safeguard is therefore **not implemented**. |
| Utilisation: production vs comparable capacity | 🟡 | Only management-stated utilisation, labelled "management-stated, not observed". **Revenue is never used as a utilisation proxy**, which matches the safeguard. |
| Mix: segment share and profitability | ❌ | Segment reporting tables aren't parsed. |
| Debt: borrowings, interest, operating cash flow | ❌ | Finance cost is read. Borrowings and cash-flow statements aren't parsed. Capex and fund-raise mentions only raise flags. |
| Turnaround: segment losses, cash burn, restructuring | ❌ | Not built. |

## 3. Management judged on delivery

| Recommendation | Status | Notes |
|---|---|---|
| Promise-vs-delivery ledger | 🟡 | Exists for revenue, growth and margin guidance (`guidance_ledger.py`). |
| Capacity commissioning / customer-qualification deadlines | ❌ | Not built. |
| Capex vs budget, debt/working-capital commitments | ❌ | Not built. |
| Dilution, related-party transactions, minority treatment | ❌ | Dilution is only flagged when a fund-raise is mentioned. |
| Was the explanation credible? | ❌ | Needs judgment; not built. |
| Don't punish conservative guidance or justified revisions | **Conflict** | Today **any** lowered or withdrawn guidance makes the status `CONTRADICTED`, whatever the reason. This contradicts the recommendation and should change before testing, for example by recording a revision as "explained / unexplained" for human review. |
| Flag disappearing targets | ❌ | A target that is never repeated stays `PENDING` until its period ends, then becomes `UNVERIFIABLE`. Silence isn't flagged. |
| "Management confidence ≠ customer demand" | ✅ | Forward statements alone can only reach `ASSERTION_ONLY`. |

## 4. Two research routes

| Route | Status | Notes |
|---|---|---|
| Early inflection: customer commitment + executable capacity + unit economics + funding | 🟡 | `COMMITMENT_BACKED` requires deduplicated firm or provisional orders that are material against TTM revenue. **Capacity, unit economics and funding sufficiency aren't checked.** |
| Confirmed acceleration: volume/mix, normalised margins, cash conversion | 🟡 | `EXECUTION_EMERGING` / `EXECUTION_CONFIRMED` use revenue growth, EBITDA margin and PAT, with persistence over at least 2 quarters. No volume, mix or cash conversion. |
| No "every metric must improve" rule | ✅ | Any material realized driver qualifies. Management that gives no guidance can still reach `EXECUTION_CONFIRMED`. |
| Missing information stays unverified | ✅ | `INSUFFICIENT_EVIDENCE` / `NOT_COMPUTED_MISSING_INPUTS`. Nothing is estimated when an input is missing. |

## 5. Earnings bridge

| Recommendation | Status | Notes |
|---|---|---|
| Normalised base earnings | 🟡 | TTM (4 reported quarters, never annualised). Exceptional items are removed, assuming the sign convention described in the implementation report. |
| + sales, + margin/mix, + lower interest/segment losses | 🟡 | Revenue × margin only. Interest and D&A are held flat. No mix or segment-loss term. |
| − additional depreciation, overheads, tax | 🟡 | Tax and minority interest are applied. **No incremental depreciation for new capacity.** |
| ÷ diluted shares | ✅ | Implied from reported PAT and diluted EPS. Future dilution isn't modelled. |
| No double-counting of operating leverage | ✅ | One margin assumption per scenario. Operating leverage is a reported driver, never added into the bridge. |
| Downside / base / upside | 🟡 | Scenarios are trailing run-rate, latest-trend-persists and management case. **There's no true downside case.** |
| Firm PO vs framework | ✅ | Binding / provisional / non-binding. |
| Annual executable revenue vs multi-year headline value | ❌ | Not built. |
| Order value incl. taxes vs accounting revenue | ❌ | Not built. |
| Incremental vs replacement business | ❌ | Not built. |
| Order material relative to existing **earnings** | 🟡 | Materiality is measured against **revenue** (firm orders ≥ 25% of TTM revenue, disclosed inflow / TTM revenue ≥ 1.2, or order book ≥ 2 years of TTM revenue), not earnings. |

## 6. One-page, decision-oriented output

| Question | Status | Notes |
|---|---|---|
| Why investigate now? | ✅ | "What changed", with first-public timestamps. |
| What drives earnings? | 🟡 | A driver table, but not condensed to the 2–3 drivers that matter. |
| How credible? | 🟡 | Tier, commitment strength, guidance ledger, contradictions. No management delivery score. |
| How much can EPS improve? | 🟡 | Scenario table, which isn't conservative enough (see §5). |
| What is already priced in? (valuation, prior price rise) | ⛔ | The original spec excludes prices from detection. Prior price appreciation and valuation aren't shown. |
| What could fail? | 🟡 | Financing and customer risks, missing inputs. |
| **Decision: candidate / wait for price / wait for milestone / pass** | ⛔ **Conflict** | The original spec forbids investment actions, and an output guard blocks that kind of language. **This needs a decision from you** (see below). |
| What changes the decision + review date | 🟡 | "Next checks" lists named milestones. No review dates. |
| One page | ❌ | The Markdown output is several pages. |

## 7. Division of work and cadence

| Recommendation | Status | Notes |
|---|---|---|
| LLM reads and extracts with source references | 🟡 | A constrained LLM extractor exists (verbatim quotes, hard budget) but is **off**. Extraction is deterministic today. |
| Calculations and checks done by code | ✅ | Units, reconciliations, TTM, scenarios. Ownership and dilution are only partly covered. |
| LLM must not certify customers or management | ✅ | The pipeline never sets a reviewed status. Only a human can. |
| Human reviews only the strongest files and open judgments | 🟡 | `review_status` exists. There's no review queue or prioritisation. |
| Event-driven announcements, quarterly reassessment | ❌ | No scheduling (deliberately: run-once only). |

## 8. Freeze and test before thresholds

✅ Agreed, and consistent with the build. Current thresholds are **descriptive defaults that weren't tuned to any company**. The evaluation sandbox exists, but no test has been run. A fair test needs:

- a frozen rule set;
- a pre-registered sample that includes **failed** inflections (missed guidance, cancelled orders, margin reversals), not only winners;
- point-in-time data, including the BSE and annual-report text that ingestion is only now starting to collect.

## Decisions needed before freezing

1. **Revision handling.** Should a justified downward revision stay out of `CONTRADICTED`? (The recommendation says yes; the code currently says no.)
2. **Decision and valuation fields.** Point 6 asks for "investment candidate / wait for price / pass" and valuation context. The original spec forbids both. Keep the tracker evidence-only, or allow these labels as human-entered fields that the system never fills in?
3. **Scope of the frozen v1.** Which of the ❌ items must exist before testing, versus being recorded as known gaps? EBIT margin with incremental depreciation, order executable-revenue and tax adjustments, and capacity/commissioning deadlines matter most for an earnings bridge.
