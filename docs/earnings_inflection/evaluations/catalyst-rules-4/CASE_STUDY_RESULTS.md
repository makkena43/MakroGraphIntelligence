# Case study under catalyst-rules-4: Waaree Energies, Shakti Pumps, SML (Isuzu/Mahindra), Olectra

- **Registration:** pre-registered in `CASE_STUDY_PREREGISTRATION.md` (commit 27dbacd) before any filings were fetched.
- **Rules:** catalyst-rules-4, fingerprint `8d896bb571e58393`, frozen during the run.
- **Replay:** quarterly as-of dates from 2021-05-31 to 2025-11-30, plus a 2025-12-31 cut-off. Each run sees only filings public by its date.
- **Selection:** the user named these companies, so this is a case study of the process, not an unbiased evaluation. Olectra is in-sample.
- **Excluded:** no prices or returns.

## Bottom line

1. **The process ran end to end on all four issuers.** No catalyst's dates changed in any later snapshot, and none vanished.
2. **It did not detect any of the four companies' earnings changes from their disclosures before the results showed them.** The causes are extraction and coverage defects that can be named, not the detection rules:
   - **Shakti Pumps.** Its 2023 PM-KUSUM work orders were disclosed: ₹358 cr on 2023-08-30, ₹293 cr on 2023-09-15 and ₹258 cr on 2023-12-29. The amount sits in the sentence after "received a work order" ("The total amount of the work order is for around Rs. 358 Crores"), and that sentence is not linked to the order (**D8**). No order amount means no order event, and no order event means no catalyst.
     - The run therefore saw only a 2025 order inflow (₹2,500 cr, 0.99× TTM revenue). It is correctly **delayed**: revenue rose only 7.2% year on year in the test window.
     - Meanwhile TTM revenue rose from 763 to 2,307 cr (Sep-23 → Sep-24), and TTM PAT from 44 cr (Dec-22) to 329 cr (Sep-24).
     - PAT was not parsed for the six quarters in between, and owners' profit not at all. The pre-registered inflection test therefore **could not run**.
   - **SML Isuzu / SML Mahindra.** No catalyst of any kind was found.
     - Its filings are mostly monthly sales-volume updates (52 of 276), and no catalyst type reads monthly volume trends (**D11**).
     - Its statements never say "standalone" or "consolidated", so the parent share is unknown under the rules-3 strictness.
     - PAT was parsed only from the March 2024 quarter. The inflection test **could not run**.
   - **Waaree Energies.** Exchange filings start at listing (2024-10-28), so there are 7 quarters of revenue. No catalyst was found.
     - PAT was parsed for 3 quarters and EBITDA for none, so nothing about earnings could be measured.
     - Anything before the listing is outside the reach of exchange filings by design.
   - **Olectra Greentech (in-sample).** There is 1 alert, verdict **confirmed**, and the mechanical inflection test counts the earnings rise (TTM owners' PAT 128.7 → 194.7 cr, Dec-23 → Dec-24) as caught.
     - However, the alert is a **segment-table misread**: "insulator division share of segment revenue 0.1% → 94.9%".
     - The segment parser splits the bus segment into "e-bus", "c-bus" and "bus division", treats a company total as a segment, and mixes full-year with quarter figures (**D10**).
     - So this is **not a genuine detection**. Olectra's large bus orders were again not extracted as order events (D7, probably the same cause as D8).
3. **Measured on all four:** 1 alert, and it is spurious. 0 genuine detections. 0 false alerts by the pre-registered definition. 1 mechanical inflection was measurable (Olectra), and it was "caught" only by the spurious alert.

## Per issuer

| Issuer | Filings with text | Quarters parsed (revenue) | Scope | Owners' PAT parsed | Catalysts | Alerts | Verdicts | Inflection test |
|---|---|---|---|---|---|---|---|---|
| WAAREEENER | 197 | 7 (2024-03 → 2025-09) | consolidated | 0 quarters | 0 | 0 | — | not measurable (no PAT series) |
| SHAKTIPUMP | 315 | 19 (2021-03 → 2025-09) | consolidated | 0 quarters | 1 (2025 order inflow) | 0 | delayed (not an alert) | not measurable (PAT gaps Mar-23 → Jun-24) |
| SMLMAH | 276 | 18 (2021-03 → 2025-09) | not stated | 0 quarters | 0 | 0 | — | not measurable (scope unstated; PAT from Mar-24 only) |
| OLECTRA (in-sample) | 252 | 23 (2019-12 → 2025-09) | consolidated | 17 quarters | 3 | 1 | confirmed, but spurious (D10) | 1 inflection; "caught" only by the spurious alert |

All four issuers: 0 date changes and 0 vanished catalysts across 20 snapshots each.

## New defects (recorded; not fixed during the run)

| # | Defect | Evidence | Effect |
|---|---|---|---|
| D8 | An order amount stated in the sentence after the award ("The total amount of the work order is … Rs. 358 Crores") is not linked to the order | Shakti Pumps' three 2023 KUSUM orders; likely also Olectra's bus orders (D7) | The main order catalysts are missed entirely |
| D9 | **Historical correctness:** re-assessment times come from the *final* financial series. When a later filing supersedes a figure first shown earlier (here an investor-presentation figure replaced by the annual report), the earlier time drops out and a catalyst is re-dated. | BORORENEW debt-reduction catalyst: dated 2025-07-24 in the 2025-08-31 run, 2025-08-30 in later runs, and it vanished from the record (found in the rules-4 re-run of the cohort) | Earlier detection dates can change, which is the property rules-3 was built to guarantee |
| D10 | Segment tables: inconsistent segment names, totals read as segments, and full-year figures read as quarters create false mix shifts | Olectra "insulator 0.1% → 94.9%" | Spurious catalyst confirmed |
| D11 | No catalyst type reads monthly volume disclosures | SML: 52 monthly business updates unused | Volume-led turnarounds are invisible before the quarterly results |
| — | Coverage: unstated statement scope leaves the parent share unknown; PAT is missing for runs of quarters; there is no EBITDA line for some layouts | SML, Shakti, Waaree | The earnings materiality and inflection tests cannot run |

D9 matters most: it breaks the historical-correctness guarantee. The fix is to take re-assessment and mechanism-scan times from every figure's original availability, not from the final series. That needs a new rules version (rules-5).

## Rules-4 re-run of the rules-3 cohort (development check, not an evaluation)

The seven rules-3 cohort issuers exposed D1 and D3, so this re-run only checks that the fixes work.

| | Rules-3 | Rules-4 |
|---|---|---|
| Alerts | 19 | 28 |
| Confirmed | 5 | 10 (Olectra's is the spurious D10 alert) |
| Delayed or contradicted ("false alerts") | 6 | **0** |
| Executed but not confirmable (new D3 verdict) | — | 10 |
| Open / data unavailable | 6 / 2 | 6 / 2 |
| Owners' PAT quarters parsed: TARIL / GENUSPOWER / OLECTRA / BORORENEW / GMMPFAUDLR / HBLENGINE | 0 / 0 / 9 / 0 / 0 / 0 | 7 / 1 / 17 / 13 / 4 / 0 |
| Vanished catalysts | 0 | 1 (BORORENEW, D9) |

SWSOLAR's rules-4 parse coverage was still being computed when this report was written.

The six rules-3 "false alerts" were all executed catalysts that could not be confirmed. Under rules-4 they move to `executed_unconfirmable`, as intended, and no catalyst was hidden from the "delayed" verdict: Shakti's 2025 inflow, whose execution test was missed, is still "delayed". D4 (a missing quarter masking a contradiction) is still visible at BORORENEW (2 data-unavailable verdicts).

## What this means for the process

- **What works:** the point-in-time machinery (no recorded date changed across 10 issuers' snapshots; one catalyst vanished, D9), the separation of executed-but-unconfirmable from delayed, and the rule that missing data never becomes "established".
- **What dominates the outcome is extraction:**
  - order amounts (D8);
  - segment tables (D10);
  - the results-statement coverage needed for PAT, scope and EBITDA.
- **What to fix first:**
  - D9, first, because it is a correctness bug;
  - then D8 and D10, because they decide whether the main catalysts of named companies are seen at all;
  - then a fresh pre-registered cohort, since these four companies and the earlier seven have now been used for development.
