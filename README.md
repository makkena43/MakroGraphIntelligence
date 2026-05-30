# MakroGraph Intelligence

> **Version 0.2.0** — Full intelligence pipeline: SEC filing ingestion → NLP → Semantic Embeddings → Knowledge Graph → Theme Detection → Selective LLM

---

## Pipeline Flow — All Steps Confirmed ✅

```
┌──────────────────────────┐
│  SEC EDGAR / Transcripts │   10-K · 10-Q · 8-K  (NVDA MSFT AMZN AMD TSLA AAPL META GOOGL)
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   Ingestion Layer        │   EDGARFetcher → SourceAdapter → PDFParser / BeautifulSoup
│                          │   Deduplication (xxhash) · TextNormalizer · Checkpoint tracking
└────────────┬─────────────┘
             │  mg_documents (PostgreSQL)
             ▼
┌──────────────────────────┐
│   Structured Storage     │   PostgreSQL tables: mg_documents, mg_source_checkpoints
│   (Structured JSON/PG)   │   Raw text saved to data/raw/  ·  status = 'fetched'
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   NLP Extraction         │   Strategy 1: Rule-based keywords (TECHNOLOGY · SECTOR · CONCEPT)
│                          │   Strategy 2: spaCy en_core_web_sm (ORG · PERSON · GPE · PRODUCT)
│                          │   Strategy 3: FinBERT NER (optional, config use_finbert: true)
│                          │   SignalExtractor → capex_increase · technology_adoption · etc.
│                          │   Stores → mg_entities · mg_document_entities · mg_signals
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   Semantic Layer         │   EmbeddingEngine (sentence-transformers all-MiniLM-L6-v2, 384-dim)
│                          │   VectorStore (pgvector / text fallback)
│                          │   TopicModeler (BERTopic) + BERTrend velocity/acceleration
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   Ontology Graph         │   GraphBuilder → Neo4j (bolt://localhost:7687)
│                          │   Nodes: Company · Technology · Concept · Sector · Person · Product
│                          │   Edges: INVESTS_IN · DEVELOPS · USES · ENABLES (co-occurrence)
│                          │   Graphiti TemporalGraphStore (bi-temporal, optional)
│                          │   GraphEvolutionTracker → momentum · acceleration metrics
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   Theme Engine           │   ThemeDetector  (seed-based + graph cross-sector signals)
│                          │   ThemeRanker    (composite score: signal 35% · breadth 30% · momentum 35%)
│                          │   BeneficiaryMapper → mg_theme_beneficiaries
│                          │   Stores → mg_themes · mg_theme_snapshots
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│   Selective LLM          │   GraphRAG  — multi-hop subgraph extraction → LLM context
│                          │   LLMReasoner — investment hypothesis generation
│                          │   Providers: DeepSeek-V3 · DeepSeek-R1 · Claude · GPT-4o
│                          │   Budget cap: $2/day · token tracking · response cache
│                          │   (disabled by default — set llm.llm_enabled: true + API key)
└──────────────────────────┘
```

---

## Core Philosophy

- **No daemons** — run once, checkpoint, exit
- **Incremental** — fetches only new filings since last checkpoint
- **Graceful degradation** — works without Neo4j, pgvector, or LLM API keys
- **Laptop-friendly** — all processing local; Neo4j + PostgreSQL via Homebrew

---

## Repository Structure

```
MakroGraphIntelligence/
│
├── run_pipeline.py               # ← Main entry point
├── config/
│   └── settings.yaml             # All configuration (tickers, models, thresholds)
├── schema/
│   ├── postgres_schema.sql       # PostgreSQL table definitions
│   └── neo4j_schema.cypher       # Neo4j constraints + indexes
├── requirements.txt
├── setup.py
│
├── src/makrograph/
│   │
│   ├── pipeline/
│   │   ├── intelligence_pipeline.py  # Orchestrator — all stages wired here
│   │   ├── runner.py                 # Legacy batch runner
│   │   └── checkpoint.py             # Source checkpoint management
│   │
│   ├── fetcher/                      # ── STAGE 1: INGESTION ──
│   │   ├── edgar_fetcher.py          # SEC EDGAR API — 10-K/10-Q/8-K per ticker
│   │   ├── source_adapter.py         # Fetch + download + FetchResult dataclass
│   │   ├── base_fetcher.py           # FetchResult(local_path, url, success, ...)
│   │   ├── async_fetcher.py          # Async HTTP downloader
│   │   └── web_fetcher.py            # Generic HTTP fetcher
│   │
│   ├── parser/
│   │   └── pdf_parser.py             # pdfplumber (primary) + PyMuPDF (fallback)
│   │                                 # HTML parsed via BeautifulSoup + lxml
│   ├── dedup/
│   │   └── deduplicator.py           # xxhash content dedup + URL dedup
│   │
│   ├── normalizer/
│   │   └── text_normalizer.py        # Encoding fix · whitespace · header/footer strip
│   │
│   ├── nlp/                          # ── STAGE 2: NLP EXTRACTION ──
│   │   ├── entity_extractor.py       # spaCy + keyword rules + FinBERT (optional)
│   │   │                             # Entity types: COMPANY TECHNOLOGY SECTOR
│   │   │                             #               CONCEPT PERSON PRODUCT LOCATION AMOUNT
│   │   ├── signal_extractor.py       # Investment signals: capex_increase · technology_adoption
│   │   │                             #   supply_bottleneck · demand_surge · partnership_formed
│   │   │                             #   regulatory_tailwind/headwind · acquisition_intent
│   │   └── embeddings.py             # EmbeddingEngine (sentence-transformers)
│   │
│   ├── storage/                      # ── STAGE 3: STORAGE ──
│   │   ├── pg_store.py               # PostgreSQL — all structured data
│   │   ├── graph_store.py            # Neo4j — knowledge graph (Bolt protocol)
│   │   ├── vector_store.py           # pgvector / text fallback for embeddings
│   │   └── db_store.py               # Legacy SQLite store
│   │
│   ├── ontology/                     # ── STAGE 4: ONTOLOGY GRAPH ──
│   │   ├── ontology_model.py         # NodeType · RelationType · OntologyNode · OntologyEdge
│   │   │                             # InvestmentTheme · ThemeConviction enums
│   │   ├── graph_builder.py          # Converts NLP results → Neo4j nodes + edges
│   │   │                             # build_from_pg_entities() — reads pre-extracted PG data
│   │   ├── graph_evolution.py        # Entity mention velocity + acceleration tracking
│   │   └── graphiti_store.py         # Bi-temporal knowledge graph (optional, graphiti-core)
│   │
│   ├── topics/                       # ── SEMANTIC LAYER ──
│   │   ├── topic_modeler.py          # BERTopic topic discovery across filing corpus
│   │   └── bertrend.py               # BERTrend: topic velocity + acceleration over time
│   │
│   ├── themes/                       # ── STAGE 5: THEME ENGINE ──
│   │   ├── theme_detector.py         # Seed themes + graph cross-sector detection
│   │   │                             # Uses BERTopic clusters + signal co-occurrence
│   │   ├── theme_ranker.py           # Composite score: signal · breadth · momentum weights
│   │   └── beneficiary_mapper.py     # Maps companies → theme beneficiary roles
│   │
│   ├── llm/                          # ── STAGE 6: SELECTIVE LLM ──
│   │   ├── llm_reasoner.py           # Claude / GPT-4o / DeepSeek-V3 / DeepSeek-R1
│   │   │                             # Tasks: hypothesis · disambiguation · enrichment
│   │   │                             # Budget cap · token tracking · response cache
│   │   └── graph_rag.py              # GraphRAG: Neo4j subgraph → LLM multi-hop reasoning
│   │                                 # Modes: LOCAL · GLOBAL · HYBRID
│   │
│   └── cli.py                        # Command-line interface
│
├── data/
│   ├── raw/                          # Downloaded SEC filings (HTML / PDF)
│   ├── parsed/                       # Extracted text
│   └── models/                       # Saved BERTopic model
│
└── tests/
    └── test_pipeline.py
```

---

## PostgreSQL Schema

| Table | Purpose |
|---|---|
| `mg_documents` | Every fetched filing — status lifecycle: `fetched → nlp_done → graph_built` |
| `mg_source_checkpoints` | Per-source last-run timestamps (incremental fetch) |
| `mg_entities` | Canonical entities (Company, Technology, Concept…) with mention counts |
| `mg_document_entities` | Entity ↔ document link table with sentiment + mention count |
| `mg_signals` | Investment signals extracted per document (capex, adoption, bottleneck…) |
| `mg_themes` | Detected investment themes with conviction + strength score |
| `mg_theme_snapshots` | Historical theme score snapshots for trend tracking |
| `mg_theme_beneficiaries` | Company → theme beneficiary mapping with roles |
| `mg_llm_calls` | Every LLM call logged (cost tracking + response cache) |

---

## Neo4j Knowledge Graph

| Label | Count (live) | Description |
|---|---|---|
| `Company` | 96 | Public companies from filings |
| `Concept` | 694 | Macro trends, financial concepts |
| `Technology` | 12 | AI, GPU, Cloud, Semiconductor… |
| `Sector` | 8 | Technology, Healthcare, Industrials… |
| `Product` | 13 | Named products and platforms |
| `Person` | 23 | Executives, board members |
| `Location` | 12 | Countries, regions |

| Relationship | Count (live) | Description |
|---|---|---|
| `INVESTS_IN` | 807 | Company → Technology/Concept |
| `ENABLES` | 55 | Technology co-occurrence pairs |
| `DEVELOPS` | — | Company builds a technology |
| `USES` | — | Company adopts a technology |
| `PART_OF` | — | Entity → Theme link |

---

## NLP Tools Used

| Tool | Module | Status |
|---|---|---|
| **spaCy** `en_core_web_sm` | `nlp/entity_extractor.py` | ✅ Active — ORG, PERSON, GPE, PRODUCT, MONEY |
| **FinBERT** (`ProsusAI/finbert`) | `nlp/entity_extractor.py` | ⚙️ Installed — enable via `nlp.use_finbert: true` |
| **sentence-transformers** `all-MiniLM-L6-v2` | `nlp/embeddings.py` + `storage/vector_store.py` | ✅ Active — 384-dim semantic embeddings |
| **BERTopic** | `topics/topic_modeler.py` + `themes/theme_detector.py` | ✅ Installed — topic clustering feeds theme detection |
| **BERTrend** | `topics/bertrend.py` | ✅ Installed — topic velocity + acceleration |
| **Filing Parser** (BeautifulSoup + lxml) | `pipeline/intelligence_pipeline.py` | ✅ Active — HTML/XBRL SEC filings |
| **Filing Parser** (pdfplumber / PyMuPDF) | `parser/pdf_parser.py` | ✅ Active — PDF annual reports |

---

## Quick Start

```bash
# 1. Setup
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# 2. Database (PostgreSQL + Neo4j must be running)
psql -U postgres -c "CREATE DATABASE makrograph;"
psql -U postgres -d makrograph -f schema/postgres_schema.sql
brew services start neo4j
# Change Neo4j password to 'makrograph' on first run

# 3. Run full pipeline
python run_pipeline.py

# 4. Run individual stages
python run_pipeline.py --stage ingest    # Fetch SEC filings only
python run_pipeline.py --stage nlp       # NLP extraction only
python run_pipeline.py --stage themes    # Theme detection only

# 5. Reports
python run_pipeline.py --report          # Print active themes
python run_pipeline.py --signals         # Print recent signals
```

---

## Configuration Reference (`config/settings.yaml`)

### Tickers to Track
```yaml
edgar:
  ticker_list: [NVDA, MSFT, AMZN, AMD, TSLA, AAPL, META, GOOGL]
  filing_types: [10-K, 10-Q, 8-K]
  max_filings_per_company: 5
```

### NLP
```yaml
nlp:
  use_spacy: true
  use_finbert: false        # Set true for FinBERT NER (requires model download)
  spacy_model: en_core_web_sm
  max_entities_per_doc: 500
```

### Semantic Embeddings
```yaml
embeddings:
  embedding_model: all-MiniLM-L6-v2
  embedding_dim: 384
```

### Ontology / Graph
```yaml
neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password: makrograph

graphiti:
  enabled: false            # Set true for bi-temporal graph tracking
```

### Theme Detection
```yaml
themes:
  min_companies_for_theme: 2
  ranking_weights:
    signal: 0.35
    breadth: 0.30
    momentum: 0.35
```

### Selective LLM (off by default)
```yaml
llm:
  llm_enabled: false
  llm_provider: deepseek    # deepseek | anthropic | openai
  llm_model: deepseek-chat  # deepseek-chat | deepseek-reasoner | gpt-4o-mini | claude-3-haiku
  llm_daily_cost_cap_usd: 2.0
# Set env var: export DEEPSEEK_API_KEY=sk-...
```

---

## Current Live Results (v0.2.0)

```
Tickers processed : NVDA · MSFT · AMD · AAPL · GOOGL
Filings ingested  : 7 documents (10-K / 10-Q / 8-K)
Entities extracted: 1,515 (COMPANY · TECHNOLOGY · CONCEPT · PERSON…)
Signals detected  : 200 (capex_increase · technology_adoption · supply_bottleneck…)
Graph nodes       : 858 across 7 label types in Neo4j
Graph edges       : 862 (INVESTS_IN + ENABLES)

Active Themes:
  1. AI Infrastructure Buildout          score=65.8  conviction=developing  companies=6
  2. Semiconductor Supply Chain Reshoring score=28.3  conviction=emerging    companies=3
```

---

## Databases Required

| Service | Version | Install |
|---|---|---|
| PostgreSQL | 15+ with TimescaleDB | `brew install postgresql@17 timescaledb` |
| Neo4j | 5.x | `brew install neo4j` then `brew services start neo4j` |

pgvector is optional — the system falls back to text column storage if unavailable.
