from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

# These will be implemented in ROB-561
from app.mcp_server.tooling.orders_toss_variants import (
    toss_place_order,
    toss_preview_order,
)

# We'll use monkeypatch to check internal helper if needed,
# or just check the external side effects (payloads/responses).
from tests.test_mcp_toss_order_variants import MockTossClient


@pytest.mark.asyncio
async def test_toss_preview_order_snaps_price_kr(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    _ = MockTossClient(monkeypatch)

    # KR 005930, price 87350. Tick size for 50k-200k is 100.
    # Buy should floor to 87300.
    res = await toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        price="87350",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["payload_preview"]["price"] == "87300"  # Expect snapped
    assert res["tick_adjusted"] is True
    assert res["original_price"] == "87350"
    assert res["adjusted_price"] == "87300"


@pytest.mark.asyncio
async def test_toss_preview_order_snaps_price_kr_sell(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    _ = MockTossClient(monkeypatch)

    # KR 005930, price 87350. Sell should ceil to 87400.
    res = await toss_preview_order(
        symbol="005930",
        side="sell",
        order_type="limit",
        price="87350",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["payload_preview"]["price"] == "87400"
    assert res["tick_adjusted"] is True
    assert res["original_price"] == "87350"
    assert res["adjusted_price"] == "87400"


@pytest.mark.asyncio
async def test_toss_place_order_snaps_price_and_includes_meta(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    # Mock record_toss_place_order to avoid DB
    monkeypatch.setattr(
        otv, "record_toss_place_order", AsyncMock(return_value={"ledger_id": 1})
    )

    # KR 005930, price 87350 -> buy floor 87300
    res = await toss_place_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="1",
        price="87350",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert mock_client.placed_payloads[0]["price"] == "87300"
    assert res["tick_adjusted"] is True
    assert res["original_price"] == "87350"
    assert res["adjusted_price"] == "87300"


@pytest.mark.asyncio
async def test_toss_place_order_uses_snapped_price_for_sell_loss_guard(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    # avg = 100,000. floor = 101,000.
    # Price 101,050. Tick for 50k-200k is 100.
    # Sell Ceil(101050, 100) = 101100. 101100 >= 101000 (Pass)
    # BUT if Ceil went DOWN (buy floor), it would be 101000 (Pass).
    # Let's pick a case where snapping MATTERS for the guard.
    # avg = 100,000. floor = 101,000.
    # User Price = 100,950.
    # If not snapped: 100,950 < 101,000 (Block)
    # Snapped Sell: Ceil(100950, 100) = 101000. 101000 >= 101000 (Pass!)

    mock_client.holdings_list = [
        {
            "symbol": "005930",
            "quantity": Decimal("10"),
            "average_purchase_price": Decimal("100000"),
            "last_price": Decimal("100000"),
            "name": "Samsung",
            "market_country": "KR",
            "currency": "KRW",
            "market_value": {},
            "profit_loss": {},
            "daily_profit_loss": {},
            "cost": {},
        }
    ]

    monkeypatch.setattr(
        otv, "record_toss_place_order", AsyncMock(return_value={"ledger_id": 1})
    )
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)

    res = await toss_place_order(
        symbol="005930",
        side="sell",
        order_type="limit",
        quantity="1",
        price="100950",  # Will snap to 101000
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    # If snapping is implemented BEFORE the guard, this should SUCCEED.
    assert res["success"] is True, (
        f"Order should succeed after snapping 100950 to 101000. Error: {res.get('error')}"
    )
    assert mock_client.placed_payloads[0]["price"] == "101000"


@pytest.mark.asyncio
async def test_us_limit_order_not_snapped(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    _ = MockTossClient(monkeypatch)

    # US AAPL, price 150.05. Should NOT be snapped.
    res = await toss_preview_order(
        symbol="AAPL",
        side="buy",
        order_type="limit",
        price="150.05",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["payload_preview"]["price"] == "150.05"
    assert "tick_adjusted" not in res
