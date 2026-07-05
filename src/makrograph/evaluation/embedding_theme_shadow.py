"""SHADOW: vocabulary-free theme matching via embeddings.

Replaces hand-written keyword families (hindsight-contaminated) with
semantic similarity: a company's evidence vector (mean embedding of its
signal-bearing documents) vs each active theme's name embedding — computed
by the same local MiniLM model that built mg_embeddings. No authored
vocabulary anywhere, so it generalizes to themes that don't exist yet.

Runs side-by-side with the production keyword matcher and reports:
  - agreement (both pick semantically same theme)
  - embedding-only coverage (keyword matcher showed '—', embeddings found one)
  - disagreements (for human review)

Usage:
    PYTHONPATH=src:. python -m makrograph.evaluation.embedding_theme_shadow --year 2024
"""

import argparse
import logging

logger = logging.getLogger(__name__)

SIM_FLOOR = 0.30   # below this, show no theme (mirrors the keyword gate's spirit)


def run(country: str, year: int) -> str:
    import numpy as np
    from sentence_transformers import SentenceTransformer
    import backend.main as app_main

    base = app_main.get_investment_final_shortlist(country=country, year=year)
    shortlist = base.get("final_shortlist", [])
    themes = base.get("constraint_regimes", [])
    if not shortlist or not themes:
        return "empty shortlist or theme universe"

    from makrograph.themes.theme_detector import _is_noise_entity
    theme_names = [
        t.get("theme_name", "") for t in themes
        if t.get("theme_name")
        and not _is_noise_entity(t["theme_name"].split(":")[0]
                                 .replace(" Critical Shortage", "")
                                 .replace(" Severe Constraint", "").strip())
    ]
    # Strip structural suffixes ("X: Demand-Supply Tension" → "X") so the
    # theme vector is the TOPIC, comparable with evidence sentences.
    def _topic(nm: str) -> str:
        core = nm.split(":")[0]
        for suf in (" Critical Shortage", " Severe Constraint"):
            core = core.replace(suf, "")
        return core.strip()
    model = SentenceTransformer("all-MiniLM-L6-v2")
    tvecs = model.encode([_topic(n) for n in theme_names], normalize_embeddings=True)

    pg = app_main.get_pg()
    from datetime import date
    yr = year
    to_d = date(yr, 12, 31)
    from_d = date(yr, 1, 1)

    tickers = [(c.get("ticker") or "").upper() for c in shortlist if c.get("ticker")]
    with pg._conn() as conn:
        cur = conn.cursor()
        # The company's evidence = its SIGNAL CONTEXT sentences (short,
        # content-rich), NOT whole-document chunks (which in India are
        # dominated by exchange-filing boilerplate). Embedded fresh with the
        # same local model — cheap (a few hundred short texts).
        cur.execute(
            f"""SELECT UPPER(TRIM(d.ticker)) AS tk, LEFT(s.context_text, 350)
                FROM mg_signals s JOIN mg_documents d ON d.id = s.document_id
                WHERE d.country = %s AND d.filed_at BETWEEN %s AND %s
                  AND UPPER(TRIM(d.ticker)) = ANY(%s)
                  AND s.context_text IS NOT NULL AND LENGTH(s.context_text) > 60
                  AND (({app_main.CONSTRAINT_PRED_SQL})
                       OR ({app_main.DS_SELLER_PRED_SQL})
                       OR s.signal_type IN ('capex_increase',
                            'realized_margin_expansion','demand_surge'))""",
            (country, from_d, to_d, tickers),
        )
        co_texts: dict[str, list] = {}
        for tk, txt in cur.fetchall():
            if len(co_texts.setdefault(tk, [])) < 25:   # cap per company
                co_texts[tk].append(txt)

    co_vecs: dict[str, list] = {}
    for tk, texts in co_texts.items():
        vs = model.encode(texts, normalize_embeddings=True)
        co_vecs[tk] = list(vs)

    lines = [f"{country} {year}: {len(shortlist)} companies, {len(theme_names)} themes, "
             f"{len(co_vecs)} with embeddings"]
    agree = emb_only = kw_only = disagree = none_both = 0
    rows = []
    for c in shortlist:
        tk = (c.get("ticker") or "").upper()
        kw_theme = c.get("theme") or ""
        vecs = co_vecs.get(tk)
        if not vecs:
            rows.append((tk, kw_theme, "(no embeddings)", 0.0))
            continue
        # Per-theme score = mean of the TOP-2 matching evidence sentences.
        # Max alone lets one stray sentence win a wrong theme; requiring two
        # agreeing sentences keeps the "one clear railway sentence" property
        # while damping single-sentence flukes.
        V = np.vstack(vecs)                 # texts × dim
        M = V @ tvecs.T                     # texts × themes
        if M.shape[0] >= 2:
            sims = np.sort(M, axis=0)[-2:, :].mean(axis=0)
        else:
            sims = M.max(axis=0)
        bi = int(np.argmax(sims))
        emb_theme, sim = (theme_names[bi], float(sims[bi]))
        if sim < SIM_FLOOR:
            emb_theme = ""
        rows.append((tk, kw_theme, emb_theme, round(sim, 3)))
        if kw_theme and emb_theme:
            # same theme family if sharing a word (case-insensitive)
            kws = set(kw_theme.lower().split())
            ews = set(emb_theme.lower().split())
            if kws & ews:
                agree += 1
            else:
                disagree += 1
        elif emb_theme and not kw_theme:
            emb_only += 1
        elif kw_theme and not emb_theme:
            kw_only += 1
        else:
            none_both += 1

    lines.append(f"agree={agree} emb_only(new coverage)={emb_only} "
                 f"kw_only={kw_only} disagree={disagree} none={none_both}")
    lines.append(f"\n{'ticker':<12}{'keyword theme':<38}{'embedding theme':<38}{'sim':>6}")
    for tk, kt, et, sim in rows:
        mark = " " if (kt.lower().split() and set(kt.lower().split()) & set(et.lower().split())) else "≠"
        lines.append(f"{tk:<12}{(kt or '—')[:36]:<38}{(et or '—')[:36]:<38}{sim:>6}{mark if kt and et else ''}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="IN")
    ap.add_argument("--year", type=int, default=2024)
    args = ap.parse_args()
    print(run(args.country, args.year))
