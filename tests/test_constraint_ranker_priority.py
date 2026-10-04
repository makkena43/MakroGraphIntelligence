#!/usr/bin/env python3
"""Regression tests for transparent constraint-to-company research ordering."""

from __future__ import annotations

from datetime import date
import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "stock_report"))

from constraint_ranker import (  # noqa: E402
    _automatic_coverage_placeholder,
    build_final_selection,
    rank_greatest_constraints,
)


class ConstraintResearchPriorityTests(unittest.TestCase):
    def test_automatic_identity_without_evidence_is_coverage_not_constraint(self):
        alias = {"status": "AUTO_DISCOVERY"}
        ledger = {
            "state": "DISCOVERY", "classification": "WATCH", "evidence": [],
            "import_dependency_ratio": None, "capacity_gap_ratio": None,
        }
        self.assertTrue(_automatic_coverage_placeholder(alias, ledger))
        self.assertFalse(_automatic_coverage_placeholder(
            alias, {**ledger, "evidence": [{"source_url": "https://source.example"}]}
        ))

    def test_same_tier_prefers_exact_catalyst_backed_producer_universe(self):
        as_of = date(2022, 12, 31)
        products = [
            {"constrained_product": "Product With Capture", "theme_name": "Theme A",
             "n_companies": 1, "any_order_book": True, "any_import_sub": False,
             "last_mapped": as_of},
            {"constrained_product": "Product Without Capture", "theme_name": "Theme B",
             "n_companies": 20, "any_order_book": True, "any_import_sub": False,
             "last_mapped": as_of},
        ]
        gaps = [
            {"component": "Product With Capture", "gap_pct": 30, "as_of_date": as_of,
             "provenance_status": "PRIMARY_SOURCE", "source_url": "https://one.example",
             "source_published_at": as_of},
            {"component": "Product Without Capture", "gap_pct": 30, "as_of_date": as_of,
             "provenance_status": "PRIMARY_SOURCE", "source_url": "https://two.example",
             "source_published_at": as_of},
        ]
        supply = {
            "Product With Capture": [{
                "ticker": "EXACT", "company": "Exact Ltd", "corroborated": True,
                "beneficiary_type": "direct_supplier", "demand_side_flag": False,
                "sector_mismatch": False, "has_order_book_signals": True, "capex_signals": 2,
            }],
            "Product Without Capture": [{
                "ticker": "NOISY", "company": "Noisy Ltd", "corroborated": False,
                "beneficiary_type": "direct_supplier", "demand_side_flag": False,
                "sector_mismatch": False, "has_order_book_signals": True, "capex_signals": 5,
            }],
        }

        ranked = rank_greatest_constraints(
            products, gaps, [], supply, {}, {}, as_of, top_n=2,
        )

        self.assertEqual(ranked[0]["constraint"], "Product With Capture")
        capture = ranked[0]["company_capture_readiness"]
        self.assertEqual(capture["catalyst_backed_exact_role_count"], 1)
        self.assertIn("research ordering only", capture["selection_use"])
        self.assertIn("cannot override", ranked[0]["constraint_research_priority"]["selection_use"])

    def test_missing_measurement_is_an_explicit_coverage_state_not_constraint_absence(self):
        as_of = date(2022, 12, 31)
        products = [{
            "constrained_product": "Exact product", "theme_name": "Theme",
            "n_companies": 1, "any_order_book": True, "any_import_sub": False,
            "last_mapped": as_of,
        }]
        ranked = rank_greatest_constraints(
            products, [], [], {"Exact product": []}, {}, {}, as_of, top_n=1,
        )

        coverage = ranked[0]["constraint_quality"]["measurement_coverage"]
        self.assertEqual(coverage["status"], "MISSING_EXACT_MEASUREMENT")
        self.assertIn("DETECTED", coverage["display_label"])
        self.assertIn("data-coverage state", coverage["interpretation"])

    def test_every_constraint_has_role_separated_company_populations(self):
        as_of = date(2022, 12, 31)
        products = [{
            "constrained_product": "Exact product", "theme_name": "Theme",
            "n_companies": 2, "any_order_book": True, "any_import_sub": False,
            "last_mapped": as_of,
        }]
        supply = {"Exact product": [{
            "ticker": "LEAD", "company": "Lead Ltd", "corroborated": True,
            "beneficiary_type": "direct_supplier", "demand_side_flag": False,
            "sector_mismatch": False, "signal_count": 2,
        }]}
        ranked = rank_greatest_constraints(
            products, [], [], supply, {}, {}, as_of, top_n=1,
        )

        populations = ranked[0]["company_populations"]
        self.assertEqual(populations["status"], "AUTOMATED_ADJUDICATION_UNRESOLVED")
        self.assertEqual(
            populations["quarantined_exceptions"][0]["ticker"], "LEAD"
        )
        self.assertEqual(
            populations["quarantined_exceptions"][0]["adjudication_state"],
            "QUARANTINED_MAPPER_ONLY",
        )
        self.assertFalse(
            populations["automated_adjudication"]["manual_review_required_for_routine_promotion"]
        )
        self.assertEqual(populations["operating_product_producers"], [])

    def test_mapper_exception_display_is_capped_and_weak_hits_are_auto_rejected(self):
        as_of = date(2022, 12, 31)
        products = [{
            "constrained_product": "Exact product", "theme_name": "Theme",
            "n_companies": 7, "any_order_book": True, "last_mapped": as_of,
        }]
        supply = {"Exact product": [
            {
                "ticker": f"Q{i}", "company": f"Queue {i}", "corroborated": True,
                "beneficiary_type": "direct_supplier", "demand_side_flag": False,
                "sector_mismatch": False, "signal_count": i,
            }
            for i in range(5)
        ] + [{
            "ticker": "NO_ROLE", "company": "No Role", "corroborated": False,
            "beneficiary_type": "direct_supplier", "demand_side_flag": False,
            "sector_mismatch": False, "signal_count": 10,
        }, {
            "ticker": "EPC", "company": "EPC", "corroborated": True,
            "beneficiary_type": "epc_contractor", "demand_side_flag": False,
            "sector_mismatch": False, "signal_count": 10,
        }]}

        populations = rank_greatest_constraints(
            products, [], [], supply, {}, {}, as_of, top_n=1,
        )[0]["company_populations"]

        self.assertEqual(len(populations["quarantined_exceptions"]), 3)
        audit = populations["automated_adjudication"]
        self.assertEqual(audit["quarantined_count"], 5)
        self.assertEqual(audit["auto_rejected_count"], 2)
        self.assertEqual(audit["rejection_reasons"]["AUTO_REJECTED_NO_ROLE"], 1)
        self.assertEqual(audit["rejection_reasons"]["AUTO_REJECTED_WRONG_ROLE"], 1)

    def test_family_context_without_a_number_is_not_called_a_family_measurement(self):
        as_of = date(2022, 12, 31)
        products = [{
            "constrained_product": "Family product", "theme_name": "Theme",
            "n_companies": 1, "any_order_book": True, "last_mapped": as_of,
        }]
        ledger = {"family_chain": {
            "constraint_key": "family_chain", "constraint_name": "Family",
            "as_of_date": as_of, "state": "DISCOVERY", "classification": "WATCH",
            "product_scope": "FAMILY", "evidence": [],
        }}
        aliases = {"family product": {
            "product_label": "Family product", "constraint_key": "family_chain",
            "match_scope": "FAMILY", "status": "REVIEWED",
        }}
        ranked = rank_greatest_constraints(
            products, [], [], {}, {}, {}, as_of, top_n=1,
            constraint_ledger=ledger, constraint_aliases=aliases,
        )

        coverage = ranked[0]["constraint_quality"]["measurement_coverage"]
        self.assertEqual(coverage["status"], "FAMILY_CONTEXT_ONLY")
        self.assertIn("measurement is missing", coverage["display_label"])

    def test_reference_only_physical_constraint_is_detected_but_cannot_create_stock_authority(self):
        as_of = date(2022, 12, 31)
        ranked = rank_greatest_constraints(
            [], [{"component": "Reference-only product", "gap_pct": 42,
                  "as_of_date": as_of, "provenance_status": "PRIMARY_SOURCE",
                  "source_url": "https://official.example/reference",
                  "source_published_at": as_of}], [], {}, {}, {}, as_of, top_n=8,
        )

        self.assertEqual(len(ranked), 1)
        constraint = ranked[0]
        self.assertEqual(constraint["constraint"], "Reference-only product")
        self.assertTrue(constraint["reference_only_constraint"])
        self.assertIn("CAPACITY_REFERENCE", constraint["detection_origins"])
        self.assertIn("listed-company mapping incomplete", constraint["research_tier"])
        self.assertIn("exact listed-company mapping", constraint["stock_selection_status"])

        decision = build_final_selection([], ranked)
        self.assertEqual(decision["constraint_decisions"][0]["state"], "MAP_COMPANIES")
        self.assertEqual(decision["decision_summary"]["conditional_starter_count"], 0)

    def test_unsourced_physical_estimate_is_context_not_a_measured_grade(self):
        as_of = date(2022, 12, 31)
        ranked = rank_greatest_constraints(
            [], [{"component": "Unverified estimate", "gap_pct": 80,
                  "as_of_date": as_of, "provenance_status": "PENDING_SOURCE"}],
            [], {}, {}, {}, as_of, top_n=8,
        )

        quality = ranked[0]["constraint_quality"]
        self.assertEqual(quality["grade"], "UNMEASURED")
        self.assertEqual(
            quality["measurement_coverage"]["status"],
            "MISSING_EXACT_MEASUREMENT",
        )

    def test_exact_ledger_can_prove_constraint_legs_without_inventing_a_company(self):
        as_of = date(2022, 12, 31)
        ledger = {
            "test_chain": {
                "constraint_key": "test_chain", "constraint_name": "Test chain",
                "as_of_date": as_of, "state": "BINDING",
                "classification": "PHYSICAL_CONSTRAINT", "product_scope": "EXACT_CHAIN",
                "import_dependency_ratio": 0.8, "measurement_date": as_of,
                "binding_demand_status": "CONFIRMED",
                "resupply_barrier_status": "CONFIRMED", "next_validation_date": None,
                "evidence": [
                    {"evidence_type": "IMPORT", "source_date": as_of,
                     "source_url": "https://one.example", "independence_key": "source-one"},
                    {"evidence_type": "BINDING", "source_date": as_of,
                     "source_url": "https://two.example", "independence_key": "source-two"},
                ],
            }
        }
        aliases = {
            "test product": {
                "product_label": "Test Product", "constraint_key": "test_chain",
                "match_scope": "EXACT", "status": "REVIEWED",
            }
        }
        ranked = rank_greatest_constraints(
            [], [], [], {}, {}, {}, as_of, top_n=8,
            constraint_ledger=ledger, constraint_aliases=aliases,
        )

        # Legacy ledger rows without immutable observation IDs remain useful
        # exact-chain research coverage, but cannot create a measured grade.
        self.assertEqual(ranked[0]["constraint_quality"]["grade"], "UNMEASURED")
        self.assertEqual(
            ranked[0]["constraint_quality"]["source_diversity"]["current_independent_source_count"], 0
        )
        decision = build_final_selection([], ranked)
        self.assertEqual(decision["constraint_decisions"][0]["state"], "MAP_COMPANIES")

    def test_dated_upstream_measure_is_not_hidden_by_unmeasured_mapper_rows(self):
        as_of = date(2022, 12, 31)
        products = [
            {
                "constrained_product": "Measured upstream component",
                "theme_name": "Measured upstream component — Physical Constraint",
                "n_companies": 0,
                "last_mapped": date(2022, 12, 7),
                "any_order_book": True,
                "detection_origin": "UPSTREAM_CONSTRAINT_PIPELINE",
                "upstream_candidate": {
                    "physical_quality": "B",
                    "research_priority": 66,
                    "last_mapped": date(2022, 12, 7),
                    "evidence_legs": {
                        "has_import_measure": True,
                        "import_share": 0.35,
                        "import_as_of": date(2022, 12, 7),
                    },
                },
            },
            {
                "constrained_product": "Unmeasured mapper theme",
                "theme_name": "Theme",
                "n_companies": 20,
                "last_mapped": as_of,
                "any_order_book": True,
            },
        ]

        ranked = rank_greatest_constraints(
            products, [], [], {}, {}, {}, as_of, top_n=1,
        )

        self.assertEqual(ranked[0]["constraint"], "Measured upstream component")
        # A stored upstream grade is not self-authenticating. The immutable
        # accepted physical observation must be linked before it can survive.
        self.assertEqual(ranked[0]["constraint_quality"]["grade"], "UNMEASURED")
        self.assertEqual(
            ranked[0]["import_dependency"]["evidence_source"],
            "UPSTREAM_CONSTRAINT_PIPELINE",
        )


if __name__ == "__main__":
    unittest.main()
