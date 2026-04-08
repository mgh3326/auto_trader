# tests/test_ohlcv_cache_common.py
"""Tests for the shared OHLCV cache utility module."""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest

from app.services import ohlcv_cache_common as common


class TestToJsonValue:
    def test_nan_returns_none(self):
        assert common._to_json_value(float("nan")) is None

    def test_int_returns_float(self):
        assert common._to_json_value(42) == 42.0

    def test_float_passthrough(self):
        assert common._to_json_value(3.14) == 3.14

    def test_string_passthrough(self):
        assert common._to_json_value("hello") == "hello"

    def test_none_returns_none(self):
        assert common._to_json_value(None) is None


class TestNormalizeBool:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            (None, False),
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
            ("yes", True),
            ("on", True),
            ("off", False),
        ],
    )
    def test_values(self, value, expected):
        assert common._normalize_bool(value) is expected


class TestEpochDay:
    def test_known_date(self):
        result = common._epoch_day(date(2026, 2, 14))
        expected = int(datetime(2026, 2, 14, tzinfo=UTC).timestamp() // 86400)
        assert result == expected

    def test_epoch_origin(self):
        assert common._epoch_day(date(1970, 1, 1)) == 0


class TestEmptyDataframe:
    def test_has_correct_columns(self):
        df = common._empty_dataframe()
        assert list(df.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "value",
        ]
        assert len(df) == 0


class TestAcquireLock:
    @pytest.mark.asyncio
    async def test_acquire_succeeds(self):
        redis_client = AsyncMock()
        redis_client.set = AsyncMock(return_value=True)
        token = await common._acquire_lock(redis_client, "lock:test", 10)
        assert token is not None
        redis_client.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_acquire_fails_when_held(self):
        redis_client = AsyncMock()
        redis_client.set = AsyncMock(return_value=False)
        token = await common._acquire_lock(redis_client, "lock:test", 10)
        assert token is None


class TestReleaseLock:
    @pytest.mark.asyncio
    async def test_release_calls_eval(self):
        redis_client = AsyncMock()
        await common._release_lock(redis_client, "lock:test", "tok-123")
        redis_client.eval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_swallows_errors(self):
        redis_client = AsyncMock()
        redis_client.eval = AsyncMock(side_effect=RuntimeError("oops"))
        await common._release_lock(redis_client, "lock:test", "tok-123")


class TestEnforceRetentionLimit:
    @pytest.mark.asyncio
    async def test_no_overflow_returns_zero(self):
        redis_client = AsyncMock()
        redis_client.zcard = AsyncMock(return_value=5)
        result = await common._enforce_retention_limit(
            redis_client, "dates", "rows", 10
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_zero_max_returns_zero(self):
        redis_client = AsyncMock()
        result = await common._enforce_retention_limit(redis_client, "dates", "rows", 0)
        assert result == 0


class TestRefreshMeta:
    @pytest.mark.asyncio
    async def test_uses_custom_meta_date_field(self):
        redis_client = AsyncMock()
        redis_client.zrange = AsyncMock(return_value=["2026-01-01"])
        redis_client.hset = AsyncMock()

        await common._refresh_meta(
            redis_client,
            "dates",
            "meta",
            date(2026, 2, 14),
            True,
            meta_date_field="last_closed_bucket",
        )

        call_args = redis_client.hset.call_args
        mapping = call_args.kwargs.get("mapping") or call_args[1].get("mapping")
        assert "last_closed_bucket" in mapping
        assert "last_closed_date" not in mapping

    @pytest.mark.asyncio
    async def test_default_meta_date_field(self):
        redis_client = AsyncMock()
        redis_client.zrange = AsyncMock(return_value=["2026-01-01"])
        redis_client.hset = AsyncMock()

        await common._refresh_meta(
            redis_client,
            "dates",
            "meta",
            date(2026, 2, 14),
            False,
        )

        call_args = redis_client.hset.call_args
        mapping = call_args.kwargs.get("mapping") or call_args[1].get("mapping")
        assert "last_closed_date" in mapping
