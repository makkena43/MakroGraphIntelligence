"""Named entity extraction using spaCy with FinBERT-based financial NER.

Entity types extracted:
    COMPANY       - Public companies, subsidiaries, competitors
    TECHNOLOGY    - AI, semiconductors, EV, biotech, cloud, etc.
    SECTOR        - Industry sector names
    PERSON        - Executives, analysts, board members
    PRODUCT       - Product names, platforms, services
    CONCEPT       - Macro trends, bottlenecks, demand drivers
    REGULATION    - Regulatory bodies, laws, standards
    LOCATION      - Countries, regions (for supply chain)
    AMOUNT        - Monetary values (capex, revenue, investments)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

TECHNOLOGY_KEYWORDS = {
    "AI", "Artificial Intelligence", "Machine Learning", "Deep Learning",
    "Semiconductor", "Chip", "GPU", "CPU", "ASIC", "Wafer", "Foundry",
    "Electric Vehicle", "EV", "Battery", "Lithium", "Solar", "Wind",
    "Cloud", "Edge Computing", "5G", "6G", "Quantum Computing",
    "Biotech", "mRNA", "Gene Therapy", "CRISPR", "Drug Discovery",
    "Robotics", "Automation", "IoT", "Cybersecurity", "Blockchain",
    "Generative AI", "LLM", "Large Language Model", "Transformer",
    "Data Center", "HBM", "High Bandwidth Memory", "CoWoS",
}

SECTOR_KEYWORDS = {
    "Technology", "Healthcare", "Industrials", "Energy", "Financials",
    "Consumer Discretionary", "Consumer Staples", "Materials",
    "Utilities", "Real Estate", "Communication Services",
    "Semiconductor", "Pharma", "Defense", "Aerospace", "Automotive",
    # Emerging / non-US sectors (valuable globally, not India-only)
    "NBFC", "Infrastructure", "Specialty Chemicals", "Agrochemicals",
    "Textiles", "Power", "Steel", "Cement", "Logistics", "EPC",
}

CONCEPT_KEYWORDS = {
    "supply chain", "bottleneck", "shortage", "overcapacity", "capex",
    "capital expenditure", "demand surge", "headwind", "tailwind",
    "inflation", "deflation", "interest rate", "monetary policy",
    "geopolitical", "sanctions", "tariff", "regulatory", "ESG",
    "decarbonization", "electrification", "digitalization",
    # Policy / trade concepts (relevant across markets)
    "PLI scheme", "Make in India", "Atmanirbhar", "China+1",
    "import substitution", "export opportunity",
    # Regulatory bodies — extracted as CONCEPT so they surface in themes
    # (applies regardless of market — FDA surfaces for US the same way SEBI surfaces for IN)
    "FDA", "EPA", "FTC", "CFPB",                          # US
    "SEBI", "RBI", "Reserve Bank of India", "NCLT",       # India financial
    "CCI", "DPIIT", "NHAI", "TRAI", "IRDAI",             # India sectoral
}

# Words that spaCy frequently misclassifies as ORG in SEC filings.
# These are legal/structural/boilerplate terms that produce junk COMPANY
# entities and downstream noise themes.
_SPACY_ORG_NOISE: frozenset = frozenset({
    # SEC structural terms
    "commission", "registrant", "issuer", "filer", "exhibit",
    "section", "item", "schedule", "form", "part", "appendix",
    # Generic legal / corporate terms
    "agreement", "committee", "board", "management", "officers",
    "counsel", "subsidiary", "corporation", "company", "entity",
    "partnership", "association", "foundation", "trust",
    "government", "authority", "agency", "bureau", "department",
    # Finance / accounting boilerplate
    "revenue", "earnings", "dividend", "interest", "principal",
    "proceeds", "consideration", "premium", "discount", "tranche",
    # Time / reporting period terms
    "quarter", "year", "period", "month", "fiscal",
})


@dataclass
class ExtractedEntity:
    """A single entity extracted from text."""
    entity_text: str
    entity_type: str
    canonical_name: str = ""
    confidence: float = 1.0
    start_char: int = 0
    end_char: int = 0
    context: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.canonical_name:
            self.canonical_name = self.entity_text.strip()


@dataclass
class ExtractionResult:
    """Result of NLP entity extraction on a document."""
    document_id: Optional[int]
    entities: list[ExtractedEntity] = field(default_factory=list)
    sentences: int = 0
    word_count: int = 0
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None

    def by_type(self, entity_type: str) -> list[ExtractedEntity]:
        return [e for e in self.entities if e.entity_type == entity_type]


class EntityExtractor:
    """Multi-strategy entity extractor: spaCy + keyword rules + FinBERT.

    Performance notes:
        - spaCy NER is capped at max_spacy_chars (default 40k). SEC 10-K
          filings are often 500k+ chars. spaCy's en_core_web_sm processes
          roughly 10k words/sec, so uncapped full-doc processing takes 30-60s
          per document. Capping to the first 40k chars keeps it under 2s.
        - The keyword/rule extractor (TECHNOLOGY_KEYWORDS etc.) is O(n_keywords)
          and uses fast regex, so it runs on the full text.
    """

    def __init__(self, config: dict):
        self.use_finbert = config.get("use_finbert", False)
        self.use_spacy = config.get("use_spacy", True)
        self.spacy_model = config.get("spacy_model", "en_core_web_sm")
        self.min_entity_len = config.get("min_entity_len", 2)
        self.min_confidence = config.get("min_confidence", 0.65)
        self.max_entities_per_doc = config.get("max_entities_per_doc", 500)
        # spaCy is only applied to the first N chars; rule-based runs on full text
        self.max_spacy_chars = config.get("max_spacy_chars", 40_000)
        # None = not yet tried; False = tried and unavailable; <model> = loaded
        self._nlp = None
        self._spacy_unavailable = False   # sentinel to suppress repeat warnings
        self._finbert = None
        self._finbert_unavailable = False

    def _load_spacy(self):
        if self._nlp is not None or self._spacy_unavailable:
            return
        try:
            import spacy
            # Only exclude lemmatizer — it runs after NER and has no upstream effect on NER quality.
            # tok2vec in en_core_web_sm shares features with tagger/parser so those must stay.
            # Speed comes from nlp.pipe() batch processing, not from disabling components.
            self._nlp = spacy.load(self.spacy_model, exclude=["lemmatizer"])
            # Increase max_length for long PDF documents (default 1M can be exceeded)
            self._nlp.max_length = 2_000_000
            logger.info(f"spaCy model loaded: {self.spacy_model} (lemmatizer excluded, full NER quality)")
        except OSError:
            logger.warning(f"spaCy model '{self.spacy_model}' not found. Run: python -m spacy download {self.spacy_model}")
            self._spacy_unavailable = True
        except ImportError:
            logger.warning("spaCy not installed. Falling back to rule-based extraction.")
            self._spacy_unavailable = True

    def _load_finbert(self):
        if self._finbert is not None or self._finbert_unavailable:
            return
        try:
            from transformers import pipeline
            self._finbert = pipeline(
                "ner",
                model="ProsusAI/finbert",
                aggregation_strategy="simple",
                device=-1,
            )
            logger.info("FinBERT NER pipeline loaded")
        except ImportError:
            logger.warning("transformers not installed. FinBERT NER disabled.")
            self._finbert_unavailable = True
        except Exception as e:
            logger.warning(f"FinBERT load failed: {e}")
            self._finbert_unavailable = True

    def extract(self, text: str, document_id: int = None) -> ExtractionResult:
        """Extract entities from text using all available strategies."""
        result = ExtractionResult(document_id=document_id)
        if not text or not text.strip():
            return result

        result.word_count = len(text.split())
        all_entities: list[ExtractedEntity] = []

        # Strategy 1: Keyword / pattern rules — run on FULL text.
        # This is fast (O(keywords × text_len) with simple regex) and catches
        # the investment-relevant TECHNOLOGY/SECTOR/CONCEPT keywords we care about.
        rule_entities = self._extract_with_rules(text)
        all_entities.extend(rule_entities)

        # Strategy 2: spaCy NER — capped at max_spacy_chars.
        # spaCy en_core_web_sm processes ~10k words/sec. A full 10-K (300k chars)
        # would take 30s. We cap at 40k chars (~6k words, <4s) which covers
        # the executive summary, MD&A, and key risk factors.
        if self.use_spacy:
            self._load_spacy()
            if self._nlp:
                spacy_text = text[:self.max_spacy_chars]
                spacy_entities = self._extract_with_spacy(spacy_text)
                all_entities.extend(spacy_entities)

        # Strategy 3: FinBERT NER (financial-domain NER)
        if self.use_finbert:
            self._load_finbert()
            if self._finbert:
                finbert_entities = self._extract_with_finbert(text[:4096])  # model max
                all_entities.extend(finbert_entities)

        # Deduplicate and normalize
        seen = set()
        unique = []
        for ent in all_entities:
            key = (ent.canonical_name.lower(), ent.entity_type)
            if key not in seen and len(ent.entity_text) >= self.min_entity_len:
                seen.add(key)
                unique.append(ent)

        result.entities = unique[:self.max_entities_per_doc]
        result.sentences = text.count(". ")
        logger.debug(f"Extracted {len(result.entities)} entities from {result.word_count} words")
        return result

    def _extract_with_spacy_doc(self, doc, full_text: str) -> list[ExtractedEntity]:
        """Extract entities from a pre-processed spaCy Doc object (used by nlp.pipe batch)."""
        return self._spacy_doc_to_entities(doc, full_text)

    def _extract_with_spacy(self, text: str) -> list[ExtractedEntity]:
        """spaCy NER extraction. Caller must pre-slice to max_spacy_chars."""
        entities = []
        doc = self._nlp(text)

        return self._spacy_doc_to_entities(doc, text)

    def _spacy_doc_to_entities(self, doc, full_text: str) -> list[ExtractedEntity]:
        """Convert a spaCy Doc's entities to ExtractedEntity list. Shared by single + batch paths."""
        entities = []
        spacy_type_map = {
            "ORG": "COMPANY", "PRODUCT": "PRODUCT",
            "GPE": "LOCATION", "LOC": "LOCATION",
            "PERSON": "PERSON", "MONEY": "AMOUNT", "PERCENT": "AMOUNT",
        }
        for ent in doc.ents:
            mapped = spacy_type_map.get(ent.label_, "CONCEPT")
            raw_text = ent.text.strip()
            if ent.label_ == "ORG":
                lower = raw_text.lower()
                words = lower.split()
                if len(raw_text) < 3:
                    continue
                if len(words) == 1 and lower == raw_text and not raw_text.isupper():
                    continue
                if lower in _SPACY_ORG_NOISE:
                    continue
                if raw_text[0].isdigit():
                    continue
            context = full_text[max(0, ent.start_char - 80): ent.end_char + 80]
            entities.append(ExtractedEntity(
                entity_text=raw_text,
                entity_type=mapped,
                canonical_name=raw_text.title(),
                confidence=0.85,
                start_char=ent.start_char,
                end_char=ent.end_char,
                context=context,
                metadata={"spacy_label": ent.label_},
            ))
        return entities

    def _extract_with_finbert(self, text: str) -> list[ExtractedEntity]:
        """FinBERT-based financial NER extraction."""
        entities = []
        try:
            ner_results = self._finbert(text)
            label_map = {
                "B-ORG": "COMPANY", "I-ORG": "COMPANY",
                "ORG": "COMPANY",
                "B-PER": "PERSON", "I-PER": "PERSON",
                "PER": "PERSON",
            }
            for item in ner_results:
                label = label_map.get(item.get("entity_group", ""), "CONCEPT")
                entities.append(ExtractedEntity(
                    entity_text=item["word"],
                    entity_type=label,
                    confidence=float(item.get("score", 0.8)),
                    metadata={"finbert_label": item.get("entity_group", "")},
                ))
        except Exception as e:
            logger.warning(f"FinBERT extraction error: {e}")
        return entities

    def _extract_with_rules(self, text: str) -> list[ExtractedEntity]:
        """Rule-based extraction for technologies, sectors, and financial concepts."""
        entities = []

        # Technology keywords
        for kw in TECHNOLOGY_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
                entities.append(ExtractedEntity(
                    entity_text=kw,
                    entity_type="TECHNOLOGY",
                    canonical_name=kw,
                    confidence=0.9,
                    metadata={"source": "keyword"},
                ))

        # Sector keywords
        for kw in SECTOR_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
                entities.append(ExtractedEntity(
                    entity_text=kw,
                    entity_type="SECTOR",
                    canonical_name=kw,
                    confidence=0.9,
                    metadata={"source": "keyword"},
                ))

        # Concept keywords
        for kw in CONCEPT_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
                entities.append(ExtractedEntity(
                    entity_text=kw,
                    entity_type="CONCEPT",
                    canonical_name=kw,
                    confidence=0.85,
                    metadata={"source": "keyword"},
                ))

        # Dollar amounts (capex signals)
        for match in re.finditer(
            r"\$\s?(\d+(?:\.\d+)?)\s*(billion|million|trillion|B|M|bn|mn)?",
            text, re.IGNORECASE
        ):
            entities.append(ExtractedEntity(
                entity_text=match.group(0),
                entity_type="AMOUNT",
                canonical_name=match.group(0),
                confidence=0.95,
                start_char=match.start(),
                end_char=match.end(),
                context=text[max(0, match.start()-100): match.end()+100],
                metadata={"source": "regex", "raw_value": match.group(1), "unit": match.group(2)},
            ))

        # Indian Rupee amounts: Rs. 1,234 crore / INR 500 lakh / ₹1200 Cr
        for match in re.finditer(
            r"(?:Rs\.?\s*|INR\s*|₹\s*)(\d[\d,]*(?:\.\d+)?)\s*(crores?|lakhs?|Crs?\b|cr\b|L\b)",
            text, re.IGNORECASE
        ):
            canonical = f"Rs {match.group(1)} {match.group(2)}"
            entities.append(ExtractedEntity(
                entity_text=match.group(0),
                entity_type="AMOUNT",
                canonical_name=canonical,
                confidence=0.95,
                start_char=match.start(),
                end_char=match.end(),
                context=text[max(0, match.start()-100): match.end()+100],
                metadata={
                    "source": "regex",
                    "currency": "INR",
                    "raw_value": match.group(1).replace(",", ""),
                    "unit": match.group(2),
                },
            ))

        return entities

    def extract_batch(self, texts: list[tuple[int, str]]) -> list[ExtractionResult]:
        """Extract entities from multiple (doc_id, text) pairs using nlp.pipe() for speed.

        spaCy's nlp.pipe() processes a batch of texts in one pass — typically 3-5x faster
        than calling nlp(text) individually because it amortizes tokenization overhead.
        Rule-based extraction still runs per-doc (it's already fast).
        """
        if not texts:
            return []

        total = len(texts)
        results = [None] * total
        doc_ids = [t[0] for t in texts]
        raw_texts = [t[1] for t in texts]

        # ── spaCy batch pass (NER only) ────────────────────────────────────
        spacy_ents_by_idx: list[list] = [[] for _ in range(total)]
        if self.use_spacy:
            self._load_spacy()
            if self._nlp:
                capped = [txt[:self.max_spacy_chars] for txt in raw_texts]
                try:
                    for idx, spacy_doc in enumerate(self._nlp.pipe(capped, batch_size=32)):
                        spacy_ents_by_idx[idx] = self._extract_with_spacy_doc(spacy_doc, raw_texts[idx])
                        if (idx + 1) % 100 == 0:
                            logger.info(f"NLP spaCy pipe [{idx+1}/{total}]")
                except Exception as e:
                    logger.warning(f"spaCy pipe failed, falling back per-doc: {e}")
                    for idx, txt in enumerate(capped):
                        try:
                            spacy_ents_by_idx[idx] = self._extract_with_spacy(txt)
                        except Exception:
                            pass

        # ── Rule-based + combine per doc ──────────────────────────────────
        for idx, (doc_id, text) in enumerate(texts):
            all_entities = []
            all_entities.extend(self._extract_with_rules(text))
            all_entities.extend(spacy_ents_by_idx[idx])
            if self.use_finbert:
                self._load_finbert()
                if self._finbert:
                    all_entities.extend(self._extract_with_finbert(text[:5000]))
            # Deduplicate — same logic as extract()
            _seen = set()
            deduped = []
            for ent in all_entities:
                key = (ent.canonical_name.lower(), ent.entity_type)
                if key not in _seen and len(ent.entity_text) >= self.min_entity_len:
                    _seen.add(key)
                    deduped.append(ent)
            filtered = [e for e in deduped if e.confidence >= self.min_confidence]
            filtered = filtered[:self.max_entities_per_doc]
            results[idx] = ExtractionResult(
                document_id=doc_id,
                entities=filtered,
            )

        logger.info(f"Batch extraction complete: {total} docs")
        return results
