#!/usr/bin/env python3
"""Build a dated, company-originated product-and-role ledger.

The beneficiary mapper is intentionally theme-first: it can tell us which
companies are discussed near a constraint.  This module answers the different
question that must come first for a stock universe: *what does the company say
it makes, installs, or integrates?*

It uses only company documents filed on or before ``--as-of``.  Product labels
come from the database's product/entity vocabulary, never from a sector or
company list in code.  Exact issuer-to-product proof is kept separate from the
reviewed product-to-theme crosswalk: a family-level theme alias can no longer
erase a proven literal manufacturer, but the role still does not prove
scarcity or create Buy authority.

Usage:
    python3 scripts/stock_report/company_product_roles.py --as-of 2022-12-31
    python3 scripts/stock_report/company_product_roles.py --as-of 2022-12-31 --ticker ABC
    python3 scripts/stock_report/company_product_roles.py --as-of 2022-12-31 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from extract_report_data import connect, q  # noqa: E402
from constraint_ledger import fetch_constraint_aliases, normalize_product_label  # noqa: E402
from company_capabilities import (  # noqa: E402
    MFG_EVIDENCE,
    STRICT_MFG_ASSET,
    _document_role_candidate_detail,
    candidate_terms,
    gate_terms_by_corpus_frequency,
)
from src.makrograph.nlp.product_quality import is_product_label  # noqa: E402
from src.makrograph.constraint_contract import (  # noqa: E402
    COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION,
    product_identity_constraint_key,
)


# These are generic disclosure verbs and assets, not products or sectors.
# The separate patterns deliberately distinguish an owned manufacturing role
# from an EPC, installation, or system-integration role.
EPC_OR_INSTALL_RE = re.compile(
    r"\b(?:epc|turnkey|install(?:ation|ed|ing)?|deploy(?:ment|ed|ing)?|"
    r"commission(?:ing|ed)?|execute(?:d|s|ing)?)\b", re.I)
INTEGRATOR_RE = re.compile(r"\b(?:system\s+integrat(?:or|ion|ed|ing)|integrat(?:e|ed|ing))\b", re.I)
INPUT_SUPPLIER_RE = re.compile(r"\b(?:supply|supplier|vendor|component\s+provider)\b", re.I)
MFG_RE = re.compile(MFG_EVIDENCE, re.I)
# Commissioning and operating are valid physical/capability anchors in the
# broad mapper, but they are not production verbs.  The strict role ledger
# keeps them in EPC/installation and plant-state lanes respectively.
OWNED_MAKING_ACTION_RE = re.compile(
    r"\b(?:manufactur(?:e|ed|es|ing)|produc(?:e|ed|es|ing)|"
    r"fabricat(?:e|ed|es|ing|ion)|assembl(?:e|ed|es|ing|y))\b", re.I)
STRICT_ASSET_RE = re.compile(STRICT_MFG_ASSET, re.I)
POLICY_OR_DEMAND_RE = re.compile(
    r"\b(?:tender|order\s*book|order|demand|subsid(?:y|ies)|policy|scheme|"
    r"procurement|award)\b", re.I)
EARNINGS_CAPTURE_RE = re.compile(
    r"\b(?:key\s+orders?\s+booked|order\s+intake|order\s*book|"
    r"orders?\s+(?:booked|received|secured|won|executed|worth)|"
    r"revenues?\s+(?:from|comprised|included|generated\s+by|derived\s+from)|"
    r"sales?\s+of|commercial\s+sales?|dispatch(?:es|ed)?|"
    r"capacity\s+utili[sz]ation|sold\s+to|customer\s+qualification)\b", re.I)
DIRECT_COMMERCIAL_RE = re.compile(
    r"\b(?:key\s+orders?\s+booked|order\s+intake|order\s*book|"
    r"orders?\s+(?:booked|received|secured|won)|"
    r"revenues?\s+(?:from|comprised|included|generated\s+by|derived\s+from)|sales?\s+of|"
    r"commercial\s+sales?|dispatch(?:es|ed)?|sold\s+to)\b", re.I)

# Common disclosure language is excluded solely to stop labels such as
# "annual report", "manufacturing facility", or "company has" from becoming
# products.  It intentionally contains no industry, product, theme, or company
# vocabulary.
NON_PRODUCT_TOKENS = frozenset(
    "a an and are as at be been being by company companies for from has have in "
    "is it its of on or our that the their this to was were will with within "
    "manufacture manufacturing manufactured manufacturer manufacturers produce "
    "production produced producing fabricate fabrication fabricated assemble "
    "assembly assembled plant plants facility facilities factory factories unit "
    "units capacity capacities line lines project projects business businesses "
    "market markets products product services service operations operation annual "
    "report presentation company group subsidiary subsidiaries technology systems "
    "equipment solution solutions development developed developing new existing "
    "growth expansion addition increase including range whole various strong "
    "leading pioneer efficient energy customer customers government india indian "
    "under through more than per year years fy fiscal crore lakh million global "
    "standard standards quality compliance wholly owned finished good goods".split()
    + "reportable segment segments identity number numbers order book books middle east west north south".split()
    + "revenue profit pat ebitda ebit margin margins cash flow flows equity share shares capital issue "
      "financial finance result results income expenditure expense expenses depreciation interest tax earnings sales "
      "exceptional item items debt free total comprehensive net gross paid unpaid credit other scheme "
      "schemes retirement voluntary program programme".split()
)

LOOKBACK_DAYS = 1095
MIN_PRODUCT_WORDS = 2
MAX_PRODUCT_WORDS = 5
MAX_EVIDENCE = 3
MIN_INDEPENDENT_EVENT_GAP_DAYS = 45
ENTITY_PRODUCT_MIN_DOCUMENTS = 3
ENTITY_PRODUCT_MIN_ISSUERS = 2
# Version the extractor whenever role precision changes.  A stored v1 record
# may have been generated before the issuer-ownership and end-use guards; it
# must not reappear as a fresh review candidate merely because the code was
# upgraded.  Rebuilds are explicit and point-in-time.
EXTRACTION_METHOD = COMPANY_PRODUCT_ROLE_EXTRACTOR_VERSION

# These machine states are deliberately more specific than the coarse
# DISCOVERY/EVIDENCED extraction state.  They let the selector automatically
# promote strong literal role evidence and dispose of weak mapper/lexical hits
# without turning every row into an analyst assignment.
AUTO_REJECTED_NO_ROLE = "AUTO_REJECTED_NO_ROLE"
AUTO_REJECTED_WRONG_ROLE = "AUTO_REJECTED_WRONG_ROLE"
QUARANTINED_AMBIGUOUS_ROLE = "QUARANTINED_AMBIGUOUS_ROLE"
QUARANTINED_ADJACENT_PRODUCT = "QUARANTINED_ADJACENT_PRODUCT"
QUARANTINED_UNLINKED_PRODUCT = "QUARANTINED_UNLINKED_PRODUCT"
EXACT_ROLE_EVIDENCED = "EXACT_ROLE_EVIDENCED"
PIPELINE_EVIDENCED = "PIPELINE_EVIDENCED"
OPERATING_PRODUCER_EVIDENCED = "OPERATING_PRODUCER_EVIDENCED"
EARNINGS_CAPTURE_EVIDENCED = "EARNINGS_CAPTURE_EVIDENCED"


def automatic_role_adjudication(
    *, role_type: str, role_state: str, link_type: str,
    physical_count: int, pipeline_count: int, earnings_capture_count: int,
) -> dict[str, Any]:
    """Return a deterministic company-product promotion state.

    Exact product scope, repeated issuer-owned role evidence and the economic
    role are separate gates.  This function never uses a company or theme
    allowlist and never looks beyond the report date.
    """
    if link_type == "UNLINKED":
        return {
            "state": QUARANTINED_UNLINKED_PRODUCT,
            "reason": "issuer product role found, but no reviewed exact constraint crosswalk exists",
            "missing_evidence": ["exact product-to-constraint crosswalk"],
            "producer_eligible": False,
        }
    if link_type not in {"EXACT", "EXACT_PRODUCT"}:
        return {
            "state": QUARANTINED_ADJACENT_PRODUCT,
            "reason": "the reviewed crosswalk is adjacent or family-level, not the exact constrained product",
            "missing_evidence": ["exact product scope"],
            "producer_eligible": False,
        }
    if role_state != "EVIDENCED":
        return {
            "state": QUARANTINED_AMBIGUOUS_ROLE,
            "reason": "literal product evidence exists, but repeated issuer-owned role proof is incomplete",
            "missing_evidence": ["second non-duplicative issuer role event"],
            "producer_eligible": False,
        }
    if role_type in {"EPC_OR_INSTALLER", "SYSTEM_INTEGRATOR"}:
        return {
            "state": AUTO_REJECTED_WRONG_ROLE,
            "reason": "exact deployment/integration role is not producer proof for this constraint",
            "missing_evidence": [],
            "producer_eligible": False,
        }
    if role_type == "DIRECT_ROLE_UNCLASSIFIED" and pipeline_count:
        return {
            "state": PIPELINE_EVIDENCED,
            "reason": "repeated exact-product capacity or manufacturing-plan evidence",
            "missing_evidence": ["commissioning and operating production"],
            "producer_eligible": True,
        }
    if role_type == "MANUFACTURER" and physical_count >= 2:
        if earnings_capture_count >= 2:
            return {
                "state": EARNINGS_CAPTURE_EVIDENCED,
                "reason": "operating producer proof plus repeated same-product commercial capture evidence",
                "missing_evidence": ["underwriting, valuation, liquidity and entry review"],
                "producer_eligible": True,
            }
        return {
            "state": OPERATING_PRODUCER_EVIDENCED,
            "reason": "repeated exact-product issuer-owned physical manufacturing proof",
            "missing_evidence": ["same-product revenue, orders or utilisation capture"],
            "producer_eligible": True,
        }
    if role_type == "DIRECT_PRODUCT_SUPPLIER":
        if earnings_capture_count >= 2:
            return {
                "state": EARNINGS_CAPTURE_EVIDENCED,
                "reason": "repeated issuer-owned exact-product orders, sales or dispatch evidence",
                "missing_evidence": [
                    "operating manufacture versus outsourced supply, then underwriting, valuation and risk"
                ],
                "producer_eligible": True,
            }
        return {
            "state": EXACT_ROLE_EVIDENCED,
            "reason": "repeated issuer-owned exact-product supply role without repeated commercial capture",
            "missing_evidence": ["same-product orders, sales, revenue or dispatch capture"],
            "producer_eligible": False,
        }
    if role_type == "INPUT_SUPPLIER":
        return {
            "state": EXACT_ROLE_EVIDENCED,
            "reason": "exact input-supplier role is evidenced, but the company is not the constrained-product producer",
            "missing_evidence": ["positive economic transmission and earnings capture"],
            "producer_eligible": False,
        }
    return {
        "state": QUARANTINED_AMBIGUOUS_ROLE,
        "reason": "exact product association lacks qualifying operating or pipeline producer proof",
        "missing_evidence": ["owned physical production or a dated capacity pipeline"],
        "producer_eligible": False,
    }


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def normalized_product(value: str | None) -> str:
    """A conservative normal form for literal product matching.

    Unlike a semantic alias, this only makes plural spelling and punctuation
    stable.  It must never decide that two economically adjacent products are
    interchangeable.
    """
    key = normalize_product_label(value)
    return " ".join(_singular(token) for token in key.split())


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",))
    return bool(cur.fetchone()["name"])


def _valid_product_label(value: str | None) -> bool:
    # Commas and semicolons generally mean an entity extractor captured a list
    # rather than one product noun phrase ("oil, gas and mining"), so it is
    # not safe vocabulary for literal company-role matching.
    if re.search(r"[,;]", value or ""):
        return False
    # Entity extraction sometimes captures an exchange or issuer web address
    # that happens to sit near a real manufacturing disclosure.  Domains are
    # never products, regardless of the sector or issuer involved.
    if re.search(r"\b(?:https?[:.]|www[.\s])|\.(?:com|in|org|net)\b", value or "", re.I):
        return False
    if not is_product_label(value, min_words=MIN_PRODUCT_WORDS, max_words=MAX_PRODUCT_WORDS):
        return False
    words = normalized_product(value).split()
    if not (MIN_PRODUCT_WORDS <= len(words) <= MAX_PRODUCT_WORDS):
        return False
    if any(word in NON_PRODUCT_TOKENS or len(word) < 2 for word in words):
        return False
    return len(set(words) - NON_PRODUCT_TOKENS) >= 1


def product_vocabulary(cur, as_of: date, country: str = "IN") -> dict[str, str]:
    """Return product names already discovered by data pipelines as of date.

    Entity products are candidates, not role proof.  They become relevant only
    after the same phrase appears in an issuer's role-bearing disclosure.  This
    gives the generic role layer access to new product chains without smuggling
    a product dictionary into code.
    """
    raw: list[str] = []
    if _table_exists(cur, "mg_entities"):
        # Entity extraction is exploratory.  A single issuer's financial table
        # can otherwise label an arbitrary phrase as PRODUCT ("PAT Margin", a
        # ticker name, etc.).  Requiring independent issuer usage makes a
        # novel phrase a market research lead rather than one parser artefact.
        # Known constraint/alias labels below remain eligible even if one
        # issuer is the only early source.
        raw.extend(row["label"] for row in q(cur, """
            SELECT COALESCE(NULLIF(BTRIM(e.canonical_name), ''), BTRIM(e.entity_text)) AS label
            FROM mg_entities e
            JOIN mg_document_entities de ON de.entity_id = e.id
            JOIN mg_documents d ON d.id = de.document_id
            WHERE e.entity_type = 'PRODUCT' AND d.country = %s
              AND COALESCE(e.metadata->>'source', '') LIKE 'generic_manufacturing_phrase_v%%'
              AND d.ticker IS NOT NULL AND BTRIM(d.ticker) <> ''
              AND d.filed_at::date <= %s
              AND COALESCE(e.first_seen_at, e.created_at::date) <= %s
            GROUP BY e.id, e.canonical_name, e.entity_text
            HAVING COUNT(DISTINCT d.id) >= %s
               AND COUNT(DISTINCT UPPER(TRIM(d.ticker))) >= %s
        """, (country, as_of, as_of, ENTITY_PRODUCT_MIN_DOCUMENTS,
               ENTITY_PRODUCT_MIN_ISSUERS)) if row.get("label"))
    if country == "IN":
        raw.extend(row["label"] for row in q(cur, """
            SELECT DISTINCT constrained_product AS label
            FROM mg_india_beneficiaries
            WHERE constrained_product IS NOT NULL AND BTRIM(constrained_product) <> ''
              AND as_of_date <= %s
        """, (as_of,)) if row.get("label"))
    if _table_exists(cur, "mg_constraint_product_aliases"):
        raw.extend(row["label"] for row in q(cur, """
            SELECT product_label AS label
            FROM mg_constraint_product_aliases
            WHERE country = %s AND status <> 'REJECTED'
              AND (first_seen_date IS NULL OR first_seen_date <= %s)
              AND (effective_from IS NULL OR effective_from <= %s)
              AND (effective_to IS NULL OR effective_to >= %s)
        """, (country, as_of, as_of, as_of)) if row.get("label"))
    if _table_exists(cur, "mg_policy_product_discoveries"):
        # A product phrase in an issuer's dated policy disclosure is a useful
        # early-discovery lead even before two unrelated issuers use the same
        # phrase.  It remains only vocabulary here: the strict role extractor
        # below must separately find an issuer-owned manufacturing disclosure,
        # and no policy-discovered phrase can create a constraint or Buy gate.
        raw.extend(row["label"] for row in q(cur, """
            SELECT DISTINCT product_label AS label
            FROM mg_policy_product_discoveries
            WHERE country=%s AND as_of_date <= %s
        """, (country, as_of)) if row.get("label"))

    # A canonical display label is chosen mechanically: shortest spelling,
    # then lexical.  The evidence still retains the original filing phrase.
    grouped: dict[str, list[str]] = defaultdict(list)
    for label in raw:
        if _valid_product_label(label):
            grouped[normalized_product(label)].append(re.sub(r"\s+", " ", label).strip())
    return {key: sorted(set(labels), key=lambda item: (len(item), item.casefold()))[0]
            for key, labels in grouped.items()}


ROLE_BREAK_RE = re.compile(
    r"\b(?:for|to|by|from|with|using|market|demand|customer|scheme|policy|"
    r"procurement|tender|installation|deployment)\b", re.I)
ISSUER_CONTEXT_RE = re.compile(
    r"\b(?:our|we|the company|company's|subsidiar\w*)\b", re.I)
NON_COMMERCIAL_CONTEXT_RE = re.compile(
    r"\b(?:corporate\s+social\s+responsibility|\bcsr\b|governance|school|"
    r"medical|community|charit(?:y|able))\b", re.I)
MARKET_ASSERTION_RE = re.compile(
    r"\b(?:we\s+(?:believe|estimate|expect)|manufacturers?\s+of)\b", re.I)
# A filing frequently describes *somebody else's* plant, announced capacity,
# product market, or end-use.  These are language roles, not sector/product
# exceptions: they prevent an issuer that makes solar glass from being called
# a solar-module maker, or an issuer discussing industry plans from becoming
# the manufacturer in the report.
THIRD_PARTY_CONTEXT_RE = re.compile(
    r"\b(?:industry|market|customers?|successful\s+bidders?|third[-\s]party|"
    r"other\s+(?:companies|manufacturers|producers)|competitors?|developers?)\b", re.I)
COMMERCIAL_MARKET_CONTEXT_RE = re.compile(
    r"\b(?:industry|market|successful\s+bidders?|third[-\s]party|"
    r"other\s+(?:companies|manufacturers|producers)|competitors?|developers?)\b", re.I)
PIPELINE_CONTEXT_RE = re.compile(
    r"\b(?:plan(?:s|ned)?\s+to|intend(?:s|ed)?\s+to|aim(?:s|ed)?\s+to|"
    r"propos(?:e|ed|al)|set(?:ting)?\s+up|under\s+construction|"
    r"feasibility|memorandum|\bmou\b|term\s+sheet|application\s+submitted)\b", re.I)
END_USE_CONTEXT_RE = re.compile(
    r"\b(?:used\s+(?:in|for|by)|for\s+use\s+(?:in|with)|input(?:s)?\s+"
    r"(?:for|to)|component(?:s)?\s+(?:for|of)|demand\s+for|"
    r"cater(?:ing|s|ed)?\s+to|serv(?:ing|es|ed)\s+(?:the\s+)?(?:user\s+)?industr(?:y|ies))\b", re.I)
SERVICE_OR_ADVISORY_CONTEXT_RE = re.compile(
    r"\b(?:maintenance|repair(?:s|ing)?|consult(?:ancy|ing|ation)?|inspection|"
    r"project\s+management|advisory|service\s+contract)\b", re.I)
BUYER_OR_INPUT_CONTEXT_RE = re.compile(
    r"\b(?:procur(?:e|ed|es|ing|ement)|purchas(?:e|ed|es|ing)|buy(?:s|ing)?|"
    r"sourc(?:e|ed|es|ing)|input(?:s)?|raw\s+materials?|commodity|"
    r"requirements?|prices?|costs?)\b", re.I)
CAPABILITY_NOT_ROLE_RE = re.compile(
    r"\b(?:capacity|capable|capability|ability|potential|sufficient)\b.{0,80}?\b"
    r"(?:to\s+)?(?:manufacture|produce|fabricate|assemble)\b", re.I | re.S)
CLAUSE_BREAK_RE = re.compile(r"[.;:\n]+")
NEGATED_MANUFACTURING_RE = re.compile(
    r"\b(?:not|non[-\s]?|no)\s+(?:a\s+)?(?:manufacturing|manufacturer|manufacture)\b", re.I)
TRANSACTION_CONTEXT_RE = re.compile(
    r"\b(?:acquir(?:e|ed|ing|ition)|target\s+company|investee|purchase\s+of\s+shares?|"
    r"share\s+capital|stake\s+in|business\s+transfer)\b", re.I)
# Aspirational entry / diversification language — "targeting to become one of the
# largest X manufacturer", "mega foray into", "plans to enter". A stated ambition
# to become a maker is a pipeline lead, never operating proof. Distinct from
# PIPELINE_CONTEXT_RE, which omits these aspirational verbs.
_ASPIRATIONAL_ROLE_RE = re.compile(
    r"\b(?:target(?:s|ed|ing)?\s+to\s+become|aim(?:s|ed|ing)?\s+to\s+become|"
    r"to\s+become\s+(?:one\s+of\s+the\s+)?(?:the\s+)?(?:largest|leading|a\s+)|"
    r"(?:mega\s+)?foray\s+into|foray\b|plans?\s+to\s+enter|enter(?:ing)?\s+the\s+\w+\s+"
    r"(?:manufactur|business|space|segment)|aspir\w+\s+to|announce[sd]?\s+.{0,30}\bentry\b)\b",
    re.I)


def _local_clause(text: str, position: int) -> str:
    """Return the short sentence/bullet carrying a role claim."""
    left = 0
    for match in CLAUSE_BREAK_RE.finditer(text):
        if match.end() > position:
            break
        left = match.end()
    right_match = CLAUSE_BREAK_RE.search(text, position)
    right = right_match.start() if right_match else len(text)
    return text[left:right]


def _issuer_owns_action(product_context: str, action_start: int, action_end: int) -> bool:
    """Require an issuer subject close to the making action.

    The old extractor required only a product, a manufacturing word, and a
    physical-asset phrase.  That lets market commentary inherit the issuer's
    role.  An automatic operating-maker record needs the issuer to own the
    verb in the same sentence/bullet; ambiguous corporate language remains a
    research discovery for human review.
    """
    clause = _local_clause(product_context, action_start)
    local_start = product_context.find(clause)
    action_in_clause = max(0, action_start - local_start)
    # The issuer must precede and own the action ("the Company manufactures").
    # A later "our performance" or similar commentary must not retroactively
    # turn a statement about what the capacity *could* make into an issuer
    # manufacturing claim.
    issuer_before = [match for match in ISSUER_CONTEXT_RE.finditer(clause)
                     if match.end() <= action_in_clause and action_in_clause - match.end() <= 140]
    if not issuer_before:
        return False
    # A direct issuer marker overrides broad contextual words only when it is
    # local to the action.  "successful bidders ... manufacturing" otherwise
    # has no issuer proof and must fail this gate.
    before_action = clause[max(0, action_in_clause - 100):action_in_clause]
    if THIRD_PARTY_CONTEXT_RE.search(before_action) and not ISSUER_CONTEXT_RE.search(before_action):
        return False
    return True


def _bridge_rejects_product_role(bridge: str) -> bool:
    """Reject product/end-use relationships rather than manufacturer claims."""
    return bool(ROLE_BREAK_RE.search(bridge) or END_USE_CONTEXT_RE.search(bridge) or "," in bridge)


def _direct_manufacturing_binding(product_context: str, product_start: int, product_end: int) -> bool:
    """Require the product to be the grammatical object of a making action.

    This rejects ``manufacturing solar structures *for* solar panels`` and
    ``solar-pump market ... pumps manufacturing facility`` while preserving
    ``manufacturing of solar pumps`` and ``solar pumps manufacturing unit``.
    It is a language rule, not a product exception.
    """
    for action in OWNED_MAKING_ACTION_RE.finditer(product_context):
        if action.end() <= product_start:
            bridge = product_context[action.end():product_start]
        elif product_end <= action.start():
            bridge = product_context[product_end:action.start()]
        else:
            bridge = ""
        # Manufacturer proof needs a tight grammatical object relationship.
        # A long comma-separated project list otherwise lets the first item
        # inherit the manufacturing verb belonging to a later item.
        if len(bridge) > 60 or _bridge_rejects_product_role(bridge):
            continue
        if not _issuer_owns_action(product_context, action.start(), action.end()):
            continue
        # A stated future facility is a useful direct-role discovery, but it
        # is not an operating manufacturer.  Pipeline claims fall through to
        # DIRECT_ROLE_UNCLASSIFIED below and cannot enter a maker universe.
        clause = _local_clause(product_context, action.start())
        if PIPELINE_CONTEXT_RE.search(clause) or CAPABILITY_NOT_ROLE_RE.search(clause):
            continue
        # Aspirational entry ("targeting to become one of the largest ...
        # manufacturer", "mega foray into") and capacity/role attributed to a
        # NAMED group/sister entity are not the issuer's operating proof.
        if _ASPIRATIONAL_ROLE_RE.search(clause) or _GROUP_ATTRIBUTION_RE.search(product_context):
            continue
        if not (STRICT_ASSET_RE.search(product_context) or MFG_RE.search(product_context)):
            continue
        return True
    return False


def _direct_owned_asset_binding(
    product_context: str, product_start: int, product_end: int,
) -> bool:
    """Recognize an issuer-owned product plant expressed without an action verb.

    Corporate profiles often say ``it has a solar-cell manufacturing plant``.
    The asset is operating ownership evidence even though ``manufactures`` is
    absent.  A plan (``plans to set up``) does not match the ownership verb,
    and third-party/transaction contexts are rejected by the surrounding role
    classifier.
    """
    for asset in STRICT_ASSET_RE.finditer(product_context):
        if asset.end() <= product_start:
            distance = product_start - asset.end()
        elif product_end <= asset.start():
            distance = asset.start() - product_end
        else:
            distance = 0
        if distance > 180:
            continue
        left = max(0, min(asset.start(), product_start) - 180)
        right = min(len(product_context), max(asset.end(), product_end) + 80)
        local = product_context[left:right]
        owner = re.search(
            r"\b(?:our|we|the\s+company|company's|subsidiar\w*|it)\b"
            r".{0,80}?\b(?:has|have|owns?|operates?|runs?|maintains?)\b",
            local,
            re.I | re.S,
        )
        pipeline = PIPELINE_CONTEXT_RE.search(local)
        asset_in_local = asset.start() - left
        if (not owner or MARKET_ASSERTION_RE.search(local) or
                THIRD_PARTY_CONTEXT_RE.search(local[:owner.start()]) or
                _ASPIRATIONAL_ROLE_RE.search(local) or _GROUP_ATTRIBUTION_RE.search(local) or
                (pipeline and pipeline.start() < asset_in_local)):
            continue
        return True
    return False


# A concrete, quantified operating capacity for the product — "Cells: 500 MW",
# "installed capacity of 2 GW", "1,220 MW module capacity". A stated numeric
# nameplate/installed capacity IS operating-maker evidence: you cannot report a
# figure in MW/GW/GWh/MTPA/tonnes for a product you do not make. This is the
# distinct signal that investor-presentation capacity tables carry and prose
# making-verbs do not — many genuine operating producers (e.g. integrated solar
# cell/module makers) disclose scale as a capacity table, not "we manufacture X".
_CAPACITY_QTY_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*"
    r"(?:mw|mwp|gw|gwp|gwh|mwh|ktpa|mtpa|tpa|mt|tonnes?|tons?|"
    r"units?\s*(?:/|per)\s*(?:year|annum|month)|nos\.?)\b", re.I)
# "capacity to manufacture" (no number) is capability, not proof; the numeric
# guard above already separates it. These reject a capacity figure that belongs
# to the market/nation or is a cost/utilisation metric rather than own nameplate.
_CAPACITY_AGGREGATE_RE = re.compile(
    r"\b(?:india'?s|nation(?:al|wide|'?s)?|country'?s|domestic|global|world(?:wide)?|"
    r"industry|market|sector|combined\s+industry)\b", re.I)
_CAPACITY_NON_MAKING_RE = re.compile(
    r"\bcapacity\s+(?:utili[sz]ation|charges?|charge|factor|payment|cost)\b", re.I)
# Generation / EPC / project context: a developer reporting MW of solar it has
# INSTALLED or GENERATES (a power plant) is not a module/cell MAKER. Reject the
# capacity figure when the local frame is generation/deployment, not factory
# output. (A concrete "manufacturing/production/fab/nameplate" cue in the same
# window overrides this — see the require-manufacturing check below.)
_CAPACITY_GENERATION_RE = re.compile(
    r"\b(?:solar\s+(?:park|plant|project|farm)|power\s+(?:plant|project|park)|"
    r"based\s+power|generation|generat\w+|"
    r"\bppa\b|off[-\s]?take|epc\b|o\s*&\s*m|independent\s+power|"
    r"install(?:ed|ation)?\s+(?:at|of\s+\d[\d,.]*\s*mw(?:p)?\s+(?:solar\s+)?(?:project|plant|park))|"
    r"operational\s+(?:solar\s+)?(?:assets?|projects?|portfolio)|renewable\s+(?:assets?|portfolio))\b",
    re.I)
# Future/announced capacity is a pipeline lead, not operating proof. Broader
# than the shared PIPELINE_CONTEXT_RE (which omits "establish/build/commission"),
# scoped to the capacity binding so it never loosens other role paths.
_CAPACITY_FUTURE_RE = re.compile(
    r"\b(?:to\s+(?:establish|build|set\s*up|commission|add|create|develop|become)|"
    r"establish\w*|greenfield|upcoming|propos\w+|target(?:s|ed|ing)?|aim(?:s|ed|ing)?|"
    r"aspir\w+|plann?(?:ed|ing)|intend\w*|foray|mega\s+foray|expected\s+to|envisag\w+|"
    r"will\s+(?:have|be|add|establish|commission|reach|become)|by\s+(?:fy\s*)?20\d{2}|"
    r"under\s+implementation|being\s+set\s+up|to\s+be\s+(?:commissioned|set\s+up|operational|"
    r"one\s+of\s+the\s+largest))\b",
    re.I)
# A capacity attributed to a NAMED group/sister entity (not the issuer) is not
# the issuer's operating proof. "Inox Clean, through its subsidiary Inox Solar"
# reads as issuer-owned via the bare "its subsidiary", but the actor is a
# different named company. Reject when a proper-noun corporate actor other than
# a bare pronoun owns the capacity clause.
_GROUP_ATTRIBUTION_RE = re.compile(
    r"\b(?:through\s+its\s+(?:other\s+)?subsidiar\w*\s+[A-Z]|"
    r"[A-Z][A-Za-z]+\s+(?:Clean|Solar|Energy|Green|Neo|Power|Group)\b[^.]{0,60}"
    r"(?:subsidiar\w*|manufactur\w*|targeting|to\s+become)|"
    r"group'?s?\s+(?:foray|entry|venture)|sister\s+(?:concern|company))\b")


def _direct_capacity_binding(product_context: str, product_start: int, product_end: int) -> bool:
    """Recognize an issuer-owned, quantified operating capacity for the product.

    Accepts "Capacity: Cells: 500 MW", "our cell manufacturing capacity of 2 GW",
    "1,220 MW module capacity". Rejects: pipeline/announced capacity ("to set up
    0.8 GW"), market/national aggregate capacity ("India's 50 GW capacity"),
    capacity-utilisation / capacity-charge metrics, and third-party plants. A
    capacity figure is only counted when it sits in the issuer's own disclosure
    and is not framed as future/planned — a language rule, not a product rule.
    """
    for qty in _CAPACITY_QTY_RE.finditer(product_context):
        if qty.end() <= product_start:
            distance = product_start - qty.end()
        elif product_end <= qty.start():
            distance = qty.start() - product_end
        else:
            distance = 0
        if distance > 120:
            continue
        left = max(0, min(qty.start(), product_start) - 160)
        right = min(len(product_context), max(qty.end(), product_end) + 60)
        local = product_context[left:right]
        clause = _local_clause(product_context, qty.start())
        # Must read as a capacity/production/manufacturing figure, not an
        # unrelated number (revenue, price, distance) near the product.
        if not re.search(r"\b(?:capacit(?:y|ies)|manufactur\w*|production|nameplate|"
                         r"installed|commissioned|operational|per\s+annum|per\s+year|pa\b)\b",
                         local, re.I):
            continue
        # A generation/EPC frame ("500 MW solar project/plant") is deployment,
        # not factory output — unless an explicit manufacturing cue is present.
        manufacturing_cue = re.search(
            r"\b(?:manufactur\w*|production\s+(?:capacity|line|facility|unit)|"
            r"nameplate|fab\b|fabrication|integrated\s+manufacturing|"
            r"(?:cell|module|wafer|ingot)\s+(?:line|plant|factory|facility))\b", local, re.I)
        if _CAPACITY_GENERATION_RE.search(local) and not manufacturing_cue:
            continue
        if (PIPELINE_CONTEXT_RE.search(clause) or CAPABILITY_NOT_ROLE_RE.search(clause) or
                _CAPACITY_FUTURE_RE.search(clause) or _CAPACITY_FUTURE_RE.search(local) or
                _GROUP_ATTRIBUTION_RE.search(local) or
                _CAPACITY_AGGREGATE_RE.search(local) or _CAPACITY_NON_MAKING_RE.search(local) or
                THIRD_PARTY_CONTEXT_RE.search(local) or NEGATED_MANUFACTURING_RE.search(local)):
            continue
        return True
    return False


def _direct_product_making_claim(product_context: str, product_start: int, product_end: int) -> bool:
    """Catch a direct product claim whose capacity detail is not local enough.

    This is deliberately weaker than a manufacturer proof: it recognizes a
    product listed beside a making action, but sends it to the review-only
    ``DIRECT_ROLE_UNCLASSIFIED`` state until two dated physical proofs exist.
    """
    for action in OWNED_MAKING_ACTION_RE.finditer(product_context):
        if action.end() <= product_start:
            bridge = product_context[action.end():product_start]
        elif product_end <= action.start():
            bridge = product_context[product_end:action.start()]
        else:
            bridge = ""
        clause = _local_clause(product_context, action.start())
        if (len(bridge) <= 220 and not _bridge_rejects_product_role(bridge)
                and not CAPABILITY_NOT_ROLE_RE.search(clause)
                and _issuer_owns_action(product_context, action.start(), action.end())):
            return True
    return False


def _direct_issuer_role_binding(
    marker_re: re.Pattern, product_context: str, product_start: int, product_end: int,
) -> bool:
    """Bind an EPC/integration/supply verb to both product and issuer.

    Policy text often says that a product *will be installed*; that is neither
    evidence that the named issuer installs it nor a company product role.
    """
    if (NON_COMMERCIAL_CONTEXT_RE.search(product_context) or
            MARKET_ASSERTION_RE.search(product_context)):
        return False
    issuer_hits = list(ISSUER_CONTEXT_RE.finditer(product_context))
    for marker in marker_re.finditer(product_context):
        if marker.end() <= product_start:
            bridge = product_context[marker.end():product_start]
        elif product_end <= marker.start():
            bridge = product_context[product_end:marker.start()]
        else:
            bridge = ""
        if len(bridge) > 90 or ROLE_BREAK_RE.search(bridge):
            continue
        if any(abs(issuer.start() - marker.start()) <= 120 for issuer in issuer_hits):
            return True
    return False


def _direct_commercial_binding(
    product_context: str, product_start: int, product_end: int,
) -> bool:
    """Recognize issuer-owned exact-product commercial capture.

    Investor presentations frequently encode ownership structurally rather
    than grammatically (``Key orders booked: <product>``).  Requiring a nearby
    ``we`` loses exactly the early supplier evidence the system is intended to
    recover.  A commercial heading/action in an issuer filing is accepted only
    when it is tightly bound to the product and the local text is not market,
    customer, end-use, CSR, or transaction commentary.  Two independently
    dated events are still required before adjudication can promote the role.
    """
    if (NON_COMMERCIAL_CONTEXT_RE.search(product_context) or
            TRANSACTION_CONTEXT_RE.search(product_context)):
        return False
    for marker in DIRECT_COMMERCIAL_RE.finditer(product_context):
        if marker.end() <= product_start:
            bridge = product_context[marker.end():product_start]
        elif product_end <= marker.start():
            bridge = product_context[product_end:marker.start()]
        else:
            bridge = ""
        if len(bridge) > 220:
            continue
        local_left = min(marker.start(), product_start)
        local_right = max(marker.end(), product_end)
        local = product_context[max(0, local_left - 100):min(len(product_context), local_right + 100)]
        if (COMMERCIAL_MARKET_CONTEXT_RE.search(local) or
                SERVICE_OR_ADVISORY_CONTEXT_RE.search(local) or
                BUYER_OR_INPUT_CONTEXT_RE.search(local) or
                END_USE_CONTEXT_RE.search(local) or END_USE_CONTEXT_RE.search(bridge)):
            continue
        # Strong structured issuer disclosures own their rows without a prose
        # subject.  Less structured commercial language still needs an issuer
        # marker close to the action.
        structured = bool(re.search(
            r"key\s+orders?\s+booked|order\s+intake|orders?\s+(?:booked|received|secured|won)",
            marker.group(0), re.I,
        ))
        issuer_owned = any(
            abs(issuer.start() - marker.start()) <= 160
            for issuer in ISSUER_CONTEXT_RE.finditer(product_context)
        )
        if structured or issuer_owned:
            return True
    return False


def _role_type(product_context: str, product_start: int, product_end: int) -> tuple[str | None, bool]:
    """Classify a role only when it is local to the named product.

    A filing can mention a market product in one sentence and its unrelated
    factory in the next.  The role anchor and product therefore must overlap a
    compact context before we call the company a maker, installer, or supplier.
    """
    # A result table saying a firm is *not* a manufacturing company, or a
    # description of an acquisition target's plant, is not issuer operating
    # proof.  These are general disclosure grammar safeguards—not exceptions
    # for a particular company, industry, or product.
    if (NEGATED_MANUFACTURING_RE.search(product_context) or
            TRANSACTION_CONTEXT_RE.search(product_context)):
        return None, False
    if _direct_owned_asset_binding(product_context, product_start, product_end):
        return "MANUFACTURER", True
    if _direct_manufacturing_binding(product_context, product_start, product_end):
        return "MANUFACTURER", True
    if _direct_capacity_binding(product_context, product_start, product_end):
        return "MANUFACTURER", True
    if _direct_product_making_claim(product_context, product_start, product_end):
        return "DIRECT_ROLE_UNCLASSIFIED", False
    if _direct_commercial_binding(product_context, product_start, product_end):
        return "DIRECT_PRODUCT_SUPPLIER", False
    # Installation/supply language in a policy table is not the issuer's
    # commercial role.  For non-manufacturing roles require an issuer/action
    # anchor in the same short context.
    if _direct_issuer_role_binding(EPC_OR_INSTALL_RE, product_context, product_start, product_end):
        return "EPC_OR_INSTALLER", False
    if _direct_issuer_role_binding(INTEGRATOR_RE, product_context, product_start, product_end):
        return "SYSTEM_INTEGRATOR", False
    if _direct_issuer_role_binding(INPUT_SUPPLIER_RE, product_context, product_start, product_end):
        return "DIRECT_PRODUCT_SUPPLIER", False
    return None, False


def _role_windows(text: str) -> list[tuple[str, int]]:
    """Short windows around generic role language, deduplicated by bounds."""
    hits = list(MFG_RE.finditer(text))
    hits += list(EPC_OR_INSTALL_RE.finditer(text))
    hits += list(INTEGRATOR_RE.finditer(text))
    hits += list(INPUT_SUPPLIER_RE.finditer(text))
    hits += list(DIRECT_COMMERCIAL_RE.finditer(text))
    # Product catalogues and plant descriptions are frequently separate PDF
    # table cells. Catalogue anchors let the extractor discover the exact
    # product; a separate document-level corroboration rule may then retain it
    # as an explicitly unverified role candidate, never as maker proof.
    hits += list(re.finditer(
        r"product portfolio|product range|our products|key products|"
        r"business (?:comprises|includes)|engaged in", text, re.I
    ))
    windows, seen = [], set()
    for hit in hits:
        # Blank lines/bullets usually divide independent disclosures.  If PDF
        # extraction loses boundaries, keep a deliberately bounded fallback.
        paragraph_at = text.rfind("\n\n", 0, hit.start())
        bullet_at = max(
            text.rfind("•", 0, hit.start()), text.rfind("", 0, hit.start()),
            text.rfind("▪", 0, hit.start()),
        )
        left = max(
            paragraph_at + 2 if paragraph_at >= 0 else 0,
            bullet_at + 1 if bullet_at >= 0 else 0,
            hit.start() - 360,
        )
        next_para = text.find("\n\n", hit.end())
        next_bullets = [text.find(char, hit.end()) for char in ("•", "", "▪")]
        right_candidates = [pos for pos in (next_para, *next_bullets) if pos >= 0]
        right = min(right_candidates) if right_candidates else min(len(text), hit.end() + 420)
        if right - left > 900:
            left, right = max(0, hit.start() - 360), min(len(text), hit.end() + 420)
        key = (left // 80, right // 80)
        if key not in seen:
            seen.add(key)
            windows.append((text[left:right], hit.start() - left))
    # In presentation PDFs an order heading owns a list of child bullets.  The
    # normal bullet boundary correctly prevents role leakage, but it also
    # separates ``Key orders booked`` from every order after the first bullet.
    # Reattach the heading to each nearby child row as a small synthetic window.
    # This is document-structure parsing: it applies to every issuer/product and
    # still requires the exact/gated product plus repeated dated capture events.
    bullet_re = re.compile(r"[•▪]")
    for hit in DIRECT_COMMERCIAL_RE.finditer(text):
        section_end = min(len(text), hit.end() + 1800)
        bullets = list(bullet_re.finditer(text, hit.end(), section_end))[:10]
        for index, bullet in enumerate(bullets):
            child_end = bullets[index + 1].start() if index + 1 < len(bullets) else section_end
            child = text[bullet.end():child_end]
            if not child.strip() or len(child) > 700:
                continue
            # Never carry text from before the commercial heading: in slide
            # decks that prefix may belong to the prior page and can falsely
            # attach its product to the next page's order section.
            heading = hit.group(0)
            window = heading + ": " + child
            anchor = 0
            key = ("commercial-child", hit.start(), bullet.start())
            if key not in seen:
                seen.add(key)
                windows.append((window, anchor))
    return windows


def _derived_term_owners(vocabulary: dict[str, str]) -> dict[str, str]:
    """Map mechanically derived, unambiguous terms to canonical products.

    Full product phrases remain universally searchable.  Short heads are only
    proposals here; a ticker must also have a dated capability row for the
    canonical product before ``discover_roles`` enables the term.  If the same
    short term is equally central to two product labels, neither gets it.
    """
    proposals: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for canonical, display in vocabulary.items():
        canonical_words = max(1, len(canonical.split()))
        for raw_term in candidate_terms(display):
            term = normalized_product(raw_term)
            if not term or term == canonical or len(term.split()) >= canonical_words:
                continue
            proposals[term].append((len(term.split()) / canonical_words, canonical))
    owners: dict[str, str] = {}
    for term, choices in proposals.items():
        ranked = sorted(set(choices), reverse=True)
        if len(ranked) == 1 or ranked[0][0] > ranked[1][0]:
            owners[term] = ranked[0][1]
    return owners


def _ticker_product_terms(
    cur, as_of: date, country: str, vocabulary: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Return capability-supported short terms by ticker and canonical product."""
    if country != "IN" or not _table_exists(cur, "mg_company_capabilities"):
        return {}
    cur.execute(
        "SELECT MAX(as_of_date) AS snapshot FROM mg_company_capabilities WHERE as_of_date <= %s",
        (as_of,),
    )
    snapshot = cur.fetchone().get("snapshot")
    if not snapshot:
        return {}
    derived = _derived_term_owners(vocabulary)
    if not derived:
        return {}
    kept, _ = gate_terms_by_corpus_frequency(
        cur, set(derived), verbose=False, country=country, as_of=as_of,
    )
    derived = {term: owner for term, owner in derived.items() if term in kept}
    if not derived:
        return {}
    rows = q(cur, """
        SELECT UPPER(TRIM(ticker)) AS ticker, product
        FROM mg_company_capabilities
        WHERE as_of_date=%s AND manufacturer IS TRUE
    """, (snapshot,))
    output: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        canonical = normalized_product(row.get("product"))
        if canonical not in vocabulary:
            continue
        for term, owner in derived.items():
            if owner == canonical:
                output[row["ticker"]][term] = canonical
    return dict(output)


def _phrases_in_window(
    window: str, term_to_product: dict[str, str], anchor: int,
) -> list[tuple[str, int, int]]:
    """Find DB-known product phrases and gated short forms near a role phrase."""
    words = list(re.finditer(r"[A-Za-z][A-Za-z0-9-]*", window.casefold()))
    found: list[tuple[str, int, int]] = []
    for size in range(1, MAX_PRODUCT_WORDS + 1):
        for start in range(0, len(words) - size + 1):
            first, last = words[start], words[start + size - 1]
            if not (first.start() - 260 <= anchor <= last.end() + 260):
                continue
            candidate = normalized_product(" ".join(word.group(0) for word in words[start:start + size]))
            canonical = term_to_product.get(candidate)
            if canonical:
                found.append((canonical, first.start(), last.end()))
    return found


_SAFE_SHORT_PRODUCT_PREFIXES = frozenset({
    "a", "an", "and", "for", "including", "new", "of", "our", "several",
    "the", "their", "these", "those", "to", "various", "with",
})


def _short_term_modifier_conflict(
    window: str, product_start: int, product_end: int, canonical_product: str,
) -> bool:
    """Reject a foreign subtype modifier attached to a gated product head.

    A capability-supported short head is useful for disclosures such as
    ``orders for our transformers``.  It must not silently turn ``instrument
    transformers`` into the narrower reviewed ``power transformer`` chain.
    Exact canonical phrases and punctuation-delimited heads are unaffected.
    """
    matched = normalized_product(window[product_start:product_end])
    if not matched or matched == canonical_product:
        return False
    prefix = window[max(0, product_start - 48):product_start]
    immediate = re.search(r"([A-Za-z][A-Za-z-]*)\s+$", prefix)
    if not immediate:
        return False
    modifier = normalized_product(immediate.group(1))
    if (not modifier or modifier in _SAFE_SHORT_PRODUCT_PREFIXES or
            modifier in set(canonical_product.split())):
        return False
    return True


def _event_key(window: str, product: str) -> str:
    """Collapse re-filed copies of the same corporate claim across dates."""
    compact = re.sub(r"[^a-z0-9]+", "", window.casefold())
    product_at = compact.find(re.sub(r"[^a-z0-9]+", "", product))
    center = compact[max(0, product_at - 220): product_at + 420] if product_at >= 0 else compact[:640]
    return hashlib.sha1(center.encode("utf-8")).hexdigest()[:20]


def _excerpt(window: str, product: str) -> str:
    match = re.search(r"\b" + r"\s+".join(map(re.escape, product.split())) + r"s?\b", window, re.I)
    if match:
        window = window[max(0, match.start() - 130): match.end() + 300]
    return re.sub(r"\s+", " ", window).strip()[:520]


def _excerpt_at(window: str, product_start: int, product_end: int) -> str:
    """Center evidence on the literal phrase actually matched, including a gated head."""
    excerpt = window[max(0, product_start - 180):min(len(window), product_end + 340)]
    return re.sub(r"\s+", " ", excerpt).strip()[:520]


def _link(product: str, alias: dict[str, Any] | None) -> tuple[str | None, str]:
    """Link an exact issuer product to product identity and, when valid, theme.

    ``EXACT`` means the reviewed dated chain itself is the literal product.
    ``EXACT_PRODUCT`` means the literal product is proven but its economic-theme
    alias is broader.  Both can support producer identity; only ``EXACT`` can
    claim an exact reviewed product-to-theme crosswalk.
    """
    if not alias or alias.get("status") not in {"REVIEWED", "AUTO_DISCOVERY"}:
        return None, "UNLINKED"
    if alias.get("status") == "AUTO_DISCOVERY" and alias.get("match_scope") != "EXACT":
        return None, "UNLINKED"
    if alias.get("match_scope") == "EXACT":
        return alias.get("constraint_key"), "EXACT"
    return product_identity_constraint_key(product), "EXACT_PRODUCT"


def discover_roles(
    cur,
    as_of: date,
    country: str = "IN",
    lookback_days: int = LOOKBACK_DAYS,
    ticker: str | None = None,
    bucket: tuple[int, int] | None = None,
) -> list[dict]:
    """Collect role evidence from issuer documents without writing it."""
    vocabulary = product_vocabulary(cur, as_of, country)
    if not vocabulary:
        return []
    aliases = {normalized_product(label): alias for label, alias in
               fetch_constraint_aliases(cur, as_of, country).items()}
    ticker_short_terms = _ticker_product_terms(cur, as_of, country, vocabulary)
    start = as_of - timedelta(days=lookback_days)
    params: list[Any] = [country, start, as_of]
    ticker_filter = ""
    if ticker:
        ticker_filter = " AND UPPER(TRIM(ticker)) = %s"
        params.append(ticker.upper())
    bucket_filter = ""
    if bucket:
        bucket_index, bucket_count = bucket
        bucket_filter = " AND MOD(ABS(HASHTEXT(UPPER(TRIM(ticker)))), %s) = %s"
        params.extend([bucket_count, bucket_index])
    # Do not materialise multi-year filing text in the client.  A server-side
    # cursor gives a full historic rebuild bounded memory.  Role prefiltering
    # happens in Python because a broad PostgreSQL text-regex forces a corpus
    # scan even for a single-ticker diagnostic and is materially slower.
    scan = cur.connection.cursor(
        name=f"company_product_role_{as_of.strftime('%Y%m%d')}_{(ticker or 'all').lower()[:12]}",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    scan.itersize = 800
    # Product-role extraction needs issuer filings, not every exchange upload.
    # Documents without a manufacturing/role or product-catalogue anchor
    # cannot emit an event, so filtering them in PostgreSQL preserves exact
    # semantics while avoiding a multi-gigabyte full-text transfer.
    role_prefilter = (
        r"manufactur|production|installed capacity|\mEPC\M|install|deploy|"
        r"commission|integrat|supplier|vendor|product portfolio|product range|"
        r"our products|key products|business (comprises|includes)|engaged in|"
        r"key orders? booked|order intake|order book|orders? (booked|received|secured|won)|"
        r"revenues? (from|comprised|included|generated by|derived from)|sales of|dispatch"
    )
    params.append(role_prefilter)
    scan.execute(f"""
        SELECT id, UPPER(TRIM(ticker)) AS ticker, COALESCE(company, '') AS company,
               filed_at::date AS filed_at, COALESCE(title, '') AS title, url, raw_text
        FROM mg_documents
        WHERE country = %s AND ticker IS NOT NULL AND BTRIM(ticker) <> ''
          AND filed_at BETWEEN %s AND %s AND raw_text IS NOT NULL
          {ticker_filter}
          {bucket_filter}
          AND raw_text ~* %s
        ORDER BY ticker, filed_at, id
    """, tuple(params))

    # (ticker, product, role) -> independently dated corporate events.
    collected: dict[tuple[str, str, str], dict[str, Any]] = {}
    scanned = 0
    while True:
        rows = scan.fetchmany(800)
        if not rows:
            break
        for row in rows:
            scanned += 1
            if scanned % 5000 == 0:
                print(f"  scanned {scanned} issuer filings for {as_of}", flush=True)
            document_candidates_seen: set[str] = set()
            term_to_product = {product: product for product in vocabulary}
            term_to_product.update(ticker_short_terms.get(row["ticker"], {}))
            for window, anchor in _role_windows(row["raw_text"]):
                for product, product_start, product_end in _phrases_in_window(
                    window, term_to_product, anchor
                ):
                    if _short_term_modifier_conflict(
                        window, product_start, product_end, product
                    ):
                        continue
                    context_start = max(0, product_start - 260)
                    product_context = window[context_start: min(len(window), product_end + 260)]
                    role, physical = _role_type(
                        product_context, product_start - context_start, product_end - context_start)
                    document_detail = None
                    if role is None:
                        if product in document_candidates_seen:
                            continue
                        product_re = re.compile(
                            r"\b" + r"\s+".join(map(re.escape, product.split())) + r"s?\b",
                            re.I,
                        )
                        document_detail = _document_role_candidate_detail(
                            row["raw_text"], product_re
                        )
                        if document_detail is None:
                            continue
                        document_candidates_seen.add(product)
                        role, physical = "DIRECT_ROLE_UNCLASSIFIED", False
                    key = (row["ticker"], product, role)
                    event_key = _event_key(window, product)
                    bucket = collected.setdefault(key, {
                        "ticker": row["ticker"], "company": row["company"],
                        "product_phrase": vocabulary[product], "normalized_product": product,
                        "role_type": role, "events": {},
                    })
                    # A document/event may be repeated at later dates; use its
                    # first dated incarnation.  Independent means different claim,
                    # not merely a different exchange upload date.
                    previous = bucket["events"].get(event_key)
                    event = {
                        "document_id": row["id"], "filed_at": row["filed_at"],
                        "title": row["title"][:160], "url": row["url"],
                        "excerpt": (document_detail.get("excerpt") if document_detail
                                    else _excerpt_at(window, product_start, product_end)),
                        "physical_asset": physical,
                        "pipeline_evidence": bool(PIPELINE_CONTEXT_RE.search(product_context)),
                        "earnings_capture": bool(EARNINGS_CAPTURE_RE.search(product_context)),
                        "demand_or_policy": bool(POLICY_OR_DEMAND_RE.search(window)),
                        "evidence_scope": (
                            document_detail.get("evidence_scope") if document_detail
                            else "local product/action disclosure"
                            if normalized_product(window[product_start:product_end]) == product
                            else "capability-gated short product term in local action disclosure"
                        ),
                    }
                    if previous is None or row["filed_at"] < previous["filed_at"]:
                        bucket["events"][event_key] = event
    scan.close()

    output = []
    for item in collected.values():
        # One dated issuer disclosure counts once even when an investor deck
        # has several nearby role words that all point at the same product.
        # Prefer the version carrying a physical proof if there is one.
        per_date: dict[date, dict] = {}
        for event in item.pop("events").values():
            previous = per_date.get(event["filed_at"])
            if previous is None or (
                bool(event["physical_asset"]), bool(event["earnings_capture"]),
                bool(event["pipeline_evidence"])
            ) > (
                bool(previous["physical_asset"]), bool(previous["earnings_capture"]),
                bool(previous["pipeline_evidence"])
            ):
                per_date[event["filed_at"]] = event
        events = sorted(per_date.values(), key=lambda event: event["filed_at"])
        # A deck and its transcript often restate the same quarterly order a
        # few days apart using different prose, defeating text-hash dedupe.
        # Treat disclosures inside one 45-day reporting cycle as one evidence
        # event.  This is intentionally conservative: it can under-count two
        # genuine same-month wins, but it cannot promote a role by counting one
        # corporate claim twice.
        independent_events: list[dict] = []
        for event in events:
            if (independent_events and
                    (event["filed_at"] - independent_events[-1]["filed_at"]).days <
                    MIN_INDEPENDENT_EVENT_GAP_DAYS):
                previous = independent_events[-1]
                if (
                    bool(event["physical_asset"]), bool(event["earnings_capture"]),
                    bool(event["pipeline_evidence"]), len(event.get("excerpt") or ""),
                ) > (
                    bool(previous["physical_asset"]), bool(previous["earnings_capture"]),
                    bool(previous["pipeline_evidence"]), len(previous.get("excerpt") or ""),
                ):
                    independent_events[-1] = event
                continue
            independent_events.append(event)
        events = independent_events
        # A direct product role is evidenced only by two non-duplicative dated
        # disclosures, including a physical manufacturing action/asset where
        # it claims to be a manufacturer.  A lower-confidence role is still
        # retained as a discovery cue, never as a constraint or Buy claim.
        physical_count = sum(bool(event["physical_asset"]) for event in events)
        pipeline_count = sum(bool(event["pipeline_evidence"]) for event in events)
        earnings_capture_count = sum(bool(event["earnings_capture"]) for event in events)
        document_only = all(
            str(event.get("evidence_scope") or "").startswith("document-level")
            for event in events
        )
        role_state = ("EVIDENCED" if not document_only and len(events) >= 2 and
                      (item["role_type"] != "MANUFACTURER" or physical_count >= 2)
                      else "DISCOVERY")
        constraint_key, link_type = _link(
            item["normalized_product"], aliases.get(item["normalized_product"])
        )
        adjudication = automatic_role_adjudication(
            role_type=item["role_type"], role_state=role_state, link_type=link_type,
            physical_count=physical_count, pipeline_count=pipeline_count,
            earnings_capture_count=earnings_capture_count,
        )
        output.append({
            **item,
            "as_of_date": as_of,
            "role_state": role_state,
            "constraint_key": constraint_key,
            "constraint_link_type": link_type,
            "first_evidence_date": events[0]["filed_at"],
            "last_evidence_date": events[-1]["filed_at"],
            "independent_document_count": len(events),
            "physical_evidence_count": physical_count,
            "pipeline_evidence_count": pipeline_count,
            "earnings_capture_count": earnings_capture_count,
            "demand_or_policy_count": sum(bool(event["demand_or_policy"]) for event in events),
            "evidence": [{**event, "filed_at": event["filed_at"].isoformat()}
                         for event in events[:MAX_EVIDENCE]],
            "extraction_method": EXTRACTION_METHOD,
            "review_status": "AUTO_DISCOVERY",
            "adjudication_state": adjudication["state"],
            "adjudication_reason": adjudication["reason"],
            "missing_evidence": adjudication["missing_evidence"],
        })
    # Keep distinct role populations.  Collapsing them here can let a noisy
    # commercial mention erase a genuine capacity pipeline (or let an EPC row
    # erase manufacturing evidence).  The report join chooses the strongest
    # machine-adjudicated producer state; rejected and quarantined roles remain
    # available for audit without acquiring position authority.
    role_rank = {
        "MANUFACTURER": 0,
        "DIRECT_PRODUCT_SUPPLIER": 1,
        "EPC_OR_INSTALLER": 2,
        "SYSTEM_INTEGRATOR": 3,
        "INPUT_SUPPLIER": 4,
        "DIRECT_ROLE_UNCLASSIFIED": 5,
    }
    return sorted(output, key=lambda item: (
        item["constraint_link_type"] not in {"EXACT", "EXACT_PRODUCT"},
        item["role_state"] != "EVIDENCED",
        -item["physical_evidence_count"], -item["independent_document_count"],
        item["normalized_product"], item["ticker"], role_rank[item["role_type"]]
    ))


def ensure_table(conn) -> None:
    """Install the product-role ledger from the shared schema, if necessary."""
    from pathlib import Path
    # ``HERE`` is scripts/stock_report; its second parent is the project root.
    schema = Path(HERE).parents[1] / "schema" / "postgres_schema.sql"
    sql = schema.read_text()
    marker = "-- Company filings are the source of truth for what a listed company actually"
    if marker not in sql:
        raise RuntimeError("company-product-role schema section is missing")
    with conn.cursor() as cur:
        cur.execute(sql[sql.index(marker):])
    conn.commit()


def store_roles(
    conn, rows: list[dict], as_of: date, country: str = "IN", ticker: str | None = None,
    bucket: tuple[int, int] | None = None,
) -> None:
    """Replace one complete as-of snapshot, ticker slice, or hash bucket atomically."""
    with conn.cursor() as cur:
        if ticker:
            cur.execute("DELETE FROM mg_company_product_roles WHERE country=%s AND as_of_date=%s AND ticker=%s",
                        (country, as_of, ticker.upper()))
        elif bucket:
            bucket_index, bucket_count = bucket
            cur.execute("""
                DELETE FROM mg_company_product_roles
                WHERE country=%s AND as_of_date=%s
                  AND MOD(ABS(HASHTEXT(ticker)), %s) = %s
            """, (country, as_of, bucket_count, bucket_index))
        else:
            cur.execute("DELETE FROM mg_company_product_roles WHERE country=%s AND as_of_date=%s",
                        (country, as_of))
        if rows:
            payloads = []
            for row in rows:
                payload = {**row, "country": country}
                payload["evidence"] = psycopg2.extras.Json(row["evidence"])
                payload["missing_evidence"] = psycopg2.extras.Json(row["missing_evidence"])
                payloads.append(payload)
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO mg_company_product_roles
                  (country, as_of_date, ticker, company, product_phrase, normalized_product,
                   role_type, role_state, constraint_key, constraint_link_type,
                   first_evidence_date, last_evidence_date, independent_document_count,
                   physical_evidence_count, pipeline_evidence_count, earnings_capture_count,
                   demand_or_policy_count, evidence, extraction_method, review_status,
                   adjudication_state, adjudication_reason, missing_evidence)
                VALUES (%(country)s, %(as_of_date)s, %(ticker)s, %(company)s,
                        %(product_phrase)s, %(normalized_product)s, %(role_type)s,
                        %(role_state)s, %(constraint_key)s, %(constraint_link_type)s,
                        %(first_evidence_date)s, %(last_evidence_date)s,
                        %(independent_document_count)s, %(physical_evidence_count)s,
                        %(pipeline_evidence_count)s, %(earnings_capture_count)s,
                        %(demand_or_policy_count)s, %(evidence)s::jsonb,
                        %(extraction_method)s, %(review_status)s,
                        %(adjudication_state)s, %(adjudication_reason)s,
                        %(missing_evidence)s::jsonb)
                ON CONFLICT (country, as_of_date, ticker, normalized_product, role_type)
                DO UPDATE SET
                  company = EXCLUDED.company, product_phrase = EXCLUDED.product_phrase,
                  role_state = EXCLUDED.role_state, constraint_key = EXCLUDED.constraint_key,
                  constraint_link_type = EXCLUDED.constraint_link_type,
                  first_evidence_date = EXCLUDED.first_evidence_date,
                  last_evidence_date = EXCLUDED.last_evidence_date,
                  independent_document_count = EXCLUDED.independent_document_count,
                  physical_evidence_count = EXCLUDED.physical_evidence_count,
                  pipeline_evidence_count = EXCLUDED.pipeline_evidence_count,
                  earnings_capture_count = EXCLUDED.earnings_capture_count,
                  demand_or_policy_count = EXCLUDED.demand_or_policy_count,
                  evidence = EXCLUDED.evidence, extraction_method = EXCLUDED.extraction_method,
                  review_status = EXCLUDED.review_status,
                  adjudication_state = EXCLUDED.adjudication_state,
                  adjudication_reason = EXCLUDED.adjudication_reason,
                  missing_evidence = EXCLUDED.missing_evidence, updated_at = NOW()
            """, payloads, page_size=300)
    conn.commit()


def relink_stored_roles(
    cur, as_of: date, country: str, source_method: str,
) -> list[dict]:
    """Reapply only identity/adjudication semantics to frozen role evidence.

    Use this when an extractor release changes the product/theme contract but
    not the filing grammar.  Historical evidence, dates, counts and excerpts
    are copied verbatim; no new document can enter the as-of snapshot.
    """
    aliases = {normalized_product(label): alias for label, alias in
               fetch_constraint_aliases(cur, as_of, country).items()}
    rows = q(cur, """
        SELECT as_of_date, ticker, company, product_phrase, normalized_product,
               role_type, role_state, first_evidence_date, last_evidence_date,
               independent_document_count, physical_evidence_count,
               pipeline_evidence_count, earnings_capture_count,
               demand_or_policy_count, evidence, review_status
        FROM mg_company_product_roles
        WHERE country=%s AND as_of_date=%s AND extraction_method=%s
        ORDER BY ticker, normalized_product, role_type
    """, (country, as_of, source_method))
    output: list[dict] = []
    for row in rows:
        if not _valid_product_label(row.get("product_phrase")):
            continue
        product = row["normalized_product"]
        constraint_key, link_type = _link(product, aliases.get(product))
        adjudication = automatic_role_adjudication(
            role_type=row["role_type"], role_state=row["role_state"],
            link_type=link_type,
            physical_count=int(row.get("physical_evidence_count") or 0),
            pipeline_count=int(row.get("pipeline_evidence_count") or 0),
            earnings_capture_count=int(row.get("earnings_capture_count") or 0),
        )
        output.append({
            **row,
            "constraint_key": constraint_key,
            "constraint_link_type": link_type,
            "extraction_method": EXTRACTION_METHOD,
            "adjudication_state": adjudication["state"],
            "adjudication_reason": adjudication["reason"],
            "missing_evidence": adjudication["missing_evidence"],
        })
    return output


def fetch_company_product_discoveries(cur, as_of: date, country: str = "IN", limit: int = 40) -> list[dict]:
    """Unlinked direct company products for a report's research-only section."""
    if not _table_exists(cur, "mg_company_product_roles"):
        return []
    rows = q(cur, """
        SELECT DISTINCT ON (ticker, normalized_product, role_type)
               ticker, company, product_phrase, normalized_product, role_type, role_state,
               first_evidence_date, last_evidence_date, independent_document_count,
               physical_evidence_count, pipeline_evidence_count, earnings_capture_count,
               demand_or_policy_count, evidence, review_status, adjudication_state,
               adjudication_reason, missing_evidence
        FROM mg_company_product_roles
        WHERE country=%s AND as_of_date <= %s AND role_state <> 'REJECTED'
          AND constraint_link_type='UNLINKED' AND review_status <> 'REJECTED'
          AND adjudication_state NOT LIKE 'AUTO_REJECTED%%'
          AND extraction_method=%s
        ORDER BY ticker, normalized_product, role_type, as_of_date DESC
    """, (country, as_of, EXTRACTION_METHOD))
    rows.sort(key=lambda row: (
        row["role_state"] != "EVIDENCED", -int(row["physical_evidence_count"] or 0),
        -int(row["independent_document_count"] or 0), row["product_phrase"], row["ticker"]
    ))
    for row in rows:
        row["research_route"] = (
            "Verify dated demand, import/capacity gap, and resupply barrier before creating a constraint chain"
        )
        row["status"] = (
            "company product role evidenced; constraint eligibility unproved"
            if row["role_state"] == "EVIDENCED" else
            "company product-role discovery; confirm a second non-duplicative filing"
        )
    return rows[:limit]


def fetch_company_role_review_queue(
    cur, as_of: date, country: str = "IN", limit: int = 3,
) -> list[dict]:
    """Return only the highest-value unresolved machine exceptions.

    Strong exact roles are promoted automatically and weak/no-role matches are
    rejected automatically.  This legacy-named queue is now a bounded audit
    surface for genuinely ambiguous evidence; it is not a mandatory step in
    the normal producer-promotion path.
    """
    if not _table_exists(cur, "mg_company_product_roles"):
        return []
    rows = q(cur, """
        WITH latest_role AS (
            SELECT DISTINCT ON (ticker, normalized_product, role_type)
                   ticker, company, product_phrase, normalized_product, role_type,
                   role_state, constraint_key, constraint_link_type,
                   first_evidence_date, last_evidence_date,
                   independent_document_count, physical_evidence_count,
                   pipeline_evidence_count, earnings_capture_count,
                   demand_or_policy_count, evidence, as_of_date,
                   adjudication_state, adjudication_reason, missing_evidence
            FROM mg_company_product_roles
            WHERE country=%s AND as_of_date <= %s
              AND adjudication_state LIKE 'QUARANTINED_%%'
              AND extraction_method=%s
            ORDER BY ticker, normalized_product, role_type, as_of_date DESC, id DESC
        ), product_coverage AS (
            SELECT normalized_product, COUNT(DISTINCT ticker)::int AS issuer_count,
                   SUM(physical_evidence_count)::int AS physical_evidence_total
            FROM latest_role
            GROUP BY normalized_product
        )
        SELECT r.*, p.issuer_count, p.physical_evidence_total,
               a.constraint_key AS alias_constraint_key,
               a.match_scope AS alias_match_scope,
               a.status AS alias_status
        FROM latest_role r
        JOIN product_coverage p ON p.normalized_product = r.normalized_product
        LEFT JOIN mg_constraint_product_aliases a
          ON a.country=%s AND a.normalized_label=r.normalized_product
         AND (a.first_seen_date IS NULL OR a.first_seen_date <= %s)
         AND (a.effective_from IS NULL OR a.effective_from <= %s)
         AND (a.effective_to IS NULL OR a.effective_to >= %s)
         AND a.status <> 'REJECTED'
        ORDER BY
          CASE WHEN r.constraint_link_type='EXACT' THEN 0
               WHEN p.issuer_count >= 2 THEN 1 ELSE 2 END,
          r.earnings_capture_count DESC,
          p.physical_evidence_total DESC, r.physical_evidence_count DESC,
          r.independent_document_count DESC, r.last_evidence_date DESC,
          r.ticker
        LIMIT %s
    """, (country, as_of, EXTRACTION_METHOD, country, as_of, as_of, as_of, limit))
    for row in rows:
        exact_chain = (
            row.get("alias_status") in {"REVIEWED", "AUTO_DISCOVERY"} and
            row.get("alias_match_scope") == "EXACT"
        )
        multi_issuer = int(row.get("issuer_count") or 0) >= 2
        row["review_priority"] = (
            "P1 — exact-chain ambiguity blocking automatic adjudication"
            if exact_chain else
            "P2 — repeated issuer evidence for a potentially new product layer"
            if multi_issuer else
            "P3 — unresolved issuer-product association"
        )
        row["review_route"] = (
            "scheduled evidence refresh: recover the missing proof printed by the machine state; "
            "no company promotion until the deterministic gate clears"
        )
        row["selection_status"] = "bounded exception — no position authority"
    return rows


def merge_exact_role_makers(cur, as_of: date, makers: dict[str, dict], country: str = "IN") -> dict[str, dict]:
    """Append exact-product operating, pipeline, and commercial roles.

    This is a literal normalized-product join only.  A family-level economic
    theme cannot erase a proven manufacturer of the literal product, while an
    EPC contractor or adjacent product still cannot enter the producer list.
    Producer identity never establishes constraint quality or Buy authority.
    """
    if not makers or not _table_exists(cur, "mg_company_product_roles"):
        return makers
    product_to_key = {product: normalized_product(product) for product in makers}
    exact_products = sorted(set(product_to_key.values()))
    if not exact_products:
        return makers
    human_rejection_veto = """
          AND NOT EXISTS (
              SELECT 1
              FROM mg_company_role_reviews review
              WHERE review.country=mg_company_product_roles.country
                AND review.ticker=mg_company_product_roles.ticker
                AND review.normalized_product=mg_company_product_roles.normalized_product
                AND review.role_type=mg_company_product_roles.role_type
                AND review.review_status='REJECTED'
                AND review.evidence_through_date >= mg_company_product_roles.first_evidence_date
                AND review.decision_available_from <= %s
          )
    """ if _table_exists(cur, "mg_company_role_reviews") else ""
    params: list[Any] = [country, as_of, exact_products, EXTRACTION_METHOD]
    if human_rejection_veto:
        params.append(as_of)
    rows = q(cur, f"""
        SELECT DISTINCT ON (ticker, normalized_product)
               ticker, company, normalized_product, role_type, role_state,
               first_evidence_date, last_evidence_date, independent_document_count,
               physical_evidence_count, pipeline_evidence_count, earnings_capture_count,
               evidence, adjudication_state, adjudication_reason, missing_evidence
        FROM mg_company_product_roles
        WHERE country=%s AND as_of_date <= %s
          AND normalized_product = ANY(%s)
          AND constraint_link_type IN ('EXACT', 'EXACT_PRODUCT')
          AND role_state='EVIDENCED'
          AND adjudication_state IN ('PIPELINE_EVIDENCED',
                                     'OPERATING_PRODUCER_EVIDENCED',
                                     'EARNINGS_CAPTURE_EVIDENCED')
          AND extraction_method=%s
          {human_rejection_veto}
        ORDER BY ticker, normalized_product, as_of_date DESC,
          CASE adjudication_state
            WHEN 'EARNINGS_CAPTURE_EVIDENCED' THEN 0
            WHEN 'OPERATING_PRODUCER_EVIDENCED' THEN 1
            WHEN 'PIPELINE_EVIDENCED' THEN 2
            ELSE 3
          END,
          CASE role_type
            WHEN 'MANUFACTURER' THEN 0
            WHEN 'DIRECT_PRODUCT_SUPPLIER' THEN 1
            ELSE 2
          END
    """, tuple(params))
    key_to_product = {value: key for key, value in product_to_key.items()}
    for row in rows:
        product = key_to_product.get(row["normalized_product"])
        if not product:
            continue
        bucket = makers[product]
        existing_makers = bucket.setdefault("makers", [])
        existing = next(
            (maker for maker in existing_makers
             if (maker.get("ticker") or "").upper() == row["ticker"]),
            None,
        )
        evidence = row.get("evidence") or []
        pipeline = row["adjudication_state"] == PIPELINE_EVIDENCED
        commercial = row["role_type"] == "DIRECT_PRODUCT_SUPPLIER"
        maker_status = (
            "capacity pipeline / group manufacturing plan" if pipeline else
            "direct product supplier — repeated commercial capture" if commercial else
            "operating manufacturer — independently corroborated"
        )
        research_route = (
            "Monitor funding, commissioning and customer qualification" if pipeline else
            "Verify owned production versus outsourced supply, then underwrite earnings capture" if commercial else
            "Validate same-product earnings capture and underwriting"
        )
        promoted = {
            "ticker": row["ticker"], "company": row["company"], "industry": "",
            "n_docs": row["independent_document_count"],
            "mfg_docs": row["physical_evidence_count"],
            "direct_evidence_count": row["independent_document_count"],
            "strict_evidence_count": row["physical_evidence_count"],
            "last_evidence_date": row["last_evidence_date"].isoformat(),
            "evidence_samples": evidence,
            "evidence": (
                f"{row['earnings_capture_count']} independently dated exact-product commercial passages"
                if commercial else
                f"{row['physical_evidence_count']} dated direct product-and-physical-asset passages"
            ),
            "maker_status": maker_status,
            "research_route": research_route,
            "needs_review": False, "review_reason": None,
            "industry_mismatch": False, "entity_scope": "company filing",
            "source": "own-filings company-product-role ledger (automatic exact-role adjudication)",
            "adjudication_state": row["adjudication_state"],
            "adjudication_reason": row["adjudication_reason"],
            "missing_evidence": row.get("missing_evidence") or [],
            "pipeline_evidence_count": int(row.get("pipeline_evidence_count") or 0),
            "earnings_capture_count": int(row.get("earnings_capture_count") or 0),
        }
        if existing is None:
            existing_makers.append(promoted)
            coverage_key = "role_ledger_additions"
        else:
            # The capability index is recall-oriented.  Once the stricter role
            # ledger has adjudicated the same ticker/product, its packet is the
            # sole producer authority; never retain a stronger-looking legacy
            # capability label over a weaker but correctly scoped ledger state.
            existing.update(promoted)
            coverage_key = "role_ledger_promotions"
        bucket.setdefault("coverage", {})[coverage_key] = (
            int(bucket.get("coverage", {}).get(coverage_key) or 0) + 1
        )
    return makers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    date_args = parser.add_mutually_exclusive_group(required=True)
    date_args.add_argument("--as-of", help="one snapshot date YYYY-MM-DD")
    date_args.add_argument("--snapshots", help="comma-separated snapshot dates YYYY-MM-DD,...")
    parser.add_argument("--country", default="IN")
    parser.add_argument("--ticker", help="optional one-ticker rebuild for diagnostics")
    parser.add_argument("--bucket", help="optional deterministic issuer slice INDEX/COUNT, e.g. 0/8")
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument(
        "--relink-from-version",
        help=("reuse frozen evidence from this extractor version and rerun only "
              "the current product/theme link and adjudication contract"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-schema", action="store_true",
        help="skip idempotent schema setup (for parallel bucket workers after one setup pass)",
    )
    args = parser.parse_args()
    bucket = None
    if args.bucket:
        try:
            bucket_index, bucket_count = (int(part) for part in args.bucket.split("/", 1))
        except ValueError as exc:
            parser.error("--bucket must be INDEX/COUNT, e.g. 0/8")
            raise exc  # unreachable; satisfies type checkers
        if bucket_count <= 0 or not 0 <= bucket_index < bucket_count:
            parser.error("--bucket requires 0 <= INDEX < COUNT")
        bucket = (bucket_index, bucket_count)
    as_of_dates = ([date.fromisoformat(args.as_of)] if args.as_of else
                   [date.fromisoformat(value.strip()) for value in args.snapshots.split(",")])
    country = args.country.upper()
    conn = connect()
    try:
        if not args.skip_schema:
            ensure_table(conn)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for as_of in as_of_dates:
            if args.relink_from_version:
                if args.ticker or bucket:
                    parser.error("--relink-from-version cannot be combined with --ticker or --bucket")
                rows = relink_stored_roles(cur, as_of, country, args.relink_from_version)
            else:
                rows = discover_roles(cur, as_of, country, args.lookback_days, args.ticker, bucket)
            exact = sum(row["constraint_link_type"] in {"EXACT", "EXACT_PRODUCT"}
                        for row in rows)
            evidenced = sum(row["role_state"] == "EVIDENCED" for row in rows)
            print(f"Discovered {len(rows)} company-product-role rows as of {as_of} "
                  f"({evidenced} evidenced roles; {exact} exact-product links).")
            if args.dry_run:
                for row in rows[:80]:
                    print(f"  {row['ticker']:<14} {row['product_phrase']:<36} {row['role_type']:<24} "
                          f"{row['role_state']:<10} {row['constraint_link_type']}")
            else:
                store_roles(conn, rows, as_of, country, args.ticker, bucket)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
