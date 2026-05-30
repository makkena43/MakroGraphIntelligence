"""Theme Canonicalization and Lifecycle Engine.

Prevents theme explosion by grouping semantically similar themes into
canonical parent + subtheme hierarchies.

Pipeline position:  detect_themes() → canonicalize() → rank()

Merge score = weighted combination of:
  embedding_similarity:  35%   semantic proximity of name + description
  shared_companies:      25%   companies reporting BOTH themes
  shared_entities:       20%   named entity overlap in theme slugs/names
  temporal_overlap:      10%   both themes active in the same date window
  supply_chain_overlap:  10%   supply-chain / downstream keyword proximity

Threshold: combined score > 0.82  →  merge candidate

Example:
  "AI datacenter electricity demand"   ─┐
  "AI power shortage"                   ├─► "AI Infrastructure Power Constraint"
  "Hyperscale grid constraints"         │
  "Electricity bottleneck"             ─┘
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..ontology.ontology_model import InvestmentTheme, ThemeConviction

logger = logging.getLogger(__name__)

# ── Merge threshold ──────────────────────────────────────────────────────────
# 0.45 = human-review mode: surface candidates liberally, human approves/dismisses.
#        False positives are fine (user clicks "Keep Separate").
#        Missed clusters (false negatives) are the worse mistake.
# 0.72+ = auto-merge mode: only use when embedding engine is fully loaded.
MERGE_THRESHOLD = 0.45

# ── Score weights (must sum to 1.0) ─────────────────────────────────────────
W_EMBED    = 0.35
W_COMPANY  = 0.25
W_ENTITY   = 0.20
W_TEMPORAL = 0.10
W_SUPPLY   = 0.10

# ── Canonical name suffix patterns to strip when building display name ───────
_STRIP_SUFFIXES = re.compile(
    r"\s*(?::"
    r"\s*(?:demand.surge|supply.bottleneck|capex.buildout|capex.increase"
    r"|supply.tension|demand.supply.tension|constraint|shortage"
    r"|demand.surge.*|tech.adoption|hiring.surge|market.entry)"
    r")?\s*$",
    re.IGNORECASE,
)

# ── Supply-chain / downstream indicator words ────────────────────────────────
_SUPPLY_CHAIN_TERMS = {
    "memory", "hbm", "dram", "nand", "power", "electricity", "grid",
    "cooling", "chip", "semiconductor", "wafer", "substrate", "pcb",
    "fiber", "copper", "rare earth", "lithium", "cobalt", "nickel",
    "transformer", "generator", "ups", "rack", "server", "networking",
    "bandwidth", "interconnect", "packaging", "tsmc", "asml",
    # Bottleneck-specific additions
    "advanced packaging", "cowos", "chiplet", "interposer", "osat",
    "ai accelerator", "inference chip", "hbm3", "hbm3e",
    "substation", "transmission", "distribution", "neodymium",
    "dysprosium", "terbium", "critical mineral", "liquid cooling",
    "immersion cooling", "cold plate", "gigafactory", "cell manufacturing",
}

# ── Domain clusters for heuristic canonical naming ───────────────────────────
# Ordered most-specific → broadest.  The cluster with the most keyword hits
# wins, so narrow bottleneck clusters beat wide domain buckets.
_DOMAIN_CLUSTERS: list[tuple[frozenset[str], str]] = [
    # ── Specific bottleneck clusters ─────────────────────────────────────────
    (
        frozenset({
            "transformer", "grid transformer", "substation",
            "power transformer", "distribution transformer",
            "step-up", "t&d", "high voltage transformer",
        }),
        "Grid Transformer Shortage",
    ),
    (
        frozenset({
            "rare earth", "rare earth element", "neodymium", "dysprosium",
            "terbium", "praseodymium", "critical mineral", "critical material",
            "rare earth magnet", "permanent magnet",
        }),
        "Rare Earth Supply Constraint",
    ),
    (
        frozenset({
            "advanced packaging", "cowos", "chip on wafer", "chip-on-wafer",
            "chiplet", "fan-out", "fan out", "2.5d", "3d stacking",
            "interposer", "osat", "substrate advanced",
        }),
        "Advanced Packaging Bottleneck",
    ),
    (
        frozenset({
            "liquid cooling", "immersion cooling", "cold plate", "cdu",
            "thermal management", "heat exchanger", "data center cooling",
            "direct liquid cooling", "dlc", "rear-door heat exchanger",
        }),
        "Data Center Cooling Bottleneck",
    ),
    (
        frozenset({
            "ai accelerator", "inference chip", "training chip",
            "h100", "h200", "b200", "gb200", "a100", "tpu", "npu",
            "dl accelerator", "ai chip allocation",
        }),
        "AI Accelerator Supply Constraint",
    ),
    (
        frozenset({
            "hbm", "high bandwidth memory", "hbm2", "hbm2e",
            "hbm3", "hbm3e", "bandwidth memory",
            "sk hynix hbm", "micron hbm", "samsung hbm",
        }),
        "HBM Memory Supply Constraint",
    ),
    (
        frozenset({
            "grid", "power grid", "electricity grid", "transmission line",
            "distribution line", "interconnection queue", "capacity addition",
            "generation capacity", "grid capacity", "load growth",
        }),
        "Power Grid Capacity Constraint",
    ),
    (
        frozenset({
            "nuclear", "smr", "small modular reactor", "nuclear power",
            "next-gen nuclear", "advanced nuclear",
        }),
        "Nuclear Power Demand Surge",
    ),
    (
        frozenset({
            "fiber", "optical fiber", "dark fiber", "fiber optic",
            "fiber cable", "fiber backbone", "network fiber",
        }),
        "Fiber Optic Network Build-out",
    ),
    (
        frozenset({
            "cobalt", "nickel", "lithium", "cathode material",
            "battery material", "anode material", "battery chemistry",
            "cell chemistry", "gigafactory material",
        }),
        "Battery Materials Supply Constraint",
    ),
    # ── Broader domain clusters (fallback) ────────────────────────────────────
    (
        frozenset({
            "power", "electricity", "energy", "watt", "mw", "gw",
            "cooling", "thermal", "datacenter", "data center",
            "renewable", "solar", "wind", "natural gas",
        }),
        "AI Infrastructure Power Constraint",
    ),
    (
        frozenset({
            "memory", "hbm", "dram", "nand", "bandwidth",
            "bandwidth memory", "advanced packaging", "cowos",
        }),
        "HBM / Advanced Memory Supply Constraint",
    ),
    (
        frozenset({
            "semiconductor", "chip", "wafer", "fab", "foundry",
            "tsmc", "logic chip", "gpu chip", "asic", "node",
        }),
        "Semiconductor Supply Constraint",
    ),
    (
        frozenset({
            "ev", "electric vehicle", "battery", "lithium", "cathode",
            "anode", "cell manufacturing", "bev", "gigafactory",
        }),
        "EV Battery Supply Chain",
    ),
    (
        frozenset({
            "cloud", "hyperscaler", "hyperscale", "aws", "azure", "gcp",
            "cloud computing", "iaas", "paas", "colocation", "colo",
        }),
        "Hyperscaler Infrastructure Build-out",
    ),
    (
        frozenset({
            "ai", "artificial intelligence", "machine learning", "llm",
            "inference", "training", "gpu", "accelerator",
            "large language model", "foundation model", "generative ai",
        }),
        "AI Compute Demand",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ThemeCluster:
    """A group of similar themes that should be merged under one canonical parent."""
    canonical_slug: str          # slug of the elected canonical parent
    canonical_name: str          # clean human-readable name
    aliases: list[str]           # alternative names (non-canonical theme names)
    member_slugs: list[str]      # all member slugs (including canonical)
    aggregate_strength: float    # max strength across members
    aggregate_companies: int     # max company count across members


@dataclass
class ThemeSimilarityScore:
    """Breakdown of similarity between two themes."""
    embedding_sim: float = 0.0
    company_sim: float   = 0.0
    entity_sim: float    = 0.0
    temporal_sim: float  = 0.0
    supply_sim: float    = 0.0

    @property
    def combined(self) -> float:
        return (
            W_EMBED    * self.embedding_sim
            + W_COMPANY * self.company_sim
            + W_ENTITY  * self.entity_sim
            + W_TEMPORAL * self.temporal_sim
            + W_SUPPLY  * self.supply_sim
        )


# ─────────────────────────────────────────────────────────────────────────────
# Core engine
# ─────────────────────────────────────────────────────────────────────────────

class ThemeCanonicalizer:
    """Groups similar investment themes into canonical parent + subtheme hierarchies.

    Usage::

        canonicalizer = ThemeCanonicalizer(config, embedding_engine=emb, llm_reasoner=llm)
        canonical_themes = canonicalizer.canonicalize(all_themes)
        # canonical_themes have parent_theme_slug / is_canonical / aliases set.
        # Persist canonical_themes — the upsert in pg_store handles the hierarchy.

    Args:
        config:           Pipeline config dict (uses llm.llm_enabled flag).
        embedding_engine: Optional EmbeddingEngine instance for semantic similarity.
        llm_reasoner:     Optional LLMReasoner for LLM-generated canonical names.
        pg_store:         Optional PGStore to fetch per-theme company lists.
    """

    def __init__(
        self,
        config: dict,
        embedding_engine=None,
        llm_reasoner=None,
        pg_store=None,
    ):
        self.config          = config
        self._emb            = embedding_engine
        self._llm            = llm_reasoner
        self._pg             = pg_store
        self._merge_threshold = float(
            config.get("theme_merge_threshold", MERGE_THRESHOLD)
        )
        self._use_llm = bool(
            config.get("llm", {}).get("llm_enabled", False)
        ) and llm_reasoner is not None

        # Cache of cluster_id → approved_name loaded once per run
        self._approved_names: dict[str, str] | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def canonicalize(
        self,
        themes: list[InvestmentTheme],
    ) -> list[InvestmentTheme]:
        """Cluster similar themes and annotate with parent/canonical metadata.

        Returns the same list of themes — every theme now has:
          - ``is_canonical``        True if this is the elected parent
          - ``canonical_name``      Clean readable name for the cluster
          - ``aliases``             All non-canonical names in its cluster
          - ``parent_theme_slug``   Points to canonical slug (None if is_canonical)

        The original theme objects are mutated in place; the same list is
        returned (with subthemes included) so callers can choose to persist
        all of them or only the canonicals.
        """
        if len(themes) < 2:
            self._mark_all_canonical(themes)
            return themes

        logger.info(f"Canonicalizing {len(themes)} themes (threshold={self._merge_threshold})")

        # Build embeddings for all themes (one batch call)
        embeddings = self._build_embeddings(themes)

        # Fetch company-per-theme data if pg_store is available
        theme_companies = self._fetch_theme_companies([t.theme_slug for t in themes])

        # Build pairwise similarity + cluster
        clusters = self._cluster(themes, embeddings, theme_companies)

        # Annotate themes
        annotated = self._annotate(themes, clusters)

        logger.info(
            f"Canonicalization: {len(themes)} themes → "
            f"{sum(1 for t in annotated if getattr(t, 'is_canonical', True))} canonical parents, "
            f"{sum(1 for t in annotated if not getattr(t, 'is_canonical', True))} subthemes"
        )
        return annotated

    # ── Embedding ─────────────────────────────────────────────────────────────

    def _build_embeddings(
        self, themes: list[InvestmentTheme]
    ) -> dict[str, list[float] | None]:
        """Return {slug → embedding vector} for all themes."""
        if self._emb is None or not self._emb.is_available:
            return {}

        texts = [
            f"{t.theme_name}. {t.description[:200]}"
            for t in themes
        ]
        try:
            vecs = self._emb.embed_batch(texts)
            return {t.theme_slug: v for t, v in zip(themes, vecs)}
        except Exception as e:
            logger.warning(f"Batch theme embedding failed: {e}")
            return {}

    # ── Company data ──────────────────────────────────────────────────────────

    def _fetch_theme_companies(
        self, slugs: list[str]
    ) -> dict[str, set[str]]:
        """Fetch company names per theme from pg_store (best-effort)."""
        if self._pg is None:
            return {}
        try:
            return self._pg.get_companies_per_theme(slugs)
        except Exception:
            return {}

    # ── Pairwise scoring ──────────────────────────────────────────────────────

    def _score_pair(
        self,
        a: InvestmentTheme,
        b: InvestmentTheme,
        embeddings: dict[str, list[float] | None],
        theme_companies: dict[str, set[str]],
    ) -> ThemeSimilarityScore:
        score = ThemeSimilarityScore()

        # 1. Embedding similarity (cosine)
        ea = embeddings.get(a.theme_slug)
        eb = embeddings.get(b.theme_slug)
        if ea and eb and self._emb:
            try:
                score.embedding_sim = max(0.0, self._emb.cosine_similarity(ea, eb))
            except Exception:
                score.embedding_sim = 0.0
        else:
            # Fallback: Jaccard on word tokens of name + description
            score.embedding_sim = self._token_jaccard(
                f"{a.theme_name} {a.description[:100]}",
                f"{b.theme_name} {b.description[:100]}",
            )

        # 2. Shared company similarity
        ca = theme_companies.get(a.theme_slug, set())
        cb = theme_companies.get(b.theme_slug, set())
        if ca and cb:
            score.company_sim = len(ca & cb) / max(len(ca | cb), 1)
        else:
            # Estimate from company_count: penalise large disparity
            max_cos = max(a.company_count, b.company_count, 1)
            min_cos = min(a.company_count, b.company_count)
            score.company_sim = min_cos / max_cos * 0.5  # max 0.5 without real data

        # 3. Entity overlap — shared meaningful tokens in theme slugs / names
        score.entity_sim = self._entity_overlap(a, b)

        # 4. Temporal overlap — both active in overlapping date windows
        score.temporal_sim = self._temporal_overlap(a, b)

        # 5. Supply-chain / downstream relationship
        score.supply_sim = self._supply_chain_sim(a, b)

        return score

    # ── Stable cluster ID ─────────────────────────────────────────────────────

    @staticmethod
    def _cluster_id(member_slugs: list[str]) -> str:
        """Stable SHA-1 hash of sorted member slugs — used as the review PK."""
        key = "|".join(sorted(member_slugs))
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    # ── Human-review prompt ──────────────────────────────────────────────────

    @staticmethod
    def _build_llm_prompt(member_themes: list[InvestmentTheme]) -> str:
        """Build the prompt text that would be sent to an LLM.

        The human reads this in the UI to understand the cluster context and
        decide on the right canonical name.  When LLM integration is added,
        this same text is sent verbatim.
        """
        names_bullet = "\n".join(f"  - {t.theme_name}" for t in member_themes)
        strongest = max(member_themes, key=lambda t: t.strength_score)
        desc = strongest.description[:400] if strongest.description else "(no description)"
        companies_total = max(t.company_count for t in member_themes)
        signals = set()
        for t in member_themes:
            signals.update(t.signal_types or [])

        return (
            "You are a senior investment analyst. The following auto-detected theme names\n"
            "all describe the same macro investment opportunity detected from SEC filings\n"
            "and earnings calls.\n\n"
            f"Theme names:\n{names_bullet}\n\n"
            f"Strongest theme description:\n{desc}\n\n"
            f"Evidence: {companies_total} companies, signals: {', '.join(sorted(signals))}\n\n"
            "Generate ONE canonical name for this investment theme cluster. Rules:\n"
            "- Max 7 words\n"
            "- Investor-grade: clear, specific, actionable\n"
            "- Focus on the structural driver + the asset class impacted\n"
            "- Good examples: 'AI Infrastructure Power Constraint', "
            "'HBM Supply Constraint', 'EV Battery Materials Shortage'\n"
            "- Bad examples: 'Technology', 'Supply Chain', 'AI Growth'\n\n"
            "Respond with ONLY the canonical name — no explanation."
        )

    # ── Approved-name cache ──────────────────────────────────────────────────

    def _get_approved_names(self) -> dict[str, str]:
        """Load approved canonical names from pg_store (cached for the run)."""
        if self._approved_names is not None:
            return self._approved_names
        if self._pg is None:
            self._approved_names = {}
            return self._approved_names
        try:
            self._approved_names = self._pg.get_approved_canonical_names()
        except Exception:
            self._approved_names = {}
        return self._approved_names

    # ── Clustering ────────────────────────────────────────────────────────────

    def _cluster(
        self,
        themes: list[InvestmentTheme],
        embeddings: dict[str, list[float] | None],
        theme_companies: dict[str, set[str]],
    ) -> list[ThemeCluster]:
        """Single-linkage clustering using the weighted merge score."""
        n = len(themes)

        # Union-Find for clustering
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                # Elect the theme with higher strength as root
                if themes[rx].strength_score >= themes[ry].strength_score:
                    parent[ry] = rx
                else:
                    parent[rx] = ry

        # Compute pairwise scores — O(n²) but n is small (<50 themes)
        for i in range(n):
            for j in range(i + 1, n):
                sc = self._score_pair(
                    themes[i], themes[j], embeddings, theme_companies
                )
                if sc.combined >= self._merge_threshold:
                    logger.debug(
                        f"Merge candidate: '{themes[i].theme_name}' ↔ "
                        f"'{themes[j].theme_name}'  score={sc.combined:.3f} "
                        f"(emb={sc.embedding_sim:.2f} co={sc.company_sim:.2f} "
                        f"ent={sc.entity_sim:.2f} tmp={sc.temporal_sim:.2f} "
                        f"sc={sc.supply_sim:.2f})"
                    )
                    union(i, j)

        # Collect clusters
        cluster_map: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            cluster_map[find(i)].append(i)

        approved_names = self._get_approved_names()
        clusters: list[ThemeCluster] = []
        pending_reviews: list[dict] = []

        for root_idx, members in cluster_map.items():
            member_themes = [themes[i] for i in members]

            # Elect canonical: highest strength_score
            canonical = max(member_themes, key=lambda t: t.strength_score)
            non_canonical = [t for t in member_themes if t.theme_slug != canonical.theme_slug]

            all_names = [t.theme_name for t in member_themes]
            cluster_id = self._cluster_id([t.theme_slug for t in member_themes])

            # ── Check if human has already approved a name for this cluster ──
            if cluster_id in approved_names:
                canonical_name = approved_names[cluster_id]
                logger.debug(f"Using human-approved name '{canonical_name}' for cluster {cluster_id}")
            elif len(member_themes) >= 2:
                # Multi-member cluster — generate heuristic name AND queue for review
                canonical_name = self._generate_canonical_name(canonical, all_names)
                llm_prompt = self._build_llm_prompt(member_themes)
                pending_reviews.append({
                    "cluster_id":           cluster_id,
                    "member_slugs":         [t.theme_slug for t in member_themes],
                    "member_names":         all_names,
                    "member_descriptions":  [
                        (t.description or "")[:300] for t in member_themes
                    ],
                    "suggested_name":       canonical_name,
                    "llm_prompt_text":      llm_prompt,
                })
            else:
                # Single-member — just clean up the name, no review needed
                canonical_name = self._generate_canonical_name(canonical, all_names)

            clusters.append(ThemeCluster(
                canonical_slug=canonical.theme_slug,
                canonical_name=canonical_name,
                aliases=[t.theme_name for t in non_canonical],
                member_slugs=[t.theme_slug for t in member_themes],
                aggregate_strength=max(t.strength_score for t in member_themes),
                aggregate_companies=max(t.company_count for t in member_themes),
            ))

        # ── Persist pending reviews to DB (non-blocking best-effort) ─────────
        if pending_reviews and self._pg:
            self._save_pending_reviews(pending_reviews)

        return clusters

    def _save_pending_reviews(self, reviews: list[dict]) -> None:
        """Persist pending canonical reviews to mg_theme_canonical_reviews."""
        saved = 0
        for review in reviews:
            try:
                self._pg.upsert_canonical_review(review)
                saved += 1
            except Exception as e:
                logger.debug(f"Failed to save canonical review {review.get('cluster_id')}: {e}")
        if saved:
            logger.info(
                f"Canonical review: {saved} cluster(s) queued for human review "
                f"(see 🔖 Canonical Review tab in the UI)"
            )

    # ── Annotation ────────────────────────────────────────────────────────────

    def _annotate(
        self,
        themes: list[InvestmentTheme],
        clusters: list[ThemeCluster],
    ) -> list[InvestmentTheme]:
        """Attach parent_theme_slug / is_canonical / canonical_name / aliases to each theme."""
        slug_to_cluster: dict[str, ThemeCluster] = {}
        for cl in clusters:
            for slug in cl.member_slugs:
                slug_to_cluster[slug] = cl

        for theme in themes:
            cl = slug_to_cluster.get(theme.theme_slug)
            if cl is None:
                theme.is_canonical    = True
                theme.canonical_name  = theme.theme_name
                theme.aliases         = []
                theme.parent_theme_slug = None
                continue

            if theme.theme_slug == cl.canonical_slug:
                # This theme IS the canonical parent
                theme.is_canonical      = True
                theme.canonical_name    = cl.canonical_name
                theme.aliases           = cl.aliases
                theme.parent_theme_slug = None
                # Boost strength to aggregate if members contributed more signal
                if cl.aggregate_strength > theme.strength_score:
                    theme.strength_score = min(cl.aggregate_strength, 100.0)
                if cl.aggregate_companies > theme.company_count:
                    theme.company_count = cl.aggregate_companies
            else:
                # Subtheme
                theme.is_canonical      = False
                theme.canonical_name    = cl.canonical_name
                theme.aliases           = []
                theme.parent_theme_slug = cl.canonical_slug

        return themes

    def _mark_all_canonical(self, themes: list[InvestmentTheme]) -> None:
        """Mark every theme as its own canonical (no clustering needed)."""
        for t in themes:
            if not hasattr(t, "is_canonical") or t.is_canonical is None:
                t.is_canonical      = True
                t.canonical_name    = t.theme_name
                t.aliases           = []
                t.parent_theme_slug = None

    # ── Canonical name generation ─────────────────────────────────────────────

    def _generate_canonical_name(
        self,
        canonical_theme: InvestmentTheme,
        all_names: list[str],
    ) -> str:
        """Generate a clean canonical name for a cluster.

        Priority:
          1. LLM call (if enabled and cluster has 2+ members worth naming)
          2. Domain cluster matching (keyword heuristics)
          3. Strongest theme name with signal-suffix stripped
        """
        # LLM path
        if self._use_llm and len(all_names) >= 2 and self._llm:
            try:
                return self._llm.generate_canonical_name(
                    canonical_theme.theme_name,
                    all_names,
                    canonical_theme.description,
                )
            except Exception as e:
                logger.debug(f"LLM canonical naming failed: {e}")

        # Domain cluster heuristic
        combined_text = " ".join(all_names).lower()
        best_domain_match: str | None = None
        best_match_count = 0
        for keywords, domain_name in _DOMAIN_CLUSTERS:
            hits = sum(1 for kw in keywords if kw in combined_text)
            if hits > best_match_count:
                best_match_count = hits
                best_domain_match = domain_name
        if best_match_count >= 2 and best_domain_match:
            return best_domain_match

        # Fallback: strip signal suffix from the strongest theme name
        return _STRIP_SUFFIXES.sub("", canonical_theme.theme_name).strip()

    # ── Similarity helpers ────────────────────────────────────────────────────

    @staticmethod
    def _token_jaccard(text_a: str, text_b: str) -> float:
        """Word-level Jaccard similarity (fallback when embeddings unavailable)."""
        stop = {
            "a", "an", "the", "and", "or", "of", "in", "for", "to", "is",
            "are", "on", "at", "by", "from", "with", "as", "via", "vs",
            "demand", "supply", "constraint", "theme",   # too generic to signal similarity
        }
        tokens_a = {w for w in re.split(r"\W+", text_a.lower()) if len(w) >= 3 and w not in stop}
        tokens_b = {w for w in re.split(r"\W+", text_b.lower()) if len(w) >= 3 and w not in stop}
        if not tokens_a or not tokens_b:
            return 0.0
        return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)

    @staticmethod
    def _entity_overlap(a: InvestmentTheme, b: InvestmentTheme) -> float:
        """Named entity overlap using tokens from the theme slugs and metadata."""
        stop_slug_tokens = {
            "auto", "demand", "supply", "tension", "bottleneck", "surge",
            "capex", "buildout", "increase", "signal", "via", "downstream",
        }

        def slug_tokens(t: InvestmentTheme) -> set[str]:
            raw = re.split(r"[-_\s]+", t.theme_slug.lower())
            tokens = {tok for tok in raw if len(tok) >= 3 and tok not in stop_slug_tokens}
            # Also add entity-like tokens from metadata
            driven_by = (t.metadata or {}).get("driven_by", "")
            if driven_by:
                for w in driven_by.lower().split():
                    if len(w) >= 3:
                        tokens.add(w)
            return tokens

        ta = slug_tokens(a)
        tb = slug_tokens(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / max(len(ta | tb), 1)

    @staticmethod
    def _temporal_overlap(a: InvestmentTheme, b: InvestmentTheme) -> float:
        """Proximity of first_detected dates (1.0 if same quarter, 0.0 if >365 days apart)."""
        try:
            fd_a = a.first_detected if isinstance(a.first_detected, date) else date.today()
            fd_b = b.first_detected if isinstance(b.first_detected, date) else date.today()
            gap = abs((fd_a - fd_b).days)
            if gap <= 90:
                return 1.0
            if gap >= 365:
                return 0.0
            return 1.0 - (gap - 90) / 275.0   # linear decay 90→365 days
        except Exception:
            return 0.5   # unknown → neutral

    @staticmethod
    def _supply_chain_sim(a: InvestmentTheme, b: InvestmentTheme) -> float:
        """Supply-chain proximity: 1.0 if both themes share supply-chain keywords."""
        def sc_tokens(t: InvestmentTheme) -> set[str]:
            text = f"{t.theme_name} {t.description}".lower()
            return {kw for kw in _SUPPLY_CHAIN_TERMS if kw in text}

        ta = sc_tokens(a)
        tb = sc_tokens(b)
        if not ta and not tb:
            # Neither theme is supply-chain related — still can be similar
            return 0.5
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / max(len(ta | tb), 1)
