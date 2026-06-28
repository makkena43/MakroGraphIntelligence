"""Investment signal extraction from financial document text.

Signals extracted:
    capex_increase / capex_decrease   - Capital expenditure guidance
    demand_surge / demand_slowdown    - Demand trajectory signals
    supply_bottleneck / supply_easing - Supply chain tightness
    demand_exceeds_supply             - Explicit demand > supply constraint
    strategic_pivot                   - Business strategy change
    partnership_formed                - New strategic partnerships
    acquisition_intent                - M&A signals
    technology_adoption               - New tech investments
    technology_disruption             - Incumbent displacement signals
    competition_threat                - Competitive pressure mentions
    market_entry                      - New market entry signals
    regulatory_tailwind / headwind    - Regulatory environment signals
    hiring_surge / hiring_freeze      - Workforce direction
    inventory_buildup / drawdown      - Inventory cycle signals

Performance notes:
  - All patterns are pre-compiled at module import time (re.compile).
    This eliminates regex compilation overhead on every extract() call.
  - Text is truncated to MAX_TEXT_CHARS before regex scanning. SEC 10-K
    filings often exceed 200k words; the signal-rich content (MD&A, risk
    factors, earnings commentary) is almost always in the first 80k chars.
  - The old generic r"\bsupply\b" pattern has been replaced with tighter
    patterns requiring explicit shortage/bottleneck/constraint context so
    that normal "supply" mentions don't flood the supply_bottleneck bucket.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Maximum characters of document text to scan with regex.
# SEC 10-K filings can be 500k+ chars. Signal-rich sections (MD&A, risk
# factors, earnings call) are almost always in the first 80k characters.
MAX_TEXT_CHARS = 80_000


@dataclass
class InvestmentSignal:
    """A single investment signal extracted from text."""
    signal_type: str
    direction: str                          # positive | negative | neutral
    confidence: float = 0.7
    signal_value: Optional[float] = None
    signal_unit: Optional[str] = None
    context_text: str = ""
    entity_text: str = ""
    extracted_by: str = "rule"
    position: int = 0
    perspective: str = "neutral"            # seller | buyer | neutral
    # perspective = 'seller': company IS the constrained supplier
    #               (customers can't get enough FROM THEM → pricing power)
    # perspective = 'buyer':  company NEEDS the constrained item
    #               (they can't source inputs → margin pressure)
    # This is the critical field for investment decisions:
    # Only 'seller' perspective supply constraints = investable bullish signal

    @property
    def is_bullish(self) -> bool:
        return self.direction == "positive"

    @property
    def is_bearish(self) -> bool:
        return self.direction == "negative"


# -----------------------------------------------------------------------
# RAW SIGNAL PATTERNS
# Each tuple: (raw_regex, signal_type, direction, confidence)
#
# Ordering matters: higher-confidence specific patterns should come first
# so that deduplicate() keeps the best match per signal type per window.
# -----------------------------------------------------------------------
_RAW_PATTERNS: list[tuple[str, str, str, float]] = [

    # ── CAPEX ────────────────────────────────────────────────────────────
    (r"\b(?:capital expenditure|capex|capital spending)\b.{0,60}(?:increas|rais|expan|doubl|grow)",
     "capex_increase", "positive", 0.85),
    (r"\b(?:increas|rais|expan|doubl)\b.{0,60}(?:capital expenditure|capex|capital spending)",
     "capex_increase", "positive", 0.85),
    # Investment with any major currency (USD / INR — same intent, same signal)
    (r"\binvest(?:ing|ment)?.{0,40}"
     r"(?:\$\s?\d+(?:\.\d+)?\s*(?:billion|million|B|M\b)|"
     r"(?:Rs\.?\s*|INR\s*|₹\s*)\d[\d,]*\s*(?:crores?|lakhs?|Crs?\b|cr\b))\b",
     "capex_increase", "positive", 0.82),
    # INR amount near expansion/plant/manufacturing context
    (r"(?:Rs\.?\s*|INR\s*|₹\s*)\d[\d,]*(?:\.\d+)?\s*(?:crores?|lakhs?|Crs?\b|cr\b)"
     r".{0,60}(?:invest|capex|plant|expand|facilit|manufactur|greenfield|brownfield)",
     "capex_increase", "positive", 0.82),
    # India-specific capex: greenfield/brownfield plant, new manufacturing line, capacity addition
    (r"\b(?:greenfield|brownfield)\b.{0,60}(?:plant|facilit|project|unit|manufactur|invest)",
     "capex_increase", "positive", 0.85),
    (r"\b(?:new\s+(?:plant|facilit|manufactur|unit|line|capacity)|"
     r"capacity\s+(?:addition|expansion|augment|enhance|creat|build))\b",
     "capex_increase", "positive", 0.83),
    (r"\b(?:set(?:ting)?\s+up|commission(?:ing)?|establish(?:ing)?)\b.{0,40}"
     r"(?:plant|unit|facilit|manufactur|capacity|production)",
     "capex_increase", "positive", 0.82),
    (r"\b(?:capital expenditure|capex)\b.{0,60}(?:decreas|reduc|cut|lower|trim)",
     "capex_decrease", "negative", 0.80),
    # Capital raises for plant/growth — only equity raises with explicit capex/expansion intent
    # EXCLUDED: NCD, debenture, rights issue, QIP alone — these are debt/equity raises that
    # fund working capital or refinancing, NOT plant investment (common in Indian filings).
    (r"\b(?:QIP|qualified\s+institutional\s+placement|preferential\s+allotment)\b"
     r".{0,80}(?:expand|capex|manufactur|plant|facilit|greenfield|brownfield|capacity)",
     "capex_increase", "positive", 0.78),

    # ── DEMAND EXCEEDING SUPPLY (highest value signal — detect first) ─────
    (r"\b(?:demand|orders?|customer(?:s)?|request(?:s)?)\b.{0,80}"
     r"(?:exceed|outstrip|outpac|overwhelm|surpass).{0,50}(?:supply|capacity|production|output)",
     "demand_surge", "positive", 0.92),
    # ── CAPACITY CONSTRAINT — SELLER perspective ──────────────────────────
    # Company IS the constrained supplier: customers can't get enough FROM THEM.
    # This is the investable signal — pricing power, order book visibility.
    # Tagged as signal_type='capacity_constraint_seller' so the investment
    # funnel can filter ONLY on this, not on buyer-side constraints.
    (r"\b(?:can(?:not|'t)\s+(?:meet|keep\s+up\s+with|satisfy|fulfill)|"
     r"unable\s+to\s+(?:meet|satisfy|fulfill)).{0,60}(?:demand|orders?|request|need)",
     "capacity_constraint_seller", "positive", 0.94),
    # "sold out" / "fully allocated" — clear seller language
    (r"\b(?:sold\s+out|fully\s+booked|fully\s+allocated|"
     r"allocation.{0,30}(?:limit|constrain|scarc)|"
     r"oversubscribed.{0,30}(?:demand|order|request))",
     "capacity_constraint_seller", "positive", 0.93),
    # "our lead times extended" — their delivery queue grew (customers waiting for them)
    (r"\b(?:our\s+)?lead.?time(?:s)?\s+(?:extend|lengthen|stretch|grow|increas).{0,40}"
     r"(?:week|month|quarter|year|\d+)",
     "capacity_constraint_seller", "positive", 0.91),
    (r"\bwaiting\s+(?:list|time|period).{0,40}(?:grow|increas|lengthen|extend)",
     "capacity_constraint_seller", "positive", 0.90),
    # "backlog at record / growing / visibility" — customers pre-ordering from them
    (r"\bbacklog.{0,60}(?:record|all.time|highest|grow|increas|strong|robust|extend|months|quarters)",
     "capacity_constraint_seller", "positive", 0.92),
    # Original supply_bottleneck negative direction kept for buyer ambiguous cases
    (r"\b(?:can(?:not|'t)\s+(?:meet|keep\s+up\s+with|satisfy|fulfill)|"
     r"unable\s+to\s+(?:meet|satisfy|fulfill)).{0,60}(?:demand|orders?|request|need)",
     "supply_bottleneck", "negative", 0.92),

    # ── DEMAND SURGE ─────────────────────────────────────────────────────
    (r"\b(?:demand|orders?|backlog|pipeline)\b.{0,60}"
     r"(?:surge|boom|strong(?:er)?|record|accelerat|exceed|outpac|robust|exceptional)",
     "demand_surge", "positive", 0.85),
    (r"\brecord\b.{0,40}(?:demand|orders?|revenue|sales|bookings?|backlog)",
     "demand_surge", "positive", 0.85),
    (r"\b(?:demand|orders?|bookings?)\b.{0,30}(?:up|grew|grow|increas).{0,20}\d+\s*%",
     "demand_surge", "positive", 0.88),
    (r"\bdemand\s+(?:outlook|trend|environment)\b.{0,40}(?:positive|strong|favor|improv|robust)",
     "demand_surge", "positive", 0.80),
    # Order wins — "bagging" is common in Indian/Asian business English for receiving an order;
    # "awarded", "received", "secured", "won" are universal equivalents
    (r"\b(?:bagg(?:ing|ed)|receiv(?:ing|ed)|secur(?:ing|ed)|award(?:ed)?|win(?:ning|s)?|won)\b"
     r".{0,60}(?:order|contract|project|work\s+order|purchase\s+order)",
     "demand_surge", "positive", 0.88),
    (r"\bnew\s+order(?:s)?\b.{0,60}(?:receiv|secur|bagg|win|award|obtain)",
     "demand_surge", "positive", 0.85),
    # Orders announced with any currency amount
    (r"\b(?:order|contract|project)\b.{0,40}"
     r"(?:(?:Rs\.?\s*|INR\s*|₹\s*)\d[\d,]*\s*(?:crores?|lakhs?|Crs?\b)|"
     r"\$\s*\d+(?:\.\d+)?(?:\s*(?:billion|million|B|M\b))?)",
     "demand_surge", "positive", 0.85),
    # Financial performance growth YoY/QoQ — applies globally (US quarterly reports too)
    (r"\b(?:PAT|profit\s+after\s+tax|net\s+profit|revenue|turnover|sales|earnings|EPS)\b"
     r".{0,40}(?:grew|increas|up|rise|rose|surged|jumped).{0,20}\d+\s*%",
     "demand_surge", "positive", 0.80),
    (r"\bgrew?\b.{0,30}\d+\s*%\b.{0,30}(?:YOY|year.on.year|YTD|QOQ|quarter)",
     "demand_surge", "positive", 0.75),

    # ── DEMAND SLOWDOWN ───────────────────────────────────────────────────
    (r"\b(?:demand|orders?)\b.{0,60}(?:weak|soft|declin|slow|disappoint|compress|soften|muted)",
     "demand_slowdown", "negative", 0.80),
    # Financial performance decline YoY/QoQ — same pattern as surge, inverted
    (r"\b(?:PAT|profit\s+after\s+tax|net\s+profit|revenue|turnover|sales|earnings|EPS)\b"
     r".{0,40}(?:declin|decreas|fell|drop|down|lower).{0,20}\d+\s*%",
     "demand_slowdown", "negative", 0.80),
    (r"\bdeclin\w*\b.{0,30}\d+\s*%\b.{0,30}(?:YOY|year.on.year|QOQ|quarter)",
     "demand_slowdown", "negative", 0.75),

    # ── SUPPLY BOTTLENECK — BUYER perspective ────────────────────────────
    # Company NEEDS the constrained item — their inputs are scarce.
    # This is margin-compressive for them. NOT the investable signal.
    # (The supplier of these scarce components is the investable play.)
    (r"\b(?:supply\s+(?:chain\s+)?(?:shortage|constraint|crunch|tightness|disruption|bottleneck)|"
     r"capacity\s+(?:constraint|crunch|limit|shortfall|tighten)|"
     r"component\s+(?:shortage|scarcity|crunch)|"
     r"material\s+(?:shortage|scarcity|constraint))",
     "supply_bottleneck", "negative", 0.87),
    (r"\b(?:supply\s+chain)\b.{0,60}(?:tighten|strain|disrupt|challeng|stress|squeez)",
     "supply_bottleneck", "negative", 0.83),
    (r"\b(?:bottleneck|constrain|shortage|scarcity|crunch)\b.{0,60}"
     r"(?:material|component|chip|wafer|lith|cobalt|copper|power|grid|labor|talent|bandwidth|rack|gpu)",
     "supply_bottleneck", "negative", 0.88),
    (r"\b(?:limited\s+(?:supply|availability|capacity)|"
     r"supply\s+(?:limited|constrained|tight|insufficient))",
     "supply_bottleneck", "negative", 0.82),

    # ── SUPPLY EASING ─────────────────────────────────────────────────────
    (r"\b(?:supply\s+chain)\b.{0,60}(?:normal|ease|improv|resol|recover|stabiliz)",
     "supply_easing", "positive", 0.75),
    (r"\b(?:inventory\s+(?:normal|recover|stabiliz|build)|"
     r"supply\s+(?:recover|improv|ease|increase|catch\s+up))",
     "supply_easing", "positive", 0.75),

    # ── INVENTORY ─────────────────────────────────────────────────────────
    (r"\b(?:inventory).{0,40}(?:build|accumulat|higher|days\s+on\s+hand|increas)",
     "inventory_buildup", "neutral", 0.70),
    (r"\b(?:inventory).{0,40}(?:draw|declin|reduc|normaliz|burn|digest|thin)",
     "inventory_drawdown", "neutral", 0.70),

    # ── STRATEGIC ────────────────────────────────────────────────────────
    (r"\b(?:strategic\s+pivot|shift\s+strateg|new\s+(?:strategic\s+)?direction|"
     r"strategic\s+review|business\s+transformation)\b",
     "strategic_pivot", "neutral", 0.80),
    (r"\b(?:pivot(?:ing|ed)?|repositioning)\b.{0,60}(?:strateg|business|model|focus|portfol)",
     "strategic_pivot", "neutral", 0.78),
    (r"\b(?:partner(?:ship)?|joint\s+venture|collaborat|alliance).{0,40}(?:announc|form|sign|enter)",
     "partnership_formed", "positive", 0.80),
    # M&A — require deal-action verb to avoid firing on routine "informed about Merger" boilerplate.
    # "buy-back" / "buyback" excluded — that is share repurchase, not acquisition.
    # "buy" alone excluded to prevent "buy-back" and financial buyouts from casual context.
    (r"\b(?:acqui(?:sition|re|ring)|merger(?!\s+of\s+equals)|takeover|buyout)\b"
     r".{0,80}(?:agree|announc|approv|complet|clos|sign|propos|plan|intend|contemplat|pursu)"
     r"|"
     r"(?:agree|announc|approv|complet|clos|sign|propos|plan|intend|contemplat|pursu)"
     r".{0,80}\b(?:acqui(?:sition|re|ring)|merger|takeover|buyout)\b",
     "acquisition_intent", "positive", 0.80),

    # ── TECHNOLOGY ────────────────────────────────────────────────────────
    (r"\b(?:deploy(?:ing|ment)?|adopt(?:ing)?|implement(?:ing)?|integrat(?:ing)?).{0,40}"
     r"(?:AI|artificial\s+intelligence|machine\s+learning|LLM|GPU|cloud|automation|robotics)",
     "technology_adoption", "positive", 0.85),
    (r"\b(?:replac|displac|disrupt|obsolete|legacy).{0,40}(?:technolog|platform|product|system)",
     "technology_disruption", "negative", 0.75),

    # ── COMPETITION ───────────────────────────────────────────────────────
    (r"\b(?:compet(?:ition|itor|itive)|market\s+share.{0,30}(?:loss|declin|gain|erode))",
     "competition_threat", "negative", 0.75),
    (r"\b(?:enter(?:ing)?|launch(?:ing)?).{0,30}(?:market|segment|geography)",
     "market_entry", "positive", 0.70),

    # ── REGULATORY ────────────────────────────────────────────────────────
    # Generic language (works for any market)
    (r"\b(?:regulat(?:ion|ory)).{0,60}(?:favorable|tailwind|benefit|support|approv)",
     "regulatory_tailwind", "positive", 0.80),
    (r"\b(?:regulat(?:ion|ory)).{0,60}(?:headwind|restrict|penalt|fine|challeng|concern)",
     "regulatory_headwind", "negative", 0.80),
    # US regulators
    (r"\b(?:FDA|EPA|FTC|CFPB|CFTC|antitrust).{0,40}(?:approv|clear|pass|grant)",
     "regulatory_tailwind", "positive", 0.85),
    (r"\b(?:FDA|EPA|FTC|CFPB).{0,40}(?:investigat|reject|fine|penalt|block)",
     "regulatory_headwind", "negative", 0.85),
    # India regulators — same signal type, same pattern structure as FDA/EPA above
    # Note: "prohibit" excluded — "SEBI (Prohibition of Insider Trading) Regulations"
    # is a regulation name, not enforcement action. "order" excluded for same reason.
    (r"\b(?:SEBI|RBI|Reserve\s+Bank|NCLT|NCLAT|CCI|DPIIT|MoF|Ministry\s+of\s+Finance)\b"
     r".{0,60}(?:approv|clear|allow|permit|grant|nod|green.?light|sanction)",
     "regulatory_tailwind", "positive", 0.85),
    (r"\b(?:SEBI|RBI|NCLT|CCI)\b.{0,60}"
     r"(?:reject|fine|penalt|investigat|suspen|restrain|block|violat|"
     r"impos\w*\s+(?:penalty|fine)|show.cause|adverse\s+order|enforcement\s+action)",
     "regulatory_headwind", "negative", 0.85),

    # ── HIRING ────────────────────────────────────────────────────────────
    (r"\b(?:hir(?:ing|e)|headcount|workforce|recruit).{0,40}"
     r"(?:expan|increas|surge|ramp|significan|accelerat)",
     "hiring_surge", "positive", 0.75),
    (r"\b(?:layoff|headcount.{0,20}reduc|workforce.{0,20}reduc|restructur|right.?siz)",
     "hiring_freeze", "negative", 0.80),

    # ── CAPACITY SHORTAGE ────────────────────────────────────────────────
    # Explicit capacity shortage language — higher conviction than supply_bottleneck
    (r"\b(?:capacity\s+(?:fully\s+booked|oversubscribed|saturated|maxed\s+out|"
     r"running\s+at\s+full|at\s+(?:full|max|peak)\s+utiliz))",
     "capacity_shortage", "negative", 0.88),
    (r"\b(?:no\s+(?:additional\s+)?capacity|capacity\s+not\s+available|"
     r"capacity\s+(?:crunch|crunch|dearth|deficit)|utilization.{0,20}(?:9[0-9]|100)\s*%)",
     "capacity_shortage", "negative", 0.85),
    (r"\b(?:backlog|order\s+backlog).{0,40}(?:\d+\s*(?:months?|quarters?|years?))",
     "capacity_shortage", "negative", 0.82),

    # ── LOCALIZATION OPPORTUNITY ─────────────────────────────────────────
    # Import substitution / Make in India / PLI-driven domestic production
    (r"\b(?:import\s+substitut|localiz(?:ation|ing|ed?)|indigeniz(?:ation|ing)|"
     r"domestic(?:ally)?\s+(?:manufactur|produc|sourc)|make\s+in\s+india)\b",
     "localization_opportunity", "positive", 0.85),
    (r"\b(?:PLI|production.linked\s+incentive|FAME|phased\s+manufacturing\s+programme|PMP)\b"
     r".{0,60}(?:approv|eligibl|benefit|receiv|sanction|disburse|claim)",
     "localization_opportunity", "positive", 0.87),
    # PLI scheme mentions with specific sector context (higher conviction)
    (r"\bPLI\b.{0,40}(?:semiconductor|solar|battery|ACC|automobile|auto\s+component|"
     r"telecom|textile|pharma|bulk\s+drug|medical\s+device|white\s+goods|"
     r"food\s+processing|specialty\s+steel|drone)",
     "localization_opportunity", "positive", 0.90),
    (r"\b(?:import\s+duty|custom\s+duty|BCD|anti.dumping)\b.{0,40}"
     r"(?:increas|hike|impos|rais|raised|hiked).{0,40}"
     r"(?:solar|semiconductor|electron|steel|chemical|battery|EV|telecom)",
     "localization_opportunity", "positive", 0.83),

    # ── TENDER PIPELINE ──────────────────────────────────────────────────
    # Active tender / bid pipeline signals — India-specific
    (r"\b(?:L1|lowest\s+bidder|lowest\s+quoted|emerged\s+L1|declared\s+L1)\b",
     "tender_pipeline", "positive", 0.88),
    (r"\b(?:tender|bid|RFP|RFQ|request\s+for\s+(?:proposal|quotation))\b.{0,60}"
     r"(?:win|won|award|bagg|secur|receiv|approv|issue)",
     "tender_pipeline", "positive", 0.85),
    (r"\b(?:tender\s+(?:floated|issued|called|invit)|SECI\s+tender|PGCIL\s+tender|"
     r"Railways\s+tender|CPWD\s+tender|NTPC\s+tender|PowerGrid\s+tender)\b",
     "tender_pipeline", "positive", 0.82),
    # GeM portal — Government e-Marketplace (major India procurement channel)
    (r"\b(?:GeM|GEM|government\s+e.?marketplace)\b.{0,60}"
     r"(?:order|bid|tender|procure|win|award|portal|purchase)",
     "tender_pipeline", "positive", 0.83),
    # SECI power purchase agreements — key demand signal for solar/wind
    (r"\bSECI\b.{0,60}(?:power\s+purchase\s+agreement|PPA|allocated|awarded|MW|GW)",
     "tender_pipeline", "positive", 0.85),
    (r"(?:Rs\.?\s*|INR\s*|₹\s*)\d[\d,]*\s*(?:crores?|Crs?\b).{0,40}"
     r"(?:tender|order|contract|project|EPC|bid)",
     "tender_pipeline", "positive", 0.82),

    # ── QUANTIFIED CONSTRAINT METRICS (world-class accuracy signals) ─────
    # These extract NUMBERS from management commentary, turning vague statements
    # into investable facts. Backlog of 12 months vs 2 months = very different.

    # Backlog duration — how many months/quarters of production are already booked
    # "18-month backlog", "backlog extends into Q4 2025", "2.5 years of orders"
    # This is the STRONGEST indicator of pricing power — customer willingness to
    # commit far in advance = inelastic demand.
    (r"\bbacklog\b.{0,60}(?:cover|extend|span|reach|vis[ib]+ilit).{0,30}"
     r"(?:\d+\s*(?:month|quarter|year|week)s?|through|into\s+(?:Q[1-4]|20\d{2}))",
     "backlog_duration", "positive", 0.88),
    (r"(?:order\s+book|backlog)\b.{0,40}"
     r"(?:of\s+)?\d+[\.,]?\d*\s*(?:months?|quarters?|years?)",
     "backlog_duration", "positive", 0.86),

    # Capacity utilization — percentage of production capacity in use
    # HIGH utilization (≥90%) = approaching constraint → pricing power incoming
    # "Operating at 94% utilization", "running at full capacity"
    (r"\b(?:operat|run(?:ning)?|utiliz)\b.{0,40}"
     r"(?:at\s+)?(?:9[0-9]|100)\s*%\s*(?:capacity|utilization|util)",
     "capacity_utilization_high", "positive", 0.87),
    (r"\b(?:capacity|plant|facility)\b.{0,40}"
     r"(?:fully\s+(?:loaded|utilized|booked)|at\s+(?:full|peak|maximum)\s+(?:capacity|utilization|load))",
     "capacity_utilization_high", "positive", 0.88),
    # Extract the actual % when stated explicitly
    (r"\butilization\b.{0,20}(?:rate\b.{0,10})?(?:of\s+)?(\d{2,3})\s*%",
     "capacity_utilization_high", "positive", 0.84),

    # Realized margin expansion from pricing / product mix
    # This is the ECONOMIC VALIDATION of pricing power — not just "we can raise prices"
    # but "our gross margin expanded 400 basis points due to better product mix"
    (r"\b(?:gross\s+)?margin\b.{0,60}"
     r"(?:expand|improv|increas|widen|higher).{0,40}"
     r"(?:\d+\s*(?:basis\s+)?(?:points?|bps?|pp)|%)",
     "realized_margin_expansion", "positive", 0.85),
    (r"\b(?:ASP|average\s+selling\s+price|realization|realisation)\b.{0,60}"
     r"(?:increase|rise|grow|improv|higher|up).{0,30}(?:\d+\s*%|\d+\s*(?:rs\.|inr|₹|\$))",
     "realized_margin_expansion", "positive", 0.84),
    (r"\b(?:better\s+(?:pricing|realiz|product\s+mix)|pricing\s+(?:power\s+)?(?:realiz|materializ|captur|seen))\b",
     "realized_margin_expansion", "positive", 0.80),

    # Supply concentration — company claims monopoly/near-monopoly position
    # "We are the only domestic manufacturer", "only 2 global suppliers"
    # This is the MOAT signal — scarcity of supply creates lasting pricing power.
    (r"\b(?:only|sole|lone|singular)\b.{0,30}"
     r"(?:domestic|local|indigenous|indian)?\s*(?:manufacturer|supplier|producer|maker|vendor)\b",
     "supply_concentration", "positive", 0.88),
    (r"\b(?:few|limited|scarce)\s+(?:global\s+)?(?:supplier|manufacturer|producer)s?\b.{0,40}"
     r"(?:world\s*wide|global(?:ly)?|across\s+(?:the\s+)?world|internationally)",
     "supply_concentration", "positive", 0.83),
    (r"\b(?:import\s+substit|indigenis|localiz)\w*.{0,40}"
     r"(?:leader|dominant|largest|only|biggest|pioneer)",
     "supply_concentration", "positive", 0.82),

    # Competitor capacity constraint — rivals at capacity too (validates constraint)
    # When a competitor says "our rival is fully booked too" = systemic shortage
    (r"\b(?:compet|rival|peer|industry).{0,50}"
     r"(?:also\s+)?(?:constrain|tight|limit|strain|at\s+(?:full|peak)\s+cap|fully\s+(?:book|allocat))",
     "competitor_constrained", "positive", 0.75),

    # Demand pull from customer side — customers ordering early due to scarcity
    # "Customers are placing orders 12 months in advance", "advance booking surge"
    (r"\b(?:customer|client)s?\b.{0,60}"
     r"(?:order(?:ing|ed)?\s+(?:ahead|early|in\s+advance|forward|long.lead)|"
     r"commit(?:ting|ted)\s+(?:capacity|production|allocation|future))",
     "demand_pull", "positive", 0.83),

    # ── POLICY SUPPORT ───────────────────────────────────────────────────
    # Government scheme / budgetary / policy support signals
    (r"\b(?:budget\s+(?:allocation|outlay|provision|support|boost)|"
     r"budgetary\s+(?:support|allocation|outlay))\b",
     "policy_support", "positive", 0.82),
    (r"\b(?:viability\s+gap\s+funding|VGF|capital\s+subsidy|interest\s+subvention|"
     r"government\s+(?:grant|subsidy|incentive|support|push|thrust))\b",
     "policy_support", "positive", 0.83),
    (r"\b(?:national\s+(?:mission|policy|programme|plan)|mission\s+shakti|"
     r"PM\s+(?:KUSUM|Gati\s+Shakti|MITRA|PRANAM|Surya\s+Ghar)|"
     r"Sagarmala|Bharatmala|UDAY|RDSS|DDUGJY)\b",
     "policy_support", "positive", 0.80),
    # MNRE (Ministry of New & Renewable Energy) — critical India policy driver
    (r"\bMNRE\b.{0,80}(?:approv|sanction|allocat|target|tender|award|notif|fund|GW|MW)",
     "policy_support", "positive", 0.84),
    # Union Budget capex signals — infrastructure push is investable theme
    (r"\b(?:union\s+budget|annual\s+budget)\b.{0,80}"
     r"(?:capex|capital\s+expenditure|infrastructure|allocat|outlay).{0,40}"
     r"(?:lakh\s+crore|trillion|billion|\d+\s*%)",
     "policy_support", "positive", 0.86),
    # Railways capex — direct order driver for Titagarh, RVNL, Texmaco etc.
    (r"\b(?:indian\s+railways?|railways?\s+(?:ministry|board|capex|invest))\b"
     r".{0,80}(?:crore|lakh|billion|wagon|locomotive|coach|electrif|loco|tender)",
     "policy_support", "positive", 0.83),
    # Defence indigenization — banned imports = captive demand for domestic suppliers
    (r"\b(?:indigenis|indigeniz|Make\s+in\s+India\s+defence|"
     r"defence\s+(?:indigenis|indigeniz|corridor|export|offset)|"
     r"positive\s+indigenisation\s+list|import\s+embargo\s+defence|"
     r"banned\s+(?:import|procurement)\s+list)\b",
     "policy_support", "positive", 0.87),

    # ── US MARKET: GUIDANCE & ALLOCATION SIGNALS ─────────────────────────
    # Revenue / earnings guidance — forward-looking management confidence
    (r"\b(?:we|the\s+company|management)\s+(?:expect|project|anticipate|forecast|"
     r"guide|guided|reiterate)\b.{0,60}"
     r"(?:revenue|sales|earnings|EPS|EBITDA|margin|growth).{0,40}"
     r"(?:\$|\d+\s*(?:billion|million|B\b|M\b)|%)",
     "guidance_revenue", "positive", 0.86),
    # "Visibility into" — management signaling multi-quarter order confidence
    (r"\b(?:visibility|confidence|comfort|clarity)\b.{0,40}"
     r"(?:into|through|for|over)\b.{0,40}"
     r"(?:next\s+(?:quarter|year|12\s*months|18\s*months)|"
     r"(?:Q[1-4]|FY)\s*2[0-9]|(?:fiscal|calendar)\s+20[0-9]{2})",
     "guidance_revenue", "positive", 0.83),
    # Customer allocation language — clearest pricing power signal for US tech
    (r"\b(?:allocat|ration|priorit)\w*\b.{0,60}"
     r"(?:customer|client|partner).{0,40}"
     r"(?:through\s+(?:Q[1-4]|H[12]|FY)|limit|capac|constrain)",
     "capacity_constraint_seller", "positive", 0.91),
    # "Sold through / booked through" — capacity committed to customers
    (r"\b(?:sold|booked|committed|locked|contracted)\b.{0,30}"
     r"(?:through|out\s+through|for)\b.{0,40}"
     r"(?:Q[1-4]|H[12]|(?:next|fiscal|coming)\s+(?:year|quarter|12|18|24))",
     "capacity_constraint_seller", "positive", 0.92),
    # Pricing power from constraint — margin expansion explicitly from price
    (r"\b(?:gross\s+margin|operating\s+margin|profitab)\w*\b.{0,60}"
     r"(?:expand|improve|higher|increas).{0,60}"
     r"(?:pric|mix|ASP|average\s+selling\s+price|pricing\s+power)",
     "pricing_power_emerging", "positive", 0.82),
    # "Customers placing orders in advance" — demand pull exceeding normal lead times
    (r"\b(?:customer|client)s?\b.{0,40}"
     r"(?:placing|placing\s+orders|ordering|booking|committing)\b.{0,40}"
     r"(?:in\s+advance|ahead|early|long.lead|future\s+delivery|future\s+need)",
     "capacity_constraint_seller", "positive", 0.88),
    # ═══════════════════════════════════════════════════════════════════════
    # TIER 1 SIGNALS — Multi-Decade Compounder Detection
    # These signals identify companies that will compound wealth for 5-20 years.
    # Peter Lynch found Taco Bell. Jhunjhunwala found Titan. Buffett found Coke.
    # They ALL had these patterns in management commentary BEFORE they were famous.
    # ═══════════════════════════════════════════════════════════════════════

    # ── ROIC & REINVESTMENT QUALITY ──────────────────────────────────────
    # The single most predictive signal for wealth creation over decades.
    # Companies earning >20% ROIC and reinvesting = exponential compounders.
    # Titan in 2003: "22% return on capital deployed in new stores" → found this.
    (r"\b(?:return\s+on\s+(?:incremental\s+|invested\s+)?capital|ROIC|ROCE)\b"
     r".{0,60}(?:\d{2,3}\s*%|(\d{2,3})\s*(?:percent|per\s*cent))",
     "roic_high_sustained", "positive", 0.88),
    (r"\b(?:capital\s+(?:efficiency|light|deployed|allocation)|asset.light)\b"
     r".{0,60}(?:generat|return|compounding|high|improv)",
     "roic_high_sustained", "positive", 0.82),
    (r"\b(?:reinvest|plough\s+back|retained\s+earnings|internal\s+accruals)\b"
     r".{0,60}(?:at\s+high|efficiently|compounding|back\s+into\s+(?:the\s+)?business)",
     "roic_reinvestment", "positive", 0.83),
    (r"\b(?:free\s+cash\s+flow|FCF)\b.{0,40}"
     r"(?:exceed|greater\s+than|convert|(\d{2,3})\s*%\s*of\s+(?:net\s+)?(?:earnings|profit))",
     "earnings_quality_high", "positive", 0.85),

    # ── COMPETITIVE MOAT SIGNALS ─────────────────────────────────────────
    # Brand, distribution, technology, switching costs — what makes a business
    # DURABLE. HDFC Bank's distribution moat, Titan's brand preference,
    # Asian Paints' distribution reach — all extractable from MD&A.
    (r"\b(?:brand\s+(?:preference|equity|loyalty|recognition|strength))\b"
     r".{0,60}(?:\d{1,3}\s*%|higher|stronger|leading|dominant|first\s+choice)",
     "competitive_moat", "positive", 0.86),
    (r"\b(?:switching\s+cost|customer\s+(?:stickiness|retention|lock.in|loyalty))\b"
     r".{0,60}(?:high|strong|significant|barrier|difficult\s+to\s+switch)",
     "competitive_moat", "positive", 0.83),
    (r"\b(?:distribution\s+(?:network|reach|advantage|depth|width)|"
     r"network\s+(?:effect|advantage)|ecosystem\s+(?:lock.in|advantage))\b"
     r".{0,60}(?:largest|deepest|strongest|unmatched|decades|built\s+over)",
     "competitive_moat", "positive", 0.82),
    (r"\b(?:pricing\s+power|ability\s+to\s+(?:raise|increase)\s+prices|"
     r"pass.?through\s+(?:costs?|inflation))\b"
     r".{0,60}(?:sustained|confirmed|demonstrated|years|customers\s+accept)",
     "competitive_moat", "positive", 0.84),
    (r"\b(?:only|sole|dominant|leading)\s+(?:domestic\s+)?(?:player|manufacturer|provider)\b"
     r".{0,40}(?:in\s+this|in\s+our|category|segment|niche)",
     "competitive_moat", "positive", 0.87),

    # ── MARKET SIZE EXPANSION (TAM) ──────────────────────────────────────
    # Multi-decade compounders sit in GROWING markets. The jewelry market grew
    # 8% annually in India for 20 years. Pharmaceutical market grew 12%.
    # These TAM signals identify companies with long runways ahead.
    (r"\b(?:market\s+(?:size|growing|growth)|TAM|addressable\s+market)\b"
     r".{0,80}(?:grow|expand|double|triple).{0,40}"
     r"(?:\d{1,3}\s*%\s*(?:CAGR|annually|per\s+year)|over\s+(?:next\s+)?(?:\d+|decade))",
     "tam_expansion_structural", "positive", 0.83),
    (r"\b(?:penetration\s+(?:rate|level)|per\s+capita\s+consumption|"
     r"underpenetrated|low\s+penetration)\b"
     r".{0,60}(?:opportunity|growing|room\s+to\s+grow|significant|large)",
     "tam_expansion_structural", "positive", 0.81),
    (r"\b(?:secular\s+(?:trend|growth|tailwind)|structural\s+(?:shift|opportunity|growth))\b"
     r".{0,60}(?:decade|multi.year|long.term|sustained|irreversible)",
     "tam_expansion_structural", "positive", 0.80),

    # ── MANAGEMENT QUALITY (CAPITAL ALLOCATION & LONG-TERM THINKING) ─────
    # The great investors identify management quality from HOW they communicate.
    # Buffett reads annual letters. Lynch attended store visits.
    # These patterns extract management quality signals from earnings calls.
    (r"\b(?:decade|multi.year|long.?term|10.?year|20.?year)\b"
     r".{0,60}(?:vision|thinking|strategy|investment|commitment|compounding|wealth)",
     "management_quality", "positive", 0.78),
    (r"\b(?:disciplined|prudent|measured|patient|conservative)\s+"
     r"(?:capital|investment|allocation|balance\s+sheet|approach|deployment)",
     "management_quality", "positive", 0.79),
    (r"\b(?:return\s+(?:to\s+)?shareholders|shareholder\s+(?:value|wealth|returns))\b"
     r".{0,60}(?:long.term|compounding|sustained|over\s+(?:decade|years))",
     "management_quality", "positive", 0.76),
    (r"\b(?:consistent|predictable|repeatable|sustained)\s+"
     r"(?:earnings|performance|delivery|execution|cash\s+flow|growth)",
     "management_quality", "positive", 0.77),

    # ── MARGIN SUSTAINABILITY (NOT TEMPORARY) ────────────────────────────
    # Temporary margin expansion ≠ quality. Sustained margins over 5+ years = quality.
    # Asian Paints: "maintained 20%+ margins for 15 years despite input cost cycles"
    (r"\b(?:gross\s+)?margin\b.{0,40}"
     r"(?:sustain|maintain|hold|preserve|consistent|structurally).{0,40}"
     r"(?:\d{1,3}\s*%|historically|over\s+(?:year|cycle|period))",
     "margin_sustainability", "positive", 0.84),
    (r"\b(?:historically|over\s+(?:the\s+)?(?:decade|years|cycle))\b"
     r".{0,60}(?:margin|profitab|returns|performance).{0,40}"
     r"(?:consistent|sustained|stable|maintained|above\s+\d{1,3}\s*%)",
     "margin_sustainability", "positive", 0.82),

    # ── POLICY TAILWIND SIGNALS (Tier 3) ─────────────────────────────────
    # Government policy creates time-bounded windows. PLI schemes, budget allocations,
    # import duties — these are 3-5 year windows for domestic manufacturers.
    # Already captured above, but ensure we have high-conviction PLI patterns.
    (r"\bPLI\b.{0,30}(?:beneficiary|approved|eligible|receiving|disburse)",
     "policy_support", "positive", 0.90),
    (r"\b(?:budget\s+outlay|budget\s+allocation)\b.{0,60}"
     r"(?:crore|billion|lakh).{0,30}(?:our\s+sector|our\s+industry|support)",
     "policy_support", "positive", 0.85),
]

# Pre-compiled at module import — shared across all SignalExtractor instances.
# Each entry is now a compiled Pattern object (not a raw string).
SIGNAL_PATTERNS: list[tuple[re.Pattern, str, str, float]] = [
    (re.compile(raw, re.IGNORECASE), sig_type, direction, conf)
    for raw, sig_type, direction, conf in _RAW_PATTERNS
]

_MONEY_RE = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s*(billion|million|trillion|B|M|bn|mn)?",
    re.IGNORECASE,
)

# Indian number format: Rs. 1,234.56 crore / INR 500 lakh / ₹1200 Cr
_MONEY_INR_RE = re.compile(
    r"(?:Rs\.?\s*|INR\s*|₹\s*)(\d[\d,]*(?:\.\d+)?)\s*"
    r"(crores?|lakhs?|Crs?\b|L\b|cr\b)",
    re.IGNORECASE,
)

# ── Theme-entity extractor for India signals ──────────────────────────────────
# When a signal fires, scan its context for known TECHNOLOGY/SECTOR keywords.
# The first match becomes entity_text so the signal is linked to the theme
# entity rather than just the filer company.
# Order matters: longer/more specific matches are listed first so they win
# over generic overlapping terms (e.g. "data center" before "data").
_THEME_ENTITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ── Highly specific (match first to avoid false positives) ──────────
    # Semiconductor components
    (re.compile(r"\bHBM\b|\bhigh.bandwidth\s+memory\b",  re.I), "HBM Memory"),
    (re.compile(r"\badvanced\s+packaging\b|\bCoWoS\b|\bSoIC\b", re.I), "Advanced Packaging"),
    (re.compile(r"\bEUV\b|\bextreme\s+ultraviolet\b",    re.I), "EUV Lithography"),
    (re.compile(r"\bAI\s+(?:chip|accelerator|processor|inference|GPU)\b", re.I), "AI Chip"),
    (re.compile(r"\bNAND\s+flash\b|\bNAND\b",            re.I), "NAND Flash"),
    (re.compile(r"\bDRAM\b",                             re.I), "DRAM Memory"),
    # Power & Grid components
    (re.compile(r"\bpower\s+transformer\b|\btransformer\s+manufactur\b", re.I), "Power Transformer"),
    (re.compile(r"\b(?:400|765|220|132)\s*kV\s*(?:transformer|substation)\b", re.I), "HV Transformer"),
    (re.compile(r"\btransmission\s+(?:line|tower|cable|conductor)\b", re.I), "Transmission Infrastructure"),
    (re.compile(r"\bsubstation\b",                       re.I), "Substation Equipment"),
    (re.compile(r"\bgrid.scale\s+storage|grid\s+battery\b", re.I), "Grid Storage"),
    (re.compile(r"\bsolar\s+(?:panel|module|cell|glass|wafer|PV)\b", re.I), "Solar PV"),
    (re.compile(r"\bwind\s+turbine\b|\bturbine\s+blade\b", re.I), "Wind Turbine"),
    # Railways & Defence
    (re.compile(r"\brailway\s+wagon|freight\s+wagon|gondola\s+wagon\b", re.I), "Railway Wagon"),
    (re.compile(r"\bVande\s+Bharat|train\s+18\b",        re.I), "Vande Bharat"),
    (re.compile(r"\blocomotive\b",                       re.I), "Locomotive"),
    (re.compile(r"\bdefence\s+(?:drone|UAV|missile|radar|electronics)\b", re.I), "Defence Electronics"),
    (re.compile(r"\bmilitary\s+(?:aircraft|helicopter)\b", re.I), "Military Aviation"),
    (re.compile(r"\bammunition\b|\bexplosive\b",          re.I), "Ammunition"),
    # Industrial & Manufacturing
    (re.compile(r"\bstainless\s+steel\b|\bspecialty\s+steel\b", re.I), "Specialty Steel"),
    (re.compile(r"\bCRGO\b|cold.rolled\s+grain.oriented\b", re.I), "CRGO Steel"),
    (re.compile(r"\bforging\b|\bforgings?\b",             re.I), "Forging"),
    (re.compile(r"\bcasting\b",                          re.I), "Casting"),
    (re.compile(r"\bprecision\s+(?:component|part|machining)\b", re.I), "Precision Components"),
    (re.compile(r"\bPCB\b|printed\s+circuit\s+board\b",  re.I), "PCB"),
    (re.compile(r"\bcompressor\b",                       re.I), "Compressor"),
    (re.compile(r"\bpump\b",                             re.I), "Pump"),
    (re.compile(r"\bvalve\b",                            re.I), "Valve"),
    (re.compile(r"\bcable\s+(?:manufactur|industry)\b|\bpower\s+cable\b", re.I), "Power Cable"),
    (re.compile(r"\bACC\s+battery\b|advanced\s+chemistry\s+cell\b", re.I), "ACC Battery"),
    (re.compile(r"\belectrolyzer\b|\bgreen\s+hydrogen\b", re.I), "Green Hydrogen"),
    # Pharma & Healthcare
    (re.compile(r"\bbulk\s+drug|API\b|active\s+pharmaceutical\b", re.I), "API/Bulk Drug"),
    (re.compile(r"\bbiologic|biosimilar\b",               re.I), "Biologics"),
    (re.compile(r"\bmedical\s+device\b",                  re.I), "Medical Device"),
    # Tech & Software
    (re.compile(r"\bdata\s+cent(?:er|re)s?\b",           re.I), "Data Center"),
    (re.compile(r"\bgenerative\s+ai\b",                  re.I), "Generative AI"),
    (re.compile(r"\bartificial\s+intelligence\b",        re.I), "Artificial Intelligence"),
    (re.compile(r"\bmachine\s+learning\b",               re.I), "Machine Learning"),
    (re.compile(r"\bcybersecurit\w+\b",                  re.I), "Cybersecurity"),
    (re.compile(r"\bcloud\b",                            re.I), "Cloud"),
    # ── Broader sector matches (lower priority) ──────────────────────────
    (re.compile(r"\belectric\s+vehicle\b|\bEV\s+(?:charging|manufactur|segment)\b", re.I), "Electric Vehicle"),
    (re.compile(r"\bspecialty\s+chem(?:ical)?s?\b",      re.I), "Specialty Chemicals"),
    (re.compile(r"\bsemiconductor\b",                    re.I), "Semiconductor"),
    (re.compile(r"\brenewable\s+energy\b",               re.I), "Renewable Energy"),
    (re.compile(r"\bagrochemic(?:al)?s?\b",              re.I), "Agrochemicals"),
    (re.compile(r"\baerospace\b",                        re.I), "Aerospace"),
    (re.compile(r"\bdefence|defense\b",                  re.I), "Defense"),
    (re.compile(r"\bautomotive\b",                       re.I), "Automotive"),
    (re.compile(r"\btextile\b",                          re.I), "Textiles"),
    (re.compile(r"\bpharmaceut\w+|pharma\b",             re.I), "Pharma"),
    (re.compile(r"\bsolar\b",                            re.I), "Solar"),
    (re.compile(r"\bwind\s+(?:energy|power|farm|project)\b", re.I), "Wind"),
    (re.compile(r"\bbattery\b",                          re.I), "Battery"),
    (re.compile(r"\blithium\b",                          re.I), "Lithium"),
    (re.compile(r"\bcement\b",                           re.I), "Cement"),
    (re.compile(r"\bsteel\b",                            re.I), "Steel"),
    (re.compile(r"\brobotics?\b",                        re.I), "Robotics"),
    (re.compile(r"\bfoundr(?:y|ies)\b",                  re.I), "Foundry"),
    (re.compile(r"\btransformer\b",                      re.I), "Transformer"),
    (re.compile(r"\bwafer\b",                            re.I), "Wafer"),
    (re.compile(r"\bbiotech\b",                          re.I), "Biotech"),
    (re.compile(r"\breal\s+estate\b",                    re.I), "Real Estate"),
    (re.compile(r"\bnbfc\b",                             re.I), "NBFC"),
    (re.compile(r"\bhealthcare\b|\bhospital\b",          re.I), "Healthcare"),
]

def _extract_theme_entity(context: str) -> str:
    """Find the most relevant theme entity in a signal's context window.

    Scans left-to-right through ordered patterns (specific → generic).
    Returns the canonical entity name or empty string if none found.
    """
    for pattern, entity_name in _THEME_ENTITY_PATTERNS:
        if pattern.search(context):
            return entity_name
    return ""


class SignalExtractor:
    """Extracts investment signals from financial document text using pre-compiled pattern rules."""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.min_confidence = cfg.get("min_confidence", 0.65)
        self.context_window = cfg.get("context_window_chars", 200)
        self.max_signals_per_doc = cfg.get("max_signals_per_doc", 100)
        # Cap text length scanned per document. SEC filings can be 500k+ chars;
        # signal-rich content is almost always within the first 80k chars.
        self.max_text_chars = cfg.get("max_text_chars_for_signals", MAX_TEXT_CHARS)

    def extract(self, text: str, document_id: int = None) -> list[InvestmentSignal]:
        """Extract all investment signals from document text."""
        if not text:
            return []

        # Truncate to signal-rich portion — avoids scanning boilerplate footnotes
        scan_text = text[:self.max_text_chars]

        signals: list[InvestmentSignal] = []

        for compiled_pattern, signal_type, direction, confidence in SIGNAL_PATTERNS:
            if confidence < self.min_confidence:
                continue
            for match in compiled_pattern.finditer(scan_text):
                start = match.start()
                ctx_start = max(0, start - self.context_window // 2)
                ctx_end = min(len(scan_text), match.end() + self.context_window // 2)
                context = scan_text[ctx_start:ctx_end].strip()

                value, unit = self._extract_amount(context)
                # Extract the theme entity (technology/sector keyword) from context.
                # This links the signal to WHAT it's about, not just WHO filed it.
                # e.g. "50% capacity expansion in Solar Glass" → entity_text="Solar"
                theme_entity = _extract_theme_entity(context)
                signals.append(InvestmentSignal(
                    signal_type=signal_type,
                    direction=direction,
                    confidence=confidence,
                    signal_value=value,
                    signal_unit=unit,
                    context_text=context,
                    entity_text=theme_entity,
                    extracted_by="rule",
                    position=start,
                ))

            if len(signals) >= self.max_signals_per_doc:
                break

        return self._deduplicate(signals)

    def _extract_amount(self, context: str) -> tuple[Optional[float], Optional[str]]:
        """Extract a monetary value from surrounding context."""
        match = _MONEY_RE.search(context)
        if match:
            raw = float(match.group(1))
            unit = (match.group(2) or "").lower()
            multiplier_map = {
                "trillion": 1e12, "billion": 1e9, "million": 1e6,
                "bn": 1e9, "mn": 1e6, "b": 1e9, "m": 1e6,
            }
            return raw * multiplier_map.get(unit, 1), "USD_" + (unit or "units")
        return None, None

    def _deduplicate(self, signals: list[InvestmentSignal]) -> list[InvestmentSignal]:
        """Remove near-duplicate signals (same type within 500-char window)."""
        seen: dict[str, int] = {}
        result = []
        for sig in sorted(signals, key=lambda s: -s.confidence):
            key = sig.signal_type
            if key not in seen or abs(sig.position - seen[key]) > 500:
                seen[key] = sig.position
                result.append(sig)
        return result

    def extract_batch(
        self, doc_texts: list[tuple[int, str]]
    ) -> dict[int, list[InvestmentSignal]]:
        """Extract signals from multiple documents. Returns {doc_id: [signals]}."""
        results = {}
        for doc_id, text in doc_texts:
            results[doc_id] = self.extract(text, document_id=doc_id)
        total = sum(len(v) for v in results.values())
        logger.info(f"Signal extraction: {total} signals from {len(doc_texts)} docs")
        return results

    def get_signal_summary(self, signals: list[InvestmentSignal]) -> dict:
        """Summarize signals: count by type and direction."""
        by_type: dict[str, dict] = {}
        for sig in signals:
            if sig.signal_type not in by_type:
                by_type[sig.signal_type] = {"positive": 0, "negative": 0, "neutral": 0, "total": 0}
            by_type[sig.signal_type][sig.direction] += 1
            by_type[sig.signal_type]["total"] += 1
        return by_type
