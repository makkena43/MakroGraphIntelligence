"""Tests for PDF page separators and BSE PDF-fetch selection."""

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_pages_joined_with_form_feed_and_blank_pages_kept():
    from makrograph.parser.pdf_parser import PAGE_SEPARATOR, _join_pages
    text = _join_pages(["page one", "", "page three"])
    assert text.split("\f")[2].strip() == "page three"          # numbering preserved
    assert text == PAGE_SEPARATOR.join(["page one", "", "page three"])


def test_scanned_pdf_still_yields_empty_text():
    from makrograph.parser.pdf_parser import _join_pages
    assert _join_pages(["", "  ", ""]) == ""                    # callers mark it unsupported


def test_normalizer_turns_page_separator_into_old_paragraph_break():
    from makrograph.normalizer.text_normalizer import TextNormalizer
    n = TextNormalizer({"strip_extra_whitespace": True, "fix_encoding": False,
                        "remove_headers_footers": False, "min_text_length": 1})
    assert n.normalize("end of page one.\n\f\nstart of page two.") == "end of page one.\n\nstart of page two."


def test_bse_subject_labels():
    from makrograph.fetcher.bse_fetcher import _classify_bse_subject as c
    assert c("Transcript of Earnings Conference Call") == "concall_update"
    assert c("Analysts/Institutional Investor Meet/Con. Call Updates") == "concall_update"
    assert c("Investor Presentation for Q1FY25") == "investor_presentation"
    assert c("Outcome of Board Meeting - Financial Results") == "board_decision"
    assert c("Receipt of order from a PSU") == "order_win"


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append((sql, params))

    def fetchall(self):
        return []


class _Store:
    _cursor_factory = None

    def __init__(self):
        self.log = []

    @contextmanager
    def _conn(self):
        log = self.log

        class C:
            def cursor(self, cursor_factory=None):
                return _Cur(log)
        yield C()


def _run(filing_types=None):
    from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
    p = IntelligencePipeline.__new__(IntelligencePipeline)     # skip heavy __init__
    p._pg_store = _Store()
    p.config = {"parser": {"output_dir": "/tmp/mg_parsed_test"}, "storage": {"project_root": "/tmp/mg_pdf_test"}}
    p.run_pdf_fetch_india(filing_types=filing_types)
    return p._pg_store.log[0]


def test_pdf_fetch_selects_bse_labels_only_for_bse_source():
    sql, params = _run()
    assert "source_name = 'bse_india' AND filing_type IN" in sql
    assert "Financial Result Updates" in params and "board_decision" in params and "concall_update" in params


def test_pdf_fetch_override_list_is_respected():
    sql, params = _run(filing_types=["Investor Presentation"])
    assert "bse_india" not in sql and "board_decision" not in params
