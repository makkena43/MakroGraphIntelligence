"""PostgreSQL adapter for the guidance radar and NSE price history."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .extractor import ExtractionResult, extraction_fingerprint
from .models import CompanyDecision, MarketSnapshot, SourceDocument


class GuidanceRadarRepository:
    def __init__(self, pg_store):
        self.pg_store = pg_store

    def ensure_schema(self, schema_path: Optional[Path] = None) -> None:
        if schema_path is None:
            schema_path = Path(__file__).resolve().parents[3] / "schema" / "guidance_radar_schema.sql"
        sql = schema_path.read_text(encoding="utf-8")
        with self.pg_store._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def load_candidate_documents(
        self,
        as_of_date: date,
        lookback_days: int = 550,
        tickers: Optional[list[str]] = None,
        max_candidates: int = 80,
        max_docs_per_ticker: int = 14,
    ) -> dict[str, list[SourceDocument]]:
        """Triage broadly with SQL, then cap expensive LLM work by issuer."""
        from psycopg2.extras import RealDictCursor

        start = as_of_date - timedelta(days=lookback_days)
        ticker_clause = "AND UPPER(d.ticker) = ANY(%s)" if tickers else ""
        params: list = [start, as_of_date]
        if tickers:
            params.append([ticker.upper() for ticker in tickers])
        params.extend([max_docs_per_ticker, max(max_candidates * max_docs_per_ticker * 2, 500)])
        sql = f"""
            WITH eligible AS (
                SELECT d.*,
                       CASE
                         WHEN d.filing_type ILIKE '%%auditor%%' OR d.filing_type ILIKE '%%resignation%%'
                           OR d.filing_type ILIKE '%%default%%' OR d.filing_type ILIKE '%%insolvency%%'
                           OR d.filing_type ILIKE '%%fraud%%' THEN 9
                         WHEN d.filing_type ILIKE '%%Transcript%%' THEN 8
                         WHEN d.filing_type ILIKE '%%Financial Result%%' OR d.filing_type ILIKE '%%Outcome of Board%%' THEN 6
                         WHEN d.filing_type ILIKE '%%order%%' OR d.filing_type ILIKE '%%contract%%' THEN 6
                         WHEN d.filing_type ILIKE '%%Capacity%%' THEN 6
                         WHEN d.filing_type ILIKE '%%Investor Presentation%%' THEN 5
                         WHEN d.filing_type ILIKE '%%Press Release%%' THEN 3
                         WHEN d.filing_type ILIKE '%%Acquisition%%' THEN 2
                         ELSE 1
                       END AS doc_priority
                FROM mg_documents d
                WHERE d.country='IN'
                  AND d.ticker IS NOT NULL
                  AND d.filed_at BETWEEN %s AND %s
                  AND LENGTH(COALESCE(d.raw_text, '')) >= 300
                  {ticker_clause}
                  AND (
                    d.filing_type ILIKE ANY(ARRAY[
                      '%%Transcript%%', '%%Financial Result%%', '%%Outcome of Board%%',
                      '%%order%%', '%%contract%%', '%%Capacity%%', '%%Investor Presentation%%',
                      '%%Press Release%%', '%%Acquisition%%', '%%Credit Rating%%'
                      , '%%auditor%%', '%%resignation%%', '%%default%%', '%%insolvency%%',
                      '%%fraud%%', '%%preferential%%', '%%warrant%%', '%%clarification%%'
                    ])
                    OR COALESCE(d.raw_text, '') ~* '(guidance|order[ -]?book|capacity|commission|debt reduction|auditor.{0,30}resign|payment default|preferential allotment)'
                  )
            ), ranked AS (
                SELECT eligible.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker ORDER BY doc_priority DESC, filed_at DESC, id DESC
                       ) AS rn
                FROM eligible
            )
            SELECT * FROM ranked WHERE rn <= %s
            ORDER BY ticker, filed_at, id
            LIMIT %s
        """
        grouped: dict[str, list[SourceDocument]] = defaultdict(list)
        with self.pg_store._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                for row in cur.fetchall():
                    ticker = (row["ticker"] or "").strip().upper()
                    grouped[ticker].append(SourceDocument(
                        document_id=row["id"],
                        ticker=ticker,
                        company=row.get("company") or ticker,
                        filed_at=row["filed_at"],
                        source_url=row.get("url") or "",
                        filing_type=row.get("filing_type") or row.get("doc_type") or "",
                        title=row.get("title") or "",
                        text=row.get("raw_text") or "",
                        source_name=row.get("source_name") or "nse",
                    ))

        # Require at least two independent event categories. This saves LLM
        # calls while retaining newly-listed issuers with one call + orders.
        scored = sorted(
            grouped.items(),
            key=lambda item: (-_triage_score(item[1]), item[0]),
        )
        return {
            ticker: docs for ticker, docs in scored[:max_candidates]
            if _triage_score(docs) >= 8 or bool(tickers)
        }

    def market_snapshot(self, ticker: str, as_of_date: date) -> Optional[MarketSnapshot]:
        from psycopg2.extras import RealDictCursor

        with self.pg_store._conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                prices = {}
                for label, offset in (("now", 0), ("m3", 91), ("m6", 182), ("m12", 365)):
                    target = as_of_date - timedelta(days=offset)
                    cur.execute("""
                        SELECT trade_date, close
                        FROM nse_bhavcopy_data
                        WHERE symbol=%s AND series IN ('EQ','BE') AND trade_date <= %s
                        ORDER BY trade_date DESC LIMIT 1
                    """, (ticker, target))
                    prices[label] = cur.fetchone()
                cur.execute("""
                    WITH p AS (
                        SELECT trade_date, close,
                               LAG(close) OVER (ORDER BY trade_date) AS previous
                        FROM nse_bhavcopy_data
                        WHERE symbol=%s AND series IN ('EQ','BE')
                          AND trade_date BETWEEN %s AND %s
                    )
                    SELECT MAX(ABS(close / NULLIF(previous, 0) - 1)) AS max_gap FROM p
                """, (ticker, as_of_date - timedelta(days=400), as_of_date))
                gap_row = cur.fetchone() or {}

        current = prices.get("now")
        if not current or current.get("close") is None:
            return None
        p0 = float(current["close"])

        def _ret(key):
            row = prices.get(key)
            if not row or row.get("close") in (None, 0):
                return None
            return (p0 / float(row["close"]) - 1.0) * 100.0

        max_gap = float(gap_row.get("max_gap") or 0.0)
        return MarketSnapshot(
            price_date=current["trade_date"],
            price=p0,
            return_3m_pct=_ret("m3"),
            return_6m_pct=_ret("m6"),
            return_12m_pct=_ret("m12"),
            adjusted_for_corporate_actions=max_gap < 0.45,
        )

    def start_run(self, as_of_date: date, model_name: str, metadata: dict) -> int:
        from psycopg2.extras import Json
        with self.pg_store._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO gr_runs (as_of_date, model_name, metadata)
                    VALUES (%s, %s, %s) RETURNING id
                """, (as_of_date, model_name, Json(metadata)))
                return cur.fetchone()[0]

    def save_company(self, run_id: int, documents: list[SourceDocument],
                     extraction: ExtractionResult, decision: CompanyDecision) -> None:
        from psycopg2.extras import Json
        ticker = decision.ticker
        with self.pg_store._conn() as conn:
            with conn.cursor() as cur:
                for claim in extraction.claims:
                    cur.execute("""
                        INSERT INTO gr_promises
                          (run_id,ticker,source_document_id,source_date,metric,target_period,
                           target_low,target_high,baseline,unit,certainty,confidence,quote)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """, (run_id,ticker,claim.source_document_id,claim.source_date,claim.metric,
                          claim.target_period,claim.target_low,claim.target_high,claim.baseline,
                          claim.unit,claim.certainty,claim.confidence,claim.quote))
                for event in extraction.evidence:
                    cur.execute("""
                        INSERT INTO gr_evidence_events
                          (run_id,ticker,source_document_id,source_date,kind,metric,value,
                           prior_value,unit,event_status,confidence,quote)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """, (run_id,ticker,event.source_document_id,event.source_date,event.kind,
                          event.metric,event.value,event.prior_value,event.unit,event.status,
                          event.confidence,event.quote))
                for risk in extraction.risks:
                    cur.execute("""
                        INSERT INTO gr_risk_flags
                          (run_id,ticker,source_document_id,source_date,kind,severity,confidence,quote)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """, (run_id,ticker,risk.source_document_id,risk.source_date,risk.kind,
                          risk.severity,risk.confidence,risk.quote))
                cur.execute("""
                    INSERT INTO gr_decisions
                      (run_id,ticker,company,as_of_date,action,score,max_position_pct,
                       evidence_dates,components,hard_vetoes,why_now,missing_proof,
                       invalidation,model_name)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id,ticker) DO UPDATE SET
                      action=EXCLUDED.action, score=EXCLUDED.score,
                      max_position_pct=EXCLUDED.max_position_pct,
                      components=EXCLUDED.components, hard_vetoes=EXCLUDED.hard_vetoes,
                      why_now=EXCLUDED.why_now, missing_proof=EXCLUDED.missing_proof,
                      invalidation=EXCLUDED.invalidation
                """, (run_id,ticker,decision.company,decision.as_of_date,decision.action.value,
                      decision.score,decision.max_position_pct,decision.evidence_dates,
                      Json(decision.components),Json(list(decision.hard_vetoes)),
                      Json(list(decision.why_now)),Json(list(decision.missing_proof)),
                      Json(list(decision.invalidation)),decision.model_name))

    def finish_run(self, run_id: int, candidate_count: int, decision_count: int,
                   status: str = "complete") -> None:
        with self.pg_store._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE gr_runs SET completed_at=NOW(), candidate_count=%s,
                        decision_count=%s, status=%s WHERE id=%s
                """, (candidate_count, decision_count, status, run_id))


def _triage_score(documents: list[SourceDocument]) -> int:
    categories: set[str] = set()
    dates: set[date] = set()
    for doc in documents:
        filing = doc.filing_type.lower()
        text = (doc.title + " " + doc.text[:6000]).lower()
        dates.add(doc.filed_at)
        if "transcript" in filing or "concall" in filing:
            categories.add("management_call")
        if "financial result" in filing or "outcome of board" in filing:
            categories.add("reported_results")
        if "order" in filing or "contract" in filing or "order book" in text:
            categories.add("orders")
        if "capacity" in filing or "commission" in text:
            categories.add("capacity")
        if "investor presentation" in filing or "guidance" in text or "target" in text:
            categories.add("guidance")
        if "credit rating" in filing or "debt" in text:
            categories.add("balance_sheet")
        if any(value in filing for value in (
            "auditor", "resignation", "default", "insolvency", "fraud",
            "preferential", "warrant", "clarification",
        )):
            categories.add("governance_review")
    score = len(categories) * 3 + min(4, len(dates))
    if "reported_results" not in categories:
        score -= 3
    return score
