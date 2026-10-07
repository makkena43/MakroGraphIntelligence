"""D5 / D6 (catalyst-rules-5): order-book restatements are one catalyst, implausible readings are none.
Under rules-4 SWSOLAR counted 4,903 and 4,900 cr (five days apart) as two catalysts, and GENUSPOWER
read 18 -> 1,761 cr (+9,683%) and 0.7 -> 21,006 cr as order-book growth."""

from datetime import date

from makrograph.earnings_inflection.contracts import CatalystKind

from .test_catalysts import BOOKS, book, kind, rows, run

FLAT = [100] * 12                        # TTM revenue 400 cr


def books(evidence, as_of=date(2024, 3, 1)):
    cats, _ = run(rows(FLAT), evidence, as_of)
    return [c for c in kind(cats, CatalystKind.ORDERS) if "order book" in c.operating_change]


def test_a_restated_book_days_later_is_the_same_catalyst():
    one = books(BOOKS)
    two = books(BOOKS + [book(510, date(2023, 8, 30), "b23r")])
    assert len(one) == len(two) == 1 and two[0].catalyst_id == one[0].catalyst_id


def test_a_book_far_below_revenue_is_not_the_order_book():
    assert books([book(18, date(2022, 8, 10), "a"), book(500, date(2023, 8, 20), "b")]) == []


def test_an_uncorroborated_jump_is_not_used_and_a_corroborated_one_is():
    base = [book(60, date(2022, 8, 10), "a"), book(410, date(2023, 8, 20), "b")]      # 6.8x
    assert books(base) == []
    seen = books(base + [book(400, date(2023, 10, 5), "c")])     # a second filing at the new level
    assert len(seen) == 1
