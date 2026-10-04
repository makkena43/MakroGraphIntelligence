"""Tests for generic opportunity-mechanism separation."""

import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "stock_report"))

from investment_mechanisms import build_investment_mechanisms  # noqa: E402


def _candidate(**overrides):
    base = {
        "ticker": "ABC", "company": "Example Co", "corroborated_maker": True,
        "types": ["direct_supplier"], "corroborated_products": ["Example Component"],
        "order_book": True, "capex_signals": 0, "risk_tier": "NORMAL",
        "rank": 4, "composite_score": 0.7, "any_demand_side": False,
    }
    return {**base, **overrides}


def _constraint(**overrides):
    base = {
        "constraint": "Example Component", "themes": ["Example theme"],
        "mapping_current": True,
        "constraint_quality": {
            "grade": "B", "import_dependence": True,
            "hard_resupply_barrier": True,
        },
    }
    return {**base, **overrides}


def test_import_substitution_is_a_distinct_non_buy_research_lane():
    output = build_investment_mechanisms(
        [_candidate()], [_constraint()], None, {}, date(2022, 12, 31),
    )
    row = output["localisation_or_qualification"][0]
    assert row["mechanism"] == "LOCALISATION_OR_QUALIFICATION"
    assert row["investment_status"] == "research lead — do not add as a Buy"
    assert "product-level revenue, margin, or order-conversion evidence" in row["missing_proof"]


def test_policy_reference_does_not_become_a_buy():
    policy = {"qualified": {"Example scheme": [{
        "ticker": "ABC", "commitment": {"n_commit_docs": 1},
        "n_last12m": 1, "first_mention": date(2022, 8, 1),
    }]}}
    output = build_investment_mechanisms(
        [_candidate()], [], policy, {}, date(2022, 12, 31),
    )
    row = output["policy_led_deployment_demand"][0]
    assert row["mechanism"] == "POLICY_LED_DEPLOYMENT_DEMAND"
    assert row["investment_status"] == "research lead — policy participation is not a Buy"


def test_physical_core_decision_is_preserved_not_re_ranked():
    final = {"priorities": [{
        "ticker": "ABC", "company": "Example Co", "decision_grade_products": ["Example Component"],
        "risk_tier": "NORMAL", "raw_screen_rank": 2, "raw_composite_score": 0.8,
    }]}
    output = build_investment_mechanisms(
        [_candidate()], [_constraint()], None, final, date(2022, 12, 31),
    )
    row = output["physical_constraint_rent"][0]
    assert row["mechanism"] == "PHYSICAL_CONSTRAINT_RENT"
    assert row["investment_status"].startswith("investment-committee candidate")


def test_policy_snapshot_name_outside_raw_mapper_is_kept_as_research_context():
    policy = {"qualified": {"Example scheme": [{
        "ticker": "NEWCO", "commitment": {"n_commit_docs": 2},
        "n_last12m": 2, "first_mention": date(2022, 8, 1),
    }]}}
    context = {
        "NEWCO": {
            "ticker": "NEWCO", "company": "New Co", "issuer_role_evidenced": True,
            "issuer_role_requires_review": True, "risk_tier": "UNASSESSED",
            "order_book": False, "capex_signals": 0, "types": [],
            "corroborated_maker": False, "any_demand_side": False,
        }
    }
    output = build_investment_mechanisms(
        [], [], policy, {}, date(2022, 12, 31), policy_company_context=context,
    )
    row = output["policy_led_deployment_demand"][0]
    assert row["ticker"] == "NEWCO"
    assert row["investment_status"] == "research lead — policy participation is not a Buy"
    assert "independent reviewer confirmation of the issuer-product manufacturing role" in row["missing_proof"]


def test_unmapped_policy_company_is_a_triage_lead_not_silently_dropped():
    policy = {"qualified": {"Example scheme": [{
        "ticker": "UNMAPPED", "commitment": {"n_commit_docs": 2},
        "n_last12m": 2, "first_mention": date(2022, 8, 1),
    }]}}
    output = build_investment_mechanisms(
        [], [], policy, {}, date(2022, 12, 31),
    )
    row = output["policy_led_deployment_demand"][0]
    assert row["ticker"] == "UNMAPPED"
    assert row["role_status"] == "role proof incomplete"
    assert "corroborated direct/critical supply-side company role" in row["missing_proof"]
    assert "as-of governance/disclosure risk review" in row["missing_proof"]
