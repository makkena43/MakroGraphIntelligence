# Dated constraint ledger

The stock selector has two different jobs:

1. The beneficiary mapper discovers chains and possible company roles.
2. The dated constraint ledger records whether a specific chain had an
   observable supply shortage at a particular point in time.

The second job is deliberately stricter. A policy announcement, a thematic
document burst, or a broad electronics import number can put a chain on a
research list, but cannot by itself create a physical-quality grade or a Buy.

## Constraint-detection denominator

The selector does not let the beneficiary mapper define which constraints can
exist. Its research universe is the union of:

1. mapper product chains (company-discovery route);
2. dated capacity-gap and import-dependence components (physical-discovery
   route); and
3. reviewed `EXACT` product aliases attached to a dated physical ledger record
   (ledger-discovery route).

A chain found only through the second or third route is shown as
`MAP_COMPANIES`: it may be a real physical shortage, but it has no position or
company authority until an exact listed-company producer is independently
mapped. Broad, family, and policy aliases are deliberately excluded from this
route. This makes a missing mapper association a visible research gap instead
of evidence that no constraint exists.

The upstream bridge is materialized before the selector in
`mg_constraint_candidates` and `mg_constraint_company_candidates`. The first
table records the complete point-in-time product denominator, evidence legs,
research priority, mechanism and missing proof. The second records literal
same-product issuer roles without calling them beneficiaries. Theme detection
and the selector consume these snapshots; they do not rescan the corpus or
infer an adjacent product at report time.

The standard India run order is:

```text
ingestion -> NLP product/mechanism signals -> company-product roles
-> capacity/import/policy/tender/order evidence -> constraint candidates
-> observation review queue -> themes -> selector
```

`run_india_post_nlp.py` and the live `IntelligencePipeline.run_full()` execute
the company-role and candidate stages in this order. The observation collector
remains one-way: automated rows are review tasks and never ledger evidence.

Physical reference rows are admissible only when their own `as_of_date` is on
or before the report date. Database `created_at` timestamps are not a
historical-data substitute: a later refreshed row cannot be used in an earlier
replay merely because it was inserted earlier. The report also records
independent current source families; multiple facts from the same release count
as one source family, not multiple confirmations.

## State model

`DISCOVERY` means a candidate chain needs primary evidence. `EVIDENCED` means
a dated source describes the chain but lacks a current numerical physical
measure. `MEASURED` has an exact-chain import or capacity-gap measurement.
`BINDING` and `INVESTIBLE` additionally require current demand and a resupply
barrier; their distinction is documented review judgement, not an automatic
score. `RESOLVING`, `RESOLVED`, and `REJECTED` preserve the history rather than
silently deleting a chain that has stopped appearing in the mapper.

The selector uses a measurement only when all of the following are true:

- the record is `PHYSICAL_CONSTRAINT`;
- it is an `EXACT` product match, rather than a family or policy crosswalk;
- its latest source is no more than 365 days old and its review date is not
  overdue; and
- the measurement itself is dated no more than 365 days before the report
  date (a new policy announcement cannot refresh an old import statistic); and
- its state is `MEASURED`, `BINDING`, or `INVESTIBLE`.

This can fill a missing physical-evidence leg, but it does not change the
existing company-role, order-book, concentration, governance, or entry gates.

## Bootstrap records

The first audit seed contains six historical snapshots and eight primary-source
observations:

- CRGO electrical steel — an old exact-chain import/supply observation;
- defence indigenisation — policy/procurement context only;
- solar cells — exact-chain, unquantified import-dependence context;
- advanced chemistry cells — exact-chain measured 100% import dependence in
  May 2021;
- semiconductors — policy/barrier context only; and
- electronic components — broad import measurement, explicitly not PCB proof.

These records expire into a revalidation requirement. Thus, for a 31-Dec-2022
replay, their existence does not manufacture a live physical constraint. That
is intentional: absence of refreshed evidence is surfaced as missing work, not
mistaken for either resolution or an evergreen Buy signal.

The audited refresh packet at
[`data/reference/constraint_ledger/IN_audited_refresh_2022_2026.json`](../data/reference/constraint_ledger/IN_audited_refresh_2022_2026.json)
adds the first state transitions: solar cells remain evidenced but unmeasured;
solar modules move from a supply-response record to `RESOLVED`; ACC has an
announced-capacity path but only 1 GWh installed by February 2026; and CRGO has
a current technology-barrier observation. Grid equipment is intentionally
stored as a `DEMAND_THEME`, not as a transformer shortage.

## Updating the ledger

Run the audited bootstrap once:

```bash
python3 scripts/policy/seed_constraint_ledger.py
```

For new research, prepare a reviewed JSON packet and validate it first:

```bash
python3 scripts/policy/seed_constraint_ledger.py --input reviewed_packet.json --dry-run
python3 scripts/policy/seed_constraint_ledger.py --input reviewed_packet.json
```

To reproduce the audited refresh:

```bash
python3 scripts/policy/seed_constraint_ledger.py --input data/reference/constraint_ledger/IN_audited_refresh_2022_2026.json
```

The packet contains `{"snapshots": [...]}`. Each snapshot must supply every
ledger field except `country` in `LEDGER_COLUMNS` in
[`seed_constraint_ledger.py`](../scripts/policy/seed_constraint_ledger.py),
plus at least one evidence item. Every evidence item needs a source type,
source date, URL, title, claim and independent-source key. The CLI and a
database trigger both reject a source dated after its snapshot.

For each candidate constraint, add independent demand, supply/import,
barrier, and resupply/resolution evidence. Record a new snapshot when the
state changes; do not overwrite history. Use `FAMILY`, `BROAD`, or `POLICY`
scope where the source does not prove the exact constrained product.

The report’s “Dated constraint ledger” column is the operator queue: `Unlinked`
means research has not created a record, while a stale or review-due record
means its next evidence capture takes priority over a new stock screen.

## Coverage and automated observation queue

The product-to-chain crosswalk is database data, not Python code. The reviewed
India packet lives at
[`data/reference/constraint_ledger/IN_reviewed_product_aliases.json`](../data/reference/constraint_ledger/IN_reviewed_product_aliases.json).
Run the coverage bootstrap whenever the mapper product universe changes:

```bash
python3 scripts/policy/bootstrap_constraint_coverage.py --dry-run
python3 scripts/policy/bootstrap_constraint_coverage.py
```

It writes every current mapper label to `mg_constraint_product_aliases`.
Labels not present in the reviewed packet are retained as `AUTO_DISCOVERY`
aliases with an explicitly generated `discovery_*` key. The bootstrap also
creates only a dated `DISCOVERY`/`WATCH` ledger record when a chain has no
record at its first sighting. This is coverage, not evidence.

The collector then makes dated source work systematic across capacity gaps,
import dependencies, linked trade flows, mapper-demand cohorts, and relevant
company filing language on commissioning or lead time:

```bash
python3 scripts/policy/queue_constraint_observations.py --as-of 2026-07-06 --dry-run
python3 scripts/policy/queue_constraint_observations.py --as-of 2026-07-06
```

Rows are written to `mg_constraint_observation_queue` with
`review_status=PENDING`. They are displayed in the report as a count and type
only. The collector never writes `mg_constraint_ledger_evidence`, changes a
ledger state, or supplies a Buy gate. An analyst must inspect the dated source,
deduplicate independent evidence, and add a reviewed ledger packet to promote
an observation.

## Company-originated product roles

`mg_company_product_roles` answers a different question from the theme mapper:
what product does the issuer itself say it makes, installs, integrates, or
supplies? Build it before a selector run:

```bash
python3 scripts/stock_report/company_product_roles.py --as-of 2022-12-31
python3 scripts/stock_report/company_product_roles.py --snapshots 2020-12-31,2021-12-31,2022-12-31
```

For a long historical corpus, deterministic issuer slices can be run or
re-run independently without creating a company allowlist; run every index for
the chosen denominator, for example `--bucket 0/32` through `--bucket 31/32`.
Each slice replaces only its own hash-partition of the as-of snapshot.

The extractor reads only company documents filed on or before the snapshot,
deduplicates repeated uploads on the same filing date, and requires a tight
product-to-role passage. It keeps a weak one-filing claim as `DISCOVERY`; two
non-duplicative dated, product-coupled physical proofs are needed for an
`EVIDENCED` manufacturing role.

The product vocabulary is assembled from manufacturing-grammar product
entities, mapped product labels, and dated aliases. General-purpose spaCy
`PRODUCT` guesses are excluded because they routinely capture auditors,
reporting periods, people, locations, and table captions. A product is attached to an existing
constraint only when a **reviewed `EXACT` alias** says it is the same product.
Family, policy, broad, and auto-discovery aliases never enter the producer or
Buy-gate universe. Unlinked issuer products are rendered in the separate
“Company-originated product discoveries” queue, then the coverage bootstrap
can create a `discovery_*` ledger record for independent demand, import,
capacity, and resupply-barrier research. A direct company product claim is not
itself evidence that the product is a constraint.

After roles and the observation queue are refreshed, materialise the dated
upstream bridge consumed by themes and the selector:

```bash
python3 scripts/policy/snapshot_constraint_candidates.py --as-of 2026-07-06 --country IN
```
