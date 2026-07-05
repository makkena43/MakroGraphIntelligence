---
name: makrograph-stock-selector
description: >
  Surface a ranked list of Indian or US stocks worth analyzing for a given date, from
  the MakroGraph DB — major + emerging themes (6-12 month window), major constraints
  with explanations, and supply-side beneficiaries. Trigger on: "which stocks should I
  analyze", "stock selector", "today's opportunities", "give me stocks for <date>",
  "what themes are emerging", "shortlist stocks", "US opportunities". Default country
  is India; use US when the user says US/America/NASDAQ/NYSE. If no date given, use
  today. Strictly point-in-time: nothing dated after the given date.
---

# Stock Selector (as-of-date opportunity scan)

One command replaces clicking through the UI tabs: for a given date, what themes are
happening/emerging (last 6-12 months), what constraints drive them and why, which
supply-side companies benefit, and which stocks deserve a full report next.

## Workflow

### Step 1 — Extract

```bash
.venv/bin/python scripts/stock_report/select_stocks.py --as-of <YYYY-MM-DD> [--country IN|US]
```

Run from project root; prints the JSON path (data/reports/stock_selector_<country>_<date>_data.json).
Default emergence window is 12 months (`--window-months 6` for a tighter scan).
**US mode** (`--country US`): constraints come from bottleneck themes in the theme
graph (no constrained-product mapper / capacity-gap / import tables for US), and
there is NO technical overlay (no US price data in DB) — say so and point to
finviz/stockanalysis for chart checks. See `us_data_note` in the JSON.
Read the JSON in parts / with jq — it can be large.

### Step 2 — Analyze and present (chat answer by default)

Output a crisp chat briefing (only build a PDF if the user asks — same html_to_pdf.py
pipeline as the stock-report skill). Structure:

1. **Major themes** (from `major_themes`): top 5-8 by strength_now. For each: one line —
   what it is, stage_label, strength_now vs strength_6mo_ago (call out rising vs stale),
   confirmed_quarters. Skip generic mega-themes (e.g. "artificial intelligence" breadth
   noise) or flag them as low-signal.
2. **Emerging in the window** (from `emerging_themes`): themes first detected inside the
   window or with strength_delta_6mo >= +10. Say WHEN each emerged and what changed.
3. **Major constraints — with explanation** (from `constrained_products`, `capacity_gaps`,
   `import_dependencies`): for each of the top 5-8 constrained products: what is short,
   why (domestic capacity vs demand, import dependence + origin country, qualification
   barriers), how broad (n_companies mapped), how convinced the pipeline is
   (avg/max conviction), and whether order-book evidence exists (any_order_book).
4. **Supply-side beneficiaries** (from `supply_side_beneficiaries`): per top constraint,
   the top 3-5 companies with ticker, beneficiary_type (direct/critical/input supplier),
   conviction, order-book flag, one-phrase rationale. Supply side is the focus — these
   are the capacity owners, not demand-side consumers.
5. **Ranked candidate list** (from `ranked_candidates`): a table of the top 10-15:
   ticker, company, products/themes it spans, conviction, order-book, technical state
   (above 200DMA? % from 52w high). Explain the composite score briefly (see
   scoring_note). Mark the 3-5 highest-priority names and say why.
6. **Next step**: suggest running the full report for the top picks, e.g.
   "generate report for KAYNES as of <date>" (the makrograph-stock-report skill).

## Hard rules

- No data dated after the as-of date, ever. The script enforces this; don't add
  DB queries without a `<= as_of` filter.
- Apply judgment on top of scores: broad noisy themes (100-relevance breadth themes),
  single-signal mappings, and thin tickers (technical=null) should be called out, not
  hidden. The score is a screen, not the conclusion.
- If two candidates tie, prefer: order-book evidence > conviction > breadth.
- DB: Postgres `makrograph` only (localhost, user postgres).
- Dates DD-Mon-YYYY in prose; keep the briefing scannable (tables + short lines).
