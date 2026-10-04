#!/usr/bin/env python3
"""Derive point-in-time constraint snapshots from immutable accepted events.

No date is refreshed merely because this command runs. Every measurement keeps
its original economic, publication and availability dates, and expires under
the observation-type TTL in ``src.makrograph.constraint_contract``. Policy and
trade-momentum observations remain research context and cannot create a
physical state.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2.extras

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_constraint_ledger import connect, ensure_schema  # noqa: E402
from src.makrograph.constraint_contract import (  # noqa: E402
    derive_constraint_state,
    latest_observation_revisions,
    observation_is_current,
    observation_ttl_days,
    nonphysical_observation_admissibility,
    physical_observation_admissibility,
)


DERIVATION_METHOD = "EVENT_SOURCED_CONSTRAINT_V1"


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ratio(value: Any, *, percent: bool = False) -> float | None:
    number = _number(value)
    if number is None:
        return None
    if percent or number > 1:
        number /= 100.0
    return max(0.0, min(1.0, number))


def fetch_events(cur, country: str, as_of: date) -> list[dict]:
    cur.execute("""
        SELECT id, country, constraint_key, product_label, observation_type,
               observed_at, economic_period_start, economic_period_end,
               published_at, available_at, source_table, source_row_id,
               source_key, source_family, source_hash, source_url, source_title,
               product_scope, revision, metrics, provenance_status, review_status
        FROM mg_constraint_observation_queue
        WHERE country=%s AND observed_at <= %s
          AND COALESCE(available_at, published_at, observed_at) <= %s
        ORDER BY constraint_key, observed_at, revision, id
    """, (country, as_of, as_of))
    latest = latest_observation_revisions(dict(result) for result in cur.fetchall())
    # A newer pending/rejected revision conservatively supersedes the older
    # accepted fact.  It must be accepted itself before state is rebuilt.
    return sorted(
        (row for row in latest
         if row.get("review_status") == "ACCEPTED" and
         row.get("provenance_status") == "PRIMARY_SOURCE"),
        key=lambda row: (row["constraint_key"], row["observed_at"], row["id"]),
    )


def derive_snapshot(events: list[dict], as_of: date) -> dict | None:
    if not events:
        return None
    current = [event for event in events if observation_is_current(
        event.get("observation_type"), event.get("observed_at"), as_of
    )]
    physical: list[dict] = []
    rejected_physical: list[dict] = []
    for event in events:
        if event.get("observation_type") not in {"CAPACITY", "IMPORT"}:
            continue
        admitted, reason = physical_observation_admissibility(event, as_of)
        if admitted:
            physical.append(event)
        else:
            rejected_physical.append({"id": event.get("id"), "reason": reason})

    state_events = []
    for event in current:
        if event.get("observation_type") not in {"DEMAND", "LEAD_TIME", "COMMISSIONING"}:
            continue
        admitted, _ = nonphysical_observation_admissibility(event, as_of)
        if admitted:
            state_events.append(event)
    admissible_events = state_events + physical

    demand_events = [event for event in state_events if event.get("observation_type") == "DEMAND"]
    lead_events = [event for event in state_events if event.get("observation_type") == "LEAD_TIME"]
    commissioning = [event for event in state_events if event.get("observation_type") == "COMMISSIONING"]
    import_barriers = [
        event for event in physical
        if event.get("observation_type") == "IMPORT"
        and int((event.get("metrics") or {}).get("substitution_horizon_years") or 0) >= 3
    ]
    resolution_states = {
        str((event.get("metrics") or {}).get("resolution_state") or "").upper()
        for event in commissioning
    }
    overcapacity = bool(resolution_states & {"OVERCAPACITY", "SUPPLY_EXCEEDS_DEMAND"})
    resolved = bool(resolution_states & {"RESOLVED", "BALANCED"})
    state_axes = derive_constraint_state({
        "measured": bool(physical),
        "demand": bool(demand_events),
        "binding": bool(lead_events),
        "barrier": bool(lead_events or import_barriers),
        "resolving": bool(commissioning),
        "resolved": resolved,
        "overcapacity": overcapacity,
        "stale": bool(events and not current),
    })

    latest_capacity = next((event for event in reversed(physical)
                            if event.get("observation_type") == "CAPACITY"), None)
    latest_import = next((event for event in reversed(physical)
                          if event.get("observation_type") == "IMPORT"), None)
    capacity_metrics = (latest_capacity or {}).get("metrics") or {}
    import_metrics = (latest_import or {}).get("metrics") or {}
    measurement_dates = [event["observed_at"] for event in physical]
    measurement_date = max(measurement_dates) if measurement_dates else None

    legacy_state = state_axes["physical_state"]
    if state_axes["trajectory"] == "RESOLVING" and legacy_state not in {"RESOLVED", "OVERCAPACITY"}:
        legacy_state = "RESOLVING"
    if legacy_state == "STALE":
        classification = "WATCH"
    elif physical:
        classification = "PHYSICAL_CONSTRAINT"
    elif demand_events:
        classification = "DEMAND_THEME"
    else:
        classification = "WATCH"

    expiry_dates = [
        event["observed_at"] + timedelta(days=observation_ttl_days(event["observation_type"]))
        for event in current
    ]
    next_validation = min(expiry_dates) if expiry_dates else as_of
    source_families = {
        event.get("source_family") or event.get("source_key")
        for event in current if event.get("source_family") or event.get("source_key")
    }
    evidence_dates = {event["observed_at"] for event in current}
    product_label = events[-1]["product_label"]
    return {
        "constraint_key": events[-1]["constraint_key"],
        "constraint_name": product_label,
        "as_of_date": as_of,
        "state": legacy_state,
        "physical_state": state_axes["physical_state"],
        "trajectory": state_axes["trajectory"],
        "evidence_completeness": state_axes["evidence_completeness"],
        "mechanism": (
            "PHYSICAL_CONSTRAINT" if physical and lead_events else
            "LOCALISATION_QUALIFICATION" if latest_import else
            "DEPLOYMENT_DEMAND_PULL" if demand_events else
            "UNCLASSIFIED_RESEARCH"
        ),
        "classification": classification,
        "product_scope": "EXACT_CHAIN",
        "investment_eligibility": "RESEARCH_ONLY",
        "import_dependency_ratio": _ratio(import_metrics.get("import_share")),
        "measurement_date": measurement_date,
        "import_value": _number(import_metrics.get("import_value_bn_usd")),
        "import_value_unit": "USD bn" if import_metrics.get("import_value_bn_usd") is not None else None,
        "primary_origin": (
            ",".join(import_metrics.get("primary_origin"))
            if isinstance(import_metrics.get("primary_origin"), list)
            else import_metrics.get("primary_origin")
        ),
        "primary_origin_ratio": _ratio(import_metrics.get("primary_origin_ratio")),
        "capacity_gap_ratio": _ratio(capacity_metrics.get("gap_pct"), percent=True),
        "domestic_capacity": _number(capacity_metrics.get("domestic_capacity")),
        "capacity_unit": capacity_metrics.get("unit"),
        "demand_volume": _number(capacity_metrics.get("required_quantity")),
        "demand_unit": capacity_metrics.get("unit"),
        "binding_demand_status": (
            "CONFIRMED" if demand_events and lead_events else
            "INDICATED" if demand_events else "UNPROVED"
        ),
        "resupply_barrier_status": "CONFIRMED" if (lead_events or import_barriers) else "UNPROVED",
        "resolution_status": (
            "RESOLVED" if resolved or overcapacity else
            "RESOLVING" if commissioning else
            "STILL_BINDING" if lead_events else "UNKNOWN"
        ),
        "next_validation_date": next_validation,
        "summary": (
            f"Event-sourced {product_label} snapshot: {len(physical)} current physical, "
            f"{len(demand_events)} demand, {len(lead_events)} binding/barrier and "
            f"{len(commissioning)} commissioning observations."
        ),
        "review_note": (
            f"Derived only from accepted point-in-time observations; "
            f"{len(source_families)} independent source families across "
            f"{len(evidence_dates)} dates. Rejected physical rows: {rejected_physical}."
        ),
        "derivation_method": DERIVATION_METHOD,
        "events": sorted(admissible_events, key=lambda event: (event["observed_at"], event["id"])),
    }


def _evidence_items(event: dict) -> list[tuple[str, float | None, str | None]]:
    metrics = event.get("metrics") or {}
    kind = event["observation_type"]
    if kind == "CAPACITY":
        return [("SUPPLY", _ratio(metrics.get("gap_pct"), percent=True), "capacity gap ratio")]
    if kind == "IMPORT":
        return [("IMPORT", _ratio(metrics.get("import_share")), "import dependency ratio")]
    if kind == "DEMAND":
        return [("DEMAND", _number(metrics.get("required_quantity") or metrics.get("demand")), metrics.get("unit"))]
    if kind == "LEAD_TIME":
        return [("BINDING", _number(metrics.get("lead_time_months")), "months"),
                ("BARRIER", _number(metrics.get("lead_time_months")), "months")]
    if kind == "COMMISSIONING":
        return [("RESOLUTION", _number(metrics.get("commissioned_capacity")), metrics.get("unit"))]
    return []


def persist_snapshot(cur, country: str, snapshot: dict) -> tuple[int, int, bool]:
    cur.execute("""
        SELECT l.id, l.derivation_method, l.state, l.classification,
               EXISTS (
                   SELECT 1 FROM mg_constraint_ledger_evidence e
                   WHERE e.ledger_id=l.id AND e.is_primary
                     AND COALESCE(e.admissibility_status, 'ADMISSIBLE')='ADMISSIBLE'
               ) AS has_admissible_evidence
        FROM mg_constraint_ledgers l
        WHERE l.country=%s AND l.constraint_key=%s AND l.as_of_date=%s
    """, (country, snapshot["constraint_key"], snapshot["as_of_date"]))
    existing = cur.fetchone()
    if existing:
        replaceable_placeholder = bool(
            existing.get("state") == "DISCOVERY" and
            existing.get("classification") == "WATCH" and
            not existing.get("has_admissible_evidence")
        )
        if (existing.get("derivation_method") not in {
                DERIVATION_METHOD, "QUARANTINED_SYNTHETIC", "COVERAGE_PLACEHOLDER",
        } and not replaceable_placeholder):
            return 0, 0, True

    columns = (
        "country", "constraint_key", "constraint_name", "as_of_date", "state",
        "physical_state", "trajectory", "evidence_completeness", "mechanism",
        "classification", "product_scope", "investment_eligibility",
        "import_dependency_ratio", "measurement_date", "import_value",
        "import_value_unit", "primary_origin", "primary_origin_ratio",
        "capacity_gap_ratio", "domestic_capacity", "capacity_unit", "demand_volume",
        "demand_unit", "binding_demand_status", "resupply_barrier_status",
        "resolution_status", "next_validation_date", "summary", "review_note",
        "derivation_method",
    )
    payload = {"country": country, **snapshot}
    values = ", ".join(f"%({column})s" for column in columns)
    updates = ", ".join(
        f"{column}=EXCLUDED.{column}" for column in columns
        if column not in {"country", "constraint_key", "as_of_date"}
    )
    cur.execute(f"""
        INSERT INTO mg_constraint_ledgers ({', '.join(columns)})
        VALUES ({values})
        ON CONFLICT (country, constraint_key, as_of_date)
        DO UPDATE SET {updates}, updated_at=NOW()
        RETURNING id
    """, payload)
    ledger_id = cur.fetchone()["id"]
    evidence_count = 0
    active_observation_ids = [event["id"] for event in snapshot["events"]]
    cur.execute("""
        UPDATE mg_constraint_ledger_evidence
        SET admissibility_status='QUARANTINED',
            quarantine_reason='source observation no longer accepted/current in rebuilt snapshot'
        WHERE ledger_id=%s AND observation_id IS NOT NULL
          AND NOT (observation_id = ANY(%s))
    """, (ledger_id, active_observation_ids or [0]))
    for event in snapshot["events"]:
        for evidence_type, value, unit in _evidence_items(event):
            cur.execute("""
                INSERT INTO mg_constraint_ledger_evidence
                  (ledger_id, observation_id, evidence_type, source_date,
                   published_at, available_at, source_url, source_title,
                   source_publisher, claim, value_numeric, value_unit, is_primary,
                   independence_key, source_hash, admissibility_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s,'ADMISSIBLE')
                ON CONFLICT (ledger_id, evidence_type, source_date, source_url)
                DO UPDATE SET observation_id=EXCLUDED.observation_id,
                              published_at=EXCLUDED.published_at,
                              available_at=EXCLUDED.available_at,
                              source_title=EXCLUDED.source_title,
                              claim=EXCLUDED.claim,
                              value_numeric=EXCLUDED.value_numeric,
                              value_unit=EXCLUDED.value_unit,
                              is_primary=TRUE,
                              independence_key=EXCLUDED.independence_key,
                              source_hash=EXCLUDED.source_hash,
                              admissibility_status='ADMISSIBLE',
                              quarantine_reason=NULL
            """, (
                ledger_id, event["id"], evidence_type, event["observed_at"],
                event.get("published_at") or event["observed_at"],
                event.get("available_at") or event.get("published_at") or event["observed_at"],
                event["source_url"], event.get("source_title") or event["source_key"],
                event.get("source_table"),
                (event.get("metrics") or {}).get("excerpt") or
                f"Accepted {event['observation_type']} observation for {event['product_label']}",
                value, unit, event.get("source_family") or event["source_key"],
                event.get("source_hash"),
            ))
            evidence_count += 1
    return 1, evidence_count, False


def materialize(cur, country: str, as_of: date) -> dict[str, int]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in fetch_events(cur, country, as_of):
        grouped[event["constraint_key"]].append(event)
    stats = {"constraints_seen": len(grouped), "snapshots_written": 0,
             "evidence_written": 0, "reviewed_snapshots_preserved": 0}
    for events in grouped.values():
        snapshot = derive_snapshot(events, as_of)
        if not snapshot:
            continue
        ledgers, evidence, preserved = persist_snapshot(cur, country, snapshot)
        stats["snapshots_written"] += ledgers
        stats["evidence_written"] += evidence
        stats["reviewed_snapshots_preserved"] += int(preserved)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="IN")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    as_of = parse_date(args.as_of)
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_schema(cur)
            stats = materialize(cur, args.country.upper(), as_of)
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
    print(("Would materialize" if args.dry_run else "Materialized"), stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
