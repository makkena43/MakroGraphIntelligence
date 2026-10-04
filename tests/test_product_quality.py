"""Generic product-vocabulary precision regression tests."""

from src.makrograph.nlp.product_quality import is_product_label


def test_real_physical_products_survive_open_vocabulary_gate():
    for label in (
        "Solar Cell", "Power Transformers", "CRGO Steel", "Polysilicon",
        "Micro Irrigation Systems", "Active Pharmaceutical Ingredients",
    ):
        assert is_product_label(label, min_words=1, max_words=8), label


def test_corporate_boilerplate_and_sentence_fragments_are_rejected():
    for label in (
        "Co. Limited", "Registered Office", "Track Record", "before us",
        "facilities spread across", "technologically advanced", "cost. So",
        "GW Module", "GWh BESS", "raw material", "joint venture",
        "Private Limited", "Fully integrated", "Excellence Award",
    ):
        assert not is_product_label(label, min_words=1, max_words=8), label
