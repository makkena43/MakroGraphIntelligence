import json, sys
sys.path.insert(0, "src")
import makrograph.earnings_inflection.pipeline as P
from makrograph.earnings_inflection.contracts import Metric
from makrograph.earnings_inflection.source_repository import FixtureRepository
P.detect_catalysts = lambda *a, **k: []          # coverage only: detection not needed here
FX, OUT = sys.argv[1], sys.argv[2]
res = {}
for S in sys.argv[3:]:
    p = P.EarningsInflectionPipeline({"xbrl_results": True}, FixtureRepository(f"{FX}/{S}"))
    p.run([S], "2025-12-31"); s = p.last_series
    q = {m.value: sorted(str(e) for (mm, pt, e) in s.points if mm == m and pt == "Q")
         for m in (Metric.REVENUE, Metric.EBITDA, Metric.PAT, Metric.PAT_ATTRIBUTABLE)}
    res[S] = {"scope": s.scope.value, **{k: len(v) for k, v in q.items()}, "revenue_quarters": q["revenue"]}
    print(S, {k: v for k, v in res[S].items() if k != "revenue_quarters"}, flush=True)
json.dump(res, open(OUT, "w"), indent=1)
