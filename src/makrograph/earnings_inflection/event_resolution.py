"""Commercial-event identity, dated state history and deduplication (WP4).

Identity, not amount:
* Mentions merge when they share an order reference, or name the same
  counterparty with a matching amount (2% tolerance) and a compatible product.
  The same order repeated by the exchange filing, the call and the deck is one
  event; repeated descriptions are not independent evidence.
* A similar amount alone never merges orders.  When a mention could be an
  existing order but identity cannot be established (e.g. an unnamed customer),
  both events are kept, linked as ``ambiguous_with``, and counted once in
  aggregates (conservative, no double count).
* "Repeat / another / fresh order" of the same size as an earlier order is a
  new event.

Dated states:
* Cancellation, partial cancellation, amendment, expiry and execution are state
  changes applied to the predecessor order in date order.  ``current_stage`` is
  the latest dated state - an amendment can reduce value and a cancellation
  reverses the commitment; the strongest-ever state is NOT retained.
* A later mention at a weaker commitment wording (a call recalling "we were L1")
  is a retelling and never downgrades an order; only explicit lifecycle events do.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Iterable, Optional

from .contracts import (
    COMMITMENT_RANK, CommitmentStrength, CustomerVerification, EconomicEvent, EventStage, EventStateChange, Evidence,
    Metric, Quantity, RelationshipStatus, TaxBasis, ValueBasis,
)
from .identity import same_counterparty

LIFECYCLE = (EventStage.CANCELLED, EventStage.EXPIRED, EventStage.AMENDED, EventStage.EXECUTION)
TERMINAL = (EventStage.CANCELLED, EventStage.EXPIRED)
_REL_RANK = {RelationshipStatus.UNKNOWN: 0, RelationshipStatus.ISSUER_ASSERTED_UNRELATED: 1,
             RelationshipStatus.INDEPENDENTLY_SUPPORTED_UNRELATED: 2, RelationshipStatus.CONFIRMED_RELATED: 3}
_STOP = {"the", "of", "for", "and", "a", "an", "to", "kv", "supply", "order", "orders"}


def _stage_from_strength(ev: Evidence) -> EventStage:
    return {CommitmentStrength.BINDING: EventStage.BINDING_ORDER,
            CommitmentStrength.PROVISIONAL: EventStage.PREFERRED_BIDDER,
            CommitmentStrength.NON_BINDING: EventStage.MOU_FRAMEWORK}.get(ev.commitment_strength,
                                                                         EventStage.PREFERRED_BIDDER)


def _strength(stage: Optional[EventStage]) -> CommitmentStrength:
    return {EventStage.BINDING_ORDER: CommitmentStrength.BINDING, EventStage.EXECUTION: CommitmentStrength.BINDING,
            EventStage.PREFERRED_BIDDER: CommitmentStrength.PROVISIONAL,
            EventStage.MOU_FRAMEWORK: CommitmentStrength.NON_BINDING,
            EventStage.INQUIRY: CommitmentStrength.NON_BINDING}.get(stage, CommitmentStrength.NOT_APPLICABLE)


def _amount_match(a: Optional[Quantity], b: Optional[Quantity], tol: float) -> bool:
    if a is None or b is None or a.unit != b.unit:
        return False
    hi = max(abs(a.value), abs(b.value))
    return hi > 0 and abs(a.value - b.value) / hi <= tol


def _product_tokens(p: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (p or "").lower()) if t not in _STOP and len(t) > 1}


def _product_compatible(a: str, b: str) -> bool:
    ta, tb = _product_tokens(a), _product_tokens(b)
    if not ta or not tb:
        return True                         # one side silent about the product: not a contradiction
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.5


def _named(ev: Evidence) -> str:
    return ev.counterparty if ev.counterparty_named else ""


def _new_event(ev: Evidence, stage: EventStage) -> EconomicEvent:
    eid = hashlib.sha1(f"{ev.ticker}|{ev.evidence_id}".encode()).hexdigest()[:12]
    evt = EconomicEvent(
        event_id=f"evt-{eid}", ticker=ev.ticker, kind=Metric.ORDER_WIN, amount=ev.quantity,
        counterparty=_named(ev), first_public_at=ev.available_at, evidence_ids=[ev.evidence_id],
        doc_ids=[ev.doc_id], commitment_strength=_strength(stage), description=ev.quote[:240],
        current_stage=stage, original_amount=ev.quantity,
        history=[EventStateChange(stage, ev.available_at, ev.doc_id, ev.evidence_id,
                                  ev.quantity.value if ev.quantity else None, "first disclosure")],
        relationship=ev.relationship, relationship_basis=[ev.relationship_basis] if ev.relationship_basis else [],
        customer_verification=(CustomerVerification.ISSUER_NAMED if ev.counterparty_named
                               else CustomerVerification.ANONYMOUS),
        value_basis=ev.value_basis or (ValueBasis.FIRM if ev.quantity else ValueBasis.UNQUANTIFIED),
        tax_basis=ev.tax_basis, duration_months=ev.duration_months, delivery_window=ev.delivery_window,
        payment_terms=ev.payment_terms, termination_terms=ev.termination_terms, reference_id=ev.reference_id,
        product=ev.product,
    )
    if not ev.counterparty_named:
        evt.unresolved_fields.append("customer identity not disclosed")
    if evt.tax_basis == TaxBasis.UNKNOWN and ev.quantity is not None:
        evt.unresolved_fields.append("tax basis of order value not stated")
    if ev.quantity is not None and not ev.duration_months:
        evt.unresolved_fields.append("execution period not stated (annual executable revenue unknown)")
    return evt


def _absorb(evt: EconomicEvent, ev: Evidence, stage: EventStage) -> None:
    """Attach a repeated description of the same order (not independent evidence)."""
    evt.evidence_ids.append(ev.evidence_id)
    if ev.doc_id not in evt.doc_ids:
        evt.doc_ids.append(ev.doc_id)
    if ev.counterparty_named and not evt.counterparty:
        evt.counterparty = ev.counterparty
        evt.customer_verification = max(evt.customer_verification, CustomerVerification.ISSUER_NAMED,
                                         key=lambda v: list(CustomerVerification).index(v))
        evt.unresolved_fields = [f for f in evt.unresolved_fields if f != "customer identity not disclosed"]
    if _REL_RANK[ev.relationship] > _REL_RANK[evt.relationship]:
        evt.relationship = ev.relationship
    if ev.relationship_basis and ev.relationship_basis not in evt.relationship_basis:
        evt.relationship_basis.append(ev.relationship_basis)
    for attr in ("duration_months", "delivery_window", "payment_terms", "termination_terms", "reference_id", "product"):
        if not getattr(evt, attr) and getattr(ev, attr):
            setattr(evt, attr, getattr(ev, attr))
    if evt.tax_basis == TaxBasis.UNKNOWN and ev.tax_basis != TaxBasis.UNKNOWN:
        evt.tax_basis = ev.tax_basis
    if evt.value_basis == ValueBasis.UNQUANTIFIED and ev.value_basis not in (None, ValueBasis.UNQUANTIFIED):
        evt.value_basis = ev.value_basis
    # genuine progression (e.g. L1 -> purchase order), dated; never a downgrade
    if (evt.current_stage in COMMITMENT_RANK and stage in COMMITMENT_RANK
            and COMMITMENT_RANK[stage] > COMMITMENT_RANK[evt.current_stage]):
        evt.current_stage = stage
        evt.history.append(EventStateChange(stage, ev.available_at, ev.doc_id, ev.evidence_id,
                                            evt.amount.value if evt.amount else None, "stage progressed"))
        if ev.quantity is not None:
            evt.amount = ev.quantity
    evt.commitment_strength = _strength(evt.current_stage)


def _apply_lifecycle(evt: EconomicEvent, ev: Evidence, stage: EventStage, tol: float) -> None:
    evt.evidence_ids.append(ev.evidence_id)
    if ev.doc_id not in evt.doc_ids:
        evt.doc_ids.append(ev.doc_id)
    current = evt.amount.value if evt.amount else None
    note = ""
    if stage == EventStage.CANCELLED:
        cancelled = ev.quantity.value if (ev.quantity and current is not None and ev.quantity.value < current * (1 - tol)) \
            else current
        evt.cancelled_amount += cancelled or 0.0
        remaining = None if current is None else max(0.0, current - (cancelled or 0.0))
        if remaining is not None and remaining > tol * (current or 1):
            note = f"partially cancelled: {cancelled:g} cr cancelled, {remaining:g} cr remains"
            evt.amount = Quantity(remaining, evt.amount.unit, raw="after partial cancellation")
            evt.history.append(EventStateChange(EventStage.CANCELLED, ev.available_at, ev.doc_id, ev.evidence_id,
                                                remaining, note))
            return
        evt.amount = Quantity(0.0, evt.amount.unit, raw="cancelled") if evt.amount else None
        evt.current_stage = EventStage.CANCELLED
        note = "cancelled"
    elif stage == EventStage.AMENDED:
        if ev.quantity is not None:
            note = f"value amended {current:g} -> {ev.quantity.value:g} cr" if current is not None else "value amended"
            evt.amount = ev.quantity
        else:
            note = "amended (terms; value not stated)"
            evt.unresolved_fields.append("amendment terms not quantified")
    elif stage == EventStage.EXPIRED:
        evt.current_stage = EventStage.EXPIRED
        note = "expired"
    elif stage == EventStage.EXECUTION:
        evt.current_stage = EventStage.EXECUTION
        note = "execution / delivery reported"
    evt.history.append(EventStateChange(stage, ev.available_at, ev.doc_id, ev.evidence_id,
                                        evt.amount.value if evt.amount else None, note))
    evt.commitment_strength = _strength(evt.current_stage)


def _find_predecessor(events: list[EconomicEvent], ev: Evidence, tol: float) -> tuple[Optional[EconomicEvent], str]:
    if ev.reference_id:
        hit = [e for e in events if e.reference_id == ev.reference_id]
        if len(hit) == 1:
            return hit[0], ""
    cp = _named(ev)
    cands = [e for e in events if e.current_stage not in TERMINAL and e.kind == Metric.ORDER_WIN
             and (not cp or same_counterparty(e.counterparty, cp) is True)
             and e.first_public_at is not None and ev.available_at is not None
             and e.first_public_at <= ev.available_at]
    if ev.quantity is not None and len(cands) > 1:
        by_amt = [e for e in cands if _amount_match(e.original_amount, ev.quantity, tol)
                  or _amount_match(e.amount, ev.quantity, tol)]
        cands = by_amt or cands
    if ev.product and len(cands) > 1:
        cands = [e for e in cands if _product_compatible(e.product, ev.product)] or cands
    if len(cands) == 1 and (cp or ev.quantity is not None):
        return cands[0], ""
    return None, ("predecessor order not identified" if not cands else
                  f"predecessor ambiguous among {len(cands)} orders")


def resolve_events(evidence: Iterable[Evidence], amount_tolerance: float = 0.02, max_gap_days: int = 400,
                   ambiguous_window_days: int = 120) -> list[EconomicEvent]:
    evs = sorted((e for e in evidence if e.usable and e.metric == Metric.ORDER_WIN
                  and (e.quantity is not None or (e.event_stage in LIFECYCLE))),
                 key=lambda e: (e.available_at, e.doc_id, e.evidence_id))
    events: list[EconomicEvent] = []
    marked: dict[str, bool] = {}          # event_id -> first mention carried a distinct ("repeat") marker
    for ev in evs:
        stage = ev.event_stage or _stage_from_strength(ev)
        if stage in LIFECYCLE:
            target, why = _find_predecessor(events, ev, amount_tolerance)
            if target is not None:
                _apply_lifecycle(target, ev, stage, amount_tolerance)
            else:
                orphan = _new_event(ev, stage)
                orphan.unresolved_fields.insert(0, why)
                orphan.amount = None                  # a lifecycle note is never new demand
                events.append(orphan)
                marked[orphan.event_id] = False
            continue
        target, ambiguous = None, []
        cp = _named(ev)
        for evt in events:
            if evt.current_stage in LIFECYCLE and evt.original_amount is None:
                continue                             # unlinked lifecycle note
            if ev.reference_id and evt.reference_id:
                if ev.reference_id == evt.reference_id:
                    target = evt
                    break
                continue                             # different references: different orders
            if not (_amount_match(evt.original_amount, ev.quantity, amount_tolerance)
                    or _amount_match(evt.amount, ev.quantity, amount_tolerance)):
                continue
            gap = (ev.available_at - evt.first_public_at) if (ev.available_at and evt.first_public_at) else timedelta(0)
            if gap > timedelta(days=max_gap_days):
                continue
            if not _product_compatible(evt.product, ev.product):
                continue
            same = same_counterparty(evt.counterparty, cp)
            if same is False:
                continue
            if ev.distinct_marker and not marked.get(evt.event_id) and ev.doc_id not in evt.doc_ids:
                continue                             # "repeat order" of the same size: new event
            if same is True:
                target = evt
                break
            if gap <= timedelta(days=ambiguous_window_days):
                ambiguous.append(evt)                # identity cannot be established
        if target is not None:
            _absorb(target, ev, stage)
            continue
        new = _new_event(ev, stage)
        for a in ambiguous:
            new.ambiguous_with.append(a.event_id)
            a.ambiguous_with.append(new.event_id)
        if ambiguous:
            new.unresolved_fields.append("may duplicate " + ", ".join(a.event_id for a in ambiguous)
                                         + " (counted once in totals)")
        events.append(new)
        marked[new.event_id] = ev.distinct_marker
    return events
