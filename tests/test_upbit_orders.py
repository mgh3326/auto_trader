"""Tests for Upbit order query helpers."""

from __future__ import annotations

import uuid
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


@pytest.mark.unit
class TestUpbitOrderIdempotencyIdentifier:
    """ROB-645: every Upbit order-creation POST carries a unique client identifier
    (idempotency key) so a resent/duplicate order is rejected by the broker."""

    def _mock_request(self, monkeypatch):
        from app.services.brokers.upbit import orders

        request = AsyncMock(return_value={"uuid": "server-uuid"})
        monkeypatch.setattr(orders._client, "_request_with_auth", request)
        return orders, request

    def _assert_valid_identifier(self, request):
        body = request.await_args.kwargs["body_params"]
        assert "identifier" in body
        # Must be a unique, valid client identifier (uuid4 in this slice).
        uuid.UUID(str(body["identifier"]))

    @pytest.mark.asyncio
    async def test_place_buy_order_includes_identifier(self, monkeypatch):
        orders, request = self._mock_request(monkeypatch)
        await orders.place_buy_order("KRW-BTC", "1000", "0.5", "limit")
        self._assert_valid_identifier(request)

    @pytest.mark.asyncio
    async def test_place_sell_order_includes_identifier(self, monkeypatch):
        orders, request = self._mock_request(monkeypatch)
        await orders.place_sell_order("KRW-BTC", "0.5", "1000")
        self._assert_valid_identifier(request)

    @pytest.mark.asyncio
    async def test_place_market_sell_order_includes_identifier(self, monkeypatch):
        orders, request = self._mock_request(monkeypatch)
        await orders.place_market_sell_order("KRW-BTC", "0.5")
        self._assert_valid_identifier(request)

    @pytest.mark.asyncio
    async def test_place_market_buy_order_includes_identifier(self, monkeypatch):
        orders, request = self._mock_request(monkeypatch)
        await orders.place_market_buy_order("KRW-BTC", "10000")
        self._assert_valid_identifier(request)

    @pytest.mark.asyncio
    async def test_each_order_gets_a_distinct_identifier(self, monkeypatch):
        orders, request = self._mock_request(monkeypatch)
        await orders.place_buy_order("KRW-BTC", "1000", "0.5", "limit")
        first = request.await_args.kwargs["body_params"]["identifier"]
        await orders.place_buy_order("KRW-BTC", "1000", "0.5", "limit")
        second = request.await_args.kwargs["body_params"]["identifier"]
        assert first != second


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_order_by_identifier_uses_read_only_get(monkeypatch):
    from app.services.brokers.upbit import orders

    request = AsyncMock(return_value={"uuid": "35bee07f-full", "state": "wait"})
    monkeypatch.setattr(orders._client, "_request_with_auth", request)

    result = await orders.fetch_order_by_identifier("oprop-fixed")

    assert result["uuid"] == "35bee07f-full"
    request.assert_awaited_once_with(
        "GET",
        f"{orders._client.UPBIT_REST}/order",
        query_params={"identifier": "oprop-fixed"},
    )
