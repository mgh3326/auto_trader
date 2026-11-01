"""Research call budget for broker market-data APIs.

A broker limits calls per app key, not per process: the serving API, the
trading sessions and this collector all draw from the same per-key window on
the broker's side. The app's own limiters (``app.core.async_rate_limiter`` for
KIS, ``toss.rate_limiter`` for Toss) are per process by default, so they cannot
stop a separate research process from adding its calls on top of serving's.

This module therefore caps research at a fixed *share* of each broker's
documented per-key limit: at most ``max_calls`` research calls in any sliding
window of ``per_seconds``. Serving keeps the rest of the window whatever
research does. The share is checked against ``BROKER_LIMITS`` and may not
exceed ``MAX_RESEARCH_SHARE``; nothing can configure research to take the whole
window.

The window is shared by every research process on the host when a state file
is given (the CLI keeps one per broker under ``<root>/.budget/``, guarded by
``flock``): the KR and US collectors, and a manual rerun, never add up to more
than one share.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from research.us_universe.store import exclusive_lock

# Documented per-key limits, as configured in this repo:
#   KIS real host: daily chart FHKST03010100 20 calls / 1 s
#     (app/core/config.py KIS rate limits; the fallback is 19/s)
#   Toss: MARKET_DATA_CHART group 5 calls / 1 s
#     (app/services/brokers/toss/rate_limiter.py)
BROKER_LIMITS: Mapping[str, tuple[int, float]] = {
    "kis": (20, 1.0),
    "toss": (5, 1.0),
}
# Research may never take more than this share of a broker's window.
MAX_RESEARCH_SHARE = 0.25
# Defaults: 10% of KIS, 20% of the Toss chart group.
DEFAULT_BUDGETS: Mapping[str, tuple[int, float]] = {
    "kis": (2, 1.0),
    "toss": (1, 1.0),
}


class BudgetError(ValueError):
    """A budget is malformed or exceeds the research share of its broker."""


@dataclass(frozen=True)
class BudgetSpec:
    broker: str
    max_calls: int
    per_seconds: float

    def text(self) -> str:
        return f"{self.max_calls}/{self.per_seconds:g}s"


def check_spec(broker: str, max_calls: int, per_seconds: float) -> BudgetSpec:
    if broker not in BROKER_LIMITS:
        raise BudgetError(
            f"unknown broker {broker!r}; expected {sorted(BROKER_LIMITS)}"
        )
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
        raise BudgetError(f"{broker}: max_calls must be a positive integer")
    if (
        isinstance(per_seconds, bool)
        or not isinstance(per_seconds, (int, float))
        or not math.isfinite(per_seconds)
        or per_seconds <= 0
    ):
        raise BudgetError(f"{broker}: per_seconds must be finite and positive")
    limit_calls, limit_seconds = BROKER_LIMITS[broker]
    # Any broker window (length limit_seconds <= per_seconds) then holds at
    # most max_calls research calls, so this bound is the whole proof.
    if per_seconds < limit_seconds:
        raise BudgetError(
            f"{broker}: per_seconds {per_seconds:g} is shorter than the broker "
            f"window {limit_seconds:g}s"
        )
    cap = math.floor(MAX_RESEARCH_SHARE * limit_calls)
    if max_calls > cap:
        raise BudgetError(
            f"{broker}: research budget {max_calls}/{per_seconds:g}s exceeds "
            f"{MAX_RESEARCH_SHARE:.0%} of the broker limit "
            f"{limit_calls}/{limit_seconds:g}s (at most {cap} per window)"
        )
    return BudgetSpec(broker, max_calls, float(per_seconds))


def parse_budgets(text: str) -> dict[str, BudgetSpec]:
    """``kis=2/1,toss=1/1`` -> specs; unspecified brokers keep their default."""
    specs = {b: BudgetSpec(b, *v) for b, v in DEFAULT_BUDGETS.items()}
    for item in (t.strip() for t in text.split(",")):
        if not item:
            continue
        try:
            broker, rest = item.split("=", 1)
            calls_text, seconds_text = rest.split("/", 1)
            calls, seconds = int(calls_text), float(seconds_text)
        except ValueError as exc:
            raise BudgetError(
                f"budget {item!r}: expected BROKER=CALLS/SECONDS"
            ) from exc
        specs[broker.strip()] = check_spec(broker.strip(), calls, seconds)
    return specs


class ResearchBudget:
    """At most ``max_calls`` acquisitions in any window of ``per_seconds``.

    ``acquire`` blocks (through the injected ``sleep``) until a slot is free.
    Thread-safe; with ``state_path`` the window is also shared across processes.
    """

    def __init__(
        self,
        spec: BudgetSpec,
        *,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        check_spec(spec.broker, spec.max_calls, spec.per_seconds)
        self.spec = spec
        self._state_path = state_path
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._memory: list[float] = []
        self.acquired = 0
        # grant instants of this instance (bounded; diagnostics and tests)
        self.grants: deque[float] = deque(maxlen=4096)

    def _load(self) -> list[float]:
        if self._state_path is None:
            return self._memory
        try:
            data = json.loads(self._state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        stamps = data.get("stamps") if isinstance(data, dict) else None
        if not isinstance(stamps, list):
            return []
        return [float(s) for s in stamps if isinstance(s, (int, float))]

    def _store(self, stamps: list[float]) -> None:
        if self._state_path is None:
            self._memory = stamps
            return
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        tmp.write_text(json.dumps({"stamps": stamps}), "utf-8")
        tmp.replace(self._state_path)

    def _try(self) -> float:
        """Take a slot (returns 0) or return the seconds to wait."""
        now = self._clock()
        stamps = [s for s in self._load() if now - s < self.spec.per_seconds]
        # A stamp in the future (another process's skewed clock) still counts.
        if len(stamps) < self.spec.max_calls:
            stamps.append(now)
            self._store(stamps)
            self.grants.append(now)
            self.acquired += 1
            return 0.0
        self._store(stamps)
        oldest = min(stamps)
        return max(oldest + self.spec.per_seconds - now, 1e-3)

    def _attempt(self) -> float:
        if self._state_path is None:
            return self._try()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self._state_path.with_suffix(".lock")):
            return self._try()

    def try_acquire(self) -> bool:
        """Take a slot now if one is free; never waits."""
        with self._lock:
            return self._attempt() == 0.0

    def acquire(self) -> None:
        with self._lock:
            while True:
                wait = self._attempt()
                if wait == 0.0:
                    return
                self._sleep(wait)
