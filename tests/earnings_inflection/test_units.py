"""Unit tests for extraction, validation, dedup, series, ledger, budget, guards."""

import re
from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.contracts import (
    IST, CommitmentStrength, ContractError, DocumentKind, Evidence, EvidenceTier, FinancialMeasurement,
    GuidanceOutcome, IssuerModel, Metric, Modality, Quantity, Scope, SourceDocument, Unit,
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
        elif "FROM mg_documents WHERE ticker" in sql:
            names = [c.split(" AS ")[-1].strip() for c in sql.split("SELECT ")[1].split(" FROM")[0].split(", ")]
            self.description = [(n,) for n in names]
            last_id = params[2]
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
