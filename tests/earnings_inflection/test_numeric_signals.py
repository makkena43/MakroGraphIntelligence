"""Numbers-first early signals (numeric-rules-1)."""

from datetime import date, datetime, timedelta, timezone

from makrograph.earnings_inflection.contracts import FinancialMeasurement, Metric, Scope, Unit
from makrograph.earnings_inflection.financial_series import FinancialSeries
from makrograph.earnings_inflection.numeric_signals import detect_numeric_signals, signals_for_quarter

IST = timezone(timedelta(hours=5, minutes=30))
ENDS = [date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31), date(2023, 3, 31),
        date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31),
        date(2024, 6, 30), date(2024, 9, 30)]


def published(end):
    return datetime(end.year, end.month, end.day, 18, 0, tzinfo=IST) + timedelta(days=45)


def quarter(end, rev, expenses, da=5.0, fc=5.0, oi=1.0, tax_rate=0.25, exc=0.0, when=None, doc=None):
    pbt = rev + oi - expenses + exc
    pat = pbt * (1 - tax_rate) if pbt > 0 else pbt
    vals = {Metric.REVENUE: rev, Metric.OTHER_INCOME: oi, Metric.TOTAL_EXPENSES: expenses,
            Metric.DEPRECIATION: da, Metric.FINANCE_COST: fc, Metric.PBT: pbt, Metric.PAT: pat,
            Metric.EXCEPTIONAL_ITEMS: exc}
    when = when or published(end)
    return [FinancialMeasurement(ticker="T", metric=m, period_end=end, period_type="Q", value=v, unit=Unit.INR_CRORE,
                                 scope=Scope.STANDALONE, doc_id=doc or f"r{end}", available_at=when)
            for m, v in vals.items()]


def flat_then(last_rev, last_exp, n_flat=8, rev=100.0, exp=90.0, **kw):
    rows = []
    for i, e in enumerate(ENDS):
        if i < n_flat:
            rows += quarter(e, rev, exp)
        else:
            rows += quarter(e, last_rev, last_exp, **kw)
    return rows


def kinds(rows, end=ENDS[8]):
    s = FinancialSeries.build("T", rows)
    return {x.kind for x in signals_for_quarter(s, end, published(end))}


def test_a_flat_company_raises_nothing():
    assert kinds(flat_then(100.0, 90.0)) == set()


def test_revenue_acceleration_needs_growth_beyond_its_own_history():
    assert "revenue_acceleration" in kinds(flat_then(130.0, 117.0))          # +30% vs ~0% before


def test_operating_leverage_and_profit_step_up():
    k = kinds(flat_then(125.0, 100.0))                                        # margin 15% -> 24%
    assert {"operating_leverage", "profit_step_up"} <= k


def test_a_profit_jump_from_other_income_or_exceptional_items_is_not_a_signal():
    assert "profit_step_up" not in kinds(flat_then(100.0, 90.0, oi=40.0))
    assert "profit_step_up" not in kinds(flat_then(100.0, 90.0, exc=30.0))


def test_finance_cost_relief():
    rows = []
    for i, e in enumerate(ENDS):
        fc = 4.0 if i >= 8 else 8.0                                            # EBITDA 20: 8 is 40%
        rows += quarter(e, 100.0, 85.0 - (8.0 - fc), fc=fc)
    assert "finance_cost_relief" in kinds(rows)


def test_signals_are_point_in_time_and_a_later_restatement_does_not_change_them():
    rows = flat_then(125.0, 100.0)
    times = sorted({r.available_at for r in rows})
    series_at = lambda t, rs=rows: FinancialSeries.build("T", [r for r in rs if r.available_at <= t])  # noqa: E731
    first = detect_numeric_signals(series_at, times)
    assert first and all(s.known_at == published(s.period_end) for s in first)
    # a year later the June-2024 quarter is restated down to flat
    restated = rows + quarter(ENDS[8], 100.0, 90.0, when=datetime(2025, 8, 14, tzinfo=IST), doc="restated")
    times2 = sorted({r.available_at for r in restated})
    series_at2 = lambda t: FinancialSeries.build("T", [r for r in restated if r.available_at <= t])  # noqa: E731
    again = detect_numeric_signals(series_at2, times2)
    assert [(s.signal_id, s.known_at) for s in again if s.period_end == ENDS[8]] == \
        [(s.signal_id, s.known_at) for s in first if s.period_end == ENDS[8]]


def test_an_old_quarter_first_read_late_is_not_judged():
    rows = flat_then(125.0, 100.0)
    late = [r for r in rows if r.period_end != ENDS[9]]
    late += [FinancialMeasurement(**{**r.__dict__, "available_at": datetime(2025, 9, 1, tzinfo=IST)})
             for r in rows if r.period_end == ENDS[9]]
    times = sorted({r.available_at for r in late})
    out = detect_numeric_signals(lambda t: FinancialSeries.build("T", [r for r in late if r.available_at <= t]), times)
    assert all(s.period_end != ENDS[9] for s in out)
