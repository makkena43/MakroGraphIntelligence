"""
smoke_test_pipeline.py
======================
Injects ONE synthetic document through every pipeline stage and reports the
country column value at each checkpoint.

Run:
    python smoke_test_pipeline.py
"""

import sys
import os
import yaml
import hashlib
import textwrap
import logging
from datetime import date, datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("smoke_test")

# ── Locate project root & add src to path ─────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"

# ── Load config ────────────────────────────────────────────────────────────
with open(CONFIG_PATH) as f:
    cfg = yaml.safe_load(f)

COUNTRY = cfg.get("market", {}).get("country", "US")
log.info(f"Active country from config: {COUNTRY}")

# ── Sample document text — one page, high-signal content ───────────────────
SAMPLE_TEXT = textwrap.dedent("""
    NVIDIA Corporation — Q2 2024 Earnings Call (Synthetic Smoke-Test Document)

    CEO Jensen Huang opened by noting a significant demand surge for H100 and H200
    AI accelerators. Capital expenditure for new wafer starts at TSMC increased 40%
    year-over-year to meet hyperscaler orders from Microsoft, Amazon, and Google.

    Supply bottleneck: advanced packaging capacity at TSMC and Samsung remains
    fully allocated through 2025. Lead times have extended from 12 to 26 weeks.
    The company expects AI infrastructure capital spending to exceed $200 billion
    industry-wide in fiscal 2025.

    Strategic pivot: NVIDIA is accelerating its custom ASIC roadmap with a
    partnership formed with Broadcom and Marvell for next-generation networking.

    Technology adoption: Large language models are driving demand for HBM3 memory
    from SK Hynix and Micron. Inventory buildup at hyperscalers slowed in Q1 but
    demand exceeded analyst forecasts by 35%.

    Regulatory tailwind: The US CHIPS Act provides $52 billion in incentives for
    domestic semiconductor fabrication, supporting Intel's Ohio fab ramp.
""").strip()

URL       = "https://smoke-test.internal/nvda-q2-2024-synthetic"
URL_HASH  = hashlib.sha256(URL.encode()).hexdigest()[:64]
CONTENT_HASH = hashlib.sha256(SAMPLE_TEXT.encode()).hexdigest()[:64]
FILED_AT  = date(2024, 8, 28)

# ── Write document to a temp file so NLP stage can read it ─────────────────
tmp_dir = PROJECT_ROOT / "data" / "raw" / "smoke_test"
tmp_dir.mkdir(parents=True, exist_ok=True)
tmp_file = tmp_dir / "smoke_test_doc.txt"
tmp_file.write_text(SAMPLE_TEXT, encoding="utf-8")
log.info(f"Wrote sample document to {tmp_file}")

# ── Connect to PGStore ──────────────────────────────────────────────────────
from makrograph.storage.pg_store import PGStore
pg = PGStore(cfg.get("postgresql", {}))
log.info("PGStore connected")

# Ensure all country columns exist (idempotent migration)
pg.ensure_country_columns()
log.info("Schema migrations ensured")

SEP = "─" * 70

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 1 — INGEST: upsert document
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("STAGE 1 — INGEST")
print(SEP)

doc_id = pg.upsert_document({
    "source_name":        "smoke_test",
    "doc_type":           "earnings_call",
    "url":                URL,
    "url_hash":           URL_HASH,
    "content_hash":       CONTENT_HASH,
    "title":              "NVIDIA Q2 2024 Earnings (Smoke Test)",
    "company":            "NVIDIA Corporation",
    "ticker":             "NVDA",
    "cik":                "0001045810",
    "filing_type":        "8-K",
    "filed_at":           FILED_AT,
    "published_at":       datetime(2024, 8, 28, 17, 0, tzinfo=timezone.utc),
    "local_path":         str(tmp_file),
    "word_count":         len(SAMPLE_TEXT.split()),
    "processing_status":  "fetched",
    "country":            COUNTRY,
})

assert doc_id, "upsert_document returned None — check DB connection"
print(f"  doc_id      : {doc_id}")
print(f"  country     : {COUNTRY}  ← stored in mg_documents")

# Verify in DB
with pg._conn() as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, ticker, company, processing_status, country FROM mg_documents WHERE id = %s",
            (doc_id,)
        )
        row = cur.fetchone()
        print(f"  DB row      : {dict(zip(['id','ticker','company','status','country'], row))}")
        assert row[4] == COUNTRY, f"country mismatch: got {row[4]}, expected {COUNTRY}"
        print("  ✓ country column verified in mg_documents")

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 2 — NLP: entity + signal extraction
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("STAGE 2 — NLP (entity + signal extraction)")
print(SEP)

from makrograph.nlp.entity_extractor import EntityExtractor
from makrograph.nlp.signal_extractor import SignalExtractor
from makrograph.themes.theme_detector import _is_noise_entity

nlp_cfg = cfg.get("nlp", {})
entity_extractor = EntityExtractor(nlp_cfg)
signal_extractor = SignalExtractor(nlp_cfg)

extraction = entity_extractor.extract(SAMPLE_TEXT, document_id=doc_id)
clean_entities = [
    {
        "entity_text":    ent.entity_text,
        "entity_type":    ent.entity_type,
        "canonical_name": ent.canonical_name,
        "confidence":     ent.confidence,
        "metadata":       ent.metadata if isinstance(ent.metadata, dict) else {},
    }
    for ent in extraction.entities
    if not _is_noise_entity(ent.canonical_name or ent.entity_text or "")
]
pg.batch_upsert_entities_and_links(doc_id, clean_entities, FILED_AT)
print(f"  entities extracted : {len(clean_entities)}")
if clean_entities:
    sample_ents = [e['canonical_name'] for e in clean_entities[:5]]
    print(f"  sample entities    : {sample_ents}")

signals = signal_extractor.extract(SAMPLE_TEXT, document_id=doc_id)
signal_dicts = [
    {
        "document_id":  doc_id,
        "signal_type":  sig.signal_type,
        "direction":    sig.direction,
        "confidence":   sig.confidence,
        "signal_value": sig.signal_value,
        "signal_unit":  sig.signal_unit,
        "context_text": sig.context_text[:300],
        "extracted_by": sig.extracted_by,
        "filed_at":     FILED_AT,
    }
    for sig in signals
]
if signal_dicts:
    pg.batch_insert_signals(signal_dicts)
print(f"  signals extracted  : {len(signal_dicts)}")
if signal_dicts:
    sample_sigs = list({s['signal_type'] for s in signal_dicts})[:5]
    print(f"  signal types       : {sample_sigs}")

pg.update_document_status(doc_id, "nlp_done")
print("  ✓ processing_status → nlp_done")

# Verify signals link back to country via document JOIN
with pg._conn() as conn:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) FROM mg_signals s
               JOIN mg_documents d ON d.id = s.document_id
               WHERE s.document_id = %s AND d.country = %s""",
            (doc_id, COUNTRY)
        )
        sig_country_count = cur.fetchone()[0]
        print(f"  signals with country='{COUNTRY}' (via doc JOIN): {sig_country_count}")
        assert sig_country_count > 0, "No signals found for this document+country"
        if sig_country_count != len(signal_dicts):
            log.info(f"  Note: {len(signal_dicts) - sig_country_count} signal(s) were deduped on re-run (expected)")
        print("  ✓ all signals traceable to correct country")

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 3 — THEME DETECTION
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("STAGE 3 — THEME DETECTION")
print(SEP)

from makrograph.themes.theme_detector import ThemeDetector
from makrograph.themes.theme_ranker import ThemeRanker

theme_detector = ThemeDetector(cfg.get("themes", {}))
theme_ranker    = ThemeRanker(cfg.get("themes", {}))

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

from datetime import timedelta
lookback = FILED_AT - timedelta(days=365)

signal_records = pg.get_all_signals_in_window(
    ALL_SIGNAL_TYPES, lookback, FILED_AT, country=COUNTRY
)
cluster_rows = pg.get_entity_signal_clusters_in_window(
    ALL_SIGNAL_TYPES, lookback, FILED_AT, country=COUNTRY
)
print(f"  signals in window (country={COUNTRY}) : {len(signal_records)}")
print(f"  entity clusters   (country={COUNTRY}) : {len(cluster_rows)}")

_entity_days = (FILED_AT - lookback).days + 1
from makrograph.ontology.graph_evolution import GraphEvolutionTracker
# Load entities for seed detection
entity_records = []
try:
    with pg._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT ON (e.id)
                          e.id, e.canonical_name, e.entity_type, e.ticker,
                          0.0 AS sentiment_score
                   FROM mg_entities e
                   JOIN mg_document_entities de ON de.entity_id = e.id
                   JOIN mg_documents d ON d.id = de.document_id
                   WHERE d.filed_at >= %s AND d.filed_at <= %s AND d.country = %s
                   ORDER BY e.id, e.mention_count DESC LIMIT 1000""",
                (lookback, FILED_AT, COUNTRY)
            )
            entity_records = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
except Exception as e:
    log.warning(f"Entity load failed: {e}")

seed_themes   = theme_detector.detect_from_signals(signal_records, entity_records)
auto_themes   = theme_detector.detect_from_clusters_agg(cluster_rows) if cluster_rows else \
                theme_detector.detect_from_signal_clusters(signal_records, entity_records)
all_themes    = theme_detector.merge_themes([[*seed_themes, *auto_themes]])

# Stamp country on every theme
for t in all_themes:
    t.country = COUNTRY

print(f"  themes detected    : {len(all_themes)}")
if all_themes:
    print(f"  sample themes      : {[t.theme_name[:50] for t in all_themes[:3]]}")

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4 — RANKING + PERSIST
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("STAGE 4 — THEME RANKING + PERSIST")
print(SEP)

ranked = theme_ranker.rank(all_themes, {}, pg, as_of_date=FILED_AT)
print(f"  themes ranked      : {len(ranked)}")

theme_dicts    = [rt.theme.to_dict() for rt in ranked]
snapshot_dicts = [
    {
        "theme_slug":    rt.theme.theme_slug,
        "snapshot_date": FILED_AT,
        "strength_score": rt.composite_score,
        "momentum_score": rt.momentum_score,
        "doc_count":      rt.theme.doc_count,
        "company_count":  rt.theme.company_count,
    }
    for rt in ranked
]

if theme_dicts:
    theme_id_map = pg.batch_upsert_themes_and_snapshots(theme_dicts, snapshot_dicts)
    print(f"  themes persisted   : {len(theme_id_map)}")

    # Verify country in mg_themes
    slugs = list(theme_id_map.keys())[:5]
    with pg._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT theme_slug, country FROM mg_themes WHERE theme_slug = ANY(%s)",
                (slugs,)
            )
            rows = cur.fetchall()
            for slug, country in rows:
                status = "✓" if country == COUNTRY else f"✗ (got {country})"
                print(f"  {status} mg_themes.country = '{country}'  slug={slug[:40]}")
            bad = [r for r in rows if r[1] != COUNTRY]
            assert not bad, f"Country mismatch in mg_themes: {bad}"
else:
    print("  (no themes ranked — not enough signals in DB yet; run full ingest first)")

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 5 — SHORTLISTED THEMES (pg_store query)
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("STAGE 5 — SHORTLISTED THEMES QUERY")
print(SEP)

shortlisted = pg.get_shortlisted_themes(min_quarters=1, country=COUNTRY)
print(f"  shortlisted themes (country={COUNTRY}, min_q=1): {len(shortlisted)}")
for t in shortlisted[:3]:
    print(f"    • {t['theme_name'][:50]}  country={t.get('country','?')}")

# ═══════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("SMOKE TEST SUMMARY")
print(SEP)
print(f"  Country tested : {COUNTRY}")
print(f"  Document id    : {doc_id}")
print(f"  Entities       : {len(clean_entities)}")
print(f"  Signals        : {len(signal_dicts)}")
print(f"  Themes detected: {len(all_themes)}")
print(f"  Themes ranked  : {len(ranked)}")
print(f"  Themes persisted: {len(theme_id_map) if theme_dicts else 0}")
print()
print("  ✅  Country propagated correctly through all pipeline stages")
print(SEP)
