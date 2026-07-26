"""ROB-1062 H4 (Run A SS14.5, AC11-AC16) — the historical fill model: a
conservative proxy for BBO-based fills, built only from Binance public-spot
1-minute bars (``daily_bars.SpotMinute`` — H1's own raw-minute type, reused
here rather than re-declared).

    entry: reference = signal-time Binance close (C_t); limit_cap =
        ref * 1.005. If either of the next 2 one-minute bars' OPEN is
        <= limit_cap, fill at the FIRST such bar's open. Otherwise
        ENTRY_UNFILLED.
    exit: symmetric — limit_floor = ref * 0.995; fill at the first
        qualifying bar's open (open >= limit_floor). Otherwise EXIT_UNFILLED.

Spread/fee/venue-mismatch are NEVER mixed into the fill price (AC13) — they
are deducted separately, per cost scenario, in ``pnl_views.py``. The fill
price is ALWAYS a later bar's ``open`` (AC14: same-close fill is
structurally impossible here — there is no code path that returns
``reference_close`` itself as a fill price; every returned ``fill_price`` is
traceable to one of the two window bars via ``fill_bar_offset``).

Documented, explicit implementation choice (SS14.5 specifies the 2-bar
window and both multipliers but does not pin an exact clock anchor for
"이후" / "after" the signal): the window's first bar is the one-minute bar
whose ``open_time_ms`` equals the DECISION timestamp itself (the instant the
order actually reaches the book), and the second is the immediately
following minute. This is flagged here (and in the H4 completion report),
mirroring H3's own precedent for genuinely open implementation choices not
pinned by the authority doc (EMA seed rule, sigma20 annualization,
greedy-continue cash allocation) — it is a documented choice, not a
reinterpretation of a specified VALUE (the ``1.005``/``0.995`` multipliers
and the "2 bars" window count themselves are taken verbatim and are never
altered here).

A missing expected minute bar (data gap) is NEVER treated as an unfilled
attempt — that would fabricate a market outcome from absent data (AC15).
It is reported as ``FILL_WINDOW_INCOMPLETE``, a distinct, structural,
"incomplete" classification the caller must never fold into a performance
verdict.

Partial fills are not modeled (AC16) — every fill is all-or-nothing at the
qualifying bar's open. This simplification is recorded on every
``FillOutcome`` via ``partial_fill_modeled=False`` (a literal constant, not a
per-call decision) so no downstream consumer can mistake this backtest proxy
for a partial-fill-aware simulation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from daily_bars import SpotMinute

__all__ = [
    "ENTRY_LIMIT_CAP_MULTIPLIER",
    "EXIT_LIMIT_FLOOR_MULTIPLIER",
    "FILL_WINDOW_MINUTE_COUNT",
    "MINUTE_MS",
    "FillOutcome",
    "FillReason",
    "model_entry_fill",
    "model_exit_fill",
]

MINUTE_MS = 60_000
FILL_WINDOW_MINUTE_COUNT = 2

# Run A SS14.5 — verbatim, never altered.
ENTRY_LIMIT_CAP_MULTIPLIER = 1.005
EXIT_LIMIT_FLOOR_MULTIPLIER = 0.995

FillReason = Literal[
    "FILLED", "ENTRY_UNFILLED", "EXIT_UNFILLED", "FILL_WINDOW_INCOMPLETE"
]

_PARTIAL_FILL_MODELED = False  # AC16 — literal, never a per-call parameter.


def _float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    return value


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


@dataclass(frozen=True)
class FillOutcome:
    filled: bool
    fill_price: float | None
    fill_bar_offset: int | None  # 1 or 2 -- which window bar filled it
    reason: FillReason
    partial_fill_modeled: bool = _PARTIAL_FILL_MODELED

    def __post_init__(self) -> None:
        if self.partial_fill_modeled is not _PARTIAL_FILL_MODELED:
            raise ValueError(
                "partial_fill_modeled is a fixed literal, never overridden"
            )
        if self.filled:
            if self.reason != "FILLED":
                raise ValueError("a filled outcome must carry reason='FILLED'")
            if self.fill_price is None or self.fill_bar_offset is None:
                raise ValueError(
                    "a filled outcome must carry fill_price and fill_bar_offset"
                )
            _float(self.fill_price, "fill_price")
            if self.fill_bar_offset not in (1, 2):
                raise ValueError("fill_bar_offset must be 1 or 2")
        else:
            if self.reason == "FILLED":
                raise ValueError("reason='FILLED' requires filled=True")
            if self.fill_price is not None or self.fill_bar_offset is not None:
                raise ValueError(
                    "an unfilled outcome must carry fill_price=None and "
                    "fill_bar_offset=None"
                )


def _window_bars(
    decision_ts_ms: int, minute_bars_after_signal: Sequence[SpotMinute]
) -> list[SpotMinute | None]:
    _int(decision_ts_ms, "decision_ts_ms")
    for bar in minute_bars_after_signal:
        if type(bar) is not SpotMinute:
            raise TypeError("minute_bars_after_signal must contain SpotMinute")
    by_ts = {b.open_time_ms: b for b in minute_bars_after_signal}
    expected = [decision_ts_ms + i * MINUTE_MS for i in range(FILL_WINDOW_MINUTE_COUNT)]
    return [by_ts.get(ts) for ts in expected]


def model_entry_fill(
    *,
    decision_ts_ms: int,
    reference_close: float,
    minute_bars_after_signal: Sequence[SpotMinute],
) -> FillOutcome:
    """Entry side: fill at the first qualifying bar's open where
    ``open <= reference_close * ENTRY_LIMIT_CAP_MULTIPLIER``, scanning the
    2-bar window in order (never bar 3+, AC14/AC16 boundary discipline)."""
    _float(reference_close, "reference_close")
    limit_cap = reference_close * ENTRY_LIMIT_CAP_MULTIPLIER
    window = _window_bars(decision_ts_ms, minute_bars_after_signal)
    if any(bar is None for bar in window):
        return FillOutcome(
            filled=False,
            fill_price=None,
            fill_bar_offset=None,
            reason="FILL_WINDOW_INCOMPLETE",
        )
    for offset, bar in enumerate(window, start=1):
        assert bar is not None  # narrowed by the `any(... is None)` check above
        if bar.open <= limit_cap:
            return FillOutcome(
                filled=True,
                fill_price=bar.open,
                fill_bar_offset=offset,
                reason="FILLED",
            )
    return FillOutcome(
        filled=False, fill_price=None, fill_bar_offset=None, reason="ENTRY_UNFILLED"
    )


def model_exit_fill(
    *,
    decision_ts_ms: int,
    reference_close: float,
    minute_bars_after_signal: Sequence[SpotMinute],
) -> FillOutcome:
    """Exit side, symmetric to entry: fill at the first qualifying bar's
    open where ``open >= reference_close * EXIT_LIMIT_FLOOR_MULTIPLIER``."""
    _float(reference_close, "reference_close")
    limit_floor = reference_close * EXIT_LIMIT_FLOOR_MULTIPLIER
    window = _window_bars(decision_ts_ms, minute_bars_after_signal)
    if any(bar is None for bar in window):
        return FillOutcome(
            filled=False,
            fill_price=None,
            fill_bar_offset=None,
            reason="FILL_WINDOW_INCOMPLETE",
        )
    for offset, bar in enumerate(window, start=1):
        assert bar is not None
        if bar.open >= limit_floor:
            return FillOutcome(
                filled=True,
                fill_price=bar.open,
                fill_bar_offset=offset,
                reason="FILLED",
            )
    return FillOutcome(
        filled=False, fill_price=None, fill_bar_offset=None, reason="EXIT_UNFILLED"
    )
