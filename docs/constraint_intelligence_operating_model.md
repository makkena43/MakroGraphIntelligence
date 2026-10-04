# Constraint Intelligence Operating Model

## Mission

Find an economic discontinuity while it is still under-researched, identify
the listed companies that can capture the economics, and make the decision
replayable as of the date it was made.  This is not a theme-news or
keyword-ranking product.

## Three distinct opportunity mechanisms

Not every exceptional return is caused by a physical shortage.  The system
must label the mechanism before it can rank a company:

| Mechanism | What must be proved | Company capture mechanism |
| --- | --- | --- |
| Physical constraint rent | Quantified supply/import gap, binding demand, resupply or qualification barrier, and resolution clock | Existing producer can price, obtain allocation, or capture scarce orders |
| Localisation / qualification regime | Import dependence or foreign concentration, a rule or qualification barrier, and domestic substitution timing | Domestic approved maker replaces imports or wins protected procurement |
| Deployment demand pull | Dated policy/tender/financing trigger, real adoption demand, and limited qualified delivery capacity | Direct maker, integrator, or supplier converts order flow into revenue and cash flow |

The last two are valid investment mechanisms, but they must never be presented
as a physical shortage.  A policy reference alone proves none of them.

## Research sequence

1. **Scout** — collect candidates from company filings, official policy and
   regulator releases, customs data, tenders, and capacity/commissioning data.
   A machine discovery is a work item, never a company recommendation.
2. **Define the product layer** — identify the literal product, value-chain
   position, unit of measurement, and potential substitutes.  An adjacent
   input, EPC contractor, buyer, or theme mention fails this step.
3. **Prove the mechanism** — build a dated evidence packet.  For a physical
   constraint it contains demand, supply/import, barrier, and resolution legs.
   For localisation or deployment it contains the corresponding policy,
   qualification, and order/adoption legs.
4. **Map companies** — prove the issuer's literal role with its own filings
   and a second independent source where practical.  Distinguish operating
   capacity from a plan, and production from installation or end use.
5. **Prove earnings capture** — establish how the product affects revenue,
   margin, cash conversion, working capital, and the likely duration of the
   advantage.  A maker is not automatically a beneficiary.
6. **Underwrite** — apply governance, balance-sheet, liquidity, valuation,
   dilution, and position-sizing review.  This is where an attractive thesis
   can still be rejected.
7. **Monitor and close** — record the stated falsifier, resolution milestones,
   results, and realised outcome.  The outcome log improves calibration but
   never rewrites an old decision.

## Machine pipeline contract

The selector is the last consumer, not the detector. The production sequence
is now explicit and versioned:

1. **Ingest dated sources** — exchange filings, primary policy/regulator
   releases, customs/trade data, tenders, capacity and commissioning records.
2. **Extract product and mechanism language** — NLP retains literal product
   phrases plus separate demand, binding, lead-time, qualification, import and
   commissioning signals. Several mechanism types in one passage remain
   separate observations; deduplication is within signal type, not across it.
   General NER `PRODUCT` guesses are not admitted to the physical-product
   vocabulary. A product must come from manufacturing grammar, a reviewed
   alias/reference, or another explicitly dated product source.
3. **Build issuer product roles** — a dated own-filing snapshot distinguishes
   operating manufacturer, planned capacity, installer/integrator and input
   supplier. This stage precedes constraint scoring.
4. **Build the constraint denominator** — `mg_constraint_candidates` unions
   reviewed/automatic product aliases, capacity/import references, ledger
   evidence, policy-product NLP, accepted observation packets and issuer roles.
   The theme mapper is not allowed to define this universe.
5. **Score research urgency** — demand, measured supply/import, binding stress,
   resupply barrier, source diversity and resolution evidence are separate
   legs. Reuploads from one issuer on one date count once, and a grade-A chain
   needs evidence on at least two independent dates. An unmeasured chain cannot
   enter the measured `INVESTIGATE_NOW` state. The score orders analyst work
   only and never creates investment authority.
6. **Map corresponding companies** — `mg_constraint_company_candidates` stores
   literal same-product roles and their review state. A capability hit is a
   lead; an approved operating maker is stronger role evidence; both still
   require product-level earnings capture.
7. **Create themes and select stocks** — theme detection consumes the candidate
   snapshot even when company count is zero. The selector independently applies
   physical-quality, company-capture, risk, valuation and position gates.

Every historical run passes one `as_of_date` through all seven stages. Bundled
reference figures carry an explicit availability date and are excluded from
earlier replays.

### Policy-first coverage rule

A broad policy scan must not be restricted to names already present in the
theme mapper.  Each dated policy snapshot therefore also stores valid product
phrases extracted from the policy-bearing company filings.  Those phrases feed
the issuer-product role extractor on the next pass.  A one-company policy
product phrase is allowed into this *research vocabulary* because it is a
genuine early-discovery case; it still has to pass the issuer-owned role,
mechanism, earnings-capture, risk, and analyst-review gates before it can
influence a decision.  This catches a new product layer without creating a
company or theme allowlist.

## Decision lanes

| Lane | Evidence threshold | Portfolio action |
| --- | --- | --- |
| Core | Fully measured mechanism, operating company role, earnings capture, and risk gates passed | Candidate for normal portfolio underwriting |
| Timing — physical constraint | Grade A/B measured physical constraint, dated **exact company-product** operating role, company catalyst, and a named confirmation milestone | Small, staged position only after separate underwriting |
| Policy / localisation research | Dated deployment or qualification mechanism plus an exact company role, but physical scarcity may be unmeasured | Research only until its separately tested decision lane is approved |
| Research | A missing product, company role, evidence leg, or earnings link | No investment action |
| Reject / archive | Wrong layer, resolved mechanism, failed economics, or governance veto | No action; retain the reason |

The selector does not make a purchase decision.  Its highest mechanical state
is an **investment-committee hand-off**, which requires fresh,
product-specific operating-maker evidence. Older mapper corroboration can
create a Core underwriting research candidate. A dated exact mapper
corroboration remains useful research evidence but cannot support a physical-
constraint Timing position by itself. That lane also needs a measured A/B
constraint and fresh product-specific operating-maker proof.

## Non-negotiable point-in-time controls

- Every source date must be on or before the report date.
- A human role approval records the date it became available; an approval made
  today cannot alter a 2022 backtest.
- Derived extraction versions are explicit.  When precision rules change, old
  extractions are rebuilt rather than silently reinterpreted.
- Policy, repeated press releases, or mapped-company counts cannot substitute
  for a physical data leg or an earnings-capture proof.
- Backtests compare overlapping cohorts transparently, use a point-in-time
  benchmark, publish dispersion and severe-loss rate, and are never used as a
  gate for a stock that was added retrospectively.

## Operating cadence

Each month: refresh primary data, run the broad company-policy and novel-policy
discovery scans into dated research snapshots, triage the company-role and
constraint-evidence queues, complete a small number of full evidence packets,
materialise `mg_constraint_candidates` / `mg_constraint_company_candidates`,
and publish only the changes and invalidations. Individual selector runs read
those snapshots plus a fast visible-universe fallback; they never wait to
rescan every filing. Each quarter:
update company capture proofs and risk underwriting. Each year: replay frozen
decisions and audit missed winners, false positives, and the reason every miss
occurred.

The scarce resource in this business is analyst attention.  The product should
therefore optimise for a small, explicit queue of high-value questions—not a
long list of stocks with superficially high scores.
