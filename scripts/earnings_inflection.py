#!/usr/bin/env python3
"""Earnings Inflection Detector - explicit, run-once, research-only CLI.

Offline (default; no network, no database):
    python scripts/earnings_inflection.py --fixtures tests/earnings_inflection/fixtures \
        --ticker ACMEGRID --as-of 2024-10-31 --out data/earnings_inflection

Read-only production source (opt-in; DSN must point at a read-only role):
    EI_READONLY_DSN="postgresql://ro_user@host/makrograph" \
    python scripts/earnings_inflection.py --source postgres --preflight-only
    python scripts/earnings_inflection.py --source postgres --ticker XYZ --as-of 2025-06-30

This script never writes to existing tables, never schedules itself, never
sends notifications and never emits investment actions.  LLM extraction and
persistence stay off unless enabled in the config file AND, for persistence,
the target database name matches the configured test pattern.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from makrograph.earnings_inflection.contracts import to_jsonable  # noqa: E402
from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline  # noqa: E402
from makrograph.earnings_inflection.rendering import render_json, render_markdown  # noqa: E402
from makrograph.earnings_inflection.source_repository import (  # noqa: E402
    FixtureRepository, PostgresReadOnlyRepository,
)


def load_config(path):
    if not path:
        return {}
    import yaml
    data = yaml.safe_load(Path(path).read_text()) or {}
    return data.get("earnings_inflection", data)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="YAML config (see config/earnings_inflection.example.yaml)")
    ap.add_argument("--source", choices=["fixtures", "postgres"], default=None)
    ap.add_argument("--fixtures", help="fixture directory (implies --source fixtures)")
    ap.add_argument("--ticker", action="append", default=[], help="repeatable; required unless --preflight-only")
    ap.add_argument("--as-of", help="YYYY-MM-DD (end of day IST) or ISO timestamp")
    ap.add_argument("--out", help="output directory for <ticker>_<as_of>.json/.md (default: print markdown)")
    ap.add_argument("--preflight-only", action="store_true", help="report schema/coverage and exit")
    ap.add_argument("--diagnose", action="store_true",
                    help="show how each filing was parsed (columns, units, scope, rows) instead of assessing")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    src_cfg = cfg.get("source", {})
    kind = args.source or ("fixtures" if args.fixtures else src_cfg.get("kind", "fixtures"))
    if kind == "fixtures":
        path = args.fixtures or src_cfg.get("path") or str(ROOT / "tests/earnings_inflection/fixtures")
        repo = FixtureRepository(path)
    else:
        repo = PostgresReadOnlyRepository(dsn_env=src_cfg.get("dsn_env", "EI_READONLY_DSN"),
                                          text_root=src_cfg.get("text_root"))

    if args.preflight_only:
        print(json.dumps(to_jsonable(repo.preflight()), indent=2, default=str))
        return 0
    if not args.ticker or not args.as_of:
        ap.error("--ticker and --as-of are required (no implicit whole-market runs)")

    pipe = EarningsInflectionPipeline(cfg, repo)
    if args.diagnose:
        for t in args.ticker:
            print(pipe.diagnose(t, args.as_of))
        return 0
    result = pipe.run(args.ticker, args.as_of)
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    for a in result.assessments:
        md = render_markdown(a)
        if out:
            stem = f"{a.ticker}_{a.as_of.date().isoformat()}"
            (out / f"{stem}.md").write_text(md)
            (out / f"{stem}.json").write_text(render_json(a))
            print(f"{a.ticker}: {a.evidence_status.value} -> {out / stem}.md")
        else:
            print(md)
    for t, err in result.errors.items():
        print(f"ERROR {t}: {err}", file=sys.stderr)
    if cfg.get("persistence", {}).get("enabled"):
        n = pipe.persist(result)
        print(f"persisted {n} assessment(s) to test database")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
