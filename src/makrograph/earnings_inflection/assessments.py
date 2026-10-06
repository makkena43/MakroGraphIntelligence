"""Evidence-only assessment with separate evidence, review and scenario statuses.

The evidence status ladder (weakest to strongest):

  INSUFFICIENT_EVIDENCE  - no usable, public, dated sources
  NO_MATERIAL_CHANGE     - sources exist, nothing material detected
  ASSERTION_ONLY         - only forward-looking management statements
  COMMITMENT_BACKED      - binding/provisional commercial commitments material
                           relative to revenue, not yet visible in results
  EXECUTION_EMERGING     - one quarter of material realized change
  EXECUTION_CONFIRMED    - >= 2 consecutive periods (quarters, or half-years for
                           half-yearly SME reporters) of material realized change
                           (no guidance required: silent management can qualify)
  CONTRADICTED           - the company missed even its latest stated guidance, or
                           made >= 2 downward revisions/withdrawals with no stated
                           reason.  A single revision, or any revision with a
                           stated reason, is disclosed for human review instead:
                           conservative guidance and justified revisions are not
                           penalised automatically.

``review_status`` is always UNREVIEWED (or NEEDS_SOURCE_CHECK) from the
pipeline - only a human changes it.  None of these states is an action.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .demand import summarise_demand
from .mechanisms import REALIZED_UPGRADING
from .contracts import (
    ChangeFinding, CommitmentStrength, Mechanism, MechanismResult, MechanismState, CustomerVerification, DriverChange, EarningsBridge, EconomicEvent, EventStage, Evidence, EvidenceStatus, EvidenceTier, GuidanceOutcome, GuidanceRecord, IssuerModel, Metric, Modality, RelationshipStatus, ReviewStatus, RevisionDirection, Unit, ValueBasis,
)


UNEXPLAINED_REVISIONS_LIMIT = 2   # repeated unexplained downward revisions -> CONTRADICTED


def _driver(drivers: list[DriverChange], name: str) -> Optional[DriverChange]:
    return next((d for d in drivers if d.driver == name), None)


def decide_status(drivers: list[DriverChange], events: list[EconomicEvent], evidence: list[Evidence],
                  guidance: list[GuidanceRecord], usable_docs: int,
                  ttm_revenue: Optional[float], as_of: Optional[datetime] = None,
                  event_lookback_days: int = 365,
                  mechanisms: Optional[list[MechanismResult]] = None) -> tuple[EvidenceStatus, list[str]]:
    why: list[str] = []
    if usable_docs == 0:
        return EvidenceStatus.INSUFFICIENT_EVIDENCE, ["no public, dated, usable documents by as-of"]

    missed_latest = [g for g in guidance if g.latest_outcome == GuidanceOutcome.MISSED]
    # Statements without a target period cannot be compared with each other (they may concern
    # different years), so they never count as revisions of one target.
    unexplained = [(g, r) for g in guidance for r in g.revisions
                   if g.target_period_label != "unspecified"
                   and r.direction in (RevisionDirection.LOWERED, RevisionDirection.WITHDRAWN) and not r.explained]
    if missed_latest or len(unexplained) >= UNEXPLAINED_REVISIONS_LIMIT:
        for g in missed_latest:
            what = "missed even the latest stated guidance" if g.revisions else "missed its guidance (never revised)"
            why.append(f"{g.metric.value} {g.target_period_label}: {what} ({g.outcome_note})")
        if len(unexplained) >= UNEXPLAINED_REVISIONS_LIMIT:
            why.append(f"{len(unexplained)} downward revisions/withdrawals without a stated reason: "
                       + "; ".join(f"{g.metric.value} {g.target_period_label} on "
                                   f"{r.stated_at.date() if r.stated_at else '?'}" for g, r in unexplained))
        return EvidenceStatus.CONTRADICTED, why
    for g in guidance:
        if any(r.direction in (RevisionDirection.LOWERED, RevisionDirection.WITHDRAWN) for r in g.revisions):
            why.append(f"note: {g.metric.value} {g.target_period_label} guidance was revised down "
                       f"(not treated as contradiction; see guidance flags)")

    streak = _driver(drivers, "material_growth_streak")
    margin = _driver(drivers, "ebitda_margin_change")
    accel = _driver(drivers, "revenue_growth_acceleration")
    material_realized = [d for d in drivers if d.material and d.driver in
                         ("revenue_yoy_growth", "revenue_growth_acceleration", "ebitda_margin_change",
                          "pat_yoy_growth", "operating_leverage", "pat_loss_to_profit", "pat_low_base")]
    if margin and margin.material and margin.change is not None and margin.change < 0:
        material_realized = [d for d in material_realized if d.driver != "ebitda_margin_change"]
        why.append(f"EBITDA margin contracted {margin.change:.0f} bps YoY")

    # Mechanism-specific confirmation (WP5): e.g. gross-margin gains in two consecutive periods
    # confirm their own mechanism even with low sales growth.  Order quality and debt reduction
    # never upgrade the status by themselves; utilisation only with a realized milestone.
    mech_pos = [m for m in (mechanisms or []) if m.qualifies_positive and (
        m.mechanism in REALIZED_UPGRADING or (m.mechanism == Mechanism.UTILIZATION
                                              and m.state == MechanismState.CONFIRMED))]
    mech_confirmed = [m for m in mech_pos if m.state == MechanismState.CONFIRMED]
    for m in (mechanisms or []):
        if m.state == MechanismState.ADVERSE:
            why.append(f"adverse mechanism {m.mechanism.value}{' (stale reading)' if m.stale else ''}: "
                       f"{m.magnitude_basis or m.attribution}")
    if mech_confirmed:
        why += [f"mechanism {m.mechanism.value} confirmed ({m.durability}): {m.magnitude_basis}" for m in mech_confirmed]
        why += [f"{d.driver}: {d.change:.1f} {d.unit}" for d in material_realized if d.change is not None]
        return EvidenceStatus.EXECUTION_CONFIRMED, why
    if streak and streak.current and streak.current >= 2 and material_realized:
        why.append(f"{int(streak.current)} consecutive {streak.unit} of material revenue growth")
        why += [f"{d.driver}: {d.change:.1f} {d.unit}" for d in material_realized if d.change is not None]
        return EvidenceStatus.EXECUTION_CONFIRMED, why
    if material_realized:
        why += [f"{d.driver}: {d.change:.1f} {d.unit}" for d in material_realized if d.change is not None]
        why += [f"mechanism {m.mechanism.value} emerging: {m.magnitude_basis}" for m in mech_pos]
        why.append(f"single {streak.unit.rstrip('s') if streak else 'period'} so far; persistence unproven")
        return EvidenceStatus.EXECUTION_EMERGING, why

    # Commitments: only events whose binding state falls in the look-back window count
    # (an old order says nothing about a current inflection).  Only validated binding
    # EXTERNAL orders with a named customer and a firm value support a verified
    # commitment; provisional / anonymous / ceiling / framework events stay visible
    # in the early lane with their limitations, never upgraded or discarded.
    as_of_d = as_of.date() if as_of is not None else max(
        [e.first_public_at.date() for e in events if e.first_public_at] or [datetime.max.date()])
    dem = summarise_demand(events, evidence, as_of_d, event_lookback_days)
    verified = [e for e in events if e.event_id in dem.verified_events]
    btb = _driver(drivers, "disclosed_order_inflow_to_ttm_revenue")
    cover = _driver(drivers, "order_book_cover")
    if verified and ((btb and btb.material) or (cover and cover.material)
                     or (ttm_revenue and dem.verified_inflow_crore >= 0.25 * ttm_revenue)):
        why.append(f"{len(verified)} verified binding external order event(s) in the last {event_lookback_days} "
                   f"days, {dem.verified_inflow_crore:.1f} cr current value (deduplicated)")
        why += dem.notes()
        return EvidenceStatus.COMMITMENT_BACKED, why
    if dem.unverified_events or (verified and not ttm_revenue):
        material = (not ttm_revenue) or \
            (dem.unverified_inflow_crore + dem.verified_inflow_crore) >= 0.25 * ttm_revenue
        if material:
            for eid, reasons in dem.unverified_events.items():
                why.append(f"early-lane order {eid}: " + "; ".join(reasons))
            if verified and not ttm_revenue:
                why.append(f"{len(verified)} verified order(s) but no current revenue base: materiality unknown")
            if not ttm_revenue:
                why.append("no current revenue base, so materiality cannot be judged")
            why.append("not a verified commitment: needs a binding order from a named external customer")
            return EvidenceStatus.EARLY_COMMITMENT_UNVERIFIED, why
    if dem.related_party_excluded:
        why.append(f"{len(dem.related_party_excluded)} related-party / own-group order(s) excluded from external demand")

    assertions = [e for e in evidence if e.usable and e.tier == EvidenceTier.MANAGEMENT_ASSERTION
                  and e.modality in (Modality.FORWARD, Modality.CONDITIONAL)]
    if assertions:
        why.append(f"{len(assertions)} forward-looking management statement(s) without realized/commitment support")
        return EvidenceStatus.ASSERTION_ONLY, why
    if not drivers:
        return EvidenceStatus.INSUFFICIENT_EVIDENCE, why + ["no current comparable financial series and no qualifying evidence"]
    return EvidenceStatus.NO_MATERIAL_CHANGE, why + ["no driver crossed materiality thresholds"]


def review_status_for(evidence: list[Evidence], kind_bases: list[str]) -> ReviewStatus:
    if any(e.extractor == "llm" for e in evidence) or any(b.startswith("title_only") for b in kind_bases):
        return ReviewStatus.NEEDS_SOURCE_CHECK
    return ReviewStatus.UNREVIEWED


def what_changed(drivers, events, guidance, evidence, first_public: dict[str, datetime],
                 as_of: Optional[datetime] = None, event_lookback_days: int = 365) -> list[ChangeFinding]:
    out: list[ChangeFinding] = []
    for d in drivers:
        if d.material:
            # earliest defensible detection = latest availability of every required input
            when = d.knowable_at or max((first_public[x] for x in d.source_doc_ids if x in first_public),
                                        default=None)
            prior = f"{d.prior:.2f}" if d.prior is not None else "-"
            if d.unit == "bps" and d.current is not None and d.change is not None:
                # levels are percentages; the change is in basis points
                text = f"{d.driver}: {prior}% -> {d.current:.2f}% ({d.change:+.0f} bps; {d.basis})"
            elif d.current is not None:
                text = f"{d.driver}: {prior} -> {d.current:.2f} {d.unit} ({d.basis})"
            else:
                text = d.driver
            out.append(ChangeFinding(
                what=text,
                business="company-level (series scope)" if d.period_end else "as stated in source",
                first_public_at=when, tier=EvidenceTier.REALIZED_EXECUTION
                if d.driver not in ("disclosed_order_inflow_to_ttm_revenue", "order_book_cover", "capacity_utilization_change")
                else EvidenceTier.COMMERCIAL_COMMITMENT if d.driver != "capacity_utilization_change"
                else EvidenceTier.MANAGEMENT_ASSERTION))
    for e in events:
        amt = f"{e.amount.value:g} {e.amount.unit.value}" if e.amount else "unquantified"
        if e.original_amount and e.amount and abs(e.original_amount.value - e.amount.value) > 1e-9:
            amt += f" (originally {e.original_amount.value:g})"
        stage = e.current_stage.value if e.current_stage else e.commitment_strength.value
        out.append(ChangeFinding(
            what=f"order event {e.event_id}: {amt} from {e.counterparty or 'unnamed counterparty'} "
                 f"(stage {stage}; customer {e.customer_verification.value}; relationship {e.relationship.value}; "
                 f"value {e.value_basis.value}; {len(e.doc_ids)} document(s) describe it - one event)"
                 + (f" [older than {event_lookback_days} days; not counted in status]"
                    if as_of and e.first_public_at and (as_of - e.first_public_at).days > event_lookback_days else ""),
            business=next((x.segment for x in evidence if x.evidence_id in e.evidence_ids and x.segment), "") or "unspecified",
            first_public_at=e.first_public_at,
            tier=EvidenceTier.COMMERCIAL_COMMITMENT if e.commitment_strength != CommitmentStrength.NON_BINDING
            else EvidenceTier.MANAGEMENT_ASSERTION,
            evidence_ids=list(e.evidence_ids)))
    for g in guidance:
        q = g.original.quantity
        out.append(ChangeFinding(
            what=f"{g.metric.value} for {g.target_period_label}: {q.raw if q else '?'} "
                 f"(outcome {g.outcome.value}; {len(g.revisions)} later statement(s))",
            business="company-level", first_public_at=g.original.stated_at,
            tier=EvidenceTier.MANAGEMENT_ASSERTION, evidence_ids=[g.original.evidence_id]))
    out.sort(key=lambda c: (c.first_public_at is None, c.first_public_at or datetime.max))
    return out


def financing_risks(evidence: list[Evidence], ttm_revenue: Optional[float], bridge: EarningsBridge,
                    drivers: Optional[list[DriverChange]] = None) -> list[str]:
    risks = []
    for d in drivers or []:
        if d.driver == "debt_change" and d.current is not None and d.prior is not None:
            risks.append(f"{d.basis}: {d.prior:,.1f} -> {d.current:,.1f} cr" + (f" ({'; '.join(d.notes)})"
                                                                               if d.notes else ""))
        if d.driver == "cash_conversion" and d.current is not None and d.current < 0.5:
            risks.append(f"cash conversion {d.current:.2f}x ({d.basis}): profit not yet backed by operating cash")
        if d.driver == "share_count_change":
            risks.append(f"share count {d.prior:.3f} -> {d.current:.3f} crore ({d.basis}); dilution or buyback")
    capex = [e for e in evidence if e.usable and e.metric == Metric.CAPEX and e.quantity and e.quantity.unit == Unit.INR_CRORE]
    funds = [e for e in evidence if e.usable and e.metric == Metric.FUNDRAISE]
    if capex:
        tot = max(e.quantity.value for e in capex)
        msg = f"stated capex up to {tot:g} cr"
        if ttm_revenue:
            msg += f" ({tot / ttm_revenue:.0%} of TTM revenue)"
        risks.append(msg + "; funding source must be verified against cash flow statement")
    if funds:
        risks.append(f"{len(funds)} fund-raise / warrant / preferential-issue mention(s): dilution affects diluted EPS")
    for s in bridge.scenarios:
        if s.recurring_pat_attributable_crore is not None and s.recurring_pat_attributable_crore < 0:
            risks.append(f"scenario {s.name} implies losses at current cost structure")
    neg_cash = [e for e in evidence if e.usable and e.metric == Metric.OPERATING_CASH_FLOW
                and (e.modality == Modality.NEGATED or re.search(r"negative|outflow|stretched|working capital", e.quote, re.I))]
    if neg_cash:
        risks.append("operating cash flow / working-capital strain mentioned")
    return risks



def next_checks(drivers, events, guidance, missing, status: EvidenceStatus) -> list[str]:
    checks = []
    for g in guidance:
        for r in g.revisions:
            if r.direction not in (RevisionDirection.LOWERED, RevisionDirection.WITHDRAWN):
                continue
            if r.explained:
                checks.append(f"Judge whether the stated reason for the {g.metric.value} {g.target_period_label} "
                              f"revision is credible: \"{r.explanation[:160]}\"")
            else:
                checks.append(f"Find management's reason for the {g.metric.value} {g.target_period_label} revision "
                              f"on {r.stated_at.date() if r.stated_at else '?'} (none found near the statement)")
        if any("disappearing" in f for f in g.flags):
            checks.append(f"Ask/check whether the {g.metric.value} {g.target_period_label} target still stands "
                          "(not repeated in later commentary)")
        if g.outcome == GuidanceOutcome.PENDING:
            checks.append(f"Compare {g.metric.value} for {g.target_period_label} with the original statement "
                          f"({g.original.quantity.raw if g.original.quantity else ''}) when results are published")
    for e in events:
        who = e.counterparty or "unnamed customer"
        if e.current_stage in (EventStage.CANCELLED, EventStage.EXPIRED):
            continue
        if e.commitment_strength == CommitmentStrength.PROVISIONAL:
            checks.append(f"Confirm conversion of provisional order {e.event_id} ({who}) into a firm order")
        elif e.commitment_strength == CommitmentStrength.BINDING:
            checks.append(f"Track execution of order {e.event_id} ({who}) in segment revenue / order book")
        if e.customer_verification == CustomerVerification.ANONYMOUS:
            checks.append(f"Identify the customer behind order {e.event_id} (identity undisclosed; unverified)")
        if e.relationship in (RelationshipStatus.UNKNOWN, RelationshipStatus.ISSUER_ASSERTED_UNRELATED) \
                and e.counterparty:
            checks.append(f"Find an attributable, dated source on whether {who} is related to the issuer "
                          f"(currently {e.relationship.value})")
        if e.value_basis in (ValueBasis.CEILING, ValueBasis.UNQUANTIFIED):
            checks.append(f"Look for executable releases / quantities under {e.event_id} (ceiling or unquantified)")
        if e.ambiguous_with:
            checks.append(f"Resolve whether {e.event_id} duplicates {', '.join(e.ambiguous_with)}")
    if status in (EvidenceStatus.EXECUTION_EMERGING, EvidenceStatus.COMMITMENT_BACKED):
        unit = next((d.unit.rstrip("s") for d in drivers if d.driver == "material_growth_streak"), "quarter")
        checks.append(f"Next {'half-yearly' if unit == 'half-year' else 'quarterly'} results: "
                      f"does the change persist for a second consecutive {unit}?")
    m = _driver(drivers, "ebitda_margin_change")
    if m and m.material and m.change and m.change > 0:
        checks.append("Check whether margin gain is mix/operating leverage vs one-off (raw material, other income, provisions)")
    for x in missing[:5]:
        checks.append(f"Obtain missing input: {x}")
    return list(dict.fromkeys(checks))


STANDARD_LIMITATIONS = [
    "Deterministic lexical extraction can miss or mis-scope statements; every quote should be read in its source.",
    "Disclosed order announcements are a subset of true order inflow; materiality thresholds are descriptive only.",
    "filed_at-only documents are treated as public at end of the filing day (IST); intraday timing is unknown.",
    "No statistical validation: states are not evidence of future returns and earlier manual case studies are not a backtest.",
    "Exceptional-item sign convention assumed (positive = charge) when deriving recurring PAT.",
    "USD amounts are not converted (no dated FX series wired).",
]


def limitations_for(issuer_model: IssuerModel, identity_basis: str, coverage: dict) -> list[str]:
    lim = list(STANDARD_LIMITATIONS)
    if issuer_model.is_financial:
        lim.insert(0, f"{issuer_model.value}: operating-margin bridge unsupported (UNSUPPORTED_FINANCIAL_MODEL)")
    if issuer_model == IssuerModel.UNKNOWN:
        lim.insert(0, "issuer business model not classified; bridge computed under operating-company assumptions")
    if identity_basis == "symbol_assumed_stable":
        lim.append("No dated symbol history supplied; ticker assumed to identify one issuer across the window.")
    if coverage.get("excluded_unknown_time"):
        lim.append(f"{coverage['excluded_unknown_time']} document(s) excluded: no publication timestamp.")
    if coverage.get("no_text"):
        lim.append(f"{coverage['no_text']} document(s) had no extractable text (scanned/unparsed).")
    if coverage.get("title_only_classified"):
        lim.append(f"{coverage['title_only_classified']} document(s) classified by title only.")
    return lim
