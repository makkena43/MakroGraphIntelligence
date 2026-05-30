"""
Graph Store — Time Evolution in Graph
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
SQLite-based graph that stores Company→Theme→Quarter relationships and
tracks how narratives evolve over time. Designed to mirror a Neo4j property
graph structure so migration to Neo4j is a thin adapter swap when needed.

Node types:
    Company  (name, sector, country)
    Theme    (name, description)
    Quarter  (label: "Q1-2024", year, quarter_num)

Edge types:
    MENTIONS          (Company)-[:MENTIONS {mention_count, confidence, strength, capex, roles}]->(Theme)
                      per Quarter
    TRIGGERS          (MacroEvent)-[:TRIGGERS]->(Theme)
    CONTRADICTS       (Company)-[:CONTRADICTS {from_quarter, to_quarter, change_type}]->(Theme)

Schema keeps all temporal data in the edges so we can query:
    "Which themes were mentioned by 5+ companies with rising strength Q1→Q3 2024?"
    "Which companies flipped narrative on Memory from Q2 to Q3?"
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GraphStore:
    """
    SQLite graph store with Neo4j-compatible property model.
    All nodes and edges are typed; properties stored as JSON columns.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        db_path = Path(config.get("graph_db_path", "data/db/makrograph_graph.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        logger.info(f"GraphStore connected: {db_path}")

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self):
        self.conn.executescript("""
            -- ── NODE TABLES ──────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS companies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                sector      TEXT DEFAULT '',
                country     TEXT DEFAULT 'US',
                properties  TEXT DEFAULT '{}',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS themes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                properties  TEXT DEFAULT '{}',
                country     TEXT DEFAULT 'US',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quarters (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL UNIQUE,   -- e.g. "Q2-2024"
                year        INTEGER NOT NULL,
                quarter_num INTEGER NOT NULL,       -- 1..4
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- ── EDGE: Company MENTIONS Theme in Quarter ───────────────────
            CREATE TABLE IF NOT EXISTS mentions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id      INTEGER NOT NULL REFERENCES companies(id),
                theme_id        INTEGER NOT NULL REFERENCES themes(id),
                quarter_id      INTEGER NOT NULL REFERENCES quarters(id),
                -- ThemeSignal fields
                mention_count   INTEGER DEFAULT 0,
                confidence      REAL    DEFAULT 0.0,
                strength_score  REAL    DEFAULT 0.0,
                capex_mentioned INTEGER DEFAULT 0,   -- bool
                capex_count     INTEGER DEFAULT 0,
                has_negative    INTEGER DEFAULT 0,   -- bool
                -- CompanyRole fields (JSON array of role strings)
                roles           TEXT    DEFAULT '[]',
                primary_role    TEXT    DEFAULT '',
                -- Raw evidence
                snippets        TEXT    DEFAULT '[]', -- JSON array
                -- Source document reference
                source_doc_id   INTEGER DEFAULT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(company_id, theme_id, quarter_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mentions_theme_quarter
                ON mentions(theme_id, quarter_id);
            CREATE INDEX IF NOT EXISTS idx_mentions_company_quarter
                ON mentions(company_id, quarter_id);

            -- ── EDGE: Contradiction ───────────────────────────────────────
            CREATE TABLE IF NOT EXISTS contradictions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id      INTEGER NOT NULL REFERENCES companies(id),
                theme_id        INTEGER NOT NULL REFERENCES themes(id),
                from_quarter_id INTEGER NOT NULL REFERENCES quarters(id),
                to_quarter_id   INTEGER NOT NULL REFERENCES quarters(id),
                change_type     TEXT NOT NULL,  -- e.g. "positive_to_negative"
                from_sentiment  REAL DEFAULT 0.0,
                to_sentiment    REAL DEFAULT 0.0,
                evidence        TEXT DEFAULT '{}',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- ── EDGE: MacroEvent TRIGGERS Theme ──────────────────────────
            CREATE TABLE IF NOT EXISTS macro_triggers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        INTEGER NOT NULL,   -- FK to macro_events
                theme_id        INTEGER NOT NULL REFERENCES themes(id),
                relevance_score REAL    DEFAULT 0.5,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- ── THEME AGGREGATE SCORES per Quarter ───────────────────────
            -- Computed/refreshed by aggregate_theme_strength()
            CREATE TABLE IF NOT EXISTS theme_quarterly_scores (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                theme_id        INTEGER NOT NULL REFERENCES themes(id),
                quarter_id      INTEGER NOT NULL REFERENCES quarters(id),
                company_count   INTEGER DEFAULT 0,
                total_mentions  INTEGER DEFAULT 0,
                avg_confidence  REAL    DEFAULT 0.0,
                avg_strength    REAL    DEFAULT 0.0,
                capex_company_count INTEGER DEFAULT 0,
                composite_score REAL    DEFAULT 0.0,
                growth_vs_prev  REAL    DEFAULT 0.0,  -- vs previous quarter
                streak_quarters INTEGER DEFAULT 0,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(theme_id, quarter_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tqs_theme ON theme_quarterly_scores(theme_id);
            CREATE INDEX IF NOT EXISTS idx_tqs_quarter ON theme_quarterly_scores(quarter_id);
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Node upserts
    # ------------------------------------------------------------------

    def upsert_company(self, name: str, sector: str = "", country: str = "India") -> int:
        cur = self.conn.execute(
            """INSERT INTO companies (name, sector, country)
               VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET sector=excluded.sector
               RETURNING id""",
            (name.strip(), sector, country),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row[0]

    def upsert_theme(self, name: str, description: str = "") -> int:
        cur = self.conn.execute(
            """INSERT INTO themes (name, description)
               VALUES (?, ?)
               ON CONFLICT(name) DO NOTHING
               RETURNING id""",
            (name.strip(), description),
        )
        row = cur.fetchone()
        if row:
            self.conn.commit()
            return row[0]
        return self.conn.execute("SELECT id FROM themes WHERE name=?", (name,)).fetchone()[0]

    def upsert_quarter(self, label: str) -> int:
        """label format: 'Q1-2024', 'Q2-2024' etc."""
        year, quarter_num = self._parse_quarter(label)
        cur = self.conn.execute(
            """INSERT INTO quarters (label, year, quarter_num)
               VALUES (?, ?, ?)
               ON CONFLICT(label) DO NOTHING
               RETURNING id""",
            (label, year, quarter_num),
        )
        row = cur.fetchone()
        if row:
            self.conn.commit()
            return row[0]
        return self.conn.execute("SELECT id FROM quarters WHERE label=?", (label,)).fetchone()[0]

    # ------------------------------------------------------------------
    # Edge writes
    # ------------------------------------------------------------------

    def record_mention(
        self,
        company: str,
        theme: str,
        quarter: str,
        mention_count: int,
        confidence: float,
        strength_score: float,
        capex_mentioned: bool = False,
        capex_count: int = 0,
        has_negative: bool = False,
        roles: list = None,
        primary_role: str = "",
        snippets: list = None,
        source_doc_id: int = None,
        min_strength: float = 0.0,
    ) -> int:
        """Upsert a Company→Theme mention edge for a quarter.

        Args:
            min_strength: minimum strength_score required to store the edge.
                          Weak edges (strength_score < min_strength) are dropped,
                          preventing entity explosion from low-confidence mentions.
                          Default 0.0 keeps backward compatibility.
        """
        if strength_score < min_strength:
            logger.debug(
                f"Dropping weak mention edge {company}→{theme} "
                f"(strength={strength_score:.3f} < min={min_strength:.3f})"
            )
            return -1
        company_id = self.upsert_company(company)
        theme_id = self.upsert_theme(theme)
        quarter_id = self.upsert_quarter(quarter)

        cur = self.conn.execute(
            """INSERT INTO mentions
               (company_id, theme_id, quarter_id,
                mention_count, confidence, strength_score,
                capex_mentioned, capex_count, has_negative,
                roles, primary_role, snippets, source_doc_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id, theme_id, quarter_id) DO UPDATE SET
                   mention_count  = mention_count + excluded.mention_count,
                   confidence     = MAX(confidence, excluded.confidence),
                   strength_score = MAX(strength_score, excluded.strength_score),
                   capex_mentioned= MAX(capex_mentioned, excluded.capex_mentioned),
                   capex_count    = capex_count + excluded.capex_count,
                   has_negative   = MAX(has_negative, excluded.has_negative),
                   roles          = excluded.roles,
                   primary_role   = excluded.primary_role,
                   snippets       = excluded.snippets,
                   source_doc_id  = excluded.source_doc_id
               RETURNING id""",
            (
                company_id, theme_id, quarter_id,
                mention_count, confidence, strength_score,
                int(capex_mentioned), capex_count, int(has_negative),
                json.dumps(roles or []), primary_role,
                json.dumps((snippets or [])[:5]),  # cap stored snippets
                source_doc_id,
            ),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row[0]

    def record_contradiction(
        self,
        company: str,
        theme: str,
        from_quarter: str,
        to_quarter: str,
        change_type: str,
        from_sentiment: float,
        to_sentiment: float,
        evidence: dict = None,
    ):
        company_id = self.upsert_company(company)
        theme_id = self.upsert_theme(theme)
        from_qid = self.upsert_quarter(from_quarter)
        to_qid = self.upsert_quarter(to_quarter)

        self.conn.execute(
            """INSERT INTO contradictions
               (company_id, theme_id, from_quarter_id, to_quarter_id,
                change_type, from_sentiment, to_sentiment, evidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id, theme_id, from_qid, to_qid,
                change_type, from_sentiment, to_sentiment,
                json.dumps(evidence or {}),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def aggregate_theme_strength(self, quarter: str):
        """
        Compute/refresh theme_quarterly_scores for a given quarter.
        Also computes growth_vs_prev and streak_quarters.
        """
        import math as _math
        quarter_id = self.upsert_quarter(quarter)

        # Aggregate current quarter
        rows = self.conn.execute(
            """SELECT theme_id,
                      COUNT(DISTINCT company_id)   AS company_count,
                      SUM(mention_count)            AS total_mentions,
                      AVG(confidence)               AS avg_confidence,
                      AVG(strength_score)           AS avg_strength,
                      SUM(capex_mentioned)          AS capex_company_count
               FROM mentions
               WHERE quarter_id = ?
               GROUP BY theme_id""",
            (quarter_id,),
        ).fetchall()

        for row in rows:
            theme_id = row["theme_id"]

            # Growth vs previous quarter
            prev_row = self._get_prev_quarter_score(theme_id, quarter)
            prev_mentions = prev_row["total_mentions"] if prev_row else None
            if prev_mentions and prev_mentions > 0:
                growth = (row["total_mentions"] - prev_mentions) / prev_mentions
            else:
                growth = 0.0

            # Streak: how many consecutive quarters this theme appeared
            streak = self._compute_streak(theme_id, quarter)

            # ── Continuous log-scaled component scores ─────────────────────
            # log1p normalization preserves raw variance:
            # e.g. 5 mentions ≠ 50 mentions — both were clipping to 1.0 before.
            # log1p(5)/log1p(100)=0.34  vs  log1p(50)/log1p(100)=0.73
            # Hard-bucketed versions set both to the same value (1.0), causing
            # score cloning across themes with very different evidence levels.
            mention_score = min(
                _math.log1p(row["total_mentions"]) / _math.log1p(100.0), 1.0
            )
            # Growth: tanh maps [-inf,+inf] → (-1,1), then shift to [0,1].
            # tanh(1.5 * growth): growth=0→0.5, growth=+1→0.82, growth=-1→0.18
            growth_score = 0.5 + 0.5 * _math.tanh(1.5 * growth)

            breadth_score = min(
                _math.log1p(row["company_count"]) / _math.log1p(20.0), 1.0
            )
            capex_score = min(
                _math.log1p(row["capex_company_count"]) / _math.log1p(10.0), 1.0
            )
            # Streak: sqrt gives diminishing returns (1Q→0.25, 4Q→0.50, 16Q→1.0)
            streak_score = min(1.0, _math.sqrt(streak) / 4.0)

            composite = (
                0.25 * mention_score
                + 0.20 * growth_score
                + 0.15 * breadth_score
                + 0.20 * (row["avg_confidence"] or 0)
                + 0.10 * capex_score
                + 0.10 * streak_score
            )

            # ── Evidence decay: survivorship bias prevention ────────────────
            # When fresh evidence weakens sharply, decay the composite so
            # themes do not persist at inflated scores when the signal fades.
            # growth < -0.30 → evidence dropped >30% vs prior quarter.
            if growth < -0.30:
                # decay = max(0.60, 1 + growth*0.5)
                # e.g. growth=-0.5→decay=0.75, growth=-1.0→decay=0.60
                decay_factor = max(0.60, 1.0 + growth * 0.5)
                composite *= decay_factor

            self.conn.execute(
                """INSERT INTO theme_quarterly_scores
                   (theme_id, quarter_id, company_count, total_mentions,
                    avg_confidence, avg_strength, capex_company_count,
                    composite_score, growth_vs_prev, streak_quarters, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(theme_id, quarter_id) DO UPDATE SET
                       company_count       = excluded.company_count,
                       total_mentions      = excluded.total_mentions,
                       avg_confidence      = excluded.avg_confidence,
                       avg_strength        = excluded.avg_strength,
                       capex_company_count = excluded.capex_company_count,
                       composite_score     = excluded.composite_score,
                       growth_vs_prev      = excluded.growth_vs_prev,
                       streak_quarters     = excluded.streak_quarters,
                       updated_at          = excluded.updated_at""",
                (
                    theme_id, quarter_id,
                    row["company_count"], row["total_mentions"],
                    row["avg_confidence"] or 0, row["avg_strength"] or 0,
                    row["capex_company_count"] or 0,
                    round(composite, 3), round(growth, 3), streak,
                    datetime.utcnow().isoformat(),
                ),
            )

        self.conn.commit()
        logger.info(f"Aggregated theme strength for {quarter}: {len(rows)} themes updated.")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_theme_evolution(self, theme: str, last_n_quarters: int = 6) -> list[dict]:
        """
        Return per-quarter scores for a theme over time.
        Sorted oldest → newest.
        """
        rows = self.conn.execute(
            """SELECT q.label, tqs.company_count, tqs.total_mentions,
                      tqs.avg_confidence, tqs.composite_score,
                      tqs.growth_vs_prev, tqs.streak_quarters,
                      tqs.capex_company_count
               FROM theme_quarterly_scores tqs
               JOIN themes t    ON t.id = tqs.theme_id
               JOIN quarters q  ON q.id = tqs.quarter_id
               WHERE t.name = ?
               ORDER BY q.year DESC, q.quarter_num DESC
               LIMIT ?""",
            (theme, last_n_quarters),
        ).fetchall()
        result = [dict(r) for r in reversed(rows)]
        return result

    def get_top_themes(self, quarter: str, top_n: int = 10) -> list[dict]:
        """Top themes by composite score for a given quarter."""
        rows = self.conn.execute(
            """SELECT t.name AS theme, tqs.composite_score, tqs.company_count,
                      tqs.total_mentions, tqs.growth_vs_prev, tqs.streak_quarters
               FROM theme_quarterly_scores tqs
               JOIN themes t   ON t.id = tqs.theme_id
               JOIN quarters q ON q.id = tqs.quarter_id
               WHERE q.label = ?
               ORDER BY tqs.composite_score DESC
               LIMIT ?""",
            (quarter, top_n),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_companies_for_theme(self, theme: str, quarter: str) -> list[dict]:
        """All companies mentioning a theme in a quarter, with their roles."""
        rows = self.conn.execute(
            """SELECT c.name AS company, m.mention_count, m.confidence,
                      m.strength_score, m.roles, m.primary_role,
                      m.capex_mentioned, m.has_negative
               FROM mentions m
               JOIN companies c ON c.id = m.company_id
               JOIN themes t    ON t.id = m.theme_id
               JOIN quarters q  ON q.id = m.quarter_id
               WHERE t.name = ? AND q.label = ?
               ORDER BY m.strength_score DESC""",
            (theme, quarter),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["roles"] = json.loads(d["roles"])
            results.append(d)
        return results

    def get_contradictions(
        self,
        company: str = None,
        theme: str = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query contradiction edges, optionally filtered by company or theme."""
        where_clauses = []
        params = []
        if company:
            where_clauses.append("c.name = ?")
            params.append(company)
        if theme:
            where_clauses.append("t.name = ?")
            params.append(theme)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        rows = self.conn.execute(
            f"""SELECT c.name AS company, t.name AS theme,
                       fq.label AS from_quarter, tq.label AS to_quarter,
                       ct.change_type, ct.from_sentiment, ct.to_sentiment,
                       ct.evidence
                FROM contradictions ct
                JOIN companies c ON c.id = ct.company_id
                JOIN themes t    ON t.id = ct.theme_id
                JOIN quarters fq ON fq.id = ct.from_quarter_id
                JOIN quarters tq ON tq.id = ct.to_quarter_id
                {where}
                ORDER BY ct.created_at DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"])
            results.append(d)
        return results

    def get_graph_snapshot(self, quarter: str) -> dict:
        """
        Full graph snapshot for a quarter — for export to Neo4j or LLM validation.
        Returns nodes (companies, themes) and edges (mentions) as dicts.
        """
        companies = self.get_companies_for_quarter(quarter)
        themes = self.get_top_themes(quarter, top_n=20)
        contradictions = self.get_contradictions(limit=20)

        return {
            "quarter": quarter,
            "themes": themes,
            "companies": companies,
            "contradictions": contradictions,
            "generated_at": datetime.utcnow().isoformat(),
        }

    def get_companies_for_quarter(self, quarter: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT DISTINCT c.name AS company, c.sector,
                      m.roles, m.primary_role,
                      t.name AS theme, m.strength_score
               FROM mentions m
               JOIN companies c ON c.id = m.company_id
               JOIN themes t    ON t.id = m.theme_id
               JOIN quarters q  ON q.id = m.quarter_id
               WHERE q.label = ?
               ORDER BY m.strength_score DESC""",
            (quarter,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["roles"] = json.loads(d["roles"])
            results.append(d)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_quarter(label: str) -> tuple[int, int]:
        """Parse 'Q2-2024' → (2024, 2)."""
        try:
            parts = label.upper().replace("Q", "").split("-")
            return int(parts[1]), int(parts[0])
        except Exception:
            return datetime.utcnow().year, 1

    def _get_prev_quarter_score(self, theme_id: int, current_quarter: str) -> Optional[sqlite3.Row]:
        """Get the theme_quarterly_scores row for the quarter before current_quarter."""
        year, qnum = self._parse_quarter(current_quarter)
        if qnum == 1:
            prev_label = f"Q4-{year - 1}"
        else:
            prev_label = f"Q{qnum - 1}-{year}"

        row = self.conn.execute(
            """SELECT tqs.total_mentions FROM theme_quarterly_scores tqs
               JOIN quarters q ON q.id = tqs.quarter_id
               WHERE tqs.theme_id = ? AND q.label = ?""",
            (theme_id, prev_label),
        ).fetchone()
        return row

    def _compute_streak(self, theme_id: int, current_quarter: str) -> int:
        """Count consecutive quarters (ending at current) that the theme appeared."""
        year, qnum = self._parse_quarter(current_quarter)
        streak = 0
        for _ in range(12):  # max 12 quarters lookback
            label = f"Q{qnum}-{year}"
            exists = self.conn.execute(
                """SELECT 1 FROM mentions m
                   JOIN quarters q ON q.id = m.quarter_id
                   WHERE m.theme_id = ? AND q.label = ?
                   LIMIT 1""",
                (theme_id, label),
            ).fetchone()
            if not exists:
                break
            streak += 1
            qnum -= 1
            if qnum == 0:
                qnum = 4
                year -= 1
        return streak

    def close(self):
        if self.conn:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
