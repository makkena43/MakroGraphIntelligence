#!/usr/bin/env python3
"""Materialise the upstream constraint/company research snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.makrograph.india.constraint_candidate_engine import ConstraintCandidateEngine  # noqa: E402
from src.makrograph.storage.pg_store import PGStore  # noqa: E402


def load_config() -> dict:
    with (PROJECT_ROOT / "config" / "settings.yaml").open() as handle:
        config = yaml.safe_load(handle)
    secrets_path = PROJECT_ROOT / "config" / "secrets.json"
    if secrets_path.exists():
        with secrets_path.open() as handle:
            secrets = json.load(handle)
        for section, values in secrets.items():
            if not section.startswith("_") and isinstance(values, dict):
                config.setdefault(section, {}).update(
                    {key: value for key, value in values.items() if value}
                )
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--country", default="IN")
    parser.add_argument("--lookback-days", type=int, default=540)
    args = parser.parse_args()
    config = load_config()
    pg = PGStore(config["postgresql"], skip_migrations=True)
    stats = ConstraintCandidateEngine(config).run(
        pg,
        as_of_date=args.as_of,
        country=args.country.upper(),
        lookback_days=args.lookback_days,
    )
    print(json.dumps(stats, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
