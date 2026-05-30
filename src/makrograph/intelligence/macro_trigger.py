"""
Macro Trigger Layer
~~~~~~~~~~~~~~~~~~~~
Ingests government policies, budgets, tariffs, datacenter announcements,
infrastructure project awards, and defense spending as structured events.
Links each event to relevant investment themes and estimates the impact
direction and magnitude.

Design: manual input + simple rule matching. No scraping at this layer —
events are recorded by you after reading budget speeches, RBI policy notes,
press releases, etc. The manual LLM validation stage then uses these events
to enrich its analysis prompts.

Example events:
    India Budget 2025: ₹10,000 Cr allocated to National Transmission Grid
        → triggers Power_Grid_Transmission (HIGH impact, POSITIVE)
    US BIS chip export restrictions
        → triggers Semiconductor_Memory (MEDIUM impact, NEGATIVE)
    PM GatiShakti new fiber projects
        → triggers Optical_Fiber_Network (HIGH impact, POSITIVE)
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MacroCategory(str, Enum):
    GOVERNMENT_POLICY = "government_policy"
    BUDGET_ALLOCATION = "budget_allocation"
    TARIFF_TRADE = "tariff_trade"
    DATACENTER_ANNOUNCEMENT = "datacenter_announcement"
    INFRASTRUCTURE_PROJECT = "infrastructure_project"
    DEFENSE_SPENDING = "defense_spending"
    REGULATORY_CHANGE = "regulatory_change"
    INTERNATIONAL_AGREEMENT = "international_agreement"
    CENTRAL_BANK_POLICY = "central_bank_policy"
    CORPORATE_ANNOUNCEMENT = "corporate_announcement"


class ImpactDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class ImpactMagnitude(str, Enum):
    LOW = "low"           # incremental, <5% sector impact
    MEDIUM = "medium"     # meaningful, 5-20% sector impact
    HIGH = "high"         # transformative, >20% sector impact
    GAME_CHANGER = "game_changer"   # structural shift


# ---------------------------------------------------------------------------
# Macro event → theme matching rules
# Keywords in the event title/description trigger theme links.
# ---------------------------------------------------------------------------

THEME_TRIGGER_RULES: dict[str, list[str]] = {
    "AI_Datacenter": [
        "ai", "artificial intelligence", "datacenter", "data center",
        "gpu", "cloud computing", "digital infrastructure", "compute",
        "semiconductor policy",
    ],
    "Semiconductor_Memory": [
        "semiconductor", "chip", "fab", "foundry", "memory", "hbm",
        "chip export", "chips act", "semiconductor subsidy",
    ],
    "Power_Grid_Transmission": [
        "transmission", "power grid", "electricity grid", "substation",
        "hvdc", "national grid", "power evacuation", "discoms",
        "national transmission",
    ],
    "Optical_Fiber_Network": [
        "optical fiber", "fiber", "broadband", "bhatnagar net", "bharat net",
        "telecom infrastructure", "5g rollout", "underground cable",
        "submarine cable",
    ],
    "Defense_Electronics": [
        "defense", "defence", "military", "atmanirbhar bharat defense",
        "indigenization", "drdo", "hal", "make in india defense",
        "arms export", "defense procurement",
    ],
    "Renewable_Energy": [
        "solar", "wind", "renewable", "green hydrogen", "clean energy",
        "energy storage", "battery storage", "pump storage",
        "energy transition",
    ],
    "EV_Adoption": [
        "electric vehicle", "ev", "fame scheme", "ev subsidy",
        "charging station", "battery policy", "cell manufacturing",
        "ev policy",
    ],
}


@dataclass
class MacroEvent:
    """A single macro-economic or policy event."""
    title: str
    description: str
    category: MacroCategory
    event_date: date
    source: str                     # e.g. "Union Budget 2025", "RBI MPC", "Press Release"
    impact_direction: ImpactDirection = ImpactDirection.POSITIVE
    impact_magnitude: ImpactMagnitude = ImpactMagnitude.MEDIUM
    amount_inr_cr: Optional[float] = None   # budget allocation in ₹ crore if applicable
    country: str = "India"
    themes: list[str] = field(default_factory=list)  # auto-populated or manual
    tags: list[str] = field(default_factory=list)
    id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description[:200],
            "category": self.category.value,
            "event_date": self.event_date.isoformat(),
            "source": self.source,
            "impact_direction": self.impact_direction.value,
            "impact_magnitude": self.impact_magnitude.value,
            "amount_inr_cr": self.amount_inr_cr,
            "country": self.country,
            "themes": self.themes,
            "tags": self.tags,
        }


class MacroTriggerLayer:
    """
    Stores and retrieves macro events, auto-matches them to themes,
    and computes macro pressure on each theme per quarter.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        db_path = Path(config.get("graph_db_path", "data/db/makrograph_graph.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()
        # Compile theme trigger patterns
        self._trigger_patterns: dict[str, list[re.Pattern]] = {
            theme: [re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in kws]
            for theme, kws in THEME_TRIGGER_RULES.items()
        }

    def _create_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS macro_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                description     TEXT DEFAULT '',
                category        TEXT NOT NULL,
                event_date      DATE NOT NULL,
                source          TEXT DEFAULT '',
                impact_direction TEXT DEFAULT 'positive',
                impact_magnitude TEXT DEFAULT 'medium',
                amount_inr_cr   REAL DEFAULT NULL,
                country         TEXT DEFAULT 'India',
                themes          TEXT DEFAULT '[]',   -- JSON
                tags            TEXT DEFAULT '[]',   -- JSON
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_macro_date ON macro_events(event_date);
            CREATE INDEX IF NOT EXISTS idx_macro_category ON macro_events(category);

            -- Pre-linked macro → theme edges (also stored in macro_triggers in graph_store)
            CREATE TABLE IF NOT EXISTS macro_theme_links (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                macro_event_id  INTEGER NOT NULL REFERENCES macro_events(id),
                theme_name      TEXT NOT NULL,
                relevance_score REAL DEFAULT 0.5,
                impact_direction TEXT DEFAULT 'positive',
                impact_magnitude TEXT DEFAULT 'medium',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(macro_event_id, theme_name)
            );
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def add_event(self, event: MacroEvent) -> int:
        """
        Add a macro event. Auto-matches themes if event.themes is empty.
        Returns the event ID.
        """
        if not event.themes:
            event.themes = self._auto_match_themes(event)

        cur = self.conn.execute(
            """INSERT INTO macro_events
               (title, description, category, event_date, source,
                impact_direction, impact_magnitude, amount_inr_cr,
                country, themes, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (
                event.title, event.description, event.category.value,
                event.event_date.isoformat(), event.source,
                event.impact_direction.value, event.impact_magnitude.value,
                event.amount_inr_cr, event.country,
                json.dumps(event.themes), json.dumps(event.tags),
            ),
        )
        row = cur.fetchone()
        event_id = row[0]
        event.id = event_id
        self.conn.commit()

        # Create theme links
        for theme in event.themes:
            self._link_theme(event_id, theme, event.impact_direction, event.impact_magnitude)

        logger.info(
            f"Macro event added: [{event.category.value}] {event.title} "
            f"→ themes: {event.themes}"
        )
        return event_id

    def add_events_bulk(self, events: list[MacroEvent]) -> list[int]:
        """Add multiple events."""
        return [self.add_event(e) for e in events]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_events_for_theme(
        self,
        theme: str,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
    ) -> list[dict]:
        """All macro events linked to a theme, optionally within a date range."""
        where = "WHERE mtl.theme_name = ?"
        params: list = [theme]
        if from_date:
            where += " AND me.event_date >= ?"
            params.append(from_date.isoformat())
        if to_date:
            where += " AND me.event_date <= ?"
            params.append(to_date.isoformat())

        rows = self.conn.execute(
            f"""SELECT me.id, me.title, me.description, me.category,
                       me.event_date, me.source, me.impact_direction,
                       me.impact_magnitude, me.amount_inr_cr,
                       mtl.relevance_score
                FROM macro_theme_links mtl
                JOIN macro_events me ON me.id = mtl.macro_event_id
                {where}
                ORDER BY me.event_date DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_macro_pressure(self, theme: str, quarter: str) -> dict:
        """
        Compute net macro pressure on a theme for a given quarter.
        Returns: positive_events, negative_events, net_score (-1 to +1).
        """
        year, qnum = _parse_quarter_dates(quarter)
        from_dt = date(year, (qnum - 1) * 3 + 1, 1)
        # End of quarter
        end_month = qnum * 3
        end_day = 31 if end_month in (1, 3, 5, 7, 8, 10, 12) else 30
        to_dt = date(year, end_month, end_day)

        events = self.get_events_for_theme(theme, from_date=from_dt, to_date=to_dt)

        magnitude_weight = {
            "low": 0.25,
            "medium": 0.5,
            "high": 0.75,
            "game_changer": 1.0,
        }

        pos_score = sum(
            magnitude_weight.get(e["impact_magnitude"], 0.5)
            for e in events if e["impact_direction"] == "positive"
        )
        neg_score = sum(
            magnitude_weight.get(e["impact_magnitude"], 0.5)
            for e in events if e["impact_direction"] == "negative"
        )
        total = pos_score + neg_score
        net = (pos_score - neg_score) / total if total > 0 else 0.0

        return {
            "theme": theme,
            "quarter": quarter,
            "positive_events": [e for e in events if e["impact_direction"] == "positive"],
            "negative_events": [e for e in events if e["impact_direction"] == "negative"],
            "net_score": round(net, 3),
            "event_count": len(events),
        }

    def get_recent_events(self, days: int = 90, limit: int = 30) -> list[dict]:
        """Get most recent macro events regardless of theme."""
        rows = self.conn.execute(
            """SELECT id, title, category, event_date, source,
                      impact_direction, impact_magnitude, themes
               FROM macro_events
               WHERE event_date >= date('now', ?)
               ORDER BY event_date DESC
               LIMIT ?""",
            (f"-{days} days", limit),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["themes"] = json.loads(d["themes"])
            results.append(d)
        return results

    def get_all_events(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM macro_events ORDER BY event_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["themes"] = json.loads(d.get("themes", "[]"))
            d["tags"] = json.loads(d.get("tags", "[]"))
            results.append(d)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auto_match_themes(self, event: MacroEvent) -> list[str]:
        """Match event to themes using keyword patterns."""
        search_text = f"{event.title} {event.description}".lower()
        matched = []
        for theme, patterns in self._trigger_patterns.items():
            if any(pat.search(search_text) for pat in patterns):
                matched.append(theme)
        return matched

    def _link_theme(
        self,
        event_id: int,
        theme_name: str,
        direction: ImpactDirection,
        magnitude: ImpactMagnitude,
    ):
        magnitude_score = {
            ImpactMagnitude.LOW: 0.25,
            ImpactMagnitude.MEDIUM: 0.5,
            ImpactMagnitude.HIGH: 0.75,
            ImpactMagnitude.GAME_CHANGER: 1.0,
        }.get(magnitude, 0.5)

        try:
            self.conn.execute(
                """INSERT INTO macro_theme_links
                   (macro_event_id, theme_name, relevance_score, impact_direction, impact_magnitude)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(macro_event_id, theme_name) DO NOTHING""",
                (event_id, theme_name, magnitude_score, direction.value, magnitude.value),
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to link macro event {event_id} → {theme_name}: {e}")

    def close(self):
        if self.conn:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Convenience builder for common Indian macro events
# ---------------------------------------------------------------------------

def make_budget_event(
    title: str,
    description: str,
    year: int,
    amount_inr_cr: Optional[float] = None,
    themes: list[str] = None,
    direction: ImpactDirection = ImpactDirection.POSITIVE,
    magnitude: ImpactMagnitude = ImpactMagnitude.HIGH,
) -> MacroEvent:
    return MacroEvent(
        title=title,
        description=description,
        category=MacroCategory.BUDGET_ALLOCATION,
        event_date=date(year, 2, 1),   # Union Budget typically presented Feb 1
        source=f"Union Budget {year}",
        impact_direction=direction,
        impact_magnitude=magnitude,
        amount_inr_cr=amount_inr_cr,
        themes=themes or [],
    )


def make_policy_event(
    title: str,
    description: str,
    event_date: date,
    source: str,
    themes: list[str] = None,
    direction: ImpactDirection = ImpactDirection.POSITIVE,
    magnitude: ImpactMagnitude = ImpactMagnitude.MEDIUM,
) -> MacroEvent:
    return MacroEvent(
        title=title,
        description=description,
        category=MacroCategory.GOVERNMENT_POLICY,
        event_date=event_date,
        source=source,
        impact_direction=direction,
        impact_magnitude=magnitude,
        themes=themes or [],
    )


def _parse_quarter_dates(quarter: str) -> tuple[int, int]:
    """Parse 'Q2-2024' → (2024, 2)."""
    try:
        parts = quarter.upper().replace("Q", "").split("-")
        return int(parts[1]), int(parts[0])
    except Exception:
        from datetime import datetime
        now = datetime.utcnow()
        return now.year, (now.month - 1) // 3 + 1
