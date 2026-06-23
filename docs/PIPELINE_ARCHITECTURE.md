# MakroGraph Intelligence — Pipeline Architecture

> **Purpose:** This document is the authoritative reference for how both the US and India intelligence pipelines work. Future Claude sessions and developers should start here before making enhancements.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Storage Layer (PostgreSQL Schema)](#2-storage-layer)
3. [Shared Components](#3-shared-components)
4. [US Intelligence Pipeline](#4-us-intelligence-pipeline)
5. [India Intelligence Pipeline](#5-india-intelligence-pipeline)
   - 5a. [India Company Filings (NSE/BSE/Screener)](#5a-india-company-filings)
   - 5b. [India Policy NLP (Tier 1–4 Sources)](#5b-india-policy-nlp)
   - 5c. [India Intelligence Layers 1–10](#5c-india-intelligence-layers-1-10)
6. [Macro & Policy Data Layer](#6-macro--policy-data-layer)
7. [Theme Detection & Ranking](#7-theme-detection--ranking)
8. [Claude AI Analysis Stage](#8-claude-ai-analysis-stage)
9. [Historical Replay Runner](#9-historical-replay-runner)
10. [API Backend](#10-api-backend)
11. [Frontend](#11-frontend)
12. [Configuration Reference](#12-configuration-reference)
13. [Key Design Decisions](#13-key-design-decisions)

---

## 1. System Overview

MakroGraph Intelligence is a **quantitative intelligence platform** that:
- Ingests company filings (US: SEC EDGAR, India: NSE/BSE/Screener)
- Ingests government policy documents (India: Economic Survey, Union Budget, RBI, SEBI, PIB, etc.)
- Runs NLP to extract entities and signals
- Detects and ranks investment themes
- Maps which companies benefit from each theme
- Identifies causal chains (policy → capacity gap → import substitution → stock beneficiary)
- Runs Claude AI to generate a final investment analysis

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                                 │
│                                                                     │
│  US:   SEC EDGAR (10-K, 10-Q, 8-K, earnings calls)                 │
│        FRED (macro series), EIA (commodities)                       │
│        Congress bills, Federal Register notices                     │
│                                                                     │
│  IN:   NSE / BSE (company announcements, filings)                   │
│        Screener.in (annual reports, presentations)                  │
│        Economic Survey · Union Budget · NITI Aayog · CEA           │
│        Ministry of Power · MNRE · DPIIT · Indian Railways           │
│        RBI Reports · SECI · PIB · SEBI · InvestIndia              │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   FETCHERS / PARSERS                                │
│  EdgarFetcher · NSEFetcher · BSEFetcher · ScreenerFetcher           │
│  IndiaPDFFetcher · PIBFetcher · RBIFetcher · SEBIFetcher           │
│  InvestIndiaFetcher · CommerceIndiaFetcher                         │
│  FredFetcher · EIAFetcher · WorldBankFetcher                       │
│  CongressFetcher · FederalRegisterFetcher                          │
│                                                                     │
│  PDFParser (pdfplumber → pymupdf fallback, max 500 pages)          │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│               POSTGRESQL (mg_documents · mg_entities                │
│               mg_signals · mg_themes · mg_causal_chains …)         │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   NLP PIPELINE                                      │
│  EntityExtractor (spaCy + rule-based) → SignalExtractor             │
│  EmbeddingEngine (all-MiniLM-L6-v2 → VectorStore/pgvector)         │
│  GraphBuilder → Neo4j (optional)                                    │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│              THEME DETECTION & RANKING                              │
│  ThemeDetector → ThemeRanker → BeneficiaryMapper                   │
│  ThemeCanonicalizer (embedding + Claude dedup)                      │
│  GeminiNoiseFilter (Claude-powered noise removal)                  │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│           INDIA INTELLIGENCE LAYERS (IN only)                       │
│  L1 PolicyIntelligence → L2 CapacityRequirements                   │
│  → L3 CapacityGaps → L4 ImportDependency                          │
│  → L5 LocalizationOpportunities → L6 SupplyChainDB                │
│  → L7 BeneficiaryDiscovery → L8 TenderIntelligence                │
│  → L9 OrderBookDetector → L10 CausalChainGenerator                │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│              CLAUDE AI ANALYSIS (Final Stage)                       │
│  Shortlisted themes + top-ranked stocks → Claude claude-sonnet-4-6 │
│  → Investment analysis text (logged + stored in stats)             │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│          FASTAPI BACKEND (port 8000) + REACT FRONTEND (port 5173)  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Storage Layer

**Database:** PostgreSQL (`makrograph` database, localhost:5432)  
**Extensions:** `pgvector` (semantic search), `pg_trgm` (text similarity)  
**Schema file:** `schema/postgres_schema.sql`

### Key Tables

| Table | Purpose |
|-------|---------|
| `mg_documents` | All ingested documents (filings, PDFs, announcements). `processing_status` tracks pipeline stage: `fetched → nlp_done → embedded → graph_built`. `country` column isolates US vs IN docs. |
| `mg_entities` | Deduplicated named entities (COMPANY, TECHNOLOGY, SECTOR, CONCEPT, REGULATION, LOCATION). Canonical name is the key. |
| `mg_document_entities` | Many-to-many: which entities appear in which documents, with sentiment and context snippets. |
| `mg_signals` | Investment signals extracted per document (capex_increase, demand_surge, regulatory_tailwind, etc.). |
| `mg_themes` | Detected investment themes with strength_score, momentum_score, conviction level, snapshot_date. |
| `mg_theme_companies` | Which companies are linked to which themes, with role and beneficiary_type. |
| `mg_causal_chains` | Causal chain records: trigger → mechanism → terminal_effect → beneficiary sectors. |
| `mg_policy_events` | Policy-layer events from government sources (Congress bills, RBI circulars, PIB announcements, etc.). |
| `mg_macro_series` | FRED / World Bank time-series data (GDP, CPI, interest rates, etc.). |
| `mg_commodity_series` | EIA commodity price data (WTI crude, natural gas, etc.). |
| `mg_replay_runs` | Historical replay audit log (batch, dates, doc counts, theme counts). |
| `mg_india_beneficiaries` | India-specific company beneficiaries with conviction_score, supply_chain_node, order_book signals. |
| `mg_policy_targets` | Extracted government targets (e.g. "500 GW solar by 2030") from Layer 1. |
| `mg_india_import_dependencies` | Sector import dependency ratios (Layer 4). |
| `mg_india_localization_opps` | Localization opportunity records (Layer 5). |

### Document Processing Status Flow

```
fetched → nlp_done → embedded → graph_built
```
Each stage reads docs at its input status and updates them on completion. NLP stage reads `fetched`, marks `nlp_done`. Graph stage reads `nlp_done`, marks `graph_built`.

---

## 3. Shared Components

### PDFParser (`src/makrograph/parser/pdf_parser.py`)
- Primary: `pdfplumber` (best text extraction quality)
- Fallback: `pymupdf` (handles encrypted/damaged PDFs)
- Hard cap: 500 pages per PDF to prevent memory exhaustion
- NUL byte stripping before database storage (Postgres rejects `\x00`)

### Deduplicator (`src/makrograph/dedup/deduplicator.py`)
- URL-hash dedup (always)
- Content-hash dedup (when full text is available)
- On conflict: `ON CONFLICT DO NOTHING` — existing records preserved

### EntityExtractor (`src/makrograph/nlp/entity_extractor.py`)
- spaCy NLP model (optional; falls back to rule-based if not installed)
- Batch processing via `nlp.pipe()` for efficiency
- Entity types: COMPANY, TECHNOLOGY, SECTOR, CONCEPT, PRODUCT, REGULATION, LOCATION, PERSON
- Noise filter: removes generic words, US state names, common false positives
- India injection: `IndiaEntityInjector` adds ministry/scheme/sector entities specific to Indian policy text

### SignalExtractor (`src/makrograph/nlp/signal_extractor.py`)
- Pattern-based extraction of 17 signal types
- Key types: `capex_increase`, `demand_surge`, `supply_bottleneck`, `technology_adoption`, `regulatory_tailwind`, `regulatory_headwind`, `order_win`, etc.

### EmbeddingEngine (`src/makrograph/nlp/embeddings.py`)
- Model: `all-MiniLM-L6-v2` (sentence-transformers)
- Output: 384-dim vectors stored in `pgvector`
- Used for semantic theme similarity and theme canonicalization

### LLMReasoner (`src/makrograph/llm/llm_reasoner.py`)
- Provider: **Anthropic Claude** (`claude-sonnet-4-6`)
- API key: `config/secrets.json → anthropic.api_key` (gitignored)
- Daily cost cap: $20 USD
- Used for: theme canonicalization, shortlist analysis, noise filtering

---

## 4. US Intelligence Pipeline

**Entry point:** `src/makrograph/pipeline/intelligence_pipeline.py → run_full()` (market.country = "US")  
**Run script:** `run_pipeline.py` or `backend/main.py → /api/macro/fetch`

### Stage 1: Ingest (`run_ingest`)

**Source:** SEC EDGAR  
**Fetcher:** `EdgarFetcher` (`src/makrograph/fetcher/edgar_fetcher.py`)

- Reads `edgar.ticker_list` or `edgar.cik_list` from config
- Discovers filings since last checkpoint (stored in DB per source)
- Downloads 10-K, 10-Q, 8-K, earnings transcripts as PDF or HTML
- Parses text with PDFParser
- Deduplicates by URL hash + content hash
- Normalizes text (TextNormalizer)
- Upserts into `mg_documents` with `country='US'`, `processing_status='fetched'`
- Updates checkpoint after each successful run

### Stage 2: NLP (`run_nlp`)

- Reads `mg_documents` where `processing_status='fetched'` and `country='US'`
- Phase 1: Resolve raw text (from `raw_text` column, or re-reads `local_path` PDF/HTML)
- Phase 2: Batch spaCy NER on all docs in the batch simultaneously (`nlp.pipe`)
- Phase 3: Per-doc entity + signal processing
  - Merges spaCy entities with rule-based entities
  - Deduplicates by (canonical_name, entity_type)
  - Filters noise entities
  - Stores entity records → `mg_entities`
  - Stores document-entity links → `mg_document_entities`
  - Runs SignalExtractor → stores to `mg_signals`
- Marks docs `nlp_done`

### Stage 3: Embeddings (`run_embeddings`)

- Reads `nlp_done` documents
- Generates 384-dim embeddings from raw text
- Upserts to VectorStore (pgvector table)
- Marks docs `embedded`

### Stage 4: Graph Building (`run_graph`)

- Reads `embedded` documents
- GraphBuilder creates entity nodes + co-occurrence edges in Neo4j (if enabled)
- GraphEvolutionTracker: monitors theme velocity, detects acceleration
- Marks docs `graph_built`

### Stage 5: Themes (`run_themes`)

See [Section 7](#7-theme-detection--ranking) — shared with India pipeline.

### Stage 6: LLM Enrichment (`run_llm_enrichment`)

- Runs LLMReasoner on low-conviction themes to promote/demote
- GraphRAG: multi-hop graph queries → LLM reasoning

### Stage 7: Macro (`run_macro`)

For US market:
1. **FRED** — fetches GDP, CPI, Federal Funds Rate, unemployment, etc. → `mg_macro_series`
2. **EIA** — fetches WTI crude, natural gas, Henry Hub prices → `mg_commodity_series`
3. **World Bank** — GDP growth, inflation, trade data
4. **Congress** — bill tracking (bills related to tech, energy, defense) → `mg_policy_events`
5. **Federal Register** — regulatory notices → `mg_policy_events`
6. **Constraint Engine** — scores active themes against macro context (e.g. rising rates constrain real estate themes)

### Stage 8: Claude AI Analysis (`run_gemini_analysis`)

See [Section 8](#8-claude-ai-analysis-stage).

---

## 5. India Intelligence Pipeline

India has **two parallel tracks** that complement each other:

| Track | What it fetches | Where stored |
|-------|----------------|--------------|
| **Company Filings** (NSE/BSE/Screener) | Announcements, order wins, concall transcripts, annual reports | `mg_documents` with `country='IN'` |
| **Policy NLP** (PDFs from govt ministries) | Budget, Economic Survey, RBI reports, SEBI circulars, PIB | `mg_documents` with `country='IN'`, then also `mg_policy_events` |

---

### 5a. India Company Filings

**Entry point:** `IntelligencePipeline.run_ingest_india()`  
**Run via:** historical runner or `backend/main.py → /api/macro/fetch` (country=IN)

#### Sources

| Source Key | Fetcher | What it ingests |
|------------|---------|----------------|
| `nse_india` | `NSEFetcher` | NSE corporate announcement API — board decisions, order wins, results, capex, M&A |
| `bse_india` | `BSEFetcher` | BSE Listing Centre — same categories, BSE-listed companies |
| `screener_india` | `ScreenerFetcher` | Screener.in — annual reports, investor presentations, concall links |

#### Design decision: Metadata-only by default, PDF-on-demand

NSE/BSE API returns full announcement metadata (company, date, subject, filing_type) without downloading PDFs. For most filings (board meeting outcomes, order wins), the title IS the signal — no full PDF needed.

Full PDF download is reserved for high-value types via `run_pdf_fetch_india()`:
- Annual reports, investor presentations, concall transcripts
- Order win announcements with financial details
- M&A/restructuring documents

**BSE ZIP fallback:** BSE sometimes serves ZIP archives at PDF URLs. The fetcher detects `PK\x03\x04` magic bytes, extracts the first PDF from the ZIP, and processes it normally.

**NSE Akamai bypass:** Uses `curl_cffi` (Chrome fingerprint impersonation) to bypass Akamai bot protection. Falls back to plain `requests` with a warning.

#### Processing flow

```
NSEFetcher.discover() → source_docs (metadata only)
    ↓
For each doc:
    - URL-hash dedup check
    - If filing_type in {annual_report, presentation, concall, earnings}:
        → Download PDF → PDFParser → raw_text
    - Else: use title as raw_text
    ↓
PGStore.upsert_document() → mg_documents (country='IN')
    ↓
PGStore.set_checkpoint(source_key)
```

---

### 5b. India Policy NLP

**Entry point:** `scripts/run_india_policy_nlp.py`  
**Run command:** `python3 scripts/run_india_policy_nlp.py --since 2020-01-01`

This is a **standalone script** (not called by `run_ingest_india`) that fetches government policy PDFs.

#### Source Tiers

**Tier 1 — Highest signal (macro PDFs):**

| Source Key | URL | What |
|------------|-----|------|
| `economic_survey` | mospi.gov.in / indiabudget.gov.in | Annual Economic Survey (500+ page volumes, multi-volume) |
| `union_budget` | indiabudget.gov.in | Union Budget documents (budget speech, receipts, expenditure, Finance Bill) |
| `niti_aayog` | niti.gov.in | NITI Aayog reports and strategy papers |
| `cea` | dea.gov.in | Chief Economic Adviser reports |
| `power_ministry` | powermin.gov.in | Power sector policy and capacity plans |
| `mnre` | mnre.gov.in | Renewable energy targets and PLI scheme docs |
| `dpiit` | dpiit.gov.in | Make in India, PLI, FDI policy documents |
| `rbi_reports` | rbi.org.in | RBI Annual Reports, Monetary Policy Reports |

**Tier 2 — Sector-specific (PDFs):**

| Source Key | What |
|------------|------|
| `powergrid` | Power Grid Corporation capex plans |
| `ntpc` | NTPC capacity addition documents |
| `seci` | Solar Energy Corporation tenders and capacity |
| `indian_railways` | Railway Board circulars, capex plans |
| `steel_ministry` | Steel sector policy |
| `heavy_industries` | PLI for automobiles, heavy machinery |
| `coal_ministry` | Coal production targets |
| `chemicals_ministry` | Chemical sector PLI |

**Tier 3 — Legislative / Research:**

| Source Key | What |
|------------|------|
| `prs_india` | PRS Legislative Research — bill summaries, committee reports |

**Secondary (RSS/HTML fetchers, run alongside PDFs):**
- `rbi_india` → `RBIFetcher` — RBI press releases (HTML)
- `invest_india` → `InvestIndiaFetcher` — Invest India announcements
- `sebi_india` → `SEBIFetcher` — SEBI circulars (sitemap scraping)

**Tier 4 Optional (disabled by default):**
- `pib_india` → `PIBFetcher` — PIB press releases (RSS). Enable via `pib.enabled: true` in `settings.yaml`.

#### IndiaPDFFetcher (`src/makrograph/fetcher/india_pdf_fetcher.py`)

Single unified fetcher for Tier 1–3 sources.

**SSL handling:** Indian government domains (`.nic.in`, `*.gov.in`) often have certificate issues. The fetcher uses `_NO_VERIFY_DOMAINS` set to selectively disable SSL verification for known problematic domains. `urllib3.InsecureRequestWarning` is suppressed for these.

**Caps:**
- `max_results_per_source`: 500 (prevents one rich source exhausting global budget)
- `max_results_per_run`: 5000 (global cap across all Tier 1–3 sources)

**Date handling:** `published_at` is extracted from URL patterns and page text (e.g. `/2023-24/` in budget URLs, dates in PDF filenames). When genuinely unknown, stored as `NULL` — never falls back to today's date.

#### Policy NLP processing flow

```
IndiaPDFFetcher.discover(since) → source_docs (562 docs in initial run)
    ↓
For each source_doc:
    - Download PDF (requests, verify=False for .nic.in)
    - PDFParser.parse() → raw_text
    - raw_text.replace("\x00", "") [NUL byte strip for Postgres]
    - content_hash = MD5(url + raw_text)
    - doc_date = doc.published_at.date() or None
    ↓
PGStore.upsert_document() → mg_documents (source_name='india_pdf', country='IN')
    ↓
RBIFetcher / InvestIndiaFetcher / SEBIFetcher (secondary sources)
    ↓
PIBFetcher (if enabled)
    ↓
HistoricalRunner.run() → NLP pass on all newly stored docs
    → 482 docs processed, 842 entities, 157 signals
    ↓
IndiaCausalChainGenerator.score_and_persist() → 21 causal chains
```

**Typical run output (initial fetch since 2020-01-01):**

| Source | Docs |
|--------|------|
| economic_survey | 149 |
| union_budget | 335 |
| niti_aayog / cea / mnre / seci | ~78 |
| rbi_india (press releases) | 9 |
| invest_india | 6 |
| sebi_india | 182 |
| **Total** | **759** |

---

### 5c. India Intelligence Layers 1–10

**Entry point:** `IntelligencePipeline.run_india_intelligence()`  
**Called:** after NLP pass, during `run_full()` for IN market or via API `/api/macro/fetch`

These 10 layers build the causal intelligence that powers the India ranking engine.

#### Layer 1 — PolicyIntelligenceEngine (`india/policy_intelligence.py`)

- Static knowledge base of well-known government targets (e.g. "500 GW renewables by 2030", "PLI electronics $10B")
- Live extraction: scans `mg_policy_events` (country='IN') for numeric targets using regex patterns
- Output: `PolicyTarget` records → stored to `mg_policy_targets`
- Sources: MNRE, Union Budget, NITI Aayog, RBI, Railways, PLI schemes, DPIIT

#### Layer 2 — CapacityRequirementGenerator (`india/capacity_engine.py`)

- Takes `PolicyTarget` list from Layer 1
- Maps each target to component requirements
  - Example: "280 GW solar" → requires solar panels, inverters, transformers, cables, EPC contractors
- Output: `CapacityRequirement` records listing each component, quantity estimate, and theme name

#### Layer 3 — CapacityGapDetector (`india/capacity_engine.py`)

- Compares capacity requirements (Layer 2) against known domestic production capacity
- Identifies where domestic supply < requirement → capacity gap
- Assigns severity: `critical`, `high`, `medium`, `low`
- Output: `CapacityGap` records → stored to DB; gap theme names flow into Layer 7

#### Layer 4 — ImportDependencyEngine (`india/import_localization.py`)

- Scans sectors for import dependency (% of demand met by imports)
- Min threshold: `min_import_share=0.50` (sectors >50% import-dependent)
- Static + semi-dynamic data on import ratios by sector
- Output: `ImportDependency` records → stored to `mg_india_import_dependencies`
- Examples: semiconductors, solar cells, defense electronics, specialty chemicals

#### Layer 5 — LocalizationOpportunityEngine (`india/import_localization.py`)

- Takes import dependencies (Layer 4)
- Cross-references government incentives (PLI schemes, import duties, Make in India)
- Identifies sectors where India is actively subsidizing domestic production → "localization opportunities"
- Output: `LocalizationOpportunity` records → stored to DB; opportunity theme names flow into Layer 7

#### Layer 6 — IndiaSupplyChainDB (`india/supply_chain_db.py`)

- In-memory supply chain knowledge graph (no DB write — static reference data)
- Nodes: raw materials, components, manufacturers, integrators, end-users
- Edges: supply-of, requires, competes-with
- `get_bottleneck_nodes(severity='critical')` → components with no domestic alternative
- Used by Layer 7 to constrain beneficiary discovery to supply-chain-aware companies

#### Layer 7 — BeneficiaryDiscoveryLayer (`india/beneficiary_discovery.py`)

- Combines gap themes (Layer 3) + localization themes (Layer 5)
- For each theme, scans `mg_signals` in lookback window for companies with:
  - Order book growth signals
  - Capex expansion in theme-relevant sectors
  - PLI scheme beneficiary mentions
- Cross-references supply chain DB (Layer 6) for structural positioning
- Assigns `conviction_score`, `beneficiary_type` (direct/indirect/derivative)
- Output: `Beneficiary` records → stored to `mg_india_beneficiaries`

#### Layer 8 — TenderIntelligence (`india/tender_intelligence.py`)

- Parses tender feed records (if provided via `tender_records` parameter)
- Extracts: tender value, issuing authority, sector, due date
- Creates signals from high-value tenders (e.g. Railways ₹5,000 Cr electrification tender)
- Output: tender signals → stored to DB

#### Layer 9 — OrderBookPressureDetector (`india/order_book_detector.py`)

- Scans `mg_documents` batch (NSE/BSE filings, concall transcripts) for order book commentary
- Pattern matching: "order book at ₹X Cr", "order inflows of ₹Y Cr", "L1 in tender"
- Generates `order_book_growth` signals for companies with expanding backlogs
- Output: signals → `mg_signals`

#### Layer 10 — IndiaCausalChainGenerator (`india/causal_chain_generator.py`)

- Reads `mg_signals` to auto-discover recurring signal patterns
- Builds chains: `policy_catalyst → sector_impact → component_demand → company_beneficiary`
- `CausalMapper.auto_discover()` finds 21+ chains from signal co-occurrence
- Scores chains by signal frequency and recency
- Output: `mg_causal_chains` (with `country='IN'`)

---

## 6. Macro & Policy Data Layer

**Entry point:** `IntelligencePipeline.run_macro()`  
**Called:** after graph building in `run_full()`

### US Macro Sources

| Source | Fetcher | Data |
|--------|---------|------|
| FRED | `FredFetcher` | GDP, CPI, Federal Funds Rate, unemployment, PMI, yield curve |
| EIA | `EIAFetcher` | WTI crude, Brent, Henry Hub gas, gasoline, heating oil |
| World Bank | `WorldBankFetcher` | Cross-country GDP growth, inflation, trade balance |
| Congress | `CongressFetcher` | Bills relevant to tech, energy, defense, infrastructure |
| Federal Register | `FederalRegisterFetcher` | Regulatory notices affecting industry sectors |

### India Macro Sources (via `run_macro` when country='IN')

| Source | Fetcher | Data |
|--------|---------|------|
| PIB | `PIBFetcher` | Press releases → `mg_policy_events` (country='IN') |
| SEBI | `SEBIFetcher` | Circulars → `mg_policy_events` |
| RBI | `RBIFetcher` | Press releases, rate decisions → `mg_policy_events` |
| InvestIndia | `InvestIndiaFetcher` | FDI announcements → `mg_policy_events` |
| Commerce/DGFT | `CommerceIndiaFetcher` | Trade policy notices → `mg_policy_events` |

> **Note:** The richer India policy intelligence (PDFs) is fetched separately via `run_india_policy_nlp.py`. The `run_macro()` India path handles press-release/RSS-level policy events only.

### Constraint Engine

After fetching macro data, the `ConstraintEngine` runs:
- For each active theme: checks if macro context supports or constrains it
- Example: rising interest rates → constrain capital-intensive themes
- Example: strong FDI inflow → boosts manufacturing themes
- Adds `macro_constraint_score` to theme records

---

## 7. Theme Detection & Ranking

**Entry point:** `IntelligencePipeline.run_themes()`  
**Shared between US and India pipelines (country parameter gates DB queries)**

### Detection flow

```
1. Load all signals in window → mg_signals (17 signal types, lookback N days)
2. Load entity-signal clusters → pre-aggregated counts per entity (avoids 600K+ row scans)
3. Load recent entities
4. Load active causal chains → extract entity keywords → +15 boost for chain-linked entities
5. ThemeDetector.detect() → seed themes from config keywords + auto-detected themes
6. ThemeDetector.detect_from_clusters_agg() → cluster-based theme discovery
7. GeminiNoiseFilter (Claude) → filters generic/noise themes
8. ThemeCanonicalizer → embedding similarity + Claude to merge near-duplicate theme names
9. ThemeRanker → scores themes: signal velocity, entity diversity, doc count, momentum
10. BeneficiaryMapper → links companies to themes via signal co-occurrence + ticker mentions
11. PGStore.upsert_theme_snapshot() → writes to mg_themes with snapshot_date
```

### Signal Types

```
capex_increase     capex_decrease    demand_surge       demand_slowdown
supply_bottleneck  supply_easing     technology_adoption technology_disruption
strategic_pivot    partnership_formed acquisition_intent  market_entry
regulatory_tailwind regulatory_headwind hiring_surge     inventory_buildup
inventory_drawdown
```

### Theme conviction levels

| Score | Conviction |
|-------|-----------|
| < 20 | `watch` |
| 20–40 | `emerging` |
| 40–70 | `confirmed` |
| > 70 | `high` |

### Historical replay mode

When `as_of_date` is in the past (replay mode), the theme window uses strictly `[as_of - signal_window_days, as_of]` — no extension to all-time data. This ensures each replay snapshot sees only signals from its own year, preventing old dominant themes from bleeding into later snapshots.

---

## 8. Claude AI Analysis Stage

**Entry point:** `IntelligencePipeline.run_gemini_analysis()`  
**Note:** Method is named `run_gemini_analysis` for historical reasons — it now calls Claude API.

### Flow

```
1. Load shortlisted themes (min 2 quarters of sustained signal) via PGStore.get_shortlisted_themes()
   Fallback: active themes with strength_score ≥ 30.0
2. Load top 20 ranked stocks via RankingEngine
3. LLMReasoner.analyze_shortlisted(themes, stocks, country) → Claude API call
4. Claude generates: theme summaries, stock rationale, risk factors, catalysts
5. Analysis logged at INFO level + returned in stats dict
```

### Claude config

| Config key | Value |
|-----------|-------|
| API key | `config/secrets.json → anthropic.api_key` (gitignored) |
| Model | `claude-sonnet-4-6` |
| Max tokens | 8192 |
| Temperature | 0.4 |
| Timeout | 120s |
| Daily cost cap | $20 USD |

### Also used for

- `ThemeCanonicalizer` — merges near-duplicate theme names via Claude
- `GeminiNoiseFilter` (`src/makrograph/themes/gemini_noise_filter.py`) — Claude-powered noise removal for detected themes
- `backend/main.py → _call_claude()` — API endpoint for ad-hoc AI analysis from the frontend

---

## 9. Historical Replay Runner

**Entry point:** `src/makrograph/pipeline/historical_runner.py → HistoricalRunner`  
**Also:** standalone script `run_india_yearly.py` for India yearly replay

### Purpose

Validates that the pipeline discovers investment themes **before markets recognize them**. Replays the pipeline month-by-month through a historical window, taking snapshots at each month-end.

### Design principles

1. **Never use `datetime.now()` during replay** — all date arithmetic is relative to `replay_date`
2. **Document filed_at is always real** — the actual EDGAR/NSE date, never overridden
3. **Replay date = window ceiling only** — it does not backdate documents
4. **Theme snapshots stamped with replay_date** — so score evolution is fully reconstructable
5. **Forward return validation** — after replay, actual price returns can be filled in to measure lead time

### India-specific NLP pass

When `market.country = 'IN'`, the HistoricalRunner activates `IndiaEntityInjector` which enriches entity extraction with:
- Ministry names and abbreviations (MNRE, DPIIT, NCLT, SEBI, RBI…)
- Scheme names (PLI, PM Gati Shakti, Sagarmala, UDAY…)
- Sector-specific vocabulary (GW, FDRE, ISTS, STU, DISCOM…)

---

## 10. API Backend

**File:** `backend/main.py`  
**Framework:** FastAPI  
**Port:** 8000  
**Start:** `.venv/bin/uvicorn backend.main:app --reload --port 8000`

### Key Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/kpis?country=IN` | Dashboard KPIs: doc count, entities, signals, active themes, chains |
| GET | `/api/themes?country=IN&as_of=&from_date=&min_strength=` | Active themes with snapshot history |
| GET | `/api/causal-chains?country=IN&as_of=` | Active causal chains with beneficiary sectors |
| GET | `/api/india/chain-beneficiaries?as_of=&min_conviction=` | India company beneficiaries with scores |
| GET | `/api/macro/series?series_id=GDP&country=IN` | Macro time-series data |
| GET | `/api/macro/commodity?commodity_id=WTI_CRUDE` | Commodity price series |
| GET | `/api/macro/events?as_of=&since_days=365` | Recent policy events |
| POST | `/api/macro/fetch` | Trigger full pipeline run (ingest + NLP + themes + macro) |
| GET | `/api/india/ranking?as_of=&top_n=` | India company ranking by theme conviction |
| GET | `/api/ranking?country=US` | US theme + stock ranking |
| GET | `/api/contradictions?country=IN` | Contradictory signals (bearish vs bullish on same entity) |
| GET | `/api/canonical/pending` | Themes pending canonicalization review |

### CORS

Allowed origins: `http://localhost:5173`, `http://localhost:3000`, `http://127.0.0.1:5173`

---

## 11. Frontend

**Directory:** `frontend/`  
**Framework:** React + TypeScript  
**Bundler:** Vite  
**Styling:** Tailwind CSS  
**Port:** 5173  
**Start:** `cd frontend && npm run dev`

### Key Tabs

| Tab | Component | What it shows |
|-----|-----------|--------------|
| Themes | `ThemesTab.tsx` | Active investment themes, strength scores, snapshot history chart |
| Company | `CompanyTab.tsx` | Company-level signals and beneficiary analysis |
| AI | `AITab.tsx` | Claude AI investment analysis output |
| Macro | `MacroTab.tsx` | FRED/EIA charts, policy events, causal chains |
| Rankings | `RankingTab.tsx` | Top-ranked themes and stocks |
| Shortlisted | `ShortlistedTab.tsx` | Multi-quarter sustained themes |
| Filings | `FilingsTab.tsx` | Raw document browser |
| Pipeline | `PipelineTab.tsx` | Run pipeline, view status, replay controls |

---

## 12. Configuration Reference

**Main config:** `config/settings.yaml`  
**Secrets (gitignored):** `config/secrets.json`

### Critical config blocks

```yaml
market:
  country: IN          # "US" or "IN" — controls which pipeline branches run

anthropic:
  api_key: ""          # blank here; real key in secrets.json
  model: "claude-sonnet-4-6"
  max_tokens: 8192
  temperature: 0.4

india_pdf:
  sources:             # Tier 1-3 sources in priority order
    - economic_survey
    - union_budget
    - niti_aayog
    - cea
    - power_ministry
    - mnre
    - dpiit
    - rbi_reports
    - seci
    - indian_railways
    - ...
  start_date: "2020-01-01"
  max_results_per_run: 5000
  max_results_per_source: 500

pib:
  enabled: false       # Tier 4 — enable manually when needed
```

---

## 13. Key Design Decisions

### US vs India isolation

`run_ingest()` (US/EDGAR) and `run_ingest_india()` (NSE/BSE) are **completely separate methods** that share no code paths. The `country` column in all tables (`mg_documents`, `mg_signals`, `mg_themes`, `mg_causal_chains`) ensures all DB queries are country-scoped.

### Policy NLP is a separate script, not part of `run_ingest_india()`

India policy documents (Economic Survey, Union Budget, etc.) are fetched by `scripts/run_india_policy_nlp.py` which runs `IndiaPDFFetcher` and secondary fetchers. This is intentionally separate because:
- Policy PDFs require different fetching logic (bulk PDF download, SSL quirks)
- Policy sources change less frequently (annual budget, quarterly RBI) vs daily NSE/BSE filings
- Allows running policy ingestion on-demand without touching the live filing pipeline

### Document date = publication date, never run date

When `published_at` cannot be determined from the URL/filename/page, it is stored as `NULL`. The run date (`date.today()`) is never used as a fallback — this would make undated historical documents appear current.

### PDF download only for high-value filing types

For NSE/BSE (which can have 3,000–8,000 filings per month), PDFs are downloaded only for:  
`annual_report`, `investor_presentation`, `concall_transcript`, `earnings`  
All other filings use the announcement title as raw_text — sufficient for signal extraction.

### GCP keys removed; Anthropic Claude is the sole LLM provider

The project previously used Google Gemini. All API calls now go to Anthropic Claude (`claude-sonnet-4-6`). The `_init_gemini()` method and `run_gemini_analysis()` names are legacy — they now call Claude.

### NUL byte stripping

Some Indian Railways and BSE PDFs contain `\x00` bytes. PostgreSQL rejects strings with NUL bytes. All `raw_text` values are stripped with `raw_text.replace("\x00", "")` before upsert.

### SSL verification for Indian government domains

`.nic.in` domains (and some `.gov.in`) have certificate chain issues. The fetcher uses `_NO_VERIFY_DOMAINS` to selectively skip SSL verification for these domains while keeping verification on for all others. `urllib3.InsecureRequestWarning` is suppressed for these domains only.
