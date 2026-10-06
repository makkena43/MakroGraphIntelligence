"""Shared ingestion: credit-rating rationales are downloadable only behind an opt-in flag."""

from makrograph.pipeline.intelligence_pipeline import CREDIT_RATING_CATEGORIES, high_value_pdf_categories


def test_default_categories_are_unchanged_and_exclude_ratings():
    cats = high_value_pdf_categories()
    assert "Outcome of Board Meeting" in cats and "Investor Presentation" in cats
    assert not set(CREDIT_RATING_CATEGORIES) & set(cats)


def test_ratings_are_added_only_when_requested_and_never_override_an_explicit_list():
    assert set(CREDIT_RATING_CATEGORIES) <= set(high_value_pdf_categories(include_credit_rating_rationales=True))
    assert high_value_pdf_categories(["Press Release"], include_credit_rating_rationales=True) == ["Press Release"]
