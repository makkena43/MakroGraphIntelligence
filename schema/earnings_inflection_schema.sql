-- ============================================================
-- Earnings Inflection Detector - isolated research schema
--
-- STATUS: NOT EXECUTED.  Apply ONLY to a dedicated test database until
-- production activation is explicitly authorised.  It creates objects in
-- its own schema and does not alter any existing mg_* table.
--
--   psql "$EI_TEST_DSN" -f schema/earnings_inflection_schema.sql
-- ============================================================

CREATE SCHEMA IF NOT EXISTS earnings_inflection;

CREATE TABLE IF NOT EXISTS earnings_inflection.ei_runs (
    id            BIGSERIAL PRIMARY KEY,
    as_of         TIMESTAMPTZ NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    code_version  TEXT,
    config_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    budget_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes         TEXT
);

-- One row per company per run.  States are evidence states only; there is
-- intentionally no column for actions, ratings, targets or position sizes.
CREATE TABLE IF NOT EXISTS earnings_inflection.ei_assessments (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT NOT NULL REFERENCES earnings_inflection.ei_runs(id) ON DELETE CASCADE,
    ticker           VARCHAR(30) NOT NULL,
    as_of            TIMESTAMPTZ NOT NULL,
    evidence_status  VARCHAR(40) NOT NULL CHECK (evidence_status IN (
                        'INSUFFICIENT_EVIDENCE','NO_MATERIAL_CHANGE','ASSERTION_ONLY',
                        'EARLY_COMMITMENT_UNVERIFIED','COMMITMENT_BACKED',
                        'EXECUTION_EMERGING','EXECUTION_CONFIRMED','CONTRADICTED')),
    review_status    VARCHAR(40) NOT NULL CHECK (review_status IN (
                        'UNREVIEWED','NEEDS_SOURCE_CHECK','REVIEWED_ACCEPTED','REVIEWED_REJECTED')),
    scenario_status  VARCHAR(40) NOT NULL CHECK (scenario_status IN (
                        'COMPUTED_ASSUMPTION_BASED','NOT_COMPUTED_MISSING_INPUTS','UNSUPPORTED_FINANCIAL_MODEL')),
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_ei_assess_ticker ON earnings_inflection.ei_assessments (ticker, as_of DESC);

-- Human review trail (the pipeline never writes REVIEWED_* itself).
CREATE TABLE IF NOT EXISTS earnings_inflection.ei_reviews (
    id              BIGSERIAL PRIMARY KEY,
    assessment_id   BIGINT NOT NULL REFERENCES earnings_inflection.ei_assessments(id) ON DELETE CASCADE,
    reviewer        TEXT NOT NULL,
    review_status   VARCHAR(40) NOT NULL,
    comment         TEXT,
    reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Additions for WP1-WP4 (still NOT EXECUTED anywhere; test database only).
-- Idempotent: safe to re-apply to a test database created from the
-- block above.
-- ============================================================

-- Run manifest (WP1/WP2): what a run read and how, so it can be replayed.
ALTER TABLE earnings_inflection.ei_runs ADD COLUMN IF NOT EXISTS run_uuid      UUID;
ALTER TABLE earnings_inflection.ei_runs ADD COLUMN IF NOT EXISTS replay_mode   VARCHAR(40);
ALTER TABLE earnings_inflection.ei_runs ADD COLUMN IF NOT EXISTS config_hash   CHAR(64);
ALTER TABLE earnings_inflection.ei_runs ADD COLUMN IF NOT EXISTS run_status    VARCHAR(20);
ALTER TABLE earnings_inflection.ei_runs ADD COLUMN IF NOT EXISTS manifest_json JSONB;
DO $$ BEGIN
    ALTER TABLE earnings_inflection.ei_runs ADD CONSTRAINT ei_runs_replay_mode_chk
        CHECK (replay_mode IS NULL OR replay_mode IN ('PUBLIC_INFORMATION_RECONSTRUCTION','SYSTEM_KNOWLEDGE_REPLAY'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE earnings_inflection.ei_runs ADD CONSTRAINT ei_runs_status_chk
        CHECK (run_status IS NULL OR run_status IN ('COMPLETE','PARTIAL','FAILED'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- An existing test database created before EARLY_COMMITMENT_UNVERIFIED existed.
DO $$
DECLARE c TEXT;
BEGIN
    SELECT conname INTO c FROM pg_constraint
     WHERE conrelid = 'earnings_inflection.ei_assessments'::regclass AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%evidence_status%'
       AND pg_get_constraintdef(oid) NOT LIKE '%EARLY_COMMITMENT_UNVERIFIED%';
    IF c IS NOT NULL THEN
        EXECUTE format('ALTER TABLE earnings_inflection.ei_assessments DROP CONSTRAINT %I', c);
        ALTER TABLE earnings_inflection.ei_assessments ADD CONSTRAINT ei_assessments_evidence_status_check
            CHECK (evidence_status IN ('INSUFFICIENT_EVIDENCE','NO_MATERIAL_CHANGE','ASSERTION_ONLY',
                   'EARLY_COMMITMENT_UNVERIFIED','COMMITMENT_BACKED','EXECUTION_EMERGING','EXECUTION_CONFIRMED',
                   'CONTRADICTED'));
    END IF;
END $$;

-- Exactly which text version of each source an assessment read (WP1).
CREATE TABLE IF NOT EXISTS earnings_inflection.ei_source_versions (
    id                    BIGSERIAL PRIMARY KEY,
    assessment_id         BIGINT NOT NULL REFERENCES earnings_inflection.ei_assessments(id) ON DELETE CASCADE,
    doc_id                TEXT NOT NULL,
    text_source           VARCHAR(40),          -- artifact | raw_text | text_file | none
    extraction_version_id TEXT,                 -- content-addressed artifact id; NULL for legacy text
    extraction_status     VARCHAR(40),
    raw_hash              CHAR(64),
    text_hash             CHAR(64),
    available_at          TIMESTAMPTZ,          -- public availability
    first_seen_at         TIMESTAMPTZ,          -- when this system first stored the row
    text_available_at     TIMESTAMPTZ,          -- when readable text existed in this system
    UNIQUE (assessment_id, doc_id)
);

-- Extraction attempts, mirroring the local artifact store's attempts.jsonl (WP1).
-- Failures are recoverable states, not silent gaps.
CREATE TABLE IF NOT EXISTS earnings_inflection.ei_extraction_results (
    id                    BIGSERIAL PRIMARY KEY,
    doc_id                TEXT NOT NULL,
    raw_hash              CHAR(64),
    method                VARCHAR(40) NOT NULL,  -- pymupdf | pdfplumber | ocr:<provider> | ...
    parser_version        TEXT NOT NULL,
    status                VARCHAR(40) NOT NULL CHECK (status IN (
                             'COMPLETE','PARTIAL','OCR_REQUIRED','PARSE_FAILED_RETRYABLE','ENCRYPTED','EMPTY',
                             'UNSUPPORTED_FORMAT','MISSING_ORIGINAL','NOT_EXTRACTED','LEGACY_UNVERSIONED')),
    extraction_version_id TEXT,                 -- set when text was produced
    pages_extracted       INTEGER,
    pages_expected        INTEGER,
    truncated             BOOLEAN NOT NULL DEFAULT FALSE,
    quality_issues        JSONB NOT NULL DEFAULT '[]'::jsonb,
    error                 TEXT,
    extracted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ei_extract_doc ON earnings_inflection.ei_extraction_results (doc_id, extracted_at DESC);

-- Deduplicated commercial events as seen by one assessment (WP4).
CREATE TABLE IF NOT EXISTS earnings_inflection.ei_events (
    id                    BIGSERIAL PRIMARY KEY,
    assessment_id         BIGINT NOT NULL REFERENCES earnings_inflection.ei_assessments(id) ON DELETE CASCADE,
    event_id              TEXT NOT NULL,
    current_stage         VARCHAR(30) NOT NULL CHECK (current_stage IN (
                             'inquiry','mou_framework','preferred_bidder','binding_order','execution',
                             'amended','cancelled','expired')),
    counterparty          TEXT,
    customer_verification VARCHAR(20) CHECK (customer_verification IN ('anonymous','issuer_named','corroborated')),
    relationship          VARCHAR(40) CHECK (relationship IN (
                             'confirmed_related','issuer_asserted_unrelated','independently_supported_unrelated',
                             'unknown')),
    value_basis           VARCHAR(30),
    tax_basis             VARCHAR(20),
    amount_crore          NUMERIC,              -- current value (after amendments / partial cancellations)
    original_amount_crore NUMERIC,
    cancelled_amount_crore NUMERIC NOT NULL DEFAULT 0,
    duration_months       INTEGER,
    reference_id          TEXT,
    first_public_at       TIMESTAMPTZ,
    verified              BOOLEAN NOT NULL,     -- may support COMMITMENT_BACKED
    unverified_reasons    JSONB NOT NULL DEFAULT '[]'::jsonb,
    ambiguous_with        JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload               JSONB NOT NULL,
    UNIQUE (assessment_id, event_id)
);

-- Dated state history of each event; append-only, never downgraded by a later retelling.
CREATE TABLE IF NOT EXISTS earnings_inflection.ei_event_states (
    id            BIGSERIAL PRIMARY KEY,
    event_row_id  BIGINT NOT NULL REFERENCES earnings_inflection.ei_events(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    stage         VARCHAR(30) NOT NULL,
    at            TIMESTAMPTZ,
    doc_id        TEXT,
    amount_crore  NUMERIC,
    evidence_id   TEXT,
    note          TEXT,
    UNIQUE (event_row_id, seq)
);

-- ============================================================
-- WP8: bounded universe runs and research shortlists (test database only; NOT EXECUTED
-- against any shared database).  Rows are immutable per run; no column carries an action.
-- ============================================================
CREATE TABLE IF NOT EXISTS earnings_inflection.ei_universe_snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    as_of         DATE NOT NULL,
    source        TEXT NOT NULL,
    cohort_label  TEXT,
    members_json  JSONB NOT NULL,              -- includes delisted / suspended members
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS earnings_inflection.ei_universe_runs (
    run_id            TEXT PRIMARY KEY,
    snapshot_id       TEXT NOT NULL REFERENCES earnings_inflection.ei_universe_snapshots(snapshot_id),
    as_of             DATE NOT NULL,
    status            VARCHAR(20) NOT NULL CHECK (status IN ('COMPLETE','PARTIAL','FAILED')),
    coverage_complete BOOLEAN NOT NULL,
    counts_json       JSONB NOT NULL,          -- scanned / excluded / eligible / completed / failed / deferred
    config_json       JSONB NOT NULL,          -- limits, weights, critical checks
    previous_run_id   TEXT,                    -- incremental runs
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS earnings_inflection.ei_shortlist_entries (
    run_id                TEXT NOT NULL REFERENCES earnings_inflection.ei_universe_runs(run_id) ON DELETE CASCADE,
    ticker                VARCHAR(30) NOT NULL,
    lane                  VARCHAR(30) NOT NULL CHECK (lane IN ('EXECUTION_RESEARCH','COMMITMENT_RESEARCH',
                              'ASSERTION_WATCH','DATA_REPAIR','CONTRADICTED_OR_STALE')),
    rank_in_lane          INTEGER NOT NULL,
    score                 NUMERIC,
    score_components_json JSONB,               -- visible heuristic components, not probabilities
    first_signal_at       TIMESTAMPTZ,
    change_since_previous TEXT,
    payload               JSONB NOT NULL,
    PRIMARY KEY (run_id, ticker)
);
