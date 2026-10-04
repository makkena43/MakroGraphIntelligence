#!/usr/bin/env python3
"""Run a multi-company, multi-anchor point-in-time radar evaluation.

Input is a JSON array of {"ticker": "...", "as_of_date": "YYYY-MM-DD"}.
Case selection is deliberately external to the scoring code so validation sets
can be frozen before model/rule changes and include both winners and failures.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from makrograph.cli import load_config
from makrograph.guidance_radar.backtest import PostgresPriceProvider, WalkForwardEvaluator
from makrograph.guidance_radar.extractor import AnthropicJSONClient, HybridDisclosureExtractor
from makrograph.guidance_radar.pipeline import GuidanceRadar
from makrograph.guidance_radar.repository import GuidanceRadarRepository
from makrograph.storage.pg_store import PGStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Blind multi-anchor guidance-radar walk-forward")
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--through", required=True, type=date.fromisoformat)
    parser.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--benchmark-symbol", default="JUNIORBEES")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows = json.loads(args.cases.read_text(encoding="utf-8"))
    cases: dict[date, list[str]] = defaultdict(list)
    for row in rows:
        cases[date.fromisoformat(row["as_of_date"])].append(row["ticker"].upper())

    config = load_config(args.config)
    pg_store = PGStore(config.get("postgresql", {}), skip_migrations=True)
    repository = GuidanceRadarRepository(pg_store)
    client = None
    if args.use_llm:
        acfg = config.get("anthropic", {})
        client = AnthropicJSONClient(
            api_key=acfg.get("api_key", ""),
            model=acfg.get("model", "claude-sonnet-4-6"),
            max_tokens=int(acfg.get("max_tokens", 6000)),
            temperature=0.0,
        )
    radar = GuidanceRadar(repository, HybridDisclosureExtractor(client))

    decisions = []
    try:
        for anchor, tickers in sorted(cases.items()):
            decisions.extend(radar.run(
                as_of_date=anchor,
                tickers=tickers,
                max_candidates=len(tickers),
                persist=args.persist,
            ))
        evaluator = WalkForwardEvaluator(
            PostgresPriceProvider(pg_store, benchmark_symbol=args.benchmark_symbol)
        )
        outcomes = evaluator.evaluate(decisions, through_date=args.through)
        result = {
            "method": {
                "entry": "first trading-session close strictly after the disclosure date",
                "benchmark": f"{args.benchmark_symbol} price-return ETF proxy",
                "corporate_actions": "exclude series with >=45% one-day discontinuity",
                "case_file": str(args.cases),
            },
            "decisions": [d.to_dict() for d in decisions],
            "outcomes": [o.to_dict() for o in outcomes],
            "summary": evaluator.summarize(outcomes),
        }
    finally:
        pg_store.close()

    rendered = json.dumps(result, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
