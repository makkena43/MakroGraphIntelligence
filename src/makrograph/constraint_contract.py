"""Shared persisted-version contract for constraint and company-role stages.

Keeping these values in the core package prevents an ingestion snapshot from
silently reading an older role-extractor generation than the selector/report
pipeline writes.  A version change is therefore one edit, consumed by both
ends of the durable database bridge.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable


COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION = "company_filing_role_window_v11_product_quality"
CONSTRAINT_CANDIDATE_EXTRACTOR_VERSION = "constraint_candidate_union_v8_event_sourced"


# Evidence expires according to the cadence of the underlying economic fact.
# These are state-engine rules, not report-rendering preferences.  In
# particular, carrying an observation into a later snapshot never changes its
# original date or restarts this clock.
OBSERVATION_TTL_DAYS: dict[str, int] = {
    "TRADE_FLOW": 120,
    "IMPORT": 365,
    "LEAD_TIME": 180,
    "CAPACITY": 450,
    "DEMAND": 365,
    "COMMISSIONING": 450,
}

PHYSICAL_MEASUREMENT_TYPES = frozenset({"CAPACITY", "IMPORT"})
POLICY_ONLY_SOURCE_PREFIXES = ("scheme:", "policy:")
PRIMARY_SOURCE_URL_PREFIXES = ("http://", "https://")
_REVISION_SUFFIX_RE = re.compile(r"\|revision=\d+$")


def normalized_chain_label(value: str | None) -> str:
    """Normalize a chain label without making a semantic alias judgment."""
    return re.sub(
        r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").casefold())
    ).strip()


def latest_observation_revisions(
    observations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse an append-only observation stream to its latest revisions.

    Review status is deliberately *not* part of the event identity.  Therefore
    a newer PENDING or REJECTED correction supersedes an older ACCEPTED event;
    callers may filter to accepted rows only after this function returns.  This
    prevents a stale accepted revision from surviving merely because a query
    discarded its correction before revision resolution.
    """
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for observation in observations:
        source_key = str(observation.get("source_key") or "")
        identity = _REVISION_SUFFIX_RE.sub("", source_key)
        key = (
            str(observation.get("constraint_key") or ""),
            str(observation.get("observation_type") or "").upper(),
            identity,
        )
        previous = latest.get(key)
        version = (
            int(observation.get("revision") or 1),
            int(observation.get("id") or 0),
        )
        previous_version = (
            int(previous.get("revision") or 1),
            int(previous.get("id") or 0),
        ) if previous else (-1, -1)
        if previous is None or version > previous_version:
            latest[key] = observation
    return sorted(
        latest.values(),
        key=lambda row: (
            str(row.get("constraint_key") or ""),
            row.get("observed_at") or date.min,
            int(row.get("id") or 0),
        ),
    )


def product_identity_constraint_key(product_label: str | None) -> str:
    """Return the stable research key for a literal physical product.

    A product identity is not itself a scarcity claim.  It gives exact issuer
    roles somewhere safe to attach when the reviewed economic-theme alias is
    family-level or broad.  This prevents a theme rename from making a proven
    manufacturer disappear, without allowing the manufacturer to prove that
    the product is constrained.
    """
    slug = "_".join(normalized_chain_label(product_label).split())[:130].strip("_")
    return f"discovery_{slug or 'unlabelled_product'}"


def effective_alias_scope(
    product_label: str | None,
    stored_scope: str | None,
    latest_constraint_name: str | None,
) -> tuple[str | None, str]:
    """Return point-in-time alias scope and its auditable basis.

    Alias rows describe the long-lived relationship between a product and a
    chain.  A chain itself can later broaden (for example, from one physical
    product to a family).  When the latest ledger snapshot available on the
    report date names the exact same product, that dated canonical identity is
    stronger than the timeless family label.  This rule is mechanical and is
    deliberately unable to equate adjacent products.
    """
    if (normalized_chain_label(product_label) and
            normalized_chain_label(product_label) ==
            normalized_chain_label(latest_constraint_name)):
        return "EXACT", "DATED_CANONICAL_IDENTITY"
    return stored_scope, "REVIEWED_ALIAS"


def observation_ttl_days(observation_type: str | None) -> int:
    """Return the admissibility window for a dated observation type."""
    return OBSERVATION_TTL_DAYS.get((observation_type or "").upper(), 365)


def observation_is_current(
    observation_type: str | None,
    observed_at: date | None,
    as_of: date,
) -> bool:
    """Current means the original economic observation is still inside its TTL."""
    return bool(
        observed_at
        and observed_at <= as_of
        and observed_at >= as_of - timedelta(days=observation_ttl_days(observation_type))
    )


def _metric_number(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def physical_observation_admissibility(
    observation: dict[str, Any],
    as_of: date,
) -> tuple[bool, str]:
    """Validate whether an observation may create a physical measurement.

    Discovery, policy and trade-momentum rows intentionally fail this contract.
    A CAPACITY or IMPORT fact must be exact-product, accepted, primary, available
    point-in-time, current for its data cadence, and carry either an auditable
    numerator/denominator or a ratio explicitly reported by the primary source.
    """
    kind = (observation.get("observation_type") or "").upper()
    if kind not in PHYSICAL_MEASUREMENT_TYPES:
        return False, "not a physical measurement type"
    if observation.get("review_status") != "ACCEPTED":
        return False, "observation is not accepted"
    if observation.get("provenance_status") != "PRIMARY_SOURCE":
        return False, "observation lacks primary-source provenance"
    if (observation.get("product_scope") or "EXACT").upper() != "EXACT":
        return False, "observation is not exact-product scoped"
    source_url = (observation.get("source_url") or "").strip().lower()
    if (not source_url.startswith(PRIMARY_SOURCE_URL_PREFIXES)
            or source_url.startswith(POLICY_ONLY_SOURCE_PREFIXES)):
        return False, "physical measurement needs a reviewable primary-source URL"
    observed_at = observation.get("observed_at")
    published_at = observation.get("published_at") or observed_at
    available_at = observation.get("available_at") or published_at
    if not observed_at or not published_at or not available_at:
        return False, "observation, publication and availability dates are required"
    if max(observed_at, published_at, available_at) > as_of:
        return False, "observation was not available by the snapshot date"
    if not observation_is_current(kind, observed_at, as_of):
        return False, "physical observation is stale"

    metrics = observation.get("metrics") or {}
    basis = (metrics.get("measurement_basis") or observation.get("measurement_basis") or "").upper()
    if kind == "CAPACITY" and basis not in {
        "CURRENT_SUPPLY_DEMAND", "CURRENT_INSTALLED_VS_CURRENT_DEMAND",
    }:
        return False, "future-target or unspecified capacity gap is research context only"
    if kind == "IMPORT" and basis not in {
        "CURRENT_IMPORT_SHARE", "SOURCE_REPORTED_IMPORT_SHARE",
    }:
        return False, "trade value or unspecified import ratio is not import dependence"

    ratio_key = "gap_pct" if kind == "CAPACITY" else "import_share"
    ratio = _metric_number(metrics, ratio_key)
    if ratio is None:
        return False, f"{ratio_key} is missing"
    numerator = _metric_number(metrics, "numerator")
    denominator = _metric_number(metrics, "denominator")
    reported_ratio = bool(metrics.get("ratio_reported_by_source"))
    if not reported_ratio and (numerator is None or denominator is None or denominator <= 0):
        return False, "measurement needs numerator and denominator or a source-reported ratio"
    return True, "accepted current exact primary physical observation"


def nonphysical_observation_admissibility(
    observation: dict[str, Any], as_of: date,
) -> tuple[bool, str]:
    """Validate demand, lead-time and commissioning state events.

    Policy targets and generic narrative are deliberately inadmissible. These
    events can establish demand/barrier/trajectory axes but can never create a
    numerical physical measurement.
    """
    kind = (observation.get("observation_type") or "").upper()
    if kind not in {"DEMAND", "LEAD_TIME", "COMMISSIONING"}:
        return False, "not a constraint-state event type"
    if observation.get("review_status") != "ACCEPTED":
        return False, "observation is not accepted"
    if observation.get("provenance_status") != "PRIMARY_SOURCE":
        return False, "observation lacks primary-source provenance"
    if (observation.get("product_scope") or "EXACT").upper() != "EXACT":
        return False, "observation is not exact-product scoped"
    source_url = (observation.get("source_url") or "").strip().lower()
    if not source_url.startswith(PRIMARY_SOURCE_URL_PREFIXES):
        return False, "state event needs a reviewable primary-source URL"
    observed_at = observation.get("observed_at")
    published_at = observation.get("published_at") or observed_at
    available_at = observation.get("available_at") or published_at
    if not observed_at or not published_at or not available_at:
        return False, "observation, publication and availability dates are required"
    if max(observed_at, published_at, available_at) > as_of:
        return False, "observation was not available by the snapshot date"
    if not observation_is_current(kind, observed_at, as_of):
        return False, "state observation is stale"
    metrics = observation.get("metrics") or {}
    if kind == "DEMAND":
        basis = str(metrics.get("measurement_basis") or "").upper()
        if basis not in {
            "CURRENT_DEMAND", "SOURCE_REPORTED_CURRENT_DEMAND", "CONTRACTED_DEMAND",
        }:
            return False, "future policy target or unspecified demand is research context only"
        if not any(metrics.get(key) is not None for key in (
            "required_quantity", "demand", "demand_volume", "source_reported_demand",
        )):
            return False, "demand quantity/assertion is missing"
    elif kind == "LEAD_TIME":
        if metrics.get("lead_time_months") is None and not metrics.get("source_reported_pressure"):
            return False, "lead-time pressure is not source-reported"
    elif not (metrics.get("commissioning_status") or
              metrics.get("commissioned_capacity") is not None):
        return False, "commissioning state/capacity is missing"
    return True, "accepted current exact primary state observation"


def derive_constraint_state(profile: dict[str, Any]) -> dict[str, str]:
    """Derive independent physical, trajectory and completeness axes.

    This function deliberately has no company, price or desired-pick input.
    """
    measured = bool(profile.get("measured"))
    binding = bool(profile.get("binding"))
    demand = bool(profile.get("demand"))
    barrier = bool(profile.get("barrier"))
    resolving = bool(profile.get("resolving"))
    resolved = bool(profile.get("resolved"))
    overcapacity = bool(profile.get("overcapacity"))
    stale = bool(profile.get("stale"))

    if overcapacity:
        physical_state = "OVERCAPACITY"
    elif resolved:
        physical_state = "RESOLVED"
    elif stale:
        physical_state = "STALE"
    elif measured and binding and demand:
        physical_state = "BINDING"
    elif measured:
        physical_state = "MEASURED"
    elif demand or binding or barrier:
        physical_state = "EVIDENCED"
    else:
        physical_state = "DISCOVERY"

    if overcapacity or resolved:
        trajectory = "RESOLVED"
    elif resolving:
        trajectory = "RESOLVING"
    elif measured and binding:
        trajectory = "TIGHTENING"
    elif stale:
        trajectory = "UNKNOWN"
    else:
        trajectory = "STABLE_OR_UNKNOWN"

    legs = sum((measured, demand, binding, barrier))
    completeness = (
        "COMPLETE" if legs == 4 else
        "PARTIAL" if legs >= 2 else
        "MINIMAL" if legs == 1 else
        "UNMEASURED"
    )
    return {
        "physical_state": physical_state,
        "trajectory": trajectory,
        "evidence_completeness": completeness,
    }
