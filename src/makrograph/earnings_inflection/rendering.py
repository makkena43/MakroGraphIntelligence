"""JSON + Markdown rendering with mandatory source and limitation sections.

Rendering refuses an assessment that lacks limitations, lacks sources while
claiming evidence, or contains investment-action language in generated text.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from .contracts import Assessment, ScenarioStatus, assert_no_action_language, to_jsonable


def render_json(a: Assessment) -> str:
    a.validate()
    return json.dumps(to_jsonable(a), indent=2, ensure_ascii=False)


def _ts(d: Optional[datetime]) -> str:
    return d.strftime("%Y-%m-%d %H:%M %Z").strip() if d else "unknown"


def render_markdown(a: Assessment) -> str:
    a.validate()
    L: list[str] = []
    L.append(f"# Earnings inflection evidence — {a.company or a.ticker} ({a.ticker})")
    L.append("")
    L.append(f"> {a.notice}")
    L.append("")
    L.append(f"- As of: **{_ts(a.as_of)}** (only documents public by then are used)")
    L.append(f"- Market: {a.country} · listing: {a.listing_segment.value} · business model: {a.issuer_model.value}")
    L.append(f"- Evidence status: **{a.evidence_status.value}** · review: {a.review_status.value} · "
             f"scenarios: {a.scenario_status.value}")
    L.append("")
    L.append("## Why this status")
    L += [f"- {x}" for x in a.status_rationale] or ["- (none)"]
    L.append("")
    th = a.thesis
    if th is not None:
        L.append("## Forward-looking thesis (per mechanism; research states, not actions)")
        L.append(f"- Thesis stage: **{th.stage.value}** · confidence: {th.confidence}"
                 + (f" · earliest defensible signal still standing: {_ts(th.earliest_defensible_at)}"
                    if th.earliest_defensible_at else "")
                 + (" · **STALE**" if th.stale else ""))
        if th.stale_note:
            L.append(f"- Stale: {th.stale_note}")
        if th.last_known_status:
            L.append(f"- Last known evidence status (stale): **{th.last_known_status}** as of {th.last_known_as_of}")
            L += [f"  - {x}" for x in th.last_known_rationale]
        if th.next_milestone:
            L.append(f"- Next milestone: {th.next_milestone}")
        L += [f"- {n}" for n in th.notes]
        if th.outcome_history:
            L.append("- Reported outcomes by period (first publication; not attributed to a mechanism):")
            L += [f"  - {x}" for x in th.outcome_history]
        active = [t for t in th.mechanisms if t.stage.value != "none" or t.positive_evidence or t.negative_evidence]
        for t in active:
            L.append(f"### {t.mechanism.value}: {t.stage.value} (confidence {t.confidence})"
                     + (" — stale" if t.stale else ""))
            if t.leading_signal:
                L.append(f"- Leading signal ({_ts(t.leading_signal_at)}): {t.leading_signal}")
            if t.validation:
                v = t.validation
                L.append(f"- First-results check [{v.outcome.value}]: {v.metric_basis}; due by {v.expected_by}"
                         + (f"; {v.period_end}: {v.observed} (public {_ts(v.observed_at)})" if v.observed else ""))
            if t.confirmation:
                L.append(f"- Second period: {t.confirmation}")
            if t.stale_note:
                L.append(f"- Stale: {t.stale_note}")
            L += [f"- (+) {x}" for x in t.positive_evidence[-6:]]
            L += [f"- (−) {x}" for x in t.negative_evidence[-6:]]
            if t.research_question:
                L.append(f"- Research question: {t.research_question}")
        L.append("")
    L.append("## 1. What changed, where, and when it became public")
    if not a.what_changed:
        L.append("- No qualifying change detected.")
    for c in a.what_changed:
        L.append(f"- [{c.tier.value}] {c.what} — business: {c.business}; first public: {_ts(c.first_public_at)}")
    L.append("")
    L.append("## 2. Assertion vs commitment vs realized execution")
    for tier in ("realized_execution", "commercial_commitment", "management_assertion"):
        n = sum(1 for c in a.what_changed if c.tier.value == tier)
        L.append(f"- {tier}: {n}")
    if a.guidance:
        L.append("")
        L.append("| Metric | Target period | Original (date) | Revisions | vs original | vs latest | Note |")
        L.append("|---|---|---|---|---|---|---|")
        for g in a.guidance:
            q = g.original.quantity
            revs = ", ".join(r.direction.value + (" (reason stated)" if r.explained else " (no reason found)"
                                                  if r.explained is False else "")
                             for r in g.revisions) or "—"
            L.append(f"| {g.metric.value} | {g.target_period_label} | {q.raw if q else '?'} ({_ts(g.original.stated_at)}) "
                     f"| {revs} | {g.outcome.value} | {g.latest_outcome.value} | {g.outcome_note} |")
    tr = a.coverage.get("management_track_record")
    if tr and tr.get("targets"):
        L.append(f"- Track record (counts, not a score): {tr['judged']} judged of {tr['targets']} target(s); "
                 f"vs original {tr['vs_original']}; vs latest {tr['vs_latest']}; "
                 f"{tr['downward_revisions']} downward revision(s), {tr['downward_with_stated_reason']} with a stated "
                 f"reason (credibility is a human judgment). {tr['sample_note']}.")
    L.append("")
    if a.events:
        L.append("")
        L.append("### Order events (deduplicated; latest dated state)")
        L.append("| Event | First public | Stage now | Value now (original) cr | Customer | Relationship | "
                 "Value basis | Tax | Execution period | Mentions | Open questions |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for e in a.events:
            now = f"{e.amount.value:,.1f}" if e.amount else "—"
            orig = f" ({e.original_amount.value:,.1f})" if e.original_amount and e.amount and \
                abs(e.original_amount.value - e.amount.value) > 1e-9 else ""
            L.append(f"| {e.event_id} | {_ts(e.first_public_at)} | {e.current_stage.value if e.current_stage else '—'} "
                     f"| {now}{orig} | {e.counterparty or '(unnamed)'} [{e.customer_verification.value}] "
                     f"| {e.relationship.value} | {e.value_basis.value} | {e.tax_basis.value} "
                     f"| {str(e.duration_months) + ' months' if e.duration_months else 'not disclosed'} "
                     f"| {len(e.doc_ids)} doc(s) | {'; '.join(e.unresolved_fields) or '—'} |")
        L.append("")
    if a.mechanisms:
        L.append("### Earnings mechanisms (each judged on its own evidence)")
        L.append("| Mechanism | State | Direction | Magnitude | Durability | Attribution | Confidence | Invalidators / overlaps |")
        L.append("|---|---|---|---|---|---|---|---|")
        for m in a.mechanisms:
            mag = (f"{m.magnitude:,.1f} {m.magnitude_unit}" if m.magnitude is not None else "unknown")
            if m.magnitude_basis:
                mag += f" ({m.magnitude_basis})"
            inv = "; ".join(m.invalidators + [f"overlaps: {', '.join(m.overlaps_with)}"] * bool(m.overlaps_with))
            L.append(f"| {m.mechanism.value} | {m.state.value} | {m.direction} | {mag} | {m.durability} | "
                     f"{m.attribution or '—'} | {m.confidence} | {inv or '—'} |")
        hyps = [m for m in a.mechanisms if m.hypothesis]
        for m in hyps:
            L.append(f"- Research question ({m.mechanism.value}): {m.hypothesis}")
        L.append("")
    L.append("## 3. Effect on recurring parent-attributable diluted EPS and cash")
    b = a.bridge
    if b.status == ScenarioStatus.COMPUTED_ASSUMPTION_BASED:
        L.append(f"Base: {b.base_period_label}. Scenarios are arithmetic on stated assumptions, not forecasts.")
        L.append("")
        L.append("| Scenario | Revenue (cr) | EBITDA margin | Recurring attributable PAT (cr) | Recurring diluted EPS (INR) |")
        L.append("|---|---|---|---|---|")
        for s in b.scenarios:
            L.append(f"| {s.name} | {s.revenue_crore:,.1f} | {s.assumptions.get('ebitda_margin_pct')}% | "
                     f"{s.recurring_pat_attributable_crore:,.1f} | {s.recurring_diluted_eps:,.2f} |")
        L.append("")
        for s in b.scenarios:
            for n in s.notes:
                L.append(f"- {s.name}: {n}")
        if b.management_case_note:
            L.append(f"- management case: {b.management_case_note}")
        if b.assumption_register:
            L.append("")
            L.append("| Input | Value | Source | Note |")
            L.append("|---|---|---|---|")
            for r in b.assumption_register:
                L.append(f"| {r['input']} | {r['value']} | {r['source']} | {r.get('note', '')} |")
        for c in b.mechanism_contributions:
            if c["mechanism"] == "note":
                L.append(f"- {c['basis']}")
            else:
                eff = (f"{c['annual_effect_crore']:,.1f} cr/yr - {c['basis']}"
                       if c.get("annual_effect_crore") is not None else "not quantified")
                L.append(f"- mechanism {c['mechanism']} ({c['state']}): {eff}")
        for n in b.cash_notes:
            L.append(f"- cash: {n}")
    else:
        L.append(f"Not computed ({b.status.value}). Missing: " + ("; ".join(b.missing_inputs) or "—"))
    if a.drivers:
        L.append("")
        L.append("| Driver | Prior | Current | Change | Material | Basis |")
        L.append("|---|---|---|---|---|---|")
        for d in a.drivers:
            f = lambda v: "—" if v is None else f"{v:,.2f}"  # noqa: E731
            L.append(f"| {d.driver} | {f(d.prior)} | {f(d.current)} | {f(d.change)} {d.unit} | "
                     f"{'yes' if d.material else 'no'} | {d.basis} |")
    L.append("")
    if a.valuation:
        v = a.valuation
        L.append("### Valuation context (optional; does not affect the evidence status)")
        if v.get("status") != "COMPUTED":
            L.append(f"- unavailable: {v.get('reason')}")
        else:
            L.append(f"- price {v['price']['close']} on {v['price']['date']} ({v['security']['symbol']} "
                     f"{v['security']['series']}); shares {v['shares_crore']} crore; market cap "
                     f"{v['market_cap_crore']:,.1f} cr")
            L.append("- scenario multiples: " + "; ".join(
                f"{r['scenario']} {r['pe'] if r['pe'] is not None else r['pe_note']}x" for r in v["scenario_multiples"]))
            L.append("- recurring PAT implied by the price at reference multiples: "
                     + "; ".join(f"{k}: {x:,.1f} cr" for k, x in v["implied_recurring_pat_crore"].items()))
            if v["price_change_context"]:
                L.append("- dated price changes to the as-of date: " + "; ".join(
                    f"{k} {c['pct']:+.1f}%" for k, c in v["price_change_context"].items()))
            L.append(f"- {v['note']}")
        L.append("")
    L.append("## 4. Contradictions, financing needs, customer risks, missing inputs")
    for title, items in (("Contradictions", a.contradictions), ("Financing", a.financing_risks),
                         ("Customer / counterparty", a.customer_risks), ("Missing inputs", a.missing_inputs)):
        L.append(f"**{title}:** " + ("; ".join(items) if items else "none identified"))
        L.append("")
    L.append("## 5. Next evidence / milestones to check")
    L += [f"- {x}" for x in a.next_checks] or ["- none"]
    L.append("")
    L.append("## Sources")
    for s in a.sources:
        L.append(f"- `{s.doc_id}` {s.kind} — {s.title or '(untitled)'} — public {_ts(s.available_at)} "
                 f"[{s.availability_basis}]" + (f" — {s.url}" if s.url else ""))
        if s.quote:
            L.append(f"  > p.{s.page}: \"{s.quote[:300]}\"")
    L.append("")
    L.append("## Coverage")
    for k, v in a.coverage.items():
        L.append(f"- {k}: {v}")
    L.append("")
    L.append("## Limitations")
    L += [f"- {x}" for x in a.limitations]
    md = "\n".join(L) + "\n"
    return md
