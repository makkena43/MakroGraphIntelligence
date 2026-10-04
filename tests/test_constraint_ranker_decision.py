#!/usr/bin/env python3
"""Regression tests for strict company-evidence decision hand-off."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "stock_report"))

from constraint_ranker import (  # noqa: E402
    _capability_evidence,
    _company_capture_priority,
    build_final_selection,
)


def constraint(makers: list[dict], *, early: bool = False, grade: str = "A",
               binding: bool = False, pipeline: list[dict] | None = None,
               as_of_evidence: bool = True,
               catalyst_tickers: list[str] | None = None) -> dict:
    return {
        "constraint": "Test Product",
        "as_of_constraint_evidence": as_of_evidence,
        "early_investability_signal": early,
        "constraint_quality": {
            "company_selection_allowed": grade in {"A", "B"},
            "grade": grade,
            "evidence_completeness": {"level": "THIN"},
            "missing_legs": ["quantified physical supply"],
            "binding_demand": binding,
        },
        "verified_current_makers": makers,
        "pipeline_or_direct_role_makers": pipeline or [],
        "company_capture_readiness": {
            "catalyst_backed_exact_roles": [
                {"ticker": ticker} for ticker in (catalyst_tickers or [])
            ],
        },
    }


def candidate(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "company": ticker + " Limited",
        "products": ["Test Product"],
        "corroborated_products": ["Test Product"],
        "corroborated_maker": True,
        "corr_false": False,
        "order_book": True,
        "types": ["direct_supplier"],
        "any_demand_side": False,
        "risk_tier": "NORMAL",
        "composite_score": 0.8,
        "technical": None,
        "capex_signals": 2,
        "product_evidence": {
            "Test Product": {
                "order_book": True,
                "capex_signals": 2,
                "types": ["direct_supplier"],
                "demand_side": False,
            },
        },
    }


class StrictEvidenceHandOffTests(unittest.TestCase):
    def test_capture_priority_rewards_same_product_economic_proof_not_mentions(self):
        weak = candidate("WEAK")
        weak["product_evidence"]["Test Product"].update({
            "order_book": False,
            "capex_signals": 0,
            "max_earnings_capture_count": 0,
            "max_physical_evidence_count": 1,
        })
        strong = candidate("STRONG")
        strong["product_evidence"]["Test Product"].update({
            "max_earnings_capture_count": 3,
            "max_physical_evidence_count": 2,
        })
        theme = constraint([], early=True, binding=True)

        weak_score = _company_capture_priority(weak, theme, "Test Product")["score"]
        strong_score = _company_capture_priority(strong, theme, "Test Product")["score"]

        self.assertGreater(strong_score, weak_score)

    def test_legacy_capability_hit_cannot_self_promote_into_producer_population(self):
        payload = {
            "Test Product": {
                "snapshot": "2022-12-31",
                "makers": [{
                    "ticker": "LEGACY",
                    "maker_status": "operating manufacturer — independently corroborated",
                    "last_evidence_date": "2022-12-20",
                    "strict_evidence_count": 3,
                    "direct_evidence_count": 3,
                    "evidence_samples": [],
                }],
                "role_candidates": [],
                "rejected_candidates": [],
                "coverage": {},
            },
        }
        operating, pipeline, _, role_candidates, _, _, _ = _capability_evidence(
            "Test Product", payload, date(2022, 12, 31)
        )

        self.assertEqual(operating, [])
        self.assertEqual(pipeline, [])
        self.assertEqual(role_candidates[0]["ticker"], "LEGACY")
        self.assertEqual(
            role_candidates[0]["adjudication_state"],
            "QUARANTINED_AMBIGUOUS_ROLE",
        )

    def test_exact_role_ledger_state_is_the_producer_authority(self):
        payload = {
            "Test Product": {
                "snapshot": "2022-12-31",
                "makers": [{
                    "ticker": "LEDGER",
                    "maker_status": "operating manufacturer — independently corroborated",
                    "last_evidence_date": "2022-12-20",
                    "adjudication_state": "OPERATING_PRODUCER_EVIDENCED",
                    "source": "own-filings company-product-role ledger",
                    "evidence_samples": [],
                }],
                "role_candidates": [],
                "rejected_candidates": [],
                "coverage": {},
            },
        }
        operating, pipeline, _, _, _, _, _ = _capability_evidence(
            "Test Product", payload, date(2022, 12, 31)
        )

        self.assertEqual([row["ticker"] for row in operating], ["LEDGER"])
        self.assertEqual(pipeline, [])

    def test_operating_product_identity_persists_when_catalyst_evidence_is_old(self):
        payload = {
            "Test Product": {
                "snapshot": "2022-12-31",
                "makers": [{
                    "ticker": "DURABLE",
                    "maker_status": "operating manufacturer — independently corroborated",
                    "last_evidence_date": "2019-12-20",
                    "adjudication_state": "OPERATING_PRODUCER_EVIDENCED",
                    "source": "own-filings company-product-role ledger",
                    "evidence_samples": [],
                }],
                "role_candidates": [], "rejected_candidates": [], "coverage": {},
            },
        }
        operating, pipeline, stale, _, _, _, _ = _capability_evidence(
            "Test Product", payload, date(2022, 12, 31)
        )

        self.assertEqual([row["ticker"] for row in operating], ["DURABLE"])
        self.assertFalse(operating[0]["producer_identity_fresh"])
        self.assertIn("refresh current", operating[0]["verification_status"])
        self.assertEqual(pipeline, [])
        self.assertEqual(stale, [])

    def test_stale_pipeline_plan_does_not_become_durable_operating_identity(self):
        payload = {
            "Test Product": {
                "snapshot": "2022-12-31",
                "makers": [{
                    "ticker": "OLDPLAN",
                    "maker_status": "capacity pipeline / group manufacturing plan",
                    "last_evidence_date": "2019-12-20",
                    "adjudication_state": "PIPELINE_EVIDENCED",
                    "source": "own-filings company-product-role ledger",
                    "evidence_samples": [],
                }],
                "role_candidates": [], "rejected_candidates": [], "coverage": {},
            },
        }
        operating, pipeline, stale, _, _, _, _ = _capability_evidence(
            "Test Product", payload, date(2022, 12, 31)
        )

        self.assertEqual(operating, [])
        self.assertEqual(pipeline, [])
        self.assertEqual([row["ticker"] for row in stale], ["OLDPLAN"])

    def test_mapper_corroboration_without_fresh_maker_packet_is_research_only(self):
        result = build_final_selection([candidate("MAPPED")], [constraint([])])

        self.assertEqual(result["priorities"], [])
        self.assertEqual(result["eligible_tickers"], [])
        self.assertEqual(result["underwriting_candidate_tickers"], ["MAPPED"])
        self.assertIn("research candidate", result["underwriting_candidates"][0]["status"])

    def test_fresh_product_specific_maker_packet_reaches_committee_hand_off(self):
        result = build_final_selection(
            [candidate("STRICT")], [constraint([{"ticker": "STRICT"}])]
        )

        self.assertEqual(result["eligible_tickers"], ["STRICT"])
        self.assertEqual(result["underwriting_candidates"], [])
        self.assertEqual(result["priorities"][0]["strict_verified_products"], ["Test Product"])
        self.assertIn("investment-committee candidate", result["priorities"][0]["status"])

    def test_early_lane_rejects_mapper_role_without_strict_maker_packet(self):
        result = build_final_selection(
            [candidate("EARLY")], [constraint([], early=True, grade="UNMEASURED")]
        )

        self.assertEqual(result["priorities"], [])
        self.assertEqual(result["early_timing_tickers"], [])

    def test_early_lane_cannot_borrow_mapper_proof_from_another_product(self):
        row = candidate("WRONGPRODUCT")
        row["products"] = ["Test Product", "Other Product"]
        row["corroborated_products"] = ["Other Product"]
        result = build_final_selection(
            [row], [constraint([], early=True, grade="UNMEASURED")]
        )

        self.assertEqual(result["early_timing_candidates"], [])

    def test_early_lane_cannot_borrow_company_catalyst_from_another_product(self):
        row = candidate("WRONGCATALYST")
        row["products"] = ["Test Product", "Other Product"]
        row["corroborated_products"] = ["Test Product"]
        row["product_evidence"] = {
            "Test Product": {
                "order_book": False,
                "capex_signals": 0,
                "types": ["direct_supplier"],
                "demand_side": False,
            },
            "Other Product": {
                "order_book": True,
                "capex_signals": 4,
                "types": ["direct_supplier"],
                "demand_side": False,
            },
        }
        result = build_final_selection(
            [row], [constraint([], early=True, grade="UNMEASURED")]
        )

        self.assertEqual(result["early_timing_candidates"], [])

    def test_unmeasured_constraint_has_no_position_authority_even_with_maker(self):
        result = build_final_selection(
            [candidate("UNMEASURED")],
            [constraint([{"ticker": "UNMEASURED"}], early=True, grade="UNMEASURED")],
        )

        self.assertEqual(result["early_timing_candidates"], [])
        self.assertEqual(result["stock_verdicts"][0]["state"], "RESEARCH_ONLY")

    def test_raw_screen_name_has_no_position_authority(self):
        result = build_final_selection([candidate("RAW")], [constraint([], grade="UNMEASURED")])

        verdict = result["stock_verdicts"][0]
        self.assertEqual(verdict["state"], "RESEARCH_ONLY")
        self.assertEqual(verdict["action"], "DO NOT BUY FROM THIS LIST")
        self.assertEqual(result["decision_summary"]["conditional_starter_count"], 0)

    def test_unrelated_failed_mapper_tag_does_not_veto_exact_core_tuple(self):
        row = candidate("TUPLE")
        row["all_products"] = ["Adjacent Noise", "Another Tag", "Test Product"]
        row["corr_false"] = True
        row["uncorroborated_products"] = ["Adjacent Noise", "Another Tag"]

        result = build_final_selection(
            [row], [constraint([{"ticker": "TUPLE"}])]
        )

        self.assertEqual(result["eligible_tickers"], ["TUPLE"])
        self.assertNotIn(
            "at least one mapped product failed own-filing corroboration",
            result["priorities"][0]["failed_gates"],
        )

    def test_unmeasured_exact_role_can_only_enter_capped_discovery_sleeve(self):
        result = build_final_selection(
            [candidate("DISCOVERY")],
            [constraint(
                [{"ticker": "DISCOVERY", "earnings_capture_count": 1}],
                early=True, grade="UNMEASURED", binding=True,
            )],
        )

        self.assertEqual(result["priorities"], [])
        self.assertEqual(result["early_timing_candidates"], [])
        self.assertEqual(result["discovery_starter_tickers"], ["DISCOVERY"])
        verdict = result["stock_verdicts"][0]
        self.assertEqual(verdict["state"], "DISCOVERY_STARTER")
        self.assertEqual(verdict["max_portfolio_weight_pct"], 1.0)
        self.assertEqual(result["discovery_sleeve_max_portfolio_weight_pct"], 5.0)

    def test_discovery_sleeve_rejects_non_normal_risk(self):
        row = candidate("RISKY")
        row["risk_tier"] = "ELEVATED"
        result = build_final_selection(
            [row],
            [constraint(
                [{"ticker": "RISKY"}], early=True,
                grade="UNMEASURED", binding=True,
            )],
        )

        self.assertEqual(result["discovery_starter_candidates"], [])
        self.assertEqual(result["stock_verdicts"][0]["state"], "RESEARCH_ONLY")

    def test_stale_operating_identity_is_visible_but_has_no_discovery_position_authority(self):
        result = build_final_selection(
            [candidate("STALE_FACTORY")],
            [constraint(
                [{"ticker": "STALE_FACTORY", "producer_identity_fresh": False}],
                early=True, grade="UNMEASURED", binding=True,
            )],
        )

        self.assertEqual(result["discovery_starter_candidates"], [])
        self.assertEqual(result["stock_verdicts"][0]["state"], "RESEARCH_ONLY")

    def test_discovery_sleeve_cannot_borrow_role_from_another_product(self):
        row = candidate("WRONGROLE")
        row["all_products"] = ["Test Product", "Other Product"]
        row["corroborated_products"] = ["Other Product"]
        result = build_final_selection(
            [row],
            [constraint(
                [], early=True, grade="UNMEASURED", binding=True,
                pipeline=[{"ticker": "OTHER", "pipeline_evidence_count": 3}],
            )],
        )

        self.assertEqual(result["discovery_starter_candidates"], [])

    def test_company_triangulation_requires_and_can_admit_strict_producer_role(self):
        result = build_final_selection(
            [candidate("MAPPER")],
            [constraint(
                [], early=True, grade="UNMEASURED", binding=True,
                as_of_evidence=False, catalyst_tickers=["MAPPER", "PEER"],
                pipeline=[
                    {"ticker": "MAPPER", "pipeline_evidence_count": 2,
                     "earnings_capture_count": 2},
                    {"ticker": "PEER", "pipeline_evidence_count": 2,
                     "earnings_capture_count": 2},
                ],
            )],
        )

        row = result["discovery_starter_candidates"][0]
        self.assertEqual(row["ticker"], "MAPPER")
        self.assertEqual(row["producer_state"], "PIPELINE_OR_DIRECT_ROLE")
        self.assertEqual(row["max_portfolio_weight_pct"], 1.0)

    def test_company_led_constraint_requires_selected_company_earnings_capture(self):
        result = build_final_selection(
            [candidate("NO_CAPTURE")],
            [constraint(
                [], early=True, grade="UNMEASURED", binding=True,
                as_of_evidence=False, catalyst_tickers=["NO_CAPTURE", "PEER"],
                pipeline=[
                    {"ticker": "NO_CAPTURE", "pipeline_evidence_count": 3,
                     "earnings_capture_count": 0},
                    {"ticker": "PEER", "pipeline_evidence_count": 3,
                     "earnings_capture_count": 2},
                ],
            )],
        )

        self.assertEqual(result["discovery_starter_candidates"], [])

    def test_quarantined_mapper_only_role_has_no_discovery_position_authority(self):
        result = build_final_selection(
            [candidate("MAPPER_ONLY")],
            [constraint(
                [], early=True, grade="UNMEASURED", binding=True,
                as_of_evidence=True, catalyst_tickers=["MAPPER_ONLY", "PEER"],
            )],
        )

        self.assertEqual(result["discovery_starter_candidates"], [])
        self.assertEqual(result["stock_verdicts"][0]["state"], "RESEARCH_ONLY")

    def test_one_company_cannot_self_prove_unstructured_constraint(self):
        result = build_final_selection(
            [candidate("SOLE")],
            [constraint(
                [], early=True, grade="UNMEASURED", binding=True,
                as_of_evidence=False, catalyst_tickers=["SOLE"],
            )],
        )

        self.assertEqual(result["discovery_starter_candidates"], [])


if __name__ == "__main__":
    unittest.main()
