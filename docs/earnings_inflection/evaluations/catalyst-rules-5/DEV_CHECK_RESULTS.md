# catalyst-rules-5: development check on the ten issuers

Rules **catalyst-rules-5**, fingerprint `b253e4cca34d124d` (follow-ups from this run are included).

These ten issuers exposed the defects that rules-5 fixes, so this is a **check that the fixes work, not an evaluation**. A fresh pre-registered cohort is still needed before rules-5 results mean anything about unseen companies.

The process is the same as the case study:
- quarterly point-in-time replays, from 2021-05-31 to the 2025-12-31 cut-off;
- NSE filings, read-only;
- no prices or returns.

## What changed, issuer by issuer (rules-4 → rules-5)

| Issuer | Change | Defect |
|---|---|---|
| BORORENEW | The debt-reduction catalyst that vanished in rules-4 is kept. The August 2022 price-realisation alert now ends **contradicted** instead of "data unavailable". | D9, D4 |
| SHAKTIPUMP | The ₹358 cr KUSUM order of 2023-08-30 is now a catalyst. Before this fix the amount was in the next sentence and the order was lost. | D8 |
| SMLMAH | **Monthly volume:** two volume catalysts from the monthly sales updates.<br>• **3 months to Dec 2021** (+27%): ends **data unavailable**; the base was depressed by the 2021 lockdown and 2019 data is out of reach.<br>• **3 months to Feb 2023** (+33%): ends **delayed**, because revenue did not grow 15% in the next two quarters.<br>**Scope:** statements are standalone (was unknown), and the parent share is now known. | D11, scope |
| OLECTRA | The spurious "insulator 0.1% → 94.9%" alert is gone. The real insulator mix shift (11.9% → 15.8%, 2025) remains, still open. The 2023–24 earnings rise is now counted as **missed**; the bus orders are linked to their values (₹10,000 cr and ₹4,000 cr) but are related-party demand (EVEY Trans, a promoter-group company), which the rules exclude from external demand. | D10, D8 |
| GMMPFAUDLR | Parsed revenue quarters 8 → 23 and owners'-profit quarters 4 → 17. Two alerts: an overseas segment turnaround (2022, data unavailable) and an India mix shift (confirmed; earnings not delivered). A misread "1,269 pp margin gap" table is now rejected. | D2, D10 |
| GENUSPOWER | Catalysts 21 → 16, after misread and restated order books were removed. Still no alerts: only 7 quarters of results are parsed. | D5, D6 |
| SWSOLAR, TARIL, HBLENGINE | Essentially unchanged. | — |
| WAAREEENER | Unchanged: 7 quarters, all after listing; EBITDA and owners' profit are not parsed. | — |

## Totals across the ten issuers

| | Rules-4 | Rules-5 |
|---|---|---|
| Catalyst dates changed in later snapshots | 0 | **0** |
| Catalysts vanished | 1 (BORORENEW) | **0** |
| Alerts (supported or better) | 28 | 33 |
| Confirmed | 10 (one was the spurious Olectra alert) | 9 |
| Executed but not confirmable | 10 | 11 |
| Delayed / contradicted | 0 | 2 / 1 (Shakti order, SML volume / Borosil price) |
| Open / data unavailable | 6 / 2 | 7 / 3 |

## Still open (not fixed by rules-5)

- **Shakti Pumps:**
  - The December 2023 and March 2024 results are garbled scans and unreadable without OCR. The order catalyst therefore validated only on 2025-01-24, when those quarters arrived as comparatives.
  - Its final "delayed" comes from monitoring a 2023 order against 2025 revenue (8 monitored periods, an existing rule), which deserves a review.
  - Owners' profit is still not parsed.
- **Waaree:** the period header is damaged ("30-1)9-2023"), so its 2024 statements are not read.
- **TARIL:** some unit lines are printed on a different page from the statement.
- **Volume catalysts:** COVID-era base effects cannot be removed without pre-2020 data.
- **Order inflows without a disclosed backlog** remain unsizable by design, so they end as executed but not confirmable.
- **Earnings delivery and the mechanical inflection test** are still rarely measurable. Owners'-profit series exist for 6 of the 10 issuers.

## Next step

Pre-register a new, company-neutral cohort under rules-5 (frozen) and evaluate it before any further rule changes.
