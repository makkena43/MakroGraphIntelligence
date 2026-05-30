"""Pipeline runner — executes available stages given installed packages.

Stages attempted (in order):
    1. ingest     — EDGAR fetch + PDF parse + PostgreSQL store  (always)
    2. nlp        — entity + signal extraction (rule-based, no spaCy required)
    3. embeddings — skipped if sentence-transformers missing
    4. graph      — skipped if neo4j missing
    5. themes     — seed-based theme detection from signals (always)
    6. report     — print active theme summary

Usage:
    .venv/bin/python run_pipeline.py
    .venv/bin/python run_pipeline.py --stage ingest
    .venv/bin/python run_pipeline.py --stage themes
    .venv/bin/python run_pipeline.py --report
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# ── ensure src/ is on the path ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent / "src"))

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("makrograph.runner")


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_pg_store(cfg: dict):
    from makrograph.storage.pg_store import PGStore
    pg_cfg = cfg.get("postgresql", {})
    if not pg_cfg.get("host"):
        logger.error("postgresql.host not set in settings.yaml")
        return None
    return PGStore(pg_cfg)


def run_ingest(pipeline, cfg: dict) -> dict:
    logger.info("=" * 60)
    logger.info("STAGE: INGEST — EDGAR SEC Filings")
    logger.info("=" * 60)
    stats = pipeline.run_ingest()
    logger.info(f"Ingest result: {stats}")
    return stats


def run_nlp(pipeline, cfg: dict, batch_size: int = 500, loop: bool = False) -> dict:
    logger.info("=" * 60)
    logger.info("STAGE: NLP — Entity + Signal Extraction")
    logger.info("=" * 60)
    if not loop:
        stats = pipeline.run_nlp(batch_size=batch_size)
        logger.info(f"NLP result: {stats}")
        return stats

    # Loop until all fetched docs are processed
    total = {"docs_processed": 0, "entities_found": 0, "signals_found": 0, "docs_failed": 0, "duration_sec": 0.0}
    iteration = 0
    while True:
        iteration += 1
        stats = pipeline.run_nlp(batch_size=batch_size)
        total["docs_processed"] += stats.get("docs_processed", 0)
        total["entities_found"] += stats.get("entities_found", 0)
        total["signals_found"] += stats.get("signals_found", 0)
        total["docs_failed"] += stats.get("docs_failed", 0)
        total["duration_sec"] += stats.get("duration_sec", 0.0)
        in_batch = stats.get("docs_in_batch", stats.get("docs_processed", 0) + stats.get("docs_failed", 0))
        logger.info(
            f"  Batch {iteration}: +{stats.get('docs_processed',0)} done / "
            f"+{stats.get('docs_failed',0)} failed / "
            f"+{stats.get('signals_found',0)} signals  "
            f"[total: {total['docs_processed']} docs, {total['signals_found']} signals]"
        )
        if in_batch == 0:
            break
    logger.info(f"NLP bulk complete: {total}")
    return total


def run_themes(pipeline, cfg: dict) -> dict:
    logger.info("=" * 60)
    logger.info("STAGE: THEMES — Detection + Ranking + Beneficiaries")
    logger.info("=" * 60)
    stats = pipeline.run_themes()
    logger.info(f"Themes result: {stats}")
    return stats


def print_report(pg_store) -> None:
    logger.info("=" * 60)
    logger.info("ACTIVE INVESTMENT THEMES")
    logger.info("=" * 60)
    themes = pg_store.get_active_themes(min_strength=0.0)
    if not themes:
        logger.info("No themes detected yet. Run ingest + nlp + themes stages first.")
        return

    for i, t in enumerate(themes[:20], 1):
        sectors = ", ".join(t.get("sectors") or []) or "—"
        print(
            f"\n  {i:>2}. {t['theme_name']}\n"
            f"       Conviction : {t['conviction']}\n"
            f"       Strength   : {t['strength_score']:.1f}\n"
            f"       Docs       : {t['doc_count']}   Companies: {t['company_count']}\n"
            f"       Sectors    : {sectors}"
        )
        if t.get("hypothesis_text"):
            print(f"       Hypothesis : {t['hypothesis_text'][:200]}")

    logger.info(f"\nTotal active themes: {len(themes)}")


def print_signals_summary(pg_store) -> None:
    logger.info("=" * 60)
    logger.info("RECENT SIGNALS SUMMARY (last 90 days)")
    logger.info("=" * 60)
    signal_types = [
        "capex_increase", "demand_surge", "technology_adoption",
        "supply_bottleneck", "regulatory_tailwind", "partnership_formed",
    ]
    for stype in signal_types:
        sigs = pg_store.get_signals_by_type(stype, days=90)
        if sigs:
            companies = list({s.get("company", "") for s in sigs if s.get("company")})[:5]
            print(f"  {stype:<30} {len(sigs):>4} signals | {', '.join(companies)}")


def main():
    parser = argparse.ArgumentParser(description="MakroGraph Intelligence Pipeline")
    parser.add_argument("--stage", choices=["ingest", "nlp", "themes", "all"], default="all",
                        help="Which stage to run (default: all)")
    parser.add_argument("--bulk", action="store_true",
                        help="NLP: loop batches until all fetched docs are processed")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="NLP: documents per batch (default: 500)")
    parser.add_argument("--report", action="store_true", help="Print theme report only")
    parser.add_argument("--signals", action="store_true", help="Print signals summary")
    parser.add_argument("--config", default="config/settings.yaml", help="Config path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger.info(f"MakroGraph Intelligence Pipeline — {cfg.get('project', {}).get('version', '?')}")

    # ── report only ──────────────────────────────────────────────────────────
    if args.report or args.signals:
        pg = build_pg_store(cfg)
        if pg:
            if args.report:
                print_report(pg)
            if args.signals:
                print_signals_summary(pg)
            pg.close()
        return

    # ── import pipeline ──────────────────────────────────────────────────────
    from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline

    t0 = time.time()
    with IntelligencePipeline(cfg) as pipeline:
        pipeline._init_storage()
        pipeline._init_nlp()
        pipeline._init_themes()

        all_stats = {}

        if args.stage in ("ingest", "all"):
            all_stats["ingest"] = run_ingest(pipeline, cfg)

        if args.stage in ("nlp", "all"):
            all_stats["nlp"] = run_nlp(pipeline, cfg, batch_size=args.batch_size, loop=args.bulk)

        if args.stage in ("themes", "all"):
            all_stats["themes"] = run_themes(pipeline, cfg)

        # ── print final report ────────────────────────────────────────────
        if pipeline._pg_store:
            print_report(pipeline._pg_store)
            print_signals_summary(pipeline._pg_store)

    elapsed = time.time() - t0
    logger.info(f"\nPipeline complete in {elapsed:.1f}s")
    logger.info(f"Stage stats: {all_stats}")


if __name__ == "__main__":
    main()
