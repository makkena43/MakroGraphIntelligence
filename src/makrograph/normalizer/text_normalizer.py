"""Text normalization: encoding fixes, whitespace cleanup, header/footer removal."""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


class TextNormalizer:
    """Cleans and normalizes extracted document text."""

    def __init__(self, config: dict):
        self.strip_whitespace = config.get("strip_extra_whitespace", True)
        self.fix_encoding = config.get("fix_encoding", True)
        self.remove_headers_footers = config.get("remove_headers_footers", True)
        self.min_text_length = config.get("min_text_length", 50)

    def normalize(self, text: str) -> Optional[str]:
        """Apply all normalization steps to text."""
        if not text or not text.strip():
            return None

        result = text

        # Fix encoding issues (mojibake, garbled characters)
        if self.fix_encoding:
            result = self._fix_encoding(result)

        # Remove common PDF headers/footers
        if self.remove_headers_footers:
            result = self._remove_headers_footers(result)

        # Clean whitespace
        if self.strip_whitespace:
            result = self._normalize_whitespace(result)

        # Remove control characters (keep newlines and tabs)
        result = self._remove_control_chars(result)

        # Check minimum length
        if len(result.strip()) < self.min_text_length:
            logger.debug(f"Text too short ({len(result.strip())} chars), skipping")
            return None

        return result.strip()

    def _fix_encoding(self, text: str) -> str:
        """Fix common encoding issues."""
        try:
            import ftfy
            return ftfy.fix_text(text)
        except ImportError:
            # Manual fixes if ftfy not available
            replacements = {
                "\u2019": "'",
                "\u2018": "'",
                "\u201c": '"',
                "\u201d": '"',
                "\u2013": "-",
                "\u2014": "--",
                "\u2026": "...",
                "\u00a0": " ",
                "\ufeff": "",
                "\u200b": "",
            }
            for old, new in replacements.items():
                text = text.replace(old, new)
            return text

    def _normalize_whitespace(self, text: str) -> str:
        """Collapse excessive whitespace while preserving paragraph breaks."""
        # Replace tabs with spaces
        text = text.replace("\t", " ")
        # Collapse multiple spaces into one
        text = re.sub(r" {2,}", " ", text)
        # Collapse 3+ newlines into 2 (preserve paragraph breaks)
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Remove trailing whitespace per line
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        return text

    def _remove_headers_footers(self, text: str) -> str:
        """Remove common PDF header/footer patterns."""
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # Skip page numbers (standalone numbers)
            if re.match(r"^\d{1,4}$", stripped):
                continue
            # Skip "Page X of Y" patterns
            if re.match(r"^page\s+\d+\s*(of\s+\d+)?$", stripped, re.IGNORECASE):
                continue
            # Skip "Confidential" / "Draft" standalone markers
            if stripped.lower() in ("confidential", "draft", "private & confidential"):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)

    def _remove_control_chars(self, text: str) -> str:
        """Remove non-printable control characters, keeping newlines/tabs."""
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    def normalize_batch(self, texts: list[str]) -> list[Optional[str]]:
        """Normalize a list of texts."""
        results = []
        for text in texts:
            results.append(self.normalize(text))
        valid = sum(1 for r in results if r is not None)
        logger.info(f"Normalized {valid}/{len(texts)} texts successfully")
        return results
