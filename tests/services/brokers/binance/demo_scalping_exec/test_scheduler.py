"""ROB-307 follow-up — tests for the default-OFF scalping tick orchestration.

One tick = run the deterministic signal per allowlisted symbol and place a
bounded-monitor entry (which always exits flat in-run). Kill-switch
(``enabled=False`` → no-op), failure-only alerting (errors collected, tick
continues). Broker/market data faked; no network, no real orders.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from app.services.brokers.binance.demo_scalping.signal import Candle
from app.services.brokers.binance.demo_scalping_exec.scheduler import (
    run_scalping_tick,
)

_NOW = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _uptrend() -> list[Candle]:
    closes = list(range(100, 130))
    n = len(closes)
    return [
        Candle(
            open_time_ms=_NOW_MS - (n - 1 - i) * 60_000 - 59_999,
            open=Decimal(c),
            high=Decimal(c),
            low=Decimal(c),
            close=Decimal(c),
            close_time_ms=_NOW_MS - (n - 1 - i) * 60_000,
        )
        for i, c in enumerate(closes)
    ]


def _flat() -> list[Candle]:
    return [
        Candle(
            open_time_ms=_NOW_MS - (29 - i) * 60_000 - 59_999,
            open=Decimal(100),
            high=Decimal(100),
            low=Decimal(100),
            close=Decimal(100),
            close_time_ms=_NOW_MS - (29 - i) * 60_000,
        )
        for i in range(30)
    ]


class _Result:
    def __init__(self, status, *, reason_codes=()):
        self.status = status
        self.reason_codes = tuple(reason_codes)


class _FakeExecutor:
    def __init__(
        self, *, entry_status="reconciled", raise_on_entry=False, reason_codes=()
    ):
        self.entered: list[str] = []
        self.markets: list = []  # ROB-315 0c: captured market conditions
        self._entry_status = entry_status
        self._raise_on_entry = raise_on_entry
        self._reason_codes = reason_codes

    async def execute_monitored(self, intent, *, confirm, **kwargs):
        self.markets.append(kwargs.get("market"))
        if self._raise_on_entry:
            raise RuntimeError("boom")
        self.entered.append(intent.symbol)
        return _Result(self._entry_status, reason_codes=self._reason_codes)


class _FakeMarketData:
    def __init__(self, candles, *, book=None):
        self._candles = candles
        self._book = book

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        return self._candles

    async def fetch_book_ticker(self, product, symbol):
        # Tight default spread (~2 bps) so the D4 gates don't block entries.
        from app.services.brokers.binance.demo_scalping.market_data import BookTicker

        return self._book or BookTicker(bid=Decimal("100"), ask=Decimal("100.02"))


def _executors(spot, fut):
    return {"spot": spot, "usdm_futures": fut}


@pytest.mark.asyncio
async def test_disabled_is_noop() -> None:
    spot, fut = _FakeExecutor(), _FakeExecutor()
    summary = await run_scalping_tick(
        executors=_executors(spot, fut),
        market_data=_FakeMarketData(_uptrend()),
        symbols=["XRPUSDT"],
        products=["spot"],
        now=_NOW,
        confirm=True,
        enabled=False,
    )
    assert summary.status == "disabled"
    assert spot.entered == []


@pytest.mark.asyncio
async def test_enters_monitored_on_signal() -> None:
    spot, fut = _FakeExecutor(), _FakeExecutor()
    summary = await run_scalping_tick(
        executors=_executors(spot, fut),
        market_data=_FakeMarketData(_uptrend()),
        symbols=["XRPUSDT"],
        products=["spot", "usdm_futures"],
        now=_NOW,
        limits=ScalpingRiskLimits(),
        confirm=True,
        enabled=True,
    )
    assert summary.status == "ran"
    assert spot.entered == ["XRPUSDT"]  # uptrend -> long entry placed (monitored)
    assert fut.entered == ["XRPUSDT"]
    assert summary.errors == []
    # ROB-907: entered tuples are (product, symbol, status, reason_codes).
    assert ("spot", "XRPUSDT", "reconciled", []) in summary.entered
    assert ("usdm_futures", "XRPUSDT", "reconciled", []) in summary.entered


@pytest.mark.asyncio
async def test_entered_tuple_surfaces_blocked_reason_codes() -> None:
    """ROB-907: blocked entries carry their reason_codes into the summary so
    Prefect logs can diagnose all-blocked ticks without a DB read."""
    spot = _FakeExecutor(
        entry_status="blocked", reason_codes=("spread_too_wide", "stale_data")
    )
    summary = await run_scalping_tick(
        executors=_executors(spot, _FakeExecutor()),
        market_data=_FakeMarketData(_uptrend()),
        symbols=["XRPUSDT"],
        products=["spot"],
        now=_NOW,
        confirm=True,
        enabled=True,
    )
    assert summary.status == "ran"
    assert summary.entered == [
        ("spot", "XRPUSDT", "blocked", ["spread_too_wide", "stale_data"])
    ]
    evidence = summary.to_evidence_dict()
    assert evidence["entered"] == [
        ["spot", "XRPUSDT", "blocked", ["spread_too_wide", "stale_data"]]
    ]


@pytest.mark.asyncio
async def test_passes_real_market_conditions_to_executor() -> None:
    """ROB-315 0c / D4: the tick computes spread (from bookTicker) + data age
    (from the latest candle) and threads them into execute_monitored, so the
    executor's SPREAD_TOO_WIDE / STALE_DATA gates have real inputs."""
    from app.services.brokers.binance.demo_scalping.market_data import BookTicker

    fut = _FakeExecutor()
    md = _FakeMarketData(
        _uptrend(), book=BookTicker(bid=Decimal("100"), ask=Decimal("100.10"))
    )
    summary = await run_scalping_tick(
        executors=_executors(_FakeExecutor(), fut),
        market_data=md,
        symbols=["XRPUSDT"],
        products=["usdm_futures"],
        now=_NOW,
        limits=ScalpingRiskLimits(),
        confirm=True,
        enabled=True,
    )
    assert summary.status == "ran"
    assert fut.markets and fut.markets[0] is not None
    assert fut.markets[0].spread_bps > 0  # ~10 bps from the 100/100.10 book
    assert fut.markets[0].data_age_seconds >= 0


@pytest.mark.asyncio
async def test_no_entry_when_flat() -> None:
    spot, fut = _FakeExecutor(), _FakeExecutor()
    summary = await run_scalping_tick(
        executors=_executors(spot, fut),
        market_data=_FakeMarketData(_flat()),
        symbols=["XRPUSDT"],
        products=["spot"],
        now=_NOW,
        confirm=True,
        enabled=True,
    )
    assert summary.status == "ran"
    assert spot.entered == []


@pytest.mark.asyncio
async def test_entry_error_is_collected_and_tick_continues() -> None:
    spot = _FakeExecutor(raise_on_entry=True)
    fut = _FakeExecutor()
    summary = await run_scalping_tick(
        executors=_executors(spot, fut),
        market_data=_FakeMarketData(_uptrend()),
        symbols=["XRPUSDT", "DOGEUSDT"],
        products=["spot"],
        now=_NOW,
        confirm=True,
        enabled=True,
    )
    assert summary.status == "ran"
    assert len(summary.errors) == 2  # both symbols errored, tick did not crash
    assert all("enter spot" in e for e in summary.errors)


@pytest.mark.asyncio
async def test_dry_run_threads_confirm_false() -> None:
    captured = {}

    class _Spy(_FakeExecutor):
        async def execute_monitored(self, intent, *, confirm, **kwargs):
            captured["confirm"] = confirm
            return _Result("dry_run")

    spy = _Spy()
    await run_scalping_tick(
        executors=_executors(spy, _FakeExecutor()),
        market_data=_FakeMarketData(_uptrend()),
        symbols=["XRPUSDT"],
        products=["spot"],
        now=_NOW,
        confirm=False,
        enabled=True,
    )
    assert captured["confirm"] is False
