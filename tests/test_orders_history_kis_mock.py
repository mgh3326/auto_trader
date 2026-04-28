import pytest

PENDING_MOCK_UNSUPPORTED_BY_MARKET = {
    "kr": (
        "equity_kr",
        "KIS domestic pending-orders inquiry (TTTC8036R) is not available in mock mode.",
    ),
    "us": (
        "equity_us",
        "KIS overseas pending-orders inquiry (TTTS3018R) is not available in mock mode.",
    ),
}


class PendingMockUnsupportedKIS:
    def __init__(self, *, is_mock: bool = False) -> None:
        self.is_mock = is_mock

    async def inquire_korea_orders(self, *, is_mock=False):
        raise RuntimeError(PENDING_MOCK_UNSUPPORTED_BY_MARKET["kr"][1])

    async def inquire_overseas_orders(self, exchange, *, is_mock=False):
        raise RuntimeError(PENDING_MOCK_UNSUPPORTED_BY_MARKET["us"][1])


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_get_order_history_pending_mock_surfaces_unsupported(
    monkeypatch,
    market: str,
):
    """Mock pending history must surface a structured mock-unsupported error."""
    from app.mcp_server.tooling import orders_history

    expected_market, _ = PENDING_MOCK_UNSUPPORTED_BY_MARKET[market]
    monkeypatch.setattr(orders_history, "KISClient", PendingMockUnsupportedKIS)

    result = await orders_history.get_order_history_impl(
        status="pending", market=market, is_mock=True
    )

    assert result["orders"] == []
    assert any(
        e.get("market") == expected_market
        and e.get("mock_unsupported") is True
        and "mock" in (e.get("error") or "").lower()
        for e in result["errors"]
    ), result["errors"]
