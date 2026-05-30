"""FRED / ALFRED macro data fetcher.

Fetches economic time-series from the St. Louis Fed FRED API and its
vintage archive (ALFRED). Supports GDP, CPI, unemployment, treasury
yields, industrial production, M2, and any other FRED series by ID.

ALFRED mode (use_alfred=True) fetches the version of each data point
that was *first published* at that time, preventing look-ahead bias
in historical replays.

Endpoints:
    https://api.stlouisfed.org/fred/series/observations
    https://api.stlouisfed.org/alfred/series/observations (vintage)

API key: free registration at https://fred.stlouisfed.org/docs/api/fred/
Set FRED_API_KEY env var or fred.api_key in settings.yaml.
"""

import logging
import os
from datetime import date, datetime
from typing import Optional

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"
ALFRED_BASE = "https://api.stlouisfed.org/alfred"

# Default series to fetch when none configured explicitly
DEFAULT_SERIES = [
    # Core macro
    {"id": "GDP",       "name": "US Real GDP",                  "freq": "quarterly"},
    {"id": "GDPC1",     "name": "US Real GDP (Chained 2017$)",  "freq": "quarterly"},
    {"id": "CPIAUCSL",  "name": "CPI All Urban Consumers SA",   "freq": "monthly"},
    {"id": "CPILFESL",  "name": "Core CPI (ex Food & Energy)",  "freq": "monthly"},
    {"id": "PCEPI",     "name": "PCE Price Index",              "freq": "monthly"},
    {"id": "UNRATE",    "name": "Unemployment Rate",            "freq": "monthly"},
    {"id": "PAYEMS",    "name": "Nonfarm Payrolls",             "freq": "monthly"},
    {"id": "INDPRO",    "name": "Industrial Production Index",  "freq": "monthly"},
    {"id": "HOUST",     "name": "Housing Starts",               "freq": "monthly"},
    # Rates & yield curve
    {"id": "FEDFUNDS",  "name": "Federal Funds Rate",           "freq": "monthly"},
    {"id": "DGS10",     "name": "10-Year Treasury Yield",       "freq": "daily"},
    {"id": "DGS2",      "name": "2-Year Treasury Yield",        "freq": "daily"},
    {"id": "DGS3MO",    "name": "3-Month Treasury Yield",       "freq": "daily"},
    {"id": "T10Y2Y",    "name": "10Y-2Y Yield Spread",         "freq": "daily"},
    {"id": "BAMLH0A0HYM2", "name": "HY Credit Spread",         "freq": "daily"},
    # Money supply & credit
    {"id": "M2SL",      "name": "M2 Money Supply",              "freq": "monthly"},
    {"id": "TOTLL",     "name": "Total Bank Loans & Leases",    "freq": "weekly"},
    # Commodities via FRED
    {"id": "DCOILWTICO","name": "WTI Crude Oil Price",          "freq": "daily"},
    {"id": "DHHNGSP",   "name": "Henry Hub Natural Gas Price",  "freq": "daily"},
    # Global
    {"id": "DEXUSEU",   "name": "USD/EUR Exchange Rate",        "freq": "daily"},
    {"id": "DEXCHUS",   "name": "USD/CNY Exchange Rate",        "freq": "daily"},
]


class FredFetcher(SourceAdapter):
    """Fetches FRED macro time-series data.

    Config keys (under `fred:` in settings.yaml):
        api_key           - FRED API key (or set FRED_API_KEY env var)
        series_ids        - list of series to fetch (overrides defaults)
        use_alfred        - bool, fetch vintage (first-published) values
        start_date        - ISO date string, default "2018-01-01"
        end_date          - ISO date string, default today
        api_delay_seconds - delay between requests (FRED allows 120 req/min)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = (
            config.get("api_key")
            or os.environ.get("FRED_API_KEY", "")
        )
        self.use_alfred: bool = config.get("use_alfred", False)
        self.start_date: str = config.get("start_date", "2018-01-01")
        self.end_date: str = config.get("end_date", date.today().isoformat())
        series_cfg = config.get("series_ids", [])
        if series_cfg:
            self.series_list = [
                {"id": s, "name": s, "freq": "auto"} if isinstance(s, str) else s
                for s in series_cfg
            ]
        else:
            self.series_list = DEFAULT_SERIES

        self.session.headers.update({"Accept": "application/json"})

    @property
    def source_name(self) -> str:
        return "fred"

    def _fetch_series_observations(
        self,
        series_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Fetch observations for a single FRED series."""
        if not self.api_key:
            logger.warning("FRED API key not set — skipping series fetch. Set FRED_API_KEY env var.")
            return []

        base = ALFRED_BASE if self.use_alfred else FRED_BASE
        url = f"{base}/series/observations"
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start_date or self.start_date,
            "observation_end": end_date or self.end_date,
            "sort_order": "asc",
        }
        if self.use_alfred:
            params["vintage_dates"] = end_date or self.end_date

        try:
            data = self._api_get(url, params=params)
            return data.get("observations", [])
        except Exception as e:
            logger.error(f"FRED series {series_id} fetch failed: {e}")
            return []

    def fetch_series(
        self,
        series_id: str,
        series_name: str = "",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Fetch and normalise observations for one series.

        Returns list of dicts ready for MacroStore.upsert_macro_series().
        Each dict: series_id, series_name, source, observation_date,
                   value, vintage_date, is_revised
        """
        raw = self._fetch_series_observations(series_id, start_date, end_date)
        rows = []
        for obs in raw:
            raw_val = obs.get("value", ".")
            if raw_val == "." or raw_val is None:
                continue
            try:
                value = float(raw_val)
            except (ValueError, TypeError):
                continue

            rows.append({
                "series_id":       series_id,
                "series_name":     series_name or series_id,
                "source":          "alfred" if self.use_alfred else "fred",
                "country":         "US",
                "observation_date": obs.get("date"),
                "value":           value,
                "vintage_date":    obs.get("vintage_date"),
                "is_revised":      False,
            })

        logger.info(f"FRED {series_id}: {len(rows)} observations [{start_date or self.start_date} → {end_date or self.end_date}]")
        return rows

    def fetch_all_series(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict[str, list[dict]]:
        """Fetch every configured series. Returns {series_id: [rows]}."""
        results: dict[str, list[dict]] = {}
        for s in self.series_list:
            sid = s["id"]
            rows = self.fetch_series(sid, s.get("name", sid), start_date, end_date)
            results[sid] = rows
        return results

    # SourceAdapter protocol — FRED data arrives as structured dicts,
    # not downloadable documents, so discover() returns an empty list.
    # Callers should use fetch_all_series() directly.
    def discover(self, since: Optional[datetime] = None) -> list[SourceDocument]:
        return []
