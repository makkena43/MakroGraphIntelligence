"""Historical Validation & Replay Runner.

Replays the MakroGraph intelligence pipeline month-by-month through a
historical time window to validate whether the system discovers themes
before markets recognize them.

Design principles:
    1. NEVER use datetime.now() during replay — all date arithmetic is
       relative to replay_date (the simulated "current" date).
    2. Each document's REAL filed_at from EDGAR is preserved throughout
       the entire pipeline so concall/earnings data is correctly dated.
    3. The replay_date acts only as a window CEILING for ingest — it does
       not override or backdate any document.
    4. Theme snapshots are stamped with the replay_date so the theme
       score evolution curve is fully reconstructable month-by-month.
    5. After replay completes, forward-return validation can be filled in
       by calling fill_forward_returns(theme_slug, price_data).

Usage:
    runner = HistoricalRunner(
        config=cfg,
        start_date=date(2020, 1, 1),
        end_date=date(2023, 12, 31),
        replay_mode="monthly",
    )
    results = runner.run()

    # Later — fill in actual forward price returns
    runner.fill_forward_returns("ai-infrastructure", price_data_dict)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# -------------------------------------------------------
# MONTHLY TIMELINE GENERATOR
# -------------------------------------------------------

def generate_monthly_timeline(
    start_date: date,
    end_date: date,
) -> list[tuple[date, date]]:
    """Generate a list of (window_start, window_end) pairs, one per month.

    Each window is [first_of_month, last_of_month].
    The replay_date equals window_end.

    Example:
        (2020-01-01, 2020-01-31)
        (2020-02-01, 2020-02-28)
        ...
    """
    import calendar
    windows = []
    current = date(start_date.year, start_date.month, 1)
    end_month = date(end_date.year, end_date.month, 1)

    while current <= end_month:
        _, last_day = calendar.monthrange(current.year, current.month)
        window_start = current
        window_end = date(current.year, current.month, last_day)
        if window_end > end_date:
            window_end = end_date
        windows.append((window_start, window_end))

        # Advance to first of next month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return windows


# -------------------------------------------------------
# MONTHLY RESULT
# -------------------------------------------------------

@dataclass
class MonthlyResult:
    """Stats for a single replay month."""
    replay_batch: str
    replay_date: date
    window_start: date
    window_end: date
    docs_ingested: int = 0
    docs_nlp: int = 0
    nodes_built: int = 0
    edges_built: int = 0
    themes_detected: int = 0
    themes_snapped: int = 0
    events_extracted: int = 0
    causal_score: float = 0.0
    duration_sec: float = 0.0
    status: str = "ok"
    error: str = ""
    theme_ids: dict = field(default_factory=dict)   # slug → id at this batch

    def to_dict(self) -> dict:
        return {
            "replay_batch": self.replay_batch,
            "replay_date": self.replay_date,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "docs_ingested": self.docs_ingested,
            "docs_nlp": self.docs_nlp,
            "nodes_built": self.nodes_built,
            "edges_built": self.edges_built,
            "themes_detected": self.themes_detected,
            "themes_snapped": self.themes_snapped,
            "events_extracted": self.events_extracted,
            "causal_score": self.causal_score,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "error_message": self.error or None,
        }


# -------------------------------------------------------
# HISTORICAL RUNNER
# -------------------------------------------------------

class HistoricalRunner:
    """Month-by-month replay engine for MakroGraph.

    For each month in [start_date, end_date]:
      1. Fetch filings with filed_at in [month_start, month_end]
      2. Run NLP on new docs (preserving real filed_at throughout)
      3. Build knowledge graph from NLP entities
      4. Extract events with event-centric extractor
      5. Detect themes as_of month_end → snapshot with replay_date
      6. Score causal chains against active entities
      7. Log MonthlyResult to mg_replay_runs

    Date guarantee:
        Every query uses [window_start, replay_date] as date bounds.
        datetime.now() / date.today() are NEVER called during a replay step.
        Only the actual filed_at of each SEC document (sourced from EDGAR)
        is used to anchor entities, signals, events, and themes in time.
    """

    def __init__(
        self,
        config: dict,
        start_date: date,
        end_date: date,
        replay_mode: str = "monthly",
        skip_ingest: bool = False,
        skip_neo4j: bool = False,
        skip_nlp: bool = False,
        skip_graph: bool = False,
        skip_events: bool = False,
        skip_causal: bool = False,
        skip_themes: bool = False,
        skip_pdf_fetch: bool = False,
    ):
        self.config = config
        self.start_date = start_date
        self.end_date = end_date
        self.replay_mode = replay_mode
        self.skip_ingest = skip_ingest
        self.skip_neo4j = skip_neo4j
        self.skip_nlp = skip_nlp
        self.skip_graph = skip_graph or skip_neo4j   # skip_neo4j also disables graph
        self.skip_events = skip_events
        self.skip_causal = skip_causal
        self.skip_themes = skip_themes
        self.skip_pdf_fetch = skip_pdf_fetch

        self._pipeline = None
        self._pg_store = None
        self._results: list[MonthlyResult] = []
        # Pre-resolved CIK list for this run — built once in _init_pipeline so
        # all months process the SAME set of companies and the batch offset only
        # advances a single time per user-initiated run (not once per month).
        self._run_cik_list: list[str] = []

    # ----------------------------------------------------------
    # PUBLIC API
    # ----------------------------------------------------------

    def run(self, resume_from: Optional[date] = None) -> list[MonthlyResult]:
        """Run the full historical replay.

        Args:
            resume_from: if set, skip all months before this date (useful
                         for resuming an interrupted replay run).

        Returns:
            list of MonthlyResult, one per replay month.
        """
        self._init_pipeline()
        timeline = generate_monthly_timeline(self.start_date, self.end_date)
        logger.info(
            f"HistoricalRunner: {len(timeline)} months "
            f"[{self.start_date} → {self.end_date}]"
        )

        for window_start, window_end in timeline:
            if resume_from and window_end < resume_from:
                logger.info(f"Skipping {window_end} (before resume_from={resume_from})")
                continue

            result = self._run_month(window_start, window_end)
            self._results.append(result)
            self._log_result(result)

            logger.info(
                f"Month {result.replay_batch}: "
                f"docs={result.docs_ingested} nlp={result.docs_nlp} "
                f"themes={result.themes_detected} "
                f"causal={result.causal_score:.1f} "
                f"[{result.duration_sec:.1f}s] {result.status}"
            )

        self._close()
        logger.info(
            f"HistoricalRunner complete: {len(self._results)} months processed"
        )
        return self._results

    def fill_forward_returns(
        self,
        theme_slug: str,
        price_data: dict,
        benchmark_data: Optional[dict] = None,
    ) -> int:
        """Fill in forward price returns for theme beneficiaries after replay.

        Args:
            theme_slug: e.g. "ai-infrastructure"
            price_data: {ticker: {date_str: close_price}} mapping
            benchmark_data: {date_str: close_price} for benchmark (e.g. SPY)

        Returns:
            Number of rows updated.
        """
        if not self._pg_store:
            self._init_pipeline()

        records = self._pg_store.get_theme_performance(theme_slug)
        updated = 0
        for rec in records:
            ticker = rec["ticker"]
            det_date = rec["detection_date"]
            if isinstance(det_date, str):
                det_date = date.fromisoformat(det_date)

            ticker_prices = price_data.get(ticker, {})
            bm_prices = benchmark_data or {}

            fwd = {
                "forward_30d_return": _calc_return(ticker_prices, det_date, 30),
                "forward_90d_return": _calc_return(ticker_prices, det_date, 90),
                "forward_180d_return": _calc_return(ticker_prices, det_date, 180),
                "forward_365d_return": _calc_return(ticker_prices, det_date, 365),
                "benchmark_30d": _calc_return(bm_prices, det_date, 30),
                "benchmark_90d": _calc_return(bm_prices, det_date, 90),
                "benchmark_180d": _calc_return(bm_prices, det_date, 180),
                "benchmark_365d": _calc_return(bm_prices, det_date, 365),
                "measured_at": date.today(),
            }
            try:
                self._pg_store.upsert_theme_performance({
                    **rec,
                    **fwd,
                    "theme_slug": theme_slug,
                })
                updated += 1
            except Exception as e:
                logger.warning(f"fill_forward_returns failed for {ticker}: {e}")

        logger.info(f"fill_forward_returns: {updated}/{len(records)} rows updated for {theme_slug}")
        return updated

    def print_summary(self):
        """Print a replay summary table."""
        if not self._results:
            print("No results yet.")
            return
        print(f"\n{'='*80}")
        print(f"{'HISTORICAL REPLAY SUMMARY':^80}")
        print(f"{'='*80}")
        print(
            f"{'Batch':<10} {'Ingested':>9} {'NLP':>5} {'Themes':>7} "
            f"{'Snapped':>8} {'Causal':>7} {'Dur(s)':>7} {'Status':<10}"
        )
        print("-" * 80)
        for r in self._results:
            print(
                f"{r.replay_batch:<10} {r.docs_ingested:>9} {r.docs_nlp:>5} "
                f"{r.themes_detected:>7} {r.themes_snapped:>8} "
                f"{r.causal_score:>7.1f} {r.duration_sec:>7.1f} {r.status:<10}"
            )
        print("=" * 80)

    # ----------------------------------------------------------
    # INTERNAL — MONTH PROCESSING
    # ----------------------------------------------------------

    def _run_month(self, window_start: date, window_end: date) -> MonthlyResult:
        """Execute one replay month."""
        replay_batch = window_end.strftime("%Y-%m")
        result = MonthlyResult(
            replay_batch=replay_batch,
            replay_date=window_end,
            window_start=window_start,
            window_end=window_end,
        )
        t0 = time.time()

        try:
            logger.info(f"\n{'='*60}")
            logger.info(f"REPLAY [{replay_batch}]  window: {window_start} → {window_end}")
            logger.info(f"{'='*60}")

            # ---- STAGE 1: INGEST ----------------------------------------
            if not self.skip_ingest:
                ingest_stats = self._ingest_month(window_start, window_end, replay_batch)
                result.docs_ingested = ingest_stats.get("docs_stored", 0)

            # ---- STAGE 1b: PDF FETCH + TEXT EXTRACT (India only) ---------
            # Download PDFs for this month's high-value docs, extract text into
            # raw_text DB column, then delete files — zero disk accumulation.
            _country = self.config.get("market", {}).get("country", "US")
            if not self.skip_pdf_fetch and _country == "IN":
                self._pdf_fetch_month_india(window_start, window_end, replay_batch)

            # ---- STAGE 2: NLP (+ events combined in same file-read pass) ---
            _nlp_handled_events = False
            if not self.skip_nlp:
                nlp_stats = self._nlp_month(window_start, window_end)
                result.docs_nlp = nlp_stats.get("docs_processed", 0)
                result.events_extracted = nlp_stats.get("events_extracted", 0)
                _nlp_handled_events = nlp_stats.get("events_extracted", 0) > 0 or not self.skip_events

            # ---- STAGE 3: GRAPH -----------------------------------------
            if not self.skip_graph:
                graph_stats = self._graph_month(window_start, window_end)
                result.nodes_built = graph_stats.get("nodes_built", 0)
                result.edges_built = graph_stats.get("edges_built", 0)

            # ---- STAGE 4: EVENTS (skipped if NLP already handled it) ----
            if not self.skip_events and not _nlp_handled_events:
                event_stats = self._events_month(window_start, window_end)
                result.events_extracted = event_stats.get("events_extracted", 0)

            # ---- STAGE 5: CAUSAL CHAINS ---------------------------------
            if not self.skip_causal:
                causal_stats = self._causal_month(window_end)
                result.causal_score = causal_stats.get("top_score", 0.0)

            # ---- STAGE 6: THEMES (as_of=replay_date) --------------------
            if not self.skip_themes:
                theme_stats = self._themes_month(window_end)
                result.themes_detected = theme_stats.get("themes_detected", 0)
                result.themes_snapped = theme_stats.get("themes_snapped", 0)
                result.theme_ids = theme_stats.get("theme_id_map", {})

            # ---- STAGE 7: MACRO CONSTRAINT ENGINE (as_of=replay_date) ---
            self._macro_month(window_start, window_end)

            # ---- STAGE 8: SNAPSHOT BENEFICIARIES → performance seed -----
            self._seed_performance(result)

        except Exception as e:
            logger.error(f"Replay month {replay_batch} failed: {e}", exc_info=True)
            result.status = "error"
            result.error = str(e)

        result.duration_sec = round(time.time() - t0, 2)
        return result

    def _ingest_month(self, window_start: date, window_end: date, replay_batch: str) -> dict:
        """Fetch and store filings in the monthly window.

        Dispatch logic:
          - US  → EDGAR CIK-based batch fetcher (date-windowed, one month at a time)
          - IN  → India company filing sources (NSE/BSE/Screener) — date-windowed
                  per monthly chunk.  India macro/policy sources (PIB/SEBI/RBI/
                  InvestIndia/Commerce) are handled by run_macro() as policy events
                  in mg_policy_events (country='IN'), not here.
          - Other → falls through to EDGAR path (may be empty if no CIKs configured)
        """
        _country = self.config.get("market", {}).get("country", "US")

        if _country == "IN":
            return self._ingest_month_india(window_start, window_end, replay_batch)

        if not self._run_cik_list:
            self._build_run_cik_list()

        from ..fetcher.edgar_fetcher import EdgarFetcher
        from ..dedup.deduplicator import Deduplicator
        from ..normalizer.text_normalizer import TextNormalizer

        edgar_cfg = self.config.get("edgar", {})
        fetcher = EdgarFetcher({
            **edgar_cfg,
            # Pass the pre-built CIK list directly so the offset file is not
            # re-read or advanced again — all months share the same company set.
            "cik_list": list(self._run_cik_list),
            "fetch_mode": "selected",   # CIKs already resolved; bypass _build_cik_list
            "download_dir": self.config.get("storage", {}).get("download_dir", "data/raw"),
            "user_agent": edgar_cfg.get("user_agent", "MakroGraph/0.2 (Research)"),
        })

        dedup = Deduplicator(self.config.get("dedup", {}))
        normalizer = TextNormalizer(self.config.get("normalizer", {}))

        start_dt = datetime(window_start.year, window_start.month, window_start.day, tzinfo=timezone.utc)
        end_dt = datetime(window_end.year, window_end.month, window_end.day, 23, 59, 59, tzinfo=timezone.utc)

        source_docs = fetcher.discover_date_range(start_dt, end_dt, replay_batch=replay_batch)
        fetch_results, matched_docs = fetcher.fetch_discovered_from_list(source_docs)

        stats = {"docs_fetched": len(fetch_results), "docs_stored": 0, "docs_skipped": 0}

        for result, source_doc in zip(fetch_results, matched_docs):
            if not result.success:
                stats["docs_skipped"] += 1
                continue

            raw_text = ""
            if result.local_path and result.local_path.exists():
                suffix = result.local_path.suffix.lower()
                if suffix == ".pdf":
                    from ..parser.pdf_parser import PDFParser
                    parse_result = PDFParser(self.config.get("parser", {})).parse(result.local_path)
                    raw_text = parse_result.text if parse_result.success else ""
                elif suffix in (".html", ".htm", ".xhtml"):
                    from bs4 import BeautifulSoup
                    html = result.local_path.read_text(encoding="utf-8", errors="ignore")
                    raw_text = BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)
                else:
                    raw_text = result.local_path.read_text(encoding="utf-8", errors="ignore")

            url_hash = dedup._hash_url(result.url)
            content_hash = dedup._hash_content(raw_text) if raw_text else url_hash
            dup = dedup.check(result.url, raw_text or "")
            if dup.status.value != "new":
                stats["docs_skipped"] += 1
                continue

            if self._pg_store:
                # Preserve the REAL filing date from source_doc — never replay_date
                actual_filed_at = source_doc.published_at.date() if source_doc.published_at else window_end
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
                    "filed_at": actual_filed_at,           # real document date
                    "published_at": source_doc.published_at,
                    "local_path": str(result.local_path) if result.local_path else "",
                    "word_count": len(raw_text.split()) if raw_text else 0,
                    "processing_status": "fetched",
                    "fiscal_period": source_doc.metadata.get("replay_batch", replay_batch),
                })
                if doc_id:
                    stats["docs_stored"] += 1

        logger.info(f"  Ingest [{replay_batch}]: {stats}")
        return stats

    def _ingest_month_india(self, window_start: date, window_end: date, replay_batch: str) -> dict:
        """India ingest for historical replay.

        Strategy:
          - First replay month (window_start == self.start_date): run a full India
            ingest so ALL historical documents from start_date onwards are fetched
            and stored with their real published_at as filed_at.
          - Subsequent months: skip ingest — all docs are already in the DB.
            Deduplication ensures nothing is double-stored if the user reruns.

        The NLP / graph / themes / causal stages downstream window by filed_at, so
        replay integrity (no look-ahead) is maintained even though all docs are
        loaded up front in the first month.

        Now that NSE/BSE support `until` (upper bound), each replay month fetches
        ONLY documents published in [window_start, window_end] — no over-fetching,
        no need to load everything in the first month.  Deduplication still prevents
        double-storing if the user reruns a month.
        """
        from datetime import datetime, timezone

        since_dt = datetime(
            window_start.year, window_start.month, window_start.day,
            tzinfo=timezone.utc,
        )
        until_dt = datetime(
            window_end.year, window_end.month, window_end.day,
            23, 59, 59, tzinfo=timezone.utc,
        )
        logger.info(
            f"  India ingest [{replay_batch}]: "
            f"{window_start} → {window_end}"
        )
        try:
            stats = self._pipeline.run_ingest_india(since=since_dt, until=until_dt)
        except Exception as e:
            logger.error(f"  India ingest [{replay_batch}] failed: {e}", exc_info=True)
            stats = {"docs_stored": 0, "docs_skipped": 0, "docs_fetched": 0, "error": str(e)}
        return stats

    def _pdf_fetch_month_india(self, window_start: date, window_end: date, replay_batch: str) -> dict:
        """Download PDFs for this month's India docs, extract text to DB, delete files.

        Historical-mode only:
          - Scoped to window_start → window_end so only this month's docs are processed.
          - store_text_to_db=True  → extracted text written to raw_text column.
          - delete_after_parse=True → PDF deleted after text extraction (zero disk growth).
          - NLP stage then reads raw_text from DB, no file I/O needed.
        """
        logger.info(f"  India PDF fetch [{replay_batch}]: {window_start} → {window_end}")
        try:
            stats = self._pipeline.run_pdf_fetch_india(
                batch_size=100,
                max_workers=4,
                window_start=window_start,
                window_end=window_end,
                store_text_to_db=True,
                delete_after_parse=True,
            )
            logger.info(
                f"  India PDF fetch [{replay_batch}] done: "
                f"downloaded={stats.get('docs_downloaded', 0)} "
                f"failed={stats.get('docs_failed', 0)} "
                f"unsupported={stats.get('docs_unsupported', 0)}"
            )
            return stats
        except Exception as e:
            logger.error(f"  India PDF fetch [{replay_batch}] failed: {e}", exc_info=True)
            return {"error": str(e)}

    def _nlp_month(self, window_start: date, window_end: date) -> dict:
        """Run NLP on docs fetched in this window.

        Optimisations vs original:
          1. Batch entity upsert + doc-entity links: 80 round-trips → 1 transaction per doc.
          2. Batch signal insert: N round-trips → 1 transaction per doc.
          3. Pre-filter noise entities before touching DB (dates, form labels, CIK numbers).
          4. Combined NLP + Events pass: file is read ONCE per doc; events extracted in
             the same loop so _events_month can be skipped for this window.
        """
        from ..themes.theme_detector import _is_noise_entity

        p = self._pipeline
        if not p._entity_extractor:
            p._init_nlp()

        # Lazily init event extractor so events are extracted in the same pass
        has_events = False
        if not self.skip_events:
            try:
                if not p._event_extractor:
                    p._init_intelligence()
                has_events = p._event_extractor is not None
            except Exception:
                pass

        project_root = Path(self.config.get("storage", {}).get(
            "project_root", Path(__file__).resolve().parent.parent.parent.parent
        ))

        _country = self.config.get("market", {}).get("country", "US")
        CHUNK = 500
        stats = {"docs_processed": 0, "entities_found": 0, "signals_found": 0,
                 "events_extracted": 0, "noise_filtered": 0}

        while True:
            docs = self._pg_store.get_documents_for_replay(
                "fetched", window_start, window_end, limit=CHUNK, country=_country
            )
            if not docs:
                break

            failed_ids: list[int] = []
            done_ids: list[int] = []

            for doc in docs:
                doc_id = doc["id"]
                doc_filed_at = doc.get("filed_at")

                # ── Read text — DB raw_text first, then fall back to file ─────
                raw_text = doc.get("raw_text", "") or ""

                if not raw_text:
                    raw_path = doc.get("local_path", "")
                    if raw_path and raw_path not in ("UNSUPPORTED_FORMAT",):
                        lp = Path(raw_path)
                        if not lp.is_absolute():
                            lp = project_root / lp
                        if lp.exists():
                            try:
                                suffix = lp.suffix.lower()
                                if suffix == ".pdf":
                                    from ..parser.pdf_parser import PDFParser
                                    res = PDFParser(self.config.get("parser", {})).parse(lp)
                                    raw_text = res.text if res.success else ""
                                elif suffix in (".html", ".htm", ".xhtml"):
                                    from bs4 import BeautifulSoup
                                    html = lp.read_text(encoding="utf-8", errors="ignore")
                                    raw_text = BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)
                                else:
                                    raw_text = lp.read_text(encoding="utf-8", errors="ignore")
                            except Exception as e:
                                logger.warning(f"Text read failed {lp}: {e}")

                if not raw_text:
                    failed_ids.append(doc_id)
                    continue

                # ── Entity extraction — pre-filter noise before DB ────────────
                extraction = p._entity_extractor.extract(raw_text, document_id=doc_id)
                clean_entities = []
                for ent in extraction.entities:
                    if _is_noise_entity(ent.canonical_name or ent.entity_text or ""):
                        stats["noise_filtered"] += 1
                        continue
                    clean_entities.append({
                        "entity_text": ent.entity_text,
                        "entity_type": ent.entity_type,
                        "canonical_name": ent.canonical_name,
                        "confidence": ent.confidence,
                        "metadata": ent.metadata if isinstance(ent.metadata, dict) else {},
                    })

                try:
                    self._pg_store.batch_upsert_entities_and_links(
                        doc_id, clean_entities, doc_filed_at
                    )
                except Exception as e:
                    logger.warning(f"Batch entity upsert failed doc {doc_id} ({e}), falling back")
                    for ent_d in clean_entities:
                        eid = self._pg_store.upsert_entity({**ent_d,
                            "first_seen_at": doc_filed_at, "last_seen_at": doc_filed_at})
                        if eid:
                            self._pg_store.link_document_entity(doc_id, eid)
                stats["entities_found"] += len(clean_entities)

                # ── Signal extraction — batch insert ──────────────────────────
                signals = p._signal_extractor.extract(raw_text, document_id=doc_id)
                signal_dicts = [
                    {
                        "document_id": doc_id,
                        "signal_type": sig.signal_type,
                        "direction": sig.direction,
                        "confidence": sig.confidence,
                        "signal_value": sig.signal_value,
                        "signal_unit": sig.signal_unit,
                        "context_text": sig.context_text[:500],
                        "extracted_by": sig.extracted_by,
                        "filed_at": doc_filed_at,
                    }
                    for sig in signals
                ]
                try:
                    self._pg_store.batch_insert_signals(signal_dicts)
                except Exception as e:
                    logger.warning(f"Batch signal insert failed doc {doc_id} ({e}), falling back")
                    for sd in signal_dicts:
                        self._pg_store.insert_signal(sd)
                stats["signals_found"] += len(signal_dicts)

                # ── Events — extracted in the SAME pass (file already in memory) ──
                if has_events:
                    try:
                        events = p._event_extractor.extract(
                            text=raw_text,
                            document_id=doc_id,
                            company=doc.get("company", ""),
                            filed_at=doc_filed_at,
                        )
                        for ev in events:
                            try:
                                self._pg_store.insert_event({
                                    "document_id": ev.document_id,
                                    "event_type": ev.event_type.value,
                                    "subject_entity": ev.subject_entity,
                                    "subject_type": (ev.subject_type.value
                                                     if hasattr(ev.subject_type, "value")
                                                     else str(ev.subject_type)),
                                    "description": ev.description,
                                    "magnitude": ev.magnitude,
                                    "magnitude_unit": ev.magnitude_unit,
                                    "direction": ev.direction,
                                    "confidence": ev.confidence,
                                    "second_order_entities": ev.second_order_entities,
                                    "context_text": ev.context_text,
                                    "filed_at": ev.filed_at,
                                })
                                stats["events_extracted"] += 1
                            except Exception:
                                pass
                    except Exception as e:
                        logger.debug(f"Event extraction failed doc {doc_id}: {e}")

                done_ids.append(doc_id)
                stats["docs_processed"] += 1

            # Flush status updates for this chunk before fetching the next
            if done_ids:
                self._pg_store.batch_update_document_status(done_ids, "nlp_done")
            if failed_ids:
                self._pg_store.batch_update_document_status(failed_ids, "nlp_failed")

        logger.info(f"  NLP [{window_end}]: {stats}")
        return stats

    def _graph_month(self, window_start: date, window_end: date) -> dict:
        """Build graph from NLP docs in this window."""
        p = self._pipeline
        if not p._graph_builder:
            p._init_graph_builder()
        if not p._graph_store:
            return {"nodes_built": 0, "edges_built": 0}

        _country = self.config.get("market", {}).get("country", "US")
        CHUNK = 500
        stats = {"nodes_built": 0, "edges_built": 0, "docs_processed": 0}

        while True:
            docs = self._pg_store.get_documents_for_replay(
                "nlp_done", window_start, window_end, limit=CHUNK, country=_country
            )
            if not docs:
                break

            doc_ids = [d["id"] for d in docs]
            try:
                entities_by_doc = self._pg_store.get_entities_for_documents(doc_ids)
            except Exception as e:
                logger.warning(f"Batch entity fetch failed ({e}), falling back to per-doc")
                entities_by_doc = {d["id"]: self._pg_store.get_entities_for_document(d["id"]) for d in docs}

            graph_done_ids: list[int] = []
            for doc in docs:
                doc_id = doc["id"]
                try:
                    pg_entities = entities_by_doc.get(doc_id, [])
                    if not pg_entities:
                        graph_done_ids.append(doc_id)
                        continue
                    nodes, edges = p._graph_builder.build_from_pg_entities(pg_entities, dict(doc))
                    stats["nodes_built"] += len(nodes)
                    stats["edges_built"] += len(edges)
                    stats["docs_processed"] += 1
                    graph_done_ids.append(doc_id)
                except Exception as e:
                    logger.warning(f"Graph build failed doc {doc_id}: {e}")

            if graph_done_ids:
                self._pg_store.batch_update_document_status(graph_done_ids, "graph_built")

        logger.info(f"  Graph [{window_end}]: {stats}")
        return stats

    def _events_month(self, window_start: date, window_end: date) -> dict:
        """Extract events from docs in this window."""
        p = self._pipeline
        if not p._event_extractor:
            p._init_intelligence()

        project_root = Path(self.config.get("storage", {}).get(
            "project_root", Path(__file__).resolve().parent.parent.parent.parent
        ))
        # Events are already extracted inside _nlp_month (same file-read pass).
        # _graph_month (which runs before this) marks docs graph_built, so this
        # fetch returns nothing in the normal replay flow. It runs as a safety net
        # for any nlp_done docs that were skipped by the combined pass.
        _country = self.config.get("market", {}).get("country", "US")
        docs = self._pg_store.get_documents_for_replay(
            "nlp_done", window_start, window_end, limit=500, country=_country
        )
        stats = {"events_extracted": 0}

        for doc in docs:
            lp = doc.get("local_path", "")
            if not lp:
                continue
            lp = Path(lp) if Path(lp).is_absolute() else project_root / lp
            if not lp.exists():
                continue
            try:
                from bs4 import BeautifulSoup
                raw = lp.read_text(encoding="utf-8", errors="ignore")
                text = BeautifulSoup(raw, "lxml").get_text(separator=" ", strip=True)
            except Exception:
                continue

            events = p._event_extractor.extract(
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
                        "subject_type": ev.subject_type.value if hasattr(ev.subject_type, "value") else str(ev.subject_type),
                        "description": ev.description,
                        "magnitude": ev.magnitude,
                        "magnitude_unit": ev.magnitude_unit,
                        "direction": ev.direction,
                        "confidence": ev.confidence,
                        "second_order_entities": ev.second_order_entities,
                        "context_text": ev.context_text,
                        "filed_at": ev.filed_at,
                    })
                    stats["events_extracted"] += 1
                except Exception as e:
                    logger.debug(f"Event insert failed: {e}")

        return stats

    def _causal_month(self, as_of: date) -> dict:
        """Score causal chains as_of the replay date."""
        p = self._pipeline
        if not p._causal_mapper:
            p._init_intelligence()

        # Use a wider lookback (365 days) so a single month of historical replay
        # still has enough entity context to score chains. The causal chains are
        # cumulative — if "Semiconductor" was mentioned at any point in the last
        # year, that link should be considered active.
        _country = self.config.get("market", {}).get("country", "US")
        lookback = as_of - timedelta(days=365)
        active_entities = {
            r.get("canonical_name", "")
            for r in self._pg_store.get_entities_in_window(lookback, as_of, country=_country)
        }
        try:
            active_signals = self._pg_store.get_all_signals_in_window(
                ["capex_increase", "technology_adoption", "demand_surge", "supply_bottleneck"],
                lookback, as_of,
                country=_country,
            )
        except Exception:
            active_signals = []
            for stype in ["capex_increase", "technology_adoption", "demand_surge", "supply_bottleneck"]:
                active_signals.extend(
                    self._pg_store.get_signals_in_window(stype, lookback, as_of, country=_country)
                )

        # Auto-discover new chains from the signals accumulated up to this date
        try:
            p._causal_mapper.discover_chains_from_data(
                self._pg_store, as_of_date=as_of, lookback_days=730
            )
        except Exception as e:
            logger.debug(f"Causal auto-discovery failed: {e}")

        chains = p._causal_mapper.score_chains(active_entities, active_signals)
        try:
            p._causal_mapper.persist(self._pg_store)
        except Exception as e:
            logger.debug(f"Causal persist failed: {e}")

        top_score = chains[0].activation_score if chains else 0.0
        return {"top_score": top_score, "chains_active": sum(1 for c in chains if c.activation_score > 20)}

    def _themes_month(self, replay_date: date) -> dict:
        """Detect and snapshot themes as_of replay_date."""
        p = self._pipeline
        if not p._theme_detector:
            p._init_themes()

        stats = p.run_themes(as_of_date=replay_date)
        stats["theme_id_map"] = {}

        # Collect theme_id_map from what was just persisted
        try:
            active = self._pg_store.get_active_themes(min_strength=0.0)
            stats["theme_id_map"] = {t["theme_slug"]: t["id"] for t in active}
        except Exception:
            pass

        return stats

    def _macro_month(self, window_start: date, window_end: date):
        """Run Constraint Engine for this replay month.

        Macro series data must already be in PostgreSQL (fetched once via the
        Macro & Policy tab or CLI before running the historical replay).
        This method only runs the constraint engine — it does NOT re-fetch
        FRED/EIA/World Bank to avoid enormous API call counts per month.
        Macro data is fetched once for the full replay range; constraint
        scoring is applied per-month using as_of=replay_date to prevent
        future leakage.
        """
        p = self._pipeline
        try:
            if p._macro_store is None:
                p._init_macro()
            if p._constraint_engine is None:
                return

            active_themes = self._pg_store.get_active_themes(min_strength=5.0)
            if not active_themes:
                return

            p._constraint_engine.run(active_themes, as_of_date=window_end)
            logger.debug(
                f"Constraint Engine [{window_end}]: scored {len(active_themes)} themes"
            )
        except Exception as e:
            logger.warning(f"_macro_month constraint engine skipped: {e}")

    def _seed_performance(self, result: MonthlyResult):
        """Seed mg_theme_performance with NULL forward returns at detection time.

        These rows are later filled in by fill_forward_returns() once price
        data is available for the forward windows.

        Optimised: one query for ALL beneficiaries across all active themes
        (was: one connection checkout per theme → N separate queries).
        """
        if not result.theme_ids:
            return

        try:
            from psycopg2.extras import RealDictCursor, execute_values
            active = self._pg_store.get_active_themes(min_strength=20.0)
            if not active:
                return

            theme_ids = [t["id"] for t in active]
            theme_by_id = {t["id"]: t for t in active}

            # ONE query for all beneficiaries across all active themes,
            # then one batch INSERT — all inside a single cursor context.
            beneficiary_map: dict[int, list[dict]] = {}
            with self._pg_store._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Step 1: fetch beneficiaries
                    cur.execute(
                        "SELECT theme_id, ticker, company_name, relevance_score, "
                        "       beneficiary_type "
                        "FROM mg_theme_beneficiaries "
                        "WHERE theme_id = ANY(%s) AND ticker IS NOT NULL",
                        (theme_ids,)
                    )
                    for row in cur.fetchall():
                        beneficiary_map.setdefault(row["theme_id"], []).append(dict(row))

                    # Step 2: build performance rows (cursor still open)
                    perf_rows = []
                    for theme in active:
                        tid = theme["id"]
                        slug = theme.get("theme_slug", "")
                        for ben in beneficiary_map.get(tid, []):
                            ticker = ben.get("ticker") or ""
                            if not ticker:
                                continue
                            perf_rows.append((
                                tid,
                                slug,
                                ticker,
                                ben.get("company_name", ""),
                                result.replay_date,
                                theme.get("strength_score", 0.0),
                                theme.get("conviction", "emerging"),
                                result.replay_batch,
                            ))

                    # Step 3: batch INSERT (same cursor — not yet closed)
                    if perf_rows:
                        perf_sql = """
                            INSERT INTO mg_theme_performance
                                (theme_id, theme_slug, ticker, company_name, detection_date,
                                 detection_score, conviction, replay_batch)
                            VALUES %s
                            ON CONFLICT (theme_slug, ticker, detection_date)
                            DO UPDATE SET
                                detection_score = EXCLUDED.detection_score,
                                conviction      = EXCLUDED.conviction
                        """
                        execute_values(cur, perf_sql, perf_rows)

        except Exception as e:
            logger.warning(f"_seed_performance failed: {e}")

    # ----------------------------------------------------------
    # INIT / CLEANUP
    # ----------------------------------------------------------

    def _init_pipeline(self):
        from .intelligence_pipeline import IntelligencePipeline
        from ..storage.pg_store import PGStore

        if self._pipeline is not None:
            return

        self._pipeline = IntelligencePipeline(self.config)
        self._pipeline._init_storage()
        self._pg_store = self._pipeline._pg_store
        try:
            self._pipeline._init_macro()
        except Exception as e:
            logger.debug(f"Macro init skipped (will retry per-month): {e}")

        # Apply any new schema tables created in this release
        try:
            schema_path = Path(__file__).resolve().parent.parent.parent.parent / "schema" / "postgres_schema.sql"
            if schema_path.exists():
                self._pg_store.apply_schema(str(schema_path))
        except Exception as e:
            logger.debug(f"Schema re-apply skipped: {e}")

        # Build the company CIK list once for this entire run.
        # This advances the batch offset a single time regardless of how many
        # months are in the replay window.
        self._build_run_cik_list()

        logger.info("HistoricalRunner pipeline initialised")

    def _build_run_cik_list(self):
        """Resolve and cache the CIK list for this run (advances offset once).

        No-op for non-US markets — EDGAR CIK lists are only used by the US ingest
        path.  India and other markets use their own fetchers (NSE, BSE, etc.) which
        do not require CIK resolution.
        """
        _country = self.config.get("market", {}).get("country", "US")
        if _country != "US":
            logger.info(
                f"HistoricalRunner: skip CIK list build (market.country={_country}, "
                f"EDGAR/CIK lookup only needed for US)"
            )
            return

        from ..fetcher.edgar_fetcher import EdgarFetcher

        edgar_cfg = self.config.get("edgar", {})
        fetcher = EdgarFetcher({
            **edgar_cfg,
            "download_dir": self.config.get("storage", {}).get("download_dir", "data/raw"),
            "user_agent": edgar_cfg.get("user_agent", "MakroGraph/0.2 (Research)"),
        })
        fetcher._build_cik_list()
        self._run_cik_list = fetcher.cik_list
        if not self._run_cik_list:
            logger.warning(
                "Company CIK list is empty! Check fetch_mode and ticker_list in config. "
                "No documents will be ingested this run."
            )
        fetch_mode = edgar_cfg.get("fetch_mode", "selected")
        logger.info(
            f"Company batch for this run: {len(self._run_cik_list)} companies "
            f"(fetch_mode={fetch_mode})"
        )

    def _log_result(self, result: MonthlyResult):
        """Persist MonthlyResult to mg_replay_runs."""
        if self._pg_store:
            try:
                self._pg_store.log_replay_run(result.to_dict())
            except Exception as e:
                logger.warning(f"log_replay_run failed: {e}")

    def _close(self):
        if self._pipeline:
            try:
                self._pipeline.close()
            except Exception:
                pass


# -------------------------------------------------------
# HELPER: price return calculation
# -------------------------------------------------------

def _calc_return(price_map: dict, start_date: date, days: int) -> Optional[float]:
    """Calculate % price return over `days` from start_date using price_map.

    Args:
        price_map: {date_str: float} closing prices (YYYY-MM-DD keys)
        start_date: detection date
        days: forward window

    Returns:
        Percentage return (e.g. 15.2 for +15.2%) or None if data unavailable.
    """
    if not price_map:
        return None

    def _nearest(target: date) -> Optional[float]:
        for offset in range(5):
            key = str(target + timedelta(days=offset))
            if key in price_map:
                return price_map[key]
        return None

    p0 = _nearest(start_date)
    p1 = _nearest(start_date + timedelta(days=days))
    if p0 and p1 and p0 != 0:
        return round((p1 - p0) / p0 * 100.0, 4)
    return None
