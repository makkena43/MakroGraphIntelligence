"""Hybrid disclosure extraction for the guidance radar.

The language model is a bounded semantic parser, not the decision maker.  It
must return source-document ids and verbatim supporting text.  This module
then validates every quote against the dated source and discards unsupported
claims before deterministic scoring.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Protocol

from .models import EvidenceEvent, FinancialSnapshot, GuidanceClaim, RiskFlag, SourceDocument

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_SPACE = re.compile(r"\s+")
_AMOUNT = re.compile(
    r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(crore|crores|cr\.?|lakh|lakhs|million|billion)?",
    re.I,
)
_RELEVANT = re.compile(
    r"guidance|outlook|target|expect|revenue|sales|profit|pat\b|ebitda|margin|"
    r"order\s*book|orders?|capacity|commission|utili[sz]ation|plant|facility|"
    r"debt|borrow|cash\s*flow|inventory|receivable|working\s*capital|audit|"
    r"resign|pledge|preferential|warrant|related.party|default|fraud|clarification",
    re.I,
)

HARD_RISK_KINDS = {
    "audit_qualification",
    "auditor_resignation",
    "default_or_insolvency",
    "regulatory_enforcement",
    "fraud_or_misstatement",
}


class JSONExtractionClient(Protocol):
    model_name: str

    def extract(self, prompt: str) -> dict[str, Any]: ...


@dataclass
class ExtractionResult:
    claims: list[GuidanceClaim]
    evidence: list[EvidenceEvent]
    risks: list[RiskFlag]
    financials: list[FinancialSnapshot]
    narrative_tags_by_date: dict[date, set[str]]
    model_name: str


class AnthropicJSONClient:
    """Small, explicit Anthropic adapter with no dependency on selector code."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6",
                 max_tokens: int = 6000, temperature: float = 0.0):
        if not api_key:
            raise ValueError("Anthropic API key is required when --use-llm is enabled")
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model_name = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def extract(self, prompt: str) -> dict[str, Any]:
        response = self._client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(getattr(block, "text", "") for block in response.content)
        return _parse_json_object(raw)


class HybridDisclosureExtractor:
    """Extract a company packet using rules plus an optional LLM review pass."""

    def __init__(self, llm_client: Optional[JSONExtractionClient] = None,
                 max_prompt_chars: int = 65_000):
        self.llm_client = llm_client
        self.max_prompt_chars = max_prompt_chars

    def extract(self, documents: list[SourceDocument], as_of_date: date) -> ExtractionResult:
        docs = [d for d in documents if d.filed_at <= as_of_date and d.text.strip()]
        docs.sort(key=lambda d: (d.filed_at, d.document_id))

        rules = self._heuristic_extract(docs)
        if not self.llm_client or not docs:
            return rules

        compact = self._compact_documents(docs)
        payload = self.llm_client.extract(_build_prompt(compact, as_of_date))
        reviewed = self._validated_llm_result(payload, docs)
        return _merge_results(rules, reviewed, self.llm_client.model_name)

    def _compact_documents(self, documents: list[SourceDocument]) -> str:
        """Keep signal-rich passages while preserving the original document id."""
        blocks: list[str] = []
        budget = self.max_prompt_chars
        # Newest evidence normally resolves older promises, so include it first.
        for doc in reversed(documents):
            chosen: list[str] = []
            for sentence in _SENTENCE_SPLIT.split(doc.text):
                sentence = _SPACE.sub(" ", sentence).strip()
                if len(sentence) >= 25 and _RELEVANT.search(sentence):
                    chosen.append(sentence[:900])
                if len(chosen) >= 28:
                    break
            if not chosen:
                continue
            block = (
                f"\n[DOC id={doc.document_id} date={doc.filed_at.isoformat()} "
                f"type={doc.filing_type!r} title={doc.title!r}]\n"
                + "\n".join(chosen)
            )
            if len(block) > budget:
                block = block[:budget]
            blocks.append(block)
            budget -= len(block)
            if budget <= 500:
                break
        return "".join(reversed(blocks))

    def _heuristic_extract(self, documents: list[SourceDocument]) -> ExtractionResult:
        claims: list[GuidanceClaim] = []
        evidence: list[EvidenceEvent] = []
        risks: list[RiskFlag] = []
        tags: dict[date, set[str]] = {}

        for doc in documents:
            filing = doc.filing_type.lower()
            doc_tags: set[str] = set()
            for raw_sentence in _SENTENCE_SPLIT.split(doc.text):
                sentence = _SPACE.sub(" ", raw_sentence).strip()
                if len(sentence) < 25 or not _RELEVANT.search(sentence):
                    continue
                low = sentence.lower()

                if any(word in low for word in ("revenue", "sales", "turnover")):
                    doc_tags.add("revenue_growth")
                if any(word in low for word in ("capacity", "plant", "facility")):
                    doc_tags.add("capacity_expansion")
                if "order book" in low or "orderbook" in low:
                    doc_tags.add("orderbook_growth")
                if any(word in low for word in ("debt", "borrowings")):
                    doc_tags.add("deleveraging")

                metric = _metric_in_sentence(low)
                amount = _first_amount(sentence)
                if metric and re.search(r"\b(?:guidance|target|expect(?:s|ed)?|aim(?:s|ed)?|project(?:s|ed)?)\b", low):
                    certainty = "aspiration" if re.search(r"vision|aspir|ambition|aim", low) else "guidance"
                    claims.append(GuidanceClaim(
                        metric=metric,
                        target_period=_target_period(sentence),
                        source_document_id=doc.document_id,
                        source_date=doc.filed_at,
                        quote=sentence[:700],
                        target_low=amount,
                        target_high=amount,
                        unit=_amount_unit(sentence),
                        certainty=certainty,
                        confidence=0.62,
                    ))

                event_kind, status = _event_kind(low, filing)
                if event_kind:
                    evidence.append(EvidenceEvent(
                        kind=event_kind,
                        source_document_id=doc.document_id,
                        source_date=doc.filed_at,
                        quote=sentence[:700],
                        metric=metric or event_kind,
                        value=amount,
                        unit=_amount_unit(sentence),
                        status=status,
                        confidence=0.58,
                    ))

                risk_kind, severity = _risk_kind(low, filing)
                if risk_kind:
                    risks.append(RiskFlag(
                        kind=risk_kind,
                        source_document_id=doc.document_id,
                        source_date=doc.filed_at,
                        quote=sentence[:700],
                        severity=severity,
                        confidence=0.62,
                    ))
            if doc_tags:
                tags.setdefault(doc.filed_at, set()).update(doc_tags)

        return ExtractionResult(
            claims=_dedupe_claims(claims),
            evidence=_dedupe_evidence(evidence),
            risks=_dedupe_risks(risks),
            financials=[],
            narrative_tags_by_date=tags,
            model_name="heuristic-v1",
        )

    def _validated_llm_result(self, payload: dict[str, Any],
                              documents: list[SourceDocument]) -> ExtractionResult:
        by_id = {d.document_id: d for d in documents}
        claims: list[GuidanceClaim] = []
        evidence: list[EvidenceEvent] = []
        risks: list[RiskFlag] = []
        financials: list[FinancialSnapshot] = []
        tags: dict[date, set[str]] = {}

        for row in payload.get("claims", []):
            doc = _supported_row(row, by_id)
            if not doc:
                continue
            claims.append(GuidanceClaim(
                metric=_safe_slug(row.get("metric")) or "other",
                target_period=str(row.get("target_period") or "unspecified")[:40],
                source_document_id=doc.document_id,
                source_date=doc.filed_at,
                quote=str(row["quote"])[:700],
                target_low=_number(row.get("target_low")),
                target_high=_number(row.get("target_high")),
                baseline=_number(row.get("baseline")),
                unit=str(row.get("unit") or "")[:30],
                certainty=_enum(row.get("certainty"), {"guidance", "aspiration", "conditional"}, "conditional"),
                confidence=_confidence(row.get("confidence")),
            ))

        for row in payload.get("evidence", []):
            doc = _supported_row(row, by_id)
            if not doc:
                continue
            evidence.append(EvidenceEvent(
                kind=_safe_slug(row.get("kind")) or "other",
                source_document_id=doc.document_id,
                source_date=doc.filed_at,
                quote=str(row["quote"])[:700],
                metric=_safe_slug(row.get("metric")),
                value=_number(row.get("value")),
                prior_value=_number(row.get("prior_value")),
                unit=str(row.get("unit") or "")[:30],
                status=_enum(row.get("status"), {"announced", "ordered", "commissioned", "realized"}, "announced"),
                confidence=_confidence(row.get("confidence")),
            ))

        for row in payload.get("risks", []):
            doc = _supported_row(row, by_id)
            if not doc:
                continue
            kind = _safe_slug(row.get("kind")) or "other"
            severity = _enum(row.get("severity"), {"soft", "hard"}, "soft")
            if kind in HARD_RISK_KINDS:
                severity = "hard"
            risks.append(RiskFlag(
                kind=kind,
                source_document_id=doc.document_id,
                source_date=doc.filed_at,
                quote=str(row["quote"])[:700],
                severity=severity,
                confidence=_confidence(row.get("confidence")),
            ))

        for row in payload.get("financials", []):
            doc = by_id.get(_int(row.get("document_id")))
            period_end = _date(row.get("period_end"))
            if not doc or not period_end:
                continue
            financials.append(FinancialSnapshot(
                available_at=doc.filed_at,
                period_end=period_end,
                revenue=_number(row.get("revenue")),
                revenue_prior=_number(row.get("revenue_prior")),
                pat=_number(row.get("pat")),
                pat_prior=_number(row.get("pat_prior")),
                ebitda_margin_pct=_number(row.get("ebitda_margin_pct")),
                ebitda_margin_prior_pct=_number(row.get("ebitda_margin_prior_pct")),
                cfo=_number(row.get("cfo")),
                debt=_number(row.get("debt")),
                debt_prior=_number(row.get("debt_prior")),
                eps=_number(row.get("eps")),
                period_months=_int_or_none(row.get("period_months")),
                currency=str(row.get("currency") or "INR_CR")[:20],
                source_document_id=doc.document_id,
            ))

        for row in payload.get("narrative_tags", []):
            doc = by_id.get(_int(row.get("document_id")))
            if doc:
                tags.setdefault(doc.filed_at, set()).update(
                    _safe_slug(tag) for tag in row.get("tags", []) if _safe_slug(tag)
                )

        return ExtractionResult(
            claims=_dedupe_claims(claims),
            evidence=_dedupe_evidence(evidence),
            risks=_dedupe_risks(risks),
            financials=sorted(financials, key=lambda f: (f.available_at, f.period_end)),
            narrative_tags_by_date=tags,
            model_name=getattr(self.llm_client, "model_name", "llm"),
        )


def _build_prompt(compact_documents: str, as_of_date: date) -> str:
    return f"""You are a forensic Indian small/mid-cap equity disclosure parser.
The analysis date is {as_of_date.isoformat()}. Use only the supplied official disclosures.

Your task is extraction, not investment recommendation. Return ONE JSON object and no prose.
Never infer an unstated number. Every claim, evidence event, and risk must include a document_id
and a short EXACT quote copied from that document. Distinguish announcement from commissioning
and realized financial delivery. Treat management aspiration as weaker than formal guidance.

Return this schema:
{{
  "claims": [{{"document_id": 1, "metric": "revenue|pat|ebitda_margin|debt|capacity",
    "target_period": "FY2027", "target_low": 0, "target_high": 0, "baseline": 0,
    "unit": "INR_CR|PCT|UNITS", "certainty": "guidance|aspiration|conditional",
    "confidence": 0.0, "quote": "exact source words"}}],
  "evidence": [{{"document_id": 1, "kind": "order_win|orderbook|capacity_addition|capacity_commissioned|customer_approval|revenue_delivery|profit_delivery|margin_delivery|debt_reduction|cash_conversion",
    "metric": "", "value": 0, "prior_value": 0, "unit": "INR_CR|PCT|UNITS",
    "status": "announced|ordered|commissioned|realized", "confidence": 0.0,
    "quote": "exact source words"}}],
  "risks": [{{"document_id": 1, "kind": "audit_qualification|auditor_resignation|default_or_insolvency|regulatory_enforcement|fraud_or_misstatement|promoter_pledge|preferential_dilution|receivable_stress|inventory_stress|negative_cash_conversion|guidance_cut|target_miss|narrative_drift|related_party_risk",
    "severity": "soft|hard", "confidence": 0.0, "quote": "exact source words"}}],
  "financials": [{{"document_id": 1, "period_end": "YYYY-MM-DD", "revenue": 0,
    "revenue_prior": 0, "pat": 0, "pat_prior": 0, "ebitda_margin_pct": 0,
    "ebitda_margin_prior_pct": 0, "cfo": 0, "debt": 0, "debt_prior": 0,
    "eps": 0, "period_months": 12,
    "currency": "INR_CR"}}],
  "narrative_tags": [{{"document_id": 1, "tags": ["stable_business_driver"]}}]
}}

Rules:
- A new order is visibility, not revenue delivery.
- Announced capacity is not commissioned capacity.
- Do not count duplicate exchange coversheets as independent proof.
- Numeric units must match the source; use null when absent.
- Flag concrete governance/cash-conversion problems, not boilerplate risks.
- Narrative tags describe the actual business/earnings driver, not generic words.

DOCUMENTS:
{compact_documents}
"""


def _merge_results(rule: ExtractionResult, llm: ExtractionResult, model_name: str) -> ExtractionResult:
    tags = {d: set(v) for d, v in rule.narrative_tags_by_date.items()}
    for day, values in llm.narrative_tags_by_date.items():
        tags.setdefault(day, set()).update(values)
    return ExtractionResult(
        claims=_dedupe_claims(rule.claims + llm.claims),
        evidence=_dedupe_evidence(rule.evidence + llm.evidence),
        risks=_dedupe_risks(rule.risks + llm.risks),
        financials=llm.financials,
        narrative_tags_by_date=tags,
        model_name=f"hybrid:{model_name}",
    )


def _supported_row(row: Any, documents: dict[int, SourceDocument]) -> Optional[SourceDocument]:
    if not isinstance(row, dict):
        return None
    doc = documents.get(_int(row.get("document_id")))
    quote = str(row.get("quote") or "").strip()
    if not doc or len(quote) < 12:
        return None
    if _normalize_for_match(quote) not in _normalize_for_match(doc.text):
        logger.warning("Discarded unsupported LLM quote for document %s", getattr(doc, "document_id", "?"))
        return None
    return doc


def _normalize_for_match(value: str) -> str:
    return _SPACE.sub(" ", value).strip().lower().replace("₹", "rs")


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM returned no JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM extraction must be a JSON object")
    return value


def _metric_in_sentence(low: str) -> str:
    if "ebitda" in low and "margin" in low:
        return "ebitda_margin"
    if re.search(r"\b(?:pat|profit after tax|net profit)\b", low):
        return "pat"
    if re.search(r"\b(?:revenue|sales|turnover|total income)\b", low):
        return "revenue"
    if "order book" in low or "orderbook" in low:
        return "orderbook"
    if "capacity" in low:
        return "capacity"
    if "debt" in low or "borrowings" in low:
        return "debt"
    return ""


def _event_kind(low: str, filing: str) -> tuple[str, str]:
    if re.search(r"commission(?:ed|ing)|commercial production|commenced production", low):
        return "capacity_commissioned", "commissioned"
    if re.search(r"capacity.{0,80}(?:add|expan|increase|from|to)", low):
        return "capacity_addition", "announced"
    if "order book" in low or "orderbook" in low:
        return "orderbook", "ordered"
    if ("bagging/receiving" in filing or "awarding" in filing or
            re.search(r"(?:received|secured|awarded).{0,60}(?:order|contract|loa|loi)", low)):
        return "order_win", "ordered"
    if re.search(r"debt.free|reduc(?:ed|tion).{0,40}(?:debt|borrow)|debt.{0,40}(?:reduc|declin)", low):
        return "debt_reduction", "realized"
    return "", ""


def _risk_kind(low: str, filing: str) -> tuple[str, str]:
    filing_is_auditor_exit = "resignation" in filing and "auditor" in filing
    filing_is_default = any(value in filing for value in (
        "default", "insolvency", "corporate insolvency resolution",
    ))
    filing_is_fraud = "fraud" in filing
    if filing_is_auditor_exit:
        return "auditor_resignation", "hard"
    if re.search(r"qualified opinion|audit qualification|adverse opinion|disclaimer of opinion", low):
        # Results routinely reproduce accounting-policy boilerplate.  A rule
        # match is a review flag; only an LLM-supported, issuer-specific quote
        # or a matching exchange category may activate the hard veto.
        return "audit_qualification_review", "soft"
    if filing_is_default and re.search(r"(?:insolvency|payment default|defaulted on)", low):
        return "default_or_insolvency", "hard"
    if filing_is_fraud and re.search(r"fraud|misstatement", low):
        return "fraud_or_misstatement", "hard"
    if re.search(r"preferential.{0,60}(?:issue|allotment)|issue.{0,30}warrants", low):
        return "preferential_dilution", "soft"
    if re.search(r"negative.{0,30}(?:cash flow|operating cash)|cash flow.{0,30}negative", low):
        return "negative_cash_conversion", "soft"
    return "", ""


def _first_amount(sentence: str) -> Optional[float]:
    match = _AMOUNT.search(sentence)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    unit = (match.group(2) or "").lower()
    if unit in {"lakh", "lakhs"}:
        value /= 100.0
    elif unit == "million":
        value /= 10.0
    elif unit == "billion":
        value *= 100.0
    return value


def _amount_unit(sentence: str) -> str:
    if re.search(r"%|percent", sentence, re.I):
        return "PCT"
    if re.search(r"₹|\brs\.?|\binr\b|crore|\bcr\.?\b", sentence, re.I):
        return "INR_CR"
    if re.search(r"units?|mw|gw|mtpa|tonnes?", sentence, re.I):
        return "UNITS"
    return ""


def _target_period(sentence: str) -> str:
    match = re.search(r"\b(?:fy\s*)?20\d{2}(?:[-/]\d{2})?\b", sentence, re.I)
    return match.group(0).upper().replace(" ", "") if match else "unspecified"


def _dedupe_claims(rows: list[GuidanceClaim]) -> list[GuidanceClaim]:
    best: dict[tuple[Any, ...], GuidanceClaim] = {}
    for row in rows:
        key = (row.source_date, row.metric, row.target_period, row.target_low, row.target_high)
        if key not in best or row.confidence > best[key].confidence:
            best[key] = row
    return sorted(best.values(), key=lambda r: (r.source_date, r.metric, -r.confidence))


def _dedupe_evidence(rows: list[EvidenceEvent]) -> list[EvidenceEvent]:
    best: dict[tuple[Any, ...], EvidenceEvent] = {}
    for row in rows:
        if row.independent_key not in best or row.confidence > best[row.independent_key].confidence:
            best[row.independent_key] = row
    return sorted(best.values(), key=lambda r: (r.source_date, r.kind, -r.confidence))


def _dedupe_risks(rows: list[RiskFlag]) -> list[RiskFlag]:
    best: dict[tuple[Any, ...], RiskFlag] = {}
    for row in rows:
        key = (row.source_date, row.kind, row.severity)
        if key not in best or row.confidence > best[key].confidence:
            best[key] = row
    return sorted(best.values(), key=lambda r: (r.source_date, r.kind, -r.confidence))


def _safe_slug(value: Any) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return value[:60]


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _int_or_none(value: Any) -> Optional[int]:
    parsed = _int(value)
    return parsed if parsed > 0 else None


def _confidence(value: Any) -> float:
    parsed = _number(value)
    return min(1.0, max(0.0, parsed if parsed is not None else 0.5))


def _enum(value: Any, allowed: set[str], default: str) -> str:
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def _date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def extraction_fingerprint(documents: list[SourceDocument], model_name: str) -> str:
    """Stable cache key for a source set and model version."""
    raw = model_name + "|" + "|".join(
        f"{d.document_id}:{d.filed_at}:{hashlib.sha256(d.text.encode()).hexdigest()}"
        for d in documents
    )
    return hashlib.sha256(raw.encode()).hexdigest()
