from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from app.services.brokers.toss.rate_limiter import TossApiGroup, _BASE_LIMITS
from app.core.config import DEFAULT_KIS_API_RATE_LIMITS
from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import (
    DailyCandleSyncService,
    NoDataError,
    SyncTarget,
    UsDailyLane,
)
from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow


def _frame(symbol: str, price: int = 10) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-11-20",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 100,
                "value": price * 100,
            }
        ]
    )


class _Repo:
    def __init__(self, *, write=None) -> None:
        self.session = AsyncMock()
        self.rows = []
        self.write = write

    async def upsert_rows(self, *, market, rows):
        assert market is MarketKey.US
        if self.write is not None:
            return await self.write(rows)
        self.rows.extend(rows)
        return len(rows)


class _LimiterSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> bool:
        self.calls += 1
        return True


def _target(symbol: str) -> SyncTarget:
    return SyncTarget(MarketKey.US, symbol, "NASD")


def _service(repo: _Repo, lanes: list[UsDailyLane], yahoo=None) -> DailyCandleSyncService:
    service = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=yahoo or AsyncMock(return_value=[]),
        upbit_crypto_fetcher=AsyncMock(),
        us_daily_lanes=lanes,
    )
    service._resolve_universe = AsyncMock()  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
async def test_us_targets_are_distributed_and_each_symbol_uses_one_source() -> None:
    repo = _Repo()
    kis_limiter, toss_limiter = _LimiterSpy(), _LimiterSpy()
    kis_symbols, toss_symbols = [], []

    async def kis(target, _n):
        kis_symbols.append(target.symbol)
        return _frame(target.symbol, 11)

    async def toss(target, _n):
        toss_symbols.append(target.symbol)
        return _frame(target.symbol, 22)

    service = _service(
        repo,
        [
            UsDailyLane("kis", "kis", kis, 10, 10, kis_limiter),
            UsDailyLane("toss", "toss_fallback", toss, 5, 5, toss_limiter),
        ],
    )
    targets = [_target(f"S{i}") for i in range(4)]
    service._resolve_universe.return_value = targets

    result = await service.sync_market_universe(market="us", horizon_bars=10)

    assert result["status"] == "ok"
    assert kis_symbols == ["S0", "S2"]
    assert toss_symbols == ["S1", "S3"]
    assert {row.symbol: row.source for row in repo.rows} == {
        "S0": "kis",
        "S1": "toss_fallback",
        "S2": "kis",
        "S3": "toss_fallback",
    }
    assert len(repo.rows) == 4
    assert repo.session.commit.await_count == 1
    assert kis_limiter.calls == 2
    assert toss_limiter.calls == 2
    assert result["lane_counts"]["kis"]["attempted"] == 2
    assert result["lane_counts"]["toss"]["attempted"] == 2


@pytest.mark.asyncio
async def test_empty_assigned_lane_tries_other_lane_then_yahoo() -> None:
    repo = _Repo()
    calls: list[str] = []

    async def kis(target, _n):
        calls.append(f"kis:{target.symbol}")
        return pd.DataFrame()

    async def toss(target, _n):
        calls.append(f"toss:{target.symbol}")
        return pd.DataFrame()

    async def yahoo(*, symbol, n):
        calls.append(f"yahoo:{symbol}")
        return [
            YahooFallbackRow(
                datetime(2026, 11, 20, tzinfo=UTC), symbol, 1, 2, 1, 2, None, 3, 6
            )
        ]

    service = _service(
        repo,
        [
            UsDailyLane("kis", "kis", kis, 10, 10, _LimiterSpy()),
            UsDailyLane("toss", "toss_fallback", toss, 5, 5, _LimiterSpy()),
        ],
        yahoo,
    )
    service._resolve_universe.return_value = [_target("YHOO")]

    result = await service.sync_market_universe(market="us", horizon_bars=10)

    assert calls == ["kis:YHOO", "toss:YHOO", "yahoo:YHOO"]
    assert result["status"] == "ok"
    assert {row.symbol: row.source for row in repo.rows} == {"YHOO": "yahoo_fallback"}


@pytest.mark.asyncio
async def test_yahoo_empty_is_reported_as_no_data_after_each_lane_once() -> None:
    repo = _Repo()
    calls = []

    async def empty(name):
        async def fetch(target, _n):
            calls.append(f"{name}:{target.symbol}")
            return pd.DataFrame()

        return fetch

    async def yahoo(*, symbol, n):
        calls.append(f"yahoo:{symbol}")
        return []

    service = _service(
        repo,
        [
            UsDailyLane("kis", "kis", await empty("kis"), 10, 10),
            UsDailyLane("toss", "toss_fallback", await empty("toss"), 5, 5),
        ],
        yahoo,
    )
    service._resolve_universe.return_value = [_target("EMPTY")]

    result = await service.sync_market_universe(market="us", horizon_bars=10)

    assert calls == ["kis:EMPTY", "toss:EMPTY", "yahoo:EMPTY"]
    assert result["failed_symbols"] == [
        {"symbol": "EMPTY", "error_class": NoDataError.__name__}
    ]


@pytest.mark.asyncio
async def test_lane_error_does_not_stop_other_lane_or_symbols() -> None:
    repo = _Repo()

    async def kis(target, _n):
        if target.symbol == "BAD_KIS":
            raise RuntimeError("provider down")
        return pd.DataFrame()

    async def toss(target, _n):
        return _frame(target.symbol)

    service = _service(
        repo,
        [
            UsDailyLane("kis", "kis", kis, 10, 10),
            UsDailyLane("toss", "toss_fallback", toss, 5, 5),
        ],
    )
    service._resolve_universe.return_value = [_target("BAD_KIS"), _target("TOSS")]

    result = await service.sync_market_universe(market="us", horizon_bars=10)

    assert result["status"] == "ok"
    assert result["failed_symbols"] == []
    assert {row.symbol for row in repo.rows} == {"BAD_KIS", "TOSS"}
    assert result["lane_counts"]["kis"]["errors"] == 1


@pytest.mark.asyncio
async def test_job_wide_database_failure_still_aborts_us_sync() -> None:
    async def fail_write(_rows):
        raise OperationalError("insert", {}, RuntimeError("connection lost"))

    repo = _Repo(write=fail_write)
    async def kis(target, _n):
        return _frame(target.symbol)

    service = _service(repo, [UsDailyLane("kis", "kis", kis, 10, 10)])
    service._resolve_universe.return_value = [_target("DB_FAIL")]

    with pytest.raises(OperationalError):
        await service.sync_market_universe(market="us", horizon_bars=10)

    repo.session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_symbol_local_database_failure_is_reported_and_other_symbol_commits() -> None:
    repo = _Repo()

    async def write(rows):
        if any(row.symbol == "BAD_WRITE" for row in rows):
            raise ValueError("statement rejected")
        repo.rows.extend(rows)
        return len(rows)

    repo.write = write

    async def kis(target, _n):
        return _frame(target.symbol)

    service = _service(repo, [UsDailyLane("kis", "kis", kis, 10, 10)])
    service._resolve_universe.return_value = [_target("BAD_WRITE"), _target("GOOD")]

    result = await service.sync_market_universe(market="us", horizon_bars=10)

    assert result["status"] == "partial"
    assert result["failed_symbols"] == [
        {"symbol": "BAD_WRITE", "error_class": "ValueError"}
    ]
    assert [row.symbol for row in repo.rows] == ["GOOD"]


def test_us_daily_lane_rate_budgets_match_provider_limits() -> None:
    assert DEFAULT_KIS_API_RATE_LIMITS[
        "HHDFS76240000|/uapi/overseas-price/v1/quotations/dailyprice"
    ] == {"rate": 10, "period": 1.0}
    assert _BASE_LIMITS[TossApiGroup.MARKET_DATA_CHART] == 15


@pytest.mark.asyncio
async def test_yahoo_empty_daily_result_is_a_normal_empty_fallback(monkeypatch) -> None:
    from app.services.daily_candles import yahoo_us_fallback

    async def raw(**kwargs):
        assert kwargs["empty_ok"] is True
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    monkeypatch.setattr(yahoo_us_fallback.yahoo_service, "_fetch_ohlcv_raw", raw)
    monkeypatch.setattr(
        yahoo_us_fallback.yahoo_service,
        "_filter_closed_buckets_nyse",
        lambda frame, _period: frame,
    )

    assert await yahoo_us_fallback.fetch_us_daily_yahoo_fallback(
        symbol="EMPTY", n=10
    ) == []
