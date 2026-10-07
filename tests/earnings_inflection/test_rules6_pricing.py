"""D12 (catalyst-rules-6): price-change catalysts read their direction from the sentence, and quarterly
restatements of the same direction are one catalyst.  Quotes are RAIN's (Rain Industries) verbatim."""

from datetime import date

from makrograph.earnings_inflection.catalysts import _price_seeds, _stated_direction, _PRICE_TERM
from makrograph.earnings_inflection.contracts import CatalystKind, Metric, Modality, Quantity, Unit

from .test_catalysts import stmt

UP = [("Further, the average blended realisation increased by ~36.3% driven by higher prices.", date(2021, 7, 31)),
      ("During Q3 CY21, the average blended realisation increased by ~23.4% driven by prices.", date(2021, 10, 30)),
      ("During Q4 CY21, the average blended realisation increased by ~39.7% on prices.", date(2022, 2, 25))]
DOWN = [("During Q4 CY23, realisations decreased by 15.4% due to fall in commodity prices.", date(2024, 2, 24)),
        ("During Q1 CY24, realisations decreased by 4.5% due to fall in commodity prices.", date(2024, 5, 9))]


def ev(quote, when, direction):
    e = stmt(Metric.PRICING, quote, when)
    e.quantity, e.direction = Quantity(float(quote.split("%")[0].split("~")[-1].split()[-1]), Unit.PERCENT, ""), direction
    return e


def test_direction_is_read_from_the_sentence():
    assert _stated_direction(UP[0][0], _PRICE_TERM) == 1
    assert _stated_direction(DOWN[0][0], _PRICE_TERM) == -1


def test_price_decreases_are_never_catalysts_even_if_tagged_positive():
    assert _price_seeds([ev(q, d, +1) for q, d in DOWN]) == []          # rules-5 seeded these


def test_quarterly_restatements_are_one_catalyst():
    seeds = _price_seeds([ev(q, d, +1) for q, d in UP])
    assert {s.key for s in seeds} == {"price:2021-07-31"} and all(s.kind == CatalystKind.CONTRACT_PRICING for s in seeds)


def test_a_new_rise_after_a_decline_is_a_new_catalyst():
    later = [("During Q1 CY25, the average blended realisation increased by ~12% on prices.", date(2025, 5, 9))]
    seeds = _price_seeds([ev(q, d, +1) for q, d in UP + DOWN + later])
    assert {s.key for s in seeds} == {"price:2021-07-31", "price:2025-05-09"}
