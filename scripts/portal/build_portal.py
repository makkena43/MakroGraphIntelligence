#!/usr/bin/env python3
"""
MakroGraph static research-portal generator.

Builds a fully self-contained static site (single index.html, inline CSS,
no JS, no external assets) presenting:
  - the system's approach (hero),
  - the live track record from the append-only decision log `mg_decisions`,
  - forward returns per BUY at 3m / 6m / 12m / since-decision horizons,
    benchmarked against the median of the top-25-turnover stocks on the
    same entry date, with identical window logic,
  - a report library linking the monthly selector PDFs,
  - prominent compliance disclaimers (top and bottom).

Usage:
    python scripts/portal/build_portal.py [--out data/portal/]

Reads ONLY the `makrograph` Postgres DB (tables: mg_decisions,
nse_bhavcopy_data, mg_symbol_renames, security_master) and the
data/reports/ directory. Writes ONLY to the output directory.
It never publishes anything anywhere.
"""

import argparse
import html
import os
import re
import statistics
import sys
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "data", "reports")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "data", "portal")

HORIZONS = [("3m", 91), ("6m", 182), ("12m", 365)]
ENTRY_WINDOW_DAYS = 10       # entry = first close within 10 days after as_of
CENSOR_SLACK_DAYS = 45       # exit >45 days short of horizon target => null
RETURN_FLOOR = -0.95         # returns outside (-95%, +2000%) are data errors
RETURN_CEIL = 20.0
EQUITY_SERIES = ("EQ", "BE")
BENCHMARK_TOP_N = 25

MG_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS mg_decisions (
    id SERIAL PRIMARY KEY,
    as_of DATE NOT NULL,
    country TEXT DEFAULT 'IN',
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    size TEXT,
    weight_pct NUMERIC,
    category TEXT,
    rationale TEXT,
    flips_when TEXT,
    constraint_name TEXT,
    judgment_file TEXT,
    judgment_sha256 TEXT,
    logged_at TIMESTAMPTZ DEFAULT now(),
    source TEXT DEFAULT 'live'
);
"""


def connect():
    return psycopg2.connect(
        host=os.environ.get("MAKROGRAPH_PG_HOST", "localhost"),
        port=int(os.environ.get("MAKROGRAPH_PG_PORT", "5432")),
        dbname=os.environ.get("MAKROGRAPH_PG_DB", "makrograph"),
        user=os.environ.get("MAKROGRAPH_PG_USER", "postgres"),
        password=os.environ.get("MAKROGRAPH_PG_PASSWORD", ""),
    )


# --------------------------------------------------------------------------
# Symbol aliasing (renamed stocks otherwise show broken price histories)
# --------------------------------------------------------------------------

def load_alias_groups(cur):
    """Union-find over confirmed/high-confidence renames -> symbol: full alias set."""
    cur.execute(
        """
        SELECT old_symbol, new_symbol
        FROM mg_symbol_renames
        WHERE (confidence = 'high' OR status = 'confirmed')
          AND status IS DISTINCT FROM 'rejected'
        """
    )
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for old, new in cur.fetchall():
        union(old.strip(), new.strip())

    groups = {}
    for sym in list(parent):
        groups.setdefault(find(sym), set()).add(sym)
    alias_of = {}
    for members in groups.values():
        for sym in members:
            alias_of[sym] = members
    return alias_of


def aliases_for(symbol, alias_of):
    return sorted(alias_of.get(symbol, {symbol}) | {symbol})


# --------------------------------------------------------------------------
# Price series + forward returns
# --------------------------------------------------------------------------

def load_price_series(cur, symbol, alias_of, start=None):
    """date -> close for a symbol (union of its rename aliases).
    On a date with rows for several aliases, the canonical symbol wins."""
    syms = aliases_for(symbol, alias_of)
    params = [syms, list(EQUITY_SERIES)]
    where_start = ""
    if start is not None:
        where_start = "AND trade_date >= %s"
        params.append(start)
    cur.execute(
        f"""
        SELECT trade_date, symbol, close
        FROM nse_bhavcopy_data
        WHERE symbol = ANY(%s) AND series = ANY(%s) AND close > 0
          {where_start}
        ORDER BY trade_date
        """,
        params,
    )
    series = {}
    for d, sym, close in cur.fetchall():
        if d not in series or sym == symbol:
            series[d] = float(close)
    return series


def compute_returns(series, as_of):
    """Forward returns from entry (first close within ENTRY_WINDOW_DAYS after
    as_of) to each horizon and since-decision. Returns dict or None if no entry.
    Horizon is None when right-censored (nearest exit >45d short of target)."""
    if not series:
        return None
    dates = sorted(series)
    entry_date = next(
        (d for d in dates if as_of <= d <= as_of + timedelta(days=ENTRY_WINDOW_DAYS)),
        None,
    )
    if entry_date is None:
        return None
    entry_px = series[entry_date]
    out = {"entry_date": entry_date, "entry_px": entry_px}

    def ret(px):
        r = px / entry_px - 1.0
        return r if RETURN_FLOOR < r < RETURN_CEIL else None

    for name, days in HORIZONS:
        target = entry_date + timedelta(days=days)
        exit_date = None
        for d in dates:
            if entry_date < d <= target:
                exit_date = d
        if exit_date is None or (target - exit_date).days > CENSOR_SLACK_DAYS:
            out[name] = None  # right-censored: never a truncated number
        else:
            out[name] = ret(series[exit_date])

    last_date = dates[-1]
    out["since"] = ret(series[last_date]) if last_date > entry_date else None
    out["since_date"] = last_date if last_date > entry_date else None
    return out


def returns_for_ticker(cur, ticker, as_of, alias_of):
    """Handles basket tickers like 'EXIDEIND + ARE&M' (average of legs;
    a horizon is null unless every leg has a value for it)."""
    legs = [t.strip() for t in ticker.split("+") if t.strip()]
    leg_results = []
    for leg in legs:
        series = load_price_series(cur, leg, alias_of, start=as_of)
        leg_results.append(compute_returns(series, as_of))
    if any(r is None for r in leg_results):
        return None
    if len(leg_results) == 1:
        return leg_results[0]
    combined = {
        "entry_date": max(r["entry_date"] for r in leg_results),
        "entry_px": None,
        "since_date": min(
            (r["since_date"] for r in leg_results if r["since_date"]), default=None
        ),
    }
    for key in [h[0] for h in HORIZONS] + ["since"]:
        vals = [r[key] for r in leg_results]
        combined[key] = sum(vals) / len(vals) if all(v is not None for v in vals) else None
    return combined


# --------------------------------------------------------------------------
# Benchmark: median of top-25-turnover stocks, same window logic
# --------------------------------------------------------------------------

def benchmark_returns(cur, as_of, alias_of, cache={}):
    """Median forward returns of the 25 highest-turnover EQ stocks (present in
    security_master) on the decision's entry date, per horizon."""
    if as_of in cache:
        return cache[as_of]
    cur.execute(
        """
        SELECT min(trade_date) FROM nse_bhavcopy_data
        WHERE trade_date >= %s AND trade_date <= %s
        """,
        (as_of, as_of + timedelta(days=ENTRY_WINDOW_DAYS)),
    )
    entry_date = cur.fetchone()[0]
    if entry_date is None:
        cache[as_of] = None
        return None
    cur.execute(
        """
        SELECT b.symbol
        FROM nse_bhavcopy_data b
        JOIN security_master sm ON sm.nse_symbol = b.symbol
        WHERE b.trade_date = %s AND b.series = 'EQ'
        ORDER BY b.tottrdval DESC NULLS LAST
        LIMIT %s
        """,
        (entry_date, BENCHMARK_TOP_N),
    )
    symbols = [r[0] for r in cur.fetchall()]
    per_horizon = {name: [] for name, _ in HORIZONS}
    per_horizon["since"] = []
    for sym in symbols:
        series = load_price_series(cur, sym, alias_of, start=as_of)
        res = compute_returns(series, as_of)
        if res is None:
            continue
        for key in per_horizon:
            if res.get(key) is not None:
                per_horizon[key].append(res[key])
    result = {
        key: (statistics.median(vals) if vals else None)
        for key, vals in per_horizon.items()
    }
    result["n"] = len(symbols)
    cache[as_of] = result
    return result


# --------------------------------------------------------------------------
# Report library
# --------------------------------------------------------------------------

def find_reports():
    """[(date_str, relative_path_from_portal, filename)] newest first."""
    pattern = re.compile(r"^stock_selector_IN_(\d{4}-\d{2}-\d{2})_v3_report\.pdf$")
    items = []
    if os.path.isdir(REPORTS_DIR):
        for fn in os.listdir(REPORTS_DIR):
            m = pattern.match(fn)
            if m:
                items.append((m.group(1), os.path.join("..", "reports", fn), fn))
    items.sort(reverse=True)
    return items


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def esc(x):
    return html.escape(str(x))


def fmt_ret(r):
    if r is None:
        return '<td class="num na">&mdash;</td>'
    cls = "pos" if r >= 0 else "neg"
    return f'<td class="num {cls}">{r * 100:+.1f}%</td>'


DISCLAIMER_HTML = """
<div class="disclaimer">
  <strong>Important disclaimer.</strong> Everything on this page is research and
  backtest information published for educational purposes only. It is <strong>not
  investment advice</strong>, not a recommendation, and not an offer or solicitation
  to buy or sell any security. Past performance does not guarantee future results;
  returns shown are gross of all costs, taxes and slippage. Investments in
  securities markets are subject to market risks &mdash; read all related documents
  carefully before investing. SEBI registration details: <strong>[PENDING]</strong>.
</div>
"""

def build_html(decision_groups, benchmarks, reports, generated_on, empty_since=None):
    horizon_heads = "".join(
        f"<th class=\"num\">{h}</th>" for h in ["3m", "6m", "12m", "Since decision"]
    )

    if decision_groups:
        rows = []
        for as_of, decisions in decision_groups:
            bm = benchmarks.get(as_of)
            n = len(decisions)
            for i, d in enumerate(decisions):
                res = d["returns"]
                cells = (
                    "".join(fmt_ret(res.get(k)) for k in ["3m", "6m", "12m", "since"])
                    if res
                    else '<td class="num na" colspan="4">no price data yet (entry pending)</td>'
                )
                date_cell = (
                    f'<td class="date" rowspan="{n + 1}">{esc(as_of)}</td>' if i == 0 else ""
                )
                rows.append(
                    f"<tr>{date_cell}<td class=\"tk\">{esc(d['ticker'])}</td>"
                    f"<td class=\"size\">{esc(d['size'] or '')}</td>{cells}</tr>"
                )
            if bm:
                bm_cells = "".join(
                    fmt_ret(bm.get(k)) for k in ["3m", "6m", "12m", "since"]
                )
                rows.append(
                    f'<tr class="bench"><td class="tk">Benchmark &mdash; median of '
                    f"top-{bm['n']} turnover stocks</td><td class=\"size\"></td>{bm_cells}</tr>"
                )
            else:
                rows.append(
                    '<tr class="bench"><td class="tk">Benchmark &mdash; median of '
                    'top-25 turnover stocks</td><td class="size"></td>'
                    '<td class="num na" colspan="4">no price data yet</td></tr>'
                )
        track_record = f"""
<div class="tablewrap">
<table>
  <thead>
    <tr><th>Decision date</th><th>Ticker</th><th>Size</th>{horizon_heads}</tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>
</div>
<p class="footnote">Entry = first close within {ENTRY_WINDOW_DAYS} days after the decision
date. A dash (&mdash;) means the horizon has not completed yet (right-censored) or no
usable price exists &mdash; never a truncated number. Basket positions (e.g. two
tickers joined by "+") show the simple average of the legs' returns. Returns are
price-only, before costs.</p>
"""
    else:
        begins = esc(empty_since) if empty_since else "with the next monthly cycle"
        track_record = f"""
<div class="empty">The decision log begins {begins}. Every future BUY / NO / EXIT
decision is logged append-only with its rationale before outcomes are known, and
forward returns will appear here as they accrue.</div>
"""

    if reports:
        report_items = "".join(
            f'<li><a href="{esc(rel)}">{esc(fn)}</a>'
            f'<span class="rdate">{esc(dt)}</span></li>'
            for dt, rel, fn in reports
        )
        library = f"<ul class=\"reports\">{report_items}</ul>"
    else:
        library = '<div class="empty">No reports published yet.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>MakroGraph Intelligence &mdash; Research &amp; Track Record</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: Georgia, 'Times New Roman', serif;
    color: #1a2332; background: #f7f6f2; line-height: 1.55;
  }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 0 24px 48px; }}
  header {{
    background: #10203a; color: #f2f0ea; padding: 40px 0 34px;
    border-bottom: 4px solid #b08d3e;
  }}
  header .wrap {{ padding-bottom: 0; }}
  h1 {{ font-size: 30px; letter-spacing: 0.5px; font-weight: normal; }}
  h1 .accent {{ color: #d9b96a; }}
  .tagline {{ margin-top: 6px; color: #b9c2d0; font-size: 15px; font-style: italic; }}
  h2 {{
    font-size: 21px; font-weight: normal; margin: 42px 0 14px;
    padding-bottom: 6px; border-bottom: 2px solid #10203a;
  }}
  .hero p {{ margin: 12px 0; font-size: 16.5px; }}
  .hero .kicker {{ font-variant: small-caps; letter-spacing: 1px; color: #b08d3e; }}
  .disclaimer {{
    background: #fdf3e3; border: 2px solid #b08d3e; border-radius: 4px;
    padding: 14px 18px; margin: 26px 0; font-size: 14px;
    font-family: Helvetica, Arial, sans-serif; color: #4a3a18;
  }}
  .tablewrap {{ overflow-x: auto; }}
  table {{
    width: 100%; border-collapse: collapse; margin-top: 10px;
    font-family: Helvetica, Arial, sans-serif; font-size: 13.5px;
    background: #fff; border: 1px solid #d8d4c8;
  }}
  th {{
    text-align: left; background: #10203a; color: #f2f0ea;
    padding: 8px 10px; font-weight: 600; font-size: 12.5px;
  }}
  td {{ padding: 7px 10px; border-top: 1px solid #e6e2d6; vertical-align: top; }}
  td.date {{ white-space: nowrap; font-weight: 600; background: #faf9f4; }}
  td.tk {{ font-weight: 600; }}
  td.size {{ color: #5b6577; font-size: 12.5px; white-space: nowrap; }}
  th.num, td.num {{ text-align: right; white-space: nowrap; }}
  td.pos {{ color: #14691f; }}
  td.neg {{ color: #a01d1d; }}
  td.na {{ color: #9a9484; }}
  tr.bench td {{
    background: #eef1f6; font-style: italic; color: #33415c;
    border-top: 1px solid #c9cfdd;
  }}
  .footnote {{ font-size: 12.5px; color: #6b675c; margin-top: 10px;
    font-family: Helvetica, Arial, sans-serif; }}
  .empty {{
    background: #fff; border: 1px dashed #b8b2a2; border-radius: 4px;
    padding: 22px; color: #6b675c; font-style: italic; margin-top: 10px;
  }}
  ul.reports {{ list-style: none; margin-top: 10px; }}
  ul.reports li {{
    background: #fff; border: 1px solid #d8d4c8; border-radius: 4px;
    padding: 10px 14px; margin-bottom: 8px; display: flex;
    justify-content: space-between; align-items: baseline;
    font-family: Helvetica, Arial, sans-serif; font-size: 14px;
  }}
  ul.reports a {{ color: #10203a; text-decoration: none; border-bottom: 1px solid #b08d3e; }}
  ul.reports a:hover {{ color: #b08d3e; }}
  .rdate {{ color: #8a8574; font-size: 12.5px; }}
  footer {{
    margin-top: 48px; padding-top: 14px; border-top: 1px solid #d8d4c8;
    font-size: 12.5px; color: #8a8574; font-family: Helvetica, Arial, sans-serif;
  }}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>MakroGraph <span class="accent">Intelligence</span></h1>
    <div class="tagline">Constraint-first equity research &mdash; India</div>
  </div>
</header>
<div class="wrap">

{DISCLAIMER_HTML}

<section class="hero">
  <h2>Approach</h2>
  <p class="kicker">How the system works</p>
  <p>We start from physical and supply-side constraints &mdash; capacity gaps,
  import dependencies, bottlenecked products &mdash; and work outward to the
  companies positioned on the right side of them, rather than starting from
  stock charts or factor screens.</p>
  <p>Quantitative screens only generate candidates; every inclusion, sizing and
  exclusion is an explicit, written judgment logged before outcomes are known,
  in an append-only decision log.</p>
  <p>The cycle runs monthly: each report is strictly point-in-time, decisions
  are recorded with their rationale and the conditions that would flip them,
  and forward returns are published here against a same-window benchmark.</p>
</section>

<section>
  <h2>Track record</h2>
  {track_record}
</section>

<section>
  <h2>Report library</h2>
  {library}
</section>

{DISCLAIMER_HTML}

<footer>
  Generated {esc(generated_on)} from the MakroGraph decision log and NSE
  bhavcopy price data. Static page &mdash; no cookies, no scripts, no tracking.
</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build the MakroGraph static portal")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    args = ap.parse_args()

    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(MG_DECISIONS_DDL)

    alias_of = load_alias_groups(cur)

    cur.execute(
        """
        SELECT as_of, ticker, size, weight_pct, category
        FROM mg_decisions
        WHERE action = 'BUY'
        ORDER BY as_of, ticker
        """
    )
    buys = cur.fetchall()

    decision_groups = []   # [(as_of, [ {ticker,size,returns}, ... ])]
    benchmarks = {}
    current = None
    for as_of, ticker, size, weight_pct, category in buys:
        res = returns_for_ticker(cur, ticker, as_of, alias_of)
        if current is None or current[0] != as_of:
            current = (as_of, [])
            decision_groups.append(current)
        current[1].append({"ticker": ticker, "size": size, "returns": res})
    for as_of, _ in decision_groups:
        benchmarks[as_of] = benchmark_returns(cur, as_of, alias_of)

    reports = find_reports()

    empty_since = None
    if not buys:
        cur.execute("SELECT min(as_of) FROM mg_decisions")
        row = cur.fetchone()
        empty_since = str(row[0]) if row and row[0] else None

    page = build_html(
        decision_groups,
        benchmarks,
        reports,
        generated_on=date.today().isoformat(),
        empty_since=empty_since,
    )

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "index.html")
    with open(out_path, "w") as f:
        f.write(page)

    print(f"wrote {out_path}")
    print(f"  BUY decisions: {len(buys)} across {len(decision_groups)} dates")
    print(f"  reports listed: {len(reports)}")
    for as_of, ds in decision_groups:
        for d in ds:
            r = d["returns"]
            if r:
                parts = ", ".join(
                    f"{k}={'—' if r.get(k) is None else f'{r[k]*100:+.1f}%'}"
                    for k in ["3m", "6m", "12m", "since"]
                )
            else:
                parts = "no entry price yet"
            print(f"  {as_of} {d['ticker']:<20} {parts}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
