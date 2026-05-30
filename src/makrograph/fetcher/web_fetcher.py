"""HTTP-based document fetcher with retry logic and rate limiting."""

import hashlib
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base_fetcher import BaseFetcher, DocType, FetchResult

logger = logging.getLogger(__name__)


class WebFetcher(BaseFetcher):
    """Fetches documents over HTTP/HTTPS with robust error handling."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.user_agent = config.get(
            "user_agent", "MakroGraph/0.1 (Document Research Pipeline)"
        )
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create a requests session with retry strategy."""
        session = requests.Session()
        retry_strategy = Retry(
            total=self.retries,
            backoff_factor=self.retry_delay,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_maxsize=20)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/pdf,text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })
        return session

    def _generate_filename(self, url: str, extension: str) -> str:
        """Generate a unique filename from URL."""
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        parsed = urlparse(url)
        path_parts = Path(parsed.path).stem[:50]
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in path_parts)
        return f"{safe_name}_{url_hash}{extension}"

    def fetch(self, url: str, filename: Optional[str] = None) -> FetchResult:
        """Fetch a single document from URL."""
        self._throttle()
        result = FetchResult(url=url)

        try:
            response = self.session.get(url, timeout=self.timeout, stream=True)
            result.status_code = response.status_code

            if response.status_code != 200:
                result.error = f"HTTP {response.status_code}"
                logger.warning(f"Failed to fetch {url}: HTTP {response.status_code}")
                return result

            content_type = response.headers.get("Content-Type", "")
            extension = self._get_extension(url, content_type)
            doc_type = self._classify_doc_type(extension)

            if filename is None:
                filename = self._generate_filename(url, extension)

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
            }

            logger.info(
                f"Fetched {url} -> {local_path.name} "
                f"({result.content_length:,} bytes, {doc_type.value})"
            )
            return result

        except requests.exceptions.Timeout:
            result.error = "Request timed out"
            logger.error(f"Timeout fetching {url}")
        except requests.exceptions.ConnectionError as e:
            result.error = f"Connection error: {e}"
            logger.error(f"Connection error fetching {url}: {e}")
        except Exception as e:
            result.error = str(e)
            logger.error(f"Unexpected error fetching {url}: {e}")

        return result

    def fetch_batch(self, urls: list[str]) -> list[FetchResult]:
        """Fetch multiple documents sequentially with rate limiting."""
        results = []
        total = len(urls)
        for i, url in enumerate(urls, 1):
            logger.info(f"Fetching [{i}/{total}]: {url}")
            result = self.fetch(url)
            results.append(result)
        success = sum(1 for r in results if r.success)
        logger.info(f"Batch complete: {success}/{total} successful")
        return results

    def head(self, url: str) -> dict:
        """Perform HEAD request to check document metadata without downloading."""
        self._throttle()
        try:
            response = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            return {
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type", ""),
                "content_length": int(response.headers.get("Content-Length", 0)),
                "last_modified": response.headers.get("Last-Modified", ""),
            }
        except Exception as e:
            logger.error(f"HEAD request failed for {url}: {e}")
            return {"status_code": 0, "error": str(e)}

    def close(self):
        """Close the underlying session."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
