"""Score PDF results-table reading against validated XBRL figures, for two text extractions:
pdftotext -layout (the fixture texts) and pdfplumber word layout (re-read from the raw PDF).

Usage: python pdf_vs_xbrl.py SYMBOL FX_ROOT DL_ROOT OUT_JSON
A PDF filing is matched to the XBRL figures disseminated within 3 days of it; each validated XBRL
figure (company level) is scored as agree / disagree / missing for each extraction."""
import glob, json, os, sys
from datetime import datetime, timedelta

sys.path[:0] = ["src"]
from makrograph.earnings_inflection.chunking import chunk_document          # noqa: E402
from makrograph.earnings_inflection.contracts import Metric, SourceDocument  # noqa: E402
from makrograph.earnings_inflection.extraction import parse_results_tables  # noqa: E402
from makrograph.earnings_inflection.source_repository import FixtureRepository  # noqa: E402
from makrograph.earnings_inflection.xbrl_results import is_xbrl_document, parse_xbrl_results  # noqa: E402
from makrograph.parser.pdf_layout import layout_pages  # noqa: E402

SYM, FXR, DLR, OUT = sys.argv[1:5]
METRICS = (Metric.REVENUE, Metric.OTHER_INCOME, Metric.TOTAL_EXPENSES, Metric.DEPRECIATION, Metric.FINANCE_COST,
           Metric.PBT, Metric.TAX, Metric.PAT, Metric.PAT_ATTRIBUTABLE)
repo = FixtureRepository(os.path.join(FXR, SYM))
docs = repo._docs
xdocs = [d for d in docs if is_xbrl_document(d)]
truth = {}                                   # (metric, ptype, end, scope) -> (value, time)
for d in xdocs:
    rows, _ = parse_xbrl_results(d)
    for r in rows:
        if r.integrity == "validated" and not r.segment and r.metric in METRICS:
            truth.setdefault((r.metric, r.period_type, r.period_end, r.scope), (r.value, d.published_at))
meta = {d["doc_id"]: d for d in json.load(open(os.path.join(FXR, SYM, "export.json")))["documents"]}
RESULTS = ("financial result", "outcome of board", "integrated filing")
pdfs = [d for d in docs if not is_xbrl_document(d) and any(k in (d.filing_type or "").lower() for k in RESULTS)]


def raw_pdf(doc_id):
    seq = doc_id[3:]
    p = os.path.join(DLR, SYM, "raw", seq + ".pdf")
    if os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(DLR, SYM, "unz", seq, "*.pdf")) + glob.glob(os.path.join(DLR, SYM, "unz", seq, "**", "*.pdf"))
    return hits[0] if hits else None


def parse(doc_id, when, text, pages=None):
    d = SourceDocument(doc_id=doc_id, source_name="t", ticker=SYM, text=text, pages=pages, published_at=when)
    un = []
    rows, _ = parse_results_tables(d, chunk_document(d), unscaled=un)
    return rows, un


Z = lambda: {"agree": 0, "agree_unit_unread": 0, "disagree": 0, "missing": 0}  # noqa: E731
score = {"pdftotext": Z(), "pdfplumber": Z()}
SCALES = (1.0, 0.01, 0.1, 1e-4, 1e-7)
per_metric = {}
examples = {"pdftotext": [], "pdfplumber": []}
scored_docs = 0
for d in pdfs:
    if not d.published_at:
        continue
    keys = [k for k, (v, t) in truth.items() if t and abs((t - d.published_at).total_seconds()) <= 3 * 86400]
    if not keys:
        continue
    raw = raw_pdf(d.doc_id)
    if not raw:
        continue
    try:
        pages = layout_pages(raw)
    except Exception as e:                      # noqa: BLE001
        print(SYM, d.doc_id, "pdfplumber failed", str(e)[:80], flush=True)
        continue
    scored_docs += 1
    found = {"pdftotext": parse(d.doc_id, d.published_at, d.full_text()),
             "pdfplumber": parse(d.doc_id, d.published_at, "\f".join(pages), pages)}
    for name, (rows, un) in found.items():
        got, gun = {}, {}
        for r in rows:
            got.setdefault((r.metric, r.period_type, r.period_end, r.scope), []).append(r.value)
        for r in un:
            gun.setdefault((r.metric, r.period_type, r.period_end, r.scope), []).append(r.value)
        for k in keys:
            v = truth[k][0]
            vals = got.get(k, [])
            uv = gun.get(k, [])
            if not vals and any(abs(x * sc - v) <= max(0.05, 0.01 * abs(v)) for x in uv for sc in SCALES):
                res = "agree_unit_unread"
            elif not vals and not uv:
                res = "missing"
            elif not vals:
                res = "disagree"
                vals = uv
            elif any(abs(x - v) <= max(0.05, 0.01 * abs(v)) for x in vals):
                res = "agree"
            else:
                res = "disagree"
                if len(examples[name]) < 15:
                    examples[name].append(f"{d.doc_id} {k[0].value} {k[1]} {k[2]} {k[3].value}: {vals[:3]} vs XBRL {v:.2f}")
            score[name][res] += 1
            pm = per_metric.setdefault(k[0].value, {}).setdefault(name, Z())
            pm[res] += 1
out = {"symbol": SYM, "xbrl_filings": len(xdocs), "xbrl_validated_figures": len(truth), "pdf_results_filings": len(pdfs),
       "scored_pdf_filings": scored_docs, "score": score, "per_metric": per_metric, "examples": examples}
json.dump(out, open(OUT, "w"), indent=1)
print(SYM, json.dumps({"scored": scored_docs, **score}), flush=True)
