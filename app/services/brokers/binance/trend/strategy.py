"""TREND-TSMOM-v1 plugin for the ROB-993 ``StrategyPlugin.evaluate`` interface.

Signal: the sign of ``close_t / close_{t-L} - 1`` where both closes are complete
UTC 4h bars (``t - L`` is exactly ``L`` days earlier on the same 4h phase). A
missing bar is NO_SIGNAL: nothing is forward-filled or substituted.

The plugin only reads bars handed to it; it performs no I/O and never sees an
account, quote or order. ``lookback_days`` and ``hard_stop_fraction`` are
required constructor arguments with no defaults: CT-5 fixes their values when
it seals the manifest, so no code path may silently pick one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.services.brokers.binance.demo_strategy_loop.strategy import (
    Signal,
    StrategyPlugin,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS, Bar4h

from .constants import (
    ANNUALISATION_DAYS,
    EWMA_SPAN_DAYS,
    IDENTITY,
    LOCKED_LIMITS,
    LOOKBACK_CHOICES,
)

DAY_MS = 6 * FOUR_HOUR_MS
Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class TrendBar4h(Bar4h):
    """ROB-993 bar plus the exchange's exact decimal close text."""

    close_text: str


def bar_close(bar: Bar4h) -> Decimal:
    """Exact close: the exchange text when present, never a float round trip."""
    if isinstance(bar, TrendBar4h):
        return Decimal(bar.close_text)
    return Decimal(str(bar.close))


def bar_close_text(bar: Bar4h) -> str:
    return bar.close_text if isinstance(bar, TrendBar4h) else str(bar.close)


def required_close_times(decision_ts: int, lookback_days: int) -> tuple[int, ...]:
    """Every 4h close the signal and the volatility estimate need.

    ``t - k days`` for ``k = 0..EWMA_SPAN_DAYS`` (21 closes, 20 daily returns)
    plus ``t - L days``. Fetching exactly these buckets keeps a 120 day
    lookback at 22 small requests per symbol instead of ~170k minute rows.
    """
    needed = {decision_ts - k * DAY_MS for k in range(EWMA_SPAN_DAYS + 1)}
    needed.add(decision_ts - lookback_days * DAY_MS)
    return tuple(sorted(needed))


def _by_close(bars: tuple[Bar4h, ...]) -> dict[int, Bar4h]:
    return {bar.close_ts: bar for bar in bars}


def momentum_sign(
    bars: tuple[Bar4h, ...], *, decision_ts: int, lookback_days: int
) -> Side | None:
    """``BUY`` for a positive, ``SELL`` for a negative lookback return.

    ``None`` (NO_SIGNAL) when either bar is missing or the return is exactly 0.
    """
    index = _by_close(bars)
    now_bar = index.get(decision_ts)
    past_bar = index.get(decision_ts - lookback_days * DAY_MS)
    if now_bar is None or past_bar is None:
        return None
    ratio = bar_close(now_bar) / bar_close(past_bar) - 1
    if ratio > 0:
        return "BUY"
    if ratio < 0:
        return "SELL"
    return None


def realized_vol_annual(bars: tuple[Bar4h, ...], *, decision_ts: int) -> Decimal | None:
    """EWMA (span 20 days) of daily simple returns, annualised by sqrt(365).

    Daily samples are the 4h closes at ``t - k days``. Zero-mean second moment
    with weights ``(1 - alpha)^j`` (newest first). ``None`` when any sample is
    missing or the estimate is not positive: the entry cannot be sized.
    """
    index = _by_close(bars)
    closes: list[Decimal] = []
    for k in range(EWMA_SPAN_DAYS + 1):
        bar = index.get(decision_ts - k * DAY_MS)
        if bar is None:
            return None
        closes.append(bar_close(bar))
    alpha = Decimal(2) / Decimal(EWMA_SPAN_DAYS + 1)
    weight = Decimal(1)
    total_weight = Decimal(0)
    second_moment = Decimal(0)
    for newer, older in zip(closes[:-1], closes[1:], strict=True):
        daily = newer / older - 1
        second_moment += weight * daily * daily
        total_weight += weight
        weight *= 1 - alpha
    variance = second_moment / total_weight
    sigma = variance.sqrt() * Decimal(ANNUALISATION_DAYS).sqrt()
    return sigma if sigma > 0 else None


@dataclass(frozen=True, kw_only=True)
class TrendStrategy(StrategyPlugin):
    lookback_days: int
    hard_stop_fraction: Decimal
    strategy_id: str = IDENTITY

    def __post_init__(self) -> None:
        if self.strategy_id != IDENTITY:
            raise ValueError("trend plugin identity cannot be changed")
        if type(self.lookback_days) is not int or (
            self.lookback_days not in LOOKBACK_CHOICES
        ):
            raise ValueError(f"lookback_days must be one of {LOOKBACK_CHOICES}")
        stop = self.hard_stop_fraction
        if (
            not isinstance(stop, Decimal)
            or not stop.is_finite()
            or not Decimal(0) < stop < Decimal(1)
        ):
            raise ValueError("hard_stop_fraction must be a Decimal in (0, 1)")

    def hard_stop_price(self, *, side: Side, entry_price: Decimal) -> Decimal:
        factor = (
            1 - self.hard_stop_fraction
            if side == "BUY"
            else 1 + (self.hard_stop_fraction)
        )
        return entry_price * factor

    def evaluate_symbol(
        self, symbol: str, bars: tuple[Bar4h, ...], *, decision_ts: int
    ) -> Signal | None:
        if symbol not in LOCKED_LIMITS.universe:
            return None
        side = momentum_sign(
            bars, decision_ts=decision_ts, lookback_days=self.lookback_days
        )
        # A signal that cannot be sized is not a signal.
        if side is None or realized_vol_annual(bars, decision_ts=decision_ts) is None:
            return None
        return Signal(
            symbol=symbol,
            side=side,
            decision_ts=decision_ts,
            strategy_id=IDENTITY,
            reason=f"tsmom_l{self.lookback_days}",
        )

    def evaluate(
        self,
        bars_4h_multi_symbol: Mapping[str, tuple[Bar4h, ...]],
        *,
        decision_ts: int,
    ) -> Signal | None:
        """ROB-993 contract: the first universe symbol with a signal, else None."""
        for symbol in LOCKED_LIMITS.universe:
            signal = self.evaluate_symbol(
                symbol, bars_4h_multi_symbol.get(symbol, ()), decision_ts=decision_ts
            )
            if signal is not None:
                return signal
        return None


def evaluate_all(
    plugin: TrendStrategy,
    bars_by_symbol: Mapping[str, tuple[Bar4h, ...]],
    *,
    decision_ts: int,
) -> tuple[Signal, ...]:
    """One signal per universe symbol that has one (the interface returns one)."""
    found: list[Signal] = []
    for symbol in LOCKED_LIMITS.universe:
        signal = plugin.evaluate_symbol(
            symbol, bars_by_symbol.get(symbol, ()), decision_ts=decision_ts
        )
        if signal is not None:
            found.append(signal)
    return tuple(found)
