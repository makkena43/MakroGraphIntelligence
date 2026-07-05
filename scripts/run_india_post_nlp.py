#!/usr/bin/env python3
"""Run post-NLP stages on all India documents after NLP rebuild.

Stages run (in order):
  1. Graph building + Events  — monthly pass (resume-aware via resume_from)

  For each replay year (2020 → 2021 → ... → current):
  2. India Intelligence L1-L10  — policy targets (filtered by as_of_date so
                                   future targets don't leak into past years),
                                   capacity gaps (SupplyChainDB-enhanced),
                                   localization opportunities, beneficiary
                                   discovery, tender signals, order book
                                   signals, causal chain generation.
                                   Must run BEFORE themes so causal chains
                                   are in mg_causal_chains when ThemeRanker
                                   computes causal_chain_score.
  3. Theme detection snapshot   — as_of=Dec 31 of the year; uses causal
                                   chain score + beneficiary boost from Step 2.

  4. Claude final analysis      — live only (no key needed to skip).

Docs must be in 'nlp_done' status (output of run_india_nlp_rebuild.py).

Usage:
  python scripts/run_india_post_nlp.py [--resume-from YYYY-MM-DD]
  python scripts/run_india_post_nlp.py --skip-graph          # skip Stage 1
  python scripts/run_india_post_nlp.py --skip-intelligence   # skip Stage 2
  python scripts/run_india_post_nlp.py --from-year 2023      # years 2023+

  --resume-from  Skip all months before this date in Stage 1 (default: 2021-11-01)
"""
import sys, yaml, json, logging, argparse
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("india_post_nlp")

sys.path.insert(0, ".")

parser = argparse.ArgumentParser()
parser.add_argument("--resume-from", default="2021-11-01",
                    help="Resume monthly graph pass from this date (YYYY-MM-DD)")
parser.add_argument("--skip-graph", action="store_true",
                    help="Skip Stage 1 graph+events pass (already done)")
parser.add_argument("--skip-intelligence", action="store_true",
                    help="Skip Stage 2 India Intelligence L1-L10 per year")
parser.add_argument("--from-year", type=int, default=None,
                    help="Only run yearly stages for this year and later")
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

# ── Stage 1: Graph + Events (monthly, causal SKIPPED — done per-year below) ───
if not args.skip_graph:
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
        skip_events=False,      # ← extract events (combined in NLP pass)
        skip_causal=True,       # ← causal chains run per-year in Stage 2 below
        skip_themes=True,       # ← themes done as yearly snapshots in Stage 3
    )
    results = runner.run(resume_from=resume_from)
    total_nodes  = sum(r.nodes_built for r in results)
    total_edges  = sum(r.edges_built for r in results)
    total_events = sum(r.events_extracted for r in results)
    logger.info(
        f"Graph/Events complete: nodes={total_nodes:,} edges={total_edges:,} events={total_events:,}"
    )
else:
    logger.info("\n=== STAGE 1: Graph + Events SKIPPED (--skip-graph) ===")

# ── Stages 2 + 3: Per-year India Intelligence → Themes ───────────────────────
# Stage 2 (India Intelligence L1-L10) MUST run before Stage 3 (Themes) so that:
#   - mg_causal_chains is populated before ThemeRanker reads causal_chain_score
#   - mg_capacity_gaps seeds new investable themes in ThemeDetector
#   - PolicyIntelligence filters future targets (as_of_date guard, Change 3)
logger.info("\n=== STAGES 2+3: India Intelligence → Themes (per year) ===")
from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline

pipeline = IntelligencePipeline(config)
pipeline._init_storage()
pipeline._init_nlp()
pipeline._init_themes()
pipeline._init_intelligence()

REPLAY_DATES = [
    date(2020, 12, 31),
    date(2021, 12, 31),
    date(2022, 12, 31),
    date(2023, 12, 31),
    date(2024, 12, 31),
    date(2025, 12, 31),
    None,  # current / live
]

# Filter replay dates by --from-year if provided
if args.from_year:
    REPLAY_DATES = [
        d for d in REPLAY_DATES
        if d is None or d.year >= args.from_year
    ]
    logger.info(f"--from-year {args.from_year}: running {len(REPLAY_DATES)} snapshots")

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

import time as _time

for replay_date in REPLAY_DATES:
    label = str(replay_date) if replay_date else "CURRENT (live)"
    year_t0 = _time.time()

    logger.info(f"\n{'─'*60}")
    logger.info(f"  as_of={label}")
    logger.info(f"{'─'*60}")

    # Yearly replay: strict 365-day signal window so each year is independent.
    # Live mode uses 730 days to pick up multi-year structural themes.
    if replay_date is not None:
        pipeline.config.setdefault("themes", {})["signal_window_days"] = 365
    else:
        pipeline.config.setdefault("themes", {})["signal_window_days"] = 730

    # ── Stage 2: India Intelligence L1-L10 (per year) ─────────────────────
    # MUST run before themes — populates mg_causal_chains and mg_capacity_gaps
    # which ThemeRanker and ThemeDetector read during the themes stage.
    if not args.skip_intelligence:
        logger.info(f"  [Stage 2] India Intelligence L1-L10 as_of={label} ...")
        try:
            intel_stats = pipeline.run_india_intelligence(
                as_of_date=replay_date,      # None → today for live run
                pg_store=pipeline._pg_store,
            )
            logger.info(
                f"  [Stage 2] Done: "
                f"targets={intel_stats.get('policy_targets', 0)} "
                f"gaps={intel_stats.get('capacity_gaps', 0)} "
                f"localization={intel_stats.get('localization_opportunities', 0)} "
                f"beneficiaries={intel_stats.get('india_beneficiaries', 0)} "
                f"chains={intel_stats.get('causal_chains', 0)}"
            )
        except Exception as e:
            logger.warning(f"  [Stage 2] India Intelligence failed ({e}) — continuing to themes")
    else:
        logger.info(f"  [Stage 2] India Intelligence SKIPPED (--skip-intelligence)")

    # ── Stage 3: Theme detection snapshot ─────────────────────────────────
    logger.info(f"  [Stage 3] Themes as_of={label} ...")
    try:
        result = pipeline.run_themes(as_of_date=replay_date, country="IN")
        logger.info(
            f"  [Stage 3] Done: "
            f"detected={result.get('themes_detected', 0)} "
            f"ranked={result.get('themes_ranked', 0)} "
            f"beneficiaries={result.get('beneficiaries_mapped', 0)}"
        )
    except Exception as e:
        logger.error(f"  [Stage 3] Themes failed for {label}: {e}", exc_info=True)

    logger.info(f"  {label} complete in {_time.time()-year_t0:.1f}s")

logger.info("\n✓ All post-NLP stages complete.")
