from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import kr_intraday as _intraday_module
from app.services.kr_intraday import _kis_api as _kis_api_module
from app.services.kr_intraday import _repository as _repo_module
from app.services.kr_intraday._types import _MinuteRow
from app.services.kr_intraday._utils import _merge_overlay_into_intraday_frame


def _create_mock_kis_client(
    return_df: pd.DataFrame | None = None,
    side_effect: Any | None = None,
) -> SimpleNamespace:
    """
    Create a mock KISClient with inquire_time_dailychartprice method.

    This helper function creates a mock KIS API client that returns
    the specified DataFrame or uses the specified side effect.

    Parameters
    ----------
    return_df : pd.DataFrame | None
        The DataFrame to return from the API call
    side_effect : Any | None
        The side effect to use (e.g., exception to raise)

    Returns
    -------
    SimpleNamespace
        A mock KIS client with inquire_time_dailychartprice method
    """
    mock_method = AsyncMock()
    if return_df is not None:
        mock_method.return_value = return_df
    if side_effect is not None:
        mock_method.side_effect = side_effect

    return SimpleNamespace(inquire_time_dailychartprice=mock_method)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _MappingsResult:
    def __init__(self, rows: list[dict[str, object]]):
        self._rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class DummySessionManager:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _dt_kst(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime.datetime:
    return datetime.datetime(
        y, m, d, hh, mm, ss, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
    )


def _make_hour_row(
    *,
    bucket_kst_naive: datetime.datetime,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    value: float,
    venues: list[str],
) -> dict[str, object]:
    return {
        "bucket": bucket_kst_naive.replace(
            tzinfo=datetime.timezone(datetime.timedelta(hours=9))
        ),
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "value": value,
        "venues": venues,
    }


def _make_minute_row(
    *,
    time_kst: datetime.datetime,
    venue: str,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    value: float,
) -> dict[str, object]:
    return {
        "time": time_kst.astimezone(datetime.UTC),
        "venue": venue,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "value": value,
    }


def _make_intraday_db_manager(
    *,
    symbol: str,
    nxt_eligible: bool,
    history_table: str | None = None,
    history_rows: list[dict[str, object]] | None = None,
    minute_rows: list[dict[str, object]] | None = None,
) -> DummySessionManager:
    history_rows_list: list[dict[str, object]] = list(history_rows or [])
    minute_rows_list: list[dict[str, object]] = list(minute_rows or [])

    class DummyDB:
        async def execute(self, query, params=None):
            await asyncio.sleep(0)
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": nxt_eligible,
                            "is_active": True,
                        }
                    ]
                )
            if history_table is not None and f"FROM {history_table}" in sql:
                return _MappingsResult(history_rows_list)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult(minute_rows_list)
            raise AssertionError(f"unexpected sql: {sql}")

    return DummySessionManager(DummyDB())


def _patch_intraday_mocks(
    monkeypatch,
    *,
    symbol: str,
    nxt_eligible: bool,
    history_table: str | None = None,
    history_rows: list[dict[str, object]] | None = None,
    minute_rows: list[dict[str, object]] | None = None,
    kis: object | None = None,
    store_background: object | None = None,
) -> None:
    monkeypatch.setattr(
        _repo_module,
        "AsyncSessionLocal",
        lambda: _make_intraday_db_manager(
            symbol=symbol,
            nxt_eligible=nxt_eligible,
            history_table=history_table,
            history_rows=history_rows,
            minute_rows=minute_rows,
        ),
    )
    if kis is not None:
        monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)
    if store_background is not None:
        monkeypatch.setattr(
            _repo_module,
            "_store_minute_candles_background",
            store_background,
        )


@pytest.mark.asyncio
async def test_api_prefetch_plan_time_boundaries_for_nxt_eligible(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"

    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 8, 0, 0),
            venue="NTX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 0, 0),
            venue="KRX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 15, 0, 0),
            venue="KRX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 20, 0, 0),
            venue="NTX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        ),
    ]

    class DummyDB:
        def __init__(self):
            self.calls: list[str] = []

        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            self.calls.append(sql)
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": True,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult([])
            if "FROM public.kr_candles_1m" in sql:
                target_rows = list(minute_rows)
                if isinstance(params, dict) and params.get("start_time") is not None:
                    start = params["start_time"]
                    end = params["end_time"]
                    target_rows = [
                        row for row in target_rows if start <= row["time"] < end
                    ]
                return _MappingsResult(target_rows)
            raise AssertionError(f"unexpected sql: {sql}")

    db = DummyDB()
    from app.services.kr_intraday import _repository as _repo_module

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(db)
    )

    class DummyKIS:
        def __init__(self):
            self.calls: list[str] = []

        async def inquire_time_dailychartprice(
            self, *, code, market, n, end_date, end_time
        ):
            del code, n, end_date, end_time
            self.calls.append(str(market))
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

    kis = DummyKIS()
    from app.services.kr_intraday import _kis_api as _kis_module

    monkeypatch.setattr(_kis_module, "KISClient", lambda: kis)

    from app.services.kr_intraday import _repository as _repo_module

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(db)
    )

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 8, 0, 0),
    )
    assert kis.calls == ["NX"]
    kis.calls.clear()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 9, 0, 0),
    )
    assert sorted(set(kis.calls)) == ["J", "NX"]
    kis.calls.clear()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 15, 34, 0),
    )
    assert sorted(set(kis.calls)) == ["J", "NX"]
    kis.calls.clear()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 15, 35, 0),
    )
    assert "NX" in kis.calls
    kis.calls.clear()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 20, 0, 0),
    )
    assert kis.calls == []


@pytest.mark.asyncio
async def test_api_prefetch_plan_respects_nxt_ineligible(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"

    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 8, 0, 0),
            venue="KRX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
            venue="KRX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 16, 0, 0),
            venue="KRX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        ),
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            await asyncio.sleep(0)
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult([])
            if "FROM public.kr_candles_1m" in sql:
                target_rows = list(minute_rows)
                if isinstance(params, dict) and params.get("start_time") is not None:
                    start = params["start_time"]
                    end = params["end_time"]
                    target_rows = [
                        row for row in target_rows if start <= row["time"] < end
                    ]
                return _MappingsResult(target_rows)
            raise AssertionError(f"unexpected sql: {sql}")

    from app.services.kr_intraday import _kis_api as _kis_module
    from app.services.kr_intraday import _repository as _repo_module

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = SimpleNamespace(inquire_time_dailychartprice=AsyncMock())
    monkeypatch.setattr(_kis_module, "KISClient", lambda: kis)

    # 08:00-09:00 NX-only time window but nxt_eligible=false -> API 0
    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 8, 10, 0),
    )
    kis.inquire_time_dailychartprice.assert_not_awaited()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
    )
    kis.inquire_time_dailychartprice.assert_awaited_once()
    assert kis.inquire_time_dailychartprice.await_args.kwargs["market"] == "J"

    kis.inquire_time_dailychartprice.reset_mock()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 16, 0, 0),
    )
    kis.inquire_time_dailychartprice.assert_not_awaited()


@pytest.mark.asyncio
async def test_end_date_in_past_disables_api(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"

    hour_rows = [
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 20, 10, 0, 0),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
            venues=["KRX"],
        )
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": True,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = SimpleNamespace(inquire_time_dailychartprice=AsyncMock())
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=_dt_kst(2026, 2, 20, 0, 0, 0),
        now_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
    )

    kis.inquire_time_dailychartprice.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_hour_is_reaggregated_from_minutes_not_from_db_hour(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 10, 10, 0)
    current_bucket = datetime.datetime(2026, 2, 23, 10, 0, 0)

    hour_rows = [
        _make_hour_row(
            bucket_kst_naive=current_bucket,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
            venues=["KRX"],
        )
    ]

    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
            venue="KRX",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
            value=1000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 10, 1, 0),
            venue="KRX",
            open=100.5,
            high=102.0,
            low=100.0,
            close=101.0,
            volume=20.0,
            value=2000.0,
        ),
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult(minute_rows)
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )

    assert len(out) == 1
    row = out.iloc[0]
    assert row["datetime"] == current_bucket
    assert row["open"] == 100.0
    assert row["high"] == 102.0
    assert row["low"] == 99.0
    assert row["close"] == 101.0
    assert row["volume"] == 30.0
    assert row["value"] == 3000.0


@pytest.mark.asyncio
async def test_api_overrides_db_minutes_for_same_minute_and_venue(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 10, 10, 0)
    current_bucket = datetime.datetime(2026, 2, 23, 10, 0, 0)

    hour_rows: list[dict[str, object]] = []
    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
            venue="KRX",
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=10.0,
            value=1000.0,
        )
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult(minute_rows)
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    api_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 10:00:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(10, 0, 0),
                "open": 200.0,
                "high": 200.0,
                "low": 200.0,
                "close": 200.0,
                "volume": 1,
                "value": 100,
            }
        ]
    )

    kis = SimpleNamespace(inquire_time_dailychartprice=AsyncMock(return_value=api_df))
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )

    assert len(out) == 1
    row = out.iloc[0]
    assert row["datetime"] == current_bucket
    assert row["open"] == 200.0
    assert row["close"] == 200.0
    assert row["volume"] == 1.0


@pytest.mark.asyncio
async def test_same_minute_both_venues_price_krx_priority_volume_sum(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 9, 10, 0)
    current_bucket = datetime.datetime(2026, 2, 23, 9, 0, 0)

    hour_rows: list[dict[str, object]] = []
    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 0, 0),
            venue="KRX",
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=10.0,
            value=1000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 0, 0),
            venue="NTX",
            open=200.0,
            high=210.0,
            low=190.0,
            close=205.0,
            volume=5.0,
            value=500.0,
        ),
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": True,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult(minute_rows)
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )
    assert len(out) == 1
    row = out.iloc[0]
    assert row["datetime"] == current_bucket
    assert row["open"] == 100.0
    assert row["close"] == 105.0
    assert row["volume"] == 15.0
    assert row["value"] == 1500.0
    assert row["venues"] == ["KRX", "NTX"]


@pytest.mark.asyncio
async def test_synthetic_current_hour_created_when_db_hour_missing(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 10, 10, 0)
    current_bucket = datetime.datetime(2026, 2, 23, 10, 0, 0)

    hour_rows: list[dict[str, object]] = []
    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
            venue="KRX",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
            value=1000.0,
        )
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult(minute_rows)
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )
    assert len(out) == 1
    assert out.iloc[0]["datetime"] == current_bucket


@pytest.mark.asyncio
async def test_session_and_venues_fields_present_and_labeled(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 8, 10, 0)

    hour_rows = [
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 8, 0, 0),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
            venues=["NTX"],
        )
    ]

    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 8, 0, 0),
            venue="NTX",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            value=1.0,
        )
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": True,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult(minute_rows)
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )

    row = out.iloc[0]
    assert row["session"] == "PRE_MARKET"
    assert row["venues"] == ["NTX"]


@pytest.mark.asyncio
async def test_db_insufficient_rows_returns_empty_frame(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult([])
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=2,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
    )

    assert out.empty


@pytest.mark.asyncio
async def test_api_partial_failure_returns_empty_frame(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 9, 0, 0)

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": True,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult([])
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    async def _fail_on_nx(*, market, **_):
        if market == "NX":
            raise RuntimeError("NX failed")
        return pd.DataFrame()

    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(side_effect=_fail_on_nx)
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )

    assert out.empty


@pytest.mark.asyncio
async def test_db_first_returns_existing_data(monkeypatch):
    """Test that DB-first query returns existing data without calling KIS API."""
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    # now_kst is the next day, so all queried hours are historical (no current hour aggregation)
    now_kst = _dt_kst(2026, 2, 24, 10, 0, 0)

    # DB has 5 hours of historical data (valid trading hours: 08:00-12:00)
    hour_rows = [
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 8, 0, 0),
            open=101.0,
            high=103.0,
            low=100.5,
            close=102.0,
            volume=1200.0,
            value=120000.0,
            venues=["KRX"],
        ),
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 9, 0, 0),
            open=102.0,
            high=104.0,
            low=101.0,
            close=103.0,
            volume=1300.0,
            value=130000.0,
            venues=["KRX"],
        ),
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 10, 0, 0),
            open=103.0,
            high=105.0,
            low=102.0,
            close=104.0,
            volume=1400.0,
            value=140000.0,
            venues=["KRX"],
        ),
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 11, 0, 0),
            open=104.0,
            high=106.0,
            low=103.0,
            close=105.0,
            volume=1500.0,
            value=150000.0,
            venues=["KRX"],
        ),
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 12, 0, 0),
            open=105.0,
            high=107.0,
            low=104.0,
            close=106.0,
            volume=1600.0,
            value=160000.0,
            venues=["KRX"],
        ),
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    # Mock KIS API to track calls
    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    # Query with end_date on a previous day - all hours are historical
    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=5,
        end_date=_dt_kst(2026, 2, 23, 12, 0, 0),
        now_kst=now_kst,
    )

    # Verify all 5 hours returned from DB
    assert len(out) == 5
    assert list(out["datetime"]) == [
        datetime.datetime(2026, 2, 23, 8, 0, 0),
        datetime.datetime(2026, 2, 23, 9, 0, 0),
        datetime.datetime(2026, 2, 23, 10, 0, 0),
        datetime.datetime(2026, 2, 23, 11, 0, 0),
        datetime.datetime(2026, 2, 23, 12, 0, 0),
    ]

    # Verify KIS API was NOT called (DB had sufficient data, end_date in past)
    kis.inquire_time_dailychartprice.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_to_kis_api_when_db_empty(monkeypatch):
    """Test fallback to KIS API when DB is empty."""
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 10, 0, 0)

    # KIS API returns some minute candles
    api_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:00:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(9, 0, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000,
                "value": 100000,
            },
            {
                "datetime": pd.Timestamp("2026-02-23 10:00:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(10, 0, 0),
                "open": 100.5,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "volume": 1100,
                "value": 110000,
            },
        ]
    )

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            # DB is empty - no hourly candles
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult([])
            # No minute candles in DB either
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            if "INSERT INTO public.kr_candles_1m" in sql:
                return None
            raise AssertionError(f"unexpected sql: {sql}")

        async def commit(self):
            pass

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = _create_mock_kis_client(return_df=api_df)
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    # Query with end_date=None (current time) - should fallback to KIS API
    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=2,
        end_date=None,
        now_kst=now_kst,
    )

    # Verify candles were returned from KIS API
    assert len(out) == 2

    # Verify KIS API was called (fallback happened)
    kis.inquire_time_dailychartprice.assert_awaited()
    markets_called = [
        call.kwargs["market"]
        for call in kis.inquire_time_dailychartprice.call_args_list
        if "market" in call.kwargs
    ]
    assert "J" in markets_called


@pytest.mark.asyncio
async def test_hour_aggregation_from_minutes():
    """Test 1m → 1h aggregation math (OHLCV).

    Creates 60 1-minute candles with known OHLCV values and verifies that
    aggregation produces correct hourly candle with:
    - open: first minute's open
    - high: max of all highs
    - low: min of all lows
    - close: last minute's close
    - volume: sum of all volumes
    """
    from app.services import kr_hourly_candles_read_service as svc

    # Create 60 minute candles for 09:00-09:59 hour with specific OHLCV values
    minute_data = []
    for i in range(60):
        minute_data.append(
            {
                "datetime": pd.Timestamp(f"2026-02-23 09:{i:02d}:00"),
                "open": 100.0 + i,  # First minute: 100, Last minute: 159
                "high": 200.0 + i,  # Range: 200-259, max should be 259
                "low": 50.0 + i,  # Range: 50-109, min should be 50
                "close": 150.0 + i,  # First minute: 150, Last minute: 209
                "volume": 1000
                + i * 10,  # Range: 1000-1590, sum should be 1000+1010+...+1590
            }
        )

    df_minutes = pd.DataFrame(minute_data)

    # Call the aggregation function
    result = svc._aggregate_minutes_to_hourly(df_minutes)

    # Verify we get exactly 1 hourly candle
    assert len(result) == 1, f"Expected 1 hour, got {len(result)}"

    # Verify the OHLCV aggregation math
    row = result.iloc[0]

    # open: first minute's open (100.0)
    assert row["open"] == 100.0, f"Expected open=100.0, got {row['open']}"

    # high: max of all highs (259.0)
    assert row["high"] == 259.0, f"Expected high=259.0, got {row['high']}"

    # low: min of all lows (50.0)
    assert row["low"] == 50.0, f"Expected low=50.0, got {row['low']}"

    # close: last minute's close (209.0)
    assert row["close"] == 209.0, f"Expected close=209.0, got {row['close']}"

    # volume: sum of all volumes (1000 + 1010 + ... + 1590 = 60 * (1000 + 1590) / 2 = 77700)
    expected_volume = sum(1000 + i * 10 for i in range(60))
    assert row["volume"] == expected_volume, (
        f"Expected volume={expected_volume}, got {row['volume']}"
    )

    # Verify datetime is floored to hour
    assert row["datetime"] == pd.Timestamp("2026-02-23 09:00:00")


def test_normalize_intraday_rows_returns_naive_kst_minute_times():
    from app.services import kr_hourly_candles_read_service as svc

    frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-03-10 08:05:00+09:00"),
                "open": 101.0,
                "high": 102.0,
                "low": 100.5,
                "close": 101.5,
                "volume": 10.0,
                "value": 1000.0,
            }
        ]
    )

    rows = svc._normalize_intraday_rows(
        frame=frame,
        symbol="005930",
        venue_config=svc._VENUE_CONFIGS["NTX"],
        target_day=datetime.date(2026, 3, 10),
    )

    assert len(rows) == 1
    assert rows[0].minute_time == datetime.datetime(2026, 3, 10, 8, 5, 0)
    assert rows[0].minute_time.tzinfo is None


@pytest.mark.asyncio
async def test_background_task_non_blocking(monkeypatch):
    """Test that background storage task is non-blocking.

    Verifies that:
    1. Background storage is scheduled when API minute candles are fetched
    2. The main function returns immediately without waiting for DB write
    3. Background task executes after main function returns
    """
    import asyncio

    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 10, 10, 0)

    # Track whether background storage was called and if it completed
    background_storage_started = False
    background_storage_completed = False
    background_storage_args = []

    # Mock DB to return empty data (triggers API fallback via _build_current_hour_row)
    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult([])
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    # Mock _store_minute_candles_background with delay to simulate slow DB write
    async def mock_store_background(*, symbol, minute_rows):
        nonlocal \
            background_storage_started, \
            background_storage_completed, \
            background_storage_args
        background_storage_started = True
        background_storage_args = (symbol, minute_rows)
        # Simulate slow DB operation (200ms)
        await asyncio.sleep(0.2)
        background_storage_completed = True

    monkeypatch.setattr(
        _repo_module, "_store_minute_candles_background", mock_store_background
    )

    # Mock KIS API to return minute candles (triggers background storage)
    api_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 10:00:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(10, 0, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000,
                "value": 100000,
            }
        ]
    )
    kis = SimpleNamespace(inquire_time_dailychartprice=AsyncMock(return_value=api_df))
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    # Call the function and measure time
    start_time = datetime.datetime.now()
    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )
    end_time = datetime.datetime.now()
    elapsed_ms = (end_time - start_time).total_seconds() * 1000

    # Verify function returned quickly, with room for CI scheduler jitter.
    assert elapsed_ms < 250, (
        f"Function took {elapsed_ms}ms, should return immediately (< 250ms)"
    )

    # Verify function returned data
    assert len(out) > 0, "Function should return data"

    # Let the event loop run the background task once before asserting.
    await asyncio.sleep(0)

    # At this point, background storage started but may not have completed (non-blocking)
    assert background_storage_started, "Background storage should have been started"

    # Wait for background task to complete and verify it finishes
    await asyncio.sleep(0.3)
    assert background_storage_completed, (
        "Background storage should complete after main function returns"
    )

    # Verify background storage was called with correct arguments
    assert len(background_storage_args) == 2, (
        "Background storage should receive symbol and minute_rows"
    )
    assert background_storage_args[0] == symbol, (
        f"Background storage symbol should be {symbol}"
    )
    assert len(background_storage_args[1]) > 0, (
        "Background storage should receive minute candles"
    )


@pytest.mark.asyncio
async def test_api_failure_returns_partial_data(monkeypatch):
    """Test graceful degradation when KIS API fails.

    Verifies that:
    1. When DB has partial data (insufficient rows)
    2. And KIS API call fails (raises exception)
    3. Function returns available DB data instead of raising error
    """
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 10, 0, 0)

    # DB has only 2 hourly candles (user requested 5)
    hour_rows = [
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 8, 0, 0),
            open=101.0,
            high=103.0,
            low=100.5,
            close=102.0,
            volume=1200.0,
            value=120000.0,
            venues=["KRX"],
        ),
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 9, 0, 0),
            open=102.0,
            high=104.0,
            low=101.0,
            close=103.0,
            volume=1300.0,
            value=130000.0,
            venues=["KRX"],
        ),
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    # Mock KIS API to raise exception (simulating network failure or API error)
    async def _fail_api(*, code, market, n, end_date, end_time):
        del code, market, n, end_date, end_time
        raise RuntimeError("KIS API network error")

    kis = SimpleNamespace(inquire_time_dailychartprice=AsyncMock(side_effect=_fail_api))
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    # Request 5 candles but only 2 in DB and API fails
    # Should return 2 candles from DB (graceful degradation)
    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=5,
        end_date=None,
        now_kst=now_kst,
    )

    # Verify partial data returned (no exception raised)
    assert len(out) == 2, f"Expected 2 partial candles from DB, got {len(out)}"

    # Verify the data is from DB
    assert list(out["datetime"]) == [
        datetime.datetime(2026, 2, 23, 8, 0, 0),
        datetime.datetime(2026, 2, 23, 9, 0, 0),
    ]

    # Verify KIS API was called (DB had insufficient data)
    kis.inquire_time_dailychartprice.assert_awaited()


@pytest.mark.asyncio
async def test_venue_separation_preserved(monkeypatch):
    """Test venue separation (KRX/NTX) is preserved in background storage.

    Verifies that:
    1. KIS API can return mixed KRX/NTX minute candles
    2. Background upsert stores candles with correct venue
    3. Multi-venue aggregation works correctly
    """
    import asyncio

    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 10, 10, 0)

    # Track background storage calls to verify venue separation
    background_storage_calls: list[dict[str, object]] = []

    # Mock DB to return empty data (triggers API fallback)
    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": True,  # NXT eligible - both KRX and NTX
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult([])
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult([])
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    # Mock _store_minute_candles_background to track venue preservation
    async def mock_store_background(*, symbol, minute_rows):
        background_storage_calls.append(
            {"symbol": symbol, "minute_rows": list(minute_rows)}
        )

    monkeypatch.setattr(
        _repo_module, "_store_minute_candles_background", mock_store_background
    )

    # Mock KIS API to return mixed KRX/NTX minute candles
    # First call (J/KRX market): returns KRX candles
    api_df_krx = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 10:00:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(10, 0, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000,
                "value": 100000,
            },
            {
                "datetime": pd.Timestamp("2026-02-23 10:01:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(10, 1, 0),
                "open": 100.5,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "volume": 1100,
                "value": 110000,
            },
        ]
    )

    # Second call (NX/NTX market): returns NTX candles
    api_df_ntx = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 10:00:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(10, 0, 0),
                "open": 200.0,
                "high": 201.0,
                "low": 199.0,
                "close": 200.5,
                "volume": 500,
                "value": 50000,
            },
            {
                "datetime": pd.Timestamp("2026-02-23 10:01:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(10, 1, 0),
                "open": 200.5,
                "high": 202.0,
                "low": 200.0,
                "close": 201.0,
                "volume": 600,
                "value": 60000,
            },
        ]
    )

    # Mock KIS API to return different data for KRX vs NTX markets
    async def mock_inquire(*, code, market, n, end_date, end_time):
        del code, n, end_date, end_time
        if market == "J":  # KRX market
            return api_df_krx
        elif market == "NX":  # NTX market
            return api_df_ntx
        return pd.DataFrame()

    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(side_effect=mock_inquire)
    )
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    # Call the function - should fetch from both markets
    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=now_kst,
    )

    # Verify candles were returned
    assert len(out) == 1

    # Verify KIS API was called for both markets
    assert kis.inquire_time_dailychartprice.call_count >= 1

    # Get the markets that were called
    markets_called = [
        call.kwargs["market"]
        for call in kis.inquire_time_dailychartprice.call_args_list
        if "market" in call.kwargs
    ]

    # Should include at least KRX (J) and possibly NTX (NX) depending on time
    assert "J" in markets_called, "KIS API should be called for KRX market"

    # Verify background storage was called
    await asyncio.sleep(0)
    assert len(background_storage_calls) > 0, (
        "Background storage should have been called"
    )

    # Verify venue separation in background storage
    stored_rows = cast(
        list[dict[str, object]], background_storage_calls[0]["minute_rows"]
    )
    venues_stored = {row.get("venue") for row in stored_rows if row.get("venue")}

    # At least KRX should be present (NTX depends on time window)
    assert "KRX" in venues_stored, "KRX venue should be preserved in background storage"

    # Verify multi-venue aggregation in result
    row = out.iloc[0]
    # Volume should be sum of both venues (KRX has priority for price)
    # KRX: 1000 + 1100 = 2100, NTX: 500 + 600 = 1100
    # If both venues present, volume = 2100 + 1100 = 3200
    # If only KRX, volume = 2100
    assert row["volume"] >= 2100, (
        f"Volume should be at least KRX volume (2100), got {row['volume']}"
    )

    # Venues field should list all venues present
    if "venues" in row and row["venues"]:
        # Verify venues field contains KRX
        assert "KRX" in row["venues"], "KRX should be in venues list"


@pytest.mark.asyncio
async def test_partial_db_data_filled_by_api(monkeypatch):
    """Test that partial DB data is filled by KIS API.

    Verifies that:
    1. When DB has partial data (some rows but insufficient)
    2. KIS API successfully returns the missing data
    3. The result combines DB and API data to return requested count
    """
    from app.services import kr_hourly_candles_read_service as svc
    from app.services.kr_intraday import _repository as _repo_module

    symbol = "005930"
    # Use 14:00 during regular market hours (after market opened at 09:00)
    now_kst = _dt_kst(2026, 2, 23, 14, 0, 0)

    # DB has only 2 hourly candles (user requested 5)
    # DB has: 12:00, 13:00
    # Missing: 10:00, 11:00 (to be filled by API)
    # Plus current hour 14:00 (built separately from minute candles)
    hour_rows = [
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 12, 0, 0),
            open=102.0,
            high=104.0,
            low=101.0,
            close=103.0,
            volume=1300.0,
            value=130000.0,
            venues=["KRX"],
        ),
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 2, 23, 13, 0, 0),
            open=103.0,
            high=105.0,
            low=102.0,
            close=104.0,
            volume=1400.0,
            value=140000.0,
            venues=["KRX"],
        ),
    ]

    # Add minute candles for current hour (14:00) to build current hour row
    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 14, 0, 0),
            venue="KRX",
            open=104.0,
            high=106.0,
            low=103.0,
            close=105.0,
            volume=10.0,
            value=1000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 14, 1, 0),
            venue="KRX",
            open=105.0,
            high=107.0,
            low=104.0,
            close=106.0,
            volume=20.0,
            value=2000.0,
        ),
    ]

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "FROM public.kr_symbol_universe" in sql and "LIMIT 1" in sql:
                return _ScalarResult(symbol)
            if "FROM public.kr_symbol_universe" in sql and "WHERE symbol" in sql:
                return _MappingsResult(
                    [
                        {
                            "symbol": symbol,
                            "nxt_eligible": False,  # Not NXT eligible to avoid venue complexity
                            "is_active": True,
                        }
                    ]
                )
            if "FROM public.kr_candles_1h" in sql:
                return _MappingsResult(hour_rows)
            if "FROM public.kr_candles_1m" in sql:
                return _MappingsResult(minute_rows)
            # Handle background storage
            if "INSERT INTO public.kr_candles_1m" in sql:
                return None
            raise AssertionError(f"unexpected sql: {sql}")

        async def commit(self):
            """Mock commit for background storage."""
            pass

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    # Mock KIS API to return minute candles for the missing hours (10:00, 11:00)
    # Build 60 minute candles for 2 hours to ensure proper aggregation
    api_minute_data = []
    for hour_offset in range(2):
        hour = 10 + hour_offset
        for minute in range(60):
            base_price = 100.0 + hour_offset * 10 + minute * 0.1
            api_minute_data.append(
                {
                    "datetime": pd.Timestamp(f"2026-02-23 {hour:02d}:{minute:02d}:00"),
                    "date": datetime.date(2026, 2, 23),
                    "time": datetime.time(hour, minute, 0),
                    "open": base_price,
                    "high": base_price + 1.0,
                    "low": base_price - 1.0,
                    "close": base_price + 0.5,
                    "volume": 1000 + minute * 10,
                    "value": (1000 + minute * 10) * base_price,
                }
            )

    api_df = pd.DataFrame(api_minute_data)

    kis = SimpleNamespace(inquire_time_dailychartprice=AsyncMock(return_value=api_df))
    monkeypatch.setattr(_kis_api_module, "KISClient", lambda: kis)

    # Request 5 candles but only 2 in DB
    # API should fill the missing 2 candles (10:00, 11:00)
    # Plus current hour (14:00) built from minute candles
    out = await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=5,
        end_date=None,
        now_kst=now_kst,
    )

    # Verify we got 5 candles (2 from DB + 2 from API + 1 current)
    assert len(out) == 5, f"Expected 5 candles (DB + API + current), got {len(out)}"

    # Verify the datetime sequence (oldest to newest)
    datetimes = list(out["datetime"])
    assert len(datetimes) == 5

    # Should have candles for: 10:00, 11:00, 12:00, 13:00, 14:00
    # (10:00, 11:00 from API, 12:00, 13:00 from DB, 14:00 current)
    expected_buckets = [
        datetime.datetime(2026, 2, 23, 10, 0, 0),
        datetime.datetime(2026, 2, 23, 11, 0, 0),
        datetime.datetime(2026, 2, 23, 12, 0, 0),
        datetime.datetime(2026, 2, 23, 13, 0, 0),
        datetime.datetime(2026, 2, 23, 14, 0, 0),
    ]
    assert datetimes == expected_buckets, (
        f"Expected {expected_buckets}, got {datetimes}"
    )

    # Verify KIS API was called (DB had insufficient data)
    kis.inquire_time_dailychartprice.assert_awaited()

    # Verify DB data is preserved (12:00 and 13:00 hours from DB)
    row_12 = out[out["datetime"] == datetime.datetime(2026, 2, 23, 12, 0, 0)].iloc[0]
    assert row_12["open"] == 102.0, "DB data for 12:00 should be preserved"
    assert row_12["close"] == 103.0, "DB data for 12:00 should be preserved"

    row_13 = out[out["datetime"] == datetime.datetime(2026, 2, 23, 13, 0, 0)].iloc[0]
    assert row_13["open"] == 103.0, "DB data for 13:00 should be preserved"
    assert row_13["close"] == 104.0, "DB data for 13:00 should be preserved"

    # Verify API data filled the missing hours (10:00, 11:00)
    # Note: The aggregated hourly close is the last minute's close (minute 59)
    # For 10:00 hour: last minute (10:59) has close = 100.0 + 0*10 + 59*0.1 + 0.5 = 106.4
    # For 11:00 hour: last minute (11:59) has close = 100.0 + 1*10 + 59*0.1 + 0.5 = 116.4
    row_10 = out[out["datetime"] == datetime.datetime(2026, 2, 23, 10, 0, 0)].iloc[0]
    assert row_10["open"] == 100.0, "API data for 10:00 should be present"
    assert row_10["close"] == 106.4, (
        f"API data for 10:00 close should be 106.4, got {row_10['close']}"
    )

    row_11 = out[out["datetime"] == datetime.datetime(2026, 2, 23, 11, 0, 0)].iloc[0]
    assert row_11["open"] == 110.0, "API data for 11:00 should be present"
    assert row_11["close"] == 116.4, (
        f"API data for 11:00 close should be 116.4, got {row_11['close']}"
    )


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_1m_merges_same_minute_venues(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 0, 0),
            venue="KRX",
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=10.0,
            value=1000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 0, 0),
            venue="NTX",
            open=200.0,
            high=210.0,
            low=190.0,
            close=205.0,
            volume=5.0,
            value=500.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 1, 0),
            venue="KRX",
            open=106.0,
            high=111.0,
            low=101.0,
            close=107.0,
            volume=7.0,
            value=700.0,
        ),
    ]

    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=True,
        minute_rows=minute_rows,
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="1m",
        count=2,
        end_date=_dt_kst(2026, 2, 23, 0, 0, 0),
        now_kst=_dt_kst(2026, 2, 24, 10, 0, 0),
    )

    assert len(out) == 2
    first = out.iloc[0]
    second = out.iloc[1]
    assert first["datetime"] == datetime.datetime(2026, 2, 23, 9, 0, 0)
    assert first["open"] == pytest.approx(100.0)
    assert first["close"] == pytest.approx(105.0)
    assert first["volume"] == pytest.approx(15.0)
    assert first["value"] == pytest.approx(1500.0)
    assert first["venues"] == ["KRX", "NTX"]
    assert second["datetime"] == datetime.datetime(2026, 2, 23, 9, 1, 0)
    assert second["venues"] == ["KRX"]


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_5m_includes_current_partial_bucket(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 0, 0),
            venue="KRX",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
            value=1000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 1, 0),
            venue="KRX",
            open=100.5,
            high=102.0,
            low=100.0,
            close=101.0,
            volume=20.0,
            value=2000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 2, 0),
            venue="KRX",
            open=101.0,
            high=103.0,
            low=100.5,
            close=102.5,
            volume=30.0,
            value=3000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 5, 0),
            venue="KRX",
            open=103.0,
            high=104.0,
            low=102.0,
            close=103.5,
            volume=40.0,
            value=4000.0,
        ),
        _make_minute_row(
            time_kst=_dt_kst(2026, 2, 23, 9, 6, 0),
            venue="KRX",
            open=103.5,
            high=105.0,
            low=103.0,
            close=104.0,
            volume=50.0,
            value=5000.0,
        ),
    ]

    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=False,
        history_table="public.kr_candles_5m",
        history_rows=[],
        minute_rows=minute_rows,
        kis=kis,
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="5m",
        count=2,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 9, 7, 0),
    )

    assert len(out) == 2
    first = out.iloc[0]
    second = out.iloc[1]
    assert first["datetime"] == datetime.datetime(2026, 2, 23, 9, 0, 0)
    assert first["open"] == pytest.approx(100.0)
    assert first["close"] == pytest.approx(102.5)
    assert first["volume"] == pytest.approx(60.0)
    assert second["datetime"] == datetime.datetime(2026, 2, 23, 9, 5, 0)
    assert second["open"] == pytest.approx(103.0)
    assert second["close"] == pytest.approx(104.0)
    assert second["volume"] == pytest.approx(90.0)
    assert second["session"] == "REGULAR"
    assert second["venues"] == ["KRX"]


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_5m_overlay_starts_background_storage(
    monkeypatch,
):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 9, 7, 0)
    background_started = False
    background_completed = False
    background_calls: list[tuple[str, list[dict[str, object]]]] = []

    async def mock_store_background(*, symbol, minute_rows):
        nonlocal background_started, background_completed
        background_started = True
        background_calls.append((symbol, list(minute_rows)))
        await asyncio.sleep(0.2)
        background_completed = True

    overlay_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:05:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(9, 5, 0),
                "open": 103.0,
                "high": 104.0,
                "low": 102.0,
                "close": 103.5,
                "volume": 40.0,
                "value": 4000.0,
            },
            {
                "datetime": pd.Timestamp("2026-02-23 09:06:00"),
                "date": datetime.date(2026, 2, 23),
                "time": datetime.time(9, 6, 0),
                "open": 103.5,
                "high": 105.0,
                "low": 103.0,
                "close": 104.0,
                "volume": 50.0,
                "value": 5000.0,
            },
        ]
    )
    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=overlay_df)
    )
    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=False,
        history_table="public.kr_candles_5m",
        history_rows=[],
        minute_rows=[],
        kis=kis,
        store_background=mock_store_background,
    )

    start_time = datetime.datetime.now()
    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="5m",
        count=1,
        end_date=None,
        now_kst=now_kst,
    )
    elapsed_ms = (datetime.datetime.now() - start_time).total_seconds() * 1000

    assert len(out) == 1
    assert elapsed_ms < 250, (
        f"Function took {elapsed_ms}ms, should return immediately (< 250ms)"
    )

    await asyncio.sleep(0)
    assert background_started, "Overlay API rows should start background storage"
    assert background_completed is False
    assert len(background_calls) == 1
    stored_symbol, stored_rows = background_calls[0]
    assert stored_symbol == symbol
    assert len(stored_rows) == 2

    await asyncio.sleep(0.25)
    assert background_completed, "Background storage should complete after response"


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_5m_fallback_schedules_background_storage(
    monkeypatch,
):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    now_kst = _dt_kst(2026, 2, 23, 9, 7, 0)
    background_storage_calls: list[dict[str, object]] = []

    async def mock_store_background(*, symbol, minute_rows):
        background_storage_calls.append(
            {"symbol": symbol, "minute_rows": list(minute_rows)}
        )
        await asyncio.sleep(0)

    kis = SimpleNamespace(
        inquire_time_dailychartprice=AsyncMock(return_value=pd.DataFrame())
    )
    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=False,
        history_table="public.kr_candles_5m",
        history_rows=[],
        minute_rows=[],
        kis=kis,
        store_background=mock_store_background,
    )

    fallback_minute_rows = [
        _MinuteRow(
            minute_time=datetime.datetime(2026, 2, 23, 9, 5, 0),
            venue="KRX",
            open=103.0,
            high=104.0,
            low=102.0,
            close=103.5,
            volume=40.0,
            value=4000.0,
        ),
        _MinuteRow(
            minute_time=datetime.datetime(2026, 2, 23, 9, 6, 0),
            venue="KRX",
            open=103.5,
            high=105.0,
            low=103.0,
            close=104.0,
            volume=50.0,
            value=5000.0,
        ),
    ]
    monkeypatch.setattr(
        _intraday_module,
        "_fetch_historical_minutes_via_kis",
        AsyncMock(return_value=([], fallback_minute_rows)),
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="5m",
        count=1,
        end_date=None,
        now_kst=now_kst,
    )

    assert len(out) == 1
    await asyncio.sleep(0)
    assert len(background_storage_calls) == 1, (
        "Fallback API minute rows should be stored in the background"
    )
    stored_rows = cast(
        list[dict[str, object]], background_storage_calls[0]["minute_rows"]
    )
    assert background_storage_calls[0]["symbol"] == symbol
    assert len(stored_rows) == 2
    assert {row.get("venue") for row in stored_rows} == {"KRX"}


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_1m_mixed_history_and_aware_fallback_keeps_naive_kst(
    monkeypatch,
):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    now_kst = _dt_kst(2026, 3, 10, 8, 18, 0)
    db_minute_rows = [
        _make_minute_row(
            time_kst=_dt_kst(2026, 3, 9, 15, 59, 0),
            venue="KRX",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
            value=1000.0,
        )
    ]

    fallback_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-03-10 08:00:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 0, 0),
                "open": 101.0,
                "high": 102.0,
                "low": 100.5,
                "close": 101.5,
                "volume": 20.0,
                "value": 2000.0,
            },
            {
                "datetime": pd.Timestamp("2026-03-10 08:01:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 1, 0),
                "open": 101.5,
                "high": 103.0,
                "low": 101.0,
                "close": 102.5,
                "volume": 30.0,
                "value": 3000.0,
            },
        ]
    )

    async def mock_inquire(*, market, end_time, **_):
        if market == "NX" and end_time == "200000":
            return fallback_df
        return pd.DataFrame()

    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=True,
        minute_rows=db_minute_rows,
        kis=SimpleNamespace(
            inquire_time_dailychartprice=AsyncMock(side_effect=mock_inquire)
        ),
        store_background=AsyncMock(),
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="1m",
        count=20,
        end_date=None,
        now_kst=now_kst,
    )

    assert list(out["datetime"]) == [
        datetime.datetime(2026, 3, 9, 15, 59, 0),
        datetime.datetime(2026, 3, 10, 8, 0, 0),
        datetime.datetime(2026, 3, 10, 8, 1, 0),
    ]
    assert all(value.tzinfo is None for value in out["datetime"])
    assert out.iloc[0]["venues"] == ["KRX"]
    assert out.iloc[1]["venues"] == ["NTX"]
    assert out.iloc[2]["venues"] == ["NTX"]


def test_merge_overlay_into_intraday_frame_mixed_timezones_keeps_naive_kst():

    out = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-03-10 08:00:00+09:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 0, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
                "value": 1000.0,
                "session": "PRE_MARKET",
                "venues": ["KRX"],
            }
        ]
    )
    overlay_frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-03-10 08:00:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 0, 0),
                "open": 101.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.5,
                "volume": 20.0,
                "value": 2000.0,
                "session": "PRE_MARKET",
                "venues": ["NTX"],
            },
            {
                "datetime": pd.Timestamp("2026-03-10 08:01:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 1, 0),
                "open": 101.5,
                "high": 102.5,
                "low": 101.0,
                "close": 102.0,
                "volume": 30.0,
                "value": 3000.0,
                "session": "PRE_MARKET",
                "venues": ["NTX"],
            },
        ]
    )

    merged = _merge_overlay_into_intraday_frame(
        out=out,
        overlay_frame=overlay_frame,
        bucket_minutes=1,
    )

    assert list(merged["datetime"]) == [
        datetime.datetime(2026, 3, 10, 8, 0, 0),
        datetime.datetime(2026, 3, 10, 8, 1, 0),
    ]
    assert all(value.tzinfo is None for value in merged["datetime"])
    assert merged.iloc[0]["venues"] == ["NTX"]


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_5m_mixed_history_and_aware_fallback_keeps_naive_kst(
    monkeypatch,
):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    now_kst = _dt_kst(2026, 3, 10, 8, 19, 0)
    history_rows = [
        _make_hour_row(
            bucket_kst_naive=datetime.datetime(2026, 3, 9, 15, 55, 0),
            open=99.0,
            high=100.0,
            low=98.5,
            close=99.5,
            volume=120.0,
            value=12000.0,
            venues=["KRX"],
        )
    ]

    fallback_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-03-10 08:15:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 15, 0),
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
                "volume": 10.0,
                "value": 1000.0,
            },
            {
                "datetime": pd.Timestamp("2026-03-10 08:16:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 16, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.8,
                "close": 100.8,
                "volume": 20.0,
                "value": 2000.0,
            },
            {
                "datetime": pd.Timestamp("2026-03-10 08:17:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 17, 0),
                "open": 100.8,
                "high": 101.2,
                "low": 100.5,
                "close": 101.0,
                "volume": 30.0,
                "value": 3000.0,
            },
            {
                "datetime": pd.Timestamp("2026-03-10 08:18:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 18, 0),
                "open": 101.0,
                "high": 101.8,
                "low": 100.9,
                "close": 101.5,
                "volume": 40.0,
                "value": 4000.0,
            },
            {
                "datetime": pd.Timestamp("2026-03-10 08:19:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, 19, 0),
                "open": 101.5,
                "high": 102.0,
                "low": 101.3,
                "close": 101.9,
                "volume": 50.0,
                "value": 5000.0,
            },
        ]
    )

    async def mock_inquire(*, market, end_time, **_):
        if market == "NX" and end_time == "200000":
            return fallback_df
        return pd.DataFrame()

    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=True,
        history_table="public.kr_candles_5m",
        history_rows=history_rows,
        minute_rows=[],
        kis=SimpleNamespace(
            inquire_time_dailychartprice=AsyncMock(side_effect=mock_inquire)
        ),
        store_background=AsyncMock(),
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="5m",
        count=5,
        end_date=None,
        now_kst=now_kst,
    )

    assert list(out["datetime"]) == [
        datetime.datetime(2026, 3, 9, 15, 55, 0),
        datetime.datetime(2026, 3, 10, 8, 15, 0),
    ]
    assert all(value.tzinfo is None for value in out["datetime"])
    fallback_bucket = out.iloc[1]
    assert fallback_bucket["open"] == pytest.approx(100.0)
    assert fallback_bucket["close"] == pytest.approx(101.9)
    assert fallback_bucket["volume"] == pytest.approx(150.0)
    assert fallback_bucket["session"] == "PRE_MARKET"
    assert fallback_bucket["venues"] == ["NTX"]


@pytest.mark.asyncio
async def test_schedule_background_minute_storage_writes_utc_naive_time(monkeypatch):
    symbol = "005930"
    executed_params: list[dict[str, object]] = []
    commit_called = False

    class DummyDB:
        async def execute(self, query, params=None):
            sql = str(getattr(query, "text", query))
            if "INSERT INTO public.kr_candles_1m" not in sql:
                raise AssertionError(f"unexpected sql: {sql}")
            assert isinstance(params, dict)
            executed_params.append(dict(params))
            return None

        async def commit(self):
            nonlocal commit_called
            commit_called = True

    monkeypatch.setattr(
        _repo_module, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    _repo_module._schedule_background_minute_storage(
        symbol=symbol,
        minute_rows=[
            _MinuteRow(
                minute_time=datetime.datetime(2026, 3, 10, 8, 5, 0),
                venue="KRX",
                open=101.0,
                high=102.0,
                low=100.0,
                close=101.5,
                volume=10.0,
                value=1000.0,
            )
        ],
    )

    for _ in range(10):
        if executed_params:
            break
        await asyncio.sleep(0)

    assert commit_called is True
    assert len(executed_params) == 1
    assert executed_params[0]["symbol"] == symbol
    stored_time = cast(datetime.datetime, executed_params[0]["time"])
    assert stored_time == datetime.datetime(2026, 3, 9, 23, 5, 0)
    assert stored_time.tzinfo is None


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_1m_pure_kis_overlay_and_fallback_keep_naive_kst(
    monkeypatch,
):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    now_kst = _dt_kst(2026, 3, 10, 8, 18, 0)

    overlay_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp(f"2026-03-10 08:{minute:02d}:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, minute, 0),
                "open": 100.0 + minute * 0.1,
                "high": 100.5 + minute * 0.1,
                "low": 99.5 + minute * 0.1,
                "close": 100.2 + minute * 0.1,
                "volume": 10.0 + minute,
                "value": 1000.0 + minute * 100.0,
            }
            for minute in range(19)
        ]
    )
    fallback_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp(f"2026-03-10 08:{minute:02d}:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, minute, 0),
                "open": 101.0 + minute * 0.1,
                "high": 101.5 + minute * 0.1,
                "low": 100.5 + minute * 0.1,
                "close": 101.2 + minute * 0.1,
                "volume": 20.0 + minute,
                "value": 2000.0 + minute * 100.0,
            }
            for minute in range(20)
        ]
    )

    async def mock_inquire(*, market, end_time, **_):
        if market != "NX":
            return pd.DataFrame()
        if end_time == "081900":
            return overlay_df
        if end_time == "200000":
            return fallback_df
        return pd.DataFrame()

    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=True,
        minute_rows=[],
        kis=SimpleNamespace(
            inquire_time_dailychartprice=AsyncMock(side_effect=mock_inquire)
        ),
        store_background=AsyncMock(),
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="1m",
        count=20,
        end_date=None,
        now_kst=now_kst,
    )

    assert len(out) == 20
    assert out.iloc[0]["datetime"] == datetime.datetime(2026, 3, 10, 8, 0, 0)
    assert out.iloc[-1]["datetime"] == datetime.datetime(2026, 3, 10, 8, 19, 0)
    assert all(value.tzinfo is None for value in out["datetime"])
    assert all(venues == ["NTX"] for venues in out["venues"])


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_5m_pure_kis_overlay_and_fallback_keep_naive_kst(
    monkeypatch,
):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"
    now_kst = _dt_kst(2026, 3, 10, 8, 19, 0)

    overlay_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp(f"2026-03-10 08:{minute:02d}:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, minute, 0),
                "open": 100.0 + minute * 0.1,
                "high": 100.5 + minute * 0.1,
                "low": 99.5 + minute * 0.1,
                "close": 100.2 + minute * 0.1,
                "volume": 10.0 + minute,
                "value": 1000.0 + minute * 100.0,
            }
            for minute in range(19)
        ]
    )
    fallback_df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp(f"2026-03-10 08:{minute:02d}:00"),
                "date": datetime.date(2026, 3, 10),
                "time": datetime.time(8, minute, 0),
                "open": 101.0 + minute * 0.1,
                "high": 101.5 + minute * 0.1,
                "low": 100.5 + minute * 0.1,
                "close": 101.2 + minute * 0.1,
                "volume": 20.0 + minute,
                "value": 2000.0 + minute * 100.0,
            }
            for minute in range(20)
        ]
    )

    async def mock_inquire(*, market, end_time, **_):
        if market != "NX":
            return pd.DataFrame()
        if end_time == "082000":
            return overlay_df
        if end_time == "200000":
            return fallback_df
        return pd.DataFrame()

    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=True,
        history_table="public.kr_candles_5m",
        history_rows=[],
        minute_rows=[],
        kis=SimpleNamespace(
            inquire_time_dailychartprice=AsyncMock(side_effect=mock_inquire)
        ),
        store_background=AsyncMock(),
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="5m",
        count=5,
        end_date=None,
        now_kst=now_kst,
    )

    assert list(out["datetime"]) == [
        datetime.datetime(2026, 3, 10, 8, 0, 0),
        datetime.datetime(2026, 3, 10, 8, 5, 0),
        datetime.datetime(2026, 3, 10, 8, 10, 0),
        datetime.datetime(2026, 3, 10, 8, 15, 0),
    ]
    assert all(value.tzinfo is None for value in out["datetime"])
    assert out.iloc[-1]["close"] == pytest.approx(103.1)
    assert out.iloc[-1]["venues"] == ["NTX"]


@pytest.mark.asyncio
async def test_read_kr_intraday_candles_db_only_does_not_schedule_background_storage(
    monkeypatch,
):
    from app.services import kr_hourly_candles_read_service as svc

    symbol = "005930"

    store_mock = AsyncMock()
    _patch_intraday_mocks(
        monkeypatch,
        symbol=symbol,
        nxt_eligible=False,
        history_table="public.kr_candles_5m",
        history_rows=[
            _make_hour_row(
                bucket_kst_naive=datetime.datetime(2026, 2, 21, 9, 0, 0),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1200.0,
                value=120000.0,
                venues=["KRX"],
            )
        ],
        minute_rows=[],
        store_background=store_mock,
    )

    out = await svc.read_kr_intraday_candles(
        symbol=symbol,
        period="5m",
        count=1,
        end_date=_dt_kst(2026, 2, 21, 0, 0, 0),
        now_kst=_dt_kst(2026, 2, 23, 9, 7, 0),
    )

    assert len(out) == 1
    store_mock.assert_not_awaited()
