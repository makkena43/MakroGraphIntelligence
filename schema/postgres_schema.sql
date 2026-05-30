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
