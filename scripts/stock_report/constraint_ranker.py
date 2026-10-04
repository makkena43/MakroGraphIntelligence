"""Constraint-first report layer for the stock selector.

The report date is the only clock. A 31-Dec-2022 decision may use only evidence
available by 31-Dec-2022; it must not require a 2026 reference-table refresh.

The layer distinguishes quantified physical evidence from filing-derived
constraint evidence available at the as-of date. Missing structured coverage is
an evidence-grade downgrade, not proof that a constraint did not exist and not
an automatic veto on a historical Buy candidate.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from company_capabilities import match_component
from constraint_emergence import match_emergence as _match_emergence
from constraint_ledger import ledger_context_for_product, normalize_product_label


STALE_MAPPING_DAYS = 120
STALE_REFERENCE_DAYS = 365
# Legacy capacity/import tables remain useful discovery inputs, but their rows
# are mutable reference snapshots.  They cannot support a physical grade until
# the observation collector has copied the source fact into the immutable,
# point-in-time observation ledger.  Currency is therefore adjudicated by the
# observation/materialization contract rather than a second TTL here.
STALE_STRUCTURAL_REFERENCE_DAYS = 0
STALE_CAPABILITY_DAYS = 365
MAX_DISPLAYED_COMPANY_EXCEPTIONS = 3
COMPANY_CAPTURE_RANKING_VERSION = "exact_product_capture_v1"


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _percent(value: Any) -> float:
    """Display DB ratios (0.75) and stored percentages (75) consistently."""
    numeric = float(value)
    return numeric * 100 if 0 <= numeric <= 1 else numeric


def _physical_context(product: str, gaps: list[dict], imports: list[dict]) -> tuple[dict | None, dict | None]:
    gap_by_component = {str(row.get("component") or ""): row for row in gaps}
    import_by_component = {str(row.get("component") or ""): row for row in imports}
    gap_name = match_component(product, list(gap_by_component))
    import_name = match_component(product, list(import_by_component))
    return (gap_by_component.get(gap_name) if gap_name else None,
            import_by_component.get(import_name) if import_name else None)


def _mapped_associations(companies: list[dict]) -> list[dict]:
    """Show mapped names as leads, never as verified beneficiaries.

    Theme co-occurrence can say that a company is exposed to a chain; it cannot
    say whether the constraint raises its profit, raises its input cost, or is
    even a product it makes.  The report must keep that economic transmission
    question open until own-filing evidence answers it.
    """
    eligible = []
    seen = set()
    for company in companies:
        ticker = (company.get("ticker") or "").strip().upper()
        if (not ticker or ticker in seen or company.get("sector_mismatch")
                or company.get("demand_side_flag")):
            continue
        seen.add(ticker)
        eligible.append(company)
    eligible.sort(key=lambda c: (-float(c.get("conviction_score") or 0),
                                 -int(c.get("signal_count") or 0)))
    return [{
        "ticker": (company.get("ticker") or "").strip().upper(),
        "company": company.get("company"),
        "beneficiary_type": company.get("beneficiary_type"),
        "mapping_status": (
            "supply-side role corroborated in the mapper; verify price pass-through and earnings capture"
            if company.get("corroborated") else
            "unverified association — do not infer a beneficiary direction"
        ),
        "economic_direction": (
            "potential supplier benefit; earnings capture not proved"
            if company.get("corroborated") else
            "unknown — could be a producer, buyer, or lexical mapping artifact"
        ),
        "role_evidence_status": (
            "mapper corroboration only" if company.get("corroborated")
            else "no exact own-filing product role"
        ),
        "corroborated": bool(company.get("corroborated")),
        "signal_count": int(company.get("signal_count") or 0),
    } for company in eligible]


_CAPACITY_RE = re.compile(
    r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(GW|MW|GWh|MWh|MT(?:PA)?|TPD)\b",
    re.I,
)
_COMMISSIONING_YEAR_RE = re.compile(
    r"\b(?:commission(?:ing|ed)?|commercial(?:\s+production|\s+operations)?|"
    r"operational|start(?:ing)?\s+production|production\s+start)\b"
    r".{0,80}?\b((?:FY\s*)?20\d{2})\b",
    re.I | re.S,
)


def _physical_quality_label(
    quantified_gap: bool,
    import_dependence: bool,
    binding_demand: bool,
    hard_barrier: bool,
) -> tuple[str, str]:
    """Return *physical* quality, deliberately separate from coverage quality.

    ``UNMEASURED`` does not mean the constraint is bad.  It means the database
    has not yet captured the numerical supply/import leg needed to make a
    physical-quality judgement.  This distinction matters especially in
    historical runs, where a contemporaneous company/order signal can be real
    even though today's reference tables have no point-in-time observation.
    """
    measured = quantified_gap or import_dependence
    if measured and binding_demand and hard_barrier:
        return "A", "measured, binding physical constraint with a documented resupply barrier"
    if measured and binding_demand:
        return "B", "measured and binding; resolution/barrier evidence remains incomplete"
    if measured:
        return "WEAK", "physical measure exists, but binding demand or resupply barrier is not proved"
    return "UNMEASURED", "physical scarcity has not been measured in the as-of reference data"


def _constraint_quality(
    gap: dict | None,
    import_dependency: dict | None,
    item: dict,
    physical_current: bool,
    filing_constraint_evidence: bool,
    ledger_context: dict | None = None,
) -> dict:
    """Grade what is actually proved about a physical constraint.

    A high document count cannot substitute for a quantified gap, an import
    fact, a binding demand signal, and a resupply barrier.  The current
    reference tables do not yet hold barrier evidence, so this intentionally
    refuses to manufacture an A-grade constraint from narrative momentum.
    """
    ledger_context = ledger_context or {}
    ledger_measure_current = bool(ledger_context.get("physical_measure_current"))
    quantified_gap = bool(gap and gap.get("gap_pct") is not None) or bool(
        ledger_measure_current and ledger_context.get("capacity_gap_ratio") is not None
    )
    import_dependence = bool(import_dependency and import_dependency.get("import_share") is not None) or bool(
        ledger_measure_current and ledger_context.get("import_dependency_ratio") is not None
    )
    exact_current_ledger = bool(
        ledger_context.get("matched") and ledger_context.get("current") and
        ledger_context.get("match_scope") == "EXACT" and
        ledger_context.get("classification") == "PHYSICAL_CONSTRAINT" and
        ledger_context.get("product_scope") == "EXACT_CHAIN"
    )
    # A reviewed, exact-chain ledger demand statement is valid dated demand
    # evidence. It is not replaced by generic mapper document volume, and it
    # still cannot create a company selection without the separate company
    # role and catalyst gates.
    binding_demand = bool(item.get("any_order_book")) or bool(
        exact_current_ledger and ledger_context.get("binding_demand_status") == "CONFIRMED"
    )
    hard_barrier = bool((gap or {}).get("resupply_barrier") or
                        (import_dependency or {}).get("qualification_barrier") or
                        (exact_current_ledger and
                         ledger_context.get("resupply_barrier_status") == "CONFIRMED"))
    ledger_resolution_veto = bool(
        ledger_context.get("matched") and ledger_context.get("match_scope") == "EXACT" and
        ledger_context.get("classification") == "PHYSICAL_CONSTRAINT" and
        ledger_context.get("product_scope") == "EXACT_CHAIN" and
        ledger_context.get("state") in {"RESOLVED", "REJECTED"}
    )
    missing = []
    if not (quantified_gap or import_dependence):
        missing.append("quantified domestic gap or import dependence")
    if not binding_demand:
        missing.append("as-of binding-demand/order-book evidence")
    if not hard_barrier:
        missing.append("documented resupply/qualification barrier")

    grade, verdict = _physical_quality_label(
        quantified_gap, import_dependence, binding_demand, hard_barrier
    )
    measurement_date = _as_date(ledger_context.get("measurement_date"))
    ledger_has_measurement = bool(
        ledger_context.get("capacity_gap_ratio") is not None or
        ledger_context.get("import_dependency_ratio") is not None or
        ledger_context.get("domestic_capacity") is not None or
        ledger_context.get("demand_volume") is not None
    )
    if physical_current:
        measurement_status = "CURRENT_MEASUREMENT"
        measurement_label = "MEASURED — current as of run date"
        measurement_next_action = "monitor demand, capacity additions, imports, and resolution timing"
    elif (ledger_context.get("match_scope") == "EXACT" and
          (measurement_date or ledger_has_measurement)):
        measurement_status = "STALE_EXACT_MEASUREMENT"
        measurement_label = "DETECTED — exact physical measurement is stale"
        measurement_next_action = "refresh the same-product capacity/demand or import observation"
    elif (gap and gap.get("gap_pct") is not None) or (
            import_dependency and import_dependency.get("import_share") is not None):
        measurement_status = "STALE_REFERENCE_MEASUREMENT"
        measurement_label = "DETECTED — matched reference measurement is stale"
        measurement_next_action = "refresh and verify the matched product-level reference observation"
    elif (ledger_context.get("matched") and
          ledger_context.get("classification") in {
              "POLICY_CATALYST", "DEPLOYMENT_DEMAND", "POLICY_PROCUREMENT"
          }):
        measurement_status = "POLICY_OR_DEPLOYMENT_ONLY"
        measurement_label = "DETECTED — policy/deployment evidence only; physical magnitude is missing"
        measurement_next_action = "measure domestic supply versus binding demand; do not infer scarcity from policy"
    elif (ledger_context.get("matched") and
          ledger_context.get("match_scope") in {"FAMILY", "BROAD"} and
          (measurement_date or ledger_has_measurement)):
        measurement_status = "FAMILY_MEASUREMENT_ONLY"
        measurement_label = "DETECTED — family-level evidence only; exact product is not measured"
        measurement_next_action = "decompose the family and ingest an exact-product physical observation"
    elif (ledger_context.get("matched") and
          ledger_context.get("match_scope") in {"FAMILY", "BROAD"}):
        measurement_status = "FAMILY_CONTEXT_ONLY"
        measurement_label = "DETECTED — family/broad context only; exact physical measurement is missing"
        measurement_next_action = "decompose the family and ingest exact-product supply and demand observations"
    else:
        measurement_status = "MISSING_EXACT_MEASUREMENT"
        measurement_label = "DETECTED — exact physical measurement is missing"
        measurement_next_action = "ingest dated domestic capacity, demand, imports, lead time, or utilization for this product"
    known_legs = sum((bool(quantified_gap or import_dependence), binding_demand, hard_barrier))
    if known_legs == 3:
        completeness = "COMPLETE"
    elif known_legs >= 2 or filing_constraint_evidence:
        completeness = "PARTIAL"
    else:
        completeness = "THIN"
    return {
        "grade": grade,
        "physical_quality_verdict": verdict,
        "measurement_coverage": {
            "status": measurement_status,
            "display_label": measurement_label,
            "measured_as_of_run_date": physical_current,
            "measurement_date": measurement_date.isoformat() if measurement_date else None,
            "next_action": measurement_next_action,
            "interpretation": (
                "This is a data-coverage state, not a verdict that the physical constraint is absent or weak."
                if not physical_current else
                "The physical magnitude is measured; the remaining quality legs still determine investability."
            ),
        },
        "quality_dimension": "physical scarcity and market structure; not document count",
        "company_selection_allowed": grade in {"A", "B"} and not ledger_resolution_veto,
        "selection_veto": (
            "dated exact-chain ledger is RESOLVED/REJECTED; refresh the ledger with contrary evidence before selecting"
            if ledger_resolution_veto else None
        ),
        "evidence_completeness": {
            "level": completeness,
            "basis": (
                "dated exact-chain ledger measurement" if ledger_measure_current else
                "quantified physical reference" if physical_current else
                "company-filings proxy" if filing_constraint_evidence else
                "mapping/order signal only"
            ),
            "known_legs": known_legs,
            "required_legs": 3,
            "note": (
                "Completeness describes what the as-of record proves. It does not turn an "
                "unmeasured constraint into a weak physical constraint."
            ),
        },
        "source_diversity": {
            "current_independent_source_count": int(
                ledger_context.get("current_independent_source_count") or 0
            ),
            "current_evidence_types": list(ledger_context.get("current_evidence_types") or []),
            "status": ledger_context.get("evidence_diversity_status") or (
                "reference-table evidence; source-family audit unavailable"
            ),
            "note": (
                "Independent source count prevents multiple fields from one release being treated as "
                "multiple confirmations. It is a research-quality diagnostic, not a score or Buy gate."
            ),
        },
        "quantified_gap": quantified_gap,
        "import_dependence": import_dependence,
        "binding_demand": binding_demand,
        "hard_resupply_barrier": hard_barrier,
        "missing_legs": missing,
        "evidence_mode": (
            "dated exact-chain ledger measurement" if ledger_measure_current else
            "quantified physical reference" if physical_current else
            "filing-derived proxy" if filing_constraint_evidence else
            "incomplete"
        ),
    }


def _resolution_clock(pipeline_makers: list[dict], maker_coverage: dict, as_of: date) -> dict:
    """Describe supply-response timing without treating plans as operating supply.

    This is deliberately a *listed-company* clock, not an estimate of the
    industry's future balance.  A disclosed project with no dated completion
    remains ``ANNOUNCED_UNDATED`` rather than being incorrectly classified as
    a near-term resolution of a constraint.
    """
    capacity_events, unknown_tickers = [], []
    seen = set()
    for maker in pipeline_makers:
        ticker = (maker.get("ticker") or "").upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        excerpt = " ".join(
            str(sample.get("excerpt") or "")
            for sample in (maker.get("evidence_samples") or [])[:2]
        )
        match = _CAPACITY_RE.search(excerpt)
        target_match = _COMMISSIONING_YEAR_RE.search(excerpt)
        target_year = None
        if target_match:
            target_year = int(re.search(r"20\d{2}", target_match.group(1)).group(0))
        if match:
            capacity_events.append({
                "ticker": ticker,
                "capacity": f"{match.group(1)} {match.group(2)}",
                "state": maker.get("maker_status"),
                "last_evidence_date": maker.get("last_evidence_date"),
                "stated_target_year": target_year,
                "target_is_company_statement_not_operating_proof": bool(target_year),
            })
        else:
            unknown_tickers.append(ticker)

    dated_targets = [event["stated_target_year"] for event in capacity_events
                     if event.get("stated_target_year")]
    if not pipeline_makers:
        status = "no listed-company supply-response event recovered"
        next_check = "expand producer coverage before concluding the constraint persists"
        response_state = "NO_LISTED_RESPONSE_RECOVERED"
    elif capacity_events:
        response_state = "DATED_TARGET_DISCLOSED" if dated_targets else "ANNOUNCED_UNDATED"
        status = (
            "announced supply response has a stated target; commissioning and utilization remain unproved"
            if dated_targets else
            "announced supply response may resolve the constraint; commissioning timing is unproved"
        )
        next_check = "verify financing, commissioning date, utilization, and customer qualification"
    else:
        status = "direct-role/pipeline disclosures found, but capacity or commissioning timing is undisclosed"
        next_check = "verify physical capacity, funding, and commissioning milestone"
        response_state = "DIRECT_ROLE_TIMING_UNDISCLOSED"
    horizon = None
    if dated_targets:
        earliest = min(dated_targets)
        months = max(0, (earliest - as_of.year) * 12)
        horizon = (
            "stated target is within 12 months" if months <= 12 else
            "stated target is 13-24 months away" if months <= 24 else
            "stated target is more than 24 months away"
        )
    return {
        "status": status,
        "supply_response_state": response_state,
        "stated_resolution_horizon": horizon or "unknown — no dated commissioning target recovered",
        "pipeline_company_count": len(pipeline_makers),
        "disclosed_capacity_events": capacity_events,
        "undisclosed_capacity_tickers": unknown_tickers,
        "next_validation": next_check,
        "coverage_note": (
            "This is a listed-company evidence clock, not total industry supply. "
            "It never infers actual commissioning, utilization, or industry balance from a plan. "
            f"{maker_coverage.get('rejected_no_product_proof', 0)} lexical screen hit(s) were excluded from it."
        ),
    }


def _cohort_diagnostic(history: dict) -> dict:
    """Summarise completed payoff history as context, never a selection input."""
    vintages = history.get("vintages") or []
    medians = sorted(float(v.get("median_2y_return_pct")) for v in vintages
                     if v.get("median_2y_return_pct") is not None)
    broad = [float(v.get("pct_cohort_gt_100pct")) for v in vintages
             if v.get("pct_cohort_gt_100pct") is not None]
    n = len(medians)
    median_payoff = (medians[n // 2] if n % 2 else (medians[n // 2 - 1] + medians[n // 2]) / 2) if n else None
    b_n = len(broad)
    median_breadth = (sorted(broad)[b_n // 2] if b_n % 2 else
                      (sorted(broad)[b_n // 2 - 1] + sorted(broad)[b_n // 2]) / 2) if b_n else None
    return {
        "completed_vintages": int(history.get("n_vintages_tested") or 0),
        "broad_winning_vintages": int(history.get("n_broad_cohort_wins") or 0),
        "median_2y_cohort_return_pct": round(median_payoff, 1) if median_payoff is not None else None,
        "median_pct_cohort_gt_100pct": round(median_breadth, 1) if median_breadth is not None else None,
        "durability_status": history.get("durability_status") or "insufficient_history",
        "selection_use": "diagnostic only — completed historical payoffs do not change the as-of quality grade or Buy gate",
    }


def _capability_evidence(
    product: str,
    capability_makers: dict[str, dict],
    as_of: date,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], date | None, dict]:
    """Return operating makers, pipeline/direct-role leads, and audit gaps.

    A direct product-capacity plan is useful for research but is not the same
    thing as an operating bottleneck owner.  Keeping those populations
    separate is essential for historical reports: it lets a 2022 filing put a
    future solar producer on the radar without backfilling its later success
    into the Buy gate.
    """
    info = capability_makers.get(product) or {}
    snapshot = _as_date(info.get("snapshot"))
    fresh = bool(snapshot and snapshot >= as_of - timedelta(days=STALE_CAPABILITY_DAYS))
    current, pipeline, stale, role_candidates, rejected = [], [], [], [], []

    def normalize_maker(maker: dict) -> dict:
        last_evidence = _as_date(maker.get("last_evidence_date"))
        strict_count = int(maker.get("strict_evidence_count") or 0)
        return {
            "ticker": maker.get("ticker"),
            "company": maker.get("company"),
            "manufacturing_filings": int(maker.get("mfg_docs") or 0),
            "strict_evidence_count": strict_count,
            "last_evidence_date": last_evidence,
            "evidence_samples": maker.get("evidence_samples") or [],
            "industry": maker.get("industry"),
            "needs_review": bool(maker.get("needs_review")),
            "source": maker.get("source"),
            "maker_status": maker.get("maker_status"),
            "research_route": maker.get("research_route"),
            "direct_evidence_count": int(maker.get("direct_evidence_count") or 0),
            "document_candidate_evidence_count": int(
                maker.get("document_candidate_evidence_count") or 0
            ),
            "entity_scope": maker.get("entity_scope"),
            "industry_mismatch": bool(maker.get("industry_mismatch")),
            "adjudication_state": maker.get("adjudication_state"),
            "adjudication_reason": maker.get("adjudication_reason"),
            "missing_evidence": maker.get("missing_evidence") or [],
            "pipeline_evidence_count": int(maker.get("pipeline_evidence_count") or 0),
            "earnings_capture_count": int(maker.get("earnings_capture_count") or 0),
        }

    promoted_states = {
        "PIPELINE_EVIDENCED", "OPERATING_PRODUCER_EVIDENCED",
        "EARNINGS_CAPTURE_EVIDENCED",
    }
    for maker in info.get("makers") or []:
        item = normalize_maker(maker)
        last_evidence = _as_date(item.get("last_evidence_date"))
        evidence_current = bool(
            last_evidence and
            last_evidence >= as_of - timedelta(days=STALE_CAPABILITY_DAYS)
        )
        adjudication = item.get("adjudication_state")
        # The legacy capability scan is a recall index, not a second company-
        # truth system.  Only a row promoted by the versioned exact-role ledger
        # may enter operating/pipeline producer populations or a position gate.
        if adjudication not in promoted_states:
            item["verification_status"] = (
                "capability discovery only — exact role ledger has not promoted this ticker/product"
            )
            item["adjudication_state"] = "QUARANTINED_AMBIGUOUS_ROLE"
            item["adjudication_reason"] = (
                "recall-oriented capability evidence is not automatic producer adjudication"
            )
            item["missing_evidence"] = [
                "two non-duplicative exact-product issuer role events in the current role ledger"
            ]
            item["position_authority"] = False
            role_candidates.append(item)
            continue
        operating_role = (
            item["maker_status"] == "operating manufacturer — independently corroborated" and
            adjudication in {"OPERATING_PRODUCER_EVIDENCED", "EARNINGS_CAPTURE_EVIDENCED"}
        )
        # A verified operating factory is a durable company-product identity,
        # not a short-lived catalyst.  Keep it in the producer universe unless
        # a dated contradiction/divestiture exists; separately mark old
        # evidence so it cannot imply current capture or position authority.
        if operating_role:
            item["producer_identity_fresh"] = evidence_current
            item["verification_status"] = (
                "exact role-ledger operating producer as of report date"
                if evidence_current else
                "verified historical operating producer; refresh current utilisation, orders and earnings capture"
            )
            item["position_authority"] = False
            if not evidence_current:
                item["missing_evidence"] = sorted(set(
                    (item.get("missing_evidence") or []) +
                    ["fresh exact-product utilisation, orders or earnings-capture evidence"]
                ))
            current.append(item)
            continue
        if not fresh or not evidence_current:
            item["verification_status"] = "stale relative to report date — refresh exact role snapshot"
            item["adjudication_state"] = "QUARANTINED_STALE_EVIDENCE"
            item["adjudication_reason"] = "role evidence is outside the as-of freshness window"
            item["missing_evidence"] = ["fresh exact-product operating or pipeline evidence"]
            item["position_authority"] = False
            stale.append(item)
            continue

        pipeline_or_commercial_role = (
            adjudication == "PIPELINE_EVIDENCED" or
            item["maker_status"] == "direct product supplier — repeated commercial capture"
        )
        if pipeline_or_commercial_role:
            item["verification_status"] = item["research_route"] or "scheduled milestone monitoring"
            item["position_authority"] = False
            item["needs_review"] = False
            pipeline.append(item)
        else:
            item["verification_status"] = "adjudicated role is not direct producer proof"
            item["adjudication_state"] = "QUARANTINED_AMBIGUOUS_ROLE"
            item["adjudication_reason"] = "role state and producer classification disagree"
            item["missing_evidence"] = ["consistent exact-product producer adjudication"]
            item["position_authority"] = False
            stale.append(item)
    for maker in info.get("role_candidates") or []:
        item = normalize_maker(maker)
        item["verification_status"] = (
            "same filing corroborates issuer manufacturing and the exact product, but local product-to-asset binding is unresolved"
        )
        item["adjudication_state"] = "QUARANTINED_AMBIGUOUS_ROLE"
        item["adjudication_reason"] = "same-document evidence lacks local product-to-owned-asset binding"
        item["missing_evidence"] = ["local issuer-owned product/asset passage", "second non-duplicative dated event"]
        item["position_authority"] = False
        role_candidates.append(item)
    for maker in info.get("rejected_candidates") or []:
        item = normalize_maker(maker)
        item["verification_status"] = "lexical capability hit; no direct or document-corroborated product role"
        item["adjudication_state"] = "AUTO_REJECTED_NO_ROLE"
        item["adjudication_reason"] = "lexical capability hit has no exact own-filing product role"
        item["position_authority"] = False
        rejected.append(item)
    coverage = dict(info.get("coverage") or {})
    coverage["snapshot"] = snapshot.isoformat() if snapshot else None
    coverage["pipeline_or_direct_role_count"] = len(pipeline)
    coverage["verified_operating_count"] = len(current)
    return current, pipeline, stale, role_candidates, rejected, snapshot, coverage


def _company_populations(
    operating: list[dict], pipeline: list[dict], stale: list[dict], role_candidates: list[dict],
    rejected: list[dict], mapped: list[dict], coverage: dict,
) -> dict:
    """Adjudicate every company hit without creating an unbounded review job.

    Exact operating/pipeline proof is promoted, mapper-only or stale evidence
    is quarantined, and lexical/no-role or wrong-role hits are rejected.  Only
    the three highest-value unresolved exceptions are displayed; aggregate
    outcomes retain audit coverage for the rest.
    """
    operating = [dict(row) for row in operating]
    pipeline = [dict(row) for row in pipeline]
    for row in operating:
        if not row.get("adjudication_state"):
            row["adjudication_state"] = (
                "EARNINGS_CAPTURE_EVIDENCED"
                if int(row.get("earnings_capture_count") or 0) >= 2
                else "OPERATING_PRODUCER_EVIDENCED"
            )
        if not row.get("adjudication_reason"):
            row["adjudication_reason"] = "fresh repeated exact-product operating-maker proof"
        row["position_authority"] = False
    for row in pipeline:
        if not row.get("adjudication_state"):
            row["adjudication_state"] = "PIPELINE_EVIDENCED"
        if not row.get("adjudication_reason"):
            row["adjudication_reason"] = "dated exact-product capacity or direct-role evidence"
        row["position_authority"] = False
    used = {
        (row.get("ticker") or "").upper()
        for row in operating + pipeline if row.get("ticker")
    }
    quarantined: list[dict] = []
    rejected_rows: list[dict] = []

    def quarantine(row: dict, state: str, reason: str, missing: list[str], priority: int) -> None:
        ticker = (row.get("ticker") or "").upper()
        if not ticker or ticker in used:
            return
        item = dict(row)
        item.update({
            "ticker": ticker,
            "adjudication_state": state,
            "adjudication_reason": reason,
            "missing_evidence": missing,
            "position_authority": False,
            "research_route": "scheduled evidence refresh; promote only when the printed machine gate clears",
            "_exception_priority": priority,
        })
        quarantined.append(item)
        used.add(ticker)

    def reject(row: dict, state: str, reason: str) -> None:
        ticker = (row.get("ticker") or "").upper()
        if not ticker or ticker in used:
            return
        rejected_rows.append({
            "ticker": ticker, "adjudication_state": state,
            "adjudication_reason": reason, "position_authority": False,
        })
        used.add(ticker)

    for row in role_candidates:
        quarantine(
            row, "QUARANTINED_AMBIGUOUS_ROLE",
            "same-document product and issuer-role evidence lacks a local product-to-owned-asset binding",
            ["local issuer-owned product/asset passage", "second non-duplicative dated event"], 0,
        )
    for row in stale:
        quarantine(
            row, "QUARANTINED_STALE_EVIDENCE",
            "previous role evidence is outside the as-of freshness window",
            ["fresh exact-product operating or pipeline evidence"], 1,
        )

    supply_roles = {"direct_supplier", "critical_supplier", "input_supplier", "supplier"}
    for row in mapped:
        ticker = (row.get("ticker") or "").upper()
        if not ticker or ticker in used:
            continue
        role = str(row.get("beneficiary_type") or "").casefold()
        if row.get("corroborated") and role in supply_roles:
            quarantine(
                row, "QUARANTINED_MAPPER_ONLY",
                "same-product mapper corroboration exists, but exact own-filing producer proof is absent",
                ["exact issuer-owned product role", "owned operating capacity or dated pipeline"], 2,
            )
        elif row.get("corroborated"):
            reject(
                row, "AUTO_REJECTED_WRONG_ROLE",
                "corroborated deployment, buyer or other non-producer role cannot enter the producer universe",
            )
        else:
            reject(
                row, "AUTO_REJECTED_NO_ROLE",
                "theme or product association has no exact corroborated supply-side company role",
            )
    for row in rejected:
        reject(
            row, "AUTO_REJECTED_NO_ROLE",
            "lexical capability hit has no direct or document-corroborated exact product role",
        )

    quarantined.sort(key=lambda row: (
        int(row.get("_exception_priority") or 0),
        -int(row.get("strict_evidence_count") or row.get("signal_count") or 0),
        row.get("ticker") or "",
    ))
    displayed_exceptions = []
    for row in quarantined[:MAX_DISPLAYED_COMPANY_EXCEPTIONS]:
        row.pop("_exception_priority", None)
        displayed_exceptions.append(row)
    rejection_reasons: dict[str, int] = {}
    for row in rejected_rows:
        state = row["adjudication_state"]
        rejection_reasons[state] = rejection_reasons.get(state, 0) + 1
    if operating:
        status = "OPERATING_PRODUCERS_FOUND"
    elif pipeline:
        status = "CAPACITY_OR_DIRECT_ROLE_FOUND"
    elif quarantined:
        status = "AUTOMATED_ADJUDICATION_UNRESOLVED"
    elif rejected_rows:
        status = "ALL_COMPANY_HITS_AUTO_REJECTED"
    elif int(coverage.get("candidate_count") or 0):
        status = "LEXICAL_CANDIDATES_REJECTED_ROLE_COVERAGE_GAP"
    else:
        status = "NO_EXACT_COMPANY_CANDIDATES_RECOVERED"
    return {
        "status": status,
        "operating_product_producers": operating,
        "capacity_or_localisation_entrants": pipeline,
        # Backward-compatible keys remain, but mapper-only rows now live in a
        # single bounded exception lane rather than a second pseudo-universe.
        "mapped_supply_or_deployment_leads": [],
        "unverified_company_leads": displayed_exceptions,
        "quarantined_exceptions": displayed_exceptions,
        "population_counts": {
            "operating_product_producers": len(operating),
            "capacity_or_localisation_entrants": len(pipeline),
            "mapped_supply_or_deployment_leads": 0,
            "unverified_company_leads": len(displayed_exceptions),
            "quarantined_total": len(quarantined),
            "auto_rejected_total": len(rejected_rows),
        },
        "rejected_lexical_candidate_count": max(
            len(rejected), int(coverage.get("rejected_no_product_proof") or 0)
        ),
        "automated_adjudication": {
            "mode": "precision-first automatic promotion, quarantine and rejection",
            "promoted_operating_count": len(operating),
            "promoted_pipeline_count": len(pipeline),
            "quarantined_count": len(quarantined),
            "displayed_exception_count": len(displayed_exceptions),
            "display_limit": MAX_DISPLAYED_COMPANY_EXCEPTIONS,
            "auto_rejected_count": len(rejected_rows),
            "rejection_reasons": rejection_reasons,
            "manual_review_required_for_routine_promotion": False,
            "human_rejection_override_preserved": True,
        },
        "interpretation": (
            "Exact evidence is promoted automatically; ambiguous evidence is quarantined and weak/wrong-role evidence "
            "is auto-rejected. The displayed exceptions are capped and have no position authority."
        ),
    }


def _company_capture_readiness(mapped_companies: list[dict]) -> dict:
    """Summarise the *same-product* producer proof behind a constraint.

    The theme mapper is intentionally broad: it is useful for discovery, but
    neither its document count nor the number of names it finds says that a
    listed company captures the economics.  This compact summary separates
    exact, corroborated supplier roles from unverified associations and is
    used only to order research within an otherwise identical constraint tier.
    It never changes a company Buy gate.
    """
    exact_roles, catalyst_backed = [], []
    for company in mapped_companies or []:
        ticker = (company.get("ticker") or "").strip().upper()
        role = company.get("beneficiary_type") or ""
        if (not ticker or company.get("sector_mismatch") or
                company.get("demand_side_flag") or
                company.get("corroborated") is not True or
                role not in {"direct_supplier", "critical_supplier"}):
            continue
        record = {
            "ticker": ticker,
            "company": company.get("company"),
            "role": role,
            "as_of_order_book": bool(company.get("has_order_book_signals")),
            "capex_signals": int(company.get("capex_signals") or 0),
        }
        exact_roles.append(record)
        if record["as_of_order_book"] or record["capex_signals"] >= 2:
            catalyst_backed.append(record)

    exact_roles.sort(key=lambda row: (
        not row["as_of_order_book"], -row["capex_signals"], row["ticker"]
    ))
    catalyst_backed.sort(key=lambda row: (
        not row["as_of_order_book"], -row["capex_signals"], row["ticker"]
    ))
    # This is deliberately a small, transparent *research ordering* signal.
    # Physical quality and tier always dominate it; it cannot promote a theme
    # to an investment decision or a stock to an Early/Timing candidate.
    score = min(12, len(exact_roles) * 2) + min(12, len(catalyst_backed) * 4)
    return {
        "exact_supply_role_count": len(exact_roles),
        "catalyst_backed_exact_role_count": len(catalyst_backed),
        "exact_supply_roles": exact_roles[:8],
        "catalyst_backed_exact_roles": catalyst_backed[:8],
        "research_priority_points": score,
        "selection_use": (
            "research ordering only — exact role/catalyst evidence does not prove "
            "earnings capture, valuation, or position eligibility"
        ),
    }


def _automatic_coverage_placeholder(alias: dict, ledger: dict | None) -> bool:
    """Identify label-coverage rows that have not become research evidence.

    Automatic product-to-self identities remain durable in the DB so future
    ingestion can attach to them.  Until they acquire evidence, measurement or
    an evidenced state, they are coverage diagnostics—not detected constraints
    and should not enter the reader-facing research universe.
    """
    row = ledger or {}
    return bool(
        alias.get("status") == "AUTO_DISCOVERY" and
        row.get("state") == "DISCOVERY" and
        row.get("classification") == "WATCH" and
        not (row.get("evidence") or []) and
        row.get("import_dependency_ratio") is None and
        row.get("capacity_gap_ratio") is None
    )


def rank_greatest_constraints(
    products: list[dict],
    gaps: list[dict],
    imports: list[dict],
    supply_by_product: dict[str, list[dict]],
    cohort_history: dict[str, dict],
    capability_makers: dict[str, dict],
    as_of: date,
    top_n: int = 8,
    constraint_ledger: dict[str, dict] | None = None,
    constraint_aliases: dict[str, dict] | None = None,
    observation_summary: dict[str, dict] | None = None,
    emergence: dict[str, dict] | None = None,
) -> list[dict]:
    """Return current constraints, including physically evidenced unmapped chains.

    The mapper is a company-discovery input, not the denominator for real
    bottlenecks.  A current capacity/import observation or a reviewed exact
    ledger chain must therefore enter the research universe even when no
    listed company has yet been attached.  Such a reference-only chain is
    explicitly non-investable until the separate company-role pipeline finds
    an exact supplier; this improves constraint recall without manufacturing a
    stock recommendation.
    """
    grouped: dict[str, dict] = {}

    def register(label: str | None, origin: str, row: dict | None = None) -> None:
        product = (label or "").strip()
        normalized = normalize_product_label(product)
        if not normalized:
            return
        item = grouped.setdefault(normalized, {
            "product_label": product,
            "themes": set(), "n_companies": 0, "last_mapped": None,
            "any_order_book": False, "any_import_sub": False,
            "detection_origins": set(),
            "upstream_candidates": [],
        })
        # Prefer the mapper spelling when available: it is the key under
        # which company evidence is stored. A capacity/import-only spelling is
        # still retained when it has no mapper counterpart.
        if origin == "MAPPER":
            item["product_label"] = product
        item["detection_origins"].add(origin)
        if row and row.get("upstream_candidate"):
            item["upstream_candidates"].append(row["upstream_candidate"])
        if not row:
            return
        theme = (row.get("theme_name") or "").strip()
        if theme:
            item["themes"].add(theme)
        item["n_companies"] = max(item["n_companies"], int(row.get("n_companies") or 0))
        item["any_order_book"] = item["any_order_book"] or bool(row.get("any_order_book"))
        item["any_import_sub"] = item["any_import_sub"] or bool(row.get("any_import_sub"))
        mapped = _as_date(row.get("last_mapped"))
        if mapped and (item["last_mapped"] is None or mapped > item["last_mapped"]):
            item["last_mapped"] = mapped

    for row in products:
        product = (row.get("constrained_product") or "").strip()
        if not product:
            continue
        register(product, row.get("detection_origin") or "MAPPER", row)

    # Reference tables are an independent constraint-discovery input. They
    # must not depend on a beneficiary mapper already having found a company.
    for row in gaps:
        if row.get("gap_pct") is not None:
            register(row.get("component"), "CAPACITY_REFERENCE")
    for row in imports:
        if row.get("import_share") is not None:
            register(row.get("component"), "IMPORT_REFERENCE")
    # Only a reviewed EXACT product alias can bring a ledger chain into the
    # product universe. Family/broad/policy crosswalks remain useful context,
    # but cannot be silently converted into an item-level constraint.
    seen_aliases: set[tuple[str, str]] = set()
    for alias in (constraint_aliases or {}).values():
        key = alias.get("constraint_key") or ""
        label = alias.get("product_label") or ""
        identity = (normalize_product_label(label), key)
        ledger_row = (constraint_ledger or {}).get(key)
        if (not label or identity in seen_aliases or alias.get("match_scope") != "EXACT" or
                key not in (constraint_ledger or {}) or
                _automatic_coverage_placeholder(alias, ledger_row)):
            continue
        seen_aliases.add(identity)
        register(label, "EXACT_LEDGER")

    ranked = []
    for item in grouped.values():
        product = item["product_label"]
        supply_rows = supply_by_product.get(product)
        if supply_rows is None:
            supply_rows = next(
                (rows for label, rows in supply_by_product.items()
                 if normalize_product_label(label) == normalize_product_label(product)),
                [],
            )
        detection_origins = sorted(item.get("detection_origins") or [])
        upstream_candidates = sorted(
            item.get("upstream_candidates") or [],
            key=lambda row: -int(row.get("research_priority") or 0),
        )
        upstream_candidate = upstream_candidates[0] if upstream_candidates else None
        upstream_last_evidence = _as_date((upstream_candidate or {}).get("last_mapped"))
        upstream_current = bool(
            upstream_last_evidence and
            upstream_last_evidence >= as_of - timedelta(days=STALE_REFERENCE_DAYS)
        )
        reference_only = (
            "MAPPER" not in detection_origins and int(item.get("n_companies") or 0) == 0
        )
        gap, import_dependency = _physical_context(product, gaps, imports)
        # The materialized upstream candidate is itself a dated selector input.
        # Preserve its measured import/capacity leg when the legacy reference
        # tables do not contain the same product label. Otherwise a rigorously
        # detected Grade-B physical constraint can disappear behind unmeasured
        # mapper themes merely because the two stages use different labels.
        upstream_legs = (upstream_candidate or {}).get("evidence_legs") or {}
        upstream_grade = (upstream_candidate or {}).get("physical_quality")
        if upstream_current and upstream_grade in {"A", "B", "WEAK"}:
            capacity_as_of = _as_date(upstream_legs.get("capacity_as_of"))
            import_as_of = _as_date(upstream_legs.get("import_as_of"))
            if (gap is None and upstream_legs.get("has_capacity_measure") and
                    upstream_legs.get("capacity_gap_pct") is not None and capacity_as_of):
                gap = {
                    "component": product,
                    "gap_pct": upstream_legs.get("capacity_gap_pct"),
                    "as_of_date": capacity_as_of,
                    "evidence_source": "UPSTREAM_CONSTRAINT_PIPELINE",
                    "measurement_basis": "CURRENT_SUPPLY_DEMAND",
                }
            if (import_dependency is None and upstream_legs.get("has_import_measure") and
                    upstream_legs.get("import_share") is not None and import_as_of):
                import_dependency = {
                    "component": product,
                    "import_share": upstream_legs.get("import_share"),
                    "primary_origin": upstream_legs.get("primary_origin"),
                    "as_of_date": import_as_of,
                    "evidence_source": "UPSTREAM_CONSTRAINT_PIPELINE",
                    "measurement_basis": "CURRENT_IMPORT_SHARE",
                }
        ledger_context = ledger_context_for_product(
            product, constraint_ledger, as_of,
            aliases_by_label=constraint_aliases,
            observation_summary=observation_summary,
        )
        queued_observations = ledger_context.get("queued_observations") or {}
        history = cohort_history.get(product) or {}
        historical_durable = bool(history.get("durable"))
        mapping_current = bool(item["last_mapped"] and item["last_mapped"] >= as_of - timedelta(days=STALE_MAPPING_DAYS))
        gap_date = _as_date((gap or {}).get("as_of_date"))
        import_date = _as_date((import_dependency or {}).get("as_of_date"))
        # Only the event-sourced upstream snapshot can carry a physical
        # measurement into a decision.  A primary-looking legacy reference is
        # still mutable and may encode a future policy target; it must first be
        # admitted by queue_constraint_observations and materialized.
        gap_source_ready = bool(
            gap and gap.get("evidence_source") == "UPSTREAM_CONSTRAINT_PIPELINE" and
            gap.get("measurement_basis") in {
                "CURRENT_SUPPLY_DEMAND", "CURRENT_INSTALLED_VS_CURRENT_DEMAND",
            }
        )
        import_source_ready = bool(
            import_dependency and
            import_dependency.get("evidence_source") == "UPSTREAM_CONSTRAINT_PIPELINE" and
            import_dependency.get("measurement_basis") in {
                "CURRENT_IMPORT_SHARE", "SOURCE_REPORTED_IMPORT_SHARE",
            }
        )
        gap_current = bool(gap_date and gap_date >= as_of - timedelta(days=STALE_STRUCTURAL_REFERENCE_DAYS)
                           and gap and gap.get("gap_pct") is not None and gap_source_ready)
        import_current = bool(import_date and import_date >= as_of - timedelta(days=STALE_STRUCTURAL_REFERENCE_DAYS)
                              and import_dependency and import_dependency.get("import_share") is not None
                              and import_source_ready)
        ledger_physical_current = bool(ledger_context.get("physical_measure_current"))
        exact_ledger_constraint_evidence = bool(
            ledger_context.get("matched") and ledger_context.get("current") and
            ledger_context.get("match_scope") == "EXACT" and
            ledger_context.get("classification") == "PHYSICAL_CONSTRAINT" and
            ledger_context.get("product_scope") == "EXACT_CHAIN"
        )
        physical_current = gap_current or import_current or ledger_physical_current
        # These flags are derived from filings whose snapshot is <= as_of. They
        # are weaker than a quantified table, but are legitimate historical
        # evidence when the structured reference coverage had not yet been
        # ingested. They cannot be replaced by today's data.
        filing_constraint_evidence = bool(
            mapping_current and item["any_order_book"] and item["any_import_sub"]
        )
        constraint_evidence = (physical_current or filing_constraint_evidence or
                               exact_ledger_constraint_evidence)
        # A current mapped chain with order-book evidence is a legitimate
        # early research signal even if import/gap coverage has not yet been
        # assembled. It cannot create position authority in the physical-
        # constraint lane; the Early/Timing gate separately requires A/B.
        ledger_resolved_or_rejected = bool(
            ledger_context.get("matched") and ledger_context.get("match_scope") == "EXACT" and
            ledger_context.get("classification") == "PHYSICAL_CONSTRAINT" and
            ledger_context.get("product_scope") == "EXACT_CHAIN" and
            ledger_context.get("state") in {"RESOLVED", "REJECTED"}
        )
        early_investability_signal = bool(
            mapping_current and item["any_order_book"] and not ledger_resolved_or_rejected
        )
        (verified_makers, pipeline_makers, stale_makers, role_candidates,
         rejected_company_candidates, capability_snapshot, maker_coverage) = _capability_evidence(
            product, capability_makers, as_of
        )
        mapped_company_leads = _mapped_associations(supply_rows)
        company_populations = _company_populations(
            verified_makers, pipeline_makers, stale_makers, role_candidates,
            rejected_company_candidates, mapped_company_leads, maker_coverage,
        )
        company_capture = _company_capture_readiness(supply_rows)
        quality_gap = gap if gap_current else (
            {**gap, "gap_pct": None} if gap else None
        )
        quality_import = import_dependency if import_current else (
            {**import_dependency, "import_share": None}
            if import_dependency else None
        )
        quality = _constraint_quality(
            quality_gap, quality_import, item, physical_current, filing_constraint_evidence,
            ledger_context,
        )
        resolution_clock = _resolution_clock(pipeline_makers, maker_coverage, as_of)
        cohort_diagnostic = _cohort_diagnostic(history)

        # The primary sort remains the evidence tier.  Within a tier, put a
        # binding chain with a recoverable same-product producer universe ahead
        # of one with only generic mapper associations.  This makes the report
        # more useful for finding companies while preserving the hard gates.
        severity_points = (
            {"A": 24, "B": 18, "UNMEASURED": 10, "WEAK": 4}.get(
                quality.get("grade"), 0
            )
            + (8 if quality.get("binding_demand") else 0)
            + (5 if quality.get("hard_resupply_barrier") else 0)
            + (4 if mapping_current else 0)
            + int(company_capture["research_priority_points"])
        )
        # Emergence-weighted blend: severity is the base, multiplied up by how
        # much this constraint is INFLECTING this year (YoY signal acceleration,
        # point-in-time). This is what makes the year-to-year ranking differ
        # instead of the largest/best-evidenced constraint winning every year.
        # emergence_score is ~0-3 (2x surge -> +1, plus a novelty kicker); a 0.35
        # weight lets a strongly inflecting constraint roughly double its rank
        # weight without letting a weak-evidence theme leapfrog a proven one
        # (the evidence TIER remains the primary sort key below).
        emergence_info = _match_emergence(product, emergence or {}) or {}
        emergence_score = float(emergence_info.get("emergence_score") or 0.0)
        priority_points = round(severity_points * (1.0 + 0.35 * emergence_score), 2)

        if historical_durable and mapping_current and physical_current:
            tier, tier_order = "Historically durable — revalidate current economics", 0
        elif mapping_current and physical_current:
            tier, tier_order = "As-of physical constraint — proof of capture incomplete", 1
        elif physical_current:
            tier, tier_order = "As-of physical constraint — listed-company mapping incomplete", 1
        elif exact_ledger_constraint_evidence:
            tier, tier_order = "As-of exact-chain ledger — physical measurement incomplete", 2
        elif filing_constraint_evidence:
            tier, tier_order = "As-of filing-derived constraint — structured physical data unavailable", 2
        elif mapping_current:
            tier, tier_order = "Theme/mapping lead — constraint evidence incomplete", 3
        elif upstream_current:
            tier, tier_order = "Upstream cross-source candidate — complete missing evidence legs", 3
        elif reference_only and (gap or import_dependency):
            tier, tier_order = (
                "Reference context — measurement review and listed-company mapping incomplete",
                3,
            )
        else:
            tier, tier_order = "Stale as-of evidence — do not research until refreshed", 4

        evidence = []
        if reference_only:
            evidence.append(
                "detected from dated physical/ledger reference without a current mapper company cohort; "
                "build an exact supplier universe before any stock research"
            )
        if upstream_candidate:
            evidence.append(
                f"upstream ingestion/NLP candidate: {upstream_candidate.get('mechanism') or 'unclassified'}; "
                f"research priority {int(upstream_candidate.get('research_priority') or 0)}/100; "
                "this is research ordering, not investment authority"
            )
        if historical_durable:
            evidence.append("completed historical cohort durability passed every tested two-year vintage")
        if gap_current:
            evidence.append(f"as-of physical context: {float(gap['gap_pct']):.1f}% capacity gap")
        if import_current:
            evidence.append(f"import context: {_percent(import_dependency['import_share']):.1f}% import dependence")
        if ledger_context.get("matched"):
            if ledger_context.get("can_support_buy_gate"):
                ledger_measure = ledger_context.get("capacity_gap_ratio")
                if ledger_measure is not None:
                    evidence.append(f"dated constraint ledger: {float(ledger_measure) * 100:.1f}% capacity gap")
                else:
                    evidence.append("dated constraint ledger: exact-chain import dependence measurement")
            elif ledger_context.get("current"):
                evidence.append("dated constraint ledger: " + ledger_context.get("status", "context only"))
            else:
                evidence.append("dated constraint ledger is stale or due for revalidation; it cannot support this decision")
        if ledger_resolved_or_rejected:
            evidence.append("dated ledger marks this exact physical constraint resolved/rejected; it is excluded from the Early/Timing lane")
        if queued_observations.get("pending_count"):
            queued_types = ", ".join(queued_observations.get("observation_types") or [])
            evidence.append(
                f"{queued_observations['pending_count']} automated {queued_types} observation(s) queued for source review; they do not support a Buy gate"
            )
        if filing_constraint_evidence and not physical_current:
            evidence.append("as-of filing evidence: mapped supply chain had both order-book and import-substitution signals; quantified reference table unavailable")
        if not constraint_evidence:
            evidence.append("no as-of quantified or filing-derived constraint evidence")
        if quality["missing_legs"]:
            evidence.append("decision-grade evidence still missing: " + ", ".join(quality["missing_legs"]))
        if quality["grade"] == "UNMEASURED":
            evidence.append("physical quality is unmeasured, not graded weak: retain as research and obtain the missing measurement before position review")
        if verified_makers:
            evidence.append(f"{len(verified_makers)} independently corroborated direct operating producer(s) recovered")
        elif pipeline_makers:
            evidence.append(f"{len(pipeline_makers)} direct product/capacity lead(s) recovered; verify commissioning or physical capacity")
        else:
            evidence.append("no strict product-specific maker proof recovered; use as-of role corroboration where available")

        ranked.append({
            "constraint": product,
            "detection_origins": detection_origins,
            "reference_only_constraint": reference_only,
            "upstream_constraint_candidate": upstream_candidate,
            "research_tier": tier,
            "themes": sorted(item["themes"]),
            "mapped_company_count": item["n_companies"],
            "last_mapped": item["last_mapped"],
            "mapping_current": mapping_current,
            "capacity_gap": gap,
            "import_dependency": import_dependency,
            "constraint_ledger": ledger_context,
            "automated_observation_queue": queued_observations or None,
            "physical_evidence_status": (
                "as-of quantified physical evidence" if physical_current
                else "as-of exact-chain ledger evidence (unquantified)" if exact_ledger_constraint_evidence
                else "as-of filing-derived constraint evidence (unquantified)" if filing_constraint_evidence
                else "as-of constraint evidence incomplete"),
            "physical_evidence_current": physical_current,
            "filing_constraint_evidence": filing_constraint_evidence,
            "as_of_constraint_evidence": constraint_evidence,
            "early_investability_signal": early_investability_signal,
            "constraint_quality": quality,
            "resolution_clock": resolution_clock,
            "historical_cohort": history or None,
            "cohort_quality_diagnostic": cohort_diagnostic,
            "verified_current_makers": verified_makers,
            "pipeline_or_direct_role_makers": pipeline_makers,
            "stale_or_review_makers": stale_makers,
            "document_level_role_candidates": role_candidates,
            "company_populations": company_populations,
            "company_capture_readiness": company_capture,
            "emergence": {
                "emergence_score": round(emergence_score, 2),
                "emergence_ratio": emergence_info.get("emergence_ratio"),
                "recent_signals": emergence_info.get("recent_signals"),
                "prior_signals": emergence_info.get("prior_signals"),
                "new_this_year": bool(emergence_info.get("new_this_year")),
                "basis": ("YoY acceleration in dated shortage/bottleneck/capex/order/"
                          "localisation filing signals for this constraint, point-in-time"),
            },
            "constraint_research_priority": {
                "score": priority_points,
                "severity_score": severity_points,
                "emergence_multiplier": round(1.0 + 0.35 * emergence_score, 2),
                "basis": (
                    "severity (physical quality, binding demand, resupply barrier, mapping "
                    "freshness, exact producer/catalyst evidence) x emergence multiplier "
                    "(this-year signal acceleration)"
                ),
                "selection_use": (
                    "research ordering only — it cannot override the Core or Early/Timing "
                    "company gates"
                ),
            },
            "capability_snapshot": capability_snapshot,
            "maker_coverage": maker_coverage,
            "mapped_company_leads": mapped_company_leads,
            "evidence": evidence,
            "stock_selection_status": (
                "research-only — exact listed-company mapping has not been recovered"
                if reference_only else
                "eligible for as-of company selection" if quality["company_selection_allowed"]
                else "research-only — demand/catalyst signal present, but the physical constraint is not position-grade"
                if early_investability_signal else
                "research-only — no current binding-demand signal"
            ),
            "caveat": (
                "Mapped-company counts and mapper conviction are not rank inputs. A company is not a beneficiary until its "
                "product role and earnings transmission (pricing/pass-through or order capture) are proved as of this report date."
            ),
            "_tier_order": tier_order,
        })

    ranked.sort(key=lambda item: (
        item["_tier_order"],
        -float((item.get("constraint_research_priority") or {}).get("score") or 0),
        -len(item["verified_current_makers"]),
        item["constraint"],
    ))
    for item in ranked:
        item.pop("_tier_order")
    # Truncating the UNIVERSE is a display convenience, but a constraint that
    # has a verified operating/earnings producer is investable and must never
    # fall off merely because the universe grew (else its makers vanish from the
    # As-of Decision — the 2026 Pharma APIs regression). Keep the top_n head plus
    # every tail constraint that carries a verified current maker.
    kept = ranked[:top_n]
    if len(ranked) > top_n:
        kept_ids = {id(item) for item in kept}
        kept.extend(item for item in ranked[top_n:]
                    if item.get("verified_current_makers")
                    and id(item) not in kept_ids)
    return kept


def _best_stock_research_queue(candidates: list[dict], constraints: list[dict], top_n: int) -> list[dict]:
    """Return the strongest proof-led company research queue, never a Buy list.

    This is the answer to "which stocks should we add?": add a name only when
    an exact role is evidenced, then make the remaining constraint and
    earnings-capture work explicit.  Raw mapping ranks cannot add a company to
    this queue by themselves.
    """
    raw_by_ticker = {(c.get("ticker") or "").upper(): c for c in candidates}
    queue_by_key: dict[tuple[str, str], dict] = {}
    for constraint in constraints:
        product = constraint.get("constraint") or ""
        quality = constraint.get("constraint_quality") or {}
        quality_grade = quality.get("grade") or "UNMEASURED"
        missing_constraint = ", ".join(quality.get("missing_legs") or [])
        for population, role_rank in (
            (constraint.get("verified_current_makers") or [], 0),
            (constraint.get("pipeline_or_direct_role_makers") or [], 1),
        ):
            for maker in population:
                ticker = (maker.get("ticker") or "").upper()
                if not ticker:
                    continue
                raw = raw_by_ticker.get(ticker) or {}
                operating = role_rank == 0
                next_gate = (
                    "prove product-level revenue/order conversion and pricing or pass-through"
                    if operating else maker.get("research_route") or
                    "verify funding, commissioning, and customer qualification"
                )
                if missing_constraint:
                    next_gate += "; complete constraint evidence: " + missing_constraint
                item = {
                    "ticker": ticker,
                    "company": maker.get("company") or raw.get("company"),
                    "constraint": product,
                    "constraint_quality_grade": quality_grade,
                    "maker_status": maker.get("maker_status"),
                    "role_state": (maker.get("adjudication_state") or
                                   ("operating direct producer" if operating else
                                    "capacity/direct-role pipeline")),
                    "benefit_direction": (
                        "potential positive supplier exposure; earnings capture still unproved"
                        if operating else
                        "conditional upside if capacity commissions and sells into the shortage; new supply may also resolve it"
                    ),
                    "latest_evidence_date": maker.get("last_evidence_date"),
                    "raw_screen_rank": raw.get("rank"),
                    "raw_composite_score": raw.get("composite_score"),
                    "screen_coverage": "present in raw screen" if raw else "missed by raw screen",
                    "order_book": bool(raw.get("order_book")),
                    "risk_tier": raw.get("risk_tier"),
                    "next_gate": next_gate,
                    "research_status": (
                        "research now — role proved, but not Buy-ready"
                        if operating and quality.get("company_selection_allowed") else
                        "research lead — do not add as Buy"
                    ),
                    "_role_rank": role_rank,
                    "_quality_rank": {"A": 0, "B": 1, "UNMEASURED": 2, "WEAK": 3}.get(quality_grade, 4),
                }
                key = (ticker, product)
                old = queue_by_key.get(key)
                if old is None or (item["_role_rank"], item["_quality_rank"]) < (old["_role_rank"], old["_quality_rank"]):
                    queue_by_key[key] = item
    queue = list(queue_by_key.values())
    queue.sort(key=lambda row: (
        row["_quality_rank"], row["_role_rank"], not row["order_book"],
        -float(row["raw_composite_score"] or 0), row["ticker"], row["constraint"]
    ))
    for row in queue:
        row.pop("_role_rank")
        row.pop("_quality_rank")
    return queue[:top_n]


def _regime_position_guidance(market_regime: dict | None, lane: str) -> str:
    """Translate market regime into implementation discipline, never eligibility."""
    label = (market_regime or {}).get("regime_label") or "N/A"
    if lane == "core":
        if label == "BEAR_CAUTION":
            return "half intended position, staged; add only after the company confirmation gate"
        if label == "FLAT_MIXED":
            return "stage in two tranches; add only after confirmation"
        return "standard staged position; still use the stated company confirmation"
    if label == "BEAR_CAUTION":
        return "pilot only (about one-quarter of intended starter); no add before confirmation"
    if label == "FLAT_MIXED":
        return "half starter, staged; add only after confirmation"
    return "capped starter, staged; add only after confirmation"


def _product_evidence(candidate: dict, product: str) -> dict:
    """Return a candidate's evidence for one exact constrained product.

    The raw screen keeps ticker-wide flags for diagnostic ranking.  Decision
    lanes need the narrower answer: did the *same product* have a direct role
    and a company catalyst?  A fallback is retained only for older saved
    selector JSON that predates the product-evidence ledger.
    """
    evidence = (candidate.get("product_evidence") or {}).get(product)
    if evidence is not None:
        return evidence
    return {
        "order_book": bool(candidate.get("order_book")),
        "capex_signals": int(candidate.get("capex_signals") or 0),
        "types": list(candidate.get("types") or []),
        "demand_side": bool(candidate.get("any_demand_side")),
    }


def _company_capture_priority(
    candidate: dict,
    constraint: dict,
    product: str,
    maker: dict | None = None,
    producer_state: str | None = None,
) -> dict:
    """Rank eligible company/product tuples by visible economic transmission.

    The score is intentionally outcome-blind and has no eligibility authority.
    It is used only to order companies *after* the constraint, exact role and
    risk gates have run.  This keeps filing mention volume from outranking a
    producer that already shows same-product orders, earnings capture and
    current operating proof.
    """
    evidence = _product_evidence(candidate, product)
    quality = constraint.get("constraint_quality") or {}
    grade = quality.get("grade") or "UNMEASURED"
    role_types = set(evidence.get("types") or [])
    earnings_count = max(
        int(evidence.get("max_earnings_capture_count") or 0),
        int((maker or {}).get("earnings_capture_count") or 0),
    )
    physical_count = max(
        int(evidence.get("max_physical_evidence_count") or 0),
        int((maker or {}).get("physical_evidence_count") or 0),
    )
    pipeline_count = max(
        int(evidence.get("max_pipeline_evidence_count") or 0),
        int((maker or {}).get("pipeline_evidence_count") or 0),
    )
    capex_count = int(evidence.get("capex_signals") or 0)
    operating = producer_state == "OPERATING"
    pipeline = producer_state == "PIPELINE_OR_DIRECT_ROLE"
    exact_focus_count = len(candidate.get("corroborated_products") or [])
    response_state = str(
        (constraint.get("resolution_clock") or {}).get("supply_response_state") or "UNKNOWN"
    ).upper()

    components = {
        "constraint_quality": {"A": 14, "B": 10, "UNMEASURED": 4}.get(grade, 0),
        "binding_demand": 8 if quality.get("binding_demand") else 0,
        "operating_producer": 14 if operating else 0,
        "pipeline_or_direct_role": 6 if pipeline else 0,
        "direct_supply_role": 8 if role_types.intersection(
            {"direct_supplier", "critical_supplier"}
        ) else 0,
        "same_product_order_book": 16 if evidence.get("order_book") else 0,
        "earnings_capture": min(20, earnings_count * 5),
        "physical_role_proof": min(8, physical_count * 2),
        "pipeline_or_capex_proof": min(10, pipeline_count * 2 + capex_count * 2),
        "focused_exact_exposure": 8 if exact_focus_count == 1 else 4 if exact_focus_count == 2 else 0,
        "fresh_producer_identity": 4 if (maker or {}).get("producer_identity_fresh") is True else 0,
        "demand_side_penalty": -30 if evidence.get("demand_side") else 0,
        "resolution_penalty": (
            -25 if response_state in {"RESOLVED", "OVERCAPACITY"} else
            -12 if response_state in {"RESOLVING", "EASING"} else 0
        ),
        "risk_penalty": (
            -30 if candidate.get("risk_tier") == "HIGH" else
            -8 if candidate.get("risk_tier") == "ELEVATED" else 0
        ),
    }
    return {
        "version": COMPANY_CAPTURE_RANKING_VERSION,
        "score": max(0, min(100, sum(components.values()))),
        "product": product,
        "components": components,
        "selection_use": "rank eligible exact company-product tuples only; never creates eligibility",
    }


def _early_timing_candidates(candidates: list[dict], constraints: list[dict], top_n: int,
                             market_regime: dict | None = None) -> list[dict]:
    """Return a risk-budgeted early-investment lane, distinct from Core Buys.

    The physical-constraint lane requires an A/B measured constraint plus an
    as-of corroborated supply-side role and a real company catalyst (order book
    or repeated capex signal). It deliberately does not use raw score, breadth,
    generic policy mentions, or later outcomes as an eligibility shortcut.
    Unmeasured company/product discoveries remain research leads; they do not
    carry starter-position authority.
    """
    by_product = {c.get("constraint"): c for c in constraints if c.get("constraint")}
    # A mapper corroboration is a discovery signal, not literal issuer-role
    # proof.  Early position authority must agree with the strict dated maker
    # packet produced from company filings for the same product.
    strict_tickers_by_product = {
        constraint.get("constraint"): {
            (maker.get("ticker") or "").upper()
            for maker in (constraint.get("verified_current_makers") or [])
            if maker.get("ticker")
        }
        for constraint in constraints
        if constraint.get("constraint")
    }
    strict_makers_by_product = {
        constraint.get("constraint"): {
            (maker.get("ticker") or "").upper(): maker
            for maker in (constraint.get("verified_current_makers") or [])
            if maker.get("ticker")
        }
        for constraint in constraints
        if constraint.get("constraint")
    }
    grade_rank = {"A": 0, "B": 1, "UNMEASURED": 2, "WEAK": 3}
    rows = []
    # Constraint-driven physical-rent investing needs a measured constraint.
    # An unmeasured product with demand and a maker is a valuable research
    # lead, but it is not position authority. Policy/deployment starters belong
    # in their separately labelled mechanism lane, not under physical scarcity.
    allowed_grades = {"A", "B"}
    for raw_rank, candidate in enumerate(candidates, 1):
        ticker = (candidate.get("ticker") or "").upper()
        # Early eligibility is anchored to the *specific* product whose
        # supply-side role was corroborated in the company mapper.  A broad
        # ticker-level boolean is insufficient: one good product must not
        # legitimise an unrelated CRGO/PCB/solar association.
        # Early has a deliberately different evidence threshold from the
        # investment-committee lane.  It needs a dated, *exact* company ↔
        # product corroboration from the mapper, not the later, fresh
        # operating-maker packet that Core requires.  Reusing
        # ``verified_constraint_products`` here accidentally made Early
        # inherit Core's threshold and caused it to emit no candidates even
        # where the documented Early conditions were satisfied.
        #
        # Do not replace this with a ticker-level boolean.  A company proved
        # for one product must not receive an Early label for an adjacent one.
        products = [p for p in (candidate.get("corroborated_products") or [])
                    if p in by_product]
        eligible_constraints = [by_product[p] for p in products
                                if (by_product[p].get("early_investability_signal") and
                                    (by_product[p].get("constraint_quality") or {}).get("grade")
                                    in allowed_grades)]
        if not eligible_constraints:
            continue
        eligible_constraints.sort(key=lambda c: (
            grade_rank.get((c.get("constraint_quality") or {}).get("grade"), 3),
            not bool((c.get("constraint_quality") or {}).get("binding_demand")),
            c.get("constraint") or "",
        ))
        constraint = eligible_constraints[0]
        quality = constraint.get("constraint_quality") or {}
        selected_product = constraint.get("constraint")
        company_evidence = _product_evidence(candidate, selected_product)
        role_types = set(company_evidence.get("types") or [])
        policy_count = int((candidate.get("policy_evidence") or {}).get("n_policy_filings") or 0)
        capex_signals = int(company_evidence.get("capex_signals") or 0)
        product_order_book = bool(company_evidence.get("order_book"))
        capture_priority = _company_capture_priority(
            candidate,
            constraint,
            selected_product,
            strict_makers_by_product.get(selected_product, {}).get(ticker),
            "OPERATING",
        )
        failures = []
        if candidate.get("risk_tier") == "HIGH":
            failures.append("high governance/disclosure risk")
        if not candidate.get("corroborated_maker"):
            failures.append("no as-of corroborated supply-side company role")
        if ticker not in strict_tickers_by_product.get(selected_product, set()):
            failures.append("no fresh exact operating-maker proof for the selected product")
        if not role_types.intersection({"direct_supplier", "critical_supplier"}):
            failures.append("not a direct or critical supply-side role")
        if company_evidence.get("demand_side"):
            failures.append("demand-side mapping contamination")
        if not constraint.get("early_investability_signal"):
            failures.append("no as-of current demand/order signal for the selected constraint")
        if not product_order_book and capex_signals < 2:
            failures.append("no as-of order-book or repeated capex signal for the selected product")
        if failures:
            continue

        catalysts = []
        if product_order_book:
            catalysts.append(f"as-of {selected_product} order-book evidence")
        if capex_signals >= 2:
            catalysts.append(f"{capex_signals} mapped {selected_product} capex signal(s)")
        # Policy evidence is useful context only: unlike the two company
        # catalysts above, it cannot by itself admit a name to this lane.
        if policy_count:
            catalysts.append(f"{policy_count} company policy disclosure(s), product linkage to verify")
        missing = quality.get("missing_legs") or []
        constraint_grade = quality.get("grade") or "UNMEASURED"
        category = "Timing candidate — capped starter until earnings/entry confirmation"
        position_action = _regime_position_guidance(market_regime, "early")
        position_action += "; scale only after product-level order/revenue conversion"
        next_gate = "prove product-level revenue/order conversion and pricing or pass-through"
        if missing:
            next_gate += "; improve constraint evidence: " + ", ".join(missing)

        rows.append({
            "ticker": ticker,
            "company": candidate.get("company"),
            "category": category,
            "constraint": constraint.get("constraint"),
            "constraint_quality_grade": constraint_grade,
            "constraint_evidence_completeness": (quality.get("evidence_completeness") or {}).get("level"),
            "corroborated_products_for_lane": products,
            "company_catalyst_product": selected_product,
            "role": "corroborated " + "/".join(sorted(role_types.intersection(
                {"direct_supplier", "critical_supplier"}))),
            "catalysts": catalysts,
            "position_action": position_action,
            "confirmation_event": "product-level order/revenue conversion and pricing or pass-through",
            "next_gate": next_gate,
            "raw_screen_rank": raw_rank,
            "raw_composite_score": candidate.get("composite_score"),
            "technical": candidate.get("technical"),
            "risk_tier": candidate.get("risk_tier"),
            "company_capture_priority": capture_priority,
            "screen_coverage": "present in raw screen",
            "policy_context_count": policy_count,
            "_grade_rank": grade_rank.get(constraint_grade, 3),
            "_has_order_book": not product_order_book,
        })
    rows.sort(key=lambda r: (
        r["_grade_rank"], r["_has_order_book"],
        -int((r.get("company_capture_priority") or {}).get("score") or 0),
        -float(r.get("raw_composite_score") or 0), r["ticker"], r["constraint"],
    ))
    for row in rows:
        row.pop("_grade_rank")
        row.pop("_has_order_book")
    return rows[:top_n]


def _discovery_starter_candidates(
    candidates: list[dict], constraints: list[dict], top_n: int,
    market_regime: dict | None = None,
) -> list[dict]:
    """Return a small proof-led sleeve for constraints not yet measurable A/B.

    This is deliberately not a weakened Core gate.  It exists because an early
    physical bottleneck is often visible first in repeated issuer-owned role,
    capacity and order evidence across several companies, before public
    import/capacity tables catch up.
    Eligibility is one exact constraint-product-company tuple; unrelated noisy
    mappings on the same ticker neither admit nor veto it.
    """
    grade_rank = {"A": 0, "B": 1, "UNMEASURED": 2, "WEAK": 3}
    rows = []
    for constraint in constraints:
        product = constraint.get("constraint") or ""
        if not product:
            continue
        quality = constraint.get("constraint_quality") or {}
        grade = quality.get("grade") or "UNMEASURED"
        if grade == "WEAK" or not quality.get("binding_demand"):
            continue
        if not constraint.get("early_investability_signal"):
            continue
        operating = {
            (maker.get("ticker") or "").upper(): maker
            for maker in (constraint.get("verified_current_makers") or [])
            if maker.get("ticker")
        }
        pipeline = {
            (maker.get("ticker") or "").upper(): maker
            for maker in (constraint.get("pipeline_or_direct_role_makers") or [])
            if maker.get("ticker")
        }
        producer_tickers = set(operating) | set(pipeline)
        capture = constraint.get("company_capture_readiness") or {}
        catalyst_backed_roles = capture.get("catalyst_backed_exact_roles") or []
        strict_catalyst_tickers = {
            (row.get("ticker") or "").upper()
            for row in catalyst_backed_roles
            if (row.get("ticker") or "").upper() in producer_tickers
        }
        structured_constraint = bool(constraint.get("as_of_constraint_evidence"))
        company_triangulated = len(strict_catalyst_tickers) >= 2
        # Company-led detection is admissible only when at least two different
        # producer-ledger companies independently show same-product catalysts.
        # Mapper-only and quarantined rows cannot prove the bottleneck and then
        # use that self-created proof to receive position authority.
        if not structured_constraint and not company_triangulated:
            continue

        for raw_rank, candidate in enumerate(candidates, 1):
            ticker = (candidate.get("ticker") or "").upper()
            maker = operating.get(ticker) or pipeline.get(ticker)
            if not maker or candidate.get("risk_tier") != "NORMAL":
                continue
            evidence = _product_evidence(candidate, product)
            role_types = set(evidence.get("types") or [])
            if evidence.get("demand_side"):
                continue
            # The producer ledger is the role authority. Mapper role types add
            # context but cannot replace the exact issuer-owned maker packet.
            order_book = bool(evidence.get("order_book"))
            capex_signals = int(evidence.get("capex_signals") or 0)
            earnings_count = int((maker or {}).get("earnings_capture_count") or 0)
            pipeline_count = int((maker or {}).get("pipeline_evidence_count") or 0)
            # When the constraint itself is inferred from company
            # triangulation rather than independent structured evidence, the
            # selected company must prove its own economic transmission.  A
            # theme-wide order/capex signal cannot authorize capital for a
            # producer that has no repeated exact-product capture events.
            if not structured_constraint and earnings_count < 2:
                continue
            if not order_book and capex_signals < 2 and earnings_count == 0 and pipeline_count < 2:
                continue

            is_operating = ticker in operating
            is_pipeline = ticker in pipeline
            # Durable factory identity belongs in the producer universe, but
            # an old role packet cannot by itself authorize new capital.  The
            # company must refresh its exact-product operating/capture proof;
            # current theme demand elsewhere is not a substitute.
            if is_operating and maker.get("producer_identity_fresh") is False:
                continue
            role_state = (maker.get("adjudication_state") or
                          maker.get("maker_status") or
                          "EXACT_PRODUCER_ROLE")
            capture_priority = _company_capture_priority(
                candidate,
                constraint,
                product,
                maker,
                "OPERATING" if is_operating else "PIPELINE_OR_DIRECT_ROLE",
            )
            base_cap = 1.0 if grade == "UNMEASURED" else 1.5
            if (market_regime or {}).get("regime_label") == "BEAR_CAUTION":
                base_cap *= 0.5
            catalysts = []
            if order_book:
                catalysts.append(f"same-product {product} order/backlog evidence")
            if capex_signals >= 2:
                catalysts.append(f"{capex_signals} same-product capex signals")
            if earnings_count:
                catalysts.append(f"{earnings_count} issuer earnings-capture event(s)")
            if pipeline_count:
                catalysts.append(f"{pipeline_count} issuer capacity-pipeline event(s)")
            rows.append({
                "ticker": ticker,
                "company": candidate.get("company") or (maker or {}).get("company"),
                "category": "Discovery starter — proof-led, measurement incomplete",
                "constraint": product,
                "constraint_quality_grade": grade,
                "role_state": role_state,
                "producer_state": (
                    "OPERATING" if is_operating else "PIPELINE_OR_DIRECT_ROLE"
                ),
                "constraint_confirmation_state": (
                    "STRUCTURED_CONSTRAINT_EVIDENCE" if structured_constraint else
                    "TWO_OR_MORE_COMPANY_CATALYSTS"
                ),
                "independent_catalyst_company_count": len(strict_catalyst_tickers),
                "mapper_role_types": sorted(role_types),
                "catalysts": catalysts,
                "position_action": (
                    f"maximum {base_cap:.1f}% portfolio starter after valuation, liquidity and entry review; "
                    "do not average down without the confirmation event"
                ),
                "max_portfolio_weight_pct": base_cap,
                "confirmation_event": (
                    "reported product-level order/revenue conversion, commissioning or pricing evidence"
                ),
                "invalidation": (
                    "cancel/exit if the exact product role is withdrawn, commissioning slips materially, "
                    "orders fail to convert, the shortage resolves, or governance risk becomes elevated"
                ),
                "holding_horizon": (
                    "6–12 month discovery window; revalidate quarterly and either graduate to Early/Core or exit"
                ),
                "review_frequency": "quarterly and immediately after any material product, capacity or governance disclosure",
                "evidence_expiry_days": 120,
                "graduation_gate": (
                    "dated physical measurement plus product-level order/revenue conversion or operating commissioning proof"
                ),
                "why_not_core": (
                    "physical constraint measurement is incomplete" if grade == "UNMEASURED" else
                    "strict Core earnings/operating evidence is incomplete"
                ),
                "raw_screen_rank": raw_rank,
                "raw_composite_score": candidate.get("composite_score"),
                "risk_tier": candidate.get("risk_tier"),
                "technical": candidate.get("technical"),
                "company_capture_priority": capture_priority,
                "_score": int(capture_priority["score"]),
                "_role_rank": 0 if is_operating else 1,
                "_grade_rank": grade_rank.get(grade, 3),
            })

    rows.sort(key=lambda row: (
        row["_grade_rank"], row["_role_rank"], -row["_score"],
        -float(row.get("raw_composite_score") or 0), row["ticker"], row["constraint"],
    ))
    # Avoid handing the reader a pseudo-diversified list containing many names
    # for one chain.  At most two companies per exact constraint and five names
    # in the whole experimental sleeve.
    selected, counts, seen_tickers = [], {}, set()
    for row in rows:
        if row["ticker"] in seen_tickers or counts.get(row["constraint"], 0) >= 2:
            continue
        counts[row["constraint"]] = counts.get(row["constraint"], 0) + 1
        seen_tickers.add(row["ticker"])
        row.pop("_score")
        row.pop("_role_rank")
        row.pop("_grade_rank")
        selected.append(row)
        if len(selected) >= min(top_n, 5):
            break
    return selected


def _policy_research_watchlist(policy_screen: dict | None, candidates: list[dict],
                               top_n: int) -> list[dict]:
    """Surface generic policy discovery signals without turning policy into a Buy.

    A visible row needs both a policy signal and an independently corroborated
    company product role.  Unlinked policy activity remains in the JSON audit
    instead of becoming a client-facing claimant list.  A qualifying row is an
    auditable research prompt, not an economic-benefit claim: the next step
    still requires company-product and order/capacity proof.  This preserves
    early discovery while avoiding a hard-coded scheme/company exception list.
    """
    if not policy_screen:
        return []
    candidate_by_ticker = {(c.get("ticker") or "").upper(): c for c in candidates}
    by_ticker: dict[str, dict] = {}
    for bucket_name in ("qualified", "early_pings"):
        for scheme, entries in (policy_screen.get(bucket_name) or {}).items():
            for entry in entries or []:
                ticker = (entry.get("ticker") or "").upper()
                if not ticker:
                    continue
                committed = bool(entry.get("commitment"))
                recent_docs = int(entry.get("n_last12m") or 0)
                # A single generic mention is not an investable discovery
                # input.  Either a commitment-stage disclosure or two recent
                # company filings is required before it reaches the report.
                if not committed and recent_docs < 2:
                    continue
                raw = candidate_by_ticker.get(ticker) or {}
                # Keep unlinked policy activity in the internal audit.  A
                # visible research row still needs an independently
                # corroborated company product role; otherwise the reader is
                # handed a claimant/administrator list rather than an
                # investment-research universe.
                if not raw or not raw.get("corroborated_products"):
                    continue
                if raw.get("risk_tier") == "HIGH":
                    continue
                old = by_ticker.get(ticker)
                row = {
                    "ticker": ticker,
                    "company": raw.get("company"),
                    "scheme": scheme,
                    "signal_state": (
                        "company commitment-stage policy disclosure"
                        if committed else "rising company policy disclosure intensity"
                    ),
                    "first_mention": entry.get("first_mention"),
                    "recent_company_documents": recent_docs,
                    "commitment_documents": int((entry.get("commitment") or {}).get("n_commit_docs") or 0),
                    "raw_screen_coverage": "present in raw screen" if raw else "not yet in raw constraint screen",
                    "next_gate": "verify exact company product/capacity or order evidence, then link it to a demand constraint",
                    "research_status": "policy-led research prompt — not a Buy or beneficiary claim",
                    "_committed": committed,
                }
                if old is None or (row["_committed"], row["recent_company_documents"]) > (
                        old["_committed"], old["recent_company_documents"]):
                    by_ticker[ticker] = row
    rows = list(by_ticker.values())
    rows.sort(key=lambda r: (
        not r["_committed"], -r["recent_company_documents"],
        -r["commitment_documents"], r["ticker"],
    ))
    for row in rows:
        row.pop("_committed")
    return rows[:top_n]


def _stock_verdicts(
    rows: list[dict],
    priorities: list[dict],
    underwriting_candidates: list[dict],
    early_timing_candidates: list[dict],
    discovery_starter_candidates: list[dict],
) -> list[dict]:
    """Give every ranked company one mutually-exclusive position authority.

    The selector cannot establish a valuation, an investor's risk budget, or
    a trade execution plan.  It can, however, state exactly which evidence
    lane a stock cleared and prevent a research table from being mistaken for
    a Buy list.  ``CONDITIONAL_STARTER`` is intentionally distinct from an
    investment-committee hand-off and has a mandatory confirmation/entry
    review before a position may be opened.
    """
    priority_tickers = {(c.get("ticker") or "").upper() for c in priorities}
    underwriting_tickers = {(c.get("ticker") or "").upper() for c in underwriting_candidates}
    early_by_ticker = {
        (row.get("ticker") or "").upper(): row for row in early_timing_candidates
    }
    discovery_by_ticker = {
        (row.get("ticker") or "").upper(): row for row in discovery_starter_candidates
    }
    verdicts = []
    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        max_weight = None
        confirmation = None
        invalidation = None
        holding_horizon = None
        review_frequency = None
        evidence_expiry_days = None
        capture_priority = row.get("company_capture_priority") or {
            "version": COMPANY_CAPTURE_RANKING_VERSION,
            "score": 0,
            "product": None,
            "components": {},
            "selection_use": "rank eligible exact company-product tuples only; never creates eligibility",
        }
        products = (row.get("strict_verified_products") or
                    row.get("mapper_corroborated_products") or
                    row.get("products") or [])
        if ticker in priority_tickers:
            state = "IC_HANDOFF"
            authority = "NO AUTOMATIC POSITION — complete normal investment-committee, valuation, and entry review"
            action = "COMPLETE FULL UNDERWRITE"
            next_gate = (
                "validate product-level earnings capture, valuation, liquidity, and entry/risk plan; "
                "then obtain investment-committee approval"
            )
        elif ticker in early_by_ticker:
            early = early_by_ticker[ticker]
            capture_priority = early.get("company_capture_priority") or capture_priority
            state = "CONDITIONAL_STARTER"
            authority = (
                "LIMITED STARTER ONLY after valuation/entry review and a written confirmation/exit plan; "
                "never a full position from this report alone"
            )
            action = "CAPPED STARTER AFTER ENTRY REVIEW"
            next_gate = early.get("next_gate") or early.get("confirmation_event")
            confirmation = early.get("confirmation_event")
            products = [early.get("constraint")] if early.get("constraint") else products
        elif ticker in discovery_by_ticker:
            discovery = discovery_by_ticker[ticker]
            capture_priority = discovery.get("company_capture_priority") or capture_priority
            state = "DISCOVERY_STARTER"
            authority = (
                "EXPERIMENTAL STARTER ONLY after valuation/liquidity/entry review; obey the printed per-name cap "
                "and the 5% aggregate sleeve cap"
            )
            action = "CAPPED DISCOVERY STARTER AFTER ENTRY REVIEW"
            next_gate = discovery.get("confirmation_event")
            confirmation = discovery.get("confirmation_event")
            invalidation = discovery.get("invalidation")
            max_weight = discovery.get("max_portfolio_weight_pct")
            holding_horizon = discovery.get("holding_horizon")
            review_frequency = discovery.get("review_frequency")
            evidence_expiry_days = discovery.get("evidence_expiry_days")
            products = [discovery.get("constraint")] if discovery.get("constraint") else products
        elif ticker in underwriting_tickers:
            state = "UNDERWRITE"
            authority = "NO POSITION — research hand-off only"
            action = "REFRESH EVIDENCE, THEN UNDERWRITE"
            next_gate = (
                "recover fresh product-specific operating-maker proof; then validate earnings capture, "
                "valuation, and entry/risk plan"
            )
        else:
            state = "RESEARCH_ONLY"
            authority = "NO POSITION — raw-screen association has no investment authority"
            action = "DO NOT BUY FROM THIS LIST"
            next_gate = "; ".join(row.get("failed_gates") or []) or (
                "prove the exact company-product role, physical constraint, catalyst, and earnings capture"
            )
        verdicts.append({
            "ticker": ticker,
            "company": row.get("company"),
            "state": state,
            "position_authority": authority,
            "action": action,
            "constraints": products,
            "next_gate": next_gate,
            "confirmation_event": confirmation,
            "invalidation": invalidation,
            "max_portfolio_weight_pct": max_weight,
            "holding_horizon": holding_horizon,
            "review_frequency": review_frequency,
            "evidence_expiry_days": evidence_expiry_days,
            "raw_screen_rank": row.get("raw_screen_rank"),
            "risk_tier": row.get("risk_tier"),
            "company_capture_priority": capture_priority,
        })
    order = {"IC_HANDOFF": 0, "CONDITIONAL_STARTER": 1, "DISCOVERY_STARTER": 2,
             "UNDERWRITE": 3, "RESEARCH_ONLY": 4}
    return sorted(verdicts, key=lambda row: (
        order[row["state"]],
        -int((row.get("company_capture_priority") or {}).get("score") or 0),
        int(row.get("raw_screen_rank") or 999), row["ticker"]
    ))


def _constraint_decisions(
    constraints: list[dict], stock_verdicts: list[dict],
) -> list[dict]:
    """Make the constraint-to-company path explicit for every displayed theme."""
    decisions = []
    for constraint in constraints:
        product = constraint.get("constraint") or ""
        linked = [row for row in stock_verdicts if product in (row.get("constraints") or [])]
        by_state: dict[str, list[str]] = {}
        for row in linked:
            by_state.setdefault(row["state"], []).append(row["ticker"])
        quality = constraint.get("constraint_quality") or {}
        measurement = quality.get("measurement_coverage") or {}
        capture = constraint.get("company_capture_readiness") or {}
        populations = constraint.get("company_populations") or {}
        verified = [
            (maker.get("ticker") or "").upper()
            for maker in constraint.get("verified_current_makers") or []
            if maker.get("ticker")
        ]
        pipeline = [
            (maker.get("ticker") or "").upper()
            for maker in constraint.get("pipeline_or_direct_role_makers") or []
            if maker.get("ticker")
        ]
        if constraint.get("reference_only_constraint"):
            state = "MAP_COMPANIES"
            authority = "no position — a physical chain was detected before an exact listed-company supplier universe"
            next_proof = "recover and corroborate exact listed producers, then test product-level earnings capture"
        elif by_state.get("IC_HANDOFF"):
            state = "IC_HANDOFF"
            authority = "company may proceed to full investment-committee underwriting; no automatic position"
            next_proof = "complete valuation, earnings-capture, liquidity, and entry/risk review"
        elif by_state.get("CONDITIONAL_STARTER"):
            state = "CONDITIONAL_STARTER"
            authority = "only listed conditional-starter names may be considered for a limited starter after entry review"
            next_proof = next(
                (row.get("next_gate") for row in linked if row["state"] == "CONDITIONAL_STARTER"),
                "prove product-level order/revenue conversion and pricing or pass-through",
            )
        elif by_state.get("DISCOVERY_STARTER"):
            state = "DISCOVERY_STARTER"
            authority = (
                "only the named discovery-starter stock may enter the experimental sleeve after entry review; "
                "maximum 5% aggregate sleeve"
            )
            next_proof = next(
                (row.get("next_gate") for row in linked if row["state"] == "DISCOVERY_STARTER"),
                "obtain product-level conversion and dated physical measurement",
            )
        elif quality.get("company_selection_allowed"):
            state = "UNDERWRITE"
            authority = "no position — constraint is measured, but no company has cleared the strict evidence hand-off"
            next_proof = "recover an exact operating-maker packet and product-level earnings capture"
        elif constraint.get("as_of_constraint_evidence"):
            state = "RESEARCH_ONLY"
            authority = "no position — evidence supports a research thesis, not a trade"
            next_proof = "close the listed physical-quality and company-capture evidence gaps"
        else:
            state = "EXCLUDED"
            authority = "do not allocate research or capital until current constraint evidence is refreshed"
            next_proof = "refresh dated demand and physical-supply evidence"
        decisions.append({
            "constraint": product,
            "themes": constraint.get("themes") or [],
            "state": state,
            "position_authority": authority,
            "next_proof": next_proof,
            "physical_quality_grade": quality.get("grade") or "UNMEASURED",
            "measurement_coverage_status": measurement.get("status"),
            "measurement_display_label": measurement.get("display_label"),
            "research_priority_score": (constraint.get("constraint_research_priority") or {}).get("score"),
            "conditional_starter_tickers": by_state.get("CONDITIONAL_STARTER", []),
            "discovery_starter_tickers": by_state.get("DISCOVERY_STARTER", []),
            "ic_handoff_tickers": by_state.get("IC_HANDOFF", []),
            "underwrite_tickers": by_state.get("UNDERWRITE", []),
            "verified_producer_tickers": verified,
            "pipeline_or_direct_role_tickers": pipeline,
            "mapped_supply_or_deployment_tickers": [
                row.get("ticker") for row in populations.get("mapped_supply_or_deployment_leads") or []
                if row.get("ticker")
            ],
            "unverified_company_lead_tickers": [
                row.get("ticker") for row in populations.get("unverified_company_leads") or []
                if row.get("ticker")
            ],
            "quarantined_exception_tickers": [
                row.get("ticker") for row in populations.get("quarantined_exceptions") or []
                if row.get("ticker")
            ],
            "company_universe_status": populations.get("status"),
            "exact_mapper_role_tickers": [
                row.get("ticker") for row in capture.get("exact_supply_roles") or []
            ],
            "catalyst_backed_mapper_tickers": [
                row.get("ticker") for row in capture.get("catalyst_backed_exact_roles") or []
            ],
            "company_linkage_note": (
                "Verified producers and exact mapper roles are a company universe, not position authority. "
                "Only the state and named tickers above determine the report action."
            ),
        })
    order = {"IC_HANDOFF": 0, "CONDITIONAL_STARTER": 1, "DISCOVERY_STARTER": 2,
             "UNDERWRITE": 3, "MAP_COMPANIES": 4, "RESEARCH_ONLY": 5, "EXCLUDED": 6}
    return sorted(decisions, key=lambda row: (
        order[row["state"]], -(int(row.get("research_priority_score") or 0)), row["constraint"]
    ))


def build_final_selection(candidates: list[dict], constraints: list[dict], top_n: int = 8,
                          policy_screen: dict | None = None,
                          display_constraints: list[dict] | None = None,
                          market_regime: dict | None = None) -> dict:
    """Apply non-negotiable investment-readiness gates to raw candidates.

    The mechanical score is intentionally not an eligibility input. It can
    order candidates that pass as-of evidence gates, but it cannot repair a
    demand-side mapping or create a constraint. This gives the report a single
    source of truth at the requested date.
    """
    by_product = {c.get("constraint"): c for c in constraints if c.get("constraint")}
    rows, priorities, underwriting_candidates = [], [], []
    for raw_rank, candidate in enumerate(candidates, 1):
        ticker = (candidate.get("ticker") or "").upper()
        all_products = list(candidate.get("all_products") or candidate.get("products") or [])
        products = [p for p in all_products if p in by_product]
        evidence_products = [p for p in products if by_product[p].get("as_of_constraint_evidence")]
        decision_grade_products = [p for p in evidence_products
                                   if (by_product[p].get("constraint_quality") or {}).get("company_selection_allowed")]
        strict_proof_products = [
            p for p in decision_grade_products
            if ticker in {(m.get("ticker") or "").upper()
                          for m in by_product[p].get("verified_current_makers") or []}
        ]
        # Mapper corroboration is a useful (and historically point-in-time)
        # research signal, but it is not interchangeable with a fresh,
        # product-specific operating-maker evidence packet.  Retain both
        # states explicitly: the former can enter analyst underwriting; only
        # the latter can clear this mechanical hand-off to an investment
        # committee.
        mapper_corroborated_products = [
            p for p in decision_grade_products
            if p in (candidate.get("corroborated_products") or [])
        ]
        capture_rankings = {}
        for product in products:
            constraint = by_product[product]
            operating_maker = next((
                maker for maker in (constraint.get("verified_current_makers") or [])
                if (maker.get("ticker") or "").upper() == ticker
            ), None)
            pipeline_maker = next((
                maker for maker in (constraint.get("pipeline_or_direct_role_makers") or [])
                if (maker.get("ticker") or "").upper() == ticker
            ), None)
            capture_rankings[product] = _company_capture_priority(
                candidate,
                constraint,
                product,
                operating_maker or pipeline_maker,
                "OPERATING" if operating_maker else
                "PIPELINE_OR_DIRECT_ROLE" if pipeline_maker else None,
            )
        ranking_products = decision_grade_products or evidence_products or products
        best_capture = max(
            (capture_rankings[product] for product in ranking_products),
            key=lambda value: (int(value.get("score") or 0), value.get("product") or ""),
            default={
                "version": COMPANY_CAPTURE_RANKING_VERSION,
                "score": 0,
                "product": None,
                "components": {},
                "selection_use": "rank eligible exact company-product tuples only; never creates eligibility",
            },
        )
        candidate["company_capture_priority"] = best_capture
        candidate["company_capture_rankings"] = capture_rankings
        corroborated_role = bool(mapper_corroborated_products)
        # Exposure concentration is judged on exact, decision-grade product
        # links. A noisy adjacent mapper tag elsewhere on the ticker must not
        # veto a valid constraint-product-company tuple.
        focused_exposure = 0 < len(mapper_corroborated_products) <= 2
        failed = []
        if candidate.get("risk_tier") == "HIGH":
            failed.append("high governance/disclosure risk")
        if not products:
            failed.append("no constraint mapping survived product resolution")
        elif not evidence_products:
            failed.append("no as-of constraint evidence")
        elif not decision_grade_products:
            failed.append("constraint lacks an A/B measured physical-quality grade; unmeasured or weak physical constraints remain research-only")
        if evidence_products and not corroborated_role:
            failed.append("company role not corroborated by as-of maker evidence")
        if not focused_exposure:
            failed.append("no focused exact exposure among decision-grade constraint products")
        core_order_book_products = [
            product for product in mapper_corroborated_products
            if _product_evidence(candidate, product).get("order_book")
        ]
        # Demand-capture proof. An order book is the capital-goods signal that a
        # constraint has become contracted revenue. Process constraints (APIs,
        # bulk drugs, specialty chemicals) do not report order books, so the
        # equivalent proof is: the constraint itself carries CONFIRMED binding
        # demand in the curated exact-chain ledger (a high, analyst-reviewed bar)
        # AND this ticker is a strict verified operating/earnings producer of it.
        # Realized production into a confirmed-binding shortage IS demand capture;
        # requiring a backlog on top would structurally bar every process-industry
        # maker from position authority. Scoped to ledger-CONFIRMED constraints
        # only, so order-book-driven constraints (e.g. solar) are unaffected.
        ledger_binding_capture_products = [
            product for product in strict_proof_products
            if (by_product.get(product, {}).get("constraint_ledger") or {}).get(
                "binding_demand_status") == "CONFIRMED"
        ]
        demand_capture_products = core_order_book_products or ledger_binding_capture_products
        if not demand_capture_products:
            failed.append("no as-of demand-capture evidence (order book, or a confirmed "
                          "binding-demand ledger with a verified producer)")

        if not failed and strict_proof_products:
            status = "investment-committee candidate — complete normal underwriting and entry review"
            priorities.append(candidate)
        elif not failed:
            status = ("Core underwriting research candidate — refresh strict dated product-maker evidence "
                      "before an investment decision")
            underwriting_candidates.append(candidate)
        else:
            status = "blocked — " + "; ".join(failed)

        row = {
            "ticker": ticker,
            "company": candidate.get("company"),
            "raw_screen_rank": raw_rank,
            "raw_composite_score": candidate.get("composite_score"),
            "products": products,
            "decision_grade_products": decision_grade_products,
            "mapper_corroborated_products": mapper_corroborated_products,
            "product_order_book_evidence": core_order_book_products,
            "strict_verified_products": strict_proof_products,
            "verified_products": strict_proof_products,
            "status": status,
            "failed_gates": failed,
            "risk_tier": candidate.get("risk_tier"),
            "technical": candidate.get("technical"),
            "company_capture_priority": best_capture,
            "position_guidance": _regime_position_guidance(market_regime, "core"),
        }
        candidate["final_selection_status"] = status
        candidate["final_selection_failed_gates"] = failed
        candidate["verified_constraint_products"] = strict_proof_products
        candidate["mapper_corroborated_constraint_products"] = mapper_corroborated_products
        # Kept separately for audit/debugging: Early may only use the exact
        # mapped product, even when it cannot yet meet the later Core proof
        # packet.  This field never relaxes the Core gate above.
        candidate["early_role_products"] = [
            p for p in (candidate.get("corroborated_products") or []) if p in by_product
        ]
        rows.append(row)

    priorities.sort(key=lambda c: (
        -int((c.get("company_capture_priority") or {}).get("score") or 0),
        -float(c.get("composite_score") or 0), c.get("ticker") or "",
    ))
    underwriting_candidates.sort(
        key=lambda c: (
            -int((c.get("company_capture_priority") or {}).get("score") or 0),
            -float(c.get("composite_score") or 0), c.get("ticker") or "",
        )
    )
    early_timing_candidates = _early_timing_candidates(
        candidates, constraints, top_n=top_n, market_regime=market_regime
    )
    discovery_starter_candidates = _discovery_starter_candidates(
        candidates, constraints, top_n=top_n, market_regime=market_regime
    )
    policy_research_watchlist = _policy_research_watchlist(policy_screen, candidates, top_n=top_n)
    visible_constraints = list(display_constraints or constraints)
    visible_names = {row.get("constraint") for row in visible_constraints}
    # The decision page must show the evidence card for every constraint that
    # generated an actionable row, even if it ranked outside the compact top
    # constraint list.  This is how a downstream monetiser (for example an
    # equipment maker) remains auditable rather than disappearing behind a
    # more upstream raw-material label.
    decision_constraint_names = {
        row.get("constraint")
        for row in early_timing_candidates + discovery_starter_candidates
    }
    for candidate in priorities + underwriting_candidates:
        decision_constraint_names.update(
            p for p in (candidate.get("products") or []) if p in by_product)
    for name in sorted(decision_constraint_names):
        if name and name not in visible_names and name in by_product:
            visible_constraints.append(by_product[name])
            visible_names.add(name)
    raw_tickers = {(candidate.get("ticker") or "").upper() for candidate in candidates}
    best_stock_research_queue = _best_stock_research_queue(candidates, constraints, top_n=12)
    unmapped_maker_leads = []
    unmapped_pipeline_leads = []
    for constraint in constraints:
        if not constraint.get("as_of_constraint_evidence"):
            continue
        for maker in constraint.get("verified_current_makers") or []:
            ticker = (maker.get("ticker") or "").upper()
            if not ticker or ticker in raw_tickers:
                continue
            unmapped_maker_leads.append({
                "ticker": ticker,
                "company": maker.get("company"),
                "constraint": constraint.get("constraint"),
                "status": "verified maker missed by raw screen — full company research required",
                "last_evidence_date": maker.get("last_evidence_date"),
                "evidence_samples": maker.get("evidence_samples") or [],
            })
        # A dated capacity plan is not an operating-maker proof, so it cannot
        # feed the Buy gates above.  It is nevertheless exactly the kind of
        # early supply-side signal a raw score can miss (for example, a new
        # solar-cell line announced before commissioning).  Preserve it as a
        # clearly-labelled research lead instead of silently discarding it.
        for maker in constraint.get("pipeline_or_direct_role_makers") or []:
            ticker = (maker.get("ticker") or "").upper()
            if not ticker or ticker in raw_tickers:
                continue
            unmapped_pipeline_leads.append({
                "ticker": ticker,
                "company": maker.get("company"),
                "constraint": constraint.get("constraint"),
                "maker_status": maker.get("maker_status"),
                "research_route": maker.get("research_route"),
                "status": "as-of capacity/direct-role research lead — not a Buy until operating proof and company gates clear",
                "last_evidence_date": maker.get("last_evidence_date"),
                "evidence_samples": maker.get("evidence_samples") or [],
            })
    unmapped_maker_leads.sort(key=lambda row: (row["constraint"] or "", row["ticker"]))
    unmapped_pipeline_leads.sort(key=lambda row: (row["constraint"] or "", row["ticker"]))
    stock_verdicts = _stock_verdicts(
        rows, priorities, underwriting_candidates, early_timing_candidates,
        discovery_starter_candidates,
    )
    constraint_decisions = _constraint_decisions(visible_constraints, stock_verdicts)
    physical_reference_origins = {
        "CAPACITY_REFERENCE", "IMPORT_REFERENCE", "EXACT_LEDGER",
    }
    upstream_origin = "UPSTREAM_CONSTRAINT_PIPELINE"
    constraint_detection_coverage = {
        "constraint_universe_count": len(constraints),
        "displayed_constraint_count": len(visible_constraints),
        "mapper_detected_count": sum(
            "MAPPER" in (row.get("detection_origins") or []) for row in constraints
        ),
        "reference_detected_count": sum(
            bool(physical_reference_origins.intersection(row.get("detection_origins") or []))
            for row in constraints
        ),
        "upstream_pipeline_detected_count": sum(
            upstream_origin in (row.get("detection_origins") or [])
            for row in constraints
        ),
        "reference_only_count": sum(bool(row.get("reference_only_constraint")) for row in constraints),
        "structured_constraint_evidence_count": sum(
            bool(row.get("as_of_constraint_evidence")) for row in visible_constraints
        ),
        "strict_company_role_packet_count": sum(
            bool((row.get("verified_current_makers") or []) or
                 (row.get("pipeline_or_direct_role_makers") or []))
            for row in visible_constraints
        ),
        "company_triangulated_constraint_count": sum(
            len({
                (company.get("ticker") or "").upper()
                for company in ((row.get("company_capture_readiness") or {})
                                .get("catalyst_backed_exact_roles") or [])
                if company.get("ticker")
            }) >= 2
            for row in visible_constraints
        ),
        "method": (
            "The detector unions mapper products, the country-scoped ingestion/NLP candidate snapshot, dated "
            "capacity/import references where available, and reviewed exact-chain ledger aliases. Upstream or "
            "reference-only chains remain research coverage and cannot create a stock decision until the separate "
            "constraint and exact listed-company evidence gates clear."
        ),
    }
    covered_for_decision = sum(
        bool(row.get("as_of_constraint_evidence")) or
        len({
            (company.get("ticker") or "").upper()
            for company in ((row.get("company_capture_readiness") or {})
                            .get("catalyst_backed_exact_roles") or [])
            if company.get("ticker")
        }) >= 2
        for row in visible_constraints
    )
    visible_count = len(visible_constraints)
    coverage_pct = round(100 * covered_for_decision / visible_count, 1) if visible_count else 0.0
    constraint_detection_coverage["decision_evidence_coverage_pct"] = coverage_pct
    constraint_detection_coverage["coverage_health"] = (
        "ADEQUATE" if coverage_pct >= 60 else
        "PARTIAL" if coverage_pct >= 30 else "SPARSE"
    )
    decision_summary = {
        "position_authority_policy": (
            "The decision card displays every state. IC_HANDOFF and CONDITIONAL_STARTER can progress through the strict "
            "investing workflow. IC_HANDOFF still requires normal investment-committee, valuation, and entry review. "
            "CONDITIONAL_STARTER permits only a limited starter after valuation/entry review and a written "
            "confirmation/exit plan. DISCOVERY_STARTER is a separate experimental sleeve: no more than the printed "
            "per-name cap and 5% in aggregate, and only after valuation/liquidity/entry review. UNDERWRITE, RESEARCH_ONLY, "
            "producer universes, policy, PLI, quarantine, and raw-screen sections have no position authority."
        ),
        "ic_handoff_count": sum(row["state"] == "IC_HANDOFF" for row in stock_verdicts),
        "conditional_starter_count": sum(row["state"] == "CONDITIONAL_STARTER" for row in stock_verdicts),
        "discovery_starter_count": sum(row["state"] == "DISCOVERY_STARTER" for row in stock_verdicts),
        "underwrite_count": sum(row["state"] == "UNDERWRITE" for row in stock_verdicts),
        "research_only_count": sum(row["state"] == "RESEARCH_ONLY" for row in stock_verdicts),
        "research_priority_note": (
            "Constraint research-priority scores rank evidence work only; they do not create investment eligibility."
        ),
    }
    actionable_states = {"IC_HANDOFF", "CONDITIONAL_STARTER", "DISCOVERY_STARTER"}
    final_investable_shortlist = [
        row for row in stock_verdicts if row.get("state") in actionable_states
    ]
    discovery_cap_used = round(sum(
        float(row.get("max_portfolio_weight_pct") or 0)
        for row in final_investable_shortlist
        if row.get("state") == "DISCOVERY_STARTER"
    ), 2)
    decision_summary["final_investable_count"] = len(final_investable_shortlist)
    decision_summary["discovery_maximum_new_capital_pct"] = discovery_cap_used
    decision_summary["coverage_health"] = constraint_detection_coverage["coverage_health"]
    decision_summary["empty_list_instruction"] = (
        "NO ALLOCATION FROM THIS STRATEGY; do not promote a research-only row"
        if not final_investable_shortlist else None
    )
    return {
        "method": (
            "The report date is the decision date. An investment-committee candidate needs an A/B *measured physical-quality* "
            "constraint, fresh product-specific operating-maker proof, focused exact-product exposure, order-book evidence, and no high-risk disclosure. "
            "Mapper corroboration without that strict evidence may enter analyst underwriting, never a Buy-style result. "
            "UNMEASURED means physical scarcity was not captured in the as-of reference data; it is not a weak-quality verdict, but it has "
            "no position authority in the physical-constraint lane. Early/Timing candidates require an A/B measured constraint, a dated "
            "exact operating-maker role, and an as-of order-book or repeated-capex catalyst; they are capped starters after normal "
            "risk/valuation underwriting until their stated confirmation. A separately capped Discovery Starter lane may admit an unmeasured "
            "constraint only when dated binding-demand evidence and either structured constraint evidence or two independent producer-ledger company "
            "catalysts agree. Each stock also needs an exact issuer-owned operating/pipeline role in the producer ledger, a same-product "
            "company catalyst, and NORMAL risk. Mapper-only and quarantined roles have no position authority. The lane is capped at five names; "
            "producer-ledger names receive at most 1.0% when unmeasured (1.5% if A/B), and the sleeve is capped at 5% in aggregate. "
            "Market regime adjusts position size and staging, never eligibility "
            "or constraint quality. "
            "Raw scores only order names after a lane's evidence gates clear."
        ),
        "constraints": visible_constraints,
        "constraint_detection_coverage": constraint_detection_coverage,
        "constraint_decisions": constraint_decisions,
        "stock_verdicts": stock_verdicts,
        "final_investable_shortlist": final_investable_shortlist,
        "decision_summary": decision_summary,
        "priorities": [
            next(row for row in rows if row["ticker"] == (c.get("ticker") or "").upper())
            for c in priorities[:top_n]
        ],
        "underwriting_candidates": [
            next(row for row in rows if row["ticker"] == (c.get("ticker") or "").upper())
            for c in underwriting_candidates[:top_n]
        ],
        "blocked_candidates": [row for row in rows if row["failed_gates"]][:top_n],
        "eligible_tickers": [(c.get("ticker") or "").upper() for c in priorities[:top_n]],
        "investment_committee_candidate_tickers": [(c.get("ticker") or "").upper() for c in priorities[:top_n]],
        "underwriting_candidate_tickers": [(c.get("ticker") or "").upper()
                                              for c in underwriting_candidates[:top_n]],
        "early_timing_candidates": early_timing_candidates,
        "early_timing_tickers": [row["ticker"] for row in early_timing_candidates],
        "discovery_starter_candidates": discovery_starter_candidates,
        "discovery_starter_tickers": [row["ticker"] for row in discovery_starter_candidates],
        "discovery_sleeve_max_portfolio_weight_pct": 5.0,
        "policy_research_watchlist": policy_research_watchlist,
        "best_stock_research_queue": best_stock_research_queue,
        "unmapped_maker_leads": unmapped_maker_leads[:top_n],
        "unmapped_pipeline_leads": unmapped_pipeline_leads[:top_n],
        "raw_screen_note": "Raw candidates are diagnostics only; they are never Buy recommendations.",
        "no_investment_committee_candidates": not priorities,
        "no_actionable_candidates": (
            not priorities and not early_timing_candidates and not discovery_starter_candidates
        ),
    }
