"""ROB-307 PR1 — tests for the observe-only scalping runner.

The runner wires market data + deterministic signal + risk envelope +
a (pre-loaded) ledger snapshot into a single observe-only record. It
**never executes**: ``action`` is always ``observe_only`` even when the
signal+risk would permit entry. Market data is faked; the ledger
snapshot is injected as a value object, so the runner test is pure.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import (
    LedgerSnapshot,
    ReasonCode,
)
from app.services.brokers.binance.demo_scalping.market_data import BookTicker
from app.services.brokers.binance.demo_scalping.runner import evaluate_symbol
from app.services.brokers.binance.demo_scalping.signal import Candle

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _candles(closes: list[int], *, now_ms: int = _NOW_MS) -> list[Candle]:
    n = len(closes)
    out: list[Candle] = []
    for i, c in enumerate(closes):
        close = Decimal(str(c))
        close_time = now_ms - (n - 1 - i) * 60_000  # last candle closes at now
        out.append(
            Candle(
                open_time_ms=close_time - 59_999,
                open=close,
                high=close,
                low=close,
                close=close,
                close_time_ms=close_time,
            )
        )
    return out


_UPTREND = list(range(100, 130))  # monotonic up -> long breakout
_DOWNTREND = list(range(200, 170, -1))  # monotonic down -> short breakdown
_FLAT = [100] * 30  # no trend, no breakout -> no signal

_TIGHT_BOOK = BookTicker(bid=Decimal("129.99"), ask=Decimal("130.00"))
_WIDE_BOOK = BookTicker(bid=Decimal("100"), ask=Decimal("102"))  # 200 bps spread


class _FakeMarketData:
    def __init__(self, candles: list[Candle], book: BookTicker) -> None:
        self._candles = candles
        self._book = book
        self.klines_calls: list[tuple[str, str]] = []

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        self.klines_calls.append((product, symbol))
        return self._candles

    async def fetch_book_ticker(self, product, symbol):
        return self._book


def _healthy_snapshot() -> LedgerSnapshot:
    return LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=0,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )


@pytest.mark.asyncio
async def test_uptrend_healthy_would_enter_long_but_action_is_observe_only() -> None:
    record = await evaluate_symbol(
        product="spot",
        symbol="XRPUSDT",
        market_data=_FakeMarketData(_candles(_UPTREND), _TIGHT_BOOK),
        ledger_snapshot=_healthy_snapshot(),
        now=_NOW,
    )
    assert record.action == "observe_only"  # PR1 never executes
    assert record.has_entry is True
    assert record.side == "BUY"
    assert record.would_enter is True
    assert record.risk_allowed is True
    assert record.tp_price is not None and record.sl_price is not None
    assert ReasonCode.ENTER_LONG_BREAKOUT in record.signal_reason_codes


@pytest.mark.asyncio
async def test_open_lifecycle_blocks_would_enter() -> None:
    snap = LedgerSnapshot(
        has_open_lifecycle_for_symbol=True,
        global_open_lifecycle_count=1,
        orders_today=1,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )
    record = await evaluate_symbol(
        product="spot",
        symbol="XRPUSDT",
        market_data=_FakeMarketData(_candles(_UPTREND), _TIGHT_BOOK),
        ledger_snapshot=snap,
        now=_NOW,
    )
    assert record.has_entry is True
    assert record.would_enter is False
    assert ReasonCode.OPEN_LIFECYCLE_EXISTS in record.risk_reason_codes
    assert record.action == "observe_only"


@pytest.mark.asyncio
async def test_no_signal_when_flat() -> None:
    record = await evaluate_symbol(
        product="spot",
        symbol="XRPUSDT",
        market_data=_FakeMarketData(_candles(_FLAT), _TIGHT_BOOK),
        ledger_snapshot=_healthy_snapshot(),
        now=_NOW,
    )
    assert record.has_entry is False
    assert record.would_enter is False
    assert ReasonCode.NO_SIGNAL in record.signal_reason_codes


@pytest.mark.asyncio
async def test_wide_spread_blocks_entry() -> None:
    record = await evaluate_symbol(
        product="spot",
        symbol="XRPUSDT",
        market_data=_FakeMarketData(_candles(_UPTREND), _WIDE_BOOK),
        ledger_snapshot=_healthy_snapshot(),
        now=_NOW,
    )
    assert record.has_entry is True
    assert record.would_enter is False
    assert ReasonCode.SPREAD_TOO_WIDE in record.risk_reason_codes


@pytest.mark.asyncio
async def test_futures_allows_short_on_downtrend() -> None:
    record = await evaluate_symbol(
        product="usdm_futures",
        symbol="XRPUSDT",
        market_data=_FakeMarketData(
            _candles(_DOWNTREND),
            BookTicker(bid=Decimal("170.99"), ask=Decimal("171.00")),
        ),
        ledger_snapshot=_healthy_snapshot(),
        now=_NOW,
    )
    assert record.has_entry is True
    assert record.side == "SELL"
    assert ReasonCode.ENTER_SHORT_BREAKDOWN in record.signal_reason_codes


@pytest.mark.asyncio
async def test_evidence_dict_is_json_safe_and_observe_only() -> None:
    import json

    record = await evaluate_symbol(
        product="spot",
        symbol="XRPUSDT",
        market_data=_FakeMarketData(_candles(_UPTREND), _TIGHT_BOOK),
        ledger_snapshot=_healthy_snapshot(),
        now=_NOW,
    )
    payload = record.to_evidence_dict()
    assert payload["action"] == "observe_only"
    assert payload["symbol"] == "XRPUSDT"
    assert payload["product"] == "spot"
    # Must serialize without error (Decimals -> str).
    text = json.dumps(payload)
    assert "observe_only" in text
