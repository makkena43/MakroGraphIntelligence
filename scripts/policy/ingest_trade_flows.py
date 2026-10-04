#!/usr/bin/env python3
"""
Trade-flow ingester — pulls real monthly India import data (UN Comtrade's
free "preview" endpoint, no API key required) into mg_trade_flows, for the
exact HS codes already curated in mg_import_dependencies.

Why: a company's filing only mentions "we face an import constraint" once
it's already hurting margins, 1-2 quarters after the fact (self-reported,
quarterly cadence at best). DGCI&S-style trade data is monthly and reflects
actual customs flow, not what a company chooses to disclose — a widening
import-dependency ratio for an HS code is a leading indicator, visible
before any filing mentions the underlying constraint.

Source: UN Comtrade preview API (https://comtradeapi.un.org/public/v1/preview),
reporter=India (699), partner=World (0), flow=imports (M), monthly frequency.
This is the free, unauthenticated tier: max 1 period per request, no bulk
history endpoint, and an unpublished (conservative) rate limit -- this
script is deliberately polite (1.2s delay, small per-run fetch budget,
same pattern as scripts/policy/ingest_pib.py) and idempotent (skips
(hs_code, period) pairs already stored before fetching).

Values as returned by the API ("primaryValue") are used as-is (documented
by UN Comtrade as USD in the current API, unlike the deprecated legacy API
which used thousands). Only relative trend (rate of change) is used
downstream, so unit-scale uncertainty does not affect the signal.

Usage:
    python scripts/policy/ingest_trade_flows.py                    # last 24 months, all tracked HS codes
    python scripts/policy/ingest_trade_flows.py --months-back 6    # narrower catch-up window
    python scripts/policy/ingest_trade_flows.py --max-fetches 30   # smaller budget this run
    python scripts/policy/ingest_trade_flows.py --dry-run
"""
import argparse
import os
import sys
import time
from datetime import date

import psycopg2
import psycopg2.extras
import requests

COMTRADE_URL = "https://comtradeapi.un.org/public/v1/preview/C/M/HS"
REPORTER_INDIA = 699
PARTNER_WORLD = 0
FLOW_IMPORT = "M"
MAX_FETCHES_DEFAULT = 60
FETCH_DELAY_SECS = 6.0
TIMEOUT_SECS = 30


def connect():
    return psycopg2.connect(
        host=os.environ.get("MAKROGRAPH_PG_HOST", "localhost"),
        port=int(os.environ.get("MAKROGRAPH_PG_PORT", "5432")),
        dbname=os.environ.get("MAKROGRAPH_PG_DB", "makrograph"),
        user=os.environ.get("MAKROGRAPH_PG_USER", "postgres"),
        password=os.environ.get("MAKROGRAPH_PG_PASSWORD", ""),
    )


def tracked_hs_codes(cur, as_of: date) -> list:
    """Return effective product/HS relationships without a code allowlist.

    Reviewed effective-dated crosswalks are authoritative. Legacy import rows
    are retained only as acquisition fallbacks; downstream observations from
    them remain research context until the crosswalk is reviewed.
    """
    cur.execute("SELECT to_regclass('public.mg_product_hs_crosswalks') AS name")
    has_crosswalk = bool(cur.fetchone()["name"])
    if has_crosswalk:
        cur.execute("""
            SELECT DISTINCT hs_code, NULL::text AS sector,
                   normalized_product AS component
            FROM mg_product_hs_crosswalks
            WHERE country='IN' AND review_status='REVIEWED'
              AND relationship_scope='EXACT'
              AND effective_from <= %s
              AND (effective_to IS NULL OR effective_to >= %s)
            ORDER BY hs_code
        """, (as_of, as_of))
        reviewed = list(cur.fetchall())
        if reviewed:
            rows = reviewed
        else:
            cur.execute("SELECT DISTINCT hs_code, sector, component FROM mg_import_dependencies "
                        "WHERE hs_code IS NOT NULL ORDER BY hs_code")
            rows = list(cur.fetchall())
    else:
        cur.execute("SELECT DISTINCT hs_code, sector, component FROM mg_import_dependencies "
                    "WHERE hs_code IS NOT NULL ORDER BY hs_code")
        rows = list(cur.fetchall())
    seen, out = set(), []
    for r in rows:
        if r["hs_code"] not in seen:
            seen.add(r["hs_code"])
            out.append((r["hs_code"], r["sector"], r["component"]))
    return out


def months_back_list(n: int, lag_months: int = 3) -> list:
    """YYYYMM strings for n months ending `lag_months` before the current month
    (India's customs data typically isn't published for ~2-3 months after the
    fact -- skipping that window avoids burning fetch budget on 'no data yet')."""
    today = date.today()
    y, m = today.year, today.month
    for _ in range(lag_months):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def existing_periods(cur, hs_code: str) -> set:
    cur.execute("""SELECT period FROM mg_trade_flows
                   WHERE reporter_country='India' AND partner_country='World'
                     AND hs_code=%s AND flow_direction='import'""", (hs_code,))
    return {r["period"] for r in cur.fetchall()}


def fetch_period(session, hs_code: str, period: str):
    params = {"reporterCode": REPORTER_INDIA, "period": period,
              "partnerCode": PARTNER_WORLD, "cmdCode": hs_code, "flowCode": FLOW_IMPORT}
    last_exc = None
    for attempt in range(3):
        resp = session.get(COMTRADE_URL, params=params, timeout=TIMEOUT_SECS)
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"  429 rate-limited, backing off {wait}s...", file=sys.stderr)
            time.sleep(wait)
            last_exc = requests.HTTPError("429 after retries")
            continue
        resp.raise_for_status()
        return resp.json()
    raise last_exc


def main():
    ap = argparse.ArgumentParser(description="Ingest UN Comtrade monthly India import data into mg_trade_flows")
    ap.add_argument("--months-back", type=int, default=24, help="how many trailing months to catch up (default 24)")
    ap.add_argument("--lag-months", type=int, default=3, help="skip this many most-recent months (reporting lag)")
    ap.add_argument("--periods", default=None,
                    help="comma-separated explicit YYYYMM periods to fetch instead of a "
                         "trailing window (e.g. for point-in-time historical backfill of "
                         "past report anchors: --periods 202012,202112,202212,202312,202412,202512)")
    ap.add_argument("--max-fetches", type=int, default=MAX_FETCHES_DEFAULT, help="HTTP fetch budget this run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--as-of", default=date.today().isoformat(),
                    help="effective date for reviewed product/HS crosswalks")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    as_of = date.fromisoformat(args.as_of)
    hs_codes = tracked_hs_codes(cur, as_of)
    periods = ([p.strip() for p in args.periods.split(",") if p.strip()]
               if args.periods else months_back_list(args.months_back, args.lag_months))
    print(f"Tracked HS codes: {len(hs_codes)} | catch-up window: {periods[0]}..{periods[-1]}")

    session = requests.Session()
    fetches = inserted = no_data = failed = skipped_existing = 0

    for hs_code, sector, component in hs_codes:
        have = existing_periods(cur, hs_code)
        for period in periods:
            if period in have:
                skipped_existing += 1
                continue
            if fetches >= args.max_fetches:
                print(f"Fetch budget ({args.max_fetches}) reached; stopping.")
                break
            time.sleep(FETCH_DELAY_SECS)
            try:
                payload = fetch_period(session, hs_code, period)
            except requests.RequestException as exc:
                print(f"  FETCH FAILED hs={hs_code} period={period}: {exc}", file=sys.stderr)
                failed += 1
                fetches += 1
                continue
            fetches += 1
            rows = payload.get("data") or []
            if not rows:
                no_data += 1
                print(f"  [{hs_code}] {period} ({component}): no data yet")
                continue
            value_usd = sum((r.get("primaryValue") or 0) for r in rows)
            qty = sum((r.get("qty") or 0) for r in rows)
            qty_unit = next((r.get("qtyUnitCode") for r in rows if r.get("qtyUnitCode")), None)
            print(f"  [{hs_code}] {period} ({component}): value_usd={value_usd:,.0f} qty={qty:,.1f}")
            if args.dry_run:
                continue
            year = int(period[:4])
            with conn.cursor() as wcur:
                wcur.execute(
                    """
                    INSERT INTO mg_trade_flows
                        (reporter_country, partner_country, hs_code, product_name,
                         flow_direction, year, period, value_usd, quantity, quantity_unit,
                         source, fetched_at)
                    VALUES (%s, %s, %s, %s, 'import', %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (reporter_country, partner_country, hs_code, flow_direction, period)
                    DO NOTHING
                    """,
                    ("India", "World", hs_code, component, year, period,
                     value_usd, qty, str(qty_unit) if qty_unit else None,
                     "UN_COMTRADE_PREVIEW"),
                )
                inserted += wcur.rowcount
            conn.commit()
        if fetches >= args.max_fetches:
            break

    conn.close()
    print(f"Done. inserted={inserted} already_present={skipped_existing} "
          f"no_data={no_data} failed={failed} http_fetches={fetches}"
          + (" (dry-run: nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
