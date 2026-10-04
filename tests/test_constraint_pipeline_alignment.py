"""Regression tests for the ingestion-to-selector constraint bridge."""

import sys
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.makrograph.india.capacity_engine import (  # noqa: E402
    CapacityGapDetector,
    CapacityRequirement,
)
from src.makrograph.india.constraint_candidate_engine import (  # noqa: E402
    ROLE_EXTRACTOR_VERSION,
    discovery_constraint_key,
    score_constraint_profile,
)
from src.makrograph.constraint_contract import (  # noqa: E402
    COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
    derive_constraint_state,
    effective_alias_scope,
    physical_observation_admissibility,
)
from src.makrograph.india.import_localization import ImportDependencyEngine  # noqa: E402
from src.makrograph.nlp.entity_extractor import EntityExtractor  # noqa: E402
from src.makrograph.nlp.signal_extractor import SignalExtractor  # noqa: E402
from src.makrograph.nlp.product_quality import is_product_label  # noqa: E402


class ConstraintPipelineAlignmentTests(unittest.TestCase):
    def test_role_writer_and_constraint_reader_share_one_version_contract(self):
        self.assertEqual(
            ROLE_EXTRACTOR_VERSION,
            COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
        )

    def test_literal_product_identity_key_is_stable_and_not_a_scarcity_claim(self):
        self.assertEqual(
            discovery_constraint_key("Power Transformer"),
            "discovery_power_transformer",
        )

    def test_dated_canonical_identity_overrides_a_later_family_scope(self):
        scope, basis = effective_alias_scope(
            "Power Transformer", "FAMILY", "Power Transformer"
        )
        self.assertEqual(scope, "EXACT")
        self.assertEqual(basis, "DATED_CANONICAL_IDENTITY")
        later_scope, later_basis = effective_alias_scope(
            "Power Transformer", "FAMILY", "Power-grid transmission build-out"
        )
        self.assertEqual(later_scope, "FAMILY")
        self.assertEqual(later_basis, "REVIEWED_ALIAS")

    def test_generic_manufacturing_grammar_discovers_product_not_policy_noise(self):
        extractor = EntityExtractor({"use_spacy": False, "min_confidence": 0.65})
        entities = extractor._extract_with_rules(
            "The Company manufactures precision irrigation pumps for export markets."
        )
        products = {entity.canonical_name.casefold() for entity in entities
                    if entity.entity_type == "PRODUCT"}
        self.assertIn("precision irrigation pumps", products)

        noise = extractor._extract_with_rules(
            "The industry plans to set up manufacturing capacity in coming years."
        )
        self.assertFalse([entity for entity in noise if entity.entity_type == "PRODUCT"])

    def test_signal_dedup_preserves_independent_mechanism_types(self):
        extractor = SignalExtractor({"min_confidence": 0.65})
        signals = extractor.extract(
            "Customer qualification cycle takes 18 months. "
            "Our lead times extended to 52 weeks. "
            "Imports account for 85% of domestic demand. "
            "Commercial production commenced at the 2 GW plant."
        )
        signal_types = {signal.signal_type for signal in signals}
        self.assertTrue({
            "qualification_barrier", "lead_time_extension",
            "import_dependency_quantified", "supply_response_commissioning",
        }.issubset(signal_types))

    def test_disclosure_noise_is_not_a_physical_product(self):
        for label in (
            "Chartered Accountant", "Financial Results", "Q2 & H1",
            "Madhya Pradesh", "Debt EBITDA", "Eligible Shareholder",
        ):
            self.assertFalse(is_product_label(label), label)
        self.assertTrue(is_product_label("precision irrigation pumps"))
        self.assertTrue(is_product_label("printed circuit board"))

    def test_2024_capacity_reference_does_not_leak_into_2022(self):
        requirement = CapacityRequirement(
            sector="solar", component="solar_cells_gw", required_quantity=100,
            unit="GW", supply_chain_stage="cell", source_target="test",
            target_year=2030,
        )
        detector = CapacityGapDetector()
        self.assertEqual(detector.detect([requirement], as_of_date=date(2022, 12, 31)), [])
        self.assertEqual(len(detector.detect([requirement], as_of_date=date(2024, 12, 31))), 1)

    def test_2024_import_packet_does_not_leak_into_earlier_replay(self):
        engine = ImportDependencyEngine()
        self.assertEqual(engine.get_dependencies(as_of_date=date(2022, 12, 31)), [])
        self.assertGreater(len(engine.get_dependencies(as_of_date=date(2024, 12, 31))), 0)

    def test_grade_a_requires_independent_source_diversity(self):
        base = {
            "has_capacity_measure": True,
            "has_import_measure": False,
            "demand_source_count": 1,
            "binding_source_count": 1,
            "barrier_source_count": 1,
            "policy_source_count": 0,
            "resolution_source_count": 0,
            "company_count": 1,
            "physical_observation_count": 1,
            "non_policy_source_count": 1,
        }
        one_source = score_constraint_profile({
            **base, "independent_source_count": 1, "independent_date_count": 1,
        })
        two_sources = score_constraint_profile({
            **base, "independent_source_count": 2, "non_policy_source_count": 2,
            "independent_date_count": 2,
        })
        self.assertEqual(one_source["physical_quality"], "B")
        self.assertEqual(two_sources["physical_quality"], "A")

    def test_measurement_without_accepted_primary_observation_is_unmeasured(self):
        scored = score_constraint_profile({
            "has_capacity_measure": True,
            "demand_source_count": 2,
            "binding_source_count": 2,
            "barrier_source_count": 2,
            "independent_source_count": 3,
            "non_policy_source_count": 3,
            "independent_date_count": 3,
            "physical_observation_count": 0,
        })
        self.assertEqual(scored["physical_quality"], "UNMEASURED")
        self.assertIn("accepted exact-product primary observation", " ".join(scored["missing_legs"]))

    def test_future_policy_target_gap_cannot_become_physical_measurement(self):
        admitted, reason = physical_observation_admissibility({
            "observation_type": "CAPACITY",
            "review_status": "ACCEPTED",
            "provenance_status": "PRIMARY_SOURCE",
            "product_scope": "EXACT",
            "source_url": "https://example.gov/capacity",
            "observed_at": date(2022, 6, 1),
            "published_at": date(2022, 6, 2),
            "available_at": date(2022, 6, 2),
            "metrics": {
                "measurement_basis": "FUTURE_POLICY_TARGET",
                "gap_pct": 80,
                "numerator": 20,
                "denominator": 100,
            },
        }, date(2022, 12, 31))
        self.assertFalse(admitted)
        self.assertIn("future-target", reason)

    def test_value_chain_layers_can_have_opposing_physical_states(self):
        cells = derive_constraint_state({
            "measured": True, "demand": True, "binding": True, "barrier": True,
        })
        modules = derive_constraint_state({
            "measured": True, "demand": True, "overcapacity": True,
        })
        self.assertEqual(cells["physical_state"], "BINDING")
        self.assertEqual(modules["physical_state"], "OVERCAPACITY")

    def test_unmeasured_chain_never_gets_measured_investigation_state(self):
        scored = score_constraint_profile({
            "has_capacity_measure": False,
            "has_import_measure": False,
            "demand_source_count": 10,
            "binding_source_count": 10,
            "barrier_source_count": 10,
            "policy_source_count": 10,
            "resolution_source_count": 0,
            "independent_source_count": 10,
            "independent_date_count": 10,
            "company_count": 10,
        })
        self.assertEqual(scored["physical_quality"], "UNMEASURED")
        self.assertNotEqual(scored["research_state"], "INVESTIGATE_NOW")


if __name__ == "__main__":
    unittest.main()
