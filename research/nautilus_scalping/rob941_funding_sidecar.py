"""ROB-941 (AC8) — PIT-safe funding-rate sidecar query API.

Wraps parsed ``funding_oi_archive.FundingRow`` sequences with point-in-time
query semantics only — no PnL/dollar computation happens here (that belongs to
the execution engine consuming this data):

- ``last_known_rate(ts_ms)``: the entry-gate contract. Never returns a row whose
  ``calc_time`` is in the caller's future; a row becomes visible AT (not before)
  its own ``calc_time``, matching ``funding_oi_archive``'s "known only at/after
  calc_time" invariant.
- ``realized_crossings(entry_ms, exit_ms)``: the PnL contract. Returns only the
  funding events that actually occur strictly inside a held position's window
  (``entry_ms <= calc_time < exit_ms``) — realized crossings only, never a
  future event the position hasn't reached yet.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass

from funding_oi_archive import FundingRow


@dataclass(frozen=True)
class FundingSidecar:
    symbol: str
    rows: tuple[FundingRow, ...]  # sorted ascending by calc_time, conflict-free

    @classmethod
    def from_rows(cls, symbol: str, rows: Iterable[FundingRow]) -> FundingSidecar:
        ordered = tuple(sorted(rows, key=lambda r: r.calc_time))
        for a, b in zip(ordered, ordered[1:], strict=False):
            if a.calc_time == b.calc_time and a != b:
                raise ValueError(
                    f"{symbol}: conflicting duplicate funding rows at calc_time={a.calc_time}"
                )
        return cls(symbol=symbol, rows=ordered)

    def last_known_rate(self, ts_ms: int) -> FundingRow | None:
        """Most recent row with ``calc_time <= ts_ms``; ``None`` before any data."""
        times = [r.calc_time for r in self.rows]
        idx = bisect.bisect_right(times, ts_ms) - 1
        return self.rows[idx] if idx >= 0 else None

    def realized_crossings(
        self, entry_ts_ms: int, exit_ts_ms: int
    ) -> tuple[FundingRow, ...]:
        """Funding events realized while held: ``entry_ts_ms <= calc_time < exit_ts_ms``."""
        if exit_ts_ms <= entry_ts_ms:
            return ()
        times = [r.calc_time for r in self.rows]
        lo = bisect.bisect_left(times, entry_ts_ms)
        hi = bisect.bisect_left(times, exit_ts_ms)
        return self.rows[lo:hi]
