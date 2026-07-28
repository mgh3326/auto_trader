"""Canonical deterministic corpus for the only currently runnable H4 identity.

This is the production-side counterpart of the original AC27 test fixture.
It has no clock, random, network, database, broker, or performance surface.
Every value is a closed-form input used to reproduce the source hashes pinned
by :mod:`run_manifest`.

SINGLE-HISTORY INVARIANT (the ROB-1062 v1 defect this module was rewritten
to remove).  Every price here is a function of ABSOLUTE UTC calendar time and
of nothing else.  The v1 generator keyed prices off the offset from the
caller's ``window_start_ms``, so each walk-forward fold restarted the same
path at ``day_index == 0``: all eight folds observed a byte-identical price
series, one calendar day carried a different price in every fold, and the
128-cell terminal artifact collapsed to 16 distinct observations replicated
eight times.  ``structural_incomplete == 0`` was arithmetically true and
informationally empty.

Consequence to preserve: for any two windows that overlap on a calendar day,
the bar delivered for that day MUST be identical.  ``close_for`` therefore
takes an absolute day number from :func:`absolute_day_index` and MUST NEVER
be handed a window-relative offset.  ``tests/test_synthetic_corpus_
single_history.py`` pins this against every fold pair.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

import provider_evidence as pe
from daily_bars import DailyBar, SpotMinute
from pit_universe_alpaca import SymbolEligibility, UniverseSnapshot

__all__ = [
    "DAY_MS",
    "N_SYMBOLS",
    "absolute_day_index",
    "build_bars_by_symbol",
    "close_for",
    "make_minute_bars_provider",
    "make_universe_snapshot_provider",
    "symbol_names",
]

DAY_MS = 86_400_000
N_SYMBOLS = 20


def symbol_names(n: int = N_SYMBOLS) -> list[str]:
    return [f"SYM{index:02d}/USD" for index in range(n)]


def absolute_day_index(timestamp_ms: int) -> int:
    """Return the absolute UTC day number ``timestamp_ms`` falls in.

    This is the ONLY sanctioned way to obtain a ``close_for`` day index.  It
    is anchored on the Unix epoch, never on a caller-supplied window, so the
    same calendar day always maps to the same index in every fold.
    """

    if type(timestamp_ms) is not int:
        raise TypeError("timestamp_ms must be a built-in int")
    return timestamp_ms // DAY_MS


def close_for(symbol_index: int, day_index: int) -> float:
    """Return one deterministic, quantized synthetic close.

    ``day_index`` MUST be an absolute UTC day number from
    :func:`absolute_day_index`.  Passing a window-relative offset silently
    reintroduces the v1 fold-replication defect: the series would restart at
    every window start and no fold would observe a distinct period.
    """

    amplitude = 0.15 + 0.02 * (symbol_index % 5)
    period = 80 + 15 * (symbol_index % 6)
    phase = (symbol_index * 0.7) % (2 * math.pi)
    return round(
        100.0 * (1.0 + amplitude * math.sin(2 * math.pi * day_index / period + phase)),
        10,
    )


def build_bars_by_symbol(
    *,
    window_start_ms: int,
    num_days: int,
    n_symbols: int = N_SYMBOLS,
) -> dict[str, tuple[DailyBar, ...]]:
    """Slice ``num_days`` of the single absolute-time history from
    ``window_start_ms``.

    ``window_start_ms`` selects WHICH days are delivered; it never shifts the
    price of a given day.  Two overlapping windows return identical bars on
    their shared days.
    """

    if window_start_ms % DAY_MS:
        raise ValueError("window_start_ms must be UTC midnight aligned")
    if type(num_days) is not int or num_days <= 0:
        raise ValueError("num_days must be a positive built-in int")

    result: dict[str, tuple[DailyBar, ...]] = {}
    for symbol_index, symbol in enumerate(symbol_names(n_symbols)):
        bars = []
        for offset in range(num_days):
            day_start = window_start_ms + offset * DAY_MS
            close = close_for(symbol_index, absolute_day_index(day_start))
            bars.append(
                DailyBar(
                    day_start_ms=day_start,
                    day_end_ms=day_start + DAY_MS,
                    open=close,
                    high=close * 1.001,
                    low=close * 0.999,
                    close=close,
                    volume=1_000.0,
                    minute_count_observed=1440,
                    imputed_minutes=0,
                    max_gap_minutes=0,
                    gap_in_last_60min=False,
                    is_valid=True,
                    # Window-relative on purpose: this marks the first bar of
                    # THIS delivered slice, it is not a price input.
                    is_segment_start=(offset == 0),
                )
            )
        result[symbol] = tuple(bars)
    return result


def make_universe_snapshot_provider(
    n_symbols: int = N_SYMBOLS,
) -> Callable[[int], pe.UniverseSnapshotEvidence]:
    names = tuple(sorted(symbol_names(n_symbols)))

    def provider(decision_ts_ms: int) -> pe.UniverseSnapshotEvidence:
        per_symbol = tuple(
            SymbolEligibility(
                symbol=symbol,
                eligible=True,
                fail_reason=None,
                pit_history_days=999,
                listing_proxy_source="alpaca_first_daily_proxy",
            )
            for symbol in names
        )
        snapshot = UniverseSnapshot(
            decision_ts_ms=decision_ts_ms,
            eligible_symbols=names,
            per_symbol=per_symbol,
            n_t=len(names),
            meets_min_universe_size=True,
        )
        return pe.bind_universe_snapshot(
            snapshot,
            source_as_of_ts_ms=decision_ts_ms,
        )

    return provider


def make_minute_bars_provider(
    *,
    n_symbols: int = N_SYMBOLS,
) -> Callable[[str, int], pe.MinuteBarsEvidence]:
    """Build the minute provider for the single absolute-time history.

    Takes no window: the price at a decision timestamp is fixed by that
    timestamp's absolute calendar day, so the same instant yields the same
    minute bars in every fold.
    """

    index_by_symbol: Mapping[str, int] = {
        symbol: index for index, symbol in enumerate(symbol_names(n_symbols))
    }

    def provider(symbol: str, decision_ts_ms: int) -> pe.MinuteBarsEvidence:
        symbol_index = index_by_symbol[symbol]
        price = close_for(symbol_index, absolute_day_index(decision_ts_ms))
        bars = (
            SpotMinute(
                open_time_ms=decision_ts_ms + 60_000,
                open=price,
                high=price * 1.001,
                low=price * 0.999,
                close=price,
                volume=1.0,
            ),
            SpotMinute(
                open_time_ms=decision_ts_ms + 120_000,
                open=price,
                high=price * 1.001,
                low=price * 0.999,
                close=price,
                volume=1.0,
            ),
        )
        return pe.bind_minute_bars(
            symbol=symbol,
            signal_ts_ms=decision_ts_ms,
            bars=bars,
        )

    return provider
