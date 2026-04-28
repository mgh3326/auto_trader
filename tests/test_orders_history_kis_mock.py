import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_get_order_history_pending_kr_mock_surfaces_unsupported(
    monkeypatch,
):
    """KR mock pending must surface a structured mock-unsupported error."""
    from app.mcp_server.tooling import orders_history

    class FakeKIS:
        def __init__(self, *, is_mock: bool = False) -> None:
            pass

        async def inquire_korea_orders(self, *, is_mock=False):
            raise RuntimeError(
                "KIS domestic pending-orders inquiry (TTTC8036R) is not "
                "available in mock mode."
            )

    monkeypatch.setattr(orders_history, "KISClient", FakeKIS)
    result = await orders_history.get_order_history_impl(
        status="pending", market="kr", is_mock=True
    )

    assert result["orders"] == []
    assert any(
        e.get("market") == "equity_kr"
        and e.get("mock_unsupported") is True
        and "mock" in (e.get("error") or "").lower()
        for e in result["errors"]
    ), result["errors"]


@pytest.mark.asyncio
async def test_get_order_history_pending_us_mock_surfaces_unsupported(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_history

    class FakeKIS:
        def __init__(self, *, is_mock: bool = False) -> None:
            pass

        async def inquire_overseas_orders(self, exchange, *, is_mock=False):
            raise RuntimeError(
                "KIS overseas pending-orders inquiry (TTTS3018R) is not "
                "available in mock mode."
            )

    monkeypatch.setattr(orders_history, "KISClient", FakeKIS)
    result = await orders_history.get_order_history_impl(
        status="pending", market="us", is_mock=True
    )

    assert result["orders"] == []
    assert any(
        e.get("market") == "equity_us"
        and e.get("mock_unsupported") is True
        for e in result["errors"]
    ), result["errors"]
