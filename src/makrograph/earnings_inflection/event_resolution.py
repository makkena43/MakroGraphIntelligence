"""Cross-document economic-event deduplication.

The same order announced on the exchange, repeated in the call transcript and
again in the investor presentation is ONE economic event, whatever the dates.
Independence is never inferred from distinct dates.

Merge rule for two order mentions:
* amounts agree within ``amount_tolerance`` (relative), and
* counterparties are compatible (same, or at least one unknown), and
* neither mention carries a distinct-event marker ("repeat order",
  "another order", "fresh order") relative to an already-known event, and
* they fall within ``max_gap_days`` (a guard against identical-sized
  recurring orders years apart; not an independence test).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Iterable

from .contracts import CommitmentStrength, EconomicEvent, Evidence, Metric, Quantity
from .identity import same_counterparty

_STRENGTH_RANK = {
    CommitmentStrength.NOT_APPLICABLE: 0, CommitmentStrength.NON_BINDING: 1,
    CommitmentStrength.PROVISIONAL: 2, CommitmentStrength.BINDING: 3,
}


def _amount_match(a: Quantity, b: Quantity, tol: float) -> bool:
    if a is None or b is None or a.unit != b.unit:
        return False
    hi = max(abs(a.value), abs(b.value))
    return hi > 0 and abs(a.value - b.value) / hi <= tol


def resolve_events(evidence: Iterable[Evidence], amount_tolerance: float = 0.02,
                   max_gap_days: int = 400) -> list[EconomicEvent]:
    evs = sorted((e for e in evidence if e.usable and e.metric == Metric.ORDER_WIN and e.quantity is not None),
                 key=lambda e: (e.available_at, e.doc_id, e.evidence_id))
    events: list[EconomicEvent] = []
    marked: dict[str, bool] = {}   # event_id -> its first mention carried a distinct marker
    for ev in evs:
        target = None
        for evt in events:
            if not _amount_match(evt.amount, ev.quantity, amount_tolerance):
                continue
            # unnamed descriptors ("a leading OEM") are treated as unknown identity
            cp = ev.counterparty if ev.counterparty_named else ""
            if same_counterparty(evt.counterparty, cp) is False:
                continue
            if ev.available_at and evt.first_public_at and ev.available_at - evt.first_public_at > timedelta(days=max_gap_days):
                continue
            if ev.distinct_marker and not marked[evt.event_id] and ev.doc_id not in evt.doc_ids:
                # "repeat/another order" of the same size as an earlier, unmarked
                # order is a new event; two marked mentions describe the same repeat.
                continue
            target = evt
            break
        if target is None:
            eid = hashlib.sha1(f"{ev.ticker}|{ev.evidence_id}".encode()).hexdigest()[:12]
            events.append(EconomicEvent(
                event_id=f"evt-{eid}", ticker=ev.ticker, kind=Metric.ORDER_WIN, amount=ev.quantity,
                counterparty=ev.counterparty if ev.counterparty_named else "",
                first_public_at=ev.available_at, evidence_ids=[ev.evidence_id], doc_ids=[ev.doc_id],
                commitment_strength=ev.commitment_strength, description=ev.quote[:240],
            ))
            marked[events[-1].event_id] = ev.distinct_marker
        else:
            target.evidence_ids.append(ev.evidence_id)
            if ev.doc_id not in target.doc_ids:
                target.doc_ids.append(ev.doc_id)
            if ev.counterparty_named and not target.counterparty:
                target.counterparty = ev.counterparty
            if _STRENGTH_RANK[ev.commitment_strength] > _STRENGTH_RANK[target.commitment_strength]:
                target.commitment_strength = ev.commitment_strength
    return events
