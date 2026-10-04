#!/usr/bin/env python3
"""Materialise per-constraint monthly filing-signal activity — the fast backing
table that makes year-specific ("emergence-weighted") constraint ranking possible.

WHY
---
"Same constraints every year" has one fixable lever: rank by how much a
constraint is INFLECTING this year (YoY acceleration in shortage/bottleneck/
capex/order/localisation signals), not by static level from a fixed pool. That
needs per-(constraint, month) signal counts, which is far too slow to compute
with ILIKE-over-raw_text at report time. This does ONE corpus pass, matching a
broad constraint vocabulary (import-substitution AND demand-driven constraints
the fixed 12 never covered — T&D, data-centre power, cement, railway, hydrogen)
against each signal-bearing document, and stores the aggregates. Emergence
scoring then reads this instantly and point-in-time (filter period <= anchor).

NO HARDCODING OF STOCKS: this is a constraint *taxonomy* (like the existing 12),
keyed on generic keyword patterns, not a company/winner list. Point-in-time safe:
period is derived from the document's filed_at; nothing is backdated.

USAGE
    python scripts/policy/build_constraint_signal_activity.py [--country IN] [--rebuild]
"""
import argparse
import os
import re
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

_SIGNAL_TYPES = (
    "supply_bottleneck", "capacity_shortage", "tender_pipeline", "capex_increase",
    "demand_surge", "localization_opportunity", "order_book", "inventory_drawdown",
)

# LEADING subset — signals that appear while a constraint is FORMING (a shortage
# is called out, capacity is being built, localisation is flagged) rather than
# once it is underway (order_book / demand_surge / tender are confirming/late).
# The early-inflection detector keys off a sharp rise in these from a low base,
# which precedes the total-signal level the level-based emergence gate needs.
_LEADING_TYPES = (
    "supply_bottleneck", "capacity_shortage", "capex_increase",
    "localization_opportunity",
)

# Constraint taxonomy: label -> distinctive keyword regex. Includes the existing
# import-substitution set AND demand-driven constraints the fixed 12 omitted.
# Keywords are physical/technical product or bottleneck terms, not company names.
_TAXONOMY = {
    "Solar Cell":            r"solar cell|solar pv cell|topcon cell|pv cell",
    "Solar Module":          r"solar module|solar panel|pv module",
    "Solar Wafer":           r"solar wafer|ingot|polysilicon",
    "Battery Cell (Li-ion)": r"battery cell|lithium[- ]ion cell|li-ion cell|cell manufactur|gigafactory",
    "Cathode/Anode Materials": r"cathode active|anode material|\bcam\b|lfp\b|nmc\b",
    "Lithium":               r"lithium carbonate|lithium hydroxide|lithium refin",
    "Power Transformer":     r"power transformer|distribution transformer|transformer manufactur",
    "CRGO Steel":            r"crgo|grain[- ]oriented|electrical steel",
    "HVDC / T&D Equipment":  r"hvdc|transmission line|conductor|gis substation|switchgear|765 kv|400 kv|grid equipment",
    "PCB / Printed Circuit Board": r"printed circuit board|\bpcb\b|\bpcba\b",
    "EMS / Contract Manufacturing": r"\bems\b|electronic manufacturing services|contract manufactur",
    "Semiconductor IC":      r"semiconductor|integrated circuit|\bosat\b|\bfab\b|chip manufactur|wafer fab",
    "Display Panels":        r"display panel|lcd panel|oled|flat panel",
    "Passive Components":    r"\bmlcc\b|passive component|multilayer ceramic",
    "Optical Fibre":         r"optical fib(?:re|er)|fibre preform|fiber preform|\bofc\b",
    "Defense electronics":   r"defen[cs]e electronic|radar|avionics|missile|electro[- ]optic|indigenis",
    "Rolling Stock / Railway": r"rolling stock|locomotive|\bwagon\b|vande bharat|railway coach|metro coach|traction",
    "Pharma APIs":           r"\bapi\b|active pharmaceutical|bulk drug|key starting material",
    "Specialty Chemicals":   r"fluorochemical|agrochemical active|specialty chemical|electronic chemical",
    "Green Hydrogen":        r"green hydrogen|electroly[sz]er|electroly[sz]is",
    "Data Centre Power":     r"data cent(?:re|er)|hyperscale|colocation|ups system",
    "Cement":                r"\bcement\b|clinker|grinding unit",
    "Wind Turbine":          r"wind turbine|nacelle|wind blade|tower internal",
    "Textile / Technical Textile": r"technical textile|man[- ]made fibre|\bpsf\b|viscose",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in _TAXONOMY.items()}
_TEXT_CAP = 60000


def _ensure_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mg_constraint_signal_activity (
            country          VARCHAR(4)  NOT NULL,
            constraint_label TEXT        NOT NULL,
            period           CHAR(6)     NOT NULL,   -- YYYYMM from filed_at
            signal_count     INTEGER     NOT NULL DEFAULT 0,
            doc_count        INTEGER     NOT NULL DEFAULT 0,
            first_filed      DATE,
            updated_at       TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (country, constraint_label, period)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_csa_label_period "
                "ON mg_constraint_signal_activity(country, constraint_label, period)")
    # leading_count: subset of signal_count from constraint-FORMING signal types
    # (added Sep-2026 for the early-inflection detector).
    cur.execute("ALTER TABLE mg_constraint_signal_activity "
                "ADD COLUMN IF NOT EXISTS leading_count INTEGER NOT NULL DEFAULT 0")
    # Per-(constraint, company) signal exposure — which listed issuers file the
    # constraint's shortage/capex/order signals. Gives EVERY constraint a company
    # list (as exposure, distinct from a verified maker role) so demand-driven
    # constraints (Green Hydrogen, Data Centre Power) are not blank.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mg_constraint_company_signals (
            country          VARCHAR(4)  NOT NULL,
            constraint_label TEXT        NOT NULL,
            ticker           TEXT        NOT NULL,
            period           CHAR(6)     NOT NULL,
            signal_count     INTEGER     NOT NULL DEFAULT 0,
            PRIMARY KEY (country, constraint_label, ticker, period)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ccs_label "
                "ON mg_constraint_company_signals(country, constraint_label, period)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="IN")
    ap.add_argument("--rebuild", action="store_true", help="truncate before repopulating")
    args = ap.parse_args()

    conn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")
    rconn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")
    cur = conn.cursor()
    _ensure_schema(cur)
    if args.rebuild:
        cur.execute("DELETE FROM mg_constraint_signal_activity WHERE country=%s", (args.country,))
        cur.execute("DELETE FROM mg_constraint_company_signals WHERE country=%s", (args.country,))
    conn.commit()

    # One pass over documents that carry >=1 constraint-type signal; match the
    # whole taxonomy in memory. (period, label) -> [signal_count, doc set].
    rcur = rconn.cursor(name="csa_scan", cursor_factory=psycopg2.extras.RealDictCursor)
    rcur.itersize = 400
    rcur.execute("""
        SELECT d.id, UPPER(TRIM(d.ticker)) AS ticker,
               to_char(d.filed_at,'YYYYMM') AS period, d.filed_at::date AS filed,
               d.raw_text, COUNT(s.id) AS n_sig,
               COUNT(s.id) FILTER (WHERE s.signal_type = ANY(%s)) AS n_lead
        FROM mg_documents d JOIN mg_signals s ON s.document_id = d.id
        WHERE d.country=%s AND d.raw_text IS NOT NULL AND s.signal_type = ANY(%s)
        GROUP BY d.id, d.ticker, d.filed_at, d.raw_text
    """, (list(_LEADING_TYPES), args.country, list(_SIGNAL_TYPES)))

    agg_sig: dict[tuple, int] = defaultdict(int)
    agg_lead: dict[tuple, int] = defaultdict(int)
    agg_doc: dict[tuple, int] = defaultdict(int)
    agg_first: dict[tuple, str] = {}
    agg_co: dict[tuple, int] = defaultdict(int)   # (label, ticker, period) -> signals
    seen = matched = 0
    for row in rcur:
        seen += 1
        text = (row["raw_text"] or "")[:_TEXT_CAP]
        n_sig = int(row["n_sig"] or 0)
        n_lead = int(row["n_lead"] or 0)
        period = row["period"]
        ticker = (row["ticker"] or "").strip()
        hit = False
        for label, rx in _COMPILED.items():
            if rx.search(text):
                key = (label, period)
                agg_sig[key] += n_sig
                agg_lead[key] += n_lead
                agg_doc[key] += 1
                f = row["filed"].isoformat()
                if key not in agg_first or f < agg_first[key]:
                    agg_first[key] = f
                if ticker:
                    agg_co[(label, ticker, period)] += n_sig
                hit = True
        if hit:
            matched += 1
        if seen % 20000 == 0:
            print(f"  scanned {seen} signal-docs, {matched} matched a constraint", flush=True)

    rows = [(args.country, label, period, agg_sig[(label, period)],
             agg_lead[(label, period)],
             agg_doc[(label, period)], agg_first[(label, period)])
            for (label, period) in agg_sig]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO mg_constraint_signal_activity
            (country, constraint_label, period, signal_count, leading_count, doc_count, first_filed)
        VALUES %s
        ON CONFLICT (country, constraint_label, period) DO UPDATE
          SET signal_count=EXCLUDED.signal_count, leading_count=EXCLUDED.leading_count,
              doc_count=EXCLUDED.doc_count,
              first_filed=LEAST(mg_constraint_signal_activity.first_filed, EXCLUDED.first_filed),
              updated_at=now()
    """, rows, page_size=1000)
    co_rows = [(args.country, label, ticker, period, cnt)
               for (label, ticker, period), cnt in agg_co.items()]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO mg_constraint_company_signals
            (country, constraint_label, ticker, period, signal_count)
        VALUES %s
        ON CONFLICT (country, constraint_label, ticker, period) DO UPDATE
          SET signal_count = EXCLUDED.signal_count
    """, co_rows, page_size=2000)
    conn.commit()
    print(f"DONE: {seen} signal-docs scanned, {matched} matched; "
          f"{len(rows)} (constraint,month) rows across {len(set(l for l,_ in agg_sig))} constraints; "
          f"{len(co_rows)} (constraint,company,month) rows.")
    rconn.close(); conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
