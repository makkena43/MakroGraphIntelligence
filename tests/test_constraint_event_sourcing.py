"""Pure regression tests for event-sourced constraint-state derivation."""

import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "policy"))

from materialize_constraint_ledger import derive_snapshot  # noqa: E402
from src.makrograph.constraint_contract import latest_observation_revisions  # noqa: E402


def _event(event_id, kind, observed, metrics):
    return {
        "id": event_id,
        "country": "IN",
        "constraint_key": "solar_cells",
        "product_label": "Solar Cell",
        "observation_type": kind,
        "observed_at": observed,
        "published_at": observed,
        "available_at": observed,
        "source_table": "official_source",
        "source_key": f"official:{event_id}",
        "source_family": f"official:{event_id}",
        "source_hash": f"hash-{event_id}",
        "source_url": f"https://example.gov/{event_id}",
        "source_title": f"Official source {event_id}",
        "product_scope": "EXACT",
        "metrics": metrics,
        "provenance_status": "PRIMARY_SOURCE",
        "review_status": "ACCEPTED",
    }


def test_carry_forward_retains_measurement_date_and_expires():
    capacity = _event(1, "CAPACITY", date(2022, 1, 1), {
        "measurement_basis": "CURRENT_SUPPLY_DEMAND",
        "gap_pct": 50,
        "numerator": 5,
        "denominator": 10,
        "domestic_capacity": 5,
        "required_quantity": 10,
        "unit": "GW",
    })
    current = derive_snapshot([capacity], date(2022, 12, 31))
    stale = derive_snapshot([capacity], date(2024, 12, 31))
    assert current["measurement_date"] == date(2022, 1, 1)
    assert current["physical_state"] == "MEASURED"
    assert stale["measurement_date"] is None
    assert stale["physical_state"] == "STALE"


def test_policy_target_capacity_row_stays_unmeasured():
    future_gap = _event(2, "CAPACITY", date(2022, 6, 1), {
        "measurement_basis": "FUTURE_POLICY_TARGET",
        "gap_pct": 90,
        "numerator": 10,
        "denominator": 100,
    })
    snapshot = derive_snapshot([future_gap], date(2022, 12, 31))
    assert snapshot["physical_state"] == "DISCOVERY"
    assert snapshot["capacity_gap_ratio"] is None
    assert snapshot["events"] == []


def test_commissioning_changes_trajectory_without_refreshing_measurement():
    capacity = _event(1, "CAPACITY", date(2022, 9, 1), {
        "measurement_basis": "CURRENT_SUPPLY_DEMAND",
        "gap_pct": 40,
        "numerator": 6,
        "denominator": 10,
        "domestic_capacity": 6,
        "required_quantity": 10,
        "unit": "GW",
    })
    commissioning = _event(2, "COMMISSIONING", date(2023, 3, 1), {
        "commissioned_capacity": 2,
        "unit": "GW",
    })
    snapshot = derive_snapshot([capacity, commissioning], date(2023, 6, 30))
    assert snapshot["measurement_date"] == date(2022, 9, 1)
    assert snapshot["trajectory"] == "RESOLVING"
    assert snapshot["state"] == "RESOLVING"


def test_future_policy_demand_does_not_create_binding_state():
    demand = _event(3, "DEMAND", date(2022, 6, 1), {
        "measurement_basis": "FUTURE_POLICY_TARGET",
        "required_quantity": 100,
    })
    lead = _event(4, "LEAD_TIME", date(2022, 7, 15), {
        "source_reported_pressure": True,
    })
    snapshot = derive_snapshot([demand, lead], date(2022, 12, 31))
    assert snapshot["physical_state"] == "EVIDENCED"
    assert snapshot["binding_demand_status"] == "UNPROVED"


def test_current_demand_and_lead_time_create_binding_context_not_measurement():
    demand = _event(5, "DEMAND", date(2022, 6, 1), {
        "measurement_basis": "SOURCE_REPORTED_CURRENT_DEMAND",
        "source_reported_demand": True,
    })
    lead = _event(6, "LEAD_TIME", date(2022, 7, 15), {
        "source_reported_pressure": True,
    })
    snapshot = derive_snapshot([demand, lead], date(2022, 12, 31))
    assert snapshot["physical_state"] == "EVIDENCED"
    assert snapshot["binding_demand_status"] == "CONFIRMED"
    assert snapshot["measurement_date"] is None


def test_newer_pending_revision_supersedes_accepted_fact():
    accepted = _event(7, "CAPACITY", date(2022, 6, 1), {
        "measurement_basis": "CURRENT_SUPPLY_DEMAND",
        "gap_pct": 40,
        "numerator": 6,
        "denominator": 10,
    })
    accepted.update({"source_key": "official:capacity|revision=1", "revision": 1})
    correction = {
        **accepted,
        "id": 8,
        "source_key": "official:capacity|revision=2",
        "revision": 2,
        "review_status": "PENDING_REVIEW",
    }
    latest = latest_observation_revisions([accepted, correction])
    assert [row["id"] for row in latest] == [8]
    assert not [row for row in latest if row["review_status"] == "ACCEPTED"]
