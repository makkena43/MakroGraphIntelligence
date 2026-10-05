"""Counterparty identity, relationship and commitment evidence (WP4).

Profiles are built from DEDUPLICATED economic events only, so a repeated
announcement cannot inflate a customer's apparent importance.

Relationship status is sourced and never upgraded by issuer statements:
* ``confirmed_related``: the disclosure says own subsidiary / group / related party;
* ``issuer_asserted_unrelated``: the issuer says the customer is unrelated (e.g. the
  SEBI order-disclosure answer) - recorded as an issuer assertion only;
* ``independently_supported_unrelated``: only from explicitly supplied reference
  data with a dated, attributable source available by the cutoff;
* ``unknown`` otherwise.  Missing information is never read as a shell company
  or fraud, and customer prestige says nothing about order profitability.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from typing import Optional

from .contracts import (
    CommitmentStrength, CounterpartyProfile, CustomerVerification, EconomicEvent, EventStage, RelationshipStatus,
    TaxBasis, Unit, ValueBasis,
)
from .identity import normalize_counterparty

_RANK = {CommitmentStrength.NOT_APPLICABLE: 0, CommitmentStrength.NON_BINDING: 1,
         CommitmentStrength.PROVISIONAL: 2, CommitmentStrength.BINDING: 3}
_REL_RANK = {RelationshipStatus.UNKNOWN: 0, RelationshipStatus.ISSUER_ASSERTED_UNRELATED: 1,
             RelationshipStatus.INDEPENDENTLY_SUPPORTED_UNRELATED: 2, RelationshipStatus.CONFIRMED_RELATED: 3}


def _as_date(v) -> Optional[date]:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def apply_reference_data(events: list[EconomicEvent], reference: dict, cutoff: date) -> list[str]:
    """Apply explicitly supplied, dated counterparty reference data.

    ``reference``: {name: {"relationship": "unrelated" | "related",
                           "sources": [{"source": "...", "as_of": "YYYY-MM-DD"}]}}
    Only sources dated on/before the cutoff count; returns notes for the report.
    """
    notes = []
    ref = {normalize_counterparty(k): v for k, v in (reference or {}).items()}
    for e in events:
        if not e.counterparty:
            continue
        entry = ref.get(normalize_counterparty(e.counterparty))
        if not entry:
            continue
        srcs = [s for s in entry.get("sources", []) if _as_date(s.get("as_of")) and _as_date(s["as_of"]) <= cutoff]
        if not srcs:
            notes.append(f"reference data for {e.counterparty} postdates the cutoff; not used")
            continue
        label = "; ".join(f"{s.get('source', 'source')} ({s['as_of']})" for s in srcs)
        if entry.get("relationship") == "related":
            e.relationship = RelationshipStatus.CONFIRMED_RELATED
        elif entry.get("relationship") == "unrelated" and e.relationship != RelationshipStatus.CONFIRMED_RELATED:
            e.relationship = RelationshipStatus.INDEPENDENTLY_SUPPORTED_UNRELATED
        e.relationship_basis.append(f"reference data: {label}")
        e.customer_verification = CustomerVerification.CORROBORATED
    return notes


def build_profiles(events: list[EconomicEvent], ttm_revenue_crore: Optional[float],
                   concentration_threshold: float = 0.25) -> list[CounterpartyProfile]:
    groups: "OrderedDict[str, CounterpartyProfile]" = OrderedDict()
    counted: set[str] = set()
    for e in events:
        if e.amount is None and e.original_amount is None and e.current_stage in (
                EventStage.CANCELLED, EventStage.EXPIRED, EventStage.AMENDED, EventStage.EXECUTION):
            continue                                  # unlinked lifecycle note
        key = normalize_counterparty(e.counterparty) or "(unnamed counterparty)"
        prof = groups.get(key)
        if prof is None:
            prof = groups[key] = CounterpartyProfile(name=e.counterparty or "(unnamed counterparty)",
                                                     named=bool(e.counterparty))
        prof.events.append(e.event_id)
        group = {e.event_id, *e.ambiguous_with}
        if e.amount and e.amount.unit == Unit.INR_CRORE and not (group & counted):
            prof.total_amount_crore += e.amount.value      # current value; possible duplicates once
            counted |= group
        if _RANK[e.commitment_strength] > _RANK[prof.strongest_commitment]:
            prof.strongest_commitment = e.commitment_strength
        if _REL_RANK[e.relationship] > _REL_RANK[prof.relationship]:
            prof.relationship = e.relationship
        prof.relationship_sources += [b for b in e.relationship_basis if b not in prof.relationship_sources]
        if list(CustomerVerification).index(e.customer_verification) > list(CustomerVerification).index(
                prof.verification):
            prof.verification = e.customer_verification

    total_disclosed = sum(p.total_amount_crore for p in groups.values())
    for p in groups.values():
        evs = [e for e in events if e.event_id in p.events]
        if not p.named:
            p.risk_flags.append("counterparty not named in disclosure; identity unverified (not evidence of "
                                "wrongdoing)")
        if p.relationship == RelationshipStatus.CONFIRMED_RELATED:
            p.risk_flags.append("related party / own group: excluded from external demand")
        elif p.relationship == RelationshipStatus.ISSUER_ASSERTED_UNRELATED:
            p.risk_flags.append("unrelated status is an issuer assertion only (not independently verified)")
        elif p.relationship == RelationshipStatus.UNKNOWN and p.named:
            p.risk_flags.append("relationship to the issuer not disclosed")
        if p.strongest_commitment == CommitmentStrength.NON_BINDING:
            p.risk_flags.append("non-binding only (MoU / framework / discussions)")
        elif p.strongest_commitment == CommitmentStrength.PROVISIONAL:
            p.risk_flags.append("provisional only (L1 / LoI / selected, not yet a firm order)")
        if any(e.current_stage == EventStage.CANCELLED or e.cancelled_amount for e in evs):
            p.risk_flags.append("cancellation recorded: "
                                + ", ".join(f"{e.cancelled_amount:g} cr" for e in evs if e.cancelled_amount))
        if any(e.value_basis in (ValueBasis.CEILING, ValueBasis.UNQUANTIFIED) for e in evs):
            p.risk_flags.append("ceiling / unquantified value: executable amount unknown")
        if any(e.tax_basis == TaxBasis.INCLUSIVE for e in evs):
            p.risk_flags.append("order value includes taxes: not accounting revenue")
        if any(e.amount and not e.duration_months for e in evs):
            p.risk_flags.append("execution period not disclosed: annual revenue contribution unknown")
        if any(e.ambiguous_with for e in evs):
            p.risk_flags.append("possible duplicate mentions of the same order (counted once)")
        if ttm_revenue_crore and ttm_revenue_crore > 0:
            p.share_of_ttm_revenue = round(p.total_amount_crore / ttm_revenue_crore, 3)
            if p.share_of_ttm_revenue >= concentration_threshold:
                p.risk_flags.append(f"disclosed orders = {p.share_of_ttm_revenue:.0%} of TTM revenue (concentration)")
        if total_disclosed > 0 and len(groups) > 1 and p.total_amount_crore / total_disclosed >= 0.5:
            p.risk_flags.append("majority of disclosed order value from this counterparty")
        if len(p.events) >= 2:
            p.risk_flags.append(f"{len(p.events)} distinct orders (repeat relationship)")
    return list(groups.values())
