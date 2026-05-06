"""Tests for intraday order review background tasks."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.tasks.intraday_order_review_tasks import (
    _is_kr_trading_hours,
    _is_us_trading_hours,
    intraday_crypto_order_review,
)


class TestTradingHoursCheck:
    def test_kr_trading_hours_weekday(self):
        dt = datetime(2026, 3, 16, 10, 0)
        assert _is_kr_trading_hours(dt) is True

    def test_kr_trading_hours_weekend(self):
        dt = datetime(2026, 3, 15, 10, 0)
        assert _is_kr_trading_hours(dt) is False

    def test_kr_trading_hours_before_open(self):
        dt = datetime(2026, 3, 16, 8, 0)
        assert _is_kr_trading_hours(dt) is False

    def test_us_trading_hours_late_night(self):
        dt = datetime(2026, 3, 16, 0, 30)
        assert _is_us_trading_hours(dt) is True

    def test_us_trading_hours_early_morning(self):
        dt = datetime(2026, 3, 16, 4, 0)
        assert _is_us_trading_hours(dt) is True

    def test_us_trading_hours_daytime(self):
        dt = datetime(2026, 3, 16, 12, 0)
        assert _is_us_trading_hours(dt) is False


class TestIntradayCryptoReview:
    @pytest.mark.asyncio
    async def test_returns_order_count(self):
        mock_result = {
            "summary": {"total": 3},
            "orders": [
                {
                    "symbol": "BTC",
                    "side": "buy",
                    "gap_pct": -2.1,
                    "indicators": {"rsi_14": 55.0},
                },
            ],
        }

        with patch(
            "app.tasks.intraday_order_review_tasks.fetch_pending_orders",
            AsyncMock(return_value=mock_result),
        ):
            result = await intraday_crypto_order_review()

        assert result["market"] == "crypto"
        assert result["order_count"] == 3
        assert len(result["orders"]) == 1
        assert result["orders"][0]["indicators"] == pytest.approx({"rsi_14": 55.0})
