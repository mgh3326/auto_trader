"""Unit tests for the daily-candle fetch_recent SQL builders and time-floor helper.

These are DB-free: they assert the shape of the generated SQL and the window
sizing of the bounded-time predicate (ROB-812).
"""

from datetime import UTC, datetime

import pytest

from app.services.daily_candles.repository import (
    _build_kr_us_recent_sql,
    _recent_time_floor,
)


@pytest.mark.unit
def test_recent_time_floor_uses_generous_window_floor():
    now = datetime(2026, 7, 10, tzinfo=UTC)
    # count=200 → 200*3 = 600 days (exceeds the 400-day floor)
    floor = _recent_time_floor(200, now=now)
    assert (now - floor).days == 600  # 200 * 3


@pytest.mark.unit
def test_recent_time_floor_scales_with_large_count():
    now = datetime(2026, 7, 10, tzinfo=UTC)
    # count=50 → max(400, 150) = 400-day floor
    floor = _recent_time_floor(50, now=now)
    assert (now - floor).days == 400  # max(400, 150)


@pytest.mark.unit
def test_kr_us_recent_sql_carries_time_floor_predicate():
    sql = _build_kr_us_recent_sql("venue", "NULL AS adj_close, ")
    assert "time >= :time_floor" in sql
    assert "ORDER BY time DESC" in sql
    assert "LIMIT :count" in sql
    assert "venue = :partition" in sql


@pytest.mark.unit
def test_crypto_recent_sql_carries_time_floor_predicate():
    from app.services.daily_candles.repository import _CRYPTO_RECENT_SQL

    assert "time >= :time_floor" in _CRYPTO_RECENT_SQL
    assert "instrument_id = :iid" in _CRYPTO_RECENT_SQL
    assert "ORDER BY time DESC" in _CRYPTO_RECENT_SQL
    assert "LIMIT :count" in _CRYPTO_RECENT_SQL
