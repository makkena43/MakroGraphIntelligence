"""Checkpoint manager for incremental batch ingestion.

Tracks the last successful fetch timestamp per source so each run
only processes documents that are new since the previous run.

Flow:
    1. Read last checkpoint for source
    2. Fetch documents newer than checkpoint
    3. Process documents
    4. Update checkpoint on success
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Manages fetch checkpoints for incremental batch processing."""

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn
        self._create_tables()

    def _create_tables(self):
        """Create checkpoint and run history tables."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS source_checkpoints (
                source_name TEXT PRIMARY KEY,
                last_fetched_at TIMESTAMP NOT NULL,
                last_doc_count INTEGER DEFAULT 0,
                last_new_count INTEGER DEFAULT 0,
                total_docs_fetched INTEGER DEFAULT 0,
                total_runs INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS batch_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                status TEXT DEFAULT 'running',
                docs_fetched INTEGER DEFAULT 0,
                docs_new INTEGER DEFAULT 0,
                docs_duplicate INTEGER DEFAULT 0,
                docs_failed INTEGER DEFAULT 0,
                checkpoint_before TIMESTAMP,
                checkpoint_after TIMESTAMP,
                error TEXT,
                metadata TEXT,
                FOREIGN KEY (source_name) REFERENCES source_checkpoints(source_name)
            );

            CREATE INDEX IF NOT EXISTS idx_batch_runs_source
                ON batch_runs(source_name, started_at DESC);
        """)
        self.conn.commit()

    def get_checkpoint(self, source_name: str) -> Optional[datetime]:
        """Get the last successful fetch timestamp for a source.

        Returns None if source has never been fetched (first run).
        """
        row = self.conn.execute(
            "SELECT last_fetched_at FROM source_checkpoints WHERE source_name = ?",
            (source_name,),
        ).fetchone()
        if row:
            ts = row[0] if isinstance(row, tuple) else row["last_fetched_at"]
            if isinstance(ts, str):
                return datetime.fromisoformat(ts)
            return ts
        return None

    def update_checkpoint(
        self,
        source_name: str,
        fetched_at: datetime,
        doc_count: int = 0,
        new_count: int = 0,
    ):
        """Update checkpoint after a successful batch run."""
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_checkpoint(source_name)

        if existing:
            self.conn.execute(
                """UPDATE source_checkpoints
                   SET last_fetched_at = ?,
                       last_doc_count = ?,
                       last_new_count = ?,
                       total_docs_fetched = total_docs_fetched + ?,
                       total_runs = total_runs + 1,
                       updated_at = ?
                   WHERE source_name = ?""",
                (fetched_at.isoformat(), doc_count, new_count, doc_count, now, source_name),
            )
        else:
            self.conn.execute(
                """INSERT INTO source_checkpoints
                   (source_name, last_fetched_at, last_doc_count, last_new_count,
                    total_docs_fetched, total_runs, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (source_name, fetched_at.isoformat(), doc_count, new_count, doc_count, now),
            )
        self.conn.commit()
        logger.info(
            f"Checkpoint updated: {source_name} -> {fetched_at.isoformat()} "
            f"({doc_count} fetched, {new_count} new)"
        )

    def start_run(self, source_name: str) -> int:
        """Record the start of a batch run. Returns run_id."""
        checkpoint = self.get_checkpoint(source_name)
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT INTO batch_runs
               (source_name, started_at, status, checkpoint_before)
               VALUES (?, ?, 'running', ?)""",
            (source_name, now, checkpoint.isoformat() if checkpoint else None),
        )
        self.conn.commit()
        run_id = cursor.lastrowid
        logger.info(f"Batch run #{run_id} started for '{source_name}' "
                     f"(checkpoint: {checkpoint or 'FIRST RUN'})")
        return run_id

    def finish_run(
        self,
        run_id: int,
        source_name: str,
        stats: dict,
        new_checkpoint: datetime,
        error: str = "",
    ):
        """Record completion of a batch run and update checkpoint."""
        now = datetime.now(timezone.utc).isoformat()
        status = "failed" if error else "completed"

        self.conn.execute(
            """UPDATE batch_runs
               SET finished_at = ?, status = ?,
                   docs_fetched = ?, docs_new = ?,
                   docs_duplicate = ?, docs_failed = ?,
                   checkpoint_after = ?, error = ?,
                   metadata = ?
               WHERE id = ?""",
            (
                now, status,
                stats.get("fetched", 0), stats.get("new", 0),
                stats.get("duplicate", 0), stats.get("failed", 0),
                new_checkpoint.isoformat(), error,
                json.dumps(stats),
                run_id,
            ),
        )

        if not error:
            self.update_checkpoint(
                source_name, new_checkpoint,
                doc_count=stats.get("fetched", 0),
                new_count=stats.get("new", 0),
            )
        else:
            self.conn.commit()
            logger.error(f"Batch run #{run_id} failed: {error}")

    def get_all_checkpoints(self) -> list[dict]:
        """Get checkpoints for all sources."""
        rows = self.conn.execute(
            """SELECT source_name, last_fetched_at, last_doc_count, last_new_count,
                      total_docs_fetched, total_runs, updated_at
               FROM source_checkpoints
               ORDER BY updated_at DESC"""
        ).fetchall()
        return [dict(row) if hasattr(row, "keys") else {
            "source_name": row[0],
            "last_fetched_at": row[1],
            "last_doc_count": row[2],
            "last_new_count": row[3],
            "total_docs_fetched": row[4],
            "total_runs": row[5],
            "updated_at": row[6],
        } for row in rows]

    def get_run_history(self, source_name: str = "", limit: int = 10) -> list[dict]:
        """Get recent batch run history."""
        if source_name:
            rows = self.conn.execute(
                """SELECT * FROM batch_runs
                   WHERE source_name = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (source_name, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM batch_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) if hasattr(row, "keys") else row for row in rows]

    def reset_checkpoint(self, source_name: str):
        """Reset a source checkpoint (next run will fetch everything)."""
        self.conn.execute(
            "DELETE FROM source_checkpoints WHERE source_name = ?",
            (source_name,),
        )
        self.conn.commit()
        logger.info(f"Checkpoint reset for '{source_name}' — next run will fetch all")
