"""Precision tests for automatic issuer product-role extraction.

These examples exercise grammar categories, not company, theme, or product
allowlists.  An automatic maker must be the issuer's operating role; industry
commentary and capacity plans belong in research, never in a Buy universe.
"""

import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "stock_report"))

from company_product_roles import (  # noqa: E402
    _derived_term_owners,
    _link,
    _phrases_in_window,
    _role_windows,
    _role_type,
    _short_term_modifier_conflict,
    _valid_product_label,
    automatic_role_adjudication,
)
from company_capabilities import (  # noqa: E402
    _document_role_candidate_detail,
    _maker_evidence_detail,
    candidate_terms,
)
import re


def classify(text: str, product: str):
    start = text.casefold().index(product.casefold())
    return _role_type(text, start, start + len(product))


def test_industry_capacity_commentary_is_not_issuer_manufacturing():
    text = (
        "Successful bidders have announced plans to set up manufacturing capacities "
        "of three gigawatts of solar cells and modules in the country."
    )
    assert classify(text, "solar cells") == (None, False)


def test_industry_production_capacity_noun_is_not_an_issuer_production_action():
    text = (
        "We believe manufacturers of solar cells have significant installed "
        "production capacity relative to global demand."
    )
    assert classify(text, "solar cells") == (None, False)


def test_company_capacity_plan_is_research_not_operating_proof():
    text = (
        "The Company has approved a plan to set up a 3 GW solar cell manufacturing "
        "plant after completion of feasibility studies."
    )
    assert classify(text, "solar cell") == ("DIRECT_ROLE_UNCLASSIFIED", False)


def test_end_use_does_not_turn_an_input_maker_into_product_maker():
    text = (
        "Our company manufactures solar glass at its manufacturing plant for use in "
        "solar modules."
    )
    assert classify(text, "solar modules") == (None, False)


def test_capacity_equivalent_does_not_claim_the_product_is_made_by_issuer():
    text = (
        "The board approved a glass expansion that would be sufficient to manufacture "
        "solar panels of 2.5 GW. That is a summary of our performance."
    )
    assert classify(text, "solar panels") == (None, False)


def test_owned_operating_plant_is_a_manufacturer():
    text = (
        "The Company manufactures solar cells at its manufacturing plant in Gujarat, "
        "with 1 GW operating capacity."
    )
    assert classify(text, "solar cells") == ("MANUFACTURER", True)


def test_owned_product_plant_without_action_verb_is_a_manufacturer():
    text = (
        "It has a state-of-the-art solar cell and module manufacturing plant "
        "of 1.1 GW at Bengaluru."
    )
    assert classify(text, "solar cell") == ("MANUFACTURER", True)


def test_future_owned_product_plant_is_not_operating_proof():
    text = "The Company plans to set up a 4 GW solar cell manufacturing plant."
    assert classify(text, "solar cell") == ("DIRECT_ROLE_UNCLASSIFIED", False)


def test_commissioning_a_product_is_installation_not_manufacturing():
    text = "We commissioned transformers at the customer site."
    assert classify(text, "transformers") == ("EPC_OR_INSTALLER", False)


def test_legacy_capability_proof_does_not_treat_commissioning_as_production():
    text = "The Company commissioned transformers at the customer's manufacturing facility."
    assert _maker_evidence_detail(
        text, re.compile(r"\btransformers?\b", re.I)
    ) is None


def test_legacy_capability_proof_does_not_treat_capacity_noun_as_making_action():
    text = (
        "The Company reviewed market manufacturers of solar cells and their "
        "installed production capacity."
    )
    assert _maker_evidence_detail(
        text, re.compile(r"\bsolar cells?\b", re.I)
    ) is None


def test_financial_and_corporate_terms_are_not_product_vocabulary():
    assert not _valid_product_label("PAT Margin")
    assert not _valid_product_label("Equity Share")
    assert not _valid_product_label("Standalone Cash Flow")
    assert not _valid_product_label("Exceptional Items")
    assert not _valid_product_label("Debt Free")
    assert not _valid_product_label("Total Comprehensive")
    assert not _valid_product_label("Voluntary Retirement Scheme")
    assert not _valid_product_label("www.exchange.example.com")
    assert _valid_product_label("Optical Fiber Cable")


def test_negated_manufacturing_statement_is_not_a_company_role():
    text = (
        "The Company is not a manufacturing company and hence its operating margin "
        "is not applicable to total comprehensive income."
    )
    assert classify(text, "total comprehensive") == (None, False)


def test_acquisition_target_facility_is_not_issuer_operating_proof():
    text = (
        "The Company acquired a stake in the target company, whose manufacturing facility "
        "is situated at Sikkim and manufactures nutritional products."
    )
    assert classify(text, "nutritional products") == (None, False)


def test_document_level_product_and_plant_are_retained_only_as_role_candidate():
    text = (
        "Our product portfolio includes power transformers for utilities and power transformers "
        "for industrial customers.\n\nThe Company operates manufacturing facilities in three locations."
    )
    detail = _document_role_candidate_detail(text, re.compile(r"\bpower transformers?\b", re.I))
    assert detail is not None
    assert detail["state"] == "document_role_candidate"
    assert detail["physical_asset"] is False
    assert detail["evidence_scope"].startswith("document-level")


def test_document_level_policy_population_is_not_an_issuer_role_candidate():
    text = (
        "Successful bidders have announced manufacturing capacity for solar cells. "
        "Eligible manufacturers may receive incentives for solar cells."
    )
    assert _document_role_candidate_detail(
        text, re.compile(r"\bsolar cells?\b", re.I)
    ) is None


def test_business_model_alias_does_not_replace_the_product_domain():
    terms = candidate_terms("EMS / Contract Manufacturing")
    assert "EMS" in terms
    assert "Contract Manufacturing" not in terms
    assert "Manufacturing" not in terms


def test_exact_repeated_operating_proof_is_promoted_without_manual_approval():
    result = automatic_role_adjudication(
        role_type="MANUFACTURER", role_state="EVIDENCED", link_type="EXACT",
        physical_count=2, pipeline_count=0, earnings_capture_count=0,
    )
    assert result["state"] == "OPERATING_PRODUCER_EVIDENCED"
    assert result["producer_eligible"] is True
    assert "same-product revenue" in result["missing_evidence"][0]


def test_same_product_commercial_capture_has_a_separate_machine_state():
    result = automatic_role_adjudication(
        role_type="MANUFACTURER", role_state="EVIDENCED", link_type="EXACT",
        physical_count=3, pipeline_count=0, earnings_capture_count=2,
    )
    assert result["state"] == "EARNINGS_CAPTURE_EVIDENCED"
    assert result["producer_eligible"] is True


def test_repeated_direct_commercial_supplier_can_be_promoted_without_being_called_manufacturer():
    text = "Key Orders Booked: 400 kV 315 MVA power transformer"
    assert classify(text, "power transformer") == ("DIRECT_PRODUCT_SUPPLIER", False)
    result = automatic_role_adjudication(
        role_type="DIRECT_PRODUCT_SUPPLIER", role_state="EVIDENCED", link_type="EXACT",
        physical_count=0, pipeline_count=0, earnings_capture_count=2,
    )
    assert result["state"] == "EARNINGS_CAPTURE_EVIDENCED"
    assert result["producer_eligible"] is True
    assert "outsourced supply" in result["missing_evidence"][0]


def test_product_revenue_mix_is_direct_commercial_capture():
    text = "The Company reported that revenues comprised 158 MW of solar cells."
    assert classify(text, "solar cells") == ("DIRECT_PRODUCT_SUPPLIER", False)


def test_repeated_direct_supply_without_capture_stays_research_only():
    result = automatic_role_adjudication(
        role_type="DIRECT_PRODUCT_SUPPLIER", role_state="EVIDENCED", link_type="EXACT",
        physical_count=0, pipeline_count=0, earnings_capture_count=0,
    )
    assert result["state"] == "EXACT_ROLE_EVIDENCED"
    assert result["producer_eligible"] is False
    assert "orders" in result["missing_evidence"][0]


def test_issuer_supply_of_exact_product_is_not_mislabeled_as_input_supplier():
    assert classify(
        "We supply power transformers to transmission utilities.", "power transformers"
    ) == ("DIRECT_PRODUCT_SUPPLIER", False)


def test_market_order_commentary_is_not_direct_commercial_supplier_proof():
    text = "Industry customers reported a growing order book for power transformers."
    assert classify(text, "power transformers") == (None, False)


def test_buyer_procurement_context_is_not_supplier_earnings_capture():
    text = "Our EPC order book requires solar modules to be procured locally."
    assert classify(text, "solar modules")[0] != "DIRECT_PRODUCT_SUPPLIER"


def test_input_price_context_is_not_supplier_earnings_capture():
    text = "Orders won: new contracts now factor higher solar module prices."
    assert classify(text, "solar module")[0] != "DIRECT_PRODUCT_SUPPLIER"


def test_end_market_order_book_does_not_make_the_issuer_a_product_supplier():
    text = (
        "Our order book comprises engineering components catering to industries "
        "like electric locomotives."
    )
    assert classify(text, "electric locomotives") == (None, False)


def test_product_maintenance_order_is_not_a_product_supply_order():
    text = "We secured the first order for transformer maintenance using our robot."
    assert classify(text, "transformer") == (None, False)


def test_short_product_head_requires_an_external_ticker_capability_gate():
    vocabulary = {
        "power transformer": "Power Transformer",
        "electric transformer static converter and inductor":
            "Electric transformers, static converters and inductors",
        "solar cell": "Solar Cell",
        "battery cell": "Battery Cell",
    }
    owners = _derived_term_owners(vocabulary)
    assert owners["transformer"] == "power transformer"
    assert "cell" not in owners
    window = "Key Orders Booked: 400 kV transformer"
    anchor = window.casefold().index("key orders")
    assert _phrases_in_window(window, {"power transformer": "power transformer"}, anchor) == []
    assert _phrases_in_window(
        window, {"power transformer": "power transformer", "transformer": "power transformer"}, anchor
    ) == [("power transformer", window.casefold().index("transformer"), len(window))]


def test_gated_short_head_does_not_absorb_a_foreign_product_subtype():
    text = "We supply instrument transformers to export customers."
    start = text.casefold().index("transformers")
    assert _short_term_modifier_conflict(
        text, start, start + len("transformers"), "power transformer"
    ) is True
    generic = "We booked orders for our transformers."
    generic_start = generic.casefold().index("transformers")
    assert _short_term_modifier_conflict(
        generic, generic_start, generic_start + len("transformers"), "power transformer"
    ) is False


def test_commercial_heading_is_attached_to_each_child_order_bullet():
    text = (
        "Order Intake Q2 Key Orders booked during Q2 "
        "• GIS package for a utility "
        "• 500 MVA 765KV Power Transformers from a grid customer "
        "• Service contract"
    )
    windows = _role_windows(text)
    transformer_windows = [window for window, _ in windows if "Power Transformers" in window]
    assert transformer_windows
    assert any("Key Orders booked" in window for window in transformer_windows)


def test_commercial_child_window_never_inherits_prior_slide_product():
    text = (
        "Commissioned transformers at the prior project\n\n"
        "Order Intake Q4 • 400 kV switchyard package • GIS bay extension"
    )
    order_windows = [window for window, _ in _role_windows(text) if "Order Intake:" in window]
    assert order_windows
    assert all("transformers" not in window for window in order_windows)


def test_exact_pipeline_is_promoted_but_not_called_operating():
    result = automatic_role_adjudication(
        role_type="DIRECT_ROLE_UNCLASSIFIED", role_state="EVIDENCED", link_type="EXACT",
        physical_count=0, pipeline_count=2, earnings_capture_count=0,
    )
    assert result["state"] == "PIPELINE_EVIDENCED"
    assert result["producer_eligible"] is True
    assert "commissioning" in result["missing_evidence"][0]


def test_exact_epc_role_is_automatically_rejected_from_producer_universe():
    result = automatic_role_adjudication(
        role_type="EPC_OR_INSTALLER", role_state="EVIDENCED", link_type="EXACT",
        physical_count=0, pipeline_count=0, earnings_capture_count=2,
    )
    assert result["state"] == "AUTO_REJECTED_WRONG_ROLE"
    assert result["producer_eligible"] is False


def test_unlinked_role_is_quarantined_instead_of_silently_promoted():
    result = automatic_role_adjudication(
        role_type="MANUFACTURER", role_state="EVIDENCED", link_type="UNLINKED",
        physical_count=2, pipeline_count=0, earnings_capture_count=2,
    )
    assert result["state"] == "QUARANTINED_UNLINKED_PRODUCT"
    assert result["producer_eligible"] is False


def test_literal_product_to_self_constraint_identity_needs_no_manual_crosswalk():
    key, scope = _link("precision irrigation pump", {
        "status": "AUTO_DISCOVERY",
        "match_scope": "EXACT",
        "constraint_key": "discovery_precision_irrigation_pump",
    })
    assert key == "discovery_precision_irrigation_pump"
    assert scope == "EXACT"


def test_auto_canonical_never_upgrades_an_adjacent_scope():
    key, scope = _link("precision irrigation pump", {
        "status": "AUTO_DISCOVERY",
        "match_scope": "FAMILY",
        "constraint_key": "family_chain",
    })
    assert key is None
    assert scope == "UNLINKED"


def test_reviewed_family_theme_keeps_exact_literal_product_identity():
    key, scope = _link("power transformer", {
        "status": "REVIEWED",
        "match_scope": "FAMILY",
        "constraint_key": "power_grid_equipment",
    })
    assert key == "discovery_power_transformer"
    assert scope == "EXACT_PRODUCT"


def test_exact_product_role_can_identify_producer_without_proving_theme_constraint():
    result = automatic_role_adjudication(
        role_type="MANUFACTURER", role_state="EVIDENCED",
        link_type="EXACT_PRODUCT", physical_count=2,
        pipeline_count=0, earnings_capture_count=0,
    )
    assert result["state"] == "OPERATING_PRODUCER_EVIDENCED"
    assert result["producer_eligible"] is True
