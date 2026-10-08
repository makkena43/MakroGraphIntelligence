# Case test: Trejhara Solutions (TREJHARA) and Patil Automation (PATILAUTOM, NSE SME)

Run on 2026-10-08 on code `edbda90` (catalyst-rules-6, numeric-rules-1, XBRL on). Read-only fetch of
public NSE announcements, annual reports and results XBRL into the session scratchpad; no OCR, no LLM.
Both names were chosen by the user; this is a case test, not a cohort, and nothing here was used to
change rules. Snapshots: `TREJHARA_2026-10-08.json`, `PATILAUTOM_2026-10-08.json`; tools in `tools/`.

| | TREJHARA | PATILAUTOM |
|---|---|---|
| Board | main board | NSE Emerge (SME), listed June 2025 |
| Filings read | 97 announcements, 6 annual reports, 89 results XBRL | 69 announcements, 2 annual reports, 8 results XBRL |
| Results frequency | quarterly | half-yearly (SME) |

## Trejhara Solutions: the growth is mergers and acquisitions, not an organic inflection

What the filings say, in date order:

- 2023: the business was sold to Aurionpro for up to Rs 140 cr. Standalone revenue fell from about
  Rs 13-15 cr a quarter to about Rs 3 cr.
- Oct 2024 to Oct 2025: LP Logistics Plus Chemical SCM was merged in. The NCLT sanctioned the merger on
  14 Oct 2025; it took effect on 16 Oct 2025, with an appointed date of 1 Apr 2024. On 5 Nov 2025,
  8.99 mn new shares were issued, taking the total to 23.5 mn.
- Dec 2025 to Mar 2026: Trejhara acquired LP Logistics Plus LLC (Dubai) and set up an exhibitions
  joint venture with GS Marketing Associates. Both were completed on 23 Mar 2026. A preferential issue
  of warrants and shares was also approved.

Numbers (XBRL, Rs cr, dated at publication):

| Quarter | Standalone revenue | Consolidated revenue | Consolidated PBT | of which other income | Consolidated PAT |
|---|---|---|---|---|---|
| Sep-24 | 3.41 | | | | |
| Jun-25 | 5.83 | 8.65 | 0.98 | 0.16 | 0.67 |
| Sep-25 | 30.11 | 33.93 | 4.85 | 3.84 | 3.57 |
| Dec-25 | 28.67 | 33.81 | 1.52 | 1.13 | 1.21 |
| Mar-26 | 27.94 | 41.87 | 3.19 | 3.16 | 3.22 |
| Jun-26 | 26.85 | 67.64 | 5.41 | 0.81 | 4.79 |

What the detector did:

- **numeric-rules-1** raised `revenue_acceleration` for four quarters in a row: Sep-25 +435%, Dec-25 +426%,
  Mar-26 +338%, Jun-26 +682%. The arithmetic is right, but the meaning is wrong.
  - The year-ago quarters are the pre-merger figures as originally filed.
  - Standalone revenue has been flat to falling since the merger (30.1, 28.7, 27.9, 26.9).
  - `profit_step_up` correctly did not fire: other income was 74-99% of PBT in Sep-25 to Mar-26.
- **catalyst-rules-6** set status `EXECUTION_CONFIRMED`, with the rationale "6 consecutive quarters of
  material revenue growth". That is wrong for this company (see D16).
  - Its only catalyst was a weak `customer_approval` item (16 Jun 2026).
  - The merger and acquisitions, the actual cause, are not modelled as an earnings driver at all.

Honest reading: Jun-26 is the first quarter with real operating profit (about Rs 4.6 cr PBT excluding
other income). It comes from the Dubai acquisition, on a share count about 62% larger, plus pending
warrants. Whether there is an inflection per share, and whether it is organic, is not shown yet.

## Patil Automation: guidance runs well ahead of the order book

What the filings say:

- FY25: revenue Rs 118.1 cr (FY24 115.3); PAT Rs 11.7 cr (FY24 7.6). The plant was at about 87% utilisation.
- Capacity: the new Pune facility was inaugurated in Aug 2025, taking capacity from 2,304 to 3,454
  units (+50%). Faridabad started in Apr 2026.
- Acquisitions:
  - Pentaco Automation (up to Rs 3 cr) and 60% of MII Robotics, agreed Aug 2025 and completed Sep 2025;
  - MII Robotics stake raised to 70% in Aug 2026.
- Order book and pipeline (management):

  | Date | Order book | Proposals pipeline |
  |---|---|---|
  | Oct-Nov 2025 | "Rs 140 cr plus" | > Rs 600 cr |
  | May 2026 | Rs 118 cr (Rs 100 cr+ standalone, plus Rs 14-18 cr in subsidiaries) | > Rs 800 cr |

- Orders announced, Oct 2025 to Jun 2026, including taxes: 10.82, 30.13 (may overlap the 10.82),
  6.60, 8.25, 12.67 and 9.03, for a total of about Rs 67-78 cr.
- Guidance (calls):
  - FY26: Rs 150-170 cr. Actual: consolidated revenue Rs 166.6 cr, standalone Rs 150.0 cr; standalone PAT Rs 15.85 cr.
  - FY27: Rs 260-270 cr; margin "a little more" than about 13% EBITDA.
  - Target: Rs 700 cr+ by 2030.
- Sep 2026: a preferential raise of up to Rs 96.4 cr (warrants plus shares at Rs 246) was approved,
  with the EGM on 3 Oct 2026.

Numbers (XBRL, consolidated, Rs cr): H1 FY26 revenue 71.3, PBT 10.4; H2 FY26 revenue 95.3, PBT 13.8.

What the detector did:

- **numeric-rules-1** raised only `finance_cost_relief` "for the quarter ended 31 Mar 2025", known on
  1 Sep 2025. The figures are FY25 vs FY24 annual finance costs (144.45 vs 238.34 lakh) from the annual
  report, mislabelled as a quarter (D17). It never judged a half year: the detector is quarterly-only,
  so it is blind to SME half-yearly filers (D18).
- **catalyst-rules-6** found the capacity expansion (+50%, `potential_catalyst`, waiting on
  commissioning) and three early-lane orders. Correctly, all three are marked provisional (L1/LoI) or
  with the customer unnamed. Status: `EARLY_COMMITMENT_UNVERIFIED`. The commissioning was stated (Aug 2025
  inauguration; May 2026 call: about Rs 50 cr of FY26 revenue from the new facility) but not linked to
  the capacity catalyst.

Through the lens of the orders-vs-results study (`../orders-vs-results/`):

- Orders announced in 12 months come to about 0.4-0.5x TTM revenue. Order book / TTM revenue fell
  from about 1.2x (Oct 2025, on FY25 revenue) to about 0.7x (May 2026, on FY26 revenue). Both sit in
  the weak buckets, not the ≥1x orders or ≥3x book buckets that preceded large profit rises.
- FY27 guidance of Rs 260-270 cr is +56-62% on Rs 166.6 cr. The Rs 118 cr book covers about 45% of it;
  the rest depends on converting the proposal pipeline, which is not orders.
- So the document evidence supports "expected" only on management's word. It does not yet support it
  on orders. The test is the H1 FY27 result (due by mid-Nov 2026). Being on track needs roughly
  Rs 110-120 cr of H1 revenue against 71.3, with margins holding, on a share count that grows with the
  preferential issue.

## Defects found (not fixed; no rule changes made)

- **D16 (Trejhara): inorganic step-changes are treated as organic.** An amalgamation or acquisition with
  a stated effective or appointed date should do three things:
  - split the year-on-year comparison (or use restated comparatives);
  - block `revenue_acceleration` and "consecutive growth" execution credit;
  - report growth per share.
- **D17 (Patil): annual figures labelled as a quarter.** The annual-report statement of profit and loss
  (FY columns) produced Q rows for the 31-Mar period end.
- **D18 (Patil): no half-yearly path.** SME filers report H1/H2 only. The numeric detector and the
  timeline tools need H-on-H year-ago comparisons, or they stay silent for every SME company.
- **D19 (Patil): commissioning is not linked to the capacity catalyst.** The "new facility inaugurated"
  and "revenue from new facility" statements were not matched to the capacity catalyst's
  commissioning milestone.
