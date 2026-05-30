"""SEC EDGAR filing fetcher.

Discovers and downloads SEC filings using the EDGAR full-text search API
and the company submissions endpoint. Supports 10-K, 10-Q, 8-K, and
earnings call transcripts linked from filings.

EDGAR endpoints used:
  - https://data.sec.gov/submissions/CIK{cik}.json    (company filings)
  - https://efts.sec.gov/LATEST/search-index?q=...    (full-text search)
  - https://www.sec.gov/Archives/edgar/data/           (document downloads)
"""

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .source_adapter import SourceAdapter, SourceDocument

logger = logging.getLogger(__name__)

EDGAR_BASE_URL = "https://data.sec.gov"
EDGAR_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data"
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions"


class EdgarFetcher(SourceAdapter):
    """Fetches SEC filings from EDGAR for a list of CIKs or tickers.

    Supported filing types:
        10-K  - Annual report
        10-Q  - Quarterly report
        8-K   - Current report (events)
        DEF14A - Proxy statement (for capex/governance signals)
    """

    # Exchanges considered "US public" for all_us mode
    _US_EXCHANGES = {"NYSE", "Nasdaq", "NASDAQ", "NYSE MKT", "NYSE ARCA", "BATS"}

    def __init__(self, config: dict):
        super().__init__(config)
        self.filing_types = config.get("filing_types", ["10-K", "10-Q", "8-K"])
        self.cik_list: list[str] = list(config.get("cik_list", []))
        self.ticker_list: list[str] = list(config.get("ticker_list", []))
        self.max_filings_per_company = config.get("max_filings_per_company", 100)
        self.fetch_mode: str = config.get("fetch_mode", "selected")   # "selected" | "all_us"
        self.max_companies_per_run: int = config.get("max_companies_per_run", 200)
        self._ticker_to_cik: dict[str, str] = {}
        # Ordered CIK list built for all_us mode; used for incremental batching
        self._all_us_ciks: list[str] = []

        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        })

    @property
    def source_name(self) -> str:
        return "edgar"

    def _load_ticker_map(self):
        """Fetch and cache the full EDGAR ticker → CIK map (called once)."""
        if self._ticker_to_cik:
            return
        url = "https://www.sec.gov/files/company_tickers.json"
        try:
            data = self._api_get(url)
            for _, entry in data.items():
                t = entry.get("ticker", "").upper()
                cik = str(entry["cik_str"]).zfill(10)
                if t:
                    self._ticker_to_cik[t] = cik
            logger.info(f"EDGAR ticker map loaded: {len(self._ticker_to_cik)} entries")
        except Exception as e:
            logger.error(f"Failed to load EDGAR ticker map: {e}")

    def _load_all_us_tickers(self) -> list[str]:
        """Return an ordered list of CIKs for all NYSE + NASDAQ listed companies.

        Uses company_tickers_exchange.json which includes exchange info so we
        can filter out funds, foreign-only filers, OTC pink sheets, etc.
        Falls back to company_tickers.json (no exchange filter) if the
        exchange endpoint is unavailable.

        The list is cached in self._all_us_ciks so the HTTP call only fires once.
        """
        if self._all_us_ciks:
            return self._all_us_ciks

        url = "https://www.sec.gov/files/company_tickers_exchange.json"
        try:
            data = self._api_get(url)
            # Format: {"fields": [...], "data": [[cik, name, ticker, exchange], ...]}
            fields = data.get("fields", [])
            rows = data.get("data", [])
            cik_idx = fields.index("cik") if "cik" in fields else 0
            exch_idx = fields.index("exchange") if "exchange" in fields else 3

            seen: set[str] = set()
            ciks: list[str] = []
            for row in rows:
                exchange = str(row[exch_idx] or "").strip()
                if exchange not in self._US_EXCHANGES:
                    continue
                cik = str(row[cik_idx]).zfill(10)
                if cik not in seen:
                    seen.add(cik)
                    ciks.append(cik)

            logger.info(f"All-US universe loaded: {len(ciks)} companies (NYSE + NASDAQ)")
            self._all_us_ciks = ciks
        except Exception as e:
            logger.warning(f"company_tickers_exchange.json unavailable ({e}), falling back to full ticker map")
            self._load_ticker_map()
            self._all_us_ciks = list(self._ticker_to_cik.values())

        return self._all_us_ciks

    # ------------------------------------------------------------------
    # COMPANY BATCH OFFSET  (all_us mode only)
    # ------------------------------------------------------------------
    # Offset is persisted in data/db/edgar_company_offset.json so each
    # run picks up where the last one left off, cycling through the full
    # NYSE + NASDAQ universe incrementally:
    #   Run 1: CIKs[0   : 200]  → saves offset 200
    #   Run 2: CIKs[200 : 400]  → saves offset 400
    #   ...
    #   Run N: CIKs[4800:5000]  → wraps to 0 (new cycle starts)

    _OFFSET_FILE = Path(__file__).resolve().parent.parent.parent.parent / "data" / "db" / "edgar_company_offset.json"

    def _read_offset(self) -> int:
        try:
            if self._OFFSET_FILE.exists():
                import json
                data = json.loads(self._OFFSET_FILE.read_text())
                return int(data.get("offset", 0))
        except Exception:
            pass
        return 0

    def _write_offset(self, offset: int):
        import json
        self._OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._OFFSET_FILE.write_text(json.dumps({"offset": offset}, indent=2))

    def _build_cik_list(self):
        """Populate self.cik_list from fetch_mode."""
        if self.fetch_mode == "all_us_complete":
            # Load every NYSE + NASDAQ company — no batching, no offset.
            all_ciks = self._load_all_us_tickers()
            self.cik_list = all_ciks
            logger.info(f"All-US COMPLETE: {len(all_ciks)} companies (no batch limit)")

        elif self.fetch_mode == "all_us":
            # Batched mode — advance the persistent offset so each run picks
            # up the next slice of companies.
            all_ciks = self._load_all_us_tickers()
            total = len(all_ciks)
            if total == 0:
                return

            offset = self._read_offset()
            if offset >= total:
                offset = 0

            end = min(offset + self.max_companies_per_run, total)
            self.cik_list = all_ciks[offset:end]
            next_offset = end if end < total else 0
            self._write_offset(next_offset)

            cycle_pct = round(end / total * 100, 1)
            wrapped = " — full cycle complete, wrapping to start" if next_offset == 0 else ""
            logger.info(
                f"All-US batch: [{offset}:{end}] of {total} ({cycle_pct}% through universe){wrapped}"
            )
        else:
            # "selected" mode — resolve configured tickers to CIKs
            for ticker in self.ticker_list:
                cik = self._resolve_ticker_to_cik(ticker)
                if cik and cik not in self.cik_list:
                    self.cik_list.append(cik)

    def _resolve_ticker_to_cik(self, ticker: str) -> Optional[str]:
        """Resolve ticker to CIK using EDGAR company tickers endpoint."""
        self._load_ticker_map()
        cik = self._ticker_to_cik.get(ticker.upper())
        if not cik:
            logger.warning(f"Ticker not found in EDGAR map: {ticker}")
        return cik

    def _get_company_submissions(self, cik: str) -> Optional[dict]:
        """Fetch company submissions metadata from EDGAR."""
        padded = cik.zfill(10)
        url = f"{EDGAR_SUBMISSIONS_URL}/CIK{padded}.json"
        try:
            return self._api_get(url)
        except Exception as e:
            logger.error(f"Failed to get submissions for CIK {cik}: {e}")
            return None

    def _submissions_to_source_docs(
        self,
        submissions: dict,
        since: Optional[datetime],
        until: Optional[datetime] = None,
        replay_batch: Optional[str] = None,
    ) -> list[SourceDocument]:
        """Extract SourceDocuments from company submissions JSON.

        Args:
            since:  lower-bound (exclusive).  Skip filings on or before this date.
            until:  upper-bound (inclusive).  Skip filings after this date.
                    When set the filing's actual date is always preserved as-is
                    so the document's filed_at carries through the whole pipeline.
            replay_batch: optional label ("2021-06", etc.) added to metadata.
        """
        docs = []
        company_name = submissions.get("name", "")
        cik = submissions.get("cik", "")
        ticker = ""
        tickers = submissions.get("tickers", [])
        if tickers:
            ticker = tickers[0]

        recent = submissions.get("filings", {}).get("recent", {})
        if not recent:
            return docs

        accession_nums = recent.get("accessionNumber", [])
        form_types = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        descriptions = recent.get("primaryDocument", [])

        matched = 0
        for accn, form, filed_str, primary_doc in zip(
            accession_nums, form_types, filing_dates, descriptions
        ):
            if form not in self.filing_types:
                continue

            try:
                filed_dt = datetime.strptime(filed_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            # Upper-bound filter: skip filings newer than the ceiling (keep scanning —
            # EDGAR is newest-first so older filings may still be in range).
            if until and filed_dt > until:
                continue

            # Lower-bound filter: EDGAR returns newest-first, so once we see a filing
            # older than `since` all remaining entries are also older — stop scanning.
            if since and filed_dt <= since:
                break

            # Cap on the number of *matching* filings collected per company.
            if matched >= self.max_filings_per_company:
                break
            matched += 1

            accn_clean = accn.replace("-", "")
            doc_url = f"{EDGAR_ARCHIVE_URL}/{cik}/{accn_clean}/{primary_doc}"

            meta = {
                "cik": cik,
                "accession_number": accn,
                "filed_at": filed_str,          # actual document date — always preserved
                "form_type": form,
            }
            if replay_batch:
                meta["replay_batch"] = replay_batch

            docs.append(SourceDocument(
                url=doc_url,
                title=f"{company_name} {form} {filed_str}",
                doc_type="filing",
                source_name=self.source_name,
                published_at=filed_dt,          # actual filing date, NOT replay date
                company=company_name,
                ticker=ticker,
                filing_type=form,
                metadata=meta,
            ))

        return docs

    def discover(self, since: Optional[datetime] = None) -> list[SourceDocument]:
        """Discover new SEC filings since last checkpoint."""
        all_docs: list[SourceDocument] = []

        self._build_cik_list()

        # Fetch submissions for each CIK
        for cik in self.cik_list:
            if len(all_docs) >= self.max_results:
                break
            submissions = self._get_company_submissions(cik)
            if not submissions:
                continue
            docs = self._submissions_to_source_docs(submissions, since)
            all_docs.extend(docs)
            logger.info(f"EDGAR CIK {cik}: found {len(docs)} new filings")

        logger.info(f"EDGAR total discovered: {len(all_docs)} filings")
        return all_docs[:self.max_results]

    def discover_date_range(
        self,
        start_date: datetime,
        end_date: datetime,
        replay_batch: Optional[str] = None,
    ) -> list[SourceDocument]:
        """Discover filings strictly within [start_date, end_date].

        Used by HistoricalRunner for replay mode.
        The actual filed_at of each document is preserved exactly as returned
        by EDGAR — the replay_date is only a ceiling filter, not a date override.

        Args:
            start_date:    inclusive lower bound (filings on/after this date)
            end_date:      inclusive upper bound (filings on/before this date)
            replay_batch:  label stored in metadata (e.g. "2021-06")

        Returns:
            List of SourceDocuments with their real EDGAR filed_at dates.
        """
        all_docs: list[SourceDocument] = []

        self._build_cik_list()

        # Use start_date - 1 day as exclusive lower bound for _submissions_to_source_docs
        import datetime as _dt_mod
        since = start_date - _dt_mod.timedelta(days=1)

        for cik in self.cik_list:
            if len(all_docs) >= self.max_results:
                break
            submissions = self._get_company_submissions(cik)
            if not submissions:
                continue
            docs = self._submissions_to_source_docs(
                submissions,
                since=since,
                until=end_date,
                replay_batch=replay_batch or end_date.strftime("%Y-%m"),
            )
            all_docs.extend(docs)
            if docs:
                logger.info(
                    f"Replay [{replay_batch}] CIK {cik}: "
                    f"{len(docs)} filings in [{start_date.date()}, {end_date.date()}]"
                )

        logger.info(
            f"Replay discover [{start_date.date()} → {end_date.date()}]: "
            f"{len(all_docs)} filings"
        )
        return all_docs[:self.max_results]

    def discover_by_full_text_search(
        self,
        keywords: list[str],
        since: Optional[datetime] = None,
        form_types: Optional[list[str]] = None,
    ) -> list[SourceDocument]:
        """Search EDGAR full-text index for filings mentioning specific keywords.

        Useful for theme-driven discovery (e.g., "AI infrastructure capex").
        """
        docs = []
        query = " ".join(f'"{kw}"' for kw in keywords)
        forms = ",".join(form_types or self.filing_types)

        params = {
            "q": query,
            "dateRange": "custom",
            "forms": forms,
            "_source": "file_date,entity_name,file_num,period_of_report,form_type",
            "hits.hits.total.value": 20,
        }
        if since:
            params["startdt"] = since.strftime("%Y-%m-%d")

        try:
            data = self._api_get(EDGAR_SEARCH_URL, params=params)
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits:
                src = hit.get("_source", {})
                entity = src.get("entity_name", "")
                filed_str = src.get("file_date", "")
                form = src.get("form_type", "")
                accn = hit.get("_id", "")

                try:
                    filed_dt = datetime.strptime(filed_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                doc_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum={src.get('file_num','')}&type={form}&dateb=&owner=include&count=1"

                docs.append(SourceDocument(
                    url=doc_url,
                    title=f"{entity} {form} {filed_str}",
                    doc_type="filing",
                    source_name=self.source_name,
                    published_at=filed_dt,
                    company=entity,
                    filing_type=form,
                    metadata={"accession_id": accn, "filed_at": filed_str},
                ))

        except Exception as e:
            logger.error(f"EDGAR full-text search failed: {e}")

        logger.info(f"EDGAR keyword search '{query}': {len(docs)} results")
        return docs

    def fetch_8k_items(self, cik: str, since: Optional[datetime] = None) -> list[SourceDocument]:
        """Fetch 8-K filings for event-driven analysis (earnings guidance, acquisitions)."""
        submissions = self._get_company_submissions(cik)
        if not submissions:
            return []
        original_types = self.filing_types
        self.filing_types = ["8-K"]
        docs = self._submissions_to_source_docs(submissions, since)
        self.filing_types = original_types
        return docs
