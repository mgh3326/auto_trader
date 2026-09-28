"""H5 plugin for the ROB-993 StrategyPlugin.evaluate interface.

Only the complete 4h bars emitted by the ROB-993 collector are admitted.
The entry price is supplied later by a fresh executable book quote.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.services.brokers.binance.demo_strategy_loop.strategy import (
    Signal,
    StrategyPlugin,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS, Bar4h

IDENTITY = "H5-LS-ENV-v1"
UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DEMO_URL = "https://demo-fapi.binance.com"
_KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class H5Bar4h(Bar4h):
    """ROB-993 bar interface with exact exchange OHLC text for H5 comparisons."""

    price_text: tuple[str, str, str, str]


def bar_price_text(bar: Bar4h, field: str) -> str:
    if isinstance(bar, H5Bar4h):
        return bar.price_text[("open", "high", "low", "close").index(field)]
    return str(getattr(bar, field))


def bar_price(bar: Bar4h, field: str) -> Decimal:
    return Decimal(bar_price_text(bar, field))


def assert_h5_demo_url(base_url: str) -> None:
    """Exact origin is required, including scheme and no URL decorations."""
    if base_url != DEMO_URL:
        raise ValueError("H5 requires the exact Futures Demo origin")


def make_signal_key(
    symbol: str, decision_ts: int, side: str, signal_price_text: str
) -> str:
    if symbol not in UNIVERSE or side not in {"BUY", "SELL"}:
        raise ValueError("invalid H5 signal identity")
    if not signal_price_text or Decimal(signal_price_text) <= 0:
        raise ValueError("invalid H5 signal price")
    instant = dt.datetime.fromtimestamp(decision_ts / 1000, tz=dt.UTC)
    minute = instant.astimezone(_KST).strftime("%Y-%m-%d %H:%M")
    return f"{symbol}|{minute}|{side}|{signal_price_text}"


@dataclass(frozen=True)
class H5Strategy(StrategyPlugin):
    """No context filter beyond the registered symmetric 20-bar formula."""

    base_url: str = DEMO_URL
    strategy_id: str = IDENTITY

    def __post_init__(self) -> None:
        assert_h5_demo_url(self.base_url)
        if self.strategy_id != IDENTITY:
            raise ValueError("H5 plugin identity cannot be changed")

    def validate_client(self, client: object) -> None:
        """Bind the plugin to the H5-only Demo adapter before any IO."""
        from .client import assert_h5_client

        assert_h5_demo_url(self.base_url)
        assert_h5_client(client)

    def evaluate(
        self,
        bars_4h_multi_symbol: Mapping[str, tuple[Bar4h, ...]],
        *,
        decision_ts: int,
    ) -> Signal | None:
        """ROB-993 StrategyPlugin.evaluate(bars_4h_multi_symbol, decision_ts).

        A 21-bar contiguous segment is needed: 20 prior bars and completed t.
        Comparison uses the literal <=/> and >=/< boundaries of H5 v1.1.
        """
        assert_h5_demo_url(self.base_url)
        for symbol in UNIVERSE:
            bars = bars_4h_multi_symbol.get(symbol, ())
            if len(bars) < 21:
                continue
            window = bars[-21:]
            if window[-1].close_ts != decision_ts:
                continue
            if any(
                bar.close_ts != decision_ts - (20 - i) * FOUR_HOUR_MS
                or (i > 0 and bar.is_segment_start)
                for i, bar in enumerate(window)
            ):
                continue
            prior = window[:-1]
            current = window[-1]
            close = bar_price(current, "close")
            low20 = min(bar_price(bar, "low") for bar in prior)
            high20 = max(bar_price(bar, "high") for bar in prior)
            prev_high = bar_price(prior[-1], "high")
            prev_low = bar_price(prior[-1], "low")
            if close <= low20 * Decimal("1.05") and close > prev_high:
                side = "BUY"
            elif close >= high20 * Decimal("0.95") and close < prev_low:
                side = "SELL"
            else:
                continue
            return Signal(
                symbol=symbol,
                side=side,
                decision_ts=decision_ts,
                strategy_id=IDENTITY,
                reason="h5_20bar_envelope_recovery",
            )
        return None


def evaluate_all(
    plugin: H5Strategy,
    bars_by_symbol: Mapping[str, tuple[Bar4h, ...]],
    *,
    decision_ts: int,
) -> tuple[Signal, ...]:
    """Call the ROB-993 one-signal plugin separately for each H5 symbol."""
    found: list[Signal] = []
    for symbol in UNIVERSE:
        signal = plugin.evaluate(
            {symbol: bars_by_symbol.get(symbol, ())}, decision_ts=decision_ts
        )
        if signal is not None:
            found.append(signal)
    return tuple(found)
