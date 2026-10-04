"""Precision tests for automatic issuer constraint observations."""

import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "policy"))

from queue_constraint_observations import (  # noqa: E402
    COMMISSIONING_RE,
    LEAD_TIME_PRESSURE_RE,
    LEAD_TIME_RE,
    _local_product_signal,
)


def test_exact_local_lead_time_pressure_is_source_ready():
    detail = _local_product_signal(
        "Our power transformer lead time has extended to 18 months due to the order backlog.",
        "Power Transformer",
        LEAD_TIME_RE,
    )
    assert detail is not None
    assert LEAD_TIME_PRESSURE_RE.search(detail["excerpt"])


def test_document_wide_product_and_unrelated_lead_time_are_not_joined():
    text = (
        "Our power transformer portfolio serves utilities.\n\n"
        "Delivery timelines for civil construction have extended to 18 months."
    )
    assert _local_product_signal(text, "Power Transformer", LEAD_TIME_RE) is None


def test_third_party_commissioning_is_not_auto_accepted():
    text = (
        "Industry producers commissioned a new solar cell manufacturing facility "
        "during the year."
    )
    assert _local_product_signal(text, "Solar Cell", COMMISSIONING_RE) is None


def test_issuer_production_commissioning_is_source_ready():
    text = "The Company commenced commercial production at its solar cell facility."
    detail = _local_product_signal(text, "Solar Cell", COMMISSIONING_RE)
    assert detail is not None
    assert "commercial production" in detail["signal"].casefold()
