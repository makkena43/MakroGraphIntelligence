"""Incremental batch pipeline runner.

Architecture: NO daemons, NO polling, NO always-on services.

Flow:
    Start Run
    ↓
    Read last fetch timestamps (checkpoints)
    ↓
    Fetch only new documents since last run
    ↓
    Download documents
    ↓
    Parse and clean
    ↓
    Extract metadata
    ↓
    Deduplicate
    ↓
    Store searchable content
    ↓
    Update checkpoints
    ↓
    Exit
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..fetcher.web_fetcher import WebFetcher
from ..fetcher.async_fetcher import AsyncFetcher
from ..parser.pdf_parser import PDFParser
from ..dedup.deduplicator import Deduplicator, DupStatus
from ..normalizer.text_normalizer import TextNormalizer
from ..storage.db_store import DocumentStore
from .checkpoint import CheckpointManager

logger = logging.getLogger(__name__)


class BatchRunner:
    """Incremental batch pipeline — run once, process new docs, exit.

    Core philosophy: Simple, Reliable, Incremental, Historical-first,
    Low-maintenance, Laptop-friendly.
    """

    def __init__(self, config: dict):
        self.config = config

        # Initialize components
        fetcher_cfg = config.get("fetcher", {})
        parser_cfg = config.get("parser", {})
        dedup_cfg = config.get("dedup", {})
        normalizer_cfg = config.get("normalizer", {})
        storage_cfg = config.get("storage", {})
        pipeline_cfg = config.get("pipeline", {})

        self.store = DocumentStore(storage_cfg)
        self.checkpoints = CheckpointManager(self.store.conn)
        self.fetcher = WebFetcher(fetcher_cfg)
        self.async_fetcher = AsyncFetcher(fetcher_cfg)
        self.parser = PDFParser(parser_cfg)
        self.dedup = Deduplicator(dedup_cfg, storage=self.store)
        self.normalizer = TextNormalizer(normalizer_cfg)

        self.checkpoint_interval = pipeline_cfg.get("checkpoint_interval", 50)

        # Pre-load dedup cache from DB
        self.dedup.load_from_db()

    def _new_stats(self) -> dict:
        """Fresh stats dict for each run."""
        return {
            "fetched": 0,
            "parsed": 0,
            "new": 0,
            "duplicate": 0,
            "failed": 0,
            "skipped": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_batch(
        self,
        source_name: str,
        urls: list[str],
        use_async: bool = False,
    ) -> dict:
        """Run an incremental batch for a named source.

        Steps:
            1. Record run start, read last checkpoint
            2. Fetch documents (caller filters to new-since-checkpoint)
            3. Parse → Dedup → Normalize → Store
            4. Update checkpoint on success
            5. Exit
        """
        stats = self._new_stats()
        run_start = datetime.now(timezone.utc)
        last_checkpoint = self.checkpoints.get_checkpoint(source_name)
        run_id = self.checkpoints.start_run(source_name)

        if last_checkpoint:
            logger.info(
                f"Incremental run for '{source_name}': "
                f"fetching docs newer than {last_checkpoint.isoformat()}"
            )
        else:
            logger.info(f"First run for '{source_name}': fetching all documents")

        total = len(urls)
        if total == 0:
            logger.info(f"No URLs to process for '{source_name}'")
            self.checkpoints.finish_run(run_id, source_name, stats, run_start)
            return stats

        logger.info(f"Processing {total} URLs for '{source_name}'")

        try:
            # Fetch
            if use_async and total > 5:
                fetch_results = self.async_fetcher.fetch_batch(urls)
            else:
                fetch_results = self.fetcher.fetch_batch(urls)

            # Process each result through the pipeline
            for i, fetch_result in enumerate(fetch_results, 1):
                self.store.log_fetch(
                    url=fetch_result.url,
                    status_code=fetch_result.status_code,
                    content_length=fetch_result.content_length,
                    error=fetch_result.error or "",
                )

                if not fetch_result.success:
                    stats["failed"] += 1
                    continue

                stats["fetched"] += 1
                self._process_single(fetch_result, stats, i, total)

                if i % self.checkpoint_interval == 0:
                    logger.info(f"  Progress [{i}/{total}]: {stats}")

            # Success — update checkpoint
            self.checkpoints.finish_run(run_id, source_name, stats, run_start)

        except Exception as e:
            logger.error(f"Batch run failed for '{source_name}': {e}")
            self.checkpoints.finish_run(
                run_id, source_name, stats, run_start, error=str(e)
            )

        duration = (datetime.now(timezone.utc) - run_start).total_seconds()
        stats["duration"] = round(duration, 2)
        self.store.log_pipeline_run(stats)

        logger.info(
            f"Batch '{source_name}' complete in {duration:.1f}s: "
            f"{stats['new']} new, {stats['duplicate']} dup, "
            f"{stats['failed']} failed, {stats['skipped']} skipped"
        )
        return stats

    def run_directory(
        self,
        source_name: str,
        directory: Path,
        pattern: str = "*.pdf",
    ) -> dict:
        """Process all matching files in a local directory as an incremental batch."""
        stats = self._new_stats()
        run_start = datetime.now(timezone.utc)
        run_id = self.checkpoints.start_run(source_name)

        directory = Path(directory)
        last_checkpoint = self.checkpoints.get_checkpoint(source_name)

        # Incremental: only process files modified after last checkpoint
        all_files = sorted(directory.glob(pattern))
        if last_checkpoint:
            files = [
                f for f in all_files
                if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) > last_checkpoint
            ]
            logger.info(
                f"Incremental: {len(files)}/{len(all_files)} files "
                f"modified since {last_checkpoint.isoformat()}"
            )
        else:
            files = all_files
            logger.info(f"First run: processing all {len(files)} files from {directory}")

        total = len(files)

        try:
            for i, file_path in enumerate(files, 1):
                # Parse
                text, page_count = self._parse_file(file_path)
                if text is None:
                    stats["failed"] += 1
                    continue

                stats["parsed"] += 1

                if not text.strip():
                    stats["skipped"] += 1
                    continue

                # Dedup
                file_url = f"file://{file_path.resolve()}"
                dedup_result = self.dedup.check(file_url, text)
                if dedup_result.status != DupStatus.NEW:
                    stats["duplicate"] += 1
                    continue

                # Normalize
                normalized = self.normalizer.normalize(text)
                if normalized is None:
                    stats["skipped"] += 1
                    continue

                # Store
                title = self._extract_title(normalized, file_path.name)
                doc_id = self.store.insert_document(
                    url=file_url,
                    url_hash=dedup_result.url_hash,
                    content_hash=dedup_result.content_hash,
                    raw_text=text,
                    normalized_text=normalized,
                    title=title,
                    source_domain="local",
                    doc_type=file_path.suffix.lstrip("."),
                    page_count=page_count,
                    file_size=file_path.stat().st_size,
                    local_path=str(file_path),
                    metadata="{}",
                )

                if doc_id:
                    stats["new"] += 1
                    logger.info(f"  [{i}/{total}] Stored: {title}")
                else:
                    stats["duplicate"] += 1

                if i % self.checkpoint_interval == 0:
                    logger.info(f"  Progress [{i}/{total}]: {stats}")

            self.checkpoints.finish_run(run_id, source_name, stats, run_start)

        except Exception as e:
            logger.error(f"Directory batch failed for '{source_name}': {e}")
            self.checkpoints.finish_run(
                run_id, source_name, stats, run_start, error=str(e)
            )

        duration = (datetime.now(timezone.utc) - run_start).total_seconds()
        stats["duration"] = round(duration, 2)
        self.store.log_pipeline_run(stats)

        logger.info(f"Directory batch '{source_name}' complete in {duration:.1f}s: {stats}")
        return stats

    def process_urls(self, urls: list[str], use_async: bool = False) -> dict:
        """Quick batch run without named source (ad-hoc processing)."""
        return self.run_batch("_adhoc", urls, use_async=use_async)

    # ------------------------------------------------------------------
    # Checkpoint queries
    # ------------------------------------------------------------------

    def get_checkpoint(self, source_name: str) -> Optional[datetime]:
        """Get last successful fetch timestamp for a source."""
        return self.checkpoints.get_checkpoint(source_name)

    def get_all_checkpoints(self) -> list[dict]:
        """Get all source checkpoints."""
        return self.checkpoints.get_all_checkpoints()

    def get_run_history(self, source_name: str = "", limit: int = 10) -> list[dict]:
        """Get recent batch run history."""
        return self.checkpoints.get_run_history(source_name, limit)

    def reset_checkpoint(self, source_name: str):
        """Reset a source — next run will fetch everything."""
        self.checkpoints.reset_checkpoint(source_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _process_single(self, fetch_result, stats: dict, idx: int, total: int):
        """Process a single fetched document through parse → dedup → normalize → store."""
        text, page_count = "", 0

        if fetch_result.doc_type.value == "pdf" and fetch_result.local_path:
            parse_result = self.parser.parse(fetch_result.local_path)
            if parse_result.success:
                text = parse_result.text
                page_count = parse_result.page_count
                stats["parsed"] += 1
            else:
                stats["failed"] += 1
                return
        elif fetch_result.local_path:
            try:
                text = fetch_result.local_path.read_text(encoding="utf-8", errors="replace")
                stats["parsed"] += 1
            except Exception as e:
                logger.error(f"Failed to read {fetch_result.local_path}: {e}")
                stats["failed"] += 1
                return

        if not text.strip():
            stats["skipped"] += 1
            return

        # Dedup
        dedup_result = self.dedup.check(fetch_result.url, text)
        if dedup_result.status != DupStatus.NEW:
            stats["duplicate"] += 1
            return

        # Normalize
        normalized = self.normalizer.normalize(text)
        if normalized is None:
            stats["skipped"] += 1
            return

        # Store
        title = self._extract_title(normalized, fetch_result.url)
        doc_id = self.store.insert_document(
            url=fetch_result.url,
            url_hash=dedup_result.url_hash,
            content_hash=dedup_result.content_hash,
            raw_text=text,
            normalized_text=normalized,
            title=title,
            source_domain=fetch_result.metadata.get("source_domain", ""),
            doc_type=fetch_result.doc_type.value,
            page_count=page_count,
            file_size=fetch_result.content_length,
            local_path=str(fetch_result.local_path) if fetch_result.local_path else "",
            fetched_at=fetch_result.fetched_at,
            metadata=json.dumps(fetch_result.metadata),
        )

        if doc_id:
            stats["new"] += 1
            logger.info(f"  [{idx}/{total}] Stored #{doc_id}: {title}")
        else:
            stats["duplicate"] += 1

    def _parse_file(self, file_path: Path) -> tuple[Optional[str], int]:
        """Parse a local file and return (text, page_count)."""
        if file_path.suffix.lower() == ".pdf":
            parse_result = self.parser.parse(file_path)
            if parse_result.success:
                return parse_result.text, parse_result.page_count
            return None, 0
        else:
            try:
                return file_path.read_text(encoding="utf-8", errors="replace"), 0
            except Exception:
                return None, 0

    def _extract_title(self, text: str, fallback: str) -> str:
        """Extract a title from the first non-empty line of text."""
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped and len(stripped) > 5:
                return stripped[:200]
        return fallback

    def get_store_stats(self) -> dict:
        """Get current document store statistics."""
        return self.store.get_stats()

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search stored documents."""
        return self.store.search(query, limit)

    def close(self):
        """Clean up resources."""
        self.fetcher.close()
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
