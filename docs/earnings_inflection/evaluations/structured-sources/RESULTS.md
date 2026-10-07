# Structured results data (XBRL) and layout-aware PDF reading

This work replaces per-company parser patches with sources and checks that generalise:
- **A.** results figures from the exchange's XBRL filings;
- **B.** pdfplumber word-position (layout) text for PDFs.

It covers the 22 development issuers: the 12 of the rules-5 cohort and the 10 used earlier. It is a development measurement, not a detector evaluation. All data was fetched read-only from NSE public endpoints. No LLM, no OCR.

## A. XBRL results filings

**Sources**
- `api/corporates-financial-results` (`in-bse-fin` taxonomy) covers quarters to December 2024.
- `api/integrated-filing-results` (SEBI integrated filings, `in-capmkt` taxonomy) covers the March 2025 quarter onwards.
- Every quarter has one file per scope (standalone and consolidated), dated at the exchange's dissemination time.

**What the parser does** (`src/makrograph/earnings_inflection/xbrl_results.py`):
- **Periods:** taken from each context group's declared reporting dates. NSE files leave the main contexts undefined (Rain Q4 CY21), or give year-to-date contexts the quarter's dates (all files checked). Reading context dates directly would mislabel every year-to-date figure.
- **Arithmetic checks:** each statement's own arithmetic is checked:
  - revenue + other income = total income;
  - the expense lines sum to total expenses;
  - total income − expenses = profit before exceptional items;
  - PBT − tax = continuing profit;
  - continuing + discontinued (+ associates) = profit for the period;
  - owners' share + minority share = profit for the period.

  A figure in a failed check is **unresolved**, never repaired.
- **XBRL is not ground truth by itself.** Rain's Q4 CY21 consolidated XBRL reports owners' profit as **+969.59 mn**, where the PDF statement prints **(969.59)**. The arithmetic check catches this.
- **Cross-checking PDF figures:** validated XBRL figures check PDF-read figures that became public at or after the XBRL. PDF figures that disagree are rejected and counted.
- **Opt-in:** off by default (`xbrl_results: true` turns it on), so existing runs are unchanged.

**Coverage, 22 issuers** (quarters in the chosen series, by the 2025-12-31 cut-off; `results/coverage_pdf_vs_xbrl.json`):

| | PDF only (rules-6) | With XBRL |
|---|---|---|
| Revenue | 380 | **532** |
| EBITDA | 316 | **529** |
| Profit after tax | 303 | **531** |
| Owners' profit | 121 | **267** |
| Issuers whose series reaches Sep-2025 | — | 21 of 22 (HBLENGINE: Jun-2025) |
| Revenue quarters lost | — | **0** |

Rain, Bata and Affle now use their consolidated series. It had been unavailable for 2025 from the PDFs.

**Gaps**
- Owners' profit is still missing for some consolidated issuers (Pricol, Lumax, Shakti Pumps, HBL, GPPL). The figure is either absent from their XBRL or fails the attribution check, and is left out rather than guessed.
- Waaree Energies has 7 quarters, all since listing.

## B. PDF tables: pdftotext versus pdfplumber layout, scored against XBRL

`tools/pdf_vs_xbrl.py` scores each results PDF in two ways: as `pdftotext -layout` text and as pdfplumber word-layout text (`src/makrograph/parser/pdf_layout.py`).
- **Answer key:** the validated XBRL figures disseminated within 3 days of the PDF.
- **Metrics scored:** revenue, other income, total expenses, depreciation, finance cost, PBT, tax, PAT and owners' profit.
- **Scale:** 568 PDF filings.
- **Code:** commit `3e9fe66` (`results/pdf_vs_xbrl/`).

| | pdftotext | pdfplumber layout |
|---|---|---|
| Read correctly (incl. unit inferred later) | 5,362 (34.2%) | 5,542 (35.4%) |
| Read but **wrong** | 580 (9.8% of figures read) | 587 (9.6% of figures read) |
| Not found | 9,723 | 9,536 |

**What this shows**
- **PDF table reading is a weak source either way.** It finds only about a third of the figures, and about one in ten figures it does read is wrong. The series-level voting catches some of these, but not all.
  - Part of "not found" is expected: a PDF filed alongside an XBRL may hold only one scope.
  - The relative numbers are what matter.
- **pdfplumber layout is not better overall.**
  - **It helps on scanned and tilted PDFs:** Pricol (+135 correct), Borosil Renewables (+91), GMM Pfaudler (+75), Lumax (+57), CESC (+22).
    - Two general fixes made this possible: straightening tilted scans, and re-joining characters stored one by one.
    - pdfplumber keeps "3213" whole, where pdftotext gives "32 13".
  - **It hurts on others:** Jyothy Labs (−72), Waaree Solar (SWSOLAR) (−80), Aurionpro (−43) and RBA (−36). These are scans with a poor text layer that the two tools misread differently.
- **Decision**
  - XBRL is the primary results source.
  - PDF tables are a fallback, used only where XBRL is missing.
  - pdfplumber layout stays opt-in (`PDFParser(layout="words")`).
  - A better fallback would keep only the figures both extractions agree on. This is not built yet.
- **Two general parser rules came out of this work:**
  - standalone and consolidated column groups whose labels sit on separate lines are ordered by the labels' columns;
  - "revenue from operations" printed in two lines, (a) revenue and (b) other operating revenue, is summed only when the sum makes the statement add up. Under Ind AS revenue includes other operating revenue, and XBRL agrees.

  Both are scored here across all 22 issuers, not only on the company that exposed them.

## Tools

The `tools/` scripts were run from the repository root with `PYTHONPATH=src`:
- `fetch_xbrl.py` (read-only NSE fetch into a fixture);
- `pdf_vs_xbrl.py`;
- `parse_cov.py` and `parse_cov_x.py` (coverage without and with XBRL).
