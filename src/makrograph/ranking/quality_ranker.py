"""Quality Ranker — Tier 1 Multi-Decade Compounder Detection.

This is the second engine of the 3-tier investment framework:

Tier 1 (THIS ENGINE): Quality Compounders — Hold 5-20 years
    Companies earning >20% ROIC, with competitive moats, in expanding markets.
    Titan Watches, HDFC Bank, Asian Paints, NVIDIA (2015), Coca-Cola.
    NOT constraint plays — these are QUALITY businesses.

Tier 2 (ranking_engine.py): Constraint Cycle Winners — Hold 2-5 years
    Supply constraints + seller perspective + capex expansion.
    TSMC, Waaree, GE Vernova T&D, Micron.

Tier 3 (beneficiary_mapper.py): Policy/PLI Windows — Hold 1-3 years
    Government scheme + domestic manufacturer + short exclusivity window.
    PLI battery, PLI semiconductor, budget capex beneficiaries.

The QualityScore formula:
    quality_score = (
        0.40 × roic_score        +    # Are they earning high returns?
        0.30 × moat_score        +    # Can they DEFEND those returns?
        0.20 × tam_expansion     +    # Is there a LONG RUNWAY ahead?
        0.10 × management_score       # Is management CAPITAL DISCIPLINED?
    ) × margin_sustainability_mult

This score is independent of constraint signals. A company can be:
    - High quality_score + High constraint = BEST OF BOTH (Tier 1+2 overlap)
    - High quality_score + Low constraint  = Pure Tier 1 (Titan, HDFC type)
    - Low quality_score + High constraint  = Tier 2 only (cyclical constraint play)
    - Low quality_score + Low constraint  = EXCLUDE
"""

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Signal Type → Score Component Mapping ────────────────────────────────────
_ROIC_SIGNALS = frozenset({
    "roic_high_sustained",
    "roic_reinvestment",
    "earnings_quality_high",
})

_MOAT_SIGNALS = frozenset({
    "competitive_moat",
    "supply_concentration",       # also in Tier 2 — moat overlap
    "pricing_power_emerging",     # also in Tier 2 — moat overlap
    "realized_margin_expansion",  # also in Tier 2 — validated pricing power
})

_TAM_SIGNALS = frozenset({
    "tam_expansion_structural",
    "demand_surge",               # growing demand = expanding market evidence
    "market_entry",               # entering new markets = TAM expanding
})

_MGMT_SIGNALS = frozenset({
    "management_quality",
    "capex_increase",             # investing for growth = capital allocation
})

_MARGIN_SIGNALS = frozenset({
    "margin_sustainability",
    "realized_margin_expansion",
})


@dataclass
class QualityScore:
    """Per-company quality score for Tier 1 compounder detection."""
    ticker: str
    company_name: str
    quality_score: float                # 0.0–1.0 (higher = better compounder)
    roic_score: float
    moat_score: float
    tam_score: float
    management_score: float
    margin_sustainability: float
    signal_count: int
    signal_quarters: int                # how many quarters with quality signals
    best_roic_quote: str = ""
    best_moat_quote: str = ""
    best_tam_quote: str = ""
    signal_types_found: list = field(default_factory=list)
    investment_tier: str = "tier_2"    # tier_1, tier_2, tier_3
    tier_confidence: float = 0.0
    quality_thesis: str = ""           # auto-generated thesis


class QualityRanker:
    """Score companies on multi-decade compounder potential.

    Inputs: Signal records from get_all_signals_in_window() with quality signals.
    Output: Ranked list of QualityScore objects (highest = best compounders).
    """

    def __init__(self, min_signals: int = 2, min_quality_score: float = 0.35):
        self.min_signals     = min_signals
        self.min_quality     = min_quality_score

    def rank(
        self,
        signal_records: list[dict],
        country: str = "IN",
    ) -> list[QualityScore]:
        """Score all companies on quality/compounder potential.

        Uses the same signal_records as the constraint engine so no extra
        DB queries are needed. Quality signals (roic_high_sustained, competitive_moat,
        etc.) fire from MD&A sections of the same concalls.
        """
        # Aggregate quality signals per company
        co_map: dict[str, dict] = defaultdict(lambda: {
            "ticker": "", "company": "",
            "roic_sigs": [], "moat_sigs": [], "tam_sigs": [],
            "mgmt_sigs": [], "margin_sigs": [],
            "quarters": set(), "total": 0,
        })

        for sig in signal_records:
            stype   = sig.get("signal_type", "")
            company = sig.get("company", "") or ""
            ticker  = sig.get("ticker") or sig.get("doc_ticker") or ""
            ctx     = (sig.get("context_text") or "")[:250]
            conf    = float(sig.get("confidence") or 0.7)
            filed_at = sig.get("filed_at") or sig.get("doc_filed_at")

            if not company:
                continue

            key = ticker or company
            co_map[key]["ticker"]  = ticker
            co_map[key]["company"] = company

            if filed_at:
                try:
                    from datetime import date as _date
                    d = filed_at if hasattr(filed_at, "month") else _date.fromisoformat(str(filed_at)[:10])
                    co_map[key]["quarters"].add(f"Q{(d.month-1)//3+1}-{d.year}")
                except Exception:
                    pass

            if stype in _ROIC_SIGNALS:
                co_map[key]["roic_sigs"].append({"conf": conf, "quote": ctx, "type": stype})
            elif stype in _MOAT_SIGNALS:
                co_map[key]["moat_sigs"].append({"conf": conf, "quote": ctx, "type": stype})
            elif stype in _TAM_SIGNALS:
                co_map[key]["tam_sigs"].append({"conf": conf, "quote": ctx, "type": stype})
            elif stype in _MGMT_SIGNALS:
                co_map[key]["mgmt_sigs"].append({"conf": conf, "quote": ctx, "type": stype})
            if stype in _MARGIN_SIGNALS:
                co_map[key]["margin_sigs"].append({"conf": conf, "quote": ctx})

            co_map[key]["total"] += 1

        # Score each company
        results: list[QualityScore] = []
        for key, data in co_map.items():
            roic_sigs   = data["roic_sigs"]
            moat_sigs   = data["moat_sigs"]
            tam_sigs    = data["tam_sigs"]
            mgmt_sigs   = data["mgmt_sigs"]
            margin_sigs = data["margin_sigs"]
            quarters    = len(data["quarters"])

            total_quality = len(roic_sigs) + len(moat_sigs) + len(tam_sigs) + len(mgmt_sigs)
            if total_quality < self.min_signals:
                continue

            # Component scores (0–1)
            def _score(sigs: list[dict], max_sigs: int = 5) -> float:
                if not sigs:
                    return 0.0
                conf_sum = sum(s["conf"] for s in sigs)
                return min(1.0, (conf_sum / max(max_sigs * 0.85, 1)) *
                           (1.0 + 0.1 * min(quarters - 1, 3)))  # multi-quarter bonus

            roic_score  = _score(roic_sigs, 3)
            moat_score  = _score(moat_sigs, 4)
            tam_score   = _score(tam_sigs, 3)
            mgmt_score  = _score(mgmt_sigs, 3)

            # Margin sustainability multiplier (1.0–1.3)
            margin_mult = 1.0 + 0.15 * _score(margin_sigs, 2)

            # Weighted quality score
            raw_score = (
                0.40 * roic_score
                + 0.30 * moat_score
                + 0.20 * tam_score
                + 0.10 * mgmt_score
            ) * margin_mult

            quality_score = min(1.0, round(raw_score, 3))

            if quality_score < self.min_quality:
                continue

            # Best quotes for justification
            best_roic = max(roic_sigs, key=lambda s: s["conf"], default={}).get("quote", "")
            best_moat = max(moat_sigs, key=lambda s: s["conf"], default={}).get("quote", "")
            best_tam  = max(tam_sigs,  key=lambda s: s["conf"], default={}).get("quote", "")

            # Investment tier classification
            if roic_score >= 0.60 and moat_score >= 0.50 and quality_score >= 0.65:
                tier      = "tier_1"
                tier_conf = round(quality_score * 0.90, 2)
            elif quality_score >= 0.45:
                tier      = "tier_1_watch"
                tier_conf = round(quality_score * 0.75, 2)
            else:
                tier      = "tier_2"
                tier_conf = round(quality_score * 0.60, 2)

            # Auto-generate quality thesis
            thesis = _auto_quality_thesis(
                company       = data["company"],
                ticker        = data["ticker"],
                roic_score    = roic_score,
                moat_score    = moat_score,
                tam_score     = tam_score,
                quarters      = quarters,
                roic_quote    = best_roic,
                moat_quote    = best_moat,
                tam_quote     = best_tam,
            )

            found_types = list({s["type"] for sl in [roic_sigs,moat_sigs,tam_sigs,mgmt_sigs,margin_sigs] for s in sl})

            results.append(QualityScore(
                ticker               = data["ticker"],
                company_name         = data["company"],
                quality_score        = quality_score,
                roic_score           = round(roic_score, 3),
                moat_score           = round(moat_score, 3),
                tam_score            = round(tam_score, 3),
                management_score     = round(mgmt_score, 3),
                margin_sustainability= round(margin_mult, 3),
                signal_count         = total_quality,
                signal_quarters      = quarters,
                best_roic_quote      = best_roic[:300],
                best_moat_quote      = best_moat[:300],
                best_tam_quote       = best_tam[:300],
                signal_types_found   = found_types,
                investment_tier      = tier,
                tier_confidence      = tier_conf,
                quality_thesis       = thesis,
            ))

        results.sort(key=lambda r: -r.quality_score)
        return results


def _auto_quality_thesis(
    company: str, ticker: str,
    roic_score: float, moat_score: float, tam_score: float,
    quarters: int, roic_quote: str, moat_quote: str, tam_quote: str,
) -> str:
    """Generate plain-English Tier 1 investment thesis."""
    parts = []

    if roic_score >= 0.60:
        parts.append(
            f"{company} demonstrates high capital efficiency — "
            f"earning strong returns on invested capital that compound over time."
        )
    if moat_quote:
        parts.append(f'Management on competitive positioning: "{moat_quote[:180]}"')
    elif moat_score >= 0.50:
        parts.append(
            f"The company shows evidence of durable competitive advantages — "
            f"brand preference, distribution strength, or customer switching costs."
        )
    if tam_score >= 0.50:
        parts.append(
            f"Operating in a structurally growing market with a long runway — "
            f"a key ingredient for multi-decade compounding."
        )
    if quarters >= 3:
        parts.append(
            f"Quality signals confirmed across {quarters} quarters — "
            f"sustained evidence, not a one-quarter anomaly."
        )

    return " ".join(parts) or f"{company} shows quality compounder characteristics from concall analysis."
