from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.services.invest_view_model.stock_detail_orders_service import (
    build_stock_detail_orders,
)


@pytest.mark.asyncio
async def test_stock_detail_orders_filters_symbol_and_paginates():
    async def fetcher(days, markets):
        assert days == 90
        assert markets == ["kr"]
        return [
            {
                "symbol": "005930",
                "side": "buy",
                "quantity": 1,
                "price": 70000,
                "filled_at": datetime.now(UTC),
                "order_id": "a",
            },
            {
                "symbol": "000660",
                "side": "sell",
                "quantity": 2,
                "price": 180000,
                "filled_at": datetime.now(UTC),
                "order_id": "b",
            },
            {
                "symbol": "005930",
                "side": "buy",
                "quantity": 3,
                "price": 71000,
                "filled_at": datetime.now(UTC),
                "order_id": "c",
            },
        ]

    response = await build_stock_detail_orders(
        market="kr",
        symbol="005930",
        fetcher=fetcher,
        limit=1,
        cursor="1",
    )

    assert [item.orderId for item in response.items] == ["c"]
    assert response.nextCursor is None
    assert response.meta.emptyState is None


@pytest.mark.asyncio
async def test_stock_detail_orders_empty_state_is_explicit():
    async def fetcher(days, markets):
        return []

    response = await build_stock_detail_orders(
        market="us",
        symbol="BRK-B",
        fetcher=fetcher,
    )

    assert response.symbol == "BRK.B"
    assert response.items == []
    assert response.meta.emptyState == "no_filled_orders"


@pytest.mark.asyncio
async def test_stock_detail_orders_clamps_days_at_365():
    seen = {}

    async def fetcher(days, markets):
        seen["days"] = days
        return []

    await build_stock_detail_orders(
        market="crypto",
        symbol="BTC-KRW",
        fetcher=fetcher,
        days=999,
    )

    assert seen["days"] == 365


@pytest.mark.asyncio
async def test_stock_detail_orders_timeout_degrades_to_empty_with_warning():
    async def fetcher(days, markets):
        await asyncio.sleep(0.05)
        return [
            {
                "symbol": "BTC",
                "side": "buy",
                "quantity": 1,
                "price": 100,
                "filled_at": datetime.now(UTC),
                "order_id": "late",
            }
        ]

    response = await build_stock_detail_orders(
        market="crypto",
        symbol="KRW-BTC",
        fetcher=fetcher,
        timeout_seconds=0.001,
    )

    assert response.symbol == "BTC"
    assert response.items == []
    assert response.meta.emptyState == "no_filled_orders"
    assert response.meta.warnings == ["filled_orders_timeout"]


@pytest.mark.asyncio
async def test_stock_detail_orders_fetch_error_degrades_to_empty_with_warning():
    async def fetcher(days, markets):
        raise RuntimeError("broker history unavailable")

    response = await build_stock_detail_orders(
        market="crypto",
        symbol="BTC-KRW",
        fetcher=fetcher,
    )

    assert response.symbol == "BTC"
    assert response.items == []
    assert response.meta.emptyState == "no_filled_orders"
    assert response.meta.warnings == ["filled_orders_unavailable"]
