#!/usr/bin/env python3
"""Replay the frozen India selector from 2020 onward and measure outcomes.

This is an evaluation tool, not a report generator and not a source-data
writer.  Selection evidence is restricted to each anchor date; only the price
measurement window looks forward.  It evaluates every user-visible stock
section separately, so an Early/Timing or policy research lead is never
silently credited to the Core Buy gate.

Usage:
    python3 scripts/research/selector_forward_test.py
    python3 scripts/research/selector_forward_test.py --anchors 2022,2023
    python3 scripts/research/selector_forward_test.py --out /tmp/selector_test.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta

import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stock_report"))

from company_capabilities import capability_makers  # noqa: E402
from company_product_roles import (  # noqa: E402
    fetch_company_product_discoveries,
    merge_exact_role_makers,
)
from constraint_ranker import build_final_selection, rank_greatest_constraints  # noqa: E402
from constraint_emergence import (  # noqa: E402
    attach_constraint_exposure_companies, attach_emerging_constraint_makers,
    detect_emerging_constraints, fetch_all_constraint_emergence)
from extract_report_data import REPORTS_DIR, connect, jsonify, q  # noqa: E402
from investment_mechanisms import build_investment_mechanisms  # noqa: E402
import select_stocks as selector  # noqa: E402


ANCHORS = (date(2020, 12, 31), date(2021, 12, 31),
           date(2022, 12, 31), date(2023, 12, 31),
           date(2024, 12, 31), date(2025, 12, 31),
           date(2026, 7, 5))
HORIZONS = ((180, "6m"), (365, "12m"), (730, "24m"), (1095, "36m"))
SECTION_ORDER = (
    ("final_actionable", "Final investable shortlist — position-authority cohort"),
    ("emerging_verified", "Greatest Emerging Constraints — verified makers"),
    ("emerging_pipeline", "Greatest Emerging Constraints — pipeline (building)"),
    ("emerging_exposure", "Greatest Emerging Constraints — exposure (filings)"),
    ("core_buy", "Investment-committee hand-off"),
    ("early_timing", "Early / Timing capped starter"),
    ("discovery_starter", "Discovery Starter experimental sleeve"),
    ("policy_research", "Emerging policy research leads — not a Buy"),
    ("localisation_or_qualification", "Domesticisation / qualification company leads — research only"),
    ("policy_led_deployment", "Policy-led deployment company leads — research only"),
    ("maker_operating", "Evidenced operating direct producers"),
    ("maker_pipeline_direct_role", "Evidenced capacity pipeline / direct role"),
    ("maker_quarantined_exception", "Machine-quarantined company-role exceptions"),
    ("proof_research", "Proof-led maker research"),
    ("pli_research", "PLI research now"),
    ("pli_monitor", "PLI monitor milestone"),
    ("company_product_discovery", "Company-originated product discoveries — unlinked research queue"),
    ("raw_screen", "Raw screen diagnostic"),
)

SECTION_AUTHORITY = {
    key: (
        "POSITION_AUTHORITY" if key in {
            "final_actionable", "core_buy", "early_timing", "discovery_starter",
        } else "RESEARCH_ONLY"
    )
    for key, _ in SECTION_ORDER
}


def _median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 2) if values else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _load_readonly_reference_data(cur, as_of: date) -> None:
    """Load selector globals from existing DB records without seed/write helpers."""
    selector.MANUAL_EXCLUDE.clear()
    for row in q(cur, "SELECT ticker, reason FROM mg_manual_exclusions"):
        ticker = (row.get("ticker") or "").strip().upper()
        if ticker:
            selector.MANUAL_EXCLUDE[ticker] = row.get("reason") or ""

    selector.INDIA_POLICY_SCHEMES.clear()
    selector.TRACKED_SCHEME_META.clear()
    for row in q(cur, """
        SELECT scheme_name, pattern, scheme_class, status, first_detected
        FROM mg_tracked_schemes
        WHERE status = 'active' AND country = 'IN'
          AND (first_detected IS NULL OR first_detected <= %s)
    """, (as_of,)):
        selector.INDIA_POLICY_SCHEMES[row["scheme_name"]] = row["pattern"]
        selector.TRACKED_SCHEME_META[row["scheme_name"]] = {
            "scheme_class": row.get("scheme_class"), "status": row.get("status"),
            "first_detected": row.get("first_detected"),
        }

    selector.CHAIN_LAYER_TYPE.clear()
    for row in q(cur, "SELECT chain_key, layer_type FROM mg_chain_classifications"):
        if row.get("chain_key"):
            selector.CHAIN_LAYER_TYPE[row["chain_key"].strip().lower()] = row.get("layer_type")

    selector.SYMBOL_ALIASES.clear()
    for row in q(cur, """
        SELECT old_symbol, new_symbol FROM mg_symbol_renames
        WHERE status <> 'rejected' AND (confidence = 'high' OR status = 'confirmed')
          AND renamed_on <= %s
    """, (as_of,)):
        old, new = row.get("old_symbol"), row.get("new_symbol")
        if old and new:
            selector.SYMBOL_ALIASES.setdefault(new, []).append(old)
            selector.SYMBOL_ALIASES.setdefault(old, []).append(new)

    selector.MARKET_PROXY_IN.clear()
    selector.MARKET_PROXY_IN.extend(selector.compute_market_proxy(cur, as_of))


def _load_outcome_symbol_aliases(cur, through_date: date) -> None:
    """Load issuer-identity aliases for outcome measurement only.

    A rename that happens after an anchor is future information for the ticker
    label, but not for the economic identity of the already-selected company.
    The selection replay is completed before this function is called.  Loading
    the later, high-confidence alias here prevents a 2024 rename from making a
    stock selected in 2022 look as if it first listed in 2024, without allowing
    any post-anchor filing, price, or company event to affect selection.
    """
    selector.SYMBOL_ALIASES.clear()
    for row in q(cur, """
        SELECT old_symbol, new_symbol FROM mg_symbol_renames
        WHERE status <> 'rejected' AND (confidence = 'high' OR status = 'confirmed')
          AND renamed_on <= %s
    """, (through_date,)):
        old, new = row.get("old_symbol"), row.get("new_symbol")
        if old and new:
            selector.SYMBOL_ALIASES.setdefault(new, []).append(old)
            selector.SYMBOL_ALIASES.setdefault(old, []).append(new)


def _theme_quarters(cur, as_of: date) -> dict[str, int]:
    """The selector's ranker only needs the stored point-in-time theme field."""
    rows = q(cur, """
        SELECT theme_name, metadata
        FROM mg_themes
        WHERE country='IN' AND is_active AND first_detected <= %s
    """, (as_of,))
    out: dict[str, int] = {}
    for row in rows:
        meta = row.get("metadata") or {}
        value = meta.get("confirmed_quarters")
        if row.get("theme_name") and value is not None:
            try:
                out[row["theme_name"]] = int(value)
            except (TypeError, ValueError):
                pass
    return out


def _candidate_policy_screen(cur, as_of: date, candidates: list[dict]) -> dict:
    """Run the policy evidence logic only for the selector's as-of universe.

    The production discovery scan deliberately searches every filing to find
    names outside the mapper.  A forward test needs the *visible candidate
    policy section*, whose final gate is candidate/role scoped; scanning the
    whole corpus for every annual replay makes a read-only test needlessly
    impractical.  This keeps the same dated scheme, intensity and commitment
    evidence fields for the tested candidate universe.
    """
    tickers = sorted({(c.get("ticker") or "").upper() for c in candidates if c.get("ticker")})
    if not tickers:
        return {"qualified": {}, "early_pings": {}}
    commit_verbs = (r"(appl(?:y|ied|ication)|bid|approv\w*|select\w*|sanction\w*|allot\w*|"
                    r"award\w*|letter of (?:intent|award)|\yLoI\y|\yLoA\y|disburs\w*|beneficiar\w*)")
    docs = q(cur, """
        SELECT UPPER(TRIM(d.ticker)) AS ticker, d.filed_at::date AS filed_at, d.raw_text,
               CONCAT_WS(' | ', NULLIF(sm.industry_nse,''), NULLIF(sm.industry_bse,'')) AS industry
        FROM mg_documents d
        LEFT JOIN security_master sm ON sm.nse_symbol = d.ticker
        WHERE d.country='IN' AND d.ticker = ANY(%s) AND d.filed_at <= %s
    """, (tickers, as_of))
    qualified, early = {}, {}
    for scheme, pattern in selector.INDIA_POLICY_SCHEMES.items():
        commitment_pattern = (commit_verbs + r"[\s\S]{0,200}(?:" + pattern + r")"
                              r"|(?:" + pattern + r")[\s\S]{0,200}" + commit_verbs)
        scheme_re = re.compile(selector._python_policy_pattern(pattern), flags=re.I)
        commitment_re = re.compile(selector._python_policy_pattern(commitment_pattern), flags=re.I)
        by_ticker: dict[str, list[dict]] = {}
        for doc in docs:
            if scheme_re.search(doc.get("raw_text") or ""):
                by_ticker.setdefault(doc["ticker"], []).append(doc)
        entries, pings = [], []
        for ticker, matched_docs in by_ticker.items():
            if not ticker or ticker in selector.MANUAL_EXCLUDE:
                continue
            n_last12 = sum(doc["filed_at"] > as_of - timedelta(days=365) for doc in matched_docs)
            n_prior12 = sum(as_of - timedelta(days=730) < doc["filed_at"] <= as_of - timedelta(days=365)
                            for doc in matched_docs)
            commitment_docs = [doc for doc in matched_docs if commitment_re.search(doc.get("raw_text") or "")]
            entry = {
                "ticker": ticker, "industry": matched_docs[0].get("industry") or None,
                "n_docs_total": len(matched_docs), "n_last12m": n_last12, "n_prior12m": n_prior12,
                "trend": ("rising" if n_last12 > n_prior12 else "fading" if n_last12 < n_prior12 else "flat"),
                "first_mention": min(doc["filed_at"] for doc in matched_docs),
                "commitment": ({"first_commit": min(doc["filed_at"] for doc in commitment_docs),
                                "n_commit_docs": len(commitment_docs)} if commitment_docs else None),
            }
            if entry["n_docs_total"] >= 4:
                entries.append(entry)
            elif entry["n_last12m"] >= 1:
                pings.append(entry)
        entries.sort(key=lambda row: (row["commitment"] is None, -row["n_last12m"]))
        pings.sort(key=lambda row: (row["first_mention"] is None, str(row["first_mention"])), reverse=True)
        if entries:
            qualified[scheme] = entries[:15]
        if pings:
            early[scheme] = pings[:10]
    return {"qualified": qualified, "early_pings": early}


def _section_tickers(
    decision: dict,
    pli: dict,
    candidates: list[dict],
    maker_universe_constraints: list[dict],
    company_product_discoveries: list[dict],
    opportunity_mechanisms: dict,
) -> dict[str, list[str]]:
    """Return each visible section's exact ticker cohort.

    The report's maker-universe panel is driven by ``maker_universe_constraints``
    (the displayed primary constraints), not the capped proof-led research queue.
    Keep operating proof and a single-plan/direct-role lead separate: the latter
    is deliberately a research cohort, not an operating-producer claim.
    """
    def unique(rows: list[dict]) -> list[str]:
        seen: set[str] = set()
        ticks: list[str] = []
        for row in rows:
            ticker = (row.get("ticker") or "").upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                ticks.append(ticker)
        return ticks

    operating_rows: list[dict] = []
    pipeline_rows: list[dict] = []
    quarantined_rows: list[dict] = []
    for constraint in maker_universe_constraints:
        operating_rows.extend(constraint.get("verified_current_makers") or [])
        pipeline_rows.extend(constraint.get("pipeline_or_direct_role_makers") or [])
        populations = constraint.get("company_populations") or {}
        quarantined_rows.extend(
            populations.get("quarantined_exceptions") or
            populations.get("unverified_company_leads") or []
        )

    # The PDF renders policy rows only if they link to a selected constraint.
    # Match that public cohort exactly; a credible but unlinked policy item is
    # an internal research monitor, not a client-visible selector section.
    visible_pli_research = [
        row for row in (pli.get("research_leads") or []) if row.get("constraint_links")
    ]
    visible_pli_monitor = [
        row for row in (pli.get("monitor_leads") or []) if row.get("constraint_links")
    ]

    return {
        "final_actionable": unique(decision.get("final_investable_shortlist") or []),
        # The decision layer intentionally stopped calling this a "Core Buy":
        # it is a full-evidence hand-off for normal investment-committee
        # underwriting.  Keeping the internal section key stable preserves
        # historical forward-test comparability without overstating what the
        # selector itself decides.
        "core_buy": list(decision.get("investment_committee_candidate_tickers") or []),
        "early_timing": list(decision.get("early_timing_tickers") or []),
        "discovery_starter": list(decision.get("discovery_starter_tickers") or []),
        "policy_research": unique(decision.get("policy_research_watchlist") or []),
        "localisation_or_qualification": unique(
            opportunity_mechanisms.get("localisation_or_qualification") or []
        ),
        "policy_led_deployment": unique(
            opportunity_mechanisms.get("policy_led_deployment_demand") or []
        ),
        "maker_operating": unique(operating_rows),
        "maker_pipeline_direct_role": unique(pipeline_rows),
        "maker_quarantined_exception": unique(quarantined_rows),
        "proof_research": unique(decision.get("best_stock_research_queue") or []),
        "pli_research": unique(visible_pli_research),
        "pli_monitor": unique(visible_pli_monitor),
        "company_product_discovery": unique(company_product_discoveries),
        "raw_screen": unique(candidates[:8]),
    }


def _selector_at_anchor(cur, as_of: date, *, include_capabilities: bool = True) -> dict:
    """Run the decision-producing selector path, with no report/PDF writes."""
    _load_readonly_reference_data(cur, as_of)
    products, gaps, imports = selector.fetch_constraints(cur, as_of, window_months=12)
    constraint_ledger = selector.fetch_constraint_ledger(cur, as_of, "IN")
    constraint_aliases = selector.fetch_constraint_aliases(cur, as_of, "IN")
    observation_summary = selector.fetch_observation_summary(cur, as_of, "IN")
    supply = selector.fetch_supply_beneficiaries(cur, as_of, window_months=12)
    # The production selector's product-capability input is a dated issuer
    # filing lookup, not a hindsight winner list.  Include it in the replay by
    # default so the historical test evaluates the same broad, supply-side
    # company universe that the current report is designed to surface.  Exact
    # role and lane gates below still decide whether any such name is usable.
    if include_capabilities:
        capability_supply = selector.fetch_capability_beneficiaries(cur, as_of)
        for product, rows in capability_supply.items():
            existing = {(row.get("ticker") or "").upper()
                        for row in supply.get(product, [])}
            supply.setdefault(product, []).extend(
                row for row in rows if (row.get("ticker") or "").upper() not in existing
            )
    # Mirror production: bridge the verified-operating-maker ledger (pharma /
    # chemical / cement / wind) into the constraint universe AND the candidate
    # pool, so the replay evaluates the SAME decision spine the live report
    # produces. Without this the forward test would still measure the old
    # solar/electronics-only selector and miss the new position-authority names.
    _rl_labels, _rl_products, _rl_supply, _rl_makers = selector.fetch_role_ledger_products(
        cur, as_of, products, "IN")
    if _rl_products:
        products.extend(_rl_products)
        for _lab, _rws in _rl_supply.items():
            have = {(x.get("ticker") or "").upper() for x in supply.get(_lab, [])}
            supply.setdefault(_lab, []).extend(
                x for x in _rws if (x.get("ticker") or "").upper() not in have)
    candidates = selector.rank_candidates(cur, supply, as_of, top_n=140,
                                          theme_quarters=_theme_quarters(cur, as_of))
    regime = selector.compute_market_regime(cur, as_of, "IN")
    product_names = [p.get("constrained_product") for p in products if p.get("constrained_product")]
    makers = capability_makers(cur, as_of, product_names)
    # Keep the replay aligned with production: independently evidenced issuer
    # manufacturers may enter a maker universe only through an exact reviewed
    # product link.  Unlinked discoveries remain outside every decision cohort.
    makers = merge_exact_role_makers(cur, as_of, makers)
    # Attach the bridged role-ledger makers to their constraint by display label
    # (same as production), so verified_current_makers is populated for the new
    # process constraints and their names can reach the As-of Decision cohort.
    makers = selector._merge_role_ledger_maker_buckets(makers, _rl_makers)
    # Cohort history is deliberately diagnostic-only.  It does not change
    # selection gates, so omitting its costly payoff replay here cannot alter a
    # selected ticker or create post-anchor leakage in this forward test.
    emergence = fetch_all_constraint_emergence(cur, as_of, "IN")
    constraints = rank_greatest_constraints(
        products, gaps, imports, supply, {}, makers, as_of, top_n=max(24, len(product_names)),
        constraint_ledger=constraint_ledger,
        constraint_aliases=constraint_aliases,
        observation_summary=observation_summary,
        emergence=emergence,
    )
    # This is exactly the constraint population rendered in the PDF's
    # "Who actually makes this?" panel.  Final-decision rows may append
    # downstream monetisation constraints and must not alter that panel's cohort.
    # Company-universe coverage is not a presentation shortlist.  Every
    # detected constraint must contribute its operating, pipeline, and
    # role-review populations or the cohort silently drops ninth-ranked and
    # lower chains.  This mirrors the selector JSON/report contract.
    maker_universe_constraints = constraints
    policy_full = _candidate_policy_screen(cur, as_of, candidates)
    decision = build_final_selection(
        candidates, constraints, policy_screen=policy_full,
        display_constraints=maker_universe_constraints, market_regime=regime,
    )
    pli = selector.compute_pli_pipeline(cur, as_of, policy_full, maker_universe_constraints)
    company_product_discoveries = fetch_company_product_discoveries(cur, as_of)
    policy_company_role_context = selector._policy_company_role_context(
        cur, as_of, policy_full, "IN"
    )
    opportunity_mechanisms = build_investment_mechanisms(
        candidates, constraints, policy_full, decision, as_of,
        policy_company_context=policy_company_role_context,
    )
    # Greatest-constraint-theme detector cohorts (the new report section), by
    # tier — union of each tier's stocks across the year's emerging constraints.
    _emerging = detect_emerging_constraints(cur, as_of, "IN", top_n=8)
    attach_emerging_constraint_makers(cur, as_of, _emerging, "IN")
    attach_constraint_exposure_companies(cur, as_of, _emerging, "IN")
    def _emerging_cohort(field):
        seen, out = set(), []
        for c in _emerging:
            for tk in (c.get(field) or []):
                u = (tk or "").upper()
                if u and u not in seen:
                    seen.add(u)
                    out.append(u)
        return out
    emerging_cohorts = {
        "emerging_verified": _emerging_cohort("stocks_verified"),
        "emerging_pipeline": _emerging_cohort("stocks_pipeline"),
        "emerging_exposure": _emerging_cohort("stocks_exposure"),
    }
    quality_counts = Counter((c.get("constraint_quality") or {}).get("grade", "UNMEASURED")
                             for c in constraints)
    completeness_counts = Counter(((c.get("constraint_quality") or {}).get("evidence_completeness") or {})
                                  .get("level", "THIN") for c in constraints)
    response_counts = Counter((c.get("resolution_clock") or {}).get("supply_response_state", "UNKNOWN")
                              for c in constraints)
    actionable_constraints = {
        product
        for verdict in (decision.get("final_investable_shortlist") or [])
        for product in (verdict.get("constraints") or [])
    }
    constraint_funnel = {
        "detected": len(constraints),
        "structured_as_of_evidence": sum(
            bool(c.get("as_of_constraint_evidence")) for c in constraints
        ),
        "measured_grade_a_or_b": sum(
            (c.get("constraint_quality") or {}).get("grade") in {"A", "B"}
            for c in constraints
        ),
        "binding_demand": sum(
            bool((c.get("constraint_quality") or {}).get("binding_demand"))
            for c in constraints
        ),
        "exact_operating_or_pipeline_role": sum(
            bool((c.get("verified_current_makers") or []) or
                 (c.get("pipeline_or_direct_role_makers") or []))
            for c in constraints
        ),
        "company_capture_catalyst": sum(
            bool((c.get("company_capture_readiness") or {}).get(
                "catalyst_backed_exact_roles"
            )) for c in constraints
        ),
        "actionable_constraint": len(actionable_constraints),
        "purpose": (
            "diagnose whether weak returns originate in constraint coverage, "
            "company-role recovery, earnings capture, or final capital gates"
        ),
    }
    return {
        "as_of_date": as_of.isoformat(),
        "market_regime": regime,
        "section_tickers": {
            **emerging_cohorts,
            **_section_tickers(
                decision, pli, candidates, maker_universe_constraints,
                company_product_discoveries, opportunity_mechanisms,
            ),
        },
        "selected_constraints": [
            {
                "constraint": c.get("constraint"),
                "physical_quality": (c.get("constraint_quality") or {}).get("grade"),
                "evidence_completeness": ((c.get("constraint_quality") or {}).get("evidence_completeness") or {}).get("level"),
                "supply_response_state": (c.get("resolution_clock") or {}).get("supply_response_state"),
            }
            for c in decision.get("constraints") or []
        ],
        "constraint_quality_counts": dict(sorted(quality_counts.items())),
        "evidence_completeness_counts": dict(sorted(completeness_counts.items())),
        "supply_response_counts": dict(sorted(response_counts.items())),
        "constraint_funnel": constraint_funnel,
        "position_authority_rows": list(decision.get("final_investable_shortlist") or []),
        "n_raw_candidates": len(candidates),
        "capability_supply_included": include_capabilities,
    }


def _price_return_from_rows(ticker: str, entry_date: date, exit_date: date, rows: list[dict]) -> dict:
    """Measure a return from rows already fetched for one ticker's symbols.

    A factor is inferred only where the exchange's reported ``prev_close``
    disagrees materially with the previous observed close.  The result remains
    a *price* return (not total return): cash dividends and demerger terms are
    intentionally not invented.  A discontinuity without such an exchange
    factor is excluded and reported rather than silently corrupting results.
    """
    by_date: dict[date, dict] = {}
    for row in rows:
        if row.get("trade_date") not in by_date and row.get("close") is not None:
            by_date[row["trade_date"]] = row
    series = [by_date[key] for key in sorted(by_date)]
    entry = next((row for row in series if row["trade_date"] >= entry_date), None)
    exit_row = next((row for row in reversed(series) if row["trade_date"] <= exit_date), None)
    if not entry or not exit_row or entry["trade_date"] >= exit_row["trade_date"]:
        return {"ticker": ticker, "status": "excluded_no_complete_price_window"}
    # A forward cohort selected at the anchor cannot take credit for a later
    # IPO, a multi-month suspension, or a missing historical alias.  Ten
    # calendar days covers ordinary exchange holidays while keeping every
    # measured position comparable to an anchor-date investment.
    if entry["trade_date"] > entry_date + timedelta(days=10):
        return {
            "ticker": ticker,
            "status": "excluded_not_tradable_at_anchor",
            "first_available_date": entry["trade_date"].isoformat(),
        }
    if exit_row["trade_date"] < exit_date - timedelta(days=10):
        return {
            "ticker": ticker,
            "status": "excluded_no_price_near_horizon",
            "last_available_date": exit_row["trade_date"].isoformat(),
        }

    entry_index = series.index(entry)
    exit_index = series.index(exit_row)
    factor, adjustments = 1.0, []
    previous = entry
    for row in series[entry_index + 1: exit_index + 1]:
        gap_days = (row["trade_date"] - previous["trade_date"]).days
        previous_close = float(previous["close"])
        close, reported_prev = float(row["close"]), row.get("prev_close")
        if gap_days <= 12 and previous_close > 0:
            observed_ratio = close / previous_close
            reported_ratio = (float(reported_prev) / previous_close
                              if reported_prev is not None and float(reported_prev) > 0 else None)
            if reported_ratio is not None and not 0.80 <= reported_ratio <= 1.25:
                factor *= reported_ratio
                adjustments.append({"date": row["trade_date"].isoformat(),
                                    "exchange_prev_close_factor": round(reported_ratio, 5)})
            elif not 0.45 <= observed_ratio <= 2.20:
                # NSE bhavcopies sometimes keep the unadjusted previous close
                # on the ex-date. A simple stock split or bonus then appears as
                # an otherwise impossible one-day jump. On such an event date,
                # the opening auction already reflects the action while the
                # stored prev_close may not. Use open/prev_close as the factor
                # only when open and close are within a normal 25% session
                # range; anything else remains excluded for review.
                open_value = row.get("open")
                action_factor = (
                    float(open_value) / previous_close
                    if open_value is not None and float(open_value) > 0 else None
                )
                normal_session = (
                    action_factor is not None
                    and 0.75 <= close / float(open_value) <= 1.25
                    and (action_factor < 0.45 or action_factor > 2.20)
                )
                if normal_session:
                    factor *= action_factor
                    adjustments.append({
                        "date": row["trade_date"].isoformat(),
                        "inferred_ex_date_open_factor": round(action_factor, 6),
                        "observed_close_ratio": round(observed_ratio, 5),
                    })
                else:
                    return {
                        "ticker": ticker, "status": "excluded_unexplained_price_discontinuity",
                        "date": row["trade_date"].isoformat(), "observed_ratio": round(observed_ratio, 5),
                    }
        previous = row

    adjusted_exit = float(exit_row["close"]) / factor
    result = {
        "ticker": ticker,
        "status": "measured_exchange_adjusted" if adjustments else "measured_no_detected_action",
        "entry_date": entry["trade_date"].isoformat(), "entry_close": round(float(entry["close"]), 2),
        "exit_date": exit_row["trade_date"].isoformat(), "exit_close": round(float(exit_row["close"]), 2),
        "return_pct": round((adjusted_exit / float(entry["close"]) - 1) * 100, 2),
        "exchange_adjustments": adjustments,
    }
    return result


def _price_returns(cur, tickers: list[str], entry_date: date, exit_date: date) -> dict[str, dict]:
    """Fetch all price paths for a horizon in one query, then measure locally.

    The older replay queried the same NSE table once for every ticker in every
    report section and horizon.  That was methodologically correct but made a
    six-anchor verification needlessly slow.  This keeps the identical
    ticker-specific symbol preference and corporate-action calculation, while
    reducing each horizon to a single read-only price query.
    """
    normalized = sorted({(ticker or "").upper() for ticker in tickers if ticker})
    if not normalized:
        return {}
    symbols_by_ticker = {ticker: selector.price_symbols(ticker) for ticker in normalized}
    all_symbols = sorted({symbol for symbols in symbols_by_ticker.values() for symbol in symbols})
    rows = q(cur, """
        SELECT symbol, trade_date, open, close, prev_close
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series='EQ'
          AND trade_date BETWEEN %s - 10 AND %s
        ORDER BY trade_date, symbol
    """, (all_symbols, entry_date, exit_date))
    rows_by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_symbol.setdefault((row.get("symbol") or "").upper(), []).append(row)

    output = {}
    for ticker in normalized:
        # Preserve the prior single-query ordering: prefer the current ticker
        # over a historical alias on a day where both happen to trade.
        symbol_set = {(symbol or "").upper() for symbol in symbols_by_ticker[ticker]}
        ticker_rows = [row for symbol in symbol_set for row in rows_by_symbol.get(symbol, [])]
        ticker_rows.sort(key=lambda row: (
            row.get("trade_date"),
            0 if (row.get("symbol") or "").upper() == ticker else 1,
            row.get("symbol") or "",
        ))
        output[ticker] = _price_return_from_rows(ticker, entry_date, exit_date, ticker_rows)
    return output


def _price_return(cur, ticker: str, entry_date: date, exit_date: date) -> dict:
    """Single-ticker compatibility wrapper for diagnostics and callers."""
    return _price_returns(cur, [ticker], entry_date, exit_date).get(
        ticker, {"ticker": ticker, "status": "excluded_no_complete_price_window"}
    )


def _benchmark_return(cur, entry_date: date, exit_date: date, tickers: list[str]) -> dict:
    by_ticker = _price_returns(cur, tickers, entry_date, exit_date)
    rows = [by_ticker.get(ticker, {"ticker": ticker, "status": "excluded_no_complete_price_window"})
            for ticker in tickers]
    returns = [row["return_pct"] for row in rows if row.get("status", "").startswith("measured")]
    return {
        "return_pct": _mean(returns),
        "n_measured": len(returns), "n_requested": len(tickers),
        "excluded": [row for row in rows if not row.get("status", "").startswith("measured")],
    }


def _official_smallcap_benchmark(cur, entry_date: date, exit_date: date) -> dict | None:
    """Return official Nifty Smallcap 250 TRI performance when ingested."""
    cur.execute("SELECT to_regclass('public.mg_benchmark_prices') AS n")
    if not cur.fetchone()["n"]:
        return None
    rows = q(cur, """
        SELECT price_date, total_return_index, source_url
        FROM mg_benchmark_prices
        WHERE country='IN' AND benchmark_key='NIFTY_SMALLCAP_250_TRI'
          AND price_date BETWEEN %s AND %s
        ORDER BY price_date
    """, (entry_date, exit_date))
    entry = next((row for row in rows if row["price_date"] <= entry_date + timedelta(days=10)), None)
    exit_row = rows[-1] if rows else None
    if not entry or not exit_row or float(entry["total_return_index"]) <= 0:
        return None
    return {
        "return_pct": round(
            (float(exit_row["total_return_index"]) / float(entry["total_return_index"]) - 1) * 100,
            2,
        ),
        "benchmark_key": "NIFTY_SMALLCAP_250_TRI",
        "benchmark_quality": "OFFICIAL_TOTAL_RETURN_INDEX",
        "entry_date": entry["price_date"].isoformat(),
        "exit_date": exit_row["price_date"].isoformat(),
        "source_url": exit_row.get("source_url"),
        "n_measured": len(rows),
        "n_requested": len(rows),
        "excluded": [],
    }


def _section_metrics(rows: list[dict], benchmark: float | None) -> dict:
    returns = [row["return_pct"] for row in rows if row.get("status", "").startswith("measured")]
    alphas = [value - benchmark for value in returns] if benchmark is not None else []
    return {
        "n_requested": len(rows),
        "n_measured": len(returns),
        "n_excluded": len(rows) - len(returns),
        "mean_return_pct": _mean(returns),
        "median_return_pct": _median(returns),
        "positive_return_pct": round(100 * sum(value > 0 for value in returns) / len(returns), 1) if returns else None,
        "multibagger_pct": round(100 * sum(value >= 100 for value in returns) / len(returns), 1) if returns else None,
        "severe_loss_pct": round(100 * sum(value <= -30 for value in returns) / len(returns), 1) if returns else None,
        "mean_alpha_pct": _mean(alphas),
        "median_alpha_pct": _median(alphas),
        "beat_benchmark_pct": (round(100 * sum(value > 0 for value in alphas) / len(alphas), 1)
                                if alphas else None),
    }


def _evaluate_anchor(cur, selection: dict, latest_price_date: date) -> dict:
    as_of = date.fromisoformat(selection["as_of_date"])
    proxy = list(selector.MARKET_PROXY_IN)
    # Selection is already frozen.  Outcome measurement may now use later
    # symbol-renaming metadata solely to stitch the same listed company across
    # its historical and current NSE symbols.
    _load_outcome_symbol_aliases(cur, latest_price_date)
    horizons = [(days, label) for days, label in HORIZONS if as_of + timedelta(days=days) <= latest_price_date]
    if latest_price_date > as_of:
        horizons.append(((latest_price_date - as_of).days, "to_latest"))
    result = {"as_of_date": selection["as_of_date"], "horizons": {}}
    for days, label in horizons:
        target = min(as_of + timedelta(days=days), latest_price_date)
        requested_tickers = list(proxy)
        for tickers in selection["section_tickers"].values():
            requested_tickers.extend(tickers)
        price_by_ticker = _price_returns(cur, requested_tickers, as_of, target)
        benchmark_rows = [price_by_ticker.get(
            ticker, {"ticker": ticker, "status": "excluded_no_complete_price_window"}
        ) for ticker in proxy]
        benchmark_returns = [row["return_pct"] for row in benchmark_rows
                             if row.get("status", "").startswith("measured")]
        fallback_benchmark = {
            "return_pct": _mean(benchmark_returns),
            "n_measured": len(benchmark_returns), "n_requested": len(proxy),
            "excluded": [row for row in benchmark_rows
                         if not row.get("status", "").startswith("measured")],
            "benchmark_key": "TOP25_TRAILING_TURNOVER_PROXY",
            "benchmark_quality": "FALLBACK_NOT_SIZE_MATCHED_NOT_TRI",
        }
        benchmark = _official_smallcap_benchmark(cur, as_of, target) or fallback_benchmark
        section_results = {}
        for key, _display in SECTION_ORDER:
            prices = [price_by_ticker.get(
                ticker, {"ticker": ticker, "status": "excluded_no_complete_price_window"}
            ) for ticker in selection["section_tickers"].get(key, [])]
            section_results[key] = {
                "benchmark": benchmark["return_pct"],
                "metrics": _section_metrics(prices, benchmark["return_pct"]),
                "positions": prices,
            }
        result["horizons"][label] = {
            "target_date": target.isoformat(),
            "benchmark": benchmark,
            "sections": section_results,
        }
    return result


def _aggregate(anchors: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for _days, horizon in HORIZONS:
        by_section: dict[str, list[dict]] = {key: [] for key, _ in SECTION_ORDER}
        benchmarks: list[float] = []
        for anchor in anchors:
            payload = (anchor.get("forward_test") or {}).get("horizons", {}).get(horizon)
            if not payload:
                continue
            if payload["benchmark"].get("return_pct") is not None:
                benchmarks.append(payload["benchmark"]["return_pct"])
            for key, _display in SECTION_ORDER:
                for row in payload["sections"][key]["positions"]:
                    by_section[key].append({**row, "_benchmark_return": payload["benchmark"]["return_pct"]})
        if not any(by_section.values()):
            continue
        benchmark = _mean(benchmarks)
        section_metrics = {}
        for key, rows in by_section.items():
            measured = [row for row in rows if row.get("status", "").startswith("measured")]
            returns = [row["return_pct"] for row in measured]
            alphas = [row["return_pct"] - row["_benchmark_return"] for row in measured
                      if row.get("_benchmark_return") is not None]
            section_metrics[key] = {
                "n_requested": len(rows), "n_measured": len(measured),
                "n_excluded": len(rows) - len(measured),
                "mean_return_pct": _mean(returns), "median_return_pct": _median(returns),
                "positive_return_pct": (round(100 * sum(value > 0 for value in returns) / len(returns), 1)
                                        if returns else None),
                "multibagger_pct": (round(100 * sum(value >= 100 for value in returns) / len(returns), 1)
                                      if returns else None),
                "severe_loss_pct": (round(100 * sum(value <= -30 for value in returns) / len(returns), 1)
                                      if returns else None),
                "mean_alpha_pct": _mean(alphas), "median_alpha_pct": _median(alphas),
                "beat_benchmark_pct": (round(100 * sum(value > 0 for value in alphas) / len(alphas), 1)
                                        if alphas else None),
            }
        out[horizon] = {
            "mean_benchmark_return_pct": benchmark,
            "sections": section_metrics,
        }
    return out


def _actionable_coverage_alarm(anchors: list[dict]) -> dict:
    """Audit whether any position-authority selector lane emitted a cohort.

    This deliberately has no target number of picks and never changes a gate.
    An all-zero result is an implementation/data-coverage alarm, not evidence
    that no historical opportunities existed.  Individual empty years remain
    informative: attractive constraints can genuinely be absent at an anchor.
    """
    lanes = ("core_buy", "early_timing", "discovery_starter")
    per_anchor = []
    lane_totals = {lane: 0 for lane in lanes}
    for anchor in anchors:
        cohorts = anchor.get("section_tickers") or {}
        counts = {lane: len(cohorts.get(lane) or []) for lane in lanes}
        for lane, value in counts.items():
            lane_totals[lane] += value
        per_anchor.append({
            "as_of_date": anchor.get("as_of_date"),
            "lane_counts": counts,
            "actionable_candidate_count": sum(counts.values()),
            "has_actionable_candidate": bool(sum(counts.values())),
        })

    populated = sum(row["has_actionable_candidate"] for row in per_anchor)
    all_empty = bool(per_anchor) and populated == 0
    if all_empty:
        status = "ALARM_NO_ACTIONABLE_COVERAGE"
        message = (
            "Core, Early/Timing, and Discovery Starter emitted zero candidates at every requested anchor. "
            "Treat this as a selector-gate or dated-evidence coverage failure to investigate; "
            "do not infer that no investable opportunities existed."
        )
    elif populated < len(per_anchor):
        status = "WATCH_PARTIAL_ACTIONABLE_COVERAGE"
        message = (
            "At least one anchor has no Core or Early/Timing candidate. This can be a valid "
            "historical outcome, but review the associated dated constraint and company-evidence coverage."
        )
    else:
        status = "PASS_ACTIONABLE_COVERAGE"
        message = "At least one investable-lane candidate was emitted at every requested anchor."
    return {
        "status": status,
        "message": message,
        "does_not_set_pick_target": True,
        "does_not_change_selection_gates": True,
        "lanes": list(lanes),
        "lane_candidate_totals": lane_totals,
        "anchors_with_any_candidate": populated,
        "anchors_requested": len(per_anchor),
        "per_anchor": per_anchor,
    }


def _edge_assessment(
    pooled: dict,
    benchmark_qualities: set[str],
    *,
    evaluation_role: str,
    frozen_at: date | None,
) -> dict:
    """Classify evidence strength without feeding outcomes back into ranking."""
    horizons = {}
    for horizon in ("12m", "24m", "36m"):
        metrics = ((pooled.get(horizon) or {}).get("sections") or {}).get(
            "final_actionable", {}
        )
        horizons[horizon] = {
            key: metrics.get(key) for key in (
                "n_measured", "median_return_pct", "median_alpha_pct",
                "beat_benchmark_pct", "multibagger_pct", "severe_loss_pct",
            )
        }
    official = benchmark_qualities == {"OFFICIAL_TOTAL_RETURN_INDEX"}
    measured = [row for row in horizons.values() if int(row.get("n_measured") or 0) > 0]
    positive_alpha = sum(float(row.get("median_alpha_pct") or 0) > 0 for row in measured)
    majority_beats = sum(float(row.get("beat_benchmark_pct") or 0) >= 50 for row in measured)
    minimum_sample = all(int(row.get("n_measured") or 0) >= 20 for row in measured) and len(measured) >= 2

    if evaluation_role != "holdout":
        status = "DEVELOPMENT_REPLAY_NOT_OUT_OF_SAMPLE"
        reason = (
            "These anchors have been inspected while the rules were developed. Returns are diagnostic "
            "and cannot validate alpha or justify another rule change."
        )
    elif not frozen_at:
        status = "INVALID_HOLDOUT_NO_FREEZE_DATE"
        reason = "A holdout claim requires the immutable logic freeze date."
    elif not official:
        status = "UNVALIDATED_BENCHMARK"
        reason = "A size-appropriate official total-return benchmark is missing."
    elif not minimum_sample:
        status = "INSUFFICIENT_HOLDOUT_SAMPLE"
        reason = "Fewer than two horizons with at least 20 actionable company-vintages."
    elif positive_alpha < 2 or majority_beats < 2:
        status = "NO_DEMONSTRATED_ACTIONABLE_EDGE"
        reason = "The final position-authority cohort did not beat the benchmark robustly across horizons."
    else:
        status = "HOLDOUT_EDGE_CANDIDATE"
        reason = (
            "The frozen actionable cohort cleared the preregistered descriptive hurdle; "
            "continue accumulating untouched vintages before treating the edge as durable."
        )
    return {
        "status": status,
        "reason": reason,
        "evaluation_role": evaluation_role,
        "logic_frozen_at": frozen_at.isoformat() if frozen_at else None,
        "official_total_return_benchmark_only": official,
        "minimum_sample_rule": "at least 20 actionable company-vintages in at least two completed horizons",
        "edge_rule": "positive median benchmark-relative return and >=50% benchmark hit-rate in at least two horizons",
        "actionable_horizons": horizons,
        "research_sections_excluded": True,
        "selection_rules_changed_by_this_test": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only 2020-onward selector forward test")
    parser.add_argument("--out", default=os.path.join(REPORTS_DIR, "selector_forward_test_IN_2022_onward.json"))
    parser.add_argument("--anchors", default="2020,2021,2022,2023,2024,2025",
                        help="comma-separated 31-Dec selection years (default: all)")
    parser.add_argument("--fail-on-coverage-alarm", action="store_true",
                        help="exit non-zero after writing output if Core and Early/Timing are empty at every anchor")
    parser.add_argument("--without-capabilities", action="store_true",
                        help="exclude dated company-capability filings; use only for comparison with legacy selector replays")
    parser.add_argument("--evaluation-role", choices=("development", "holdout"), default="development",
                        help="development is descriptive; holdout may validate only logic frozen before every tested anchor")
    parser.add_argument("--frozen-at",
                        help="YYYY-MM-DD immutable logic freeze date; required with --evaluation-role holdout")
    args = parser.parse_args()
    requested_years = {int(value.strip()) for value in args.anchors.split(",") if value.strip()}
    anchors = tuple(anchor for anchor in ANCHORS if anchor.year in requested_years)
    if not anchors:
        raise ValueError("--anchors must include at least one of: 2020, 2021, 2022, 2023, 2024, 2025")
    frozen_at = date.fromisoformat(args.frozen_at) if args.frozen_at else None
    if args.evaluation_role == "holdout":
        if not frozen_at:
            raise ValueError("--evaluation-role holdout requires --frozen-at")
        if any(anchor < frozen_at for anchor in anchors):
            raise ValueError("every holdout anchor must be on or after --frozen-at")

    conn = connect()
    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(trade_date) AS latest FROM nse_bhavcopy_data WHERE series='EQ'")
            latest = cur.fetchone()["latest"]
            if not latest:
                raise RuntimeError("No EQ price history available")
            anchor_results = []
            for as_of in anchors:
                selection = _selector_at_anchor(
                    cur, as_of, include_capabilities=not args.without_capabilities
                )
                print(f"selected {as_of.isoformat()}: "
                      f"early={len(selection['section_tickers']['early_timing'])}, "
                      f"discovery={len(selection['section_tickers']['discovery_starter'])}, "
                      f"policy={len(selection['section_tickers']['policy_research'])}, "
                      f"raw={len(selection['section_tickers']['raw_screen'])}", flush=True)
                forward = _evaluate_anchor(cur, selection, latest)
                selection["forward_test"] = forward
                anchor_results.append(selection)
                print(f"measured {as_of.isoformat()}", flush=True)
    finally:
        conn.close()

    coverage_alarm = _actionable_coverage_alarm(anchor_results)
    pooled = _aggregate(anchor_results)
    benchmark_qualities = {
        horizon["benchmark"].get("benchmark_quality")
        for anchor in anchor_results
        for horizon in (anchor.get("forward_test") or {}).get("horizons", {}).values()
    }
    edge_assessment = _edge_assessment(
        pooled, benchmark_qualities,
        evaluation_role=args.evaluation_role, frozen_at=frozen_at,
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "country": "IN",
        "selection_anchors": [item["as_of_date"] for item in anchor_results],
        "latest_price_date": latest.isoformat(),
        "method": {
            "selection": "frozen selector replay using only evidence dated on or before each anchor",
            "evaluation_role": args.evaluation_role,
            "freeze_date": frozen_at.isoformat() if frozen_at else None,
            "outcomes": "NSE EQ price return from first close after anchor to last close on/before each horizon",
            "benchmark": (
                "Nifty Smallcap 250 total-return index when available in mg_benchmark_prices; "
                "otherwise a visibly labelled top-25 trailing-turnover fallback that cannot validate alpha"
            ),
            "corporate_actions": "exchange prev_close discontinuities are chained; when prev_close remains unadjusted on an otherwise impossible ex-date, the opening-auction ratio is used only if the same-day close stays within 25% of open; remaining discontinuities are excluded and retained in position detail",
            "tradability": "a position is measured only when a close exists within 10 calendar days of both the anchor and horizon; later listings, suspensions, and incomplete histories are excluded",
            "not_total_return": "cash dividends and demerger consideration are not modeled",
            "cohort_history": "omitted from the replay because it is diagnostic-only and cannot change eligibility or selected tickers",
            "policy_scope": "policy sections use the same evidence gates over the selector candidate universe; the production all-market policy-discovery scan is not needed to evaluate the visible candidate section",
            "report_section_scope": (
                "Every client-visible stock table is measured as a separate, unique-ticker cohort. "
                "Coverage and audit text has no stock cohort; research sections remain research-only regardless of outcome."
            ),
            "company_capabilities": (
                "dated issuer-filing product capabilities included in the supply-side candidate universe"
                if not args.without_capabilities else
                "excluded for legacy-comparison replay only"
            ),
        },
        "section_definitions": dict(SECTION_ORDER),
        "section_authority": SECTION_AUTHORITY,
        "anchors": anchor_results,
        "pooled_completed_horizons": pooled,
        "edge_assessment": edge_assessment,
        "actionable_coverage_alarm": coverage_alarm,
        "logic_manifest": selector.build_logic_manifest(),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as handle:
        json.dump(payload, handle, default=jsonify, indent=2)
    official_benchmark = benchmark_qualities == {"OFFICIAL_TOTAL_RETURN_INDEX"}
    validation_state = edge_assessment["status"]
    if coverage_alarm["status"] == "ALARM_NO_ACTIONABLE_COVERAGE":
        validation_state = "FAILED_ACTIONABLE_COVERAGE"
    validation_manifest = {
        "generated_at": payload["generated_at"],
        "country": "IN",
        "logic_hash": payload["logic_manifest"]["logic_hash"],
        "status": validation_state,
        "selection_anchors": payload["selection_anchors"],
        "latest_price_date": payload["latest_price_date"],
        "benchmark_qualities": sorted(benchmark_qualities),
        "evaluation_role": args.evaluation_role,
        "logic_frozen_at": frozen_at.isoformat() if frozen_at else None,
        "edge_assessment": edge_assessment,
        "forward_test_path": os.path.abspath(args.out),
        "actionable_coverage_status": coverage_alarm["status"],
    }
    validation_path = os.path.join(REPORTS_DIR, "selector_validation_manifest_IN.json")
    with open(validation_path, "w") as handle:
        json.dump(validation_manifest, handle, indent=2)
    print(args.out)
    print(validation_path)
    print(f"actionable coverage: {coverage_alarm['status']} — {coverage_alarm['message']}")
    if args.fail_on_coverage_alarm and coverage_alarm["status"] == "ALARM_NO_ACTIONABLE_COVERAGE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
