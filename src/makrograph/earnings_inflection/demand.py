"""External demand: inflow, backlog and cancellations kept separate (WP4).

* Inflow = eligible NEW events whose first binding state falls in the
  look-back window, valued at their CURRENT amount (after amendments and
  partial cancellations).  Ambiguous possible duplicates count once.
* Backlog = a dated outstanding order-book snapshot stated by the company,
  used only while fresh.  It is never added to inflow.
* Cancellations are reported separately.
* Related-party / own-group orders, ceilings, unquantified frameworks,
  provisional (L1/LoI) and anonymous-customer orders are excluded from the
  VERIFIED total and listed with the reason; provisional/anonymous/ceiling
  events remain visible in the early (unverified) lane.
* A long contract is not annual revenue: an annual executable estimate is
  computed only when an execution period is disclosed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .contracts import (
    CustomerVerification, EconomicEvent, EventStage, Evidence, Metric, RelationshipStatus, TaxBasis, Unit,
    ValueBasis,
)

BINDING_STAGES = (EventStage.BINDING_ORDER, EventStage.EXECUTION)


def _d(x) -> Optional[date]:
    if x is None:
        return None
    return x.date() if isinstance(x, datetime) else x


def normalise_legacy(e: EconomicEvent) -> EconomicEvent:
    """Events built without WP4 fields (no history) get stage / verification / value
    basis derived from their legacy strength, counterparty and amount."""
    if e.history:
        return e
    from .contracts import CommitmentStrength, EventStateChange
    e.current_stage = e.current_stage or {CommitmentStrength.BINDING: EventStage.BINDING_ORDER,
                                          CommitmentStrength.PROVISIONAL: EventStage.PREFERRED_BIDDER,
                                          CommitmentStrength.NON_BINDING: EventStage.MOU_FRAMEWORK
                                          }.get(e.commitment_strength, EventStage.PREFERRED_BIDDER)
    e.history = [EventStateChange(e.current_stage, e.first_public_at, (e.doc_ids or [""])[0], "",
                                  e.amount.value if e.amount else None, "legacy event")]
    if e.counterparty and e.customer_verification == CustomerVerification.ANONYMOUS:
        e.customer_verification = CustomerVerification.ISSUER_NAMED
    if e.amount is not None and e.value_basis == ValueBasis.UNQUANTIFIED:
        e.value_basis = ValueBasis.FIRM
    e.original_amount = e.original_amount or e.amount
    return e


def event_state_at(e: EconomicEvent, t: datetime) -> Optional[EconomicEvent]:
    """The event as it stood at ``t``: dated history truncated, stage / amount / cancelled value
    re-derived from it.  None if the event was not public yet.  (Verification and relationship
    are not dated on the event itself; callers that need them point-in-time rebuild events from
    the evidence public by ``t``.)"""
    import copy
    if e.first_public_at is None or e.first_public_at > t:
        return None
    x = copy.deepcopy(normalise_legacy(copy.deepcopy(e)))
    hist = [h for h in x.history if h.at is None or h.at <= t]
    if not hist:
        return None
    x.history = hist
    stage, amount, cancelled = None, None, 0.0
    for h in hist:
        if h.stage == EventStage.CANCELLED:
            before = amount
            if h.amount is None or h.amount <= 0:
                stage, amount = EventStage.CANCELLED, 0.0
            else:
                amount = h.amount                 # partial cancellation: the remainder stays active
            cancelled += (before or 0.0) - (amount or 0.0) if before is not None else 0.0
            continue
        if h.stage == EventStage.AMENDED:
            amount = h.amount if h.amount is not None else amount
            continue
        stage = h.stage
        if h.amount is not None:
            amount = h.amount
    x.current_stage = stage or x.current_stage
    if x.amount is not None and amount is not None:
        from .contracts import Quantity
        x.amount = Quantity(amount, x.amount.unit, raw=f"as of {t.date()}")
    x.cancelled_amount = cancelled
    return x


def first_binding_at(e: EconomicEvent) -> Optional[datetime]:
    return next((h.at for h in e.history if h.stage in BINDING_STAGES), None)


def unverified_reasons(e: EconomicEvent) -> list[str]:
    """Why an event cannot support a verified-commitment classification ([] = verified)."""
    r = []
    if e.current_stage in (EventStage.CANCELLED, EventStage.EXPIRED):
        r.append(f"{e.current_stage.value}")
    elif e.current_stage not in BINDING_STAGES:
        r.append({EventStage.PREFERRED_BIDDER: "provisional (L1 / LoI / selected), not a firm order",
                  EventStage.MOU_FRAMEWORK: "MoU / framework, not a firm order",
                  EventStage.INQUIRY: "enquiry / discussions only"}.get(e.current_stage, "not binding"))
    if e.relationship == RelationshipStatus.CONFIRMED_RELATED:
        r.append("related party / own group: not external demand")
    if e.customer_verification == CustomerVerification.ANONYMOUS:
        r.append("customer not named: identity unverified")
    if e.value_basis in (ValueBasis.CEILING, ValueBasis.UNQUANTIFIED):
        r.append("ceiling / unquantified value: executable amount unknown")
    if e.amount is None or e.amount.unit != Unit.INR_CRORE or e.amount.value <= 0:
        r.append("no positive INR amount")
    if any(f.startswith("predecessor") for f in e.unresolved_fields):
        r.append("lifecycle note without an identified order")
    return r


@dataclass
class DemandSummary:
    window_start: date
    as_of: date
    verified_inflow_crore: float = 0.0
    verified_events: list[str] = field(default_factory=list)
    unverified_inflow_crore: float = 0.0                  # early lane (headline values, not executable)
    unverified_events: dict[str, list[str]] = field(default_factory=dict)
    related_party_excluded: list[str] = field(default_factory=list)
    cancellations_crore: float = 0.0
    backlog_crore: Optional[float] = None
    backlog_as_of: Optional[date] = None
    backlog_doc: str = ""
    backlog_note: str = ""
    annual_executable_crore: Optional[float] = None       # only events with a disclosed execution period
    annual_executable_unknown: list[str] = field(default_factory=list)
    tax_inclusive_events: list[str] = field(default_factory=list)
    ambiguous_groups: int = 0
    knowable_at: Optional[datetime] = None

    def notes(self) -> list[str]:
        n = []
        if self.tax_inclusive_events:
            n.append(f"{len(self.tax_inclusive_events)} order value(s) include taxes: not accounting revenue")
        if self.annual_executable_unknown:
            n.append(f"{len(self.annual_executable_unknown)} order(s) without an execution period: "
                     "annual executable revenue unknown (headline value is not annual revenue)")
        if self.ambiguous_groups:
            n.append(f"{self.ambiguous_groups} group(s) of possibly duplicate orders counted once")
        return n


def summarise_demand(events: list[EconomicEvent], evidence: list[Evidence], as_of: date,
                     lookback_days: int = 365, backlog_max_age_days: int = 200) -> DemandSummary:
    as_of = _d(as_of)
    s = DemandSummary(window_start=as_of - timedelta(days=lookback_days), as_of=as_of)
    events = [normalise_legacy(e) for e in events]
    by_id = {e.event_id: e for e in events}
    counted: set[str] = set()
    known: list[datetime] = []
    # verified members of an ambiguous group are taken first, so the group counts once at its best basis
    for e in sorted(events, key=lambda x: (bool(unverified_reasons(x)), -(x.amount.value if x.amount else 0),
                                           x.event_id)):
        fb = first_binding_at(e)
        when = _d(fb) or _d(e.first_public_at)
        if when is None or not (s.window_start <= when <= as_of):
            continue
        if e.relationship == RelationshipStatus.CONFIRMED_RELATED:
            s.related_party_excluded.append(e.event_id)
            continue
        group = {e.event_id, *e.ambiguous_with}
        if group & counted:
            continue                                         # possible duplicate already counted
        if len(group) > 1:
            s.ambiguous_groups += 1
        reasons = unverified_reasons(e)
        amount = e.amount.value if (e.amount and e.amount.unit == Unit.INR_CRORE) else 0.0
        if not reasons:
            s.verified_inflow_crore += amount
            s.verified_events.append(e.event_id)
            counted |= group
            if e.annual_executable_estimate is not None:
                s.annual_executable_crore = (s.annual_executable_crore or 0.0) + e.annual_executable_estimate
            else:
                s.annual_executable_unknown.append(e.event_id)
            if e.tax_basis == TaxBasis.INCLUSIVE:
                s.tax_inclusive_events.append(e.event_id)
            if e.first_public_at:
                known.append(fb or e.first_public_at)
        elif e.current_stage not in (EventStage.CANCELLED, EventStage.EXPIRED) and \
                not any(r.startswith("lifecycle") for r in reasons):
            s.unverified_events[e.event_id] = reasons
            s.unverified_inflow_crore += amount
            counted |= group
    s.cancellations_crore = sum(e.cancelled_amount for e in events
                                if any(h.stage == EventStage.CANCELLED and h.at
                                       and s.window_start <= _d(h.at) <= as_of for h in e.history))
    ob = sorted((x for x in evidence if x.usable and x.metric == Metric.ORDER_BOOK and x.quantity
                 and x.quantity.unit == Unit.INR_CRORE and x.modality.value == "realized" and x.available_at),
                key=lambda x: x.available_at)
    if ob:
        latest = ob[-1]
        age = (as_of - _d(latest.available_at)).days
        if age <= backlog_max_age_days:
            s.backlog_crore, s.backlog_as_of, s.backlog_doc = latest.quantity.value, _d(latest.available_at), latest.doc_id
            s.backlog_note = "company-stated outstanding order book (dated snapshot; not added to inflow)"
        else:
            s.backlog_note = f"latest stated order book is {age} days old: stale, not used"
    s.knowable_at = max(known) if known else None
    return s
