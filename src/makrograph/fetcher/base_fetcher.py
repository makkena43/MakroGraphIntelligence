"""Base fetcher interface for document acquisition."""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DocType(Enum):
    PDF = "pdf"
    HTML = "html"
    TEXT = "text"
    CSV = "csv"
    EXCEL = "excel"
    UNKNOWN = "unknown"


@dataclass
class FetchResult:
    """Result of a single document fetch operation."""
    url: str
    local_path: Optional[Path] = None
    doc_type: DocType = DocType.UNKNOWN
    status_code: int = 0
    content_length: int = 0
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status_code == 200 and self.local_path is not None


class BaseFetcher(ABC):
    """Abstract base class for all document fetchers."""

    def __init__(self, config: dict):
        self.config = config
        self.download_dir = Path(config.get("download_dir", "data/raw"))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = config.get("request_timeout_seconds", 30)
        self.retries = config.get("retry_attempts", 3)
        self.retry_delay = config.get("retry_delay_seconds", 2)
        self.rate_limit = config.get("rate_limit_per_second", 5)
        self._last_request_time = 0.0

    def _throttle(self):
        """Enforce rate limiting between requests."""
        if self.rate_limit > 0:
            min_interval = 1.0 / self.rate_limit
            elapsed = time.time() - self._last_request_time
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        self._last_request_time = time.time()

    def _get_extension(self, url: str, content_type: str = "") -> str:
        """Determine file extension from URL or content type."""
        url_lower = url.lower().split("?")[0]
        if url_lower.endswith(".pdf"):
            return ".pdf"
        elif url_lower.endswith(".csv"):
            return ".csv"
        elif url_lower.endswith((".xls", ".xlsx")):
            return ".xlsx"
        elif "pdf" in content_type:
            return ".pdf"
        elif "html" in content_type:
            return ".html"
        elif "csv" in content_type:
            return ".csv"
        return ".bin"

    def _classify_doc_type(self, extension: str) -> DocType:
        """Classify document type from extension."""
        mapping = {
            ".pdf": DocType.PDF,
            ".html": DocType.HTML,
            ".htm": DocType.HTML,
            ".txt": DocType.TEXT,
            ".csv": DocType.CSV,
            ".xls": DocType.EXCEL,
            ".xlsx": DocType.EXCEL,
        }
        return mapping.get(extension, DocType.UNKNOWN)

    @abstractmethod
    def fetch(self, url: str, filename: Optional[str] = None) -> FetchResult:
        """Fetch a single document. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def fetch_batch(self, urls: list[str]) -> list[FetchResult]:
        """Fetch multiple documents. Must be implemented by subclasses."""
        pass
