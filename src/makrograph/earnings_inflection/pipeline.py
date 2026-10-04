"""Explicit run-once orchestration.

read-only source snapshot
  -> document / identity / coverage validation
  -> chronological evidence extraction
  -> numeric validation + economic-event deduplication
  -> driver changes + promise delivery + counterparty checks
  -> evidence-only assessment

No scheduling, no notifications, no writes to existing tables.  LLM calls and
database persistence are off unless explicitly enabled in config; persistence
additionally refuses any database whose name does not match the configured
test pattern.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable, Optional

from .assessments import (
    decide_status, financing_risks, limitations_for, next_checks, review_status_for, what_changed,
)
from .budget import Budget
from .chunking import chunk_document
from .contracts import (
    IST, Assessment, DocumentKind, Evidence, ListingSegment, Metric, SourceRef, to_jsonable,
)
from .counterparty import build_profiles
from .document_versions import availability, is_restatement, link_versions
from .drivers import compute_drivers
from .earnings_bridge import build_bridge
from .event_resolution import resolve_events
from .extraction import ConstrainedLLMExtractor, extract_sentence_evidence, parse_results_tables
from .financial_series import FinancialSeries
from .guidance_ledger import build_ledger
from .identity import IdentityResolver, SymbolSpan, classify_issuer_model, listing_segment_from
from .validation import reconcile, scope_conflicts, validate_evidence, validate_measurements

logger = logging.getLogger(__name__)


class PersistenceDisabled(RuntimeError):
    pass


def as_of_datetime(value) -> datetime:
    """Dates are interpreted as end of that day, IST."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    if isinstance(value, str):
        if "T" in value or " " in value.strip():
            return as_of_datetime(datetime.fromisoformat(value))
        from datetime import date as _d
        value = _d.fromisoformat(value)
    return datetime.combine(value, time(23, 59, 59), tzinfo=IST)


@dataclass
class RunResult:
    as_of: datetime
    assessments: list[Assessment] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    preflight: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)


class EarningsInflectionPipeline:
    def __init__(self, config: dict, repository, llm_complete: Optional[Callable[[str], str]] = None):
        self.cfg = config or {}
        self.repo = repository
        self.country = self.cfg.get("country", "IN")
        self.budget = Budget.from_config(self.cfg)
        llm_cfg = self.cfg.get("llm", {})
        self.llm = ConstrainedLLMExtractor(llm_complete, self.budget, enabled=bool(llm_cfg.get("enabled", False)),
                                           est_tokens_per_call=int(llm_cfg.get("est_tokens_per_call", 2500)))
        spans = [SymbolSpan(**{**s, "valid_from": _d(s["valid_from"]), "valid_to": _d(s.get("valid_to"))})
                 for s in self.cfg.get("identity", {}).get("symbol_history", [])]
        self.identity = IdentityResolver(spans)

    # -- public ------------------------------------------------------------

    def run(self, tickers: list[str], as_of) -> RunResult:
        as_of_dt = as_of_datetime(as_of)
        res = RunResult(as_of=as_of_dt, preflight=self.repo.preflight())
        if res.preflight.get("ok") is False:
            raise RuntimeError(f"source preflight failed: {res.preflight}")
        for t in tickers:
            try:
                res.assessments.append(self.assess(t, as_of_dt))
            except Exception as e:   # one company never aborts the run; recorded, not hidden
                logger.exception("assessment failed for %s", t)
                res.errors[t] = f"{type(e).__name__}: {e}"
        res.budget = self.budget.summary()
        return res

    def assess(self, ticker: str, as_of: datetime) -> Assessment:
        issuer_id, identity_basis = self.identity.resolve(ticker, as_of.date())
        meta = self.repo.issuer_metadata(ticker) or {}
        raw_docs = self.repo.documents(ticker, self.country, as_of)

        # 1. availability first, THEN version linking (no future versions leak in)
        coverage = {"documents_returned": len(raw_docs), "identity_basis": identity_basis}
        stamped = [(d, availability(d)[0]) for d in raw_docs]
        visible = [d for d, ts in stamped if ts is not None and ts <= as_of]
        coverage["excluded_unknown_time"] = sum(1 for _, ts in stamped if ts is None)
        coverage["excluded_after_as_of"] = sum(1 for _, ts in stamped if ts is not None and ts > as_of)
        docs = link_versions(visible)   # lineage built only from what was public
        coverage["no_text"] = sum(1 for d in docs if not d.full_text().strip())
        coverage["title_only_classified"] = sum(1 for d in docs if d.kind_basis.startswith("title_only"))
        coverage["superseded_or_duplicate"] = sum(1 for d in docs if d.superseded_by)
        coverage["by_kind"] = {}
        for d in docs:
            coverage["by_kind"][d.kind.value] = coverage["by_kind"].get(d.kind.value, 0) + 1
        docs.sort(key=lambda d: (d.available_at, d.doc_id))   # chronological
        docs_by_id = {d.doc_id: d for d in docs}
        company = meta.get("name") or next((d.company for d in docs if d.company), "")

        # 2. classification
        all_text = "\n".join(d.full_text()[:20000] for d in docs if d.kind.value == "financial_results")
        issuer_model, model_basis = classify_issuer_model(company, meta.get("industry", ""), all_text)
        coverage["issuer_model_basis"] = model_basis
        segment = listing_segment_from(meta.get("series"), meta.get("board", ""))

        # 3. extraction (deterministic; LLM only if enabled)
        evidence: list[Evidence] = []
        measurements, issues = [], []
        llm_rejected: list[str] = []
        for d in docs:
            exact_dup = (d.superseded_by and d.superseded_by in docs_by_id
                         and docs_by_id[d.superseded_by].content_hash == d.content_hash)
            if not d.full_text().strip() or exact_dup:
                continue
            chunks = chunk_document(d)
            if d.kind.value in ("financial_results", "annual_report", "investor_presentation"):
                rows, iss = parse_results_tables(d, chunks)
                if d.superseded_by:
                    rows = []   # a later version for the same period replaces these numbers
                for r in rows:
                    r.restated = is_restatement(d)
                measurements += rows
                issues += iss
            evidence += extract_sentence_evidence(d, chunks)
            if self.llm._enabled:
                ev, rej = self.llm.extract(d, chunks)
                evidence += ev
                llm_rejected += rej

        # 4. validation + dedup
        evidence = validate_evidence(evidence, docs_by_id, as_of)
        measurements, m_issues = validate_measurements(measurements, as_of)
        issues += m_issues + reconcile(measurements) + scope_conflicts(measurements)
        events = resolve_events(evidence)

        # 5. series, drivers, ledger, counterparties, bridge
        series = FinancialSeries.build(ticker, measurements)
        cadence = series.cadence()
        end = series.latest_period(Metric.REVENUE, cadence) if cadence else None
        ttm_rev = series.ttm(Metric.REVENUE, end, cadence)[0] if end else None
        drivers, missing = compute_drivers(series, events, evidence, issuer_model, as_of.date(),
                                           self.cfg.get("thresholds"))
        commentary_times = [d.available_at for d in docs if d.kind in (
            DocumentKind.EARNINGS_CALL_TRANSCRIPT, DocumentKind.INVESTOR_PRESENTATION)]
        guidance = build_ledger(evidence, series, as_of, docs_by_id, commentary_times)
        profiles = build_profiles(events, ttm_rev)
        bridge = build_bridge(series, issuer_model, guidance)
        missing += [m for m in bridge.missing_inputs if m not in missing]

        usable_docs = sum(1 for d in docs if d.full_text().strip())
        status, why = decide_status(drivers, events, evidence, guidance, usable_docs, ttm_rev)
        first_public = {d.doc_id: d.available_at for d in docs}

        contradictions = [f"{g.metric.value} {g.target_period_label}: {f}" for g in guidance for f in g.flags]
        contradictions += [f"negated statement: \"{e.quote[:160]}\"" for e in evidence
                           if e.usable and e.modality.value == "negated" and e.tier.value == "management_assertion"][:5]
        contradictions += [i for i in issues if "!=" in i or "vs computed" in i]

        cited_ids = set()
        for e in evidence:
            if e.usable:
                cited_ids.add(e.doc_id)
        for p in series.points.values():
            cited_ids.update(p.doc_ids)
        sources = []
        ev_by_doc: dict[str, Evidence] = {}
        for e in evidence:
            if e.usable and e.doc_id not in ev_by_doc:
                ev_by_doc[e.doc_id] = e
        for d in docs:
            if d.doc_id in cited_ids:
                e = ev_by_doc.get(d.doc_id)
                sources.append(SourceRef(d.doc_id, d.title, d.kind.value, d.available_at, d.availability_basis,
                                         d.url, e.quote if e else "", e.page if e else 0))

        lim = limitations_for(issuer_model, identity_basis, coverage)
        lim += [f"Revised figures: {n}" for n in series.lineage_notes]
        lim += [f"Data issue: {i}" for i in issues if "unit line" in i or "excluded" in i][:10]
        if cadence == "H":
            lim.append("Half-yearly reporter: growth, margin and TTM use half-years (TTM = last two halves). "
                       "Changes surface up to six months later than for quarterly reporters, and two "
                       "consecutive half-years span a full year of evidence.")
        if segment == ListingSegment.SME:
            lim.append("SME-listed issuer: thinner disclosure (often no earnings call or presentation); "
                       "verify figures against the filed statement.")
        if llm_rejected:
            lim.append(f"{len(llm_rejected)} LLM-extracted item(s) rejected by validation.")

        unusable = [e for e in evidence if not e.usable]
        coverage["evidence_items"] = len(evidence)
        coverage["evidence_rejected"] = len(unusable)
        coverage["financial_rows"] = len(measurements)
        coverage["series_scope"] = series.scope.value
        coverage["reporting_cadence"] = {"Q": "quarterly", "H": "half-yearly"}.get(cadence or "", "none")
        coverage["order_mentions"] = sum(1 for e in evidence if e.usable and e.metric.value == "order_win")
        coverage["economic_events_after_dedup"] = len(events)

        a = Assessment(
            ticker=ticker, company=company, country=self.country, as_of=as_of, issuer_model=issuer_model,
            listing_segment=segment, evidence_status=status,
            review_status=review_status_for(evidence, [d.kind_basis for d in docs]),
            scenario_status=bridge.status, status_rationale=why,
            what_changed=what_changed(drivers, events, guidance, evidence, first_public),
            drivers=drivers, guidance=guidance, events=events, counterparties=profiles, bridge=bridge,
            contradictions=contradictions, financing_risks=financing_risks(evidence, ttm_rev, bridge),
            customer_risks=[f"{p.name}: {f}" for p in profiles for f in p.risk_flags],
            missing_inputs=sorted(set(missing)),
            next_checks=next_checks(drivers, events, guidance, sorted(set(missing)), status),
            sources=sources, limitations=lim, coverage=coverage,
        )
        a.validate()
        return a

    # -- persistence (disabled by default) ----------------------------------

    def persist(self, result: RunResult, connect=None) -> int:
        pcfg = self.cfg.get("persistence", {})
        if not pcfg.get("enabled", False):
            raise PersistenceDisabled("persistence.enabled is false")
        dsn = os.environ.get(pcfg.get("dsn_env", "EI_TEST_DSN"), "")
        pattern = pcfg.get("allowed_dbname_pattern", r"(?:^|_)test(?:_|$)")
        m = re.search(r"(?:dbname=|/)([A-Za-z0-9_\-]+)(?:\?|\s|$)", dsn)
        dbname = m.group(1) if m else ""
        if not dsn or not re.search(pattern, dbname):
            raise PersistenceDisabled(f"refusing to write to database {dbname!r}: does not match {pattern!r}")
        if connect is None:
            import psycopg2
            connect = psycopg2.connect
        import json
        conn = connect(dsn)
        n = 0
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO earnings_inflection.ei_runs (as_of, config_json) VALUES (%s, %s) RETURNING id",
                            (result.as_of, json.dumps(to_jsonable({k: v for k, v in self.cfg.items() if k != "persistence"}))))
                run_id = cur.fetchone()[0]
                for a in result.assessments:
                    cur.execute("INSERT INTO earnings_inflection.ei_assessments (run_id, ticker, as_of, evidence_status, "
                                "review_status, scenario_status, payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                                (run_id, a.ticker, a.as_of, a.evidence_status.value, a.review_status.value,
                                 a.scenario_status.value, json.dumps(to_jsonable(a))))
                    n += 1
            conn.commit()
        finally:
            conn.close()
        return n


def _d(v):
    from datetime import date
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))
