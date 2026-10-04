"""Generate the synthetic offline fixture set (fictional issuers only).

    python tests/earnings_inflection/fixtures/build_fixtures.py

Writes ``sample_in.json`` next to this file.  Companies, counterparties and
numbers are invented; they are NOT chosen to resemble any historical winner.
"""

import json
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent


def qend(y, m):
    return date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m])


def prev_q(d):
    return {6: qend(d.year, 3), 9: qend(d.year, 6), 12: qend(d.year, 9), 3: qend(d.year - 1, 12)}[d.month]


def yago(d):
    return date(d.year - 1, d.month, d.day)


def fy_end_before(d):
    return date(d.year, 3, 31) if d.month > 3 else date(d.year - 1, 3, 31)


def quarter_pl(rev, margin, shares=10.0, minority=0.05, oi=2.0, da=5.0, fin=3.0, tax_rate=0.25):
    ebitda = rev * margin
    te = rev - ebitda + da + fin
    pbt = rev + oi - te
    tax = pbt * tax_rate
    pat = pbt - tax
    attr = pat * (1 - minority)
    return {"rev": rev, "oi": oi, "te": te, "da": da, "fin": fin, "pbt": pbt, "tax": tax, "pat": pat,
            "attr": attr, "eps": attr / shares}


def sum_pl(rows):
    out = {k: sum(r[k] for r in rows) for k in rows[0]}
    return out


def fmt(d):
    return d.strftime("%d.%m.%Y")


def results_text(company, q, quarters, scope="Consolidated"):
    cols = [q, prev_q(q), yago(q)]
    fy_cols = [q, yago(q)] if q.month == 3 else [fy_end_before(q)]

    def fy(d):
        ends = [qend(d.year - 1, 6), qend(d.year - 1, 9), qend(d.year - 1, 12), d]
        return sum_pl([quarters[e] for e in ends])

    data = [quarters[c] for c in cols] + [fy(c) for c in fy_cols]
    header_dates = "   ".join(fmt(c) for c in cols + fy_cols)
    lines = [
        company.upper(),
        f"Statement of {scope} Unaudited Financial Results for the quarter ended {q.strftime('%d %B, %Y')}",
        "(Rs. in crore)",
        "Particulars                          Quarter ended                      Year ended",
        f"                          {header_dates}",
        "                          Unaudited    Unaudited    Unaudited    Audited",
    ]

    def row(label, key, nd=2):
        vals = "   ".join(f"{d[key]:,.{nd}f}" for d in data)
        return f"{label:<36}{vals}"

    lines += [
        row("1. Revenue from operations", "rev"),
        row("2. Other income", "oi"),
        row("4. Total expenses", "te"),
        row("   Depreciation and amortisation", "da"),
        row("   Finance costs", "fin"),
        row("5. Profit before tax", "pbt"),
        row("6. Tax expense", "tax"),
        row("7. Profit for the period", "pat"),
        row("   - Owners of the Company", "attr"),
        "8. Earnings per share (face value Rs 10 each)",
        row("   (a) Basic", "eps"),
        row("   (b) Diluted", "eps"),
        "",
        "Notes: Previous period figures have been regrouped wherever necessary.",
    ]
    return "\n".join(lines)


def build():
    docs, issuers = [], {}

    # ---------------- ACMEGRID: commitment -> execution -----------------
    issuers["ACMEGRID"] = {"name": "Acme Grid Equipment Limited", "industry": "Electrical Equipment", "series": "EQ"}
    revs = {qend(2022, 6): 100, qend(2022, 9): 105, qend(2022, 12): 110, qend(2023, 3): 120,
            qend(2023, 6): 115, qend(2023, 9): 125, qend(2023, 12): 135, qend(2024, 3): 160,
            qend(2024, 6): 165, qend(2024, 9): 190}
    margins = {qend(2022, 6): .11, qend(2022, 9): .11, qend(2022, 12): .115, qend(2023, 3): .12,
               qend(2023, 6): .12, qend(2023, 9): .125, qend(2023, 12): .13, qend(2024, 3): .15,
               qend(2024, 6): .16, qend(2024, 9): .17}
    acme = {d: quarter_pl(r, margins[d]) for d, r in revs.items()}
    pub = {qend(2023, 6): "2023-08-09T16:30:00+05:30", qend(2023, 9): "2023-11-08T16:30:00+05:30",
           qend(2023, 12): "2024-02-07T16:30:00+05:30", qend(2024, 3): "2024-05-15T17:10:00+05:30",
           qend(2024, 6): "2024-08-08T17:45:00+05:30", qend(2024, 9): "2024-11-10T18:00:00+05:30"}
    for q, ts in pub.items():
        docs.append({"doc_id": f"ACME-R-{q.isoformat()}", "ticker": "ACMEGRID", "source_name": "nse",
                     "doc_type": "announcement", "filing_type": "Financial Results",
                     "title": f"Financial results for quarter ended {q}", "company": "Acme Grid Equipment Limited",
                     "published_at": ts, "text": results_text("Acme Grid Equipment Limited", q, acme)})

    docs.append({"doc_id": "ACME-ORD-1", "ticker": "ACMEGRID", "source_name": "bse", "doc_type": "announcement",
                 "filing_type": "order_win", "title": "Intimation of receipt of order",
                 "published_at": "2024-02-10T11:05:00+05:30",
                 "text": ("Sub: Intimation of receipt of order\n\nWe wish to inform that Acme Grid Equipment Limited has "
                          "received a purchase order worth Rs 450 crore from Northern Grid Corporation Limited for "
                          "supply of 220 kV power transformers in the transmission segment. The order is to be "
                          "executed over 18 months.")})
    docs.append({"doc_id": "ACME-INV-1", "ticker": "ACMEGRID", "source_name": "nse", "doc_type": "announcement",
                 "filing_type": "Analysts/Institutional Investor Meet/Con. Call Updates",
                 "title": "Earnings conference call transcript Q4FY24",   # misleading title on purpose
                 "published_at": "2024-05-10T10:00:00+05:30",
                 "text": ("Acme Grid Equipment Limited invites you to its Q4 FY24 earnings conference call on "
                          "May 20, 2024 at 4:00 PM IST. Universal access number: +91 22 0000 0000. "
                          "Pre-registration via DiamondPass is available.")})
    docs.append({"doc_id": "ACME-TR-Q4FY24", "ticker": "ACMEGRID", "source_name": "nse", "doc_type": "announcement",
                 "filing_type": "Transcript", "title": "Transcript of earnings call",
                 "published_at": "2024-05-24T19:00:00+05:30",
                 "text": "\n".join([
                     "Moderator: Ladies and gentlemen, good day and welcome to the Q4 FY24 earnings conference call of Acme Grid Equipment Limited.",
                     "Ravi Kumar: Thank you. FY24 was a strong year for us.",
                     "Ravi Kumar: During the quarter we received a purchase order worth Rs 450 crore from Northern Grid Corporation Limited for 220 kV transformers.",
                     "Ravi Kumar: Our order book stands at Rs 1,400 crore as of March 31, 2024.",
                     "Ravi Kumar: Capacity utilisation was 82% in FY24.",
                     "Ravi Kumar: We expect revenue growth of 25-30% in FY25.",
                     "Ravi Kumar: We expect EBITDA margin of 15-16% in FY25.",
                     "Ravi Kumar: We plan capex of Rs 120 crore over FY25 and FY26, funded through internal accruals.",
                     "Moderator: Thank you. The first question is from Anil of Sample Securities.",
                     "Anil: Is there any pricing pressure?",
                     "Ravi Kumar: We do not expect any material pricing pressure this year.",
                     "Moderator: That was the last question. I now hand the conference over to the management.",
                 ])})
    docs.append({"doc_id": "ACME-ORD-2", "ticker": "ACMEGRID", "source_name": "bse", "doc_type": "announcement",
                 "filing_type": "order_win", "title": "Receipt of repeat order",
                 "published_at": "2024-06-15T12:00:00+05:30",
                 "text": ("Acme Grid Equipment Limited has received a repeat purchase order worth Rs 450 crore from "
                          "Northern Grid Corporation Limited for 400 kV transformers.")})
    docs.append({"doc_id": "ACME-TR-Q1FY25", "ticker": "ACMEGRID", "source_name": "nse", "doc_type": "announcement",
                 "filing_type": "Transcript", "title": "Transcript Q1FY25", "published_at": "2024-08-16T19:00:00+05:30",
                 "text": "\n".join([
                     "Moderator: Ladies and gentlemen, welcome to the Q1 FY25 earnings call of Acme Grid Equipment Limited.",
                     "Ravi Kumar: In June we received a repeat purchase order worth Rs 450 crore from Northern Grid Corporation Limited.",
                     "Ravi Kumar: We have also received a letter of intent for Rs 200 crore from a leading European OEM.",
                     "Ravi Kumar: Capacity utilisation was 93% in Q1FY25.",
                     "Ravi Kumar: We reiterate that we expect revenue growth of 25-30% in FY25.",
                     "Moderator: The first question is from Meera of Example Capital.",
                     "Meera: How is working capital?",
                     "Ravi Kumar: Working capital is stretched due to advance-free orders but cash from operations remained positive.",
                     "Moderator: Thank you. I now hand the conference over to the management.",
                 ])})
    docs.append({"doc_id": "ACME-NO-TIME", "ticker": "ACMEGRID", "source_name": "bse", "doc_type": "announcement",
                 "title": "Undated disclosure", "text": "Acme Grid Equipment Limited received an order worth Rs 900 crore."})

    # ---------------- PLAINCO: silent management, realized change only ------------
    issuers["PLAINCO"] = {"name": "Plain Components Limited", "industry": "Auto Components", "series": "EQ"}
    prevs = {qend(2022, 6): 200, qend(2022, 9): 200, qend(2022, 12): 205, qend(2023, 3): 210,
             qend(2023, 6): 205, qend(2023, 9): 210, qend(2023, 12): 212, qend(2024, 3): 214, qend(2024, 6): 210}
    plain = {d: quarter_pl(r, .14, shares=20) for d, r in prevs.items()}
    for q, ts in {qend(2023, 6): "2023-08-10T15:00:00+05:30", qend(2023, 9): "2023-11-09T15:00:00+05:30",
                  qend(2023, 12): "2024-02-09T15:00:00+05:30", qend(2024, 3): "2024-05-20T15:00:00+05:30",
                  qend(2024, 6): None}.items():
        d = {"doc_id": f"PLAIN-R-{q.isoformat()}", "ticker": "PLAINCO", "source_name": "bse",
             "doc_type": "announcement", "title": "Outcome of board meeting - results",
             "text": results_text("Plain Components Limited", q, plain)}
        if ts:
            d["published_at"] = ts
        else:
            d["filed_at"] = "2024-08-12"     # date only -> end of day IST
        docs.append(d)

    # ---------------- CONTRACO: guidance missed -> contradicted ---------------
    issuers["CONTRACO"] = {"name": "Contra Industries Limited", "industry": "Capital Goods", "series": "EQ"}
    crevs = {qend(2022, 6): 300, qend(2022, 9): 300, qend(2022, 12): 300, qend(2023, 3): 300,
             qend(2023, 6): 310, qend(2023, 9): 315, qend(2023, 12): 315, qend(2024, 3): 320}
    contra = {d: quarter_pl(r, .10, shares=15) for d, r in crevs.items()}
    docs.append({"doc_id": "CONTRA-TR-Q4FY23", "ticker": "CONTRACO", "source_name": "nse",
                 "title": "Transcript Q4FY23", "published_at": "2023-05-25T19:00:00+05:30",
                 "text": "\n".join([
                     "Moderator: Ladies and gentlemen, welcome to the Q4 FY23 call of Contra Industries Limited.",
                     "Sunil Rao: We expect revenue growth of 30% in FY24 on the back of new capacity.",
                     "Moderator: The first question is from Dev of Demo Research.",
                     "Dev: What about margins?",
                     "Sunil Rao: Margins should be stable.",
                     "Moderator: I now hand the conference over to the management.",
                 ])})
    for q, ts in {qend(2023, 12): "2024-02-12T16:00:00+05:30", qend(2024, 3): "2024-05-28T16:00:00+05:30"}.items():
        docs.append({"doc_id": f"CONTRA-R-{q.isoformat()}", "ticker": "CONTRACO", "source_name": "nse",
                     "title": "Financial Results", "published_at": ts,
                     "text": results_text("Contra Industries Limited", q, contra)})

    # ---------------- SAMPLEBANK: unsupported financial model ---------------
    issuers["SAMPLEBANK"] = {"name": "Sample Cooperative Bank Limited", "industry": "Private Sector Bank", "series": "EQ"}
    docs.append({"doc_id": "BANK-R-1", "ticker": "SAMPLEBANK", "source_name": "nse", "title": "Financial Results",
                 "published_at": "2024-07-20T16:00:00+05:30",
                 "text": ("SAMPLE COOPERATIVE BANK LIMITED\nStatement of Standalone Unaudited Financial Results for the "
                          "quarter ended 30 June, 2024\n(Rs. in crore)\nInterest earned on advances to customers grew "
                          "to Rs 820 crore. Gross NPA stood at 2.1%. We expect loan growth of 18% in FY25.")})
    return {"issuers": issuers, "documents": docs}


if __name__ == "__main__":
    (HERE / "sample_in.json").write_text(json.dumps(build(), indent=1))
    print("wrote", HERE / "sample_in.json")
