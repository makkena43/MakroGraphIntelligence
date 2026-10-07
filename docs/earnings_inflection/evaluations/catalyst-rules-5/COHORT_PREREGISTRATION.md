# Pre-registered company-neutral cohort — catalyst-rules-5

Registered **2026-10-07**, before the index lists were downloaded and before any issuer was drawn or any filing fetched.

**Rules:** frozen at **catalyst-rules-5**, fingerprint `b253e4cca34d124d`. Defects found during the run are recorded; they are fixed only under a later rules version, never in this run's results.

**Selection rule, fixed now:** names never enter the rule, so neither I nor the user picks companies.

## Universe

- **Source:** the union of the **current** constituents of the Nifty Smallcap 250 and the Nifty Microcap 250, from `nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv` and `ind_niftymicrocap250_list.csv`, downloaded on the day of the draw. The files are archived with the results.
- **Survivorship bias:** this is a list of *today's* constituents, so companies that failed and left the indices before 2026 cannot appear. A point-in-time constituent history is not available from these files. Results therefore over-represent survivors, and false-alert rates may be understated.

## Exclusions, applied before the draw

1. **Financial services:** issuers whose index `Industry` is "Financial Services". These are banks, NBFCs and insurers, for which the detector's catalysts (orders, capacity, volumes) do not apply.
2. **Development issuers:** every issuer used in development so far:
   - INDOTECH;
   - the other seven exported issuers: SUPRIYA, GOLDIAM, SHAILY, DEEPAKFERT, ASALCBR, PGIL, REFEX;
   - the rules-3 cohort: GENUSPOWER, HBLENGINE, TARIL, OLECTRA, BORORENEW, GMMPFAUDLR, SWSOLAR;
   - the rules-4 case study: WAAREEENER, SHAKTIPUMP, SMLMAH;
   - also HBLPOWER, SMLISUZU and SHAKTIPUMP under any old symbol.
3. **Short history:** issuers with no NSE corporate announcement dated 2021-01-01 to 2021-06-30, which means they were not listed or not filing early enough for the replay.

## Draw

1. Rank all eligible issuers by `sha256("ei-rules5-cohort-2026-10-07|" + SYMBOL)`, ascending.
2. Walk down the ranking and take each issuer unless its index `Industry` already has two issuers in the cohort.
3. Apply exclusion 3 as the walk reaches each issuer; an excluded issuer is skipped and the walk continues.
4. Stop at **12 issuers**.

The ranked list, every skip and its reason, and the final 12 are committed **before** any filing is fetched.

## Process (identical to the earlier cohorts)

- **Data:** NSE public announcements and annual reports, 2021-01-01 to 2025-12-31, fetched read-only into a local scratch fixture, with `pdftotext`. No OCR. No database writes.
- **Replay:** quarterly point-in-time as-of dates at the month-ends of February, May, August and November, from 2021-05-31 to 2025-11-30, plus a 2025-12-31 cut-off.
- **Metrics:** the same as `../catalyst-rules-3/COHORT_PREREGISTRATION.md`:
  - coverage;
  - alerts (supported or better) and their verdicts at the cut-off, including `executed_unconfirmable`;
  - false alerts (contradicted or delayed);
  - earnings delivery;
  - mechanical inflections (owners'-profit TTM +50% over 4 periods, from a positive base) and misses;
  - date stability (prefix invariance);
  - review workload.
- **Not done:** no prices, no returns, and no investment labels.

## Draw result (committed before any filing was fetched)

The universe has 505 constituents. 433 are eligible: 72 were excluded as financial services or development issuers.

The script is `cohort/draw_cohort.py`; the downloaded index lists and the full log are in `cohort/r5cohort_draw.json`.

| # | NSE symbol | Company | Index industry |
|---|---|---|---|
| 1 | PRICOLLTD | Pricol Ltd. | Automobile and Auto Components |
| 2 | RAIN | Rain Industries Ltd | Chemicals |
| 3 | TATACHEM | Tata Chemicals Ltd. | Chemicals |
| 4 | BATAINDIA | Bata India Ltd. | Consumer Durables |
| 5 | CESC | CESC Ltd. | Power |
| 6 | AURIONPRO | Aurionpro Solution Ltd. | Information Technology |
| 7 | HGINFRA | H.G. Infra Engineering Ltd. | Construction |
| 8 | GPPL | Gujarat Pipavav Port Ltd. | Services |
| 9 | AFFLE | Affle 3i Ltd. | Information Technology |
| 10 | RBA | Restaurant Brands Asia Ltd. | Consumer Services |
| 11 | LUMAXIND | Lumax Industries Ltd. | Automobile and Auto Components |
| 12 | JYOTHYLAB | Jyothy Labs Ltd. | Fast Moving Consumer Goods |

**Skipped during the walk:**
- VISL, KIRLPNU, CMSINFO, CMPDI and SHADOWFAX had no NSE announcements in H1 2021. A re-check found none in 2021 and none in H1 2022 either, except CMSINFO, which was listed in December 2021.
- MAPMYINDIA was skipped by the industry cap (Information Technology).

The sample spans 10 industries. It is not targeted at industrial order or capacity stories, which is intended: the detector should be quiet where nothing changes.
