from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot
from tests._mcp_tooling_support import build_tools


def _make_snapshot(**overrides: object) -> OrderbookSnapshot:
    snapshot = OrderbookSnapshot(
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
    for key, value in overrides.items():
        setattr(snapshot, key, value)
    return snapshot


@pytest.mark.asyncio
async def test_get_orderbook_returns_kr_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_make_snapshot()),
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
        "pressure": "buy",
        "pressure_desc": "매수잔량이 매도잔량의 1.5배 - 매수 압력",
        "spread": 100,
        "spread_pct": 0.143,
        "expected_price": 70050,
        "expected_qty": 42,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "total_ask_qty",
        "total_bid_qty",
        "bid_ask_ratio",
        "expected_pressure",
        "expected_desc",
    ),
    [
        (1000, 2100, 2.1, "strong_buy", "매수잔량이 매도잔량의 2.1배 - 강한 매수 압력"),
        (1000, 2000, 2.0, "buy", "매수잔량이 매도잔량의 2.0배 - 매수 압력"),
        (1000, 1300, 1.3, "neutral", "매수/매도 잔량이 균형권 - 중립"),
        (1000, 700, 0.7, "neutral", "매수/매도 잔량이 균형권 - 중립"),
        (1000, 500, 0.5, "sell", "매도잔량이 매수잔량의 2.0배 - 매도 압력"),
        (1000, 400, 0.4, "strong_sell", "매도잔량이 매수잔량의 2.5배 - 강한 매도 압력"),
    ],
)
async def test_get_orderbook_classifies_pressure_by_ratio_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    total_ask_qty: int,
    total_bid_qty: int,
    bid_ask_ratio: float,
    expected_pressure: str,
    expected_desc: str,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_make_snapshot(
                total_ask_qty=total_ask_qty,
                total_bid_qty=total_bid_qty,
                bid_ask_ratio=bid_ask_ratio,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result["pressure"] == expected_pressure
    assert result["pressure_desc"] == expected_desc


@pytest.mark.asyncio
async def test_get_orderbook_returns_null_pressure_when_ratio_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=_make_snapshot(
                total_ask_qty=0,
                total_bid_qty=1500,
                bid_ask_ratio=None,
            )
        ),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result["pressure"] is None
    assert result["pressure_desc"] is None
    assert result["spread"] == 100
    assert result["spread_pct"] == pytest.approx(0.143)


@pytest.mark.asyncio
async def test_get_orderbook_preserves_null_expected_qty_in_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_make_snapshot(expected_qty=None)),
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
        "pressure": "buy",
        "pressure_desc": "매수잔량이 매도잔량의 1.5배 - 매수 압력",
        "spread": 100,
        "spread_pct": 0.143,
        "expected_price": 70050,
        "expected_qty": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asks", "bids", "expected_spread", "expected_spread_pct"),
    [
        ([], [OrderbookLevel(price=70000, quantity=321)], None, None),
        ([OrderbookLevel(price=70100, quantity=123)], [], None, None),
        (
            [OrderbookLevel(price=70100, quantity=123)],
            [OrderbookLevel(price=0, quantity=321)],
            None,
            None,
        ),
    ],
)
async def test_get_orderbook_returns_null_spread_when_best_prices_unusable(
    monkeypatch: pytest.MonkeyPatch,
    asks: list[OrderbookLevel],
    bids: list[OrderbookLevel],
    expected_spread: int | None,
    expected_spread_pct: float | None,
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_make_snapshot(asks=asks, bids=bids)),
    )
    tools = build_tools()

    result = await tools["get_orderbook"]("005930")

    assert result["spread"] == expected_spread
    assert result["spread_pct"] == expected_spread_pct


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
