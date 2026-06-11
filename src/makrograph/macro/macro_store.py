"""PostgreSQL persistence layer for the macro/policy data layer.

Handles upserts for:
  - mg_macro_series       (FRED, World Bank, IMF observations)
  - mg_commodity_series   (EIA, USDA commodity prices/inventories)
  - mg_policy_events      (Congress bills, Federal Register rules)
  - mg_macro_events       (computed threshold crossings)
  - mg_macro_theme_links  (constraint engine output)

All writes are idempotent (ON CONFLICT DO UPDATE) so the fetchers can
be re-run safely without duplicating data.
"""

import logging
from datetime import date, datetime
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


class MacroStore:
    """Read/write macro & policy data to PostgreSQL.

    Usage:
        store = MacroStore(pg_config)
        store.upsert_macro_series(rows)      # FRED / World Bank
        store.upsert_commodity_series(rows)  # EIA / USDA
        store.upsert_policy_event(events)    # Congress / Federal Register
        store.detect_and_store_events(as_of) # Emit macro threshold events
    """

    def __init__(self, config: dict):
        self.config = config
        self._conn = None

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(
                host=self.config.get("host", "localhost"),
                port=self.config.get("port", 5432),
                dbname=self.config.get("dbname", "makrograph"),
                user=self.config.get("user", "postgres"),
                password=self.config.get("password", ""),
            )
            self._conn.autocommit = False
        return self._conn

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ----------------------------------------------------------
    # MACRO SERIES
    # ----------------------------------------------------------
    def upsert_macro_series(self, rows: list[dict]) -> int:
        """Upsert FRED/World Bank observations into mg_macro_series."""
        if not rows:
            return 0

        conn = self._get_conn()
        sql = """
        INSERT INTO mg_macro_series
            (series_id, series_name, source, country, frequency, units,
             observation_date, value, vintage_date, is_revised)
        VALUES
            (%(series_id)s, %(series_name)s, %(source)s,
             %(country)s, %(frequency)s, %(units)s,
             %(observation_date)s, %(value)s, %(vintage_date)s, %(is_revised)s)
        ON CONFLICT (series_id, observation_date, vintage_date)
        DO UPDATE SET
            value        = EXCLUDED.value,
            series_name  = EXCLUDED.series_name,
            units        = EXCLUDED.units,
            fetched_at   = NOW()
        """
        # Supply defaults for optional fields
        normalised = []
        for r in rows:
            normalised.append({
                "series_id":       r.get("series_id", ""),
                "series_name":     r.get("series_name", r.get("series_id", "")),
                "source":          r.get("source", "fred"),
                "country":         r.get("country", "US"),
                "frequency":       r.get("frequency", ""),
                "units":           r.get("units", ""),
                "observation_date": r.get("observation_date"),
                "value":           r.get("value"),
                "vintage_date":    r.get("vintage_date"),
                "is_revised":      r.get("is_revised", False),
            })

        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, normalised, page_size=500)
            conn.commit()
            logger.info(f"MacroStore: upserted {len(normalised)} macro series rows")
            return len(normalised)
        except Exception as e:
            conn.rollback()
            logger.error(f"MacroStore.upsert_macro_series failed: {e}")
            return 0

    # ----------------------------------------------------------
    # COMMODITY SERIES
    # ----------------------------------------------------------
    def upsert_commodity_series(self, rows: list[dict]) -> int:
        """Upsert EIA/USDA commodity observations into mg_commodity_series."""
        if not rows:
            return 0

        conn = self._get_conn()
        sql = """
        INSERT INTO mg_commodity_series
            (commodity_id, commodity_name, category, source, units,
             observation_date, value, volume, inventory_change)
        VALUES
            (%(commodity_id)s, %(commodity_name)s, %(category)s, %(source)s,
             %(units)s, %(observation_date)s, %(value)s, %(volume)s, %(inventory_change)s)
        ON CONFLICT (commodity_id, observation_date)
        DO UPDATE SET
            value            = EXCLUDED.value,
            volume           = EXCLUDED.volume,
            inventory_change = EXCLUDED.inventory_change,
            fetched_at       = NOW()
        """
        normalised = [
            {
                "commodity_id":    r.get("commodity_id", ""),
                "commodity_name":  r.get("commodity_name", r.get("commodity_id", "")),
                "category":        r.get("category", "energy"),
                "source":          r.get("source", "eia"),
                "units":           r.get("units", ""),
                "observation_date": r.get("observation_date"),
                "value":           r.get("value"),
                "volume":          r.get("volume"),
                "inventory_change": r.get("inventory_change"),
            }
            for r in rows
        ]
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, normalised, page_size=500)
            conn.commit()
            logger.info(f"MacroStore: upserted {len(normalised)} commodity rows")
            return len(normalised)
        except Exception as e:
            conn.rollback()
            logger.error(f"MacroStore.upsert_commodity_series failed: {e}")
            return 0

    # ----------------------------------------------------------
    # POLICY EVENTS
    # ----------------------------------------------------------
    def upsert_policy_events(self, events: list[dict]) -> int:
        """Upsert policy/regulatory events into mg_policy_events.

        Supports both US sources (Congress, Federal Register) and
        India macro sources (PIB, SEBI, RBI, InvestIndia, Commerce/DGFT).
        Pass ``country`` in each event dict (e.g. ``"IN"`` or ``"US"``);
        defaults to ``"US"`` to remain backwards-compatible with existing
        Congress / Federal Register callers that don't set the field.
        """
        if not events:
            return 0

        conn = self._get_conn()
        sql = """
        INSERT INTO mg_policy_events
            (policy_id, source, policy_type, title, description, status,
             introduced_date, enacted_date, effective_date, sponsor,
             sectors_affected, technologies_affected, impact_direction,
             impact_magnitude, keywords, raw_url, country)
        VALUES
            (%(policy_id)s, %(source)s, %(policy_type)s, %(title)s,
             %(description)s, %(status)s, %(introduced_date)s, %(enacted_date)s,
             %(effective_date)s, %(sponsor)s, %(sectors_affected)s,
             %(technologies_affected)s, %(impact_direction)s, %(impact_magnitude)s,
             %(keywords)s, %(raw_url)s, %(country)s)
        ON CONFLICT (policy_id)
        DO UPDATE SET
            status             = EXCLUDED.status,
            enacted_date       = EXCLUDED.enacted_date,
            effective_date     = EXCLUDED.effective_date,
            impact_direction   = EXCLUDED.impact_direction,
            impact_magnitude   = EXCLUDED.impact_magnitude,
            sectors_affected   = EXCLUDED.sectors_affected,
            technologies_affected = EXCLUDED.technologies_affected,
            country            = EXCLUDED.country,
            created_at         = mg_policy_events.created_at
        """
        normalised = [
            {
                "policy_id":           e.get("policy_id", ""),
                "source":              e.get("source", ""),
                "policy_type":         e.get("policy_type", "notice"),
                "title":               (e.get("title", "") or "")[:1000],
                "description":         (e.get("description", "") or "")[:2000],
                "status":              e.get("status", ""),
                "introduced_date":     e.get("introduced_date") or None,
                "enacted_date":        e.get("enacted_date") or None,
                "effective_date":      e.get("effective_date") or None,
                "sponsor":             e.get("sponsor", ""),
                "sectors_affected":    e.get("sectors_affected", []),
                "technologies_affected": e.get("technologies_affected", []),
                "impact_direction":    e.get("impact_direction", "neutral"),
                "impact_magnitude":    float(e.get("impact_magnitude", 0.0)),
                "keywords":            e.get("keywords", []),
                "raw_url":             e.get("raw_url", ""),
                "country":             e.get("country", "US"),
            }
            for e in events
        ]
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, normalised, page_size=200)
            conn.commit()
            logger.info(f"MacroStore: upserted {len(normalised)} policy events")
            return len(normalised)
        except Exception as e:
            conn.rollback()
            logger.error(f"MacroStore.upsert_policy_events failed: {e}")
            return 0

    # ----------------------------------------------------------
    # MACRO EVENTS (threshold crossings)
    # ----------------------------------------------------------
    def upsert_macro_event(self, event: dict) -> Optional[int]:
        """Insert one macro threshold event; return its DB id."""
        conn = self._get_conn()
        sql = """
        INSERT INTO mg_macro_events
            (event_type, series_id, commodity_id, policy_id, event_date,
             description, severity, direction,
             threshold_value, observed_value, prior_value, change_pct,
             sectors_at_risk, sectors_benefit, themes_triggered, replay_safe_date)
        VALUES
            (%(event_type)s, %(series_id)s, %(commodity_id)s, %(policy_id)s,
             %(event_date)s, %(description)s, %(severity)s, %(direction)s,
             %(threshold_value)s, %(observed_value)s, %(prior_value)s, %(change_pct)s,
             %(sectors_at_risk)s, %(sectors_benefit)s, %(themes_triggered)s,
             %(replay_safe_date)s)
        ON CONFLICT (event_type, series_id, event_date) DO UPDATE SET
            severity       = GREATEST(mg_macro_events.severity, EXCLUDED.severity),
            description    = EXCLUDED.description,
            themes_triggered = EXCLUDED.themes_triggered
        RETURNING id
        """
        row = {
            "event_type":       event.get("event_type", ""),
            "series_id":        event.get("series_id"),
            "commodity_id":     event.get("commodity_id"),
            "policy_id":        event.get("policy_id"),
            "event_date":       event.get("event_date"),
            "description":      (event.get("description", "") or "")[:2000],
            "severity":         float(event.get("severity", 0.0)),
            "direction":        event.get("direction", ""),
            "threshold_value":  event.get("threshold_value"),
            "observed_value":   event.get("observed_value"),
            "prior_value":      event.get("prior_value"),
            "change_pct":       event.get("change_pct"),
            "sectors_at_risk":  event.get("sectors_at_risk", []),
            "sectors_benefit":  event.get("sectors_benefit", []),
            "themes_triggered": event.get("themes_triggered", []),
            "replay_safe_date": event.get("replay_safe_date") or event.get("event_date"),
        }
        try:
            with conn.cursor() as cur:
                cur.execute(sql, row)
                result = cur.fetchone()
            conn.commit()
            return result[0] if result else None
        except Exception as e:
            conn.rollback()
            logger.error(f"MacroStore.upsert_macro_event failed: {e}")
            return None

    # ----------------------------------------------------------
    # MACRO-THEME LINKS
    # ----------------------------------------------------------
    def upsert_macro_theme_link(self, link: dict) -> bool:
        """Store a constraint engine output link between macro event and theme."""
        conn = self._get_conn()
        sql = """
        INSERT INTO mg_macro_theme_links
            (theme_slug, link_type, macro_event_id, policy_event_id,
             series_id, commodity_id, evidence_text, strength, as_of_date)
        VALUES
            (%(theme_slug)s, %(link_type)s, %(macro_event_id)s, %(policy_event_id)s,
             %(series_id)s, %(commodity_id)s, %(evidence_text)s, %(strength)s, %(as_of_date)s)
        ON CONFLICT (theme_slug, link_type, macro_event_id, policy_event_id, as_of_date)
        DO UPDATE SET
            strength     = GREATEST(mg_macro_theme_links.strength, EXCLUDED.strength),
            evidence_text = EXCLUDED.evidence_text
        """
        row = {
            "theme_slug":     link.get("theme_slug", ""),
            "link_type":      link.get("link_type", "corroborates"),
            "macro_event_id": link.get("macro_event_id"),
            "policy_event_id": link.get("policy_event_id"),
            "series_id":      link.get("series_id"),
            "commodity_id":   link.get("commodity_id"),
            "evidence_text":  (link.get("evidence_text", "") or "")[:2000],
            "strength":       float(link.get("strength", 0.0)),
            "as_of_date":     link.get("as_of_date", date.today()),
        }
        try:
            with conn.cursor() as cur:
                cur.execute(sql, row)
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            logger.error(f"MacroStore.upsert_macro_theme_link failed: {e}")
            return False

    # ----------------------------------------------------------
    # READ HELPERS
    # ----------------------------------------------------------
    def get_series_latest(
        self,
        series_ids: list[str],
        as_of_date: Optional[date] = None,
    ) -> dict[str, dict]:
        """Return the most recent observation for each series as of as_of_date.

        Returns {series_id: {observation_date, value, series_name, units}}
        """
        if not series_ids:
            return {}
        conn = self._get_conn()
        ceiling = as_of_date or date.today()
        placeholders = ", ".join(["%s"] * len(series_ids))
        sql = f"""
        SELECT DISTINCT ON (series_id)
            series_id, series_name, units, observation_date, value
        FROM mg_macro_series
        WHERE series_id IN ({placeholders})
          AND observation_date <= %s
        ORDER BY series_id, observation_date DESC
        """
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, series_ids + [ceiling])
                rows = cur.fetchall()
            return {r["series_id"]: dict(r) for r in rows}
        except Exception as e:
            logger.error(f"MacroStore.get_series_latest failed: {e}")
            return {}

    def get_series_history(
        self,
        series_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        country: Optional[str] = None,
    ) -> list[dict]:
        """Return ordered time series for charting (one row per observation date)."""
        conn = self._get_conn()
        sql = """
        SELECT DISTINCT ON (observation_date)
            observation_date, value, series_name, units, source, country
        FROM mg_macro_series
        WHERE series_id = %s
          AND (%s IS NULL OR observation_date >= %s)
          AND (%s IS NULL OR observation_date <= %s)
          AND (%s IS NULL OR country = %s)
        ORDER BY observation_date ASC, vintage_date DESC
        """
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, [series_id, start_date, start_date, end_date, end_date, country, country])
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"MacroStore.get_series_history failed: {e}")
            return []

    def get_commodity_history(
        self,
        commodity_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> list[dict]:
        conn = self._get_conn()
        sql = """
        SELECT observation_date, value, commodity_name, units, category
        FROM mg_commodity_series
        WHERE commodity_id = %s
          AND (%s IS NULL OR observation_date >= %s)
          AND (%s IS NULL OR observation_date <= %s)
        ORDER BY observation_date ASC
        """
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, [commodity_id, start_date, start_date, end_date, end_date])
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"MacroStore.get_commodity_history failed: {e}")
            return []

    def get_recent_policy_events(
        self,
        as_of_date: Optional[date] = None,
        sectors: Optional[list[str]] = None,
        limit: int = 50,
        country: Optional[str] = None,
    ) -> list[dict]:
        """Return recent policy events, optionally filtered by sector and country."""
        conn = self._get_conn()
        ceiling = as_of_date or date.today()
        sql = """
        SELECT policy_id, source, policy_type, title, status,
               introduced_date, enacted_date, impact_direction,
               impact_magnitude, sectors_affected, technologies_affected
        FROM mg_policy_events
        WHERE COALESCE(enacted_date, introduced_date) <= %s
        """
        params: list = [ceiling]
        if country:
            sql += " AND COALESCE(country, 'US') = %s"
            params.append(country)
        if sectors:
            sql += " AND sectors_affected && %s::text[]"
            params.append(sectors)
        sql += " ORDER BY COALESCE(enacted_date, introduced_date) DESC LIMIT %s"
        params.append(limit)

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"MacroStore.get_recent_policy_events failed: {e}")
            return []

    def get_macro_events(
        self,
        as_of_date: Optional[date] = None,
        since_days: int = 365,
    ) -> list[dict]:
        """Return recent macro threshold events for constraint engine."""
        conn = self._get_conn()
        ceiling = as_of_date or date.today()
        sql = """
        SELECT id, event_type, series_id, commodity_id, event_date,
               description, severity, direction,
               sectors_at_risk, sectors_benefit, themes_triggered
        FROM mg_macro_events
        WHERE event_date <= %s
          AND event_date >= %s - INTERVAL '%s days'
        ORDER BY severity DESC, event_date DESC
        """
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, [ceiling, ceiling, since_days])
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"MacroStore.get_macro_events failed: {e}")
            return []

    def get_macro_theme_links(
        self,
        theme_slug: str,
        as_of_date: Optional[date] = None,
    ) -> list[dict]:
        """Return all macro constraints/corroborations for a theme."""
        conn = self._get_conn()
        ceiling = as_of_date or date.today()
        sql = """
        SELECT mtl.link_type, mtl.strength, mtl.evidence_text, mtl.as_of_date,
               me.event_type, me.description AS macro_description, me.severity,
               pe.title AS policy_title, pe.policy_type, pe.impact_direction,
               mtl.series_id, mtl.commodity_id
        FROM mg_macro_theme_links mtl
        LEFT JOIN mg_macro_events  me ON me.id  = mtl.macro_event_id
        LEFT JOIN mg_policy_events pe ON pe.id  = mtl.policy_event_id
        WHERE mtl.theme_slug = %s AND mtl.as_of_date <= %s
        ORDER BY mtl.strength DESC
        """
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, [theme_slug, ceiling])
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"MacroStore.get_macro_theme_links failed: {e}")
            return []
