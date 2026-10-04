# Manual earnings-inflection study: three follow-up tests

Research only. No software, model, scoring pipeline, or database changes.

## Protocol frozen before new-cohort forward returns

Run date: 2026-09-11. Historical price evaluation endpoint: 2026-07-03 (latest common local archive date). This is not a live-price study.

### A. Company-neutral archive cohort

- For each year 2020–2025 use 31 August as the research cutoff.
- Eligible documents are existing India-tagged records with a nonempty ticker, filed from May through August of that year, at least 10,000 characters of extracted text, title matching `transcript|call`, and text matching `earnings conference call|earnings call|results conference call` (case insensitive).
- For each ticker/year retain the most recent eligible filing, breaking same-day ties by descending document ID.
- Exclude the six original cases and previously discussed companies: LAURUSLABS, DEEPAKNTR, GOKEX, GET&D, GVT&D, PGEL, AMBER, DIXON, SIEMENS, PATILAUTOM, TEMBO, POWERINDIA, SHAKTIPUMP, TARIL, WEBELSOLAR, WAAREEINDO, WAAREEENER, ADANIENT, TATAPOWER, EXIDEIND, SALASAR, GENSOL.
- Sort remaining ticker/year pairs by ascending MD5 of ticker + year + the fixed string `earnings-pilot-v1`; take the first two each year. No selection by subsequent earnings, share prices, market capitalization today, or returns.
- Do not replace financial companies, incomplete documents, difficult cases, failures, or unavailable price histories after selection. Record inapplicability and missing data explicitly.
- Assess dated business evidence before querying the new cohort's forward prices. Categories: ADVANCE (specific material operating inflection with evidence of execution), WATCH (plausible but timing, cyclicality, financing, or materiality unresolved), NOT SUPPORTED (no supported material positive inflection), OUTSIDE MODEL (financial-sector economics require a different underwriting model), and INSUFFICIENT SOURCE.
- These are research-priority labels, not Buy recommendations. This pilot evaluates discovery/evidence discrimination, not a fully specified investment strategy.
- Returns: next trading-session close after the cutoff; 12/24/36-month anniversary close on or before the anniversary if the full period exists; also to the fixed archive endpoint. Track matched-date benchmarks and adverse outcomes. Verify security identity and corporate actions; exclude unverified calculations rather than assume raw prices are adjusted.
- Selection is neutral within this incomplete archive, not representative of every stock then listed. In particular, available qualifying issuer counts vary markedly by year. An LLM may remember historical outcomes even without querying prices; this is not a genuinely blind prospective test.

| Year | Eligible issuers | Selected issuer | Document ID | Filed date | Selection hash |
|---|---:|---|---:|---|---|
| 2020 | 92 | UBL | 338040 | 2020-06-25 | 008ac15be49f56bb95b9b6621d290d1a |
| 2020 | 92 | CEATLTD | 71741 | 2020-05-28 | 0233be52613589cf51eb438e80ad0273 |
| 2021 | 18 | MOLDTKPAC | 367462 | 2021-05-27 | 0ce688f57ba58a9a72a6e8da1a867a37 |
| 2021 | 18 | ASAHISONG | 372868 | 2021-06-11 | 1c4720f532f2809112168b3af7c83d0e |
| 2022 | 183 | BALAMINES | 142637 | 2022-05-18 | 00042dff4dcb085d22dfcc0465a8d057 |
| 2022 | 183 | GIPCL | 146332 | 2022-06-13 | 004d2a56e33a111c657c173c78f34299 |
| 2023 | 294 | FINOPB | 456624 | 2023-08-03 | 02207ab526e0f2d7837fbd89bdfaf96e |
| 2023 | 294 | BHARTIARTL | 454777 | 2023-08-09 | 0235edb92ae2ffd3eda7bebedf6a9876 |
| 2024 | 222 | INDOSTAR | 510294 | 2024-08-07 | 00a710d50bb16b311c9a747250fbca19 |
| 2024 | 222 | HTMEDIA | 246134 | 2024-08-01 | 02be16bcb3ca12a99203004a41c16ae6 |
| 2025 | 273 | COCHINSHIP | 291501 | 2025-08-26 | 0054dfa249a771cb654063dc3f430261 |
| 2025 | 273 | 360ONE | 560610 | 2025-07-23 | 007957611055a1bfe9996fffc2562cd6 |

### B. Earlier detection in the original six

Preserve the original six cases and their previously reported returns unchanged. Trace backward using dated disclosures; distinguish first suggestion, first operational confirmation, and first defensible research flag. Do not call a located signal the earliest ever unless earlier coverage is demonstrably complete. Any earlier-entry calculation is a separate sensitivity study, not a replacement for the original result.

### C. Original-cutoff valuations

Use only evidence dated on or before the original research cutoffs: Laurus 2020-08-31; Deepak Nitrite 2021-08-31; Gokaldas 2022-05-31; GE T&D 2023-02-28; PG Electroplast 2024-05-31; Amber 2025-05-31. Analyst downside/base/upside assumptions are not management guidance. Model revenue, operating margins, depreciation, financing costs, tax, minority interests, and diluted shares where supported. Present a roughly two-year normalized earnings-power scenario, not falsely precise quarterly forecasts. Disclose which inputs remain judgmental or unverifiable. Never use realized future earnings to set scenario assumptions.

## Assessments and results

Pending. The new-cohort forward return query has not been run at protocol creation.
