"""Duplicate detection using content hashing and URL tracking."""

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class DupStatus(Enum):
    NEW = "new"
    EXACT_DUPLICATE = "exact_duplicate"
    URL_DUPLICATE = "url_duplicate"
    NEAR_DUPLICATE = "near_duplicate"


@dataclass
class DedupResult:
    """Result of duplicate check."""
    content_hash: str
    url_hash: str
    status: DupStatus
    existing_doc_id: Optional[int] = None


class Deduplicator:
    """Detects exact and near-duplicate documents."""

    def __init__(self, config: dict, storage=None):
        self.hash_algo = config.get("hash_algorithm", "xxhash")
        self.check_url = config.get("check_url", True)
        self.check_content = config.get("check_content_hash", True)
        self.storage = storage
        self._url_cache: set[str] = set()
        self._content_cache: set[str] = set()

    def _hash_content(self, text: str) -> str:
        """Generate hash of document content."""
        normalized = " ".join(text.lower().split())
        if self.hash_algo == "xxhash":
            try:
                import xxhash
                return xxhash.xxh64(normalized.encode("utf-8")).hexdigest()
            except ImportError:
                pass
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _hash_url(self, url: str) -> str:
        """Generate hash of URL."""
        clean_url = url.strip().lower().rstrip("/")
        return hashlib.md5(clean_url.encode("utf-8")).hexdigest()

    def check(self, url: str, text: str) -> DedupResult:
        """Check if a document is a duplicate."""
        url_hash = self._hash_url(url)
        content_hash = self._hash_content(text)

        # Check URL duplicate
        if self.check_url and url_hash in self._url_cache:
            logger.debug(f"URL duplicate detected: {url}")
            return DedupResult(
                content_hash=content_hash,
                url_hash=url_hash,
                status=DupStatus.URL_DUPLICATE,
            )

        # Check content duplicate
        if self.check_content and content_hash in self._content_cache:
            logger.debug(f"Content duplicate detected: {url}")
            return DedupResult(
                content_hash=content_hash,
                url_hash=url_hash,
                status=DupStatus.EXACT_DUPLICATE,
            )

        # Check against database if storage available
        if self.storage:
            existing = self.storage.find_by_hash(content_hash=content_hash, url_hash=url_hash)
            if existing:
                status = DupStatus.EXACT_DUPLICATE if existing.get("match") == "content" else DupStatus.URL_DUPLICATE
                logger.debug(f"Database duplicate ({existing.get('match')}): {url}")
                return DedupResult(
                    content_hash=content_hash,
                    url_hash=url_hash,
                    status=status,
                    existing_doc_id=existing.get("doc_id"),
                )

        # New document - add to caches
        self._url_cache.add(url_hash)
        self._content_cache.add(content_hash)

        return DedupResult(
            content_hash=content_hash,
            url_hash=url_hash,
            status=DupStatus.NEW,
        )

    def load_from_db(self):
        """Pre-load existing hashes from database into memory caches."""
        if not self.storage:
            return
        try:
            hashes = self.storage.get_all_hashes()
            self._url_cache = set(hashes.get("url_hashes", []))
            self._content_cache = set(hashes.get("content_hashes", []))
            logger.info(
                f"Loaded {len(self._url_cache)} URL hashes, "
                f"{len(self._content_cache)} content hashes from DB"
            )
        except Exception as e:
            logger.error(f"Failed to load hashes from DB: {e}")

    @property
    def cache_size(self) -> dict:
        return {
            "url_hashes": len(self._url_cache),
            "content_hashes": len(self._content_cache),
        }
