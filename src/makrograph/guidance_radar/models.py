"""Typed records used by the guidance-to-execution radar."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional


class Action(str, Enum):
    """Unambiguous portfolio-research authority emitted by the radar."""

    REJECT = "REJECT"
    WATCH = "WATCH"
    STARTER = "STARTER"
    ACCUMULATE = "ACCUMULATE"
    HOLD_NO_CHASE = "HOLD_NO_CHASE"
    THESIS_BROKEN = "THESIS_BROKEN"


@dataclass(frozen=True)
class SourceDocument:
    document_id: int
    ticker: str
    company: str
    filed_at: date
    source_url: str
    filing_type: str
    title: str
    text: str
    source_name: str = "nse"


@dataclass(frozen=True)
class GuidanceClaim:
    metric: str
    target_period: str
    source_document_id: int
    source_date: date
    quote: str
    target_low: Optional[float] = None
    target_high: Optional[float] = None
    baseline: Optional[float] = None
    unit: str = ""
    certainty: str = "guidance"  # guidance | aspiration | conditional
    confidence: float = 0.0


@dataclass(frozen=True)
class EvidenceEvent:
    kind: str
    source_document_id: int
    source_date: date
    quote: str
    metric: str = ""
    value: Optional[float] = None
    prior_value: Optional[float] = None
    unit: str = ""
    status: str = "announced"  # announced | ordered | commissioned | realized
    confidence: float = 0.0

    @property
    def independent_key(self) -> tuple[Any, ...]:
        """A repeated press release on the same date is one evidence leg."""
        return (
            self.source_date,
            self.kind.strip().lower(),
            self.metric.strip().lower(),
            round(self.value, 4) if self.value is not None else None,
            self.status.strip().lower(),
        )


@dataclass(frozen=True)
class RiskFlag:
    kind: str
    source_document_id: int
    source_date: date
    quote: str
    severity: str = "soft"  # soft | hard
    confidence: float = 0.0


@dataclass(frozen=True)
class FinancialSnapshot:
    available_at: date
    period_end: date
    revenue: Optional[float] = None
    revenue_prior: Optional[float] = None
    pat: Optional[float] = None
    pat_prior: Optional[float] = None
    ebitda_margin_pct: Optional[float] = None
    ebitda_margin_prior_pct: Optional[float] = None
    cfo: Optional[float] = None
    debt: Optional[float] = None
    debt_prior: Optional[float] = None
    eps: Optional[float] = None
    period_months: Optional[int] = None
    currency: str = "INR_CR"
    source_document_id: Optional[int] = None


@dataclass(frozen=True)
class MarketSnapshot:
    price_date: date
    price: float
    market_cap: Optional[float] = None
    trailing_eps: Optional[float] = None
    forward_eps: Optional[float] = None
    return_3m_pct: Optional[float] = None
    return_6m_pct: Optional[float] = None
    return_12m_pct: Optional[float] = None
    adjusted_for_corporate_actions: bool = False


@dataclass
class CompanyPacket:
    ticker: str
    company: str
    as_of_date: date
    documents: list[SourceDocument] = field(default_factory=list)
    claims: list[GuidanceClaim] = field(default_factory=list)
    evidence: list[EvidenceEvent] = field(default_factory=list)
    risks: list[RiskFlag] = field(default_factory=list)
    financials: list[FinancialSnapshot] = field(default_factory=list)
    market: Optional[MarketSnapshot] = None
    narrative_tags_by_date: dict[date, set[str]] = field(default_factory=dict)

    def assert_point_in_time(self) -> None:
        """Reject silently contaminated packets before any score is computed."""
        late: list[str] = []
        late.extend(f"document:{d.document_id}" for d in self.documents if d.filed_at > self.as_of_date)
        late.extend(f"claim:{c.source_document_id}" for c in self.claims if c.source_date > self.as_of_date)
        late.extend(f"evidence:{e.source_document_id}" for e in self.evidence if e.source_date > self.as_of_date)
        late.extend(f"risk:{r.source_document_id}" for r in self.risks if r.source_date > self.as_of_date)
        late.extend(
            f"financial:{f.source_document_id}" for f in self.financials
            if f.available_at > self.as_of_date
        )
        if self.market and self.market.price_date > self.as_of_date:
            late.append("market")
        if late:
            raise ValueError(
                f"look-ahead data in {self.ticker} packet as of {self.as_of_date}: "
                + ", ".join(late)
            )


@dataclass(frozen=True)
class CompanyDecision:
    ticker: str
    company: str
    as_of_date: date
    action: Action
    score: float
    components: dict[str, float]
    hard_vetoes: tuple[str, ...]
    why_now: tuple[str, ...]
    missing_proof: tuple[str, ...]
    invalidation: tuple[str, ...]
    max_position_pct: float
    evidence_dates: int
    model_name: str = "deterministic-gates-v1"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action"] = self.action.value
        payload["as_of_date"] = self.as_of_date.isoformat()
        return payload
