#!/usr/bin/env python3
"""Re-run NLP on all India documents to rebuild signals with correct entity linkage.

Steps:
  1. Reset processing_status from 'graph_built'/'nlp_done'/'nlp_failed' → 'fetched' for IN docs
  2. Run HistoricalRunner NLP-only pass over the full India date range
  3. New theme-entity signals INSERT alongside existing company-entity signals
     (dedup key: document_id, entity_id, signal_type, direction — no data loss)

Usage:
  python scripts/run_india_nlp_rebuild.py [--dry-run]
"""
import sys, yaml, json, logging, argparse
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("india_nlp_rebuild")

sys.path.insert(0, ".")

# ── Load config ──────────────────────────────────────────────────────────────
with open("config/settings.yaml") as f:
    config = yaml.safe_load(f)
try:
    with open("config/secrets.json") as f:
        secrets = json.load(f)
    for section, values in secrets.items():
        if section.startswith("_"):
            continue
        if isinstance(values, dict):
            config.setdefault(section, {}).update({k: v for k, v in values.items() if v})
except Exception as e:
    logger.warning(f"secrets: {e}")

# Force India market so IndiaEntityInjector is activated
config.setdefault("market", {})["country"] = "IN"

# ── CLI args ─────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true",
                    help="Show counts only; don't reset status or run NLP")
args = parser.parse_args()

# ── Connect ──────────────────────────────────────────────────────────────────
import psycopg2
pg = config["postgresql"]
conn = psycopg2.connect(
    host=pg["host"], port=pg["port"], dbname=pg["dbname"],
    user=pg["user"], password=pg.get("password", ""),
)
cur = conn.cursor()

# ── Inspect current state ────────────────────────────────────────────────────
cur.execute(
    "SELECT processing_status, COUNT(*) FROM mg_documents WHERE country='IN' "
    "GROUP BY processing_status ORDER BY processing_status"
)
rows = cur.fetchall()
logger.info("India document status before reset:")
total_to_reset = 0
for status, cnt in rows:
    logger.info(f"  {status}: {cnt:,}")
    if status in ("graph_built", "nlp_done", "nlp_failed"):
        total_to_reset += cnt

cur.execute(
    "SELECT MIN(filed_at), MAX(filed_at) FROM mg_documents WHERE country='IN'"
)
min_date, max_date = cur.fetchone()
logger.info(f"Date range: {min_date} → {max_date}")

if args.dry_run:
    logger.info(f"DRY RUN: would reset {total_to_reset:,} docs → 'fetched' then run NLP")
    conn.close()
    sys.exit(0)

# ── Step 1: Reset status to 'fetched' ────────────────────────────────────────
logger.info(f"Resetting {total_to_reset:,} India docs → 'fetched' …")
cur.execute(
    "UPDATE mg_documents SET processing_status = 'fetched' "
    "WHERE country = 'IN' AND processing_status IN ('graph_built', 'nlp_done', 'nlp_failed')"
)
conn.commit()
logger.info(f"Reset complete: {cur.rowcount:,} rows updated")
conn.close()

# ── Step 2: Run NLP-only historical pass ─────────────────────────────────────
from src.makrograph.pipeline.historical_runner import HistoricalRunner

runner = HistoricalRunner(
    config=config,
    start_date=min_date,
    end_date=max_date,
    replay_mode="monthly",
    skip_ingest=True,
    skip_neo4j=True,
    skip_graph=True,
    skip_events=True,
    skip_causal=True,
    skip_themes=True,
    skip_pdf_fetch=True,
)

logger.info(f"Starting NLP rebuild: {min_date} → {max_date}")
results = runner.run()

total_docs = sum(r.docs_nlp for r in results)
total_months = len(results)
logger.info(f"NLP rebuild complete: {total_docs:,} docs across {total_months} months")

for r in results:
    if r.docs_nlp > 0 or r.docs_ingested > 0:
        logger.info(f"  {r.window_start} → {r.window_end}: nlp={r.docs_nlp}")
