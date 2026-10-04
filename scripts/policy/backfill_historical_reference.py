#!/usr/bin/env python3
"""Backfill mg_capacity_gaps / mg_import_dependencies with genuinely dated,
cited historical observations (not projected/fabricated figures).

WHY
---
Both tables held exactly one undated 2024 "reference packet" snapshot
(provenance_status='PENDING_SOURCE', no source_url) — see
src/makrograph/india/capacity_engine.py::_DOMESTIC_CAPACITY and
src/makrograph/india/import_localization.py::_IMPORT_DEPENDENCY_DATA. Because
that packet was only ever written to the DB as of 2026-07-06/2026-08-31, every
historical point-in-time report (2022-2025) saw zero rows here, so the
quantified_gap / import_dependence legs of _constraint_quality() graded
UNMEASURED almost everywhere — which is why Core Buy / Early-Timing never
fired at any of the 5 report anchors (traced and documented in
[[trace-core-buy-early-timing-empty]]).

This script inserts a fixed, hand-researched set of REAL point-in-time
observations (MNRE/Mercom/GTRI/Lok Sabha/Ministry of Mines primary sources),
each tagged provenance_status='PRIMARY_SOURCE' with its actual source_url,
source_title and source_published_at, and an as_of_date that is the genuine
measurement date (not forced to a fiscal year-end when the source states a
different date). Where the source could not supply both a demand AND a
supply figure, only the supply-side (domestic_capacity) fact is written and
gap/gap_pct/required_quantity are left NULL — this script never invents a
demand figure to manufacture a gap_pct.

This is a ONE-TIME hand-curated backfill (research done via a dedicated
sourcing pass), not a live ingestor. It is idempotent: re-running it upserts
the same fixed rows via each table's (component, target_year, as_of_date) /
(sector, component, as_of_date) unique constraint.

Usage:
    python scripts/policy/backfill_historical_reference.py [--dry-run]
"""
import argparse
import os
import sys
from datetime import date

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
from extract_report_data import connect  # noqa: E402

SOURCE_FAMILY_GOV = "GOVERNMENT_PRIMARY"
SOURCE_FAMILY_INDUSTRY = "INDUSTRY_RESEARCH"

# ── Capacity observations (mg_capacity_gaps) ─────────────────────────────────
# Only entries where a real gap could be computed from the SAME source carry
# gap/gap_pct/required_quantity. Pure supply-side facts leave those NULL.
CAPACITY_ROWS = [
    dict(sector="solar", component="solar_modules_gw", as_of_date=date(2022, 9, 30),
         target_year=2022, domestic_capacity=39, unit="GW/year",
         supply_chain_stage="module", theme_name="Solar Module Overcapacity Risk",
         source_title="India's solar module manufacturing capacity to reach 95 GW by 2025: Report",
         source_publisher="Mercom India Research (via Business Standard)",
         source_url="https://www.business-standard.com/amp/article/companies/india-s-solar-module-manufacturing-capacity-to-reach-95-gw-by-2025-report-123013100454_1.html",
         source_published_at=date(2023, 1, 30), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.7),
    dict(sector="solar", component="solar_modules_gw", as_of_date=date(2023, 12, 31),
         target_year=2023, domestic_capacity=64.5, unit="GW/year",
         supply_chain_stage="module", theme_name="Solar Module Overcapacity Risk",
         source_title="India's Total Solar Module Manufacturing Capacity Reached 64.5 GW in 2023",
         source_publisher="Mercom India Research",
         source_url="https://www.mercomindia.com/india-solar-module-manufacturing-2023",
         source_published_at=date(2024, 3, 28), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.85),
    dict(sector="solar", component="solar_modules_gw", as_of_date=date(2024, 12, 31),
         target_year=2024, domestic_capacity=90.9, unit="GW/year",
         supply_chain_stage="module", theme_name="Solar Module Overcapacity Risk",
         source_title="India added 25.3 GW of solar module and 11.6 GW cell capacity in 2024",
         source_publisher="Mercom India Research (via pv-magazine India)",
         source_url="https://www.pv-magazine-india.com/2025/04/08/india-added-25-3-gw-of-solar-module-and-11-6-gw-cell-capacity-in-2024-says-mercom/",
         source_published_at=date(2025, 4, 8), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.85),
    dict(sector="solar", component="solar_modules_gw", as_of_date=date(2025, 3, 31),
         target_year=2025, domestic_capacity=74, unit="GW/year",
         supply_chain_stage="module", theme_name="Solar Module Overcapacity Risk",
         source_title="India almost trebles cell manufacturing capacity to 25GW in 12 months to March 2025",
         source_publisher="Ministry of New and Renewable Energy (MNRE), via PV Tech",
         source_url="https://www.pv-tech.org/india-almost-trebles-cell-manufacturing-capacity-25gw-12-months-march-2025/",
         source_published_at=date(2025, 4, 2), source_family=SOURCE_FAMILY_GOV,
         confidence=0.8),
    dict(sector="solar", component="solar_modules_gw", as_of_date=date(2025, 12, 31),
         target_year=2025, domestic_capacity=210, unit="GW/year",
         supply_chain_stage="module", theme_name="Solar Module Overcapacity Risk",
         source_title="India's cumulative solar module capacity reaches 210GW",
         source_publisher="Mercom India Research (via PV Tech)",
         source_url="https://www.pv-tech.org/indias-cumulative-solar-module-capacity-reaches-210gw-cell-capacity-hits-27gw-mercom/",
         source_published_at=date(2026, 3, 19), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.85),
    dict(sector="solar", component="solar_cells_gw", as_of_date=date(2023, 12, 31),
         target_year=2023, domestic_capacity=5.8, unit="GW/year",
         supply_chain_stage="cell", theme_name="Solar Cell Manufacturing Gap",
         source_title="India's PV module production capacity hits 64.5 GW, cell output reaches 5.8 GW",
         source_publisher="Mercom India Research (via pv-magazine)",
         source_url="https://www.pv-magazine.com/2024/04/02/indias-pv-module-production-capacity-hits-64-5-gw-cell-output-reaches-5-8-gw/",
         source_published_at=date(2024, 4, 2), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.85),
    dict(sector="solar", component="solar_cells_gw", as_of_date=date(2024, 12, 31),
         target_year=2024, domestic_capacity=17.4, unit="GW/year",
         supply_chain_stage="cell", theme_name="Solar Cell Manufacturing Gap",
         source_title="India added 25.3 GW of solar module and 11.6 GW cell capacity in 2024 (derived: 5.8 + 11.6)",
         source_publisher="Mercom India Research (via pv-magazine India)",
         source_url="https://www.pv-magazine-india.com/2025/04/08/india-added-25-3-gw-of-solar-module-and-11-6-gw-cell-capacity-in-2024-says-mercom/",
         source_published_at=date(2025, 4, 8), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.65),
    dict(sector="solar", component="solar_cells_gw", as_of_date=date(2025, 3, 31),
         target_year=2025, domestic_capacity=25, unit="GW/year",
         supply_chain_stage="cell", theme_name="Solar Cell Manufacturing Gap",
         source_title="India almost trebles cell manufacturing capacity to 25GW in 12 months to March 2025",
         source_publisher="Ministry of New and Renewable Energy (MNRE), via PV Tech",
         source_url="https://www.pv-tech.org/india-almost-trebles-cell-manufacturing-capacity-25gw-12-months-march-2025/",
         source_published_at=date(2025, 4, 2), source_family=SOURCE_FAMILY_GOV,
         confidence=0.8),
    dict(sector="solar", component="solar_cells_gw", as_of_date=date(2025, 12, 31),
         target_year=2025, domestic_capacity=27, unit="GW/year",
         supply_chain_stage="cell", theme_name="Solar Cell Manufacturing Gap",
         source_title="India's cumulative solar module capacity reaches 210GW, cell capacity hits 27GW",
         source_publisher="Mercom India Research (via PV Tech)",
         source_url="https://www.pv-tech.org/indias-cumulative-solar-module-capacity-reaches-210gw-cell-capacity-hits-27gw-mercom/",
         source_published_at=date(2026, 3, 19), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.85),
    dict(sector="solar", component="solar_wafers_gw", as_of_date=date(2025, 3, 31),
         target_year=2025, domestic_capacity=2, unit="GW/year",
         supply_chain_stage="wafer", theme_name="Solar Wafer Capacity Gap",
         source_title="India almost trebles cell manufacturing capacity to 25GW in 12 months to March 2025 "
                      "(India commissioned its first 2GW of ingot and wafer manufacturing capacity)",
         source_publisher="Ministry of New and Renewable Energy (MNRE), via PV Tech",
         source_url="https://www.pv-tech.org/india-almost-trebles-cell-manufacturing-capacity-25gw-12-months-march-2025/",
         source_published_at=date(2025, 4, 2), source_family=SOURCE_FAMILY_GOV,
         confidence=0.8),
    # CRGO steel: the ONLY entry where the same primary source states both a
    # domestic-production and a total-demand figure, so gap_pct is genuine
    # arithmetic on two cited numbers, not an invented demand figure.
    dict(sector="power_transmission", component="crgo_steel_mt", as_of_date=date(2024, 3, 31),
         target_year=2023, domestic_capacity=50000, required_quantity=400000,
         gap=350000, gap_pct=87.5, unit="MT/year",
         supply_chain_stage="material", theme_name="CRGO Steel Shortage", severity="critical",
         source_title="GTRI: India's power sector facing 30 percent shortage of CRGO steel",
         source_publisher="Global Trade Research Initiative (GTRI), via Business Standard",
         source_url="https://www.business-standard.com/industry/news/crgo-steel-shortage-could-impact-india-s-power-sector-expansion-plans-gtri-124102800575_1.html",
         source_published_at=date(2024, 10, 28), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.75),
    dict(sector="battery_storage", component="battery_cells_gwh", as_of_date=date(2025, 10, 31),
         target_year=2025, domestic_capacity=1.4, unit="GWh/year (commissioned cell capacity, excl. pack assembly)",
         supply_chain_stage="cell", theme_name="Battery Cell Manufacturing Gap",
         source_title="Only 2.8% of target capacity delivered yet under India's battery manufacturing incentive scheme",
         source_publisher="IEEFA / JMK Research",
         source_url="https://ieefa.org/articles/only-28-target-capacity-delivered-yet-under-indias-battery-manufacturing-incentive-scheme",
         source_published_at=date(2025, 10, 15), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence=0.75),
]

# ── Import-dependency observations (mg_import_dependencies) ──────────────────
IMPORT_ROWS = [
    dict(sector="power_transmission", component="CRGO Steel", hs_code="7225",
         as_of_date=date(2024, 3, 31), import_share=0.875, risk_level="critical",
         source_title="GTRI: India's power sector facing 30 percent shortage of CRGO steel",
         source_publisher="Global Trade Research Initiative (GTRI), via Business Standard",
         source_url="https://www.business-standard.com/industry/news/crgo-steel-shortage-could-impact-india-s-power-sector-expansion-plans-gtri-124102800575_1.html",
         source_published_at=date(2024, 10, 28), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence_note="import_share = (400,000-50,000)/400,000, arithmetic on the two figures GTRI states together"),
    dict(sector="solar", component="Polysilicon", hs_code="2804",
         as_of_date=date(2025, 3, 31), import_share=1.0, risk_level="critical",
         source_title="India's Emerging Polysilicon Manufacturing Ecosystem",
         source_publisher="Government of India ministry statement, via industry press",
         source_url="https://urjadaily.com/indias-emerging-polysilicon-manufacturing-ecosystem-opportunities-and-challenges/",
         source_published_at=date(2025, 3, 15), source_family=SOURCE_FAMILY_GOV,
         confidence_note="government statement: 'no commercial production of polysilicon in the country' as of Mar-2025"),
    dict(sector="specialty_chemicals", component="Pharma APIs", hs_code="2941",
         as_of_date=date(2024, 3, 31), import_share=0.35, import_value_bn_usd=4.5, risk_level="moderate",
         source_title="India struggling to free pharma industry from dependence on Chinese APIs",
         source_publisher="Policy Circle (citing Dept. of Pharmaceuticals / trade data)",
         source_url="https://www.policycircle.org/industry/apis-import-depencence-on-china/",
         source_published_at=date(2024, 6, 1), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence_note="FY2023-24: ~35% of total API requirement met by imports (~USD 4.5bn)"),
    dict(sector="battery_storage", component="Lithium Carbonate / Hydroxide", hs_code="2836",
         as_of_date=date(2023, 12, 31), import_share=1.0, risk_level="critical",
         source_title="India's Ministry of Mines 2023 Critical Minerals List",
         source_publisher="Ministry of Mines, Government of India, via Down To Earth",
         source_url="https://www.downtoearth.org.in/energy/indias-critical-mineral-imports-remain-highly-concentrated-exposing-supply-risks-and-driving-diversification-push",
         source_published_at=date(2023, 7, 1), source_family=SOURCE_FAMILY_GOV,
         confidence_note="lithium classified with '100 per cent import dependence' on the Ministry's 2023 critical-minerals list"),
    dict(sector="electronics_manufacturing", component="Semiconductor ICs", hs_code="8542",
         as_of_date=date(2024, 3, 31), import_share=0.68, import_value_bn_usd=22.3, risk_level="high",
         source_title="India's semiconductor imports an astonishing Rs 1.71 lakh crore in FY24",
         source_publisher="Lok Sabha reply, Minister of State for Electronics & IT, via Communications Today",
         source_url="https://www.communicationstoday.co.in/indias-semiconductor-imports-an-astonishing-%e2%82%b91-71-lakh-crore-in-fy24/",
         source_published_at=date(2024, 8, 1), source_family=SOURCE_FAMILY_GOV,
         confidence_note="IC/microprocessor import line specifically, ~68% of that subcategory's demand, FY2023-24"),
    dict(sector="electronics_manufacturing", component="Printed Circuit Boards", hs_code="8534",
         as_of_date=date(2025, 3, 31), import_share=0.88, import_value_bn_usd=3.7, risk_level="critical",
         source_title="India Needs 10-12 New Big PCB Units With Rs 20K Cr Investment To Cut Reliance On Imports",
         source_publisher="Outlook Business",
         source_url="https://www.outlookbusiness.com/news/india-needs-10-12-new-big-pcb-units-with-20k-cr-investment-to-cut-reliance-on-imports",
         source_published_at=date(2025, 1, 1), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence_note="FY2024-25: 88% of India's bare-PCB requirement met through imports"),
    dict(sector="electronics_manufacturing", component="Display Panels", hs_code="8524",
         as_of_date=date(2024, 12, 31), import_share=0.98, import_value_bn_usd=2.7, risk_level="critical",
         source_title="Manufacturing: India Pushes Domestic Display Production",
         source_publisher="invidis (display-industry trade press)",
         source_url="https://invidis.com/news/2026/01/manufacturing-india-pushes-domestic-display-production/",
         source_published_at=date(2026, 1, 15), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence_note="directional reading of 'nearly 100 percent' import-sourced, not a precise dated percentage; lower confidence"),
    dict(sector="battery_storage", component="Cathode Active Materials (LFP/NMC)", hs_code="2841",
         as_of_date=date(2024, 12, 31), import_share=0.95, risk_level="critical",
         source_title="Cathode active material manufacturing is vital to India's energy storage ambitions",
         source_publisher="EVreporter (citing IEEFA analysis)",
         source_url="https://evreporter.com/cathode-active-material-manufacturing-is-vital-to-indias-energy-storage-ambitions/",
         source_published_at=date(2024, 9, 1), source_family=SOURCE_FAMILY_INDUSTRY,
         confidence_note="domestic CAM output 'will remain below 5% of domestic demand through at least 2028' — forward-looking industry estimate, not a measured historical ratio"),
]


def upsert_capacity(cur, row, dry_run):
    print(f"  [capacity] {row['component']} as_of={row['as_of_date']} "
          f"domestic_capacity={row.get('domestic_capacity')} "
          f"gap_pct={row.get('gap_pct')}")
    if dry_run:
        return
    cur.execute("""
        INSERT INTO mg_capacity_gaps
            (sector, component, required_quantity, domestic_capacity, gap, gap_pct,
             unit, supply_chain_stage, theme_name, severity, target_year, confidence,
             as_of_date, source_url, source_title, source_published_at, source_family,
             provenance_status, ingestion_method)
        VALUES (%(sector)s, %(component)s, %(required_quantity)s, %(domestic_capacity)s,
                %(gap)s, %(gap_pct)s, %(unit)s, %(supply_chain_stage)s, %(theme_name)s,
                %(severity)s, %(target_year)s, %(confidence)s, %(as_of_date)s,
                %(source_url)s, %(source_title)s, %(source_published_at)s, %(source_family)s,
                'PRIMARY_SOURCE', 'historical_backfill_research_v1')
        ON CONFLICT (component, target_year, as_of_date) DO UPDATE SET
            required_quantity = EXCLUDED.required_quantity,
            domestic_capacity = EXCLUDED.domestic_capacity,
            gap = EXCLUDED.gap, gap_pct = EXCLUDED.gap_pct,
            source_url = EXCLUDED.source_url, source_title = EXCLUDED.source_title,
            source_published_at = EXCLUDED.source_published_at,
            source_family = EXCLUDED.source_family,
            provenance_status = 'PRIMARY_SOURCE',
            ingestion_method = 'historical_backfill_research_v1',
            updated_at = now()
    """, {
        "sector": row["sector"], "component": row["component"],
        "required_quantity": row.get("required_quantity"),
        "domestic_capacity": row.get("domestic_capacity"),
        "gap": row.get("gap"), "gap_pct": row.get("gap_pct"),
        "unit": row.get("unit"), "supply_chain_stage": row.get("supply_chain_stage"),
        "theme_name": row.get("theme_name"), "severity": row.get("severity"),
        "target_year": row.get("target_year"), "confidence": row.get("confidence"),
        "as_of_date": row["as_of_date"], "source_url": row["source_url"],
        "source_title": row["source_title"],
        "source_published_at": row.get("source_published_at"),
        "source_family": row.get("source_family"),
    })


def upsert_import(cur, row, dry_run):
    print(f"  [import]   {row['component']} as_of={row['as_of_date']} "
          f"import_share={row.get('import_share')}")
    if dry_run:
        return
    cur.execute("""
        INSERT INTO mg_import_dependencies
            (sector, component, import_share, import_value_bn_usd, hs_code, risk_level,
             as_of_date, source_url, source_title, source_published_at, source_family,
             provenance_status, ingestion_method)
        VALUES (%(sector)s, %(component)s, %(import_share)s, %(import_value_bn_usd)s,
                %(hs_code)s, %(risk_level)s, %(as_of_date)s, %(source_url)s,
                %(source_title)s, %(source_published_at)s, %(source_family)s,
                'PRIMARY_SOURCE', 'historical_backfill_research_v1')
        ON CONFLICT (sector, component, as_of_date) DO UPDATE SET
            import_share = EXCLUDED.import_share,
            import_value_bn_usd = EXCLUDED.import_value_bn_usd,
            hs_code = EXCLUDED.hs_code, risk_level = EXCLUDED.risk_level,
            source_url = EXCLUDED.source_url, source_title = EXCLUDED.source_title,
            source_published_at = EXCLUDED.source_published_at,
            source_family = EXCLUDED.source_family,
            provenance_status = 'PRIMARY_SOURCE',
            ingestion_method = 'historical_backfill_research_v1',
            updated_at = now()
    """, {
        "sector": row["sector"], "component": row["component"],
        "import_share": row.get("import_share"),
        "import_value_bn_usd": row.get("import_value_bn_usd"),
        "hs_code": row.get("hs_code"), "risk_level": row.get("risk_level"),
        "as_of_date": row["as_of_date"], "source_url": row["source_url"],
        "source_title": row["source_title"],
        "source_published_at": row.get("source_published_at"),
        "source_family": row.get("source_family"),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print(f"Capacity rows: {len(CAPACITY_ROWS)}")
    for row in CAPACITY_ROWS:
        upsert_capacity(cur, row, args.dry_run)
    print(f"Import-dependency rows: {len(IMPORT_ROWS)}")
    for row in IMPORT_ROWS:
        upsert_import(cur, row, args.dry_run)

    if args.dry_run:
        print("DRY RUN — nothing written.")
        conn.rollback()
    else:
        conn.commit()
        print("Committed.")
    conn.close()


if __name__ == "__main__":
    main()
