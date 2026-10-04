"""Deterministic investment gates for extracted issuer evidence."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from .ledger import assess_ledger
from .models import Action, CompanyDecision, CompanyPacket, FinancialSnapshot


@dataclass(frozen=True)
class GateConfig:
    starter_score: float = 68.0
    accumulate_score: float = 80.0
    min_independent_evidence_dates: int = 2
    no_chase_6m_pct: float = 100.0
    no_chase_12m_pct: float = 180.0
    max_starter_position_pct: float = 1.0
    max_accumulate_position_pct: float = 3.0


class GuidanceScorer:
    """Turn a point-in-time packet into one explicit, auditable action."""

    HARD_VETOES = {
        "audit_qualification",
        "auditor_resignation",
        "default_or_insolvency",
        "regulatory_enforcement",
        "fraud_or_misstatement",
    }

    def __init__(self, config: GateConfig | None = None):
        self.config = config or GateConfig()

    def score(self, packet: CompanyPacket) -> CompanyDecision:
        packet.assert_point_in_time()
        ledger = assess_ledger(packet)
        components = {
            "guidance_magnitude": self._guidance_magnitude(packet),       # 20
            "demand_visibility": self._demand_visibility(packet),         # 15
            "execution": self._execution(packet),                         # 20
            "guidance_consistency": ledger.consistency_score,             # 10
            "balance_sheet_and_cash": self._financial_quality(packet),    # 10
            "valuation": self._valuation(packet),                         # 10
            "entry_timing": self._timing(packet),                         # 10
            "evidence_quality": self._evidence_quality(packet),           # 5
        }
        risk_penalty = self._risk_penalty(packet)
        score = max(0.0, min(100.0, sum(components.values()) - risk_penalty))

        hard_vetoes = tuple(sorted({
            risk.kind for risk in packet.risks
            if risk.severity == "hard" or risk.kind in self.HARD_VETOES
        }))
        evidence_dates = len({event.source_date for event in packet.evidence})
        formal_claims = [c for c in packet.claims if c.certainty == "guidance" and c.confidence >= 0.60]
        realized = [e for e in packet.evidence if e.status in {"realized", "commissioned"}]
        valuation_known = components["valuation"] > 0
        chased = self._is_chased(packet)

        missing: list[str] = []
        if not formal_claims:
            missing.append("formal numeric management guidance")
        if evidence_dates < self.config.min_independent_evidence_dates:
            missing.append("a second independent dated execution event")
        if not realized:
            missing.append("commissioning or realized earnings proof")
        if not valuation_known:
            missing.append("point-in-time diluted valuation")
        if packet.market and not packet.market.adjusted_for_corporate_actions:
            missing.append("corporate-action-adjusted price history")

        why_now = self._why_now(packet, components, ledger.explanations)
        invalidation = self._invalidation(packet)

        if hard_vetoes:
            action = Action.THESIS_BROKEN
            max_position = 0.0
        elif not packet.documents or (not formal_claims and evidence_dates == 0):
            action = Action.REJECT
            max_position = 0.0
        elif chased and score >= self.config.starter_score:
            action = Action.HOLD_NO_CHASE
            max_position = 0.0
        elif (
            score >= self.config.accumulate_score
            and formal_claims
            and evidence_dates >= self.config.min_independent_evidence_dates
            and realized
            and valuation_known
        ):
            action = Action.ACCUMULATE
            max_position = self.config.max_accumulate_position_pct
        elif (
            score >= self.config.starter_score
            and formal_claims
            and evidence_dates >= self.config.min_independent_evidence_dates
            and valuation_known
        ):
            action = Action.STARTER
            max_position = self.config.max_starter_position_pct
        else:
            action = Action.WATCH
            max_position = 0.0

        return CompanyDecision(
            ticker=packet.ticker,
            company=packet.company,
            as_of_date=packet.as_of_date,
            action=action,
            score=round(score, 2),
            components={key: round(value, 2) for key, value in components.items()},
            hard_vetoes=hard_vetoes,
            why_now=why_now,
            missing_proof=tuple(dict.fromkeys(missing)),
            invalidation=invalidation,
            max_position_pct=max_position,
            evidence_dates=evidence_dates,
        )

    @staticmethod
    def _guidance_magnitude(packet: CompanyPacket) -> float:
        best = 0.0
        formal = [c for c in packet.claims if c.certainty == "guidance" and c.confidence >= 0.60]
        for claim in formal:
            target_values = [v for v in (claim.target_low, claim.target_high) if v is not None]
            target = mean(target_values) if target_values else None
            if target is not None and claim.baseline and claim.baseline > 0:
                multiple = target / claim.baseline
                points = 20.0 if multiple >= 3 else 17.0 if multiple >= 2 else 13.0 if multiple >= 1.5 else 8.0 if multiple >= 1.25 else 4.0
                if claim.metric == "pat":
                    points = min(20.0, points + 2.0)
                best = max(best, points)
        if best:
            return best
        # Numeric guidance without a recovered baseline is useful but cannot earn
        # a "multi-fold" score merely because its absolute number is large.
        return min(10.0, len({(c.metric, c.target_period) for c in formal}) * 5.0)

    @staticmethod
    def _demand_visibility(packet: CompanyPacket) -> float:
        weights = {
            "orderbook": 5.0,
            "order_win": 3.0,
            "customer_approval": 4.0,
            "capacity_addition": 2.0,
            "capacity_commissioned": 4.0,
        }
        by_date_kind: dict[tuple, float] = {}
        for event in packet.evidence:
            key = (event.source_date, event.kind)
            value = weights.get(event.kind, 0.0)
            if event.status in {"commissioned", "realized"}:
                value += 1.0
            by_date_kind[key] = max(by_date_kind.get(key, 0.0), value)
        return min(15.0, sum(by_date_kind.values()))

    @staticmethod
    def _execution(packet: CompanyPacket) -> float:
        points = 0.0
        for event in packet.evidence:
            if event.kind == "capacity_commissioned":
                points += 5.0
            elif event.kind in {"revenue_delivery", "profit_delivery", "margin_delivery"}:
                points += 4.0
            elif event.status == "realized":
                points += 2.0

        if packet.financials:
            latest = max(packet.financials, key=lambda f: (f.period_end, f.available_at))
            rev_growth = _growth(latest.revenue, latest.revenue_prior)
            pat_growth = _growth(latest.pat, latest.pat_prior)
            if rev_growth is not None:
                points += 5.0 if rev_growth >= 30 else 3.0 if rev_growth >= 15 else 1.0 if rev_growth > 0 else -2.0
            if pat_growth is not None:
                points += 6.0 if pat_growth >= 40 else 4.0 if pat_growth >= 20 else 1.0 if pat_growth > 0 else -3.0
            if latest.ebitda_margin_pct is not None and latest.ebitda_margin_prior_pct is not None:
                points += 3.0 if latest.ebitda_margin_pct >= latest.ebitda_margin_prior_pct + 1.0 else -1.0
        return max(0.0, min(20.0, points))

    @staticmethod
    def _financial_quality(packet: CompanyPacket) -> float:
        points = 2.0 if packet.financials else 0.0
        if packet.financials:
            latest = max(packet.financials, key=lambda f: (f.period_end, f.available_at))
            if latest.pat is not None and latest.cfo is not None:
                if latest.pat <= 0:
                    points -= 2.0
                elif latest.cfo >= latest.pat * 0.8:
                    points += 5.0
                elif latest.cfo > 0:
                    points += 2.0
                else:
                    points -= 3.0
            if latest.debt is not None and latest.debt_prior is not None and latest.debt_prior > 0:
                reduction = (latest.debt_prior - latest.debt) / latest.debt_prior
                points += 3.0 if reduction >= 0.25 else 1.0 if reduction > 0 else -1.0
        if any(e.kind == "debt_reduction" for e in packet.evidence):
            points += 2.0
        return max(0.0, min(10.0, points))

    @staticmethod
    def _valuation(packet: CompanyPacket) -> float:
        market = packet.market
        if not market or market.price <= 0:
            return 0.0
        eps = market.forward_eps or market.trailing_eps
        if eps is None or eps <= 0:
            return 0.0
        pe = market.price / eps
        return 10.0 if pe <= 12 else 8.0 if pe <= 18 else 6.0 if pe <= 25 else 3.0 if pe <= 35 else 1.0 if pe <= 50 else 0.0

    @staticmethod
    def _timing(packet: CompanyPacket) -> float:
        market = packet.market
        if not market:
            return 3.0
        r6 = market.return_6m_pct
        r12 = market.return_12m_pct
        if (r6 is not None and r6 >= 100) or (r12 is not None and r12 >= 180):
            return 0.0
        if (r6 is None or r6 <= 40) and (r12 is None or r12 <= 80):
            return 10.0
        if (r6 is None or r6 <= 70) and (r12 is None or r12 <= 120):
            return 7.0
        return 3.0

    @staticmethod
    def _evidence_quality(packet: CompanyPacket) -> float:
        official_docs = {d.document_id for d in packet.documents if d.source_name.lower() in {"nse", "nse_india", "bse", "bse_india", "company"}}
        dated = {e.source_date for e in packet.evidence if e.source_document_id in official_docs and e.confidence >= 0.55}
        quoted_claims = {c.source_document_id for c in packet.claims if c.quote and c.source_document_id in official_docs}
        return min(5.0, len(dated) * 1.5 + min(2.0, len(quoted_claims)))

    @staticmethod
    def _risk_penalty(packet: CompanyPacket) -> float:
        unique = {(r.source_date, r.kind, r.severity) for r in packet.risks if r.confidence >= 0.55}
        return min(30.0, sum(10.0 if severity == "hard" else 4.0 for _, _, severity in unique))

    def _is_chased(self, packet: CompanyPacket) -> bool:
        market = packet.market
        return bool(market and (
            (market.return_6m_pct is not None and market.return_6m_pct >= self.config.no_chase_6m_pct)
            or (market.return_12m_pct is not None and market.return_12m_pct >= self.config.no_chase_12m_pct)
        ))

    @staticmethod
    def _why_now(packet: CompanyPacket, components: dict[str, float],
                 ledger_notes: tuple[str, ...]) -> tuple[str, ...]:
        items: list[str] = []
        if components["guidance_magnitude"] >= 13:
            items.append("management disclosed a large, quantified earnings step-up")
        elif components["guidance_magnitude"]:
            items.append("numeric management guidance is now on record")
        if components["demand_visibility"] >= 8:
            items.append("multiple dated orders/order-book/capacity events support visibility")
        if components["execution"] >= 10:
            items.append("reported execution is beginning to validate the promise")
        items.extend(ledger_notes[:1])
        return tuple(items[:4])

    @staticmethod
    def _invalidation(packet: CompanyPacket) -> tuple[str, ...]:
        metrics = sorted({c.metric for c in packet.claims if c.certainty == "guidance"})
        items = [f"material cut or miss in {metric} guidance" for metric in metrics[:2]]
        items.extend([
            "commissioning/order conversion slips without a dated explanation",
            "cash conversion weakens while receivables or inventory accelerate",
            "audit, default, enforcement, or unexplained dilution hard veto appears",
        ])
        return tuple(items)


def _growth(current, prior):
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior) * 100.0


def annualized_eps(financials: list[FinancialSnapshot]):
    """Use only the latest disclosed period; do not sum overlapping reports."""
    usable = [f for f in financials if f.eps is not None and f.eps > 0 and f.period_months]
    if not usable:
        return None
    latest = max(usable, key=lambda f: (f.period_end, f.available_at))
    return latest.eps * 12.0 / latest.period_months
