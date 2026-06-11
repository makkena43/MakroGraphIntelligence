#!/usr/bin/env python3
"""Fetch, store, and run NLP on India macro/policy documents.

Source priority order:
  Tier 1 (PDF, highest signal):
    Economic Survey → Union Budget → NITI Aayog → CEA → Ministry of Power →
    MNRE → DPIIT → RBI Reports

  Tier 2 (PDF, sector-specific):
    PowerGrid → NTPC → SECI → Indian Railways → Ministry of Steel →
    Ministry of Heavy Industries → Ministry of Coal → Ministry of Chemicals

  Tier 3 (PDF/HTML, legislative):
    PRS India

  Secondary (RSS / HTML, existing fetchers):
    RBI (press releases) → Invest India → SEBI

  Tier 4 Optional:
    PIB  (disabled by default due to reliability; enable via pib.enabled in settings.yaml)

Steps:
  1. Run IndiaPDFFetcher for Tier 1-3 sources (download + parse PDFs)
  2. Run secondary fetchers (RBI RSS, InvestIndia, SEBI)
  3. Optionally run PIBFetcher if enabled
  4. Upsert all docs into mg_documents with raw_text and processing_status='fetched'
  5. Run the standard NLP pass over newly fetched docs
  6. Run India causal chain discovery

Usage:
  python scripts/run_india_policy_nlp.py [--since YYYY-MM-DD] [--dry-run]
"""
import sys, yaml, json, logging, argparse, hashlib
from datetime import datetime, date, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("india_policy_nlp")

sys.path.insert(0, ".")

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--since", default=None,
                    help="Only fetch docs published since this date (YYYY-MM-DD). "
                         "Defaults to 2 years ago.")
parser.add_argument("--dry-run", action="store_true",
                    help="Discover docs and print counts; don't store or run NLP")
args = parser.parse_args()

since_dt: datetime | None = None
if args.since:
    since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
else:
    from datetime import timedelta
    since_dt = (datetime.now(timezone.utc) - timedelta(days=730))

# ── Config ────────────────────────────────────────────────────────────────────
with open("config/settings.yaml") as f:
    config = yaml.safe_load(f)
try:
    with open("config/secrets.json") as f:
        secrets = json.load(f)
    for section, values in secrets.items():
        if section.startswith("_"):
            continue
        if isinstance(values, dict):
            config.setdefault(section, {}).update({k: v for k, v in values.items() if v})
except Exception as e:
    logger.warning(f"secrets: {e}")

config.setdefault("market", {})["country"] = "IN"

# ── Storage ───────────────────────────────────────────────────────────────────
from src.makrograph.storage.pg_store import PGStore
pg_store = PGStore(config)

# ── Shared fetcher base config ────────────────────────────────────────────────
_fetcher_base = config.get("fetcher", {})
_base_cfg = {
    "download_dir":            config.get("storage", {}).get("download_dir", "data/raw"),
    "user_agent":              config.get("user_agent", "MakroGraph/0.2"),
    "request_timeout_seconds": _fetcher_base.get("request_timeout_seconds", 30),
    "retry_attempts":          _fetcher_base.get("retry_attempts", 3),
    "retry_delay_seconds":     _fetcher_base.get("retry_delay_seconds", 2),
}

# ── Source definitions ────────────────────────────────────────────────────────
# Tier 1-3: unified PDF fetcher (IndiaPDFFetcher).
# Sources list matches the recommended priority order from settings.yaml.
PDF_SOURCE_KEY = "india_pdf"

# Secondary: RSS/HTML-based fetchers (existing, keep running alongside PDFs).
SECONDARY_SOURCES = [
    ("rbi_india",    "rbi_fetcher",          "RBIFetcher",          "rbi"),
    ("invest_india", "invest_india_fetcher",  "InvestIndiaFetcher",  "invest_india"),
    ("sebi_india",   "sebi_fetcher",          "SEBIFetcher",         "sebi"),
]

# Tier 4 Optional: PIB (enabled/disabled via pib.enabled in settings.yaml).
OPTIONAL_SOURCES = [
    ("pib_india",    "pib_fetcher",           "PIBFetcher",          "pib"),
]

# ── Helper: store one document batch ─────────────────────────────────────────

def _store_doc(doc, src_key: str, fetcher=None) -> bool:
    """Download text if needed, then upsert doc to mg_documents.

    Returns True if a new row was stored (raw_text written), False otherwise.
    """
    url_hash = hashlib.md5(doc.url.encode()).hexdigest()
    meta = doc.metadata or {}
    raw_text = meta.get("body_text", "") or meta.get("body_snippet", "") or ""

    if not raw_text:
        if doc.doc_type == "pdf" or doc.url.lower().endswith(".pdf"):
            try:
                import tempfile, requests as _req
                from src.makrograph.parser.pdf_parser import PDFParser
                resp = _req.get(
                    doc.url, timeout=60,
                    headers={"User-Agent": config.get("user_agent", "MakroGraph/0.2")},
                    stream=True,
                )
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    for chunk in resp.iter_content(65536):
                        tmp.write(chunk)
                    tmp_path = tmp.name
                result = PDFParser(config.get("parser", {})).parse(tmp_path)
                raw_text = result.text if result.success else ""
                Path(tmp_path).unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"[{src_key}] PDF fetch/parse failed {doc.url}: {e}")
        elif fetcher and hasattr(fetcher, "_fetch_article_text"):
            try:
                raw_text = fetcher._fetch_article_text(doc.url) or ""
            except Exception as e:
                logger.debug(f"[{src_key}] article text fetch failed {doc.url}: {e}")

    if not raw_text and doc.title:
        raw_text = f"{doc.filing_type or doc.doc_type}: {doc.title}"

    # Postgres rejects strings with NUL bytes (0x00); strip them.
    raw_text = raw_text.replace("\x00", "") if raw_text else raw_text

    content_hash = hashlib.md5((doc.url + (raw_text or doc.title or "")).encode()).hexdigest()

    # Use the document's actual publication date extracted from its title/URL.
    # Store None when the date is genuinely unknown — do NOT fall back to the
    # run date (date.today()), which would make all undated docs look current.
    doc_date = doc.published_at.date() if doc.published_at else None

    doc_record = {
        "source_name":       src_key,
        "doc_type":          doc.doc_type or "pdf",
        "url":               doc.url,
        "url_hash":          url_hash,
        "content_hash":      content_hash,
        "title":             doc.title or "",
        "company":           "",
        "ticker":            "",
        "cik":               "",
        "filing_type":       doc.filing_type or doc.doc_type or "",
        "fiscal_period":     "",
        "filed_at":          doc_date,
        "published_at":      doc_date,
        "local_path":        "",
        "page_count":        0,
        "word_count":        len(raw_text.split()) if raw_text else 0,
        "processing_status": "fetched",
        "country":           "IN",
    }

    try:
        doc_id = pg_store.upsert_document(doc_record)
    except Exception as e:
        logger.debug(f"[{src_key}] upsert failed {doc.url}: {e}")
        return False

    if doc_id and raw_text:
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE mg_documents SET raw_text = %s WHERE id = %s",
                        (raw_text[:1_000_000], doc_id),
                    )
        except Exception as e:
            logger.warning(f"[{src_key}] raw_text store failed doc {doc_id}: {e}")
        return True

    return False


# ── Step 1: Tier 1-3 PDF sources (IndiaPDFFetcher) ───────────────────────────
total_stored = 0

pdf_cfg = config.get(PDF_SOURCE_KEY, {})
if not pdf_cfg.get("enabled", True):
    logger.info(f"[{PDF_SOURCE_KEY}] disabled in config — skipping Tier 1-3 PDF sources")
else:
    try:
        from src.makrograph.fetcher.india_pdf_fetcher import IndiaPDFFetcher
        merged_pdf_cfg = {**_base_cfg, **pdf_cfg}

        with IndiaPDFFetcher(merged_pdf_cfg) as pdf_fetcher:
            pdf_docs = pdf_fetcher.discover(since=since_dt)
            logger.info(f"[india_pdf] discovered {len(pdf_docs)} documents across Tier 1-3 sources")

            if args.dry_run:
                from collections import Counter
                counts = Counter(d.source_name for d in pdf_docs)
                for src, cnt in sorted(counts.items()):
                    sample = next((d for d in pdf_docs if d.source_name == src), None)
                    logger.info(f"  {src}: {cnt} docs")
                    if sample:
                        logger.info(f"    sample: {sample.title[:80]} | {sample.url[:80]}")
            else:
                stored = 0
                for doc in pdf_docs:
                    # Use doc.source_name (the specific source key, e.g. "economic_survey")
                    # rather than the generic "india_pdf" adapter name
                    if _store_doc(doc, doc.source_name):
                        stored += 1
                logger.info(f"[india_pdf] stored {stored}/{len(pdf_docs)} docs to mg_documents")
                total_stored += stored

    except Exception as exc:
        logger.error(f"[india_pdf] Tier 1-3 PDF sources failed: {exc}", exc_info=True)


# ── Step 2: Secondary sources (RSS/HTML — RBI press releases, InvestIndia, SEBI) ──
for src_key, module_name, class_name, cfg_key in SECONDARY_SOURCES:
    src_cfg = config.get(cfg_key, {})
    if not src_cfg.get("enabled", True):
        logger.info(f"[{src_key}] disabled in config — skipping")
        continue

    try:
        import importlib
        mod = importlib.import_module(f"src.makrograph.fetcher.{module_name}")
        FC = getattr(mod, class_name)
        merged_cfg = {**_base_cfg, **src_cfg, "fetch_full_text": True}

        with FC(merged_cfg) as fetcher:
            docs = fetcher.discover(since=since_dt)
            logger.info(f"[{src_key}] discovered {len(docs)} documents")

            if args.dry_run:
                for d in docs[:3]:
                    logger.info(f"  sample: {d.title[:80]} | {d.url[:80]}")
                continue

            stored = 0
            for doc in docs:
                if _store_doc(doc, src_key, fetcher=fetcher):
                    stored += 1
            logger.info(f"[{src_key}] stored {stored}/{len(docs)} docs to mg_documents")
            total_stored += stored

    except Exception as exc:
        logger.error(f"[{src_key}] failed: {exc}", exc_info=True)


# ── Step 3: Tier 4 Optional — PIB ─────────────────────────────────────────────
for src_key, module_name, class_name, cfg_key in OPTIONAL_SOURCES:
    src_cfg = config.get(cfg_key, {})
    if not src_cfg.get("enabled", False):
        logger.info(f"[{src_key}] Tier 4 optional — disabled in config (set pib.enabled: true to enable)")
        continue

    try:
        import importlib
        mod = importlib.import_module(f"src.makrograph.fetcher.{module_name}")
        FC = getattr(mod, class_name)
        merged_cfg = {**_base_cfg, **src_cfg, "fetch_full_text": True}

        with FC(merged_cfg) as fetcher:
            docs = fetcher.discover(since=since_dt)
            logger.info(f"[{src_key}] discovered {len(docs)} documents")

            if args.dry_run:
                for d in docs[:3]:
                    logger.info(f"  sample: {d.title[:80]} | {d.url[:80]}")
                continue

            stored = 0
            for doc in docs:
                if _store_doc(doc, src_key, fetcher=fetcher):
                    stored += 1
            logger.info(f"[{src_key}] stored {stored}/{len(docs)} docs to mg_documents")
            total_stored += stored

    except Exception as exc:
        logger.error(f"[{src_key}] failed: {exc}", exc_info=True)

if args.dry_run:
    logger.info("Dry run complete — no data written")
    sys.exit(0)

logger.info(f"Total policy docs stored: {total_stored}")
if total_stored == 0:
    logger.warning("No new policy docs stored — NLP stage skipped")
    sys.exit(0)

# ── Step 5: NLP pass over newly fetched policy docs ─────────────────────────────
logger.info("Running NLP on newly fetched policy docs...")

from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
from src.makrograph.pipeline.historical_runner import HistoricalRunner

# Build runner with a dummy date range; we call _nlp_month directly so the
# range only matters to skip the ingest/graph/causal stages we don't want.
window_start = date(2020, 1, 1)
window_end   = date.today()

runner = HistoricalRunner(
    config,
    start_date=window_start,
    end_date=window_end,
    skip_ingest=True,
    skip_graph=True,
    skip_events=True,
    skip_causal=True,
    skip_themes=True,
    skip_pdf_fetch=True,
)
# Initialise internal pipeline + pg_store, then inject our shared pg_store
# so the NLP pass uses the same connection we already used above.
runner._init_pipeline()
runner._pipeline._pg_store = pg_store
runner._pg_store = pg_store
runner._pipeline._init_nlp()

nlp_stats = runner._nlp_month(window_start, window_end)
logger.info(
    f"NLP complete: docs={nlp_stats.get('docs_processed',0)} "
    f"entities={nlp_stats.get('entities_found',0)} "
    f"signals={nlp_stats.get('signals_found',0)}"
)

# ── Step 6: India causal chain discovery ─────────────────────────────────────
logger.info("Running India causal chain discovery (policy + company patterns)...")

from src.makrograph.india.causal_chain_generator import IndiaCausalChainGenerator
chain_gen = IndiaCausalChainGenerator(config)
saved = chain_gen.score_and_persist(pg_store, as_of_date=date.today())
logger.info(f"Causal chain stage complete: {saved} chains persisted")

logger.info("India policy NLP pipeline finished.")
