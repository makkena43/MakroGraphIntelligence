#!/usr/bin/env python3
"""Ingest reviewed primary-source constraint observations as immutable events.

This is the source-neutral bridge for government statistics, trade releases,
industry capacity/demand datasets and other reviewed feeds.  Connectors write a
small JSON packet; this command validates point-in-time dates, exact product
identity, provenance and measurement semantics before inserting anything.

No packet can name or score a stock.  Company roles remain a separate issuer-
filing pipeline, which prevents desired winners from influencing constraint
detection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import psycopg2.extras


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "reference" / "constraint_observations"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "stock_report"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from constraint_ledger import fetch_constraint_aliases, normalize_product_label  # noqa: E402
from queue_constraint_observations import queue_row  # noqa: E402
from seed_constraint_ledger import connect, ensure_schema  # noqa: E402
from src.makrograph.constraint_contract import (  # noqa: E402
    nonphysical_observation_admissibility,
    physical_observation_admissibility,
)


ALLOWED_TYPES = {
    "CAPACITY", "IMPORT", "DEMAND", "TRADE_FLOW", "LEAD_TIME", "COMMISSIONING",
}
FORBIDDEN_SELECTION_FIELDS = frozenset({
    "ticker", "tickers", "symbol", "symbols", "company", "companies",
    "stock", "stocks", "share_price", "price_return", "forward_return",
    "market_cap", "winner", "selected_company",
})


def _forbidden_selection_paths(value: Any, path: str = "") -> list[str]:
    """Find stock/company/outcome fields anywhere in a constraint packet."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).casefold() in FORBIDDEN_SELECTION_FIELDS:
                found.append(child_path)
            found.extend(_forbidden_selection_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_selection_paths(child, f"{path}[{index}]"))
    return found


def _date(value: Any, field: str, *, required: bool = True) -> date | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _packet_rows(path: Path) -> Iterable[tuple[Path, dict]]:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    for file_path in files:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        rows = payload.get("observations") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError(f"{file_path}: expected a list or an observations list")
        packet_country = payload.get("country") if isinstance(payload, dict) else None
        for raw in rows:
            if not isinstance(raw, dict):
                raise ValueError(f"{file_path}: every observation must be an object")
            yield file_path, {"country": packet_country, **raw}


def _alias_index(cur, country: str, as_of: date) -> dict[tuple[str, str], dict]:
    rows = fetch_constraint_aliases(cur, as_of, country).values()
    return {
        (str(row.get("constraint_key") or ""),
         normalize_product_label(row.get("product_label"))): dict(row)
        for row in rows
    }


def validate_observation(
    raw: dict, *, country: str, as_of: date, aliases: dict[tuple[str, str], dict],
) -> dict:
    forbidden_paths = _forbidden_selection_paths(raw)
    if forbidden_paths:
        raise ValueError(
            "constraint observations cannot contain company, ticker, price or "
            f"outcome fields: {', '.join(forbidden_paths[:8])}"
        )
    row_country = str(raw.get("country") or country).upper()
    if row_country != country:
        raise ValueError(f"packet country {row_country} does not match requested {country}")
    kind = str(raw.get("observation_type") or "").upper()
    if kind not in ALLOWED_TYPES:
        raise ValueError(f"unsupported observation_type {kind!r}")
    constraint_key = str(raw.get("constraint_key") or "").strip()
    product_label = str(raw.get("product_label") or "").strip()
    alias = aliases.get((constraint_key, normalize_product_label(product_label)))
    if not alias or alias.get("status") != "REVIEWED" or alias.get("match_scope") != "EXACT":
        raise ValueError("accepted feed row needs an effective REVIEWED/EXACT product alias")

    observed_at = _date(raw.get("observed_at"), "observed_at")
    published_at = _date(raw.get("published_at"), "published_at")
    available_at = _date(raw.get("available_at"), "available_at")
    if max(observed_at, published_at, available_at) > as_of:
        raise ValueError("observation was not available by --as-of")
    if available_at < max(observed_at, published_at):
        raise ValueError("available_at cannot precede observation/publication")
    source_url = str(raw.get("source_url") or "").strip()
    if not source_url.lower().startswith(("http://", "https://")):
        raise ValueError("a reviewable primary-source HTTP(S) URL is required")
    metrics = raw.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")

    candidate = {
        **raw,
        "country": country,
        "constraint_key": constraint_key,
        "product_label": product_label,
        "observation_type": kind,
        "observed_at": observed_at,
        "published_at": published_at,
        "available_at": available_at,
        "economic_period_start": _date(
            raw.get("economic_period_start"), "economic_period_start", required=False
        ),
        "economic_period_end": _date(
            raw.get("economic_period_end"), "economic_period_end", required=False
        ),
        "source_url": source_url,
        "source_title": str(raw.get("source_title") or "").strip(),
        "source_family": str(raw.get("source_family") or "").strip(),
        "product_scope": "EXACT",
        "metrics": metrics,
        "provenance_status": "PRIMARY_SOURCE",
        "review_status": "ACCEPTED",
        "revision": int(raw.get("revision") or 1),
        "source_table": str(raw.get("source_table") or "reviewed_primary_feed"),
        "source_row_id": raw.get("source_row_id"),
    }
    if not candidate["source_title"] or not candidate["source_family"]:
        raise ValueError("source_title and source_family are required")
    if candidate["revision"] < 1:
        raise ValueError("revision must be positive")

    if kind in {"CAPACITY", "IMPORT"}:
        admissible, reason = physical_observation_admissibility(candidate, available_at)
        if not admissible:
            raise ValueError(reason)
    elif kind in {"DEMAND", "LEAD_TIME", "COMMISSIONING"}:
        admissible, reason = nonphysical_observation_admissibility(candidate, available_at)
        if not admissible:
            raise ValueError(reason)

    base_key = str(raw.get("source_key") or "").strip()
    if not base_key:
        base_key = hashlib.sha256(
            f"{source_url}|{constraint_key}|{kind}".encode("utf-8")
        ).hexdigest()[:24]
    candidate["source_key"] = f"reviewed_feed:{base_key}"
    return candidate


def ingest_path(cur, input_path: Path, country: str, as_of: date, dry_run: bool) -> dict[str, int]:
    stats = {"files": 0, "rows_seen": 0, "inserted": 0, "rejected": 0}
    if not input_path.exists():
        return stats
    aliases = _alias_index(cur, country, as_of)
    seen_files: set[Path] = set()
    errors: list[str] = []
    for file_path, raw in _packet_rows(input_path):
        seen_files.add(file_path)
        stats["rows_seen"] += 1
        try:
            row = validate_observation(raw, country=country, as_of=as_of, aliases=aliases)
            stats["inserted"] += int(queue_row(cur, row, dry_run))
        except (TypeError, ValueError) as exc:
            stats["rejected"] += 1
            errors.append(f"{file_path.name} row {stats['rows_seen']}: {exc}")
    stats["files"] = len(seen_files)
    if errors:
        raise ValueError("; ".join(errors[:20]))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="IN")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    country = args.country.upper()
    as_of = _date(args.as_of, "as_of")
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_schema(cur)
            stats = ingest_path(cur, args.input, country, as_of, args.dry_run)
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
    print(("Would ingest" if args.dry_run else "Ingested"), stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
