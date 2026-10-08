"""Read-only fetch of NSE public announcements + annual reports for one symbol into a local fixture.
Lean variant: each downloaded file is deleted once its text is in the fixture (disk space).
Usage: python fetch_cohort.py SYMBOL DL_ROOT FX_ROOT"""
import collections, glob, hashlib, json, os, subprocess, sys, time, zipfile
from datetime import datetime

SYM, DLR, FXR = sys.argv[1], sys.argv[2], sys.argv[3]
IDX = os.environ.get("NSE_INDEX", "equities")
DL, FX = os.path.join(DLR, SYM), os.path.join(FXR, SYM)
os.makedirs(os.path.join(DL, "raw"), exist_ok=True)
os.makedirs(os.path.join(DL, "txt"), exist_ok=True)
os.makedirs(os.path.join(FX, "texts"), exist_ok=True)
UA = ["-A", "Mozilla/5.0", "-H", "Accept: application/json"]
SKIP = {'Trading Window', 'Certificate under SEBI (Depositories and Participants) Regulations, 2018',
        'Closure of trading window', 'Copy of Newspaper Publication', 'Book Closure',
        'Disclosure under SEBI Takeover Regulations', 'Change in Company Secretary/Compliance Officer',
        'Shareholders meeting', 'Appointment', 'Resignation', 'Cessation', 'Change in Auditors',
        'Change in Management', 'Newspaper Advertisements', 'ESOP/ESOS/ESPS', 'Allotment of ESOP / ESPS',
        'Loss of Share Certificates', 'Duplicate Share Certificate', 'Change in Directorate',
        'Notice of Shareholders Meetings', 'Postal Ballot', 'Reg. 74 (5) of SEBI (DP) Regulations, 2018',
        'Record Date', 'Dividend', 'Change in Registrar and Share Transfer Agent'}


def curl(url, out, timeout=60):
    r = subprocess.run(["curl", "-s", "-m", str(timeout), *UA, "-o", out, "-w", "%{http_code}", url],
                       capture_output=True, text=True)
    return r.stdout



def drop(raw, base):
    import shutil
    for p in (raw, os.path.join(DL, "txt", base + ".txt")):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(os.path.join(DL, "unz", base), ignore_errors=True)


ann = {}
for y in range(2021, 2027):
    for a, b in (("01-01", "30-06"), ("01-07", "31-12")):
        f = os.path.join(DL, f"ann_{y}_{a}.json")
        if not os.path.exists(f) or os.path.getsize(f) < 3:
            for _ in range(3):
                code = curl(f"https://www.nseindia.com/api/corporate-announcements?index={IDX}&symbol={SYM}"
                            f"&from_date={a}-{y}&to_date={b}-{y}", f)
                if code == "200":
                    break
                time.sleep(3)
            time.sleep(1)
        try:
            for x in json.load(open(f)):
                ann[x["seq_id"]] = x
        except Exception as e:
            print(SYM, "bad announcements file", f, str(e)[:80], flush=True)
keep = [x for x in ann.values() if x.get("desc") not in SKIP and x.get("attchmntFile") and x["attchmntFile"] != "-"]
company = next((x.get("sm_name") for x in ann.values() if x.get("sm_name")), SYM)
print(SYM, company, "announcements", len(ann), "kept", len(keep), flush=True)

stats = collections.Counter()
docs = []


def to_text(path, base):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")]
                if not pdfs:
                    return None
                z.extract(pdfs[0], os.path.join(DL, "unz", base))
                path = os.path.join(DL, "unz", base, pdfs[0])
        except Exception:
            return None
    elif ext not in (".pdf",):
        return None
    out = os.path.join(DL, "txt", base + ".txt")
    if not os.path.exists(out):
        subprocess.run(["pdftotext", "-layout", path, out], capture_output=True)
    return out if os.path.exists(out) else None


def add(doc_id, url, title, ftype, dt, txt):
    t = open(txt, errors="replace").read()
    if len(t.split()) < 30:
        stats["skipped_no_text"] += 1
        return
    open(os.path.join(FX, "texts", f"nse{doc_id}.txt"), "w").write(t)
    docs.append({"doc_id": f"nse{doc_id}", "ticker": SYM, "country": "IN", "source_name": "nse_india",
                 "company": company, "title": title, "doc_type": "announcement", "filing_type": ftype, "url": url,
                 "filed_at": dt.date().isoformat(), "published_at": dt.isoformat() + "+05:30",
                 "first_seen_at": dt.isoformat() + "+05:30",
                 "content_hash": hashlib.sha256(t.encode()).hexdigest()[:16],
                 "exported_text_source": "pdftotext_layout", "text_file": f"texts/nse{doc_id}.txt"})
    stats["with_text"] += 1


for x in sorted(keep, key=lambda x: x["seq_id"]):
    u = x["attchmntFile"]
    ext = os.path.splitext(u)[1].lower()
    raw = os.path.join(DL, "raw", x["seq_id"] + ext)
    if not os.path.exists(raw) or os.path.getsize(raw) == 0:
        code = curl(u, raw)
        time.sleep(0.4)
        if code != "200":
            stats["download_failed"] += 1
            continue
    stats["downloaded"] += 1
    txt = to_text(raw, x["seq_id"])
    if not txt:
        stats["not_pdf_or_unreadable"] += 1
        drop(raw, x["seq_id"])
        continue
    add(x["seq_id"], u, x.get("attchmntText") or x["desc"], x["desc"],
        datetime.strptime(x["an_dt"], "%d-%b-%Y %H:%M:%S"), txt)
    drop(raw, x["seq_id"])

# annual reports
arf = os.path.join(DL, "annual_reports.json")
if not os.path.exists(arf):
    curl(f"https://www.nseindia.com/api/annual-reports?index={IDX}&symbol={SYM}", arf)
try:
    ars = json.load(open(arf)).get("data", [])
except Exception:
    ars = []
for a in ars:
    u, fy = a.get("fileName"), f"{a.get('fromYr')}-{a.get('toYr')}"
    if not u or not a.get("toYr") or not (2021 <= int(a["toYr"]) <= 2026):
        continue
    when = a.get("submission_date") or a.get("disseminationDateTime")
    try:
        dt = datetime.strptime(when, "%d-%b-%Y %H:%M:%S")
    except Exception:
        try:
            dt = datetime.strptime(when[:11], "%d-%b-%Y")
        except Exception:
            import re
            m = re.search(r"_(\d{14})", os.path.basename(u))      # NSE file name: _DDMMYYYYHHMMSS
            try:
                dt = datetime.strptime(m.group(1), "%d%m%Y%H%M%S")
                stats["annual_report_date_from_filename"] += 1
            except Exception:
                stats["annual_report_no_date"] += 1
                continue
    base = "AR_" + hashlib.sha1(u.encode()).hexdigest()[:10]
    raw = os.path.join(DL, "raw", base + os.path.splitext(u)[1].lower())
    if not os.path.exists(raw) or os.path.getsize(raw) == 0:
        if curl(u, raw, 180) != "200":
            stats["download_failed"] += 1
            continue
    txt = to_text(raw, base)
    if txt:
        add(base, u, f"Annual Report {fy}", "Annual Report", dt, txt)
        stats["annual_reports"] += 1
    drop(raw, base)

docs.sort(key=lambda d: d["published_at"])
json.dump({"exported": {"source": "NSE public announcements and annual reports, fetched read-only "
                                  f"{datetime.now().date()} for the pre-registered rules-3 cohort",
                        "tickers": [SYM], "since": "2021-01-01", "until": "2025-12-31"},
           "issuers": {SYM: {"name": company}}, "documents": docs},
          open(os.path.join(FX, "export.json"), "w"), indent=1)
json.dump({"symbol": SYM, "announcements": len(ann), "kept": len(keep), **stats,
           "by_year": collections.Counter(d["published_at"][:4] for d in docs)},
          open(os.path.join(FX, "coverage.json"), "w"), indent=1)
print(SYM, "done", dict(stats), flush=True)
