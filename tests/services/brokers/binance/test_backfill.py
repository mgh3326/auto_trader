"""ROB-285 — REST kline backfill with bounded caps."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.backfill import (
    BackfillCaps,
    BackfillResult,
    RestBackfiller,
)
from app.services.brokers.binance.dto import BinanceKlineRow
from app.services.brokers.binance.errors import BinanceBackfillCapExceeded


class _FakeRest:
    """In-memory fake REST client for deterministic tests."""

    def __init__(self, *, all_klines: list[BinanceKlineRow]) -> None:
        self.all = all_klines
        self.calls = 0

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: dt.datetime,
        end_time: dt.datetime | None = None,
        limit: int,
    ) -> list[BinanceKlineRow]:
        self.calls += 1
        # Return up to ``limit`` klines whose open_time >= start_time.
        slice_ = [k for k in self.all if k.open_time >= start_time][:limit]
        return slice_


def _mk_kline(t: dt.datetime) -> BinanceKlineRow:
    return BinanceKlineRow(
        symbol="BTCUSDT",
        interval="1m",
        open_time=t,
        close_time=t + dt.timedelta(minutes=1) - dt.timedelta(milliseconds=1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        base_volume=Decimal("0"),
        quote_volume=None,
        trade_count=None,
        taker_buy_base_volume=None,
        taker_buy_quote_volume=None,
        is_closed=True,
    )


@pytest.mark.asyncio
async def test_backfill_within_caps_returns_all_klines() -> None:
    start = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
    klines = [_mk_kline(start + dt.timedelta(minutes=i)) for i in range(50)]
    rest = _FakeRest(all_klines=klines)
    bf = RestBackfiller(
        rest=rest,
        caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    result = await bf.backfill(symbol="BTCUSDT", interval="1m", since=start)
    assert isinstance(result, BackfillResult)
    assert len(result.klines) == 50
    assert rest.calls == 1


@pytest.mark.asyncio
async def test_backfill_paginates_with_starttime_anchor() -> None:
    start = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
    # 2500 candles → 3 pages at 1000/page.
    klines = [_mk_kline(start + dt.timedelta(minutes=i)) for i in range(2500)]
    rest = _FakeRest(all_klines=klines)
    bf = RestBackfiller(
        rest=rest,
        caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    result = await bf.backfill(symbol="BTCUSDT", interval="1m", since=start)
    assert len(result.klines) == 2500
    assert rest.calls == 3


@pytest.mark.asyncio
async def test_backfill_cap_exceeded_raises() -> None:
    start = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
    klines = [_mk_kline(start + dt.timedelta(minutes=i)) for i in range(8000)]
    rest = _FakeRest(all_klines=klines)
    bf = RestBackfiller(
        rest=rest,
        caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    with pytest.raises(BinanceBackfillCapExceeded) as exc_info:
        await bf.backfill(symbol="BTCUSDT", interval="1m", since=start)
    # Exception carries a message identifying the cap that tripped.
    assert exc_info.value.args
    assert "max_candles" in str(exc_info.value) or "max_requests" in str(
        exc_info.value
    )


def test_caps_from_env_default_values(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_KLINE_BACKFILL_MAX_CANDLES", raising=False)
    monkeypatch.delenv("BINANCE_KLINE_BACKFILL_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("BINANCE_KLINE_BACKFILL_PAGE_SIZE", raising=False)
    caps = BackfillCaps.from_env()
    assert caps.max_candles == 5000
    assert caps.max_requests == 10
    assert caps.page_size == 1000


def test_caps_from_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_KLINE_BACKFILL_MAX_CANDLES", "7777")
    monkeypatch.setenv("BINANCE_KLINE_BACKFILL_MAX_REQUESTS", "20")
    monkeypatch.setenv("BINANCE_KLINE_BACKFILL_PAGE_SIZE", "500")
    caps = BackfillCaps.from_env()
    assert caps.max_candles == 7777
    assert caps.max_requests == 20
    assert caps.page_size == 500
