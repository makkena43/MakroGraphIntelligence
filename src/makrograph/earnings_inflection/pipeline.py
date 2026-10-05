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
    IST, Assessment, DocumentKind, Evidence, ListingSegment, Metric, SourceRef, to_jsonable,
)
from .counterparty import build_profiles
from .document_versions import availability, is_restatement, link_versions
from .drivers import compute_drivers
from .earnings_bridge import build_bridge
from .event_resolution import resolve_events
from .extraction import ConstrainedLLMExtractor, extract_sentence_evidence, garbled_ratio, parse_results_tables
from .financial_series import FinancialSeries
from .coverage import document_coverage, result_period_coverage
from .guidance_ledger import build_ledger
from .text_artifacts import text_hash
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
        coverage["garbled_text_docs"] = sum(1 for d in docs if d.full_text() and garbled_ratio(d.full_text()) > 0.3)
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
            # Tables are parsed in EVERY text document, not only those classified
            # as results: a misclassified "Outcome of Board Meeting" filing must not
            # hide its results statement.  The parser itself requires period
            # columns, recognised rows and a unit line.
            if d.kind.value not in ("earnings_call_transcript",):
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
            contradictions=contradictions, financing_risks=financing_risks(evidence, ttm_rev, bridge),
            customer_risks=[f"{p.name}: {f}" for p in profiles for f in p.risk_flags],
            missing_inputs=sorted(set(missing)),
            next_checks=next_checks(drivers, events, guidance, sorted(set(missing)), status),
            sources=sources, limitations=lim, coverage=coverage, source_manifest=source_manifest,
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
