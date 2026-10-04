"""Per-year narrative-emergence scoring for the constraint research queue.

The greatest-constraint ranking historically drew from a fixed ~12-product pool
and ranked by LEVEL (scarcity/evidence), so Solar (most/best-evidenced makers)
won every year and the list barely moved. This measures, point-in-time, how much
a constraint is INFLECTING in the trailing year vs the prior year — from the
materialised per-constraint monthly filing-signal table
(mg_constraint_signal_activity, built by
scripts/policy/build_constraint_signal_activity.py). Blended with severity
downstream (severity base × emergence multiplier), per the "emergence-weighted"
design, so the ranking finally becomes year-specific.

Point-in-time: only periods <= the anchor month are read. No look-ahead.
"""

from __future__ import annotations

import re
from datetime import date

from extract_report_data import q


# Mechanism families — used only to DIVERSIFY the emerging-constraint detector so
# the year's list is not three solar rows. A constraint's family, not a company.
_FAMILY = {
    "Solar Cell": "Solar", "Solar Module": "Solar", "Solar Wafer": "Solar",
    "Battery Cell (Li-ion)": "Battery/EV", "Cathode/Anode Materials": "Battery/EV",
    "Lithium": "Battery/EV", "Power Transformer": "Power/T&D",
    "HVDC / T&D Equipment": "Power/T&D", "Data Centre Power": "Power/T&D",
    "PCB / Printed Circuit Board": "Electronics", "EMS / Contract Manufacturing": "Electronics",
    "Semiconductor IC": "Electronics", "Display Panels": "Electronics",
    "Passive Components": "Electronics", "Optical Fibre": "Telecom",
    "Defense electronics": "Defense", "Rolling Stock / Railway": "Railway",
    "Pharma APIs": "Pharma/Chem", "Specialty Chemicals": "Pharma/Chem",
    "Green Hydrogen": "Hydrogen", "Cement": "Materials", "Wind Turbine": "Wind",
    "Textile / Technical Textile": "Textile",
}


def constraint_family(label: str) -> str:
    return _FAMILY.get(label, "Other")


def detect_emerging_constraints(cur, as_of: date, country: str = "IN", *, top_n: int = 10,
                                per_family_cap: int = 2, min_signals: int = 30,
                                window_months: int = 12) -> list[dict]:
    """Return the year's most-inflecting *research signals*.

    Filing acceleration measures attention, not a binding physical shortage.
    Each row is explicitly diagnostic and is augmented with the independently
    materialised physical state when an exact candidate exists.
    """
    activity = fetch_constraint_activity(cur, country)
    physical_by_product: dict[str, dict] = {}
    cur.execute("SELECT to_regclass('public.mg_constraint_candidates') AS n")
    if cur.fetchone()["n"]:
        # Select only columns that exist (the candidate table's schema has
        # drifted across versions); the physical enrichment is optional context,
        # so never let a missing column break emergence detection itself.
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='mg_constraint_candidates'")
        _have = {r["column_name"] for r in cur.fetchall()}
        _want = [c for c in ("normalized_product", "product_label", "physical_quality",
                             "physical_state", "trajectory", "evidence_completeness",
                             "mechanism", "research_state", "evidence_legs") if c in _have]
        if "normalized_product" in _have and "product_label" in _have:
            try:
                rows = q(cur, f"""
                    SELECT DISTINCT ON (normalized_product) {', '.join(_want)}
                    FROM mg_constraint_candidates
                    WHERE country=%s AND as_of_date <= %s
                    ORDER BY normalized_product, as_of_date DESC, id DESC
                """, (country, as_of))
                physical_by_product = {_norm(row["product_label"]): row for row in rows}
            except Exception:
                physical_by_product = {}

    def _anchor_back(yrs):
        return date(as_of.year - yrs, as_of.month, min(as_of.day, 28))

    scored = []
    for label, rows in activity.items():
        m = emergence_for(rows, as_of, window_months)
        # Admit a constraint if EITHER it clears the level gate (enough total
        # chatter + accelerating) OR the early-inflection detector fires — a
        # sharp rise in constraint-FORMING (leading) signals from a low base,
        # which precedes the level by 1-5 quarters and is the earliness edge.
        level_ok = m["recent_signals"] >= min_signals and m["emergence_score"] > 0
        early_ok = m.get("early_inflection") and not m.get("level_detected")
        if level_ok or early_ok:
            # Trajectory over the prior TWO years (single-year emergence oscillates).
            # SUSTAINED = elevated & accelerating in both prior years (the
            # persistent structural backdrop — real, but not "news"). NEW/
            # NEW_INFLECTION = the genuine year-specific signal, low/flat before
            # and surging now. Distinguishes them honestly instead of forcing a
            # different list each year.
            p1 = emergence_for(rows, _anchor_back(1), window_months)
            p2 = emergence_for(rows, _anchor_back(2), window_months)
            hot1 = p1["emergence_score"] >= 0.4 and p1["recent_signals"] >= min_signals
            hot2 = p2["emergence_score"] >= 0.4 and p2["recent_signals"] >= min_signals
            if early_ok and not level_ok:
                # Caught on leading signals before the constraint is broadly
                # discussed — the earliest, highest-lead-time tag.
                trajectory = "EARLY_INFLECTION"
            elif m["new_this_year"] or (not hot1 and not hot2):
                trajectory = "NEW" if m["new_this_year"] else "NEW_INFLECTION"
            elif hot1 and hot2:
                trajectory = "SUSTAINED"
            else:
                trajectory = "RE_ACCELERATING"    # hot before, cooled, surging again
            prior = p1
            scored.append({
                "constraint": label, "family": constraint_family(label),
                "trajectory": trajectory,
                "signal_class": "NARRATIVE_ACCELERATION",
                "position_authority": False,
                "prior_year_emergence": prior["emergence_score"],
                "physical_constraint_state": physical_by_product.get(_norm(label)),
                **m,
            })
    # Emergence magnitude leads (a big surge is a big surge), with a modest lift
    # for genuinely NEW inflections so the year-specific signal is not buried by
    # the persistent backdrop. Trajectory is also surfaced as a tag so the reader
    # can tell "new this year" from "structural, still accelerating".
    _traj_boost = {"EARLY_INFLECTION": 0.7, "NEW": 0.6, "NEW_INFLECTION": 0.4,
                   "RE_ACCELERATING": 0.2, "SUSTAINED": 0.0}
    scored.sort(key=lambda x: (-(x["emergence_score"] + _traj_boost.get(x["trajectory"], 0)),
                               -x["emergence_ratio"], -x["recent_signals"]))
    # Diversify: greedily accept in emergence order but cap rows per family, so
    # a hot sector shows its strongest 1-2 constraints, not five.
    out, fam_count = [], {}
    for item in scored:
        f = item["family"]
        if fam_count.get(f, 0) >= per_family_cap:
            continue
        fam_count[f] = fam_count.get(f, 0) + 1
        out.append(item)
        if len(out) >= top_n:
            break
    return out


_EVIDENCED_STATES = ("OPERATING_PRODUCER_EVIDENCED", "EARNINGS_CAPTURE_EVIDENCED")


def attach_emerging_constraint_makers(cur, as_of, emerging: list[dict], country: str = "IN") -> None:
    """Attach the corresponding listed stocks to each emerging constraint in place.

    Answers "where are the stocks for this constraint" for EVERY emerging
    constraint (not just the few in the maker universe), from the role ledger,
    point-in-time at ``as_of``: `stocks_verified` = operating/earnings makers,
    `stocks_pipeline` = pre-operational / building-capacity names. A constraint
    with neither (Green Hydrogen, Data Centre Power) is honestly stock-less."""
    cur.execute("SELECT to_regclass('public.mg_company_product_roles') AS n")
    if not cur.fetchone()["n"]:
        return
    cur.execute("""
        SELECT DISTINCT ticker, normalized_product, adjudication_state
        FROM mg_company_product_roles
        WHERE country=%s AND as_of_date=%s AND ticker IS NOT NULL
    """, (country, as_of))
    verified_by_np: dict[str, set] = {}
    pipeline_by_np: dict[str, set] = {}
    quarantined_by_np: dict[str, set] = {}
    for r in cur.fetchall():
        np = _norm(r["normalized_product"])
        tk = (r["ticker"] or "").upper()
        if not np or not tk:
            continue
        if r["adjudication_state"] in _EVIDENCED_STATES:
            verified_by_np.setdefault(np, set()).add(tk)
        elif r["adjudication_state"] == "PIPELINE_EVIDENCED":
            pipeline_by_np.setdefault(np, set()).add(tk)
        elif "QUARANTINED" in (r["adjudication_state"] or ""):
            quarantined_by_np.setdefault(np, set()).add(tk)

    def _match(label, table):
        n = _norm(label)
        if not n:
            return []
        if n in table:
            return sorted(table[n])
        first = n.split()[0]
        hits = set()
        for np, tks in table.items():
            if np.split()[:1] == [first] and (np in n or n in np):
                hits |= tks
        return sorted(hits)

    for c in emerging:
        label = c.get("constraint")
        c["stocks_verified"] = _match(label, verified_by_np)
        c["stocks_pipeline"] = [t for t in _match(label, pipeline_by_np)
                                if t not in set(c["stocks_verified"])]
        c["stocks_quarantined"] = [
            t for t in _match(label, quarantined_by_np)
            if t not in set(c["stocks_verified"]) | set(c["stocks_pipeline"])
        ]


def attach_constraint_exposure_companies(cur, as_of, emerging: list[dict], country: str = "IN",
                                         top_n: int = 8, window_months: int = 24) -> None:
    """Attach `stocks_exposure` — listed issuers whose own filings carry THIS
    constraint's shortage/capex/order signals, from mg_constraint_company_signals
    (built in one corpus pass). Gives every constraint a company list even when
    the role ledger has no verified maker (Green Hydrogen, Data Centre Power).
    This is EXPOSURE/association, not a proven maker role — flagged as such.
    Point-in-time (period <= anchor); excludes names already shown as verified or
    pipeline makers."""
    cur.execute("SELECT to_regclass('public.mg_constraint_company_signals') AS n")
    if not cur.fetchone()["n"]:
        return
    cutoff = _period(as_of)
    lo = _shift_months(as_of, window_months)
    # Services/financial/media issuers cannot MAKE a physical product; a capex/
    # order signal co-occurring with the product term there is noise (the
    # NETWORK18/TV18/SIS-under-Cathode class). Two guards: (1) exclude non-maker
    # sectors, checking BOTH industry_nse and industry_bse (nse is often 'None'
    # while bse has the real sector); (2) require a non-empty industrial sector —
    # an issuer with no sector (not in security_master, e.g. TV18BRDCST) cannot
    # be verified as a maker, so it is dropped from this research tier.
    _EXCLUDE_SECTORS = ("financial", "media", "entertainment", "publication",
                        "broadcast", "bank", "insurance", "telecommunication",
                        "information technology", "software", "healthcare",
                        "consumer", "retail", "hospitality", "realty", "trading",
                        "services", "diversified financials")
    _INDUSTRIAL_HINT = ("capital goods", "electrical", "power", "chemical", "metal",
                        "steel", "auto", "construction", "cement", "oil", "gas",
                        "energy", "engineering", "industrial", "manufactur",
                        "renewabl", "textile", "pharma", "fertiliser", "fertilizer",
                        "electronic", "infrastructure", "utilities", "mining",
                        "aerospace", "defence", "defense", "railway", "transport")
    for c in emerging:
        label = c.get("constraint")
        already = {t.upper() for t in (c.get("stocks_verified") or []) + (c.get("stocks_pipeline") or [])}
        cur.execute("""
            SELECT s.ticker, SUM(s.signal_count) AS n,
                   LOWER(CONCAT_WS(' ', NULLIF(sm.industry_nse,'None'),
                                        NULLIF(sm.industry_bse,'None'))) AS sector
            FROM mg_constraint_company_signals s
            LEFT JOIN security_master sm ON sm.nse_symbol = s.ticker
            WHERE s.country=%s AND s.constraint_label=%s AND s.period > %s AND s.period <= %s
              AND s.ticker <> ''
            GROUP BY s.ticker, sm.industry_nse, sm.industry_bse
            HAVING SUM(s.signal_count) >= 3
            ORDER BY n DESC LIMIT %s
        """, (country, label, lo, cutoff, top_n * 6))
        exposure = []
        for r in cur.fetchall():
            tk = (r["ticker"] or "").upper()
            sector = (r["sector"] or "").strip()
            if not tk or tk in already or len(tk) > 12:
                continue
            # Must be an identifiable industrial/manufacturing issuer, and not a
            # services/media/financial one.
            if not sector or any(x in sector for x in _EXCLUDE_SECTORS):
                continue
            if not any(h in sector for h in _INDUSTRIAL_HINT):
                continue
            exposure.append(tk)
            if len(exposure) >= top_n:
                break
        c["stocks_exposure"] = exposure


def fetch_verified_maker_universe(cur, as_of, country: str = "IN") -> list[dict]:
    """Diagnostic operating-maker inventory, grouped by exact product.

    This establishes company role only. It is never an investable universe and
    has no position authority without a selected constraint and earnings/risk
    gates in ``final_decision``.
    """
    _EV = ("OPERATING_PRODUCER_EVIDENCED", "EARNINGS_CAPTURE_EVIDENCED")
    cur.execute("""
        SELECT normalized_product, array_agg(DISTINCT ticker) AS makers
        FROM mg_company_product_roles
        WHERE country=%s AND as_of_date=%s AND adjudication_state = ANY(%s) AND ticker <> ''
        GROUP BY normalized_product
    """, (country, as_of, list(_EV)))
    products = {r["normalized_product"]: sorted({t.upper() for t in r["makers"] if t})
                for r in cur.fetchall()}
    # Collapse near-duplicate product labels that share the SAME maker set (e.g.
    # "optical fiber" / "optical fiber cable" / "optical fibre cable" all = HFCL,
    # "active pharmaceutical" / "active pharmaceutical ingredient") into the
    # cleanest (shortest) label — the India-text garble produces these variants.
    by_makers: dict[tuple, str] = {}
    for np, makers in sorted(products.items(), key=lambda kv: len(kv[0])):
        key = tuple(makers)
        if key in by_makers:
            continue          # a shorter label with the same makers already kept
        by_makers[key] = np
    products = {np: list(k) for k, np in by_makers.items()}
    emergence_all = fetch_all_constraint_emergence(cur, as_of, country) if isinstance(as_of, date) else {}
    out = []
    for np, makers in products.items():
        if not makers:
            continue
        em = match_emergence(np, emergence_all) if emergence_all else {}
        out.append({
            "product": np, "family": constraint_family(np.title()),
            "makers": makers, "n_makers": len(makers),
            "is_emerging": bool(em.get("emergence_score", 0)),
            "emergence_score": em.get("emergence_score"),
            "emergence_ratio": em.get("emergence_ratio"),
            "position_authority": False,
            "diagnostic_only": True,
        })
    # Emerging constraints first (by emergence), then the rest by maker count.
    out.sort(key=lambda x: (not x["is_emerging"],
                            -(x.get("emergence_score") or 0), -x["n_makers"], x["product"]))
    return out


def _period(d: date) -> str:
    return f"{d.year}{d.month:02d}"


def _shift_months(d: date, months: int) -> str:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}{m:02d}"


def _norm(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()
    # Fold common spelling variants so taxonomy and role-ledger labels align
    # ("Optical Fibre" <-> "optical fiber", "defence" <-> "defense").
    s = s.replace("fibre", "fiber").replace("defence", "defense")
    # Canonicalize role-ledger normalized_product labels onto the emergence
    # taxonomy head so the first-token match fires (else "active pharmaceutical"
    # / "bulk drug" miss "Pharma APIs" and "integrated cement" misses "Cement",
    # and the maker set scores 0 emergence unfairly).
    _toks = s.split()
    if ("pharmaceutical" in s or "bulk drug" in s or "pharma api" in s
            or "pharma apis" in s or "api" in _toks or "apis" in _toks):
        s = "pharma api"
    elif "cement" in s:
        s = "cement"
    elif "specialty chemical" in s or "speciality chemical" in s:
        s = "specialty chemical"
    elif "wind turbine" in s or s == "wind turbine generator":
        s = "wind turbine"
    # Fold the granular role-ledger product labels onto their taxonomy head so
    # they merge with the mapper constraint instead of forming single-maker
    # fragment rows (2026: "lithium ion cell", "solar panel", "optical fiber
    # preform" were each ranking as their own row). Kept narrow so the distinct
    # solar cell / wafer / module constraints stay separate.
    elif "optical fib" in s:                       # cable + preform + bare fibre
        s = "optical fiber"
    elif "battery cell" in s or "lithium ion cell" in s or "li ion cell" in s:
        s = "battery cell"                          # NB: bare "lithium" stays distinct
    elif s in ("solar panel", "pv module") or "solar module" in s:
        s = "solar module"
    return s


def fetch_constraint_activity(cur, country: str = "IN") -> dict[str, list[dict]]:
    """All materialised (constraint_label -> monthly activity rows). One cheap read."""
    cur.execute("SELECT to_regclass('public.mg_constraint_signal_activity') AS n")
    if not cur.fetchone()["n"]:
        return {}
    # leading_count is present once build_constraint_signal_activity has run with
    # the early-inflection split; COALESCE keeps this working on older tables.
    rows = q(cur, """
        SELECT constraint_label, period, signal_count,
               COALESCE(leading_count, 0) AS leading_count, doc_count, first_filed
        FROM mg_constraint_signal_activity WHERE country=%s
        ORDER BY constraint_label, period
    """, (country,))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["constraint_label"], []).append(r)
    return out


def emergence_for(activity_rows: list[dict], as_of: date, window_months: int = 12) -> dict:
    """Emergence metrics for one constraint at ``as_of`` from its monthly rows."""
    cutoff = _period(as_of)
    recent_lo = _shift_months(as_of, window_months)
    prior_lo = _shift_months(as_of, 2 * window_months)
    recent = prior = 0
    lead_recent = lead_prior = 0
    first_period = None
    for r in activity_rows:
        p = r["period"]
        if p > cutoff:
            continue
        if first_period is None or p < first_period:
            first_period = p
        if recent_lo < p <= cutoff:
            recent += int(r["signal_count"] or 0)
            lead_recent += int(r.get("leading_count") or 0)
        elif prior_lo < p <= recent_lo:
            prior += int(r["signal_count"] or 0)
            lead_prior += int(r.get("leading_count") or 0)
    ratio = recent / max(prior, 20)          # floor damps tiny-base blowups
    new_this_year = bool(first_period and first_period > recent_lo)
    accel = min(2.0, max(0.0, ratio - 1.0)) if recent >= 40 else 0.0
    novelty = 1.0 if (new_this_year and recent >= 40) else 0.0

    # EARLY-INFLECTION detector: fire on a sharp rise in constraint-FORMING
    # (leading) signals from a low base — BEFORE the total-signal level reaches
    # the 40 the level-based accel needs. This is the earliness edge: a constraint
    # going from ~4 to ~12 leading signals/yr (capex/shortage/localisation) is a
    # real forming shortage even though total chatter has not yet caught up.
    lead_ratio = lead_recent / max(lead_prior, 4)   # low floor: catch genuine take-off
    early_inflection = bool(lead_recent >= 6 and lead_ratio >= 2.0)
    # Contribution scaled by how sharp the leading take-off is; capped below the
    # full level-based score so a confirmed level constraint still ranks above a
    # merely-inflecting one. Only credited when NOT already level-detected.
    early_score = 0.0
    if early_inflection and recent < 40:
        early_score = round(min(1.2, 0.6 * (lead_ratio - 1.0)), 2)
    emergence_score = round(max(accel + novelty, early_score), 2)
    return {
        "recent_signals": recent, "prior_signals": prior,
        "leading_recent": lead_recent, "leading_prior": lead_prior,
        "leading_ratio": round(lead_ratio, 2),
        "emergence_ratio": round(ratio, 2), "new_this_year": new_this_year,
        "early_inflection": early_inflection,
        "level_detected": recent >= 40,
        "emergence_score": emergence_score,
    }


def fetch_all_constraint_emergence(cur, as_of: date, country: str = "IN",
                                   window_months: int = 12) -> dict[str, dict]:
    """Emergence for every materialised taxonomy constraint, keyed by its label.

    Computed once per report; the ranker matches each product to a label via
    ``match_emergence``. Point-in-time (only periods <= as_of are read)."""
    activity = fetch_constraint_activity(cur, country)
    return {label: emergence_for(rows, as_of, window_months)
            for label, rows in activity.items()}


def match_emergence(product_label: str, emergence_all: dict[str, dict]) -> dict:
    """Fuzzy-match a constraint product label to a taxonomy emergence entry.

    'Solar Cell', 'solar_cells_gw', 'Solar PV Cell' all resolve to the taxonomy
    'Solar Cell'. Requires the first distinctive token to agree so 'Solar Cell'
    and 'Solar Wafer' do not collide."""
    if not emergence_all:
        return {}
    npr = _norm(product_label)
    if not npr:
        return {}
    index = {_norm(k): k for k in emergence_all}
    if npr in index:
        return emergence_all[index[npr]]
    ptoks = npr.split()
    for nlabel, label in index.items():
        ltoks = nlabel.split()
        if not ltoks or not ptoks:
            continue
        if ltoks[0] != ptoks[0]:
            continue
        if nlabel in npr or npr in nlabel or (set(ltoks) & set(ptoks)) >= {ltoks[0]}:
            return emergence_all[label]
    return {}


def fetch_constraint_emergence(cur, as_of: date, products: list[str], country: str = "IN",
                               window_months: int = 12) -> dict[str, dict]:
    """Point-in-time emergence per requested product label.

    Matches each requested product to a materialised constraint_label by
    normalised containment (so 'Solar Cell', 'solar_cells_gw' map to the taxonomy
    'Solar Cell'). Products with no activity row get a neutral zero score.
    """
    activity = fetch_constraint_activity(cur, country)
    norm_index = {_norm(label): label for label in activity}
    out: dict[str, dict] = {}
    for product in products:
        npr = _norm(product)
        match = norm_index.get(npr)
        if not match:
            # containment either direction: 'solar cells gw' ~ 'solar cell'
            for nlabel, label in norm_index.items():
                if nlabel and (nlabel in npr or npr in nlabel or
                               set(nlabel.split()) & set(npr.split()) >= set(nlabel.split()[:1])):
                    # require the first distinctive token to match
                    if nlabel.split()[0] == npr.split()[0] if npr.split() else False:
                        match = label
                        break
        if match:
            out[product] = emergence_for(activity[match], as_of, window_months)
            out[product]["matched_label"] = match
        else:
            out[product] = {"recent_signals": 0, "prior_signals": 0, "emergence_ratio": 0.0,
                            "new_this_year": False, "emergence_score": 0.0, "matched_label": None}
    return out
