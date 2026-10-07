# Text extraction: what each rupee amount refers to (no LLM)

## Sample

- **Draw:** a seeded random draw (`tools/sample.py`) of 179 sentences from the 22 development issuers' filings. Each sentence contains a rupee amount near an order, capacity or price word, with at most 10 sentences per issuer. The draw never looked at any extractor's output.
- **Labels:** every amount (246 in all) is labelled with what it refers to: `labelled.jsonl`.

  | Label | Meaning | Amounts |
  |---|---|---|
  | `book_level` | the current outstanding order book | 37 |
  | `book_past` | a superseded order-book level (scored as `other`) | 3 |
  | `order_value` | a specific order or contract won | 17 |
  | `inflow_total` | orders received over a period | 13 |
  | `other` | revenue, capex, issue proceeds, tax, pipeline, guidance, parts of a book… | 176 |

- **Who labelled:** I did, and the labels should be reviewed. Where a choice was a judgment call (a product-level or segment-level book, a figure in an analyst's question), the rule is applied consistently: only company-level current figures count.
- **Split:** by issuer (hash of the symbol) into a development half (74 sentences, 103 amounts), used to write and adjust the rules, and a test half (105 sentences, 143 amounts), used only for scoring.
- **Caveat:** I read every sentence while labelling, so the test half is not fully unseen.

## Results on the test half (precision / recall)

| Role | Current regex extractor | spaCy grammar rules | Logistic regression (local, trained on dev half) |
|---|---|---|---|
| Order-book level | 0.67 / **0.12** (2 of 17) | 0.62 / **0.59** (10 of 17) | 0.61 / 0.65 |
| Order value | 1.00 / 0.13 | **0.89 / 0.53** | 1.00 / 0.07 |
| Inflow total | 0.56 / 0.71 | **0.80 / 0.57** | 0.80 / 0.57 |

Leave-one-issuer-out over all 246 amounts:

| Role | spaCy grammar rules | Logistic regression |
|---|---|---|
| Order-book level | 0.77 / 0.73 | 0.70 / 0.70 |
| Order value | 0.83 / 0.59 | 0.75 / 0.35 |

## What this shows

1. **The current regex extractor misses most order-book statements.** It found 2 of the 17 in the test half.
   - This is a larger problem than the misreads fixed one at a time in D5, D6 and D15: most of the evidence never reaches the detector at all.
2. **Grammar-based rules (spaCy dependency parse) generalise better than word lists.** They raise order-book recall from 0.12 to 0.59 at similar precision, and order-value recall from 0.13 to 0.53.
   - **Method:** each amount is tied to its clause; it is a book level when its clause is about the order book and its verb is a state ("stands at", "exceeds") rather than a flow ("added", "retired").
   - **Checks on nearby words:** future, past and part-of wording is judged only in the words around the amount, not in the whole sentence.
   - **Overfitting:** the rules still score lower on the test half (0.62 / 0.59) than on the development half (0.89 / 0.85) they were written on.
3. **A local classifier does not beat the rules yet.**
   - 103 training amounts are too few; only 2 order values were in the development half.
   - Roughly 1,000 labelled amounts would be needed; more labelling can be done in the same way.
   - No paid API is needed at any point.

## In the code

- **Module:** `src/makrograph/earnings_inflection/amount_roles.py` implements the grammar rules. spaCy and `en_core_web_sm` are optional dependencies.
- **Not yet used:** the module is not wired into catalyst detection. Under the numbers-first design, text extraction supports numeric signals; wiring it in comes after the held-out cohort result for the numeric detector.

Tools (`tools/`): `sample.py` (draw), `score.py` (regex versus grammar rules), `classify.py` (local classifier). Run them from the repository root with `PYTHONPATH=src`.
