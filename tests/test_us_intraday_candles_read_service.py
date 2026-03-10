from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import us_intraday_candles_read_service as svc
from app.services.us_symbol_universe_service import (
    USSymbolInactiveError,
    USSymbolNotRegisteredError,
    USSymbolUniverseEmptyError,
)


class _MappingsResult:
    _rows: list[dict[str, object]]

    def __init__(self, rows: Sequence[Mapping[str, object]]):
        self._rows = [dict(row) for row in rows]

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return list(self._rows)


class _DummySessionManager:
    _session: object

    def __init__(self, session: object):
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        return None


def _sql_text(query: object) -> str:
    return str(getattr(query, "text", query))


def _kis_frame(records: Sequence[Mapping[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(records)


def _internal_frame(datetimes: Sequence[dt.datetime]) -> pd.DataFrame:
    columns = ["datetime", "open", "high", "low", "close", "volume", "value"]
    rows: list[dict[str, object]] = []
    for index, value in enumerate(datetimes, start=1):
        rows.append(
            {
                "datetime": value,
                "open": float(index),
                "high": float(index) + 0.5,
                "low": float(index) - 0.5,
                "close": float(index) + 0.25,
                "volume": float(index * 100),
                "value": float(index * 1000),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _minute_pages(end_time: dt.datetime, page_count: int) -> list[SimpleNamespace]:
    pages: list[SimpleNamespace] = []
    for page_index in range(page_count):
        rows: list[dict[str, object]] = []
        for minute_index in range(120):
            local_dt = end_time - dt.timedelta(
                minutes=(page_index * 120) + minute_index
            )
            rows.append(
                {
                    "datetime": pd.Timestamp(local_dt),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10.0,
                    "value": 1005.0,
                }
            )
        oldest = end_time - dt.timedelta(minutes=((page_index + 1) * 120) - 1)
        pages.append(
            SimpleNamespace(
                frame=_kis_frame(rows),
                has_more=page_index < page_count - 1,
                next_keyb=(oldest - dt.timedelta(minutes=1)).strftime("%Y%m%d%H%M%S"),
            )
        )
    return pages


@pytest.mark.asyncio
async def test_read_us_intraday_candles_queries_db_with_utc_cursor_and_returns_et_naive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_params: list[dict[str, object]] = []
    db_rows = [
        {
            "time": dt.datetime(2024, 6, 30, 18, 30, tzinfo=dt.UTC),
            "open": 150.0,
            "high": 151.0,
            "low": 149.5,
            "close": 150.5,
            "volume": 5000.0,
            "value": 752500.0,
        }
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_1m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            assert params is not None
            captured_params.append(dict(params))
            return _MappingsResult(db_rows)

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="1m",
        count=1,
        end_date=dt.datetime(2024, 6, 30, 14, 30, 0),
    )

    assert captured_params == [
        {
            "symbol": "AAPL",
            "exchange": "NASD",
            "end_time": dt.datetime(2024, 6, 30, 18, 30, tzinfo=dt.UTC),
            "limit": 2,
        }
    ]
    assert list(out["datetime"]) == [dt.datetime(2024, 6, 30, 14, 30, 0)]
    assert out.iloc[0]["session"] == "REGULAR"


@pytest.mark.asyncio
async def test_read_us_intraday_candles_date_only_cursor_uses_post_market_et(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_params: list[dict[str, object]] = []
    db_rows = [
        {
            "time": dt.datetime(2024, 7, 1, 0, 0, tzinfo=dt.UTC),
            "open": 150.0,
            "high": 151.0,
            "low": 149.5,
            "close": 150.5,
            "volume": 5000.0,
            "value": 752500.0,
        }
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_1m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            assert params is not None
            captured_params.append(dict(params))
            return _MappingsResult(db_rows)

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="1m",
        count=1,
        end_date=dt.datetime(2024, 6, 30, 0, 0, 0),
        end_date_is_date_only=True,
    )

    assert captured_params == [
        {
            "symbol": "AAPL",
            "exchange": "NASD",
            "end_time": dt.datetime(2024, 7, 1, 0, 0, tzinfo=dt.UTC),
            "limit": 2,
        }
    ]
    assert list(out["datetime"]) == [dt.datetime(2024, 6, 30, 20, 0, 0)]
    assert out.iloc[0]["session"] == "POST_MARKET"


@pytest.mark.asyncio
async def test_self_heal_1m_candles_persists_utc_aware_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[dict[str, object]] = []
    committed = False

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> None:
            sql = _sql_text(query)
            if "INSERT INTO public.us_candles_1m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            assert params is not None
            executed.append(dict(params))

        async def commit(self) -> None:
            nonlocal committed
            committed = True

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )

    await svc._self_heal_1m_candles(
        symbol="AAPL",
        exchange="NASD",
        minute_rows=[
            {
                "datetime": dt.datetime(2024, 6, 30, 14, 30, 0),
                "open": 150.0,
                "high": 151.0,
                "low": 149.5,
                "close": 150.5,
                "volume": 5000.0,
                "value": 752500.0,
            }
        ],
    )

    assert committed is True
    assert len(executed) == 1
    assert executed[0]["time"] == dt.datetime(2024, 6, 30, 18, 30, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_fetch_minutes_from_kis_allows_cross_day_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_one = SimpleNamespace(
        frame=_kis_frame(
            [
                {
                    "datetime": pd.Timestamp("2024-06-30 20:00:00"),
                    "open": 200.0,
                    "high": 201.0,
                    "low": 199.0,
                    "close": 200.5,
                    "volume": 100.0,
                    "value": 20050.0,
                }
            ]
        ),
        has_more=True,
        next_keyb="20240629200000",
    )
    page_two = SimpleNamespace(
        frame=_kis_frame(
            [
                {
                    "datetime": pd.Timestamp("2024-06-29 20:00:00"),
                    "open": 190.0,
                    "high": 191.0,
                    "low": 189.0,
                    "close": 190.5,
                    "volume": 90.0,
                    "value": 17145.0,
                }
            ]
        ),
        has_more=False,
        next_keyb=None,
    )
    kis = SimpleNamespace(
        inquire_overseas_minute_chart=AsyncMock(side_effect=[page_one, page_two])
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    # Create required_buckets for testing: 2 buckets representing 2 minutes
    required_buckets = {
        dt.datetime(2024, 6, 30, 0, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 1, 0, 0, 0, tzinfo=dt.UTC),
    }
    out = await svc._fetch_minutes_from_kis(
        symbol="AAPL",
        exchange="NASD",
        end_time_et=dt.datetime(2024, 6, 30, 20, 0, 0),
        required_buckets=required_buckets,
        required_window_bucket_count=2,
        period="1m",
    )

    assert list(out["datetime"]) == [
        dt.datetime(2024, 6, 30, 0, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 1, 0, 0, 0, tzinfo=dt.UTC),
    ]
    assert list(out["datetime"]) == [
        dt.datetime(2024, 6, 30, 0, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 1, 0, 0, 0, tzinfo=dt.UTC),
    ]


@pytest.mark.asyncio
async def test_fetch_minutes_from_kis_1m_recent_gap_stops_after_one_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    end_time = dt.datetime(2024, 6, 30, 20, 0, 0)
    inquire_mock = AsyncMock(side_effect=_minute_pages(end_time, 2))
    kis = SimpleNamespace(inquire_overseas_minute_chart=inquire_mock)
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    expected_buckets = svc._expected_recent_buckets_utc(
        period="1m",
        count=61,
        end_time_utc=svc._et_naive_to_utc(end_time),
    )
    required_buckets = {expected_buckets[0]}
    out = await svc._fetch_minutes_from_kis(
        symbol="AAPL",
        exchange="NASD",
        end_time_et=end_time,
        required_buckets=required_buckets,
        required_window_bucket_count=1,
        period="1m",
    )

    assert inquire_mock.await_count == 1
    assert not out.empty


@pytest.mark.asyncio
async def test_fetch_minutes_from_kis_5m_scales_pages_with_required_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    end_time = dt.datetime(2024, 6, 30, 20, 0, 0)
    inquire_mock = AsyncMock(side_effect=_minute_pages(end_time, 4))
    kis = SimpleNamespace(inquire_overseas_minute_chart=inquire_mock)
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    expected_buckets = svc._expected_recent_buckets_utc(
        period="5m",
        count=61,
        end_time_utc=svc._et_naive_to_utc(end_time),
    )
    required_buckets = {expected_buckets[-1]}
    out = await svc._fetch_minutes_from_kis(
        symbol="AAPL",
        exchange="NASD",
        end_time_et=end_time,
        required_buckets=required_buckets,
        required_window_bucket_count=len(expected_buckets),
        period="5m",
    )

    aggregated = svc._aggregate_minutes_to_period_utc(out, "5m")

    assert 3 <= inquire_mock.await_count <= 4
    assert not out.empty
    assert expected_buckets[-1] in set(aggregated["datetime"].tolist())


@pytest.mark.asyncio
async def test_fetch_minutes_from_kis_1h_still_uses_multiple_pages_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that 1h period can still use many pages when truly needed."""
    end_time = dt.datetime(2024, 6, 30, 20, 0, 0)
    inquire_mock = AsyncMock(side_effect=_minute_pages(end_time, 31))
    kis = SimpleNamespace(inquire_overseas_minute_chart=inquire_mock)
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    expected_buckets = svc._expected_recent_buckets_utc(
        period="1h",
        count=61,
        end_time_utc=svc._et_naive_to_utc(end_time),
    )
    required_buckets = {expected_buckets[-1]}
    out = await svc._fetch_minutes_from_kis(
        symbol="AAPL",
        exchange="NASD",
        end_time_et=end_time,
        required_buckets=required_buckets,
        required_window_bucket_count=len(expected_buckets),
        period="1h",
    )

    assert inquire_mock.await_count >= 20
    assert out.iloc[0]["datetime"] < dt.datetime(2024, 6, 29, 0, 0, tzinfo=dt.UTC)
    assert out.iloc[-1]["datetime"] == dt.datetime(2024, 7, 1, 0, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_read_us_intraday_candles_5m_merges_db_history_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cagg_rows = [
        {
            "bucket": dt.datetime(2024, 6, 30, 17, 50, tzinfo=dt.UTC),
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 500.0,
            "value": 50250.0,
        }
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_5m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            return _MappingsResult(cagg_rows)

    fallback_page = SimpleNamespace(
        frame=_kis_frame(
            [
                {
                    "datetime": pd.Timestamp("2024-06-30 13:55:00"),
                    "open": 101.0,
                    "high": 101.5,
                    "low": 100.8,
                    "close": 101.2,
                    "volume": 100.0,
                    "value": 10120.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:56:00"),
                    "open": 101.2,
                    "high": 101.8,
                    "low": 101.1,
                    "close": 101.4,
                    "volume": 120.0,
                    "value": 12168.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:57:00"),
                    "open": 101.4,
                    "high": 102.0,
                    "low": 101.3,
                    "close": 101.6,
                    "volume": 140.0,
                    "value": 14224.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:58:00"),
                    "open": 101.6,
                    "high": 102.1,
                    "low": 101.5,
                    "close": 101.8,
                    "volume": 160.0,
                    "value": 16288.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:59:00"),
                    "open": 101.8,
                    "high": 102.4,
                    "low": 101.7,
                    "close": 102.0,
                    "volume": 180.0,
                    "value": 18360.0,
                },
            ]
        ),
        has_more=False,
        next_keyb=None,
    )
    kis = SimpleNamespace(
        inquire_overseas_minute_chart=AsyncMock(return_value=fallback_page)
    )

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)
    monkeypatch.setattr(svc, "_schedule_background_self_heal", lambda **_: None)

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="5m",
        count=2,
        end_date=dt.datetime(2024, 6, 30, 14, 0, 0),
    )

    assert list(out["datetime"]) == [
        dt.datetime(2024, 6, 30, 13, 50, 0),
        dt.datetime(2024, 6, 30, 13, 55, 0),
    ]
    assert out.iloc[0]["close"] == pytest.approx(100.5)
    assert out.iloc[1]["close"] == pytest.approx(102.0)


@pytest.mark.asyncio
async def test_read_us_intraday_candles_5m_backfills_missing_recent_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cagg_rows = [
        {
            "bucket": dt.datetime(2024, 6, 30, 17, 45, tzinfo=dt.UTC),
            "open": 99.0,
            "high": 100.0,
            "low": 98.5,
            "close": 99.5,
            "volume": 400.0,
            "value": 39800.0,
        },
        {
            "bucket": dt.datetime(2024, 6, 30, 17, 55, tzinfo=dt.UTC),
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 500.0,
            "value": 50250.0,
        },
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_5m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            return _MappingsResult(cagg_rows)

    fallback_page = SimpleNamespace(
        frame=_kis_frame(
            [
                {
                    "datetime": pd.Timestamp("2024-06-30 13:50:00"),
                    "open": 100.6,
                    "high": 100.8,
                    "low": 100.4,
                    "close": 100.7,
                    "volume": 90.0,
                    "value": 9063.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:51:00"),
                    "open": 100.7,
                    "high": 100.9,
                    "low": 100.5,
                    "close": 100.8,
                    "volume": 92.0,
                    "value": 9273.6,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:52:00"),
                    "open": 100.8,
                    "high": 101.0,
                    "low": 100.6,
                    "close": 100.9,
                    "volume": 94.0,
                    "value": 9484.6,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:53:00"),
                    "open": 100.9,
                    "high": 101.1,
                    "low": 100.7,
                    "close": 101.0,
                    "volume": 96.0,
                    "value": 9696.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:54:00"),
                    "open": 101.0,
                    "high": 101.2,
                    "low": 100.8,
                    "close": 101.1,
                    "volume": 98.0,
                    "value": 9907.8,
                },
            ]
        ),
        has_more=False,
        next_keyb=None,
    )
    kis = SimpleNamespace(
        inquire_overseas_minute_chart=AsyncMock(return_value=fallback_page)
    )

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)
    monkeypatch.setattr(svc, "_schedule_background_self_heal", lambda **_: None)

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="5m",
        count=2,
        end_date=dt.datetime(2024, 6, 30, 14, 0, 0),
    )

    assert list(out["datetime"]) == [
        dt.datetime(2024, 6, 30, 13, 50, 0),
        dt.datetime(2024, 6, 30, 13, 55, 0),
    ]
    assert out.iloc[0]["close"] == pytest.approx(101.1)
    assert out.iloc[1]["close"] == pytest.approx(100.5)


@pytest.mark.asyncio
async def test_read_us_intraday_candles_5m_backfills_missing_interior_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cagg_rows = [
        {
            "bucket": dt.datetime(2024, 6, 30, 17, 45, tzinfo=dt.UTC),
            "open": 99.0,
            "high": 100.0,
            "low": 98.5,
            "close": 99.5,
            "volume": 400.0,
            "value": 39800.0,
        },
        {
            "bucket": dt.datetime(2024, 6, 30, 17, 55, tzinfo=dt.UTC),
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 500.0,
            "value": 50250.0,
        },
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_5m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            return _MappingsResult(cagg_rows)

    fallback_page = SimpleNamespace(
        frame=_kis_frame(
            [
                {
                    "datetime": pd.Timestamp("2024-06-30 13:50:00"),
                    "open": 100.6,
                    "high": 100.8,
                    "low": 100.4,
                    "close": 100.7,
                    "volume": 90.0,
                    "value": 9063.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:51:00"),
                    "open": 100.7,
                    "high": 100.9,
                    "low": 100.5,
                    "close": 100.8,
                    "volume": 92.0,
                    "value": 9273.6,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:52:00"),
                    "open": 100.8,
                    "high": 101.0,
                    "low": 100.6,
                    "close": 100.9,
                    "volume": 94.0,
                    "value": 9484.6,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:53:00"),
                    "open": 100.9,
                    "high": 101.1,
                    "low": 100.7,
                    "close": 101.0,
                    "volume": 96.0,
                    "value": 9696.0,
                },
                {
                    "datetime": pd.Timestamp("2024-06-30 13:54:00"),
                    "open": 101.0,
                    "high": 101.2,
                    "low": 100.8,
                    "close": 101.1,
                    "volume": 98.0,
                    "value": 9907.8,
                },
            ]
        ),
        has_more=False,
        next_keyb=None,
    )
    kis = SimpleNamespace(
        inquire_overseas_minute_chart=AsyncMock(return_value=fallback_page)
    )

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)
    monkeypatch.setattr(svc, "_schedule_background_self_heal", lambda **_: None)

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="5m",
        count=2,
        end_date=dt.datetime(2024, 6, 30, 13, 59, 0),
    )

    assert list(out["datetime"]) == [
        dt.datetime(2024, 6, 30, 13, 50, 0),
        dt.datetime(2024, 6, 30, 13, 55, 0),
    ]
    assert out.iloc[0]["close"] == pytest.approx(101.1)
    assert out.iloc[1]["close"] == pytest.approx(100.5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_type", "message"),
    [
        (
            USSymbolUniverseEmptyError,
            "us_symbol_universe is empty. Sync required: uv run python scripts/sync_us_symbol_universe.py",
        ),
        (
            USSymbolInactiveError,
            "US symbol 'AAPL' is inactive in us_symbol_universe. Sync required: uv run python scripts/sync_us_symbol_universe.py",
        ),
        (
            USSymbolNotRegisteredError,
            "US symbol 'AAPL' is not registered in us_symbol_universe. Sync required: uv run python scripts/sync_us_symbol_universe.py",
        ),
    ],
)
async def test_read_us_intraday_candles_propagates_symbol_universe_errors(
    monkeypatch: pytest.MonkeyPatch,
    exc_type: type[Exception],
    message: str,
) -> None:
    monkeypatch.setattr(
        svc,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=exc_type(message)),
    )

    with pytest.raises(exc_type, match=message):
        _ = await svc.read_us_intraday_candles(symbol="AAPL", period="1m", count=1)


def test_aggregate_minutes_to_hourly_uses_us_session_anchors() -> None:
    frame = pd.DataFrame(
        [
            {
                "datetime": dt.datetime(2024, 6, 30, 8, 0, 0, tzinfo=dt.UTC),
                "open": 90.0,
                "high": 91.0,
                "low": 89.0,
                "close": 90.5,
                "volume": 10.0,
                "value": 905.0,
            },
            {
                "datetime": dt.datetime(2024, 6, 30, 8, 59, 0, tzinfo=dt.UTC),
                "open": 90.5,
                "high": 92.0,
                "low": 90.0,
                "close": 91.5,
                "volume": 12.0,
                "value": 1098.0,
            },
            {
                "datetime": dt.datetime(2024, 6, 30, 13, 30, 0, tzinfo=dt.UTC),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 14.0,
                "value": 1407.0,
            },
            {
                "datetime": dt.datetime(2024, 6, 30, 14, 29, 0, tzinfo=dt.UTC),
                "open": 100.5,
                "high": 102.0,
                "low": 100.0,
                "close": 101.5,
                "volume": 16.0,
                "value": 1624.0,
            },
        ]
    )

    out = svc._aggregate_minutes_to_period_utc(frame, "1h")

    assert list(out["datetime"]) == [
        dt.datetime(2024, 6, 30, 8, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 6, 30, 13, 30, 0, tzinfo=dt.UTC),
    ]
    assert list(svc._to_output_frame(out)["session"]) == ["PRE_MARKET", "REGULAR"]


def test_expected_recent_buckets_5m_skips_overnight_gap_at_pre_market_open() -> None:
    end_time_utc = dt.datetime(2024, 7, 2, 8, 0, 0, tzinfo=dt.UTC)

    buckets = svc._expected_recent_buckets_utc(
        period="5m",
        count=4,
        end_time_utc=end_time_utc,
    )

    assert buckets == [
        dt.datetime(2024, 7, 2, 8, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 2, 0, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 1, 23, 55, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 1, 23, 50, 0, tzinfo=dt.UTC),
    ]


def test_expected_recent_buckets_1m_skips_overnight_minutes() -> None:
    end_time_utc = dt.datetime(2024, 7, 2, 8, 1, 0, tzinfo=dt.UTC)

    buckets = svc._expected_recent_buckets_utc(
        period="1m",
        count=5,
        end_time_utc=end_time_utc,
    )

    assert buckets == [
        dt.datetime(2024, 7, 2, 8, 1, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 2, 8, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 2, 0, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 1, 23, 59, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 7, 1, 23, 58, 0, tzinfo=dt.UTC),
    ]


def test_get_missing_buckets_empty_for_complete_cross_session_5m_history() -> None:
    current = dt.datetime(2024, 7, 2, 9, 35, 0)
    session_datetimes_et = [dt.datetime(2024, 7, 1, 20, 0, 0)]
    while current >= dt.datetime(2024, 7, 2, 4, 0, 0):
        session_datetimes_et.append(current)
        current -= dt.timedelta(minutes=5)

    frame = _internal_frame(
        [svc._et_naive_to_utc(value) for value in reversed(session_datetimes_et)]
    )

    missing = svc._get_missing_buckets(
        frame,
        "5m",
        len(session_datetimes_et),
        svc._et_naive_to_utc(dt.datetime(2024, 7, 2, 9, 35, 0)),
    )
    assert missing == set()


def test_get_missing_buckets_returns_missing_buckets_when_incomplete() -> None:
    """Test that _get_missing_buckets correctly identifies missing buckets."""
    end_time_utc = dt.datetime(2024, 6, 30, 14, 0, 0, tzinfo=dt.UTC)

    # Create a frame with only the first 2 buckets out of 4 expected
    existing_datetimes = [
        dt.datetime(2024, 6, 30, 13, 50, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 6, 30, 13, 55, 0, tzinfo=dt.UTC),
    ]
    frame = _internal_frame(existing_datetimes)

    missing = svc._get_missing_buckets(
        frame,
        "5m",
        4,
        end_time_utc,
    )

    # Get the expected buckets and subtract the ones we have
    expected_all = svc._expected_recent_buckets_utc(
        period="5m",
        count=4,
        end_time_utc=end_time_utc,
    )
    # We have 13:50 and 13:55, so missing should be the other two
    expected_missing = set(expected_all) - {
        dt.datetime(2024, 6, 30, 13, 50, 0, tzinfo=dt.UTC),
        dt.datetime(2024, 6, 30, 13, 55, 0, tzinfo=dt.UTC),
    }
    assert missing == expected_missing


def test_get_missing_buckets_returns_all_buckets_for_empty_frame() -> None:
    """Test that _get_missing_buckets returns all expected buckets when frame is empty."""
    end_time_utc = dt.datetime(2024, 6, 30, 14, 0, 0, tzinfo=dt.UTC)

    frame = svc._empty_internal_frame()
    missing = svc._get_missing_buckets(frame, "5m", 3, end_time_utc)

    # Should return 3 expected buckets
    assert len(missing) == 3
    expected_buckets = svc._expected_recent_buckets_utc(
        period="5m",
        count=3,
        end_time_utc=end_time_utc,
    )
    assert missing == set(expected_buckets)


def test_get_missing_buckets_with_repair_window_extends_to_oldest_gap() -> None:
    end_time_utc = dt.datetime(2024, 6, 30, 20, 0, 0, tzinfo=dt.UTC)
    expected_buckets = svc._expected_recent_buckets_utc(
        period="5m",
        count=61,
        end_time_utc=end_time_utc,
    )
    frame = _internal_frame(list(reversed(expected_buckets[:-1])))

    missing, repair_window = svc._get_missing_buckets_with_repair_window(
        frame,
        "5m",
        61,
        end_time_utc,
    )

    assert missing == {expected_buckets[-1]}
    assert repair_window == expected_buckets


def test_get_missing_buckets_with_repair_window_trims_recent_gap() -> None:
    end_time_utc = dt.datetime(2024, 6, 30, 20, 0, 0, tzinfo=dt.UTC)
    expected_buckets = svc._expected_recent_buckets_utc(
        period="1m",
        count=61,
        end_time_utc=end_time_utc,
    )
    frame = _internal_frame(list(reversed(expected_buckets[1:])))

    missing, repair_window = svc._get_missing_buckets_with_repair_window(
        frame,
        "1m",
        61,
        end_time_utc,
    )

    assert missing == {expected_buckets[0]}
    assert repair_window == [expected_buckets[0]]


@pytest.mark.asyncio
async def test_read_us_intraday_candles_5m_uses_complete_cross_session_db_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cagg_rows = [
        {
            "bucket": dt.datetime(2024, 7, 1, 23, 50, 0, tzinfo=dt.UTC),
            "open": 100.0,
            "high": 100.5,
            "low": 99.8,
            "close": 100.2,
            "volume": 1000.0,
            "value": 100200.0,
        },
        {
            "bucket": dt.datetime(2024, 7, 1, 23, 55, 0, tzinfo=dt.UTC),
            "open": 100.2,
            "high": 100.7,
            "low": 100.1,
            "close": 100.4,
            "volume": 1100.0,
            "value": 110440.0,
        },
        {
            "bucket": dt.datetime(2024, 7, 2, 0, 0, 0, tzinfo=dt.UTC),
            "open": 100.4,
            "high": 100.9,
            "low": 100.3,
            "close": 100.6,
            "volume": 1200.0,
            "value": 120720.0,
        },
        {
            "bucket": dt.datetime(2024, 7, 2, 8, 0, 0, tzinfo=dt.UTC),
            "open": 101.0,
            "high": 101.5,
            "low": 100.8,
            "close": 101.3,
            "volume": 1300.0,
            "value": 131690.0,
        },
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_5m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            return _MappingsResult(cagg_rows)

    inquire_mock = AsyncMock(side_effect=AssertionError("KIS should not be called"))
    kis = SimpleNamespace(inquire_overseas_minute_chart=inquire_mock)

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)
    monkeypatch.setattr(svc, "_schedule_background_self_heal", lambda **_: None)

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="5m",
        count=4,
        end_date=dt.datetime(2024, 7, 2, 4, 0, 0),
    )

    assert inquire_mock.await_count == 0
    assert list(out["datetime"]) == [
        dt.datetime(2024, 7, 1, 19, 50, 0),
        dt.datetime(2024, 7, 1, 19, 55, 0),
        dt.datetime(2024, 7, 1, 20, 0, 0),
        dt.datetime(2024, 7, 2, 4, 0, 0),
    ]


@pytest.mark.asyncio
async def test_read_us_intraday_candles_partial_repair_fetches_one_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that when DB is missing only the last bucket, fallback fetches one page."""
    # DB has 3 of 4 required buckets (missing 13:55)
    cagg_rows = [
        {
            "bucket": dt.datetime(2024, 6, 30, 17, 45, tzinfo=dt.UTC),
            "open": 99.0,
            "high": 100.0,
            "low": 98.5,
            "close": 99.5,
            "volume": 400.0,
            "value": 39800.0,
        },
        {
            "bucket": dt.datetime(2024, 6, 30, 17, 50, tzinfo=dt.UTC),
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 500.0,
            "value": 50250.0,
        },
        {
            "bucket": dt.datetime(2024, 6, 30, 18, 0, tzinfo=dt.UTC),
            "open": 101.0,
            "high": 102.0,
            "low": 100.5,
            "close": 101.5,
            "volume": 600.0,
            "value": 60900.0,
        },
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_5m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            return _MappingsResult(cagg_rows)

    fallback_page = SimpleNamespace(
        frame=_kis_frame(
            [
                {
                    "datetime": pd.Timestamp("2024-06-30 13:55:00"),
                    "open": 101.0,
                    "high": 101.5,
                    "low": 100.8,
                    "close": 101.2,
                    "volume": 100.0,
                    "value": 10120.0,
                },
            ]
        ),
        has_more=True,
        next_keyb="20240630135400",
    )
    inquire_mock = AsyncMock(return_value=fallback_page)
    kis = SimpleNamespace(inquire_overseas_minute_chart=inquire_mock)

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)
    monkeypatch.setattr(svc, "_schedule_background_self_heal", lambda **_: None)

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="5m",
        count=4,
        end_date=dt.datetime(2024, 6, 30, 14, 0, 0),
    )

    # Should fetch only 1 page since only 1 bucket is missing
    assert inquire_mock.await_count == 1
    assert list(out["datetime"]) == [
        dt.datetime(2024, 6, 30, 13, 45, 0),
        dt.datetime(2024, 6, 30, 13, 50, 0),
        dt.datetime(2024, 6, 30, 13, 55, 0),
        dt.datetime(2024, 6, 30, 14, 0, 0),
    ]


@pytest.mark.asyncio
async def test_read_us_intraday_candles_5m_repairs_sparse_oldest_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    end_time_et = dt.datetime(2024, 6, 30, 20, 0, 0)
    end_time_utc = svc._et_naive_to_utc(end_time_et)
    expected_buckets = svc._expected_recent_buckets_utc(
        period="5m",
        count=61,
        end_time_utc=end_time_utc,
    )
    cagg_rows = [
        {
            "bucket": bucket,
            "open": float(index),
            "high": float(index) + 0.5,
            "low": float(index) - 0.5,
            "close": float(index) + 0.25,
            "volume": float(index * 100),
            "value": float(index * 1000),
        }
        for index, bucket in enumerate(reversed(expected_buckets[:-1]), start=1)
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_5m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            return _MappingsResult(cagg_rows)

    inquire_mock = AsyncMock(side_effect=_minute_pages(end_time_et, 4))
    kis = SimpleNamespace(inquire_overseas_minute_chart=inquire_mock)

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)
    monkeypatch.setattr(svc, "_schedule_background_self_heal", lambda **_: None)

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="5m",
        count=61,
        end_date=end_time_et,
    )

    merged_internal = _internal_frame(
        [svc._et_naive_to_utc(value) for value in out["datetime"].tolist()]
    )

    assert 3 <= inquire_mock.await_count <= 4
    assert len(out) == 61
    assert list(out["datetime"]) == [
        svc._utc_to_et_naive(bucket) for bucket in reversed(expected_buckets)
    ]
    assert svc._get_missing_buckets(merged_internal, "5m", 61, end_time_utc) == set()


@pytest.mark.asyncio
async def test_read_us_intraday_candles_no_fallback_when_db_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that when DB has all expected buckets, KIS is not called."""
    end_time_et = dt.datetime(2024, 6, 30, 14, 0, 0)
    end_time_utc = svc._et_naive_to_utc(end_time_et)

    # Generate all 4 expected buckets
    expected_buckets = svc._expected_recent_buckets_utc(
        period="5m", count=4, end_time_utc=end_time_utc
    )
    cagg_rows = [
        {
            "bucket": bucket,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 500.0,
            "value": 50250.0,
        }
        for bucket in expected_buckets
    ]

    class DummyDB:
        async def execute(
            self, query: object, params: dict[str, object] | None = None
        ) -> _MappingsResult:
            sql = _sql_text(query)
            if "FROM public.us_candles_5m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            return _MappingsResult(cagg_rows)

    inquire_mock = AsyncMock(side_effect=AssertionError("KIS should not be called"))
    kis = SimpleNamespace(inquire_overseas_minute_chart=inquire_mock)

    monkeypatch.setattr(
        svc,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(DummyDB()),
    )
    monkeypatch.setattr(
        svc, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(svc, "KISClient", lambda: kis)
    monkeypatch.setattr(svc, "_schedule_background_self_heal", lambda **_: None)

    out = await svc.read_us_intraday_candles(
        symbol="AAPL",
        period="5m",
        count=4,
        end_date=end_time_et,
    )

    # KIS should not be called
    assert inquire_mock.await_count == 0
    assert len(out) == 4
