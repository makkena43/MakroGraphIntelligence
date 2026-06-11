#!/usr/bin/env python3
"""Run India theme detection for each historical year-end date.

Creates year-specific snapshots in mg_theme_snapshots so the ranking engine
can produce genuine year-specific rankings (not just filtered current data).

Run order: 2022 → 2023 → 2024 → 2025 → current (restores live state).
"""
import sys, yaml, json, logging
from datetime import date
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
sys.path.insert(0, ".")

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
    print(f"secrets: {e}")

from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline

pipeline = IntelligencePipeline(config)
pipeline._init_storage()

REPLAY_DATES = [
    date(2022, 12, 31),   # 2022 snapshot — uses 2021-01-01 to 2022-12-31 signals
    date(2023, 12, 31),   # 2023 snapshot — uses 2022-01-01 to 2023-12-31 signals
    date(2024, 12, 31),   # 2024 snapshot — uses 2023-01-01 to 2024-12-31 signals
    date(2025, 12, 31),   # 2025 snapshot — uses 2024-01-01 to 2025-12-31 signals
    None,                 # current / live — uses 2024-06-06 to 2026-06-06 signals
]

for replay_date in REPLAY_DATES:
    label = str(replay_date) if replay_date else "CURRENT (live)"
    print(f"\n{'='*60}")
    print(f"Running themes as_of={label}")
    print(f"{'='*60}")
    result = pipeline.run_themes(as_of_date=replay_date, country="IN")
    print(f"Done: {result}")

print("\nAll historical snapshots created. Rankings are now year-specific.")
