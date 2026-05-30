"""Smoke tests for MakroGraph Intelligence — incremental batch pipeline."""

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_text_normalizer():
    """Test text normalization."""
    from makrograph.normalizer.text_normalizer import TextNormalizer

    normalizer = TextNormalizer({
        "strip_extra_whitespace": True,
        "fix_encoding": True,
        "remove_headers_footers": True,
        "min_text_length": 10,
    })

    # Basic normalization
    result = normalizer.normalize("  Hello   World  \n\n\n\nThis is a test document.  ")
    assert result is not None
    assert "Hello World" in result
    assert "\n\n\n\n" not in result

    # Header/footer removal
    result = normalizer.normalize("Page 1 of 10\nActual content goes here with enough length.\n42")
    assert result is not None
    assert "Page 1 of 10" not in result
    assert "Actual content" in result

    # Too short
    result = normalizer.normalize("Hi")
    assert result is None

    print("  text_normalizer: PASS")


def test_deduplicator():
    """Test duplicate detection."""
    from makrograph.dedup.deduplicator import Deduplicator, DupStatus

    dedup = Deduplicator({"hash_algorithm": "sha256", "check_url": True, "check_content_hash": True})

    # First document is new
    r1 = dedup.check("https://example.com/doc1.pdf", "This is document one content.")
    assert r1.status == DupStatus.NEW

    # Same URL = duplicate
    r2 = dedup.check("https://example.com/doc1.pdf", "Different text but same URL.")
    assert r2.status == DupStatus.URL_DUPLICATE

    # Same content, different URL = duplicate
    r3 = dedup.check("https://other.com/doc2.pdf", "This is document one content.")
    assert r3.status == DupStatus.EXACT_DUPLICATE

    # Truly new document
    r4 = dedup.check("https://other.com/doc3.pdf", "Completely different content here.")
    assert r4.status == DupStatus.NEW

    print("  deduplicator: PASS")


def test_document_store():
    """Test SQLite storage and search."""
    from makrograph.storage.db_store import DocumentStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        store = DocumentStore({"db_path": db_path, "enable_fts": True})

        # Insert
        doc_id = store.insert_document(
            url="https://example.com/report.pdf",
            url_hash="abc123",
            content_hash="def456",
            raw_text="Raw quarterly earnings report for Q3 2024.",
            normalized_text="Quarterly earnings report for Q3 2024.",
            title="Q3 2024 Earnings Report",
            source_domain="example.com",
            doc_type="pdf",
            page_count=5,
            file_size=1024,
        )
        assert doc_id is not None

        # Duplicate insert returns None
        dup_id = store.insert_document(
            url="https://example.com/report2.pdf",
            url_hash="xyz789",
            content_hash="def456",
            raw_text="Same content hash.",
            normalized_text="Same content hash.",
        )
        assert dup_id is None

        # Search
        results = store.search("earnings report")
        assert len(results) >= 1
        assert results[0]["title"] == "Q3 2024 Earnings Report"

        # Stats
        stats = store.get_stats()
        assert stats["total_documents"] == 1

        # Hash lookup
        found = store.find_by_hash(content_hash="def456")
        assert found is not None
        assert found["match"] == "content"

        store.close()
        print("  document_store: PASS")


def test_checkpoint_manager():
    """Test incremental batch checkpoint tracking."""
    from makrograph.pipeline.checkpoint import CheckpointManager

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_cp.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cm = CheckpointManager(conn)

        # No checkpoint for new source
        assert cm.get_checkpoint("concalls") is None

        # Start a run
        run_id = cm.start_run("concalls")
        assert run_id is not None

        # Finish run and update checkpoint
        now = datetime.now(timezone.utc)
        stats = {"fetched": 50, "new": 45, "duplicate": 5, "failed": 0}
        cm.finish_run(run_id, "concalls", stats, now)

        # Checkpoint now exists
        cp = cm.get_checkpoint("concalls")
        assert cp is not None

        # All checkpoints
        all_cp = cm.get_all_checkpoints()
        assert len(all_cp) == 1
        assert all_cp[0]["source_name"] == "concalls"
        assert all_cp[0]["total_docs_fetched"] == 50

        # Second run — checkpoint should update
        run_id2 = cm.start_run("concalls")
        now2 = datetime.now(timezone.utc)
        stats2 = {"fetched": 30, "new": 25, "duplicate": 5, "failed": 0}
        cm.finish_run(run_id2, "concalls", stats2, now2)

        all_cp = cm.get_all_checkpoints()
        assert all_cp[0]["total_docs_fetched"] == 80  # 50 + 30
        assert all_cp[0]["total_runs"] == 2

        # Run history
        history = cm.get_run_history("concalls")
        assert len(history) == 2

        # Reset
        cm.reset_checkpoint("concalls")
        assert cm.get_checkpoint("concalls") is None

        conn.close()
        print("  checkpoint_manager: PASS")


def test_web_fetcher_init():
    """Test WebFetcher initialization (no network calls)."""
    from makrograph.fetcher.web_fetcher import WebFetcher

    fetcher = WebFetcher({
        "request_timeout_seconds": 10,
        "retry_attempts": 2,
        "rate_limit_per_second": 5,
        "download_dir": tempfile.mkdtemp(),
    })
    assert fetcher.timeout == 10
    assert fetcher.retries == 2
    fetcher.close()
    print("  web_fetcher_init: PASS")


def test_pdf_parser_missing_file():
    """Test PDF parser handles missing files gracefully."""
    from makrograph.parser.pdf_parser import PDFParser

    parser = PDFParser({"output_dir": tempfile.mkdtemp()})
    result = parser.parse(Path("/nonexistent/file.pdf"))
    assert not result.success
    assert "not found" in result.error.lower()
    print("  pdf_parser_missing_file: PASS")


if __name__ == "__main__":
    print("Running MakroGraph Intelligence smoke tests...\n")
    test_text_normalizer()
    test_deduplicator()
    test_document_store()
    test_checkpoint_manager()
    test_web_fetcher_init()
    test_pdf_parser_missing_file()
    print("\nAll tests PASSED.")
