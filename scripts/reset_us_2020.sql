-- ============================================================
-- Reset US 2020 data only for a clean pipeline re-run
-- Keeps India data and other years untouched
-- ============================================================

BEGIN;

-- Step 1: Get US 2020 document IDs
CREATE TEMP TABLE us_2020_docs AS
SELECT id FROM mg_documents
WHERE country = 'US'
  AND filed_at BETWEEN '2020-01-01' AND '2020-12-31';

-- Step 2: Delete signals from those documents
DELETE FROM mg_signals WHERE document_id IN (SELECT id FROM us_2020_docs);

-- Step 3: Delete document-entity links
DELETE FROM mg_document_entities WHERE document_id IN (SELECT id FROM us_2020_docs);

-- Step 4: Reset document status so NLP will reprocess them
UPDATE mg_documents
SET processing_status = 'fetched',
    sentiment_score   = NULL,
    nlp_summary       = NULL,
    updated_at        = NOW()
WHERE id IN (SELECT id FROM us_2020_docs);

-- Step 5: Delete US themes (they'll be rebuilt from new signals)
DELETE FROM mg_theme_beneficiaries tb
USING mg_themes t
WHERE tb.theme_id = t.id AND t.country = 'US';

DELETE FROM mg_theme_snapshots ts
USING mg_themes t
WHERE ts.theme_id = t.id AND t.country = 'US';

DELETE FROM mg_themes WHERE country = 'US';

-- Step 6: Delete US causal chains
DELETE FROM mg_causal_chains WHERE country = 'US';

-- Verify
SELECT 'us_2020_docs'    , COUNT(*) FROM us_2020_docs
UNION ALL
SELECT 'remaining_signals', COUNT(*) FROM mg_signals s JOIN mg_documents d ON d.id = s.document_id WHERE d.country='US' AND d.filed_at BETWEEN '2020-01-01' AND '2020-12-31'
UNION ALL
SELECT 'docs_to_process' , COUNT(*) FROM mg_documents WHERE country='US' AND filed_at BETWEEN '2020-01-01' AND '2020-12-31' AND processing_status='fetched';

COMMIT;
