"""
Intelligence Pipeline
~~~~~~~~~~~~~~~~~~~~~
Orchestrates the full intelligence layer:
  1. ThemeTracker   — extract themes + signals from document text
  2. CompanyClassifier — classify company roles per theme
  3. GraphStore     — persist Company→Theme→Quarter relationships
  4. ContradictionDetector — scan for narrative reversals
  5. MacroTriggerLayer — link macro events to themes
  6. LLMValidator   — format output for manual LLM review

Usage:
    from makrograph.intelligence.pipeline import IntelligencePipeline

    pipeline = IntelligencePipeline()

    # Process one earnings call document
    pipeline.process_document(
        text="...",
        company="Nvidia Corporation",
        quarter="Q2-2024",
        source_doc_id=42,      # optional: FK to your documents table
    )

    # After loading several documents, aggregate scores
    pipeline.aggregate(quarter="Q2-2024")

    # Scan for contradictions across all loaded data
    contradictions = pipeline.scan_contradictions()

    # Export LLM validation package
    paths = pipeline.export_for_llm(quarter="Q2-2024")
"""

import logging
from pathlib import Path
from typing import Optional

from .theme_tracker import ThemeTracker, ThemeSignal
from .company_classifier import CompanyClassifier, ClassificationResult
from .graph_store import GraphStore
from .contradiction_detector import ContradictionDetector, Contradiction
from .macro_trigger import MacroTriggerLayer, MacroEvent
from .llm_validator import LLMValidator

logger = logging.getLogger(__name__)


class IntelligencePipeline:
    """
    Single entry point for all intelligence processing.
    Instantiate once, then call process_document() for each earnings document.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.theme_tracker = ThemeTracker(config.get("theme_tracker", {}))
        self.classifier = CompanyClassifier(config.get("company_classifier", {}))
        self.graph = GraphStore(config.get("graph_store", {}))
        self.contradiction_detector = ContradictionDetector(config.get("contradiction_detector", {}))
        self.macro_layer = MacroTriggerLayer(config.get("macro_trigger", {}))
        self.llm_validator = LLMValidator(config.get("llm_validator", {}))

        self._processed_count = 0
        logger.info("IntelligencePipeline initialized.")

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def process_document(
        self,
        text: str,
        company: str,
        quarter: str,
        sector: str = "",
        source_doc_id: Optional[int] = None,
    ) -> dict:
        """
        Process a single document (earnings call, annual report, etc.).
        Extracts themes, classifies roles, persists to graph.

        Args:
            text:           Full normalized document text
            company:        Company name (e.g., "Nvidia Corporation")
            quarter:        Quarter string (e.g., "Q2-2024")
            sector:         Company sector (optional)
            source_doc_id:  FK to documents table (optional)

        Returns:
            Summary dict with themes and roles found.
        """
        if not text or not text.strip():
            logger.warning(f"Empty text for {company} {quarter}, skipping.")
            return {"company": company, "quarter": quarter, "themes": [], "roles": []}

        # Ensure company exists in graph
        self.graph.upsert_company(company, sector=sector)

        # 1. Extract theme signals
        signals: list[ThemeSignal] = self.theme_tracker.extract(text, company, quarter)

        if not signals:
            logger.debug(f"No themes detected: {company} {quarter}")
            return {"company": company, "quarter": quarter, "themes": [], "roles": []}

        # 2. Classify roles
        classifications: list[ClassificationResult] = self.classifier.classify_batch(
            company=company,
            quarter=quarter,
            text=text,
            theme_signals=signals,
        )
        # Build lookup: theme → classification
        cls_by_theme = {c.theme: c for c in classifications}

        # 3. Persist mentions to graph
        mention_ids = []
        for signal in signals:
            cls = cls_by_theme.get(signal.theme)
            roles = [r.value for r in cls.roles] if cls else []
            primary_role = cls.primary_role().value if (cls and cls.primary_role()) else ""

            mention_id = self.graph.record_mention(
                company=company,
                theme=signal.theme,
                quarter=quarter,
                mention_count=signal.mention_count,
                confidence=signal.confidence_score,
                strength_score=signal.strength_score,
                capex_mentioned=signal.capex_mentioned,
                capex_count=signal.capex_count,
                has_negative=signal.has_negative_signal,
                roles=roles,
                primary_role=primary_role,
                snippets=signal.snippets[:5],
                source_doc_id=source_doc_id,
            )
            mention_ids.append(mention_id)

        self._processed_count += 1

        summary = {
            "company": company,
            "quarter": quarter,
            "themes_detected": len(signals),
            "themes": [
                {
                    **s.to_dict(),
                    "roles": [r.value for r in cls_by_theme[s.theme].roles]
                    if s.theme in cls_by_theme else [],
                    "primary_role": cls_by_theme[s.theme].primary_role().value
                    if (s.theme in cls_by_theme and cls_by_theme[s.theme].primary_role()) else "",
                }
                for s in signals
            ],
        }

        logger.info(
            f"[{self._processed_count}] Processed {company} {quarter}: "
            f"{len(signals)} themes — "
            + ", ".join(f"{s.theme}({s.strength_score:.2f})" for s in signals[:5])
        )
        return summary

    def process_batch(
        self,
        documents: list[dict],
    ) -> list[dict]:
        """
        Process multiple documents.

        Each element in documents should be a dict with keys:
            text, company, quarter
            (optional) sector, source_doc_id
        """
        results = []
        for doc in documents:
            result = self.process_document(
                text=doc["text"],
                company=doc["company"],
                quarter=doc["quarter"],
                sector=doc.get("sector", ""),
                source_doc_id=doc.get("source_doc_id"),
            )
            results.append(result)

        total = len(documents)
        found = sum(1 for r in results if r["themes_detected"] > 0) if results and "themes_detected" in results[0] else 0
        logger.info(f"Batch complete: {found}/{total} documents had detectable themes.")
        return results

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def aggregate(self, quarter: str):
        """
        Compute cross-company theme strength scores for a quarter.
        Call this AFTER processing all documents for the quarter.
        """
        self.graph.aggregate_theme_strength(quarter)
        logger.info(f"Theme strength aggregated for {quarter}.")

    # ------------------------------------------------------------------
    # Contradiction scanning
    # ------------------------------------------------------------------

    def scan_contradictions(
        self,
        company: Optional[str] = None,
        theme: Optional[str] = None,
    ) -> list[Contradiction]:
        """
        Scan the entire graph for narrative contradictions.
        Optionally filter to a specific company or theme.
        """
        return self.contradiction_detector.detect_from_graph(
            graph_store=self.graph,
            company=company,
            theme=theme,
        )

    # ------------------------------------------------------------------
    # Macro events
    # ------------------------------------------------------------------

    def add_macro_event(self, event: MacroEvent) -> int:
        """Add a macro event to the layer."""
        return self.macro_layer.add_event(event)

    def get_macro_pressure(self, theme: str, quarter: str) -> dict:
        """Get net macro pressure on a theme for a quarter."""
        return self.macro_layer.get_macro_pressure(theme, quarter)

    # ------------------------------------------------------------------
    # LLM export
    # ------------------------------------------------------------------

    def export_for_llm(self, quarter: str, format: str = "text") -> list[Path]:
        """
        Build LLM validation package and export to files.
        Paste the text file contents directly into Claude or ChatGPT.

        Returns list of output file paths.
        """
        package = self.llm_validator.prepare_validation_package(
            graph_store=self.graph,
            macro_layer=self.macro_layer,
            quarter=quarter,
        )
        paths = self.llm_validator.export(package, format=format)
        logger.info(f"LLM validation exported: {[str(p) for p in paths]}")
        return paths

    def export_neo4j_cypher(self, quarter: str) -> Path:
        """Export Cypher import script for Neo4j migration."""
        return self.llm_validator.export_neo4j_cypher(self.graph, quarter)

    # ------------------------------------------------------------------
    # Queries (pass-through to GraphStore)
    # ------------------------------------------------------------------

    def get_top_themes(self, quarter: str, top_n: int = 10) -> list[dict]:
        return self.graph.get_top_themes(quarter, top_n)

    def get_theme_evolution(self, theme: str, last_n_quarters: int = 6) -> list[dict]:
        return self.graph.get_theme_evolution(theme, last_n_quarters)

    def get_companies_for_theme(self, theme: str, quarter: str) -> list[dict]:
        return self.graph.get_companies_for_theme(theme, quarter)

    def get_contradictions(self, company: str = None, theme: str = None) -> list[dict]:
        return self.graph.get_contradictions(company=company, theme=theme)

    def get_graph_snapshot(self, quarter: str) -> dict:
        return self.graph.get_graph_snapshot(quarter)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return pipeline processing statistics."""
        return {
            "documents_processed": self._processed_count,
            "graph_db": str(self.graph.conn),
        }

    def close(self):
        self.graph.close()
        self.macro_layer.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
