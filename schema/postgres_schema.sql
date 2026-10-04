-- ============================================================
-- MakroGraph Intelligence - PostgreSQL Schema
-- Metadata, signal, theme, and ontology storage
-- ============================================================

-- Enable pgvector extension for semantic embeddings
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for text similarity

-- ============================================================
-- 1. DOCUMENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_documents (
    id              BIGSERIAL PRIMARY KEY,
    source_name     VARCHAR(50)  NOT NULL,      -- edgar, nse, bse, transcript
    doc_type        VARCHAR(50)  NOT NULL,       -- 10-K, 10-Q, 8-K, earnings_call
    url             TEXT         NOT NULL UNIQUE,
    url_hash        VARCHAR(64)  NOT NULL,
    content_hash    VARCHAR(64)  NOT NULL UNIQUE,
    title           TEXT,
    company         TEXT,
    ticker          VARCHAR(20),
    cik             VARCHAR(20),                 -- SEC CIK number
    filing_type     VARCHAR(120),
    fiscal_period   VARCHAR(20),                -- Q1-2024, FY-2023
    filed_at        DATE,
    published_at    TIMESTAMP WITH TIME ZONE,
    local_path      TEXT,
    page_count      INTEGER      DEFAULT 0,
    word_count      INTEGER      DEFAULT 0,
    language        VARCHAR(10)  DEFAULT 'en',
    processing_status VARCHAR(30) DEFAULT 'fetched',  -- fetched|parsed|nlp_done|embedded|graph_built
    country         VARCHAR(10)  DEFAULT 'US',        -- ISO-2 market: US | IN | GB | ...
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_docs_source    ON mg_documents(source_name);
CREATE INDEX IF NOT EXISTS idx_mg_docs_ticker    ON mg_documents(ticker);
CREATE INDEX IF NOT EXISTS idx_mg_docs_type      ON mg_documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_mg_docs_filed     ON mg_documents(filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_mg_docs_status    ON mg_documents(processing_status);
CREATE INDEX IF NOT EXISTS idx_mg_docs_country   ON mg_documents(country);
-- Exact maker verification asks for a bounded set of issuer filings inside a
-- dated lookback window.  The individual ticker and date indexes force
-- PostgreSQL to combine broad scans before it can evaluate the evidence
-- passage.  This composite index keeps that final, decision-critical proof
-- check proportional to the candidate universe rather than the full corpus.
CREATE INDEX IF NOT EXISTS idx_mg_docs_country_ticker_filed
    ON mg_documents(country, ticker, filed_at DESC);

-- ============================================================
-- 2. ENTITIES (spaCy + FinBERT extracted)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_entities (
    id              BIGSERIAL PRIMARY KEY,
    entity_text     TEXT NOT NULL,
    entity_type     VARCHAR(50)  NOT NULL,       -- COMPANY, TECHNOLOGY, SECTOR, CONCEPT, PERSON, PRODUCT, REGULATION, LOCATION
    canonical_name  TEXT,                        -- normalized / resolved name
    ticker          VARCHAR(20),                 -- if entity is a public company
    mention_count   INTEGER      DEFAULT 1,
    first_seen_at   DATE,
    last_seen_at    DATE,
    confidence      FLOAT        DEFAULT 1.0,
    metadata        JSONB        DEFAULT '{}',
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(canonical_name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_mg_ent_type       ON mg_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_mg_ent_canonical  ON mg_entities(canonical_name);
CREATE INDEX IF NOT EXISTS idx_mg_ent_ticker     ON mg_entities(ticker);
CREATE INDEX IF NOT EXISTS idx_mg_ent_text_trgm  ON mg_entities USING gin(entity_text gin_trgm_ops);

-- Document <-> Entity co-occurrence
CREATE TABLE IF NOT EXISTS mg_document_entities (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT       NOT NULL REFERENCES mg_documents(id) ON DELETE CASCADE,
    entity_id       BIGINT       NOT NULL REFERENCES mg_entities(id)  ON DELETE CASCADE,
    mention_count   INTEGER      DEFAULT 1,
    sentiment_score FLOAT,                       -- entity sentiment in this doc (-1 to 1)
    context_snippets TEXT[],
    UNIQUE(document_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_mg_doc_ent_doc    ON mg_document_entities(document_id);
CREATE INDEX IF NOT EXISTS idx_mg_doc_ent_ent    ON mg_document_entities(entity_id);

-- ============================================================
-- 3. INVESTMENT SIGNALS
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_signals (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT       NOT NULL REFERENCES mg_documents(id) ON DELETE CASCADE,
    entity_id       BIGINT       REFERENCES mg_entities(id),
    signal_type     VARCHAR(80)  NOT NULL,
    -- Signal types:
    --   capex_increase, capex_decrease
    --   demand_surge, demand_slowdown
    --   supply_bottleneck, supply_easing
    --   strategic_pivot, partnership_formed, acquisition_intent
    --   technology_adoption, technology_disruption
    --   competition_threat, market_entry
    --   regulatory_change, regulatory_tailwind, regulatory_headwind
    --   hiring_surge, hiring_freeze
    --   inventory_buildup, inventory_drawdown
    signal_value    FLOAT,                       -- magnitude / quantified value if present
    signal_unit     VARCHAR(50),                 -- e.g. "USD_billions", "pct_yoy"
    direction       VARCHAR(20),                 -- positive | negative | neutral
    confidence      FLOAT        DEFAULT 0.7,
    context_text    TEXT,                        -- sentence where signal was found
    extracted_by    VARCHAR(30)  DEFAULT 'rule', -- rule | finbert | llm
    filed_at        DATE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_sig_type       ON mg_signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_mg_sig_doc        ON mg_signals(document_id);
CREATE INDEX IF NOT EXISTS idx_mg_sig_entity     ON mg_signals(entity_id);
CREATE INDEX IF NOT EXISTS idx_mg_sig_filed      ON mg_signals(filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_mg_sig_direction  ON mg_signals(direction);

-- ============================================================
-- 4. ONTOLOGY NODES (synchronized from Neo4j, queryable in PG)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_ontology_nodes (
    id              BIGSERIAL PRIMARY KEY,
    neo4j_id        VARCHAR(100) UNIQUE,
    node_type       VARCHAR(50)  NOT NULL,       -- Company, Technology, Sector, Concept, Product
    name            TEXT NOT NULL,
    properties      JSONB        DEFAULT '{}',
    mention_frequency INTEGER    DEFAULT 1,
    first_seen_at   DATE,
    last_seen_at    DATE,
    importance_score FLOAT       DEFAULT 0.0,
    country         VARCHAR(10)  DEFAULT 'US',   -- ISO-2 market: US | IN | GB | ...
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_node_country  ON mg_ontology_nodes(country);
CREATE INDEX IF NOT EXISTS idx_mg_node_type      ON mg_ontology_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_mg_node_name      ON mg_ontology_nodes(name);
CREATE INDEX IF NOT EXISTS idx_mg_node_name_trgm ON mg_ontology_nodes USING gin(name gin_trgm_ops);

-- ============================================================
-- 5. ONTOLOGY EDGES (synchronized from Neo4j)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_ontology_edges (
    id              BIGSERIAL PRIMARY KEY,
    neo4j_rel_id    VARCHAR(100) UNIQUE,
    source_node_id  BIGINT       NOT NULL REFERENCES mg_ontology_nodes(id),
    target_node_id  BIGINT       NOT NULL REFERENCES mg_ontology_nodes(id),
    relationship    VARCHAR(80)  NOT NULL,
    -- Relationship types:
    --   DEVELOPS, INVESTS_IN, USES, COMPETES_WITH, SUPPLIES_TO
    --   REGULATED_BY, MENTIONED_IN, PART_OF, LEADS, ACQUIRES
    weight          FLOAT        DEFAULT 1.0,    -- co-mention frequency / strength
    properties      JSONB        DEFAULT '{}',
    first_seen_at   DATE,
    last_seen_at    DATE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_edge_src       ON mg_ontology_edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_mg_edge_tgt       ON mg_ontology_edges(target_node_id);
CREATE INDEX IF NOT EXISTS idx_mg_edge_rel       ON mg_ontology_edges(relationship);

-- ============================================================
-- 6. TOPIC CLUSTERS (BERTopic output)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_topic_clusters (
    id              BIGSERIAL PRIMARY KEY,
    topic_id        INTEGER      NOT NULL,        -- BERTopic internal ID
    run_date        DATE         NOT NULL,
    top_words       TEXT[],
    top_ngrams      TEXT[],
    doc_count       INTEGER      DEFAULT 0,
    coherence_score FLOAT,
    label           TEXT,                -- Human-readable auto-label
    is_emerging     BOOLEAN      DEFAULT FALSE,  -- flagged by trend analysis
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(topic_id, run_date)
);

-- Document <-> Topic
CREATE TABLE IF NOT EXISTS mg_document_topics (
    document_id     BIGINT       NOT NULL REFERENCES mg_documents(id) ON DELETE CASCADE,
    topic_cluster_id BIGINT      NOT NULL REFERENCES mg_topic_clusters(id) ON DELETE CASCADE,
    probability     FLOAT        DEFAULT 1.0,
    PRIMARY KEY (document_id, topic_cluster_id)
);

-- ============================================================
-- 7. THEMES (cross-sector investment themes)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_themes (
    id              BIGSERIAL PRIMARY KEY,
    theme_name      TEXT NOT NULL,
    theme_slug      VARCHAR(100) NOT NULL,          -- machine-readable key (unique per country)
    description     TEXT,
    sectors         TEXT[],                       -- affected sectors
    signal_types    TEXT[],                       -- driving signals
    strength_score  FLOAT        DEFAULT 0.0,    -- 0-100
    momentum_score  FLOAT        DEFAULT 0.0,    -- recent acceleration
    conviction      VARCHAR(20)  DEFAULT 'emerging',  -- emerging|developing|confirmed|declining
    first_detected  DATE,
    last_updated    DATE,
    doc_count       INTEGER      DEFAULT 0,       -- # of documents citing this theme
    company_count   INTEGER      DEFAULT 0,
    hypothesis_text TEXT,                         -- LLM-generated investment hypothesis
    metadata        JSONB        DEFAULT '{}',
    is_active       BOOLEAN      DEFAULT TRUE,
    country         VARCHAR(10)  DEFAULT 'US',        -- ISO-2 market: US | IN | GB | ...
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS mg_themes_slug_country_key ON mg_themes(theme_slug, country);
CREATE INDEX IF NOT EXISTS idx_mg_theme_score    ON mg_themes(strength_score DESC);
CREATE INDEX IF NOT EXISTS idx_mg_theme_conv     ON mg_themes(conviction);
CREATE INDEX IF NOT EXISTS idx_mg_theme_active   ON mg_themes(is_active);
CREATE INDEX IF NOT EXISTS idx_mg_theme_country  ON mg_themes(country);

-- Theme Snapshots (temporal tracking of theme evolution)
CREATE TABLE IF NOT EXISTS mg_theme_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    theme_id        BIGINT       NOT NULL REFERENCES mg_themes(id) ON DELETE CASCADE,
    snapshot_date   DATE         NOT NULL,
    strength_score  FLOAT,
    momentum_score  FLOAT,
    doc_count       INTEGER,
    company_count   INTEGER,
    top_entities    JSONB,                        -- top entities at this point in time
    country         VARCHAR(10)  DEFAULT 'US',   -- ISO-2 market: US | IN | GB | ...
    UNIQUE(theme_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_mg_snap_country   ON mg_theme_snapshots(country);

-- ============================================================
-- 8. THEME BENEFICIARIES
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_theme_beneficiaries (
    id              BIGSERIAL PRIMARY KEY,
    theme_id        BIGINT       NOT NULL REFERENCES mg_themes(id) ON DELETE CASCADE,
    entity_id       BIGINT       NOT NULL REFERENCES mg_entities(id),
    ticker          VARCHAR(20),
    company_name    TEXT,
    beneficiary_type VARCHAR(30) DEFAULT 'direct',  -- direct | indirect | disruptee
    company_role    VARCHAR(50) DEFAULT '',        -- infrastructure_provider | supplier | bottleneck_player | beneficiary | downstream_user | hidden_enabler
    relevance_score FLOAT        DEFAULT 0.0,     -- 0-100
    signal_count    INTEGER      DEFAULT 0,
    capex_signals   INTEGER      DEFAULT 0,        -- count of capex-specific signals
    quarterly_mentions JSONB DEFAULT '{}',         -- {"Q1-2024": 3, "Q2-2024": 5, ...}
    first_seen_at   DATE,
    last_seen_at    DATE,
    rank_in_theme   INTEGER,
    reasoning       TEXT,                         -- why this company benefits
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(theme_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_mg_ben_theme      ON mg_theme_beneficiaries(theme_id);
CREATE INDEX IF NOT EXISTS idx_mg_ben_ticker     ON mg_theme_beneficiaries(ticker);
CREATE INDEX IF NOT EXISTS idx_mg_ben_score      ON mg_theme_beneficiaries(relevance_score DESC);

-- ============================================================
-- 9. SEMANTIC EMBEDDINGS (pgvector)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT       REFERENCES mg_documents(id) ON DELETE CASCADE,
    entity_id       BIGINT       REFERENCES mg_entities(id)  ON DELETE CASCADE,
    theme_id        BIGINT       REFERENCES mg_themes(id)    ON DELETE CASCADE,
    embedding_type  VARCHAR(50)  NOT NULL,         -- document | entity | theme | chunk
    model_name      VARCHAR(100) NOT NULL,          -- e.g. all-MiniLM-L6-v2
    embedding       vector(384),                   -- 384-dim for MiniLM, 768 for FinBERT
    text_chunk      TEXT,
    chunk_index     INTEGER      DEFAULT 0,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_emb_doc        ON mg_embeddings(document_id);
CREATE INDEX IF NOT EXISTS idx_mg_emb_type       ON mg_embeddings(embedding_type);
-- IVFFlat index for fast ANN search
CREATE INDEX IF NOT EXISTS idx_mg_emb_ivfflat    ON mg_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- 10. LLM REASONING LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_llm_log (
    id              BIGSERIAL PRIMARY KEY,
    task_type       VARCHAR(80)  NOT NULL,         -- theme_hypothesis | entity_resolution | signal_validation
    input_summary   TEXT,
    prompt_tokens   INTEGER      DEFAULT 0,
    completion_tokens INTEGER    DEFAULT 0,
    model_used      VARCHAR(100),
    output_text     TEXT,
    output_json     JSONB,
    cost_usd        NUMERIC(10,6),
    latency_ms      INTEGER,
    related_theme_id BIGINT      REFERENCES mg_themes(id),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- 11. SOURCE CHECKPOINTS (track last fetched per source)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_source_checkpoints (
    source_name     VARCHAR(50)  PRIMARY KEY,
    last_fetched_at TIMESTAMP WITH TIME ZONE,
    last_doc_count  INTEGER      DEFAULT 0,
    last_run_status VARCHAR(30)  DEFAULT 'ok',
    metadata        JSONB        DEFAULT '{}',
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- 12. PIPELINE RUN LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_pipeline_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE         NOT NULL,
    stage           VARCHAR(50)  NOT NULL,          -- ingest | nlp | embed | graph | topics | themes | llm
    docs_processed  INTEGER      DEFAULT 0,
    entities_found  INTEGER      DEFAULT 0,
    signals_found   INTEGER      DEFAULT 0,
    themes_updated  INTEGER      DEFAULT 0,
    duration_sec    FLOAT,
    status          VARCHAR(20)  DEFAULT 'ok',
    error_message   TEXT,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- VIEWS
-- ============================================================

-- Emerging themes with top beneficiaries
CREATE OR REPLACE VIEW v_emerging_themes AS
SELECT
    t.id,
    t.theme_name,
    t.strength_score,
    t.momentum_score,
    t.conviction,
    t.doc_count,
    t.company_count,
    t.sectors,
    t.first_detected,
    t.last_updated
FROM mg_themes t
WHERE t.is_active = TRUE
  AND t.conviction IN ('emerging', 'developing', 'confirmed')
ORDER BY t.momentum_score DESC, t.strength_score DESC;

-- Theme signals heatmap
CREATE OR REPLACE VIEW v_theme_signal_heatmap AS
SELECT
    t.theme_slug,
    s.signal_type,
    COUNT(*) AS signal_count,
    AVG(s.confidence) AS avg_confidence,
    MAX(s.filed_at) AS latest_signal
FROM mg_themes t
JOIN mg_theme_beneficiaries tb ON tb.theme_id = t.id
JOIN mg_entities e ON e.id = tb.entity_id
JOIN mg_document_entities de ON de.entity_id = e.id
JOIN mg_signals s ON s.document_id = de.document_id
GROUP BY t.theme_slug, s.signal_type;

-- ============================================================
-- 13. ENTITY TIMESERIES  (temporal intelligence)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_entity_timeseries (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       BIGINT       NOT NULL REFERENCES mg_entities(id) ON DELETE CASCADE,
    period_date     DATE         NOT NULL,            -- weekly / monthly bucket
    period_type     VARCHAR(20)  DEFAULT 'monthly',   -- weekly | monthly | quarterly
    mention_count   INTEGER      DEFAULT 0,
    signal_count    INTEGER      DEFAULT 0,
    sentiment_avg   FLOAT,
    doc_count       INTEGER      DEFAULT 0,
    sector_spread   INTEGER      DEFAULT 0,           -- # distinct sectors co-mentioned
    velocity        FLOAT        DEFAULT 0.0,         -- mentions / window vs prior
    acceleration    FLOAT        DEFAULT 0.0,         -- delta velocity
    trend_direction VARCHAR(20),                      -- accelerating|stable|decelerating|dormant
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(entity_id, period_date, period_type)
);

CREATE INDEX IF NOT EXISTS idx_mg_ets_entity    ON mg_entity_timeseries(entity_id);
CREATE INDEX IF NOT EXISTS idx_mg_ets_period    ON mg_entity_timeseries(period_date DESC);
CREATE INDEX IF NOT EXISTS idx_mg_ets_velocity  ON mg_entity_timeseries(velocity DESC);
CREATE INDEX IF NOT EXISTS idx_mg_ets_accel     ON mg_entity_timeseries(acceleration DESC);

-- ============================================================
-- 14. BUSINESS EVENTS  (event-centric architecture)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_events (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT       REFERENCES mg_documents(id) ON DELETE CASCADE,
    event_type      VARCHAR(80)  NOT NULL,
    -- factory_expansion | factory_closure | shortage | oversupply
    -- price_increase | price_decrease | export_restriction | import_restriction
    -- investment_announcement | partnership_announcement | acquisition
    -- regulatory_approval | regulatory_ban | technology_breakthrough
    -- demand_surge | demand_collapse | supply_chain_disruption | hiring_announcement
    subject_entity  TEXT NOT NULL,            -- primary entity affected
    subject_type    VARCHAR(50)  DEFAULT 'Company',
    description     TEXT,
    magnitude       FLOAT,                            -- quantified if available
    magnitude_unit  VARCHAR(50),                      -- USD_bn, pct, units
    direction       VARCHAR(20)  DEFAULT 'positive',  -- positive | negative | neutral
    confidence      FLOAT        DEFAULT 0.75,
    second_order    TEXT[],                           -- indirect entities affected
    context_text    TEXT,
    filed_at        DATE,
    country         VARCHAR(10)  DEFAULT 'US',        -- ISO-2 market: US | IN | GB | ...
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_ev_country    ON mg_events(country);
CREATE INDEX IF NOT EXISTS idx_mg_ev_type       ON mg_events(event_type);
CREATE INDEX IF NOT EXISTS idx_mg_ev_subject    ON mg_events(subject_entity);
CREATE INDEX IF NOT EXISTS idx_mg_ev_filed      ON mg_events(filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_mg_ev_doc        ON mg_events(document_id);
CREATE INDEX IF NOT EXISTS idx_mg_ev_direction  ON mg_events(direction);

-- ============================================================
-- 15. CAUSAL CHAINS  (causal ontology layer)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_causal_chains (
    id              BIGSERIAL PRIMARY KEY,
    chain_id        VARCHAR(100) NOT NULL UNIQUE,
    chain_name      TEXT NOT NULL,
    description     TEXT,
    depth           INTEGER      DEFAULT 1,           -- number of hops
    terminal_effect TEXT,                     -- final downstream entity
    activation_score FLOAT       DEFAULT 0.0,         -- 0-100 current firing strength
    links           JSONB        DEFAULT '[]',         -- ordered array of CausalLink dicts
    -- Each link: {cause, cause_type, effect, effect_type, mechanism, probability, lag_days}
    first_detected  DATE,
    last_scored_at  DATE,
    is_active       BOOLEAN      DEFAULT TRUE,
    country         VARCHAR(10)  DEFAULT 'US',        -- ISO-2 market: US | IN | GB | ...
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_cc_country    ON mg_causal_chains(country);
CREATE INDEX IF NOT EXISTS idx_mg_cc_score      ON mg_causal_chains(activation_score DESC);
CREATE INDEX IF NOT EXISTS idx_mg_cc_active     ON mg_causal_chains(is_active);

-- ============================================================
-- 16. NARRATIVE PROPAGATION  (narrative momentum engine)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_theme_propagation (
    id              BIGSERIAL PRIMARY KEY,
    narrative_slug  VARCHAR(100) NOT NULL,
    narrative_name  TEXT,
    origin_company  TEXT,
    origin_date     DATE,
    propagation_trail JSONB      DEFAULT '[]',
    -- Array of {company, sector, date, signal_type}
    sector_spread   TEXT[],                           -- sectors narrative has reached
    sector_count    INTEGER      DEFAULT 0,
    company_count   INTEGER      DEFAULT 0,
    velocity        FLOAT        DEFAULT 0.0,         -- mentions per 30-day window
    acceleration    FLOAT        DEFAULT 0.0,         -- delta velocity
    diffusion_score FLOAT        DEFAULT 0.0,         -- 0-100
    is_confirmed    BOOLEAN      DEFAULT FALSE,        -- spread to >= 3 sectors
    snapshot_date   DATE         NOT NULL DEFAULT CURRENT_DATE,
    country         VARCHAR(10)  DEFAULT 'US',        -- ISO-2 market: US | IN | GB | ...
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(narrative_slug, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_mg_prop_country  ON mg_theme_propagation(country);

CREATE INDEX IF NOT EXISTS idx_mg_prop_slug     ON mg_theme_propagation(narrative_slug);
CREATE INDEX IF NOT EXISTS idx_mg_prop_diffuse  ON mg_theme_propagation(diffusion_score DESC);
CREATE INDEX IF NOT EXISTS idx_mg_prop_confirm  ON mg_theme_propagation(is_confirmed);
CREATE INDEX IF NOT EXISTS idx_mg_prop_date     ON mg_theme_propagation(snapshot_date DESC);

-- ============================================================
-- ADDITIONAL VIEWS
-- ============================================================

-- Accelerating entities (temporal momentum)
CREATE OR REPLACE VIEW v_accelerating_entities AS
SELECT
    e.canonical_name,
    e.entity_type,
    e.ticker,
    ts.period_date,
    ts.velocity,
    ts.acceleration,
    ts.trend_direction,
    ts.sector_spread
FROM mg_entity_timeseries ts
JOIN mg_entities e ON e.id = ts.entity_id
WHERE ts.trend_direction = 'accelerating'
  AND ts.period_date >= CURRENT_DATE - INTERVAL '90 days'
ORDER BY ts.acceleration DESC;

-- Active causal chains with high activation
CREATE OR REPLACE VIEW v_active_causal_chains AS
SELECT
    chain_id,
    chain_name,
    description,
    depth,
    terminal_effect,
    activation_score,
    last_scored_at
FROM mg_causal_chains
WHERE is_active = TRUE
  AND activation_score > 20.0
ORDER BY activation_score DESC;

-- ============================================================
-- 17. THEME PERFORMANCE  (forward-return validation)
-- ============================================================
-- Records the predicted beneficiary for a theme at detection_date,
-- then tracks the actual forward price return vs benchmark.
-- Filled in by HistoricalRunner after advancing replay_date.
CREATE TABLE IF NOT EXISTS mg_theme_performance (
    id                  BIGSERIAL PRIMARY KEY,
    theme_id            BIGINT       REFERENCES mg_themes(id) ON DELETE CASCADE,
    theme_slug          VARCHAR(100) NOT NULL,
    ticker              VARCHAR(20)  NOT NULL,
    company_name        VARCHAR(200),
    detection_date      DATE         NOT NULL,     -- replay_date when theme was first detected
    detection_score     FLOAT        DEFAULT 0.0,  -- strength_score at detection
    conviction          VARCHAR(30),               -- emerging | developing | confirmed
    -- Forward returns measured from detection_date
    forward_30d_return  FLOAT,                     -- % price return T+30
    forward_90d_return  FLOAT,                     -- % price return T+90
    forward_180d_return FLOAT,                     -- % price return T+180
    forward_365d_return FLOAT,                     -- % price return T+365
    benchmark_30d       FLOAT,                     -- S&P 500 return same window
    benchmark_90d       FLOAT,
    benchmark_180d      FLOAT,
    benchmark_365d      FLOAT,
    alpha_30d           FLOAT GENERATED ALWAYS AS (forward_30d_return - benchmark_30d) STORED,
    alpha_90d           FLOAT GENERATED ALWAYS AS (forward_90d_return - benchmark_90d) STORED,
    alpha_180d          FLOAT GENERATED ALWAYS AS (forward_180d_return - benchmark_180d) STORED,
    alpha_365d          FLOAT GENERATED ALWAYS AS (forward_365d_return - benchmark_365d) STORED,
    measured_at         DATE,                      -- when forward returns were filled
    replay_batch        VARCHAR(20),               -- e.g. "2021-06"
    country             VARCHAR(10)  DEFAULT 'US',   -- ISO-2 market: US | IN | GB | ...
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(theme_slug, ticker, detection_date)
);
CREATE INDEX IF NOT EXISTS idx_mg_perf_country  ON mg_theme_performance(country);

CREATE INDEX IF NOT EXISTS idx_mg_perf_theme    ON mg_theme_performance(theme_slug);
CREATE INDEX IF NOT EXISTS idx_mg_perf_ticker   ON mg_theme_performance(ticker);
CREATE INDEX IF NOT EXISTS idx_mg_perf_detect   ON mg_theme_performance(detection_date);
CREATE INDEX IF NOT EXISTS idx_mg_perf_alpha90  ON mg_theme_performance(alpha_90d DESC NULLS LAST);

-- ============================================================
-- 18. REPLAY RUNS  (historical runner audit log)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_replay_runs (
    id              BIGSERIAL PRIMARY KEY,
    replay_batch    VARCHAR(20)  NOT NULL,          -- "2021-06"
    replay_date     DATE         NOT NULL,          -- end of the replay window
    window_start    DATE         NOT NULL,          -- start of ingest window
    window_end      DATE         NOT NULL,          -- = replay_date
    docs_ingested   INTEGER      DEFAULT 0,
    docs_nlp        INTEGER      DEFAULT 0,
    nodes_built     INTEGER      DEFAULT 0,
    edges_built     INTEGER      DEFAULT 0,
    themes_detected INTEGER      DEFAULT 0,
    themes_snapped  INTEGER      DEFAULT 0,
    events_extracted INTEGER     DEFAULT 0,
    causal_score    FLOAT,                          -- top causal chain activation
    duration_sec    FLOAT,
    status          VARCHAR(20)  DEFAULT 'ok',
    error_message   TEXT,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mg_replay_batch  ON mg_replay_runs(replay_batch);
CREATE INDEX IF NOT EXISTS idx_mg_replay_date   ON mg_replay_runs(replay_date);

-- ============================================================
-- ADDITIONAL VIEWS (continued)
-- ============================================================

-- Theme prediction accuracy leaderboard
CREATE OR REPLACE VIEW v_theme_prediction_accuracy AS
SELECT
    theme_slug,
    COUNT(*) AS predictions,
    ROUND(AVG(alpha_90d)::numeric, 2) AS avg_alpha_90d,
    ROUND(AVG(alpha_180d)::numeric, 2) AS avg_alpha_180d,
    ROUND(AVG(forward_90d_return)::numeric, 2) AS avg_return_90d,
    SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) AS win_count_90d,
    ROUND(
        100.0 * SUM(CASE WHEN alpha_90d > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0),
        1
    ) AS win_rate_90d
FROM mg_theme_performance
WHERE forward_90d_return IS NOT NULL
GROUP BY theme_slug
ORDER BY avg_alpha_90d DESC NULLS LAST;

-- Narrative diffusion leaderboard
CREATE OR REPLACE VIEW v_narrative_diffusion AS
SELECT
    tp.narrative_name,
    tp.origin_company,
    tp.origin_date,
    tp.sector_count,
    tp.company_count,
    tp.velocity,
    tp.acceleration,
    tp.diffusion_score,
    tp.is_confirmed,
    tp.snapshot_date
FROM mg_theme_propagation tp
WHERE tp.snapshot_date = (
    SELECT MAX(snapshot_date) FROM mg_theme_propagation tp2
    WHERE tp2.narrative_slug = tp.narrative_slug
)
ORDER BY tp.diffusion_score DESC;

-- ============================================================
-- MACRO & POLICY DATA LAYER
-- Appended: economic series, commodity prices, policy events
-- ============================================================

-- ============================================================
-- 19. MACRO SERIES  (FRED, World Bank, IMF, ALFRED)
-- ============================================================
-- Stores one row per (series_id, observation_date) data point.
-- series_id follows FRED naming conventions where possible.
CREATE TABLE IF NOT EXISTS mg_macro_series (
    id              BIGSERIAL PRIMARY KEY,
    series_id       VARCHAR(100)  NOT NULL,   -- e.g. GDP, CPIAUCSL, DGS10
    series_name     TEXT          NOT NULL,   -- human label
    source          VARCHAR(50)   NOT NULL,   -- fred | world_bank | imf | alfred
    country         VARCHAR(10)   DEFAULT 'US',  -- ISO-2
    frequency       VARCHAR(20),              -- daily | monthly | quarterly | annual
    units           VARCHAR(100),             -- Billions of Dollars, Percent, Index
    seasonal_adj    VARCHAR(10)   DEFAULT 'SA',  -- SA | NSA | SAAR
    observation_date DATE         NOT NULL,
    value           DOUBLE PRECISION,         -- NULL if revised/withdrawn
    vintage_date    DATE,                     -- ALFRED: when this value was first published
    is_revised      BOOLEAN       DEFAULT FALSE,
    prior_value     DOUBLE PRECISION,         -- value before revision
    revision_pct    DOUBLE PRECISION,         -- (value - prior_value) / |prior_value|
    fetched_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(series_id, observation_date, vintage_date)
);

CREATE INDEX IF NOT EXISTS idx_mg_macro_series_id   ON mg_macro_series(series_id);
CREATE INDEX IF NOT EXISTS idx_mg_macro_obs_date    ON mg_macro_series(observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_mg_macro_source      ON mg_macro_series(source);
CREATE INDEX IF NOT EXISTS idx_mg_macro_country     ON mg_macro_series(country);

-- ============================================================
-- 20. COMMODITY SERIES  (EIA, USDA, Trading Economics)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_commodity_series (
    id              BIGSERIAL PRIMARY KEY,
    commodity_id    VARCHAR(100)  NOT NULL,   -- e.g. WTI_CRUDE, HENRY_HUB, CORN
    commodity_name  TEXT          NOT NULL,
    category        VARCHAR(50)   NOT NULL,   -- energy | agriculture | metals | freight
    source          VARCHAR(50)   NOT NULL,   -- eia | usda | trading_economics | comtrade
    units           VARCHAR(100),
    observation_date DATE         NOT NULL,
    value           DOUBLE PRECISION,
    volume          DOUBLE PRECISION,         -- production/inventory volume (if available)
    inventory_change DOUBLE PRECISION,        -- week-over-week change
    fetched_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(commodity_id, observation_date)
);

CREATE INDEX IF NOT EXISTS idx_mg_comm_id           ON mg_commodity_series(commodity_id);
CREATE INDEX IF NOT EXISTS idx_mg_comm_cat          ON mg_commodity_series(category);
CREATE INDEX IF NOT EXISTS idx_mg_comm_date         ON mg_commodity_series(observation_date DESC);

-- ============================================================
-- 21. POLICY EVENTS  (Congress API, Federal Register)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_policy_events (
    id              BIGSERIAL PRIMARY KEY,
    policy_id       VARCHAR(200)  NOT NULL UNIQUE,  -- source_type::external_id
    source          VARCHAR(50)   NOT NULL,    -- congress | federal_register
    policy_type     VARCHAR(50)   NOT NULL,    -- bill | executive_order | rule | notice | resolution
    title           TEXT          NOT NULL,
    description     TEXT,
    status          VARCHAR(50),               -- introduced | passed_house | passed_senate | enacted | proposed | final
    introduced_date DATE,
    enacted_date    DATE,
    effective_date  DATE,
    sponsor         TEXT,                      -- legislator name or agency
    -- Categorised impact
    sectors_affected TEXT[],                   -- ['Energy', 'Technology', 'Healthcare']
    technologies_affected TEXT[],
    commodities_affected TEXT[],
    impact_direction VARCHAR(20),              -- positive | negative | neutral | mixed
    impact_magnitude FLOAT         DEFAULT 0.0, -- 0-100 estimated economic magnitude
    -- Keyword-driven theme links
    keywords        TEXT[],
    raw_url         TEXT,
    full_text       TEXT,
    country         VARCHAR(10)  DEFAULT 'US',    -- ISO-2 market: US | IN | GB | ...
    fetched_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mg_policy_country   ON mg_policy_events(country);

CREATE INDEX IF NOT EXISTS idx_mg_policy_source     ON mg_policy_events(source);
CREATE INDEX IF NOT EXISTS idx_mg_policy_type       ON mg_policy_events(policy_type);
CREATE INDEX IF NOT EXISTS idx_mg_policy_enacted    ON mg_policy_events(enacted_date DESC);
CREATE INDEX IF NOT EXISTS idx_mg_policy_impact     ON mg_policy_events(impact_direction);
CREATE INDEX IF NOT EXISTS idx_mg_policy_sectors    ON mg_policy_events USING gin(sectors_affected);
CREATE INDEX IF NOT EXISTS idx_mg_policy_techs      ON mg_policy_events USING gin(technologies_affected);

-- ============================================================
-- 22. MACRO EVENTS  (significant threshold crossings)
-- ============================================================
-- Emitted automatically when a macro series crosses a key level.
-- Feeds the Constraint Engine exactly like signals from SEC filings.
CREATE TABLE IF NOT EXISTS mg_macro_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      VARCHAR(80)   NOT NULL,    -- recession | rate_hike | inflation_spike | yield_inversion | credit_tightening | commodity_shock
    series_id       VARCHAR(100),              -- triggering macro series
    commodity_id    VARCHAR(100),              -- triggering commodity (if any)
    policy_id       VARCHAR(200),              -- triggering policy event (if any)
    event_date      DATE          NOT NULL,
    description     TEXT          NOT NULL,
    severity        FLOAT         DEFAULT 0.0, -- 0-100
    direction       VARCHAR(20),               -- tightening | easing | rising | falling | inverted
    -- Threshold details
    threshold_value DOUBLE PRECISION,
    observed_value  DOUBLE PRECISION,
    prior_value     DOUBLE PRECISION,
    change_pct      DOUBLE PRECISION,
    -- Sector/company impact assessment
    sectors_at_risk TEXT[],
    sectors_benefit TEXT[],
    themes_triggered TEXT[],                   -- theme slugs this event corroborates
    -- Replay correctness: only events known at replay_date are applied
    replay_safe_date DATE,                     -- = event_date (no forward leakage)
    country         VARCHAR(10)  DEFAULT 'US',    -- ISO-2 market: US | IN | GB | ...
    fetched_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(event_type, series_id, event_date)
);
CREATE INDEX IF NOT EXISTS idx_mg_mev_country       ON mg_macro_events(country);

CREATE INDEX IF NOT EXISTS idx_mg_mev_type          ON mg_macro_events(event_type);
CREATE INDEX IF NOT EXISTS idx_mg_mev_date          ON mg_macro_events(event_date DESC);
CREATE INDEX IF NOT EXISTS idx_mg_mev_severity      ON mg_macro_events(severity DESC);
CREATE INDEX IF NOT EXISTS idx_mg_mev_themes        ON mg_macro_events USING gin(themes_triggered);

-- ============================================================
-- 23. TRADE FLOWS  (UN Comtrade, World Bank)
-- ============================================================
CREATE TABLE IF NOT EXISTS mg_trade_flows (
    id              BIGSERIAL PRIMARY KEY,
    reporter_country VARCHAR(10)  NOT NULL,    -- ISO-2 exporter/importer
    partner_country  VARCHAR(10),              -- ISO-2 trade partner
    hs_code         VARCHAR(20),               -- Harmonised System product code
    product_name    TEXT,
    flow_direction  VARCHAR(10)   NOT NULL,    -- export | import
    year            INTEGER       NOT NULL,
    period          VARCHAR(20),               -- 2022 | 2022-Q1 | 2022-01
    value_usd       DOUBLE PRECISION,          -- trade value in USD
    quantity        DOUBLE PRECISION,
    quantity_unit   VARCHAR(50),
    source          VARCHAR(50)   DEFAULT 'comtrade',
    fetched_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(reporter_country, partner_country, hs_code, flow_direction, period)
);

CREATE INDEX IF NOT EXISTS idx_mg_trade_reporter    ON mg_trade_flows(reporter_country);
CREATE INDEX IF NOT EXISTS idx_mg_trade_hs          ON mg_trade_flows(hs_code);
CREATE INDEX IF NOT EXISTS idx_mg_trade_year        ON mg_trade_flows(year DESC);

-- ============================================================
-- 24. MACRO-THEME LINKS  (constraint engine output)
-- ============================================================
-- Each row = one macro signal corroborating (or constraining) a theme
CREATE TABLE IF NOT EXISTS mg_macro_theme_links (
    id              BIGSERIAL PRIMARY KEY,
    theme_slug      VARCHAR(200)  NOT NULL,
    link_type       VARCHAR(50)   NOT NULL,    -- corroborates | constrains | amplifies | reduces
    macro_event_id  BIGINT        REFERENCES mg_macro_events(id) ON DELETE SET NULL,
    policy_event_id BIGINT        REFERENCES mg_policy_events(id) ON DELETE SET NULL,
    series_id       VARCHAR(100),
    commodity_id    VARCHAR(100),
    evidence_text   TEXT,
    strength        FLOAT         DEFAULT 0.0, -- 0-100
    as_of_date      DATE          NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(theme_slug, link_type, macro_event_id, policy_event_id, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_mg_mtl_theme         ON mg_macro_theme_links(theme_slug);
CREATE INDEX IF NOT EXISTS idx_mg_mtl_type          ON mg_macro_theme_links(link_type);
CREATE INDEX IF NOT EXISTS idx_mg_mtl_date          ON mg_macro_theme_links(as_of_date DESC);

-- ============================================================
-- VIEWS: macro dashboard helpers
-- ============================================================

CREATE OR REPLACE VIEW v_macro_dashboard AS
SELECT
    ms.series_id,
    ms.series_name,
    ms.source,
    ms.country,
    ms.units,
    ms.observation_date,
    ms.value,
    LAG(ms.value) OVER (PARTITION BY ms.series_id ORDER BY ms.observation_date) AS prior_value,
    ROUND(
        (100.0 * (ms.value - LAG(ms.value) OVER (PARTITION BY ms.series_id ORDER BY ms.observation_date))
        / NULLIF(ABS(LAG(ms.value) OVER (PARTITION BY ms.series_id ORDER BY ms.observation_date)), 0))::numeric,
        2
    ) AS pct_change
FROM mg_macro_series ms
WHERE ms.observation_date >= CURRENT_DATE - INTERVAL '5 years'
ORDER BY ms.series_id, ms.observation_date DESC;

CREATE OR REPLACE VIEW v_recent_policy_events AS
SELECT
    policy_id,
    source,
    policy_type,
    title,
    status,
    introduced_date,
    enacted_date,
    impact_direction,
    impact_magnitude,
    sectors_affected
FROM mg_policy_events
WHERE COALESCE(enacted_date, introduced_date) >= CURRENT_DATE - INTERVAL '2 years'
ORDER BY COALESCE(enacted_date, introduced_date) DESC;

CREATE OR REPLACE VIEW v_active_macro_constraints AS
SELECT
    mtl.theme_slug,
    mtl.link_type,
    mtl.strength,
    mtl.as_of_date,
    me.event_type,
    me.description     AS macro_description,
    me.severity,
    pe.title           AS policy_title,
    pe.policy_type,
    pe.impact_direction
FROM mg_macro_theme_links mtl
LEFT JOIN mg_macro_events  me ON me.id  = mtl.macro_event_id
LEFT JOIN mg_policy_events pe ON pe.id  = mtl.policy_event_id
WHERE mtl.as_of_date >= CURRENT_DATE - INTERVAL '180 days'
ORDER BY mtl.strength DESC;

-- ============================================================
-- 25. DATED CONSTRAINT LEDGER  (point-in-time investment research)
-- ============================================================
-- The beneficiary mapper is an opportunity-discovery system.  This ledger is
-- the separate, auditable record of what was actually known about a shortage
-- or supply-chain constraint on a given date.  Its evidence is intentionally
-- typed and dated so broad policy context cannot be mistaken for an exact,
-- current, investible physical constraint.
CREATE TABLE IF NOT EXISTS mg_constraint_ledgers (
    id                      BIGSERIAL PRIMARY KEY,
    country                 VARCHAR(10) NOT NULL DEFAULT 'IN',
    constraint_key          VARCHAR(160) NOT NULL,
    constraint_name         TEXT NOT NULL,
    as_of_date              DATE NOT NULL,
    state                   VARCHAR(24) NOT NULL,
    classification          VARCHAR(32) NOT NULL,
    product_scope           VARCHAR(24) NOT NULL DEFAULT 'EXACT_CHAIN',
    investment_eligibility  VARCHAR(24) NOT NULL DEFAULT 'RESEARCH_ONLY',
    import_dependency_ratio NUMERIC(9, 6),
    measurement_date        DATE,
    import_value            NUMERIC,
    import_value_unit       TEXT,
    primary_origin          VARCHAR(120),
    primary_origin_ratio    NUMERIC(9, 6),
    capacity_gap_ratio      NUMERIC(9, 6),
    domestic_capacity       NUMERIC,
    capacity_unit           TEXT,
    demand_volume           NUMERIC,
    demand_unit             TEXT,
    binding_demand_status   VARCHAR(24) NOT NULL DEFAULT 'UNPROVED',
    resupply_barrier_status VARCHAR(24) NOT NULL DEFAULT 'UNPROVED',
    resolution_status       VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN',
    next_validation_date    DATE,
    summary                 TEXT NOT NULL,
    review_note             TEXT,
    physical_state          VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN',
    trajectory              VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN',
    evidence_completeness   VARCHAR(24) NOT NULL DEFAULT 'UNMEASURED',
    mechanism               VARCHAR(40) NOT NULL DEFAULT 'UNCLASSIFIED_RESEARCH',
    derivation_method       VARCHAR(64) NOT NULL DEFAULT 'REVIEWED_PACKET',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, constraint_key, as_of_date),
    CHECK (state IN ('DISCOVERY', 'EVIDENCED', 'MEASURED', 'BINDING',
                     'INVESTIBLE', 'RESOLVING', 'RESOLVED', 'OVERCAPACITY',
                     'STALE', 'REJECTED')),
    CHECK (classification IN ('PHYSICAL_CONSTRAINT', 'POLICY_PROCUREMENT',
                              'DEMAND_THEME', 'WATCH')),
    CHECK (product_scope IN ('EXACT_CHAIN', 'FAMILY', 'BROAD', 'POLICY')),
    CHECK (investment_eligibility IN ('RESEARCH_ONLY', 'EARLY_ELIGIBLE',
                                      'CORE_ELIGIBLE', 'EXCLUDED')),
    CHECK (binding_demand_status IN ('UNPROVED', 'INDICATED', 'CONFIRMED')),
    CHECK (resupply_barrier_status IN ('UNPROVED', 'INDICATED', 'CONFIRMED')),
    CHECK (resolution_status IN ('UNKNOWN', 'STILL_BINDING', 'RESOLVING',
                                 'RESOLVED', 'REJECTED')),
    CHECK (import_dependency_ratio IS NULL OR
           import_dependency_ratio BETWEEN 0 AND 1),
    CHECK (primary_origin_ratio IS NULL OR
           primary_origin_ratio BETWEEN 0 AND 1),
    CHECK (capacity_gap_ratio IS NULL OR capacity_gap_ratio BETWEEN 0 AND 1)
);
-- Keep the live database compatible when it was created before the richer
-- import-origin and demand fields were added to the ledger definition.
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS import_value NUMERIC;
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS measurement_date DATE;
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS import_value_unit TEXT;
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS primary_origin VARCHAR(120);
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS primary_origin_ratio NUMERIC(9, 6);
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS demand_volume NUMERIC;
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS demand_unit TEXT;
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS physical_state VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS trajectory VARCHAR(24) NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS evidence_completeness VARCHAR(24) NOT NULL DEFAULT 'UNMEASURED';
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS mechanism VARCHAR(40) NOT NULL DEFAULT 'UNCLASSIFIED_RESEARCH';
ALTER TABLE mg_constraint_ledgers ADD COLUMN IF NOT EXISTS derivation_method VARCHAR(64) NOT NULL DEFAULT 'REVIEWED_PACKET';
ALTER TABLE mg_constraint_ledgers ALTER COLUMN import_value_unit TYPE TEXT;
ALTER TABLE mg_constraint_ledgers ALTER COLUMN capacity_unit TYPE TEXT;
ALTER TABLE mg_constraint_ledgers ALTER COLUMN demand_unit TYPE TEXT;
ALTER TABLE mg_constraint_ledgers DROP CONSTRAINT IF EXISTS mg_constraint_ledgers_state_check;
ALTER TABLE mg_constraint_ledgers ADD CONSTRAINT mg_constraint_ledgers_state_check
    CHECK (state IN ('DISCOVERY', 'EVIDENCED', 'MEASURED', 'BINDING',
                     'INVESTIBLE', 'RESOLVING', 'RESOLVED', 'OVERCAPACITY',
                     'STALE', 'REJECTED'));
CREATE INDEX IF NOT EXISTS idx_constraint_ledgers_asof
    ON mg_constraint_ledgers(country, constraint_key, as_of_date DESC);
CREATE INDEX IF NOT EXISTS idx_constraint_ledgers_state
    ON mg_constraint_ledgers(country, classification, state, as_of_date DESC);

CREATE TABLE IF NOT EXISTS mg_constraint_ledger_evidence (
    id                  BIGSERIAL PRIMARY KEY,
    ledger_id           BIGINT NOT NULL REFERENCES mg_constraint_ledgers(id)
                        ON DELETE CASCADE,
    evidence_type       VARCHAR(32) NOT NULL,
    source_date         DATE NOT NULL,
    source_url          TEXT NOT NULL,
    source_title        TEXT NOT NULL,
    source_publisher    TEXT,
    claim               TEXT NOT NULL,
    value_numeric       NUMERIC,
    value_unit          VARCHAR(64),
    is_primary          BOOLEAN NOT NULL DEFAULT TRUE,
    independence_key    VARCHAR(200),
    published_at        DATE,
    available_at        DATE,
    source_hash         VARCHAR(64),
    admissibility_status VARCHAR(24) NOT NULL DEFAULT 'ADMISSIBLE',
    quarantine_reason   TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(ledger_id, evidence_type, source_date, source_url),
    CHECK (evidence_type IN ('DEMAND', 'SUPPLY', 'IMPORT', 'BARRIER',
                             'BINDING', 'RESOLUTION', 'POLICY'))
);
CREATE INDEX IF NOT EXISTS idx_constraint_ledger_evidence_ledger
    ON mg_constraint_ledger_evidence(ledger_id, source_date DESC);
ALTER TABLE mg_constraint_ledger_evidence ADD COLUMN IF NOT EXISTS published_at DATE;
ALTER TABLE mg_constraint_ledger_evidence ADD COLUMN IF NOT EXISTS available_at DATE;
ALTER TABLE mg_constraint_ledger_evidence ADD COLUMN IF NOT EXISTS source_hash VARCHAR(64);
ALTER TABLE mg_constraint_ledger_evidence ADD COLUMN IF NOT EXISTS admissibility_status VARCHAR(24) NOT NULL DEFAULT 'ADMISSIBLE';
ALTER TABLE mg_constraint_ledger_evidence ADD COLUMN IF NOT EXISTS quarantine_reason TEXT;

-- A source discovered after a historical snapshot is not admissible for that
-- snapshot.  The trigger makes that point-in-time rule a database invariant,
-- instead of a convention in the report renderer.
CREATE OR REPLACE FUNCTION mg_constraint_ledger_evidence_asof_guard()
RETURNS TRIGGER AS $$
DECLARE
    snapshot_date DATE;
BEGIN
    SELECT as_of_date INTO snapshot_date
    FROM mg_constraint_ledgers
    WHERE id = NEW.ledger_id;
    IF snapshot_date IS NULL THEN
        RAISE EXCEPTION 'constraint ledger % does not exist', NEW.ledger_id;
    END IF;
    IF NEW.source_date > snapshot_date THEN
        RAISE EXCEPTION
            'evidence date % is after constraint snapshot %',
            NEW.source_date, snapshot_date;
    END IF;
    IF COALESCE(NEW.published_at, NEW.source_date) > snapshot_date OR
       COALESCE(NEW.available_at, NEW.published_at, NEW.source_date) > snapshot_date THEN
        RAISE EXCEPTION
            'evidence was published/available after constraint snapshot %', snapshot_date;
    END IF;
    IF LOWER(NEW.source_url) LIKE 'scheme:%' AND NEW.evidence_type <> 'POLICY' THEN
        RAISE EXCEPTION
            'scheme evidence may be POLICY only, not physical evidence type %', NEW.evidence_type;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_constraint_ledger_evidence_asof_guard
    ON mg_constraint_ledger_evidence;
CREATE TRIGGER trg_constraint_ledger_evidence_asof_guard
BEFORE INSERT OR UPDATE OF ledger_id, source_date, published_at, available_at,
                           source_url, evidence_type ON mg_constraint_ledger_evidence
FOR EACH ROW EXECUTE FUNCTION mg_constraint_ledger_evidence_asof_guard();

-- ============================================================
-- 26. CONSTRAINT COVERAGE, PRODUCT ALIASES, AND EVIDENCE QUEUE
-- ============================================================
-- Product labels are operational data, not selector code.  A reviewed alias
-- connects a mapper label to one constraint chain; an automatically discovered
-- alias establishes coverage only and must be reviewed before it can attach to
-- a physical-chain ledger record.
CREATE TABLE IF NOT EXISTS mg_constraint_product_aliases (
    id                  BIGSERIAL PRIMARY KEY,
    country             VARCHAR(10) NOT NULL DEFAULT 'IN',
    product_label       TEXT NOT NULL,
    normalized_label    TEXT NOT NULL,
    constraint_key      VARCHAR(160) NOT NULL,
    match_scope         VARCHAR(16) NOT NULL DEFAULT 'BROAD',
    status              VARCHAR(24) NOT NULL DEFAULT 'AUTO_DISCOVERY',
    first_seen_date     DATE,
    last_seen_date      DATE,
    source              TEXT NOT NULL DEFAULT 'constraint coverage bootstrap',
    review_note         TEXT,
    effective_from      DATE,
    effective_to        DATE,
    provenance_url      TEXT,
    reviewed_at         DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, normalized_label),
    CHECK (match_scope IN ('EXACT', 'FAMILY', 'BROAD', 'POLICY')),
    CHECK (status IN ('AUTO_DISCOVERY', 'REVIEWED', 'REJECTED'))
);
CREATE INDEX IF NOT EXISTS idx_constraint_product_aliases_chain
    ON mg_constraint_product_aliases(country, constraint_key, status);
ALTER TABLE mg_constraint_product_aliases ADD COLUMN IF NOT EXISTS effective_from DATE;
ALTER TABLE mg_constraint_product_aliases ADD COLUMN IF NOT EXISTS effective_to DATE;
ALTER TABLE mg_constraint_product_aliases ADD COLUMN IF NOT EXISTS provenance_url TEXT;
ALTER TABLE mg_constraint_product_aliases ADD COLUMN IF NOT EXISTS reviewed_at DATE;

CREATE TABLE IF NOT EXISTS mg_constraint_product_alias_versions (
    id                  BIGSERIAL PRIMARY KEY,
    country             VARCHAR(10) NOT NULL DEFAULT 'IN',
    product_label       TEXT NOT NULL,
    normalized_label    TEXT NOT NULL,
    constraint_key      VARCHAR(160) NOT NULL,
    match_scope         VARCHAR(16) NOT NULL,
    status              VARCHAR(24) NOT NULL,
    effective_from      DATE NOT NULL,
    effective_to        DATE,
    provenance_url      TEXT,
    source              TEXT NOT NULL,
    review_note         TEXT,
    reviewed_at         DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, normalized_label, effective_from),
    CHECK (match_scope IN ('EXACT', 'FAMILY', 'BROAD', 'POLICY')),
    CHECK (status IN ('AUTO_DISCOVERY', 'REVIEWED', 'REJECTED')),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);
CREATE INDEX IF NOT EXISTS idx_constraint_alias_versions_asof
    ON mg_constraint_product_alias_versions(country, normalized_label,
                                             effective_from, effective_to);

-- Automated collectors put source rows here first.  A queued row is neither
-- ledger evidence nor a Buy-gate leg: it needs a dated, reviewable source
-- before an analyst promotes it into mg_constraint_ledger_evidence.
CREATE TABLE IF NOT EXISTS mg_constraint_observation_queue (
    id                  BIGSERIAL PRIMARY KEY,
    country             VARCHAR(10) NOT NULL DEFAULT 'IN',
    constraint_key      VARCHAR(160) NOT NULL,
    product_label       TEXT NOT NULL,
    observation_type    VARCHAR(32) NOT NULL,
    observed_at         DATE NOT NULL,
    source_table        VARCHAR(80) NOT NULL,
    source_row_id       BIGINT,
    source_key          TEXT NOT NULL,
    source_url          TEXT,
    source_title        TEXT,
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance_status   VARCHAR(24) NOT NULL DEFAULT 'PENDING_SOURCE',
    review_status       VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    review_note         TEXT,
    economic_period_start DATE,
    economic_period_end DATE,
    published_at        DATE,
    available_at        DATE,
    source_family       TEXT,
    source_hash         VARCHAR(64),
    product_scope       VARCHAR(16) NOT NULL DEFAULT 'EXACT',
    revision            INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, constraint_key, observation_type, source_key),
    CHECK (observation_type IN ('CAPACITY', 'IMPORT', 'DEMAND', 'TRADE_FLOW',
                                'LEAD_TIME', 'COMMISSIONING')),
    CHECK (provenance_status IN ('PENDING_SOURCE', 'PRIMARY_SOURCE', 'REJECTED')),
    CHECK (review_status IN ('PENDING', 'ACCEPTED', 'REJECTED'))
);
CREATE INDEX IF NOT EXISTS idx_constraint_observation_queue_review
    ON mg_constraint_observation_queue(country, constraint_key, review_status,
                                       observed_at DESC);
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS economic_period_start DATE;
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS economic_period_end DATE;
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS published_at DATE;
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS available_at DATE;
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS source_family TEXT;
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS source_hash VARCHAR(64);
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS product_scope VARCHAR(16) NOT NULL DEFAULT 'EXACT';
ALTER TABLE mg_constraint_observation_queue ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;

-- Backfill availability only where the original source table preserves it.
-- Accepted legacy rows without a recoverable date are demoted to review rather
-- than guessed into historical availability.
UPDATE mg_constraint_observation_queue q
SET published_at=d.filed_at::date,
    available_at=d.filed_at::date,
    source_family=COALESCE(q.source_family,
                           CONCAT('issuer:', UPPER(TRIM(d.ticker)), ':', d.filed_at::date)),
    source_hash=COALESCE(q.source_hash, d.content_hash)
FROM mg_documents d
WHERE q.source_table='mg_documents' AND q.source_row_id=d.id
  AND (q.published_at IS NULL OR q.available_at IS NULL);

DO $$ BEGIN
    IF to_regclass('public.mg_capacity_gaps') IS NOT NULL THEN
        UPDATE mg_constraint_observation_queue q
        SET published_at=c.source_published_at,
            available_at=c.source_published_at,
            source_family=COALESCE(q.source_family, c.source_family, c.source_url)
        FROM mg_capacity_gaps c
        WHERE q.source_table='mg_capacity_gaps' AND q.source_row_id=c.id
          AND c.source_published_at IS NOT NULL
          AND (q.published_at IS NULL OR q.available_at IS NULL);
    END IF;
    IF to_regclass('public.mg_import_dependencies') IS NOT NULL THEN
        UPDATE mg_constraint_observation_queue q
        SET published_at=i.source_published_at,
            available_at=i.source_published_at,
            source_family=COALESCE(q.source_family, i.source_family, i.source_url)
        FROM mg_import_dependencies i
        WHERE q.source_table='mg_import_dependencies' AND q.source_row_id=i.id
          AND i.source_published_at IS NOT NULL
          AND (q.published_at IS NULL OR q.available_at IS NULL);
    END IF;
END $$;

-- Make old evidence-free auto-coverage rows explicitly replaceable by the
-- event materializer. Analyst-reviewed/nonempty ledgers are untouched.
UPDATE mg_constraint_ledgers l
SET derivation_method='COVERAGE_PLACEHOLDER',
    physical_state=COALESCE(physical_state, 'DISCOVERY'),
    trajectory=COALESCE(trajectory, 'STABLE_OR_UNKNOWN'),
    evidence_completeness=COALESCE(evidence_completeness, 'UNMEASURED'),
    mechanism=COALESCE(mechanism, 'UNCLASSIFIED_RESEARCH')
WHERE l.state='DISCOVERY' AND l.classification='WATCH'
  AND l.derivation_method IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM mg_constraint_ledger_evidence e
      WHERE e.ledger_id=l.id AND e.is_primary
        AND COALESCE(e.admissibility_status, 'ADMISSIBLE')='ADMISSIBLE'
  );

UPDATE mg_constraint_observation_queue
SET review_status='PENDING', provenance_status='PENDING_SOURCE',
    review_note=CONCAT_WS(' ', review_note,
        'Legacy acceptance demoted: publication/availability date was not recoverable.')
WHERE review_status='ACCEPTED'
  AND (published_at IS NULL OR available_at IS NULL OR source_url IS NULL);

-- Accepted source observations are immutable economic events. A later source
-- revision must be inserted with a new source_key/revision rather than silently
-- changing what a historical report knew.
CREATE OR REPLACE FUNCTION mg_constraint_observation_immutable_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.review_status = 'ACCEPTED' AND (
        NEW.constraint_key, NEW.product_label, NEW.observation_type,
        NEW.observed_at, NEW.source_key, NEW.source_url, NEW.metrics,
        NEW.published_at, NEW.available_at, NEW.source_hash,
        NEW.product_scope, NEW.provenance_status
    ) IS DISTINCT FROM (
        OLD.constraint_key, OLD.product_label, OLD.observation_type,
        OLD.observed_at, OLD.source_key, OLD.source_url, OLD.metrics,
        OLD.published_at, OLD.available_at, OLD.source_hash,
        OLD.product_scope, OLD.provenance_status
    ) THEN
        RAISE EXCEPTION 'accepted constraint observation % is immutable; insert a revision', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_constraint_observation_immutable
    ON mg_constraint_observation_queue;
CREATE TRIGGER trg_constraint_observation_immutable
BEFORE UPDATE ON mg_constraint_observation_queue
FOR EACH ROW EXECUTE FUNCTION mg_constraint_observation_immutable_guard();

CREATE OR REPLACE FUNCTION mg_constraint_observation_acceptance_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.review_status = 'ACCEPTED' THEN
        IF NEW.provenance_status <> 'PRIMARY_SOURCE' OR NEW.source_url IS NULL OR
           NEW.published_at IS NULL OR NEW.available_at IS NULL THEN
            RAISE EXCEPTION 'accepted observation requires primary URL, published_at and available_at';
        END IF;
        IF NEW.available_at < NEW.published_at OR NEW.available_at < NEW.observed_at THEN
            RAISE EXCEPTION 'available_at cannot precede publication/economic observation';
        END IF;
        IF NEW.product_scope <> 'EXACT' THEN
            RAISE EXCEPTION 'accepted constraint observation must be exact-product scoped';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_constraint_observation_acceptance
    ON mg_constraint_observation_queue;
CREATE TRIGGER trg_constraint_observation_acceptance
BEFORE INSERT OR UPDATE OF review_status, provenance_status, source_url,
                           published_at, available_at, product_scope
ON mg_constraint_observation_queue
FOR EACH ROW EXECUTE FUNCTION mg_constraint_observation_acceptance_guard();

-- The evidence table links back to the immutable event that justified it.
ALTER TABLE mg_constraint_ledger_evidence ADD COLUMN IF NOT EXISTS observation_id BIGINT;
DO $$ BEGIN
    ALTER TABLE mg_constraint_ledger_evidence
      ADD CONSTRAINT fk_constraint_evidence_observation
      FOREIGN KEY (observation_id) REFERENCES mg_constraint_observation_queue(id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Reviewed, effective-dated product/HS relationships. Trade rows outside the
-- effective window or without a reviewed crosswalk remain momentum context.
CREATE TABLE IF NOT EXISTS mg_product_hs_crosswalks (
    id                  BIGSERIAL PRIMARY KEY,
    country             VARCHAR(10) NOT NULL DEFAULT 'IN',
    normalized_product  TEXT NOT NULL,
    hs_code             VARCHAR(16) NOT NULL,
    relationship_scope  VARCHAR(24) NOT NULL DEFAULT 'EXACT',
    effective_from      DATE NOT NULL,
    effective_to        DATE,
    review_status       VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    source_url          TEXT,
    review_note         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, normalized_product, hs_code, effective_from),
    CHECK (relationship_scope IN ('EXACT', 'FAMILY', 'BROAD')),
    CHECK (review_status IN ('PENDING', 'REVIEWED', 'REJECTED')),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);
CREATE INDEX IF NOT EXISTS idx_product_hs_crosswalk_asof
    ON mg_product_hs_crosswalks(country, hs_code, effective_from, effective_to);

-- Non-destructive audit trail for evidence removed from decision authority.
CREATE TABLE IF NOT EXISTS mg_constraint_evidence_quarantine (
    evidence_id         BIGINT PRIMARY KEY,
    ledger_id           BIGINT NOT NULL,
    quarantined_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason              TEXT NOT NULL,
    original_payload    JSONB NOT NULL
);

-- Optional official total-return benchmark feed. Forward tests must state a
-- visible fallback when this table lacks the requested point-in-time series.
CREATE TABLE IF NOT EXISTS mg_benchmark_prices (
    country             VARCHAR(10) NOT NULL,
    benchmark_key       VARCHAR(80) NOT NULL,
    price_date          DATE NOT NULL,
    total_return_index  NUMERIC NOT NULL,
    source_url          TEXT NOT NULL,
    available_at        DATE NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(country, benchmark_key, price_date),
    CHECK (available_at >= price_date)
);

-- Legacy India reference tables are created by their respective engines.
-- When present, every decision-grade physical row must carry auditable
-- point-in-time provenance. Static engineering estimates remain useful
-- discovery context but default to PENDING_SOURCE and cannot satisfy a
-- measured constraint gate.
ALTER TABLE IF EXISTS mg_capacity_gaps
  ADD COLUMN IF NOT EXISTS source_url TEXT,
  ADD COLUMN IF NOT EXISTS source_title TEXT,
  ADD COLUMN IF NOT EXISTS source_published_at DATE,
  ADD COLUMN IF NOT EXISTS source_family TEXT,
  ADD COLUMN IF NOT EXISTS provenance_status TEXT DEFAULT 'PENDING_SOURCE',
  ADD COLUMN IF NOT EXISTS ingestion_method TEXT,
  ADD COLUMN IF NOT EXISTS measurement_basis TEXT DEFAULT 'UNKNOWN';

ALTER TABLE IF EXISTS mg_import_dependencies
  ADD COLUMN IF NOT EXISTS source_url TEXT,
  ADD COLUMN IF NOT EXISTS source_title TEXT,
  ADD COLUMN IF NOT EXISTS source_published_at DATE,
  ADD COLUMN IF NOT EXISTS source_family TEXT,
  ADD COLUMN IF NOT EXISTS provenance_status TEXT DEFAULT 'PENDING_SOURCE',
  ADD COLUMN IF NOT EXISTS ingestion_method TEXT,
  ADD COLUMN IF NOT EXISTS measurement_basis TEXT DEFAULT 'UNKNOWN';

-- Existing estimates predate the semantic split.  Preserve their values for
-- research display, but label them explicitly so no downstream gate can treat
-- an unknown/current-looking percentage as a measured physical constraint.
DO $$ BEGIN
    IF to_regclass('public.mg_capacity_gaps') IS NOT NULL THEN
        UPDATE mg_capacity_gaps
        SET measurement_basis='STATIC_REFERENCE_CONTEXT'
        WHERE measurement_basis IS NULL OR measurement_basis='UNKNOWN';
    END IF;
    IF to_regclass('public.mg_import_dependencies') IS NOT NULL THEN
        UPDATE mg_import_dependencies
        SET measurement_basis='STATIC_REFERENCE_CONTEXT'
        WHERE measurement_basis IS NULL OR measurement_basis='UNKNOWN';
    END IF;
END $$;

-- Company filings are the source of truth for what a listed company actually
-- makes, installs, or integrates.  Keep this product-role layer separate from
-- the constraint ledger: a direct company product disclosure can discover a
-- new product theme, but it does not establish that the product is scarce.
CREATE TABLE IF NOT EXISTS mg_company_product_roles (
    id                          BIGSERIAL PRIMARY KEY,
    country                     VARCHAR(10) NOT NULL DEFAULT 'IN',
    as_of_date                  DATE NOT NULL,
    ticker                      VARCHAR(32) NOT NULL,
    company                     TEXT,
    product_phrase              TEXT NOT NULL,
    normalized_product           TEXT NOT NULL,
    role_type                   VARCHAR(32) NOT NULL,
    role_state                  VARCHAR(24) NOT NULL DEFAULT 'DISCOVERY',
    constraint_key              VARCHAR(160),
    constraint_link_type        VARCHAR(24) NOT NULL DEFAULT 'UNLINKED',
    first_evidence_date         DATE NOT NULL,
    last_evidence_date          DATE NOT NULL,
    independent_document_count  INTEGER NOT NULL DEFAULT 0,
    physical_evidence_count     INTEGER NOT NULL DEFAULT 0,
    pipeline_evidence_count     INTEGER NOT NULL DEFAULT 0,
    earnings_capture_count      INTEGER NOT NULL DEFAULT 0,
    demand_or_policy_count      INTEGER NOT NULL DEFAULT 0,
    evidence                    JSONB NOT NULL DEFAULT '[]'::jsonb,
    extraction_method           VARCHAR(64) NOT NULL,
    review_status               VARCHAR(24) NOT NULL DEFAULT 'AUTO_DISCOVERY',
    review_note                 TEXT,
    adjudication_state          VARCHAR(48) NOT NULL DEFAULT 'QUARANTINED_AMBIGUOUS_ROLE',
    adjudication_reason         TEXT,
    missing_evidence            JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, as_of_date, ticker, normalized_product, role_type),
    CHECK (role_type IN ('MANUFACTURER', 'DIRECT_PRODUCT_SUPPLIER',
                         'EPC_OR_INSTALLER', 'SYSTEM_INTEGRATOR',
                         'INPUT_SUPPLIER', 'DIRECT_ROLE_UNCLASSIFIED')),
    CHECK (role_state IN ('DISCOVERY', 'EVIDENCED', 'REJECTED')),
    CHECK (constraint_link_type IN ('EXACT', 'EXACT_PRODUCT',
                                    'REVIEWED_ADJACENT', 'UNLINKED')),
    CHECK (review_status IN ('AUTO_DISCOVERY', 'REVIEWED', 'REJECTED')),
    CHECK (first_evidence_date <= last_evidence_date),
    CHECK (last_evidence_date <= as_of_date)
);
-- Idempotent migration for installations created before automatic company-role
-- adjudication was introduced.
ALTER TABLE mg_company_product_roles
    ADD COLUMN IF NOT EXISTS pipeline_evidence_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS earnings_capture_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS adjudication_state VARCHAR(48) NOT NULL DEFAULT 'QUARANTINED_AMBIGUOUS_ROLE',
    ADD COLUMN IF NOT EXISTS adjudication_reason TEXT,
    ADD COLUMN IF NOT EXISTS missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb;
-- PostgreSQL names the inline role check deterministically.  Replace the old
-- constraint so existing installations can persist the separately identified
-- direct commercial-supplier role without weakening any other role state.
ALTER TABLE mg_company_product_roles
    DROP CONSTRAINT IF EXISTS mg_company_product_roles_role_type_check;
ALTER TABLE mg_company_product_roles
    ADD CONSTRAINT mg_company_product_roles_role_type_check
    CHECK (role_type IN ('MANUFACTURER', 'DIRECT_PRODUCT_SUPPLIER',
                         'EPC_OR_INSTALLER', 'SYSTEM_INTEGRATOR',
                         'INPUT_SUPPLIER', 'DIRECT_ROLE_UNCLASSIFIED'));
-- Exact issuer-product identity and exact product-to-theme identity are
-- separate judgments.  ``EXACT_PRODUCT`` preserves the former when the
-- reviewed economic-theme alias is family-level or broad.
ALTER TABLE mg_company_product_roles
    DROP CONSTRAINT IF EXISTS mg_company_product_roles_constraint_link_type_check;
ALTER TABLE mg_company_product_roles
    ADD CONSTRAINT mg_company_product_roles_constraint_link_type_check
    CHECK (constraint_link_type IN ('EXACT', 'EXACT_PRODUCT',
                                    'REVIEWED_ADJACENT', 'UNLINKED'));
CREATE INDEX IF NOT EXISTS idx_company_product_roles_asof
    ON mg_company_product_roles(country, as_of_date DESC, normalized_product);
CREATE INDEX IF NOT EXISTS idx_company_product_roles_constraint
    ON mg_company_product_roles(country, constraint_key, as_of_date DESC);

-- A review is intentionally separate from the derived company-role snapshot.
-- Rebuilding an extraction snapshot must never overwrite an analyst decision,
-- and a decision made today must never appear in a historical report.  The
-- ``decision_available_from`` field is therefore the point-in-time guard for
-- company role approval, just as source_date is for ledger evidence.
CREATE TABLE IF NOT EXISTS mg_company_role_reviews (
    id                          BIGSERIAL PRIMARY KEY,
    country                     VARCHAR(10) NOT NULL DEFAULT 'IN',
    ticker                      VARCHAR(32) NOT NULL,
    normalized_product          TEXT NOT NULL,
    role_type                   VARCHAR(32) NOT NULL,
    review_status               VARCHAR(24) NOT NULL,
    evidence_through_date       DATE NOT NULL,
    decision_available_from     DATE NOT NULL DEFAULT CURRENT_DATE,
    reviewer                    TEXT NOT NULL,
    review_note                 TEXT NOT NULL,
    source_url                  TEXT,
    source_title                TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, ticker, normalized_product, role_type, decision_available_from),
    CHECK (role_type IN ('MANUFACTURER', 'EPC_OR_INSTALLER',
                         'SYSTEM_INTEGRATOR', 'INPUT_SUPPLIER',
                         'DIRECT_ROLE_UNCLASSIFIED')),
    CHECK (review_status IN ('APPROVED', 'REJECTED')),
    CHECK (evidence_through_date <= decision_available_from)
);
CREATE INDEX IF NOT EXISTS idx_company_role_reviews_asof
    ON mg_company_role_reviews(country, ticker, normalized_product, role_type,
                               decision_available_from DESC);

-- Broad policy discovery is a monthly research job, not a synchronous report
-- query.  Materialising its dated output keeps the decision path fast while
-- preserving names that appear in policy filings before the theme mapper.
CREATE TABLE IF NOT EXISTS mg_policy_company_signals (
    id                  BIGSERIAL PRIMARY KEY,
    country             VARCHAR(10) NOT NULL DEFAULT 'IN',
    as_of_date          DATE NOT NULL,
    scheme_name         TEXT NOT NULL,
    ticker              VARCHAR(32) NOT NULL,
    signal_tier         VARCHAR(24) NOT NULL,
    first_mention_date  DATE,
    n_docs_total        INTEGER NOT NULL DEFAULT 0,
    n_last12m           INTEGER NOT NULL DEFAULT 0,
    n_prior12m          INTEGER NOT NULL DEFAULT 0,
    first_commit_date   DATE,
    n_commit_docs       INTEGER NOT NULL DEFAULT 0,
    industry            TEXT,
    source_method       VARCHAR(80) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, as_of_date, scheme_name, ticker),
    CHECK (signal_tier IN ('QUALIFIED', 'EARLY_PING')),
    CHECK (n_docs_total >= 0 AND n_last12m >= 0 AND n_prior12m >= 0 AND n_commit_docs >= 0),
    CHECK (first_mention_date IS NULL OR first_mention_date <= as_of_date),
    CHECK (first_commit_date IS NULL OR first_commit_date <= as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_policy_company_signals_asof
    ON mg_policy_company_signals(country, as_of_date DESC, scheme_name, signal_tier);

-- Product phrases appearing in a dated company-policy disclosure are a
-- separate high-recall discovery source.  They are not aliases, constraints,
-- or company endorsements: the company-product-role extractor must still
-- recover an issuer-owned manufacturing disclosure, and an analyst must still
-- establish demand/supply economics before the phrase can enter a decision.
CREATE TABLE IF NOT EXISTS mg_policy_product_discoveries (
    id                  BIGSERIAL PRIMARY KEY,
    country             VARCHAR(10) NOT NULL DEFAULT 'IN',
    as_of_date          DATE NOT NULL,
    scheme_name         TEXT NOT NULL,
    product_label       TEXT NOT NULL,
    normalized_product  TEXT NOT NULL,
    source_document_count INTEGER NOT NULL DEFAULT 0,
    source_issuer_count INTEGER NOT NULL DEFAULT 0,
    first_source_date   DATE,
    last_source_date    DATE,
    source_method       VARCHAR(80) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, as_of_date, scheme_name, normalized_product),
    CHECK (source_document_count > 0),
    CHECK (source_issuer_count > 0),
    CHECK (first_source_date IS NULL OR first_source_date <= as_of_date),
    CHECK (last_source_date IS NULL OR last_source_date <= as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_policy_product_discoveries_asof
    ON mg_policy_product_discoveries(country, as_of_date DESC, normalized_product);

-- ============================================================
-- 27. UPSTREAM CONSTRAINT CANDIDATE SNAPSHOTS
-- ============================================================
-- This is the bridge between ingestion/NLP and the selector. It preserves a
-- product chain even when no beneficiary mapper row exists, records each
-- missing mechanism leg, and keeps company-role discovery separate from the
-- physical constraint score. Research priority is not investment authority.
CREATE TABLE IF NOT EXISTS mg_constraint_candidates (
    id                         BIGSERIAL PRIMARY KEY,
    country                    VARCHAR(10) NOT NULL DEFAULT 'IN',
    as_of_date                 DATE NOT NULL,
    constraint_key             VARCHAR(160) NOT NULL,
    product_label              TEXT NOT NULL,
    normalized_product         TEXT NOT NULL,
    theme_name                 TEXT NOT NULL,
    mechanism                  VARCHAR(40) NOT NULL,
    physical_quality           VARCHAR(16) NOT NULL,
    research_priority          INTEGER NOT NULL,
    research_state             VARCHAR(24) NOT NULL,
    resolution_risk            VARCHAR(24) NOT NULL,
    detection_origins          TEXT[] NOT NULL DEFAULT '{}',
    evidence_legs              JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_legs               TEXT[] NOT NULL DEFAULT '{}',
    independent_source_count   INTEGER NOT NULL DEFAULT 0,
    company_count              INTEGER NOT NULL DEFAULT 0,
    reviewed_company_count     INTEGER NOT NULL DEFAULT 0,
    first_detected_date        DATE,
    last_evidence_date         DATE,
    next_action                TEXT NOT NULL,
    extractor_version          VARCHAR(80) NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, as_of_date, constraint_key, normalized_product),
    CHECK (mechanism IN ('PHYSICAL_CONSTRAINT', 'LOCALISATION_QUALIFICATION',
                         'DEPLOYMENT_DEMAND_PULL', 'UNCLASSIFIED_RESEARCH')),
    CHECK (physical_quality IN ('A', 'B', 'WEAK', 'UNMEASURED')),
    CHECK (research_priority BETWEEN 0 AND 100),
    CHECK (research_state IN ('INVESTIGATE_NOW', 'MEASURE_NEXT', 'DISCOVERY'))
);
CREATE INDEX IF NOT EXISTS idx_constraint_candidates_priority
    ON mg_constraint_candidates(country, as_of_date DESC, research_priority DESC);

CREATE TABLE IF NOT EXISTS mg_constraint_company_candidates (
    id                          BIGSERIAL PRIMARY KEY,
    country                     VARCHAR(10) NOT NULL DEFAULT 'IN',
    as_of_date                  DATE NOT NULL,
    constraint_key              VARCHAR(160) NOT NULL,
    normalized_product          TEXT NOT NULL,
    product_label               TEXT NOT NULL,
    ticker                      VARCHAR(32) NOT NULL,
    company                     TEXT,
    role_type                   VARCHAR(32) NOT NULL,
    role_state                  VARCHAR(24) NOT NULL,
    link_type                   VARCHAR(32) NOT NULL,
    independent_document_count  INTEGER NOT NULL DEFAULT 0,
    physical_evidence_count     INTEGER NOT NULL DEFAULT 0,
    pipeline_evidence_count     INTEGER NOT NULL DEFAULT 0,
    earnings_capture_count      INTEGER NOT NULL DEFAULT 0,
    demand_signal_count         INTEGER NOT NULL DEFAULT 0,
    role_confidence             NUMERIC(6, 3) NOT NULL DEFAULT 0,
    selection_state             VARCHAR(40) NOT NULL,
    earnings_capture_status     VARCHAR(24) NOT NULL DEFAULT 'UNPROVED',
    adjudication_state          VARCHAR(48),
    adjudication_reason         TEXT,
    evidence                    JSONB NOT NULL DEFAULT '[]'::jsonb,
    extractor_version           VARCHAR(80) NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(country, as_of_date, constraint_key, normalized_product, ticker),
    CHECK (selection_state IN ('APPROVED_OPERATING_MAKER', 'PIPELINE_DIRECT_ROLE',
                               'EXACT_ROLE_REVIEW', 'PRODUCT_ROLE_DISCOVERY',
                               'CAPABILITY_LEAD')),
    CHECK (earnings_capture_status IN ('UNPROVED', 'INDICATED', 'CONFIRMED')),
    CHECK (role_confidence BETWEEN 0 AND 1)
);
ALTER TABLE mg_constraint_company_candidates
    ADD COLUMN IF NOT EXISTS pipeline_evidence_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS earnings_capture_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS adjudication_state VARCHAR(48),
    ADD COLUMN IF NOT EXISTS adjudication_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_constraint_company_candidates_chain
    ON mg_constraint_company_candidates(country, as_of_date DESC,
                                         constraint_key, role_confidence DESC);
