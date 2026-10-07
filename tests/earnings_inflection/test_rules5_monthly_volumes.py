"""D11 (catalyst-rules-5): monthly business updates (unit sales) as a leading signal.

The table layout is SML Isuzu's monthly Regulation 30 update, including its scanning damage."""

import calendar
from datetime import date, datetime, timedelta

from makrograph.earnings_inflection.catalysts import DEFAULT_CATALYST_THRESHOLDS, _volume_seeds
from makrograph.earnings_inflection.contracts import IST, CatalystKind, Metric, SourceDocument
from makrograph.earnings_inflection.extraction import parse_monthly_volumes

SML_OCT21 = """Pursuant to Regulation 30 of the SEBI (Listing Obligations and Disclosure Requirements)
    Regu lations , 2015 , sales figure for the month of October 2021 is given below :
                                                     October ·                     Cumulative (Apr-Oct)
I Category                         2021-22 2020-21 1 % Change 2021-22 2020-21 % Change
    Cargo Vehicles
                                       637               443         44%         2614      1298          101 %
    Passenger Vehicles
                                       106       I       139         -24%        788       473           67%
I Total                                743 1             582          28%        3402      1771          92%
"""


def doc(text, when):
    d = SourceDocument(doc_id=f"m{when:%y%m%d}", source_name="t", ticker="T", text=text,
                       published_at=datetime.combine(when, datetime.min.time(), tzinfo=IST))
    d.available_at = d.published_at
    return d


def test_total_row_is_read_and_checked_against_the_printed_change():
    e = parse_monthly_volumes(doc(SML_OCT21, date(2021, 11, 1)))
    assert len(e) == 1 and e[0].metric == Metric.VOLUME
    assert e[0].quantity.value == 743 and e[0].prior_year_value == 582      # not "1" from the scan damage
    assert e[0].period_label == "2021-10-31"


def test_a_total_row_whose_change_does_not_match_is_not_used():
    assert parse_monthly_volumes(doc(SML_OCT21.replace("28%", "61%"), date(2021, 11, 1))) == []


def month_doc(y, m, cur, prev):
    name = calendar.month_name[m]
    txt = (f"Sales figure for the month of {name} {y} is given below:\n"
           f"Total      {cur}      {prev}      {(cur / prev - 1) * 100:.0f}%      0     0     0%\n")
    end = date(y, m, calendar.monthrange(y, m)[1])
    return parse_monthly_volumes(doc(txt, end + timedelta(days=1)))[0]


def series(values, start=(2023, 1)):
    y, m = start
    out = []
    for cur, prev in values:
        out.append(month_doc(y, m, cur, prev))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def test_two_consecutive_strong_windows_make_one_catalyst_dated_at_the_second():
    ev = series([(100, 100)] * 3 + [(140, 100)] * 4)          # strong from April: windows to May (+27%), Jun (+40%)
    seeds = _volume_seeds(ev, DEFAULT_CATALYST_THRESHOLDS)
    first = min(seeds, key=lambda s: s.at)
    assert first.kind == CatalystKind.VOLUME and first.at.date() == date(2023, 7, 1)   # June update published
    assert {s.key for s in seeds} == {"volume:2023-05"}


def test_a_single_spike_month_is_not_a_run():
    assert _volume_seeds(series([(100, 100)] * 3 + [(200, 100)] + [(100, 100)] * 3), DEFAULT_CATALYST_THRESHOLDS) == []


def test_separate_runs_are_separate_catalysts():
    ev = series([(140, 100)] * 4 + [(100, 100)] * 4 + [(140, 100)] * 4)
    assert len({s.key for s in _volume_seeds(ev, DEFAULT_CATALYST_THRESHOLDS)}) == 2


def test_a_depressed_year_ago_base_must_also_be_beaten_against_two_years_earlier():
    year1 = series([(100, 100)] * 12, start=(2022, 1))
    slump = series([(60, 100)] * 12, start=(2023, 1))          # 2023 down 40% on 2022
    rebound = series([(90, 60)] * 6, start=(2024, 1))          # +50% on 2023 but -10% on 2022
    assert _volume_seeds(year1 + slump + rebound, DEFAULT_CATALYST_THRESHOLDS) == []
