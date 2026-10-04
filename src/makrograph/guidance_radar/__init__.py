"""Point-in-time management guidance and execution radar.

This package is intentionally independent from the constraint/theme selector.
It ranks an issuer only from its own dated disclosures, financial delivery,
valuation and price state.
"""

from .models import (
    Action,
    CompanyDecision,
    CompanyPacket,
    EvidenceEvent,
    FinancialSnapshot,
    GuidanceClaim,
    MarketSnapshot,
    RiskFlag,
    SourceDocument,
)
from .scoring import GuidanceScorer

__all__ = [
    "Action",
    "CompanyDecision",
    "CompanyPacket",
    "EvidenceEvent",
    "FinancialSnapshot",
    "GuidanceClaim",
    "GuidanceScorer",
    "MarketSnapshot",
    "RiskFlag",
    "SourceDocument",
]
