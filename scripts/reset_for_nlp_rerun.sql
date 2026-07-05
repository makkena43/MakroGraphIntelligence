-- ============================================================
-- MakroGraph: Reset data for full NLP re-run
--
-- What this deletes:
--   mg_signals            → re-extracted with new signal types + perspective
--   mg_document_entities  → re-extracted during NLP
--   mg_themes             → re-detected from new signals
--   mg_theme_snapshots    → re-created during theme detection
--   mg_theme_beneficiaries → re-mapped with perspective filter
--   mg_causal_chains      → re-scored from new themes
--   mg_ai_summaries       → stale, re-generate on demand
--
-- What this KEEPS (do NOT delete):
--   mg_documents          → already ingested, expensive to re-fetch
--   mg_entities           → canonical entity registry, stable
--   mg_macro_events       → policy/budget events, not from concalls
--   mg_policy_events      → PLI/budget data, not from concalls
--   mg_replay_history     → audit trail
--
-- Run order:
--   1. Run this script
--   2. Run pipeline: NLP → Themes → Causal Chains → (India Intelligence for IN)
--   DO NOT run Ingest — documents are already fetched
-- ============================================================

BEGIN;

-- Step 1: Reset all documents to 'fetched' so NLP will reprocess them
-- This is the trigger: NLP picks up documents with status='fetched'
UPDATE mg_documents
SET processing_status = 'fetched',
    sentiment_score   = NULL,
    nlp_summary       = NULL,
    updated_at        = NOW()
WHERE processing_status IN ('nlp_done', 'embedded', 'graph_built',
                            'graphiti_done', 'nlp_failed', 'embedded_failed');

-- Step 2: Delete signals (re-extracted with new types: capacity_constraint_seller,
-- guidance_revenue, pricing_power_emerging, perspective field, etc.)
DELETE FROM mg_signals;

-- Step 3: Delete document-entity links (rebuilt during NLP)
DELETE FROM mg_document_entities;

-- Step 4: Delete theme beneficiaries (rebuilt with perspective filter)
DELETE FROM mg_theme_beneficiaries;

-- Step 5: Delete theme snapshots (rebuilt during theme detection)
DELETE FROM mg_theme_snapshots;

-- Step 6: Delete themes (rebuilt from new signals; they will be recreated
-- with improved specificity gates and seller/buyer distinction)
DELETE FROM mg_themes;

-- Step 7: Delete causal chains (rebuilt from new themes)
DELETE FROM mg_causal_chains;

-- Step 8: Clear AI summaries (stale; generated on-demand when user opens AI tab)
DELETE FROM mg_ai_summaries;

-- Verify counts
SELECT 'mg_documents'          AS table_name, COUNT(*) AS remaining FROM mg_documents
UNION ALL
SELECT 'mg_documents fetched'  , COUNT(*) FROM mg_documents WHERE processing_status = 'fetched'
UNION ALL
SELECT 'mg_signals'            , COUNT(*) FROM mg_signals
UNION ALL
SELECT 'mg_themes'             , COUNT(*) FROM mg_themes
UNION ALL
SELECT 'mg_theme_beneficiaries', COUNT(*) FROM mg_theme_beneficiaries
UNION ALL
SELECT 'mg_causal_chains'      , COUNT(*) FROM mg_causal_chains;

COMMIT;

-- ============================================================
-- After running this, execute pipeline stages in order:
--
-- For USA:
--   pipeline.run_nlp(country='US')
--   pipeline.run_themes(country='US')
--   pipeline.run_causal_chains(country='US')
--
-- For India:
--   pipeline.run_nlp(country='IN')
--   pipeline.run_india_intelligence(country='IN')   ← L1-L10 capacity/localization
--   pipeline.run_themes(country='IN')
--   pipeline.run_causal_chains(country='IN')
--
-- Can run US and India in parallel (separate processes).
-- ============================================================
