# Guidance-to-Execution Radar

This is a separate investment process. It does not read stock-selector ranks,
constraint scores, themes, PLI screens, or beneficiary mappings.

Its question is narrower: did an issuer make a large, quantified earnings
promise; is independent operating evidence accumulating; has management kept
the same story; is the balance sheet supporting the promise; is valuation still
reasonable; and has the share price already discounted too much of the result?

## Decision contract

Every run is point-in-time. A source is usable only when `filed_at <= as_of_date`.
Financial data is usable only when `available_at <= as_of_date`. Backtests enter
at the first trading-session close strictly after the disclosure date.

The final action has one meaning:

- `ACCUMULATE`: execution is realized, valuation is known, no veto exists; maximum model position 3%.
- `STARTER`: large formal guidance plus at least two independent dated proof events and known valuation; maximum model position 1%.
- `HOLD_NO_CHASE`: the company clears fundamental gates but the 6/12-month price move breaches the chase limit; no new model position.
- `WATCH`: the thesis may be attractive but one or more named gates are missing; no position authority.
- `REJECT`: no actionable guidance-and-proof chain exists.
- `THESIS_BROKEN`: a hard governance veto overrides every growth score.

Position percentages are research-model caps, not personal financial advice.

## What the LLM may and may not do

The LLM extracts structured promises, realized evidence, financial snapshots,
risks, and narrative tags from compacted official disclosures. Every extracted
claim must carry a document id and verbatim quote. Code verifies that quote
against the source and drops unsupported output.

The LLM never computes returns, chooses an action, changes a threshold, fills a
missing value, or treats an announced factory as commissioned. Dates, evidence
deduplication, valuation arithmetic, risk vetoes, actions, and walk-forward
returns are deterministic.

Repeated press releases on one date count as one evidence leg. Promotional
repetition does not increase conviction.

## Running it

Single dated market scan (LLM calls only after a cheap SQL triage):

```bash
python3 scripts/guidance_radar.py --as-of 2026-09-10 --use-llm
```

Focused audit without persistence:

```bash
python3 scripts/guidance_radar.py \
  --as-of 2023-12-31 \
  --tickers 'GVT&D,PGEL,SHAKTIPUMP,TARIL,TEMBO,WEBELSOLAR' \
  --use-llm --no-persist
```

For blind walk-forward work, freeze a JSON case file before changing rules:

```json
[
  {"ticker": "EXAMPLE1", "as_of_date": "2022-12-31"},
  {"ticker": "EXAMPLE2", "as_of_date": "2023-06-30"}
]
```

Then run:

```bash
python3 scripts/research/guidance_radar_walkforward.py \
  --cases /absolute/path/to/frozen_cases.json \
  --through 2026-07-03 --use-llm \
  --output output/guidance_radar_walkforward.json
```

The result reports 12/24/36-month and through-date returns, median rather than
only mean performance, hit rate, multibagger rate, severe-loss rate, and alpha
against a labelled ETF proxy. A raw price series with a corporate-action-sized
one-day discontinuity is excluded instead of silently producing a false return.

## Validation discipline

A useful evaluation cohort must include:

1. Winners the process should have found.
2. Companies with impressive guidance that later missed.
3. Governance failures that appeared attractive before the veto.
4. Companies already too extended when proof arrived.
5. Delisted and illiquid names where data is available.

Keep rule-development and locked holdout case files separate. Do not change a
threshold after viewing holdout returns. Production promotion requires adequate
price coverage, positive median benchmark-relative return, a tolerable severe
loss rate, and stable results across more than one market regime.

`tests/fixtures/guidance_radar_development_cases.json` is a regression/development
set, not proof of alpha: several cases were chosen because their later outcomes
are already known. Freeze a separate universe-derived holdout before claiming
performance.
