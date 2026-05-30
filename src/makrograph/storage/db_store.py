"""SQLite storage with FTS5 full-text search for document content."""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DocumentStore:
    """Local SQLite-based document storage with full-text search."""

    def __init__(self, config: dict):
        self.db_path = Path(config.get("db_path", "data/db/makrograph.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.enable_fts = config.get("enable_fts", True)
        self.batch_size = config.get("batch_size", 100)
        self.conn = None
        self._connect()
        self._create_tables()

    def _connect(self):
        """Establish database connection."""
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        logger.info(f"Connected to database: {self.db_path}")

    def _create_tables(self):
        """Create document tables and FTS index."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                url_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                title TEXT,
                source_domain TEXT,
                doc_type TEXT,
                raw_text TEXT,
                normalized_text TEXT,
                page_count INTEGER DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                local_path TEXT,
                fetched_at TIMESTAMP,
                parsed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT,
                country TEXT DEFAULT 'US',
                UNIQUE(content_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_documents_url_hash ON documents(url_hash);
            CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash);
            CREATE INDEX IF NOT EXISTS idx_documents_source_domain ON documents(source_domain);
            CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at);
            CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);

            CREATE TABLE IF NOT EXISTS fetch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                status_code INTEGER,
                content_length INTEGER,
                error TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pipeline_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date DATE NOT NULL,
                docs_fetched INTEGER DEFAULT 0,
                docs_parsed INTEGER DEFAULT 0,
                docs_new INTEGER DEFAULT 0,
                docs_duplicate INTEGER DEFAULT 0,
                docs_failed INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        if self.enable_fts:
            try:
                self.conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
                    USING fts5(
                        title,
                        normalized_text,
                        source_domain,
                        content='documents',
                        content_rowid='id'
                    )
                """)
                # Triggers to keep FTS in sync
                self.conn.executescript("""
                    CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                        INSERT INTO documents_fts(rowid, title, normalized_text, source_domain)
                        VALUES (new.id, new.title, new.normalized_text, new.source_domain);
                    END;

                    CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                        INSERT INTO documents_fts(documents_fts, rowid, title, normalized_text, source_domain)
                        VALUES ('delete', old.id, old.title, old.normalized_text, old.source_domain);
                    END;

                    CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                        INSERT INTO documents_fts(documents_fts, rowid, title, normalized_text, source_domain)
                        VALUES ('delete', old.id, old.title, old.normalized_text, old.source_domain);
                        INSERT INTO documents_fts(rowid, title, normalized_text, source_domain)
                        VALUES (new.id, new.title, new.normalized_text, new.source_domain);
                    END;
                """)
                logger.info("FTS5 full-text search enabled")
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS5 not available: {e}. Falling back to LIKE search.")
                self.enable_fts = False

        self.conn.commit()

    def insert_document(
        self,
        url: str,
        url_hash: str,
        content_hash: str,
        raw_text: str,
        normalized_text: str,
        title: str = "",
        source_domain: str = "",
        doc_type: str = "",
        page_count: int = 0,
        file_size: int = 0,
        local_path: str = "",
        fetched_at: Optional[datetime] = None,
        metadata: str = "",
    ) -> Optional[int]:
        """Insert a new document. Returns doc_id or None if duplicate."""
        try:
            cursor = self.conn.execute(
                """INSERT INTO documents
                   (url, url_hash, content_hash, title, source_domain, doc_type,
                    raw_text, normalized_text, page_count, file_size, local_path,
                    fetched_at, parsed_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    url, url_hash, content_hash, title, source_domain, doc_type,
                    raw_text, normalized_text, page_count, file_size, local_path,
                    fetched_at or datetime.utcnow(),
                    datetime.utcnow(),
                    metadata,
                ),
            )
            self.conn.commit()
            doc_id = cursor.lastrowid
            logger.debug(f"Inserted document {doc_id}: {title or url}")
            return doc_id
        except sqlite3.IntegrityError:
            logger.debug(f"Duplicate document skipped: {url}")
            return None
        except Exception as e:
            logger.error(f"Failed to insert document: {e}")
            self.conn.rollback()
            return None

    def log_fetch(self, url: str, status_code: int, content_length: int = 0, error: str = ""):
        """Log a fetch attempt."""
        try:
            self.conn.execute(
                "INSERT INTO fetch_log (url, status_code, content_length, error) VALUES (?, ?, ?, ?)",
                (url, status_code, content_length, error),
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to log fetch: {e}")

    def find_by_hash(self, content_hash: str = "", url_hash: str = "") -> Optional[dict]:
        """Find existing document by content or URL hash."""
        if content_hash:
            row = self.conn.execute(
                "SELECT id FROM documents WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if row:
                return {"doc_id": row["id"], "match": "content"}
        if url_hash:
            row = self.conn.execute(
                "SELECT id FROM documents WHERE url_hash = ?", (url_hash,)
            ).fetchone()
            if row:
                return {"doc_id": row["id"], "match": "url"}
        return None

    def get_all_hashes(self) -> dict:
        """Get all stored hashes for dedup cache loading."""
        url_hashes = [
            row[0] for row in self.conn.execute("SELECT url_hash FROM documents").fetchall()
        ]
        content_hashes = [
            row[0] for row in self.conn.execute("SELECT content_hash FROM documents").fetchall()
        ]
        return {"url_hashes": url_hashes, "content_hashes": content_hashes}

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across stored documents."""
        results = []
        try:
            if self.enable_fts:
                rows = self.conn.execute(
                    """SELECT d.id, d.title, d.url, d.source_domain, d.doc_type,
                              d.page_count, d.created_at,
                              snippet(documents_fts, 1, '<b>', '</b>', '...', 40) as snippet
                       FROM documents_fts f
                       JOIN documents d ON d.id = f.rowid
                       WHERE documents_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (query, limit),
                )
            else:
                rows = self.conn.execute(
                    """SELECT id, title, url, source_domain, doc_type,
                              page_count, created_at, '' as snippet
                       FROM documents
                       WHERE normalized_text LIKE ?
                       ORDER BY created_at DESC
                       LIMIT ?""",
                    (f"%{query}%", limit),
                )

            for row in rows:
                results.append(dict(row))

        except Exception as e:
            logger.error(f"Search failed: {e}")

        return results

    def get_stats(self) -> dict:
        """Get document store statistics."""
        row = self.conn.execute("SELECT COUNT(*) as total FROM documents").fetchone()
        total = row["total"]
        type_counts = {}
        for row in self.conn.execute(
            "SELECT doc_type, COUNT(*) as cnt FROM documents GROUP BY doc_type"
        ):
            type_counts[row["doc_type"]] = row["cnt"]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "total_documents": total,
            "by_type": type_counts,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
        }

    def log_pipeline_run(self, stats: dict):
        """Log pipeline run statistics."""
        try:
            self.conn.execute(
                """INSERT INTO pipeline_stats
                   (run_date, docs_fetched, docs_parsed, docs_new, docs_duplicate,
                    docs_failed, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.utcnow().date().isoformat(),
                    stats.get("fetched", 0),
                    stats.get("parsed", 0),
                    stats.get("new", 0),
                    stats.get("duplicate", 0),
                    stats.get("failed", 0),
                    stats.get("duration", 0),
                ),
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to log pipeline stats: {e}")

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
