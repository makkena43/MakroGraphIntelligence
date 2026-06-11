"""India Context Entity Injector
================================
Bridges the gap between India's short-form NSE/BSE announcements and the
theme-detection engine.

PROBLEM
-------
India filings are short board-meeting outcomes and investor presentations
(50–300 words) rather than full 10-K/10-Q transcripts. spaCy's named-entity
recogniser only extracts organisational names (companies, exchanges) from
these snippets — it never extracts technology or sector entities like
"optical fiber", "power grid", or "solar energy".

The theme-detection engine (ThemeDetector.detect_from_clusters_agg) works by
joining mg_entities with mg_signals via the same document: if a document
generates a capex_increase signal AND has a "power grid" entity linked to it,
the cluster {"power grid" → capex_increase, 5 companies} forms a theme.
Without the entity, the signal floats unattached and the theme never surfaces.

SOLUTION
--------
After the signal extractor runs on an India document, feed each signal's
context_text through IndiaEntityInjector.extract().  It returns synthetic
entities (entity_type="TECHNOLOGY") for every India tech keyword found in
that context window.  These are inserted into mg_entities / mg_document_entities
exactly like real spaCy entities — the theme engine cannot tell the difference.

KEYWORD COVERAGE
----------------
20 India investment themes, each with:
  - 2-5 regex patterns covering spelling variants, abbreviations, and
    Hinglish mixing (e.g. "fibre" vs "fiber", "EV" vs "electric vehicle")
  - A canonical entity name (the key that appears in mg_entities.canonical_name)

All patterns are pre-compiled at module import — zero overhead per call.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical entity name → list of regex patterns
# ---------------------------------------------------------------------------
# Each canonical name matches a distinct investable theme in India's market.
# Patterns are intentionally broad to avoid missing mentions in telegraphic
# board-meeting language ("capacity addition for fiber" → optical fiber).

_INDIA_THEME_PATTERNS: list[tuple[str, list[str]]] = [
    ("optical fiber", [
        r"\b(?:optical\s+fi(?:b|br)e?r|dark\s+fi(?:b|br)e?r|fi(?:b|br)e?r\s+optic)",
        r"\b(?:fi(?:b|br)e?r(?:ization|isation)|fi(?:b|br)e?r\s+roll.?out|fi(?:b|br)e?r\s+network)",
        r"\b(?:submarine\s+cable|undersea\s+cable|OPGW|ADSS)\b",
        r"\b(?:fi(?:b|br)e?r\s+(?:backbone|backhaul|lay(?:ing)?|deployment|cable|infra))\b",
        r"\bBharat\s*Net\b",
    ]),
    ("power grid", [
        r"\b(?:power\s+grid|electricity\s+grid|transmission\s+(?:line|network|grid|tower))",
        r"\b(?:sub.?station|switchyard|HVDC|HVAC\s+line|extra\s+high\s+voltage|EHV)\b",
        r"\b(?:power\s+evacuation|grid\s+(?:upgrade|expansion|connect|infrastructure))",
        r"\b(?:T&D|transmission\s+and\s+distribution|power\s+infra(?:structure)?)",
        r"\b(?:smart\s+grid|grid\s+modernisation|grid\s+modernization)\b",
    ]),
    ("solar energy", [
        r"\b(?:solar\s+(?:energy|power|panel|module|cell|park|farm|plant|project|EPC))",
        r"\b(?:photovoltaic|PV\s+(?:module|panel|cell|system|plant)|solar\s+PV)\b",
        r"\b(?:solar\s+(?:manufactur|capacity|installation|deploy|procure|tender))",
        r"\b(?:rooftop\s+solar|utility.scale\s+solar|solar\s+EPC)\b",
    ]),
    ("wind energy", [
        r"\b(?:wind\s+(?:energy|power|turbine|farm|park|project|EPC|capacity))",
        r"\b(?:onshore\s+wind|offshore\s+wind|wind\s+mill|windmill)\b",
        r"\b(?:wind\s+(?:manufactur|installation|deploy|procurement|tender))\b",
    ]),
    ("green hydrogen", [
        r"\b(?:green\s+hydrogen|grey\s+hydrogen|blue\s+hydrogen|hydrogen\s+(?:fuel|energy|project))",
        r"\b(?:electrolyzer|electrolysis\s+(?:plant|capacity)|alkaline\s+electrolyzer)\b",
        r"\b(?:green\s+ammonia|hydrogen\s+storage|fuel\s+cell)\b",
    ]),
    ("electric vehicle", [
        r"\b(?:electric\s+vehicle|EV\s+(?:charging|battery|motor|two.?wheeler|segment|sales))",
        r"\b(?:two.?wheeler\s+(?:EV|electric)|three.?wheeler\s+(?:EV|electric)|four.?wheeler\s+(?:EV|electric))",
        r"\b(?:battery\s+management\s+system|BMS|EV\s+(?:infrastructure|fleet|adoption|penetration))",
        r"\b(?:electric\s+(?:scooter|bike|motorcycle|bus|truck|auto))\b",
        r"\bFAME\s*(?:II|2|scheme)?\b",
    ]),
    ("railway infrastructure", [
        r"\b(?:railway|rail(?:road)?\s+(?:infra|project|contract|order|electrification|station))",
        r"\b(?:Vande\s+Bharat|bullet\s+train|semi.?high.?speed|metro\s+rail|light\s+rail|MRTS)",
        r"\b(?:dedicated\s+freight\s+corridor|DFC|RRTS|rapid\s+rail\s+transit)",
        r"\b(?:track\s+(?:laying|renewal|doubling)|signalling\s+system|TCAS|Kavach)\b",
        r"\b(?:Indian\s+Railways\s+(?:order|contract|project|tender))\b",
    ]),
    ("defense electronics", [
        r"\b(?:defence|defense)\s+(?:electronics|equipment|manufactur|export|order|contract|program)",
        r"\b(?:atmanirbhar\s+(?:bharat|defense)|indigeni(?:s|z)ation|Make\s+in\s+India\s+defence)",
        r"\b(?:DRDO|HAL|BEL|BEML|Armoured|armored|missile|radar|sonar|electronic\s+warfare)",
        r"\b(?:military\s+(?:aircraft|helicopter|ship|submarine|vehicle|equipment|electronics))",
        r"\b(?:defence\s+(?:PSU|ministry|procurement|offset|IDDM|IDMM))\b",
    ]),
    ("semiconductor", [
        r"\b(?:semiconductor|chip\s+(?:design|manufactur|fab|packaging)|integrated\s+circuit)",
        r"\b(?:fab(?:rication)?\s+(?:plant|unit|facility)|wafer\s+(?:manufactur|fab|foundry))",
        r"\b(?:OSAT|assembly\s+and\s+test|chip\s+assembly|back.?end\s+(?:manufactur|process))",
        r"\b(?:India\s+Semiconductor\s+Mission|ISM|semiconductor\s+(?:PLI|subsidy|incentive))",
        r"\b(?:display\s+fab|ATMP|advanced\s+(?:packaging|manufactur))\b",
    ]),
    ("data center", [
        r"\b(?:data\s+cent(?:er|re)|datacent(?:er|re)|hyperscal(?:er|e)\s+(?:facility|campus))",
        r"\b(?:server\s+(?:farm|park|room)|cloud\s+(?:infrastructure|campus|park|data))",
        r"\b(?:co.?location|colocation|edge\s+(?:data\s+center|computing\s+node))",
        r"\b(?:data\s+center\s+(?:capacity|build|park|campus|zone|expansion|investment))\b",
    ]),
    ("5G telecom", [
        r"\b(?:5G\s+(?:rollout|network|deployment|infrastructure|spectrum|backhaul|core|site))",
        r"\b(?:telecom\s+(?:infra(?:structure)?|tower|network\s+expansion|capex))",
        r"\b(?:optical\s+network|IP.?MPLS|network\s+upgrade|radio\s+access\s+network|RAN)\b",
        r"\b(?:tower\s+company|passive\s+infra|active\s+sharing|small\s+cell)\b",
        r"\b(?:6G|network\s+densification|FWA|fixed\s+wireless\s+access)\b",
    ]),
    ("PLI scheme", [
        r"\bPLI\b.{0,40}(?:scheme|incentive|benefit|sanction|approval|approv|disburse)",
        r"\b(?:production.linked\s+incentive)\b",
        r"\bPLI\s+(?:for|in|under|scheme)\b.{0,40}(?:manufactur|sector|appli|approv)",
    ]),
    ("battery storage", [
        r"\b(?:battery\s+(?:storage|pack|cell|manufactur|energy|gigafactory|plant))",
        r"\b(?:energy\s+storage\s+(?:system|project|capacity)|BESS|grid\s+storage)",
        r"\b(?:lithium.?ion\s+(?:battery|cell|pack)|LFP\s+(?:battery|cell)|NMC\s+battery)",
        r"\b(?:pump(?:ed)?\s+(?:hydro|storage)|PHES|gravity\s+storage)\b",
    ]),
    ("electrical equipment", [
        r"\b(?:transformer\s+(?:manufactur|order|supply|plant)|power\s+transformer|distribution\s+transformer)",
        r"\b(?:switchgear|circuit\s+breaker|GIS\s+substation|busman\s+duct)",
        r"\b(?:copper\s+(?:cable|wire|rod|conductor|winding)|winding\s+wire)\b",
        r"\b(?:motor\s+(?:manufactur|winding|stator|rotor)|traction\s+motor)\b",
        r"\b(?:relay|protection\s+system|energy\s+meter|smart\s+meter)\b",
    ]),
    ("specialty chemicals", [
        r"\b(?:specialty\s+chem(?:ical)?|fine\s+chem(?:ical)?|fluorochem(?:ical)?)",
        r"\b(?:agrochemical|pesticide|herbicide|insecticide|fungicide|crop\s+protection)",
        r"\b(?:pigment|dye(?:stuff)?|specialty\s+coatings|surfactant|polymer\s+additive)",
        r"\b(?:pharma\s+intermediate|API\s+(?:manufactur|plant|facility)|CRAMS|CDMO)\b",
        r"\b(?:battery\s+chemical|electrolyte|cathode\s+material|anode\s+material)\b",
    ]),
    ("water infrastructure", [
        r"\b(?:water\s+(?:treatment|supply|project|pipeline|infra|infrastructure|distribution))",
        r"\b(?:Jal\s+Jeevan|AMRUT|sewage\s+treatment|STP|effluent\s+treatment|ETP)\b",
        r"\b(?:desalination|reverse\s+osmosis|RO\s+plant|water\s+recycling)\b",
    ]),
    ("road infrastructure", [
        r"\b(?:highway|expressway|road\s+(?:project|construction|development|widening))",
        r"\b(?:NHAI|BOT|HAM\s+(?:project|contract)|EPC\s+road)\b",
        r"\b(?:bridge|flyover|tunnel|NH\s+project|national\s+highway\s+project)\b",
    ]),
    ("hospital/healthcare infra", [
        r"\b(?:hospital\s+(?:project|expansion|greenfield|brownfield|capacity|bed))",
        r"\b(?:medical\s+(?:college|device|equipment|infra)|diagnostic\s+center)",
        r"\b(?:AIIMS|healthcare\s+(?:infra|facility|expansion|project))\b",
    ]),
    ("logistics/warehousing", [
        r"\b(?:warehousing|warehouse\s+(?:park|space|capacity|expansion|leasing))",
        r"\b(?:cold\s+(?:chain|storage|warehouse)|logistics\s+(?:park|hub|infra))",
        r"\b(?:multi.?modal\s+(?:hub|logistics)|inland\s+container\s+depot|ICD)\b",
    ]),
    ("real estate/affordable housing", [
        r"\b(?:affordable\s+housing|PMAY|Pradhan\s+Mantri\s+Awas)",
        r"\b(?:residential\s+(?:project|launch|unit|complex)|township\s+project)",
        r"\b(?:commercial\s+real\s+estate|office\s+space|IT\s+park|SEZ\s+project)\b",
    ]),
]

# Pre-compile all patterns for zero overhead at call time
_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (entity_name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for entity_name, patterns in _INDIA_THEME_PATTERNS
]

# Minimum context window (chars) around a signal match to scan for entity keywords.
# India signals often have very short context_text (50–100 chars from short announcements).
# Scanning the full context is fine — it's already capped at 500 chars in the DB.
_MIN_SCAN_CHARS = 20


@dataclass
class SyntheticEntity:
    """A technology entity synthesised from India signal context text."""
    entity_text: str          # human-readable match (lowercase canonical)
    canonical_name: str       # key for mg_entities.canonical_name
    entity_type: str = "TECHNOLOGY"
    confidence: float = 0.78  # slightly below spaCy ORG confidence (0.85) — intentional
    context_snippet: str = ""
    metadata: dict = field(default_factory=dict)


class IndiaEntityInjector:
    """Scans India signal context_text for investment-theme keywords and
    emits synthetic TECHNOLOGY entities for insertion into mg_entities.

    Usage (in _nlp_month after signal extraction)::

        injector = IndiaEntityInjector()
        signals = signal_extractor.extract(raw_text)
        synthetic = injector.extract_from_signals(signals)
        # Insert synthetic into pg_store exactly like regular entities
        pg_store.batch_upsert_entities_and_links(
            doc_id, [e.__dict__ for e in synthetic], filed_at
        )

    Why this matters
    ----------------
    The theme-detection query joins mg_entities ✕ mg_signals by document_id.
    If a document generates a capex_increase signal but no "optical fiber"
    entity, the cluster "optical fiber → capex_increase, 4 companies" never
    forms — the theme is invisible.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.min_confidence = config.get("min_entity_confidence", 0.70)
        self._patterns = _COMPILED

    def extract_from_signals(self, signals: list) -> list[SyntheticEntity]:
        """Scan each signal's context_text and full raw match for India entities.

        Args:
            signals: list of InvestmentSignal (or dicts with context_text key)

        Returns:
            Deduplicated list of SyntheticEntity — one per unique canonical_name
            found across all signal contexts.  Duplicates are collapsed so the
            same entity is not inserted twice for the same document.
        """
        seen: set[str] = set()
        entities: list[SyntheticEntity] = []

        for sig in signals:
            # Support both dataclass and dict
            ctx = (
                sig.context_text if hasattr(sig, "context_text")
                else sig.get("context_text", "")
            ) or ""

            if len(ctx) < _MIN_SCAN_CHARS:
                continue

            for canonical_name, patterns in self._patterns:
                if canonical_name in seen:
                    continue
                for pat in patterns:
                    m = pat.search(ctx)
                    if m:
                        seen.add(canonical_name)
                        entities.append(SyntheticEntity(
                            entity_text=canonical_name,
                            canonical_name=canonical_name,
                            entity_type="TECHNOLOGY",
                            confidence=0.78,
                            context_snippet=ctx[:200],
                            metadata={"injected_by": "IndiaEntityInjector",
                                      "matched_pattern": m.group(0)[:60]},
                        ))
                        break  # one match per canonical name is enough

        logger.debug(
            f"IndiaEntityInjector: {len(entities)} tech entities from "
            f"{len(signals)} signals — {[e.canonical_name for e in entities]}"
        )
        return entities

    def extract_from_text(self, text: str) -> list[SyntheticEntity]:
        """Scan raw document text directly (used when signal extraction hasn't run yet).

        Useful for short announcement titles that never pass the signal threshold
        but still mention a sector keyword.
        """
        seen: set[str] = set()
        entities: list[SyntheticEntity] = []

        if not text:
            return entities

        for canonical_name, patterns in self._patterns:
            for pat in patterns:
                m = pat.search(text)
                if m:
                    seen.add(canonical_name)
                    start = max(0, m.start() - 80)
                    end = min(len(text), m.end() + 80)
                    entities.append(SyntheticEntity(
                        entity_text=canonical_name,
                        canonical_name=canonical_name,
                        entity_type="TECHNOLOGY",
                        confidence=0.72,
                        context_snippet=text[start:end],
                        metadata={"injected_by": "IndiaEntityInjector.text",
                                  "matched_pattern": m.group(0)[:60]},
                    ))
                    break  # one match per canonical name

        return entities
