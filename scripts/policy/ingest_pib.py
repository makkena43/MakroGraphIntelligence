#!/usr/bin/env python3
"""
PIB Policy-Announcement Ingester — pulls recent Press Information Bureau
press releases into mg_policy_announcements, with a scheme-likeness score.

Why: government scheme announcements (PLI schemes, mandates, approved-list
regimes) hit pib.gov.in months before they show up in company filings.
This script is the day-0 feed for the SCHEME-FIRST playbook in the
makrograph-stock-selector skill.

Usage:
    python scripts/policy/ingest_pib.py                     # ingest latest
    python scripts/policy/ingest_pib.py --since 2026-07-01  # only newer rows
    python scripts/policy/ingest_pib.py --dry-run           # print, no insert

Source endpoints (verified working 2026-07-20):
    RSS (English, all-ministries releases):
        https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3&reg=3
      NOTE: the `reg=3` param is required — without it PIB 301-redirects to
      the Hindi feed (Lang=2). Items carry only <title> and <link>
      (PressReleaseIframePage.aspx?PRID=...); no pubDate, so each release
      page must be fetched for date/ministry/body.
    Release page markup:
        ministry  -> class "MinistryNameSubhead"
        date      -> class "ReleaseDateSubHeaddateTime"  ("19 JUL 2026 9:02PM by PIB Delhi")
        title     -> first <h2>
        body      -> class "innner-page-main-about-us-content-right-part"

Politeness: max ~30 HTTP fetches per run, 1s delay between requests,
browser User-Agent, 30s timeouts. Already-ingested URLs are skipped
before fetching, so repeated runs only spend budget on new releases.
"""

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime

import psycopg2
import requests
from bs4 import BeautifulSoup

RSS_URL = "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3&reg=3"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_FETCHES = 30          # total HTTP requests per run (incl. the RSS fetch)
FETCH_DELAY_SECS = 1.0    # polite gap between requests
TIMEOUT_SECS = 30

# ---------------------------------------------------------------------------
# Scheme-likeness scorer: 5 signal families, 1 point each (score 0-5).
# Keyword lists are linguistic patterns only — no company/stock names.
# ---------------------------------------------------------------------------
SCHEME_SIGNALS = {
    "incentive_linked": [
        "production linked", "production-linked", "pli scheme",
        "incentive scheme", "turnover linked", "turnover-linked",
        "outlay of rs", "outlay of ₹", "financial outlay",
        "capital subsidy", "viability gap funding",
    ],
    "import_substitution": [
        "import substitution", "atmanirbhar", "aatmanirbhar",
        "self-reliance", "self reliant", "domestic manufacturing",
        "domestic production", "domestic value addition", "make in india",
        "indigenous", "indigenisation", "indigenization",
        "reduce import", "reducing imports", "import dependence",
    ],
    "trade_barrier": [
        "customs duty", "basic customs duty", "import duty",
        "anti-dumping", "safeguard duty", "almm", "approved list",
        "approved models and manufacturers", "quality control order",
        "qco", "bis certification", "tariff",
    ],
    "mandate": [
        "mandatory", "mandated", "shall be required", "shall be mandatory",
        "notification", "notified", "gazette", "compliance shall",
        "it shall be obligatory", "directed that", "phased manner",
    ],
    "eligibility_threshold": [
        "eligibility", "eligible applicant", "eligible under",
        "minimum investment", "threshold investment", "incremental investment",
        "applicant", "application window", "selected under the scheme",
        "beneficiaries under", "empanelment",
    ],
    "draft_stage": [
        "draft rules", "draft notification", "draft scheme", "draft order",
        "draft bill", "draft policy", "draft guidelines", "draft amendment",
        "released for stakeholder", "for stakeholder consultation",
        "for public consultation", "public consultation", "invites comments",
        "invite comments", "invited comments", "seeks comments",
        "comments are invited", "comments/suggestions", "in-principle approval",
        "in principle approval", "consultation paper", "pre-application conference",
        "proposed to notify", "proposal to introduce", "draft cabinet note",
    ],
}

# Finalization language -- distinguishes a rule that has actually taken
# effect from a draft that merely mentions "notification"/"mandatory" in
# describing what the draft WOULD do. Checked only when draft_stage did not
# already fire (a release can say both "draft notification" and "notified"
# in different sentences; draft wording takes precedence since it means the
# rule is not yet binding).
FINAL_STAGE_SIGNALS = [
    "notified vide", "vide notification no", "hereby notifies", "gazette of india",
    "with effect from", "notification dated", "in supersession of",
    "come into force", "shall come into effect", "hereby notified",
]


def classify_stage(text_lower: str) -> str:
    """draft -> pre-finalization stage visible months before filings mention it;
    notified -> the rule has actually taken effect; unclear -> neither pattern hit
    (may still be a live scheme mentioned in passing, just not classifiable here)."""
    if any(kw in text_lower for kw in SCHEME_SIGNALS["draft_stage"]):
        return "draft"
    if any(kw in text_lower for kw in FINAL_STAGE_SIGNALS):
        return "notified"
    return "unclear"


def connect():
    return psycopg2.connect(
        host=os.environ.get("MAKROGRAPH_PG_HOST", "localhost"),
        port=int(os.environ.get("MAKROGRAPH_PG_PORT", "5432")),
        dbname=os.environ.get("MAKROGRAPH_PG_DB", "makrograph"),
        user=os.environ.get("MAKROGRAPH_PG_USER", "postgres"),
        password=os.environ.get("MAKROGRAPH_PG_PASSWORD", ""),
    )


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mg_policy_announcements (
                id SERIAL PRIMARY KEY,
                published_date DATE,
                ministry TEXT,
                title TEXT NOT NULL,
                url TEXT UNIQUE,
                raw_text TEXT,
                scheme_score INT DEFAULT 0,
                scheme_signals TEXT[],
                stage TEXT DEFAULT 'unclear',
                fetched_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            "ALTER TABLE mg_policy_announcements ADD COLUMN IF NOT EXISTS stage TEXT DEFAULT 'unclear'"
        )
    conn.commit()


def score_scheme(title: str, body: str):
    """Return (score 0-6, [signal names hit], stage) from keyword families."""
    text = f"{title}\n{body}".lower()
    hits = []
    for signal, keywords in SCHEME_SIGNALS.items():
        if any(kw in text for kw in keywords):
            hits.append(signal)
    stage = classify_stage(text)
    return len(hits), hits, stage


def canonical_url(link: str) -> str:
    """Normalise any PIB release link to a stable PRID-based URL."""
    m = re.search(r"PRID=(\d+)", link, re.I)
    if m:
        return f"https://pib.gov.in/PressReleasePage.aspx?PRID={m.group(1)}"
    return link.strip()


def fetch(session: requests.Session, url: str) -> requests.Response:
    # The RSS links point at bare pib.gov.in, which intermittently times out;
    # www.pib.gov.in serves the same pages reliably.
    url = url.replace("://pib.gov.in/", "://www.pib.gov.in/")
    last_exc = None
    for attempt in range(2):
        try:
            resp = session.get(url, timeout=TIMEOUT_SECS, allow_redirects=True)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(FETCH_DELAY_SECS)
    raise last_exc


def parse_rss(xml_bytes: bytes):
    """Yield (title, link) from the PIB RSS feed (handles UTF-8 BOM)."""
    if xml_bytes.startswith(b"\xef\xbb\xbf"):
        xml_bytes = xml_bytes[3:]
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            yield title, link


def parse_release_page(html: str):
    """Extract (published_date, ministry, title, body_text) from a release page."""
    soup = BeautifulSoup(html, "html.parser")

    ministry = None
    node = soup.find(class_="MinistryNameSubhead")
    if node:
        ministry = node.get_text(strip=True) or None

    published = None
    node = soup.find(class_="ReleaseDateSubHeaddateTime")
    if node:
        m = re.search(r"(\d{1,2}\s+[A-Z]{3}\s+\d{4})", node.get_text(" ", strip=True), re.I)
        if m:
            try:
                published = datetime.strptime(m.group(1).upper(), "%d %b %Y").date()
            except ValueError:
                published = None

    title = None
    node = soup.find("h2")
    if node:
        title = node.get_text(" ", strip=True) or None

    body = ""
    node = soup.find(class_="innner-page-main-about-us-content-right-part")
    if node:
        body = node.get_text(" ", strip=True)

    return published, ministry, title, body


def main():
    ap = argparse.ArgumentParser(description="Ingest PIB press releases into mg_policy_announcements")
    ap.add_argument("--since", help="Only keep releases published on/after YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be inserted; no DB writes")
    args = ap.parse_args()

    since = None
    if args.since:
        since = datetime.strptime(args.since.replace("/", "-"), "%Y-%m-%d").date()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    fetches = 0

    print(f"Fetching RSS: {RSS_URL}")
    rss = fetch(session, RSS_URL)
    fetches += 1
    items = list(parse_rss(rss.content))
    print(f"RSS items: {len(items)}")

    conn = connect()
    ensure_table(conn)

    # Skip URLs already ingested so the fetch budget goes to new releases.
    urls = [canonical_url(link) for _, link in items]
    existing = set()
    if urls:
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM mg_policy_announcements WHERE url = ANY(%s)", (urls,))
            existing = {r[0] for r in cur.fetchall()}

    inserted = skipped_existing = skipped_since = failed = 0

    for rss_title, link in items:
        url = canonical_url(link)
        if url in existing:
            skipped_existing += 1
            continue
        if fetches >= MAX_FETCHES:
            print(f"Fetch budget ({MAX_FETCHES}) reached; stopping.")
            break

        time.sleep(FETCH_DELAY_SECS)
        try:
            resp = fetch(session, link)
        except requests.RequestException as exc:
            print(f"  FETCH FAILED {url}: {exc}", file=sys.stderr)
            failed += 1
            fetches += 1
            continue
        fetches += 1

        published, ministry, page_title, body = parse_release_page(resp.text)
        title = page_title or rss_title

        if since and published and published < since:
            skipped_since += 1
            continue

        score, signals, stage = score_scheme(title, body)
        tag = f"score={score} signals={','.join(signals) or '-'} stage={stage}"
        print(f"  [{published}] {ministry or '?'} | {title[:90]} | {tag}")

        if args.dry_run:
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mg_policy_announcements
                    (published_date, ministry, title, url, raw_text,
                     scheme_score, scheme_signals, stage)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
                """,
                (published, ministry, title, url, body, score, signals, stage),
            )
            inserted += cur.rowcount
        conn.commit()

    conn.close()
    print(
        f"Done. inserted={inserted} already_present={skipped_existing} "
        f"before_since={skipped_since} failed={failed} http_fetches={fetches}"
        + (" (dry-run: nothing written)" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
