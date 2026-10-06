"""Append-only catalyst ledger: every change of a catalyst is a new dated version.

Detection rebuilds catalysts from all documents public by the as-of date (so a new quarter
adds evidence but never erases an older catalyst).  The ledger records what each run saw:
first detection, every stage change and milestone verdict, with the as-of date.  Earlier
versions are never rewritten, so "when was this first defensible?" and "when did it become
confirmed?" stay answerable after later filings arrive.

Local JSON-lines per ticker (``<root>/<TICKER>.jsonl``); the optional test-database tables
``ei_catalyst`` / ``ei_catalyst_version`` mirror it (see schema).  Writing is explicit:
``record()`` is called by the caller, never on import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .contracts import Catalyst, to_jsonable


def _fingerprint(c: Catalyst) -> str:
    return json.dumps({"stage": c.stage.value, "milestones": [(m.question, m.status.value, m.observed)
                                                              for m in c.milestones],
                       "contribution": [c.contribution.status, c.contribution.base_crore],
                       "chain": [(x.link, x.status) for x in c.chain]}, sort_keys=True, default=str)


class CatalystLedger:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _file(self, ticker: str) -> Path:
        return self.root / f"{ticker.upper()}.jsonl"

    def versions(self, ticker: str) -> list[dict]:
        f = self._file(ticker)
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]

    def history(self, ticker: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for v in self.versions(ticker):
            out.setdefault(v["catalyst_id"], []).append(v)
        return out

    def record(self, ticker: str, as_of: str, catalysts: Iterable[Catalyst], rules_version: str = "") -> int:
        """Append a version for each new or changed catalyst.  Returns the number appended.
        Versions are written in as-of order; recording an earlier as-of after a later one is
        refused (the ledger is a timeline, not a cache)."""
        hist = self.history(ticker)
        last_as_of = max((v["as_of"] for vs in hist.values() for v in vs), default="")
        if last_as_of and as_of < last_as_of:
            raise ValueError(f"ledger for {ticker} already holds {last_as_of}; refusing to record {as_of}")
        self.root.mkdir(parents=True, exist_ok=True)
        n = 0
        with self._file(ticker).open("a") as fh:
            for c in catalysts:
                prev = hist.get(c.catalyst_id, [])
                fp = _fingerprint(c)
                if prev and prev[-1]["fingerprint"] == fp:
                    continue
                fh.write(json.dumps({"catalyst_id": c.catalyst_id, "ticker": ticker, "as_of": as_of,
                                     "version": len(prev) + 1, "stage": c.stage.value,
                                     "first_public_at": c.first_public_at.isoformat() if c.first_public_at else None,
                                     "rules_version": rules_version, "fingerprint": fp,
                                     "record": to_jsonable(c)}, default=str) + "\n")
                n += 1
        return n

    def stage_dates(self, ticker: str) -> dict[str, dict[str, str]]:
        """{catalyst_id: {stage: first as-of in that stage}}."""
        out: dict[str, dict[str, str]] = {}
        for cid, vs in self.history(ticker).items():
            for v in vs:
                out.setdefault(cid, {}).setdefault(v["stage"], v["as_of"])
        return out
