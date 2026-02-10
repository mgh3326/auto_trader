"""Upbit order history/modify helper tests."""

from unittest.mock import AsyncMock

import pytest

from app.services import upbit


@pytest.mark.asyncio
async def test_fetch_closed_orders_builds_query(monkeypatch):
    request_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(upbit, "_request_with_auth", request_mock)

    await upbit.fetch_closed_orders(market="KRW-BTC", limit=15)

    request_mock.assert_awaited_once()
    method, url = request_mock.await_args.args[:2]
    query_params = request_mock.await_args.kwargs["query_params"]

    assert method == "GET"
    assert url.endswith("/orders/closed")
    assert query_params["market"] == "KRW-BTC"
    assert query_params["limit"] == 15
    assert query_params["states[]"] == ["done", "cancel"]


@pytest.mark.asyncio
async def test_fetch_order_detail_builds_query(monkeypatch):
    request_mock = AsyncMock(return_value={"uuid": "order-1"})
    monkeypatch.setattr(upbit, "_request_with_auth", request_mock)

    result = await upbit.fetch_order_detail("order-1")

    assert result["uuid"] == "order-1"
    request_mock.assert_awaited_once_with(
        "GET",
        f"{upbit.UPBIT_REST}/order",
        query_params={"uuid": "order-1"},
    )


@pytest.mark.asyncio
async def test_cancel_and_reorder_bid_success(monkeypatch):
    monkeypatch.setattr(
        upbit,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "bid",
                "market": "KRW-BTC",
                "remaining_volume": "0.001",
            }
        ),
    )
    monkeypatch.setattr(
        upbit, "cancel_orders", AsyncMock(return_value=[{"uuid": "order-1"}])
    )

    place_buy_mock = AsyncMock(return_value={"uuid": "order-2"})
    place_sell_mock = AsyncMock()
    monkeypatch.setattr(upbit, "place_buy_order", place_buy_mock)
    monkeypatch.setattr(upbit, "place_sell_order", place_sell_mock)

    result = await upbit.cancel_and_reorder(
        order_uuid="order-1",
        new_price=56000000,
        new_quantity=0.0015,
    )

    assert result["new_order"]["uuid"] == "order-2"
    place_buy_mock.assert_awaited_once_with(
        "KRW-BTC", "56000000", "0.00150000", "limit"
    )
    place_sell_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_and_reorder_ask_success(monkeypatch):
    monkeypatch.setattr(
        upbit,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "ask",
                "market": "KRW-BTC",
                "remaining_volume": "0.001",
            }
        ),
    )
    monkeypatch.setattr(
        upbit, "cancel_orders", AsyncMock(return_value=[{"uuid": "order-1"}])
    )

    place_buy_mock = AsyncMock()
    place_sell_mock = AsyncMock(return_value={"uuid": "order-3"})
    monkeypatch.setattr(upbit, "place_buy_order", place_buy_mock)
    monkeypatch.setattr(upbit, "place_sell_order", place_sell_mock)

    result = await upbit.cancel_and_reorder(
        order_uuid="order-1",
        new_price=56000000,
        new_quantity=0.0015,
    )

    assert result["new_order"]["uuid"] == "order-3"
    place_sell_mock.assert_awaited_once_with("KRW-BTC", "0.00150000", "56000000")
    place_buy_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_and_reorder_rejects_non_wait(monkeypatch):
    monkeypatch.setattr(
        upbit,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "done",
                "ord_type": "limit",
            }
        ),
    )

    result = await upbit.cancel_and_reorder(order_uuid="order-1", new_price=56000000)

    assert result["new_order"] is None
    assert "wait-state" in result["cancel_result"]["error"]


@pytest.mark.asyncio
async def test_cancel_and_reorder_rejects_non_limit(monkeypatch):
    monkeypatch.setattr(
        upbit,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "market",
            }
        ),
    )

    result = await upbit.cancel_and_reorder(order_uuid="order-1", new_price=56000000)

    assert result["new_order"] is None
    assert "limit" in result["cancel_result"]["error"]


@pytest.mark.asyncio
async def test_cancel_and_reorder_rejects_invalid_quantity(monkeypatch):
    monkeypatch.setattr(
        upbit,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "remaining_volume": "0",
            }
        ),
    )

    result = await upbit.cancel_and_reorder(order_uuid="order-1", new_price=56000000)

    assert result["new_order"] is None
    assert "must be positive" in result["cancel_result"]["error"]


@pytest.mark.asyncio
async def test_cancel_and_reorder_handles_cancel_failure(monkeypatch):
    monkeypatch.setattr(
        upbit,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "order-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "bid",
                "market": "KRW-BTC",
                "remaining_volume": "0.001",
            }
        ),
    )
    monkeypatch.setattr(
        upbit,
        "cancel_orders",
        AsyncMock(return_value=[{"uuid": "order-1", "error": "cancel failed"}]),
    )

    result = await upbit.cancel_and_reorder(
        order_uuid="order-1",
        new_price=56000000,
        new_quantity=0.001,
    )

    assert result["new_order"] is None
    assert result["cancel_result"]["error"] == "cancel failed"
