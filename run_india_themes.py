#!/usr/bin/env python3
import sys, yaml, json, logging
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
    print(f"secrets error: {e}")

pg_cfg = config.get("postgresql", {})
print(f"PG host: {pg_cfg.get('host')}, dbname: {pg_cfg.get('dbname')}")

from src.makrograph.storage.pg_store import PGStore
pg = PGStore(pg_cfg)
print(f"PGStore: {pg}")

from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
pipeline = IntelligencePipeline(config)
pipeline._init_storage()
print(f"pipeline._pg_store: {pipeline._pg_store}")

print("Running themes for IN...")
result = pipeline.run_themes(country="IN")
print(f"Themes result: {result}")
