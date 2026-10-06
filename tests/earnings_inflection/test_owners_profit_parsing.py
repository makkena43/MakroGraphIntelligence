"""D1 (catalyst-rules-4): consolidated profit attributable to owners of the parent.

Excerpts are verbatim from the companies' public NSE filings (scanning damage kept). In Ind AS
statements the "Profit/(Loss) attributable to:" block follows the comprehensive-income lines, and
the same "Owners of the Company" rows repeat under the OCI and total-comprehensive-income headings;
statements also continue across page breaks without repeating their column header."""

from datetime import date, datetime

import pytest

from makrograph.earnings_inflection.contracts import IST, Metric, Scope

from .test_real_filing_layouts import parse, val

FF = "\f"                              # page break in the stored text

# Borosil Renewables, consolidated Q1 FY25 (published 2024-08); page break before line 12
BOROSIL_Q1FY25 = """                               UNAUDITED CONSOLIDATED FINANCIAL RESULTS
                                  FOR THE QUARTER ENDED 30TH JUNE, 2024
                                                                                              (Rs. in Lakhs except as stated)
 S. Particulars                                                             Quarter Ended                      Year Ended
No.                                                          30.06.2024      31.03.2024      30.06.2023        31.03.2024
 1 Income:
    (a) Revenue from Operations                                37,079.21       28,311.49        35,449.91             1,36,928.34
    (b) Other Income                                              352.98          472.26           796.77                2,117 .89
                   Total Income (1)                            37,432.19       28,783.75        36,246.68             1,39,046.23
 2 Expenses
    (a) Cost of Materials Consumed                              9,632.44        7,254.14         8,662.43                35,072.85
    (b) Changes in Inventories of Finished Goods, Work            568.17          222 .77          533 .96                1,184.03
          in-Progress and Stock-in-Trade
    (c) Employee Benefits Expense                               5,755.82        5,368.27         5,615.10                21,823 .75
    (d) Finance costs                                             728.67          316.34           901.15                 2,921.86
    (e) Depreciation and Amortisation Expense                   3,395.10        3,358.45         3,242.24                13,171.59
    (f)   Power and Fuel                                       10,762.55        9,805.34         9,982.08                41,201.08
    (g) Other Expenses                                          8,121.80        8,214.78         8,009 .17               32,279.90
                   TotaIExpenses{2)                            38,964.55       34,540.09        36,946.13             1,47,655.06
 3 Loss before share of profit in associate, exceptional       {1,532.36)      (5,756.34)         {699.45)              (8,608.83)
    items and tax (1-2)
 4 Share of profit/(Loss) in associate                            (24.57)          33.78             24.37                     91.70
 5 Loss before exceptional items and tax (3+4)                 {1,556.93)      (5,722.56)         {675.08)                (8,517.13)
 6 Exceptional Items (Refer Note No. 2)                                            (5 .62)                                (3,244.22)
 7 Loss Before Tax {5-6)                                       {1,556.93)      (5,716.94)         {675.08)                (5,272.91)
 8 Tax Expense
    (a) Current Tax                                                 0.40           36.39           371.28                    246.26
    (b) Deferred Tax                                             (133.53)        (420.93)          106.72                   (383.11)
    (c) Income Tax of earlier years                                                                                         (108 .70)
    Total Tax Expenses                                           (133.13)        {384.54)          478.00                   {245.55)
 9 Loss for the period/year (7-8)                              (1,423.80)      (5,332.40)       (1,153.08)                (5,027.36)
10 Other Comprehensive Income (OCI)
    (a) Items that will not be reclassified to profit or
          loss:
    (i)   Re-measurement    gains/(losses)    on   defined        (21.95)         {71.32)             (5.49)                  (87 .79)
          benefit plans
   (ii) Tax effect on above                                         4.77           14.94               1.38                    19.08
   (b) Items that will be reclassified to profit & Loss
   (i)   Foreign currency Translation Reserve                     (88.68)        (165.23)           (59 .66)                     3.50
   (ii) Tax effect on above
   Total Other Comprehensive                  ,o                 {105.86)        (221.61)           (63. 77)                      (65.21)
11 Total Comprehensive lnco                          year      {1,529.66)      {5,554.01)       {1,21      1,i.,::::::==:::::,,c,   2.57)
   (9+10)
\f12 Profit/(Loss) attributable to:
     (i)   Owners of the Company                                 (1,296.28)       (4,807.22)         (832.16)       (4,689.54)
     (ii) Non-controlling interest                                 (127.52)         (525.18)         (320.92)         (337.82)
13   Other Comprehensive Income attributable to:
     (i)   Owners of the Company                                   (105.13)         (218.70)           (63.77)              (62.30)
     (ii) Non-controlling interest                                    (0.73)           (2 .91)            -                  (2.91)
14   Total Comprehensive Income attributable to:
     (i)   Owners of the Company                                 (1,401.41)       (5,025.92)          (895.93)      (4,751.84)
     (ii) Non-controlling interest                                 (128.25)         (528.09)          (320.92)        (340.73)
15   Paid-up Equity Share Capital (Face value of Re. 1/-          1,305.38         1,305 .38         1,305.21        1,305.38
"""

# Olectra Greentech, consolidated Q2 FY25: scanned "Profil", attribution printed before OCI
OLECTRA_Q2FY25 = """                     STATEMENT OF CONSOLIDATED FINANCIAL RESULTS FOR THE QUARTER AND SIX MONTHS ENDED 30 SEPTEMBER 2024
                                                                                                              All amounts in Indian Rupees Lakhs
                                                                      Quarter Ended                   Six Months Ended          Year Ended
SI.
                            Particulars                   30.09.2024    30.06.2024   30.09.2023   30.09.2024       30.09.2023       31.03.2024
No
                                                         (Un audited)  (Un audited) (Un audited) (Un audited)   (Un audited)        (Audited)
1                                2                            3             4            5            6                7                8
 1 Income
   (a) Revenue from operations                                                       52,367.48             31,393.67             30,716.26             83,761.15        52,318.40     1,15,413.54
   (b) Other Income                                                                     251.36                305.97                312.10                557.33           432.16        1,16038
   Total Income                                                                      52,618.84             31,699.64             31,028.36             84,318.48        52,750.56     1,16,573.92
 2 Expe nses
   (a) Cost of materials consumed                                                    41,252.33             22,410.35             21,847.29             63,662.68        17,182.61       85.~79.17
   (b) Purchases of stock - in - trade                                                      -                      -                    -                                      -
   (c) Changes in inventories of finished goods, work-in-progress
                                                                                                              576.61                986.74              (1,900.76)          99.71       (3,056.73)
   and stock-in trade                                                               (2,477.37)
   (d) Power & Fuel                                                                    420.31                289.31                 115.50                709.62           238.55          863.84
   (e) Testing & other operating expenses                                            1,411.25              1,150.67               1,463.60              2,561.92         2,241 .22       5,247.85
   (f) Employee Benefit Expenses                                                     2,467.99              1,842.02               1,652.18              4,310.01         3,143.06        6,907.21
   (g) Finance costs                                                                 1,121.28                959.25                 970.19              2,080.53         1,693.89        4,305.32
   (h) Depreciation and amortization expense                                           964.89                907.19                 879.20              1,872.08         1,780.09        3,667.66
   (i) Other Expenses                                                                1,168.06                735.03                 596.66              1,903.09         1,153.01        3,381 .20
   Total Expenses                                                                   46,328.74             28,870.43              28,511.36             75,199.17        47,532.14     1,06,795.52
   Profil/(loss) before share of profil/(Ioss) of associates,
 3                                                                                    6,290.10              2,829.21              2,517.00              9,119.31         5,218.42        9,778.40
   exceptional items and tax (1 - 2)
 4 Share of profit/ (loss) of associates                                                192.71                355.67                139.91                548.38            (35.55)       799.36
 5 Profil/(loss) before exceptional items and tax ( 3 + 4)                            6,48281               3,184.88              2,656.91              9,667.69         5,182.87      10,577.76
 6 Exceptional items                                                                        -                      -                    -                     -               -               -
 7 Profit/ (loss) before tax ( 5 - 6)                                                 6,482.81              3,184.88              2,656.91              9,667.69         5,182.87      10,577.76
 8 Tax Expense:
   (a) Current tax                                                                    1,757.81                789.38                592.70              2,547.19         1,363.07        2,563.37
   (b) Deferred Tax charge/ (credit)                                                     (40.29)              (29.82)               206.29                 (70.11)         154.64          148.97
   Total Tax Expense                                                                  1,717.52                759.56                798.99              2,477.08         1,517.71        2,712.34
 9 Ne t Profit after tax ( 7 - 8)                                                     4,765.29              2,425.32              1,857.92              7,190.61         3,665.16        7,865.42
10 Profit/ (Loss) attributable to non controlling interest                                 9.11                25.67                 51 .98                 34.78           51 .50         182.08
11 Profil/(Loss) attributable to equity holders of the Parent                         4,756.18              2,399.65              1,805.94              7,155.83         3,613.66        7,683.34
      O ther Comprehensive income - not reclassifiable to P&L
12
      ( net of tax)
                                                                                            -                      -                    -                     -                             17.93

13 Total Comprehensive income ( 9 + 12)                                               4,765.29              2,425.32              1,857.92              7,190.61         3,665.16        7,883.35
   Total comprehensive income attributable to non controlling
14                                                                                                              25.67                51.98                  34.78           51.50         182.08
   interest                                                                               9.11
   Total comprehensive income attributable to equity holders of
15                                                                                    4,756.18              2,399.65              1,805.94              7,155.83         3,613.66        7,701 .27
   the Parent (13 - 14)
16 Paid up equity share capital (Face value of Rs.4/- each)                           3,283.23              3,283.23              3,283.23              3,283.23         3,283.23       3,283.23
17 Other equity                                                                                                                                                                        88,064.53
18
"""


def test_owners_profit_after_oci_and_across_a_page_break():
    """Also: a loss-maker's "Loss for the period" is its PAT."""
    rows, _ = parse(BOROSIL_Q1FY25, when=datetime(2024, 8, 9, 20, 0, tzinfo=IST))
    c = Scope.CONSOLIDATED
    assert val(rows, Metric.PAT_ATTRIBUTABLE, date(2024, 6, 30), scope=c) == pytest.approx(-12.9628)
    assert val(rows, Metric.PAT_ATTRIBUTABLE, date(2024, 3, 31), scope=c) == pytest.approx(-48.0722)
    assert val(rows, Metric.PAT_ATTRIBUTABLE, date(2024, 3, 31), "FY", scope=c) == pytest.approx(-46.8954)
    assert val(rows, Metric.NCI_PROFIT, date(2024, 6, 30), scope=c) == pytest.approx(-1.2752)
    # owners + non-controlling interest = profit for the period
    pat = val(rows, Metric.PAT, date(2024, 6, 30), scope=c)
    assert pat == pytest.approx(-14.238)
    assert val(rows, Metric.PAT_ATTRIBUTABLE, date(2024, 6, 30), scope=c) + val(
        rows, Metric.NCI_PROFIT, date(2024, 6, 30), scope=c) == pytest.approx(pat)


def test_owners_share_of_comprehensive_income_is_never_profit():
    rows, _ = parse(BOROSIL_Q1FY25, when=datetime(2024, 8, 9, 20, 0, tzinfo=IST))
    got = {round(r.value, 4) for r in rows if r.metric == Metric.PAT_ATTRIBUTABLE}
    assert got and not got & {-1.0513, -14.0141, -50.2592, -2.187}      # OCI / TCI owners rows


def test_scanned_profil_and_tci_rows_naming_equity_holders():
    rows, _ = parse(OLECTRA_Q2FY25, when=datetime(2024, 10, 24, 20, 0, tzinfo=IST))
    c = Scope.CONSOLIDATED
    assert val(rows, Metric.PAT_ATTRIBUTABLE, date(2024, 9, 30), scope=c) == pytest.approx(47.5618)
    # year: profit to owners 7,683.34 lakh, not total comprehensive income to owners 7,701.27
    assert val(rows, Metric.PAT_ATTRIBUTABLE, date(2024, 3, 31), "FY", scope=c) == pytest.approx(76.8334)


def test_a_chunk_far_from_the_previous_table_does_not_inherit_its_columns():
    prose = "The Board reviewed the results. " * 20      # > 300 characters of prose in between
    text = BOROSIL_Q1FY25.replace(FF + "12 Profit/(Loss)", prose + "\n" + FF + "12 Profit/(Loss)")
    assert text != BOROSIL_Q1FY25
    rows, _ = parse(text, when=datetime(2024, 8, 9, 20, 0, tzinfo=IST))
    assert val(rows, Metric.PAT_ATTRIBUTABLE, date(2024, 6, 30), scope=Scope.CONSOLIDATED) is None
