from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.brokers.binance.dto import BinanceBookTicker, BinanceKlineRow
from app.services.paper_cohort.market_snapshot import (
    CanonicalSnapshotCapture,
    CanonicalSnapshotPayload,
    SnapshotCaptureRequest,
)

pytestmark = pytest.mark.unit

CAPTURED_AT = datetime(2026, 7, 14, 12, 35, 30, tzinfo=UTC)


def candle(symbol: str, minute: int) -> BinanceKlineRow:
    opened = datetime(2026, 7, 14, 12, minute, tzinfo=UTC)
    return BinanceKlineRow(
        symbol=symbol,
        interval="1m",
        open_time=opened,
        close_time=opened + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        base_volume=Decimal("10"),
        quote_volume=Decimal("1000"),
        trade_count=10,
        taker_buy_base_volume=Decimal("5"),
        taker_buy_quote_volume=Decimal("500"),
        is_closed=True,
    )


def ticker(symbol: str, fetched_at: datetime | None = None) -> BinanceBookTicker:
    return BinanceBookTicker(
        symbol=symbol,
        bid_price=Decimal("100"),
        bid_qty=Decimal("2"),
        ask_price=Decimal("101"),
        ask_qty=Decimal("3"),
        fetched_at=fetched_at or CAPTURED_AT + timedelta(milliseconds=100),
    )


class FakePublicClient:
    def __init__(self) -> None:
        self.candles = {
            symbol: [candle(symbol, minute) for minute in (32, 33, 34)]
            for symbol in ("BTCUSDT", "ETHUSDT")
        }
        self.tickers = {symbol: ticker(symbol) for symbol in ("BTCUSDT", "ETHUSDT")}
        self.calls: list[tuple[object, ...]] = []

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> list[BinanceKlineRow]:
        self.calls.append(("klines", symbol, interval, start_time, end_time, limit))
        return self.candles[symbol]

    async def book_ticker(self, symbol: str) -> BinanceBookTicker:
        self.calls.append(("book_ticker", symbol))
        return self.tickers[symbol]


def request() -> SnapshotCaptureRequest:
    return SnapshotCaptureRequest(
        snapshot_id="snapshot-1",
        cohort_id="cohort-1",
        run_id="run-1",
        round_decision_id="round-1",
        required_lookback=3,
        max_capture_skew_ms=2000,
        max_ticker_age_ms=5000,
    )


@pytest.mark.asyncio
async def test_capture_is_exact_ordered_reproducible_and_json_roundtrips() -> None:
    first_client = FakePublicClient()
    second_client = FakePublicClient()
    clock_values = iter([CAPTURED_AT, CAPTURED_AT + timedelta(milliseconds=200)])
    capture = CanonicalSnapshotCapture(first_client, clock=lambda: next(clock_values))

    first = await capture.capture(request())
    second_clock = iter([CAPTURED_AT, CAPTURED_AT + timedelta(milliseconds=200)])
    second = await CanonicalSnapshotCapture(
        second_client, clock=lambda: next(second_clock)
    ).capture(request())

    assert first == second
    assert first.schema_id == "canonical_market_snapshot.v1"
    assert first.source == "binance_public_spot"
    assert first.host == "https://api.binance.com"
    assert first.interval == "1m"
    assert tuple(item.symbol for item in first.symbols) == ("BTCUSDT", "ETHUSDT")
    assert tuple(row.open_time for row in first.symbols[0].candles) == tuple(
        sorted(row.open_time for row in first.symbols[0].candles)
    )
    assert len(first.content_hash) == 64
    assert first.content_hash == first.recomputed_content_hash()
    roundtripped = CanonicalSnapshotPayload.model_validate_json(first.model_dump_json())
    assert roundtripped == first
    assert roundtripped.recomputed_content_hash() == first.content_hash
    expected_end = datetime(2026, 7, 14, 12, 35, tzinfo=UTC) - timedelta(microseconds=1)
    assert all(
        call[4] == expected_end for call in first_client.calls if call[0] == "klines"
    )


@pytest.mark.asyncio
async def test_content_hash_changes_for_any_canonical_content_change() -> None:
    client = FakePublicClient()
    clocks = iter([CAPTURED_AT, CAPTURED_AT + timedelta(milliseconds=200)])
    original = await CanonicalSnapshotCapture(
        client, clock=lambda: next(clocks)
    ).capture(request())
    changed = original.model_copy(
        update={
            "symbols": (
                original.symbols[0].model_copy(
                    update={
                        "ticker": original.symbols[0].ticker.model_copy(
                            update={"bid_qty": "2.00000001"}
                        )
                    }
                ),
                original.symbols[1],
            )
        }
    )

    assert changed.recomputed_content_hash() != original.content_hash


__all__ = ["CAPTURED_AT", "FakePublicClient", "candle", "request", "ticker"]
