"""Hard spend / call / token reservations and a content-addressed cache.

A call must reserve its estimated cost BEFORE it is made; a reservation that
would exceed any limit raises ``BudgetExceeded`` and no call happens.  With
``enabled=False`` (the default) every reservation raises ``BudgetDisabled``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class BudgetDisabled(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Reservation:
    rid: int
    tokens: int
    cost_usd: float
    committed: bool = False


class Budget:
    def __init__(self, enabled: bool = False, max_calls: int = 0, max_tokens: int = 0,
                 max_spend_usd: float = 0.0, usd_per_1k_tokens: float = 0.0, cache_dir: Optional[str] = None):
        self.enabled = enabled
        self.max_calls, self.max_tokens, self.max_spend = max_calls, max_tokens, max_spend_usd
        self.rate = usd_per_1k_tokens
        self.calls = self.tokens = 0
        self.spend = 0.0
        self._lock = threading.Lock()
        self._next = 0
        self._mem: dict[str, str] = {}
        self._dir = Path(cache_dir) if cache_dir else None

    @classmethod
    def from_config(cls, cfg: dict) -> "Budget":
        b = (cfg or {}).get("budget", {})
        return cls(enabled=bool((cfg or {}).get("llm", {}).get("enabled", False)) and bool(b.get("enabled", False)),
                   max_calls=int(b.get("max_calls", 0)), max_tokens=int(b.get("max_tokens", 0)),
                   max_spend_usd=float(b.get("max_spend_usd", 0.0)),
                   usd_per_1k_tokens=float(b.get("usd_per_1k_tokens", 0.0)), cache_dir=b.get("cache_dir"))

    def reserve(self, est_tokens: int) -> Reservation:
        if not self.enabled:
            raise BudgetDisabled("paid calls disabled (llm.enabled and budget.enabled must both be true)")
        cost = est_tokens / 1000.0 * self.rate
        with self._lock:
            if self.calls + 1 > self.max_calls:
                raise BudgetExceeded(f"call limit {self.max_calls} reached")
            if self.tokens + est_tokens > self.max_tokens:
                raise BudgetExceeded(f"token limit {self.max_tokens} would be exceeded")
            if self.spend + cost > self.max_spend + 1e-12:
                raise BudgetExceeded(f"spend limit ${self.max_spend:.2f} would be exceeded")
            self.calls += 1
            self.tokens += est_tokens
            self.spend += cost
            self._next += 1
            return Reservation(self._next, est_tokens, cost)

    def commit(self, res: Reservation, actual_tokens: int) -> None:
        """Reconcile with actual usage; overruns are charged and can block later calls."""
        with self._lock:
            delta = actual_tokens - res.tokens
            self.tokens += delta
            self.spend += delta / 1000.0 * self.rate
            res.committed = True

    # -- cache ---------------------------------------------------------------

    @staticmethod
    def _key(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def cache_get(self, prompt: str) -> Optional[str]:
        k = self._key(prompt)
        if k in self._mem:
            return self._mem[k]
        if self._dir and (self._dir / f"{k}.json").exists():
            v = json.loads((self._dir / f"{k}.json").read_text())["response"]
            self._mem[k] = v
            return v
        return None

    def cache_put(self, prompt: str, response: str) -> None:
        k = self._key(prompt)
        self._mem[k] = response
        if self._dir:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / f"{k}.json").write_text(json.dumps({"response": response}))

    def summary(self) -> dict:
        return {"enabled": self.enabled, "calls": self.calls, "tokens": self.tokens, "spend_usd": round(self.spend, 4),
                "limits": {"calls": self.max_calls, "tokens": self.max_tokens, "spend_usd": self.max_spend}}
