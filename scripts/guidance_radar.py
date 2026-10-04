#!/usr/bin/env python3
"""Run the independent management guidance-to-execution radar."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from makrograph.cli import load_config
from makrograph.guidance_radar.extractor import AnthropicJSONClient, HybridDisclosureExtractor
from makrograph.guidance_radar.pipeline import GuidanceRadar
from makrograph.guidance_radar.repository import GuidanceRadarRepository
from makrograph.storage.pg_store import PGStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Point-in-time guidance, execution, governance, valuation and timing radar",
    )
    parser.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--tickers", default="", help="comma-separated NSE symbols; empty scans the market")
    parser.add_argument("--lookback-days", type=int, default=550)
    parser.add_argument("--max-candidates", type=int, default=80)
    parser.add_argument("--max-docs-per-ticker", type=int, default=14)
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    pg_store = PGStore(config.get("postgresql", {}), skip_migrations=True)
    repository = GuidanceRadarRepository(pg_store)
    llm_client = None
    if args.use_llm:
        acfg = config.get("anthropic", {})
        llm_client = AnthropicJSONClient(
            api_key=acfg.get("api_key", ""),
            model=acfg.get("model", "claude-sonnet-4-6"),
            max_tokens=int(acfg.get("max_tokens", 6000)),
            temperature=0.0,
        )
    radar = GuidanceRadar(repository, HybridDisclosureExtractor(llm_client))
    tickers = [value.strip().upper() for value in args.tickers.split(",") if value.strip()] or None
    try:
        decisions = radar.run(
            as_of_date=args.as_of,
            tickers=tickers,
            lookback_days=args.lookback_days,
            max_candidates=args.max_candidates,
            max_docs_per_ticker=args.max_docs_per_ticker,
            persist=not args.no_persist,
        )
    finally:
        pg_store.close()

    if args.json:
        print(json.dumps([decision.to_dict() for decision in decisions], indent=2, default=str))
        return 0

    print(f"\nGUIDANCE RADAR — AS OF {args.as_of.isoformat()}")
    print("action          score ticker          cap%  evidence  missing")
    for row in decisions:
        missing = "; ".join(row.missing_proof[:2]) or "none"
        print(f"{row.action.value:<15} {row.score:>5.1f} {row.ticker:<15} "
              f"{row.max_position_pct:>4.1f} {row.evidence_dates:>9}  {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
