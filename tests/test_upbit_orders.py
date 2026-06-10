"""Tests for Upbit order query helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.unit
class TestUpbitClosedOrders:
    @pytest.mark.asyncio
    async def test_closed_orders_passes_time_window_and_state_filters(
        self, monkeypatch
    ):
        from datetime import UTC, datetime

        from app.services.brokers.upbit import orders

        request = AsyncMock(return_value=[])
        monkeypatch.setattr(orders._client, "_request_with_auth", request)

        result = await orders.fetch_closed_orders(
            market="KRW-BTC",
            limit=1500,
            states=["done"],
            order_by="asc",
            start_time=datetime(2026, 2, 1, 0, 0, tzinfo=UTC),
            end_time=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
        )

        assert result == []
        request.assert_awaited_once()
        method, url = request.await_args.args
        assert method == "GET"
        assert url.endswith("/orders/closed")
        assert request.await_args.kwargs["query_params"] == {
            "states[]": ["done"],
            "limit": 1000,
            "order_by": "asc",
            "market": "KRW-BTC",
            "start_time": "2026-02-01T00:00:00+00:00",
            "end_time": "2026-02-07T00:00:00+00:00",
        }
