"""Tests for Upbit order query helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.unit
class TestUpbitClosedOrders:
    @pytest.mark.asyncio
    async def test_closed_orders_passes_pagination_and_state_filters(self, monkeypatch):
        from app.services.brokers.upbit import orders

        request = AsyncMock(return_value=[])
        monkeypatch.setattr(orders._client, "_request_with_auth", request)

        result = await orders.fetch_closed_orders(
            market="KRW-BTC",
            limit=500,
            page=3,
            states=["done"],
            order_by="asc",
        )

        assert result == []
        request.assert_awaited_once()
        method, url = request.await_args.args
        assert method == "GET"
        assert url.endswith("/orders/closed")
        assert request.await_args.kwargs["query_params"] == {
            "states[]": ["done"],
            "limit": 100,
            "page": 3,
            "order_by": "asc",
            "market": "KRW-BTC",
        }
