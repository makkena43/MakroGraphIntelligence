"""Grammar-based roles of rupee amounts (spaCy).  Sentences are from the labelled sample, verbatim."""

import pytest

from makrograph.earnings_inflection import amount_roles

pytestmark = pytest.mark.skipif(not amount_roles.available(), reason="spaCy / en_core_web_sm not installed")


@pytest.mark.parametrize("sentence, expected", [
    ("Our order book now exceeds Rs. 900 Cr, demand and deal pipeline is the largest it's ever been.", ["book_level"]),
    ("The INR2,600 crore order book, which we are sitting on, 15% of that has already realized in FY '25.", ["book_level"]),
    ("With a combined order book and pipeline of over INR 2000 Cr, we are confident to maintain the revenue growth "
     "rate as well as the margins for this financial year.", ["other"]),
    ("Rajiv Rupani: In the month of May you had told that the order book is Rs. 931 Crores and now our order book "
     "is Rs. 1163 Crores.", ["other", "book_level"]),
    ("So we have added orders around Rs. 320 Cr or so this quarter.", ["inflow_total"]),
    ("We said that when we close this current financial year, we should have order book somewhere close to "
     "INR8,000 crores.", ["other"]),
])
def test_roles(sentence, expected):
    assert amount_roles.roles(sentence) == expected
