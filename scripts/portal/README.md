# MakroGraph Static Research Portal

Generates a fully self-contained static site (`index.html`, inline CSS, no
JavaScript, no external assets) presenting the track record and report
library. Output is local-only.

## Rebuild

```bash
.venv/bin/python scripts/portal/build_portal.py            # writes data/portal/
.venv/bin/python scripts/portal/build_portal.py --out /some/dir
```

Rebuild after each monthly selector run (new `mg_decisions` rows / new PDFs)
or whenever fresh bhavcopy data lands.

## What it reads

- **Postgres `makrograph` DB only** (env: `MAKROGRAPH_PG_*`):
  - `mg_decisions` — BUY rows drive the track-record table (table is
    created-if-missing with the canonical DDL).
  - `nse_bhavcopy_data` — entry = first close within 10 days after `as_of`;
    exits at 3m/6m/12m and since-decision. A horizon whose nearest price is
    more than 45 days short of target is shown as "—" (right-censored), never
    a truncated number. Returns outside (−95%, +2000%) are dropped as data
    errors.
  - `mg_symbol_renames` — high-confidence/confirmed renames are unioned into
    price queries so renamed stocks keep continuous histories.
  - `security_master` — benchmark universe filter (median return of the
    top-25-turnover EQ stocks on the same entry date, same window logic).
- **Filesystem**: `data/reports/stock_selector_IN_*_v3_report.pdf` for the
  report library (relative links, so the PDFs must ship alongside the site).

Basket tickers ("A + B") average the legs; a horizon is null unless every leg
has a value.

## Publishing — DO NOT

This script only writes local files. **Publishing the portal anywhere
(web, PMS client portal, social) requires compliance sign-off first** —
SEBI registration details are still `[PENDING]` in the disclaimer blocks,
and the disclaimers must be reviewed by counsel before any public release.
