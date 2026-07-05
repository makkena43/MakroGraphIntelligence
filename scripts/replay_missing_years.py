"""
replay_missing_years.py
───────────────────────
Runs the historical pipeline for every year that is missing a Dec-31 theme
snapshot.  Documents + NLP are already in the DB — only graph, causal chains,
and themes are re-computed.

Usage:
    python scripts/replay_missing_years.py              # both countries
    python scripts/replay_missing_years.py --country US
    python scripts/replay_missing_years.py --country IN
"""

import sys
import os
import argparse
import logging
from datetime import date
from pathlib import Path

# ── project root on path ──────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml
from makrograph.pipeline.historical_runner import HistoricalRunner

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("replay")

# ── config ────────────────────────────────────────────────────────────────────
with open(ROOT / "config" / "settings.yaml") as f:
    CFG = yaml.safe_load(f)

# Override password from env if set
pg_pass = os.environ.get("MAKROGRAPH_PG_PASSWORD", "")
if pg_pass:
    CFG.setdefault("postgresql", {})["password"] = pg_pass

# ── years available in the UI ─────────────────────────────────────────────────
ALL_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]


def get_existing_year_snapshots(country: str) -> set[int]:
    """Return years that already have a Dec-31 snapshot for this country."""
    from makrograph.storage.pg_store import PGStore
    pg = PGStore(CFG.get("postgresql", {}), skip_migrations=True)
    with pg._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT EXTRACT(YEAR FROM s.snapshot_date)::int
                FROM mg_theme_snapshots s
                JOIN mg_themes t ON t.id = s.theme_id
                WHERE t.country = %s
                  AND EXTRACT(MONTH FROM s.snapshot_date) = 12
                  AND EXTRACT(DAY   FROM s.snapshot_date) = 31
            """, (country,))
            return {r[0] for r in cur.fetchall()}


def run_year(country: str, year: int, cfg: dict) -> None:
    run_cfg = {**cfg}
    run_cfg.setdefault("market", {})["country"] = country

    start = date(year, 1, 1)
    end   = date(year, 12, 31)

    log.info("=" * 70)
    log.info(f"  Starting replay: {country}  {year}  ({start} → {end})")
    log.info("=" * 70)

    runner = HistoricalRunner(
        config=run_cfg,
        start_date=start,
        end_date=end,
        replay_mode="monthly",   # produces quarterly snapshots at each month-end
        skip_ingest=True,        # docs already in DB
        skip_nlp=True,           # NLP already done
        skip_graph=False,        # build entity graph
        skip_events=False,       # extract events
        skip_causal=False,       # score causal chains
        skip_themes=False,       # detect + rank themes → snapshots
        skip_pdf_fetch=True,     # no new PDF fetch
        skip_neo4j=True,         # skip Neo4j to keep replay fast
    )

    results = runner.run()

    ok  = sum(1 for r in results if not r.errors)
    err = sum(1 for r in results if r.errors)
    log.info(f"  {country} {year}: {len(results)} months processed — {ok} OK, {err} with errors")
    if err:
        for r in results:
            if r.errors:
                log.warning(f"    {r.window_end}: {r.errors}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--country", choices=["US", "IN", "both"], default="both")
    args = parser.parse_args()

    countries = (
        ["US", "IN"] if args.country == "both"
        else [args.country]
    )

    for country in countries:
        existing = get_existing_year_snapshots(country)
        missing  = sorted(y for y in ALL_YEARS if y not in existing)

        if not missing:
            log.info(f"{country}: all years already have Dec-31 snapshots — nothing to do.")
            continue

        log.info(f"{country}: existing year-end snapshots → {sorted(existing)}")
        log.info(f"{country}: missing years to replay     → {missing}")

        for year in missing:
            try:
                run_year(country, year, CFG)
            except Exception as e:
                log.error(f"  {country} {year} FAILED: {e}", exc_info=True)
                log.info("  Continuing with next year…")

    log.info("All done.")


if __name__ == "__main__":
    main()
