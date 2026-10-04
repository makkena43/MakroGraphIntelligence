#!/usr/bin/env python3
"""One-command monthly PMS pipeline (Tier-1 ops, Jul-2026).

    run_monthly.py [--as-of YYYY-MM-DD] [--country IN] [--force]

Steps, with hard gates:
  0. DATA FRESHNESS GATES — abort loudly if inputs are stale (a PMS must
     never discover stale data by accident):
       - nse_bhavcopy_data max(trade_date) within 7 days of as_of (IN)
       - mg_documents max(filed_at) within 21 days of as_of
       - mg_india_beneficiaries latest snapshot within 120 days (IN)
     Override with --force (the override itself is printed in the report gate
     log — visible, never silent).
  1. SCAN     — select_stocks.py (full extraction, reference layer, screens)
  2. JUDGMENT — requires stock_selector_<CC>_<date>_judgment.json to exist AND
     contain a `decision` block; if missing, exits with instructions (the
     judgment layer is Claude via the makrograph-stock-selector skill — it is
     the one step that cannot and must not be mechanized).
  3. PORTFOLIO — portfolio_construct.py (weights, caps, cash floor)
  4. RENDER   — render_selector_html.py + html_to_pdf.py
  5. LOG      — log_decisions.py (append-only mg_decisions track record)

Exit codes: 0 ok, 2 judgment missing, 3 freshness gate failed, 4 step failed.
"""
import argparse
import datetime as dt
import os
import subprocess
import sys

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from extract_report_data import connect, REPORTS_DIR  # noqa: E402

PY = sys.executable


def gate_freshness(cur, as_of, country, force):
    checks = []
    if country == "IN":
        cur.execute("SELECT MAX(trade_date) AS d FROM nse_bhavcopy_data")
        px = cur.fetchone()["d"]
        checks.append(("bhavcopy prices", px, 7))
    cur.execute("SELECT MAX(filed_at)::date AS d FROM mg_documents WHERE country=%s", (country,))
    docs = cur.fetchone()["d"]
    checks.append(("filings corpus", docs, 21))
    if country == "IN":
        cur.execute("SELECT MAX(as_of_date) AS d FROM mg_india_beneficiaries")
        snap = cur.fetchone()["d"]
        checks.append(("beneficiary mapping snapshot", snap, 120))

    failed = []
    print("── Freshness gates ──")
    for name, latest, max_lag in checks:
        lag = (as_of - latest).days if latest else 99999
        ok = lag <= max_lag
        print(f"  {'OK ' if ok else 'FAIL'} {name}: latest {latest} ({lag}d before as-of, limit {max_lag}d)")
        if not ok:
            failed.append(name)
    if failed and not force:
        print(f"\nGATE FAILED: {', '.join(failed)} stale. Refresh the data pipeline or rerun with --force.")
        sys.exit(3)
    if failed and force:
        print(f"\n!! GATES OVERRIDDEN with --force for: {', '.join(failed)} — noted visibly.")
    return failed


def run(cmd, step):
    print(f"── {step} ──\n  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(HERE)))
    if r.returncode != 0:
        print(f"STEP FAILED: {step} (exit {r.returncode})")
        sys.exit(4)


def run_soft(cmd, step):
    """Best-effort step: a failure warns and continues rather than aborting the
    run. For the leading-indicator feeds, which depend on external endpoints
    (PIB, UN Comtrade) — a feed being down should degrade detection freshness,
    not block the client report."""
    print(f"── {step} (best-effort) ──\n  $ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(HERE)),
                           timeout=1800)
        if r.returncode != 0:
            print(f"  !! {step} exit {r.returncode} — continuing without fresh feed")
    except Exception as e:
        print(f"  !! {step} failed ({e}) — continuing without fresh feed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--country", default="IN", choices=["IN", "US"])
    ap.add_argument("--skip-feeds", action="store_true",
                    help="skip the leading-indicator + reference-snapshot steps "
                         "(e.g. offline, or backfilling a historical date)")
    ap.add_argument("--force", action="store_true",
                    help="override freshness gates (logged visibly)")
    args = ap.parse_args()
    as_of = dt.date.fromisoformat(args.as_of)

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    gate_freshness(cur, as_of, args.country, args.force)
    conn.close()

    data_json = os.path.join(REPORTS_DIR, f"stock_selector_{args.country}_{as_of}_data.json")
    judgment_json = os.path.join(REPORTS_DIR, f"stock_selector_{args.country}_{as_of}_judgment.json")
    html = os.path.join(REPORTS_DIR, f"stock_selector_{args.country}_{as_of}_v3_report.html")

    # ── 0b. LEADING INDICATORS (India only) — run BEFORE the scan so the fresh
    # draft-stage PIB rows and trade-flow momentum are available to it. These
    # detect a constraint 2-4 quarters before filings mention it (filings-based
    # detection is inherently late). Network-dependent and best-effort: a feed
    # being down must not block the report, so failures warn and continue.
    POLICY = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts", "policy")
    if args.country == "IN" and not args.skip_feeds:
        run_soft([PY, os.path.join(POLICY, "ingest_pib.py")],
                 "0b-i. PIB draft/scheme feed")
        run_soft([PY, os.path.join(POLICY, "ingest_trade_flows.py"), "--months-back", "3",
                  "--as-of", str(as_of)],
                 "0b-ii. trade-flow momentum feed")
        # Audit structural reference coverage. This command is intentionally
        # read-only: it never restamps an old estimate as a fresh observation.
        run_soft([PY, os.path.join(POLICY, "snapshot_reference.py"),
                  "--as-of", str(as_of)],
                 "0b-iii. reference-table snapshot")
    if not args.skip_feeds:
        run_soft([PY, os.path.join(POLICY, "snapshot_company_policy_signals.py"),
                  "--as-of", str(as_of), "--country", args.country],
                 "0b-iv. broad company-policy discovery snapshot")

    # India keeps a dated recall vocabulary before building the stricter role
    # ledger. The capability output is discovery-only and has no position
    # authority; it prevents newly disclosed short product heads from being
    # lost because the last capability snapshot is stale.
    if args.country == "IN":
        run([PY, os.path.join(HERE, "company_capabilities.py"),
             "--as-of", str(as_of)],
            "0c-0. dated company capability vocabulary")

    # Establish literal product-to-self identities before role extraction so
    # a newly discovered product does not spend one cycle artificially
    # unlinked. This creates WATCH/UNMEASURED coverage only.
    run([PY, os.path.join(POLICY, "bootstrap_constraint_coverage.py"),
         "--as-of", str(as_of), "--country", args.country],
        "0c-0b. pre-role product-chain coverage")

    # Build the issuer-first product-role view before either country selector.
    # It proves what companies make; it does not infer that the product is
    # constrained or give the stock position authority.
    run([PY, os.path.join(HERE, "company_product_roles.py"),
         "--as-of", str(as_of), "--country", args.country],
        "0c-i. company product-role ledger")

    run([PY, os.path.join(POLICY, "bootstrap_constraint_coverage.py"),
         "--as-of", str(as_of), "--country", args.country],
        "0c-ii. post-role product-chain research coverage")

    # Coverage alone only tells us what products exist. Ingest reviewed source
    # feed packets, then collect dated observations immediately afterwards.
    # India additionally uses its legacy
    # capacity/import/mapper references; every country uses filing timing
    # evidence. Exact local issuer observations may be machine accepted as
    # binding/resolution evidence; only source-qualified physical observations
    # can supply a capacity/import measurement.
    run([PY, os.path.join(POLICY, "ingest_primary_constraint_observations.py"),
         "--as-of", str(as_of), "--country", args.country],
        "0c-iii. reviewed primary constraint feeds")
    run([PY, os.path.join(POLICY, "queue_constraint_observations.py"),
         "--as-of", str(as_of), "--country", args.country],
        "0c-iv. constraint evidence collection")
    run([PY, os.path.join(POLICY, "materialize_constraint_ledger.py"),
         "--as-of", str(as_of), "--country", args.country],
        "0c-v. immutable observation -> constraint-state ledger")
    # Materialise the country-scoped ingestion/NLP -> constraint -> company
    # bridge. US uses ledger/NLP/policy evidence and cannot read the legacy
    # India-only gap/import tables.
    run([PY, os.path.join(POLICY, "snapshot_constraint_candidates.py"),
         "--as-of", str(as_of), "--country", args.country],
        "0c-vi. constraint/company candidate snapshot")

    scan_cmd = [PY, os.path.join(HERE, "select_stocks.py"), "--as-of", str(as_of),
                "--country", args.country, "--out", data_json]
    if args.country == "IN":
        # The scheduled research refresh may spend time on the separate
        # whole-market moonshot sleeve; an ad-hoc decision scan deliberately
        # does not block on it.
        scan_cmd.extend(["--include-moonshot", "--include-novel-policy-discovery",
                         "--include-coverage-audit", "--include-historical-cohort-study",
                         "--include-mapping-artifact-audit"])
    run(scan_cmd, "1. SCAN")

    if not os.path.exists(judgment_json):
        print(f"""
── 2. JUDGMENT — REQUIRED, NOT FOUND ──
The scan is done: {data_json}
Now run the judgment layer (Claude, makrograph-stock-selector skill) to produce:
  {judgment_json}
It must contain the full sidecar incl. the `decision` block (BUY/NO/exits).
Then re-run this command — it will resume from the portfolio step.""")
        sys.exit(2)
    import json
    with open(judgment_json) as f:
        if not (json.load(f).get("decision") or {}):
            print(f"JUDGMENT INCOMPLETE: {judgment_json} has no `decision` block. "
                  f"Finish the judgment pass, then re-run.")
            sys.exit(2)
    print(f"── 2. JUDGMENT — found {os.path.basename(judgment_json)} with decision block ──")

    run([PY, os.path.join(HERE, "portfolio_construct.py"), judgment_json, data_json],
        "3. PORTFOLIO")
    run([PY, os.path.join(HERE, "render_selector_html.py"), data_json], "4a. RENDER HTML")
    run([PY, os.path.join(HERE, "html_to_pdf.py"), html], "4b. RENDER PDF")
    run([PY, os.path.join(HERE, "log_decisions.py"), judgment_json, "--source", "live"],
        "5. DECISION LOG")

    print(f"""
── DONE ──
Report (client-facing, decision-only): {html.replace('.html', '.pdf')}
Decisions logged to mg_decisions (append-only).

Internal judgment-layer checklist — read from {data_json}, NOT the report
(these are review queues for the analyst, never client-facing):
  1. novel_policy_vocabulary   — review all terms; promote/reject in mg_tracked_schemes
  2. scheme_graduation_candidates — same
  3. reference_data.symbol_renames_pending_review — confirm/reject in mg_symbol_renames
  4. reference_data.chain_classifications (any "unclassified") — classify in mg_chain_classifications
  5. exclusion_proposals       — confirm into mg_manual_exclusions
  6. mapping_artifacts         — factor into pure-play-leg judgment before any BUY""")


if __name__ == "__main__":
    main()
