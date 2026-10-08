import os, sys
from collections import defaultdict
sys.path.insert(0, "src")
from makrograph.earnings_inflection.contracts import Metric
from makrograph.earnings_inflection.source_repository import FixtureRepository
from makrograph.earnings_inflection.xbrl_results import is_xbrl_document, parse_xbrl_results
SYM, FX = sys.argv[1], sys.argv[2]
repo = FixtureRepository(os.path.join(FX, SYM))
M = [Metric.REVENUE, Metric.OTHER_INCOME, Metric.EBITDA, Metric.FINANCE_COST, Metric.PBT, Metric.PAT, Metric.PAT_ATTRIBUTABLE]
t = defaultdict(dict)
for d in repo._docs:
    if not is_xbrl_document(d): continue
    rows, iss = parse_xbrl_results(d)
    for r in rows:
        if r.segment or r.metric not in M: continue
        k = (r.scope.value, r.period_type, r.period_end)
        if r.metric in t[k] and t[k][r.metric][1] and r.available_at and t[k][r.metric][1] <= r.available_at: continue
        t[k][r.metric] = (r.value, r.available_at, r.integrity)
for k in sorted(t, key=lambda k: (k[0], k[1], k[2])):
    if k[1] not in ("Q", "H", "FY"): continue
    v = t[k]; pub = min((x[1] for x in v.values() if x[1]), default=None)
    print(f"{k[0][:4]} {k[1]:2s} {k[2]}  pub {str(pub)[:10]}  " + "  ".join(
        f"{m.value[:7]}={v[m][0]:8.2f}{'?' if v[m][2]=='unresolved' else ''}" for m in M if m in v))
