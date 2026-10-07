"""D8/D7 (catalyst-rules-5): an order's value stated in a sentence after the award.

Wording is verbatim from Shakti Pumps' and Olectra Greentech's Regulation 30 filings."""

from datetime import datetime

from makrograph.earnings_inflection.chunking import chunk_document
from makrograph.earnings_inflection.contracts import IST, EventStage, Metric, SourceDocument
from makrograph.earnings_inflection.extraction import extract_sentence_evidence

SHAKTI = ("Dear Sir/Madam, Pursuant to regulation 30 of SEBI (Listing Obligations and Disclosure Requirements) "
          "Regulations, 2015 read with Schedule III thereof, we would like to inform you that Shakti Pumps (India) "
          "Limited Received its first work order under the KUSUM-3 scheme from Haryana Renewable Energy Department "
          "(HAREDA) for 7,781 pumps. The total amount of the work order is for around Rs. 358 Crores (inclusive of "
          "GST). Kindly take the same on record.")
OLECTRA = ("We would like to inform you that Olectra Greentech Limited has received a Letter of Award for supply of "
           "2,400 Electric Buses. These Buses shall be delivered over a period of 18 months. Value of these 2,400 "
           "Electric Buses supply would be approximately Rs. 4,000 Crores for Olectra.")


def orders(text):
    d = SourceDocument(doc_id="d", source_name="t", ticker="T", text=text,
                       published_at=datetime(2023, 8, 30, 18, 0, tzinfo=IST))
    d.available_at = d.published_at
    return [e for e in extract_sentence_evidence(d, chunk_document(d)) if e.metric == Metric.ORDER_WIN]


def test_value_in_the_following_sentence_belongs_to_the_award():
    o = [e for e in orders(SHAKTI) if e.quantity is not None]
    assert len(o) == 1 and o[0].quantity.value == 358.0
    assert "total amount of the work order" in o[0].quantity.raw          # where the value came from
    assert o[0].event_stage == EventStage.BINDING_ORDER


def test_value_of_supply_sentence_and_execution_period():
    o = [e for e in orders(OLECTRA) if e.quantity is not None]
    assert len(o) == 1 and o[0].quantity.value == 4000.0 and o[0].duration_months == 18


def test_two_values_are_never_guessed_between():
    o = orders(SHAKTI + " The total amount of the second work order is Rs. 120 Crores.")
    assert all(e.quantity is None for e in o)


def test_two_awards_with_one_value_are_not_linked():
    o = orders(SHAKTI.replace("Kindly take", "The Company has also received a work order from UP Agriculture "
                                            "Department. Kindly take"))
    assert all(e.quantity is None for e in o)
