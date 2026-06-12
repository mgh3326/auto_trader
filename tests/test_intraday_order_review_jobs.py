"""Unit tests for app.jobs.intraday_order_review (jobs/ layer).

These test the orchestration functions directly, independent of TaskIQ.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

# Imports will FAIL until Task 2 creates the module.
from app.jobs.intraday_order_review import (
    is_kr_trading_hours,
    is_us_trading_hours,
    run_crypto_order_review,
    run_kr_order_review,
    run_us_order_review,
)


class TestTradingHoursHelpers:
    def test_kr_trading_hours_open_on_weekday(self) -> None:
        dt = datetime(2026, 3, 16, 10, 0)  # Monday 10:00
        assert is_kr_trading_hours(dt) is True

    def test_kr_trading_hours_closed_on_weekend(self) -> None:
        dt = datetime(2026, 3, 15, 10, 0)  # Sunday
        assert is_kr_trading_hours(dt) is False

    def test_kr_trading_hours_closed_before_open(self) -> None:
        dt = datetime(2026, 3, 16, 8, 0)  # Monday 08:00
        assert is_kr_trading_hours(dt) is False

    def test_kr_trading_hours_closed_after_close(self) -> None:
        dt = datetime(2026, 3, 16, 16, 0)  # Monday 16:00
        assert is_kr_trading_hours(dt) is False

    def test_us_trading_hours_open_regular_session_dst(self) -> None:
        # 2026-03-16 22:45 KST == 2026-03-16 09:45 ET.
        dt_value = datetime(2026, 3, 16, 22, 45)
        assert is_us_trading_hours(dt_value) is True

    def test_us_trading_hours_open_regular_session_standard_time(self) -> None:
        # 2026-01-05 23:45 KST == 2026-01-05 09:45 ET.
        dt_value = datetime(2026, 1, 5, 23, 45)
        assert is_us_trading_hours(dt_value) is True

    def test_us_trading_hours_closed_kst_daytime(self) -> None:
        dt_value = datetime(2026, 3, 16, 12, 0)
        assert is_us_trading_hours(dt_value) is False

    def test_us_trading_hours_closed_on_us_holiday(self) -> None:
        # 2026-07-03 is the observed Independence Day market holiday.
        dt_value = datetime(2026, 7, 3, 23, 45)
        assert is_us_trading_hours(dt_value) is False

    def test_kr_trading_hours_closed_on_xkrx_holiday(self) -> None:
        # Children's Day in Korea.
        dt_value = datetime(2026, 5, 5, 10, 0)
        assert is_kr_trading_hours(dt_value) is False


class TestRunCryptoOrderReview:
    @pytest.mark.asyncio
    async def test_returns_order_count_and_orders(self) -> None:
        mock_result = {
            "summary": {"total": 2},
            "orders": [
                {
                    "symbol": "KRW-BTC",
                    "side": "buy",
                    "gap_pct": -1.5,
                    "indicators": {"rsi_14": 48.0},
                }
            ],
        }
        with patch(
            "app.jobs.intraday_order_review.fetch_pending_orders",
            AsyncMock(return_value=mock_result),
        ):
            result = await run_crypto_order_review()
        assert result["market"] == "crypto"
        assert result["order_count"] == 2
        assert len(result["orders"]) == 1
        assert result["orders"][0]["symbol"] == "KRW-BTC"

    @pytest.mark.asyncio
    async def test_calls_fetch_with_correct_params(self) -> None:
        with patch(
            "app.jobs.intraday_order_review.fetch_pending_orders",
            AsyncMock(return_value={"summary": {"total": 0}, "orders": []}),
        ) as mock_fetch:
            await run_crypto_order_review()
        call_kwargs = mock_fetch.call_args.kwargs
        assert call_kwargs["market"] == "crypto"
        assert call_kwargs["include_current_price"] is True
        assert call_kwargs["include_indicators"] is True


class TestRunKrOrderReview:
    @pytest.mark.asyncio
    async def test_skips_outside_trading_hours(self) -> None:
        # Patch now_kst to return a Sunday
        fake_dt = datetime(2026, 3, 15, 10, 0)  # Sunday
        with patch("app.jobs.intraday_order_review.now_kst", return_value=fake_dt):
            result = await run_kr_order_review()
        assert result["skipped"] is True
        assert result["reason"] == "outside_trading_hours"

    @pytest.mark.asyncio
    async def test_runs_during_trading_hours(self) -> None:
        fake_dt = datetime(2026, 3, 16, 10, 0)  # Monday 10:00
        mock_result = {"summary": {"total": 3}, "orders": []}
        with (
            patch("app.jobs.intraday_order_review.now_kst", return_value=fake_dt),
            patch(
                "app.jobs.intraday_order_review.fetch_pending_orders",
                AsyncMock(return_value=mock_result),
            ),
        ):
            result = await run_kr_order_review()
        assert result["market"] == "kr"
        assert result["order_count"] == 3


class TestRunUsOrderReview:
    @pytest.mark.asyncio
    async def test_skips_outside_trading_hours(self) -> None:
        fake_dt = datetime(2026, 3, 16, 12, 0)  # Monday noon KST
        with patch("app.jobs.intraday_order_review.now_kst", return_value=fake_dt):
            result = await run_us_order_review()
        assert result["skipped"] is True
        assert result["reason"] == "outside_trading_hours"

    @pytest.mark.asyncio
    async def test_runs_during_trading_hours(self) -> None:
        fake_dt = datetime(2026, 3, 16, 22, 45)
        mock_result = {"summary": {"total": 1}, "orders": []}
        with (
            patch("app.jobs.intraday_order_review.now_kst", return_value=fake_dt),
            patch(
                "app.jobs.intraday_order_review.fetch_pending_orders",
                AsyncMock(return_value=mock_result),
            ),
        ):
            result = await run_us_order_review()
        assert result["market"] == "us"
        assert result["order_count"] == 1
