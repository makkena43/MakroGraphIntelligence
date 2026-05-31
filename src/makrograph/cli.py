"""MakroGraph Intelligence - Incremental Batch CLI.

No daemons. No polling. No always-on services.
Run → Fetch new since last checkpoint → Process → Update checkpoint → Exit.
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml


def setup_logging(level: str = "INFO", log_file: str = ""):
    """Configure logging for the pipeline."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=getattr(logging, level.upper()), format=log_format, handlers=handlers)
    # Suppress Neo4j driver notification spam (UNRECOGNIZED Cypher warnings from older Neo4j versions)
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


def load_config(config_path: str = "config/settings.yaml") -> dict:
    """Load configuration from YAML file with environment variable overrides.

    API keys and passwords in settings.yaml can be left blank ("") and set
    via environment variables instead. Mapping:
        neo4j.password        ← MAKROGRAPH_NEO4J_PASSWORD
        postgresql.password   ← MAKROGRAPH_PG_PASSWORD
        gemini.api_key        ← GEMINI_API_KEY
        fred.api_key          ← FRED_API_KEY
        eia.api_key           ← EIA_API_KEY
        congress.api_key      ← CONGRESS_API_KEY
    """
    import os
    path = Path(config_path)
    if not path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(path) as f:
        cfg = yaml.safe_load(f)

    # Resolve secrets from environment variables (env var wins over yaml value)
    _env_overrides = {
        ("neo4j",       "password"):  "MAKROGRAPH_NEO4J_PASSWORD",
        ("postgresql",  "password"):  "MAKROGRAPH_PG_PASSWORD",
        ("gemini",      "api_key"):   "GEMINI_API_KEY",
        ("fred",        "api_key"):   "FRED_API_KEY",
        ("eia",         "api_key"):   "EIA_API_KEY",
        ("congress",    "api_key"):   "CONGRESS_API_KEY",
    }
    for (section, key), env_var in _env_overrides.items():
        val = os.environ.get(env_var)
        if val:
            cfg.setdefault(section, {})[key] = val

    return cfg


def cmd_run(args, config):
    """Run an incremental batch — fetch new docs since last checkpoint, process, exit."""
    from .pipeline.runner import BatchRunner

    pipeline_cfg = config.get("pipeline", {})
    setup_logging(
        level=args.log_level or pipeline_cfg.get("log_level", "INFO"),
        log_file=pipeline_cfg.get("log_file", ""),
    )
    logger = logging.getLogger(__name__)

    with BatchRunner(config) as runner:
        source_name = args.source or "_adhoc"

        if args.directory:
            logger.info(f"Batch run '{source_name}' from directory: {args.directory}")
            stats = runner.run_directory(
                source_name=source_name,
                directory=Path(args.directory),
                pattern=args.pattern or "*.pdf",
            )
        elif args.urls_file:
            urls_path = Path(args.urls_file)
            if not urls_path.exists():
                logger.error(f"URLs file not found: {args.urls_file}")
                sys.exit(1)
            urls = [line.strip() for line in urls_path.read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
            logger.info(f"Batch run '{source_name}': {len(urls)} URLs")
            stats = runner.run_batch(source_name, urls, use_async=args.async_mode)
        elif args.url:
            stats = runner.run_batch(source_name, [args.url])
        else:
            logger.error("Provide --url, --urls-file, or --directory")
            sys.exit(1)

        _print_run_summary(stats)


def cmd_search(args, config):
    """Search stored documents."""
    from .storage.db_store import DocumentStore

    setup_logging(level="WARNING")
    storage_cfg = config.get("storage", {})

    with DocumentStore(storage_cfg) as store:
        results = store.search(args.query, limit=args.limit)
        if not results:
            print("No results found.")
            return
        print(f"\n--- {len(results)} Results for '{args.query}' ---\n")
        for r in results:
            print(f"  [{r['id']}] {r.get('title', 'Untitled')}")
            print(f"       Source: {r.get('source_domain', '')} | Type: {r.get('doc_type', '')}")
            if r.get("snippet"):
                print(f"       ...{r['snippet']}...")
            print()


def cmd_status(args, config):
    """Show checkpoints, run history, and document store stats."""
    from .pipeline.runner import BatchRunner

    setup_logging(level="WARNING")

    with BatchRunner(config) as runner:
        # Document stats
        store_stats = runner.get_store_stats()
        print(f"\n--- MakroGraph Document Store ---")
        print(f"  Total documents: {store_stats['total_documents']}")
        print(f"  Database size:   {store_stats['db_size_mb']} MB")
        if store_stats["by_type"]:
            print(f"  By type:")
            for doc_type, count in store_stats["by_type"].items():
                print(f"    {doc_type}: {count}")

        # Checkpoints
        checkpoints = runner.get_all_checkpoints()
        if checkpoints:
            print(f"\n--- Source Checkpoints ---")
            for cp in checkpoints:
                print(f"  {cp['source_name']}:")
                print(f"    Last fetched:  {cp['last_fetched_at']}")
                print(f"    Last run docs: {cp['last_doc_count']} ({cp['last_new_count']} new)")
                print(f"    Total fetched: {cp['total_docs_fetched']} across {cp['total_runs']} runs")
        else:
            print(f"\n  No checkpoints yet (no runs completed)")

        # Recent runs
        history = runner.get_run_history(limit=5)
        if history:
            print(f"\n--- Recent Batch Runs ---")
            for run in history:
                status = run.get("status", "?")
                source = run.get("source_name", "?")
                started = run.get("started_at", "?")
                fetched = run.get("docs_fetched", 0)
                new = run.get("docs_new", 0)
                print(f"  [{status.upper()}] {source} @ {started} — {fetched} fetched, {new} new")


def cmd_reset(args, config):
    """Reset checkpoint for a source — next run will fetch everything."""
    from .pipeline.runner import BatchRunner

    setup_logging(level="WARNING")

    with BatchRunner(config) as runner:
        runner.reset_checkpoint(args.source)
        print(f"Checkpoint reset for '{args.source}'. Next run will fetch all documents.")


def _print_run_summary(stats: dict):
    """Print a clean summary of a batch run."""
    print(f"\n--- Batch Run Complete ---")
    print(f"  Duration:    {stats.get('duration', 0)}s")
    print(f"  Fetched:     {stats.get('fetched', 0)}")
    print(f"  Parsed:      {stats.get('parsed', 0)}")
    print(f"  New:         {stats.get('new', 0)}")
    print(f"  Duplicates:  {stats.get('duplicate', 0)}")
    print(f"  Failed:      {stats.get('failed', 0)}")
    print(f"  Skipped:     {stats.get('skipped', 0)}")


def main():
    parser = argparse.ArgumentParser(
        prog="makrograph",
        description="MakroGraph Intelligence — Incremental Batch Document Pipeline",
    )
    parser.add_argument("--config", default="config/settings.yaml", help="Path to config file")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Run command — incremental batch
    run_parser = subparsers.add_parser("run", help="Run incremental batch (fetch new → process → exit)")
    run_parser.add_argument("--source", help="Source name for checkpoint tracking")
    run_parser.add_argument("--url", help="Single URL to fetch")
    run_parser.add_argument("--urls-file", help="File containing URLs (one per line)")
    run_parser.add_argument("--directory", help="Local directory of documents to process")
    run_parser.add_argument("--pattern", default="*.pdf", help="File pattern for directory mode")
    run_parser.add_argument("--async-mode", action="store_true", help="Use async fetcher for batch URLs")
    run_parser.add_argument("--log-level", dest="log_level", default=None, help="Override log level")

    # Search command
    search_parser = subparsers.add_parser("search", help="Search stored documents")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=20, help="Max results")

    # Status command — checkpoints + stats
    subparsers.add_parser("status", help="Show checkpoints, run history, and stats")

    # Reset command — clear checkpoint for a source
    reset_parser = subparsers.add_parser("reset", help="Reset checkpoint for a source")
    reset_parser.add_argument("source", help="Source name to reset")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config(args.config)

    commands = {
        "run": cmd_run,
        "search": cmd_search,
        "status": cmd_status,
        "reset": cmd_reset,
    }
    commands[args.command](args, config)


if __name__ == "__main__":
    main()
