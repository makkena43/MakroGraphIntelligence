#!/usr/bin/env python3
"""Regression tests for the report-section cohorts in the selector replay."""

from __future__ import annotations

import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "research"))

from selector_forward_test import _section_tickers  # noqa: E402


def _row(ticker: str, **extra) -> dict:
    return {"ticker": ticker, **extra}


def test_every_visible_stock_table_has_its_own_unique_ticker_cohort():
    decision = {
        "final_investable_shortlist": [_row("CORE"), _row("EARLY")],
        "investment_committee_candidate_tickers": ["CORE"],
        "early_timing_tickers": ["EARLY"],
        "policy_research_watchlist": [_row("POLICY"), _row("POLICY")],
        "best_stock_research_queue": [_row("PROOF")],
    }
    pli = {
        "research_leads": [_row("PLI_RESEARCH", constraint_links=["Cell"]), _row("HIDDEN")],
        "monitor_leads": [_row("PLI_MONITOR", constraint_links=["Cell"]), _row("HIDDEN")],
    }
    candidates = [_row("RAW"), _row("RAW")]
    makers = [{
        "verified_current_makers": [_row("OPERATING")],
        "pipeline_or_direct_role_makers": [_row("PIPELINE")],
        "company_populations": {
            "quarantined_exceptions": [_row("QUARANTINED"), _row("QUARANTINED")],
        },
    }]
    discoveries = [_row("DISCOVERY"), _row("DISCOVERY")]
    mechanisms = {
        "localisation_or_qualification": [_row("LOCAL")],
        "policy_led_deployment_demand": [_row("DEPLOYMENT"), _row("DEPLOYMENT")],
    }

    result = _section_tickers(
        decision, pli, candidates, makers, discoveries, mechanisms,
    )

    assert result["policy_research"] == ["POLICY"]
    assert result["final_actionable"] == ["CORE", "EARLY"]
    assert result["localisation_or_qualification"] == ["LOCAL"]
    assert result["policy_led_deployment"] == ["DEPLOYMENT"]
    assert result["maker_operating"] == ["OPERATING"]
    assert result["maker_pipeline_direct_role"] == ["PIPELINE"]
    assert result["maker_quarantined_exception"] == ["QUARANTINED"]
    assert result["pli_research"] == ["PLI_RESEARCH"]
    assert result["pli_monitor"] == ["PLI_MONITOR"]
    assert result["company_product_discovery"] == ["DISCOVERY"]


if __name__ == "__main__":
    test_every_visible_stock_table_has_its_own_unique_ticker_cohort()
