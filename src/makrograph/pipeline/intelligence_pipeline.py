"""Intelligence Pipeline Orchestrator.

Full pipeline:
    SEC Filings / Earnings Calls
        → Ingest (EdgarFetcher + SourceAdapter)
        → Parse (PDFParser)
        → Deduplicate
        → Normalize (TextNormalizer)
        → Store in PostgreSQL (PGStore)
        → NLP Extraction (EntityExtractor + SignalExtractor)
        → Semantic Embeddings (EmbeddingEngine → VectorStore)
        → Graph Building (GraphBuilder → Neo4j + PGStore)
        → Graphiti Episode Ingest (TemporalGraphStore - bi-temporal facts)
        → Topic Modeling (TopicModeler → BERTrend acceleration)
        → Theme Detection (ThemeDetector)
        → Theme Ranking (ThemeRanker)
        → Beneficiary Mapping (BeneficiaryMapper)
        → GraphRAG Reasoning (GraphRAG - multi-hop analysis)
        → Selective LLM (LLMReasoner - DeepSeek / Claude / GPT-4o)
        → Ontology Evolution (GraphEvolutionTracker)
        → Macro/Policy Layer (FRED + EIA + World Bank + Congress + Federal Register [US]
                              + PIB + SEBI + RBI + InvestIndia + Commerce/DGFT [IN])
        → Constraint Engine (macro signals → theme corroboration/constraint)
"""

import logging
import re
import time
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

logger = logging.getLogger(__name__)


class IntelligencePipeline:
    """Full end-to-end intelligence pipeline for theme detection.

    Usage:
        with IntelligencePipeline(config) as pipeline:
            pipeline.run_ingest()        # fetch + parse + store
            pipeline.run_nlp()           # entity + signal extraction
            pipeline.run_graph()         # build ontology graph
            pipeline.run_themes()        # detect + rank themes
            pipeline.run_llm_enrichment()  # selective LLM

            # Or run everything:
            pipeline.run_full()
    """

    def __init__(self, config: dict):
        self.config = config
        self._pg_store = None
        self._vector_store = None
        self._graph_store = None
        self._entity_extractor = None
        self._signal_extractor = None
        self._embedding_engine = None
        self._graph_builder = None
        self._evolution_tracker = None
        self._topic_modeler = None
        self._bertrend = None
        self._theme_detector = None
        self._theme_ranker = None
        self._theme_canonicalizer = None
        self._beneficiary_mapper = None
        self._llm_reasoner = None
        self._graphiti_store = None
        self._graph_rag = None
        self._event_extractor = None
        self._causal_mapper = None
        self._supply_chain_analyzer = None
        self._macro_store = None
        self._macro_graph = None
        self._constraint_engine = None

    # ----------------------------------------------------------
    # INITIALIZATION
    # ----------------------------------------------------------
    def _init_storage(self):
        """Initialize PostgreSQL, Neo4j, and vector stores."""
        from ..storage.pg_store import PGStore
        from ..storage.graph_store import GraphStore
        from ..storage.vector_store import VectorStore

        pg_cfg = self.config.get("postgresql", {})
        neo4j_cfg = self.config.get("neo4j", {})
        vec_cfg = {**pg_cfg, **self.config.get("embeddings", {})}

        if pg_cfg.get("host"):
            self._pg_store = PGStore(pg_cfg)
        if neo4j_cfg.get("enabled", False) and neo4j_cfg.get("uri"):
            try:
                self._graph_store = GraphStore(neo4j_cfg)
            except Exception as e:
                logger.warning(f"Neo4j unreachable — graph features disabled. Set neo4j.enabled: false in settings.yaml to silence this. ({type(e).__name__})")

        # Graphiti temporal graph (optional — requires graphiti-core)
        if neo4j_cfg.get("enabled", False) and neo4j_cfg.get("uri") and self.config.get("graphiti", {}).get("enabled", False):
            from ..ontology.graphiti_store import TemporalGraphStore
            graphiti_cfg = {**neo4j_cfg, **self.config.get("graphiti", {})}
            try:
                self._graphiti_store = TemporalGraphStore(graphiti_cfg)
                if self._graphiti_store.is_available:
                    self._graphiti_store.build_indices()
            except Exception as e:
                logger.warning(f"Graphiti init failed: {e}")
        if pg_cfg.get("host") and self.config.get("embeddings", {}).get("enabled", True):
            self._vector_store = VectorStore(vec_cfg)

    def _init_nlp(self):
        """Initialize NLP components."""
        from ..nlp.entity_extractor import EntityExtractor
        from ..nlp.signal_extractor import SignalExtractor
        from ..nlp.embeddings import EmbeddingEngine

        nlp_cfg = self.config.get("nlp", {})
        emb_cfg = self.config.get("embeddings", {})

        self._entity_extractor = EntityExtractor(nlp_cfg)
        self._signal_extractor = SignalExtractor(nlp_cfg)
        self._embedding_engine = EmbeddingEngine(emb_cfg)

    def _init_graph_builder(self):
        from ..ontology.graph_builder import GraphBuilder
        from ..ontology.graph_evolution import GraphEvolutionTracker

        self._graph_builder = GraphBuilder(
            graph_store=self._graph_store,
            pg_store=self._pg_store,
        )
        self._evolution_tracker = GraphEvolutionTracker(
            pg_store=self._pg_store,
            window_days=self.config.get("evolution", {}).get("window_days", 30),
            compare_days=self.config.get("evolution", {}).get("compare_days", 90),
        )

    def _init_themes(self):
        from ..topics.topic_modeler import TopicModeler
        from ..topics.bertrend import BERTrend
        from ..themes.theme_detector import ThemeDetector
        from ..themes.theme_ranker import ThemeRanker
        from ..themes.beneficiary_mapper import BeneficiaryMapper
        from ..themes.theme_canonicalizer import ThemeCanonicalizer

        self._topic_modeler = TopicModeler(self.config.get("topics", {}))
        self._bertrend = BERTrend(self.config.get("bertrend", {}))
        self._theme_detector = ThemeDetector(self.config.get("themes", {}))
        self._theme_ranker = ThemeRanker(self.config.get("themes", {}))
        self._beneficiary_mapper = BeneficiaryMapper(self.config.get("themes", {}))
        # Canonicalizer wires in the embedding engine and LLM once they're available
        self._theme_canonicalizer = ThemeCanonicalizer(
            config=self.config,
            embedding_engine=self._embedding_engine,  # may be None if not yet initialized
            llm_reasoner=self._llm_reasoner,           # may be None — LLM is optional
            pg_store=self._pg_store,
        )

        # Ensure canonicalization + contradiction schema is up to date (idempotent)
        if self._pg_store:
            try:
                self._pg_store.ensure_canonicalization_columns()
                self._pg_store.ensure_canonical_review_table()
                self._pg_store.ensure_contradictions_table()
                self._pg_store.ensure_country_columns()
            except Exception as e:
                logger.warning(f"Schema migration failed (non-fatal): {e}")

    def _init_intelligence(self):
        """Initialize event extraction, causal mapping, and supply chain layers."""
        from ..nlp.event_extractor import EventExtractor
        from ..ontology.causal_mapper import CausalMapper
        from ..ontology.supply_chain import SupplyChainAnalyzer

        nlp_cfg = self.config.get("nlp", {})
        self._event_extractor = EventExtractor(nlp_cfg)
        self._causal_mapper = CausalMapper(self.config)
        self._supply_chain_analyzer = SupplyChainAnalyzer(
            config=self.config,
            graph_store=self._graph_store,
        )

    def _init_llm(self):
        from ..llm.llm_reasoner import LLMReasoner
        from ..llm.graph_rag import GraphRAG

        self._llm_reasoner = LLMReasoner(self.config.get("llm", {}))
        self._graph_rag = GraphRAG(
            graph_store=self._graph_store,
            graphiti_store=self._graphiti_store,
            pg_store=self._pg_store,
            llm_reasoner=self._llm_reasoner,
            config=self.config.get("graph_rag", {}),
        )

    # ----------------------------------------------------------
    # STAGE 1: INGEST
    # ----------------------------------------------------------
    def run_ingest(self, since: Optional[datetime] = None) -> dict:
        """Fetch, parse, deduplicate, normalize, and store documents."""
        from ..fetcher.edgar_fetcher import EdgarFetcher
        from ..parser.pdf_parser import PDFParser
        from ..dedup.deduplicator import Deduplicator
        from ..normalizer.text_normalizer import TextNormalizer

        start = time.time()
        stats = {"docs_fetched": 0, "docs_parsed": 0, "docs_stored": 0, "docs_skipped": 0}

        edgar_cfg = self.config.get("edgar", {})
        if not edgar_cfg.get("ticker_list") and not edgar_cfg.get("cik_list"):
            logger.info("No EDGAR tickers/CIKs configured. Skipping ingest.")
            return stats

        fetcher = EdgarFetcher({
            **edgar_cfg,
            "download_dir": self.config.get("storage", {}).get("download_dir", "data/raw"),
            "user_agent": self.config.get("user_agent", "MakroGraph/0.2 (Research Pipeline)"),
        })

        parser = PDFParser(self.config.get("parser", {}))
        dedup = Deduplicator(self.config.get("dedup", {}))
        normalizer = TextNormalizer(self.config.get("normalizer", {}))

        # Load checkpoint
        if self._pg_store and since is None:
            since = self._pg_store.get_checkpoint("edgar")

        logger.info(f"EDGAR ingest since: {since}")
        fetch_results, source_docs = fetcher.fetch_discovered(since)
        stats["docs_fetched"] = len(fetch_results)

        for result, source_doc in zip(fetch_results, source_docs):
            if not result.success:
                stats["docs_skipped"] += 1
                continue

            # Parse text from downloaded file
            raw_text = ""
            if result.local_path and result.local_path.exists():
                suffix = result.local_path.suffix.lower()
                if suffix == ".pdf":
                    parse_result = parser.parse(result.local_path)
                    raw_text = parse_result.text if parse_result.success else ""
                else:
                    try:
                        raw_text = result.local_path.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        raw_text = ""

            stats["docs_parsed"] += 1

            # Deduplicate
            url_hash = dedup._hash_url(result.url)
            content_hash = dedup._hash_content(raw_text) if raw_text else url_hash
            dup_result = dedup.check(result.url, raw_text or "")
            if dup_result.status.value != "new":
                stats["docs_skipped"] += 1
                continue

            # Normalize
            normalized = normalizer.normalize(raw_text or "") or raw_text

            # Store
            if self._pg_store:
                doc_id = self._pg_store.upsert_document({
                    "source_name": "edgar",
                    "doc_type": source_doc.filing_type or "filing",
                    "url": result.url,
                    "url_hash": url_hash,
                    "content_hash": content_hash,
                    "title": source_doc.title,
                    "company": source_doc.company,
                    "ticker": source_doc.ticker,
                    "cik": source_doc.metadata.get("cik", ""),
                    "filing_type": source_doc.filing_type,
                    "filed_at": source_doc.published_at.date() if source_doc.published_at else None,
                    "published_at": source_doc.published_at,
                    "local_path": str(result.local_path) if result.local_path else "",
                    "word_count": len(raw_text.split()) if raw_text else 0,
                    "processing_status": "fetched",
                    "country": self.config.get("market", {}).get("country", "US"),
                })
                if doc_id:
                    stats["docs_stored"] += 1

        if self._pg_store:
            self._pg_store.set_checkpoint("edgar", datetime.now(timezone.utc), stats["docs_stored"])

        stats["duration_sec"] = round(time.time() - start, 2)
        self._log_run("ingest", stats)
        logger.info(f"Ingest complete: {stats}")
        return stats

    # ----------------------------------------------------------
    # STAGE 1b: INGEST — INDIA
    # Completely isolated from run_ingest() (US/EDGAR path).
    # Called by run_full() when market.country == "IN".
    # ----------------------------------------------------------
    def run_ingest_india(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> dict:
        """Fetch, parse, deduplicate, normalize, and store India company filings.

        Sources (all optional, enabled by presence of config block):
          - NSE India    (corporate announcements, filings)
          - BSE India    (announcements, order wins, board decisions)
          - Screener.in  (annual reports, presentations, concall links)

        India policy / regulatory context sources (PIB, SEBI, RBI, InvestIndia,
        Commerce/DGFT) are handled separately by run_macro() which stores them
        as policy events in mg_policy_events with country='IN'.

        Args:
            since: Fetch documents published after this datetime (lower bound).
                   None → use per-source checkpoint from DB (incremental mode).
            until: Fetch documents published on or before this datetime (upper bound).
                   Comes directly from the UI end_date for historical / quarterly runs.
                   None → fetch up to today (weekly incremental mode).

        Each source maintains its own checkpoint key so partial failures
        do not reset the entire India ingest.

        No code from run_ingest() (EDGAR/US) is called or modified here.
        """
        from ..fetcher.nse_fetcher import NSEFetcher
        from ..fetcher.bse_fetcher import BSEFetcher
        from ..fetcher.screener_fetcher import ScreenerFetcher
        from ..parser.pdf_parser import PDFParser
        from ..dedup.deduplicator import Deduplicator
        from ..normalizer.text_normalizer import TextNormalizer

        start = time.time()
        combined_stats: dict = {
            "docs_fetched": 0, "docs_parsed": 0,
            "docs_stored": 0, "docs_skipped": 0,
        }

        _dl_dir = self.config.get("storage", {}).get("download_dir", "data/raw")
        _ua = self.config.get("user_agent", "MakroGraph/0.2 (India Research Pipeline)")
        _fetcher_cfg = self.config.get("fetcher", {})
        _base_cfg = {
            "download_dir": _dl_dir,
            "user_agent": _ua,
            "request_timeout_seconds": _fetcher_cfg.get("request_timeout_seconds", 30),
            "retry_attempts":          _fetcher_cfg.get("retry_attempts", 3),
            "retry_delay_seconds":     _fetcher_cfg.get("retry_delay_seconds", 2),
            # max_results_per_run is intentionally NOT set here — each source's own
            # config block (nse, bse, screener …) controls its limit.
            # 0 = unlimited, which is the recommended default for India.
        }

        dedup = Deduplicator(self.config.get("dedup", {}))
        normalizer = TextNormalizer(self.config.get("normalizer", {}))
        # PDF parser is only used for sources that actually download heavy documents
        # (e.g. Screener annual reports).  NSE/BSE store metadata only — no PDF download.
        _parser_lazy = None

        def _get_parser():
            nonlocal _parser_lazy
            if _parser_lazy is None:
                _parser_lazy = PDFParser(self.config.get("parser", {}))
            return _parser_lazy

        # Company filing sources only — exchange announcements and filings.
        # Policy / regulatory sources (PIB, SEBI, RBI, InvestIndia, Commerce/DGFT)
        # are fetched by run_macro() and stored as policy events (mg_policy_events).
        india_sources = [
            ("nse_india",       NSEFetcher,      self.config.get("nse", {})),
            ("bse_india",       BSEFetcher,      self.config.get("bse", {})),
            ("screener_india",  ScreenerFetcher, self.config.get("screener", {})),
        ]

        # Doc types that are worth downloading as full PDF for NLP.
        # All other NSE/BSE announcements are metadata-only (no download needed).
        _PDF_WORTHY_TYPES = {
            "annual_report", "investor_presentation", "presentation",
            "concall_transcript", "earnings",
        }

        for source_key, FetcherClass, source_cfg in india_sources:
            if not source_cfg.get("enabled", True):
                logger.info(f"India ingest: '{source_key}' disabled in config — skipping")
                continue

            cfg = {**_base_cfg, **source_cfg}
            source_since = since
            if self._pg_store and source_since is None:
                source_since = self._pg_store.get_checkpoint(source_key)

            logger.info(
                f"India ingest [{source_key}] "
                f"since: {source_since.date() if source_since else 'start'} "
                f"until: {until.date() if until else 'today'}"
            )

            src_stats = {"docs_fetched": 0, "docs_parsed": 0, "docs_stored": 0, "docs_skipped": 0}

            try:
                with FetcherClass(cfg) as fetcher:
                    # ── DISCOVER only — do NOT call fetch_discovered() ─────────────────
                    # fetch_discovered() downloads every single PDF attachment, which for
                    # NSE/BSE can be 3 000–8 000 files per month (hours of I/O).
                    # Exchange announcement metadata (company, date, subject, category)
                    # is already in the API response — no PDF download needed.
                    # Full PDF download is reserved for annual_report / presentation types.
                    source_docs = fetcher.discover(source_since, until=until)
                    src_stats["docs_fetched"] = len(source_docs)
                    logger.info(f"India ingest [{source_key}]: {len(source_docs)} documents discovered")

                    for source_doc in source_docs:
                        filing_type = (source_doc.filing_type or source_doc.doc_type or "announcement").lower()

                        url_hash = dedup._hash_url(source_doc.url)
                        # Check URL-only dedup (no content yet — content is fetched below).
                        # Pass URL as text so content_hash is unique per URL and the
                        # in-memory content_cache doesn't give false "duplicate" hits
                        # when all docs have empty text (hash("") would match every doc).
                        dup_result = dedup.check(source_doc.url, source_doc.url)
                        if dup_result.status.value != "new":
                            src_stats["docs_skipped"] += 1
                            continue

                        src_stats["docs_parsed"] += 1
                        raw_text = ""
                        local_path_str = ""

                        # ── PDF download only for high-value document types ─────────────
                        # Reuse the same fetcher session (no extra context manager needed).
                        if filing_type in _PDF_WORTHY_TYPES:
                            try:
                                fetch_result = fetcher.fetch(source_doc.url)
                                if (fetch_result.success and fetch_result.local_path
                                        and fetch_result.local_path.exists()):
                                    local_path_str = str(fetch_result.local_path)
                                    suffix = fetch_result.local_path.suffix.lower()
                                    if suffix == ".pdf":
                                        parse_result = _get_parser().parse(fetch_result.local_path)
                                        raw_text = parse_result.text if parse_result.success else ""
                                    else:
                                        try:
                                            raw_text = fetch_result.local_path.read_text(
                                                encoding="utf-8", errors="ignore"
                                            )
                                        except Exception:
                                            raw_text = ""
                            except Exception as _fe:
                                logger.debug(f"[{source_key}] PDF fetch failed for {source_doc.url}: {_fe}")

                        # ── Use announcement title as text when no PDF downloaded ────────
                        # The title/subject from the NSE/BSE API IS the announcement content
                        # for most filings (board decisions, order wins, results, capex, etc.)
                        if not raw_text:
                            raw_text = source_doc.title or ""

                        content_hash = dedup._hash_content(raw_text) if raw_text else url_hash

                        if self._pg_store:
                            try:
                                doc_id = self._pg_store.upsert_document({
                                    "source_name":      source_key,
                                    "doc_type":         source_doc.doc_type or "announcement",
                                    "url":              source_doc.url,
                                    "url_hash":         url_hash,
                                    "content_hash":     content_hash,
                                    "title":            source_doc.title,
                                    "company":          source_doc.company,
                                    "ticker":           source_doc.ticker,
                                    "cik":              "",
                                    "filing_type":      source_doc.filing_type,
                                    "filed_at":         source_doc.published_at.date() if source_doc.published_at else None,
                                    "published_at":     source_doc.published_at,
                                    "local_path":       local_path_str,
                                    "word_count":       len(raw_text.split()) if raw_text else 0,
                                    "processing_status": "fetched",
                                    "country":          "IN",
                                })
                                if doc_id:
                                    src_stats["docs_stored"] += 1
                                else:
                                    # ON CONFLICT returned no row — already exists, count as skipped
                                    src_stats["docs_skipped"] += 1
                            except Exception as _de:
                                # Per-document error (e.g. content_hash collision from a different URL).
                                # Log and skip this doc; do not abort the entire source.
                                logger.debug(f"[{source_key}] upsert failed for {source_doc.url}: {_de}")
                                src_stats["docs_skipped"] += 1

                if self._pg_store:
                    self._pg_store.set_checkpoint(
                        source_key, datetime.now(timezone.utc), src_stats["docs_stored"]
                    )

                logger.info(f"India ingest [{source_key}] complete: {src_stats}")

            except Exception as exc:
                logger.error(f"India ingest [{source_key}] failed: {exc}", exc_info=True)
                src_stats["error"] = str(exc)

            for k in ("docs_fetched", "docs_parsed", "docs_stored", "docs_skipped"):
                combined_stats[k] += src_stats.get(k, 0)

        combined_stats["duration_sec"] = round(time.time() - start, 2)
        self._log_run("ingest_india", combined_stats)
        logger.info(f"India ingest complete: {combined_stats}")
        return combined_stats

    # ----------------------------------------------------------
    # STAGE 1b: PDF CONTENT FETCH FOR INDIA HIGH-VALUE DOCS
    # ----------------------------------------------------------
    def run_pdf_fetch_india(
        self,
        batch_size: int = 200,
        filing_types: list[str] | None = None,
        rate_limit_seconds: float = 0.5,
        max_workers: int = 4,
        window_start=None,
        window_end=None,
        store_text_to_db: bool = False,
        delete_after_parse: bool = False,
    ) -> dict:
        """Download PDFs for high-signal-value India filing categories.

        NSE ingestion by default stores only the announcement title (30-90 words)
        because downloading every PDF (177k+ files) would take hours.  This stage
        selectively downloads PDFs for the categories that contain the richest
        financial signals.

        Historical mode (store_text_to_db=True, delete_after_parse=True):
            Each PDF is downloaded → text extracted → raw_text stored in DB →
            file deleted immediately.  Disk usage stays near-zero regardless of
            how many months are processed.  NLP then reads raw_text from DB.

        Live mode (defaults):
            PDFs written to data/india/pdfs/. NLP reads from local_path on disk.

        Args:
            batch_size:          How many docs to process per loop iteration.
            filing_types:        Override the default high-value category list.
            rate_limit_seconds:  Seconds to sleep between downloads per worker.
            max_workers:         Parallel download threads.
            window_start:        Optional date — only process docs filed >= this date.
            window_end:          Optional date — only process docs filed <= this date.
            store_text_to_db:    If True, write extracted text to raw_text column in DB.
                                 Used by historical runner to avoid re-reading files later.
            delete_after_parse:  If True, delete the PDF from disk after extracting text.
                                 Only meaningful when store_text_to_db=True.
        Returns:
            dict with downloaded/failed/skipped/unsupported counts.
        """
        import time as _time
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from pathlib import Path
        from ..parser.pdf_parser import PDFParser

        _HIGH_VALUE_CATEGORIES = filing_types or [
            # ── Financial performance ─────────────────────────────────────
            "Outcome of Board Meeting",
            "Financial Result Updates",
            "Financial Results Updates",          # alternate spelling
            "Reply to Clarification- Financial results",
            "Reply to Clarification Sought- Financial Results",
            "Integrated Filing- Financial",
            # ── Management commentary ─────────────────────────────────────
            "Analysts/Institutional Investor Meet/Con. Call Updates",
            "Transcript of Analysts/Institutional Investor Meet/Con. Call",
            "Recording of Analysts/Institutional Investor Meet/Con. Call",
            "Schedule of Analysts/Institutional Investor Meet/Con. Call",
            "Investor Presentation",
            "Press Release",
            "Press Release (Revised)",
            # ── M&A / restructuring ───────────────────────────────────────
            "Acquisition",
            "Amalgamation/Merger",
            "Scheme of Arrangement",
            "Demerger",
            "Open Offer",
            "Public Announcement-Open Offer",
            # ── Capital market events ─────────────────────────────────────
            "Buyback",
            "Rights Issue",
            "Qualified Institutional Placement",
            "Stock split",
            "Offer for sale",
            # ── Order wins / capacity ─────────────────────────────────────
            "Bagging/Receiving of orders/contracts",
            "Bagging orders/contract",
            "Awarding of order(s)/contract(s)",
            "Awarding orders/contract",
            "Capacity addition",
            "Capacity addition/product launch",
            "Commencement of commercial production/operations",
            # ── Agreements ───────────────────────────────────────────────
            "Memorandum of Understanding/Agreements",
            "Agreements",
            # ── Distress / risk signals ───────────────────────────────────
            "Corporate Insolvency Resolution Process",
            "Defaults on Payment of Interest/Principal",
            "Strikes/Lockouts/Disturbances",
            "Disruption of Operations",
            "Disruption of operations",
        ]

        stats = {
            "docs_attempted": 0,
            "docs_downloaded": 0,
            "docs_failed": 0,
            "docs_skipped": 0,
            "docs_unsupported": 0,   # ZIP with no PDF / non-PDF content
        }

        if not self._pg_store:
            logger.warning("PGStore not initialized. PDF fetch skipped.")
            return stats

        start = time.time()

        # Storage dir for downloaded PDFs — same pattern as EDGAR downloads
        storage_cfg = self.config.get("storage", {})
        project_root = Path(storage_cfg.get(
            "project_root",
            Path(__file__).resolve().parent.parent.parent.parent,
        ))
        pdf_dir = project_root / "data" / "india" / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)

        pdf_parser = PDFParser(self.config.get("parser", {}))

        # Use curl_cffi for Akamai bypass if available
        try:
            from curl_cffi import requests as _cffi_requests
            _cffi_available = True
        except ImportError:
            import requests as _std_requests
            _cffi_available = False
            logger.warning("[pdf_fetch_india] curl_cffi not installed — using plain requests. "
                           "NSE Akamai may block some downloads. pip install curl_cffi for reliability.")

        # ZIP magic bytes — BSE sometimes serves ZIP archives at PDF URLs
        _ZIP_MAGIC = b"PK\x03\x04"

        def _download_pdf(url: str, dest: Path) -> str:
            """Download a PDF from NSE/BSE. Returns status string:
              'ok'          — PDF downloaded and confirmed valid
              'zip'         — BSE served a ZIP archive (handled separately)
              'fail'        — HTTP error or too small
            For BSE AttachLive URLs that return 404, automatically retries
            with AttachHis (older filings are archived there).
            """
            def _fetch(u: str):
                if _cffi_available:
                    return _cffi_requests.get(u, impersonate="chrome124", timeout=120,
                                              headers={"Referer": "https://www.bseindia.com/"})
                return _std_requests.get(u, timeout=120, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0",
                    "Referer": "https://www.nseindia.com/",
                })

            urls_to_try = [url]
            # BSE AttachLive → AttachHis fallback for older filings
            if "AttachLive" in url:
                urls_to_try.append(url.replace("AttachLive", "AttachHis"))

            try:
                for attempt_url in urls_to_try:
                    resp = _fetch(attempt_url)
                    if resp.status_code == 200 and len(resp.content) > 500:
                        content = resp.content
                        # Detect ZIP archive — BSE sometimes wraps PDFs in a .zip
                        if content[:4] == _ZIP_MAGIC:
                            import zipfile, io
                            try:
                                zf = zipfile.ZipFile(io.BytesIO(content))
                                # Find first PDF entry inside the ZIP
                                pdf_entries = [n for n in zf.namelist()
                                               if n.lower().endswith(".pdf")]
                                if pdf_entries:
                                    # Save just the first PDF, rename dest to .pdf
                                    pdf_bytes = zf.read(pdf_entries[0])
                                    dest.write_bytes(pdf_bytes)
                                    logger.debug(f"[pdf_fetch_india] Extracted {pdf_entries[0]} from ZIP at {attempt_url}")
                                    return "ok"
                                else:
                                    logger.info(f"[pdf_fetch_india] ZIP has no PDF entries: {attempt_url}")
                                    return "zip"   # unsupported — ZIP with no PDF
                            except Exception as ze:
                                logger.info(f"[pdf_fetch_india] ZIP extract failed for {attempt_url}: {ze}")
                                return "zip"
                        # Normal PDF
                        dest.write_bytes(content)
                        return "ok"
                    logger.info(f"[pdf_fetch_india] HTTP {resp.status_code} for {attempt_url}")
                return "fail"
            except Exception as exc:
                logger.info(f"[pdf_fetch_india] Download error for {url}: {exc}")
                return "fail"

        # Build type placeholders for SQL
        placeholders = ", ".join(["%s"] * len(_HIGH_VALUE_CATEGORIES))

        # Optional date-window filter (used by historical runner — one month at a time)
        _date_clause = ""
        _date_params: tuple = ()
        if window_start is not None:
            _date_clause += " AND filed_at >= %s"
            _date_params += (window_start,)
        if window_end is not None:
            _date_clause += " AND filed_at <= %s"
            _date_params += (window_end,)

        # Historical mode: only fetch docs without raw_text.
        # DO NOT include "local_path IS NULL" here — after delete_after_parse, local_path
        # is set to NULL but raw_text is populated. Including local_path IS NULL would
        # cause the loop to re-query already-processed docs, emptying the batch after 1 pass.
        # Live mode: fetch docs without a local file on disk.
        _path_clause = (
            "AND (raw_text IS NULL OR raw_text = '') AND processing_status != 'unsupported'"
            if store_text_to_db
            else "AND (local_path IS NULL OR local_path = '') AND processing_status != 'unsupported'"
        )

        # Track already-attempted doc IDs to avoid re-fetching failed docs on retry
        # within the same run (failed docs keep local_path=NULL so re-query picks them).
        _seen_ids: set[int] = set()

        while True:
            # Exclude already-attempted IDs at DB level so ORDER BY + LIMIT always
            # returns genuinely new docs. Python-side _seen_ids filtering caused the
            # batch to shrink to 0 when "bad" docs (empty parse → raw_text=NULL) sat
            # at the top of ORDER BY filed_at DESC and crowded out valid unseen docs.
            _excl_clause = ""
            _excl_params: tuple = ()
            if _seen_ids:
                _excl_placeholders = ", ".join(["%s"] * len(_seen_ids))
                _excl_clause = f"AND id NOT IN ({_excl_placeholders})"
                _excl_params = tuple(_seen_ids)

            with self._pg_store._conn() as conn:
                with conn.cursor(cursor_factory=self._pg_store._cursor_factory) as cur:
                    cur.execute(
                        f"""SELECT id, ticker, url, filing_type, title
                            FROM mg_documents
                            WHERE country = 'IN'
                              {_path_clause}
                              AND url IS NOT NULL AND url != ''
                              AND filing_type IN ({placeholders})
                              {_date_clause}
                              {_excl_clause}
                            ORDER BY filed_at DESC
                            LIMIT %s""",
                        tuple(_HIGH_VALUE_CATEGORIES) + _date_params + _excl_params + (batch_size,),
                    )
                    batch = [dict(r) for r in cur.fetchall()]

            if not batch:
                break

            logger.info(f"[pdf_fetch_india] Processing batch of {len(batch)} docs "
                        f"(total attempted so far: {len(_seen_ids)}, excluded from query: {len(_seen_ids)})")

            # ── Concurrent download within the batch ──────────────────────
            # Each worker: download → sleep(rate_limit_seconds) → return result.
            # PDF parsing and DB update happen sequentially after all downloads
            # complete (PDFParser is not thread-safe; psycopg2 connections are
            # per-thread-safe but we reuse self._pg_store which pools internally).
            _stats_lock = threading.Lock()

            def _process_doc(doc):
                """Download one PDF; return (doc_id, pdf_path, dl_status, skipped).

                dl_status: 'ok' | 'zip' | 'fail'
                skipped:   True if PDF was already on disk (no download needed)
                """
                doc_id = doc["id"]
                url = doc["url"]
                ticker = doc["ticker"] or "UNKNOWN"

                url_tail = url.rstrip("/").split("/")[-1][:80]
                pdf_name = f"{ticker}_{doc_id}_{url_tail}"
                if not pdf_name.lower().endswith(".pdf"):
                    pdf_name += ".pdf"
                pdf_path = pdf_dir / pdf_name

                if pdf_path.exists() and pdf_path.stat().st_size > 500:
                    return doc_id, pdf_path, "ok", True  # (id, path, status, skipped)

                dl_status = _download_pdf(url, pdf_path)
                _time.sleep(rate_limit_seconds)
                return doc_id, pdf_path, dl_status, False

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_process_doc, d): d for d in batch}
                for fut in as_completed(futures):
                    doc_id, pdf_path, dl_status, skipped = fut.result()
                    _seen_ids.add(doc_id)  # mark as attempted regardless of outcome
                    with _stats_lock:
                        stats["docs_attempted"] += 1

                    if dl_status == "zip":
                        # ZIP with no extractable PDF — mark unsupported in DB so it's
                        # never retried (local_path set to sentinel, status='unsupported')
                        with _stats_lock:
                            stats["docs_unsupported"] += 1
                        with self._pg_store._conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """UPDATE mg_documents
                                       SET local_path = 'UNSUPPORTED_FORMAT',
                                           processing_status = 'unsupported',
                                           updated_at = NOW()
                                       WHERE id = %s""",
                                    (doc_id,),
                                )
                        continue

                    if dl_status == "fail":
                        with _stats_lock:
                            stats["docs_failed"] += 1
                        continue

                    # dl_status == 'ok' — parse text and update DB
                    # (skipped files already on disk still need DB update so
                    #  local_path is set and next query excludes them)
                    parse_result = pdf_parser.parse(pdf_path)
                    extracted_text = parse_result.text if parse_result.success else ""
                    # Strip NUL bytes — Postgres TEXT rejects \x00 characters
                    if extracted_text:
                        extracted_text = extracted_text.replace("\x00", "")

                    # If download succeeded but text extraction failed (scanned image,
                    # password-protected, Excel/Word inside ZIP), mark as 'unsupported'
                    # so it is never re-fetched. Delete file if present.
                    if not extracted_text:
                        with _stats_lock:
                            stats["docs_unsupported"] += 1
                        with self._pg_store._conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """UPDATE mg_documents
                                       SET local_path = 'UNSUPPORTED_FORMAT',
                                           processing_status = 'unsupported',
                                           updated_at = NOW()
                                       WHERE id = %s""",
                                    (doc_id,),
                                )
                        if pdf_path.exists():
                            try:
                                pdf_path.unlink()
                            except Exception:
                                pass
                    elif store_text_to_db:
                        # Historical mode: store text in DB, optionally delete file
                        new_path = None if delete_after_parse else str(pdf_path)
                        with self._pg_store._conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """UPDATE mg_documents
                                       SET raw_text = %s,
                                           local_path = %s,
                                           word_count = %s,
                                           processing_status = 'fetched',
                                           updated_at = NOW()
                                       WHERE id = %s""",
                                    (extracted_text, new_path,
                                     len(extracted_text.split()), doc_id),
                                )
                        if delete_after_parse and pdf_path.exists():
                            try:
                                pdf_path.unlink()
                            except Exception:
                                pass
                    else:
                        # Live mode: keep file on disk, store local_path
                        with self._pg_store._conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """UPDATE mg_documents
                                       SET local_path = %s,
                                           word_count = %s,
                                           processing_status = 'fetched',
                                           updated_at = NOW()
                                       WHERE id = %s""",
                                    (str(pdf_path),
                                     len(extracted_text.split()), doc_id),
                                )

                    if skipped:
                        with _stats_lock:
                            stats["docs_skipped"] += 1
                    else:
                        with _stats_lock:
                            stats["docs_downloaded"] += 1

            logger.info(f"[pdf_fetch_india] Progress: {stats}")

        stats["duration_sec"] = round(time.time() - start, 2)
        logger.info(f"[pdf_fetch_india] Complete: {stats}")
        return stats

    # ----------------------------------------------------------
    # STAGE 2: NLP
    # ----------------------------------------------------------
    def run_nlp(
        self,
        batch_size: int = 500,
        window_start=None,
        window_end=None,
        country: str = None,
    ) -> dict:
        """Run entity and signal extraction on fetched documents.

        If window_start/window_end are given, processes ALL documents whose
        filed_at falls in that range (looping in chunks of batch_size).
        Otherwise processes the next batch_size documents globally.

        Args:
            country: ISO-2 country code (e.g. "IN", "US"). When provided this
                     takes priority over config["market"]["country"], so the
                     UI-selected country is always respected regardless of what
                     is written in settings.yaml.
        """
        if not self._entity_extractor:
            self._init_nlp()

        start = time.time()
        # Explicit parameter wins; fall back to config only as last resort.
        _country = country or self.config.get("market", {}).get("country", "US")
        stats = {"docs_processed": 0, "entities_found": 0, "signals_found": 0, "docs_failed": 0}

        if not self._pg_store:
            logger.warning("PGStore not initialized. NLP skipped.")
            return stats

        def _fetch_batch() -> list[dict]:
            if window_start is not None and window_end is not None:
                return self._pg_store.get_documents_for_replay(
                    "fetched", window_start, window_end, limit=batch_size, country=_country
                )
            return self._pg_store.get_documents_by_status("fetched", limit=batch_size, country=_country)

        docs = _fetch_batch()
        stats["docs_in_batch"] = len(docs)

        # Resolve project root for relative paths stored in DB
        # intelligence_pipeline.py = src/makrograph/pipeline/intelligence_pipeline.py
        # .parent.parent.parent.parent = MakroGraphIntelligence/ (project root)
        project_root = Path(self.config.get("storage", {}).get(
            "project_root", Path(__file__).resolve().parent.parent.parent.parent
        ))

        # Import noise filter — same filter used by historical runner
        from ..themes.theme_detector import _is_noise_entity

        # Cache PDFParser — avoid re-instantiation for every PDF doc
        _pdf_parser = None

        # When a window is given, loop until all docs in the range are processed.
        # Without a window, process exactly one batch then stop.
        use_window = window_start is not None and window_end is not None

        while docs:
            done_ids: list[int] = []
            failed_ids: list[int] = []

            # ── Phase 1: Resolve raw text for every doc in the batch ─────────
            doc_texts: dict[int, str] = {}  # doc_id → raw_text
            for doc in docs:
                doc_id = doc["id"]
                raw_text = (doc.get("raw_text") or "").strip()
                if not raw_text:
                    raw_path = doc.get("local_path", "") or ""
                    if raw_path and raw_path not in ("UNSUPPORTED_FORMAT",):
                        local_path = Path(raw_path)
                        if not local_path.is_absolute():
                            local_path = project_root / local_path
                        if local_path.exists():
                            suffix = local_path.suffix.lower()
                            try:
                                if suffix == ".pdf":
                                    if _pdf_parser is None:
                                        from ..parser.pdf_parser import PDFParser
                                        _pdf_parser = PDFParser(self.config.get("parser", {}))
                                    result = _pdf_parser.parse(local_path)
                                    raw_text = result.text if result.success else ""
                                elif suffix in (".html", ".htm", ".xhtml"):
                                    from bs4 import BeautifulSoup
                                    html = local_path.read_text(encoding="utf-8", errors="ignore")
                                    soup = BeautifulSoup(html, "lxml")
                                    for tag in soup(["script", "style", "header", "footer", "nav"]):
                                        tag.decompose()
                                    raw_text = soup.get_text(separator=" ", strip=True)
                                else:
                                    raw_text = local_path.read_text(encoding="utf-8", errors="ignore")
                            except Exception as e:
                                logger.warning(f"Text extraction failed for {local_path}: {e}")
                if not raw_text:
                    raw_text = (doc.get("title") or "").strip()
                doc_texts[doc_id] = raw_text

            # ── Phase 2: Batch spaCy NER on all texts at once (nlp.pipe) ────
            _extractor = self._entity_extractor
            _spacy_results: dict[int, list] = {}
            if (getattr(_extractor, 'use_spacy', False)
                    and not getattr(_extractor, '_spacy_unavailable', True)):
                _extractor._load_spacy()
                if _extractor._nlp:
                    _ids = list(doc_texts.keys())
                    _capped = [doc_texts[i][:_extractor.max_spacy_chars] for i in _ids]
                    try:
                        for _idx, _sdoc in enumerate(_extractor._nlp.pipe(_capped, batch_size=32)):
                            _spacy_results[_ids[_idx]] = _extractor._spacy_doc_to_entities(
                                _sdoc, doc_texts[_ids[_idx]])
                    except Exception as _e:
                        logger.warning(f"spaCy pipe batch failed: {_e}")

            # ── Phase 3: Per-doc entity + signal processing ──────────────────
            for doc in docs:
                doc_id = doc["id"]
                raw_text = doc_texts.get(doc_id, "")

                if not raw_text:
                    failed_ids.append(doc_id)
                    continue

                # Use pre-computed spaCy entities + rule-based extraction
                _pre_spacy = _spacy_results.get(doc_id, [])
                _rule_ents = _extractor._extract_with_rules(raw_text)
                _all_raw = _rule_ents + _pre_spacy
                _seen_keys = set()
                _deduped = []
                for _e in _all_raw:
                    _k = (_e.canonical_name.lower(), _e.entity_type)
                    if _k not in _seen_keys and len(_e.entity_text) >= _extractor.min_entity_len:
                        _seen_keys.add(_k)
                        _deduped.append(_e)
                _filtered = [e for e in _deduped if e.confidence >= _extractor.min_confidence]
                _filtered = _filtered[:_extractor.max_entities_per_doc]

                from ..nlp.entity_extractor import ExtractionResult
                extraction = ExtractionResult(
                    document_id=doc_id,
                    entities=_filtered,
                )
                clean_entities = [
                    {
                        "entity_text": ent.entity_text,
                        "entity_type": ent.entity_type,
                        "canonical_name": ent.canonical_name,
                        "confidence": ent.confidence,
                        "metadata": ent.metadata if isinstance(ent.metadata, dict) else {},
                    }
                    for ent in extraction.entities
                    if not _is_noise_entity(ent.canonical_name or ent.entity_text or "")
                ]

                # ── Auto-entity from structured metadata (NSE/BSE docs) ──────
                # When company + ticker are stored on the document (all NSE/BSE
                # filings), inject a COMPANY entity so the beneficiary mapper can
                # link it to themes even if NLP text extraction missed it.
                _doc_company = (doc.get("company") or "").strip()
                _doc_ticker  = (doc.get("ticker")  or "").strip()
                if _doc_company and _doc_ticker and not _is_noise_entity(_doc_company):
                    _already = any(
                        (e.get("canonical_name") or e.get("entity_text") or "").lower()
                        == _doc_company.lower()
                        for e in clean_entities
                    )
                    if not _already:
                        clean_entities.append({
                            "entity_text":    _doc_company,
                            "entity_type":    "COMPANY",
                            "canonical_name": _doc_company,
                            "ticker":         _doc_ticker,   # top-level so batch_upsert stores it
                            "confidence":     1.0,
                            "metadata":       {"source": "doc_metadata"},
                        })
                # Capture the name→entity_id map returned by the upsert so we can
                # stamp signals with the correct entity_id (signals previously had
                # entity_id=NULL because this return value was discarded).
                name_to_id: dict[str, int] = {}
                try:
                    name_to_id = self._pg_store.batch_upsert_entities_and_links(
                        doc_id, clean_entities, doc.get("filed_at")
                    )
                except Exception as e:
                    logger.warning(f"Batch entity upsert failed doc {doc_id}: {e}")
                    for ent_d in clean_entities:
                        eid = self._pg_store.upsert_entity({
                            **ent_d,
                            "first_seen_at": doc.get("filed_at"),
                            "last_seen_at": doc.get("filed_at"),
                        })
                        if eid:
                            self._pg_store.link_document_entity(doc_id, eid)
                            name_to_id[ent_d.get("canonical_name") or ent_d.get("entity_text", "")] = eid

                stats["entities_found"] += len(clean_entities)

                # Resolve entity_id for this document's company (from the auto-injected
                # COMPANY entity or any NLP-extracted entity matching the ticker).
                # This entity_id is used to stamp all signals from this document so
                # signals are traceable to their source company entity.
                _doc_entity_id: int | None = None
                if _doc_company and name_to_id:
                    _doc_entity_id = name_to_id.get(_doc_company)
                if _doc_entity_id is None and name_to_id:
                    # Fallback: any COMPANY entity with a matching ticker
                    for ent_d in clean_entities:
                        if (ent_d.get("entity_type") == "COMPANY"
                                and ent_d.get("ticker", "").upper() == _doc_ticker.upper()
                                and (ent_d.get("canonical_name") or ent_d.get("entity_text")) in name_to_id):
                            _doc_entity_id = name_to_id[
                                ent_d.get("canonical_name") or ent_d.get("entity_text")
                            ]
                            break

                # ── Signal extraction ────────────────────────────────────────
                signals = self._signal_extractor.extract(raw_text, document_id=doc_id)
                signal_dicts = [
                    {
                        "document_id": doc_id,
                        "entity_id":   _doc_entity_id,   # link signal → company entity
                        "signal_type": sig.signal_type,
                        "direction": sig.direction,
                        "confidence": sig.confidence,
                        "signal_value": sig.signal_value,
                        "signal_unit": sig.signal_unit,
                        "context_text": sig.context_text[:500],
                        "extracted_by": sig.extracted_by,
                        "filed_at": doc.get("filed_at"),
                        "country": doc.get("country", "US"),
                    }
                    for sig in signals
                ]
                try:
                    self._pg_store.batch_insert_signals(signal_dicts)
                except Exception as e:
                    logger.warning(f"Batch signal insert failed doc {doc_id}: {e}")
                    for sd in signal_dicts:
                        self._pg_store.insert_signal(sd)

                stats["signals_found"] += len(signal_dicts)
                done_ids.append(doc_id)
                stats["docs_processed"] += 1

            # Flush status updates for this chunk
            if done_ids:
                self._pg_store.batch_update_document_status(done_ids, "nlp_done")
            if failed_ids:
                self._pg_store.batch_update_document_status(failed_ids, "nlp_failed")
                stats["docs_failed"] += len(failed_ids)

            logger.info(
                f"NLP chunk: {len(done_ids)} done / {len(failed_ids)} failed  "
                f"[total so far: {stats['docs_processed']} docs, {stats['signals_found']} signals]"
            )

            if not use_window:
                break  # no window — single batch only

            docs = _fetch_batch()  # fetch next chunk within the window

        stats["duration_sec"] = round(time.time() - start, 2)
        self._log_run("nlp", stats)
        logger.info(f"NLP complete: {stats}")
        return stats

    # ----------------------------------------------------------
    # STAGE 3: EMBEDDINGS
    # ----------------------------------------------------------
    def run_embeddings(self, batch_size: int = 500, window_start=None, window_end=None, country: str = None) -> dict:
        """Generate and store semantic embeddings for NLP-processed documents.

        If window_start/window_end are given, processes ALL docs in that date range.
        Otherwise processes one batch of batch_size.

        Args:
            country: ISO-2 country code. Overrides config["market"]["country"] so the
                     UI-selected country is always respected.
        """
        if not self._embedding_engine:
            self._init_nlp()

        start = time.time()
        _country = country or self.config.get("market", {}).get("country", "US")
        stats = {"docs_embedded": 0, "embedding_records": 0}

        if not self._pg_store or not self._vector_store:
            logger.warning("PGStore or VectorStore not initialized. Embeddings skipped.")
            return stats

        project_root = Path(self.config.get("storage", {}).get(
            "project_root", Path(__file__).resolve().parent.parent.parent.parent
        ))
        use_window = window_start is not None and window_end is not None

        def _fetch() -> list[dict]:
            if use_window:
                return self._pg_store.get_documents_for_replay("nlp_done", window_start, window_end, limit=batch_size, country=_country)
            return self._pg_store.get_documents_by_status("nlp_done", limit=batch_size, country=_country)

        docs = _fetch()
        while docs:
            doc_texts = []
            for doc in docs:
                text = ""

                # Priority 1: raw_text from DB (historical mode — files deleted after parse)
                text = (doc.get("raw_text") or "").strip()

                # Priority 2: local file on disk
                if not text:
                    local_path = doc.get("local_path", "") or ""
                    if local_path and local_path not in ("UNSUPPORTED_FORMAT",):
                        lp = Path(local_path)
                        if not lp.is_absolute():
                            lp = project_root / lp
                        if lp.exists():
                            try:
                                suffix = lp.suffix.lower()
                                if suffix == ".pdf":
                                    from ..parser.pdf_parser import PDFParser
                                    result = PDFParser(self.config.get("parser", {})).parse(lp)
                                    text = result.text if result.success else ""
                                else:
                                    text = lp.read_text(encoding="utf-8", errors="ignore")
                            except Exception:
                                pass

                # Priority 3: title fallback
                if not text:
                    text = (doc.get("title") or "").strip()

                if text:
                    doc_texts.append((doc["id"], text))

            if doc_texts:
                embedding_records = self._embedding_engine.embed_documents_batch(doc_texts)
                if embedding_records:
                    self._vector_store.store_batch(embedding_records)
                    stats["embedding_records"] += len(embedding_records)
                    stats["docs_embedded"] += len(doc_texts)

                for doc_id, _ in doc_texts:
                    self._pg_store.update_document_status(doc_id, "embedded")

            docs = _fetch()  # always loop until no more docs

        stats["duration_sec"] = round(time.time() - start, 2)
        self._log_run("embed", stats)
        logger.info(f"Embeddings complete: {stats}")
        return stats

    # ----------------------------------------------------------
    # STAGE 4: GRAPH BUILDING
    # ----------------------------------------------------------
    def run_graph(self, batch_size: int = 500, window_start=None, window_end=None, country: str = None) -> dict:
        """Build ontology graph nodes and edges from NLP results stored in PostgreSQL.

        If window_start/window_end are given, processes ALL docs in that date range.
        Otherwise processes one batch of batch_size.

        Args:
            country: ISO-2 country code. Overrides config["market"]["country"] so the
                     UI-selected country is always respected.
        """
        if not self._graph_builder:
            self._init_graph_builder()

        start = time.time()
        _country = country or self.config.get("market", {}).get("country", "US")
        stats = {"docs_processed": 0, "nodes_built": 0, "edges_built": 0}

        if not self._pg_store:
            logger.warning("PGStore not initialized. Graph building skipped.")
            return stats

        if not self._graph_store:
            logger.warning("Neo4j not available. Graph building skipped.")
            return stats

        use_window = window_start is not None and window_end is not None

        def _fetch() -> list[dict]:
            if use_window:
                docs = self._pg_store.get_documents_for_replay("nlp_done", window_start, window_end, limit=batch_size, country=_country)
                if not docs:
                    docs = self._pg_store.get_documents_for_replay("embedded", window_start, window_end, limit=batch_size, country=_country)
            else:
                docs = self._pg_store.get_documents_by_status("nlp_done", limit=batch_size, country=_country)
                if not docs:
                    docs = self._pg_store.get_documents_by_status("embedded", limit=batch_size, country=_country)
            return docs

        docs = _fetch()
        while docs:
            for doc in docs:
                doc_id = doc["id"]
                try:
                    pg_entities = self._pg_store.get_entities_for_document(doc_id)
                    if not pg_entities:
                        self._pg_store.update_document_status(doc_id, "graph_built")
                        stats["docs_processed"] += 1
                        continue

                    nodes, edges = self._graph_builder.build_from_pg_entities(
                        pg_entities, dict(doc)
                    )
                    stats["nodes_built"] += len(nodes)
                    stats["edges_built"] += len(edges)
                    stats["docs_processed"] += 1
                    self._pg_store.update_document_status(doc_id, "graph_built")

                except Exception as e:
                    logger.warning(f"Graph build failed for doc {doc_id}: {e}")

            if not use_window:
                break
            docs = _fetch()

        stats["duration_sec"] = round(time.time() - start, 2)
        self._log_run("graph", stats)
        logger.info(f"Graph building complete: {stats}")
        return stats

    # ----------------------------------------------------------
    # STAGE 5: THEME DETECTION + RANKING
    # ----------------------------------------------------------
    def run_themes(self, as_of_date=None, country: str = None) -> dict:
        """Detect, rank, and map beneficiaries for all investment themes.

        Args:
            as_of_date: date or datetime used as the lookback ceiling.
                        When None, defaults to today (live mode).
                        In replay mode, pass the replay_date so all window
                        queries use the simulated "current" date and the
                        theme snapshot is stamped with that date.
            country:    ISO-2 country code (e.g. "IN", "US"). When provided
                        this takes priority over config["market"]["country"],
                        so the UI-selected country is always respected
                        regardless of what is written in settings.yaml.
        """
        if not self._theme_detector:
            self._init_themes()

        start = time.time()
        # Explicit parameter wins; fall back to config only as last resort.
        _country = country or self.config.get("market", {}).get("country", "US")
        stats = {"themes_detected": 0, "themes_ranked": 0, "beneficiaries_mapped": 0}

        if not self._pg_store:
            logger.warning("PGStore not initialized. Theme detection skipped.")
            return stats

        from datetime import timedelta as _td, date as _date, datetime as _datetime
        _as_of = as_of_date
        if _as_of is None:
            _as_of = _date.today()
        if isinstance(_as_of, _datetime):
            _as_of = _as_of.date()

        # ── Resolve effective lookback floor ─────────────────────────────────
        # Use config signal_window_days (default 365).
        # In replay mode (as_of_date explicitly set to a past date), we extend
        # the floor to the actual signal start date so nothing is silently skipped.
        # In live mode (as_of_date == today), we respect signal_window_days strictly
        # so themes reflect only recent activity and don't mix all historical years.
        _window_days = self.config.get("themes", {}).get("signal_window_days", 365)
        _lookback = _as_of - _td(days=_window_days)

        from datetime import date as _today_cls
        _is_replay = (as_of_date is not None) and (_as_of < _today_cls.today() - _td(days=7))

        # Auto-extend lookback only during historical replay — NOT in live mode.
        # In live mode the window is always capped at signal_window_days so that
        # themes for 2023, 2024, 2025 are distinct, not all blended together.
        if _is_replay:
            try:
                with self._pg_store._conn() as _rng_conn:
                    with _rng_conn.cursor() as _rng_cur:
                        _rng_cur.execute(
                            "SELECT MIN(filed_at) FROM mg_signals WHERE filed_at IS NOT NULL AND country = %s",
                            (_country,)
                        )
                        _rng_row = _rng_cur.fetchone()
                        if _rng_row and _rng_row[0]:
                            _data_min = (
                                _rng_row[0] if isinstance(_rng_row[0], _date) else _rng_row[0].date()
                            )
                            if _data_min < _lookback:
                                logger.warning(
                                    f"[Replay] Signal data starts {_data_min}, extending lookback from {_lookback}."
                                )
                                _lookback = _data_min
            except Exception as _rng_e:
                logger.debug(f"Signal range pre-check failed (non-fatal): {_rng_e}")

        logger.info(f"Theme detection as_of={_as_of}, lookback_from={_lookback}")

        # ── Sub-phase timing dict ───────────────────────────────────────────
        _t = {}

        ALL_SIGNAL_TYPES = [
            "capex_increase", "capex_decrease",
            "demand_surge", "demand_slowdown",
            "supply_bottleneck", "supply_easing",
            "technology_adoption", "technology_disruption",
            "strategic_pivot", "partnership_formed",
            "acquisition_intent", "market_entry",
            "regulatory_tailwind", "regulatory_headwind",
            "hiring_surge", "inventory_buildup", "inventory_drawdown",
        ]

        # ── Path A: Raw signals WITHOUT entity join (~15K rows)
        # Used by: seed theme detection + beneficiary mapper.
        # get_signals_in_window returns plain signal rows (no de/entities join).
        _t0 = time.time()
        try:
            signal_records = self._pg_store.get_all_signals_in_window(
                ALL_SIGNAL_TYPES, _lookback, _as_of, country=_country
            )
        except Exception as e:
            logger.warning(f"Bulk signal load failed ({e}), falling back to per-type queries")
            signal_records = []
            for stype in ALL_SIGNAL_TYPES:
                try:
                    signal_records.extend(
                        self._pg_store.get_signals_in_window(stype, _lookback, _as_of)
                    )
                except Exception as e2:
                    logger.warning(f"Signal load failed for {stype}: {e2}")
        _t["signal_load"] = round(time.time() - _t0, 2)
        logger.info(f"Loaded {len(signal_records)} raw signal rows")

        # ── Path B: Pre-aggregated entity clusters (~300 rows, replaces 600K+)
        # Used by: auto theme detection (detect_from_clusters_agg).
        # All grouping + counting is done in the DB — Python gets one row per entity.
        _t0 = time.time()
        cluster_rows = []
        try:
            cluster_rows = self._pg_store.get_entity_signal_clusters_in_window(
                ALL_SIGNAL_TYPES, _lookback, _as_of, country=_country
            )
        except Exception as e:
            logger.warning(f"Cluster aggregation query failed ({e}); auto-detection will use raw signals")
        _t["cluster_load"] = round(time.time() - _t0, 2)
        logger.info(f"Loaded {len(cluster_rows)} pre-aggregated entity cluster rows")

        # ── Entity records: needed for seed keyword matching only ─────────────
        # Use same extended window so entities from historical data are included.
        _t0 = time.time()
        _entity_days = (_as_of - _lookback).days + 1  # covers full signal window
        entity_records = self._load_recent_entities(days=_entity_days, as_of_date=_as_of, country=_country)
        _t["entity_load"] = round(time.time() - _t0, 2)

        # ── Active causal-chain entity keywords (used for causal-chain boost) ──
        # Fetch lightweight list of active causal chains and extract the keywords
        # that appear in their names / terminal effects.  Any auto-detected theme
        # whose entity matches one of these keywords gets a +15 boost — the chain
        # is structural evidence that the entity is a real supply bottleneck.
        causal_chain_entities: frozenset = frozenset()
        if self._pg_store:
            try:
                active_chains = self._pg_store.get_active_causal_chains(min_score=10.0)
                kw_set: set[str] = set()
                for ch in active_chains:
                    for field_val in (ch.get("chain_name", ""), ch.get("terminal_effect", "")):
                        if field_val:
                            # Split on common delimiters; lower-case each token ≥ 3 chars
                            for tok in re.split(r"[\s\-→>,/|]+", field_val.lower()):
                                if len(tok) >= 3:
                                    kw_set.add(tok)
                causal_chain_entities = frozenset(kw_set)
                if causal_chain_entities:
                    logger.info(
                        f"Causal-chain boost: {len(active_chains)} active chains → "
                        f"{len(causal_chain_entities)} entity keywords"
                    )
            except Exception as _cc_err:
                logger.debug(f"Could not load active causal chains: {_cc_err}")

        # ── Detect themes (split paths) ──────────────────────────────────────
        _t0 = time.time()
        # Seed-based: raw signals + entities
        seed_themes = self._theme_detector.detect_from_signals(signal_records, entity_records)
        # Auto-clustering: pre-aggregated clusters (fast path) or raw fallback
        if cluster_rows:
            auto_themes = self._theme_detector.detect_from_clusters_agg(
                cluster_rows, causal_chain_entities=causal_chain_entities
            )
        else:
            auto_themes = self._theme_detector.detect_from_signal_clusters(signal_records, entity_records)
        graph_themes = self._theme_detector.detect_from_graph(self._graph_store)

        # 4th detection path: BOTTLENECK THEMES — constraint-keyword scan
        # Finds "HBM Shortage", "Transformer Bottleneck", "Advanced Packaging Backlog"
        # directly from management language in signal context texts.
        # This is the highest-conviction detection path: management explicitly saying
        # "shortage", "sold out", "lead time extended" across multiple companies.
        bottleneck_themes: list = []
        try:
            bottleneck_themes = self._theme_detector.detect_bottleneck_themes(
                self._pg_store, as_of_date=_as_of, lookback_days=365,
                min_constraint_signals=3, min_companies=3,
                country=_country,
            )
        except Exception as e:
            logger.warning(f"Bottleneck detection failed: {e}")

        # 5th detection path: DOWNSTREAM CONSTRAINT THEMES (picks-and-shovels)
        # Finds "Memory because of AI demand" via document co-occurrence +
        # industry adjacency + path score gates.
        downstream_themes: list = []
        try:
            downstream_themes = self._theme_detector.detect_downstream_constraint_themes(
                self._pg_store, as_of_date=_as_of, lookback_days=365,
                country=_country,
            )
        except Exception as e:
            logger.warning(f"Downstream-constraint detection failed: {e}")

        all_themes = self._theme_detector.merge_themes(
            [seed_themes, auto_themes, graph_themes, bottleneck_themes, downstream_themes]
        )
        # Stamp every detected theme with the active market country so the DB
        # row is correctly tagged regardless of the InvestmentTheme default.
        for _theme in all_themes:
            _theme.country = _country
        stats["themes_detected"] = len(all_themes)
        _t["detect"] = round(time.time() - _t0, 2)

        # ── Canonicalization: cluster similar themes → parent/subtheme hierarchy ──
        # Groups semantically similar themes (e.g. "AI power shortage" + "Hyperscale
        # grid constraints" + "AI datacenter electricity demand") under one canonical
        # parent ("AI Infrastructure Power Constraint").  Updates is_canonical,
        # canonical_name, aliases, parent_theme_slug on each InvestmentTheme object.
        _t0 = time.time()
        if self._theme_canonicalizer and len(all_themes) >= 2:
            try:
                # Wire embedding engine in case it was initialized after _init_themes
                if self._theme_canonicalizer._emb is None and self._embedding_engine:
                    self._theme_canonicalizer._emb = self._embedding_engine
                if self._theme_canonicalizer._llm is None and self._llm_reasoner:
                    self._theme_canonicalizer._llm = self._llm_reasoner
                all_themes = self._theme_canonicalizer.canonicalize(all_themes)
                n_canonical = sum(1 for t in all_themes if getattr(t, "is_canonical", True))
                n_sub = len(all_themes) - n_canonical
                logger.info(f"Canonicalization: {n_canonical} parent themes, {n_sub} subthemes")
                stats["canonical_themes"] = n_canonical
                stats["subthemes"] = n_sub
            except Exception as e:
                logger.warning(f"Theme canonicalization failed (non-fatal): {e}")
        _t["canonicalize"] = round(time.time() - _t0, 2)

        # Load evolution metrics
        _t0 = time.time()
        if self._evolution_tracker:
            self._evolution_tracker.load_from_pg(days=90, as_of_date=_as_of)
        _t["evolution_load"] = round(time.time() - _t0, 2)

        # Rank themes
        _t0 = time.time()
        evolution_data = {}
        if self._evolution_tracker:
            for t in all_themes:
                ev = self._evolution_tracker.compute_theme_momentum(
                    t.theme_slug, as_of_date=_as_of
                )
                if ev:
                    evolution_data[t.theme_slug] = ev

        ranked = self._theme_ranker.rank(all_themes, evolution_data, self._pg_store, as_of_date=_as_of)
        stats["themes_ranked"] = len(ranked)
        _t["rank"] = round(time.time() - _t0, 2)

        # Persist themes and get IDs — single transaction batch
        _t0 = time.time()
        # Inject persistence scores into each theme's metadata BEFORE to_dict()
        # so the UI can surface the persistence multiplier and confirmed-quarter count.
        for rt in ranked:
            if rt.theme.metadata is None:
                rt.theme.metadata = {}
            rt.theme.metadata["persistence_multiplier"] = rt.persistence_multiplier
            rt.theme.metadata["confirmed_quarters"]     = rt.confirmed_quarters
            rt.theme.metadata["eligibility_score"]      = rt.eligibility_score

        theme_dicts = [rt.theme.to_dict() for rt in ranked]
        snapshot_dicts = [
            {
                "theme_slug": rt.theme.theme_slug,
                "snapshot_date": _as_of,
                "strength_score": rt.composite_score,
                "momentum_score": rt.momentum_score,
                "doc_count": rt.theme.doc_count,
                "company_count": rt.theme.company_count,
            }
            for rt in ranked
        ]
        try:
            theme_id_map = self._pg_store.batch_upsert_themes_and_snapshots(
                theme_dicts, snapshot_dicts
            )
        except Exception as e:
            logger.warning(f"Batch theme persist failed ({e}), falling back to per-theme")
            theme_id_map = {}
            for rt in ranked:
                theme_id = self._pg_store.upsert_theme(rt.theme.to_dict())
                if theme_id:
                    theme_id_map[rt.theme.theme_slug] = theme_id
                    self._pg_store.snapshot_theme(theme_id, {
                        "snapshot_date": _as_of,
                        "strength_score": rt.composite_score,
                        "momentum_score": rt.momentum_score,
                        "doc_count": rt.theme.doc_count,
                        "company_count": rt.theme.company_count,
                    })
        stats["themes_snapped"] = len(theme_id_map)
        _t["persist_themes"] = round(time.time() - _t0, 2)

        # ── Persist theme hierarchy to Neo4j (SUB_THEME_OF relationships) ──────
        # Writes (:Theme)-[:SUB_THEME_OF]->(:Theme) for every subtheme.
        # Node structure:  Company → mentions → Evidence → supports →
        #                  Subtheme → SUB_THEME_OF → ParentTheme
        _t0 = time.time()
        neo4j_rels = 0
        if self._graph_store:
            try:
                neo4j_rels = self._graph_store.persist_theme_hierarchy(theme_dicts)
                logger.info(f"Neo4j theme hierarchy: {neo4j_rels} SUB_THEME_OF relationships written")
            except Exception as e:
                logger.debug(f"Neo4j theme hierarchy persist failed (non-fatal): {e}")
        stats["neo4j_theme_rels"] = neo4j_rels
        _t["neo4j_hierarchy"] = round(time.time() - _t0, 2)

        # Map beneficiaries
        _t0 = time.time()
        theme_objects = [rt.theme for rt in ranked]
        beneficiary_results = self._beneficiary_mapper.map_all_themes(
            themes=theme_objects,
            signal_records=signal_records,
            entity_records=entity_records,
            seed_themes=None,
            graph_store=self._graph_store,
            as_of_date=_as_of,   # pass replay date so beneficiary dates are historically correct
        )
        _t["beneficiary_map"] = round(time.time() - _t0, 2)

        _t0 = time.time()
        self._beneficiary_mapper.persist(beneficiary_results, self._pg_store, theme_id_map)
        stats["beneficiaries_mapped"] = sum(len(r.all_beneficiaries) for r in beneficiary_results)

        # ── Sync company_count on mg_themes to match actual persisted beneficiaries ──
        # The theme detector sets company_count from global supply-chain models (can be
        # 50–300 for US data), but for India (or any country) we want the count of
        # ACTUAL mapped beneficiaries so the breadth penalty and UI display are accurate.
        if self._pg_store and theme_id_map:
            try:
                self._pg_store.sync_theme_company_counts(list(theme_id_map.values()))
                logger.debug(f"Synced company_count for {len(theme_id_map)} themes")
            except Exception as _cc_err:
                logger.warning(f"company_count sync failed (non-fatal): {_cc_err}")

        _t["beneficiary_persist"] = round(time.time() - _t0, 2)

        # ── Beneficiary Validation ────────────────────────────────────────────
        # Reject (mark inactive / log warning) any ranked theme that ended up
        # with fewer than 2 beneficiaries with relevance_score ≥ min_relevance.
        # A theme with zero actionable beneficiaries is not investable.
        MIN_STRONG_BENS = 2
        MIN_BEN_RELEVANCE = self.config.get("themes", {}).get("min_relevance_score", 15.0)
        ben_map = {r.theme_slug: r for r in beneficiary_results}
        themes_below_ben_threshold = []
        for rt in ranked:
            slug = rt.theme.theme_slug
            bres = ben_map.get(slug)
            if bres is None:
                continue
            strong_bens = [
                b for b in bres.all_beneficiaries
                if (b.relevance_score or 0) >= MIN_BEN_RELEVANCE
            ]
            if len(strong_bens) < MIN_STRONG_BENS:
                themes_below_ben_threshold.append(slug)
                logger.debug(
                    f"Beneficiary validation: '{slug}' has only {len(strong_bens)} "
                    f"strong beneficiaries (need ≥{MIN_STRONG_BENS}) — "
                    f"theme remains but flagged in metadata"
                )
                # Flag in metadata rather than delete — UI can show a warning badge
                rt.theme.metadata = rt.theme.metadata or {}
                rt.theme.metadata["beneficiary_warning"] = True
                rt.theme.metadata["strong_beneficiary_count"] = len(strong_bens)

        if themes_below_ben_threshold:
            logger.info(
                f"Beneficiary validation: {len(themes_below_ben_threshold)} themes "
                f"flagged with <{MIN_STRONG_BENS} strong beneficiaries: "
                + ", ".join(themes_below_ben_threshold[:5])
            )

        # Log sub-phase breakdown for diagnostics
        logger.info(
            f"Themes sub-phase timing: "
            + "  |  ".join(f"{k}={v}s" for k, v in _t.items())
            + f"  |  signals={len(signal_records)}  entities={len(entity_records)}"
        )

        # Print ranking table
        if ranked:
            logger.info("\n" + self._theme_ranker.format_ranking_table(ranked))

        stats["duration_sec"] = round(time.time() - start, 2)
        self._log_run("themes", stats)
        logger.info(f"Theme detection complete: {stats}")
        return stats

    # ----------------------------------------------------------
    # STAGE 6: SELECTIVE LLM ENRICHMENT
    # ----------------------------------------------------------
    def run_llm_enrichment(self, country: str = None) -> dict:
        """Generate LLM hypotheses for confirmed/developing themes.

        Args:
            country: ISO-2 country code. Overrides config["market"]["country"] so the
                     UI-selected country is always respected.
        """
        if not self._llm_reasoner:
            self._init_llm()

        start = time.time()
        _country = country or self.config.get("market", {}).get("country", "US")
        stats = {"hypotheses_generated": 0}

        if not self._pg_store or not self._llm_reasoner.enabled:
            logger.info("LLM disabled or PGStore unavailable. Skipping enrichment.")
            return stats

        themes = self._pg_store.get_active_themes(min_strength=40.0, country=_country)
        theme_id_map = {t["theme_slug"]: t["id"] for t in themes}

        hypotheses = self._llm_reasoner.enrich_themes_batch(
            themes=themes,
            beneficiary_map={},
            signal_summary_map={},
            pg_store=self._pg_store,
            theme_id_map=theme_id_map,
        )

        for slug, hypothesis in hypotheses.items():
            theme_id = theme_id_map.get(slug)
            if theme_id:
                self._pg_store.upsert_theme({
                    "theme_slug": slug,
                    "theme_name": next((t["theme_name"] for t in themes if t["theme_slug"] == slug), slug),
                    "hypothesis_text": hypothesis,
                })
                stats["hypotheses_generated"] += 1

        logger.info(f"LLM budget: {self._llm_reasoner.budget_status}")
        stats["duration_sec"] = round(time.time() - start, 2)
        self._log_run("llm", stats)
        return stats

    # ----------------------------------------------------------
    # FULL PIPELINE
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    # STAGE: GRAPHITI TEMPORAL INGEST
    # ----------------------------------------------------------
    def run_graphiti_ingest(self, batch_size: int = 20) -> dict:
        """Ingest graph-built documents as Graphiti bi-temporal episodes."""
        stats = {"episodes_added": 0, "skipped": 0}

        if not self._graphiti_store or not self._graphiti_store.is_available:
            logger.info("Graphiti not available. Temporal ingest skipped.")
            return stats
        if not self._pg_store:
            return stats

        docs = self._pg_store.get_documents_by_status("graph_built", limit=batch_size)
        for doc in docs:
            local_path = doc.get("local_path", "")
            if not local_path or not Path(local_path).exists():
                stats["skipped"] += 1
                continue
            try:
                from ..parser.pdf_parser import PDFParser
                parser = PDFParser(self.config.get("parser", {}))
                result = parser.parse(Path(local_path))
                if not result.success or not result.text:
                    stats["skipped"] += 1
                    continue

                from datetime import date as _date
                filed_at = doc.get("filed_at")
                if isinstance(filed_at, str):
                    from datetime import datetime as _dt
                    filed_at = _dt.strptime(filed_at, "%Y-%m-%d").date()

                self._graphiti_store.add_document_episode(
                    doc_id=doc["id"],
                    text=result.text,
                    company=doc.get("company", ""),
                    filed_at=filed_at,
                    filing_type=doc.get("filing_type", ""),
                    source_description=f"{doc.get('doc_type','')} - {doc.get('company','')}",
                )
                self._pg_store.update_document_status(doc["id"], "graphiti_done")
                stats["episodes_added"] += 1
            except Exception as e:
                logger.warning(f"Graphiti episode failed for doc {doc['id']}: {e}")
                stats["skipped"] += 1

        logger.info(f"Graphiti ingest: {stats}")
        return stats

    # ----------------------------------------------------------
    # STAGE: BERTREND ACCELERATION ANALYSIS
    # ----------------------------------------------------------
    def run_bertrend(self) -> dict:
        """Run BERTrend trend acceleration on the latest BERTopic results."""
        if not self._bertrend or not self._topic_modeler:
            self._init_themes()

        stats = {"topics_analyzed": 0, "emerging_topics": 0}

        if not self._pg_store:
            return stats

        # Load documents + timestamps for trend analysis
        try:
            with self._pg_store._conn() as conn:
                from psycopg2.extras import RealDictCursor
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT id, filed_at, local_path FROM mg_documents
                        WHERE processing_status IN ('embedded','graph_built','graphiti_done')
                        AND filed_at IS NOT NULL
                        ORDER BY filed_at DESC LIMIT 500
                    """)
                    doc_rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"BERTrend doc load failed: {e}")
            return stats

        if not doc_rows:
            return stats

        # Load text and timestamps
        from ..parser.pdf_parser import PDFParser
        from datetime import date as _date
        parser = PDFParser(self.config.get("parser", {}))
        texts, timestamps = [], []
        for row in doc_rows:
            local_path = row.get("local_path", "")
            if not local_path or not Path(local_path).exists():
                continue
            try:
                result = parser.parse(Path(local_path))
                if result.success and result.text:
                    texts.append(result.text[:2000])
                    filed = row["filed_at"]
                    timestamps.append(filed if isinstance(filed, _date) else _date.today())
            except Exception:
                continue

        if len(texts) < 10:
            logger.info("Not enough documents for BERTrend analysis.")
            return stats

        # Fit BERTopic and get topic assignments
        topic_results = self._topic_modeler.fit(texts)
        if not topic_results:
            return stats

        assignments = self._topic_modeler.transform(texts)
        topic_labels = {t.topic_id: t.label for t in topic_results}
        topic_words = {t.topic_id: t.top_words for t in topic_results}

        trends = self._bertrend.analyze(
            documents=texts,
            timestamps=timestamps,
            topic_assignments=assignments,
            topic_labels=topic_labels,
            topic_words=topic_words,
        )

        stats["topics_analyzed"] = len(trends)
        emerging = self._bertrend.get_emerging_trends(trends)
        stats["emerging_topics"] = len(emerging)

        if trends:
            logger.info(self._bertrend.format_report(trends))

        # Feed emerging BERTrend topics into theme detector
        if emerging and self._theme_detector:
            # Convert emerging trends to TopicResult-like objects for ThemeDetector
            class _FakeTopic:
                def __init__(self, t):
                    self.topic_id = t.topic_id
                    self.label = t.label
                    self.top_words = (
                        t.time_series[-1].top_words if t.time_series else []
                    )
                    self.doc_count = (
                        t.time_series[-1].doc_count if t.time_series else 0
                    )
                    self.is_emerging = True

            fake_topics = [_FakeTopic(t) for t in emerging]
            bertrend_themes = self._theme_detector.detect_from_topics(fake_topics)
            for theme in bertrend_themes:
                if self._pg_store:
                    self._pg_store.upsert_theme(theme.to_dict())
            stats["bertrend_themes_added"] = len(bertrend_themes)

        self._log_run("bertrend", stats)
        return stats

    # ----------------------------------------------------------
    # STAGE: GRAPHRAG REASONING
    # ----------------------------------------------------------
    def run_graph_rag(self, theme_slugs: list[str] = None) -> dict:
        """Run GraphRAG analysis for active themes and cross-theme opportunities."""
        if not self._graph_rag:
            self._init_llm()

        stats = {"themes_analyzed": 0, "answers_generated": 0}

        if not self._llm_reasoner or not self._llm_reasoner.enabled:
            logger.info("LLM disabled. GraphRAG skipped.")
            return stats

        # Use configured themes or load from DB
        slugs = theme_slugs
        if not slugs and self._pg_store:
            active = self._pg_store.get_active_themes(min_strength=40.0)
            slugs = [t["theme_slug"] for t in active[:10]]

        if slugs:
            answers = self._graph_rag.batch_theme_analysis(slugs)
            stats["themes_analyzed"] = len(slugs)
            stats["answers_generated"] = sum(1 for a in answers if a.answer)

            for answer in answers:
                logger.info(
                    f"\nGraphRAG [{answer.query}]:\n"
                    f"{answer.answer[:500]}..."
                    if len(answer.answer) > 500 else
                    f"\nGraphRAG [{answer.query}]:\n{answer.answer}"
                )

        # Cross-theme opportunity analysis
        cross_answer = self._graph_rag.find_cross_theme_opportunities(min_sectors=3)
        if cross_answer.answer:
            stats["cross_theme_answer"] = cross_answer.answer[:300]

        self._log_run("graph_rag", stats)
        return stats

    def run_full(self, since: Optional[datetime] = None, country: str = None) -> dict:
        """Run all pipeline stages end-to-end.

        The ingest stage is dispatched based on market.country:
          - "IN"  → run_ingest_india()  (NSE/BSE/Screener — company filings only)
                    India macro/policy (PIB/SEBI/RBI/InvestIndia/Commerce) are in run_macro()
          - other → run_ingest()        (SEC EDGAR — US default, unchanged)

        All downstream stages (NLP, graph, themes, macro …) are country-agnostic
        and run identically for both markets.

        Args:
            country: ISO-2 country code. Overrides config["market"]["country"] so the
                     UI-selected country is always respected regardless of settings.yaml.
        """
        self._init_storage()
        self._init_nlp()
        self._init_graph_builder()
        self._init_themes()
        self._init_llm()

        _country = country or self.config.get("market", {}).get("country", "US")
        _ingest_fn = (
            (lambda: self.run_ingest_india(since))
            if _country == "IN"
            else (lambda: self.run_ingest(since))
        )

        # Build stage list — pass _country explicitly to every stage that accepts it
        # so the UI-selected country propagates end-to-end without reading settings.yaml.
        _stages = [
            ("ingest",         _ingest_fn,                        {}),
            ("nlp",            self.run_nlp,                      {"country": _country}),
            ("embeddings",     self.run_embeddings,               {"country": _country}),
            ("graph",          self.run_graph,                    {"country": _country}),
            ("graphiti",       self.run_graphiti_ingest,          {}),
            ("bertrend",       self.run_bertrend,                 {}),
            ("themes",         self.run_themes,                   {"country": _country}),
            ("contradictions", self.run_contradictions,           {}),
            ("macro",          self.run_macro,                    {}),
            ("graph_rag",      self.run_graph_rag,                {}),
            ("llm",            self.run_llm_enrichment,           {"country": _country}),
        ]

        all_stats = {}
        for stage, fn, kwargs in _stages:
            logger.info(f"\n{'='*60}\nPipeline Stage: {stage.upper()}\n{'='*60}")
            try:
                stage_stats = fn(**kwargs)
                all_stats[stage] = stage_stats
            except Exception as e:
                logger.error(f"Stage '{stage}' failed: {e}")
                all_stats[stage] = {"error": str(e)}

        return all_stats

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------
    def _load_recent_entities(self, days: int = 90, as_of_date=None, country: str = None) -> list[dict]:
        """Load recently seen entities from PostgreSQL.

        Args:
            as_of_date: upper bound date (defaults to today). In replay mode
                        this is the replay_date so NOW() is never used.
            country: if provided, restricts entities to those extracted from
                     documents tagged with this country (e.g. 'IN' for India).
                     Prevents US entities from polluting India theme beneficiary runs.
        """
        if not self._pg_store:
            return []
        from datetime import date as _date, timedelta as _td, datetime as _dt
        _ceil = as_of_date or _date.today()
        if isinstance(_ceil, _dt):
            _ceil = _ceil.date()
        _floor = _ceil - _td(days=days)
        try:
            return self._pg_store.get_entities_in_window(_floor, _ceil, country=country)
        except Exception as e:
            logger.warning(f"Failed to load recent entities: {e}")
            return []

    def _log_run(self, stage: str, stats: dict):
        """Log pipeline run stats to PostgreSQL."""
        if self._pg_store:
            try:
                self._pg_store.log_pipeline_run({
                    "run_date": date.today(),
                    "stage": stage,
                    "docs_processed": stats.get("docs_processed", stats.get("docs_stored", 0)),
                    "entities_found": stats.get("entities_found", 0),
                    "signals_found": stats.get("signals_found", 0),
                    "themes_updated": stats.get("themes_ranked", 0),
                    "duration_sec": stats.get("duration_sec", 0),
                    "status": "error" if "error" in stats else "ok",
                    "error_message": stats.get("error"),
                })
            except Exception:
                pass

    # ----------------------------------------------------------
    # STAGE: EVENTS (event-centric extraction)
    # ----------------------------------------------------------
    def run_events(self, batch_size: int = 500, window_start=None, window_end=None, country: str = None) -> dict:
        """Extract business events from NLP-processed documents.

        If window_start/window_end are given, processes ALL docs in that date range.
        Otherwise processes one batch of batch_size.

        Args:
            country: ISO-2 country code. Overrides config["market"]["country"] so the
                     UI-selected country is always respected.
        """
        if not self._pg_store or not self._event_extractor:
            return {"skipped": True, "reason": "pg_store or event_extractor not initialised"}

        start = time.time()
        events_stored = 0
        docs_processed = 0
        project_root = Path(__file__).parent.parent.parent.parent
        use_window = window_start is not None and window_end is not None
        _country = country or self.config.get("market", {}).get("country", "US")

        def _fetch() -> list[dict]:
            if use_window:
                return self._pg_store.get_documents_for_replay(
                    "nlp_done", window_start, window_end, limit=batch_size, country=_country
                )
            return self._pg_store.get_documents_by_status("nlp_done", limit=batch_size, country=_country)

        docs = _fetch()
        while docs:
            for doc in docs:
                try:
                    local_path = doc.get("local_path", "")
                    if not local_path:
                        continue
                    full_path = Path(local_path) if Path(local_path).is_absolute() else project_root / local_path
                    if not full_path.exists():
                        continue

                    raw = full_path.read_text(encoding="utf-8", errors="ignore")
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(raw, "lxml")
                        text = soup.get_text(separator=" ", strip=True)
                    except Exception:
                        text = raw

                    events = self._event_extractor.extract(
                        text=text,
                        document_id=doc["id"],
                        company=doc.get("company", ""),
                        filed_at=doc.get("filed_at"),
                    )

                    for ev in events:
                        try:
                            self._pg_store.insert_event({
                                "document_id": ev.document_id,
                                "event_type": ev.event_type.value,
                                "subject_entity": ev.subject_entity,
                                "subject_type": ev.subject_type.value if hasattr(ev.subject_type, 'value') else str(ev.subject_type),
                                "description": ev.description,
                                "magnitude": ev.magnitude,
                                "magnitude_unit": ev.magnitude_unit,
                                "direction": ev.direction,
                                "confidence": ev.confidence,
                                "second_order_entities": ev.second_order_entities,
                                "context_text": ev.context_text,
                                "filed_at": ev.filed_at,
                            })
                            events_stored += 1
                        except Exception as e:
                            logger.debug(f"Event insert failed: {e}")

                    docs_processed += 1
                except Exception as e:
                    logger.warning(f"Event extraction failed for doc {doc.get('id')}: {e}")

            if not use_window:
                break
            docs = _fetch()

        stats = {
            "docs_processed": docs_processed,
            "events_extracted": events_stored,
            "duration_sec": round(time.time() - start, 2),
        }
        logger.info(f"Event extraction complete: {stats}")
        self._log_run("events", stats)
        return stats

    # ----------------------------------------------------------
    # STAGE: CAUSAL CHAINS
    # ----------------------------------------------------------
    def run_causal_chains(self, as_of_date: date = None) -> dict:
        """Score all causal chains against current active entities and persist.

        Args:
            as_of_date: The historical analysis date (e.g. end of replay window).
                        Stored as both ``first_detected`` (on new chains) and
                        ``last_scored_at`` (on every run) so the UI shows *when*
                        the data was current — not when the pipeline ran.
                        When None, resolved automatically from MAX(filed_at).
        """
        if not self._causal_mapper or not self._pg_store:
            return {"skipped": True, "reason": "causal_mapper or pg_store not initialised"}

        start = time.time()

        # ── Resolve scoring window ceiling ────────────────────────────────────
        # as_of_date (from caller) sets the upper bound for the scoring lookup.
        # When None, default to today so scoring still works even with no data.
        _scoring_ceil = as_of_date if as_of_date is not None else date.today()

        # ── Resolve data_date: from MAX(signals.filed_at) ───────────────────
        # This is stored as last_scored_at — it must reflect WHEN THE SIGNAL
        # DATA IS FROM, never when the pipeline ran.
        # Fallback 1: SEC filings only (10-K/10-Q/8-K) to exclude macro/FRED
        #             records that have near-today filed_at dates.
        # Fallback 2: stay None — persist() handles None safely.
        _data_date: date | None = None
        _date_sources: list[tuple[str, tuple]] = [
            (
                "SELECT MAX(filed_at) FROM mg_signals WHERE filed_at IS NOT NULL",
                (),
            ),
            (
                "SELECT MAX(d.filed_at) FROM mg_documents d "
                "WHERE d.filed_at IS NOT NULL "
                "  AND d.filing_type IN ('10-K','10-Q','8-K','10-K/A','10-Q/A','6-K')",
                (),
            ),
        ]
        for _sql, _params in _date_sources:
            try:
                with self._pg_store._conn() as _conn:
                    with _conn.cursor() as _cur:
                        _cur.execute(_sql, _params)
                        _row = _cur.fetchone()
                        if _row and _row[0]:
                            _candidate = (
                                _row[0] if isinstance(_row[0], date) else _row[0].date()
                            )
                            # Accept data dated up to and including the scoring ceiling.
                            # In historical replay, MAX(filed_at) == scoring_ceil is valid.
                            # Only reject data dated strictly after the ceiling (future).
                            if _candidate <= _scoring_ceil:
                                _data_date = _candidate
                                _src = _sql.split("FROM")[1].strip().split()[0]
                                logger.info(
                                    f"Causal data_date → {_data_date} (source: {_src})"
                                )
                                break
                            else:
                                logger.debug(
                                    f"data_date candidate {_candidate} > scoring_ceil "
                                    f"{_scoring_ceil} — skipping (future-dated data)"
                                )
            except Exception as _e:
                logger.warning(f"data_date query failed: {_e}")

        if _data_date is None:
            # No usable signal data found — store None so the UI shows nothing
            # rather than today's pipeline-run date.
            logger.warning(
                "Causal data_date: no signal data found; last_scored_at will be NULL"
            )

        # Effective as_of for signal QUERIES = ceiling (don't query future data).
        # Effective as_of for STORAGE     = data_date (what evidence is from).
        # When _data_date is None (no signals yet), use _scoring_ceil for queries
        # but pass None to persist() so it stores NULL rather than today's date.
        as_of_date = min(_scoring_ceil, _data_date) if _data_date is not None else _scoring_ceil
        _persist_date = _data_date  # None → persist() stores NULL / skips date stamping
        logger.info(
            f"run_causal_chains: scoring_ceil={_scoring_ceil}, "
            f"data_date={_data_date}, effective_as_of={as_of_date}, "
            f"persist_date={_persist_date}"
        )

        active = self._load_recent_entities(days=365, as_of_date=as_of_date)
        active_entity_names = {r.get("canonical_name", "") for r in active}
        active_signals: list[dict] = []
        try:
            active_signals = self._pg_store.get_all_signals_in_window(
                ["capex_increase", "technology_adoption", "demand_surge", "supply_bottleneck"],
                as_of_date - __import__("datetime").timedelta(days=365),
                as_of_date,
            )
        except Exception:
            for stype in ["capex_increase", "technology_adoption", "demand_surge", "supply_bottleneck"]:
                try:
                    active_signals.extend(
                        self._pg_store.get_signals_in_window(stype, as_of_date - __import__("datetime").timedelta(days=365), as_of_date)
                    )
                except Exception:
                    pass

        # ── Step 1: Auto-discover new chains from actual signal data ─────────
        # This mines demand→supply, demand→capex, and tech→downstream patterns
        # from the signals table and adds them alongside the static library chains.
        try:
            discovered = self._causal_mapper.discover_chains_from_data(
                self._pg_store,
                as_of_date=as_of_date,
                lookback_days=730,
            )
            logger.info(f"Causal auto-discovery: {len(discovered)} new chains found")
        except Exception as e:
            logger.warning(f"Causal auto-discovery failed: {e}")

        # ── Step 2: Score all chains (static library + auto-discovered) ──────
        chains = self._causal_mapper.score_chains(active_entity_names, active_signals)
        persisted = self._causal_mapper.persist(self._pg_store, as_of_date=_persist_date)
        self._causal_mapper.log_results(chains)

        active_chains = [c for c in chains if c.activation_score > 20]
        stats = {
            "chains_scored": len(chains),
            "chains_active": len(active_chains),
            "chains_discovered": len(discovered) if "discovered" in dir() else 0,
            "chains_persisted": persisted,
            "top_chain": chains[0].name if chains else "",
            "top_score": chains[0].activation_score if chains else 0.0,
            "duration_sec": round(time.time() - start, 2),
        }
        logger.info(f"Causal chain scoring complete: {stats}")
        self._log_run("causal", stats)
        return stats

    # ----------------------------------------------------------
    # STAGE: SUPPLY CHAIN
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    # STAGE: CONTRADICTION DETECTION
    # ----------------------------------------------------------
    def run_contradictions(self, lookback_days: int = 365) -> dict:
        """Detect narrative reversals across consecutive quarters for company+theme pairs.

        Algorithm:
          1. Pull signal context_text aggregated by (company, theme-entity, quarter).
          2. For each (company, entity) pair with ≥2 quarters of data, compare
             consecutive quarters using ContradictionDetector.detect().
          3. Persist significant reversals to mg_contradictions.

        Returns stats: {pairs_checked, contradictions_found, contradictions_written}
        """
        if not self._pg_store:
            return {"skipped": True, "reason": "pg_store not initialised"}

        start = time.time()

        # Ensure table exists (idempotent)
        try:
            self._pg_store.ensure_contradictions_table()
        except Exception as e:
            logger.warning(f"Could not create mg_contradictions table: {e}")
            return {"skipped": True, "reason": str(e)}

        from ..intelligence.contradiction_detector import ContradictionDetector
        detector = ContradictionDetector(self.config.get("contradiction", {}))

        # ── Fetch signal snippets grouped by (company, entity, quarter) ───────
        try:
            rows = self._pg_store.get_company_theme_quarter_snippets(
                lookback_days=lookback_days,
                min_signals=2,
            )
        except Exception as e:
            logger.warning(f"Contradiction snippet query failed: {e}")
            return {"skipped": True, "reason": str(e)}

        if not rows:
            logger.info("Contradiction detection: no multi-quarter data yet — skipping")
            return {"pairs_checked": 0, "contradictions_found": 0, "contradictions_written": 0,
                    "duration_sec": round(time.time() - start, 2)}

        # ── Group rows by (company, entity) → sorted list of (quarter, snippets) ─
        from collections import defaultdict
        timeline: dict[tuple, list] = defaultdict(list)
        for row in rows:
            key = (row["company"], row["entity"])
            timeline[key].append((row["quarter"], row.get("snippets") or ""))

        # Sort each timeline chronologically (quarter strings "Q1-2024" sort correctly)
        for key in timeline:
            timeline[key].sort(key=lambda x: x[0])

        # ── Run contradiction detection on consecutive-quarter pairs ──────────
        found: list[dict] = []
        pairs_checked = 0
        for (company, entity), quarters in timeline.items():
            for i in range(len(quarters) - 1):
                from_q, from_text = quarters[i]
                to_q, to_text     = quarters[i + 1]
                pairs_checked += 1
                try:
                    result = detector.detect(
                        company=company,
                        theme=entity,
                        from_quarter=from_q,
                        from_snippets=[from_text],
                        to_quarter=to_q,
                        to_snippets=[to_text],
                    )
                    if result and result.is_significant(detector.sentiment_threshold):
                        found.append(result.to_dict())
                except Exception as e:
                    logger.debug(f"Contradiction check failed {company}/{entity}: {e}")

        # ── Persist to Postgres ───────────────────────────────────────────────
        written = 0
        if found:
            try:
                written = self._pg_store.batch_upsert_contradictions(found)
            except Exception as e:
                logger.warning(f"Contradiction persistence failed: {e}")

        stats = {
            "pairs_checked": pairs_checked,
            "contradictions_found": len(found),
            "contradictions_written": written,
            "duration_sec": round(time.time() - start, 2),
        }
        logger.info(f"Contradiction detection: {stats}")
        self._log_run("contradictions", stats)
        return stats

    def run_supply_chain(self, write_to_neo4j: bool = True) -> dict:
        """Build supply chain maps for all known themes and optionally persist to Neo4j."""
        if not self._supply_chain_analyzer:
            return {"skipped": True, "reason": "supply_chain_analyzer not initialised"}

        start = time.time()
        reports = self._supply_chain_analyzer.get_all_reports()
        neo4j_written = 0

        if write_to_neo4j and self._graph_store:
            from ..ontology.supply_chain import SUPPLY_CHAIN_TEMPLATES
            for slug in SUPPLY_CHAIN_TEMPLATES:
                sc_map = self._supply_chain_analyzer.build_map(slug)
                if sc_map:
                    self._supply_chain_analyzer.write_to_neo4j(sc_map)
                    neo4j_written += len(sc_map.edges)

        stats = {
            "themes_mapped": len(reports),
            "neo4j_edges_written": neo4j_written,
            "duration_sec": round(time.time() - start, 2),
        }
        logger.info(f"Supply chain mapping complete: {stats}")
        self._log_run("supply_chain", stats)
        return stats

    # ----------------------------------------------------------
    # STAGE: MACRO & POLICY DATA LAYER
    # ----------------------------------------------------------
    def _init_macro(self):
        """Initialise macro store, graph, and constraint engine."""
        from ..macro.macro_store import MacroStore
        from ..macro.macro_graph import MacroGraphStore
        from ..macro.constraint_engine import ConstraintEngine

        pg_cfg = self.config.get("postgresql", {})
        if pg_cfg.get("host"):
            self._macro_store = MacroStore(pg_cfg)
            if self._graph_store:
                self._macro_graph = MacroGraphStore(self._graph_store)
            self._constraint_engine = ConstraintEngine(self._macro_store, self._macro_graph)

    def run_macro(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        as_of_date=None,
        country: str = None,
    ) -> dict:
        """Fetch, store, and constraint-score the macro/policy data layer.

        This stage runs AFTER graph construction and theme detection.
        It:
          1. Fetches macro series (FRED, World Bank) and stores to PostgreSQL
          2. Fetches commodity data (EIA) and stores
          3. Fetches policy events (Congress, Federal Register — US) and stores
          4. Updates Neo4j macro nodes (Country, Commodity, Policy, MacroIndicator)
          5. [US] Congress bills + Federal Register notices → mg_policy_events
          6. [IN] PIB / SEBI / RBI / InvestIndia / Commerce/DGFT → mg_policy_events
             with country='IN'  (only when market.country == "IN")
          7. Runs the Constraint Engine to score active themes with macro context
          8. Returns per-source fetch counts and constraint scores

        Args:
            start_date: ISO date string for historical fetch lower bound
            end_date:   ISO date string for upper bound (or replay ceiling)
            as_of_date: date object for constraint engine (replay-safe)
        """
        if not self._macro_store:
            self._init_storage()
            self._init_macro()
        if not self._macro_store:
            return {"skipped": True, "reason": "PostgreSQL not configured"}

        import time as _time
        from datetime import date as _date

        start = _time.time()
        macro_cfg = self.config.get("macro", {})
        stats: dict = {}

        # Derive date range
        _end = end_date or (str(as_of_date) if as_of_date else str(_date.today()))
        _start = start_date or macro_cfg.get("start_date", "2018-01-01")

        # ---- 1. FRED ----
        fred_cfg = {
            **self.config.get("fred", {}),
            "api_delay_seconds": self.config.get("fred", {}).get("api_delay_seconds", 0.6),
            "max_results_per_run": 10000,
            "user_agent": self.config.get("fetcher", {}).get("user_agent", "MakroGraph/0.2"),
            "request_timeout_seconds": 30,
            "retry_attempts": 3,
            "retry_delay_seconds": 2,
            "download_dir": "data/raw",
        }
        fred_rows = 0
        try:
            from ..fetcher.fred_fetcher import FredFetcher
            with FredFetcher(fred_cfg) as ff:
                all_series = ff.fetch_all_series(start_date=_start, end_date=_end)
            flat: list[dict] = []
            for rows in all_series.values():
                flat.extend(rows)
            fred_rows = self._macro_store.upsert_macro_series(flat)
            # Update Neo4j MacroIndicator nodes
            if self._macro_graph:
                self._macro_graph.upsert_macro_indicators_bulk(flat)
        except Exception as e:
            logger.error(f"Macro FRED stage failed: {e}")
        stats["fred_rows"] = fred_rows

        # ---- 2. EIA ----
        eia_cfg = {
            **self.config.get("eia", {}),
            "api_delay_seconds": 0.5,
            "max_results_per_run": 10000,
            "user_agent": self.config.get("fetcher", {}).get("user_agent", "MakroGraph/0.2"),
            "request_timeout_seconds": 30,
            "retry_attempts": 3,
            "retry_delay_seconds": 2,
            "download_dir": "data/raw",
        }
        eia_rows = 0
        try:
            from ..fetcher.eia_fetcher import EiaFetcher
            with EiaFetcher(eia_cfg) as ef:
                all_commodities = ef.fetch_all(start_date=_start, end_date=_end)
            flat_comm: list[dict] = []
            for rows in all_commodities.values():
                flat_comm.extend(rows)
            eia_rows = self._macro_store.upsert_commodity_series(flat_comm)
            if self._macro_graph:
                self._macro_graph.upsert_commodities_bulk(flat_comm)
        except Exception as e:
            logger.error(f"Macro EIA stage failed: {e}")
        stats["eia_rows"] = eia_rows

        # ---- 3. World Bank ----
        wb_cfg = {
            **self.config.get("world_bank", {}),
            "api_delay_seconds": 0.5,
            "max_results_per_run": 50000,
            "user_agent": self.config.get("fetcher", {}).get("user_agent", "MakroGraph/0.2"),
            "request_timeout_seconds": 30,
            "retry_attempts": 2,
            "retry_delay_seconds": 2,
            "download_dir": "data/raw",
            "start_year": int((_start or "2018-01-01")[:4]),
            "end_year": int((_end or str(_date.today()))[:4]),
        }
        wb_rows = 0
        try:
            from ..fetcher.world_bank_fetcher import WorldBankFetcher
            with WorldBankFetcher(wb_cfg) as wf:
                wb_data = wf.fetch_country_indicators()
            wb_rows = self._macro_store.upsert_macro_series(wb_data)
        except Exception as e:
            logger.error(f"Macro World Bank stage failed: {e}")
        stats["world_bank_rows"] = wb_rows

        # ---- 4. Congress ----
        congress_cfg = {
            **self.config.get("congress", {}),
            "api_delay_seconds": 0.5,
            "max_results_per_run": self.config.get("congress", {}).get("max_results", 200),
            "user_agent": self.config.get("fetcher", {}).get("user_agent", "MakroGraph/0.2"),
            "request_timeout_seconds": 30,
            "retry_attempts": 3,
            "retry_delay_seconds": 2,
            "download_dir": "data/raw",
            "start_date": _start,
            "end_date": _end,
        }
        congress_events = 0
        try:
            from ..fetcher.congress_fetcher import CongressFetcher
            with CongressFetcher(congress_cfg) as cf:
                bills = cf.fetch_bills(start_date=_start, end_date=_end)
            congress_events = self._macro_store.upsert_policy_events(bills)
            if self._macro_graph:
                self._macro_graph.upsert_policies_bulk(bills)
        except Exception as e:
            logger.error(f"Macro Congress stage failed: {e}")
        stats["congress_events"] = congress_events

        # ---- 5. Federal Register ----
        fr_cfg = {
            **self.config.get("federal_register", {}),
            "api_delay_seconds": 0.4,
            "max_results_per_run": self.config.get("federal_register", {}).get("max_results", 200),
            "user_agent": self.config.get("fetcher", {}).get("user_agent", "MakroGraph/0.2"),
            "request_timeout_seconds": 30,
            "retry_attempts": 3,
            "retry_delay_seconds": 2,
            "download_dir": "data/raw",
            "start_date": _start,
            "end_date": _end,
        }
        fr_events = 0
        try:
            from ..fetcher.federal_register_fetcher import FederalRegisterFetcher
            with FederalRegisterFetcher(fr_cfg) as frf:
                fr_docs = frf.fetch_documents(start_date=_start, end_date=_end)
            fr_events = self._macro_store.upsert_policy_events(fr_docs)
            if self._macro_graph:
                self._macro_graph.upsert_policies_bulk(fr_docs)
        except Exception as e:
            logger.error(f"Macro Federal Register stage failed: {e}")
        stats["federal_register_events"] = fr_events

        # ---- 6. India Macro Sources — PIB / SEBI / RBI / InvestIndia / Commerce/DGFT ----
        # Only runs when market.country == "IN".
        # These are "latest-only" policy/regulatory context sources (RSS/web scraping).
        # SourceDocuments are converted to policy event dicts and stored in
        # mg_policy_events with country='IN' — same table as US Congress/Federal Register.
        _country = country or self.config.get("market", {}).get("country", "US")
        india_macro_events = 0
        if _country == "IN":
            from datetime import datetime as _dt, timezone as _tz
            import hashlib as _hashlib

            # Parse _start / _end strings into datetimes for since/until interface
            try:
                _since_dt = _dt.strptime(_start, "%Y-%m-%d").replace(tzinfo=_tz.utc) if _start else None
            except Exception:
                _since_dt = None
            try:
                _until_dt = _dt.strptime(_end, "%Y-%m-%d").replace(tzinfo=_tz.utc) if _end else None
            except Exception:
                _until_dt = None

            _fetcher_base = self.config.get("fetcher", {})
            _base_fetcher_cfg = {
                "download_dir": self.config.get("storage", {}).get("download_dir", "data/raw"),
                "user_agent":   self.config.get("user_agent", "MakroGraph/0.2"),
                "request_timeout_seconds": _fetcher_base.get("request_timeout_seconds", 30),
                "retry_attempts":          _fetcher_base.get("retry_attempts", 3),
                "retry_delay_seconds":     _fetcher_base.get("retry_delay_seconds", 2),
            }

            # (source_key, fetcher_import_path, config_key)
            _india_macro_sources = [
                ("pib_india",      "pib_fetcher",           "PIBFetcher",           "pib"),
                ("sebi_india",     "sebi_fetcher",          "SEBIFetcher",          "sebi"),
                ("rbi_india",      "rbi_fetcher",           "RBIFetcher",           "rbi"),
                ("invest_india",   "invest_india_fetcher",  "InvestIndiaFetcher",   "invest_india"),
                ("commerce_india", "commerce_india_fetcher","CommerceIndiaFetcher", "commerce_india"),
            ]

            for src_key, module_name, class_name, cfg_key in _india_macro_sources:
                src_cfg = self.config.get(cfg_key, {})
                if not src_cfg.get("enabled", True):
                    logger.info(f"India macro (IN) [{src_key}]: disabled in config — skipping")
                    continue
                try:
                    import importlib as _importlib
                    _mod = _importlib.import_module(f"..fetcher.{module_name}", package=__name__)
                    _FC = getattr(_mod, class_name)

                    merged_cfg = {**_base_fetcher_cfg, **src_cfg}
                    with _FC(merged_cfg) as fetcher:
                        source_docs = fetcher.discover(since=_since_dt, until=_until_dt)

                    # Convert SourceDocuments → policy event dicts
                    events_batch: list[dict] = []
                    for doc in source_docs:
                        policy_id = _hashlib.md5(doc.url.encode()).hexdigest()
                        events_batch.append({
                            "policy_id":             policy_id,
                            "source":                f"IN:{src_key}",
                            "policy_type":           doc.filing_type or doc.doc_type or "notice",
                            "title":                 doc.title or "",
                            "description":           "",
                            "status":                "published",
                            "introduced_date":       doc.published_at.date() if doc.published_at else None,
                            "enacted_date":          None,
                            "effective_date":        doc.published_at.date() if doc.published_at else None,
                            "sponsor":               doc.company or src_key,
                            "sectors_affected":      [],
                            "technologies_affected": [],
                            "impact_direction":      "neutral",
                            "impact_magnitude":      0.0,
                            "keywords":              [],
                            "raw_url":               doc.url,
                            "country":               "IN",
                        })

                    stored = self._macro_store.upsert_policy_events(events_batch)
                    india_macro_events += stored
                    logger.info(
                        f"India macro (IN) [{src_key}]: "
                        f"{len(source_docs)} items → {stored} policy events stored"
                    )

                    if self._macro_graph and events_batch:
                        self._macro_graph.upsert_policies_bulk(events_batch)

                except Exception as exc:
                    logger.error(f"India macro (IN) [{src_key}] failed: {exc}")

        stats["india_macro_events"] = india_macro_events

        # ---- 7. Constraint Engine ----
        constraint_results = 0
        if self._constraint_engine and self._pg_store:
            try:
                _as_of = as_of_date
                if isinstance(_as_of, str):
                    from datetime import date as _d
                    _as_of = _d.fromisoformat(_as_of)
                active_themes = self._pg_store.get_active_themes(min_strength=5.0)
                enriched = self._constraint_engine.run(active_themes, as_of_date=_as_of)
                constraint_results = len(enriched)
                logger.info(
                    f"Constraint Engine: enriched {constraint_results} themes with macro context"
                )
            except Exception as e:
                logger.error(f"Constraint Engine failed: {e}")
        stats["themes_constraint_scored"] = constraint_results

        stats["duration_sec"] = round(_time.time() - start, 2)
        logger.info(f"Macro stage complete: {stats}")
        self._log_run("macro", stats)
        return stats

    def search_similar(self, query_text: str, top_k: int = 10) -> list[dict]:
        """Find documents semantically similar to a query."""
        if not self._embedding_engine or not self._vector_store:
            return []
        embedding = self._embedding_engine.embed(query_text)
        if not embedding:
            return []
        return self._vector_store.search_similar_documents(embedding, top_k=top_k)

    def get_theme_report(self) -> str:
        """Return a human-readable summary of current active themes."""
        if not self._pg_store:
            return "No database connection."
        themes = self._pg_store.get_active_themes(min_strength=20.0)
        if not themes:
            return "No active themes detected."

        lines = [f"\n{'='*60}", "ACTIVE INVESTMENT THEMES", f"{'='*60}"]
        for i, t in enumerate(themes[:15], 1):
            lines.append(
                f"\n{i}. {t['theme_name']} [{t['conviction']}]\n"
                f"   Strength: {t['strength_score']:.1f} | "
                f"Docs: {t['doc_count']} | Companies: {t['company_count']}\n"
                f"   Sectors: {', '.join(t.get('sectors') or [])}"
            )
            if t.get("hypothesis_text"):
                lines.append(f"   Hypothesis: {t['hypothesis_text'][:200]}...")
        return "\n".join(lines)

    def close(self):
        for attr in ["_pg_store", "_vector_store", "_graph_store", "_macro_store"]:
            obj = getattr(self, attr)
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
