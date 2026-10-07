# Pre-registered held-out cohort: numbers-first detector

Registered **2026-10-07**, before the detector existed, before the draw and before any filing for these issuers was fetched.

## What is evaluated

1. **Primary: the numbers-first early-signal detector**, rules `numeric-rules-1`. It runs on the point-in-time results series (XBRL on, PDF tables as fallback).
   - Its rules are developed and checked only on the 22 development issuers.
   - They are then frozen and recorded with a fingerprint in `FREEZE.md`, committed **before** any filing of the cohort below is fetched.
2. **Comparison: catalyst-rules-6**, the text-led catalyst detector, run on the same data with XBRL on.

Defects found during the cohort run are recorded and fixed only under a later rules version, never in this run's results.

## Selection rule (names never enter it)

- **Universe:** the union of the current Nifty Smallcap 250 and Nifty Microcap 250 constituents. These are the lists archived with the rules-5 draw (`../catalyst-rules-5/cohort/`, downloaded 2026-10-07).
  - Survivorship bias is the same as before: only today's constituents appear.
- **Exclusions:**
  1. index industry "Financial Services";
  2. every development issuer:
     - INDOTECH, SUPRIYA, GOLDIAM, SHAILY, DEEPAKFERT, ASALCBR, PGIL, REFEX;
     - GENUSPOWER, HBLENGINE, TARIL, OLECTRA, BORORENEW, GMMPFAUDLR, SWSOLAR;
     - WAAREEENER, SHAKTIPUMP, SMLMAH, HBLPOWER, SMLISUZU;
     - the rules-5 cohort: PRICOLLTD, RAIN, TATACHEM, BATAINDIA, CESC, AURIONPRO, HGINFRA, GPPL, AFFLE, RBA, LUMAXIND, JYOTHYLAB;
  3. no NSE announcement dated 2021-01-01 to 2021-06-30 (short history).
- **Draw:**
  - rank eligible issuers by `sha256("ei-numeric1-cohort-2026-10-07|" + SYMBOL)`, ascending;
  - take each in turn, skipping an issuer whose index industry already has **3** in the cohort;
  - stop at **30**.
- **Commit:** the ranked list, every skip and the final 30 are committed before any filing is fetched.

## Data and replay (same as earlier cohorts)

- **Data:** read-only NSE fetches into a local fixture:
  - public announcements and annual reports, 2021-01-01 to 2025-12-31 (pdftotext);
  - results XBRL (`corporates-financial-results`, 2019–2024 quarters);
  - integrated filings (2025 quarters).

  No OCR, no LLM, no database writes.
- **Replay:** quarterly point-in-time as-of dates, Feb/May/Aug/Nov month-ends from 2021-05-31 to 2025-11-30, plus 2025-12-31. Each run sees only what was public by its date. Figures are dated at their own dissemination time.

## Metrics (fixed now)

Profit series: owners' share of profit where the chosen scope is consolidated and it is available, otherwise profit after tax.

1. **Earnings inflection (the outcome):**
   - definition: TTM profit rises at least 50% over 4 quarters from a positive base, the same definition as earlier cohorts;
   - episodes: overlapping windows are one episode;
   - dating: an inflection is dated at the publication of the quarter that completes it.
2. **Caught:** at least one numeric signal fired between the window's first quarter and the publication of its completing quarter.
3. **Lead time:** days from that first signal to the publication of the completing quarter, reported as median and distribution, with signals fired on the completing quarter itself counted separately.
4. **Precision:** the share of signal episodes followed, within the next 4 published quarters, by TTM profit at least 25% above its level when the signal fired. A signal with no 4 following quarters by the cut-off is "open", counted separately.
5. **Lift:** precision against the base rate, i.e. the share of all issuer-quarters (with 4 following quarters) followed by the same +25%.
6. **Workload:** signals per issuer-year.
7. **Date stability:** no signal's date may change, and no signal may vanish, in a later snapshot (prefix invariance).
8. **Coverage:** quarters of revenue, EBITDA and profit per issuer, and their sources (XBRL / PDF).
9. **Catalyst-rules-6 comparison:** alerts, verdicts, false alerts and inflection credit under the rules-6 rule.

**Not done:** no prices, no returns, no investment labels. Signals are research leads, not recommendations.

## Draw result (committed before any filing was fetched)

Universe 505 constituents; 421 eligible after the financial-services and development-issuer exclusions. Script `cohort/draw_cohort.py`; full log in `cohort/numeric1_cohort_draw.json`.

| # | NSE symbol | Company | Index industry |
|---|---|---|---|
| 1 | CENTUM | Centum Electronics Ltd. | Capital Goods |
| 2 | POLYMED | Poly Medicure Ltd. | Healthcare |
| 3 | PGHL | Procter & Gamble Health Ltd. | Healthcare |
| 4 | QUESS | Quess Corp Ltd. | Services |
| 5 | EMBDL | Embassy Developments Ltd. | Realty |
| 6 | WEBELSOLAR | Websol Energy System Ltd. | Capital Goods |
| 7 | ENGINERSIN | Engineers India Ltd. | Construction |
| 8 | MMTC | MMTC Ltd. | Services |
| 9 | CIEINDIA | CIE Automotive India Ltd. | Automobile and Auto Components |
| 10 | RELAXO | Relaxo Footwears Ltd. | Consumer Durables |
| 11 | GRAPHITE | Graphite India Ltd. | Capital Goods |
| 12 | LTFOODS | LT Foods Ltd. | Fast Moving Consumer Goods |
| 13 | RAMCOCEM | The Ramco Cements Ltd. | Construction Materials |
| 14 | ABREL | Aditya Birla Real Estate Ltd. | Realty |
| 15 | MRPL | Mangalore Refinery & Petrochemicals Ltd. | Oil Gas & Consumable Fuels |
| 16 | DBREALTY | VALOR ESTATE Ltd. | Realty |
| 17 | DEEPAKNTR | Deepak Nitrite Ltd. | Chemicals |
| 18 | FIEMIND | Fiem Industries Ltd | Automobile and Auto Components |
| 19 | PCBL | PCBL Chemical Ltd. | Chemicals |
| 20 | GUJALKALI | Gujarat Alkalies & Chemicals Ltd. | Chemicals |
| 21 | CCL | CCL Products (I) Ltd. | Fast Moving Consumer Goods |
| 22 | WELSPUNLIV | Welspun Living Ltd. | Textiles |
| 23 | BALRAMCHIN | Balrampur Chini Mills Ltd. | Fast Moving Consumer Goods |
| 24 | IRB | IRB Infrastructure Developers Ltd. | Construction |
| 25 | ZENSARTECH | Zensar Technolgies Ltd. | Information Technology |
| 26 | INDIAMART | Indiamart Intermesh Ltd. | Consumer Services |
| 27 | JUBLPHARMA | Jubilant Pharmova Ltd. | Healthcare |
| 28 | CERA | Cera Sanitaryware Ltd | Consumer Durables |
| 29 | RPOWER | Reliance Power Ltd. | Power |
| 30 | GABRIEL | Gabriel India Ltd. | Automobile and Auto Components |

Skipped during the walk: no NSE announcement in H1 2021: SENORES, TENNIND, ACUTAAS, DUMMYTRVN, IONEXCHANG, WEWORK, KPIGREEN, CLEANMAX, RATEGAIN, HONASA, TVSSCS, TBOTEK, SKYGOLD, SAGILITY, PWL; industry cap: OSWALPUMPS, POWERICA, GRINDWELL, ELLEN, GREAVESCOT, SUMICHEM, AXISCADES, WOCKPHARMA, RALLIS.

The cohort spans 15 industries.
