"""PostgreSQL metadata store for documents, entities, signals, and themes."""

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PGStore:
    """PostgreSQL-backed metadata store.

    Handles all structured metadata: documents, entities, signals, themes,
    beneficiaries, checkpoints, and pipeline run logs.
    """

    def __init__(self, config: dict):
        import psycopg2
        from psycopg2 import pool as pg_pool
        from psycopg2.extras import RealDictCursor

        self._pool = pg_pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=config.get("pool_size", 5),
            host=config.get("host", "localhost"),
            port=config.get("port", 5432),
            dbname=config.get("dbname", "makrograph"),
            user=config.get("user", "postgres"),
            password=config.get("password", ""),
        )
        self._cursor_factory = RealDictCursor
        logger.info(f"PGStore connected to {config.get('host')}:{config.get('port')}/{config.get('dbname')}")
        # Always run migrations on first connect so the dashboard works even if
        # the pipeline has never been executed (adds country cols, widens filing_type, etc.)
        try:
            self.ensure_country_columns()
        except Exception as _e:
            logger.warning(f"ensure_country_columns on init skipped: {_e}")

    @contextmanager
    def _conn(self):
        """Context manager for connection + cursor from pool."""
        import psycopg2
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def apply_schema(self, schema_path: str = "schema/postgres_schema.sql"):
        """Apply the PostgreSQL schema from file."""
        sql = Path(schema_path).read_text()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        logger.info("Schema applied successfully")

    def ensure_canonical_review_table(self) -> None:
        """Create the mg_theme_canonical_reviews table if it doesn't exist.

        This table stores theme clusters that need a human-reviewed canonical
        name.  Each row is one cluster of similar themes that the canonicalizer
        detected as merge candidates.

        Columns:
            cluster_id        – stable hash of sorted member slugs (unique)
            member_slugs      – all theme slugs in this cluster
            member_names      – human-readable names for display
            member_descriptions – one description per member (truncated)
            suggested_name    – heuristic auto-generated name
            llm_prompt_text   – the exact prompt text that would be sent to an LLM
                                 (human reads this to decide on canonical name)
            approved_name     – the canonical name the human chose
            status            – 'pending' | 'approved' | 'dismissed'
        """
        ddl = """
            CREATE TABLE IF NOT EXISTS mg_theme_canonical_reviews (
                id              SERIAL PRIMARY KEY,
                cluster_id      TEXT NOT NULL UNIQUE,
                member_slugs    TEXT[]    NOT NULL,
                member_names    TEXT[]    NOT NULL,
                member_descriptions TEXT[] DEFAULT '{}',
                suggested_name  TEXT,
                llm_prompt_text TEXT,
                approved_name   TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_canonical_reviews_status
                ON mg_theme_canonical_reviews (status);
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        logger.info("mg_theme_canonical_reviews table ensured")

    def upsert_canonical_review(self, review: dict) -> None:
        """Insert or update a canonical review record.

        Args:
            review: dict with keys:
                cluster_id, member_slugs, member_names, member_descriptions,
                suggested_name, llm_prompt_text
        """
        sql = """
            INSERT INTO mg_theme_canonical_reviews
                (cluster_id, member_slugs, member_names, member_descriptions,
                 suggested_name, llm_prompt_text, status)
            VALUES
                (%(cluster_id)s, %(member_slugs)s, %(member_names)s,
                 %(member_descriptions)s, %(suggested_name)s, %(llm_prompt_text)s,
                 'pending')
            ON CONFLICT (cluster_id) DO UPDATE SET
                member_slugs      = EXCLUDED.member_slugs,
                member_names      = EXCLUDED.member_names,
                member_descriptions = EXCLUDED.member_descriptions,
                suggested_name    = EXCLUDED.suggested_name,
                llm_prompt_text   = EXCLUDED.llm_prompt_text,
                -- Preserve approved_name and status if already reviewed
                status            = CASE
                    WHEN mg_theme_canonical_reviews.status = 'approved' THEN 'approved'
                    ELSE 'pending'
                END,
                updated_at        = NOW()
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    "cluster_id":          review["cluster_id"],
                    "member_slugs":        review.get("member_slugs", []),
                    "member_names":        review.get("member_names", []),
                    "member_descriptions": review.get("member_descriptions", []),
                    "suggested_name":      review.get("suggested_name", ""),
                    "llm_prompt_text":     review.get("llm_prompt_text", ""),
                })

    def get_pending_canonical_reviews(self) -> list[dict]:
        """Return all clusters awaiting human review, ordered newest first."""
        from psycopg2.extras import RealDictCursor
        with self._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM mg_theme_canonical_reviews
                    WHERE status = 'pending'
                    ORDER BY created_at DESC
                """)
                return [dict(r) for r in cur.fetchall()]

    def get_approved_canonical_names(self) -> dict[str, str]:
        """Return {cluster_id → approved_name} for all approved reviews.

        Used by ThemeCanonicalizer to skip LLM/heuristic and use the
        human-approved name directly.
        """
        from psycopg2.extras import RealDictCursor
        result: dict[str, str] = {}
        try:
            with self._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT cluster_id, approved_name, member_slugs
                        FROM mg_theme_canonical_reviews
                        WHERE status = 'approved'
                          AND approved_name IS NOT NULL
                          AND approved_name != ''
                    """)
                    for row in cur.fetchall():
                        result[row["cluster_id"]] = row["approved_name"]
        except Exception as e:
            logger.debug(f"get_approved_canonical_names failed (ok on first run): {e}")
        return result

    def approve_canonical_review(self, cluster_id: str, approved_name: str) -> None:
        """Mark a review as approved with the human-chosen canonical name."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE mg_theme_canonical_reviews
                    SET approved_name = %s,
                        status        = 'approved',
                        updated_at    = NOW()
                    WHERE cluster_id = %s
                """, (approved_name.strip(), cluster_id))

    def dismiss_canonical_review(self, cluster_id: str) -> None:
        """Dismiss a review — these themes will stay separate (no merge)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE mg_theme_canonical_reviews
                    SET status     = 'dismissed',
                        updated_at = NOW()
                    WHERE cluster_id = %s
                """, (cluster_id,))

    def bulk_approve_canonical_reviews(self, approvals: dict[str, str]) -> int:
        """Bulk approve multiple clusters from a parsed LLM response.

        Args:
            approvals: {cluster_id → canonical_name}

        Returns:
            Number of clusters successfully approved.
        """
        count = 0
        for cluster_id, canonical_name in approvals.items():
            if not canonical_name or not canonical_name.strip():
                continue
            try:
                self.approve_canonical_review(cluster_id, canonical_name.strip())
                count += 1
            except Exception as e:
                logger.warning(f"bulk_approve: failed for {cluster_id}: {e}")
        return count

    @staticmethod
    def build_combined_canonical_prompt(pending_reviews: list[dict]) -> str:
        """Build a single combined prompt for all pending clusters.

        The user copies this, pastes it into any LLM chat (Claude, GPT, etc.),
        and pastes the numbered response back into the UI for bulk approval.

        Format of expected LLM response::

            1. AI Infrastructure Power Constraint
            2. HBM Supply Constraint from AI Demand
            3. EV Battery Materials Shortage
        """
        if not pending_reviews:
            return ""

        lines = [
            "You are a senior investment analyst reviewing auto-detected investment themes",
            "from SEC filings and earnings calls.",
            "",
            f"Below are {len(pending_reviews)} theme cluster(s). Each cluster contains similar",
            "themes that describe the SAME macro investment opportunity.",
            "For EACH cluster (numbered 1 to N), provide ONE canonical name.",
            "",
            "RULES:",
            "  • Max 7 words per name",
            "  • Investor-grade, specific, actionable",
            "  • Focus on: structural driver → impacted asset class",
            "  • Good: 'AI Infrastructure Power Constraint', 'HBM Supply Constraint', 'EV Battery Shortage'",
            "  • Bad:  'Technology', 'Supply Chain', 'AI Growth'",
            "",
            "Reply with ONLY numbered answers, one per line:",
            "  1. [canonical name for cluster 1]",
            "  2. [canonical name for cluster 2]",
            "  ...",
            "",
            "═" * 60,
        ]

        for i, rev in enumerate(pending_reviews, 1):
            members  = rev.get("member_names") or []
            descs    = rev.get("member_descriptions") or []
            suggest  = rev.get("suggested_name", "")
            slugs    = rev.get("member_slugs") or []

            # Collect signal info from descriptions
            desc_text = " | ".join(
                d[:120] for d in descs if d
            )

            lines += [
                "",
                f"CLUSTER {i}:",
                f"  Themes:  {' | '.join(members)}",
            ]
            if desc_text:
                lines.append(f"  Context: {desc_text[:300]}")
            if suggest:
                lines.append(f"  Auto-suggested name: {suggest}  (improve or keep as-is)")
            lines.append("─" * 60)

        lines += [
            "",
            "Now write the canonical names (numbered list only, no explanation):",
        ]

        return "\n".join(lines)

    @staticmethod
    def build_single_canonical_prompt(review: dict) -> str:
        """Build a prompt for a single cluster.
        
        Used when the combined prompt is too large and we need to process
        clusters individually to avoid token limits.
        """
        members  = review.get("member_names") or []
        descs    = review.get("member_descriptions") or []
        suggest  = review.get("suggested_name", "")
        slugs    = review.get("member_slugs") or []

        # Collect signal info from descriptions
        desc_text = " | ".join(
            d[:120] for d in descs if d
        )

        lines = [
            "You are a senior investment analyst reviewing auto-detected investment themes",
            "from SEC filings and earnings calls.",
            "",
            "Below is ONE theme cluster containing similar themes that describe the SAME",
            "macro investment opportunity. Provide ONE canonical name for this cluster.",
            "",
            "RULES:",
            "  • Max 7 words per name",
            "  • Investor-grade, specific, actionable",
            "  • Focus on: structural driver → impacted asset class",
            "  • Good: 'AI Infrastructure Power Constraint', 'HBM Supply Constraint', 'EV Battery Shortage'",
            "  • Bad:  'Technology', 'Supply Chain', 'AI Growth'",
            "",
            "═" * 60,
            "",
            "CLUSTER 1:",
            f"  Themes:  {' | '.join(members)}",
        ]
        
        if desc_text:
            lines.append(f"  Context: {desc_text[:300]}")
        if suggest:
            lines.append(f"  Auto-suggested name: {suggest}  (improve or keep as-is)")
        
        lines += [
            "─" * 60,
            "",
            "Now write the canonical name (no numbering, no explanation):",
        ]

        return "\n".join(lines)

    def ensure_canonicalization_columns(self) -> None:
        """Add canonicalization columns to mg_themes if they don't exist yet.

        Idempotent — safe to call on every pipeline startup.  Uses
        ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` so it is a no-op after
        the first successful run.

        New columns:
            is_canonical        BOOLEAN   — TRUE for top-level parent themes
            canonical_name      TEXT      — LLM-generated clean display name
            aliases             TEXT[]    — alternative names for the same macro-theme
            parent_theme_slug   TEXT      — FK to the canonical parent (NULL if root)
        """
        ddl_statements = [
            "ALTER TABLE mg_themes ADD COLUMN IF NOT EXISTS is_canonical BOOLEAN DEFAULT TRUE",
            "ALTER TABLE mg_themes ADD COLUMN IF NOT EXISTS canonical_name TEXT",
            "ALTER TABLE mg_themes ADD COLUMN IF NOT EXISTS aliases TEXT[] DEFAULT '{}'",
            "ALTER TABLE mg_themes ADD COLUMN IF NOT EXISTS parent_theme_slug TEXT",
            # Index for fast UI lookups: "give me all canonical themes"
            "CREATE INDEX IF NOT EXISTS idx_mg_themes_canonical ON mg_themes (is_canonical) WHERE is_canonical = TRUE",
            # Index for fast subtheme lookup by parent
            "CREATE INDEX IF NOT EXISTS idx_mg_themes_parent ON mg_themes (parent_theme_slug) WHERE parent_theme_slug IS NOT NULL",
        ]
        with self._conn() as conn:
            with conn.cursor() as cur:
                for stmt in ddl_statements:
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        logger.debug(f"Schema migration (ok if already applied): {e}")
        logger.info("Canonicalization columns ensured on mg_themes")

    def ensure_country_columns(self) -> None:
        """Add country column to mg_documents and mg_themes if not present.

        Idempotent — safe to call on every pipeline startup.  Enables
        multi-market support (US, IN, GB, ...) by tagging every document
        and derived theme with an ISO-2 country code.
        """
        ddl_statements = [
            # Core tables (already in CREATE TABLE schema)
            "ALTER TABLE mg_documents          ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            "ALTER TABLE mg_themes             ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            # Theme-derived tables
            "ALTER TABLE mg_theme_snapshots    ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            "ALTER TABLE mg_theme_performance  ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            "ALTER TABLE mg_theme_propagation  ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            # Causal / event / graph tables
            "ALTER TABLE mg_causal_chains      ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            "ALTER TABLE mg_events             ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            "ALTER TABLE mg_ontology_nodes     ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            # Macro / policy tables
            "ALTER TABLE mg_macro_events       ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            "ALTER TABLE mg_policy_events      ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            # Contradictions table (created dynamically — safe to attempt)
            "ALTER TABLE mg_contradictions     ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            # Widen filing_type — NSE categories can exceed 30 chars (e.g. "Disclosure under SEBI Takeover Regulations" = 42)
            "ALTER TABLE mg_documents ALTER COLUMN filing_type TYPE VARCHAR(120)",
            # Add country to mg_signals so rankings can be scoped without always joining mg_documents
            "ALTER TABLE mg_signals ADD COLUMN IF NOT EXISTS country VARCHAR(10) DEFAULT 'US'",
            "CREATE INDEX IF NOT EXISTS idx_mg_signals_country ON mg_signals(country)",
            # Indexes
            "CREATE INDEX IF NOT EXISTS idx_mg_docs_country       ON mg_documents         (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_theme_country      ON mg_themes             (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_snap_country       ON mg_theme_snapshots    (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_perf_country       ON mg_theme_performance  (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_prop_country       ON mg_theme_propagation  (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_cc_country         ON mg_causal_chains      (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_ev_country         ON mg_events             (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_node_country       ON mg_ontology_nodes     (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_mev_country        ON mg_macro_events       (country)",
            "CREATE INDEX IF NOT EXISTS idx_mg_policy_country     ON mg_policy_events      (country)",
        ]
        with self._conn() as conn:
            with conn.cursor() as cur:
                for stmt in ddl_statements:
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        logger.debug(f"ensure_country_columns (ok if already applied): {e}")
        logger.info("Country columns ensured on mg_documents + mg_themes")

    def get_companies_per_theme(self, theme_slugs: list[str]) -> dict[str, set[str]]:
        """Return {theme_slug → set of company names} for the given slugs.

        Used by ThemeCanonicalizer to compute shared-company similarity between
        theme pairs.  Queries mg_theme_beneficiaries which stores the per-theme
        company mapping built by BeneficiaryMapper.
        """
        from psycopg2.extras import RealDictCursor
        if not theme_slugs:
            return {}
        sql = """
            SELECT t.theme_slug, b.company_name
            FROM mg_theme_beneficiaries b
            JOIN mg_themes t ON t.id = b.theme_id
            WHERE t.theme_slug = ANY(%s)
              AND b.company_name IS NOT NULL
              AND b.company_name != ''
        """
        result: dict[str, set[str]] = {s: set() for s in theme_slugs}
        try:
            with self._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, (theme_slugs,))
                    for row in cur.fetchall():
                        result[row["theme_slug"]].add(row["company_name"])
        except Exception as e:
            logger.debug(f"get_companies_per_theme failed (ok on first run): {e}")
        return result

    def get_canonical_themes(self, min_strength: float = 10.0) -> list[dict]:
        """Return only canonical parent themes — what the UI top-level shows.

        Subthemes (is_canonical=FALSE) are hidden from this view but can be
        fetched via get_subthemes_of().
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(
                    """
                    SELECT * FROM mg_themes
                    WHERE (is_canonical IS NULL OR is_canonical = TRUE)
                      AND is_active = TRUE
                      AND strength_score >= %s
                    ORDER BY momentum_score DESC, strength_score DESC
                    """,
                    (min_strength,),
                )
                return [dict(r) for r in cur.fetchall()]

    def get_subthemes_of(self, parent_slug: str) -> list[dict]:
        """Return all subthemes belonging to a canonical parent theme."""
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(
                    """
                    SELECT * FROM mg_themes
                    WHERE parent_theme_slug = %s
                    ORDER BY strength_score DESC
                    """,
                    (parent_slug,),
                )
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # DOCUMENTS
    # ----------------------------------------------------------
    def upsert_document(self, doc: dict) -> int:
        """Insert or update a document. Returns document id.

        Uses url as the primary conflict key (most stable identifier for
        exchange announcements).  content_hash conflicts are handled via a
        second ON CONFLICT clause using CTE so both unique constraints are safe.
        Returns the document id whether inserted or updated (never raises on dup).
        """
        # Single-statement upsert on URL (most natural dedup key for NSE/BSE).
        # If the URL already exists we update mutable fields and return the id.
        # A separate DO NOTHING handles content_hash collisions from different URLs.
        sql = """
            INSERT INTO mg_documents
                (source_name, doc_type, url, url_hash, content_hash, title,
                 company, ticker, cik, filing_type, fiscal_period, filed_at,
                 published_at, local_path, page_count, word_count, processing_status,
                 country)
            VALUES
                (%(source_name)s, %(doc_type)s, %(url)s, %(url_hash)s, %(content_hash)s,
                 %(title)s, %(company)s, %(ticker)s, %(cik)s, %(filing_type)s,
                 %(fiscal_period)s, %(filed_at)s, %(published_at)s, %(local_path)s,
                 %(page_count)s, %(word_count)s, %(processing_status)s,
                 %(country)s)
            ON CONFLICT (url)
            DO UPDATE SET
                title             = COALESCE(NULLIF(EXCLUDED.title, ''), mg_documents.title),
                processing_status = EXCLUDED.processing_status,
                word_count        = GREATEST(mg_documents.word_count, EXCLUDED.word_count),
                country           = EXCLUDED.country,
                updated_at        = NOW()
            RETURNING id
        """
        def _trunc(val: str | None, limit: int) -> str:
            """Truncate a string to fit a VARCHAR(limit) column safely."""
            s = (val or "")
            return s[:limit] if len(s) > limit else s

        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "source_name": _trunc(doc.get("source_name", ""), 50),
                    "doc_type": _trunc(doc.get("doc_type", ""), 50),
                    "url": doc.get("url", ""),
                    "url_hash": doc.get("url_hash", ""),
                    "content_hash": doc.get("content_hash", ""),
                    "title": doc.get("title", ""),
                    "company": doc.get("company", ""),
                    "ticker": _trunc(doc.get("ticker", ""), 20),
                    "cik": _trunc(doc.get("cik", ""), 20),
                    "filing_type": _trunc(doc.get("filing_type", ""), 120),
                    "fiscal_period": _trunc(doc.get("fiscal_period", ""), 20),
                    "filed_at": doc.get("filed_at"),
                    "published_at": doc.get("published_at"),
                    "local_path": doc.get("local_path", ""),
                    "page_count": doc.get("page_count", 0),
                    "word_count": doc.get("word_count", 0),
                    "processing_status": _trunc(doc.get("processing_status", "fetched"), 30),
                    "country": _trunc(doc.get("country", "US"), 10),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def update_document_status(self, doc_id: int, status: str):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mg_documents SET processing_status=%s, updated_at=NOW() WHERE id=%s",
                    (status, doc_id)
                )

    def batch_update_document_status(self, doc_ids: list[int], status: str):
        """Update processing_status for multiple documents in one statement."""
        if not doc_ids:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mg_documents SET processing_status=%s, updated_at=NOW() WHERE id = ANY(%s)",
                    (status, doc_ids)
                )

    def get_documents_by_status(
        self, status: str, limit: int = 100, country: str = None
    ) -> list[dict]:
        country_clause = "AND country = %s" if country else ""
        params = [status] + ([country] if country else []) + [limit]
        sql = f"SELECT * FROM mg_documents WHERE processing_status=%s {country_clause} ORDER BY filed_at DESC LIMIT %s"
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_entities_for_document(self, document_id: int) -> list[dict]:
        """Return all entities linked to a document (joined with entity details)."""
        sql = """
            SELECT e.id, e.entity_text, e.entity_type, e.canonical_name, e.ticker,
                   e.mention_count, e.confidence, e.metadata,
                   de.sentiment_score, de.mention_count AS doc_mention_count
            FROM mg_entities e
            JOIN mg_document_entities de ON de.entity_id = e.id
            WHERE de.document_id = %s
            ORDER BY de.mention_count DESC
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (document_id,))
                return [dict(r) for r in cur.fetchall()]

    def get_entities_for_documents(self, document_ids: list[int]) -> dict[int, list[dict]]:
        """Batch version of get_entities_for_document — one query for N docs.

        Returns:
            dict mapping document_id → list of entity dicts.
        """
        if not document_ids:
            return {}
        sql = """
            SELECT e.id, e.entity_text, e.entity_type, e.canonical_name, e.ticker,
                   e.mention_count, e.confidence, e.metadata,
                   de.sentiment_score, de.mention_count AS doc_mention_count,
                   de.document_id
            FROM mg_entities e
            JOIN mg_document_entities de ON de.entity_id = e.id
            WHERE de.document_id = ANY(%s)
            ORDER BY de.document_id, de.mention_count DESC
        """
        result: dict[int, list[dict]] = {did: [] for did in document_ids}
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (document_ids,))
                for row in cur.fetchall():
                    r = dict(row)
                    did = r.pop("document_id")
                    result.setdefault(did, []).append(r)
        return result

    # ----------------------------------------------------------
    # ENTITIES
    # ----------------------------------------------------------
    def upsert_entity(self, entity: dict) -> int:
        """Upsert entity and return its id."""
        sql = """
            INSERT INTO mg_entities
                (entity_text, entity_type, canonical_name, ticker,
                 mention_count, first_seen_at, last_seen_at, confidence, metadata)
            VALUES
                (%(entity_text)s, %(entity_type)s, %(canonical_name)s, %(ticker)s,
                 %(mention_count)s, %(first_seen_at)s, %(last_seen_at)s, %(confidence)s,
                 %(metadata)s::jsonb)
            ON CONFLICT (canonical_name, entity_type)
            DO UPDATE SET
                mention_count = mg_entities.mention_count + EXCLUDED.mention_count,
                last_seen_at  = GREATEST(mg_entities.last_seen_at, EXCLUDED.last_seen_at),
                confidence    = GREATEST(mg_entities.confidence, EXCLUDED.confidence),
                ticker        = COALESCE(mg_entities.ticker, EXCLUDED.ticker)
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "entity_text": entity.get("entity_text", ""),
                    "entity_type": entity.get("entity_type", "CONCEPT"),
                    "canonical_name": entity.get("canonical_name") or entity.get("entity_text", ""),
                    "ticker": entity.get("ticker"),
                    "mention_count": entity.get("mention_count", 1),
                    "first_seen_at": entity.get("first_seen_at"),
                    "last_seen_at": entity.get("last_seen_at"),
                    "confidence": entity.get("confidence", 1.0),
                    "metadata": json.dumps(entity.get("metadata", {})),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def link_document_entity(self, doc_id: int, entity_id: int, mention_count: int = 1,
                              sentiment: float = 0.0, snippets: list = None):
        sql = """
            INSERT INTO mg_document_entities (document_id, entity_id, mention_count, sentiment_score, context_snippets)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (document_id, entity_id)
            DO UPDATE SET
                mention_count = mg_document_entities.mention_count + EXCLUDED.mention_count,
                sentiment_score = EXCLUDED.sentiment_score
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (doc_id, entity_id, mention_count, sentiment, snippets or []))

    def batch_upsert_entities_and_links(
        self, doc_id: int, entities: list[dict], doc_filed_at=None
    ) -> dict[str, int]:
        """Upsert all entities for one document + their doc-entity links in a SINGLE transaction.

        Replaces N×upsert_entity + N×link_document_entity calls (one per entity)
        with two batched statements.  For a doc with 40 entities this goes from
        80 round-trips to 1 transaction (80× speedup for the NLP stage).

        Returns:
            dict mapping canonical_name → entity_id
        """
        from psycopg2.extras import execute_values
        if not entities:
            return {}

        entity_sql = """
            INSERT INTO mg_entities
                (entity_text, entity_type, canonical_name, ticker,
                 mention_count, first_seen_at, last_seen_at, confidence, metadata)
            VALUES %s
            ON CONFLICT (canonical_name, entity_type)
            DO UPDATE SET
                mention_count = mg_entities.mention_count + EXCLUDED.mention_count,
                last_seen_at  = GREATEST(mg_entities.last_seen_at, EXCLUDED.last_seen_at),
                confidence    = GREATEST(mg_entities.confidence, EXCLUDED.confidence),
                ticker        = COALESCE(mg_entities.ticker, EXCLUDED.ticker)
            RETURNING id, canonical_name
        """
        link_sql = """
            INSERT INTO mg_document_entities
                (document_id, entity_id, mention_count, sentiment_score, context_snippets)
            VALUES %s
            ON CONFLICT (document_id, entity_id)
            DO UPDATE SET
                mention_count   = mg_document_entities.mention_count + EXCLUDED.mention_count,
                sentiment_score = EXCLUDED.sentiment_score
        """
        entity_rows = [
            (
                ent.get("entity_text", ""),
                ent.get("entity_type", "CONCEPT"),
                ent.get("canonical_name") or ent.get("entity_text", ""),
                ent.get("ticker"),
                ent.get("mention_count", 1),
                doc_filed_at or ent.get("first_seen_at"),
                doc_filed_at or ent.get("last_seen_at"),
                ent.get("confidence", 1.0),
                json.dumps(ent.get("metadata", {})),
            )
            for ent in entities
        ]

        name_to_id: dict[str, int] = {}
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                # Pass 1: upsert all entities, collect returned IDs
                results = execute_values(cur, entity_sql, entity_rows, fetch=True)
                for row in results:
                    name_to_id[row["canonical_name"]] = row["id"]

                # Pass 2: create doc-entity links for all returned IDs
                link_rows = [
                    (
                        doc_id,
                        eid,
                        1,    # mention_count
                        0.0,  # sentiment_score
                        [],   # context_snippets
                    )
                    for eid in name_to_id.values()
                ]
                if link_rows:
                    execute_values(cur, link_sql, link_rows)

        return name_to_id

    def batch_insert_signals(self, signals: list[dict]) -> int:
        """Insert all signals for one document in a single statement.

        Replaces N×insert_signal round-trips with one batched INSERT.

        Deduplicates within the batch before sending to Postgres.  PostgreSQL's
        ON CONFLICT DO UPDATE cannot handle two rows in the same VALUES list that
        map to the same constraint key — it raises "cannot affect row a second
        time".  We keep the highest-confidence signal when duplicates exist.

        Returns:
            number of signals inserted / updated.
        """
        from psycopg2.extras import execute_values
        if not signals:
            return 0

        # ── Deduplicate: same (document_id, entity_id, signal_type, direction) ──
        # This mirrors the unique index: (document_id, COALESCE(entity_id,-1),
        # signal_type, COALESCE(direction,''))
        deduped: dict[tuple, dict] = {}
        for s in signals:
            key = (
                s.get("document_id"),
                s.get("entity_id"),          # None treated as same bucket
                s.get("signal_type", ""),
                s.get("direction") or "",
            )
            existing = deduped.get(key)
            if existing is None or (s.get("confidence", 0.0) or 0.0) > (existing.get("confidence", 0.0) or 0.0):
                deduped[key] = s

        sql = """
            INSERT INTO mg_signals
                (document_id, entity_id, signal_type, signal_value, signal_unit,
                 direction, confidence, context_text, extracted_by, filed_at, country)
            VALUES %s
            ON CONFLICT (document_id, COALESCE(entity_id, -1), signal_type, COALESCE(direction, ''))
            WHERE document_id IS NOT NULL
            DO UPDATE SET
                confidence   = GREATEST(mg_signals.confidence, EXCLUDED.confidence),
                signal_value = COALESCE(EXCLUDED.signal_value, mg_signals.signal_value),
                context_text = COALESCE(EXCLUDED.context_text, mg_signals.context_text),
                country      = COALESCE(EXCLUDED.country, mg_signals.country)
        """
        rows = [
            (
                s.get("document_id"),
                s.get("entity_id"),
                s.get("signal_type", ""),
                s.get("signal_value"),
                s.get("signal_unit"),
                s.get("direction"),
                s.get("confidence", 1.0),
                (s.get("context_text") or "")[:500],
                s.get("extracted_by", ""),
                s.get("filed_at"),
                s.get("country", "US"),
            )
            for s in deduped.values()
        ]
        with self._conn() as conn:
            with conn.cursor() as cur:
                execute_values(cur, sql, rows)
        return len(rows)

    # ----------------------------------------------------------
    # SIGNALS
    # ----------------------------------------------------------
    def insert_signal(self, signal: dict) -> int:
        """Insert a signal. ON CONFLICT DO NOTHING prevents duplicates when
        the same document is re-processed (e.g. re-running the same month).
        The unique index on (document_id, entity_id, signal_type, direction)
        guarantees exactly one signal per (doc, entity, type, direction) tuple.
        """
        sql = """
            INSERT INTO mg_signals
                (document_id, entity_id, signal_type, signal_value, signal_unit,
                 direction, confidence, context_text, extracted_by, filed_at, country)
            VALUES
                (%(document_id)s, %(entity_id)s, %(signal_type)s, %(signal_value)s,
                 %(signal_unit)s, %(direction)s, %(confidence)s, %(context_text)s,
                 %(extracted_by)s, %(filed_at)s, %(country)s)
            ON CONFLICT (document_id, COALESCE(entity_id, -1), signal_type, COALESCE(direction, ''))
            WHERE document_id IS NOT NULL
            DO UPDATE SET
                confidence   = GREATEST(mg_signals.confidence, EXCLUDED.confidence),
                signal_value = COALESCE(EXCLUDED.signal_value, mg_signals.signal_value),
                context_text = COALESCE(EXCLUDED.context_text, mg_signals.context_text),
                country      = COALESCE(EXCLUDED.country, mg_signals.country)
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "document_id": signal.get("document_id"),
                    "entity_id": signal.get("entity_id"),
                    "signal_type": signal.get("signal_type", ""),
                    "signal_value": signal.get("signal_value"),
                    "signal_unit": signal.get("signal_unit"),
                    "direction": signal.get("direction", "neutral"),
                    "confidence": signal.get("confidence", 0.7),
                    "context_text": signal.get("context_text", ""),
                    "extracted_by": signal.get("extracted_by", "rule"),
                    "filed_at": signal.get("filed_at"),
                    "country": signal.get("country", "US"),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def get_signals_by_type(self, signal_type: str, days: int = 90, as_of_date=None) -> list[dict]:
        """Return signals of the given type within a rolling window.

        Args:
            signal_type: e.g. 'demand_surge', 'capex_increase'
            days:        look-back window in days
            as_of_date:  upper bound date (defaults to MAX(filed_at) in DB so
                         historical data is never excluded by today's date).
        """
        from datetime import date as _date, timedelta as _td
        if as_of_date is None:
            # Use the latest document date in the DB instead of NOW() so that
            # historical pipelines (where filed_at << today) still return data.
            try:
                with self._conn() as _conn:
                    with _conn.cursor() as _cur:
                        _cur.execute(
                            "SELECT MAX(filed_at) FROM mg_documents WHERE filed_at IS NOT NULL"
                        )
                        _row = _cur.fetchone()
                        as_of_date = _row[0] if (_row and _row[0]) else _date.today()
            except Exception:
                as_of_date = _date.today()
        if hasattr(as_of_date, "date"):
            as_of_date = as_of_date.date()
        floor_date = as_of_date - _td(days=days)

        sql = """
            SELECT s.*, e.canonical_name, e.ticker, d.company
            FROM mg_signals s
            LEFT JOIN mg_entities e ON e.id = s.entity_id
            LEFT JOIN mg_documents d ON d.id = s.document_id
            WHERE s.signal_type = %s
              AND s.filed_at >= %s
              AND s.filed_at <= %s
            ORDER BY s.filed_at DESC
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (signal_type, floor_date, as_of_date))
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # THEMES
    # ----------------------------------------------------------
    def upsert_theme(self, theme: dict) -> int:
        # Auto-compute stage from theme data (imported lazily to avoid circular imports)
        _stage_n     = theme.get("stage", 0)
        _stage_label = theme.get("stage_label", "")
        _stage_ev    = theme.get("stage_evidence", "")
        if not _stage_label:
            try:
                from ..themes.theme_stage import stage_from_theme_dict
                _ts = stage_from_theme_dict(theme)
                _stage_n, _stage_label, _stage_ev = _ts.stage, _ts.label, _ts.evidence
            except Exception:
                pass

        sql = """
            INSERT INTO mg_themes
                (theme_name, theme_slug, description, sectors, signal_types,
                 strength_score, momentum_score, conviction, first_detected,
                 last_updated, doc_count, company_count, hypothesis_text, metadata,
                 stage, stage_label, stage_evidence,
                 is_canonical, canonical_name, aliases, parent_theme_slug, country)
            VALUES
                (%(theme_name)s, %(theme_slug)s, %(description)s, %(sectors)s,
                 %(signal_types)s, %(strength_score)s, %(momentum_score)s, %(conviction)s,
                 %(first_detected)s, %(last_updated)s, %(doc_count)s, %(company_count)s,
                 %(hypothesis_text)s, %(metadata)s::jsonb,
                 %(stage)s, %(stage_label)s, %(stage_evidence)s,
                 %(is_canonical)s, %(canonical_name)s, %(aliases)s, %(parent_theme_slug)s,
                 %(country)s)
            ON CONFLICT (theme_slug, country)
            DO UPDATE SET
                strength_score     = EXCLUDED.strength_score,
                momentum_score     = EXCLUDED.momentum_score,
                conviction         = EXCLUDED.conviction,
                doc_count          = EXCLUDED.doc_count,
                company_count      = EXCLUDED.company_count,
                hypothesis_text    = COALESCE(EXCLUDED.hypothesis_text, mg_themes.hypothesis_text),
                last_updated       = EXCLUDED.last_updated,
                -- Promote from NULL once evidence crosses threshold; never overwrite
                -- an existing real date with NULL or a later date.
                first_detected     = COALESCE(mg_themes.first_detected, EXCLUDED.first_detected),
                stage              = EXCLUDED.stage,
                stage_label        = EXCLUDED.stage_label,
                stage_evidence     = EXCLUDED.stage_evidence,
                is_canonical       = EXCLUDED.is_canonical,
                canonical_name     = COALESCE(EXCLUDED.canonical_name, mg_themes.canonical_name),
                aliases            = EXCLUDED.aliases,
                parent_theme_slug  = EXCLUDED.parent_theme_slug,
                -- Never re-activate a theme that was manually deactivated (is_active=false).
                -- A manual deactivation is an explicit override; the pipeline should not
                -- silently undo it on the next run.
                is_active          = mg_themes.is_active,
                updated_at         = NOW()
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "theme_name":        theme.get("theme_name", ""),
                    "theme_slug":        theme.get("theme_slug", ""),
                    "description":       theme.get("description", ""),
                    "sectors":           theme.get("sectors", []),
                    "signal_types":      theme.get("signal_types", []),
                    "strength_score":    theme.get("strength_score", 0.0),
                    "momentum_score":    theme.get("momentum_score", 0.0),
                    "conviction":        theme.get("conviction", "emerging"),
                    "first_detected":    theme.get("first_detected"),
                    "last_updated":      theme.get("last_updated", date.today()),
                    "doc_count":         theme.get("doc_count", 0),
                    "company_count":     theme.get("company_count", 0),
                    "hypothesis_text":   theme.get("hypothesis_text"),
                    "metadata":          json.dumps(theme.get("metadata", {})),
                    "stage":             _stage_n,
                    "is_canonical":      theme.get("is_canonical", True),
                    "canonical_name":    theme.get("canonical_name") or theme.get("theme_name", ""),
                    "aliases":           theme.get("aliases", []),
                    "parent_theme_slug": theme.get("parent_theme_slug"),
                    "stage_label":    _stage_label,
                    "stage_evidence": _stage_ev,
                    "country":           theme.get("country", "US"),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def sync_theme_company_counts(self, theme_ids: list[int]) -> None:
        """Update mg_themes.company_count to match actual beneficiary rows.

        Called after BeneficiaryMapper.persist() so that:
          - country-specific pipelines (India) show India beneficiary counts, not
            global supply-chain estimates (which can be 50-300 for US data)
          - the breadth penalty in RankingEngine uses the correct company count
          - the UI's Co.Count column reflects real mapped companies, not model estimates
        """
        if not theme_ids:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mg_themes t
                    SET company_count = sub.real_count,
                        updated_at    = NOW()
                    FROM (
                        SELECT theme_id,
                               COUNT(DISTINCT COALESCE(ticker, company_name)) AS real_count
                        FROM mg_theme_beneficiaries
                        WHERE theme_id = ANY(%s)
                        GROUP BY theme_id
                    ) sub
                    WHERE t.id = sub.theme_id
                    """,
                    (theme_ids,),
                )
                # Themes with zero beneficiaries: reset to 0
                cur.execute(
                    """
                    UPDATE mg_themes
                    SET company_count = 0,
                        updated_at    = NOW()
                    WHERE id = ANY(%s)
                      AND id NOT IN (
                          SELECT DISTINCT theme_id FROM mg_theme_beneficiaries
                          WHERE theme_id = ANY(%s)
                      )
                    """,
                    (theme_ids, theme_ids),
                )

    def get_active_themes(self, min_strength: float = 20.0, country: str = None) -> list[dict]:
        """Return active themes ordered by momentum. Optionally filter by country."""
        params = [min_strength]
        country_clause = ""
        if country:
            country_clause = "AND country = %s"
            params.append(country)
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(
                    f"""SELECT * FROM mg_themes
                       WHERE is_active = TRUE AND strength_score >= %s
                       {country_clause}
                       ORDER BY momentum_score DESC, strength_score DESC""",
                    params,
                )
                return [dict(r) for r in cur.fetchall()]

    def get_themes_as_of(
        self,
        as_of_date,
        from_date=None,
        min_strength: float = 0.0,
        country: str = None,
    ) -> list[dict]:
        """Return themes with scores as they appeared on as_of_date.

        For each theme, picks the latest snapshot on or before as_of_date.
        Themes with no snapshot before as_of_date are excluded (they weren't
        detected yet at that point in time).

        If from_date is also given, only returns themes that had at least one
        snapshot in [from_date, as_of_date].

        Returns rows with extra fields:
            snap_strength, snap_momentum, snap_doc_count, snap_date
        """
        sql = """
            WITH latest_snap AS (
                SELECT DISTINCT ON (theme_id)
                    theme_id,
                    snapshot_date       AS snap_date,
                    strength_score      AS snap_strength,
                    momentum_score      AS snap_momentum,
                    doc_count           AS snap_doc_count,
                    company_count       AS snap_company_count
                FROM mg_theme_snapshots
                WHERE snapshot_date <= %s
                  {from_clause}
                ORDER BY theme_id, snapshot_date DESC
            )
            SELECT t.*,
                   ls.snap_date, ls.snap_strength, ls.snap_momentum,
                   ls.snap_doc_count, ls.snap_company_count
            FROM mg_themes t
            JOIN latest_snap ls ON ls.theme_id = t.id
            WHERE ls.snap_strength >= %s
              AND t.is_active = TRUE
              {country_clause}
            ORDER BY ls.snap_strength DESC
        """
        from_clause = "AND snapshot_date >= %s" if from_date else ""
        country_clause = "AND t.country = %s" if country else ""
        params = [as_of_date]
        if from_date:
            params.append(from_date)
        params.append(min_strength)
        if country:
            params.append(country)

        final_sql = sql.format(from_clause=from_clause, country_clause=country_clause)
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(final_sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_beneficiaries_as_of(self, theme_id: int, as_of_date) -> list[dict]:
        """Return theme beneficiaries that were first seen on or before as_of_date."""
        sql = """
            SELECT b.ticker, b.company_name, b.beneficiary_type, b.company_role,
                   b.relevance_score, b.signal_count, b.capex_signals,
                   b.rank_in_theme, b.reasoning, b.first_seen_at, b.last_seen_at
            FROM mg_theme_beneficiaries b
            WHERE b.theme_id = %s
              AND (b.first_seen_at IS NULL OR b.first_seen_at <= %s)
            ORDER BY b.rank_in_theme NULLS LAST, b.relevance_score DESC
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (theme_id, as_of_date))
                return [dict(r) for r in cur.fetchall()]

    def get_snapshots_in_window(
        self, theme_id: int, from_date, to_date
    ) -> list[dict]:
        """Return snapshots for a theme between from_date and to_date."""
        sql = """
            SELECT snapshot_date, strength_score, momentum_score, doc_count
            FROM mg_theme_snapshots
            WHERE theme_id = %s
              AND snapshot_date >= %s
              AND snapshot_date <= %s
            ORDER BY snapshot_date
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (theme_id, from_date, to_date))
                return [dict(r) for r in cur.fetchall()]

    def snapshot_theme(self, theme_id: int, data: dict):
        sql = """
            INSERT INTO mg_theme_snapshots
                (theme_id, snapshot_date, strength_score, momentum_score,
                 doc_count, company_count, top_entities)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (theme_id, snapshot_date) DO UPDATE SET
                strength_score = EXCLUDED.strength_score,
                momentum_score = EXCLUDED.momentum_score
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    theme_id,
                    data.get("snapshot_date", date.today()),
                    data.get("strength_score", 0.0),
                    data.get("momentum_score", 0.0),
                    data.get("doc_count", 0),
                    data.get("company_count", 0),
                    json.dumps(data.get("top_entities", [])),
                ))

    def batch_upsert_themes_and_snapshots(
        self, ranked_themes: list[dict], snapshot_data: list[dict]
    ) -> dict:
        """Persist all themes + snapshots in a SINGLE transaction.

        Much faster than per-theme upsert_theme + snapshot_theme (eliminates
        2×N pool checkout/commit/return cycles during historical replay).

        Args:
            ranked_themes: list of theme dicts (from InvestmentTheme.to_dict())
            snapshot_data: list of dicts with {theme_slug, snapshot_date, strength_score, ...}

        Returns:
            dict mapping theme_slug → theme_id
        """
        theme_id_map = {}
        # Pre-compute stage for all themes
        try:
            from ..themes.theme_stage import (
                stage_from_theme_dict,
                compute_stage_progression,
                compute_explosive_potential,
            )
            _stage_fn = stage_from_theme_dict
            _progression_fn = compute_stage_progression
            _explosive_fn = compute_explosive_potential
        except Exception:
            _stage_fn = None
            _progression_fn = None
            _explosive_fn = None

        # ── Pass 0: Fetch PRIOR SNAPSHOTS for all themes in this batch ──────
        # This single query enables stage progression tracking quarter-over-quarter
        # by comparing the current theme to its most recent snapshot.
        prior_snapshots: dict[str, dict] = {}
        try:
            slugs_to_lookup = [t.get("theme_slug") for t in ranked_themes if t.get("theme_slug")]
            if slugs_to_lookup:
                with self._conn() as conn:
                    with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                        cur.execute("""
                            SELECT DISTINCT ON (mt.theme_slug)
                                   mt.theme_slug,
                                   s.snapshot_date,
                                   s.strength_score,
                                   s.doc_count,
                                   s.company_count,
                                   s.top_entities
                            FROM mg_theme_snapshots s
                            JOIN mg_themes mt ON mt.id = s.theme_id
                            WHERE mt.theme_slug = ANY(%s)
                            ORDER BY mt.theme_slug, s.snapshot_date DESC
                        """, (slugs_to_lookup,))
                        for row in cur.fetchall():
                            prior_snapshots[row["theme_slug"]] = dict(row)
        except Exception as e:
            logger.debug(f"Prior snapshot lookup failed (ok on first run): {e}")

        # Enrich every theme's metadata with stage_progression + explosive_potential
        # BEFORE persisting. This is what powers quarter-over-quarter stage upgrades.
        for t in ranked_themes:
            meta = t.get("metadata") or {}
            slug = t.get("theme_slug")

            # Stage progression vs last quarter's snapshot
            if _progression_fn:
                prior = prior_snapshots.get(slug)
                try:
                    prog = _progression_fn(t, prior)
                    meta["stage_trend"]          = prog["stage_trend"]
                    meta["progression_score"]    = prog["progression_score"]
                    meta["progression_evidence"] = prog["progression_evidence"]
                    meta["company_growth_pct"]   = prog["company_growth_pct"]

                    # Append to stage_history (audit trail of stage over time)
                    history = meta.get("stage_history") or []
                    if isinstance(history, str):
                        try:
                            history = json.loads(history)
                        except Exception:
                            history = []
                    if _stage_fn:
                        try:
                            cur_stage = _stage_fn(t).stage
                            from datetime import date as _date
                            history.append({
                                "date":           str(_date.today()),
                                "stage":          cur_stage,
                                "trend":          prog["stage_trend"],
                                "company_count":  t.get("company_count", 0),
                            })
                            # Keep last 8 quarters
                            meta["stage_history"] = history[-8:]
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"Progression calc failed for {slug}: {e}")

            # Explosive return potential
            if _explosive_fn:
                try:
                    # Inject current stage into theme dict so explosive scoring can see it
                    if _stage_fn:
                        try:
                            meta["stage"] = _stage_fn(t).stage
                        except Exception:
                            pass
                    expl = _explosive_fn({**t, "metadata": meta})
                    meta["explosive_score"]    = expl["explosive_score"]
                    meta["explosive_evidence"] = expl["explosive_evidence"]
                except Exception as e:
                    logger.debug(f"Explosive calc failed for {slug}: {e}")

            t["metadata"] = meta

        theme_upsert_sql = """
            INSERT INTO mg_themes
                (theme_name, theme_slug, description, sectors, signal_types,
                 strength_score, momentum_score, conviction, first_detected,
                 last_updated, doc_count, company_count, hypothesis_text, metadata,
                 stage, stage_label, stage_evidence,
                 is_canonical, canonical_name, aliases, parent_theme_slug, country)
            VALUES
                (%(theme_name)s, %(theme_slug)s, %(description)s, %(sectors)s,
                 %(signal_types)s, %(strength_score)s, %(momentum_score)s, %(conviction)s,
                 %(first_detected)s, %(last_updated)s, %(doc_count)s, %(company_count)s,
                 %(hypothesis_text)s, %(metadata)s::jsonb,
                 %(stage)s, %(stage_label)s, %(stage_evidence)s,
                 %(is_canonical)s, %(canonical_name)s, %(aliases)s, %(parent_theme_slug)s,
                 %(country)s)
            ON CONFLICT (theme_slug, country)
            DO UPDATE SET
                strength_score     = EXCLUDED.strength_score,
                momentum_score     = EXCLUDED.momentum_score,
                conviction         = EXCLUDED.conviction,
                doc_count          = EXCLUDED.doc_count,
                company_count      = EXCLUDED.company_count,
                hypothesis_text    = COALESCE(EXCLUDED.hypothesis_text, mg_themes.hypothesis_text),
                metadata           = EXCLUDED.metadata,
                last_updated       = EXCLUDED.last_updated,
                country            = EXCLUDED.country,
                -- Promote from NULL once evidence crosses threshold; never overwrite
                -- an existing real date with NULL or a later date.
                first_detected     = COALESCE(mg_themes.first_detected, EXCLUDED.first_detected),
                stage              = EXCLUDED.stage,
                stage_label        = EXCLUDED.stage_label,
                stage_evidence     = EXCLUDED.stage_evidence,
                is_canonical       = EXCLUDED.is_canonical,
                canonical_name     = COALESCE(EXCLUDED.canonical_name, mg_themes.canonical_name),
                aliases            = EXCLUDED.aliases,
                parent_theme_slug  = EXCLUDED.parent_theme_slug,
                -- Never re-activate a manually deactivated theme.
                is_active          = mg_themes.is_active,
                updated_at         = NOW()
            RETURNING id, theme_slug
        """
        snap_sql = """
            INSERT INTO mg_theme_snapshots
                (theme_id, snapshot_date, strength_score, momentum_score,
                 doc_count, company_count, top_entities, country)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (theme_id, snapshot_date) DO UPDATE SET
                strength_score = EXCLUDED.strength_score,
                momentum_score = EXCLUDED.momentum_score,
                country        = EXCLUDED.country
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                # Pass 1: upsert all themes
                for t in ranked_themes:
                    _sn, _sl, _se = 0, "", ""
                    if _stage_fn:
                        try:
                            _ts = _stage_fn(t)
                            _sn, _sl, _se = _ts.stage, _ts.label, _ts.evidence
                        except Exception:
                            pass
                    cur.execute(theme_upsert_sql, {
                        "theme_name":        t.get("theme_name", ""),
                        "theme_slug":        t.get("theme_slug", ""),
                        "description":       t.get("description", ""),
                        "sectors":           t.get("sectors", []),
                        "signal_types":      t.get("signal_types", []),
                        "strength_score":    t.get("strength_score", 0.0),
                        "momentum_score":    t.get("momentum_score", 0.0),
                        "conviction":        t.get("conviction", "emerging"),
                        "first_detected":    t.get("first_detected"),
                        "last_updated":      t.get("last_updated", date.today()),
                        "doc_count":         t.get("doc_count", 0),
                        "company_count":     t.get("company_count", 0),
                        "hypothesis_text":   t.get("hypothesis_text"),
                        "metadata":          json.dumps(t.get("metadata", {})),
                        "stage":             _sn,
                        "stage_label":       _sl,
                        "stage_evidence":    _se,
                        # Canonicalization — default to canonical if not set by merge engine
                        "is_canonical":      t.get("is_canonical", True),
                        "canonical_name":    t.get("canonical_name") or t.get("theme_name", ""),
                        "aliases":           t.get("aliases", []),
                        "parent_theme_slug": t.get("parent_theme_slug"),
                        "country":           t.get("country", "US"),
                    })
                    row = cur.fetchone()
                    if row:
                        theme_id_map[row["theme_slug"]] = row["id"]

                # Pass 2: upsert all snapshots — carry country from theme
                slug_to_country = {t.get("theme_slug"): t.get("country", "US") for t in ranked_themes}
                for snap in snapshot_data:
                    tid = theme_id_map.get(snap["theme_slug"])
                    if not tid:
                        continue
                    snap_country = snap.get("country") or slug_to_country.get(snap["theme_slug"], "US")
                    cur.execute(snap_sql, (
                        tid,
                        snap.get("snapshot_date", date.today()),
                        snap.get("strength_score", 0.0),
                        snap.get("momentum_score", 0.0),
                        snap.get("doc_count", 0),
                        snap.get("company_count", 0),
                        json.dumps(snap.get("top_entities", [])),
                        snap_country,
                    ))

        return theme_id_map

    # ----------------------------------------------------------
    # THEME BENEFICIARIES
    # ----------------------------------------------------------
    def upsert_beneficiary(self, beneficiary: dict) -> int:
        sql = """
            INSERT INTO mg_theme_beneficiaries
                (theme_id, entity_id, ticker, company_name, beneficiary_type,
                 company_role, relevance_score, signal_count, capex_signals,
                 quarterly_mentions, first_seen_at, last_seen_at, rank_in_theme, reasoning,
                 window_start, window_end)
            VALUES
                (%(theme_id)s, %(entity_id)s, %(ticker)s, %(company_name)s, %(beneficiary_type)s,
                 %(company_role)s, %(relevance_score)s, %(signal_count)s, %(capex_signals)s,
                 %(quarterly_mentions)s::jsonb, %(first_seen_at)s, %(last_seen_at)s,
                 %(rank_in_theme)s, %(reasoning)s, %(window_start)s, %(window_end)s)
            ON CONFLICT (theme_id, entity_id) DO UPDATE SET
                relevance_score    = EXCLUDED.relevance_score,
                signal_count       = EXCLUDED.signal_count,
                capex_signals      = EXCLUDED.capex_signals,
                company_role       = COALESCE(NULLIF(EXCLUDED.company_role, ''), mg_theme_beneficiaries.company_role),
                quarterly_mentions = EXCLUDED.quarterly_mentions,
                last_seen_at       = EXCLUDED.last_seen_at,
                rank_in_theme      = EXCLUDED.rank_in_theme,
                reasoning          = COALESCE(EXCLUDED.reasoning, mg_theme_beneficiaries.reasoning),
                window_start       = EXCLUDED.window_start,
                window_end         = EXCLUDED.window_end,
                updated_at         = NOW()
            RETURNING id
        """
        import json as _json
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "theme_id": beneficiary["theme_id"],
                    "entity_id": beneficiary["entity_id"],
                    "ticker": beneficiary.get("ticker"),
                    "company_name": beneficiary.get("company_name", ""),
                    "beneficiary_type": beneficiary.get("beneficiary_type", "direct"),
                    "company_role": beneficiary.get("company_role", ""),
                    "relevance_score": beneficiary.get("relevance_score", 0.0),
                    "signal_count": beneficiary.get("signal_count", 0),
                    "capex_signals": beneficiary.get("capex_signals", 0),
                    "quarterly_mentions": _json.dumps(beneficiary.get("quarterly_mentions", {})),
                    "first_seen_at": beneficiary.get("first_seen_at"),
                    "last_seen_at": beneficiary.get("last_seen_at", date.today()),
                    "rank_in_theme": beneficiary.get("rank_in_theme"),
                    "reasoning": beneficiary.get("reasoning"),
                    "window_start": beneficiary.get("window_start"),
                    "window_end": beneficiary.get("window_end"),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    # ----------------------------------------------------------
    # BUSINESS EVENTS  (event-centric architecture)
    # ----------------------------------------------------------
    def insert_event(self, event: dict) -> int:
        """Insert a business event. ON CONFLICT keeps the best-confidence version."""
        sql = """
            INSERT INTO mg_events
                (document_id, event_type, subject_entity, subject_type, description,
                 magnitude, magnitude_unit, direction, confidence, second_order,
                 context_text, filed_at)
            VALUES
                (%(document_id)s, %(event_type)s, %(subject_entity)s, %(subject_type)s,
                 %(description)s, %(magnitude)s, %(magnitude_unit)s, %(direction)s,
                 %(confidence)s, %(second_order)s, %(context_text)s, %(filed_at)s)
            ON CONFLICT (document_id, event_type, COALESCE(subject_entity, ''))
            WHERE document_id IS NOT NULL
            DO UPDATE SET
                confidence  = GREATEST(mg_events.confidence, EXCLUDED.confidence),
                magnitude   = COALESCE(EXCLUDED.magnitude,   mg_events.magnitude),
                description = COALESCE(EXCLUDED.description, mg_events.description)
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "document_id": event.get("document_id"),
                    "event_type": event.get("event_type", ""),
                    "subject_entity": event.get("subject_entity", ""),
                    "subject_type": event.get("subject_type", "Company"),
                    "description": event.get("description", ""),
                    "magnitude": event.get("magnitude"),
                    "magnitude_unit": event.get("magnitude_unit", ""),
                    "direction": event.get("direction", "positive"),
                    "confidence": event.get("confidence", 0.75),
                    "second_order": event.get("second_order_entities", []),
                    "context_text": event.get("context_text", ""),
                    "filed_at": event.get("filed_at"),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def get_events_by_type(self, event_type: str, days: int = 90) -> list[dict]:
        sql = """
            SELECT e.*, d.company, d.ticker
            FROM mg_events e
            LEFT JOIN mg_documents d ON d.id = e.document_id
            WHERE e.event_type = %s
              AND e.filed_at >= NOW() - INTERVAL '%s days'
            ORDER BY e.filed_at DESC
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (event_type, days))
                return [dict(r) for r in cur.fetchall()]

    def get_recent_events(self, days: int = 90, direction: str = None) -> list[dict]:
        base = """
            SELECT e.*, d.company, d.ticker
            FROM mg_events e
            LEFT JOIN mg_documents d ON d.id = e.document_id
            WHERE e.filed_at >= NOW() - INTERVAL '%s days'
        """
        params = [days]
        if direction:
            base += " AND e.direction = %s"
            params.append(direction)
        base += " ORDER BY e.filed_at DESC LIMIT 500"
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(base, params)
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # ENTITY TIMESERIES  (temporal intelligence)
    # ----------------------------------------------------------
    def upsert_entity_timeseries(self, record: dict) -> int:
        sql = """
            INSERT INTO mg_entity_timeseries
                (entity_id, period_date, period_type, mention_count, signal_count,
                 sentiment_avg, doc_count, sector_spread, velocity, acceleration, trend_direction)
            VALUES
                (%(entity_id)s, %(period_date)s, %(period_type)s, %(mention_count)s,
                 %(signal_count)s, %(sentiment_avg)s, %(doc_count)s, %(sector_spread)s,
                 %(velocity)s, %(acceleration)s, %(trend_direction)s)
            ON CONFLICT (entity_id, period_date, period_type) DO UPDATE SET
                mention_count   = EXCLUDED.mention_count,
                signal_count    = EXCLUDED.signal_count,
                sentiment_avg   = EXCLUDED.sentiment_avg,
                doc_count       = EXCLUDED.doc_count,
                sector_spread   = EXCLUDED.sector_spread,
                velocity        = EXCLUDED.velocity,
                acceleration    = EXCLUDED.acceleration,
                trend_direction = EXCLUDED.trend_direction
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "entity_id": record["entity_id"],
                    "period_date": record.get("period_date", date.today()),
                    "period_type": record.get("period_type", "monthly"),
                    "mention_count": record.get("mention_count", 0),
                    "signal_count": record.get("signal_count", 0),
                    "sentiment_avg": record.get("sentiment_avg"),
                    "doc_count": record.get("doc_count", 0),
                    "sector_spread": record.get("sector_spread", 0),
                    "velocity": record.get("velocity", 0.0),
                    "acceleration": record.get("acceleration", 0.0),
                    "trend_direction": record.get("trend_direction", "stable"),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def get_entity_timeseries(self, entity_id: int, periods: int = 12) -> list[dict]:
        sql = """
            SELECT * FROM mg_entity_timeseries
            WHERE entity_id = %s
            ORDER BY period_date DESC
            LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (entity_id, periods))
                return [dict(r) for r in cur.fetchall()]

    def get_accelerating_entities(self, limit: int = 20) -> list[dict]:
        """Return entities with highest acceleration in recent period."""
        sql = """
            SELECT e.canonical_name, e.entity_type, e.ticker,
                   ts.velocity, ts.acceleration, ts.trend_direction, ts.period_date
            FROM mg_entity_timeseries ts
            JOIN mg_entities e ON e.id = ts.entity_id
            WHERE ts.trend_direction = 'accelerating'
              AND ts.period_date >= CURRENT_DATE - INTERVAL '90 days'
            ORDER BY ts.acceleration DESC
            LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (limit,))
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # CAUSAL CHAINS
    # ----------------------------------------------------------
    def upsert_causal_chain(self, chain: dict) -> int:
        sql = """
            INSERT INTO mg_causal_chains
                (chain_id, chain_name, description, depth, terminal_effect,
                 activation_score, links, first_detected, last_scored_at, country)
            VALUES
                (%(chain_id)s, %(chain_name)s, %(description)s, %(depth)s,
                 %(terminal_effect)s, %(activation_score)s, %(links)s::jsonb,
                 %(first_detected)s, %(last_scored_at)s, %(country)s)
            ON CONFLICT (chain_id) DO UPDATE SET
                activation_score = EXCLUDED.activation_score,
                links            = EXCLUDED.links,
                country          = EXCLUDED.country,
                -- Keep the earliest first_detected so historical runs anchor chains
                -- to when they first appeared in data, not when first stored.
                last_scored_at   = COALESCE(EXCLUDED.last_scored_at, mg_causal_chains.last_scored_at),
                first_detected   = LEAST(
                    COALESCE(mg_causal_chains.first_detected, EXCLUDED.first_detected),
                    COALESCE(EXCLUDED.first_detected, mg_causal_chains.first_detected)
                ),
                updated_at       = NOW()
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "chain_id": chain["chain_id"],
                    "chain_name": chain.get("chain_name", ""),
                    "description": chain.get("description", ""),
                    "depth": chain.get("depth", 1),
                    "terminal_effect": chain.get("terminal_effect"),
                    "activation_score": chain.get("activation_score", 0.0),
                    "links": chain.get("links", "[]"),
                    "first_detected": chain.get("first_detected"),
                    "last_scored_at": chain.get("last_scored_at"),
                    "country": chain.get("country", "US"),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def get_active_causal_chains(self, min_score: float = 20.0) -> list[dict]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(
                    "SELECT * FROM mg_causal_chains WHERE is_active=TRUE AND activation_score>=%s "
                    "ORDER BY activation_score DESC",
                    (min_score,)
                )
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # CONTRADICTION DETECTION
    # ----------------------------------------------------------

    def ensure_contradictions_table(self) -> None:
        """Create mg_contradictions if it does not exist yet.

        Called once at pipeline init — idempotent.

        Columns:
            company, theme           — the narrative pair being compared
            from_quarter, to_quarter — e.g. "Q1-2024" → "Q2-2024"
            change_type              — ContradictionType value string
            from_sentiment           — sentiment score in [-1, +1] for from_quarter
            to_sentiment             — sentiment score in [-1, +1] for to_quarter
            delta                    — to_sentiment - from_sentiment
            confidence               — overall contradiction confidence
            evidence                 — JSONB with from_phrases, to_phrases, reversal_pairs
            detected_at              — timestamp of detection run
        """
        ddl = """
            CREATE TABLE IF NOT EXISTS mg_contradictions (
                id              SERIAL PRIMARY KEY,
                company         TEXT        NOT NULL,
                theme           TEXT        NOT NULL,
                from_quarter    TEXT        NOT NULL,
                to_quarter      TEXT        NOT NULL,
                change_type     TEXT        NOT NULL DEFAULT 'general_reversal',
                from_sentiment  FLOAT       NOT NULL DEFAULT 0,
                to_sentiment    FLOAT       NOT NULL DEFAULT 0,
                delta           FLOAT       NOT NULL DEFAULT 0,
                confidence      FLOAT       NOT NULL DEFAULT 0,
                evidence        JSONB,
                country         VARCHAR(10) NOT NULL DEFAULT 'US',
                detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (company, theme, from_quarter, to_quarter)
            );
            CREATE INDEX IF NOT EXISTS idx_mg_contradictions_detected
                ON mg_contradictions (detected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_mg_contradictions_company
                ON mg_contradictions (company, theme);
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)

    def batch_upsert_contradictions(self, contradictions: list[dict]) -> int:
        """Insert or update contradiction records.  Returns count written.

        Each dict: {company, theme, from_quarter, to_quarter, change_type,
                    from_sentiment, to_sentiment, delta, confidence, evidence}
        """
        if not contradictions:
            return 0
        import json as _json
        sql = """
            INSERT INTO mg_contradictions
                (company, theme, from_quarter, to_quarter, change_type,
                 from_sentiment, to_sentiment, delta, confidence, evidence, country, detected_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (company, theme, from_quarter, to_quarter) DO UPDATE SET
                change_type    = EXCLUDED.change_type,
                from_sentiment = EXCLUDED.from_sentiment,
                to_sentiment   = EXCLUDED.to_sentiment,
                delta          = EXCLUDED.delta,
                confidence     = EXCLUDED.confidence,
                evidence       = EXCLUDED.evidence,
                detected_at    = NOW()
        """
        rows = [
            (
                c["company"],
                c["theme"],
                c["from_quarter"],
                c["to_quarter"],
                c.get("type", "general_reversal"),
                c.get("from_sentiment", 0.0),
                c.get("to_sentiment", 0.0),
                c.get("delta", 0.0),
                c.get("confidence", 0.0),
                _json.dumps({
                    "from_phrases":   c.get("from_phrases", []),
                    "to_phrases":     c.get("to_phrases", []),
                    "reversal_pairs": c.get("reversal_pairs", []),
                }),
                c.get("country", "US"),
            )
            for c in contradictions
        ]
        with self._conn() as conn:
            with conn.cursor() as cur:
                from psycopg2.extras import execute_values
                execute_values(cur, sql, rows)
                return len(rows)

    def get_company_theme_quarter_snippets(
        self,
        lookback_days: int = 365,
        min_signals: int = 2,
        limit_combos: int = 500,
    ) -> list[dict]:
        """Aggregate signal context_text by (company, theme-entity, quarter).

        Returns rows suitable for contradiction detection:
            company, entity (theme proxy), quarter (e.g. 'Q1-2024'), snippets (text)

        Only returns (company, entity) combos that appear in ≥2 quarters so
        consecutive-quarter comparison is possible.
        """
        sql = """
            WITH base AS (
                SELECT
                    COALESCE(NULLIF(d.company,''), d.ticker) AS company,
                    e.canonical_name                         AS entity,
                    TO_CHAR(DATE_TRUNC('quarter', d.filed_at), 'Q"Q"-YYYY') AS quarter,
                    STRING_AGG(
                        s.context_text,
                        ' '
                        ORDER BY s.confidence DESC NULLS LAST
                    ) AS snippets
                FROM mg_signals s
                JOIN mg_documents d  ON d.id  = s.document_id
                JOIN mg_document_entities de ON de.document_id = s.document_id
                JOIN mg_entities e   ON e.id  = de.entity_id
                WHERE d.filed_at >= NOW() - (%s * INTERVAL '1 day')
                  AND s.context_text IS NOT NULL
                  AND length(s.context_text) > 15
                  AND e.entity_type IN ('TECHNOLOGY','PRODUCT','CONCEPT','SECTOR')
                  AND length(e.canonical_name) >= 3
                GROUP BY company, entity, quarter
                HAVING COUNT(*) >= %s
            ),
            multi_quarter AS (
                SELECT company, entity
                FROM base
                GROUP BY company, entity
                HAVING COUNT(DISTINCT quarter) >= 2
            )
            SELECT b.company, b.entity, b.quarter, b.snippets
            FROM base b
            JOIN multi_quarter mq ON mq.company = b.company AND mq.entity = b.entity
            ORDER BY b.company, b.entity, b.quarter
            LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (lookback_days, min_signals, limit_combos * 10))
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # NARRATIVE PROPAGATION
    # ----------------------------------------------------------
    def upsert_narrative_propagation(self, record: dict) -> int:
        sql = """
            INSERT INTO mg_theme_propagation
                (narrative_slug, narrative_name, origin_company, origin_date,
                 propagation_trail, sector_spread, sector_count, company_count,
                 velocity, acceleration, diffusion_score, is_confirmed, snapshot_date)
            VALUES
                (%(narrative_slug)s, %(narrative_name)s, %(origin_company)s, %(origin_date)s,
                 %(propagation_trail)s::jsonb, %(sector_spread)s, %(sector_count)s,
                 %(company_count)s, %(velocity)s, %(acceleration)s, %(diffusion_score)s,
                 %(is_confirmed)s, %(snapshot_date)s)
            ON CONFLICT (narrative_slug, snapshot_date) DO UPDATE SET
                velocity        = EXCLUDED.velocity,
                acceleration    = EXCLUDED.acceleration,
                diffusion_score = EXCLUDED.diffusion_score,
                sector_count    = EXCLUDED.sector_count,
                company_count   = EXCLUDED.company_count,
                is_confirmed    = EXCLUDED.is_confirmed,
                propagation_trail = EXCLUDED.propagation_trail,
                updated_at      = NOW()
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                import json as _json
                cur.execute(sql, {
                    "narrative_slug": record["narrative_slug"],
                    "narrative_name": record.get("narrative_name", ""),
                    "origin_company": record.get("origin_company", ""),
                    "origin_date": record.get("origin_date", date.today()),
                    "propagation_trail": _json.dumps(record.get("propagation_trail", [])),
                    "sector_spread": record.get("sector_spread", []),
                    "sector_count": record.get("sector_count", 0),
                    "company_count": record.get("company_count", 0),
                    "velocity": record.get("velocity", 0.0),
                    "acceleration": record.get("acceleration", 0.0),
                    "diffusion_score": record.get("diffusion_score", 0.0),
                    "is_confirmed": record.get("is_confirmed", False),
                    "snapshot_date": record.get("snapshot_date", date.today()),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def get_narrative_propagation(self, narrative_slug: str) -> list[dict]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(
                    "SELECT * FROM mg_theme_propagation WHERE narrative_slug=%s "
                    "ORDER BY snapshot_date DESC LIMIT 90",
                    (narrative_slug,)
                )
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # HISTORICAL REPLAY — date-windowed queries
    # ----------------------------------------------------------
    def get_signals_in_window(
        self,
        signal_type: str,
        since_date,
        as_of_date,
        country: str = None,
    ) -> list[dict]:
        """Signals enriched with all non-person entities from the same document.

        Each (signal, entity) pair becomes one row, so a single signal in a
        document that mentions GPU, AI, and NVIDIA produces three rows — one
        per entity. This allows detect_from_signal_clusters() to correctly
        group signals by what they are ABOUT rather than relying on entity_id
        linkage on the signal row itself (which is often NULL).

        Entity types excluded: PERSON, LOCATION, AMOUNT, DATE, TIME.
        Only TECHNOLOGY, COMPANY, SECTOR, CONCEPT, PRODUCT, REGULATION kept.

        country: if provided, restricts to signals from documents of that country.
        """
        country_clause = "AND d.country = %s" if country else ""
        sql = f"""
            SELECT
                s.id            AS signal_id,
                s.signal_type,
                s.direction,
                s.confidence,
                s.context_text,
                s.signal_value,
                s.signal_unit,
                s.document_id,
                s.filed_at,
                d.company,
                d.ticker        AS doc_ticker,
                d.filed_at      AS doc_filed_at,
                e.canonical_name,
                e.entity_type,
                e.ticker        AS entity_ticker
            FROM mg_signals s
            JOIN mg_documents d ON d.id = s.document_id
            JOIN mg_document_entities de ON de.document_id = s.document_id
            JOIN mg_entities e ON e.id = de.entity_id
            WHERE s.signal_type = %s
              AND d.filed_at >= %s
              AND d.filed_at <= %s
              {country_clause}
              AND e.entity_type NOT IN ('PERSON', 'LOCATION', 'AMOUNT', 'DATE', 'TIME')
              AND length(e.canonical_name) >= 3
            ORDER BY d.filed_at DESC
        """
        params = [signal_type, since_date, as_of_date] + ([country] if country else [])
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_all_signals_in_window(
        self,
        signal_types: list[str],
        since_date,
        as_of_date,
        country: str = None,
    ) -> list[dict]:
        """Lean signal query — only signals + documents (NO entity cross-join).

        The old version joined mg_document_entities × mg_entities, producing a
        cartesian product (~800K rows for a 3-year window).  Neither seed
        detection nor beneficiary mapping need entity columns — they use
        signal_type, company, context_text, filed_at which all live on the
        signal / document tables.

        Returns ~18K rows instead of ~800K → 40× less data to fetch/ship.
        """
        from psycopg2.extras import RealDictCursor
        sql = """
            SELECT
                s.id            AS signal_id,
                s.signal_type,
                s.direction,
                s.confidence,
                s.context_text,
                s.signal_value,
                s.signal_unit,
                s.document_id,
                s.filed_at,
                d.company,
                d.ticker        AS doc_ticker,
                d.filed_at      AS doc_filed_at
            FROM mg_signals s
            JOIN mg_documents d ON d.id = s.document_id
            WHERE s.signal_type = ANY(%s)
              AND d.filed_at >= %s
              AND d.filed_at <= %s
              {country_clause}
            ORDER BY d.filed_at DESC
        """
        country_clause = "AND d.country = %s" if country else ""
        sql = sql.format(country_clause=country_clause)
        params = [signal_types, since_date, as_of_date] + ([country] if country else [])
        with self._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_existing_theme_ids(self, theme_ids: set[int]) -> set[int]:
        """Return the subset of theme_ids that still exist in mg_themes."""
        if not theme_ids:
            return set()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM mg_themes WHERE id = ANY(%s)",
                    (list(theme_ids),)
                )
                return {row[0] for row in cur.fetchall()}

    def get_entity_signal_clusters_in_window(
        self,
        signal_types: list[str],
        since_date,
        as_of_date,
        min_signal_count: int = 2,
        limit: int | None = None,
        country: str = None,
    ) -> list[dict]:
        """Pre-aggregated entity-signal clusters for theme detection.

        Returns ONE ROW PER ENTITY (not one row per signal×entity pair).
        Replaces shipping 600K+ raw rows to Python — the DB does the GROUP BY.

        Each row contains:
            canonical_name, entity_type,
            companies          (list of distinct company names)
            document_ids       (list of distinct document IDs)
            signal_type_counts (dict: signal_type → count)
            total_signals      (int)
            capex_count        (int)
            first_signal_date       (date — earliest filing that generated a signal)
            quarter_count           (int — number of distinct fiscal quarters with signals)
            constraint_keyword_count (int — signals whose context contains shortage/backlog/etc.)
        """
        from psycopg2.extras import RealDictCursor
        sql = """
            WITH raw AS (
                SELECT
                    e.canonical_name,
                    e.entity_type,
                    COALESCE(NULLIF(d.company, ''), d.ticker) AS company,
                    s.document_id,
                    s.signal_type,
                    CASE WHEN s.signal_type LIKE '%%capex%%' THEN 1 ELSE 0 END AS is_capex,
                    d.filed_at,
                    -- Constraint keyword hit: 1 if signal context contains a supply
                    -- tightness phrase (shortage, backlog, sold out, etc.), else 0.
                    -- These carry far higher investment signal than generic mentions.
                    CASE WHEN s.context_text ILIKE ANY(ARRAY[
                        '%%shortage%%','%%allocation%%','%%lead time%%','%%backlog%%',
                        '%%fully booked%%','%%sold out%%','%%bottleneck%%','%%constrained%%',
                        '%%rationing%%','%%wait list%%','%%supply tight%%','%%capacity tight%%',
                        '%%order push%%','%%push-out%%','%%push out%%','%%extended lead%%',
                        '%%cannot meet demand%%','%%demand exceeds%%','%%supply limited%%'
                    ]) THEN 1 ELSE 0 END AS is_constraint_kw
                FROM mg_signals s
                JOIN mg_documents d       ON d.id = s.document_id
                JOIN mg_document_entities de ON de.document_id = s.document_id
                JOIN mg_entities e        ON e.id = de.entity_id
                WHERE s.signal_type = ANY(%s)
                  AND d.filed_at >= %s
                  AND d.filed_at <= %s
                  AND (%s IS NULL OR d.country = %s)
                  -- Only theme-forming entity types: technology, sector, product concepts.
                  -- COMPANY and CONCEPT types are excluded here because ~95%% of COMPANY
                  -- entities in India filings are boilerplate ("Audit Committee", "Standalone",
                  -- "Limited Review Report") and CONCEPT types are regulatory phrases.
                  -- Real companies appear as BENEFICIARIES of themes (mapped separately);
                  -- theme ENTITIES must be investable technology/sector/product concepts.
                  AND e.entity_type IN ('TECHNOLOGY','SECTOR','PRODUCT')
                  AND length(e.canonical_name) >= 4
                  -- Must start with a letter (excludes punctuation, symbol, number prefixes)
                  AND e.canonical_name ~ '^[A-Za-z]'
                  -- Exclude ISO dates anywhere in the name (e.g. "TreasuryMember 2019-12-31")
                  AND e.canonical_name !~ '[0-9]{4}-[0-9]{2}-[0-9]{2}'
                  -- Exclude month-name dates: "March 31, 2020", "July 2"
                  AND e.canonical_name !~* '^(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec) '
                  -- Exclude pure year strings: "2020", "2019-2022"
                  AND e.canonical_name !~ '^(19|20)[0-9]{2}[ -]'
                  AND e.canonical_name !~ '^(19|20)[0-9]{2}$'
                  -- Exclude SEC form labels: "Item 1", "Item 1A", "Exhibit 10"
                  AND e.canonical_name !~* '^(item|exhibit|section|schedule|form|note|table|part|appendix) +[0-9a-z]'
                  -- Exclude strings with 7+ digit runs (CIK / accession numbers)
                  AND e.canonical_name !~ '[0-9]{7}'
                  -- Exclude single-word generic labels and SEC boilerplate
                  AND lower(e.canonical_name) NOT IN (
                      'controller','director','officer','member','trustee','signature',
                      'pursuant','amendment','registration','incorporated','corporation',
                      'buildout','quarterly','annual','fiscal','common','preferred',
                      'class','series','shares','stock','equity','debt','notes','bonds',
                      'interest','principal','maturity','coupon','dividend','yield',
                      'total','subtotal','net','gross','other','various','certain',
                      'following','including','excluding','related','associated',
                      'applicable','required','permitted','authorized',
                      -- Temporal / reporting-period single words
                      'monthly','weekly','annually','semi-annually','biannually',
                      'annually','annually','daily','hourly',
                      'plan','plans','program','programs','policy','policies',
                      'exhibit','exhibits','filer','registrant','issuer',
                      'committee','committees','board','management','officers',
                      -- Generic economic nouns (signal contexts, not investable themes)
                      'inflation','deflation','recession','liquidity','credit',
                      'overcapacity','capacity','demand','supply','pricing','volume',
                      'growth','expansion','profitability','productivity','efficiency',
                      'technology','innovation','automation','digitalization',
                      'infrastructure','platform','software','hardware','services',
                      'solutions','products','systems','applications',
                      -- Ordinals and number words
                      'first','second','third','fourth','fifth','sixth','seventh',
                      'eighth','ninth','tenth','one','two','three','four','five',
                      'six','seven','eight','nine','ten','eleven','twelve','thirteen',
                      -- GICS sectors (valid types but not theme-worthy standalone)
                      'industrials','financials','utilities','materials','healthcare',
                      'energy','consumer','communication','real',
                      -- SEC/legal generic
                      'registrant','issuer','subsidiaries','subsidiary','affiliates',
                      'consolidated','unaudited','audited','condensed','interim',
                      'operating','financial','statements','report','filing',
                      'forward','looking','cautionary','statement','commission',
                      'shortage','shortages','geopolitical','geopolitics','notes',
                      -- India audit/report single-word noise
                      'standalone','standalone','particulars','auditors','auditor',
                      'responsibilities','group','rules','year','circular',
                      -- Additional single-word corporate/legal/financial noise
                      'operations','operation','treasury',
                      'agreement','agreements','charter','charters',
                      'diluted','accretive','dilutive','compensatory','compensation',
                      'today','item','items',
                      'proceeds','consideration','transaction','transactions',
                      'amendment','amendments','notice','notices',
                      'certificate','certificates','prospectus',
                      'warrant','warrants','covenant','covenants',
                      'authorization','authorizations','approval','approvals',
                      'allocation','allocations','distribution','distributions',
                      'measurement','measurements','assessment','assessments'
                  )
                  -- Exclude SEC section headers and legal boilerplate (multi-word)
                  AND lower(e.canonical_name) NOT IN (
                      'the board of directors','board of directors',
                      'the private securities litigation reform act',
                      'securities litigation reform act',
                      'security ownership of certain beneficial owners and management',
                      'principal accounting fees and services',
                      'selected financial data','financial statements and supplementary data',
                      'management discussion and analysis','risk factors',
                      'quantitative and qualitative disclosures about market risk',
                      'controls and procedures','unresolved staff comments',
                      'executive compensation','consumer discretionary',
                      'communication services','information technology',
                      'real estate','consumer staples','health care',
                      'new york stock exchange','date of report',
                      'commission file','common stock','equity securities',
                      'contracts with customers','supplementary data',
                      'financial statement schedules','address of principal'
                  )
                  -- Exclude names with 7+ words (SEC section headers)
                  AND array_length(string_to_array(e.canonical_name, ' '), 1) <= 6
                  -- Exclude multi-word temporal / SEC reporting period phrases
                  AND lower(e.canonical_name) NOT IN (
                      'three months','six months','nine months','twelve months',
                      'the year','the quarter','the period','the month',
                      'second fiscal quarter','third fiscal quarter',
                      'first fiscal quarter','fourth fiscal quarter',
                      'underwriting agreement','the underwriting agreement',
                      'the committee','the board','the company',
                      'park avenue','madison avenue','wall street',
                      'capital expenditure','capital expenditures',
                      'monetary policy','fiscal policy',
                      'exchange act','securities act',
                      -- Temporal multi-word phrases
                      'one year','two year','three year','four year','five year',
                      'one month','two months','three months ended',
                      -- Generic multi-word noise
                      'the agreement','the transaction','the offering',
                      'the amendment','the warrant','the certificate',
                      'the charter','the prospectus','the covenant',
                      'operating operations','treasury operations',
                      'general operations','business operations',
                      -- India audit / financial statement boilerplate
                      'independent auditor''s report','the independent auditor''s report',
                      'auditor''s report','the auditor''s report',
                      'auditor''s responsibilities','the auditor''s responsibilities',
                      'internal auditors','the internal auditors',
                      'audited standalone','the audited standalone',
                      'standalone financial results','the standalone financial results',
                      'consolidated financial results','the consolidated financial results',
                      'the indian accounting standards','indian accounting standards',
                      'accounting standards',
                      'the group','particulars year',
                      'full year','the full year',
                      'the last three years','the beginning of the year',
                      'the 4th quarter','quarter four','quarter three','quarter two','quarter one',
                      'government of india','the government of india',
                      'companies act','the companies act'
                  )
                  -- Exclude bank / underwriter names that appear in every equity filing
                  AND lower(e.canonical_name) !~ '(bofa|merrill lynch|goldman sachs|morgan stanley|jp morgan|wells fargo|citigroup|barclays|credit suisse|national association|bancorp)'
                  -- Exclude SEC location/boilerplate prefixes and abstract noise
                  AND e.canonical_name !~* '^(d[.]?c[.]?\\s*[0-9]|washington|united states|securities and exchange|commission file|irs employer|state of |address of)'
                  AND e.canonical_name !~* '^(shortage|shortages|geopolitical|geopolitics)$'
                  AND e.canonical_name !~ '^[0-9]{5}'   -- ZIP codes like 20549
                  -- ── India mega-boilerplate: entities appearing in virtually every filing ──
                  -- These saturate the LIMIT before real investment entities are reached.
                  -- They are blocked here (SQL layer) so they never compete for the row budget.
                  AND lower(e.canonical_name) NOT IN (
                      'sebi','rbi','nclt','nclat','cci','mca',             -- regulators
                      'dalal street','g block','bandra kurla','bkc',       -- addresses
                      'the companies act','companies act',
                      'the listing regulations','listing regulations',
                      'the regulation 33','regulation 33',
                      'disclosure requirements) regulations',
                      'the institute of chartered accountants',
                      'the audit committee','audit committee',
                      'the statutory auditors','statutory auditors',
                      'inter alia','pursuant to regulation 30',
                      'bse limited','nse limited',
                      'indian accounting standards','the indian accounting standards',
                      'interim financial reporting',
                      'the holding company','holding company',
                      'the group','group',
                      'standalone','consolidated',
                      'financials','financial',
                      'year','this quarter','last year',
                      'materials','power','energy',   -- sector names alone are too generic for entity-level
                      'indian','india'
                  )
                  -- Exclude India regulatory circular IDs: CFD/CMD/4/2015 etc.
                  AND e.canonical_name !~ '^[A-Za-z]{2,}/[A-Za-z]{2,}/[0-9]'
                  -- Exclude FY / quarter labels: FY2026, Q3FY20 etc.
                  AND e.canonical_name !~* '^(fy|q[1-4]fy)[0-9]'
                  -- PRODUCT-type noise that slips through (audit/reporting phrases)
                  AND lower(e.canonical_name) NOT IN (
                      'conclude','report','standards','codes','shareholders',
                      'standalone financial results','financial results',
                      'the financial results','auditor','auditors',
                      'chartered','chartered accountants',
                      'as 108','as 34','as 116','as 115',   -- Ind AS numbers
                      'intangible','deferred tax','total equity',
                      'q1 fy26','q2 fy26','q3 fy26','q4 fy26',
                      'q1 fy25','q2 fy25','q3 fy25','q4 fy25'
                  )
            ),
            -- Count distinct companies per entity to detect ubiquitous boilerplate.
            -- Entities appearing in >55%% of all companies are filing boilerplate,
            -- not investable themes.
            company_counts AS (
                SELECT canonical_name, COUNT(DISTINCT company) AS n_cos
                FROM raw
                WHERE company IS NOT NULL
                GROUP BY canonical_name
            ),
            total_cos AS (
                SELECT COUNT(DISTINCT company) AS total FROM raw WHERE company IS NOT NULL
            ),
            sig_counts AS (
                SELECT
                    canonical_name,
                    signal_type,
                    COUNT(*) AS cnt
                FROM raw
                GROUP BY canonical_name, signal_type
            ),
            sig_json AS (
                SELECT
                    canonical_name,
                    jsonb_object_agg(signal_type, cnt) AS signal_type_counts,
                    SUM(cnt) AS total_signals
                FROM sig_counts
                GROUP BY canonical_name
            )
            SELECT
                r.canonical_name,
                r.entity_type,
                array_remove(array_agg(DISTINCT r.company), NULL)  AS companies,
                array_agg(DISTINCT r.document_id)                  AS document_ids,
                sj.signal_type_counts,
                sj.total_signals::int                              AS total_signals,
                SUM(r.is_capex)::int                               AS capex_count,
                MIN(r.filed_at)::date                              AS first_signal_date,
                MAX(r.filed_at)::date                              AS last_signal_date,
                COUNT(DISTINCT date_trunc('quarter', r.filed_at))::int AS quarter_count,
                SUM(r.is_constraint_kw)::int                       AS constraint_keyword_count,
                -- Signals in the most recent 90 days vs the full window.
                -- Used downstream to compute a recency weight so new surges
                -- outrank old persistent themes with large historical doc counts.
                COUNT(DISTINCT r.document_id) FILTER (
                    WHERE r.filed_at >= %s - INTERVAL '90 days'
                )::int                                             AS recent_doc_count
            FROM raw r
            JOIN sig_json sj ON sj.canonical_name = r.canonical_name
            JOIN company_counts cc ON cc.canonical_name = r.canonical_name
            CROSS JOIN total_cos tc
            GROUP BY r.canonical_name, r.entity_type,
                     sj.signal_type_counts, sj.total_signals,
                     cc.n_cos, tc.total
            HAVING SUM(r.is_capex) + sj.total_signals >= %s
              -- Ubiquity guard: entities in >22%% of all companies are boilerplate.
              -- With TECHNOLOGY/SECTOR/PRODUCT types this rarely filters real themes.
              AND (cc.n_cos::float / NULLIF(tc.total, 0)) < 0.22
            -- Order by investment-signal breadth (company count for capex/demand signals)
            -- then by recency. Real emerging themes have both breadth AND freshness.
            ORDER BY cc.n_cos DESC, sj.total_signals DESC
        """
        params = [signal_types, since_date, as_of_date,
                  country, country,
                  as_of_date,       # recent_doc_count FILTER (as_of - 90d)
                  min_signal_count]
        if limit is not None:
            sql += "\n            LIMIT %s"
            params.append(limit)
        with self._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_companies_by_theme_entity(
        self,
        entity_keywords: list[str],
        since_date,
        as_of_date,
        country: str = None,
        min_mentions: int = 2,
    ) -> list[dict]:
        """Find companies whose documents explicitly contain a theme entity
        AND have at least one investment-relevant signal in the same window.

        This is Beneficiary Strategy 4: catches companies like Shakti Pumps (solar)
        where the keyword appears in extracted entities but NOT always in the
        200-char signal context window that Strategy 1 requires.

        Returns: list of {company, ticker, entity_mentions, signal_count, filed_at_max}
        """
        if not entity_keywords:
            return []
        from psycopg2.extras import RealDictCursor
        kw_patterns = [f"%{kw}%" for kw in entity_keywords]
        # Build OR conditions for each keyword
        kw_conditions = " OR ".join(
            "lower(e.canonical_name) LIKE %s" for _ in kw_patterns
        )
        country_clause = "AND d.country = %s" if country else ""
        sql = f"""
            SELECT
                COALESCE(NULLIF(d.company,''), d.ticker)  AS company,
                d.ticker,
                COUNT(DISTINCT de.id)                     AS entity_mentions,
                COUNT(DISTINCT s.id)                      AS signal_count,
                MAX(d.filed_at)::date                     AS filed_at_max
            FROM mg_document_entities de
            JOIN mg_entities e  ON e.id = de.entity_id
            JOIN mg_documents d ON d.id = de.document_id
            -- Only count if there's also an investment signal in the same doc window
            JOIN mg_signals s   ON s.document_id = d.id
                AND s.signal_type IN (
                    'demand_surge','supply_bottleneck','capex_increase',
                    'technology_adoption','regulatory_tailwind',
                    'hiring_surge','market_entry'
                )
            WHERE ({kw_conditions})
              AND d.filed_at >= %s AND d.filed_at <= %s
              {country_clause}
              AND COALESCE(NULLIF(d.company,''), d.ticker) IS NOT NULL
            GROUP BY d.company, d.ticker
            HAVING COUNT(DISTINCT de.id) >= %s
            ORDER BY entity_mentions DESC
            LIMIT 100
        """
        params = kw_patterns + [since_date, as_of_date]
        if country:
            params.append(country)
        params.append(min_mentions)
        with self._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_entities_in_window(self, since_date, as_of_date, country: str = None) -> list[dict]:
        """Entities seen in documents filed between since_date and as_of_date.

        Joins through mg_document_entities → mg_documents using d.filed_at so
        that historical replay windows (e.g. 2022-01-01 → 2022-01-31) correctly
        return entities from that period, regardless of when the NLP ran.

        country: if provided, restricts to entities extracted from documents of
                 that country (e.g. 'IN' for India, 'US' for US). This prevents
                 US companies from appearing as beneficiaries in India theme runs.
        """
        country_clause = "AND d.country = %s" if country else ""
        sql = f"""
            SELECT DISTINCT ON (e.id)
                   e.id, e.canonical_name, e.entity_type, e.ticker,
                   0.0 AS sentiment_score
            FROM mg_entities e
            JOIN mg_document_entities de ON de.entity_id = e.id
            JOIN mg_documents d          ON d.id = de.document_id
            WHERE d.filed_at >= %s
              AND d.filed_at <= %s
              {country_clause}
              AND e.entity_type IN ('COMPANY','TECHNOLOGY','SECTOR','PRODUCT','CONCEPT','REGULATION','ORG')
              AND length(e.canonical_name) >= 3
            ORDER BY e.id, e.mention_count DESC
            LIMIT 5000
        """
        params = [since_date, as_of_date] + ([country] if country else [])
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_documents_for_replay(
        self,
        status: str,
        window_start,
        window_end,
        limit: int = 500,
        country: str = None,
    ) -> list[dict]:
        """Return documents with the given status whose filed_at falls in the window.

        Used by HistoricalRunner to process only the batch available at replay_date.
        """
        country_clause = "AND country = %s" if country else ""
        params = [status, window_start, window_end] + ([country] if country else []) + [limit]
        sql = f"""
            SELECT * FROM mg_documents
            WHERE processing_status = %s
              AND filed_at >= %s
              AND filed_at <= %s
              {country_clause}
            ORDER BY filed_at ASC
            LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_all_documents_as_of(self, status: str, as_of_date, limit: int = 500) -> list[dict]:
        """Return documents processed up to as_of_date (inclusive filed_at).

        Looks back across all prior batches, not just the current window.
        Used for theme detection which aggregates across all known history.
        """
        sql = """
            SELECT * FROM mg_documents
            WHERE processing_status IN ('nlp_done', 'graph_built', 'embedded')
              AND filed_at <= %s
            ORDER BY filed_at DESC
            LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (as_of_date, limit))
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # THEME PERFORMANCE  (forward-return validation)
    # ----------------------------------------------------------
    def upsert_theme_performance(self, record: dict) -> int:
        """Record or update a theme prediction entry.

        Forward return fields (forward_30d_return etc.) can be NULL at detection
        time and filled later once the forward window has elapsed.
        """
        sql = """
            INSERT INTO mg_theme_performance
                (theme_id, theme_slug, ticker, company_name, detection_date,
                 detection_score, conviction,
                 forward_30d_return, forward_90d_return,
                 forward_180d_return, forward_365d_return,
                 benchmark_30d, benchmark_90d, benchmark_180d, benchmark_365d,
                 measured_at, replay_batch)
            VALUES
                (%(theme_id)s, %(theme_slug)s, %(ticker)s, %(company_name)s,
                 %(detection_date)s, %(detection_score)s, %(conviction)s,
                 %(forward_30d_return)s, %(forward_90d_return)s,
                 %(forward_180d_return)s, %(forward_365d_return)s,
                 %(benchmark_30d)s, %(benchmark_90d)s,
                 %(benchmark_180d)s, %(benchmark_365d)s,
                 %(measured_at)s, %(replay_batch)s)
            ON CONFLICT (theme_slug, ticker, detection_date) DO UPDATE SET
                forward_30d_return  = COALESCE(EXCLUDED.forward_30d_return,  mg_theme_performance.forward_30d_return),
                forward_90d_return  = COALESCE(EXCLUDED.forward_90d_return,  mg_theme_performance.forward_90d_return),
                forward_180d_return = COALESCE(EXCLUDED.forward_180d_return, mg_theme_performance.forward_180d_return),
                forward_365d_return = COALESCE(EXCLUDED.forward_365d_return, mg_theme_performance.forward_365d_return),
                benchmark_30d       = COALESCE(EXCLUDED.benchmark_30d,       mg_theme_performance.benchmark_30d),
                benchmark_90d       = COALESCE(EXCLUDED.benchmark_90d,       mg_theme_performance.benchmark_90d),
                benchmark_180d      = COALESCE(EXCLUDED.benchmark_180d,      mg_theme_performance.benchmark_180d),
                benchmark_365d      = COALESCE(EXCLUDED.benchmark_365d,      mg_theme_performance.benchmark_365d),
                detection_score     = GREATEST(EXCLUDED.detection_score,     mg_theme_performance.detection_score),
                measured_at         = EXCLUDED.measured_at,
                updated_at          = NOW()
            RETURNING id
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, {
                    "theme_id": record.get("theme_id"),
                    "theme_slug": record["theme_slug"],
                    "ticker": record["ticker"],
                    "company_name": record.get("company_name", ""),
                    "detection_date": record["detection_date"],
                    "detection_score": record.get("detection_score", 0.0),
                    "conviction": record.get("conviction", "emerging"),
                    "forward_30d_return": record.get("forward_30d_return"),
                    "forward_90d_return": record.get("forward_90d_return"),
                    "forward_180d_return": record.get("forward_180d_return"),
                    "forward_365d_return": record.get("forward_365d_return"),
                    "benchmark_30d": record.get("benchmark_30d"),
                    "benchmark_90d": record.get("benchmark_90d"),
                    "benchmark_180d": record.get("benchmark_180d"),
                    "benchmark_365d": record.get("benchmark_365d"),
                    "measured_at": record.get("measured_at"),
                    "replay_batch": record.get("replay_batch"),
                })
                row = cur.fetchone()
                return row["id"] if row else None

    def get_theme_performance(self, theme_slug: str) -> list[dict]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(
                    "SELECT * FROM mg_theme_performance WHERE theme_slug=%s "
                    "ORDER BY detection_date",
                    (theme_slug,)
                )
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # REPLAY RUN LOG
    # ----------------------------------------------------------
    def log_replay_run(self, run: dict):
        """Record the result of one HistoricalRunner monthly step."""
        sql = """
            INSERT INTO mg_replay_runs
                (replay_batch, replay_date, window_start, window_end,
                 docs_ingested, docs_nlp, nodes_built, edges_built,
                 themes_detected, themes_snapped, events_extracted,
                 causal_score, duration_sec, status, error_message)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    run.get("replay_batch", ""),
                    run.get("replay_date"),
                    run.get("window_start"),
                    run.get("window_end"),
                    run.get("docs_ingested", 0),
                    run.get("docs_nlp", 0),
                    run.get("nodes_built", 0),
                    run.get("edges_built", 0),
                    run.get("themes_detected", 0),
                    run.get("themes_snapped", 0),
                    run.get("events_extracted", 0),
                    run.get("causal_score"),
                    run.get("duration_sec", 0.0),
                    run.get("status", "ok"),
                    run.get("error_message"),
                ))

    # ----------------------------------------------------------
    # SOURCE CHECKPOINTS
    # ----------------------------------------------------------
    def get_checkpoint(self, source_name: str) -> Optional[datetime]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(
                    "SELECT last_fetched_at FROM mg_source_checkpoints WHERE source_name=%s",
                    (source_name,)
                )
                row = cur.fetchone()
                return row["last_fetched_at"] if row else None

    def set_checkpoint(self, source_name: str, fetched_at: datetime, doc_count: int = 0):
        sql = """
            INSERT INTO mg_source_checkpoints (source_name, last_fetched_at, last_doc_count, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (source_name) DO UPDATE SET
                last_fetched_at = EXCLUDED.last_fetched_at,
                last_doc_count  = EXCLUDED.last_doc_count,
                updated_at      = NOW()
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (source_name, fetched_at, doc_count))

    # ----------------------------------------------------------
    # PIPELINE RUN LOG
    # ----------------------------------------------------------
    def log_pipeline_run(self, run: dict):
        sql = """
            INSERT INTO mg_pipeline_runs
                (run_date, stage, docs_processed, entities_found, signals_found,
                 themes_updated, duration_sec, status, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    run.get("run_date", date.today()),
                    run.get("stage", ""),
                    run.get("docs_processed", 0),
                    run.get("entities_found", 0),
                    run.get("signals_found", 0),
                    run.get("themes_updated", 0),
                    run.get("duration_sec", 0.0),
                    run.get("status", "ok"),
                    run.get("error_message"),
                ))

    # ----------------------------------------------------------
    # THEME DETAIL QUERIES  (source companies + evidence + persistence)
    # ----------------------------------------------------------

    def get_source_companies_for_theme(
        self,
        theme_slug: str,
        as_of_date,
        since_date=None,
        limit: int = 50,
    ) -> list[dict]:
        """Companies whose SEC filings generated the signals that identified a theme.

        Returns one row per company, aggregated across all their documents
        that contain signals matching the theme's entity cluster.

        Fields: company, ticker, doc_count, signal_count, first_mention,
                last_mention, sectors (array), avg_confidence
        """
        sql = """
            SELECT
                d.company,
                d.ticker,
                COUNT(DISTINCT d.id)  AS doc_count,
                COUNT(s.id)           AS signal_count,
                MIN(d.filed_at)       AS first_mention,
                MAX(d.filed_at)       AS last_mention,
                ARRAY_AGG(DISTINCT d.doc_type) FILTER (WHERE d.doc_type IS NOT NULL) AS filing_types,
                ROUND(AVG(s.confidence)::numeric, 2) AS avg_confidence
            FROM mg_signals s
            JOIN mg_documents d ON d.id = s.document_id
            -- Join to theme via the entities that appear in these documents
            JOIN mg_document_entities de ON de.document_id = d.id
            JOIN mg_entities e ON e.id = de.entity_id
            JOIN mg_themes t ON (
                -- Entity name appears somewhere in the theme's top_entities JSONB array
                t.top_entities::text ILIKE '%%' || e.canonical_name || '%%'
                OR t.theme_name ILIKE '%%' || e.canonical_name || '%%'
            )
            WHERE t.theme_slug = %s
              AND d.filed_at <= %s
              AND (%s IS NULL OR d.filed_at >= %s)
              AND d.company IS NOT NULL
            GROUP BY d.company, d.ticker
            ORDER BY signal_count DESC, doc_count DESC
            LIMIT %s
        """
        _since = since_date
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (theme_slug, as_of_date, _since, _since, limit))
                return [dict(r) for r in cur.fetchall()]

    def get_signal_evidence_for_theme(
        self,
        theme_slug: str,
        as_of_date,
        since_date=None,
        limit: int = 30,
    ) -> list[dict]:
        """Actual text snippets from filings that identified this theme.

        Returns one row per signal with its context excerpt, company, date,
        and signal direction — the raw evidence behind the theme.

        Fields: company, ticker, filed_at, filing_type, signal_type,
                direction, context_text, confidence
        """
        sql = """
            SELECT
                d.company,
                d.ticker,
                d.filed_at,
                d.doc_type        AS filing_type,
                s.signal_type,
                s.direction,
                s.confidence,
                s.context_text
            FROM mg_signals s
            JOIN mg_documents d ON d.id = s.document_id
            JOIN mg_document_entities de ON de.document_id = d.id
            JOIN mg_entities e ON e.id = de.entity_id
            JOIN mg_themes t ON (
                t.top_entities::text ILIKE '%%' || e.canonical_name || '%%'
                OR t.theme_name ILIKE '%%' || e.canonical_name || '%%'
            )
            WHERE t.theme_slug = %s
              AND d.filed_at <= %s
              AND (%s IS NULL OR d.filed_at >= %s)
              AND s.context_text IS NOT NULL
              AND length(s.context_text) > 40
            ORDER BY s.confidence DESC, d.filed_at DESC
            LIMIT %s
        """
        _since = since_date
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (theme_slug, as_of_date, _since, _since, limit))
                return [dict(r) for r in cur.fetchall()]

    def get_confirmed_quarter_counts(self, theme_slugs: list[str]) -> dict[str, int]:
        """Batch fetch confirmed-quarter counts for a list of theme slugs.

        Returns {slug → n_confirmed_quarters} where a quarter is "confirmed"
        when the theme's max strength in that snapshot quarter >= 30.

        Single-query replacement for calling _count_active_quarters() per theme.
        Used by ThemeRanker to apply the persistence multiplier without N+1 queries.
        """
        if not theme_slugs:
            return {}
        sql = """
            SELECT t.theme_slug,
                   COUNT(DISTINCT DATE_TRUNC('quarter', s.snapshot_date))::int AS confirmed_quarters
            FROM mg_themes t
            JOIN mg_theme_snapshots s ON s.theme_id = t.id
            WHERE t.theme_slug = ANY(%s)
            GROUP BY t.theme_slug
            HAVING MAX(s.strength_score) >= 30
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (list(theme_slugs),))
                return {row["theme_slug"]: row["confirmed_quarters"] for row in cur.fetchall()}

    def get_quarterly_persistence(
        self,
        theme_id: int,
        as_of_date,
    ) -> list[dict]:
        """Return per-quarter snapshot summary for this theme up to as_of_date.

        Used to show Q1/Q2/Q3/Q4 confirmation badges.
        Returns one row per quarter: year, quarter (1-4), avg_strength,
        max_strength, doc_count, confirmed (bool: strength > 30).
        """
        sql = """
            SELECT
                EXTRACT(YEAR  FROM snapshot_date)::int AS year,
                EXTRACT(QUARTER FROM snapshot_date)::int AS quarter,
                ROUND(AVG(strength_score)::numeric, 1)  AS avg_strength,
                ROUND(MAX(strength_score)::numeric, 1)  AS max_strength,
                SUM(doc_count)::int                     AS doc_count,
                MAX(strength_score) >= 30               AS confirmed
            FROM mg_theme_snapshots
            WHERE theme_id = %s
              AND snapshot_date <= %s
            GROUP BY year, quarter
            ORDER BY year, quarter
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (theme_id, as_of_date))
                return [dict(r) for r in cur.fetchall()]

    def get_shortlisted_themes(
        self,
        min_quarters: int = 3,
        country: str = "US",
    ) -> list[dict]:
        """Return themes that have appeared in at least min_quarters distinct quarters,
        ordered by persistence (confirmed quarters DESC) then strength DESC.

        For each theme also returns the per-quarter strength series so the UI
        can draw a sparkline and show momentum direction.
        """
        sql = """
            WITH quarterly AS (
                SELECT
                    t.id,
                    t.theme_name,
                    t.theme_slug,
                    t.conviction,
                    t.strength_score,
                    t.momentum_score,
                    t.company_count,
                    t.first_detected,
                    t.country,
                    EXTRACT(YEAR    FROM s.snapshot_date)::int    AS yr,
                    EXTRACT(QUARTER FROM s.snapshot_date)::int    AS qtr,
                    ROUND(MAX(s.strength_score)::numeric, 1)      AS q_strength,
                    ROUND(AVG(s.momentum_score)::numeric, 1)      AS q_momentum,
                    SUM(s.doc_count)::int                         AS q_docs
                FROM mg_themes t
                JOIN mg_theme_snapshots s ON s.theme_id = t.id
                WHERE t.is_active = TRUE
                  AND s.strength_score >= 20
                  AND t.country = %s
                GROUP BY t.id, t.theme_name, t.theme_slug, t.conviction,
                         t.strength_score, t.momentum_score, t.company_count,
                         t.first_detected, t.country, yr, qtr
            ),
            -- Add row numbers so we can pick first/last quarter per theme
            -- without nesting aggregate functions (which PostgreSQL rejects).
            ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY id ORDER BY yr, qtr
                    ) AS rn_asc,
                    ROW_NUMBER() OVER (
                        PARTITION BY id ORDER BY yr DESC, qtr DESC
                    ) AS rn_desc
                FROM quarterly
            ),
            persistence AS (
                SELECT
                    id, theme_name, theme_slug, conviction,
                    strength_score, momentum_score, company_count, first_detected, country,
                    COUNT(*)                                        AS confirmed_quarters,
                    ROUND(AVG(q_strength)::numeric, 1)             AS avg_strength,
                    MAX(q_strength)                                 AS peak_strength,
                    MIN(q_strength)                                 AS trough_strength,
                    -- trend: most-recent quarter strength minus earliest quarter strength
                    MAX(CASE WHEN rn_desc = 1 THEN q_strength END)
                        - MAX(CASE WHEN rn_asc  = 1 THEN q_strength END) AS strength_trend,
                    json_agg(
                        json_build_object(
                            'year', yr, 'quarter', qtr,
                            'strength', q_strength,
                            'momentum', q_momentum,
                            'docs', q_docs
                        )
                        ORDER BY yr, qtr
                    ) AS quarter_series
                FROM ranked
                GROUP BY id, theme_name, theme_slug, conviction,
                         strength_score, momentum_score, company_count, first_detected, country
                HAVING COUNT(*) >= %s
            )
            SELECT *
            FROM persistence
            ORDER BY confirmed_quarters DESC, avg_strength DESC
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (country, min_quarters,))
                return [dict(r) for r in cur.fetchall()]

    def get_theme_macro_context(
        self,
        theme_slug: str,
        as_of_date,
    ) -> list[dict]:
        """Macro/policy links for this theme from the Constraint Engine."""
        sql = """
            SELECT
                mtl.link_type,
                mtl.strength,
                mtl.evidence_text,
                mtl.series_id,
                mtl.commodity_id,
                mtl.as_of_date,
                me.event_type,
                me.description    AS macro_description,
                me.severity,
                me.direction      AS macro_direction,
                pe.title          AS policy_title,
                pe.policy_type,
                pe.impact_direction,
                pe.sectors_affected
            FROM mg_macro_theme_links mtl
            LEFT JOIN mg_macro_events  me ON me.id  = mtl.macro_event_id
            LEFT JOIN mg_policy_events pe ON pe.id  = mtl.policy_event_id
            WHERE mtl.theme_slug = %s
              AND mtl.as_of_date <= %s
            ORDER BY mtl.strength DESC
            LIMIT 20
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (theme_slug, as_of_date))
                return [dict(r) for r in cur.fetchall()]

    # ----------------------------------------------------------
    # CONCALL / FILINGS ANALYSIS
    # ----------------------------------------------------------

    def get_concall_documents(
        self,
        country: str = "US",
        from_date=None,
        to_date=None,
        ticker_search: str = None,
        filing_type_filter: str = None,
        limit: int = 200,
    ) -> list[dict]:
        """Return filings/concall documents with signal and entity counts.

        country        : 'US' | 'IN' — filters by mg_documents.country
        from_date      : start of filing window (filed_at >=)
        to_date        : end of filing window   (filed_at <=)
        ticker_search  : partial ticker or company name match
        filing_type_filter : exact match on filing_type (or None for all)
        """
        clauses = ["d.country = %s"]
        params: list = [country]

        if from_date:
            clauses.append("d.filed_at >= %s")
            params.append(from_date)
        if to_date:
            clauses.append("d.filed_at <= %s")
            params.append(to_date)
        if ticker_search:
            clauses.append("(d.ticker ILIKE %s OR d.company ILIKE %s)")
            params.extend([f"%{ticker_search}%", f"%{ticker_search}%"])
        if filing_type_filter and filing_type_filter != "All":
            clauses.append("d.filing_type = %s")
            params.append(filing_type_filter)

        where = " AND ".join(clauses)
        params.append(limit)

        sql = f"""
            SELECT
                d.id,
                d.company,
                d.ticker,
                d.filing_type,
                d.doc_type,
                d.filed_at,
                d.fiscal_period,
                d.country,
                d.word_count,
                d.title,
                d.processing_status,
                COUNT(DISTINCT s.id)        AS signal_count,
                COUNT(DISTINCT de.entity_id) AS entity_count,
                ROUND(AVG(s.confidence)::numeric, 3) AS avg_confidence
            FROM mg_documents d
            LEFT JOIN mg_signals           s  ON s.document_id  = d.id
            LEFT JOIN mg_document_entities de ON de.document_id = d.id
            WHERE {where}
            GROUP BY d.id
            ORDER BY d.filed_at DESC NULLS LAST
            LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_document_signals(self, document_id: int, limit: int = 50) -> list[dict]:
        """Return signals extracted from a specific document, with entity name."""
        sql = """
            SELECT
                s.id,
                s.signal_type,
                s.direction,
                s.confidence,
                s.signal_value,
                s.signal_unit,
                s.context_text,
                s.filed_at,
                e.entity_text AS entity_name,
                e.entity_type
            FROM mg_signals s
            LEFT JOIN mg_entities e ON e.id = s.entity_id
            WHERE s.document_id = %s
            ORDER BY s.confidence DESC NULLS LAST
            LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (document_id, limit))
                return [dict(r) for r in cur.fetchall()]

    def get_document_theme_contributions(self, document_id: int) -> list[dict]:
        """Which themes received signals from this document."""
        sql = """
            SELECT DISTINCT
                t.theme_name,
                t.theme_slug,
                t.strength_score,
                t.conviction,
                t.country      AS theme_country,
                COUNT(s.id)    AS signal_count
            FROM mg_signals s
            JOIN mg_document_entities de
                ON de.document_id = s.document_id
            JOIN mg_entities e  ON e.id = de.entity_id
            JOIN mg_themes   t
                ON t.top_entities::text ILIKE '%' || e.entity_text || '%'
                OR t.theme_name ILIKE '%' || e.entity_text || '%'
            WHERE s.document_id = %s
              AND t.is_active = TRUE
            GROUP BY t.id
            ORDER BY signal_count DESC
            LIMIT 20
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                try:
                    cur.execute(sql, (document_id,))
                    return [dict(r) for r in cur.fetchall()]
                except Exception:
                    return []

    # ----------------------------------------------------------
    # COMPANY EXPLORER
    # ----------------------------------------------------------

    def get_company_profile(
        self,
        ticker: str,
        country: str = "US",
        as_of_date=None,
    ) -> dict:
        """Return filing/signal/theme summary for a company."""
        as_of = as_of_date or date.today()
        sql = """
            SELECT
                d.ticker,
                d.company,
                d.country,
                COUNT(DISTINCT d.id)                                        AS filing_count,
                COUNT(DISTINCT d.filing_type)                               AS filing_types_count,
                COUNT(DISTINCT s.id)                                        AS total_signals,
                ROUND(AVG(s.confidence)::numeric, 3)                        AS avg_confidence,
                MIN(d.filed_at)                                             AS first_filing,
                MAX(d.filed_at)                                             AS last_filing,
                array_agg(DISTINCT d.filing_type ORDER BY d.filing_type)    AS filing_types,
                COUNT(DISTINCT EXTRACT(YEAR FROM d.filed_at))               AS active_years
            FROM mg_documents d
            LEFT JOIN mg_signals s ON s.document_id = d.id
            WHERE d.ticker ILIKE %s
              AND d.country = %s
              AND d.filed_at <= %s
            GROUP BY d.ticker, d.company, d.country
            LIMIT 1
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (ticker.upper(), country, as_of))
                row = cur.fetchone()
                return dict(row) if row else {}

    def get_company_signal_timeline(
        self,
        ticker: str,
        from_date=None,
        to_date=None,
        country: str = "US",
    ) -> list[dict]:
        """Monthly signal counts + avg confidence for a company, for charting."""
        params = [ticker.upper(), country]
        date_clauses = ""
        if from_date:
            date_clauses += " AND d.filed_at >= %s"
            params.append(from_date)
        if to_date:
            date_clauses += " AND d.filed_at <= %s"
            params.append(to_date)

        sql = f"""
            SELECT
                DATE_TRUNC('month', d.filed_at)::date   AS month,
                COUNT(DISTINCT d.id)                     AS filings,
                COUNT(s.id)                              AS signals,
                ROUND(AVG(s.confidence)::numeric, 3)     AS avg_confidence,
                array_agg(DISTINCT d.filing_type)        AS filing_types
            FROM mg_documents d
            LEFT JOIN mg_signals s ON s.document_id = d.id
            WHERE d.ticker ILIKE %s
              AND d.country = %s
              {date_clauses}
              AND d.filed_at IS NOT NULL
            GROUP BY DATE_TRUNC('month', d.filed_at)
            ORDER BY month
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

    def get_company_themes(
        self,
        ticker: str,
        as_of_date=None,
        country: str = "US",
    ) -> list[dict]:
        """Return themes sourced from a specific company's filings."""
        as_of = as_of_date or date.today()
        sql = """
            SELECT DISTINCT
                t.theme_name,
                t.theme_slug,
                t.strength_score,
                t.momentum_score,
                t.conviction,
                t.first_detected,
                COUNT(DISTINCT s.id) AS company_signal_count,
                MAX(d.filed_at)      AS last_filing_date
            FROM mg_documents d
            JOIN mg_signals s ON s.document_id = d.id
            JOIN mg_theme_beneficiaries tb ON tb.ticker ILIKE d.ticker
            JOIN mg_themes t ON t.id = tb.theme_id
            WHERE d.ticker ILIKE %s
              AND d.country = %s
              AND d.filed_at <= %s
              AND t.is_active = TRUE
              AND t.country = %s
            GROUP BY t.id
            ORDER BY company_signal_count DESC
            LIMIT 20
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                try:
                    cur.execute(sql, (ticker.upper(), country, as_of, country))
                    return [dict(r) for r in cur.fetchall()]
                except Exception:
                    return []

    def search_companies(
        self,
        query: str,
        country: str = "US",
        limit: int = 20,
    ) -> list[dict]:
        """Typeahead search: find distinct tickers/companies matching query."""
        sql = """
            SELECT DISTINCT
                d.ticker,
                d.company,
                d.country,
                COUNT(DISTINCT d.id) AS filing_count,
                MAX(d.filed_at)      AS last_filing
            FROM mg_documents d
            WHERE d.country = %s
              AND (d.ticker ILIKE %s OR d.company ILIKE %s)
              AND d.ticker IS NOT NULL
            GROUP BY d.ticker, d.company, d.country
            ORDER BY filing_count DESC
            LIMIT %s
        """
        q = f"%{query}%"
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql, (country, q, q, limit))
                return [dict(r) for r in cur.fetchall()]

    def close(self):
        self._pool.closeall()
        logger.info("PGStore pool closed")

    # ─────────────────────────────────────────────────────────────────────────
    # RANKING LAYER — data loader
    # ─────────────────────────────────────────────────────────────────────────

    def get_ranking_data(self, date_from, date_to, country: str = 'US') -> dict:
        """
        Load all data needed by RankingEngine for a given date window.

        Every dataset is scoped to [date_from, date_to] so that changing the
        date range produces genuinely different rankings:

            themes        – themes that had at least one snapshot in the window
                            (scored using only that window's snapshot data)
            snapshots     – mg_theme_snapshots rows in [date_from, date_to]
            beneficiaries – mg_theme_beneficiaries rows whose last_seen_at
                            falls inside the window (who was active then?)
            signals       – mg_signals rows filed in [date_from, date_to]
            chains        – mg_causal_chains whose last_scored_at is in window
                            (falls back to all active chains if none qualify)
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._cursor_factory) as cur:

                # Snapshots in window — fetched first so we know which themes
                # were active during this period.
                cur.execute(
                    """SELECT theme_id, snapshot_date,
                              strength_score, momentum_score, doc_count, company_count
                       FROM mg_theme_snapshots
                       WHERE snapshot_date BETWEEN %s AND %s
                       ORDER BY theme_id, snapshot_date""",
                    (date_from, date_to),
                )
                snapshots = [dict(r) for r in cur.fetchall()]

                # Themes that actually had snapshot evidence in this window.
                # If none exist (pipeline hasn't run yet), fall back to all
                # active themes so the UI still shows something meaningful.
                active_in_window = {s["theme_id"] for s in snapshots}
                if active_in_window:
                    cur.execute(
                        """SELECT t.id, t.theme_name, t.theme_slug, t.conviction,
                                  t.strength_score, t.momentum_score,
                                  t.company_count, t.first_detected
                           FROM mg_themes t
                           WHERE t.is_active = TRUE
                             AND t.id = ANY(%s)
                             AND t.country = %s
                           ORDER BY t.strength_score DESC""",
                        (list(active_in_window), country),
                    )
                else:
                    cur.execute(
                        """SELECT id, theme_name, theme_slug, conviction,
                                  strength_score, momentum_score, company_count,
                                  first_detected
                           FROM mg_themes
                           WHERE is_active = TRUE
                             AND country = %s
                           ORDER BY strength_score DESC""",
                        (country,),
                    )
                themes = [dict(r) for r in cur.fetchall()]

                # Beneficiaries last seen WITHIN the date window — this ensures
                # that changing the date range surfaces the companies that were
                # actually relevant during that period, not just the latest run.
                # Falls back to the full set if nothing matches (e.g. fresh DB).
                cur.execute(
                    """WITH co_sigs AS (
                           -- Total signals per company IN THE SELECTED WINDOW.
                           -- Using window-scoped total prevents 6-year veterans from
                           -- dominating recent-company rankings: a company listed in 2026
                           -- competes on equal footing with one listed in 2020 when the
                           -- user selects a 2026-only date range.
                           SELECT COALESCE(NULLIF(d.company,''), d.ticker) AS co,
                                  COUNT(*) AS total_sigs
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE d.country = %s
                             AND d.filed_at BETWEEN %s AND %s
                           GROUP BY 1
                       )
                       SELECT tb.theme_id, tb.entity_id, tb.ticker, tb.company_name,
                              tb.beneficiary_type, tb.company_role,
                              tb.relevance_score,
                              -- Window-scoped total replaces all-time stored signal_count.
                              -- This makes rankings year-specific: selecting 2024 shows
                              -- companies active IN 2024, not their entire history.
                              COALESCE(cs.total_sigs, tb.signal_count) AS signal_count,
                              tb.rank_in_theme, tb.capex_signals,
                              tb.first_seen_at,
                              COALESCE(cs.total_sigs, 0) AS company_total_signals
                       FROM mg_theme_beneficiaries tb
                       JOIN mg_themes t ON t.id = tb.theme_id
                       LEFT JOIN co_sigs cs ON cs.co = tb.company_name
                       WHERE t.is_active = TRUE
                         AND t.country = %s
                         AND tb.company_name IS NOT NULL
                         AND tb.company_name != ''
                         AND tb.first_seen_at <= %s AND tb.last_seen_at >= %s
                       ORDER BY tb.theme_id, tb.rank_in_theme""",
                    (country, date_from, date_to,
                     country, date_to, date_from),
                )
                beneficiaries = [dict(r) for r in cur.fetchall()]

                # Fall back if no date-scoped beneficiaries (fresh pipeline run)
                if not beneficiaries:
                    cur.execute(
                        """WITH co_sigs AS (
                               SELECT COALESCE(NULLIF(d.company,''), d.ticker) AS co,
                                      COUNT(*) AS total_sigs
                               FROM mg_signals s
                               JOIN mg_documents d ON d.id = s.document_id
                               WHERE d.country = %s
                               GROUP BY 1
                           )
                           SELECT tb.theme_id, tb.entity_id, tb.ticker, tb.company_name,
                                  tb.beneficiary_type, tb.company_role,
                                  tb.relevance_score,
                                  COALESCE(cs.total_sigs, tb.signal_count) AS signal_count,
                                  tb.rank_in_theme, tb.capex_signals,
                                  tb.first_seen_at,
                                  COALESCE(cs.total_sigs, 0) AS company_total_signals
                           FROM mg_theme_beneficiaries tb
                           JOIN mg_themes t ON t.id = tb.theme_id
                           LEFT JOIN co_sigs cs ON cs.co = tb.company_name
                           WHERE t.is_active = TRUE
                             AND t.country = %s
                             AND tb.company_name IS NOT NULL
                             AND tb.company_name != ''
                           ORDER BY tb.theme_id, tb.rank_in_theme""",
                        (country, country),
                    )
                    beneficiaries = [dict(r) for r in cur.fetchall()]

                # Signals filed in the window — scoped to the selected country.
                # mg_signals.country is backfilled from mg_documents at ingest time.
                cur.execute(
                    """SELECT s.entity_id, s.signal_type, s.direction,
                              s.confidence, s.signal_value, s.filed_at,
                              e.ticker
                       FROM mg_signals s
                       LEFT JOIN mg_entities e ON e.id = s.entity_id
                       WHERE s.filed_at BETWEEN %s AND %s
                         AND s.country = %s""",
                    (date_from, date_to, country),
                )
                signals = [dict(r) for r in cur.fetchall()]

                # Causal chains whose last score falls in the window; fall back
                # to all active chains so theme bottleneck scoring still works.
                cur.execute(
                    """SELECT chain_id, chain_name, depth, terminal_effect,
                              activation_score, is_active
                       FROM mg_causal_chains
                       WHERE is_active = TRUE
                         AND (last_scored_at BETWEEN %s AND %s
                              OR last_scored_at IS NULL)
                       ORDER BY activation_score DESC""",
                    (date_from, date_to),
                )
                chains = [dict(r) for r in cur.fetchall()]
                if not chains:
                    cur.execute(
                        """SELECT chain_id, chain_name, depth, terminal_effect,
                                  activation_score, is_active
                           FROM mg_causal_chains
                           WHERE is_active = TRUE
                           ORDER BY activation_score DESC""",
                    )
                    chains = [dict(r) for r in cur.fetchall()]

                # ── v4 Fix #4: ticker-keyed signals via mg_documents.ticker ──
                # mg_document_entities → mg_entities join returns 0 rows because
                # mg_signals.entity_id is NULL for all 18 728 signals.
                # The correct path is: mg_signals.document_id → mg_documents.ticker
                # which yields ~5 000 rows per calendar year.
                # Fallback: if the date window has no signals (e.g. pre-2020 query),
                # load all signals without a date filter for enrichment purposes.
                try:
                    cur.execute(
                        """SELECT DISTINCT ON (d.ticker, s.signal_type)
                                  d.ticker, s.signal_type, s.direction,
                                  s.confidence, s.filed_at
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           WHERE s.filed_at BETWEEN %s AND %s
                             AND d.ticker IS NOT NULL
                             AND d.ticker != ''
                             AND d.country = %s
                           ORDER BY d.ticker, s.signal_type, s.confidence DESC""",
                        (date_from, date_to, country),
                    )
                    ticker_signals = [dict(r) for r in cur.fetchall()]
                except Exception:
                    ticker_signals = []

                # Fallback: if the selected window has no signals at all (date
                # range outside the data), load all signals for enrichment so
                # role inference and signal highlights still work.
                if not ticker_signals:
                    try:
                        cur.execute(
                            """SELECT DISTINCT ON (d.ticker, s.signal_type)
                                      d.ticker, s.signal_type, s.direction,
                                      s.confidence, s.filed_at
                               FROM mg_signals s
                               JOIN mg_documents d ON d.id = s.document_id
                               WHERE d.ticker IS NOT NULL
                                 AND d.ticker != ''
                                 AND d.country = %s
                               ORDER BY d.ticker, s.signal_type, s.confidence DESC
                               LIMIT 50000""",
                            (country,),
                        )
                        ticker_signals = [dict(r) for r in cur.fetchall()]
                    except Exception:
                        ticker_signals = []

                # ── Entity metadata for role confidence ───────────────────────
                # Fetch mention_count (source frequency) for beneficiary entities.
                # mg_entities.confidence is stored as 1.0 for all confirmed
                # entities so it contributes a flat NER score; mention_count
                # (range 1–42) is the real differentiator for source frequency.
                try:
                    cur.execute(
                        """SELECT DISTINCT ON (e.id)
                                  e.id AS entity_id, e.ticker,
                                  e.confidence, e.mention_count
                           FROM mg_entities e
                           JOIN mg_theme_beneficiaries tb ON tb.entity_id = e.id
                           WHERE tb.ticker IS NOT NULL AND tb.ticker != ''""",
                    )
                    entity_meta = [dict(r) for r in cur.fetchall()]
                except Exception:
                    entity_meta = []

                # ── Per-theme signal count in window (signal_intensity) ────────
                # Joins via mg_documents.ticker (same fix as ticker_signals above).
                # Fallback: if window has no signals, count across all time so
                # that signal_intensity is non-zero and themes can be differentiated.
                try:
                    cur.execute(
                        """SELECT tb.theme_id, COUNT(DISTINCT s.id) AS sig_count
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           JOIN mg_theme_beneficiaries tb ON tb.ticker = d.ticker
                           WHERE s.filed_at BETWEEN %s AND %s
                             AND d.ticker IS NOT NULL AND d.ticker != ''
                           GROUP BY tb.theme_id""",
                        (date_from, date_to),
                    )
                    theme_signal_counts = {
                        r["theme_id"]: int(r["sig_count"])
                        for r in cur.fetchall()
                    }
                except Exception:
                    theme_signal_counts = {}

                # All-time signal counts — ALWAYS fetched (not just as fallback).
                # Used as denominator in signal_intensity ratio:
                #   signal_intensity = window_count / all_time_count
                # This means a theme with 80% of its signals in 2021 scores 0.80
                # in the 2021 window and proportionally less in other windows —
                # creating genuine year-to-year differentiation even for themes
                # that appear in every year's snapshot.
                try:
                    cur.execute(
                        """SELECT tb.theme_id, COUNT(DISTINCT s.id) AS sig_count
                           FROM mg_signals s
                           JOIN mg_documents d ON d.id = s.document_id
                           JOIN mg_theme_beneficiaries tb ON tb.ticker = d.ticker
                           WHERE d.ticker IS NOT NULL AND d.ticker != ''
                           GROUP BY tb.theme_id""",
                    )
                    theme_signal_counts_all = {
                        r["theme_id"]: int(r["sig_count"])
                        for r in cur.fetchall()
                    }
                except Exception:
                    theme_signal_counts_all = {}

                # If window yielded no signals either (pre-data-range query),
                # fall back: treat window counts = all-time counts (ratio = 1.0)
                if not theme_signal_counts:
                    theme_signal_counts = theme_signal_counts_all

        return {
            "themes":                   themes,
            "snapshots":                snapshots,
            "beneficiaries":            beneficiaries,
            "signals":                  signals,
            "chains":                   chains,
            "ticker_signals":           ticker_signals,
            "entity_meta":              entity_meta,
            "theme_signal_counts":      theme_signal_counts,       # window
            "theme_signal_counts_all":  theme_signal_counts_all,   # all-time denominator
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
