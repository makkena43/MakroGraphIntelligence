"""WP4 - economically meaningful external-demand evidence."""

from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.assessments import decide_status
from makrograph.earnings_inflection.chunking import chunk_document
from makrograph.earnings_inflection.contracts import (
    IST, CustomerVerification, EventStage, EvidenceStatus, Metric, RelationshipStatus, SourceDocument, TaxBasis,
    ValueBasis,
)
from makrograph.earnings_inflection.counterparty import apply_reference_data, build_profiles
from makrograph.earnings_inflection.demand import summarise_demand
from makrograph.earnings_inflection.event_resolution import resolve_events
from makrograph.earnings_inflection.extraction import extract_sentence_evidence


def ev_from(*docs):
    """docs: (doc_id, text, 'YYYY-MM-DD')"""
    out = []
    for doc_id, text, when in docs:
        d = SourceDocument(doc_id=doc_id, source_name="t", ticker="T", text=text,
                           published_at=datetime.fromisoformat(when + "T12:00:00+05:30"))
        d.available_at = d.published_at
        out += extract_sentence_evidence(d, chunk_document(d))
    return [e for e in out if e.metric == Metric.ORDER_WIN]


AS_OF = datetime(2024, 9, 30, tzinfo=IST)
PO = "The Company has received a purchase order worth Rs 450 crore from Eastern Rail Systems Limited for supply of bogie frames."


# ---------------- dated states ----------------

def test_partial_cancellation_reduces_current_value_and_keeps_history():
    events = resolve_events(ev_from(
        ("A", PO, "2024-02-10"),
        ("B", "Eastern Rail Systems Limited has cancelled Rs 150 crore of the purchase order for bogie frames.",
         "2024-07-01")))
    assert len(events) == 1
    e = events[0]
    assert e.amount.value == 300.0 and e.original_amount.value == 450.0 and e.cancelled_amount == 150.0
    assert e.current_stage == EventStage.BINDING_ORDER
    assert [h.stage for h in e.history] == [EventStage.BINDING_ORDER, EventStage.CANCELLED]


def test_full_cancellation_reverses_the_commitment():
    events = resolve_events(ev_from(
        ("A", PO, "2024-02-10"),
        ("B", "The purchase order from Eastern Rail Systems Limited has been cancelled by the customer.", "2024-07-01")))
    e = events[0]
    assert e.current_stage == EventStage.CANCELLED and e.amount.value == 0.0
    dem = summarise_demand(events, [], AS_OF.date())
    assert dem.verified_inflow_crore == 0.0 and dem.cancellations_crore == 450.0


def test_amendment_can_reduce_value_strongest_ever_not_retained():
    events = resolve_events(ev_from(
        ("A", PO, "2024-02-10"),
        ("B", "The order value from Eastern Rail Systems Limited has been revised to Rs 300 crore.", "2024-05-01")))
    e = events[0]
    assert e.amount.value == 300.0 and e.history[-1].stage == EventStage.AMENDED
    assert summarise_demand(events, [], AS_OF.date()).verified_inflow_crore == 300.0


def test_l1_progresses_to_po_but_a_later_retelling_never_downgrades():
    events = resolve_events(ev_from(
        ("A", "The Company has been declared L1 bidder for a Rs 450 crore tender of Eastern Rail Systems Limited.",
         "2024-01-10"),
        ("B", PO, "2024-03-01"),
        ("C", "Earlier this year we were L1 for the Rs 450 crore bogie frames tender of Eastern Rail Systems Limited.",
         "2024-08-01")))
    assert len(events) == 1
    e = events[0]
    assert e.current_stage == EventStage.BINDING_ORDER
    assert [h.stage for h in e.history] == [EventStage.PREFERRED_BIDDER, EventStage.BINDING_ORDER]
    assert len(e.doc_ids) == 3                                   # three descriptions, one event


def test_unlinked_cancellation_is_recorded_but_never_counted_as_demand():
    events = resolve_events(ev_from(("B", "An order worth Rs 80 crore has been cancelled.", "2024-07-01")))
    assert events[0].amount is None and "predecessor" in events[0].unresolved_fields[0]
    assert summarise_demand(events, [], AS_OF.date()).verified_inflow_crore == 0.0


# ---------------- relationships ----------------

def test_own_subsidiary_order_is_related_and_excluded():
    events = resolve_events(ev_from(("A", "The Company has received a purchase order worth Rs 200 crore from "
                                          "Rail Components Limited, its wholly owned subsidiary.", "2024-03-01")))
    e = events[0]
    assert e.relationship == RelationshipStatus.CONFIRMED_RELATED
    dem = summarise_demand(events, [], AS_OF.date())
    assert dem.verified_inflow_crore == 0 and dem.related_party_excluded == [e.event_id]
    assert any("own group" in f for f in build_profiles(events, 1000.0)[0].risk_flags)


def test_customers_subsidiary_is_not_the_issuers_subsidiary():
    events = resolve_events(ev_from(("A", "The Company has received a purchase order worth Rs 200 crore from "
                                          "Metro Coach Limited, a subsidiary of Global Mobility Corporation.",
                                     "2024-03-01")))
    assert events[0].relationship == RelationshipStatus.UNKNOWN


def test_sebi_related_party_answer_is_an_issuer_assertion_only():
    text = ("Disclosure under Regulation 30. The Company has received a purchase order worth Rs 200 crore from "
            "Metro Coach Limited. Whether the order has been awarded by any related party: No. "
            "Time period by which the order is to be executed: 24 months.")
    events = resolve_events(ev_from(("A", text, "2024-03-01")))
    e = events[0]
    assert e.relationship == RelationshipStatus.ISSUER_ASSERTED_UNRELATED
    assert e.relationship != RelationshipStatus.INDEPENDENTLY_SUPPORTED_UNRELATED
    assert e.duration_months == 24 and e.annual_executable_estimate == pytest.approx(100.0)
    assert any("issuer assertion only" in f for f in build_profiles(events, 1000.0)[0].risk_flags)


def test_reference_data_needs_a_dated_source_before_the_cutoff():
    events = resolve_events(ev_from(("A", PO, "2024-02-10")))
    ref = {"Eastern Rail Systems Ltd": {"relationship": "unrelated",
                                        "sources": [{"source": "customer annual report", "as_of": "2024-10-15"}]}}
    notes = apply_reference_data(events, ref, date(2024, 9, 30))
    assert events[0].relationship == RelationshipStatus.UNKNOWN and "postdates the cutoff" in notes[0]
    ref["Eastern Rail Systems Ltd"]["sources"][0]["as_of"] = "2023-12-01"
    apply_reference_data(events, ref, date(2024, 9, 30))
    assert events[0].relationship == RelationshipStatus.INDEPENDENTLY_SUPPORTED_UNRELATED
    assert events[0].customer_verification == CustomerVerification.CORROBORATED


# ---------------- contract economics ----------------

@pytest.mark.parametrize("text,basis", [
    ("The Company has signed a framework agreement with Northern Utility Limited for supplies up to Rs 1,000 crore.",
     ValueBasis.CEILING),
    ("The Company has received a purchase order worth Rs 100 crore from Northern Utility Limited with a minimum "
     "guaranteed offtake.", ValueBasis.GUARANTEED_MINIMUM),
    ("The Company has received a release order worth Rs 60 crore from Northern Utility Limited under the rate contract.",
     ValueBasis.EXECUTABLE_RELEASE),
    ("The Company has entered into a seven-year framework agreement with Northern Utility Limited.",
     ValueBasis.UNQUANTIFIED),
])
def test_value_basis(text, basis):
    evs = ev_from(("A", text, "2024-03-01"))
    assert evs and evs[0].value_basis == basis


def test_ceiling_and_unquantified_frameworks_are_not_verified_inflow():
    events = resolve_events(ev_from(
        ("A", "The Company has signed a framework agreement with Northern Utility Limited for supplies up to "
              "Rs 1,000 crore.", "2024-03-01")))
    dem = summarise_demand(events, [], AS_OF.date())
    assert dem.verified_inflow_crore == 0
    assert any("ceiling" in r for r in dem.unverified_events[events[0].event_id])


def test_tax_inclusive_value_is_labelled_not_revenue():
    events = resolve_events(ev_from(("A", "The Company has received a purchase order worth Rs 118 crore "
                                          "(inclusive of GST) from Northern Utility Limited.", "2024-03-01")))
    assert events[0].tax_basis == TaxBasis.INCLUSIVE
    assert any("include taxes" in n for n in summarise_demand(events, [], AS_OF.date()).notes())


# ---------------- deduplication by identity ----------------

def test_same_amount_different_named_customers_are_two_orders():
    events = resolve_events(ev_from(("A", PO, "2024-02-10"),
                                    ("B", PO.replace("Eastern Rail Systems Limited", "Western Coach Limited"),
                                     "2024-02-20")))
    assert len(events) == 2 and not events[0].ambiguous_with


def test_same_amount_unnamed_mention_is_ambiguous_and_counted_once():
    events = resolve_events(ev_from(("A", PO, "2024-02-10"),
                                    ("B", "We bagged an order worth Rs 450 crore for bogie frames.", "2024-03-05")))
    assert len(events) == 2 and events[0].ambiguous_with == [events[1].event_id]
    dem = summarise_demand(events, [], AS_OF.date())
    assert dem.verified_inflow_crore == 450.0 and dem.ambiguous_groups == 1


def test_different_order_references_are_different_orders():
    events = resolve_events(ev_from(
        ("A", PO.replace("purchase order", "purchase order No. ERS/2024/011"), "2024-02-10"),
        ("B", PO.replace("purchase order", "purchase order No. ERS/2024/019"), "2024-02-11")))
    assert len(events) == 2


# ---------------- inflow vs backlog vs cancellations ----------------

def test_backlog_is_a_dated_snapshot_not_added_to_inflow():
    order_book = ev_from(("T", "Our order book stands at Rs 1,400 crore as of June 30, 2024.", "2024-08-10"))
    from makrograph.earnings_inflection.extraction import extract_sentence_evidence as _x  # noqa: F401
    d = SourceDocument(doc_id="T", source_name="t", ticker="T",
                       text="Our order book stands at Rs 1,400 crore as of June 30, 2024.",
                       published_at=datetime(2024, 8, 10, tzinfo=IST))
    d.available_at = d.published_at
    ob = [e for e in extract_sentence_evidence(d, chunk_document(d)) if e.metric == Metric.ORDER_BOOK]
    events = resolve_events(ev_from(("A", PO, "2024-02-10")))
    dem = summarise_demand(events, ob, AS_OF.date())
    assert dem.backlog_crore == 1400.0 and dem.verified_inflow_crore == 450.0      # separate, not summed
    stale = summarise_demand(events, ob, date(2025, 6, 30))
    assert stale.backlog_crore is None and "stale" in stale.backlog_note
    assert order_book == []


# ---------------- verified vs early lane ----------------

def test_verified_binding_external_order_backs_a_commitment():
    events = resolve_events(ev_from(("A", PO, "2024-06-10")))
    st, why = decide_status([], events, [], [], 1, 1000.0, AS_OF)
    assert st == EvidenceStatus.COMMITMENT_BACKED and any("verified binding external" in w for w in why)


def test_anonymous_binding_order_stays_in_the_early_lane():
    events = resolve_events(ev_from(("A", "We have received a purchase order worth Rs 450 crore from a leading "
                                          "European OEM.", "2024-06-10")))
    st, why = decide_status([], events, [], [], 1, 1000.0, AS_OF)
    assert st == EvidenceStatus.EARLY_COMMITMENT_UNVERIFIED and any("customer not named" in w for w in why)


def test_related_party_orders_never_support_a_commitment():
    events = resolve_events(ev_from(("A", "The Company has received a purchase order worth Rs 900 crore from "
                                          "Rail Components Limited, its wholly owned subsidiary.", "2024-06-10")))
    st, why = decide_status([], events, [], [], 1, 1000.0, AS_OF)
    assert st not in (EvidenceStatus.COMMITMENT_BACKED, EvidenceStatus.EARLY_COMMITMENT_UNVERIFIED)
    assert any("related-party" in w for w in why)


def test_end_to_end_report_shows_order_lifecycle(repo):
    from makrograph.earnings_inflection.pipeline import EarningsInflectionPipeline
    from makrograph.earnings_inflection.rendering import render_markdown
    a = EarningsInflectionPipeline({}, repo).run(["ACMEGRID"], "2024-10-31").assessments[0]
    md = render_markdown(a)
    assert "### Order events (deduplicated; latest dated state)" in md
    lo = [e for e in a.events if e.customer_verification == CustomerVerification.ANONYMOUS]
    assert lo and lo[0].current_stage == EventStage.PREFERRED_BIDDER
