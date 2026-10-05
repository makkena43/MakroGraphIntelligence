"""Read-only source adapters with schema / coverage preflight.

* ``FixtureRepository`` - offline JSON fixtures (default for development/tests).
* ``PostgresReadOnlyRepository`` - production reads through an EXPLICITLY
  configured read-only connection.  It never uses ``PGStore`` (whose
  constructor runs migrations by default), opens every session with
  ``default_transaction_read_only = on``, refuses any non-SELECT statement,
  introspects the live ``mg_documents`` columns instead of trusting the base
  DDL (``raw_text`` is read by repository code but absent from
  ``schema/postgres_schema.sql``), and pages per ticker by primary key: there
  is no global LIMIT ordered by ticker that could silently drop later-alphabet
  companies.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional, Protocol

from .contracts import IST, SourceDocument
from .text_artifacts import ExtractionStatus, TextArtifactStore


class ReadOnlyViolation(RuntimeError):
    pass


class SourceRepository(Protocol):
    def preflight(self) -> dict: ...
    def tickers(self, country: str) -> list[str]: ...
    def documents(self, ticker: str, country: str, as_of: datetime) -> list[SourceDocument]: ...
    def issuer_metadata(self, ticker: str) -> dict: ...


def _parse_dt(v) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    dt = datetime.fromisoformat(str(v))
    return dt


def _parse_d(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _visible(doc: SourceDocument, as_of: datetime) -> bool:
    """Coarse pre-filter; precise availability is computed in document_versions."""
    if doc.published_at is not None:
        ts = doc.published_at if doc.published_at.tzinfo else doc.published_at.replace(tzinfo=IST)
        return ts <= as_of
    if doc.filed_at is not None:
        return doc.filed_at <= as_of.date()
    return True   # unknown time: kept so coverage can report it, excluded later


# ---------------------------------------------------------------------------
# Text attachment (WP1): artifact store first, then legacy text
# ---------------------------------------------------------------------------

class TextPolicy:
    """Which extracted text a run may read.

    ``pinned`` maps doc_id -> artifact version (replaying an earlier run's
    manifest); ``extracted_by`` restricts artifacts to those created no later
    than a cutoff (system-knowledge replay)."""

    def __init__(self, pinned: Optional[dict] = None, extracted_by: Optional[datetime] = None):
        self.pinned = dict(pinned or {})
        self.extracted_by = extracted_by


def attach_text(doc: SourceDocument, store: Optional[TextArtifactStore], legacy_text: Optional[str],
                legacy_source: str, policy: Optional[TextPolicy] = None) -> SourceDocument:
    """Fill ``doc.text/pages`` and the provenance fields.  Never parses or downloads."""
    policy = policy or TextPolicy()
    pinned = policy.pinned.get(doc.doc_id)
    if store is not None:
        res = store.select(doc.doc_id, pinned_version=pinned, extracted_by=policy.extracted_by)
        if res is not None:
            doc.pages, doc.text = store.read_pages(res), None
            doc.text_source = f"artifact:{res.version_id}"
            doc.extraction_status = res.status.value
            doc.extraction_version_id = res.version_id
            doc.extraction_complete = res.complete
            doc.extraction_issues = list(res.quality_issues)
            doc.raw_hash = res.raw_hash
            doc.text_available_at = res.extracted_at
            return doc
        if pinned:
            doc.extraction_issues.append(f"pinned artifact {pinned} not found in the store")
    if legacy_text:
        doc.text, doc.pages = legacy_text, None
        doc.text_source = legacy_source
        doc.extraction_status = ExtractionStatus.LEGACY_UNVERSIONED.value
        doc.extraction_complete = None
        doc.extraction_issues.append("legacy text without version history or page-coverage record")
        doc.text_available_at = None           # when this text first existed cannot be proven
        return doc
    doc.text, doc.pages = None, None
    doc.text_source = "none"
    last = store.latest_status(doc.doc_id) if store is not None else None
    if last is not None and not last.status.usable:
        doc.extraction_status = last.status.value
        doc.extraction_issues.append(last.failure_reason)
    elif (doc.local_path or "").lower().endswith(".pdf") or (doc.url or "").lower().endswith(".pdf"):
        doc.extraction_status = ExtractionStatus.NOT_EXTRACTED.value
        doc.extraction_issues.append("PDF original never extracted; run the explicit --extract step")
    else:
        doc.extraction_status = ExtractionStatus.EMPTY.value
    return doc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FixtureRepository:
    """Directory of ``*.json`` files: {"issuers": {...}, "documents": [...]}.

    A document may embed ``text`` / ``pages`` or reference ``text_file``
    relative to the fixture directory.
    """

    def __init__(self, path: str | Path, artifact_store: Optional[TextArtifactStore] = None):
        self.path = Path(path)
        self.artifact_store = artifact_store
        self.text_policy = TextPolicy()
        self._docs: list[SourceDocument] = []
        self._issuers: dict[str, dict] = {}
        for f in sorted(self.path.glob("*.json")):
            data = json.loads(f.read_text())
            self._issuers.update(data.get("issuers", {}))
            for d in data.get("documents", []):
                text = d.get("text")
                if text is None and d.get("text_file"):
                    text = (self.path / d["text_file"]).read_text()
                self._docs.append(SourceDocument(
                    doc_id=str(d["doc_id"]), source_name=d.get("source_name", "fixture"), ticker=d["ticker"],
                    country=d.get("country", "IN"), company=d.get("company", ""), title=d.get("title", ""),
                    doc_type=d.get("doc_type", ""), filing_type=d.get("filing_type", ""), url=d.get("url", ""),
                    filed_at=_parse_d(d.get("filed_at")), published_at=_parse_dt(d.get("published_at")),
                    content_hash=d.get("content_hash", ""),
                    local_path=str(self.path / d["local_path"]) if d.get("local_path") else "",
                    text=text, pages=d.get("pages"), first_seen_at=_parse_dt(d.get("first_seen_at")),
                ))

    def preflight(self) -> dict:
        return {"source": "fixtures", "path": str(self.path), "documents": len(self._docs),
                "tickers": len({d.ticker for d in self._docs}), "read_only": True}

    def tickers(self, country: str) -> list[str]:
        return sorted({d.ticker for d in self._docs if d.country == country})

    def documents(self, ticker: str, country: str, as_of: datetime) -> list[SourceDocument]:
        import copy
        out = []
        for d in self._docs:
            if d.ticker == ticker and d.country == country and _visible(d, as_of):
                d = copy.deepcopy(d)
                legacy = "\f".join(d.pages) if d.pages else d.text
                d.pages = None
                out.append(attach_text(d, self.artifact_store, legacy, "fixture_text", self.text_policy))
        return out

    def issuer_metadata(self, ticker: str) -> dict:
        return dict(self._issuers.get(ticker, {}))


# ---------------------------------------------------------------------------
# PostgreSQL (read-only)
# ---------------------------------------------------------------------------

_ALLOWED_SQL = re.compile(r"^\s*(?:select|with)\b", re.I)
_FORBIDDEN_SQL = re.compile(
    r"\b(?:insert|update|delete|merge|alter|create|drop|truncate|grant|revoke|copy|vacuum|analyze|"
    r"refresh|lock|call|do|comment|security|set\s+role|reset)\b", re.I)


def assert_read_only_sql(sql: str) -> None:
    body = re.sub(r"'(?:[^']|'')*'", "''", sql)   # ignore string literals
    if not _ALLOWED_SQL.match(body) or _FORBIDDEN_SQL.search(body) or ";" in body.strip().rstrip(";"):
        raise ReadOnlyViolation(f"refusing non read-only SQL: {sql[:80]!r}")


DOC_COLUMNS_WANTED = ["id", "source_name", "doc_type", "filing_type", "title", "ticker", "company", "cik",
                      "country", "url", "filed_at", "published_at", "local_path", "content_hash", "raw_text",
                      "page_count", "processing_status", "created_at"]
REQUIRED_DOC_COLUMNS = {"id", "ticker", "country", "filed_at"}


class PostgresReadOnlyRepository:
    def __init__(self, dsn: Optional[str] = None, dsn_env: str = "EI_READONLY_DSN", page_size: int = 500,
                 text_root: Optional[str] = None, connect=None,
                 artifact_store: Optional[TextArtifactStore] = None):
        self.artifact_store = artifact_store
        self.text_policy = TextPolicy()
        dsn = dsn or os.environ.get(dsn_env)
        if not dsn:
            raise RuntimeError(f"read-only DSN not configured (set {dsn_env}); production reads are opt-in")
        self._dsn = dsn
        self._page = page_size
        self._text_root = Path(text_root) if text_root else None
        self._connect = connect
        self._conn = None
        self._cols: Optional[set[str]] = None

    # -- connection ----------------------------------------------------------

    def _connection(self):
        if self._conn is None:
            if self._connect is None:
                import psycopg2  # imported lazily: optional dependency
                self._connect = psycopg2.connect
            self._conn = self._connect(self._dsn, options="-c default_transaction_read_only=on")
            try:
                self._conn.set_session(readonly=True, autocommit=False)
            except Exception:
                pass
            with self._conn.cursor() as cur:
                cur.execute("SHOW default_transaction_read_only")
                row = cur.fetchone()
                if not row or str(row[0]).lower() != "on":
                    raise ReadOnlyViolation("connection is not read-only")
        return self._conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        assert_read_only_sql(sql)
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            names = [c[0] for c in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        conn.rollback()   # never leave a transaction open; nothing to commit
        return rows

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- schema ----------------------------------------------------------------

    def document_columns(self) -> set[str]:
        if self._cols is None:
            rows = self._query("SELECT column_name FROM information_schema.columns "
                               "WHERE table_name = %s AND table_schema = current_schema()", ("mg_documents",))
            self._cols = {r["column_name"] for r in rows}
        return self._cols

    def preflight(self, country: str = "IN") -> dict:
        cols = self.document_columns()
        report: dict = {"source": "postgres", "read_only": True,
                        "mg_documents_columns": sorted(cols),
                        "missing_required_columns": sorted(REQUIRED_DOC_COLUMNS - cols),
                        "has_raw_text": "raw_text" in cols, "has_published_at": "published_at" in cols,
                        "base_ddl_discrepancies": sorted(c for c in ("raw_text", "sentiment_score", "nlp_summary")
                                                         if c in cols)}
        if report["missing_required_columns"]:
            report["ok"] = False
            return report
        tables = {r["table_name"] for r in self._query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
        report["optional_tables"] = {t: t in tables for t in
                                     ("fundamentals_snapshot", "nse_bhavcopy_data", "bse_bhavcopy_data",
                                      "mg_benchmark_prices", "mg_signals", "mg_events")}
        pub = "published_at" if "published_at" in cols else "NULL::timestamptz"
        txt = "(raw_text IS NOT NULL AND raw_text <> '')" if "raw_text" in cols else "FALSE"
        report["coverage"] = self._query(
            f"SELECT source_name, doc_type, COUNT(*) AS n, COUNT(DISTINCT ticker) AS tickers, "
            f"SUM(CASE WHEN {pub} IS NULL AND filed_at IS NULL THEN 1 ELSE 0 END) AS no_time, "
            f"SUM(CASE WHEN {txt} THEN 1 ELSE 0 END) AS with_text, MIN(filed_at) AS first, MAX(filed_at) AS last "
            f"FROM mg_documents WHERE country = %s GROUP BY source_name, doc_type ORDER BY source_name, doc_type",
            (country,))
        report["ok"] = True
        return report

    # -- reads -----------------------------------------------------------------

    def tickers(self, country: str) -> list[str]:
        rows = self._query("SELECT DISTINCT ticker FROM mg_documents WHERE country = %s AND ticker IS NOT NULL "
                           "AND ticker <> '' ORDER BY ticker", (country,))
        return [r["ticker"] for r in rows]

    def _iter_rows(self, tickers: list[str], country: str, as_of: datetime) -> Iterator[dict]:
        """All rows for the given ticker aliases, paged by primary key (no cross-company LIMIT)."""
        cols = self.document_columns()
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in DOC_COLUMNS_WANTED)
        pub = "published_at" if "published_at" in cols else "NULL::timestamptz"
        last_id = 0
        while True:
            rows = self._query(
                f"SELECT {sel} FROM mg_documents WHERE ticker = ANY(%s) AND country = %s "
                f"AND (COALESCE({pub}, (filed_at + INTERVAL '1 day' - INTERVAL '1 second')::timestamp AT TIME ZONE 'Asia/Kolkata') <= %s "
                f"     OR ({pub} IS NULL AND filed_at IS NULL)) "
                f"AND id > %s ORDER BY id LIMIT %s", (list(tickers), country, as_of, last_id, self._page))
            if not rows:
                return
            yield from rows
            last_id = rows[-1]["id"]

    def _text_for(self, row: dict) -> tuple[Optional[str], str]:
        """Legacy text: (text, source label).  PDFs are never parsed here."""
        if row.get("raw_text"):
            return row["raw_text"], "legacy_raw_text"
        lp = row.get("local_path")
        if lp and self._text_root:
            p = (self._text_root / lp).resolve() if not os.path.isabs(lp) else Path(lp)
            for cand in (p.with_suffix(".txt"), p):
                if cand.suffix == ".txt" and cand.exists():
                    return cand.read_text(errors="replace"), "legacy_txt"
        return None, "none"

    def documents(self, ticker: str, country: str, as_of: datetime) -> list[SourceDocument]:
        out = []
        for r in self._iter_rows([ticker], country, as_of):
            pub = r.get("published_at")
            if isinstance(pub, datetime) and pub.tzinfo is None:
                pub = pub.replace(tzinfo=IST)
            seen = r.get("created_at")
            if isinstance(seen, datetime) and seen.tzinfo is None:
                seen = seen.replace(tzinfo=IST)
            doc = SourceDocument(
                doc_id=str(r["id"]), source_name=r.get("source_name") or "", ticker=r["ticker"],
                country=r.get("country") or country, company=r.get("company") or "", title=r.get("title") or "",
                doc_type=r.get("doc_type") or "", filing_type=r.get("filing_type") or "", url=r.get("url") or "",
                filed_at=r.get("filed_at"), published_at=pub, content_hash=r.get("content_hash") or "",
                local_path=r.get("local_path") or "", first_seen_at=seen)
            legacy, label = self._text_for(r)
            out.append(attach_text(doc, self.artifact_store, legacy, label, self.text_policy))
        return out

    def issuer_metadata(self, ticker: str) -> dict:
        # fundamentals_snapshot is a present-day upsert: only the static industry
        # label is taken, and only as a classification hint.
        try:
            rows = self._query("SELECT company_name, sector, industry FROM fundamentals_snapshot "
                               "WHERE nse_symbol = %s OR bse_symbol = %s LIMIT 1", (ticker, ticker))
        except Exception:
            self._conn and self._conn.rollback()
            return {}
        if not rows:
            return {}
        r = rows[0]
        return {"name": r.get("company_name") or "", "industry": r.get("industry") or r.get("sector") or "",
                "industry_source": "fundamentals_snapshot(present-day label)"}
