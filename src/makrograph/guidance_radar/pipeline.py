"""Orchestration for the independent guidance-to-execution radar."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Optional

from .extractor import HybridDisclosureExtractor
from .models import CompanyDecision, CompanyPacket
from .repository import GuidanceRadarRepository
from .scoring import GuidanceScorer, annualized_eps


class GuidanceRadar:
    def __init__(self, repository: GuidanceRadarRepository,
                 extractor: HybridDisclosureExtractor,
                 scorer: GuidanceScorer | None = None):
        self.repository = repository
        self.extractor = extractor
        self.scorer = scorer or GuidanceScorer()

    def run(
        self,
        as_of_date: date,
        tickers: Optional[list[str]] = None,
        lookback_days: int = 550,
        max_candidates: int = 80,
        max_docs_per_ticker: int = 14,
        persist: bool = True,
    ) -> list[CompanyDecision]:
        docs_by_ticker = self.repository.load_candidate_documents(
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            tickers=tickers,
            max_candidates=max_candidates,
            max_docs_per_ticker=max_docs_per_ticker,
        )
        run_id = None
        if persist:
            self.repository.ensure_schema()
            run_id = self.repository.start_run(
                as_of_date,
                getattr(self.extractor.llm_client, "model_name", "heuristic-v1"),
                {"lookback_days": lookback_days, "tickers": tickers or [],
                 "max_candidates": max_candidates},
            )

        decisions: list[CompanyDecision] = []
        try:
            for ticker, documents in docs_by_ticker.items():
                extraction = self.extractor.extract(documents, as_of_date)
                market = self.repository.market_snapshot(ticker, as_of_date)
                eps = annualized_eps(extraction.financials)
                if market and eps:
                    market = replace(market, trailing_eps=eps)
                packet = CompanyPacket(
                    ticker=ticker,
                    company=documents[-1].company if documents else ticker,
                    as_of_date=as_of_date,
                    documents=documents,
                    claims=extraction.claims,
                    evidence=extraction.evidence,
                    risks=extraction.risks,
                    financials=extraction.financials,
                    market=market,
                    narrative_tags_by_date=extraction.narrative_tags_by_date,
                )
                decision = self.scorer.score(packet)
                decisions.append(decision)
                if run_id is not None:
                    self.repository.save_company(run_id, documents, extraction, decision)
            if run_id is not None:
                self.repository.finish_run(run_id, len(docs_by_ticker), len(decisions))
        except Exception:
            if run_id is not None:
                self.repository.finish_run(run_id, len(docs_by_ticker), len(decisions), "failed")
            raise

        priority = {
            "ACCUMULATE": 0, "STARTER": 1, "HOLD_NO_CHASE": 2,
            "WATCH": 3, "REJECT": 4, "THESIS_BROKEN": 5,
        }
        return sorted(decisions, key=lambda d: (priority[d.action.value], -d.score, d.ticker))
