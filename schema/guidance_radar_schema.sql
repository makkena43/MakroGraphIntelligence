-- Separate guidance-to-execution radar.  No dependency on selector outputs.

CREATE TABLE IF NOT EXISTS gr_runs (
    id              BIGSERIAL PRIMARY KEY,
    as_of_date      DATE NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    model_name      TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    decision_count  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running',
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS gr_document_extractions (
    document_id       BIGINT NOT NULL REFERENCES mg_documents(id) ON DELETE CASCADE,
    extraction_model  TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    extracted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload            JSONB NOT NULL,
    PRIMARY KEY (document_id, extraction_model, source_fingerprint)
);

CREATE TABLE IF NOT EXISTS gr_promises (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES gr_runs(id) ON DELETE CASCADE,
    ticker             VARCHAR(30) NOT NULL,
    source_document_id BIGINT NOT NULL REFERENCES mg_documents(id) ON DELETE CASCADE,
    source_date        DATE NOT NULL,
    metric             TEXT NOT NULL,
    target_period      TEXT,
    target_low         NUMERIC,
    target_high        NUMERIC,
    baseline           NUMERIC,
    unit               TEXT,
    certainty          TEXT NOT NULL,
    confidence         NUMERIC NOT NULL,
    quote              TEXT NOT NULL,
    UNIQUE (run_id, ticker, source_date, metric, target_period, target_low, target_high)
);

CREATE TABLE IF NOT EXISTS gr_evidence_events (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES gr_runs(id) ON DELETE CASCADE,
    ticker             VARCHAR(30) NOT NULL,
    source_document_id BIGINT NOT NULL REFERENCES mg_documents(id) ON DELETE CASCADE,
    source_date        DATE NOT NULL,
    kind               TEXT NOT NULL,
    metric             TEXT,
    value              NUMERIC,
    prior_value        NUMERIC,
    unit               TEXT,
    event_status       TEXT NOT NULL,
    confidence         NUMERIC NOT NULL,
    quote              TEXT NOT NULL,
    -- Repeated exchange coversheets on one date are one independent proof leg.
    UNIQUE (run_id, ticker, source_date, kind, metric, value, event_status)
);

CREATE TABLE IF NOT EXISTS gr_risk_flags (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES gr_runs(id) ON DELETE CASCADE,
    ticker             VARCHAR(30) NOT NULL,
    source_document_id BIGINT NOT NULL REFERENCES mg_documents(id) ON DELETE CASCADE,
    source_date        DATE NOT NULL,
    kind               TEXT NOT NULL,
    severity           TEXT NOT NULL,
    confidence         NUMERIC NOT NULL,
    quote              TEXT NOT NULL,
    UNIQUE (run_id, ticker, source_date, kind, severity)
);

CREATE TABLE IF NOT EXISTS gr_decisions (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES gr_runs(id) ON DELETE CASCADE,
    ticker             VARCHAR(30) NOT NULL,
    company            TEXT,
    as_of_date         DATE NOT NULL,
    action             TEXT NOT NULL,
    score              NUMERIC NOT NULL,
    max_position_pct   NUMERIC NOT NULL DEFAULT 0,
    evidence_dates     INTEGER NOT NULL DEFAULT 0,
    components         JSONB NOT NULL,
    hard_vetoes        JSONB NOT NULL,
    why_now            JSONB NOT NULL,
    missing_proof      JSONB NOT NULL,
    invalidation       JSONB NOT NULL,
    model_name         TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_gr_decisions_asof_action
    ON gr_decisions (as_of_date DESC, action, score DESC);
CREATE INDEX IF NOT EXISTS idx_gr_promises_ticker_date
    ON gr_promises (ticker, source_date DESC);
CREATE INDEX IF NOT EXISTS idx_gr_evidence_ticker_date
    ON gr_evidence_events (ticker, source_date DESC);
CREATE INDEX IF NOT EXISTS idx_gr_risks_ticker_date
    ON gr_risk_flags (ticker, source_date DESC);
