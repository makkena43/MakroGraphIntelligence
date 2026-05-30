"""Base source adapter for incremental batch fetching.

Each data source (SEC, NSE, BSE, etc.) implements this interface.
The adapter knows how to:
    1. Discover new documents since a given checkpoint
    2. Download those documents
    3. Return structured FetchResults with rich metadata

Design inspired by MAK ML Trading System's resilient API client pattern:
    - Config-driven endpoints
    - Retry with exponential backoff
    - Rate limiting
    - Structured logging
"""

import logging
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base_fetcher import BaseFetcher, FetchResult

logger = logging.getLogger(__name__)


@dataclass
class SourceDocument:
    """A document discovered by a source adapter (before download)."""
    url: str
    title: str = ""
    doc_type: str = ""
    source_name: str = ""
    published_at: Optional[datetime] = None
    company: str = ""
    ticker: str = ""
    filing_type: str = ""
    metadata: dict = field(default_factory=dict)


class SourceAdapter(BaseFetcher):
    """Base class for all data source adapters.

    Subclasses must implement:
        - discover(since) → list of SourceDocuments
        - source_name property
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.user_agent = config.get(
            "user_agent", "MakroGraph/0.2 (Document Research Pipeline)"
        )
        self.session = self._create_session()

        # API-specific config
        self.api_delay = config.get("api_delay_seconds", 0.2)
        self.max_results = config.get("max_results_per_run", 500)

    def _create_session(self) -> requests.Session:
        """Create a resilient HTTP session with retry + backoff."""
        session = requests.Session()
        retry_strategy = Retry(
            total=self.retries,
            backoff_factor=self.retry_delay,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_maxsize=10)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json,application/pdf,text/html,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        return session

    def _api_get(self, url: str, params: dict = None, headers: dict = None) -> dict:
        """Make a GET request to an API endpoint with rate limiting and retries.

        Pattern from MAK ML Trading System's resilient API client.
        """
        self._throttle()

        request_headers = {}
        if headers:
            request_headers.update(headers)

        try:
            response = self.session.get(
                url, params=params, headers=request_headers, timeout=self.timeout
            )
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type:
                return response.json()
            else:
                return {"_raw": response.text, "_status": response.status_code}

        except requests.exceptions.HTTPError as e:
            logger.error(f"API error {url}: {e}")
            raise
        except requests.exceptions.Timeout:
            logger.error(f"API timeout: {url}")
            raise
        except Exception as e:
            logger.error(f"API request failed {url}: {e}")
            raise

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Unique name for this source (used as checkpoint key)."""
        pass

    @abstractmethod
    def discover(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[SourceDocument]:
        """Discover new documents within the given time window.

        Args:
            since: Only return documents published AFTER this timestamp.
                   None means use start_date from config (first run).
            until: Only return documents published ON OR BEFORE this timestamp.
                   None means fetch up to today (or config end_date if set).

        Returns:
            List of SourceDocuments to download.
        """
        pass

    def fetch(self, url: str, filename: Optional[str] = None) -> FetchResult:
        """Download a single document."""
        import hashlib
        from pathlib import Path
        from urllib.parse import urlparse

        self._throttle()
        result = FetchResult(url=url)

        try:
            response = self.session.get(url, timeout=self.timeout, stream=True)
            result.status_code = response.status_code

            if response.status_code != 200:
                result.error = f"HTTP {response.status_code}"
                return result

            content_type = response.headers.get("Content-Type", "")
            extension = self._get_extension(url, content_type)
            doc_type = self._classify_doc_type(extension)

            if filename is None:
                url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                parsed = urlparse(url)
                stem = Path(parsed.path).stem[:50]
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)
                filename = f"{self.source_name}_{safe}_{url_hash}{extension}"

            local_path = self.download_dir / filename
            content = response.content
            result.content_length = len(content)

            with open(local_path, "wb") as f:
                f.write(content)

            result.local_path = local_path
            result.doc_type = doc_type
            result.metadata = {
                "content_type": content_type,
                "source_domain": urlparse(url).netloc,
                "source_name": self.source_name,
            }

            logger.debug(f"Downloaded {url} -> {local_path.name} ({result.content_length:,} bytes)")
            return result

        except Exception as e:
            result.error = str(e)
            logger.error(f"Download failed {url}: {e}")
            return result

    def fetch_batch(self, urls: list[str]) -> list[FetchResult]:
        """Download multiple documents sequentially."""
        results = []
        total = len(urls)
        for i, url in enumerate(urls, 1):
            if i % 50 == 0:
                logger.info(f"  [{self.source_name}] Downloading [{i}/{total}]")
            result = self.fetch(url)
            results.append(result)
        success = sum(1 for r in results if r.success)
        logger.info(f"[{self.source_name}] Downloads complete: {success}/{total} ok")
        return results

    def fetch_discovered(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> tuple[list[FetchResult], list[SourceDocument]]:
        """Discover + download in one step. Returns (fetch_results, discovered_docs).

        Args:
            since: Lower bound — only documents published after this timestamp.
            until: Upper bound — only documents published on or before this timestamp.
                   Pass the UI end_date here for historical / quarterly runs.
        """
        docs = self.discover(since, until)
        if not docs:
            logger.info(f"[{self.source_name}] No new documents since {since}")
            return [], docs

        logger.info(f"[{self.source_name}] Discovered {len(docs)} new documents")
        urls = [d.url for d in docs]
        results = self.fetch_batch(urls)

        # Merge source metadata into fetch results
        for result, doc in zip(results, docs):
            result.metadata.update({
                "title": doc.title,
                "company": doc.company,
                "ticker": doc.ticker,
                "filing_type": doc.filing_type,
                "source_name": doc.source_name,
                "published_at": doc.published_at.isoformat() if doc.published_at else "",
            })
            result.metadata.update(doc.metadata)

        return results, docs

    def fetch_discovered_from_list(
        self, docs: list[SourceDocument]
    ) -> tuple[list[FetchResult], list[SourceDocument]]:
        """Download a pre-discovered list of SourceDocuments.

        Used by HistoricalRunner which calls discover_date_range() first and
        then passes the result here, avoiding a second discover() call.
        """
        if not docs:
            return [], []

        logger.info(f"[{self.source_name}] Downloading {len(docs)} pre-discovered docs")
        urls = [d.url for d in docs]
        results = self.fetch_batch(urls)

        for result, doc in zip(results, docs):
            result.metadata.update({
                "title": doc.title,
                "company": doc.company,
                "ticker": doc.ticker,
                "filing_type": doc.filing_type,
                "source_name": doc.source_name,
                "published_at": doc.published_at.isoformat() if doc.published_at else "",
            })
            result.metadata.update(doc.metadata)

        return results, docs

    def close(self):
        """Close the HTTP session."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
