#!/usr/bin/env python3
"""India Pipeline — Year-by-Year Historical Runner.

Runs all required stages for India, year by year:

  For each year (2021 → 2022 → 2023 → 2024 → 2025 → current):
    Stage 1  run_nlp(country='IN', window)          — entity + signal extraction
    Stage 2  run_india_intelligence(as_of_date)     — Layers 1-10 (policy, gaps,
                                                       import dep, supply chain,
                                                       beneficiaries, tenders,
                                                       order book, causal chains)
    Stage 3  run_themes(as_of_date, country='IN')   — detect + rank themes with
                                                       all new signals + chains

Stages that already ran (ingest, PDF fetch, embeddings) are skipped here.
Use --from-year to resume from a specific year after an interruption.

Usage:
    python3 run_india_yearly.py                    # all years 2021-current
    python3 run_india_yearly.py --from-year 2023   # resume from 2023
    python3 run_india_yearly.py --year 2024        # single year only
    python3 run_india_yearly.py --skip-nlp         # skip NLP (already done)
    python3 run_india_yearly.py --dry-run          # print plan, don't execute
"""

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("india_yearly")
sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
import yaml

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
    logger.warning(f"secrets.json: {e}")

# ---------------------------------------------------------------------------
# Year plan
# ---------------------------------------------------------------------------

def _year_window(year: int) -> tuple[date, date, date]:
    """Return (window_start, window_end, as_of_date) for a given year.

    window_start  = Jan 1 of that year  (NLP processes docs filed in this range)
    window_end    = Dec 31 of that year
    as_of_date    = Dec 31 of that year  (theme snapshot anchored here)

    For the current year (2026) use today as the ceiling.
    """
    today = date.today()
    ws = date(year, 1, 1)
    we = date(year, 12, 31)
    if we > today:
        we = today
    return ws, we, we


# Years to run, in order. Edit to add/remove years.
ALL_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="India yearly pipeline runner")
parser.add_argument("--from-year",  type=int, default=None,
                    help="Skip years before this year (resume mode)")
parser.add_argument("--year",       type=int, default=None,
                    help="Run a single year only")
parser.add_argument("--skip-nlp",   action="store_true",
                    help="Skip NLP stage (already processed)")
parser.add_argument("--skip-intelligence", action="store_true",
                    help="Skip run_india_intelligence() (Layers 1-10)")
parser.add_argument("--skip-themes", action="store_true",
                    help="Skip theme detection/snapshot")
parser.add_argument("--dry-run",    action="store_true",
                    help="Print the plan and exit without executing")
args = parser.parse_args()

# Build year list
if args.year:
    years = [args.year]
elif args.from_year:
    years = [y for y in ALL_YEARS if y >= args.from_year]
else:
    years = ALL_YEARS

# ---------------------------------------------------------------------------
# Print plan
# ---------------------------------------------------------------------------

print(f"\n{'='*65}")
print(f"  INDIA PIPELINE — YEAR-BY-YEAR RUNNER")
print(f"{'='*65}")
print(f"  Years      : {years}")
print(f"  Skip NLP   : {args.skip_nlp}")
print(f"  Skip Intel : {args.skip_intelligence}")
print(f"  Skip Themes: {args.skip_themes}")
print(f"  Stages per year:")
for y in years:
    ws, we, as_of = _year_window(y)
    label = "current" if y == date.today().year else str(as_of)
    print(f"    {y}: window [{ws} → {we}]  as_of={label}")
print(f"{'='*65}\n")

if args.dry_run:
    print("DRY RUN — exiting without executing.")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Pipeline init
# ---------------------------------------------------------------------------

from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline

pipeline = IntelligencePipeline(config)
pipeline._init_storage()

if not pipeline._pg_store:
    logger.error("PostgreSQL not configured or unreachable. Aborting.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Per-year execution
# ---------------------------------------------------------------------------

overall_start = time.time()
year_results: list[dict] = []

for year in years:
    ws, we, as_of = _year_window(year)
    label = f"{year} ({ws} → {we})"
    year_start = time.time()

    print(f"\n{'─'*65}")
    print(f"  YEAR {year}  as_of={as_of}")
    print(f"{'─'*65}")

    result: dict = {"year": year, "as_of": str(as_of)}

    # ── Stage 1: NLP ────────────────────────────────────────────────────────
    if not args.skip_nlp:
        print(f"\n  [Stage 1] NLP  ({ws} → {we}) ...")
        try:
            pipeline._init_nlp()
            nlp_stats = pipeline.run_nlp(
                batch_size=500,
                window_start=ws,
                window_end=we,
                country="IN",
            )
            result["nlp"] = nlp_stats
            print(f"  [Stage 1] NLP done: {nlp_stats.get('docs_processed',0)} docs, "
                  f"{nlp_stats.get('signals_found',0)} signals")
        except Exception as e:
            logger.error(f"[Stage 1] NLP failed for {year}: {e}", exc_info=True)
            result["nlp_error"] = str(e)
    else:
        print(f"  [Stage 1] NLP skipped (--skip-nlp)")
        result["nlp"] = "skipped"

    # ── Stage 2: India Intelligence (Layers 1–10) ────────────────────────────
    if not args.skip_intelligence:
        print(f"\n  [Stage 2] India Intelligence (Layers 1-10) as_of={as_of} ...")
        try:
            intel_stats = pipeline.run_india_intelligence(
                as_of_date=as_of,
                lookback_days=365,   # one full year lookback per snapshot
            )
            result["intelligence"] = intel_stats
            print(f"  [Stage 2] Intelligence done:")
            print(f"    Policy targets        : {intel_stats.get('policy_targets', 0)}")
            print(f"    Capacity gaps         : {intel_stats.get('capacity_gaps', 0)}")
            print(f"    Import dependencies   : {intel_stats.get('import_dependencies', 0)}")
            print(f"    Localization opps     : {intel_stats.get('localization_opportunities', 0)}")
            print(f"    Beneficiaries         : {intel_stats.get('beneficiaries_discovered', 0)}")
            print(f"    Order book signals    : {intel_stats.get('order_book_signals_generated', 0)}")
            print(f"    Causal chains         : {intel_stats.get('causal_chains_persisted', 0)}")
            if intel_stats.get("errors"):
                logger.warning(f"  [Stage 2] Non-fatal errors: {intel_stats['errors']}")
        except Exception as e:
            logger.error(f"[Stage 2] India Intelligence failed for {year}: {e}", exc_info=True)
            result["intelligence_error"] = str(e)
    else:
        print(f"  [Stage 2] Intelligence skipped (--skip-intelligence)")
        result["intelligence"] = "skipped"

    # ── Stage 3: Theme Detection + Snapshot ──────────────────────────────────
    if not args.skip_themes:
        print(f"\n  [Stage 3] Themes as_of={as_of} ...")
        try:
            pipeline._init_themes()
            pipeline._init_intelligence()
            theme_stats = pipeline.run_themes(as_of_date=as_of, country="IN")
            result["themes"] = theme_stats
            print(f"  [Stage 3] Themes done: "
                  f"{theme_stats.get('themes_detected', 0)} detected, "
                  f"{theme_stats.get('themes_ranked', 0)} ranked, "
                  f"{theme_stats.get('beneficiaries_mapped', 0)} beneficiaries")
        except Exception as e:
            logger.error(f"[Stage 3] Themes failed for {year}: {e}", exc_info=True)
            result["themes_error"] = str(e)
    else:
        print(f"  [Stage 3] Themes skipped (--skip-themes)")
        result["themes"] = "skipped"

    result["duration_sec"] = round(time.time() - year_start, 1)
    year_results.append(result)
    print(f"\n  Year {year} complete in {result['duration_sec']}s")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

total_sec = round(time.time() - overall_start, 1)

print(f"\n{'='*65}")
print(f"  INDIA YEARLY RUN COMPLETE  ({total_sec}s total)")
print(f"{'='*65}")
print(f"  {'Year':<6} {'NLP docs':>9} {'Gaps':>5} {'Themes':>7} {'Chains':>7} {'Dur(s)':>7}")
print(f"  {'─'*6} {'─'*9} {'─'*5} {'─'*7} {'─'*7} {'─'*7}")
for r in year_results:
    nlp_docs  = r.get("nlp", {}).get("docs_processed", "-") if isinstance(r.get("nlp"), dict) else "-"
    gaps      = r.get("intelligence", {}).get("capacity_gaps", "-") if isinstance(r.get("intelligence"), dict) else "-"
    themes    = r.get("themes", {}).get("themes_ranked", "-") if isinstance(r.get("themes"), dict) else "-"
    chains    = r.get("intelligence", {}).get("causal_chains_persisted", "-") if isinstance(r.get("intelligence"), dict) else "-"
    dur       = r.get("duration_sec", "-")
    print(f"  {r['year']:<6} {str(nlp_docs):>9} {str(gaps):>5} {str(themes):>7} {str(chains):>7} {str(dur):>7}")
print(f"{'='*65}\n")
