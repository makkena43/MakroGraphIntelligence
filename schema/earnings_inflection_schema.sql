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
                        'INSUFFICIENT_EVIDENCE','NO_MATERIAL_CHANGE','ASSERTION_ONLY','COMMITMENT_BACKED',
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
