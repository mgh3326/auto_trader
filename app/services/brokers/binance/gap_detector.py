"""ROB-285 — Gap detector for kline streams.

On WS reconnect, compares the last persisted closed candle's ``open_time``
against ``now()`` to determine missed candles. Returns ``GapDecision`` with
``needs_fill``, ``since``, and ``expected_count``. Pure-function — no I/O.

Task 12 orchestration consumes this output and calls ``RestBackfiller``
when ``needs_fill`` is true, and ``CryptoInstrumentHealthService`` when
the backfiller raises ``BinanceBackfillCapExceeded``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

_INTERVAL_TO_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}


@dataclass(frozen=True, slots=True)
class GapDecision:
    needs_fill: bool
    since: dt.datetime | None
    expected_count: int


def detect_gap(
    *,
    last_closed: dt.datetime | None,
    interval: str = "1m",
    now: dt.datetime | None = None,
) -> GapDecision:
    """Compute the kline gap (if any) for one instrument.

    ``last_closed`` is the ``open_time`` of the most recently persisted
    closed candle (i.e., the start of the most recent bucket whose
    close_time is in the past). When ``None``, no fill is needed —
    no baseline to compare against (the caller decides whether to do
    cold-start backfill or skip).
    """
    n = now or dt.datetime.now(tz=dt.UTC)
    sec = _INTERVAL_TO_SECONDS[interval]
    if last_closed is None:
        return GapDecision(needs_fill=False, since=None, expected_count=0)
    elapsed = (n - last_closed).total_seconds()
    # ``expected`` = number of full buckets that have *completed* since
    # last_closed. ``elapsed // sec`` includes the bucket that contains
    # last_closed itself; -1 because the current in-progress bucket is
    # not yet closed.
    expected = int(elapsed // sec) - 1
    if expected <= 0:
        return GapDecision(needs_fill=False, since=None, expected_count=0)
    return GapDecision(
        needs_fill=True,
        since=last_closed + dt.timedelta(seconds=sec),
        expected_count=expected,
    )
