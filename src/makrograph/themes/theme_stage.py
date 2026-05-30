"""Theme Stage Detection — classifies each theme into Stage 0-5 lifecycle.

Framework:
  Stage 0 — Hidden Formation   : < 4 companies, weak signals, very early
  Stage 1 — Emerging           : 4-9 companies, bottlenecks becoming visible
  Stage 2 — Accelerating       : 10-24 companies, capex committed, institutions entering
  Stage 3 — Consensus / Mania  : 25+ companies, everyone knows, momentum decelerating
  Stage 4 — Saturating         : supply catching demand, margins compressing
  Stage 5 — Declining          : demand slowdown dominant, or transformation beginning

Investment sweet spot: late Stage 0 → early Stage 1
  - Highest asymmetry (3-100x potential)
  - Systems like MakroGraph detect this before market consensus
"""

from dataclasses import dataclass


STAGE_LABELS = {
    0: "Hidden Formation",
    1: "Emerging",
    2: "Accelerating",
    3: "Consensus",
    4: "Saturating",
    5: "Declining",
}

STAGE_ICONS = {
    0: "🔍",
    1: "🌱",
    2: "🚀",
    3: "🎯",
    4: "⚖️",
    5: "📉",
}

STAGE_COLORS = {
    0: "#7c3aed",   # purple  — hidden, rare
    1: "#059669",   # green   — emerging, opportunity
    2: "#2563eb",   # blue    — acceleration, momentum
    3: "#d97706",   # amber   — consensus, caution
    4: "#dc2626",   # red     — saturating, risk
    5: "#6b7280",   # grey    — declining
}

STAGE_RETURN_POTENTIAL = {
    0: "10–100×",
    1: "3–10×",
    2: "2–5×",
    3: "Limited",
    4: "Ordinary",
    5: "Negative risk",
}

STAGE_DESCRIPTIONS = {
    0: (
        "Very early — fewer than 4 companies discussing this. "
        "Strange signals in earnings calls, small supply constraints, new research. "
        "Almost nobody notices. Very high risk, very high reward."
    ),
    1: (
        "Multiple companies now mentioning the same issue. "
        "Supply bottlenecks becoming visible. Customers changing behavior. "
        "Narrative is forming. Graph connects the dots. "
        "This is often the sweet spot for explosive investing."
    ),
    2: (
        "Revenue starting to appear. Institutions entering. "
        "Capex committed by multiple players. "
        "News and social media beginning to pick it up. "
        "Strong momentum but less asymmetric than Stage 1."
    ),
    3: (
        "Everybody knows. Retail entering aggressively. "
        "Excessive valuation expansion. ETFs launching. "
        "YouTube videos, mainstream news, friends discussing it. "
        "Upside limited, drawdown risk rising."
    ),
    4: (
        "Supply catching up to demand. Competition intensifying. "
        "Margins beginning to compress. "
        "Returns becoming ordinary — the easy money is gone."
    ),
    5: (
        "Theme fading OR transforming into a larger second-order theme. "
        "Watch for the next layer: what does this theme enable? "
        "e.g. Cloud → AI infrastructure → Power demand → Nuclear."
    ),
}


@dataclass
class ThemeStage:
    stage: int
    label: str
    icon: str
    color: str
    return_potential: str
    description: str
    evidence: str   # human-readable explanation of why this stage was assigned


def compute_stage(
    company_count: int,
    doc_count: int,
    strength_score: float,
    momentum_score: float,
    conviction: str,
    signal_types: list[str],
    metadata: dict = None,
) -> ThemeStage:
    """Classify a theme into lifecycle Stage 0-5.

    Args:
        company_count:  distinct companies discussing the theme
        doc_count:      total documents mentioning it
        strength_score: 0-100 composite strength
        momentum_score: 0-100 momentum (rate of change)
        conviction:     'emerging' | 'developing' | 'confirmed'
        signal_types:   list of signal types present (e.g. ['supply_bottleneck','capex_increase'])
        metadata:       optional dict with tension_score, demand_count, supply_constraint_count

    Returns:
        ThemeStage dataclass with stage number, label, color, and evidence string.
    """
    sig_set = set(s.lower() for s in (signal_types or []))
    meta = metadata or {}

    has_supply_constraint = "supply_bottleneck" in sig_set or "inventory_drawdown" in sig_set
    has_demand_surge      = "demand_surge" in sig_set or "capex_increase" in sig_set
    has_demand_slow       = "demand_slowdown" in sig_set
    has_supply_ease       = "supply_easing" in sig_set
    has_capex             = "capex_increase" in sig_set
    has_hiring            = "hiring_surge" in sig_set

    tension_score = float(meta.get("tension_score", 0) or 0)
    demand_count  = int(meta.get("demand_count", 0) or 0)
    supply_count  = int(meta.get("supply_constraint_count", 0) or 0)
    capex_count   = int(meta.get("capex_count", 0) or 0)

    # ── Stage 5: Declining / Transforming ─────────────────────────────────
    # Guard: active demand-supply tension (score ≥ 30) means the theme is still
    # in formation/acceleration — it CANNOT be declining regardless of signal mix.
    # A single supply_easing signal alongside dominant demand_surge is NOT late-cycle.
    if has_demand_slow and has_supply_ease and company_count >= 8 and tension_score < 30:
        return _make_stage(5, (
            f"Demand slowing ({demand_count} demand signals) while supply easing — "
            f"classic late-cycle. {company_count} companies discussing this."
        ))

    # ── Stage 4: Saturating ────────────────────────────────────────────────
    # Supply ease + no fresh supply constraint = capacity catching up.
    # Guard: high tension_score means demand still dominates — skip saturation.
    if (
        has_supply_ease and not has_supply_constraint
        and company_count >= 12
        and tension_score < 25
    ):
        return _make_stage(4, (
            f"Supply easing with {company_count} companies — capacity is catching up. "
            "Margin compression likely beginning."
        ))

    # ── Stage 3: Consensus / Mania ─────────────────────────────────────────
    # Very broad adoption AND momentum decelerating relative to strength
    momentum_ratio = (momentum_score / strength_score) if strength_score > 0 else 1.0
    if company_count >= 25 and conviction == "confirmed" and momentum_ratio < 0.75:
        return _make_stage(3, (
            f"{company_count} companies now discussing this — broad consensus. "
            f"Momentum ({momentum_score:.0f}) decelerating vs strength ({strength_score:.0f}). "
            "Asymmetric upside narrowing."
        ))

    # Also Stage 3 if extremely broad with no supply tension (everyone already knows)
    if company_count >= 40 and not has_supply_constraint:
        return _make_stage(3, (
            f"Very broad adoption ({company_count} companies) with no supply bottleneck. "
            "Theme is consensus — market has likely priced it."
        ))

    # ── Stage 2: Accelerating ──────────────────────────────────────────────
    # 10+ companies, capex committed, demand surging, institutions entering
    if company_count >= 10 and has_capex and has_demand_surge and conviction == "confirmed":
        return _make_stage(2, (
            f"{company_count} companies with {capex_count} capex commitments and strong demand. "
            "Confirmed theme with institutional-scale activity."
        ))

    # Also Stage 2 if high tension at scale
    if company_count >= 10 and tension_score >= 40:
        return _make_stage(2, (
            f"High demand-supply tension (score {tension_score:.0f}) across {company_count} companies. "
            "Accelerating phase — revenue becoming visible."
        ))

    # ── Stage 1: Emerging ──────────────────────────────────────────────────
    # 4-9 companies, visible bottlenecks, narrative forming
    if company_count >= 4 and has_supply_constraint and has_demand_surge:
        return _make_stage(1, (
            f"{company_count} companies mentioning supply bottlenecks AND demand surge. "
            f"Demand signals: {demand_count}, supply constraints: {supply_count}. "
            "Narrative forming — this is the early sweet spot."
        ))

    if company_count >= 4 and tension_score >= 20:
        return _make_stage(1, (
            f"Supply-demand tension emerging (score {tension_score:.0f}) across {company_count} companies. "
            "Multiple independent signals — theme is forming."
        ))

    if company_count >= 6 and has_capex and conviction in ("developing", "confirmed"):
        return _make_stage(1, (
            f"{company_count} companies committing capex ({capex_count} capex signals). "
            "Capital being deployed — theme transitioning from early to emerging."
        ))

    # ── Stage 0: Hidden Formation ──────────────────────────────────────────
    return _make_stage(0, (
        f"Only {company_count} companies discussing this so far"
        + (f" with tension score {tension_score:.0f}" if tension_score > 0 else "")
        + ". Early / hidden formation — high risk, high reward if correct."
    ))


def _make_stage(n: int, evidence: str) -> ThemeStage:
    return ThemeStage(
        stage=n,
        label=STAGE_LABELS[n],
        icon=STAGE_ICONS[n],
        color=STAGE_COLORS[n],
        return_potential=STAGE_RETURN_POTENTIAL[n],
        description=STAGE_DESCRIPTIONS[n],
        evidence=evidence,
    )


def stage_from_theme_dict(theme: dict) -> ThemeStage:
    """Convenience wrapper — accepts a plain theme dict (from DB or to_dict())."""
    meta = theme.get("metadata") or {}
    if isinstance(meta, str):
        import json
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return compute_stage(
        company_count  = int(theme.get("company_count") or 0),
        doc_count      = int(theme.get("doc_count") or 0),
        strength_score = float(theme.get("strength_score") or 0),
        momentum_score = float(theme.get("momentum_score") or 0),
        conviction     = (theme.get("conviction") or "emerging").lower(),
        signal_types   = theme.get("signal_types") or [],
        metadata       = meta,
    )


# =====================================================================
# STAGE PROGRESSION TRACKING (compares current quarter vs prior snapshot)
# =====================================================================

def compute_stage_progression(
    current_theme: dict,
    prior_snapshot: dict | None,
) -> dict:
    """Compute stage trend by comparing current theme metrics to last quarter's snapshot.

    Returns a dict with:
        stage_trend:        "advancing" | "stable" | "weakening" | "new"
        progression_score:  0-100, how strongly the theme is advancing
        progression_evidence: human-readable summary of why
        company_growth_pct: % change in company count
        new_signal_types:   list of signal types that didn't exist last quarter

    Args:
        current_theme: dict with company_count, doc_count, signal_types, metadata
        prior_snapshot: dict from mg_theme_snapshots (or None if first detection)

    Logic:
        - new                   → no prior snapshot exists
        - advancing             → company_count grew >30% OR new high-signal types appeared
                                  OR supply_bottleneck signals emerged (transition trigger)
        - stable                → metrics roughly same (±15%)
        - weakening             → company_count dropped >20% AND no new signals
    """
    cur_companies   = int(current_theme.get("company_count") or 0)
    cur_docs        = int(current_theme.get("doc_count") or 0)
    cur_signals     = set((current_theme.get("signal_types") or []))
    cur_meta        = current_theme.get("metadata") or {}
    cur_supply      = int(cur_meta.get("supply_constraint_count", 0) or 0)
    cur_demand      = int(cur_meta.get("demand_count", 0) or 0)

    # ── No prior snapshot → this is a fresh detection ─────────────────────
    if not prior_snapshot:
        return {
            "stage_trend": "new",
            "progression_score": 50.0,
            "progression_evidence": "First detection — no prior baseline.",
            "company_growth_pct": 0.0,
            "new_signal_types": list(cur_signals),
        }

    prior_companies = int(prior_snapshot.get("company_count") or 0)
    prior_docs      = int(prior_snapshot.get("doc_count") or 0)
    prior_top       = prior_snapshot.get("top_entities") or {}
    if isinstance(prior_top, str):
        import json
        try:
            prior_top = json.loads(prior_top)
        except Exception:
            prior_top = {}
    prior_signals   = set(prior_top.get("signal_types", []) if isinstance(prior_top, dict) else [])

    # Company growth %
    if prior_companies > 0:
        growth_pct = ((cur_companies - prior_companies) / prior_companies) * 100.0
    else:
        growth_pct = 100.0 if cur_companies > 0 else 0.0

    # New signal types that didn't exist before
    new_sigs = list(cur_signals - prior_signals)

    # Key transition triggers (Stage 0 → 1 or 1 → 2 evidence)
    supply_emerged = "supply_bottleneck" in new_sigs or "inventory_drawdown" in new_sigs
    capex_emerged  = "capex_increase"    in new_sigs
    demand_emerged = "demand_surge"      in new_sigs

    # ── Score the progression ────────────────────────────────────────────
    progression_score = 50.0  # neutral baseline
    evidence_parts: list[str] = []

    if growth_pct >= 30:
        progression_score += min(growth_pct * 0.5, 30)
        evidence_parts.append(f"Companies +{growth_pct:.0f}%")
    elif growth_pct >= 15:
        progression_score += 10
        evidence_parts.append(f"Companies +{growth_pct:.0f}%")
    elif growth_pct <= -20:
        progression_score -= 20
        evidence_parts.append(f"Companies {growth_pct:.0f}%")

    if supply_emerged:
        progression_score += 20
        evidence_parts.append("⚡ supply constraints emerged")
    if capex_emerged:
        progression_score += 15
        evidence_parts.append("capex now flowing")
    if demand_emerged:
        progression_score += 10
        evidence_parts.append("demand surge appeared")

    progression_score = max(0.0, min(100.0, progression_score))

    # ── Classify the trend ────────────────────────────────────────────────
    if progression_score >= 70 or growth_pct >= 30 or supply_emerged:
        stage_trend = "advancing"
    elif progression_score <= 30 or growth_pct <= -20:
        stage_trend = "weakening"
    else:
        stage_trend = "stable"

    evidence = " | ".join(evidence_parts) if evidence_parts else "Metrics stable vs last quarter"

    return {
        "stage_trend": stage_trend,
        "progression_score": round(progression_score, 1),
        "progression_evidence": evidence,
        "company_growth_pct": round(growth_pct, 1),
        "new_signal_types": new_sigs,
    }


def compute_explosive_potential(theme: dict) -> dict:
    """Compute 'explosive return potential' score (0-100) for a theme.

    The picks-and-shovels investment logic: themes with the highest asymmetric
    return potential are typically:
        (a) DOWNSTREAM of a major primary theme  (+30)  — beneficiary of bigger trend
        (b) Supply-constrained with concentrated supply  (+20)  — pricing power
        (c) Showing demand-supply TENSION                (+20)  — active scarcity
        (d) Advancing stage quarter-over-quarter         (+10)  — momentum
        (e) Still early stage (0 or 1)                   (+20)  — asymmetry remaining

    Returns dict with explosive_score (0-100) and explosive_evidence.
    """
    meta = theme.get("metadata") or {}
    if isinstance(meta, str):
        import json
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    score = 0.0
    evidence: list[str] = []

    # (a) Downstream of a primary mega-theme
    if meta.get("theme_type") == "downstream_constraint":
        score += 30
        driver = meta.get("driven_by") or "primary theme"
        evidence.append(f"📐 picks-and-shovels for {driver}")

    # (b) Supply concentration (oligopoly pricing power)
    n_cos = int(theme.get("company_count") or 0)
    supply = int(meta.get("supply_constraint_count", 0) or 0)
    if supply >= 3 and 0 < n_cos <= 8:
        score += 20
        evidence.append(f"concentrated supply ({n_cos} co's, {supply} constraints)")
    elif supply >= 2 and n_cos <= 12:
        score += 10
        evidence.append("moderate supply concentration")

    # (c) Demand-supply tension
    tension = float(meta.get("tension_score", 0) or 0)
    if tension >= 40:
        score += 20
        evidence.append(f"⚡ active tension ({tension:.0f})")
    elif tension >= 20:
        score += 10
        evidence.append(f"tension building ({tension:.0f})")

    # (d) Stage advancing
    if meta.get("stage_trend") == "advancing":
        score += 10
        evidence.append("📈 advancing stage")

    # (e) Early stage — asymmetric upside remaining
    stage = int(meta.get("stage", theme.get("stage", 0)) or 0)
    if stage in (0, 1):
        score += 20
        evidence.append(f"early stage {stage} — asymmetric upside")
    elif stage == 2:
        score += 10
        evidence.append("stage 2 — still accelerating")

    score = max(0.0, min(100.0, score))
    return {
        "explosive_score": round(score, 1),
        "explosive_evidence": " | ".join(evidence) if evidence else "Standard theme",
    }
