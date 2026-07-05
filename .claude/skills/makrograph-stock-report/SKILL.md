---
name: makrograph-stock-report
description: >
  Generate a PDF research report for an Indian stock as of a user-given date.
  Trigger whenever the user asks for a "report", "stock report", "research report",
  or "generate report" for a stock/company — e.g. "generate report for TITAGARH as of
  2025/06/01", "give me report of Kaynes for 2024-12-31", "report on HAL". If no date
  is given, use today. The report is strictly point-in-time: NOTHING dated after the
  as-of date may appear in it.
---

# Stock Research Report (as-of-date PDF)

Produce a professional PDF research report for one Indian listed company, using only
information available on or before the user's as-of date (no look-ahead). The user gives
just a stock name/symbol and optionally a date (formats like 2025/06/01 or 2025-06-01).

## Workflow

### Step 1 — Extract data (deterministic)

```bash
.venv/bin/python scripts/stock_report/extract_report_data.py --symbol "<SYMBOL_OR_NAME>" --as-of <YYYY-MM-DD>
```

- Run from the project root. It prints the JSON path (data/reports/<SYMBOL>_<date>_data.json).
- The script already enforces `<= as_of` on every table. Do not query the DB for anything
  dated after the as-of date.
- If the symbol is ambiguous/not found, the script errors — try the company-name fragment,
  or query `security_master` yourself to resolve, then re-run.
- Read the JSON in parts if large (it can be a few hundred KB — read with offset/limit or
  use `jq` per section).

### Step 2 — Fill concall gaps (only when needed)

The `concalls` section has the last ~10 concall-related docs with `text_excerpt`, `url`,
`doc_kind`. You need the **last 4 quarters** of management commentary:

- Prefer docs with `doc_kind: transcript` and meaningful `text_len` (> 2000).
- If fewer than 4 usable transcripts, fetch the `url` of the thin ones (they are NSE/BSE
  attachment PDFs). Download with `curl -sL -A "Mozilla/5.0" -o <scratchpad>/<name>.pdf <url>`
  and extract text with `.venv/bin/python -c "import fitz; print(fitz.open('<file>').load_page(0)...)"`
  or pdfplumber — first ~15 pages is enough per transcript.
- NEVER use a concall dated after the as-of date, even if it's in the JSON by accident.

### Step 3 — Analyze (your intelligence layer)

Write the analytical narratives:

1. **Themes** — for each theme the company benefits from: 2-4 sentence summary, when it
   first appeared (`first_detected`, `first_seen_at`), current stage as of the report date
   (`stage_label`, `stage_history_asof`, `snapshots_asof` trend), and whether momentum is
   building or fading.
2. **Constraints** — DETAILED treatment of `constraint_themes`, `capacity_gaps`,
   `import_dependencies`, and `constrained_product` entries in `india_beneficiary_mappings`.
   For each constraint write three parts:
   - **What & since when**: the bottleneck, first-appeared date, current stage.
   - **Why it is happening**: root-cause the demand side (govt capex, orders, tenders,
     policy) vs the supply side (capacity limits, import dependence, qualification
     barriers, lead times) using `constraint_evidence.evidence_quotes` — quote 2-3
     dated extracts from actual filings and cite the doc (with link).
   - **Authenticity check**: is the constraint corroborated by HARD signals (order
     wins, capex commitments, tender pipelines recurring across multiple quarters —
     see `constraint_evidence.signal_counts` first_seen→last_seen spans) or is it
     narrative-only (few signals, single doc, no follow-through)? Give a verdict:
     Authentic / Partially corroborated / Narrative-only.
3. **Benefit verdict** — one clear paragraph: does this company actually benefit from these
   themes/constraints? Use `beneficiary_type`, `company_role`, `reasoning`, `rationale`,
   `has_order_book_signals`, `import_substitution_play`. Be direct — say "primary
   beneficiary", "secondary/derivative play", or "weak/narrative-only linkage".
4. **Concalls (last 4)** — per call: quarter, date, 3-5 bullet highlights, and a
   **sentiment rating on a 1-5 scale** with label. Rubric (be strict — 5 must mean
   "investable-grade commentary"):
   - **5 — Strongly Positive**: results beat + guidance raised/confirmed AND prior
     guidance visibly delivered. Order book/margins improving. No red flags.
   - **4 — Positive**: good results, guidance intact, minor niggles only.
   - **3 — Mixed**: genuine positives offset by a real miss or a quiet deviation.
   - **2 — Cautious**: misses/deviations outweigh positives; guidance slipping.
   - **1 — Negative**: deteriorating business, guidance abandoned, red flags.
   Show the rating as "X/5" with a colored pill (5,4=green; 3=amber; 2,1=red).
   Then the **authenticity check** across the 4 calls: does management deliver on
   prior-quarter statements or keep deviating (guidance changes, repeated
   postponements, shifting story)? Verdict: Consistent / Mixed / Deviating, with 1-2
   concrete examples, plus an **average concall score** (e.g., "3.5/5").
5. **Price action** — interpret `price_action`: VCP present or not (use the contraction
   depths, dry-up ratio, verdict — but apply judgment, don't just echo the boolean),
   lifetime-high-volume date and what happened around it, breakout status vs pivot.
6. **Bulk/block/insider** — who accumulated or exited (top clients, net qty), notable
   insider buys/sells (promoters buying = strong signal; pledges/sells = caution).
   If the deal windows are empty, state "no bulk/block deals recorded in the window" —
   do not invent.
7. **Peers** — brief comparison table; note fundamentals are current-snapshot (flag it).
8. **Sub-themes** — child themes and constrained products with their company lists.
9. **Quarterly trend** — from `quarterly_financials`: sales/OPM/PAT trajectory, note
   acceleration or deceleration in the last 2-3 quarters.
10. **Shareholding trend** — from `shareholding_trend`: promoter/FII/DII direction over
    the available quarters; FII+DII rising = institutional endorsement, promoter falling
    = flag it (cross-check against insider sales).
11. **Corporate events** — from `corporate_events`: order wins, acquisitions, QIP/rights,
    capacity additions in the window — a short dated timeline of the material ones.
12. **Policy tailwinds** — from `policy_events`: government policies touching the
    company's themes, with dates, direction, and `raw_url` rendered as clickable links.
12b. **Key links & sources** — a dedicated section of clickable public links so the
    reader can verify and read further (Chrome print-to-pdf keeps `<a href>` clickable):
    - **Concall documents — list ALL of them, not just the 4 analyzed**: every
      transcript, recording and presentation in `concalls[]` (url per doc). If more
      exist beyond the JSON's limit, query mg_documents directly for the full set
      (filing_type concall/transcript/presentation, filed_at <= as-of).
    - **Major update documents — a dated link list** from `corporate_events[].url`
      plus a direct mg_documents query for: Bagging/Awarding of orders, Press
      Release, Acquisition, Amalgamation/Merger, MoU/Agreements, QIP/Rights/Buyback,
      results board-outcomes. Label each like "Order LoA disclosure — 12-Feb-2025".
    - Latest investor presentations / annual reports (`key_links.filing_documents`).
    - Policy source links (`policy_events[].raw_url`).
    - Company pages (`key_links.company_pages`): Screener, NSE quote page, NSE
      corporate announcements, NSE insider-trading page, Trendlyne, Tijori, BSE.
      BSE filings are usually deduplicated against NSE at ingestion — link the BSE
      company page (bseindia.com stock page) rather than claiming BSE docs exist.
    - Constraint evidence source docs (`constraint_evidence.evidence_quotes[].doc_url`).
    Render links with a short human label (e.g., "Q1 FY24 transcript (NSE, 27-Jul-23)"),
    never bare URLs longer than one line.
13. **SUMMARY & RECOMMENDATION** (rendered FIRST in the PDF, right after the snapshot
    strip): a verdict box with:
    - **Call: BUY / HOLD / SELL / AVOID** + conviction (High/Medium/Low)
    - A 4-6 bullet case: theme strength, benefit verdict, avg concall score,
      authenticity, price-action state, smart-money/insider signals, shareholding trend
    - **Trigger levels** where relevant (e.g., "becomes a Buy on close above pivot ₹X
      with volume", "invalidated below ₹Y")
    - **What would change the call** — one line each for upgrade and downgrade
    - Decision guide (judgment, not formula): BUY needs the theme accelerating AND real
      beneficiary status AND avg concall ≥ 3.5 with authenticity not "Deviating" AND
      constructive price structure AND no major insider red flag. SELL when theme fading
      or authenticity Deviating plus distribution/insider exits plus broken price
      structure. Otherwise HOLD (own it, don't add) or AVOID (don't own). State clearly
      that a 5/5 concall average with all pillars green is the "investable now" case.
    - End the box with: "Not investment advice — data-driven view as of <date>."

### Step 4 — Render the PDF

1. Write a self-contained HTML file to `data/reports/<SYMBOL>_<date>_report.html`.
   Style guide: A4-friendly, inline CSS only, no external fonts/JS. Clean financial-report
   look: dark header band with company name + symbol + "As of <date>", section headings
   with a thin accent underline (use a deep blue like #1a3a5c and one accent like #c8a02c),
   compact tables with right-aligned numbers, sentiment/verdict badges as colored pills
   (green/amber/red), a footer on the first page: "Point-in-time report — contains no
   information dated after <as-of date>. Generated <today> by MakroGraph Intelligence."
   Keep it ~4-8 pages.
2. Convert: `.venv/bin/python scripts/stock_report/html_to_pdf.py data/reports/<...>.html`
3. Send the PDF to the user with SendUserFile (display: attach) with a 1-2 line caption
   giving the headline verdict.

## Report sections (in order)

1. Header: Company name, NSE symbol, Industry, Sector, as-of date
2. Snapshot strip: last close, 52w high/low, % from high, market cap, PE, ROCE, promoter %
3. **Summary & Recommendation box** (BUY/HOLD/SELL/AVOID + conviction + triggers)
4. Themes (with key dates + stage)
5. Constraints (with key dates + stage)
6. Benefit verdict (does the company benefit — clear call)
7. Last 4 concalls: highlights + 1-5 sentiment ratings + avg score + authenticity verdict
8. Quarterly financial trend + shareholding (FII/DII/promoter) trend
9. Price action: VCP / lifetime high volume / breakout / accumulation days
10. Bulk deals, block deals, insider trading insights
11. Corporate events timeline (order wins, M&A, capital raises)
12. Policy tailwinds (with source links)
13. Peers table
14. Sub-themes & underlying constraints with company lists
15. Key links & sources (clickable: concalls, presentations, policy, company pages)

## Hard rules

- **No forward-looking data.** Filter everything to <= as-of date. Fundamentals/peer
  snapshots are current-only in the DB — include them but visibly label
  "current snapshot, not as-of".
- If a section has no data, say so in one line; never fabricate deals, calls, or dates.
- All DB access goes to the `makrograph` Postgres DB only (localhost, user postgres).
- Dates in the PDF in DD-Mon-YYYY format (e.g., 01-Jun-2025).
- Volumes/values in Indian units (lakh/crore) where natural.
