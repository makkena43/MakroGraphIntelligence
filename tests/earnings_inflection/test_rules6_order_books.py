"""D15 (catalyst-rules-6): order-book readings.  Aurionpro's rules-5 alerts rested on "200 cr" (an
amount retired from the book) and on "800 cr" (last year's level, in a sentence that then says the
book expanded to 900 cr); readings 900 and 800 cr three days apart became two catalysts.
Quotes are Aurionpro's earnings-call transcripts, verbatim."""

from datetime import date

from makrograph.earnings_inflection.contracts import CatalystKind
from makrograph.earnings_inflection.thesis import order_book_snapshots

from .test_catalysts import BOOKS, book, kind, rows, run

FLOW = ("And typically, most of the order book that we declared almost 80% of it is a next four quarter order "
        "book so we would have retired close to INR 200 Cr from that order book in Q1, we added a little bit "
        "more than INR 200 Cr so we are at 800 crore+.")
SUPERSEDED = ("Sahil Sharma: Sir for the last year or so our order book was hovering around Rs. 800 Cr, and in "
              "this quarter it has expanded to Rs. 900 Cr.")
LEVEL = "Our order book now exceeds INR 900 crores and with the demand environment remaining exceptionally strong."


def quoted(value, when, doc, quote):
    e = book(value, when, doc)
    e.quote = quote
    return e


def test_amounts_added_or_retired_and_superseded_levels_are_not_the_book():
    got = order_book_snapshots([quoted(200, date(2023, 8, 2), "a", FLOW),
                                quoted(900, date(2024, 2, 5), "b", LEVEL),
                                quoted(800, date(2024, 2, 8), "c", SUPERSEDED)])
    assert [e.doc_id for e in got] == ["b"]


def test_a_past_tense_book_statement_is_still_a_reading():
    q = "The order book as on 31 March 2024 was Rs. 1,250 crore, executable over 18 months."
    assert [e.doc_id for e in order_book_snapshots([quoted(1250, date(2024, 5, 20), "d", q)])] == ["d"]


def books(evidence, as_of=date(2024, 3, 1)):
    cats, _ = run(rows([100] * 12), evidence, as_of)
    return [c for c in kind(cats, CatalystKind.ORDERS) if "order book" in c.operating_change]


def test_conflicting_readings_days_apart_are_one_catalyst_with_the_conflict_recorded():
    got = books(BOOKS + [book(600, date(2023, 8, 25), "b23x")])        # +20% five days later
    assert len(got) == 1
    assert any("conflicting order-book reading of 600 cr" in f for f in got[0].facts)


def test_differing_readings_a_month_apart_stay_separate():
    assert len(books(BOOKS + [book(560, date(2023, 9, 20), "b23y")])) == 2      # +12%, 31 days later
