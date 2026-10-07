"""Layout text from pdfplumber word boxes (makrograph.parser.pdf_layout).  The word boxes below are
CESC's Q2 FY22 consolidated statement as pdfplumber reads it; pdftotext rendered the same row as
"3494  32 13  3351" and the header as "30 092021  30 062021 ..."."""

from makrograph.earnings_inflection.extraction import resolve_columns, split_numeric_row
from makrograph.parser.pdf_layout import words_to_text
from makrograph.parser.pdf_parser import PDFParser


def w(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 7}


HEADER = [w(t, x0, x1, 100) for t, x0, x1 in (("30.09.2021", 305, 333), ("30.06.2021", 349, 377),
                                               ("30.09.2020", 393, 422), ("30.09.2021", 438, 466),
                                               ("30.09.2020", 482, 511), ("31.03.2021", 528, 556))]
ROW = [w("Revenue", 66, 89, 120), w("from", 91, 102, 120), w("operations", 104, 131, 121)] + [
    w(t, x0, x1, 120) for t, x0, x1 in (("3494", 312, 324), ("3213", 356, 369), ("3351", 400, 412),
                                        ("6707", 445, 457), ("5965", 489, 502), ("11632", 534, 549))]


def test_numbers_stay_whole_and_columns_are_separated():
    text = words_to_text(HEADER + ROW)
    header, row = text.split("\n")
    label, cells = split_numeric_row(row)
    assert label.strip() == "Revenue from operations"
    assert cells == ["3494", "3213", "3351", "6707", "5965", "11632"]
    cols, issue = resolve_columns([header])
    assert len(cols) == 6 and not issue


def test_words_of_one_label_keep_single_spaces():
    assert "Revenue from operations" in words_to_text(ROW)


def test_an_empty_page_is_empty_text():
    assert words_to_text([]) == ""


def test_parser_layout_is_opt_in(tmp_path):
    plain = PDFParser({"output_dir": str(tmp_path)})
    words = PDFParser({"output_dir": str(tmp_path), "layout": "words"})
    assert plain.layout == "plain" and "layout-" not in plain.parser_version
    assert words.parser_version.endswith("/layout-words-v3")


def tilted(text_cells, top0, x0=340.0, slope=0.021, cw=3.8, gap=0.1):
    """Characters stored one by one with almost no gap, each a little lower than the last (Pricol's scans)."""
    out, x = [], x0
    for cell in text_cells:
        for ch in cell:
            out.append(w(ch, x, x + cw, top0 + slope * (x - x0)))
            x += cw + gap
        x += 12.0
    return out


def test_a_tilted_scan_stored_character_by_character_reads_as_whole_numbers_on_one_line():
    cells = ["56,269.01", "55,322.33", "50,113.79", "2,19,175.34", "1,87,191.81", "56,621.24", "55,719.10",
             "50,968.55", "2,20,816.89", "1,90,283.12"]
    row = [w("Revenue", 66, 89, 156.0), w("from", 91, 102, 156.1)] + tilted(cells, 163.6)
    nxt = [w("Other", 66, 81, 166.0), w("income", 82, 101, 166.1)] + tilted([c.replace("5", "4") for c in cells], 173.6)
    lines = words_to_text(row + nxt).split("\n")
    assert len(lines) == 2
    assert split_numeric_row(lines[0])[1] == cells
