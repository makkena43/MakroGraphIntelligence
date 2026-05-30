"""EIA (U.S. Energy Information Administration) data fetcher.

Fetches oil, natural gas, electricity, and refinery data from the
EIA Open Data API v2.

Endpoints used:
    https://api.eia.gov/v2/petroleum/pri/spt/data/   (spot prices)
    https://api.eia.gov/v2/natural-gas/pri/sum/data/ (gas prices)
    https://api.eia.gov/v2/electricity/retail-sales/data/ (electricity)
    https://api.eia.gov/v2/petroleum/sum/sndw/data/  (crude inventory)

API key: free at https://www.eia.gov/opendata/
Set EIA_API_KEY env var or eia.api_key in settings.yaml.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

EIA_BASE = "https://api.eia.gov/v2"

# (route, facet_filters, value_column, commodity_id, commodity_name, units, category)
# Frequencies: petroleum spot = monthly, crude inventory sndw = weekly, electricity = monthly
DEFAULT_DATASETS = [
    # WTI Crude spot price (monthly) — Cushing OK
    (
        "petroleum/pri/spt/data",
        {"series": ["RWTC"]},
        "value", "WTI_CRUDE", "WTI Crude Oil Spot Price", "Dollars per Barrel", "energy",
    ),
    # Brent Crude spot price (monthly)
    (
        "petroleum/pri/spt/data",
        {"series": ["RBRTE"]},
        "value", "BRENT_CRUDE", "Brent Crude Oil Spot Price", "Dollars per Barrel", "energy",
    ),
    # Henry Hub — NYMEX NG Futures Contract 1 (best proxy, monthly)
    # Note: EIA v2 no longer publishes Henry Hub daily spot; futures C1 is the standard proxy
    (
        "natural-gas/pri/fut/data",
        {"series": ["RNGC1"], "duoarea": ["Y35NY"]},
        "value", "HENRY_HUB", "Henry Hub NG Futures Contract 1", "Dollars per MMBtu", "energy",
    ),
    # US Crude Oil Ending Stocks excl. SPR (weekly → store as-is)
    (
        "petroleum/sum/sndw/data",
        {"series": ["WCESTUS1"]},
        "value", "US_CRUDE_INVENTORY", "US Crude Oil Stocks excl. SPR", "Thousand Barrels", "energy",
    ),
    # US Crude Oil total ending stocks monthly (from crdsnd — good for trends)
    (
        "petroleum/sum/crdsnd/data",
        {"series": ["MCRSTUS1"]},
        "value", "US_CRUDE_STOCKS_TOTAL", "US Crude Oil Total Ending Stocks", "Thousand Barrels", "energy",
    ),
    # Electricity retail price — US Total, all sectors (monthly)
    (
        "electricity/retail-sales/data",
        {"stateid": ["US"], "sectorid": ["ALL"]},
        "price", "ELEC_RETAIL_US", "US Electricity Retail Price (All Sectors)", "Cents per kWh", "energy",
    ),
    # Natural gas electric power consumption (monthly)
    (
        "natural-gas/cons/sum/data",
        {"series": ["N3045US2"]},
        "value", "NG_ELECTRIC_CONS", "Natural Gas for Electricity Generation", "MMcf", "energy",
    ),
]


class EiaFetcher(SourceAdapter):
    """Fetches EIA energy commodity and inventory data.

    Config keys (under `eia:` in settings.yaml):
        api_key           - EIA v2 API key (or EIA_API_KEY env var)
        datasets          - list of dataset specs (overrides defaults)
        start_date        - ISO date, default "2018-01-01"
        end_date          - ISO date, default today
        api_delay_seconds - delay between requests
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key = (
            config.get("api_key")
            or os.environ.get("EIA_API_KEY", "")
        )
        self.start_date: str = config.get("start_date", "2018-01-01")
        self.end_date: str = config.get("end_date", datetime.utcnow().strftime("%Y-%m-%d"))

        self.session.headers.update({"Accept": "application/json"})

    @property
    def source_name(self) -> str:
        return "eia"

    def _fetch_dataset(
        self,
        route: str,
        facets: dict,
        value_col: str,
        start_date: str,
        end_date: str,
        frequency: str = "monthly",
    ) -> list[dict]:
        """Generic EIA v2 data fetch.

        frequency: 'monthly' (default) or 'weekly' — weekly routes
        return YYYY-MM-DD periods and are stored as-is.
        """
        if not self.api_key:
            logger.warning("EIA_API_KEY not set — skipping EIA fetch.")
            return []

        url = f"{EIA_BASE}/{route}"
        params = {
            "api_key": self.api_key,
            "frequency": frequency,
            "data[0]": value_col,
            "start": start_date,
            "end": end_date,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": 5000,
        }
        # Encode facets as facets[key][]=value
        for k, vals in facets.items():
            for i, v in enumerate(vals):
                params[f"facets[{k}][]"] = v

        try:
            resp = self._api_get(url, params=params)
            return resp.get("response", {}).get("data", [])
        except Exception as e:
            logger.error(f"EIA {route} fetch failed: {e}")
            return []

    # Weekly routes need a different frequency param
    _WEEKLY_ROUTES = {"petroleum/sum/sndw/data"}

    def fetch_commodity(
        self,
        route: str,
        facets: dict,
        value_col: str,
        commodity_id: str,
        commodity_name: str,
        units: str,
        category: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Fetch one EIA dataset and normalise to commodity rows.

        Automatically uses 'weekly' frequency for weekly routes (sndw).
        Period normalisation:
            YYYY-MM      → YYYY-MM-01   (monthly)
            YYYY-MM-DD   → kept as-is   (weekly)
        """
        freq = "weekly" if route in self._WEEKLY_ROUTES else "monthly"
        raw = self._fetch_dataset(
            route, facets, value_col,
            start_date or self.start_date,
            end_date or self.end_date,
            frequency=freq,
        )
        rows = []
        for obs in raw:
            period = obs.get("period", "")
            # Monthly: "YYYY-MM" → "YYYY-MM-01"
            if len(period) == 7:
                period = f"{period}-01"
            # Weekly: "YYYY-MM-DD" → kept as-is (already a full date)
            val = obs.get(value_col)
            if val is None:
                continue
            try:
                value = float(val)
            except (ValueError, TypeError):
                continue

            rows.append({
                "commodity_id":     commodity_id,
                "commodity_name":   commodity_name,
                "category":         category,
                "source":           "eia",
                "units":            units,
                "observation_date": period,
                "value":            value,
            })

        logger.info(f"EIA {commodity_id} ({freq}): {len(rows)} observations")
        return rows

    def fetch_all(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict[str, list[dict]]:
        """Fetch every default EIA dataset. Returns {commodity_id: [rows]}."""
        results: dict[str, list[dict]] = {}
        for route, facets, val_col, cid, cname, units, cat in DEFAULT_DATASETS:
            rows = self.fetch_commodity(
                route, facets, val_col, cid, cname, units, cat,
                start_date, end_date,
            )
            results[cid] = rows
            import time as _time
            _time.sleep(self.config.get("api_delay_seconds", 0.5))
        return results

    def discover(self, since: Optional[datetime] = None) -> list[SourceDocument]:
        return []
