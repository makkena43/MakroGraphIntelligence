"""End-to-end India pipeline test — single company (INFY), last 90 days.

Runs every stage:
  1. Ingest   — NSE per-symbol fetch + Screener.in annual reports/concalls
  2. NLP      — entity + signal extraction
  3. Graph    — supply-chain graph (Neo4j optional)
  4. Themes   — theme detection + beneficiaries
  5. Ranking  — constraint-quality ranking engine

Reports pass / warn / fail at each stage so you know exactly what to fix
before running the full 2020-present India backfill.

Usage:
    cd /Users/makkenasrinivas/PycharmProjects/MakroGraphIntelligence
    .venv/bin/python test_india_e2e.py
"""

import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("india_e2e")

import yaml

# ── Test config — single company, last 90 days ──────────────────────────────
SYMBOL      = "INFY"
SINCE_DAYS  = 90
SINCE_DT    = datetime.now(timezone.utc) - timedelta(days=SINCE_DAYS)
START_DATE  = SINCE_DT.strftime("%Y-%m-%d")

def _sep(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def load_config():
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    # Force India market + single-company overrides for this test
    cfg["market"] = {"country": "IN"}
    cfg["neo4j"]  = {**cfg.get("neo4j", {}), "enabled": False}  # skip Neo4j for test
    # Override each India source to INFY only and reasonable limits
    cfg["nse"] = {
        **cfg.get("nse", {}),
        "symbol_list": [SYMBOL],
        "start_date":  START_DATE,
        "max_results_per_run": 0,
        "api_delay_seconds": 0.8,
    }
    cfg["bse"] = {
        **cfg.get("bse", {}),
        "scrip_list":  [],     # INFY BSE code = 500209; leave empty for cookie-free test
        "start_date":  START_DATE,
        "use_selenium": False,
        "max_results_per_run": 0,
    }
    cfg["screener"] = {
        **cfg.get("screener", {}),
        "symbol_list": [SYMBOL],
        "start_date":  START_DATE,
        "max_results_per_run": 0,
        "api_delay_seconds": 1.0,
    }
    # Disable heavy sources for this focused test
    cfg["pib"]           = {**cfg.get("pib", {}),           "enabled": False}
    cfg["invest_india"]  = {**cfg.get("invest_india", {}),  "enabled": False}
    cfg["commerce_india"]= {**cfg.get("commerce_india", {}), "enabled": False}
    cfg["sebi"]          = {**cfg.get("sebi", {}),           "enabled": False}
    cfg["rbi"]           = {**cfg.get("rbi", {}),            "enabled": False}
    return cfg

# ── Stage results tracker ────────────────────────────────────────────────────
results = {}

def record(stage: str, status: str, detail: str = "", data=None):
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}.get(status, "?")
    results[stage] = {"status": status, "detail": detail, "data": data}
    print(f"  {icon} {stage}: {detail}")


def main():
    _sep(f"INDIA E2E TEST — {SYMBOL}  |  since {START_DATE}  ({SINCE_DAYS} days)")

    cfg = load_config()

    # ── Import pipeline ──────────────────────────────────────────────────────
    try:
        from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
    except Exception as exc:
        record("import", "FAIL", str(exc))
        return

    with IntelligencePipeline(cfg) as pipeline:

        # ── Storage init ─────────────────────────────────────────────────────
        _sep("STAGE 0 — Storage Init")
        try:
            pipeline._init_storage()
            pg = pipeline._pg_store
            if pg:
                record("pg_connect", "PASS", f"PostgreSQL connected: {cfg['postgresql']['dbname']}")
            else:
                record("pg_connect", "FAIL", "PGStore is None — check postgresql config")
                return
        except Exception as exc:
            record("pg_connect", "FAIL", str(exc))
            return

        # ── Stage 1: Ingest ──────────────────────────────────────────────────
        _sep(f"STAGE 1 — Ingest  ({SYMBOL})")
        t0 = time.time()
        try:
            ingest_stats = pipeline.run_ingest_india(since=SINCE_DT)
            elapsed = time.time() - t0
            fetched = ingest_stats.get("docs_fetched", 0)
            stored  = ingest_stats.get("docs_stored", 0)
            skipped = ingest_stats.get("docs_skipped", 0)
            print(f"  Fetched={fetched}  Stored={stored}  Skipped={skipped}  Time={elapsed:.1f}s")
            if stored > 0:
                record("ingest", "PASS", f"{stored} docs stored, {skipped} deduped")
            elif fetched > 0:
                record("ingest", "WARN", f"Fetched {fetched} but stored 0 — all deduped or parse failed")
            else:
                record("ingest", "WARN", f"Nothing fetched — API geo-blocked? check NSE/Screener logs")
        except Exception as exc:
            record("ingest", "FAIL", str(exc))
            logger.exception("Ingest failed")

        # ── Stage 2: NLP ─────────────────────────────────────────────────────
        _sep("STAGE 2 — NLP (entity + signal extraction)")
        try:
            pipeline._init_nlp()
            t0 = time.time()
            nlp_stats = pipeline.run_nlp(batch_size=200)
            elapsed = time.time() - t0
            processed = nlp_stats.get("docs_processed", 0)
            signals   = nlp_stats.get("signals_found", 0)
            entities  = nlp_stats.get("entities_found", 0)
            print(f"  Processed={processed}  Entities={entities}  Signals={signals}  Time={elapsed:.1f}s")
            if processed > 0 and signals > 0:
                record("nlp", "PASS", f"{processed} docs, {signals} signals, {entities} entities")
            elif processed > 0:
                record("nlp", "WARN", f"{processed} docs but 0 signals — check NLP patterns for India text")
            else:
                record("nlp", "WARN", "0 docs processed — ingest may have stored 0")
        except Exception as exc:
            record("nlp", "FAIL", str(exc))
            logger.exception("NLP failed")

        # ── Stage 3: Embeddings ───────────────────────────────────────────────
        _sep("STAGE 3 — Embeddings")
        try:
            emb_stats = pipeline.run_embeddings(batch_size=100)
            embedded = emb_stats.get("docs_embedded", 0)
            if embedded > 0:
                record("embeddings", "PASS", f"{embedded} docs embedded")
            else:
                record("embeddings", "WARN", "0 docs embedded (sentence-transformers installed?)")
        except Exception as exc:
            record("embeddings", "WARN", f"Embeddings skipped: {exc}")

        # ── Stage 4: Graph ────────────────────────────────────────────────────
        _sep("STAGE 4 — Graph (supply-chain, Neo4j disabled for test)")
        try:
            graph_stats = pipeline.run_graph()
            nodes = graph_stats.get("nodes_created", 0)
            edges = graph_stats.get("edges_created", 0)
            record("graph", "PASS", f"nodes={nodes}  edges={edges}")
        except Exception as exc:
            record("graph", "WARN", f"Graph skipped: {exc}")

        # ── Stage 5: Themes ───────────────────────────────────────────────────
        _sep("STAGE 5 — Theme detection + beneficiaries")
        try:
            pipeline._init_themes()
            theme_stats = pipeline.run_themes()
            themes_found  = theme_stats.get("themes_detected", 0)
            beneficiaries = theme_stats.get("beneficiaries_linked", 0)
            print(f"  Themes={themes_found}  Beneficiaries={beneficiaries}")
            if themes_found > 0:
                record("themes", "PASS", f"{themes_found} themes, {beneficiaries} beneficiary links")
            else:
                record("themes", "WARN", "0 themes — need more signals or lower min_eligibility_score")
        except Exception as exc:
            record("themes", "FAIL", str(exc))
            logger.exception("Themes failed")

        # ── Stage 6: Ranking ──────────────────────────────────────────────────
        _sep("STAGE 6 — Constraint-quality ranking")
        try:
            from makrograph.ranking import RankingEngine
            # RankingEngine takes a PGStore, not a config dict — reuse the pipeline's store
            engine = RankingEngine(pipeline._pg_store)
            date_to   = datetime.now(timezone.utc).date()
            date_from = SINCE_DT.date()
            result    = engine.run(date_from=date_from, date_to=date_to,
                                   top_n_themes=15, country="IN")
            # run() returns (theme_scores, stock_rankings) — unpack accordingly
            if isinstance(result, tuple):
                theme_scores, stock_rankings = result
                rankings = stock_rankings
            else:
                rankings = result   # backwards-compat if signature changes
            infy_rank = next((r for r in rankings if r.ticker == SYMBOL), None)
            print(f"  Total ranked: {len(rankings)}")
            if rankings:
                record("ranking", "PASS", f"{len(rankings)} companies ranked")
                print(f"\n  Top 10 rankings:")
                for i, r in enumerate(rankings[:10], 1):
                    cname = getattr(r, "company_name", getattr(r, "company", ""))
                    print(f"    {i:>2}. {r.ticker:<12} {cname[:30]:<30}  score={r.final_score:.3f}  cq={r.constraint_quality:.3f}")
                if infy_rank:
                    pos = next(i for i, r in enumerate(rankings, 1) if r.ticker == SYMBOL)
                    print(f"\n  {SYMBOL} position: #{pos}  score={infy_rank.final_score:.3f}")
            else:
                record("ranking", "WARN", "0 companies ranked — themes may be empty")
        except Exception as exc:
            record("ranking", "FAIL", str(exc))
            logger.exception("Ranking failed")

    # ── Summary ───────────────────────────────────────────────────────────────
    _sep("SUMMARY")
    passes  = sum(1 for v in results.values() if v["status"] == "PASS")
    warns   = sum(1 for v in results.values() if v["status"] == "WARN")
    fails   = sum(1 for v in results.values() if v["status"] == "FAIL")

    for stage, r in results.items():
        icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[r["status"]]
        print(f"  {icon}  {stage:<20} {r['detail'][:70]}")

    print(f"\n  PASS={passes}  WARN={warns}  FAIL={fails}")

    if fails == 0 and warns == 0:
        print("\n  🎉 All stages passed — safe to run full India backfill from 2020.")
    elif fails == 0:
        print("\n  ⚠️  Warnings present — review above before full backfill.")
    else:
        print("\n  ❌  Failures present — fix before running full backfill.")


if __name__ == "__main__":
    main()
