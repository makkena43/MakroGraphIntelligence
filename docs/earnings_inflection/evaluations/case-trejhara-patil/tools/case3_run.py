import json, os, sys
sys.path.insert(0, "src")
from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
from makrograph.earnings_inflection.replay import snapshot
from makrograph.earnings_inflection.source_repository import FixtureRepository
SYM, FXR, OUT, ASOF = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
pipe = EarningsInflectionPipeline({}, FixtureRepository(os.path.join(FXR, SYM)))
res = pipe.run([SYM], ASOF)
if not res.assessments:
    print("no assessment", res.errors); sys.exit()
snap = snapshot(res.assessments[0])
json.dump(snap, open(os.path.join(OUT, f"{SYM}_{ASOF}.json"), "w"), indent=1, default=str)
print(json.dumps({k: v for k, v in snap.items() if k not in ("numeric_signals",)}, default=str)[:3000])
print("NUMERIC SIGNALS")
for s in snap.get("numeric_signals", []):
    print(" ", s)
