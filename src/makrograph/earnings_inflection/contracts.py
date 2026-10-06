"""Typed records, enums and output guards for the Earnings Inflection Detector.

RESEARCH-ONLY.  Nothing in this package produces investment actions
(BUY / STARTER / ACCUMULATE), position sizes or portfolio instructions.
States describe *evidence*, never what to do about it.

All records are stdlib dataclasses so the package carries no new runtime
dependency.  ``validate()`` methods raise ``ContractError`` on malformed data.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional

IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA_VERSION = "ei-assessment-5"

RESEARCH_ONLY_NOTICE = (
    "Research-only evidence assessment. Not an investment recommendation, "
    "rating, price target, position size or portfolio instruction. Analytical "
    "states change as evidence changes and confer no investment authority."
)


class ContractError(ValueError):
    """Raised when a record violates its contract."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DocumentKind(str, Enum):
    FINANCIAL_RESULTS = "financial_results"
    ANNUAL_REPORT = "annual_report"
    INVESTOR_PRESENTATION = "investor_presentation"
    EARNINGS_CALL_TRANSCRIPT = "earnings_call_transcript"
    EARNINGS_CALL_INVITATION = "earnings_call_invitation"   # NOT a transcript
    ORDER_ANNOUNCEMENT = "order_announcement"
    CREDIT_RATING_RATIONALE = "credit_rating_rationale"    # ICRA / CARE / CRISIL / India Ratings ...
    OTHER_ANNOUNCEMENT = "other_announcement"
    UNKNOWN = "unknown"


class IssuerModel(str, Enum):
    OPERATING = "operating"
    BANK = "bank"
    INSURER = "insurer"
    INVESTMENT_COMPANY = "investment_company"
    NBFC = "nbfc"
    UNKNOWN = "unknown"

    @property
    def is_financial(self) -> bool:
        return self in (IssuerModel.BANK, IssuerModel.INSURER,
                        IssuerModel.INVESTMENT_COMPANY, IssuerModel.NBFC)


class ListingSegment(str, Enum):
    MAINBOARD = "mainboard"
    SME = "sme"
    UNKNOWN = "unknown"


class EvidenceTier(str, Enum):
    """What kind of claim a piece of evidence is (spec question 2)."""
    MANAGEMENT_ASSERTION = "management_assertion"   # forward-looking statement
    COMMERCIAL_COMMITMENT = "commercial_commitment"  # order / contract / LoA
    REALIZED_EXECUTION = "realized_execution"        # reported, audited/reviewed numbers


class Modality(str, Enum):
    REALIZED = "realized"
    FORWARD = "forward"
    CONDITIONAL = "conditional"
    NEGATED = "negated"


class Scope(str, Enum):
    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"
    SEGMENT = "segment"
    UNKNOWN = "unknown"


class Metric(str, Enum):
    REVENUE = "revenue"
    OTHER_INCOME = "other_income"
    TOTAL_EXPENSES = "total_expenses"
    EBITDA = "ebitda"
    EBITDA_MARGIN = "ebitda_margin"
    DEPRECIATION = "depreciation"
    FINANCE_COST = "finance_cost"
    EXCEPTIONAL_ITEMS = "exceptional_items"
    PBT = "pbt"
    TAX = "tax"
    PAT = "pat"
    PAT_ATTRIBUTABLE = "pat_attributable"
    DILUTED_EPS = "diluted_eps"
    BASIC_EPS = "basic_eps"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    CAPEX = "capex"
    NET_DEBT = "net_debt"
    ORDER_BOOK = "order_book"
    ORDER_INFLOW = "order_inflow"
    ORDER_WIN = "order_win"
    CAPACITY = "capacity"
    UTILIZATION = "utilization"
    REVENUE_GUIDANCE = "revenue_guidance"
    REVENUE_GROWTH_GUIDANCE = "revenue_growth_guidance"
    MARGIN_GUIDANCE = "margin_guidance"
    FUNDRAISE = "fundraise"
    # WP3: integrity / recurring earnings / balance sheet / cash flow / share basis
    PBT_PRE_EXCEPTIONAL = "pbt_pre_exceptional"
    NCI_PROFIT = "nci_profit"
    PAID_UP_CAPITAL = "paid_up_capital"
    FACE_VALUE = "face_value"
    SHARES_FROM_CAPITAL = "shares_from_capital"        # derived: paid-up capital / face value
    INVESTING_CASH_FLOW = "investing_cash_flow"
    FINANCING_CASH_FLOW = "financing_cash_flow"
    NET_CHANGE_IN_CASH = "net_change_in_cash"
    BORROWINGS_NONCURRENT = "borrowings_noncurrent"
    BORROWINGS_CURRENT = "borrowings_current"
    CASH = "cash"
    RECEIVABLES = "receivables"
    INVENTORIES = "inventories"
    TOTAL_ASSETS = "total_assets"
    TOTAL_EQUITY_AND_LIABILITIES = "total_equity_and_liabilities"
    # WP5: mechanism inputs
    COST_OF_MATERIALS = "cost_of_materials"            # statement row: cost of materials consumed
    PURCHASES_STOCK = "purchases_stock_in_trade"
    INVENTORY_CHANGE = "inventory_change"              # changes in inventories (statement sign)
    EQUITY_ISSUED = "equity_issued"                    # cash flow: proceeds from issue of shares / warrants
    SEGMENT_REVENUE = "segment_revenue"                # per-segment rows (FinancialMeasurement.segment)
    SEGMENT_RESULT = "segment_result"
    VOLUME = "volume"                                  # stated units / tonnes / volume growth
    PRICING = "pricing"                                # stated realisations / price changes
    INPUT_COST = "input_cost"                          # stated raw-material / input cost changes
    MIX_SHARE = "mix_share"                            # stated share of a product/segment in revenue
    ACQUISITION = "acquisition"                        # acquisition / inorganic growth mentions
    MARKET_SHARE = "market_share"


class Unit(str, Enum):
    INR_CRORE = "INR_crore"
    USD_MN = "USD_mn"
    PERCENT = "percent"
    BPS = "bps"
    INR_PER_SHARE = "INR_per_share"
    SHARES_CRORE = "shares_crore"
    MW = "MW"
    GW = "GW"
    TONNES_PER_ANNUM = "TPA"
    UNITS = "units"
    RATIO = "ratio"


class CommitmentStrength(str, Enum):
    BINDING = "binding"            # PO, contract signed, LoA/LoI accepted with value
    PROVISIONAL = "provisional"    # L1 bidder, LoI, selected but not awarded
    NON_BINDING = "non_binding"    # MoU, framework, "in discussions"
    NOT_APPLICABLE = "not_applicable"


class EventStage(str, Enum):
    """Dated states of a commercial event (WP4).  Not a single linear sequence:
    an amendment can reduce value; cancellation/expiry reverse a commitment."""
    INQUIRY = "inquiry"                     # enquiries, discussions, pipeline, bids submitted
    MOU_FRAMEWORK = "mou_framework"         # MoU, framework / rate contract (may be unquantified)
    PREFERRED_BIDDER = "preferred_bidder"   # L1, LoI, selected - not yet a firm order
    BINDING_ORDER = "binding_order"         # PO, LoA, signed contract, work order
    EXECUTION = "execution"                 # delivered, commissioned, executed
    AMENDED = "amended"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


COMMITMENT_RANK = {EventStage.INQUIRY: 1, EventStage.MOU_FRAMEWORK: 2, EventStage.PREFERRED_BIDDER: 3,
                   EventStage.BINDING_ORDER: 4, EventStage.EXECUTION: 5}


class RelationshipStatus(str, Enum):
    CONFIRMED_RELATED = "confirmed_related"                       # own subsidiary / group / related party
    ISSUER_ASSERTED_UNRELATED = "issuer_asserted_unrelated"       # issuer says so; not independent
    INDEPENDENTLY_SUPPORTED_UNRELATED = "independently_supported_unrelated"   # attributable external source
    UNKNOWN = "unknown"


class CustomerVerification(str, Enum):
    ANONYMOUS = "anonymous"                 # "a leading OEM": unverified
    ISSUER_NAMED = "issuer_named"           # named by the issuer only
    CORROBORATED = "corroborated"           # dated, attributable external source


class ValueBasis(str, Enum):
    FIRM = "firm"
    CEILING = "ceiling"                     # "up to", framework / rate-contract maximum
    GUARANTEED_MINIMUM = "guaranteed_minimum"
    EXECUTABLE_RELEASE = "executable_release"   # release / call-off order under a framework
    UNQUANTIFIED = "unquantified"


class TaxBasis(str, Enum):
    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"
    UNKNOWN = "unknown"


class Mechanism(str, Enum):
    """How an earnings change is produced (WP5).  Each is assessed on its own evidence."""
    UTILIZATION = "utilization"
    PRODUCT_MIX = "product_customer_mix"
    PRICING_INPUT = "pricing_input_costs"
    ORDER_QUALITY = "order_quality"
    DEBT_REDUCTION = "debt_reduction"
    SEGMENT_TURNAROUND = "segment_turnaround"
    ORGANIC_VOLUME = "organic_volume_share"


class MechanismState(str, Enum):
    INSUFFICIENT_DATA = "insufficient_data"    # the comparison the mechanism needs is not available
    NO_MATERIAL_CHANGE = "no_material_change"  # comparable data, change below the mechanism threshold
    ASSERTION = "assertion"                    # management statements only
    COMMITMENT = "commitment"                  # binding external commitments, not yet realized
    EMERGING = "emerging"                      # one period of realized, comparable change
    CONFIRMED = "confirmed"                    # mechanism-specific persistence / independent milestone
    ADVERSE = "adverse"                        # the mechanism is moving against earnings
    CONTRADICTED = "contradicted"              # evidence invalidates the claimed mechanism


@dataclass
class MechanismResult:
    mechanism: "Mechanism"
    state: "MechanismState"
    direction: str = "neutral"                 # positive | negative | neutral (never "large = good")
    magnitude: Optional[float] = None
    magnitude_unit: str = ""
    magnitude_basis: str = ""                  # what the number measures; "" when unknown
    period_end: Optional[date] = None
    first_signal_at: Optional[datetime] = None  # latest availability of the inputs that first showed it
    durability: str = "unknown"                 # e.g. "2 consecutive quarters", "single statement"
    attribution: str = ""                       # how the change is attributed (observed / issuer-stated / none)
    confidence: str = "low"                     # low | medium | high (evidence quality, not probability)
    invalidators: list[str] = field(default_factory=list)
    overlaps_with: list[str] = field(default_factory=list)   # mechanisms describing the same earnings change
    evidence_ids: list[str] = field(default_factory=list)
    source_doc_ids: list[str] = field(default_factory=list)
    hypothesis: str = ""                        # research question when magnitude cannot be quantified
    notes: list[str] = field(default_factory=list)
    # computed on the latest parsed period although later results should already be public:
    # kept as the last known reading, never used to upgrade the evidence status
    stale: bool = False

    @property
    def qualifies_positive(self) -> bool:
        return (not self.stale and self.direction == "positive"
                and self.state in (MechanismState.EMERGING, MechanismState.CONFIRMED))


@dataclass
class RationaleFact:
    """One fact from a credit-rating rationale.  ``expected`` = the agency reports a plan or a
    forecast (often management's) - context, not evidence that execution happened."""
    field: str                       # capacity / order_book / capex / dscr / ...
    value: Optional[float]
    unit: str
    text: str                        # the value as written, with its qualifier ("as of June 2024")
    expected: bool
    quote: str


@dataclass
class RatingRationale:
    doc_id: str
    agency: str
    published_at: Optional[datetime]
    rationale_date: Optional[date]
    action: str = ""                 # upgraded / downgraded / reaffirmed / assigned / withdrawn / watch
    long_term_rating: str = ""
    outlook: str = ""
    facts: list[RationaleFact] = field(default_factory=list)
    sensitivities: list[str] = field(default_factory=list)

    def get(self, name: str) -> list[RationaleFact]:
        return [f for f in self.facts if f.field == name]


class CatalystKind(str, Enum):
    """A forward change that can move recurring earnings.  Discovery is triggered by these,
    not by current-quarter growth."""
    ORDERS = "executable_orders"
    CUSTOMER_APPROVAL = "customer_approval"
    CAPACITY = "capacity_or_bottleneck"
    UTILIZATION = "utilization"
    PRODUCT_MIX = "product_mix"
    CONTRACT_PRICING = "contract_pricing"
    FINANCING_COST = "financing_cost"
    SEGMENT_TURNAROUND = "segment_turnaround"


class ResearchStage(str, Enum):
    """Research stages - none of them is an instruction to invest."""
    POTENTIAL = "potential_catalyst"                       # announced; economics or execution support incomplete
    SUPPORTED = "supported_prospective_inflection"         # mechanism, materiality and execution pathway credible
    VALIDATING = "execution_validating"                    # milestones / first financial evidence support it
    CONFIRMED = "confirmed_for_investment_review"          # relevant reporting periods support the mechanism
    DELAYED = "delayed"                                    # timetable slipped; thesis not contradicted
    CONTRADICTED = "contradicted"                          # business evidence against the thesis
    DATA_UNAVAILABLE = "data_unavailable"                  # results/filings due but missing: not a business verdict


class MilestoneStatus(str, Enum):
    NOT_YET_RELEVANT = "not_yet_relevant"    # the catalyst cannot have contributed yet
    PENDING = "pending"
    MET = "met"
    CONSISTENT_MUTED = "consistent_muted"    # not met, not against the thesis (lumpy execution)
    MISSED = "missed"                        # deadline passed without it (delay)
    CONTRADICTED = "contradicted"
    DATA_UNAVAILABLE = "data_unavailable"


@dataclass
class ChainLink:
    """Demand -> deliverable capacity -> revenue conversion -> recurring profit -> cash."""
    link: str
    status: str            # supported | unsupported | unknown | contradicted | not_applicable
    basis: str = ""
    source_doc_ids: list[str] = field(default_factory=list)


@dataclass
class CatalystMilestone:
    question: str                         # fixed when the catalyst is first detected
    test: str                             # how it is judged
    relevant_from: Optional[date] = None  # first period end that can count
    due_by: Optional[date] = None
    status: "MilestoneStatus" = MilestoneStatus.PENDING
    observed: str = ""
    observed_at: Optional[datetime] = None
    source_doc_ids: list[str] = field(default_factory=list)
    periods_judged: int = 0               # relevant reporting periods the verdict rests on
    value: Optional[float] = None         # the measured value the verdict rests on
    # continuous monitoring: the ORIGINAL-timetable verdict is kept; later periods can show recovery or
    # deterioration, labelled separately (deadlines are never moved silently)
    original_status: Optional["MilestoneStatus"] = None
    original_observed: str = ""
    timetable: str = ""                   # on_schedule | recovered_late | missed | deteriorated | pending
    latest_observed: str = ""
    revised_due_by: Optional[date] = None
    revised_basis: str = ""


@dataclass
class EarningsContribution:
    status: str = "unresolved"     # estimated | potentially_material_unresolved | immaterial | unresolved
    downside_crore: Optional[float] = None    # incremental EBITDA per year
    base_crore: Optional[float] = None
    upside_crore: Optional[float] = None
    share_of_ttm_ebitda: Optional[float] = None
    basis: str = ""
    assumptions: list[str] = field(default_factory=list)
    # three tiers: illustrative operating upside (never counted) | supported incremental EBITDA (above) |
    # recurring parent earnings after D&A, interest, tax, minority interest and dilution
    illustrative_crore: Optional[float] = None
    illustrative_basis: str = ""
    pat_downside_crore: Optional[float] = None
    pat_base_crore: Optional[float] = None
    pat_upside_crore: Optional[float] = None
    eps_base: Optional[float] = None
    share_of_ttm_parent_pat: Optional[float] = None
    bridge_status: str = "not_computed"      # computed | unresolved
    bridge_missing: list[str] = field(default_factory=list)
    bridge_assumptions: list[str] = field(default_factory=list)
    earnings_materiality: str = "unresolved"  # established | not_material | unresolved


@dataclass
class Catalyst:
    catalyst_id: str
    ticker: str
    kind: "CatalystKind"
    first_public_at: Optional[datetime]           # when discovery was defensible
    first_doc_ids: list[str]
    operating_change: str                          # what is different
    facts: list[str] = field(default_factory=list)          # dated source facts
    expectations: list[str] = field(default_factory=list)   # plans / forecasts (incl. agency-repeated)
    corroboration: list[str] = field(default_factory=list)  # rating-agency context etc.
    uncertainties: list[str] = field(default_factory=list)
    window_start: Optional[date] = None           # when confirmation can start to arrive
    window_end: Optional[date] = None
    window_basis: str = ""
    contribution: EarningsContribution = field(default_factory=EarningsContribution)
    chain: list[ChainLink] = field(default_factory=list)
    milestones: list[CatalystMilestone] = field(default_factory=list)
    invalidators: list[str] = field(default_factory=list)
    stage: "ResearchStage" = ResearchStage.POTENTIAL
    stage_reasons: list[str] = field(default_factory=list)
    stage_since: Optional[date] = None
    quantities: dict[str, Any] = field(default_factory=dict)  # seed values used to rebuild it
    # what was knowable when: the initial assessment uses ONLY evidence public at first disclosure;
    # later evidence produces dated upgrades and never improves the original signal retrospectively
    review_conditions: list[str] = field(default_factory=list)   # unresolved items the review must settle
    initial_assessment: dict[str, Any] = field(default_factory=dict)
    materiality_supported_at: Optional[datetime] = None
    execution_supported_at: Optional[datetime] = None
    supported_at: Optional[datetime] = None
    validating_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    upgrades: list[str] = field(default_factory=list)


@dataclass
class InvestmentReview:
    """Separate gate, opened only by a confirmed catalyst.  Assembles review inputs;
    never a recommendation, never a position size."""
    status: str = "not_started"                # not_started | inputs_assembled | inputs_incomplete
    opened_by: list[str] = field(default_factory=list)
    remaining_upside: list[str] = field(default_factory=list)
    valuation: dict[str, Any] = field(default_factory=dict)
    cash_and_financing: list[str] = field(default_factory=list)
    governance: list[str] = field(default_factory=list)
    liquidity_and_downside: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)       # explicit unresolved review conditions
    note: str = ("A valid inflection can still be an unattractive investment at the prevailing price; "
                 "this gate assembles inputs for a human review and does not authorise any action.")


class ThesisStage(str, Enum):
    """Where a mechanism's earnings thesis stands.  Admission does not wait for results:
    a source-backed change in economics is a LEADING candidate; the first results after it
    are a separate validation milestone; persistence raises confidence (not admission)."""
    NONE = "none"
    LEADING = "leading"                                   # economics changed; first results pending
    FIRST_RESULTS_VALIDATED = "first_results_validated"   # first results after the signal show it
    FIRST_RESULTS_NOT_VALIDATED = "first_results_not_validated"
    REALIZED_EMERGING = "realized_emerging"               # first seen in results (no prior leading signal)
    CONFIRMED = "confirmed"                               # mechanism-specific persistence
    ADVERSE = "adverse"                                   # moving against earnings


class MilestoneOutcome(str, Enum):
    PENDING = "pending"
    OVERDUE = "overdue"                  # results should be public but are not parsed: data, not a verdict
    VALIDATED = "validated"
    NOT_VALIDATED = "not_validated"
    ADVERSE = "adverse"


@dataclass
class ThesisMilestone:
    name: str
    metric_basis: str                    # what is measured, with its threshold
    expected_by: Optional[date] = None   # results deadline for the first period after the signal
    period_end: Optional[date] = None
    outcome: "MilestoneOutcome" = MilestoneOutcome.PENDING
    observed: str = ""
    observed_at: Optional[datetime] = None


@dataclass
class MechanismThesis:
    mechanism: "Mechanism"
    stage: "ThesisStage"
    confidence: str = "low"              # low | medium | high - evidence quality, not probability
    leading_signal: str = ""
    leading_signal_at: Optional[datetime] = None
    validation: Optional[ThesisMilestone] = None
    confirmation: str = ""
    positive_evidence: list[str] = field(default_factory=list)   # dated, per reported period / filing
    negative_evidence: list[str] = field(default_factory=list)
    earliest_defensible_at: Optional[datetime] = None
    stale: bool = False
    stale_note: str = ""
    research_question: str = ""
    source_doc_ids: list[str] = field(default_factory=list)


@dataclass
class InflectionThesis:
    stage: "ThesisStage" = ThesisStage.NONE           # most advanced positive mechanism
    confidence: str = "low"
    earliest_defensible_at: Optional[datetime] = None  # earliest positive signal still standing
    mechanisms: list[MechanismThesis] = field(default_factory=list)
    stale: bool = False
    stale_note: str = ""
    next_milestone: str = ""
    notes: list[str] = field(default_factory=list)
    outcome_history: list[str] = field(default_factory=list)   # reported outcomes, attributed to no mechanism
    # when the series is stale: the evidence status as of the day the latest parsed results were
    # first public, recomputed from what was public then (kept visible, never the current status)
    last_known_status: str = ""
    last_known_as_of: Optional[date] = None
    last_known_rationale: list[str] = field(default_factory=list)


class EvidenceStatus(str, Enum):
    """Evidence-only assessment (no action authority)."""
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"
    ASSERTION_ONLY = "ASSERTION_ONLY"
    EARLY_COMMITMENT_UNVERIFIED = "EARLY_COMMITMENT_UNVERIFIED"   # early research lane, explicit limitations
    COMMITMENT_BACKED = "COMMITMENT_BACKED"                       # validated binding external commitments
    EXECUTION_EMERGING = "EXECUTION_EMERGING"
    EXECUTION_CONFIRMED = "EXECUTION_CONFIRMED"
    CONTRADICTED = "CONTRADICTED"


class ReviewStatus(str, Enum):
    UNREVIEWED = "UNREVIEWED"
    NEEDS_SOURCE_CHECK = "NEEDS_SOURCE_CHECK"
    REVIEWED_ACCEPTED = "REVIEWED_ACCEPTED"
    REVIEWED_REJECTED = "REVIEWED_REJECTED"


class ScenarioStatus(str, Enum):
    COMPUTED_ASSUMPTION_BASED = "COMPUTED_ASSUMPTION_BASED"
    NOT_COMPUTED_MISSING_INPUTS = "NOT_COMPUTED_MISSING_INPUTS"
    UNSUPPORTED_FINANCIAL_MODEL = "UNSUPPORTED_FINANCIAL_MODEL"


class GuidanceOutcome(str, Enum):
    PENDING = "PENDING"
    MET = "MET"
    PARTIALLY_MET = "PARTIALLY_MET"
    MISSED = "MISSED"
    UNVERIFIABLE = "UNVERIFIABLE"   # target period passed, no comparable realized data


class RevisionDirection(str, Enum):
    ORIGINAL = "original"
    REITERATED = "reiterated"
    RAISED = "raised"
    LOWERED = "lowered"
    WITHDRAWN = "withdrawn"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Quantity:
    value: float
    unit: Unit
    raw: str = ""
    low: Optional[float] = None    # for ranges ("15-18%")
    high: Optional[float] = None

    def validate(self) -> None:
        if self.value is None or self.value != self.value:  # NaN
            raise ContractError(f"quantity has no value: {self.raw!r}")
        if self.unit == Unit.PERCENT and not (-1000 <= self.value <= 1000):
            raise ContractError(f"implausible percent {self.value}")


@dataclass
class SourceDocument:
    doc_id: str
    source_name: str
    ticker: str
    country: str = "IN"
    company: str = ""
    title: str = ""
    doc_type: str = ""
    filing_type: str = ""
    url: str = ""
    filed_at: Optional[date] = None
    published_at: Optional[datetime] = None
    content_hash: str = ""
    local_path: str = ""
    text: Optional[str] = None
    pages: Optional[list[str]] = None
    # filled by document_versions
    kind: DocumentKind = DocumentKind.UNKNOWN
    kind_basis: str = ""
    available_at: Optional[datetime] = None
    availability_basis: str = ""
    supersedes: list[str] = field(default_factory=list)
    superseded_by: Optional[str] = None
    # text provenance (WP1): where the text came from and how complete it is
    text_source: str = ""                  # "artifact:<version>" | "legacy_raw_text" | "legacy_txt" | "none"
    extraction_status: str = ""            # ExtractionStatus value
    extraction_version_id: str = ""
    extraction_complete: Optional[bool] = None    # None = unknown (legacy text)
    extraction_issues: list[str] = field(default_factory=list)
    raw_hash: str = ""
    # system-time provenance (WP2): when MakroGraph first held the document and its text
    first_seen_at: Optional[datetime] = None
    text_available_at: Optional[datetime] = None  # None = not provable (legacy text)
    issuer_id: str = ""
    alias_used: str = ""

    def validate(self) -> None:
        if not self.doc_id:
            raise ContractError("document without id")
        if not self.ticker:
            raise ContractError(f"document {self.doc_id} has no ticker")

    def full_text(self) -> str:
        if self.pages:
            return "\f".join(self.pages)
        return self.text or ""


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    page: int
    ordinal: int
    kind: str              # "prose" | "table"
    text: str
    header: str = ""       # table header / nearest heading retained with the chunk
    char_start: int = 0
    char_end: int = 0


@dataclass
class Evidence:
    evidence_id: str
    doc_id: str
    ticker: str
    metric: Metric
    tier: EvidenceTier
    modality: Modality
    quote: str
    available_at: Optional[datetime]
    page: int = 0
    chunk_id: str = ""
    quantity: Optional[Quantity] = None
    period_label: str = ""
    target_period_label: str = ""
    scope: Scope = Scope.UNKNOWN
    segment: str = ""
    facility: str = ""                  # plant / unit / product line the statement is about ("" = company-level)
    counterparty: str = ""
    counterparty_named: bool = False
    commitment_strength: CommitmentStrength = CommitmentStrength.NOT_APPLICABLE
    distinct_marker: bool = False      # "repeat order", "fresh order", "another order"
    # commercial-event fields (WP4); None/"" = not disclosed
    event_stage: Optional[EventStage] = None
    relationship: RelationshipStatus = RelationshipStatus.UNKNOWN
    relationship_basis: str = ""
    value_basis: Optional[ValueBasis] = None
    tax_basis: TaxBasis = TaxBasis.UNKNOWN
    duration_months: Optional[int] = None
    delivery_window: str = ""
    payment_terms: str = ""
    termination_terms: str = ""
    reference_id: str = ""
    product: str = ""
    extractor: str = "deterministic"   # "deterministic" | "llm"
    direction: int = 0                 # +1 increase / -1 decrease stated in the sentence, 0 = not stated
    validation_issues: list[str] = field(default_factory=list)
    # who is speaking: "" = the issuer / its filing; "rating_agency" = a credit-rating rationale
    # (corroborating context: an agency repeating management's plan is not proof of execution)
    source_role: str = ""

    @property
    def usable(self) -> bool:
        return not any(i.startswith("FATAL:") for i in self.validation_issues)


@dataclass
class EconomicEvent:
    event_id: str
    ticker: str
    kind: Metric                    # ORDER_WIN, FUNDRAISE, CAPACITY ...
    amount: Optional[Quantity]
    counterparty: str
    first_public_at: Optional[datetime]
    evidence_ids: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    commitment_strength: CommitmentStrength = CommitmentStrength.NOT_APPLICABLE
    description: str = ""
    # WP4: dated state history; current_stage is the latest dated state, never the strongest-ever
    current_stage: Optional[EventStage] = None
    history: list["EventStateChange"] = field(default_factory=list)
    original_amount: Optional[Quantity] = None
    cancelled_amount: float = 0.0
    relationship: RelationshipStatus = RelationshipStatus.UNKNOWN
    relationship_basis: list[str] = field(default_factory=list)
    customer_verification: CustomerVerification = CustomerVerification.ANONYMOUS
    value_basis: ValueBasis = ValueBasis.UNQUANTIFIED
    tax_basis: TaxBasis = TaxBasis.UNKNOWN
    duration_months: Optional[int] = None
    delivery_window: str = ""
    payment_terms: str = ""
    termination_terms: str = ""
    reference_id: str = ""
    product: str = ""
    ambiguous_with: list[str] = field(default_factory=list)   # possible duplicates (counted once)
    unresolved_fields: list[str] = field(default_factory=list)

    @property
    def annual_executable_estimate(self) -> Optional[float]:
        """Amount per 12 months when a duration is disclosed; unknown otherwise (labelled estimate)."""
        if self.amount is None or not self.duration_months:
            return None
        return self.amount.value * min(1.0, 12.0 / self.duration_months)


@dataclass
class EventStateChange:
    stage: EventStage
    at: Optional[datetime]
    doc_id: str
    evidence_id: str
    amount: Optional[float] = None       # value after this change (crore)
    note: str = ""


@dataclass
class FinancialMeasurement:
    ticker: str
    metric: Metric
    period_end: date
    period_type: str               # "Q" | "FY" | "H" | "9M"
    value: float
    unit: Unit
    scope: Scope
    doc_id: str
    available_at: Optional[datetime]
    source: str = "reported"       # "reported" | "derived"
    restated: bool = False
    system_available_at: Optional[datetime] = None   # when MakroGraph held the document AND its text
    display_unit: Optional[float] = None    # value of one unit in the last displayed digit (crore), for tolerances
    integrity: str = "unchecked"            # unchecked | validated | definition_difference | unresolved | rejected
    integrity_notes: list[str] = field(default_factory=list)
    evidence_id: str = ""
    quote: str = ""
    segment: str = ""                       # segment-reporting rows only ("" = company level)


@dataclass
class DriverChange:
    driver: str
    ticker: str
    period_end: Optional[date]
    current: Optional[float]
    prior: Optional[float]
    change: Optional[float]
    unit: str
    basis: str
    material: bool = False
    comparable: bool = True
    notes: list[str] = field(default_factory=list)
    source_doc_ids: list[str] = field(default_factory=list)
    # Earliest defensible detection time = latest availability of ALL required inputs
    # (never the oldest cited document).  system_known_at adds MakroGraph's own
    # ingestion/extraction times; None when they cannot be proven.
    knowable_at: Optional[datetime] = None
    system_known_at: Optional[datetime] = None


@dataclass(frozen=True)
class GuidanceRevision:
    stated_at: Optional[datetime]
    doc_id: str
    evidence_id: str
    quantity: Optional[Quantity]
    direction: RevisionDirection
    quote: str
    # For lowered / withdrawn revisions: did management give a reason nearby?
    # True/False when the source text was checked, None when it was not available.
    # Whether the reason is CREDIBLE is a human judgment and is never set here.
    explained: Optional[bool] = None
    explanation: str = ""          # verbatim sentence containing the stated reason


@dataclass
class GuidanceRecord:
    guidance_id: str
    ticker: str
    metric: Metric
    target_period_label: str
    original: GuidanceRevision
    # append-only: every later statement in publication order, each an immutable record
    revisions: tuple[GuidanceRevision, ...] = ()
    outcome: GuidanceOutcome = GuidanceOutcome.PENDING          # vs ORIGINAL statement
    latest_outcome: GuidanceOutcome = GuidanceOutcome.PENDING   # vs latest stated guidance
    realized_value: Optional[float] = None
    outcome_note: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class CounterpartyProfile:
    name: str
    named: bool
    events: list[str] = field(default_factory=list)
    total_amount_crore: float = 0.0
    strongest_commitment: CommitmentStrength = CommitmentStrength.NOT_APPLICABLE
    share_of_ttm_revenue: Optional[float] = None
    risk_flags: list[str] = field(default_factory=list)
    relationship: RelationshipStatus = RelationshipStatus.UNKNOWN
    relationship_sources: list[str] = field(default_factory=list)
    verification: CustomerVerification = CustomerVerification.ANONYMOUS


@dataclass
class BridgeScenario:
    name: str
    assumptions: dict[str, Any]
    revenue_crore: Optional[float] = None
    ebitda_crore: Optional[float] = None
    recurring_pat_attributable_crore: Optional[float] = None
    recurring_diluted_eps: Optional[float] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class EarningsBridge:
    status: ScenarioStatus
    base_period_label: str = ""
    scenarios: list[BridgeScenario] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    cash_notes: list[str] = field(default_factory=list)
    # WP7: every input with its source (reported / management assertion / analyst assumption)
    assumption_register: list[dict[str, Any]] = field(default_factory=list)
    # WP7: annual earnings contribution by mechanism (overlapping effects are not additive)
    mechanism_contributions: list[dict[str, Any]] = field(default_factory=list)
    management_case_note: str = ""


@dataclass
class SourceRef:
    doc_id: str
    title: str
    kind: str
    available_at: Optional[datetime]
    availability_basis: str
    url: str = ""
    quote: str = ""
    page: int = 0


@dataclass
class ChangeFinding:
    what: str
    business: str
    first_public_at: Optional[datetime]
    tier: EvidenceTier
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    ticker: str
    company: str
    country: str
    as_of: datetime
    issuer_model: IssuerModel
    listing_segment: ListingSegment
    evidence_status: EvidenceStatus
    review_status: ReviewStatus
    scenario_status: ScenarioStatus
    status_rationale: list[str]
    what_changed: list[ChangeFinding]
    drivers: list[DriverChange]
    guidance: list[GuidanceRecord]
    events: list[EconomicEvent]
    counterparties: list[CounterpartyProfile]
    bridge: EarningsBridge
    contradictions: list[str]
    financing_risks: list[str]
    customer_risks: list[str]
    missing_inputs: list[str]
    next_checks: list[str]
    sources: list[SourceRef]
    limitations: list[str]
    coverage: dict[str, Any] = field(default_factory=dict)
    notice: str = RESEARCH_ONLY_NOTICE
    # exact text versions read for this assessment (replayable via the run manifest)
    source_manifest: list[dict[str, Any]] = field(default_factory=list)
    mechanisms: list["MechanismResult"] = field(default_factory=list)       # WP5
    valuation: dict[str, Any] = field(default_factory=dict)                  # WP7, optional context only
    thesis: Optional["InflectionThesis"] = None                              # forward-looking layer
    rating_rationales: list["RatingRationale"] = field(default_factory=list)  # dated, every version kept
    catalysts: list["Catalyst"] = field(default_factory=list)                # forward setup (discovery)
    research_summary: dict[str, Any] = field(default_factory=dict)            # detected / why / waiting for
    investment_review: Optional["InvestmentReview"] = None                   # only after confirmation
    replay_mode: str = "PUBLIC_INFORMATION_RECONSTRUCTION"
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.limitations:
            raise ContractError("assessment must carry limitations")
        if self.evidence_status not in (EvidenceStatus.INSUFFICIENT_EVIDENCE,) and not self.sources:
            raise ContractError("assessment with evidence status must cite sources")
        assert_no_action_language(self)


# ---------------------------------------------------------------------------
# Output guard: no investment-action language in generated fields
# ---------------------------------------------------------------------------

FORBIDDEN_ACTION_PATTERNS = [
    re.compile(r"\b(BUY|STRONG BUY|SELL|STARTER|ACCUMULATE|ADD ON DIPS|OVERWEIGHT|UNDERWEIGHT)\b"),
    re.compile(r"\bposition[ _-]?siz", re.I),
    re.compile(r"\b(?:portfolio|capital)\s+(?:allocation|weight)", re.I),
    re.compile(r"\btarget\s+price\b|\bprice\s+target\b", re.I),
    re.compile(r"\b(?:entry|exit)\s+(?:price|point)\b", re.I),
]

# Fields that carry verbatim source text; these are quoted evidence, not
# generated language, and are excluded from the guard.
_VERBATIM_FIELDS = {"quote", "raw", "title", "company", "counterparty", "url", "notice"}


def _walk_generated_strings(obj: Any, path: str = ""):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            if f.name in _VERBATIM_FIELDS:
                continue
            yield from _walk_generated_strings(getattr(obj, f.name), f"{path}.{f.name}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), f"{path}[key]"
            if str(k) not in _VERBATIM_FIELDS:
                yield from _walk_generated_strings(v, f"{path}[{k}]")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_generated_strings(v, f"{path}[{i}]")
    elif isinstance(obj, Enum):
        yield str(obj.value), path
    elif isinstance(obj, str):
        yield obj, path


def find_action_language(obj: Any) -> list[str]:
    hits = []
    for s, path in _walk_generated_strings(obj):
        for pat in FORBIDDEN_ACTION_PATTERNS:
            m = pat.search(s)
            if m:
                hits.append(f"{path}: {m.group(0)!r}")
    return hits


def assert_no_action_language(obj: Any) -> None:
    hits = find_action_language(obj)
    if hits:
        raise ContractError("investment-action language in generated output: " + "; ".join(hits[:5]))


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return obj
