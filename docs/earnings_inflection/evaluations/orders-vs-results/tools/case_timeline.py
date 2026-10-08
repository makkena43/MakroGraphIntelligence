"""Document-led evidence vs the numbers, per company.  Usage (repo root):
PYTHONPATH=src python case_timeline.py FX_ROOT OUT_DIR SYMBOL...

Numbers (XBRL, dated at dissemination): quarterly revenue and profit; the first 'visible' inflection =
first quarter whose TTM profit is >= 1.5x the TTM a year earlier (positive base) and stays >= 1.25x it
for the next two published quarters.
Documents (every filing, dated at publication): order values and order-book levels found by the
grammar-based amount-role extractor, compared with the TTM revenue known on that date.
A 'document signal' is the first date on which either
  - the latest stated order book is >= 1.5x TTM revenue and >= 1.3x the book stated 9-15 months earlier, or
  - order values announced in the trailing 12 months sum to >= 1.0x TTM revenue.
Thresholds fixed before the timelines were looked at."""
import json, os, re, sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from makrograph.earnings_inflection.amount_roles import AMT, roles
from makrograph.earnings_inflection.contracts import Metric, Scope
from makrograph.earnings_inflection.source_repository import FixtureRepository
from makrograph.earnings_inflection.xbrl_results import is_xbrl_document, parse_xbrl_results

FX, OUT, SYMS = sys.argv[1], sys.argv[2], sys.argv[3:]
ORDERISH = re.compile(r"\border|backlog|contract|loa\b|letter of (?:award|acceptance)|work order", re.I)


def crore(text):
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)", text[re.search(r"\d", text).start():])
    v = float(m.group(1).replace(",", ""))
    u = m.group(2).lower()
    return v * (0.01 if u.startswith(("lakh", "lac")) else 0.1 if u.startswith(("mn", "million")) else
                100 if u.startswith(("bn", "billion")) else 1.0)


def qprev(d, n=1):
    y, m = d.year, d.month
    for _ in range(n):
        y, m = (y, m - 3) if m > 3 else (y - 1, m + 9)
    return date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m])


for sym in SYMS:
    repo = FixtureRepository(os.path.join(FX, sym))
    docs = repo._docs
    # ---- numbers from XBRL: chosen scope = consolidated if it has more quarters
    q = {Scope.CONSOLIDATED: defaultdict(dict), Scope.STANDALONE: defaultdict(dict)}
    for d in docs:
        if not is_xbrl_document(d):
            continue
        rows, _ = parse_xbrl_results(d)
        for r in rows:
            if r.segment or r.period_type != "Q" or r.integrity == "unresolved" or r.scope not in q:
                continue
            if r.metric in (Metric.REVENUE, Metric.PAT, Metric.PAT_ATTRIBUTABLE):
                cur = q[r.scope][r.period_end].get(r.metric)
                if cur is None or (r.available_at and cur[1] and r.available_at < cur[1]):
                    q[r.scope][r.period_end][r.metric] = (r.value, r.available_at)
    scope = max(q, key=lambda s: len([e for e in q[s] if Metric.REVENUE in q[s][e]]))
    Q = q[scope]
    ends = sorted(e for e in Q if Metric.REVENUE in Q[e])

    def profit(e):
        x = Q.get(e, {})
        p = x.get(Metric.PAT_ATTRIBUTABLE) if scope == Scope.CONSOLIDATED else None
        return (p or x.get(Metric.PAT) or (None, None))

    def ttm(e, metric):
        vals = []
        for k in range(4):
            x = Q.get(qprev(e, k), {})
            v = (profit(qprev(e, k)) if metric == "profit" else x.get(Metric.REVENUE, (None, None)))[0]
            if v is None:
                return None
            vals.append(v)
        return sum(vals)

    def published(e):
        return Q[e][Metric.REVENUE][1]

    path = [(e, ttm(e, "rev"), ttm(e, "profit"), published(e)) for e in ends]
    visible_list, last_i = [], -10
    for i, (e, r, p, pub) in enumerate(path):
        p_ly = ttm(qprev(e, 4), "profit")
        if p is None or p_ly is None or p_ly <= 0 or p < 1.5 * p_ly or i - last_i <= 4:
            continue
        nxt = [x for x in path[i + 1:i + 3]]
        if len(nxt) == 2 and all(x[2] is not None and x[2] >= 1.25 * p_ly for x in nxt):
            visible_list.append({"quarter": str(e), "published": str(pub.date()) if pub else None,
                                 "ttm_profit_year_ago": round(p_ly, 1), "ttm_profit": round(p, 1)})
            last_i = i

    def ttm_rev_known(t):
        known = [x for x in path if x[3] and x[3] <= t and x[1] is not None]
        return known[-1][1] if known else None

    # ---- documents: order values and order-book levels
    events = []
    for d in docs:
        if is_xbrl_document(d) or not d.published_at:
            continue
        text = re.sub(r"\s+", " ", d.full_text() or "")
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text):
            if len(s) > 500 or not AMT.search(s) or not ORDERISH.search(s):
                continue
            try:
                rs = roles(s)
            except Exception:                                   # noqa: BLE001
                continue
            for m, role in zip(AMT.finditer(s), rs):
                if role in ("book_level", "order_value", "inflow_total"):
                    events.append({"date": d.published_at, "role": role, "crore": round(crore(m.group(0)), 1),
                                   "doc": d.doc_id, "type": (d.filing_type or "")[:40], "quote": s[:220]})
    events.sort(key=lambda x: x["date"])
    # de-duplicate the same order value repeated within 45 days (press release + Reg-30 + presentation)
    seen, orders = [], []
    for ev in events:
        if ev["role"] != "order_value":
            continue
        if any(abs(ev["crore"] - o["crore"]) <= 0.01 * ev["crore"] and (ev["date"] - o["date"]).days <= 45 for o in seen):
            continue
        seen.append(ev)
        orders.append(ev)
    books = [ev for ev in events if ev["role"] == "book_level"]
    # monthly timeline of the document view
    timeline, signals, prev_true = [], [], None
    if path:
        tz = path[0][3].tzinfo if path[0][3] else None
        months = []
        for y in range(2021, 2026):
            for m in range(1, 13):
                nxt = date(y + (m == 12), m % 12 + 1, 1)
                months.append(datetime(nxt.year, nxt.month, 1, tzinfo=tz) - timedelta(seconds=1))
        for t in months:
            rev = ttm_rev_known(t)
            inflow = sum(o["crore"] for o in orders if t - timedelta(days=365) < o["date"] <= t)
            bk = [b for b in books if b["date"] <= t and (t - b["date"]).days <= 200]
            book = bk[-1]["crore"] if bk else None
            old = [b for b in books if 270 <= (t - b["date"]).days <= 460]
            book_ly = old[-1]["crore"] if old else None
            timeline.append({"date": str(t.date()), "ttm_revenue": round(rev, 1) if rev else None,
                             "orders_12m": round(inflow, 1), "book": book, "book_year_ago": book_ly})
            if rev:
                why = []
                if book and book >= 1.5 * rev and book_ly and book >= 1.3 * book_ly:
                    why.append(f"order book {book:,.0f} cr = {book / rev:.1f}x TTM revenue, up from {book_ly:,.0f}")
                if inflow >= 1.0 * rev:
                    why.append(f"orders announced in 12 months {inflow:,.0f} cr = {inflow / rev:.1f}x TTM revenue")
                if why:
                    if prev_true is None or (t - prev_true).days > 183:
                        signals.append({"date": str(t.date()), "why": why})
                    prev_true = t
    for v in visible_list:
        pubd = date.fromisoformat(v["published"]) if v["published"] else None
        prior = [g for g in signals if pubd and 0 <= (pubd - date.fromisoformat(g["date"])).days <= 548]
        v["document_signal"] = prior[0] if prior else None
        v["lead_days"] = (pubd - date.fromisoformat(prior[0]["date"])).days if prior else None
    out = {"symbol": sym, "scope": scope.value, "quarters": len(ends),
           "inflections": visible_list, "document_signals": signals,
           "ttm_path": [(str(e), None if r is None else round(r, 1), None if p is None else round(p, 1),
                         str(pub.date()) if pub else None) for e, r, p, pub in path],
           "orders": [{**o, "date": str(o["date"].date())} for o in orders],
           "books": [{**b, "date": str(b["date"].date())} for b in books],
           "timeline": timeline}
    json.dump(out, open(os.path.join(OUT, f"{sym}_timeline.json"), "w"), indent=1, default=str)
    print(sym, scope.value, len(ends), "inflections", visible_list, "signals", [g["date"] for g in signals],
          "orders", len(orders), "books", len(books), flush=True)
