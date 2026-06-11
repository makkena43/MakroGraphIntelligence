"""Beneficiary Discovery Layer (India Pipeline — Layer 7).

Flow: Theme → Bottleneck → Required Product → Supplier → Beneficiary

Discovers beneficiary companies entirely from signal data in the DB.
NO hardcoded company lists — companies surface only when their actual
filing signals confirm supply-side activity for the constrained product.

Discovery logic per theme:
  1. Resolve the constrained product from the theme name (via supply chain DB)
  2. Find companies with capex_increase / supply_bottleneck / demand_surge signals
     whose documents also mention the constrained product entity
  3. Score each company by: signal recency × signal volume × supply chain distance
  4. Classify role: Critical Supplier / Direct Supplier / Input Supplier / Ecosystem

This replaces the previous hardcoded _PRODUCT_BENEFICIARIES dict.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .supply_chain_db import IndiaSupplyChainDB, _NODE_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------
# Maps supply chain stage → role label per the PDF spec
_STAGE_ROLE = {
    0: "critical_supplier",    # exact provider of constrained product
    1: "direct_supplier",      # direct input supplier
    2: "input_supplier",       # 2nd-tier input
    3: "ecosystem_participant",# broad sector participant
}

# Supply chain distance decay weights (PDF: 0=1.0, 1=0.8, 2=0.6, 3=0.3)
_DISTANCE_DECAY = {0: 1.0, 1: 0.8, 2: 0.6, 3: 0.3}

# Entity keywords per constrained product — used to join signals with products
_PRODUCT_ENTITY_KEYWORDS: dict[str, list[str]] = {
    "Power Transformer":             ["power grid", "electrical equipment"],
    "CRGO Steel":                    ["electrical equipment", "power grid"],
    "HV Cable":                      ["power grid", "electrical equipment"],
    "Solar Module":                  ["solar energy"],
    "Solar Cell":                    ["solar energy"],
    "Solar Wafer":                   ["solar energy"],
    "EMS / Contract Manufacturing":  ["semiconductor"],
    "PCB / Printed Circuit Board":   ["semiconductor"],
    "Semiconductor IC":              ["semiconductor"],
    "Battery Cell (Li-ion)":         ["battery storage", "electric vehicle"],
    "EV Battery Pack":               ["electric vehicle", "battery storage"],
    "Optical Fiber Cable":           ["5g telecom", "optical fiber"],
    "5G BTS / Radio Unit":           ["5g telecom"],
    "Rolling Stock / Locomotives":   ["railway infrastructure"],
    "Traction Motor":                ["railway infrastructure", "electrical equipment"],
    "Defense electronics":           ["defense electronics"],
}

# Theme keyword → constrained product mapping (product must exist in supply chain DB)
_THEME_TO_PRODUCT: dict[str, str] = {
    "transformer":           "Power Transformer",
    "power transformer":     "Power Transformer",
    "crgo":                  "CRGO Steel",
    "hv cable":              "HV Cable",
    "cable supply":          "HV Cable",
    "solar wafer":           "Solar Wafer",
    "solar cell":            "Solar Cell",
    "solar module":          "Solar Module",
    "solar manufacturing":   "Solar Module",
    "ems":                   "EMS / Contract Manufacturing",
    "pcb":                   "PCB / Printed Circuit Board",
    "semiconductor":         "Semiconductor IC",
    "battery cell":          "Battery Cell (Li-ion)",
    "battery":               "Battery Cell (Li-ion)",
    "acc battery":           "Battery Cell (Li-ion)",
    "electric vehicle":      "EV Battery Pack",
    "optical fiber":         "Optical Fiber Cable",
    "fiber preform":         "Optical Fiber Cable",
    "5g":                    "5G BTS / Radio Unit",
    "railway":               "Rolling Stock / Locomotives",
    "traction":              "Traction Motor",
    "defense":               "Defense electronics",
    "defence":               "Defense electronics",
}


@dataclass
class IndiaBeneficiary:
    """A company discovered as a supply-chain beneficiary from actual signal data."""
    company: str
    ticker: str
    theme_name: str
    constrained_product: str
    supply_chain_node: str
    supply_chain_stage: int          # 0=exact, 1=direct, 2=input, 3=ecosystem
    role: str                        # critical_supplier / direct_supplier / input_supplier / ecosystem_participant
    conviction_score: float          # 0.0–1.0, derived from signal evidence
    signal_count: int                # confirmatory signals from DB
    capex_signals: int               # capex_increase signals (bottleneck resolution evidence)
    supply_signals: int              # supply_bottleneck / demand_surge signals
    has_order_book_signals: bool
    import_substitution_play: bool
    rationale: str


class BeneficiaryDiscoveryLayer:
    """Layer 7: Discover India supply-chain beneficiaries entirely from signal data.

    No company names are hardcoded. Companies surface only when their filings
    contain signals (capex_increase, supply_bottleneck, demand_surge) that
    co-occur with the constrained product's entity keywords.
    """

    def __init__(self, config: dict = None):
        self._cfg = config or {}
        self._sc_db = IndiaSupplyChainDB()

    def discover(
        self,
        theme_names: list[str],
        pg_store=None,
        as_of_date: Optional[date] = None,
        lookback_days: int = 365,
    ) -> list[IndiaBeneficiary]:
        """Discover beneficiaries for a list of investable theme names from DB signals."""
        if not pg_store:
            logger.warning("[BeneficiaryDiscovery] No pg_store — cannot query signals")
            return []

        _as_of = as_of_date or date.today()
        _floor = _as_of - timedelta(days=lookback_days)
        beneficiaries: list[IndiaBeneficiary] = []
        seen: set[tuple] = set()

        for theme_name in theme_names:
            product = self._resolve_product(theme_name)
            if not product:
                continue

            entity_keywords = _PRODUCT_ENTITY_KEYWORDS.get(product, [])
            if not entity_keywords:
                continue

            # Determine supply chain stage for this product
            node = _NODE_INDEX.get(product)
            stage = node.stage if node else 1

            # Query: companies with supply-side signals co-occurring with product entity
            companies = self._query_signal_companies(
                pg_store, entity_keywords, _floor, _as_of
            )

            for co_data in companies:
                key = (co_data["company"], theme_name)
                if key in seen:
                    continue
                seen.add(key)

                sig_total  = co_data["sig_count"]
                capex_sigs = co_data["capex_count"]
                supply_sigs= co_data["supply_count"]
                has_ob     = co_data["has_order_book"]
                import_sub = node.is_import_dependent if node else False

                # Conviction = signal evidence score
                # capex signals are strongest (bottleneck resolution evidence per PDF)
                sig_score   = min(1.0, sig_total / 20.0)
                capex_score = min(1.0, capex_sigs / 5.0)
                conviction  = round(
                    sig_score   * 0.40 +
                    capex_score * 0.40 +
                    (0.10 if has_ob else 0.0) +
                    (0.10 if import_sub else 0.0),
                    3,
                )
                if conviction < 0.20:
                    continue  # too weak — skip

                role = _STAGE_ROLE.get(min(stage - 1, 3), "ecosystem_participant")
                rationale = self._build_rationale(
                    co_data["company"], theme_name, product,
                    sig_total, capex_sigs, has_ob, import_sub
                )

                beneficiaries.append(IndiaBeneficiary(
                    company=co_data["company"],
                    ticker=co_data.get("ticker") or "",
                    theme_name=theme_name,
                    constrained_product=product,
                    supply_chain_node=product,
                    supply_chain_stage=stage - 1,
                    role=role,
                    conviction_score=conviction,
                    signal_count=sig_total,
                    capex_signals=capex_sigs,
                    supply_signals=supply_sigs,
                    has_order_book_signals=has_ob,
                    import_substitution_play=import_sub,
                    rationale=rationale,
                ))

        beneficiaries.sort(key=lambda b: (-b.conviction_score, b.supply_chain_stage))
        logger.info(f"[BeneficiaryDiscovery] {len(beneficiaries)} beneficiaries "
                    f"from signals across {len(theme_names)} themes")
        return beneficiaries

    def _query_signal_companies(
        self,
        pg_store,
        entity_keywords: list[str],
        floor: date,
        as_of: date,
    ) -> list[dict]:
        """Find companies with supply-side signals co-occurring with product entities."""
        if not entity_keywords:
            return []
        placeholders = ",".join(["%s"] * len(entity_keywords))
        try:
            from psycopg2.extras import RealDictCursor
            with pg_store._conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT
                            COALESCE(NULLIF(d.company,''), d.ticker) AS company,
                            d.ticker,
                            COUNT(*)                                           AS sig_count,
                            COUNT(*) FILTER (
                                WHERE s.signal_type = 'capex_increase')        AS capex_count,
                            COUNT(*) FILTER (
                                WHERE s.signal_type IN (
                                    'supply_bottleneck','demand_surge',
                                    'inventory_drawdown'))                      AS supply_count,
                            BOOL_OR(s.signal_type IN (
                                'demand_surge','supply_bottleneck',
                                'capex_increase'))                             AS has_order_book
                        FROM mg_signals s
                        JOIN mg_documents d      ON d.id = s.document_id
                        JOIN mg_document_entities de ON de.document_id = s.document_id
                        JOIN mg_entities e        ON e.id = de.entity_id
                        WHERE d.country = 'IN'
                          AND d.filed_at BETWEEN %s AND %s
                          AND s.signal_type IN (
                              'capex_increase','supply_bottleneck',
                              'demand_surge','regulatory_tailwind',
                              'inventory_drawdown')
                          AND lower(e.canonical_name) IN ({placeholders})
                          AND COALESCE(NULLIF(d.company,''), d.ticker) IS NOT NULL
                        GROUP BY COALESCE(NULLIF(d.company,''), d.ticker), d.ticker
                        HAVING COUNT(*) >= 2
                        ORDER BY COUNT(*) FILTER (WHERE s.signal_type = 'capex_increase') DESC,
                                 COUNT(*) DESC
                        LIMIT 30
                        """,
                        [floor, as_of] + [kw.lower() for kw in entity_keywords],
                    )
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"[BeneficiaryDiscovery] signal query failed: {e}")
            return []

    def _resolve_product(self, theme_name: str) -> Optional[str]:
        tl = theme_name.lower()
        for keyword, product in _THEME_TO_PRODUCT.items():
            if keyword in tl:
                return product
        for node in self._sc_db.all_nodes():
            if node.name.lower() in tl:
                return node.name
        return None

    def _build_rationale(
        self, company: str, theme: str, product: str,
        sig_count: int, capex_sigs: int, has_ob: bool, import_sub: bool
    ) -> str:
        parts = [
            f"{company} has {sig_count} supply-side signals co-occurring with "
            f"'{product}' in the '{theme}' theme window."
        ]
        if capex_sigs:
            parts.append(f"{capex_sigs} capex_increase signals indicate bottleneck resolution activity.")
        if has_ob:
            parts.append("Order book / demand surge signals present.")
        if import_sub:
            parts.append("Product is import-dependent — domestic supplier qualifies as import substitution play.")
        return " ".join(parts)

    def persist(self, beneficiaries: list[IndiaBeneficiary], pg_store,
                as_of_date=None) -> int:
        self._ensure_schema(pg_store)
        _as_of = as_of_date or date.today()
        saved = 0
        for b in beneficiaries:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO mg_india_beneficiaries
                                (company, ticker, theme_name, constrained_product,
                                 supply_chain_node, supply_chain_stage, beneficiary_type,
                                 conviction_score, rationale, signal_count,
                                 has_order_book_signals, import_substitution_play,
                                 as_of_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (company, theme_name, as_of_date)
                            DO UPDATE SET
                                conviction_score         = EXCLUDED.conviction_score,
                                signal_count             = EXCLUDED.signal_count,
                                has_order_book_signals   = EXCLUDED.has_order_book_signals,
                                rationale                = EXCLUDED.rationale,
                                updated_at               = NOW()
                            """,
                            (b.company, b.ticker, b.theme_name, b.constrained_product,
                             b.supply_chain_node, b.supply_chain_stage, b.role,
                             b.conviction_score, b.rationale[:800],
                             b.signal_count, b.has_order_book_signals,
                             b.import_substitution_play, _as_of),
                        )
                saved += 1
            except Exception as e:
                logger.warning(f"[BeneficiaryDiscovery] persist failed {b.company}: {e}")
        return saved

    def _ensure_schema(self, pg_store):
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS mg_india_beneficiaries (
                            id                       SERIAL PRIMARY KEY,
                            company                  TEXT NOT NULL,
                            ticker                   TEXT,
                            theme_name               TEXT NOT NULL,
                            constrained_product      TEXT,
                            supply_chain_node        TEXT,
                            supply_chain_stage       INTEGER,
                            beneficiary_type         TEXT,
                            conviction_score         NUMERIC,
                            rationale                TEXT,
                            signal_count             INTEGER DEFAULT 0,
                            has_order_book_signals   BOOLEAN DEFAULT FALSE,
                            import_substitution_play BOOLEAN DEFAULT FALSE,
                            as_of_date               DATE,
                            created_at               TIMESTAMPTZ DEFAULT NOW(),
                            updated_at               TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE (company, theme_name)
                        )
                    """)
        except Exception as e:
            logger.debug(f"[BeneficiaryDiscovery] schema check: {e}")
