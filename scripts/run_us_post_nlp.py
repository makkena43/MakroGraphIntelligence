#!/usr/bin/env python3
"""Run post-NLP stages on all US documents year by year.

Stages (in order):
  1. Graph + Events  — monthly pass over all nlp_done US docs (resume-aware)
  2. Causal chains   — single discovery pass over full data
  3. Theme snapshots — yearly: 2020, 2021, 2022, 2023, 2024, 2025, current

Usage:
  python scripts/run_us_post_nlp.py [--resume-from YYYY-MM-DD]
"""
import sys, yaml, json, logging, argparse, psycopg2
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("us_post_nlp")
sys.path.insert(0, ".")

parser = argparse.ArgumentParser()
parser.add_argument("--resume-from", default=None,
                    help="Resume monthly graph pass from this date (default: start of data)")
parser.add_argument("--skip-graph", action="store_true")
parser.add_argument("--skip-causal", action="store_true")
parser.add_argument("--skip-themes", action="store_true")
args = parser.parse_args()

# ── Config ─────────────────────────────────────────────────────────────────
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

config.setdefault("market", {})["country"] = "US"

# ── Get date range ──────────────────────────────────────────────────────────
pg = config["postgresql"]
conn = psycopg2.connect(
    host=pg["host"], port=pg["port"], dbname=pg["dbname"],
    user=pg["user"], password=pg.get("password", ""),
)
cur = conn.cursor()
cur.execute("SELECT processing_status, COUNT(*) FROM mg_documents WHERE country='US' GROUP BY processing_status ORDER BY processing_status")
logger.info("US document status:")
for status, cnt in cur.fetchall():
    logger.info(f"  {status}: {cnt:,}")

cur.execute("SELECT MIN(filed_at), MAX(filed_at) FROM mg_documents WHERE country='US'")
min_date, max_date = cur.fetchone()
logger.info(f"Date range: {min_date} → {max_date}")
conn.close()

resume_from = date.fromisoformat(args.resume_from) if args.resume_from else min_date

# ── Stage 1: Graph + Events (monthly) ─────────────────────────────────────
if not args.skip_graph:
    logger.info(f"\n=== STAGE 1: Graph + Events (resume from {resume_from}) ===")
    from src.makrograph.pipeline.historical_runner import HistoricalRunner
    runner = HistoricalRunner(
        config=config,
        start_date=min_date,
        end_date=max_date,
        replay_mode="monthly",
        skip_ingest=True,
        skip_nlp=True,
        skip_pdf_fetch=True,
        skip_graph=False,
        skip_events=False,
        skip_causal=True,
        skip_themes=True,
    )
    results = runner.run(resume_from=resume_from)
    logger.info(
        f"Graph/Events complete: "
        f"nodes={sum(r.nodes_built for r in results):,} "
        f"edges={sum(r.edges_built for r in results):,} "
        f"events={sum(r.events_extracted for r in results):,}"
    )
else:
    logger.info("Skipping Stage 1 (--skip-graph)")

# ── Stage 2: Causal chains (single full-data pass) ─────────────────────────
if not args.skip_causal:
    logger.info("\n=== STAGE 2: Causal chains ===")
    try:
        from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
        cp = IntelligencePipeline(config)
        cp._init_storage()
        result = cp.run_causal_chains(country="US")
        logger.info(f"Causal chains complete: {result}")
    except Exception as e:
        logger.warning(f"Causal pass failed (non-fatal): {e}", exc_info=True)
else:
    logger.info("Skipping Stage 2 (--skip-causal)")

# ── Stage 3: Theme snapshots — all years ──────────────────────────────────
if not args.skip_themes:
    logger.info("\n=== STAGE 3: Theme snapshots (2020 → 2021 → ... → current) ===")

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

    # Wipe existing snapshots so rebuild is clean
    pg_cfg = config["postgresql"]
    wipe_conn = psycopg2.connect(
        host=pg_cfg["host"], port=pg_cfg["port"], dbname=pg_cfg["dbname"],
        user=pg_cfg["user"], password=pg_cfg.get("password", ""),
    )
    wipe_cur = wipe_conn.cursor()
    snap_dates = [d for d in REPLAY_DATES if d is not None]
    for sd in snap_dates:
        wipe_cur.execute(
            "DELETE FROM mg_theme_snapshots WHERE snapshot_date = %s "
            "AND theme_id IN (SELECT id FROM mg_themes WHERE country='US')",
            (sd,)
        )
        logger.info(f"Wiped {wipe_cur.rowcount} existing rows for {sd}")
    today = date.today()
    wipe_cur.execute(
        "DELETE FROM mg_theme_snapshots WHERE snapshot_date = %s "
        "AND theme_id IN (SELECT id FROM mg_themes WHERE country='US')",
        (today,)
    )
    logger.info(f"Wiped {wipe_cur.rowcount} rows for today ({today})")
    wipe_conn.commit()
    wipe_conn.close()

    for replay_date in REPLAY_DATES:
        label = str(replay_date) if replay_date else "CURRENT (live)"
        logger.info(f"\n--- Themes as_of={label} ---")

        # Year-specific window for historical dates; 2-year window for live
        if replay_date is not None:
            pipeline.config.setdefault("themes", {})["signal_window_days"] = 365
        else:
            pipeline.config.setdefault("themes", {})["signal_window_days"] = 730

        try:
            result = pipeline.run_themes(as_of_date=replay_date, country="US")
            logger.info(f"Done: {result}")
        except Exception as e:
            logger.error(f"Themes failed for {label}: {e}", exc_info=True)
else:
    logger.info("Skipping Stage 3 (--skip-themes)")

logger.info("\n✓ US post-NLP pipeline complete.")
