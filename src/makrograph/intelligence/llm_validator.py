"""
LLM Validator — Manual Validation Stage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Before automating anything, you feed the graph's structured output to Claude
or ChatGPT and ask:
  1. What hidden bottlenecks did the machine miss?
  2. Who are the second-order beneficiaries?
  3. Are there contradictions the system didn't catch?
  4. What emerging themes are not yet in the ontology?
  5. Which macro events are most important for the top themes?

This module formats all that data into clean, structured prompts and exports
them to text files you can paste directly into Claude or ChatGPT.

It also supports JSON export so you can programmatically POST to the API
when you're ready to automate.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates — edit these to tune what you ask the LLM
# ---------------------------------------------------------------------------

THEME_ANALYSIS_PROMPT = """
You are an expert investment analyst specializing in identifying multi-year structural themes
in Indian and global equity markets. Below is structured data extracted from earnings calls
and annual reports by MakroGraph Intelligence.

## Quarter: {quarter}

## Top Themes by Strength Score
{themes_table}

## Companies and Their Roles per Theme
{company_roles_table}

## Macro Events Linked to These Themes
{macro_events_table}

## Your Tasks:
1. **Hidden Bottlenecks**: Which companies in the list control a critical chokepoint that
   the scoring system may have underweighted? Why?
2. **Second-Order Beneficiaries**: Which companies benefit indirectly that are NOT in the
   above list? Name specific companies with reasoning.
3. **Emerging Themes**: Based on the language patterns above, what new investment theme
   might be forming that is NOT yet in the system's ontology?
4. **Theme Acceleration Check**: Which themes show the clearest multi-quarter acceleration
   and why? Which look like one-quarter noise?
5. **Risk Flags**: Where do you see risk of theme reversal or disappointment?

Provide specific, actionable output. Cite the data above where possible.
""".strip()

CONTRADICTION_ANALYSIS_PROMPT = """
You are reviewing narrative contradictions detected in earnings call transcripts.
The system has found cases where management language changed significantly between quarters.

## Detected Contradictions (Quarter: {quarter})
{contradictions_table}

## Your Tasks:
1. **Severity Assessment**: For each contradiction, rate severity: Critical / Moderate / Minor.
   Critical = likely to impact stock price materially. Explain your reasoning.
2. **Pattern Detection**: Do you see a sector-wide pattern (e.g., all memory companies
   shifted negative in the same quarter)? What does that signal?
3. **Hidden Contradictions**: Based on the snippets shown, are there subtler narrative
   changes the system missed? (e.g., management stopped using certain positive phrases
   without replacing them with negative ones.)
4. **Investment Implications**: For the most significant contradictions, what is the
   actionable investment implication?
""".strip()

BOTTLENECK_DISCOVERY_PROMPT = """
You are analyzing a supply chain graph for investment themes.
Focus on identifying hidden bottlenecks and underappreciated enablers.

## Theme: {theme}
## Quarter: {quarter}

## Known Companies and Their Roles
{company_table}

## Key Excerpts (Management Language)
{snippets}

## Your Tasks:
1. **Bottleneck Map**: Draw the supply chain from raw material to end user.
   At each step, name the key Indian/global companies.
2. **Hardest to Replace**: Which single company or capability is hardest to substitute?
   Why? What would happen to the theme if it disappeared?
3. **Hidden Enablers**: Name 2-3 companies NOT in the list above that enable this theme
   but rarely get discussed in its context. Why are they important?
4. **Valuation Implication**: If this theme reaches full scale in 3-5 years, which
   company in the chain captures most of the value? Why?
""".strip()


@dataclass
class ValidationPackage:
    """Complete output package for LLM manual review."""
    quarter: str
    theme_prompt: str
    contradiction_prompt: str
    bottleneck_prompts: dict[str, str]   # theme → prompt
    raw_data: dict
    generated_at: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.utcnow().isoformat()


class LLMValidator:
    """
    Formats graph store data into LLM-ready prompts and exports them.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.output_dir = Path(config.get("llm_output_dir", "data/llm_validation"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_snippets_per_theme = config.get("max_snippets_per_theme", 3)
        self.top_n_themes = config.get("top_n_themes", 8)
        self.top_n_companies = config.get("top_n_companies_per_theme", 5)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def prepare_validation_package(
        self,
        graph_store,
        macro_layer,
        quarter: str,
        top_themes: list[dict] = None,
    ) -> ValidationPackage:
        """
        Pull all relevant data from graph_store and macro_layer,
        format into prompts, and return a ValidationPackage.

        Args:
            graph_store:    GraphStore instance
            macro_layer:    MacroTriggerLayer instance
            quarter:        Quarter to analyze (e.g. "Q2-2024")
            top_themes:     Pre-computed theme scores (optional — fetched if None)
        """
        # Gather data
        if top_themes is None:
            top_themes = graph_store.get_top_themes(quarter, top_n=self.top_n_themes)

        company_roles: dict[str, list[dict]] = {}
        macro_events: dict[str, list[dict]] = {}
        all_snippets: dict[str, list[str]] = {}

        for theme_row in top_themes:
            theme = theme_row["theme"]
            companies = graph_store.get_companies_for_theme(theme, quarter)
            company_roles[theme] = companies[:self.top_n_companies]
            macro_events[theme] = macro_layer.get_events_for_theme(theme)[:5]
            # Collect snippets
            snippets = []
            for c in companies[:3]:
                snippets.extend(json.loads(c.get("snippets", "[]")) if isinstance(c.get("snippets"), str) else [])
            all_snippets[theme] = snippets[:self.max_snippets_per_theme]

        contradictions = graph_store.get_contradictions(limit=20)

        # Build raw data bundle (also usable for API calls)
        raw_data = {
            "quarter": quarter,
            "top_themes": top_themes,
            "company_roles": company_roles,
            "macro_events": macro_events,
            "contradictions": contradictions,
        }

        # Format prompts
        theme_prompt = self._format_theme_prompt(quarter, top_themes, company_roles, macro_events)
        contradiction_prompt = self._format_contradiction_prompt(quarter, contradictions)
        bottleneck_prompts = {
            t["theme"]: self._format_bottleneck_prompt(
                t["theme"], quarter, company_roles.get(t["theme"], []),
                all_snippets.get(t["theme"], []),
            )
            for t in top_themes[:4]  # only top 4 themes get full bottleneck analysis
        }

        package = ValidationPackage(
            quarter=quarter,
            theme_prompt=theme_prompt,
            contradiction_prompt=contradiction_prompt,
            bottleneck_prompts=bottleneck_prompts,
            raw_data=raw_data,
        )

        logger.info(f"LLM validation package prepared for {quarter}.")
        return package

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export(self, package: ValidationPackage, format: str = "text") -> list[Path]:
        """
        Export validation package to files.
        format: "text" (human-readable prompts) | "json" (structured data)
        Returns list of created file paths.
        """
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        q_slug = package.quarter.replace("-", "_").upper()
        created = []

        if format in ("text", "both"):
            # Master prompt file
            master_path = self.output_dir / f"llm_prompts_{q_slug}_{ts}.txt"
            with open(master_path, "w", encoding="utf-8") as f:
                f.write(f"MakroGraph Intelligence — LLM Validation Package\n")
                f.write(f"Quarter: {package.quarter}  |  Generated: {package.generated_at}\n")
                f.write("=" * 80 + "\n\n")

                f.write("PROMPT 1: THEME & COMPANY ANALYSIS\n")
                f.write("-" * 40 + "\n")
                f.write(package.theme_prompt)
                f.write("\n\n" + "=" * 80 + "\n\n")

                f.write("PROMPT 2: CONTRADICTION ANALYSIS\n")
                f.write("-" * 40 + "\n")
                f.write(package.contradiction_prompt)
                f.write("\n\n" + "=" * 80 + "\n\n")

                for theme, prompt in package.bottleneck_prompts.items():
                    f.write(f"PROMPT 3: BOTTLENECK ANALYSIS — {theme}\n")
                    f.write("-" * 40 + "\n")
                    f.write(prompt)
                    f.write("\n\n" + "=" * 80 + "\n\n")

            created.append(master_path)
            logger.info(f"Text prompts exported: {master_path}")

        if format in ("json", "both"):
            json_path = self.output_dir / f"llm_data_{q_slug}_{ts}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(package.raw_data, f, indent=2, default=str)
            created.append(json_path)
            logger.info(f"JSON data exported: {json_path}")

        return created

    def export_neo4j_cypher(self, graph_store, quarter: str) -> Path:
        """
        Export graph data as Cypher statements for import into Neo4j.
        Useful when you're ready to migrate from SQLite to Neo4j.
        """
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        q_slug = quarter.replace("-", "_").upper()
        out_path = self.output_dir / f"neo4j_import_{q_slug}_{ts}.cypher"

        snapshot = graph_store.get_graph_snapshot(quarter)

        lines = [
            f"// MakroGraph Intelligence — Neo4j Import — {quarter}",
            f"// Generated: {datetime.utcnow().isoformat()}",
            "",
            "// ── THEMES ──",
        ]

        for t in snapshot["themes"]:
            lines.append(
                f"MERGE (t:Theme {{name: '{t['theme']}'}}) "
                f"SET t.composite_score = {t['composite_score']}, "
                f"    t.company_count = {t['company_count']}, "
                f"    t.growth_vs_prev = {t['growth_vs_prev']};"
            )

        lines += ["", "// ── COMPANIES & MENTIONS ──"]
        for c in snapshot["companies"]:
            company_safe = c["company"].replace("'", "\\'")
            lines.append(f"MERGE (c:Company {{name: '{company_safe}'}});")
            roles_str = ", ".join(c["roles"]) if c.get("roles") else ""
            lines.append(
                f"MATCH (c:Company {{name: '{company_safe}'}}), "
                f"      (t:Theme {{name: '{c['theme']}'}}) "
                f"MERGE (c)-[r:MENTIONS {{quarter: '{quarter}'}}]->(t) "
                f"SET r.primary_role = '{c.get('primary_role', '')}', "
                f"    r.roles = '{roles_str}', "
                f"    r.strength_score = {c['strength_score']};"
            )

        lines += ["", "// ── CONTRADICTIONS ──"]
        for con in snapshot["contradictions"]:
            comp_safe = con["company"].replace("'", "\\'")
            lines.append(
                f"MATCH (c:Company {{name: '{comp_safe}'}}), "
                f"      (t:Theme {{name: '{con['theme']}'}}) "
                f"CREATE (c)-[:CONTRADICTS {{from_quarter: '{con['from_quarter']}', "
                f"    to_quarter: '{con['to_quarter']}', "
                f"    change_type: '{con['change_type']}'}}]->(t);"
            )

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"Neo4j Cypher export: {out_path}")
        return out_path

    # ------------------------------------------------------------------
    # Internal formatting
    # ------------------------------------------------------------------

    def _format_theme_prompt(
        self,
        quarter: str,
        themes: list[dict],
        company_roles: dict,
        macro_events: dict,
    ) -> str:
        # Theme table
        theme_lines = ["| Theme | Score | Companies | Mentions | Growth | Streak |"]
        theme_lines.append("|-------|-------|-----------|----------|--------|--------|")
        for t in themes:
            theme_lines.append(
                f"| {t['theme']} | {t['composite_score']:.2f} | {t['company_count']} "
                f"| {t['total_mentions']} | {t.get('growth_vs_prev', 0):+.0%} "
                f"| {t.get('streak_quarters', 0)}Q |"
            )

        # Company roles table
        role_lines = []
        for theme, companies in list(company_roles.items())[:6]:
            role_lines.append(f"\n**{theme}**")
            for c in companies:
                roles = c.get("roles", [])
                if isinstance(roles, str):
                    roles = json.loads(roles)
                role_lines.append(
                    f"  • {c['company']} | Roles: {', '.join(roles) or 'beneficiary'} "
                    f"| Strength: {c['strength_score']:.2f}"
                )

        # Macro events
        macro_lines = []
        for theme, events in list(macro_events.items())[:4]:
            if events:
                macro_lines.append(f"\n**{theme}**")
                for e in events:
                    arrow = "↑" if e["impact_direction"] == "positive" else "↓"
                    macro_lines.append(
                        f"  {arrow} [{e['impact_magnitude'].upper()}] {e['title']} "
                        f"({e.get('event_date', '')})"
                    )

        return THEME_ANALYSIS_PROMPT.format(
            quarter=quarter,
            themes_table="\n".join(theme_lines),
            company_roles_table="\n".join(role_lines) or "(No company data)",
            macro_events_table="\n".join(macro_lines) or "(No macro events linked)",
        )

    def _format_contradiction_prompt(self, quarter: str, contradictions: list[dict]) -> str:
        lines = ["| Company | Theme | From Q | To Q | Type | Δ Sentiment |"]
        lines.append("|---------|-------|--------|------|------|-------------|")
        for c in contradictions[:15]:
            delta = c.get("to_sentiment", 0) - c.get("from_sentiment", 0)
            lines.append(
                f"| {c['company']} | {c['theme']} | {c['from_quarter']} | {c['to_quarter']} "
                f"| {c['change_type']} | {delta:+.2f} |"
            )
        if not contradictions:
            lines = ["No contradictions detected yet. Run ContradictionDetector.detect_from_graph() first."]

        return CONTRADICTION_ANALYSIS_PROMPT.format(
            quarter=quarter,
            contradictions_table="\n".join(lines),
        )

    def _format_bottleneck_prompt(
        self,
        theme: str,
        quarter: str,
        companies: list[dict],
        snippets: list[str],
    ) -> str:
        company_lines = ["| Company | Primary Role | Strength | Capex |"]
        company_lines.append("|---------|-------------|----------|-------|")
        for c in companies:
            capex = "✓" if c.get("capex_mentioned") else "–"
            company_lines.append(
                f"| {c['company']} | {c.get('primary_role', '?')} "
                f"| {c['strength_score']:.2f} | {capex} |"
            )

        snippet_text = "\n\n".join(f'> "{s}"' for s in snippets[:3]) if snippets else "(No snippets available)"

        return BOTTLENECK_DISCOVERY_PROMPT.format(
            theme=theme,
            quarter=quarter,
            company_table="\n".join(company_lines),
            snippets=snippet_text,
        )
