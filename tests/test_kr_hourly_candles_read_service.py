from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest


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
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: DummySessionManager(db))

    class DummyKIS:
        def __init__(self):
            self.calls: list[str] = []

        async def inquire_minute_chart(
            self, *, code, market, time_unit, n, end_date=None
        ):
            del code, time_unit, n, end_date
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
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
    assert sorted(kis.calls) == ["J", "NX"]
    kis.calls.clear()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 15, 34, 0),
    )
    assert sorted(kis.calls) == ["J", "NX"]
    kis.calls.clear()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 15, 35, 0),
    )
    assert kis.calls == ["NX"]
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

    monkeypatch.setattr(
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = SimpleNamespace(inquire_minute_chart=AsyncMock())
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    # 08:00-09:00 NX-only time window but nxt_eligible=false -> API 0
    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 8, 10, 0),
    )
    kis.inquire_minute_chart.assert_not_awaited()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
    )
    kis.inquire_minute_chart.assert_awaited_once()
    assert kis.inquire_minute_chart.await_args.kwargs["market"] == "J"

    kis.inquire_minute_chart.reset_mock()

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 16, 0, 0),
    )
    kis.inquire_minute_chart.assert_not_awaited()


@pytest.mark.asyncio
async def test_end_date_in_past_disables_api(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = SimpleNamespace(inquire_minute_chart=AsyncMock())
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    await svc.read_kr_hourly_candles_1h(
        symbol=symbol,
        count=1,
        end_date=_dt_kst(2026, 2, 20, 0, 0, 0),
        now_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
    )

    kis.inquire_minute_chart.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_hour_is_reaggregated_from_minutes_not_from_db_hour(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
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

    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=api_df))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
async def test_db_insufficient_rows_raises(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )
    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    with pytest.raises(ValueError, match="DB does not have enough KR 1h candles"):
        await svc.read_kr_hourly_candles_1h(
            symbol=symbol,
            count=2,
            end_date=None,
            now_kst=_dt_kst(2026, 2, 23, 10, 0, 0),
        )


@pytest.mark.asyncio
async def test_api_partial_failure_raises(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    async def _fail_on_nx(*, market, **_):
        if market == "NX":
            raise RuntimeError("NX failed")
        return pd.DataFrame()

    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(side_effect=_fail_on_nx))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

    with pytest.raises(RuntimeError, match="NX failed"):
        await svc.read_kr_hourly_candles_1h(
            symbol=symbol,
            count=1,
            end_date=None,
            now_kst=now_kst,
        )


@pytest.mark.asyncio
async def test_db_first_returns_existing_data(monkeypatch):
    """Test that DB-first query returns existing data without calling KIS API."""
    from app.services import kr_hourly_candles_read_service as svc

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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    # Mock KIS API to track calls
    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
    kis.inquire_minute_chart.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_to_kis_api_when_db_empty(monkeypatch):
    """Test fallback to KIS API when DB is empty."""
    from app.services import kr_hourly_candles_read_service as svc

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
            raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=api_df))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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
    kis.inquire_minute_chart.assert_awaited()
    assert kis.inquire_minute_chart.await_args.kwargs["market"] == "J"


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
                "volume": 1000 + i * 10,  # Range: 1000-1590, sum should be 1000+1010+...+1590
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
    assert row["volume"] == expected_volume, f"Expected volume={expected_volume}, got {row['volume']}"

    # Verify datetime is floored to hour
    assert row["datetime"] == pd.Timestamp("2026-02-23 09:00:00")


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
        svc, "AsyncSessionLocal", lambda: DummySessionManager(DummyDB())
    )

    # Mock _store_minute_candles_background with delay to simulate slow DB write
    async def mock_store_background(symbol_arg, minute_rows):
        nonlocal background_storage_started, background_storage_completed, background_storage_args
        background_storage_started = True
        background_storage_args = (symbol_arg, minute_rows)
        # Simulate slow DB operation (200ms)
        await asyncio.sleep(0.2)
        background_storage_completed = True

    monkeypatch.setattr(svc, "_store_minute_candles_background", mock_store_background)

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
    kis = SimpleNamespace(inquire_minute_chart=AsyncMock(return_value=api_df))
    monkeypatch.setattr(svc, "KISClient", lambda: kis)

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

    # Verify function returned quickly (< 100ms, much less than background storage's 200ms)
    assert elapsed_ms < 100, f"Function took {elapsed_ms}ms, should return immediately (< 100ms)"

    # Verify function returned data
    assert len(out) > 0, "Function should return data"

    # At this point, background storage started but may not have completed (non-blocking)
    assert background_storage_started, "Background storage should have been started"

    # Wait for background task to complete and verify it finishes
    await asyncio.sleep(0.3)
    assert background_storage_completed, "Background storage should complete after main function returns"

    # Verify background storage was called with correct arguments
    assert len(background_storage_args) == 2, "Background storage should receive symbol and minute_rows"
    assert background_storage_args[0] == symbol, f"Background storage symbol should be {symbol}"
    assert len(background_storage_args[1]) > 0, "Background storage should receive minute candles"
