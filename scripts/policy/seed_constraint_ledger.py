#!/usr/bin/env python3
"""Create and seed the first audited, point-in-time constraint ledger.

This is deliberately a small seed, not a claim that every selected chain is
validated.  Each record is a dated primary-source observation from the 2020-22
audit.  A later report sees the record only if its source already existed, and
the selector marks it stale once its review date has passed.

Usage:
    python scripts/policy/seed_constraint_ledger.py
    python scripts/policy/seed_constraint_ledger.py --dry-run
    python scripts/policy/seed_constraint_ledger.py --input reviewed_packet.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

import psycopg2


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "schema" / "postgres_schema.sql"
SCHEMA_MARKER = "-- 25. DATED CONSTRAINT LEDGER  (point-in-time investment research)"


def connect():
    return psycopg2.connect(
        host=os.environ.get("MAKROGRAPH_PG_HOST", "localhost"),
        port=int(os.environ.get("MAKROGRAPH_PG_PORT", "5432")),
        dbname=os.environ.get("MAKROGRAPH_PG_DB", "makrograph"),
        user=os.environ.get("MAKROGRAPH_PG_USER", "postgres"),
        password=os.environ.get("MAKROGRAPH_PG_PASSWORD", ""),
    )


# These are source observations, not ratings.  Exact-chain records can supply
# a physical measurement only while current; family and policy records remain
# discovery context even when current.
SEED_ROWS: tuple[dict, ...] = (
    {
        "constraint_key": "crgo_electrical_steel",
        "constraint_name": "CRGO electrical steel",
        "as_of_date": date(2015, 3, 19),
        "state": "EVIDENCED",
        "classification": "PHYSICAL_CONSTRAINT",
        "product_scope": "EXACT_CHAIN",
        "investment_eligibility": "RESEARCH_ONLY",
        "import_dependency_ratio": None,
        "capacity_gap_ratio": None,
        "domestic_capacity": None,
        "capacity_unit": None,
        "binding_demand_status": "UNPROVED",
        "resupply_barrier_status": "INDICATED",
        "resolution_status": "UNKNOWN",
        "next_validation_date": date(2016, 3, 19),
        "summary": "A primary government disclosure said CRGO steel used in transformers was not manufactured in India and was met through imports from a small global producer base.",
        "review_note": "Require a dated import share or domestic capacity balance before treating CRGO as measured; the initial source is intentionally stale in all 2020+ replays.",
        "evidence": (
            {
                "evidence_type": "SUPPLY",
                "source_date": date(2015, 3, 19),
                "source_url": "https://www.pib.gov.in/newsite/PrintRelease.aspx?relid=117414",
                "source_title": "Shortage of Raw Material in Electrical Equipment Manufacturing Industry",
                "source_publisher": "Press Information Bureau / Ministry of Heavy Industries",
                "claim": "CRGO steel used in transformers was not manufactured in India; the critical input requirement was met through imports from a few global manufacturers.",
                "value_numeric": None,
                "value_unit": None,
                "independence_key": "pib-117414",
            },
        ),
    },
    {
        "constraint_key": "defence_indigenisation",
        "constraint_name": "Defence indigenisation / procurement substitution",
        "as_of_date": date(2020, 8, 9),
        "state": "EVIDENCED",
        "classification": "POLICY_PROCUREMENT",
        "product_scope": "POLICY",
        "investment_eligibility": "RESEARCH_ONLY",
        "import_dependency_ratio": None,
        "capacity_gap_ratio": None,
        "domestic_capacity": None,
        "capacity_unit": None,
        "binding_demand_status": "INDICATED",
        "resupply_barrier_status": "UNPROVED",
        "resolution_status": "UNKNOWN",
        "next_validation_date": date(2021, 2, 9),
        "summary": "The first negative import list created a procurement-substitution research lead, not proof that any listed company manufactured a constrained defence product.",
        "review_note": "Require item-level procurement, domestic capacity and company product evidence before a defence label can be classified as a physical constraint.",
        "evidence": (
            {
                "evidence_type": "POLICY",
                "source_date": date(2020, 8, 9),
                "source_url": "https://www.pib.gov.in/PressReleasePage.aspx?PRID=1644570",
                "source_title": "Defence Ministry announces first negative list for import embargo",
                "source_publisher": "Press Information Bureau / Ministry of Defence",
                "claim": "The Ministry of Defence announced an import embargo list of 101 defence items to be progressively procured from domestic industry.",
                "value_numeric": 101,
                "value_unit": "items",
                "independence_key": "pib-1644570",
            },
        ),
    },
    {
        "constraint_key": "solar_cells",
        "constraint_name": "Solar PV cells and integrated modules",
        "as_of_date": date(2021, 4, 7),
        "state": "EVIDENCED",
        "classification": "PHYSICAL_CONSTRAINT",
        "product_scope": "EXACT_CHAIN",
        "investment_eligibility": "RESEARCH_ONLY",
        "import_dependency_ratio": None,
        "capacity_gap_ratio": None,
        "domestic_capacity": None,
        "capacity_unit": None,
        "binding_demand_status": "INDICATED",
        "resupply_barrier_status": "UNPROVED",
        "resolution_status": "UNKNOWN",
        "next_validation_date": date(2022, 4, 7),
        "summary": "The government recorded limited operational domestic solar-cell/module capacity and import dependence, but no point-in-time import ratio or domestic capacity balance was captured in the seed.",
        "review_note": "Obtain dated cell-level demand, domestic operating capacity, imports and commissioning schedule. Solar wafer/module rows are family context only until separately measured.",
        "evidence": (
            {
                "evidence_type": "IMPORT",
                "source_date": date(2021, 4, 7),
                "source_url": "https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=1710113",
                "source_title": "Cabinet approves PLI scheme for high-efficiency solar PV modules",
                "source_publisher": "Press Information Bureau / Cabinet",
                "claim": "Solar capacity additions largely depended on imported PV cells and modules because domestic operational capacity was limited.",
                "value_numeric": None,
                "value_unit": None,
                "independence_key": "pib-1710113",
            },
            {
                "evidence_type": "POLICY",
                "source_date": date(2021, 4, 7),
                "source_url": "https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=1710113",
                "source_title": "Cabinet approves PLI scheme for high-efficiency solar PV modules",
                "source_publisher": "Press Information Bureau / Cabinet",
                "claim": "The approved programme targeted 10,000 MW of integrated solar PV manufacturing capacity and annual import substitution.",
                "value_numeric": 10000,
                "value_unit": "MW target",
                "independence_key": "pib-1710113",
            },
        ),
    },
    {
        "constraint_key": "advanced_chemistry_cells",
        "constraint_name": "Advanced chemistry cell batteries",
        "as_of_date": date(2021, 5, 12),
        "state": "MEASURED",
        "classification": "PHYSICAL_CONSTRAINT",
        "product_scope": "EXACT_CHAIN",
        "investment_eligibility": "RESEARCH_ONLY",
        "import_dependency_ratio": 1.0,
        "measurement_date": date(2021, 5, 12),
        "capacity_gap_ratio": None,
        "domestic_capacity": None,
        "capacity_unit": None,
        "binding_demand_status": "INDICATED",
        "resupply_barrier_status": "UNPROVED",
        "resolution_status": "UNKNOWN",
        "next_validation_date": date(2022, 5, 12),
        "summary": "A dated primary source explicitly stated that Indian ACC demand was met through imports and domestic ACC manufacturing investment was negligible.",
        "review_note": "This is measured import dependence, not a Core-grade constraint: validate order demand, commissioning, qualification and listed-company earnings capture before any promotion.",
        "evidence": (
            {
                "evidence_type": "IMPORT",
                "source_date": date(2021, 5, 12),
                "source_url": "https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=1717938",
                "source_title": "Cabinet approves PLI scheme for Advanced Chemistry Cell Battery Storage",
                "source_publisher": "Press Information Bureau / Cabinet",
                "claim": "All ACC demand in India was met through imports and investment in domestic ACC manufacturing/value addition was negligible.",
                "value_numeric": 1.0,
                "value_unit": "share of ACC demand imported",
                "independence_key": "pib-1717938",
            },
            {
                "evidence_type": "POLICY",
                "source_date": date(2021, 5, 12),
                "source_url": "https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=1717938",
                "source_title": "Cabinet approves PLI scheme for Advanced Chemistry Cell Battery Storage",
                "source_publisher": "Press Information Bureau / Cabinet",
                "claim": "The programme targeted 50 GWh of ACC manufacturing capacity, subject to selection and commissioning rather than proof of operating supply.",
                "value_numeric": 50,
                "value_unit": "GWh target",
                "independence_key": "pib-1717938",
            },
        ),
    },
    {
        "constraint_key": "semiconductors",
        "constraint_name": "Semiconductor and display ecosystem",
        "as_of_date": date(2021, 12, 15),
        "state": "EVIDENCED",
        "classification": "POLICY_PROCUREMENT",
        "product_scope": "POLICY",
        "investment_eligibility": "RESEARCH_ONLY",
        "import_dependency_ratio": None,
        "capacity_gap_ratio": None,
        "domestic_capacity": None,
        "capacity_unit": None,
        "binding_demand_status": "INDICATED",
        "resupply_barrier_status": "INDICATED",
        "resolution_status": "UNKNOWN",
        "next_validation_date": date(2022, 6, 15),
        "summary": "The semiconductor programme evidenced strategic policy support and high capital/technology barriers, but did not establish a current product-level domestic supply gap for a listed beneficiary.",
        "review_note": "Do not use this policy record as physical semiconductor scarcity. Add exact product trade/capacity data and company-specific operating proof.",
        "evidence": (
            {
                "evidence_type": "POLICY",
                "source_date": date(2021, 12, 15),
                "source_url": "https://www.pib.gov.in/PressReleasePage.aspx?PRID=1781723",
                "source_title": "Cabinet approves Programme for Development of Semiconductors and Display Manufacturing Ecosystem in India",
                "source_publisher": "Press Information Bureau / Cabinet",
                "claim": "The programme described semiconductor manufacturing as complex, capital-intensive, high-risk and long-gestation, with fiscal support for proposed fabs and packaging units.",
                "value_numeric": 76000,
                "value_unit": "INR crore programme outlay",
                "independence_key": "pib-1781723",
            },
        ),
    },
    {
        "constraint_key": "electronic_components",
        "constraint_name": "Electronic components (broad import exposure)",
        "as_of_date": date(2022, 12, 7),
        "state": "MEASURED",
        "classification": "PHYSICAL_CONSTRAINT",
        "product_scope": "BROAD",
        "investment_eligibility": "RESEARCH_ONLY",
        "import_dependency_ratio": 0.35,
        "measurement_date": date(2022, 12, 7),
        "capacity_gap_ratio": None,
        "domestic_capacity": None,
        "capacity_unit": None,
        "binding_demand_status": "UNPROVED",
        "resupply_barrier_status": "UNPROVED",
        "resolution_status": "UNKNOWN",
        "next_validation_date": date(2023, 12, 7),
        "summary": "A primary source quantified broad electronic-component imports, but it does not identify PCB-specific demand, supply or a listed manufacturer; PCB is therefore a family-level lead only.",
        "review_note": "Split this broad record into PCB, passive components, semiconductors and other exact products before an individual product can receive a physical-quality grade.",
        "evidence": (
            {
                "evidence_type": "IMPORT",
                "source_date": date(2022, 12, 7),
                "source_url": "https://www.pib.gov.in/Pressreleaseshare.aspx?PRID=1881410",
                "source_title": "Production of Electronic Goods",
                "source_publisher": "Press Information Bureau / Ministry of Electronics & IT",
                "claim": "Electronic-component imports were INR 193,745 crore in FY2021-22, equal to 35% of total electronic-goods imports.",
                "value_numeric": 193745,
                "value_unit": "INR crore FY2021-22",
                "independence_key": "pib-1881410",
            },
        ),
    },
)


LEDGER_COLUMNS = (
    "country", "constraint_key", "constraint_name", "as_of_date", "state",
    "classification", "product_scope", "investment_eligibility",
    "import_dependency_ratio", "measurement_date", "import_value", "import_value_unit",
    "primary_origin", "primary_origin_ratio", "capacity_gap_ratio",
    "domestic_capacity", "capacity_unit", "demand_volume", "demand_unit",
    "binding_demand_status", "resupply_barrier_status",
    "resolution_status", "next_validation_date", "summary", "review_note",
)


def ensure_schema(cur) -> None:
    sql = SCHEMA_PATH.read_text()
    if SCHEMA_MARKER not in sql:
        raise RuntimeError(f"Could not find ledger schema marker in {SCHEMA_PATH}")
    cur.execute(sql.split(SCHEMA_MARKER, 1)[1])


def _parse_date(value, field: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}") from exc
    raise ValueError(f"{field} must be a date or YYYY-MM-DD string")


def load_reviewed_packet(path: str) -> tuple[dict, ...]:
    """Load a human-reviewed JSON packet without accepting post-snapshot facts.

    Expected envelope: ``{"snapshots": [{...ledger columns..., "evidence":
    [{...evidence columns...}]}]}``. The database trigger repeats the date
    rule, but validating here gives the analyst a useful input error first.
    """
    raw = json.loads(Path(path).read_text())
    rows = raw.get("snapshots") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise ValueError("ledger packet needs a non-empty 'snapshots' list")
    parsed = []
    required = set(LEDGER_COLUMNS) - {"country"}
    for index, raw_row in enumerate(rows, 1):
        if not isinstance(raw_row, dict):
            raise ValueError(f"snapshot {index} must be an object")
        missing = sorted(key for key in required if key not in raw_row)
        if missing:
            raise ValueError(f"snapshot {index} missing ledger field(s): {', '.join(missing)}")
        row = dict(raw_row)
        row["as_of_date"] = _parse_date(row["as_of_date"], f"snapshot {index}.as_of_date")
        row["next_validation_date"] = _parse_date(
            row.get("next_validation_date"), f"snapshot {index}.next_validation_date"
        )
        row["measurement_date"] = _parse_date(
            row.get("measurement_date"), f"snapshot {index}.measurement_date"
        )
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"snapshot {index} needs at least one evidence item")
        clean_evidence = []
        for ev_index, raw_ev in enumerate(evidence, 1):
            if not isinstance(raw_ev, dict):
                raise ValueError(f"snapshot {index} evidence {ev_index} must be an object")
            ev = dict(raw_ev)
            for required_ev in ("evidence_type", "source_date", "source_url", "source_title",
                                "claim", "independence_key"):
                if not ev.get(required_ev):
                    raise ValueError(f"snapshot {index} evidence {ev_index} missing {required_ev}")
            ev["source_date"] = _parse_date(
                ev["source_date"], f"snapshot {index} evidence {ev_index}.source_date"
            )
            if ev["source_date"] > row["as_of_date"]:
                raise ValueError(
                    f"snapshot {index} evidence {ev_index} is dated after its as_of_date"
                )
            for optional_key in ("source_publisher", "value_numeric", "value_unit"):
                ev.setdefault(optional_key, None)
            clean_evidence.append(ev)
        row["evidence"] = tuple(clean_evidence)
        parsed.append(row)
    return tuple(parsed)


def seed(cur, rows: tuple[dict, ...] = SEED_ROWS) -> tuple[int, int]:
    ledger_count = evidence_count = 0
    values = ", ".join(f"%({column})s" for column in LEDGER_COLUMNS)
    columns = ", ".join(LEDGER_COLUMNS)
    updates = ", ".join(
        f"{column}=EXCLUDED.{column}" for column in LEDGER_COLUMNS
        if column not in {"country", "constraint_key", "as_of_date"}
    ) + ", updated_at=NOW()"
    for row in rows:
        payload = {
            column: ("IN" if column == "country" else row.get(column))
            for column in LEDGER_COLUMNS
        }
        cur.execute(f"""
            INSERT INTO mg_constraint_ledgers ({columns})
            VALUES ({values})
            ON CONFLICT (country, constraint_key, as_of_date) DO UPDATE SET {updates}
            RETURNING id
        """, payload)
        ledger_id = cur.fetchone()[0]
        ledger_count += 1
        for evidence in row["evidence"]:
            cur.execute("""
                INSERT INTO mg_constraint_ledger_evidence
                    (ledger_id, evidence_type, source_date, source_url, source_title,
                     source_publisher, claim, value_numeric, value_unit, is_primary,
                     independence_key)
                VALUES (%(ledger_id)s, %(evidence_type)s, %(source_date)s, %(source_url)s,
                        %(source_title)s, %(source_publisher)s, %(claim)s,
                        %(value_numeric)s, %(value_unit)s, TRUE, %(independence_key)s)
                ON CONFLICT (ledger_id, evidence_type, source_date, source_url)
                DO UPDATE SET source_title=EXCLUDED.source_title,
                              source_publisher=EXCLUDED.source_publisher,
                              claim=EXCLUDED.claim,
                              value_numeric=EXCLUDED.value_numeric,
                              value_unit=EXCLUDED.value_unit,
                              independence_key=EXCLUDED.independence_key
            """, {"ledger_id": ledger_id, **evidence})
            evidence_count += 1
    return ledger_count, evidence_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show seed scope without changing the database")
    parser.add_argument("--input", help="Reviewed JSON ledger packet; defaults to the audited bootstrap rows")
    args = parser.parse_args()
    rows = load_reviewed_packet(args.input) if args.input else SEED_ROWS
    if args.dry_run:
        print(f"Would seed {len(rows)} ledger snapshots and "
              f"{sum(len(row['evidence']) for row in rows)} evidence rows.")
        for row in rows:
            print(f"  {row['as_of_date']}  {row['constraint_key']}  {row['state']}  {row['classification']}")
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_schema(cur)
            ledger_count, evidence_count = seed(cur, rows)
    print(f"Seeded/updated {ledger_count} ledger snapshots and {evidence_count} evidence rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
