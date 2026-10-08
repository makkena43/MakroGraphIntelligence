"""Read-only fetch of NSE results XBRL filings for one symbol into an existing fixture directory.
Usage: python fetch_xbrl.py SYMBOL FX_ROOT [FROM_YEAR TO_YEAR]

Writes FX_ROOT/SYMBOL/xbrl/*.xml and FX_ROOT/SYMBOL/xbrl_index.json ({"documents": [...]}), one document
per XBRL file, dated at the exchange's dissemination time.  Nothing else in the fixture is changed."""
import json, os, re, subprocess, sys, time
from datetime import datetime

SYM, FXR = sys.argv[1], sys.argv[2]
Y0, Y1 = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (2019, 2025)
FX = os.path.join(FXR, SYM)
IDX = os.environ.get("NSE_INDEX", "equities")
XD = os.path.join(FX, "xbrl")
os.makedirs(XD, exist_ok=True)
UA = ["-A", "Mozilla/5.0", "-H", "Accept: application/json"]


def curl(url, out, timeout=60):
    r = subprocess.run(["curl", "-s", "-m", str(timeout), *UA, "-o", out, "-w", "%{http_code}", url],
                       capture_output=True, text=True)
    return r.stdout


entries = []
for per in ("Quarterly", "Half-Yearly", "Annual"):
    api = os.path.join(XD, f"api_{per}.json")
    if not os.path.exists(api) or os.path.getsize(api) < 3:
        for _ in range(3):
            if curl(f"https://www.nseindia.com/api/corporates-financial-results?index={IDX}&symbol={SYM}"
                    f"&period={per}", api) == "200":
                break
            time.sleep(3)
    try:
        entries += json.load(open(api))
    except Exception as e:                      # noqa: BLE001
        print(SYM, per, "no results list", str(e)[:80])


def iso(s):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%S+05:30")
        except (TypeError, ValueError):
            continue
    return None


docs, stats = [], {"entries": len(entries), "in_window": 0, "fetched": 0, "failed": 0, "no_xbrl": 0}
for e in entries:
    try:
        year = int(e["toDate"][-4:])
    except (KeyError, ValueError):
        continue
    if not Y0 <= year <= Y1:
        continue
    stats["in_window"] += 1
    url = e.get("xbrl") or ""
    m = re.search(r"/([A-Za-z0-9_\-]+)\.xml$", url)
    if not m or not url.startswith("https://nsearchives.nseindia.com/"):
        stats["no_xbrl"] += 1
        continue
    out = os.path.join(XD, m.group(1) + ".xml")
    if not os.path.exists(out) or os.path.getsize(out) < 200:
        code = curl(url, out)
        time.sleep(0.5)
        if code != "200" or os.path.getsize(out) < 200:
            stats["failed"] += 1
            continue
    stats["fetched"] += 1
    when = iso(e.get("exchdisstime")) or iso(e.get("broadCastDate")) or iso(e.get("filingDate"))
    docs.append({"doc_id": f"xbrl_{m.group(1)}", "ticker": SYM, "source_name": "nse_xbrl", "doc_type": "xbrl_results",
                 "filing_type": "XBRL Financial Results",
                 "title": f"{e.get('consolidated', '')} results {e.get('fromDate', '')} to {e.get('toDate', '')}",
                 "url": url, "published_at": when, "text_file": f"xbrl/{m.group(1)}.xml",
                 "api": {k: e.get(k) for k in ("consolidated", "fromDate", "toDate", "relatingTo", "audited",
                                               "cumulative", "reInd", "filingDate", "broadCastDate", "exchdisstime")}})
# integrated filings (SEBI, from the December 2024 quarter): results XBRL in the in-capmkt taxonomy
ifa = os.path.join(XD, "api_integrated.json")
if not os.path.exists(ifa) or os.path.getsize(ifa) < 3:
    for _ in range(3):
        if curl(f"https://www.nseindia.com/api/integrated-filing-results?index={IDX}&symbol={SYM}"
                f"&page=1&size=100", ifa) == "200":
            break
        time.sleep(3)
try:
    integ = json.load(open(ifa)).get("data", [])
except Exception:                           # noqa: BLE001
    integ = []
stats["integrated_entries"] = len(integ)
for e in integ:
    if e.get("type") != "Integrated Filing- Financials":
        continue
    try:
        year = int((e.get("qe_Date") or "")[-4:])
    except ValueError:
        continue
    if not Y0 <= year <= Y1:
        continue
    stats["in_window"] += 1
    url = e.get("xbrl") or ""
    m = re.search(r"/([A-Za-z0-9_\-]+)\.xml$", url)
    if not m or not url.startswith("https://nsearchives.nseindia.com/"):
        stats["no_xbrl"] += 1
        continue
    out = os.path.join(XD, m.group(1) + ".xml")
    if not os.path.exists(out) or os.path.getsize(out) < 200:
        code = curl(url, out)
        time.sleep(0.5)
        if code != "200" or os.path.getsize(out) < 200:
            stats["failed"] += 1
            continue
    stats["fetched"] += 1
    stats["integrated_fetched"] = stats.get("integrated_fetched", 0) + 1
    when = iso(e.get("broadcast_Date")) or iso(e.get("creation_Date"))
    docs.append({"doc_id": f"xbrl_{m.group(1)}", "ticker": SYM, "source_name": "nse_xbrl", "doc_type": "xbrl_results",
                 "filing_type": "XBRL Integrated Filing - Financials",
                 "title": f"{e.get('consolidated', '')} integrated filing, quarter ended {e.get('qe_Date', '')} "
                          f"({e.get('type_Sub', '')})",
                 "url": url, "published_at": when, "text_file": f"xbrl/{m.group(1)}.xml",
                 "api": {k: e.get(k) for k in ("consolidated", "qe_Date", "audited", "type_Sub", "revised_Date",
                                               "broadcast_Date", "creation_Date")}})
json.dump({"documents": docs, "xbrl_fetch": stats}, open(os.path.join(FX, "xbrl_index.json"), "w"), indent=1)
print(SYM, json.dumps(stats), flush=True)
