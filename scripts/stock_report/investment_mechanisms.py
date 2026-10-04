"""Classify company research by the economic mechanism it could monetise.

An exceptional return can arise from a physical scarcity, localisation behind a
qualification/import barrier, or a policy-led deployment wave.  Treating all
three as a ``constraint`` is analytically wrong: it hides legitimate early
companies and lets a narrative policy reference masquerade as a shortage.

This module is deliberately a synthesis layer.  It uses the selector's
already point-in-time inputs and never changes a Buy gate.  The resulting rows
are explicit research or underwriting routes with their missing proof, not a
second mechanical ranking.
"""

from __future__ import annotations

from datetime import date
from typing import Any


DIRECT_SUPPLY_ROLES = frozenset({"direct_supplier", "critical_supplier"})


def _ticker(row: dict) -> str:
    return (row.get("ticker") or "").strip().upper()


def _direct_supply_role(candidate: dict) -> bool:
    return bool(
        candidate.get("corroborated_maker")
        and DIRECT_SUPPLY_ROLES.intersection(candidate.get("types") or [])
        and not candidate.get("any_demand_side")
    )


def _issuer_role_evidenced(candidate: dict) -> bool:
    """True for a dated issuer-product role that is awaiting analyst review.

    This is intentionally weaker than ``_direct_supply_role``: it may surface
    a company found outside the mapper as a policy research lead, but never
    changes a Buy/committee gate.
    """
    return bool(candidate.get("issuer_role_evidenced"))


def _company_catalyst(candidate: dict) -> list[str]:
    catalysts: list[str] = []
    if candidate.get("order_book"):
        catalysts.append("as-of order-book evidence")
    capex = int(candidate.get("capex_signals") or 0)
    if capex >= 2:
        catalysts.append(f"{capex} mapped company-capex signals")
    return catalysts


def _constraint_products(candidate: dict, constraint: dict) -> bool:
    product = constraint.get("constraint")
    return bool(product and product in (candidate.get("corroborated_products") or []))


def _research_row(candidate: dict, constraint: dict, mechanism: str,
                  route: str, missing: list[str], catalysts: list[str]) -> dict:
    quality = constraint.get("constraint_quality") or {}
    return {
        "ticker": _ticker(candidate),
        "company": candidate.get("company"),
        "mechanism": mechanism,
        "constraint_or_product": constraint.get("constraint"),
        "themes": constraint.get("themes") or [],
        "physical_quality_grade": quality.get("grade"),
        "role_status": (
            "corroborated direct/critical supply-side role"
            if _direct_supply_role(candidate) else "role proof incomplete"
        ),
        "catalysts": catalysts,
        "research_route": route,
        "missing_proof": missing,
        "risk_tier": candidate.get("risk_tier"),
        "raw_screen_rank": candidate.get("rank"),
        "raw_composite_score": candidate.get("composite_score"),
        "investment_status": (
            "candidate for analyst underwriting — not an automatic Buy"
            if not missing else "research lead — do not add as a Buy"
        ),
    }


def _localisation_lane(candidates: list[dict], constraints: list[dict], top_n: int) -> list[dict]:
    """Return import-substitution candidates without calling imports a shortage."""
    output: list[dict] = []
    for constraint in constraints:
        quality = constraint.get("constraint_quality") or {}
        if not quality.get("import_dependence") or not constraint.get("mapping_current"):
            continue
        for candidate in candidates:
            if not _constraint_products(candidate, constraint):
                continue
            if candidate.get("risk_tier") == "HIGH":
                continue
            catalysts = _company_catalyst(candidate)
            missing: list[str] = []
            if not _direct_supply_role(candidate):
                missing.append("corroborated direct/critical domestic supply role")
            if not catalysts:
                missing.append("as-of company order or repeated product-capex catalyst")
            if not quality.get("hard_resupply_barrier"):
                missing.append("qualification, policy, or other domesticisation barrier")
            # Import dependency says local substitution is possible; it does
            # not prove the issuer earns the rent.  Keep that evidence leg
            # visible even for otherwise strong rows.
            missing.append("product-level revenue, margin, or order-conversion evidence")
            output.append(_research_row(
                candidate, constraint, "LOCALISATION_OR_QUALIFICATION",
                "verify approved domestic capacity, qualification status, and import replacement economics",
                missing, catalysts,
            ))
    return _dedupe_and_sort(output, top_n)


def _deployment_lane(candidates: list[dict], policy_screen: dict | None, top_n: int,
                     policy_company_context: dict[str, dict] | None = None) -> list[dict]:
    """Return policy-led demand research leads with explicit missing linkage.

    The policy screen records that an issuer disclosed a scheme action.  It
    does not generally prove that the action concerns the same literal product
    as the issuer's supply role, so this lane cannot become an automatic Buy.
    """
    if not policy_screen:
        return []
    by_ticker = {_ticker(candidate): candidate for candidate in candidates}
    policy_company_context = policy_company_context or {}
    output: list[dict] = []
    for bucket in ("qualified", "early_pings"):
        for scheme, entries in (policy_screen.get(bucket) or {}).items():
            for entry in entries or []:
                candidate = (by_ticker.get(_ticker(entry))
                             or policy_company_context.get(_ticker(entry))
                             or {
                                 "ticker": _ticker(entry),
                                 "company": entry.get("company") or _ticker(entry),
                                 "risk_tier": "UNASSESSED", "order_book": False,
                                 "capex_signals": 0, "types": [],
                                 "corroborated_maker": False,
                                 "any_demand_side": False,
                             })
                if candidate.get("risk_tier") == "HIGH":
                    continue
                committed = bool(entry.get("commitment"))
                recent_documents = int(entry.get("n_last12m") or 0)
                if not committed and recent_documents < 2:
                    continue
                catalysts = _company_catalyst(candidate)
                missing: list[str] = []
                direct_role = _direct_supply_role(candidate) or _issuer_role_evidenced(candidate)
                if not direct_role:
                    missing.append("corroborated direct/critical supply-side company role")
                elif candidate.get("issuer_role_requires_review"):
                    missing.append("independent reviewer confirmation of the issuer-product manufacturing role")
                if candidate.get("risk_tier") in (None, "UNASSESSED"):
                    missing.append("as-of governance/disclosure risk review")
                if not catalysts:
                    missing.append("as-of order or repeated product-capex catalyst")
                missing.extend([
                    "exact policy-action-to-product linkage in a dated source",
                    "product-level revenue, margin, or order-conversion evidence",
                ])
                output.append({
                    "ticker": _ticker(candidate),
                    "company": candidate.get("company"),
                    "mechanism": "POLICY_LED_DEPLOYMENT_DEMAND",
                    "scheme": scheme,
                    "policy_signal": (
                        "company commitment-stage disclosure" if committed
                        else "rising company policy disclosure intensity"
                    ),
                    "first_policy_mention": entry.get("first_mention"),
                    "role_status": (
                        "corroborated direct/critical supply-side role"
                        if _direct_supply_role(candidate) else
                        "issuer-product manufacturing evidence — analyst review pending"
                        if _issuer_role_evidenced(candidate) else "role proof incomplete"
                    ),
                    "catalysts": catalysts,
                    "research_route": "verify demand programme terms, product linkage, award/order conversion, and delivery capacity",
                    "missing_proof": missing,
                    "risk_tier": candidate.get("risk_tier"),
                    "raw_screen_rank": candidate.get("rank"),
                    "raw_composite_score": candidate.get("composite_score"),
                    "investment_status": "research lead — policy participation is not a Buy",
                })
    return _dedupe_and_sort(output, top_n)


def _physical_lane(final_decision: dict | None) -> list[dict]:
    """Translate the existing physical decision lanes into the common schema."""
    final_decision = final_decision or {}
    output: list[dict] = []
    for row in final_decision.get("priorities") or []:
        output.append({
            "ticker": _ticker(row), "company": row.get("company"),
            "mechanism": "PHYSICAL_CONSTRAINT_RENT",
            "constraint_or_product": (row.get("decision_grade_products") or [None])[0],
            "research_route": "complete normal portfolio underwriting and entry review",
            "missing_proof": [], "risk_tier": row.get("risk_tier"),
            "raw_screen_rank": row.get("raw_screen_rank"),
            "raw_composite_score": row.get("raw_composite_score"),
            "investment_status": "investment-committee candidate — complete portfolio underwriting and entry review",
        })
    for row in final_decision.get("underwriting_candidates") or []:
        output.append({
            "ticker": _ticker(row), "company": row.get("company"),
            "mechanism": "PHYSICAL_CONSTRAINT_RENT",
            "constraint_or_product": (row.get("mapper_corroborated_products") or
                                      row.get("decision_grade_products") or [None])[0],
            "research_route": "refresh strict dated product-maker evidence, then complete normal portfolio underwriting",
            "missing_proof": ["fresh product-specific operating-maker evidence packet"],
            "risk_tier": row.get("risk_tier"),
            "raw_screen_rank": row.get("raw_screen_rank"),
            "raw_composite_score": row.get("raw_composite_score"),
            "investment_status": "Core underwriting research candidate — do not add as a Buy",
        })
    for row in final_decision.get("early_timing_candidates") or []:
        output.append({
            "ticker": _ticker(row), "company": row.get("company"),
            "mechanism": "PHYSICAL_CONSTRAINT_RENT",
            "constraint_or_product": row.get("constraint"),
            "research_route": row.get("next_gate"),
            "missing_proof": [row.get("next_gate")] if row.get("next_gate") else [],
            "risk_tier": row.get("risk_tier"),
            "raw_screen_rank": row.get("raw_screen_rank"),
            "raw_composite_score": row.get("raw_composite_score"),
            "investment_status": row.get("category"),
        })
    for row in final_decision.get("discovery_starter_candidates") or []:
        output.append({
            "ticker": _ticker(row), "company": row.get("company"),
            "mechanism": "PHYSICAL_CONSTRAINT_DISCOVERY",
            "constraint_or_product": row.get("constraint"),
            "research_route": row.get("confirmation_event"),
            "missing_proof": [row.get("why_not_core")] if row.get("why_not_core") else [],
            "risk_tier": row.get("risk_tier"),
            "raw_screen_rank": row.get("raw_screen_rank"),
            "raw_composite_score": row.get("raw_composite_score"),
            "investment_status": row.get("category"),
            "max_portfolio_weight_pct": row.get("max_portfolio_weight_pct"),
            "invalidation": row.get("invalidation"),
        })
    return output


def _dedupe_and_sort(rows: list[dict], top_n: int) -> list[dict]:
    by_key: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (_ticker(row), row.get("mechanism") or "", row.get("constraint_or_product") or row.get("scheme") or "")
        old = by_key.get(key)
        new_score = (not bool(row.get("missing_proof")), bool(row.get("catalysts")),
                     -(float(row.get("raw_composite_score") or 0)))
        old_score = (not bool(old.get("missing_proof")), bool(old.get("catalysts")),
                     -(float(old.get("raw_composite_score") or 0))) if old else None
        if old is None or new_score > old_score:
            by_key[key] = row
    return sorted(by_key.values(), key=lambda row: (
        bool(row.get("missing_proof")), -float(row.get("raw_composite_score") or 0),
        _ticker(row), row.get("constraint_or_product") or row.get("scheme") or "",
    ))[:top_n]


def build_investment_mechanisms(
    candidates: list[dict], constraints: list[dict], policy_screen: dict | None,
    final_decision: dict | None, as_of: date, top_n: int = 12,
    policy_company_context: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Build separate, auditable economic-mechanism research lanes."""
    return {
        "as_of_date": as_of.isoformat(),
        "method": (
            "Physical scarcity, localisation/qualification, and policy-led deployment are separate economic mechanisms. "
            "Only a physical-constraint row with fresh product-specific operating-maker evidence can reach the "
            "investment-committee hand-off. Every other lane is research until literal product linkage, earnings capture, "
            "and normal underwriting are proved."
        ),
        "physical_constraint_rent": _physical_lane(final_decision),
        "localisation_or_qualification": _localisation_lane(candidates, constraints, top_n),
        "policy_led_deployment_demand": _deployment_lane(
            candidates, policy_screen, top_n, policy_company_context
        ),
    }
