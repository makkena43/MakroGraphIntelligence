#!/usr/bin/env python3
"""Run India causal chain discovery + beneficiary ranking for each year snapshot.

Fixes two gaps:
  1. Per-year chain variation — chains get first_detected anchored to the year
     they first emerged in data, so the UI year-selector shows meaningful changes.
  2. Per-year beneficiary/shortlisted data — India ranking runs for each year-end
     so the Shortlisted tab has historical data, not just today's snapshot.

Runs for: 2020, 2021, 2022, 2023, 2024, 2025, and current date.

Usage:
  python scripts/run_india_yearly_chains.py [--years 2022,2023,2024]
"""
import sys, yaml, json, logging, argparse
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("india_yearly_chains")

sys.path.insert(0, ".")

parser = argparse.ArgumentParser()
parser.add_argument("--years", default=None,
                    help="Comma-separated years to run e.g. 2022,2023 (default: all 2020–current)")
args = parser.parse_args()

# ── Config ────────────────────────────────────────────────────────────────────
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

# ── Year list ─────────────────────────────────────────────────────────────────
current_year = date.today().year
if args.years:
    years = [int(y.strip()) for y in args.years.split(",")]
else:
    years = list(range(2020, current_year + 1))

# Use Dec 31 for historical years, today for current year
year_dates = []
for y in years:
    if y < current_year:
        year_dates.append(date(y, 12, 31))
    else:
        year_dates.append(date.today())

logger.info(f"Running India yearly chains for: {[str(d) for d in year_dates]}")

# ── Storage ───────────────────────────────────────────────────────────────────
from src.makrograph.storage.pg_store import PGStore
pg_store = PGStore(config)

# ── Per-year loop ─────────────────────────────────────────────────────────────
for as_of in year_dates:
    logger.info(f"\n{'='*60}")
    logger.info(f"Processing year snapshot: {as_of}")
    logger.info(f"{'='*60}")

    # ── Step 1: Causal chain discovery ───────────────────────────────────────
    logger.info(f"[{as_of.year}] Running causal chain discovery...")
    try:
        from src.makrograph.india.causal_chain_generator import IndiaCausalChainGenerator
        chain_gen = IndiaCausalChainGenerator(config)
        saved = chain_gen.score_and_persist(pg_store, as_of_date=as_of)
        logger.info(f"[{as_of.year}] Causal chains: {saved} persisted")
    except Exception as e:
        logger.error(f"[{as_of.year}] Causal chain stage failed: {e}", exc_info=True)

    # ── Step 2: India beneficiary discovery (shortlisted stocks) ─────────────
    logger.info(f"[{as_of.year}] Running India beneficiary discovery...")
    try:
        from src.makrograph.india.beneficiary_discovery import BeneficiaryDiscoveryLayer as IndiaBeneficiaryDiscovery

        # Get active themes for this year window
        from psycopg2.extras import RealDictCursor
        from datetime import timedelta
        floor = as_of - timedelta(days=365)

        with pg_store._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT DISTINCT theme_name FROM mg_themes
                    WHERE country = 'IN'
                      AND is_active = TRUE
                      AND (first_detected <= %s OR first_detected IS NULL)
                      AND strength_score >= 40
                    ORDER BY theme_name
                """, (as_of,))
                theme_names = [r["theme_name"] for r in cur.fetchall()]

        logger.info(f"[{as_of.year}] {len(theme_names)} active themes to rank")

        if theme_names:
            discovery = IndiaBeneficiaryDiscovery(config)
            beneficiaries = discovery.discover(
                theme_names=theme_names,
                pg_store=pg_store,
                as_of_date=as_of,
                lookback_days=365,
            )
            logger.info(f"[{as_of.year}] Discovered {len(beneficiaries)} beneficiaries")

            # Persist to mg_india_beneficiaries
            if beneficiaries:
                discovery.persist(beneficiaries, pg_store, as_of_date=as_of)
                logger.info(f"[{as_of.year}] Persisted {len(beneficiaries)} beneficiaries")
    except Exception as e:
        logger.error(f"[{as_of.year}] Beneficiary stage failed: {e}", exc_info=True)

logger.info("\nIndia yearly chains + beneficiaries complete.")
