"""Event detection and matched-control sampling over one symbol's bars.

Look-ahead discipline
---------------------
Every field that decides whether a bar is an event is computed from bars at
index <= i:

  * ``ret_24h``  = close[i] / close[i-1] - 1
  * ``rsi``      = Wilder RSI over close[:i+1]
  * ``levels``   = S/R proxy over bars [i-119 .. i]

Fields read from bars after i (``exit_price``, ``window_low``, the next
open) are named ``fwd_*`` or live on the outcome side, never in the gate.
``tests/test_no_lookahead.py`` truncates each frame at the event bar and
re-derives the gate fields to prove it.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .corpora import SymbolBars
from .levels import compute_levels, rsi_series
from .spec import (
    BASIS_EVENT_CLOSE,
    BASIS_NEXT_OPEN,
    CONTROLS_PER_EVENT,
    HORIZONS,
    LEVEL_WINDOW,
    RANDOM_SEED,
    REBUY_STRENGTH,
    REBUY_STRENGTH_FALLBACK,
    RESISTANCE_COUNT_MAX,
    RSI_MIN,
    RSI_PERIOD,
    SPIKE_RETURN_MIN,
)


@dataclass
class Observation:
    """One decision point — an event or one of its matched controls."""

    market: str
    segment: str
    group: str
    symbol: str
    kind: str  # "event" | "control"
    index: int
    session: str
    prev_close: float
    close: float
    ret_24h: float
    rsi: float
    resistance_count: int
    named_resistance_count: int
    rebuy_price: float | None
    rebuy_price_moderate_plus: float | None
    nearest_support_any: float | None
    limit_locked: int
    next_open_limit_locked: int
    gap_next_open: float | None
    forward: dict[str, dict[str, Any]] = field(default_factory=dict)


def _max_lookahead(horizons: tuple[int, ...]) -> int:
    """Bars needed after the event bar so both bases and both horizons exist."""
    return max(horizons) + 1


def eligible_mask(
    frame: pd.DataFrame,
    horizons: tuple[int, ...] = HORIZONS,
    level_window: int = LEVEL_WINDOW,
) -> np.ndarray:
    """Bars with a full trailing window, a real 24h step, and a full forward window."""
    n = len(frame)
    mask = np.zeros(n, dtype=bool)
    if n == 0:
        return mask
    first = level_window - 1
    last = n - 1 - _max_lookahead(horizons)
    if last < first:
        return mask
    mask[first : last + 1] = True
    return mask & frame["contiguous_prev"].to_numpy(dtype=bool)


def _forward_block(
    frame: pd.DataFrame,
    i: int,
    horizon: int,
    basis: str,
    rebuy_price: float | None,
) -> dict[str, Any]:
    """Decision price, exit price and the rebid fill test for one basis.

    The rebid fill window is bars ``i+1 .. i+horizon`` under both bases: the
    trim lands at ``close[i]`` (continuous markets) or ``open[i+1]`` (gap
    basis), and in either case bar ``i+1`` onwards is live for the rebid.

    KR limit-up locked bars are removed from the fill window: their whole
    range collapses to the ceiling, so a printed touch is not a reachable
    price.  ``fill_used_locked_bar`` records whether that removal changed the
    answer, so the exclusion's real cost is measurable rather than assumed.
    """
    opens = frame["open"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    locked = frame["limit_locked"].to_numpy(dtype=int)

    if basis == BASIS_EVENT_CLOSE:
        p0 = float(closes[i])
        pt = float(closes[i + horizon])
        executable = locked[i] != 1
    elif basis == BASIS_NEXT_OPEN:
        p0 = float(opens[i + 1])
        pt = float(opens[i + 1 + horizon])
        executable = locked[i + 1] != 1
    else:  # pragma: no cover - callers pass literals from spec
        raise ValueError(f"unsupported basis {basis!r}")

    window = slice(i + 1, i + horizon + 1)
    window_lows = lows[window]
    window_locked_up = locked[window] == 1
    low_all = float(window_lows.min()) if window_lows.size else None
    tradable = window_lows[~window_locked_up]
    low_tradable = float(tradable.min()) if tradable.size else None

    fill_used_locked_bar = False
    if rebuy_price is not None and low_all is not None:
        would_fill_all = low_all <= rebuy_price
        would_fill_tradable = low_tradable is not None and low_tradable <= rebuy_price
        fill_used_locked_bar = bool(would_fill_all and not would_fill_tradable)

    return {
        "basis": basis,
        "horizon": horizon,
        "p0": p0,
        "exit_price": pt,
        "window_low": low_tradable,
        "window_low_including_locked": low_all,
        "trim_executable": bool(executable),
        "fill_used_locked_bar": fill_used_locked_bar,
    }


def _observation(
    bars: SymbolBars,
    frame: pd.DataFrame,
    i: int,
    kind: str,
    rsi: np.ndarray,
    ret: np.ndarray,
    price_decimals: int | None,
    level_window: int,
) -> Observation | None:
    window = frame.iloc[i - level_window + 1 : i + 1]
    try:
        view = compute_levels(window, price_decimals=price_decimals)
    except ValueError:
        # A degenerate window (zero traded volume across 120 sessions) cannot
        # produce levels.  Dropping the bar is the fail-closed answer; the
        # count is reported.
        return None

    opens = frame["open"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    locked = frame["limit_locked"].to_numpy(dtype=int)
    rebuy_price = view.nearest_support(REBUY_STRENGTH)
    rebuy_price_moderate_plus = view.nearest_support(REBUY_STRENGTH_FALLBACK)

    observation = Observation(
        market=bars.market,
        segment=bars.segment,
        group=bars.group,
        symbol=bars.symbol,
        kind=kind,
        index=i,
        session=str(frame["session"].iloc[i].date()),
        prev_close=float(closes[i - 1]),
        close=float(closes[i]),
        ret_24h=float(ret[i]),
        rsi=float(rsi[i]),
        resistance_count=view.resistance_count,
        named_resistance_count=view.named_resistance_count,
        rebuy_price=rebuy_price,
        rebuy_price_moderate_plus=rebuy_price_moderate_plus,
        nearest_support_any=view.nearest_support(None),
        limit_locked=int(locked[i]),
        next_open_limit_locked=int(locked[i + 1]),
        gap_next_open=float(opens[i + 1] / closes[i] - 1.0),
    )
    for horizon in HORIZONS:
        for basis in (BASIS_EVENT_CLOSE, BASIS_NEXT_OPEN):
            block = _forward_block(frame, i, horizon, basis, rebuy_price)
            fallback = _forward_block(
                frame, i, horizon, basis, rebuy_price_moderate_plus
            )
            block["fill_used_locked_bar_moderate_plus"] = fallback[
                "fill_used_locked_bar"
            ]
            observation.forward[f"{basis}:{horizon}"] = block
    return observation


def _symbol_rng(market: str, symbol: str) -> np.random.Generator:
    """Deterministic per-symbol stream.

    ``zlib.crc32`` rather than ``hash()``: Python's string hash is salted per
    process, which would make control sampling irreproducible across runs.
    """
    key = f"{market}:{symbol}".encode()
    return np.random.default_rng(RANDOM_SEED + zlib.crc32(key))


@dataclass
class ScanResult:
    observations: list[Observation]
    bars_scanned: int
    eligible_bars: int
    prefilter_hits: int
    events: int
    events_dropped_degenerate_window: int
    events_rejected_resistance: int
    control_candidates: int


def scan_symbol(
    bars: SymbolBars,
    *,
    price_decimals: int | None = None,
    level_window: int = LEVEL_WINDOW,
) -> ScanResult:
    """Find the symbol's events and draw its matched controls."""
    frame = bars.frame
    n = len(frame)
    empty = ScanResult([], n, 0, 0, 0, 0, 0, 0)
    if n < level_window + _max_lookahead(HORIZONS) + 1:
        return empty

    closes = frame["close"]
    ret = (closes / closes.shift(1) - 1.0).to_numpy(dtype=float)
    rsi = rsi_series(closes, period=RSI_PERIOD).to_numpy(dtype=float)

    eligible = eligible_mask(frame, level_window=level_window)
    if not eligible.any():
        return empty

    with np.errstate(invalid="ignore"):
        prefilter = eligible & (ret >= SPIKE_RETURN_MIN) & (rsi >= RSI_MIN)
    prefilter_indices = np.flatnonzero(prefilter)

    observations: list[Observation] = []
    degenerate = 0
    rejected_resistance = 0
    for i in prefilter_indices:
        observation = _observation(
            bars, frame, int(i), "event", rsi, ret, price_decimals, level_window
        )
        if observation is None:
            degenerate += 1
            continue
        # Gate on the looser "named" rule.  The strict arm
        # (``resistance_count == 0``) is a strict subset of what passes here,
        # so one scan serves both arms and the report slices between them.
        if observation.named_resistance_count > RESISTANCE_COUNT_MAX:
            rejected_resistance += 1
            continue
        observations.append(observation)

    events = len(observations)
    control_pool = np.flatnonzero(eligible & ~prefilter)
    if events and control_pool.size:
        rng = _symbol_rng(bars.market, bars.symbol)
        wanted = min(events * CONTROLS_PER_EVENT, control_pool.size)
        picks = rng.choice(control_pool, size=wanted, replace=False)
        for i in np.sort(picks):
            observation = _observation(
                bars, frame, int(i), "control", rsi, ret, price_decimals, level_window
            )
            if observation is not None:
                observations.append(observation)

    return ScanResult(
        observations=observations,
        bars_scanned=n,
        eligible_bars=int(eligible.sum()),
        prefilter_hits=int(prefilter.sum()),
        events=events,
        events_dropped_degenerate_window=degenerate,
        events_rejected_resistance=rejected_resistance,
        control_candidates=int(control_pool.size),
    )
