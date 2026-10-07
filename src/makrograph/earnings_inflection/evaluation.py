"""Outcome sandbox - deliberately isolated from detection.

No detection module may import this file (enforced by a test).  It consumes
already-produced assessments plus an injected, identity-checked price series
and reports forward outcomes for research.  It never feeds outcomes back into
thresholds, and its output is not evidence of alpha: the case set, survivorship
and multiple testing must be addressed before any statistical claim.

Price identity rules (spec §2): NSE bhavcopy is keyed by (trade_date, symbol)
without series/ISIN, so the caller must supply series-filtered, corporate-
action-adjusted closes and say how adjustment was done.  Inferring adjustment
from "no daily move above 45%" is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .contracts import Assessment


@dataclass
class PriceSeries:
    ticker: str
    closes: dict[date, float]
    adjustment_method: str            # e.g. "exchange corporate-action file, splits+bonus"
    series_filter: str                # e.g. "NSE EQ/BE only"

    def __post_init__(self):
        bad = ("no daily move", "heuristic", "assumed adjusted", "")
        if self.adjustment_method.strip().lower() in bad or "45%" in self.adjustment_method:
            raise ValueError("corporate-action adjustment must be explicit, not inferred from price moves")
        if not self.series_filter:
            raise ValueError("series filter required (warrants/partly-paid lines collide on symbol)")

    def close_on_or_after(self, d: date, max_days: int = 7) -> Optional[tuple[date, float]]:
        for i in range(max_days + 1):
            x = d + timedelta(days=i)
            if x in self.closes:
                return x, self.closes[x]
        return None


@dataclass
class OutcomeRow:
    ticker: str
    as_of: date
    evidence_status: str
    horizon_days: int
    start: Optional[date]
    end: Optional[date]
    return_pct: Optional[float]
    benchmark_return_pct: Optional[float] = None
    notes: list[str] = field(default_factory=list)


def forward_outcomes(assessments: list[Assessment], prices: dict[str, PriceSeries],
                     horizons=(182, 365), benchmark: Optional[PriceSeries] = None) -> list[OutcomeRow]:
    rows = []
    for a in assessments:
        ps = prices.get(a.ticker)
        # start strictly AFTER the as-of day to avoid same-day look-ahead
        start_d = a.as_of.date() + timedelta(days=1)
        for h in horizons:
            r = OutcomeRow(a.ticker, a.as_of.date(), a.evidence_status.value, h, None, None, None)
            if ps is None:
                r.notes.append("no identity-checked price series")
                rows.append(r)
                continue
            s, e = ps.close_on_or_after(start_d), ps.close_on_or_after(start_d + timedelta(days=h))
            if s and e:
                r.start, r.end = s[0], e[0]
                r.return_pct = round((e[1] / s[1] - 1) * 100, 2)
                if benchmark:
                    bs, be = benchmark.close_on_or_after(s[0]), benchmark.close_on_or_after(e[0])
                    if bs and be:
                        r.benchmark_return_pct = round((be[1] / bs[1] - 1) * 100, 2)
            else:
                r.notes.append("price data incomplete for horizon")
            rows.append(r)
    return rows


# ===========================================================================
# WP9 - validation before any performance claim
# ===========================================================================
#
# Order of work: (1) freeze the detector configuration, (2) build a company-
# neutral cohort, (3) validate data coverage, extraction, classification and
# delay against independently reviewed labels, and only then (4) look at
# returns for dated lanes - with costs, liquidity, delistings, benchmarks and
# censoring.  Detection never reads anything from this module.

import hashlib as _hashlib
import json as _json
import math as _math
import random as _random
from statistics import median as _median


def config_hash(cfg: dict) -> str:
    cfg = {k: v for k, v in (cfg or {}).items() if k not in ("persistence",)}
    return _hashlib.sha256(_json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class FrozenConfig:
    config_hash: str
    frozen_at: str
    note: str = "frozen before any holdout outcome was examined"

    def check(self, manifest: dict) -> None:
        if manifest.get("config_hash") != self.config_hash:
            raise ValueError("detector output was produced with a different configuration than the frozen one; "
                             "outcomes from it cannot be used for validation")


# --- cohort -----------------------------------------------------------------------

def build_cohort(members: list[dict], anchors: list[date], per_stratum: int, seed: str = "ei-cohort-v1",
                 strata_keys=("sector", "size_bucket", "liquidity_bucket", "cadence")) -> list[dict]:
    """Company-neutral sample: at each anchor, every member LISTED AT THAT DATE (failures and
    later delistings included) is stratified by sector / size / liquidity / reporting cadence and
    sampled deterministically by hash.  Names never enter the rule; named historical winners can
    only be regression examples, never the validation universe."""
    out = []
    for anchor in anchors:
        listed = [m for m in members if date.fromisoformat(m["listed_from"]) <= anchor
                  and (not m.get("delisted_on") or date.fromisoformat(m["delisted_on"]) > anchor)]
        strata: dict[tuple, list[dict]] = {}
        for m in listed:
            strata.setdefault(tuple(m.get(k, "unknown") for k in strata_keys), []).append(m)
        for key, ms in sorted(strata.items()):
            ms = sorted(ms, key=lambda m: _hashlib.sha256(f"{seed}|{anchor}|{m['ticker']}".encode()).hexdigest())
            for m in ms[:per_stratum]:
                out.append({"ticker": m["ticker"], "anchor": anchor.isoformat(),
                            "stratum": dict(zip(strata_keys, key))})
    return out


# --- labelled evaluation -----------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * _math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(c - h, 3), round(c + h, 3)


# flagged = a credible forward setup (supported or better); potential catalysts are a watch state
FLAG_LANES = ("CONFIRMED_FOR_REVIEW", "EXECUTION_VALIDATING", "PROSPECTIVE_SUPPORTED")


def classification_report(predictions: dict[tuple, dict], labels: list[dict]) -> dict:
    """``predictions[(ticker, as_of)]`` = shortlist record (lane, first_defensible_signal, outcome, ...).
    ``labels``: reviewer labels {ticker, as_of, should_flag, documents_available, first_public_signal, labeler}.

    A missed label whose documents were not available/readable is a DATA COVERAGE failure, not a detector
    false negative; the two are reported separately."""
    tp = fp = fn = tn = 0
    coverage_failures, misses, false_alarms, delays = [], [], [], []
    for lab in labels:
        key = (lab["ticker"], lab["as_of"])
        pred = predictions.get(key)
        flagged = bool(pred and pred.get("lane") in FLAG_LANES)
        if lab["should_flag"]:
            if flagged:
                tp += 1
                if lab.get("first_public_signal") and pred.get("first_defensible_signal"):
                    delays.append((date.fromisoformat(pred["first_defensible_signal"][:10])
                                   - date.fromisoformat(lab["first_public_signal"])).days)
            elif not lab.get("documents_available", True) or (pred or {}).get("outcome") in ("not_assessed",
                                                                                            "failed", None):
                coverage_failures.append({**lab, "predicted": (pred or {}).get("lane") or (pred or {}).get("outcome")})
            else:
                fn += 1
                misses.append({**lab, "predicted_lane": pred.get("lane") or "none",
                               "status": pred.get("evidence_status")})
        else:
            if flagged:
                fp += 1
                false_alarms.append({**lab, "predicted_lane": pred.get("lane")})
            else:
                tn += 1
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {"n_labels": len(labels), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": prec, "precision_ci95": wilson(tp, tp + fp),
            "recall_detector": rec, "recall_ci95": wilson(tp, tp + fn),
            "data_coverage_failures": coverage_failures,
            "missed_cases": misses, "false_alarms": false_alarms,
            "detection_delay_days": {"n": len(delays), "median": _median(delays) if delays else None,
                                     "values": delays},
            "review_burden": {"flagged": tp + fp,
                              "per_label": round((tp + fp) / len(labels), 3) if labels else None},
            "labelers": sorted({l.get("labeler", "unknown") for l in labels})}


def extraction_accuracy(parsed: list[dict], labels: list[dict], rel_tol: float = 0.005) -> dict:
    """Numeric / scope accuracy of parsed statement rows against independently keyed figures.
    Rows are matched on (doc_id, metric, period_end, period_type); scope must also match."""
    idx = {(p["doc_id"], p["metric"], p["period_end"], p["period_type"]): p for p in parsed}
    right = wrong = missing = scope_err = 0
    errors = []
    for l in labels:
        p = idx.get((l["doc_id"], l["metric"], l["period_end"], l["period_type"]))
        if p is None:
            missing += 1
            errors.append({**l, "error": "not parsed"})
        elif p.get("scope") != l.get("scope"):
            scope_err += 1
            errors.append({**l, "error": f"scope {p.get('scope')}"})
        elif abs(p["value"] - l["value"]) > rel_tol * max(abs(l["value"]), 1e-9):
            wrong += 1
            errors.append({**l, "error": f"value {p['value']}"})
        else:
            right += 1
    n = len(labels)
    return {"n": n, "correct": right, "wrong_value": wrong, "wrong_scope": scope_err, "not_parsed": missing,
            "accuracy": round(right / n, 3) if n else None, "accuracy_ci95": wilson(right, n), "errors": errors}


# --- returns for dated lanes ----------------------------------------------------------------

@dataclass
class LaneOutcome:
    ticker: str
    lane: str
    signal_date: date
    horizon_days: int
    entry: Optional[date] = None
    exit: Optional[date] = None
    gross_return_pct: Optional[float] = None
    net_return_pct: Optional[float] = None
    excess_vs: dict = field(default_factory=dict)
    max_drawdown_pct: Optional[float] = None
    censored: bool = False
    censor_reason: str = ""
    illiquid: bool = False


def lane_outcomes(entries: list[dict], prices: dict[str, PriceSeries], benchmarks: dict[str, PriceSeries],
                  horizons=(182, 365, 1095), data_until: Optional[date] = None, round_trip_cost_bps: float = 60.0,
                  traded_value: Optional[dict[str, dict[date, float]]] = None, min_traded_value_cr: float = 1.0
                  ) -> list[LaneOutcome]:
    """Entry = first close strictly AFTER the disclosure date (feasible next tradable price).
    Horizons that end after ``data_until`` (the last verified price date) or after a delisting /
    suspension are CENSORED - an incomplete 36-month window is never reported as a 36-month return."""
    out = []
    for e in entries:
        sig = date.fromisoformat(e["first_defensible_signal"][:10]) if e.get("first_defensible_signal") \
            else date.fromisoformat(e["as_of"][:10])
        sig = max(sig, date.fromisoformat(e["as_of"][:10]))          # never before the run's own as-of
        ps = prices.get(e["ticker"])
        for h in horizons:
            r = LaneOutcome(e["ticker"], e["lane"], sig, h)
            if ps is None:
                r.censored, r.censor_reason = True, "no identity-checked adjusted price series"
                out.append(r)
                continue
            ent = ps.close_on_or_after(sig + timedelta(days=1), max_days=10)
            if ent is None:
                r.censored, r.censor_reason = True, "no tradable price after the disclosure (suspended?)"
                out.append(r)
                continue
            r.entry = ent[0]
            target = ent[0] + timedelta(days=h)
            last_px_date = max(ps.closes)
            if data_until and target > data_until:
                r.censored, r.censor_reason = True, f"horizon ends after verified data ({data_until})"
            elif target > last_px_date + timedelta(days=10):
                r.censored, r.censor_reason = True, f"price series ends {last_px_date} (delisted/suspended)"
            if not r.censored:
                ex = ps.close_on_or_after(target, max_days=10)
            elif r.censor_reason.startswith("price series ends") and last_px_date >= ent[0]:
                ex = (last_px_date, ps.closes[last_px_date])     # last observed price of a delisted stock
            else:
                ex = None                                         # not yet observable: no number at all
            if ex:
                r.exit = ex[0]
                path = [v for d, v in sorted(ps.closes.items()) if ent[0] <= d <= ex[0]]
                peak, dd = path[0], 0.0
                for v in path:
                    peak = max(peak, v)
                    dd = min(dd, v / peak - 1)
                r.max_drawdown_pct = round(dd * 100, 1)
                r.gross_return_pct = round((ex[1] / ent[1] - 1) * 100, 2)
                r.net_return_pct = round(r.gross_return_pct - round_trip_cost_bps / 100, 2)
                for name, b in benchmarks.items():
                    bs, be = b.close_on_or_after(ent[0]), b.close_on_or_after(ex[0])
                    if bs and be:
                        r.excess_vs[name] = round(r.net_return_pct - (be[1] / bs[1] - 1) * 100, 2)
            if traded_value is not None:
                tv = [v for d, v in traded_value.get(e["ticker"], {}).items() if d <= sig][-20:]
                r.illiquid = (not tv) or (_median(tv) < min_traded_value_cr)
            out.append(r)
    return out


def _boot_ci(xs: list[float], n: int = 2000, seed: int = 7) -> tuple[Optional[float], Optional[float]]:
    if len(xs) < 3:
        return None, None
    rng = _random.Random(seed)
    meds = sorted(_median([rng.choice(xs) for _ in xs]) for _ in range(n))
    return round(meds[int(0.025 * n)], 2), round(meds[int(0.975 * n)], 2)


def lane_return_report(outcomes: list[LaneOutcome]) -> dict:
    rep = {}
    for lane in sorted({o.lane for o in outcomes}):
        for h in sorted({o.horizon_days for o in outcomes}):
            rows = [o for o in outcomes if o.lane == lane and o.horizon_days == h]
            done = [o for o in rows if not o.censored and o.net_return_pct is not None and not o.illiquid]
            ex = [o.excess_vs.get("broad") for o in done if "broad" in o.excess_vs]
            # overlapping windows: same ticker entered again before the previous window ended
            byt: dict[str, list[date]] = {}
            for o in rows:
                if o.entry:
                    byt.setdefault(o.ticker, []).append(o.entry)
            overlap = sum(1 for ds in byt.values() for a, b in zip(sorted(ds), sorted(ds)[1:]) if (b - a).days < h)
            rep[f"{lane}/{h}d"] = {
                "n": len(rows), "complete": len(done), "censored": sum(1 for o in rows if o.censored),
                "illiquid_excluded": sum(1 for o in rows if o.illiquid),
                "median_net_return_pct": _median([o.net_return_pct for o in done]) if done else None,
                "median_excess_vs_broad_pct": _median(ex) if ex else None,
                "median_excess_ci95": _boot_ci(ex) if ex else (None, None),
                "hit_rate_vs_broad": round(sum(1 for x in ex if x > 0) / len(ex), 3) if ex else None,
                "worst_drawdown_pct": min((o.max_drawdown_pct for o in done if o.max_drawdown_pct is not None),
                                          default=None),
                "overlapping_windows": overlap,
                "note": "small samples; overlapping windows are not independent; censored rows excluded, not "
                        "counted as returns",
            }
    return rep


def evaluation_report(frozen: FrozenConfig, manifest: dict, classification: dict, extraction: Optional[dict],
                      returns: Optional[dict], cohort_note: str) -> str:
    frozen.check(manifest)
    L = ["# Detection validation report", "",
         f"Config {frozen.config_hash[:12]} frozen at {frozen.frozen_at} ({frozen.note}). Cohort: {cohort_note}", "",
         "## Classification (independently reviewed labels)",
         f"- labels {classification['n_labels']} (labelers: {', '.join(classification['labelers'])}); "
         f"TP {classification['tp']} FP {classification['fp']} FN {classification['fn']} TN {classification['tn']}",
         f"- precision {classification['precision']} (95% CI {classification['precision_ci95']}); detector recall "
         f"{classification['recall_detector']} (95% CI {classification['recall_ci95']})",
         f"- data-coverage failures (not detector misses): {len(classification['data_coverage_failures'])}",
         f"- detection delay vs first public signal: {classification['detection_delay_days']}",
         f"- review burden: {classification['review_burden']}", "", "### Missed cases (detector false negatives)"]
    L += [f"- {m['ticker']} @ {m['as_of']}: predicted {m['predicted_lane']} ({m.get('status')}); "
          f"label note: {m.get('note', '')}" for m in classification["missed_cases"]] or ["- none in this sample"]
    L += ["", "### False alarms"]
    L += [f"- {m['ticker']} @ {m['as_of']}: predicted {m['predicted_lane']}" for m in classification["false_alarms"]] \
        or ["- none in this sample"]
    L += ["", "### Data-coverage failures"]
    L += [f"- {m['ticker']} @ {m['as_of']}: {m.get('predicted')}" for m in classification["data_coverage_failures"]] \
        or ["- none in this sample"]
    if extraction:
        L += ["", "## Extraction accuracy",
              f"- {extraction['correct']}/{extraction['n']} correct (95% CI {extraction['accuracy_ci95']}); wrong value "
              f"{extraction['wrong_value']}, wrong scope {extraction['wrong_scope']}, not parsed {extraction['not_parsed']}"]
    if returns:
        L += ["", "## Returns by dated lane (after costs; censored windows excluded)"]
        for k, v in returns.items():
            L.append(f"- {k}: n {v['n']}, complete {v['complete']}, censored {v['censored']}, median net "
                     f"{v['median_net_return_pct']}%, median excess vs broad {v['median_excess_vs_broad_pct']}% "
                     f"(CI {v['median_excess_ci95']}), hit rate {v['hit_rate_vs_broad']}, worst drawdown "
                     f"{v['worst_drawdown_pct']}%, overlapping windows {v['overlapping_windows']}")
    L += ["", "No claim of multibagger detection follows from this report; sample sizes and censoring bound "
              "every statement above."]
    return "\n".join(L)


# --- replay timelines: earliest detection, lead time, false alarms, earnings delivery -------------

_POSITIVE_STAGES = ("leading", "first_results_validated", "realized_emerging", "confirmed")


def _pd(x):
    from datetime import datetime as _dt
    if x is None:
        return None
    return _dt.fromisoformat(x).date() if "T" in x or "+" in x else date.fromisoformat(x[:10])


@dataclass
class ThesisEpisode:
    mechanism: str
    origin: str                          # "leading" | "realized"
    signal_at: Optional[date]            # earliest defensible time of the signal
    signal: str
    first_seen_as_of: Optional[date]     # first replay date that showed it (replay granularity)
    last_seen_as_of: Optional[date] = None
    validation_outcome: str = "pending"
    validated_at: Optional[date] = None  # first results after the signal were public
    validation_observed: str = ""
    confirmed_as_of: Optional[date] = None
    final_stage: str = ""
    verdict: str = "unresolved"          # validated | false_alarm | unresolved
    delivery: dict = field(default_factory=dict)

    @property
    def lead_time_days(self) -> Optional[int]:
        """Signal to first validating results."""
        if self.signal_at and self.validated_at:
            return (self.validated_at - self.signal_at).days
        return None


def thesis_timeline(snapshots: list[dict]) -> dict:
    """Episodes per mechanism from successive replay snapshots (see ``replay.snapshot``).

    A leading episode is keyed by its signal time; a realized-first episode by its earliest
    defensible time.  Verdict: validated when the first results after the signal showed the
    change; false_alarm when they did not (or moved against it); unresolved when results are
    still pending / overdue at the end of the replay or the signal expired unchecked."""
    eps: dict[tuple, ThesisEpisode] = {}
    statuses: list[tuple[date, str]] = []
    for s in sorted((x for x in snapshots if "error" not in x), key=lambda x: x["as_of"]):
        d = date.fromisoformat(s["as_of"])
        statuses.append((d, s["evidence_status"]))
        for m in s["mechanisms"]:
            if m["stage"] not in _POSITIVE_STAGES + ("first_results_not_validated", "adverse") \
                    and not m["leading_signal_at"]:
                continue
            if m["leading_signal_at"]:
                key = (m["mechanism"], "leading", m["leading_signal_at"])
                origin, sig, at = "leading", m["leading_signal"], _pd(m["leading_signal_at"])
            elif m["stage"] in ("realized_emerging", "confirmed"):
                key = (m["mechanism"], "realized", m["earliest_defensible_at"])
                origin, sig, at = "realized", m.get("confirmation") or "first seen in reported results", \
                    _pd(m["earliest_defensible_at"])
            else:
                continue
            ep = eps.get(key) or ThesisEpisode(m["mechanism"], origin, at, sig, d)
            eps[key] = ep
            ep.last_seen_as_of, ep.final_stage = d, m["stage"]
            v = m.get("validation")
            if v:
                ep.validation_outcome = v["outcome"]
                if v["outcome"] in ("validated", "not_validated", "adverse"):
                    ep.validated_at = _pd(v["observed_at"]) if v["observed_at"] else ep.validated_at
                    ep.validation_observed = f"{v['period_end']}: {v['observed']}"
            if m["stage"] == "confirmed" and ep.confirmed_as_of is None:
                ep.confirmed_as_of = d
    for ep in eps.values():
        if ep.origin == "realized":
            ep.verdict = "validated"           # seen in results by construction
        elif ep.validation_outcome == "validated":
            ep.verdict = "validated"
        elif ep.validation_outcome in ("not_validated", "adverse"):
            ep.verdict = "false_alarm"
    first = lambda st: next((d for d, x in statuses if x == st), None)  # noqa: E731
    leading = sorted((e for e in eps.values() if e.origin == "leading"), key=lambda e: e.signal_at or date.max)
    confirmed_status = first("EXECUTION_CONFIRMED")
    earliest = min((e.signal_at for e in eps.values() if e.signal_at and e.verdict != "false_alarm"), default=None)
    earliest_leading = min((e.signal_at for e in leading if e.signal_at and e.verdict != "false_alarm"), default=None)
    return {
        "earliest_leading_signal": earliest_leading,
        "episodes": sorted(eps.values(), key=lambda e: (e.signal_at or date.max, e.mechanism)),
        "first_status": {st: first(st) for st in ("EXECUTION_EMERGING", "EXECUTION_CONFIRMED",
                                                  "COMMITMENT_BACKED", "EARLY_COMMITMENT_UNVERIFIED")},
        "earliest_defensible_signal": earliest,
        # how far a standing LEADING signal preceded the first confirmed status that followed it
        "days_before_first_confirmed_status": next(
            ((d - earliest_leading).days for d, x in statuses
             if x == "EXECUTION_CONFIRMED" and earliest_leading and d >= earliest_leading), None),
        "leading_episodes": len(leading),
        "false_alarms": sum(1 for e in leading if e.verdict == "false_alarm"),
        "validated": sum(1 for e in leading if e.verdict == "validated"),
        "unresolved": sum(1 for e in leading if e.verdict == "unresolved"),
        # signal -> first results that validated it (validated episodes only)
        "lead_times_days": [e.lead_time_days for e in leading if e.verdict == "validated"
                            and e.lead_time_days is not None],
    }


def earnings_delivery(episode: ThesisEpisode, series, horizon_periods: int = 4,
                      data_until: Optional[date] = None) -> dict:
    """Did earnings follow?  TTM revenue and PAT at the last period public before the signal vs
    ``horizon_periods`` later, from the full (later) series.  Evaluation only: uses data the
    detector could not see.  Missing figures leave the outcome censored, never zero."""
    from .contracts import Metric
    from .financial_series import prev_period_end
    p = series.cadence(Metric.REVENUE)
    if p is None or episode.signal_at is None:
        return {"censored": True, "reason": "no series"}
    base = None
    for e in series.period_ends(Metric.REVENUE, p):
        pt = series.get(Metric.REVENUE, e, p)
        at = pt and (pt.first_public_at or pt.available_at)
        if at and at.date() <= episode.signal_at:
            base = e
    if base is None:
        return {"censored": True, "reason": "no results public before the signal"}
    from .thesis import next_period_end_on_or_after
    later = base
    for _ in range(horizon_periods):                 # calendar periods, not available ones
        later = next_period_end_on_or_after(later + timedelta(days=1), p)
    out = {"base_period": base.isoformat(), "horizon_period": later.isoformat(),
           "censored": series.get(Metric.REVENUE, later, p) is None
           or (data_until is not None and later > data_until)}
    if out["censored"]:
        out["reason"] = f"period {later} ({horizon_periods} {p} periods later) not available"
        return out
    for metric, name in ((Metric.REVENUE, "revenue"), (Metric.PAT, "pat")):
        a, _ = series.ttm(metric, base, p)
        b, _ = series.ttm(metric, later, p)
        out[f"ttm_{name}_base"], out[f"ttm_{name}_later"] = a, b
        out[f"ttm_{name}_change_pct"] = ((b / a - 1) * 100 if a and b is not None and a > 0 else None)
    if out.get("ttm_pat_change_pct") is None and out.get("ttm_revenue_change_pct") is None:
        out["censored"], out["reason"] = True, "TTM figures incomplete"
    return out


def timeline_report(ticker: str, timeline: dict, note: str = "") -> str:
    L = [f"# Replay timeline — {ticker}", "",
         "> Research evaluation of a point-in-time replay. One issuer is an example, not a validation "
         "sample; no return or performance claim follows from it.", ""]
    if note:
        L += [note, ""]
    fs = timeline["first_status"]
    L.append(f"- Earliest defensible signal still standing (any origin): "
             f"**{timeline['earliest_defensible_signal'] or '—'}** · earliest standing leading signal: "
             f"**{timeline['earliest_leading_signal'] or '—'}**")
    L.append(f"- First EXECUTION_EMERGING: {fs['EXECUTION_EMERGING'] or '—'} · first EXECUTION_CONFIRMED: "
             f"{fs['EXECUTION_CONFIRMED'] or '—'}"
             + (f" · the leading signal preceded the next confirmed status by "
                f"{timeline['days_before_first_confirmed_status']} days"
                if timeline["days_before_first_confirmed_status"] is not None else ""))
    L.append(f"- Leading episodes: {timeline['leading_episodes']} · validated {timeline['validated']} · "
             f"false alarms {timeline['false_alarms']} · unresolved {timeline['unresolved']}")
    if timeline["lead_times_days"]:
        L.append(f"- Signal -> validating results (days): {timeline['lead_times_days']}")
    L += ["", "| Mechanism | Origin | Signal (public) | First seen in replay | First-results check | "
          "Confirmed | Verdict | Earnings delivery (TTM, +4 periods) |", "|---|---|---|---|---|---|---|---|"]
    for e in timeline["episodes"]:
        d = e.delivery or {}
        if not d:
            dl = "—"
        elif d.get("censored"):
            dl = f"censored ({d.get('reason', '')})"
        else:
            parts = [f"{k.split('_')[1]} {d[k]:+.0f}%" for k in ("ttm_revenue_change_pct", "ttm_pat_change_pct")
                     if d.get(k) is not None]
            dl = f"{d['base_period']} -> {d['horizon_period']}: " + ", ".join(parts)
        L.append(f"| {e.mechanism} | {e.origin} | {e.signal_at or '—'}: {e.signal[:110]} | {e.first_seen_as_of} | "
                 + ("n/a (seen in results)" if e.origin == "realized" else e.validation_outcome)
                 + (f" ({e.validated_at}; {e.validation_observed})" if e.validated_at else "")
                 + f" | {e.confirmed_as_of or '—'} | {e.verdict} | {dl} |")
    return "\n".join(L) + "\n"


# --- catalyst replays: early detection, not hindsight success --------------------------------------

_STAGES = ("potential_catalyst", "supported_prospective_inflection", "execution_validating",
           "confirmed_for_investment_review", "delayed", "contradicted", "data_unavailable")


_FLAGGED = ("supported_prospective_inflection", "execution_validating", "confirmed_for_investment_review")
# verdicts under which an alert can be credited with an inflection: its own test was not failed
_CREDITABLE = ("confirmed", "executed_unconfirmable", "open")


def inflection_credit(window: dict, alerts: list[dict]) -> dict:
    """Which alerts may be credited with a mechanical earnings inflection (rules-6 D13).

    Timing alone is not enough: an alert counts only if it was flagged inside the window (from the
    window's first period to the publication of its last), its change was not immaterial when it was
    flagged, and its own verdict was not contradicted, delayed or data-unavailable.  ``window`` has
    ``from`` and ``end_published`` (ISO dates); each alert has ``catalyst_id``, ``supported_at``,
    ``verdict`` and ``contribution_when_flagged``.  Also returns the timing-only list, for comparison
    with rules-5 and earlier results."""
    end, start = window.get("end_published"), window["from"]
    timed = [a for a in alerts if end and start <= str(a["supported_at"]) <= end]
    credited = [a["catalyst_id"] for a in timed if a["verdict"] in _CREDITABLE
                and a.get("contribution_when_flagged") != "immaterial"]
    return {"alert_before": credited, "timing_only": [a["catalyst_id"] for a in timed], "missed": not credited}


def rules_fingerprint(thresholds: dict, rules_version: str) -> str:
    """Freeze the catalyst rules before looking at outcomes: record this with every evaluation."""
    return config_hash({"rules_version": rules_version, "thresholds": thresholds})[:16]


def catalyst_timeline(snapshots: list[dict]) -> dict:
    """Per catalyst: first public disclosure, first replay date in each stage, days to validating
    and to confirmation, final stage.  Verdicts: confirmed / contradicted / delayed /
    data_unavailable / open (still potential, supported or validating at the end)."""
    cats: dict[str, dict] = {}
    for s in sorted((x for x in snapshots if "error" not in x), key=lambda x: x["as_of"]):
        d = date.fromisoformat(s["as_of"])
        for c in s.get("catalysts", []):
            r = cats.setdefault(c["catalyst_id"], {"catalyst_id": c["catalyst_id"], "kind": c["kind"],
                                                   "first_public": _pd(c["first_public_at"]),
                                                   "change": c["operating_change"], "first_seen_as_of": d,
                                                   "stage_first_as_of": {}, "stages": []})
            r["stage_first_as_of"].setdefault(c["stage"], d)
            if not r["stages"] or r["stages"][-1][1] != c["stage"]:
                r["stages"].append((d, c["stage"]))
            r["final_stage"], r["last_seen_as_of"] = c["stage"], d
            r["confirmation_blocked"] = c.get("confirmation_blocked", "")
            r["contribution"], r["base_crore"] = c["contribution"], c["base_crore"]
            if c["stage"] in _FLAGGED and "contribution_when_flagged" not in r:
                r["contribution_when_flagged"] = c["contribution"]
            r["initial_stage"] = r.get("initial_stage") or c.get("initial_stage")
            for k in ("supported_at", "validating_at", "confirmed_at", "materiality_supported_at",
                      "execution_supported_at"):
                if c.get(k):                      # point-in-time dates computed from what was public then
                    r.setdefault(k, _pd(c[k]))
    for r in cats.values():
        f, fp = r["stage_first_as_of"], r["first_public"]

        def days(key, st):
            when = r.get(key) or f.get(st)        # exact date when known, else first replay date in the stage
            return (when - fp).days if when and fp else None
        r["days_to_supported"] = days("supported_at", "supported_prospective_inflection")
        r["days_to_validating"] = days("validating_at", "execution_validating")
        r["days_to_confirmed"] = days("confirmed_at", "confirmed_for_investment_review")
        r["ever_supported"] = any(st in f for st in ("supported_prospective_inflection", "execution_validating",
                                                     "confirmed_for_investment_review"))
        fs = r["final_stage"]
        r["verdict"] = ("confirmed" if "confirmed_for_investment_review" in f and fs != "contradicted" else
                        fs if fs in ("contradicted", "delayed", "data_unavailable") else
                        # execution verified, confirmation impossible from the disclosures (rules-4 D3)
                        "executed_unconfirmable" if fs == "execution_validating" and r.get("confirmation_blocked")
                        else "open")
    rows = sorted(cats.values(), key=lambda r: (r["first_public"] or date.max, r["kind"]))
    supported = [r for r in rows if r["ever_supported"]]
    return {
        "catalysts": rows,
        # the date support (materiality + execution pathway) was established - not the first mention
        "first_defensible_catalyst": min((r.get("supported_at") or r["first_public"] for r in supported
                                          if r.get("supported_at") or r["first_public"]), default=None),
        "first_any_catalyst": min((r["first_public"] for r in rows if r["first_public"]), default=None),
        "supported": len(supported),
        "confirmed": sum(1 for r in supported if r["verdict"] == "confirmed"),
        "false_positives": sum(1 for r in supported if r["verdict"] == "contradicted"),
        "delayed": sum(1 for r in supported if r["verdict"] == "delayed"),
        "executed_unconfirmable": sum(1 for r in supported if r["verdict"] == "executed_unconfirmable"),
        "data_unavailable": sum(1 for r in rows if r["verdict"] == "data_unavailable"),
        "potential_only": sum(1 for r in rows if not r["ever_supported"]),
        "days_to_confirmed": [r["days_to_confirmed"] for r in supported if r["days_to_confirmed"] is not None],
        "rules_version": next((s.get("rules_version") for s in snapshots if s.get("rules_version")), ""),
    }


def review_entries(timeline: dict, ticker: str) -> list[dict]:
    """Entries for ``lane_outcomes`` dated at CONFIRMATION (the investment-review date), never at
    the earlier watch-list date: returns are measured from when the thesis was confirmed."""
    out = []
    for r in timeline["catalysts"]:
        d = r.get("confirmed_at") or r["stage_first_as_of"].get("confirmed_for_investment_review")
        if d:
            out.append({"ticker": ticker, "as_of": d.isoformat(), "lane": "CONFIRMED_FOR_INVESTMENT_REVIEW",
                        "catalyst_id": r["catalyst_id"]})
    return out


def missed_candidates(labels: list[dict], timelines: dict[str, dict], coverage: dict[str, dict]) -> list[dict]:
    """Labelled inflections with no supported catalyst: split into data-coverage misses (documents
    missing or unparsed) and detector misses.  ``coverage[ticker]`` = {"missing_periods": n,
    "unreadable_documents": n} from the assessments."""
    out = []
    for l in labels:
        if not l.get("should_detect"):
            continue
        tl = timelines.get(l["ticker"])
        if tl and tl["supported"]:
            continue
        cov = coverage.get(l["ticker"], {})
        data = (tl is None or cov.get("missing_periods", 0) > 0 or cov.get("unreadable_documents", 0) > 0
                or (tl and tl["data_unavailable"]))
        out.append({"ticker": l["ticker"], "reason": "data coverage" if data else "detector miss",
                    "detail": cov})
    return out


def catalyst_report(ticker: str, tl: dict, note: str = "", fingerprint: str = "") -> str:
    L = [f"# Catalyst replay — {ticker}", "",
         "> Early-detection evaluation of a point-in-time replay. One issuer is a regression example, not a "
         "validation sample. Stages are research states, not instructions to invest; returns, where shown, are "
         "measured from the confirmation date only.", ""]
    if note:
        L += [note, ""]
    if fingerprint:
        L.append(f"- Frozen rules: {tl['rules_version']} (fingerprint {fingerprint})")
    L.append(f"- First defensible (supported) catalyst: **{tl['first_defensible_catalyst'] or '—'}** · first catalyst "
             f"of any stage: {tl['first_any_catalyst'] or '—'}")
    L.append(f"- Supported catalysts {tl['supported']}: confirmed {tl['confirmed']}, contradicted (false positives) "
             f"{tl['false_positives']}, delayed {tl['delayed']}, executed but not confirmable "
             f"{tl.get('executed_unconfirmable', 0)}; potential only {tl['potential_only']}; "
             f"data unavailable {tl['data_unavailable']}")
    if tl["days_to_confirmed"]:
        L.append(f"- Days from first disclosure to confirmation: {tl['days_to_confirmed']}")
    L += ["", "| Catalyst | First public | Stage path (replay dates) | Days to supported / validating / confirmed | "
          "Contribution (base EBITDA/yr) | Verdict |", "|---|---|---|---|---|---|"]
    for r in tl["catalysts"]:
        path = " → ".join(f"{st} ({d})" for d, st in r["stages"])
        contrib = (f"{r['base_crore']:,.1f} cr" if r["base_crore"] is not None else
                   r["contribution"].replace("_", " "))
        L.append(f"| {r['kind']}: {r['change'][:90]} | {r['first_public'] or '—'} | {path} | "
                 f"{r['days_to_supported'] if r['days_to_supported'] is not None else '—'} / "
                 f"{r['days_to_validating'] if r['days_to_validating'] is not None else '—'} / "
                 f"{r['days_to_confirmed'] if r['days_to_confirmed'] is not None else '—'} | {contrib} | "
                 f"{r['verdict']} |")
    return "\n".join(L) + "\n"
