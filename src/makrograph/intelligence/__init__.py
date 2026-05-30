"""
MakroGraph Intelligence Layer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Modules for detecting themes, scoring their strength, classifying company roles,
tracking narrative evolution over time, detecting contradictions, and linking macro triggers.

Usage:
    from makrograph.intelligence.pipeline import IntelligencePipeline
    pipeline = IntelligencePipeline(config)
    pipeline.process_document(text, company="Nvidia", quarter="Q2-2024")
"""

from .theme_tracker import ThemeTracker, ThemeSignal
from .company_classifier import CompanyClassifier, CompanyRole
from .graph_store import GraphStore
from .contradiction_detector import ContradictionDetector, Contradiction
from .macro_trigger import MacroTriggerLayer, MacroEvent
from .llm_validator import LLMValidator
from .pipeline import IntelligencePipeline

__all__ = [
    "ThemeTracker",
    "ThemeSignal",
    "CompanyClassifier",
    "CompanyRole",
    "GraphStore",
    "ContradictionDetector",
    "Contradiction",
    "MacroTriggerLayer",
    "MacroEvent",
    "LLMValidator",
    "IntelligencePipeline",
]
