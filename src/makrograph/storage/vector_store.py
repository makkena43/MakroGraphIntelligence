"""Vector store using pgvector for semantic similarity search."""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class VectorStore:
    """Stores and queries semantic embeddings via PostgreSQL pgvector extension.

    Supports document-level, entity-level, and theme-level embeddings.
    Uses cosine similarity for nearest-neighbor search.
    """

    def __init__(self, config: dict):
        import psycopg2
        from psycopg2 import pool as pg_pool
        from psycopg2.extras import RealDictCursor

        self._pool = pg_pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=config.get("pool_size", 3),
            host=config.get("host", "localhost"),
            port=config.get("port", 5432),
            dbname=config.get("dbname", "makrograph"),
            user=config.get("user", "postgres"),
            password=config.get("password", ""),
        )
        self._cursor_factory = RealDictCursor
        self.embedding_dim = config.get("embedding_dim", 384)
        self.model_name = config.get("embedding_model", "all-MiniLM-L6-v2")
        logger.info(f"VectorStore ready (dim={self.embedding_dim}, model={self.model_name})")

    def _vec_literal(self, embedding: list[float]) -> str:
        """Convert a float list to pgvector literal string."""
        return "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    def store_embedding(
        self,
        embedding: list[float],
        embedding_type: str,
        text_chunk: str = "",
        document_id: int = None,
        entity_id: int = None,
        theme_id: int = None,
        chunk_index: int = 0,
    ) -> int:
        """Store a single embedding vector. Returns row id."""
        sql = """
            INSERT INTO mg_embeddings
                (document_id, entity_id, theme_id, embedding_type,
                 model_name, embedding, text_chunk, chunk_index)
            VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
            RETURNING id
        """
        vec_str = self._vec_literal(embedding)
        import psycopg2
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (
                    document_id, entity_id, theme_id, embedding_type,
                    self.model_name, vec_str, text_chunk, chunk_index,
                ))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def store_batch(self, records: list[dict]) -> int:
        """Bulk-store embeddings. Each record must have 'embedding' + type fields."""
        if not records:
            return 0
        sql = """
            INSERT INTO mg_embeddings
                (document_id, entity_id, theme_id, embedding_type,
                 model_name, embedding, text_chunk, chunk_index)
            VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
            ON CONFLICT DO NOTHING
        """
        rows = []
        for r in records:
            vec_str = self._vec_literal(r["embedding"])
            rows.append((
                r.get("document_id"), r.get("entity_id"), r.get("theme_id"),
                r.get("embedding_type", "document"),
                r.get("model_name", self.model_name),
                vec_str,
                r.get("text_chunk", ""),
                r.get("chunk_index", 0),
            ))

        import psycopg2.extras as extras
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                extras.execute_batch(cur, sql, rows, page_size=50)
                conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def search_similar(
        self,
        query_embedding: list[float],
        embedding_type: str = "document",
        top_k: int = 10,
        threshold: float = 0.7,
    ) -> list[dict]:
        """Find top-K most similar embeddings using cosine similarity.

        Returns records sorted by cosine similarity descending.
        """
        sql = """
            SELECT
                e.id,
                e.document_id,
                e.entity_id,
                e.theme_id,
                e.text_chunk,
                e.chunk_index,
                1 - (e.embedding <=> %s::vector) AS similarity
            FROM mg_embeddings e
            WHERE e.embedding_type = %s
              AND 1 - (e.embedding <=> %s::vector) >= %s
            ORDER BY e.embedding <=> %s::vector
            LIMIT %s
        """
        vec_str = self._vec_literal(query_embedding)
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (vec_str, embedding_type, vec_str, threshold, vec_str, top_k))
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._pool.putconn(conn)

    def search_similar_documents(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        threshold: float = 0.65,
    ) -> list[dict]:
        """Find top-K similar documents with their metadata."""
        sql = """
            SELECT
                d.id AS document_id,
                d.title,
                d.company,
                d.ticker,
                d.doc_type,
                d.filed_at,
                e.text_chunk,
                1 - (e.embedding <=> %s::vector) AS similarity
            FROM mg_embeddings e
            JOIN mg_documents d ON d.id = e.document_id
            WHERE e.embedding_type = 'document'
              AND 1 - (e.embedding <=> %s::vector) >= %s
            ORDER BY e.embedding <=> %s::vector
            LIMIT %s
        """
        vec_str = self._vec_literal(query_embedding)
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (vec_str, vec_str, threshold, vec_str, top_k))
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._pool.putconn(conn)

    def get_theme_embedding_centroid(self, theme_id: int) -> Optional[list[float]]:
        """Compute the centroid embedding for all documents associated with a theme."""
        sql = """
            SELECT embedding
            FROM mg_embeddings
            WHERE theme_id = %s AND embedding_type = 'theme'
            LIMIT 200
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (theme_id,))
                rows = cur.fetchall()
                if not rows:
                    return None
                vecs = [list(r[0]) for r in rows]
                centroid = np.mean(vecs, axis=0).tolist()
                return centroid
        finally:
            self._pool.putconn(conn)

    def close(self):
        self._pool.closeall()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
