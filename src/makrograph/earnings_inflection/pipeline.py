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
from datetime import datetime, time, timezone
from typing import Callable, Optional

from .assessments import (
    decide_status, financing_risks, limitations_for, next_checks, review_status_for, what_changed,
)
from .budget import Budget
from .chunking import chunk_document
from .contracts import (
    IST, Assessment, DocumentKind, Evidence, ListingSegment, Metric, SourceDocument, SourceRef, Unit, to_jsonable,
)
from .counterparty import apply_reference_data, build_profiles
from .document_versions import availability, is_restatement, link_versions
from .drivers import compute_drivers
from .earnings_bridge import build_bridge
from .event_resolution import resolve_events
from .extraction import ConstrainedLLMExtractor, extract_sentence_evidence, garbled_ratio, parse_results_tables
from .financial_series import FinancialSeries
from .coverage import document_coverage, result_period_coverage
from .validation import margin_conflicts
from .guidance_ledger import build_ledger
from .text_artifacts import text_hash
from .identity import (
    AliasRecord, IdentityResolver, IssuerRecord, IssuerRegistry, ReplayMode, SymbolSpan, alias_for_document,
    classify_issuer_model, listing_segment_from,
)
from .validation import (
    Integrity, reconcile_structured, scope_conflicts, validate_evidence, validate_measurements,
)

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
    manifest: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.manifest.get("status", "")


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
        reg = IssuerRegistry.from_dict(getattr(self.repo, "identity_records", lambda: {})() or {})
        reg_file = self.cfg.get("identity", {}).get("registry_file")
        if reg_file:
            import json as _json
            reg = reg.merge(IssuerRegistry.from_dict(_json.loads(open(reg_file).read())))
        reg = reg.merge(IssuerRegistry.from_dict(self.cfg.get("identity", {}).get("issuers", {})))
        self.registry = _legacy_spans_to_registry(spans, reg)
        self.replay_mode = self.cfg.get("replay_mode", "PUBLIC_INFORMATION_RECONSTRUCTION")
        pins = self.cfg.get("pinned_extractions") or {}
        if pins and hasattr(self.repo, "text_policy"):
            self.repo.text_policy.pinned.update(pins)

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
        res.manifest = self._manifest(tickers, res)
        return res

    def _manifest(self, tickers: list[str], res: "RunResult") -> dict:
        """Run manifest: everything needed to reproduce (or audit) this run."""
        import hashlib
        import json
        import subprocess
        import uuid
        try:
            code = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                  cwd=os.path.dirname(__file__), timeout=5).stdout.strip() or "unknown"
        except Exception:
            code = "unknown"
        cfg = {k: v for k, v in self.cfg.items() if k not in ("persistence",)}
        incomplete = [a.ticker for a in res.assessments
                      if a.coverage.get("result_periods", {}).get("missing") or a.coverage.get("unreadable_documents")]
        status = "FAILED" if (res.errors and not res.assessments) else \
                 "PARTIAL" if (res.errors or incomplete) else "COMPLETE"
        return {
            "schema_version": "ei-run-manifest-1",
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "code_version": code,
            "config_hash": hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest(),
            "replay_mode": self.replay_mode,
            "cutoff": res.as_of.isoformat(),
            "universe": list(tickers),
            "counts": {"requested": len(tickers), "completed": len(res.assessments),
                       "failed": len(res.errors), "deferred": 0, "incomplete_coverage": len(incomplete)},
            "failed": res.errors,
            "incomplete_coverage": incomplete,
            "budget": self.budget.summary(),
            "sources": {a.ticker: a.source_manifest for a in res.assessments},
            "status": status,
        }

    @staticmethod
    def pins_from_manifest(manifest: dict) -> dict:
        """doc_id -> artifact version used by an earlier run (for an exact replay)."""
        return {s["doc_id"]: s["extraction_version_id"]
                for srcs in manifest.get("sources", {}).values() for s in srcs if s.get("extraction_version_id")}

    def _retrieve(self, ticker: str, as_of: datetime, mode: str, coverage: dict, notes: list[str]
                  ) -> tuple[Optional[IssuerRecord], list[SourceDocument]]:
        """Documents of the issuer behind ``ticker`` via all eligible dated aliases."""
        issuer, basis = self.registry.resolve(ticker, as_of, mode)
        coverage["identity_basis"] = basis
        if issuer is None:
            sets = [(None, [AliasRecord(alias=ticker, kind="symbol", issuer_id=ticker.upper())], None)]
        else:
            sets = [(issuer, self.registry.eligible_aliases(issuer, as_of, mode), None)]
            preds, pred_notes = self.registry.comparable_predecessors(issuer)
            notes.extend(pred_notes)
            for p in preds:
                link = next(l for l in issuer.predecessors if l.predecessor_issuer_id == p.issuer_id)
                sets.append((p, self.registry.eligible_aliases(p, as_of, mode), link.effective_date))
        out: dict[str, SourceDocument] = {}
        mismatched = 0
        for iss, aliases, until in sets:
            symbols = sorted({a.alias for a in aliases})
            if not symbols:
                continue
            fetch = getattr(self.repo, "documents_for", None)
            docs = fetch(symbols, self.country, as_of) if fetch else \
                [d for sym in symbols for d in self.repo.documents(sym, self.country, as_of)]
            for d in docs:
                ts = availability(d)[0]
                when = ts.date() if ts else d.filed_at
                a = alias_for_document(aliases, d.ticker, when)
                if a is None or (until is not None and when is not None and when >= until):
                    mismatched += 1          # symbol belonged to another issuer then / after merger date
                    continue
                d.issuer_id = (iss.issuer_id if iss else ticker.upper())
                d.alias_used = a.alias
                out.setdefault(d.doc_id, d)
        coverage["aliases_used"] = sorted({d.alias_used for d in out.values()})
        coverage["documents_outside_alias_validity"] = mismatched
        return issuer, list(out.values())

    def assess(self, ticker: str, as_of: datetime) -> Assessment:
        mode = self.replay_mode
        if mode not in ReplayMode.ALL:
            raise ValueError(f"unknown replay mode {mode!r}")
        notes: list[str] = []
        coverage: dict = {"replay_mode": mode}
        policy = getattr(self.repo, "text_policy", None)
        prev_cut = policy.extracted_by if policy is not None else None
        if policy is not None and mode == ReplayMode.SYSTEM:
            policy.extracted_by = as_of          # only text extracted by the cutoff
        try:
            issuer, raw_docs = self._retrieve(ticker, as_of, mode, coverage, notes)
        finally:
            if policy is not None:
                policy.extracted_by = prev_cut
        # Present-day metadata (e.g. fundamentals_snapshot) is display context only.
        meta = self.repo.issuer_metadata(ticker) or {}
        if meta:
            coverage["present_day_context"] = {**meta, "label": "present-day metadata; display only, "
                                                                "not used for historical qualification"}

        # 1. availability first, THEN version linking (no future versions leak in)
        coverage["documents_returned"] = len(raw_docs)
        stamped = [(d, availability(d)[0]) for d in raw_docs]
        visible = [d for d, ts in stamped if ts is not None and ts <= as_of]
        coverage["excluded_unknown_time"] = sum(1 for _, ts in stamped if ts is None)
        coverage["excluded_after_as_of"] = sum(1 for _, ts in stamped if ts is not None and ts > as_of)
        if mode == ReplayMode.SYSTEM:
            # MakroGraph must have held the document by the cutoff, and its text too.
            not_held = [d for d in visible if d.first_seen_at is None or d.first_seen_at > as_of]
            visible = [d for d in visible if d not in not_held]
            coverage["system_replay_excluded_not_ingested"] = sum(1 for d in not_held if d.first_seen_at)
            coverage["system_replay_excluded_unknown_ingestion"] = sum(1 for d in not_held if not d.first_seen_at)
            unproven_text = 0
            for d in visible:
                if d.full_text().strip() and (d.text_available_at is None or d.text_available_at > as_of):
                    d.text, d.pages = None, None
                    d.extraction_status = "TEXT_NOT_PROVEN_BY_CUTOFF"
                    d.extraction_issues.append("text existence by the cutoff cannot be proven (legacy text "
                                               "or extracted later)")
                    unproven_text += 1
            coverage["system_replay_text_unproven"] = unproven_text
        docs = link_versions(visible)   # lineage built only from what was public
        coverage["no_text"] = sum(1 for d in docs if not d.full_text().strip())
        coverage["title_only_classified"] = sum(1 for d in docs if d.kind_basis.startswith("title_only"))
        coverage["garbled_text_docs"] = sum(1 for d in docs if d.full_text() and garbled_ratio(d.full_text()) > 0.3)
        coverage["superseded_or_duplicate"] = sum(1 for d in docs if d.superseded_by)
        docs.sort(key=lambda d: (d.available_at, d.doc_id))   # chronological
        docs_by_id = {d.doc_id: d for d in docs}
        company = (issuer.name if issuer and issuer.name else "") or next((d.company for d in docs if d.company), "") \
            or meta.get("name", "")
        identity_basis = coverage["identity_basis"]

        # 2. classification - dated facts only (registry history, statement layout)
        all_text = "\n".join(d.full_text()[:20000] for d in docs if d.kind.value == "financial_results")
        hist_industry, ind_src = issuer.industry_at(as_of.date()) if issuer else ("", "")
        issuer_model, model_basis = classify_issuer_model(company, hist_industry, all_text)
        coverage["issuer_model_basis"] = model_basis + (f" ({ind_src})" if ind_src else "")
        listing = issuer.listing_at(as_of.date()) if issuer else None
        segment = listing_segment_from(listing.series, listing.board) if listing else ListingSegment.UNKNOWN
        if listing:
            coverage["security_at_as_of"] = {"isin": listing.isin, "exchange": listing.exchange,
                                             "symbol": listing.symbol, "series": listing.series,
                                             "board": listing.board}

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
            # Tables are parsed in EVERY text document, not only those classified
            # as results: a misclassified "Outcome of Board Meeting" filing must not
            # hide its results statement.  The parser itself requires period
            # columns, recognised rows and a unit line.
            if d.kind.value not in ("earnings_call_transcript",):
                rows, iss = parse_results_tables(d, chunks)
                if d.superseded_by:
                    rows = []   # a later version for the same period replaces these numbers
                sys_t = (None if d.first_seen_at is None or d.text_available_at is None
                         else max(d.first_seen_at, d.text_available_at))
                for r in rows:
                    r.restated = is_restatement(d)
                    r.system_available_at = sys_t
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
        recon = reconcile_structured(measurements)
        issues += m_issues + scope_conflicts(measurements)
        coverage["reconciliation"] = {st: sum(1 for r in recon if r.status == st)
                                      for st in (Integrity.VALIDATED, Integrity.DEFINITION_DIFFERENCE,
                                                 Integrity.UNRESOLVED, Integrity.REJECTED)}
        events = resolve_events(evidence)
        ref_notes = apply_reference_data(events, self.cfg.get("counterparties", {}), as_of.date())

        # 5. series, drivers, ledger, counterparties, bridge
        series = FinancialSeries.build(ticker, measurements)
        cadence, end, stale_note = series.current_period(as_of.date())
        ttm_rev = series.ttm(Metric.REVENUE, end, cadence)[0] if end else None
        # Revenue guidance given as an amount must be plausible against the
        # current revenue base (catches "1 million doses"-type mis-reads).
        for e in evidence:
            if (e.usable and e.metric == Metric.REVENUE_GUIDANCE and e.quantity is not None
                    and ttm_rev and not (0.25 * ttm_rev <= e.quantity.value <= 20 * ttm_rev)):
                e.validation_issues.append(
                    f"FATAL: revenue guidance {e.quantity.value:g} cr implausible vs TTM revenue {ttm_rev:.1f} cr")
        drivers, missing = compute_drivers(series, events, evidence, issuer_model, as_of.date(),
                                           self.cfg.get("thresholds"))
        commentary_times = [d.available_at for d in docs if d.kind in (
            DocumentKind.EARNINGS_CALL_TRANSCRIPT, DocumentKind.INVESTOR_PRESENTATION)]
        guidance = build_ledger(evidence, series, as_of, docs_by_id, commentary_times)
        profiles = build_profiles(events, ttm_rev)
        bridge = build_bridge(series, issuer_model, guidance, as_of.date())
        missing += [m for m in bridge.missing_inputs if m not in missing]

        usable_docs = sum(1 for d in docs if d.full_text().strip())
        status, why = decide_status(drivers, events, evidence, guidance, usable_docs, ttm_rev, as_of,
                                    int(self.cfg.get("event_lookback_days", 365)))
        first_public = {d.doc_id: d.available_at for d in docs}

        contradictions = [f"{g.metric.value} {g.target_period_label}: {f}" for g in guidance for f in g.flags]
        contradictions += [f"negated statement: \"{e.quote[:160]}\"" for e in evidence
                           if e.usable and e.modality.value == "negated" and e.tier.value == "management_assertion"][:5]
        seen_recon = set()
        for r in recon:
            if r.status in Integrity.BLOCKING and (r.check, r.period_end, r.period_type, r.detail) not in seen_recon:
                seen_recon.add((r.check, r.period_end, r.period_type, r.detail))
                contradictions.append(f"{r.check} {r.status} for {r.period_type} {r.period_end} ({r.scope}): "
                                      f"{r.detail}; affected figures excluded from calculations")
        contradictions += margin_conflicts(evidence, series)

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
        lim += notes + ref_notes
        if mode == ReplayMode.RECONSTRUCTION:
            lim.append("Public-information reconstruction: documents are included when their public availability "
                       "by the cutoff is proven, even if MakroGraph ingested or extracted them later; this does "
                       "not claim the live system held them at the time.")
        else:
            dropped = (coverage.get("system_replay_excluded_not_ingested", 0)
                       + coverage.get("system_replay_excluded_unknown_ingestion", 0)
                       + coverage.get("system_replay_text_unproven", 0))
            lim.append(f"System-knowledge replay: only documents and text MakroGraph provably held by the cutoff; "
                       f"{dropped} document(s) excluded or text-less because ingestion/extraction time is after "
                       "the cutoff or cannot be proven.")
        lim += [f"Revised figures: {n}" for n in series.lineage_notes]
        defs = sorted({r.detail for r in recon if r.status == Integrity.DEFINITION_DIFFERENCE})
        lim += [f"Definition difference (labelled, not an error): {d}" for d in defs]
        if series.excluded:
            lim.append(f"{len(series.excluded)} reported figure(s) excluded from calculations after failed "
                       f"reconciliation, e.g. {series.excluded[0]}")
        if stale_note:
            lim.insert(0, "Stale financial series, so no growth or margin drivers were computed: " + stale_note
                       + ". Run --diagnose to see why recent results did not parse.")
        if coverage.get("garbled_text_docs"):
            lim.append(f"{coverage['garbled_text_docs']} document(s) contain mostly garbled PDF text "
                       "(letter-spaced or mis-encoded fonts); garbled sentences were ignored.")
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
        coverage.update(document_coverage(docs))
        cov_cadence = cadence or ("H" if segment == ListingSegment.SME else "Q")
        parsed_ends = [e for (m, t, e), p in series.points.items()
                       if m == Metric.REVENUE and t == cov_cadence]
        coverage["result_periods"] = result_period_coverage(as_of.date(), cov_cadence, parsed_ends)
        missing_periods = coverage["result_periods"]["missing"]
        if missing_periods:
            lim.insert(0, f"Results not parsed for {len(missing_periods)} of "
                          f"{len(coverage['result_periods']['expected'])} expected "
                          f"{coverage['result_periods']['cadence']} periods: {', '.join(missing_periods)}.")
        unreadable = coverage["unreadable_documents"]
        if unreadable:
            lim.insert(0, f"{len(unreadable)} document(s) have no readable text "
                          f"({', '.join(sorted({u['status'] for u in unreadable}))}); run the explicit "
                          "--extract step for PDF-only rows. A downloaded PDF is not an extraction.")
        if coverage["partial_extractions"]:
            lim.append(f"{len(coverage['partial_extractions'])} document(s) were only partially extracted "
                       "(page limit or pages without a text layer).")
        source_manifest = [{
            "doc_id": d.doc_id, "text_source": d.text_source, "extraction_version_id": d.extraction_version_id,
            "extraction_status": d.extraction_status, "raw_hash": d.raw_hash,
            "text_hash": text_hash(d.full_text()) if d.full_text() else "",
            "available_at": d.available_at.isoformat() if d.available_at else None,
            "first_seen_at": d.first_seen_at.isoformat() if d.first_seen_at else None,
            "text_available_at": d.text_available_at.isoformat() if d.text_available_at else None,
        } for d in docs]

        a = Assessment(
            ticker=ticker, company=company, country=self.country, as_of=as_of, issuer_model=issuer_model,
            listing_segment=segment, evidence_status=status,
            review_status=review_status_for(evidence, [d.kind_basis for d in docs]),
            scenario_status=bridge.status, status_rationale=why,
            what_changed=what_changed(drivers, events, guidance, evidence, first_public, as_of,
                                      int(self.cfg.get("event_lookback_days", 365))),
            drivers=drivers, guidance=guidance, events=events, counterparties=profiles, bridge=bridge,
            contradictions=contradictions, financing_risks=financing_risks(evidence, ttm_rev, bridge, drivers),
            customer_risks=[f"{p.name}: {f}" for p in profiles for f in p.risk_flags],
            missing_inputs=sorted(set(missing)),
            next_checks=next_checks(drivers, events, guidance, sorted(set(missing)), status),
            sources=sources, limitations=lim, coverage=coverage, source_manifest=source_manifest,
            replay_mode=mode,
        )
        a.validate()
        return a

    # -- diagnostics -----------------------------------------------------------

    def diagnose(self, ticker: str, as_of) -> str:
        """Plain-text report of how each visible document was read (for debugging parsing)."""
        from collections import Counter
        from .chunking import chunk_document
        from .extraction import _table_scale, _resolve_scope, resolve_columns
        as_of = as_of_datetime(as_of)
        raw = self.repo.documents(ticker, self.country, as_of)
        visible = [d for d in raw if availability(d)[0] is not None and availability(d)[0] <= as_of]
        docs = sorted(link_versions(visible), key=lambda d: (d.available_at, d.doc_id))
        out = [f"DIAGNOSE {ticker} as of {as_of.isoformat()}: {len(raw)} returned, {len(docs)} public with a timestamp"]
        all_rows = []
        for d in docs:
            text = d.full_text()
            out.append("")
            out.append(f"== {d.doc_id}  {d.available_at:%Y-%m-%d %H:%M}  {d.title[:70]!r}")
            out.append(f"   kind={d.kind.value} ({d.kind_basis})  chars={len(text)}  pages={text.count(chr(12)) + 1}"
                       f"  garbled_lines={garbled_ratio(text):.0%}" + (f"  superseded_by={d.superseded_by}" if d.superseded_by else ""))
            if not text.strip():
                out.append("   no text: run the PDF-fetch stage with store_text_to_db=True")
                continue
            chunks = chunk_document(d)
            pages = d.pages or text.split("\f")
            tables = [c for c in chunks if c.kind == "table"]
            rows, issues = parse_results_tables(d, chunks)
            all_rows += rows
            parsed_any = False
            for c in tables:
                lines = [l for l in c.text.split("\n") if l.strip()]
                cols, col_issue = resolve_columns(c.header.split("\n") + lines[:4])
                if not cols:
                    continue
                parsed_any = True
                scale = _table_scale(c.header, pages[c.page - 1] if c.page <= len(pages) else "")
                found = Counter(r.metric.value for r in rows
                                if r.quote.split(" | ")[-1].strip() in {l.strip() for l in lines}
                                or r.quote.startswith(c.header.splitlines()[-1] if c.header else "\0"))
                out.append(f"   table p{c.page}: columns=" + ", ".join(f"{dt:%d.%m.%Y}:{t or '?'}" for dt, t in cols)
                           + f"  scale={'crore x' + str(scale) if scale else 'MISSING'}"
                           + f"  scope={_resolve_scope(c, pages).value}")
                out.append(f"      rows matched: {dict(found) or 'none'}")
                if col_issue:
                    out.append(f"      issue: {col_issue}")
            if not parsed_any and re.search(r"financial\s+results|revenue\s+from\s+operations", text, re.I):
                i = re.search(r"revenue\s+from\s+operations", text, re.I)
                start = max(0, (i.start() if i else 0) - 700)
                snippet = text[start:start + 1100].replace("\f", "\n<page break>\n")
                out.append("   RESULTS-LIKE TEXT BUT NO PERIOD COLUMNS FOUND. Text around the revenue row:")
                out += ["      | " + l for l in snippet.split("\n")]
            for i in issues:
                out.append(f"   issue: {i}")
        series = FinancialSeries.build(ticker, all_rows)
        p, end, stale = series.current_period(as_of.date())
        out.append("")
        out.append(f"SERIES scope={series.scope.value} cadence={p} latest={end} {stale}")
        for pt in ("Q", "H", "FY"):
            ends = series.period_ends(Metric.REVENUE, pt)
            if ends:
                out.append(f"   revenue {pt}: " + ", ".join(f"{e:%Y-%m}={series.get(Metric.REVENUE, e, pt).value:,.1f}"
                                                        for e in ends[-10:]))
        return "\n".join(out)

    # -- persistence (disabled by default) ----------------------------------

    def persist(self, result: RunResult, connect=None) -> int:
        pcfg = self.cfg.get("persistence", {})
        if not pcfg.get("enabled", False):
            raise PersistenceDisabled("persistence.enabled is false")
        dsn = os.environ.get(pcfg.get("dsn_env", "EI_TEST_DSN"), "")
        pattern = pcfg.get("allowed_dbname_pattern", r"(?:^|_)test(?:_|$)")
        dbname = _dsn_dbname(dsn)
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
                man = result.manifest or {}
                cur.execute("INSERT INTO earnings_inflection.ei_runs (as_of, config_json, code_version, run_uuid, "
                            "replay_mode, config_hash, run_status, manifest_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                            "RETURNING id",
                            (result.as_of, json.dumps(to_jsonable({k: v for k, v in self.cfg.items() if k != "persistence"})),
                             man.get("code_version"), man.get("run_id"), man.get("replay_mode", self.replay_mode),
                             man.get("config_hash"), man.get("status"),
                             json.dumps(to_jsonable(man)) if man else None))
                run_id = cur.fetchone()[0]
                for a in result.assessments:
                    cur.execute("INSERT INTO earnings_inflection.ei_assessments (run_id, ticker, as_of, evidence_status, "
                                "review_status, scenario_status, payload) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                                (run_id, a.ticker, a.as_of, a.evidence_status.value, a.review_status.value,
                                 a.scenario_status.value, json.dumps(to_jsonable(a))))
                    aid = cur.fetchone()[0]
                    self._persist_children(cur, aid, a)
                    n += 1
            conn.commit()
        finally:
            conn.close()
        return n


    @staticmethod
    def _persist_children(cur, assessment_id: int, a: Assessment) -> None:
        """Source text versions and deduplicated events (with dated states) of one assessment."""
        import json
        from .demand import unverified_reasons
        for src in a.source_manifest:
            cur.execute("INSERT INTO earnings_inflection.ei_source_versions (assessment_id, doc_id, text_source, "
                        "extraction_version_id, extraction_status, raw_hash, text_hash, available_at, first_seen_at, "
                        "text_available_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (assessment_id, src["doc_id"], src.get("text_source"), src.get("extraction_version_id"),
                         src.get("extraction_status"), src.get("raw_hash") or None, src.get("text_hash") or None,
                         src.get("available_at"), src.get("first_seen_at"), src.get("text_available_at")))
        for e in a.events:
            reasons = unverified_reasons(e)
            crore = lambda q: q.value if (q is not None and q.unit == Unit.INR_CRORE) else None  # noqa: E731
            cur.execute("INSERT INTO earnings_inflection.ei_events (assessment_id, event_id, current_stage, "
                        "counterparty, customer_verification, relationship, value_basis, tax_basis, amount_crore, "
                        "original_amount_crore, cancelled_amount_crore, duration_months, reference_id, "
                        "first_public_at, verified, unverified_reasons, ambiguous_with, payload) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (assessment_id, e.event_id, e.current_stage.value, e.counterparty or None,
                         e.customer_verification.value, e.relationship.value, e.value_basis.value,
                         e.tax_basis.value, crore(e.amount), crore(e.original_amount), e.cancelled_amount or 0,
                         e.duration_months, e.reference_id or None, e.first_public_at, not reasons,
                         json.dumps(reasons), json.dumps(list(e.ambiguous_with)), json.dumps(to_jsonable(e))))
            eid = cur.fetchone()[0]
            for i, h in enumerate(e.history):
                cur.execute("INSERT INTO earnings_inflection.ei_event_states (event_row_id, seq, stage, at, doc_id, "
                            "amount_crore, evidence_id, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (eid, i, h.stage.value, h.at, h.doc_id or None, h.amount, h.evidence_id or None,
                             h.note or None))


def _dsn_dbname(dsn: str) -> str:
    """Database name of a libpq keyword DSN or a postgres URI ("" when absent or ambiguous).

    Only the dbname keyword or the URI path counts; a host/socket path that happens to
    contain "test" must never make a production database look like a test one.
    """
    from urllib.parse import parse_qs, unquote, urlparse
    if "://" in dsn:
        u = urlparse(dsn)
        q = parse_qs(u.query).get("dbname")
        return q[-1] if q else unquote(u.path.lstrip("/"))
    names = re.findall(r"(?:^|\s)dbname\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn)
    return names[-1].strip("'") if names else ""


def _legacy_spans_to_registry(spans: list, reg: IssuerRegistry) -> IssuerRegistry:
    """Old-style ``identity.symbol_history`` entries become registry aliases."""
    if not spans:
        return reg
    by_issuer: dict[str, IssuerRecord] = {}
    for sp in spans:
        rec = by_issuer.setdefault(sp.issuer_id, IssuerRecord(issuer_id=sp.issuer_id))
        rec.aliases.append(AliasRecord(alias=sp.symbol, kind="symbol", issuer_id=sp.issuer_id,
                                       valid_from=sp.valid_from, valid_to=sp.valid_to, source="config"))
    return IssuerRegistry(list(by_issuer.values())).merge(reg)


def _d(v):
    from datetime import date
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))
