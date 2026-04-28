"""Unit tests for research_run_live_refresh_service."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.research_run_decision_session import LiveRefreshSnapshot
from app.services.research_run_live_refresh_service import build_live_refresh_snapshot

pytestmark = pytest.mark.asyncio


@pytest.mark.unit
async def test_build_snapshot_returns_live_refresh_snapshot():
    """Test that build_live_refresh_snapshot returns a LiveRefreshSnapshot."""
    mock_run = SimpleNamespace(
        id=1,
        market_scope="kr",
        candidates=[
            SimpleNamespace(symbol="005930", payload={}),
        ],
        reconciliations=[],
    )
    mock_db = AsyncMock()

    with patch("app.services.market_data.get_quote") as mock_quote:
        with patch("app.services.market_data.get_orderbook") as mock_orderbook:
            with patch(
                "app.services.kr_symbol_universe_service.is_nxt_eligible"
            ) as mock_nxt:
                with patch(
                    "app.mcp_server.tooling.orders_history.get_order_history_impl"
                ) as mock_orders:
                    mock_quote.return_value = AsyncMock(price=70000.0)
                    mock_orderbook.return_value = AsyncMock(
                        bids=[SimpleNamespace(price=69900.0, quantity=100)],
                        asks=[SimpleNamespace(price=70100.0, quantity=100)],
                        total_bid_qty=1000.0,
                        total_ask_qty=1000.0,
                    )
                    mock_nxt.return_value = True
                    mock_orders.return_value = {"orders": []}

                    result = await build_live_refresh_snapshot(
                        cast(AsyncSession, mock_db), run=mock_run
                    )

                    assert isinstance(result, LiveRefreshSnapshot)
                    assert result.refreshed_at is not None
                    assert "005930" in result.quote_by_symbol


@pytest.mark.unit
async def test_build_snapshot_quote_failure_adds_warning():
    """Test that quote fetch failure adds a warning but doesn't fail."""
    mock_run = SimpleNamespace(
        id=1,
        market_scope="kr",
        candidates=[
            SimpleNamespace(symbol="005930", payload={}),
        ],
        reconciliations=[],
    )
    mock_db = AsyncMock()

    with patch("app.services.market_data.get_quote") as mock_quote:
        with patch("app.services.market_data.get_orderbook") as mock_orderbook:
            with patch(
                "app.services.kr_symbol_universe_service.is_nxt_eligible"
            ) as mock_nxt:
                with patch(
                    "app.mcp_server.tooling.orders_history.get_order_history_impl"
                ) as mock_orders:
                    mock_quote.side_effect = Exception("Quote failed")
                    mock_orderbook.return_value = AsyncMock(
                        bids=[],
                        asks=[],
                        total_bid_qty=0.0,
                        total_ask_qty=0.0,
                    )
                    mock_nxt.return_value = True
                    mock_orders.return_value = {"orders": []}

                    result = await build_live_refresh_snapshot(
                        cast(AsyncSession, mock_db), run=mock_run
                    )

                    assert isinstance(result, LiveRefreshSnapshot)
                    assert any("quote_failed:005930" in w for w in result.warnings)


@pytest.mark.unit
async def test_build_snapshot_us_skips_orderbook():
    """Test that US market skips orderbook fetch and adds warning."""
    mock_run = SimpleNamespace(
        id=1,
        market_scope="us",
        candidates=[
            SimpleNamespace(symbol="AAPL", payload={}),
        ],
        reconciliations=[],
    )
    mock_db = AsyncMock()

    with patch("app.services.market_data.get_quote") as mock_quote:
        with patch("app.services.market_data.get_orderbook") as mock_orderbook:
            with patch(
                "app.mcp_server.tooling.orders_history.get_order_history_impl"
            ) as mock_orders:
                mock_quote.return_value = AsyncMock(price=150.0)
                mock_orderbook.return_value = AsyncMock(
                    bids=[],
                    asks=[],
                    total_bid_qty=0.0,
                    total_ask_qty=0.0,
                )
                mock_orders.return_value = {"orders": []}

                result = await build_live_refresh_snapshot(
                    cast(AsyncSession, mock_db), run=mock_run
                )

                assert isinstance(result, LiveRefreshSnapshot)
                assert any("orderbook_unavailable_us" in w for w in result.warnings)


@pytest.mark.unit
async def test_build_snapshot_missing_kr_universe_adds_warning():
    """Test that missing KR universe row adds warning."""
    mock_run = SimpleNamespace(
        id=1,
        market_scope="kr",
        candidates=[
            SimpleNamespace(symbol="005930", payload={}),
        ],
        reconciliations=[],
    )
    mock_db = AsyncMock()

    with patch("app.services.market_data.get_quote") as mock_quote:
        with patch("app.services.market_data.get_orderbook") as mock_orderbook:
            with patch(
                "app.services.kr_symbol_universe_service.is_nxt_eligible"
            ) as mock_nxt:
                with patch(
                    "app.mcp_server.tooling.orders_history.get_order_history_impl"
                ) as mock_orders:
                    mock_quote.return_value = AsyncMock(price=70000.0)
                    mock_orderbook.return_value = AsyncMock(
                        bids=[],
                        asks=[],
                        total_bid_qty=0.0,
                        total_ask_qty=0.0,
                    )
                    mock_nxt.side_effect = Exception("DB error")
                    mock_db.execute.side_effect = Exception("DB error")
                    mock_orders.return_value = {"orders": []}

                    result = await build_live_refresh_snapshot(
                        cast(AsyncSession, mock_db), run=mock_run
                    )

                    assert isinstance(result, LiveRefreshSnapshot)
                    assert any(
                        "missing_kr_universe:005930" in w for w in result.warnings
                    )


@pytest.mark.unit
async def test_build_snapshot_refreshed_at_after_gather():
    """Test that refreshed_at is set after all fetches complete."""
    mock_run = SimpleNamespace(
        id=1,
        market_scope="kr",
        candidates=[
            SimpleNamespace(symbol="005930", payload={}),
        ],
        reconciliations=[],
    )
    mock_db = AsyncMock()
    start_time = datetime.now(UTC)

    with patch("app.services.market_data.get_quote") as mock_quote:
        with patch("app.services.market_data.get_orderbook") as mock_orderbook:
            with patch(
                "app.services.kr_symbol_universe_service.is_nxt_eligible"
            ) as mock_nxt:
                with patch(
                    "app.mcp_server.tooling.orders_history.get_order_history_impl"
                ) as mock_orders:
                    mock_quote.return_value = AsyncMock(price=70000.0)
                    mock_orderbook.return_value = AsyncMock(
                        bids=[],
                        asks=[],
                        total_bid_qty=0.0,
                        total_ask_qty=0.0,
                    )
                    mock_nxt.return_value = True
                    mock_orders.return_value = {"orders": []}

                    result = await build_live_refresh_snapshot(
                        cast(AsyncSession, mock_db), run=mock_run
                    )

                    assert result.refreshed_at >= start_time
