"""ROB-1062 H4 — a deterministic, closed-form SYNTHETIC corpus fixture for
runner integration tests and the golden digest. NEVER real Binance archive
data (H1 AC25's real one-time collection has not happened and is not this
module's concern) — every price is a pure function of a symbol index and a
day index, no randomness, no wall clock, byte-identical across repeated
calls.

20 symbols (matching H2's sealed_effective_n) with varying sinusoidal
amplitude/period/phase so AP-A1 DATS sees both persistent-uptrend (never
exits within the window) and up-then-down (closes within the window)
symbols, and AP-A2 WCM-B sees genuine cross-sectional rank variation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from daily_bars import DailyBar, SpotMinute
from pit_universe_alpaca import SymbolEligibility, UniverseSnapshot

DAY_MS = 86_400_000
N_SYMBOLS = 20


def symbol_names(n: int = N_SYMBOLS) -> list[str]:
    return [f"SYM{idx:02d}/USD" for idx in range(n)]


def close_for(symbol_idx: int, day_index: int) -> float:
    """Deterministic sinusoidal close price, always positive.

    The test corpus is quantized at generation so CPython/libm last-bit
    differences cannot turn the same semantic fixture into different source
    corpus hashes on macOS and Linux. This affects this synthetic fixture
    only; runner calculations and gate comparisons retain full precision.
    """
    amplitude = 0.15 + 0.02 * (symbol_idx % 5)
    period = 80 + 15 * (symbol_idx % 6)
    phase = (symbol_idx * 0.7) % (2 * math.pi)
    return round(
        100.0 * (1.0 + amplitude * math.sin(2 * math.pi * day_index / period + phase)),
        10,
    )


def build_bars_by_symbol(
    *, window_start_ms: int, num_days: int, n_symbols: int = N_SYMBOLS
) -> dict[str, tuple[DailyBar, ...]]:
    """One complete, gap-free, all-valid ``DailyBar`` series per symbol
    spanning ``num_days`` whole UTC days starting at ``window_start_ms``
    (which must be UTC-midnight aligned)."""
    if window_start_ms % DAY_MS:
        raise ValueError("window_start_ms must be UTC midnight aligned")
    result: dict[str, tuple[DailyBar, ...]] = {}
    names = symbol_names(n_symbols)
    for idx, symbol in enumerate(names):
        bars = []
        for day_index in range(num_days):
            close = close_for(idx, day_index)
            day_start = window_start_ms + day_index * DAY_MS
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
                    is_segment_start=(day_index == 0),
                )
            )
        result[symbol] = tuple(bars)
    return result


def make_universe_snapshot_provider(
    n_symbols: int = N_SYMBOLS,
) -> callable:
    names = tuple(sorted(symbol_names(n_symbols)))

    def provider(decision_ts_ms: int) -> UniverseSnapshot:
        per_symbol = tuple(
            SymbolEligibility(
                symbol=s,
                eligible=True,
                fail_reason=None,
                pit_history_days=999,
                listing_proxy_source="alpaca_first_daily_proxy",
            )
            for s in names
        )
        return UniverseSnapshot(
            decision_ts_ms=decision_ts_ms,
            eligible_symbols=names,
            per_symbol=per_symbol,
            n_t=len(names),
            meets_min_universe_size=True,
        )

    return provider


def make_minute_bars_provider(
    *, window_start_ms: int, n_symbols: int = N_SYMBOLS
) -> callable:
    idx_by_symbol: Mapping[str, int] = {
        s: i for i, s in enumerate(symbol_names(n_symbols))
    }

    def provider(symbol: str, decision_ts_ms: int) -> Sequence[SpotMinute]:
        idx = idx_by_symbol[symbol]
        day_index = (decision_ts_ms - window_start_ms) // DAY_MS
        price = close_for(idx, day_index)
        m1 = decision_ts_ms + 60_000
        m2 = decision_ts_ms + 120_000
        return [
            SpotMinute(
                open_time_ms=m1,
                open=price,
                high=price * 1.001,
                low=price * 0.999,
                close=price,
                volume=1.0,
            ),
            SpotMinute(
                open_time_ms=m2,
                open=price,
                high=price * 1.001,
                low=price * 0.999,
                close=price,
                volume=1.0,
            ),
        ]

    return provider
