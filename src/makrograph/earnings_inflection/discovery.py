"""Bounded universe discovery and explainable research shortlisting (WP8).

Explicit, run-once, research-only.  No scheduler, no notifications, no action labels.

* Universe = a versioned eligibility snapshot.  A historical run needs a snapshot dated
  on/before the as-of date (within ``max_snapshot_age_days``) or the output carries a
  "limited cohort" label.  Delisted / suspended members stay in the denominator.
* Limits are mandatory: max issuers, max documents, page size and the LLM spend budget.
  Issuers beyond a limit are DEFERRED, never silently dropped, and the run is PARTIAL.
* Order is deterministic and fair: sha256(snapshot_id, issuer) - neither the input order
  nor alphabetical position decides who is assessed first.
* Pages are checkpointed; ``resume`` continues an interrupted run and reproduces the
  full-run output.  ``incremental`` reassesses only issuers whose document versions
  changed since a previous run and reports the change since the previous assessment.
* Lanes keep evidence types apart; ranking is within a lane only, from visible component
  scores (not probabilities).  The shortlist is never padded to a quota.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .contracts import Assessment, EvidenceStatus, MechanismState, ReviewStatus, assert_no_action_language

# Lanes follow the CURRENT FORWARD CATALYST stage first; reported performance is a separate
# dimension on every record (and a lower lane only when no current catalyst exists).
LANES = ("CONFIRMED_FOR_REVIEW", "EXECUTION_VALIDATING", "PROSPECTIVE_SUPPORTED", "POTENTIAL_CATALYST",
         "REPORTED_PERFORMANCE_ONLY", "DATA_REPAIR", "CONTRADICTED_OR_STALE")
_STAGE_LANE = {"confirmed_for_investment_review": "CONFIRMED_FOR_REVIEW",
               "execution_validating": "EXECUTION_VALIDATING",
               "supported_prospective_inflection": "PROSPECTIVE_SUPPORTED",
               "potential_catalyst": "POTENTIAL_CATALYST"}
_STAGE_RANK = {"confirmed_for_investment_review": 4, "execution_validating": 3,
               "supported_prospective_inflection": 2, "potential_catalyst": 1}
_STAGE_QUALITY = {"confirmed_for_investment_review": 1.0, "execution_validating": 0.75,
                  "supported_prospective_inflection": 0.6, "potential_catalyst": 0.3}

DEFAULT_DISCOVERY = {
    "max_issuers": 50,
    "max_documents": 5000,
    "page_size": 10,
    "max_snapshot_age_days": 31,
    "max_shortlist_per_lane": 10,          # a cap, never a quota
    "weights": {"evidence_quality": 0.35, "materiality": 0.30, "timing": 0.15, "persistence": 0.20,
                "risk_penalty": 0.30},
    "critical_checks": {
        "CONFIRMED_FOR_REVIEW": ["identity_resolved", "dated_catalyst", "latest_period_reconciled"],
        "EXECUTION_VALIDATING": ["identity_resolved", "dated_catalyst"],
        "PROSPECTIVE_SUPPORTED": ["identity_resolved", "dated_catalyst"],
        "POTENTIAL_CATALYST": ["identity_resolved", "dated_catalyst"],
        "REPORTED_PERFORMANCE_ONLY": ["identity_resolved", "current_financial_series", "latest_period_reconciled"],
    },
}


# --- universe --------------------------------------------------------------------------

@dataclass
class EligibilitySnapshot:
    snapshot_id: str
    as_of: date
    source: str
    members: list[dict]                     # {"ticker", "status": listed|suspended|delisted, "excluded_reason"?}
    cohort_label: str = ""

    @classmethod
    def load(cls, path) -> "EligibilitySnapshot":
        d = json.loads(Path(path).read_text())
        return cls(d["snapshot_id"], date.fromisoformat(d["as_of"]), d.get("source", ""), d["members"],
                   d.get("cohort_label", ""))

    def contemporaneity(self, run_as_of: date, max_age_days: int) -> str:
        if self.as_of > run_as_of:
            return (f"LIMITED COHORT: snapshot {self.snapshot_id} is dated {self.as_of}, after the run date "
                    f"{run_as_of}; membership may include later listings (look-ahead)")
        if (run_as_of - self.as_of).days > max_age_days:
            return (f"LIMITED COHORT: snapshot {self.snapshot_id} is {(run_as_of - self.as_of).days} days older "
                    "than the run date; later listings / delistings are missing")
        return ""


def fair_order(snapshot_id: str, tickers: list[str]) -> list[str]:
    return sorted(set(tickers), key=lambda t: hashlib.sha256(f"{snapshot_id}|{t.upper()}".encode()).hexdigest())


# --- lanes and ranking ----------------------------------------------------------------------

def _fingerprint(a: Assessment) -> str:
    rows = sorted((s.get("doc_id"), s.get("text_hash"), s.get("extraction_version_id"), s.get("raw_hash"))
                  for s in a.source_manifest)
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


def critical_checks(a: Assessment) -> dict[str, bool]:
    cov = a.coverage
    expected = cov.get("result_periods", {}).get("expected", [])
    latest = expected[-1] if expected else None
    # a blocking reconciliation failure (figures excluded from calculations) in the latest expected period
    latest_bad = bool(latest) and any("excluded from calculations" in c and latest in c for c in a.contradictions)
    stale = any("Stale financial series" in w or "latest parsed" in w for w in a.status_rationale + a.missing_inputs)
    return {
        "identity_resolved": cov.get("identity_basis") != "alias_mapping_not_known_by_cutoff",
        "current_financial_series": not stale and cov.get("reporting_cadence", "none") != "none",
        "latest_period_reconciled": not latest_bad,
        "dated_order_events": all(e.first_public_at is not None for e in a.events),
        "dated_catalyst": all(c.first_public_at is not None for c in a.catalysts),
    }


def current_catalysts(a: Assessment) -> list:
    return [c for c in a.catalysts if c.quantities.get("current", True)]


def lead_catalyst(a: Assessment):
    live = [c for c in current_catalysts(a) if c.stage.value in _STAGE_RANK]
    if not live:
        return None
    return max(live, key=lambda c: (_STAGE_RANK[c.stage.value], c.contribution.share_of_ttm_ebitda or 0.0,
                                    -(c.first_public_at.timestamp() if c.first_public_at else 0)))


def reported_performance(a: Assessment) -> dict:
    """The backward-looking dimension, kept apart from the forward setup."""
    s = a.evidence_status
    lane = ("EXECUTION" if s in (EvidenceStatus.EXECUTION_EMERGING, EvidenceStatus.EXECUTION_CONFIRMED)
            else "COMMITMENT" if s in (EvidenceStatus.COMMITMENT_BACKED, EvidenceStatus.EARLY_COMMITMENT_UNVERIFIED)
            else "ASSERTION" if s == EvidenceStatus.ASSERTION_ONLY else s.value)
    return {"evidence_status": s.value, "category": lane, "basis": a.status_rationale[:3],
            "material_drivers": [f"{d.driver}: {d.change:.1f} {d.unit}" for d in a.drivers
                                 if d.material and d.change is not None][:4]}


def lane_for(a: Assessment, checks: dict[str, bool], cfg: dict) -> tuple[str, list[str]]:
    """Primary input: the most advanced CURRENT forward catalyst.  Muted reported results do
    not demote a supported forward setup; strong reported results without a catalyst sit in
    REPORTED_PERFORMANCE_ONLY."""
    s = a.evidence_status
    lead = lead_catalyst(a)
    cur = current_catalysts(a)
    if lead is not None:
        lane = _STAGE_LANE[lead.stage.value]
        reasons = [f"{lead.kind.value} since {lead.first_public_at.date() if lead.first_public_at else '?'}: "
                   f"{lead.operating_change}"] + lead.stage_reasons[:2]
        others = [c for c in cur if c is not lead and c.stage.value in ("contradicted", "delayed")]
        if others:
            reasons.append("MIXED: also " + ", ".join(f"{c.kind.value} {c.stage.value}" for c in others[:3]))
        if s == EvidenceStatus.CONTRADICTED:
            reasons.append("MIXED: management guidance contradicted (see contradictions)")
    elif any(c.stage.value == "data_unavailable" for c in cur):
        return "DATA_REPAIR", ["catalyst milestones due but results not available"] + a.missing_inputs[:2]
    elif any(c.stage.value in ("contradicted", "delayed") for c in cur):
        bad = [c for c in cur if c.stage.value in ("contradicted", "delayed")]
        return "CONTRADICTED_OR_STALE", [f"{c.kind.value} {c.stage.value}: {(c.stage_reasons or [''])[0]}"
                                         for c in bad[:3]]
    elif s == EvidenceStatus.CONTRADICTED:
        return "CONTRADICTED_OR_STALE", ["management guidance contradicted (see contradictions)"]
    elif s == EvidenceStatus.INSUFFICIENT_EVIDENCE:
        return "DATA_REPAIR", ["insufficient usable evidence"] + a.missing_inputs[:2]
    elif s in (EvidenceStatus.EXECUTION_EMERGING, EvidenceStatus.EXECUTION_CONFIRMED):
        lane, reasons = "REPORTED_PERFORMANCE_ONLY", ["reported results improved; no current forward catalyst"] + \
            list(a.status_rationale[:2])
    else:
        return "", ["assessed: no current forward catalyst and no qualifying reported change"]
    failed = [c for c in cfg["critical_checks"].get(lane, []) if not checks.get(c, True)]
    if failed:
        # a failed critical check blocks only the qualification that depends on it
        if "current_financial_series" in failed and lane == "REPORTED_PERFORMANCE_ONLY":
            return "CONTRADICTED_OR_STALE", [f"failed critical check: {c}" for c in failed]
        return "DATA_REPAIR", [f"failed critical check: {c}" for c in failed]
    adverse = [m for m in a.mechanisms if m.state == MechanismState.ADVERSE and not m.stale]
    if adverse:
        reasons.append("MIXED: adverse mechanism(s) " + ", ".join(m.mechanism.value for m in adverse))
    return lane, reasons


def _clip(x):
    return max(0.0, min(1.0, x))


def score_components(a: Assessment, as_of: date) -> dict[str, float]:
    """Visible heuristics in [0, 1] from the lead forward catalyst; NOT probabilities and not
    return forecasts.  Without a current catalyst, reported performance is scored instead."""
    lead = lead_catalyst(a)
    if lead is not None:
        q = _STAGE_QUALITY[lead.stage.value]
        if lead.facts and all("rating-agency" in f or "ICRA" in f or "CARE" in f or "CRISIL" in f for f in lead.facts):
            q -= 0.05                                      # agency restatement only: corroboration, lower weight
        if a.coverage.get("result_periods", {}).get("missing"):
            q -= 0.1
        # recurring parent earnings when bridged; otherwise the supported EBITDA increment
        share = (lead.contribution.share_of_ttm_parent_pat if lead.contribution.bridge_status == "computed"
                 and lead.contribution.share_of_ttm_parent_pat is not None else lead.contribution.share_of_ttm_ebitda)
        materiality = _clip((share or 0.0) / 0.5) if lead.contribution.status == "estimated" else \
            (0.2 if lead.contribution.status == "potentially_material_unresolved" else 0.0)
        anchor = getattr(lead, "supported_at", None) or lead.first_public_at
        timing = _clip(1 - (as_of - anchor.date()).days / 365) if anchor else 0.0
        primary = [m for m in lead.milestones if m.status.value in ("met", "consistent_muted", "pending",
                                                                     "not_yet_relevant", "missed", "contradicted")]
        met = sum(1 for m in primary if m.status.value == "met")
        persistence = _clip(met / max(1, len(lead.milestones)))
        cur = current_catalysts(a)
        risk = _clip(0.15 * sum(1 for c in cur if c.stage.value in ("contradicted", "delayed"))
                     + 0.05 * len(a.contradictions) + 0.05 * len(a.financing_risks)
                     + 0.05 * sum(1 for c in a.counterparties if c.relationship.value == "confirmed_related")
                     + 0.1 * sum(1 for m in lead.milestones if m.status.value == "missed"))
        return {"evidence_quality": round(_clip(q), 3), "materiality": round(materiality, 3),
                "timing": round(timing, 3), "persistence": round(persistence, 3), "risk_penalty": round(risk, 3)}
    s = a.evidence_status
    q = {EvidenceStatus.EXECUTION_CONFIRMED: 0.5, EvidenceStatus.EXECUTION_EMERGING: 0.3}.get(s, 0.0)
    mats = []
    for d in a.drivers:
        if not d.material or d.change is None:
            continue
        if d.driver == "revenue_yoy_growth":
            mats.append(d.change / 100)
        elif d.driver == "pat_yoy_growth":
            mats.append(d.change / 200)
        elif d.driver == "ebitda_margin_change":
            mats.append(d.change / 1000)
    first = first_signal(a)
    timing = _clip(1 - (as_of - first.date()).days / 365) if first else 0.0
    streak = next((d.current for d in a.drivers if d.driver == "material_growth_streak"), 0) or 0
    adverse = sum(1 for m in a.mechanisms if m.state == MechanismState.ADVERSE and not m.stale)
    risk = _clip(0.15 * adverse + 0.05 * len(a.contradictions) + 0.05 * len(a.financing_risks))
    return {"evidence_quality": round(_clip(q), 3), "materiality": round(_clip(max(mats, default=0.0)), 3),
            "timing": round(timing, 3), "persistence": round(_clip(streak / 4), 3), "risk_penalty": round(risk, 3)}


def first_signal(a: Assessment) -> Optional[datetime]:
    """Earliest defensible signal: for a catalyst, the date its materiality AND execution became
    supportable (not merely its first mention); otherwise the earliest material reported change."""
    lead = lead_catalyst(a)
    if lead is not None:
        return getattr(lead, "supported_at", None) or lead.first_public_at
    times = [d.knowable_at for d in a.drivers if d.material and d.knowable_at]
    times += [m.first_signal_at for m in a.mechanisms if m.qualifies_positive and m.first_signal_at]
    times += [e.first_public_at for e in a.events if e.first_public_at]
    return min(times) if times else None


def summarise(a: Assessment, as_of: date, cfg: dict) -> dict:
    checks = critical_checks(a)
    lane, reasons = lane_for(a, checks, cfg)
    comps = score_components(a, as_of)
    w = cfg["weights"]
    score = round(sum(w[k] * comps[k] for k in ("evidence_quality", "materiality", "timing", "persistence"))
                  - w["risk_penalty"] * comps["risk_penalty"], 4)
    cites = []
    for c in a.what_changed[:6]:
        cites.append({"what": c.what[:220], "first_public": c.first_public_at.isoformat() if c.first_public_at else None})
    exp = a.coverage.get("result_periods", {}).get("expected", [])
    nxt = a.next_checks[:3]
    fs = first_signal(a)
    return {
        "ticker": a.ticker, "company": a.company, "security": a.coverage.get("security_at_as_of", {}),
        "as_of": a.as_of.isoformat(), "evidence_status": a.evidence_status.value,
        "reported_performance": reported_performance(a),
        "forward_setup": _forward(a),
        "review_status": a.review_status.value, "lane": lane, "lane_reasons": reasons, "critical_checks": checks,
        "mechanisms": [{"mechanism": m.mechanism.value, "state": m.state.value, "direction": m.direction,
                        "magnitude": m.magnitude, "unit": m.magnitude_unit, "hypothesis": m.hypothesis}
                       for m in a.mechanisms if m.state.value not in ("insufficient_data", "no_material_change")],
        "first_defensible_signal": fs.isoformat() if fs else None,
        "score_components": comps, "score": score,
        "strongest_citations": cites, "contradictions": a.contradictions[:5],
        "scenarios_available": a.bridge.status.value, "valuation_available": a.valuation.get("status", "NOT_REQUESTED"),
        "next_milestones": ((a.research_summary or {}).get("waiting_for") or [])[:3] or nxt,
        "latest_expected_period": exp[-1] if exp else None,
        "review_effort": {"documents_cited": len(a.sources), "open_checks": len(a.next_checks)},
        "fingerprint": _fingerprint(a),
    }


def _forward(a: Assessment) -> dict:
    lead = lead_catalyst(a)
    rs = a.research_summary or {}
    out = {"detected": rs.get("detected", ""), "investment_review": rs.get("investment_review", ""),
           "catalysts": [{"catalyst_id": c.catalyst_id, "kind": c.kind.value, "stage": c.stage.value,
                          "first_public": c.first_public_at.isoformat() if c.first_public_at else None,
                          "supported_at": (getattr(c, "supported_at", None).isoformat()
                                           if getattr(c, "supported_at", None) else None),
                          "operating_change": c.operating_change[:200], "contribution": c.contribution.status,
                          "base_ebitda_crore": c.contribution.base_crore,
                          "demand_basis": getattr(c, "demand_basis", ""), "confidence": getattr(c, "confidence", "")}
                         for c in current_catalysts(a)]}
    out["lead"] = out["catalysts"][[c.catalyst_id for c in current_catalysts(a)].index(lead.catalyst_id)] \
        if lead is not None else None
    return out


# --- run ---------------------------------------------------------------------------------------

@dataclass
class UniverseRun:
    run_id: str
    run_dir: Path
    status: str = "PARTIAL"
    counts: dict = field(default_factory=dict)
    shortlist: dict = field(default_factory=dict)


class UniverseScanner:
    def __init__(self, pipeline, repo, config: Optional[dict] = None):
        self.pipe, self.repo = pipeline, repo
        self.cfg = {**DEFAULT_DISCOVERY, **(config or {})}
        self.cfg["weights"] = {**DEFAULT_DISCOVERY["weights"], **(config or {}).get("weights", {})}
        self.cfg["critical_checks"] = {**DEFAULT_DISCOVERY["critical_checks"],
                                       **(config or {}).get("critical_checks", {})}

    def run(self, snapshot: EligibilitySnapshot, as_of, runs_root, resume: Optional[Path] = None,
            previous: Optional[Path] = None, stop_after_pages: Optional[int] = None) -> UniverseRun:
        from .pipeline import as_of_datetime
        as_of_dt = as_of_datetime(as_of)
        as_d = as_of_dt.date()
        if resume:
            run_dir = Path(resume)
            ck = json.loads((run_dir / "checkpoint.json").read_text())
            if ck["snapshot_id"] != snapshot.snapshot_id or ck["as_of"] != as_d.isoformat():
                raise ValueError("resume: snapshot or as-of differs from the interrupted run")
            run_id = ck["run_id"]
        else:
            run_id = f"{as_d.isoformat()}_{snapshot.snapshot_id}_{uuid.uuid4().hex[:8]}"
            run_dir = Path(runs_root) / run_id
            run_dir.mkdir(parents=True, exist_ok=False)            # immutable: never overwrite a run
            (run_dir / "issuers").mkdir()
            ck = {"run_id": run_id, "snapshot_id": snapshot.snapshot_id, "as_of": as_d.isoformat(),
                  "config": self.cfg, "done": [], "pages": 0, "documents": 0}
        prev = _load_previous(previous) if previous else {}
        members = {m["ticker"].upper(): m for m in snapshot.members}
        excluded = {t: m["excluded_reason"] for t, m in members.items() if m.get("excluded_reason")}
        eligible = fair_order(snapshot.snapshot_id, [t for t in members if t not in excluded])
        budget_cap = eligible[:self.cfg["max_issuers"]]
        deferred = eligible[self.cfg["max_issuers"]:]
        todo = [t for t in budget_cap if t not in set(ck["done"])]
        pages = [todo[i:i + self.cfg["page_size"]] for i in range(0, len(todo), self.cfg["page_size"])]
        for pi, page in enumerate(pages):
            if stop_after_pages is not None and pi >= stop_after_pages:
                break
            for t in page:
                if ck["documents"] >= self.cfg["max_documents"]:
                    deferred.append(t)
                    continue
                rec = self._assess(t, as_of_dt, members[t], prev.get(t))
                ck["documents"] += rec.get("documents", 0)
                (run_dir / "issuers" / f"{t}.json").write_text(json.dumps(rec, indent=1, default=str))
                ck["done"].append(t)
            ck["pages"] += 1
            (run_dir / "checkpoint.json").write_text(json.dumps(ck, indent=1, default=str))
        (run_dir / "checkpoint.json").write_text(json.dumps(ck, indent=1, default=str))
        interrupted = len(ck["done"]) + len([t for t in deferred if t in budget_cap]) < len(budget_cap)
        return self._finish(run_id, run_dir, snapshot, as_d, excluded, eligible, deferred, interrupted)

    def _assess(self, ticker, as_of_dt, member, prev_rec) -> dict:
        try:
            docs = self.repo.documents_for([ticker], self.pipe.country, as_of_dt)
        except Exception as e:
            return {"ticker": ticker, "outcome": "failed", "error": f"{type(e).__name__}: {e}"}
        if not docs:
            return {"ticker": ticker, "outcome": "not_assessed", "reason": "no documents in the source by as-of",
                    "member_status": member.get("status", "listed"), "lane": "DATA_REPAIR"}
        if prev_rec and prev_rec.get("outcome") == "assessed" and prev_rec.get("doc_set") == _doc_set(docs):
            return {**prev_rec, "carried_forward": True, "change_since_previous": "no new or amended documents"}
        try:
            a = self.pipe.assess(ticker, as_of_dt)
        except Exception as e:
            return {"ticker": ticker, "outcome": "failed", "error": f"{type(e).__name__}: {e}"}
        s = summarise(a, as_of_dt.date(), self.cfg)
        assert_no_action_language(s)
        rec = {**s, "outcome": "assessed", "member_status": member.get("status", "listed"),
               "documents": len(a.source_manifest), "doc_set": _doc_set(docs)}
        rec["change_since_previous"] = _delta(prev_rec, rec)
        return rec

    def _finish(self, run_id, run_dir, snapshot, as_d, excluded, eligible, deferred, interrupted) -> UniverseRun:
        recs = [json.loads(p.read_text()) for p in sorted((run_dir / "issuers").glob("*.json"))]
        assessed = [r for r in recs if r["outcome"] == "assessed"]
        not_assessed = [r for r in recs if r["outcome"] == "not_assessed"]
        failed = [r for r in recs if r["outcome"] == "failed"]
        lanes: dict[str, list[dict]] = {l: [] for l in LANES}
        for r in assessed + not_assessed:
            if r.get("lane"):
                lanes[r["lane"]].append(r)
        cap = self.cfg["max_shortlist_per_lane"]
        shortlist = {}
        for lane, rs in lanes.items():
            rs.sort(key=lambda r: (-(r.get("score") or 0.0),
                                   hashlib.sha256(f"{snapshot.snapshot_id}|{r['ticker']}".encode()).hexdigest()))
            shortlist[lane] = [{k: v for k, v in r.items() if k not in ("doc_set",)} for r in rs[:cap]]
        n_elig = len(eligible)
        counts = {"scanned": len(snapshot.members), "excluded": len(excluded), "eligible": n_elig,
                  "completed": len(assessed), "not_assessed_no_documents": len(not_assessed), "failed": len(failed),
                  "deferred": len(deferred), "no_qualifying_evidence": sum(1 for r in assessed if not r.get("lane")),
                  "coverage_rate": round(len(assessed) / n_elig, 3) if n_elig else 0.0}
        complete = not deferred and not interrupted and not failed
        status = "COMPLETE" if complete else "PARTIAL"
        cohort = snapshot.contemporaneity(as_d, self.cfg["max_snapshot_age_days"]) or snapshot.cohort_label
        out = {"run_id": run_id, "as_of": as_d.isoformat(), "snapshot_id": snapshot.snapshot_id,
               "snapshot_source": snapshot.source, "cohort_note": cohort, "status": status,
               "coverage_complete": complete, "counts": counts,
               "excluded": excluded, "deferred": deferred,
               "not_assessed": [r["ticker"] for r in not_assessed], "failed": {r["ticker"]: r["error"] for r in failed},
               "ranking": {"weights": self.cfg["weights"],
                           "note": "heuristic component scores within a lane; not probabilities or return forecasts"},
               "lanes": shortlist,
               "notice": "Research shortlist only: research questions, not investment actions."}
        assert_no_action_language({k: v for k, v in out.items() if k != "notice"})
        (run_dir / "shortlist.json").write_text(json.dumps(out, indent=1, default=str))
        (run_dir / "shortlist.md").write_text(render_shortlist(out))
        return UniverseRun(run_id, run_dir, status, counts, out)


def _doc_set(docs) -> str:
    rows = sorted((d.doc_id, d.content_hash or "", d.extraction_version_id or "",
                   d.available_at.isoformat() if d.available_at else "") for d in docs)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _load_previous(prev_dir) -> dict:
    out = {}
    for p in (Path(prev_dir) / "issuers").glob("*.json"):
        r = json.loads(p.read_text())
        out[r["ticker"]] = r
    return out


def _delta(prev: Optional[dict], cur: dict) -> str:
    if not prev:
        return "first assessment"
    if prev.get("outcome") != "assessed":
        return f"previously {prev.get('outcome')}"
    parts = []
    pl, cl = (prev.get("forward_setup") or {}).get("lead") or {}, (cur.get("forward_setup") or {}).get("lead") or {}
    if (pl.get("catalyst_id"), pl.get("stage")) != (cl.get("catalyst_id"), cl.get("stage")):
        parts.append(f"lead catalyst {pl.get('kind', 'none')} {pl.get('stage', '')} -> "
                     f"{cl.get('kind', 'none')} {cl.get('stage', '')}".replace("  ", " "))
    if prev.get("evidence_status") != cur["evidence_status"]:
        parts.append(f"reported status {prev.get('evidence_status')} -> {cur['evidence_status']}")
    if prev.get("lane") != cur["lane"]:
        parts.append(f"lane {prev.get('lane') or 'none'} -> {cur['lane'] or 'none'}")
    pm = {m["mechanism"]: m["state"] for m in prev.get("mechanisms", [])}
    for m in cur["mechanisms"]:
        if pm.get(m["mechanism"]) != m["state"]:
            parts.append(f"{m['mechanism']} {pm.get(m['mechanism'], 'none')} -> {m['state']}")
    return "; ".join(parts) or "new documents, no change in status, lane or mechanisms"


def render_shortlist(s: dict) -> str:
    c = s["counts"]
    L = [f"# Research shortlist - {s['as_of']} (run {s['run_id']})", "",
         f"> {s['notice']}", "",
         f"Status: **{s['status']}** (coverage complete: {s['coverage_complete']}). Snapshot {s['snapshot_id']} "
         f"({s['snapshot_source']}). {s['cohort_note']}", "",
         f"Scanned {c['scanned']} · excluded {c['excluded']} · eligible {c['eligible']} · completed {c['completed']} · "
         f"no documents {c['not_assessed_no_documents']} · failed {c['failed']} · deferred {c['deferred']} · "
         f"assessed with no qualifying evidence {c['no_qualifying_evidence']} · coverage {c['coverage_rate']:.0%}", "",
         f"Ranking within a lane uses visible components (weights {s['ranking']['weights']}); "
         f"{s['ranking']['note']}.", ""]
    for lane, rows in s["lanes"].items():
        L.append(f"## {lane} ({len(rows)})")
        if not rows:
            L.append("- none (no issuer met this lane's evidence standard; the list is never padded)")
        for r in rows:
            if r.get("outcome") == "not_assessed":
                L.append(f"- **{r['ticker']}**: not assessed - {r['reason']}")
                continue
            mech = ", ".join(f"{m['mechanism']}={m['state']}" for m in r["mechanisms"]) or "none"
            lead = (r.get("forward_setup") or {}).get("lead")
            L.append(f"- **{r['ticker']}** (score {r['score']} = {r['score_components']}); first defensible signal "
                     f"{r['first_defensible_signal']}")
            if lead:
                L.append(f"  - forward catalyst: {lead['kind']} [{lead['stage']}] first public {lead['first_public']}"
                         f"; {lead['operating_change']}; contribution {lead['contribution']}"
                         + (f" (base {lead['base_ebitda_crore']} cr EBITDA/yr)" if lead.get('base_ebitda_crore')
                            is not None else ""))
            rp = r.get("reported_performance") or {}
            L.append(f"  - reported performance (separate): {rp.get('evidence_status', r['evidence_status'])}"
                     f"; mechanisms: {mech}")
            L.append(f"  - change since previous: {r.get('change_since_previous')}")
            for x in r["strongest_citations"][:3]:
                L.append(f"  - cite: {x['what']} (public {x['first_public']})")
            if r["contradictions"]:
                L.append(f"  - CONTRADICTIONS: {'; '.join(r['contradictions'][:3])}")
            for q in r["next_milestones"]:
                L.append(f"  - research question: {q}")
            L.append(f"  - scenarios: {r['scenarios_available']}; valuation: {r['valuation_available']}; review effort: "
                     f"{r['review_effort']['documents_cited']} documents, {r['review_effort']['open_checks']} open checks")
        L.append("")
    return "\n".join(L)
