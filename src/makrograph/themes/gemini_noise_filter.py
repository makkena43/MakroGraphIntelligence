"""Claude-powered noise filter for investment themes.

Automatically removes regulatory boilerplate, accounting terms, and
generic temporal expressions that slip through deterministic filters.

Design principles:
  - FILTER ONLY: never changes theme content, scores, or rankings
  - GRACEFUL FALLBACK: if Claude unavailable, returns themes unchanged
  - NO AI BIAS: Claude only classifies existing theme names as noise/signal
  - SMART CHUNKING: batches 40 themes per call to avoid token limits
  - TRANSPARENT: logs every removal with reason

Invoked automatically in run_themes() when anthropic.api_key is configured.
User does NOT need to trigger this manually.
"""

import json
import logging
import urllib.request
import urllib.error
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..themes.theme_detector import InvestmentTheme

logger = logging.getLogger(__name__)

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 40  # themes per Claude call — stays well within token limits

_FILTER_PROMPT = """You are a financial data quality filter for an India equity research system.

Your ONLY job: classify each auto-detected theme name as KEEP or REMOVE.

KEEP if: real investable macro theme (commodity shortage, sector demand surge,
supply chain constraint, capex cycle, M&A wave, regulatory tailwind/headwind
affecting a specific industry)

REMOVE if ANY of these apply:
- Regulatory/legal boilerplate (SEBI, NCLT, listing regulations, companies act,
  disclosure requirements, regulation 30/33, audit committee, statutory auditors,
  institute of chartered accountants, standards on auditing, basis for opinion)
- Temporal expressions (this quarter, last year, next year, full financial year,
  current financial year, year ended, quarter ended, the year, the quarter)
- Generic/non-specific terms (group, standalone, consolidated, indian, india,
  inter alia, equity share capital, plant and equipment, g block)
- City/state names used as themes (mumbai, delhi, maharashtra, gujarat, dalal street)
- Accounting concepts (indian accounting standards, audited financial results,
  the companies act, goodwill, deferred tax)
- COVID-19 (too broad and not sector-specific)

KEEP examples: Steel Critical Shortage, Power: Constraint from Real Estate Demand,
Automotive Critical Shortage, pharma: Demand-Supply Tension, Materials Critical Shortage,
Semiconductor Critical Shortage, Cement: Demand Surge, Defense: Capex Surge

REMOVE examples: Year Critical Shortage, SEBI: Constraint from Steel Demand,
basis for opinion: Demand-Supply Tension, the audit committee: Demand-Supply Tension,
inter alia: Demand-Supply Tension, mumbai: Demand-Supply Tension

Themes to classify:
{themes}

Respond ONLY with valid JSON array:
[{{"theme": "theme name", "action": "KEEP" or "REMOVE"}}]
"""


import re as _re

# ---------------------------------------------------------------------------
# Change 9: Deterministic noise filter (runs BEFORE Claude to reduce API calls)
# ---------------------------------------------------------------------------

_DETERMINISTIC_REMOVE_PATTERNS: list[_re.Pattern] = [
    # Regulatory / legal boilerplate
    _re.compile(r"\b(?:SEBI|NCLT|NCLAT|Companies\s+Act|Listing\s+Obligat|"
                r"Regulation\s+\d+|Insider\s+Trading|Statutory\s+Auditor|"
                r"Chartered\s+Accountant|Standards?\s+on\s+Auditing|"
                r"basis\s+for\s+opinion|audit\s+committee|"
                r"Ind\s*AS|IFRS|Going\s+Concern)\b", _re.I),
    # Pure temporal expressions
    _re.compile(r"^\s*(?:this|last|next|current|full|financial|fiscal)\s+"
                r"(?:quarter|year|half|period|month)\b", _re.I),
    _re.compile(r"\b(?:quarter\s+ended|year\s+ended|the\s+year|the\s+quarter|"
                r"YoY|QoQ|year.on.year|quarter.on.quarter)\b", _re.I),
    # Generic / non-specific single-word or city themes
    _re.compile(r"^(?:India|Indian|Group|Standalone|Consolidated|Inter\s+alia|"
                r"Mumbai|Delhi|Bangalore|Chennai|Hyderabad|Gujarat|Maharashtra|"
                r"Dalal\s+Street|G\s+Block|COVID|Covid.19|Pandemic)\s*", _re.I),
    # Accounting concepts
    _re.compile(r"\b(?:Goodwill|Deferred\s+Tax|Plant\s+and\s+Equipment|"
                r"Right.of.Use|Equity\s+Share\s+Capital|Par\s+Value|"
                r"Other\s+Comprehensive\s+Income|EBITDA\s+Margin\b)\b", _re.I),
]

_DETERMINISTIC_KEEP_PATTERNS: list[_re.Pattern] = [
    # Always keep themes containing strong investable keywords
    _re.compile(r"\b(?:shortage|bottleneck|constraint|capex|supply\s+gap|"
                r"localization|PLI|import\s+substitut|capacity\s+gap|"
                r"tender|order\s+book|export|domestic\s+manufactur)\b", _re.I),
]


def _deterministic_filter(themes: list) -> tuple[list, list]:
    """Pre-filter themes deterministically before calling Claude (Change 9).

    Returns:
        (kept_themes, rejected_themes) — kept go to Claude for confirmation,
        rejected are definitively removed without any LLM call.
    """
    kept, rejected = [], []
    for t in themes:
        name = t.theme_name or ""
        # Hard keep: always-keep patterns override everything
        if any(p.search(name) for p in _DETERMINISTIC_KEEP_PATTERNS):
            kept.append(t)
            continue
        # Hard remove: definitively noise — no need to call Claude
        if any(p.search(name) for p in _DETERMINISTIC_REMOVE_PATTERNS):
            logger.debug(f"  REMOVE (deterministic): {name}")
            rejected.append(t)
            continue
        # Ambiguous: pass to Claude for classification
        kept.append(t)
    return kept, rejected


def filter_themes_with_gemini(
    themes: list,
    api_key: str,
    country: str = "IN",
) -> list:
    """Filter noisy themes — deterministic pre-filter then Claude for ambiguous cases.

    Change 9: Deterministic patterns handle obvious noise (regulatory boilerplate,
    temporal expressions, accounting terms) without any LLM call. Claude is only
    invoked for ambiguous cases, reducing API usage and keeping ranking deterministic.

    Args:
        themes: list of InvestmentTheme objects
        api_key: Anthropic API key (from config/secrets.json)
        country: only filter themes for this country (others pass through unchanged)

    Returns:
        Filtered list — noise themes removed, signal themes kept unchanged.
        If API unavailable or fails, returns original list unmodified.
    """
    if not themes:
        return themes

    # Only filter themes for the specified country
    to_filter = [t for t in themes if getattr(t, "country", "US") == country]
    pass_through = [t for t in themes if getattr(t, "country", "US") != country]

    if not to_filter:
        return themes

    # ── Change 9: Deterministic pre-filter ───────────────────────────────────
    pre_kept, pre_rejected = _deterministic_filter(to_filter)
    if pre_rejected:
        logger.info(
            f"Deterministic filter removed {len(pre_rejected)} noisy themes "
            f"without calling Claude"
        )

    # If no API key or nothing left for Claude, return immediately
    if not api_key or not pre_kept:
        return pass_through + pre_kept

    # Only call Claude for ambiguous themes not handled deterministically
    to_filter = pre_kept
    logger.info(f"Claude noise filter: checking {len(to_filter)} {country} themes (after deterministic pre-filter)...")

    keep_names: set[str] = set()
    remove_names: set[str] = set()

    import time as _time
    # Process in batches of BATCH_SIZE — with delay to avoid 429 rate limits
    for batch_start in range(0, len(to_filter), BATCH_SIZE):
        if batch_start > 0:
            _time.sleep(2)  # 2s between batches — stays within free tier limits
        batch = to_filter[batch_start: batch_start + BATCH_SIZE]
        theme_list = "\n".join(
            f"{i+1}. {t.theme_name}" for i, t in enumerate(batch)
        )
        prompt = _FILTER_PROMPT.format(themes=theme_list)

        try:
            payload = json.dumps({
                "model": CLAUDE_MODEL,
                "max_tokens": 2000,
                "temperature": 0.05,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()

            req = urllib.request.Request(
                CLAUDE_URL,
                data=payload,
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                text = result["content"][0]["text"]

            # Extract JSON array — handle markdown code blocks and plain JSON
            import re
            # Strip markdown code fences if present
            text_clean = re.sub(r"```(?:json)?\s*", "", text).strip()
            # Find the JSON array (greedy to capture nested objects)
            match = re.search(r"\[.*\]", text_clean, re.DOTALL)
            if not match:
                logger.warning("Claude filter: no JSON in response — keeping batch unchanged")
                keep_names.update(t.theme_name for t in batch)
                continue

            try:
                decisions = json.loads(match.group())
            except json.JSONDecodeError as je:
                logger.warning(f"Claude filter: JSON parse error ({je}) — keeping batch unchanged")
                keep_names.update(t.theme_name for t in batch)
                continue
            for d in decisions:
                name = d.get("theme", "")
                action = d.get("action", "KEEP").upper()
                if action == "REMOVE":
                    remove_names.add(name)
                    logger.debug(f"  REMOVE: {name}")
                else:
                    keep_names.add(name)

        except urllib.error.URLError as e:
            logger.warning(f"Claude filter: API unreachable ({e}) — keeping batch unchanged")
            keep_names.update(t.theme_name for t in batch)
        except Exception as e:
            logger.warning(f"Claude filter: error ({e}) — keeping batch unchanged")
            keep_names.update(t.theme_name for t in batch)

    # Apply decisions
    filtered = []
    for t in to_filter:
        if t.theme_name in remove_names:
            logger.info(f"  [Claude] Removed noise theme: {t.theme_name}")
        else:
            filtered.append(t)

    removed_count = len(to_filter) - len(filtered)
    if removed_count:
        logger.info(
            f"Claude noise filter: removed {removed_count} noisy themes "
            f"({len(filtered)} kept from {len(to_filter)} {country} themes)"
        )

    return pass_through + filtered
