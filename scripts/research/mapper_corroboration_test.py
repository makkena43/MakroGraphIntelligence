#!/usr/bin/env python3
"""
Test a fix for the beneficiary mapper BEFORE wiring it in.

THE DEFECT (audited)
--------------------
The mapper maps a company to a constrained product on signal co-occurrence
and scores conviction purely from signal COUNTS. Nothing checks the company
actually supplies the product. Result at 2022-12-31: 2,805 companies mapped
to "Defense electronics", only 19% industry-coherent overall, and staffing
firms / banks / cement at 0.85 conviction.

THE PROPOSED FIX
----------------
Corroboration gate: keep a mapping at full strength only if the company's OWN
filings independently evidence making that product (it appears in
mg_company_capabilities for the product, manufacturer=true). Un-corroborated
mappings are demoted, not deleted.

WHY TEST FIRST, NOT SHIP
------------------------
Every mechanical fix this session has had an edge case that broke a real
constraint. The obvious risk here: the capability mapper MISSES some genuine
constraints (its 'Steel' term was dropped by the frequency gate, so it finds
zero makers for CRGO Steel — the single best-performing constraint). A naive
corroboration gate would therefore delete the best names. This script
measures that risk directly on two axes:

  1. does corroboration improve industry-COHERENCE?           (precision)
  2. does the corroborated basket's forward RETURN hold up,   (does it keep
     or does it destroy real constraints like CRGO Steel?      the winners?)

Only if BOTH hold does the fix get wired into discovery.
"""
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date

import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stock_report"))
sys.path.insert(0, HERE)

from extract_report_data import connect            # noqa: E402
from moonshot_screen import alias_map              # noqa: E402
from winner_autopsy import fwd_return              # noqa: E402

ANCHORS = ["2020-12-31", "2021-12-31", "2022-12-31"]
HORIZON = 1095


def snap(cur, table, as_of, col="as_of_date"):
    cur.execute(f"SELECT MAX({col}) s FROM {table} WHERE {col} <= %s", (as_of,))
    r = cur.fetchone()
    return r["s"] if r else None


def main():
    conn = connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    al = alias_map(cur)

    base_all, corr_all, uncorr_all = [], [], []
    coh_base = coh_corr = tot_base = tot_corr = 0
    kept_by_constraint = defaultdict(lambda: [0, 0])   # product -> [corr, total]

    for a in ANCHORS:
        as_of = date.fromisoformat(a)
        bsnap = snap(cur, "mg_india_beneficiaries", as_of)
        csnap = snap(cur, "mg_company_capabilities", as_of)
        if not bsnap:
            continue

        # corroboration set: (product, ticker) the capability mapper confirms
        corr_set = set()
        cap_products = set()
        if csnap:
            cur.execute("""SELECT product, ticker FROM mg_company_capabilities
                           WHERE as_of_date=%s AND manufacturer""", (csnap,))
            for r in cur.fetchall():
                corr_set.add((r["product"], r["ticker"]))
                cap_products.add(r["product"])

        cur.execute("""SELECT b.constrained_product p, UPPER(TRIM(b.ticker)) t,
                              b.conviction_score conv,
                              COALESCE(s.industry_bse,s.industry_nse,'') ind
                       FROM mg_india_beneficiaries b
                       LEFT JOIN security_master s
                         ON UPPER(TRIM(s.nse_symbol))=UPPER(TRIM(b.ticker))
                       WHERE b.as_of_date=%s AND b.constrained_product IS NOT NULL
                         AND b.ticker <> ''""", (bsnap,))
        rows = cur.fetchall()

        # expected sector per product = modal sector of its CORROBORATED makers
        exp = {}
        sec_by_p = defaultdict(Counter)
        for r in rows:
            if (r["p"], r["t"]) in corr_set:
                sec = (r["ind"] or "").split("|")[0].strip()
                if sec:
                    sec_by_p[r["p"]][sec] += 1
        exp = {p: c.most_common(1)[0][0] for p, c in sec_by_p.items() if c}

        for r in rows:
            p, t = r["p"], r["t"]
            fr = fwd_return(cur, al, t, as_of, HORIZON)
            corroborated = (p, t) in corr_set
            product_has_cap = p in cap_products    # can this product be corroborated at all?

            if fr is not None:
                base_all.append(fr)
                # only APPLY the gate where the product is coverable by the
                # capability mapper; where it's not (e.g. CRGO Steel), the gate
                # can't judge, so those mappings pass through unchanged
                if not product_has_cap or corroborated:
                    corr_all.append(fr)
                    kept_by_constraint[p][0] += 1
                else:
                    uncorr_all.append(fr)
                kept_by_constraint[p][1] += 1

            # coherence (only where we have an expected sector)
            e = exp.get(p)
            if e:
                sec = (r["ind"] or "").split("|")[0].strip()
                tot_base += 1
                coh_base += (sec == e)
                if not product_has_cap or corroborated:
                    tot_corr += 1
                    coh_corr += (sec == e)
        print(f"  scored {a}", flush=True)

    def med(x):
        return statistics.median(x) if x else None

    print(f"\n{'='*72}\nMAPPER CORROBORATION TEST — {len(ANCHORS)} anchors, 3y forward\n{'='*72}")
    print(f"  industry coherence   BEFORE {coh_base}/{tot_base} = "
          f"{coh_base/max(tot_base,1)*100:.0f}%   "
          f"AFTER {coh_corr}/{tot_corr} = {coh_corr/max(tot_corr,1)*100:.0f}%")
    print(f"  basket 3y return     BEFORE median {med(base_all):.1f}% (n={len(base_all)})")
    print(f"                       AFTER  median {med(corr_all):.1f}% (n={len(corr_all)})")
    print(f"  DROPPED (uncorroborated) median {med(uncorr_all):.1f}% (n={len(uncorr_all)})")
    print(f"\n  per-constraint kept fraction (corroborated / total):")
    for p, (k, tt) in sorted(kept_by_constraint.items(), key=lambda kv:-kv[1][1]):
        print(f"    {p[:44]:46} {k:4d}/{tt:4d}  ({k/max(tt,1)*100:3.0f}% kept)")
    print("\n  READ: fix is good only if coherence RISES and AFTER return does not "
          "fall below BEFORE. If DROPPED return >= AFTER return, the gate is "
          "throwing away winners (capability-coverage gaps) and must not ship.")
    conn.close()


if __name__ == "__main__":
    main()
