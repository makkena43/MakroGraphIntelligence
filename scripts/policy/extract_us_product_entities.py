#!/usr/bin/env python3
"""Backfill US product-vocabulary entities from the manufacturing-grammar
extractor, so the country-generic constraint pipeline (product_vocabulary ->
company_product_roles -> bootstrap_constraint_coverage -> constraint candidates)
has something to work with for the US.

WHY THIS EXISTS
---------------
`company_product_roles.product_vocabulary()` seeds the US product universe ONLY
from `mg_entities` PRODUCT rows whose metadata source is
`generic_manufacturing_phrase_v*` and that appear across >=2 issuers. India also
falls back to its beneficiary mapper's `constrained_product` list; the US has no
such fallback. And the manufacturing-grammar product extractor
(EntityExtractor, source `generic_manufacturing_phrase_v1`) had never been run
to PERSIST US product entities — the only US PRODUCT rows present were generic
NER junk ("Report", "Bylaws", "Mergers") with a null source. Result: the US
constraint universe was a 4-item seed stub and the US investment list was empty.

This runs that extractor over US annual/quarterly filings (10-K/10-Q/20-F, which
carry business/product descriptions — the 8-Ks are events) and upserts the
resulting phrases as PRODUCT entities tagged with the expected source, plus the
document links product_vocabulary counts issuers over. Per-document extractor
noise is tolerated because the downstream vocabulary gate requires the SAME
phrase from >=2 independent issuers and company_product_roles then applies the
strict issuer-owned role test — one-off prose never survives to a constraint.

USAGE
-----
    python scripts/policy/extract_us_product_entities.py [--limit N] [--filing-types 10-K,10-Q]
"""
import argparse
import os
import re
import sys

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# Apply ONLY the manufacturing-grammar product patterns (not the full
# EntityExtractor, which runs dozens of passes and is ~10x slower) — same
# patterns and cleaner the extractor tags 'generic_manufacturing_phrase_v1'.
from src.makrograph.nlp.entity_extractor import (  # noqa: E402
    _PRODUCT_ACTION_RE, _PRODUCT_ASSET_RE, _PRODUCT_OWNED_ASSET_RE,
    _clean_product_phrase,
)
from src.makrograph.nlp.product_quality import is_product_label  # noqa: E402

SOURCE = "generic_manufacturing_phrase_v1"
_PATTERNS = (_PRODUCT_ACTION_RE, _PRODUCT_OWNED_ASSET_RE, _PRODUCT_ASSET_RE)
# Product/business descriptions sit early in a 10-K ("Item 1. Business"); a
# generous head window captures them while keeping the regex cost bounded.
_TEXT_CAP = 120000


def _extract_products(text: str) -> set[str]:
    out = set()
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            phrase = _clean_product_phrase(m.group("product"))
            if phrase:
                out.add(phrase.strip().strip(".,;:"))
    return out

# Reject obvious non-product phrases the manufacturing-grammar regex catches in
# verbose US legal prose. This is a coarse pre-filter only — the real gates are
# the >=2-issuer vocabulary requirement and the issuer-owned role test
# downstream. Judged on FORM (leading function word, length, digit-only) so it
# stays a rule, not a product blocklist.
_LEAD_STOP = {
    "and", "or", "by", "the", "such", "with", "for", "to", "is", "are", "be",
    "our", "its", "their", "a", "an", "of", "in", "on", "at", "as", "that",
    "which", "however", "unless", "expected", "including", "certain", "other",
    "location", "significant", "total", "various", "these", "this", "we",
    "beyond", "across", "through", "into", "from", "over", "under", "any",
    "all", "more", "most", "new", "additional", "further", "same", "both",
    "each", "several", "many", "some", "using", "used", "based", "related",
    "primarily", "generally", "typically", "currently", "approximately",
}
# Phrases that are ALL function/abstract words carry no product — drop them.
# The real gate is the >=2-independent-issuer requirement downstream; this only
# trims obvious prose so the vocabulary table isn't drowned.
_ABSTRACT = {
    "work", "parties", "party", "reserves", "practices", "commodity", "portion",
    "growth", "excellence", "management", "operations", "activities", "capital",
    "investment", "investments", "relationships", "requirements", "customers",
    "providers", "solutions", "space", "shortages", "damage", "believe",
    "cost", "costs", "revenue", "value", "returns", "margin", "margins",
    "expenditures", "planning", "system", "controls", "results", "position",
}


def _acceptable(phrase: str) -> bool:
    p = phrase.strip().strip(".,;:").lower()
    words = p.split()
    if not (1 <= len(words) <= 5):
        return False
    if words[0] in _LEAD_STOP:
        return False
    if not re.search(r"[a-z]", p):
        return False
    if any(ch.isdigit() for ch in p):
        return False
    if not any(w not in _LEAD_STOP and w not in _ABSTRACT for w in words):
        return False
    # Defer to the shared product-validity gate (abstract-head-noun, boilerplate,
    # fiscal/OCR/web rejects) so extraction, role discovery and the selector all
    # agree on what counts as a product. min_words=1 keeps single-word products
    # (the vocabulary stage requires >=2 issuers regardless).
    return is_product_label(phrase, min_words=1, max_words=5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max documents (0=all)")
    ap.add_argument("--country", default="US", help="country code (US or IN)")
    ap.add_argument("--filing-types", default="10-K,10-Q,20-F",
                    help="comma-separated filing types to scan, or ALL for every type "
                         "(India exchange filings have no US-style 10-K label)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    country = args.country.strip().upper()
    types = [] if args.filing_types.strip().upper() == "ALL" else \
        [t.strip() for t in args.filing_types.split(",") if t.strip()]

    conn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")
    # Server-side read cursor must live on its OWN connection: committing writes
    # on the same connection invalidates a named cursor mid-iteration.
    rconn = psycopg2.connect(dbname="makrograph", host="localhost", user="postgres")

    rcur = rconn.cursor(name=f"prod_scan_{country.lower()}", cursor_factory=psycopg2.extras.RealDictCursor)
    rcur.itersize = 200
    type_filter = ""
    params: list = [country]
    if types:
        type_filter = " AND (" + " OR ".join(["filing_type ILIKE %s"] * len(types)) + ")"
        params += [f"%{t}%" for t in types]
    limit = f" LIMIT {args.limit}" if args.limit else ""
    rcur.execute(f"""
        SELECT id, UPPER(TRIM(ticker)) AS ticker, filed_at::date AS filed, raw_text
        FROM mg_documents
        WHERE country=%s AND ticker IS NOT NULL AND BTRIM(ticker) <> ''
          AND raw_text IS NOT NULL{type_filter}
        ORDER BY filed_at{limit}
    """, params)

    ent_cache: dict[str, int] = {}     # canonical -> entity_id
    w = conn.cursor()
    seen_docs = 0
    ent_new = 0
    links = 0
    for row in rcur:
        seen_docs += 1
        text = row["raw_text"][:_TEXT_CAP]
        try:
            raw = _extract_products(text)
        except Exception:
            continue
        phrases = {p for p in raw if _acceptable(p)}
        if not phrases or args.dry_run:
            if args.dry_run and seen_docs <= 5:
                print(f"  doc {row['id']} ({row['ticker']}): {sorted(phrases)[:10]}")
            continue
        for canonical in phrases:
            eid = ent_cache.get(canonical.lower())
            if eid is None:
                w.execute("""
                    INSERT INTO mg_entities
                      (entity_text, entity_type, canonical_name, ticker,
                       mention_count, first_seen_at, last_seen_at, confidence, metadata)
                    VALUES (%s,'PRODUCT',%s,%s,1,%s,%s,0.74,
                            jsonb_build_object('source',%s))
                    ON CONFLICT (canonical_name, entity_type) DO UPDATE
                      SET last_seen_at = GREATEST(mg_entities.last_seen_at, EXCLUDED.last_seen_at),
                          first_seen_at = LEAST(mg_entities.first_seen_at, EXCLUDED.first_seen_at),
                          metadata = mg_entities.metadata || EXCLUDED.metadata
                    RETURNING id
                """, (canonical[:200], canonical[:200], row["ticker"],
                      row["filed"], row["filed"], SOURCE))
                eid = w.fetchone()[0]
                ent_cache[canonical.lower()] = eid
                ent_new += 1
            w.execute("""
                INSERT INTO mg_document_entities (document_id, entity_id, mention_count)
                VALUES (%s,%s,1)
                ON CONFLICT (document_id, entity_id) DO NOTHING
            """, (row["id"], eid))
            links += 1
        if seen_docs % 500 == 0:
            conn.commit()
            print(f"  scanned {seen_docs} docs, {len(ent_cache)} distinct products, {links} links", flush=True)
    conn.commit()
    print(f"DONE: {seen_docs} docs scanned, {len(ent_cache)} distinct US product phrases, {links} document links")
    rconn.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
