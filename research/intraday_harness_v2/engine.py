"""Pure v2 execution semantics.

The only fill-producing operation is ``run``. It derives the fill bar from a
signal's close timestamp; callers cannot supply a bar or a fill price.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class IncompleteReason(StrEnum):
    SIGNAL_BAR_MISSING = "SIGNAL_BAR_MISSING"
    SIGNAL_BAR_INCOMPLETE = "SIGNAL_BAR_INCOMPLETE"
    NEXT_BAR_MISSING = "NEXT_BAR_MISSING"
    NEXT_BAR_INCOMPLETE = "NEXT_BAR_INCOMPLETE"


def _price(value: Decimal | int | str | float) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    complete: bool = True

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.close_time.tzinfo is None:
            raise ValueError("bar timestamps must be timezone-aware")
        if self.close_time <= self.open_time:
            raise ValueError("bar close_time must be after open_time")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or value <= 0:
                raise ValueError(f"{name} must be a positive Decimal")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise ValueError("OHLC bounds are invalid")


@dataclass(frozen=True, slots=True)
class BarSeries:
    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        keys = [(bar.symbol, bar.open_time) for bar in self.bars]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate symbol/open_time bars are forbidden")

    @classmethod
    def from_iterable(cls, bars: Iterable[Bar]) -> BarSeries:
        return cls(tuple(sorted(bars, key=lambda bar: (bar.symbol, bar.open_time))))

    def _by_key(self) -> Mapping[tuple[str, datetime], Bar]:
        return {(bar.symbol, bar.open_time): bar for bar in self.bars}

    def get(self, symbol: str, open_time: datetime) -> Bar | None:
        return self._by_key().get((symbol, open_time))


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    bar_close_time: datetime
    side: Side
    quantity: Decimal
    bar_interval: timedelta

    def __post_init__(self) -> None:
        if self.bar_close_time.tzinfo is None or self.bar_interval <= timedelta(0):
            raise ValueError("signal close time must be aware and interval positive")
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise ValueError("quantity must be a positive Decimal")


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    side: Side
    signal_bar_close_time: datetime
    fill_bar_open_time: datetime | None
    quantity: Decimal
    fill_price: Decimal | None
    fee: Decimal
    slippage: Decimal
    incomplete_reason: IncompleteReason | None

    @property
    def incomplete(self) -> bool:
        return self.incomplete_reason is not None


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    fills: tuple[Fill, ...]
    status: str
    incomplete_count: int
    fee_total: Decimal
    slippage_total: Decimal


def _incomplete(signal: Signal, reason: IncompleteReason) -> Fill:
    return Fill(
        signal.symbol,
        signal.side,
        signal.bar_close_time,
        None,
        signal.quantity,
        None,
        Decimal(0),
        Decimal(0),
        reason,
    )


def run(
    signals: Iterable[Signal],
    bars: BarSeries,
    *,
    fee_bps: Decimal | int | str | float,
    slippage_bps: Decimal | int | str | float,
) -> ExecutionSummary:
    """Execute market/all-or-none signals at the strictly next bar's open.

    Fee and slippage are accounting fields, never folded into ``fill_price``.
    A zero fee/slippage model is explicit at the call site, never a default.
    """
    fee_rate = _price(fee_bps) / Decimal(10_000)
    slippage_rate = _price(slippage_bps) / Decimal(10_000)
    if fee_rate < 0 or slippage_rate < 0:
        raise ValueError("fee_bps and slippage_bps must be non-negative")
    fills: list[Fill] = []
    for signal in signals:
        signal_bar = bars.get(
            signal.symbol, signal.bar_close_time - signal.bar_interval
        )
        if signal_bar is None:
            fills.append(_incomplete(signal, IncompleteReason.SIGNAL_BAR_MISSING))
            continue
        if not signal_bar.complete:
            fills.append(_incomplete(signal, IncompleteReason.SIGNAL_BAR_INCOMPLETE))
            continue
        next_bar = bars.get(signal.symbol, signal.bar_close_time)
        if next_bar is None:
            fills.append(_incomplete(signal, IncompleteReason.NEXT_BAR_MISSING))
            continue
        if not next_bar.complete:
            fills.append(_incomplete(signal, IncompleteReason.NEXT_BAR_INCOMPLETE))
            continue
        notional = next_bar.open * signal.quantity
        fills.append(
            Fill(
                signal.symbol,
                signal.side,
                signal.bar_close_time,
                next_bar.open_time,
                signal.quantity,
                next_bar.open,
                notional * fee_rate,
                notional * slippage_rate,
                None,
            )
        )
    result = tuple(fills)
    return ExecutionSummary(
        fills=result,
        status="INCOMPLETE" if any(fill.incomplete for fill in result) else "COMPLETE",
        incomplete_count=sum(fill.incomplete for fill in result),
        fee_total=sum((fill.fee for fill in result), Decimal(0)),
        slippage_total=sum((fill.slippage for fill in result), Decimal(0)),
    )
