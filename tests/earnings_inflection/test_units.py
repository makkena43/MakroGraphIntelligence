"""Unit tests for extraction, validation, dedup, series, ledger, budget, guards."""

import re
from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.contracts import (
    IST, CommitmentStrength, ContractError, DocumentKind, Evidence, EvidenceTier, FinancialMeasurement,
    EvidenceStatus, GuidanceOutcome, IssuerModel, Metric, Modality, Quantity, Scope, SourceDocument, Unit,
    assert_no_action_language, find_action_language,
)


def doc(text, **kw):
    d = SourceDocument(doc_id=kw.pop("doc_id", "D1"), source_name="t", ticker=kw.pop("ticker", "T"), text=text, **kw)
    return d


# ---------------- classification / provenance ----------------

def test_invitation_is_not_transcript_even_with_transcript_title():
    from makrograph.earnings_inflection.document_versions import classify_document
    d = doc("XYZ Ltd invites you to its Q4 FY24 earnings conference call. Universal access number: +91 22 000.",
            title="Earnings call transcript")
    kind, basis = classify_document(d)
    assert kind == DocumentKind.EARNINGS_CALL_INVITATION
    assert basis.startswith("content")


def test_transcript_needs_dialogue():
    from makrograph.earnings_inflection.document_versions import classify_document
    t = "\n".join(["Moderator: Ladies and gentlemen, welcome.", "Asha Iyer: Thanks.",
                   "Moderator: The first question is from Dev.", "Dev Shah: Margins?", "Asha Iyer: Stable.",
                   "Moderator: over to you."])
    assert classify_document(doc(t))[0] == DocumentKind.EARNINGS_CALL_TRANSCRIPT


def test_title_only_classification_is_labelled():
    from makrograph.earnings_inflection.document_versions import classify_document
    kind, basis = classify_document(doc("", title="Annual Report 2023-24"))
    assert kind == DocumentKind.ANNUAL_REPORT and basis == "title_only"


def test_filed_at_only_is_end_of_day_ist_and_unknown_is_none():
    from makrograph.earnings_inflection.document_versions import availability
    ts, basis = availability(doc("x", filed_at=date(2024, 8, 12)))
    assert ts == datetime(2024, 8, 12, 23, 59, 59, tzinfo=IST) and "conservative" in basis
    assert availability(doc("x"))[0] is None


def test_restatement_lineage_links_versions_without_overwriting():
    from makrograph.earnings_inflection.document_versions import link_versions
    base = "Statement of Consolidated Unaudited Financial Results for the quarter ended 30 June 2024\n"
    a = doc(base + "Revenue 100", doc_id="A", published_at=datetime(2024, 8, 1, tzinfo=IST))
    b = doc(base + "Revenue 101 (restated)", doc_id="B", published_at=datetime(2024, 8, 20, tzinfo=IST))
    link_versions([a, b])
    assert a.superseded_by == "B" and b.supersedes == ["A"]


# ---------------- extraction ----------------

def test_amount_units_normalised_to_crore():
    from makrograph.earnings_inflection.extraction import parse_inr
    assert parse_inr("order of Rs 4,500 lakh").value == 45.0
    assert parse_inr("₹1.2 billion contract").value == 120.0
    assert parse_inr("INR 250 million").value == 25.0
    assert parse_inr("Rs. 75 crore").unit == Unit.INR_CRORE


def test_negation_is_scoped():
    from makrograph.earnings_inflection.extraction import classify_modality
    assert classify_modality("We do not expect revenue growth in FY26.") == Modality.NEGATED
    assert classify_modality("The order has not been received.") == Modality.NEGATED
    assert classify_modality("Revenue grew 40%, with no single customer above 10%.") == Modality.REALIZED
    assert classify_modality("We expect revenue growth of 20% in FY26.") == Modality.FORWARD
    assert classify_modality("Revenue could grow 20% if approvals come.") == Modality.CONDITIONAL


def test_order_evidence_tiers_and_strength():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import extract_sentence_evidence
    d = doc("The company received a purchase order worth Rs 300 crore from Alpha Power Limited. "
            "The company has been declared L1 bidder for a Rs 500 crore tender. "
            "We signed an MoU worth Rs 1,000 crore with Beta Corp for future supplies.",
            published_at=datetime(2024, 1, 1, tzinfo=IST))
    d.available_at = d.published_at
    evs = [e for e in extract_sentence_evidence(d, chunk_document(d)) if e.metric == Metric.ORDER_WIN]
    by_amt = {e.quantity.value: e for e in evs}
    assert by_amt[300.0].commitment_strength == CommitmentStrength.BINDING
    assert by_amt[300.0].tier == EvidenceTier.COMMERCIAL_COMMITMENT
    assert by_amt[300.0].counterparty == "Alpha Power Limited" and by_amt[300.0].counterparty_named
    assert by_amt[500.0].commitment_strength == CommitmentStrength.PROVISIONAL
    assert by_amt[1000.0].commitment_strength == CommitmentStrength.NON_BINDING
    assert by_amt[1000.0].tier == EvidenceTier.MANAGEMENT_ASSERTION


def test_guidance_does_not_require_large_number_and_negated_guidance_not_a_commitment():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import extract_sentence_evidence
    d = doc("We expect revenue growth of 8% in FY26. We do not expect revenue growth of 30% in FY26.",
            published_at=datetime(2025, 5, 1, tzinfo=IST))
    d.available_at = d.published_at
    evs = extract_sentence_evidence(d, chunk_document(d))
    g = [e for e in evs if e.metric == Metric.REVENUE_GROWTH_GUIDANCE]
    assert {e.modality for e in g} == {Modality.FORWARD, Modality.NEGATED}
    assert all(e.target_period_label == "FY26" for e in g)


def test_results_table_parser_lakh_scale_and_columns():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    text = "\n".join([
        "Statement of Standalone Unaudited Financial Results for the quarter ended 30 June 2024",
        "(Rs. in lakhs)",
        "Particulars                 Quarter ended                 Year ended",
        "                 30.06.2024   31.03.2024   30.06.2023   31.03.2024",
        "1. Revenue from operations   15,000.00   12,000.00   10,000.00   45,000.00",
        "   (b) Diluted                    2.50        2.00        1.50        7.00",
    ])
    d = doc(text, published_at=datetime(2024, 8, 1, tzinfo=IST))
    d.available_at = d.published_at
    rows, issues = parse_results_tables(d, chunk_document(d))
    rev = {(r.period_end, r.period_type): r.value for r in rows if r.metric == Metric.REVENUE}
    assert rev[(date(2024, 6, 30), "Q")] == 150.0
    assert rev[(date(2024, 3, 31), "FY")] == 450.0
    assert all(r.scope == Scope.STANDALONE for r in rows)
    eps = [r for r in rows if r.metric == Metric.DILUTED_EPS and r.period_end == date(2024, 6, 30)]
    assert eps[0].value == 2.5 and eps[0].unit == Unit.INR_PER_SHARE


def test_table_without_unit_line_is_not_guessed():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    text = ("Statement of Unaudited Financial Results for the quarter ended 30 June 2024\n"
            "Particulars   Quarter ended\n          30.06.2024   31.03.2024   30.06.2023\n"
            "Revenue from operations   150   120   100\n")
    d = doc(text)
    rows, issues = parse_results_tables(d, chunk_document(d))
    assert not [r for r in rows if r.metric == Metric.REVENUE]
    assert any("unit line" in i for i in issues)


# ---------------- chunking ----------------

def test_table_chunk_keeps_header_and_unit_and_nothing_dropped():
    from makrograph.earnings_inflection.chunking import chunk_document
    long_prose = " ".join(f"Sentence number {i} about the original promise." for i in range(200))
    text = ("(Rs. in crore)\nParticulars   Quarter ended\n   30.06.2024   31.03.2024\n"
            "Revenue from operations   150.0   120.0\n\f" + long_prose)
    d = doc(text)
    chunks = chunk_document(d, max_chars=500)
    tables = [c for c in chunks if c.kind == "table"]
    assert tables and "(Rs. in crore)" in tables[0].header and "Particulars" in tables[0].header
    joined = " ".join(c.text for c in chunks if c.kind == "prose")
    assert "Sentence number 0 " in joined and "Sentence number 199 " in joined
    assert {c.page for c in chunks} == {1, 2}


def test_retrieval_is_oldest_first_and_reports_omissions():
    from makrograph.earnings_inflection.chunking import chunk_document, retrieve
    old = doc("We promise 30% growth in FY25.", doc_id="old")
    new = doc("We now see 10% growth in FY25.", doc_id="new")
    r = retrieve([(old, chunk_document(old)), (new, chunk_document(new))], re.compile("growth"), char_budget=35)
    assert r.selected[0].doc_id == "old"
    assert [c.doc_id for c in r.omitted] == ["new"] and not r.complete


# ---------------- validation ----------------

def _ev(doc_id="D1", quote="x", metric=Metric.ORDER_WIN, q=None, when=datetime(2024, 1, 1, tzinfo=IST), **kw):
    return Evidence(evidence_id=kw.pop("eid", quote[:8] + str(q.value if q else "")), doc_id=doc_id, ticker="T",
                    metric=metric, tier=kw.pop("tier", EvidenceTier.COMMERCIAL_COMMITMENT),
                    modality=kw.pop("modality", Modality.REALIZED), quote=quote, available_at=when, quantity=q, **kw)


def test_quote_must_be_verbatim_and_lookahead_rejected():
    from makrograph.earnings_inflection.validation import validate_evidence
    d = doc("Received order worth Rs 10 crore.", published_at=datetime(2024, 1, 1, tzinfo=IST))
    good = _ev(quote="Received order worth Rs 10 crore.")
    fake = _ev(quote="Received order worth Rs 99 crore.")
    future = _ev(quote="Received order worth Rs 10 crore.", when=datetime(2025, 1, 1, tzinfo=IST), eid="f")
    validate_evidence([good, fake, future], {"D1": d}, datetime(2024, 6, 1, tzinfo=IST))
    assert good.usable and not fake.usable and not future.usable


def test_percent_unit_check():
    from makrograph.earnings_inflection.validation import check_units
    e = _ev(metric=Metric.EBITDA_MARGIN, q=Quantity(150.0, Unit.INR_CRORE))
    check_units(e)
    assert not e.usable


# ---------------- event resolution ----------------

def test_same_order_on_different_dates_is_one_event_repeat_is_two():
    from makrograph.earnings_inflection.event_resolution import resolve_events
    q = Quantity(450.0, Unit.INR_CRORE)
    a = _ev("A", "order 450 from X", q=q, counterparty="Northern Grid Corporation Limited", counterparty_named=True,
            commitment_strength=CommitmentStrength.BINDING, eid="a")
    b = _ev("B", "we got 450 order", q=Quantity(449.0, Unit.INR_CRORE), when=datetime(2024, 3, 1, tzinfo=IST),
            counterparty="Northern Grid Corp Ltd", counterparty_named=True, eid="b")
    c = _ev("C", "repeat order 450", q=q, when=datetime(2024, 6, 1, tzinfo=IST), distinct_marker=True,
            counterparty="Northern Grid Corporation Limited", counterparty_named=True, eid="c")
    d = _ev("D", "repeat order 450 again mentioned", q=q, when=datetime(2024, 8, 1, tzinfo=IST),
            distinct_marker=True, counterparty="Northern Grid Corporation Limited", counterparty_named=True, eid="d")
    events = resolve_events([a, b, c, d])
    assert len(events) == 2
    assert sorted(len(e.doc_ids) for e in events) == [2, 2]


def test_different_named_counterparties_not_merged():
    from makrograph.earnings_inflection.event_resolution import resolve_events
    q = Quantity(100.0, Unit.INR_CRORE)
    events = resolve_events([_ev("A", "a", q=q, counterparty="Alpha Ltd", counterparty_named=True, eid="1"),
                             _ev("B", "b", q=q, counterparty="Gamma Ltd", counterparty_named=True, eid="2")])
    assert len(events) == 2


# ---------------- financial series ----------------

def _fm(metric, end, v, scope=Scope.CONSOLIDATED, ptype="Q", doc_id="R", when=datetime(2024, 9, 1, tzinfo=IST)):
    unit = Unit.INR_PER_SHARE if metric == Metric.DILUTED_EPS else Unit.INR_CRORE
    return FinancialMeasurement("T", metric, end, ptype, v, unit, scope, doc_id, when)


QE = [date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31), date(2024, 6, 30)]


def test_ttm_is_sum_not_annualised_and_missing_quarter_blocks():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(Metric.REVENUE, d, v) for d, v in zip(QE, [100, 110, 120, 130, 200])]
    s = FinancialSeries.build("T", rows)
    v, miss = s.ttm(Metric.REVENUE, date(2024, 6, 30))
    assert v == 110 + 120 + 130 + 200 and v != 200 * 4 and not miss
    s2 = FinancialSeries.build("T", [r for r in rows if r.period_end != date(2023, 12, 31)])
    v2, miss2 = s2.ttm(Metric.REVENUE, date(2024, 6, 30))
    assert v2 is None and any("2023-12-31" in m for m in miss2)


def test_scopes_never_mixed_and_restatement_uses_latest_version():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(Metric.REVENUE, QE[0], 100), _fm(Metric.REVENUE, QE[4], 999, scope=Scope.STANDALONE),
            _fm(Metric.REVENUE, QE[4], 150, doc_id="v1"),
            _fm(Metric.REVENUE, QE[4], 155, doc_id="v2", when=datetime(2024, 10, 1, tzinfo=IST))]
    s = FinancialSeries.build("T", rows)
    assert s.scope == Scope.CONSOLIDATED
    p = s.get(Metric.REVENUE, QE[4])
    assert p.value == 155 and p.versions == 2 and s.lineage_notes


def test_fundamentals_snapshot_rejected_for_history():
    from makrograph.earnings_inflection.financial_series import PointInTimeViolation, assert_point_in_time_source
    with pytest.raises(PointInTimeViolation):
        assert_point_in_time_source("fundamentals_snapshot")
    assert_point_in_time_source("fundamentals_snapshot", dated_version_proven=True)


# ---------------- guidance ledger ----------------

def test_ledger_keeps_original_and_judges_against_it():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    g1 = _ev("G1", "orig", metric=Metric.REVENUE_GROWTH_GUIDANCE, q=Quantity(30.0, Unit.PERCENT, raw="30%"),
             tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=Modality.FORWARD, target_period_label="FY24",
             when=datetime(2023, 5, 1, tzinfo=IST), eid="g1")
    g2 = _ev("G2", "cut", metric=Metric.REVENUE_GROWTH_GUIDANCE, q=Quantity(10.0, Unit.PERCENT, raw="10%"),
             tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=Modality.FORWARD, target_period_label="FY24",
             when=datetime(2023, 11, 1, tzinfo=IST), eid="g2")
    rows = [_fm(Metric.REVENUE, date(2023, 3, 31), 1000, ptype="FY"),
            _fm(Metric.REVENUE, date(2024, 3, 31), 1120, ptype="FY")]
    led = build_ledger([g1, g2], FinancialSeries.build("T", rows), datetime(2024, 7, 1, tzinfo=IST))
    assert len(led) == 1
    r = led[0]
    assert r.original.quantity.value == 30.0 and r.revisions[0].direction.value == "lowered"
    assert r.outcome == GuidanceOutcome.MISSED and r.realized_value == 12.0
    # revised to 10% and delivered 12%: judged against latest too, not a contradiction by itself
    assert r.latest_outcome == GuidanceOutcome.MET
    assert any("delivered the revised guidance" in f for f in r.flags)
    assert r.revisions[0].explained is None          # no source text supplied


def test_ledger_pending_before_results_due():
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    g = _ev("G", "x", metric=Metric.MARGIN_GUIDANCE, q=Quantity(15.0, Unit.PERCENT),
            tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=Modality.FORWARD, target_period_label="FY24",
            when=datetime(2023, 5, 1, tzinfo=IST))
    led = build_ledger([g], None, datetime(2024, 4, 15, tzinfo=IST))
    assert led[0].outcome == GuidanceOutcome.PENDING


# ---------------- identity ----------------

def test_identity_dated_symbols_and_series():
    from makrograph.earnings_inflection.identity import (
        IdentityResolver, SymbolSpan, classify_issuer_model, is_operating_equity_series, same_counterparty,
    )
    r = IdentityResolver([SymbolSpan("OLD", "NSE", "I1", date(2015, 1, 1), date(2021, 6, 30)),
                          SymbolSpan("NEW", "NSE", "I1", date(2021, 7, 1))])
    assert r.resolve("OLD", date(2020, 1, 1)) == ("I1", "dated_symbol_history")
    assert r.resolve("OLD", date(2022, 1, 1))[1] == "symbol_assumed_stable"
    assert is_operating_equity_series("W1") is False and is_operating_equity_series(None) is None
    assert is_operating_equity_series("SM") is True
    assert classify_issuer_model("Foo Bank Ltd")[0] == IssuerModel.BANK
    assert classify_issuer_model("Zeta Capital Engineering")[0] == IssuerModel.UNKNOWN
    assert classify_issuer_model("X", industry="Housing Finance")[0] == IssuerModel.NBFC
    assert same_counterparty("NTPC Limited", "National Thermal Power Corporation") is True
    assert same_counterparty("", "Alpha") is None


# ---------------- budget / LLM ----------------

def test_budget_disabled_by_default_and_hard_limits():
    from makrograph.earnings_inflection.budget import Budget, BudgetDisabled, BudgetExceeded
    with pytest.raises(BudgetDisabled):
        Budget.from_config({}).reserve(10)
    b = Budget(enabled=True, max_calls=2, max_tokens=1000, max_spend_usd=0.01, usd_per_1k_tokens=0.01)
    b.reserve(400)
    with pytest.raises(BudgetExceeded):
        b.reserve(700)            # tokens
    b.reserve(500)
    with pytest.raises(BudgetExceeded):
        b.reserve(1)              # calls


def test_llm_extractor_disabled_and_rejects_non_verbatim():
    from makrograph.earnings_inflection.budget import Budget
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import ConstrainedLLMExtractor, LLMDisabled
    d = doc("Revenue grew to Rs 200 crore in Q1FY25.")
    with pytest.raises(LLMDisabled):
        ConstrainedLLMExtractor(lambda p: "[]", Budget()).extract(d, chunk_document(d))
    resp = ('[{"metric":"revenue","quote":"Revenue grew to Rs 200 crore in Q1FY25.","modality":"realized",'
            '"value":200,"unit":"INR_crore"},{"metric":"revenue","quote":"Revenue tripled.","modality":"realized"},'
            '{"metric":"revenue","quote":"Revenue grew to Rs 200 crore in Q1FY25.","value":900,"unit":"INR_crore"}]')
    b = Budget(enabled=True, max_calls=5, max_tokens=100000, max_spend_usd=1, usd_per_1k_tokens=0.001)
    ev, rej = ConstrainedLLMExtractor(lambda p: resp, b, enabled=True).extract(d, chunk_document(d))
    assert len(ev) == 1 and ev[0].extractor == "llm" and len(rej) == 2
    ConstrainedLLMExtractor(lambda p: resp, b, enabled=True).extract(d, chunk_document(d))
    assert b.calls == 1                                   # second run served from cache


# ---------------- guards ----------------

def test_action_language_guard():
    assert find_action_language({"x": "Rating: BUY"})
    assert find_action_language({"x": "suggested position size 2%"})
    assert not find_action_language({"quote": "the board approved a BUY-back"})
    with pytest.raises(ContractError):
        assert_no_action_language({"note": "ACCUMULATE on dips"})


def test_read_only_sql_guard():
    from makrograph.earnings_inflection.source_repository import ReadOnlyViolation, assert_read_only_sql
    assert_read_only_sql("SELECT id FROM mg_documents WHERE title = 'update; drop'")
    for bad in ("UPDATE mg_documents SET x=1", "SELECT 1; DROP TABLE x", "WITH a AS (DELETE FROM t RETURNING *) SELECT 1",
                "ALTER TABLE mg_documents ADD COLUMN x int"):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only_sql(bad)


def test_postgres_repo_requires_explicit_dsn(monkeypatch):
    from makrograph.earnings_inflection.source_repository import PostgresReadOnlyRepository
    monkeypatch.delenv("EI_READONLY_DSN", raising=False)
    with pytest.raises(RuntimeError):
        PostgresReadOnlyRepository()


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [("x",)]
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.conn.log.append(sql)
        if sql.startswith("SHOW"):
            self._rows = [(self.conn.ro,)]
        elif "information_schema.columns" in sql:
            self.description = [("column_name",)]
            self._rows = [(c,) for c in ("id", "ticker", "country", "filed_at", "published_at", "title")]
        elif "FROM mg_documents WHERE" in sql:
            names = [c.split(" AS ")[-1].strip() for c in sql.split("SELECT ")[1].split(" FROM")[0].split(", ")]
            self.description = [(n,) for n in names]
            last_id = params[-2]
            data = [r for r in self.conn.docs if r["id"] > last_id][: params[-1]]
            self._rows = [tuple(r.get(n) for n in names) for r in data]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, ro="on", docs=()):
        self.ro, self.docs, self.log = ro, list(docs), []

    def set_session(self, **kw):
        pass

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        pass

    def close(self):
        pass


def test_postgres_repo_pages_without_global_limit_and_handles_missing_raw_text():
    from makrograph.earnings_inflection.source_repository import PostgresReadOnlyRepository
    docs = [{"id": i, "ticker": "ZZZ", "country": "IN", "filed_at": date(2024, 1, 1), "title": f"t{i}"} for i in range(1, 8)]
    conn = _FakeConn(docs=docs)
    repo = PostgresReadOnlyRepository(dsn="postgresql://ro@h/db", page_size=3, connect=lambda *a, **k: conn)
    out = repo.documents("ZZZ", "IN", datetime(2024, 6, 1, tzinfo=IST))
    assert [d.doc_id for d in out] == [str(i) for i in range(1, 8)]
    assert any("NULL AS raw_text" in s for s in conn.log)
    assert not any(re.search(r"ORDER BY ticker.*LIMIT", s) for s in conn.log)


def test_postgres_repo_refuses_writable_session():
    from makrograph.earnings_inflection.source_repository import PostgresReadOnlyRepository, ReadOnlyViolation
    repo = PostgresReadOnlyRepository(dsn="x", connect=lambda *a, **k: _FakeConn(ro="off"))
    with pytest.raises(ReadOnlyViolation):
        repo.tickers("IN")


# ---------------- pdfplumber-style text (single-space columns, \f pages) ----------------

def test_single_space_rows_are_tabular_but_sentences_are_not():
    from makrograph.earnings_inflection.chunking import _is_tabular, split_numeric_row
    assert _is_tabular("1. Revenue from operations 1,650.00 1,600.00 1,150.00 5,350.00")
    assert _is_tabular("31.03.2024 31.12.2023 31.03.2023 31.03.2024")
    assert _is_tabular("Exceptional items - - (12.50) -")
    assert not _is_tabular("Our order book stands at Rs 1,400 crore as of March 31, 2024.")
    assert not _is_tabular("Revenue grew 40% to Rs 165 crore in FY 2024")
    assert split_numeric_row("(b) Diluted 3.66 2.10 (0.40)") == ("(b) Diluted", ["3.66", "2.10", "(0.40)"])


def test_pdfplumber_style_statement_parses_with_pages_and_dash_cells():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    page1 = "\n".join([
        "Statement of Consolidated Unaudited Financial Results for the quarter ended 30 June 2024",
        "(Rs. in lakhs)",
        "Particulars Quarter ended Year ended",
        "30.06.2024 31.03.2024 30.06.2023 31.03.2024",
        "Unaudited Audited Unaudited Audited",
        "1. Revenue from operations 15,000.00 12,000.00 10,000.00 45,000.00",
        "Exceptional items - - (50.00) (50.00)",
        "(b) Diluted 2.50 2.00 1.50 7.00",
    ])
    page2 = "Notes:\n1. Results reviewed by the Audit Committee."
    d = doc(page1 + "\n\f\n" + page2, published_at=datetime(2024, 8, 1, tzinfo=IST))
    d.available_at = d.published_at
    chunks = chunk_document(d)
    assert {c.page for c in chunks} == {1, 2}
    rows, issues = parse_results_tables(d, chunks)
    rev = {(r.period_end, r.period_type): r.value for r in rows if r.metric == Metric.REVENUE}
    assert rev == {(date(2024, 6, 30), "Q"): 150.0, (date(2024, 3, 31), "Q"): 120.0,
                   (date(2023, 6, 30), "Q"): 100.0, (date(2024, 3, 31), "FY"): 450.0}
    exc = {r.period_end: r.value for r in rows if r.metric == Metric.EXCEPTIONAL_ITEMS}
    assert exc == {date(2023, 6, 30): -0.5} or set(exc) == {date(2023, 6, 30), date(2024, 3, 31)}
    assert not any(r.metric == Metric.EXCEPTIONAL_ITEMS and r.period_end == date(2024, 6, 30) for r in rows)


# ---------------- revision handling ----------------

def _gev(doc_id, value, when, label="FY25", eid=None, modality=Modality.FORWARD, quote=None):
    return _ev(doc_id, quote or f"We expect revenue growth of {value:g}% in {label}.",
               metric=Metric.REVENUE_GROWTH_GUIDANCE, q=Quantity(value, Unit.PERCENT, raw=f"{value:g}%"),
               tier=EvidenceTier.MANAGEMENT_ASSERTION, modality=modality, target_period_label=label,
               when=when, eid=eid or f"{doc_id}{value}")


def test_revision_with_stated_reason_is_quoted_for_review():
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    g1 = _gev("A", 25, datetime(2024, 5, 1, tzinfo=IST))
    rev_q = "We now expect revenue growth of 15% in FY25."
    d_rev = doc(rev_q + " This is due to a delay in customer site readiness for two large projects.", doc_id="B")
    g2 = _gev("B", 15, datetime(2024, 11, 1, tzinfo=IST), quote=rev_q)
    led = build_ledger([g1, g2], None, datetime(2024, 12, 1, tzinfo=IST), {"B": d_rev})
    r = led[0].revisions[0]
    assert r.direction.value == "lowered" and r.explained is True
    assert "delay in customer site readiness" in r.explanation
    assert any("stated reason" in f and "human review" in f for f in led[0].flags)


def test_revision_without_reason_detected():
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    g1 = _gev("A", 25, datetime(2024, 5, 1, tzinfo=IST))
    rev_q = "We now expect revenue growth of 15% in FY25."
    g2 = _gev("B", 15, datetime(2024, 11, 1, tzinfo=IST), quote=rev_q)
    led = build_ledger([g1, g2], None, datetime(2024, 12, 1, tzinfo=IST),
                       {"B": doc(rev_q + " Thank you.", doc_id="B")})
    assert led[0].revisions[0].explained is False


def _status(guidance):
    from makrograph.earnings_inflection.assessments import decide_status
    return decide_status([], [], [], guidance, usable_docs=1, ttm_revenue=None)[0]


def test_single_or_explained_revision_is_not_contradiction_but_repeated_unexplained_is():
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    when = lambda m: datetime(2024, m, 1, tzinfo=IST)  # noqa: E731
    one = build_ledger([_gev("A", 25, when(5)), _gev("B", 15, when(11))], None, when(12))
    assert _status(one) != EvidenceStatus.CONTRADICTED
    two = build_ledger([_gev("A", 25, when(5)), _gev("B", 15, when(8)), _gev("C", 10, when(11))], None, when(12))
    assert _status(two) == EvidenceStatus.CONTRADICTED
    rq = "We now expect revenue growth of {}% in FY25."
    docs = {k: doc(rq.format(v) + " This reflects weaker demand in exports.", doc_id=k) for k, v in (("B", 15), ("C", 10))}
    expl = build_ledger([_gev("A", 25, when(5)), _gev("B", 15, when(8), quote=rq.format(15)),
                         _gev("C", 10, when(11), quote=rq.format(10))], None, when(12), docs)
    assert _status(expl) != EvidenceStatus.CONTRADICTED


def test_missing_latest_guidance_is_contradiction():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    rows = [_fm(Metric.REVENUE, date(2024, 3, 31), 1000, ptype="FY"),
            _fm(Metric.REVENUE, date(2025, 3, 31), 1050, ptype="FY")]
    led = build_ledger([_gev("A", 25, datetime(2024, 5, 1, tzinfo=IST)),
                        _gev("B", 15, datetime(2024, 11, 1, tzinfo=IST))],
                       FinancialSeries.build("T", rows), datetime(2025, 7, 1, tzinfo=IST))
    assert led[0].latest_outcome == GuidanceOutcome.MISSED
    assert _status(led) == EvidenceStatus.CONTRADICTED


def test_conservative_guidance_beaten_is_fine():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    rows = [_fm(Metric.REVENUE, date(2024, 3, 31), 1000, ptype="FY"),
            _fm(Metric.REVENUE, date(2025, 3, 31), 1400, ptype="FY")]
    led = build_ledger([_gev("A", 10, datetime(2024, 5, 1, tzinfo=IST))],
                       FinancialSeries.build("T", rows), datetime(2025, 7, 1, tzinfo=IST))
    assert led[0].outcome == GuidanceOutcome.MET and not led[0].flags
    assert _status(led) != EvidenceStatus.CONTRADICTED


def test_disappearing_target_flagged():
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    later = [datetime(2024, 8, 10, tzinfo=IST), datetime(2024, 11, 10, tzinfo=IST)]
    led = build_ledger([_gev("A", 25, datetime(2024, 5, 1, tzinfo=IST))], None,
                       datetime(2024, 12, 1, tzinfo=IST), commentary_times=later)
    assert any("disappearing" in f for f in led[0].flags)
    led2 = build_ledger([_gev("A", 25, datetime(2024, 5, 1, tzinfo=IST))], None,
                        datetime(2024, 12, 1, tzinfo=IST), commentary_times=later[:1])
    assert not led2[0].flags


# ---------------- results-table column / year-ago matching ----------------

def _rev_by_period(text):
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    d = doc(text, published_at=datetime(2025, 2, 10, tzinfo=IST))
    d.available_at = d.published_at
    rows, issues = parse_results_tables(d, chunk_document(d))
    return {(r.period_end, r.period_type): r.value for r in rows if r.metric == Metric.REVENUE}, issues


Q3_TITLE = "Statement of Consolidated Unaudited Financial Results for the quarter and nine months ended 31 December 2024"


def test_q3_statement_separates_quarter_nine_months_and_year():
    rev, _ = _rev_by_period("\n".join([
        Q3_TITLE, "(Rs. in crore)", "Particulars Quarter ended Nine months ended Year ended",
        "31.12.2024 30.09.2024 31.12.2023 31.12.2024 31.12.2023 31.03.2024",
        "1. Revenue from operations 300.00 280.00 250.00 850.00 700.00 950.00"]))
    assert rev == {(date(2024, 12, 31), "Q"): 300.0, (date(2024, 9, 30), "Q"): 280.0,
                   (date(2023, 12, 31), "Q"): 250.0,                      # year-ago quarter
                   (date(2024, 12, 31), "9M"): 850.0, (date(2023, 12, 31), "9M"): 700.0,
                   (date(2024, 3, 31), "FY"): 950.0}


def test_q2_statement_half_year_columns_not_taken_as_quarters():
    rev, _ = _rev_by_period("\n".join([
        "Statement of Standalone Unaudited Financial Results for the quarter and half year ended 30 September 2024",
        "(Rs. in crore)", "Particulars Quarter ended Half year ended Year ended",
        "30.09.2024 30.06.2024 30.09.2023 30.09.2024 30.09.2023 31.03.2024",
        "1. Revenue from operations 280.00 270.00 240.00 550.00 470.00 950.00"]))
    assert rev[(date(2023, 9, 30), "Q")] == 240.0 and rev[(date(2024, 9, 30), "Q")] == 280.0
    assert rev[(date(2024, 9, 30), "H")] == 550.0 and rev[(date(2024, 3, 31), "FY")] == 950.0


def test_q4_statement_repeated_march_dates_are_full_years():
    rev, _ = _rev_by_period("\n".join([
        "Statement of Audited Financial Results for the quarter and year ended 31 March 2025", "(Rs. in crore)",
        "Particulars Quarter ended Year ended",
        "31.03.2025 31.12.2024 31.03.2024 31.03.2025 31.03.2024",
        "1. Revenue from operations 320.00 300.00 260.00 1,170.00 950.00"]))
    assert rev[(date(2024, 3, 31), "Q")] == 260.0 and rev[(date(2024, 3, 31), "FY")] == 950.0
    assert rev[(date(2025, 3, 31), "Q")] == 320.0 and rev[(date(2025, 3, 31), "FY")] == 1170.0


@pytest.mark.parametrize("header", [
    "30-Jun-24 31-Mar-24 30-Jun-23 31-Mar-24",
    "Jun-24 Mar-24 Jun-23 Mar-24",
    "June 30, 2024 March 31, 2024 June 30, 2023 March 31, 2024",
    "30.06.24 31.03.24 30.06.23 31.03.24",
])
def test_alternative_date_header_formats(header):
    rev, _ = _rev_by_period("\n".join([
        "Statement of Consolidated Unaudited Financial Results for the quarter ended 30 June 2024",
        "(Rs. in crore)", "Particulars Quarter ended Year ended", header,
        "1. Revenue from operations 270.00 260.00 230.00 950.00"]))
    assert rev == {(date(2024, 6, 30), "Q"): 270.0, (date(2024, 3, 31), "Q"): 260.0,
                   (date(2023, 6, 30), "Q"): 230.0, (date(2024, 3, 31), "FY"): 950.0}


def test_fiscal_label_headers():
    rev, _ = _rev_by_period("\n".join([
        "Consolidated financial highlights", "(Rs. in crore)",
        "Particulars Q3 FY25 Q2 FY25 Q3 FY24 9M FY25 9M FY24 FY24",
        "Revenue from operations 300.0 280.0 250.0 850.0 700.0 950.0"]))
    assert rev[(date(2023, 12, 31), "Q")] == 250.0 and rev[(date(2024, 12, 31), "9M")] == 850.0
    assert rev[(date(2024, 3, 31), "FY")] == 950.0


def test_q1_without_previous_quarter_column():
    rev, _ = _rev_by_period("\n".join([
        "Statement of Unaudited Financial Results for the quarter ended 30 June 2024", "(Rs. in crore)",
        "Particulars Quarter ended Year ended", "30.06.2024 30.06.2023 31.03.2024",
        "1. Revenue from operations 270.00 230.00 950.00"]))
    assert rev == {(date(2024, 6, 30), "Q"): 270.0, (date(2023, 6, 30), "Q"): 230.0,
                   (date(2024, 3, 31), "FY"): 950.0}


def test_missing_year_ago_column_is_reported():
    _, issues = _rev_by_period("\n".join([
        "Statement of Unaudited Financial Results for the quarter ended 30 June 2024", "(Rs. in crore)",
        "Particulars Quarter ended", "30.06.2024 31.03.2024",
        "1. Revenue from operations 270.00 260.00"]))
    assert any("no year-ago quarter" in i for i in issues)


def test_yearless_month_day_not_read_as_year():
    from makrograph.earnings_inflection.extraction import resolve_columns
    cols, _ = resolve_columns(["Quarter ended Sep 30 Jun 30"])
    assert cols == []


def test_yoy_uses_year_ago_quarter_from_q3_statement():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    d = doc("\n".join([
        Q3_TITLE, "(Rs. in crore)", "Particulars Quarter ended Nine months ended Year ended",
        "31.12.2024 30.09.2024 31.12.2023 31.12.2024 31.12.2023 31.03.2024",
        "1. Revenue from operations 300.00 280.00 250.00 850.00 700.00 950.00"]),
        published_at=datetime(2025, 2, 10, tzinfo=IST))
    d.available_at = d.published_at
    rows, _ = parse_results_tables(d, chunk_document(d))
    s = FinancialSeries.build("T", rows)
    assert s.yoy(Metric.REVENUE, date(2024, 12, 31)) == pytest.approx(20.0)     # 300 vs 250, not vs 700


def test_sme_half_yearly_statement():
    rev, issues = _rev_by_period("\n".join([
        "Statement of Unaudited Financial Results for the half year ended 30 September 2024", "(Rs. in lakhs)",
        "Particulars Half year ended Year ended", "30.09.2024 31.03.2024 30.09.2023 31.03.2024",
        "1. Revenue from operations 6,000.00 5,500.00 4,500.00 10,000.00"]))
    assert rev == {(date(2024, 9, 30), "H"): 60.0, (date(2024, 3, 31), "H"): 55.0,
                   (date(2023, 9, 30), "H"): 45.0, (date(2024, 3, 31), "FY"): 100.0}
    assert not issues


def test_annual_only_statement():
    rev, _ = _rev_by_period("\n".join([
        "Statement of Audited Financial Results for the year ended 31 March 2025", "(Rs. in crore)",
        "Particulars Year ended", "31.03.2025 31.03.2024",
        "1. Revenue from operations 1,170.00 950.00"]))
    assert rev == {(date(2025, 3, 31), "FY"): 1170.0, (date(2024, 3, 31), "FY"): 950.0}


# ---------------- half-yearly series ----------------

def test_h2_derived_from_fy_minus_h1_and_ttm_uses_halves():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(Metric.REVENUE, date(2023, 9, 30), 40, ptype="H"),
            _fm(Metric.REVENUE, date(2024, 3, 31), 100, ptype="FY"),       # H2FY24 not reported separately
            _fm(Metric.REVENUE, date(2024, 9, 30), 70, ptype="H")]
    s = FinancialSeries.build("T", rows)
    h2 = s.get(Metric.REVENUE, date(2024, 3, 31), "H")
    assert h2.value == 60 and h2.source == "derived:FY-H1"
    assert s.cadence() == "H"
    assert s.ttm(Metric.REVENUE, date(2024, 9, 30), "H") == (130, [])
    assert s.yoy(Metric.REVENUE, date(2024, 9, 30), "H") == pytest.approx(75.0)


def test_cadence_prefers_quarterly_for_mainboard_q2_statement():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(Metric.REVENUE, d, v) for d, v in ((date(2023, 9, 30), 100), (date(2024, 9, 30), 130))]
    rows += [_fm(Metric.REVENUE, d, v, ptype="H") for d, v in ((date(2023, 9, 30), 190), (date(2024, 9, 30), 250))]
    assert FinancialSeries.build("T", rows).cadence() == "Q"


def test_cadence_after_sme_to_mainboard_migration_stays_half_yearly_until_quarters_comparable():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(Metric.REVENUE, d, v, ptype="H") for d, v in ((date(2023, 3, 31), 50), (date(2024, 3, 31), 70))]
    rows += [_fm(Metric.REVENUE, date(2024, 6, 30), 40)]          # first quarterly result, no year-ago quarter
    assert FinancialSeries.build("T", rows).cadence() == "H"


def test_half_year_guidance_judged_on_half_year_actuals():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    from makrograph.earnings_inflection.guidance_ledger import build_ledger
    g = _gev("A", 40, datetime(2024, 5, 1, tzinfo=IST), label="H1FY25")
    rows = [_fm(Metric.REVENUE, date(2023, 9, 30), 48, ptype="H"), _fm(Metric.REVENUE, date(2024, 9, 30), 68, ptype="H")]
    led = build_ledger([g], FinancialSeries.build("T", rows), datetime(2025, 1, 15, tzinfo=IST))
    assert led[0].outcome == GuidanceOutcome.MET and led[0].realized_value == pytest.approx(41.67, abs=0.01)


def test_fy_label_for_half_years():
    from makrograph.earnings_inflection.extraction import fy_label_for
    assert fy_label_for(date(2024, 9, 30), "H") == "H1FY25" and fy_label_for(date(2025, 3, 31), "H") == "H2FY25"


# ---------------- real-filing label / header / text-quality cases ----------------

@pytest.mark.parametrize("label,metric", [
    ("I Revenue from operations", Metric.REVENUE),
    ("1. Income from Operations", Metric.REVENUE),
    ("(a) Revenue from operations", Metric.REVENUE),
    ("VII Profit before tax (V-VI)", Metric.PBT),
    ("V Profit before exceptional items and tax (III-IV)", Metric.PBT_PRE_EXCEPTIONAL),
    ("IX Profit/(Loss) for the period (VII-VIII)", Metric.PAT),
    ("Net Profit attributable to owners of the Company", Metric.PAT_ATTRIBUTABLE),
    ("c) Depreciation and amortisation expense", Metric.DEPRECIATION),
    ("Total expenses (IV)", Metric.TOTAL_EXPENSES),
    ("(2) Diluted (in Rs.)", Metric.DILUTED_EPS),
    ("III Total Income (I+II)", None),
])
def test_row_label_matching(label, metric):
    from makrograph.earnings_inflection.extraction import _match_metrics
    got = _match_metrics(label)
    assert (got[0] if got else None) == metric


def test_amounts_without_currency():
    from makrograph.earnings_inflection.extraction import parse_inr
    assert parse_inr("revenue of 1 million in the coming year") is None
    assert parse_inr("revenue of Rs 1 million").value == 0.1
    assert parse_inr("an order of 450 crore").value == 450.0


def test_garbled_text_detection():
    from makrograph.earnings_inflection.extraction import is_garbled
    assert is_garbled("D c I o T m ! , E w D e b F n o t r e v e n u e")
    assert not is_garbled("We do not expect revenue growth to slow in FY25.")
    assert not is_garbled("Revenue from operations grew 36% to Rs 150 crore in Q3 FY24.")


def test_board_outcome_letter_with_call_details_is_results():
    from makrograph.earnings_inflection.document_versions import classify_document
    d = doc("Sub: Outcome of Board Meeting. The Board approved the Unaudited Standalone and Consolidated Financial "
            "Results. Dial-in details for the earnings conference call will follow.\n"
            "Statement of Unaudited Standalone Financial Results for the quarter ended 31 December 2023\n"
            "I Revenue from operations 17,345.12 16,210.40 12,001.11")
    assert classify_document(d)[0] == DocumentKind.FINANCIAL_RESULTS


def test_wrapped_and_stacked_headers():
    from makrograph.earnings_inflection.extraction import resolve_columns
    want = [(date(2023, 12, 31), "Q"), (date(2023, 9, 30), "Q"), (date(2022, 12, 31), "Q"),
            (date(2023, 12, 31), "9M"), (date(2022, 12, 31), "9M"), (date(2023, 3, 31), "FY")]
    assert resolve_columns(["Quarter ended Nine months ended Year ended", "31.12.2023 30.09.2023 31.12.2022",
                            "31.12.2023 31.12.2022 31.03.2023"])[0] == want
    assert resolve_columns(["Particulars", "31st 30th 31st 31st 31st 31st",
                            "December September December December December March",
                            "2023 2023 2022 2023 2022 2023"])[0] == want


def test_row_with_more_values_than_columns_is_skipped_not_shifted():
    rev, issues = _rev_by_period("\n".join([
        "Statement of Unaudited Financial Results for the quarter ended 30 June 2024", "(Rs. in crore)",
        "Particulars Quarter ended", "30.06.2024 31.03.2024 30.06.2023",
        "1. Revenue from operations 270.00 260.00 230.00 950.00 900.00"]))
    assert rev == {} and any("more values" in i for i in issues)


def test_tax_summed_from_current_and_deferred_rows():
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    d = doc("\n".join([
        "Statement of Unaudited Standalone Financial Results for the quarter ended 30 June 2024", "(Rs. in crore)",
        "Particulars Quarter ended Year ended", "30.06.2024 31.03.2024 30.06.2023 31.03.2024",
        "VIII Tax expense", "(1) Current tax 10.00 9.00 8.00 36.00", "(2) Deferred tax 1.00 - (1.00) 2.00"]),
        published_at=datetime(2024, 8, 1, tzinfo=IST))
    d.available_at = d.published_at
    rows, _ = parse_results_tables(d, chunk_document(d))
    tax = {r.period_end: r.value for r in rows if r.metric == Metric.TAX and r.period_type == "Q"}
    assert tax == {date(2024, 6, 30): 11.0, date(2024, 3, 31): 9.0, date(2023, 6, 30): 7.0}


def test_stale_latest_period():
    from makrograph.earnings_inflection.financial_series import FinancialSeries
    rows = [_fm(Metric.REVENUE, d, v) for d, v in ((date(2021, 6, 30), 50), (date(2022, 6, 30), 200))]
    p, end, note = FinancialSeries.build("T", rows).current_period(date(2024, 3, 31))
    assert end is None and "2022-06-30" in note


def test_old_or_unmeasurable_orders_do_not_make_commitment_backed():
    from makrograph.earnings_inflection.assessments import decide_status
    from makrograph.earnings_inflection.contracts import EconomicEvent
    old = EconomicEvent("e1", "T", Metric.ORDER_WIN, Quantity(2.88, Unit.INR_CRORE), "X",
                        datetime(2021, 11, 15, tzinfo=IST), commitment_strength=CommitmentStrength.BINDING)
    st, _ = decide_status([], [old], [], [], 1, 100.0, datetime(2024, 3, 31, tzinfo=IST))
    assert st != EvidenceStatus.COMMITMENT_BACKED
    new = EconomicEvent("e2", "T", Metric.ORDER_WIN, Quantity(500.0, Unit.INR_CRORE), "Y",
                        datetime(2024, 1, 15, tzinfo=IST), commitment_strength=CommitmentStrength.BINDING)
    st, why = decide_status([], [new], [], [], 1, None, datetime(2024, 3, 31, tzinfo=IST))
    assert st != EvidenceStatus.COMMITMENT_BACKED and any("materiality cannot be judged" in w for w in why)
    st, _ = decide_status([], [new], [], [], 1, 1000.0, datetime(2024, 3, 31, tzinfo=IST))
    assert st == EvidenceStatus.COMMITMENT_BACKED


# --- real-filing regressions (WINDMACHIN Q3 FY24) -------------------------------

WRAPPED_HEADER = [
    "Corresponding 3 Year to date Year to date",
    "Previous",
    "Preceding 3 months in the figures for figures for",
    "3 months ended Accounting Year",
    "Sr. months ended previous year current previous",
    "Particulars on 31.12.2023 ended on",
    "No. on 30.09.2023 ended on period ended On period ended On",
    "31.03.2023",
    "31.12.2022 31.12.2023 31.12.2022",
    "(Unaudited) (Unaudited) (Unaudited) (Unaudited) (Unaudited) (Audited)",
]


def test_header_cells_wrapped_over_many_lines_use_the_fixed_sebi_layout():
    rev, issues = _rev_by_period("\n".join([
        "STANDALONE UNAUDITED FINANCIAL RESULTS FOR THE QUARTER AND NINE MONTHS ENDED ON DECEMBER 31, 2023",
        "PARTI Rs. in Lakhs", *WRAPPED_HEADER,
        "a) Revenue from operations 8,409.92 8,476.75 9,167.68 23,446.62 22,725.79 35,112.84"]))
    assert rev[(date(2023, 12, 31), "Q")] == pytest.approx(84.0992)
    assert rev[(date(2022, 12, 31), "Q")] == pytest.approx(91.6768)
    assert rev[(date(2023, 12, 31), "9M")] == pytest.approx(234.4662)
    assert rev[(date(2023, 3, 31), "FY")] == pytest.approx(351.1284)


def test_ocr_letters_and_split_decimals_inside_figures_are_repaired():
    from makrograph.earnings_inflection.chunking import split_numeric_row
    assert split_numeric_row("a) Revenue from operations 8,409.92 8,476. 75 9,167.68")[1] == \
        ["8,409.92", "8,476.75", "9,167.68"]
    assert split_numeric_row("Cost of raw materials S,981.34 (363.8S) 6,S59.95")[1] == \
        ["5,981.34", "(363.85)", "6,559.95"]
    # words are never turned into numbers
    assert split_numeric_row("Total Income IS")[1] == []


def test_standalone_and_consolidated_statements_in_one_page_keep_their_own_scope():
    stmt = lambda title, rev: [title, "Rs. in Lakhs", *WRAPPED_HEADER,
                               f"a) Revenue from operations {rev}", ""]
    from makrograph.earnings_inflection.chunking import chunk_document
    from makrograph.earnings_inflection.extraction import parse_results_tables
    d = doc("\n".join(
        stmt("STANDALONE UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED ON DECEMBER 31, 2023",
             "8,409.92 8,476.75 9,167.68 23,446.62 22,725.79 35,112.84")
        + stmt("CONSOLIDATED UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED ON DECEMBER 31, 2023",
               "8,703.38 8,926.88 9,877.32 24,533.14 25,105.05 37,744.95")), published_at=datetime(2024, 2, 9, tzinfo=IST))
    d.available_at = d.published_at
    rows, _ = parse_results_tables(d, chunk_document(d))
    q = {(r.scope.value, r.period_end): r.value for r in rows
         if r.metric == Metric.REVENUE and r.period_type == "Q"}
    assert q[("standalone", date(2023, 12, 31))] == pytest.approx(84.0992)
    assert q[("consolidated", date(2023, 12, 31))] == pytest.approx(87.0338)
