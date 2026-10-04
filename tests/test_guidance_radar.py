"""Tests for the independent management-guidance radar."""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from makrograph.guidance_radar.extractor import HybridDisclosureExtractor
from makrograph.guidance_radar.models import (
    Action,
    CompanyPacket,
    EvidenceEvent,
    FinancialSnapshot,
    GuidanceClaim,
    MarketSnapshot,
    RiskFlag,
    SourceDocument,
)
from makrograph.guidance_radar.scoring import GuidanceScorer


def _doc(doc_id: int, day: date, text: str, filing="Transcript"):
    return SourceDocument(
        document_id=doc_id,
        ticker="CASE",
        company="Case Industries",
        filed_at=day,
        source_url=f"https://nsearchives.nseindia.com/corporate/{doc_id}.pdf",
        filing_type=filing,
        title="official disclosure",
        text=text,
        source_name="nse_india",
    )


def _strong_packet(ticker="GVT&D", as_of=date(2023, 8, 8)):
    d1 = _doc(1, date(2023, 2, 13), "Revenue target is INR 4500 crore versus a baseline of INR 2773 crore.")
    d2 = _doc(2, date(2023, 5, 25), "The order book is INR 3700 crore and debt reduced materially.")
    d3 = _doc(3, as_of, "Revenue and profit growth were realized and margins expanded.")
    return CompanyPacket(
        ticker=ticker,
        company=ticker,
        as_of_date=as_of,
        documents=[d1, d2, d3],
        claims=[
            GuidanceClaim("revenue", "FY2025", 1, d1.filed_at, d1.text,
                          4500, 4500, 2773, "INR_CR", "guidance", .9),
            GuidanceClaim("revenue", "FY2025", 2, d2.filed_at, d2.text,
                          4500, 4500, 2773, "INR_CR", "guidance", .9),
        ],
        evidence=[
            EvidenceEvent("orderbook", 1, d1.filed_at, d1.text, "orderbook", 3482,
                          unit="INR_CR", status="ordered", confidence=.9),
            EvidenceEvent("order_win", 2, d2.filed_at, d2.text, "orders", 1010,
                          unit="INR_CR", status="ordered", confidence=.9),
            EvidenceEvent("revenue_delivery", 3, d3.filed_at, d3.text, "revenue", 717,
                          593, "INR_CR", "realized", .9),
            EvidenceEvent("profit_delivery", 3, d3.filed_at, d3.text, "pat", 28.2,
                          6.4, "INR_CR", "realized", .9),
            EvidenceEvent("debt_reduction", 3, d3.filed_at, d3.text, "debt", 117,
                          190, "INR_CR", "realized", .9),
        ],
        financials=[FinancialSnapshot(
            available_at=as_of, period_end=date(2023, 6, 30), revenue=717,
            revenue_prior=593, pat=28.2, pat_prior=6.4, ebitda_margin_pct=8.7,
            ebitda_margin_prior_pct=5.2, cfo=24, debt=117, debt_prior=190,
            eps=3.0, period_months=3, source_document_id=3,
        )],
        market=MarketSnapshot(
            price_date=as_of, price=271.85, trailing_eps=16.0,
            return_3m_pct=44, return_6m_pct=131, return_12m_pct=80,
            adjusted_for_corporate_actions=True,
        ),
        narrative_tags_by_date={
            d1.filed_at: {"transmission_orderbook", "legacy_margin_exit"},
            d2.filed_at: {"transmission_orderbook", "legacy_margin_exit"},
            d3.filed_at: {"transmission_orderbook", "legacy_margin_exit"},
        },
    )


def test_point_in_time_rejects_future_document():
    packet = _strong_packet(as_of=date(2023, 8, 8))
    packet.documents.append(_doc(99, date(2024, 1, 1), "Future results."))
    with pytest.raises(ValueError, match="look-ahead"):
        GuidanceScorer().score(packet)


def test_multiple_dated_proof_can_reach_action_but_chase_is_blocked():
    packet = _strong_packet()
    decision = GuidanceScorer().score(packet)
    assert decision.score >= 68
    assert decision.action == Action.HOLD_NO_CHASE
    assert decision.max_position_pct == 0

    # At the same fundamental evidence state, a not-yet-run price can earn
    # position authority. The production system never changes the evidence.
    packet.market = MarketSnapshot(
        price_date=packet.as_of_date, price=180, trailing_eps=16,
        return_3m_pct=20, return_6m_pct=40, return_12m_pct=55,
        adjusted_for_corporate_actions=True,
    )
    earlier_price_decision = GuidanceScorer().score(packet)
    assert earlier_price_decision.action in {Action.STARTER, Action.ACCUMULATE}
    assert earlier_price_decision.max_position_pct > 0


def test_repetitive_promotional_evidence_is_one_leg_and_stays_watch():
    day = date(2024, 5, 10)
    doc = _doc(10, day, "We target revenue of INR 500 crore. We received an order of INR 20 crore.")
    extraction = HybridDisclosureExtractor().extract([doc, _doc(11, day, doc.text)], day)
    packet = CompanyPacket(
        ticker="PROMO", company="Promotional Limited", as_of_date=day,
        documents=[doc], claims=extraction.claims, evidence=extraction.evidence,
        market=MarketSnapshot(day, 100, adjusted_for_corporate_actions=True),
    )
    decision = GuidanceScorer().score(packet)
    assert decision.evidence_dates <= 1
    assert decision.action == Action.WATCH
    assert "a second independent dated execution event" in decision.missing_proof


def test_governance_hard_veto_overrides_strong_growth_story():
    packet = _strong_packet(ticker="CONTROL")
    packet.risks.append(RiskFlag(
        "auditor_resignation", 3, packet.as_of_date,
        "The statutory auditor resigned before completion of the audit.", "hard", .95,
    ))
    decision = GuidanceScorer().score(packet)
    assert decision.action == Action.THESIS_BROKEN
    assert decision.max_position_pct == 0


class _FakeLLM:
    model_name = "fake-structured-model"

    def extract(self, prompt):
        return {
            "claims": [
                {"document_id": 1, "metric": "revenue", "target_period": "FY2026",
                 "target_low": 200, "target_high": 220, "baseline": 100,
                 "unit": "INR_CR", "certainty": "guidance", "confidence": .9,
                 "quote": "We expect revenue of INR 200 to 220 crore in FY2026."},
                {"document_id": 1, "metric": "pat", "target_period": "FY2026",
                 "target_low": 30, "target_high": 30, "baseline": 10,
                 "unit": "INR_CR", "certainty": "guidance", "confidence": .99,
                 "quote": "This sentence was hallucinated by the model."},
            ],
            "evidence": [], "risks": [], "financials": [], "narrative_tags": [],
        }


def test_llm_claim_requires_verbatim_source_support():
    doc = _doc(1, date(2025, 1, 1), "Management said: We expect revenue of INR 200 to 220 crore in FY2026.")
    result = HybridDisclosureExtractor(_FakeLLM()).extract([doc], doc.filed_at)
    assert any(c.metric == "revenue" and c.confidence == .9 for c in result.claims)
    assert not any(c.metric == "pat" and c.confidence == .99 for c in result.claims)
