"""Async document fetcher for high-throughput acquisition (1000+ docs/day)."""

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import aiofiles

from .base_fetcher import BaseFetcher, DocType, FetchResult

logger = logging.getLogger(__name__)


class AsyncFetcher(BaseFetcher):
    """Async HTTP fetcher for parallel document downloads."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.max_concurrent = config.get("max_concurrent_requests", 10)
        self.user_agent = config.get(
            "user_agent", "MakroGraph/0.1 (Document Research Pipeline)"
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    def _generate_filename(self, url: str, extension: str) -> str:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        parsed = urlparse(url)
        path_parts = Path(parsed.path).stem[:50]
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in path_parts)
        return f"{safe_name}_{url_hash}{extension}"

    async def _fetch_one(
        self, session: aiohttp.ClientSession, url: str, filename: Optional[str] = None
    ) -> FetchResult:
        """Fetch a single document with concurrency control."""
        result = FetchResult(url=url)
        async with self._semaphore:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                    result.status_code = response.status

                    if response.status != 200:
                        result.error = f"HTTP {response.status}"
                        logger.warning(f"Failed: {url} -> HTTP {response.status}")
                        return result

                    content_type = response.headers.get("Content-Type", "")
                    extension = self._get_extension(url, content_type)
                    doc_type = self._classify_doc_type(extension)

                    if filename is None:
                        filename = self._generate_filename(url, extension)

                    local_path = self.download_dir / filename
                    content = await response.read()
                    result.content_length = len(content)

                    async with aiofiles.open(local_path, "wb") as f:
                        await f.write(content)

                    result.local_path = local_path
                    result.doc_type = doc_type
                    result.metadata = {
                        "content_type": content_type,
                        "source_domain": urlparse(url).netloc,
                    }

                    logger.info(
                        f"Fetched {url} -> {local_path.name} "
                        f"({result.content_length:,} bytes)"
                    )
                    return result

            except asyncio.TimeoutError:
                result.error = "Request timed out"
                logger.error(f"Timeout: {url}")
            except aiohttp.ClientError as e:
                result.error = f"Client error: {e}"
                logger.error(f"Client error: {url}: {e}")
            except Exception as e:
                result.error = str(e)
                logger.error(f"Error: {url}: {e}")

            return result

    async def _fetch_batch_async(self, urls: list[str]) -> list[FetchResult]:
        """Internal async batch fetch."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/pdf,text/html,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        connector = aiohttp.TCPConnector(limit=self.max_concurrent, ssl=False)
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            tasks = [self._fetch_one(session, url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        final = []
        for url, r in zip(urls, results):
            if isinstance(r, Exception):
                final.append(FetchResult(url=url, error=str(r)))
            else:
                final.append(r)
        return final

    def fetch(self, url: str, filename: Optional[str] = None) -> FetchResult:
        """Synchronous single-document fetch (wraps async)."""
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(self._fetch_batch_async([url]))
            return results[0]
        finally:
            loop.close()

    def fetch_batch(self, urls: list[str]) -> list[FetchResult]:
        """Fetch multiple documents concurrently."""
        logger.info(f"Starting async batch fetch of {len(urls)} URLs "
                    f"(max {self.max_concurrent} concurrent)")
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(self._fetch_batch_async(urls))
        finally:
            loop.close()

        success = sum(1 for r in results if r.success)
        logger.info(f"Async batch complete: {success}/{len(urls)} successful")
        return results
