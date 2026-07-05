#!/usr/bin/env python3
"""Backfill new signal types into existing nlp_done documents.

Runs ONLY the 4 new signal patterns against documents that are already
processed (nlp_done / embedded / graph_built). Does NOT:
  - reset processing_status
  - re-extract entities
  - touch any table other than mg_signals

New signal types added to signal_extractor.py:
  capacity_shortage        — utilization pressure, fully booked, backlogs
  localization_opportunity — PLI eligibility, import substitution, Make in India
  tender_pipeline          — L1 bidder, tender wins, SECI/Railways tenders
  policy_support           — budget allocations, VGF, PM schemes

Usage:
  python3 scripts/backfill_new_signals.py --years 2020 2021
  python3 scripts/backfill_new_signals.py --years 2020 2021 --dry-run
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_signals")

sys.path.insert(0, ".")

import yaml
with open("config/settings.yaml") as f:
    config = yaml.safe_load(f)
try:
    with open("config/secrets.json") as f:
        secrets = json.load(f)
    for s, v in secrets.items():
        if isinstance(v, dict):
            config.setdefault(s, {}).update({k: vv for k, vv in v.items() if vv})
except Exception as e:
    logger.warning(f"secrets.json: {e}")

# ── Argument parsing ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--years", nargs="+", type=int, default=[2020, 2021],
                    help="Years to backfill (e.g. 2020 2021)")
parser.add_argument("--batch-size", type=int, default=500)
parser.add_argument("--dry-run", action="store_true",
                    help="Extract signals but do not write to DB")
args = parser.parse_args()

# ── New signal patterns only — compiled here so we don't rerun ALL patterns ──
from src.makrograph.nlp.signal_extractor import _RAW_PATTERNS, _extract_theme_entity

NEW_SIGNAL_TYPES = frozenset({
    "capacity_shortage", "localization_opportunity",
    "tender_pipeline", "policy_support",
})

import re as _re
NEW_PATTERNS = [
    (_re.compile(raw, _re.IGNORECASE), sig_type, direction, conf)
    for raw, sig_type, direction, conf in _RAW_PATTERNS
    if sig_type in NEW_SIGNAL_TYPES
]
logger.info(f"Loaded {len(NEW_PATTERNS)} new signal patterns for: {sorted(NEW_SIGNAL_TYPES)}")

# ── Pipeline init ─────────────────────────────────────────────────────────────
from src.makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
pipeline = IntelligencePipeline(config)
pipeline._init_storage()

if not pipeline._pg_store:
    logger.error("PostgreSQL not available")
    sys.exit(1)

pg = pipeline._pg_store
project_root = Path(config.get("storage", {}).get(
    "project_root", Path(__file__).resolve().parent.parent
))

# ── Per-year execution ────────────────────────────────────────────────────────
overall_start = time.time()
total_docs = 0
total_new_signals = 0
total_skipped = 0

CONTEXT_WINDOW = 200
MAX_TEXT = 80_000

for year in args.years:
    win_start = date(year, 1, 1)
    win_end   = date(year, 12, 31)

    logger.info(f"\n{'='*58}")
    logger.info(f"  Backfilling {year}  ({win_start} → {win_end})")
    logger.info(f"{'='*58}")

    yr_start  = time.time()
    yr_docs   = 0
    yr_signals = 0

    # Fetch docs in batches — all statuses post-nlp (nlp_done, embedded, graph_built)
    offset = 0
    while True:
        with pg._conn() as conn:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, company, ticker, raw_text, local_path,
                           title, filed_at, country
                    FROM mg_documents
                    WHERE country = 'IN'
                      AND processing_status IN ('nlp_done','embedded','graph_built')
                      AND filed_at BETWEEN %s AND %s
                    ORDER BY filed_at
                    LIMIT %s OFFSET %s
                """, (win_start, win_end, args.batch_size, offset))
                docs = [dict(r) for r in cur.fetchall()]

        if not docs:
            break

        batch_signals = []

        for doc in docs:
            doc_id   = doc["id"]
            filed_at = doc.get("filed_at")

            # Resolve text — DB raw_text first, then local file, then title
            text = (doc.get("raw_text") or "").strip()
            if not text:
                lp_str = doc.get("local_path") or ""
                if lp_str and lp_str != "UNSUPPORTED_FORMAT":
                    lp = Path(lp_str) if Path(lp_str).is_absolute() else project_root / lp_str
                    if lp.exists():
                        try:
                            sfx = lp.suffix.lower()
                            if sfx == ".pdf":
                                from src.makrograph.parser.pdf_parser import PDFParser
                                pr = PDFParser(config.get("parser", {})).parse(lp)
                                text = pr.text if pr.success else ""
                            else:
                                text = lp.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            pass
            if not text:
                text = (doc.get("title") or "").strip()
            if not text:
                total_skipped += 1
                continue

            scan = text[:MAX_TEXT]

            # Check which new signal types already exist for this doc (skip duplicates)
            with pg._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT signal_type FROM mg_signals "
                        "WHERE document_id = %s AND signal_type = ANY(%s)",
                        (doc_id, list(NEW_SIGNAL_TYPES)),
                    )
                    already = {r[0] for r in cur.fetchall()}

            for compiled, sig_type, direction, conf in NEW_PATTERNS:
                if sig_type in already:
                    continue   # already backfilled for this doc
                for match in compiled.finditer(scan):
                    start = match.start()
                    ctx_s = max(0, start - CONTEXT_WINDOW // 2)
                    ctx_e = min(len(scan), match.end() + CONTEXT_WINDOW // 2)
                    ctx   = scan[ctx_s:ctx_e].strip()
                    entity = _extract_theme_entity(ctx)
                    batch_signals.append({
                        "document_id": doc_id,
                        "entity_id":   None,
                        "signal_type": sig_type,
                        "direction":   direction,
                        "confidence":  conf,
                        "signal_value": None,
                        "signal_unit":  None,
                        "context_text": ctx[:500],
                        "extracted_by": "rule_backfill",
                        "filed_at":    filed_at,
                        "country":     "IN",
                    })
                    already.add(sig_type)   # avoid duplicate within same doc

        yr_docs   += len(docs)
        total_docs += len(docs)

        if batch_signals and not args.dry_run:
            try:
                pg.batch_insert_signals(batch_signals)
            except Exception as e:
                logger.warning(f"Batch insert failed, falling back: {e}")
                for sd in batch_signals:
                    try:
                        pg.insert_signal(sd)
                    except Exception:
                        pass

        yr_signals    += len(batch_signals)
        total_new_signals += len(batch_signals)

        logger.info(
            f"  {year} offset={offset}  docs={len(docs)}  "
            f"new_signals={len(batch_signals)}  "
            f"(year total so far: {yr_docs} docs / {yr_signals} signals)"
        )

        offset += args.batch_size

    logger.info(
        f"  {year} complete in {round(time.time()-yr_start,1)}s  "
        f"docs={yr_docs}  new_signals={yr_signals}"
        + ("  [DRY RUN — nothing written]" if args.dry_run else "")
    )

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*58}")
print(f"  BACKFILL COMPLETE  total={round(time.time()-overall_start,1)}s")
print(f"{'='*58}")
print(f"  Docs processed    : {total_docs:,}")
print(f"  New signals added : {total_new_signals:,}")
print(f"  Docs skipped      : {total_skipped:,} (no text)")
print(f"  Years             : {args.years}")
if args.dry_run:
    print("  MODE              : DRY RUN — nothing written to DB")
