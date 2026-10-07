# catalyst-rules-6: development check on the rules-5 cohort issuers

Rules **catalyst-rules-6**, fingerprint `76b4dd78c36b9506`, code at commit `6f720c0`. Every issuer was run on that one commit.

These 12 issuers (the pre-registered rules-5 cohort) exposed defects D12–D15, so this re-run is a **check that the fixes work, not an evaluation**. The rules-5 cohort results in `../catalyst-rules-5/COHORT_RESULTS.md` stay as published.

The process is the same as for the cohort:
- quarterly point-in-time replays from 2021-05-31 to the 2025-12-31 cut-off;
- NSE filings, read-only;
- no prices or returns.

The files in `dev_check/` are:
- `evals/`: per-issuer results;
- `compare_r5_r6.json` and `compare_r5_r6.py`: the comparison;
- `parse_coverage.json`: results coverage for these 12 and the 10 earlier development issuers.

## Totals (12 issuers)

| | Rules-5 (cohort result) | Rules-6 (this check) |
|---|---|---|
| Catalysts | 42 | 25 |
| Alerts (supported or better) | 27 | **6** |
| Contradicted or delayed ("false alerts") | 10 | **5** |
| Executed but not confirmable | 15 | 1 |
| Open | 2 | 0 |
| Catalyst dates changed in later snapshots | 0 | **0** |
| Catalysts vanished | 0 | **0** |
| Mechanical earnings inflections | 4 | 4 |
| Caught, timing only (the rules-5 definition) | 3 | 1 |
| Caught under the rules-6 credit rule (D13) | — | **0** |

## Outcome in plain terms

- **The fixes did what they were built to do.**
  - Price decreases no longer create alerts.
  - Repeated quarterly price statements form one catalyst.
  - Immaterial changes are research notes, not alerts.
  - Misread order books are gone.
  - Results coverage rose sharply where it had collapsed.
  - Historical dates stayed stable.
- **The detector still finds nothing useful on these companies.**
  - 5 of the 6 remaining alerts were later contradicted or delayed.
  - None of the 4 earnings inflections is caught once credit needs a non-immaterial alert whose own verdict did not fail.
  - The rules-5 "catches" were artefacts: Jyothy Labs' was a 1.4 cr segment swing, and Rain's were price statements that ended contradicted.
**Inflections, and why each is missed:**

| Issuer | Window | Owners' profit, TTM | Why missed |
|---|---|---|---|
| AFFLE | 2021-06 → 2024-06 | 39.6 → 88.5 cr | no catalyst type covers organic software growth |
| RAIN | 2021-09 → 2024-03 | 28.0 → 50.6 cr | the only alert in the window, a price rise, ended **contradicted** |
| RAIN | 2024-06 → 2025-06 | 12.1 → 43.2 cr | nothing flagged |
| JYOTHYLAB | 2024-09 → 2025-09 | 375.9 → 643.1 cr | nothing flagged |

## Per issuer

| Issuer | Results coverage, rules-5 → rules-6 (rev / EBITDA / PAT / owners' PAT quarters) | Alerts, rules-5 → rules-6 | What changed |
|---|---|---|---|
| PRICOLLTD | 15/15/15/0 → 15/17/15/0 | 0 → 0 | — |
| RAIN | 24/22/24/0 → unchanged | 16 → 1 | D12: decreases no longer seed; restated rises are one catalyst. Fixed during this check: a rising input cost in the same sentence no longer splits a price run. The remaining alert (2021 price rise) ends **contradicted**. |
| TATACHEM | 5/0/2/0 → **16/14/11/12** | 0 → 0 | D14: stacked header split across chunks; "(` in crore)" unit line |
| BATAINDIA | 6/6/9/0 → **19/11/9/0** | 0 → 0 | D14: lower-case enumerator "a Revenue from operations" |
| CESC | 10/0/13/0 → **15/8/19/0** | 0 → 0 | D14: quarter-end dates without separators; scope from the auditor's review report |
| AURIONPRO | 20/19/13/18 → 21/20/12/18 | 3 → 1 | D15: the "200 cr" book (an amount retired from the book) and the superseded "800 cr" reading are not book readings. The 760 → 900 cr book is not a 30% rise. One PAT quarter fewer (not traced). |
| HGINFRA | 21/20/20/3 → 22/21/23/3 | 1 → 1 | the 2023 order inflow still ends contradicted |
| GPPL | unchanged | 0 → 0 | — |
| AFFLE | unchanged | 0 → 0 | inflection still missed |
| RBA | 17/17/16/9 → 17/17/16/9 | 1 → 1 | Two quarters (Sep-24, Sep-25) were replaced by two others (Dec-24, Mar-25); see below |
| LUMAXIND | 19/19/16/0 → 19/19/18/0 | 5 → 2 | D13: three order-book catalysts were already rated **immaterial** under rules-5 and are now research notes, not alerts |
| JYOTHYLAB | unchanged | 1 → 0 | D13: the 1.4 cr segment turnaround is immaterial and no longer an alert |

**RBA, Sep-24 and Sep-25 quarters:**
- Both came only from the Oct-25 results filing. It has no unit line, and part of it is misaligned: the Sep-24 half-year loss (−1,176.43 mn) is printed in the quarter column, while the Oct-24 filing gives −654.54 mn.
- Rules-6 now reads the Oct-24 filing, so only 79% of the Oct-25 filing's comparatives match earlier filings, below the 80% needed to infer its unit. The filing is left unused.
- This is the evidence standard working as intended, not a parser regression.

## Changes made during this check (part of rules-6, before this run)

- **Price runs and input-cost runs are separate** (Rain): "realisation increased by ~61.5% driven by increased raw material prices" no longer ends the price run through its rising-cost reading.
- **Fail closed on misaligned tables:** a table whose revenue + other income ≠ total income is not used at all. Previously only revenue and other income were dropped; RBA's Oct-24 table showed profit rows that were shifted too.

## The ten earlier development issuers (coverage only)

Results coverage was unchanged or improved for all ten, and no revenue quarter was lost:
- SMLMAH: revenue 18 → 22 quarters, EBITDA 2 → 7;
- TARIL: revenue 10 → 13;
- GMMPFAUDLR and OLECTRA: one more quarter each of PAT or EBITDA;
- all others identical.

Detection was not re-run on these ten.

## What this means

Fixing defects one cohort at a time has made the detector quieter and its dates stable, but not more useful: on these 12 issuers it now flags almost nothing, and what it flags is mostly wrong. Every cohort so far has found a new class of defect, and each fix was judged on the few companies that exposed it.

The next step is not rules-7 patches but the change of approach recorded in `../../IMPLEMENTATION_REPORT.md` §15:
- structured results data (XBRL) with accounting-identity checks;
- layout-aware table reading as the fallback;
- a labelled random sample to judge text extraction;
- a broad random-issuer sample to accept or reject any change;
- then one held-out, pre-registered cohort.
