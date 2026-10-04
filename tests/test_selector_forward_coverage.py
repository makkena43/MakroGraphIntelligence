#!/usr/bin/env python3
"""Regression tests for the selector replay's all-zero coverage alarm."""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import patch


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "research"))

from selector_forward_test import (  # noqa: E402
    _actionable_coverage_alarm,
    _edge_assessment,
    _price_return_from_rows,
    _price_returns,
)


def anchor(day: str, core: list[str], early: list[str],
           discovery: list[str] | None = None) -> dict:
    return {"as_of_date": day, "section_tickers": {
        "core_buy": core, "early_timing": early,
        "discovery_starter": discovery or [],
    }}


def test_all_zero_is_an_alarm_not_a_target_pick_count():
    result = _actionable_coverage_alarm([
        anchor("2020-12-31", [], []),
        anchor("2021-12-31", [], []),
    ])
    assert result["status"] == "ALARM_NO_ACTIONABLE_COVERAGE"
    assert result["does_not_set_pick_target"] is True


def test_partial_coverage_is_a_watch_not_an_alarm():
    result = _actionable_coverage_alarm([
        anchor("2020-12-31", [], ["EARLY"]),
        anchor("2021-12-31", [], []),
    ])
    assert result["status"] == "WATCH_PARTIAL_ACTIONABLE_COVERAGE"
    assert result["lane_candidate_totals"] == {
        "core_buy": 0, "early_timing": 1, "discovery_starter": 0,
    }


def test_discovery_starter_counts_as_position_authority_coverage():
    result = _actionable_coverage_alarm([
        anchor("2020-12-31", [], [], ["DISCOVERY"]),
    ])
    assert result["status"] == "PASS_ACTIONABLE_COVERAGE"


def test_every_anchor_covered_passes():
    result = _actionable_coverage_alarm([
        anchor("2020-12-31", ["CORE"], []),
        anchor("2021-12-31", [], ["EARLY"]),
    ])
    assert result["status"] == "PASS_ACTIONABLE_COVERAGE"


def test_development_replay_never_claims_validated_edge():
    pooled = {
        "12m": {"sections": {"final_actionable": {
            "n_measured": 100, "median_alpha_pct": 50,
            "beat_benchmark_pct": 90, "median_return_pct": 70,
            "multibagger_pct": 30, "severe_loss_pct": 0,
        }}},
        "24m": {"sections": {"final_actionable": {
            "n_measured": 100, "median_alpha_pct": 80,
            "beat_benchmark_pct": 90, "median_return_pct": 100,
            "multibagger_pct": 50, "severe_loss_pct": 0,
        }}},
    }
    result = _edge_assessment(
        pooled, {"OFFICIAL_TOTAL_RETURN_INDEX"},
        evaluation_role="development", frozen_at=None,
    )
    assert result["status"] == "DEVELOPMENT_REPLAY_NOT_OUT_OF_SAMPLE"


def test_batched_price_query_preserves_exchange_adjusted_return_method():
    # The 50 reported prev_close versus the preceding 100 is a split factor.
    # The final close must be adjusted by that same factor: 60 / 0.5 / 100 - 1.
    rows = [
        {"symbol": "TEST", "trade_date": date(2022, 1, 3), "open": 100, "close": 100, "prev_close": 100},
        {"symbol": "TEST", "trade_date": date(2022, 1, 4), "open": 52, "close": 52, "prev_close": 50},
        {"symbol": "TEST", "trade_date": date(2022, 1, 5), "open": 60, "close": 60, "prev_close": 52},
    ]
    with patch("selector_forward_test.q", return_value=rows):
        result = _price_returns(None, ["TEST"], date(2022, 1, 3), date(2022, 1, 5))["TEST"]
    assert result["status"] == "measured_exchange_adjusted"
    assert result["return_pct"] == 20.0


def test_later_listing_or_missing_alias_is_not_credited_to_anchor_cohort():
    rows = [
        {"symbol": "LATE", "trade_date": date(2024, 1, 2), "close": 100, "prev_close": 100},
        {"symbol": "LATE", "trade_date": date(2024, 6, 28), "close": 200, "prev_close": 199},
    ]
    result = _price_return_from_rows(
        "LATE", date(2022, 12, 31), date(2024, 6, 30), rows,
    )
    assert result["status"] == "excluded_not_tradable_at_anchor"
    assert result["first_available_date"] == "2024-01-02"


def test_stale_last_price_is_not_treated_as_horizon_exit():
    rows = [
        {"symbol": "STALE", "trade_date": date(2023, 1, 2), "close": 100, "prev_close": 100},
        {"symbol": "STALE", "trade_date": date(2023, 6, 1), "close": 150, "prev_close": 149},
    ]
    result = _price_return_from_rows(
        "STALE", date(2022, 12, 31), date(2023, 12, 31), rows,
    )
    assert result["status"] == "excluded_no_price_near_horizon"
    assert result["last_available_date"] == "2023-06-01"


def test_simple_integer_corporate_action_is_adjusted_when_prev_close_is_stale():
    rows = [
        {"symbol": "BONUS", "trade_date": date(2023, 1, 2), "open": 100, "close": 100, "prev_close": 100},
        {"symbol": "BONUS", "trade_date": date(2023, 5, 31), "open": 100, "close": 100, "prev_close": 100},
        # A 4-for-1 bonus creates five shares: theoretical factor 1/5.
        # The observed 21 close includes a normal +5% market move.
        {"symbol": "BONUS", "trade_date": date(2023, 6, 1), "open": 20, "close": 21, "prev_close": 100},
        {"symbol": "BONUS", "trade_date": date(2023, 12, 29), "open": 30, "close": 30, "prev_close": 29},
    ]
    result = _price_return_from_rows(
        "BONUS", date(2022, 12, 31), date(2023, 12, 31), rows,
    )
    assert result["status"] == "measured_exchange_adjusted"
    assert result["return_pct"] == 50.0
    assert result["exchange_adjustments"][0]["inferred_ex_date_open_factor"] == 0.2


if __name__ == "__main__":
    test_all_zero_is_an_alarm_not_a_target_pick_count()
    test_partial_coverage_is_a_watch_not_an_alarm()
    test_every_anchor_covered_passes()
    test_batched_price_query_preserves_exchange_adjusted_return_method()
