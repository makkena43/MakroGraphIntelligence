#!/usr/bin/env python3
"""
Company capability mapper — build the universe from what companies MAKE,
not from what they TALK ABOUT.

THE PROBLEM THIS FIXES
----------------------
The beneficiary mapper links a company to a theme when its filings co-occur
with theme-narrative documents. A pure-play that files dedicated documents
and never discusses a national shortage is therefore invisible. Measured
cost (scripts/research/winner_autopsy.py, 778 winner-instances, 2020-2023):
82% of >=5x winners never entered the screen's universe at all, and the
universe itself was only 132-291 tickers against ~2,000 filing tickers.

Worse, the bias is not random. Requiring theme co-occurrence selects FOR
companies that market themselves as policy/theme plays -- the "claimant not
winner" population the skill file already warns about -- and selects AGAINST
the quiet operator that simply manufactures the constrained product.

THE INVERSION
-------------
A company does not need to mention the constraint. It needs to make the
product. Constraints are market facts ("CRGO steel is short"); filings are
full of product facts ("installed capacity of X MVA transformers"). This
module reads the second and joins it to the first, with no theme document
required anywhere in the path.

NO HARDCODING (standing user rule)
----------------------------------
- Product vocabulary comes from DB rows only: `mg_india_beneficiaries.
  constrained_product` and `mg_import_dependencies.component`. Adding a
  product is an INSERT, never a code edit. No company, theme, sector or
  product name is written in this file.
- Search terms are DERIVED from those labels mechanically (split
  parentheticals and separators, take the phrase and its head noun), then
  filtered by CORPUS FREQUENCY: a term is kept only if it is rare enough in
  the corpus to be informative. That is what stops "Solar Cell" -> "Cell"
  and "Semiconductor IC" -> "IC" without anyone hand-listing exceptions,
  while still allowing "Power Transformer" -> "Transformer". Same philosophy
  as the novel-vocab detector's corpus-wide ordinary-English test.
- Manufacturing evidence uses generic Indian disclosure vocabulary
  ("installed capacity", "per annum", "our plant"...), which is language
  every manufacturer uses regardless of sector.

NOT TUNED TO ANY ONE COMPANY (standing user rule)
-------------------------------------------------
Thresholds here are population defaults (>=3 documents, manufacturing
proximity required) carried over from the existing detectors, NOT fitted to
make any particular ticker appear. Validation is by re-running the winner
autopsy across all 778 winner-instances and reading the recall lift and the
noise cost together -- never by checking whether one favourite name appears.
The T3/GRAVITA reversal earlier this year is the standing reminder of why.

POINT-IN-TIME
-------------
Every snapshot reads only documents filed <= as_of. One corpus pass per
product bucketed by quarter yields every anchor's snapshot at once.

USAGE
-----
    python scripts/stock_report/company_capabilities.py --as-of 2022-12-31
    python scripts/stock_report/company_capabilities.py --snapshots 2020-12-31,2021-12-31
    python scripts/stock_report/company_capabilities.py --as-of 2022-12-31 --dry-run
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from extract_report_data import connect  # noqa: E402

# Generic manufacturing-disclosure vocabulary. Not products, not companies,
# not sectors -- this is how any Indian manufacturer describes owning
# capacity, and it is what separates a MAKER from a trader or a company
# merely name-dropping a product in market commentary.
MFG_EVIDENCE = (
    r"installed capacity|production capacity|manufacturing facilit|"
    r"manufacturing unit|manufacturing plant|our plant|our factory|"
    r"production line|capacity expansion|capacity addition|"
    r"per annum|greenfield|brownfield|commissioned|commissioning of|"
    r"plant at|facility at|manufactur"
)
# A generic word such as "manufacturing" close to a product can still describe
# a customer's project, market commentary, or an industry scheme.  The stricter
# gate below is deliberately used only for the client-facing proof bundle and
# the final investment gate: it requires an action *and* a physical asset in the
# same local passage.  The broad mapper remains a discovery tool so we do not
# silently erase legitimate but unusually worded manufacturers from research.
STRICT_MFG_ACTION = (
    r"\b(?:manufactur(?:e|ed|es|ing)|produc(?:e|ed|es|ing)|"
    r"fabricat(?:e|ed|es|ing)|assembl(?:e|ed|es|ing))\b"
)
STRICT_MFG_ASSET = (
    r"installed capacity|production capacity|manufacturing facilit|"
    r"manufacturing (?:unit|plant)|our (?:plant|factory)|production line|"
    r"capacity (?:expansion|addition)|greenfield|brownfield|plant at|facility at|"
    r"(?:existing|dedicated|new) (?:unit|plant|facility)|unit at"
)
CONSUMER_CONTEXT = (
    r"procure|purchas|buy(?:ing)?|consum(?:e|ed|ption)|customer|end[- ]user|"
    r"project developer|power project|installation|deployment"
)
# A company can make a component through a subsidiary or reportable segment
# without repeating a plant/capacity figure in every disclosure.  This is
# enough to surface a *direct-role, capacity-unquantified* research lead, but
# never enough to call it a verified operating manufacturer or promote it to a
# Buy gate.
OWNERSHIP_CONTEXT = (
    r"\bour\b|\bwe\b|\bthe company\b|\bgroup\b|\bsubsidiar\w*\b|"
    r"\bwholly owned\b|\boperating segment\b|\bbusiness segment\b|"
    r"\bdivision\b|\bowned\b"
)
DIRECT_OWNERSHIP_CONTEXT = (
    r"\bour\b|\bwe\b|\bthe company\b|\bsubsidiar\w*\b|\bwholly owned\b|"
    r"\boperating segment\b|\bbusiness segment\b|\bdivision\b|\bowned\b"
)
GROUP_OWNERSHIP_CONTEXT = r"\bgroup\b"
PIPELINE_CONTEXT = (
    r"term sheet|memorandum|\bmou\b|propos\w*|intend\w*|plan\w* to|"
    r"in the process of|set(?:ting)? up|establish\w*|expect\w* to|"
    r"under construction|greenfield|brownfield|joint venture"
)
OPERATING_CONTEXT = (
    r"commercial production|commenc\w* production|commissioned|operational|"
    r"operating segment|currently manufactur\w*|"
    r"operat(?:e|ed|es|ing)|\bestablished\b|state[- ]of[- ]the[- ]art|"
    r"world[- ]class"
)
# An incorporation object's laundry list ("manufacture, trade, import, buy …")
# describes what a company may legally do, not what it actually makes.  It can
# be a discovery clue but never establishes a direct operating role without a
# product-coupled physical asset.
# Beyond the header phrases, an objects clause has a STRUCTURAL signature: a
# chain of 4+ commerce verbs/nouns ("manufacture, sell, export, import and deal
# in ..." / "manufacturers, traders, importers, exporters and dealers") over a
# catalog of unrelated products. It states what a company MAY legally do, not
# what it makes. Real operating disclosures never chain four trading verbs
# ("designs, develops and manufactures" is three, and none are trading verbs);
# this is precisely what tagged NITIRAJ (weighing / home-automation maker) as a
# defense-electronics manufacturer via a "Radar Human sensor" objects entry.
_OBJECTS_VERB = (r"(?:manufactur\w*|produc\w*|trad\w*|sell\w*|market\w*|import\w*|"
                 r"export\w*|deal\w*|buy\w*|purchas\w*|distribut\w*)")
_OBJECTS_SEP = r"(?:\s*[,/&]\s*|\s+and\s+|\s+or\s+)"
BUSINESS_OBJECT_CONTEXT = (
    r"main object(?:ive)?|objects? of (?:the )?company|incorporated (?:with|to)|"
    r"memorandum of association|authori[sz]ed to (?:manufacture|produce)|"
    + _OBJECTS_VERB + _OBJECTS_SEP + _OBJECTS_VERB + _OBJECTS_SEP
    + _OBJECTS_VERB + _OBJECTS_SEP + _OBJECTS_VERB
)
# These words state that the named product is a market input, a policy topic,
# or somebody else's capacity.  They are a useful negative signal only when
# the filing has no product-coupled physical asset: a real maker can of course
# discuss a tariff or its customers alongside its own plant.
MACRO_OR_INPUT_CONTEXT = (
    r"\bdemand\b|supply chain|\bcustomers?\b|\bsuppliers?\b|raw materials?|"
    r"used (?:by|for)|input(?:s)?\b|imports?|customs duty|tariffs?|"
    r"government (?:of|has)|policy|\bbidders?\b|tender"
)
# Trading language: a distributor naming a product is not a beneficiary of
# a shortage in it. Used as a NEGATIVE signal only when no MFG evidence.
TRADE_ONLY = r"trading|distributor|dealership|reseller|stockist"

MIN_DOCS = 3               # repetition, not one passing mention
LOOKBACK_DAYS = 1095       # 3 years of filings define current capability
MIN_INDEPENDENT_EVENT_GAP_DAYS = 45
# Division of labour between the two precision gates: this one removes words
# that are simply too common to carry information ("Stock" 95%, "Board" 78%,
# "Equipment" 27%), while the INDUSTRY-COHERENCE gate below removes words
# that are rare but semantically leaky ("Panels" -> plywood/laminate/steel).
# Deliberately NOT set tight: at 1% the term "APIs" (~0.8-1.0%) sat exactly on
# the boundary and flipped in and out between sampling runs, silently taking
# all 45 API manufacturers with it. Frequency alone is the wrong instrument
# for that decision -- coherence is -- so this cut is loose and coherence does
# the semantic work.
CORPUS_DF_MAX = 0.02
DF_SAMPLE = 12000          # docs sampled to estimate corpus frequency
DF_SEED = 0.42             # fixed seed: the build MUST be reproducible
# Postgres ARE caps {m,n} at 255 -- chain two spans for a ~500-char window,
# same trick the moonshot screen uses for its vendor-proximity pattern.
PROX = r"[\s\S]{0,250}[\s\S]{0,250}"


def _norm_label(label):
    """Normalising key for merging the SAME product written two ways.
    The two source tables disagree on casing, parentheticals and plurals
    ("Defense electronics" vs "Defense Electronics (radar, avionics)";
    "Solar Cell" vs "Solar Cells"), and treating those as distinct products
    would both double-count evidence and make their shared head noun look
    falsely ambiguous."""
    base = re.sub(r"\(.*?\)", " ", label).lower()
    base = re.sub(r"[^a-z0-9/ ]+", " ", base)
    # "A / B" labels give two names for ONE product ("PCB / Printed Circuit
    # Board"); key on the last segment so both spellings land in one group
    base = base.split("/")[-1]
    return " ".join(_singular(w) for w in base.split()).strip()


# Units of measure and quantity words that appear as suffixes in the
# reference tables' component keys ("crgo_steel_mt", "solar_cells_gw",
# "ems_capacity_bn_usd"). Units, not business entities — the same category of
# reference data as currency codes, so listing them here is not a hardcoded
# product/company/theme.
_UNIT_TOKENS = {
    "mt", "kt", "tonne", "tonnes", "ton", "kg", "gw", "gwh", "mw", "mwh",
    "kwh", "km", "mn", "bn", "cr", "usd", "inr", "sqm", "sqft", "unit",
    "units", "capacity", "pct", "percent", "value", "qty", "quantity",
}


def component_tokens(name):
    """Normalise either naming convention to a comparable token set.

        'crgo_steel_mt'                -> {crgo, steel}
        'CRGO Steel'                   -> {crgo, steel}
        'pcb_sqm_mn'                   -> {pcb}
        'PCB / Printed Circuit Board'  -> {pcb, printed, circuit, board}

    The reference tables (mg_capacity_gaps, mg_import_dependencies) key on
    snake_case component names with unit suffixes, while the theme graph uses
    human product labels. They never matched, so the capacity-gap and
    import-dependence legs of the constraint grade reached the grader on ZERO
    of 86 constraint-anchors — two of the four legs silently unavailable."""
    base = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    toks = {_singular(t) for t in base.split() if t}
    return {t for t in toks if t not in _UNIT_TOKENS and len(t) > 1}


def match_component(product_label, component_names):
    """Best reference-table component for a product label, or None.

    Matches when one side's distinctive tokens are a subset of the other's,
    which handles both the unit-suffix case ({crgo,steel} == {crgo,steel})
    and the abbreviation case ({pcb} subset of {pcb,printed,circuit,board}).
    Ties break toward the most specific (largest) matching component."""
    pt = component_tokens(product_label)
    if not pt:
        return None
    best, best_n = None, 0
    for c in component_names:
        ct = component_tokens(c)
        if not ct:
            continue
        if ct <= pt or pt <= ct:
            if len(ct) > best_n:
                best, best_n = c, len(ct)
    return best


def _is_acronym(term):
    """Form-based: a short token that is mostly capitals (PCB, MLCCs, LFP,
    NMC, EMS, APIs, HVDC). These are product NAMES and belong in the narrow
    term set; 'Panels', 'Boards', 'Cable', 'Module' are not and do not."""
    if len(term.split()) > 1 or len(term) > 6:
        return False
    caps = sum(1 for c in term if c.isupper())
    return caps >= 2 and caps >= len([c for c in term if c.isalpha()]) - 1


def _singular(word):
    """Crude de-pluralisation, enough to unify label and term variants
    ("ICs"/"IC", "Cells"/"Cell", "Boards"/"Board")."""
    return word[:-1] if len(word) > 2 and word.endswith("s") else word


def product_labels(cur, hs_top_n=0):
    """Every constrained-product the system tracks, from DB rows only,
    merged across the source tables. Returns {canonical_label: {variants}}.

    hs_top_n > 0 additionally pulls the HS customs taxonomy
    (scripts/policy/ingest_hs_products.py), highest Indian import value
    first. That is what lifts the vocabulary past the ~20 hand-curated
    products which the winner autopsy identified as the real coverage
    ceiling — 97% of missed winners already had filing text, they just made
    things nobody had put on the list."""
    raw = set()
    cur.execute("""SELECT DISTINCT constrained_product AS p
                   FROM mg_india_beneficiaries WHERE constrained_product IS NOT NULL""")
    raw.update(r["p"].strip() for r in cur.fetchall() if r["p"] and r["p"].strip())
    cur.execute("""SELECT DISTINCT component AS p
                   FROM mg_import_dependencies WHERE component IS NOT NULL""")
    raw.update(r["p"].strip() for r in cur.fetchall() if r["p"] and r["p"].strip())
    if hs_top_n:
        try:
            cur.execute("""SELECT description FROM mg_hs_products
                           WHERE tracked ORDER BY value_rank LIMIT %s""", (hs_top_n,))
            raw.update(r["description"].strip() for r in cur.fetchall()
                       if r["description"] and r["description"].strip())
        except Exception:
            pass   # taxonomy not ingested yet; curated products still work

    groups = defaultdict(set)
    for lb in raw:
        groups[_norm_label(lb)].add(lb)
    # canonical = shortest variant (the cleanest label), terms merged from all
    return {sorted(v, key=len)[0]: v for v in groups.values()}


def candidate_terms(label):
    """Derive searchable phrases from a product label MECHANICALLY.

    "PCB / Printed Circuit Board"        -> {PCB, Printed Circuit Board}
    "Cathode Active Materials (LFP/NMC)" -> {Cathode Active Materials, LFP, NMC,
                                             Materials}
    "Power Transformer"                  -> {Power Transformer, Transformer}

    Head nouns are proposed for every multi-word phrase; the corpus-frequency
    gate downstream is what decides whether one survives ("Transformer" does,
    "Cell"/"Materials" do not). Nothing about which is which is written here.
    """
    # A long label is a DEFINITION, not a name. Customs descriptions read
    # "Aircraft launching gear, deck-arrestor or similar gear, ground flying
    # trainers; parts thereof" or "...excluding those of heading no. 8608" —
    # splitting those the same way as a curated two-word label produced junk
    # head nouns (gear, pipes, holders, tubes, valves) that tagged 77 tickers
    # as aircraft-launching-gear makers. For anything definition-length, keep
    # only the leading clause (the actual product) and refuse bare head
    # nouns. Judged on LENGTH, so it stays a rule rather than a list of which
    # products are "real".
    is_definition = len(_norm_label(label).split()) > 6
    if is_definition:
        label = re.split(r"[;:]", label)[0]
        label = re.sub(r"\b(?:n\.e\.c\.|excluding|other than|whether or not)\b.*",
                       "", label, flags=re.I)

    parts = []
    for chunk in re.split(r"[(),]", label):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts.extend(p.strip() for p in chunk.split("/") if p.strip())
    # A slash label can combine a domain acronym with a business model
    # ("ACRONYM / contract manufacturing services"). The business model is
    # not an atomic product: searching it alone maps food, pharma, chemicals,
    # and electronics into one false producer universe. Keep the domain side
    # and require a real product/domain noun in every non-acronym alternative.
    business_model_tokens = {
        "contract", "manufacture", "manufacturer", "manufacturing",
        "service", "services", "solution", "solutions", "outsourcing",
        "original", "equipment", "provider", "providers", "business",
    }
    has_acronym_alias = any(_is_acronym(part) for part in parts)
    terms = set()
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if len(p) < 2:
            continue
        words = p.split()
        material_words = {
            re.sub(r"[^a-z0-9]+", "", word.lower()) for word in words
        } - business_model_tokens
        if has_acronym_alias and not material_words and not _is_acronym(p):
            continue
        if is_definition and len(words) < 2 and not _is_acronym(p):
            continue          # no bare head nouns out of a definition
        terms.add(p)
        if not is_definition and len(words) > 1 and len(words[-1]) >= 4:
            terms.add(words[-1])          # head noun, gated by frequency later
    return terms


def term_regex(term):
    """Word-boundary, whitespace-tolerant, plural-tolerant pattern.
    Word boundaries matter: \\btransformer\\b must not fire on
    'digital transformation'."""
    esc = r"\s+".join(re.escape(w) for w in term.split())
    return r"\y" + esc + r"s?\y"


def gate_terms_by_corpus_frequency(
    cur, all_terms, verbose=True, country="IN", as_of=None,
):
    """Keep only terms specific enough to carry information.

    Estimated on a random document sample rather than a full scan per term --
    a frequency threshold does not need census precision, and this keeps the
    build to one heavy pass per product instead of one per candidate term."""
    # seeded so the same corpus always yields the same term set -- an
    # unseeded sample made borderline terms flip between runs and silently
    # changed the investable universe from one build to the next
    cur.execute("SELECT setseed(%s)", (DF_SEED,))
    if as_of is None:
        cur.execute("""SELECT raw_text FROM mg_documents
                       WHERE country=%s AND raw_text IS NOT NULL
                       ORDER BY random() LIMIT %s""", (country, DF_SAMPLE))
    else:
        cur.execute("""SELECT raw_text FROM mg_documents
                       WHERE country=%s AND raw_text IS NOT NULL
                         AND filed_at::date <= %s
                       ORDER BY random() LIMIT %s""", (country, as_of, DF_SAMPLE))
    sample = [r["raw_text"][:40000] for r in cur.fetchall()]
    n = len(sample) or 1
    kept, dropped = {}, {}
    for term in sorted(all_terms):
        pat = re.compile(term_regex(term).replace(r"\y", r"\b"), re.I)
        df = sum(1 for txt in sample if pat.search(txt)) / n
        (kept if df <= CORPUS_DF_MAX else dropped)[term] = round(df, 4)
    if verbose:
        print(f"  corpus-frequency gate on {n} sampled docs "
              f"(max DF {CORPUS_DF_MAX:.0%}): kept {len(kept)}, dropped {len(dropped)}")
        if dropped:
            worst = sorted(dropped.items(), key=lambda kv: -kv[1])[:12]
            print("    dropped as too generic: " +
                  ", ".join(f"{t}({d:.1%})" for t, d in worst))
    return kept, dropped


def _patterns_for(terms):
    joined = "|".join(term_regex(t) for t in terms)
    prod_pat = r"(?:" + joined + r")"
    mfg_prox = (prod_pat + PROX + r"(?:" + MFG_EVIDENCE + r")"
                r"|(?:" + MFG_EVIDENCE + r")" + PROX + prod_pat)
    trade_prox = (prod_pat + PROX + r"(?:" + TRADE_ONLY + r")"
                  r"|(?:" + TRADE_ONLY + r")" + PROX + prod_pat)
    return prod_pat, mfg_prox, trade_prox


def _maker_evidence_detail(text, product_re):
    """Return an auditable direct-product manufacturing proof, if one exists.

    There are three intentionally separate outcomes:

    * ``operating``: direct product + production action + owned physical asset;
    * ``pipeline``: the same direct product proof, but the filing says the
      facility is proposed, under construction, or subject to an agreement;
    * ``direct_role_unquantified``: the company/subsidiary explicitly reports
      making the product but does not disclose a physical asset nearby.

    The last outcome fixes the historic Websol/Indosolar-type blind spot while
    remaining a research lead, not a verified maker.  A buyer, EPC contractor,
    or generic industry statement still fails because it has no ownership
    context next to the exact product.
    """
    if not text:
        return None
    action_re = re.compile(STRICT_MFG_ACTION, re.I)
    asset_re = re.compile(STRICT_MFG_ASSET, re.I)
    consumer_re = re.compile(CONSUMER_CONTEXT, re.I)
    ownership_re = re.compile(OWNERSHIP_CONTEXT, re.I)
    direct_ownership_re = re.compile(DIRECT_OWNERSHIP_CONTEXT, re.I)
    group_ownership_re = re.compile(GROUP_OWNERSHIP_CONTEXT, re.I)
    pipeline_re = re.compile(PIPELINE_CONTEXT, re.I)
    operating_re = re.compile(OPERATING_CONTEXT, re.I)
    business_object_re = re.compile(BUSINESS_OBJECT_CONTEXT, re.I)
    macro_or_input_re = re.compile(MACRO_OR_INPUT_CONTEXT, re.I)

    def distance(hit, match, offset=0):
        """Characters separating an evidence phrase from the product phrase."""
        match_start = match.start() + offset
        match_end = match.end() + offset
        if match_end <= hit.start():
            return hit.start() - match_end
        if match_start >= hit.end():
            return match_start - hit.end()
        return 0

    def local_disclosure(hit):
        """Return the bullet/paragraph containing the product, not a page.

        Investor presentations commonly place a market statement in one
        bullet and the issuer's factory in the next.  A page-wide proximity
        rule incorrectly fuses those two claims (the solar-glass false
        positive found in the 2022 review is the canonical failure).  Newline
        and bullet boundaries are stable across document formats; when a PDF
        loses them, the bounded fallback remains deliberately narrow.
        """
        # A single newline is normally just PDF line wrapping.  Only a blank
        # line or a presentation bullet starts a new disclosure unit.
        boundaries = [m for m in re.finditer(r"(?:[\r\n\f]\s*){2,}|[•▪◦]", text)]
        left = 0
        right = len(text)
        for boundary in boundaries:
            if boundary.end() <= hit.start():
                left = boundary.end()
            elif boundary.start() >= hit.end():
                right = boundary.start()
                break
        # Bad PDF extraction can leave a whole page as one paragraph.  It is
        # safer to miss an unusual disclosure than to join unrelated claims.
        if right - left > 700:
            left = max(left, hit.start() - 300)
            right = min(right, hit.end() + 300)
        return left, right, text[left:right]

    for product_hit in product_re.finditer(text):
        start, end, window = local_disclosure(product_hit)
        actions = list(action_re.finditer(window))
        assets = list(asset_re.finditer(window))
        ownerships = list(ownership_re.finditer(window))
        if not actions:
            continue
        # Product, manufacturing action, and asset must be tied inside the
        # same disclosure unit.  The bounds are intentionally modest: an
        # asset 500 characters away is usually a different table row, bullet,
        # or industry commentary rather than a property of this product.
        action = min(actions, key=lambda m: distance(product_hit, m, start))
        if distance(product_hit, action, start) > 260:
            continue
        action_start, action_end = action.start() + start, action.end() + start
        asset = (min(assets, key=lambda m: distance(product_hit, m, start))
                 if assets else None)
        physical = bool(asset and distance(product_hit, asset, start) <= 300)
        ownership = (min(ownerships, key=lambda m: distance(product_hit, m, start))
                     if ownerships else None)
        owned_role = bool(ownership and distance(product_hit, ownership, start) <= 300)
        if not (physical or owned_role):
            continue
        # A customer can discuss a product and a factory in the same filing.
        # Ownership or a direct production asset must therefore be local to
        # the named product; consumer-only language is never maker proof.
        consumer = consumer_re.search(window)
        if consumer and not owned_role and not physical:
            continue
        # If the product is being described as an input or supplied by someone
        # else, the issuer's nearby plant is normally making a downstream
        # product.  This prevents an EPC/module maker from becoming a cell
        # maker merely because it discusses its cell supply.
        macro_hits = list(macro_or_input_re.finditer(window))
        input_role_near = any(distance(product_hit, m, start) <= 180 for m in macro_hits)
        if input_role_near:
            continue
        # A statutory business-object list is not a direct role.  It may
        # still point to a real capacity project, but only a product-coupled
        # physical asset can move it into the research universe.
        if business_object_re.search(window) and not physical:
            continue
        # With no physical asset, require a company/product role rather than
        # a macro or policy commentary.  The disclosure can still become a
        # direct-role lead if it explicitly names the company/segment.
        if not physical and macro_hits:
            continue
        anchors = [product_hit.start(), action_start]
        if asset:
            anchors.append(asset.start() + start)
        left = max(0, min(anchors) - 90)
        if ownership:
            left = max(0, min(left, ownership.start() + start - 90))
        right = min(len(text), max(product_hit.end(), action_end,
                                  asset.end() + start if asset else 0) + 220)
        excerpt = re.sub(r"\s+", " ", text[left:right]).strip()[:500]
        # A proposed plant does not become an operating manufacturer merely
        # because the disclosure contains the word "manufacturing".
        has_pipeline = bool(pipeline_re.search(window))
        has_operating = bool(physical and operating_re.search(window))
        direct_owner = bool(direct_ownership_re.search(window))
        group_owner = bool(group_ownership_re.search(window)) and not direct_owner
        state = ("operating_and_expanding" if has_pipeline and has_operating else
                 "pipeline" if has_pipeline else
                 "operating" if has_operating else
                 "direct_role_unquantified" if not physical else
                 "capacity_disclosed")
        return {
            "excerpt": excerpt,
            "state": state,
            "physical_asset": physical,
            "ownership_context": owned_role,
            "evidence_scope": "local product/action disclosure",
            "entity_scope": ("group disclosure" if group_owner else
                             "company/subsidiary disclosure" if direct_owner else
                             "listed entity disclosure"),
        }
    return None


_DOCUMENT_PRODUCT_BUSINESS_RE = re.compile(
    r"product portfolio|product range|our products|key products|business (?:comprises|includes)|"
    r"engaged in|design(?:s|ed|ing)?|suppl(?:y|ies|ied)|manufactur(?:e|es|ed|ing)",
    re.I,
)
_DOCUMENT_ISSUER_MANUFACTURING_RE = re.compile(
    r"(?:\bour\b|\bwe\b|\bthe company\b|\bsubsidiar\w*\b|\bdivision\b)"
    r".{0,140}?(?:manufactur\w*|production (?:unit|plant|facilit|capacity)|"
    r"installed capacity|\bplant\b|\bfacilit\w*)|"
    r"(?:manufactur\w*|production (?:unit|plant|facilit|capacity)|installed capacity|"
    r"\bplant\b|\bfacilit\w*).{0,140}?"
    r"(?:\bour\b|\bwe\b|\bthe company\b|\bsubsidiar\w*\b|\bdivision\b)",
    re.I | re.S,
)
_DOCUMENT_ROLE_NEGATIVE_RE = re.compile(
    r"not (?:a )?manufactur(?:er|ing)|acquir(?:e|ed|ing)|acquisition|"
    r"target company|successful bidders?|industry (?:capacity|production)|"
    r"eligible (?:applicants?|manufacturers?)",
    re.I,
)
_DOCUMENT_PRODUCT_END_USE_RE = re.compile(
    r"procure|purchas|buy(?:ing)?|consum(?:e|ed|ption)|end[- ]user|"
    r"project developer|installation|deployment|(?:input|raw material)\s+(?:for|to)|"
    r"used (?:by|for)|customs duty|tariff|government (?:of|has)|policy|"
    r"successful bidders?|eligible (?:applicants?|manufacturers?)",
    re.I,
)


def _document_role_candidate_detail(text, product_re):
    """Return a document-level product/manufacturing *candidate*, never proof.

    PDF tables often separate a product list from the issuer's plant/capacity
    description.  The strict local rule correctly refuses to fuse those
    passages, but throwing the issuer away creates a systematic recall hole.
    This fallback preserves it in a separately labelled review population
    when the exact product is repeated in issuer-business language and the
    same filing independently describes issuer-owned manufacturing.

    It intentionally cannot return ``operating`` or ``pipeline`` and therefore
    cannot satisfy a company or Buy gate.  Customer/input, policy, transaction,
    and statutory-object passages remain negative controls.
    """
    if not text:
        return None
    issuer_manufacturing = list(_DOCUMENT_ISSUER_MANUFACTURING_RE.finditer(text))
    if not issuer_manufacturing:
        return None
    hits = list(product_re.finditer(text))
    if not hits:
        return None
    qualified = []
    for hit in hits[:40]:
        left, right = max(0, hit.start() - 260), min(len(text), hit.end() + 300)
        context = text[left:right]
        if (_DOCUMENT_PRODUCT_END_USE_RE.search(context) or
                re.search(BUSINESS_OBJECT_CONTEXT, context, re.I) or
                _DOCUMENT_ROLE_NEGATIVE_RE.search(context)):
            continue
        if _DOCUMENT_PRODUCT_BUSINESS_RE.search(context):
            qualified.append((hit, context))
    # Repetition is the generic safeguard against a one-off table or market
    # mention. A single hit survives only when its own context explicitly
    # describes the issuer's product business; it still remains unverified.
    if not qualified or (len(hits) < 2 and not re.search(
            r"our products|product portfolio|product range|engaged in|"
            r"(?:we|the company)\s+manufactur", qualified[0][1], re.I)):
        return None
    # Reject documents whose only purported issuer-manufacturing passage is a
    # negation, an acquisition target, or an industry/policy population.
    if all(_DOCUMENT_ROLE_NEGATIVE_RE.search(
            text[max(0, match.start() - 180):min(len(text), match.end() + 180)]
    ) for match in issuer_manufacturing):
        return None
    hit, context = qualified[0]
    excerpt = re.sub(r"\s+", " ", context).strip()[:500]
    return {
        "excerpt": excerpt,
        "state": "document_role_candidate",
        "physical_asset": False,
        "ownership_context": True,
        "evidence_scope": "document-level corroboration; local product/asset binding unresolved",
        "entity_scope": "issuer document; entity/product binding requires review",
    }


def _strict_evidence_excerpt(text, product_re):
    """Compatibility wrapper for callers that only need the proof passage."""
    detail = _maker_evidence_detail(text, product_re)
    return detail["excerpt"] if detail else None


def strict_maker_evidence(cur, product, tickers, as_of, lookback_days=1095):
    """Fetch dated own-filing proof passages for the final maker gate.

    This is separate from the broad capability scan because the selector must
    show an investor exactly *why* a company was treated as a manufacturer.
    """
    if not tickers:
        return {}
    terms = {t for t in candidate_terms(product) if len(t.split()) > 1 or _is_acronym(t)}
    if not terms:
        return {}
    product_re = re.compile("|".join(term_regex(t).replace(r"\y", r"\b") for t in terms), re.I)
    product_pat = "(?:" + "|".join(term_regex(t) for t in terms) + ")"
    cur.execute("""
        SELECT UPPER(TRIM(ticker)) AS ticker, filed_at::date AS filed_at,
               COALESCE(title, '') AS title, url, raw_text
        FROM mg_documents
        WHERE country='IN' AND ticker = ANY(%s)
          AND filed_at BETWEEN %s AND %s
          AND raw_text IS NOT NULL AND raw_text ~* %s
        ORDER BY ticker, filed_at DESC
    """, (list(tickers), as_of.fromordinal(as_of.toordinal() - lookback_days), as_of, product_pat))
    out = defaultdict(list)
    seen_dates = defaultdict(set)
    seen_events = defaultdict(set)
    for row in cur.fetchall():
        ticker = row["ticker"]
        if len(out[ticker]) >= 4:
            continue
        detail = (_maker_evidence_detail(row["raw_text"], product_re) or
                  _document_role_candidate_detail(row["raw_text"], product_re))
        if not detail or row["filed_at"] in seen_dates[ticker]:
            continue
        if any(abs((row["filed_at"] - prior).days) < MIN_INDEPENDENT_EVENT_GAP_DAYS
               for prior in seen_dates[ticker]):
            continue
        # Independent means a new dated operating/capex event, not the same
        # PR re-filed through multiple exchange notices.  Keep the audit trail
        # short, but do not stop before finding older distinct evidence.
        signature = re.sub(r"[^a-z0-9]+", "", detail["excerpt"].lower())[:280]
        if signature in seen_events[ticker]:
            continue
        seen_dates[ticker].add(row["filed_at"])
        seen_events[ticker].add(signature)
        out[ticker].append({
            "filed_at": row["filed_at"].isoformat(),
            "title": row["title"][:140],
            "url": row["url"],
            **detail,
        })
    return dict(out)


def strict_maker_evidence_batch(cur, product_tickers, as_of, lookback_days=1095):
    """Recover strict proof passages for many product/ticker groups in one read.

    ``capability_makers`` formerly issued one raw-document regex query per
    constrained product.  A point-in-time selector can easily contain dozens
    of products, so a multi-anchor replay spent nearly all of its time
    repeatedly reading the same issuer filings.  This function reads the
    union of the already capability-screened tickers once, then applies each
    product's exact proof rule only to the products assigned to that issuer.

    It preserves the evidence semantics of :func:`strict_maker_evidence`:
    exact product terms, own-filing passage validation, dated-event
    de-duplication, and at most four proofs per product/ticker.  It is a
    query-plan improvement only; it neither broadens a product match nor
    changes the committee gate.
    """
    product_res = {}
    product_patterns = []
    products_by_ticker = defaultdict(list)
    for product, tickers in (product_tickers or {}).items():
        terms = {term for term in candidate_terms(product)
                 if len(term.split()) > 1 or _is_acronym(term)}
        if not terms:
            continue
        term_patterns = [term_regex(term) for term in terms]
        product_res[product] = re.compile(
            "|".join(pattern.replace(r"\y", r"\b") for pattern in term_patterns), re.I
        )
        # Same exact terms as the Python proof extractor, but used here as a
        # single SQL prefilter.  Without it, the batch correctly examined
        # every filing of the candidate issuers, which became needlessly
        # expensive once the newer snapshots contained far more filings.
        product_patterns.append("(?:" + "|".join(term_patterns) + ")")
        for ticker in tickers or []:
            if ticker:
                products_by_ticker[(ticker or "").upper()].append(product)
    if not products_by_ticker:
        return {product: {} for product in (product_tickers or {})}

    tickers = sorted(products_by_ticker)
    cur.execute("""
        SELECT UPPER(TRIM(ticker)) AS ticker, filed_at::date AS filed_at,
               COALESCE(title, '') AS title, url, raw_text
        FROM mg_documents
        WHERE country='IN' AND ticker = ANY(%s)
          AND filed_at BETWEEN %s AND %s
          AND raw_text IS NOT NULL
          AND raw_text ~* %s
        ORDER BY ticker, filed_at DESC
    """, (tickers, as_of.fromordinal(as_of.toordinal() - lookback_days), as_of,
          "(?:" + "|".join(product_patterns) + ")"))

    out = {product: defaultdict(list) for product in product_res}
    seen_dates = {product: defaultdict(set) for product in product_res}
    seen_events = {product: defaultdict(set) for product in product_res}
    for row in cur.fetchall():
        ticker = (row.get("ticker") or "").upper()
        for product in products_by_ticker.get(ticker, []):
            product_re = product_res.get(product)
            if product_re is None or len(out[product][ticker]) >= 4:
                continue
            detail = (_maker_evidence_detail(row["raw_text"], product_re) or
                      _document_role_candidate_detail(row["raw_text"], product_re))
            if not detail or row["filed_at"] in seen_dates[product][ticker]:
                continue
            if any(abs((row["filed_at"] - prior).days) < MIN_INDEPENDENT_EVENT_GAP_DAYS
                   for prior in seen_dates[product][ticker]):
                continue
            signature = re.sub(r"[^a-z0-9]+", "", detail["excerpt"].lower())[:280]
            if signature in seen_events[product][ticker]:
                continue
            seen_dates[product][ticker].add(row["filed_at"])
            seen_events[product][ticker].add(signature)
            out[product][ticker].append({
                "filed_at": row["filed_at"].isoformat(),
                "title": row["title"][:140],
                "url": row["url"],
                **detail,
            })
    return {product: dict(by_ticker) for product, by_ticker in out.items()}


def scan_product(cur, label, terms):
    """One corpus pass for a single product (used by the coherence retry)."""
    if not terms:
        return []
    prod_pat, mfg_prox, trade_prox = _patterns_for(terms)
    cur.execute("""
        SELECT UPPER(TRIM(ticker)) AS ticker,
               date_trunc('quarter', filed_at)::date AS q,
               COUNT(*) AS n_docs,
               COUNT(*) FILTER (WHERE raw_text ~* %s) AS mfg_docs,
               COUNT(*) FILTER (WHERE raw_text ~* %s) AS trade_docs
        FROM mg_documents
        WHERE country='IN' AND ticker IS NOT NULL AND ticker <> ''
          AND raw_text IS NOT NULL AND raw_text ~* %s
        GROUP BY 1, 2
    """, (mfg_prox, trade_prox, prod_pat))
    return cur.fetchall()


def scan_all_single_pass(cur, usable, chunk_rows=2000, progress_every=20000):
    """Scan the corpus ONCE for every product, in Python.

    Why this exists: the SQL approach costs docs x products, because each
    product (or batch of products) re-reads all ~150k documents. Measured at
    ~35 min per 12-product batch, a 152-product vocabulary was a ~7.5 hour
    build; parallelising to 6 workers barely helped because the work is
    CPU-bound regex evaluation and the backends just contended for cores.

    This inverts the loop. Per document it runs exactly TWO regex passes,
    independent of how many products are tracked:
      1. one master alternation of every product term -> which products the
         document mentions and WHERE
      2. one pass for manufacturing / trading language -> where that appears
    Proximity is then a position comparison, not another regex. Cost becomes
    docs x text, so adding products is nearly free — which is the whole point
    of moving from 22 hand-curated products to the HS taxonomy.

    Terms are unique to one product by construction (the ambiguity gate drops
    any term two products share), so a matched string maps to exactly one.
    """
    term_owner, alts = {}, []
    for lb, terms in usable.items():
        for t in terms:
            term_owner[t.lower()] = (lb, t)
            alts.append(term_regex(t).replace(r"\y", r"\b"))
    master = re.compile("|".join(alts), re.I)
    mfg_re = re.compile(MFG_EVIDENCE, re.I)
    trade_re = re.compile(TRADE_ONLY, re.I)
    # DOCUMENT-LEVEL objects-clause guard. A memorandum / statutory-objects
    # document enumerates many "to manufacture / to trade / to deal in ..."
    # intentions over a product catalog, so EVERY product in it sits near
    # "manufactur" — the proximity gate cannot tell "we make radar" from a
    # catalog listing "Radar Device" beside "drone manufacturing". Three or more
    # infinitive commerce-objects markers in one document means the whole thing
    # is legal scope, not operating evidence: its manufacturing language is not
    # counted for any product. This is what tagged NITIRAJ (weighing / home-
    # automation maker, objects doc with 4 markers/doc) as a defense-electronics
    # manufacturer; genuine makers (BEL/DATAPATTNS/SONACOMS max 2) are untouched,
    # and a maker with some objects-heavy docs (AXISCADES) survives on its many
    # ordinary operating filings.
    objects_item_re = re.compile(
        r"\bto\s+(?:manufactur\w*|produc\w*|trad\w*|\bdeal\b|import\w*|export\w*|"
        r"carry\s+on|market\w*)", re.I)
    OBJECTS_DOC_MIN = 3
    # A product term that is part of a COMPANY NAME ("Gujarat Fluorochemicals
    # Limited", named as a group affiliate / guarantor / a director's past
    # employer) is not evidence the FILER makes that product. Inox Wind was
    # tagged a fluorochemicals maker purely by such name mentions. A term
    # immediately followed by a corporate suffix is dropped; a genuine maker
    # also states the product outside its name ("manufacture of fluorochemicals")
    # and keeps those hits (verified: NAVINFLUOR 29->28, FLUOROCHEM 15->11).
    company_suffix_re = re.compile(
        r"\s*(?:limited|ltd\.?|pvt\.?|private|llp|incorporated)\b", re.I)
    # GROUP-PORTFOLIO boilerplate: "The Group's interests span diversified
    # business segments comprising fluoropolymers, fluorochemicals, ... wind
    # turbines and renewables" — describes a parent conglomerate's portfolio
    # ACROSS MULTIPLE LISTED ENTITIES, not what the filer itself makes. This is
    # what kept Inox Wind (a wind-turbine maker) tagged a fluorochemicals maker
    # even after the company-suffix guard, because the boilerplate names the
    # product without a corporate suffix directly attached. A term inside a
    # "the Group ... comprising/spans/segments/listed entities" window is
    # dropped. Verified: kills Inox Wind/Inox Green's fluorochemicals tag
    # entirely, keeps every genuine fluorochemicals maker's own count intact
    # (NAVINFLUOR/FLUOROCHEM/SRF/GFL unaffected).
    group_portfolio_re = re.compile(
        r"\bthe\s+group\b.{0,140}?(?:comprising|span(?:ning)?|segments?|"
        r"interests|listed\s+entities|diversif\w*)", re.I | re.S)
    window = 500          # same ~500-char proximity the SQL version used

    agg = defaultdict(lambda: {"n_docs": 0, "mfg_docs": 0, "trade_docs": 0})
    term_hits = defaultdict(set)     # term -> tickers it evidenced
    # server-side (named) cursor — the corpus is ~2.6GB and a client-side
    # cursor would buffer all of it in memory before the first row
    scur = cur.connection.cursor(name="cap_scan",
                                 cursor_factory=psycopg2.extras.RealDictCursor)
    scur.itersize = chunk_rows
    scur.execute("""SELECT UPPER(TRIM(ticker)) AS ticker,
                           date_trunc('quarter', filed_at)::date AS q,
                           raw_text
                    FROM mg_documents
                    WHERE country='IN' AND ticker IS NOT NULL AND ticker <> ''
                      AND raw_text IS NOT NULL""")
    seen = 0
    while True:
        rows = scur.fetchmany(chunk_rows)
        if not rows:
            break
        for r in rows:
            seen += 1
            if progress_every and seen % progress_every == 0:
                print(f"    scanned {seen} documents", flush=True)
            txt = r["raw_text"]
            hits, hit_terms = {}, {}
            for m in master.finditer(txt):
                raw = m.group(0).lower()
                key = term_owner.get(raw.rstrip("s")) or term_owner.get(raw)
                if key is None:
                    continue
                if company_suffix_re.match(txt, m.end()):
                    continue      # term is part of a company NAME, not a product
                if group_portfolio_re.search(
                        txt[max(0, m.start() - 200):m.end() + 200]):
                    continue      # group-wide portfolio boilerplate, not this filer's product
                lb, tm = key
                hits.setdefault(lb, []).append(m.start())
                hit_terms.setdefault(lb, set()).add(tm)
            if not hits:
                continue
            # An objects-clause / catalog document carries no operating evidence
            # for any single product; drop its manufacturing positions entirely.
            if len(objects_item_re.findall(txt)) >= OBJECTS_DOC_MIN:
                mfg_pos = []
            else:
                mfg_pos = [m.start() for m in mfg_re.finditer(txt)]
            trade_pos = [m.start() for m in trade_re.finditer(txt)]

            def near(positions, targets):
                for p in positions:
                    for t in targets:
                        if abs(t - p) <= window:
                            return True
                return False

            for lb, positions in hits.items():
                e = agg[(r["ticker"], r["q"], lb)]
                e["n_docs"] += 1
                if mfg_pos and near(positions, mfg_pos):
                    e["mfg_docs"] += 1
                    # remember WHICH term carried the manufacturing evidence,
                    # so a term whose matches scatter across unrelated sectors
                    # can be identified and removed afterwards
                    for tm in hit_terms.get(lb, ()):
                        term_hits[tm].add(r["ticker"])
                if trade_pos and near(positions, trade_pos):
                    e["trade_docs"] += 1
    print(f"    scanned {seen} documents total", flush=True)

    scans = {lb: [] for lb in usable}
    for (ticker, q, lb), e in agg.items():
        scans[lb].append({"ticker": ticker, "q": q, **e})
    return scans, term_hits


def scan_batch(cur, batch):
    """One corpus pass for a BATCH of products.

    Scanning per-product costs a full pass over ~150k documents (2.6GB) each
    time; at 22 products that was ~15 minutes, and the HS taxonomy pushes the
    vocabulary past 130. Batching evaluates every product's patterns as
    FILTER clauses inside ONE table pass, so I/O scales with the number of
    BATCHES rather than the number of products. The pre-filter is the OR of
    all products in the batch, so a row is only regex-tested per product when
    it already matched something.

    batch: [(label, terms), ...] -> {label: rows}
    """
    if not batch:
        return {}
    sel, params, any_pat = [], [], []
    for i, (_lb, terms) in enumerate(batch):
        prod_pat, mfg_prox, trade_prox = _patterns_for(terms)
        sel.append(f"COUNT(*) FILTER (WHERE raw_text ~* %s) AS n{i}, "
                   f"COUNT(*) FILTER (WHERE raw_text ~* %s) AS m{i}, "
                   f"COUNT(*) FILTER (WHERE raw_text ~* %s) AS t{i}")
        params += [prod_pat, mfg_prox, trade_prox]
        any_pat.append(prod_pat)
    sql = f"""
        SELECT UPPER(TRIM(ticker)) AS ticker,
               date_trunc('quarter', filed_at)::date AS q,
               {', '.join(sel)}
        FROM mg_documents
        WHERE country='IN' AND ticker IS NOT NULL AND ticker <> ''
          AND raw_text IS NOT NULL AND raw_text ~* %s
        GROUP BY 1, 2
    """
    params.append("(?:" + "|".join(any_pat) + ")")
    cur.execute(sql, params)
    rows = cur.fetchall()
    out = {lb: [] for lb, _ in batch}
    for r in rows:
        for i, (lb, _terms) in enumerate(batch):
            n = int(r[f"n{i}"] or 0)
            if n == 0:
                continue
            out[lb].append({"ticker": r["ticker"], "q": r["q"], "n_docs": n,
                            "mfg_docs": int(r[f"m{i}"] or 0),
                            "trade_docs": int(r[f"t{i}"] or 0)})
    return out


TERM_MIN_N = 8              # below this a term's sector mix is not evidence
TERM_MIN_SECTOR_SHARE = 0.5  # a real product term concentrates in one sector
COH_MIN_N = 15        # below this, an industry mix is too small to judge
COH_MIN_SHARE = 0.25  # top-industry share required when n is large enough


def industry_of(cur):
    cur.execute("""SELECT UPPER(TRIM(nse_symbol)) AS t,
                          COALESCE(industry_bse, industry_nse, '(none)') AS ind
                   FROM security_master WHERE nse_symbol IS NOT NULL""")
    return {r["t"]: r["ind"] for r in cur.fetchall()}


def coherence(tickers, ind_map):
    """Top-industry share of a product's tagged population.

    A real product tags an industry-coherent set (transformer makers are
    electrical-equipment companies). A leaking term tags everyone: the bare
    head noun "Panels" pulled in plywood, laminate, steel and cement names
    at 12.5% coherence, while the same rule left "Transformer" (47%) and
    "APIs" (71%) alone. This is measured from the population, not from any
    prior belief about which industries exist -- which is what keeps it a
    rule rather than a hardcoded exception."""
    if not tickers:
        return 0.0, 0
    counts = Counter(ind_map.get(t, "(none)") for t in tickers)
    return counts.most_common(1)[0][1] / len(tickers), len(tickers)


def rollup(scans, as_of):
    """Point-in-time roll-up: only quarters filed <= as_of and inside the
    trailing capability window count."""
    start = date.fromordinal(as_of.toordinal() - LOOKBACK_DAYS)
    out = defaultdict(lambda: {"n_docs": 0, "mfg": 0, "trade": 0})
    for label, rows in scans.items():
        for r in rows:
            q = r["q"]
            if q is None or q > as_of or q < start:
                continue
            e = out[(r["ticker"], label)]
            e["n_docs"] += int(r["n_docs"] or 0)
            e["mfg"] += int(r["mfg_docs"] or 0)
            e["trade"] += int(r["trade_docs"] or 0)
    return out


# Scarcity tiers, derived from the measured distribution across 11 anchors
# (353 product-anchors): genuine makers per product ran p25=1, median=4,
# p75=10. Cutting at those quartiles is what the +26pp result was measured
# on -- constraints in the narrow half returned 89.2% median vs 63.3% for the
# broad half (86 constraint-anchors, 3-year forward).
SCARCITY_TIERS = ((1, "MONOPOLY"), (4, "SCARCE"), (10, "CONTESTED"))


def compute_constraint_scarcity(cur, as_of, products):
    """NOT VALIDATED — DO NOT WIRE INTO THE REPORT OR ANY GRADE.

    Built to operationalise the one constraint-grading feature that appeared
    to predict (+26.0pp, 86 constraint-anchors). Two checks made before
    shipping killed it:

    1. THE UNDERLYING COUNT IS AN ARTIFACT. mg_india_beneficiaries assigns
       companies per THEME with a cap of 40, so the per-constraint count
       piles up at exactly 40 (16 occurrences) and 80 (7) across snapshots.
       "Narrow cohort" therefore means "mapped to fewer themes, or under-
       covered by the mapper" -- not "few listed companies can serve this".
       The +26pp association is real; the scarcity INTERPRETATION is not
       supported by it.

    2. IT INVERTS AT THE ANCHOR CHECKED. At 2022-12-31 the two NARROW
       constraints returned 59.2% and 33.3% while BROAD ones included the
       two best performers in the whole study (CRGO Steel +148.6%,
       Semiconductor IC +86.2%).

    Replacing the count with a genuine maker count (from this module) does
    not rescue it either: term-coverage gaps make it report zero makers for
    CRGO Steel, the single best constraint.

    Kept as a documented negative result so the same association is not
    rediscovered and shipped later. A real scarcity metric needs a listed-
    universe count per product that is neither theme-capped nor dependent on
    filing-term coverage.
    """
    if not products:
        return {}
    cur.execute("""SELECT MAX(as_of_date) AS s FROM mg_company_capabilities
                   WHERE as_of_date <= %s""", (as_of,))
    row = cur.fetchone()
    snap = row["s"] if row else None
    if not snap:
        return {}

    cur.execute("""SELECT product, COUNT(*) AS n_makers,
                          COUNT(*) FILTER (WHERE mfg_docs >= 4) AS n_strong
                   FROM mg_company_capabilities
                   WHERE as_of_date = %s AND manufacturer AND product = ANY(%s)
                   GROUP BY product""", (snap, list(products)))
    counts = {r["product"]: r for r in cur.fetchall()}

    # The VALIDATED signal is the mapped-cohort count -- that is what the
    # +26pp was measured on. The maker count is the cleaner concept but was
    # never itself backtested, and where its terms miss it reports zero: at
    # Dec-2022 it returns 0 makers for CRGO Steel, the single best-performing
    # constraint in the study (+148.6%). Absence of evidence is not evidence
    # of absence, so the maker count is carried as a CROSS-CHECK and a
    # disagreement between the two is surfaced for review, never silently
    # resolved in favour of either.
    cur.execute("""SELECT MAX(as_of_date) AS s FROM mg_india_beneficiaries
                   WHERE as_of_date <= %s""", (as_of,))
    brow = cur.fetchone()
    mapped = {}
    if brow and brow["s"]:
        cur.execute("""SELECT constrained_product AS p, COUNT(*) AS n
                       FROM mg_india_beneficiaries
                       WHERE as_of_date = %s AND constrained_product = ANY(%s)
                       GROUP BY 1""", (brow["s"], list(products)))
        mapped = {r["p"]: int(r["n"]) for r in cur.fetchall()}
    med_mapped = (sorted(mapped.values())[len(mapped) // 2] if mapped else None)

    out = {}
    for p in products:
        r = counts.get(p)
        n = int(r["n_makers"]) if r else 0
        strong = int(r["n_strong"]) if r else 0
        n_mapped = mapped.get(p)

        # primary grade: the measured signal (narrow cohort beat broad by 26pp)
        if n_mapped is None or med_mapped is None:
            grade, note = "UNGRADED", "no mapped cohort at this snapshot"
        elif n_mapped < med_mapped:
            grade = "NARROW"
            note = ("fewer listed vehicles than the median constraint — the "
                    "band that measured 89.2% vs 63.3% over 3 years")
        else:
            grade = "BROAD"
            note = ("as many or more listed vehicles than the median "
                    "constraint — the half that measured 63.3%")

        tier = "CROWDED"
        for cut, name in SCARCITY_TIERS:
            if n <= cut:
                tier = name
                break
        if n == 0:
            tier = "NO_MAKER_FOUND"

        disagree = (n == 0 and (n_mapped or 0) > 0)
        out[p] = {
            "grade": grade, "note": note,
            "n_mapped": n_mapped, "median_mapped": med_mapped,
            "n_makers": n, "n_strong_evidence": strong, "maker_tier": tier,
            "needs_review": disagree,
            "review_reason": ("mapped cohort exists but the capability mapper "
                              "found no manufacturer — likely a product-term "
                              "coverage gap, not an absent industry; verify "
                              "before treating as uninvestable" if disagree else None),
            "snapshot": snap.isoformat(),
        }
    return out


def capability_makers(cur, as_of, products, min_mfg_docs=1, limit_per_product=40):
    """Return the as-of, constraint-scoped *direct producer* universe.

    The capability table is a recall-oriented discovery index; a single
    product/manufacturing hit can be enough to find a small 2022 capacity
    announcement.  The report-facing classification below then decides
    whether that hit establishes an operating maker, an evidenced pipeline,
    a direct role with capacity still unquantified, or nothing usable.

    Coarse exchange industry labels are retained as context only.  They must
    never veto exact own-filing product/capacity evidence: a listed parent can
    own the manufacturing subsidiary while being classified as power, trading,
    or diversified capital goods.
    """
    if not products:
        return {}
    cur.execute("""SELECT MAX(as_of_date) AS s FROM mg_company_capabilities
                   WHERE as_of_date <= %s""", (as_of,))
    row = cur.fetchone()
    snap = row["s"] if row else None
    if not snap:
        return {product: {
            "snapshot": None, "modal_industry": None, "makers": [],
            "role_candidates": [], "rejected_candidates": [],
            "coverage": {"status": "no capability snapshot available", "candidate_count": 0},
        } for product in products}

    cur.execute("""
        SELECT c.product, c.ticker, c.n_docs, c.mfg_docs,
               COALESCE(s.company_name, '') AS company,
               COALESCE(s.industry_bse, s.industry_nse, '') AS industry
        FROM mg_company_capabilities c
        LEFT JOIN security_master s ON UPPER(TRIM(s.nse_symbol)) = c.ticker
        WHERE c.as_of_date = %s AND c.manufacturer
          AND c.mfg_docs >= %s AND c.product = ANY(%s)
        ORDER BY c.product, c.mfg_docs DESC, c.n_docs DESC
    """, (snap, min_mfg_docs, list(products)))

    by_product = defaultdict(list)
    for r in cur.fetchall():
        by_product[r["product"]].append(dict(r))

    candidate_tickers = {
        product: [row["ticker"] for row in by_product.get(product, [])[:limit_per_product]]
        for product in products
    }
    evidence_by_product = strict_maker_evidence_batch(
        cur, candidate_tickers, as_of
    )

    out = {}
    for product in products:
        rows = by_product.get(product, [])
        modal = Counter(r["industry"] for r in rows[:8] if r["industry"]).most_common(1)
        modal_ind = modal[0][0] if modal else None
        evidence_by_ticker = evidence_by_product.get(product, {})
        makers, role_candidates, rejected = [], [], []
        for r in rows[:limit_per_product]:
            mismatch = bool(modal_ind and r["industry"] and
                            r["industry"].split("|")[0].strip()
                            != modal_ind.split("|")[0].strip())
            samples = evidence_by_ticker.get(r["ticker"], [])
            local_samples = [s for s in samples
                             if s.get("state") != "document_role_candidate"]
            document_samples = [s for s in samples
                                if s.get("state") == "document_role_candidate"]
            physical = [s for s in samples if s.get("physical_asset")]
            # A parent can disclose a group manufacturing asset without being
            # the listed manufacturing entity.  It remains a useful capacity
            # lead, but it must not satisfy the direct operating-maker gate.
            operating = [s for s in physical
                         if s.get("state") in ("operating", "operating_and_expanding")
                         and s.get("entity_scope") != "group disclosure"]
            pipeline = [s for s in samples
                        if s.get("state") in ("pipeline", "operating_and_expanding", "capacity_disclosed")
                        or s.get("entity_scope") == "group disclosure"]
            direct_count = len(local_samples)
            document_count = len(document_samples)
            strict_count = len(physical)
            if len(operating) >= 2:
                maker_status = "operating manufacturer — independently corroborated"
                route = "Direct producer research"
                needs_review = False
                reason = None
            elif operating:
                maker_status = "operating manufacturer — single dated physical proof"
                route = "Confirm second operating proof"
                needs_review = True
                reason = "one dated direct operating proof; confirm a second independent event"
            elif pipeline:
                maker_status = "capacity pipeline / group manufacturing plan"
                route = "Monitor commissioning / funding milestone"
                needs_review = True
                reason = "direct product/capacity plan is evidenced, but operating production is not yet proved"
            elif direct_count:
                maker_status = "direct product role — capacity unquantified"
                route = "Verify physical capacity / operating status"
                needs_review = True
                reason = "company filing states a direct product role without a nearby plant or capacity proof"
            elif document_count:
                maker_status = "document-corroborated role candidate — exact local binding unresolved"
                route = "Review issuer product catalogue and plant/capacity disclosure together"
                needs_review = True
                reason = (
                    "the same issuer filing contains exact product-business and issuer-manufacturing evidence, "
                    "but the local product-to-asset sentence was not recovered"
                )
            else:
                maker_status = "not displayed — no direct product proof"
                route = "Reject from maker universe"
                needs_review = True
                reason = "capability-screen hit did not survive exact own-filing product-role validation"
            item = {
                "ticker": r["ticker"], "company": r["company"],
                "industry": r["industry"], "n_docs": r["n_docs"],
                "mfg_docs": r["mfg_docs"],
                "direct_evidence_count": direct_count,
                "document_candidate_evidence_count": document_count,
                "strict_evidence_count": strict_count,
                "last_evidence_date": samples[0]["filed_at"] if samples else None,
                "evidence_samples": samples,
                "evidence": (f"{strict_count} dated direct product-and-physical-asset passages"
                             if strict_count else f"{direct_count} dated direct product-role passages"),
                "maker_status": maker_status,
                "research_route": route,
                "needs_review": needs_review,
                "review_reason": reason,
                "industry_mismatch": mismatch,
                "entity_scope": ("company/subsidiary disclosure" if any(
                    s.get("entity_scope") == "company/subsidiary disclosure" for s in samples) else
                                 "group disclosure" if any(
                    s.get("entity_scope") == "group disclosure" for s in samples) else
                                 "listed entity disclosure"),
                "source": "own-filings product manufacture (no theme document required)",
            }
            # Rejects remain in JSON for audit, but do not pollute the
            # reader-facing maker universe.  This prevents a biscuit wafer or
            # generic renewable mention from looking like a candidate stock.
            if direct_count:
                makers.append(item)
            elif document_count:
                role_candidates.append(item)
            else:
                rejected.append(item)
        makers.sort(key=lambda m: (
            not m["maker_status"].startswith("operating manufacturer — independently"),
            not m["maker_status"].startswith("operating manufacturer"),
            not m["maker_status"].startswith("capacity pipeline"),
            -m["strict_evidence_count"], -m["direct_evidence_count"], m["ticker"]
        ))
        role_candidates.sort(key=lambda m: (
            -m["document_candidate_evidence_count"], -m["mfg_docs"], m["ticker"]
        ))
        out[product] = {"snapshot": snap.isoformat(), "modal_industry": modal_ind,
                        "makers": makers,
                        "role_candidates": role_candidates,
                        "rejected_candidates": rejected,
                        "coverage": {
                            "status": ("direct producer candidates reviewed" if makers else
                                       "document-level role candidates require review" if role_candidates else
                                       "no direct producer recovered from capability snapshot"),
                            "candidate_count": len(rows),
                            "visible_direct_roles": len(makers),
                            "document_role_candidates": len(role_candidates),
                            "rejected_no_product_proof": len(rejected),
                        }}
    return out


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mg_company_capabilities (
                id SERIAL PRIMARY KEY,
                as_of_date DATE NOT NULL,
                ticker TEXT NOT NULL,
                product TEXT NOT NULL,
                n_docs INT,
                mfg_docs INT,
                trade_docs INT,
                manufacturer BOOLEAN,
                computed_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (as_of_date, ticker, product)
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_mg_cap_asof
                       ON mg_company_capabilities (as_of_date)""")
    conn.commit()


def build(conn, cur, as_of_list, dry_run=False, verbose=True, batch_size=12,
          hs_top_n=0, workers=1):
    groups = product_labels(cur, hs_top_n=hs_top_n)
    print(f"Products tracked (from DB rows, variants merged): {len(groups)}"
          + (f" [incl. top-{hs_top_n} HS categories]" if hs_top_n else ""))

    term_map = {}
    for canon, variants in groups.items():
        terms = set()
        for v in variants:
            terms |= candidate_terms(v)
        term_map[canon] = terms

    # AMBIGUITY RULE: a term derived from two different products cannot tell
    # them apart ("Cell" belongs to both Battery Cell and Solar Cell), so it
    # would tag every solar maker as a battery maker and vice versa. Derived
    # from the label set itself — nothing about which terms clash is written
    # down anywhere.
    # compared on a singularised key so "Cell" (Battery Cell) and "Cells"
    # (Solar Cells) are recognised as the same clashing token
    def _tkey(t):
        return " ".join(_singular(w) for w in t.lower().split())

    owners = defaultdict(set)
    for canon, terms in term_map.items():
        for t in terms:
            owners[_tkey(t)].add(canon)

    # A shared term is not automatically ambiguous. Two taxonomies routinely
    # describe the SAME product differently -- curated "Power Transformer"
    # vs HS 8504 "Electrical transformers, static converters and inductors;
    # parts thereof" -- and naively dropping every shared token destroyed
    # exactly the terms that work ("transformer", "circuit", "resistor",
    # "diode", "generator") the moment the HS vocabulary was added.
    #
    # Resolve by CENTRALITY instead: assign the term to the product whose
    # label it most nearly IS. "transformer" is half of "Power Transformer"
    # (0.50) but a small part of the HS sentence (0.12), so it belongs to the
    # former. Only a genuine tie -- "cell" is equally central to "Battery
    # Cell" and "Solar Cell" -- is truly ambiguous and gets dropped.
    def _centrality(term, label):
        lw = len(_norm_label(label).split()) or 1
        return len(term.split()) / lw

    ambiguous, reassigned = set(), {}
    for key, owner_set in owners.items():
        if len(owner_set) < 2:
            continue
        scored = sorted(((_centrality(key, lb), lb) for lb in owner_set),
                        reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            ambiguous.add(key)          # equally central -> genuinely ambiguous
        else:
            reassigned[key] = scored[0][1]
    if verbose:
        if ambiguous:
            print(f"  dropped as genuinely ambiguous: {', '.join(sorted(ambiguous))}")
        if reassigned:
            sample = list(reassigned.items())[:8]
            print(f"  {len(reassigned)} shared terms resolved by centrality, e.g. "
                  + "; ".join(f"{k!r}->{v[:34]!r}" for k, v in sample))

    all_terms = set().union(*term_map.values()) if term_map else set()
    kept, _ = gate_terms_by_corpus_frequency(cur, all_terms, verbose)

    usable = {}
    for lb, terms in term_map.items():
        good = {t for t in terms if t in kept and _tkey(t) not in ambiguous}
        if good:
            usable[lb] = good
        else:
            print(f"  !! no usable terms for product {lb!r} — all too generic/ambiguous")
    print(f"Products with usable search terms: {len(usable)}/{len(groups)}")

    print(f"  single-pass scan over the corpus for all {len(usable)} products",
          flush=True)
    scans, term_hits = scan_all_single_pass(cur, usable)

    # TERM-DISPERSION ADVISORY (not a filter).
    # A term whose manufacturing matches scatter across unrelated SECTORS is
    # ambiguous in the language itself: "Contract Manufacturing" reaches
    # pharma CDMOs as well as electronics assemblers, "Wafer" reaches biscuit
    # makers as well as semiconductors.
    #
    # This was briefly implemented as a FILTER that dropped such terms. That
    # was wrong: at a 50% sector-concentration cut it destroyed Solar Cell,
    # Battery Cell, PCB and Rolling Stock outright -- real constraints, killed
    # because Indian solar and battery firms legitimately span Power, Capital
    # Goods and Chemicals, so a genuine industry reads as "dispersed" against
    # coarse sector labels -- and it STILL admitted a biscuit maker under
    # Solar Wafer. Precision bought that way costs more than it saves.
    #
    # Elimination belongs to the judgment layer. The screener's job is to
    # surface candidates WITH the evidence needed to discard them quickly, so
    # dispersion is now recorded and surfaced, never acted on.
    ind_map0 = industry_of(cur)

    def _sector(t):
        return (ind_map0.get(t, "") or "").split("|")[0].strip()

    ambiguous_terms = {}
    for term, ticks in term_hits.items():
        secs = [s for s in (_sector(t) for t in ticks) if s]
        if len(secs) < TERM_MIN_N:
            continue
        top = Counter(secs).most_common(1)[0][1] / len(secs)
        if top < TERM_MIN_SECTOR_SHARE:
            ambiguous_terms[term] = round(top, 2)
    if ambiguous_terms and verbose:
        print(f"  cross-sector ambiguous terms (FLAGGED, still surfaced): "
              + ", ".join(f"{t}({v:.0%})" for t, v in
                          sorted(ambiguous_terms.items(), key=lambda kv: kv[1])[:14]))

    # INDUSTRY-COHERENCE GATE: a term set that tags an industry-incoherent
    # population is leaking. Retry those products with multi-word phrases
    # only (dropping bare head nouns) and keep the retry if it improves.
    # Judged on the widest snapshot, and only where n is large enough for the
    # mix to mean anything -- a 6-company product cannot be assessed this way.
    ind_map = industry_of(cur)
    widest = max(as_of_list)
    for lb in list(scans):
        agg = rollup({lb: scans[lb]}, widest)
        tick = {t for (t, _), e in agg.items()
                if e["n_docs"] >= MIN_DOCS and e["mfg"] > 0}
        share, n = coherence(tick, ind_map)
        if n < COH_MIN_N or share >= COH_MIN_SHARE:
            continue
        # "narrower" = multi-word phrases PLUS acronyms. An acronym is a
        # distinctive product name, not a generic head noun -- dropping every
        # single-word term wiped out PCB (and would wipe MLCCs, LFP, HVDC),
        # losing a genuinely constrained product entirely. Judged on FORM
        # (short, capitalised) so it stays a rule rather than a word list.
        multi = {t for t in usable[lb]
                 if len(t.split()) > 1 or _is_acronym(t)}
        if not multi or multi == usable[lb]:
            print(f"  !! {lb!r} incoherent ({share:.0%} top industry, n={n}) "
                  f"but no narrower terms available — left as is, REVIEW")
            continue
        print(f"  !! {lb!r} incoherent ({share:.0%} top industry, n={n}) — "
              f"retrying without head nouns: {sorted(usable[lb] - multi)}", flush=True)
        retry = scan_product(cur, lb, multi)
        agg2 = rollup({lb: retry}, widest)
        tick2 = {t for (t, _), e in agg2.items()
                 if e["n_docs"] >= MIN_DOCS and e["mfg"] > 0}
        share2, n2 = coherence(tick2, ind_map)
        print(f"     -> {share2:.0%} top industry, n={n2}")
        # Adopt the narrower set unconditionally: it is more precise by
        # construction, and an EMPTY result is the honest answer when no
        # domestic maker of that product exists in the corpus. Keeping the
        # incoherent set because it is "bigger" is how 32 plywood and steel
        # names ended up tagged as display-panel manufacturers.
        scans[lb] = retry
        usable[lb] = multi
        if n2 == 0:
            print(f"     -> no domestic manufacturer evidence for {lb!r}; "
                  f"product yields no names (correct, not a failure)")

    for as_of in as_of_list:
        agg = rollup(scans, as_of)
        rows = []
        for (ticker, product), e in agg.items():
            if e["n_docs"] < MIN_DOCS:
                continue                     # repetition gate
            manufacturer = e["mfg"] > 0
            if not manufacturer and e["trade"] > 0:
                continue                     # trader-only mention, not a maker
            if not manufacturer:
                continue                     # no capacity evidence => not a maker
            rows.append((as_of, ticker, product, e["n_docs"], e["mfg"],
                         e["trade"], manufacturer))
        tickers = {r[1] for r in rows}
        print(f"\n  as-of {as_of}: {len(rows)} (ticker, product) capabilities "
              f"across {len(tickers)} distinct tickers")
        if dry_run:
            continue
        with conn.cursor() as w:
            w.execute("DELETE FROM mg_company_capabilities WHERE as_of_date=%s", (as_of,))
            psycopg2.extras.execute_batch(w, """
                INSERT INTO mg_company_capabilities
                  (as_of_date, ticker, product, n_docs, mfg_docs, trade_docs, manufacturer)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (as_of_date, ticker, product) DO NOTHING
            """, rows, page_size=500)
        conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Build company capability profiles")
    ap.add_argument("--as-of", help="single snapshot date YYYY-MM-DD")
    ap.add_argument("--snapshots", help="comma-separated snapshot dates")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hs-top-n", type=int, default=0,
                    help="also use the top-N HS import categories as products "
                         "(0 = curated products only)")
    ap.add_argument("--batch-size", type=int, default=12,
                    help="products per corpus pass")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel corpus passes (each uses its own DB connection)")
    args = ap.parse_args()

    dates = []
    if args.snapshots:
        dates = [date.fromisoformat(d.strip()) for d in args.snapshots.split(",")]
    elif args.as_of:
        dates = [date.fromisoformat(args.as_of)]
    else:
        ap.error("pass --as-of or --snapshots")

    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    ensure_table(conn)
    build(conn, cur, dates, args.dry_run, batch_size=args.batch_size,
          hs_top_n=args.hs_top_n, workers=args.workers)
    conn.close()


if __name__ == "__main__":
    main()
