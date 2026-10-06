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
from makrograph.earnings_inflection.text_artifacts import TextArtifactStore  # noqa: E402


def load_config(path):
    if not path:
        return {}
    import yaml
    data = yaml.safe_load(Path(path).read_text()) or {}
    return data.get("earnings_inflection", data)


def _replay(args, cfg, repo) -> int:
    """Monthly point-in-time replay, then a timeline scored in the outcome sandbox."""
    from datetime import date
    from makrograph.earnings_inflection.evaluation import earnings_delivery, thesis_timeline, timeline_report
    from makrograph.earnings_inflection.replay import month_ends, replay
    pipe = EarningsInflectionPipeline(cfg, repo)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(args.replay_from), date.fromisoformat(args.replay_to)
    for t in args.ticker:
        snaps = replay(pipe, t, month_ends(start, end))
        (out / f"{t}_replay.json").write_text(json.dumps(snaps, indent=1, default=str))
        tl = thesis_timeline(snaps)
        pipe.run([t], end.isoformat())               # series as known at the end of the replay window
        for ep in tl["episodes"]:
            ep.delivery = earnings_delivery(ep, pipe.last_series, data_until=end)
        (out / f"{t}_timeline.md").write_text(timeline_report(
            t, tl, f"Replay {start} -> {end}, month-ends; outcomes use data public by {end} only."))
        print(f"{t}: {len(snaps)} snapshots; earliest defensible signal {tl['earliest_defensible_signal']}; "
              f"leading episodes {tl['leading_episodes']} (validated {tl['validated']}, false alarms "
              f"{tl['false_alarms']}) -> {out / (t + '_timeline.md')}")
    return 0


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
    ap.add_argument("--artifact-root",
                    help="versioned text-artifact store (local directory); read by assessments, written by --extract")
    ap.add_argument("--extract", action="store_true",
                    help="explicit, bounded extraction of PDF-only documents into --artifact-root "
                         "(local originals only; no network, no database writes)")
    ap.add_argument("--max-docs", type=int, default=50, help="--extract: maximum documents this run")
    ap.add_argument("--ocr", choices=["none", "ocrmypdf"], default="none",
                    help="--extract: local OCR fallback for scanned PDFs (default none; ocrmypdf must be installed)")
    ap.add_argument("--universe", help="WP8: eligibility snapshot JSON; runs a bounded universe scan instead of tickers")
    ap.add_argument("--runs-root", default="data/earnings_inflection/runs",
                    help="universe: parent of immutable run directories")
    ap.add_argument("--resume", help="universe: run directory of an interrupted run to continue")
    ap.add_argument("--previous", help="universe: earlier run directory; reassess only issuers with new documents")
    ap.add_argument("--max-issuers", type=int, help="universe: hard cap on issuers assessed (others deferred)")
    ap.add_argument("--page-size", type=int, help="universe: issuers per checkpointed page")
    ap.add_argument("--replay-from", help="point-in-time replay: first as-of date (month-ends to --replay-to)")
    ap.add_argument("--replay-to", help="point-in-time replay: last as-of date; also the outcome data cut-off")
    ap.add_argument("--replay-manifest",
                    help="manifest.json of an earlier run: read exactly the text versions that run used")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    src_cfg = cfg.get("source", {})
    artifact_root = args.artifact_root or src_cfg.get("artifact_root")
    store = TextArtifactStore(artifact_root) if artifact_root else None
    kind = args.source or ("fixtures" if args.fixtures else src_cfg.get("kind", "fixtures"))
    if kind == "fixtures":
        path = args.fixtures or src_cfg.get("path") or str(ROOT / "tests/earnings_inflection/fixtures")
        repo = FixtureRepository(path, artifact_store=store)
    else:
        repo = PostgresReadOnlyRepository(dsn_env=src_cfg.get("dsn_env", "EI_READONLY_DSN"),
                                          text_root=src_cfg.get("text_root"), artifact_store=store)
    if args.replay_manifest:
        manifest = json.loads(Path(args.replay_manifest).read_text())
        cfg["pinned_extractions"] = EarningsInflectionPipeline.pins_from_manifest(manifest)
        cfg.setdefault("replay_mode", manifest.get("replay_mode", "PUBLIC_INFORMATION_RECONSTRUCTION"))

    if args.preflight_only:
        print(json.dumps(to_jsonable(repo.preflight()), indent=2, default=str))
        return 0
    if args.replay_from or args.replay_to:
        if not (args.replay_from and args.replay_to and args.ticker and args.out):
            ap.error("--replay-from, --replay-to, --ticker and --out are required together")
        return _replay(args, cfg, repo)
    if not args.as_of or (not args.ticker and not args.universe):
        ap.error("--as-of and either --ticker or --universe <snapshot> are required (no implicit whole-market runs)")

    if args.extract:
        if store is None:
            ap.error("--extract needs --artifact-root (where versioned text artifacts are written)")
        from makrograph.earnings_inflection.backfill import extract_missing_text
        from makrograph.parser.pdf_parser import PDFParser
        parser = PDFParser({**cfg.get("parser", {}), "output_dir": str(Path(artifact_root) / "_parser_tmp")})
        ocr = None
        if args.ocr == "ocrmypdf":
            from makrograph.parser.text_artifacts import LocalOcrmypdfProvider
            ocr = LocalOcrmypdfProvider(parser)
        from makrograph.earnings_inflection.pipeline import as_of_datetime
        report = extract_missing_text(repo, args.ticker, cfg.get("country", "IN"), as_of_datetime(args.as_of),
                                      store, parser, ocr=ocr, max_docs=args.max_docs,
                                      text_root=src_cfg.get("text_root"))
        print(json.dumps(report, indent=2, default=str))
        return 0

    from makrograph.earnings_inflection.llm import LLMPreflightError
    try:
        pipe = EarningsInflectionPipeline(cfg, repo)
    except LLMPreflightError as e:
        print(f"LLM preflight failed (no company processed): {e}", file=sys.stderr)
        return 2
    if args.universe:
        from makrograph.earnings_inflection.discovery import EligibilitySnapshot, UniverseScanner
        dcfg = dict(cfg.get("discovery", {}) or {})
        if args.max_issuers:
            dcfg["max_issuers"] = args.max_issuers
        if args.page_size:
            dcfg["page_size"] = args.page_size
        scan = UniverseScanner(pipe, repo, dcfg).run(
            EligibilitySnapshot.load(args.universe), args.as_of, Path(args.runs_root),
            resume=Path(args.resume) if args.resume else None,
            previous=Path(args.previous) if args.previous else None)
        print(json.dumps({"run_id": scan.run_id, "status": scan.status, "counts": scan.counts,
                          "shortlist": str(scan.run_dir / "shortlist.md")}, indent=1))
        return 0
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
    if out:
        (out / "manifest.json").write_text(json.dumps(result.manifest, indent=1, default=str))
        print(f"run {result.manifest['run_id']}: {result.status} -> {out / 'manifest.json'}")
    for t, err in result.errors.items():
        print(f"ERROR {t}: {err}", file=sys.stderr)
    if cfg.get("persistence", {}).get("enabled"):
        n = pipe.persist(result)
        print(f"persisted {n} assessment(s) to test database")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
