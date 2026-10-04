# Policy ingestion scripts

## ingest_pib.py — PIB press-release ingester

Fetches the latest Press Information Bureau press releases (English RSS,
all ministries) into the `mg_policy_announcements` table in the
`makrograph` DB, and scores each announcement 0-5 for "scheme-likeness"
based on five keyword signal families:

1. `incentive_linked` — production/turnover-linked incentive language ("production linked", "incentive scheme", "outlay of Rs")
2. `import_substitution` — "import substitution", "atmanirbhar", "domestic manufacturing"
3. `trade_barrier` — "customs duty", "ALMM", "approved list", QCO/BIS
4. `mandate` — "mandatory", "shall be required", "notification"
5. `eligibility_threshold` — "eligibility", "minimum investment", "applicant"

The score plus the list of signals hit are stored in `scheme_score` /
`scheme_signals`. Rows are upserted by URL (`ON CONFLICT DO NOTHING`),
and already-ingested URLs are skipped before any HTTP fetch, so repeat
runs are cheap (1 request if nothing is new).

### Running it

```bash
.venv/bin/python scripts/policy/ingest_pib.py                     # latest releases
.venv/bin/python scripts/policy/ingest_pib.py --since 2026-07-01  # only newer
.venv/bin/python scripts/policy/ingest_pib.py --dry-run           # print, no writes
```

Run it **at least monthly** (weekly or daily is better — the RSS feed
only carries the ~20 most recent releases, so infrequent runs miss
announcements). It is polite by design: max ~30 HTTP fetches per run,
1s delay between requests, browser User-Agent.

Endpoint notes (verified 2026-07-20): the English feed is
`https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3&reg=3` —
the `reg=3` param is required or PIB redirects to the Hindi feed. RSS
items carry only title+link, so each release page is fetched for
date/ministry/body. Release links on bare `pib.gov.in` intermittently
time out; the script rewrites them to `www.pib.gov.in`.

### How it feeds the scheme-first playbook

This table is the day-0 input for the **"3b-pre SCHEME-FIRST PLAYBOOK"**
in `.claude/skills/makrograph-stock-selector/SKILL.md`. Government
scheme announcements (PLI schemes, KAVACH-style mandates, ALMM-style
approved-list regimes) appear on PIB months before they surface in
company filings — whoever reads them first owns the early trades.

At selector time, query for recent high-scoring announcements:

```sql
SELECT published_date, ministry, title, url, scheme_score, scheme_signals
FROM mg_policy_announcements
WHERE scheme_score >= 3
  AND published_date >= <as-of date - 6 months>
ORDER BY scheme_score DESC, published_date DESC;
```

Announcements with `scheme_score >= 3` are the day-0 candidates the
judgment layer then scores on the playbook's 5-question scorecard
(the keyword score is only a screen — the judgment layer decides).
Lower-scoring rows (1-2) are still useful as weak signals when several
from the same ministry cluster around one product area.

### `stage` column — draft vs notified (Jul-2026)

A 6th keyword family, `draft_stage`, catches pre-finalization language
("draft notification", "for stakeholder consultation", "invites comments",
"in-principle approval", "pre-application conference") that the original
5 families missed entirely — e.g. "Draft Advocates (Amendment) Bill...
Released for Stakeholder and Public Consultation" scored **0** before this
change. `classify_stage()` tags each row `draft` / `notified` / `unclear`
(draft wording wins if both draft and finalization language appear, since
a draft is by definition not yet binding).

This matters because a draft QCO or PLI amendment is public 2-4 quarters
before any company's filing mentions compliance with it (nothing is
mandatory yet, so nobody discloses it). `select_stocks.py`'s
`compute_regulatory_watchlist()` surfaces `stage='draft'` rows as a
**judgment-review-only watchlist** (`regulatory_watchlist` in the report
JSON) — never client-facing, never auto-scored, since a draft can be
watered down or dropped. Same separation as `novel_policy_vocabulary`.

## ingest_trade_flows.py — UN Comtrade monthly import-flow ingester

Pulls real monthly India import data (free, unauthenticated UN Comtrade
"preview" endpoint) into `mg_trade_flows`, for exactly the HS codes already
curated in `mg_import_dependencies` (no hardcoded HS-code list — add a row
to that table to track a new one). This is the DGCI&S-style leading
indicator: trade flow is monthly customs reality, not a company's
self-reported quarterly disclosure, so a widening import-dependency trend
shows up here 1-2+ quarters before any filing mentions the constraint.

```bash
.venv/bin/python scripts/policy/ingest_trade_flows.py                   # last 24 months, all tracked HS codes
.venv/bin/python scripts/policy/ingest_trade_flows.py --months-back 12
.venv/bin/python scripts/policy/ingest_trade_flows.py --max-fetches 30  # smaller budget this run
.venv/bin/python scripts/policy/ingest_trade_flows.py --dry-run
```

Notes:
- One HTTP request per (HS code, month) — the free tier caps at 1 period
  per request. Politeness: 6s delay, 429 backoff+retry (10/20/30s), small
  per-run fetch budget (default 200) — same pattern as `ingest_pib.py`.
- `--lag-months` (default 3) skips the most recent months, since India's
  customs data isn't published for ~2-3 months after the fact — avoids
  burning fetch budget on "no data yet" responses.
- Idempotent: skips (hs_code, period) pairs already in `mg_trade_flows`
  before fetching, so repeat runs only fill gaps.
- `select_stocks.py`'s `compute_trade_momentum_signals()` computes a
  recent-half vs prior-half average import value per HS code and flags
  `widening`/`narrowing`/`flat` trend (`trade_momentum_signals` in the
  report JSON). Same judgment-review-only, never-client-facing treatment
  as the regulatory watchlist above — it confirms a constraint's trend,
  it does not name beneficiary companies (HS codes are commodity-level,
  not 1:1 to a specific company's end market).
