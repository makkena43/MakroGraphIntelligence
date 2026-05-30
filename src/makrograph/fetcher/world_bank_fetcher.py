"""World Bank Open Data fetcher.

Fetches country-level macroeconomic and development indicators via the
World Bank Data API v2. No API key required.

Endpoint: https://api.worldbank.org/v2/country/{country}/indicator/{indicator}

Covers:
  - GDP and growth rates
  - Population and urbanisation
  - Energy consumption and production
  - Trade (exports/imports as % of GDP)
  - Debt-to-GDP ratios
  - Foreign reserves
  - Infrastructure investment
"""

import logging
from datetime import datetime
from typing import Optional

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

WB_BASE = "https://api.worldbank.org/v2"

# G20 + key trading partners (ISO-2)
DEFAULT_COUNTRIES = [
    "US", "CN", "DE", "JP", "IN", "KR", "TW",
    "GB", "FR", "CA", "AU", "BR", "MX", "SA",
    "RU", "ID", "TR", "ZA", "AR",
]

# (indicator_code, series_name, units, category)
DEFAULT_INDICATORS = [
    # Economy
    ("NY.GDP.MKTP.CD",      "GDP (current USD)",                "USD",         "economy"),
    ("NY.GDP.MKTP.KD.ZG",   "GDP growth rate (annual %)",       "Percent",     "economy"),
    ("NY.GDP.PCAP.CD",      "GDP per capita (current USD)",     "USD",         "economy"),
    ("FP.CPI.TOTL.ZG",      "CPI inflation (annual %)",         "Percent",     "economy"),
    # Trade
    ("NE.EXP.GNFS.ZS",      "Exports of goods/services (% GDP)", "Percent",   "trade"),
    ("NE.IMP.GNFS.ZS",      "Imports of goods/services (% GDP)", "Percent",   "trade"),
    ("BN.CAB.XOKA.CD",      "Current account balance (USD)",    "USD",         "trade"),
    # Debt & Reserves
    ("GC.DOD.TOTL.GD.ZS",   "Government debt (% GDP)",          "Percent",     "debt"),
    ("FI.RES.TOTL.CD",      "Total reserves (USD)",             "USD",         "reserves"),
    # Energy
    ("EG.USE.PCAP.KG.OE",   "Energy use per capita (kg oil eq)","kg oil eq",   "energy"),
    ("EG.ELC.ACCS.ZS",      "Access to electricity (% pop)",    "Percent",     "energy"),
    ("EG.FEC.RNEW.ZS",      "Renewable energy share (% total)", "Percent",     "energy"),
    # Demographics
    ("SP.POP.TOTL",          "Population (total)",               "Persons",     "demographics"),
    ("SP.URB.TOTL.IN.ZS",    "Urban population (% total)",       "Percent",     "demographics"),
    # Infrastructure
    ("IS.ROD.TOTL.KM",       "Road network (km)",                "km",          "infrastructure"),
]


class WorldBankFetcher(SourceAdapter):
    """Fetches World Bank country indicators.

    Config keys (under `world_bank:` in settings.yaml):
        countries         - list of ISO-2 country codes
        indicators        - list of WB indicator codes (overrides defaults)
        start_year        - int, e.g. 2018
        end_year          - int, e.g. 2024
        api_delay_seconds - default 0.5 (WB is generous but rate-limited)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.countries: list[str] = config.get("countries", DEFAULT_COUNTRIES)
        self.start_year: int = int(config.get("start_year", 2018))
        self.end_year: int = int(config.get("end_year", datetime.utcnow().year))

        custom_indicators = config.get("indicators", [])
        if custom_indicators:
            self.indicators = [
                (ind, ind, "", "economy") if isinstance(ind, str) else tuple(ind)
                for ind in custom_indicators
            ]
        else:
            self.indicators = DEFAULT_INDICATORS

        self.session.headers.update({"Accept": "application/json"})

    @property
    def source_name(self) -> str:
        return "world_bank"

    def _fetch_indicator(
        self,
        country: str,
        indicator: str,
        start_year: int,
        end_year: int,
    ) -> list[dict]:
        """Fetch all years of one indicator for one country."""
        url = f"{WB_BASE}/country/{country}/indicator/{indicator}"
        params = {
            "format": "json",
            "per_page": 100,
            "date": f"{start_year}:{end_year}",
            "mrv": 50,
        }
        try:
            resp = self._api_get(url, params=params)
            # WB returns [metadata_dict, [data_list]] as a list
            if isinstance(resp, list) and len(resp) >= 2:
                return resp[1] or []
            # Our _api_get wraps text in {"_raw": ...} when content-type isn't JSON
            return []
        except Exception as e:
            logger.debug(f"World Bank {country}/{indicator}: {e}")
            return []

    def fetch_country_indicators(
        self,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> list[dict]:
        """Fetch all configured indicators for all countries.

        Returns list of dicts for MacroStore.upsert_macro_series().
        Fields: series_id, series_name, source, country, units,
                observation_date, value, frequency
        """
        sy = start_year or self.start_year
        ey = end_year or self.end_year

        rows: list[dict] = []
        for country in self.countries:
            for indicator_code, ind_name, units, _cat in self.indicators:
                data = self._fetch_indicator(country, indicator_code, sy, ey)
                for obs in data:
                    val = obs.get("value")
                    if val is None:
                        continue
                    try:
                        value = float(val)
                    except (TypeError, ValueError):
                        continue

                    year = obs.get("date", "")
                    if not year:
                        continue
                    # WB returns year as "2022" — convert to Jan 1
                    obs_date = f"{year}-01-01"

                    rows.append({
                        "series_id":       f"WB_{indicator_code}",
                        "series_name":     ind_name,
                        "source":          "world_bank",
                        "country":         country,
                        "units":           units,
                        "frequency":       "annual",
                        "observation_date": obs_date,
                        "value":           value,
                    })

            logger.debug(f"World Bank {country}: fetched indicators")

        logger.info(f"World Bank: {len(rows)} observations for {len(self.countries)} countries")
        return rows

    def discover(self, since: Optional[datetime] = None) -> list[SourceDocument]:
        return []
