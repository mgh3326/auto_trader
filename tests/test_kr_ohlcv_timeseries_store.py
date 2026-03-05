from __future__ import annotations

import datetime

import pandas as pd
import pytest

from app.core.config import settings
from app.services import kr_ohlcv_timeseries_store


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DummySession:
    def __init__(self, values: list[object | None]):
        self._values = list(values)
        self._index = 0
        self.committed = False

    async def execute(self, statement, params=None):
        del statement, params
        if self._index < len(self._values):
            value = self._values[self._index]
        else:
            value = None
        self._index += 1
        return _ScalarResult(value)

    async def commit(self):
        self.committed = True


class _DummySessionManager:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return None


class _RecordingSession:
    def __init__(self, *, cagg_value: object | None, minute_value: object | None):
        self.cagg_value = cagg_value
        self.minute_value = minute_value
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        del params
        sql = str(statement)
        self.statements.append(sql)

        if "FROM market_candles_1h_kr" in sql:
            return _ScalarResult(self.cagg_value)
        if "FROM market_candles_1m_kr" in sql:
            return _ScalarResult(self.minute_value)
        return _ScalarResult(None)

    async def commit(self):
        return None


class _CaptureWriteSession:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        del params
        self.statements.append(str(statement))
        return _ScalarResult(None)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_ensure_timescale_ready_bypasses_in_test_env(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "test", raising=False)

    def _should_not_open_session():
        raise AssertionError("AsyncSessionLocal should not be called in test env")

    monkeypatch.setattr(
        kr_ohlcv_timeseries_store,
        "AsyncSessionLocal",
        _should_not_open_session,
    )

    await kr_ohlcv_timeseries_store.ensure_timescale_ready()


@pytest.mark.asyncio
async def test_ensure_timescale_ready_fails_when_extension_missing(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production", raising=False)
    dummy = _DummySession(values=[None])
    monkeypatch.setattr(
        kr_ohlcv_timeseries_store,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(dummy),
    )

    with pytest.raises(RuntimeError, match="TimescaleDB extension is not installed"):
        await kr_ohlcv_timeseries_store.ensure_timescale_ready(allow_test_bypass=False)


@pytest.mark.asyncio
async def test_ensure_timescale_ready_fails_when_minute_table_missing(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production", raising=False)
    dummy = _DummySession(values=["timescaledb", None])
    monkeypatch.setattr(
        kr_ohlcv_timeseries_store,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(dummy),
    )

    with pytest.raises(RuntimeError, match="market_candles_1m_kr table is missing"):
        await kr_ohlcv_timeseries_store.ensure_timescale_ready(allow_test_bypass=False)


@pytest.mark.asyncio
async def test_upsert_market_candles_1m_returns_zero_for_empty_frame():
    result = await kr_ohlcv_timeseries_store.upsert_market_candles_1m(
        symbol="005930",
        exchange="KRX",
        route="J",
        frame=pd.DataFrame(),
    )

    assert result == {
        "rows": 0,
        "min_ts": None,
        "max_ts": None,
    }


@pytest.mark.asyncio
async def test_upsert_market_candles_1m_invalid_exchange_to_quarantine(monkeypatch):
    session = _CaptureWriteSession()
    monkeypatch.setattr(
        kr_ohlcv_timeseries_store,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(session),
    )

    frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-19 09:01:00"),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 100,
                "value": 10050,
            }
        ]
    )

    result = await kr_ohlcv_timeseries_store.upsert_market_candles_1m(
        symbol="005930",
        exchange="BADX",
        route="J",
        frame=frame,
    )

    assert result["rows"] == 0
    assert result["quarantined_rows"] == 1
    assert any(
        "INSERT INTO market_candles_ingest_quarantine" in stmt
        for stmt in session.statements
    )


def test_frame_from_hour_rows_builds_dataframe():
    frame = kr_ohlcv_timeseries_store.frame_from_hour_rows(
        [
            {
                "datetime": pd.Timestamp("2026-02-19 09:00:00"),
                "date": pd.Timestamp("2026-02-19").date(),
                "time": pd.Timestamp("2026-02-19 09:00:00").time(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 100,
                "value": 10050,
            }
        ]
    )

    assert len(frame) == 1
    assert set(frame.columns) == {
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    }


@pytest.mark.asyncio
async def test_fetch_market_candles_1h_kr_returns_empty_when_range_invalid():
    start_bucket = pd.Timestamp("2026-02-19 12:00:00").to_pydatetime()
    end_bucket = pd.Timestamp("2026-02-19 10:00:00").to_pydatetime()

    frame = await kr_ohlcv_timeseries_store.fetch_market_candles_1h_kr(
        symbol="005930",
        start_bucket=start_bucket,
        end_bucket=end_bucket,
    )

    assert frame.empty


@pytest.mark.asyncio
async def test_fetch_previous_close_before_bucket_prefers_hour_cagg(monkeypatch):
    session = _RecordingSession(cagg_value=101.25, minute_value=88.0)
    monkeypatch.setattr(
        kr_ohlcv_timeseries_store,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(session),
    )

    close = await kr_ohlcv_timeseries_store.fetch_previous_close_before_bucket(
        symbol="005930",
        before_bucket=datetime.datetime(2026, 2, 19, 10, 0),
    )

    assert close == 101.25
    assert any("FROM market_candles_1h_kr" in sql for sql in session.statements)
    assert not any("FROM market_candles_1m_kr" in sql for sql in session.statements)


@pytest.mark.asyncio
async def test_fetch_previous_close_before_bucket_uses_minute_fallback_tiebreak(
    monkeypatch,
):
    session = _RecordingSession(cagg_value=None, minute_value=77.7)
    monkeypatch.setattr(
        kr_ohlcv_timeseries_store,
        "AsyncSessionLocal",
        lambda: _DummySessionManager(session),
    )

    close = await kr_ohlcv_timeseries_store.fetch_previous_close_before_bucket(
        symbol="005930",
        before_bucket=datetime.datetime(2026, 2, 19, 10, 0),
    )

    assert close == 77.7
    minute_sql = next(
        sql for sql in session.statements if "FROM market_candles_1m_kr" in sql
    )
    assert "CASE WHEN exchange = 'KRX' THEN 1 ELSE 0 END DESC" in minute_sql
