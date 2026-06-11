#!/usr/bin/env python3
"""Run post-NLP stages on all India documents after NLP rebuild.

Stages run (in order):
  1. Graph building + Events  — monthly pass (resume-aware via resume_from)
  2. Causal chains            — single pass at the end (avoids 230s/month overhead)
  3. Theme detection          — yearly snapshots: 2022, 2023, 2024, 2025, current

Docs must be in 'nlp_done' status (output of run_india_nlp_rebuild.py).

Usage:
  python scripts/run_india_post_nlp.py [--resume-from YYYY-MM-DD]

  --resume-from  Skip all months before this date (default: 2021-11-01,
                 the month after the last completed month)
"""
import sys, yaml, json, logging, argparse
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("india_post_nlp")

sys.path.insert(0, ".")

parser = argparse.ArgumentParser()
parser.add_argument("--resume-from", default="2021-11-01",
                    help="Resume monthly pass from this date (YYYY-MM-DD)")
args = parser.parse_args()
resume_from = date.fromisoformat(args.resume_from)

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

config.setdefault("market", {})["country"] = "IN"

# ── Verify current doc status ────────────────────────────────────────────────
import psycopg2
pg = config["postgresql"]
conn = psycopg2.connect(
    host=pg["host"], port=pg["port"], dbname=pg["dbname"],
    user=pg["user"], password=pg.get("password", ""),
)
cur = conn.cursor()
cur.execute(
    "SELECT processing_status, COUNT(*) FROM mg_documents WHERE country='IN' "
    "GROUP BY processing_status ORDER BY processing_status"
)
logger.info("India document status at post-NLP start:")
for status, cnt in cur.fetchall():
    logger.info(f"  {status}: {cnt:,}")

cur.execute("SELECT MIN(filed_at), MAX(filed_at) FROM mg_documents WHERE country='IN'")
min_date, max_date = cur.fetchone()
logger.info(f"Full date range: {min_date} → {max_date}")
logger.info(f"Resuming monthly pass from: {resume_from}")
conn.close()

# ── Stage 1: Graph + Events (monthly, causal SKIPPED — done once below) ──────
from src.makrograph.pipeline.historical_runner import HistoricalRunner

logger.info("\n=== STAGE 1: Graph + Events (monthly, resume from %s) ===", resume_from)
runner = HistoricalRunner(
    config=config,
    start_date=min_date,
    end_date=max_date,
    replay_mode="monthly",
    skip_ingest=True,
    skip_nlp=True,
    skip_pdf_fetch=True,
    skip_graph=False,       # ← build graph nodes/edges from nlp_done docs
    skip_events=False,      # ← extract events
    skip_causal=True,       # ← skip per-month causal (run once below instead)
    skip_themes=True,       # ← themes done separately as yearly snapshots
)

results = runner.run(resume_from=resume_from)
total_nodes = sum(r.nodes_built for r in results)
total_edges = sum(r.edges_built for r in results)
total_events = sum(r.events_extracted for r in results)
logger.info(
    f"Graph/Events complete: nodes={total_nodes:,} edges={total_edges:,} events={total_events:,}"
)

# ── Stage 2: Single causal chain pass (over all data, run once) ──────────────
logger.info("\n=== STAGE 2: Causal chains (single full-data pass) ===")
try:
    from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline as _IP
    _cp = _IP(config)
    _cp._init_storage()
    causal_result = _cp.run_causal_chains(country="IN")
    logger.info(f"Causal complete: {causal_result}")
except Exception as e:
    logger.warning(f"Causal pass failed (non-fatal): {e}")

# ── Stage 3: Yearly theme snapshots (wipe then rebuild each year cleanly) ────
logger.info("\n=== STAGE 3: Theme snapshots (2020 → 2021 → 2022 → 2023 → 2024 → 2025 → current) ===")
from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline

pipeline = IntelligencePipeline(config)
pipeline._init_storage()

REPLAY_DATES = [
    date(2020, 12, 31),
    date(2021, 12, 31),
    date(2022, 12, 31),
    date(2023, 12, 31),
    date(2024, 12, 31),
    date(2025, 12, 31),
    None,  # current / live
]

# Wipe existing year-end snapshots before rebuild so we get clean data
# (avoids stale rows from previous runs coexisting with new ones)
import psycopg2 as _pg
_pg_cfg = config["postgresql"]
_wipe_conn = _pg.connect(
    host=_pg_cfg["host"], port=_pg_cfg["port"], dbname=_pg_cfg["dbname"],
    user=_pg_cfg["user"], password=_pg_cfg.get("password", ""),
)
_wipe_cur = _wipe_conn.cursor()
# Use date objects (not strings) to avoid type-cast issues
_snap_dates = [d for d in REPLAY_DATES if d is not None]
for _sd in _snap_dates:
    _wipe_cur.execute(
        "DELETE FROM mg_theme_snapshots WHERE snapshot_date = %s AND country = 'IN'",
        (_sd,)
    )
    logger.info(f"Wiped {_wipe_cur.rowcount} existing rows for snapshot {_sd}")
# Also wipe today's snapshot so live run starts fresh
from datetime import date as _date_cls
_today = _date_cls.today()
_wipe_cur.execute(
    "DELETE FROM mg_theme_snapshots WHERE snapshot_date = %s AND country = 'IN'",
    (_today,)
)
logger.info(f"Wiped {_wipe_cur.rowcount} existing rows for today ({_today})")
_wipe_conn.commit()
_wipe_conn.close()

for replay_date in REPLAY_DATES:
    label = str(replay_date) if replay_date else "CURRENT (live)"
    logger.info(f"\n--- Themes as_of={label} ---")

    # Yearly replay snapshots: strict 365-day window so each year is independent.
    # Transformer dominates 2023 because it had 2023 signals, NOT because it
    # accumulated signals from 2020-2023.  Live (None) uses 730 days.
    if replay_date is not None:
        pipeline.config.setdefault("themes", {})["signal_window_days"] = 365
        logger.info("  signal_window_days=365 (year-specific window)")
    else:
        pipeline.config.setdefault("themes", {})["signal_window_days"] = 730
        logger.info("  signal_window_days=730 (live 2-year window)")

    result = pipeline.run_themes(as_of_date=replay_date, country="IN")
    logger.info(f"Done: {result}")

logger.info("\n✓ All post-NLP stages complete.")
