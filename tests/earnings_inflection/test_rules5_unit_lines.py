"""D2 (catalyst-rules-5): unit lines damaged by scanning, as printed in GMM Pfaudler, Shakti Pumps
and Waaree Energies results (verbatim).  Without a unit, a results table is not used at all."""

import pytest

from makrograph.earnings_inflection.extraction import _table_scale


@pytest.mark.parametrize("line,scale", [
    ("¥ In Crore (except per share data)", 1.0),                    # rupee sign scanned as a yen sign
    ("Z In Crore (except per share data)", 1.0),                    # ... or as a Z
    ("                     in [acs, unless otherwise", 0.01),        # "lacs" with its l scanned as "["
    ('Amount in" MilHons unless otherwise stJ1ted', 0.1),            # "Millions"
    ("Amount In, Millions unless otherwise stated", 0.1),           # plural scale word
    ("(Rs. in Lakhs)", 0.01),                                        # unchanged
])
def test_damaged_unit_lines(line, scale):
    assert _table_scale(line, "") == pytest.approx(scale)


@pytest.mark.parametrize("prose", [
    "The Board approved an investment in crores of rupees over three years.",
    "in [acs of the region",                                         # damaged word but not a unit line
    "Zone In Crore",
])
def test_prose_is_not_a_unit_line(prose):
    assert _table_scale(prose, "") is None
