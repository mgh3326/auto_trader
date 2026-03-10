from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot
from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_get_orderbook_returns_kr_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=OrderbookSnapshot(
                symbol="005930",
                instrument_type="equity_kr",
                source="kis",
                asks=[OrderbookLevel(price=70100, quantity=123)],
                bids=[OrderbookLevel(price=70000, quantity=321)],
                total_ask_qty=1000,
                total_bid_qty=1500,
                bid_ask_ratio=1.5,
                expected_price=70050,
                expected_qty=42,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("5930")

    assert result == {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "source": "kis",
        "asks": [{"price": 70100, "quantity": 123}],
        "bids": [{"price": 70000, "quantity": 321}],
        "total_ask_qty": 1000,
        "total_bid_qty": 1500,
        "bid_ask_ratio": 1.5,
        "expected_price": 70050,
        "expected_qty": 42,
    }


@pytest.mark.asyncio
async def test_get_orderbook_preserves_null_expected_qty_without_extra_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=OrderbookSnapshot(
                symbol="005930",
                instrument_type="equity_kr",
                source="kis",
                asks=[OrderbookLevel(price=70100, quantity=123)],
                bids=[OrderbookLevel(price=70000, quantity=321)],
                total_ask_qty=1000,
                total_bid_qty=1500,
                bid_ask_ratio=1.5,
                expected_price=70050,
                expected_qty=None,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("5930")

    assert result == {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "source": "kis",
        "asks": [{"price": 70100, "quantity": 123}],
        "bids": [{"price": 70000, "quantity": 321}],
        "total_ask_qty": 1000,
        "total_bid_qty": 1500,
        "bid_ask_ratio": 1.5,
        "expected_price": 70050,
        "expected_qty": None,
    }


@pytest.mark.asyncio
async def test_get_orderbook_returns_error_payload_on_kis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(side_effect=RuntimeError("kis down")),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result == {
        "error": "kis down",
        "source": "kis",
        "symbol": "005930",
        "instrument_type": "equity_kr",
    }


@pytest.mark.asyncio
async def test_get_orderbook_raises_on_invalid_input() -> None:
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_orderbook"]("")

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_orderbook"]("AAPL", market="kr")

    with pytest.raises(ValueError, match="get_orderbook only supports KR market"):
        await tools["get_orderbook"]("005930", market="us")
