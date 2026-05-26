"""ROB-317 — event-driven signal over a rolling candle buffer."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.brokers.binance.demo_scalping.contract import ReasonCode
from app.services.brokers.binance.demo_scalping_ws.signal import EventDrivenSignal
from app.services.brokers.binance.ws_client import KlineEvent


def _kline(
    close: str, *, high: str | None = None, low: str | None = None, minute: int = 0
) -> KlineEvent:
    base = dt.datetime(2026, 5, 26, 10, 0, tzinfo=dt.UTC) + dt.timedelta(minutes=minute)
    c = Decimal(close)
    return KlineEvent(
        symbol="XRPUSDT",
        interval="1m",
        open_time=base,
        close_time=base + dt.timedelta(seconds=59),
        open=c,
        high=Decimal(high) if high else c,
        low=Decimal(low) if low else c,
        close=c,
        base_volume=Decimal("1000"),
        quote_volume=Decimal("515"),
        trade_count=42,
        is_closed=True,
    )


def test_insufficient_history_until_buffer_fills() -> None:
    sig = EventDrivenSignal(product="usdm_futures", symbol="XRPUSDT")
    decision = sig.ingest_kline(_kline("0.50", minute=0))
    assert decision.has_entry is False
    assert ReasonCode.INSUFFICIENT_HISTORY in decision.reason_codes


def test_long_breakout_fires_after_enough_candles() -> None:
    sig = EventDrivenSignal(product="usdm_futures", symbol="XRPUSDT")
    # 24 flat candles at 0.50 (range 0.49-0.51), then a breakout close above
    # the prior 20-bar high with a rising fast SMA.
    last = None
    for m in range(24):
        last = sig.ingest_kline(_kline("0.50", high="0.51", low="0.49", minute=m))
    assert last.has_entry is False  # 24 < 25 needed
    fill = sig.ingest_kline(_kline("0.50", high="0.51", low="0.49", minute=24))
    assert fill.has_entry is False  # 25th: flat, no breakout
    breakout = sig.ingest_kline(_kline("0.60", high="0.60", low="0.50", minute=25))
    assert breakout.has_entry is True
    assert breakout.side == "BUY"
    assert ReasonCode.ENTER_LONG_BREAKOUT in breakout.reason_codes


def test_buffer_is_bounded() -> None:
    sig = EventDrivenSignal(product="usdm_futures", symbol="XRPUSDT", max_candles=30)
    for m in range(100):
        sig.ingest_kline(_kline("0.50", minute=m))
    assert sig.candle_count == 30
