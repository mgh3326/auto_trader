from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.jobs import kr_ohlcv_precompute


@pytest.mark.asyncio
async def test_incremental_bootstraps_new_symbols_with_7_days(monkeypatch):
    monkeypatch.setattr(
        kr_ohlcv_precompute.kr_ohlcv_timeseries_store,
        "ensure_timescale_ready",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        kr_ohlcv_precompute,
        "_collect_kr_symbols",
        AsyncMock(return_value={"005930", "000660"}),
    )

    latest_bucket_mock = AsyncMock(
        side_effect=[None, datetime.datetime(2026, 2, 19, 9, 0)]
    )
    monkeypatch.setattr(
        kr_ohlcv_precompute.kr_ohlcv_timeseries_store,
        "fetch_latest_hourly_bucket",
        latest_bucket_mock,
    )

    sync_mock = AsyncMock(return_value={"status": "completed", "rows": 12})
    monkeypatch.setattr(kr_ohlcv_precompute, "_sync_symbol_minutes", sync_mock)

    result = await kr_ohlcv_precompute.run_kr_ohlcv_incremental_precompute()

    assert result["status"] == "completed"
    assert result["mode"] == "incremental"
    assert result["symbols"] == 2
    assert result["bootstrapped"] == 1

    called_days = [call.args[1] for call in sync_mock.await_args_list]
    assert sorted(called_days) == [1, 7]


@pytest.mark.asyncio
async def test_nightly_expands_existing_symbols_to_30_days(monkeypatch):
    monkeypatch.setattr(
        kr_ohlcv_precompute.kr_ohlcv_timeseries_store,
        "ensure_timescale_ready",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        kr_ohlcv_precompute,
        "_collect_kr_symbols",
        AsyncMock(return_value={"005930", "000660"}),
    )

    latest_bucket_mock = AsyncMock(
        side_effect=[None, datetime.datetime(2026, 2, 19, 9, 0)]
    )
    monkeypatch.setattr(
        kr_ohlcv_precompute.kr_ohlcv_timeseries_store,
        "fetch_latest_hourly_bucket",
        latest_bucket_mock,
    )

    sync_mock = AsyncMock(return_value={"status": "completed", "rows": 24})
    monkeypatch.setattr(kr_ohlcv_precompute, "_sync_symbol_minutes", sync_mock)

    result = await kr_ohlcv_precompute.run_kr_ohlcv_nightly_precompute()

    assert result["status"] == "completed"
    assert result["mode"] == "nightly"
    assert result["symbols"] == 2
    assert result["bootstrapped"] == 1
    assert result["expanded_to_30d"] == 1

    called_days = [call.args[1] for call in sync_mock.await_args_list]
    assert sorted(called_days) == [7, 30]


@pytest.mark.asyncio
async def test_incremental_returns_failed_status_on_exception(monkeypatch):
    monkeypatch.setattr(
        kr_ohlcv_precompute.kr_ohlcv_timeseries_store,
        "ensure_timescale_ready",
        AsyncMock(side_effect=RuntimeError("timescale down")),
    )

    result = await kr_ohlcv_precompute.run_kr_ohlcv_incremental_precompute()

    assert result["status"] == "failed"
    assert result["mode"] == "incremental"
    assert "timescale down" in str(result["error"])


@pytest.mark.asyncio
async def test_resolve_route_returns_dual_for_canary_symbol(monkeypatch):
    class _ScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DummySession:
        async def execute(self, _statement):
            return _ScalarResult(
                type("Row", (), {"is_active": True, "nxt_eligible": True})
            )

    class _DummySessionManager:
        async def __aenter__(self):
            return _DummySession()

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

    monkeypatch.setattr(
        kr_ohlcv_precompute,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(),
    )
    monkeypatch.setattr(
        kr_ohlcv_precompute.settings,
        "KR_OHLCV_DUAL_ROUTE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        kr_ohlcv_precompute.settings,
        "KR_OHLCV_DUAL_ROUTE_CANARY_SYMBOLS",
        ["005930"],
        raising=False,
    )

    routes = await kr_ohlcv_precompute._resolve_route("005930")

    assert routes == ["J", "NX"]


@pytest.mark.asyncio
async def test_sync_symbol_minutes_skips_non_trading_day_and_upserts_split_routes(
    monkeypatch,
):
    monkeypatch.setattr(
        kr_ohlcv_precompute,
        "_resolve_route",
        AsyncMock(return_value=["J", "NX"]),
    )

    fixed_now = datetime.datetime(2026, 2, 23, 10, 0, tzinfo=kr_ohlcv_precompute._KST)
    base_datetime = kr_ohlcv_precompute.datetime.datetime

    class _FixedDateTime(base_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(kr_ohlcv_precompute.datetime, "datetime", _FixedDateTime)

    def _session_bounds(_route: str, day: datetime.date):
        if day.weekday() >= 5:
            return None
        return (
            datetime.datetime.combine(
                day, datetime.time(9, 0), tzinfo=kr_ohlcv_precompute._KST
            ),
            datetime.datetime.combine(
                day, datetime.time(15, 30), tzinfo=kr_ohlcv_precompute._KST
            ),
        )

    monkeypatch.setattr(kr_ohlcv_precompute, "get_session_bounds", _session_bounds)

    called_days: list[tuple[str, datetime.date]] = []

    class _DummyKISClient:
        async def inquire_time_dailychartprice(
            self, code, market, n, end_date=None, end_time=None
        ):
            del code, n, end_time
            assert end_date is not None
            called_days.append((market, end_date))
            return pd.DataFrame(
                [
                    {
                        "datetime": pd.Timestamp(f"{end_date} 09:00:00"),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "volume": 100,
                        "value": 10050,
                    }
                ]
            )

    monkeypatch.setattr(kr_ohlcv_precompute, "KISClient", _DummyKISClient)

    upsert_mock = AsyncMock(
        return_value={
            "rows": 1,
            "min_ts": _FixedDateTime(2026, 2, 20, 0, 0, tzinfo=datetime.UTC),
            "max_ts": _FixedDateTime(2026, 2, 23, 6, 0, tzinfo=datetime.UTC),
        }
    )
    refresh_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        kr_ohlcv_precompute.kr_ohlcv_timeseries_store,
        "upsert_market_candles_1m",
        upsert_mock,
    )
    monkeypatch.setattr(
        kr_ohlcv_precompute.kr_ohlcv_timeseries_store,
        "refresh_market_candles_1h_kr",
        refresh_mock,
    )

    result = await kr_ohlcv_precompute._sync_symbol_minutes("005930", days=4)

    assert result["status"] == "completed"
    assert result["rows"] == 2
    assert result["route"] == "J,NX"
    assert upsert_mock.await_count == 2
    called_exchanges = {call.kwargs["exchange"] for call in upsert_mock.await_args_list}
    assert called_exchanges == {"KRX", "NXT"}
    assert all(day.weekday() < 5 for _, day in called_days)
    refresh_mock.assert_awaited_once()
