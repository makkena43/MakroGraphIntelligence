"""Magnitude extraction: turn constraint-signal TEXT into NUMBERS.

"Orders grew 173%" and "orders grew 4%" are currently the same demand_surge
signal. The magnitude IS the story — this module extracts it so ranking can
measure intensity instead of counting mentions.

Pure regex, deterministic, no LLM cost. Fills mg_signals.signal_value /
signal_unit ONLY where signal_value IS NULL (never overwrites), and stamps
extracted_by='magnitude_regex_v1' so a later LLM pass can supersede it.

Units written (all generic, country-agnostic):
    order_growth_pct      — "orders grew 173%", "order inflow up 88%"
    utilization_pct       — "capacity utilization of 95%"
    backlog_growth_pct    — "backlog increased 45%"
    booktobill_ratio      — "book-to-bill of 1.4"
    backlog_coverage_x    — "order book at 3x annual revenue"
    margin_change_bps     — "margins expanded 240 basis points"
    capex_growth_pct      — "capex up 60%"

Usage:
    python -m makrograph.nlp.magnitude_extractor --country IN --dry-run
    python -m makrograph.nlp.magnitude_extractor --country IN --apply
"""

import argparse
import logging
import re

logger = logging.getLogger(__name__)

# Signal types worth quantifying (the constraint-thesis chain)
TARGET_SIGNAL_TYPES = (
    "demand_surge", "capacity_constraint_seller", "capacity_utilization_high",
    "backlog_duration", "capex_increase", "realized_margin_expansion",
    "supply_bottleneck", "capacity_shortage", "tender_pipeline",
)

_NUM = r"(\d{1,3}(?:[.,]\d{1,2})?)"          # 173, 95.5, 1,5 (euro style)


def _f(x: str) -> float:
    return float(x.replace(",", "."))


# Ordered: first match wins within each category.
# Every pattern requires the metric word NEXT TO the number — proximity is the
# noise guard (a bare "95%" could be anything).
_PATTERNS: list[tuple[str, re.Pattern, float, float]] = [
    # (unit, compiled_pattern, min_valid, max_valid)
    ("order_growth_pct", re.compile(
        rf"order(?:s|\s*book|\s*inflow|\s*intake)?[^.%]{{0,60}}?"
        rf"(?:grew|growth|up|increase[d]?|rose|higher)\s*(?:by\s*)?{_NUM}\s*%", re.I), 5, 500),
    ("order_growth_pct", re.compile(
        rf"{_NUM}\s*%\s*(?:growth|increase|rise|jump)[^.]{{0,40}}?order", re.I), 5, 500),
    ("order_growth_pct", re.compile(
        rf"orders?\s*:\s*{_NUM}\s*%\s*growth", re.I), 5, 500),

    ("utilization_pct", re.compile(
        rf"(?:capacity\s+)?utili[sz]ation\s*(?:rate|level)?\s*(?:of|at|was|is|around|near|~)?\s*{_NUM}\s*%", re.I), 30, 100),
    ("utilization_pct", re.compile(
        rf"{_NUM}\s*%\s*(?:capacity\s+)?utili[sz]ation", re.I), 30, 100),
    ("utilization_pct", re.compile(
        rf"(?:operating|running)\s+at\s*{_NUM}\s*%\s*(?:of\s+)?(?:capacity|utili[sz]ation)", re.I), 30, 100),

    ("backlog_growth_pct", re.compile(
        rf"backlog[^.%]{{0,50}}?(?:grew|growth|up|increase[d]?|rose)\s*(?:by\s*)?{_NUM}\s*%", re.I), 5, 500),

    ("booktobill_ratio", re.compile(
        rf"book[\s-]*to[\s-]*bill\s*(?:ratio)?\s*(?:of|at|was|is|above|over)?\s*{_NUM}", re.I), 0.5, 5),

    ("backlog_coverage_x", re.compile(
        rf"(?:order\s*book|backlog)[^.]{{0,60}}?{_NUM}\s*(?:x|times)\s*(?:of\s+)?(?:annual\s+)?(?:revenue|sales|turnover)", re.I), 0.5, 10),

    ("margin_change_bps", re.compile(
        rf"margins?[^.%]{{0,60}}?(?:expand|improv|increas)\w*\s*(?:by\s*)?{_NUM}\s*(?:bps|basis\s*points)", re.I), 10, 3000),

    ("capex_growth_pct", re.compile(
        rf"cap(?:ital\s+expenditure|ex)[^.%]{{0,60}}?(?:up|grew|growth|increase[d]?|higher|rose)\s*(?:by\s*)?{_NUM}\s*%", re.I), 5, 500),
]


def extract_magnitude(text: str) -> tuple[str, float] | None:
    """Return (unit, value) for the strongest quantifiable claim in text."""
    if not text:
        return None
    first_unit: str | None = None
    unit_max: dict[str, float] = {}
    for unit, pat, lo, hi in _PATTERNS:
        for m in pat.finditer(text):
            try:
                val = _f(m.group(1))
            except ValueError:
                continue
            if not (lo <= val <= hi):
                continue
            if first_unit is None:
                first_unit = unit
            # Within a unit, keep the STRONGEST claim ("173% growth" beats
            # the "up by 88%" mentioned later in the same passage)
            unit_max[unit] = max(unit_max.get(unit, 0.0), val)
    if first_unit is None:
        return None
    return (first_unit, unit_max[first_unit])


def backfill(country: str | None, apply: bool, batch: int = 5000) -> dict:
    """Scan target signals with NULL signal_value and fill magnitudes."""
    import yaml, json
    from pathlib import Path
    from makrograph.storage.pg_store import PGStore

    root = Path(__file__).resolve().parents[3]
    with open(root / "config" / "settings.yaml") as f:
        cfg = yaml.safe_load(f)
    sp = root / "config" / "secrets.json"
    if sp.exists():
        with open(sp) as f:
            for sec, vals in json.load(f).items():
                if not sec.startswith("_") and isinstance(vals, dict):
                    cfg.setdefault(sec, {}).update({k: v for k, v in vals.items() if v})

    store = PGStore(cfg.get("postgresql", {}))
    stats: dict[str, int] = {"scanned": 0, "extracted": 0}
    unit_counts: dict[str, int] = {}

    with store._conn() as conn:
        cur = conn.cursor()
        where_country = "AND country = %s" if country else ""
        params: list = [list(TARGET_SIGNAL_TYPES)]
        if country:
            params.append(country)
        cur.execute(
            f"""SELECT id, context_text FROM mg_signals
                WHERE signal_type = ANY(%s)
                  AND signal_value IS NULL
                  AND context_text IS NOT NULL
                  {where_country}""",
            params,
        )
        rows = cur.fetchall()
        stats["scanned"] = len(rows)

        updates: list[tuple[float, str, int]] = []
        for r in rows:
            sid, text = r[0], r[1]
            hit = extract_magnitude(text or "")
            if hit:
                unit, val = hit
                updates.append((val, unit, sid))
                unit_counts[unit] = unit_counts.get(unit, 0) + 1

        stats["extracted"] = len(updates)
        stats.update({f"unit_{u}": c for u, c in unit_counts.items()})

        if apply and updates:
            for i in range(0, len(updates), batch):
                chunk = updates[i:i + batch]
                cur.executemany(
                    """UPDATE mg_signals
                       SET signal_value = %s, signal_unit = %s,
                           extracted_by = 'magnitude_regex_v1'
                       WHERE id = %s AND signal_value IS NULL""",
                    chunk,
                )
            conn.commit()
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default=None, help="IN / US, default all")
    ap.add_argument("--apply", action="store_true", help="write updates (default dry-run)")
    args = ap.parse_args()
    out = backfill(args.country, args.apply)
    print(("APPLIED " if args.apply else "DRY-RUN ") + str(out))
