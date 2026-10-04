"""Counterparty identity, relationship and commitment evidence.

Profiles are built from DEDUPLICATED economic events only, so a repeated
announcement cannot inflate a customer's apparent importance.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from .contracts import CommitmentStrength, CounterpartyProfile, EconomicEvent, Unit
from .identity import normalize_counterparty

_RANK = {CommitmentStrength.NOT_APPLICABLE: 0, CommitmentStrength.NON_BINDING: 1,
         CommitmentStrength.PROVISIONAL: 2, CommitmentStrength.BINDING: 3}


def build_profiles(events: list[EconomicEvent], ttm_revenue_crore: Optional[float],
                   concentration_threshold: float = 0.25) -> list[CounterpartyProfile]:
    groups: "OrderedDict[str, CounterpartyProfile]" = OrderedDict()
    for e in events:
        key = normalize_counterparty(e.counterparty) or "(unnamed counterparty)"
        prof = groups.get(key)
        if prof is None:
            prof = groups[key] = CounterpartyProfile(name=e.counterparty or "(unnamed counterparty)",
                                                     named=bool(e.counterparty))
        prof.events.append(e.event_id)
        if e.amount and e.amount.unit == Unit.INR_CRORE:
            prof.total_amount_crore += e.amount.value
        if _RANK[e.commitment_strength] > _RANK[prof.strongest_commitment]:
            prof.strongest_commitment = e.commitment_strength

    total_disclosed = sum(p.total_amount_crore for p in groups.values())
    for p in groups.values():
        if not p.named:
            p.risk_flags.append("counterparty not named in disclosure; identity unverifiable")
        if p.strongest_commitment == CommitmentStrength.NON_BINDING:
            p.risk_flags.append("non-binding only (MoU / discussions / pipeline)")
        elif p.strongest_commitment == CommitmentStrength.PROVISIONAL:
            p.risk_flags.append("provisional only (L1 / LoI / selected, not yet a firm order)")
        if ttm_revenue_crore and ttm_revenue_crore > 0:
            p.share_of_ttm_revenue = round(p.total_amount_crore / ttm_revenue_crore, 3)
            if p.share_of_ttm_revenue >= concentration_threshold:
                p.risk_flags.append(f"disclosed orders = {p.share_of_ttm_revenue:.0%} of TTM revenue (concentration)")
        if total_disclosed > 0 and len(groups) > 1 and p.total_amount_crore / total_disclosed >= 0.5:
            p.risk_flags.append("majority of disclosed order value from this counterparty")
        if len(p.events) >= 2:
            p.risk_flags.append(f"{len(p.events)} distinct orders (repeat relationship)")
    return list(groups.values())
