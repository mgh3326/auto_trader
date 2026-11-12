from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

import pytest

from app.services.market_data_coverage.ohlcv_freshness import kr_candles_freshness

LOGGER_NAME = "app.services.market_data_coverage.ohlcv_freshness"
TRADING_DAY = dt.date(2026, 1, 12)
LATEST_TIME = dt.datetime(2026, 1, 9, 6, 30, tzinfo=dt.UTC)
LATEST_DATE = dt.date(2026, 1, 9)


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def one(self) -> object:
        return self._row


class _SavepointContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _SavepointContext:
        self._session.savepoints_entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._session.savepoint_exit_types.append(exc_type)
        return False


class _FakeSession:
    def __init__(
        self,
        *,
        row: object | None = None,
        begin_error: BaseException | None = None,
        execute_error: BaseException | None = None,
    ) -> None:
        self.row = row
        self.begin_error = begin_error
        self.execute_error = execute_error
        self.savepoints_entered = 0
        self.savepoint_exit_types: list[type[BaseException] | None] = []
        self.rollback_calls = 0

    def begin_nested(self) -> _SavepointContext:
        if self.begin_error is not None:
            raise self.begin_error
        return _SavepointContext(self)

    async def execute(self, statement: object, params: object = None) -> _Result:
        if self.execute_error is not None:
            raise self.execute_error
        return _Result(self.row)

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_savepoint_refusal_returns_error_without_rollback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _FakeSession(begin_error=RuntimeError("savepoint refused"))

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        result = await kr_candles_freshness(session, trading_day=TRADING_DAY)

    assert result == ("error", None, None, 0, 0)
    assert session.rollback_calls == 0
    assert "RuntimeError" in caplog.text
    assert "savepoint refused" not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_select_exits_savepoint_and_returns_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _FakeSession(execute_error=ValueError("bad sql text"))

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        result = await kr_candles_freshness(session, trading_day=TRADING_DAY)

    assert result == ("error", None, None, 0, 0)
    assert session.savepoints_entered == 1
    assert session.savepoint_exit_types == [ValueError]
    assert session.rollback_calls == 0
    assert "ValueError" in caplog.text
    assert "bad sql text" not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fresh", "stale", "expected_symbols", "state"),
    [
        (3, 0, None, "fresh"),
        (0, 0, None, "missing"),
        (0, 2, None, "stale"),
        (1, 2, None, "partial"),
        (1, 0, 5, "partial"),
        (0, 2, 5, "stale"),
    ],
)
async def test_normal_path_maps_counts_to_state(
    fresh: int, stale: int, expected_symbols: int | None, state: str
) -> None:
    session = _FakeSession(
        row=SimpleNamespace(
            latest_time=LATEST_TIME,
            latest_date=LATEST_DATE,
            fresh_symbols=fresh,
            stale_symbols=stale,
        )
    )

    result = await kr_candles_freshness(
        session, trading_day=TRADING_DAY, expected_symbols=expected_symbols
    )

    if state == "missing":
        assert result == ("missing", None, None, 0, 0)
    else:
        assert result == (state, LATEST_TIME, LATEST_DATE, fresh, stale)
    assert session.savepoints_entered == 1
    assert session.savepoint_exit_types == [None]
    assert session.rollback_calls == 0
