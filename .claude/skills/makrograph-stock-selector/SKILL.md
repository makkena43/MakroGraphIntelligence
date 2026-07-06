---
name: makrograph-stock-selector
description: >
  Surface a ranked list of Indian or US stocks worth analyzing for a given date, from
  the MakroGraph DB — major + emerging themes (6-12 month window), major constraints
  with explanations, and supply-side beneficiaries. Also answers single-theme queries:
  "who benefits from the transformer constraint", "solar cell companies as of <date>".
  Trigger on: "which stocks should I analyze", "stock selector", "today's
  opportunities", "give me stocks for <date>", "what themes are emerging", "shortlist
  stocks", "US opportunities", or any theme/constraint + beneficiaries question.
  Default country is India; use US when the user says US/America/NASDAQ/NYSE. If no
  date given, use today. Strictly point-in-time: nothing dated after the given date.
---

# Stock Selector (as-of-date opportunity scan)

One command replaces clicking through the UI tabs: for a given date, what themes are
happening/emerging (last 6-12 months), what constraints drive them and why, which
supply-side companies benefit, and which stocks deserve a full report next.

## Process efficiency (read before Step 1)

- **Check the printed `SUMMARY:` line first** (theme/candidate counts, top-5 tickers)
  instead of dumping the full JSON with a Python heredoc — it's usually enough to
  confirm the run worked before you start writing the briefing.
- **Use `jq` for targeted pulls** (e.g., `jq '.ranked_candidates[:15]' file.json`)
  rather than printing entire arrays to inspect them.
- The chat briefing is text you write directly from the JSON fields — no HTML/PDF
  step unless asked, so there's no multi-draft rewrite cost here; just avoid
  re-reading the same JSON section more than once.

## Workflow

### Step 1 — Extract

```bash
.venv/bin/python scripts/stock_report/select_stocks.py --as-of <YYYY-MM-DD> [--country IN|US]
```

Run from project root; prints the JSON path (data/reports/stock_selector_<country>_<date>_data.json).
Default emergence window is 12 months (`--window-months 6` for a tighter scan).
**US mode** (`--country US`): constraints come from bottleneck themes in the theme
graph (no constrained-product mapper / capacity-gap / import tables for US), and
there is NO technical overlay (no US price data in DB) — leave price/technical
columns out entirely for US candidates and point to finviz/stockanalysis for chart
checks. See `us_data_note` in the JSON.

**Theme-focus mode** — when the user names ONE theme or constraint (e.g. "transformer",
"Solar Cell", "PCB", "Artificial Intelligence"), add `--theme "<name>"` (fuzzy match):

```bash
.venv/bin/python scripts/stock_report/select_stocks.py --as-of <date> --theme "transformer" [--country US]
```

The JSON then has mode=theme_focus with `focus`: matched_themes (stage, snapshots,
full beneficiary list with ranks), matched_constrained_products,
matched_supply_side_companies (IN), capacity_gaps and import_dependencies (IN).
Present: what the theme/constraint is + why it exists, key dates (first_detected /
first_mapped), stage now, then the beneficiary table (rank, ticker, type, conviction,
order-book), and suggest full reports for the top 2-3 names. If the name matches
nothing, list a few available theme names (query mg_themes) and ask which one.

### Step 1b — Verify the new improvement fields are present

Check the SUMMARY line for `cross_theme_overlap_leaders` and `high_confidence_themes`.
If the JSON was generated before these improvements, re-run Step 1. The JSON must
contain: `evidence_dashboard`, `cross_theme_overlap`, `bear_cases`, and each candidate
must have `scoring_breakdown`. Use `jq '.evidence_dashboard[:3]'` to confirm.

### Step 2 — Analyze and present (chat answer by default)

Output a crisp chat briefing (only build a PDF if the user asks — same html_to_pdf.py
pipeline as the stock-report skill). Structure:

1. **Major themes — explainer table (MANDATORY columns)**: for the top 5-8 by
   strength_now, a table with exactly these columns:
   | Theme | Plain-English meaning | Started (`first_detected`, DD-Mon-YYYY) |
   | Phase now (`stage_label` + one-phrase read from `stage_evidence`) | Qtrs confirmed |
   The plain-English column translates the cryptic "X: Constraint from Y Demand" label
   into what is actually short and why (use `description` + `stage_evidence`; flag NLP
   label artifacts like "ESG/FDA Demand" and name the real chain). Always include a
   phase legend line: Emerging = first signals; Accelerating = capex committed,
   revenue visible, not crowded (the investable phase); Consensus = broadly discussed,
   momentum decelerating — market already knows.
2. **Emerging in the window** (from `emerging_themes`): same columns as above plus
   companies-mapped-in-window (`new_beneficiaries_in_window`). Say WHEN each emerged
   (`first_detected`) and what phase it has reached NOW — call out any theme that
   raced from birth to Consensus quickly (late to join) vs ones still Accelerating
   (the actionable ones).
3. **Major constraints — with explanation** (from `constrained_products`, `capacity_gaps`,
   `import_dependencies`): for each of the top 5-8 constrained products: what is short,
   why (domestic capacity vs demand, import dependence + origin country, qualification
   barriers), how broad (n_companies mapped), how convinced the pipeline is
   (avg/max conviction), and whether order-book evidence exists (any_order_book).
4. **Beneficiary stocks under EVERY theme/constraint (MANDATORY)** (from
   `supply_side_beneficiaries`): each theme or constraint presented in the briefing
   must carry its own beneficiary stock list (top 5-8 tickers in theme-rank order) —
   never present a theme without its stocks. For IN include beneficiary_type
   (direct/critical/input supplier), conviction, order-book flag. Supply side is the
   focus — capacity owners, not demand-side consumers; flag demand-side names that
   appear inside supply themes (e.g. software names in an energy-constraint theme)
   as noise instead of hiding them. Group related theme-chains (e.g. five utility
   variants) into one row with the union of their top names.
5. **Ranked candidate list** (from `ranked_candidates`): a table of the top 10-15:
   ticker, company, products/themes it spans, conviction, order-book, technical state
   (above 200DMA? % from 52w high). Explain the composite score briefly (see
   scoring_note). Mark the 3-5 highest-priority names and say why.
6. **Evidence dashboard — MANDATORY (Improvement 1)** (`evidence_dashboard`): a table
   for the top 6-8 themes showing companies_mapped, filings_covered,
   bottleneck_signals, confirmed_quarters, policy_events, and evidence_confidence_pct.
   The confidence_pct encodes how much hard evidence exists vs narrative momentum — a
   100% Consensus theme with 0 policy events is market-known but policy-unsupported.
   Flag any theme with confidence < 50 as "low-evidence, treat as watch only."

7. **Cross-theme overlap — MANDATORY (Improvement 2)** (`cross_theme_overlap`): a table
   of companies that appear in 3+ independent constraint chains, sorted by
   n_independent_chains then overlap_score. This is the "conviction multiplier" — a
   stock appearing in 8 independent chains is not noise, it's a structural chokepoint.
   Show ticker, chain count, and list the chains grouped by type (energy / semi / infra).
   Clearly flag demand-side names (PLTR, META) that appear here as cross-chain because
   EVERY theme mentions them, not because they own the constrained capacity.

8. **Transparent scoring — MANDATORY (Improvement 3)** (`scoring_breakdown` on each
   candidate): when presenting the ranked-candidate table include one sentence on the
   formula: IN = "0.45×conviction + 0.25×breadth + 0.20×order_book + 0.10×import_sub,
   ×1.15 technical"; US = "0.40×relevance + 0.30×breadth + 0.30×rank". For the top 3
   candidates show the actual component values from scoring_breakdown — never just the
   final number. Users should never wonder why a stock ranks where it does.

9. **Visual analytics (Improvement 4)** — for PDF/HTML output only:
   - Momentum bar: a CSS inline-style width bar proportional to evidence_confidence_pct
     (e.g., `<div style="width:{pct}%;background:#1a3a5c;height:6px">`) next to each
     theme in the evidence dashboard.
   - Overlap network: a two-column table — ticker | chains (listed as colored pills by
     category: energy=green, semi=blue, infra=amber).
   - Policy timeline: the policy_events count becomes a dated mini-timeline table.
   These do NOT apply to chat-text briefings; render the same information as compact
   text tables there.

10. **Bear-case analysis — MANDATORY (Improvement 5)** (`bear_cases`): after each Bucket A
    and B theme, a "What invalidates this" block with 1-3 specific risks from the
    bear_cases JSON (not generic boilerplate). Always include the two structural risks
    that apply to every energy theme (rate sensitivity, AI efficiency) and the two that
    apply to every chip theme (export-control two-sided, capex-reversal). End with:
    "If 2+ of these risks materialise simultaneously, revisit the bucket assignment."

11. **Next step**: suggest running the full report for the top picks, e.g.
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
