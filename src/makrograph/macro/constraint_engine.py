"""Macro Constraint Engine.

Bridges the macro/policy data layer and the theme detection layer.
For each active theme detected from SEC filings, the engine:

1. Checks macro series for confirming/disconfirming signals
   (e.g. rising yields = headwind for rate-sensitive capex themes)
2. Checks recent policy events for tailwinds (subsidies, grants) or
   headwinds (export controls, tariffs)
3. Checks commodity price trends for input-cost signals
4. Emits macro_events when key thresholds are crossed
5. Writes macro_theme_links for every corroboration / constraint found
6. Returns an enriched theme record with macro_score boost/penalty

The engine moves the system from:
  "Company mentioned AI"
  to
  "AI demand + power shortage + IRA subsidies + tight copper supply → identify beneficiaries"

All reads respect the as_of_date to prevent future leakage in replay mode.
"""

import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# MACRO THRESHOLDS  (trigger macro_events when crossed)
# ------------------------------------------------------------------
THRESHOLDS = {
    # (series_id, direction, threshold, event_type, severity, description_template,
    #  sectors_at_risk, sectors_benefit)
    "DGS10": [
        ("above", 4.5, "rate_shock", 70,
         "10Y Treasury yield above 4.5% — significant financing cost pressure",
         ["Technology", "Real Estate", "Utilities", "Consumer Discretionary"],
         ["Financials"]),
        ("below", 2.0, "rate_easing", 60,
         "10Y Treasury yield below 2.0% — historically accommodative for growth",
         ["Financials"],
         ["Technology", "Real Estate", "Consumer Discretionary"]),
    ],
    "T10Y2Y": [
        ("below", 0.0, "yield_inversion", 80,
         "Yield curve inverted — historical recession precursor (10Y-2Y < 0)",
         ["Financials", "Consumer Discretionary", "Industrials"],
         ["Utilities", "Consumer Staples", "Healthcare"]),
    ],
    "CPIAUCSL": [
        ("above", 5.0, "inflation_spike", 75,
         "CPI above 5% — input cost pressure; Fed likely to tighten",
         ["Consumer Discretionary", "Technology"],
         ["Energy", "Materials"]),
    ],
    "UNRATE": [
        ("above", 6.0, "recession_risk", 65,
         "Unemployment above 6% — demand destruction risk",
         ["Consumer Discretionary", "Industrials"],
         ["Consumer Staples", "Healthcare", "Utilities"]),
    ],
    "FEDFUNDS": [
        ("above", 5.0, "credit_tightening", 70,
         "Fed Funds Rate above 5% — credit conditions tightening sharply",
         ["Real Estate", "Consumer Discretionary", "Technology"],
         ["Financials", "Cash-heavy companies"]),
    ],
    "WTI_CRUDE": [
        ("above", 100.0, "commodity_shock_oil", 75,
         "WTI crude above $100/bbl — energy cost shock",
         ["Transportation", "Consumer Discretionary", "Industrials"],
         ["Energy", "Oil Services"]),
        ("below", 50.0, "commodity_low_oil", 50,
         "WTI crude below $50/bbl — deflationary signal, energy sector stress",
         ["Energy", "Oil Services"],
         ["Transportation", "Consumer Discretionary"]),
    ],
    "HENRY_HUB": [
        ("above", 5.0, "commodity_shock_gas", 65,
         "Henry Hub natural gas above $5/MMBtu — utilities and industrial cost pressure",
         ["Utilities", "Industrials", "Chemicals"],
         ["LNG exporters", "Energy"]),
    ],
    "DCOILWTICO": [
        ("above", 100.0, "commodity_shock_oil", 75,
         "WTI crude above $100/bbl — energy cost shock",
         ["Transportation", "Consumer Discretionary"],
         ["Energy", "Oil Services"]),
    ],
    "COPPER": [
        ("above", 10000.0, "copper_shortage", 70,
         "Copper above $10,000/MT — supply constraint for electrification build-out",
         ["Consumer Discretionary", "Construction"],
         ["Mining", "Copper producers"]),
    ],
    "LITHIUM": [
        ("above", 50000.0, "lithium_shortage", 80,
         "Lithium carbonate above $50,000/MT — EV battery cost spike",
         ["EV", "Consumer Discretionary"],
         ["Lithium miners"]),
    ],
    "BALTIC_DRY": [
        ("above", 3000.0, "freight_pressure", 55,
         "Baltic Dry Index above 3000 — shipping constraint signal",
         ["Industrials", "Consumer Discretionary", "Materials"],
         ["Shipping companies"]),
    ],
}

# Map macro event types → which themes they corroborate or constrain
# (theme_keyword, link_type, strength_boost)
MACRO_EVENT_THEME_MAP = [
    # Positive macro → theme corroborations
    ("rate_easing",         ["technology", "ai", "growth", "cloud", "biotech"],     "corroborates", 15),
    ("inflation_spike",     ["energy", "commodity", "mining", "materials"],         "corroborates", 20),
    ("commodity_shock_oil", ["energy", "lng", "clean energy", "nuclear"],           "corroborates", 25),
    ("copper_shortage",     ["electrification", "ev", "grid", "clean energy"],      "corroborates", 30),
    ("lithium_shortage",    ["ev", "battery", "energy storage"],                    "corroborates", 35),
    ("freight_pressure",    ["logistics", "reshoring", "supply chain"],             "corroborates", 20),
    # Negative macro → theme constraints
    ("rate_shock",          ["real estate", "fintech", "growth", "saas"],           "constrains",   20),
    ("yield_inversion",     ["consumer", "discretionary", "cyclical"],              "constrains",   25),
    ("credit_tightening",   ["leveraged", "high yield", "growth"],                  "constrains",   20),
    ("recession_risk",      ["consumer", "luxury", "auto"],                         "constrains",   30),
    # Policy → theme amplifiers
    ("ira_subsidy",         ["clean energy", "solar", "wind", "ev", "nuclear"],     "amplifies",    40),
    ("chips_act",           ["semiconductor", "chip", "fab"],                       "amplifies",    50),
    ("export_control",      ["semiconductor", "ai", "china", "advanced tech"],      "constrains",   30),
]


class ConstraintEngine:
    """Links macro/policy signals to theme records.

    Usage:
        engine = ConstraintEngine(macro_store, macro_graph)
        results = engine.run(themes, as_of_date)
        # results is list of enriched theme dicts with macro_score, constraints, amplifiers
    """

    def __init__(self, macro_store, macro_graph=None):
        self._store = macro_store
        self._graph = macro_graph  # optional MacroGraphStore

    def run(
        self,
        themes: list[dict],
        as_of_date: Optional[date] = None,
    ) -> list[dict]:
        """Enrich themes with macro context.

        Args:
            themes:      List of theme dicts with at least 'slug' and 'name' keys.
            as_of_date:  Replay date ceiling (default: today).

        Returns:
            Same list with added keys:
              - macro_score:    float, net macro boost (+) or penalty (-)
              - macro_links:    list of {link_type, evidence, strength}
              - policy_links:   list of matching policy events
              - constraint_summary: human-readable summary string
        """
        as_of = as_of_date or date.today()

        # --- 1. Detect macro threshold crossings and emit events ---
        macro_events = self._detect_threshold_events(as_of)

        # --- 2. Get recent policy events ---
        policy_events = self._store.get_recent_policy_events(as_of_date=as_of, limit=100)

        # --- 3. Enrich each theme ---
        enriched = []
        for theme in themes:
            enriched.append(self._enrich_theme(theme, macro_events, policy_events, as_of))

        logger.info(
            f"ConstraintEngine: enriched {len(enriched)} themes with "
            f"{len(macro_events)} macro events, {len(policy_events)} policy events"
        )
        return enriched

    # ----------------------------------------------------------
    def _detect_threshold_events(self, as_of: date) -> list[dict]:
        """Check latest macro values against THRESHOLDS; emit events for crosses."""
        # Pull latest observations for all series in THRESHOLDS
        series_ids = list(THRESHOLDS.keys())
        latest = self._store.get_series_latest(series_ids, as_of_date=as_of)

        # Also check commodity series for commodity_ids (WTI_CRUDE etc)
        commodity_ids = [k for k in series_ids if k in (
            "WTI_CRUDE", "BRENT_CRUDE", "HENRY_HUB", "COPPER", "LITHIUM",
            "COBALT", "BALTIC_DRY", "CONTAINER_RATE",
        )]
        for cid in commodity_ids:
            hist = self._store.get_commodity_history(cid, end_date=as_of)
            if hist:
                last = hist[-1]
                latest[cid] = {
                    "series_id":        cid,
                    "value":            last["value"],
                    "observation_date": last["observation_date"],
                    "series_name":      last["commodity_name"],
                    "units":            last["units"],
                }

        emitted: list[dict] = []
        for series_id, rules in THRESHOLDS.items():
            obs = latest.get(series_id)
            if not obs or obs.get("value") is None:
                continue
            val = float(obs["value"])
            obs_date = obs["observation_date"]

            for direction, threshold, event_type, severity, desc_tmpl, at_risk, benefit in rules:
                triggered = (direction == "above" and val > threshold) or \
                            (direction == "below" and val < threshold)
                if not triggered:
                    continue

                event = {
                    "event_type":     event_type,
                    "series_id":      series_id,
                    "event_date":     obs_date,
                    "description":    f"{desc_tmpl} (current: {val:.2f})",
                    "severity":       severity,
                    "direction":      direction,
                    "threshold_value": threshold,
                    "observed_value":  val,
                    "sectors_at_risk": at_risk,
                    "sectors_benefit": benefit,
                    "themes_triggered": [],
                }
                # Persist the event
                event_id = self._store.upsert_macro_event(event)
                event["_db_id"] = event_id
                emitted.append(event)

        return emitted

    def _enrich_theme(
        self,
        theme: dict,
        macro_events: list[dict],
        policy_events: list[dict],
        as_of: date,
    ) -> dict:
        """Score one theme against all active macro/policy signals."""
        slug = theme.get("slug", "")
        name = (theme.get("name", "") or "").lower()

        macro_links: list[dict] = []
        policy_links: list[dict] = []
        macro_score = 0.0

        # --- Match macro events ---
        for event in macro_events:
            evt_type = event.get("event_type", "")
            for kw_list_entry in MACRO_EVENT_THEME_MAP:
                evt_pattern, theme_kws, link_type, strength = kw_list_entry
                if evt_pattern != evt_type:
                    continue
                if not any(kw in name or kw in slug for kw in theme_kws):
                    continue

                link = {
                    "theme_slug":    slug,
                    "link_type":     link_type,
                    "macro_event_id": event.get("_db_id"),
                    "series_id":     event.get("series_id"),
                    "evidence_text": event.get("description", ""),
                    "strength":      float(strength),
                    "as_of_date":    as_of,
                }
                self._store.upsert_macro_theme_link(link)
                macro_links.append(link)

                if link_type in ("corroborates", "amplifies"):
                    macro_score += strength
                elif link_type == "constrains":
                    macro_score -= strength

        # --- Match policy events ---
        for pe in policy_events:
            pe_sectors = pe.get("sectors_affected") or []
            pe_techs = pe.get("technologies_affected") or []
            pe_title = (pe.get("title", "") or "").lower()
            pe_direction = pe.get("impact_direction", "neutral")

            # Check if this policy touches any keyword in the theme name
            relevant = (
                any(kw in name or kw in slug for kw in
                    [s.lower() for s in pe_sectors + pe_techs] +
                    pe_title.split())
            )
            if not relevant:
                continue

            policy_link_type = "amplifies" if pe_direction == "positive" else \
                               "constrains" if pe_direction == "negative" else \
                               "corroborates"
            magnitude = float(pe.get("impact_magnitude", 10.0))

            link = {
                "theme_slug":     slug,
                "link_type":      policy_link_type,
                "policy_event_id": None,  # we don't have the DB id here
                "evidence_text":  f"Policy: {pe.get('title', '')[:200]}",
                "strength":       magnitude * 0.3,  # policy weight vs macro
                "as_of_date":     as_of,
            }
            self._store.upsert_macro_theme_link(link)
            policy_links.append({
                "title":          pe.get("title", ""),
                "policy_type":    pe.get("policy_type", ""),
                "impact_direction": pe_direction,
                "strength":       magnitude * 0.3,
            })

            if policy_link_type in ("amplifies", "corroborates"):
                macro_score += magnitude * 0.3
            else:
                macro_score -= magnitude * 0.3

        # Build human-readable constraint summary
        constraint_summary = _build_summary(macro_links, policy_links, macro_score)

        result = dict(theme)
        result["macro_score"] = round(macro_score, 1)
        result["macro_links"] = macro_links
        result["policy_links"] = policy_links
        result["constraint_summary"] = constraint_summary
        return result


def _build_summary(macro_links: list[dict], policy_links: list[dict], net_score: float) -> str:
    parts = []
    for ml in macro_links[:3]:
        icon = "✅" if ml["link_type"] in ("corroborates", "amplifies") else "⚠️"
        parts.append(f"{icon} {ml.get('evidence_text', '')[:120]}")
    for pl in policy_links[:2]:
        icon = "🏛" if pl.get("impact_direction") == "positive" else "🚫"
        parts.append(f"{icon} {pl.get('title', '')[:120]}")
    sentiment = "MACRO TAILWIND" if net_score > 10 else "MACRO HEADWIND" if net_score < -10 else "MACRO NEUTRAL"
    return f"[{sentiment} {net_score:+.0f}] " + " | ".join(parts)
