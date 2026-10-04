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
        L.append("| Metric | Target period | Original (date) | Revisions | Outcome |")
        L.append("|---|---|---|---|---|")
        for g in a.guidance:
            q = g.original.quantity
            revs = ", ".join(r.direction.value for r in g.revisions) or "—"
            L.append(f"| {g.metric.value} | {g.target_period_label} | {q.raw if q else '?'} ({_ts(g.original.stated_at)}) "
                     f"| {revs} | {g.outcome.value} {g.outcome_note} |")
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
        common = {k: v for k, v in b.scenarios[0].assumptions.items() if k not in ("revenue_crore", "ebitda_margin_pct")}
        L.append("Common assumptions: " + "; ".join(f"{k} = {v}" for k, v in common.items()))
        for s in b.scenarios:
            for n in s.notes:
                L.append(f"- {s.name}: {n}")
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
